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

MAX_CANDIDATES = 12
MAX_NEW_ORDERS = 6
MIN_NOTIONAL = 1.05          # the exchange's $1 minimum on marketable orders, with slack
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

    cfg = {
        "topic": str(raw.get("topic") or "").strip()[:80],
        "minvol": max(0.0, _f(raw.get("minvol"), 20000)),
        "bankroll": min(max(10.0, _f(raw.get("bankroll"), 1000)), 100000.0),
        "perorder": min(max(1.0, _f(raw.get("perorder"), 25)), 500.0),
        "maxpos": int(min(max(1, _f(raw.get("maxpos"), 5)), 50)),
        "closing_h": max(0.0, _f(raw.get("closing"), 2)),
        "on": on,
        "fade": {"jump": val("fade", "jump", 8) / 100.0, "hours": min(max(1.0, val("fade", "hours", 6)), 24.0)},
        "settle": {"lo": val("settle", "lo", 0.9), "hi": min(val("settle", "hi", 0.97), 0.969)},
        "value": {"edge": val("value", "edge", 5) / 100.0, "kelly": min(max(0.05, val("value", "kelly", 0.25)), 1.0),
                  "views": str((vals.get("value") or {}).get("views") or "")},
        "momentum": {"move": val("momentum", "move", 10) / 100.0, "hours": min(max(1.0, val("momentum", "hours", 6)), 24.0)},
        "mm": {"edge": val("mm", "edge", 2) / 100.0, "shares": min(max(5.0, val("mm", "shares", 20)), 500.0)},
        "stoploss": val("stoploss", "pct", 25) / 100.0,
        "takeprofit": val("takeprofit", "pct", 40) / 100.0,
        "daily": val("daily", "usd", 50),
        "expo": val("expo", "usd", 50),
        "dispute": int(val("dispute", "score", 30)),
        "liquidity": val("liquidity", "slip", 2) / 100.0,
        "watch": min(max(1.0, val("watch", "sec", 20)), 8.0),
    }
    return cfg


# --------------------------------------------------------------------------- #
# the pass                                                                    #
# --------------------------------------------------------------------------- #

class Pass:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.t0 = time.time()
        self.steps: list[str] = []
        self.decisions: list[dict] = []
        self.new_orders = 0
        self.halted: str | None = None
        self.positions: dict = {}        # token_id -> row from paper.positions()
        self.cost_by_market: dict = {}   # slug -> cost of held positions

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


async def _universe(p: Pass, query: str, closing_hours: float | None = None) -> list[dict]:
    """Unified, filtered candidates for a query (or the closing-soon list)."""
    try:
        if closing_hours:
            raw = await pm.closing_soon(hours=closing_hours, limit=20)
            if query:
                raw = [m for m in raw if xv.similarity(m.get("question") or "", query) > 0] or raw
        else:
            raw = await pm.search_markets(query=query, limit=MAX_CANDIDATES)
    except Exception as e:
        p.log(f"universe scan failed: {type(e).__name__}: {e}")
        return []
    out, dropped = [], {}
    for m in raw:
        u = xv.unify_polymarket(m)
        why = _ok_candidate(p, u)
        if why:
            dropped[why] = dropped.get(why, 0) + 1
            continue
        out.append(u)
    p.log(f"universe {'closing-soon' if closing_hours else repr(query)}: {len(raw)} scanned, "
          f"{len(out)} candidates" + (f", dropped {dropped}" if dropped else ""))
    return out[:MAX_CANDIDATES]


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


async def _hygiene(p: Pass, m: dict, token: str, side: str, price: float, size: float,
                   resting: bool = False) -> str | None:
    """Dispute, fill-quality and watch checks. Returns a skip reason or None.
    A resting quote is meant not to fill now, so the fill checks do not
    apply to it; the dispute check still does."""
    c = p.cfg
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
                return "no resolution source named"
            if score > c["dispute"]:
                return f"dispute_risk {score} above {c['dispute']}"
        except Exception as e:
            return f"dispute check failed: {type(e).__name__}"
    if not resting and (c["on"].get("liquidity") or c["on"].get("watch")):
        book = await _book(token)
        if not book:
            return "no order book"
        levels = book["asks"] if side == "BUY" else book["bids"]
        walk = xv.walk_book(paper._within_limit(levels, side, price), size)
        if c["on"].get("liquidity"):
            if not walk.get("fillable"):
                return "book cannot fill the size inside the limit"
            best = book["ask"] if side == "BUY" else book["bid"]
            slip = abs(float(walk.get("slippage_vs_best") or 0))
            if best and slip / best > c["liquidity"]:
                return f"slippage {slip:.3f} above {c['liquidity']:.1%} of best"
        if c["on"].get("watch"):
            await asyncio.sleep(c["watch"])
            again = await _book(token)
            if again:
                if side == "BUY" and again["ask"] is not None and book["ask"] is not None \
                        and again["ask"] > book["ask"] + 0.01:
                    return f"book moved against while watching (ask {book['ask']} -> {again['ask']})"
                if side == "SELL" and again["bid"] is not None and book["bid"] is not None \
                        and again["bid"] < book["bid"] - 0.01:
                    return f"book moved against while watching (bid {book['bid']} -> {again['bid']})"
    return None


async def _order(p: Pass, m: dict, token: str, outcome: str, side: str, price: float,
                 notional_usd: float, strategy: str, why: str, resting: bool = False) -> dict:
    """Size, cap, check and paper-place one order; always returns a decision."""
    c = p.cfg
    title = m.get("title") or ""
    base = dict(strategy=strategy, market=title, slug=(m.get("venue_ref") or {}).get("slug"),
                token_id=token, outcome=outcome.upper(), side=side, price=round(price, 4), why=why)
    if p.halted:
        return p.decide(**base, result="skipped", detail=p.halted)
    if p.new_orders >= MAX_NEW_ORDERS:
        return p.decide(**base, result="skipped", detail=f"pass limit of {MAX_NEW_ORDERS} new orders")
    held = p.positions.get(token)
    if side == "BUY":
        if not held and len(p.positions) >= c["maxpos"]:
            return p.decide(**base, result="skipped", detail=f"max open positions ({c['maxpos']}) reached")
        if held and c["on"].get("noadd") and held.get("mark") is not None and held["mark"] < held["avg_cost"]:
            return p.decide(**base, result="skipped", detail="never add to a loser: mark below entry")
        notional = min(notional_usd, c["perorder"])
        if c["on"].get("expo"):
            spent = p.cost_by_market.get(base["slug"], 0.0)
            if spent + notional > c["expo"]:
                notional = c["expo"] - spent
                if notional <= 0:
                    return p.decide(**base, result="skipped", detail=f"exposure cap ${c['expo']:g} reached for this market")
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

    skip = await _hygiene(p, m, token, side, price, size, resting=resting)
    if skip:
        return p.decide(**base, result="skipped", detail=skip)

    # The intent is what check_order compares with the market: the plain
    # order in words. The strategy's reasoning stays in the decision record.
    intent = f"{'buy' if side == 'BUY' else 'sell'} {outcome.upper()} on {title}"
    try:
        chk = await ck.check_order("polymarket", token, side, price, size, intent, outcome)
    except Exception as e:
        return p.decide(**base, result="skipped", detail=f"check_order failed: {type(e).__name__}: {e}")
    failed = [f"{x['check']}: {x.get('detail', '')}" for x in chk.get("checks", []) if x.get("status") != "ok"]
    base["verdict"] = chk.get("verdict")
    if chk.get("verdict") != "ok":
        return p.decide(**base, result="skipped", detail="; ".join(failed) or chk.get("verdict"))

    try:
        fill = await paper.simulate_polymarket(token, side, price, size)
    except Exception as e:
        return p.decide(**base, result="skipped", detail=f"paper fill failed: {type(e).__name__}: {e}")
    p.new_orders += 1
    if fill.get("refused"):
        return p.decide(**base, result="refused", detail=fill["refused"])
    filled = float(fill.get("filled_size") or 0)
    result = "filled" if filled >= size - 1e-9 else "partial" if filled > 0 else "resting"
    if side == "BUY" and filled > 0:
        p.cost_by_market[base["slug"]] = p.cost_by_market.get(base["slug"], 0.0) + filled * float(fill.get("avg_price") or price)
        p.positions.setdefault(token, {"size": 0.0, "avg_cost": price, "mark": None})
        p.positions[token]["size"] += filled
    return p.decide(**base, result=result, filled=filled, avg_price=fill.get("avg_price"),
                    resting=fill.get("resting_size"), paper_order_id=fill.get("paper_order_id"),
                    cash_after=fill.get("cash_after"))


# ------------------------------- strategies -------------------------------- #

async def _fade(p: Pass, cands: list[dict]) -> None:
    c = p.cfg["fade"]
    for m in cands:
        yes, no = _tokens(m)
        try:
            times, prices = await pm.price_history(yes, c["hours"], 1)
            rep = signals.overshoot_report(times, prices, lookback=60.0, threshold=c["jump"])
        except Exception as e:
            p.log(f"fade: history failed for {m.get('title')!r}: {type(e).__name__}")
            continue
        if not rep.get("ok") or not rep.get("fade_setup_active"):
            continue
        tend = rep.get("median_reversion_120s")
        if tend is not None and tend < 0.1:
            p.decide(strategy="fade", market=m.get("title"), result="skipped",
                     detail=f"jump detected but this series barely reverts (median {tend:.0%})")
            continue
        e = rep["active_jump"]
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
        fair = (1 - pre_yes) if up else pre_yes
        k = au.kelly_size(min(p.cfg["bankroll"], p.equity), book["ask"], fair, 0.25)
        stake = float(k.get("recommended_stake_usd") or 0)
        if stake <= 0:
            p.decide(strategy="fade", market=m.get("title"), result="skipped",
                     detail=f"no edge at the ask ({book['ask']}) against pre-jump fair {fair:.3f}")
            continue
        await _order(p, m, token, outcome, "BUY", book["ask"], stake, "fade",
                     f"fade the {e.get('direction')} overshoot of {e['jump_size']:.0%}, pre-jump price {pre_yes:.2f}")


async def _settle(p: Pass, cands: list[dict]) -> None:
    c = p.cfg["settle"]
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
        if 1 - ask < 0.025:
            continue
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


async def _momentum(p: Pass, cands: list[dict]) -> None:
    c = p.cfg["momentum"]
    for m in cands:
        yes, no = _tokens(m)
        try:
            times, prices = await pm.price_history(yes, c["hours"], 1)
        except Exception:
            continue
        if len(prices) < 5:
            continue
        move = prices[-1] - prices[0]
        if abs(move) < c["move"]:
            continue
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
    for m in cands:
        if (m.get("spread") or 1) > 0.02:
            continue
        yes, no = _tokens(m)
        if not yes or not no:
            continue
        book = await _book(yes)
        if not book or book["bid"] is None or book["ask"] is None:
            continue
        mid = (book["bid"] + book["ask"]) / 2
        py = round(mid - c["edge"], 2)
        pn = round((1 - mid) - c["edge"], 2)
        if not (0.02 < py < 0.98 and 0.02 < pn < 0.98):
            continue
        await _order(p, m, yes, "yes", "BUY", py, c["shares"] * py, "mm",
                     f"rest a YES bid {c['edge']:.0%} below mid {mid:.2f}", resting=True)
        await _order(p, m, no, "no", "BUY", pn, c["shares"] * pn, "mm",
                     f"rest a NO bid {c['edge']:.0%} below the NO mid {1 - mid:.2f}", resting=True)


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
        await _order(p, m, row["token_id"], "yes", "SELL", book["bid"], 0.0, rule[0], rule[1])


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
        cands = await _universe(p, cfg["topic"])
        if not strategies:
            p.log("no strategy switched on: read-only pass")
        if "fade" in strategies:
            await _fade(p, cands)
        if "settle" in strategies:
            soon = await _universe(p, cfg["topic"], closing_hours=72)
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
            "strategies": strategies, "candidates": [{"title": c.get("title"), "yes_price": c.get("yes_price"),
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
