"""V2 allocation and daily-stop regressions using real paper accounting offline."""

import datetime as dt
import time

import pytest

from oddsrail import paper, polymarket as pm
from oddsrail.cloud import runner
from test_runner import Fake, TITLE, YES, NO, cfg, seed


@pytest.fixture
def budget_ledger(tmp_path, monkeypatch):
    path = tmp_path / "v2-paper.json"
    token = runner.FORCED_LEDGER.set(path)
    fake = Fake(yes_book=(0.49, 0.51), no_book=(0.49, 0.51))
    fake.books["third"] = (0.49, 0.51)
    fake.install(monkeypatch)
    paper.reset()

    async def checked(*args, **kwargs):
        return {"verdict": "ok", "checks": [{"check": "offline", "status": "ok"}]}

    monkeypatch.setattr(runner.ck, "check_order", checked)
    yield path
    runner.FORCED_LEDGER.reset(token)


def pass_for(**kwargs):
    return runner.Pass(runner.normalize(cfg([], schema_version=2, **kwargs)))


def quote(oid, token=YES, side="BUY", price=0.25, size=20):
    return {"id": oid, "time": time.time(), "token_id": token, "title": TITLE,
            "side": side, "price": price, "size": size}


async def buy(p, token, notional, *, title=TITLE, price=0.3, strategy="mm", resting=True):
    return await runner._order(p, {"title": title, "venue_ref": {}}, token, "yes", "BUY",
                               price, notional, strategy, "offline budget regression", resting=resting)


def test_normalized_marker_opts_in_without_changing_legacy():
    assert runner.normalize({"schema_version": 2})["schema_version"] == 2
    assert runner.normalize({})["schema_version"] == 1


async def test_all_strategies_share_position_and_resting_order_allocation(budget_ledger):
    p = pass_for(bankroll=25, perorder=25)
    first = await buy(p, YES, 15, price=0.51, resting=False, strategy="momentum", title="First")
    second = await buy(p, NO, 6, strategy="mm", title="Second")
    third = await buy(p, "third", 10, strategy="fade", title="Third")

    assert first["result"] == "filled"
    assert second["result"] == "resting"
    assert third["result"] == "resting" and third["notional"] <= 4.01
    held, reserved = runner._committed_capital(paper.load())
    assert held > 0 and reserved > 0 and held + reserved <= 25 + 1e-9


async def test_existing_positions_and_buy_quotes_count_while_sell_quotes_do_not_release_cost(budget_ledger):
    seed(budget_ledger, positions={YES: {"size": 20, "cost": 10, "title": TITLE}},
         open_orders=[quote("buy"), quote("sell", side="SELL", price=0.9, size=20)])
    result = await buy(pass_for(bankroll=20), NO, 10)
    assert result["result"] == "resting"
    assert result["notional"] <= 5
    assert sum(runner._committed_capital(paper.load())) <= 20 + 1e-9


async def test_resting_orders_cannot_double_allocate_remaining_cash(budget_ledger):
    seed(budget_ledger, cash=6, open_orders=[quote("reserved", price=0.5, size=10)])
    result = await buy(pass_for(bankroll=100), NO, 20)
    assert result["result"] == "resting" and result["notional"] <= 1
    assert runner._committed_capital(paper.load())[1] <= 6


async def test_partial_fill_and_unfilled_remainder_both_consume_allocation(budget_ledger, monkeypatch):
    async def thin_book(token_id):
        return {"best_bid": "0.49", "best_ask": "0.50", "bids": [],
                "asks": [{"price": "0.50", "size": "2"}]}

    monkeypatch.setattr(pm, "get_orderbook", thin_book)
    p = pass_for(bankroll=10)
    result = await buy(p, YES, 10, price=0.5, resting=False)
    assert result["result"] == "partial"
    assert runner._committed_capital(paper.load()) == pytest.approx((1, 9))
    next_order = await buy(p, NO, 10, title="Other market")
    assert next_order["result"] == "skipped" and "capital" in next_order["detail"]


async def test_minimum_notional_never_upsizes_past_remaining_allocation(budget_ledger):
    seed(budget_ledger, positions={YES: {"size": 19, "cost": 9.5, "title": TITLE}})
    result = await buy(pass_for(bankroll=10), NO, 20, price=0.51, resting=False)
    assert result["result"] == "skipped"
    assert "minimum" in result["detail"]
    assert paper.load()["fills"] == []


@pytest.mark.parametrize("constraint", ["perorder", "bankroll", "expo"])
async def test_final_hygiene_price_cannot_exceed_any_money_cap(budget_ledger, monkeypatch, constraint):
    raw = cfg([], schema_version=2, bankroll=100, perorder=100)
    if constraint == "expo":
        raw["on"]["expo"] = True
        raw["vals"]["expo"] = {"usd": 10}
    else:
        raw[constraint] = 10
    p = runner.Pass(runner.normalize(raw))

    async def deeper_limit(*args, **kwargs):
        return None, 0.54

    monkeypatch.setattr(runner, "_hygiene", deeper_limit)
    result = await buy(p, YES, 10, price=0.5, resting=False)
    assert result["result"] == "filled"
    assert result["price"] == 0.54
    assert result["size"] * result["price"] <= 10
    assert sum(runner._committed_capital(paper.load())) <= 10


async def test_exposure_includes_preexisting_resting_buys(budget_ledger):
    seed(budget_ledger, open_orders=[quote("already-reserved", size=36)])  # $9
    p = pass_for(bankroll=100, vals={"expo": {"usd": 10}})
    p.cfg["on"]["expo"] = True
    result = await buy(p, NO, 20, price=0.51, resting=False)
    assert result["result"] == "skipped" and "minimum" in result["detail"]
    assert len(paper.load()["open_orders"]) == 1


async def test_budget_is_rechecked_after_async_preflight(budget_ledger, monkeypatch):
    async def consume_during_check(*args, **kwargs):
        d = paper.load()
        d["open_orders"].append(quote("another-entry", price=0.5, size=20))
        paper.save(d)
        return {"verdict": "ok", "checks": []}

    monkeypatch.setattr(runner.ck, "check_order", consume_during_check)
    result = await buy(pass_for(bankroll=10), YES, 10)
    assert result["result"] == "skipped" and "changed" in result["detail"]
    assert len(paper.load()["open_orders"]) == 1


async def test_lowered_allocation_cancels_overcommitted_quotes_before_they_can_fill(budget_ledger):
    seed(budget_ledger, positions={YES: {"size": 10, "cost": 5, "title": TITLE}},
         open_orders=[quote("crossed", token=NO, price=0.6, size=20)])
    result = await runner.run_pass(cfg([], schema_version=2, bankroll=10), budget_ledger)
    d = paper.load()
    assert d["fills"] == []
    assert NO not in d["positions"]
    assert d["open_orders"] == []
    assert any(x["strategy"] == "capital" and x["result"] == "cancelled" for x in result["decisions"])


async def test_positions_above_lowered_allocation_are_retained_and_new_entries_blocked(budget_ledger):
    seed(budget_ledger, positions={YES: {"size": 40, "cost": 20, "title": TITLE}})
    result = await buy(pass_for(bankroll=10), NO, 5)
    assert result["result"] == "skipped" and "capital" in result["detail"]
    assert paper.load()["positions"][YES]["cost"] == 20


def today():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


async def test_observed_daily_halt_cancels_all_resting_buys_and_preserves_sells(budget_ledger):
    seed(budget_ledger, cash=900,
         positions={YES: {"size": 10, "cost": 5, "title": TITLE}},
         open_orders=[quote("buy-yes"), quote("buy-no", token=NO),
                      quote("exit", side="SELL", price=0.9, size=10)],
         runner={"day": today(), "day_start_equity": 1000})
    result = await runner.run_pass(cfg(["daily", "mm"], schema_version=2,
                                      vals={"daily": {"usd": 50}}), budget_ledger)
    assert "daily loss limit" in result["halted"]
    assert [o["id"] for o in paper.load()["open_orders"]] == ["exit"]
    assert len([x for x in result["decisions"] if x["result"] == "cancelled"]) == 2
    assert result["orders_placed"] == 0
    assert paper.load()["runner"]["daily_halted_day"] == today()


async def test_same_day_halt_is_applied_before_initial_mark_even_if_equity_recovered(budget_ledger):
    seed(budget_ledger, open_orders=[quote("would-fill", price=0.9)],
         runner={"day": today(), "day_start_equity": 1000, "daily_halted_day": today()})
    result = await runner.run_pass(cfg(["daily", "mm"], schema_version=2), budget_ledger)
    assert "daily loss limit" in result["halted"]
    assert paper.load()["fills"] == []
    assert paper.load()["open_orders"] == []
    assert result["orders_placed"] == 0


async def test_zero_equity_triggers_daily_halt_before_strategy_review(budget_ledger, monkeypatch):
    seed(budget_ledger, cash=0, runner={"day": today(), "day_start_equity": 1000})

    async def review(p, pos):
        assert p.equity == 0
        assert "daily loss limit" in p.halted

    monkeypatch.setattr(runner, "_review", review)
    result = await runner.run_pass(cfg(["daily", "mm"], schema_version=2), budget_ledger)
    assert result["orders_placed"] == 0


async def test_yesterdays_halt_does_not_block_new_utc_day(budget_ledger):
    yesterday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    seed(budget_ledger, runner={"day": yesterday, "day_start_equity": 1000,
                               "daily_halted_day": yesterday})
    result = await runner.run_pass(cfg(["daily", "mm"], schema_version=2), budget_ledger)
    assert result["halted"] is None
    assert result["orders_placed"] == 2


async def test_loss_first_observed_on_final_mark_cancels_new_quotes(budget_ledger, monkeypatch):
    original = paper.positions
    calls = 0

    async def mark():
        nonlocal calls
        calls += 1
        if calls == 2:
            d = paper.load()
            d["cash"] = 900
            paper.save(d)
        return await original()

    monkeypatch.setattr(paper, "positions", mark)
    result = await runner.run_pass(cfg(["daily", "mm"], schema_version=2), budget_ledger)
    assert "daily loss limit" in result["halted"]
    assert result["orders_placed"] == 2
    assert result["ledger"]["open_orders"] == []
    assert paper.load()["open_orders"] == []


async def test_unavailable_initial_mark_halts_and_cancels_entries(budget_ledger, monkeypatch):
    seed(budget_ledger, open_orders=[quote("pending")])

    async def unavailable():
        raise RuntimeError("offline mark unavailable")

    monkeypatch.setattr(paper, "positions", unavailable)
    result = await runner.run_pass(cfg(["daily", "mm"], schema_version=2), budget_ledger)
    assert "status unavailable" in result["halted"]
    assert result["orders_placed"] == 0
    assert paper.load()["open_orders"] == []


async def test_legacy_bankroll_remains_a_sizing_input(budget_ledger):
    result = await runner.run_pass(cfg(["mm"], bankroll=10), budget_ledger)
    assert result["orders_placed"] == 2
    assert sum(runner._committed_capital(paper.load())) > 10


async def test_v2_quote_pair_requires_two_available_outcome_slots(budget_ledger):
    result = await runner.run_pass(cfg(["mm"], schema_version=2, maxpos=1), budget_ledger)
    assert result["orders_placed"] == 0
    assert paper.load()["open_orders"] == []
    assert any("both outcome quotes" in x.get("detail", "") for x in result["decisions"])


async def test_v2_quote_pair_can_reserve_exactly_two_outcome_slots(budget_ledger):
    result = await runner.run_pass(cfg(["mm"], schema_version=2, maxpos=2), budget_ledger)
    assert result["orders_placed"] == 2
    assert runner._position_tokens(paper.load()) == {YES, NO}


async def test_one_held_outcome_leaves_only_one_new_slot_needed_for_pair(budget_ledger):
    seed(budget_ledger, positions={YES: {"size": 10, "cost": 5, "title": TITLE}})
    result = await runner.run_pass(cfg(["mm"], schema_version=2, maxpos=2), budget_ledger)
    assert result["orders_placed"] == 2
    assert runner._position_tokens(paper.load()) == {YES, NO}


async def test_unrelated_resting_buy_consumes_a_slot_before_quoting_a_pair(budget_ledger):
    seed(budget_ledger, open_orders=[quote("other-market", token="third")])
    result = await runner.run_pass(cfg(["mm"], schema_version=2, maxpos=2), budget_ledger)
    assert result["orders_placed"] == 0
    assert [o["id"] for o in paper.load()["open_orders"]] == ["other-market"]


async def test_position_slots_deduplicate_held_and_resting_buys_for_same_token(budget_ledger):
    seed(budget_ledger, positions={YES: {"size": 10, "cost": 5, "title": TITLE}},
         open_orders=[quote("same-token"), quote("risk-exit", side="SELL", price=0.9)])
    assert runner._position_tokens(paper.load()) == {YES}
    result = await buy(pass_for(maxpos=1), YES, 5)
    assert result["result"] == "resting"
    assert runner._position_tokens(paper.load()) == {YES}


async def test_all_new_buys_count_pending_outcomes_across_strategies(budget_ledger):
    p = pass_for(maxpos=1)
    assert (await buy(p, YES, 5, strategy="mm"))["result"] == "resting"
    second = await buy(p, NO, 5, title="Different market", strategy="momentum")
    assert second["result"] == "skipped" and "max open positions" in second["detail"]
    assert runner._position_tokens(paper.load()) == {YES}


async def test_position_slots_are_rechecked_after_async_preflight(budget_ledger, monkeypatch):
    async def occupy_other_token(*args, **kwargs):
        d = paper.load()
        d["open_orders"].append(quote("occupied-during-check", token=NO, size=1))
        paper.save(d)
        return {"verdict": "ok", "checks": []}

    monkeypatch.setattr(runner.ck, "check_order", occupy_other_token)
    result = await buy(pass_for(maxpos=1), YES, 5)
    assert result["result"] == "skipped" and "position capacity changed" in result["detail"]
    assert runner._position_tokens(paper.load()) == {NO}


async def test_v2_quote_refresh_never_cancels_existing_sell_exit(budget_ledger):
    seed(budget_ledger, positions={YES: {"size": 10, "cost": 5, "title": TITLE}},
         open_orders=[quote("old-buy"), quote("risk-exit", side="SELL", price=0.9, size=10)])
    result = await runner.run_pass(cfg(["mm"], schema_version=2), budget_ledger)
    assert result["orders_placed"] == 2
    ids = {o["id"] for o in paper.load()["open_orders"]}
    assert "risk-exit" in ids and "old-buy" not in ids


async def test_legacy_position_limit_retains_old_quote_pair_behavior(budget_ledger):
    result = await runner.run_pass(cfg(["mm"], maxpos=1), budget_ledger)
    assert result["orders_placed"] == 2
    assert runner._position_tokens(paper.load()) == {YES, NO}
