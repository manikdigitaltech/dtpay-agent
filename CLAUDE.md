# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python service that turns DTPay payment-transaction data into partner-facing performance analysis, via two entrypoints that share one pipeline:

1. **Scheduled weekly email** — [main.py](main.py) `run_weekly()`, one HTML digest per partner covering the last completed Mon–Sun week vs. the week before.
2. **On-demand dashboard API** — [api.py](api.py), `POST /summary` (arbitrary date range, JSON out, Redis-cached) and `POST /chat` (follow-up Q&A grounded in that summary's stored data). Client-facing contract for both is in [CHAT_API.md](CHAT_API.md).

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in DTPAY_DB_*, ANTHROPIC_API_KEY, SMTP_*, REDIS_*, ALLOWED_ORIGINS

python main.py                                    # run the weekly job once
uvicorn api:app --host 0.0.0.0 --port 8000        # run the on-demand API (also GET /health)
python cleanup_sessions.py                        # delete expired chat sessions — schedule daily
```

There is no test suite, linter, or CI config in this repo. `compact.from_compact_table()` exists only for round-trip verification and is not called by application code — verification described in the README was done ad hoc, not as committed tests.

`REVIEW_MODE=true` (the default) writes each partner email to `review_output/` instead of sending it. Two DB tables (`agent_chat_sessions`, `agent_chat_logs`) must be created before either API endpoint works — DDL is in [README.md](README.md). Nothing deletes expired `agent_chat_sessions` rows automatically. Run [cleanup_sessions.py](cleanup_sessions.py) from cron or Task Scheduler. It never touches `agent_chat_logs`, which is the permanent audit and token-usage record.

## Pipeline

```
scope resolution → extract.fetch_all → rollup.rollup_by_partner → analyze.analyze_partner → render/respond
```

Both entrypoints run the identical middle three stages; they differ only at the ends. Changing anything in `extract`/`rollup`/`analyze` affects both — check both call sites.

| | weekly email | on-demand API |
|---|---|---|
| scope | `extract.get_weekly_eligible_cp_product_ids()` (approved + enabled + `weekly_summary_enabled`) | `auth.resolve_cp_product_ids()` (role + ownership) |
| volume floor | `MIN_RESOLVED_THRESHOLD` (100) | `ON_DEMAND_MIN_RESOLVED = 1` in [api.py](api.py) |
| prev-period comparison | yes — calls `rollup.merge_with_previous()` | never |
| caching | none | Redis via [summary_cache.py](summary_cache.py) |
| per-campaign breakdown | never | `extract.fetch_campaign_breakdown()`, whole-product requests only |
| placement / device split | never | `extract.fetch_segment_breakdown()`, on products and on each campaign |
| output | HTML via `email_template` → `email_sender` | JSON + `session_id`, passed through `sanitize_aggregator_names()` |

`fetch_all(day_start, day_end, cp_product_ids, ...)` does **not** resolve scope itself — callers pass `cp_product_ids` in explicitly, because the two eligibility rules are unrelated. It returns a 7-tuple: `(product_metrics, reason_breakdown, operator_breakdown, daily_metrics, hourly_metrics, daily_reason_breakdown, daily_operator_breakdown)`; the last four are empty lists unless the matching `include_*` flag is set.

**Two extraction paths.** `fetch_all` always runs both query sets. The legacy set reads `payment_transactions`, the unique users who opened checkout (top of funnel). The payout set reads `payout_logs`, one row per operator-API attempt, where conversion actually happens. Python then keeps one result per product based on `providers.uses_payout_logs_metrics(provider)`. **Both pawapay and razorpay now have this flag set to `True`**, so both use payout_logs. The module docstrings in `extract.py` and `providers.py` still say razorpay is on the legacy path; that is stale, and `PROVIDER_RULES` is the source of truth. Every payout query filters `request_type IN ('newpurchase', 'repurchase')`, which limits counts to user-initiated checkouts and leaves out things like recurring-billing attempts.

**Optional campaign filter.** Every query in both paths ends with `AND (%(campaign_id)s IS NULL OR campaign_id = %(campaign_id)s)`. `fetch_all(..., campaign_id=None)` therefore means "no filter", and the weekly email never passes one. `/summary` accepts `campaign_id` only together with `cp_product_id`. It looks up the name with `extract.get_campaign_name()` *after* the ownership check: `campaign_id` is a varchar and indexed in both tables, and one id maps to one name. The `{campaign_id, campaign_name}` pair is added to the cache key, to each service before `analyze_partner()` (so Claude knows the scope), to top-level `campaign` in the response, and to `context_data`. A new query must include this clause, or campaign requests will silently mix in the whole product's data.

**Per-campaign breakdown.** If `/summary` gets no `campaign_id`, it also calls `extract.fetch_campaign_breakdown()`. That is a separate query set, so `fetch_all`'s 7-tuple and the weekly email are untouched. The function returns `{product_id: [campaign, ...]}`, but only for products with two or more campaigns that have resolved volume. It reuses the providers.py classifiers by packing `product_id` as `(product_id, campaign_id)` and unpacking afterwards, so every success and exclusion rule stays in one place. The campaigns sum exactly to the product's totals and operators; this was verified on real data.

Each service carries `service["campaigns"]`. When at least one service in a digest has campaigns, `analyze.py` does two extra things:
- It adds a campaign section to the system prompt and the `campaign_overview`/`campaigns` fields to the output schema (`_output_schema(has_campaigns)`).
- It raises `max_tokens` to 8192.

When no service has campaigns, the prompt and schema are byte-identical to before. Claude keys its per-campaign summaries by `campaign_id`, using `""` for the no-campaign bucket. The Redis key carries `KEY_VERSION`; bump it whenever the cached response shape changes.

**Placement / device split.** `extract.fetch_segment_breakdown()` returns `{"by_product": {pid: ...}, "by_campaign": {(pid, cid): ...}}`. Each value is `{placements, devices}`, and either can be `None` when nothing has data. Things to know:
- **Capture is recent.** Both columns were captured only from 2026-10-12 in `payment_transactions` and from 2026-10-13 in `payout_logs`.
- **Payout rows fall back** to the linked `payment_transactions` row (`LEFT JOIN … ON pt.id = pl.payment_transaction_id`) when their own column is empty.
- **Values are reduced first** because they are extremely long-tailed (about 3k placements and 1.7k device models per day). Device becomes its brand in SQL. Placements become an app-vs-website split plus the top 10 with at least 30 resolved in Python, and the rest go into an "other" row.
- **Classification** uses `providers.classify_segment_counts()` and `classify_payout_segment_counts()`, which are generic over a field name.
- **Gating in analyze.py**: `_segment_payload()` adds `covers_all_traffic`, which is true when coverage is at least 95%. Without it, Claude kept leaving out the partial-coverage caveat. The prompt section is gated by `has_segments`, so the weekly email prompt is still unchanged.
- **The text has no schema fields of its own.** It goes into the existing summary, recommendations and campaign fields.

## Architectural rules that are easy to break

**Provider knowledge lives only in [providers.py](providers.py).** SQL in [extract.py](extract.py) fetches raw counts grouped by status/operator/channel and decides nothing about success. `PROVIDER_RULES` is where "COMPLETED means success for pawapay, AUTHORIZED for razorpay", which statuses are excluded from the denominator, and which field (`operator` vs `channel`) is the meaningful comparison axis all live. Adding a provider is a new dict entry — never a new query or a status literal in SQL. Note `success_statuses`/`excluded_statuses` (payment_transactions, legacy) and `payout_success_statuses`/`payout_excluded_statuses` (payout_logs, live) are deliberately separate vocabularies. A few per-provider keys go beyond success and failure:
- `reason_excluded_statuses` / `excluded_comparison_values` drop non-diagnostic rows from the reason and channel breakdowns while still counting them in `total_resolved`. For razorpay these are `Subscription_Created` and the `deposite` channel, both of which mean the user clicked but never picked a payment method.
- `payout_excluded_statuses: {None}` for razorpay drops the null-status placeholder rows that are created before the user clicks.
- Razorpay's payout_logs `error_message` is always `'Success'`, so its reasons are derived from `status` instead.
- `comparison_label` sets the key name in the API response: razorpay products return `channels`, everything else returns `operators`. See `api.py`.

**`compare_to_previous` is the switch for the whole comparison concept.** Only `rollup.merge_with_previous()` sets it. When false, [analyze.py](analyze.py)'s `_service_payload()` omits the `previous_*` keys entirely (not null) and `_build_system_prompt()` never mentions period-over-period at all — so Claude can't write "there's no prior week to compare against" for a request that was never about weeks.

**Claude never produces a number the user sees as data.** All figures come from extract/rollup/providers; `analyze.py`'s prompt forbids restating them, and `email_template.py` renders every number itself. `notable_days`/`notable_hours` are the one crossover: they're Claude's own list of dates it discussed, used to color the chart bars, so the chart and the prose always point at the same days.

**`/chat` has no tools, by construction.** [chat.py](chat.py) sends the session's stored `context_data` plus text and gets text back — nothing is re-queried live, and the user's message only ever reaches a parameterized log insert and the Claude message body. Keep it that way; on-topic-ness and answer length are the only prompt-level (non-architectural) constraints there.

**Token logging needs all four fields.** With prompt caching on (`cache_control: ephemeral` on both system prompts), `input_tokens` alone badly understates cost — `cache_creation_input_tokens` and `cache_read_input_tokens` carry the bulk. Every Claude call logs to the single `agent_chat_logs` table via `chat_store.log_message()`, distinguished by `source` (`weekly_email` | `dashboard_summary` | `chat`), each populating `session_id`/`uid`/`cp_id` differently.

**Compaction applies to `hourly` everywhere, `daily` only in analyze.** [compact.py](compact.py)'s `to_compact_table()` replaces repeated field names with a CSV-style table. `chat.py` deliberately leaves `daily` alone: `context_data`'s daily entries carry per-day `reasons`/`operators`, which a flat numeric table cannot represent. Compacting it there would silently break day-specific questions.

**`/summary` cache: check after auth, key by uid only when needed, never cache the session.** In [api.py](api.py), the cache lookup runs *after* `authenticate()`/`resolve_cp_product_ids()`, so a cache hit can never bypass authorization. `summary_cache.build_key()` puts `uid` in the key **only when `cp_product_id` is omitted**, because "everything this uid can see" is different for each partner and leaving uid out would leak data between partners. When an explicit product is given, ownership has already been checked, so one entry can be shared. Only the summary data is cached, never `session_id`. Every request, hit or miss, creates its own fresh chat session, so the question limit and the session TTL are never shared between unrelated users. If a range includes today, its TTL is `SUMMARY_CACHE_TTL_TODAY_SECONDS` (4h) instead of `SUMMARY_CACHE_TTL_SECONDS` (24h). Any Redis error degrades to a cache miss or a skipped write and never fails the request.

**Aggregator names are scrubbed at exactly one place: the API response boundary.** [response_sanitizer.py](response_sanitizer.py) `sanitize_aggregator_names()` replaces pawapay, ampere, razorpay, thirdpay and thirdparty (case-insensitive) with "DTPay". It runs on a copy, right before `/summary` and `/chat` return. Everything upstream must keep the real names:
- `PROVIDER_RULES` is keyed by them.
- `chat.py`'s prompt relies on the provider to explain whether "operator" means a network or a channel.
- The Redis cache and `context_data` are read again later, so they must stay unscrubbed.
- `agent_chat_logs` is an internal audit trail.

Don't move sanitizing earlier. The weekly email does not go through this sanitizer.

**`_clean_rows()` in extract.py converts pymysql `Decimal`s to int/float** right after fetch — `json.dumps()` in `analyze.py` can't serialize `Decimal`, so anything bypassing that cleanup will fail downstream.

## Debugging

- `LOG_CLAUDE_PROMPTS=true` (then restart) writes every full system prompt, message payload, and raw response to `agent.log`. Off by default — payloads run to several KB per call.
- A hung request: `SHOW FULL PROCESSLIST;` against MySQL while it hangs distinguishes the DB side from the Anthropic side. All DB connections and Claude calls are bounded by `DB_CONNECT_TIMEOUT_SECONDS` / `DB_READ_TIMEOUT_SECONDS` / `CLAUDE_TIMEOUT_SECONDS`.
- Stale or unexpected `/summary` output: it may be a cache hit. Delete the `DTPAY-AGENT-summary:*` key or wait for its TTL. A Redis outage shows up only as every request being a miss.
- A failed Claude call is caught per-partner in `analyze_partner()` — the digest comes back with empty summary/recommendations rather than aborting the run, and the reason is in `agent.log`.

## Config

Everything tunable is an env var read in [config.py](config.py) (`MAX_DATE_RANGE_DAYS`, `MAX_QUESTIONS_PER_SESSION`, `MIN_RESOLVED_THRESHOLD`, timeouts, `REVIEW_MODE`, `ALLOWED_ORIGINS` for CORS, `REDIS_*` including `REDIS_KEY_PREFIX` (default `DTPAY-AGENT-`), and the two `SUMMARY_CACHE_TTL_*` values) — changing a limit means editing `.env` and restarting, not a code change. Two limits that sound alike but aren't: `MAX_QUESTIONS_PER_SESSION` (config.py) hard-blocks a 6th question with a 429; `CHAT_HISTORY_LIMIT` (chat_store.py) only trims how many past rows get resent to Claude, and never blocks anything. Two more live in code, not env: `ON_DEMAND_MIN_RESOLVED` and `ON_DEMAND_MAX_HOURLY_DAYS` in [api.py](api.py), and `SESSION_TTL_HOURS` in [chat_store.py](chat_store.py).

Model is `claude-sonnet-5` in both [analyze.py](analyze.py) and [chat.py](chat.py). `analyze.py` uses structured outputs (`output_config` + `json_schema`) via `client.beta.messages.create` with the `structured-outputs-2025-11-13` beta header.

## Repo notes

Large sample exports (`*.csv`, `*.xlsx`, ~340MB) sit in the project root and are gitignored, as are `.env`, `review_output/`, and `agent.log`. Auth ([auth.py](auth.py)) checks the bearer token against a non-expired `users_jwt_tokens` row for that exact `uid`; it does not verify the JWT cryptographically. The README documents the reasoning behind most design decisions here at length and is worth reading before changing behavior — especially "What's verified vs. still open" and "Known gap" (a service with previous-week activity but none this week is silently dropped from the email).
