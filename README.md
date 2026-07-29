# DTPay Partner Performance Agent

Weekly job: pulls the most recently completed Monday-Sunday week's
transaction data, compares it against the week before, rolls it up
**per partner** (not per service — one partner can run 20+
service/country rows, see Digital Technology / cp_id 122), asks
Claude for a summary and recommendations per service, and emails one
digest per partner.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in:
   - `DTPAY_DB_*` — the same database this data already lives in
   - `ANTHROPIC_API_KEY` — your Claude API key
   - `SMTP_*` — your email provider's SMTP credentials (SendGrid, SES,
     Postmark, Mailgun all support SMTP relay alongside their REST APIs)
   - leave `REVIEW_MODE=true` for now
3. `python main.py`

With `REVIEW_MODE=true` (the default), nothing gets emailed — each
partner's digest is written as an HTML file to `review_output/`
instead, so a few weeks of output can be read before any partner sees
one. Flip to `REVIEW_MODE=false` only once that's trusted.

## Scheduling

`main.py` is a script, not a daemon. Point your scheduler at it once
a week — on Windows, Task Scheduler set to weekly; on a Linux host,
cron, e.g. 6am every Monday:

```
0 6 * * 1  cd /path/to/project && /path/to/venv/bin/python main.py
```

## Files

- `config.py` — all settings, read from environment variables
- `extract.py` — four queries: conversion metrics, reason breakdown,
  operator breakdown, and a day-level breakdown within the week
  (`DAILY_METRICS_SQL`, only fetched for the current week, not the
  previous one). `PRODUCT_METRICS_SQL` excludes any service with fewer
  than `MIN_RESOLVED_THRESHOLD` (default 100) resolved transactions in
  the window, so low-volume noise never reaches Claude or the email at all
- `rollup.py` — groups per-product rows into one digest per partner,
  merges the current week's digest with the previous week's
  (`merge_with_previous`, adding a conversion-rate delta per service),
  and fills in a complete 7-day series per service (zeros for any day
  with no data) from the daily breakdown
- `analyze.py` — calls Claude for the narrative + recommendations,
  given both weeks' data plus the daily breakdown so it can write the
  comparison and name specific days itself (`notable_days` in its
  response - the exact dates its own summary calls out, so the chart
  and the words always point at the same days); every rule baked into
  the system prompt (low-volume caveat, never blame the partner for
  operator/customer-side failures, never restate numbers) traces back
  to something found earlier in this build
- `email_template.py` — renders a digest into HTML: a week-over-week
  line per service, then a 7-bar daily chart (plain HTML tables with
  fixed-pixel-height divs — no JS, no embedded images, since Outlook's
  desktop renderer supports neither reliably) with days from
  `notable_days` colored differently, then Claude's summary/recommendations
- `email_sender.py` — sends it, or saves it to `review_output/`
- `main.py` — `run_weekly()` runs the full chain for the most recently
  completed calendar week