from datetime import datetime, timedelta, timezone

from extract import fetch_all, get_weekly_eligible_cp_product_ids
from rollup import rollup_by_partner, merge_with_previous
from analyze import analyze_partner
from email_template import render_partner_email
from email_sender import send_partner_email


def _last_complete_week(reference_date=None):
    """
    Returns (week_start, week_end) for the most recently completed
    Monday-Sunday week as of reference_date (defaults to today, UTC).
    week_end is exclusive. Using calendar weeks rather than "the 7
    days before whenever this happens to run" keeps the reporting
    period fixed and predictable regardless of which day the
    scheduler actually fires on.
    """
    if reference_date is None:
        reference_date = datetime.now(timezone.utc).date()
    this_monday = reference_date - timedelta(days=reference_date.weekday())
    week_start = this_monday - timedelta(days=7)
    week_end = this_monday
    return week_start, week_end


def run_weekly(reference_date=None):
    """
    Extracts, rolls up, compares against the previous week, analyzes,
    and emails one digest per partner - covering the most recently
    completed calendar week, compared against the week before it.
    """
    week_start, week_end = _last_complete_week(reference_date)
    prev_week_start, prev_week_end = week_start - timedelta(days=7), week_start

    eligible = get_weekly_eligible_cp_product_ids()

    current_metrics, current_reasons, current_operators, current_daily, _ = fetch_all(
        week_start, week_end, eligible, include_daily=True
    )
    previous_metrics, previous_reasons, previous_operators, _, _ = fetch_all(
        prev_week_start, prev_week_end, eligible
    )

    current_digests = rollup_by_partner(
        current_metrics, current_reasons, current_operators, current_daily, week_start, week_end
    )
    previous_digests = rollup_by_partner(previous_metrics, previous_reasons, previous_operators)

    merge_with_previous(current_digests, previous_digests)

    for digest in current_digests:
        analyze_partner(digest)
        html = render_partner_email(digest)
        send_partner_email(digest, html)

    return current_digests


if __name__ == "__main__":
    results = run_weekly()
    print(f"Processed {len(results)} partner digest(s).")