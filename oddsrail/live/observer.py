"""Read public books and print prospective quotes; never submit orders.

Usage: python -m oddsrail.live.observer --market 0x<condition_id> --seconds 30

Metadata comes from Gamma /markets?condition_ids=... and CLOB
/clob-markets/{condition_id}. Gamma identifies eligibility and both tokens;
CLOB supplies current tick, minimum size and fees. These public observations
do not establish account eligibility, available funds or execution permission.
See https://docs.polymarket.com/market-data/market-details and the official
https://docs.polymarket.com/api-spec/clob-openapi.yaml.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
import json
import math
import re
import time

import httpx

from oddsrail.live.market_feed import SharedMarketFeed, TICK_SIZES
from oddsrail.live.quotes import ProposedQuote, QuoteBook, QuotePolicy, QuoteUnavailable, plan_quotes


MAX_METADATA_BYTES = 128 * 1024
METADATA_MAX_AGE = 30.0
METADATA_REFRESH = 20.0
_CONDITION = re.compile(r"0x[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[1-9][0-9]{0,77}\Z")
_NUMBER = re.compile(r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,12})?\Z")


class MetadataUnavailable(ValueError):
    """Public metadata was unavailable, ambiguous or malformed."""


def condition_id(value: str) -> str:
    if type(value) is not str or not _CONDITION.fullmatch(value):
        raise ValueError("market must be one lowercase 0x-prefixed 32-byte condition ID")
    return value


def _token(value) -> str:
    if type(value) is not str or not _TOKEN.fullmatch(value) or int(value) >= 2**256:
        raise MetadataUnavailable("invalid_token")
    return value


def _number(value, *, positive: bool = False) -> Decimal:
    if type(value) not in (str, int, Decimal) or not _NUMBER.fullmatch(str(value)):
        raise MetadataUnavailable("invalid_number")
    result = Decimal(value)
    if result > 1_000_000 or (positive and result <= 0):
        raise MetadataUnavailable("number_out_of_range")
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MetadataUnavailable("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_):
    raise MetadataUnavailable("nonfinite_json")


def _json(raw: bytes | str):
    try:
        return json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise MetadataUnavailable("invalid_json") from exc


@dataclass(frozen=True, slots=True)
class MarketMetadata:
    condition_id: str
    yes_token_id: str
    no_token_id: str
    tick_size: Decimal
    min_order_size: Decimal
    min_notional_usd: Decimal
    maker_fee_bps: Decimal
    taker_fee_bps: Decimal
    maker_fee_free: bool
    active: bool
    accepting_orders: bool
    closed: bool
    archived: bool
    enable_order_book: bool
    neg_risk: bool
    observed_at: float

    def __post_init__(self):
        condition_id(self.condition_id)
        _token(self.yes_token_id)
        _token(self.no_token_id)
        if self.yes_token_id == self.no_token_id:
            raise MetadataUnavailable("duplicate_tokens")
        if self.tick_size not in TICK_SIZES:
            raise MetadataUnavailable("unsupported_tick")
        for value in (self.min_order_size, self.min_notional_usd):
            _number(value, positive=True)
        for value in (self.maker_fee_bps, self.taker_fee_bps):
            if _number(value) > 10_000:
                raise MetadataUnavailable("unsupported_fee")
        for value in (self.maker_fee_free, self.active, self.accepting_orders, self.closed, self.archived,
                      self.enable_order_book, self.neg_risk):
            if type(value) is not bool:
                raise MetadataUnavailable("missing_market_state")
        if type(self.observed_at) not in (int, float) or not math.isfinite(self.observed_at) or self.observed_at < 0:
            raise MetadataUnavailable("invalid_clock")

    @property
    def eligible(self) -> bool:
        return (self.active and self.accepting_orders and self.enable_order_book
                and not self.closed and not self.archived)


def parse_market_metadata(selected_condition: str, gamma, clob, *, observed_at: float) -> MarketMetadata:
    """Bind exactly one Gamma market to the matching pair of CLOB tokens.

    For maker-only plans, Gamma and CLOB must agree that the fee curve applies
    to takers only, or both must explicitly report no fees. CLOB base fee fields
    can be nonzero even on taker-only markets; preserve them without treating
    them as the effective maker charge. No fee defaults are invented.
    Gamma describes orderMinSize as notional, while CLOB mos is a size minimum;
    apply both restrictions conservatively rather than guessing their units.
    """
    condition_id(selected_condition)
    if type(gamma) is not list or len(gamma) != 1 or type(gamma[0]) is not dict or type(clob) is not dict:
        raise MetadataUnavailable("ambiguous_market")
    market = gamma[0]
    if market.get("conditionId") != selected_condition:
        raise MetadataUnavailable("condition_mismatch")
    tokens_raw = market.get("clobTokenIds")
    if type(tokens_raw) is not str or len(tokens_raw) > 200:
        raise MetadataUnavailable("invalid_market_tokens")
    tokens = _json(tokens_raw)
    if type(tokens) is not list or len(tokens) != 2:
        raise MetadataUnavailable("not_binary_market")
    yes, no = map(_token, tokens)
    if yes == no:
        raise MetadataUnavailable("duplicate_tokens")
    clob_tokens = clob.get("t")
    if (type(clob_tokens) is not list or len(clob_tokens) != 2
            or any(type(token) is not dict for token in clob_tokens)):
        raise MetadataUnavailable("invalid_clob_tokens")
    if {_token(token.get("t")) for token in clob_tokens} != {yes, no}:
        raise MetadataUnavailable("token_mismatch")
    maker_fee, taker_fee = _number(clob.get("mbf")), _number(clob.get("tbf"))
    fees = clob.get("fd")
    if type(fees) is not dict:
        raise MetadataUnavailable("missing_fee_curve")
    rate, exponent = _number(fees.get("r")), _number(fees.get("e"))
    if rate > 1 or exponent > 10 or type(fees.get("to")) is not bool:
        raise MetadataUnavailable("invalid_fee_curve")
    gamma_fees = market.get("feeSchedule")
    maker_free = False
    if market.get("feesEnabled") is True and type(gamma_fees) is dict:
        if (_number(gamma_fees.get("rate")) != rate
                or _number(gamma_fees.get("exponent")) != exponent
                or type(gamma_fees.get("takerOnly")) is not bool
                or gamma_fees["takerOnly"] != fees["to"]):
            raise MetadataUnavailable("fee_curve_mismatch")
        maker_free = fees["to"] is True
    elif market.get("feesEnabled") is False:
        maker_free = rate == 0 and maker_fee == 0 and taker_fee == 0
    if not maker_free:
        raise MetadataUnavailable("maker_fee_unsupported")
    return MarketMetadata(
        selected_condition, yes, no, _number(clob.get("mts"), positive=True),
        _number(clob.get("mos"), positive=True), _number(market.get("orderMinSize"), positive=True),
        maker_fee, taker_fee, maker_free, market.get("active"), market.get("acceptingOrders"),
        market.get("closed"), market.get("archived"), market.get("enableOrderBook"),
        market.get("negRisk"), observed_at,
    )


async def _read_json(client: httpx.AsyncClient, url: str, *, params=None):
    async with client.stream("GET", url, params=params, follow_redirects=False,
                             timeout=httpx.Timeout(5.0), headers={"Accept": "application/json"}) as response:
        if response.status_code != 200:
            raise MetadataUnavailable("metadata_http_error")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_METADATA_BYTES:
                raise MetadataUnavailable("metadata_too_large")
        return _json(bytes(body))


async def fetch_market_metadata(selected_condition: str, *, client: httpx.AsyncClient | None = None,
                                clock: Callable[[], float] = time.monotonic) -> MarketMetadata:
    """Read fixed public endpoints, with an eight-second aggregate deadline."""
    condition_id(selected_condition)
    if client is None:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                    limits=httpx.Limits(max_connections=2)) as owned:
            return await fetch_market_metadata(selected_condition, client=owned, clock=clock)
    started = clock()  # Include network time in the metadata's age.
    try:
        async with asyncio.timeout(8):
            results = await asyncio.gather(
                _read_json(client, "https://gamma-api.polymarket.com/markets",
                           params={"condition_ids": selected_condition, "limit": "2"}),
                _read_json(client, f"https://clob.polymarket.com/clob-markets/{selected_condition}"),
                return_exceptions=True,
            )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        gamma, clob = results
        return parse_market_metadata(selected_condition, gamma, clob, observed_at=started)
    except (httpx.HTTPError, TimeoutError) as exc:
        raise MetadataUnavailable("metadata_unavailable") from exc


def plan_current(metadata: MarketMetadata, policy: QuotePolicy, feed: SharedMarketFeed,
                 *, now: float) -> tuple[ProposedQuote, ProposedQuote]:
    """Plan from current public state only; no account or execution approval."""
    if not math.isfinite(now) or not 0 <= now - metadata.observed_at <= METADATA_MAX_AGE:
        raise QuoteUnavailable("stale_metadata", "Current market eligibility is required")
    if not metadata.eligible:
        raise QuoteUnavailable("market_unavailable", "Market is not accepting orders")
    if not metadata.maker_fee_free:
        raise QuoteUnavailable("maker_fee_unsupported", "Observer plans require a verified zero maker fee")
    if (metadata.condition_id, metadata.yes_token_id, metadata.no_token_id) != (
            policy.condition_id, policy.yes_token_id, policy.no_token_id):
        raise QuoteUnavailable("identity_mismatch", "Selected market identity changed")
    generation = feed.generation
    books = []
    exchange_timestamps = []
    for token in (policy.yes_token_id, policy.no_token_id):
        snapshot = feed.fresh_snapshot(token, policy.max_book_age_ms / 1000, now)
        if snapshot is None:
            raise QuoteUnavailable("stale_or_offline", "Both current outcome books are required")
        if snapshot.best_bid is None or snapshot.best_ask is None:
            raise QuoteUnavailable("empty_book", "Both sides of each outcome book are required")
        if snapshot.tick_size is not None and snapshot.tick_size != metadata.tick_size:
            raise QuoteUnavailable("tick_changed", "Refresh metadata after a tick-size change")
        if type(snapshot.exchange_timestamp_ms) is not int or snapshot.exchange_timestamp_ms <= 0:
            raise QuoteUnavailable("missing_exchange_time", "Exchange timestamps are required for both books")
        exchange_timestamps.append(snapshot.exchange_timestamp_ms)
        try:
            books.append(QuoteBook(metadata.condition_id, token, snapshot.best_bid, snapshot.best_ask,
                                   metadata.tick_size, metadata.min_order_size,
                                   int(snapshot.received_at * 1000), snapshot.generation,
                                   metadata.active, metadata.accepting_orders))
        except ValueError as exc:
            raise QuoteUnavailable("unsupported_book", "The current book cannot be quoted") from exc
    if abs(exchange_timestamps[0] - exchange_timestamps[1]) > policy.max_book_skew_ms:
        raise QuoteUnavailable("exchange_book_skew", "Outcome exchange timestamps are too far apart")
    pair = plan_quotes(policy, books[0], books[1], now_ms=int(now * 1000), generation=generation)
    if any(quote.notional < metadata.min_notional_usd for quote in pair):
        raise QuoteUnavailable("below_minimum_notional", "Dollar cap is below the market's conservative notional minimum")
    return pair


def _emit_json(value: dict) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


async def observe_market(selected_condition: str, *, seconds: int = 30,
                         shares: Decimal = Decimal("20"), distance_cents: Decimal = Decimal("2"),
                         per_order: Decimal = Decimal("3"), emit: Callable[[dict], None] = _emit_json,
                         metadata_fetcher=fetch_market_metadata, feed_factory=SharedMarketFeed,
                         clock: Callable[[], float] = time.monotonic) -> int:
    condition_id(selected_condition)
    if type(seconds) is not int or not 1 <= seconds <= 300:
        raise ValueError("seconds must be between 1 and 300")
    # Validate options before network access; temporary IDs are never subscribed.
    QuotePolicy(selected_condition, "1", "2", shares, distance_cents / 100, per_order)
    started, plans = clock(), 0
    deadline = started + seconds
    feed, task = None, None
    last_state, last_pair, last_plan_at, last_pause_reason = None, None, started - 1, None

    def report(state: str, **fields):
        emit({"observation_only": True, "submitted_orders": 0, "state": state,
              "condition_id": selected_condition, **fields})

    def paused(reason: str):
        nonlocal last_state, last_pair, last_pause_reason
        diagnostics = {}
        if feed is not None:
            error = getattr(feed, "last_error", None)
            diagnostics = {"feed_connected": bool(getattr(feed, "connected", False)),
                           "feed_generation": feed.generation}
            if error is not None:
                diagnostics["feed_error"] = error if re.fullmatch(r"[a-z_]{1,64}", str(error)) else "feed_unavailable"
        fingerprint = (reason, tuple(diagnostics.items()))
        if last_state != "paused" or fingerprint != last_pause_reason:
            report("paused", reason=reason, **diagnostics)
        last_state, last_pair, last_pause_reason = "paused", None, fingerprint

    report("starting")
    try:
        async with asyncio.timeout(seconds):
            metadata = await metadata_fetcher(selected_condition, clock=clock)
            policy = QuotePolicy(selected_condition, metadata.yes_token_id, metadata.no_token_id,
                                 shares, distance_cents / 100, per_order)
            feed = feed_factory((metadata.yes_token_id, metadata.no_token_id), clock=clock)
            task = asyncio.create_task(feed.run())
            next_refresh = clock() + METADATA_REFRESH
            while clock() < deadline:
                now = clock()
                if now >= next_refresh:
                    paused("refreshing_metadata")
                    metadata = None
                    try:
                        metadata = await metadata_fetcher(selected_condition, clock=clock)
                        if (metadata.yes_token_id, metadata.no_token_id) != feed.token_ids:
                            paused("market_identity_changed")
                            return 1
                    except MetadataUnavailable:
                        pass
                    next_refresh = clock() + (METADATA_REFRESH if metadata else 5)
                    now = clock()
                if metadata is None:
                    paused("metadata_unavailable")
                else:
                    try:
                        pair = plan_current(metadata, policy, feed, now=now)
                    except QuoteUnavailable as exc:
                        paused(exc.code)
                    else:
                        fingerprint = tuple((quote.token_id, str(quote.price), str(quote.size)) for quote in pair)
                        if fingerprint != last_pair and now - last_plan_at >= 1:
                            report("quote_plan", generation=feed.generation, quotes=[{
                                "token_id": quote.token_id, "side": quote.side, "post_only": quote.post_only,
                                "price": str(quote.price), "shares": str(quote.size),
                                "notional_usd": str(quote.notional),
                            } for quote in pair])
                            last_pair, last_plan_at, last_state = fingerprint, now, "quote_plan"
                            plans += 1
                # Bounded evaluation rate; feed processing remains event-driven.
                await asyncio.sleep(min(0.1, max(0, deadline - clock())))
    except TimeoutError:
        pass
    except MetadataUnavailable as exc:
        paused(str(exc))
        return 1
    finally:
        if feed is not None:
            await feed.close()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        report("stopped", quote_plans=plans)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Observe public quotes for one binary market; submits zero orders.")
    parser.add_argument("--market", required=True, help="Lowercase 0x-prefixed condition ID")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--shares", type=Decimal, default=Decimal("20"))
    parser.add_argument("--distance-cents", type=Decimal, default=Decimal("2"))
    parser.add_argument("--per-order", type=Decimal, default=Decimal("3"))
    args = parser.parse_args(argv)
    try:
        return asyncio.run(observe_market(args.market, seconds=args.seconds, shares=args.shares,
                                         distance_cents=args.distance_cents, per_order=args.per_order))
    except (ValueError, ArithmeticError) as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
