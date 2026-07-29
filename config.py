"""
DB connection config. All values come from the environment so real
credentials never live in source control — see .env.example.
"""
import os

from dotenv import load_dotenv

load_dotenv()  # reads .env in the project root into the environment

DB_CONFIG = {
    "host": os.environ.get("DTPAY_DB_HOST", "localhost"),
    "port": int(os.environ.get("DTPAY_DB_PORT", "3306")),
    "user": os.environ.get("DTPAY_DB_USER"),
    "password": os.environ.get("DTPAY_DB_PASSWORD"),
    "database": os.environ.get("DTPAY_DB_NAME"),
    "charset": "utf8mb4",
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Services with fewer resolved transactions than this in a given week
# are excluded before Claude or the email ever see them - a handful of
# hits produces a meaningless conversion rate either way.
MIN_RESOLVED_THRESHOLD = int(os.environ.get("MIN_RESOLVED_THRESHOLD", "100"))

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