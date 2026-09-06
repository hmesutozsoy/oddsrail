"""The paper arena: hosted accounts that opted in with a display name, ranked
by the return on their paper ledger.

Registration happens from inside the agent's own chat (the arena_register
tool), so an agent can enter itself. The public board is one JSON document
computed at most every few minutes; the site renders it. Nothing here is
real money, and the ranking says so.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from .. import paper
from .db import DB

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{2,23}$")
RESERVED = {"house", "oddsrail", "admin", "maintainer"}
STRATEGY_MAX = 140
BOARD_TTL = 300.0

# Set while the board marks one ledger, so the paper module reads that file
# instead of the request's account.
FORCED_LEDGER = paper.forced_ledger


class ArenaError(Exception):
    """A message for the agent, returned in the tool result."""


def validate_name(name: str) -> str:
    name = " ".join(str(name or "").split())
    if not NAME_RE.match(name):
        raise ArenaError("name must be 3 to 24 characters: letters, digits, spaces, dot, dash or underscore, "
                         "starting with a letter or digit")
    if name.lower() in RESERVED:
        raise ArenaError(f"{name!r} is reserved")
    return name


def register(db: DB, user_id: str, name: str, strategy: str = "") -> dict:
    name = validate_name(name)
    strategy = " ".join(str(strategy or "").split())[:STRATEGY_MAX]
    taken = db.arena_by_name(name)
    if taken and taken["user_id"] != user_id:
        raise ArenaError(f"the name {name!r} is taken; pick another")
    db.arena_put(user_id, name, strategy)
    return {"registered": True, "name": name, "strategy": strategy,
            "board": "https://oddsrail.app/arena",
            "note": "your paper ledger is now public under this name. arena_unregister removes it."}


def unregister(db: DB, user_id: str) -> dict:
    removed = db.arena_delete(user_id)
    return {"registered": False, "removed": removed}


class Board:
    """Computes and caches the public paper board."""

    def __init__(self, db: DB, ledgers_dir: Path, house_ledger: Path | None = None):
        self.db = db
        self.ledgers = ledgers_dir
        self.house = house_ledger
        self._cache: dict | None = None
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """A registration change shows on the next read, not after the TTL."""
        self._cache = None

    async def get(self, force: bool = False) -> dict:
        if not force and self._cache and time.time() - self._cache["computed_at_ts"] < BOARD_TTL:
            return self._cache
        async with self._lock:
            if not force and self._cache and time.time() - self._cache["computed_at_ts"] < BOARD_TTL:
                return self._cache
            self._cache = await self._compute()
            return self._cache

    async def _mark(self, path: Path, readonly: bool = False) -> dict | None:
        """Mark one ledger at current prices. positions() also settles and
        saves, so a ledger we must not write (the house agent's, owned by
        another service) is copied into our own data dir first."""
        import shutil
        from .. import paper
        if not path.exists():
            return None
        if readonly:
            copy = self.ledgers.parent / "house-ledger-copy.json"
            try:
                shutil.copyfile(path, copy)
            except OSError:
                return None
            path = copy
        token = FORCED_LEDGER.set(path)
        try:
            return await paper.positions()
        except Exception:
            return None
        finally:
            FORCED_LEDGER.reset(token)

    async def _compute(self) -> dict:
        rows = []
        for e in self.db.arena_list():
            p = await self._mark(self.ledgers / f"{e['user_id']}.json")
            rows.append(self._row(e["name"], e["strategy"], e["created"], p, house=False))
        if self.house:
            p = await self._mark(self.house, readonly=True)
            if p:
                rows.append(self._row("house paper agent", "reference quote-and-settle agent, hourly, "
                                      "examples/paper_agent", None, p, house=True))
        ranked = [r for r in rows if not r["house"] and r["equity"] is not None]
        ranked.sort(key=lambda r: (-(r["return_pct"] or 0), r["since"] or ""))
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
        now = time.time()
        return {"division": "paper", "computed_at": _iso(now), "computed_at_ts": now,
                "ttl_seconds": int(BOARD_TTL), "entries": ranked + [r for r in rows if r["house"]],
                "note": ("paper ledgers on the hosted oddsrail, marked at the current mid; no fees, "
                         "no queue, no market impact, no payout at resolution. Return is equity "
                         "over the starting bankroll. Nothing here is real money.")}

    @staticmethod
    def _row(name: str, strategy: str, created: float | None, p: dict | None, house: bool) -> dict:
        if not p:
            return {"name": name, "strategy": strategy, "house": house, "since": _iso(created) if created else None,
                    "equity": None, "return_pct": None, "note": "no ledger yet"}
        bank = float(p.get("bankroll") or 0) or 1.0
        eq = float(p.get("equity") or 0)
        return {"name": name, "strategy": strategy, "house": house,
                "since": _iso(created) if created else None,
                "equity": round(eq, 2), "return_pct": round(100.0 * (eq - bank) / bank, 2),
                "cash": p.get("cash"), "realized_pnl": p.get("realized_pnl"),
                "unrealized_pnl": p.get("unrealized_pnl"),
                "positions": len(p.get("positions") or []), "open_orders": len(p.get("open_orders") or []),
                "fills": p.get("fills")}


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
