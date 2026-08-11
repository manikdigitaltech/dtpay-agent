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

## Timeouts, and diagnosing a hung request

Every DB connection and every Claude call is bounded by a timeout
(`DB_CONNECT_TIMEOUT_SECONDS`, `DB_READ_TIMEOUT_SECONDS`,
`CLAUDE_TIMEOUT_SECONDS` in `.env` / `config.py`) - this wasn't true
until a real request to `/summary` hung for 10+ minutes with no
response and no error. Without a timeout, a stuck connection or an
unreachable API has exactly one way to fail: forever, silently,
tying up a thread and a DB connection the whole time. With one, the
same problem becomes a normal, fast error instead.

If a request hangs despite this (or before upgrading to a build that
has these timeouts), the fastest way to tell which side it's on:
`SHOW FULL PROCESSLIST;` against MySQL while it's still hanging. A
query still running against `payment_transactions` or `payout_logs`
means the DB side; nothing unusual there means it's most likely stuck
on the Claude API call instead (check outbound network access to
`api.anthropic.com` from wherever this runs).

**If it turns out to be the DB side**, the likely cause is a missing
index — every query in `extract.py` filters on `cp_product_id IN
(...)` and a `date_time` range, and testing this project never ran
against a table anywhere near production size, so an absent index
here would never have shown up as a problem until now. Worth
confirming with `EXPLAIN` on the actual query, and if it's not using
an index, adding one:
```sql
CREATE INDEX idx_payment_transactions_cp_product_date
    ON payment_transactions (cp_product_id, date_time);
CREATE INDEX idx_payout_logs_cp_product_date
    ON payout_logs (cp_product_id, date_time);
```

**To see exactly what's being sent to and received from Claude**, set
`LOG_CLAUDE_PROMPTS=true` and restart. Every call (weekly email,
dashboard summary, chat) then writes its full system prompt, message
payload, and raw response text to `agent.log` before/after sending -
off by default since a payload can run to several KB per call
(especially with hourly data included), so it's meant for active
debugging, not left on in normal operation.

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
  partner, then flattens the results into one JSON response. Also
  creates a chat session for every summary (`chat_store.create_session`)
  and exposes `POST /chat` for the follow-up "Any Questions?" flow
- `chat.py` — answers follow-up questions about a summary already
  shown to the user. No tools, no DB/code access during the call - see
  the module docstring for exactly what guarantees that and why
- `chat_store.py` — database access for chat sessions and the unified
  `agent_chat_logs` table (weekly email, dashboard summary, and chat
  all log here, distinguished by a `source` column)
- `cleanup_sessions.py` — deletes expired chat sessions; nothing else
  does this automatically, so run it periodically (daily via cron or
  Task Scheduler) or `agent_chat_sessions` grows unbounded
- `logging_setup.py` — shared rotating logger for `agent.log`, used
  by `analyze.py` and `chat.py`, so it doesn't grow forever
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

**No period-over-period comparison here, on purpose.** The weekly
email always compares against the previous calendar week; this
endpoint takes an arbitrary date range with no well-defined "previous
period" (a first version reused the weekly prompt unmodified, and
Claude correctly-but-uselessly wrote "there's no prior week to
compare against" for a request that was never about weeks at all).
`analyze.py`'s prompt only mentions comparison when a digest's
services carry `compare_to_previous=True`, which only
`rollup.merge_with_previous()` ever sets - this endpoint never calls
it, so the concept doesn't exist in what Claude sees at all.

**Range limited to `MAX_DATE_RANGE_DAYS` (`config.py`, default 7,
inclusive of both `start_date` and `end_date`)** — a request beyond
that gets a `400` (`{"detail": "Date range spans N days; the maximum
allowed is 7."}`) rather than being processed. Change the limit by
updating the env var and restarting the API process; no code change
or redeploy needed.

**Day-wise and hour-wise breakdowns are both included**, each
product's response entry carries `daily` (one entry per day in the
range, gap-filled with real zeros the same way the weekly chart is,
generalized to however many days were actually requested rather than
hardcoded to 7) and `hourly` (same idea at hour granularity, `null`
instead of a list if the range is longer than
`ON_DEMAND_MAX_HOURLY_DAYS` in `api.py`, currently 14 days - beyond
that it's hundreds of rows and not actually more useful than the
daily view). Claude sees both too, and can call out a specific day or
hour by name in `notable_days`/`notable_hours` the same way the
weekly email flags a standout day in its chart.

No volume floor here (`ON_DEMAND_MIN_RESOLVED = 1` in `api.py`) —
unlike the weekly email, which hides anything under
`MIN_RESOLVED_THRESHOLD` so a partner never gets an alarming "0% of
1" in their inbox, this endpoint is attached to a dashboard that
already shows those same low-volume rows in plain sight. Hiding them
here would just be inconsistent with what's already on screen.
Analyze.py's system prompt is told to flag low volume explicitly
instead of pretending a 1-attempt row is a real rate.

The response also includes a `session_id` — every `/summary` call
creates a chat session behind the scenes (see below) whether or not
the user ever clicks "Any Questions?".

## Chat ("Any Questions?")

**`context_data` includes the reason and operator breakdowns, both in
aggregate and per-day** — this wasn't true in the first version, and
a real chat question ("what are the errors on the 25th?") surfaced
it: the response only ever had `total_resolved`/`completed`/rate per
day, never the failure reasons or operators behind them, so any
reason- or operator-related question was unanswerable regardless of
whether it was day-specific. Each product now carries a top-level
`reasons`/`operators` (aggregate across the whole range) and, inside
each entry in `daily`, a `reasons`/`operators` scoped to just that
day - co-located with that day's numbers rather than a separate
array the model would have to cross-reference by date.

Run these once against your database before using either endpoint:

```sql
CREATE TABLE agent_chat_sessions (
    id VARCHAR(36) PRIMARY KEY,
    uid INT NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    cp_product_id INT,
    context_data JSON NOT NULL,
    created_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL
);

CREATE TABLE agent_chat_logs (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    session_id VARCHAR(36),
    uid INT,
    cp_id INT,
    source VARCHAR(20) NOT NULL,   -- 'weekly_email' | 'dashboard_summary' | 'chat'
    role VARCHAR(10) NOT NULL,     -- 'user' | 'assistant'
    message TEXT,
    input_tokens INT,
    output_tokens INT,
    cache_creation_input_tokens INT,
    cache_read_input_tokens INT,
    created_at DATETIME NOT NULL
);
```

**Once prompt caching (below) is in place, `input_tokens` alone
badly understates real cost - `cache_creation_input_tokens` and
`cache_read_input_tokens` are what actually catch it.** A real call
in this session showed `input_tokens=15` for a question that clearly
needed the full context behind it - that's not the call costing next
to nothing, it's the cached portion (the bulk of the real cost)
showing up in a different field entirely, one the logging didn't
capture at first. All three fields together are the real input cost;
`input_tokens` by itself is only the fresh, non-cached part.

`agent_chat_logs` is the single place token usage is logged for
**all three** Claude-calling paths, not just chat — `source`
distinguishes them, and `session_id`/`uid`/`cp_id` are populated
differently per source since each one has a different idea of "who
triggered this": `weekly_email` rows have neither `session_id` nor
`uid` (a scheduled job, not a person, triggers it) but do have
`cp_id`; `dashboard_summary` rows have `uid` and `cp_id` but no
`session_id` on the row itself (the session is the one just created,
tracked in `agent_chat_sessions` instead); `chat` rows have both
`session_id` and `uid`, one row per side of each exchange (`role`
`'user'` for the question, `'assistant'` for the reply, the latter
carrying the token counts).

`POST /chat`:
```json
{
  "uid": 122,
  "session_id": "2fda4486-f5b2-4d6a-a649-ecdb0793a4e6",
  "message": "Why did DRC dip on the 25th?"
}
```
Same `Authorization: Bearer <jwt>` header as `/summary`. Auth is
identical to `/summary`'s, plus one more check specific to chat: the
session must belong to the requesting `uid`, or the requester must be
`ADMIN` — `403` otherwise. Sessions expire 24h after the `/summary`
call that created them (`SESSION_TTL_HOURS` in `chat_store.py`); past
that, `410`. An unrecognized `session_id` is `404`.

**Guardrails are architectural, not just prompted** — this was the
explicit ask, so it's worth being precise about what's actually
guaranteed versus what's merely instructed:
- Claude gets **no tools** in `/chat`. No function-calling, no code
  execution, no database access during the call - it only ever
  receives the session's already-stored `context_data` (identical to
  what `/summary` returned - nothing is re-queried live) plus text,
  and returns text. There is no code path in `chat.py` or `api.py`
  where the user's message reaches SQL, a shell, or any write
  operation - not "the prompt says not to," but no such path exists.
  Verified directly: sent `'; DROP TABLE dtpay_users; --` as a chat
  message, got a normal `200` back, and confirmed both that
  `dtpay_users` was untouched and that the literal string was stored
  as plain text in `agent_chat_logs.message`, never interpreted as SQL.
- Staying on-topic (only answering from this session's data) and
  reply length (1-2 sentences by default) are prompt-level, in
  `chat.py`'s `CHAT_SYSTEM_PROMPT` - those are about response
  quality, not about what the system will let happen, so they can't
  be made architectural the same way.
- Chat answers **are** allowed to state specific numbers directly
  (unlike the summary text) - the number only ever appears once, in
  the answer itself, so there's no duplicate-elsewhere-in-the-output
  to drift out of sync with, unlike the summary/chart split that's
  why `analyze.py` avoids it there.

**Conversation history sent to Claude is capped at the last 5 rows**
in `agent_chat_logs` for that session (`CHAT_HISTORY_LIMIT` in
`chat_store.py`), not the full conversation - keeps a long
back-and-forth from growing the request without bound. Worth being
precise about what this means in practice: each turn logs 2 rows (the
question, the answer), so 5 rows is roughly the last 2-3 exchanges,
not 5 full question-answer pairs - flag it if you meant the latter,
it's a one-line change (`CHAT_HISTORY_LIMIT = 10`).

**Separately, `MAX_QUESTIONS_PER_SESSION` (`config.py`, default 5) is
a hard cap on how many questions a session allows at all** - not to
be confused with `CHAT_HISTORY_LIMIT` above, which only trims how
much *history* gets resent to Claude and never blocks a message.
This one counts `role='user'` rows for the session; the 5th question
is answered normally, a 6th attempt gets a `429`
(`{"detail": "This session has reached its limit of 5 questions.
Generate a new summary to keep asking questions."}`) before it's
logged or sent to Claude at all - a rejected attempt is never counted,
so retrying doesn't dig the hole deeper, it just stays rejected.
Change the limit the same way as `MAX_DATE_RANGE_DAYS`: update the
env var, restart.

## Token usage optimizations

**Caching surfaced a real gap in the logging itself, since fixed:**
`chat.ask()` now returns a dict (`answer`, `input_tokens`,
`output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`) instead of a 3-item tuple - if anything
outside `api.py` calls it directly, that call site needs updating.
`analyze_partner()`'s digest gained the same two new keys. See the
DDL above for why both new columns matter.

Two changes, both verified to leave what Claude actually sees
unchanged (or, for the one exception, verified to lose nothing that
matters) - neither should affect summary or chat quality, only cost:

**Prompt caching.** The system prompt in both `analyze.py` and
`chat.py` is marked `cache_control: {"type": "ephemeral"}` - repeated
calls with the same prefix read it from cache instead of reprocessing
it fresh. This matters most for chat: the same `context_data` gets
resent in full on every turn in a session (up to `MAX_QUESTIONS_PER_SESSION`
times), and it's now the cached part rather than fully reprocessed
each time. It also helps a weekly run or a multi-partner admin
request in `analyze.py`, where the same prompt repeats once per
partner. Verified the request shape is accepted by the real API
(reached auth rejection, not a shape-validation error) on both the
beta and standard endpoints.

**Compact tables instead of repeating field names per row.**
`compact.py`'s `to_compact_table()` turns a list of daily/hourly
objects into one header line plus one line per row, comma-separated -
same numbers, without restating `total_resolved`/`completed`/etc. on
every single row. Verified with a real round-trip test: convert real
extracted hourly data to the compact form, parse it back, and confirm
it's exactly equal to the original list of dicts. On a single day's
real hourly data this cut the JSON size by two-thirds.

Applied differently in the two places that use it, because their
`daily` fields aren't the same shape:
- `analyze.py`'s `_service_payload()` compacts both `daily` and
  `hourly` - neither ever carried per-row reasons/operators, so
  there's nothing at risk.
- `chat.py`'s `_compact_context_data()` compacts only `hourly`.
  `context_data`'s `daily` (unlike `analyze.py`'s) carries each day's
  own reasons/operators breakdown - the fix from a few turns back for
  answering "what were the errors on day X" - and a flat numeric
  table has no way to represent that, so compacting it would have
  silently undone that fix. `daily` is left exactly as stored;
  `hourly` never had that problem (it's only ever
  total_resolved/completed/conversion_rate_pct) and can run to
  hundreds of rows for a longer range, so it's where the real savings
  are anyway.

## Known gap

If a service had activity last week but none this week, it won't
appear in the email at all (current week's digest only contains
services present in the current window) — silently dropped rather
than flagged. Hasn't come up yet; worth adding if it does.

## Fixed: duplicate reason_code entries

Found in real production data (VUZ360/Uganda, a different partner
than anything tested during development) — `INSUFFICIENT_BALANCE`
appeared twice in one product's `reasons` list, at 219 and 8
occurrences, instead of once at 227. Cause: `REASON_COUNTS_SQL`
groups by `(product_id, transaction_status, reason_code)`, and the
same `reason_code` string can come from two different
`transaction_status` values - here, one `FAILED` row where
`cb_error_message` was `INSUFFICIENT_BALANCE#...` (parsed via the
`#` split) and a separate row under a different status where
`partner_error_message` was the literal string `INSUFFICIENT_BALANCE`
directly. `providers.merge_duplicate_reasons()` now combines these
after `filter_reason_rows()` runs, for both the aggregate and
per-day breakdowns. Confirmed `operators` never had this problem -
operator classification already aggregates across every status in
Python, reasons just never had the equivalent step until now.

## What's verified vs. still open

Verified: the SQL logic against real sample exports (several rounds
earlier), the partner-rollup and week-over-week merge logic against
real and fixture data, and the Claude request shape against the live
API (got a real `invalid x-api-key` back from api.anthropic.com with
a throwaway key — proves the request reaches the server correctly,
just needs a real key).

Not yet run: a real two-week comparison against the actual database —
only against fixture data standing in for a "previous week."