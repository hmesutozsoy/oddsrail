"""check_order: deterministic pre-trade verification of an agent's order
against the operator's stated intent, the live market, and the guardrails.

The worry it answers is "the agent hallucinated": it meant one market and
picked another, said NO and bought YES, typed 100 instead of 10, or is about
to pay through a thin book. The answer here is not a second model judging
the first one; it is a list of checks a human would run, each returning ok,
caution or block with the evidence, plus a one-line read-back of what the
order actually is. Nothing here sends anything.
"""

from __future__ import annotations

import datetime as dt
import re

_STOP = {
    "the", "a", "an", "of", "on", "in", "at", "to", "by", "for", "and", "or",
    "is", "be", "will", "buy", "sell", "yes", "no", "shares", "share", "market",
    "position", "order", "price", "size", "with", "my", "i", "this", "that",
    "than", "before", "after", "above", "below", "over", "under", "up", "down",
    "usd", "usdc", "dollars", "get", "go", "long", "short",
}


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 1 and w not in _STOP}


def intent_overlap(intent: str, *texts: str) -> float:
    """Share of the intent's content words that appear in the market's
    title/description. 1.0 = every word found; 0.0 = nothing matches."""
    want = _words(intent)
    if not want:
        return 1.0
    have = set()
    for t in texts:
        have |= _words(t)
    return len(want & have) / len(want)


def intent_outcome(intent: str) -> str | None:
    """'no' if the intent says NO, 'yes' if it says YES, else None."""
    s = (intent or "").lower()
    # "fade" is NOT a side: fading a rally means buying NO, fading a drop
    # means buying YES. Inferring a side from it blocked the flagship
    # workflow, so this function only reads words that name a side.
    has_no = re.search(r"\b(no|against)\b", s) is not None
    has_yes = re.search(r"\byes\b", s) is not None
    if has_no and not has_yes:
        return "no"
    if has_yes and not has_no:
        return "yes"
    return None


def _check(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def verdict(checks: list) -> str:
    statuses = {c["status"] for c in checks}
    if "block" in statuses:
        return "block"
    if "caution" in statuses:
        return "caution"
    return "ok"


def evaluate(order: dict, market: dict, book: dict | None, intent: str,
             guard_refusal: dict | None, now: dt.datetime | None = None) -> dict:
    """Pure: run every check on already-fetched data.

    order  : {venue, market_id, side, price, size, outcome}
    market : {title, description, outcome_of_market_id, accepting_orders,
              end_date, resolution_source, uma_status, neg_risk}
    book   : {best_bid, best_ask, walk} where walk is crossvenue.walk_book output
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    checks = []
    side, price, size = order["side"], float(order["price"]), float(order["size"])
    notional = price * size

    # 1. market exists and is open
    if not market:
        checks.append(_check("market_exists", "block",
                             f"no market found for {order['market_id']!r}; nothing to trade"))
        return {"verdict": "block", "checks": checks, "order": order}
    checks.append(_check("market_exists", "ok", f"resolved to: {market.get('title')}"))
    if market.get("accepting_orders") is False:
        checks.append(_check("market_open", "block", "the market is not accepting orders"))
    else:
        end = market.get("end_date")
        try:
            end_dt = dt.datetime.fromisoformat(str(end).replace("Z", "+00:00")) if end else None
        except ValueError:
            end_dt = None
        if end_dt and end_dt <= now:
            checks.append(_check("market_open", "block", f"the market's end date {end} has passed"))
        elif end_dt and end_dt - now < dt.timedelta(hours=1):
            checks.append(_check("market_open", "caution", f"the market ends within the hour ({end})"))
        else:
            checks.append(_check("market_open", "ok", f"accepting orders; ends {end or 'unknown'}"))

    # 2. intent matches the market and the outcome
    if intent:
        ov = intent_overlap(intent, market.get("title", ""), market.get("description", ""))
        if ov >= 0.5:
            checks.append(_check("intent_matches_market", "ok", f"{ov:.0%} of the intent's words appear in the market"))
        elif ov >= 0.25:
            checks.append(_check("intent_matches_market", "caution",
                                 f"only {ov:.0%} of the intent's words appear in the market title/description; confirm this is the market you meant"))
        else:
            checks.append(_check("intent_matches_market", "block",
                                 f"{ov:.0%} of the intent's words appear in the market; this looks like the wrong market"))
        want = intent_outcome(intent)
        have = (order.get("outcome") or market.get("outcome_of_market_id") or "").lower() or None
        if want and have and want != have:
            checks.append(_check("intent_matches_outcome", "block",
                                 f"the intent says {want.upper()} but this order is on the {have.upper()} side"))
        elif want and have:
            checks.append(_check("intent_matches_outcome", "ok", f"intent and order both say {have.upper()}"))
    else:
        checks.append(_check("intent_matches_market", "caution",
                             "no intent text supplied; pass the operator's words so the market and outcome can be checked"))

    # 3. price sanity
    if not (0 < price < 1):
        checks.append(_check("price_sane", "block", "price must be an implied probability in (0,1)"))
    else:
        bb = book.get("best_bid") if book else None
        ba = book.get("best_ask") if book else None
        bb = float(bb) if bb is not None else None
        ba = float(ba) if ba is not None else None
        if side == "BUY" and ba is not None and price > ba + 0.05:
            checks.append(_check("price_sane", "caution",
                                 f"BUY limit {price} is {price - ba:.3f} through the best ask {ba}; you would pay up to {price}"))
        elif side == "SELL" and bb is not None and price < bb - 0.05:
            checks.append(_check("price_sane", "caution",
                                 f"SELL limit {price} is {bb - price:.3f} below the best bid {bb}"))
        elif side == "BUY" and price >= 0.97:
            checks.append(_check("price_sane", "caution",
                                 f"buying at {price}: at most {1 - price:.3f} upside per share before fees"))
        else:
            checks.append(_check("price_sane", "ok",
                                 f"limit {price} vs book bid {bb} / ask {ba}"))

    # 4. size and notional
    if size <= 0:
        checks.append(_check("size_sane", "block", "size must be positive"))
    elif order.get("venue") == "polymarket" and notional < 1.0 and (
            (side == "BUY" and book and book.get("best_ask") is not None and price >= float(book["best_ask"]))
            or (side == "SELL" and book and book.get("best_bid") is not None and price <= float(book["best_bid"]))):
        checks.append(_check("size_sane", "block",
                             f"notional ${notional:.2f} is under Polymarket's $1 minimum for marketable orders"))
    else:
        checks.append(_check("size_sane", "ok", f"{size:g} shares, notional ${notional:.2f}"))

    # 5. guardrails
    if guard_refusal:
        checks.append(_check("guardrails", "block",
                             f"{guard_refusal.get('rule')}: limit {guard_refusal.get('limit')}, requested {guard_refusal.get('requested')}"))
    else:
        checks.append(_check("guardrails", "ok", "inside the operator's limits"))

    # 6. cost of taking, if the order would take
    walk = (book or {}).get("walk")
    if walk:
        if walk.get("filled_size", 0) and not walk.get("fillable", True):
            checks.append(_check("liquidity", "caution",
                                 f"only {walk.get('filled_size')} of {size:g} shares are available within the limit"))
        elif walk.get("slippage_vs_best") and float(walk["slippage_vs_best"]) > 0.02:
            checks.append(_check("liquidity", "caution",
                                 f"taking {size:g} shares walks the book {float(walk['slippage_vs_best']):.3f} past the best level (avg {walk.get('avg_price')})"))
        else:
            checks.append(_check("liquidity", "ok",
                                 f"avg fill {walk.get('avg_price')} for {size:g} shares if taken now"))

    # 7. resolution
    src = market.get("resolution_source")
    if not src or src == "(none named)":
        checks.append(_check("resolution", "caution",
                             "no resolution source named; read the description before trusting the price"))
    else:
        checks.append(_check("resolution", "ok", f"resolves per: {str(src)[:120]}"))
    if market.get("uma_status") and "dispute" in str(market["uma_status"]).lower():
        checks.append(_check("resolution", "caution", f"UMA status: {market['uma_status']}"))

    outcome = (order.get("outcome") or market.get("outcome_of_market_id") or "?").upper()
    read_back = (f"{side} {size:g} {outcome} on \"{market.get('title')}\" at {price} "
                 f"(notional ${notional:.2f}); ends {market.get('end_date') or 'unknown'}")
    return {"verdict": verdict(checks), "read_back": read_back, "checks": checks, "order": order}


# ------------------------------ orchestration ------------------------------ #

async def check_order(venue: str, market_id: str, side: str, price: float,
                      size: float, intent: str = "", outcome: str = "") -> dict:
    """Fetch what the checks need and run them. Never sends anything."""
    from . import crossvenue as xv
    from . import guard
    v = str(venue or "").lower()
    sd = str(side or "").upper()
    if sd not in ("BUY", "SELL"):
        return {"verdict": "block", "checks": [_check("input", "block", "side must be BUY or SELL")]}
    try:
        price, size = float(price), float(size)
    except (TypeError, ValueError):
        return {"verdict": "block", "checks": [_check("input", "block", "price and size must be numbers")]}
    order = {"venue": v, "market_id": market_id, "side": sd, "price": price,
             "size": size, "outcome": (outcome or "").lower() or None}
    market, book = None, None

    if v == "polymarket":
        from . import polymarket as pm
        try:
            m = await pm.get_market_by_token(market_id)
        except Exception:
            m = None
        if m:
            outs = m.get("outcomes") or {}
            side_of = None
            for k in ("yes", "no"):
                if str((outs.get(k) or {}).get("token_id")) == str(market_id):
                    side_of = k
            market = {"title": m.get("question"), "description": "",
                      "outcome_of_market_id": side_of,
                      "accepting_orders": m.get("accepting_orders"),
                      "end_date": m.get("end_date"),
                      "resolution_source": m.get("resolution_source"),
                      "uma_status": m.get("uma_resolution_status"),
                      "neg_risk": m.get("neg_risk")}
            try:
                rc = await pm.resolution_criteria(m.get("slug") or m.get("id"))
                market["description"] = rc.get("description") or ""
                market["resolution_source"] = rc.get("resolution_source") or market["resolution_source"]
            except Exception:
                pass
            try:
                ob = await pm.get_orderbook(market_id)
                levels = ob["asks"] if sd == "BUY" else ob["bids"]
                within = [l for l in levels if (float(l["price"]) <= price if sd == "BUY" else float(l["price"]) >= price)]
                book = {"best_bid": ob.get("best_bid"), "best_ask": ob.get("best_ask"),
                        "walk": xv.walk_book(within, size)}
            except Exception:
                book = None
    elif v == "kalshi":
        from . import kalshi as kx
        try:
            m = await kx.get_market(market_id)
        except Exception:
            m = None
        if m:
            want = (outcome or "yes").lower()
            market = {"title": m.get("title") or m.get("event_title"),
                      "description": m.get("rules_primary") or "",
                      "outcome_of_market_id": want,
                      "accepting_orders": (m.get("status") in (None, "open", "active")),
                      "end_date": m.get("close_time"),
                      "resolution_source": None, "uma_status": None}
            try:
                rc = await kx.resolution_criteria(market_id)
                srcs = rc.get("settlement_sources") or []
                market["resolution_source"] = ", ".join(
                    str(s.get("name") or s.get("url") or s) for s in srcs) or None
            except Exception:
                pass
            yb, ya = m.get("yes_bid"), m.get("yes_ask")
            try:
                if want == "no" and yb is not None and ya is not None:
                    book = {"best_bid": round(1 - float(ya), 4), "best_ask": round(1 - float(yb), 4)}
                else:
                    book = {"best_bid": yb, "best_ask": ya}
            except (TypeError, ValueError):
                book = None
    else:
        return {"verdict": "block", "checks": [_check("input", "block", "venue must be 'polymarket' or 'kalshi'")]}

    refusal = guard.check_order(v, market_id, price * size, dry=True)
    out = evaluate(order, market, book, intent, refusal)
    from .trading import dry_run
    out["dry_run"] = dry_run()
    out["note"] = ("deterministic checks only; no model judged this. 'block' means do not "
                   "place the order as written; 'caution' means read the detail to the "
                   "operator before placing. Nothing was sent.")
    return out
