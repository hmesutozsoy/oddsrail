"""Paper-trading ledger: makes dry-run a real sandbox.

Without this, ODDSRAIL_DRY_RUN=1 returns the order that WOULD post and then
forgets it, so an agent rehearsing a strategy can never see whether it made
money. With it, every dry-run Polymarket order is filled against the LIVE
order book (walked, within the limit price), the remainder rests as a paper
order that fills when the market crosses it, and paper_positions reports
cash, positions at current marks, realized and unrealized P&L.

  ODDSRAIL_PAPER=1            default on; 0/false/no disables
  ODDSRAIL_PAPER_LEDGER       JSON file, default ~/.oddsrail/paper.json
  ODDSRAIL_PAPER_BANKROLL     starting cash in USDC, default 1000

Honesty notes: fills are simulated from the book at the moment of the call,
with no queue position, no latency and no market impact, so paper results
are an upper bound on what the same orders would have done live. Fees are
not deducted. Kalshi orders are not papered yet (they still return the
intent). The ledger is one local JSON file the operator can read or delete.
"""

from __future__ import annotations

import contextvars
import json
import os
import time
import uuid
from pathlib import Path


# A position whose book has been gone this long is no longer valued: the last
# mark stops counting toward equity and the row says why.
STALE_MARK_H = 12.0


def enabled() -> bool:
    return os.environ.get("ODDSRAIL_PAPER", "1").strip().lower() not in ("0", "false", "no")


# A multi-tenant host (oddsrail.cloud) installs a callable here that returns
# the ledger path for the account behind the current request, so one process
# keeps one ledger per signed-in agent. None means the local single-operator
# path below.
ledger_resolver = None

# Set for the duration of one task to point every paper call at one file:
# how the hosted runner and the arena board work on a ledger that is not the
# request's own. Wins over the resolver and the environment.
forced_ledger: contextvars.ContextVar[Path | None] = contextvars.ContextVar("oddsrail_forced_ledger", default=None)


def ledger_path() -> Path:
    forced = forced_ledger.get()
    if forced is not None:
        return Path(forced)
    if ledger_resolver is not None:
        resolved = ledger_resolver()
        if resolved:
            return Path(resolved)
    p = os.environ.get("ODDSRAIL_PAPER_LEDGER")
    return Path(p).expanduser() if p else Path.home() / ".oddsrail" / "paper.json"


def bankroll() -> float:
    try:
        v = float(os.environ.get("ODDSRAIL_PAPER_BANKROLL", "1000"))
    except ValueError:
        v = 1000.0
    return v if v > 0 else 1000.0


def _empty() -> dict:
    b = bankroll()
    return {"version": 1, "created": time.time(), "bankroll": b, "cash": b,
            "positions": {}, "open_orders": [], "fills": [], "realized_pnl": 0.0}


def load() -> dict:
    p = ledger_path()
    if not p.exists():
        return _empty()
    try:
        d = json.loads(p.read_text())
        if not isinstance(d, dict) or "cash" not in d:
            return _empty()
        return d
    except Exception:
        return _empty()


def save(d: dict) -> None:
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, default=str))
    tmp.replace(p)


def reset() -> dict:
    d = _empty()
    save(d)
    return {"reset": True, "ledger": str(ledger_path()), "cash": d["cash"]}


# ------------------------------- accounting -------------------------------- #

def _apply_fill(d: dict, token_id: str, side: str, size: float, price: float,
                title: str | None, source: str) -> dict:
    """Book a fill into cash / positions / realized P&L. Raises ValueError
    when the paper account cannot support it."""
    notional = size * price
    pos = d["positions"].get(token_id) or {"size": 0.0, "cost": 0.0, "title": title}
    if side == "BUY":
        if notional > d["cash"] + 1e-9:
            raise ValueError(f"insufficient paper cash: ${d['cash']:.2f} available, "
                             f"${notional:.2f} needed")
        d["cash"] -= notional
        pos["size"] += size
        pos["cost"] += notional
    else:
        if size > pos["size"] + 1e-9:
            raise ValueError(f"paper account holds {pos['size']:.4f} of this token, "
                             f"cannot sell {size:.4f} (no shorting in paper mode)")
        avg_cost = pos["cost"] / pos["size"] if pos["size"] else 0.0
        realized = (price - avg_cost) * size
        d["realized_pnl"] += realized
        d["cash"] += notional
        pos["size"] -= size
        pos["cost"] -= avg_cost * size
        if pos["size"] <= 1e-9:
            pos["size"], pos["cost"] = 0.0, 0.0
    if title and not pos.get("title"):
        pos["title"] = title
    if pos["size"] > 0:
        d["positions"][token_id] = pos
    else:
        d["positions"].pop(token_id, None)
    fill = {"id": uuid.uuid4().hex[:12], "time": time.time(), "token_id": token_id,
            "title": title or pos.get("title"), "side": side, "size": round(size, 6),
            "price": price, "notional": round(notional, 6), "source": source}
    d["fills"].append(fill)
    return fill


def _within_limit(levels: list, side: str, limit: float) -> list:
    out = []
    for lv in levels:
        try:
            p = float(lv["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if (side == "BUY" and p <= limit) or (side == "SELL" and p >= limit):
            out.append(lv)
    return out


async def _title_for(token_id: str) -> str | None:
    try:
        from . import polymarket as pm
        m = await pm.get_market_by_token(token_id)
        return (m or {}).get("question")
    except Exception:
        return None


async def simulate_polymarket(token_id: str, side: str, price: float, size: float) -> dict:
    """Fill a dry-run order against the live book, rest the remainder."""
    from . import crossvenue as xv
    from . import polymarket as pm
    d = load()
    ob = await pm.get_orderbook(token_id)
    levels = ob["asks"] if side == "BUY" else ob["bids"]
    walk = xv.walk_book(_within_limit(levels, side, price), size)
    filled = float(walk.get("filled_size") or 0)
    title = await _title_for(token_id)
    out = {"ledger": str(ledger_path()), "filled_size": filled,
           "avg_price": walk.get("avg_price"), "notional": walk.get("notional"),
           "resting_size": round(size - filled, 6)}
    if filled > 0:
        try:
            _apply_fill(d, token_id, side, filled, float(walk["avg_price"]), title, "immediate")
        except ValueError as e:
            save(d)
            return {**out, "filled_size": 0.0, "resting_size": 0.0,
                    "refused": str(e),
                    "note": "paper account could not support this order; nothing recorded"}
    if size - filled > 1e-9:
        oid = "paper-" + uuid.uuid4().hex[:10]
        d["open_orders"].append({"id": oid, "time": time.time(), "token_id": token_id,
                                 "title": title, "side": side, "price": price,
                                 "size": round(size - filled, 6)})
        out["paper_order_id"] = oid
        out["note"] = ("not marketable at that price; resting as a paper order "
                       "that fills when the book crosses it (checked on the "
                       "next paper_positions call). cancel_order accepts the "
                       "paper_order_id.")
    else:
        out["note"] = "filled against the live book (simulated: no queue, no impact, no fees)"
    save(d)
    pos = d["positions"].get(token_id)
    out["cash_after"] = round(d["cash"], 2)
    out["position_after"] = round(pos["size"], 6) if pos else 0.0
    return out


def cancel(order_id: str) -> dict:
    d = load()
    before = len(d["open_orders"])
    d["open_orders"] = [o for o in d["open_orders"] if o["id"] != order_id]
    save(d)
    return {"paper": "cancelled" if len(d["open_orders"]) < before else "no such paper order",
            "order_id": order_id}


async def _fill_resting(d: dict, pm, o: dict) -> float:
    """Fill a resting paper order against the depth that is really there.
    Returns the size filled; the remainder keeps resting."""
    from . import crossvenue as xv
    ob = await pm.get_orderbook(o["token_id"])
    levels = ob["asks"] if o["side"] == "BUY" else ob["bids"]
    walk = xv.walk_book(_within_limit(levels, o["side"], o["price"]), o["size"])
    filled = float(walk.get("filled_size") or 0)
    if filled <= 1e-9:
        return 0.0
    # A resting order is the maker, so it trades at its own price; what the
    # crossing side has on offer only limits HOW MUCH of it trades.
    _apply_fill(d, o["token_id"], o["side"], filled, float(o["price"]),
                o.get("title"), f"resting:{o['id']}")
    return filled


async def _settle_resting(d: dict, pm) -> list:
    """Fill any resting paper order the market has since crossed."""
    filled_ids = []
    for o in list(d["open_orders"]):
        try:
            ob = await pm.get_orderbook(o["token_id"])
        except Exception:
            continue
        bb, ba = ob.get("best_bid"), ob.get("best_ask")
        crossed = False
        if o["side"] == "BUY" and ba is not None and float(ba) <= o["price"]:
            crossed = True
        if o["side"] == "SELL" and bb is not None and float(bb) >= o["price"]:
            crossed = True
        if crossed:
            try:
                filled = await _fill_resting(d, pm, o)
                if filled <= 1e-9:
                    continue                      # crossed on the top of book only, no depth
                filled_ids.append(o["id"])
                if filled >= float(o["size"]) - 1e-9:
                    d["open_orders"].remove(o)
                else:
                    o["size"] = round(float(o["size"]) - filled, 6)   # partial: the rest keeps resting
            except ValueError:
                pass  # cannot afford it any more; leave it resting
            except Exception:
                pass  # the book call failed; try again on the next read
    return filled_ids


async def _settle_if_resolved(d: dict, pm, tid: str, pos: dict) -> dict | None:
    """A position whose market has resolved pays 1 or 0 per share. Without
    this, a winning position is worth nothing the moment its book vanishes,
    which is the opposite of what happened."""
    try:
        m = await pm.get_market_by_token(tid) or {}
    except Exception:
        return None
    outs = m.get("outcomes") or {}
    side = "no" if str(((outs.get("no") or {}).get("token_id"))) == str(tid) else "yes"
    price = (outs.get(side) or {}).get("price")
    status = str(m.get("uma_resolution_status") or "").lower()
    closed = bool(m.get("closed")) or status.startswith("resolved")
    if price is None or not closed:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    payout = 1.0 if price >= 0.99 else 0.0 if price <= 0.01 else None
    if payout is None:
        return None
    size = float(pos["size"])
    fill = _apply_fill(d, tid, "SELL", size, payout, pos.get("title"), "resolution")
    return {"token_id": tid, "title": pos.get("title"), "side": side.upper(), "size": round(size, 6),
            "payout": payout, "proceeds": round(size * payout, 4), "fill_id": fill["id"]}


async def positions() -> dict:
    from . import polymarket as pm
    d = load()
    just_filled = await _settle_resting(d, pm)
    rows, unreal, value, resolved, stale = [], 0.0, 0.0, [], []
    for tid, pos in list(d["positions"].items()):
        mark = None
        try:
            ob = await pm.get_orderbook(tid)
            bb, ba = ob.get("best_bid"), ob.get("best_ask")
            if bb is not None and ba is not None:
                mark = (float(bb) + float(ba)) / 2
            elif bb is not None:
                mark = float(bb)
        except Exception:
            pass
        stale_for = None
        if mark is None:
            settled = await _settle_if_resolved(d, pm, tid, pos)
            if settled:
                resolved.append(settled)
                continue
            # No book right now. Carry the last mark for a while, because a
            # momentary gap should not write a position to zero, but do not
            # carry it forever: a mark nobody can refresh is not a valuation.
            mark = pos.get("last_mark")
            age = time.time() - float(pos.get("last_mark_at") or 0)
            stale_for = round(age / 3600.0, 1)
            if mark is not None and age > STALE_MARK_H * 3600:
                mark = None
        else:
            pos["last_mark"] = mark
            pos["last_mark_at"] = time.time()
        avg = pos["cost"] / pos["size"] if pos["size"] else 0.0
        u = (mark - avg) * pos["size"] if mark is not None else None
        v = mark * pos["size"] if mark is not None else None
        if u is not None:
            unreal += u
        if v is not None:
            value += v
        row = {"token_id": tid, "title": pos.get("title"), "size": round(pos["size"], 6),
               "avg_cost": round(avg, 4), "mark": mark,
               "unrealized_pnl": round(u, 4) if u is not None else None,
               "value": round(v, 4) if v is not None else None}
        if stale_for is not None:
            row["mark_age_hours"] = stale_for
            row["note"] = (f"no order book for {stale_for}h; "
                           + ("carrying the last mark" if mark is not None else
                              f"older than {STALE_MARK_H:g}h, so this position is excluded from equity"))
        rows.append(row)
        if stale_for is not None and mark is None:
            stale.append(row)
    save(d)
    return {"enabled": enabled(), "ledger": str(ledger_path()),
            "bankroll": d["bankroll"], "cash": round(d["cash"], 2),
            "positions": rows, "open_orders": d["open_orders"],
            "just_filled_resting": just_filled, "resolved": resolved, "stale": stale,
            "realized_pnl": round(d["realized_pnl"], 4),
            "unrealized_pnl": round(unreal, 4),
            "equity": round(d["cash"] + value, 2),
            "fills": len(d["fills"]),
            "note": ("simulated fills: live book at call time, no queue position, "
                     "no impact, no fees, so treat results as an upper bound. "
                     "Polymarket only; Kalshi dry-run still returns intents.")}
