"""Offline subprocess used by test_live_process_recovery.py.

Only the parent creates/kills these processes. Checkpoints are reached after a
real SQLite commit and block on stdin, so SIGKILL cannot run Python cleanup.
The separate venue database is an external durable witness, not a production
adapter: this helper never uses a network, credentials, or wallet signatures.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from oddsrail.live.contracts import (AccountPolicy, AccountSnapshot, AgentPolicy,
                                    OrderObservation, OrderRequest, Permission, PreparedOrder)
from oddsrail.live.engine import LiveEngine
from oddsrail.live.store import ExecutionBlocked, ExecutionStore

OWNER = "0x" + "1" * 40
ACCOUNT = "0x" + "2" * 40
MARKET = "0x" + "a" * 64
AGENT = "crash-agent"
SESSION = "crash-session"
BATCH = "durable-pair"


def emit(value):
    print(json.dumps(value), flush=True)


class Checkpoints:
    def __init__(self, target):
        self.target = target
        self.used = False

    def hit(self, name):
        if self.target != name or self.used:
            return
        self.used = True
        emit({"event": "checkpoint", "name": name})
        # Deliberately synchronous: model a stopped/stuck process without its
        # renewal task winning a timing race against the parent-controlled gate.
        if sys.stdin.readline() != "continue\n":
            raise RuntimeError("Parent closed checkpoint gate")


class CheckpointStore(ExecutionStore):
    def __init__(self, *args, checkpoints, **kwargs):
        self.checkpoints = checkpoints
        self.bound = 0
        self.acknowledged = 0
        self.write_failures = []
        super().__init__(*args, **kwargs)

    def reserve_pair(self, *args, **kwargs):
        try:
            result = super().reserve_pair(*args, **kwargs)
        except sqlite3.DatabaseError as exc:
            self.write_failures.append(exc.sqlite_errorname)
            raise
        if result[1]:
            self.checkpoints.hit("pair_reserved")
        return result

    def bind_hash(self, *args, **kwargs):
        super().bind_hash(*args, **kwargs)
        self.bound += 1
        if self.bound == 2:
            self.checkpoints.hit("hashes_bound")

    def begin_dispatch(self, *args, **kwargs):
        super().begin_dispatch(*args, **kwargs)
        self.checkpoints.hit("dispatch_committed")

    def acknowledge(self, *args, **kwargs):
        super().acknowledge(*args, **kwargs)
        self.acknowledged += 1
        if self.acknowledged == 1:
            self.checkpoints.hit("acknowledgment_committed")


class DurableFakeExchange:
    def __init__(self, path, clock, checkpoints):
        self.path, self.clock, self.checkpoints = path, clock, checkpoints
        with closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY, order_hash TEXT NOT NULL,
                    intent_id TEXT NOT NULL, account TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders (
                    order_hash TEXT PRIMARY KEY, account TEXT NOT NULL,
                    session_id TEXT NOT NULL, state TEXT NOT NULL,
                    matched TEXT NOT NULL DEFAULT '0', confirmed TEXT NOT NULL DEFAULT '0',
                    final INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS cancels (
                    id INTEGER PRIMARY KEY, order_hash TEXT NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    async def account_snapshot(self, account):
        return AccountSnapshot(account, OWNER, self.clock(), D("100"), D("100"),
                               D(0), (), D(0), True, True)

    async def permission(self, account, session):
        return Permission(account, session, self.clock(), self.clock() + 3_600_000, True, True)

    async def heartbeat(self, account, session):
        pass

    async def prepare(self, account, session, intent, request):
        # A stable fake identity, never a cryptographic order signature.
        order_hash = "0x" + hashlib.sha256((account + session + intent).encode()).hexdigest()
        return PreparedOrder(account, session, intent, order_hash, request)

    async def submit(self, order):
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO attempts(order_hash,intent_id,account) VALUES(?,?,?)",
                       (order.order_hash, order.intent_id, order.account))
            # Do not hide duplicate calls behind an idempotent fake adapter.
            # Attempts records every call even if the exchange knows the hash.
            db.execute("INSERT OR IGNORE INTO orders(order_hash,account,session_id,state) VALUES(?,?,?,'open')",
                       (order.order_hash, order.account, order.session_id))
            db.commit()
        self.checkpoints.hit("exchange_accepted")
        return order.order_hash

    async def lookup(self, account, session, order_hash):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM orders WHERE order_hash=?", (order_hash,)).fetchone()
        if row is None:
            return None
        if row["account"] != account or row["session_id"] != session:
            raise RuntimeError("Wrong fake exchange identity")
        return OrderObservation(account, session, order_hash, self.clock(), row["state"],
                                D(row["matched"]), D(row["confirmed"]), bool(row["final"]))

    async def cancel(self, account, session, order_hash):
        with closing(self.connect()) as db:
            db.execute("INSERT INTO cancels(order_hash) VALUES(?)", (order_hash,))
        # A request receipt proves no terminal exchange state or settlement.


async def run(args):
    checkpoints = Checkpoints(args.checkpoint)
    clock = lambda: args.now
    store = CheckpointStore(args.store, clock=clock, checkpoints=checkpoints)
    store.register_account(AccountPolicy(OWNER, ACCOUNT, D("100"), D("10"), D("50"), D("10")))
    store.register_agent(AgentPolicy(ACCOUNT, AGENT, SESSION, MARKET, ("123", "456"),
                                     D("40"), D("10"), D("40")))
    venue = DurableFakeExchange(args.exchange, clock, checkpoints)
    engine = LiveEngine(store, venue=venue, worker_id=args.worker, timeout_seconds=2)
    requests = tuple(OrderRequest(MARKET, token, D("0.48"), D("10"), D("0.01"), D("5"))
                     for token in ("123", "456"))
    results = []
    error = None
    recovered_agent_state = None
    try:
        if args.action == "submit":
            results.append((await engine.start(ACCOUNT, AGENT)).state)
            checkpoints.hit("before_submit")
            result = await engine.submit_pair(ACCOUNT, AGENT, BATCH, requests, market_guard=lambda _: True)
            results.append(result.state)
            error = result.reason
        elif args.action == "recover":
            results.append((await engine.recover(ACCOUNT)).state)
            recovered_agent_state = store.agent(ACCOUNT, AGENT)["state"]
            # Replaying a persisted strategy intent must never transmit again,
            # including when neither of its original orders reached the venue.
            for _ in range(2):
                results.append((await engine.submit_pair(ACCOUNT, AGENT, BATCH, requests,
                                                        market_guard=lambda _: True)).state)
        elif args.action == "takeover":
            results.append((await engine.recover(ACCOUNT)).state)
            lease = store.acquire(ACCOUNT, args.worker)
            try:
                checkpoints.hit("takeover_holds_lease")
            finally:
                store.release(lease)
        else:
            raise ValueError("Unknown worker action")
    except ExecutionBlocked as exc:
        error = str(exc)
    emit({"event": "result", "results": results, "error": error,
          "recovered_agent_state": recovered_agent_state,
          "agent_state": store.agent(ACCOUNT, AGENT)["state"],
          "status": store.status(ACCOUNT), "write_failures": store.write_failures})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--exchange", type=Path, required=True)
    parser.add_argument("--now", type=int, required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--action", choices=("submit", "recover", "takeover"), required=True)
    parser.add_argument("--checkpoint", default="")
    asyncio.run(run(parser.parse_args()))
