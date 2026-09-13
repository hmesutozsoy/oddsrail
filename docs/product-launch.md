# Product implementation and launch dependencies

Internal engineering and pilot notes, reviewed 2026-09-12. These notes describe
the current implementation and acceptance conditions for later work. They are
not a security certification or a claim that hosted live trading is available.

## Current boundary

The product flow no longer exposes paper trading, simulation accounts, test
passes, or legacy competition boards. Existing backend ledgers and internal
regression fixtures are retained. Newly saved website configurations use
`mode: draft`; only the review endpoint accepts that mode, and execution and
scheduling reject it regardless of schema version.

The quoting strategy is first for new drafts, with shares per outcome visible
and price distance labeled in cents. Existing saved strategy choices remain
unchanged. Cents replace probability-point labels without changing numeric
values; relative-percent risk controls keep their original units.


The guided builder preserves the five strategy families: fade, settlement,
user forecast, momentum, and two-sided quotes. It also preserves the market,
risk, and execution settings. The optional description is draft metadata. It
does not change executable rules, invoke a model, or authorize a trade.

The hosted runner executes deterministic paper passes. The legacy hourly scheduler
does not provide continuous supervision between passes. Existing self-hosted
MCP installations have a separate live trading path with operator credentials.
A browser connection or a saved draft does not create a live execution account.

Keep `/capabilities` authoritative for availability: hosted live execution,
session authorization, hosted AI research, and provider API-key connections must
remain unavailable until the corresponding backend is implemented and verified.

## Wallet navigation and public dashboard

The shared header connects an injected browser wallet through account exposure
only. A session-storage flag allows passive `eth_accounts` restoration; there is
no stored address claim, signed login, owner-key collection or CLOB credential.
Switching or disconnecting clears displayed account data immediately. Both request
generations and abort signals fence late portfolio responses. The guided builder
subscribes to this shared connection and has no second connection control.

`GET /portfolio?address=...` reads Polymarket's public profile wallet association,
then the current V2 positions, activity and aggregate holdings endpoints. It does
not use the operator's credentials, hosted bearer token or existing paper ledger.
A missing profile mapping stays unresolved; it does not become an empty portfolio
on the connected owner address. Public profile association is not ownership proof
and does not discover every linked account.

Cash and the cash-inclusive total remain unavailable until a verified account
balance integration exists. V2 holdings value excludes cash and includes unresolved
combo cost; the position table lists single-market token positions. Bounded pages,
partial results and section failures remain visible. No P&L chart, funding action,
order cancellation or hosted trading activation is implied by the dashboard.

The Agents tab contains existing browser drafts, explicitly independent of wallet
ownership. The wallet menu and post-save flow open this tab through `/portfolio?tab=agents`.
Public address handling and transient portfolio data are described in the privacy page.

## Configuration and selection contract

`POST /config/validate` validates the original configuration and returns a
normalized review summary. Preserve and submit the original reviewed configuration.
The runner's normalized dictionary is an internal representation, not a
round-trip replacement for the original `vals` and form fields. Reject active
unknown rules, invalid values, and conflicting limits instead of silently
removing or changing requested behavior. Validate version 2 configurations
again when running or saving them; the review endpoint alone is insufficient.

The frontend must make any difference between the reviewed settings and server
summary visible before execution. Editing after review invalidates the reviewed
state. A saved description must never be treated as permission to bypass limits.

| Field or endpoint | Meaning |
| --- | --- |
| `market_mode: universe` | Combine the selected categories and optional search keyword into a candidate pool. This is a union, not a keyword intersection. |
| `market_mode: specific` | Resolve and use only the explicit outcome token IDs. Empty, invalid, or unresolved selections do not fall back to broad discovery. |
| `market_ids` | At most 20 exact decimal token-ID strings. Adding a complete binary market includes both outcomes. Do not convert IDs to JavaScript numbers. |
| `GET /markets/search?q=` | Browse actual public venue data; a nonempty query searches it. No sample entries should be presented as live results. |
| `GET /markets/resolve?q=URL` | Parse a supported Polymarket page URL and use venue lookup methods. Never fetch the supplied URL as an arbitrary address. |

An event can contain several markets. Display those choices explicitly. Exact
token identity must survive lookup, review, storage, and execution. Specific
selection also fences user-forecast matching and settlement scans. Existing
holdings may still receive risk-reducing exits after the selection changes;
out-of-scope resting entries must be cancelled before ledger refresh.

## Units and execution semantics

| UI setting | Actual meaning |
| --- | --- |
| Price `0.52` | A normalized price of 0.52 currency units per share, also displayed as an implied 52% probability. Ten shares have 5.20 gross notional before fees. |
| Fade jump, momentum move, quote distance | Absolute price distance in cents. Eight cents means a price change of `0.08`, not an 8% relative price change. Numeric values are unchanged from the legacy point labels. |
| Stop loss and take profit | Relative change from the position's average entry price, checked at a runner review. The fill price depends on available bids. |
| Maximum slippage | Relative difference from the best displayed price to the walked average fill price, not probability points. |
| Per-market exposure | Cost of holdings plus reserved entry commitments under the applicable runner version, not current marked value or maximum possible loss. |
| Open positions | Distinct held outcomes plus resting BUY outcomes in version 2. A fresh YES/NO quote pair needs two available positions; an already held outcome is counted only once. |
| Daily entry-loss threshold | Equity change from the first recorded pass of the UTC day. It is not a guaranteed loss floor or necessarily a midnight valuation. |
| Allocated capital | Version 2 must cap combined position cost and reserved BUY notional. Legacy `bankroll` only controls Kelly sizing and must not be described as a hard allocation cap. |
| Paper balance | Starts at 1,000 simulated units independently of the sizing input. Paper fills omit fees, queue priority, and market impact. |

After resizing, price adjustment, or an exchange-minimum check, recheck final
order notional against per-order, market, and capital limits. A minimum-size
rule must never raise an order above an approved cap. Reserve resting and
partially filled entry commitments without double-counting completed fills.
Cancel resting entry orders when a version 2 daily halt becomes active.

Do not promise strategy behavior solely because it appears in an external
prompt. The deterministic runner does not implement the half-retracement exits
described in the legacy fade and momentum prompts. Momentum uses a minimum
volume filter, not a rising-volume detector. The runner's book check compares
two snapshots; the MCP `watch_book` tool provides a bounded subscription.
The paper ledger now supports resolution payouts under its resolution checks;
legacy text saying it never pays at resolution must be corrected.

Kalshi normalization remains a separate self-hosted capability. Its adapter
consumes dollar-price strings and derives the YES ask from the NO bid. The
guided Polymarket builder must not interpret Kalshi cents, tickers, or keys as
Polymarket prices, tokens, or credentials.

## Hosted live dependencies

Polymarket's session-key beta currently supports Deposit Wallets, with a separate
authorized signer and Builder API enablement during rollout. Session keys cannot
withdraw, but trading can still lose funds. Authorizations expose venue scopes;
do not represent OddsRail's market or spending limits as venue-enforced session
restrictions. Prefer the necessary CLOB scope to the broader default. Source:
[official session-key documentation](https://docs.polymarket.com/trading/session-keys).

The project's `polymarket-client>=0.6.0` dependency baseline does not guarantee
session-key support. The Python SDK changelog adds that support in 0.7.0 and
changes authorization expiry handling in 0.7.1. Select and test a compatible
release, then lock deployment dependencies. Do not copy TypeScript version
numbers or expiry parameters into Python integrations. Source:
[official SDK changelog](https://docs.polymarket.com/changelog/sdks).

The following are implementation requirements, not completed capabilities:

1. **Account identity and compatibility.** Verify owner identity through a
   server-issued, expiring, replay-protected signing challenge. Discover the
   actual trading account type, ownership, balances, approvals, and chain.
   Reuse supported balances where possible. Do not require a second deposit
   just because a wallet was connected. Unsupported account migration must be
   an explicit separate flow.
2. **Program enablement and authorization.** Confirm session-management access
   for the builder. Keep owner authorization separate from login and trading
   activation. Confirm the authorization and active signer registry before
   declaring the agent ready. Test rejection, expiry, revocation, interrupted
   authorization, and owner or network changes.
3. **Dedicated signer and key protection.** Design a signer identity and policy
   boundary per agent or account. Protect private signing material with a
   managed secret store, restricted service access, rotation, and redacted
   logs. Do not store owner private keys or session secrets in browser storage,
   strategy descriptions, model context, analytics, or shared artifacts.
4. **Persistent execution policy.** Enforce market scope, final order limits,
   reserved capital, exposure, and daily entry rules in code immediately before
   submission. Define budget periods, resets, and account aggregation. Survive
   restarts and concurrent workers with durable reservations and an audit log.
   Existing process-local session counters are not sufficient for a hosted
   multi-user budget guarantee.
5. **Reconciliation and supervision.** Persist order intent and identifiers
   before submission. Reconcile accepted, rejected, unknown, partially filled,
   cancelled, and settled states from authoritative account data. An unknown
   result must not trigger a fresh submission. Validate complete pagination,
   duplicate notifications, stale data, process interruption, and recovery.
   A continuous runner needs supervised workers and explicit freshness rules.
6. **Stop, cancel, and revoke behavior.** Provide separate controls and accurate
   status for pausing decisions, cancelling resting orders, and revoking signer
   access. Verify what remains open after each operation. Revocation must not
   be described as closing existing positions.
7. **Eligibility and access.** Check current venue eligibility, jurisdiction,
   and account restrictions before activation. Keep read availability separate
   from permission to place new orders. Do not infer eligibility from a healthy
   market-data endpoint or a builder badge.
8. **Independent review and bounded live evidence.** Review auth, signing,
   account isolation, policy enforcement, and accounting. Exercise small,
   explicitly authorized live transactions and revocation recovery. Record
   evidence without secret material. Passing offline tests does not establish
   that the deployed service is fully secured.

## Competition and pitch sequence

Start with a private pilot of ten independently operated entrants before a
public competition or grant pitch built around live performance. No cash-prize
claim is necessary for the pilot. Document failures and accounting differences
as carefully as successful runs.

Use dedicated competition accounts or an equally auditable account scope. Agree
on starting capital, eligible markets, valuation, fees, transfer treatment,
unresolved positions, stale marks, and evaluation windows before the first run.
Preserve strategy-version changes and reset epochs; never join a fresh bankroll
to an earlier equity curve without showing the boundary.

Rank with a documented performance measure and show drawdown, elapsed time,
activity, and valuation coverage. Do not rank by trading volume or reward churn.
Attributed volume is a separate contribution metric. Whole-wallet P&L must be
labelled as such when it includes activity outside OddsRail.

Public fills alone do not establish who or what made the decision. Describe
operator-declared automation separately from runs observed through the OddsRail
runner. Preserve decision traces while acknowledging that they do not prove an
operator never intervened.

Pilot evidence should include completed onboarding, repeat participation,
non-house attributable fills, reconciled account histories, successful pause
and recovery, and explicit unresolved issues. Publish concrete results and a
bounded next milestone. Do not substitute simulated returns, a redesigned
landing page, or Verified Builder status for that evidence.
