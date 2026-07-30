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
- `providers.py` — **the provider-specific rules live here, not in SQL.**
  DTPay routes products through different wallet providers (pawapay,
  razorpay confirmed so far; ampere/thirdpay not live yet) with
  different success markers (`COMPLETED` for pawapay, `AUTHORIZED` for
  razorpay), different pending/excluded statuses, and different
  meaningful comparison fields (`operator`/network for pawapay,
  `channel`/payment method for razorpay, since `operator` is always
  just `'razorpay'` there). `PROVIDER_RULES` is the single place that
  knowledge lives; adding a provider once it's live is a new dict
  entry, not new SQL or new queries
- `extract.py` — fetches *raw counts* grouped by product, provider,
  and status (via `agg_name`) — it no longer decides what counts as
  "success" or "resolved" itself. `fetch_all()` hands those raw counts
  to `providers.py`'s classification functions and returns the same
  shape as before the provider split, so `rollup.py` downstream didn't
  need to change. The volume floor (`MIN_RESOLVED_THRESHOLD`, default
  100) is applied in Python after classification now, since "how many
  resolved" depends on which statuses that product's provider excludes
- `rollup.py` — groups per-product rows into one digest per partner,
  merges the current week's digest with the previous week's
  (`merge_with_previous`, adding a conversion-rate delta per service),
  and fills in a complete 7-day series per service (zeros for any day
  with no data) from the daily breakdown
- `analyze.py` — calls Claude for the narrative + recommendations,
  given both weeks' data, the daily breakdown, and which provider a
  service uses (so it doesn't call a UPI/card channel a "network" the
  way pawapay's actual mobile networks are) so it can write the
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

## Known gap

If a service had activity last week but none this week, it won't
appear in the email at all (current week's digest only contains
services present in the current window) — silently dropped rather
than flagged. Hasn't come up yet; worth adding if it does.
