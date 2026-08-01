"""
Database access for the "Any Questions?" chat feature and the unified
token/conversation log.

Two tables (DDL in README.md - create them before using this):

agent_chat_sessions - one row per /summary call from the dashboard.
Stores the exact grounding data that summary was built from
(context_data), so a later /chat call answers from the same numbers
the user already saw, not a fresh query that might have moved on.
Expires 24h after creation (SESSION_TTL_HOURS).

agent_chat_logs - one row per Claude call, across all three sources
(weekly_email, dashboard_summary, chat) with a source column so token
usage can be broken down by which feature it came from. Chat turns
log twice (a 'user' row for the question, an 'assistant' row for the
reply); weekly_email/dashboard_summary log once per Claude call
(role='assistant', message=NULL - there's no user-typed text on those
paths, only structured JSON in and structured JSON out).

count_user_messages() enforces MAX_QUESTIONS_PER_SESSION (config.py) -
a hard cap on how many questions one session allows, checked in
api.py before a new question is processed at all. This is a different
knob from CHAT_HISTORY_LIMIT below: that one controls how many past
rows get resent to Claude as context on each turn (trims history);
this one blocks further questions entirely once the count is reached.
"""
import json
from datetime import datetime, timedelta, timezone

from extract import get_connection

SESSION_TTL_HOURS = 24

# How many of the most recent agent_chat_logs rows for a session get
# resent to Claude as conversation history on the next chat turn -
# not the total number of questions a session can ever ask, just how
# much history is replayed each time, so a long back-and-forth
# doesn't grow the request without bound.
CHAT_HISTORY_LIMIT = 5


def create_session(session_id, uid, start_date, end_date, cp_product_id, context_data):
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=SESSION_TTL_HOURS)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_chat_sessions "
                "(id, uid, start_date, end_date, cp_product_id, context_data, created_at, expires_at) "
                "VALUES (%(id)s, %(uid)s, %(start_date)s, %(end_date)s, %(cp_product_id)s, %(context_data)s, %(created_at)s, %(expires_at)s)",
                {
                    "id": session_id,
                    "uid": uid,
                    "start_date": start_date,
                    "end_date": end_date,
                    "cp_product_id": cp_product_id,
                    "context_data": json.dumps(context_data),
                    "created_at": now,
                    "expires_at": expires_at,
                },
            )
        conn.commit()
    return session_id


def get_session(session_id):
    """Returns {uid, context_data, expires_at} or None if the session
    doesn't exist. Does not check expiry itself - callers compare
    expires_at against the current time so the caller controls what
    "expired" means for its own error response."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT uid, context_data, expires_at FROM agent_chat_sessions WHERE id = %(id)s",
                {"id": session_id},
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "uid": row["uid"],
        "context_data": json.loads(row["context_data"]),
        "expires_at": row["expires_at"],
    }


def log_message(source, role, session_id=None, uid=None, cp_id=None, message=None,
                 input_tokens=None, output_tokens=None):
    """source: 'weekly_email' | 'dashboard_summary' | 'chat'. role:
    'user' | 'assistant'. The user's message text (chat only) is
    stored exactly as typed and only ever used as a string here - see
    chat.py for why that's a guarantee, not just a description."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_chat_logs "
                "(session_id, uid, cp_id, source, role, message, input_tokens, output_tokens, created_at) "
                "VALUES (%(session_id)s, %(uid)s, %(cp_id)s, %(source)s, %(role)s, %(message)s, %(input_tokens)s, %(output_tokens)s, %(created_at)s)",
                {
                    "session_id": session_id,
                    "uid": uid,
                    "cp_id": cp_id,
                    "source": source,
                    "role": role,
                    "message": message,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "created_at": datetime.now(timezone.utc),
                },
            )
        conn.commit()


def get_recent_messages(session_id, limit=CHAT_HISTORY_LIMIT):
    """Last `limit` chat rows (role/message only) for this session,
    oldest first - ready to hand straight to chat.py as conversation
    history."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role, message FROM agent_chat_logs "
                "WHERE session_id = %(session_id)s AND source = 'chat' "
                "ORDER BY id DESC LIMIT %(limit)s",
                {"session_id": session_id, "limit": limit},
            )
            rows = cur.fetchall()
    return list(reversed(rows))


def count_user_messages(session_id):
    """How many questions have already been asked in this session -
    used to enforce MAX_QUESTIONS_PER_SESSION (config.py). Counts only
    role='user' rows, so an assistant reply never counts toward the
    limit, and only ever counts rows that were actually logged - a
    request rejected for being over the cap is never logged as a
    'user' row in the first place, so it can't itself push the count
    up on a retry."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM agent_chat_logs "
                "WHERE session_id = %(session_id)s AND source = 'chat' AND role = 'user'",
                {"session_id": session_id},
            )
            return cur.fetchone()["n"]