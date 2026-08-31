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

from . import audit as au
from . import crossvenue as xv
from . import kalshi as kx
from . import polymarket as pm
from . import signals
from . import trading

VERSION = "0.8.0"

srv = MCPServer(
    name="oddsrail",
    version=VERSION,
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


def _err(e: Exception, hint: str = "") -> str:
    """Turn an exception into something an agent can act on.

    The MCP layer otherwise surfaces only "Error executing tool <name>" — no
    status, no URL, no cause — which an agent cannot self-correct from, so it
    retries blindly.
    """
    out = {"error": str(e) or type(e).__name__, "error_type": type(e).__name__}
    resp = getattr(e, "response", None)
    if resp is not None:
        out["http_status"] = getattr(resp, "status_code", None)
        req = getattr(e, "request", None)
        if req is not None:
            out["url"] = str(getattr(req, "url", ""))
    if hint:
        out["hint"] = hint
    return _j(out)


# ------------------------------ market data -------------------------------- #

@srv.tool(description="Search Polymarket markets by text (Gamma public-search "
                      "under the hood); empty query lists open markets. "
                      "Returns token ids, prices, metrics, resolution info.",
           annotations=READ, structured_output=False)
async def search_markets(query: str = "", limit: int = 10) -> str:
    try:
        return _j(await pm.search_markets(query=query, limit=limit))
    except Exception as e:
        return _err(e, "check the query; an empty query lists open markets")


@srv.tool(description="Get one market's details by slug (or id).",
           annotations=READ, structured_output=False)
async def get_market(id_or_slug: str) -> str:
    try:
        return _j(await pm.get_market(id_or_slug))
    except Exception as e:
        return _err(e, "no market with that slug or id — run search_markets first "
                      "and use the `slug` field")


@srv.tool(description="Get the live orderbook (bids/asks) for a CLOB token id.",
           annotations=READ, structured_output=False)
async def get_orderbook(token_id: str) -> str:
    try:
        return _j(await pm.get_orderbook(token_id))
    except Exception as e:
        return _err(e, "token_id is the long numeric CLOB id from a market's "
                      "outcomes.yes.token_id — not the slug or condition_id")


@srv.tool(description="Recent price history for a CLOB token id: hours back, "
                      "at fidelity_minutes resolution.",
           annotations=READ, structured_output=False)
async def price_history(token_id: str, hours: float = 6.0,
                        fidelity_minutes: int = 1) -> str:
    try:
        times, prices = await pm.price_history(token_id, hours, fidelity_minutes)
    except Exception as e:
        return _err(e, "token_id is the numeric CLOB id from outcomes.yes.token_id")
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
    own = bool(os.environ.get("ODDSRAIL_BUILDER_CODE"))
    if code:
        key = "my_trades" if own else "bundled_default_code_trades"
        try:
            out[key] = await pm.builder_trades(code)
        except Exception as e:
            out[f"{key}_error"] = str(e)
        if not own:
            out["note"] = ("these trades belong to the BUNDLED oddsrail builder "
                           "code, not to you — they are whatever flow the "
                           "default has attributed project-wide. Set "
                           "ODDSRAIL_BUILDER_CODE to your own code to track "
                           "yours.")
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
    if want not in ("both", "polymarket", "kalshi"):
        return _j({"error": f"unknown venues={venues!r}",
                   "valid": ["both", "polymarket", "kalshi"]})
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
    if want not in ("both", "polymarket", "kalshi"):
        return _j({"error": f"unknown venues={venues!r}",
                   "valid": ["both", "polymarket", "kalshi"]})
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



# ------------------------------ audit & sizing ------------------------------ #

@srv.tool(description="Settlement-divergence audit for a cross-venue pair, on "
                      "LIVE data (no pre-curated pair list). Compares close "
                      "times, resolution sources, UMA dispute status and market "
                      "structure, and returns ok / caution / block with "
                      "reasons. Run this before treating any cross-venue price "
                      "difference as an edge.",
           annotations=READ, structured_output=False)
async def settlement_audit(polymarket_id: str, kalshi_ticker: str,
                           notional_usd: float = 0.0) -> str:
    try:
        pm_rc = await pm.resolution_criteria(polymarket_id)
    except Exception as e:
        return _j({"error": f"could not load Polymarket leg: {e}"})
    try:
        kx_rc = await kx.resolution_criteria(kalshi_ticker)
    except Exception as e:
        return _j({"error": f"could not load Kalshi leg: {e}"})
    return _j(au.audit_pair(pm_rc, kx_rc, notional_usd or None))


@srv.tool(description="Fractional-Kelly position size for a binary contract, "
                      "given your bankroll, the market price, and YOUR fair "
                      "value estimate. Caps at a fraction of full Kelly and "
                      "refuses negative-edge bets. Returns its assumptions — "
                      "subtract quote_cost before trusting the number.",
           annotations=READ, structured_output=False)
def position_size(bankroll_usd: float, price: float, fair_value: float,
                  max_fraction_of_kelly: float = 0.25) -> str:
    return _j(au.kelly_size(bankroll_usd, price, fair_value,
                            max_fraction_of_kelly))


# ------------------------------ workflow prompts ---------------------------- #
# Prompts surface in MCP clients as ready-made workflows. They encode the
# ORDER of operations that keeps an agent out of trouble — the sequencing is
# the expertise, and a tool list alone does not convey it.

@srv.prompt(description="Find and evaluate a fade (mean-reversion) setup on "
                        "prediction markets, end to end.")
def find_fade_setup(query: str = "", bankroll_usd: str = "100") -> str:
    return f"""Find a fade setup on prediction markets{f' related to: {query}' if query else ''}.

Work in this order and stop if a step fails:
1. find_markets({query!r}) — pick liquid candidates (volume_24h > 10000).
2. For each candidate, overshoot_signal(token_id) — you want
   fade_setup_active=true AND median_reversion_120s > 0.2. If
   median_reversion_120s is null, the series had no measurable history:
   treat that as no signal, not as a weak one.
3. get_orderbook(token_id) — confirm the book is two-sided. bids/asks are
   already best-first here.
4. quote_cost(venue, market_id, side, size) — the reversion must beat
   slippage + fees, not just the headline move. Most setups die here.
5. resolution_criteria — a market that resolves ambiguously can strand the
   position regardless of price action.
6. position_size(bankroll_usd={bankroll_usd}, price, fair_value) where
   fair_value is the pre-jump price if you believe it fully reverts.
7. place_order(...) — dry-run first and read back the intent before setting
   ODDSRAIL_DRY_RUN=0.

Report the candidates you rejected and why; a rejected setup is a result."""


@srv.prompt(description="Check whether a cross-venue price difference is a "
                        "real edge or a settlement mismatch.")
def check_cross_venue_edge(query: str) -> str:
    return f"""Investigate whether {query!r} offers a genuine cross-venue edge.

1. compare_venues({query!r}) — candidates only. It returns nothing for most
   queries, which is the honest answer, not a failure.
2. For any candidate: settlement_audit(polymarket_id, kalshi_ticker).
   A 'block' verdict ends it — the legs do not hedge each other and the
   price difference is not an edge.
3. On 'ok' or 'caution': quote_cost on BOTH legs at the size you would
   actually trade. Cross-venue differences are usually smaller than the
   combined slippage.
4. Read resolution_criteria on both sides yourself. Identical wording does
   not mean identical settlement.
5. Only then size the trade.

State plainly if the conclusion is 'no edge here' — that is the usual and
correct outcome."""


@srv.prompt(description="Daily review of open exposure: positions, resting "
                        "orders, recent fills, attribution.")
def daily_review() -> str:
    return """Review current prediction-market exposure.

1. my_positions() — what is held, and at what marks.
2. open_orders() — what is still resting; for anything stale, order_status()
   to see whether it partially filled.
3. my_fills(limit=25) — what actually executed since the last review.
4. closing_soon(hours=24) — positions or orders in markets about to resolve
   need a decision now.
5. builder_stats() — confirm routed flow is being attributed.

Flag: resting orders far from the current book, positions in markets with an
open UMA dispute (dispute_risk), and anything resolving within 24h."""



@srv.tool(description="Server status: dry-run state, attribution config, "
                      "and which capabilities are enabled.",
           annotations=READ, structured_output=False)
def server_info() -> str:
    return _j({
        "name": "oddsrail",
        "version": VERSION,
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


USAGE = f"""oddsrail {VERSION} — MCP server for AI agents trading prediction
markets (Polymarket + Kalshi).

This is NOT an interactive CLI. It speaks the Model Context Protocol over
stdio, so it is meant to be launched by an MCP client:

    claude mcp add --transport stdio oddsrail -- oddsrail

Then ask your agent: "find markets about the world cup and quote the cost of
100 shares on the favourite".

Environment:
  ODDSRAIL_DRY_RUN          1 (default) simulates orders and never sends them.
                            Only "0", "false" or "no" enable real trading.
  ODDSRAIL_BUILDER_CODE     your Polymarket builder code; overrides the
                            bundled default so attribution accrues to you.
  POLYMARKET_PRIVATE_KEY    required only for real trading. Never leaves this
                            machine.
  POLYMARKET_WALLET_ADDRESS proxy/deposit wallet, if your account uses one.
  KALSHI_KEY_ID             Kalshi API key id (private endpoints only).
  KALSHI_PRIVATE_KEY_PATH   PKCS#8 PEM for Kalshi request signing.
  KALSHI_DEMO=1             use Kalshi's demo environment.

Read-only tools need no credentials at all.
Docs: https://github.com/hmesutozsoy/oddsrail
"""


def main() -> None:
    import sys
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(USAGE)
        return
    if any(a in ("-V", "--version") for a in args):
        print(f"oddsrail {VERSION}")
        return
    if args:
        print(f"oddsrail: unrecognised argument {args[0]!r}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)
    # A bare run on a terminal is almost always someone expecting a CLI; say so
    # on stderr (never stdout — that carries the JSON-RPC stream) and continue,
    # since a client may still be piping us stdio.
    if sys.stdin.isatty():
        print(f"oddsrail {VERSION}: MCP stdio server, waiting for a client on "
              f"stdin.\nNot an interactive CLI — see `oddsrail --help`.",
              file=sys.stderr)
    srv.run(transport="stdio")


if __name__ == "__main__":
    main()
