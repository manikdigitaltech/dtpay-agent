"""
Groups the flat, per-product rows from extract.py into one digest per
partner (cp_id) - see the cp_services / dtpay_cp_products findings:
a single partner (e.g. cp_id 122, "Digital Technology") can run 20+
service/country rows, and they should get one email with a section
per service, not one email per row.

Also resolves the abbreviated country_code (what payment_transactions
actually stores, despite the column being named country_name) into a
proper full name for anything downstream that displays it.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta

# Known country_code values seen in payment_transactions so far.
# Falls back to showing the raw code for anything not listed here
# rather than guessing or showing nothing.
COUNTRY_NAMES = {
    "BJ": "Benin",
    "CI": "Côte d'Ivoire",
    "CM": "Cameroon",
    "DRC": "Democratic Republic of the Congo",
    "GA": "Gabon",
    "IN": "India",
    "KE": "Kenya",
    "PK": "Pakistan",
    "SN": "Senegal",
    "TZ": "Tanzania",
    "UGA": "Uganda",
    "ZM": "Zambia",
}


def resolve_country(country_code):
    return COUNTRY_NAMES.get(country_code, country_code)


def rollup_by_partner(product_metrics, reason_breakdown, operator_breakdown,
                       daily_metrics=None, range_start=None, range_end=None,
                       hourly_metrics=None, daily_reason_breakdown=None,
                       daily_operator_breakdown=None):
    """
    range_start/range_end (dates, range_end exclusive) fill gaps in
    daily_metrics with real zeros for the whole requested range,
    however many days that is - not hardcoded to 7, since the
    on-demand API can be asked for any range, not just a week.
    hourly_metrics gets the same gap-filling treatment, at hour
    granularity, when given.

    daily_reason_breakdown/daily_operator_breakdown (both optional)
    get attached directly to each day's entry in the resulting
    "daily" series - not kept as a separate top-level structure -
    since day-level numbers and that day's reasons/operators being
    co-located is what makes "what happened on the 25th" answerable
    from a single place in the data rather than needing to
    cross-reference two separate arrays by date.
    """
    reasons_by_product = defaultdict(list)
    for r in reason_breakdown:
        reasons_by_product[r["product_id"]].append(r)

    operators_by_product = defaultdict(list)
    for o in operator_breakdown:
        operators_by_product[o["product_id"]].append(o)

    daily_by_product = defaultdict(dict)
    for d in daily_metrics or []:
        daily_by_product[d["product_id"]][d["day"]] = d

    daily_reasons_by_product_day = defaultdict(list)
    for r in daily_reason_breakdown or []:
        daily_reasons_by_product_day[(r["product_id"], r["day"])].append(r)

    daily_operators_by_product_day = defaultdict(list)
    for o in daily_operator_breakdown or []:
        daily_operators_by_product_day[(o["product_id"], o["day"])].append(o)

    range_dates = None
    if range_start and range_end:
        num_days = (range_end - range_start).days
        range_dates = [range_start + timedelta(days=i) for i in range(num_days)]

    hourly_by_product = defaultdict(dict)
    for h in hourly_metrics or []:
        hourly_by_product[h["product_id"]][h["hour"]] = h

    range_hours = None
    if range_start and range_end:
        start_dt = datetime.combine(range_start, time.min)
        end_dt = datetime.combine(range_end, time.min)
        num_hours = int((end_dt - start_dt).total_seconds() // 3600)
        range_hours = [start_dt + timedelta(hours=i) for i in range(num_hours)]

    partners = {}
    for row in product_metrics:
        cp_id = row["cp_id"]
        if cp_id not in partners:
            partners[cp_id] = {
                "cp_id": cp_id,
                "partner_name": row["partner_name"],
                "partner_email": row["partner_email"],
                "services": [],
            }

        daily_series = []
        if range_dates is not None:
            product_daily = daily_by_product.get(row["product_id"], {})
            for d in range_dates:
                entry = product_daily.get(d)
                daily_series.append({
                    "date": d,
                    "total_resolved": entry["total_resolved"] if entry else 0,
                    "completed": entry["completed"] if entry else 0,
                    "conversion_rate_pct": entry["conversion_rate_pct"] if entry else 0.0,
                    "reasons": daily_reasons_by_product_day.get((row["product_id"], d), []),
                    "operators": daily_operators_by_product_day.get((row["product_id"], d), []),
                })

        hourly_series = []
        if range_hours is not None and hourly_metrics is not None:
            product_hourly = hourly_by_product.get(row["product_id"], {})
            for h in range_hours:
                entry = product_hourly.get(h)
                hourly_series.append({
                    "hour": h,
                    "total_resolved": entry["total_resolved"] if entry else 0,
                    "completed": entry["completed"] if entry else 0,
                    "conversion_rate_pct": entry["conversion_rate_pct"] if entry else 0.0,
                })

        partners[cp_id]["services"].append({
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "country_code": row["country_code"],
            "country": resolve_country(row["country_code"]),
            "provider": row["provider"],
            "total_resolved": row["total_resolved"],
            "completed": row["completed"],
            "failed": row["failed"],
            "conversion_rate_pct": row["conversion_rate_pct"],
            "reasons": reasons_by_product.get(row["product_id"], []),
            "operators": operators_by_product.get(row["product_id"], []),
            "daily": daily_series,
            "hourly": hourly_series,
        })

    return list(partners.values())


def merge_with_previous(current_digests, previous_digests):
    """
    Adds the previous period's figures and a conversion-rate delta to
    each service in current_digests, matched by (cp_id, product_id),
    and marks every service as compare_to_previous=True so analyze.py
    knows a period-over-period comparison is actually a meaningful
    concept here (the weekly email always calls this; the on-demand
    API never does, so its services stay compare_to_previous=False
    and Claude is never even shown the concept, let alone told to
    comment on it being absent). Mutates and returns current_digests.

    Known gap: a service with previous-period activity but none this
    period won't appear at all (current_digests only has services
    that show up in the current window), so a service going fully
    quiet is silently dropped rather than flagged - fine for now,
    worth revisiting if that turns out to happen in practice.
    """
    previous_by_key = {}
    for digest in previous_digests:
        for service in digest["services"]:
            previous_by_key[(digest["cp_id"], service["product_id"])] = service

    for digest in current_digests:
        for service in digest["services"]:
            service["compare_to_previous"] = True
            prev = previous_by_key.get((digest["cp_id"], service["product_id"]))
            if prev:
                service["previous_total_resolved"] = prev["total_resolved"]
                service["previous_completed"] = prev["completed"]
                service["previous_failed"] = prev["failed"]
                service["previous_conversion_rate_pct"] = prev["conversion_rate_pct"]
                service["conversion_rate_delta"] = round(
                    service["conversion_rate_pct"] - prev["conversion_rate_pct"], 2
                )
                service["previous_reasons"] = prev["reasons"]
                service["previous_operators"] = prev["operators"]
            else:
                service["previous_total_resolved"] = None
                service["previous_completed"] = None
                service["previous_failed"] = None
                service["previous_conversion_rate_pct"] = None
                service["conversion_rate_delta"] = None
                service["previous_reasons"] = []
                service["previous_operators"] = []

    return current_digests