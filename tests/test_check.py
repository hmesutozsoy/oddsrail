"""check_order: the deterministic answer to 'the agent hallucinated'."""

import datetime as dt

import pytest

from oddsrail import check, guard, polymarket as pm

NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone.utc)
MARKET = {"title": "Will the price of Bitcoin be above $72,000 on September 2?",
          "description": "Resolves YES if the Binance BTC/USDT price is above 72,000 at 12:00 ET.",
          "outcome_of_market_id": "yes", "accepting_orders": True,
          "end_date": "2026-09-30T12:00:00Z", "resolution_source": "Binance", "uma_status": None}
BOOK = {"best_bid": "0.60", "best_ask": "0.62",
        "walk": {"fillable": True, "filled_size": 10.0, "avg_price": 0.62, "slippage_vs_best": 0.0}}


def order(**kw):
    o = {"venue": "polymarket", "market_id": "tok-yes", "side": "BUY", "price": 0.62, "size": 10, "outcome": None}
    o.update(kw)
    return o


def statuses(res):
    return {c["check"]: c["status"] for c in res["checks"]}


# --------------------------------------------------------------------------- #
# intent helpers                                                              #
# --------------------------------------------------------------------------- #

def test_intent_overlap_ignores_stopwords_and_finds_content_words():
    assert check.intent_overlap("buy yes on bitcoin above 72,000", MARKET["title"]) == 1.0
    assert check.intent_overlap("buy yes on ethereum staking", MARKET["title"]) == 0.0
    assert check.intent_overlap("", MARKET["title"]) == 1.0          # nothing to check


@pytest.mark.parametrize("text,want", [
    ("buy NO on the fed cut", "no"), ("fade the favourite", "no"),
    ("buy yes bitcoin", "yes"), ("bitcoin above 72k", None), ("yes or no, either", None),
])
def test_intent_outcome(text, want):
    assert check.intent_outcome(text) == want


# --------------------------------------------------------------------------- #
# evaluate                                                                    #
# --------------------------------------------------------------------------- #

def test_clean_order_is_ok_with_read_back():
    res = check.evaluate(order(), MARKET, BOOK, "buy 10 yes on bitcoin above 72,000", None, NOW)
    assert res["verdict"] == "ok"
    assert res["read_back"].startswith('BUY 10 YES on "Will the price of Bitcoin')
    assert statuses(res)["intent_matches_outcome"] == "ok"


def test_wrong_market_blocks():
    res = check.evaluate(order(), MARKET, BOOK, "buy yes on the fed cutting rates in march", None, NOW)
    assert res["verdict"] == "block"
    assert statuses(res)["intent_matches_market"] == "block"


def test_said_no_but_order_is_on_yes_blocks():
    res = check.evaluate(order(), MARKET, BOOK, "buy NO on bitcoin above 72,000", None, NOW)
    assert statuses(res)["intent_matches_outcome"] == "block"
    assert res["verdict"] == "block"


def test_no_intent_is_a_caution_not_a_block():
    res = check.evaluate(order(), MARKET, BOOK, "", None, NOW)
    assert statuses(res)["intent_matches_market"] == "caution" and res["verdict"] == "caution"


def test_closed_or_expired_market_blocks():
    res = check.evaluate(order(), {**MARKET, "accepting_orders": False}, BOOK, "bitcoin 72,000", None, NOW)
    assert statuses(res)["market_open"] == "block"
    res = check.evaluate(order(), {**MARKET, "end_date": "2026-09-01T00:00:00Z"}, BOOK, "bitcoin 72,000", None, NOW)
    assert statuses(res)["market_open"] == "block"


def test_missing_market_blocks_immediately():
    res = check.evaluate(order(), None, None, "anything", None, NOW)
    assert res["verdict"] == "block" and res["checks"][0]["check"] == "market_exists"


def test_price_through_the_book_and_high_price_are_cautions():
    res = check.evaluate(order(price=0.70), MARKET, BOOK, "bitcoin 72,000", None, NOW)
    assert statuses(res)["price_sane"] == "caution"
    res = check.evaluate(order(price=0.98), MARKET, {"best_bid": "0.97", "best_ask": "0.98"}, "bitcoin 72,000", None, NOW)
    assert statuses(res)["price_sane"] == "caution"
    res = check.evaluate(order(price=1.2), MARKET, BOOK, "bitcoin 72,000", None, NOW)
    assert statuses(res)["price_sane"] == "block"


def test_under_one_dollar_marketable_order_blocks():
    res = check.evaluate(order(size=1), MARKET, BOOK, "bitcoin 72,000", None, NOW)   # 0.62 notional, marketable
    assert statuses(res)["size_sane"] == "block"
    res = check.evaluate(order(size=1, price=0.50), MARKET, BOOK, "bitcoin 72,000", None, NOW)  # resting, allowed
    assert statuses(res)["size_sane"] == "ok"


def test_guardrail_refusal_blocks():
    ref = {"rule": "max_order_notional", "limit": 5.0, "requested": 6.2}
    res = check.evaluate(order(), MARKET, BOOK, "bitcoin 72,000", ref, NOW)
    assert statuses(res)["guardrails"] == "block" and res["verdict"] == "block"


def test_thin_liquidity_and_missing_resolution_are_cautions():
    thin = {**BOOK, "walk": {"fillable": False, "filled_size": 4.0, "avg_price": 0.62, "slippage_vs_best": 0.0}}
    res = check.evaluate(order(), MARKET, thin, "bitcoin 72,000", None, NOW)
    assert statuses(res)["liquidity"] == "caution"
    res = check.evaluate(order(), {**MARKET, "resolution_source": "(none named)"}, BOOK, "bitcoin 72,000", None, NOW)
    assert statuses(res)["resolution"] == "caution"


# --------------------------------------------------------------------------- #
# orchestration (offline: market and book faked)                             #
# --------------------------------------------------------------------------- #

async def test_check_order_end_to_end_polymarket(monkeypatch):
    async def fake_market(token_id, full=False):
        return {"question": MARKET["title"], "slug": "btc-72k", "id": "1",
                "outcomes": {"yes": {"token_id": "tok-yes"}, "no": {"token_id": "tok-no"}},
                "accepting_orders": True, "end_date": MARKET["end_date"],
                "resolution_source": "Binance", "uma_resolution_status": None, "neg_risk": False}
    async def fake_rc(id_or_slug):
        return {"description": MARKET["description"], "resolution_source": "Binance"}
    async def fake_book(token_id):
        return {"best_bid": "0.60", "best_ask": "0.62",
                "bids": [{"price": "0.60", "size": "50"}], "asks": [{"price": "0.62", "size": "50"}]}
    monkeypatch.setattr(pm, "get_market_by_token", fake_market)
    monkeypatch.setattr(pm, "resolution_criteria", fake_rc)
    monkeypatch.setattr(pm, "get_orderbook", fake_book)
    monkeypatch.delenv("ODDSRAIL_MAX_ORDER_NOTIONAL", raising=False)
    guard.reset_session()

    ok = await check.check_order("polymarket", "tok-yes", "buy", 0.62, 10, "buy 10 yes on bitcoin above 72,000")
    assert ok["verdict"] == "ok" and ok["dry_run"] is True

    wrong_side = await check.check_order("polymarket", "tok-no", "buy", 0.40, 10, "buy yes on bitcoin above 72,000")
    assert wrong_side["verdict"] == "block"
    assert statuses(wrong_side)["intent_matches_outcome"] == "block"

    monkeypatch.setenv("ODDSRAIL_MAX_ORDER_NOTIONAL", "5")
    capped = await check.check_order("polymarket", "tok-yes", "buy", 0.62, 10, "bitcoin 72,000")
    assert statuses(capped)["guardrails"] == "block"


async def test_check_order_rejects_bad_input_without_network():
    res = await check.check_order("polymarket", "tok", "SIDEWAYS", 0.5, 1)
    assert res["verdict"] == "block"
    res = await check.check_order("binance", "tok", "BUY", 0.5, 1)
    assert res["verdict"] == "block"
