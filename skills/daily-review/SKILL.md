---
name: daily-review
description: Daily review of open prediction-market exposure with oddsrail: positions, resting orders, fills, markets closing soon, builder attribution.
---

# daily review

Use the tools of the `oddsrail` MCP server (install: `pip install oddsrail`, then `claude mcp add --transport stdio oddsrail -- oddsrail`, or enable the oddsrail plugin). Prices are implied probabilities in (0,1). Trading tools are dry-run unless the operator has set ODDSRAIL_DRY_RUN=0; never assume an order was placed without checking the `dry_run` field of the result.

Review current prediction-market exposure.

1. my_positions() — what is held, and at what marks.
2. open_orders() — what is still resting; for anything stale, order_status()
   to see whether it partially filled.
3. my_fills(limit=25) — what actually executed since the last review.
4. closing_soon(hours=24) — positions or orders in markets about to resolve
   need a decision now.
5. builder_stats() — confirm routed flow is being attributed.

Flag: resting orders far from the current book, positions in markets with an
open UMA dispute (dispute_risk), and anything resolving within 24h.
