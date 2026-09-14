(function () {
  'use strict';
  var STRATEGIES = { mm: 'Quote both sides', fade: 'Fade sharp moves', settle: 'High-probability outcomes', value: 'Trade my forecast', momentum: 'Follow momentum' };
  var ADDRESS = /^0x[0-9a-f]{40}$/i;
  function object(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function money(value, signed) {
    if (!finite(value)) return '—';
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: signed ? 'exceptZero' : 'auto' }).format(value);
  }
  function quantity(value) { return finite(value) ? new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(value) : '—'; }
  function price(value) { return finite(value) && value >= 0 && value <= 1 ? new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(value * 100) + '¢' : '—'; }
  function text(value, fallback, max) { return typeof value === 'string' && value.trim() ? value.slice(0, max || 300) : (fallback || '—'); }
  function shortAddress(value) { return typeof value === 'string' && ADDRESS.test(value) ? value.slice(0, 6) + '…' + value.slice(-4) : '—'; }
  function date(value) {
    if (typeof value !== 'string' && !finite(value)) return null;
    var result = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isFinite(result.getTime()) ? result : null;
  }
  function dateLabel(value) {
    var parsed = date(value);
    return parsed ? new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(parsed) : '—';
  }
  function marketURL(value) {
    if (typeof value !== 'string') return null;
    try {
      var url = new URL(value);
      if (url.protocol !== 'https:' || (url.hostname !== 'polymarket.com' && url.hostname !== 'www.polymarket.com') || url.username || url.password || url.port) return null;
      return url.href;
    } catch (_) { return null; }
  }
  function matchingPortfolio(state) {
    if (!object(state) || state.status !== 'connected' || !ADDRESS.test(state.address || '') || state.portfolioStatus !== 'ready') return null;
    var portfolio = state.portfolio;
    if (!object(portfolio) || typeof portfolio.address !== 'string' || portfolio.address.toLowerCase() !== state.address.toLowerCase()) return null;
    return portfolio;
  }
  function validDrafts(raw) {
    if (!Array.isArray(raw)) throw new Error('Saved drafts could not be read.');
    return raw.filter(function (draft) { return object(draft) && typeof draft.id === 'string' && draft.id.length > 0 && draft.id.length <= 200 && object(draft.config) && object(draft.config.on); })
      .sort(function (a, b) { return text(b.updated_at, '').localeCompare(text(a.updated_at, '')); });
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { money: money, price: price, quantity: quantity, dateLabel: dateLabel, marketURL: marketURL, matchingPortfolio: matchingPortfolio, validDrafts: validDrafts };
  if (typeof document === 'undefined') return;
  var $ = function (id) { return document.getElementById(id); };
  var wallet = window.OddsRailWallet;
  function el(tag, content, cls) { var node = document.createElement(tag); if (content !== undefined) node.textContent = content; if (cls) node.className = cls; return node; }
  function set(id, content) { $(id).textContent = content; }
  function empty(id, title, message, kind) {
    var target = $(id);
    target.replaceChildren(el('span', kind === 'loading' ? '◌' : '↗', 'empty-portfolio-symbol'), el('h3', title), el('p', message));
    target.firstChild.setAttribute('aria-hidden', 'true');
    target.className = 'portfolio-empty' + (kind ? ' is-' + kind : '');
    target.hidden = false;
  }
  function marketCell(item) {
    var cell = el('td'), url = marketURL(item.url), title = el(url ? 'a' : 'span', text(item.title, 'Market title unavailable'), 'portfolio-market-title');
    if (url) { title.href = url; title.target = '_blank'; title.rel = 'noopener noreferrer'; }
    cell.appendChild(title);
    return cell;
  }
  function amountCell(value, cls) { return el('td', value, cls); }
  function clearTables() {
    ['positions', 'history'].forEach(function (key) {
      $(key + '-body').replaceChildren(); $(key + '-table-wrap').hidden = true; set(key + '-note', '');
    });
    $('positions-count').hidden = true;
  }
  function renderPositions(data) {
    if (!object(data) || !['ok', 'partial'].includes(data.status) || !Array.isArray(data.items)) {
      empty('positions-state', 'Positions are unavailable.', 'The account was found, but its positions could not be verified. Refresh to try again.', 'error');
      return;
    }
    var items = data.items.filter(object);
    var partial = data.status === 'partial' || data.truncated === true;
    set('positions-count', items.length + (partial ? '+' : '')); $('positions-count').hidden = !items.length && partial;
    if (!items.length) {
      empty('positions-state', partial ? 'Position data is incomplete.' : 'No positions reported.', partial ? 'No displayable positions are available in this partial response. This does not establish that the account has no holdings.' : 'No positions were returned for this Polymarket account. Other wallets and trading accounts are outside this view.');
    } else {
      $('positions-state').hidden = true;
      $('positions-table-wrap').hidden = false;
      items.forEach(function (item) {
        var row = el('tr'), market = marketCell(item), outcome = el('td'), outcomeLabel = el('span', text(item.outcome), 'portfolio-outcome');
        if (typeof item.token_id === 'string') outcomeLabel.title = 'Outcome token: ' + item.token_id;
        outcome.appendChild(outcomeLabel);
        if (item.redeemable === true) market.appendChild(el('small', 'Reported as redeemable', 'portfolio-market-detail'));
        var pnl = el('td', money(item.total_pnl_usd, true), 'portfolio-return');
        if (finite(item.total_pnl_usd) && item.total_pnl_usd !== 0) pnl.classList.add(item.total_pnl_usd > 0 ? 'portfolio-positive' : 'portfolio-negative');
        if (finite(item.percent_pnl)) pnl.appendChild(el('small', (item.percent_pnl > 0 ? '+' : '') + quantity(item.percent_pnl) + '%'));
        row.append(market, outcome, amountCell(quantity(item.size)), amountCell(price(item.avg_price)), amountCell(price(item.current_price)), amountCell(money(item.current_value_usd)), pnl);
        $('positions-body').appendChild(row);
      });
    }
    set('positions-note', (partial ? 'Position data is incomplete; this list may not include every holding. ' : '') + (typeof data.message === 'string' && data.message ? text(data.message, '', 400) + ' ' : '') + 'Holdings value can also include unresolved combos at cost, beyond these single-market rows. Returns combine reported realized and unrealized P&L for each outcome. They are not an OddsRail agent performance record.');
  }
  function renderHistory(data) {
    if (!object(data) || !['ok', 'partial'].includes(data.status) || !Array.isArray(data.items)) {
      empty('history-state', 'Activity is unavailable.', 'The account was found, but its public activity could not be verified. Refresh to try again.', 'error');
      return;
    }
    var items = data.items.filter(object), partial = data.status === 'partial' || data.truncated === true;
    if (!items.length) {
      empty('history-state', partial ? 'Activity data is incomplete.' : 'No activity reported.', partial ? 'No displayable activity is available in this partial response. This does not establish that the account has no activity.' : 'No recent public activity was returned for this Polymarket account.');
    } else {
      $('history-state').hidden = true; $('history-table-wrap').hidden = false;
      items.forEach(function (item) {
        var row = el('tr'), market = marketCell(item);
        if (typeof item.outcome === 'string' && item.outcome.trim()) market.appendChild(el('small', text(item.outcome), 'portfolio-market-detail'));
        if (item.is_combo === true) market.appendChild(el('small', 'Combo activity', 'portfolio-market-detail'));
        var kind = text(item.type, 'Activity', 40).toLowerCase().replace(/_/g, ' ');
        kind = kind.charAt(0).toUpperCase() + kind.slice(1);
        if (item.side === 'BUY' || item.side === 'SELL') kind += ' · ' + (item.side === 'BUY' ? 'Buy' : 'Sell');
        var time = el('td', dateLabel(item.timestamp), 'portfolio-date');
        var parsed = date(item.timestamp); if (parsed) time.title = parsed.toLocaleString();
        row.append(el('td', kind, 'portfolio-activity'), market, amountCell(money(item.usdc_size)), amountCell(quantity(item.size)), amountCell(price(item.price)), time);
        $('history-body').appendChild(row);
      });
    }
    set('history-note', (partial ? 'Showing an incomplete set of recent activity. ' : '') + (typeof data.message === 'string' && data.message ? text(data.message, '', 400) + ' ' : '') + 'Public activity is not a complete cash ledger and may include actions made outside OddsRail. An activity amount is not an available balance.');
  }
  function render(state) {
    state = object(state) ? state : {};
    set('portfolio-action-status', typeof state.error === 'string' && state.error ? text(state.error, '', 300) : '');
    var authLabels = {connecting:'Connecting…',signing:'Sign in your wallet…',verifying:'Verifying…'};
    var busy = Boolean(authLabels[state.status]);
    var connected = state.status === 'connected' && state.authenticated === true && ADDRESS.test(state.address || ''), loading = busy || (connected && state.portfolioStatus === 'loading');
    $('portfolio-connect').hidden = connected;
    $('portfolio-connect').disabled = !wallet || busy;
    $('portfolio-connect').textContent = authLabels[state.status] || (state.signOutPending ? 'Retry sign-out' : 'Connect wallet ↗');
    $('portfolio-refresh').hidden = !connected;
    $('portfolio-refresh').disabled = loading;
    $('portfolio-setup').hidden = true;
    $('portfolio-setup-check').disabled = !connected || loading;
    set('portfolio-setup-address', connected ? shortAddress(state.address) : '');
    $('portfolio-setup-address').title = connected ? state.address : '';
    $('portfolio-address').hidden = !connected;
    set('portfolio-address', shortAddress(state.address)); $('portfolio-address').title = connected ? state.address : '';
    $('account-indicator').className = 'account-indicator' + (connected ? ' connected' : '');
    set('portfolio-account-label', connected ? 'Wallet ownership verified' : (busy ? 'Waiting for wallet sign-in' : 'Not signed in'));
    set('portfolio-updated', '');
    set('portfolio-holdings', '—');
    set('portfolio-holdings-note', connected ? 'Reported positions only. Excludes cash.' : 'Connect to view reported positions value. Excludes cash.');
    set('portfolio-holdings-tag', loading ? 'Loading' : (connected ? 'Not verified' : 'Not connected'));
    clearTables();
    if (!connected) {
      var connecting = busy;
      set('portfolio-scope-note', connecting ? 'Approve the connection, then sign the OddsRail sign-in message in your wallet. This does not authorize trading.' : 'Connect and sign in with your wallet to view its public Polymarket account. Trading permission is separate.');
      empty('positions-state', connecting ? 'Connecting your wallet…' : 'See what you hold.', connecting ? 'Waiting for your wallet connection.' : 'Connect a wallet to find its public Polymarket positions.', connecting ? 'loading' : '');
      empty('history-state', connecting ? 'Connecting your wallet…' : 'Your activity, together.', connecting ? 'Waiting for your wallet connection.' : 'Connect a wallet to look up its public account activity.', connecting ? 'loading' : '');
      if (typeof state.error === 'string' && state.error) set('portfolio-action-status', text(state.error, '', 300));
      return;
    }
    if (loading || state.portfolioStatus === 'idle') {
      set('portfolio-scope-note', 'Looking up the Polymarket account linked to this wallet. Values stay blank until the account and data can be verified.');
      empty('positions-state', 'Loading positions…', 'Resolving the trading account and requesting its public holdings.', 'loading');
      empty('history-state', 'Loading activity…', 'Requesting public activity for the resolved account.', 'loading');
      return;
    }
    var portfolio = matchingPortfolio(state);
    if (state.portfolioStatus === 'error' || !portfolio) {
      set('portfolio-holdings-tag', 'Unavailable');
      set('portfolio-scope-note', text(state.portfolioError, 'Account data could not be verified. Refresh to try again.', 400));
      empty('positions-state', 'Could not load this account.', 'Account data is temporarily unavailable. Refresh to try again.', 'error');
      empty('history-state', 'Could not load this account.', 'Account data is temporarily unavailable. Refresh to try again.', 'error');
      return;
    }
    var account = object(portfolio.account) ? portfolio.account : {};
    if (account.status !== 'resolved' || !ADDRESS.test(account.trading_address || '')) {
      var unresolved = account.status === 'unresolved';
      $('portfolio-setup').hidden = !unresolved;
      set('portfolio-holdings-tag', unresolved ? 'Account not found' : 'Unavailable');
      set('portfolio-scope-note', unresolved ? 'Wallet ownership is verified. A linked Polymarket trading account has not been identified yet; this does not establish that its balances or positions are zero.' : 'Your wallet sign-in is verified, but the Polymarket account lookup is temporarily unavailable. Refresh to try again.');
      empty('positions-state', unresolved ? 'Find your trading account first.' : 'Account lookup unavailable.', unresolved ? 'Complete the account setup above, or check again if you already use this wallet on Polymarket. Positions appear after the linked account is found.' : 'We could not verify the account linked to this wallet. Refresh to try again.', unresolved ? '' : 'error');
      empty('history-state', unresolved ? 'Find your trading account first.' : 'Account lookup unavailable.', 'Public activity will appear after the linked trading account is found.', unresolved ? '' : 'error');
      return;
    }
    var summary = object(portfolio.summary) ? portfolio.summary : {};
    var holdings = finite(summary.holdings_value_usd) && summary.holdings_value_usd >= 0 ? summary.holdings_value_usd : null;
    set('portfolio-holdings', money(holdings));
    set('portfolio-holdings-tag', holdings !== null ? 'Public snapshot' : 'Unavailable');
    set('portfolio-holdings-note', holdings !== null ? (summary.valuation_scope === 'single_market_marks_plus_unresolved_combo_cost' ? 'Market values plus unresolved combo cost. Excludes cash.' : 'Reported position value at current prices. Excludes cash.') : 'Holdings value could not be verified. Excludes cash.');
    set('portfolio-scope-note', 'Public records for ' + account.trading_address + ', returned by the Polymarket profile lookup for the connected address. Other accounts are outside this view. This lookup does not verify account ownership or OddsRail agent attribution.');
    set('portfolio-updated', date(portfolio.as_of) ? 'Updated ' + dateLabel(portfolio.as_of) : 'Snapshot time unavailable');
    renderPositions(portfolio.positions); renderHistory(portfolio.history);
  }
  function renderDrafts() {
    $('portfolio-drafts').replaceChildren(); $('agents-state').hidden = true; $('agents-count').hidden = true;
    try {
      var raw = JSON.parse(localStorage.getItem('oddsrail-drafts-v2') || '[]'), drafts = validDrafts(raw);
      set('agents-count', String(drafts.length)); $('agents-count').hidden = false;
      if (!drafts.length) { empty('agents-state', 'Your next idea starts here.', 'Create a strategy and save a draft. Your drafts stay in this browser.'); return; }
      drafts.slice(0, 100).forEach(function (draft) {
        var card = el('article', undefined, 'panel portfolio-draft'), c = draft.config;
        card.append(el('span', 'Browser draft', 'draft-label'), el('h3', text(draft.name, 'Untitled agent', 100)));
        var strategies = Object.keys(STRATEGIES).filter(function (key) { return c.on[key] === true; }).map(function (key) { return STRATEGIES[key]; });
        card.appendChild(el('p', strategies.join(' · ') || 'No strategy selected', 'muted'));
        var scope = c.market_mode === 'specific' ? (Array.isArray(c.market_ids) ? c.market_ids.length + ' selected outcomes' : 'Market selection needs review') : (Array.isArray(c.topics) ? c.topics.filter(function (topic) { return typeof topic === 'string'; }).join(', ') : '');
        card.appendChild(el('p', text(scope, 'Market scope needs review'), 'muted draft-meta'));
        if (date(draft.updated_at)) card.appendChild(el('p', 'Saved ' + dateLabel(draft.updated_at), 'muted draft-meta'));
        var link = el('a', 'Continue editing ↗', 'pill secondary'); link.href = '/build?draft=' + encodeURIComponent(draft.id); card.appendChild(link); $('portfolio-drafts').appendChild(card);
      });
      if (drafts.length > 100) { empty('agents-state', 'Showing 100 browser drafts.', 'Open My agents to view more saved drafts.'); }
    } catch (_) { empty('agents-state', 'Drafts could not be loaded.', 'Browser storage is unavailable or saved draft data could not be read. Your wallet account data is separate.', 'error'); }
  }
  var tabs = Array.from(document.querySelectorAll('[data-portfolio-tab]'));
  function selectTab(name, focus, updateURL) {
    if (!['positions', 'agents', 'history'].includes(name)) return;
    tabs.forEach(function (tab) {
      var selected = tab.dataset.portfolioTab === name;
      tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
      $('panel-' + tab.dataset.portfolioTab).hidden = !selected;
      if (selected && focus) tab.focus();
    });
    if (name === 'agents') renderDrafts();
    if (updateURL) {
      var url = new URL(window.location.href);
      url.searchParams.set('tab', name);
      if (['#positions', '#agents', '#history'].includes(url.hash)) url.hash = '';
      window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash);
    }
  }
  tabs.forEach(function (tab, index) {
    tab.addEventListener('click', function () { selectTab(tab.dataset.portfolioTab, false, true); });
    tab.addEventListener('keydown', function (event) {
      var next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1;
      if (next >= 0) { event.preventDefault(); selectTab(tabs[next].dataset.portfolioTab, true, true); }
    });
  });
  ['positions-table-wrap', 'history-table-wrap'].forEach(function (id) { $(id).tabIndex = 0; $(id).setAttribute('aria-label', 'Scrollable ' + id.split('-')[0] + ' table'); });
  async function walletAction(action) {
    if (!wallet || typeof wallet[action] !== 'function') { set('portfolio-action-status', 'Wallet connection is unavailable. Reload the page to try again.'); return; }
    set('portfolio-action-status', '');
    try { await wallet[action](); } catch (error) { set('portfolio-action-status', text(error && error.message, 'The wallet request could not be completed.', 300)); }
  }
  $('portfolio-connect').addEventListener('click', function () { walletAction(wallet?.getState().signOutPending ? 'disconnect' : 'connect'); });
  $('portfolio-refresh').addEventListener('click', function () { walletAction('refresh'); });
  $('portfolio-setup-check').addEventListener('click', function () { walletAction('refresh'); });
  try { set('history-timezone', 'Times in ' + Intl.DateTimeFormat().resolvedOptions().timeZone); } catch (_) { set('history-timezone', 'Local times'); }
  renderDrafts();
  if (wallet && typeof wallet.getState === 'function' && typeof wallet.subscribe === 'function') {
    render(wallet.getState()); wallet.subscribe(render);
  } else {
    render({ status: 'disconnected' }); set('portfolio-action-status', 'Wallet connection is unavailable. Reload the page to try again.');
  }
  selectTab(new URLSearchParams(window.location.search).get('tab') || window.location.hash.slice(1), false);
  window.addEventListener('hashchange', function () { selectTab(window.location.hash.slice(1), false); });
  window.addEventListener('storage', function (event) { if (event.key === 'oddsrail-drafts-v2' || event.key === null) renderDrafts(); });
  window.addEventListener('focus', renderDrafts);
}());
