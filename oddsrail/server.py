"""oddsrail — the rail AI agents use to trade prediction markets.

MCP server exposing Polymarket market data, order routing with on-chain
builder-code attribution (CLOB V2), and premium signal tools (overshoot,
dispute risk). Self-hosted and non-custodial: keys never leave the machine.

Run (stdio, for Claude Code / Desktop / any MCP client):
    .venv/bin/python -m oddsrail.server
"""

import json
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import crossvenue as xv
from . import kalshi as kx
from . import polymarket as pm
from . import signals
from . import trading

srv = MCPServer(
    name="oddsrail",
    instructions=(
        "Prediction-market rail for trading agents. Read tools need no keys. "
        "place_order defaults to dry-run; the operator must set "
        "ODDSRAIL_DRY_RUN=0 and POLYMARKET_PRIVATE_KEY to trade. Prices are "
        "implied probabilities in (0,1). Orders carry the operator's builder "
        "code (ODDSRAIL_BUILDER_CODE) signed into the order for on-chain "
        "attribution. Kalshi tools are prefixed kalshi_ and need the "
        "operator's own API key for private endpoints; Kalshi prices are "
        "probabilities in (0,1) here, translated to its YES-book bid/ask "
        "internally."
    ),
)


def _j(x) -> str:
    return json.dumps(x, default=str)


# Tools are annotated -> str and return json.dumps(...), which makes the SDK
# derive an empty {"result": string} output schema and ship the same payload
# twice (once as text, once as an escaped string in structuredContent). The
# second copy is unusable, so turn it off rather than pay for it.
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                       idempotent_hint=True, open_world_hint=True)
# Trading tools are NOT read-only and NOT idempotent: retrying a place_order
# after a timeout can double a position. Clients use these hints to decide
# what needs confirmation.
TRADE = ToolAnnotations(read_only_hint=False, destructive_hint=True,
                        idempotent_hint=False, open_world_hint=True)


# ------------------------------ market data -------------------------------- #

@srv.tool(description="Search Polymarket markets by text (Gamma public-search "
                      "under the hood); empty query lists open markets. "
                      "Returns token ids, prices, metrics, resolution info.",
           annotations=READ, structured_output=False)
async def search_markets(query: str = "", limit: int = 10) -> str:
    return _j(await pm.search_markets(query=query, limit=limit))


@srv.tool(description="Get one market's details by slug (or id).",
           annotations=READ, structured_output=False)
async def get_market(id_or_slug: str) -> str:
    return _j(await pm.get_market(id_or_slug))


@srv.tool(description="Get the live orderbook (bids/asks) for a CLOB token id.",
           annotations=READ, structured_output=False)
async def get_orderbook(token_id: str) -> str:
    return _j(await pm.get_orderbook(token_id))


@srv.tool(description="Recent price history for a CLOB token id: hours back, "
                      "at fidelity_minutes resolution.",
           annotations=READ, structured_output=False)
async def price_history(token_id: str, hours: float = 6.0,
                        fidelity_minutes: int = 1) -> str:
    times, prices = await pm.price_history(token_id, hours, fidelity_minutes)
    return _j({"points": len(times),
               "series": [[t, p] for t, p in zip(times, prices)]})


@srv.tool(description="Current positions for a wallet address.",
           annotations=READ, structured_output=False)
async def get_positions(address: str, limit: int = 25) -> str:
    return _j(await pm.get_positions(address, limit))


# ------------------------------ signals (premium) --------------------------- #

@srv.tool(description="PREMIUM SIGNAL — overshoot/fade detector. Analyzes a "
                      "token's recent price series for fresh panic jumps and "
                      "reports whether a fade setup is active plus this "
                      "market's historical reversion tendency.",
           annotations=READ, structured_output=False)
async def overshoot_signal(token_id: str, hours: float = 6.0,
                           threshold: float = 0.05,
                           lookback_s: float = 60.0) -> str:
    times, prices = await pm.price_history(token_id, hours, fidelity_minutes=1)
    return _j(signals.overshoot_report(times, prices,
                                       lookback=lookback_s,
                                       threshold=threshold))


@srv.tool(description="PREMIUM SIGNAL — dispute-risk triage. Scores 0-100 how "
                      "likely a market's resolution gets contested (UMA "
                      "dispute risk) with transparent reasons.",
           annotations=READ, structured_output=False)
async def dispute_risk(id_or_slug: str) -> str:
    market = await pm.get_market(id_or_slug, full=True)
    flat = dict(market)
    # V2 model nests resolution info; surface it for the heuristic
    res = market.get("resolution") or {}
    if isinstance(res, dict):
        flat.setdefault("umaResolutionStatus", res.get("uma_resolution_status")
                        or res.get("status"))
    out = signals.dispute_risk(flat)
    out["market"] = {"question": market.get("question"),
                     "slug": market.get("slug")}
    return _j(out)


# ------------------------------ trading ------------------------------------ #

@srv.tool(description="Place a limit order. DRY-RUN by default: returns the "
                      "order it would post. Real trading needs "
                      "ODDSRAIL_DRY_RUN=0 and POLYMARKET_PRIVATE_KEY. The "
                      "operator's builder code is signed into the order. "
                      "price is the implied probability in (0,1); size is in "
                      "SHARES (notional = price * size), and the exchange "
                      "enforces a $1 minimum notional on marketable orders. "
                      "post_only=True rejects rather than crosses the book.",
           annotations=TRADE, structured_output=False)
async def place_order(token_id: str, side: str, price: float, size: float,
                      post_only: bool = False) -> str:
    return _j(await trading.place_order(token_id, side, price, size, post_only))


@srv.tool(description="Cancel an open order by id (respects dry-run).",
           annotations=TRADE, structured_output=False)
async def cancel_order(order_id: str) -> str:
    return _j(await trading.cancel_order(order_id))


@srv.tool(description="List the operator wallet's open orders.",
           annotations=READ, structured_output=False)
async def open_orders() -> str:
    return _j(await trading.open_orders())


# ------------------------------ builder ------------------------------------ #

@srv.tool(description="Builder attribution stats: the public builder "
                      "leaderboard, and (if ODDSRAIL_BUILDER_CODE is set) "
                      "matched trades attributed to this operator's code.",
           annotations=READ, structured_output=False)
async def builder_stats(time_period: str = "WEEK") -> str:
    out = {"leaderboard": await pm.builder_leaderboard(time_period)}
    code = trading.builder_code()
    if code:
        try:
            out["my_trades"] = await pm.builder_trades(code)
        except Exception as e:
            out["my_trades_error"] = str(e)
    else:
        out["note"] = "set ODDSRAIL_BUILDER_CODE to track your attributed flow"
    return _j(out)


# ------------------------------ kalshi (venue #2) --------------------------- #

@srv.tool(description="Search Kalshi markets. Kalshi has no text-search "
                      "endpoint, so this pages open markets and filters on "
                      "title/ticker; auto-generated MVE combo shards are "
                      "excluded. Prices are dollar strings, not cents.",
           annotations=READ, structured_output=False)
async def kalshi_search_markets(query: str = "", limit: int = 10,
                                min_volume: float = 0.0) -> str:
    return _j(await kx.search_markets(query=query, limit=limit,
                                      min_volume=min_volume))


@srv.tool(description="Get one Kalshi market by ticker.",
           annotations=READ, structured_output=False)
async def kalshi_get_market(ticker: str) -> str:
    return _j(await kx.get_market(ticker))


@srv.tool(description="Kalshi orderbook for a ticker, normalised to a YES-book "
                      "bid/ask view (Kalshi publishes bid ladders only; asks "
                      "are derived as 1 - NO bid). Raw ladders included.",
           annotations=READ, structured_output=False)
async def kalshi_get_orderbook(ticker: str, depth: int = 10) -> str:
    return _j(await kx.get_orderbook(ticker, depth))


@srv.tool(description="Recent public trades for a Kalshi ticker.",
           annotations=READ, structured_output=False)
async def kalshi_get_trades(ticker: str, limit: int = 50) -> str:
    return _j(await kx.get_trades(ticker, limit))


@srv.tool(description="Kalshi account balance (needs the operator's API key).",
           annotations=READ, structured_output=False)
async def kalshi_balance() -> str:
    return _j(await kx.get_balance())


@srv.tool(description="Kalshi positions (needs the operator's API key).",
           annotations=READ, structured_output=False)
async def kalshi_positions(limit: int = 50) -> str:
    return _j(await kx.get_positions(limit))


@srv.tool(description="Kalshi resting orders (needs the operator's API key).",
           annotations=READ, structured_output=False)
async def kalshi_open_orders(limit: int = 50) -> str:
    return _j(await kx.open_orders(limit))


@srv.tool(description="Place a Kalshi limit order. State it naturally: "
                      "outcome yes|no, action buy|sell, price = probability of "
                      "THAT outcome in (0,1). Translated to Kalshi's YES-book "
                      "bid/ask internally. DRY-RUN by default.",
           annotations=TRADE, structured_output=False)
async def kalshi_place_order(ticker: str, outcome: str, action: str,
                             price: float, count: float,
                             time_in_force: str = "good_till_cancelled") -> str:
    return _j(await kx.place_order(ticker, outcome, action, price, count,
                                   time_in_force))


@srv.tool(description="Cancel a Kalshi order by id (respects dry-run).",
           annotations=TRADE, structured_output=False)
async def kalshi_cancel_order(order_id: str) -> str:
    return _j(await kx.cancel_order(order_id))


# ------------------------------ cross-venue --------------------------------- #

@srv.tool(description="Search BOTH Polymarket and Kalshi at once and return "
                      "one normalised shape per market: venue, market_id (the "
                      "id that venue's place_order takes), title, yes/no price "
                      "as probabilities in (0,1), best bid/ask, spread, 24h "
                      "volume, close time. Use this instead of the per-venue "
                      "search tools when you do not already know the venue.",
           annotations=READ, structured_output=False)
async def find_markets(query: str, limit: int = 10,
                       venues: str = "both") -> str:
    want = str(venues or "both").lower()
    out = []
    if want in ("both", "polymarket"):
        try:
            for m in await pm.search_markets(query=query, limit=limit):
                out.append(xv.unify_polymarket(m))
        except Exception as e:
            out.append({"venue": "polymarket", "error": str(e)})
    kalshi_scan = None
    if want in ("both", "kalshi"):
        try:
            det = await kx.search_markets_detailed(query=query, limit=limit)
            for m in det["markets"]:
                out.append(xv.unify_kalshi(m))
            kalshi_scan = {k: det[k] for k in
                           ("scanned_markets", "pages_read", "truncated")}
        except Exception as e:
            out.append({"venue": "kalshi", "error": str(e)})
    live = [m for m in out if "error" not in m]
    live.sort(key=lambda m: -(m.get("volume_24h") or 0))
    errs = [m for m in out if "error" in m]
    return _j({"markets": live[: limit * 2], "venue_errors": errs or None,
               "kalshi_scan": kalshi_scan,
               "note": ("prices are implied probabilities in (0,1) on both "
                        "venues; pass market_id to the tool named in "
                        "trade_with. If kalshi_scan.truncated is true, the "
                        "Kalshi side is incomplete — it has no text search, "
                        "so absence there is not evidence of absence.")})


@srv.tool(description="Find markets that may be the SAME event on both "
                      "Polymarket and Kalshi. NOT an arbitrage scanner: "
                      "matches are candidates from title similarity plus a "
                      "close-date check, and a price difference between two "
                      "candidates is not profit. Identical wording does not "
                      "mean identical resolution criteria — read both, and run "
                      "quote_cost on each leg, before acting.",
           annotations=READ, structured_output=False)
async def compare_venues(query: str, limit: int = 10,
                         min_similarity: float = 0.5,
                         max_close_days_apart: int = 7) -> str:
    unified = []
    try:
        for m in await pm.search_markets(query=query, limit=limit):
            unified.append(xv.unify_polymarket(m))
    except Exception as e:
        return _j({"error": f"polymarket search failed: {e}"})
    kalshi_scan = None
    try:
        det = await kx.search_markets_detailed(query=query, limit=limit)
        for m in det["markets"]:
            unified.append(xv.unify_kalshi(m))
        kalshi_scan = {k: det[k] for k in
                       ("scanned_markets", "pages_read", "truncated")}
    except Exception as e:
        return _j({"error": f"kalshi search failed: {e}"})
    pairs = xv.pair_across_venues(unified, min_similarity=min_similarity,
                                  max_close_days_apart=max_close_days_apart)
    return _j({"candidates": pairs[:limit], "candidates_found": len(pairs),
               "searched": {"polymarket": sum(1 for m in unified if m["venue"] == "polymarket"),
                            "kalshi": sum(1 for m in unified if m["venue"] == "kalshi")},
               "kalshi_scan": kalshi_scan,
               "note": ("yes_price_difference > 0 means Kalshi prices YES "
                        "higher. It is a DIFFERENCE, not an edge: cross-venue "
                        "entity resolution is unsolved here, so treat every "
                        "row as a lead to verify by hand, never as a signal to "
                        "trade. Zero candidates usually means no same-event "
                        "listing was found, which is the common case.")})


@srv.tool(description="What a given size would ACTUALLY cost, by walking the "
                      "order book rather than reading the top level. Returns "
                      "average fill price, slippage vs best, notional, and the "
                      "levels consumed — plus the venue's fee schedule where "
                      "it publishes one. Call this before sizing any trade, "
                      "and on both legs before acting on a cross-venue gap.",
           annotations=READ, structured_output=False)
async def quote_cost(venue: str, market_id: str, side: str,
                     size: float) -> str:
    v = str(venue or "").lower()
    sd = str(side or "").upper()
    if sd not in ("BUY", "SELL"):
        return _j({"error": "side must be BUY or SELL"})
    if size <= 0:
        return _j({"error": "size must be positive (number of shares)"})

    if v == "polymarket":
        ob = await pm.get_orderbook(market_id)
        levels = ob["asks"] if sd == "BUY" else ob["bids"]
        walk = xv.walk_book(levels, size)
        fees = {"note": "could not resolve this token to a market"}
        try:
            full = await pm.get_market_by_token(market_id, full=True)
            if full:
                fees = xv.polymarket_fee_note(full)
                fees["market"] = full.get("question")
        except Exception as e:
            fees = {"note": f"fee lookup failed: {e}"}
        return _j({"venue": "polymarket", "market_id": market_id, "side": sd,
                   "cost": walk, "fees": fees,
                   "best_bid": ob.get("best_bid"), "best_ask": ob.get("best_ask"),
                   "min_order_size": ob.get("min_order_size"),
                   "note": "prices are probabilities; notional is in USDC. "
                           "The exchange also enforces a $1 minimum notional "
                           "on marketable orders."})

    if v == "kalshi":
        ob = await kx.get_orderbook(market_id, depth=50)
        levels = ob["yes_asks"] if sd == "BUY" else ob["yes_bids"]
        walk = xv.walk_book(levels, size)
        return _j({"venue": "kalshi", "market_id": market_id, "side": sd,
                   "cost": walk, "fees": xv.KALSHI_FEE_NOTE,
                   "best_yes_bid": ob.get("best_yes_bid"),
                   "best_yes_ask": ob.get("best_yes_ask"),
                   "note": "quoted on the YES book; buying NO at q is selling "
                           "YES at (1-q)."})

    return _j({"error": "venue must be 'polymarket' or 'kalshi'"})


# ------------------------------ lifecycle & discovery ----------------------- #

@srv.tool(description="Status of one Polymarket order: resting, partially "
                      "filled, filled, or gone. The lifecycle answer an agent "
                      "needs after place_order.",
           annotations=READ, structured_output=False)
async def order_status(order_id: str) -> str:
    return _j(await trading.order_status(order_id))


@srv.tool(description="Recent executions (fills) for the operator wallet — "
                      "confirms what actually traded, with tx hashes.",
           annotations=READ, structured_output=False)
async def my_fills(limit: int = 25) -> str:
    return _j(await trading.my_fills(limit))


@srv.tool(description="The operator's current Polymarket positions (uses the "
                      "configured wallet; no address needed).",
           annotations=READ, structured_output=False)
async def my_positions(limit: int = 50) -> str:
    w = trading.operator_wallet()
    if not w:
        return _j({"note": "set POLYMARKET_WALLET_ADDRESS to see positions"})
    return _j({"wallet": w, "positions": await pm.get_positions(w, limit)})


@srv.tool(description="KILL SWITCH — cancel every resting Polymarket order on "
                      "the operator account at once. Use when exposure must "
                      "go to zero fast. Respects dry-run.",
           annotations=TRADE, structured_output=False)
async def cancel_all_orders() -> str:
    return _j(await trading.cancel_all_orders())


@srv.tool(description="READ BEFORE TRUSTING A PRICE: the full resolution "
                      "contract for a market — what exactly resolves YES, who "
                      "resolves it, from which sources. venue is 'polymarket' "
                      "(pass slug or id) or 'kalshi' (pass ticker).",
           annotations=READ, structured_output=False)
async def resolution_criteria(venue: str, market_id: str) -> str:
    v = str(venue or "").lower()
    if v == "polymarket":
        return _j(await pm.resolution_criteria(market_id))
    if v == "kalshi":
        return _j(await kx.resolution_criteria(market_id))
    return _j({"error": "venue must be 'polymarket' or 'kalshi'"})


@srv.tool(description="Markets closing within N hours on either venue, by "
                      "volume — where trading activity concentrates.",
           annotations=READ, structured_output=False)
async def closing_soon(hours: float = 24.0, limit: int = 10,
                       venues: str = "both") -> str:
    want = str(venues or "both").lower()
    out = {}
    if want in ("both", "polymarket"):
        try:
            out["polymarket"] = await pm.closing_soon(hours, limit)
        except Exception as e:
            out["polymarket_error"] = str(e)[:200]
    if want in ("both", "kalshi"):
        try:
            out["kalshi"] = await kx.closing_soon(hours, limit)
        except Exception as e:
            out["kalshi_error"] = str(e)[:200]
    return _j(out)



@srv.tool(description="Server status: dry-run state, attribution config, "
                      "and which capabilities are enabled.",
           annotations=READ, structured_output=False)
def server_info() -> str:
    return _j({
        "name": "oddsrail",
        "version": "0.6.0",
        "dry_run": trading.dry_run(),
        "trading_key_configured": bool(os.environ.get("POLYMARKET_PRIVATE_KEY")),
        "builder_code_configured": bool(trading.builder_code()),
        "builder_code_source": trading.builder_code_source(),
        "attribution": "on-chain (CLOB V2 builder field)",
        "custody": "none — self-hosted, keys stay local",
        "venues": {
            "polymarket": {"attribution": "on-chain builder code",
                           "trading_key": bool(os.environ.get("POLYMARKET_PRIVATE_KEY"))},
            "kalshi": {"attribution": "none available on REST",
                       "credentials": kx.has_credentials(),
                       "environment": "demo" if os.environ.get("KALSHI_DEMO") else "production"},
        },
        "premium_tools": ["overshoot_signal", "dispute_risk"],
    })


def main() -> None:
    srv.run(transport="stdio")


if __name__ == "__main__":
    main()
