# Changelog

## 0.20.0rc1 (2026-10-07)

- Upgrade the reviewed Polymarket SDK to 0.12.0 with an exact hashed dependency lock.
- Select outcome IDs by market version and resolve V2 position IDs without falling back to CTF token IDs. Missing or unsupported versions expose no trading IDs.
- Refuse local Protocol V2 live orders and position transactions before signing while funded validation remains pending. Existing CTF execution and dry-run defaults remain supported. This candidate is not a declaration of end-to-end V2 live readiness.
- Include Data API v2 migration and existing execution-safety fixes in the local MCP candidate.

## 0.19.0 (2026-09-25)

- Perps bots spend less of the shared request budget on history. A running
  grid, market maker or DCA, and a Scale with a loss stop, used to recount its
  realized result from fill and funding history every 30 seconds, each from
  its own market: about 80 of the 214 tokens a minute measured for two idle
  bots and an open Perps page. It now recounts when one of its orders fills,
  when its position size changes, while a failed recount is retried (every 30
  seconds) and before its first count, and otherwise at each five-minute UTC
  boundary, when an owner with two or more bots that recount there has its
  fills and funding read once in every market and each bot keeps its own
  market's rows. The venue lists a fill a moment after the position changes,
  so after a fill or a size change the bot also recounts its own market every
  30 seconds for two minutes, and a loss stop or profit target sees that
  fill's result as soon as before. Loss stops and profit targets use the last
  realized result plus the live unrealized one, so funding, and a trade in the
  bot's market that leaves its position size as it was without one of its
  orders filling (a round trip by hand), can be up to five minutes late in the
  realized result. In a ten-minute test with two idle bots, history reads fell
  from 840 tokens (84 a minute) to 120.
- The admin page shows where the Perps request budget goes. Every Perps
  request is logged with where it came from (each bot kind, the bots' shared
  account, price and history reads, the Perps page, owner commands, the
  leaderboard) and its method and path without ids. Under the budget meter, a
  table gives each source's tokens a minute and share over the last 10
  minutes, and a second one the eight busiest requests; both scroll inside
  their card on a phone.
- The admin page has a Daily activity section: the last 7 days in four
  tiles and a table with one row per UTC day for 30 or 90 days (visitors,
  wallets signed in, new wallets, Perps authorizations, bots created,
  session keys, MCP registrations and paper runs). Wallets that sign in are
  counted once a day against a hash held in memory under a key that is never
  saved and changes every day, so nothing saved can be matched to an
  address, and the count is written in the background so it never slows a
  sign-in. The day counting starts is marked as incomplete, new wallets are
  the wallets added to the leaderboard, and the bots tile counts each person
  who created bots once. The admin wallets are left out. Writes to the admin
  database now give up after 50 ms when another process holds it, instead of
  holding up the server for 5 seconds.
- Last review round before release. A paused TWAP, Scale or Chase keeps its
  stop loss (a fill just before the pause is guarded while paused), and the
  exits keeper never guards a position the owner flipped by hand. An order
  the venue accepts without a usable order id is found by client id instead
  of sent again (TWAP slices, grid starting orders, DCA base orders, limit
  entries, resting orders). Cancelling a bot's orders also cancels the ones
  it tracks that a lagging open-orders read left out, and delete or close
  waits a few seconds after an order was sent. A limit entry that filled in
  part is never entered a second time; if it leaves the book, the filled
  part is the position and gets its exits.
- A TWAP whose slices would round under the market minimum takes one slice
  fewer instead of refusing: the ticket's default $40 run at BTC 83,000 goes
  in three slices of about $13 (four $10 slices round to $9.99).
- Three order types for Perps, worked on the server the way Hyperliquid's
  are and chosen under Pro in the ticket. TWAP sends a size in equal
  immediate-or-cancel slices over a running time (5 minutes to 7 days, 30
  minutes to start): as many slices as the size allows at the market
  minimum, never closer than 30 seconds, catch-up slices of up to three
  normal ones, Randomize, a trigger price that starts the run and a max or
  min price that ends it. Scale rests a ladder of post-only limits spread
  evenly between a start and an end price (2 to 20 orders; a size skew
  from 0.25 to 4, the ratio of the end order's size to the start order's;
  an optional loss stop), moves the optional exits to the average as rungs
  fill, and leaves out a rung the venue refuses as crossed. Chase rests one
  post-only limit one tick inside the best bid or ask and re-prices it
  every tick as the book moves, so it fills as a maker; once the price has
  moved the max chase distance (0.5% to start) it stops following and rests
  as a plain limit, and at the end of its running time (1 hour to start)
  the order is cancelled. All three take the Buy / Long | Sell / Short
  control, capital and leverage, a take profit and stop loss as percentages
  from the average fill (2% and 1% to start), pump protection (TWAP and
  Scale, opening runs only), and Reduce only, which closes the whole
  position on the market instead of opening one, needs no free margin, is
  never held by pump protection, settles as soon as its closing fills are
  listed, and is refused while another bot manages that market. An
  opening run is refused with `position_exists` when the market already
  holds a position on either side, because the exits are placed for the
  whole position. A finished run is
  watched until the position is flat, like a directional bot's, and its
  progress shows on the bot card and in Bot history. The engine reads the
  best bid and ask once a tick from the venue's `bbo` route. docs/perps.md
  has each one's settings, tick, settlement and limits.
- Rules the three order types keep, from the review before release. An
  order that left the book is asked about by its client id up to three
  times before it counts as unfilled (`order_lookup_failed`), so a
  throttled look-up never reports a fill as gone. A command the venue may
  have taken (a timeout, an outage, a 200 the engine could not read) keeps
  its intent and is reconciled after the grace period; only a refusal
  drops it. Delete refuses while a TWAP slice is in flight and pulls a
  ladder's or chase's orders. A paused TWAP, Scale or Chase gets
  the paused time back on resume: the whole clock shifts, so the run
  continues at pace and ends that much later. A Chase's clock starts on
  the first tick, its max distance is measured against the run's own
  starting price (a fresh one only after a resume), and post-only
  refusals do not count toward the five that end a chase. TWAP slices are
  never priced past the Max or Min price, and Randomize stays within the
  three-slice cap. A Scale rung pulled after a partial fill keeps its
  remainder, and `ladder_done` is logged only when every rung filled
  (`scale_stopped` otherwise).
- More rules for the three order types, from the second review. An order
  stays tracked until the venue says it is gone: a cancel the venue
  refuses keeps the order tracked (`cancel_refused`) and is asked again,
  and a finished TWAP, Scale or Chase cancels anything of its own still
  resting before it settles, so no order outlives a run. An order the
  venue still reports open with nothing filled (a batch it admitted after
  a timeout, an order one open-orders read left out) is adopted again
  under its order id rather than placed a second time; a grid and a DCA
  bot share that plumbing and keep the same two rules. A percentage take
  profit or stop loss that a finished run should hold and does not is
  looked for on the venue and otherwise placed again, every 30 seconds
  while the position is watched (`exit_restored`). An opening run refused
  with `position_exists` on its first tick never changes the market's
  leverage: the position is read before the leverage is touched. A TWAP
  claims a position it counted no fill for only when it is on the run's
  own side and no larger than its target; anything else was opened by hand
  and is left alone. A reduce-only TWAP run whose position was flipped by
  hand ends closed at once and leaves the hand position alone. A
  reduce-only Chase closes a residue under the market minimum with one
  reduce-only taker order at the touch (`chase_residue`), since the venue
  may refuse to rest it as a limit. The average is marked as estimated
  (`average_estimated`) when the venue did not report the fill price and
  the limit that was sent stands in; the bot card and Bot history then
  read "about" before the figure and say why on hover.
- More rules for the three order types and the engine they share, from the
  third review. The engine reads a bot again under its lock before each
  step, and Pause, Resume, Close position and Delete read it inside the same
  lock, so a pause, a close or a delete is never undone by a step that
  started from older facts. One exits keeper holds the percentage take
  profit and stop loss of a TWAP, Scale or Chase from the first fill on, not
  only once the run is done: the stop loss first, moved as later fills move
  the average, looked for on the venue before anything is sent
  (`exit_restored` for one found or placed, `exit_moved`, `exit_missing` for
  one the venue refused), each exit on its own so a refusal of one never
  stops the other, and at most every 30 seconds unless a fill just landed.
  An exit whose level the price has already passed cannot rest, so the
  position is closed at market in its place (`exit_passed`) and the bot ends
  with the reason `stop_passed` or `target_passed`. Exits the owner sets or
  clears by hand in the position panel are left alone: the bot neither moves
  them with its average nor places one back. A bot that is closing and still
  holds a position sends its reduce-only close again, after eight seconds
  and then every 30 (`close_retried`), and a loss stop whose close timed out
  is picked up the same way. A finished run does not settle while an order
  of its own still rests: the closing sweep repeats on every tick, up to 20
  times, before the run settles over it (`sweep_incomplete`). Pause, Delete
  and a Close position that finds no position answer `cancel_failed` when the
  venue refuses a cancel and leave the bot in the state it had; a TWAP rests
  nothing, so its pause pulls nothing. Delete of a TWAP, Scale, Chase, market
  maker, grid or DCA bot reads the position after its cancel and answers
  `bot_active` when an order filled since the last check, and its `bot_busy`
  refusal for an order in flight now applies to a running TWAP only. A
  leverage change the venue refuses five times fails the bot with
  `leverage_rejected`, for a market maker, a grid and a DCA bot as well. An
  order the venue accepted without naming an order id stays tracked and is
  found by its client id; an order the venue still lists as open or pending
  is adopted again whatever it has filled, and its fill is counted when it
  leaves the book; a look-up that was throttled or timed out holds the bot's
  other look-ups until the next tick. A Chase never outbids its own order:
  while its order is the best bid or ask it stays where it is. A reduce-only
  TWAP lifts a slice that closes only part of the position to the market
  minimum, never past what the position still holds; only the exact
  remainder may go under it, like a close.
- The Perps ticket takes Hyperliquid's shape: Market | Limit | Pro, where
  Pro is a select with the stop entries, the webhook signal, TWAP, Scale,
  Chase, Market make, Grid, DCA and Channel, in place of the five-tab strip
  and the "Set up a bot" heading. A chosen Pro option shows its name in the
  tab and the address bar carries it (`?mode=` for a kind, `?pro=` for a
  stop entry or the signal), so every ticket can be linked to. One Buy /
  Long | Sell / Short control serves directional, TWAP, Scale, Chase and
  DCA orders (DCA's own radio pair is gone); market making, grid and
  channel keep their own direction controls. An Advanced row under the
  exit tools shows the stop trigger, pump protection and each order type's
  extra settings (TWAP trigger and limit price, Chase distance and end
  time, Scale loss stop, market-making requote); off, they are hidden and
  left out of the order, and the browser remembers the choice.
- Perps ticket defaults, with the reasoning in the tooltips. Capital $20
  (was $6), which at 2x clears the $10 market minimum with room for
  rounding for a market, limit, stop or signal entry, a TWAP, a Chase and
  a market-making pair; Scale, Grid and DCA need the minimum for every
  order, so the ticket lifts the capital to the least that fits when one
  is chosen (about $28 for a five-order Scale, $33 for a six-rung grid,
  $45 for a DCA with three safety orders) and the server refuses less.
  Suggested exits when a switch is turned on: a take profit 2% from the
  mark against a stop loss 1% from it, a 2 : 1 reward to risk (was 1.5%
  and 1%); a TWAP, Scale or Chase starts at the same 2% and 1% as
  percentages from the average fill; signal bots keep 1% and 0.5%. The grid and
  market-making loss stop is blank and means 30% of capital, rounded to
  the nearest half dollar and at least $0.50, filled the same on the page
  and the server: room for the ladder to breathe, and a stop well before
  the margin is gone. DCA starts with a 1.5% price step (was 1%), a 1.2
  step multiplier (was 1) and a 1.5% take profit (was 1%), so the first
  safety order sits clear of ordinary noise, the later ones spread wider
  and the take profit clears the taker fee on every leg; the server's
  fallbacks for a create request that leaves those keys out are the same,
  so its ladder is deeper and a stop loss must sit below it (docs/perps.md
  says how). TWAP: 30 minutes,
  Randomize and Reduce only off, no trigger or limit price. Scale: 5
  orders, skew 1.00, a range 0.5% to 2% under the mark for a buy (mirrored
  for a sell), loss stop blank. Chase: max distance 0.5% on, ends after one
  hour. Leverage (2x), the trailing stop, stop trigger and pump protection
  (1%, 1%, 3%), the grid (6 rungs, 3% either side), market making (20 bps,
  a 2x cap, a quarter of the spread) and the channel settings are
  unchanged.
- Perps page controls in Hyperliquid's proportions with OddsRail's own
  accent: 8px corners on the arm button, inputs, selects and the segmented
  tracks, 5px on the pressed side pill, 32px tall inputs, side buttons and
  arm button, no hover lift inside the ticket, and Market | Limit | Pro as
  underline tabs. The purple accent and the green and red sides stay.
- A bot's result waits for the closing fill. The venue lists a fill a
  moment after the position is gone, and a result counted at once missed
  it (one channel trade showed -$0.07 for a -$2.22 stop). The engine now
  counts a result once the fills since the entry net to zero, trying again
  each check for up to two minutes, then counts what is listed and logs a
  `settle_incomplete` event. A reduce-only TWAP, Scale or Chase settles as
  soon as its closing fills are listed, without that wait.
- The Perps page is denser, in the way of the exchanges people already
  use: 13px text, 12px tables, tighter panels, a 60px header, and the
  ticket and tables fit without sideways scrolling from 1000px up (a
  ticket input could push the page wider). Market names in every account
  table open that market. Bot history shows Result, Fees and Funding as
  their own columns and names channel bots (it printed "undefined"). The
  "How the bots trade" text and the pilot note left the page; the risk
  line is a hover on the Perps account heading and the docs keep the rest.
- The Perps chart looks like Hyperliquid's: fills are B and S dots at their
  own time and price (hover for size, price, profit and fee), the position
  line carries a profit and size tag, and the exchange-held TP and SL show
  whenever there is a position. The bin also hides the bots' lines on that
  market; a bot's Show on chart brings them back. The stop loss is on by
  default in the Directional ticket.
- Channel orders follow their lines in finer steps (a twentieth of a narrow
  channel, at least 0.005%), and the ticket says how often they move. The
  Perps chart opens on 15-minute candles.
- The leaderboard marks your own row "(your wallet)" when your wallet is
  connected; the page compares the shortened addresses itself and sends
  nothing.
- The leaderboard ranks every row by the chosen figure, best first,
  including the market-making wallet.
- Channel bots end: an "Ends after" setting (1 hour to 1 week, 1 day by
  default, or never) stops new trades after that time, and the lines on
  the chart stop there instead of running off the edge.
- Channel bots can rest post-only limit orders on the lines instead of
  waiting for a touch (on by default in the ticket). The limits follow the
  lines, the nearer line always has one and the other one does when the
  account has margin for both, and the take profit rests as a limit on the
  other line; limits pay the maker fee and fill on a quick wick. A fill
  pulls what is left and takes its exits within one check; a price that
  gaps through a line still trades at market; pause, delete and breakouts
  pull the limits.
- The leaderboard lists every wallet that signs in to oddsrail.app, with no
  joining and no names: each row is a shortened address (the first and last
  four characters), the board data carries no full address, and no row links
  to a Polymarket profile. Perps figures now come from Polymarket Perps'
  public per-address data (profit, fills, account value), so no Perps
  authorization is needed. The join, entry and leave routes are gone, the
  earlier entries carried over without their names, and the privacy page
  says so. docs/leaderboard.md has the details and how to remove an address
  on request.
- Wallet sign-in says why it failed: a request already waiting in the
  wallet, an account the wallet has not allowed, a wallet that cannot sign,
  or the wallet's own error text, instead of one generic message.
- The leaderboard drops its Paper tab; the paper arena keeps its own board
  for agents at /arena/paper.json.
- The leaderboard's reference market-making row shows a shortened address
  (0x69cd…d48d) instead of a name, has no link to a Polymarket profile, and
  the board data no longer carries its full address.
- Channel bots for Perps: draw a channel on the chart (two points for one
  line, a third for the parallel line) and the bot sells when the mark
  touches the upper line and buys at the lower line, with an exchange-held
  take profit toward the other line and a stop just past the touched line.
  The lines extend through time and the exits move with them while a trade
  is open. After a take profit it waits for the next touch; a stop, or the
  price leaving the channel by the stop distance, ends the bot. Buy-only
  and sell-only channels, a take profit short of the other line, and pump
  protection are options. A drawn channel can be adjusted by dragging a
  line or an end point, and the ticket offers Redraw and Remove. The
  narrowest channel allowed is set by fees: the take profit must sit at
  least 0.12% from the entry line. Nothing rests on the book: the bot checks
  the price against where each line is at that moment, and the ticket shows
  each line's price now and an hour ahead. Details and limits are in
  docs/perps.md.
- The Perps bot types sit in one compact row, and chart drawings made on
  one candle interval now stay in place on another.
- Perps exit tools, one switch each in the ticket: take profit, stop loss,
  trailing stop, a stop trigger that starts the trailing stop only after a
  set gain, a trailing take profit for DCA bots, a profit target for grid
  and market-making bots, and pump protection for every type, which holds
  new orders that add to the position while the price swings more than a
  set percentage within five minutes. Details are in docs/perps.md.
- Perps abuse limits: six new bots per owner every ten minutes and thirty
  a day (removed ones count), ten creation tries a minute per address, a
  new bot must be covered by the account's available margin, service caps
  of 60 active bots and 25 owners, and a webhook limit per address.
- Dragging a grid's upper or lower line on the Perps chart no longer runs
  away from the cursor: the price scale holds still while a line moves and
  re-fits when it is released.
- The leaderboard drops its biggest-wins panel, and the server no longer
  reads closed positions for it.
- The scheduled smoke test prints the site's response headers when a page
  is not a 200 and treats a Cloudflare bot challenge on the GitHub runner
  as a warning instead of a failure.
- A private admin panel at oddsrail.app/admin for the site owner:
  warnings that need attention, traffic (visitors and page views today, 7
  and 30 days, a daily chart, top pages and referrers), refused requests by
  kind with their sources, product counts (sign-ins, paper accounts and
  agents, hosted agents, session keys, Perps authorizations and bots,
  leaderboard entries), OddsRail's volume and rank on Polymarket's builder
  leaderboard, PyPI downloads, GitHub stars, and server health (disk,
  memory, load, certificates, the Perps feed, engine and request budget).
  Only wallets in `ODDSRAIL_ADMIN_WALLETS` can read it; everyone else gets a
  404. Pages now send one first-party page-view beacon with no cookie and no
  identifier; unique visitors are counted per day with a salted hash whose
  salt is deleted daily. The privacy policy says so. See docs/admin.md.
- A real leaderboard at oddsrail.app/arena, in the shape of Polymarket's,
  replaces the "coming soon" page. Tabs for Predictions, Perps and Paper;
  Today, Weekly, Monthly and All periods; sorting by profit or volume
  (account value for Perps, return and fills for Paper); search by name;
  pages of 20; and medals for the top three. Owners join from the page with a display name
  and choose which records to show. Prediction figures are Polymarket's
  own per wallet and period, for the trading wallets the owner controls,
  verified on Polygon at join time. Perps figures come from the account's
  fills and funding, stored once and read incrementally. The house market maker is
  shown for reference and never ranked. New routes: `/leaderboard/me`,
  `/leaderboard/join`, `/leaderboard/leave` and `/leaderboard/board.json`.
- Repository split. The MCP server stays open source at
  github.com/hmesutozsoy/oddsrail. The hosted service, the website and the
  live and Perps engines moved to a private repository, and `oddsrail.cloud`,
  `oddsrail.live`, `oddsrail.perps` and `oddsrail.market_selection` no longer
  ship in the published package. Setting `ODDSRAIL_HOSTED=1` without the
  hosted service now fails with a clear message instead of an import error.
- Perps market making. A second bot kind rests a post-only bid and ask
  around the mark, sized as half of capital times leverage per side, with a
  requote distance, a position cap (only the reducing side rests at the
  cap), a loss limit that pulls both quotes and closes at market, and
  orphan cleanup after an interrupted tick. The page gets a bot-type switch,
  bid and ask lines on the chart, and bot cards with a live profit figure,
  entry and mark, exits with their dollar estimates, a stop-to-target range
  bar, and the venue's own words when a command is refused.
- Perps service on a request budget. Polymarket allows 1,000 weighted
  request tokens per minute per IP address, and the service was spending
  most of them on one user: an unfiltered open-orders read (20 tokens)
  plus order, fill and funding history on every account refresh, and an
  HTTP tickers read on every three-second engine tick. Now the engine
  takes tickers from the Perps WebSocket (`tickers::all`, which costs no
  tokens) and reads them over HTTP only while the socket is stale; the
  account route reads open orders per market with a position or a live
  bot (1 token each) and runs the unfiltered sweep once a minute; history
  is cached for 90 seconds; the engine ticks every five seconds; the page
  polls every ten. The status route reports the feed's state. Rough
  effect: from one or two concurrent authorized users per IP to about
  fifteen, or about thirty unattended bots.
- Portfolio shows a Perps card with the Perps account value next to
  Cash, Holdings and Unrealized P&L when the wallet has authorized Perps
  trading. The Create agent button leaves the header: it sits above the
  refresh control on Markets, next to Explore markets on Portfolio, and
  stays on the leaderboard. The Markets and Portfolio page descriptions
  are gone.
- The header shows a Perps figure next to Balance for a wallet that has
  authorized Perps trading: the Perps account value (collateral plus open
  positions, which lives on the Perps exchange apart from the
  prediction-market balance), refreshed every 30 seconds and linking to
  the Perps page.
- Perps Bots tab shows active bots only: a bot that closes, is cancelled
  or stops leaves the tab on its own and stays in Bot history. "Show
  inactive bots" brings the finished ones back as cards with their
  results, and "Set up again" on any bot card or Bot history row copies
  that bot's market and settings into the ticket (kind, side, entry type
  and price, leverage, capital, exits, trailing stop, grid, DCA and quoting
  parameters) for review before placing a new order.
- Perps market picker in the shape of Hyperliquid's: the market name in
  the top bar opens a panel with a search box, category tabs (all,
  favorites, crypto, index, equity, commodity), and a sortable table of
  every market with last price, 24h change, funding, 24h volume and open
  interest. Arrow keys and Enter pick a market, Esc closes, Cmd K or
  Ctrl K opens it, stars mark favorites (kept in the browser), and the
  chosen market goes into the page address. Open interest sorts the list
  by default because it comes with the tickers; the 24h figures load only
  for the rows in view, two at a time with a pause on a rate limit, and are
  cached in the browser for five minutes.
- Perps page: a wallet outside the pilot saw "Cannot access 'chip' before
  initialization" instead of its account state; fixed. The message for
  such a wallet now says the wallet is not on the pilot list and links to
  Support, and the deposit hint says to activate Perps on Polymarket. The
  footer's "Built in the open" link is gone from every page.
- Perps page uses the whole screen, in the shape of Hyperliquid and
  Binance: the header and the page run edge to edge, a market bar with the
  live statistics sits on top, the chart fills the left with the order
  book and recent trades in a column beside it, the order ticket and the
  account block stack on the right, and the positions, open orders, bots
  and history tabs sit under the chart. Bots are a tab there now (with a
  live count, like Positions and Open orders), the page switches to it
  after an order is placed, and the ticket button authorizes Perps trading
  directly when that is the only thing missing. Narrow screens stack the
  columns; phones get a single column.
- Perps page shows sizes in dollars: the positions table, the ticket
  facts, the bot cards, the drawer, the fill markers and the summary rows
  all lead with the dollar value at the mark, and the coin amount sits in
  the hover tooltip. Renew and Revoke leave the account panel: the
  Authorized chip opens a small menu with both, and a visible Renew pill
  appears on its own only inside the last week. The Isolated tooltip is
  one line: "Only the capital you commit is at risk".
- Perps page: "arm" is gone from the wording (Place order, Waiting,
  Resting, Cancel); the DCA safety orders, take profit and stop can be
  dragged on the chart and the ladder rescales to follow; the account
  panel gains Order history, Funding and Bot history tabs next to Trade
  history (the account route returns recent orders and funding payments,
  and a new bots/history route lists finished bots including removed ones).
- Perps order ticket in the shape traders know: Market and Limit entry
  tabs with stop entries and the webhook signal under Pro, Buy/Long and
  Sell/Short buttons, isolated margin and a leverage button with its own
  presets and slider, available margin and the current position shown,
  take profit and stop loss as an optional section with gain and loss
  percentages that convert to prices and back, and a compact summary
  (order value, margin, entry, liquidation, exits) instead of prose; the
  button itself says what blocks it. A limit entry rests as a post-only
  order and its exits attach once it fills; exits are optional for every
  directional bot. Pick-on-chart buttons, the EMA toggles and the market
  meta line are gone.
- Perps chart drawings: a trend line tool and a position tool (entry,
  target, stop) that shows size, gain, loss and the reward-to-risk ratio
  for the ticket's capital and leverage, drawn on an overlay and kept per
  market in the browser, with one click to load the drawn position into
  the ticket; a clear-drawings tool.
- Perps page: explanations move from paragraphs into hover tooltips on
  the controls themselves (row actions, drawer buttons, presets, bot-type
  tabs, chart tools, authorization buttons), rendered from one floating
  element so scrolling tables cannot clip them.
- Perps positions table in the shape traders know: size, value, entry,
  mark, PnL with return on margin, liquidation, margin, funding, close
  actions and the exchange-held take profit and stop loss. A row opens a
  drawer to close all or part of the position at market or with a
  reduce-only limit, to set or replace position-scoped exits (with quick
  return-on-margin presets), and to reach the bot managing that market.
  Two new commands back it: `/perps/position/close` and
  `/perps/position/exits`; a bot on the market adopts new exits.
- Perps chart tools: a tool strip beside the chart (draw a level, measure
  between two points, fit, jump to the latest candle, logarithmic scale,
  expand), draggable form lines (trigger, take profit, stop loss, grid
  range, quotes) that write back into the form, and a bot's exchange-held
  lines shown only while its card is selected. Capital and leverage get
  sliders; the capital slider runs from the kind's minimum to the account's
  available margin.
- Perps: two more bot kinds and two directional options. A grid bot
  rests rungs across a range and answers each fill one level away, with
  neutral, long and short variants and a loss limit. A DCA bot sends a
  base order, rests safety orders at multiplied steps, keeps a reduce-only
  take profit on the exchange's average entry and an optional stop, and
  can repeat. A directional bot can trail its stop (new stop placed before
  the old one is cancelled) and can be triggered by a private webhook
  ("enter long", "enter short", "close", "pause"), taking its exits as
  percentages from the fill. The page gets Grid and DCA tabs, the signal
  trigger, percentage exits, a trailing field, range picks on the chart,
  rung and safety levels drawn on it, and cards for every kind.
- Perps chart: TradingView's open-source Lightweight Charts (Apache 2.0,
  vendored under site/vendor) replaces the hand-drawn canvas. Candles with
  volume, crosshair, zoom and pan, older history loaded as you scroll
  left, 4h and 1D intervals, EMA 20 and EMA 50, your trigger, take profit,
  stop loss or quotes as dashed lines, your live entry and exchange-held
  exits as solid lines, your fills as markers, and Pick on chart buttons
  that set the trigger, take profit or stop loss from a click.
- Perps page: a market stats strip above the chart (mark, index, 24h
  change and volume, open interest, funding with a countdown, max
  leverage), a compact order book and recent trades from the public feed,
  Positions, Open orders and Fills tabs on the account panel (the account
  route now returns recent fills), the chart stretched to the form height,
  and less prose.
- Perps page starts at the account panel: authorization state as a chip,
  the days left with a Renew button (a renewal deletes the previous key on
  the exchange), account figures in a grid, and a note on what expiry
  means. Bot cards show whether the exchange holds the exits, entry prices
  come from the exchange's average fill instead of the limit that was
  sent, and Remove on a finished bot archives it instead of doing nothing.
- Perps prices are rounded to what the venue accepts (market decimals, at
  most five significant figures) on the server and in the page; the first
  live entry had been refused for six-figure exits. A refused entry is
  marked as not executed instead of showing a planned entry price.
- Perps bots (pilot). A new Perps page sets up one rule-based bot per
  Polymarket Perps market on a chart: enter long or short now or at a trigger
  price, a capital limit, isolated leverage capped at 5x, and exchange-held
  take profit and stop loss attached to a fill-or-kill entry. The server holds
  a proxy signer the owner authorizes with one wallet signature; it trades and
  reads the account but cannot move collateral. Pause and Close position are
  separate actions, results after a close include fees and funding, and an
  uncertain entry is reconciled before any retry. Entries that round under
  the market minimum round up to it within five percent of the capital
  limit; the page prefills exits from the live price and says why Arm is
  disabled. See docs/perps.md.
- Live venue: an order Polymarket no longer returns (404, or 200 with a
  null body, as seen for quotes the exchange dropped after a balance fall)
  is settled from the trade pages after a two-minute grace period and a
  second look, instead of blocking reconciliation forever. Activation
  failures now carry their cause, restores keep trying for an hour (four
  quick attempts, then every five minutes) and report progress in the
  status payload and on the dashboard.
- Hosted live quoting pilot. A new Polymarket venue signs Deposit Wallet
  orders locally with a stored session key (ERC-7739 wrap plus the
  session-signer envelope), submits each order once as post-only GTC with the
  builder code, reconciles against the authenticated order and trade
  endpoints, reads funding from the chain and the CLOB, and keeps the venue
  heartbeat alive. `/trading/live/start`, `/stop` and `/status` run one
  supervised Quote-both-sides agent per wallet, behind the wallet sign-in
  and an owner allowlist (`ODDSRAIL_HOSTED_LIVE=1`, `ODDSRAIL_LIVE_OWNERS`).
- The activation check reports real funding, permission and runner
  readiness when the pilot is enabled, and the build page shows start and
  stop controls with live status on a ready draft.
- Minimum order sizes are shares. The planner used to read Gamma's
  `orderMinSize` as a dollar minimum and refused every quote under five
  dollars; the CLOB documents the minimum in shares and live books carry
  five-share orders worth under two dollars. The larger share minimum from
  Gamma and the CLOB now applies. Start also plans the first pair from the
  current books and refuses limits that can never quote, with the numbers.
- Book freshness follows connection liveness instead of per-token change
  times. The first live agent placed two acknowledged quotes and was halted
  11 seconds later because neither book had changed for five seconds. The
  feed now pings every two seconds, any delivered frame keeps an unchanged
  book current, the hosted runner cross-checks the top of book against the
  REST endpoint and reconnects on repeated disagreement, and halts are logged
  with feed diagnostics that the status route also reports.
- Book snapshots are stamped with their last change, not with delivery, so a
  quiet market's snapshot no longer counts as delayed data; only change events
  carry delivery lag. The hosted runner warms the feed up before activation,
  resumes on its own after transient feed or metadata halts (bounded), and
  the build page confirms a start inline instead of with a browser dialog.
- After the fourth hosted start, the first reconciliation on an in-play
  sports market failed validation for twenty seconds and the halt loop
  re-cancelled three times a second. The order reads now accept the documented
  delay-window statuses, every validation failure is recorded with its own
  message in the venue status and the halt log, a blocked account gets one
  cancellation pass every two seconds, the first halt reason is kept, and
  transient reconciliation halts resume on their own once the orders are final.
- A server restart brings back every hosted agent its owner left running,
  through the same recover, reconcile and verified start, so deploys no longer
  end a session; a restore that fails is recorded and shown as stopped.
- Documented Polymarket's credential scoping: the app's Open orders tab does
  not list the hosted agent's quotes and the agent cannot see app-placed
  orders; the live panel says so and the status is the record of its orders.
- The dashboard shows the hosted agent: an Agent figure next to Balance in
  the header and on the overview with the money it is handling (resting
  quotes plus settled fills), and a card on the Agents tab with the market,
  its open quotes and its fills. The status route now carries the market
  title, outcome labels, the money figures and the fills.
- The quoting distance can go down to a tenth of a cent (0.001), which joins
  the best bid on one-cent markets; the activation panel shows a server-verified
  session permission as active.

## 0.18.1 (2026-09-21)

- Claude Desktop extension: install a version-pinned local server with a
  settings form, secure private-key storage and simulation enabled by default.
- The installer defaults to $25 per order and $100 submitted per process
  session; restarting the server resets the session budget.
- Builder handoff: download the extension and take the exact strategy settings
  to Claude for review. It does not automatically run website strategy rules.
- Concurrent local orders now reserve their shared session budget atomically;
  uncertain submissions keep their reservation. Open-order counts are checked
  before both Polymarket and Kalshi submission.
- Authenticated cash reads replace the misleading public holdings lookup in
  `my_balance`. Cash is separate from the unverified amount available to trade.
- Empty optional wallet fields no longer pass an invalid address to the SDK.
- Bundle tests no longer require the optional image-generation dependency.
  Pillow loads only when rebuilding the extension icon.


- The `attribution_ledger` tool description pointed agents at
  oddsrail.app/attribution, which was removed and now returns 404. It now
  names Polymarket's own public builder feed, where the figures can be
  checked.
- Site: the six notes on the venue APIs are reachable again from the
  navigation's More menu, and the run permalink page no longer tells visitors
  to press a Run button that the builder redesign removed.
- The launch post describes the product as it is now: markets and a builder
  that saves drafts on the site, paper trading by chat through the Claude
  connector, and live trading self-hosted. Every reference to the removed
  attribution page and the old one-click paper pass is gone.

## 0.18.0 (2026-09-11)

- **`my_balance`**: the operator's real Polymarket collateral, so an agent
  can size against the account rather than a number it assumed. Kalshi had
  one; Polymarket did not. 42 tools.
- A dry-run order now reports `accepted`, like every other path. A paper
  account that could not fund the order used to return a top level that read
  like success.
- `daily_review` works on a fresh install: it starts with `server_info` and
  routes to the paper ledger when the live tools have nothing to report, and
  every no-key answer now names the tool that does have data.
- The guardrail note says which rules apply in dry-run. Two of the four are
  live-only by nature, and claiming otherwise gave false confidence.
- A mark that cannot be refreshed for twelve hours stops counting toward
  equity and is listed as stale, instead of being carried forever.
- One inbox is one account: plus-addressing and gmail dots normalise, which
  also closes the hole under the board's new activity rules.
- **Six notes** at `/notes/`, one page per venue-API footgun, each with its
  own title, description, structured data and share card, linked from the
  home page, the navigation, the sitemap and llms.txt.
- CI: the site is checked for parse errors, em dashes in prose, internal
  links that go nowhere and a sitemap that lists missing pages. A scheduled
  workflow smoke-tests the hosted server every six hours.

## 0.17.1 (2026-09-11)

- A `market_id` from a search now works in the tools that describe a market.
  `find_markets` and `search_markets` hand back a CLOB token id, while
  `resolution_criteria`, `dispute_risk`, `settlement_audit` and `get_market`
  used to accept only a slug or a Gamma id, so chaining two tools failed with
  "invalid integer" from the venue. All of them now take any of the three,
  and say so in their descriptions.

## 0.17.0 (2026-09-10)

More fixes from the adversarial audit, verified by reproduction.

- `check_order` no longer reads the word "fade" as the NO side. Fading a
  drop means buying YES, so the flagship workflow was being blocked when an
  operator described it in plain words.
- The paper ledger no longer crashes when a token is delisted: a market
  lookup that comes back empty is handled, so one dead position cannot take
  down a whole ledger read or drop an agent off the board.
- Resting paper orders fill only the depth that actually crosses them, at
  the maker's own price, and keep resting for the remainder. They used to
  fill their whole size on any touch.
- `cancel_all_orders` clears resting paper orders in dry-run, which is the
  only place orders exist there. The kill switch left them alive.
- Signing out revokes a connector's OAuth token, not just a website session.
- Scheduled hourly passes store a permalink like manual ones do.
- The book-watch window respects the number you set, up to a minute.
- Arena: an entry is ranked only after 10 fills, 6 passes and 12 hours; the
  rest are listed with the reason. A registered agent cannot reset its own
  ledger. Reserved words cannot be worn inside a name.
- Tests are hermetic (an operator's own environment cannot turn them red),
  the settle and value strategies and take profit have coverage, and a test
  now keeps the published tool count honest.
- Site: share cards on every page, the home page leads with the demo, and
  the sitemap no longer advertises two parameterless share shells. The
  registry entry advertises the hosted endpoint.

## 0.16.0 (2026-09-10)

- **Shareable passes.** Every hosted pass gets a permalink: `/run?id=...`
  on the site, served from `GET /runs/<id>.json`, showing the decisions,
  the verdicts and the switches behind them, with a button to build the
  same agent. Anonymous runs are kept 30 days; runs belonging to an
  account are kept.
- **A public page per agent** at `/agent?name=...`, from
  `GET /arena/agent/<name>.json`: strategy, equity curve from the recorded
  run history, the last pass in full, and a permalink to it. Names on the
  arena board link to it.
- The account API accepts the OAuth access token a connector already holds,
  not only a website session, so the same account works from either side.
- Housekeeping prunes expired anonymous runs.

## 0.15.0 (2026-09-08)

Fixes from an eight-lens adversarial audit of the hosted runner, the
builder and the operations around them.

- Runner: the daily loss limit no longer blocks stop-loss and take-profit
  exits; the exposure cap counts what is already held; two-sided quotes are
  refreshed each pass instead of stacking and respect the positions cap;
  the fill-quality rule walks the whole book, refuses real slippage and
  sets the limit to the deepest level touched (never more than five points
  through the book); two pieces cannot buy both sides of one market in a
  pass; fades act only on jumps fresh by the clock and price fair value
  from the series' own retrace; sells carry the position's real outcome;
  every risk number is bounded.
- Paper ledger: positions in resolved markets settle at the payout (1 or 0)
  as a recorded fill instead of being valued at zero; a missing book
  carries the last mark.
- Hosted server: pass deadlines for requests (120 s) and scheduled runs
  (180 s); a scheduler heartbeat on /healthz; hourly housekeeping (expired
  tokens and links, guest ledgers idle for 30 days); /run/reset never
  creates files for unknown ids and is rate limited; the connector sign-in
  page no longer claims an email was sent when mail is not configured.
- Deploy: nightly backups (sqlite plus ledgers, 14 kept), a watchdog timer
  that restarts a dead app or a stale scheduler, resource caps on the unit.
- Builder: hero and copy sell the Run path; the Connect panel is collapsed;
  the always-on rules state honestly how cautions are handled; the starter
  preset no longer switches on two-sided quoting; Run waits for the server
  probe and a fixed Run bar appears on phones; expired sessions are
  reported; Keep this agent is hidden until mail delivery is live;
  accessibility labels on switches, inputs and tabs; Open Graph and
  Twitter card tags with a share image on the home, builder and arena pages.
- Server URL is now https://mcp.oddsrail.app.

## 0.14.0 (2026-09-07)

- **Keep this agent.** Email sign-in on the builder's Run tab: the link
  turns the browser's guest ledger into the account's, hands the page a
  bearer session, and unlocks naming the agent, putting it on the arena
  board and running it hourly. Endpoints `/claim/start`, `/claim/verify`,
  `/me`, `/agents`, `/logout`; `/run` and `/run/ledger` use the account's
  ledger when a session is present.
- **Hourly scheduling.** A loop inside the hosted server runs every kept
  agent whose schedule is hourly, one at a time on its own ledger, and
  stores a result summary on the agent (`oddsrail.cloud.scheduler`).
- Launch post: the Show HN title and first comment rewritten for the
  builder and the arena.

## 0.13.3 (2026-09-06)

- Builder and runner: markets are chosen by ticking categories (everything,
  crypto, politics, geopolitics, economy and the Fed, business, football,
  esports, tennis, US sports, motorsport), any number at once, plus an
  optional keyword. Categories map to Polymarket's own tags
  (`polymarket.markets_by_tag`), fetched in parallel, merged and
  de-duplicated, most traded first. The run record's universe summary
  names the categories and keyword used.

## 0.13.2 (2026-09-06)

- Runner universe: an empty topic now means the whole venue, ordered by 24h
  volume (`polymarket.top_markets`); a topic reads 40 search results
  instead of 12 before the filters; up to 15 candidates per pass. Each
  strategy logs what it checked and why nothing qualified, and the run
  record carries a `universe` summary (scanned, candidates, dropped).
- Builder: topic quick picks, a starter preset on first visit, and an
  explanation of an empty pass in the panel instead of a bare zero.
  Stylesheet and scripts are versioned in the page so a cached old
  stylesheet cannot break the layout.

## 0.13.1 (2026-09-06)

- Runner: `check_order` cautions about the resolution source or liquidity
  are advisory unless the matching hygiene switch is on (dispute risk,
  refuse bad fills), and fill checks do not apply to resting quotes. The
  accepted cautions are recorded on the decision. Everything else that is
  not ok still stops the order.

## 0.13.0 (2026-09-06)

- **The runner** (`oddsrail.cloud.runner`): a deterministic paper pass for a
  builder configuration. Scans the universe, computes the signal each
  strategy names (overshoot, closing-soon resolution, momentum, stated
  probabilities, two-sided quotes), sizes with Kelly, applies the risk rules
  to what is held (stop loss, take profit, daily loss limit, exposure caps,
  never add to a loser), runs `check_order` on every order and paper-fills
  against the live book. No model is consulted; every decision is returned
  with its verdict and reason.
- Hosted endpoints `POST /run`, `GET /run/ledger`, `POST /run/reset` with a
  guest ledger per browser, CORS for the site, rate limits, and a lock per
  ledger. The builder page gets a Run tab: press Run, read the pass, no
  account, nothing installed.
- `paper.forced_ledger`: a context variable that points every paper call at
  one file for the duration of a task; the arena board and the runner use it.

## 0.12.0 (2026-09-06)

- **Paper arena** on the hosted server: `arena_register`, `arena_unregister`
  and `arena_status` tools (hosted profile only) put an account's paper
  ledger on a public board under a display name; `GET /arena/paper.json`
  serves the board, recomputed at most every five minutes, with the house
  paper agent as an unranked reference row (`ODDSRAIL_ARENA_HOUSE_LEDGER`).
- Site: the agent builder page (`/build`, tickable strategy fragments and
  risk rules composing an editable prompt for Claude) and the arena page
  (`/arena`, paper and live divisions from public data, live-division
  registry in `site/arena/agents.json`).

## 0.11.0 (2026-09-06)

- **Hosted server** (`oddsrail.cloud`, `oddsrail/hosted.py`): the same
  server as a remote MCP endpoint over streamable HTTP with OAuth 2.1
  accounts (dynamic client registration, PKCE, magic-link sign-in by email,
  rotating tokens). Runs at `https://mcp.oddsrail.app/mcp`; add it in Claude
  as a custom connector or with `claude mcp add --transport http`. Each
  account gets its own paper ledger.
- The hosted profile (`ODDSRAIL_HOSTED=1`) holds no keys, forces dry-run,
  papers every order against the live Polymarket book, hides the
  account-scoped and relayer tools, and does not serve Kalshi (its API
  Developer Agreement limits API use to a member's own trading). Twenty-one
  tools remain. `server_info` reports `hosted` and the signed-in `account`.
- `paper.ledger_resolver`: a hook a multi-tenant host installs to map the
  current request to its account's ledger; local single-operator behaviour
  is unchanged.
- Deployment files in `deploy/cloud/` (systemd unit, Caddyfile, env
  template). Privacy policy at oddsrail.app/privacy.
- Tests: the hosted flow end to end against a real local process
  (`tests/test_cloud.py`). 143 tests.
- Docs: tool count corrected to 41 (32 read-only, 9 trading).

## 0.10.2 (2026-09-06)

- **`check_order`**: deterministic pre-trade verification of a proposed order
  against the operator's intent, the live market and the guardrails. Checks
  that the market exists and is open, that the intent's words match the
  market and the YES/NO side, that the price is sane against the book, the
  size and Polymarket's $1 minimum, the guardrails, liquidity within the
  limit, and the resolution source. Returns ok / caution / block with
  evidence and a one-line read-back. No second model; nothing is sent. The
  `find_fade_setup` prompt (and skill) now calls it before `place_order`.

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
