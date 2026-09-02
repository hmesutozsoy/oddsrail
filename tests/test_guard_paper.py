"""Guardrails and the paper ledger: the paths where an agent is fenced in,
and where dry-run money must add up.

Offline: the order book is faked, the ledger lives in a temp dir, and no
client is ever constructed."""

import json

import pytest

from oddsrail import geo, guard, paper, polymarket as pm, trading


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("ODDSRAIL_PAPER_LEDGER", str(tmp_path / "paper.json"))
    monkeypatch.setenv("ODDSRAIL_PAPER_BANKROLL", "100")
    monkeypatch.delenv("ODDSRAIL_DRY_RUN", raising=False)
    for v in ("ODDSRAIL_MAX_ORDER_NOTIONAL", "ODDSRAIL_MAX_SESSION_NOTIONAL",
              "ODDSRAIL_MAX_OPEN_ORDERS", "ODDSRAIL_ALLOWED_MARKETS", "ODDSRAIL_PAPER"):
        monkeypatch.delenv(v, raising=False)
    guard.reset_session()

    async def boom():
        raise AssertionError("network client must not be constructed here")
    monkeypatch.setattr(trading, "_client", boom)

    async def fake_book(token_id):
        # best-first, like the real get_orderbook: asks 0.60@50 then 0.62@100
        return {"bids": [{"price": "0.58", "size": "80"}, {"price": "0.55", "size": "200"}],
                "asks": [{"price": "0.60", "size": "50"}, {"price": "0.62", "size": "100"}],
                "best_bid": "0.58", "best_ask": "0.60"}
    monkeypatch.setattr(pm, "get_orderbook", fake_book)

    async def no_title(token_id):
        return {"question": "Test market?"}
    monkeypatch.setattr(pm, "get_market_by_token", no_title)


# --------------------------------------------------------------------------- #
# guardrails                                                                  #
# --------------------------------------------------------------------------- #

async def test_per_order_cap_refuses_in_dry_run_too(monkeypatch):
    monkeypatch.setenv("ODDSRAIL_MAX_ORDER_NOTIONAL", "10")
    out = await trading.place_order("tok", "BUY", 0.5, 100)   # $50 notional
    assert out["accepted"] is False and out["blocked_by"] == "guardrail"
    assert out["rule"] == "max_order_notional" and out["limit"] == 10.0
    assert "paper" not in out                                  # nothing papered either


async def test_allowed_markets_fence(monkeypatch):
    monkeypatch.setenv("ODDSRAIL_ALLOWED_MARKETS", "111, 222")
    bad = await trading.place_order("333", "BUY", 0.5, 1)
    assert bad["rule"] == "allowed_markets"
    ok = await trading.place_order("222", "BUY", 0.5, 1)
    assert ok["dry_run"] is True and "blocked_by" not in ok


def test_session_cap_counts_only_live_submissions(monkeypatch):
    monkeypatch.setenv("ODDSRAIL_MAX_SESSION_NOTIONAL", "100")
    assert guard.check_order("polymarket", "t", 60, dry=True) is None
    assert guard.check_order("polymarket", "t", 60, dry=False) is None
    guard.record_live_submission(60)
    assert guard.check_order("polymarket", "t", 60, dry=True) is None      # dry-run never consumes
    ref = guard.check_order("polymarket", "t", 60, dry=False)
    assert ref and ref["rule"] == "max_session_notional" and ref["requested"] == 120.0


def test_open_orders_cap_only_when_count_supplied(monkeypatch):
    monkeypatch.setenv("ODDSRAIL_MAX_OPEN_ORDERS", "3")
    assert guard.check_order("polymarket", "t", 1, dry=False) is None
    assert guard.check_order("polymarket", "t", 1, dry=False, open_orders_now=2) is None
    assert guard.check_order("polymarket", "t", 1, dry=False, open_orders_now=3)["rule"] == "max_open_orders"


def test_unset_or_garbage_limits_mean_unlimited(monkeypatch):
    monkeypatch.setenv("ODDSRAIL_MAX_ORDER_NOTIONAL", "banana")
    assert guard.check_order("polymarket", "t", 1e9, dry=True) is None
    assert guard.status()["max_order_notional_usd"] is None


# --------------------------------------------------------------------------- #
# paper ledger — the money must add up                                        #
# --------------------------------------------------------------------------- #

async def test_marketable_buy_fills_by_walking_the_book_within_limit():
    out = await trading.place_order("tok", "BUY", 0.61, 80)   # limit 0.61: only the 0.60 level
    p = out["paper"]
    assert p["filled_size"] == 50.0 and float(p["avg_price"]) == 0.60
    assert p["resting_size"] == 30.0 and p["paper_order_id"].startswith("paper-")
    assert p["cash_after"] == 70.0                               # 100 - 50*0.60


async def test_non_marketable_order_rests_and_fills_when_crossed(monkeypatch):
    out = await trading.place_order("tok", "BUY", 0.50, 10)    # below best ask: rests
    assert out["paper"]["filled_size"] == 0.0
    oid = out["paper"]["paper_order_id"]

    async def crossed(token_id):
        return {"bids": [], "asks": [{"price": "0.49", "size": "10"}],
                "best_bid": "0.48", "best_ask": "0.49"}
    monkeypatch.setattr(pm, "get_orderbook", crossed)
    pos = await paper.positions()
    assert oid in pos["just_filled_resting"]
    assert pos["positions"][0]["size"] == 10.0 and pos["cash"] == 95.0   # filled at the 0.50 limit


async def test_sell_realizes_pnl_and_no_shorting():
    await trading.place_order("tok", "BUY", 0.60, 50)          # cost 30, cash 70
    short = await trading.place_order("tok", "SELL", 0.58, 60)  # more than held
    assert "refused" in short["paper"] and "shorting" in short["paper"]["refused"]
    sell = await trading.place_order("tok", "SELL", 0.58, 50)   # hits the 0.58 bid
    assert sell["paper"]["filled_size"] == 50.0
    pos = await paper.positions()
    assert pos["positions"] == [] and pos["cash"] == 99.0        # 70 + 29
    assert pos["realized_pnl"] == pytest.approx(-1.0)            # (0.58-0.60)*50


async def test_cannot_buy_more_than_paper_cash():
    out = await trading.place_order("tok", "BUY", 0.62, 150)   # $92.5 > $100? no: 50*.6+100*.62=92 fits
    assert out["paper"]["filled_size"] == 150.0
    out2 = await trading.place_order("tok", "BUY", 0.62, 100)  # only $8 left
    assert "refused" in out2["paper"] and "insufficient paper cash" in out2["paper"]["refused"]


async def test_paper_cancel_and_reset():
    out = await trading.place_order("tok", "BUY", 0.40, 5)
    oid = out["paper"]["paper_order_id"]
    c = await trading.cancel_order(oid)
    assert c["dry_run"] is True and c["paper"] == "cancelled"
    assert (await paper.positions())["open_orders"] == []
    r = paper.reset()
    assert r["cash"] == 100.0


async def test_paper_disabled_returns_plain_intent(monkeypatch):
    monkeypatch.setenv("ODDSRAIL_PAPER", "0")
    out = await trading.place_order("tok", "BUY", 0.61, 1)
    assert out["dry_run"] is True and "paper" not in out


# --------------------------------------------------------------------------- #
# realtime event trimming + the local-TLS failure class                       #
# --------------------------------------------------------------------------- #

def test_event_summary_reads_best_levels_from_any_order():
    ev = {"type": "book", "payload": {"bids": [{"price": "0.40"}, {"price": "0.45"}],
                                      "asks": [{"price": "0.55"}, {"price": "0.50"}]}}
    s = pm._event_summary(ev, 0.0)
    assert s["best_bid"] == 0.45 and s["best_ask"] == 0.50 and s["type"] == "book"


def test_missing_ca_bundle_is_local_not_a_filter():
    e = Exception("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                  "unable to get local issuer certificate")
    assert geo.classify(e) == "local_tls"
    assert "certifi" in geo.HINTS["local_tls"]
