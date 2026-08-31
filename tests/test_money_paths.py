"""Tests for the paths where a bug costs money.

Scope is deliberate: these cover the pure logic that decides *what order gets
sent* and *how big it is*. They run offline with no keys and no network, so
they can gate CI. Live-venue behaviour is verified separately by hand; see
README.
"""

import os
from decimal import Decimal

import pytest

from oddsrail import audit, crossvenue as xv, signals, trading
from oddsrail.kalshi import _to_yes_book, matches


# --------------------------------------------------------------------------- #
# Kalshi side translation — the inverted-position trap                         #
# --------------------------------------------------------------------------- #
# Kalshi V2 has no yes/no side and no buy/sell action: everything is quoted
# from the YES book as bid/ask. Getting this backwards silently takes the
# opposite position, which is why it is tested exhaustively rather than by
# example.

@pytest.mark.parametrize("outcome,action,price,want_side,want_price", [
    ("yes", "buy",  Decimal("0.60"), "bid", Decimal("0.60")),
    ("yes", "sell", Decimal("0.60"), "ask", Decimal("0.60")),
    ("no",  "buy",  Decimal("0.25"), "ask", Decimal("0.75")),
    ("no",  "sell", Decimal("0.25"), "bid", Decimal("0.75")),
    # boundaries
    ("no",  "buy",  Decimal("0.01"), "ask", Decimal("0.99")),
    ("no",  "sell", Decimal("0.99"), "bid", Decimal("0.01")),
])
def test_yes_book_translation(outcome, action, price, want_side, want_price):
    side, px = _to_yes_book(outcome, action, price)
    assert side == want_side
    assert px == want_price


def test_buying_no_is_not_the_same_as_buying_yes():
    """The specific inversion that would cost real money."""
    assert _to_yes_book("no", "buy", Decimal("0.25")) != \
           _to_yes_book("yes", "buy", Decimal("0.25"))


@pytest.mark.parametrize("outcome,action", [("maybe", "buy"), ("yes", "hold")])
def test_translation_rejects_nonsense(outcome, action):
    with pytest.raises(ValueError):
        _to_yes_book(outcome, action, Decimal("0.5"))


# --------------------------------------------------------------------------- #
# Position sizing                                                              #
# --------------------------------------------------------------------------- #

def test_kelly_math():
    # edge .15 at price .40 -> full kelly .15/.60 = .25, quarter-kelly = .0625
    r = audit.kelly_size(1000, 0.40, 0.55)
    assert r["recommended_stake_usd"] == pytest.approx(62.5)
    assert r["full_kelly_fraction"] == pytest.approx(0.25)


def test_kelly_refuses_negative_edge_rather_than_flipping_side():
    r = audit.kelly_size(1000, 0.40, 0.35)
    assert r["recommended_stake_usd"] == 0.0
    assert "no positive edge" in r["reason"]


@pytest.mark.parametrize("bank,price,fv", [
    (1000, 0, 0.5), (1000, 1, 0.5), (1000, 0.5, 0), (1000, 0.5, 1), (0, 0.5, 0.6),
])
def test_kelly_rejects_impossible_inputs(bank, price, fv):
    assert "error" in audit.kelly_size(bank, price, fv)


# --------------------------------------------------------------------------- #
# Book walking — the number that decides if an edge survives                   #
# --------------------------------------------------------------------------- #

BOOK = [{"price": "0.15", "size": "100"},
        {"price": "0.16", "size": "200"},
        {"price": "0.18", "size": "50"}]


def test_walk_book_averages_across_levels_not_just_the_top():
    r = xv.walk_book(BOOK, 250)
    # 100@.15 + 150@.16 = 39.00 over 250 -> .156, not the .15 top of book
    assert r["fillable"] is True
    assert r["avg_price"] == pytest.approx(0.156)
    assert r["notional"] == pytest.approx(39.0)
    assert r["slippage_vs_best"] == pytest.approx(0.006)


def test_walk_book_reports_partial_fills_honestly():
    r = xv.walk_book(BOOK, 1000)
    assert r["fillable"] is False
    assert r["filled_size"] == 350
    assert r["unfilled_size"] == 650


def test_walk_book_empty_side():
    assert xv.walk_book([], 10)["fillable"] is False


# --------------------------------------------------------------------------- #
# Cross-venue pairing must not manufacture arbitrage                           #
# --------------------------------------------------------------------------- #

def _m(venue, title, price, close, mid="x"):
    return {"venue": venue, "title": title, "yes_price": price,
            "market_id": mid, "close_time": close,
            "best_bid": None, "best_ask": None}


def test_unrelated_events_are_not_paired():
    """Real regression: a naive title overlap paired a Brazilian election with
    a Ukrainian one and reported a 70-point 'gap'."""
    pairs = xv.pair_across_venues([
        _m("polymarket", "Will Camilo Santana win the 2026 Brazilian presidential election", 0.02, "2026-10-04T00:00:00Z"),
        _m("kalshi", "Will Ukraine hold a presidential election before Jan 1 2027", 0.72, "2027-01-01T00:00:00Z"),
    ])
    assert pairs == []


def test_genuine_same_event_pair_survives():
    pairs = xv.pair_across_venues([
        _m("polymarket", "Will Trump resign before his term is up", 0.05, "2029-01-20T00:00:00Z"),
        _m("kalshi", "Will President Trump resign before his term is up", 0.07, "2029-01-20T00:00:00Z"),
    ])
    assert len(pairs) == 1
    assert pairs[0]["yes_price_difference"] == pytest.approx(0.02)


def test_similar_titles_far_apart_in_time_are_rejected():
    pairs = xv.pair_across_venues([
        _m("polymarket", "Will Trump resign before his term is up", 0.05, "2027-01-20T00:00:00Z"),
        _m("kalshi", "Will President Trump resign before his term is up", 0.07, "2029-01-20T00:00:00Z"),
    ])
    assert pairs == []


# --------------------------------------------------------------------------- #
# Signals must not invent data                                                 #
# --------------------------------------------------------------------------- #

def test_overshoot_does_not_fabricate_reversion_past_the_series_end():
    """A jump at the very end has no future data; reporting 0.0 ('did not
    retrace') would bias the freshest fade signal."""
    times = [float(t) for t in range(0, 900, 2)]
    prices = [0.50 if t < 880 else 0.62 for t in times]
    rep = signals.overshoot_report(times, prices, threshold=0.05, lookback=60)
    if rep.get("events"):
        rev = rep["events"][-1]["reversion_by_horizon_s"]
        assert all(v is None for v in rev.values())
    assert rep["median_reversion_120s"] is None


def test_overshoot_still_measures_mid_series():
    times = [float(t) for t in range(0, 2000, 2)]
    prices = [0.50 if t < 600 else 0.62 - 0.06 * min(1, (t - 600) / 300) for t in times]
    rep = signals.overshoot_report(times, prices, threshold=0.05, lookback=60)
    rev = rep["events"][-1]["reversion_by_horizon_s"]
    assert any(v is not None for v in rev.values())


def test_dispute_risk_word_boundaries():
    """'major' must not fire on 'majority'."""
    r = signals.dispute_risk({"question": "Will a majority form?",
                              "description": "a majority is needed"})
    assert not any("major" in reason and "ambiguous" in reason
                   for reason in r["reasons"])


def test_dispute_risk_reads_v2_sub_objects():
    """volume and neg_risk moved into metrics/state; the old top-level lookups
    silently never fired."""
    r = signals.dispute_risk({"question": "x", "description": "y",
                              "metrics": {"volume": 9_000_000},
                              "state": {"neg_risk": True}})
    assert any("open interest" in x for x in r["reasons"])
    assert any("neg-risk" in x for x in r["reasons"])


def test_kalshi_search_matching_is_word_boundary():
    assert matches("fed", "Fed funds rate at end of 2026")
    assert not matches("fed", "Will FDP win the German Bundestag confederation")
    assert matches("bitcoin price", "Bitcoin price above 100k")
    assert not matches("trump", "Trumpet concerto")


# --------------------------------------------------------------------------- #
# Safety defaults                                                              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("val,expect_dry", [
    # Only three values disable the safety net, and each one unambiguously
    # reads as "dry run: off". Everything else — unset, empty, a typo — stays
    # dry, so the failure mode of a mistake is "no trade", never "wrong trade".
    (None, True), ("", True), ("1", True), ("true", True), ("yes", True),
    ("off", True), ("nope", True), ("FALSE", True),   # case-sensitive by design
    ("0", False), ("false", False), ("no", False),
])
def test_only_explicit_values_disable_dry_run(monkeypatch, val, expect_dry):
    monkeypatch.delenv("ODDSRAIL_DRY_RUN", raising=False)
    if val is not None:
        monkeypatch.setenv("ODDSRAIL_DRY_RUN", val)
    assert trading.dry_run() is expect_dry


def test_operator_builder_code_overrides_project_default(monkeypatch):
    monkeypatch.setenv("ODDSRAIL_BUILDER_CODE", "0x" + "ab" * 32)
    assert trading.builder_code() == "0x" + "ab" * 32
    assert "operator" in trading.builder_code_source()


def test_project_default_applies_when_operator_sets_nothing(monkeypatch):
    monkeypatch.delenv("ODDSRAIL_BUILDER_CODE", raising=False)
    assert trading.builder_code() == trading.DEFAULT_BUILDER_CODE
    assert "project default" in trading.builder_code_source()


@pytest.mark.asyncio
async def test_place_order_dry_run_never_touches_the_network(monkeypatch):
    monkeypatch.setenv("ODDSRAIL_DRY_RUN", "1")
    r = await trading.place_order("tok", "BUY", 0.5, 10)
    assert r["dry_run"] is True
    assert "would_post" in r


@pytest.mark.asyncio
@pytest.mark.parametrize("side,price,size", [
    ("SIDEWAYS", 0.5, 10), ("BUY", 0.0, 10), ("BUY", 1.0, 10), ("BUY", 0.5, 0),
])
async def test_place_order_rejects_bad_input(side, price, size):
    with pytest.raises(ValueError):
        await trading.place_order("tok", side, price, size)


# --------------------------------------------------------------------------- #
# Settlement audit                                                             #
# --------------------------------------------------------------------------- #

def test_settlement_audit_blocks_legs_that_resolve_days_apart():
    r = audit.audit_pair(
        {"question": "q", "end_date": "2026-09-01T00:00:00Z",
         "resolution_source": "AP"},
        {"title": "t", "close_time": "2026-12-01T00:00:00Z",
         "settlement_sources": [{"name": "AP"}]})
    assert r["verdict"] == "block"


def test_settlement_audit_flags_an_open_uma_dispute():
    r = audit.audit_pair(
        {"question": "q", "end_date": "2026-09-01T00:00:00Z",
         "resolution_source": "AP", "uma_resolution_status": "disputed"},
        {"title": "t", "close_time": "2026-09-01T00:00:00Z",
         "settlement_sources": [{"name": "AP"}]})
    assert r["verdict"] == "block"
    assert any(f["check"] == "uma_status" for f in r["findings"])
