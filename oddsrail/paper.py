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

import json
import os
import time
import uuid
from pathlib import Path


def enabled() -> bool:
    return os.environ.get("ODDSRAIL_PAPER", "1").strip().lower() not in ("0", "false", "no")


def ledger_path() -> Path:
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
                _apply_fill(d, o["token_id"], o["side"], o["size"], o["price"],
                            o.get("title"), f"resting:{o['id']}")
                filled_ids.append(o["id"])
                d["open_orders"].remove(o)
            except ValueError:
                pass  # cannot afford it any more; leave it resting
    return filled_ids


async def positions() -> dict:
    from . import polymarket as pm
    d = load()
    just_filled = await _settle_resting(d, pm)
    rows, unreal, value = [], 0.0, 0.0
    for tid, pos in d["positions"].items():
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
        avg = pos["cost"] / pos["size"] if pos["size"] else 0.0
        u = (mark - avg) * pos["size"] if mark is not None else None
        v = mark * pos["size"] if mark is not None else None
        if u is not None:
            unreal += u
        if v is not None:
            value += v
        rows.append({"token_id": tid, "title": pos.get("title"), "size": round(pos["size"], 6),
                     "avg_cost": round(avg, 4), "mark": mark,
                     "unrealized_pnl": round(u, 4) if u is not None else None,
                     "value": round(v, 4) if v is not None else None})
    save(d)
    return {"enabled": enabled(), "ledger": str(ledger_path()),
            "bankroll": d["bankroll"], "cash": round(d["cash"], 2),
            "positions": rows, "open_orders": d["open_orders"],
            "just_filled_resting": just_filled,
            "realized_pnl": round(d["realized_pnl"], 4),
            "unrealized_pnl": round(unreal, 4),
            "equity": round(d["cash"] + value, 2),
            "fills": len(d["fills"]),
            "note": ("simulated fills: live book at call time, no queue position, "
                     "no impact, no fees, so treat results as an upper bound. "
                     "Polymarket only; Kalshi dry-run still returns intents.")}
