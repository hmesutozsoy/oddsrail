# oddsrail

<!-- mcp-name: io.github.hmesutozsoy/oddsrail -->

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

## Why this and not Parsec

The closest competitor (parsecapi.com) is a closed-source hosted service:
it stores your exchange keys (or holds a managed wallet that signs for you),
and its Builder Program keeps **55–85% of the fees builders collect**.
oddsrail is the opposite on every axis: **self-hosted, non-custodial (keys
never leave your machine), open source, and you keep 100% of your Polymarket
builder fees** because attribution uses Polymarket's native mechanism, not a
middleman escrow. Parsec also ships zero signal/analytics tools and nothing
on resolution risk — that's our paid layer.

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

If you skip this, orders carry the bundled oddsrail code at 0 bps — costing
you nothing and funding the project. If you set your own, yours wins; the
default is a default, not a lock-in.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ODDSRAIL_DRY_RUN` | `1` | `1` = orders are simulated and returned, never posted. Set `0` to trade. |
| `ODDSRAIL_BUILDER_CODE` | project default | Your bytes32 builder code. Overrides the bundled project default so attribution (and any reward-pool share) accrues to you instead. |
| `POLYMARKET_PRIVATE_KEY` | unset | Operator wallet key; required only for real trading. Never leaves this machine. |
| `POLYMARKET_WALLET_ADDRESS` | unset | Proxy/deposit wallet address, if the account uses one. |

## Status — live-verified 2026-08-23

All 10 callable tools were driven end-to-end through a real MCP client
session against live Polymarket, from the Finland VPS (`/opt/oddsrail`).
Verified working: search, market lookup, orderbook (9/65 levels), price
history (361 pts), overshoot signal, dispute-risk, builder leaderboard,
dry-run order, open orders, server info.

Field mappings were corrected against the real API during that run — the
docs-guessed shapes were wrong in three places (`outcomes` is a dict keyed
`yes`/`no`, `search()` nests markets inside events, and book/volume/
resolution data live in `prices`/`metrics`/`state`/`resolution`
sub-objects).

## ⚠️ Network note

Polymarket API domains are **blocked on Turkish networks (BTK)** — local
testing fails TLS with a block page. Run the server where Polymarket is
reachable (the Finland VPS at `/opt/oddsrail`, a VPN, or any unblocked
network). The signal logic and MCP layer are fully testable offline.

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
YES-book `bid`/`ask` internally (buy NO @ 0.25 becomes ask @ 0.75). That
translation is unit-tested, since it is the obvious place to ship an
inverted-position bug.

Credentials: `KALSHI_KEY_ID` plus `KALSHI_PRIVATE_KEY_PATH` (PKCS#8 PEM) or
`KALSHI_PRIVATE_KEY`. Set `KALSHI_DEMO=1` to hit the demo environment. Read
tools need no key at all.

## Tools (21)

- `search_markets`, `get_market`, `get_orderbook`, `price_history`,
  `get_positions` — read-only, no keys
- `overshoot_signal` — premium: fresh panic-jump detection + this market's
  historical reversion tendency (ported from the polymarket-wc analyzer)
- `dispute_risk` — premium: transparent 0–100 heuristic for contested
  (UMA-dispute-prone) resolutions
- `place_order`, `cancel_order`, `open_orders` — trading, dry-run by default
- `builder_stats` — attribution verification + public builder leaderboard
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

## What the builder economy looks like (live, 2026-08-23)

Pulled from the public leaderboard via `builder_stats`:

| | weekly | all-time |
|---|---|---|
| #1 (betmoar) | $2.31M | $2.10B |
| median of top 25 | $127K | $88.2M |
| **entry to top 25** | **$42K** | $36.8M |

The instructive rows are the small-user ones: MagicMarkets routes $354K/week
with **1 active user**, Gate $1.11M/week with 2, PolymarketScan $277K with 3.
Those are bot operators routing their own flow — oddsrail's exact target
customer — and they show a single serious agent trader is worth real volume.
Wallets (MetaMask, 37K users) dominate on user count, not on volume per user.

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
- **Official MCP registry**: `server.json` is ready. Publishing needs the
  package on PyPI first (the registry verifies ownership via an
  `mcp-name: io.github.hmesutozsoy/oddsrail` marker in the PyPI README),
  then `mcp-publisher login github && mcp-publisher publish`.
- **Smithery**: requires a public HTTPS streamable-HTTP endpoint — available
  once oddsrail is hosted rather than run locally over stdio.
