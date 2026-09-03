---
name: find-fade-setup
description: "Find and evaluate a fade (mean-reversion) setup on prediction markets with oddsrail: candidates, overshoot signal, book, walked cost, resolution criteria, Kelly size, dry-run order."
---

# find fade setup

Use the tools of the `oddsrail` MCP server (install: `pip install oddsrail`, then `claude mcp add --transport stdio oddsrail -- oddsrail`, or enable the oddsrail plugin). Prices are implied probabilities in (0,1). Trading tools are dry-run unless the operator has set ODDSRAIL_DRY_RUN=0; never assume an order was placed without checking the `dry_run` field of the result.

Find a fade setup on prediction markets related to: $ARGUMENTS.

Work in this order and stop if a step fails:
1. find_markets('$ARGUMENTS') — pick liquid candidates (volume_24h > 10000).
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
6. position_size(bankroll_usd=<bankroll_usd>, price, fair_value) where
   fair_value is the pre-jump price if you believe it fully reverts.
7. place_order(...) — dry-run first and read back the intent before setting
   ODDSRAIL_DRY_RUN=0.

Report the candidates you rejected and why; a rejected setup is a result.
