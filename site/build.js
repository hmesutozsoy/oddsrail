(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };

  // Every fragment: id, group, label, blurb, optional inputs, and a render
  // function that returns prompt lines given the config and its own values.
  var FRAGMENTS = [
    // ------------------------------ strategies ------------------------------
    { id: 'fade', group: 'strategies', label: 'Fade overshoots (mean reversion)',
      blurb: 'Trade against fresh panic jumps that the market has historically reverted.',
      inputs: [{ k: 'jump', label: 'minimum jump, % of price', def: 8 }, { k: 'hours', label: 'lookback, hours', def: 6 }],
      render: function (c, v) { return [
        'Strategy: fade overshoots.',
        '- For each candidate market call overshoot_signal(token_id, hours=' + v.hours + ', threshold=' + (v.jump / 100) + ').',
        '- Act only when it reports a fresh jump of at least ' + v.jump + '% of price AND the market\'s own history shows reversion after jumps of that size. No signal, no trade.',
        '- Trade against the jump: if YES jumped up, buy NO (or sell YES if held); if YES jumped down, buy YES.',
        '- Fair value is the pre-jump price. Size with position_size(bankroll_usd=' + c.bankroll + ', price=<entry>, fair_value=<pre-jump price>) and use no more than the quarter-Kelly figure it returns.',
        '- Exit when the price has retraced half the jump, or at the risk rules below.' ]; } },
    { id: 'settle', group: 'strategies', label: 'Buy near-certain resolutions',
      blurb: 'Markets priced 0.90 to 0.97 with an objective resolution source, held to resolution.',
      inputs: [{ k: 'lo', label: 'minimum price', def: 0.9 }, { k: 'hi', label: 'maximum price', def: 0.97 }],
      render: function (c, v) { return [
        'Strategy: buy near-certain resolutions.',
        '- Use closing_soon(hours=72, venues="polymarket") and find_markets to list markets whose likely side trades between ' + v.lo + ' and ' + v.hi + '.',
        '- For each, call resolution_criteria(venue="polymarket", market_id=<slug or id>) and dispute_risk(<slug or id>). Require a named, objective resolution source and a dispute_risk score of 20 or lower. Read the criteria and state in one sentence why the likely side resolves YES.',
        '- Buy the likely side at the ask only if the remaining upside (1 - price) exceeds 2.5% and quote_cost shows the size fills inside the limit.',
        '- Hold to resolution. Note for paper: the ledger marks at the mid and does not pay out at resolution, so judge this strategy by the mark, not by cash.' ]; } },
    { id: 'value', group: 'strategies', label: 'Trade my own probability',
      blurb: 'You supply the view; the agent trades only where the market disagrees enough.',
      inputs: [{ k: 'edge', label: 'minimum edge, probability points', def: 5 }, { k: 'kelly', label: 'Kelly fraction', def: 0.25 }],
      text: { k: 'views', label: 'my views, one per line as "market: probability"', def: 'Will Bitcoin be above 80,000 on the last day of the month: 0.35' },
      render: function (c, v) { return [
        'Strategy: trade my stated probabilities.',
        'My views (market: my probability):',
        (v.views || '').split('\n').filter(function (l) { return l.trim(); }).map(function (l) { return '  ' + l.trim(); }).join('\n') || '  (none given)',
        '- Find each market with find_markets, confirm it is the same event by reading resolution_criteria, and compare the market price with my probability.',
        '- Trade only when the gap is at least ' + v.edge + ' probability points in my favour: buy YES when my probability is higher than the ask, buy NO when it is lower than the bid.',
        '- Size with position_size(bankroll_usd=' + c.bankroll + ', price=<entry>, fair_value=<my probability>, max_fraction_of_kelly=' + v.kelly + ').' ]; } },
    { id: 'momentum', group: 'strategies', label: 'Follow the move',
      blurb: 'Buy what has moved with rising volume; give back half and you are out.',
      inputs: [{ k: 'move', label: 'minimum move, probability points', def: 10 }, { k: 'hours', label: 'over, hours', def: 6 }],
      render: function (c, v) { return [
        'Strategy: follow the move.',
        '- For each candidate call price_history(token_id, hours=' + v.hours + '). Require a move of at least ' + v.move + ' probability points in one direction with 24h volume above the universe minimum.',
        '- Buy the side that moved, at the ask, only if the spread is 3 points or less.',
        '- Exit when the price gives back half of the move measured from entry, or at the risk rules below.' ]; } },
    { id: 'mm', group: 'strategies', label: 'Two-sided quotes (market making)',
      blurb: 'Rest a bid on YES and a bid on NO below the mid and collect the spread. Read the warning.',
      inputs: [{ k: 'edge', label: 'distance below mid, probability points', def: 2 }, { k: 'shares', label: 'shares per side', def: 20 }],
      render: function (c, v) { return [
        'Strategy: two-sided quotes.',
        '- Pick liquid markets (spread of 2 points or less, 24h volume above the minimum). Read get_orderbook.',
        '- Rest a BUY on YES at (mid - ' + v.edge + ' points) and a BUY on NO at ((1 - mid) - ' + v.edge + ' points), ' + v.shares + ' shares each, with post_only=True so neither order crosses.',
        '- When both sides fill you hold a YES and a NO share pair worth exactly $1 at resolution. On a self-hosted server with a relayer key you can merge_positions to get the dollar back now; on the hosted server just hold.',
        '- Warning written by the maintainer: our own market-making engine lost about 4.5 cents per share on markout live at this size, after looking fine on paper. Paper fills here assume no queue and no adverse selection. Treat paper results from this fragment as an upper bound and never take it live without the merge path and small size.' ]; } },

    // ------------------------------ risk rules ------------------------------
    { id: 'stoploss', group: 'risk', label: 'Stop loss', inputs: [{ k: 'pct', label: '% below entry', def: 25 }],
      render: function (c, v) { return ['- Stop loss: at each review, if a position\'s mark is ' + v.pct + '% or more below its average entry price, sell the whole position at the current bid with place_order (side SELL, price = best bid). Polymarket has no stop orders, so this only triggers when you review.']; } },
    { id: 'takeprofit', group: 'risk', label: 'Take profit', inputs: [{ k: 'pct', label: '% above entry', def: 40 }],
      render: function (c, v) { return ['- Take profit: at each review, if a position\'s mark is ' + v.pct + '% or more above its average entry, sell it at the bid.']; } },
    { id: 'daily', group: 'risk', label: 'Daily loss limit', inputs: [{ k: 'usd', label: 'USD', def: 50 }],
      render: function (c, v) { return ['- Daily loss limit: if realized plus unrealized P&L for today (paper_positions) is worse than -$' + v.usd + ', place no new orders for the rest of the day and say so.']; } },
    { id: 'noadd', group: 'risk', label: 'Never add to a losing position',
      render: function () { return ['- Never add to a position whose mark is below its average entry.']; } },
    { id: 'expo', group: 'risk', label: 'Max exposure per market', inputs: [{ k: 'usd', label: 'USD', def: 50 }],
      render: function (c, v) { return ['- Max exposure per market: $' + v.usd + ' of cost across all positions in the same market.']; } },

    // ------------------------------- hygiene --------------------------------
    { id: 'dispute', group: 'hygiene', label: 'Skip dispute-prone resolutions', inputs: [{ k: 'score', label: 'max dispute_risk score', def: 30 }],
      render: function (c, v) { return ['- Before any order call resolution_criteria(venue="polymarket", market_id=<market slug or id>) and dispute_risk(<market slug or id>); both take the market, not the token id (get_market gives the slug). Skip it if no resolution source is named or the score is above ' + v.score + '.']; } },
    { id: 'liquidity', group: 'hygiene', label: 'Refuse bad fills', inputs: [{ k: 'slip', label: 'max slippage vs best price, %', def: 2 }],
      render: function (c, v) { return ['- Call quote_cost for the exact size before ordering. Skip if the walked average price is more than ' + v.slip + '% worse than the best price, or if the book cannot fill the size inside the limit.']; } },
    { id: 'watch', group: 'hygiene', label: 'Watch the book before acting', inputs: [{ k: 'sec', label: 'seconds', def: 20 }],
      render: function (c, v) { return ['- After choosing a market, call watch_book(token_id, seconds=' + v.sec + ') and only proceed if the book did not move against the plan while you watched.']; } },

    // -------------------------------- extras --------------------------------
    { id: 'report', group: 'extras', label: 'Report as a table', checked: true,
      render: function () { return [
        'Reporting: when the pass is done, show one table with a row per decision: market, side, price, size, check_order verdict, what happened (paper fill, resting, skipped and why). Then paper_positions as cash, equity, realized and unrealized P&L. If nothing qualified, say so plainly instead of forcing a trade.' ]; } },
    { id: 'arena', group: 'extras', label: 'Enter the paper arena', inputs: [{ k: 'name', label: 'agent name', def: 'my-agent', text: true }],
      render: function (c, v) { return ['Arena: if arena_status shows this account is not registered, call arena_register(name="' + (v.name || 'my-agent') + '", strategy="<one line describing this prompt>") once, so the paper ledger appears on oddsrail.app/arena.']; } }
  ];

  var BY_ID = {}; FRAGMENTS.forEach(function (f) { BY_ID[f.id] = f; });

  // ------------------------------ form rendering ------------------------------
  function optionHtml(f) {
    var h = '<div class="opt"><label><input type="checkbox" name="f_' + f.id + '"' + (f.checked ? ' checked' : '') + '> <b>' + f.label + '</b>' + (f.blurb ? '<span class="blurb">' + f.blurb + '</span>' : '') + '</label>';
    if (f.inputs) f.inputs.forEach(function (i) {
      h += '<label class="inl">' + i.label + '<input type="' + (i.text ? 'text' : 'number') + '" name="' + f.id + '_' + i.k + '" value="' + i.def + '"' + (i.text ? '' : ' step="any"') + '></label>';
    });
    if (f.text) h += '<label class="inl wide">' + f.text.label + '<textarea name="' + f.id + '_' + f.text.k + '" rows="3">' + f.text.def + '</textarea></label>';
    return h + '</div>';
  }
  ['strategies', 'risk', 'hygiene', 'extras'].forEach(function (g) {
    $(g).innerHTML = FRAGMENTS.filter(function (f) { return f.group === g; }).map(optionHtml).join('');
  });

  var form = $('bform');
  function read() {
    var fd = new FormData(form), c = { mode: fd.get('mode') || 'paper', on: {}, vals: {} };
    ['topic', 'minvol', 'bankroll', 'perorder', 'maxpos', 'closing'].forEach(function (k) { c[k] = fd.get(k); });
    FRAGMENTS.forEach(function (f) {
      c.on[f.id] = fd.get('f_' + f.id) === 'on';
      var v = {};
      (f.inputs || []).forEach(function (i) { v[i.k] = fd.get(f.id + '_' + i.k); });
      if (f.text) v[f.text.k] = fd.get(f.id + '_' + f.text.k);
      c.vals[f.id] = v;
    });
    return c;
  }
  function apply(c) {
    if (!c) return;
    form.querySelectorAll('[name]').forEach(function (el) {
      var n = el.name;
      if (n === 'mode') { el.checked = (el.value === c.mode); return; }
      if (n.slice(0, 2) === 'f_') { el.checked = !!c.on[n.slice(2)]; return; }
      if (c[n] !== undefined) { el.value = c[n]; return; }
      var m = n.match(/^([a-z]+)_(.+)$/);
      if (m && c.vals[m[1]] && c.vals[m[1]][m[2]] !== undefined) el.value = c.vals[m[1]][m[2]];
    });
  }

  // ------------------------------- composition --------------------------------
  function compose(c) {
    var paper = c.mode === 'paper';
    var L = [];
    L.push('You are trading prediction markets through the oddsrail MCP server. Use its tools, in the order given, and never invent a market, a price or a fill.');
    L.push(paper
      ? 'Mode: hosted PAPER trading. Orders are simulated against the live book into my paper ledger; nothing real is sent. Start by calling server_info and confirming hosted is true.'
      : 'Mode: LIVE, self-hosted. Start by calling server_info. Stop and tell me if dry_run is not false, if trading_key_configured is not true, or if guardrails shows no per-order cap. Real money: when in doubt, do not trade.');
    L.push('');
    L.push('UNIVERSE');
    L.push('- Markets about: ' + (c.topic || '(anything)') + '. Use find_markets(query, venues="polymarket"); on Polymarket, market_id is the token id of the YES or NO side.');
    L.push('- Skip markets with less than $' + c.minvol + ' of 24h volume' + (+c.closing > 0 ? ', and markets closing within ' + c.closing + ' hours' : '') + '.');
    L.push('- Bankroll for this agent: $' + c.bankroll + '. Never spend more than $' + c.perorder + ' on one order, never hold more than ' + c.maxpos + ' open positions.');
    L.push('- Prices are implied probabilities in (0,1); size is in shares; the exchange refuses marketable orders under $1.');
    L.push('');
    var strat = FRAGMENTS.filter(function (f) { return f.group === 'strategies' && c.on[f.id]; });
    if (!strat.length) L.push('STRATEGY\n- (no strategy ticked: do a read-only pass, list the qualifying markets with prices and liquidity, and place nothing)');
    strat.forEach(function (f) { L.push('STRATEGY'); L = L.concat(f.render(c, c.vals[f.id])); L.push(''); });
    L.push('BEFORE EVERY ORDER (not optional)');
    L.push('1. quote_cost(venue="polymarket", market_id, side, size) for the exact size.');
    L.push('2. check_order(venue="polymarket", market_id, side, price, size, intent="<my words for this trade>", outcome="yes" or "no"). Pass my intent verbatim.');
    L.push('3. Verdict ok: place_order. Verdict caution: stop and show me the checks before doing anything. Verdict block: do not place it, and do not retry with different numbers.');
    L.push('4. Never resubmit an order whose result is unknown; call paper_positions' + (paper ? '' : ' or open_orders') + ' first.');
    L.push('');
    var risk = FRAGMENTS.filter(function (f) { return f.group === 'risk' && c.on[f.id]; });
    var hyg = FRAGMENTS.filter(function (f) { return f.group === 'hygiene' && c.on[f.id]; });
    if (risk.length) { L.push('RISK RULES'); risk.forEach(function (f) { L = L.concat(f.render(c, c.vals[f.id])); }); L.push(''); }
    if (hyg.length) { L.push('MARKET HYGIENE'); hyg.forEach(function (f) { L = L.concat(f.render(c, c.vals[f.id])); }); L.push(''); }
    FRAGMENTS.filter(function (f) { return f.group === 'extras' && c.on[f.id]; }).forEach(function (f) {
      if (f.id === 'arena' && !paper) return;
      L = L.concat(f.render(c, c.vals[f.id])); L.push('');
    });
    L.push('Do one full pass now: review existing positions against the rules first, then look for new entries. Ask before anything the rules do not cover.');
    return L.join('\n');
  }

  // --------------------------------- wiring -----------------------------------
  var out = $('out'), status = $('status');
  function save(c) { try { localStorage.setItem('oddsrail-build', JSON.stringify(c)); } catch (e) {} }
  function load() { try { return JSON.parse(localStorage.getItem('oddsrail-build') || 'null'); } catch (e) { return null; } }
  function regen() {
    var c = read(); save(c);
    out.value = compose(c);
    $('open').href = 'https://claude.ai/new?q=' + encodeURIComponent(out.value);
    status.textContent = out.value.length + ' characters';
  }
  function flash(msg) { status.textContent = msg; setTimeout(function () { status.textContent = out.value.length + ' characters'; }, 1400); }

  apply(load());
  regen();
  form.addEventListener('change', regen);
  form.addEventListener('input', function (e) { if (e.target.tagName !== 'INPUT' || e.target.type === 'text' || e.target.type === 'number') regen(); });
  $('regen').addEventListener('click', regen);
  $('copy').addEventListener('click', function () { navigator.clipboard.writeText(out.value).then(function () { flash('copied'); }); });
  $('copycfg').addEventListener('click', function () { navigator.clipboard.writeText(JSON.stringify(read(), null, 1)).then(function () { flash('config copied'); }); });
  out.addEventListener('input', function () { $('open').href = 'https://claude.ai/new?q=' + encodeURIComponent(out.value); status.textContent = out.value.length + ' characters (edited)'; });
})();
