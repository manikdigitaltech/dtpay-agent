"""
DB connection config. All values come from the environment so real
credentials never live in source control — see .env.example.
"""
import os

from dotenv import load_dotenv

load_dotenv()  # reads .env in the project root into the environment

# Nothing in this project had any timeout, anywhere, until a real
# request hung for 10+ minutes with no response and no error - a slow
# network to the DB or to Anthropic's API had no way to fail fast, it
# could only hang forever. These bound every DB connection and every
# Claude call to a fixed maximum wait, so the failure mode becomes a
# clear, fast error instead of an unresponsive server.
DB_CONNECT_TIMEOUT_SECONDS = int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "10"))
DB_READ_TIMEOUT_SECONDS = int(os.environ.get("DB_READ_TIMEOUT_SECONDS", "30"))
CLAUDE_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "60"))

# When true, every Claude call (weekly email, dashboard summary, chat)
# logs its full system prompt and message payload to agent.log before
# sending, and the raw response text after - off by default since a
# payload can run to several KB per call (especially with hourly data
# included) and isn't something you want written on every request in
# normal operation, only while actively debugging.
LOG_CLAUDE_PROMPTS = os.environ.get("LOG_CLAUDE_PROMPTS", "false").lower() == "true"

DB_CONFIG = {
    "host": os.environ.get("DTPAY_DB_HOST", "localhost"),
    "port": int(os.environ.get("DTPAY_DB_PORT", "3306")),
    "user": os.environ.get("DTPAY_DB_USER"),
    "password": os.environ.get("DTPAY_DB_PASSWORD"),
    "database": os.environ.get("DTPAY_DB_NAME"),
    "charset": "utf8mb4",
    "connect_timeout": DB_CONNECT_TIMEOUT_SECONDS,
    "read_timeout": DB_READ_TIMEOUT_SECONDS,
    "write_timeout": DB_READ_TIMEOUT_SECONDS,
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Services with fewer resolved transactions than this in a given week
# are excluded before Claude or the email ever see them - a handful of
# hits produces a meaningless conversion rate either way.
MIN_RESOLVED_THRESHOLD = int(os.environ.get("MIN_RESOLVED_THRESHOLD", "100"))

# The on-demand API rejects any request spanning more than this many
# days (inclusive of both start_date and end_date) rather than
# processing it - change by updating this env var and restarting the
# API process, no code change or redeploy needed.
MAX_DATE_RANGE_DAYS = int(os.environ.get("MAX_DATE_RANGE_DAYS", "7"))

# Hard cap on how many questions a user may ask in one chat session -
# the 5th question is still answered, a 6th attempt is rejected
# outright before it reaches Claude. Distinct from CHAT_HISTORY_LIMIT
# in chat_store.py, which controls how many past rows get resent to
# Claude as context on each turn - that one trims history, this one
# blocks further messages entirely once reached.
MAX_QUESTIONS_PER_SESSION = int(os.environ.get("MAX_QUESTIONS_PER_SESSION", "5"))

SMTP_CONFIG = {
    "host": os.environ.get("SMTP_HOST", "localhost"),
    "port": int(os.environ.get("SMTP_PORT", "587")),
    "user": os.environ.get("SMTP_USER"),
    "password": os.environ.get("SMTP_PASSWORD"),
    "from_address": os.environ.get("SMTP_FROM", "noreply@dtpay.example"),
}

# Defaults to True on purpose - the first runs should write emails
# somewhere a human can read them, not send to real partners. Flip to
# "false" only once you've eyeballed a few days of review output.
REVIEW_MODE = os.environ.get("REVIEW_MODE", "true").lower() == "true"
REVIEW_OUTPUT_DIR = os.environ.get("REVIEW_OUTPUT_DIR", "./review_output")