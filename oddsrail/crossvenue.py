"""Cross-venue layer: one vocabulary over Polymarket and Kalshi.

Without this, "cross-venue" is a marketing claim — the two tool families speak
incompatible dialects (token ids vs tickers, probability strings vs dollar
strings, best-first vs raw ladders), so an agent has to know which venue it
wants before it can ask anything, and cannot compare a price across them at
all. Everything here returns the SAME shape regardless of venue, with the
venue-specific identifier an agent needs in order to actually trade.

Honesty rules this module follows:
  * Prices are floats in (0,1) — implied probability — on both venues.
  * Cost is computed by WALKING THE BOOK, not from the top level, so the
    number reflects what a given size would really pay.
  * Fees are reported only where the venue exposes a schedule. Polymarket
    carries a per-market fee_schedule (often disabled); Kalshi does not
    publish one in its market payload, so it is reported as unknown rather
    than guessed. A wrong fee number is worse than an absent one.
  * Matching the "same" event across venues is fuzzy and is presented as
    CANDIDATES with a similarity score, never as a settled fact.
"""

from __future__ import annotations

import re
from decimal import Decimal


def _f(v):
    """Anything -> float, or None. Both venues return numbers as strings."""
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def unify_polymarket(m: dict) -> dict:
    """A slim_market() dict from polymarket.py -> the shared shape."""
    outs = m.get("outcomes") or {}
    yes = (outs.get("yes") or {}) if isinstance(outs, dict) else {}
    no = (outs.get("no") or {}) if isinstance(outs, dict) else {}
    yes_px = _f(yes.get("price"))
    return {
        "venue": "polymarket",
        "market_id": yes.get("token_id"),      # what place_order() takes
        "title": m.get("question"),
        "yes_price": yes_px,
        "no_price": _f(no.get("price")) if no.get("price") is not None
                    else (round(1 - yes_px, 4) if yes_px is not None else None),
        "best_bid": _f(m.get("best_bid")),
        "best_ask": _f(m.get("best_ask")),
        "spread": _f(m.get("spread")),
        "volume_24h": _f(m.get("volume_24hr")),
        "liquidity": _f(m.get("liquidity")),
        "close_time": m.get("end_date"),
        "open": bool(m.get("accepting_orders")) and not bool(m.get("closed")),
        "trade_with": "place_order(token_id=market_id, ...)",
        "venue_ref": {"slug": m.get("slug"), "condition_id": m.get("condition_id"),
                      "no_token_id": no.get("token_id")},
    }


def unify_kalshi(m: dict) -> dict:
    """A slim_market() dict from kalshi.py -> the shared shape."""
    return {
        "venue": "kalshi",
        "market_id": m.get("ticker"),          # what kalshi_place_order() takes
        "title": m.get("title"),
        "yes_price": _f(m.get("last_price")) or _f(m.get("yes_bid")),
        "no_price": _f(m.get("no_bid")),
        "best_bid": _f(m.get("yes_bid")),
        "best_ask": _f(m.get("yes_ask")),
        "spread": (round(_f(m.get("yes_ask")) - _f(m.get("yes_bid")), 4)
                   if _f(m.get("yes_ask")) is not None
                   and _f(m.get("yes_bid")) is not None else None),
        "volume_24h": _f(m.get("volume_24h")),
        "liquidity": None,                      # Kalshi publishes no equivalent
        "close_time": m.get("close_time"),
        "open": str(m.get("status", "")).lower() in ("active", "open"),
        "trade_with": "kalshi_place_order(ticker=market_id, ...)",
        "venue_ref": {"event_ticker": m.get("event_ticker"),
                      "yes_sub_title": m.get("yes_sub_title")},
    }


# --------------------------------------------------------------------------- #
# Fuzzy cross-venue matching                                                   #
# --------------------------------------------------------------------------- #

_STOP = {"will", "the", "be", "a", "an", "of", "in", "on", "by", "to", "is",
         "for", "at", "before", "after", "and", "or", "any", "this", "that",
         "there", "it", "as", "than", "then", "who", "what", "when"}


def _tokens(title: str) -> set:
    words = re.findall(r"[a-z0-9$]+", str(title or "").lower())
    return {w for w in words if w not in _STOP and len(w) > 1}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap on content words. Crude on purpose — it is a triage
    signal for a human/agent to confirm, not an entity-resolution claim."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return round(len(ta & tb) / len(ta | tb), 3)


def _close_days_apart(a: dict, b: dict):
    """Days between two markets' close times, or None if either is missing.

    Two listings of the SAME event resolve at about the same time. Title words
    alone cannot tell "Brazilian presidential election" from "Ukraine holds an
    election" — close dates can.
    """
    from datetime import datetime

    def parse(v):
        if not v:
            return None
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:
            return None
    da, db = parse(a.get("close_time")), parse(b.get("close_time"))
    if da is None or db is None:
        return None
    return abs((da - db).days)


def pair_across_venues(unified: list, min_similarity: float = 0.5,
                       max_close_days_apart: int = 7) -> list:
    """Candidate SAME-EVENT listings across venues, deliberately conservative.

    This is NOT an arbitrage scanner and must not be used as one. Matching a
    market across venues is an entity-resolution problem: the venues word
    things differently, and a naive title overlap happily pairs a Brazilian
    election with a Ukrainian one and reports a 70-point "gap" that is pure
    fiction. Acting on that would lose money confidently.

    Two gates apply and both must pass:
      1. title similarity >= min_similarity (default 0.5, strict)
      2. close dates within max_close_days_apart, when both venues publish one

    Even then the row is a CANDIDATE, and the price delta is reported as
    `yes_price_difference` rather than as profit: identical wording still does
    not guarantee identical resolution criteria, and settlement source is
    exactly where these two venues differ most.
    """
    pm_side = [m for m in unified if m["venue"] == "polymarket"]
    kx_side = [m for m in unified if m["venue"] == "kalshi"]
    out = []
    for a in pm_side:
        for b in kx_side:
            sim = similarity(a["title"], b["title"])
            if sim < min_similarity:
                continue
            days = _close_days_apart(a, b)
            if days is not None and days > max_close_days_apart:
                continue
            diff = None
            if a["yes_price"] is not None and b["yes_price"] is not None:
                diff = round(b["yes_price"] - a["yes_price"], 4)
            out.append({
                "title_similarity": sim,
                "close_dates_days_apart": days,
                "date_check": ("both venues publish a close date and they agree"
                               if days is not None else
                               "one venue published no close date — UNVERIFIED"),
                "yes_price_difference": diff,
                "polymarket": {"title": a["title"], "market_id": a["market_id"],
                               "yes_price": a["yes_price"],
                               "close_time": a["close_time"],
                               "best_bid": a["best_bid"], "best_ask": a["best_ask"]},
                "kalshi": {"title": b["title"], "market_id": b["market_id"],
                           "yes_price": b["yes_price"],
                           "close_time": b["close_time"],
                           "best_bid": b["best_bid"], "best_ask": b["best_ask"]},
                "before_trading_this": [
                    "read both markets' resolution criteria — same question with "
                    "a different settlement source is the normal case here",
                    "run quote_cost on BOTH legs; book-walk cost routinely "
                    "exceeds the headline difference",
                    "Kalshi fees are not published in its market payload and are "
                    "not included anywhere in this output",
                ],
            })
    out.sort(key=lambda x: (-x["title_similarity"],
                            -(abs(x["yes_price_difference"])
                              if x["yes_price_difference"] is not None else 0)))
    return out

# --------------------------------------------------------------------------- #
# Cost of actually filling a size (book walk)                                  #
# --------------------------------------------------------------------------- #

def walk_book(levels: list, size: float, price_key: str = "price",
              size_key: str = "size") -> dict:
    """Consume `size` shares from a best-first ladder.

    Returns the true average price, not the top-of-book price. The gap
    between them is the slippage an agent pays for asking, and it is the
    number that decides whether a cross-venue gap is real profit or not.
    """
    remaining = Decimal(str(size))
    spent = Decimal("0")
    filled = Decimal("0")
    consumed = []
    for lvl in levels or []:
        px = _f(lvl.get(price_key))
        av = _f(lvl.get(size_key))
        if px is None or av is None or av <= 0:
            continue
        take = min(remaining, Decimal(str(av)))
        if take <= 0:
            break
        spent += take * Decimal(str(px))
        filled += take
        consumed.append({"price": px, "size": float(take)})
        remaining -= take
        if remaining <= 0:
            break

    if filled <= 0:
        return {"fillable": False, "reason": "no depth on that side of the book",
                "requested_size": size}
    best = _f((levels or [{}])[0].get(price_key))
    avg = float(spent / filled)
    return {
        "fillable": remaining <= 0,
        "requested_size": size,
        "filled_size": float(filled),
        "unfilled_size": float(remaining),
        "best_price": best,
        "avg_price": round(avg, 6),
        "worst_price": consumed[-1]["price"] if consumed else None,
        "notional": round(float(spent), 4),
        "slippage_vs_best": (round(avg - best, 6) if best is not None else None),
        "levels_consumed": consumed,
    }


def polymarket_fee_note(market_full: dict) -> dict:
    """Polymarket publishes a per-market fee schedule; report it verbatim."""
    tr = (market_full or {}).get("trading") or {}
    return {
        "fees_enabled": tr.get("fees_enabled"),
        "fee_type": tr.get("fee_type"),
        "fee_schedule": tr.get("fee_schedule"),
        "note": ("taken from the market's own trading.fee_schedule; when "
                 "fees_enabled is false this market charges no exchange fee"),
    }


KALSHI_FEE_NOTE = {
    "fee_schedule": None,
    "note": ("Kalshi does not publish a fee schedule in its market payload, so "
             "oddsrail does not estimate one — check current Kalshi fee terms "
             "before sizing on a thin edge. Book-walk cost above is exact; the "
             "fee is the only unknown."),
}
