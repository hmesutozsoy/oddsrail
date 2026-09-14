"""Offline protocol and transport lifecycle checks for the shared public feed."""

import asyncio
from dataclasses import FrozenInstanceError
from decimal import Decimal
import json
import ssl

import pytest

from oddsrail.live import market_feed as mf


TOKEN = "123"
OTHER = "456"
UNSELECTED = "789"
WALL = 1800000000.0
STAMP = int(WALL * 1000)


def test_default_connector_requires_certificate_and_hostname_verification(monkeypatch):
    import websockets.asyncio.client

    captured = {}
    sentinel = object()

    def connect(url, **kwargs):
        captured.update(url=url, **kwargs)
        return sentinel

    monkeypatch.setattr(websockets.asyncio.client, "connect", connect)
    insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    assert mf._connect(mf.MARKET_URL, ssl=insecure) is sentinel
    context = captured["ssl"]
    assert captured["url"] == mf.MARKET_URL
    assert context is not insecure and context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True and context.cert_store_stats()["x509_ca"] > 0


def test_missing_ca_bundle_fails_closed_without_connecting(monkeypatch, tmp_path):
    import certifi
    import websockets.asyncio.client

    calls = []
    monkeypatch.setattr(certifi, "where", lambda: str(tmp_path / "missing-certificates.pem"))
    monkeypatch.setattr(websockets.asyncio.client, "connect", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(FileNotFoundError):
        mf._connect(mf.MARKET_URL)
    assert calls == []


class Clock:
    def __init__(self):
        self.now = 100.0
        self.wall = WALL

    def advance(self, seconds):
        self.now += seconds
        self.wall += seconds


def book(token=TOKEN, timestamp=STAMP, bids=None, asks=None, **extra):
    return {"event_type": "book", "asset_id": token, "timestamp": str(timestamp),
            "bids": bids if bids is not None else [{"price": "0.40", "size": "20"}],
            "asks": asks if asks is not None else [{"price": "0.60", "size": "30"}], **extra}


def change(token=TOKEN, price="0.42", size="21", side="BUY", **extra):
    return {"asset_id": token, "price": price, "size": size, "side": side, **extra}


def delta(*changes, timestamp=STAMP + 1):
    return {"event_type": "price_change", "price_changes": list(changes), "timestamp": str(timestamp)}


def tick(token=TOKEN, timestamp=STAMP + 2, **extra):
    return {"event_type": "tick_size_change", "asset_id": token,
            "timestamp": str(timestamp), "new_tick_size": "0.001", **extra}


def process(feed, event):
    return feed.process_message(json.dumps(event))


@pytest.fixture
def fixture():
    clock = Clock()
    feed = mf.SharedMarketFeed([TOKEN, OTHER], clock=lambda: clock.now, wall_clock=lambda: clock.wall)
    # Unit-test the parser independently of transport; integration tests below
    # establish these lifecycle values exclusively through run().
    feed._connected = True
    feed._generation = 1
    return feed, clock


@pytest.mark.parametrize("tokens", [[], [TOKEN, TOKEN], TOKEN, ["0"], ["01"], [123],
                                     ["a"], [str(2**256)], [True], [str(i) for i in range(1, 42)]])
def test_subscription_is_fixed_validated_and_bounded(tokens):
    with pytest.raises(ValueError):
        mf.SharedMarketFeed(tokens)


def test_constructor_bounds_infinite_iterables():
    def endless():
        number = 1
        while True:
            yield str(number)
            number += 1
    with pytest.raises(ValueError):
        mf.SharedMarketFeed(endless())


def test_book_is_sorted_immutable_and_uses_decimal(fixture):
    feed, _ = fixture
    process(feed, book(bids=[{"price": "0.1", "size": "1"}, {"price": "0.4", "size": "2"}],
                       asks=[{"price": "0.9", "size": "3"}, {"price": "0.6", "size": "4"}], tick_size="0.01"))
    snapshot = feed.fresh_snapshot(TOKEN, 5)
    assert snapshot == mf.BookSnapshot(TOKEN, ((Decimal("0.4"), Decimal(2)), (Decimal("0.1"), Decimal(1))),
                                       ((Decimal("0.6"), Decimal(4)), (Decimal("0.9"), Decimal(3))),
                                       Decimal("0.01"), 100.0, STAMP, 1)
    assert snapshot.best_bid == Decimal("0.4") and snapshot.best_ask == Decimal("0.6")
    with pytest.raises(FrozenInstanceError):
        snapshot.received_at = 102
    assert feed.token_ids == (TOKEN, OTHER)


def test_empty_sides_are_not_invented_prices(fixture):
    feed, _ = fixture
    process(feed, book(bids=[], asks=[]))
    snapshot = feed.fresh_snapshot(TOKEN, 5)
    assert snapshot.best_bid is None and snapshot.best_ask is None and snapshot.tick_size is None


def test_delta_applies_absolute_size_and_zero_removal(fixture):
    feed, clock = fixture
    process(feed, book())
    original = feed.fresh_snapshot(TOKEN, 5)
    clock.advance(1)
    process(feed, delta(change(price="0.4", size="0"), change(price="0.42", size="7", best_bid="0.42", best_ask="0.6")))
    new = feed.fresh_snapshot(TOKEN, 5)
    assert new.bids == ((Decimal("0.42"), Decimal(7)),)
    assert new.received_at == 101.0 and original.bids == ((Decimal("0.4"), Decimal(20)),)
    process(feed, delta(change(size="2"), timestamp=STAMP + 2))
    assert feed.fresh_snapshot(TOKEN, 5).bids[0][1] == 2  # Absolute, not cumulative.


def test_multi_token_delta_is_atomic_and_accepts_same_millisecond(fixture):
    feed, _ = fixture
    process(feed, [book(), book(OTHER)])
    process(feed, delta(change(), change(OTHER, side="SELL", price="0.58")))
    process(feed, delta(change(size="17")))  # Same millisecond can contain distinct updates.
    assert feed.fresh_snapshot(TOKEN, 5).bids[0][1] == 17
    assert feed.fresh_snapshot(OTHER, 5).best_ask == Decimal("0.58")


def test_bad_tail_of_batch_invalidates_every_book(fixture):
    feed, _ = fixture
    process(feed, [book(), book(OTHER)])
    with pytest.raises(mf.FeedProtocolError):
        process(feed, [delta(change()), delta(change(OTHER, price="0.9"))])
    assert not feed.connected and feed.fresh_snapshot(TOKEN, 5) is None
    assert feed.fresh_snapshot(OTHER, 5) is None and not feed._books


@pytest.mark.parametrize("event", [delta(change()), delta(change(OTHER))])
def test_requires_full_snapshot_for_each_token(fixture, event):
    feed, _ = fixture
    with pytest.raises(mf.FeedProtocolError, match="delta_before_snapshot"):
        process(feed, event)
    assert not feed.connected


def test_unknown_tokens_are_never_retained(fixture):
    feed, _ = fixture
    process(feed, book())
    process(feed, book(UNSELECTED))
    process(feed, delta(change(UNSELECTED), change()))
    assert set(feed._books) == {TOKEN}
    assert feed.fresh_snapshot(UNSELECTED, 5) is None


@pytest.mark.parametrize("event", [book(timestamp=STAMP - 1), delta(change(), timestamp=STAMP - 1),
                                   tick(timestamp=STAMP - 1)])
def test_older_exchange_event_invalidates_books(fixture, event):
    feed, _ = fixture
    process(feed, [book(), book(OTHER)])
    with pytest.raises(mf.FeedProtocolError, match="out_of_order"):
        process(feed, event)
    assert not feed._books


def test_heartbeat_trade_and_tick_do_not_refresh_quote_freshness(fixture):
    feed, clock = fixture
    process(feed, book(tick_size="0.01"))
    clock.advance(4)
    revision = feed.revision
    assert feed.process_message("PING") == "PONG"
    assert feed.process_message(b"PONG") is None
    process(feed, {"event_type": "last_trade_price", "asset_id": TOKEN, "price": "0.5",
                   "size": "2", "side": "BUY", "timestamp": str(STAMP + 4000)})
    assert feed.revision == revision
    process(feed, tick(timestamp=STAMP + 4001, old_tick_size="0.01"))
    assert feed.fresh_snapshot(TOKEN, 5).tick_size == Decimal("0.001")
    assert feed.fresh_snapshot(TOKEN, 5).received_at == 100
    clock.advance(2)
    assert feed.fresh_snapshot(TOKEN, 5) is None


def test_duplicate_snapshot_and_delta_do_not_refresh_old_book(fixture):
    feed, clock = fixture
    process(feed, book())
    clock.advance(4)
    process(feed, book())
    process(feed, delta(change(price="0.4", size="20"), timestamp=STAMP))
    assert feed.fresh_snapshot(TOKEN, 5).received_at == 100
    clock.advance(2)
    assert feed.fresh_snapshot(TOKEN, 5) is None


def test_tick_before_snapshot_is_metadata_only_and_ordered(fixture):
    feed, _ = fixture
    process(feed, tick(timestamp=STAMP))
    assert feed.fresh_snapshot(TOKEN, 5) is None
    process(feed, book())
    assert feed.fresh_snapshot(TOKEN, 5).tick_size == Decimal("0.001")
    with pytest.raises(mf.FeedProtocolError, match="tick_size_mismatch"):
        process(feed, tick(old_tick_size="0.1"))


def test_staleness_and_disconnection_are_independent(fixture):
    feed, clock = fixture
    process(feed, book())
    assert feed.fresh_snapshot(TOKEN, 5, now=105) is not None
    assert feed.fresh_snapshot(TOKEN, 5, now=105.0001) is None
    assert feed.fresh_snapshot(TOKEN, 5, now=99) is None
    assert feed.fresh_snapshot(TOKEN, 5, now=float("nan")) is None
    feed.invalidate()
    assert feed.fresh_snapshot(TOKEN, 1000) is None


@pytest.mark.parametrize("max_age", [0, -1, True, float("nan"), float("inf"), "5"])
def test_invalid_max_age_is_rejected(fixture, max_age):
    with pytest.raises(ValueError):
        fixture[0].fresh_snapshot(TOKEN, max_age)


def test_recently_received_backlog_is_still_stale_for_fast_consumers(fixture):
    feed, clock = fixture
    process(feed, book(timestamp=STAMP - 10000))
    assert feed.fresh_snapshot(TOKEN, 2) is None
    assert feed.fresh_snapshot(TOKEN, 15).received_at == clock.now
    clock.wall = float("nan")
    assert feed.fresh_snapshot(TOKEN, 15) is None


@pytest.mark.parametrize("field,value", [
    ("bids", [{"price": "0.6", "size": "1"}]),  # Locked book.
    ("bids", [{"price": "0.7", "size": "1"}]),
    ("bids", [{"price": "NaN", "size": "1"}]),
    ("bids", [{"price": "Infinity", "size": "1"}]),
    ("bids", [{"price": "1.1", "size": "1"}]),
    ("bids", [{"price": "-0.1", "size": "1"}]),
    ("bids", [{"price": "0.4", "size": "0"}]),
    ("bids", [{"price": "0.4", "size": "-1"}]),
    ("bids", [{"price": 0.4, "size": "1"}]),
    ("bids", [{"price": "1e-1", "size": "1"}]),
    ("bids", [{"price": "0.4", "size": "9" * 50}]),
    ("bids", [{"price": "0.4", "size": "1"}, {"price": "0.40", "size": "2"}]),
    ("bids", [False]),
    ("asks", None),
    ("asset_id", "not-a-token"),
    ("timestamp", None),
    ("timestamp", STAMP),
    ("timestamp", str(STAMP - 31000)),
    ("timestamp", str(STAMP + 6000)),
    ("tick_size", "0.02"),
])
def test_malformed_or_delayed_snapshot_fails_closed(fixture, field, value):
    feed, _ = fixture
    process(feed, book(OTHER))
    event = book()
    event[field] = value
    with pytest.raises(mf.FeedProtocolError):
        process(feed, event)
    assert not feed.connected and not feed._books


@pytest.mark.parametrize("event", [
    delta(change(side="buy")), delta(change(size="-1")), delta(change(size="NaN")),
    delta(change(), change()), delta(change(best_bid="0.41")), delta(change(best_ask="0.59")),
    delta(change(price="0.7")), delta(), tick(new_tick_size="0.02"),
    {"event_type": "error", "message": "bad subscription"},
    {"event_type": "price_change", "price_changes": [None], "timestamp": str(STAMP)},
])
def test_invalid_delta_or_control_frame_invalidates_all_books(fixture, event):
    feed, _ = fixture
    process(feed, [book(), book(OTHER)])
    with pytest.raises(mf.FeedProtocolError):
        process(feed, event)
    assert not feed._books


@pytest.mark.parametrize("raw", ["{", "null", "[]", "[{}]", "true", '{"a": 1, "a": 2}',
                                 '{"a": NaN}', b"\xff", "[" * 1500 + "]" * 1500])
def test_invalid_json_and_nesting_are_bounded_and_fail_closed(fixture, raw):
    feed, _ = fixture
    process(feed, book())
    with pytest.raises(mf.FeedProtocolError):
        feed.process_message(raw)
    assert not feed._books


def test_frame_bytes_batches_levels_and_changes_are_bounded(fixture, monkeypatch):
    feed, _ = fixture
    monkeypatch.setattr(mf, "MAX_LEVELS", 1)
    with pytest.raises(mf.FeedProtocolError):
        process(feed, book(bids=[{"price": "0.2", "size": "1"}, {"price": "0.3", "size": "1"}]))
    monkeypatch.setattr(mf, "MAX_EVENTS_PER_FRAME", 1)
    with pytest.raises(mf.FeedProtocolError):
        process(feed, [book(), book(OTHER)])
    monkeypatch.setattr(mf, "MAX_CHANGES_PER_EVENT", 1)
    with pytest.raises(mf.FeedProtocolError):
        process(feed, delta(change(), change(OTHER)))
    monkeypatch.setattr(mf, "MAX_FRAME_BYTES", 10)
    for raw in (" " * 11, "é" * 6, b" " * 11):
        with pytest.raises(mf.FeedProtocolError, match="frame_too_large"):
            feed.process_message(raw)


def test_delta_cannot_grow_book_past_level_limit(fixture, monkeypatch):
    feed, _ = fixture
    monkeypatch.setattr(mf, "MAX_LEVELS", 1)
    process(feed, book())
    with pytest.raises(mf.FeedProtocolError, match="too_many_book_levels"):
        process(feed, delta(change()))


def test_rate_limit_counts_batches_and_heartbeats(fixture, monkeypatch):
    feed, clock = fixture
    monkeypatch.setattr(mf, "MAX_EVENTS_PER_SECOND", 3)
    process(feed, [book(), book(OTHER)])
    feed.process_message("PONG")
    with pytest.raises(mf.FeedProtocolError, match="event_rate"):
        feed.process_message("PONG")
    clock.advance(1)
    feed.process_message("PONG")
    assert len(feed._rate) == 1


async def test_all_waiters_wake_and_slow_consumers_do_not_miss_updates(fixture):
    feed, _ = fixture
    revision = feed.revision
    waiters = [asyncio.create_task(feed.wait_for_update(revision, 1)) for _ in range(3)]
    await asyncio.sleep(0)
    process(feed, book())
    assert await asyncio.gather(*waiters) == [feed.revision] * 3
    assert await feed.wait_for_update(revision, 0) == feed.revision
    assert await feed.wait_for_update(feed.revision, 0.001) == feed.revision
    waiter = asyncio.create_task(feed.wait_for_update(feed.revision))
    await asyncio.sleep(0)
    await feed.close()
    assert await waiter == feed.revision


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.exited = False

    async def send(self, value):
        self.sent.append(value)

    async def recv(self):
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def event(self, event):
        self.incoming.put_nowait(json.dumps(event))


class Connections:
    def __init__(self, *sockets):
        self.sockets = list(sockets)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        socket = self.sockets.pop(0)

        class Context:
            async def __aenter__(self):
                return socket

            async def __aexit__(self, *args):
                socket.exited = True
        return Context()


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def test_disconnect_clears_books_before_reconnect_and_resubscribes():
    clock = Clock()
    first, second = Socket(), Socket()
    connector = Connections(first, second)
    release_backoff = asyncio.Event()
    delays = []

    async def sleep(delay):
        delays.append(delay)
        await release_backoff.wait()

    feed = mf.SharedMarketFeed([TOKEN, OTHER], connector=connector, clock=lambda: clock.now,
                              wall_clock=lambda: clock.wall, sleep=sleep, jitter=lambda: 0)
    task = asyncio.create_task(feed.run())
    try:
        await until(lambda: feed.connected)
        first.event([book(), book(OTHER)])
        await until(lambda: feed.fresh_snapshot(TOKEN, 5) is not None)
        initial = feed.fresh_snapshot(TOKEN, 5)
        first.incoming.put_nowait(OSError("offline"))
        await until(lambda: bool(delays))
        assert not feed.connected and not feed._books and first.exited
        assert initial.generation == 1 and delays == [0.5]
        release_backoff.set()
        await until(lambda: feed.connected)
        assert feed.generation == 2 and feed.fresh_snapshot(TOKEN, 5) is None
        second.event(book())
        await until(lambda: feed.fresh_snapshot(TOKEN, 5) is not None)
        assert feed.fresh_snapshot(TOKEN, 5).generation == 2
        assert feed.fresh_snapshot(OTHER, 5) is None
        assert first.sent[0] == second.sent[0]
        assert json.loads(first.sent[0]) == {"assets_ids": [TOKEN, OTHER], "type": "market"}
        assert all(url == mf.MARKET_URL and opts["max_queue"] == 8 and opts["max_size"] == mf.MAX_FRAME_BYTES
                   and opts["proxy"] is None and opts["ping_interval"] is None for url, opts in connector.calls)
    finally:
        await feed.close()
    assert task.cancelled() and second.exited and not feed.connected


async def test_protocol_error_disconnects_and_a_delta_on_reconnect_is_rejected():
    first, second = Socket(), Socket()
    connector = Connections(first, second)
    release = asyncio.Event()
    attempts = []

    async def sleep(delay):
        attempts.append(delay)
        await release.wait()
        release.clear()

    feed = mf.SharedMarketFeed([TOKEN], connector=connector, wall_clock=lambda: WALL, sleep=sleep, jitter=lambda: 1)
    task = asyncio.create_task(feed.run())
    try:
        await until(lambda: feed.connected)
        first.event(book())
        await until(lambda: feed.fresh_snapshot(TOKEN, 5) is not None)
        first.event(delta(change(best_bid="0.1")))
        await until(lambda: attempts)
        assert feed.last_error == "best_price_mismatch" and not feed._books
        release.set()
        await until(lambda: feed.connected)
        second.event(delta(change()))
        await until(lambda: len(attempts) == 2)
        assert feed.last_error == "delta_before_snapshot" and attempts == [1, 2]
    finally:
        await feed.close()
    assert task.cancelled()


async def test_backoff_is_bounded_even_for_persistent_connect_failures():
    delays = []
    clock = Clock()

    def connector(*args, **kwargs):
        class Fails:
            async def __aenter__(self):
                raise OSError("offline")

            async def __aexit__(self, *args):
                pass
        return Fails()

    async def sleep(delay):
        delays.append(delay)
        clock.advance(delay)
        if len(delays) == 10:
            raise asyncio.CancelledError()

    feed = mf.SharedMarketFeed([TOKEN], connector=connector, clock=lambda: clock.now,
                              sleep=sleep, jitter=lambda: 1)
    with pytest.raises(asyncio.CancelledError):
        await feed.run()
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30, 30, 30]
    assert not feed.connected and feed._runner is None


async def test_market_application_heartbeat_and_timeout_do_not_keep_book_fresh(monkeypatch):
    monkeypatch.setattr(mf, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(mf, "HEARTBEAT_TIMEOUT", 0.04)
    socket = Socket()
    sleep_entered = asyncio.Event()

    async def sleep(delay):
        sleep_entered.set()
        await asyncio.Event().wait()

    feed = mf.SharedMarketFeed([TOKEN], connector=Connections(socket), wall_clock=lambda: WALL, sleep=sleep)
    task = asyncio.create_task(feed.run())
    try:
        await until(lambda: feed.connected)
        socket.event(book())
        await until(lambda: feed.fresh_snapshot(TOKEN, 5) is not None)
        stamp = feed.fresh_snapshot(TOKEN, 5).received_at
        socket.incoming.put_nowait("PING")
        await until(lambda: "PONG" in socket.sent)
        socket.incoming.put_nowait("PONG")
        await until(lambda: "PING" in socket.sent)
        assert feed.fresh_snapshot(TOKEN, 5).received_at == stamp
        await asyncio.wait_for(sleep_entered.wait(), 1)
        assert feed.last_error == "heartbeat_timeout" and not feed._books
    finally:
        await feed.close()
    assert task.cancelled()


async def test_external_cancellation_is_preserved_and_closes_transport():
    socket = Socket()
    feed = mf.SharedMarketFeed([TOKEN], connector=Connections(socket), wall_clock=lambda: WALL)
    task = asyncio.create_task(feed.run())
    await until(lambda: feed.connected)
    with pytest.raises(RuntimeError):
        await feed.run()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not feed.connected and socket.exited and feed._runner is None
    await feed.close()
    with pytest.raises(RuntimeError):
        await feed.run()
