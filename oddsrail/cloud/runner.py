"""Deterministic strategy runner: one paper pass for a builder configuration.

The builder's switches are rules with tools underneath, so a pass needs no
model: scan the universe, compute the signal each strategy names, size the
order, run check_order, paper-fill against the live book, and apply the risk
rules to what is already held. Two agents with the same switches behave the
same, which is what a fair board needs, and a pass costs nothing but a few
public API calls.

Everything an order goes through here is the same code the MCP tools use:
polymarket.py for data, signals.py, audit.kelly_size, crossvenue.walk_book,
check.check_order and paper.simulate_polymarket. The run record lists every
decision with the check verdict, so a pass can be read like a transcript.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import time
from pathlib import Path

from .. import audit as au
from .. import check as ck
from .. import crossvenue as xv
from .. import paper
from .. import polymarket as pm
from .. import signals
from .arena import FORCED_LEDGER

MAX_CANDIDATES = 15
MAX_CANDIDATES_MULTI = 20    # when several categories are switched on
SCAN_TOPIC = 40              # search results read for a keyword before the filters
SCAN_TAG = 30                # volume-ordered markets read per tag
SCAN_ALL = 60                # volume-ordered markets read for the whole venue
MAX_NEW_ORDERS = 6
ALL_TOPICS = ("", "all", "any", "all markets", "everything", "*")

# Market categories a builder can switch on, as Polymarket tag ids (from the
# venue's own tag list) plus search queries where no tag covers the ground.
CATEGORIES = {
    "crypto":      {"label": "crypto", "tags": [21, 235]},
    "politics":    {"label": "politics", "tags": [2, 144]},
    "geopolitics": {"label": "geopolitics", "tags": [100265, 101970]},
    "economy":     {"label": "economy and the Fed", "tags": [100328, 159, 100196]},
    "business":    {"label": "business and finance", "tags": [107, 120, 101031]},
    "soccer":      {"label": "football (soccer)", "tags": [100350]},
    "esports":     {"label": "esports", "tags": [64]},
    "tennis":      {"label": "tennis", "tags": [864]},
    "us-sports":   {"label": "US sports", "tags": [100381], "queries": ["nfl", "nba"]},
    "motorsport":  {"label": "motorsport", "tags": [435]},
}
MIN_NOTIONAL = 1.05          # the exchange's $1 minimum on marketable orders, with slack

# check_order cautions the runner may accept, and the switch that makes each
# one binding. A caution tells an agent to read something before placing; the
# runner cannot read, so it either enforces the rule the user switched on or
# records the caution and proceeds. Everything else that is not "ok" stops it.
ADVISORY = {"resolution": "dispute", "liquidity": "liquidity"}
_locks: dict[str, asyncio.Lock] = {}


def lock_for(path: Path) -> asyncio.Lock:
    key = str(path)
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


# --------------------------------------------------------------------------- #
# config                                                                      #
# --------------------------------------------------------------------------- #

def _f(v, default: float) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def normalize(raw: dict) -> dict:
    """The builder's config JSON, coerced and bounded."""
    raw = raw or {}
    on = {k: bool(v) for k, v in (raw.get("on") or {}).items()}
    vals = raw.get("vals") or {}

    def val(frag, key, default):
        return _f((vals.get(frag) or {}).get(key), default)

    topics_raw = raw.get("topics")
    if isinstance(topics_raw, str):
        topics_raw = [t.strip() for t in topics_raw.split(",")]
    topics = [str(t).strip().lower() for t in (topics_raw or []) if str(t).strip()]
    topics = [t for t in topics if t in CATEGORIES or t == "all"]
    keyword = str(raw.get("keyword") or "").strip()[:80]
    legacy = str(raw.get("topic") or "").strip()
    if not topics and not keyword and legacy and legacy.lower() not in ALL_TOPICS:
        keyword = legacy[:80]                      # older configs: a typed topic
    if not topics and not keyword:
        topics = ["all"]
    label = "all markets" if "all" in topics else ", ".join(topics)
    if keyword:
        label = f"{label} + {keyword}" if label else keyword
    cfg = {
        "topics": topics, "keyword": keyword, "topic": label,
        "minvol": max(0.0, _f(raw.get("minvol"), 20000)),
        "bankroll": min(max(10.0, _f(raw.get("bankroll"), 1000)), 100000.0),
        "perorder": min(max(1.0, _f(raw.get("perorder"), 25)), 500.0),
        "maxpos": int(min(max(1, _f(raw.get("maxpos"), 5)), 50)),
        "closing_h": max(0.0, _f(raw.get("closing"), 2)),
        "on": on,
        "fade": {"jump": clamp(val("fade", "jump", 8), 1, 90) / 100.0,
                 "hours": clamp(val("fade", "hours", 6), 1, 24)},
        "settle": {"lo": clamp(val("settle", "lo", 0.9), 0.5, 0.96), "hi": clamp(val("settle", "hi", 0.97), 0.51, 0.969)},
        "value": {"edge": clamp(val("value", "edge", 5), 0.5, 50) / 100.0,
                  "kelly": clamp(val("value", "kelly", 0.25), 0.05, 1.0),
                  "views": str((vals.get("value") or {}).get("views") or "")[:2000]},
        "momentum": {"move": clamp(val("momentum", "move", 10), 1, 90) / 100.0,
                     "hours": clamp(val("momentum", "hours", 6), 1, 24)},
        "mm": {"edge": clamp(val("mm", "edge", 2), 0.5, 20) / 100.0, "shares": clamp(val("mm", "shares", 20), 5, 500)},
        "stoploss": clamp(val("stoploss", "pct", 25), 1, 95) / 100.0,
        "takeprofit": clamp(val("takeprofit", "pct", 40), 1, 500) / 100.0,
        "daily": clamp(val("daily", "usd", 50), 1, 100000),
        "expo": clamp(val("expo", "usd", 50), 1, 100000),
        "dispute": int(clamp(val("dispute", "score", 30), 0, 100)),
        "liquidity": clamp(val("liquidity", "slip", 2), 0.1, 20) / 100.0,
        "watch": clamp(val("watch", "sec", 20), 1, 60),
    }
    if cfg["settle"]["lo"] >= cfg["settle"]["hi"]:
        cfg["settle"]["lo"] = min(cfg["settle"]["lo"], cfg["settle"]["hi"] - 0.01)
    return cfg


def clamp(x: float, lo: float, hi: float) -> float:
    return min(max(float(x), lo), hi)


# --------------------------------------------------------------------------- #
# the pass                                                                    #
# --------------------------------------------------------------------------- #

class Pass:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.t0 = time.time()
        self.steps: list[str] = []
        self.decisions: list[dict] = []
        self.universe: dict = {}
        self.new_orders = 0
        self.halted: str | None = None
        self.positions: dict = {}        # token_id -> row from paper.positions()
        self.cost_by_market: dict = {}   # market title -> cost of positions held or bought
        self.taken: dict = {}            # market title -> outcome bought this pass
        self.outcome_of: dict = {}       # token_id -> "yes" | "no", looked up once

    def log(self, msg: str) -> None:
        self.steps.append(f"{time.time() - self.t0:5.1f}s {msg}")

    def decide(self, **d) -> dict:
        d.setdefault("time", time.time())
        self.decisions.append(d)
        return d


def _hours_until(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return (t - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600.0
    except ValueError:
        return None


def _ok_candidate(p: Pass, m: dict) -> str | None:
    """Universe filters. Returns the reason it fails, or None."""
    c = p.cfg
    if not m.get("open"):
        return "closed"
    if m.get("market_id") is None or m.get("yes_price") is None:
        return "no tradable token"
    if not (0.02 < float(m["yes_price"]) < 0.98):
        return "price at the edge"
    if (m.get("volume_24h") or 0) < c["minvol"]:
        return f"24h volume below ${c['minvol']:,.0f}"
    h = _hours_until(m.get("close_time"))
    if h is not None and h <= c["closing_h"]:
        return f"closes within {c['closing_h']:g}h"
    return None


async def _sources(p: Pass, closing_hours: float | None) -> tuple[list[dict], list[str]]:
    """Raw market lists for the switched-on categories and keyword, merged
    and de-duplicated, most traded first. Returns (markets, failures)."""
    c = p.cfg
    jobs: list[tuple[str, object]] = []
    if closing_hours:
        jobs.append(("closing-soon", pm.closing_soon(hours=closing_hours, limit=20)))
    else:
        if "all" in c["topics"]:
            jobs.append(("all markets", pm.top_markets(SCAN_ALL)))
        for key in c["topics"]:
            cat = CATEGORIES.get(key)
            if not cat:
                continue
            for tid in cat.get("tags", []):
                jobs.append((f"{key}#{tid}", pm.markets_by_tag(tid, SCAN_TAG)))
            for q in cat.get("queries", []):
                jobs.append((f"{key}:{q}", pm.search_markets(query=q, limit=SCAN_TAG)))
        if c["keyword"]:
            jobs.append((f"keyword {c['keyword']!r}", pm.search_markets(query=c["keyword"], limit=SCAN_TOPIC)))
    results = await asyncio.gather(*(j[1] for j in jobs), return_exceptions=True)
    seen, merged, failures = set(), [], []
    for (name, _), res in zip(jobs, results):
        if isinstance(res, BaseException):
            failures.append(f"{name}: {type(res).__name__}")
            continue
        for m in res:
            key = ((m.get("outcomes") or {}).get("yes") or {}).get("token_id") or m.get("id")
            if key in seen:
                continue
            seen.add(key)
            merged.append(m)
    merged.sort(key=lambda m: -float(m.get("volume_24hr") or 0))
    return merged, failures


async def _universe(p: Pass, query: str = "", closing_hours: float | None = None) -> list[dict]:
    """Unified, filtered candidates for the switched-on categories (or the
    closing-soon list)."""
    c = p.cfg
    raw, failures = await _sources(p, closing_hours)
    if closing_hours and c["keyword"]:
        raw = [m for m in raw if xv.similarity(m.get("question") or "", c["keyword"]) > 0] or raw
    label = "closing-soon" if closing_hours else c["topic"]
    if not raw and failures:
        p.log(f"universe {label}: scan failed ({'; '.join(failures)})")
        if not closing_hours:
            p.universe = {"query": c["topic"], "topics": c["topics"], "keyword": c["keyword"],
                          "scanned": 0, "candidates": 0, "error": "; ".join(failures)}
        return []
    out, dropped = [], {}
    for m in raw:
        u = xv.unify_polymarket(m)
        why = _ok_candidate(p, u)
        if why:
            dropped[why] = dropped.get(why, 0) + 1
            continue
        out.append(u)
    cap = MAX_CANDIDATES_MULTI if len(c["topics"]) + bool(c["keyword"]) > 1 else MAX_CANDIDATES
    p.log(f"universe {label}: {len(raw)} scanned, {len(out)} candidates"
          + (f", dropped {dropped}" if dropped else "") + (f", failed {failures}" if failures else ""))
    if not closing_hours:
        p.universe = {"query": c["topic"], "topics": c["topics"], "keyword": c["keyword"],
                      "scanned": len(raw), "candidates": min(len(out), cap), "dropped": dropped}
        if failures:
            p.universe["partial"] = failures
    return out[:cap]


def _tokens(m: dict) -> tuple[str | None, str | None]:
    return m.get("market_id"), (m.get("venue_ref") or {}).get("no_token_id")


async def _book(token_id: str) -> dict | None:
    try:
        ob = await pm.get_orderbook(token_id)
    except Exception:
        return None
    bid = ob.get("best_bid"); ask = ob.get("best_ask")
    return {"bid": float(bid) if bid is not None else None, "ask": float(ask) if ask is not None else None,
            "bids": ob.get("bids") or [], "asks": ob.get("asks") or []}


def _price_to_cover(levels: list, size: float) -> float | None:
    """The price of the deepest level needed to fill `size` on one side of a
    best-first book, or None when the side is too thin."""
    left = float(size)
    for lvl in levels:
        try:
            px, sz = float(lvl.get("price")), float(lvl.get("size"))
        except (TypeError, ValueError, AttributeError):
            continue
        left -= sz
        if left <= 1e-9:
            return px
    return None


async def _hygiene(p: Pass, m: dict, token: str, side: str, price: float, size: float,
                   resting: bool = False) -> tuple[str | None, float]:
    """Dispute, fill-quality and watch checks. Returns (skip reason or None,
    limit price to use). The fill check walks the whole side of the book, so
    an order that needs deeper levels gets a limit that covers them, and one
    that would pay more than the allowance is refused. A resting quote is
    meant not to fill now, so the fill checks do not apply to it; the dispute
    check still does."""
    c = p.cfg
    limit = price
    slug = (m.get("venue_ref") or {}).get("slug")
    if c["on"].get("dispute") and slug:
        try:
            full = await pm.get_market(slug, full=True)
            flat = dict(full)
            res = full.get("resolution") or {}
            if isinstance(res, dict):
                flat.setdefault("umaResolutionStatus", res.get("uma_resolution_status") or res.get("status"))
            score = int(signals.dispute_risk(flat).get("score", 0))
            source = (res.get("source") if isinstance(res, dict) else None)
            if not source:
                return "no resolution source named", limit
            if score > c["dispute"]:
                return f"dispute_risk {score} above {c['dispute']}", limit
        except Exception as e:
            return f"dispute check failed: {type(e).__name__}", limit
    if not resting and (c["on"].get("liquidity") or c["on"].get("watch")):
        book = await _book(token)
        if not book:
            return "no order book", limit
        levels = book["asks"] if side == "BUY" else book["bids"]
        if c["on"].get("liquidity"):
            walk = xv.walk_book(levels, size)
            if not walk.get("fillable"):
                return "book cannot fill the size", limit
            best = book["ask"] if side == "BUY" else book["bid"]
            slip = abs(float(walk.get("slippage_vs_best") or 0))
            if best and slip / best > c["liquidity"]:
                return f"slippage {slip:.3f} above {c['liquidity']:.1%} of best {best}", limit
            worst = _price_to_cover(levels, size)
            if worst is None:
                return "book cannot fill the size", limit
            if best is not None and abs(worst - best) > 0.049:
                return f"filling {size:g} shares would need a limit more than 5 points through the book", limit
            # The limit must reach the deepest level the fill touches, not the average.
            limit = round(max(price, worst), 4) if side == "BUY" else round(min(price, worst), 4)
        if c["on"].get("watch"):
            await asyncio.sleep(c["watch"])
            again = await _book(token)
            if again:
                if side == "BUY" and again["ask"] is not None and book["ask"] is not None \
                        and again["ask"] > book["ask"] + 0.01:
                    return f"book moved against while watching (ask {book['ask']} -> {again['ask']})", limit
                if side == "SELL" and again["bid"] is not None and book["bid"] is not None \
                        and again["bid"] < book["bid"] - 0.01:
                    return f"book moved against while watching (bid {book['bid']} -> {again['bid']})", limit
    return None, limit


async def _order(p: Pass, m: dict, token: str, outcome: str, side: str, price: float,
                 notional_usd: float, strategy: str, why: str, resting: bool = False) -> dict:
    """Size, cap, check and paper-place one order; always returns a decision."""
    c = p.cfg
    title = m.get("title") or ""
    base = dict(strategy=strategy, market=title, slug=(m.get("venue_ref") or {}).get("slug"),
                token_id=token, outcome=outcome.upper(), side=side, price=round(price, 4), why=why)
    held = p.positions.get(token)
    if side == "BUY":
        # Entries are rationed: the loss limit, the pass cap, positions cap,
        # exposure cap and the no-add rule all apply. Exits never are.
        if p.halted:
            return p.decide(**base, result="skipped", detail=p.halted)
        if p.new_orders >= MAX_NEW_ORDERS:
            return p.decide(**base, result="skipped", detail=f"pass limit of {MAX_NEW_ORDERS} new orders")
        other = p.taken.get(title)
        if other and other != outcome.lower():
            return p.decide(**base, result="skipped",
                            detail=f"another piece already bought {other.upper()} on this market this pass")
        if not held and len(p.positions) >= c["maxpos"]:
            return p.decide(**base, result="skipped", detail=f"max open positions ({c['maxpos']}) reached")
        if held and c["on"].get("noadd") and held.get("mark") is not None and held["mark"] < held["avg_cost"]:
            return p.decide(**base, result="skipped", detail="never add to a loser: mark below entry")
        notional = min(notional_usd, c["perorder"])
        if c["on"].get("expo"):
            spent = p.cost_by_market.get(title, 0.0)
            if spent + notional > c["expo"]:
                notional = c["expo"] - spent
                if notional <= 0:
                    return p.decide(**base, result="skipped",
                                    detail=f"exposure cap ${c['expo']:g} reached for this market (${spent:.2f} held)")
        if not resting and notional < MIN_NOTIONAL:
            if c["perorder"] >= MIN_NOTIONAL:
                notional = MIN_NOTIONAL
            else:
                return p.decide(**base, result="skipped", detail="under the $1 exchange minimum")
        size = math.floor(notional / price * 100) / 100
    else:
        size = float(held["size"]) if held else 0.0
        if size <= 0:
            return p.decide(**base, result="skipped", detail="nothing held to sell")
    if size <= 0:
        return p.decide(**base, result="skipped", detail="size rounds to zero")
    base["size"] = size
    base["notional"] = round(size * price, 2)

    skip, price = await _hygiene(p, m, token, side, price, size, resting=resting)
    if skip:
        return p.decide(**base, result="skipped", detail=skip)
    base["price"] = round(price, 4)
    base["notional"] = round(size * price, 2)

    # The intent is what check_order compares with the market: the plain
    # order in words. The strategy's reasoning stays in the decision record.
    intent = f"{'buy' if side == 'BUY' else 'sell'} {outcome.upper()} on {title}"
    try:
        chk = await ck.check_order("polymarket", token, side, price, size, intent, outcome)
    except Exception as e:
        return p.decide(**base, result="skipped", detail=f"check_order failed: {type(e).__name__}: {e}")
    base["verdict"] = chk.get("verdict")
    binding, accepted = [], []
    for x in chk.get("checks", []):
        st = x.get("status")
        if st == "ok":
            continue
        name = x.get("check")
        switch = ADVISORY.get(name)
        # check_order's liquidity caution is advisory here: when the fill-quality
        # switch is on the runner has already walked the book with the user's
        # own allowance, and when it is off the user chose not to gate on it.
        advisory = (st == "caution" and switch is not None
                    and (name == "liquidity" or not c["on"].get(switch)))
        (accepted if advisory else binding).append(f"{name}: {x.get('detail', '')}")
    if binding:
        return p.decide(**base, result="skipped", detail="; ".join(binding))
    if accepted:
        base["caution_accepted"] = accepted

    try:
        fill = await paper.simulate_polymarket(token, side, price, size)
    except Exception as e:
        return p.decide(**base, result="skipped", detail=f"paper fill failed: {type(e).__name__}: {e}")
    p.new_orders += 1
    if fill.get("refused"):
        return p.decide(**base, result="refused", detail=fill["refused"])
    filled = float(fill.get("filled_size") or 0)
    result = "filled" if filled >= size - 1e-9 else "partial" if filled > 0 else "resting"
    if side == "BUY":
        p.taken[title] = outcome.lower()
    if side == "BUY" and filled > 0:
        p.cost_by_market[title] = p.cost_by_market.get(title, 0.0) + filled * float(fill.get("avg_price") or price)
        p.positions.setdefault(token, {"size": 0.0, "avg_cost": price, "mark": None})
        p.positions[token]["size"] += filled
    return p.decide(**base, result=result, filled=filled, avg_price=fill.get("avg_price"),
                    resting=fill.get("resting_size"), paper_order_id=fill.get("paper_order_id"),
                    cash_after=fill.get("cash_after"))


# ------------------------------- strategies -------------------------------- #

async def _fade(p: Pass, cands: list[dict]) -> None:
    c = p.cfg["fade"]
    checked = quiet = 0
    for m in cands:
        yes, no = _tokens(m)
        try:
            times, prices = await pm.price_history(yes, c["hours"], 1)
            rep = signals.overshoot_report(times, prices, lookback=60.0, threshold=c["jump"])
        except Exception as e:
            p.log(f"fade: history failed for {m.get('title')!r}: {type(e).__name__}")
            continue
        checked += 1
        if not rep.get("ok") or not rep.get("fade_setup_active"):
            quiet += 1
            continue
        tend = rep.get("median_reversion_120s")
        if tend is not None and tend < 0.1:
            p.decide(strategy="fade", market=m.get("title"), result="skipped",
                     detail=f"jump detected but this series barely reverts (median {tend:.0%})")
            continue
        e = rep["active_jump"]
        age = time.time() - float(e.get("extreme_at") or 0)
        if age > 300:
            p.decide(strategy="fade", market=m.get("title"), result="skipped",
                     detail=f"the jump is {age / 60:.0f} minutes old by the clock; only fresh jumps are faded")
            continue
        up = e.get("direction") == "up"
        pre_yes = e.get("pre_jump_price")
        if pre_yes is None:
            pre_yes = e["extreme_price"] - e["jump_size"] if up else e["extreme_price"] + e["jump_size"]
        token, outcome = (no, "no") if up else (yes, "yes")
        if not token:
            continue
        book = await _book(token)
        if not book or book["ask"] is None:
            continue
        # Fair value assumes the series' own median retrace of past jumps,
        # half the move when there is no history, never a full reversion.
        rev = min(max(float(tend) if tend is not None else 0.5, 0.2), 1.0)
        fair_yes = e["extreme_price"] - e["jump_size"] * rev if up else e["extreme_price"] + e["jump_size"] * rev
        fair = (1 - fair_yes) if up else fair_yes
        k = au.kelly_size(min(p.cfg["bankroll"], p.equity), book["ask"], fair, 0.25)
        stake = float(k.get("recommended_stake_usd") or 0)
        if stake <= 0:
            p.decide(strategy="fade", market=m.get("title"), result="skipped",
                     detail=f"no edge at the ask ({book['ask']}) against pre-jump fair {fair:.3f}")
            continue
        await _order(p, m, token, outcome, "BUY", book["ask"], stake, "fade",
                     f"fade the {e.get('direction')} overshoot of {e['jump_size']:.0%}, pre-jump price {pre_yes:.2f}, "
                     f"expected retrace {rev:.0%}")
    p.log(f"fade: {checked} candidates checked, {quiet} without a fresh jump of {c['jump']:.0%} in the last 5 minutes")


async def _settle(p: Pass, cands: list[dict]) -> None:
    c = p.cfg["settle"]
    in_range = 0
    for m in cands:
        yes, no = _tokens(m)
        yp = float(m["yes_price"])
        outcome, token = ("yes", yes) if yp >= 0.5 else ("no", no)
        if not token:
            continue
        book = await _book(token)
        if not book or book["ask"] is None:
            continue
        ask = book["ask"]
        if not (c["lo"] <= ask <= c["hi"]):
            continue
        in_range += 1
        slug = (m.get("venue_ref") or {}).get("slug")
        try:
            rc = await pm.resolution_criteria(slug)
            full = await pm.get_market(slug, full=True)
            flat = dict(full); res = full.get("resolution") or {}
            if isinstance(res, dict):
                flat.setdefault("umaResolutionStatus", res.get("uma_resolution_status") or res.get("status"))
            score = int(signals.dispute_risk(flat).get("score", 0))
        except Exception as e:
            p.log(f"settle: criteria failed for {m.get('title')!r}: {type(e).__name__}")
            continue
        if rc.get("resolution_source") in (None, "(none named)"):
            p.decide(strategy="settle", market=m.get("title"), result="skipped", detail="no resolution source named")
            continue
        if score > 20:
            p.decide(strategy="settle", market=m.get("title"), result="skipped", detail=f"dispute_risk {score} above 20")
            continue
        await _order(p, m, token, outcome, "BUY", ask, p.cfg["perorder"], "settle",
                     f"near-certain resolution at {ask:.2f}, source {rc.get('resolution_source')}, dispute risk {score}")
    p.log(f"settle: {len(cands)} candidates, {in_range} priced between {c['lo']:.2f} and {c['hi']:.2f}")


async def _momentum(p: Pass, cands: list[dict]) -> None:
    c = p.cfg["momentum"]
    checked = moved = 0
    for m in cands:
        yes, no = _tokens(m)
        try:
            times, prices = await pm.price_history(yes, c["hours"], 1)
        except Exception:
            continue
        if len(prices) < 5:
            continue
        checked += 1
        move = prices[-1] - prices[0]
        if abs(move) < c["move"]:
            continue
        moved += 1
        if (m.get("spread") or 0) > 0.03:
            p.decide(strategy="momentum", market=m.get("title"), result="skipped", detail="spread wider than 3 points")
            continue
        token, outcome = (yes, "yes") if move > 0 else (no, "no")
        if not token:
            continue
        book = await _book(token)
        if not book or book["ask"] is None:
            continue
        await _order(p, m, token, outcome, "BUY", book["ask"], p.cfg["perorder"], "momentum",
                     f"moved {move:+.0%} over {c['hours']:g}h with volume")
    p.log(f"momentum: {checked} candidates checked, {moved} moved {c['move']:.0%} or more over {c['hours']:g}h")


async def _value(p: Pass) -> None:
    c = p.cfg["value"]
    for line in c["views"].splitlines():
        if ":" not in line:
            continue
        text, _, prob_s = line.rpartition(":")
        prob = _f(prob_s.strip().rstrip("%"), -1)
        if prob > 1:
            prob /= 100.0
        text = text.strip()
        if not text or not (0 < prob < 1):
            continue
        try:
            found = [xv.unify_polymarket(x) for x in await pm.search_markets(query=text, limit=5)]
        except Exception as e:
            p.log(f"value: search failed for {text!r}: {type(e).__name__}")
            continue
        found = [(xv.similarity(f.get("title") or "", text), f) for f in found if not _ok_candidate(p, f)]
        found.sort(key=lambda t: -t[0])
        if not found or found[0][0] < 0.3:
            p.decide(strategy="value", market=text, result="skipped", detail="no open market matches this view closely enough")
            continue
        sim, m = found[0]
        yes, no = _tokens(m)
        by = await _book(yes); bn = await _book(no) if no else None
        if by and by["ask"] is not None and prob - by["ask"] >= c["edge"]:
            k = au.kelly_size(min(p.cfg["bankroll"], p.equity), by["ask"], prob, c["kelly"])
            await _order(p, m, yes, "yes", "BUY", by["ask"], float(k.get("recommended_stake_usd") or 0), "value",
                         f"my probability {prob:.2f} vs ask {by['ask']:.2f} (match {sim:.0%})")
        elif bn and bn["ask"] is not None and (1 - prob) - bn["ask"] >= c["edge"]:
            k = au.kelly_size(min(p.cfg["bankroll"], p.equity), bn["ask"], 1 - prob, c["kelly"])
            await _order(p, m, no, "no", "BUY", bn["ask"], float(k.get("recommended_stake_usd") or 0), "value",
                         f"my probability {prob:.2f} vs NO ask {bn['ask']:.2f} (match {sim:.0%})")
        else:
            p.decide(strategy="value", market=m.get("title"), result="skipped",
                     detail=f"edge below {c['edge']:.0%}: market {m.get('yes_price')}, my view {prob:.2f}")


async def _mm(p: Pass, cands: list[dict]) -> None:
    c = p.cfg["mm"]
    liquid = quoted = cancelled = 0
    resting = paper.load().get("open_orders") or []
    for m in cands:
        if (m.get("spread") or 1) > 0.02:
            continue
        liquid += 1
        yes, no = _tokens(m)
        if not yes or not no:
            continue
        # Fresh quotes every pass: stale ones on this market are pulled first.
        for o in [o for o in resting if o.get("token_id") in (yes, no)]:
            paper.cancel(o["id"])
            cancelled += 1
        held_here = yes in p.positions or no in p.positions
        if not held_here and len(p.positions) + quoted >= p.cfg["maxpos"]:
            p.decide(strategy="mm", market=m.get("title"), result="skipped",
                     detail=f"max open positions ({p.cfg['maxpos']}) would be exceeded if these quotes filled")
            continue
        book = await _book(yes)
        if not book or book["bid"] is None or book["ask"] is None:
            continue
        mid = (book["bid"] + book["ask"]) / 2
        py = round(mid - c["edge"], 2)
        pn = round((1 - mid) - c["edge"], 2)
        if not (0.02 < py < 0.98 and 0.02 < pn < 0.98):
            continue
        a = await _order(p, m, yes, "yes", "BUY", py, c["shares"] * py, "mm",
                         f"rest a YES bid {c['edge']:.0%} below mid {mid:.2f}", resting=True)
        p.taken.pop(m.get("title") or "", None)       # a quote is not a directional view
        b = await _order(p, m, no, "no", "BUY", pn, c["shares"] * pn, "mm",
                         f"rest a NO bid {c['edge']:.0%} below the NO mid {1 - mid:.2f}", resting=True)
        p.taken.pop(m.get("title") or "", None)
        if a.get("result") == "resting" or b.get("result") == "resting":
            quoted += 1
    p.log(f"quotes: {liquid} of {len(cands)} candidates had a spread of 2 points or less; "
          f"{cancelled} stale quotes pulled, {quoted} markets quoted")


# ------------------------------- risk rules -------------------------------- #

async def _review(p: Pass, pos: dict) -> None:
    """Stop loss and take profit on what is held, then the loss limit."""
    c = p.cfg
    for row in pos.get("positions") or []:
        mark, avg, size = row.get("mark"), row.get("avg_cost"), row.get("size")
        if mark is None or not avg or not size:
            continue
        chg = (mark - avg) / avg
        rule = None
        if c["on"].get("stoploss") and chg <= -c["stoploss"]:
            rule = ("stoploss", f"mark {mark:.2f} is {-chg:.0%} below entry {avg:.2f}")
        elif c["on"].get("takeprofit") and chg >= c["takeprofit"]:
            rule = ("takeprofit", f"mark {mark:.2f} is {chg:.0%} above entry {avg:.2f}")
        if not rule:
            continue
        book = await _book(row["token_id"])
        if not book or book["bid"] is None:
            p.decide(strategy=rule[0], market=row.get("title"), result="skipped", detail="no bid to sell into")
            continue
        m = {"title": row.get("title"), "venue_ref": {}}
        await _order(p, m, row["token_id"], await _outcome_of(p, row["token_id"]), "SELL", book["bid"], 0.0,
                     rule[0], rule[1])


async def _outcome_of(p: Pass, token: str) -> str:
    """Whether a held token is the YES or the NO side of its market."""
    if token not in p.outcome_of:
        side = "yes"
        try:
            m = await pm.get_market_by_token(token)
            no = ((m.get("outcomes") or {}).get("no") or {}).get("token_id")
            if no is not None and str(no) == str(token):
                side = "no"
        except Exception:
            pass
        p.outcome_of[token] = side
    return p.outcome_of[token]


def _day_pnl(d: dict, equity: float) -> float:
    """Equity change since the first pass of this UTC day, recorded in the ledger."""
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    r = d.setdefault("runner", {})
    if r.get("day") != today:
        r["day"], r["day_start_equity"] = today, equity
    return equity - float(r.get("day_start_equity") or equity)


# --------------------------------- entry ----------------------------------- #

async def run_pass(raw_cfg: dict, ledger: Path) -> dict:
    cfg = normalize(raw_cfg)
    p = Pass(cfg)
    token = FORCED_LEDGER.set(ledger)
    try:
        try:
            pos = await paper.positions()
        except Exception as e:
            pos = None
            p.log(f"ledger mark failed: {type(e).__name__}: {e}")
        p.equity = float((pos or {}).get("equity") or paper.bankroll())
        if pos:
            p.positions = {r["token_id"]: r for r in pos.get("positions") or []}
            for r in p.positions.values():
                if r.get("title") and r.get("size") and r.get("avg_cost"):
                    p.cost_by_market[r["title"]] = p.cost_by_market.get(r["title"], 0.0) + float(r["size"]) * float(r["avg_cost"])
            p.log(f"ledger: cash ${pos['cash']:.2f}, equity ${pos['equity']:.2f}, "
                  f"{len(p.positions)} positions, {len(pos.get('open_orders') or [])} resting")
            d = paper.load()
            day = _day_pnl(d, p.equity)
            paper.save(d)
            if cfg["on"].get("daily") and day <= -cfg["daily"]:
                p.halted = f"daily loss limit: today {day:+.2f} against -${cfg['daily']:g}; no new entries"
                p.log(p.halted)
            await _review(p, pos)

        strategies = [s for s in ("fade", "settle", "value", "momentum", "mm") if cfg["on"].get(s)]
        cands = await _universe(p)
        if not strategies:
            p.log("no strategy switched on: read-only pass")
        if "fade" in strategies:
            await _fade(p, cands)
        if "settle" in strategies:
            soon = await _universe(p, closing_hours=72)
            seen = {c["market_id"] for c in soon}
            await _settle(p, soon + [c for c in cands if c["market_id"] not in seen])
        if "value" in strategies:
            await _value(p)
        if "momentum" in strategies:
            await _momentum(p, cands)
        if "mm" in strategies:
            await _mm(p, cands)

        try:
            after = await paper.positions()
        except Exception as e:
            after = pos
            p.log(f"final mark failed: {type(e).__name__}: {e}")
        placed = [d for d in p.decisions if d.get("result") in ("filled", "partial", "resting")]
        p.log(f"done: {len(p.decisions)} decisions, {len(placed)} orders placed")
        return {
            "ok": True, "mode": "paper", "engine": "rules",
            "started": dt.datetime.fromtimestamp(p.t0, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seconds": round(time.time() - p.t0, 1),
            "strategies": strategies, "universe": p.universe,
            "candidates": [{"title": c.get("title"), "yes_price": c.get("yes_price"),
                                                      "volume_24h": c.get("volume_24h"), "spread": c.get("spread")}
                                                     for c in cands],
            "decisions": p.decisions, "orders_placed": len(placed), "halted": p.halted,
            "ledger": {k: after.get(k) for k in ("cash", "equity", "realized_pnl", "unrealized_pnl",
                                                 "positions", "open_orders", "fills", "bankroll")} if after else None,
            "steps": p.steps,
            "note": ("deterministic pass: the switches are rules, no model was consulted. Paper fills come "
                     "from the live book with no queue, no impact and no fees, so this is an upper bound."),
        }
    finally:
        FORCED_LEDGER.reset(token)
