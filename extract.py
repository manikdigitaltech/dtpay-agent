"""
Extraction layer for the weekly partner performance agent.

SQL here fetches raw counts grouped by status (and, where relevant,
by provider/operator/channel) - it does NOT decide what counts as
"success" or "resolved" anymore. That decision is provider-specific
(pawapay's success marker is COMPLETED, razorpay's is AUTHORIZED, and
each excludes different pending-like statuses), so it lives in
providers.py instead of being baked into SQL as literal strings that
only happened to be correct for pawapay.

See dtpay_daily_metrics_draft.sql for the original single-provider
version of these queries and the earlier assumptions verified against
real pawapay data before the provider split existed.
"""
import pymysql
import pymysql.cursors
from decimal import Decimal

from config import DB_CONFIG, MIN_RESOLVED_THRESHOLD
from providers import (
    classify_status_counts,
    classify_daily_counts,
    filter_reason_rows,
    classify_operator_counts,
)


def get_connection():
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **DB_CONFIG)


# pymysql returns SUM()/ROUND() results as decimal.Decimal, not int or
# float - confirmed directly against a real connection. json.dumps()
# in analyze.py can't serialize Decimal at all, so this cleans every
# row right after fetching, before anything downstream ever sees one.
# Rate-like fields become float; anything else that's a Decimal is a
# count and becomes int.
_FLOAT_FIELDS = {"conversion_rate_pct"}


def _clean_rows(rows):
    for row in rows:
        for key, value in row.items():
            if isinstance(value, Decimal):
                row[key] = float(value) if key in _FLOAT_FIELDS else int(value)
    return rows


# One row per (product, provider, transaction_status) combination in
# the window - no HAVING threshold here, since "how many resolved"
# depends on which statuses that product's provider excludes, decided
# in providers.classify_status_counts() after this comes back.
STATUS_COUNTS_SQL = """
    SELECT
        t.product_id,
        t.product_name,
        t.country_name                                                     AS country_code,
        t.agg_name                                                          AS provider,
        t.transaction_status,
        cpp.id                                                             AS cp_product_id,
        u.id                                                                AS cp_id,
        u.email                                                             AS partner_email,
        u.company_name                                                      AS partner_name,
        COUNT(*)                                                            AS count
    FROM payment_transactions t
    JOIN dtpay_cp_products cpp ON cpp.id = t.cp_product_id
    JOIN dtpay_users u         ON u.id   = cpp.cp_id
    WHERE UPPER(cpp.status) = 'APPROVED'
      AND u.enabled = 1
      AND t.date_time >= %(day_start)s
      AND t.date_time <  %(day_end)s
    GROUP BY t.product_id, t.product_name, t.country_name, t.agg_name,
             t.transaction_status, cpp.id, u.id, u.email, u.company_name
"""

# Same idea, grouped by day instead of collapsing the whole window -
# feeds the within-week trend chart.
DAILY_STATUS_COUNTS_SQL = """
    SELECT
        product_id,
        DATE(date_time)     AS day,
        agg_name             AS provider,
        transaction_status,
        COUNT(*)             AS count
    FROM payment_transactions
    WHERE date_time >= %(day_start)s
      AND date_time <  %(day_end)s
    GROUP BY product_id, DATE(date_time), agg_name, transaction_status
"""

# reason_code: cb_error_message is 'CODE#description' on pawapay's
# checkout-stage failures (contains '#'); everything else falls back
# to partner_error_message, or to transaction_status itself if
# partner_error_message is empty or just the generic acceptance
# placeholder. This is provider-agnostic on purpose - it's a content
# check (does this field look like CODE#description), not a check
# against a specific provider's status literal, so it doesn't need a
# separate branch for razorpay, which barely uses cb_error_message at
# all in what's been seen so far.
REASON_COUNTS_SQL = """
    SELECT
        product_id,
        agg_name AS provider,
        transaction_status,
        CASE
            WHEN cb_error_message LIKE '%%#%%'
                THEN SUBSTRING_INDEX(cb_error_message, '#', 1)
            WHEN partner_error_message IS NULL
                 OR partner_error_message LIKE 'Request completed successfully%%'
                THEN COALESCE(transaction_status, 'UNKNOWN')
            ELSE partner_error_message
        END AS reason_code,
        COUNT(*) AS count
    FROM payment_transactions
    WHERE date_time >= %(day_start)s
      AND date_time <  %(day_end)s
    GROUP BY product_id, agg_name, transaction_status, reason_code
"""

# Fetches both operator (pawapay's network) and channel (razorpay's
# payment method) - providers.classify_operator_counts() picks the
# right one per product's provider rather than this query guessing.
OPERATOR_COUNTS_SQL = """
    SELECT
        product_id,
        operator,
        channel,
        status,
        COUNT(*) AS count
    FROM payout_logs
    WHERE date_time >= %(day_start)s
      AND date_time <  %(day_end)s
    GROUP BY product_id, operator, channel, status
"""


def fetch_all(day_start, day_end, min_resolved=None, include_daily=False):
    """
    Runs the extraction queries for the [day_start, day_end) window
    and classifies the results per-provider (see providers.py),
    returning the same shape as before the provider split: a list of
    per-product metric rows, a list of per-product reason rows, a
    list of per-product operator/channel rows, and (if include_daily)
    a list of per-product-per-day rows. min_resolved defaults to
    config's MIN_RESOLVED_THRESHOLD.

    include_daily only runs the daily breakdown - only needed for the
    current week (the trend chart), not the previous week.
    """
    if min_resolved is None:
        min_resolved = MIN_RESOLVED_THRESHOLD
    params = {"day_start": day_start, "day_end": day_end}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(STATUS_COUNTS_SQL, params)
            status_counts = _clean_rows(cur.fetchall())

            cur.execute(REASON_COUNTS_SQL, params)
            reason_counts = _clean_rows(cur.fetchall())

            cur.execute(OPERATOR_COUNTS_SQL, params)
            operator_counts = _clean_rows(cur.fetchall())

            daily_status_counts = []
            if include_daily:
                cur.execute(DAILY_STATUS_COUNTS_SQL, params)
                daily_status_counts = _clean_rows(cur.fetchall())

    product_metrics = classify_status_counts(status_counts, min_resolved)

    # Only keep reason/operator rows for products that actually made
    # the cut above (approved, enabled, over the volume floor).
    kept_product_ids = {p["product_id"] for p in product_metrics}
    reason_rows = filter_reason_rows(
        [r for r in reason_counts if r["product_id"] in kept_product_ids]
    )
    reason_breakdown = [
        {"product_id": r["product_id"], "transaction_status": r["transaction_status"],
         "reason_code": r["reason_code"], "occurrences": r["count"]}
        for r in reason_rows
    ]

    operator_rows = [o for o in operator_counts if o["product_id"] in kept_product_ids]
    # Tag each payout_logs row with its product's provider (payout_logs
    # doesn't reliably carry its own agg_name the way payment_transactions
    # does), then let providers.py pick operator vs channel per provider.
    provider_by_product = {p["product_id"]: p["provider"] for p in product_metrics}
    for row in operator_rows:
        row["provider"] = provider_by_product.get(row["product_id"])
    operator_breakdown = classify_operator_counts(operator_rows)

    daily_metrics = []
    if include_daily:
        daily_rows = [d for d in daily_status_counts if d["product_id"] in kept_product_ids]
        daily_metrics = classify_daily_counts(daily_rows)

    return product_metrics, reason_breakdown, operator_breakdown, daily_metrics