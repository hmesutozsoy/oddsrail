"""Magic-link delivery. With ODDSRAIL_RESEND_API_KEY set the link goes out
through Resend from ODDSRAIL_MAIL_FROM; without it the link is printed to
the server log, which is how local development and tests run."""

from __future__ import annotations

import os
import sys

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


def configured() -> bool:
    return bool(os.environ.get("ODDSRAIL_RESEND_API_KEY"))


async def send_magic_link(email: str, url: str) -> str:
    """Deliver the link. Returns "sent" (email went out) or "console"."""
    key = os.environ.get("ODDSRAIL_RESEND_API_KEY")
    if not key:
        print(f"[oddsrail-cloud] sign-in link for {email}: {url}", file=sys.stderr, flush=True)
        return "console"
    sender = os.environ.get("ODDSRAIL_MAIL_FROM", "oddsrail <login@oddsrail.app>")
    body = {"from": sender, "to": [email], "subject": SUBJECT,
            "text": TEXT.format(url=url), "html": HTML.format(url=url)}
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post("https://api.resend.com/emails", json=body,
                         headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
    return "sent"
