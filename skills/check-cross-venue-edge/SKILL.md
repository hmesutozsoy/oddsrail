---
name: check-cross-venue-edge
description: "Decide whether a Polymarket vs Kalshi price difference is a real edge or a settlement mismatch: compare_venues, settlement_audit, quote_cost on both legs, resolution criteria."
---

# check cross venue edge

Use the tools of the `oddsrail` MCP server (install: `pip install oddsrail`, then `claude mcp add --transport stdio oddsrail -- oddsrail`, or enable the oddsrail plugin). Prices are implied probabilities in (0,1). Trading tools are dry-run unless the operator has set ODDSRAIL_DRY_RUN=0; never assume an order was placed without checking the `dry_run` field of the result.

Investigate whether '$ARGUMENTS' offers a genuine cross-venue edge.

1. compare_venues('$ARGUMENTS') — candidates only. It returns nothing for most
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
correct outcome.
