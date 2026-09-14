'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {createActivationChecker, validReport} = require('../site/activation.js');

const A = '0x' + 'a'.repeat(40);
const B = '0x' + 'b'.repeat(40);
const connected = () => ({authenticated:true,status:'connected',address:A,chainId:137});
const disconnected = () => ({authenticated:false,status:'disconnected',address:null,chainId:null});
const flush = () => new Promise(resolve => setImmediate(resolve));
const clone = value => JSON.parse(JSON.stringify(value));
const response = (data, status = 200) => ({ok:status >= 200 && status < 300,status,json:async () => data});
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve=yes; reject=no; });
  return {promise,resolve,reject};
}
function report(owner = A) {
  const details = [
    ['wallet','ready','Wallet sign-in'],
    ['configuration','ready','Strategy and limits'],
    ['trading_account','action_required','Polymarket trading account'],
    ['funding','unavailable','Trading balance'],
    ['permission','unavailable','Trading permission'],
    ['execution','unavailable','Hosted runner'],
  ];
  return {ok:true,address:owner,can_activate:false,checked_at:'2026-09-13T12:00:00Z',
    checks:details.map(([id,status,label]) => ({id,status,label,detail:'Readiness detail.',
      ...(id === 'trading_account' ? {href:'https://polymarket.com/'} : {})})),
    blockers:['trading_account','funding','permission','execution']};
}
function setup(initial = connected()) {
  let walletState={...initial}, connectCalls=0, sessionChecks=0, timerId=0;
  const walletListeners=new Set(), timers=new Map(), calls=[], events=[];
  const h = {
    calls,events,timers,config:{mode:'draft',market_mode:'specific',strategies:['mm'],limits:{max_order_usd:10}},
    validationError:'',fetchHandler:null,connectHandler:null,
    get connectCalls() { return connectCalls; }, get sessionChecks() { return sessionChecks; },
    walletListenerCount:() => walletListeners.size,
    setWallet(next) { walletState={...next}; [...walletListeners].forEach(fn => fn({...walletState})); },
    fireTimeout() {
      const entry=[...timers.entries()].find(([, timer]) => timer.ms === 25000);
      assert(entry,'Expected a bounded activation request'); timers.delete(entry[0]); entry[1].fn();
    },
  };
  const wallet = {
    getState:() => ({...walletState}),
    subscribe(fn) { walletListeners.add(fn); fn({...walletState}); return () => walletListeners.delete(fn); },
    async connect() {
      connectCalls++;
      if (h.connectHandler) return h.connectHandler();
      h.setWallet(connected()); return true;
    },
    async checkSession() { sessionChecks++; return false; },
  };
  h.env = {wallet,api:'https://api.example',
    getConfig:() => h.config,validate:() => h.validationError,
    async fetch(url,options) {
      const call={url,options,body:JSON.parse(options.body)}; calls.push(call);
      return h.fetchHandler ? h.fetchHandler(call) : response(report(walletState.address));
    },
    setTimeout(fn,ms) { timers.set(++timerId,{fn,ms}); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  };
  h.checker=createActivationChecker(h.env);
  h.unsubscribe=h.checker.subscribe(state => events.push(state));
  return h;
}

test('authenticated readiness posts only configuration with the session cookie and cannot activate', async () => {
  const h=setup(); assert.equal(await h.checker.check(),true);
  assert.equal(h.connectCalls,0); assert.equal(h.calls.length,1);
  const {url,options,body}=h.calls[0];
  assert.equal(url,'https://api.example/activation/check');
  assert.equal(options.method,'POST'); assert.equal(options.credentials,'include');
  assert.equal(options.cache,'no-store'); assert.equal(options.headers['Content-Type'],'application/json');
  assert.deepEqual(body,{config:h.config});
  assert.equal(Object.hasOwn(body,'address'),false); assert.equal(Object.hasOwn(body,'owner'),false);
  assert.deepEqual(h.events.map(value=>value.phase),['idle','connecting','checking','blocked']);
  assert.equal(h.checker.getState().report.can_activate,false); assert.equal(h.timers.size,0);
  assert(h.calls.every(call=>new URL(call.url).pathname === '/activation/check'));
});

test('opening the builder is passive; an explicit readiness check can request sign-in', async () => {
  const h=setup(disconnected()); await flush();
  assert.equal(h.connectCalls,0); assert.equal(h.calls.length,0);
  assert.equal(await h.checker.check(),true);
  assert.equal(h.connectCalls,1); assert.equal(h.calls.length,1);
});

test('invalid configuration is shown before any wallet prompt or API request', async () => {
  const h=setup(disconnected()); h.validationError='Choose a market first.';
  assert.equal(await h.checker.check(),false);
  assert.deepEqual(h.checker.getState(),{phase:'error',report:null,error:'Choose a market first.'});
  assert.equal(h.connectCalls,0); assert.equal(h.calls.length,0); assert.equal(h.timers.size,0);
});

test('cancelled sign-in never checks the API and a subsequent attempt can succeed', async () => {
  const h=setup(disconnected()); h.connectHandler=() => false;
  assert.equal(await h.checker.check(),false); assert.equal(h.calls.length,0);
  assert.match(h.checker.getState().error,/sign in/i);
  h.connectHandler=null; assert.equal(await h.checker.check(),true); assert.equal(h.calls.length,1);
});

test('a successful connect return without authenticated wallet state is insufficient', async () => {
  for (const state of [disconnected(),{...connected(),authenticated:false},{...connected(),address:'invalid'}]) {
    const h=setup(state); h.connectHandler=() => true;
    assert.equal(await h.checker.check(),false); assert.equal(h.calls.length,0);
    assert.match(h.checker.getState().error,/could not be verified/i);
  }
});

test('duplicate readiness clicks cannot create concurrent HTTP checks', async () => {
  const h=setup(), waiting=deferred(); h.fetchHandler=() => waiting.promise;
  const first=h.checker.check(); await flush();
  assert.equal(h.checker.getState().phase,'checking');
  assert.equal(await h.checker.check(),false); assert.equal(h.calls.length,1);
  waiting.resolve(response(report())); assert.equal(await first,true); assert.equal(h.timers.size,0);
});

test('duplicate clicks during connection share the existing user prompt', async () => {
  const h=setup(disconnected()), prompt=deferred();
  h.connectHandler=() => prompt.promise;
  const first=h.checker.check(); await flush();
  assert.equal(await h.checker.check(),false); assert.equal(h.connectCalls,1); assert.equal(h.calls.length,0);
  h.setWallet(connected()); prompt.resolve(true); assert.equal(await first,true); assert.equal(h.calls.length,1);
});

test('reports must match the owner and retain blocked permission and execution', () => {
  assert.equal(validReport(report(),A),true);
  assert.equal(validReport(report(A.toUpperCase().replace('0X','0x')),A),true);
  const mutations=[
    value=>{value.ok='true';},value=>{value.can_activate=true;},value=>{delete value.can_activate;},
    value=>{value.address=B;},value=>{value.address='invalid';},
    value=>{value.checks.find(c=>c.id==='wallet').status='action_required';},
    value=>{value.checks.find(c=>c.id==='configuration').status='unavailable';},
    value=>{value.checks.find(c=>c.id==='funding').status='ready';value.blockers=value.blockers.filter(id=>id!=='funding');},
    value=>{value.checks.find(c=>c.id==='permission').status='ready';},
    value=>{value.checks.find(c=>c.id==='execution').status='ready';},
  ];
  for (const mutate of mutations) { const value=report(); mutate(value); assert.equal(validReport(value,A),false); }
});

test('reports require all six unique, bounded, well-formed checks', () => {
  const mutations=[
    value=>{value.checks.pop();},value=>{value.checks.push(clone(value.checks[0]));},
    value=>{value.checks[1]=clone(value.checks[0]);},value=>{value.checks[2].id='unknown';},
    value=>{value.checks[2].status='active';},value=>{value.checks[2]=null;},
    value=>{value.checks[2].label={text:'bad'};},value=>{value.checks[2].label='x'.repeat(101);},
    value=>{value.checks[2].detail=[];},value=>{value.checks[2].detail='x'.repeat(801);},
  ];
  for (const mutate of mutations) { const value=report(); mutate(value); assert.equal(validReport(value,A),false); }
  for (const value of [null,{},[],{...report(),checks:null}]) assert.equal(validReport(value,A),false);
});

test('untrusted report links cannot direct the user outside the exact Polymarket homepage', async () => {
  for (const href of ['https://evil.example','https://polymarket.com.evil.example/','https://polymarket.com@evil.example/','javascript:alert(1)','http://polymarket.com/','https://polymarket.com/transfer']) {
    const value=report(); value.checks[2].href=href;
    const h=setup(); h.fetchHandler=() => response(value);
    assert.equal(await h.checker.check(),false); assert.equal(h.checker.getState().report,null);
    assert.match(h.checker.getState().error,/could not be verified/i);
  }
  for (const href of ['https://polymarket.com','https://polymarket.com/']) {
    const value=report(); value.checks[2].href=href; assert.equal(validReport(value,A),true);
  }
});

test('blocker metadata cannot contradict the six checks or contain arbitrary values', () => {
  for (const blockers of [null,[],['permission'],[{}],['unknown'],
    ['trading_account','funding','permission','execution','execution']]) {
    assert.equal(validReport({...report(),blockers},A),false);
  }
  const resolved=report(); resolved.checks[2].status='ready'; delete resolved.checks[2].href;
  resolved.blockers=['funding','permission','execution'];
  assert.equal(validReport(resolved,A),true);
});

test('editing configuration clears an existing result and discards an in-flight result', async () => {
  const h=setup(); assert.equal(await h.checker.check(),true);
  h.config.limits.max_order_usd=5; h.checker.invalidate();
  assert.deepEqual(h.checker.getState(),{phase:'idle',report:null,error:''});
  const pending=deferred(); h.fetchHandler=() => pending.promise;
  const check=h.checker.check(); await flush();
  assert.equal(h.calls[1].body.config.limits.max_order_usd,5);
  h.config.limits.max_order_usd=3; h.checker.invalidate();
  assert.equal(h.calls[1].options.signal.aborted,true);
  pending.resolve(response(report())); assert.equal(await check,false);
  assert.deepEqual(h.checker.getState(),{phase:'idle',report:null,error:''}); assert.equal(h.timers.size,0);
});

test('changing configuration while signing prevents a subsequent readiness request', async () => {
  const h=setup(disconnected()), prompt=deferred(); h.connectHandler=() => prompt.promise;
  const pending=h.checker.check(); h.checker.invalidate();
  h.setWallet(connected()); prompt.resolve(true);
  assert.equal(await pending,false); assert.equal(h.calls.length,0); assert.equal(h.checker.getState().phase,'idle');
});

test('account changes, chain changes, and expired sign-in invalidate pending and displayed results', async () => {
  for (const next of [{...connected(),address:B},{...connected(),chainId:1},disconnected()]) {
    const h=setup(); assert.equal(await h.checker.check(),true);
    h.setWallet(next); assert.equal(h.checker.getState().report,null);
    h.setWallet(connected()); const responsePending=deferred(); h.fetchHandler=() => responsePending.promise;
    const check=h.checker.check(); await flush(); h.setWallet(next);
    assert.equal(h.calls[1].options.signal.aborted,true);
    responsePending.resolve(response(report())); assert.equal(await check,false);
    assert.equal(h.checker.getState().phase,'idle'); assert.equal(h.checker.getState().report,null);
  }
});

test('portfolio updates for the same signed-in identity do not discard a readiness check', async () => {
  const h=setup(), waiting=deferred(); h.fetchHandler=() => waiting.promise;
  const check=h.checker.check(); await flush();
  h.setWallet({...connected(),portfolioStatus:'ready',portfolio:{ok:true}});
  assert.equal(h.calls[0].options.signal.aborted,false);
  waiting.resolve(response(report())); assert.equal(await check,true);
});

test('expired sessions produce a sign-in action even if the 401 body is malformed', async () => {
  for (const result of [response({ok:false,error:'session_expired',detail:'Expired'},401),
    {ok:false,status:401,json:async()=>{throw new SyntaxError('Unexpected token <');}}]) {
    const h=setup(); h.fetchHandler=() => result;
    assert.equal(await h.checker.check(),false); await flush();
    assert.equal(h.sessionChecks,1); assert.match(h.checker.getState().error,/sign-in expired/i);
    assert.equal(h.checker.getState().report,null);
  }
});

test('server configuration errors use bounded human detail, and malformed bodies have a friendly fallback', async () => {
  const h=setup(); h.fetchHandler=() => response({ok:false,error:'invalid_configuration',detail:'Choose at least one strategy.'},400);
  assert.equal(await h.checker.check(),false); assert.equal(h.checker.getState().error,'Choose at least one strategy.');
  h.fetchHandler=() => response({ok:false,error:'invalid_configuration',detail:'x'.repeat(900)},400);
  assert.equal(await h.checker.check(),false); assert.equal(h.checker.getState().error.length,400);
  for (const result of [response(null,400),response({ok:false,error:{message:'bad'},detail:{}},400),
    {ok:false,status:502,json:async()=>{throw new SyntaxError('Unexpected token < in HTML');}}]) {
    const broken=setup(); broken.fetchHandler=() => result;
    assert.equal(await broken.checker.check(),false); assert.equal(broken.checker.getState().report,null);
    assert.doesNotMatch(broken.checker.getState().error,/Unexpected token|SyntaxError|\[object Object\]/);
    assert.match(broken.checker.getState().error,/try again|could not be checked|unavailable/i);
  }
});

test('busy service responses give a retry message without repeated requests', async () => {
  for (const status of [429,503]) {
    const h=setup(); h.fetchHandler=() => response({ok:false,error:'internal_service_name',detail:'Internal capacity details'},status);
    assert.equal(await h.checker.check(),false); assert.match(h.checker.getState().error,/busy.*try again/i);
    assert.equal(h.calls.length,1); assert.equal(h.timers.size,0);
  }
});

test('timeout aborts the request and clears timers; late failures cannot overwrite a newer result', async () => {
  const h=setup(); h.fetchHandler=call => new Promise((resolve,reject) => {
    call.options.signal.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'})),{once:true});
  });
  const check=h.checker.check(); await flush(); h.fireTimeout();
  assert.equal(await check,false); assert.match(h.checker.getState().error,/timed out/i); assert.equal(h.timers.size,0);
  const old=deferred(); h.fetchHandler=() => old.promise;
  const stale=h.checker.check(); await flush(); h.checker.invalidate();
  h.fetchHandler=null; assert.equal(await h.checker.check(),true);
  old.reject(new Error('Late response from prior request'));
  assert.equal(await stale,false); assert.equal(h.checker.getState().phase,'blocked'); assert.equal(h.checker.getState().error,'');
});

test('destroy aborts work, unsubscribes wallet events, suppresses late rendering, and prevents new requests', async () => {
  const h=setup(), waiting=deferred(); h.fetchHandler=() => waiting.promise;
  assert.equal(h.walletListenerCount(),1); const check=h.checker.check(); await flush();
  h.checker.destroy(); const eventCount=h.events.length;
  assert.equal(h.walletListenerCount(),0); assert.equal(h.calls[0].options.signal.aborted,true);
  waiting.resolve(response(report())); assert.equal(await check,false);
  h.setWallet(disconnected()); assert.equal(await h.checker.check(),false);
  assert.equal(h.events.length,eventCount); assert.equal(h.calls.length,1); assert.equal(h.timers.size,0);
  h.checker.destroy(); h.unsubscribe();
});
