---
name: settle-resolved
description: Turn resolved and hedged Polymarket positions back into USDC, gasless, through the operator's relayer key, with a dry-run read-back first.
---

# settle resolved

Use the tools of the `oddsrail` MCP server (install: `pip install oddsrail`, then `claude mcp add --transport stdio oddsrail -- oddsrail`, or enable the oddsrail plugin). Prices are implied probabilities in (0,1). Trading tools are dry-run unless the operator has set ODDSRAIL_DRY_RUN=0; never assume an order was placed without checking the `dry_run` field of the result.

Settle whatever can be settled on the operator's Polymarket account.

1. server_info() — confirm relayer_key_configured is true and note dry_run.
2. redeemable_positions() — two lists: redeemable (resolved winners) and
   mergeable (both sides held).
3. For each redeemable entry: redeem_positions(condition_id=...). For each
   mergeable entry: merge_positions(condition_id=..., amount="max").
   In dry-run these return the intent; read each back before the operator
   sets ODDSRAIL_DRY_RUN=0.
4. After live submissions: report transaction ids and outcomes, then
   my_positions() to confirm the balances moved.

Do not resubmit anything whose outcome is "unknown"; report it instead.
