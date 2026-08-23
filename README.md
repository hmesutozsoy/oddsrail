# oddsrail

**The rail AI agents use to trade prediction markets.**

An MCP server that gives any agent (Claude Code, Claude Desktop, or anything
MCP-compatible) prediction-market access: market search, orderbooks, price
history, positions, and order routing with **on-chain builder-code
attribution** — plus two premium signal tools (in-play overshoot/fade
detection, resolution dispute-risk).

Business model: routed order flow earns builder fees; signal tools are the
paid data layer (x402 pay-per-call planned).

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

The venv is already built (`.venv`, Python 3.12). Plug into Claude Code:

```bash
claude mcp add --transport stdio oddsrail -- /Users/mesutozsoy/Desktop/oddsrail/.venv/bin/python -m oddsrail.server
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

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ODDSRAIL_DRY_RUN` | `1` | `1` = orders are simulated and returned, never posted. Set `0` to trade. |
| `ODDSRAIL_BUILDER_CODE` | unset | Your bytes32 builder code — attribution + fees. |
| `POLYMARKET_PRIVATE_KEY` | unset | Operator wallet key; required only for real trading. Never leaves this machine. |
| `POLYMARKET_WALLET_ADDRESS` | unset | Proxy/deposit wallet address, if the account uses one. |

## ⚠️ Network note

Polymarket API domains are **blocked on Turkish networks (BTK)** — local
testing fails TLS with a block page. Run the server where Polymarket is
reachable (the Finland VPS, a VPN, or any unblocked network). The signal
logic and MCP layer are fully testable offline.

## Tools (12)

- `search_markets`, `get_market`, `get_orderbook`, `price_history`,
  `get_positions` — read-only, no keys
- `overshoot_signal` — premium: fresh panic-jump detection + this market's
  historical reversion tendency (ported from the polymarket-wc analyzer)
- `dispute_risk` — premium: transparent 0–100 heuristic for contested
  (UMA-dispute-prone) resolutions
- `place_order`, `cancel_order`, `open_orders` — trading, dry-run by default
- `builder_stats` — attribution verification + public builder leaderboard
- `server_info` — config status

## Stack notes

- Official unified SDK `polymarket-client` (0.6.x): `AsyncPublicClient` for
  data, `AsyncSecureClient.place_limit_order(..., builder_code=...)` for
  attributed orders. The legacy `py-clob-client` is archived and cannot
  attach builder codes — do not use it.
- MCP SDK 2.0: `MCPServer` from `mcp.server.mcpserver` (the old
  `mcp.server.fastmcp.FastMCP` import is gone in 2.x).
- x402 (planned): the official `x402` PyPI package (v2.20+) can wrap MCP
  tools directly (`x402.mcp`, payment rides in tool-call `_meta`), but its
  MCP helpers currently target mcp 1.x — integrating means pinning
  `mcp>=1.28,<2` or waiting for the 2.x-compatible release. Mainnet
  settlement needs a facilitator (Coinbase CDP: 1,000 free settlements/mo,
  then $0.001). Keep free tiers of both signals so registries can index the
  server.

## Roadmap

1. Live smoke test from an unblocked network (VPS) — search → book →
   history → dry-run order end-to-end
2. Register builder code; first attributed order on a tiny size
3. Kalshi as venue #2 (no official Kalshi MCP exists; auth is RSA-PSS
   request signing — bring-your-own key, keeps us non-custodial)
4. x402 paid wrapping for the two signals once the mcp-2.x conflict clears
5. Registry listings: official MCP registry (`mcp-publisher`, PyPI
   `mcp-name:` marker), Smithery (needs public streamable-HTTP + a free
   tool for their scanner), Glama (`glama.json`)
