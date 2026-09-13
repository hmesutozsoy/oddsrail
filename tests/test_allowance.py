"""Private allowance accounting: offline temp SQLite files, no provider calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
from threading import Barrier

import pytest

from oddsrail.cloud import allowance as mod
from oddsrail.cloud.allowance import Allowance, AllowanceConfig, VerifiedAccount


ACCOUNT = VerifiedAccount("verified-account-1", 100_000_000)
OTHER = VerifiedAccount("verified-account-2", 100_000_000)


@pytest.fixture
def clock():
    return [datetime(2026, 9, 12, 12, tzinfo=timezone.utc)]


@pytest.fixture
def ledger(tmp_path, clock):
    return Allowance(tmp_path / "allowance.sqlite3",
                     AllowanceConfig(enabled=True, period="daily", cap_microusd=1_000_000), clock=lambda: clock[0])


def test_default_is_disabled_even_with_verified_eligible_account(tmp_path):
    ledger = Allowance(tmp_path / "disabled.sqlite3")
    assert ledger.config.cap_microusd is None
    assert ledger.public_status(ACCOUNT) == {"status": "unavailable"}
    decision = ledger.reserve(ACCOUNT, 40_000)
    assert decision.reservation_id is None
    assert decision.public_status() == {"status": "unavailable"}


def test_enabling_requires_explicit_period():
    with pytest.raises(ValueError, match="explicit period"):
        AllowanceConfig(enabled=True)


def test_enabling_requires_explicit_private_cap():
    with pytest.raises(ValueError, match="explicit private cap"):
        AllowanceConfig(enabled=True, period="daily")


@pytest.mark.parametrize("account", [None, VerifiedAccount("under-floor", 99_999_999)])
def test_only_verified_eligible_accounts_can_reserve(ledger, account):
    assert ledger.public_status(account) == {"status": "unavailable"}
    assert ledger.reserve(account, 1).reservation_id is None


def test_client_shaped_data_is_not_a_verified_account(ledger):
    with pytest.raises(TypeError, match="trusted-server"):
        ledger.reserve({"account_id": ACCOUNT.account_id, "eligible_capital_microusd": 1_000_000_000}, 1)


def test_configured_floor_applies(tmp_path):
    ledger = Allowance(tmp_path / "custom.sqlite3", AllowanceConfig(
        enabled=True, period="lifetime", cap_microusd=1_000_000, minimum_capital_microusd=200_000_000))
    assert ledger.reserve(ACCOUNT, 1).status == "unavailable"
    assert ledger.reserve(VerifiedAccount("eligible", 200_000_000), 1).status == "included"


def test_reserves_before_request_and_never_exceeds_cap(ledger):
    first = ledger.reserve(ACCOUNT, 800_000)
    assert first.reservation_id
    assert ledger.reserve(ACCOUNT, 200_001).status == "limit_reached"
    assert ledger.reserve(ACCOUNT, 200_000).reservation_id
    assert ledger.reserve(ACCOUNT, 1).reservation_id is None
    assert ledger.public_status(ACCOUNT) == {"status": "limit_reached"}
    assert ledger.reserve(OTHER, 1_000_000).reservation_id


def test_public_status_exposes_neither_cost_nor_reservation(ledger):
    decision = ledger.reserve(ACCOUNT, 40_000)
    assert decision.public_status() == {"status": "included"}
    assert ledger.public_status(ACCOUNT) == {"status": "included"}


def test_verified_actual_cost_releases_only_unused_reservation(ledger):
    first = ledger.reserve(ACCOUNT, 1_000_000)
    ledger.reconcile(ACCOUNT, first.reservation_id, 600_000)
    ledger.reconcile(ACCOUNT, first.reservation_id, 600_000)  # idempotent notification
    assert ledger.reserve(ACCOUNT, 400_001).status == "limit_reached"
    assert ledger.reserve(ACCOUNT, 400_000).reservation_id
    assert ledger.public_status(ACCOUNT) == {"status": "limit_reached"}


def test_failed_or_unknown_cost_retains_full_reservation(ledger):
    first = ledger.reserve(ACCOUNT, 1_000_000)
    ledger.reconcile(ACCOUNT, first.reservation_id, None)
    ledger.reconcile(ACCOUNT, first.reservation_id, None)
    assert ledger.reserve(ACCOUNT, 1).status == "limit_reached"
    # Later authoritative evidence can still settle an earlier unknown result.
    ledger.reconcile(ACCOUNT, first.reservation_id, 0)
    assert ledger.reserve(ACCOUNT, 1_000_000).reservation_id


def test_cost_above_reservation_is_rejected_without_refund(ledger):
    first = ledger.reserve(ACCOUNT, 1_000_000)
    with pytest.raises(ValueError, match="exceeds"):
        ledger.reconcile(ACCOUNT, first.reservation_id, 1_000_001)
    assert ledger.reserve(ACCOUNT, 1).status == "limit_reached"


def test_conflicting_reconciliation_cannot_refund_twice(ledger):
    first = ledger.reserve(ACCOUNT, 1_000_000)
    ledger.reconcile(ACCOUNT, first.reservation_id, 800_000)
    with pytest.raises(ValueError, match="different cost"):
        ledger.reconcile(ACCOUNT, first.reservation_id, 0)
    assert ledger.reserve(ACCOUNT, 200_001).status == "limit_reached"


def test_other_account_cannot_reconcile_reservation(ledger):
    first = ledger.reserve(ACCOUNT, 1_000_000)
    with pytest.raises(KeyError, match="not found"):
        ledger.reconcile(OTHER, first.reservation_id, 0)
    assert ledger.reserve(ACCOUNT, 1).status == "limit_reached"


def test_account_reconciliation_survives_loss_of_eligibility_or_disable(ledger):
    first = ledger.reserve(ACCOUNT, 1_000_000)
    disabled = Allowance(ledger.path)
    disabled.reconcile(VerifiedAccount(ACCOUNT.account_id, 0), first.reservation_id, 800_000)
    assert disabled.reserve(ACCOUNT, 1).status == "unavailable"
    assert ledger.reserve(ACCOUNT, 200_000).reservation_id


@pytest.mark.parametrize("period,next_day_available,next_month_available", [
    ("daily", True, True), ("monthly", False, True), ("lifetime", False, False),
])
def test_explicit_period_boundaries(tmp_path, clock, period, next_day_available, next_month_available):
    ledger = Allowance(tmp_path / "period.sqlite3", AllowanceConfig(enabled=True, period=period, cap_microusd=1_000_000),
                       clock=lambda: clock[0])
    assert ledger.reserve(ACCOUNT, 1_000_000).reservation_id
    clock[0] = datetime(2026, 9, 13, tzinfo=timezone.utc)
    assert (ledger.public_status(ACCOUNT)["status"] == "included") is next_day_available
    clock[0] = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert (ledger.public_status(ACCOUNT)["status"] == "included") is next_month_available


def test_daily_period_uses_utc_not_server_local_date(ledger, clock):
    clock[0] = datetime(2026, 9, 13, 0, 1, tzinfo=timezone(timedelta(hours=5)))
    assert ledger.reserve(ACCOUNT, 1_000_000).reservation_id  # Sept 12 in UTC
    clock[0] = datetime(2026, 9, 12, 23, 59, tzinfo=timezone.utc)
    assert ledger.reserve(ACCOUNT, 1).status == "limit_reached"
    clock[0] = datetime(2026, 9, 13, tzinfo=timezone.utc)
    assert ledger.reserve(ACCOUNT, 1_000_000).reservation_id


def test_late_reconciliation_does_not_refund_new_period(ledger, clock):
    old = ledger.reserve(ACCOUNT, 1_000_000)
    clock[0] = datetime(2026, 9, 13, tzinfo=timezone.utc)
    assert ledger.reserve(ACCOUNT, 1_000_000).reservation_id
    ledger.reconcile(ACCOUNT, old.reservation_id, 0)
    assert ledger.reserve(ACCOUNT, 1).status == "limit_reached"


def test_restart_preserves_reserved_spend(ledger, clock):
    assert ledger.reserve(ACCOUNT, 1_000_000).reservation_id
    reopened = Allowance(ledger.path, ledger.config, clock=lambda: clock[0])
    assert reopened.reserve(ACCOUNT, 1).status == "limit_reached"


def test_concurrent_workers_cannot_overspend_shared_account(ledger, clock):
    workers = [Allowance(ledger.path, ledger.config, clock=lambda: clock[0]) for _ in range(16)]
    barrier = Barrier(len(workers))

    def reserve(worker):
        barrier.wait(timeout=10)
        return worker.reserve(ACCOUNT, 160_000)

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        decisions = list(pool.map(reserve, workers))
    approved = [d for d in decisions if d.reservation_id]
    assert len(approved) == 6  # 960,000 reserved; next full request cannot fit
    assert len({d.reservation_id for d in approved}) == 6
    assert all(d.status == "limit_reached" for d in decisions if not d.reservation_id)
    assert ledger.reserve(ACCOUNT, 40_000).reservation_id
    assert ledger.reserve(ACCOUNT, 1).status == "limit_reached"


def test_concurrent_duplicate_reconciliation_refunds_once(ledger):
    reservation = ledger.reserve(ACCOUNT, 1_000_000)
    barrier = Barrier(8)

    def reconcile(_):
        barrier.wait(timeout=10)
        ledger.reconcile(ACCOUNT, reservation.reservation_id, 800_000)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(reconcile, range(8)))
    assert ledger.reserve(ACCOUNT, 200_001).status == "limit_reached"
    assert ledger.reserve(ACCOUNT, 200_000).reservation_id


def test_locked_accounting_store_fails_closed(ledger, monkeypatch):
    monkeypatch.setattr(mod, "_SQLITE_TIMEOUT_SECONDS", 0.01)
    with sqlite3.connect(ledger.path, isolation_level=None) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        try:
            assert ledger.reserve(ACCOUNT, 1).status == "unavailable"
            assert ledger.public_status(ACCOUNT) == {"status": "unavailable"}
        finally:
            blocker.rollback()
    assert ledger.reserve(ACCOUNT, 1_000_000).reservation_id


def test_write_error_does_not_authorize_request_or_consume_budget(ledger):
    with sqlite3.connect(ledger.path) as db:
        db.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON ai_allowance_reservations
            BEGIN SELECT RAISE(ABORT, 'offline write failure'); END""")
    decision = ledger.reserve(ACCOUNT, 1_000_000)
    assert decision.status == "unavailable" and decision.reservation_id is None
    with sqlite3.connect(ledger.path) as db:
        db.execute("DROP TRIGGER fail_insert")
    assert ledger.reserve(ACCOUNT, 1_000_000).reservation_id


@pytest.mark.parametrize("value", [True, 0, -1, 0.75, "1000000", 2**63])
def test_reservation_requires_positive_integer_microusd(ledger, value):
    with pytest.raises(ValueError, match="integer microUSD"):
        ledger.reserve(ACCOUNT, value)


@pytest.mark.parametrize("value", [True, -1, 0.75, "1", 2**63])
def test_bad_actual_cost_cannot_release_reservation(ledger, value):
    reservation = ledger.reserve(ACCOUNT, 1_000_000)
    with pytest.raises(ValueError, match="integer microUSD"):
        ledger.reconcile(ACCOUNT, reservation.reservation_id, value)
    assert ledger.reserve(ACCOUNT, 1).status == "limit_reached"


def test_naive_clock_is_rejected_before_reservation(ledger, clock):
    clock[0] = datetime(2026, 9, 12)
    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.reserve(ACCOUNT, 1)


@pytest.mark.parametrize("config", [
    {"period": "weekly"}, {"enabled": "true"}, {"cap_microusd": 0},
    {"minimum_capital_microusd": -1},
])
def test_invalid_policy_configuration_is_rejected(config):
    with pytest.raises(ValueError):
        AllowanceConfig(**config)


@pytest.mark.parametrize("account_id", ["", " account", "account ", None])
def test_invalid_server_account_identifier_is_rejected(account_id):
    with pytest.raises(ValueError):
        VerifiedAccount(account_id, 100_000_000)
