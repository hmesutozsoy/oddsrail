"""Event-driven supervision for a single selected Quote both sides agent.

Market data can be shared by many supervisors; each account's financial calls
are serialized by the SQLite coordinator. This service does not create a key,
authorize an agent, or silently resume one after restart or an error halt.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from decimal import Decimal

from .contracts import OrderRequest
from .engine import ExecutionResult, LiveEngine
from .market_feed import SharedMarketFeed
from .observer import MarketMetadata, fetch_market_metadata, plan_current
from .quotes import QuotePolicy, QuoteUnavailable
from .store import ExecutionBlocked


class QuoteSupervisor:
    def __init__(self, engine: LiveEngine, account: str, agent_id: str,
                 feed: SharedMarketFeed, policy: QuotePolicy, *,
                 metadata_fetcher=fetch_market_metadata, clock=time.monotonic,
                 min_quote_interval=2.0, metadata_interval=20.0):
        if (type(min_quote_interval) not in (int, float) or not math.isfinite(min_quote_interval) or
                not 2 <= min_quote_interval <= 60 or type(metadata_interval) not in (int, float) or
                not math.isfinite(metadata_interval) or not 1 <= metadata_interval <= 20):
            raise ValueError("Invalid supervisor cadence")
        agent = engine.store.agent(account, agent_id)["policy"]
        if (agent["condition_id"] != policy.condition_id or
                tuple(agent["tokens"]) != (policy.yes_token_id, policy.no_token_id) or
                policy.per_order_usd > Decimal(agent["per_order"]) or policy.shares > 10000):
            raise ValueError("Planner must match the approved agent policy")
        self.engine, self.account, self.agent_id = engine, account, agent_id
        self.feed, self.policy = feed, policy
        self.metadata_fetcher, self.clock = metadata_fetcher, clock
        self.min_quote_interval, self.metadata_interval = min_quote_interval, metadata_interval
        self.metadata: MarketMetadata | None = None
        self.last_result = ExecutionResult("paused")
        self.last_quote_at = float("-inf")
        self.last_reconcile_at = float("-inf")
        self.heartbeat_failed = False

    def _requests(self) -> tuple[OrderRequest, OrderRequest]:
        if self.heartbeat_failed:
            raise QuoteUnavailable("heartbeat_unavailable", "The order heartbeat is unavailable")
        if self.metadata is None:
            raise QuoteUnavailable("metadata_unavailable", "Verified market metadata is required")
        # Initial pilot signs only markets with explicitly verified zero maker
        # fees. Unknown/positive maker fees require a reviewed pricing adapter.
        if self.metadata.maker_fee_free is not True:
            raise QuoteUnavailable("maker_fee_unsupported", "Maker fee support requires review")
        quotes = plan_current(self.metadata, self.policy, self.feed, now=self.clock())
        try:
            return tuple(OrderRequest(q.condition_id, q.token_id, q.price, q.size, q.tick_size,
                                      q.min_order_size) for q in quotes)
        except ValueError as exc:
            raise QuoteUnavailable("unsupported_quote", "The quote is outside the pilot's supported limits") from exc

    def _guard(self, requests) -> bool:
        try:
            return self._requests() == requests
        except (ValueError, QuoteUnavailable):
            return False

    async def step(self) -> ExecutionResult:
        """Evaluate on a feed event and at least every 250 ms for stale data."""
        active = self.engine.store.orders(self.account, agent_id=self.agent_id, active_only=True)
        running = self.engine.store.agent(self.account, self.agent_id)["state"] == "running"
        if self.engine.store.status(self.account)["blocked"] and active:
            self.last_result = await self.engine.halt_and_cancel(self.account, self.agent_id, "account_halted")
            return self.last_result
        if not running and not active:
            self.last_result = ExecutionResult("paused")
            return self.last_result
        try:
            requests = self._requests()
        except QuoteUnavailable as exc:
            self.last_result = await self.engine.halt_and_cancel(self.account, self.agent_id, exc.code)
            return self.last_result
        now = self.clock()
        if now - self.last_reconcile_at >= 2:
            await self.engine.reconcile(self.account)
            await self.engine.check_risk(self.account, self.agent_id)
            self.last_reconcile_at = self.clock()
        if self.engine.store.agent(self.account, self.agent_id)["state"] != "running":
            self.last_result = ExecutionResult("paused")
            return self.last_result
        if self.clock() - self.last_quote_at < self.min_quote_interval:
            return self.last_result
        self.last_quote_at = self.clock()
        self.last_result = await self.engine.maintain_pair(
            self.account, self.agent_id, secrets.token_hex(16), requests, market_guard=self._guard)
        return self.last_result

    async def run(self, stop: asyncio.Event):
        """The caller starts the shared feed and explicitly starts the agent.

        Shutdown attempts cancellation, but reports remain pending until the
        adapter confirms both cancellation and settlement. The venue heartbeat
        is an additional cancellation mechanism, not evidence of completion.
        """
        async def metadata_loop():
            while not stop.is_set():
                try:
                    async with asyncio.timeout(8):
                        self.metadata = await self.metadata_fetcher(self.policy.condition_id)
                except Exception:
                    self.metadata = None
                try:
                    await asyncio.wait_for(stop.wait(), self.metadata_interval)
                except TimeoutError:
                    pass

        async def heartbeat_loop():
            while not stop.is_set():
                active = self.engine.store.orders(self.account, agent_id=self.agent_id, active_only=True)
                running = self.engine.store.agent(self.account, self.agent_id)["state"] == "running"
                if ((running or active) and (self.metadata is not None or active)
                        and not self.engine.store.status(self.account)["blocked"]):
                    try:
                        # Never prolong standing quotes while the main loop is
                        # awaiting slow account reads with stale market data.
                        self._requests()
                        await self.engine.heartbeat(self.account, self.agent_id)
                    except Exception:
                        self.heartbeat_failed = True
                        try:
                            await self.engine.halt_and_cancel(self.account, self.agent_id, "supervision_unavailable")
                        except ExecutionBlocked:
                            pass  # Busy coordinator sees the latched guard failure.
                try:
                    await asyncio.wait_for(stop.wait(), 5)
                except TimeoutError:
                    pass

        metadata_task = asyncio.create_task(metadata_loop())
        heartbeat_task = asyncio.create_task(heartbeat_loop())
        try:
            while not stop.is_set():
                # Initial metadata acquisition is a warm-up, never a grant to
                # start. Any existing orders still require active supervision.
                if self.metadata is not None or self.engine.store.orders(self.account, agent_id=self.agent_id, active_only=True):
                    try:
                        await self.step()
                    except ExecutionBlocked as exc:
                        if str(exc) != "account_busy":
                            raise
                revision = self.feed.revision
                try:
                    await self.feed.wait_for_update(revision, timeout=0.25)
                except TimeoutError:
                    pass
        finally:
            metadata_task.cancel()
            heartbeat_task.cancel()
            await asyncio.gather(metadata_task, heartbeat_task, return_exceptions=True)
            # The shared feed belongs to the host, not to this agent.
            try:
                self.last_result = await self.engine.cancel_orders(self.account, self.agent_id)
            except Exception:
                self.last_result = ExecutionResult("cancel_pending", reason="shutdown_reconciliation_required")
