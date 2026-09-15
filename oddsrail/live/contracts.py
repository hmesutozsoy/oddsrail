"""Strict internal contracts for the BUY-only, one-market live pilot.

These objects are trusted server adapter outputs, never browser assertions.
No private key, API credential, signature or signed payload belongs in status
objects, events, exceptions or the execution database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_CEILING, localcontext
from typing import Protocol

MICRO = Decimal(1_000_000)
MAX_USD = Decimal(100_000)


def decimal(value: Decimal, *, minimum=Decimal(0), maximum=MAX_USD) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or not minimum <= value <= maximum:
        raise ValueError("Expected a finite Decimal in range")
    if len(value.as_tuple().digits) > 40 or value.as_tuple().exponent < -18:
        raise ValueError("Decimal precision exceeds the pilot limit")
    return value


def money(value: Decimal) -> int:
    with localcontext(Context(prec=80)):
        return int((decimal(value) * MICRO).to_integral_value(rounding=ROUND_CEILING))


def wallet(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ValueError("Invalid wallet")
    return value.lower()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value):
        raise ValueError("Invalid identifier")
    return value


def condition(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise ValueError("Invalid condition or order hash")
    return value.lower()


def token(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,77}", value) or int(value) >= 2**256:
        raise ValueError("Invalid outcome token")
    return value


def milliseconds(value: int) -> int:
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("Invalid timestamp")
    return value


@dataclass(frozen=True)
class AccountPolicy:
    owner: str
    account: str
    capital: Decimal
    per_order: Decimal
    per_market: Decimal
    daily_loss: Decimal
    max_open_orders: int = 10

    def __post_init__(self):
        wallet(self.owner)
        wallet(self.account)
        for value in (self.capital, self.per_order, self.per_market, self.daily_loss):
            decimal(value, minimum=Decimal("0.01"))
            with localcontext(Context(prec=80)):
                if value % Decimal("0.000001"):
                    raise ValueError("Budget precision exceeds microdollars")
        if type(self.max_open_orders) is not int or not 2 <= self.max_open_orders <= 100:
            raise ValueError("Invalid open-order limit")


@dataclass(frozen=True)
class AgentPolicy:
    account: str
    agent_id: str
    session_id: str
    condition_id: str
    tokens: tuple[str, str]
    capital: Decimal
    per_order: Decimal
    per_market: Decimal

    def __post_init__(self):
        wallet(self.account)
        identifier(self.agent_id)
        identifier(self.session_id)
        condition(self.condition_id)
        if not isinstance(self.tokens, tuple) or len(self.tokens) != 2 or self.tokens[0] == self.tokens[1]:
            raise ValueError("Select exactly two distinct outcomes")
        for item in self.tokens:
            token(item)
        for value in (self.capital, self.per_order, self.per_market):
            decimal(value, minimum=Decimal("0.01"))
            with localcontext(Context(prec=80)):
                if value % Decimal("0.000001"):
                    raise ValueError("Budget precision exceeds microdollars")


@dataclass(frozen=True)
class AccountSnapshot:
    """Complete coordinator view, with amounts from current authenticated reads.

    external_exposure/external_reserved exclude this store's own orders, which
    the coordinator reconciles across every managed session. A single public
    holdings lookup or one session's order list cannot produce this snapshot.
    captured_at_ms is the start of the reads, so fills arriving during a read
    retain a cash hold. The adapter must verify current chain cash/allowances,
    complete open orders and positions, eligibility and daily loss accounting.
    """

    account: str
    owner: str
    captured_at_ms: int
    cash: Decimal
    allowance: Decimal
    external_reserved: Decimal
    external_exposure: tuple[tuple[str, Decimal], ...]
    daily_loss: Decimal
    complete: bool
    eligible: bool
    external_open_orders: int = 0

    def __post_init__(self):
        wallet(self.account)
        wallet(self.owner)
        milliseconds(self.captured_at_ms)
        for value in (self.cash, self.allowance, self.external_reserved, self.daily_loss):
            decimal(value)
            with localcontext(Context(prec=80)):
                if value % Decimal("0.000001"):
                    raise ValueError("Balance precision exceeds microdollars")
        if type(self.complete) is not bool or type(self.eligible) is not bool:
            raise ValueError("Explicit account eligibility and completeness required")
        if type(self.external_open_orders) is not int or not 0 <= self.external_open_orders <= 10000:
            raise ValueError("Invalid external order count")
        if not isinstance(self.external_exposure, tuple) or len(self.external_exposure) > 1000:
            raise ValueError("Invalid exposure snapshot")
        seen = set()
        for key, value in self.external_exposure:
            key = condition(key)
            decimal(value)
            if key in seen:
                raise ValueError("Duplicate market exposure")
            seen.add(key)


@dataclass(frozen=True)
class Permission:
    account: str
    session_id: str
    verified_at_ms: int
    expires_at_ms: int
    active: bool
    can_trade: bool

    def __post_init__(self):
        wallet(self.account)
        identifier(self.session_id)
        milliseconds(self.verified_at_ms)
        milliseconds(self.expires_at_ms)
        if type(self.active) is not bool or type(self.can_trade) is not bool:
            raise ValueError("Explicit trading permission required")


@dataclass(frozen=True)
class OrderRequest:
    condition_id: str
    token_id: str
    price: Decimal
    size: Decimal
    tick_size: Decimal
    min_order_size: Decimal
    fee_buffer_rate: Decimal = Decimal(0)
    side: str = "BUY"
    post_only: bool = True

    def __post_init__(self):
        condition(self.condition_id)
        token(self.token_id)
        decimal(self.price, minimum=Decimal("0.0001"), maximum=Decimal("0.9999"))
        decimal(self.size, minimum=Decimal("0.01"), maximum=Decimal(10000))
        if self.tick_size not in tuple(map(Decimal, ("0.1", "0.01", "0.001", "0.0001"))):
            raise ValueError("Unsupported tick")
        decimal(self.min_order_size, minimum=Decimal("0.01"), maximum=Decimal(10000))
        decimal(self.fee_buffer_rate, maximum=Decimal("0.1"))
        with localcontext(Context(prec=80)):
            if self.price % self.tick_size or self.size % Decimal("0.01") or self.size < self.min_order_size:
                raise ValueError("Invalid tick, size precision or minimum size")
            if self.fee_buffer_rate % Decimal("0.000001"):
                raise ValueError("Invalid fee precision")
        if self.side != "BUY" or self.post_only is not True:
            raise ValueError("Pilot supports post-only BUY orders only")

    @property
    def cost(self) -> int:
        with localcontext(Context(prec=80)):
            return money(self.price * self.size * (1 + self.fee_buffer_rate))


@dataclass(frozen=True)
class PreparedOrder:
    """Signer-produced hash bound to the exact economic request.

    The adapter holds its corresponding signed payload privately. A restart
    reconciles this hash; the engine never retries or reconstructs submission.
    """

    account: str
    session_id: str
    intent_id: str
    order_hash: str
    request: OrderRequest

    def __post_init__(self):
        wallet(self.account)
        identifier(self.session_id)
        identifier(self.intent_id)
        condition(self.order_hash)
        if not isinstance(self.request, OrderRequest):
            raise ValueError("Invalid prepared request")


@dataclass(frozen=True)
class OrderObservation:
    """Authoritative per-order reconciliation, not a submission/cancel ACK.

    final=True requires complete order and trade reads, no pending settlement,
    and matched_size==confirmed_size+failed_size. 404/absence is None.
    All quantities are monotonic and deduplicated. FAILED trades count only
    when authoritative complete reads establish permanent settlement failure.
    """

    account: str
    session_id: str
    order_hash: str
    observed_at_ms: int
    state: str
    matched_size: Decimal
    confirmed_size: Decimal
    final: bool = False
    failed_size: Decimal = Decimal(0)

    def __post_init__(self):
        wallet(self.account)
        identifier(self.session_id)
        condition(self.order_hash)
        milliseconds(self.observed_at_ms)
        if self.state not in ("open", "matched", "canceled", "filled", "rejected"):
            raise ValueError("Invalid observed state")
        decimal(self.matched_size, maximum=Decimal(10000))
        decimal(self.confirmed_size, maximum=Decimal(10000))
        decimal(self.failed_size, maximum=Decimal(10000))
        with localcontext(Context(prec=80)):
            resolved = self.confirmed_size + self.failed_size
            if resolved > self.matched_size or type(self.final) is not bool:
                raise ValueError("Invalid cumulative fill")
            if self.final and (self.state not in ("canceled", "filled", "rejected") or self.matched_size != resolved):
                raise ValueError("Settlement is incomplete")
        if self.state == "rejected" and (self.matched_size or not self.final):
            raise ValueError("Invalid final rejection")


class Venue(Protocol):
    """No default implementation can trade; inject a reviewed delegated adapter.

    prepare MUST be pure local signing (no deployment, allowances or writes).
    submit MUST make at most one HTTP attempt, post-only, with a bounded
    lifetime and return only the exact acknowledged hash. Lookup must reconcile
    complete session order/trade data. heartbeat is credential-scoped, separate
    from WebSocket PING. No method may use the shared operator trading client.
    """

    async def account_snapshot(self, account: str) -> AccountSnapshot: ...
    async def permission(self, account: str, session_id: str) -> Permission: ...
    async def heartbeat(self, account: str, session_id: str) -> None: ...
    async def prepare(self, account: str, session_id: str, intent_id: str, request: OrderRequest) -> PreparedOrder: ...
    async def submit(self, prepared: PreparedOrder) -> str: ...
    async def lookup(self, account: str, session_id: str, order_hash: str) -> OrderObservation | None: ...
    async def cancel(self, account: str, session_id: str, order_hash: str) -> None: ...
