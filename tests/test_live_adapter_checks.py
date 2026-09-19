"""Contract verification only: synthetic documented fixtures, no venue writes.

These tests do not satisfy the production financial integration gate. In
particular no session permission, real signer, funding or settlement is tested.
"""

from dataclasses import replace
from decimal import Decimal, localcontext
import itertools
import json
from pathlib import Path

import pytest

from oddsrail.live.adapter_checks import (
    AdapterContractError,
    CursorPage,
    FillEvidence,
    acknowledged_order,
    aggregate_fills,
    checked_order,
    collect_complete_pages,
    verify_encoded_buy_amounts,
    verify_fill_coverage,
)
from oddsrail.live.contracts import OrderRequest, PreparedOrder


FIXTURE = json.loads((Path(__file__).parent / "fixtures/adapter/documented-wire.json").read_text())
PREPARED = PreparedOrder(
    account=FIXTURE["account"], session_id="session-1", intent_id="intent-1", order_hash=FIXTURE["order_hash"],
    request=OrderRequest(FIXTURE["condition_id"], FIXTURE["token_id"], Decimal("0.52"), Decimal("10"),
                         Decimal("0.01"), Decimal("5")),
)


def order(raw=None, *, prepared=PREPARED, owner=FIXTURE["owner_uuid"], order_type="GTC", expiration=0):
    return checked_order(FIXTURE["order"] if raw is None else raw, prepared, credential_owner=owner,
                         expected_order_type=order_type, expected_expiration=expiration)


def fill(*, trade_id="trade-1", size="2", status="MATCHED", at=1_000, authoritative=False, **changes):
    return replace(FillEvidence(
        account=PREPARED.account, session_id=PREPARED.session_id, order_hash=PREPARED.order_hash,
        condition_id=PREPARED.request.condition_id, token_id=PREPARED.request.token_id,
        trade_id=trade_id, bucket_index=0, size=Decimal(size), price=Decimal("0.52"),
        status=status, updated_at_ms=at, authoritative=authoritative,
    ), **changes)


def page(rows, cursor="LTE=", request=None):
    return CursorPage(request, {"data": rows, "limit": 100, "count": len(rows), "next_cursor": cursor})


def test_documented_fixture_is_explicitly_not_real_integration_evidence():
    assert "not captured exchange evidence" in FIXTURE["provenance"]["kind"]
    assert "No raw trade parser is enabled" in FIXTURE["provenance"]["raw_trade_blocker"]
    assert len(FIXTURE["provenance"]["sources"]) == 5


def test_acceptance_only_returns_exact_prepared_hash():
    assert acknowledged_order(FIXTURE["submit_ack"], PREPARED, http_status=200) == PREPARED.order_hash
    # Do not use ACK makingAmount/takingAmount as cumulative settlement.
    raw = dict(FIXTURE["submit_ack"], takingAmount="999999", makingAmount="999999")
    assert acknowledged_order(raw, PREPARED, http_status=200) == PREPARED.order_hash


@pytest.mark.parametrize("key,value", [
    ("success", False), ("success", 1), ("success", "true"), ("success", None),
    ("errorMsg", "not enough balance / allowance"), ("errorMsg", None),
    ("orderID", "0x" + "c" * 64), ("orderID", ""), ("orderID", None),
    ("status", "matched"), ("status", "delayed"), ("status", "unmatched"), ("status", "LIVE"),
])
def test_malformed_or_contradictory_ack_is_unknown_not_rejection(key, value):
    with pytest.raises(AdapterContractError):
        acknowledged_order(dict(FIXTURE["submit_ack"], **{key: value}), PREPARED, http_status=200)


@pytest.mark.parametrize("status", [201, 400, 401, 429, 500, True, "200"])
def test_http_status_cannot_be_ignored_even_with_success_shaped_body(status):
    with pytest.raises(AdapterContractError):
        acknowledged_order(FIXTURE["submit_ack"], PREPARED, http_status=status)


def test_sdk_normalized_ack_is_not_misread_as_raw_http():
    with pytest.raises(AdapterContractError):
        acknowledged_order({"ok": True, "order_id": PREPARED.order_hash, "status": "live"}, PREPARED, http_status=200)


@pytest.mark.parametrize("key", ["success", "errorMsg", "orderID", "status"])
def test_ack_does_not_default_missing_required_fields(key):
    raw = dict(FIXTURE["submit_ack"])
    del raw[key]
    with pytest.raises(AdapterContractError):
        acknowledged_order(raw, PREPARED, http_status=200)


def test_response_errors_do_not_leak_response_text_or_credential_identifiers():
    marker = "sensitive-signer-detail-do-not-log"
    with pytest.raises(AdapterContractError) as error:
        acknowledged_order(dict(FIXTURE["submit_ack"], errorMsg=marker), PREPARED, http_status=200)
    assert marker not in str(error.value)
    with pytest.raises(AdapterContractError) as error:
        order(dict(FIXTURE["order"], owner=marker))
    assert marker not in str(error.value)


def test_buy_amounts_are_encoded_six_decimal_integers_under_tiny_decimal_context():
    with localcontext() as context:
        context.prec = 2
        verify_encoded_buy_amounts(PREPARED.request, maker_amount="5200000", taker_amount="10000000")


@pytest.mark.parametrize("maker,taker", [
    ("5.20", "10"), ("5200001", "10000000"), ("5200000", "9999999"),
    (5200000, "10000000"), ("05200000", "10000000"), ("5200000", "1e7"),
    ("+5200000", "10000000"), ("5200000", "100000000000000000000000"),
])
def test_buy_amount_conversion_never_silently_rounds_or_changes_request(maker, taker):
    with pytest.raises(AdapterContractError):
        verify_encoded_buy_amounts(PREPARED.request, maker_amount=maker, taker_amount=taker)


def test_complete_pagination_retains_duplicate_events_for_domain_reconciliation():
    rows = ({"id": "trade-1", "status": "MATCHED"}, {"id": "trade-1", "status": "CONFIRMED"})
    pages = (page([rows[0]], "MTAw"), page([rows[1]], request="MTAw"))
    assert collect_complete_pages(pages, lambda value: value) == rows
    assert collect_complete_pages((CursorPage(None, FIXTURE["empty_final_page"]),), lambda value: value) == ()


@pytest.mark.parametrize("pages", [
    (),
    (page([], cursor="MTAw"),),
    (page([{}], cursor="MTAw"),),
    (page([{}], cursor="MTAw"), page([], request="wrong")),
    (page([{}], cursor="MTAw"), page([{}], cursor="MTAw", request="MTAw")),
    (page([{}], cursor="MTAw"), page([{}], cursor="MjAw", request="MTAw"),
     page([{}], cursor="MTAw", request="MjAw")),
    (page([]), page([])),
    (page([{}], request="MTAw"),),
    (CursorPage(None, []),),
    (CursorPage(None, {"items": [], "has_more": False}),),
])
def test_incomplete_and_cyclic_pagination_never_becomes_an_empty_account(pages):
    with pytest.raises(AdapterContractError):
        collect_complete_pages(pages, lambda value: value)


@pytest.mark.parametrize("key,value", [
    ("count", 2), ("count", True), ("count", "1"), ("count", -1),
    ("limit", 0), ("limit", True), ("limit", 10000), ("data", {}),
    ("next_cursor", None), ("next_cursor", ""), ("next_cursor", "x" * 129),
    ("next_cursor", "https://untrusted.example/cursor"),
])
def test_malformed_pagination_is_rejected(key, value):
    raw = dict(page([{}]).response, **{key: value})
    with pytest.raises(AdapterContractError):
        collect_complete_pages((CursorPage(None, raw),), lambda value: value)


def test_pagination_checks_over_budget_final_page_before_parsing_any_rows():
    parsed = []
    pages = (page([{}], "MTAw"), page([{}, {}], request="MTAw"))
    with pytest.raises(AdapterContractError, match="row budget"):
        collect_complete_pages(pages, parsed.append, max_rows=2)
    assert parsed == []
    with pytest.raises(AdapterContractError, match="over-budget"):
        collect_complete_pages(pages, parsed.append, max_pages=1)


def test_order_identity_and_economics_use_normalized_shares_and_keep_canceled_fills():
    result = order()
    assert result.matched_size == Decimal("4")
    assert result.original_size == Decimal("10")
    assert result.state == "CANCELED"
    assert not hasattr(result, "final")
    assert result.associated_trades == ("trade-1", "trade-2")


@pytest.mark.parametrize("key,value", [
    ("id", "0x" + "d" * 64), ("market", "0x" + "d" * 64), ("asset_id", "123"),
    ("maker_address", "0x" + "e" * 40), ("owner", "00000000-0000-0000-0000-000000000000"),
    ("side", "SELL"), ("price", "0.53"), ("original_size", "11"), ("size_matched", "11"),
    ("size_matched", "10000000"), ("size_matched", 4), ("size_matched", "NaN"),
    ("size_matched", "-0"), ("size_matched", "1e-2"), ("size_matched", "0.0000000000000000001"),
    ("status", "FILLED"), ("status", []), ("associate_trades", None),
    ("associate_trades", ["trade-1", "trade-1"]), ("order_type", "FOK"), ("expiration", "1"),
])
def test_order_mismatch_and_units_drift_fail_closed(key, value):
    with pytest.raises(AdapterContractError):
        order(dict(FIXTURE["order"], **{key: value}))


def test_order_trades_field_is_not_defaulted_to_no_fills():
    raw = dict(FIXTURE["order"])
    del raw["associate_trades"]
    with pytest.raises(AdapterContractError, match="trade coverage"):
        order(raw)


def test_reading_complete_pages_still_checks_every_order_owner():
    wrong = dict(FIXTURE["order"], maker_address="0x" + "e" * 40)
    with pytest.raises(AdapterContractError, match="account"):
        collect_complete_pages((page([FIXTURE["order"]], "MTAw"), page([wrong], request="MTAw")), order)


def test_order_expected_lifetime_must_match_the_submitted_envelope():
    raw = dict(FIXTURE["order"], order_type="GTD", expiration="1700003600")
    assert order(raw, order_type="GTD", expiration=1700003600).state == "CANCELED"
    with pytest.raises(AdapterContractError, match="lifetime"):
        order(raw)
    with pytest.raises(AdapterContractError, match="lifetime"):
        order(order_type="GTD", expiration=0)


def test_duplicate_and_out_of_order_notifications_do_not_double_count_or_unconfirm():
    events = (fill(), fill(status="MINED", at=2_000),
              fill(status="CONFIRMED", at=3_000, authoritative=True), fill())
    for sequence in itertools.permutations(events):
        result = aggregate_fills(PREPARED, sequence)
        assert result.matched_size == result.confirmed_size == Decimal("2")
        assert result.failed_size == result.unsettled_size == 0
        assert result.terminal_states_verified


def test_unverified_failure_does_not_release_funds_and_retrying_is_not_failure():
    events = (fill(status="FAILED"), fill(trade_id="trade-2", status="RETRYING"))
    result = aggregate_fills(PREPARED, events)
    assert result.failed_size == result.confirmed_size == 0
    assert result.unsettled_size == result.matched_size == Decimal("4")
    assert not result.terminal_states_verified
    result = aggregate_fills(PREPARED, events + (fill(status="FAILED", at=2_000, authoritative=True),))
    assert result.failed_size == result.unsettled_size == Decimal("2")
    assert not result.terminal_states_verified


def test_verified_partial_failure_and_confirmed_fill_cover_canceled_order_without_finalizing():
    result = aggregate_fills(PREPARED, (
        fill(status="CONFIRMED", authoritative=True),
        fill(trade_id="trade-2", status="FAILED", authoritative=True),
    ))
    assert result.confirmed_size == result.failed_size == Decimal("2")
    assert result.matched_size == Decimal("4")
    assert result.unsettled_size == 0
    assert result.terminal_states_verified
    assert verify_fill_coverage(order(), result) is None
    assert not hasattr(result, "final")


@pytest.mark.parametrize("changed", [
    {"account": "0x" + "e" * 40}, {"session_id": "session-2"}, {"order_hash": "0x" + "e" * 64},
    {"condition_id": "0x" + "e" * 64}, {"token_id": "123"}, {"price": Decimal("0.51")},
])
def test_fill_scope_and_maker_economics_cannot_cross_order_account_or_session(changed):
    with pytest.raises(AdapterContractError):
        aggregate_fills(PREPARED, (fill(**changed),))


@pytest.mark.parametrize("history", [
    (fill(), fill(size="3")),
    (fill(), fill(bucket_index=1)),
    (fill(status="CONFIRMED"), fill(status="FAILED", at=2_000)),
    (fill(status="FAILED"), fill(status="CONFIRMED", at=2_000)),
    (fill(status="CONFIRMED"), fill(status="MINED", at=2_000)),
    (fill(status="FAILED"), fill(status="RETRYING", at=2_000)),
])
def test_conflicting_trade_evidence_halts_instead_of_selecting_a_convenient_value(history):
    for sequence in (history, tuple(reversed(history))):
        with pytest.raises(AdapterContractError):
            aggregate_fills(PREPARED, sequence)


def test_terminal_event_and_match_in_same_timestamp_bucket_keep_terminal_status():
    result = aggregate_fills(PREPARED, (fill(status="CONFIRMED", authoritative=True), fill()))
    assert result.confirmed_size == 2
    assert result.terminal_states_verified


def test_fill_aggregation_is_exact_under_small_decimal_context_and_rejects_overfill():
    with localcontext() as context:
        context.prec = 2
        result = aggregate_fills(PREPARED, (fill(size="1.23456789"), fill(trade_id="trade-2", size="0.123456789")))
    assert result.matched_size == Decimal("1.358024679")
    with pytest.raises(AdapterContractError, match="exceed"):
        aggregate_fills(PREPARED, (fill(size="6"), fill(trade_id="trade-2", size="6")))


def test_raw_trade_shapes_are_not_implicitly_scaled_or_normalized():
    for raw in ({"size": "10", "status": "MATCHED"},
                {"size": "100000000", "status": "TRADE_STATUS_CONFIRMED"}):
        with pytest.raises(AdapterContractError, match="normalized"):
            aggregate_fills(PREPARED, (raw,))
    with pytest.raises(AdapterContractError, match="normalized"):
        fill(status="TRADE_STATUS_CONFIRMED")


def test_cross_account_summary_with_identical_trade_ids_cannot_satisfy_order_coverage():
    other = replace(PREPARED, account="0x" + "e" * 40)
    result = aggregate_fills(other, (replace(fill(), account=other.account),
                                    replace(fill(trade_id="trade-2"), account=other.account)))
    with pytest.raises(AdapterContractError, match="scope"):
        verify_fill_coverage(order(), result)


def test_missing_and_extra_trades_or_read_races_cannot_prove_coverage():
    with pytest.raises(AdapterContractError, match="incomplete"):
        verify_fill_coverage(order(), aggregate_fills(PREPARED, (fill(size="4"),)))
    with pytest.raises(AdapterContractError, match="incomplete"):
        verify_fill_coverage(order(), aggregate_fills(PREPARED, (fill(), fill(trade_id="other"))))
    with pytest.raises(AdapterContractError, match="disagree"):
        verify_fill_coverage(order(), aggregate_fills(PREPARED, (fill(), fill(trade_id="trade-2", size="1"))))


def test_bounded_event_input():
    with pytest.raises(AdapterContractError, match="budget"):
        aggregate_fills(PREPARED, (fill(),) * 10_001)


@pytest.mark.parametrize("changes", [
    {"bucket_index": True}, {"bucket_index": -1}, {"size": Decimal(0)},
    {"size": Decimal("NaN")}, {"price": Decimal("NaN")}, {"at": True},
    {"authoritative": "yes"},
])
def test_typed_evidence_rejects_unsafe_values(changes):
    with pytest.raises(ValueError):
        fill(**changes)
