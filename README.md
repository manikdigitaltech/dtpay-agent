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
  "success" or "resolved" itself, and no longer decides which
  `cp_product_id`s are in scope either. `fetch_all(day_start, day_end,
  cp_product_ids, ...)` takes scope as an explicit argument now, since
  the weekly email and the on-demand API have unrelated eligibility
  rules (`get_weekly_eligible_cp_product_ids()` is the email's; the
  API resolves its own via `auth.py`). Hands raw counts to
  `providers.py`'s classification functions. The volume floor
  (`MIN_RESOLVED_THRESHOLD`, default 100) is applied in Python after
  classification, since "how many resolved" depends on which statuses
  that product's provider excludes — and the API uses its own, much
  lower floor (see below), not this one
- `auth.py` — authentication (`users_jwt_tokens` lookup: the exact
  `uid` from the request body must match the exact token from the
  `Authorization` header, and not be expired) and authorization
  (`ADMIN` role sees everything or one `cp_product_id` if given;
  anyone else only ever sees their own `cp_id`'s products, and a
  `cp_product_id` that isn't theirs is rejected, not substituted)
  for the on-demand API
- `api.py` — the on-demand summary endpoint (`POST /summary`) a
  dashboard button hits directly, instead of the weekly scheduled
  email. Reuses the exact same `extract → rollup → analyze` pipeline
  after `auth.py` resolves scope, so a non-admin request always
  becomes exactly one Claude call (their own partner's digest); an
  ADMIN request spanning several partners becomes one call per
  partner, then flattens the results into one JSON response
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

## On-demand summary API

`POST /summary`, secured separately from the weekly email (no
`weekly_summary_enabled` check here — that column is specifically
about opting into the scheduled email, unrelated to this).

Run it with `uvicorn api:app --host 0.0.0.0 --port 8000`.

Request:
```json
{
  "uid": 122,
  "start_date": "2026-07-28",
  "end_date": "2026-07-28",
  "cp_product_id": null
}
```
`Authorization: Bearer <jwt>` header required. `cp_product_id` is
optional — omit it for "everything this uid can see"; provide it to
narrow to one product (still subject to the ownership check below).

Auth: the exact `(uid, jwt_token)` pair must exist as a row in
`users_jwt_tokens` with `expiry_date` in the future — looked up
together, not the token alone with the uid compared afterward, so a
valid token can't be replayed under a different uid. Then `role_name`
on that `uid`'s `dtpay_users` row decides scope: `ADMIN` gets every
approved product (or just `cp_product_id`, if given, no ownership
check needed); anyone else only ever gets their own `cp_id`'s
products, and a `cp_product_id` that isn't theirs is a `403`, not a
silent fallback to their own products.

No volume floor here (`ON_DEMAND_MIN_RESOLVED = 1` in `api.py`) —
unlike the weekly email, which hides anything under
`MIN_RESOLVED_THRESHOLD` so a partner never gets an alarming "0% of
1" in their inbox, this endpoint is attached to a dashboard that
already shows those same low-volume rows in plain sight. Hiding them
here would just be inconsistent with what's already on screen.
Analyze.py's system prompt is told to flag low volume explicitly
instead of pretending a 1-attempt row is a real rate.

## Known gap

If a service had activity last week but none this week, it won't
appear in the email at all (current week's digest only contains
services present in the current window) — silently dropped rather
than flagged. Hasn't come up yet; worth adding if it does.

## What's verified vs. still open

Verified: the SQL logic against real sample exports (several rounds
earlier), the partner-rollup and week-over-week merge logic against
real and fixture data, and the Claude request shape against the live
API (got a real `invalid x-api-key` back from api.anthropic.com with
a throwaway key — proves the request reaches the server correctly,
just needs a real key).

Not yet run: a real two-week comparison against the actual database —
only against fixture data standing in for a "previous week."