"""Explicit selection never widens its scope, including through strategy paths."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from oddsrail import market_selection as selection, paper, polymarket as pm
from oddsrail.cloud import runner

YES, NO, OTHER_YES, OTHER_NO = (str(10**50 + n) for n in range(1, 5))


def market(slug="bitcoin-above-80000", yes=YES, no=NO):
    return {"id": "42", "slug": slug, "question": "Will Bitcoin be above $80,000?",
            "condition_id": "0xcondition", "accepting_orders": True, "closed": False,
            "outcomes": {"yes": {"label": "Yes", "token_id": yes, "price": 0.5},
                         "no": {"label": "No", "token_id": no, "price": 0.5}},
            "volume_24hr": 50000, "spread": 0.02, "best_bid": 0.49, "best_ask": 0.51}


@pytest.mark.parametrize("url,kind,slug,child", [
    ("https://polymarket.com/event/election-2028", "event", "election-2028", None),
    ("https://www.polymarket.com/event/election-2028/candidate-a?utm_source=test#details", "event", "election-2028", "candidate-a"),
    ("polymarket.com/market/bitcoin-above-80000/", "market", "bitcoin-above-80000", None),
])
def test_known_routes(url, kind, slug, child):
    assert selection.parse_polymarket_url(url) == {"kind": kind, "slug": slug, "market_slug": child}


@pytest.mark.parametrize("url", [
    "https://evil.example/event/a", "https://polymarket.com.evil.example/event/a",
    "https://polymarket.com@evil.example/event/a", "https://user@polymarket.com/event/a",
    "https://user:pass@polymarket.com/event/a", "https://polymarket.com:443/event/a",
    "https://127.0.0.1/event/a", "https://polymarket.com./event/a",
    "http://polymarket.com/event/a", "file:///event/a", "//polymarket.com/event/a",
    "https://polymarket.com\\@evil.example/event/a", "https://polymarket.com\n/event/a",
    "https://polymarket.com/event/%2e%2e", "https://polymarket.com/event/a%2fb",
    "https://polymarket.com/event/a/../../x", "https://polymarket.com/api/markets",
    "https://polymarket.com/market/a/b", "https://polymarket.com/event/",
])
async def test_bad_urls_rejected_before_any_lookup(monkeypatch, url):
    async def forbidden(*args, **kwargs):
        pytest.fail("An untrusted URL must never reach a venue client")
    monkeypatch.setattr(pm, "public", forbidden)
    monkeypatch.setattr(pm, "get_market", forbidden)
    with pytest.raises(selection.SelectionError) as exc:
        await selection.resolve_selection(url)
    assert exc.value.code == "invalid_input"


async def test_event_keeps_every_market_and_exact_outcome_ids(monkeypatch):
    class Client:
        async def get_event(self, **kwargs):
            assert kwargs == {"slug": "bitcoin-monthly"}
            return {"markets": [market(), market("bitcoin-above-90000", OTHER_YES, OTHER_NO)]}
    async def public():
        return Client()
    monkeypatch.setattr(pm, "public", public)
    result = await selection.resolve_selection("https://polymarket.com/event/bitcoin-monthly")
    assert result["requires_selection"] is True
    assert len(result["markets"]) == 2
    assert [o["token_id"] for m in result["markets"] for o in m["outcomes"]] == [YES, NO, OTHER_YES, OTHER_NO]
    child = await selection.resolve_selection("https://polymarket.com/event/bitcoin-monthly/bitcoin-above-90000")
    assert [m["slug"] for m in child["markets"]] == ["bitcoin-above-90000"]
    with pytest.raises(selection.SelectionError) as exc:
        await selection.resolve_selection("https://polymarket.com/event/bitcoin-monthly/unrelated")
    assert exc.value.code == "not_found"


async def test_market_route_checks_returned_slug(monkeypatch):
    async def get_market(slug):
        assert slug == "bitcoin-above-80000"
        return market()
    monkeypatch.setattr(pm, "get_market", get_market)
    result = await selection.resolve_selection("https://polymarket.com/market/bitcoin-above-80000")
    assert result["source"] == "market"
    assert result["markets"][0]["outcomes"][1]["token_id"] == NO


async def test_empty_search_differs_from_lookup_failure(monkeypatch):
    async def empty(query, limit):
        assert query == "bitcoin" and limit == 20
        return []
    monkeypatch.setattr(pm, "search_markets", empty)
    assert (await selection.search_selection("bitcoin"))["markets"] == []
    async def broken(**kwargs):
        raise RuntimeError("private transport detail")
    monkeypatch.setattr(pm, "search_markets", broken)
    with pytest.raises(selection.SelectionError) as exc:
        await selection.search_selection("bitcoin")
    assert exc.value.code == "upstream_unavailable"
    assert "private transport detail" not in str(exc.value)


def stub_browse(monkeypatch, markets):
    class Page:
        async def first_page(self):
            return SimpleNamespace(items=markets)
    class Client:
        def list_markets(self, **kwargs):
            assert kwargs == {"closed": False, "order": "volume24hr",
                              "ascending": False, "page_size": 100}
            return Page()
    async def public():
        return Client()
    monkeypatch.setattr(pm, "public", public)


async def test_empty_query_browses_real_venue_markets(monkeypatch):
    stub_browse(monkeypatch, [market()])
    result = await selection.search_selection("")
    assert result["markets"][0]["outcomes"][0]["token_id"] == YES


async def test_browse_diversifies_events_by_volume_and_excludes_unavailable(monkeypatch):
    def candidate(slug, volume, event_id, *, closed=False, accepting=True):
        raw = market(slug)
        raw["condition_id"] = slug
        raw["state"] = {"closed": closed, "accepting_orders": accepting}
        raw["metrics"] = {"volume_24hr": volume}
        raw["events"] = [{"id": event_id, "slug": "event-" + event_id}]
        return raw
    stub_browse(monkeypatch, [
        candidate("candidate-low", 100, "election"),
        candidate("bitcoin", 800, "crypto"),
        candidate("candidate-high", 1000, "election"),
        candidate("closed", 2000, "closed", closed=True),
        candidate("paused", 1500, "paused", accepting=False),
        candidate("bitcoin", 700, "duplicate"),
        candidate("sports", "bad-volume", "sports"),
    ])
    choices = (await selection.search_selection("   "))["markets"]
    assert [choice["slug"] for choice in choices] == ["candidate-high", "bitcoin", "sports"]
    assert choices[0]["url"] == "https://polymarket.com/event/event-election/candidate-high"
    assert choices[0]["outcomes"][0]["token_id"] == YES


async def test_browse_is_bounded_and_preserves_distinct_markets_without_event_metadata(monkeypatch):
    markets = []
    for i in range(25):
        item = market("market-" + str(i))
        item.update(condition_id=str(i), volume_24hr=i)
        markets.append(item)
    stub_browse(monkeypatch, markets)
    choices = (await selection.search_selection(""))["markets"]
    assert len(choices) == 20
    assert choices[0]["slug"] == "market-24"
    assert choices[-1]["slug"] == "market-5"


async def test_browse_failure_is_not_presented_as_an_empty_feed(monkeypatch):
    async def unavailable():
        raise RuntimeError("private transport detail")
    monkeypatch.setattr(pm, "public", unavailable)
    with pytest.raises(selection.SelectionError) as exc:
        await selection.search_selection("")
    assert exc.value.code == "upstream_unavailable"
    assert "private transport detail" not in str(exc.value)


async def test_event_sdk_shape_is_slimmed_without_changing_tokens(monkeypatch):
    raw = market()
    raw["state"] = {"accepting_orders": True, "closed": False}
    raw["metrics"] = {"volume_24hr": 50000}
    class Client:
        async def get_event(self, *, slug):
            return {"markets": [raw]}
    async def public():
        return Client()
    monkeypatch.setattr(pm, "public", public)
    choice = (await selection.resolve_selection("https://polymarket.com/event/bitcoin"))["markets"][0]
    assert choice["accepting_orders"] is True
    assert choice["outcomes"][1] == {"label": "No", "token_id": NO, "price": 0.5, "side": "no"}


@pytest.mark.parametrize("ids", [[], YES, [int(YES)], ["1e50"], ["00123"], ["0"],
                                    [str(2**256)], [YES, "bad"], [YES] * 21])
async def test_invalid_specific_selection_never_calls_broad_sources(monkeypatch, ids):
    async def forbidden(*args, **kwargs):
        pytest.fail("An invalid explicit selection must not query any market")
    for name in ("search_markets", "top_markets", "markets_by_tag", "closing_soon", "get_market_by_token"):
        monkeypatch.setattr(pm, name, forbidden)
    p = runner.Pass(runner.normalize({"market_mode": "specific", "market_ids": ids}))
    assert await runner._universe(p) == []
    assert p.halted and p.universe["error"]


async def test_specific_candidates_never_use_broad_or_closing_sources(monkeypatch):
    looked_up = []
    async def lookup(tid):
        looked_up.append(tid)
        return market()
    async def forbidden(*args, **kwargs):
        pytest.fail("Specific selection cannot fall back to universe discovery")
    monkeypatch.setattr(pm, "get_market_by_token", lookup)
    for name in ("search_markets", "top_markets", "markets_by_tag", "closing_soon"):
        monkeypatch.setattr(pm, name, forbidden)
    p = runner.Pass(runner.normalize({"market_mode": "specific", "market_ids": [NO, YES],
                                      "topics": ["all"], "keyword": "unrelated"}))
    result = await runner._universe(p)
    soon = await runner._universe(p, closing_hours=72)
    assert len(result) == len(soon) == 1
    assert result[0]["market_id"] == YES
    assert looked_up == [NO, YES]  # Cached for the second strategy scan.


async def test_lookup_must_prove_selected_token_membership(monkeypatch):
    async def wrong(tid):
        return market()
    monkeypatch.setattr(pm, "get_market_by_token", wrong)
    p = runner.Pass(runner.normalize({"market_mode": "specific", "market_ids": [YES, OTHER_NO]}))
    assert await runner._universe(p) == []  # No partially valid fallback either.
    assert p.halted and OTHER_NO in p.halted


async def test_unselected_outcome_cannot_reach_order_checks(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Out-of-scope entry reached the order pipeline")
    monkeypatch.setattr(runner, "_hygiene", forbidden)
    p = runner.Pass(runner.normalize({"market_mode": "specific", "market_ids": [YES]}))
    result = await runner._order(p, {"title": "Bitcoin"}, NO, "no", "BUY", 0.5, 10, "fade", "test")
    assert result["result"] == "skipped"
    assert "outside" in result["detail"]


async def test_own_probability_only_matches_selected_candidates(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Own-probability strategy searched beyond the selection")
    monkeypatch.setattr(pm, "search_markets", forbidden)
    p = runner.Pass(runner.normalize({"market_mode": "specific", "market_ids": [YES],
                                      "vals": {"value": {"views": "Unselected election: 0.8"}}}))
    await runner._value(p, [])
    assert p.decisions[0]["result"] == "skipped"


async def test_selection_change_cancels_old_entries_before_ledger_mark(monkeypatch, tmp_path):
    async def lookup(tid):
        return market()
    monkeypatch.setattr(pm, "get_market_by_token", lookup)
    orders = [{"id": "outside", "token_id": OTHER_YES, "side": "BUY"},
              {"id": "selected", "token_id": YES, "side": "BUY"},
              {"id": "exit", "token_id": OTHER_NO, "side": "SELL"}]
    ledger = {"open_orders": deepcopy(orders)}
    monkeypatch.setattr(paper, "load", lambda: ledger)
    monkeypatch.setattr(paper, "save", lambda d: None)
    monkeypatch.setattr(paper, "cancel", lambda oid: ledger["open_orders"].remove(next(o for o in ledger["open_orders"] if o["id"] == oid)))
    async def positions():
        assert [o["id"] for o in ledger["open_orders"]] == ["selected", "exit"]
        return {"cash": 1000, "equity": 1000, "bankroll": 1000, "positions": [], "open_orders": ledger["open_orders"]}
    monkeypatch.setattr(paper, "positions", positions)
    result = await runner.run_pass({"market_mode": "specific", "market_ids": [YES]}, tmp_path / "ledger.json")
    assert result["decisions"][0]["result"] == "cancelled"
    assert result["decisions"][0]["token_id"] == OTHER_YES


def test_legacy_universe_and_unknown_mode_do_not_blur_together():
    assert runner.normalize({})["market_mode"] == "universe"
    assert runner.normalize({"market_ids": [YES]})["market_mode"] == "specific"
    assert runner.normalize({"market_mode": "speficic"})["selection_error"]
