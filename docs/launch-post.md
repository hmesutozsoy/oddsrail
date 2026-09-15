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

**What it is.** 42 tools and 4 workflow prompts. Read tools need no keys.
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

**The honest numbers.** All of the volume carrying the code so far is my
own market-making bot, so the count of outside users is zero. You do not
have to take my word for either figure: Polymarket publishes every builder's
volume and active users on its own feed, at
data-api.polymarket.com/v1/builders/leaderboard, and that is where to look to
see whether any of this worked.

Site: https://oddsrail.app · Source: https://github.com/hmesutozsoy/oddsrail
· Registry: `app.oddsrail/polymarket-kalshi-trading`

---

## Show HN (September 2026 version: markets, the builder and the connector)

**Title (80 characters max):**

Show HN: OddsRail – an open-source rail for AI agents that trade Polymarket

**URL:** https://oddsrail.app

**First comment, posted right after submitting:**

Author here. OddsRail is an open-source MCP server that lets an AI agent
read, cost, check and trade Polymarket and Kalshi (pip install oddsrail, MIT,
42 tools). There are three ways in, depending on how much you want to commit.

Browse and design, no account: the site lists live Polymarket markets, and
the builder walks you through a strategy (fade overshoots, near-certain
resolutions, following a move, your own probabilities) with risk rules like
stop losses and loss limits. The draft is saved in your browser. Nothing
trades from the website yet, and the page says so.

Trade by chat, no install: add https://mcp.oddsrail.app/mcp to Claude as a
custom connector and sign in with an email. Your agent gets the tools and a
paper ledger that fills against the live order book, so you can see what a
strategy would have done without risking anything.

Live, under your own key: pip install oddsrail. Orders are dry-run until you
say otherwise, every order goes through a deterministic pre-trade check first
(right market, right side, sane price, the $1 minimum, your guardrails), and
your key never leaves your machine.

What it is not: a backtest, or a promise that a strategy makes money. Paper
fills are an upper bound because there is no queue or market impact, and the
two-sided quoting strategy carries the warning that my own market-making
engine lost about 4.5 cents a share on markout live after looking fine on
paper.

Why it exists: Polymarket pays verified builders a share of a weekly pool by
attributed volume. I am one, and the server attaches my builder code at
0 bps, so it adds nothing to anyone's trade. Code: https://github.com/hmesutozsoy/oddsrail. Happy to answer anything
about the venue APIs; there are six notes on what they get wrong at
https://oddsrail.app/notes/.

**Rules of the day:** never ask anyone for votes, reply to every comment
for the first three hours, and post Tuesday to Thursday between 14:00 and
16:00 UTC.

## r/mcp

**Title:** oddsrail: MCP server for Polymarket + Kalshi, non-custodial, dry-run by default, with guardrails the agent can't change

42 tools / 4 prompts. Read tools need no keys. Trading tools are dry-run by
default and paper-trade against the live book so you get simulated P&L before
you ever set `ODDSRAIL_DRY_RUN=0`. Operator guardrails (per-order and
per-session notional caps, allowed markets) are enforced before any request.
Gasless split/merge/redeem through Polymarket's relayer with your own key.

The interesting part is what the venue APIs get wrong, and there is a keyless
script that shows all six live: `python examples/footguns.py`.

`pip install oddsrail` then `claude mcp add --transport stdio oddsrail -- oddsrail`.
Verified Polymarket builder, MIT, well over a thousand tests. Honest usage:
every attributed trade so far is my own bot, so outside users are zero, and
Polymarket's own public builder feed will confirm that.

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

6/ Honest numbers: every attributed trade so far is my own bot, so outside
users are zero. Polymarket publishes builder volume on its own public feed,
so you can check that rather than trust me. Six notes on the API traps:
oddsrail.app/notes. Repo: github.com/hmesutozsoy/oddsrail
