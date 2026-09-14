"""Adversarial SQLite tests for account-wide live reservations and reconciliation."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
from threading import Barrier

import pytest

from oddsrail.live.contracts import (
    AccountPolicy, AccountSnapshot, AgentPolicy, OrderObservation, OrderRequest,
    Permission,
)
from oddsrail.live.store import ExecutionBlocked, ExecutionStore, LEASE_MS


D = Decimal
ACCOUNT = "0x" + "1" * 40
OWNER = "0x" + "2" * 40
OTHER_ACCOUNT = "0x" + "3" * 40
OTHER_OWNER = "0x" + "4" * 40
MARKET = "0x" + "a" * 64
OTHER_MARKET = "0x" + "b" * 64
HASH = "0x" + "c" * 64
OTHER_HASH = "0x" + "d" * 64


class Clock:
    now = 1_000_000

    def __call__(self):
        return self.now

    def advance(self, ms=1):
        self.now += ms


class Harness:
    def __init__(self, path, *, account_overrides=None, agent_overrides=None):
        self.clock = Clock()
        self.path = path
        self.store = ExecutionStore(path, clock=self.clock)
        self.account_policy = AccountPolicy(**(dict(
            account=ACCOUNT, owner=OWNER, capital=D("100"), per_order=D("10"),
            per_market=D("100"), daily_loss=D("10"), max_open_orders=10,
        ) | (account_overrides or {})))
        self.agent_policy = AgentPolicy(**(dict(
            account=ACCOUNT, agent_id="agent", session_id="session", condition_id=MARKET,
            tokens=("123", "456"), capital=D("100"), per_order=D("10"), per_market=D("100"),
        ) | (agent_overrides or {})))
        self.store.register_account(self.account_policy)
        self.store.register_agent(self.agent_policy)
        self.lease = self.store.acquire(ACCOUNT, "worker")

    def snapshot(self, **overrides):
        return AccountSnapshot(**(dict(
            account=ACCOUNT, owner=OWNER, captured_at_ms=self.clock(), cash=D("100"),
            allowance=D("100"), external_reserved=D("0"), external_exposure=(),
            daily_loss=D("0"), complete=True, eligible=True,
        ) | overrides))

    def permission(self, **overrides):
        return Permission(**(dict(account=ACCOUNT, session_id="session", verified_at_ms=self.clock(),
                                 expires_at_ms=self.clock() + 120_000, active=True, can_trade=True) | overrides))

    def start(self, agent="agent", **kwargs):
        self.store.start(self.lease, agent, kwargs.get("snapshot", self.snapshot()),
                         kwargs.get("permission", self.permission()))

    def reserve(self, batch="batch", *, agent="agent", requests=None, snapshot=None, permission=None):
        return self.store.reserve_pair(self.lease, agent, batch, requests or pair(),
                                       snapshot or self.snapshot(), permission or self.permission())

    def open_one(self, *, requests=None):
        self.start()
        rows, _ = self.reserve(requests=requests)
        first, second = rows
        self.store.abandon_reserved(self.lease, second["intent_id"])
        self.store.bind_hash(self.lease, first["intent_id"], HASH)
        self.store.begin_dispatch(self.lease, first["intent_id"], self.snapshot(), self.permission())
        self.store.acknowledge(self.lease, first["intent_id"], HASH)
        return first["intent_id"]

    def observe(self, intent, **overrides):
        observation = OrderObservation(**(dict(
            account=ACCOUNT, session_id="session", order_hash=HASH, observed_at_ms=self.clock(),
            state="open", matched_size=D("0"), confirmed_size=D("0"), final=False,
        ) | overrides))
        self.store.observe(self.lease, intent, observation)

    def row(self, intent):
        return next(row for row in self.store.orders(ACCOUNT) if row["intent_id"] == intent)


def pair(*, price="0.5", size="10", fee="0", condition_id=MARKET):
    return tuple(OrderRequest(condition_id, token, D(price), D(size), D("0.01"),
                              D("0.01"), D(fee)) for token in ("123", "456"))


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path / "live.sqlite3")


def blocked(code):
    return pytest.raises(ExecutionBlocked, match=f"^{code}$")


def test_account_and_agent_registration_are_idempotent_and_immutable(h):
    before = h.store.history(ACCOUNT)
    h.store.register_account(h.account_policy)
    h.store.register_agent(h.agent_policy)
    assert h.store.history(ACCOUNT) == before
    with blocked("account_policy_immutable"):
        h.store.register_account(replace(h.account_policy, owner=OTHER_OWNER))
    with blocked("agent_policy_immutable"):
        h.store.register_agent(replace(h.agent_policy, session_id="other"))
    with blocked("account_not_registered"):
        h.store.register_agent(replace(h.agent_policy, account=OTHER_ACCOUNT))
    with blocked("session_already_assigned"):
        h.store.register_agent(replace(h.agent_policy, agent_id="shared_session"))


def test_account_isolation_applies_to_orders_policies_history_and_leases(h):
    h.store.register_account(replace(h.account_policy, account=OTHER_ACCOUNT, owner=OTHER_OWNER))
    h.store.register_agent(replace(h.agent_policy, account=OTHER_ACCOUNT))
    other = h.store.acquire(OTHER_ACCOUNT, "worker")
    h.start()
    rows, _ = h.reserve()
    assert h.store.orders(OTHER_ACCOUNT) == []
    assert h.store.status(OTHER_ACCOUNT)["committed_micro"] == 0
    assert not any(event["kind"] == "order_reserved" for event in h.store.history(OTHER_ACCOUNT))
    with blocked("intent_not_found"):
        h.store.bind_hash(other, rows[0]["intent_id"], HASH)
    with blocked("account_not_ready"):
        h.store.start(other, "agent", h.snapshot(), h.permission())


@pytest.mark.parametrize("account_limits,agent_limits,code", [
    ({"capital": D("9.99")}, {}, "capital_limit"),
    ({}, {"capital": D("9.99")}, "capital_limit"),
    ({"per_market": D("9.99")}, {}, "market_exposure_limit"),
    ({}, {"per_market": D("9.99")}, "market_exposure_limit"),
    ({"per_order": D("4.99")}, {}, "per_order_limit"),
    ({}, {"per_order": D("4.99")}, "per_order_limit"),
])
def test_pair_risk_limits_fail_atomically(tmp_path, account_limits, agent_limits, code):
    h = Harness(tmp_path / "live.sqlite3", account_overrides=account_limits, agent_overrides=agent_limits)
    h.start()
    before = h.store.history(ACCOUNT)
    with blocked(code):
        h.reserve()
    assert h.store.orders(ACCOUNT) == []
    assert h.store.history(ACCOUNT) == before
    assert h.store.status(ACCOUNT)["committed_micro"] == 0


@pytest.mark.parametrize("snapshot_fields", [
    {"cash": D("9.99")}, {"allowance": D("9.99")},
    {"cash": D("10"), "external_reserved": D("0.01")},
])
def test_pair_cash_allowance_and_external_reservations_are_shared(h, snapshot_fields):
    h.start()
    with blocked("insufficient_verified_funds"):
        h.reserve(snapshot=h.snapshot(**snapshot_fields))
    assert not h.store.orders(ACCOUNT)


def test_fee_buffer_counts_towards_cash_capital_and_each_order_limit(tmp_path):
    h = Harness(tmp_path / "live.sqlite3", account_overrides={"per_order": D("5")})
    h.start()
    with blocked("per_order_limit"):
        h.reserve(requests=pair(fee="0.001"))
    assert not h.store.orders(ACCOUNT)
    h2 = Harness(tmp_path / "second.sqlite3")
    h2.start()
    rows, _ = h2.reserve(requests=pair(fee="0.01"))
    assert [row["cost"] for row in rows] == [5_050_000, 5_050_000]
    assert h2.store.status(ACCOUNT)["committed_micro"] == 10_100_000


@pytest.mark.parametrize("limits,expected", [
    ({"capital": D("19")}, "capital_limit"),
    ({"per_market": D("19")}, "market_exposure_limit"),
    ({"max_open_orders": 3}, "max_open_orders"),
])
def test_two_agents_share_account_limits(tmp_path, limits, expected):
    h = Harness(tmp_path / "live.sqlite3", account_overrides=limits)
    h.store.register_agent(replace(h.agent_policy, agent_id="other", session_id="other_session"))
    h.start()
    h.start("other", permission=h.permission(session_id="other_session"))
    h.reserve()
    with blocked(expected):
        h.reserve(agent="other", permission=h.permission(session_id="other_session"))
    assert len(h.store.orders(ACCOUNT)) == 2
    assert h.store.status(ACCOUNT)["committed_micro"] == 10_000_000


def test_external_market_exposure_counts_against_account_market_cap(tmp_path):
    h = Harness(tmp_path / "live.sqlite3", account_overrides={"per_market": D("15")})
    h.start()
    with blocked("market_exposure_limit"):
        h.reserve(snapshot=h.snapshot(external_exposure=((MARKET, D("5.01")),)))
    h.reserve(snapshot=h.snapshot(external_exposure=((OTHER_MARKET, D("99")),)))


def test_selected_market_token_allowlist_and_existing_token_reservation(h):
    h.start()
    with blocked("market_not_allowed"):
        h.reserve(requests=pair(condition_id=OTHER_MARKET))
    wrong = (replace(pair()[0], token_id="999"), pair()[1])
    with blocked("market_not_allowed"):
        h.reserve(requests=wrong)
    h.reserve()
    with blocked("quote_already_pending"):
        h.reserve(batch="second")


def test_pair_idempotency_never_reserves_or_emits_twice_even_when_paused(h):
    h.start()
    rows, created = h.reserve()
    assert created is True
    h.store.pause(h.lease, "agent")
    before = h.store.history(ACCOUNT)
    repeated, created = h.reserve(snapshot=h.snapshot(complete=False))
    assert created is False
    assert {row["intent_id"] for row in rows} == {row["intent_id"] for row in repeated}
    assert h.store.status(ACCOUNT)["committed_micro"] == 10_000_000
    assert h.store.history(ACCOUNT) == before
    with blocked("idempotency_conflict"):
        h.reserve(requests=pair(price="0.49"))


def test_concurrent_workers_cannot_both_acquire_account_lease(h):
    h.store.release(h.lease)
    barrier = Barrier(2)

    def acquire(worker):
        store = ExecutionStore(h.path, clock=h.clock)
        barrier.wait(timeout=5)
        try:
            return store.acquire(ACCOUNT, worker)
        except ExecutionBlocked as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acquire, ("one", "two")))
    assert sum(result == "account_busy" for result in results) == 1
    assert sum(not isinstance(result, str) for result in results) == 1


def test_expired_fenced_worker_cannot_mutate_or_release_new_lease(h):
    h.start()
    rows, _ = h.reserve()
    old = h.lease
    h.clock.advance(LEASE_MS)
    new = h.store.acquire(ACCOUNT, "replacement")
    assert new.fence > old.fence
    for action in (
        lambda: h.store.renew(old), lambda: h.store.pause(old, "agent"),
        lambda: h.store.bind_hash(old, rows[0]["intent_id"], HASH),
        lambda: h.store.abandon_reserved(old, rows[0]["intent_id"]),
    ):
        with blocked("lease_lost"):
            action()
    h.store.release(old)
    h.store.renew(new)
    assert h.store.status(ACCOUNT)["committed_micro"] == 10_000_000


def test_parallel_agent_reservations_are_serialized_under_one_account_budget(tmp_path):
    h = Harness(tmp_path / "live.sqlite3", account_overrides={"capital": D("15")})
    h.store.register_agent(replace(h.agent_policy, agent_id="other", session_id="other_session"))
    h.start()
    h.start("other", permission=h.permission(session_id="other_session"))
    barrier = Barrier(2)

    def reserve(agent):
        barrier.wait(timeout=5)
        try:
            rows, created = h.reserve(agent=agent, permission=h.permission(
                session_id="session" if agent == "agent" else "other_session"))
            return len(rows), created
        except ExecutionBlocked as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ("agent", "other")))
    assert results.count((2, True)) == 1
    assert results.count("capital_limit") == 1
    assert len(h.store.orders(ACCOUNT)) == 2
    assert h.store.status(ACCOUNT)["committed_micro"] == 10_000_000


@pytest.mark.parametrize("snapshot_fields", [
    {"account": OTHER_ACCOUNT}, {"owner": OTHER_OWNER},
    {"complete": False}, {"eligible": False},
    {"captured_at_ms": 989_999}, {"captured_at_ms": 1_000_001},
])
def test_start_rejects_unverified_or_stale_account(h, snapshot_fields):
    with blocked("account_not_ready"):
        h.start(snapshot=h.snapshot(**snapshot_fields))
    assert h.store.agent(ACCOUNT, "agent")["state"] == "paused"


@pytest.mark.parametrize("permission_fields", [
    {"account": OTHER_ACCOUNT}, {"session_id": "other"},
    {"active": False}, {"can_trade": False},
    {"verified_at_ms": 989_999}, {"verified_at_ms": 1_000_001},
    {"expires_at_ms": 1_030_000},
])
def test_start_rejects_unverified_stale_or_expiring_permission(h, permission_fields):
    with blocked("permission_not_ready"):
        h.start(permission=h.permission(**permission_fields))


def test_daily_loss_limit_is_checked_on_start_reserve_and_dispatch(h):
    limit = h.snapshot(daily_loss=D("10"))
    with blocked("daily_loss_limit"):
        h.start(snapshot=limit)
    h.start(snapshot=h.snapshot(daily_loss=D("9.99")))
    with blocked("daily_loss_limit"):
        h.reserve(snapshot=limit)
    rows, _ = h.reserve()
    h.store.bind_hash(h.lease, rows[0]["intent_id"], HASH)
    with blocked("daily_loss_limit"):
        h.store.begin_dispatch(h.lease, rows[0]["intent_id"], limit, h.permission())
    assert h.row(rows[0]["intent_id"])["state"] == "reserved"


def test_dispatch_rechecks_cash_and_permission_after_reservation(h):
    h.start()
    rows, _ = h.reserve()
    intent = rows[0]["intent_id"]
    h.store.bind_hash(h.lease, intent, HASH)
    with blocked("insufficient_verified_funds"):
        h.store.begin_dispatch(h.lease, intent, h.snapshot(cash=D("9.99")), h.permission())
    with blocked("permission_not_ready"):
        h.store.begin_dispatch(h.lease, intent, h.snapshot(), h.permission(active=False))
    assert h.row(intent)["state"] == "reserved"


def test_pause_cancel_and_ack_never_release_a_submitted_reservation(h):
    intent = h.open_one()
    h.store.pause(h.lease, "agent")
    assert h.row(intent)["committed"] == 5_000_000
    h.store.request_cancel(h.lease, intent)
    assert h.row(intent)["state"] == "cancel_pending"
    assert h.row(intent)["committed"] == 5_000_000
    with blocked("intent_may_have_been_submitted"):
        h.store.abandon_reserved(h.lease, intent)
    # An open-order lookup after a cancel ACK cannot clear cancel_pending.
    h.observe(intent)
    assert h.row(intent)["state"] == "cancel_pending"
    assert h.store.status(ACCOUNT)["committed_micro"] == 5_000_000


def test_restart_keeps_unknown_submissions_and_only_releases_never_dispatched(h):
    h.start()
    rows, _ = h.reserve()
    intent = rows[0]["intent_id"]
    h.store.bind_hash(h.lease, intent, HASH)
    h.store.begin_dispatch(h.lease, intent, h.snapshot(), h.permission())
    h.store.release(h.lease)
    h.store = ExecutionStore(h.path, clock=h.clock)
    h.lease = h.store.acquire(ACCOUNT, "new_process")
    h.store.recover(h.lease)
    assert h.row(intent)["state"] == "unknown"
    assert h.row(intent)["committed"] == 5_000_000
    assert h.row(rows[1]["intent_id"])["terminal"] == 1
    assert h.row(rows[1]["intent_id"])["committed"] == 0
    assert h.store.agent(ACCOUNT, "agent")["state"] == "paused"
    with blocked("reconciliation_required"):
        h.start()
    with blocked("intent_not_dispatchable"):
        h.store.begin_dispatch(h.lease, intent, h.snapshot(), h.permission())


def test_unknown_order_halts_all_agents_without_freeing_funds(h):
    h.store.register_agent(replace(h.agent_policy, agent_id="other", session_id="other_session"))
    h.start("other", permission=h.permission(session_id="other_session"))
    intent = h.open_one()
    h.store.unknown(h.lease, intent)
    assert h.store.status(ACCOUNT)["blocked"] == "reconciliation_required"
    assert h.store.status(ACCOUNT)["committed_micro"] == 5_000_000
    assert h.store.agent(ACCOUNT, "other")["state"] == "paused"


@pytest.mark.parametrize("fields", [
    {"account": OTHER_ACCOUNT}, {"session_id": "other"}, {"order_hash": OTHER_HASH},
])
def test_reconciliation_rejects_wrong_identity_without_changing_balance(h, fields):
    intent = h.open_one()
    before = h.row(intent)
    with blocked("observation_identity_mismatch"):
        h.observe(intent, state="canceled", final=True, **fields)
    assert h.row(intent) == before


def test_stale_future_and_out_of_order_observations_cannot_release(h):
    intent = h.open_one()
    with blocked("observation_stale"):
        h.observe(intent, observed_at_ms=h.clock() - 10_001)
    with blocked("observation_stale"):
        h.observe(intent, observed_at_ms=h.clock() + 1)
    h.observe(intent)
    h.clock.advance()
    with blocked("observation_out_of_order"):
        h.observe(intent, observed_at_ms=h.clock() - 2)
    assert h.row(intent)["committed"] == 5_000_000


def test_partial_pending_fills_hold_full_cost_until_authoritative_finality(h):
    intent = h.open_one()
    h.observe(intent, state="matched", matched_size=D("6"), confirmed_size=D("2"))
    assert h.row(intent)["committed"] == 5_000_000
    h.clock.advance()
    h.observe(intent, state="canceled", matched_size=D("6"), confirmed_size=D("2"))
    assert h.row(intent)["state"] == "matched"
    assert h.row(intent)["terminal"] == 0
    h.clock.advance()
    h.observe(intent, state="canceled", matched_size=D("6"), confirmed_size=D("6"), final=True)
    assert h.row(intent)["committed"] == 3_000_000
    assert h.row(intent)["terminal"] == 1
    assert h.store.status(ACCOUNT)["committed_micro"] == 3_000_000


def test_permanent_failed_settlement_preserves_matched_counter_but_releases_failed_cost(h):
    intent = h.open_one()
    h.observe(intent, state="matched", matched_size=D("8"), confirmed_size=D("3"))
    h.clock.advance()
    h.observe(intent, state="canceled", matched_size=D("8"), confirmed_size=D("3"), failed_size=D("4"))
    assert h.row(intent)["committed"] == 5_000_000  # One share still unresolved.
    h.clock.advance()
    h.observe(intent, state="canceled", matched_size=D("8"), confirmed_size=D("3"),
              failed_size=D("5"), final=True)
    row = h.row(intent)
    assert row["terminal"] == 1
    assert D(row["matched"]) == D("8")
    assert D(row["confirmed"]) == D("3")
    assert D(row["failed"]) == D("5")
    assert row["committed"] == 1_500_000


def test_failed_settlement_counter_cannot_decrease(h):
    intent = h.open_one()
    h.observe(intent, state="matched", matched_size=D("8"), confirmed_size=D("3"), failed_size=D("4"))
    h.clock.advance()
    with blocked("fill_inconsistent"):
        h.observe(intent, state="matched", matched_size=D("8"), confirmed_size=D("3"), failed_size=D("3"))
    assert h.row(intent)["committed"] == 5_000_000


def test_failed_settlement_cannot_be_claimed_final_while_shares_are_unresolved(h):
    intent = h.open_one()
    with pytest.raises(ValueError, match="Settlement is incomplete"):
        h.observe(intent, state="canceled", matched_size=D("8"), confirmed_size=D("3"),
                  failed_size=D("4"), final=True)
    with pytest.raises(ValueError, match="Invalid cumulative fill"):
        h.observe(intent, state="canceled", matched_size=D("8"), confirmed_size=D("3"),
                  failed_size=D("6"), final=True)
    assert h.row(intent)["committed"] == 5_000_000


def test_duplicate_final_failed_settlement_does_not_move_watermark(h):
    intent = h.open_one()
    args = dict(state="canceled", matched_size=D("8"), confirmed_size=D("3"), failed_size=D("5"), final=True)
    h.observe(intent, **args)
    original = h.row(intent)
    h.clock.advance(1_000)
    h.observe(intent, **args)
    assert h.row(intent) == original


@pytest.mark.parametrize("fields", [
    {"matched_size": D("11"), "confirmed_size": D("2")},
    {"matched_size": D("5"), "confirmed_size": D("2")},
    {"matched_size": D("6"), "confirmed_size": D("1")},
    {"state": "filled", "matched_size": D("6"), "confirmed_size": D("6"), "final": True},
])
def test_inconsistent_cumulative_fills_never_reduce_commitment(h, fields):
    intent = h.open_one()
    h.observe(intent, state="matched", matched_size=D("6"), confirmed_size=D("2"))
    h.clock.advance()
    before = h.row(intent)
    with blocked("fill_inconsistent"):
        h.observe(intent, **fields)
    assert h.row(intent) == before


def test_duplicate_final_observation_cannot_move_cash_watermark_or_reopen(h):
    intent = h.open_one()
    fields = dict(state="canceled", matched_size=D("6"), confirmed_size=D("6"), final=True)
    h.observe(intent, **fields)
    before = h.row(intent)
    history = h.store.history(ACCOUNT)
    h.clock.advance(1_000)
    h.observe(intent, **fields)
    assert h.row(intent) == before
    assert h.store.history(ACCOUNT) == history
    with blocked("terminal_order_changed"):
        h.observe(intent, matched_size=D("6"), confirmed_size=D("6"))


def test_accepted_order_cannot_be_released_as_rejected(h):
    intent = h.open_one()
    with blocked("accepted_order_cannot_be_rejected"):
        h.observe(intent, state="rejected", final=True)
    assert h.row(intent)["committed"] == 5_000_000


@pytest.mark.parametrize("transition", ["unknown", "request_cancel"])
def test_prior_acceptance_cannot_be_forgotten_after_unknown_or_cancel(h, transition):
    intent = h.open_one()
    getattr(h.store, transition)(h.lease, intent)
    with blocked("accepted_order_cannot_be_rejected"):
        h.observe(intent, state="rejected", final=True)
    assert h.row(intent)["committed"] == 5_000_000
    assert h.row(intent)["terminal"] == 0


def test_final_fill_remains_in_allocated_capital_and_market_exposure(tmp_path):
    h = Harness(tmp_path / "live.sqlite3", account_overrides={"capital": D("14")})
    intent = h.open_one()
    h.observe(intent, state="filled", matched_size=D("10"), confirmed_size=D("10"), final=True)
    h.clock.advance()
    with blocked("capital_limit"):
        h.reserve(batch="next")
    assert h.store.status(ACCOUNT)["committed_micro"] == 5_000_000
    h2 = Harness(tmp_path / "second.sqlite3", account_overrides={"per_market": D("14")})
    second = h2.open_one()
    h2.observe(second, state="filled", matched_size=D("10"), confirmed_size=D("10"), final=True)
    h2.clock.advance()
    with blocked("market_exposure_limit"):
        h2.reserve(batch="next")


def test_day_change_and_worker_restart_do_not_reset_filled_allocation(h):
    intent = h.open_one()
    h.observe(intent, state="filled", matched_size=D("10"), confirmed_size=D("10"), final=True)
    h.clock.advance(86_400_000)
    h.store = ExecutionStore(h.path, clock=h.clock)
    h.lease = h.store.acquire(ACCOUNT, "next_day")
    h.store.recover(h.lease)
    h.start(snapshot=h.snapshot(daily_loss=D("0")))
    assert h.row(intent)["terminal"] == 1
    assert h.store.status(ACCOUNT)["committed_micro"] == 5_000_000


def test_final_fill_cash_hold_requires_snapshot_started_after_settlement(h):
    intent = h.open_one()
    old_snapshot = h.snapshot(cash=D("10"))
    h.observe(intent, state="filled", matched_size=D("10"), confirmed_size=D("10"), final=True)
    with blocked("insufficient_verified_funds"):
        h.reserve(batch="next", snapshot=old_snapshot)
    h.clock.advance()
    # Refreshed verified cash excludes the settled spend, while allocation still includes it.
    h.reserve(batch="next", snapshot=h.snapshot(cash=D("10")))
    assert h.store.status(ACCOUNT)["committed_micro"] == 15_000_000


def test_revocation_request_pauses_agent_but_does_not_claim_remote_revocation(h):
    intent = h.open_one()
    h.store.request_revocation(h.lease, "agent")
    assert h.store.agent(ACCOUNT, "agent")["state"] == "revocation_requested"
    assert h.row(intent)["committed"] == 5_000_000
    with blocked("revocation_pending"):
        h.start()
    h.store.pause(h.lease, "agent")
    assert h.store.agent(ACCOUNT, "agent")["state"] == "revocation_requested"


def test_money_reservations_ignore_callers_decimal_precision_and_rounding(h):
    requests = pair(price="0.49", size="20.01")
    h.start()
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_DOWN
        rows, _ = h.reserve(requests=requests)
    assert [row["cost"] for row in rows] == [9_804_900, 9_804_900]
    assert h.store.status(ACCOUNT)["committed_micro"] == 19_609_800


def test_partial_final_commitment_ignores_callers_decimal_context(h):
    intent = h.open_one(requests=pair(price="0.49", size="20.01"))
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_DOWN
        h.observe(intent, state="canceled", matched_size=D("7.01"), confirmed_size=D("7.01"), final=True)
    assert h.row(intent)["committed"] == 3_434_900


def test_risk_and_reconciliation_do_not_inherit_decimal_inexact_trap(h):
    requests = pair(price="0.49", size="20.01")
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        intent = h.open_one(requests=requests)
        h.observe(intent, state="canceled", matched_size=D("7.01"), confirmed_size=D("7.01"), final=True)
    assert h.row(intent)["committed"] == 3_434_900


@pytest.mark.parametrize("field", ["capital", "per_order", "per_market", "daily_loss"])
def test_account_budget_does_not_round_up_submicro_limits(h, field):
    with pytest.raises(ValueError, match="Budget precision"):
        replace(h.account_policy, **{field: D("5.0000001")})


@pytest.mark.parametrize("field", ["cash", "allowance", "external_reserved", "daily_loss"])
def test_snapshot_balances_do_not_round_up_submicro_amounts(h, field):
    with pytest.raises(ValueError, match="Balance precision"):
        h.snapshot(**{field: D("5.0000001")})


@pytest.mark.parametrize("value", [True, .1, "1", D("NaN"), D("sNaN"), D("Infinity"), D("-1"), D("1e-100000")])
def test_contracts_reject_unbounded_or_non_decimal_money(h, value):
    with pytest.raises(ValueError):
        replace(h.account_policy, capital=value)
    with pytest.raises(ValueError):
        h.snapshot(cash=value)
    with pytest.raises(ValueError):
        replace(pair()[0], price=value)


def test_order_request_rejects_submicro_fee_precision():
    with pytest.raises(ValueError, match="fee precision"):
        pair(fee="0.0000001")
