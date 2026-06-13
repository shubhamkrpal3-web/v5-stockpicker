"""
email_sender.py
===============
Sends an email with an Excel attachment via Gmail SMTP.

Requires env vars:
  EMAIL_FROM            - your gmail address (e.g. shubhamkrpal3@gmail.com)
  EMAIL_TO              - recipient (usually same as FROM for daily reports)
  GMAIL_APP_PASSWORD    - 16-char Google app password (NOT your gmail login password)

How to get a Gmail app password:
  1. https://myaccount.google.com/security
  2. Turn ON 2-Step Verification (if not already on)
  3. Go to https://myaccount.google.com/apppasswords
  4. Pick "Mail" / "Other (Custom name)" → "V5 stockpicker"
  5. Google shows a 16-char password — copy it (only shown once)
  6. Save to GitHub Secrets as GMAIL_APP_PASSWORD

Usage:
  python email_sender.py --subject "V5 Daily Report 2026-06-10" \
                        --body "See attached" \
                        --attach data/reports/V5_Daily_Report_2026-06-10.xlsx
"""
from __future__ import annotations

import argparse
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def send_email(subject: str, body: str, attachment_path: str | None = None,
               from_addr: str | None = None, to_addr: str | None = None) -> None:
    from_addr = from_addr or os.environ.get("EMAIL_FROM")
    to_addr = to_addr or os.environ.get("EMAIL_TO")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not from_addr or not to_addr or not password:
        raise RuntimeError(
            "Missing EMAIL_FROM / EMAIL_TO / GMAIL_APP_PASSWORD env vars."
        )

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    if attachment_path:
        p = Path(attachment_path)
        if not p.exists():
            raise FileNotFoundError(f"Attachment not found: {p}")
        with open(p, "rb") as f:
            data = f.read()
        msg.add_attachment(
            data,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=p.name,
        )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(from_addr, password)
        server.send_message(msg)
    print(f"[INFO] Email sent to {to_addr} (subject: {subject})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body", default="See attached.")
    ap.add_argument("--attach", default=None)
    ap.add_argument("--from-addr", default=None)
    ap.add_argument("--to-addr", default=None)
    args = ap.parse_args()
    send_email(args.subject, args.body, args.attach, args.from_addr, args.to_addr)


if __name__ == "__main__":
    main()
