"""Email sign-in for the site itself (the Run tab), next to the OAuth flow
Claude uses.

A visitor who wants to keep an agent enters an email; the link they get
back turns the browser's guest ledger into the account's ledger and hands
the page a bearer session token. Sessions are random, stored hashed, and
last ninety days.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from .auth import LINK_TTL, MAX_LINKS_PER_HOUR, LoginError, _h, _tok
from .db import DB

SESSION_TTL = 90 * 86400
CLAIM_PREFIX = "claim:"


class Accounts:
    def __init__(self, db: DB, base: str, ledgers: Path, guests: Path):
        self.db, self.base, self.ledgers, self.guests = db, base, ledgers, guests

    # -------------------------------- links -------------------------------- #

    def start_claim(self, email: str, guest_id: str) -> str:
        if self.db.recent_links(email, 3600) >= MAX_LINKS_PER_HOUR:
            raise LoginError("Too many sign-in emails were sent to that address in the last hour. "
                             "Open the latest one, or try again later.")
        token = _tok(24)
        self.db.put_magic_link(_h(token), email, CLAIM_PREFIX + guest_id, LINK_TTL)
        return f"{self.base}/claim/verify?t={token}"

    def peek_claim(self, token: str) -> dict | None:
        link = self.db.get_magic_link(_h(token))
        if not link or not str(link["req_id"]).startswith(CLAIM_PREFIX):
            return None
        return {"email": link["email"]}

    def finish_claim(self, token: str) -> dict:
        """Consume the link. Returns {session, email, user_id, moved}."""
        h = _h(token)
        link = self.db.get_magic_link(h)
        if not link or not str(link["req_id"]).startswith(CLAIM_PREFIX):
            raise LoginError("This sign-in link is invalid, already used, or expired. Ask for a new one.")
        if not self.db.use_magic_link(h):
            raise LoginError("This sign-in link was already used.")
        user = self.db.login_user(link["email"])
        guest_id = str(link["req_id"])[len(CLAIM_PREFIX):]
        moved = self.adopt_guest(user["id"], guest_id)
        return {"session": self.new_session(user["id"]), "email": user["email"],
                "user_id": user["id"], "moved": moved}

    def adopt_guest(self, user_id: str, guest_id: str) -> bool:
        """The guest ledger becomes the account's if the account has none
        yet. An account that already traded keeps its own history."""
        src = self.guests / f"{guest_id}.json"
        dst = self.ledgers / f"{user_id}.json"
        if not src.exists() or dst.exists():
            return False
        shutil.move(str(src), str(dst))
        return True

    # ------------------------------ sessions ------------------------------- #

    def new_session(self, user_id: str) -> str:
        token = _tok(32)
        self.db.put_session(_h(token), user_id, SESSION_TTL)
        return token

    def user_for(self, bearer: str | None) -> dict | None:
        if not bearer:
            return None
        s = self.db.get_session(_h(bearer))
        return self.db.user(s["user_id"]) if s else None

    def sign_out(self, bearer: str | None) -> None:
        if bearer:
            self.db.revoke_session(_h(bearer))
