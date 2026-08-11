"""
Per-wallet-provider rules for what counts as a resolved attempt, what
counts as success, and which payout_logs field is the meaningful
network/channel comparison axis.

PAWAPAY NOW COMPUTES ITS HEADLINE METRICS FROM payout_logs, NOT
payment_transactions. This is a real architectural change, not a
tweak - confirmed by reverse-engineering the existing dashboard's own
numbers exactly against real data: payment_transactions represents
unique users who clicked to subscribe and opened checkout (the
top-of-funnel count); payout_logs represents each individual
operator-API attempt, which can be triggered multiple times for one
user if they retry. Conversion - whether a payment actually
succeeded - is a payout_logs concept, and the dashboard has always
measured it there. payment_transactions.transaction_status can lag
or never get updated in some cases; payout_logs.status is the more
direct, real source of truth for what happened at the operator.

For pawapay, this means: "payout_excluded_statuses" (FREQUENT_REQUESTS,
DAY_LIMIT_EXCEED - requests that never really reached the operator,
confirmed against real data) are dropped from the denominator entirely,
"payout_success_statuses" (COMPLETED) is the numerator, and everything
else (FAILED, CORRESPONDENT_TEMPORARILY_UNAVAILABLE, ...) is a resolved
non-success outcome with its own reason, taken directly from
payout_logs.error_message - already human-readable, no more #-splitting
needed the way cb_error_message required.

The old "success_statuses"/"excluded_statuses" (transaction_status-based)
remain here and are what razorpay still uses - that provider hasn't
been reconsidered yet (explicitly deferred). "uses_payout_logs_metrics"
is what api.py/extract.py check to decide which path a given product's
provider takes; only pawapay has it set to True so far.
"""
from collections import defaultdict

PROVIDER_RULES = {
    "pawapay": {
        # Legacy, payment_transactions-based rules - kept for anything
        # not yet migrated to reading this provider via payout_logs
        # (there's no such consumer left for pawapay specifically, but
        # keeping them documents what the old behavior was and avoids
        # breaking anything that still imports these keys).
        "success_statuses": {"COMPLETED"},
        "excluded_statuses": {"QUEUED", "ALREADY_SUB"},
        "comparison_field": "operator",

        # Live rules: pawapay's headline metrics, reasons, and daily/
        # hourly breakdowns are now computed entirely from payout_logs.
        "uses_payout_logs_metrics": True,
        "payout_success_statuses": {"COMPLETED"},
        "payout_excluded_statuses": {"FREQUENT_REQUESTS", "DAY_LIMIT_EXCEED"},
    },
    "razorpay": {
        "success_statuses": {"AUTHORIZED"},
        "excluded_statuses": {"QUEUED"},
        "payout_success_statuses": {"AUTHORIZED", "COMPLETED"},
        "comparison_field": "channel",
        # Deliberately NOT set - razorpay stays on the
        # payment_transactions-based path until we look at its
        # payout_logs data the same way pawapay's was just examined.
        "uses_payout_logs_metrics": False,
    },
}


def uses_payout_logs_metrics(provider):
    rules = PROVIDER_RULES.get(provider)
    return bool(rules and rules.get("uses_payout_logs_metrics"))


def classify_status_counts(rows, min_resolved):
    """
    Legacy path: payment_transactions-based classification. Still
    used for any provider where uses_payout_logs_metrics is False
    (currently razorpay). Takes raw (product_id, provider,
    transaction_status, count, ...) rows - one row per distinct
    status seen for a product - and applies that product's
    provider's rules to produce one summary row per product:
    total_resolved, completed, failed, conversion_rate_pct.
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


def classify_payout_status_counts(rows, min_resolved):
    """
    New path: payout_logs-based classification, for any provider with
    uses_payout_logs_metrics=True (pawapay). Same shape and same
    calling convention as classify_status_counts, but reads "status"
    (payout_logs' field) instead of "transaction_status", and uses
    payout_success_statuses/payout_excluded_statuses instead of the
    payment_transactions-based rule set.
    """
    by_product = defaultdict(list)
    for row in rows:
        by_product[row["product_id"]].append(row)

    results = []
    for product_id, product_rows in by_product.items():
        provider = product_rows[0]["provider"]
        rules = PROVIDER_RULES.get(provider)
        if rules is None or not rules.get("uses_payout_logs_metrics"):
            continue

        total_resolved = 0
        completed = 0
        for row in product_rows:
            status = row["status"]
            if status in rules["payout_excluded_statuses"]:
                continue
            total_resolved += row["count"]
            if status in rules["payout_success_statuses"]:
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


def _classify_by_period(rows, period_field, status_field="transaction_status",
                         success_key="success_statuses", excluded_key="excluded_statuses"):
    """
    Shared logic behind the daily/hourly classifiers, for both the
    legacy (payment_transactions) and new (payout_logs) paths - which
    table's vocabulary applies is selected via status_field/
    success_key/excluded_key, so this one function serves both.
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
            status = row[status_field]
            if status in rules[excluded_key]:
                continue
            total_resolved += row["count"]
            if status in rules[success_key]:
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
    """Legacy, payment_transactions-based - per (product_id, day)."""
    return _classify_by_period(rows, "day")


def classify_hourly_counts(rows):
    """Legacy, payment_transactions-based - per (product_id, hour)."""
    return _classify_by_period(rows, "hour")


def classify_payout_daily_counts(rows):
    """New, payout_logs-based - per (product_id, day)."""
    return _classify_by_period(rows, "day", status_field="status",
                                success_key="payout_success_statuses", excluded_key="payout_excluded_statuses")


def classify_payout_hourly_counts(rows):
    """New, payout_logs-based - per (product_id, hour)."""
    return _classify_by_period(rows, "hour", status_field="status",
                                success_key="payout_success_statuses", excluded_key="payout_excluded_statuses")


def filter_reason_rows(rows):
    """
    Legacy path: drops payment_transactions rows whose status is
    either excluded or a success status for that row's provider,
    leaving only genuine non-success reasons.
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


def filter_payout_reason_rows(rows):
    """
    New path: same idea as filter_reason_rows, but for payout_logs
    rows - drops anything excluded or successful, using the
    payout_* rule set instead of the transaction_status one.
    """
    kept = []
    for row in rows:
        rules = PROVIDER_RULES.get(row["provider"])
        if rules is None:
            continue
        status = row["status"]
        if status in rules["payout_excluded_statuses"] or status in rules["payout_success_statuses"]:
            continue
        kept.append(row)
    return kept


def merge_duplicate_reasons(rows, by_day=False):
    """
    Merges rows that share the same reason_code (and, if by_day, the
    same day) into one entry per product, summing occurrences. Used
    by both paths - the payment_transactions path can produce the
    same reason_code from two different transaction_status values
    (confirmed against real production data), and duplicate
    error_message text from payout_logs should merge the same way.
    """
    merged = {}
    for row in rows:
        key = (row["product_id"], row["day"], row["reason_code"]) if by_day else (row["product_id"], row["reason_code"])
        if key not in merged:
            entry = {"product_id": row["product_id"], "reason_code": row["reason_code"],
                      "occurrences": row["occurrences"]}
            if by_day:
                entry["day"] = row["day"]
            merged[key] = entry
        else:
            merged[key]["occurrences"] += row["occurrences"]
    return list(merged.values())


def classify_operator_counts(rows, by_day=False):
    """
    Takes raw (product_id, provider, operator, channel, status, count)
    rows from payout_logs and picks the right comparison field per
    provider (operator for pawapay, channel for razorpay), using
    payout_success_statuses to judge whether each row was "ok".

    For pawapay specifically (uses_payout_logs_metrics=True), rows
    whose status is in payout_excluded_statuses are dropped entirely,
    not just excluded from the "ok" count - consistent with those
    statuses being excluded from the headline denominator too, so
    the operator breakdown's "attempts" total lines up with the
    product-level total_resolved instead of over-counting requests
    that never really reached the operator.
    """
    by_key = defaultdict(lambda: {"attempts": 0, "operator_ok": 0})
    for row in rows:
        rules = PROVIDER_RULES.get(row["provider"])
        if rules is None:
            continue
        if rules.get("uses_payout_logs_metrics") and row["status"] in rules.get("payout_excluded_statuses", set()):
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