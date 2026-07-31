"""
Calls Claude to turn one partner's rolled-up metrics into a written
summary and recommendations per service.

The numbers themselves (conversion rate, counts, reasons, operators)
come entirely from extract.py / rollup.py / providers.py - Claude
only writes the narrative on top of them, and is explicitly told not
to restate figures, so nothing generated here can put a wrong number
in front of a partner. DTPay routes products through several wallet
providers (pawapay, razorpay confirmed so far) with different success
markers and different meanings for "operator" - Claude is given the
provider name explicitly so it doesn't call a UPI/card channel a
"network" the way pawapay's actual mobile networks are.

Two callers share this module with two different concepts of time:
the weekly email always compares against the previous week
(merge_with_previous sets compare_to_previous=True on every service),
while the on-demand API summarizes whatever arbitrary date range it's
asked for, with no previous-period concept at all - an early version
of this endpoint reused the weekly prompt unmodified, and Claude
correctly-but-uselessly wrote "there's no prior week to compare
against" for a request that was never about weeks in the first place.
_build_system_prompt(compare_to_previous) and _service_payload() below
both branch on that flag: when it's false, the previous_* fields are
omitted from the payload entirely (not just left null), and the
prompt never mentions period-over-period comparison at all, so
there's nothing for Claude to comment on the absence of.

output_config / JSONOutputFormatParam usage confirmed directly against
the installed anthropic SDK (0.120.2) and a live (auth-only-failing)
request to api.anthropic.com. Some Anthropic documentation still shows
structured outputs requiring the anthropic-beta: structured-outputs-2025-11-13
header even though other pages describe it as generally available -
included below as a low-risk hedge (harmless if not actually required)
since a first real run produced an empty summary/recommendations for
every service in a digest, which is consistent with the response not
having been valid JSON. Failures now log to agent.log with the raw
response text, so if this happens again there's an actual answer
instead of a guess.
"""
import json
import logging

import anthropic

from config import ANTHROPIC_API_KEY

MODEL = "claude-sonnet-5"
STRUCTURED_OUTPUT_BETA = "structured-outputs-2025-11-13"

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.FileHandler("agent.log")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def _build_system_prompt(compare_to_previous):
    comparison_summary_note = (
        " When previous-period data is present, make the comparison the point of the summary "
        "(what changed, and - if the reason/operator breakdown suggests why - what likely drove "
        "it), not just the standalone number. When previous_conversion_rate_pct is null, there's "
        "nothing to compare against (new this period, or no resolved volume before) - say so "
        "plainly rather than implying a trend."
    ) if compare_to_previous else ""

    temporal_emphasis_note = (
        " Since there's no previous period to compare against here, the daily breakdown (and the "
        "hourly one, when given) should be a real part of the summary, not just a source for a "
        "rare standout - describe the actual shape of the period (fairly steady throughout, "
        "weaker at certain times of day, one day or hour pulling the average down or up) in "
        "addition to the reason/operator picture, even when nothing rises to the bar for "
        "notable_days/notable_hours specifically."
    ) if not compare_to_previous else ""

    summary_length_note = "one to two sentences" if compare_to_previous else "two to four sentences"

    recommendation_shift_note = ", or a specific period-over-period shift" if compare_to_previous else ""

    notable_days_priority_note = (
        ", and only when that's a more important story to tell than the period-over-period "
        "comparison. If the comparison is the more useful thing to focus on this time, write "
        "about that instead and leave notable_days empty rather than flagging a day in passing"
    ) if compare_to_previous else ""

    intro_period_note = " compared to the period before" if compare_to_previous else ""

    return f"""You are a performance analyst for DTPay, a payment integration platform, writing an analysis that will be shown directly to content-service partners (e.g. KidsFlix, Learn2bFit) about how their service performed{intro_period_note}.

DTPay routes different products through different payment providers (e.g. pawapay for African mobile money, razorpay for Indian UPI/card/bank payments), given to you as `provider`. What "operator" means in the data depends on it: for a mobile-money provider it's the actual telecom network (MTN, Airtel, ...); for a card/UPI provider it's the payment channel or method instead (upi, card, ...), not a network. Use whichever framing actually fits the given provider rather than assuming it's always a mobile network.

For each service you're given, write:
- summary: {summary_length_note} on what's actually happening, grounded in the failure/rejection reasons and operator data - not generic commentary. Refer to the service by its product_name and the country by its full name (both given to you) rather than an ID or abbreviation.{comparison_summary_note}{temporal_emphasis_note}
- recommendations: 1-3 specific, actionable items tied to the actual data (a specific failure reason, a specific underperforming network or channel{recommendation_shift_note}), not generic advice like "improve your conversion rate."
- notable_days: you're also given `daily`, a day-by-day breakdown. Only include a date here if your summary text explicitly names and discusses that day by weekday - never flag a day here that the summary doesn't actually talk about, since the report highlights these days visually and the reader will expect the text right below to explain why. Use this sparingly: only for a day that's a genuinely clear standout (a real dip or spike), not just modestly above or below the period's average{notable_days_priority_note}. A day with much lower volume than the others can look artificially swingy in its rate; weigh that before calling it notable at all.
- notable_hours: you're also given `hourly`, an hour-by-hour breakdown covering the same period as `daily`, when the requested range is short enough for it to be included at all. If a specific hour (or a recurring time of day across multiple days) clearly stands out - a burst of volume, a spike or crash in conversion - name it in your summary and list its exact timestamp (the "hour" field) here, same bar as notable_days: a genuinely clear standout, only ever an hour you actually discuss, and weigh low volume before calling one out. If `hourly` is empty, leave this empty too.

Rules:
- Never restate specific conversion percentages, counts, or figures in your text - those are shown to the partner separately, right next to your summary. This applies to daily and hourly figures too, not just the headline ones. Referring to categories ("insufficient balance", "one of the networks") or directional language ("improved", "worsened", "roughly doubled") is fine; restating "3.28%" or "1,401 of 42,684" is not.
- Do not frame operator- or customer-side issues (insufficient balance, OTP timeout, an operator's own eligibility rules, "no active deposit flow") as the partner's fault - describe those as market/network conditions outside the partner's control, not something for them to fix on their end.
- If total_resolved is under about 30, say plainly that the volume is too low to draw a real conclusion from, rather than treating a single-digit sample as a meaningful rate.
"""


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "services": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "product_id": {"type": "integer"},
                    "summary": {"type": "string"},
                    "recommendations": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "notable_days": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "ISO dates (YYYY-MM-DD) that the summary specifically calls out as notably different from the rest of the period. Empty if nothing stands out.",
                    },
                    "notable_hours": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "ISO hour timestamps (YYYY-MM-DDTHH:00:00) that the summary specifically calls out. Empty if nothing stands out, or if no hourly data was given at all.",
                    },
                },
                "required": ["product_id", "summary", "recommendations", "notable_days", "notable_hours"],
            },
        },
    },
    "required": ["services"],
}


def _service_payload(service):
    """Strips a rolled-up service record down to what Claude needs to
    see. previous_* fields are only included when compare_to_previous
    is set (the weekly email) - omitted entirely, not left null, for
    the on-demand API, so there's no "previous period" concept in the
    payload at all for Claude to comment on the absence of."""
    payload = {
        "product_id": service["product_id"],
        "product_name": service["product_name"],
        "country": service["country"],
        "provider": service.get("provider"),
        "total_resolved": service["total_resolved"],
        "completed": service["completed"],
        "failed": service["failed"],
        "conversion_rate_pct": service["conversion_rate_pct"],
        "top_reasons": [
            {"reason": r["reason_code"], "occurrences": r["occurrences"]}
            for r in sorted(service["reasons"], key=lambda r: -r["occurrences"])[:5]
        ],
        "operators": [
            {"operator": o["operator"], "attempts": o["attempts"], "ok": o["operator_ok"]}
            for o in service["operators"]
        ],
        "daily": [
            {
                "date": d["date"].isoformat(),
                "total_resolved": d["total_resolved"],
                "completed": d["completed"],
                "conversion_rate_pct": d["conversion_rate_pct"],
            }
            for d in service.get("daily", [])
        ],
        "hourly": [
            {
                "hour": h["hour"].isoformat(),
                "total_resolved": h["total_resolved"],
                "completed": h["completed"],
                "conversion_rate_pct": h["conversion_rate_pct"],
            }
            for h in service.get("hourly", [])
        ],
    }

    if service.get("compare_to_previous"):
        payload["previous_total_resolved"] = service.get("previous_total_resolved")
        payload["previous_conversion_rate_pct"] = service.get("previous_conversion_rate_pct")
        payload["conversion_rate_delta"] = service.get("conversion_rate_delta")
        payload["previous_top_reasons"] = [
            {"reason": r["reason_code"], "occurrences": r["occurrences"]}
            for r in sorted(service.get("previous_reasons", []), key=lambda r: -r["occurrences"])[:5]
        ]

    return payload


def analyze_partner(digest, client=None):
    """
    Takes one partner digest (from rollup.rollup_by_partner), adds a
    'summary' and 'recommendations' to each of its services, and
    returns the same digest. If the Claude call fails for any reason,
    the digest is returned unchanged (empty summary/recommendations)
    rather than breaking the whole run over one partner - check
    agent.log for what actually happened when that occurs.

    Whether this digest gets the period-over-period framing is read
    off the services themselves (merge_with_previous sets
    compare_to_previous=True on every service when it runs; the
    on-demand API never calls it, so it stays unset) - every service
    in one digest always comes from the same caller, so checking the
    first one is enough.
    """
    client = client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    compare_to_previous = bool(digest["services"]) and bool(digest["services"][0].get("compare_to_previous"))
    system_prompt = _build_system_prompt(compare_to_previous)
    payload = {"services": [_service_payload(s) for s in digest["services"]]}

    by_product = {}
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4096,
            betas=[STRUCTURED_OUTPUT_BETA],
            thinking={"type": "disabled"},
            system=system_prompt,
            messages=[{"role": "user", "content": json.dumps(payload)}],
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
    except Exception as exc:
        logger.error("Claude API call failed for cp_id=%s: %s", digest.get("cp_id"), exc)
        response = None

    if response is not None:
        text_blocks = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        if not text_blocks:
            block_types = [getattr(b, "type", type(b).__name__) for b in response.content]
            logger.error(
                "Claude's response for cp_id=%s had no text block at all - block types were: %s",
                digest.get("cp_id"), block_types,
            )
        else:
            raw_text = "".join(text_blocks)
            try:
                result = json.loads(raw_text)
                by_product = {s["product_id"]: s for s in result["services"]}
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.error(
                    "Claude's response for cp_id=%s wasn't the expected JSON shape (%s). Raw response: %s",
                    digest.get("cp_id"), exc, raw_text[:2000],
                )

    for service in digest["services"]:
        analysis = by_product.get(service["product_id"], {})
        service["summary"] = analysis.get("summary", "")
        service["recommendations"] = analysis.get("recommendations", [])
        service["notable_days"] = analysis.get("notable_days", [])
        service["notable_hours"] = analysis.get("notable_hours", [])

    return digest