"""
Extraction layer, shared by the scheduled weekly email and the
on-demand summary API.

SQL here fetches raw counts grouped by status (and, where relevant,
by provider/operator/channel) - it does NOT decide what counts as
"success" or "resolved". That decision is provider-specific (pawapay's
success marker is COMPLETED, razorpay's is AUTHORIZED, and each
excludes different pending-like statuses), so it lives in providers.py
instead of being baked into SQL as literal strings that only happened
to be correct for pawapay.

fetch_all() no longer decides which cp_product_ids are in scope
either - the weekly job and the on-demand API have different, unrelated
eligibility rules (weekly_summary_enabled vs. role + ownership), so
each caller resolves its own scope and passes the resulting
cp_product_ids in explicitly. get_weekly_eligible_cp_product_ids()
below is main.py's rule; auth.py has the API's.

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
    classify_hourly_counts,
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


# main.py's eligibility rule for the scheduled weekly email - separate
# from the on-demand API's, which lives in auth.py instead, since the
# two features decide "who's in scope" in completely unrelated ways.
WEEKLY_ELIGIBLE_CP_PRODUCT_IDS_SQL = """
    SELECT DISTINCT cpp.id AS cp_product_id
    FROM dtpay_cp_products cpp
    JOIN dtpay_users u ON u.id = cpp.cp_id
    WHERE UPPER(cpp.status) = 'APPROVED'
      AND u.enabled = 1
      AND u.weekly_summary_enabled = 1
"""


def get_weekly_eligible_cp_product_ids():
    """cp_product_ids eligible for the scheduled weekly email: approved,
    enabled, and opted in. Cheap - only touches the config tables."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(WEEKLY_ELIGIBLE_CP_PRODUCT_IDS_SQL)
            return tuple(row["cp_product_id"] for row in cur.fetchall())


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
    WHERE t.cp_product_id IN %(cp_product_ids)s
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
    WHERE cp_product_id IN %(cp_product_ids)s
      AND date_time >= %(day_start)s
      AND date_time <  %(day_end)s
    GROUP BY product_id, DATE(date_time), agg_name, transaction_status
"""

# Same idea again, truncated to the hour - only requested for short
# enough ranges (see api.py's ON_DEMAND_MAX_HOURLY_DAYS), since a
# multi-week hourly breakdown would be hundreds of rows and not
# actually more useful than the daily one.
HOURLY_STATUS_COUNTS_SQL = """
    SELECT
        product_id,
        DATE_ADD(DATE(date_time), INTERVAL HOUR(date_time) HOUR) AS hour,
        agg_name                                                  AS provider,
        transaction_status,
        COUNT(*)                                                  AS count
    FROM payment_transactions
    WHERE cp_product_id IN %(cp_product_ids)s
      AND date_time >= %(day_start)s
      AND date_time <  %(day_end)s
    GROUP BY product_id, hour, agg_name, transaction_status
"""

# reason_code: cb_error_message is 'CODE#description' on pawapay's
# checkout-stage failures (contains '#'); everything else falls back
# to partner_error_message, or to transaction_status itself if
# partner_error_message is empty or just the generic acceptance
# placeholder. This is provider-agnostic on purpose - it's a content
# check (does this field look like CODE#description), not a check
# against a specific provider's status literal.
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
    WHERE cp_product_id IN %(cp_product_ids)s
      AND date_time >= %(day_start)s
      AND date_time <  %(day_end)s
    GROUP BY product_id, agg_name, transaction_status, reason_code
"""

# Same reason_code logic, broken out by day too - lets a chat question
# like "what were the errors on the 25th" get answered instead of only
# "what were the errors overall" (the gap that motivated adding this:
# the aggregate breakdown alone was never enough for a day-specific
# question). Only ever requested for a MAX_DATE_RANGE_DAYS-bounded
# window (7 days by default), so this doesn't carry the same
# row-count risk hourly breakdown does - no separate cap needed.
DAILY_REASON_COUNTS_SQL = """
    SELECT
        product_id,
        DATE(date_time) AS day,
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
    WHERE cp_product_id IN %(cp_product_ids)s
      AND date_time >= %(day_start)s
      AND date_time <  %(day_end)s
    GROUP BY product_id, DATE(date_time), agg_name, transaction_status, reason_code
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
    WHERE cp_product_id IN %(cp_product_ids)s
      AND date_time >= %(day_start)s
      AND date_time <  %(day_end)s
    GROUP BY product_id, operator, channel, status
"""

DAILY_OPERATOR_COUNTS_SQL = """
    SELECT
        product_id,
        DATE(date_time) AS day,
        operator,
        channel,
        status,
        COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s
      AND date_time >= %(day_start)s
      AND date_time <  %(day_end)s
    GROUP BY product_id, DATE(date_time), operator, channel, status
"""


def fetch_all(day_start, day_end, cp_product_ids, min_resolved=None, include_daily=False,
               include_hourly=False, include_daily_reasons=False):
    """
    Runs the extraction queries for the [day_start, day_end) window,
    scoped to exactly the given cp_product_ids, and classifies the
    results per-provider (see providers.py). Returns a list of
    per-product metric rows, a list of per-product reason rows, a
    list of per-product operator/channel rows, a list of
    per-product-per-day rows (if include_daily), a list of
    per-product-per-hour rows (if include_hourly), a list of
    per-product-per-day reason rows (if include_daily_reasons), and a
    list of per-product-per-day operator rows (if
    include_daily_reasons). min_resolved defaults to config's
    MIN_RESOLVED_THRESHOLD.

    include_daily_reasons is separate from include_daily on purpose -
    the weekly email wants the daily total_resolved/completed chart
    but has no use for a day-level reason/operator breakdown (its
    summary only ever discusses reasons in aggregate), so it isn't
    charged for two extra queries it would never use. The on-demand
    API's chat feature is what actually needs this, to answer a
    question like "what were the errors on the 25th" instead of only
    being able to answer it in aggregate across the whole range.

    cp_product_ids is the caller's responsibility to resolve - this
    function doesn't know or care whether that's "everyone opted into
    the weekly email" or "this one admin/user's allowed products for
    an API request". An empty tuple returns empty results immediately
    without touching payment_transactions/payout_logs at all.
    """
    if min_resolved is None:
        min_resolved = MIN_RESOLVED_THRESHOLD
    if not cp_product_ids:
        return [], [], [], [], [], [], []

    params = {
        "day_start": day_start,
        "day_end": day_end,
        "cp_product_ids": tuple(cp_product_ids),
    }

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

            hourly_status_counts = []
            if include_hourly:
                cur.execute(HOURLY_STATUS_COUNTS_SQL, params)
                hourly_status_counts = _clean_rows(cur.fetchall())

            daily_reason_counts = []
            daily_operator_counts = []
            if include_daily_reasons:
                cur.execute(DAILY_REASON_COUNTS_SQL, params)
                daily_reason_counts = _clean_rows(cur.fetchall())
                cur.execute(DAILY_OPERATOR_COUNTS_SQL, params)
                daily_operator_counts = _clean_rows(cur.fetchall())

    product_metrics = classify_status_counts(status_counts, min_resolved)

    # Only keep reason/operator/daily/hourly rows for products that
    # actually made the cut above (over the volume floor too, not
    # just in scope).
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

    hourly_metrics = []
    if include_hourly:
        hourly_rows = [h for h in hourly_status_counts if h["product_id"] in kept_product_ids]
        hourly_metrics = classify_hourly_counts(hourly_rows)

    daily_reason_breakdown = []
    daily_operator_breakdown = []
    if include_daily_reasons:
        daily_reason_rows = filter_reason_rows(
            [r for r in daily_reason_counts if r["product_id"] in kept_product_ids]
        )
        daily_reason_breakdown = [
            {"product_id": r["product_id"], "day": r["day"], "transaction_status": r["transaction_status"],
             "reason_code": r["reason_code"], "occurrences": r["count"]}
            for r in daily_reason_rows
        ]

        daily_operator_rows = [o for o in daily_operator_counts if o["product_id"] in kept_product_ids]
        for row in daily_operator_rows:
            row["provider"] = provider_by_product.get(row["product_id"])
        daily_operator_breakdown = classify_operator_counts(daily_operator_rows, by_day=True)

    return (product_metrics, reason_breakdown, operator_breakdown, daily_metrics,
        hourly_metrics, daily_reason_breakdown, daily_operator_breakdown)