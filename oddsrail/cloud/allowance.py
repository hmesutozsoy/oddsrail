"""Private accounting for a possible future hosted AI allowance.

DISABLED BY DEFAULT. No route, model invocation, or verification is provided.
Do not enable until the operator has privately configured the cap, chosen the
period, and defined how the server verifies eligible capital.
VerifiedAccount is a server-side assertion,
not a verifier: construct it only from authenticated stable account identity
and fresh, authoritative capital evidence. Never deserialize it from a client
payload, accept a client-selected account ID, or use paper balances as evidence.

The caller must bound provider input/output and reserve the maximum possible
cost *before* each request, including retries. Only an included decision with
a new reservation_id authorizes one call. Do not reuse a reservation for a
retry. Reconcile only a final verified provider cost, including all billable
components; failed/unknown outcomes keep the full reservation. This module
cannot make an unbounded provider request stay within its reservation.

All money is integer microUSD (1 USD = 1_000_000). Calendar periods use UTC.
Reservations remain in their original period when reconciled later. Changing
the configured period is an explicit policy migration, not a reset operation.
Only public_status() or Reservation.public_status() may be sent to clients;
configuration, reservations, costs, and eligibility evidence remain private.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Callable, Literal
from uuid import uuid4


Status = Literal["included", "unavailable", "limit_reached"]
Period = Literal["daily", "monthly", "lifetime"]
_SQLITE_TIMEOUT_SECONDS = 5.0
_MAX_INTEGER = 2**63 - 1


def _money(value: int, name: str, *, positive: bool = False) -> None:
    if type(value) is not int or not (int(positive) <= value <= _MAX_INTEGER):
        raise ValueError(f"{name} must be an integer microUSD amount in range")


@dataclass(frozen=True)
class AllowanceConfig:
    enabled: bool = False
    period: Period | None = None
    cap_microusd: int | None = None
    minimum_capital_microusd: int = 100_000_000

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if self.period not in (None, "daily", "monthly", "lifetime"):
            raise ValueError("period must be daily, monthly, or lifetime")
        if self.enabled and self.period is None:
            raise ValueError("an explicit period is required before enabling the allowance")
        if self.enabled and self.cap_microusd is None:
            raise ValueError("an explicit private cap is required before enabling the allowance")
        if self.cap_microusd is not None:
            _money(self.cap_microusd, "cap_microusd", positive=True)
        _money(self.minimum_capital_microusd, "minimum_capital_microusd")


@dataclass(frozen=True)
class VerifiedAccount:
    """Trusted server assertion; this type does not perform verification."""

    account_id: str
    eligible_capital_microusd: int

    def __post_init__(self):
        if (not isinstance(self.account_id, str) or not self.account_id
                or self.account_id != self.account_id.strip() or len(self.account_id) > 200):
            raise ValueError("account_id must be a stable, nonempty server account identifier")
        _money(self.eligible_capital_microusd, "eligible_capital_microusd")


@dataclass(frozen=True)
class Reservation:
    status: Status
    reservation_id: str | None = None

    def public_status(self) -> dict[str, Status]:
        return {"status": self.status}


class Allowance:
    """One SQLite file shared by all workers; no process-local spend counter."""

    def __init__(self, path: Path, config: AllowanceConfig | None = None, *,
                 clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.config = config or AllowanceConfig()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ai_allowance_reservations (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                period TEXT NOT NULL,
                bucket TEXT NOT NULL,
                reserved_microusd INTEGER NOT NULL CHECK (reserved_microusd > 0),
                actual_microusd INTEGER CHECK (actual_microusd >= 0
                    AND actual_microusd <= reserved_microusd)
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS ai_allowance_account_period
                ON ai_allowance_reservations (account_id, period, bucket)""")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(str(self.path), timeout=_SQLITE_TIMEOUT_SECONDS,
                             isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _eligible(self, account: VerifiedAccount | None) -> bool:
        if account is not None and not isinstance(account, VerifiedAccount):
            raise TypeError("account must be a trusted-server VerifiedAccount")
        return bool(self.config.enabled and account is not None
                    and account.eligible_capital_microusd >= self.config.minimum_capital_microusd)

    def _period(self) -> tuple[str, str]:
        period = self.config.period
        if period == "lifetime":
            return period, "lifetime"
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("the server clock must return a timezone-aware datetime")
        utc = now.astimezone(timezone.utc)
        if period == "daily":
            return period, utc.date().isoformat()
        if period == "monthly":
            return period, utc.strftime("%Y-%m")
        raise ValueError("an explicit allowance period is required")

    @staticmethod
    def _used(db, account_id: str, period: str, bucket: str) -> int:
        row = db.execute("""SELECT COALESCE(SUM(COALESCE(actual_microusd,
            reserved_microusd)), 0) FROM ai_allowance_reservations
            WHERE account_id=? AND period=? AND bucket=?""",
            (account_id, period, bucket)).fetchone()
        return row[0]

    def public_status(self, account: VerifiedAccount | None) -> dict[str, Status]:
        if not self._eligible(account):
            return {"status": "unavailable"}
        period, bucket = self._period()
        try:
            with self._transaction() as db:
                used = self._used(db, account.account_id, period, bucket)
            return {"status": "included" if used < self.config.cap_microusd else "limit_reached"}
        except sqlite3.Error:
            return {"status": "unavailable"}

    def reserve(self, account: VerifiedAccount | None, max_cost_microusd: int) -> Reservation:
        """Atomically reserve one request's full worst-case cost before calling it."""
        _money(max_cost_microusd, "max_cost_microusd", positive=True)
        if not self._eligible(account):
            return Reservation("unavailable")
        period, bucket = self._period()
        try:
            with self._transaction() as db:
                used = self._used(db, account.account_id, period, bucket)
                if max_cost_microusd > self.config.cap_microusd - used:
                    return Reservation("limit_reached")
                reservation_id = uuid4().hex
                db.execute("""INSERT INTO ai_allowance_reservations
                    (id, account_id, period, bucket, reserved_microusd)
                    VALUES (?, ?, ?, ?, ?)""",
                    (reservation_id, account.account_id, period, bucket, max_cost_microusd))
            return Reservation("included", reservation_id)
        except sqlite3.Error:
            # A missing/locked/broken accounting store never authorizes a call.
            return Reservation("unavailable")

    def reconcile(self, account: VerifiedAccount, reservation_id: str,
                  actual_cost_microusd: int | None) -> None:
        """Record final verified cost once; None means failed/unknown, retain all.

        Repeating the same final cost is idempotent. Changing it is an error.
        Reconciliation is allowed after disabling or loss of capital eligibility
        so already-reserved work can settle; ownership is still required.
        """
        if not isinstance(account, VerifiedAccount):
            raise TypeError("account must be a trusted-server VerifiedAccount")
        if actual_cost_microusd is not None:
            _money(actual_cost_microusd, "actual_cost_microusd")
        with self._transaction() as db:
            row = db.execute("""SELECT reserved_microusd, actual_microusd
                FROM ai_allowance_reservations WHERE id=? AND account_id=?""",
                (reservation_id, account.account_id)).fetchone()
            if row is None:
                raise KeyError("reservation not found for this account")
            if actual_cost_microusd is None:
                return
            if actual_cost_microusd > row["reserved_microusd"]:
                raise ValueError("actual cost exceeds the reserved maximum; reservation retained")
            if row["actual_microusd"] is not None:
                if actual_cost_microusd != row["actual_microusd"]:
                    raise ValueError("reservation was already reconciled with a different cost")
                return
            db.execute("""UPDATE ai_allowance_reservations SET actual_microusd=?
                WHERE id=? AND account_id=?""",
                (actual_cost_microusd, reservation_id, account.account_id))
