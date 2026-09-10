"""Hourly runs for kept agents.

One loop in the server process: every minute, find agents whose schedule is
hourly and whose last pass is older than an hour, and run them one at a
time on their own ledger. The result summary is stored on the agent so the
site and the board can show it. Failures are recorded, never raised."""

from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import Path

from . import runner
from .db import DB

INTERVAL = 3600.0
TICK = 60.0
PASS_DEADLINE = 180.0        # seconds; a hung venue API must not stall the whole hour
GUEST_TTL = 30 * 86400       # guest ledgers untouched this long are removed
LAST_TICK = 0.0              # heartbeat read by /healthz
_last_housekeeping = 0.0


def summary(out: dict) -> dict:
    led = out.get("ledger") or {}
    return {"at": out.get("started"), "seconds": out.get("seconds"), "orders": out.get("orders_placed"),
            "decisions": len(out.get("decisions") or []), "equity": led.get("equity"),
            "halted": out.get("halted"), "ok": bool(out.get("ok"))}


async def run_agent(db: DB, ledgers: Path, agent: dict) -> dict:
    ledger = ledgers / f"{agent['user_id']}.json"
    run_id = None
    async with runner.lock_for(ledger):
        try:
            out = await asyncio.wait_for(runner.run_pass(agent["config"], ledger), timeout=PASS_DEADLINE)
            res = summary(out)
            try:                       # the hourly path leaves a permalink too
                run_id = secrets.token_urlsafe(9)
                db.put_run(run_id, agent["config"], out, user_id=agent["user_id"], agent=agent["name"])
            except Exception as e:
                print(f"[oddsrail-cloud] could not store scheduled run: {type(e).__name__}: {e}", flush=True)
                run_id = None
        except asyncio.TimeoutError:
            res = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": False,
                   "error": f"pass exceeded {PASS_DEADLINE:.0f}s and was abandoned"}
        except Exception as e:
            res = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": False,
                   "error": f"{type(e).__name__}: {e}"}
    db.agent_ran(agent["user_id"], res, run_id)
    return res


async def tick(db: DB, ledgers: Path, now: float | None = None) -> int:
    """Run every due agent once. Returns how many ran."""
    now = now or time.time()
    due = db.agents_due(now - INTERVAL)
    for agent in due:
        await run_agent(db, ledgers, agent)
    return len(due)


def housekeeping(db: DB, guests: Path | None, now: float | None = None) -> dict:
    """Expired tokens, links and codes go; guest ledgers nobody touched in a
    month go. Runs once an hour from the loop."""
    now = now or time.time()
    db.cleanup()
    pruned = db.prune_runs()
    removed = 0
    if guests and guests.exists():
        for f in guests.glob("*.json"):
            try:
                if now - f.stat().st_mtime > GUEST_TTL:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    return {"guest_ledgers_removed": removed, "anonymous_runs_pruned": pruned}


async def loop(db: DB, ledgers: Path, stop: asyncio.Event) -> None:
    global LAST_TICK, _last_housekeeping
    while not stop.is_set():
        try:
            n = await tick(db, ledgers)
            LAST_TICK = time.time()
            if n:
                print(f"[oddsrail-cloud] scheduler ran {n} agent(s)", flush=True)
            if time.time() - _last_housekeeping > 3600:
                _last_housekeeping = time.time()
                hk = housekeeping(db, ledgers.parent / "guests")
                if hk["guest_ledgers_removed"]:
                    print(f"[oddsrail-cloud] housekeeping: {hk}", flush=True)
        except Exception as e:
            print(f"[oddsrail-cloud] scheduler error: {type(e).__name__}: {e}", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK)
        except asyncio.TimeoutError:
            pass
