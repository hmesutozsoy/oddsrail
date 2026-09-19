"""Bounded account coordinator. There is intentionally no default live venue.

A reviewed delegated signer/venue must be injected explicitly. The web app
does not mount this service yet. Its public operations distinguish pausing,
cancelling and requesting owner revocation. None implicitly grants permission.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable

from .contracts import (AccountSnapshot, OrderRequest, Permission, PreparedOrder,
                        Venue, condition, wallet)
from .store import ExecutionBlocked, ExecutionStore, Lease

CANCEL_PASS_SECONDS = 20


@dataclass(frozen=True)
class ExecutionResult:
    state: str
    intent_ids: tuple[str, ...] = ()
    reason: str | None = None


class LiveEngine:
    def __init__(self, store: ExecutionStore, *, venue: Venue | None = None, worker_id=None,
                 timeout_seconds=5.0):
        if not 0 < timeout_seconds <= 10:
            raise ValueError("Invalid venue timeout")
        self.store, self.venue = store, venue
        self.worker_id = worker_id or secrets.token_hex(16)
        self.timeout_seconds = timeout_seconds

    def _venue(self) -> Venue:
        if self.venue is None:
            raise ExecutionBlocked("delegated_adapter_not_configured")
        return self.venue

    async def _call(self, awaitable):
        async with asyncio.timeout(self.timeout_seconds):
            return await awaitable

    @asynccontextmanager
    async def _coordinator(self, account: str):
        lease = self.store.acquire(account, self.worker_id)
        parent = asyncio.current_task()

        async def renew():
            while True:
                await asyncio.sleep(5)
                try:
                    self.store.renew(lease)
                except Exception:
                    # Stop the old coordinator before another financial call.
                    # Dispatch already in flight remains reserved for recovery.
                    parent.cancel()
                    return

        task = asyncio.create_task(renew())
        try:
            yield lease
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.store.release(lease)

    async def _verification(self, account: str, session: str) -> tuple[AccountSnapshot, Permission]:
        venue = self._venue()
        # Both reads share one deadline. A failed read cancels its sibling.
        async with asyncio.timeout(self.timeout_seconds):
            async with asyncio.TaskGroup() as group:
                funding = group.create_task(venue.account_snapshot(account))
                permission = group.create_task(venue.permission(account, session))
        return funding.result(), permission.result()

    def _session(self, account: str, agent_id: str) -> str:
        return self.store.agent(account, agent_id)["policy"]["session_id"]

    async def recover(self, account: str) -> ExecutionResult:
        async with self._coordinator(account) as lease:
            self.store.recover(lease)
            if self.venue:
                await self._reconcile(lease)
                # Recovery halts the account, so surviving quotes must not
                # keep trading at their pre-crash prices. Reconcile first to
                # exclude finalized orders, then request cancellation across
                # all remaining managed sessions. Unknown/cancel-pending
                # outcomes still hold funds; a cancel ACK is not settlement.
                await self._cancel_all(lease)
        return ExecutionResult("paused", reason="explicit_start_required")

    async def start(self, account: str, agent_id: str) -> ExecutionResult:
        self._venue()
        async with self._coordinator(account) as lease:
            session = self._session(account, agent_id)
            try:
                if not await self._reconcile(lease):
                    raise ExecutionBlocked("reconciliation_required")
                snapshot, permission = await self._verification(account, session)
                await self._call(self._venue().heartbeat(account, session))
                self.store.start(lease, agent_id, snapshot, permission)
            except Exception:
                self.store.halt(lease, "activation_verification_failed")
                await self._cancel_all(lease)
                raise ExecutionBlocked("activation_verification_failed") from None
        return ExecutionResult("running")

    async def pause(self, account: str, agent_id: str) -> ExecutionResult:
        async with self._coordinator(account) as lease:
            self.store.pause(lease, agent_id)
        return ExecutionResult("paused", reason="existing_orders_unchanged")

    async def request_revocation(self, account: str, agent_id: str) -> ExecutionResult:
        async with self._coordinator(account) as lease:
            self.store.request_revocation(lease, agent_id)
        return ExecutionResult("revocation_requested", reason="owner_authorization_required")

    async def heartbeat(self, account: str, agent_id: str):
        """The supervisor calls every five seconds while orders may be open."""
        session = self._session(account, agent_id)
        if self.store.status(account)["blocked"]:
            raise ExecutionBlocked("account_halted")
        try:
            await self._call(self._venue().heartbeat(account, session))
        except Exception:
            await self.halt_and_cancel(account, agent_id, "heartbeat_unavailable")
            raise ExecutionBlocked("heartbeat_unavailable") from None

    async def reconcile(self, account: str) -> ExecutionResult:
        async with self._coordinator(account) as lease:
            complete = await self._reconcile(lease)
            if not complete:
                await self._cancel_all(lease)
        return ExecutionResult("reconciled" if complete else "reconciliation_pending")

    async def _reconcile(self, lease: Lease, *, agent_id=None):
        venue = self._venue()
        complete = True
        admission = asyncio.Semaphore(4)

        async def lookup(row):
            nonlocal complete
            if not row["order_hash"] or row["state"] == "reserved":
                return
            async with admission:
                observed = await venue.lookup(lease.account, row["session_id"], row["order_hash"])
                if observed is not None:
                    self.store.observe(lease, row["intent_id"], observed)
                else:
                    self.store.unknown(lease, row["intent_id"])
                if observed is None:
                    complete = False
                # A missing order (including 404) leaves its entire reservation.
        try:
            # One aggregate deadline, not N orders multiplied by an HTTP
            # timeout. Supervision must return to freshness checks promptly.
            async with asyncio.timeout(self.timeout_seconds):
                async with asyncio.TaskGroup() as group:
                    for row in self.store.orders(lease.account, agent_id=agent_id, active_only=True):
                        group.create_task(lookup(row))
        except Exception:
            self.store.halt(lease, "reconciliation_unavailable")
            return False
        return complete

    async def check_risk(self, account: str, agent_id: str) -> ExecutionResult:
        async with self._coordinator(account) as lease:
            if not await self._standing_risk(lease, agent_id):
                return ExecutionResult("paused", reason="account_risk_unavailable")
        return ExecutionResult("risk_checked")

    async def _standing_risk(self, lease: Lease, agent_id: str) -> bool:
        try:
            snapshot, permission = await self._verification(lease.account, self._session(lease.account, agent_id))
            self.store.check_risk(lease, agent_id, snapshot, permission)
            return True
        except Exception:
            self.store.halt(lease, "account_risk_unavailable")
            await self._cancel_all(lease)
            return False

    async def maintain_pair(self, account: str, agent_id: str, batch_key: str,
                            requests: tuple[OrderRequest, OrderRequest], *, market_guard) -> ExecutionResult:
        """Replace only after the previous pair's cancellation/fills settle.

        The running state is preserved during ordinary replacement. A user
        Pause or error halt persists and is never silently turned into Start.
        """
        self._venue()
        async with self._coordinator(account) as lease:
            if self.store.agent(account, agent_id)["state"] != "running":
                return ExecutionResult("paused")
            if not await self._standing_risk(lease, agent_id):
                return ExecutionResult("paused", reason="account_risk_unavailable")
            try:
                market_ready = market_guard(requests) is True
            except Exception:
                market_ready = False
            if not market_ready:
                self.store.halt(lease, "market_not_ready")
                await self._cancel_all(lease)
                return ExecutionResult("paused", reason="market_not_ready")
            active = self.store.orders(account, agent_id=agent_id, active_only=True)
            by_token = {q.token_id: q for q in requests}
            if len(active) == 2 and all(row["state"] == "open" and self.store.request(row) == by_token.get(row["token_id"]) for row in active):
                return ExecutionResult("quotes_unchanged", tuple(row["intent_id"] for row in active))
            if active:
                await self._cancel(lease, agent_id)
                if self.store.status(account)["blocked"]:
                    await self._cancel_all(lease)
                pending = self.store.orders(account, agent_id=agent_id, active_only=True)
                if pending:
                    return ExecutionResult("cancel_pending", tuple(row["intent_id"] for row in pending))
        return await self.submit_pair(account, agent_id, batch_key, requests, market_guard=market_guard)

    async def cancel_orders(self, account: str, agent_id: str) -> ExecutionResult:
        self._venue()
        async with self._coordinator(account) as lease:
            self.store.pause(lease, agent_id)
            await self._cancel(lease, agent_id)
            if self.store.status(account)["blocked"]:
                await self._cancel_all(lease)
            pending = self.store.orders(account, agent_id=agent_id, active_only=True)
        return ExecutionResult("cancel_pending" if pending else "canceled",
                               tuple(row["intent_id"] for row in pending))

    async def _cancel(self, lease: Lease, agent_id: str):
        venue = self._venue()
        for row in self.store.orders(lease.account, agent_id=agent_id, active_only=True):
            if row["state"] == "reserved":
                self.store.abandon_reserved(lease, row["intent_id"])
                continue
            self.store.request_cancel(lease, row["intent_id"])
            try:
                await self._call(venue.cancel(lease.account, row["session_id"], row["order_hash"]))
            except Exception:
                # A failed or partial cancel is not terminal. Continue trying
                # the other outcome; neither path frees this one's reservation.
                self.store.halt(lease, "cancel_unconfirmed")
        await self._reconcile(lease, agent_id=agent_id)

    async def _cancel_all(self, lease: Lease):
        """An account halt must reach every managed session, not one strategy.

        Admit the least recently attempted orders first, with four concurrent
        cancels and one aggregate deadline. Durable attempt order means slow
        sessions cannot monopolize every pass, even after a process restart.
        Unconfirmed orders retain funds. No ACK is treated as settlement.
        """
        venue = self._venue()
        admission = asyncio.Semaphore(4)

        async def cancel(row):
            async with admission:
                if row["state"] == "reserved":
                    self.store.abandon_reserved(lease, row["intent_id"])
                    return
                # Persist admission before the call. Orders still waiting for
                # a slot retain their earlier priority on the next pass.
                self.store.request_cancel(lease, row["intent_id"])
                try:
                    await self._call(venue.cancel(lease.account, row["session_id"], row["order_hash"]))
                except Exception:
                    self.store.halt(lease, "cancel_unconfirmed")

        try:
            async with asyncio.timeout(CANCEL_PASS_SECONDS):
                async with asyncio.TaskGroup() as group:
                    for row in self.store.cancellation_candidates(lease):
                        group.create_task(cancel(row))
        except TimeoutError:
            self.store.halt(lease, "cancel_unconfirmed")
        # A slow first session's lookup cannot delay other cancellation
        # attempts. Reconciliation has its own single bounded deadline.
        await self._reconcile(lease)

    async def halt_and_cancel(self, account: str, agent_id: str, reason: str) -> ExecutionResult:
        self.store.agent(account, agent_id)
        async with self._coordinator(account) as lease:
            self.store.halt(lease, reason)
            if self.venue:
                await self._cancel_all(lease)
        return ExecutionResult("paused", reason=reason)

    async def submit_pair(self, account: str, agent_id: str, batch_key: str,
                          requests: tuple[OrderRequest, OrderRequest], *,
                          market_guard: Callable[[tuple[OrderRequest, OrderRequest]], bool]) -> ExecutionResult:
        """Reserve both outcomes, sign locally, recheck, and transmit each once.

        The pair is a strategy intent, not an atomic exchange operation. If one
        fails, halt and request cancellation of its sibling. A fill during this
        window remains a real position and stays in the ledger's exposure.
        market_guard must read the current shared feed and verified metadata,
        checking freshness, eligibility, tick/size and post-only prices.
        """
        venue = self._venue()
        account = wallet(account)
        async with self._coordinator(account) as lease:
            session = self._session(account, agent_id)
            rows = []
            dispatching = None
            try:
                snapshot, permission = await self._verification(account, session)
                if market_guard(requests) is not True:
                    raise ExecutionBlocked("market_not_ready")
                await self._call(venue.heartbeat(account, session))
                rows, created = self.store.reserve_pair(lease, agent_id, batch_key, requests, snapshot, permission)
                if not created:
                    return ExecutionResult("already_recorded", tuple(r["intent_id"] for r in rows))
                prepared = []
                for row, request in zip(rows, requests, strict=True):
                    order = await self._call(venue.prepare(account, session, row["intent_id"], request))
                    if (not isinstance(order, PreparedOrder) or wallet(order.account) != account or
                            order.session_id != session or order.intent_id != row["intent_id"] or order.request != request):
                        raise ExecutionBlocked("prepared_order_mismatch")
                    self.store.bind_hash(lease, row["intent_id"], order.order_hash)
                    prepared.append(order)
                for order in prepared:
                    snapshot, permission = await self._verification(account, session)
                    await self._call(venue.heartbeat(account, session))
                    if market_guard(requests) is not True:
                        raise ExecutionBlocked("market_not_ready")
                    self.store.begin_dispatch(lease, order.intent_id, snapshot, permission)
                    dispatching = order.intent_id
                    acknowledged_hash = await self._call(venue.submit(order))
                    if not isinstance(acknowledged_hash, str) or condition(acknowledged_hash) != condition(order.order_hash):
                        raise ExecutionBlocked("invalid_submission_ack")
                    self.store.acknowledge(lease, order.intent_id, acknowledged_hash)
                    dispatching = None
                return ExecutionResult("orders_submitted", tuple(row["intent_id"] for row in rows))
            except asyncio.CancelledError:
                # Do no additional financial I/O in cancellation cleanup. A
                # new coordinator must reconcile dispatching/unknown hashes.
                try:
                    if dispatching:
                        self.store.unknown(lease, dispatching)
                    for row in self.store.orders(account, agent_id=agent_id, active_only=True):
                        if row["state"] == "reserved":
                            self.store.abandon_reserved(lease, row["intent_id"])
                    self.store.halt(lease, "worker_interrupted")
                except ExecutionBlocked:
                    pass  # Lease lost: only the next fenced worker may write.
                raise
            except Exception as exc:
                # Never log venue exceptions: they may contain signed requests
                # or authentication headers. Only our finite reason codes leave.
                safe_reasons = {"market_not_ready", "account_not_ready", "permission_not_ready", "daily_loss_limit",
                                "idempotency_conflict", "agent_not_running", "capital_limit", "per_order_limit",
                                "market_exposure_limit", "market_not_allowed", "max_open_orders", "quote_already_pending",
                                "insufficient_verified_funds", "prepared_order_mismatch", "invalid_submission_ack",
                                "reconciliation_required", "verification_required", "lease_lost"}
                reason = str(exc) if isinstance(exc, ExecutionBlocked) and str(exc) in safe_reasons else "venue_operation_failed"
                if dispatching:
                    self.store.unknown(lease, dispatching)
                self.store.halt(lease, reason)
                await self._cancel_all(lease)
                return ExecutionResult("paused", tuple(row["intent_id"] for row in rows), reason)
