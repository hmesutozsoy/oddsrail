"""No-network contract tests for the live two-outcome quote planner."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, Inexact, localcontext

import pytest

from oddsrail.live.quotes import QuoteBook, QuotePolicy, QuoteUnavailable, plan_quotes


D = Decimal
CONDITION = "0x" + "a" * 64
OTHER_CONDITION = "0x" + "b" * 64


def policy(**overrides):
    fields = dict(condition_id=CONDITION, yes_token_id="123", no_token_id="456",
                  shares=D("20"), distance=D("0.02"), per_order_usd=D("20"))
    return QuotePolicy(**(fields | overrides))


def book(token_id="123", **overrides):
    fields = dict(condition_id=CONDITION, token_id=token_id, best_bid=D("0.49"),
                  best_ask=D("0.51"), tick_size=D("0.01"), min_order_size=D("5"),
                  observed_at_ms=10_000, generation=4, active=True,
                  accepting_orders=True)
    return QuoteBook(**(fields | overrides))


def plan(p=None, yes=None, no=None, **overrides):
    return plan_quotes(p or policy(), yes or book(), no or book("456"),
                       **(dict(now_ms=10_500, generation=4) | overrides))


def refused(code, **kwargs):
    with pytest.raises(QuoteUnavailable) as error:
        plan(**kwargs)
    assert error.value.code == code


def test_both_outcomes_keep_requested_shares_and_rest_below_own_midpoint():
    yes, no = plan()
    assert (yes.token_id, no.token_id) == ("123", "456")
    for quote in (yes, no):
        assert quote.condition_id == CONDITION
        assert quote.price == D("0.48")
        assert quote.size == D("20")
        assert quote.notional == D("9.60")
        assert quote.side == "BUY"
        assert quote.post_only is True
        assert quote.tick_size == D("0.01")
        assert quote.min_order_size == D("5")
        assert quote.observed_at_ms == 10_000 and quote.generation == 4


def test_each_outcome_uses_own_midpoint_even_if_prices_do_not_sum_to_one():
    yes, no = plan(yes=book(best_bid=D("0.29"), best_ask=D("0.31")),
                   no=book("456", best_bid=D("0.74"), best_ask=D("0.76")))
    assert yes.price == D("0.28")
    assert no.price == D("0.73")
    assert yes.price + no.price == D("1.01")  # No invented complement or profit guarantee.


@pytest.mark.parametrize("tick,bid,ask,expected", [
    ("0.1", "0.4", "0.6", "0.4"),
    ("0.01", "0.48", "0.51", "0.47"),
    ("0.001", "0.481", "0.510", "0.475"),
    ("0.0001", "0.4811", "0.5100", "0.4755"),
])
def test_rounds_each_quote_down_to_current_venue_tick(tick, bid, ask, expected):
    yes, _ = plan(yes=book(tick_size=D(tick), best_bid=D(bid), best_ask=D(ask)))
    assert yes.price == D(expected)
    assert yes.price % D(tick) == 0


def test_caps_price_one_current_tick_below_ask():
    yes, _ = plan(p=policy(distance=D("0.001")),
                  yes=book(best_bid=D("0.499"), best_ask=D("0.501")))
    assert yes.price == D("0.49")
    assert yes.price <= D("0.501") - D("0.01")


def test_dollar_limit_truncates_size_without_exceeding_requested_shares():
    yes, no = plan(p=policy(per_order_usd=D("3")))
    assert yes.size == no.size == D("6.25")
    assert yes.notional == D("3")
    yes, _ = plan(p=policy(per_order_usd=D("2.999999999999")))
    assert yes.size == D("6.24")
    assert yes.notional < D("2.999999999999")


def test_each_outcome_size_respects_its_own_price_and_cap():
    yes, no = plan(p=policy(per_order_usd=D("3")),
                   yes=book(best_bid=D("0.19"), best_ask=D("0.21")))
    assert yes.size == D("16.66")
    assert no.size == D("6.25")
    assert max(yes.notional, no.notional) <= D("3")


def test_exact_market_minimum_is_accepted_but_never_rounded_up_to_it():
    assert plan(p=policy(shares=D("5")))[0].size == D("5")
    refused("below_minimum_size", p=policy(shares=D("4.99")))
    refused("below_minimum_size", p=policy(per_order_usd=D("2.399999999999")))
    refused("below_minimum_size", no=book("456", min_order_size=D("20.001")))


def test_if_either_price_is_impossible_no_partial_pair_is_returned():
    refused("no_valid_price", no=book("456", best_bid=D("0.01"), best_ask=D("0.02")))


def test_policy_and_books_and_returned_quotes_are_immutable():
    for item, name, value in ((policy(), "shares", D("500")),
                              (book(), "generation", 99),
                              (plan()[0], "post_only", False)):
        with pytest.raises(FrozenInstanceError):
            setattr(item, name, value)
    assert isinstance(plan(), tuple)


@pytest.mark.parametrize("name", ["shares", "distance", "per_order_usd"])
@pytest.mark.parametrize("value", [True, False, 1, 0.1, "1", None,
                                   D("NaN"), D("sNaN"), D("Infinity"),
                                   D("-Infinity"), D("0"), D("-1"), D("1e-999")])
def test_policy_rejects_non_decimal_nonfinite_and_nonpositive_amounts(name, value):
    with pytest.raises(ValueError):
        policy(**{name: value})


@pytest.mark.parametrize("overrides", [
    {"shares": D("5.001")}, {"shares": D("1000001")},
    {"distance": D("1")}, {"per_order_usd": D("1000001")},
    {"yes_token_id": "456"}, {"yes_token_id": "01"},
    {"yes_token_id": str(2**256)}, {"no_token_id": 456},
    {"condition_id": "0x123"}, {"condition_id": "0x" + "A" * 64},
    {"max_book_age_ms": 0}, {"max_book_age_ms": 60_001},
    {"max_book_age_ms": True}, {"max_book_age_ms": D("5000")},
    {"max_book_skew_ms": -1}, {"max_book_skew_ms": 60_001},
    {"max_book_skew_ms": False}, {"max_book_skew_ms": 5001},
])
def test_policy_rejects_invalid_identity_precision_and_freshness_bounds(overrides):
    with pytest.raises(ValueError):
        policy(**overrides)


@pytest.mark.parametrize("name", ["best_bid", "best_ask", "tick_size", "min_order_size"])
@pytest.mark.parametrize("value", [None, True, .1, "0.1", D("NaN"), D("sNaN"),
                                   D("Infinity"), D("0"), D("-1"), D("1e-999")])
def test_book_requires_known_strict_decimal_prices_and_metadata(name, value):
    with pytest.raises(ValueError):
        book(**{name: value})


@pytest.mark.parametrize("overrides", [
    {"best_bid": D("0.51")}, {"best_bid": D("0.52")},
    {"best_ask": D("1")}, {"best_ask": D("1.01")},
    {"tick_size": D("0.02")}, {"tick_size": D("0.00001")},
    {"active": "true"}, {"active": 1}, {"accepting_orders": None},
    {"observed_at_ms": -1}, {"observed_at_ms": .1}, {"observed_at_ms": True},
    {"generation": -1}, {"generation": "4"}, {"generation": True},
    {"condition_id": OTHER_CONDITION.upper()}, {"token_id": "0"},
])
def test_invalid_book_never_becomes_quote_input(overrides):
    with pytest.raises(ValueError):
        book(**overrides)


@pytest.mark.parametrize("overrides", [{"condition_id": OTHER_CONDITION}, {"token_id": "789"}])
def test_refuses_wrong_market_and_outcome_identity(overrides):
    refused("identity_mismatch", yes=book(**overrides))


def test_refuses_swapped_outcomes_or_duplicate_books():
    refused("identity_mismatch", yes=book("456"), no=book())
    refused("identity_mismatch", no=book())


@pytest.mark.parametrize("change", [{"active": False}, {"accepting_orders": False}])
def test_either_ineligible_outcome_prevents_both_quotes(change):
    refused("market_unavailable", yes=book(**change))
    refused("market_unavailable", no=book("456", **change))


def test_requires_same_current_generation_for_both_books():
    refused("generation_mismatch", yes=book(generation=3))
    refused("generation_mismatch", no=book("456", generation=5))
    refused("generation_mismatch", generation=5)


def test_stale_future_and_skewed_books_refuse_entire_pair():
    assert len(plan(now_ms=15_000)) == 2  # Age boundary is inclusive.
    refused("stale_book", now_ms=15_001)
    refused("stale_book", yes=book(observed_at_ms=10_501))
    refused("stale_book", no=book("456", observed_at_ms=5_499))
    assert len(plan(no=book("456", observed_at_ms=9_000))) == 2
    refused("book_skew", no=book("456", observed_at_ms=8_999))


@pytest.mark.parametrize("name", ["now_ms", "generation"])
@pytest.mark.parametrize("value", [True, False, -1, 1.1, "1", None, 2**63])
def test_planning_rejects_bad_clock_and_generation(name, value):
    with pytest.raises(ValueError):
        plan(**{name: value})


def test_unvalidated_duck_typed_objects_are_rejected():
    with pytest.raises(ValueError):
        plan_quotes({}, book(), book("456"), now_ms=10_500, generation=4)
    with pytest.raises(ValueError):
        plan_quotes(policy(), {}, book("456"), now_ms=10_500, generation=4)


def test_decimal_math_does_not_depend_on_callers_context():
    p = policy(per_order_usd=D("2.999999999999"))
    with localcontext() as ctx:
        ctx.prec = 2
        result = plan(p=p)
    assert result[0].size == D("6.24")
    assert result[0].notional == D("2.9952")


def test_decimal_math_does_not_inherit_callers_rounding_traps():
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        p = policy(per_order_usd=D("2.999999999999"))
        result = plan(p=p)
        assert result[0].notional == D("2.9952")


def test_many_valid_inputs_never_exceed_shares_or_usd_limits():
    # Exercise discontinuities at a tick boundary and a cap/share boundary.
    for tick in (D("0.01"), D("0.001"), D("0.0001")):
        for midpoint in (D("0.10"), D("0.4955"), D("0.81")):
            for cap in (D("0.499999999999"), D("1"), D("3"), D("20")):
                b = book(best_bid=midpoint - tick, best_ask=midpoint + tick,
                         tick_size=tick, min_order_size=D("0.01"))
                yes, no = plan(p=policy(per_order_usd=cap), yes=b,
                               no=replace(b, token_id="456"))
                for quote in (yes, no):
                    assert quote.size <= D("20")
                    assert quote.notional <= cap
                    assert quote.price % tick == 0
                    assert quote.size % D("0.01") == 0
                    assert quote.price < b.best_ask
