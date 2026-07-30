"""
Renders one analyzed partner digest (after rollup.py + analyze.py)
into an HTML email body, as flowing prose per service rather than a
label/value stat dump. Every number still comes straight from the
digest data (nothing here is LLM-generated) - only the sentence
structure, and which days the chart highlights (from Claude's own
notable_days), determine how it's framed.

Chart is plain HTML tables with fixed-pixel-height divs, not CSS
flexbox/grid or an embedded image - Outlook's desktop renderer
doesn't support either reliably, and this is the one technique that
survives across clients without JS.
"""
from datetime import datetime

BAR_COLOR = "#4a7fd6"
NOTABLE_COLOR = "#e2a03f"
NOTABLE_TEXT_COLOR = "#b5750f"
MAX_BAR_HEIGHT = 60
MIN_BAR_HEIGHT = 3  # visible stub for a 0% day rather than nothing


def _humanize_reason(code):
    if code is None:
        return "an unspecified issue"
    # Most reason codes are SCREAMING_SNAKE_CASE (INSUFFICIENT_BALANCE,
    # INVALID_PAYER_FORMAT); a few (system-error rows with a NULL or
    # exception_payout transaction_status) fall back to a full plain-
    # English sentence instead ("Something went wrong. Please try
    # again."). Blindly lowercasing/underscoring the latter reads as
    # garbled mid-sentence, so only transform the code-shaped ones.
    if code.isupper() or "_" in code:
        return code.replace("_", " ").strip().lower()
    stripped = code.rstrip(".!").strip()
    return stripped[:1].lower() + stripped[1:] if stripped else stripped


def _reason_phrase(reasons):
    if not reasons:
        return ""
    top = sorted(reasons, key=lambda r: -r["occurrences"])[:2]
    names = [_humanize_reason(r["reason_code"]) for r in top]
    if len(names) == 1:
        return f" The shortfall was mostly {names[0]}."
    return f" The shortfall was mostly {names[0]} and {names[1]}."


def _comparison_phrase(s):
    prev_rate = s.get("previous_conversion_rate_pct")
    delta = s.get("conversion_rate_delta")
    if prev_rate is None or delta is None:
        return " No comparable data from last week to measure this against yet."
    if delta > 0:
        return f" That's up from {prev_rate}% last week."
    if delta < 0:
        return f" That's down from {prev_rate}% last week."
    return f" That's unchanged from last week."


def _daily_chart_html(service):
    """
    A 7-bar chart of this week's daily conversion rate, with the
    date range in a caption (so it's never ambiguous which week this
    is) and each day's own percentage labeled above its bar (so
    there's no unlabeled number to puzzle over). Days Claude flagged
    in notable_days get a different color, tying the chart to
    whatever the summary text calls out by name.
    """
    daily = service.get("daily") or []
    if not daily:
        return ""

    notable = set(service.get("notable_days") or [])
    rates = [d["conversion_rate_pct"] for d in daily]
    max_rate = max(rates) or 1  # avoid divide-by-zero on an all-zero week

    pct_cells, bar_cells, date_cells = [], [], []
    for d in daily:
        date_str = d["date"] if isinstance(d["date"], str) else d["date"].isoformat()
        is_notable = date_str in notable
        bar_color = NOTABLE_COLOR if is_notable else BAR_COLOR
        text_color = NOTABLE_TEXT_COLOR if is_notable else "#333"
        height = max(MIN_BAR_HEIGHT, round((d["conversion_rate_pct"] / max_rate) * MAX_BAR_HEIGHT))
        day_label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %d")

        pct_cells.append(
            f"<td style='text-align:center;padding:0 6px;font-size:11px;font-weight:bold;color:{text_color};'>"
            f"{d['conversion_rate_pct']}%</td>"
        )
        bar_cells.append(
            "<td style='vertical-align:bottom;text-align:center;padding:2px 6px 0 6px;'>"
            f"<div style='width:20px;height:{height}px;background:{bar_color};"
            "border-radius:2px 2px 0 0;margin:0 auto;'></div></td>"
        )
        date_cells.append(
            f"<td style='text-align:center;font-size:10px;color:#999;padding-top:4px;'>{day_label}</td>"
        )

    first_date = daily[0]["date"] if isinstance(daily[0]["date"], str) else daily[0]["date"].isoformat()
    last_date = daily[-1]["date"] if isinstance(daily[-1]["date"], str) else daily[-1]["date"].isoformat()
    week_range = (
        f"{datetime.strptime(first_date, '%Y-%m-%d').strftime('%b %d')} to "
        f"{datetime.strptime(last_date, '%Y-%m-%d').strftime('%b %d')}"
    )

    volumes = [d["total_resolved"] for d in daily]
    vol_range = f"{min(volumes)}-{max(volumes)}" if min(volumes) != max(volumes) else f"{volumes[0]}"

    return f"""
    <p style="margin:14px 0 6px 0;font-size:12px;color:#888;font-weight:bold;">Daily conversion - {week_range}</p>
    <table style="border-collapse:collapse;">
      <tr>{''.join(pct_cells)}</tr>
      <tr>{''.join(bar_cells)}</tr>
      <tr>{''.join(date_cells)}</tr>
    </table>
    <p style="margin:6px 0 0 0;font-size:11px;color:#aaa;">Daily volume ranged from {vol_range} resolved attempts - worth weighing against how much a single day's percentage swings.</p>
    """


def _service_block(s):
    intro = (
        f"<strong>{s['product_name']}</strong> converted "
        f"<strong>{s['conversion_rate_pct']}%</strong> of resolved attempts in "
        f"<strong>{s['country']}</strong> this week, completing {s['completed']} of {s['total_resolved']}."
        f"{_comparison_phrase(s)}"
        f"{_reason_phrase(s.get('reasons', []))}"
    )

    chart_html = _daily_chart_html(s)

    summary_html = f"<p style='margin:14px 0 10px 0;color:#333;'>{s['summary']}</p>" if s.get("summary") else ""

    recs_html = ""
    if s.get("recommendations"):
        items = "".join(f"<li style='margin-bottom:4px;'>{r}</li>" for r in s["recommendations"])
        recs_html = f"<ul style='margin:6px 0;padding-left:20px;'>{items}</ul>"

    return f"""
    <div style="padding:16px 0;border-bottom:1px solid #e5e5e5;">
      <p style="margin:0;line-height:1.5;">{intro}</p>
      {chart_html}
      {summary_html}
      {recs_html}
    </div>
    """


def render_partner_email(digest):
    blocks = "".join(_service_block(s) for s in digest["services"])
    return f"""
    <html>
      <body style="font-family:Arial,Helvetica,sans-serif;color:#222;max-width:640px;">
        <h2 style="margin-bottom:4px;">Weekly performance summary</h2>
        <p style="color:#666;margin-top:0;">{digest['partner_name']}</p>
        {blocks}
        <p style="color:#999;font-size:12px;margin-top:24px;">
          Automated weekly summary from DTPay. Reply to this email with any questions.
        </p>
      </body>
    </html>
    """