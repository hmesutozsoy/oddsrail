# Changelog

## Unreleased

- Claude Code plugin (`.claude-plugin/plugin.json`, marketplace manifest,
  `.mcp.json`) and four skills generated from the MCP prompts
  (`skills/*/SKILL.md`, `scripts/gen_skills.py`, drift test). One-click
  install links for Cursor and VS Code; `npx skills add` support.
- `examples/paper_agent`: reference quote-and-settle agent in paper mode
  with a public journal; running hourly on the maintainer's VPS since
  2026-09-03.
- `examples/footguns.py`: six venue-API footguns reproduced live, no keys.

- Live proof of the relayer path: a 1 USDC split and merge through the
  relayer with the operator's own key, recorded with relayer ids and Polygon
  hashes in `docs/live-proof.md`. Redeem remains unproven until the test
  account holds a resolved position.
- `dump()` now converts SDK dataclasses (the relayer `TransactionOutcome`)
  to plain fields instead of a repr string.

## 0.10.1 (2026-09-02)

Credibility release, ahead of the announcement.

- **Attribution ledger**: `attribution_ledger` tool and the public page at
  oddsrail.app/attribution. Every trade carrying the code, per Sunday-start
  week and per wallet, with the maintainer's own wallets subtracted into an
  "external" line. The page and the tool compute the same thing from the same
  public feed so either can be checked against the other.
- **Competitor table rewritten** against the products that actually compete
  today (pmxt, Simmer, Polymarket's agent-skills), verified from their own
  docs and dated. Crosswire and Parsec are gone from the table.
- **Registry rename**: `app.oddsrail/polymarket-kalshi-trading`. The README
  says `compare_venues` is not an arbitrage scanner, so the name should not
  say arbitrage either. The old name's versions are deprecated, not deleted.
- `ODDSRAIL_MAINTAINER_WALLETS` for operators running the ledger against
  their own code.

## 0.10.0 (2026-09-02)

The operator-safety and rehearsal release: the things anyone handing keys to
an agent asks for before anything else.

**New**
- **Guardrails** (`oddsrail/guard.py`): `ODDSRAIL_MAX_ORDER_NOTIONAL`,
  `ODDSRAIL_MAX_SESSION_NOTIONAL`, `ODDSRAIL_MAX_OPEN_ORDERS`,
  `ODDSRAIL_ALLOWED_MARKETS`. Enforced before any request on both venues, in
  dry-run too; refusals are structured and name the rule, limit and request.
  `server_info` reports them.
- **Paper trading** (`oddsrail/paper.py`): dry-run Polymarket orders fill
  against the live book within the limit, rest otherwise, and fill when
  crossed. `paper_positions` (cash, marks, realized/unrealized P&L),
  `paper_reset`. Honest caveat in the README: no queue, no impact, no fees.
- **`watch_book`**: bounded realtime streaming of one token's market events
  via the SDK websocket.
- **`redeemable_positions`** and the **`settle_resolved`** prompt: what the
  wallet can redeem or merge now, chained into the gasless tools.
- Failure class `local_tls` with a fix-it hint: a missing CA bundle (python.org
  macOS installs) used to be misreported as "unreachable".

**Notes**
- 39 tools (30 read-only / 9 destructive), 4 prompts, 111 offline tests.
- Kalshi dry-run orders are not papered yet.

## 0.9.0 (2026-09-02)

Gasless position management. Three new trading tools go through Polymarket's
relayer with the operator's own Relayer API key, the self-hosted pattern
Polymarket's builder team recommended when the oddsrail profile was verified.

**New**
- `split_position` (USDC → full YES+NO set), `merge_positions` (YES+NO →
  USDC, or `max`), `redeem_positions` (resolved winners → USDC). Dry-run by
  default; return the relayer transaction id/hash and the terminal outcome.
- `POLYMARKET_RELAYER_API_KEY` + `POLYMARKET_RELAYER_API_KEY_ADDRESS`. Both
  halves required. Without them the tools return a structured "not
  configured" answer and send nothing, there is deliberately no fallback to a
  gas-paying EOA broadcast.
- `server_info` reports `relayer_key_configured` and lists the gasless tools.

**Notes**
- 35 tools (27 read-only / 8 destructive). 17 new offline tests (98 total)
  pin dry-run, the not-configured guard, USDC base-unit conversion, and input
  validation.
- The relayer path is dry-run and validation tested only; no live
  split/merge/redeem has been sent from this code yet (README says so).
- Builder profile Verified in Polymarket's program (2026-09-02); attribution
  pattern for self-hosted tools confirmed by their builder team.

## 0.8.1 (2026-09-01)

Jurisdiction awareness. The geography story was wrong in shape: the docs
framed geoblocking as a connectivity problem, when for most restricted
jurisdictions Polymarket's restriction is enforced at ORDER PLACEMENT, reads
answer normally and the failure arrives at the trade. Kalshi restricts a
heavily overlapping country list, so it is not a general fallback either.

**Docs**
- README "Network note" replaced by "Where this works": Polymarket's three
  restriction tiers and Kalshi's Member Agreement §VI, with as-of dates and
  the authority URLs, plus the polymarket.us distinction (not supported) and
  the network-filter case. Same story in llms.txt. Removed the advice to "run
  oddsrail somewhere the venue is reachable".
- The untested-Kalshi-order caveat now states its real reason (no funded
  account) instead of letting readers infer a geographic one.

**New**
- `oddsrail/geo.py`: failure classifier (geo_blocked / geo_suspected /
  unreachable / intercepted), agent-facing hints, an ADVISORY Polymarket
  geoblock preflight (documented endpoint; never gates a trade), and per-host
  reachability probes. Caches expire after 5 minutes and results carry
  `checked_at`.
- `server_info` now reports geography: the preflight verdict, per-host
  reachability, and an explicit disclaimer that an IP verdict is not a
  compliance check. Kalshi publishes no equivalent endpoint; server_info says
  so rather than leaving anyone hunting.

**Fixes**
- `_err` recovers the HTTP status from Polymarket-SDK-shaped exceptions
  (`.status`/`.code`), which it previously dropped; the URL is reported for
  connection-level errors too, and error handling can no longer itself raise
  on httpx exceptions with an unset `.request`.
- ~20 tools that previously surfaced a bare "Error executing tool X" under
  network failure now return structured errors with a failure class and hint.
- `find_markets` no longer reports a total venue outage as an empty market
  list; it distinguishes outage / partial / genuinely-empty.
- `trading.py`: the client handshake moved inside the try on every
  non-idempotent order tool (a block at auth previously produced a bare MCP
  error on a tool where blind retry can double a position); an order response
  the SDK version does not recognise now reads as NOT-confirmed rather than
  accepted; timeout wording is honest ("the order MAY still have posted").
- Kalshi request paths (including cancel) raise a named error when an ISP
  interstitial answers 200-with-HTML instead of surfacing
  "Expecting value: line 1 column 1".
- Geo classes are venue-scoped: a Kalshi 403 keeps its credentials hint
  instead of inheriting Polymarket's jurisdiction verdict, and the SDK's
  TransportError is classified by its CAUSE so a slow venue (ReadTimeout) is
  never labelled "unreachable", nor does any hint claim "nothing was sent"
  on a possibly-post-send failure.
- 34 new offline tests (81 total) covering the classifier, the verdict tiers,
  `_err` status recovery, order-response interpretation, and the
  find_markets outage/partial notes.

## 0.8.0 (2026-08-31)

Launch-readiness pass. A fresh-eyes audit found the code was in better shape
than the packaging around it; this release fixes the packaging.

**Honesty**
- Added 47 offline tests (`tests/test_money_paths.py`) + CI on Python
  3.11/3.12/3.13. The README previously claimed the Kalshi order translation
  was unit-tested when no tests existed, the claim is now true.
- Replaced the builder-leaderboard figures, which were 2–7x stale against the
  server's own `builder_stats` output, and stamped them with a pull date.
- Rewrote the "Status" section, which described a 10-tool project three
  releases out of date, and now states plainly that the Kalshi *order* path
  has never been exercised against a real account.

**First run**
- `oddsrail --help` / `--version` print something. They previously produced
  zero output and exit 0, which reads as a broken package.
- `serverInfo` reports a version instead of an empty string.
- Read tools return `{error, error_type, http_status, url, hint}` instead of a
  bare "Error executing tool X" an agent cannot act on.

**Fixes**
- Kalshi search returned *closed* markets as top hits: an open event can
  contain closed markets, and only the event was being filtered.
- `find_markets` / `closing_soon` silently returned empty for an unrecognised
  `venues` value; they now say what the valid values are.
- `builder_stats` presented the bundled default code's trades to every user
  under `my_trades`. They are now labelled `bundled_default_code_trades` with
  a note, unless the operator has set their own code.
- Documented that only `0`, `false` and `no` disable dry-run; everything else,
  including typos, stays safe.

**Repo hygiene**
- Removed committed build artifacts and a private outreach folder; moved the
  maintainer runbook to `docs/maintainers/`.
- `requirements.txt` was missing `cryptography`, which Kalshi signing needs.

## 0.7.0
Workflow prompts (`find_fade_setup`, `check_cross_venue_edge`,
`daily_review`), live `settlement_audit`, fractional-Kelly `position_size`.

## 0.6.0
Order lifecycle and discovery: `order_status`, `my_fills`, `my_positions`,
`cancel_all_orders`, `resolution_criteria`, `closing_soon`.

## 0.5.0
Cross-venue layer: `find_markets`, `quote_cost`, `compare_venues`.

## 0.4.0
Fixed four agent-facing defects: `cancel_order` was unusable in live mode,
rejections were reported as successes, `order_type` was silently ignored, and
the orderbook was returned worst-first.

## 0.3.0
Kalshi as second venue.
