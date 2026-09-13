'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {createWallet} = require('../site/wallet.js');

const A = '0x' + 'a'.repeat(40);
const B = '0x' + 'b'.repeat(40);
const KNOWN = 'oddsrail.wallet.connected';
const flush = () => new Promise(resolve => setImmediate(resolve));

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

function makeProvider() {
  const listeners = new Map(), requests = [];
  return {
    requests,
    request(args) { const result = deferred(); requests.push({...args, ...result}); return result.promise; },
    on(event, listener) { if (!listeners.has(event)) listeners.set(event, new Set()); listeners.get(event).add(listener); },
    removeListener(event, listener) { listeners.get(event)?.delete(listener); },
    emit(event, payload) { for (const listener of [...(listeners.get(event) || [])]) listener(payload); },
    listenerCount() { return [...listeners.values()].reduce((sum, items) => sum + items.size, 0); }
  };
}

function setup(t, values = {}) {
  const storage = new Map(Object.entries(values));
  const requests = [], timers = new Map();
  let timerId = 0, currentProvider = makeProvider();
  const env = {
    provider: () => currentProvider,
    storage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    fetch(url, options) { const result = deferred(); requests.push({url, options, ...result}); return result.promise; },
    api: 'https://public.example',
    setTimeout(fn, ms) { timers.set(++timerId, {fn, ms}); return timerId; },
    clearTimeout(id) { timers.delete(id); }
  };
  const wallet = createWallet(env);
  t.after(() => wallet.disconnect());
  return {wallet, env, storage, requests, timers, get provider() { return currentProvider; }, setProvider(next) { currentProvider = next; }};
}

async function connect(h, address = A) {
  const pending = h.wallet.connect();
  await flush();
  h.provider.requests.at(-1).resolve([address]);
  assert.equal(await pending, true);
}

async function portfolio(h, index, address, extra = {}) {
  h.requests[index].resolve({ok: true, json: async () => ({ok: true, address, account: {}, summary: {}, ...extra})});
  await flush();
}

test('fresh or invalid stored connection never prompts or reads exposed accounts', async t => {
  for (const saved of [undefined, 'false', 'A', '1']) {
    const h = setup(t, saved === undefined ? {} : {[KNOWN]: saved});
    assert.equal(await h.wallet.restore(), false);
    assert.equal(h.provider.requests.length, 0);
    h.provider.emit('accountsChanged', [A]);
    assert.equal(h.wallet.getState().address, '');
    assert.equal(h.requests.length, 0);
  }
});

test('known connection restores with passive eth_accounts and current address', async t => {
  const h = setup(t, {[KNOWN]: 'true'});
  const pending = h.wallet.restore();
  await flush();
  assert.deepEqual(h.provider.requests.map(request => request.method), ['eth_accounts']);
  h.provider.requests[0].resolve([B]);
  assert.equal(await pending, true);
  assert.equal(h.wallet.getState().address, B);
  assert.equal(h.wallet.getState().status, 'connected');
  assert.equal(h.storage.get(KNOWN), 'true');
  assert.equal(h.requests[0].url, 'https://public.example/portfolio?address=' + B);
  assert.equal(h.requests[0].options.credentials, 'omit');
  assert.equal(h.requests[0].options.cache, 'no-store');
});

test('revoked or failed passive restoration forgets the known connection without prompting', async t => {
  for (const reject of [false, true]) {
    const h = setup(t, {[KNOWN]: 'true'});
    const pending = h.wallet.restore();
    await flush();
    if (reject) h.provider.requests[0].reject({code: 4100});
    else h.provider.requests[0].resolve([]);
    assert.equal(await pending, false);
    assert.equal(h.storage.has(KNOWN), false);
    assert.equal(h.wallet.getState().address, '');
    assert.equal(h.wallet.getState().error, '');
    assert.equal(h.requests.length, 0);
    assert.deepEqual(h.provider.requests.map(request => request.method), ['eth_accounts']);
  }
});

test('repeated Connect shares one account-access request and never asks for a signature', async t => {
  const h = setup(t);
  const first = h.wallet.connect(), second = h.wallet.connect();
  assert.equal(first, second);
  await flush();
  assert.deepEqual(h.provider.requests.map(request => request.method), ['eth_requestAccounts']);
  h.provider.requests[0].resolve([A]);
  assert.equal(await first, true);
  assert.equal(await h.wallet.connect(), true);
  assert.equal(h.provider.requests.length, 1);
  assert.equal(h.storage.get(KNOWN), 'true');
});

test('cancelled connection clears its pending state and a later attempt can succeed', async t => {
  const h = setup(t);
  const pending = h.wallet.connect();
  await flush();
  h.provider.requests[0].reject({code: 4001});
  assert.equal(await pending, false);
  assert.equal(h.wallet.getState().status, 'disconnected');
  assert.match(h.wallet.getState().error, /cancelled/i);
  assert.equal(h.provider.listenerCount(), 0);
  assert.equal(h.storage.has(KNOWN), false);
  await connect(h);
  assert.equal(h.wallet.getState().error, '');
});

test('synchronous provider request errors recover without an unhandled promise', async t => {
  const h = setup(t);
  h.provider.request = () => { throw new Error('provider failed synchronously'); };
  assert.equal(await h.wallet.connect(), false);
  assert.equal(h.wallet.getState().status, 'disconnected');
  assert.equal(h.provider.listenerCount(), 0);
  h.setProvider(makeProvider());
  await connect(h);
});

test('provider listener attachment errors and a missing provider are recoverable', async t => {
  const h = setup(t);
  h.provider.on = () => { throw new Error('cannot subscribe'); };
  assert.equal(await h.wallet.connect(), false);
  assert.equal(h.wallet.getState().status, 'disconnected');
  h.setProvider(null);
  assert.equal(await h.wallet.connect(), false);
  assert.match(h.wallet.getState().error, /wallet browser/i);
  h.setProvider(makeProvider());
  await connect(h);
});

test('a newer account event wins over an older account-request response', async t => {
  const h = setup(t);
  const pending = h.wallet.connect();
  await flush();
  h.provider.emit('accountsChanged', [B]);
  h.provider.requests[0].resolve([A]);
  assert.equal(await pending, true);
  assert.equal(h.wallet.getState().address, B);
  assert.equal(h.requests.length, 1);
  assert.match(h.requests[0].url, new RegExp(B + '$'));
});

test('disconnect invalidates a pending connection and removes provider listeners', async t => {
  const h = setup(t);
  const oldProvider = h.provider;
  const pending = h.wallet.connect();
  await flush();
  h.wallet.disconnect();
  oldProvider.emit('accountsChanged', [B]);
  oldProvider.requests[0].resolve([A]);
  assert.equal(await pending, false);
  assert.equal(oldProvider.listenerCount(), 0);
  assert.equal(h.wallet.getState().address, '');
  assert.equal(h.storage.has(KNOWN), false);
  assert.equal(h.requests.length, 0);
});

test('an old connect response cannot replace a new provider connection', async t => {
  const h = setup(t);
  const oldProvider = h.provider;
  const oldPending = h.wallet.connect();
  await flush();
  h.wallet.disconnect();
  h.setProvider(makeProvider());
  await connect(h, B);
  oldProvider.requests[0].resolve([A]);
  oldProvider.emit('accountsChanged', [A]);
  assert.equal(await oldPending, false);
  assert.equal(h.wallet.getState().address, B);
  assert.equal(h.provider.listenerCount(), 3);
});

test('empty and malformed account events clear address, portfolio, and restore permission', async t => {
  for (const accounts of [[], null, A, {}, ['invalid'], ['0x' + '0'.repeat(40)], [null, A]]) {
    const h = setup(t);
    await connect(h);
    await portfolio(h, 0, A);
    h.provider.emit('accountsChanged', accounts);
    const state = h.wallet.getState();
    assert.equal(state.address, '');
    assert.equal(state.status, 'disconnected');
    assert.equal(state.portfolio, null);
    assert.equal(state.portfolioStatus, 'idle');
    assert.equal(h.storage.has(KNOWN), false);
    assert.equal(h.provider.listenerCount(), 0);
  }
});

test('A to B to A portfolio race ignores aborted responses even when addresses match again', async t => {
  const h = setup(t);
  await connect(h, A);
  h.provider.emit('accountsChanged', [B]);
  h.provider.emit('accountsChanged', [A]);
  assert.equal(h.requests.length, 3);
  assert.equal(h.requests[0].options.signal.aborted, true);
  assert.equal(h.requests[1].options.signal.aborted, true);
  assert.equal(h.wallet.getState().portfolio, null);
  await portfolio(h, 2, A, {summary: {value: 30}});
  await portfolio(h, 1, B, {summary: {value: 20}});
  await portfolio(h, 0, A, {summary: {value: 10}});
  assert.equal(h.wallet.getState().address, A);
  assert.equal(h.wallet.getState().portfolio.summary.value, 30);
  assert.equal(h.wallet.getState().portfolioStatus, 'ready');
  assert.equal(h.timers.size, 0);
});

test('account switch clears an already displayed portfolio before the next load completes', async t => {
  const h = setup(t);
  await connect(h, A);
  await portfolio(h, 0, A, {summary: {value: 10}});
  h.provider.emit('accountsChanged', [B]);
  const state = h.wallet.getState();
  assert.equal(state.address, B);
  assert.equal(state.portfolio, null);
  assert.equal(state.portfolioStatus, 'loading');
});

test('disconnect during a portfolio request keeps late success or failure from reconnecting', async t => {
  for (const reject of [false, true]) {
    const h = setup(t);
    await connect(h);
    h.wallet.disconnect();
    assert.equal(h.requests[0].options.signal.aborted, true);
    if (reject) { h.requests[0].reject(new Error('late network failure')); await flush(); }
    else await portfolio(h, 0, A);
    const state = h.wallet.getState();
    assert.equal(state.status, 'disconnected');
    assert.equal(state.address, '');
    assert.equal(state.portfolio, null);
    assert.equal(state.portfolioStatus, 'idle');
    assert.equal(state.portfolioError, '');
  }
});

test('old refresh failure cannot overwrite a newer successful portfolio', async t => {
  const h = setup(t);
  await connect(h);
  const refreshed = h.wallet.refresh();
  assert.equal(h.requests[0].options.signal.aborted, true);
  await portfolio(h, 1, A, {summary: {value: 42}});
  await refreshed;
  h.requests[0].reject(new Error('old failed request'));
  await flush();
  assert.equal(h.wallet.getState().portfolioStatus, 'ready');
  assert.equal(h.wallet.getState().portfolio.summary.value, 42);
});

test('portfolio errors preserve connection and a refresh recovers', async t => {
  const h = setup(t);
  await connect(h);
  h.requests[0].reject(new Error('network unavailable'));
  await flush();
  assert.equal(h.wallet.getState().status, 'connected');
  assert.equal(h.wallet.getState().address, A);
  assert.equal(h.wallet.getState().portfolioStatus, 'error');
  assert.equal(h.storage.get(KNOWN), 'true');
  const refreshed = h.wallet.refresh();
  await portfolio(h, 1, A);
  await refreshed;
  assert.equal(h.wallet.getState().portfolioStatus, 'ready');
  assert.equal(h.wallet.getState().portfolioError, '');
});

test('wrong-account and unsuccessful portfolio payloads never render', async t => {
  for (const extra of [{address: B}, {ok: false}, {account: null}, {summary: null}]) {
    const h = setup(t);
    await connect(h);
    await portfolio(h, 0, A, extra);
    assert.equal(h.wallet.getState().portfolio, null);
    assert.equal(h.wallet.getState().portfolioStatus, 'error');
    assert.equal(h.wallet.getState().status, 'connected');
  }
});

test('portfolio response requires an explicit successful API envelope', async t => {
  for (const ok of [undefined, null, 'true']) {
    const h = setup(t);
    await connect(h);
    await portfolio(h, 0, A, {ok});
    assert.equal(h.wallet.getState().portfolio, null);
    assert.equal(h.wallet.getState().portfolioStatus, 'error');
    assert.equal(h.wallet.getState().status, 'connected');
  }
});

test('a timed-out portfolio request cannot render even if the fetch ignores its aborted signal', async t => {
  const h = setup(t);
  await connect(h);
  const timer = [...h.timers.values()][0];
  assert.equal(timer.ms, 25000);
  timer.fn();
  assert.equal(h.requests[0].options.signal.aborted, true);
  await portfolio(h, 0, A);
  assert.equal(h.wallet.getState().portfolio, null);
  assert.equal(h.wallet.getState().portfolioStatus, 'error');
  assert.equal(h.wallet.getState().status, 'connected');
  assert.equal(h.timers.size, 0);
});

test('chain change refreshes only public data and provider disconnect clears it', async t => {
  const h = setup(t);
  await connect(h);
  await portfolio(h, 0, A);
  h.provider.emit('chainChanged', '0x89');
  assert.equal(h.requests.length, 2);
  assert.equal(h.wallet.getState().portfolio, null);
  assert.deepEqual(h.provider.requests.map(request => request.method), ['eth_requestAccounts']);
  h.provider.emit('disconnect', {code: 4900});
  assert.equal(h.wallet.getState().status, 'disconnected');
  assert.equal(h.requests[1].options.signal.aborted, true);
  assert.match(h.wallet.getState().error, /disconnected/i);
});

test('wallet operations leave browser drafts and hosted login records untouched', async t => {
  const drafts = '[{"id":"local-draft","config":{"mode":"draft"}}]';
  const h = setup(t, {'oddsrail-drafts-v2': drafts, 'oddsrail-session': 'separate-hosted-session'});
  await connect(h);
  h.provider.emit('accountsChanged', [B]);
  h.wallet.disconnect();
  assert.deepEqual([...h.storage.entries()], [['oddsrail-drafts-v2', drafts], ['oddsrail-session', 'separate-hosted-session']]);
});

test('blocked storage does not prevent an explicit connection or pretend it was remembered', async t => {
  const h = setup(t);
  h.env.storage = {getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() { throw new Error('blocked'); }};
  assert.equal(await h.wallet.restore(), false);
  await connect(h);
  h.wallet.disconnect();
  assert.equal(h.wallet.getState().status, 'disconnected');
});

test('subscribers receive immediate snapshots and unsubscribe stops later notifications', async t => {
  const h = setup(t);
  const states = [];
  const unsubscribe = h.wallet.subscribe(state => states.push(state));
  assert.equal(states[0].status, 'disconnected');
  states[0].address = B;
  assert.equal(h.wallet.getState().address, '');
  await connect(h);
  assert(states.some(state => state.status === 'connecting'));
  assert(states.some(state => state.address === A));
  unsubscribe();
  const count = states.length;
  h.wallet.disconnect();
  assert.equal(states.length, count);
});
