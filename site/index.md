# oddsrail

The rail AI agents use to trade prediction markets.

Live books, costed fills, settlement audits and non-custodial order routing in one open-source MCP server your agent can call. Free to run. 0 bps on every trade. Keys never leave your machine.

```
pip install oddsrail
claude mcp add --transport stdio oddsrail -- oddsrail
```

- Status: Verified Polymarket builder
- Surface: 39 tools, 4 workflow prompts
- Cost: 0 bps added to trades
- Default: dry-run, with a paper ledger
- License: MIT, self-hosted

## What a session looks like

Captured 2026-09-02. Tool results abridged; the numbers are the venue's.

```
you    find markets about bitcoin and quote 100 shares on the favourite

agent  find_markets("bitcoin")
       {"markets": [{"venue": "polymarket",
         "title": "Will the price of Bitcoin be above $72,000 on September 2?",
         "yes_price": 0.991, "best_bid": 0.988, "best_ask": 0.994,
         "spread": 0.006, "volume_24h": 196435, "market_id": "4430…2778"}, …]}

agent  quote_cost("polymarket", "4430…2778", "BUY", 100)
       {"cost": {"fillable": true, "filled_size": 100, "avg_price": 0.994,
         "notional": 99.4, "slippage_vs_best": 0.0,
         "levels_consumed": [{"price": 0.994, "size": 100}]},
        "fees": {"fees_enabled": true, "fee_type": "crypto_fees_v2", "taker_only": true}}

agent  100 YES on "Bitcoin above $72,000 on Sept 2" costs $99.40 at 0.994, all at
       one level, no slippage. It pays $100 if it resolves YES: 0.6% gross before
       the taker fee. place_order is in dry-run; say the word and I will read back
       the intent before anything is signed.
```

## How it compares

| Capability | oddsrail | raw Polymarket API | raw Kalshi API | Crosswire | Parsec |
|---|---|---|---|---|---|
| Both venues, one vocabulary | yes, `find_markets` | single-venue | single-venue | pairs from a frozen 43-pair graph | yes, 5 venues |
| Book-walked cost and slippage | yes, `quote_cost` | book only, math is yours | book only | top-of-book, covered pairs only | preview on their infra |
| Order lifecycle (status, fills, kill switch) | yes | endpoints exist, with footguns | endpoints exist | no, read-only | yes |
| Gasless split, merge, redeem | yes, your own relayer key | relayer exists, auth is yours | n/a | no | managed wallet |
| Operator guardrails | notional caps, allowed markets | none | none | n/a | unknown |
| Paper trading in dry-run | live-book fills, P&L | no | no | no | unknown |
| Resolution criteria surfaced | both venues | buried in fields | buried in fields | deep, on 1 active pair | none |
| Trading signals | overshoot, dispute-risk | no | no | no | no |
| Realtime book streaming | `watch_book`, bounded | websocket, yours to wire | websocket, yours to wire | no | yes |
| Jurisdiction-aware failures | preflight, classified hints | a rejection at the order | enforced at signup | unknown | unknown |
| Open source, auditable | MIT, full source | n/a | n/a | 4-file listing shell, service proprietary | closed |
| Self-hosted, non-custodial | keys never leave your machine | yes | yes | hosted only | stores keys or holds a managed wallet |
| Cost to the trader | 0 bps, free tools | free | free | $0.02/call after 3/day | SaaS $0 to 250/mo; builder program keeps 55 to 85% of fees |
| Safety defaults | dry-run default, destructive-tool annotations, venue quirks pre-encoded | you discover them by rejection | same | n/a | unknown |

Where the others are honestly ahead: Parsec covers 5 venues to our 2. Crosswire's settlement-audit output on its one covered pair is deeper than our `resolution_criteria`, and it takes x402 micropayments natively. Hosted services need zero install.

## Built from the things the raw API gets wrong

1. The book arrives worst-first. Polymarket's raw orderbook lists bids ascending and asks descending, so `bids[0]` is the worst level. oddsrail normalises both venues to best-first and walks the book to quote what a size actually costs.
2. Rejections don't raise. The exchange models a rejected order as a return value. oddsrail branches on it: `accepted: false`, nothing attributed, and an unrecognised response shape is never reported as posted.
3. Geoblocks look healthy until the order. Restricted jurisdictions are enforced at order placement; public reads answer normally. oddsrail classifies every failure shape and gives the agent an actionable hint instead of a retry loop.
4. "Account trades" returns everyone's. The SDK call that sounds like your fills returns the public tape. oddsrail reads the operator's activity feed by wallet, with transaction hashes.
5. Kalshi has no yes/no side. Everything is quoted from the YES book; prices are dollar strings. oddsrail lets the agent say `outcome=no, action=buy` and translates it, tested exhaustively.
6. A price gap is not an edge. oddsrail pairs candidates by similarity and close date, then audits resolution sources, UMA status and structure before anyone calls it arbitrage.

## What's inside

- Market data: search_markets, get_market, get_orderbook, watch_book, price_history, closing_soon, kalshi_*
- Cross-venue: find_markets, compare_venues, settlement_audit, resolution_criteria
- Cost and sizing: quote_cost, position_size, overshoot_signal, dispute_risk
- Trading: place_order, cancel_order, cancel_all_orders, kalshi_place_order, split_position, merge_positions, redeem_positions
- Lifecycle: order_status, open_orders, my_fills, my_positions, redeemable_positions, paper_positions, server_info
- Prompts: find_fade_setup, check_cross_venue_edge, settle_resolved, daily_review

## Where the builder code goes

1. Your agent (any MCP client) speaks plain intent: "buy 100 YES at 0.62".
2. oddsrail, on your machine: guardrails, then the dry-run gate; book-walked cost and jurisdiction preflight; the builder code is placed in the order struct, then signed with your key.
3. Venue (Polymarket CLOB, Kalshi): your own credentials; rejections branched, never reported as resting; 0 bps builder fee added.
4. On-chain (CTF Exchange V2): every OrderFilled event carries the code; attribution is public and auditable; weekly pool share paid by Polymarket.

## How it pays for itself without charging you

- Custody: none. You run the server; your keys sign your orders on your machine.
- Fees: 0 bps. Orders carry a builder code registered at 0 bps maker and 0 bps taker.
- Attribution: the code is placed in the CLOB V2 order struct before signing, so attribution is on-chain. The project earns only a share of Polymarket's weekly builder pool.
- Override: set `ODDSRAIL_BUILDER_CODE` to your own code and the share accrues to you instead.
- Status: Verified in Polymarket's builder program; the attribution pattern for self-hosted tools was confirmed by their builder team.
- Guardrails: `ODDSRAIL_MAX_ORDER_NOTIONAL`, `ODDSRAIL_MAX_SESSION_NOTIONAL`, `ODDSRAIL_MAX_OPEN_ORDERS`, `ODDSRAIL_ALLOWED_MARKETS`. Set by you, enforced before any request, in dry-run too.

Both venues restrict trading by jurisdiction; reads work from restricted places. `server_info` reports this machine's geoblock verdict and venue reachability. Kalshi's REST API has no attribution field, so Kalshi flow is never attributed or monetised.

## Live from the builder program

The HTML page renders the weekly builder leaderboard from https://data-api.polymarket.com/v1/builders/leaderboard?timePeriod=WEEK. The same board is at https://builders.polymarket.com/.

## Links

- GitHub: https://github.com/hmesutozsoy/oddsrail
- PyPI: https://pypi.org/project/oddsrail/
- MCP registry: https://registry.modelcontextprotocol.io/?search=oddsrail
- Changelog: https://github.com/hmesutozsoy/oddsrail/blob/main/CHANGELOG.md
- llms.txt: https://oddsrail.app/llms.txt

Not financial or legal advice. Trading prediction markets involves risk; eligibility depends on where you are and who you are.
