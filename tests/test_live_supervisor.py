"""Offline event-driven supervisor tests with real engine and SQLite state."""

import asyncio
from dataclasses import replace
from decimal import Decimal as D
import hashlib

import pytest

from oddsrail.live.contracts import AccountPolicy, AccountSnapshot, AgentPolicy, OrderObservation, Permission, PreparedOrder
from oddsrail.live.engine import LiveEngine
from oddsrail.live.market_feed import BookSnapshot
from oddsrail.live.observer import MarketMetadata
from oddsrail.live.quotes import QuotePolicy
from oddsrail.live.store import ExecutionBlocked, ExecutionStore
from oddsrail.live.supervisor import QuoteSupervisor


ACCOUNT, OWNER = "0x" + "1" * 40, "0x" + "2" * 40
MARKET = "0x" + "a" * 64


class Clock:
    now = 1_000.0

    def __call__(self):
        return self.now

    def wall(self):
        return 1_800_000_000_000 + int(self.now * 1000)


class Feed:
    """Immutable snapshots and asynchronous revision wake-ups; no socket."""

    def __init__(self, clock):
        self.clock = clock
        self.generation, self.revision = 1, 0
        self.connected = True
        self.close_calls = 0
        self.event = asyncio.Event()
        self.update()

    def update(self, bid="0.49", ask="0.51"):
        self.books = {token: BookSnapshot(token, ((D(bid), D("100")),), ((D(ask), D("100")),),
                                         D("0.01"), self.clock(), self.clock.wall(), self.generation)
                      for token in ("123", "456")}
        self.revision += 1
        old, self.event = self.event, asyncio.Event()
        old.set()

    def fresh_snapshot(self, token, max_age, now):
        book = self.books.get(token)
        return book if self.connected and book and 0 <= now - book.received_at <= max_age else None

    async def wait_for_update(self, revision, timeout):
        if revision != self.revision:
            return self.revision
        await asyncio.wait_for(self.event.wait(), timeout)
        return self.revision

    async def close(self):
        self.close_calls += 1


class Venue:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        self.calls, self.prepared, self.submitted = [], [], []
        self.observations = {}
        self.cancel_final = True
        self.cancel_fill = D("0")
        self.on_prepare = None
        self.heartbeat_error = False
        self.snapshot_changes = {}
        self.permission_changes = {}

    async def account_snapshot(self, account):
        self.calls.append(("snapshot", account))
        return replace(AccountSnapshot(account, OWNER, self.clock.wall(), D("100"), D("100"),
                                       D("0"), (), D("0"), True, True), **self.snapshot_changes)

    async def permission(self, account, session):
        return replace(Permission(account, session, self.clock.wall(), self.clock.wall() + 120_000, True, True),
                       **self.permission_changes)

    async def heartbeat(self, account, session):
        self.calls.append(("heartbeat", session))
        if self.heartbeat_error:
            raise RuntimeError("offline fixture heartbeat failure")

    async def prepare(self, account, session, intent, request):
        self.calls.append(("prepare", intent))
        prepared = PreparedOrder(account, session, intent, "0x" + hashlib.sha256(intent.encode()).hexdigest(), request)
        self.prepared.append(prepared)
        if self.on_prepare:
            self.on_prepare()
        return prepared

    async def submit(self, prepared):
        self.calls.append(("submit", prepared.intent_id))
        row = next(row for row in self.store.orders(ACCOUNT) if row["intent_id"] == prepared.intent_id)
        assert row["state"] == "dispatching"
        self.submitted.append(prepared)
        self.observations[prepared.order_hash] = OrderObservation(
            prepared.account, prepared.session_id, prepared.order_hash, self.clock.wall(), "open", D("0"), D("0"))
        return prepared.order_hash

    async def lookup(self, account, session, order_hash):
        self.calls.append(("lookup", order_hash))
        result = self.observations.get(order_hash)
        return replace(result, observed_at_ms=self.clock.wall()) if result else None

    async def cancel(self, account, session, order_hash):
        self.calls.append(("cancel", order_hash))
        if self.cancel_final:
            self.observations[order_hash] = OrderObservation(
                account, session, order_hash, self.clock.wall(), "canceled", self.cancel_fill, self.cancel_fill, True)


class Harness:
    def __init__(self, path):
        self.clock = Clock()
        self.store = ExecutionStore(path, clock=self.clock.wall)
        self.store.register_account(AccountPolicy(OWNER, ACCOUNT, D("100"), D("10"), D("100"), D("10")))
        self.store.register_agent(AgentPolicy(ACCOUNT, "agent", "session", MARKET, ("123", "456"),
                                              D("100"), D("10"), D("100")))
        self.venue = Venue(self.store, self.clock)
        # See the note in test_live_engine.py: the fake venue answers
        # instantly, so a short real deadline here only measures CI load.
        self.engine = LiveEngine(self.store, venue=self.venue, worker_id="worker", timeout_seconds=5.0)
        self.feed = Feed(self.clock)
        self.policy = QuotePolicy(MARKET, "123", "456", D("10"), D("0.02"), D("10"))
        self.metadata = MarketMetadata(MARKET, "123", "456", D("0.01"), D("5"), D("1"),
                                       D("0"), D("0"), True, True, True, False, False, True, False, self.clock())
        self.supervisor = QuoteSupervisor(self.engine, ACCOUNT, "agent", self.feed, self.policy, clock=self.clock)
        self.supervisor.metadata = self.metadata

    async def start(self):
        await self.engine.start(ACCOUNT, "agent")

    def advance(self, seconds, *, fresh_books=False):
        self.clock.now += seconds
        if fresh_books:
            self.feed.update()


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path / "supervisor.sqlite")


async def test_default_cadence_never_requotes_faster_than_two_seconds(h):
    await h.start()
    assert (await h.supervisor.step()).state == "orders_submitted"
    h.advance(0.5)
    h.feed.update("0.59", "0.61")
    await h.supervisor.step()
    h.advance(1.499)
    await h.supervisor.step()
    assert len(h.venue.submitted) == 2
    h.advance(0.001)
    assert (await h.supervisor.step()).state == "orders_submitted"
    assert len(h.venue.submitted) == 4


async def test_unchanged_pair_does_not_sign_or_submit_again(h):
    await h.start()
    await h.supervisor.step()
    h.advance(2, fresh_books=True)
    result = await h.supervisor.step()
    assert result.state == "quotes_unchanged"
    assert len(h.venue.prepared) == len(h.venue.submitted) == 2
    assert not [call for call in h.venue.calls if call[0] == "cancel"]


@pytest.mark.parametrize("problem", ["daily_loss", "permission", "cash", "eligibility"])
async def test_unchanged_quotes_are_canceled_when_account_or_permission_stops_being_ready(h, problem):
    await h.start()
    await h.supervisor.step()
    if problem == "daily_loss":
        h.venue.snapshot_changes["daily_loss"] = D("10")
    elif problem == "cash":
        h.venue.snapshot_changes["cash"] = D("0")
    elif problem == "eligibility":
        h.venue.snapshot_changes["eligible"] = False
    else:
        h.venue.permission_changes["active"] = False
    h.advance(2, fresh_books=True)
    result = await h.supervisor.step()
    assert result.state == "paused"
    assert h.store.agent(ACCOUNT, "agent")["state"] == "paused"
    assert not h.store.orders(ACCOUNT, active_only=True)
    assert len(h.venue.submitted) == 2


async def test_risk_checks_continue_every_two_seconds_with_slower_quote_cadence(h):
    h.supervisor = QuoteSupervisor(h.engine, ACCOUNT, "agent", h.feed, h.policy,
                                   clock=h.clock, min_quote_interval=60)
    h.supervisor.metadata = h.metadata
    await h.start()
    await h.supervisor.step()
    h.venue.permission_changes["active"] = False
    h.advance(2, fresh_books=True)
    result = await h.supervisor.step()
    assert result.state == "paused"
    assert not h.store.orders(ACCOUNT, active_only=True)
    assert len(h.venue.submitted) == 2


async def test_guard_rechecks_live_books_after_signing_before_dispatch(h):
    await h.start()
    h.venue.on_prepare = lambda: setattr(h.feed, "connected", False)
    result = await h.supervisor.step()
    assert result.state == "paused" and result.reason == "market_not_ready"
    assert len(h.venue.prepared) == 2
    assert h.venue.submitted == []
    assert h.store.status(ACCOUNT)["committed_micro"] == 0


async def test_second_dispatch_is_blocked_if_feed_disconnects_after_first_submit(h):
    await h.start()
    original = h.venue.submit

    async def disconnect_after_submit(prepared):
        result = await original(prepared)
        h.feed.connected = False
        return result

    h.venue.submit = disconnect_after_submit
    result = await h.supervisor.step()
    assert result.state == "paused" and result.reason == "market_not_ready"
    assert len(h.venue.submitted) == 1
    assert len([call for call in h.venue.calls if call[0] == "cancel"]) == 1
    assert not h.store.orders(ACCOUNT, active_only=True)


async def test_verified_taker_only_market_can_use_nonzero_reported_base_fee(h):
    await h.start()
    h.supervisor.metadata = replace(h.metadata, maker_fee_bps=D("1000"),
                                    taker_fee_bps=D("1000"), maker_fee_free=True)
    assert (await h.supervisor.step()).state == "orders_submitted"
    assert len(h.venue.submitted) == 2


async def test_unverified_effective_maker_fee_never_signs(h):
    await h.start()
    h.supervisor.metadata = replace(h.metadata, maker_fee_free=False)
    result = await h.supervisor.step()
    assert result.state == "paused" and result.reason == "maker_fee_unsupported"
    assert h.venue.prepared == []


@pytest.mark.parametrize("problem", ["stale", "disconnected", "metadata", "closed"])
async def test_stale_offline_or_ineligible_data_halts_and_cancels_without_auto_resume(h, problem):
    await h.start()
    await h.supervisor.step()
    if problem == "stale":
        h.advance(5.001)
    elif problem == "disconnected":
        h.feed.connected = False
    elif problem == "metadata":
        h.supervisor.metadata = replace(h.metadata, observed_at=h.clock() - 30.001)
    else:
        h.supervisor.metadata = replace(h.metadata, closed=True)
    result = await h.supervisor.step()
    assert result.state == "paused"
    assert h.store.agent(ACCOUNT, "agent")["state"] == "paused"
    assert len([call for call in h.venue.calls if call[0] == "cancel"]) == 2
    assert not h.store.orders(ACCOUNT, active_only=True)
    h.feed.connected = True
    h.feed.update()
    h.supervisor.metadata = replace(h.metadata, observed_at=h.clock())
    assert (await h.supervisor.step()).state == "paused"
    assert len(h.venue.submitted) == 2


async def test_replacement_cancels_and_reconciles_both_before_signing_new_orders(h):
    await h.start()
    await h.supervisor.step()
    h.venue.cancel_fill = D("2")
    h.advance(2)
    h.feed.update("0.59", "0.61")
    old_hashes = {order.order_hash for order in h.venue.submitted}
    boundary = len(h.venue.calls)
    assert (await h.supervisor.step()).state == "orders_submitted"
    calls = h.venue.calls[boundary:]
    first_prepare = next(i for i, call in enumerate(calls) if call[0] == "prepare")
    assert {call[1] for call in calls[:first_prepare] if call[0] == "cancel"} == old_hashes
    terminal_rows = [row for row in h.store.orders(ACCOUNT) if row["terminal"]]
    assert len(terminal_rows) == 2
    assert sum(row["committed"] for row in terminal_rows) == 1_920_000
    assert len(h.venue.submitted) == 4


async def test_cancel_ack_with_open_lookup_blocks_replacement_signing(h):
    await h.start()
    await h.supervisor.step()
    h.venue.cancel_final = False
    h.advance(2)
    h.feed.update("0.59", "0.61")
    result = await h.supervisor.step()
    assert result.state == "cancel_pending"
    assert len(h.venue.prepared) == len(h.venue.submitted) == 2
    assert h.store.status(ACCOUNT)["committed_micro"] == 9_600_000


async def test_pause_during_replacement_gap_is_never_undone(h):
    await h.start()
    await h.supervisor.step()
    original = h.engine.submit_pair

    async def pause_before_submit(*args, **kwargs):
        await h.engine.pause(ACCOUNT, "agent")
        return await original(*args, **kwargs)

    h.engine.submit_pair = pause_before_submit
    h.advance(2)
    h.feed.update("0.59", "0.61")
    result = await h.supervisor.step()
    assert result.state == "paused"
    assert h.store.agent(ACCOUNT, "agent")["state"] == "paused"
    assert len(h.venue.submitted) == 2


async def test_heartbeat_failure_cancels_and_blocks_new_signing(h):
    await h.start()
    await h.supervisor.step()
    h.venue.heartbeat_error = True
    with pytest.raises(ExecutionBlocked, match="heartbeat_unavailable"):
        await h.engine.heartbeat(ACCOUNT, "agent")
    h.supervisor.heartbeat_failed = True
    h.advance(2, fresh_books=True)
    assert (await h.supervisor.step()).state == "paused"
    assert len(h.venue.submitted) == 2
    assert not h.store.orders(ACCOUNT, active_only=True)


async def test_run_warms_up_metadata_without_signing_or_starting_and_keeps_shared_feed_open(h):
    entered, release = asyncio.Event(), asyncio.Event()

    async def metadata(_):
        entered.set()
        await release.wait()
        return h.metadata

    h.supervisor.metadata = None
    h.supervisor.metadata_fetcher = metadata
    stop = asyncio.Event()
    task = asyncio.create_task(h.supervisor.run(stop))
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0)
    assert not h.venue.prepared
    assert h.store.agent(ACCOUNT, "agent")["state"] == "paused"
    release.set()
    h.feed.update()
    await asyncio.sleep(0)
    stop.set()
    h.feed.update()
    await asyncio.wait_for(task, 1)
    assert not h.venue.prepared
    assert h.feed.close_calls == 0


async def test_run_consumes_event_without_waiting_for_hourly_scheduler_and_shutdown_cancels(h):
    await h.start()

    async def metadata(_):
        return h.metadata

    h.supervisor.metadata_fetcher = metadata
    stop = asyncio.Event()
    task = asyncio.create_task(h.supervisor.run(stop))
    # Engine work is all local here; bounded cooperative polling observes completion.
    async with asyncio.timeout(1):
        while len(h.venue.submitted) < 2:
            await asyncio.sleep(0)
    stop.set()
    h.feed.update()
    await asyncio.wait_for(task, 1)
    assert len(h.venue.submitted) == 2
    assert not h.store.orders(ACCOUNT, active_only=True)
    assert h.feed.close_calls == 0


async def test_run_loop_heartbeat_failure_is_latched_without_new_quotes(h):
    await h.start()
    await h.supervisor.step()
    h.venue.heartbeat_error = True

    async def metadata(_):
        return h.metadata

    h.supervisor.metadata_fetcher = metadata
    stop = asyncio.Event()
    task = asyncio.create_task(h.supervisor.run(stop))
    async with asyncio.timeout(1):
        while not h.supervisor.heartbeat_failed:
            await asyncio.sleep(0)
    stop.set()
    h.feed.update()
    await asyncio.wait_for(task, 1)
    assert len(h.venue.submitted) == 2
    assert h.store.agent(ACCOUNT, "agent")["state"] == "paused"
    assert not h.store.orders(ACCOUNT, active_only=True)
    assert h.feed.close_calls == 0


async def test_metadata_fetch_failure_cancels_existing_orders(h):
    await h.start()
    await h.supervisor.step()
    entered = asyncio.Event()

    async def metadata(_):
        entered.set()
        raise ValueError("fixture metadata failure")

    h.supervisor.metadata_fetcher = metadata
    stop = asyncio.Event()
    task = asyncio.create_task(h.supervisor.run(stop))
    await asyncio.wait_for(entered.wait(), 1)
    h.feed.update()
    async with asyncio.timeout(1):
        while h.store.orders(ACCOUNT, active_only=True):
            await asyncio.sleep(0)
    assert h.store.agent(ACCOUNT, "agent")["state"] == "paused"
    stop.set()
    h.feed.update()
    await asyncio.wait_for(task, 1)
    assert len(h.venue.submitted) == 2
    assert h.feed.close_calls == 0


async def test_rapid_feed_updates_do_not_accumulate_order_pairs(h):
    await h.start()
    await h.supervisor.step()
    for _ in range(30):
        h.advance(0.01)
        h.feed.update("0.59", "0.61")
        await h.supervisor.step()
    assert len(h.venue.submitted) == 2
    assert len(h.store.orders(ACCOUNT)) == 2


async def test_concurrent_steps_cannot_duplicate_financial_submission(h):
    await h.start()
    results = await asyncio.gather(*(h.supervisor.step() for _ in range(20)), return_exceptions=True)
    assert all(not isinstance(result, BaseException) or
               isinstance(result, ExecutionBlocked) and str(result) == "account_busy"
               for result in results)
    assert len(h.venue.submitted) == 2
    assert len(h.store.orders(ACCOUNT)) == 2


async def test_user_pause_keeps_orders_distinct_from_account_error_halt(h):
    await h.start()
    await h.supervisor.step()
    await h.engine.pause(ACCOUNT, "agent")
    h.advance(2, fresh_books=True)
    assert (await h.supervisor.step()).state == "paused"
    assert len(h.store.orders(ACCOUNT, active_only=True)) == 2
    assert not [call for call in h.venue.calls if call[0] == "cancel"]


async def test_account_error_halt_cancels_other_agents_outstanding_orders(h):
    h.store.register_agent(AgentPolicy(ACCOUNT, "other", "other_session", MARKET, ("123", "456"),
                                      D("100"), D("10"), D("100")))
    await h.start()
    await h.engine.start(ACCOUNT, "other")
    await h.supervisor.step()
    other = QuoteSupervisor(h.engine, ACCOUNT, "other", h.feed, h.policy, clock=h.clock)
    other.metadata = h.metadata
    await other.step()
    assert len(h.venue.submitted) == 4
    await h.engine.halt_and_cancel(ACCOUNT, "agent", "daily_loss_limit")
    await other.step()
    assert not h.store.orders(ACCOUNT, active_only=True)


@pytest.mark.parametrize("fields", [
    {"min_quote_interval": True}, {"min_quote_interval": False},
    {"min_quote_interval": 1}, {"min_quote_interval": 1.999},
    {"min_quote_interval": 61}, {"min_quote_interval": float("nan")},
    {"min_quote_interval": "2"}, {"metadata_interval": True},
    {"metadata_interval": 0}, {"metadata_interval": 21},
    {"metadata_interval": float("inf")},
])
def test_cadence_must_be_finite_with_two_second_quote_minimum(h, fields):
    with pytest.raises(ValueError, match="cadence"):
        QuoteSupervisor(h.engine, ACCOUNT, "agent", h.feed, h.policy, **fields)


async def test_a_venue_too_slow_to_verify_risk_pauses_the_agent(h):
    """The conservative behaviour, asserted deliberately rather than by accident.

    An unverifiable account is a halt, not a guess. This used to arrive by
    luck: the offline fixtures put a 100 ms real deadline on a fake venue, so
    a loaded CI runner could trip it inside an unrelated test and pause the
    agent there instead.
    """
    await h.start()
    await h.supervisor.step()
    h.engine.timeout_seconds = 0.05
    answered = asyncio.Event()
    original = h.venue.account_snapshot

    async def too_slow(account):
        answered.set()
        await asyncio.sleep(0.2)                      # past the deadline, on purpose
        return await original(account)

    h.venue.account_snapshot = too_slow
    h.advance(2, fresh_books=True)
    result = await h.supervisor.step()
    assert answered.is_set(), "the venue was actually consulted"
    assert result.state == "paused"
    assert h.store.agent(ACCOUNT, "agent")["state"] == "paused"
    assert not h.store.orders(ACCOUNT, active_only=True), "resting orders are cancelled, not left behind"
    assert h.store.status(ACCOUNT)["blocked"], "the account is halted until an operator looks at it"
