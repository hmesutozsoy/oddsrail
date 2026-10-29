# oddsrail

<!-- mcp-name: app.oddsrail/polymarket-kalshi-trading -->

An MCP server that lets your AI assistant trade on Polymarket (and Kalshi)
without handing your keys to anyone.

Point your MCP client at it and your assistant can search markets, read order
books, work out what an order would really cost, and place orders. Orders are
signed on your own machine. Everything starts in dry-run: Polymarket orders
fill against the live order book into a paper account (Kalshi dry-run orders
just show what would be sent), so you can watch what your agent does before
any money is involved.

We built it after tripping over the Polymarket API ourselves. Rejected orders
come back as normal responses with `ok: false`. Order books arrive sorted worst
price first. The "trades" endpoint returns everyone's trades, not just yours.
Marketable orders under $1 fail, and that minimum isn't documented. oddsrail
handles each one.

<p align="center">
  <img src="https://raw.githubusercontent.com/hmesutozsoy/oddsrail/main/docs/img/check-order.png" alt="A real pre-trade check on a live Polymarket market: the agent's intent said NO, the order it built was on the YES side, and check_order blocked it" width="820">
</p>

That's a real check on a live market. The agent meant to buy NO, built the
order on the YES side, and `check_order` blocked it. Polymarket would have
taken it as a normal order.

> **Before you start.** These are tools, and nothing here is financial advice.
> You decide every order. Prediction markets are restricted in a lot of places, so
> check that you're allowed to trade where you live. oddsrail is an
> independent project, not affiliated with or endorsed by Polymarket or Kalshi.

If oddsrail is useful to you, a ⭐ on this repo helps other people find it.

## Install

You need Python 3.11 or newer.

```bash
pip install oddsrail
```

Add it to your MCP client as a local (stdio) server: the command is `oddsrail`,
with no arguments. For clients that use JSON config:

```json
{"mcpServers":{"oddsrail":{"command":"oddsrail","args":[]}}}
```

Have your assistant run `server_info` first. It shows whether you're in
dry-run, which builder code orders will carry, and whether Polymarket is
reachable from your network. Then try something like *"search markets about
the next Fed rate decision and run the overshoot signal on the favorite"*.

<details>
<summary>Other ways to install</summary>

| Client | How |
|---|---|
| Any agent that reads skills | `npx skills add hmesutozsoy/oddsrail` |
| Cursor | [Install oddsrail in Cursor](https://cursor.com/en/install-mcp?name=oddsrail&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJvZGRzcmFpbCJdfQ==) |
| VS Code | [Install oddsrail in VS Code](vscode:mcp/install?%7B%22name%22%3A%22oddsrail%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22oddsrail%22%5D%7D) |
| Anything else that speaks MCP over stdio | `{"command": "uvx", "args": ["oddsrail"]}` |

The plugin and the one-click links start the server with `uvx`, so they need
[uv](https://docs.astral.sh/uv/) installed. Without uv, `pip install oddsrail`
gives you an `oddsrail` command you can point any client at.

To run from a clone without installing:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Then use `/abs/path/to/oddsrail/.venv/bin/python` with arguments
`["-m", "oddsrail.server"]` in your client's config.

</details>

Setup guides, wallet notes and trading limits are in the
[documentation](https://oddsrail.app/docs).

## What's in it

42 tools and four workflow prompts.

- **Polymarket data:** `search_markets`, `get_market`, `get_orderbook`,
  `price_history`, `get_positions`, `resolution_criteria` (what resolves YES,
  who decides, from which sources), `closing_soon`, and `watch_book`, which
  streams live book updates for up to 60 seconds.
- **Costs and signals:** `quote_cost` walks the order book for your size and
  returns the average fill price, slippage, notional and the market's fee
  schedule where Polymarket publishes one (fees aren't added into the cost).
  `overshoot_signal` flags sudden price jumps and shows how far earlier jumps
  in the same window came back. `dispute_risk` is a rough 0 to 100 heuristic,
  with its reasons, for how likely a market's resolution is to be contested.
  `position_size` is capped fractional Kelly sizing that refuses bets with
  negative edge.
- **Checking an order:** `check_order` (more below).
- **Trading:** `place_order`, `cancel_order`, `open_orders`, `order_status`,
  `my_fills`, `my_positions`, `my_balance`, and `cancel_all_orders` as the
  emergency stop. Prices are probabilities between 0 and 1 and sizes are in
  shares, so an order's notional is price times size.
- **Gasless position management:** `split_position`, `merge_positions` and
  `redeem_positions` go through Polymarket's relayer; `redeemable_positions`
  lists what your wallet could redeem or merge right now.
- **Paper trading:** `paper_positions`, `paper_reset`.
- **Kalshi:** search, market details, order book, recent trades, balance,
  positions, open orders, placing and cancelling (`kalshi_*`, nine tools).
- **Across venues:** `find_markets`, `compare_venues` and `settlement_audit`.
  A price gap between two venues is not free money; the settlement audit
  checks whether the two markets actually resolve the same way.
- **Attribution and status:** `builder_stats`, `attribution_ledger`,
  `server_info`.

The prompts (`find_fade_setup`, `check_cross_venue_edge`, `settle_resolved`,
`daily_review`) each walk an agent through one job step by step.
`find_fade_setup`, for example, goes signal, book, cost, resolution rules,
size, check, then a dry-run order. The skills in
`skills/` are generated from those prompts, and a test fails if they drift
apart.

## Checking orders before they go out

`check_order` compares the order your agent built with a short paraphrase of
what was asked for and with the live market. It checks that the market exists
and is open, that the wording matches the market and the YES or NO side, that
the price makes sense against the book, that the size clears Polymarket's $1
minimum and your limits, that there's liquidity within your price, and that a
resolution source is named. You get `ok`, `caution` or `block`, with the
evidence for each check. There's no model involved; it's plain Python checks.

It only runs when your agent calls it. `place_order` doesn't call it for you,
so tell your agent to check every order first.

## Staying safe

- **Dry-run is the default.** Only `ODDSRAIL_DRY_RUN=0` (or `false`, or `no`)
  turns on live trading. Any other value keeps you in dry-run.
- **Paper orders persist.** In dry-run, Polymarket orders fill against
  the live book into a local paper ledger (`~/.oddsrail/paper.json`, starting
  at $1,000). Whatever doesn't fill rests and can fill later if the market
  crosses it. Paper fills ignore queue position, latency, market impact and
  fees, so treat paper results as a best case.
- **Limits no tool can change.** You set them as environment variables
  (see [Configuration](#configuration)). The per-order cap and the allowed
  markets apply in dry-run too; the session cap and the open-order cap count
  live orders only. They're checked before anything is sent, and a refusal
  says exactly which rule stopped the order:

  ```json
  {"accepted": false, "blocked_by": "guardrail", "rule": "max_order_notional",
   "limit": 25.0, "requested": 99.5, "note": "refused by an operator-set guardrail ... Nothing was sent."}
  ```

  The session budget resets when the server restarts. `server_info` shows your
  limits and how much of the session budget is used.
- **Clients can ask before trading.** Every tool that places or cancels orders
  or moves funds is marked destructive in its MCP annotations, so clients that
  support it can ask you to approve each call.

## Going live

Set `POLYMARKET_PRIVATE_KEY` (and `POLYMARKET_WALLET_ADDRESS` if your account
uses a proxy or deposit wallet), set `ODDSRAIL_DRY_RUN=0` and restart. The key
stays on your machine; Polymarket gets signed orders and API credentials
derived from the key, never the key itself.

For gasless split, merge and redeem, create your own relayer key at
polymarket.com (Settings, Relayer API keys) and set
`POLYMARKET_RELAYER_API_KEY` and `POLYMARKET_RELAYER_API_KEY_ADDRESS`. Without
both, those tools send nothing; they never fall back to paying gas from your
wallet. Polymarket allows 100 relayer requests a day for unverified builders
and 10,000 for verified ones.

A 1 USDC split and merge through the relayer on 2026-09-02 is written up in
[the live verification notes](docs/live-proof.md). `redeem_positions` hasn't
been exercised live yet, because the test account hasn't held a resolved
winning position.

## Fees and builder codes

oddsrail is free, and it adds nothing to your trades. By default your orders
carry our builder code (`0xa576c5ce…`, registered at 0 bps for both makers and
takers), so Polymarket attributes the volume to the project without charging
you anything for it. Orders sent from this server earn the project a share of
Polymarket's weekly builder rewards, which Polymarket pays from its own
program. Separately, oddsrail.app's links to Polymarket and its Perps
authorizations carry our referral code, which pays the project a share of
Polymarket's own fee without changing yours. There's no paid tier.

You can use your own code instead. Get one at polymarket.com (Settings,
Builders), where you set your own fee rates (takers up to 100 bps and makers
up to 50 bps, on top of platform fees, paid to your builder wallet), and set
`ODDSRAIL_BUILDER_CODE`. The code goes into the order before it's signed, so
attribution is on-chain. `server_info` shows which code is in use and
`builder_stats` lets you check the attribution. The oddsrail builder profile
was verified in Polymarket's builder program on 2026-09-02.

## The hosted version

If you'd rather not install anything, add `https://mcp.oddsrail.app/mcp` as a
remote MCP server in a client that supports OAuth, and sign in with your email
(you get a one-time link). Example prompts and troubleshooting are in the
[hosted connector docs](https://oddsrail.app/docs#hosted-connector).

The connector is paper trading only. Each account gets a $1,000 paper
bankroll that fills against the live Polymarket book. Its tools use no wallet
keys and never send a real order, and Kalshi isn't available there. It has 22
of the 42 tools; `oddsrail/hosted.py` shows exactly which are removed and why:

- Market data: `search_markets`, `find_markets`, `get_market`,
  `get_orderbook`, `price_history`, `watch_book`, `closing_soon`,
  `resolution_criteria`, `get_positions`
- Costs and signals: `overshoot_signal`, `dispute_risk`, `quote_cost`,
  `position_size`
- Paper trading: `check_order`, `place_order`, `cancel_order`,
  `paper_positions`, `paper_reset`
- A public paper arena: `arena_register`, `arena_unregister`, `arena_status`
- Status: `server_info`

Your account holds your email address, the sign-in records for the client you
connected, your paper ledger and, only if you set them up, an arena entry or a
hosted paper agent. The details are in the
[privacy policy](https://oddsrail.app/privacy).

The website at [oddsrail.app](https://oddsrail.app) also runs hosted agents
and a leaderboard. Real-money hosted agents are a small pilot for allowlisted
wallets, use a session key for a Deposit Wallet, and don't go through these
tools. Signing in to the website with a wallet lists it on the public
leaderboard under a shortened address; the privacy policy explains how to be
removed. The hosted service and the website are developed in a separate
private repository. This repository is the MCP server.

## Where it works

Two different things can stop an order, and they look different.

**Polymarket's own restrictions** apply to orders, not connections. Market
data loads normally and then orders get rejected. A few places are blocked
completely; many more are close-only, meaning you can close positions but not
open new ones. That list includes the United States, the United Kingdom,
France, Germany and Australia, among others. In sanctioned places you can't
even close positions, and a few countries (Ireland, Japan, the Netherlands,
and Malta for sports) are close-only on Polymarket's website but not through
the API. The list changes without notice, so
check [Polymarket's list](https://docs.polymarket.com/api-reference/geoblock)
rather than relying on this README.

**Network filters** block the connection itself. Turkish ISPs block
polymarket.com, for example, even though Polymarket doesn't restrict Turkey.
You'll see DNS failures, TLS errors or an ISP page where data should be.
oddsrail tells your agent which of the two it ran into.

oddsrail doesn't support Polymarket US (polymarket.us). That's a separate,
CFTC-regulated exchange with its own API, keys and SDK.

`server_info` reports Polymarket's verdict for your IP address, but that's a
network probe, not a compliance check. Polymarket's terms ask you to confirm
you're not a US person, not in a restricted jurisdiction and not using a VPN
to get around restrictions. That's between you and Polymarket; if it matters
for you, get legal advice.

## Privacy and network access

The local server has no telemetry or analytics and never contacts oddsrail.app
or any other server we run. It only talks to the venues and blockchain hosts
below, and only when a tool needs them. Each of them sees your IP address.

| Host | When | What is sent |
|---|---|---|
| `gamma-api.polymarket.com` | market search, details and listings (`search_markets`, `find_markets`, `get_market`, `closing_soon`, `resolution_criteria`, `check_order`, `dispute_risk`) | your search text and the market ids, slugs or token ids asked about |
| `clob.polymarket.com` | order books (`get_orderbook`, `quote_cost`, `check_order`, paper fills), builder trades; with a trading key, orders, cancels and account reads | token ids and builder codes; with a key, orders signed on this machine and requests authenticated with credentials derived from the key. The private key itself is never sent. |
| `data-api.polymarket.com` | price history, positions, fills, portfolio value, builder leaderboard | token ids and the wallet address being looked up (the configured wallet for `my_fills` and `my_positions`) |
| `ws-subscriptions-clob.polymarket.com` | `watch_book` websocket | the token id subscribed to |
| `polymarket.com/api/geoblock` | `server_info` | the request itself, so Polymarket can report this IP's jurisdiction verdict |
| `relayer-v2.polymarket.com` | with a trading key, wallet checks when the trading client starts; with your relayer key, gasless `split_position`, `merge_positions`, `redeem_positions` | your wallet address, signed transactions and your relayer API key |
| a public Polygon RPC (`polygon.drpc.org`, the default of the pinned `polymarket-client` SDK) | on-chain reads the SDK makes for the gasless tools, such as balances and gas estimates | public wallet and contract addresses |
| `external-api.kalshi.com` (`external-api.demo.kalshi.co` with `KALSHI_DEMO=1`) | `kalshi_*` tools, `compare_venues` and `settlement_audit`, `find_markets` / `closing_soon` when `venues` includes Kalshi (the default is both), and `quote_cost` / `resolution_criteria` / `check_order` with `venue='kalshi'` | search text and tickers; with your Kalshi key, requests signed on this machine |

`server_info` also sends one unauthenticated reachability check each to
gamma-api, clob, data-api and external-api.kalshi.com (plus the relayer when a
trading key is set). Installing with `pip` or `uvx` downloads packages from
PyPI through your package manager.

Your keys, the limit counters and the paper ledger stay on your machine. Your
AI client sees every tool call and result, under its provider's own policy.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ODDSRAIL_DRY_RUN` | `1` | Orders are simulated, never posted. Only `0`, `false` or `no` enables real trading; any other value stays in dry-run. |
| `ODDSRAIL_BUILDER_CODE` | project default | Your bytes32 builder code, used instead of the project's 0 bps default. |
| `POLYMARKET_PRIVATE_KEY` | unset | Your wallet key, needed only for real trading. It never leaves this machine. |
| `POLYMARKET_WALLET_ADDRESS` | unset | Proxy or deposit wallet address, if your account uses one. |
| `POLYMARKET_RELAYER_API_KEY` | unset | Your own relayer API key, for gasless `split_position`, `merge_positions` and `redeem_positions`. |
| `POLYMARKET_RELAYER_API_KEY_ADDRESS` | unset | The address the relayer key was issued for. Both halves are required. |
| `ODDSRAIL_MAX_ORDER_NOTIONAL` | unset | Limit: maximum USDC per order. Applies in dry-run too. |
| `ODDSRAIL_MAX_SESSION_NOTIONAL` | unset | Limit: maximum total USDC of live orders sent by this server process. |
| `ODDSRAIL_MAX_OPEN_ORDERS` | unset | Limit: maximum resting orders on the account (live; checked with the venue before placing). |
| `ODDSRAIL_ALLOWED_MARKETS` | unset | Limit: comma-separated market identifiers you allow. For Polymarket, use exact outcome token ids. Anything else is refused. |
| `ODDSRAIL_PAPER` | `1` | Paper-trade dry-run Polymarket orders against the live book. `0` turns it off. |
| `ODDSRAIL_PAPER_LEDGER` | `~/.oddsrail/paper.json` | Where the paper ledger lives. |
| `ODDSRAIL_PAPER_BANKROLL` | `1000` | Starting paper cash in USDC. |
| `KALSHI_KEY_ID` | unset | Your Kalshi API key id, needed for Kalshi account and trading tools. |
| `KALSHI_PRIVATE_KEY_PATH` | unset | Path to your Kalshi private key (PKCS#8 PEM). `KALSHI_PRIVATE_KEY` can hold the PEM itself instead. |
| `KALSHI_DEMO` | unset | `1`, `true` or `yes` uses Kalshi's demo environment. |

If `watch_book` fails with `CERTIFICATE_VERIFY_FAILED` while everything else
works, your Python has no CA bundle (common with the python.org installer on
macOS). Run `Install Certificates.command` from the Python folder in
Applications, or set `SSL_CERT_FILE` to the path printed by
`python -m certifi`.

## Polymarket Protocol V2

Polymarket is rolling out a new Protocol V2. It's separate from the CLOB V2
order format this server already uses for builder attribution, so don't read
one as support for the other. The 0.20.0 release candidate on `main` updates
the SDK and market discovery for V2, but live V2 orders and split, merge and
redeem on V2 markets are refused before signing until they've been tested with
real funds. The stable desktop bundle still pins `oddsrail==0.19.0`. Details
are in [the readiness notes](docs/protocol-v2-readiness.md).

## Other options

If you're comparing tools: [pmxt](https://github.com/pmxt-dev/pmxt) covers
many more venues, offers a hosted mode and has a much bigger community.
[Simmer](https://docs.simmer.markets) has a virtual-money sandbox, stop-loss
and take-profit, and a strategy marketplace. Polymarket's own
[agent-skills](https://github.com/Polymarket/agent-skills) document the API,
including deposits and bridging, which oddsrail doesn't cover. oddsrail is
narrower: self-hosted, your keys, your builder code, and a check that catches
wrong-side or mispriced orders. (Compared in September 2026; these projects
move quickly.)

## Tests

The test suite runs offline in CI on Python 3.11, 3.12 and 3.13 and covers
spending limits, concurrent submissions, order reconciliation, book walking,
sizing, dry-run behavior and attribution. See the
[testing guide](docs/testing.md) for the locked environment and commands. The
latest release check placed no funded orders, and the live relayer test above
was done with version 0.10.1.

## Security

Please report vulnerabilities privately, not in a public issue. See
[SECURITY.md](SECURITY.md).

## License

MIT. The MCP server in this repository is open source; the hosted service and
the website are not.
