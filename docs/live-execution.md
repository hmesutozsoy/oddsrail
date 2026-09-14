# Live execution foundation

This package implements the local execution coordinator for the first
Quote both sides pilot. Hosted live trading remains disabled. There is no
production `Venue` implementation, delegated signer, financial HTTP route,
or automatic change to `/capabilities`. The existing hourly paper runner is
unchanged and is not used by this package.

## What is implemented

- `oddsrail/live/market_feed.py`: one bounded public WebSocket connection for
  up to 40 outcome tokens, immutable order books, reconnect generations,
  timestamp checks and freshness checks. Every reconnect requires full books.
- `quotes.py`: pure post-only BUY proposals for one binary market, using each
  outcome's own midpoint, venue tick, minimum size, selected shares and dollar
  cap. Either both proposals validate or the whole pair is refused.
- `observer.py`: bounded public metadata reads and an observation-only CLI.
  Gamma and CLOB identities must agree. It checks both receipt and source
  timestamps, current eligibility, tick changes and explicitly maker-free fee
  semantics. Every output says `submitted_orders: 0`.
- `store.py`: SQLite transactions, account leases with fencing, immutable
  account/agent policies, atomic pair reservations, stable idempotency keys,
  order hashes recorded before submission, and private paginated audit events.
- `engine.py`: account-isolated coordination, one submission attempt per
  intent, account and permission checks before each dispatch, cancellation and
  cumulative fill reconciliation. The venue must be supplied explicitly.
- `supervisor.py`: event-driven evaluation, controlled replacement, periodic
  risk checks, heartbeat supervision, metadata refresh and shutdown cleanup.

The control methods distinguish Start, Pause, Cancel orders and Request
revocation. Pause stops new decisions and preserves existing orders. Account
risk/error halts request cancellation across every managed agent on that
account. Request revocation pauses decisions and records an owner action that
is still required; it never claims a key has been revoked or a position closed.

## Timing

The supervisor wakes on feed updates and checks for stale data at least every
250 ms when it is not awaiting a bounded venue operation. Quote replacement
is limited to once every two seconds or a slower configured cadence. This is
a request limit, not a claim of exchange execution latency.

Risk and permission checks run on a two-second cadence independently of quote
replacement, and immediately before each order submission. Reconciliation
uses four concurrent lookups under one aggregate deadline (five seconds by
default), rather than multiplying a timeout by the number of orders. Metadata
refreshes every 20 seconds and expires after 30 seconds. Book age defaults to
five seconds and source/receipt pair skew to one second; a caller may tighten
these values. A quiet book is deliberately treated as stale until a fresh
snapshot or update arrives. PING/PONG cannot make an old price fresh.

The separate, credential-scoped venue order heartbeat is requested every five
seconds. It stops on an account halt or invalid market data, including while
other account reads are pending. WebSocket PING is only transport liveness.
Neither a heartbeat failure nor a cancellation response proves that an order
is gone. Unconfirmed orders retain their reservations. A failed heartbeat
latches the supervisor into a halt; a new explicit verified Start and a fresh
supervisor instance are required after the cause is resolved.

## Accounting and recovery

Policies and amounts use bounded Decimal inputs; obligations round up to
integer microdollars. Cash and configured budgets require exact microdollar
precision. The calculation context is independent of caller Decimal settings.

Reservations count all managed agents sharing the trading account, plus the
verified snapshot's external reservations, positions and open-order count.
There can be only one nonterminal quote per outcome per agent. Every agent
uses a distinct session identifier within the account. The account coordinator
must know every managed session; a single session's private order list is
insufficient for an account-wide snapshot.

Both proposals reserve their full worst-case cost, including the explicit fee
buffer, in one transaction. Per-order, per-market, agent allocation, account
allocation, available cash, allowance and open-order limits are checked before
dispatch. This first supervisor supports only verified maker-free BUY orders.
It does not implement exits, withdrawals, profit crediting, loss calculation,
or position settlement. The adapter supplies authoritative daily loss and
external exposures; the engine does not infer them from public holdings.

A timeout or interrupted submission becomes unknown. The next worker keeps
the hash and reservation, pauses the agent and reconciles the existing intent.
It then requests cancellation for surviving nonterminal orders across every
managed session, preserving unconfirmed reservations. Already finalized fills
are excluded. Cancellation admits four concurrent requests under a 20-second
deadline, followed by one bounded reconciliation pass. The durable last-attempt
event determines priority; orders a timed-out pass never reached go first next
time, including after restart. Slow sessions cannot continually monopolize the
queue. It does not sign or submit a replacement. A 404 or absent lookup is not evidence
of rejection. The hash and unsigned economic request are durable; this
implementation never retries a signed payload, so it does not persist one.
Secrets, signatures, credentials and raw venue exceptions are excluded from
the ledger and status objects.

Cancel acknowledgments do not release capital. Final observations require
complete order/trade reconciliation and no unsettled matched amount. Confirmed
and permanently failed quantities are monotonic and deduplicated. Prior
acceptance cannot be erased by an unknown/cancel state and then reported as a
rejection. Duplicate terminal observations do not move cash timestamps.

Confirmed purchases stay committed against allocation and market exposure,
including across restarts and UTC day boundaries. A finalized fill keeps its
cash hold until the next verified balance snapshot whose reads began after
settlement. This is a conservative BUY-only pilot ledger; capital recycling
needs a separate reviewed position/settlement implementation. No reset method
silently releases filled capital. A lease protects one SQLite database on one
host; it is not a distributed deployment or horizontal scaling guarantee.

## Public observation command

Install the project's `cloud` extras, then run:

```bash
python -m oddsrail.live.observer --market <canonical-condition-id> --seconds 30 --shares 20 --distance-cents 2 --per-order 10
```

The market argument is a lowercase condition ID, not a token, URL or slug.
Duration is bounded to 300 seconds. No wallet, API credential, signing key or
financial endpoint is used. The observer reports pauses and prospective quotes;
it does not simulate fills, calculate PnL or create an agent.

Current venue documentation disagrees about whether some minimum-size fields
represent shares or dollars. The observer enforces both CLOB share minimum and
Gamma's documented notional minimum. Therefore a $3 cap can legitimately
produce no proposal where the conservative minimum is $5. This must be settled
against the supported financial API before a live launch; never increase an
order above the user's cap to satisfy a minimum.

## Remaining launch gates

1. Confirm Polymarket Builder enablement for session-key management and Deposit
   Wallet compatibility. Implement the explicit owner authorization flow and
   verify the active signer registry, expiry and revocation.
2. Select and test a pinned SDK migration in isolation. The installed 0.6 client
   is not the session-key adapter. Avoid convenience methods that deploy wallets
   or automatically repair allowances. Require exact request/hash binding,
   post-only submission, reviewed expiry and one HTTP attempt.
3. Implement a restricted signer service with managed secret storage. Complete
   server-side account/chain cash and allowance verification, account-wide
   reconciliation across sessions, authenticated order/trade pagination,
   settlement failure accounting and venue heartbeat handling. These are
   obligations of the currently unimplemented `Venue` protocol.
4. Connect verified, wallet-owned agent policies to authenticated start, pause,
   cancel and revocation flows. Do not mount financial actions behind the
   existing read-only `WalletAuth.authenticated_post` wrapper: it can revalidate
   after its handler. Financial commands need a durable operation ID and a
   truthful response even if the login expires after submission.
5. Enforce actual user eligibility and account restrictions. A public-data
   connection or the server's egress location does not prove eligibility.
6. Review the complete signing and deployment boundary, add operator metrics,
   retention and recovery procedures, then conduct a small explicitly
   authorized funded trial. Offline tests and observation-only streams do not
   validate real signing, settlement, cancellation or revocation.

Useful primary references: [session keys](https://docs.polymarket.com/trading/session-keys),
[placing orders](https://docs.polymarket.com/trading/place-orders),
[managing orders](https://docs.polymarket.com/trading/manage-orders),
[realtime order updates](https://docs.polymarket.com/trading/realtime-order-updates),
and [public market streaming](https://docs.polymarket.com/market-data/realtime-data).
