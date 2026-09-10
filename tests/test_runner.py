"""The deterministic runner: a builder config becomes one paper pass, offline.

Polymarket is faked at the polymarket.py boundary, so what runs is the real
signal code, the real check_order, and the real paper ledger."""

import json
import time

import pytest

from oddsrail import paper, polymarket as pm
from oddsrail.cloud import runner

YES, NO = "111", "222"
SLUG = "btc-above-80k-sep-30"
TITLE = "Will Bitcoin be above $80,000 on September 30?"
FAR = "2026-12-31T12:00:00Z"


def slim(yes_px=0.5, spread=0.02, vol=50000):
    return {"id": "1", "slug": SLUG, "question": TITLE, "condition_id": "0xc",
            "outcomes": {"yes": {"label": "Yes", "token_id": YES, "price": yes_px},
                         "no": {"label": "No", "token_id": NO, "price": round(1 - yes_px, 4)}},
            "best_bid": round(yes_px - spread / 2, 4), "best_ask": round(yes_px + spread / 2, 4),
            "spread": spread, "volume_24hr": vol, "liquidity": 1e5, "active": True, "closed": False,
            "accepting_orders": True, "neg_risk": False, "end_date": FAR, "resolution_source": "Binance"}


class Fake:
    """Books per token and a price series for YES."""

    def __init__(self, yes_book, no_book, series=None, market=None):
        self.books = {YES: yes_book, NO: no_book}
        self.series = series or ([], [])
        self.market = market or slim()
        self.calls = []

    def install(self, monkeypatch):
        async def search_markets(query="", limit=10):
            self.calls.append(("search", query)); return [self.market]

        async def closing_soon(hours=24.0, limit=15):
            return []

        async def top_markets(limit=60):
            self.calls.append(("top", limit)); return [self.market]

        async def markets_by_tag(tag_id, limit=40):
            self.calls.append(("tag", tag_id)); return [self.market]

        async def get_orderbook(token_id):
            b = self.books[token_id]
            return {"best_bid": str(b[0]), "best_ask": str(b[1]),
                    "bids": [{"price": str(b[0]), "size": "500"}], "asks": [{"price": str(b[1]), "size": "500"}]}

        async def price_history(token_id, hours=6.0, fidelity_minutes=1):
            return self.series

        async def get_market_by_token(token_id, full=False):
            m = dict(self.market); m["uma_resolution_status"] = None
            return m

        async def get_market(id_or_slug, full=False):
            return {**self.market, "resolution": {"source": "Binance", "uma_resolution_status": None},
                    "description": "Resolves YES if the Binance BTC/USDT price is above 80,000 at 12:00 ET."}

        async def resolution_criteria(id_or_slug):
            return {"description": "Resolves YES if the Binance BTC/USDT price is above 80,000.",
                    "resolution_source": "Binance"}

        for name, fn in (("search_markets", search_markets), ("closing_soon", closing_soon), ("top_markets", top_markets), ("markets_by_tag", markets_by_tag),
                         ("get_orderbook", get_orderbook), ("price_history", price_history),
                         ("get_market_by_token", get_market_by_token), ("get_market", get_market),
                         ("resolution_criteria", resolution_criteria)):
            monkeypatch.setattr(pm, name, fn)


def jump_series(pre=0.50, post=0.66, n=240, step=30.0):
    """Flat at `pre`, then a fresh jump to `post` 90 seconds before now, with a small retrace."""
    now = time.time()
    times = [now - (n - i) * step for i in range(n)]
    prices = [pre] * n
    prices[-4] = post            # the jump (>= 0.08 within 60s lookback)
    prices[-3] = post            # extreme
    prices[-2] = post - 0.02
    prices[-1] = post - 0.03
    return times, prices


def cfg(on, **kw):
    c = {"topic": "bitcoin", "minvol": 1000, "bankroll": 1000, "perorder": 25, "maxpos": 5, "closing": 2,
         "on": {k: True for k in on}, "vals": {}}
    c.update(kw)
    return c


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.delenv("ODDSRAIL_MAX_ORDER_NOTIONAL", raising=False)
    return tmp_path / "guest.json"


async def test_fade_buys_the_other_side_after_a_fresh_jump(ledger, monkeypatch):
    fake = Fake(yes_book=(0.62, 0.64), no_book=(0.36, 0.38), series=jump_series())
    fake.install(monkeypatch)
    out = await runner.run_pass(cfg(["fade", "report"]), ledger)
    assert out["ok"] and out["strategies"] == ["fade"]
    placed = [d for d in out["decisions"] if d["result"] == "filled"]
    assert len(placed) == 1, out["decisions"]
    d = placed[0]
    assert d["strategy"] == "fade" and d["outcome"] == "NO" and d["side"] == "BUY" and d["price"] == 0.38
    assert d["verdict"] == "ok" and d["notional"] <= 25.01
    assert "pre-jump price 0.50" in d["why"]
    led = json.loads(ledger.read_text())
    assert NO in led["positions"] and abs(led["cash"] - (1000 - d["notional"])) < 0.05
    assert out["ledger"]["fills"] == 1 and out["orders_placed"] == 1


async def test_no_strategy_is_a_read_only_pass(ledger, monkeypatch):
    fake = Fake(yes_book=(0.49, 0.51), no_book=(0.49, 0.51))
    fake.install(monkeypatch)
    out = await runner.run_pass(cfg(["report"]), ledger)
    assert out["ok"] and out["strategies"] == [] and out["decisions"] == []
    assert out["candidates"][0]["title"] == TITLE
    assert any("read-only" in s for s in out["steps"])


async def test_stop_loss_sells_at_the_bid_on_review(ledger, monkeypatch):
    fake = Fake(yes_book=(0.40, 0.42), no_book=(0.58, 0.60))
    fake.install(monkeypatch)
    token = runner.FORCED_LEDGER.set(ledger)
    try:
        d = paper.load(); d["cash"] = 994.0
        d["positions"][YES] = {"size": 10.0, "cost": 6.0, "title": TITLE}
        paper.save(d)
    finally:
        runner.FORCED_LEDGER.reset(token)
    out = await runner.run_pass(cfg(["stoploss"]), ledger)          # mark 0.41 vs entry 0.60: -32%
    sells = [x for x in out["decisions"] if x["strategy"] == "stoploss"]
    assert sells and sells[0]["side"] == "SELL" and sells[0]["result"] == "filled" and sells[0]["price"] == 0.40
    led = json.loads(ledger.read_text())
    assert YES not in led["positions"] and abs(led["realized_pnl"] - (0.40 - 0.60) * 10) < 1e-6


async def test_daily_loss_limit_halts_new_entries(ledger, monkeypatch):
    fake = Fake(yes_book=(0.62, 0.64), no_book=(0.36, 0.38), series=jump_series())
    fake.install(monkeypatch)
    token = runner.FORCED_LEDGER.set(ledger)
    try:
        d = paper.load(); d["cash"] = 900.0
        d["runner"] = {"day": time.strftime("%Y-%m-%d", time.gmtime()), "day_start_equity": 1000.0}
        paper.save(d)
    finally:
        runner.FORCED_LEDGER.reset(token)
    c = cfg(["fade", "daily"]); c["vals"] = {"daily": {"usd": 50}}
    out = await runner.run_pass(c, ledger)
    assert out["halted"] and "daily loss limit" in out["halted"]
    assert all(x["result"] == "skipped" for x in out["decisions"]) and out["orders_placed"] == 0


async def test_universe_filters_and_dispute_hygiene(ledger, monkeypatch):
    fake = Fake(yes_book=(0.62, 0.64), no_book=(0.36, 0.38), series=jump_series(), market=slim(vol=500))
    fake.install(monkeypatch)
    out = await runner.run_pass(cfg(["fade"]), ledger)               # volume below the minimum
    assert out["candidates"] == [] and out["orders_placed"] == 0

    fake = Fake(yes_book=(0.62, 0.64), no_book=(0.36, 0.38), series=jump_series())
    fake.install(monkeypatch)

    async def disputed(id_or_slug, full=False):
        return {**slim(), "resolution": {"source": "Binance", "uma_resolution_status": "disputed"},
                "description": "In the opinion of the consensus of credible reports"}
    monkeypatch.setattr(pm, "get_market", disputed)
    c = cfg(["fade", "dispute"]); c["vals"] = {"dispute": {"score": 30}}
    out = await runner.run_pass(c, ledger)
    skipped = [d for d in out["decisions"] if d["result"] == "skipped"]
    assert skipped and "dispute_risk" in skipped[0]["detail"] and out["orders_placed"] == 0


def test_normalize_bounds_the_config():
    c = runner.normalize({"perorder": "9999", "maxpos": "0", "bankroll": "abc", "on": {"fade": 1},
                          "vals": {"fade": {"jump": "8", "hours": "99"}, "settle": {"hi": "0.99"}}})
    assert c["perorder"] == 500 and c["maxpos"] == 1 and c["bankroll"] == 1000
    assert c["fade"] == {"jump": 0.08, "hours": 24.0} and c["settle"]["hi"] < 0.97 and c["on"]["fade"] is True


async def test_two_sided_quotes_rest_even_with_fill_checks_on(ledger, monkeypatch):
    fake = Fake(yes_book=(0.49, 0.51), no_book=(0.49, 0.51))
    fake.install(monkeypatch)
    c = cfg(["mm", "liquidity"]); c["vals"] = {"mm": {"edge": 2, "shares": 20}}
    out = await runner.run_pass(c, ledger)
    rest = [d for d in out["decisions"] if d["strategy"] == "mm"]
    assert [d["result"] for d in rest] == ["resting", "resting"], rest
    assert {d["outcome"] for d in rest} == {"YES", "NO"} and all(d["price"] == 0.48 for d in rest)
    assert out["ledger"]["open_orders"] and len(out["ledger"]["open_orders"]) == 2 and out["orders_placed"] == 2


async def test_resolution_caution_is_advisory_unless_the_dispute_switch_is_on(ledger, monkeypatch):
    fake = Fake(yes_book=(0.62, 0.64), no_book=(0.36, 0.38), series=jump_series(),
                market={**slim(), "resolution_source": None})
    fake.install(monkeypatch)

    async def no_source(id_or_slug):
        return {"description": "Resolves per the FOMC statement.", "resolution_source": "(none named)"}
    monkeypatch.setattr(pm, "resolution_criteria", no_source)

    out = await runner.run_pass(cfg(["fade"]), ledger)
    filled = [d for d in out["decisions"] if d["result"] == "filled"]
    assert filled and filled[0]["verdict"] == "caution" and any("resolution" in a for a in filled[0]["caution_accepted"])

    async def unsourced_full(id_or_slug, full=False):
        return {**slim(), "resolution": {"source": None, "uma_resolution_status": None}, "description": "Resolves per the FOMC statement."}
    monkeypatch.setattr(pm, "get_market", unsourced_full)
    out = await runner.run_pass(cfg(["fade", "dispute"]), ledger)
    assert all(d["result"] == "skipped" for d in out["decisions"]) and "no resolution source" in out["decisions"][0]["detail"]


async def test_empty_topic_scans_the_whole_venue_and_reports_the_universe(ledger, monkeypatch):
    fake = Fake(yes_book=(0.49, 0.51), no_book=(0.49, 0.51))
    fake.install(monkeypatch)
    out = await runner.run_pass(cfg(["report"], topic=""), ledger)
    assert ("top", 60) in fake.calls and not [c for c in fake.calls if c[0] == "search"]
    u = out["universe"]
    assert (u["query"], u["topics"], u["keyword"], u["scanned"], u["candidates"], u["dropped"]) == \
        ("all markets", ["all"], "", 1, 1, {})
    fake2 = Fake(yes_book=(0.49, 0.51), no_book=(0.49, 0.51), market=slim(vol=10))
    fake2.install(monkeypatch)
    out = await runner.run_pass(cfg(["report"], topic="all"), ledger)
    assert out["universe"]["candidates"] == 0 and "24h volume below $1,000" in out["universe"]["dropped"]


async def test_categories_union_tags_and_keyword_without_duplicates(ledger, monkeypatch):
    fake = Fake(yes_book=(0.49, 0.51), no_book=(0.49, 0.51))
    fake.install(monkeypatch)
    c = cfg(["report"]); c.pop("topic"); c["topics"] = ["crypto", "tennis", "bogus"]; c["keyword"] = "arsenal"
    out = await runner.run_pass(c, ledger)
    tags = sorted(x[1] for x in fake.calls if x[0] == "tag")
    assert tags == [21, 235, 864], tags                       # crypto (two tags) + tennis; 'bogus' ignored
    assert ("search", "arsenal") in fake.calls and not [x for x in fake.calls if x[0] == "top"]
    u = out["universe"]
    assert u["topics"] == ["crypto", "tennis"] and u["keyword"] == "arsenal"
    assert u["scanned"] == 1 and u["candidates"] == 1, "the same market from four sources counts once"
    assert out["candidates"][0]["title"] == TITLE


def test_normalize_topics_and_legacy_topic():
    c = runner.normalize({"topics": ["Politics", "nope"], "keyword": "x" * 200})
    assert c["topics"] == ["politics"] and len(c["keyword"]) == 80
    assert runner.normalize({"topic": "bitcoin"})["keyword"] == "bitcoin"
    assert runner.normalize({"topic": ""})["topics"] == ["all"]
    assert runner.normalize({"topics": "crypto, esports"})["topics"] == ["crypto", "esports"]
    assert runner.normalize({"topics": ["all", "crypto"]})["topic"] == "all markets"
    assert runner.normalize({"topics": ["crypto"], "keyword": "arsenal"})["topic"] == "crypto + arsenal"


# --------------------------------------------------------------------------- #
# fixes from the 2026-09-08 audit                                             #
# --------------------------------------------------------------------------- #

def seed(ledger, **fields):
    token = runner.FORCED_LEDGER.set(ledger)
    try:
        d = paper.load()
        for k, v in fields.items():
            d[k] = v
        paper.save(d)
    finally:
        runner.FORCED_LEDGER.reset(token)


async def test_daily_halt_still_lets_stop_loss_exit(ledger, monkeypatch):
    fake = Fake(yes_book=(0.62, 0.64), no_book=(0.36, 0.38), series=jump_series())
    fake.install(monkeypatch)
    # a NO position bought at 0.60 now marks 0.37: the stop loss must sell it even though the
    # day is already past the loss limit; the fade piece (which wants to buy NO) must be halted
    seed(ledger, cash=934.0, positions={NO: {"size": 10.0, "cost": 6.0, "title": TITLE}},
         runner={"day": time.strftime("%Y-%m-%d", time.gmtime()), "day_start_equity": 1000.0})
    c = cfg(["stoploss", "daily", "fade"]); c["vals"] = {"daily": {"usd": 50}}
    out = await runner.run_pass(c, ledger)
    assert out["halted"] and "daily loss limit" in out["halted"]
    by = {d["strategy"]: d for d in out["decisions"]}
    assert by["stoploss"]["side"] == "SELL" and by["stoploss"]["outcome"] == "NO" and by["stoploss"]["result"] == "filled"
    assert by["fade"]["result"] == "skipped" and "daily loss limit" in by["fade"]["detail"]
    assert NO not in json.loads(ledger.read_text())["positions"]


async def test_exposure_cap_counts_what_is_already_held(ledger, monkeypatch):
    fake = Fake(yes_book=(0.62, 0.64), no_book=(0.36, 0.38), series=jump_series())
    fake.install(monkeypatch)
    seed(ledger, cash=960.0, positions={YES: {"size": 50.0, "cost": 40.0, "title": TITLE}})
    c = cfg(["fade", "expo"]); c["vals"] = {"expo": {"usd": 50}}
    out = await runner.run_pass(c, ledger)
    d = [x for x in out["decisions"] if x["strategy"] == "fade"][0]
    assert d["result"] == "filled" and d["notional"] <= 10.01, d          # 50 cap minus 40 held


async def test_quotes_are_refreshed_not_stacked(ledger, monkeypatch):
    fake = Fake(yes_book=(0.49, 0.51), no_book=(0.49, 0.51))
    fake.install(monkeypatch)
    c = cfg(["mm"]); c["vals"] = {"mm": {"edge": 2, "shares": 20}}
    await runner.run_pass(c, ledger)
    await runner.run_pass(c, ledger)
    led = json.loads(ledger.read_text())
    assert len(led["open_orders"]) == 2, "two passes leave two quotes, not four"


async def test_slippage_rule_fires_and_otherwise_widens_the_limit(ledger, monkeypatch):
    fake = Fake(yes_book=(0.62, 0.64), no_book=(0.36, 0.38), series=jump_series())
    fake.install(monkeypatch)

    async def thin_then_deep(token_id):
        if token_id == NO:
            return {"best_bid": "0.36", "best_ask": "0.38", "bids": [{"price": "0.36", "size": "500"}],
                    "asks": [{"price": "0.38", "size": "5"}, {"price": "0.41", "size": "500"}]}
        return {"best_bid": "0.62", "best_ask": "0.64", "bids": [{"price": "0.62", "size": "500"}],
                "asks": [{"price": "0.64", "size": "500"}]}
    monkeypatch.setattr(pm, "get_orderbook", thin_then_deep)
    c = cfg(["fade", "liquidity"]); c["vals"] = {"liquidity": {"slip": 2}}
    out = await runner.run_pass(c, ledger)
    d = [x for x in out["decisions"] if x["strategy"] == "fade"][0]
    assert d["result"] == "skipped" and "slippage" in d["detail"], d

    c["vals"] = {"liquidity": {"slip": 20}}
    out = await runner.run_pass(c, ledger)
    d = [x for x in out["decisions"] if x["strategy"] == "fade"][0]
    assert d["result"] == "filled" and d["price"] > 0.38, "the limit was widened to cover the deeper level"


async def test_two_pieces_cannot_take_both_sides_of_one_market(ledger, monkeypatch):
    times, prices = jump_series()                        # up-jump: fade buys NO, momentum buys YES
    fake = Fake(yes_book=(0.62, 0.64), no_book=(0.36, 0.38), series=(times, prices))
    fake.install(monkeypatch)
    c = cfg(["fade", "momentum"]); c["vals"] = {"momentum": {"move": 5, "hours": 6}}
    out = await runner.run_pass(c, ledger)
    by = {d["strategy"]: d for d in out["decisions"]}
    assert by["fade"]["result"] == "filled" and by["fade"]["outcome"] == "NO"
    assert by["momentum"]["result"] == "skipped" and "already bought NO" in by["momentum"]["detail"]


async def test_review_sells_a_no_position_as_no(ledger, monkeypatch):
    fake = Fake(yes_book=(0.10, 0.12), no_book=(0.40, 0.42))
    fake.install(monkeypatch)
    seed(ledger, cash=994.0, positions={NO: {"size": 10.0, "cost": 6.0, "title": TITLE}})
    out = await runner.run_pass(cfg(["stoploss"]), ledger)
    d = [x for x in out["decisions"] if x["strategy"] == "stoploss"][0]
    assert d["outcome"] == "NO" and d["side"] == "SELL" and d["result"] == "filled"


def test_normalize_clamps_risk_numbers():
    c = runner.normalize({"vals": {"stoploss": {"pct": 0}, "takeprofit": {"pct": -5}, "daily": {"usd": 0},
                                   "liquidity": {"slip": 500}, "dispute": {"score": 999}, "mm": {"edge": 0}}})
    assert c["stoploss"] == 0.01 and c["takeprofit"] == 0.01 and c["daily"] == 1
    assert c["liquidity"] == 0.2 and c["dispute"] == 100 and c["mm"]["edge"] == 0.005


async def test_resolved_positions_settle_at_the_payout(ledger, monkeypatch):
    fake = Fake(yes_book=(0.49, 0.51), no_book=(0.49, 0.51))
    fake.install(monkeypatch)
    seed(ledger, cash=800.0, positions={YES: {"size": 100.0, "cost": 95.0, "title": TITLE},
                                        NO: {"size": 100.0, "cost": 5.0, "title": TITLE}})

    async def no_book(token_id):
        raise RuntimeError("404 No orderbook exists for the requested token id")

    async def resolved(token_id, full=False):
        m = slim(); m["closed"] = True; m["accepting_orders"] = False
        m["outcomes"]["yes"]["price"] = 1.0; m["outcomes"]["no"]["price"] = 0.0
        return m
    monkeypatch.setattr(pm, "get_orderbook", no_book)
    monkeypatch.setattr(pm, "get_market_by_token", resolved)
    token = runner.FORCED_LEDGER.set(ledger)
    try:
        pos = await paper.positions()
    finally:
        runner.FORCED_LEDGER.reset(token)
    assert {r["side"]: r["payout"] for r in pos["resolved"]} == {"YES": 1.0, "NO": 0.0}
    assert pos["positions"] == [] and abs(pos["cash"] - 900.0) < 1e-6      # 800 + 100 * 1 + 100 * 0
    assert abs(pos["realized_pnl"] - ((1.0 - 0.95) * 100 + (0.0 - 0.05) * 100)) < 1e-6
    assert pos["equity"] == 900.0


async def test_settle_buys_the_near_certain_side_inside_the_band(ledger, monkeypatch):
    fake = Fake(yes_book=(0.93, 0.95), no_book=(0.05, 0.07), market=slim(yes_px=0.94))
    fake.install(monkeypatch)

    async def soon(hours=24.0, limit=15):
        return [fake.market]
    monkeypatch.setattr(pm, "closing_soon", soon)
    out = await runner.run_pass(cfg(["settle"]), ledger)
    d = [x for x in out["decisions"] if x["strategy"] == "settle"]
    assert d and d[0]["outcome"] == "YES" and d[0]["result"] == "filled" and d[0]["price"] == 0.95
    assert "near-certain" in d[0]["why"] and "Binance" in d[0]["why"]

    # outside the band nothing is bought
    fake2 = Fake(yes_book=(0.70, 0.72), no_book=(0.28, 0.30), market=slim(yes_px=0.71))
    fake2.install(monkeypatch)
    monkeypatch.setattr(pm, "closing_soon", soon)
    out = await runner.run_pass(cfg(["settle"]), ledger)
    assert not [x for x in out["decisions"] if x["strategy"] == "settle"]


async def test_value_trades_only_where_the_market_disagrees_enough(ledger, monkeypatch):
    fake = Fake(yes_book=(0.40, 0.42), no_book=(0.58, 0.60))
    fake.install(monkeypatch)
    c = cfg(["value"])
    c["vals"] = {"value": {"edge": 5, "kelly": 0.25, "views": f"{TITLE}: 0.75"}}
    out = await runner.run_pass(c, ledger)
    d = [x for x in out["decisions"] if x["strategy"] == "value"]
    assert d and d[0]["outcome"] == "YES" and d[0]["result"] == "filled"
    assert "0.75" in d[0]["why"] and "0.42" in d[0]["why"]

    c["vals"]["value"]["views"] = f"{TITLE}: 0.44"          # only 2 points of edge
    out = await runner.run_pass(c, ledger)
    d = [x for x in out["decisions"] if x["strategy"] == "value"]
    assert d and d[0]["result"] == "skipped" and "edge below" in d[0]["detail"]


async def test_take_profit_sells_a_winner(ledger, monkeypatch):
    fake = Fake(yes_book=(0.90, 0.92), no_book=(0.08, 0.10))
    fake.install(monkeypatch)
    seed(ledger, cash=970.0, positions={YES: {"size": 50.0, "cost": 30.0, "title": TITLE}})  # entry 0.60, mark 0.91
    c = cfg(["takeprofit"]); c["vals"] = {"takeprofit": {"pct": 40}}
    out = await runner.run_pass(c, ledger)
    d = [x for x in out["decisions"] if x["strategy"] == "takeprofit"][0]
    assert d["side"] == "SELL" and d["result"] == "filled" and d["price"] == 0.90
    led = json.loads(ledger.read_text())
    assert YES not in led["positions"] and led["realized_pnl"] > 0


async def test_a_resting_order_takes_only_the_depth_that_is_there(ledger, monkeypatch):
    fake = Fake(yes_book=(0.49, 0.51), no_book=(0.49, 0.51))
    fake.install(monkeypatch)
    token = runner.FORCED_LEDGER.set(ledger)
    try:
        await paper.simulate_polymarket(YES, "BUY", 0.40, 100)      # rests, far below the ask

        async def thin(token_id):                                    # one share offered inside the limit
            return {"best_bid": "0.38", "best_ask": "0.39",
                    "bids": [{"price": "0.38", "size": "500"}], "asks": [{"price": "0.39", "size": "1"}]}
        monkeypatch.setattr(pm, "get_orderbook", thin)
        pos = await paper.positions()
        assert pos["positions"][0]["size"] == 1.0, "only the one share on offer filled"
        assert pos["open_orders"][0]["size"] == 99.0, "the rest keeps resting"
        assert abs(pos["cash"] - (1000 - 0.40)) < 1e-6, "the maker fills at its own price"
    finally:
        runner.FORCED_LEDGER.reset(token)


async def test_the_kill_switch_clears_resting_paper_orders(ledger, monkeypatch):
    fake = Fake(yes_book=(0.49, 0.51), no_book=(0.49, 0.51))
    fake.install(monkeypatch)
    monkeypatch.setenv("ODDSRAIL_PAPER_LEDGER", str(ledger))
    from oddsrail import trading
    await trading.place_order(YES, "BUY", 0.30, 10)                 # rests
    assert len(json.loads(ledger.read_text())["open_orders"]) == 1
    out = await trading.cancel_all_orders()
    assert out["dry_run"] is True and out["paper_cancelled"] == 1
    assert json.loads(ledger.read_text())["open_orders"] == []
