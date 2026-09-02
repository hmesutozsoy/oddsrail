# oddsrail

<!-- mcp-name: app.oddsrail/polymarket-kalshi-trading -->

**The rail AI agents use to trade prediction markets.**

An MCP server that gives any agent (Claude Code, Claude Desktop, or anything
MCP-compatible) prediction-market access across **Polymarket and Kalshi**:
market search, orderbooks, price history, positions, and order routing, with
**on-chain builder-code attribution** on Polymarket, plus two premium signal
tools (in-play overshoot/fade detection, resolution dispute-risk).

**Free to use, and free of fees.** oddsrail ships with a project builder code
registered at **0 bps**, so orders routed through it are attributed without
adding a single basis point to anyone's trade. The project's income is a share
of Polymarket's weekly builder reward pool, paid by Polymarket's own program,
not by you. Running your own builder profile instead is one environment
variable (`ODDSRAIL_BUILDER_CODE`), and `server_info` always tells you which
code is in use. No fee tiers, no paywalled tools, no account required.

## How oddsrail compares

Verified against each alternative directly (their repos, live endpoints, and
registry entries, September 2026), not from their marketing:

| | oddsrail | raw venue APIs | pmxt | Simmer | Polymarket agent-skills |
|---|---|---|---|---|---|
| What it is | self-hosted MCP server | the venues themselves | unified API + SDK + MCP, "CCXT for prediction markets" | agent trading platform + SDK + MCP | markdown skill docs for agents |
| Venues you can trade | Polymarket, Kalshi | one each | Polymarket, Opinion, Limitless (hosted writes); a dozen more for data | Polymarket, Kalshi, plus its own $SIM sandbox markets | Polymarket only |
| Custody | non-custodial; keys never leave your machine | yours | hosted mode: "PMXT handles custody, signing infrastructure"; self-hosted mode: your keys | self-custody, local signing | yours (documentation only) |
| Attribution you control | yes: `ODDSRAIL_BUILDER_CODE` overrides the 0 bps default | n/a | not documented | not documented | documents builder headers for your own code |
| Cost to the trader | 0 bps, free tools | free | hosted pricing not in the README | not documented | free |
| Open source | MIT, full source | n/a | MIT, ~2.1k stars | not stated | docs; license not stated |
| Operator guardrails | notional caps, open-order cap, allowed markets; enforced pre-request, in dry-run too | none | not documented | per-trade limits, daily caps, stop-loss/take-profit, kill switch | none |
| Paper trading | dry-run fills against the live book, P&L | none | not documented | virtual $SIM sandbox, then graduate to real money | none |
| Book-walked cost, settlement audit, jurisdiction-classified failures, dated venue-quirk notes | yes, all four | no | not documented | not documented | quirks partly documented |
| Realtime | `watch_book`, bounded | websocket, yours to wire | not documented in the README | not documented | websocket documented |

Verified 2026-09-02 from each project's own README or docs (pmxt: github.com/pmxt-dev/pmxt; Simmer: docs.simmer.markets; agent-skills: github.com/Polymarket/agent-skills). "Not documented" means exactly that, not "absent". Re-check before quoting; these projects move.

**The wedge, in one line:** pmxt is the reference for trading *everywhere*; Simmer is the reference for an agent economy with a sandbox and a reputation layer; oddsrail is the reference for trading *correctly, non-custodially, with attribution you own*.

**Where the others are honestly ahead:** pmxt trades three venues to our two and covers a dozen more for data, with hosted convenience and a community many times ours. Simmer has a virtual-balance sandbox, stop-loss and take-profit rails we do not have, a public reasoning/reputation layer, and a strategy-skills marketplace. Polymarket's agent-skills is the venue's own documentation and covers bridging and deposits, which oddsrail does not.

¹ The raw Polymarket API *has* the endpoints. It also models rejections as
`ok:false` return values, orders its books worst-first, ships a trades
endpoint that returns the market's **public** tape, and enforces an
undocumented $1 minimum notional. oddsrail exists because we hit every one of
those and encoded the fix.

² Kalshi prices are dollar strings (integer cents were removed 2026-03), its
orderbook is bids-only on both sides, and its current SDK requires
Python ≥3.13. All normalised here.

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
   50 bps, additive on top of platform fees, settled to your builder wallet.
2. `export ODDSRAIL_BUILDER_CODE=0x...` where the server runs.
3. Every order any agent routes through `place_order` has the code placed in
   the V2 order struct's `builder` field **before signing**, so attribution is
   on-chain, visible in every `OrderFilled` event on CTF Exchange V2.
4. Verify with the `builder_stats` tool (public builder-trades endpoint +
   leaderboard).

If you skip this, orders carry the bundled oddsrail builder code
(`0xa576c5ce…`, registered at 0 bps maker / 0 bps taker), costing you nothing
and funding the project. If you set your own, yours wins; the default is a
default, not a lock-in.

The oddsrail builder profile is **Verified** in Polymarket's builder program
(2026-09-02), and Polymarket's builder team confirmed builder-code attribution
as the right pattern for a self-hosted, non-custodial tool: no keys ship with
the product, and the code is attached and signed by the operator's own wallet.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ODDSRAIL_DRY_RUN` | `1` | `1` = orders are simulated and returned, never posted. Set `0` to trade. |
| `ODDSRAIL_BUILDER_CODE` | project default | Your bytes32 builder code. Overrides the bundled project default so attribution (and any reward-pool share) accrues to you instead. |
| `POLYMARKET_PRIVATE_KEY` | unset | Operator wallet key; required only for real trading. Never leaves this machine. |
| `POLYMARKET_WALLET_ADDRESS` | unset | Proxy/deposit wallet address, if the account uses one. |
| `POLYMARKET_RELAYER_API_KEY` | unset | Your own Relayer API key (polymarket.com → Settings → Relayer API keys), for gasless `split_position` / `merge_positions` / `redeem_positions`. |
| `POLYMARKET_RELAYER_API_KEY_ADDRESS` | unset | The address the relayer key was issued for. Both halves are required; without them the gasless tools send nothing. |
| `ODDSRAIL_MAX_ORDER_NOTIONAL` | unset | Guardrail: max USDC notional per order. Enforced before any request, in dry-run too. |
| `ODDSRAIL_MAX_SESSION_NOTIONAL` | unset | Guardrail: max cumulative notional of live orders submitted by this server process. |
| `ODDSRAIL_MAX_OPEN_ORDERS` | unset | Guardrail: max resting orders on the account (live; checked against the venue before placing). |
| `ODDSRAIL_ALLOWED_MARKETS` | unset | Guardrail: comma-separated Polymarket token ids and/or Kalshi tickers the agent may trade. Anything else is refused. |
| `ODDSRAIL_PAPER` | `1` | Paper-trade dry-run Polymarket orders against the live book. `0` disables. |
| `ODDSRAIL_PAPER_LEDGER` | `~/.oddsrail/paper.json` | Where the paper ledger lives. One local JSON file. |
| `ODDSRAIL_PAPER_BANKROLL` | `1000` | Starting paper cash in USDC. |

## Status

**Offline tests:** 111 tests covering the paths where a bug costs money: the
Kalshi yes/no→bid/ask translation, Kelly sizing, book walking, cross-venue
pairing, signal edge cases, the dry-run safety net, and jurisdiction-failure
handling (a geoblock must never read as an empty search result or a resting
order). They need no keys and no network:

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
a real Kalshi account, because the author does not yet have a funded,
verified one. This is untested, not untestable. Treat `kalshi_place_order` as
unproven and start in dry-run.

## Where this works

Two different things can stop oddsrail from trading, and they have opposite
remedies. One is a venue restriction, enforced at the order. The other is a
network filter, which breaks the connection itself.

**Polymarket restrictions.** Polymarket publishes its restricted-jurisdiction
list as an API reference: <https://docs.polymarket.com/api-reference/geoblock>.
There are three tiers. OFAC-sanctioned jurisdictions (Iran, Syria, Cuba,
North Korea, and the Crimea, Donetsk and Luhansk regions of Ukraine) are
blocked on both the frontend and the API, with no new orders *and* no closing
of existing positions. A longer second tier is **close-only on both the
frontend and the API**: existing positions can be closed, new ones cannot be
opened. It includes the United States, the United Kingdom, France, Germany,
Italy, Poland, Slovakia, Belgium, Singapore, Australia, New Zealand, Brazil,
Russia, Taiwan, Thailand and the Canadian provinces of Ontario, Quebec,
British Columbia and Alberta. A third group, Ireland, Japan, Malta (sports
only) and the Netherlands, is close-only on Polymarket's frontend, with the
API explicitly not restricted.

Note the shape of that failure: it lands on the order, not the connection.
Public reads answer normally, so oddsrail will look like it is working right
up until an order is rejected. Verified against Polymarket's documentation on
2026-08-31; Polymarket updates the list without notice, so read the URL
rather than this paragraph.

**Kalshi restrictions.** Kalshi is a single CFTC-designated contract market
and it does admit members outside the United States, but its Member
Agreement §VI names a long list of Restricted Jurisdictions whose members may
not trade Event Contracts, among them Australia, Belgium, Canada, France,
Ireland, Italy, New Zealand, Poland, Portugal, Singapore, Switzerland, the
United Kingdom, Hungary, India, the United Arab Emirates and mainland China.
The list is published in Kalshi's Exchange Notice of 22 June 2026
(<https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Kalshi%20Exchange%20Notice%20(Updated%20Member%20Agreement)%20(22%20June%202026).pdf>),
and Kalshi reserves the right to change it. The same section is explicit that the
restriction applies only to *trading Event Contracts* and does not by itself
bar membership or non-trading access, so oddsrail's Kalshi read tools stay
usable even where its order tools do not.

**The two lists overlap heavily.** Kalshi is not a general fallback for a
Polymarket-restricted operator, and the difference runs in both directions.
Among the jurisdictions polymarket.com lists as close-only, Germany, Brazil,
Slovakia and the United States are not on Kalshi's restricted list; Japan and
the Netherlands are restricted by neither API (only by Polymarket's
frontend). Check both lists for your own jurisdiction rather than assuming
the other venue is open. The US case has its own wrinkle.

**The United States.** polymarket.com, the venue oddsrail talks to, is
close-only for the US. Polymarket separately operates Polymarket US
(polymarket.us), run by QCX LLC as a CFTC-regulated Designated Contract
Market. **oddsrail does not support it.** It is a different API host, a
different authentication model (API-key headers rather than EIP-712 wallet
signatures), a different SDK and a different funding rail. A polymarket.us
account and its keys will not work with this server. Kalshi does not list the
US as restricted, so for a US operator Kalshi is the venue oddsrail can
actually reach, with no builder-code attribution, since Kalshi's REST API
has no such field.

**Network filters.** Separately from any venue rule, a national filter can
block the domains outright. Turkey does this: Polymarket does not restrict
Turkey, and Turkey is not on Kalshi's list either, but Turkish ISPs block
polymarket.com. That is a connectivity problem, not an eligibility one, and
it looks different: DNS failures, TLS errors, resets, or an ISP interstitial
page served where JSON was expected. oddsrail classifies both shapes and
tells the calling agent which one it hit.

**Eligibility is the operator's, not the tool's.** oddsrail is self-hosted
and non-custodial, which is a real advantage and also means *you* hold the
account and *you* make the venue's representations; there is no intermediary
making them for you. Polymarket's trading flow requires an attestation that
you are not a U.S. person, are not located in a restricted jurisdiction, and
are not "using a VPN or other measures to circumvent or attempt to
circumvent" restrictions, and states that Polymarket reserves the right to
put a non-compliant wallet in close-only mode. Kalshi's §VI is a
representation about where you are domiciled, organized and located, re-made
each time you place an order. `server_info` reports Polymarket's geoblock
verdict for this machine's IP, but a technical probe is not a compliance
check: the terms bind on residence, citizenship and incorporation, not on
egress IP. Read the terms; if any of this matters to you, get your own legal
advice. Nothing here is legal advice.

The signal logic, the MCP layer and the whole test suite run fine offline
regardless.

## Guardrails: limits the agent cannot argue with

Anyone handing keys to an agent wants three things first: a cap on one order,
a cap on a session, and a fence around which markets it may touch. All three
are operator-set environment variables (table above), enforced *before* any
request goes out, in dry-run as well as live, so the agent meets the fence in
rehearsal. A refusal is a structured answer that names the rule, the limit
and the request:

```json
{"accepted": false, "blocked_by": "guardrail", "rule": "max_order_notional",
 "limit": 25.0, "requested": 99.5, "note": "refused by an operator-set guardrail ... Nothing was sent."}
```

The session counter lives in the server process; restarting it resets the
budget, which is the operator's call. `server_info` reports the active limits
and how much of the session budget is used.

## Paper trading: dry-run with a memory

By default, every dry-run Polymarket order is filled against the **live**
order book, walked within the limit price; whatever does not fill rests as a
paper order and fills later if the market crosses it. `paper_positions`
reports cash, positions at current marks, realized and unrealized P&L and the
resting paper orders; `paper_reset` starts over. The ledger is one local JSON
file. Be clear about what this is: fills assume no queue position, no latency,
no market impact and no fees, so paper results are an upper bound on the same
strategy live. Kalshi dry-run orders still return the intent only.

## Realtime: watch the book move

`watch_book(token_id, seconds, max_events)` subscribes to a token's realtime
stream and returns the events that arrived (book snapshot, then price changes
and trades), bounded to at most 60 seconds so an agent cannot hang a session
on a quiet market. Use it after `get_orderbook` when the decision depends on
the book *moving*, not just where it is.

If the stream fails with `CERTIFICATE_VERIFY_FAILED` while the REST tools
work, your Python has no CA bundle (common with python.org macOS installs).
oddsrail classifies that as `local_tls` and tells the agent the fix: run
`Install Certificates.command` from the Python folder in /Applications, or
set `SSL_CERT_FILE` to the path printed by `python -m certifi`.

## Gasless position management (relayer)

Three tools move collateral without paying gas, through Polymarket's relayer:
`split_position` (USDC → a full YES+NO set), `merge_positions` (matching
YES+NO → USDC, or `max`), and `redeem_positions` (a resolved market's winning
shares → USDC). All three respect dry-run and return the relayer transaction
id and hash plus the terminal outcome.

They use **your own** Relayer API key, created at polymarket.com → Settings →
Relayer API keys and exported as `POLYMARKET_RELAYER_API_KEY` +
`POLYMARKET_RELAYER_API_KEY_ADDRESS`. That is the pattern Polymarket's builder
team recommends for a self-hosted tool: no builder secret ships with oddsrail,
and each operator authenticates the relayer as themselves. Relayer limits are
per builder tier: 100 requests/day unverified, 10,000 verified. Without the
key the tools return a structured "not configured" answer and send nothing;
they never fall back to a gas-paying broadcast from the signer.

**Not yet exercised live:** the relayer path has been driven in dry-run and
against input validation only; no real split, merge or redeem has been sent
from this code yet. Start in dry-run and read back the intent.
`redeemable_positions` lists what the configured wallet could redeem or merge
right now, and the `settle_resolved` prompt chains the two.

## Kalshi (venue #2)

Kalshi is **bring-your-own-key and single-tenant by design**: the operator
supplies their own API key, trades their own account, and this server caches
nothing. That is deliberate: Kalshi's Developer Agreement limits API use to a
member's own trading (§3), bars facilitating other members' trading (§3.2) and
sublicensing the API (§3.7), and restricts storing/sharing API data (§3.1). A
hosted multi-tenant Kalshi service would not be compliant; a self-hosted one is.

**Attribution does not exist here.** Kalshi Builder Codes are a
Solana/DFlow/Jupiter integration; there is no builder or affiliate field
anywhere on the REST API, so Kalshi order flow cannot be attributed or
monetised the way Polymarket's can. Kalshi is in oddsrail for coverage and
signal reach, not for routing revenue.

Two shapes on this API are easy to get wrong, so oddsrail normalises both:

- **Prices are dollar strings, not cents** (`"0.5600"`), sizes are fixed-point
  strings (`"10.00"`); the legacy integer-cent fields were removed in March
  2026. All arithmetic uses `Decimal`.
- **The orderbook is bids-only on both sides.** `yes_dollars` and `no_dollars`
  are both bid ladders, ascending, so the best bid is the *last* element, and
  a NO bid at $0.99 *is* a YES ask at $0.01. `kalshi_get_orderbook` returns a
  conventional best-first bid/ask view of the YES book plus the raw ladders.

Order placement speaks natural terms, `outcome` (yes/no), `action`
(buy/sell), `price` = probability of that outcome, and translates to Kalshi's
YES-book `bid`/`ask` internally (buy NO @ 0.25 becomes ask @ 0.75). That translation is
exhaustively unit-tested (`tests/test_money_paths.py`), since it is the
obvious place to ship an inverted-position bug.

Credentials: `KALSHI_KEY_ID` plus `KALSHI_PRIVATE_KEY_PATH` (PKCS#8 PEM) or
`KALSHI_PRIVATE_KEY`. Set `KALSHI_DEMO=1` to hit the demo environment. Read
tools need no key at all.

## Cross-venue tools

- **`find_markets(query)`**: searches Polymarket *and* Kalshi in one call and
  returns one normalised shape per market: `venue`, `market_id` (the id that
  venue's order tool takes), `title`, yes/no price as probabilities in (0,1),
  best bid/ask, spread, 24h volume, close time, and `trade_with` naming the
  tool to call. Use this when you do not already know the venue.
- **`quote_cost(venue, market_id, side, size)`**: what a size would *actually*
  cost, by walking the book rather than reading the top level. Returns average
  fill price, slippage vs best, notional, levels consumed, and whether the size
  is fillable at all, plus Polymarket's per-market fee schedule where it
  publishes one. Kalshi does not publish fees in its market payload, so they
  are reported as unknown rather than estimated.
- **`compare_venues(query)`**: candidate same-event listings across venues.
  **Not an arbitrage scanner.** Matching an event across venues is an
  unsolved entity-resolution problem: naive title overlap cheerfully pairs a
  Brazilian election with a Ukrainian one and reports a 70-point "gap" that is
  fiction. Two gates apply (title similarity ≥ 0.5 *and* close dates within a
  week), so it usually returns nothing, which is the honest answer. A price
  delta between candidates is reported as `yes_price_difference`, never as
  profit.

### Kalshi search

Kalshi has no text-search endpoint. oddsrail searches by **event** (the
human-readable index, with `with_nested_markets`) rather than paging tens of
thousands of machine-named markets, and matches on word boundaries, without
that, "fed" matches "German Bundestag" and a Fed-rate query returns German
election markets. Results carry `truncated`, because a bounded scan means an
empty result is not proof a market does not exist.

## Order lifecycle & discovery

- `order_status(order_id)`: resting / partially_filled / filled / gone, with
  size_matched. The answer an agent needs after place_order.
- `my_fills()`, `my_positions()`: the operator's executions and holdings,
  no address juggling. (Fills come from the Data API activity feed; the
  SDK's list_account_trades returns the market's *public* tape and is not
  used.)
- `cancel_all_orders()`: kill switch, flattens every resting order at once.
- `resolution_criteria(venue, market_id)` returns the full resolution contract:
  what resolves YES, who resolves it, from which sources. Read it before
  trusting a price.
- `closing_soon(hours)`: markets closing within N hours on either venue,
  where activity concentrates.

## Workflow prompts

MCP prompts show up in clients as ready-made workflows, and they encode the
*order* of operations that keeps an agent out of trouble; the sequencing is
the expertise, which a flat tool list cannot convey.

- `/find_fade_setup(query, bankroll)`: signal → book → cost → resolution →
  size → dry-run, with the rejection criteria at each step
- `/check_cross_venue_edge(query)`: candidates → settlement audit → cost on
  both legs, and says plainly when the answer is "no edge"
- `/daily_review`: positions, resting orders, fills, closing-soon, attribution

## Risk & settlement

- **`settlement_audit(polymarket_id, kalshi_ticker)`**: the check that decides
  whether a cross-venue price difference is an edge or a mismatch. Compares
  close times, resolution sources, UMA dispute status and market structure on
  **live data with no pre-curated pair list**, returning `ok` / `caution` /
  `block` with reasons, and listing the checks it did *not* perform.
- **`position_size(bankroll_usd, price, fair_value)`**: fractional-Kelly sizing,
  capped, refusing negative-edge bets, returning its own assumptions.

## Tools (32)

- `search_markets`, `get_market`, `get_orderbook`, `price_history`,
  `get_positions`: read-only, no keys
- `overshoot_signal`, premium: fresh panic-jump detection + this market's
  historical reversion tendency (ported from the polymarket-wc analyzer)
- `dispute_risk`, premium: transparent 0–100 heuristic for contested
  (UMA-dispute-prone) resolutions
- `place_order`, `cancel_order`, `open_orders`: trading, dry-run by default.
  `price` is a probability in (0,1), `size` is in SHARES, and the exchange
  enforces a **$1 minimum notional** on marketable orders. Trading tools carry
  `destructiveHint` annotations so clients can gate them.
- `builder_stats`: attribution verification + public builder leaderboard
- `find_markets`, `compare_venues`, `quote_cost`: cross-venue (above)
- `server_info`: config status, per-venue

Kalshi: `kalshi_search_markets`, `kalshi_get_market`, `kalshi_get_orderbook`,
`kalshi_get_trades`, `kalshi_balance`, `kalshi_positions`,
`kalshi_open_orders`, `kalshi_place_order`, `kalshi_cancel_order`.

## Stack notes

- Official unified SDK `polymarket-client` (0.6.x): `AsyncPublicClient` for
  data, `AsyncSecureClient.place_limit_order(..., builder_code=...)` for
  attributed orders. The legacy `py-clob-client` is archived and cannot
  attach builder codes. Do not use it.
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
  MCP helpers currently target mcp 1.x, so integrating means pinning
  `mcp>=1.28,<2` or waiting for the 2.x-compatible release. Mainnet
  settlement needs a facilitator (Coinbase CDP: 1,000 free settlements/mo,
  then $0.001). Keep free tiers of both signals so registries can index the
  server.

## Who this is for

Polymarket's public builder leaderboard shows what a single operator routing
their own flow is worth. Pulled **2026-08-31** via this server's own
`builder_stats` tool. Re-run it, the numbers move:

| | weekly volume |
|---|---|
| #1 (traderline) | $7.70M |
| median of top 25 | $533K |
| entry to top 25 | $140K |

The instructive rows are the small ones: **MagicMarkets routes $901K/week with
a single active user**; Jupiter $515K with one; Sharkbetting $1.15M with two.
Those are bot operators routing their own flow, which is exactly who this is
built for.

## Roadmap

1. ~~Live smoke test from an unblocked network~~: done 2026-08-23, all tools pass
2. Register builder code (polymarket.com → Settings → Builders), set fees to
   0 bps at launch, export `ODDSRAIL_BUILDER_CODE`; first attributed order on
   a tiny size
3. ~~Kalshi as venue #2~~: done 2026-08-23, 9 tools, verified live
4. x402 paid wrapping for the two signals once the mcp-2.x conflict clears
5. Registry listings: official MCP registry (`mcp-publisher`, PyPI
   `mcp-name:` marker), Smithery (needs public streamable-HTTP + a free
   tool for their scanner), Glama (`glama.json`)

## Listing / distribution

- **GitHub**: https://github.com/hmesutozsoy/oddsrail (public, MIT)
- **Glama**: auto-crawls GitHub; `glama.json` in the repo root claims
  maintainership.
- **PyPI**: https://pypi.org/project/oddsrail/ (`pip install oddsrail`)
- **Official MCP registry**: listed as `app.oddsrail/polymarket-kalshi-trading` (renamed from `…-arbitrage` in 0.10.1; the old name is deprecated)
  (published 2026-08-30, status active). Re-publish after a version bump with
  `mcp-publisher publish`; keep `server.json`'s version in step with
  `pyproject.toml` or the registry rejects it.
- **Smithery**: requires a public HTTPS streamable-HTTP endpoint, available
  once oddsrail is hosted rather than run locally over stdio.
