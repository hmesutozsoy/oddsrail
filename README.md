# oddsrail

<!-- mcp-name: app.oddsrail/polymarket-kalshi-arbitrage -->

**The rail AI agents use to trade prediction markets.**

An MCP server that gives any agent (Claude Code, Claude Desktop, or anything
MCP-compatible) prediction-market access across **Polymarket and Kalshi**:
market search, orderbooks, price history, positions, and order routing — with
**on-chain builder-code attribution** on Polymarket — plus two premium signal
tools (in-play overshoot/fade detection, resolution dispute-risk).

**Free to use, and free of fees.** oddsrail ships with a project builder code
registered at **0 bps**, so orders routed through it are attributed without
adding a single basis point to anyone's trade. The project's income is a share
of Polymarket's weekly builder reward pool — paid by Polymarket's own program,
not by you. Running your own builder profile instead is one environment
variable (`ODDSRAIL_BUILDER_CODE`), and `server_info` always tells you which
code is in use. No fee tiers, no paywalled tools, no account required.

## How oddsrail compares

Verified against each alternative directly (their repos, live endpoints, and
registry entries — August 2026), not from their marketing:

| | oddsrail | raw Polymarket API | raw Kalshi API | Crosswire | Parsec |
|---|---|---|---|---|---|
| Both venues, one vocabulary | ✅ `find_markets` | single-venue | single-venue | pairs from a frozen 43-pair graph | ✅ 5 venues |
| Book-walked cost + slippage | ✅ `quote_cost` | book only, math is yours | book only | top-of-book, covered pairs only | preview on their infra |
| Order lifecycle (status/fills/kill switch) | ✅ | endpoints exist, with footguns¹ | endpoints exist | ❌ read-only | ✅ |
| Resolution criteria surfaced | ✅ both venues | buried in fields | buried in fields | ✅ deep — on **1 active pair** | ❌ zero UMA/rules tools |
| Trading signals | ✅ overshoot + dispute-risk | ❌ | ❌ | ❌ | ❌ |
| Open source / auditable | ✅ MIT, full source | n/a | n/a | ❌ 4-file listing shell, service proprietary | ❌ closed |
| Self-hosted / non-custodial | ✅ keys never leave your machine | ✅ | ✅ | hosted only | stores keys or holds a managed wallet |
| Cost to the trader | **0 bps, free tools** | free | free | $0.02/call after 3/day | SaaS $0–250/mo; builder program keeps 55–85% of fees |
| Safety defaults | dry-run default, destructive-tool annotations, venue quirks pre-encoded² | you discover them by rejection | same | n/a | unknown (closed) |

¹ The raw Polymarket API *has* the endpoints — and models rejections as
`ok:false` return values, orders its books worst-first, ships a trades
endpoint that returns the market's **public** tape, and enforces an
undocumented $1 minimum notional. oddsrail exists because we hit every one of
those and encoded the fix.

² Kalshi prices are dollar strings (integer cents were removed 2026-03), its
orderbook is bids-only on both sides, and its current SDK requires
Python ≥3.13. All normalised here.

**Where the others are honestly ahead:** Parsec covers 5 venues to our 2 and
has websocket streaming; Crosswire's settlement-audit output on its one
covered pair is deeper than our `resolution_criteria`, and it takes x402
micropayments natively; hosted services need zero install. Our bet is that a
trading agent cares more about correctness, auditability, and keeping 100% of
its economics than about any of those.

## Quickstart

Python 3.11+ required.

```bash
pip install oddsrail
```

```bash
claude mcp add --transport stdio oddsrail -- oddsrail
```

Or from a clone, without installing:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
claude mcp add --transport stdio oddsrail -- /abs/path/to/oddsrail/.venv/bin/python -m oddsrail.server
```

Then ask the agent: *"search markets about the World Cup final and run the
overshoot signal on the favorite"*.

## How attribution works (CLOB V2, verified Aug 2026)

1. Get your **builder code** (a bytes32) at polymarket.com → **Settings →
   Builders**. Set your fee rates there: taker up to 100 bps, maker up to
   50 bps — additive on top of platform fees, settled to your builder wallet.
2. `export ODDSRAIL_BUILDER_CODE=0x...` where the server runs.
3. Every order any agent routes through `place_order` has the code placed in
   the V2 order struct's `builder` field **before signing** — attribution is
   on-chain, visible in every `OrderFilled` event on CTF Exchange V2.
4. Verify with the `builder_stats` tool (public builder-trades endpoint +
   leaderboard).

If you skip this, orders carry the bundled oddsrail builder code
(`0xa576c5ce…`, registered at 0 bps maker / 0 bps taker) — costing you nothing
and funding the project. If you set your own, yours wins; the default is a
default, not a lock-in.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ODDSRAIL_DRY_RUN` | `1` | `1` = orders are simulated and returned, never posted. Set `0` to trade. |
| `ODDSRAIL_BUILDER_CODE` | project default | Your bytes32 builder code. Overrides the bundled project default so attribution (and any reward-pool share) accrues to you instead. |
| `POLYMARKET_PRIVATE_KEY` | unset | Operator wallet key; required only for real trading. Never leaves this machine. |
| `POLYMARKET_WALLET_ADDRESS` | unset | Proxy/deposit wallet address, if the account uses one. |

## Status

**Offline tests:** 47 tests covering the paths where a bug costs money — the
Kalshi yes/no→bid/ask translation, Kelly sizing, book walking, cross-venue
pairing, signal edge cases, and the dry-run safety net. They need no keys and
no network:

```bash
pip install -e ".[dev]" && pytest
```

CI runs them on Python 3.11, 3.12 and 3.13 on every push and pull request.

**Live venues:** every read tool and both dry-run order paths have been driven
end-to-end against real Polymarket and Kalshi through a real MCP client
session. Two attributed Polymarket orders (a buy and a sell) have been placed
and confirmed on-chain.

**Not yet exercised live:** the Kalshi *order placement* path. Its request
shape is unit-tested and its endpoint verified, but no order has been sent to
a real Kalshi account. Treat `kalshi_place_order` as unproven and start in
dry-run.

## Network note

Polymarket's API is blocked in some jurisdictions (it geoblocks the US, and
some national filters block it outright — Turkey among them). If the read
tools return TLS or connection errors, that is where to look first: run
oddsrail somewhere the venue is reachable. Kalshi is US-regulated and applies
its own geographic rules. The signal logic, the MCP layer and the whole test
suite run fine offline regardless.

## Kalshi (venue #2)

Kalshi is **bring-your-own-key and single-tenant by design**: the operator
supplies their own API key, trades their own account, and this server caches
nothing. That is deliberate — Kalshi's Developer Agreement limits API use to a
member's own trading (§3), bars facilitating other members' trading (§3.2) and
sublicensing the API (§3.7), and restricts storing/sharing API data (§3.1). A
hosted multi-tenant Kalshi service would not be compliant; a self-hosted one is.

**Attribution does not exist here.** Kalshi Builder Codes are a
Solana/DFlow/Jupiter integration — there is no builder or affiliate field
anywhere on the REST API, so Kalshi order flow cannot be attributed or
monetised the way Polymarket's can. Kalshi is in oddsrail for coverage and
signal reach, not for routing revenue.

Two shapes on this API are easy to get wrong, so oddsrail normalises both:

- **Prices are dollar strings, not cents** (`"0.5600"`), sizes are fixed-point
  strings (`"10.00"`); the legacy integer-cent fields were removed in March
  2026. All arithmetic uses `Decimal`.
- **The orderbook is bids-only on both sides.** `yes_dollars` and `no_dollars`
  are both bid ladders, ascending — so the best bid is the *last* element, and
  a NO bid at $0.99 *is* a YES ask at $0.01. `kalshi_get_orderbook` returns a
  conventional best-first bid/ask view of the YES book plus the raw ladders.

Order placement speaks natural terms — `outcome` (yes/no), `action`
(buy/sell), `price` = probability of that outcome — and translates to Kalshi's
YES-book `bid`/`ask` internally (buy NO @ 0.25 becomes ask @ 0.75). That translation is
exhaustively unit-tested (`tests/test_money_paths.py`), since it is the
obvious place to ship an inverted-position bug.

Credentials: `KALSHI_KEY_ID` plus `KALSHI_PRIVATE_KEY_PATH` (PKCS#8 PEM) or
`KALSHI_PRIVATE_KEY`. Set `KALSHI_DEMO=1` to hit the demo environment. Read
tools need no key at all.

## Cross-venue tools

- **`find_markets(query)`** — searches Polymarket *and* Kalshi in one call and
  returns one normalised shape per market: `venue`, `market_id` (the id that
  venue's order tool takes), `title`, yes/no price as probabilities in (0,1),
  best bid/ask, spread, 24h volume, close time, and `trade_with` naming the
  tool to call. Use this when you do not already know the venue.
- **`quote_cost(venue, market_id, side, size)`** — what a size would *actually*
  cost, by walking the book rather than reading the top level. Returns average
  fill price, slippage vs best, notional, levels consumed, and whether the size
  is fillable at all — plus Polymarket's per-market fee schedule where it
  publishes one. Kalshi does not publish fees in its market payload, so they
  are reported as unknown rather than estimated.
- **`compare_venues(query)`** — candidate same-event listings across venues.
  **Not an arbitrage scanner.** Matching an event across venues is an
  unsolved entity-resolution problem: naive title overlap cheerfully pairs a
  Brazilian election with a Ukrainian one and reports a 70-point "gap" that is
  fiction. Two gates apply (title similarity ≥ 0.5 *and* close dates within a
  week), so it usually returns nothing — which is the honest answer. A price
  delta between candidates is reported as `yes_price_difference`, never as
  profit.

### Kalshi search

Kalshi has no text-search endpoint. oddsrail searches by **event** (the
human-readable index, with `with_nested_markets`) rather than paging tens of
thousands of machine-named markets, and matches on word boundaries — without
that, "fed" matches "German Bundestag" and a Fed-rate query returns German
election markets. Results carry `truncated`, because a bounded scan means an
empty result is not proof a market does not exist.

## Order lifecycle & discovery

- `order_status(order_id)` — resting / partially_filled / filled / gone, with
  size_matched. The answer an agent needs after place_order.
- `my_fills()`, `my_positions()` — the operator's executions and holdings,
  no address juggling. (Fills come from the Data API activity feed — the
  SDK's list_account_trades returns the market's *public* tape and is not
  used.)
- `cancel_all_orders()` — kill switch: flatten every resting order at once.
- `resolution_criteria(venue, market_id)` — the full resolution contract:
  what resolves YES, who resolves it, from which sources. Read it before
  trusting a price.
- `closing_soon(hours)` — markets closing within N hours on either venue,
  where activity concentrates.

## Workflow prompts

MCP prompts show up in clients as ready-made workflows, and they encode the
*order* of operations that keeps an agent out of trouble — the sequencing is
the expertise, which a flat tool list cannot convey.

- `/find_fade_setup(query, bankroll)` — signal → book → cost → resolution →
  size → dry-run, with the rejection criteria at each step
- `/check_cross_venue_edge(query)` — candidates → settlement audit → cost on
  both legs, and says plainly when the answer is "no edge"
- `/daily_review` — positions, resting orders, fills, closing-soon, attribution

## Risk & settlement

- **`settlement_audit(polymarket_id, kalshi_ticker)`** — the check that decides
  whether a cross-venue price difference is an edge or a mismatch. Compares
  close times, resolution sources, UMA dispute status and market structure on
  **live data with no pre-curated pair list**, returning `ok` / `caution` /
  `block` with reasons — and listing the checks it did *not* perform.
- **`position_size(bankroll_usd, price, fair_value)`** — fractional-Kelly sizing,
  capped, refusing negative-edge bets, returning its own assumptions.

## Tools (32)

- `search_markets`, `get_market`, `get_orderbook`, `price_history`,
  `get_positions` — read-only, no keys
- `overshoot_signal` — premium: fresh panic-jump detection + this market's
  historical reversion tendency (ported from the polymarket-wc analyzer)
- `dispute_risk` — premium: transparent 0–100 heuristic for contested
  (UMA-dispute-prone) resolutions
- `place_order`, `cancel_order`, `open_orders` — trading, dry-run by default.
  `price` is a probability in (0,1), `size` is in SHARES, and the exchange
  enforces a **$1 minimum notional** on marketable orders. Trading tools carry
  `destructiveHint` annotations so clients can gate them.
- `builder_stats` — attribution verification + public builder leaderboard
- `find_markets`, `compare_venues`, `quote_cost` — cross-venue (above)
- `server_info` — config status, per-venue

Kalshi: `kalshi_search_markets`, `kalshi_get_market`, `kalshi_get_orderbook`,
`kalshi_get_trades`, `kalshi_balance`, `kalshi_positions`,
`kalshi_open_orders`, `kalshi_place_order`, `kalshi_cancel_order`.

## Stack notes

- Official unified SDK `polymarket-client` (0.6.x): `AsyncPublicClient` for
  data, `AsyncSecureClient.place_limit_order(..., builder_code=...)` for
  attributed orders. The legacy `py-clob-client` is archived and cannot
  attach builder codes — do not use it.
- MCP SDK 2.0: `MCPServer` from `mcp.server.mcpserver` (the old
  `mcp.server.fastmcp.FastMCP` import is gone in 2.x).
- Kalshi is on plain `httpx` + `cryptography`, not the official SDK:
  `kalshi-python-sync` requires Python >=3.13 and re-releases weekly in
  lockstep with the spec version. Auth is RSA-PSS(SHA256, salt=digest length)
  over `str(unix_ms) + METHOD + path`, where the path includes `/trade-api/v2`
  and excludes the query string. Base URL is now
  `external-api.kalshi.com`.
- x402 (planned): the official `x402` PyPI package (v2.20+) can wrap MCP
  tools directly (`x402.mcp`, payment rides in tool-call `_meta`), but its
  MCP helpers currently target mcp 1.x — integrating means pinning
  `mcp>=1.28,<2` or waiting for the 2.x-compatible release. Mainnet
  settlement needs a facilitator (Coinbase CDP: 1,000 free settlements/mo,
  then $0.001). Keep free tiers of both signals so registries can index the
  server.

## Who this is for

Polymarket's public builder leaderboard shows what a single operator routing
their own flow is worth. Pulled **2026-08-31** via this server's own
`builder_stats` tool — re-run it, the numbers move:

| | weekly volume |
|---|---|
| #1 (traderline) | $7.70M |
| median of top 25 | $533K |
| entry to top 25 | $140K |

The instructive rows are the small ones: **MagicMarkets routes $901K/week with
a single active user**; Jupiter $515K with one; Sharkbetting $1.15M with two.
Those are bot operators routing their own flow — which is exactly who this is
built for.

## Roadmap

1. ~~Live smoke test from an unblocked network~~ — done 2026-08-23, all tools pass
2. Register builder code (polymarket.com → Settings → Builders), set fees to
   0 bps at launch, export `ODDSRAIL_BUILDER_CODE`; first attributed order on
   a tiny size
3. ~~Kalshi as venue #2~~ — done 2026-08-23, 9 tools, verified live
4. x402 paid wrapping for the two signals once the mcp-2.x conflict clears
5. Registry listings: official MCP registry (`mcp-publisher`, PyPI
   `mcp-name:` marker), Smithery (needs public streamable-HTTP + a free
   tool for their scanner), Glama (`glama.json`)

## Listing / distribution

- **GitHub**: https://github.com/hmesutozsoy/oddsrail (public, MIT)
- **Glama**: auto-crawls GitHub; `glama.json` in the repo root claims
  maintainership.
- **PyPI**: https://pypi.org/project/oddsrail/ — `pip install oddsrail`
- **Official MCP registry**: listed as `app.oddsrail/polymarket-kalshi-arbitrage`
  (published 2026-08-30, status active). Re-publish after a version bump with
  `mcp-publisher publish`; keep `server.json`'s version in step with
  `pyproject.toml` or the registry rejects it.
- **Smithery**: requires a public HTTPS streamable-HTTP endpoint — available
  once oddsrail is hosted rather than run locally over stdio.
