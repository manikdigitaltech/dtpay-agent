"""
Sends (or, in review mode, saves locally instead of sending) the
rendered partner emails. REVIEW_MODE defaults to on in config.py -
switching to real sends is one env var, not a code change.
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import REVIEW_MODE, REVIEW_OUTPUT_DIR, SMTP_CONFIG


def send_partner_email(digest, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your DTPay performance summary - {digest['partner_name']}"
    msg["From"] = SMTP_CONFIG["from_address"]
    msg["To"] = digest["partner_email"]
    msg.attach(MIMEText(html_body, "html"))

    if REVIEW_MODE:
        os.makedirs(REVIEW_OUTPUT_DIR, exist_ok=True)
        path = os.path.join(REVIEW_OUTPUT_DIR, f"{digest['cp_id']}_{digest['partner_email']}.html")
        with open(path, "w") as f:
            f.write(html_body)
        print(f"[review mode] wrote {path} instead of emailing {digest['partner_email']}")
        return

    with smtplib.SMTP(SMTP_CONFIG["host"], SMTP_CONFIG["port"]) as server:
        server.starttls()
        server.login(SMTP_CONFIG["user"], SMTP_CONFIG["password"])
        server.send_message(msg)
        print(f"sent to {digest['partner_email']}")