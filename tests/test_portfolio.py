"""Public portfolio reads with an in-memory HTTP transport; no venue access."""

import asyncio
import json

import httpx
import pytest

from oddsrail.cloud import portfolio as p

OWNER = "0x" + "a1" * 20
ACCOUNT = "0x" + "b2" * 20
OTHER = "0x" + "c3" * 20
CONDITION = "0x" + "d4" * 32
TX = "0x" + "e5" * 32


def position(token="123", **extra):
    return {"proxy_wallet": ACCOUNT, "token_id": token, "condition_id": CONDITION,
            "status": "OPEN", "title": "A test market", "outcome": "Yes", "event_slug": "test-market",
            "current_size": 20, "avg_price": .4, "current_price": .5, "current_value": 10,
            "total_cost_usdc": 8, "total_pnl": 3, "unrealized_pnl": 2, "realized_pnl": 1,
            "percent_pnl": 37.5, "redeemable": False, **extra}


def activity(**extra):
    return {"proxy_wallet": ACCOUNT, "token_id": "123", "condition_id": CONDITION,
            "type": "TRADE", "side": "BUY", "title": "A test market", "outcome": "Yes",
            "size": 20, "usdc_size": 8, "price": .4, "timestamp": 1_780_000_000,
            "transaction_hash": TX, "event_slug": "test-market", **extra}


def page(rows, cursor=None):
    return {"data": rows, "pagination": {"has_more": cursor is not None, "next_cursor": cursor}}


def fixture_transport(overrides=None):
    calls = []
    defaults = {
        "/public-profile": {"proxyWallet": ACCOUNT},
        "/v2/positions": page([position()]),
        "/v2/activity": page([activity()]),
        "/v2/value": {"data": {"proxy_wallet": ACCOUNT, "value": 18}},
    }
    defaults.update(overrides or {})

    async def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == ("gamma-api.polymarket.com" if request.url.path == "/public-profile" else "data-api.polymarket.com")
        assert not any(k.lower().startswith(("authorization", "poly_", "x-api-key")) for k in request.headers)
        item = defaults[request.url.path]
        if callable(item):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    return httpx.MockTransport(handler), calls


async def test_resolves_profile_account_and_only_fetches_that_account(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "must-never-be-read")
    monkeypatch.setenv("POLYMARKET_API_KEY", "must-never-be-sent")
    transport, calls = fixture_transport()
    out = await p.load_portfolio(OWNER.upper().replace("0X", "0x"), transport=transport)
    assert out["address"] == OWNER
    assert out["account"]["connected_address"] == OWNER
    assert out["account"]["trading_address"] == ACCOUNT
    assert out["account"]["status"] == "resolved"
    assert out["read_only"] is True and out["authenticated"] is False
    assert calls[0].url.params["address"] == OWNER
    assert all(call.url.params["user"] == ACCOUNT for call in calls[1:])
    assert out["summary"]["holdings_value_usd"] == 18  # Aggregate includes combo cost; no sum of rows.
    assert out["summary"]["cash_usd"] is None
    assert out["summary"]["portfolio_value_usd"] is None
    assert "returns" not in out and "chart" not in out
    assert out["positions"]["items"][0]["token_id"] == "123"
    assert out["positions"]["items"][0]["total_pnl_usd"] == 3
    assert out["positions"]["items"][0]["unrealized_pnl_usd"] == 2
    assert out["history"]["items"][0]["timestamp"] == 1_780_000_000
    history_query = next(call.url.params for call in calls if call.url.path.endswith("activity"))
    assert history_query["exclude_deposits_withdrawals"] == "false"
    assert history_query["start"] == "1"
    assert history_query["sort_direction"] == "DESC"


@pytest.mark.parametrize("profile", [{}, {"proxyWallet": None}, {"proxyWallet": "0x" + "0" * 40}, {"proxyWallet": "https://localhost/private"},
                                       [], httpx.Response(404, json={"error": "not found"})])
async def test_missing_profile_mapping_does_not_query_owner_holdings(profile):
    transport, calls = fixture_transport({"/public-profile": profile})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert len(calls) == 1
    assert out["account"]["status"] == "unresolved"
    assert out["positions"]["status"] == "unresolved"
    assert out["history"]["status"] == "unresolved"
    assert out["summary"]["holdings_value_usd"] is None


@pytest.mark.parametrize("response", [httpx.Response(503, text="private internal message"),
                                       httpx.ReadTimeout("failed"), httpx.Response(200, text="not JSON")])
async def test_profile_service_error_does_not_claim_empty_account(response):
    transport, calls = fixture_transport({"/public-profile": response})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert len(calls) == 1
    assert out["account"]["status"] == "error"
    assert out["positions"]["status"] == "unavailable"
    assert "private internal message" not in json.dumps(out)


@pytest.mark.parametrize("address", [None, "", "0x" + "0" * 40, OWNER + "\n", OWNER + "?url=http://localhost", "https://example.com", "0x12", OWNER + OWNER])
async def test_invalid_address_causes_no_public_request(address):
    transport, calls = fixture_transport()
    with pytest.raises(p.InvalidAddress):
        await p.load_portfolio(address, transport=transport)
    assert calls == []


async def test_empty_account_is_only_zero_with_explicit_authoritative_value():
    transport, _ = fixture_transport({"/v2/positions": page([]), "/v2/activity": page([]),
                                      "/v2/value": {"data": {"proxy_wallet": ACCOUNT, "value": 0}}})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert out["positions"]["status"] == out["history"]["status"] == "ok"
    assert out["summary"]["holdings_value_usd"] == 0
    assert out["summary"]["cash_usd"] is None and out["summary"]["portfolio_value_usd"] is None


@pytest.mark.parametrize("value", [[], {}, {"data": []}, {"data": {"value": 0}},
                                   {"data": {"proxy_wallet": OTHER, "value": 0}},
                                   {"data": {"proxy_wallet": ACCOUNT, "value": "NaN"}},
                                   {"data": {"proxy_wallet": ACCOUNT, "value": -1}},
                                   {"data": {"proxy_wallet": ACCOUNT, "value": True}}])
async def test_invalid_aggregate_stays_unknown_without_row_sum_fallback(value):
    transport, _ = fixture_transport({"/v2/value": value})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert out["summary"]["holdings_value_usd"] is None
    assert out["summary"]["holdings_status"] == "unavailable"
    assert out["positions"]["status"] == "ok"


async def test_follows_cursor_and_keeps_filters_and_account_bound_to_every_page():
    def response(request):
        if request.url.params.get("cursor") == "opaque-page-two":
            return page([position("456")])
        return page([position()], "opaque-page-two")
    transport, calls = fixture_transport({"/v2/positions": response})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert [row["token_id"] for row in out["positions"]["items"]] == ["123", "456"]
    assert out["positions"]["status"] == "ok" and out["positions"]["truncated"] is False
    queries = [call.url.params for call in calls if call.url.path.endswith("positions")]
    assert len(queries) == 2
    assert all(q["user"] == ACCOUNT and q["include_archived"] == "true" and q["filter_amount"] == "0" for q in queries)
    assert all("start" not in q and "end" not in q for q in queries)  # Preserve positions without timestamps.


async def test_page_cap_exposes_truncation_without_affecting_aggregate():
    def response(request):
        second = "cursor" in request.url.params
        return page([position(str((100 if second else 0) + i)) for i in range(100)], "next-2" if second else "next-1")
    transport, calls = fixture_transport({"/v2/positions": response})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert len(out["positions"]["items"]) == 200
    assert out["positions"]["status"] == "partial" and out["positions"]["truncated"]
    assert len([c for c in calls if c.url.path.endswith("positions")]) == 2
    assert out["summary"]["holdings_value_usd"] == 18


@pytest.mark.parametrize("cursor", [None, "", "x" * 4097])
async def test_invalid_cursor_never_looks_like_complete_results(cursor):
    raw = page([position()])
    raw["pagination"] = {"has_more": True, "next_cursor": cursor}
    transport, calls = fixture_transport({"/v2/positions": raw})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert out["positions"]["status"] == "partial" and out["positions"]["truncated"]
    assert len([c for c in calls if c.url.path.endswith("positions")]) == 1


async def test_repeated_cursor_and_duplicate_position_do_not_loop_or_double_count():
    transport, calls = fixture_transport({"/v2/positions": page([position()], "same")})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert len(out["positions"]["items"]) == 1
    assert out["positions"]["truncated"]
    assert len([c for c in calls if c.url.path.endswith("positions")]) == 2


async def test_one_section_failure_keeps_other_sections_and_reports_unknown():
    transport, _ = fixture_transport({"/v2/positions": httpx.ReadTimeout("no response")})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert out["positions"]["status"] == "unavailable"
    assert out["history"]["status"] == "ok"
    assert out["summary"]["holdings_status"] == "ok"


async def test_wrong_account_rows_are_omitted_with_partial_status():
    transport, _ = fixture_transport({"/v2/positions": page([position(), position("456", proxy_wallet=OTHER)]),
                                      "/v2/activity": page([activity(proxy_wallet=OTHER)])})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert len(out["positions"]["items"]) == 1
    assert out["positions"]["status"] == "partial"
    assert out["history"]["items"] == [] and out["history"]["status"] == "partial"


async def test_unknown_optional_numbers_and_untrusted_urls_are_not_invented():
    transport, _ = fixture_transport({"/v2/positions": page([position(event_slug="javascript:alert(1)", avg_price="NaN",
                                                                             current_value=None, percent_pnl=True)])})
    out = await p.load_portfolio(OWNER, transport=transport)
    row = out["positions"]["items"][0]
    assert row["url"] is None and row["avg_price"] is None
    assert row["current_value_usd"] is None and row["percent_pnl"] is None
    json.dumps(out, allow_nan=False)


async def test_deposit_activity_can_have_no_market_or_token():
    deposit = activity(type="DEPOSIT", token_id=None, condition_id=None, title=None, event_slug=None,
                       side="", usdc_size=25, price=None)
    transport, _ = fixture_transport({"/v2/activity": page([deposit])})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert out["history"]["status"] == "ok"
    assert out["history"]["items"][0]["usdc_size"] == 25
    assert out["summary"]["cash_usd"] is None  # Deposits are not current cash.


async def test_open_book_includes_settled_unredeemed_winners():
    transport, calls = fixture_transport({"/v2/positions": page([position(status="REDEEMABLE", redeemable=True)])})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert out["positions"]["status"] == "ok"
    assert out["positions"]["items"][0]["redeemable"] is True
    assert next(call for call in calls if call.url.path.endswith("positions")).url.params["status"] == "OPEN"


async def test_redirect_does_not_fetch_an_arbitrary_host():
    transport, calls = fixture_transport({"/public-profile": httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert len(calls) == 1
    assert out["account"]["status"] == "error"


async def test_response_body_and_page_length_are_bounded(monkeypatch):
    monkeypatch.setattr(p, "MAX_BODY_BYTES", 200)
    transport, _ = fixture_transport({"/public-profile": {"proxyWallet": ACCOUNT, "bio": "x" * 201}})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert out["account"]["status"] == "error"
    monkeypatch.setattr(p, "MAX_BODY_BYTES", 1_000_000)
    transport, _ = fixture_transport({"/v2/positions": page([position(str(i)) for i in range(101)])})
    out = await p.load_portfolio(OWNER, transport=transport)
    assert out["positions"]["status"] == "unavailable" and out["positions"]["items"] == []


async def test_request_deadline_cancels_slow_public_transport(monkeypatch):
    monkeypatch.setattr(p, "REQUEST_SECONDS", .01)
    async def slow(_request):
        await asyncio.sleep(10)
    out = await p.load_portfolio(OWNER, transport=httpx.MockTransport(slow))
    assert out["account"]["status"] == "error"
