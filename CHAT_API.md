# Chat API — integration guide

How to call `POST /chat` (the "Any Questions?" popup) exactly as
[api.py](api.py) and [chat.py](chat.py) implement it today.

`/chat` cannot be used on its own: every question is answered against a
**session** created by `POST /summary`, so the flow is always
`/summary` → keep the `session_id` → `/chat` (up to 5 times).

Base URL is wherever the service is running —
`uvicorn api:app --host 0.0.0.0 --port 8000`.

---

## 1. Create a session with `POST /summary`

```http
POST /summary
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "uid": 122,
  "start_date": "2026-07-28",
  "end_date": "2026-08-01",
  "cp_product_id": null,
  "campaign_id": null
}
```

| field | type | required | notes |
|---|---|---|---|
| `uid` | int | yes | must match the JWT's own uid row |
| `start_date` | date `YYYY-MM-DD` | yes | inclusive |
| `end_date` | date `YYYY-MM-DD` | yes | inclusive |
| `cp_product_id` | int \| null | no | omit for "everything this uid may see" |
| `campaign_id` | string \| null | no | narrows the whole analysis to one campaign of `cp_product_id`. Requires `cp_product_id` (else `400`); unknown for that product → `404`. Integers are accepted and treated as strings |

The range is inclusive of both ends and may not exceed
`MAX_DATE_RANGE_DAYS` (default 7) — otherwise `400`.

Response (trimmed):

```json
{
  "session_id": "2fda4486-f5b2-4d6a-a649-ecdb0793a4e6",
  "date_range": { "start": "2026-07-28", "end": "2026-08-01" },
  "campaign": null,
  "overall": { "total_resolved": 18432, "completed": 14120, "conversion_rate_pct": 76.6 },
  "products": [ { "product_id": 41, "product_name": "...", "summary": "...", "daily": [], "hourly": [] } ],
  "generated_at": "2026-08-03T09:41:22.104512+00:00"
}
```

Each product also carries `campaign_overview` (string) and `campaigns`
(array). Both are filled in only when no `campaign_id` was sent and the
product ran **two or more** campaigns in the range. Otherwise they are `""`
and `[]`:

```json
"campaign_overview": "Monthly(150) carries almost all the volume ...",
"campaigns": [
  { "campaign_id": "mFTuVimSMRzg2UjGI5u7", "campaign_name": "DTPay Learn2BFit  Monthly(150)",
    "total_resolved": 10718, "completed": 1001, "conversion_rate_pct": 9.34,
    "summary": "This campaign represents nearly the entire service's volume ...",
    "reasons": [ { "reason": "request_timed_out", "occurrences": 4120 } ],
    "channels": [ { "channel": "upi", "attempts": 3100, "ok": 999 } ] }
]
```

Campaigns are sorted by `total_resolved` (largest first) and add up exactly
to the product's totals. The comparison key is `channels` or `operators`,
the same as at product level. `campaign_name` can be `null`: either the
campaign has no name stored, or `campaign_id` is `null` too, meaning
traffic with no campaign. Fall back to `campaign_id` for display.

Every product, and every entry in `campaigns`, also carries `placements`
and `devices`. These come from the Google Ads click and are `null` when
none of that traffic has the value. Capture started on 12 Oct 2026, so
older ranges are `null` everywhere.

```json
"placements": {
  "tracked_resolved": 10216,
  "by_type": [ { "type": "mobile app", "total_resolved": 8697, "completed": 263, "conversion_rate_pct": 3.02, "count": 1185 },
               { "type": "website",    "total_resolved": 1519, "completed": 28,  "conversion_rate_pct": 1.84, "count": 433 } ],
  "top": [ { "placement": "mobileapp::2-com.gpstracker.misky.phone", "total_resolved": 288, "completed": 14, "conversion_rate_pct": 4.86 },
           { "placement": "(other placements)", "total_resolved": 8473, "completed": 241, "conversion_rate_pct": 2.84, "count": 1608 } ]
},
"devices": {
  "tracked_resolved": 10159,
  "by_brand": [ { "brand": "tecno", "total_resolved": 3419, "completed": 96, "conversion_rate_pct": 2.81 },
                { "brand": "(other brands)", "...": "..." } ]
}
```

- `tracked_resolved` is how much of the unit's `total_resolved` has the value
  at all. Show it as coverage when it's lower.
- `top` lists up to 10 placements with at least 30 resolved each. Everything
  else is in the final `(other placements)` row, where `count` is how many
  placements that row covers.
- `by_brand` lists the top 8 device brands (the part of `device` before `+`)
  plus `(other brands)`.
- `by_type`, `top` and `by_brand` each add up to `tracked_resolved`.

`campaign` is `null` unless `campaign_id` was sent, in which case it is
`{"campaign_id": "...", "campaign_name": "..."}` and every number in the
response covers that campaign only. Display `campaign_name`.

**Store `session_id`.** A session is created on every `/summary` call
whether or not the user ever opens the chat, and it is the only handle
`/chat` accepts. Its stored grounding data is exactly the `overall` +
`products` payload above — the chat answers from that frozen snapshot,
never from a fresh query.

---

## 2. Ask a question with `POST /chat`

```http
POST /chat
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "uid": 122,
  "session_id": "2fda4486-f5b2-4d6a-a649-ecdb0793a4e6",
  "message": "Why did DRC dip on the 25th?"
}
```

| field | type | required | notes |
|---|---|---|---|
| `uid` | int | yes | same uid that created the session, or an `ADMIN` |
| `session_id` | string (uuid) | yes | from the `/summary` response |
| `message` | string | yes | the user's question, free text |

All three are required; a missing or wrongly-typed field is a FastAPI
`422`, not a `400`. The `Authorization` header is also required — a
missing header is a `422` as well.

`Authorization` accepts either `Bearer <jwt>` (prefix match is
case-insensitive) or the bare token.

Response — always exactly two fields:

```json
{
  "session_id": "2fda4486-f5b2-4d6a-a649-ecdb0793a4e6",
  "answer": "DRC's conversion fell to 61.2% on the 25th, driven mainly by PAYER_LIMIT_REACHED on Vodacom, which accounted for most of that day's failures."
}
```

`answer` is plain text, deliberately formatted for a popup with no
markdown rendering: one continuous paragraph, no line breaks, no
bullets or bold, typically 1–2 sentences. Render it as-is; don't run it
through a markdown renderer.

### curl

```bash
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"uid":122,"session_id":"2fda4486-f5b2-4d6a-a649-ecdb0793a4e6","message":"Which product had the worst day?"}'
```

### JavaScript

```js
async function askQuestion(sessionId, message) {
  const res = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${jwt}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ uid, session_id: sessionId, message }),
  });

  if (!res.ok) {
    const { detail } = await res.json();   // FastAPI error shape
    throw new Error(detail);               // see the status table below
  }

  const { answer } = await res.json();
  return answer;
}
```

---

## Status codes

Checks run in this order, so the first one that fails is the status you
get:

| status | when | `detail` |
|---|---|---|
| `200` | answered (**including** the Claude-failure fallback — see below) | — |
| `401` | `(uid, jwt)` isn't an unexpired row in `users_jwt_tokens` | `Invalid or expired credentials` |
| `401` | `uid` has no `dtpay_users` row | `Unknown user` |
| `404` | no session with that `session_id` | `Session not found` |
| `410` | session older than 24h | `Session expired` |
| `403` | session belongs to a different uid and requester isn't `ADMIN` | `This session does not belong to this user` |
| `429` | 5 questions already asked in this session | `This session has reached its limit of 5 questions. Generate a new summary to keep asking questions.` |
| `422` | missing/invalid body field or missing `Authorization` header | FastAPI validation array |

Errors use FastAPI's standard `{"detail": "..."}` body. Surface
`detail` directly for `410`/`403`/`429` — those messages are written to
be user-readable.

**A `410` or `429` means "start over":** call `/summary` again for the
same date range to mint a fresh `session_id`, then keep chatting.

### The fallback answer is a `200`

If the Claude call fails or times out (`CLAUDE_TIMEOUT_SECONDS`,
default 60s), `/chat` still returns `200` with

> `Sorry, I couldn't process that question just now - please try again.`

as the `answer`, rather than a 5xx — a failed answer shouldn't look
like a server error to someone typing in a popup. The reason is in
`agent.log`. Note the consequence for the client: **that attempt still
counted** toward the 5-question limit, because the user message is
logged before the Claude call. Don't auto-retry in a loop.

Unhandled database failures are not caught and will surface as `500`.

---

## Limits and lifecycle

| limit | value | where | effect |
|---|---|---|---|
| session TTL | 24h from `/summary` | `SESSION_TTL_HOURS`, [chat_store.py](chat_store.py) | `410` after that |
| questions per session | 5 | `MAX_QUESTIONS_PER_SESSION`, [config.py](config.py) | 5th is answered, 6th is `429` |
| history resent to Claude | last 5 log rows | `CHAT_HISTORY_LIMIT`, [chat_store.py](chat_store.py) | trims context only, never blocks |
| answer length | 1024 max tokens | `chat.py` | prompt targets 1–2 sentences |

The two "5"s are unrelated. `MAX_QUESTIONS_PER_SESSION` counts
`role='user'` rows and hard-blocks; `CHAT_HISTORY_LIMIT` counts *log
rows* (each turn writes 2 — the question and the answer), so roughly
the last 2–3 exchanges are replayed as conversation context. Earlier
turns silently drop out of Claude's view; they are never sent again.

Only `MAX_QUESTIONS_PER_SESSION` is an env var — change it in `.env`
and restart. The other three are constants in code.

---

## What the chat can and can't answer

The session's stored data is the *only* source of truth for the answer.
That means it can answer:

- anything about the products, date range, and overall numbers in the
  `/summary` response that created the session
- per-day questions ("what were the errors on the 25th?") — each entry
  in `daily` carries its own `reasons` and `operators`
- hour-level questions, but only when the summary's range was ≤14 days
  (`ON_DEMAND_MAX_HOURLY_DAYS` in [api.py](api.py)); beyond that
  `hourly` is `null` and there is nothing to answer from
- specific figures stated directly (a rate, a count) — unlike the
  written summary, the chat is allowed to quote numbers

It will decline, in plain language, anything outside that: a different
date range, a product not in the session, general DTPay or payments
knowledge, or a request to change data or take an action.

**There are no tools.** Claude receives the stored `context_data` plus
text and returns text — no function calling, no live query, no write.
The user's message reaches exactly two places: a parameterized insert
into `agent_chat_logs`, and the Claude message body. There is no code
path where it becomes SQL or a shell command.

---

## Client-side notes

- **The API is stateless per request.** There is no endpoint to list
  sessions or replay a transcript — the client must hold the
  `session_id` and render its own message history. `/chat` returns only
  the current answer.
- **Serialize the calls.** Question counting reads `agent_chat_logs`
  before inserting, so firing two questions concurrently on one session
  can slip past the cap or drop an ordering. Wait for each answer.
- **Show the remaining-questions count** if you can — a `429` after
  five is much less surprising when the user has been watching a
  counter.
- **Sessions are per-summary, not per-user.** Changing the dashboard's
  date picker means a new `/summary` call and a new `session_id`; the
  old session keeps answering about the old range until it expires.

---

## Logging

Every turn writes two rows to `agent_chat_logs` with `source='chat'`:
`role='user'` (the question text) and `role='assistant'` (the answer
plus the four token fields — `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`). With prompt
caching on, `input_tokens` alone badly understates cost; sum all the
input fields when reporting usage.

Table DDL for `agent_chat_sessions` and `agent_chat_logs` is in
[README.md](README.md) — both must exist before either endpoint works.

Set `LOG_CLAUDE_PROMPTS=true` and restart to dump the full system
prompt, message payload, and raw response to `agent.log` while
debugging.

