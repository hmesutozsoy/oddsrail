# Live proof: gasless split and merge through the relayer

Run on 2026-09-02 from oddsrail 0.10.1, on the maintainer's small test
account, using the operator's own Relayer API key exactly as the README
describes (`POLYMARKET_RELAYER_API_KEY` + `POLYMARKET_RELAYER_API_KEY_ADDRESS`,
`ODDSRAIL_DRY_RUN=0`). Egress at the time: Kazakhstan, which Polymarket's
geoblock endpoint reports as permitted.

| step | tool call | relayer transaction id | Polygon transaction |
|---|---|---|---|
| split 1 USDC into a YES+NO set | `split_position(condition_id, 1.0)` | `01a06210-7d3d-71c2-b61c-4aee226b927b` | [`0x5585b08e…7bc510`](https://polygonscan.com/tx/0x5585b08ef852a957298eeeffea38a62dc93d9ff8fa06b6b676ba4cc0eb7bc510) |
| merge the set back into USDC | `merge_positions(condition_id, "max")` | `01a06210-a2ad-77ef-ba66-52b925d6e318` | [`0xa760b032…fab286`](https://polygonscan.com/tx/0xa760b032756a4f926175ad38745625f574edcab9541514401885810671fab286) |

Market: "Will Bitcoin dip to $45,000 by December 31, 2026?"
(`condition_id 0x024b68f77bfc019341ee3db8f57c103334e4b9430bba4746d8c94aafd8b36fee`).
Account: proxy wallet `0xBCC155806acDc3C881E22816E2f2CcF446fEFE0F`, signer
`0xcb51fA9e1CfC0927dEafd41dA1E13B4AAd23246A`. Both addresses are public; no
key material appears anywhere in this repository.

The split returned in about 10 seconds including the wait for a terminal
relayer state. No gas was paid by the signer; the relayer executed both
transactions. Both receipts show status `0x1` on Polygon.

## What this does and does not prove

Proven: the code path `trading.split_position` / `trading.merge_positions`
→ `polymarket-client` `AsyncSecureClient` with a `RelayerApiKey` → relayer
submit → `wait()` → terminal outcome, on real funds, from a self-hosted
install with the operator's own relayer key.

Not proven: `redeem_positions`. It needs a resolved market in which the
account holds winning shares, and the test account has not held one yet.
The tool is wired identically to the two above (same SDK client, same
relayer path) and is dry-run tested, but until a real redemption is recorded
here it should be treated as unproven. Kalshi order placement is likewise
still unproven live (see the README).

## How to check

Open either Polygonscan link. The receipts are status `0x1` in blocks
93097742 (split) and 93097748 (merge), and their logs carry the Conditional
Tokens `PositionSplit` / `PositionsMerge` events for the condition id above.
Independently: `eth_getTransactionReceipt` on any Polygon RPC returns the
same two receipts.
