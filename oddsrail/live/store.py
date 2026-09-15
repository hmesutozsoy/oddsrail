"""Durable account-wide reservations and order state, using integer microdollars.

Every write is an immediate SQLite transaction. A fenced account lease covers
coordination; an intent is marked dispatching before any network submission.
Neither lease expiry, process restart, a missing order, nor a cancel ACK frees
funds. This first pilot buys only: filled capital remains committed until a
future explicit settlement/position-accounting migration, never a daily reset.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from decimal import Context, Decimal, ROUND_CEILING, localcontext
from pathlib import Path

from .contracts import (AccountPolicy, AccountSnapshot, AgentPolicy, OrderObservation,
                        OrderRequest, Permission, condition, identifier, money, wallet)

MAX_SNAPSHOT_AGE_MS = 10_000
LEASE_MS = 30_000
NONTERMINAL = ("reserved", "dispatching", "unknown", "open", "matched", "cancel_pending")


class ExecutionBlocked(Exception):
    """Only a stable, nonsecret reason code crosses a service boundary."""


@dataclass(frozen=True)
class Lease:
    account: str
    worker: str
    fence: int


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class ExecutionStore:
    def __init__(self, path: Path, *, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # No credentials or signed payloads are persisted, but account history
        # is private and should not inherit a world-readable create mask.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with closing(self._connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS live_accounts (
                    account TEXT PRIMARY KEY, owner TEXT NOT NULL, policy TEXT NOT NULL,
                    blocked TEXT, worker TEXT, fence INTEGER NOT NULL DEFAULT 0,
                    lease_until INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS live_agents (
                    account TEXT NOT NULL, agent_id TEXT NOT NULL, policy TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'paused',
                    PRIMARY KEY(account, agent_id),
                    FOREIGN KEY(account) REFERENCES live_accounts(account));
                CREATE TABLE IF NOT EXISTS live_orders (
                    intent_id TEXT NOT NULL, account TEXT NOT NULL, agent_id TEXT NOT NULL,
                    batch_key TEXT NOT NULL, fingerprint TEXT NOT NULL, session_id TEXT NOT NULL,
                    condition_id TEXT NOT NULL, token_id TEXT NOT NULL, request TEXT NOT NULL,
                    cost INTEGER NOT NULL, committed INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'reserved', order_hash TEXT, ever_accepted INTEGER NOT NULL DEFAULT 0,
                    matched TEXT NOT NULL DEFAULT '0', confirmed TEXT NOT NULL DEFAULT '0', failed TEXT NOT NULL DEFAULT '0',
                    terminal INTEGER NOT NULL DEFAULT 0, observed_at INTEGER NOT NULL DEFAULT 0,
                    settled_at INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
                    PRIMARY KEY(account, intent_id), UNIQUE(account, order_hash),
                    FOREIGN KEY(account, agent_id) REFERENCES live_agents(account, agent_id));
                CREATE INDEX IF NOT EXISTS live_orders_batch ON live_orders(account, agent_id, batch_key);
                CREATE INDEX IF NOT EXISTS live_orders_risk ON live_orders(account) WHERE committed>0 OR terminal=0;
                CREATE INDEX IF NOT EXISTS live_orders_active ON live_orders(account,agent_id) WHERE terminal=0;
                CREATE TABLE IF NOT EXISTS live_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL,
                    agent_id TEXT, intent_id TEXT, kind TEXT NOT NULL, at INTEGER NOT NULL,
                    detail TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS live_events_account ON live_events(account, id);
                CREATE INDEX IF NOT EXISTS live_events_cancel ON live_events(account, intent_id, id)
                    WHERE kind='cancel_requested';
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _transaction(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _event(self, db, account, kind, *, agent=None, intent=None, **detail):
        db.execute("INSERT INTO live_events(account,agent_id,intent_id,kind,at,detail) VALUES(?,?,?,?,?,?)",
                   (account, agent, intent, kind, self.clock(), _json(detail)))

    def register_account(self, policy: AccountPolicy):
        if not isinstance(policy, AccountPolicy):
            raise ValueError("Account policy required")
        account, owner = wallet(policy.account), wallet(policy.owner)
        encoded = _json(asdict(policy) | {"account": account, "owner": owner})
        with self._transaction() as db:
            old = db.execute("SELECT policy FROM live_accounts WHERE account=?", (account,)).fetchone()
            if old and old["policy"] != encoded:
                raise ExecutionBlocked("account_policy_immutable")
            if not old:
                db.execute("INSERT INTO live_accounts(account,owner,policy) VALUES(?,?,?)", (account, owner, encoded))
                self._event(db, account, "account_registered")

    def register_agent(self, policy: AgentPolicy):
        if not isinstance(policy, AgentPolicy):
            raise ValueError("Agent policy required")
        account = wallet(policy.account)
        encoded = _json(asdict(policy) | {"account": account, "condition_id": condition(policy.condition_id)})
        with self._transaction() as db:
            if not db.execute("SELECT 1 FROM live_accounts WHERE account=?", (account,)).fetchone():
                raise ExecutionBlocked("account_not_registered")
            old = db.execute("SELECT policy FROM live_agents WHERE account=? AND agent_id=?", (account, policy.agent_id)).fetchone()
            if old and old["policy"] != encoded:
                raise ExecutionBlocked("agent_policy_immutable")
            if not old:
                for other in db.execute("SELECT policy FROM live_agents WHERE account=?", (account,)):
                    if json.loads(other["policy"])["session_id"] == policy.session_id:
                        raise ExecutionBlocked("session_already_assigned")
                db.execute("INSERT INTO live_agents(account,agent_id,policy) VALUES(?,?,?)", (account, policy.agent_id, encoded))
                self._event(db, account, "agent_registered", agent=policy.agent_id)

    def acquire(self, account: str, worker: str) -> Lease:
        account, worker = wallet(account), identifier(worker)
        now = self.clock()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM live_accounts WHERE account=?", (account,)).fetchone()
            if not row:
                raise ExecutionBlocked("account_not_registered")
            if row["lease_until"] > now:
                raise ExecutionBlocked("account_busy")
            fence = row["fence"] + 1
            db.execute("UPDATE live_accounts SET worker=?,fence=?,lease_until=? WHERE account=?",
                       (worker, fence, now + LEASE_MS, account))
            return Lease(account, worker, fence)

    def _lease(self, db, lease: Lease):
        row = db.execute("SELECT * FROM live_accounts WHERE account=?", (lease.account,)).fetchone()
        if (not row or row["worker"] != lease.worker or row["fence"] != lease.fence or
                row["lease_until"] <= self.clock()):
            raise ExecutionBlocked("lease_lost")
        return row

    def renew(self, lease: Lease):
        with self._transaction() as db:
            self._lease(db, lease)
            db.execute("UPDATE live_accounts SET lease_until=? WHERE account=?", (self.clock() + LEASE_MS, lease.account))

    def release(self, lease: Lease):
        with self._transaction() as db:
            db.execute("UPDATE live_accounts SET worker=NULL,lease_until=0 WHERE account=? AND worker=? AND fence=?",
                       (lease.account, lease.worker, lease.fence))

    def _agent(self, db, lease, agent_id):
        identifier(agent_id)
        row = db.execute("SELECT * FROM live_agents WHERE account=? AND agent_id=?", (lease.account, agent_id)).fetchone()
        if not row:
            raise ExecutionBlocked("agent_not_registered")
        return row

    def _readiness(self, account, agent, snapshot, permission):
        now = self.clock()
        if not isinstance(snapshot, AccountSnapshot) or not isinstance(permission, Permission):
            raise ExecutionBlocked("verification_required")
        if (wallet(snapshot.account) != account["account"] or wallet(snapshot.owner) != account["owner"] or
                not snapshot.complete or not snapshot.eligible or
                not 0 <= now - snapshot.captured_at_ms <= MAX_SNAPSHOT_AGE_MS):
            raise ExecutionBlocked("account_not_ready")
        policy = json.loads(agent["policy"])
        if (wallet(permission.account) != account["account"] or permission.session_id != policy["session_id"] or
                not permission.active or not permission.can_trade or
                not 0 <= now - permission.verified_at_ms <= MAX_SNAPSHOT_AGE_MS or
                permission.expires_at_ms <= now + LEASE_MS):
            raise ExecutionBlocked("permission_not_ready")
        if money(snapshot.daily_loss) >= money(Decimal(json.loads(account["policy"])["daily_loss"])):
            raise ExecutionBlocked("daily_loss_limit")

    def start(self, lease: Lease, agent_id: str, snapshot: AccountSnapshot, permission: Permission):
        with self._transaction() as db:
            account, agent = self._lease(db, lease), self._agent(db, lease, agent_id)
            self._readiness(account, agent, snapshot, permission)
            if agent["state"] == "revocation_requested":
                raise ExecutionBlocked("revocation_pending")
            if db.execute("SELECT 1 FROM live_orders WHERE account=? AND state IN ('unknown','dispatching','cancel_pending') AND terminal=0 LIMIT 1", (lease.account,)).fetchone():
                raise ExecutionBlocked("reconciliation_required")
            # A user/control-plane Start explicitly acknowledges a resolved halt.
            db.execute("UPDATE live_accounts SET blocked=NULL WHERE account=?", (lease.account,))
            db.execute("UPDATE live_agents SET state='running' WHERE account=? AND agent_id=?", (lease.account, agent_id))
            self._event(db, lease.account, "agent_started", agent=agent_id)

    def pause(self, lease: Lease, agent_id: str):
        with self._transaction() as db:
            self._lease(db, lease)
            row = self._agent(db, lease, agent_id)
            if row["state"] != "revocation_requested":
                db.execute("UPDATE live_agents SET state='paused' WHERE account=? AND agent_id=?", (lease.account, agent_id))
            self._event(db, lease.account, "agent_paused", agent=agent_id)

    def request_revocation(self, lease: Lease, agent_id: str):
        with self._transaction() as db:
            self._lease(db, lease)
            self._agent(db, lease, agent_id)
            db.execute("UPDATE live_agents SET state='revocation_requested' WHERE account=? AND agent_id=?", (lease.account, agent_id))
            self._event(db, lease.account, "revocation_requested", agent=agent_id)

    def halt(self, lease: Lease, reason: str):
        identifier(reason)
        with self._transaction() as db:
            self._lease(db, lease)
            db.execute("UPDATE live_accounts SET blocked=? WHERE account=?", (reason, lease.account))
            db.execute("UPDATE live_agents SET state='paused' WHERE account=? AND state='running'", (lease.account,))
            self._event(db, lease.account, "account_halted", reason=reason)

    def _risk(self, db, account, agent, snapshot, new: list[OrderRequest], *, exclude=()):
        a, p = json.loads(account["policy"]), json.loads(agent["policy"])
        orders = [dict(row) for row in db.execute("SELECT * FROM live_orders WHERE account=? AND (committed>0 OR terminal=0)", (account["account"],))
                  if row["intent_id"] not in exclude]
        proposed = sum(q.cost for q in new)
        active = [o for o in orders if not o["terminal"]]
        if len(active) + len(new) + snapshot.external_open_orders > a["max_open_orders"]:
            raise ExecutionBlocked("max_open_orders")
        for q in new:
            if condition(q.condition_id) != p["condition_id"] or q.token_id not in p["tokens"]:
                raise ExecutionBlocked("market_not_allowed")
            if q.cost > min(money(Decimal(a["per_order"])), money(Decimal(p["per_order"]))):
                raise ExecutionBlocked("per_order_limit")
            if any(o["agent_id"] == agent["agent_id"] and o["token_id"] == q.token_id for o in active):
                raise ExecutionBlocked("quote_already_pending")
        own = sum(o["committed"] for o in orders)
        agent_cost = sum(o["committed"] for o in orders if o["agent_id"] == agent["agent_id"])
        if own + proposed > money(Decimal(a["capital"])) or agent_cost + proposed > money(Decimal(p["capital"])):
            raise ExecutionBlocked("capital_limit")
        external = {condition(key): money(value) for key, value in snapshot.external_exposure}
        for market in {p["condition_id"], *(condition(q.condition_id) for q in new)}:
            market_new = sum(q.cost for q in new if condition(q.condition_id) == market)
            held = sum(o["committed"] for o in orders if o["condition_id"] == market)
            held_agent = sum(o["committed"] for o in orders if o["condition_id"] == market and o["agent_id"] == agent["agent_id"])
            if held + external.get(market, 0) + market_new > money(Decimal(a["per_market"])) or held_agent + market_new > money(Decimal(p["per_market"])):
                raise ExecutionBlocked("market_exposure_limit")
        # Resting/unknown/matched orders retain full cash holds. A final fill
        # releases its cash hold only after a snapshot whose reads began after
        # settlement confirmation; its allocation/exposure remains committed.
        cash_hold = sum(o["cost"] if not o["terminal"] else o["committed"]
                        for o in orders if not o["terminal"] or o["settled_at"] >= snapshot.captured_at_ms)
        required = cash_hold + proposed + money(snapshot.external_reserved)
        if required > min(money(snapshot.cash), money(snapshot.allowance)):
            raise ExecutionBlocked("insufficient_verified_funds")

    def check_risk(self, lease: Lease, agent_id: str, snapshot: AccountSnapshot, permission: Permission):
        """Revalidate standing quotes even when prices do not change."""
        with self._transaction() as db:
            account, agent = self._lease(db, lease), self._agent(db, lease, agent_id)
            if account["blocked"]:
                raise ExecutionBlocked("account_halted")
            self._readiness(account, agent, snapshot, permission)
            self._risk(db, account, agent, snapshot, [])

    def reserve_pair(self, lease: Lease, agent_id: str, batch_key: str, requests: tuple[OrderRequest, OrderRequest],
                     snapshot: AccountSnapshot, permission: Permission) -> tuple[list[dict], bool]:
        identifier(batch_key)
        if not isinstance(requests, tuple) or len(requests) != 2 or not all(isinstance(q, OrderRequest) for q in requests) or requests[0].token_id == requests[1].token_id:
            raise ValueError("A pair of distinct validated orders is required")
        encoded = _json([asdict(q) for q in requests])
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with self._transaction() as db:
            account, agent = self._lease(db, lease), self._agent(db, lease, agent_id)
            existing = list(db.execute("SELECT * FROM live_orders WHERE account=? AND agent_id=? AND batch_key=? ORDER BY intent_id", (lease.account, agent_id, batch_key)))
            if existing:
                if len(existing) != 2 or any(o["fingerprint"] != fingerprint for o in existing):
                    raise ExecutionBlocked("idempotency_conflict")
                return [dict(o) for o in existing], False
            if account["blocked"] or agent["state"] != "running":
                raise ExecutionBlocked(account["blocked"] or "agent_not_running")
            self._readiness(account, agent, snapshot, permission)
            self._risk(db, account, agent, snapshot, list(requests))
            p = json.loads(agent["policy"])
            rows = []
            for index, q in enumerate(requests):
                intent = hashlib.sha256(f"{lease.account}:{agent_id}:{batch_key}:{index}".encode()).hexdigest()
                db.execute("""INSERT INTO live_orders(intent_id,account,agent_id,batch_key,fingerprint,session_id,
                           condition_id,token_id,request,cost,committed,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (intent, lease.account, agent_id, batch_key, fingerprint, p["session_id"], condition(q.condition_id),
                            q.token_id, _json(asdict(q)), q.cost, q.cost, self.clock()))
                self._event(db, lease.account, "order_reserved", agent=agent_id, intent=intent, cost_micro=q.cost)
                rows.append(dict(db.execute("SELECT * FROM live_orders WHERE account=? AND intent_id=?", (lease.account, intent)).fetchone()))
            return rows, True

    def bind_hash(self, lease: Lease, intent: str, order_hash: str):
        order_hash = condition(order_hash)
        with self._transaction() as db:
            self._lease(db, lease)
            row = self._order(db, lease, intent)
            if row["state"] != "reserved" or row["order_hash"]:
                raise ExecutionBlocked("intent_already_prepared")
            db.execute("UPDATE live_orders SET order_hash=? WHERE account=? AND intent_id=?", (order_hash, lease.account, intent))
            self._event(db, lease.account, "order_prepared", agent=row["agent_id"], intent=intent, order_hash=order_hash)

    def _order(self, db, lease, intent):
        identifier(intent)
        row = db.execute("SELECT * FROM live_orders WHERE account=? AND intent_id=?", (lease.account, intent)).fetchone()
        if not row:
            raise ExecutionBlocked("intent_not_found")
        return row

    def begin_dispatch(self, lease: Lease, intent: str, snapshot: AccountSnapshot, permission: Permission):
        with self._transaction() as db:
            account, row = self._lease(db, lease), self._order(db, lease, intent)
            agent = self._agent(db, lease, row["agent_id"])
            if row["state"] != "reserved" or not row["order_hash"]:
                raise ExecutionBlocked("intent_not_dispatchable")
            if account["blocked"] or agent["state"] != "running":
                raise ExecutionBlocked(account["blocked"] or "agent_not_running")
            self._readiness(account, agent, snapshot, permission)
            self._risk(db, account, agent, snapshot, [self.request(row)], exclude=(intent,))
            db.execute("UPDATE live_orders SET state='dispatching' WHERE account=? AND intent_id=?", (lease.account, intent))
            self._event(db, lease.account, "submission_started", agent=row["agent_id"], intent=intent)

    def acknowledge(self, lease: Lease, intent: str, order_hash: str):
        with self._transaction() as db:
            self._lease(db, lease)
            row = self._order(db, lease, intent)
            if row["state"] != "dispatching" or condition(order_hash) != row["order_hash"]:
                raise ExecutionBlocked("invalid_submission_ack")
            db.execute("UPDATE live_orders SET state='open',ever_accepted=1 WHERE account=? AND intent_id=?", (lease.account, intent))
            self._event(db, lease.account, "submission_acknowledged", agent=row["agent_id"], intent=intent)

    def abandon_reserved(self, lease: Lease, intent: str):
        with self._transaction() as db:
            self._lease(db, lease)
            row = self._order(db, lease, intent)
            if row["state"] != "reserved":
                raise ExecutionBlocked("intent_may_have_been_submitted")
            db.execute("UPDATE live_orders SET state='rejected',terminal=1,committed=0 WHERE account=? AND intent_id=?", (lease.account, intent))
            self._event(db, lease.account, "unsubmitted_intent_released", agent=row["agent_id"], intent=intent)

    def unknown(self, lease: Lease, intent: str):
        with self._transaction() as db:
            self._lease(db, lease)
            row = self._order(db, lease, intent)
            if row["terminal"] or row["state"] == "reserved":
                raise ExecutionBlocked("invalid_unknown_transition")
            db.execute("UPDATE live_orders SET state='unknown' WHERE account=? AND intent_id=?", (lease.account, intent))
            db.execute("UPDATE live_accounts SET blocked='reconciliation_required' WHERE account=?", (lease.account,))
            db.execute("UPDATE live_agents SET state='paused' WHERE account=? AND state='running'", (lease.account,))
            self._event(db, lease.account, "submission_unknown", agent=row["agent_id"], intent=intent)

    def cancellation_candidates(self, lease: Lease) -> list[dict]:
        """Nonterminal orders ordered by their durable last cancel admission.

        An interrupted bounded pass must yield priority to orders it never
        reached. Event IDs, rather than wall time, preserve ordering when
        timestamps repeat or a new coordinator takes over the account.
        """
        with closing(self._connect()) as db:
            self._lease(db, lease)
            return [dict(row) for row in db.execute("""
                SELECT o.* FROM live_orders o WHERE o.account=? AND o.terminal=0
                ORDER BY COALESCE((SELECT MAX(e.id) FROM live_events e
                    WHERE e.account=o.account AND e.intent_id=o.intent_id AND e.kind='cancel_requested'),0),
                    o.created_at,o.intent_id
            """, (lease.account,))]

    def request_cancel(self, lease: Lease, intent: str):
        with self._transaction() as db:
            self._lease(db, lease)
            row = self._order(db, lease, intent)
            if row["terminal"]:
                return
            if row["state"] == "reserved" or not row["order_hash"]:
                raise ExecutionBlocked("intent_not_submitted")
            db.execute("UPDATE live_orders SET state='cancel_pending' WHERE account=? AND intent_id=?", (lease.account, intent))
            self._event(db, lease.account, "cancel_requested", agent=row["agent_id"], intent=intent)

    def observe(self, lease: Lease, intent: str, observation: OrderObservation):
        if not isinstance(observation, OrderObservation):
            raise ValueError("Authoritative observation required")
        with self._transaction() as db:
            self._lease(db, lease)
            row = self._order(db, lease, intent)
            if (wallet(observation.account) != lease.account or observation.session_id != row["session_id"] or
                    condition(observation.order_hash) != row["order_hash"] or row["state"] == "reserved"):
                raise ExecutionBlocked("observation_identity_mismatch")
            if not 0 <= self.clock() - observation.observed_at_ms <= MAX_SNAPSHOT_AGE_MS:
                raise ExecutionBlocked("observation_stale")
            if observation.observed_at_ms < row["observed_at"]:
                raise ExecutionBlocked("observation_out_of_order")
            request = self.request(row)
            if (observation.matched_size > request.size or observation.confirmed_size < Decimal(row["confirmed"]) or
                    observation.failed_size < Decimal(row["failed"]) or
                    observation.matched_size < Decimal(row["matched"])):
                raise ExecutionBlocked("fill_inconsistent")
            if observation.state == "filled" and observation.matched_size != request.size:
                raise ExecutionBlocked("fill_inconsistent")
            if row["terminal"]:
                if (not observation.final or observation.state != row["state"] or
                        observation.confirmed_size != Decimal(row["confirmed"]) or observation.failed_size != Decimal(row["failed"])):
                    raise ExecutionBlocked("terminal_order_changed")
                return  # Duplicate terminal observations do not move the cash watermark.
            if observation.state == "rejected" and (row["ever_accepted"] or row["state"] not in ("dispatching", "unknown", "cancel_pending")):
                raise ExecutionBlocked("accepted_order_cannot_be_rejected")
            with localcontext(Context(prec=80)):
                committed = (int((Decimal(row["cost"]) * observation.confirmed_size / request.size).to_integral_value(rounding=ROUND_CEILING))
                             if observation.final else row["cost"])
            state = observation.state
            if not observation.final and state in ("canceled", "filled"):
                state = "matched"  # Cancelled remainder with unsettled fills still holds the full reservation.
            if row["state"] == "cancel_pending" and not observation.final:
                state = "cancel_pending"
            db.execute("""UPDATE live_orders SET state=?,matched=?,confirmed=?,failed=?,terminal=?,committed=?,observed_at=?,settled_at=?,ever_accepted=?
                       WHERE account=? AND intent_id=?""",
                       (state, str(observation.matched_size), str(observation.confirmed_size), str(observation.failed_size), int(observation.final),
                        committed, observation.observed_at_ms, observation.observed_at_ms if observation.final else 0,
                        int(bool(row["ever_accepted"]) or observation.state != "rejected"),
                        lease.account, intent))
            self._event(db, lease.account, "order_reconciled", agent=row["agent_id"], intent=intent,
                        state=state, final=observation.final, matched=str(observation.matched_size),
                        confirmed=str(observation.confirmed_size), failed=str(observation.failed_size), committed_micro=committed)

    def recover(self, lease: Lease):
        """Called on worker startup; never resume trading automatically."""
        with self._transaction() as db:
            self._lease(db, lease)
            db.execute("UPDATE live_agents SET state='paused' WHERE account=? AND state='running'", (lease.account,))
            db.execute("UPDATE live_orders SET state='rejected',terminal=1,committed=0 WHERE account=? AND state='reserved'", (lease.account,))
            db.execute("UPDATE live_orders SET state='unknown' WHERE account=? AND state='dispatching'", (lease.account,))
            db.execute("UPDATE live_accounts SET blocked='restart_reconciliation' WHERE account=?", (lease.account,))
            self._event(db, lease.account, "worker_recovered")

    @staticmethod
    def request(row) -> OrderRequest:
        data = json.loads(row["request"])
        for key in ("price", "size", "tick_size", "min_order_size", "fee_buffer_rate"):
            data[key] = Decimal(data[key])
        return OrderRequest(**data)

    def orders(self, account: str, *, agent_id=None, active_only=False) -> list[dict]:
        account = wallet(account)
        sql, params = "SELECT * FROM live_orders WHERE account=?", [account]
        if agent_id is not None:
            sql += " AND agent_id=?"
            params.append(identifier(agent_id))
        if active_only:
            sql += " AND terminal=0"
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(sql + " ORDER BY created_at,intent_id", params)]

    def agent(self, account: str, agent_id: str) -> dict:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM live_agents WHERE account=? AND agent_id=?", (wallet(account), identifier(agent_id))).fetchone()
            if not row:
                raise ExecutionBlocked("agent_not_registered")
            return dict(row) | {"policy": json.loads(row["policy"])}

    def status(self, account: str) -> dict:
        account = wallet(account)
        with closing(self._connect()) as db:
            row = db.execute("SELECT blocked FROM live_accounts WHERE account=?", (account,)).fetchone()
            if not row:
                raise ExecutionBlocked("account_not_registered")
            totals = db.execute("SELECT COALESCE(SUM(committed),0) AS committed,COALESCE(SUM(1-terminal),0) AS pending FROM live_orders WHERE account=? AND (committed>0 OR terminal=0)", (account,)).fetchone()
            return {"account": account, "blocked": row["blocked"], "committed_micro": totals["committed"], "pending_orders": totals["pending"]}

    def history(self, account: str, *, after=0, limit=100) -> list[dict]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Invalid history page")
        with closing(self._connect()) as db:
            return [dict(row) | {"detail": json.loads(row["detail"])} for row in db.execute(
                "SELECT * FROM live_events WHERE account=? AND id>? ORDER BY id LIMIT ?", (wallet(account), after, limit))]
