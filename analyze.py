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

SYSTEM_PROMPT = """You are a performance analyst for DTPay, a payment integration platform, writing a weekly analysis that will be shown directly to content-service partners (e.g. KidsFlix, Learn2bFit) about how their service performed this week compared to the week before.

DTPay routes different products through different payment providers (e.g. pawapay for African mobile money, razorpay for Indian UPI/card/bank payments), given to you as `provider`. What "operator" means in the data depends on it: for a mobile-money provider it's the actual telecom network (MTN, Airtel, ...); for a card/UPI provider it's the payment channel or method instead (upi, card, ...), not a network. Use whichever framing actually fits the given provider rather than assuming it's always a mobile network.

For each service you're given, write:
- summary: one to two sentences on what's actually happening, grounded in this week's failure/rejection reasons and operator data - not generic commentary. Refer to the service by its product_name and the country by its full name (both given to you) rather than an ID or abbreviation. When previous-week data is present, make the comparison the point of the summary (what changed, and - if the reason/operator breakdown suggests why - what likely drove it), not just this week's standalone number. When previous_conversion_rate_pct is null, there's nothing to compare against (new this week, or no resolved volume last week) - say so plainly rather than implying a trend.
- recommendations: 1-3 specific, actionable items tied to the actual data (a specific failure reason, a specific underperforming network or channel, or a specific week-over-week shift), not generic advice like "improve your conversion rate."
- notable_days: you're also given `daily`, a day-by-day breakdown of this week. Only include a date here if your summary text explicitly names and discusses that day by weekday - never flag a day here that the summary doesn't actually talk about, since the email highlights these days visually and the reader will expect the text right below to explain why. Use this sparingly: only for a day that's a genuinely clear standout (a real dip or spike), not just modestly above or below the week's average, and only when that's a more important story to tell than the week-over-week comparison. If the week-over-week comparison is the more useful thing to focus on this time, write about that instead and leave notable_days empty rather than flagging a day in passing. A day with much lower volume than the others can look artificially swingy in its rate; weigh that before calling it notable at all.

Rules:
- Never restate specific conversion percentages, counts, or figures in your text - those are shown to the partner separately, right next to your summary. This applies to daily figures too, not just the weekly ones. Referring to categories ("insufficient balance", "one of the networks") or directional language ("improved", "worsened", "roughly doubled") is fine; restating "3.28%" or "1,401 of 42,684" is not.
- Do not frame operator- or customer-side issues (insufficient balance, OTP timeout, an operator's own eligibility rules, "no active deposit flow") as the partner's fault - describe those as market/network conditions outside the partner's control, not something for them to fix on their end.
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
                        "description": "ISO dates (YYYY-MM-DD) from this week's daily breakdown that the summary specifically calls out as notably different from the rest of the week. Empty if nothing stands out.",
                    },
                },
                "required": ["product_id", "summary", "recommendations", "notable_days"],
            },
        },
    },
    "required": ["services"],
}


def _service_payload(service):
    """Strips a rolled-up service record down to what Claude needs to see."""
    return {
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
        "previous_total_resolved": service.get("previous_total_resolved"),
        "previous_conversion_rate_pct": service.get("previous_conversion_rate_pct"),
        "conversion_rate_delta": service.get("conversion_rate_delta"),
        "previous_top_reasons": [
            {"reason": r["reason_code"], "occurrences": r["occurrences"]}
            for r in sorted(service.get("previous_reasons", []), key=lambda r: -r["occurrences"])[:5]
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
    }


def analyze_partner(digest, client=None):
    """
    Takes one partner digest (from rollup.rollup_by_partner), adds a
    'summary' and 'recommendations' to each of its services, and
    returns the same digest. If the Claude call fails for any reason,
    the digest is returned unchanged (empty summary/recommendations)
    rather than breaking the whole weekly run over one partner - check
    agent.log for what actually happened when that occurs.
    """
    client = client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    payload = {"services": [_service_payload(s) for s in digest["services"]]}

    by_product = {}
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4096,
            betas=[STRUCTURED_OUTPUT_BETA],
            thinking={"type": "disabled"},
            system=SYSTEM_PROMPT,
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

    return digest