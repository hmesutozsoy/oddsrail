# Prediction API migration and Protocol V2 readiness

Reviewed October 6, 2026 against the public Polymarket migration guides and
Data API v2 OpenAPI schema.

## Implemented

- Remaining direct Data API reads use `/v2`: trader and builder leaderboards,
  operator activity, portfolio value, positions, and price history. Hosted
  portfolio and execution position reads already used v2.
- Trader lookups read the single `data` object, validate `user_id`, and preserve
  unavailable responses instead of manufacturing zero balances or P&L.
- Builder and history pagination use opaque cursors with cycle and page bounds.
- Prediction leaderboard and builder volume is labeled in shares. Perps volume
  remains USD. Finite-window prediction P&L includes marks; lifetime is realized.
- Public market metadata requires an explicit supported version and selects only
  `clobTokenIds` for v1 or `positionIds` for v2. IDs stay decimal strings.
- Hosted V2 execution is restricted to an explicit owner/market canary allowlist,
  empty by default. Other owners and markets are refused before ledger creation.
  The supervisor pins the registered protocol and refuses version changes.
- V2 requires an explicit active resolution status. The buy-only, post-only
  pilot still requires verified zero effective maker fees.
- V2 account checks refresh the authenticated CLOB collateral cache, verify its
  ExchangeV3 allowance, and use the smaller of chain and cached balances and
  allowances. No check submits an approval transaction.
- The raw adapter can derive ExchangeV3/domain-3 hashes and wrapped Deposit
  Wallet signatures in offline tests. Registration is disabled by default;
  production can enable only explicitly listed owner/market pairs.

## Not yet enabled: funded Protocol V2 execution

Existing CTF markets continue using their original exchanges and domain 2.
Do not enable the adapter gate based on signing tests alone. Before enabling:

1. Verify maker pUSD approval to ExchangeV3, including the actual fee budget.
   Existing CTF allowances are insufficient. Approval changes require the
   wallet owner's explicit transaction approval.
2. Verify Deposit Wallet session permissions with the new exchange. The current
   product relies on approvals supplied by the existing Polymarket setup; this
   change does not submit approvals or change any user's permissions.
3. Verify the implemented authenticated collateral cache refresh against the
   real venue. Positions currently use Data API v2; `CONDITIONAL-V2` is required
   if CLOB position balance reads are introduced later.
4. Exercise accepted orders, partial fills, final settlement, cancellation,
   uncertain replies, restart recovery and fees using an approved canary
   account. Retain regression coverage for CTF orders and holdings.
5. Review ExchangeV3 fill rounding before extending the hosted buy-only pilot
   to SELLs. SELL support also requires PositionManager approval.
6. Update raw discovery/selection and the legacy SDK-backed local execution and
   redemption path together before advertising end-to-end V2 strategy support.
   The local MCP candidate now pins SDK 0.12.0; funded V2 execution remains disabled.

Offline tests cover V2 submit-once, partial settlement, cancel and restart
reconciliation, malformed/missing approvals, and owner/market restrictions.
These do not substitute for a funded venue test. No live orders, approval
transactions, credential decryption, or funded canary trades were performed.
No existing position is converted.

## Canary rollout

`ODDSRAIL_LIVE_V2_CANARIES` is a JSON object mapping enabled owner addresses to
lists of approved condition IDs. It defaults to `{}`. Configure it only after
the owner has chosen the market and approved a test spending limit. Existing
agent capital, per-order, exposure, daily-loss and session checks still apply.
This enables only hosted buy-only quoting; it does not upgrade the SDK-backed
local strategy or redemption workflow. Do not enable V2 broadly until the
remaining funded checks above pass.

## Sources

- https://docs.polymarket.com/migrate/data-api-v1-to-v2
- https://data-api.polymarket.com/v2/openapi.json
- https://docs.polymarket.com/migrate/polymarket-v2/api-integrations
- https://docs.polymarket.com/migrate/polymarket-v2/overview

Data API v1 retirement is October 24, 2026. The existing
`/v1/accounting/snapshot` is explicitly exempt from that retirement.

## Open-source MCP review — October 7

PyPI and the desktop bundle currently identify version 0.19.0 with
`polymarket-client==0.6.0`. A website deployment does not update those copies.
The local `trading.place_order` delegates to the SDK, while token discovery in
`polymarket.get_market_by_token` filters `clob_token_ids`. Position management
also delegates to SDK split/merge/redeem methods. Hosted raw adapter tests do
not establish compatibility for those separate paths.

Recommended next release scope:

1. Select market IDs by explicit protocol version throughout local discovery,
   order entry and position lookup; preserve CTF support.
2. Verify the reviewed SDK's ordinary order and position-management methods
   against ExchangeV3/domain 3, BUY collateral approvals, SELL PositionManager
   approvals, V2 balances and resolution status. Do not assume that an SDK
   containing some V3 or RFQ code covers every local MCP operation.
3. Test V1 and V2 routing, cancellation, ambiguous submissions, settlement,
   split/merge/redemption, and unsupported-market refusal before release.
4. Publish a versioned package and matching desktop bundle with upgrade notes.
   Keep dry-run as the default and retain the funded canary requirement.

This review did not upgrade dependencies or enable additional live trading.

## Candidate implementation — 0.20.0rc1

The local MCP now pins SDK 0.12.0 and the hashed CI lock changes only that
package. Discovery selects per-outcome `position_id` for v2 and `token_id`
for v1; unknown versions expose no trading IDs. Exact asset lookup checks
both version-specific Gamma filters and verifies the returned identity.

Local live orders in the V2 namespace are refused before authentication or
signing. Split/merge/redeem for V2 condition IDs are refused; market-ID
redemption verifies the market version and refuses V2 or unknown metadata.
Dry-run remains available. There is no environment switch to bypass this
local validation gate. Existing hosted canary controls are unchanged.

The release candidate is suitable for reviewing migrated data and discovery
and regression-testing existing CTF workflows. It is not full funded V2
support. Before removing the local gate, verify BUY/SELL settlement, approvals,
cancellation and position management with an explicitly approved test budget.
