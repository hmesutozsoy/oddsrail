"""oddsrail — the rail AI agents use to trade prediction markets.

MCP server exposing Polymarket market data, order routing with on-chain
builder-code attribution (CLOB V2), and signal tools (overshoot, dispute
risk). Self-hosted and non-custodial: keys never leave the machine.

Run (stdio, for any MCP-compatible AI client):
    .venv/bin/python -m oddsrail.server
"""

import json
import os
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import audit as au
from . import check as ck
from . import crossvenue as xv
from . import geo
from . import guard
from . import hosted
from . import paper
from . import kalshi as kx
from . import polymarket as pm
from . import signals
from . import trading

VERSION = "0.20.0rc2"

# Said in both profiles: what the outputs are, who decides, and who we are not.
DISCLOSURE = (
    "Analysis, signal and sizing outputs are informational and are not "
    "financial advice; the user decides every order. OddsRail is independent "
    "and is not affiliated with or endorsed by Polymarket or Kalshi."
)

HOSTED_INSTRUCTIONS = (
    "Hosted oddsrail: Polymarket market data, signals, deterministic order "
    "checks (check_order) and PAPER trading, per signed-in account. These "
    "tools use no wallet keys and never send a real order: place_order fills "
    "against the live book into your account's paper ledger and "
    "paper_positions shows the P&L. Prices are implied probabilities in "
    "(0,1). Kalshi is not served here (self-host for it). Live trading is "
    "self-hosted: pip install oddsrail. Call server_info to see the "
    "signed-in account and the server's venue reachability. " + DISCLOSURE
)


def _auth_kwargs() -> dict:
    """OAuth resource-server + authorization-server wiring, hosted mode only."""
    if not hosted.enabled():
        return {}
    try:
        from .cloud.auth import server_auth_kwargs
    except ImportError as exc:  # the public package ships without oddsrail.cloud
        raise RuntimeError(
            "ODDSRAIL_HOSTED=1 needs the hosted service (oddsrail.cloud), which is "
            "developed in a separate private repository and is not part of the "
            "published oddsrail package. Unset ODDSRAIL_HOSTED to run the MCP "
            "server locally.") from exc
    return server_auth_kwargs()


srv = MCPServer(
    name="oddsrail",
    version=VERSION,
    **_auth_kwargs(),
    instructions=HOSTED_INSTRUCTIONS if hosted.enabled() else (
        "Prediction-market rail for trading agents. Read tools need no keys. "
        "place_order defaults to dry-run; the operator must set "
        "ODDSRAIL_DRY_RUN=0 and POLYMARKET_PRIVATE_KEY to trade. Prices are "
        "implied probabilities in (0,1). Orders carry OddsRail's own builder "
        "code by default (0 bps, so no fee is added), signed into the order "
        "for on-chain attribution; when the operator sets "
        "ODDSRAIL_BUILDER_CODE, their code is used instead. Kalshi tools are "
        "prefixed kalshi_ and need the "
        "operator's own API key for private endpoints; Kalshi prices are "
        "probabilities in (0,1) here, translated to its YES-book bid/ask "
        "internally. Both venues restrict trading by jurisdiction and read "
        "tools work from restricted places, so a rejection can arrive at the "
        "order rather than the connection. Call server_info before the first "
        "order of a session to see the operator's Polymarket geoblock "
        "verdict and venue reachability. split_position / merge_positions / "
        "redeem_positions are gasless via Polymarket's relayer and need the "
        "operator's own POLYMARKET_RELAYER_API_KEY (+_ADDRESS); they respect "
        "dry-run and never fall back to a gas-paying broadcast. In dry-run, "
        "Polymarket orders are papered against the live book and "
        "paper_positions shows the simulated P&L. Operator guardrails "
        "(per-order and per-session notional caps, allowed markets) are "
        "enforced before any request and cannot be changed by the agent. "
        + DISCLOSURE
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
# Pure local arithmetic: no network, no state, same answer every time.
LOCAL_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                             idempotent_hint=True, open_world_hint=False)
# Trading tools are NOT read-only and NOT idempotent: retrying a place_order
# after a timeout can double a position. Clients use these hints to decide
# what needs confirmation.
TRADE = ToolAnnotations(read_only_hint=False, destructive_hint=True,
                        idempotent_hint=False, open_world_hint=True)
# Wipes the paper ledger and nothing else; a second reset changes nothing more.
LEDGER_RESET = ToolAnnotations(read_only_hint=False, destructive_hint=True,
                               idempotent_hint=True, open_world_hint=False)
# Reads the live book, fills crossed resting paper orders and saves the ledger.
LEDGER_SETTLE = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                idempotent_hint=False, open_world_hint=True)

# geo's "unreachable" hint ends by sending the agent to open_orders, which the
# hosted connector hides, and asks the user to check DNS from a machine that
# is ours. Hosted orders are paper, so there is nothing to look up either.
HOSTED_UNREACHABLE_HINT = (
    "Could not get a response from {host}: the connection from the hosted "
    "server failed (DNS, TLS, reset, or refused). That is a network problem "
    "between the server and the venue, not anything about the request. Do "
    "not retry in a loop; try again later. Orders on this connector are "
    "paper only and never reach the venue."
)


def _err_obj(e: Exception, hint: str = "", host: str = "") -> dict:
    """Turn an exception into something an agent can act on.

    The MCP layer otherwise surfaces only "Error executing tool <name>" — no
    status, no URL, no cause — which an agent cannot self-correct from, so it
    retries blindly. Two exception families carry their status differently:
    httpx puts it on e.response.status_code, while the Polymarket SDK's
    RequestRejectedError exposes e.status/e.code and has NEITHER .response
    NOR .request — probing only the first silently drops the status.
    """
    out = {"error": str(e) or type(e).__name__, "error_type": type(e).__name__}
    status = None
    resp = getattr(e, "response", None)
    if resp is not None:                                  # httpx-shaped
        status = getattr(resp, "status_code", None)
    elif getattr(e, "status", None) is not None:          # Polymarket SDK-shaped
        status = e.status
        if getattr(e, "code", None):
            out["venue_code"] = e.code
    if status is not None:
        out["http_status"] = status
    try:
        # httpx's .request PROPERTY raises when unset — getattr's default
        # only covers AttributeError, so guard explicitly.
        req = getattr(e, "request", None)
    except Exception:
        req = None
    if req is not None:
        out["url"] = str(getattr(req, "url", ""))
    elif getattr(e, "url", None):                         # InterceptedResponseError
        out["url"] = str(e.url)
    h = host.lower()
    venue = "kalshi" if "kalshi" in h else "polymarket" if "polymarket" in h else None
    cls = geo.classify(e, status, venue)
    if cls:
        out["failure_class"] = cls
        text = (HOSTED_UNREACHABLE_HINT if cls == "unreachable" and hosted.enabled()
                else geo.HINTS[cls])
        out["hint"] = text.format(host=host or "the venue")
    elif getattr(e, "hint", None):
        out["hint"] = e.hint
    elif hint:
        out["hint"] = hint
    return out


def _err(e: Exception, hint: str = "", host: str = "") -> str:
    return _j(_err_obj(e, hint, host))


# ------------------------------ market data -------------------------------- #

@srv.tool(description="Search Polymarket markets by text (Gamma public-search "
                      "under the hood); empty query lists open markets. "
                      "Returns token ids, prices, metrics, resolution info.",
           title="Search Polymarket markets",
           annotations=READ, structured_output=False)
async def search_markets(query: str = "", limit: int = 10) -> str:
    try:
        return _j(await pm.search_markets(query=query, limit=limit))
    except Exception as e:
        return _err(e, "check the query; an empty query lists open markets",
                    host="the Polymarket API")


@srv.tool(description="Get one market's details. Accepts the market slug, "
                      "the Gamma market id, or either of its CLOB token ids "
                      "(the market_id that find_markets and search_markets "
                      "return), so a token id from a search can be passed "
                      "straight in.",
           title="Get a Polymarket market",
           annotations=READ, structured_output=False)
async def get_market(id_or_slug: str) -> str:
    try:
        return _j(await pm.get_market(id_or_slug))
    except Exception as e:
        return _err(e, "no market with that slug or id: run search_markets first "
                      "and use the `slug` field", host="the Polymarket API")


@srv.tool(description="Get the live orderbook (bids/asks) for a CLOB token id.",
           title="Polymarket order book",
           annotations=READ, structured_output=False)
async def get_orderbook(token_id: str) -> str:
    try:
        return _j(await pm.get_orderbook(token_id))
    except Exception as e:
        return _err(e, "token_id is the long numeric CLOB id from a market's "
                      "outcomes.yes.token_id, not the slug or condition_id",
                    host="the Polymarket CLOB")


@srv.tool(description="Recent price history for a CLOB token id: hours back, "
                      "at fidelity_minutes resolution.",
           title="Polymarket price history",
           annotations=READ, structured_output=False)
async def price_history(token_id: str, hours: float = 6.0,
                        fidelity_minutes: int = 1) -> str:
    try:
        times, prices = await pm.price_history(token_id, hours, fidelity_minutes)
    except Exception as e:
        return _err(e, "token_id is the numeric CLOB id from outcomes.yes.token_id",
                    host="the Polymarket CLOB")
    return _j({"points": len(times),
               "series": [[t, p] for t, p in zip(times, prices)]})


@srv.tool(description="Current positions for a wallet address.",
           title="Polymarket positions for a wallet",
           annotations=READ, structured_output=False)
async def get_positions(address: str, limit: int = 25) -> str:
    try:
        return _j(await pm.get_positions(address, limit))
    except Exception as e:
        return _err(e, "address is the 0x wallet address whose positions you want",
                    host="the Polymarket data API")


# ------------------------------ signals ------------------------------------ #

@srv.tool(description="Overshoot and fade detector. Analyzes a token's recent "
                      "price series for fresh panic jumps and reports whether "
                      "a fade setup is active, plus the median reversion of "
                      "earlier jumps in the same window.",
           title="Polymarket overshoot signal",
           annotations=READ, structured_output=False)
async def overshoot_signal(token_id: str, hours: float = 6.0,
                           threshold: float = 0.05,
                           lookback_s: float = 60.0) -> str:
    try:
        times, prices = await pm.price_history(token_id, hours,
                                               fidelity_minutes=1)
    except Exception as e:
        return _err(e, "token_id is the numeric CLOB id from outcomes.yes.token_id",
                    host="the Polymarket CLOB")
    return _j(signals.overshoot_report(times, prices,
                                       lookback=lookback_s,
                                       threshold=threshold))


@srv.tool(description="Dispute-risk triage: a heuristic 0-100 score for how "
                      "likely a market's resolution is to be contested (UMA "
                      "dispute risk), with the reasons behind it. Takes the "
                      "market slug, the Gamma id, or a CLOB token id (the "
                      "market_id a search returns).",
           title="Polymarket dispute risk",
           annotations=READ, structured_output=False)
async def dispute_risk(id_or_slug: str) -> str:
    try:
        market = await pm.get_market(id_or_slug, full=True)
    except Exception as e:
        return _err(e, "no market with that slug or id: run search_markets first",
                    host="the Polymarket API")
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

# check.py matches these words to the market title and reads YES/NO from them,
# so a paraphrase of the order is all it needs; nothing else from the chat.
INTENT_DESC = ("A short paraphrase of the order the user asked for, naming the "
               "market and the outcome (YES or NO), e.g. 'buy YES on Spain to "
               "win the World Cup'. Size and price go in their own parameters. "
               "Include no other conversation content.")


@srv.tool(description="Pre-trade check, to run before place_order: "
                      "deterministic verification of a proposed order against "
                      "the order the user asked for, the live market and the "
                      "guardrails. Pass a short paraphrase of the requested "
                      "order as `intent` (the market and the outcome) and no "
                      "other conversation content. Returns ok / "
                      "caution / block with the evidence per check (market "
                      "exists and is open, intent matches the market and the "
                      "YES/NO side, price is sane vs the book, size and $1 "
                      "minimum, guardrails, liquidity, resolution source) "
                      "plus a one-line read-back. No model judges anything; "
                      "nothing is sent.",
           title="Pre-trade order check",
           annotations=READ, structured_output=False)
async def check_order(venue: str, market_id: str, side: str, price: float,
                      size: float, intent: Annotated[str, Field(description=INTENT_DESC)] = "",
                      outcome: str = "") -> str:
    if hosted.enabled() and str(venue).lower() == "kalshi":
        return _j({"verdict": "block", "error": hosted.KALSHI_NOTE})
    try:
        return _j(await ck.check_order(venue, market_id, side, price, size, intent, outcome))
    except Exception as e:
        return _err(e, "market_id is the Polymarket token id" if hosted.enabled()
                    else "market_id is the Polymarket token id or the Kalshi ticker",
                    host="the venue")


@srv.tool(description="Place a limit order. DRY-RUN by default: returns the "
                      "order it would post. Real trading needs "
                      "ODDSRAIL_DRY_RUN=0 and POLYMARKET_PRIVATE_KEY. The "
                      "order carries OddsRail's own 0 bps builder code (no "
                      "fee added) for attribution, or the operator's code "
                      "when ODDSRAIL_BUILDER_CODE is set. "
                      "price is the implied probability in (0,1); size is in "
                      "SHARES (notional = price * size), and the exchange "
                      "enforces a $1 minimum notional on marketable orders. "
                      "post_only=True rejects rather than crosses the book.",
           title="Place a Polymarket order",
           annotations=TRADE, structured_output=False)
async def place_order(token_id: str, side: str, price: float, size: float,
                      post_only: bool = False) -> str:
    try:
        return _j(await trading.place_order(token_id, side, price, size,
                                            post_only))
    except Exception as e:
        # trading.place_order structures its own network failures; what can
        # reach here is input validation, config, or response processing.
        return _err(e, "order NOT confirmed either way: call open_orders to "
                       "see whether anything rests before retrying",
                    host="the Polymarket CLOB")


@srv.tool(description="Cancel an open order by id (respects dry-run).",
           title="Cancel a Polymarket order",
           annotations=TRADE, structured_output=False)
async def cancel_order(order_id: str) -> str:
    try:
        return _j(await trading.cancel_order(order_id))
    except Exception as e:
        return _err(e, "cancellation not confirmed: call open_orders to see "
                       "whether it still rests", host="the Polymarket CLOB")


@srv.tool(description="List the operator wallet's open orders.",
           title="Open Polymarket orders",
           annotations=READ, structured_output=False)
async def open_orders() -> str:
    try:
        return _j(await trading.open_orders())
    except Exception as e:
        return _err(e, host="the Polymarket CLOB")


# ------------------------------ builder ------------------------------------ #

@srv.tool(description="Builder attribution stats: the public builder "
                      "leaderboard, plus matched trades for the builder code "
                      "in use (the operator's when ODDSRAIL_BUILDER_CODE is "
                      "set, otherwise OddsRail's own default code).",
           title="Polymarket builder stats",
           annotations=READ, structured_output=False)
async def builder_stats(time_period: str = "WEEK") -> str:
    try:
        out = {"leaderboard": await pm.builder_leaderboard(time_period), "volume_unit": "shares"}
    except Exception as e:
        return _err(e, host="the Polymarket API")
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
                           "code, not to you: they are whatever flow the "
                           "default has attributed project-wide. Set "
                           "ODDSRAIL_BUILDER_CODE to your own code to track "
                           "yours.")
    return _j(out)


@srv.tool(description="Attribution ledger for the builder code in use: every "
                      "trade carrying it, aggregated per Sunday-start week and "
                      "per wallet, with the maintainer's own wallets split out "
                      "into an honest 'external' line. Built from Polymarket's "
                      "own public builder feed, so any figure it reports can be "
                      "checked at data-api.polymarket.com/v2/builders/leaderboard.",
           title="Builder attribution ledger",
           annotations=READ, structured_output=False)
async def attribution_ledger() -> str:
    code = trading.builder_code()
    if not code:
        return _j({"note": "no builder code configured"})
    try:
        rows = await pm.builder_trades_all(code)
    except Exception as e:
        return _err(e, host="the Polymarket CLOB")
    return _j(pm.aggregate_ledger(rows, trading.maintainer_wallets(), code))


# ------------------------------ kalshi (venue #2) --------------------------- #

@srv.tool(description="Search Kalshi markets. Kalshi has no text-search "
                      "endpoint, so this pages open markets and filters on "
                      "title/ticker; auto-generated MVE combo shards are "
                      "excluded. Prices are dollar strings, not cents.",
           title="Search Kalshi markets",
           annotations=READ, structured_output=False)
async def kalshi_search_markets(query: str = "", limit: int = 10,
                                min_volume: float = 0.0) -> str:
    try:
        return _j(await kx.search_markets(query=query, limit=limit,
                                          min_volume=min_volume))
    except Exception as e:
        return _err(e, host="the Kalshi API")


@srv.tool(description="Get one Kalshi market by ticker.",
           title="Get a Kalshi market",
           annotations=READ, structured_output=False)
async def kalshi_get_market(ticker: str) -> str:
    try:
        return _j(await kx.get_market(ticker))
    except Exception as e:
        return _err(e, "ticker comes from kalshi_search_markets or "
                       "find_markets", host="the Kalshi API")


@srv.tool(description="Kalshi orderbook for a ticker, normalised to a YES-book "
                      "bid/ask view (Kalshi publishes bid ladders only; asks "
                      "are derived as 1 - NO bid). Raw ladders included.",
           title="Kalshi order book",
           annotations=READ, structured_output=False)
async def kalshi_get_orderbook(ticker: str, depth: int = 10) -> str:
    try:
        return _j(await kx.get_orderbook(ticker, depth))
    except Exception as e:
        return _err(e, "ticker comes from kalshi_search_markets or "
                       "find_markets", host="the Kalshi API")


@srv.tool(description="Recent public trades for a Kalshi ticker.",
           title="Recent Kalshi trades",
           annotations=READ, structured_output=False)
async def kalshi_get_trades(ticker: str, limit: int = 50) -> str:
    try:
        return _j(await kx.get_trades(ticker, limit))
    except Exception as e:
        return _err(e, "ticker comes from kalshi_search_markets or "
                       "find_markets", host="the Kalshi API")


@srv.tool(description="Kalshi account balance (needs the operator's API key).",
           title="Kalshi account balance",
           annotations=READ, structured_output=False)
async def kalshi_balance() -> str:
    try:
        return _j(await kx.get_balance())
    except Exception as e:
        return _err(e, "needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH",
                    host="the Kalshi API")


@srv.tool(description="Kalshi positions (needs the operator's API key).",
           title="Kalshi positions",
           annotations=READ, structured_output=False)
async def kalshi_positions(limit: int = 50) -> str:
    try:
        return _j(await kx.get_positions(limit))
    except Exception as e:
        return _err(e, "needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH",
                    host="the Kalshi API")


@srv.tool(description="Kalshi resting orders (needs the operator's API key).",
           title="Open Kalshi orders",
           annotations=READ, structured_output=False)
async def kalshi_open_orders(limit: int = 50) -> str:
    try:
        return _j(await kx.open_orders(limit))
    except Exception as e:
        return _err(e, "needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH",
                    host="the Kalshi API")


@srv.tool(description="Place a Kalshi limit order. State it naturally: "
                      "outcome yes|no, action buy|sell, price = probability of "
                      "THAT outcome in (0,1). Translated to Kalshi's YES-book "
                      "bid/ask internally. DRY-RUN by default.",
           title="Place a Kalshi order",
           annotations=TRADE, structured_output=False)
async def kalshi_place_order(ticker: str, outcome: str, action: str,
                             price: float, count: float,
                             time_in_force: str = "good_till_cancelled") -> str:
    try:
        return _j(await kx.place_order(ticker, outcome, action, price, count,
                                       time_in_force))
    except Exception as e:
        return _err(e, "order not confirmed: call kalshi_open_orders to see "
                       "whether anything rests before retrying",
                    host="the Kalshi API")


@srv.tool(description="Cancel a Kalshi order by id (respects dry-run).",
           title="Cancel a Kalshi order",
           annotations=TRADE, structured_output=False)
async def kalshi_cancel_order(order_id: str) -> str:
    try:
        return _j(await kx.cancel_order(order_id))
    except Exception as e:
        return _err(e, "cancellation not confirmed: call kalshi_open_orders "
                       "to see whether it still rests", host="the Kalshi API")


# ------------------------------ cross-venue --------------------------------- #

@srv.tool(description="Search BOTH Polymarket and Kalshi at once and return "
                      "one normalised shape per market: venue, market_id (the "
                      "id that venue's place_order takes), title, yes/no price "
                      "as probabilities in (0,1), best bid/ask, spread, 24h "
                      "volume, close time. Use this instead of the per-venue "
                      "search tools when you do not already know the venue.",
           title="Find markets across venues",
           annotations=READ, structured_output=False)
async def find_markets(query: str, limit: int = 10,
                       venues: str = "both") -> str:
    want = str(venues or "both").lower()
    if want not in ("both", "polymarket", "kalshi"):
        return _j({"error": f"unknown venues={venues!r}",
                   "valid": ["both", "polymarket", "kalshi"]})
    want, hosted_note = hosted.venues(want)
    if not want:
        return _j({"error": hosted_note, "valid": ["polymarket"]})
    out = []
    if want in ("both", "polymarket"):
        try:
            for m in await pm.search_markets(query=query, limit=limit):
                out.append(xv.unify_polymarket(m))
        except Exception as e:
            out.append({"venue": "polymarket",
                        **_err_obj(e, host="the Polymarket API")})
    kalshi_scan = None
    if want in ("both", "kalshi"):
        try:
            det = await kx.search_markets_detailed(query=query, limit=limit)
            for m in det["markets"]:
                out.append(xv.unify_kalshi(m))
            kalshi_scan = {k: det[k] for k in
                           ("scanned_markets", "pages_read", "truncated")}
        except Exception as e:
            out.append({"venue": "kalshi",
                        **_err_obj(e, host="the Kalshi API")})
    live = [m for m in out if "error" not in m]
    live.sort(key=lambda m: -(m.get("volume_24h") or 0))
    errs = [m for m in out if "error" in m]
    if want == "polymarket":
        note = ("prices are implied probabilities in (0,1); pass market_id "
                "to the tool named in trade_with.")
    else:
        note = ("prices are implied probabilities in (0,1) on both "
                "venues; pass market_id to the tool named in "
                "trade_with. If kalshi_scan.truncated is true, the "
                "Kalshi side is incomplete: it has no text search, "
                "so absence there is not evidence of absence.")
    # An outage must never read as "no such market exists" — this is the tool
    # the server tells agents to prefer, so say it loudly, first. "Every venue
    # errored" is only claimable when it is true: one venue erroring while the
    # other legitimately returns nothing is a PARTIAL result, not an outage.
    venues_queried = (want == "both") + 1
    if len(errs) == venues_queried:
        note = ("NO MARKETS RETURNED BECAUSE EVERY QUERIED VENUE ERRORED: "
                "this is a failed search, not an empty result. See "
                "venue_errors, and call server_info for reachability. Do not "
                "report to the operator that no such market exists. " + note)
    elif errs:
        missing = ", ".join(sorted({str(m.get("venue")) for m in errs}))
        note = (f"PARTIAL RESULT: the {missing} side errored and its markets "
                "are missing, so absence from this list is not evidence of "
                "absence. " + note)
    return _j({"markets": live[: limit * 2], "venue_errors": errs or None,
               "kalshi_scan": kalshi_scan,
               "note": (hosted_note + " " + note) if hosted_note else note})


@srv.tool(description="Find markets that may be the SAME event on both "
                      "Polymarket and Kalshi. NOT an arbitrage scanner: "
                      "matches are candidates from title similarity plus a "
                      "close-date check, and a price difference between two "
                      "candidates is not profit. Identical wording does not "
                      "mean identical resolution criteria. Read both, and run "
                      "quote_cost on each leg, before acting.",
           title="Cross-venue market candidates",
           annotations=READ, structured_output=False)
async def compare_venues(query: str, limit: int = 10,
                         min_similarity: float = 0.5,
                         max_close_days_apart: int = 7) -> str:
    unified = []
    try:
        for m in await pm.search_markets(query=query, limit=limit):
            unified.append(xv.unify_polymarket(m))
    except Exception as e:
        return _j({"stage": "polymarket search failed",
                   **_err_obj(e, host="the Polymarket API")})
    kalshi_scan = None
    try:
        det = await kx.search_markets_detailed(query=query, limit=limit)
        for m in det["markets"]:
            unified.append(xv.unify_kalshi(m))
        kalshi_scan = {k: det[k] for k in
                       ("scanned_markets", "pages_read", "truncated")}
    except Exception as e:
        return _j({"stage": "kalshi search failed",
                   **_err_obj(e, host="the Kalshi API")})
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
                      "levels consumed, plus the venue's fee schedule where "
                      "it publishes one. Call this before sizing any trade, "
                      "and on both legs before acting on a cross-venue gap.",
           title="Quote the cost of an order",
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
        try:
            ob = await pm.get_orderbook(market_id)
        except Exception as e:
            return _err(e, "market_id is the numeric CLOB token id",
                        host="the Polymarket CLOB")
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
        try:
            ob = await kx.get_orderbook(market_id, depth=50)
        except Exception as e:
            return _err(e, "market_id is the Kalshi ticker",
                        host="the Kalshi API")
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
           title="Polymarket order status",
           annotations=READ, structured_output=False)
async def order_status(order_id: str) -> str:
    try:
        return _j(await trading.order_status(order_id))
    except Exception as e:
        return _err(e, host="the Polymarket CLOB")


@srv.tool(description="Recent executions (fills) for the operator wallet. "
                      "Confirms what actually traded, with tx hashes.",
           title="Recent Polymarket fills",
           annotations=READ, structured_output=False)
async def my_fills(limit: int = 25) -> str:
    try:
        return _j(await trading.my_fills(limit))
    except Exception as e:
        return _err(e, host="the Polymarket data API")


@srv.tool(description="The operator's current Polymarket positions (uses the "
                      "configured wallet; no address needed).",
           title="My Polymarket positions",
           annotations=READ, structured_output=False)
async def my_positions(limit: int = 50) -> str:
    w = trading.operator_wallet()
    if not w:
        return _j({"note": "set POLYMARKET_WALLET_ADDRESS to see positions"})
    try:
        return _j({"wallet": w, "positions": await pm.get_positions(w, limit)})
    except Exception as e:
        return _err(e, host="the Polymarket data API")

@srv.tool(description="Authenticated Polymarket cash and allowance snapshot for an "
                      "existing configured wallet. Cash is separate from positions "
                      "value; available-to-trade remains unknown until outstanding "
                      "orders and unsettled fills are reconciled. Requires local "
                      "account credentials. In simulation use paper_positions.",
           title="Polymarket cash and allowance",
           annotations=READ, structured_output=False)
async def my_balance() -> str:
    return _j(await trading.my_balance())



@srv.tool(description="Emergency stop: cancel every resting Polymarket order "
                      "on the operator account at once. Use when exposure "
                      "must go to zero fast. Respects dry-run.",
           title="Cancel all open Polymarket orders",
           annotations=TRADE, structured_output=False)
async def cancel_all_orders() -> str:
    try:
        return _j(await trading.cancel_all_orders())
    except Exception as e:
        return _err(e, "cancellations not confirmed: call open_orders to "
                       "see what still rests", host="the Polymarket CLOB")


# ------------------------- gasless position management ------------------- #
# These go through Polymarket's relayer with the OPERATOR'S OWN relayer key —
# the self-hosted pattern Polymarket's builder team recommends. No builder
# secret ships with oddsrail.

@srv.tool(description="Split USDC collateral into a full YES+NO share set for "
                      "one market, GASLESS via Polymarket's relayer. "
                      "amount_usdc is collateral in USDC (e.g. 25). Needs "
                      "POLYMARKET_RELAYER_API_KEY and "
                      "POLYMARKET_RELAYER_API_KEY_ADDRESS (polymarket.com -> "
                      "Settings -> Relayer API keys). DRY-RUN by default. "
                      "Never falls back to a gas-paying broadcast.",
           title="Split USDC into YES and NO shares",
           annotations=TRADE, structured_output=False)
async def split_position(condition_id: str, amount_usdc: float) -> str:
    try:
        return _j(await trading.split_position(condition_id, amount_usdc))
    except Exception as e:
        return _err(e, "condition_id is the market's 0x… condition id from "
                       "get_market; amount_usdc must be positive",
                    host="the Polymarket relayer")


@srv.tool(description="Merge matching YES+NO shares back into USDC, GASLESS "
                      "via the relayer. amount is in shares, or 'max' for the "
                      "largest balanced amount held. Needs the operator's "
                      "relayer key. DRY-RUN by default.",
           title="Merge YES and NO shares into USDC",
           annotations=TRADE, structured_output=False)
async def merge_positions(condition_id: str, amount: str = "max") -> str:
    try:
        return _j(await trading.merge_positions(condition_id, amount))
    except Exception as e:
        return _err(e, "amount is a positive number of shares or 'max'",
                    host="the Polymarket relayer")


@srv.tool(description="Redeem the winning shares of a RESOLVED market for "
                      "USDC, GASLESS via the relayer. Pass exactly one of "
                      "condition_id or market_id. Needs the operator's "
                      "relayer key. DRY-RUN by default.",
           title="Redeem resolved Polymarket shares",
           annotations=TRADE, structured_output=False)
async def redeem_positions(condition_id: str = "", market_id: str = "") -> str:
    try:
        return _j(await trading.redeem_positions(condition_id, market_id))
    except Exception as e:
        return _err(e, "pass exactly one of condition_id or market_id, for a "
                       "market that has already resolved",
                    host="the Polymarket relayer")


# ------------------------------ paper trading ------------------------------ #

@srv.tool(description="Paper-trading portfolio for dry-run: cash, positions "
                      "at current marks, realized and unrealized P&L, resting "
                      "paper orders (filled here if the market has crossed "
                      "them). Dry-run Polymarket orders are papered against "
                      "the live book automatically. Simulated: no queue, no "
                      "impact, no fees, so results are an upper bound.",
           title="Paper positions",
           annotations=LEDGER_SETTLE, structured_output=False)
async def paper_positions() -> str:
    if not paper.enabled():
        return _j({"enabled": False, "note": "ODDSRAIL_PAPER=0 disables the paper ledger"})
    try:
        return _j(await paper.positions())
    except Exception as e:
        return _err(e, host="the Polymarket CLOB")


@srv.tool(description="Reset the paper-trading ledger to its starting "
                      "bankroll (ODDSRAIL_PAPER_BANKROLL, default 1000 USDC). "
                      "Deletes simulated fills and positions; touches nothing "
                      "real.",
           title="Reset the paper ledger",
           annotations=LEDGER_RESET, structured_output=False)
def paper_reset() -> str:
    return _j(paper.reset())


# ------------------------------ realtime ----------------------------------- #

@srv.tool(description="Stream one Polymarket token's realtime market events "
                      "(book snapshot, price changes, trades) for up to "
                      "`seconds` (1-60) or `max_events`, then return them. "
                      "Use after get_orderbook when you need to see the book "
                      "MOVE before acting; a quiet market may deliver only "
                      "the initial snapshot.",
           title="Watch a Polymarket order book",
           annotations=READ, structured_output=False)
async def watch_book(token_id: str, seconds: float = 10.0,
                     max_events: int = 100) -> str:
    try:
        return _j(await pm.watch_book(token_id, seconds, max_events))
    except Exception as e:
        return _err(e, "token_id is the numeric CLOB id from outcomes.yes.token_id",
                    host="the Polymarket websocket")


@srv.tool(description="What the operator can turn back into USDC now: "
                      "redeemable (resolved, winning) positions and mergeable "
                      "(hold both YES and NO) positions, with the ids the "
                      "gasless tools take. Uses the configured wallet.",
           title="Redeemable and mergeable positions",
           annotations=READ, structured_output=False)
async def redeemable_positions(limit: int = 200) -> str:
    try:
        return _j(await trading.redeemable_positions(limit))
    except Exception as e:
        return _err(e, host="the Polymarket data API")


@srv.tool(description="The full resolution contract for a market, worth "
                      "reading before trusting its price: what exactly "
                      "resolves YES, who resolves it, from which sources. "
                      "venue is 'polymarket' "
                      "(pass the slug, the Gamma id, or the market_id a "
                      "search returned, which is a CLOB token id) or 'kalshi' "
                      "(pass the ticker).",
           title="Market resolution criteria",
           annotations=READ, structured_output=False)
async def resolution_criteria(venue: str, market_id: str) -> str:
    v = str(venue or "").lower()
    if v == "polymarket":
        try:
            return _j(await pm.resolution_criteria(market_id))
        except Exception as e:
            return _err(e, "pass the market's slug, its Gamma id, or the "
                           "market_id from find_markets (a CLOB token id)",
                        host="the Polymarket API")
    if v == "kalshi":
        try:
            return _j(await kx.resolution_criteria(market_id))
        except Exception as e:
            return _err(e, "pass the Kalshi ticker", host="the Kalshi API")
    return _j({"error": "venue must be 'polymarket' or 'kalshi'"})


@srv.tool(description="Markets closing within N hours on either venue, by "
                      "volume, where trading activity concentrates.",
           title="Markets closing soon",
           annotations=READ, structured_output=False)
async def closing_soon(hours: float = 24.0, limit: int = 10,
                       venues: str = "both") -> str:
    want = str(venues or "both").lower()
    if want not in ("both", "polymarket", "kalshi"):
        return _j({"error": f"unknown venues={venues!r}",
                   "valid": ["both", "polymarket", "kalshi"]})
    want, hosted_note = hosted.venues(want)
    if not want:
        return _j({"error": hosted_note, "valid": ["polymarket"]})
    out = {"note": hosted_note} if hosted_note else {}
    if want in ("both", "polymarket"):
        try:
            out["polymarket"] = await pm.closing_soon(hours, limit)
        except Exception as e:
            out["polymarket_error"] = _err_obj(e, host="the Polymarket API")
    if want in ("both", "kalshi"):
        try:
            out["kalshi"] = await kx.closing_soon(hours, limit)
        except Exception as e:
            out["kalshi_error"] = _err_obj(e, host="the Kalshi API")
    return _j(out)



# ------------------------------ audit & sizing ------------------------------ #

@srv.tool(description="Settlement-divergence audit for a cross-venue pair, on "
                      "LIVE data (no pre-curated pair list). Compares close "
                      "times, resolution sources, UMA dispute status and market "
                      "structure, and returns ok / caution / block with "
                      "reasons. polymarket_id takes the slug, the Gamma id or "
                      "the market_id a search returned (a CLOB token id); "
                      "kalshi_ticker takes the ticker. Run this before "
                      "treating any cross-venue price difference as an edge.",
           title="Cross-venue settlement audit",
           annotations=READ, structured_output=False)
async def settlement_audit(polymarket_id: str, kalshi_ticker: str,
                           notional_usd: float = 0.0) -> str:
    try:
        pm_rc = await pm.resolution_criteria(polymarket_id)
    except Exception as e:
        return _j({"stage": "could not load the Polymarket leg",
                   **_err_obj(e, host="the Polymarket API")})
    try:
        kx_rc = await kx.resolution_criteria(kalshi_ticker)
    except Exception as e:
        return _j({"stage": "could not load the Kalshi leg",
                   **_err_obj(e, host="the Kalshi API")})
    return _j(au.audit_pair(pm_rc, kx_rc, notional_usd or None))


@srv.tool(description="Fractional-Kelly position size for a binary contract, "
                      "given your bankroll, the market price, and YOUR fair "
                      "value estimate. Caps at a fraction of full Kelly and "
                      "refuses negative-edge bets. Returns its assumptions; "
                      "subtract quote_cost before trusting the number.",
           title="Fractional Kelly position size",
           annotations=LOCAL_READ, structured_output=False)
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
1. find_markets({query!r}): pick liquid candidates (volume_24h > 10000).
2. For each candidate, overshoot_signal(token_id): you want
   fade_setup_active=true AND median_reversion_120s > 0.2. If
   median_reversion_120s is null, the series had no measurable history:
   treat that as no signal, not as a weak one.
3. get_orderbook(token_id): confirm the book is two-sided. bids/asks are
   already best-first here.
4. quote_cost(venue, market_id, side, size): the reversion must beat
   slippage + fees, not just the headline move. Most setups die here.
5. resolution_criteria: a market that resolves ambiguously can strand the
   position regardless of price action.
6. position_size(bankroll_usd={bankroll_usd}, price, fair_value) where
   fair_value is the pre-jump price if you believe it fully reverts.
7. check_order(venue, market_id, side, price, size, intent=<a short
   paraphrase of the order naming the market and the outcome>). A 'block'
   ends it; read any 'caution' back to the operator.
8. place_order(...): dry-run first and read back the intent before setting
   ODDSRAIL_DRY_RUN=0.

Report the candidates you rejected and why; a rejected setup is a result."""


# The hosted connector registers this in place of find_fade_setup: its orders
# are paper only and Kalshi is not served, so there is no dry-run switch to
# flip and only one venue to pass. Not decorated here, so the local prompt
# (which scripts/gen_skills.py renders into skills/) stays the registered one.
HOSTED_FADE_DESC = ("Find and evaluate a fade (mean-reversion) setup on "
                    "Polymarket, end to end, with a paper order.")


def hosted_find_fade_setup(query: str = "", bankroll_usd: str = "100") -> str:
    return f"""Find a fade setup on Polymarket{f' related to: {query}' if query else ''}.

Orders on this connector are paper only: place_order fills against the live
Polymarket book into your account's paper ledger and nothing real is sent.

Work in this order and stop if a step fails:
1. find_markets({query!r}): pick liquid candidates (volume_24h > 10000).
2. For each candidate, overshoot_signal(token_id): you want
   fade_setup_active=true AND median_reversion_120s > 0.2. If
   median_reversion_120s is null, the series had no measurable history:
   treat that as no signal, not as a weak one.
3. get_orderbook(token_id): confirm the book is two-sided. bids/asks are
   already best-first here.
4. quote_cost("polymarket", market_id, side, size): the reversion must beat
   slippage + fees, not just the headline move. Most setups die here.
5. resolution_criteria("polymarket", market_id): a market that resolves
   ambiguously can strand the position regardless of price action.
6. position_size(bankroll_usd={bankroll_usd}, price, fair_value) where
   fair_value is the pre-jump price if you believe it fully reverts.
7. check_order("polymarket", market_id, side, price, size, intent=<a short
   paraphrase of the order naming the market and the outcome>). A 'block'
   ends it; read any 'caution' back to the user.
8. place_order(...): read the intent back to the user before placing it.
   The order is papered: read the simulated fill in the result, then call
   paper_positions() to see what filled and what still rests.

Report the candidates you rejected and why; a rejected setup is a result."""


@srv.prompt(description="Check whether a cross-venue price difference is a "
                        "real edge or a settlement mismatch.")
def check_cross_venue_edge(query: str) -> str:
    return f"""Investigate whether {query!r} offers a genuine cross-venue edge.

1. compare_venues({query!r}): candidates only. It returns nothing for most
   queries, which is the honest answer, not a failure.
2. For any candidate: settlement_audit(polymarket_id, kalshi_ticker).
   A 'block' verdict ends it: the legs do not hedge each other and the
   price difference is not an edge.
3. On 'ok' or 'caution': quote_cost on BOTH legs at the size you would
   actually trade. Cross-venue differences are usually smaller than the
   combined slippage.
4. Read resolution_criteria on both sides yourself. Identical wording does
   not mean identical settlement.
5. Only then size the trade.

State plainly if the conclusion is 'no edge here'. That is the usual and
correct outcome."""


@srv.prompt(description="Turn resolved and hedged positions back into USDC, "
                        "gasless, with a dry-run read-back first.")
def settle_resolved() -> str:
    return """Settle whatever can be settled on the operator's Polymarket account.

1. server_info(): confirm relayer_key_configured is true and note dry_run.
2. redeemable_positions(): two lists, redeemable (resolved winners) and
   mergeable (both sides held).
3. For each redeemable entry: redeem_positions(condition_id=...). For each
   mergeable entry: merge_positions(condition_id=..., amount="max").
   In dry-run these return the intent; read each back before the operator
   sets ODDSRAIL_DRY_RUN=0.
4. After live submissions: report transaction ids and outcomes, then
   my_positions() to confirm the balances moved.

Do not resubmit anything whose outcome is "unknown"; report it instead."""


@srv.prompt(description="Daily review of open exposure: positions, resting "
                        "orders, recent fills, attribution.")
def daily_review() -> str:
    return """Review current prediction-market exposure.

1. server_info(): first, so the rest of this review is read correctly. If
   dry_run is true, the live tools below have nothing to report and the
   paper ledger is the portfolio: use paper_positions() for steps 2 and 3
   and skip step 6.
2. my_positions(): what is held, and at what marks. my_balance() for the
   collateral behind it.
3. open_orders(): what is still resting; for anything stale, order_status()
   to see whether it partially filled.
4. my_fills(limit=25): what actually executed since the last review.
5. closing_soon(hours=24): positions or orders in markets about to resolve
   need a decision now.
6. builder_stats(): confirm routed flow is being attributed.

Flag: resting orders far from the current book, positions in markets with an
open UMA dispute (dispute_risk), and anything resolving within 24h."""



PROJECT = {
    "source": "https://github.com/hmesutozsoy/oddsrail",
    "site": "https://oddsrail.app",
    "issues": "https://github.com/hmesutozsoy/oddsrail/issues",
    "license": "MIT",
}

# What the hosted server_info says about builder attribution, in every field.
PAPER_ATTRIBUTION = ("paper orders are never sent to the exchange, so no "
                     "trade is attributed to any builder code")


@srv.tool(description="Server status: dry-run state, attribution config, "
                      "enabled capabilities, venue reachability from this "
                      "machine, and Polymarket's geoblock verdict for this "
                      "machine's IP (advisory, not a compliance check). "
                      "Call this before the first order of a session.",
           title="Server status",
           annotations=READ, structured_output=False)
async def server_info() -> str:
    has_key = bool(os.environ.get("POLYMARKET_PRIVATE_KEY"))
    info = {
        "name": "oddsrail",
        "version": VERSION,
        # Where this comes from, for the person reading their agent's
        # transcript. Links only: this output is read by a model, and a model
        # is not the place to ask for anything.
        "project": PROJECT,
        "hosted": hosted.enabled(),
        "dry_run": trading.dry_run(),
        "trading_key_configured": has_key,
        "builder_code_configured": bool(trading.builder_code()),
        "builder_code_source": trading.builder_code_source(),
        "attribution": "on-chain (CLOB V2 builder field)",
        "custody": "none: self-hosted, keys stay local",
        "relayer_key_configured": trading.relayer_configured(),
        "guardrails": guard.status(),
        "paper_trading": {"enabled": paper.enabled(),
                          "ledger": str(paper.ledger_path()),
                          "bankroll_usd": paper.bankroll()},
        "venues": {
            "polymarket": {"attribution": "on-chain builder code",
                           "trading_key": has_key,
                           "relayer_key": trading.relayer_configured(),
                           "gasless_tools": ["split_position", "merge_positions",
                                             "redeem_positions"]},
            "kalshi": {"attribution": "none available on REST",
                       "credentials": kx.has_credentials(),
                       "environment": "demo" if os.environ.get("KALSHI_DEMO") else "production"},
        },
        "geography": {
            "polymarket": await geo.preflight(),
            "kalshi": geo.KALSHI_GEO_NOTE,
            "reachability": await geo.reachability(include_trading_hosts=has_key),
            "disclaimer": geo.DISCLAIMER,
        },
        "signal_tools": ["overshoot_signal", "dispute_risk"],
    }
    if hosted.enabled():
        # The builder-code fields above describe the self-hosted order path:
        # a code the operator can override and that is signed into real
        # orders. Neither applies to a connector whose orders are all paper.
        info.update({
            "account": _account(),
            "custody": "none: this connector's tools are paper only; they use no wallet keys and send nothing real",
            "trading_key_configured": False,
            "relayer_key_configured": False,
            "builder_code_configured": False,
            "builder_code_source": "not applicable: " + PAPER_ATTRIBUTION,
            "attribution": "none: " + PAPER_ATTRIBUTION,
            "live_trading": hosted.LIVE_NOTE,
        })
        info["venues"]["kalshi"] = {"available": False, "note": hosted.KALSHI_NOTE}
        info["venues"]["polymarket"] = {"attribution": "none: " + PAPER_ATTRIBUTION}
        info["geography"]["note"] = ("describes the hosted server, not you; paper orders are "
                                     "never sent to the venue")
        # Operator-profile details that mean nothing on the connector: Kalshi
        # is not served, the guardrail env vars are the server's, and the
        # ledger's file path is an internal detail of this host.
        info["geography"].pop("kalshi", None)
        g = info["guardrails"]
        info["guardrails"] = {"max_order_notional_usd": g["max_order_notional_usd"],
                              "allowed_markets": g["allowed_markets"],
                              "note": ("set by the server; these apply to paper orders on "
                                       "this connector. Unset means unlimited.")}
        info["paper_trading"].pop("ledger", None)
    return _j(info)


def _account() -> dict | None:
    """The signed-in account behind this request (hosted mode)."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        tok = get_access_token()
    except Exception:
        return None
    if tok is None:
        return None
    return {"id": tok.subject, "email": (tok.claims or {}).get("email"),
            "client_id": tok.client_id}


if hosted.enabled():
    # Account-scoped and key-holding tools have no meaning on a shared server;
    # what stays is public data and the caller's own paper ledger.
    for _name in hosted.HIDDEN_TOOLS:
        try:
            srv.remove_tool(_name)
        except Exception:
            pass
    for _name in hosted.HIDDEN_PROMPTS:
        try:
            srv.remove_prompt(_name)
        except Exception:
            pass
    # Hosted orders are paper only: a failure sent nothing anywhere, and the
    # live-order tools the local hints name (open_orders) are hidden here.
    async def paper_place_order(token_id: str, side: str, price: float, size: float,
                                post_only: bool = False) -> str:
        try:
            out = await trading.place_order(token_id, side, price, size, post_only)
            # The intent is built for a real order; a paper fill is never
            # attributed, so say so as server_info does.
            if isinstance(out, dict) and isinstance(out.get("would_post"), dict):
                out["would_post"].pop("builder_code", None)
                out["would_post"]["attribution"] = "none: " + PAPER_ATTRIBUTION
            return _j(out)
        except Exception as e:
            return _err(e, "nothing was sent: hosted orders are paper only; "
                           "paper_positions shows what rests in your ledger",
                        host="the Polymarket CLOB")

    # trading.cancel_order in dry-run answers "would_cancel" for any id at
    # all, which reads like success for an id that was never a paper order.
    # The hosted ledger is the only place an order can rest, so ask it.
    def paper_cancel_order(order_id: str) -> str:
        oid = str(order_id or "").strip()
        try:
            resting = {o.get("id") for o in paper.load().get("open_orders", [])}
            if oid not in resting:
                return _j({"cancelled": False, "order_id": oid, "sent": False,
                           "error": "no resting paper order with this id in your ledger",
                           "note": ("Nothing was sent anywhere. paper_positions lists the "
                                    "paper orders that still rest; their ids start with "
                                    "paper-.")})
            out = paper.cancel(oid)
        except Exception as e:
            return _err(e, "nothing was sent: paper_positions shows what still rests")
        done = out.get("paper") == "cancelled"
        return _j({"cancelled": done, "order_id": oid, "sent": False,
                   "note": ("removed from your paper ledger; nothing real was sent"
                            if done else "no resting paper order with this id in your "
                            "ledger; paper_positions shows what still rests")})

    srv.remove_tool("place_order")
    srv.add_tool(paper_place_order, name="place_order", title="Place a paper Polymarket order",
                 description=hosted.PLACE_ORDER_DESC,
                 annotations=TRADE, structured_output=False)
    srv.remove_tool("cancel_order")
    # Cancelling a paper order only edits the caller's ledger: the same hints
    # as a ledger reset (destructive, idempotent, closed world).
    srv.add_tool(paper_cancel_order, name="cancel_order", title="Cancel a paper order",
                 description=("Cancel a resting PAPER order in your account's paper "
                              "ledger by its id (paper-...). Nothing real is sent. "
                              "An id that is not a resting paper order returns "
                              "cancelled: false."),
                 annotations=LEDGER_RESET, structured_output=False)

    # Same functions, titles and hints as the local tools; only the words
    # change, because Kalshi, cross-venue checks and the dry-run switch do not
    # exist here and a description must not promise them.
    for _name, _fn, _title, _ann, _desc in (
        ("find_markets", find_markets, "Find Polymarket markets", READ,
         "Search Polymarket markets by text and return one normalised shape "
         "per market, busiest first: venue, market_id (the CLOB token id "
         "place_order takes), title, yes/no price as probabilities in (0,1), "
         "best bid/ask, spread, 24h volume, close time. Kalshi is not served "
         "on this connector, so venues='both' (the default) searches "
         "Polymarket only."),
        ("closing_soon", closing_soon, "Markets closing soon", READ,
         "Polymarket markets closing within N hours, by volume, where trading "
         "activity concentrates. Kalshi is not served on this connector, so "
         "venues='both' (the default) lists Polymarket only."),
        ("resolution_criteria", resolution_criteria, "Market resolution criteria", READ,
         "The full resolution contract for a Polymarket market, worth reading "
         "before trusting its price: what exactly resolves YES, who resolves "
         "it, from which sources. Pass venue='polymarket' with the slug, the "
         "Gamma id, or the market_id a search returned (a CLOB token id). "
         "Kalshi is not served on this connector."),
        ("quote_cost", quote_cost, "Quote the cost of an order", READ,
         "What a given size would ACTUALLY cost on Polymarket, by walking the "
         "order book rather than reading the top level. Pass "
         "venue='polymarket' with the CLOB token id as market_id. Returns "
         "average fill price, slippage vs best, notional, and the levels "
         "consumed, plus the market's fee schedule where Polymarket publishes "
         "one. Call this before sizing any trade. Kalshi is not served on "
         "this connector."),
        ("paper_positions", paper_positions, "Paper positions", LEDGER_SETTLE,
         "Your account's paper-trading portfolio: cash, positions at current "
         "marks, realized and unrealized P&L, and resting paper orders "
         "(filled here if the market has crossed them). Every place_order on "
         "this connector is papered against the live Polymarket book into "
         "this ledger. Simulated: no queue, no impact, no fees, so results "
         "are an upper bound."),
        # The cloud app wraps this once more so an arena entry cannot wipe
        # its own record; this is the wording when the server runs alone.
        ("paper_reset", paper_reset, "Reset the paper ledger", LEDGER_RESET,
         "Reset your account's paper ledger to its starting bankroll. Deletes "
         "simulated fills, positions and resting paper orders; touches "
         "nothing real."),
        ("server_info", server_info, "Server status", READ,
         "Connector status: the signed-in account, paper-trading state (every "
         "order here is paper), enabled capabilities, and the hosted server's "
         "venue reachability and Polymarket geoblock verdict. Both describe "
         "the server, not you, and are advisory, not a compliance check. "
         "Call this before the first order of a session."),
    ):
        srv.remove_tool(_name)
        srv.add_tool(_fn, name=_name, title=_title, description=_desc,
                     annotations=_ann, structured_output=False)

    # Paper only, Polymarket only: the local prompt's dry-run switch and
    # venue choice do not exist here.
    srv.remove_prompt("find_fade_setup")
    srv.prompt(name="find_fade_setup", description=HOSTED_FADE_DESC)(hosted_find_fade_setup)


USAGE = f"""oddsrail {VERSION}: MCP server for AI agents trading prediction
markets (Polymarket + Kalshi).

This is NOT an interactive CLI. It speaks the Model Context Protocol over
stdio, so it is meant to be launched by an MCP client:

    command: oddsrail
    arguments: []

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
  POLYMARKET_RELAYER_API_KEY / _ADDRESS
                            your own Relayer API key (Settings -> Relayer API
                            keys) for gasless split/merge/redeem.
  ODDSRAIL_MAX_ORDER_NOTIONAL / ODDSRAIL_MAX_SESSION_NOTIONAL /
  ODDSRAIL_MAX_OPEN_ORDERS / ODDSRAIL_ALLOWED_MARKETS
                            operator guardrails; unset = unlimited. Enforced
                            before any request, in dry-run too.
  ODDSRAIL_PAPER=1          paper-trade dry-run orders against the live book
                            (ledger: ~/.oddsrail/paper.json; bankroll via
                            ODDSRAIL_PAPER_BANKROLL).
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
        print(f"oddsrail {VERSION}  {PROJECT['source']}")
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
              f"stdin.\nNot an interactive CLI; see `oddsrail --help`.\n"
              f"Source and docs: {PROJECT['source']}",
              file=sys.stderr)
    srv.run(transport="stdio")


if __name__ == "__main__":
    main()
