"""Offline execution integration tests: fake venue, real durable SQLite ledger.

The fake adapter never signs keys or opens a network connection. An acknowledged
cancel is intentionally insufficient to release an order unless lookup supplies
an authoritative terminal observation.
"""

import asyncio
from dataclasses import replace
from decimal import Decimal as D
import hashlib

import pytest

from oddsrail.live.contracts import (AccountPolicy, AccountSnapshot, AgentPolicy,
                                    OrderObservation, OrderRequest, Permission, PreparedOrder)
from oddsrail.live.engine import LiveEngine
from oddsrail.live.store import ExecutionBlocked, ExecutionStore


OWNER = "0x" + "1" * 40
ACCOUNT = "0x" + "2" * 40
OTHER_ACCOUNT = "0x" + "3" * 40
MARKET = "0x" + "a" * 64
AGENT = "agent-one"
SESSION = "session-one"
NOW = 1_800_000_000_000


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def pair():
    return tuple(OrderRequest(MARKET, token, D("0.48"), D("10"), D("0.01"), D("5"))
                 for token in ("123", "456"))


class FakeVenue:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        self.calls = []
        self.prepared = []
        self.submitted = []
        self.observations = {}
        self.snapshot_changes = {}
        self.permission_changes = {}
        self.cancel_final = False
        self.heartbeat_error = None

    async def account_snapshot(self, account):
        self.calls.append(("account_snapshot", account))
        return replace(AccountSnapshot(account, OWNER, self.clock(), D("100"), D("100"), D(0), (), D(0), True, True),
                       **self.snapshot_changes)

    async def permission(self, account, session):
        self.calls.append(("permission", account, session))
        return replace(Permission(account, session, self.clock(), self.clock() + 3_600_000, True, True),
                       **self.permission_changes)

    async def heartbeat(self, account, session):
        self.calls.append(("heartbeat", account, session))
        if self.heartbeat_error:
            raise self.heartbeat_error

    async def prepare(self, account, session, intent, request):
        self.calls.append(("prepare", account, session, intent))
        order_hash = "0x" + hashlib.sha256((account + session + intent).encode()).hexdigest()
        prepared = PreparedOrder(account, session, intent, order_hash, request)
        self.prepared.append(prepared)
        return prepared

    async def submit(self, prepared):
        self.calls.append(("submit", prepared.account, prepared.session_id, prepared.order_hash))
        rows = self.store.orders(prepared.account)
        row = next(row for row in rows if row["intent_id"] == prepared.intent_id)
        # The durable dispatch mark and exact hash precede the financial call.
        assert row["state"] == "dispatching" and row["order_hash"] == prepared.order_hash
        assert self.store.request(row) == prepared.request
        self.submitted.append(prepared)
        self.observations[(prepared.account, prepared.session_id, prepared.order_hash)] = OrderObservation(
            prepared.account, prepared.session_id, prepared.order_hash, self.clock(), "open", D(0), D(0))
        return prepared.order_hash

    async def lookup(self, account, session, order_hash):
        self.calls.append(("lookup", account, session, order_hash))
        return self.observations.get((account, session, order_hash))

    async def cancel(self, account, session, order_hash):
        self.calls.append(("cancel", account, session, order_hash))
        if self.cancel_final:
            self.observations[(account, session, order_hash)] = OrderObservation(
                account, session, order_hash, self.clock(), "canceled", D(0), D(0), True)


def register(store, *, account=ACCOUNT, agent=AGENT, session=SESSION):
    store.register_account(AccountPolicy(OWNER, account, D("100"), D("10"), D("50"), D("10")))
    store.register_agent(AgentPolicy(account, agent, session, MARKET, ("123", "456"),
                                     D("40"), D("10"), D("40")))


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    store = ExecutionStore(tmp_path / "execution.sqlite", clock=clock)
    register(store)
    venue = FakeVenue(store, clock)
    # A real deadline on a fake venue that answers instantly measures the CI
    # runner, not this code. Tests that exercise a hanging venue shorten it
    # themselves; everything else gets a deadline jitter cannot reach.
    engine = LiveEngine(store, venue=venue, worker_id="worker-one", timeout_seconds=5.0)
    return engine, venue, store, clock


async def start(setup):
    engine, venue, store, clock = setup
    result = await engine.start(ACCOUNT, AGENT)
    assert result.state == "running"
    return setup


async def submit(engine, batch="batch-one", *, agent=AGENT, account=ACCOUNT, guard=lambda _: True):
    return await engine.submit_pair(account, agent, batch, pair(), market_guard=guard)


async def test_default_engine_cannot_activate_or_submit(setup):
    _, _, store, _ = setup
    engine = LiveEngine(store)
    for operation in (engine.start(ACCOUNT, AGENT), submit(engine)):
        with pytest.raises(ExecutionBlocked, match="delegated_adapter_not_configured"):
            await operation
    assert store.agent(ACCOUNT, AGENT)["state"] == "paused"
    assert store.orders(ACCOUNT) == []


async def test_pair_is_durably_reserved_and_bound_before_exactly_once_submission(setup):
    engine, venue, store, _ = await start(setup)
    result = await submit(engine)
    assert result.state == "orders_submitted"
    assert len(result.intent_ids) == len(venue.prepared) == len(venue.submitted) == 2
    rows = store.orders(ACCOUNT)
    assert {row["state"] for row in rows} == {"open"}
    assert store.status(ACCOUNT)["committed_micro"] == 9_600_000
    assert sum(call[0] == "permission" for call in venue.calls) == 4  # Start, reserve, each dispatch.
    assert all(order.account == ACCOUNT and order.session_id == SESSION for order in venue.submitted)


@pytest.mark.parametrize("snapshot,permission,reason", [
    ({"complete": False}, {}, "account_not_ready"),
    ({"eligible": False}, {}, "account_not_ready"),
    ({"account": OTHER_ACCOUNT}, {}, "account_not_ready"),
    ({"owner": OTHER_ACCOUNT}, {}, "account_not_ready"),
    ({"captured_at_ms": NOW - 10_001}, {}, "account_not_ready"),
    ({"captured_at_ms": NOW + 1}, {}, "account_not_ready"),
    ({"cash": D(0)}, {}, "insufficient_verified_funds"),
    ({"allowance": D(0)}, {}, "insufficient_verified_funds"),
    # Authoritative monetary inputs must already be exact microdollars.
    ({"cash": D("9.599999999999")}, {}, "venue_operation_failed"),
    ({"allowance": D("9.599999999999")}, {}, "venue_operation_failed"),
    ({"external_reserved": D("99")}, {}, "insufficient_verified_funds"),
    ({"daily_loss": D("10")}, {}, "daily_loss_limit"),
    ({}, {"active": False}, "permission_not_ready"),
    ({}, {"can_trade": False}, "permission_not_ready"),
    ({}, {"session_id": "another-session"}, "permission_not_ready"),
    ({}, {"account": OTHER_ACCOUNT}, "permission_not_ready"),
    ({}, {"verified_at_ms": NOW - 10_001}, "permission_not_ready"),
    ({}, {"expires_at_ms": NOW + 30_000}, "permission_not_ready"),
])
async def test_no_signing_or_submission_without_verified_permission_and_funds(setup, snapshot, permission, reason):
    engine, venue, store, _ = await start(setup)
    venue.snapshot_changes, venue.permission_changes = snapshot, permission
    result = await submit(engine)
    assert result.state == "paused" and result.reason == reason
    assert not venue.prepared and not venue.submitted and not store.orders(ACCOUNT)
    assert store.agent(ACCOUNT, AGENT)["state"] == "paused"


async def test_no_signing_or_submission_when_market_guard_is_false(setup):
    engine, venue, store, _ = await start(setup)
    result = await submit(engine, guard=lambda _: False)
    assert result.state == "paused" and result.reason == "market_not_ready"
    assert not venue.prepared and not venue.submitted and not store.orders(ACCOUNT)


@pytest.mark.parametrize("mismatch", ["account", "session", "intent", "request", "hash_collision"])
async def test_signer_must_bind_the_exact_request_account_session_and_intent(setup, mismatch):
    engine, venue, store, _ = await start(setup)
    original = venue.prepare

    async def malformed(*args):
        prepared = await original(*args)
        if mismatch == "account":
            return replace(prepared, account=OTHER_ACCOUNT)
        if mismatch == "session":
            return replace(prepared, session_id="other-session")
        if mismatch == "intent":
            return replace(prepared, intent_id="other-intent")
        if mismatch == "request":
            return replace(prepared, request=replace(prepared.request, size=D("11")))
        return replace(prepared, order_hash="0x" + "f" * 64)

    venue.prepare = malformed
    result = await submit(engine)
    assert result.state == "paused" and not venue.submitted
    assert store.status(ACCOUNT)["committed_micro"] == 0
    assert all(row["terminal"] and row["state"] == "rejected" for row in store.orders(ACCOUNT))


async def test_market_is_rechecked_after_signing_before_submission(setup):
    engine, venue, store, _ = await start(setup)
    answers = iter((True, False))
    result = await submit(engine, guard=lambda _: next(answers))
    assert result.reason == "market_not_ready" and len(venue.prepared) == 2 and not venue.submitted
    assert store.status(ACCOUNT)["committed_micro"] == 0


async def test_permission_is_rechecked_after_signing_before_submission(setup):
    engine, venue, store, _ = await start(setup)
    original = venue.prepare

    async def revoke_after_prepare(*args):
        prepared = await original(*args)
        venue.permission_changes["active"] = False
        return prepared

    venue.prepare = revoke_after_prepare
    result = await submit(engine)
    assert result.reason == "permission_not_ready" and len(venue.prepared) == 2 and not venue.submitted
    assert store.status(ACCOUNT)["committed_micro"] == 0


async def test_first_quote_accepted_second_market_check_fails_cancels_first(setup):
    engine, venue, store, _ = await start(setup)
    venue.cancel_final = True
    answers = iter((True, True, False))
    result = await submit(engine, guard=lambda _: next(answers))
    assert result.reason == "market_not_ready" and len(venue.submitted) == 1
    cancels = [call for call in venue.calls if call[0] == "cancel"]
    assert cancels == [("cancel", ACCOUNT, SESSION, venue.submitted[0].order_hash)]
    assert store.status(ACCOUNT)["committed_micro"] == 0
    assert {row["state"] for row in store.orders(ACCOUNT)} == {"canceled", "rejected"}


async def test_submission_timeout_is_not_retried_and_missing_lookup_retains_reservation(setup):
    engine, venue, store, _ = await start(setup)
    engine.timeout_seconds = 0.05          # this test waits for the deadline
    attempts = []

    async def timeout(prepared):
        attempts.append(prepared)
        await asyncio.Event().wait()

    venue.submit = timeout
    result = await submit(engine)
    assert result.state == "paused" and len(attempts) == 1
    pending = store.orders(ACCOUNT, active_only=True)
    assert len(pending) == 1 and pending[0]["state"] in {"unknown", "cancel_pending"}
    assert pending[0]["order_hash"] == attempts[0].order_hash
    assert store.status(ACCOUNT)["committed_micro"] == 4_800_000
    duplicate = await submit(engine)
    assert duplicate.state == "already_recorded" and len(attempts) == 1
    with pytest.raises(ExecutionBlocked, match="activation_verification_failed"):
        await engine.start(ACCOUNT, AGENT)
    await submit(engine, batch="new-batch")
    assert len(attempts) == 1


async def test_second_submit_timeout_cancels_accepted_sibling_without_releasing_unconfirmed(setup):
    engine, venue, store, _ = await start(setup)
    engine.timeout_seconds = 0.05          # this test waits for the deadline
    original = venue.submit
    attempts = []

    async def second_timeout(prepared):
        attempts.append(prepared)
        if len(attempts) == 2:
            await asyncio.Event().wait()
        return await original(prepared)

    venue.submit = second_timeout
    result = await submit(engine)
    assert result.state == "paused" and len(attempts) == 2 and len(venue.submitted) == 1
    assert {call[3] for call in venue.calls if call[0] == "cancel"} == {p.order_hash for p in attempts}
    assert store.status(ACCOUNT)["committed_micro"] == 9_600_000
    assert store.status(ACCOUNT)["pending_orders"] == 2


async def test_wrong_submission_ack_never_replaces_the_prepared_hash(setup):
    engine, venue, store, _ = await start(setup)
    attempts = []

    async def wrong_ack(prepared):
        attempts.append(prepared)
        return "0x" + "b" * 64

    venue.submit = wrong_ack
    result = await submit(engine)
    assert result.reason == "invalid_submission_ack" and len(attempts) == 1
    pending = store.orders(ACCOUNT, active_only=True)
    assert pending[0]["order_hash"] == attempts[0].order_hash
    assert store.status(ACCOUNT)["committed_micro"] == 4_800_000


async def test_cancel_ack_and_unsettled_fill_never_release_capital(setup):
    engine, venue, store, clock = await start(setup)
    await submit(engine)
    first, second = venue.submitted
    venue.observations[(ACCOUNT, SESSION, first.order_hash)] = OrderObservation(
        ACCOUNT, SESSION, first.order_hash, clock(), "canceled", D("3"), D("1"), False)
    result = await engine.cancel_orders(ACCOUNT, AGENT)
    assert result.state == "cancel_pending"
    assert store.status(ACCOUNT)["committed_micro"] == 9_600_000
    assert store.status(ACCOUNT)["pending_orders"] == 2
    venue.observations[(ACCOUNT, SESSION, first.order_hash)] = OrderObservation(
        ACCOUNT, SESSION, first.order_hash, clock(), "canceled", D("3"), D("3"), True)
    venue.observations[(ACCOUNT, SESSION, second.order_hash)] = OrderObservation(
        ACCOUNT, SESSION, second.order_hash, clock(), "canceled", D(0), D(0), True)
    result = await engine.cancel_orders(ACCOUNT, AGENT)
    assert result.state == "canceled" and store.status(ACCOUNT)["pending_orders"] == 0
    assert store.status(ACCOUNT)["committed_micro"] == 1_440_000  # Real fill remains committed.


async def test_partial_cancel_error_still_attempts_other_quote_and_preserves_both_holds(setup):
    engine, venue, store, _ = await start(setup)
    await submit(engine)
    calls = []

    async def partial(account, session, order_hash):
        calls.append(order_hash)
        if len(calls) == 1:
            raise OSError("fake transport failure")

    venue.cancel = partial
    result = await engine.cancel_orders(ACCOUNT, AGENT)
    assert result.state == "cancel_pending"
    # The failed agent pass may trigger a second account-wide cancellation
    # pass; both known hashes must be attempted and neither may be released.
    assert set(calls) == {prepared.order_hash for prepared in venue.submitted}
    assert store.status(ACCOUNT)["committed_micro"] == 9_600_000


async def test_user_pause_cancel_and_revocation_are_distinct(setup):
    engine, venue, store, _ = await start(setup)
    await submit(engine)
    before = len(venue.calls)
    paused = await engine.pause(ACCOUNT, AGENT)
    assert paused.state == "paused" and paused.reason == "existing_orders_unchanged"
    assert len(venue.calls) == before and store.status(ACCOUNT)["pending_orders"] == 2
    revoked = await engine.request_revocation(ACCOUNT, AGENT)
    assert revoked.state == "revocation_requested" and revoked.reason == "owner_authorization_required"
    assert len(venue.calls) == before and store.agent(ACCOUNT, AGENT)["state"] == "revocation_requested"
    with pytest.raises(ExecutionBlocked, match="activation_verification_failed"):
        await engine.start(ACCOUNT, AGENT)
    venue.cancel_final = True
    canceled = await engine.cancel_orders(ACCOUNT, AGENT)
    assert canceled.state == "canceled" and store.agent(ACCOUNT, AGENT)["state"] == "revocation_requested"


async def test_cancel_is_confined_to_named_agent_and_account(setup):
    engine, venue, store, _ = await start(setup)
    register(store, agent="agent-two", session="session-two")
    register(store, account=OTHER_ACCOUNT)
    await engine.start(ACCOUNT, "agent-two")
    await engine.start(OTHER_ACCOUNT, AGENT)
    await submit(engine)
    await submit(engine, agent="agent-two", batch="batch-two")
    await submit(engine, account=OTHER_ACCOUNT, batch="batch-three")
    venue.calls.clear()
    venue.cancel_final = True
    result = await engine.cancel_orders(ACCOUNT, AGENT)
    assert result.state == "canceled"
    assert all(call[1:3] == (ACCOUNT, SESSION) for call in venue.calls if call[0] in {"cancel", "lookup"})
    assert store.status(OTHER_ACCOUNT)["pending_orders"] == 2
    assert len(store.orders(ACCOUNT, agent_id="agent-two", active_only=True)) == 2


async def test_risk_halt_cancels_every_managed_session_but_no_other_account(setup):
    engine, venue, store, _ = await start(setup)
    register(store, agent="agent-two", session="session-two")
    register(store, account=OTHER_ACCOUNT)
    await engine.start(ACCOUNT, "agent-two")
    await engine.start(OTHER_ACCOUNT, AGENT)
    await submit(engine)
    await submit(engine, agent="agent-two", batch="batch-two")
    await submit(engine, account=OTHER_ACCOUNT, batch="batch-three")
    venue.calls.clear()
    venue.cancel_final = True
    result = await engine.halt_and_cancel(ACCOUNT, AGENT, "market_not_ready")
    assert result.state == "paused"
    cancels = [call for call in venue.calls if call[0] == "cancel"]
    assert len(cancels) == 4 and {call[2] for call in cancels} == {SESSION, "session-two"}
    assert all(call[1] == ACCOUNT for call in cancels)
    assert store.agent(ACCOUNT, AGENT)["state"] == store.agent(ACCOUNT, "agent-two")["state"] == "paused"
    assert store.status(ACCOUNT)["pending_orders"] == 0
    assert store.status(OTHER_ACCOUNT)["pending_orders"] == 2
    assert store.agent(OTHER_ACCOUNT, AGENT)["state"] == "running"


async def test_market_guard_race_failure_cancels_existing_quotes(setup):
    engine, venue, store, _ = await start(setup)
    await submit(engine)
    venue.cancel_final = True
    result = await submit(engine, batch="batch-two", guard=lambda _: False)
    assert result.reason == "market_not_ready" and len(venue.submitted) == 2
    assert len([call for call in venue.calls if call[0] == "cancel"]) == 2
    assert store.status(ACCOUNT)["pending_orders"] == 0


async def test_unchanged_quotes_still_require_a_current_market_guard(setup):
    engine, venue, store, _ = await start(setup)
    await submit(engine)
    venue.cancel_final = True
    result = await engine.maintain_pair(ACCOUNT, AGENT, "next-batch", pair(), market_guard=lambda _: False)
    assert result.state == "paused"
    assert len(venue.submitted) == 2
    assert store.agent(ACCOUNT, AGENT)["state"] == "paused"
    assert store.status(ACCOUNT)["pending_orders"] == 0


async def test_market_guard_exception_halts_instead_of_preserving_existing_quotes(setup):
    engine, venue, store, _ = await start(setup)
    await submit(engine)
    venue.cancel_final = True

    def broken_guard(_):
        raise ValueError("fake metadata parser failure")

    try:
        await engine.maintain_pair(ACCOUNT, AGENT, "next-batch", pair(), market_guard=broken_guard)
    except Exception:
        pass  # An error may propagate, but standing risk must already be stopped.
    assert store.agent(ACCOUNT, AGENT)["state"] == "paused"
    assert store.status(ACCOUNT)["pending_orders"] == 0


async def test_idempotent_batch_never_prepares_or_submits_again(setup):
    engine, venue, store, _ = await start(setup)
    first = await submit(engine)
    second = await submit(engine)
    assert second.state == "already_recorded" and set(first.intent_ids) == set(second.intent_ids)
    assert len(venue.submitted) == len(venue.prepared) == 2
    venue.cancel_final = True
    await engine.cancel_orders(ACCOUNT, AGENT)
    await engine.start(ACCOUNT, AGENT)
    third = await submit(engine)
    assert third.state == "already_recorded" and len(venue.submitted) == 2


async def test_cancelled_dispatch_is_not_retried_after_restart(setup):
    engine, venue, store, clock = await start(setup)
    entered = asyncio.Event()
    attempts = []

    async def stall(prepared):
        attempts.append(prepared)
        entered.set()
        await asyncio.Event().wait()

    venue.submit = stall
    task = asyncio.create_task(submit(engine))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(attempts) == 1 and not any(call[0] == "cancel" for call in venue.calls)
    assert store.status(ACCOUNT)["committed_micro"] == 4_800_000
    reopened = ExecutionStore(store.path, clock=clock)
    replacement = LiveEngine(reopened, venue=venue, worker_id="replacement")
    recovered = await replacement.recover(ACCOUNT)
    assert recovered.state == "paused" and reopened.agent(ACCOUNT, AGENT)["state"] == "paused"
    assert reopened.status(ACCOUNT)["committed_micro"] == 4_800_000
    with pytest.raises(ExecutionBlocked, match="activation_verification_failed"):
        await replacement.start(ACCOUNT, AGENT)
    assert len(attempts) == 1


@pytest.mark.parametrize("lookup_missing", [False, True], ids=["known_open", "lookup_unavailable"])
async def test_recovery_cancellation_is_bounded_and_fair_after_reopening_store(setup, monkeypatch, lookup_missing):
    engine, venue, store, clock = await start(setup)
    await submit(engine)
    for number in range(2, 6):
        agent, session = f"agent-{number}", f"session-{number}"
        register(store, agent=agent, session=session)
        await engine.start(ACCOUNT, agent)
        assert (await submit(engine, agent=agent, batch=f"batch-{number}")).state == "orders_submitted"

    settled = {order.order_hash for order in venue.submitted[:2]}
    pending = {order.order_hash for order in venue.submitted[2:]}
    for order in venue.submitted:
        key = (ACCOUNT, order.session_id, order.order_hash)
        if order.order_hash in settled:
            venue.observations[key] = OrderObservation(
                ACCOUNT, order.session_id, order.order_hash, clock(), "filled", D("10"), D("10"), True)
        elif lookup_missing:
            del venue.observations[key]

    # Keep individual calls alive beyond the aggregate cleanup budget. The
    # gates prove which calls were admitted; no assertion depends on a precise
    # scheduler latency or arbitrary sleep before inspecting concurrency.
    monkeypatch.setattr("oddsrail.live.engine.CANCEL_PASS_SECONDS", 0.25)

    async def stalled_pass(coordinator, adapter):
        attempts = []
        admitted = asyncio.Event()
        hold = asyncio.Event()
        active = 0
        maximum = 0

        async def stalled_cancel(account, session, order_hash):
            nonlocal active, maximum
            assert account == ACCOUNT
            attempts.append(order_hash)
            active += 1
            maximum = max(maximum, active)
            if len(attempts) == 4:
                admitted.set()
            try:
                await hold.wait()
            finally:
                active -= 1

        adapter.cancel = stalled_cancel
        recovery = asyncio.create_task(coordinator.recover(ACCOUNT))
        try:
            await asyncio.wait_for(admitted.wait(), 3)
            assert len(attempts) == active == 4
            result = await asyncio.wait_for(recovery, 3)
        finally:
            if not recovery.done():
                recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        assert result.state == "paused"
        assert len(attempts) == len(set(attempts)) == 4
        assert maximum == 4 and active == 0
        assert not settled.intersection(attempts)
        assert coordinator.store.status(ACCOUNT)["committed_micro"] == 48_000_000
        return set(attempts)

    engine.timeout_seconds = 2
    first = await stalled_pass(engine, venue)
    by_intent = {row["intent_id"]: row["order_hash"] for row in store.orders(ACCOUNT)}
    persisted = [event for event in store.history(ACCOUNT, limit=200) if event["kind"] == "cancel_requested"]
    assert len(persisted) == 4, "Queueing is not admission: only attempted calls should advance the durable cursor"
    assert {by_intent[event["intent_id"]] for event in persisted} == first

    # Neither a Python iterator nor an adapter's attempt history survives here.
    # All events share the same fake wall-clock timestamp, so fairness must
    # derive from durable admission ordering rather than timestamp ties.
    reopened = ExecutionStore(store.path, clock=clock)
    replacement_venue = FakeVenue(reopened, clock)
    replacement_venue.observations = dict(venue.observations)
    replacement = LiveEngine(reopened, venue=replacement_venue, worker_id="fair-recovery", timeout_seconds=2)
    second = await stalled_pass(replacement, replacement_venue)
    assert first.isdisjoint(second), "Previously unattempted sessions must not starve behind stalled sessions"
    assert first | second == pending
    assert len(reopened.orders(ACCOUNT, active_only=True)) == 8
    assert all(reopened.agent(ACCOUNT, agent)["state"] == "paused" for agent in [AGENT, *[f"agent-{n}" for n in range(2, 6)]])
    assert replacement_venue.prepared == replacement_venue.submitted == []


async def test_cancelled_preparation_releases_only_never_submitted_reservations(setup):
    engine, venue, store, _ = await start(setup)
    entered = asyncio.Event()

    async def stall(*args):
        entered.set()
        await asyncio.Event().wait()

    venue.prepare = stall
    task = asyncio.create_task(submit(engine))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.status(ACCOUNT)["committed_micro"] == 0
    assert store.status(ACCOUNT)["pending_orders"] == 0 and not venue.submitted


async def test_account_lease_prevents_concurrent_coordinator_from_signing(setup):
    engine, venue, store, _ = await start(setup)
    held = store.acquire(ACCOUNT, "other-worker")
    before = len(venue.calls)
    try:
        with pytest.raises(ExecutionBlocked, match="account_busy"):
            await submit(engine)
        assert len(venue.calls) == before and not venue.prepared
    finally:
        store.release(held)
    assert (await submit(engine)).state == "orders_submitted"


async def test_heartbeat_failure_before_signing_pauses_and_prevents_submission(setup):
    engine, venue, store, _ = await start(setup)
    venue.heartbeat_error = OSError("secret-bearing fake failure")
    result = await submit(engine)
    assert result.state == "paused" and result.reason == "venue_operation_failed"
    assert not venue.prepared and not venue.submitted
    assert "secret-bearing" not in str(store.history(ACCOUNT))


async def test_supervisor_heartbeat_failure_halts_and_cancels_existing_quotes(setup):
    engine, venue, store, _ = await start(setup)
    await submit(engine)
    venue.cancel_final = True
    venue.heartbeat_error = OSError("fake heartbeat failed")
    with pytest.raises(ExecutionBlocked, match="heartbeat_unavailable"):
        await engine.heartbeat(ACCOUNT, AGENT)
    assert store.agent(ACCOUNT, AGENT)["state"] == "paused"
    assert len([call for call in venue.calls if call[0] == "cancel"]) == 2
    assert store.status(ACCOUNT)["pending_orders"] == 0


async def test_blocked_account_cannot_send_a_keepalive_through_engine_directly(setup):
    engine, venue, store, _ = await start(setup)
    await submit(engine)
    lease = store.acquire(ACCOUNT, "risk-monitor")
    try:
        store.halt(lease, "daily_loss_limit")
    finally:
        store.release(lease)
    before = len(venue.calls)
    with pytest.raises(ExecutionBlocked, match="account_halted"):
        await engine.heartbeat(ACCOUNT, AGENT)
    assert len(venue.calls) == before
    assert store.status(ACCOUNT)["committed_micro"] == 9_600_000


@pytest.mark.parametrize("snapshot,permission", [
    ({"cash": D("9")}, {}),
    ({"daily_loss": D("10")}, {}),
    ({}, {"active": False}),
])
async def test_unchanged_quotes_stop_when_standing_account_risk_changes(setup, snapshot, permission):
    engine, venue, store, _ = await start(setup)
    await submit(engine)
    venue.snapshot_changes, venue.permission_changes = snapshot, permission
    venue.cancel_final = True
    result = await engine.maintain_pair(ACCOUNT, AGENT, "next-batch", pair(), market_guard=lambda _: True)
    assert result.state == "paused" and len(venue.submitted) == 2
    assert store.agent(ACCOUNT, AGENT)["state"] == "paused"
    assert store.status(ACCOUNT)["pending_orders"] == 0


async def test_halt_and_cancel_cannot_halt_account_via_unknown_agent(setup):
    engine, venue, store, _ = await start(setup)
    with pytest.raises(ExecutionBlocked, match="agent_not_registered"):
        await engine.halt_and_cancel(ACCOUNT, "not-registered", "market_not_ready")
    assert store.agent(ACCOUNT, AGENT)["state"] == "running"
    assert store.status(ACCOUNT)["blocked"] is None


async def test_repeated_start_heartbeat_failure_cannot_leave_agent_running(setup):
    engine, venue, store, _ = await start(setup)
    await submit(engine)
    venue.cancel_final = True
    venue.heartbeat_error = OSError("fake heartbeat failed")
    with pytest.raises(Exception):
        await engine.start(ACCOUNT, AGENT)
    assert store.agent(ACCOUNT, AGENT)["state"] == "paused"
    assert len([call for call in venue.calls if call[0] == "cancel"]) == 2


async def test_exception_injected_as_executionblocked_cannot_persist_secrets(setup):
    engine, venue, store, _ = await start(setup)

    async def untrusted_error(*args):
        raise ExecutionBlocked("sensitive_signed_payload")

    venue.prepare = untrusted_error
    result = await submit(engine)
    assert result.reason == "venue_operation_failed"
    assert "sensitive_signed_payload" not in str(store.history(ACCOUNT))


async def test_final_rejection_after_cancel_cannot_erase_an_acknowledged_order(setup):
    engine, venue, store, clock = await start(setup)
    await submit(engine)
    for prepared in venue.submitted:
        venue.observations[(ACCOUNT, SESSION, prepared.order_hash)] = OrderObservation(
            ACCOUNT, SESSION, prepared.order_hash, clock(), "rejected", D(0), D(0), True)
    result = await engine.cancel_orders(ACCOUNT, AGENT)
    assert result.state == "cancel_pending"
    assert store.status(ACCOUNT)["committed_micro"] == 9_600_000


async def test_permanent_failed_settlement_only_releases_confirmed_unspent_portion(setup):
    engine, venue, store, clock = await start(setup)
    await submit(engine)
    first, second = venue.submitted
    venue.observations[(ACCOUNT, SESSION, first.order_hash)] = OrderObservation(
        ACCOUNT, SESSION, first.order_hash, clock(), "canceled", D("5"), D("3"), True, D("2"))
    venue.observations[(ACCOUNT, SESSION, second.order_hash)] = OrderObservation(
        ACCOUNT, SESSION, second.order_hash, clock(), "canceled", D(0), D(0), True)
    result = await engine.cancel_orders(ACCOUNT, AGENT)
    assert result.state == "canceled" and store.status(ACCOUNT)["pending_orders"] == 0
    assert store.status(ACCOUNT)["committed_micro"] == 1_440_000


async def test_reconciliation_failure_preserves_capital_and_reports_failure(setup):
    engine, venue, store, _ = await start(setup)
    await submit(engine)

    async def fail(*args):
        raise OSError("fake lookup failure")

    venue.lookup = fail
    result = await engine.reconcile(ACCOUNT)
    assert result.state != "reconciled"
    assert store.status(ACCOUNT)["blocked"] == "reconciliation_unavailable"
    assert store.status(ACCOUNT)["committed_micro"] == 9_600_000
