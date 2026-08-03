# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python service that turns DTPay payment-transaction data into partner-facing performance analysis, via two entrypoints that share one pipeline:

1. **Scheduled weekly email** — [main.py](main.py) `run_weekly()`, one HTML digest per partner covering the last completed Mon–Sun week vs. the week before.
2. **On-demand dashboard API** — [api.py](api.py), `POST /summary` (arbitrary date range, JSON out) and `POST /chat` (follow-up Q&A grounded in that summary's stored data).

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in DTPAY_DB_*, ANTHROPIC_API_KEY, SMTP_*

python main.py                                    # run the weekly job once
uvicorn api:app --host 0.0.0.0 --port 8000        # run the on-demand API
```

There is no test suite, linter, or CI config in this repo. `compact.from_compact_table()` exists only for round-trip verification and is not called by application code — verification described in the README was done ad hoc, not as committed tests.

`REVIEW_MODE=true` (the default) writes each partner email to `review_output/` instead of sending it. Two DB tables (`agent_chat_sessions`, `agent_chat_logs`) must be created before either API endpoint works — DDL is in [README.md](README.md).

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
| output | HTML via `email_template` → `email_sender` | JSON + `session_id` |

`fetch_all(day_start, day_end, cp_product_ids, ...)` does **not** resolve scope itself — callers pass `cp_product_ids` in explicitly, because the two eligibility rules are unrelated. It returns a 7-tuple: `(product_metrics, reason_breakdown, operator_breakdown, daily_metrics, hourly_metrics, daily_reason_breakdown, daily_operator_breakdown)`; the last four are empty lists unless the matching `include_*` flag is set.

## Architectural rules that are easy to break

**Provider knowledge lives only in [providers.py](providers.py).** SQL in [extract.py](extract.py) fetches raw counts grouped by status/operator/channel and decides nothing about success. `PROVIDER_RULES` is where "COMPLETED means success for pawapay, AUTHORIZED for razorpay", which statuses are excluded from the denominator, and which field (`operator` vs `channel`) is the meaningful comparison axis all live. Adding a provider is a new dict entry — never a new query or a status literal in SQL. Note `success_statuses` (payment_transactions) and `payout_success_statuses` (payout_logs) are deliberately separate vocabularies.

**`compare_to_previous` is the switch for the whole comparison concept.** Only `rollup.merge_with_previous()` sets it. When false, [analyze.py](analyze.py)'s `_service_payload()` omits the `previous_*` keys entirely (not null) and `_build_system_prompt()` never mentions period-over-period at all — so Claude can't write "there's no prior week to compare against" for a request that was never about weeks.

**Claude never produces a number the user sees as data.** All figures come from extract/rollup/providers; `analyze.py`'s prompt forbids restating them, and `email_template.py` renders every number itself. `notable_days`/`notable_hours` are the one crossover: they're Claude's own list of dates it discussed, used to color the chart bars, so the chart and the prose always point at the same days.

**`/chat` has no tools, by construction.** [chat.py](chat.py) sends the session's stored `context_data` plus text and gets text back — nothing is re-queried live, and the user's message only ever reaches a parameterized log insert and the Claude message body. Keep it that way; on-topic-ness and answer length are the only prompt-level (non-architectural) constraints there.

**Token logging needs all four fields.** With prompt caching on (`cache_control: ephemeral` on both system prompts), `input_tokens` alone badly understates cost — `cache_creation_input_tokens` and `cache_read_input_tokens` carry the bulk. Every Claude call logs to the single `agent_chat_logs` table via `chat_store.log_message()`, distinguished by `source` (`weekly_email` | `dashboard_summary` | `chat`), each populating `session_id`/`uid`/`cp_id` differently.

**Compaction applies to `hourly` everywhere, `daily` only in analyze.** [compact.py](compact.py)'s `to_compact_table()` replaces repeated field names with a CSV-style table. `chat.py` deliberately leaves `daily` alone: `context_data`'s daily entries carry per-day `reasons`/`operators`, which a flat numeric table cannot represent. Compacting it there would silently break day-specific questions.

**`_clean_rows()` in extract.py converts pymysql `Decimal`s to int/float** right after fetch — `json.dumps()` in `analyze.py` can't serialize `Decimal`, so anything bypassing that cleanup will fail downstream.

## Debugging

- `LOG_CLAUDE_PROMPTS=true` (then restart) writes every full system prompt, message payload, and raw response to `agent.log`. Off by default — payloads run to several KB per call.
- A hung request: `SHOW FULL PROCESSLIST;` against MySQL while it hangs distinguishes the DB side from the Anthropic side. All DB connections and Claude calls are bounded by `DB_CONNECT_TIMEOUT_SECONDS` / `DB_READ_TIMEOUT_SECONDS` / `CLAUDE_TIMEOUT_SECONDS`.
- A failed Claude call is caught per-partner in `analyze_partner()` — the digest comes back with empty summary/recommendations rather than aborting the run, and the reason is in `agent.log`.

## Config

Everything tunable is an env var read in [config.py](config.py) (`MAX_DATE_RANGE_DAYS`, `MAX_QUESTIONS_PER_SESSION`, `MIN_RESOLVED_THRESHOLD`, timeouts, `REVIEW_MODE`) — changing a limit means editing `.env` and restarting, not a code change. Two limits that sound alike but aren't: `MAX_QUESTIONS_PER_SESSION` (config.py) hard-blocks a 6th question with a 429; `CHAT_HISTORY_LIMIT` (chat_store.py) only trims how many past rows get resent to Claude, and never blocks anything. Two more live in code, not env: `ON_DEMAND_MIN_RESOLVED` and `ON_DEMAND_MAX_HOURLY_DAYS` in [api.py](api.py), and `SESSION_TTL_HOURS` in [chat_store.py](chat_store.py).

Model is `claude-sonnet-5` in both [analyze.py](analyze.py) and [chat.py](chat.py). `analyze.py` uses structured outputs (`output_config` + `json_schema`) via `client.beta.messages.create` with the `structured-outputs-2025-11-13` beta header.

## Repo notes

Large sample exports (`*.csv`, `*.xlsx`, ~340MB) sit in the project root and are gitignored, as are `.env`, `review_output/`, and `agent.log`. The README documents the reasoning behind most design decisions here at length and is worth reading before changing behavior — especially "What's verified vs. still open" and "Known gap" (a service with previous-week activity but none this week is silently dropped from the email).
