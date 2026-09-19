'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {publicHoldings} = require('../site/shell.js');

const ROOT = path.join(__dirname, '..');
const SOURCE = fs.readFileSync(path.join(ROOT, 'site/portfolio.js'), 'utf8');
const HTML = fs.readFileSync(path.join(ROOT, 'site/portfolio.html'), 'utf8');
const A = '0x' + 'a'.repeat(40);
const B = '0x' + 'b'.repeat(40);
const flush = () => new Promise(resolve => setImmediate(resolve));

// An isolated DOM fixture exercises the real rendering and click handlers.
// It never connects a provider, sends an API request, or uses browser storage.
class Node {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.listeners = new Map();
    this.attributes = {}; this.dataset = {}; this.hidden = false;
    this.classList = {add: () => {}};
  }
  set textContent(value) { this.content = String(value); this.children = []; }
  get textContent() { return (this.content || '') + this.children.map(node => node.textContent).join(''); }
  get firstChild() { return this.children[0]; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.content = ''; this.children = nodes; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(event, handler) { this.listeners.set(event, handler); }
  click() { if (!this.disabled) this.listeners.get('click')?.({}); }
  focus() {}
}

function state(accountStatus = 'unresolved', overrides = {}) {
  return {
    address:A, status:'connected', authenticated:true, portfolioStatus:'ready',
    portfolio:{address:A,account:{status:accountStatus,trading_address:accountStatus === 'resolved' ? B : null},
      summary:{cash_usd:null,holdings_value_usd:null,portfolio_value_usd:null},
      positions:{status:'ok',items:[]},history:{status:'ok',items:[]}},
    ...overrides
  };
}

function setup(initial) {
  const nodes = new Map([...HTML.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, new Node()]));
  const tabs = ['positions', 'agents', 'history'].map(name => {
    const node = nodes.get('tab-' + name); node.dataset.portfolioTab = name; return node;
  });
  let current = initial, subscriber;
  const h = {nodes,refreshes:0,connections:0,refresh:async () => {}};
  const wallet = {
    getState:() => current,
    subscribe(fn) { subscriber = fn; fn(current); },
    async refresh() { h.refreshes++; await h.refresh(); },
    async connect() { h.connections++; }
  };
  h.emit = next => { current = next; subscriber(next); };
  const window = {
    OddsRailWallet:wallet,location:{href:'https://fixture.invalid/portfolio',search:'',hash:''},
    history:{state:null,replaceState() {}},addEventListener() {}
  };
  vm.runInNewContext(SOURCE, {
    window, document:{getElementById:id => {
      assert(nodes.has(id), 'Missing HTML element: ' + id); return nodes.get(id);
    },createElement:tag => new Node(tag),querySelectorAll:() => tabs},
    localStorage:{getItem:() => null},URL,URLSearchParams,Intl,Date,Number,console
  });
  return h;
}

function unknownBalances(h) {
  assert.equal(h.nodes.get('portfolio-holdings').textContent, '—', 'An unresolved holdings value must not become zero');
  assert.equal(h.nodes.has('portfolio-total'), false);
  assert.equal(h.nodes.has('portfolio-cash'), false);
}

test('a signed-in wallet without a public account gets a setup path, with unknown balances', () => {
  const h = setup(state());
  assert.equal(h.nodes.get('portfolio-setup').hidden, false);
  assert.equal(h.nodes.get('portfolio-account-label').textContent, 'Wallet ownership verified');
  assert.equal(h.nodes.get('portfolio-setup-address').textContent, '0xaaaa…aaaa');
  assert.equal(h.nodes.get('portfolio-setup-address').title, A);
  assert.match(h.nodes.get('portfolio-scope-note').textContent, /not been identified yet/);
  assert.equal(h.nodes.get('portfolio-setup-check').disabled, false);
  unknownBalances(h);
});

test('sign-in alone or a stale account response never suggests setup for another wallet', () => {
  for (const s of [
    state('unresolved', {authenticated:false}),
    state('unresolved', {status:'disconnected'}),
    state('unresolved', {address:B}),
    state('unresolved', {status:'signing'}),
    state('unresolved', {status:'verifying'})
  ]) {
    const h = setup(s);
    assert.equal(h.nodes.get('portfolio-setup').hidden, true);
    unknownBalances(h);
  }
});

test('transport and profile errors offer a retry without treating the account as missing', () => {
  for (const s of [
    state('error'),
    state('unresolved', {portfolioStatus:'error',portfolioError:'Account data is unavailable.'}),
    state('resolved', {portfolio:{address:A,account:{status:'resolved',trading_address:'invalid'}}})
  ]) {
    const h = setup(s);
    assert.equal(h.nodes.get('portfolio-setup').hidden, true);
    assert.match(h.nodes.get('positions-state').textContent, /Refresh to try again/);
    assert.equal(h.nodes.get('portfolio-refresh').disabled, false);
    unknownBalances(h);
  }
});

test('setup disappears during lookup and on a resolved account without fabricating cash', () => {
  const h = setup(state());
  h.emit(state('unresolved', {portfolioStatus:'loading',portfolio:null}));
  assert.equal(h.nodes.get('portfolio-setup').hidden, true);
  assert.equal(h.nodes.get('portfolio-setup-check').disabled, true);
  assert.match(h.nodes.get('positions-state').textContent, /Loading positions/);
  h.emit(state('resolved'));
  assert.equal(h.nodes.get('portfolio-setup').hidden, true);
  assert.match(h.nodes.get('positions-state').textContent, /No positions reported/);
  unknownBalances(h);
});

test('Check again reuses the authenticated wallet refresh, without reconnecting', async () => {
  const h = setup(state());
  h.refresh = async () => {
    h.emit(state('unresolved', {portfolioStatus:'loading',portfolio:null}));
    await flush();
    h.emit(state('resolved'));
  };
  h.nodes.get('portfolio-setup-check').click();
  assert.equal(h.refreshes, 1);
  assert.equal(h.nodes.get('portfolio-setup-check').disabled, true);
  h.nodes.get('portfolio-setup-check').click();
  await flush();
  assert.equal(h.refreshes, 1);
  assert.equal(h.connections, 0);
  assert.equal(h.nodes.get('portfolio-setup').hidden, true);
});

test('a failed Check again shows the error while preserving unknown balances', async () => {
  const h = setup(state());
  h.refresh = async () => { throw new Error('The session expired. Connect your wallet again.'); };
  h.nodes.get('portfolio-setup-check').click();
  await flush();
  assert.match(h.nodes.get('portfolio-action-status').textContent, /session expired/);
  unknownBalances(h);
});

test('wallet change and sign-out clear the prior setup identity', () => {
  const h = setup(state());
  h.emit(state('unresolved', {address:B,portfolioStatus:'loading',portfolio:null}));
  assert.equal(h.nodes.get('portfolio-setup').hidden, true);
  assert.equal(h.nodes.get('portfolio-setup-address').title, B);
  h.emit({status:'disconnected',authenticated:false});
  assert.equal(h.nodes.get('portfolio-setup').hidden, true);
  assert.equal(h.nodes.get('portfolio-setup-address').textContent, '');
  assert.equal(h.nodes.get('portfolio-setup-check').disabled, true);
  unknownBalances(h);
});

test('external setup uses Polymarket in a separate tab and explains the same-wallet boundary', () => {
  const card = HTML.match(/<section id="portfolio-setup"[\s\S]*?<\/section>/)[0];
  assert.match(card, /href="https:\/\/polymarket\.com" target="_blank" rel="noopener noreferrer"/);
  assert.match(card, /same wallet/);
  assert.match(card, /doesn't authorize trades/);
  assert.match(card, /discovery can be delayed or incomplete/);
  assert.doesNotMatch(card, /href="[^"]*deposit/);
});

test('public account summaries show holdings alone and explicitly exclude cash', () => {
  const s = state('resolved');
  s.portfolio.summary = {cash_usd:400,portfolio_value_usd:450,holdings_value_usd:50};
  const h = setup(s);
  assert.equal(h.nodes.get('portfolio-holdings').textContent, '$50.00');
  assert.match(h.nodes.get('portfolio-holdings-note').textContent, /Excludes cash/);
  assert.equal(h.nodes.has('portfolio-cash'), false);
  assert.equal(h.nodes.has('portfolio-total'), false);
  assert.equal(publicHoldings(s), 50);
});

test('header holdings cannot fall back to cash or a cash-inclusive public total', () => {
  const s = state('resolved');
  s.portfolio.summary = {cash_usd:400,portfolio_value_usd:450};
  assert.equal(publicHoldings(s), null);
  const shell = fs.readFileSync(path.join(ROOT, 'site/shell.js'), 'utf8');
  assert.doesNotMatch(shell, /id="shell-(?:cash|portfolio)-value"/);
  assert.match(shell, /<span>Holdings<\/span>/);
});

test('header holdings require a matching authenticated and resolved account snapshot', () => {
  for (const change of [
    s => { s.authenticated = false; },
    s => { s.status = 'disconnected'; },
    s => { s.address = B; },
    s => { s.portfolioStatus = 'loading'; },
    s => { s.portfolioStatus = 'error'; },
    s => { s.portfolio.account.status = 'unresolved'; },
    s => { s.portfolio.account.trading_address = 'invalid'; },
    s => { s.portfolio.address = null; }
  ]) {
    const s = state('resolved'); s.portfolio.summary.holdings_value_usd = 50;
    change(s); assert.equal(publicHoldings(s), null);
  }
});

test('reported zero holdings display as zero while invalid or negative values stay unknown', () => {
  for (const value of [null,undefined,'50',NaN,Infinity,-1]) {
    const s = state('resolved'); s.portfolio.summary.holdings_value_usd = value;
    assert.equal(publicHoldings(s), null);
    unknownBalances(setup(s));
  }
  const s = state('resolved'); s.portfolio.summary.holdings_value_usd = 0;
  assert.equal(publicHoldings(s), 0);
  assert.equal(setup(s).nodes.get('portfolio-holdings').textContent, '$0.00');
});
