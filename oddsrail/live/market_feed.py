"""Bounded public market stream, shared by consumers in one worker/event loop.

Wire schema: https://docs.polymarket.com/market-data/realtime-data (API tab).
No credentials, trading actions, REST fallback, or implicit subscriptions. A
reconnect discards every book and requires new full snapshots. The venue has no
sequence number in this stream: timestamps and advertised best prices detect
some gaps, but cannot prove that an upstream event was never omitted.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from decimal import Decimal
import json
import math
import random
import re
import time
from typing import Any


MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_TOKENS = 40
MAX_FRAME_BYTES = 512 * 1024
MAX_LEVELS = 2000  # Reject an oversized book; never silently truncate it.
MAX_EVENTS_PER_FRAME = 100
MAX_CHANGES_PER_EVENT = 2000
MAX_EVENTS_PER_SECOND = 2000
HEARTBEAT_SECONDS = 10.0
HEARTBEAT_TIMEOUT = 25.0
MAX_EXCHANGE_LAG_SECONDS = 30.0
MAX_FUTURE_SECONDS = 5.0
TICK_SIZES = frozenset(map(Decimal, ("0.1", "0.01", "0.001", "0.0001")))
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,12})?\Z")
_TOKEN = re.compile(r"[1-9][0-9]{0,77}\Z")


class FeedProtocolError(ValueError):
    """The current connection's books are no longer safe to consume."""


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    token_id: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    tick_size: Decimal | None
    received_at: float
    exchange_timestamp_ms: int | None
    generation: int

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0][0] if self.asks else None


def _token(value: Any) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value) or int(value) >= 2**256:
        raise FeedProtocolError("invalid_token")
    return value


def _number(value: Any, *, price: bool = False, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise FeedProtocolError("invalid_decimal")
    result = Decimal(value)
    if (price and result > 1) or (positive and result <= 0):
        raise FeedProtocolError("decimal_out_of_range")
    return result


def _tick(value: Any) -> Decimal:
    result = _number(value, price=True, positive=True)
    if result not in TICK_SIZES:
        raise FeedProtocolError("invalid_tick_size")
    return result


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise FeedProtocolError("duplicate_json_key")
        obj[key] = value
    return obj


def _reject_constant(value):
    raise FeedProtocolError("nonfinite_json")


def _connect(url: str, **kwargs):
    # Pinned supported transport API: websockets >=15.0.1,<16.
    from websockets.asyncio.client import connect
    import ssl
    import certifi

    # Match HTTPX's portable CA bundle; some macOS Python installs have no
    # system CA file. Certificate and hostname verification remain required.
    kwargs["ssl"] = ssl.create_default_context(cafile=certifi.where())
    return connect(url, **kwargs)


class SharedMarketFeed:
    """One connection for a fixed token set, with immutable current snapshots.

    Run ``asyncio.create_task(feed.run())`` once. Consumers keep ``revision`` and
    await ``wait_for_update(revision, timeout=...)``; timeout also permits them to
    react to books becoming stale when no event arrives. Read ``fresh_snapshot``
    immediately before planning. Snapshots retained by a consumer do not remain
    valid after a disconnect: check the feed again before any side effect.

    ``connector(url, **kwargs)`` returns an async context manager whose socket
    has async ``send(str)`` and ``recv()``. Clock, wall clock, sleep and jitter
    are injectable for offline tests. This object is not thread-safe.
    """

    def __init__(
        self,
        token_ids: Iterable[str],
        *,
        connector: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        if isinstance(token_ids, (str, bytes)):
            raise ValueError("Pass a collection of token IDs")
        # Bound iteration too: do not materialize arbitrary infinite inputs.
        tokens = []
        for value in token_ids:
            if len(tokens) == MAX_TOKENS:
                raise ValueError("One feed supports at most 40 tokens")
            tokens.append(_token(value))
        if not tokens or len(set(tokens)) != len(tokens):
            raise ValueError("Token IDs must be nonempty and unique")
        self.token_ids = tuple(tokens)
        self._tokens = frozenset(tokens)
        self._connector = connector or _connect
        self._clock, self._wall_clock = clock, wall_clock
        self._sleep, self._jitter = sleep, jitter
        self._books: dict[str, BookSnapshot] = {}
        self._ticks: dict[str, Decimal] = {}
        self._timestamps: dict[str, int] = {}
        self._rate: deque[float] = deque()
        self._event = asyncio.Event()
        self._runner: asyncio.Task | None = None
        self._closed = False
        self._connected = False
        self._generation = 0
        self._revision = 0
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def revision(self) -> int:
        return self._revision

    def _notify(self):
        self._revision += 1
        # Replace, rather than clear, the event so all current waiters wake.
        previous, self._event = self._event, asyncio.Event()
        previous.set()

    def invalidate(self, reason: str = "disconnected") -> None:
        """Immediately remove all usable books; the owner must reconnect."""
        self._connected = False
        self._books.clear()
        self._ticks.clear()
        self._timestamps.clear()
        self.last_error = reason
        self._notify()

    def fresh_snapshot(self, token_id: str, max_age: float, now: float | None = None) -> BookSnapshot | None:
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or max_age <= 0:
            raise ValueError("max_age must be finite and positive")
        now = self._clock() if now is None else now
        book = self._books.get(token_id)
        if (not self._connected or self._closed or book is None or
                not math.isfinite(now) or not 0 <= now - book.received_at <= max_age or
                book.generation != self._generation):
            return None
        # A just-received event may already have spent seconds in an upstream
        # queue. The larger parser bound permits diagnosis/recovery; a consumer
        # with a stricter freshness requirement must not trade on that backlog.
        if book.exchange_timestamp_ms is None:
            return None
        exchange_age = self._wall_clock() - book.exchange_timestamp_ms / 1000
        if not math.isfinite(exchange_age) or not -MAX_FUTURE_SECONDS <= exchange_age <= max_age:
            return None
        return book

    async def wait_for_update(self, after_revision: int, timeout: float | None = None) -> int:
        if type(after_revision) is not int or after_revision < 0:
            raise ValueError("Invalid revision")
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Invalid timeout")
        if self._revision <= after_revision and not self._closed:
            event = self._event
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except TimeoutError:
                pass
        return self._revision

    def _timestamp(self, event: dict) -> int:
        value = event.get("timestamp")
        if (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", value)):
            raise FeedProtocolError("missing_or_invalid_timestamp")
        timestamp = int(value)
        lag = self._wall_clock() - timestamp / 1000
        if not math.isfinite(lag) or not -MAX_FUTURE_SECONDS <= lag <= MAX_EXCHANGE_LAG_SECONDS:
            raise FeedProtocolError("delayed_exchange_event")
        return timestamp

    @staticmethod
    def _levels(value: Any, *, bids: bool) -> tuple[tuple[Decimal, Decimal], ...]:
        if not isinstance(value, list) or len(value) > MAX_LEVELS:
            raise FeedProtocolError("invalid_book_levels")
        levels = {}
        for row in value:
            if not isinstance(row, dict):
                raise FeedProtocolError("invalid_book_level")
            price = _number(row.get("price"), price=True)
            size = _number(row.get("size"), positive=True)
            if price in levels:
                raise FeedProtocolError("duplicate_price_level")
            levels[price] = size
        return tuple(sorted(levels.items(), reverse=bids))

    @staticmethod
    def _check_book(book: BookSnapshot):
        if book.best_bid is not None and book.best_ask is not None and book.best_bid >= book.best_ask:
            raise FeedProtocolError("crossed_book")
        if len(book.bids) > MAX_LEVELS or len(book.asks) > MAX_LEVELS:
            raise FeedProtocolError("too_many_book_levels")

    def process_message(self, raw: str | bytes) -> str | None:
        """Parse a raw frame atomically. Raises after invalidating on errors.

        Primarily exposed for captured-frame/offline verification. Only run()
        establishes a usable connection. PING/PONG never refresh a quote.
        """
        try:
            if not isinstance(raw, (str, bytes)) or len(raw) > MAX_FRAME_BYTES:
                raise FeedProtocolError("frame_too_large")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="strict")
            elif len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
                raise FeedProtocolError("frame_too_large")
            now = self._clock()
            if not math.isfinite(now):
                raise FeedProtocolError("invalid_clock")
            while self._rate and self._rate[0] <= now - 1:
                self._rate.popleft()
            if raw in ("PING", "PONG"):
                count = 1
                events = None
            else:
                value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                events = value if isinstance(value, list) else [value]
                if not events or len(events) > MAX_EVENTS_PER_FRAME:
                    raise FeedProtocolError("invalid_event_batch")
                count = len(events)
            if len(self._rate) + count > MAX_EVENTS_PER_SECOND:
                raise FeedProtocolError("event_rate_exceeded")
            self._rate.extend([now] * count)
            if events is None:
                return "PONG" if raw == "PING" else None
            books, ticks, timestamps = dict(self._books), dict(self._ticks), dict(self._timestamps)
            for event in events:
                self._apply(event, books, ticks, timestamps, now)
            changed = books != self._books or ticks != self._ticks
            self._books, self._ticks, self._timestamps = books, ticks, timestamps
            if changed:
                self._notify()
            return None
        except (FeedProtocolError, ValueError, TypeError, RecursionError, UnicodeError) as exc:
            reason = str(exc) if isinstance(exc, FeedProtocolError) else "malformed_frame"
            self.invalidate(reason)
            raise FeedProtocolError(reason) from exc

    def _apply(self, event, books, ticks, timestamps, now):
        if not isinstance(event, dict):
            raise FeedProtocolError("invalid_event")
        kind = event.get("event_type")
        if kind not in {"book", "price_change", "tick_size_change", "last_trade_price"}:
            raise FeedProtocolError("unsupported_event")
        timestamp = self._timestamp(event)
        if kind == "price_change":
            changes = event.get("price_changes")
            if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES_PER_EVENT:
                raise FeedProtocolError("invalid_price_changes")
            updates = {}
            advertised = {}
            seen = set()
            for change in changes:
                if not isinstance(change, dict):
                    raise FeedProtocolError("invalid_price_change")
                token = _token(change.get("asset_id"))
                side = change.get("side")
                if side not in {"BUY", "SELL"}:
                    raise FeedProtocolError("invalid_side")
                price, size = _number(change.get("price"), price=True), _number(change.get("size"))
                hint = {key: _number(change[key], price=True) for key in ("best_bid", "best_ask")
                        if change.get(key) is not None}
                if token not in self._tokens:
                    continue
                if token not in books:
                    raise FeedProtocolError("delta_before_snapshot")
                if timestamp < timestamps.get(token, 0):
                    raise FeedProtocolError("out_of_order_event")
                key = (token, side, price)
                if key in seen:
                    raise FeedProtocolError("duplicate_delta_level")
                seen.add(key)
                if token not in updates:
                    updates[token] = (dict(books[token].bids), dict(books[token].asks))
                levels = updates[token][0 if side == "BUY" else 1]
                if size == 0:
                    levels.pop(price, None)
                else:
                    levels[price] = size
                advertised.setdefault(token, {}).update(hint)
            for token, (bids, asks) in updates.items():
                previous = books[token]
                book = replace(previous, bids=tuple(sorted(bids.items(), reverse=True)),
                               asks=tuple(sorted(asks.items())), received_at=now,
                               exchange_timestamp_ms=timestamp)
                self._check_book(book)
                for key, value in advertised[token].items():
                    actual = getattr(book, key)
                    # Wire uses 0/1 as empty-side sentinels.
                    expected = actual if actual is not None else Decimal(0 if key == "best_bid" else 1)
                    if value != expected:
                        raise FeedProtocolError("best_price_mismatch")
                if (book.bids == previous.bids and book.asks == previous.asks and
                        timestamp == previous.exchange_timestamp_ms):
                    book = previous  # A duplicate cannot keep an old quote fresh.
                books[token], timestamps[token] = book, timestamp
            return

        token = _token(event.get("asset_id"))
        if kind == "last_trade_price":
            _number(event.get("price"), price=True)
            if event.get("size") is not None:
                _number(event["size"], positive=True)
            if event.get("side") not in {"BUY", "SELL"}:
                raise FeedProtocolError("invalid_side")
            return  # Trades alone say nothing about current resting liquidity.
        if token not in self._tokens:
            return
        if timestamp < timestamps.get(token, 0):
            raise FeedProtocolError("out_of_order_event")
        previous = books.get(token)
        if kind == "tick_size_change":
            new_tick = _tick(event.get("new_tick_size"))
            old_tick = _tick(event["old_tick_size"]) if event.get("old_tick_size") is not None else None
            if old_tick is not None and token in ticks and old_tick != ticks[token]:
                raise FeedProtocolError("tick_size_mismatch")
            ticks[token], timestamps[token] = new_tick, timestamp
            if previous is not None:
                # Metadata does not refresh liquidity or its exchange timestamp.
                books[token] = replace(previous, tick_size=new_tick)
            return

        tick = _tick(event["tick_size"]) if event.get("tick_size") is not None else ticks.get(token)
        book = BookSnapshot(token, self._levels(event.get("bids"), bids=True),
                            self._levels(event.get("asks"), bids=False), tick,
                            now, timestamp, self._generation)
        self._check_book(book)
        if (previous is not None and timestamp == previous.exchange_timestamp_ms and
                book.bids == previous.bids and book.asks == previous.asks):
            book = replace(book, received_at=previous.received_at)
        books[token], timestamps[token] = book, timestamp
        if tick is not None:
            ticks[token] = tick

    async def _session(self, socket):
        await asyncio.wait_for(socket.send(json.dumps({"assets_ids": self.token_ids, "type": "market"})), 10)
        self._connected = True
        self._generation += 1
        self.last_error = None
        self._rate.clear()
        self._notify()
        next_ping = self._clock() + HEARTBEAT_SECONDS
        last_pong = self._clock()
        while not self._closed:
            now = self._clock()
            if now - last_pong > HEARTBEAT_TIMEOUT:
                raise FeedProtocolError("heartbeat_timeout")
            if now >= next_ping:
                await asyncio.wait_for(socket.send("PING"), 5)
                next_ping = now + HEARTBEAT_SECONDS
            timeout = max(0.01, min(next_ping - self._clock(), HEARTBEAT_TIMEOUT - (self._clock() - last_pong)))
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout)
            except TimeoutError:
                continue
            if raw in ("PONG", b"PONG"):
                last_pong = self._clock()
            response = self.process_message(raw)
            if response is not None:
                await asyncio.wait_for(socket.send(response), 5)

    async def run(self) -> None:
        """Run until close/cancellation, reconnecting with 0.5..30s backoff."""
        if self._runner is not None or self._closed:
            raise RuntimeError("This feed is already running or closed")
        self._runner = asyncio.current_task()
        backoff = 1.0
        try:
            while not self._closed:
                self.invalidate("connecting")
                started = self._clock()
                try:
                    async with self._connector(
                        MARKET_URL, open_timeout=10, close_timeout=3,
                        max_size=MAX_FRAME_BYTES, max_queue=8,
                        compression=None, ping_interval=None, proxy=None,
                    ) as socket:
                        try:
                            await self._session(socket)
                        finally:
                            # Invalidate before waiting for the close handshake.
                            self.invalidate("disconnected")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.invalidate(str(exc) if isinstance(exc, FeedProtocolError) else "connection_failed")
                if self._closed:
                    break
                if self._clock() - started >= 60:
                    backoff = 1.0
                jitter = self._jitter()
                if not math.isfinite(jitter) or not 0 <= jitter <= 1:
                    raise ValueError("jitter must return a number between 0 and 1")
                await self._sleep(min(30.0, backoff * (0.5 + jitter * 0.5)))
                backoff = min(60.0, backoff * 2)
        finally:
            self.invalidate("closed" if self._closed else "stopped")
            self._runner = None

    async def close(self) -> None:
        """Stop this instance permanently and wake every waiting consumer."""
        self._closed = True
        self.invalidate("closed")
        task = self._runner
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            # Only the child's expected cancellation is consumed; cancellation
            # of this close() caller still propagates through gather().
            await asyncio.gather(task, return_exceptions=True)
