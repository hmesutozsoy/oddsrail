"""Pure, fail-closed planning for two resting BUY quotes in one binary market.

This module never signs, sends, cancels, or simulates an order. A proposed pair
does not guarantee either fill or profitability. Execution must independently
enforce account-wide reservations, eligibility, and current market metadata.

``distance`` is dollars per share, matching normalized ``mm.edge`` (the builder
displays cents). Each outcome uses its own midpoint, not ``1 - yes_midpoint``.
Book times use the same monotonic millisecond clock as ``now_ms``. The feed is
responsible for source timestamp validation and invalidating its generation on
disconnect, gaps, or changes that require a fresh snapshot. The runner must
obtain current active/accepting-orders metadata rather than invent these flags.

Venue metadata source: https://docs.polymarket.com/api-reference/market-data/get-order-book
The 0.01-share precision here is a conservative planner policy, not a claim that
the exchange's minimum order size is always 0.01 shares. That minimum is read
from each validated book and can cause the entire proposed pair to be refused.
"""

from dataclasses import dataclass, field
from decimal import Context, Decimal, ROUND_FLOOR, localcontext
import re


_TICKS = frozenset(Decimal(value) for value in ("0.1", "0.01", "0.001", "0.0001"))
_SHARE_STEP = Decimal("0.01")
_MAX_AMOUNT = Decimal("1000000")
_ONE = Decimal("1")
_MAX_CLOCK = 2**63 - 1


class QuoteUnavailable(ValueError):
    """Valid inputs cannot currently produce a complete, eligible quote pair."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _decimal(value, label: str, *, maximum: Decimal = _MAX_AMOUNT) -> Decimal:
    # Do not turn floats, booleans or numeric strings into trading amounts.
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")
    if not 0 < value <= maximum:
        raise ValueError(f"{label} is outside the supported positive range")
    # Bound both input precision and work independently of Decimal context.
    parts = value.as_tuple()
    if len(parts.digits) > 30 or not -12 <= parts.exponent <= 6:
        raise ValueError(f"{label} has unsupported precision")
    return value


def _integer(value, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def _condition(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"0x[0-9a-f]{64}", value):
        raise ValueError("condition_id must be a canonical lowercase 32-byte hex ID")
    return value


def _token(value: str) -> str:
    if (type(value) is not str or not re.fullmatch(r"[1-9][0-9]{0,77}", value)
            or int(value) >= 2**256):
        raise ValueError("token_id must be a canonical positive uint256 string")
    return value


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


@dataclass(frozen=True, slots=True)
class QuotePolicy:
    condition_id: str
    yes_token_id: str
    no_token_id: str
    shares: Decimal
    distance: Decimal
    per_order_usd: Decimal
    max_book_age_ms: int = 5_000
    max_book_skew_ms: int = 1_000

    def __post_init__(self):
        _condition(self.condition_id)
        _token(self.yes_token_id)
        _token(self.no_token_id)
        if self.yes_token_id == self.no_token_id:
            raise ValueError("The two outcome token IDs must be distinct")
        _decimal(self.shares, "shares")
        _decimal(self.distance, "distance", maximum=Decimal("0.9999"))
        _decimal(self.per_order_usd, "per_order_usd")
        with localcontext(Context(prec=64)):
            if self.shares != _floor_step(self.shares, _SHARE_STEP):
                raise ValueError("shares must use at most two decimal places")
        _integer(self.max_book_age_ms, "max_book_age_ms", 1, 60_000)
        _integer(self.max_book_skew_ms, "max_book_skew_ms", 0, 60_000)
        if self.max_book_skew_ms > self.max_book_age_ms:
            raise ValueError("max_book_skew_ms cannot exceed max_book_age_ms")


@dataclass(frozen=True, slots=True)
class QuoteBook:
    condition_id: str
    token_id: str
    best_bid: Decimal
    best_ask: Decimal
    tick_size: Decimal
    min_order_size: Decimal
    observed_at_ms: int
    generation: int
    active: bool
    accepting_orders: bool

    def __post_init__(self):
        _condition(self.condition_id)
        _token(self.token_id)
        _decimal(self.best_bid, "best_bid", maximum=_ONE)
        _decimal(self.best_ask, "best_ask", maximum=_ONE)
        if not self.best_bid < self.best_ask < _ONE:
            raise ValueError("Book must have positive, uncrossed bid/ask prices below one")
        _decimal(self.tick_size, "tick_size", maximum=_ONE)
        if self.tick_size not in _TICKS:
            raise ValueError("tick_size is not supported")
        _decimal(self.min_order_size, "min_order_size")
        _integer(self.observed_at_ms, "observed_at_ms", 0, _MAX_CLOCK)
        _integer(self.generation, "generation", 0, _MAX_CLOCK)
        if type(self.active) is not bool or type(self.accepting_orders) is not bool:
            raise ValueError("Market active/accepting_orders flags must be explicit booleans")


@dataclass(frozen=True, slots=True)
class ProposedQuote:
    condition_id: str
    token_id: str
    price: Decimal
    size: Decimal
    tick_size: Decimal
    min_order_size: Decimal
    observed_at_ms: int
    generation: int
    side: str = field(default="BUY", init=False)
    post_only: bool = field(default=True, init=False)

    @property
    def notional(self) -> Decimal:
        with localcontext(Context(prec=64)):
            return self.price * self.size


def plan_quotes(
    policy: QuotePolicy,
    yes_book: QuoteBook,
    no_book: QuoteBook,
    *,
    now_ms: int,
    generation: int,
) -> tuple[ProposedQuote, ProposedQuote]:
    """Return YES then NO quotes together, or refuse the entire pair.

    ``QuoteUnavailable.code`` is stable for expected missing eligibility. Other
    ValueErrors indicate malformed arguments; callers should not coerce them or
    fall back to legacy settings. post_only must remain true at order submission
    because the book may change after planning.
    """
    if type(policy) is not QuotePolicy or type(yes_book) is not QuoteBook or type(no_book) is not QuoteBook:
        raise ValueError("Provide a QuotePolicy and two validated QuoteBooks")
    _integer(now_ms, "now_ms", 0, _MAX_CLOCK)
    _integer(generation, "generation", 0, _MAX_CLOCK)
    for book, token_id in ((yes_book, policy.yes_token_id), (no_book, policy.no_token_id)):
        if book.condition_id != policy.condition_id or book.token_id != token_id:
            raise QuoteUnavailable("identity_mismatch", "Book identity does not match the selected outcome")
        if book.generation != generation:
            raise QuoteUnavailable("generation_mismatch", "A fresh pair from the current feed generation is required")
        if not book.active or not book.accepting_orders:
            raise QuoteUnavailable("market_unavailable", "Both outcomes must be active and accepting orders")
        age = now_ms - book.observed_at_ms
        if age < 0 or age > policy.max_book_age_ms:
            raise QuoteUnavailable("stale_book", "A current book is required for both outcomes")
    if abs(yes_book.observed_at_ms - no_book.observed_at_ms) > policy.max_book_skew_ms:
        raise QuoteUnavailable("book_skew", "Outcome books were observed too far apart")

    # Fixed precision makes output independent of the caller's Decimal context.
    # All accepted operands are bounded; 64 digits covers exact multiplication
    # and flooring, including a cap infinitesimally below a share-step boundary.
    with localcontext(Context(prec=64)):
        quotes = []
        for book in (yes_book, no_book):
            midpoint = (book.best_bid + book.best_ask) / 2
            price = _floor_step(
                min(midpoint - policy.distance, book.best_ask - book.tick_size),
                book.tick_size,
            )
            if not book.tick_size <= price <= _ONE - book.tick_size:
                raise QuoteUnavailable("no_valid_price", "Distance leaves no valid resting quote price")
            size = _floor_step(min(policy.shares, policy.per_order_usd / price), _SHARE_STEP)
            if size < book.min_order_size:
                raise QuoteUnavailable("below_minimum_size", "The requested share size or dollar cap is below the market minimum")
            quotes.append(ProposedQuote(
                condition_id=policy.condition_id, token_id=book.token_id,
                price=price, size=size, tick_size=book.tick_size,
                min_order_size=book.min_order_size,
                observed_at_ms=book.observed_at_ms, generation=book.generation,
            ))
        return quotes[0], quotes[1]
