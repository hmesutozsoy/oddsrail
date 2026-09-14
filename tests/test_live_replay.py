"""Synthetic transport-to-ledger replays; no external network or real signing.

Unlike unit tests with hand-built BookSnapshots, every usable book below must
arrive through SharedMarketFeed.run and its real JSON parser. Metadata similarly
passes through HTTPX streaming and the production public metadata parser.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from oddsrail.live.contracts import (
    AccountPolicy, AccountSnapshot, AgentPolicy, OrderObservation, Permission, PreparedOrder,
)
from oddsrail.live.engine import LiveEngine
from oddsrail.live.market_feed import MARKET_URL, SharedMarketFeed
from oddsrail.live.observer import fetch_market_metadata, plan_current
from oddsrail.live.quotes import QuotePolicy, QuoteUnavailable
from oddsrail.live.store import ExecutionStore
from oddsrail.live.supervisor import QuoteSupervisor


ACCOUNT, OWNER = "0x" + "1" * 40, "0x" + "2" * 40
MARKET = "0x" + "a" * 64
TOKENS = ("123", "456")
FIXTURE = Path(__file__).parent / "fixtures" / "live_replay" / "market.json"


async def until(predicate):
    """Wait for a real pipeline boundary, never an arbitrary wall-clock sleep."""
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0)


async def collect_tasks(*tasks):
    """Cancel unfinished children, await every child, and expose real failures."""
    tasks = [task for task in tasks if task is not None]
    for task in tasks:
        if not task.done():
            task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [result for result in results
              if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)]
    if errors:
        raise BaseExceptionGroup("Replay background task failed", errors)


async def stop_runner(stop, task):
    stop.set()
    try:
        if task is not None:
            await asyncio.wait_for(task, 3)
    finally:
        await collect_tasks(task)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def wall(self):
        return 1_800_000_000 + self.now

    def milliseconds(self):
        return int(self.wall() * 1000)

    def advance(self, seconds):
        self.now += seconds


class Socket:
    """Queue transport; received frames are parsed only by the production loop."""

    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = []
        self.receives = 0

    async def send(self, frame):
        self.sent.append(frame)

    async def recv(self):
        self.receives += 1
        frame = await self.frames.get()
        if isinstance(frame, Exception):
            raise frame
        return frame


class Transport:
    def __init__(self):
        self.sockets = []
        self.reconnect = asyncio.Event()
        self.backoffs = []

    @asynccontextmanager
    async def connect(self, url, **kwargs):
        assert url == MARKET_URL
        assert kwargs["proxy"] is None and kwargs["ping_interval"] is None
        socket = Socket()
        self.sockets.append(socket)
        yield socket

    async def sleep(self, delay):
        self.backoffs.append(delay)
        await self.reconnect.wait()
        self.reconnect.clear()


class MetadataHTTP:
    def __init__(self):
        self.payload = json.loads(FIXTURE.read_text())
        self.status = 200
        self.calls = []

    async def handle(self, request):
        assert request.method == "GET"
        assert not any(key.lower().startswith("poly_") for key in request.headers)
        assert "authorization" not in request.headers
        self.calls.append(request)
        if request.url.host == "gamma-api.polymarket.com":
            assert request.url.path == "/markets"
            assert request.url.params["condition_ids"] == MARKET
            payload = self.payload["gamma"]
        else:
            assert request.url.host == "clob.polymarket.com"
            assert request.url.path == "/clob-markets/" + MARKET
            payload = self.payload["clob"]
        return httpx.Response(self.status, json=payload)


class FinancialVenue:
    """Only financial boundary is fabricated; checks real persisted dispatches."""

    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        self.calls, self.prepared, self.submitted = [], [], []
        self.observations = {}
        self.cancel_pending = False
        self.cancel_fill = D(0)
        self.lookup_entered = asyncio.Event()
        self.lookup_release = None
        self.after_submit = None
        self.maximum_committed = 0

    async def account_snapshot(self, account):
        self.calls.append(("snapshot", account))
        return AccountSnapshot(account, OWNER, self.clock.milliseconds(), D(100), D(100),
                               D(0), (), D(0), True, True)

    async def permission(self, account, session):
        return Permission(account, session, self.clock.milliseconds(),
                          self.clock.milliseconds() + 120_000, True, True)

    async def heartbeat(self, account, session):
        self.calls.append(("heartbeat", session))

    async def prepare(self, account, session, intent, request):
        self.calls.append(("prepare", intent))
        order = PreparedOrder(account, session, intent,
                              "0x" + hashlib.sha256(intent.encode()).hexdigest(), request)
        self.prepared.append(order)
        return order

    async def submit(self, order):
        self.calls.append(("submit", order.order_hash))
        rows = self.store.orders(order.account)
        row = next(row for row in rows if row["intent_id"] == order.intent_id)
        assert row["state"] == "dispatching" and row["cost"] == order.request.cost
        assert order.order_hash not in self.observations, "Duplicate financial dispatch"
        self.maximum_committed = max(self.maximum_committed,
                                     self.store.status(order.account)["committed_micro"])
        self.submitted.append(order)
        self.observations[order.order_hash] = OrderObservation(
            order.account, order.session_id, order.order_hash, self.clock.milliseconds(),
            "open", D(0), D(0))
        if self.after_submit is not None:
            await self.after_submit(order)
        return order.order_hash

    async def lookup(self, account, session, order_hash):
        self.calls.append(("lookup", order_hash))
        if self.lookup_release is not None:
            self.lookup_entered.set()
            await self.lookup_release.wait()
        observation = self.observations.get(order_hash)
        return replace(observation, observed_at_ms=self.clock.milliseconds()) if observation else None

    async def cancel(self, account, session, order_hash):
        self.calls.append(("cancel", order_hash))
        self.observations[order_hash] = OrderObservation(
            account, session, order_hash, self.clock.milliseconds(), "canceled",
            self.cancel_fill, D(0) if self.cancel_pending else self.cancel_fill,
            not self.cancel_pending)


class Replay:
    def __init__(self, path):
        self.clock, self.transport, self.http = Clock(), Transport(), MetadataHTTP()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.http.handle))
        self.store = ExecutionStore(path, clock=self.clock.milliseconds)
        self.store.register_account(AccountPolicy(OWNER, ACCOUNT, D(20), D(3), D(20), D(10)))
        self.venue = FinancialVenue(self.store, self.clock)
        self.engine = LiveEngine(self.store, venue=self.venue, worker_id="replay", timeout_seconds=2)
        self.feed = SharedMarketFeed(TOKENS, connector=self.transport.connect,
                                    clock=self.clock, wall_clock=self.clock.wall,
                                    sleep=self.transport.sleep, jitter=lambda: 0)
        self.policy = QuotePolicy(MARKET, *TOKENS, D(10), D("0.02"), D(3))
        self.supervisors = {}
        self.add_agent("agent")
        self.task = None

    def add_agent(self, agent):
        self.store.register_agent(AgentPolicy(ACCOUNT, agent, agent + "_session", MARKET,
                                              TOKENS, D(20), D(3), D(20)))
        supervisor = QuoteSupervisor(self.engine, ACCOUNT, agent, self.feed, self.policy,
                                     metadata_fetcher=self.metadata, clock=self.clock)
        self.supervisors[agent] = supervisor
        return supervisor

    @property
    def supervisor(self):
        return self.supervisors["agent"]

    @property
    def socket(self):
        return self.transport.sockets[-1]

    async def metadata(self, selected):
        return await fetch_market_metadata(selected, client=self.client, clock=self.clock)

    async def setup(self):
        self.task = asyncio.create_task(self.feed.run())
        await until(lambda: self.feed.connected and self.socket.receives > 0)
        assert json.loads(self.socket.sent[0]) == {"assets_ids": list(TOKENS), "type": "market"}
        await self.refresh_metadata()

    async def refresh_metadata(self):
        metadata = await self.metadata(MARKET)
        for supervisor in self.supervisors.values():
            supervisor.metadata = metadata

    async def frame(self, payload):
        socket = self.socket
        receives = socket.receives
        await socket.frames.put(payload if isinstance(payload, (str, Exception)) else json.dumps(payload))
        await until(lambda: socket.receives > receives or not self.feed.connected)

    def book(self, token, bid="0.49", ask="0.51", *, lag=0):
        return {"event_type": "book", "asset_id": token,
                "timestamp": str(self.clock.milliseconds() - int(lag * 1000)),
                "tick_size": "0.01", "bids": [{"price": bid, "size": "100"}],
                "asks": [{"price": ask, "size": "100"}]}

    async def books(self, bid="0.49", ask="0.51", *, lag=0):
        # Exercise the real application-level heartbeat too; PONG itself must
        # never make a book fresh. Required for the >25 logical-second soak.
        await self.frame("PONG")
        await self.frame([self.book(token, bid, ask, lag=lag) for token in TOKENS])

    async def price_change(self, old_bid, old_ask, new_bid, new_ask):
        changes = []
        for token in TOKENS:
            for side, price, size in (("BUY", old_bid, "0"), ("BUY", new_bid, "100"),
                                      ("SELL", old_ask, "0"), ("SELL", new_ask, "100")):
                changes.append({"asset_id": token, "side": side, "price": price, "size": size,
                                "best_bid": new_bid, "best_ask": new_ask})
        await self.frame({"event_type": "price_change", "timestamp": str(self.clock.milliseconds()),
                          "price_changes": changes})

    async def start_pair(self):
        await self.books()
        await self.engine.start(ACCOUNT, "agent")
        result = await self.supervisor.step()
        assert result.state == "orders_submitted"
        assert len(self.venue.submitted) == 2

    async def close(self):
        try:
            await self.feed.close()
        finally:
            try:
                await collect_tasks(self.task)
            finally:
                await self.client.aclose()


@pytest.fixture
async def replay(tmp_path):
    replay = Replay(tmp_path / "replay.sqlite")
    try:
        await replay.setup()
        yield replay
    finally:
        await replay.close()


async def test_socket_updates_reach_real_planner_supervisor_and_ledger(replay):
    r = replay
    await r.books()
    expected = plan_current(r.supervisor.metadata, r.policy, r.feed, now=r.clock())
    await r.engine.start(ACCOUNT, "agent")
    stop = asyncio.Event()
    task = asyncio.create_task(r.supervisor.run(stop))
    try:
        await until(lambda: len(r.venue.submitted) == 2)
        assert [(o.request.price, o.request.size) for o in r.venue.submitted] == [
            (q.price, q.size) for q in expected]
        r.clock.advance(2)
        await r.price_change("0.49", "0.51", "0.39", "0.41")
        await until(lambda: len(r.venue.submitted) == 4)
        assert {o.request.price for o in r.venue.submitted[2:]} == {D("0.38")}
        assert len(r.store.orders(ACCOUNT, active_only=True)) == 2
    finally:
        await stop_runner(stop, task)
    assert not r.store.orders(ACCOUNT, active_only=True)
    assert r.feed.connected  # One agent does not own the shared transport.


async def test_exact_duplicate_socket_frames_cannot_keep_standing_quotes_fresh(replay):
    r = replay
    await r.start_pair()
    duplicate = [r.book(token) for token in TOKENS]
    before = r.feed.revision
    r.clock.advance(4)
    await r.frame(duplicate)
    assert r.feed.revision == before
    r.clock.advance(1.001)
    await r.frame(duplicate)
    result = await r.supervisor.step()
    assert result.reason == "stale_or_offline"
    assert len(r.venue.submitted) == 2
    assert not r.store.orders(ACCOUNT, active_only=True)


async def test_out_of_order_wire_delta_invalidates_and_cancels_active_pair(replay):
    r = replay
    await r.start_pair()
    await r.frame({"event_type": "price_change", "timestamp": str(r.clock.milliseconds() - 1),
                   "price_changes": [{"asset_id": "123", "side": "BUY", "price": "0.49", "size": "80"}]})
    await until(lambda: r.feed.last_error == "out_of_order_event")
    assert not r.feed.connected
    assert (await r.supervisor.step()).reason == "stale_or_offline"
    assert not r.store.orders(ACCOUNT, active_only=True)


async def test_recently_received_but_delayed_exchange_frames_never_sign(replay):
    r = replay
    await r.books(lag=6)
    await r.engine.start(ACCOUNT, "agent")
    assert r.feed.connected  # Within parser bounds, outside trading freshness.
    assert (await r.supervisor.step()).reason == "stale_or_offline"
    assert not r.venue.prepared and not r.store.orders(ACCOUNT)


async def test_reconnect_requires_both_new_generation_snapshots_and_explicit_restart(replay):
    r = replay
    await r.start_pair()
    generation = r.feed.generation
    await r.frame(ConnectionError("synthetic disconnect"))
    assert (await r.supervisor.step()).state == "paused"
    r.transport.reconnect.set()
    await until(lambda: r.feed.connected and r.feed.generation > generation and r.socket.receives > 0)
    await r.frame(r.book("123"))
    with pytest.raises(QuoteUnavailable, match="Both current outcome books"):
        plan_current(r.supervisor.metadata, r.policy, r.feed, now=r.clock())
    assert not r.store.orders(ACCOUNT, active_only=True)
    await r.frame(r.book("456"))
    assert len(plan_current(r.supervisor.metadata, r.policy, r.feed, now=r.clock())) == 2
    assert (await r.supervisor.step()).state == "paused"
    assert len(r.venue.submitted) == 2
    r.clock.advance(2)
    await r.books()
    await r.engine.start(ACCOUNT, "agent")
    assert (await r.supervisor.step()).state == "orders_submitted"
    assert len(r.venue.submitted) == 4


async def test_current_books_do_not_override_expired_market_metadata(replay):
    r = replay
    await r.start_pair()
    for _ in range(4):
        r.clock.advance(8)
        await r.books()
    assert (await r.supervisor.step()).reason == "stale_metadata"
    assert not r.store.orders(ACCOUNT, active_only=True)
    assert len(r.venue.submitted) == 2


async def test_failed_real_metadata_http_refresh_halts_and_cancels(replay):
    r = replay
    await r.start_pair()
    r.http.status = 503
    calls = len(r.http.calls)
    stop = asyncio.Event()
    task = asyncio.create_task(r.supervisor.run(stop))
    try:
        await until(lambda: len(r.http.calls) >= calls + 2 and r.supervisor.metadata is None)
        await r.books()
        await until(lambda: not r.store.orders(ACCOUNT, active_only=True))
        assert r.store.agent(ACCOUNT, "agent")["state"] == "paused"
        assert len(r.venue.submitted) == 2
    finally:
        await stop_runner(stop, task)


async def test_slow_account_lookup_cannot_keep_heartbeats_alive_on_stale_socket_books(replay):
    r = replay
    await r.start_pair()
    r.clock.advance(2)
    await r.books("0.39", "0.41")
    r.venue.lookup_release = asyncio.Event()
    stop = asyncio.Event()
    runner = None
    slow_step = asyncio.create_task(r.supervisor.step())
    try:
        await asyncio.wait_for(r.venue.lookup_entered.wait(), 3)
        r.clock.advance(5.001)
        heartbeat_count = sum(call[0] == "heartbeat" for call in r.venue.calls)
        runner = asyncio.create_task(r.supervisor.run(stop))
        await until(lambda: r.supervisor.heartbeat_failed)
        assert sum(call[0] == "heartbeat" for call in r.venue.calls) == heartbeat_count
        r.venue.lookup_release.set()
        await asyncio.wait_for(slow_step, 3)
        await until(lambda: not r.store.orders(ACCOUNT, active_only=True))
        assert r.store.agent(ACCOUNT, "agent")["state"] == "paused"
        assert len(r.venue.submitted) == 2
    finally:
        r.venue.lookup_release.set()
        try:
            await collect_tasks(slow_step)
        finally:
            await stop_runner(stop, runner)


async def test_socket_burst_preserves_pair_cadence_and_exact_per_order_caps(replay):
    r = replay
    await r.start_pair()
    for index in range(60):
        r.clock.advance(0.01)
        bid, ask = ("0.39", "0.41") if index % 2 else ("0.49", "0.51")
        await r.frame([r.book(token, bid, ask) for token in TOKENS])
        await r.supervisor.step()
    assert len(r.venue.submitted) == len(r.store.orders(ACCOUNT)) == 2
    r.clock.advance(2)
    await r.books("0.39", "0.41")
    assert (await r.supervisor.step()).state == "orders_submitted"
    assert len({o.intent_id for o in r.venue.submitted}) == 4
    assert all(o.request.cost <= 3_000_000 for o in r.venue.submitted)
    assert len(r.store.orders(ACCOUNT, active_only=True)) == 2


async def test_cancel_ack_and_pending_fill_hold_funds_before_any_replacement(replay):
    r = replay
    await r.start_pair()
    old = list(r.venue.submitted)
    r.venue.cancel_fill, r.venue.cancel_pending = D(2), True
    r.clock.advance(2)
    await r.books("0.39", "0.41")
    assert (await r.supervisor.step()).state == "cancel_pending"
    assert len(r.venue.prepared) == 2
    assert r.store.status(ACCOUNT)["committed_micro"] == sum(o.request.cost for o in old)
    for order in old:
        observation = r.venue.observations[order.order_hash]
        r.venue.observations[order.order_hash] = replace(observation, confirmed_size=D(2), final=True)
    r.venue.cancel_pending = False
    r.clock.advance(2)
    await r.books("0.39", "0.41")
    assert (await r.supervisor.step()).state == "orders_submitted"
    terminal = [row for row in r.store.orders(ACCOUNT) if row["terminal"]]
    assert len(terminal) == 2 and sum(row["committed"] for row in terminal) == 1_920_000
    assert len(r.venue.submitted) == 4


async def test_real_transport_disconnect_after_first_ack_blocks_second_dispatch(replay):
    r = replay
    await r.books()
    await r.engine.start(ACCOUNT, "agent")

    async def disconnect(_):
        await r.frame(ConnectionError("synthetic disconnect after accepted order"))

    r.venue.after_submit = disconnect
    assert (await r.supervisor.step()).reason == "market_not_ready"
    assert len(r.venue.submitted) == 1
    assert not r.store.orders(ACCOUNT, active_only=True)


async def test_logical_soak_replaces_two_agents_without_leaking_account_reservations(replay):
    """120 logical seconds, 60 changes, 240 accepted-and-canceled orders."""
    r = replay
    other = r.add_agent("other")
    await r.refresh_metadata()
    await r.books()
    for agent in r.supervisors:
        await r.engine.start(ACCOUNT, agent)
    for index in range(60):
        r.clock.advance(2)
        await r.refresh_metadata()
        bid, ask = ("0.39", "0.41") if index % 2 else ("0.49", "0.51")
        await r.books(bid, ask)
        for supervisor in (r.supervisor, other):
            assert (await supervisor.step()).state == "orders_submitted"
        active = r.store.orders(ACCOUNT, active_only=True)
        assert len(active) == 4
        assert r.store.status(ACCOUNT)["committed_micro"] == sum(row["cost"] for row in active)
        assert r.store.status(ACCOUNT)["committed_micro"] <= 20_000_000
        assert all(row["cost"] <= 3_000_000 for row in active)
    for agent in r.supervisors:
        assert (await r.engine.cancel_orders(ACCOUNT, agent)).state == "canceled"
    rows = r.store.orders(ACCOUNT)
    assert len(rows) == len(r.venue.submitted) == 240
    assert len({row["intent_id"] for row in rows}) == len({row["order_hash"] for row in rows}) == 240
    assert all(row["terminal"] and row["ever_accepted"] and row["committed"] == 0 for row in rows)
    assert r.store.status(ACCOUNT)["committed_micro"] == 0
    assert r.store.status(ACCOUNT)["pending_orders"] == 0
    assert r.venue.maximum_committed <= 20_000_000


async def test_multiple_agents_cannot_each_spend_the_same_account_cap(replay):
    r = replay
    # Three pairs fit under $20; the fourth must halt the whole account without
    # dispatching anything beyond that shared budget, then cancel the others.
    for agent in ("second", "third", "fourth"):
        r.add_agent(agent)
    await r.refresh_metadata()
    await r.books()
    for agent in r.supervisors:
        await r.engine.start(ACCOUNT, agent)
    for supervisor in list(r.supervisors.values())[:3]:
        assert (await supervisor.step()).state == "orders_submitted"
    result = await r.supervisors["fourth"].step()
    assert result.state == "paused" and result.reason == "capital_limit"
    assert len(r.venue.submitted) == 6
    assert r.venue.maximum_committed <= 20_000_000
    assert not r.store.orders(ACCOUNT, active_only=True)
    assert all(r.store.agent(ACCOUNT, agent)["state"] == "paused" for agent in r.supervisors)
