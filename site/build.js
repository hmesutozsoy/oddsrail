(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };

  // The hosted server's address. The final hostname needs a DNS record the
  // maintainer controls; until it resolves, the page shows the temporary one
  // so the connector can be added today and the prompt names a live URL.
  var HOSTS = ['https://mcp.oddsrail.app', 'https://151-241-155-39.sslip.io'];
  var HOST = HOSTS[0];
  function probe(i) {
    if (i >= HOSTS.length) return Promise.resolve(null);
    return fetch(HOSTS[i] + '/healthz', { cache: 'no-store' }).then(function (r) { return r.ok ? HOSTS[i] : probe(i + 1); }).catch(function () { return probe(i + 1); });
  }

  // Every fragment: id, group, label, tag, blurb, params (k, label, def, unit,
  // text for a free-text field), and render(cfg, values) -> prompt lines.
  var FRAGMENTS = [
    // ------------------------------ strategies ------------------------------
    { id: 'fade', group: 'strategies', label: 'Fade overshoots', tag: 'mean reversion',
      blurb: 'Trade against fresh panic jumps the market has historically reverted.',
      params: [{ k: 'jump', label: 'jump ≥', def: 8, unit: '%' }, { k: 'hours', label: 'lookback', def: 6, unit: 'h' }],
      render: function (c, v) { return [
        'Strategy: fade overshoots.',
        '- For each candidate market call overshoot_signal(token_id, hours=' + v.hours + ', threshold=' + (v.jump / 100) + ').',
        '- Act only when it reports a fresh jump of at least ' + v.jump + '% of price AND the market\'s own history shows reversion after jumps of that size. No signal, no trade.',
        '- Trade against the jump: if YES jumped up, buy NO (or sell YES if held); if YES jumped down, buy YES.',
        '- Fair value is the pre-jump price. Size with position_size(bankroll_usd=' + c.bankroll + ', price=<entry>, fair_value=<pre-jump price>) and use no more than the quarter-Kelly figure it returns.',
        '- Exit when the price has retraced half the jump, or at the risk rules below.' ]; } },
    { id: 'settle', group: 'strategies', label: 'Buy near-certain resolutions', tag: 'carry',
      blurb: 'Markets priced 0.90 to 0.97 with an objective resolution source, held to resolution.',
      params: [{ k: 'lo', label: 'price ≥', def: 0.9 }, { k: 'hi', label: 'price ≤', def: 0.97 }],
      render: function (c, v) { return [
        'Strategy: buy near-certain resolutions.',
        '- Use closing_soon(hours=72, venues="polymarket") and find_markets to list markets whose likely side trades between ' + v.lo + ' and ' + v.hi + '.',
        '- For each, call resolution_criteria(venue="polymarket", market_id=<slug or id>) and dispute_risk(<slug or id>). Require a named, objective resolution source and a dispute_risk score of 20 or lower. Read the criteria and state in one sentence why the likely side resolves YES.',
        '- Buy the likely side at the ask only if the remaining upside (1 - price) exceeds 2.5% and quote_cost shows the size fills inside the limit.',
        '- Hold to resolution. Note for paper: the ledger marks at the mid and does not pay out at resolution, so judge this strategy by the mark, not by cash.' ]; } },
    { id: 'value', group: 'strategies', label: 'Trade my own probability', tag: 'view',
      blurb: 'You supply the view; the agent trades only where the market disagrees enough.',
      params: [{ k: 'edge', label: 'edge ≥', def: 5, unit: 'pts' }, { k: 'kelly', label: 'kelly ×', def: 0.25 }],
      text: { k: 'views', label: 'my views, one per line as "market: probability"', def: 'Will Bitcoin be above 80,000 on the last day of the month: 0.35' },
      render: function (c, v) { return [
        'Strategy: trade my stated probabilities.',
        'My views (market: my probability):',
        (v.views || '').split('\n').filter(function (l) { return l.trim(); }).map(function (l) { return '  ' + l.trim(); }).join('\n') || '  (none given)',
        '- Find each market with find_markets, confirm it is the same event by reading resolution_criteria, and compare the market price with my probability.',
        '- Trade only when the gap is at least ' + v.edge + ' probability points in my favour: buy YES when my probability is higher than the ask, buy NO when it is lower than the bid.',
        '- Size with position_size(bankroll_usd=' + c.bankroll + ', price=<entry>, fair_value=<my probability>, max_fraction_of_kelly=' + v.kelly + ').' ]; } },
    { id: 'momentum', group: 'strategies', label: 'Follow the move', tag: 'momentum',
      blurb: 'Buy what has moved with rising volume; give back half and you are out.',
      params: [{ k: 'move', label: 'move ≥', def: 10, unit: 'pts' }, { k: 'hours', label: 'over', def: 6, unit: 'h' }],
      render: function (c, v) { return [
        'Strategy: follow the move.',
        '- For each candidate call price_history(token_id, hours=' + v.hours + '). Require a move of at least ' + v.move + ' probability points in one direction with 24h volume above the universe minimum.',
        '- Buy the side that moved, at the ask, only if the spread is 3 points or less.',
        '- Exit when the price gives back half of the move measured from entry, or at the risk rules below.' ]; } },
    { id: 'mm', group: 'strategies', label: 'Two-sided quotes', tag: 'market making', warn: true,
      blurb: 'Rest a bid on YES and a bid on NO below the mid and collect the spread. Read the warning.',
      params: [{ k: 'edge', label: 'below mid', def: 2, unit: 'pts' }, { k: 'shares', label: 'size', def: 20, unit: 'sh' }],
      render: function (c, v) { return [
        'Strategy: two-sided quotes.',
        '- Pick liquid markets (spread of 2 points or less, 24h volume above the minimum). Read get_orderbook.',
        '- Rest a BUY on YES at (mid - ' + v.edge + ' points) and a BUY on NO at ((1 - mid) - ' + v.edge + ' points), ' + v.shares + ' shares each, with post_only=True so neither order crosses.',
        '- When both sides fill you hold a YES and a NO share pair worth exactly $1 at resolution. On a self-hosted server with a relayer key you can merge_positions to get the dollar back now; on the hosted server just hold.',
        '- Warning written by the maintainer: our own market-making engine lost about 4.5 cents per share on markout live at this size, after looking fine on paper. Paper fills here assume no queue and no adverse selection. Treat paper results from this fragment as an upper bound and never take it live without the merge path and small size.' ]; } },

    // ------------------------------ risk rules ------------------------------
    { id: 'stoploss', group: 'risk', label: 'Stop loss', tag: 'review rule', params: [{ k: 'pct', label: 'below entry', def: 25, unit: '%' }],
      blurb: 'Checked at each review, not resting on the book.',
      render: function (c, v) { return ['- Stop loss: at each review, if a position\'s mark is ' + v.pct + '% or more below its average entry price, sell the whole position at the current bid with place_order (side SELL, price = best bid). Polymarket has no stop orders, so this only triggers when you review.']; } },
    { id: 'takeprofit', group: 'risk', label: 'Take profit', tag: 'review rule', params: [{ k: 'pct', label: 'above entry', def: 40, unit: '%' }],
      render: function (c, v) { return ['- Take profit: at each review, if a position\'s mark is ' + v.pct + '% or more above its average entry, sell it at the bid.']; } },
    { id: 'daily', group: 'risk', label: 'Daily loss limit', tag: 'kill switch', params: [{ k: 'usd', label: 'stop at', def: 50, unit: '$', pre: true }],
      render: function (c, v) { return ['- Daily loss limit: if realized plus unrealized P&L for today (paper_positions) is worse than -$' + v.usd + ', place no new orders for the rest of the day and say so.']; } },
    { id: 'noadd', group: 'risk', label: 'Never add to a loser', tag: 'discipline',
      render: function () { return ['- Never add to a position whose mark is below its average entry.']; } },
    { id: 'expo', group: 'risk', label: 'Max exposure per market', tag: 'cap', params: [{ k: 'usd', label: 'cost ≤', def: 50, unit: '$', pre: true }],
      render: function (c, v) { return ['- Max exposure per market: $' + v.usd + ' of cost across all positions in the same market.']; } },

    // ------------------------------- hygiene --------------------------------
    { id: 'dispute', group: 'hygiene', label: 'Skip dispute-prone resolutions', tag: 'resolution', params: [{ k: 'score', label: 'score ≤', def: 30 }],
      render: function (c, v) { return ['- Before any order call resolution_criteria(venue="polymarket", market_id=<market slug or id>) and dispute_risk(<market slug or id>); both take the market, not the token id (get_market gives the slug). Skip it if no resolution source is named or the score is above ' + v.score + '.']; } },
    { id: 'liquidity', group: 'hygiene', label: 'Refuse bad fills', tag: 'execution', params: [{ k: 'slip', label: 'slippage ≤', def: 2, unit: '%' }],
      render: function (c, v) { return ['- Call quote_cost for the exact size before ordering. Skip if the walked average price is more than ' + v.slip + '% worse than the best price, or if the book cannot fill the size inside the limit.']; } },
    { id: 'watch', group: 'hygiene', label: 'Watch the book first', tag: 'execution', params: [{ k: 'sec', label: 'for', def: 20, unit: 's' }],
      render: function (c, v) { return ['- After choosing a market, call watch_book(token_id, seconds=' + v.sec + ') and only proceed if the book did not move against the plan while you watched.']; } },

    // -------------------------------- extras --------------------------------
    { id: 'report', group: 'extras', label: 'Report as a table', tag: 'output', checked: true,
      render: function () { return [
        'Reporting: when the pass is done, show one table with a row per decision: market, side, price, size, check_order verdict, what happened (paper fill, resting, skipped and why). Then paper_positions as cash, equity, realized and unrealized P&L. If nothing qualified, say so plainly instead of forcing a trade.' ]; } },
    { id: 'arena', group: 'extras', label: 'Enter the paper arena', tag: 'hosted only', params: [{ k: 'name', label: 'as', def: 'my-agent', text: true }],
      render: function (c, v) { return ['Arena: if arena_status shows this account is not registered, call arena_register(name="' + (v.name || 'my-agent') + '", strategy="<one line describing this prompt>") once, so the paper ledger appears on oddsrail.app/arena.']; } }
  ];
  var GROUPS = ['strategies', 'risk', 'hygiene', 'extras'];

  var PRESETS = {
    fade:   { topic: 'bitcoin', on: { fade: 1, stoploss: 1, daily: 1, dispute: 1, liquidity: 1, report: 1 } },
    settle: { topic: 'everything closing this week', minvol: 5000, on: { settle: 1, noadd: 1, expo: 1, dispute: 1, liquidity: 1, report: 1 }, vals: { dispute: { score: 20 } } },
    quote:  { topic: 'bitcoin', minvol: 50000, on: { mm: 1, expo: 1, daily: 1, liquidity: 1, watch: 1, report: 1 } },
    view:   { topic: 'my markets', on: { value: 1, stoploss: 1, dispute: 1, liquidity: 1, report: 1 } },
    clear:  { on: { report: 1 } }
  };

  // ------------------------------ form rendering ------------------------------
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function paramHtml(f, p) {
    var inp = '<input type="' + (p.text ? 'text' : 'number') + '" name="' + f.id + '_' + p.k + '" value="' + esc(p.def) + '"' + (p.text ? '' : ' step="any"') + '>';
    var unit = p.unit ? '<em>' + esc(p.unit) + '</em>' : '';
    return '<label class="chip"><span>' + esc(p.label) + '</span>' + (p.pre ? unit + inp : inp + unit) + '</label>';
  }
  function itemHtml(f) {
    var h = '<div class="item' + (f.checked ? ' on' : '') + '" data-id="' + f.id + '">' +
      '<div class="item-h"><label class="sw"><input type="checkbox" name="f_' + f.id + '"' + (f.checked ? ' checked' : '') + '><span></span></label>' +
      '<div class="item-t"><div class="item-n"><b>' + esc(f.label) + '</b>' + (f.tag ? '<span class="tag' + (f.warn ? ' warn' : '') + '">' + esc(f.tag) + '</span>' : '') + '</div>' +
      (f.blurb ? '<small>' + esc(f.blurb) + '</small>' : '') + '</div>';
    if (f.params) h += '<div class="params">' + f.params.map(function (p) { return paramHtml(f, p); }).join('') + '</div>';
    h += '</div>';
    if (f.text) h += '<label class="views"><span>' + esc(f.text.label) + '</span><textarea name="' + f.id + '_' + f.text.k + '" rows="2">' + esc(f.text.def) + '</textarea></label>';
    return h + '</div>';
  }
  GROUPS.forEach(function (g) {
    $(g).innerHTML = FRAGMENTS.filter(function (f) { return f.group === g; }).map(itemHtml).join('');
  });

  var form = $('bform');
  function read() {
    var fd = new FormData(form), c = { mode: fd.get('mode') || 'paper', on: {}, vals: {} };
    ['topic', 'minvol', 'bankroll', 'perorder', 'maxpos', 'closing'].forEach(function (k) { c[k] = fd.get(k); });
    FRAGMENTS.forEach(function (f) {
      c.on[f.id] = fd.get('f_' + f.id) === 'on';
      var v = {};
      (f.params || []).forEach(function (p) { v[p.k] = fd.get(f.id + '_' + p.k); });
      if (f.text) v[f.text.k] = fd.get(f.id + '_' + f.text.k);
      c.vals[f.id] = v;
    });
    return c;
  }
  function apply(c) {
    if (!c) return;
    form.querySelectorAll('[name]').forEach(function (el) {
      var n = el.name;
      if (n === 'mode') { el.checked = (el.value === (c.mode || 'paper')); return; }
      if (n.slice(0, 2) === 'f_') { el.checked = !!(c.on && c.on[n.slice(2)]); return; }
      if (c[n] !== undefined && c[n] !== null) { el.value = c[n]; return; }
      var m = n.match(/^([a-z]+)_(.+)$/);
      if (m && c.vals && c.vals[m[1]] && c.vals[m[1]][m[2]] !== undefined && c.vals[m[1]][m[2]] !== null) el.value = c.vals[m[1]][m[2]];
    });
  }
  function preset(name) {
    var p = PRESETS[name]; if (!p) return;
    var c = read();
    FRAGMENTS.forEach(function (f) { c.on[f.id] = !!(p.on && p.on[f.id]); });
    ['topic', 'minvol', 'bankroll', 'perorder', 'maxpos', 'closing'].forEach(function (k) { if (p[k] !== undefined) c[k] = p[k]; });
    if (p.vals) Object.keys(p.vals).forEach(function (id) { Object.keys(p.vals[id]).forEach(function (k) { c.vals[id][k] = p.vals[id][k]; }); });
    apply(c);
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
    L.push('IF THE ODDSRAIL TOOLS ARE NOT AVAILABLE');
    L.push(paper
      ? '- Do not search for a substitute and do not simulate. Say: "the oddsrail connector is not enabled in this chat" and tell me to add ' + HOST + '/mcp as a custom connector (Settings, Connectors) and switch it on in this chat\'s tools menu.'
      : '- Do not search for a substitute and do not simulate. Say: "the oddsrail server is not connected" and tell me to add it: pip install oddsrail, then claude mcp add --transport stdio oddsrail -- oddsrail.');
    L.push('');
    var strat = FRAGMENTS.filter(function (f) { return f.group === 'strategies' && c.on[f.id]; });
    if (!strat.length) L.push('STRATEGY\n- (no strategy switched on: do a read-only pass, list the qualifying markets with prices and liquidity, and place nothing)\n');
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
  var out = $('out'), status = $('status'), TOOLS = ['server_info', 'find_markets', 'closing_soon', 'get_market', 'get_orderbook', 'price_history', 'overshoot_signal', 'dispute_risk', 'resolution_criteria', 'quote_cost', 'watch_book', 'position_size', 'check_order', 'place_order', 'paper_positions', 'open_orders', 'merge_positions', 'arena_status', 'arena_register'];
  function save(c) { try { localStorage.setItem('oddsrail-build', JSON.stringify(c)); } catch (e) {} }
  function load() { try { return JSON.parse(localStorage.getItem('oddsrail-build') || 'null'); } catch (e) { return null; } }
  function refreshUi(c) {
    form.querySelectorAll('#connect .brows').forEach(function (b) { b.hidden = (b.dataset.mode !== c.mode); });
    FRAGMENTS.forEach(function (f) { var el = form.querySelector('.item[data-id="' + f.id + '"]'); if (el) el.classList.toggle('on', !!c.on[f.id]); });
    GROUPS.forEach(function (g) {
      var n = FRAGMENTS.filter(function (f) { return f.group === g && c.on[f.id]; }).length;
      var t = form.querySelector('.tab[data-tab="' + g + '"] i'); if (t) { t.textContent = n; t.classList.toggle('z', !n); }
    });
    var arena = form.querySelector('.item[data-id="arena"]'); if (arena) arena.classList.toggle('na', c.mode !== 'paper');
  }
  function setLink() { $('open').href = 'https://claude.ai/new?q=' + encodeURIComponent(out.value); }
  function tags() {
    var used = TOOLS.filter(function (t) { return out.value.indexOf(t) !== -1; });
    $('tools').innerHTML = used.map(function (t) { return '<span>' + t + '</span>'; }).join('');
  }
  function regen() {
    var c = read(); save(c); refreshUi(c);
    out.value = compose(c); setLink(); tags();
    var on = FRAGMENTS.filter(function (f) { return c.on[f.id]; }).length;
    status.textContent = on + ' on · ' + out.value.length + ' ch · ' + (c.mode === 'paper' ? 'paper' : 'live');
  }
  function flash(msg) { var keep = status.textContent; status.textContent = msg; setTimeout(function () { status.textContent = keep; }, 1400); }

  apply(load());
  regen();
  probe(0).then(function (live) {
    var url = (live || HOSTS[0]) + '/mcp';
    HOST = live || HOSTS[0];
    var inp = document.querySelector('#connect input[value$="/mcp"]');
    if (inp) { inp.value = url; inp.nextElementSibling.dataset.copy = url; }
    var note = document.getElementById('host-note');
    if (note) note.textContent = !live ? 'The hosted server did not answer from here; check oddsrail.app for status.'
      : live !== HOSTS[0] ? 'Temporary address while mcp.oddsrail.app is being set up; connectors added with it keep working until then.' : '';
    regen();
  });
  form.addEventListener('change', regen);
  form.addEventListener('input', function (e) { if (e.target.type === 'text' || e.target.type === 'number' || e.target.tagName === 'TEXTAREA') regen(); });
  form.querySelectorAll('.tab').forEach(function (t) {
    t.addEventListener('click', function () {
      form.querySelectorAll('.tab').forEach(function (x) { x.classList.toggle('on', x === t); });
      form.querySelectorAll('.pane').forEach(function (p) { p.classList.toggle('on', p.dataset.pane === t.dataset.tab); });
    });
  });
  form.querySelectorAll('[data-preset]').forEach(function (b) {
    b.addEventListener('click', function () { preset(b.dataset.preset); regen(); form.querySelector('.tab[data-tab="strategies"]').click(); });
  });
  $('regen').addEventListener('click', regen);
  $('copy').addEventListener('click', function () { navigator.clipboard.writeText(out.value).then(function () { flash('copied'); }); });
  $('copy2').addEventListener('click', function () { navigator.clipboard.writeText(out.value).then(function () { $('copy2').textContent = 'Copied'; setTimeout(function () { $('copy2').textContent = 'Copy prompt'; }, 1400); }); });
  form.querySelectorAll('[data-copy]').forEach(function (b) {
    b.addEventListener('click', function () { navigator.clipboard.writeText(b.dataset.copy).then(function () { b.textContent = 'copied'; setTimeout(function () { b.textContent = 'copy'; }, 1400); }); });
  });
  $('copycfg').addEventListener('click', function () { navigator.clipboard.writeText(JSON.stringify(read(), null, 1)).then(function () { flash('config copied'); }); });
  out.addEventListener('input', function () { setLink(); tags(); status.textContent = out.value.length + ' ch · edited'; });
})();
