"""
Per-wallet-provider rules for what counts as a resolved attempt, what
counts as success, and which payout_logs field is the meaningful
network/channel comparison axis. Confirmed against real data for
pawapay and razorpay. ampere and thirdpay aren't live yet, so adding
either later is a new PROVIDER_RULES entry once their real status
vocabulary is confirmed the same way - not new SQL, and not a guess.

Two separate success-status sets exist per provider because
payment_transactions and payout_logs don't share one vocabulary:
pawapay happens to use "COMPLETED" in both, but razorpay's success
state is "AUTHORIZED" in payment_transactions while payout_logs
additionally uses a plain "COMPLETED" for some of the same outcomes
(confirmed: AUTHORIZED + COMPLETED in payout_logs == AUTHORIZED count
in payment_transactions, exactly, on real data).
"""
from collections import defaultdict

PROVIDER_RULES = {
    "pawapay": {
        # Confirmed directly: COMPLETED = confirmed success.
        "success_statuses": {"COMPLETED"},
        # QUEUED = payment started, no callback yet (still pending).
        # ALREADY_SUB = customer already has an active subscription -
        # not a real new-conversion opportunity, confirmed it should
        # not count toward the denominator at all.
        "excluded_statuses": {"QUEUED", "ALREADY_SUB"},
        "payout_success_statuses": {"COMPLETED"},
        # payout_logs.operator names the actual mobile network
        # (MTN_MOMO_UGA, AIRTEL_OAPI_UGA, ...).
        "comparison_field": "operator",
    },
    "razorpay": {
        # Confirmed directly: AUTHORIZED = success, NOT "COMPLETED"
        # (that's pawapay's marker, not razorpay's).
        "success_statuses": {"AUTHORIZED"},
        # QUEUED = subscriptionId created / page loaded, mostly before
        # the customer ever clicks to open the SDK (confirmed) - not
        # "payment submitted, awaiting result" the way pawapay's
        # QUEUED is. Same exclusion treatment for now regardless;
        # revisit once DTPay's flow change ties creation to the click
        # instead of page load, since QUEUED's meaning shifts then.
        "excluded_statuses": {"QUEUED"},
        "payout_success_statuses": {"AUTHORIZED", "COMPLETED"},
        # payout_logs.operator is always just 'razorpay' here -
        # uninformative. channel (upi/card/deposite) is the
        # meaningful comparison axis instead.
        "comparison_field": "channel",
    },
}


def classify_status_counts(rows, min_resolved):
    """
    Takes raw (product_id, provider, transaction_status, count, ...)
    rows - one row per distinct status seen for a product - and
    applies that product's provider's rules to produce one summary
    row per product: total_resolved, completed, failed,
    conversion_rate_pct. Products whose provider isn't in
    PROVIDER_RULES (ampere/thirdpay, not live yet) or whose
    total_resolved falls under min_resolved are dropped.
    """
    by_product = defaultdict(list)
    for row in rows:
        by_product[row["product_id"]].append(row)

    results = []
    for product_id, product_rows in by_product.items():
        provider = product_rows[0]["provider"]
        rules = PROVIDER_RULES.get(provider)
        if rules is None:
            continue

        total_resolved = 0
        completed = 0
        for row in product_rows:
            status = row["transaction_status"]
            if status in rules["excluded_statuses"]:
                continue
            total_resolved += row["count"]
            if status in rules["success_statuses"]:
                completed += row["count"]

        if total_resolved < min_resolved:
            continue

        first = product_rows[0]
        results.append({
            "product_id": product_id,
            "product_name": first["product_name"],
            "country_code": first["country_code"],
            "cp_product_id": first["cp_product_id"],
            "cp_id": first["cp_id"],
            "partner_email": first["partner_email"],
            "partner_name": first["partner_name"],
            "provider": provider,
            "total_resolved": total_resolved,
            "completed": completed,
            "failed": total_resolved - completed,
            "conversion_rate_pct": round(completed / total_resolved * 100, 2) if total_resolved else 0.0,
        })
    return results


def _classify_by_period(rows, period_field):
    """
    Shared logic behind classify_daily_counts/classify_hourly_counts:
    groups by (product_id, that period value) and applies the
    product's provider rules. No min_resolved filter - that's a
    whole-range concept; every period is included regardless of its
    own volume, and it's on the caller to show volume alongside the
    rate so a thin period doesn't get over-read.
    """
    by_key = defaultdict(list)
    for row in rows:
        by_key[(row["product_id"], row[period_field])].append(row)

    results = []
    for (product_id, period_value), period_rows in by_key.items():
        provider = period_rows[0]["provider"]
        rules = PROVIDER_RULES.get(provider)
        if rules is None:
            continue

        total_resolved = 0
        completed = 0
        for row in period_rows:
            status = row["transaction_status"]
            if status in rules["excluded_statuses"]:
                continue
            total_resolved += row["count"]
            if status in rules["success_statuses"]:
                completed += row["count"]

        results.append({
            "product_id": product_id,
            period_field: period_value,
            "total_resolved": total_resolved,
            "completed": completed,
            "conversion_rate_pct": round(completed / total_resolved * 100, 2) if total_resolved else 0.0,
        })
    return results


def classify_daily_counts(rows):
    """Per (product_id, day) - feeds the weekly email's trend chart."""
    return _classify_by_period(rows, "day")


def classify_hourly_counts(rows):
    """Per (product_id, hour) - feeds the on-demand API's hour-level
    breakdown, only computed for short enough ranges (see api.py)."""
    return _classify_by_period(rows, "hour")


def filter_reason_rows(rows):
    """
    Drops rows whose status is either excluded (pending, like QUEUED)
    or a success status for that row's provider, leaving only genuine
    non-success reasons - provider-aware version of the old hardcoded
    WHERE transaction_status NOT IN ('QUEUED', 'COMPLETED').
    """
    kept = []
    for row in rows:
        rules = PROVIDER_RULES.get(row["provider"])
        if rules is None:
            continue
        status = row["transaction_status"]
        if status in rules["excluded_statuses"] or status in rules["success_statuses"]:
            continue
        kept.append(row)
    return kept


def classify_operator_counts(rows, by_day=False):
    """
    Takes raw (product_id, provider, operator, channel, status, count)
    rows from payout_logs and picks the right comparison field per
    provider (operator for pawapay, channel for razorpay), using
    payout_success_statuses to judge whether each row was "ok" - a
    separate vocabulary from payment_transactions' success_statuses,
    since payout_logs doesn't always use the same status strings.

    by_day=True additionally groups by each row's "day" field and
    includes it in the result - used for the on-demand API's
    day-level breakdown; the aggregate (by_day=False) case doesn't
    need or expect a "day" key on the input rows at all.
    """
    by_key = defaultdict(lambda: {"attempts": 0, "operator_ok": 0})
    for row in rows:
        rules = PROVIDER_RULES.get(row["provider"])
        if rules is None:
            continue
        field_value = row.get(rules["comparison_field"])
        if field_value is None:
            continue
        key = (row["product_id"], row["day"], field_value) if by_day else (row["product_id"], field_value)
        by_key[key]["attempts"] += row["count"]
        if row["status"] in rules["payout_success_statuses"]:
            by_key[key]["operator_ok"] += row["count"]

    results = []
    for key, counts in by_key.items():
        entry = {"attempts": counts["attempts"], "operator_ok": counts["operator_ok"]}
        if by_day:
            entry["product_id"], entry["day"], entry["operator"] = key
        else:
            entry["product_id"], entry["operator"] = key
        results.append(entry)
    return results