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
from datetime import timedelta

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


def rollup_by_partner(product_metrics, reason_breakdown, operator_breakdown, daily_metrics=None, week_start=None):
    reasons_by_product = defaultdict(list)
    for r in reason_breakdown:
        reasons_by_product[r["product_id"]].append(r)

    operators_by_product = defaultdict(list)
    for o in operator_breakdown:
        operators_by_product[o["product_id"]].append(o)

    daily_by_product = defaultdict(dict)
    for d in daily_metrics or []:
        daily_by_product[d["product_id"]][d["day"]] = d

    # The full expected list of dates in the week, so a day with zero
    # resolved transactions still shows up as a real zero rather than
    # a gap - only possible to build this if week_start was given.
    week_dates = [week_start + timedelta(days=i) for i in range(7)] if week_start else None

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
        if week_dates:
            product_daily = daily_by_product.get(row["product_id"], {})
            for d in week_dates:
                entry = product_daily.get(d)
                daily_series.append({
                    "date": d,
                    "total_resolved": entry["total_resolved"] if entry else 0,
                    "completed": entry["completed"] if entry else 0,
                    "conversion_rate_pct": entry["conversion_rate_pct"] if entry else 0.0,
                })

        partners[cp_id]["services"].append({
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "country_code": row["country_code"],
            "country": resolve_country(row["country_code"]),
            "total_resolved": row["total_resolved"],
            "completed": row["completed"],
            "failed": row["failed"],
            "conversion_rate_pct": row["conversion_rate_pct"],
            "reasons": reasons_by_product.get(row["product_id"], []),
            "operators": operators_by_product.get(row["product_id"], []),
            "daily": daily_series,
        })

    return list(partners.values())


def merge_with_previous(current_digests, previous_digests):
    """
    Adds the previous period's figures and a conversion-rate delta to
    each service in current_digests, matched by (cp_id, product_id).
    Mutates and returns current_digests.

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