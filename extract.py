"""
Extraction layer for the weekly partner performance agent.

These three queries are the same ones verified against real sample
data in dtpay_daily_metrics_draft.sql, just parameterized properly
here instead of using literal dates. See that file's comments for
why each assumption (QUEUED exclusion, reason-code source per status,
case-insensitive product status, enabled partner accounts) is there.
"""
import pymysql
import pymysql.cursors
from decimal import Decimal

from config import DB_CONFIG, MIN_RESOLVED_THRESHOLD


def get_connection():
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **DB_CONFIG)


# pymysql returns SUM()/ROUND() results as decimal.Decimal, not int or
# float - confirmed directly against a real connection (completed,
# failed, conversion_rate_pct, operator_ok all came back as Decimal;
# COUNT() results like total_resolved/attempts/occurrences did not).
# json.dumps() in analyze.py can't serialize Decimal at all, so this
# cleans every row right after fetching, before anything downstream
# ever sees one. Rate-like fields become float; anything else that's
# a Decimal is a count and becomes int.
_FLOAT_FIELDS = {"conversion_rate_pct"}


def _clean_rows(rows):
    for row in rows:
        for key, value in row.items():
            if isinstance(value, Decimal):
                row[key] = float(value) if key in _FLOAT_FIELDS else int(value)
    return rows


PRODUCT_METRICS_SQL = """
    WITH terminal_txns AS (
        SELECT *
        FROM payment_transactions
        WHERE transaction_status != 'QUEUED'
          AND date_time >= %(day_start)s
          AND date_time <  %(day_end)s
    )
    SELECT
        t.product_id,
        t.product_name,
        t.country_name                                                     AS country_code,
        cpp.id                                                             AS cp_product_id,
        u.id                                                                AS cp_id,
        u.email                                                             AS partner_email,
        u.company_name                                                      AS partner_name,
        COUNT(*)                                                            AS total_resolved,
        SUM(t.transaction_status = 'COMPLETED')                             AS completed,
        SUM(t.transaction_status = 'FAILED')                                AS failed,
        ROUND(SUM(t.transaction_status = 'COMPLETED') / COUNT(*) * 100, 2)  AS conversion_rate_pct
    FROM terminal_txns t
    JOIN dtpay_cp_products cpp ON cpp.id = t.cp_product_id
    JOIN dtpay_users u         ON u.id   = cpp.cp_id
    WHERE UPPER(cpp.status) = 'APPROVED'
      AND u.enabled = 1
    GROUP BY t.product_id, t.product_name, t.country_name, cpp.id, u.id, u.email, u.company_name
    HAVING total_resolved >= %(min_resolved)s
    ORDER BY u.id, t.product_id;
"""

REASON_BREAKDOWN_SQL = """
    SELECT
        product_id,
        transaction_status,
        CASE
            WHEN transaction_status = 'FAILED'
                THEN SUBSTRING_INDEX(cb_error_message, '#', 1)
            -- partner_error_message is sometimes just the generic
            -- request-accepted placeholder ('Request completed
            -- successfully...') even on a transaction that didn't
            -- succeed overall - fall back to transaction_status
            -- itself rather than surface that as if it were a reason.
            WHEN partner_error_message IS NULL
                 OR partner_error_message LIKE 'Request completed successfully%%'
                THEN transaction_status
            ELSE partner_error_message
        END AS reason_code,
        COUNT(*) AS occurrences
    FROM payment_transactions
    WHERE transaction_status NOT IN ('QUEUED', 'COMPLETED')
      AND date_time >= %(day_start)s
      AND date_time <  %(day_end)s
    GROUP BY product_id, transaction_status, reason_code
    ORDER BY product_id, occurrences DESC;
"""

OPERATOR_BREAKDOWN_SQL = """
    SELECT
        product_id,
        operator,
        COUNT(*)                                                          AS attempts,
        SUM(status IN ('ACCEPTED', 'COMPLETED', 'Subscription_Created'))  AS operator_ok
    FROM payout_logs
    WHERE date_time >= %(day_start)s
      AND date_time <  %(day_end)s
      AND operator IS NOT NULL
    GROUP BY product_id, operator
    ORDER BY product_id, attempts DESC;
"""

DAILY_METRICS_SQL = """
    SELECT
        product_id,
        DATE(date_time)                                                     AS day,
        COUNT(*)                                                            AS total_resolved,
        SUM(transaction_status = 'COMPLETED')                               AS completed,
        ROUND(SUM(transaction_status = 'COMPLETED') / COUNT(*) * 100, 2)    AS conversion_rate_pct
    FROM payment_transactions
    WHERE transaction_status != 'QUEUED'
      AND date_time >= %(day_start)s
      AND date_time <  %(day_end)s
    GROUP BY product_id, DATE(date_time)
    ORDER BY product_id, day;
"""


def fetch_all(day_start, day_end, min_resolved=None, include_daily=False):
    """
    Runs the extraction queries for the [day_start, day_end) window.
    min_resolved defaults to config's MIN_RESOLVED_THRESHOLD - since
    main.py calls this once for the current period and once for the
    previous one, the same floor applies to both automatically, so a
    service that's too thin in either period just comes back with no
    comparison rather than a misleading one.

    include_daily also runs DAILY_METRICS_SQL and returns it as a 4th
    value - only needed for the current week (the day-wise chart), not
    the previous week, which is why it defaults to off.
    """
    if min_resolved is None:
        min_resolved = MIN_RESOLVED_THRESHOLD
    params = {"day_start": day_start, "day_end": day_end, "min_resolved": min_resolved}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(PRODUCT_METRICS_SQL, params)
            product_metrics = _clean_rows(cur.fetchall())

            cur.execute(REASON_BREAKDOWN_SQL, params)
            reason_breakdown = _clean_rows(cur.fetchall())

            cur.execute(OPERATOR_BREAKDOWN_SQL, params)
            operator_breakdown = _clean_rows(cur.fetchall())

            daily_metrics = []
            if include_daily:
                cur.execute(DAILY_METRICS_SQL, params)
                daily_metrics = _clean_rows(cur.fetchall())

    return product_metrics, reason_breakdown, operator_breakdown, daily_metrics