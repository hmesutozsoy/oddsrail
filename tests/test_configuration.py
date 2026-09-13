"""Reviewed limits must survive validation, with malformed intent rejected."""

from copy import deepcopy

import pytest

from oddsrail.cloud.configuration import validate_config


@pytest.fixture
def config():
    return {
        "mode": "paper", "market_mode": "universe", "market_ids": [],
        "topics": ["crypto"], "keyword": "", "bankroll": 1000, "perorder": 25,
        "maxpos": 5, "closing": 2, "minvol": 20000,
        "on": {"fade": True, "settle": False, "value": False, "momentum": False,
               "mm": False, "stoploss": True, "takeprofit": True, "daily": True,
               "noadd": True, "expo": True, "dispute": True, "liquidity": True,
               "watch": False, "report": True},
        "vals": {
            "fade": {"jump": 8, "hours": 6}, "settle": {"lo": .9, "hi": .96},
            "value": {"edge": 5, "kelly": .25, "views": "Will Bitcoin exceed $80,000?: 0.7"},
            "momentum": {"move": 10, "hours": 6}, "mm": {"edge": 2, "shares": 20},
            "stoploss": {"pct": 25}, "takeprofit": {"pct": 40}, "daily": {"usd": 30},
            "expo": {"usd": 100}, "dispute": {"score": 20}, "liquidity": {"slip": 2},
            "watch": {"sec": 20},
        },
    }


def test_validated_summary_preserves_limits_and_converts_units(config):
    original = deepcopy(config)
    normalized = validate_config(config)
    assert config == original
    assert {k: normalized[k] for k in ("bankroll", "perorder", "maxpos", "minvol")} == {
        "bankroll": 1000, "perorder": 25, "maxpos": 5, "minvol": 20000}
    assert normalized["closing_h"] == 2
    assert normalized["fade"] == {"jump": .08, "hours": 6}
    assert normalized["stoploss"] == .25
    assert normalized["takeprofit"] == .4
    assert normalized["liquidity"] == .02
    assert normalized["daily"] == 30
    assert normalized["expo"] == 100
    assert normalized["dispute"] == 20


@pytest.mark.parametrize("value", [True, False, "25", None, [], {}, float("nan"),
                                  float("inf"), -float("inf"), pytest.param(10**400, id="huge_integer")])
def test_money_field_rejects_invalid_and_nonfinite_numbers(config, value):
    config["perorder"] = value
    with pytest.raises(ValueError):
        validate_config(config)


@pytest.mark.parametrize("field,value", [
    ("bankroll", 9), ("bankroll", 100001), ("perorder", 0), ("perorder", 501),
    ("maxpos", 0), ("maxpos", 51), ("maxpos", 2.5), ("closing", -1),
    ("closing", 8761), ("minvol", -1), ("minvol", 1e12 + 1),
])
def test_core_ranges_are_rejected_not_clamped(config, field, value):
    config[field] = value
    with pytest.raises(ValueError):
        validate_config(config)


@pytest.mark.parametrize("fragment,key,value", [
    ("fade", "jump", 91), ("fade", "hours", 25), ("fade", "jump", float("nan")),
    ("settle", "hi", .97), ("value", "edge", 0), ("value", "kelly", 1.1),
    ("momentum", "hours", 0), ("mm", "shares", 4), ("mm", "edge", 21),
    ("stoploss", "pct", 0), ("takeprofit", "pct", 501), ("daily", "usd", 0),
    ("expo", "usd", float("inf")), ("dispute", "score", 20.5),
    ("liquidity", "slip", .09), ("watch", "sec", 61), ("watch", "sec", True),
])
def test_strategy_and_risk_parameters_cannot_be_silently_changed(config, fragment, key, value):
    config["on"][fragment] = True
    config["vals"][fragment][key] = value
    with pytest.raises(ValueError):
        validate_config(config)


def test_strategy_bounds_at_supported_edges_remain_exact(config):
    config["on"]["settle"] = True
    config["vals"]["settle"] = {"lo": .96, "hi": .969}
    config["vals"]["fade"] = {"jump": 1, "hours": 24}
    result = validate_config(config)
    assert result["settle"] == {"lo": .96, "hi": .969}
    assert result["fade"] == {"jump": .01, "hours": 24}


@pytest.mark.parametrize("conflict", ["bankroll", "exposure", "entry_range"])
def test_conflicting_limits_require_a_change_from_the_user(config, conflict):
    if conflict == "bankroll":
        config["bankroll"], config["perorder"] = 10, 25
    elif conflict == "exposure":
        config["vals"]["expo"]["usd"] = 10
    else:
        config["on"]["settle"] = True
        config["vals"]["settle"] = {"lo": .95, "hi": .94}
    before = deepcopy(config)
    with pytest.raises(ValueError):
        validate_config(config)
    assert config == before


@pytest.mark.parametrize("raw", [None, [], "agent", 1, True])
def test_top_level_requires_an_object(raw):
    with pytest.raises(ValueError):
        validate_config(raw)


@pytest.mark.parametrize("field,value", [
    ("on", []), ("vals", None), ("topics", "crypto"), ("topics", ["unsupported"]),
    ("topics", [{}]), ("keyword", {}), ("keyword", "x" * 81),
])
def test_malformed_nested_input_is_a_validation_error(config, field, value):
    config[field] = value
    with pytest.raises(ValueError):
        validate_config(config)


def test_switches_must_be_boolean_and_a_strategy_must_be_enabled(config):
    config["on"]["fade"] = "true"
    with pytest.raises(ValueError):
        validate_config(config)
    config["on"]["fade"] = False
    with pytest.raises(ValueError):
        validate_config(config)


def test_unknown_active_rules_cannot_be_ignored(config):
    config["on"]["trailing_stop"] = True
    with pytest.raises(ValueError):
        validate_config(config)


def test_unknown_strategy_parameter_cannot_be_ignored(config):
    config["vals"]["fade"]["trailing_stop"] = 10
    with pytest.raises(ValueError):
        validate_config(config)


def test_malformed_strategy_object_is_rejected(config):
    config["vals"]["fade"] = []
    with pytest.raises(ValueError):
        validate_config(config)


@pytest.mark.parametrize("views", [
    "", "   ", "a market without a probability", ": 0.7", "Bitcoin: maybe",
    "Bitcoin: NaN", "Bitcoin: inf", "Bitcoin: 0", "Bitcoin: 1",
    "Bitcoin: -0.1", "Bitcoin: 101%", "Bitcoin: 100%",
    "Bitcoin: 0.7\nEthereum: unknown", {"Bitcoin": .7}, "x" * 2001,
])
def test_forecast_input_cannot_silently_skip_malformed_lines(config, views):
    config["on"]["value"] = True
    config["vals"]["value"]["views"] = views
    with pytest.raises(ValueError):
        validate_config(config)


def test_forecast_text_is_retained_for_execution(config):
    config["on"]["value"] = True
    views = "Will Bitcoin exceed $80,000?: 0.7\nWill Ethereum exceed $5,000?: 0.3"
    config["vals"]["value"]["views"] = views
    assert validate_config(config)["value"]["views"] == views


@pytest.mark.parametrize("market_mode,ids", [
    ("specific", []), ("specific", ["not-a-token"]), ("specific", [123]),
    ("specific", [str(10**50)] * 21), ("specific", None), ("typo", []),
])
def test_invalid_specific_selection_is_rejected(config, market_mode, ids):
    config["market_mode"], config["market_ids"] = market_mode, ids
    with pytest.raises(ValueError):
        validate_config(config)


def test_specific_mode_preserves_exact_ids_without_requiring_categories(config):
    config["market_mode"] = "specific"
    config["market_ids"] = [str(10**50 + 1), str(10**50 + 2)]
    config["topics"] = []
    result = validate_config(config)
    assert result["market_mode"] == "specific"
    assert result["market_ids"] == config["market_ids"]


def test_empty_universe_does_not_implicitly_become_all_markets(config):
    config["topics"] = []
    with pytest.raises(ValueError):
        validate_config(config)


def test_hosted_live_cannot_be_requested_through_configuration(config):
    config["mode"] = "live"
    with pytest.raises(ValueError, match="live"):
        validate_config(config)


@pytest.mark.parametrize("version", ["2", "bad", True, 2.0, 3, None])
def test_version_cannot_silently_downgrade_budget_semantics(config, version):
    config["schema_version"] = version
    with pytest.raises(ValueError, match="version"):
        validate_config(config)


@pytest.mark.parametrize("version", [2, 1, None])
def test_drafts_require_explicit_review_context_for_every_version(config, version):
    config["mode"] = "draft"
    if version is not None:
        config["schema_version"] = version
    else:
        config.pop("schema_version", None)
    original = deepcopy(config)
    reviewed = validate_config(config, allow_draft=True)
    assert reviewed["schema_version"] == (version or 1)
    assert reviewed["bankroll"] == config["bankroll"]
    assert config == original
    with pytest.raises(ValueError, match="draft"):
        validate_config(config)


def test_review_context_does_not_enable_live_execution(config):
    config["mode"] = "live"
    with pytest.raises(ValueError, match="live"):
        validate_config(config, allow_draft=True)
