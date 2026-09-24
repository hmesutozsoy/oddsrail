# Reference paper agent

A deliberately trivial "quote and settle" loop that runs hourly in paper mode
and publishes a journal. It is plumbing on record, not a strategy: it shows
that finding markets, reading the book best-first, costing a size, resting
quotes inside the operator's guardrails, getting filled when the market
crosses, and settling all work end to end through oddsrail, with numbers a
reader can check against the ledger file.

What one run does:

1. `paper.positions()`: settles any resting paper quote the market crossed
   since the last run, and marks positions at the current mid.
2. Cancels paper quotes older than 24 hours.
3. Sells any position whose mid is at least 0.02 above its average cost.
4. Picks up to 5 open, liquid, mid-priced Polymarket markets (spread at most
   0.04, YES bid between 0.10 and 0.90, ending more than two days out) and
   rests a 5-share maker bid at the best bid on each one it does not already
   hold or quote. `ODDSRAIL_MAX_ORDER_NOTIONAL` applies; refusals are logged.
5. Appends one JSON line to `journal.jsonl` and rewrites `journal.md`.

Honesty notes, the same ones the paper ledger carries: fills are simulated
against the live book at call time with no queue position, no market impact
and no fees, so the journal is an upper bound on what the same orders would
have done live. The strategy is not a recommendation; it was chosen because
it exercises every tool in the loop.

## Run it

```bash
pip install oddsrail
export ODDSRAIL_PAPER_JOURNAL=./journal ODDSRAIL_MAX_ORDER_NOTIONAL=20
python examples/paper_agent/agent.py
```

The `deploy/` directory has a systemd service and hourly timer; the
maintainer runs it on a small VPS and copies the journal into the repo at
intervals.
