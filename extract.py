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
    GROUP BY t.product_id, t.product_name, t.country_name, t.agg_name,
             t.transaction_status, cpp.id, u.id, u.email, u.company_name
"""

DAILY_STATUS_COUNTS_SQL = """
    SELECT product_id, DATE(date_time) AS day, agg_name AS provider, transaction_status, COUNT(*) AS count
    FROM payment_transactions
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
    GROUP BY product_id, DATE(date_time), agg_name, transaction_status
"""

HOURLY_STATUS_COUNTS_SQL = """
    SELECT product_id, DATE_ADD(DATE(date_time), INTERVAL HOUR(date_time) HOUR) AS hour,
           agg_name AS provider, transaction_status, COUNT(*) AS count
    FROM payment_transactions
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
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
    GROUP BY product_id, DATE(date_time), agg_name, transaction_status, reason_code
"""

OPERATOR_COUNTS_SQL = """
    SELECT product_id, operator, channel, status, COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
    GROUP BY product_id, operator, channel, status
"""

DAILY_OPERATOR_COUNTS_SQL = """
    SELECT product_id, DATE(date_time) AS day, operator, channel, status, COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
    GROUP BY product_id, DATE(date_time), operator, channel, status
"""

# ============================================================
# NEW path - payout_logs-based (pawapay uses this)
# ============================================================

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
    GROUP BY pl.product_id, pl.product_name, pl.country, pl.agg_name, pl.status,
             cpp.id, u.id, u.email, u.company_name
"""

PAYOUT_DAILY_STATUS_COUNTS_SQL = """
    SELECT product_id, DATE(date_time) AS day, agg_name AS provider, status, COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
    GROUP BY product_id, DATE(date_time), agg_name, status
"""

PAYOUT_HOURLY_STATUS_COUNTS_SQL = """
    SELECT product_id, DATE_ADD(DATE(date_time), INTERVAL HOUR(date_time) HOUR) AS hour,
           agg_name AS provider, status, COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
    GROUP BY product_id, hour, agg_name, status
"""

# error_message is already human-readable (no CODE# prefix the way
# cb_error_message had), so this is a direct read, no splitting needed.
PAYOUT_REASON_COUNTS_SQL = """
    SELECT product_id, agg_name AS provider, status, error_message AS reason_code, COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
    GROUP BY product_id, agg_name, status, error_message
"""

PAYOUT_DAILY_REASON_COUNTS_SQL = """
    SELECT product_id, DATE(date_time) AS day, agg_name AS provider, status,
           error_message AS reason_code, COUNT(*) AS count
    FROM payout_logs
    WHERE cp_product_id IN %(cp_product_ids)s AND date_time >= %(day_start)s AND date_time < %(day_end)s
    GROUP BY product_id, DATE(date_time), agg_name, status, error_message
"""


def fetch_all(day_start, day_end, cp_product_ids, min_resolved=None, include_daily=False,
              include_hourly=False, include_daily_reasons=False):
    """
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

    params = {"day_start": day_start, "day_end": day_end, "cp_product_ids": tuple(cp_product_ids)}

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