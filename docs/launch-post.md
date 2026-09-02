# Launch post

Three versions of the same announcement: the long-form post (for the site,
GitHub Discussions, or a blog), the Show HN submission, and a thread for X.
No em dashes anywhere. Every claim links to something a reader can verify.

---

## Long-form: "I built a Polymarket MCP server and the API taught me six things"

I wanted my agent to trade prediction markets, so I wrote an MCP server for
Polymarket and Kalshi. The venues' APIs are good. They also have habits that
will quietly cost you money if you let an agent talk to them raw. Here are
the six that bit me, each one reproducible without keys:

```
pip install oddsrail
python examples/footguns.py
```

**1. The order book arrives worst-first.** Polymarket's raw book lists bids
ascending and asks descending, so `bids[0]` is the worst level on the book.
Every model's prior says index zero is best. oddsrail normalises both venues
to best-first and walks the book to quote what a size actually costs.

**2. Kalshi has no ask side.** It publishes two bid ladders (YES and NO),
ascending, as dollar strings. A NO bid at 0.25 is a YES ask at 0.75. Get the
translation backwards and you have silently taken the opposite position.
oddsrail lets the agent say `outcome=no, action=buy` and does the arithmetic
in `Decimal`, tested exhaustively rather than by example.

**3. A geoblocked machine looks healthy until the order.** Restrictions are
enforced at order placement; every public read answers normally. So an
operator in a close-only jurisdiction sees a working server right up to the
first trade. oddsrail runs Polymarket's own geoblock preflight, reports the
verdict as advisory, and classifies a rejection as a jurisdiction block with
a "stop retrying, tell the operator" hint instead of a retry loop.

**4. That verdict is IP-based and takes unvalidated overrides.**
`?country=ZZ` returns `blocked: false`. "Not blocked" means "absent from a
list", not "permitted", and the terms bind on residence, not egress. So the
preflight never gates a trade; the venue's own rejection stays the arbiter.

**5. The obvious endpoint is deprecated.** Gamma's `/markets` answers with
`deprecation: true` and a sunset date months in the past. The official SDK
already reads `/markets/keyset`, so oddsrail goes through the SDK.

**6. A price gap is not an edge.** Naive title matching across venues pairs
unrelated markets and reports a 70-point "gap" that is fiction. oddsrail
requires title similarity of at least 0.5 and close dates within a week,
usually returns nothing (the honest answer), and even then a settlement audit
has to say "ok" before a difference counts as anything.

Three more need keys to show: the exchange models a rejected order as an
`ok:false` return value rather than an exception (report that carelessly and
an agent believes a rejected order is resting), there is an undocumented $1
minimum notional, and the SDK call that sounds like "my trades" returns the
market's public tape. All three are encoded in the trading tools.

**What it is.** 39 tools and 4 workflow prompts. Read tools need no keys.
Trading is non-custodial: you run the server, your keys sign on your machine,
dry-run is the default and dry-run orders are papered against the live book
so you can see simulated P&L. Operator guardrails (per-order and per-session
notional caps, allowed markets) are enforced before any request and the agent
cannot change them. Gasless split, merge and redeem go through Polymarket's
relayer with your own key; a live split and merge with Polygon hashes is in
the repo. MIT.

**How it pays for itself.** A public builder code registered at 0 bps is
signed into every Polymarket order, so the project earns a share of
Polymarket's weekly builder pool and adds nothing to your trade. Override it
with one environment variable if you would rather attribute to yourself.
The profile is a verified Polymarket builder and their builder team confirmed
this is the right pattern for a self-hosted tool.

**The honest numbers.** This week the code ranks around #124 of 216
builders, and all of that volume is my own market-making bot. There is a
public attribution ledger that lists every wallet carrying the code and
subtracts mine, so the "external users" number is real and, today, zero:
https://oddsrail.app/attribution. That page is also how you will know if any
of this worked.

Site: https://oddsrail.app · Source: https://github.com/hmesutozsoy/oddsrail
· Registry: `app.oddsrail/polymarket-kalshi-trading`

---

## Show HN

**Title:** Show HN: oddsrail, an open-source MCP server for Polymarket and Kalshi, built from the APIs' footguns

**Text:**

I wrote an MCP server so my agent could trade prediction markets, and the
venue APIs taught me six things the hard way: the order book arrives
worst-first, Kalshi has no ask side, a geoblocked machine looks healthy until
the first order, the geoblock verdict takes unvalidated overrides, the obvious
Gamma endpoint is deprecated, and naive cross-venue matching pairs unrelated
markets. `python examples/footguns.py` reproduces all six live without keys.

oddsrail is self-hosted and non-custodial (your keys sign on your machine),
dry-run by default with a paper ledger, and ships operator guardrails the
agent cannot change. It pays for itself with a 0 bps builder code you can
override. 39 tools, 4 prompts, 116 offline tests, MIT.

Honest numbers: rank ~#124 of 216 builders this week, all of it my own bot.
The attribution ledger at https://oddsrail.app/attribution subtracts my
wallet, so the external-user count is real and currently zero.

Repo: https://github.com/hmesutozsoy/oddsrail

---

## r/mcp

**Title:** oddsrail: MCP server for Polymarket + Kalshi, non-custodial, dry-run by default, with guardrails the agent can't change

39 tools / 4 prompts. Read tools need no keys. Trading tools are dry-run by
default and paper-trade against the live book so you get simulated P&L before
you ever set `ODDSRAIL_DRY_RUN=0`. Operator guardrails (per-order and
per-session notional caps, allowed markets) are enforced before any request.
Gasless split/merge/redeem through Polymarket's relayer with your own key.

The interesting part is what the venue APIs get wrong, and there is a keyless
script that shows all six live: `python examples/footguns.py`.

`pip install oddsrail` then `claude mcp add --transport stdio oddsrail -- oddsrail`.
Verified Polymarket builder, MIT, 116 tests. Honest usage numbers are on the
site's attribution ledger, and today the external count is zero.

https://github.com/hmesutozsoy/oddsrail

---

## X thread (6 posts)

1/ I wrote an MCP server so my agent could trade Polymarket and Kalshi. The
APIs taught me six things the hard way. All six reproduce live, no keys:
`pip install oddsrail && python examples/footguns.py`

2/ Polymarket's order book arrives worst-first. bids[0] is the WORST level.
Every model assumes index 0 is best. That alone will price you against the
wrong side of the book.

3/ Kalshi has no ask side. Two bid ladders, ascending, dollar strings. A NO
bid at 0.25 is a YES ask at 0.75. Get it backwards and you are on the
opposite side of the trade, silently.

4/ A geoblocked machine looks healthy until the order. Every public read
works; the block lands at order placement. And the geoblock endpoint takes
?country=ZZ and says "not blocked". Advisory, never a gate.

5/ oddsrail: non-custodial, dry-run by default with a paper ledger,
guardrails the agent cannot change, gasless split/merge/redeem with your own
relayer key. 0 bps builder code you can override. Verified Polymarket
builder. MIT.

6/ Honest numbers: rank ~#124 of 216 builders this week, all of it my own
bot. The ledger at oddsrail.app/attribution subtracts my wallet, so the
external-user count is real. Today it is zero. Repo:
github.com/hmesutozsoy/oddsrail
