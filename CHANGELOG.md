# Changelog

## 0.8.0 — 2026-08-31

Launch-readiness pass. A fresh-eyes audit found the code was in better shape
than the packaging around it; this release fixes the packaging.

**Honesty**
- Added 47 offline tests (`tests/test_money_paths.py`) + CI on Python
  3.11/3.12/3.13. The README previously claimed the Kalshi order translation
  was unit-tested when no tests existed — the claim is now true.
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
