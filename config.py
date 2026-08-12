"""
DB connection config. All values come from the environment so real
credentials never live in source control — see .env.example.
"""
import os

from dotenv import load_dotenv

load_dotenv()  # reads .env in the project root into the environment

DB_CONNECT_TIMEOUT_SECONDS = int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "10"))
DB_READ_TIMEOUT_SECONDS = int(os.environ.get("DB_READ_TIMEOUT_SECONDS", "30"))
CLAUDE_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "60"))

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

MIN_RESOLVED_THRESHOLD = int(os.environ.get("MIN_RESOLVED_THRESHOLD", "100"))
MAX_DATE_RANGE_DAYS = int(os.environ.get("MAX_DATE_RANGE_DAYS", "7"))
MAX_QUESTIONS_PER_SESSION = int(os.environ.get("MAX_QUESTIONS_PER_SESSION", "5"))

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM", "noreply@dtpay.example")

REVIEW_MODE = os.environ.get("REVIEW_MODE", "true").lower() == "true"
REVIEW_OUTPUT_DIR = os.environ.get("REVIEW_OUTPUT_DIR", "./review_output")

LOG_CLAUDE_PROMPTS = os.environ.get("LOG_CLAUDE_PROMPTS", "false").lower() == "true"

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]

# Redis cache for /summary responses - see summary_cache.py for the
# key design and why session_id is deliberately excluded from what
# gets cached.
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None
# Namespaces every key this app writes - matters once this Redis
# instance is shared with other services, so a key this app uses can
# never collide with an unrelated key some other service happens to
# use too.
REDIS_KEY_PREFIX = os.environ.get("REDIS_KEY_PREFIX", "DTPAY-AGENT-")

# Two different TTLs on purpose: a range that includes today is still
# genuinely moving (transactions keep resolving throughout the day -
# confirmed directly a few messages back, comparing our numbers
# against the dashboard at two different query times), so it gets a
# much shorter cache life than a fully historical range, which
# shouldn't change anymore once the underlying transactions have
# settled.
SUMMARY_CACHE_TTL_SECONDS = int(os.environ.get("SUMMARY_CACHE_TTL_SECONDS", str(24 * 60 * 60)))       # 24h
SUMMARY_CACHE_TTL_TODAY_SECONDS = int(os.environ.get("SUMMARY_CACHE_TTL_TODAY_SECONDS", str(4 * 60 * 60)))  # 4h