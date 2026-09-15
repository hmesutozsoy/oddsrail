"""Fail-closed checks for a future delegated Polymarket ``Venue`` adapter.

These are pure contract checks, NOT a transport, signer, or production adapter.
Raw HTTP responses are intentionally separate from SDK-normalized responses.
An ACK is only acceptance evidence; cursor exhaustion is only read coverage.
Neither establishes a final order or permits releasing a cash reservation.

Sources checked 2026-09-14:
https://docs.polymarket.com/trading/place-orders (raw POST /order response)
https://docs.polymarket.com/api-reference/trade/get-single-order-by-id
https://docs.polymarket.com/api-reference/trade/get-trades (cursor envelope)
https://docs.polymarket.com/concepts/order-lifecycle (settlement states)

The guide and GET /data/trades reference disagree on trade wire units/status
spellings. Consequently this module does NOT convert raw trade responses. A
reviewed adapter must supply FillEvidence quantities explicitly in shares after
verifying its endpoint/version against actual authenticated exchange reads.
The transport still needs fixed endpoint/auth/filter binding, response byte and
time limits, freshness checks, and persistent per-trade history across restarts.
Tests of these helpers cannot certify a delegated signer or actual settlement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Context, Decimal, localcontext
from typing import Callable, TypeVar

from .contracts import OrderRequest, PreparedOrder, condition, decimal, identifier, milliseconds, token, wallet

MAX_PAGES = 20
MAX_ROWS = 2_000
MAX_EVENTS = 10_000
END_CURSOR = "LTE="
_T = TypeVar("_T")


class AdapterContractError(ValueError):
    """Sanitized response failure; never includes a response or credential."""


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise AdapterContractError(message)


def _object(value: object) -> dict:
    _require(type(value) is dict, "Expected a wire response object")
    return value


def _checked(check: Callable, value: object):
    try:
        return check(value)
    except (TypeError, ValueError):
        raise AdapterContractError("Invalid response identity") from None


def _shares(value: object) -> Decimal:
    # Wire order quantities are already shares, not six-decimal integers.
    # No float coercion, exponent strings, signs, NaN, or unbounded precision.
    _require(type(value) is str and re.fullmatch(r"(?:0|[1-9][0-9]{0,4})(?:\.[0-9]{1,18})?", value) is not None,
             "Expected a bounded decimal share string")
    try:
        return decimal(Decimal(value), maximum=Decimal(10_000))
    except ValueError:
        raise AdapterContractError("Share amount exceeds the pilot limit") from None


def _owner(value: object) -> str:
    # This is the credential-owner UUID, not the Polygon owner wallet address.
    _require(type(value) is str and re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value
    ) is not None, "Expected a credential-owner UUID")
    return value.lower()


def acknowledged_order(payload: object, prepared: PreparedOrder, *, http_status: int) -> str:
    """Return only an exact raw HTTP acceptance ACK for a post-only order.

Any error, malformed response, foreign hash, or unexpected insertion status is
an *unknown submission outcome*. The caller must reconcile the persisted hash,
never retry submission or infer that a failed parser means venue rejection.
Making/taking amounts in this ACK are deliberately not treated as fill evidence.
"""
    _require(type(http_status) is int and http_status == 200, "Submission outcome is unknown")
    raw = _object(payload)
    _require(raw.get("success") is True and raw.get("errorMsg") == "", "Submission outcome is unknown")
    order_hash = _checked(condition, raw.get("orderID"))
    _require(order_hash == condition(prepared.order_hash), "Submission hash differs from the prepared order")
    # A post-only order cannot cross immediately or enter a marketable delay.
    _require(raw.get("status") == "live", "Unexpected post-only insertion status")
    return order_hash


def verify_encoded_buy_amounts(request: OrderRequest, *, maker_amount: str, taker_amount: str) -> None:
    """Bind six-decimal unsigned order amounts to the exact economic request.

This checks only amounts. The future signer must separately recompute/bind the
EIP-712 hash, Polygon chain, exchange, maker, token, signature scope and the
postOnly HTTP field. It must sign locally without deployment or approvals.
"""
    for value in (maker_amount, taker_amount):
        _require(type(value) is str and re.fullmatch(r"[1-9][0-9]{0,11}", value) is not None,
                 "Expected canonical six-decimal order amounts")
    with localcontext(Context(prec=80)):
        _require(Decimal(maker_amount) == request.price * request.size * 1_000_000
                 and Decimal(taker_amount) == request.size * 1_000_000,
                 "Encoded amounts differ from the prepared request")


@dataclass(frozen=True)
class CursorPage:
    """Captured GET request cursor and its raw response, with no credentials.

The first request must omit next_cursor (None). Each following request must
use precisely the previous response's cursor. This does not prove the fetcher
used the right endpoint/auth/filter; a reviewed transport must bind those too.
"""

    requested_cursor: str | None
    response: object = field(repr=False)


def collect_complete_pages(
    pages: tuple[CursorPage, ...], parse_row: Callable[[object], _T],
    *, max_pages: int = MAX_PAGES, max_rows: int = MAX_ROWS,
) -> tuple[_T, ...]:
    """Validate an entire bounded raw cursor traversal, then parse every row.

Rejects a plain array/SDK page, missing sentinel, cursor cycles, count drift,
empty continuing pages and over-budget final pages. The helper never guesses
completion from an empty or short page. Duplicate rows are retained so the
domain parser can distinguish repeated events from contradictory evidence.
"""
    _require(type(max_pages) is int and 1 <= max_pages <= MAX_PAGES, "Invalid page budget")
    _require(type(max_rows) is int and 1 <= max_rows <= MAX_ROWS, "Invalid row budget")
    _require(type(pages) is tuple and 1 <= len(pages) <= max_pages, "Incomplete or over-budget traversal")
    cursor = None
    seen = set()
    rows: list[object] = []
    for index, page in enumerate(pages):
        _require(type(page) is CursorPage and page.requested_cursor == cursor, "Broken cursor chain")
        raw = _object(page.response)
        data, count, limit = raw.get("data"), raw.get("count"), raw.get("limit")
        _require(type(data) is list and type(count) is int and type(limit) is int,
                 "Missing pagination fields")
        _require(1 <= limit <= MAX_ROWS and count == len(data) and count <= limit,
                 "Inconsistent page count or limit")
        _require(len(rows) + count <= max_rows, "Traversal exceeds the row budget")
        next_cursor = raw.get("next_cursor")
        _require(type(next_cursor) is str and re.fullmatch(r"[A-Za-z0-9+/=_-]{1,128}", next_cursor) is not None,
                 "Missing or invalid continuation cursor")
        if next_cursor == END_CURSOR:
            _require(index == len(pages) - 1, "Unexpected pages after completion")
        else:
            _require(count > 0 and next_cursor not in seen, "Empty continuation or cursor cycle")
            _require(index < len(pages) - 1, "Traversal is incomplete")
            seen.add(next_cursor)
        rows.extend(data)
        cursor = next_cursor
    # All traversal validation finishes before caller-owned row parsing begins.
    return tuple(parse_row(row) for row in rows)


@dataclass(frozen=True)
class CheckedOrder:
    """Identity/economics from one authenticated raw GET /data/order response.

state remains the venue value. INVALID/MATCHED alone must not be mapped to a
final rejection/fill. A terminal-looking status is not settlement evidence.
"""

    order_hash: str
    account: str
    session_id: str
    condition_id: str
    token_id: str
    price: Decimal
    original_size: Decimal
    matched_size: Decimal
    state: str
    associated_trades: tuple[str, ...]


def checked_order(
    payload: object, prepared: PreparedOrder, *, credential_owner: str,
    expected_order_type: str, expected_expiration: int,
) -> CheckedOrder:
    """Require exact account, credential scope, order hash, token and economics.

associate_trades is optional in the public schema but mandatory here because
its absence cannot prove that there are no fills. Unknowns fail closed.
"""
    raw = _object(payload)
    request = prepared.request
    order_hash = _checked(condition, raw.get("id"))
    account = _checked(wallet, raw.get("maker_address"))
    market = _checked(condition, raw.get("market"))
    asset = _checked(token, raw.get("asset_id"))
    _require(order_hash == condition(prepared.order_hash), "Order hash mismatch")
    _require(account == wallet(prepared.account), "Trading account mismatch")
    _require(_owner(raw.get("owner")) == _owner(credential_owner), "Credential scope mismatch")
    _require(market == condition(request.condition_id) and asset == request.token_id, "Market or token mismatch")
    _require(raw.get("side") == "BUY", "Order side mismatch")
    price, original, matched = (_shares(raw.get(key)) for key in ("price", "original_size", "size_matched"))
    _require(price == request.price and original == request.size, "Order economics mismatch")
    _require(matched <= original, "Matched shares exceed original size")
    state = raw.get("status")
    _require(type(state) is str and state in {"LIVE", "INVALID", "CANCELED", "CANCELED_MARKET_RESOLVED", "MATCHED"},
             "Unsupported order status")
    _require(expected_order_type in ("GTC", "GTD") and type(expected_expiration) is int
             and 0 <= expected_expiration < 2**53, "Invalid expected order lifetime")
    _require((expected_order_type == "GTC") == (expected_expiration == 0), "Inconsistent expected order lifetime")
    _require(raw.get("order_type") == expected_order_type
             and raw.get("expiration") == str(expected_expiration), "Order lifetime mismatch")
    associated = raw.get("associate_trades")
    _require(type(associated) is list and len(associated) <= MAX_ROWS, "Missing or over-budget trade coverage")
    associated = tuple(_checked(identifier, item) for item in associated)
    _require(len(set(associated)) == len(associated), "Duplicate associated trade identity")
    return CheckedOrder(order_hash, account, prepared.session_id, market, asset, price, original, matched, state, associated)


@dataclass(frozen=True)
class FillEvidence:
    """Adapter-normalized contribution of ONE trade to ONE managed order.

All amounts are Decimal shares. In a maker trade the contribution is that
order's matched_amount, never the whole taker trade size. The adapter must
check the maker entry's owner, maker_address, asset_id, side and price before
constructing this record. There is deliberately no raw-wire constructor.

authoritative=True means the version-reviewed adapter established the state
from authenticated reconciliation; a WebSocket notification alone is False.
Only authoritative FAILED evidence can release failed-fill exposure. This
flag does not assert that an entire order/account snapshot is complete.
"""

    account: str
    session_id: str
    order_hash: str
    condition_id: str
    token_id: str
    trade_id: str
    bucket_index: int
    size: Decimal
    price: Decimal
    status: str
    updated_at_ms: int
    authoritative: bool

    def __post_init__(self):
        wallet(self.account)
        identifier(self.session_id)
        condition(self.order_hash)
        condition(self.condition_id)
        token(self.token_id)
        identifier(self.trade_id)
        _require(type(self.bucket_index) is int and 0 <= self.bucket_index <= 1_000_000, "Invalid trade bucket")
        decimal(self.size, minimum=Decimal("0.000000000000000001"), maximum=Decimal(10_000))
        decimal(self.price, minimum=Decimal("0.0001"), maximum=Decimal("0.9999"))
        _require(type(self.status) is str and self.status in {"MATCHED", "MINED", "RETRYING", "CONFIRMED", "FAILED"},
                 "Unsupported normalized settlement status")
        milliseconds(self.updated_at_ms)
        _require(type(self.authoritative) is bool, "Explicit evidence provenance required")


@dataclass(frozen=True)
class FillSummary:
    account: str
    session_id: str
    order_hash: str
    condition_id: str
    token_id: str
    matched_size: Decimal
    confirmed_size: Decimal
    failed_size: Decimal
    unsettled_size: Decimal
    trade_ids: tuple[str, ...]
    terminal_states_verified: bool


def aggregate_fills(prepared: PreparedOrder, evidence: tuple[FillEvidence, ...]) -> FillSummary:
    """Deduplicate and reconcile unordered, version-normalized trade evidence.

Changed economics/bucket, conflicting final states or a newer regression after
finality halt reconciliation. A delayed older MATCHED/MINED event cannot undo
    confirmed settlement. Every expected order/account/session field is compared.
The adapter must include retained history; this stateless function cannot detect
records lost between reads or a process restart.
    The summary never constructs OrderObservation(final=True).
"""
    _require(type(evidence) is tuple and len(evidence) <= MAX_EVENTS, "Over-budget fill evidence")
    by_trade: dict[str, list[FillEvidence]] = {}
    for item in evidence:
        _require(type(item) is FillEvidence, "Expected normalized fill evidence")
        _require(wallet(item.account) == wallet(prepared.account) and item.session_id == prepared.session_id,
                 "Fill account or session mismatch")
        _require(condition(item.order_hash) == condition(prepared.order_hash), "Fill order mismatch")
        _require(condition(item.condition_id) == condition(prepared.request.condition_id)
                 and item.token_id == prepared.request.token_id, "Fill market or token mismatch")
        # Post-only BUY fills execute at the managed maker's exact quote.
        _require(item.price == prepared.request.price and item.size <= prepared.request.size, "Fill economics mismatch")
        by_trade.setdefault(item.trade_id, []).append(item)
    _require(len(by_trade) <= MAX_ROWS, "Too many distinct fills")
    matched = confirmed = failed = Decimal(0)
    all_verified = True
    with localcontext(Context(prec=80)):
        for history in by_trade.values():
            first = history[0]
            _require(all((item.bucket_index, item.size, item.price) == (first.bucket_index, first.size, first.price)
                         for item in history), "Trade identity changed economics or bucket")
            terminal = [item for item in history if item.status in {"CONFIRMED", "FAILED"}]
            _require(len({item.status for item in terminal}) <= 1, "Conflicting terminal trade states")
            matched += first.size
            if not terminal:
                all_verified = False
                continue
            latest_terminal = max(item.updated_at_ms for item in terminal)
            _require(not any(item.status not in {"CONFIRMED", "FAILED"} and item.updated_at_ms > latest_terminal
                             for item in history), "Trade state regressed after finality")
            verified = any(item.authoritative for item in terminal)
            all_verified = all_verified and verified
            if terminal[0].status == "CONFIRMED":
                confirmed += first.size
            elif verified:
                failed += first.size
        _require(matched <= prepared.request.size, "Aggregated fills exceed order size")
        return FillSummary(wallet(prepared.account), prepared.session_id, condition(prepared.order_hash),
                           condition(prepared.request.condition_id), prepared.request.token_id,
                           matched, confirmed, failed, matched - confirmed - failed,
                           tuple(sorted(by_trade)), all_verified)


def verify_fill_coverage(order: CheckedOrder, summary: FillSummary) -> None:
    """Require exact associated-trade and share coverage for this read.

Even success is NOT an atomic snapshot or final order. Before using a final
observation the adapter must also reconcile the current terminal order, all
relevant sessions and a settlement-consistent funding snapshot. Races require
another read; never discard a larger matched amount to make these agree.
"""
    _require((order.account, order.session_id, order.order_hash, order.condition_id, order.token_id)
             == (summary.account, summary.session_id, summary.order_hash, summary.condition_id, summary.token_id),
             "Order and fill summary scope differ")
    _require(set(order.associated_trades) == set(summary.trade_ids), "Associated trades are incomplete")
    _require(order.matched_size == summary.matched_size, "Order and trade reads disagree on matched shares")
