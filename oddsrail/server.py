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


@srv.tool(description="Server status: dry-run state, attribution config, "
                      "and which capabilities are enabled.",
           annotations=READ, structured_output=False)
def server_info() -> str:
    return _j({
        "name": "oddsrail",
        "version": "0.4.0",
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
