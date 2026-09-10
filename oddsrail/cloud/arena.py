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
# Substrings, not whole names: "house paper agent" and "oddsrail bot" both
# read as official to someone glancing at the board.
RESERVED = ("house", "oddsrail", "admin", "maintainer", "official", "staff", "support")
STRATEGY_MAX = 140
BOARD_TTL = 300.0

# An entry is ranked only once it has actually traded for a while. Everything
# else is listed but unranked, with the reason, so a board of one lucky idle
# ledger cannot top a board of agents doing the work.
MIN_FILLS = 10
MIN_PASSES = 6
MIN_AGE_H = 12.0

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
    flat = re.sub(r"[^a-z0-9]", "", name.lower())
    for word in RESERVED:
        if word in flat:
            raise ArenaError(f"{name!r} is reserved: a name cannot contain {word!r}, "
                             "because it would read as an official entry")
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
        rows, now = [], time.time()
        for e in self.db.arena_list():
            p = await self._mark(self.ledgers / f"{e['user_id']}.json")
            row = self._row(e["name"], e["strategy"], e["created"], p, house=False)
            agent = self.db.agent_get(e["user_id"]) or {}
            row["passes"] = int(agent.get("runs") or 0)
            row["qualifies"], row["why_unranked"] = _qualifies(row, e["created"], now)
            rows.append(row)
        if self.house:
            p = await self._mark(self.house, readonly=True)
            if p:
                h = self._row("house paper agent", "reference quote-and-settle agent, hourly, "
                              "examples/paper_agent", None, p, house=True)
                h["qualifies"], h["why_unranked"] = False, "reference row, never ranked"
                rows.append(h)
        ranked = [r for r in rows if not r["house"] and r["qualifies"]]
        ranked.sort(key=lambda r: (-(r["return_pct"] or 0), r["since"] or ""))
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
        unranked = [r for r in rows if not r["house"] and not r["qualifies"]]
        unranked.sort(key=lambda r: -(r.get("fills") or 0))
        return {"division": "paper", "computed_at": _iso(now), "computed_at_ts": now,
                "ttl_seconds": int(BOARD_TTL),
                "entries": ranked + unranked + [r for r in rows if r["house"]],
                "qualification": {"fills": MIN_FILLS, "passes": MIN_PASSES, "hours": MIN_AGE_H},
                "note": ("paper ledgers on the hosted oddsrail, marked at the current mid; no fees, "
                         "no queue, no market impact. Return is equity over the starting bankroll. "
                         f"An entry is ranked once it has {MIN_FILLS} fills, {MIN_PASSES} passes and "
                         f"{MIN_AGE_H:g} hours on the board; the rest are listed with the reason. "
                         "Nothing here is real money.")}

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


def _qualifies(row: dict, created: float | None, now: float) -> tuple[bool, str | None]:
    """Ranked, or listed with the reason it is not."""
    if row.get("equity") is None:
        return False, "no ledger to mark yet"
    missing = []
    fills = int(row.get("fills") or 0)
    passes = int(row.get("passes") or 0)
    hours = (now - float(created)) / 3600.0 if created else 0.0
    if fills < MIN_FILLS:
        missing.append(f"{fills} of {MIN_FILLS} fills")
    if passes < MIN_PASSES:
        missing.append(f"{passes} of {MIN_PASSES} passes")
    if hours < MIN_AGE_H:
        missing.append(f"{hours:.1f} of {MIN_AGE_H:g} hours on the board")
    return (not missing), (", ".join(missing) if missing else None)


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
