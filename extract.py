"""
Extraction layer for the weekly partner performance agent.

Two parallel paths now, selected per-product by provider
(providers.uses_payout_logs_metrics):
- LEGACY path (payment_transactions-based): still used for razorpay,
  unchanged from before.
- NEW path (payout_logs-based): used for pawapay. Confirmed by
  reverse-engineering the existing dashboard's real numbers exactly -
  payment_transactions is unique users who opened checkout (top of
  funnel); payout_logs is each individual operator-API attempt
  (can be multiple per user, on retry), and that's where conversion
  actually happens. Both sets of queries always run; which result a
  given product actually uses is decided in Python by its provider.
"""
import pymysql
import pymysql.cursors
from decimal import Decimal

from config import DB_CONFIG, MIN_RESOLVED_THRESHOLD
from providers import (
    PROVIDER_RULES,
    uses_payout_logs_metrics,
    classify_status_counts,
    classify_payout_status_counts,
    classify_daily_counts,
    classify_payout_daily_counts,
    classify_hourly_counts,
    classify_payout_hourly_counts,
    filter_reason_rows,
    filter_payout_reason_rows,
    merge_duplicate_reasons,
    classify_operator_counts,
    classify_campaign_counts,
    classify_payout_campaign_counts,
)


def get_connection():
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **DB_CONFIG)


_FLOAT_FIELDS = {"conversion_rate_pct"}


def _clean_rows(rows):
    for row in rows:
        for key, value in row.items():
            if isinstance(value, Decimal):
                row[key] = float(value) if key in _FLOAT_FIELDS else int(value)
    return rows


WEEKLY_ELIGIBLE_CP_PRODUCT_IDS_SQL = """
    SELECT DISTINCT cpp.id AS cp_product_id
    FROM dtpay_cp_products cpp
    JOIN dtpay_users u ON u.id = cpp.cp_id
    WHERE UPPER(cpp.status) = 'APPROVED'
      AND u.enabled = 1
      AND u.weekly_summary_enabled = 1
"""


def get_weekly_eligible_cp_product_ids():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(WEEKLY_ELIGIBLE_CP_PRODUCT_IDS_SQL)
            return tuple(row["cp_product_id"] for row in cur.fetchall())


# campaign_id is indexed in both tables, and one campaign_id maps to
# exactly one campaign_name, so the first non-null name found is the name.
# payout_logs first, payment_transactions as a fallback for a campaign
# whose rows only ever reached checkout.
CAMPAIGN_NAME_SQL = """
    SELECT campaign_name FROM {table}
    WHERE campaign_id = %(campaign_id)s AND cp_product_id = %(cp_product_id)s
      AND campaign_name IS NOT NULL AND campaign_name <> ''
    LIMIT 1
"""


def get_campaign_name(cp_product_id, campaign_id):
    """Returns the campaign's name, or None if this campaign_id has no
    rows for this cp_product_id in either table."""
    params = {"cp_product_id": cp_product_id, "campaign_id": campaign_id}
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in ("payout_logs", "payment_transactions"):
                cur.execute(CAMPAIGN_NAME_SQL.format(table=table), params)
                row = cur.fetchone()
                if row is not None:
                    return row["campaign_name"]
    return None


# ============================================================
# LEGACY path - payment_transactions-based (razorpay uses this)
# ============================================================

STATUS_COUNTS_SQL = """
    SELECT
        t.product_id, t.product_name, t.country_name AS country_code,
        t.agg_name AS provider, t.transaction_status,
        cpp.id AS cp_product_id, u.id AS cp_id, u.email AS partner_email, u.company_name AS partner_name,
        COUNT(*) AS count
    FROM payment_transactions t
    JOIN dtpay_cp_products cpp ON cpp.id = t.cp_product_id
    JOIN dtpay_users u         ON u.id   = cpp.cp_id
    WHERE t.cp_product_id IN %(cp_product_ids)s
      AND t.date_time >= %(day_start)s AND t.date_time < %(day_end)s
      AND (%(campaign_id)s IS NULL OR t.campaign_id = %(campaign_id)s)
    GROUP BY t.product_id, t.product_name, t.country_name, t.agg_name,
             t.transaction_status, cpp.id, u.id, u.email, u.company_name
"""

DAILY_STATUS_COUNTS_SQL = """
    SELECT product_id, DATE(date_time) AS day, agg_name AS provider, transaction_status, COUNT(*) AS count
    FROM payment_transactions
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND (%(campaign_id)s IS NULL OR campaign_id = %(campaign_id)s)
    GROUP BY product_id, DATE(date_time), agg_name, transaction_status
"""

HOURLY_STATUS_COUNTS_SQL = """
    SELECT product_id, DATE_ADD(DATE(date_time), INTERVAL HOUR(date_time) HOUR) AS hour,
           agg_name AS provider, transaction_status, COUNT(*) AS count
    FROM payment_transactions
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND (%(campaign_id)s IS NULL OR campaign_id = %(campaign_id)s)
    GROUP BY product_id, hour, agg_name, transaction_status
"""

REASON_COUNTS_SQL = """
    SELECT product_id, agg_name AS provider, transaction_status,
        CASE
            WHEN cb_error_message LIKE '%%#%%' THEN SUBSTRING_INDEX(cb_error_message, '#', 1)
            WHEN partner_error_message IS NULL OR partner_error_message LIKE 'Request completed successfully%%'
                THEN COALESCE(transaction_status, 'UNKNOWN')
            ELSE partner_error_message
        END AS reason_code,
        COUNT(*) AS count
    FROM payment_transactions
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND (%(campaign_id)s IS NULL OR campaign_id = %(campaign_id)s)
    GROUP BY product_id, agg_name, transaction_status, reason_code
"""

DAILY_REASON_COUNTS_SQL = """
    SELECT product_id, DATE(date_time) AS day, agg_name AS provider, transaction_status,
        CASE
            WHEN cb_error_message LIKE '%%#%%' THEN SUBSTRING_INDEX(cb_error_message, '#', 1)
            WHEN partner_error_message IS NULL OR partner_error_message LIKE 'Request completed successfully%%'
                THEN COALESCE(transaction_status, 'UNKNOWN')
            ELSE partner_error_message
        END AS reason_code,
        COUNT(*) AS count
    FROM payment_transactions
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND (%(campaign_id)s IS NULL OR campaign_id = %(campaign_id)s)
    GROUP BY product_id, DATE(date_time), agg_name, transaction_status, reason_code
"""

OPERATOR_COUNTS_SQL = """
    SELECT product_id, operator, channel, status, COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND request_type IN ('newpurchase', 'repurchase')
      AND (%(campaign_id)s IS NULL OR campaign_id = %(campaign_id)s)
    GROUP BY product_id, operator, channel, status
"""

DAILY_OPERATOR_COUNTS_SQL = """
    SELECT product_id, DATE(date_time) AS day, operator, channel, status, COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND request_type IN ('newpurchase', 'repurchase')
      AND (%(campaign_id)s IS NULL OR campaign_id = %(campaign_id)s)
    GROUP BY product_id, DATE(date_time), operator, channel, status
"""

# ============================================================
# NEW path - payout_logs-based (pawapay uses this)
# ============================================================
#
# Every query below filters to request_type IN ('newpurchase', 'repurchase') -
# both confirmed as real, legitimate values representing an actual user
# clicking to subscribe (a first-time purchase or someone re-subscribing
# after lapsing) - not something to exclude the way the original
# newpurchase-only filter would have. This still guards against other
# request types payout_logs may log that AREN'T a user-initiated checkout
# (an automatic recurring-billing attempt being the likely candidate,
# though not yet confirmed against real data the way these two were) -
# those shouldn't count toward the new-subscriber conversion funnel this
# whole system measures. Didn't change the VUZ360 numbers already verified
# (that specific sample was 100% newpurchase), but a longer window or a
# different product could easily include both request types, or others.

PAYOUT_STATUS_COUNTS_SQL = """
    SELECT
        pl.product_id, pl.product_name, pl.country AS country_code,
        pl.agg_name AS provider, pl.status,
        cpp.id AS cp_product_id, u.id AS cp_id, u.email AS partner_email, u.company_name AS partner_name,
        COUNT(*) AS count
    FROM payout_logs pl
    JOIN dtpay_cp_products cpp ON cpp.id = pl.cp_product_id
    JOIN dtpay_users u         ON u.id   = cpp.cp_id
    WHERE pl.cp_product_id IN %(cp_product_ids)s
      AND pl.date_time >= %(day_start)s AND pl.date_time < %(day_end)s
      AND pl.request_type IN ('newpurchase', 'repurchase')
      AND (%(campaign_id)s IS NULL OR pl.campaign_id = %(campaign_id)s)
    GROUP BY pl.product_id, pl.product_name, pl.country, pl.agg_name, pl.status,
             cpp.id, u.id, u.email, u.company_name
"""

PAYOUT_DAILY_STATUS_COUNTS_SQL = """
    SELECT product_id, DATE(date_time) AS day, agg_name AS provider, status, COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND request_type IN ('newpurchase', 'repurchase')
      AND (%(campaign_id)s IS NULL OR campaign_id = %(campaign_id)s)
    GROUP BY product_id, DATE(date_time), agg_name, status
"""

PAYOUT_HOURLY_STATUS_COUNTS_SQL = """
    SELECT product_id, DATE_ADD(DATE(date_time), INTERVAL HOUR(date_time) HOUR) AS hour,
           agg_name AS provider, status, COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND request_type IN ('newpurchase', 'repurchase')
      AND (%(campaign_id)s IS NULL OR campaign_id = %(campaign_id)s)
    GROUP BY product_id, hour, agg_name, status
"""

# error_message is already human-readable (no CODE# prefix the way
# cb_error_message had), so this is a direct read, no splitting needed.
PAYOUT_REASON_COUNTS_SQL = """
    SELECT product_id, agg_name AS provider, status,
        CASE WHEN error_message = 'Success' THEN status ELSE error_message END AS reason_code,
        COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND request_type IN ('newpurchase', 'repurchase')
      AND (%(campaign_id)s IS NULL OR campaign_id = %(campaign_id)s)
    GROUP BY product_id, agg_name, status, error_message
"""

PAYOUT_DAILY_REASON_COUNTS_SQL = """
    SELECT product_id, DATE(date_time) AS day, agg_name AS provider, status,
           CASE WHEN error_message = 'Success' THEN status ELSE error_message END AS reason_code,
           COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND request_type IN ('newpurchase', 'repurchase')
      AND (%(campaign_id)s IS NULL OR campaign_id = %(campaign_id)s)
    GROUP BY product_id, DATE(date_time), agg_name, status, error_message
"""


def fetch_all(day_start, day_end, cp_product_ids, min_resolved=None, include_daily=False,
              include_hourly=False, include_daily_reasons=False, campaign_id=None):
    """
    campaign_id (optional) narrows every query - both paths - to that
    one campaign's rows. None means no campaign filter at all, which is
    what the weekly email always passes.

    Runs both the legacy (payment_transactions) and new (payout_logs)
    extraction paths for every given cp_product_id, then for each
    product keeps only the result from whichever path its provider
    actually uses (providers.uses_payout_logs_metrics) - pawapay
    products get their payout_logs-derived numbers, everything else
    (razorpay) keeps the payment_transactions-derived ones, unchanged
    from before this change.
    """
    if min_resolved is None:
        min_resolved = MIN_RESOLVED_THRESHOLD
    if not cp_product_ids:
        return [], [], [], [], [], [], []

    params = {"day_start": day_start, "day_end": day_end, "cp_product_ids": tuple(cp_product_ids),
              "campaign_id": campaign_id}

    with get_connection() as conn:
        with conn.cursor() as cur:
            # legacy path queries
            cur.execute(STATUS_COUNTS_SQL, params)
            status_counts = _clean_rows(cur.fetchall())
            cur.execute(REASON_COUNTS_SQL, params)
            reason_counts = _clean_rows(cur.fetchall())
            cur.execute(OPERATOR_COUNTS_SQL, params)
            operator_counts = _clean_rows(cur.fetchall())

            daily_status_counts, hourly_status_counts, daily_reason_counts = [], [], []
            if include_daily:
                cur.execute(DAILY_STATUS_COUNTS_SQL, params)
                daily_status_counts = _clean_rows(cur.fetchall())
            if include_hourly:
                cur.execute(HOURLY_STATUS_COUNTS_SQL, params)
                hourly_status_counts = _clean_rows(cur.fetchall())
            if include_daily_reasons:
                cur.execute(DAILY_REASON_COUNTS_SQL, params)
                daily_reason_counts = _clean_rows(cur.fetchall())

            # new path queries
            cur.execute(PAYOUT_STATUS_COUNTS_SQL, params)
            payout_status_counts = _clean_rows(cur.fetchall())
            cur.execute(PAYOUT_REASON_COUNTS_SQL, params)
            payout_reason_counts = _clean_rows(cur.fetchall())

            payout_daily_status_counts, payout_hourly_status_counts, payout_daily_reason_counts = [], [], []
            if include_daily:
                cur.execute(PAYOUT_DAILY_STATUS_COUNTS_SQL, params)
                payout_daily_status_counts = _clean_rows(cur.fetchall())
            if include_hourly:
                cur.execute(PAYOUT_HOURLY_STATUS_COUNTS_SQL, params)
                payout_hourly_status_counts = _clean_rows(cur.fetchall())
            if include_daily_reasons:
                cur.execute(PAYOUT_DAILY_REASON_COUNTS_SQL, params)
                payout_daily_reason_counts = _clean_rows(cur.fetchall())

            daily_operator_counts = []
            if include_daily_reasons:
                cur.execute(DAILY_OPERATOR_COUNTS_SQL, params)
                daily_operator_counts = _clean_rows(cur.fetchall())

    legacy_metrics = classify_status_counts(status_counts, min_resolved)
    payout_metrics = classify_payout_status_counts(payout_status_counts, min_resolved)

    # Per product, keep exactly one result - whichever path its
    # provider actually uses.
    payout_by_product = {m["product_id"]: m for m in payout_metrics}
    product_metrics = []
    for m in legacy_metrics:
        if uses_payout_logs_metrics(m["provider"]):
            continue  # this product's real result comes from payout_metrics instead
        product_metrics.append(m)
    for pid, m in payout_by_product.items():
        product_metrics.append(m)

    kept_product_ids = {p["product_id"] for p in product_metrics}
    provider_by_product = {p["product_id"]: p["provider"] for p in product_metrics}

    def _is_payout_product(pid):
        return uses_payout_logs_metrics(provider_by_product.get(pid))

    # reasons: legacy for legacy products, payout-derived for payout products
    legacy_reason_rows = filter_reason_rows(
        [r for r in reason_counts if r["product_id"] in kept_product_ids and not _is_payout_product(r["product_id"])]
    )
    payout_reason_rows_for_output = [
        {"product_id": r["product_id"], "reason_code": r["reason_code"], "occurrences": r["count"]}
        for r in filter_payout_reason_rows(
            [r for r in payout_reason_counts if r["product_id"] in kept_product_ids and _is_payout_product(r["product_id"])]
        )
    ]
    legacy_reason_rows_for_output = [
        {"product_id": r["product_id"], "reason_code": r["reason_code"], "occurrences": r["count"]}
        for r in legacy_reason_rows
    ]
    reason_breakdown = merge_duplicate_reasons(legacy_reason_rows_for_output + payout_reason_rows_for_output)

    # operators: always from payout_logs, but attempts now excludes
    # payout_excluded_statuses for payout-metrics products, to stay
    # consistent with the new headline total_resolved for those.
    operator_rows = [o for o in operator_counts if o["product_id"] in kept_product_ids]
    for row in operator_rows:
        row["provider"] = provider_by_product.get(row["product_id"])
    operator_breakdown = classify_operator_counts(operator_rows)

    daily_metrics = []
    if include_daily:
        legacy_daily = [d for d in daily_status_counts if d["product_id"] in kept_product_ids and not _is_payout_product(d["product_id"])]
        payout_daily = [d for d in payout_daily_status_counts if d["product_id"] in kept_product_ids and _is_payout_product(d["product_id"])]
        daily_metrics = classify_daily_counts(legacy_daily) + classify_payout_daily_counts(payout_daily)

    hourly_metrics = []
    if include_hourly:
        legacy_hourly = [h for h in hourly_status_counts if h["product_id"] in kept_product_ids and not _is_payout_product(h["product_id"])]
        payout_hourly = [h for h in payout_hourly_status_counts if h["product_id"] in kept_product_ids and _is_payout_product(h["product_id"])]
        hourly_metrics = classify_hourly_counts(legacy_hourly) + classify_payout_hourly_counts(payout_hourly)

    daily_reason_breakdown, daily_operator_breakdown = [], []
    if include_daily_reasons:
        legacy_daily_reason_rows = filter_reason_rows(
            [r for r in daily_reason_counts if r["product_id"] in kept_product_ids and not _is_payout_product(r["product_id"])]
        )
        payout_daily_reason_rows = filter_payout_reason_rows(
            [r for r in payout_daily_reason_counts if r["product_id"] in kept_product_ids and _is_payout_product(r["product_id"])]
        )
        combined_daily_reasons = (
            [{"product_id": r["product_id"], "day": r["day"], "reason_code": r["reason_code"], "occurrences": r["count"]}
             for r in legacy_daily_reason_rows]
            + [{"product_id": r["product_id"], "day": r["day"], "reason_code": r["reason_code"], "occurrences": r["count"]}
               for r in payout_daily_reason_rows]
        )
        daily_reason_breakdown = merge_duplicate_reasons(combined_daily_reasons, by_day=True)

        daily_operator_rows = [o for o in daily_operator_counts if o["product_id"] in kept_product_ids]
        for row in daily_operator_rows:
            row["provider"] = provider_by_product.get(row["product_id"])
        daily_operator_breakdown = classify_operator_counts(daily_operator_rows, by_day=True)

    return (product_metrics, reason_breakdown, operator_breakdown, daily_metrics,
            hourly_metrics, daily_reason_breakdown, daily_operator_breakdown)


# ============================================================
# Per-campaign breakdown (on-demand API only, whole-product requests)
# ============================================================
#
# Same filters as the product-level queries above, one more GROUP BY
# level. An empty campaign_id is folded into NULL so "no campaign" is a
# single bucket. campaign_name is MAX()'d only to carry it through the
# GROUP BY - one campaign_id maps to exactly one name.

CAMPAIGN_STATUS_COUNTS_SQL = """
    SELECT product_id, NULLIF(campaign_id, '') AS campaign_id, MAX(campaign_name) AS campaign_name,
           agg_name AS provider, transaction_status, COUNT(*) AS count
    FROM payment_transactions
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
    GROUP BY product_id, NULLIF(campaign_id, ''), agg_name, transaction_status
"""

CAMPAIGN_REASON_COUNTS_SQL = """
    SELECT product_id, NULLIF(campaign_id, '') AS campaign_id, agg_name AS provider, transaction_status,
        CASE
            WHEN cb_error_message LIKE '%%#%%' THEN SUBSTRING_INDEX(cb_error_message, '#', 1)
            WHEN partner_error_message IS NULL OR partner_error_message LIKE 'Request completed successfully%%'
                THEN COALESCE(transaction_status, 'UNKNOWN')
            ELSE partner_error_message
        END AS reason_code,
        COUNT(*) AS count
    FROM payment_transactions
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
    GROUP BY product_id, NULLIF(campaign_id, ''), agg_name, transaction_status, reason_code
"""

PAYOUT_CAMPAIGN_STATUS_COUNTS_SQL = """
    SELECT product_id, NULLIF(campaign_id, '') AS campaign_id, MAX(campaign_name) AS campaign_name,
           agg_name AS provider, status, COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND request_type IN ('newpurchase', 'repurchase')
    GROUP BY product_id, NULLIF(campaign_id, ''), agg_name, status
"""

PAYOUT_CAMPAIGN_REASON_COUNTS_SQL = """
    SELECT product_id, NULLIF(campaign_id, '') AS campaign_id, agg_name AS provider, status,
        CASE WHEN error_message = 'Success' THEN status ELSE error_message END AS reason_code,
        COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND request_type IN ('newpurchase', 'repurchase')
    GROUP BY product_id, NULLIF(campaign_id, ''), agg_name, status, error_message
"""

CAMPAIGN_OPERATOR_COUNTS_SQL = """
    SELECT product_id, NULLIF(campaign_id, '') AS campaign_id, operator, channel, status, COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
      AND request_type IN ('newpurchase', 'repurchase')
    GROUP BY product_id, NULLIF(campaign_id, ''), operator, channel, status
"""

CAMPAIGN_TOP_REASONS = 5


def fetch_campaign_breakdown(day_start, day_end, cp_product_ids, provider_by_product):
    """
    Splits each product's numbers by campaign. provider_by_product
    ({product_id: provider}, from fetch_all's product_metrics) picks the
    path per product exactly the way fetch_all does, and limits the
    breakdown to products fetch_all actually kept.

    Returns {product_id: [campaign, ...]}, sorted by total_resolved
    descending, only for products with at least two campaigns that have
    resolved volume - a single campaign is just the product itself.
    Each campaign: campaign_id (None = no campaign), campaign_name,
    total_resolved, completed, failed, conversion_rate_pct, reasons,
    operators.

    The reason/operator classifiers in providers.py key on product_id,
    so rows are passed through with product_id packed as
    (product_id, campaign_id) and unpacked afterwards - that keeps every
    success/exclusion rule in providers.py rather than re-implementing
    it per campaign.
    """
    if not cp_product_ids or not provider_by_product:
        return {}

    def _is_payout_product(pid):
        return uses_payout_logs_metrics(provider_by_product.get(pid))

    any_legacy = any(not _is_payout_product(pid) for pid in provider_by_product)
    any_payout = any(_is_payout_product(pid) for pid in provider_by_product)
    params = {"day_start": day_start, "day_end": day_end, "cp_product_ids": tuple(cp_product_ids)}

    status_rows, reason_rows, payout_status_rows, payout_reason_rows = [], [], [], []
    with get_connection() as conn:
        with conn.cursor() as cur:
            if any_legacy:
                cur.execute(CAMPAIGN_STATUS_COUNTS_SQL, params)
                status_rows = _clean_rows(cur.fetchall())
                cur.execute(CAMPAIGN_REASON_COUNTS_SQL, params)
                reason_rows = _clean_rows(cur.fetchall())
            if any_payout:
                cur.execute(PAYOUT_CAMPAIGN_STATUS_COUNTS_SQL, params)
                payout_status_rows = _clean_rows(cur.fetchall())
                cur.execute(PAYOUT_CAMPAIGN_REASON_COUNTS_SQL, params)
                payout_reason_rows = _clean_rows(cur.fetchall())
            cur.execute(CAMPAIGN_OPERATOR_COUNTS_SQL, params)
            operator_rows = _clean_rows(cur.fetchall())

    def _keep(rows, payout):
        return [r for r in rows
                if r["product_id"] in provider_by_product and _is_payout_product(r["product_id"]) == payout]

    def _packed(rows):
        return [{**r, "product_id": (r["product_id"], r["campaign_id"])} for r in rows]

    status_rows, reason_rows = _keep(status_rows, False), _keep(reason_rows, False)
    payout_status_rows, payout_reason_rows = _keep(payout_status_rows, True), _keep(payout_reason_rows, True)

    names = {(r["product_id"], r["campaign_id"]): r["campaign_name"]
             for r in status_rows + payout_status_rows if r["campaign_name"]}
    metrics = classify_campaign_counts(status_rows) + classify_payout_campaign_counts(payout_status_rows)

    reasons_by_key = {}
    for r in merge_duplicate_reasons(
        [{"product_id": r["product_id"], "reason_code": r["reason_code"], "occurrences": r["count"]}
         for r in filter_reason_rows(_packed(reason_rows)) + filter_payout_reason_rows(_packed(payout_reason_rows))]
    ):
        reasons_by_key.setdefault(r["product_id"], []).append(
            {"reason_code": r["reason_code"], "occurrences": r["occurrences"]})

    packed_operator_rows = _packed([o for o in operator_rows if o["product_id"] in provider_by_product])
    for row in packed_operator_rows:
        row["provider"] = provider_by_product[row["product_id"][0]]
    operators_by_key = {}
    for o in classify_operator_counts(packed_operator_rows):
        operators_by_key.setdefault(o["product_id"], []).append(
            {"operator": o["operator"], "attempts": o["attempts"], "operator_ok": o["operator_ok"]})

    by_product = {}
    for m in metrics:
        if m["total_resolved"] <= 0:
            continue
        key = (m["product_id"], m["campaign_id"])
        by_product.setdefault(m["product_id"], []).append({
            "campaign_id": m["campaign_id"],
            "campaign_name": names.get(key),
            "total_resolved": m["total_resolved"],
            "completed": m["completed"],
            "failed": m["total_resolved"] - m["completed"],
            "conversion_rate_pct": m["conversion_rate_pct"],
            "reasons": sorted(reasons_by_key.get(key, []), key=lambda r: -r["occurrences"])[:CAMPAIGN_TOP_REASONS],
            "operators": sorted(operators_by_key.get(key, []), key=lambda o: -o["attempts"]),
        })

    return {
        pid: sorted(campaigns, key=lambda c: -c["total_resolved"])
        for pid, campaigns in by_product.items() if len(campaigns) >= 2
    }