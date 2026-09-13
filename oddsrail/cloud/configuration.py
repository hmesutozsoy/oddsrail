"""Strict validation for the guided builder, before displaying executable values.

Legacy runner configs retain their normalization path. Version 2 drafts reject
out-of-range fields instead of silently changing the user's requested limits.
"""
import math
import re

from . import runner

RANGES = {
    "fade": {"jump": (1, 90), "hours": (1, 24)},
    "settle": {"lo": (.5, .96), "hi": (.51, .969)},
    "value": {"edge": (.5, 50), "kelly": (.05, 1)},
    "momentum": {"move": (1, 90), "hours": (1, 24)},
    "mm": {"edge": (.5, 20), "shares": (5, 500)},
    "stoploss": {"pct": (1, 95)}, "takeprofit": {"pct": (1, 500)},
    "daily": {"usd": (1, 100000)}, "expo": {"usd": (1, 100000)},
    "dispute": {"score": (0, 100)}, "liquidity": {"slip": (.1, 20)},
    "watch": {"sec": (1, 60)},
}


def number(value, label, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number.")
    if not lo <= value <= hi or not math.isfinite(value):
        raise ValueError(f"{label} must be between {lo} and {hi}.")


def validate_config(raw, *, allow_draft=False):
    if not isinstance(raw, dict):
        raise ValueError("Provide an agent configuration.")
    validate_version(raw)
    mode = raw.get("mode", "paper")
    if mode == "draft" and not allow_draft:
        raise ValueError("This is a draft configuration. Trading has not been authorized.")
    if mode != "paper" and not (allow_draft and mode == "draft"):
        raise ValueError("Hosted live trading is not enabled.")
    on, vals = raw.get("on"), raw.get("vals")
    if not isinstance(on, dict) or not isinstance(vals, dict):
        raise ValueError("Choose strategies and settings.")
    if any(not isinstance(v, bool) for v in on.values()):
        raise ValueError("Strategy and risk switches must be true or false.")
    if any(k not in (*RANGES, "noadd", "report") for k in on):
        raise ValueError("An unsupported strategy or risk rule was requested.")
    if any(k not in (*RANGES, "noadd", "report") for k in vals):
        raise ValueError("An unsupported setting was requested.")
    if not any(on.get(k) for k in ("fade", "settle", "value", "momentum", "mm")):
        raise ValueError("Choose at least one strategy.")
    for key, lo, hi in (("bankroll", 10, 100000), ("perorder", 1, 500),
                        ("maxpos", 1, 50), ("closing", 0, 8760), ("minvol", 0, 1e12)):
        number(raw.get(key), key, lo, hi)
    if int(raw["maxpos"]) != raw["maxpos"]:
        raise ValueError("Open positions must be a whole number.")
    if raw["perorder"] > raw["bankroll"]:
        raise ValueError("The order limit cannot exceed allocated capital.")
    for fragment, params in RANGES.items():
        if not isinstance(vals.get(fragment, {}), dict):
            raise ValueError(f"Invalid {fragment} settings.")
        if on.get(fragment):
            allowed = (*params, "views") if fragment == "value" else params
            if any(k not in allowed for k in vals.get(fragment, {})):
                raise ValueError(f"An unsupported {fragment} setting was requested.")
            for key, (lo, hi) in params.items():
                number(vals.get(fragment, {}).get(key), f"{fragment} {key}", lo, hi)
    if on.get("dispute") and int(vals["dispute"]["score"]) != vals["dispute"]["score"]:
        raise ValueError("The dispute-risk score must be a whole number.")
    if on.get("settle") and vals["settle"]["lo"] >= vals["settle"]["hi"]:
        raise ValueError("The minimum entry price must be below the maximum.")
    if on.get("expo") and vals["expo"]["usd"] < raw["perorder"]:
        raise ValueError("The order limit cannot exceed the market exposure limit.")
    views = vals.get("value", {}).get("views", "")
    if not isinstance(views, str) or len(views) > 2000:
        raise ValueError("Forecasts must be text of at most 2,000 characters.")
    if on.get("value") and not views.strip():
        raise ValueError("Add a market and probability for your forecast strategy.")
    if on.get("value"):
        for line in views.splitlines():
            if not line.strip():
                continue
            market, sep, probability = line.rpartition(":")
            if (not sep or not market.strip() or not re.fullmatch(r"\s*(?:0?\.\d+|0\.\d+)\s*", probability)
                    or not 0 < float(probability) < 1):
                raise ValueError("Each forecast must be 'market: probability', using a decimal strictly between 0 and 1.")
    topics = raw.get("topics", [])
    if not isinstance(topics, list) or any(t not in (*runner.CATEGORIES, "all") for t in topics):
        raise ValueError("Choose supported market categories.")
    if not isinstance(raw.get("keyword", ""), str) or len(raw.get("keyword", "")) > 80:
        raise ValueError("The market keyword is too long.")
    if raw.get("market_mode", "universe") == "universe" and not topics and not raw.get("keyword", "").strip():
        raise ValueError("Choose a market universe.")
    normalized = runner.normalize(raw)
    if normalized.get("selection_error"):
        raise ValueError(normalized["selection_error"])
    return normalized


def validate_version(raw):
    version = raw.get("schema_version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError("Unsupported configuration version. Reopen the agent setup.")
