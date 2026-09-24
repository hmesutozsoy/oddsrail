"""Reference agent: quote and settle, in paper mode, on a timer.

This is plumbing, not a strategy. It exists to show, with a public journal,
that the whole loop works end to end through oddsrail: find liquid markets,
read the book best-first, cost a size by walking the book, rest maker quotes
inside the operator's guardrails, let the paper ledger fill them when the
market crosses, and settle positions that show a small mark-up. Every run
appends one JSON line to the journal and rewrites a markdown summary.

Run it hourly with ODDSRAIL_DRY_RUN=1 (the default) and ODDSRAIL_PAPER=1
(the default). The same code runs live with ODDSRAIL_DRY_RUN=0; the point
of the paper fortnight is to publish what it did before anyone trusts it
with money.

Environment (all optional):
  ODDSRAIL_PAPER_JOURNAL     directory for journal.jsonl / journal.md (./journal)
  ODDSRAIL_PAPER_LEDGER      paper ledger file (~/.oddsrail/paper.json)
  ODDSRAIL_MAX_ORDER_NOTIONAL  keep it small; refusals are logged, not hidden
  PAPER_AGENT_MAX_MARKETS    markets quoted per run (5)
  PAPER_AGENT_QUOTE_SIZE     shares per quote (5)
"""

import asyncio
import datetime as dt
import json
import os
import pathlib
import time

from oddsrail import crossvenue as xv
from oddsrail import paper
from oddsrail import polymarket as pm
from oddsrail import trading

MAX_MARKETS = int(os.environ.get("PAPER_AGENT_MAX_MARKETS", "5"))
QUOTE_SIZE = float(os.environ.get("PAPER_AGENT_QUOTE_SIZE", "5"))
MAX_SPREAD = 0.04          # only quote two-sided, tight books
MIN_PRICE, MAX_PRICE = 0.10, 0.90
TAKE_PROFIT = 0.02         # settle a position once the mid is this far above cost
STALE_HOURS = 24           # cancel resting paper quotes older than this
JOURNAL = pathlib.Path(os.environ.get("ODDSRAIL_PAPER_JOURNAL", "journal"))


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


async def candidates() -> list:
    """Open, liquid, mid-priced markets that end more than two days out."""
    out, cutoff = [], dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)
    for m in await pm.search_markets("", 30):
        try:
            if not m.get("accepting_orders"):
                continue
            end = dt.datetime.fromisoformat(str(m.get("end_date")).replace("Z", "+00:00"))
            if end <= cutoff:
                continue
            bb, ba = float(m.get("best_bid") or 0), float(m.get("best_ask") or 0)
            if not (bb and ba) or ba - bb > MAX_SPREAD or not (MIN_PRICE <= bb <= MAX_PRICE):
                continue
            tok = ((m.get("outcomes") or {}).get("yes") or {}).get("token_id")
            if tok:
                out.append({"token_id": tok, "question": m.get("question"), "bid": bb, "ask": ba})
        except (TypeError, ValueError):
            continue
    return out[:MAX_MARKETS]


async def run_once() -> dict:
    t0 = time.monotonic()
    entry = {"time": now_utc(), "scanned": 0, "quoted": [], "settled": [], "cancelled": [],
             "refusals": [], "errors": []}

    # 1. settle: fills any resting paper quote the market crossed since last run
    port = await paper.positions()
    entry["filled_resting"] = port.get("just_filled_resting", [])

    # 2. cancel stale quotes so the book of paper orders never accretes
    cutoff = time.time() - STALE_HOURS * 3600
    for o in port.get("open_orders", []):
        if o.get("time", 0) < cutoff:
            paper.cancel(o["id"]); entry["cancelled"].append(o["id"])

    # 3. take profit on positions that drifted up
    for p in port.get("positions", []):
        mark, cost = p.get("mark"), p.get("avg_cost")
        if mark is None or cost is None or mark - cost < TAKE_PROFIT:
            continue
        try:
            r = await trading.place_order(p["token_id"], "SELL", round(mark, 3), float(p["size"]))
            entry["settled"].append({"token_id": p["token_id"][:12], "price": round(mark, 3),
                                     "size": p["size"], "result": _summ(r)})
        except Exception as e:
            entry["errors"].append(f"settle {p['token_id'][:12]}: {type(e).__name__}: {e}")

    # 4. quote: rest a maker bid on each candidate not already held or quoted
    held = {p["token_id"] for p in port.get("positions", [])}
    quoted = {o["token_id"] for o in port.get("open_orders", [])}
    cands = await candidates()
    entry["scanned"] = len(cands)
    for c in cands:
        if c["token_id"] in held or c["token_id"] in quoted:
            continue
        try:
            ob = await pm.get_orderbook(c["token_id"])
            cost = xv.walk_book(ob["asks"], QUOTE_SIZE)          # what taking would cost
            r = await trading.place_order(c["token_id"], "BUY", float(ob["best_bid"]), QUOTE_SIZE)
            entry["quoted"].append({"token_id": c["token_id"][:12], "question": str(c["question"])[:60],
                                    "bid": ob["best_bid"], "ask": ob["best_ask"],
                                    "take_cost_avg": cost.get("avg_price"), "result": _summ(r)})
            if r.get("blocked_by") == "guardrail":
                entry["refusals"].append({"rule": r.get("rule"), "limit": r.get("limit"),
                                          "requested": r.get("requested")})
        except Exception as e:
            entry["errors"].append(f"quote {c['token_id'][:12]}: {type(e).__name__}: {e}")

    # 5. snapshot
    port = await paper.positions()
    entry["portfolio"] = {k: port.get(k) for k in ("cash", "equity", "realized_pnl", "unrealized_pnl", "fills")}
    entry["positions"] = len(port.get("positions", []))
    entry["open_orders"] = len(port.get("open_orders", []))
    entry["seconds"] = round(time.monotonic() - t0, 1)
    return entry


def _summ(r: dict) -> str:
    if r.get("blocked_by"):
        return f"refused:{r.get('rule')}"
    p = r.get("paper") or {}
    if p.get("refused"):
        return "paper-refused"
    if p.get("paper_order_id"):
        return f"resting:{p['paper_order_id']}"
    if p.get("filled_size"):
        return f"filled:{p['filled_size']}@{p.get('avg_price')}"
    return "dry-run" if r.get("dry_run") else ("accepted" if r.get("accepted") else "unknown")


def write_journal(entry: dict) -> None:
    JOURNAL.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL / "journal.jsonl", "a") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    rows = [json.loads(l) for l in open(JOURNAL / "journal.jsonl") if l.strip()]
    first, last = rows[0], rows[-1]
    pf = last.get("portfolio") or {}
    md = [
        "# Paper agent journal",
        "",
        f"Runs: {len(rows)} · first: {first['time']} · latest: {last['time']} · "
        f"bankroll {paper.bankroll():.0f} USDC · equity now {pf.get('equity')} · "
        f"realized {pf.get('realized_pnl')} · unrealized {pf.get('unrealized_pnl')} · fills {pf.get('fills')}",
        "",
        "Simulated fills against the live book: no queue position, no impact, no fees. "
        "An upper bound on the same orders live, published so the plumbing is on record before "
        "anyone trusts it with money. Strategy is deliberately trivial (rest a maker bid on tight, "
        "mid-priced, liquid markets; settle at +0.02); it is not a recommendation.",
        "",
        "| time (UTC) | scanned | quoted | filled | settled | cancelled | open | cash | equity | realized | unrealized | refusals | errors |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows[-48:]:
        p = r.get("portfolio") or {}
        md.append(f"| {r['time'][5:16]} | {r.get('scanned', 0)} | {len(r.get('quoted', []))} | "
                  f"{len(r.get('filled_resting', []))} | {len(r.get('settled', []))} | {len(r.get('cancelled', []))} | "
                  f"{r.get('open_orders', 0)} | {p.get('cash')} | {p.get('equity')} | {p.get('realized_pnl')} | "
                  f"{p.get('unrealized_pnl')} | {len(r.get('refusals', []))} | {len(r.get('errors', []))} |")
    (JOURNAL / "journal.md").write_text("\n".join(md) + "\n")


async def main() -> None:
    entry = await run_once()
    write_journal(entry)
    print(json.dumps({k: entry[k] for k in ("time", "scanned", "positions", "open_orders", "portfolio", "seconds")},
                     default=str))
    print(f"quoted={len(entry['quoted'])} settled={len(entry['settled'])} "
          f"filled={len(entry['filled_resting'])} cancelled={len(entry['cancelled'])} "
          f"refusals={len(entry['refusals'])} errors={len(entry['errors'])}")
    for e in entry["errors"]:
        print("  error:", e)


if __name__ == "__main__":
    asyncio.run(main())
