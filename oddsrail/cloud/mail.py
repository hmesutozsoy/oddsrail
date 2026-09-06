"""Magic-link delivery.

Two ways out, checked in this order:
  ODDSRAIL_RESEND_API_KEY              Resend's HTTP API (sender ODDSRAIL_MAIL_FROM,
                                       a verified domain there)
  ODDSRAIL_SMTP_HOST / _PORT / _USER /
  _PASSWORD                            any SMTP server, e.g. Gmail with an app
                                       password (smtp.gmail.com:587, STARTTLS)
Without either, the link is printed to the server log, which is how local
development and tests run and how the maintainer can sign in before mail
delivery is set up.
"""

from __future__ import annotations

import asyncio
import os
import smtplib
import sys
from email.message import EmailMessage

import httpx

SUBJECT = "Sign in to oddsrail"
TEXT = """Sign in to oddsrail by opening this link (valid for 15 minutes):

{url}

If you did not request this, ignore this email. Nothing happens unless the
link is opened and confirmed.
"""
HTML = """<p>Sign in to oddsrail by opening this link (valid for 15 minutes):</p>
<p><a href="{url}">{url}</a></p>
<p style="color:#777">If you did not request this, ignore this email. Nothing
happens unless the link is opened and confirmed.</p>
"""


def mode() -> str:
    if os.environ.get("ODDSRAIL_RESEND_API_KEY"):
        return "resend"
    if os.environ.get("ODDSRAIL_SMTP_HOST") and os.environ.get("ODDSRAIL_SMTP_USER"):
        return "smtp"
    return "console"


def configured() -> bool:
    return mode() != "console"


def sender() -> str:
    return os.environ.get("ODDSRAIL_MAIL_FROM") or os.environ.get("ODDSRAIL_SMTP_USER") \
        or "oddsrail <login@oddsrail.app>"


def _smtp_send(email: str, url: str) -> None:
    host = os.environ["ODDSRAIL_SMTP_HOST"]
    port = int(os.environ.get("ODDSRAIL_SMTP_PORT", "587"))
    user = os.environ["ODDSRAIL_SMTP_USER"]
    password = os.environ.get("ODDSRAIL_SMTP_PASSWORD", "")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender(), email, SUBJECT
    msg.set_content(TEXT.format(url=url))
    msg.add_alternative(HTML.format(url=url), subtype="html")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.ehlo()
            s.starttls()
            s.login(user, password)
            s.send_message(msg)


async def send_magic_link(email: str, url: str) -> str:
    """Deliver the link. Returns "resend", "smtp" or "console"."""
    m = mode()
    if m == "console":
        print(f"[oddsrail-cloud] sign-in link for {email}: {url}", file=sys.stderr, flush=True)
        return m
    if m == "smtp":
        await asyncio.to_thread(_smtp_send, email, url)
        return m
    body = {"from": sender(), "to": [email], "subject": SUBJECT,
            "text": TEXT.format(url=url), "html": HTML.format(url=url)}
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post("https://api.resend.com/emails", json=body,
                         headers={"Authorization": f"Bearer {os.environ['ODDSRAIL_RESEND_API_KEY']}"})
        r.raise_for_status()
    return m
