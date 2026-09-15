"""Observer tests use fixture metadata and books, never real network or wallets."""

import asyncio
from dataclasses import replace
from decimal import Decimal
import json

import httpx
import pytest

from oddsrail.live.market_feed import BookSnapshot
from oddsrail.live.quotes import QuotePolicy, QuoteUnavailable
from oddsrail.live import observer as ob


CID = "0x" + "a" * 64
D = Decimal


def payloads():
    return ([{"conditionId": CID, "clobTokenIds": '["123","456"]',
              "active": True, "closed": False, "archived": False,
              "acceptingOrders": True, "enableOrderBook": True, "negRisk": False,
              "orderMinSize": 5, "feesEnabled": True,
              "feeSchedule": {"rate": "0.02", "exponent": 2, "takerOnly": True}}],
            {"t": [{"t": "123", "o": "Yes"}, {"t": "456", "o": "No"}],
             "mts": "0.01", "mos": "5", "mbf": 0, "tbf": 200,
             "fd": {"r": "0.02", "e": 2, "to": True}})


def metadata():
    return ob.parse_market_metadata(CID, *payloads(), observed_at=100)


def policy():
    return QuotePolicy(CID, "123", "456", D(20), D("0.02"), D(10))


class Feed:
    def __init__(self):
        self.generation = 1
        self.token_ids = ("123", "456")
        self.books = {token: BookSnapshot(token, ((D("0.4"), D(20)),),
                                          ((D("0.6"), D(20)),), None, 100, 1, 1)
                      for token in self.token_ids}

    def fresh_snapshot(self, token, max_age, now):
        book = self.books.get(token)
        return book if book is not None and 0 <= now - book.received_at <= max_age else None


def test_metadata_binds_public_sources_and_preserves_exact_numbers():
    meta = metadata()
    assert meta.eligible and meta.condition_id == CID
    assert meta.yes_token_id == "123" and meta.no_token_id == "456"
    assert meta.tick_size == D("0.01") and meta.min_order_size == 5
    assert meta.min_notional_usd == 5 and meta.maker_fee_bps == 0


@pytest.mark.parametrize("gamma_change,clob_change", [
    (lambda g: g.clear(), lambda c: None),
    (lambda g: g.append(g[0]), lambda c: None),
    (lambda g: g[0].update(conditionId="0x" + "b" * 64), lambda c: None),
    (lambda g: g[0].update(clobTokenIds='["123","123"]'), lambda c: None),
    (lambda g: g[0].update(clobTokenIds='["123","456","789"]'), lambda c: None),
    (lambda g: g[0].update(clobTokenIds='[123,"456"]'), lambda c: None),
    (lambda g: g[0].update(active=1), lambda c: None),
    (lambda g: g[0].pop("closed"), lambda c: None),
    (lambda g: None, lambda c: c.update(t=[{"t": "456"}, {"t": "789"}])),
    (lambda g: None, lambda c: c.update(mts="0.005")),
    (lambda g: None, lambda c: c.update(mos="0")),
    (lambda g: None, lambda c: c.update(mbf=True)),
    (lambda g: None, lambda c: c.update(mbf=10001)),
    (lambda g: None, lambda c: c.update(tbf=10001)),
    (lambda g: None, lambda c: c.pop("fd")),
    (lambda g: None, lambda c: c["fd"].update(to=False)),
    (lambda g: None, lambda c: c["fd"].update(to=None)),
    (lambda g: None, lambda c: c["fd"].update(r="NaN")),
])
def test_metadata_rejects_ambiguous_missing_or_unsupported_values(gamma_change, clob_change):
    gamma, clob = payloads()
    gamma_change(gamma)
    clob_change(clob)
    with pytest.raises(ob.MetadataUnavailable):
        ob.parse_market_metadata(CID, gamma, clob, observed_at=100)


def test_explicit_zero_fee_curve_is_allowed_without_taker_only():
    gamma, clob = payloads()
    gamma[0]["feesEnabled"] = False
    clob["fd"] = {"r": 0, "e": 0, "to": False}
    clob["tbf"] = 0
    assert ob.parse_market_metadata(CID, gamma, clob, observed_at=100).eligible


def test_base_fee_is_not_effective_maker_charge_when_both_sources_are_taker_only():
    gamma, clob = payloads()
    clob["mbf"] = clob["tbf"] = 1000
    meta = ob.parse_market_metadata(CID, gamma, clob, observed_at=100)
    assert meta.maker_fee_free and meta.maker_fee_bps == 1000


def test_disagreement_about_fee_curve_pauses_planning():
    gamma, clob = payloads()
    gamma[0]["feeSchedule"]["takerOnly"] = False
    with pytest.raises(ob.MetadataUnavailable, match="fee_curve_mismatch"):
        ob.parse_market_metadata(CID, gamma, clob, observed_at=100)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff', b'{} trailing'])
def test_json_rejects_duplicate_keys_nonfinite_and_malformed(raw):
    with pytest.raises(ob.MetadataUnavailable):
        ob._json(raw)


async def test_public_metadata_transport_uses_only_fixed_gets_and_exact_condition():
    seen = []
    gamma, clob = payloads()

    def handler(request):
        seen.append(request)
        if request.url.host == "gamma-api.polymarket.com":
            assert dict(request.url.params) == {"condition_ids": CID, "limit": "2"}
            return httpx.Response(200, json=gamma)
        assert request.url.host == "clob.polymarket.com"
        assert request.url.path == "/clob-markets/" + CID
        return httpx.Response(200, json=clob)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ob.fetch_market_metadata(CID, client=client, clock=lambda: 100)
    assert result == metadata()
    assert len(seen) == 2 and all(request.method == "GET" for request in seen)
    assert all("authorization" not in request.headers and "poly_api_key" not in request.headers for request in seen)


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "https://other.example"}),
    httpx.Response(200, content=b" " * (ob.MAX_METADATA_BYTES + 1)),
    httpx.Response(503, json={"error": "private details should not propagate"}),
])
async def test_public_metadata_rejects_redirects_large_bodies_and_errors(response):
    seen = []

    def handler(request):
        seen.append(request)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ob.MetadataUnavailable):
            await ob.fetch_market_metadata(CID, client=client)
    assert all(request.url.host in {"gamma-api.polymarket.com", "clob.polymarket.com"} for request in seen)


async def test_network_failure_is_sanitized():
    def handler(request):
        raise httpx.ConnectError("internal server detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ob.MetadataUnavailable, match="^metadata_unavailable$"):
            await ob.fetch_market_metadata(CID, client=client)


@pytest.mark.parametrize("value", ["https://other.example", "123", CID.upper(), CID + "/../../", None])
async def test_market_input_cannot_choose_network_destination(value):
    with pytest.raises(ValueError):
        await ob.fetch_market_metadata(value)


def test_plans_use_current_metadata_and_each_outcome_book():
    feed = Feed()
    feed.books["456"] = replace(feed.books["456"], bids=((D("0.5"), D(20)),),
                                asks=((D("0.7"), D(20)),))
    pair = ob.plan_current(metadata(), policy(), feed, now=100)
    assert [quote.price for quote in pair] == [D("0.48"), D("0.58")]
    assert all(quote.post_only and quote.side == "BUY" for quote in pair)
    assert all(5 <= quote.notional <= 10 for quote in pair)


@pytest.mark.parametrize("now,code", [(99, "stale_metadata"), (131, "stale_metadata"),
                                      (106, "stale_or_offline"), (float("nan"), "stale_metadata")])
def test_no_plan_for_stale_data(now, code):
    with pytest.raises(QuoteUnavailable) as error:
        ob.plan_current(metadata(), policy(), Feed(), now=now)
    assert error.value.code == code


@pytest.mark.parametrize("change", [{"active": False}, {"accepting_orders": False},
                                    {"closed": True}, {"archived": True}, {"enable_order_book": False}])
def test_no_plan_for_closed_inactive_or_disabled_market(change):
    with pytest.raises(QuoteUnavailable, match="not accepting"):
        ob.plan_current(replace(metadata(), **change), policy(), Feed(), now=100)


def test_no_plan_for_identity_tick_empty_book_or_generation_changes():
    meta, pol, feed = metadata(), policy(), Feed()
    with pytest.raises(QuoteUnavailable) as error:
        ob.plan_current(replace(meta, no_token_id="789"), pol, feed, now=100)
    assert error.value.code == "identity_mismatch"
    feed.books["123"] = replace(feed.books["123"], tick_size=D("0.001"))
    with pytest.raises(QuoteUnavailable) as error:
        ob.plan_current(meta, pol, feed, now=100)
    assert error.value.code == "tick_changed"
    feed.books["123"] = replace(feed.books["123"], tick_size=None, asks=())
    with pytest.raises(QuoteUnavailable) as error:
        ob.plan_current(meta, pol, feed, now=100)
    assert error.value.code == "empty_book"
    feed = Feed()
    feed.generation = 2
    with pytest.raises(QuoteUnavailable) as error:
        ob.plan_current(meta, pol, feed, now=100)
    assert error.value.code == "generation_mismatch"


def test_enforces_conservative_notional_minimum_without_changing_user_cap():
    with pytest.raises(QuoteUnavailable) as error:
        ob.plan_current(metadata(), replace(policy(), per_order_usd=D(3)), Feed(), now=100)
    assert error.value.code == "below_minimum_notional"


def test_exchange_skew_cannot_be_hidden_by_identical_receive_times():
    feed = Feed()
    feed.books["123"] = replace(feed.books["123"], exchange_timestamp_ms=10_000)
    feed.books["456"] = replace(feed.books["456"], exchange_timestamp_ms=14_000)
    with pytest.raises(QuoteUnavailable) as error:
        ob.plan_current(metadata(), policy(), feed, now=100)
    assert error.value.code == "exchange_book_skew"


@pytest.mark.parametrize("seconds", [0, 301, True, 1.1])
async def test_run_duration_is_bounded_before_network(seconds):
    with pytest.raises(ValueError):
        await ob.observe_market(CID, seconds=seconds)


async def test_observer_outputs_changed_plans_throttles_and_pauses_without_trades(monkeypatch):
    now = [100.0]
    original_sleep = asyncio.sleep
    feed = Feed()
    feed.closed = False

    async def run():
        await asyncio.Event().wait()

    async def close():
        feed.closed = True

    feed.run, feed.close = run, close

    async def metadata_fetcher(selected, *, clock):
        assert selected == CID
        return metadata()

    async def sleep(_):
        now[0] = round(now[0] + 0.1, 2)
        if 100.4 <= now[0] < 101.1:
            for token in feed.token_ids:
                feed.books[token] = replace(feed.books[token], bids=((D("0.42"), D(20)),))
        if 101.3 <= now[0] < 101.6:
            feed.books = {}
        await original_sleep(0)

    monkeypatch.setattr(ob.asyncio, "sleep", sleep)
    rows = []
    result = await ob.observe_market(CID, seconds=2, per_order=D(10), metadata_fetcher=metadata_fetcher,
                                     feed_factory=lambda *args, **kwargs: feed,
                                     clock=lambda: now[0], emit=lambda row: rows.append((now[0], row)))
    plans = [(at, row) for at, row in rows if row["state"] == "quote_plan"]
    assert result == 0 and feed.closed
    assert len(plans) == 2 and plans[1][0] - plans[0][0] >= 1
    assert any(row["state"] == "paused" and row["reason"] == "stale_or_offline" for _, row in rows)
    assert rows[-1][1]["state"] == "stopped"
    assert rows[-1][1]["quote_plans"] == 2
    assert all(row["observation_only"] is True and row["submitted_orders"] == 0 for _, row in rows)
    assert not any("fills" in row or "pnl" in row for _, row in rows)


async def test_initial_metadata_failure_produces_explicit_paused_zero_order_result():
    async def unavailable(*args, **kwargs):
        raise ob.MetadataUnavailable("metadata_unavailable")
    rows = []
    result = await ob.observe_market(CID, metadata_fetcher=unavailable, emit=rows.append)
    assert result == 1
    assert [row["state"] for row in rows] == ["starting", "paused", "stopped"]
    assert rows[-1]["submitted_orders"] == 0


async def test_metadata_refresh_failure_discards_old_eligibility_and_retries(monkeypatch):
    now, calls, rows = [100.0], [], []
    feed = Feed()
    original_sleep = asyncio.sleep

    async def run():
        await asyncio.Event().wait()

    async def close():
        pass

    feed.run, feed.close = run, close

    async def fetch(selected, *, clock):
        calls.append(clock())
        if len(calls) == 2:
            raise ob.MetadataUnavailable("metadata_unavailable")
        return replace(metadata(), observed_at=clock(), closed=len(calls) > 2)

    async def sleep(_):
        now[0] += 1
        for token in feed.token_ids:
            feed.books[token] = replace(feed.books[token], received_at=now[0])
        await original_sleep(0)

    monkeypatch.setattr(ob.asyncio, "sleep", sleep)
    await ob.observe_market(CID, seconds=30, per_order=D(10), metadata_fetcher=fetch,
                            feed_factory=lambda *args, **kwargs: feed, clock=lambda: now[0],
                            emit=lambda row: rows.append((now[0], row)))
    assert calls == [100, 120, 125]
    assert any(row["state"] == "paused" and row["reason"] == "metadata_unavailable" for _, row in rows)
    assert any(row["state"] == "paused" and row["reason"] == "market_unavailable" for _, row in rows)
    assert not any(at >= 120 and row["state"] == "quote_plan" for at, row in rows)
