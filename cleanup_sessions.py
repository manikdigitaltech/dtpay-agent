"""
Deletes expired chat sessions. Nothing else does this automatically -
run it periodically (daily is plenty) via cron or Windows Task
Scheduler, pointed at this project's virtualenv:

    python cleanup_sessions.py

See chat_store.delete_expired_sessions() for exactly what this does
and doesn't touch.
"""
from chat_store import delete_expired_sessions

if __name__ == "__main__":
    deleted = delete_expired_sessions()
    print(f"Deleted {deleted} expired session(s).")