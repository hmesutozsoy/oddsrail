'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {webcrypto, createHmac} = require('node:crypto');
const {createTradingConnection} = require('../site/trading-connection.js');

const OWNER = '0x' + 'a'.repeat(40);
const ACCOUNT = '0x' + 'b'.repeat(40);
const OTHER = '0x' + 'c'.repeat(40);
const SPENDER = '0x' + 'd'.repeat(40);
const CLOB = 'https://clob.polymarket.com';
const API = 'https://api.example';
const CLOCK = 1800000000000;
const SECRET = Buffer.from(Array.from({length:32}, (_, i) => i)).toString('base64');
const SIGNATURE = '0x' + '11'.repeat(65);
const clone = value => JSON.parse(JSON.stringify(value));
const flush = () => new Promise(resolve => setImmediate(resolve));
const signedIn = overrides => ({authenticated:true,status:'connected',address:OWNER,chainId:137,expiresAt:CLOCK / 1000 + 3600,...overrides});
const signedOut = () => ({authenticated:false,status:'disconnected',address:null,chainId:null});
const response = (data, status=200) => new Response(JSON.stringify(data), {status,headers:{'Content-Type':'application/json'}});
const account = (address=ACCOUNT, wallet_type='DEPOSIT_WALLET') => ({address,wallet_type,deployed:true});
const accounts = values => ({ok:true,complete:true,address:OWNER,chain_id:137,accounts:values ?? [account()]});
const credentials = overrides => ({apiKey:'public-api-key',passphrase:'private-passphrase',secret:SECRET,...overrides});
const balance = () => ({balance:'123000000',allowances:{[SPENDER]:'999000000'}});
const order = (id='order-1', overrides={}) => ({id,maker_address:ACCOUNT,asset_id:'123',market:'0x'+'1'.repeat(64),side:'BUY',price:'0.48',original_size:'20',size_matched:'2.5',status:'LIVE',order_type:'GTC',...overrides});
const page = (data=[], next_cursor='LTE=') => ({data,next_cursor,count:data.length,limit:500});
function deferred() { let resolve,reject; const promise=new Promise((yes,no) => { resolve=yes; reject=no; }); return {promise,resolve,reject}; }
async function until(predicate, message='Expected operation did not start') {
  for (let i=0;i<200;i++) { if (predicate()) return; await flush(); }
  assert.fail(message);
}

// All network and wallet prompts are isolated doubles. Real WebCrypto verifies
// the HMAC wire format against an independent node:crypto implementation.
function setup(initial=signedIn(), options={}) {
  let walletState={...initial}, nextTimer=0;
  const listeners=new Set(), timers=new Map();
  const h={calls:[],events:[],signatures:[],connectCalls:0,sessionChecks:0,timers,clock:CLOCK,
    accountResult:accounts(),credentialResult:credentials(),balanceResult:balance(),ordersResult:page([order()]),closedResult:{closed_only:false},
    fetchHandler:null,connectHandler:null,sessionHandler:null,signHandler:null,
    setWallet(value, notify=true) { walletState={...value}; if (notify) for (const fn of [...listeners]) fn({...walletState}); },
    fire(ms) {
      const entry=[...timers].find(([, value]) => value.ms === ms);
      assert(entry, 'Expected active timer for '+ms+' ms'); timers.delete(entry[0]); entry[1].fn();
    },
    listenerCount:() => listeners.size,
  };
  h.env={api:API+'/',crypto:options.crypto === undefined ? webcrypto : options.crypto,now:() => h.clock,
    setTimeout(fn,ms) { timers.set(++nextTimer,{fn,ms}); return nextTimer; }, clearTimeout(id) { timers.delete(id); },
    wallet:{
      getState:() => ({...walletState}),
      subscribe(fn) { listeners.add(fn); fn({...walletState}); return () => listeners.delete(fn); },
      async connect() { h.connectCalls++; if (h.connectHandler) return h.connectHandler(); h.setWallet(signedIn()); return true; },
      async checkSession() { h.sessionChecks++; return h.sessionHandler ? h.sessionHandler() : true; },
      async signClobAuth(data) { h.signatures.push(clone(data)); return h.signHandler ? h.signHandler(data) : SIGNATURE; },
    },
    async fetch(url, request) {
      const call={url,request}; h.calls.push(call);
      if (h.fetchHandler) { const value=await h.fetchHandler(call); if (value !== undefined) return value; }
      const path=new URL(url).pathname;
      if (url === API+'/trading/accounts') return response(h.accountResult);
      if (path === '/auth/api-key' || path === '/auth/derive-api-key') return response(h.credentialResult);
      if (path === '/balance-allowance') return response(h.balanceResult);
      if (path === '/data/orders') return response(h.ordersResult);
      if (path === '/auth/ban-status/closed-only') return response(h.closedResult);
      assert.fail('Unexpected network request: '+url);
    },
  };
  h.connection=createTradingConnection(h.env);
  h.connection.subscribe(value => h.events.push(value));
  return h;
}

test('connection is passive until clicked; fixed private reads use real WebCrypto HMAC and no application cookies', async () => {
  const h=setup(); await flush();
  assert.equal(h.calls.length,0); assert.equal(h.signatures.length,0);
  assert.equal(await h.connection.connect(),true);
  assert.equal(h.connectCalls,0); assert.equal(h.sessionChecks,1); assert.equal(h.signatures.length,1);
  assert.deepEqual(h.calls.map(call => [new URL(call.url).pathname,call.request.method]).sort(),[
    ['/auth/api-key','POST'],['/auth/ban-status/closed-only','GET'],['/balance-allowance','GET'],['/data/orders','GET'],['/trading/accounts','POST'],
  ].sort());
  const discovery=h.calls[0];
  assert.equal(discovery.request.credentials,'include'); assert.equal(discovery.request.body,'{}');
  assert.deepEqual(discovery.request.headers,{'Content-Type':'application/json'});
  assert.equal(discovery.url,API+'/trading/accounts');
  for (const call of h.calls) {
    assert.equal(call.request.cache,'no-store'); assert.equal(call.request.redirect,'error');
    assert(call.request.signal instanceof AbortSignal);
    if (call.url.startsWith(CLOB)) assert.equal(call.request.credentials,'omit');
  }
  const auth=h.calls.find(call => call.url === CLOB+'/auth/api-key');
  assert.deepEqual(auth.request.headers,{POLY_ADDRESS:OWNER,POLY_SIGNATURE:SIGNATURE,POLY_TIMESTAMP:String(CLOCK/1000),POLY_NONCE:'0'});
  assert.equal(auth.request.body,undefined);
  for (const call of h.calls.filter(call => call.request.headers.POLY_API_KEY)) {
    const url=new URL(call.url);
    const expected=createHmac('sha256',Buffer.from(SECRET,'base64')).update(String(CLOCK/1000)+'GET'+url.pathname).digest('base64').replace(/\+/g,'-').replace(/\//g,'_');
    assert.deepEqual(call.request.headers,{POLY_ADDRESS:OWNER,POLY_API_KEY:'public-api-key',POLY_PASSPHRASE:'private-passphrase',POLY_TIMESTAMP:String(CLOCK/1000),POLY_SIGNATURE:expected});
    assert.equal(call.request.body,undefined);
  }
  assert.equal(h.calls.find(call => call.url.includes('/balance-allowance')).url,CLOB+'/balance-allowance?asset_type=COLLATERAL&signature_type=3');
  assert.equal(h.calls.find(call => call.url.includes('/data/orders')).url,CLOB+'/data/orders?next_cursor=MA%3D%3D');
  const typed=h.signatures[0];
  assert.deepEqual(typed.domain,{name:'ClobAuthDomain',version:'1',chainId:137});
  assert.equal(typed.primaryType,'ClobAuth'); assert.equal(typed.message.address,OWNER); assert.equal(typed.message.nonce,0);
  assert.deepEqual(h.connection.getState().balance,balance()); assert.equal(h.connection.getState().ordersComplete,true);
  assert.equal(h.connection.getState().closedOnly,false); assert.equal(h.connection.getState().connected,true);
  assert.deepEqual([...h.timers.values()].map(value => value.ms),[1800000]);
  h.connection.destroy();
});

test('credentials and mutable internal state never leave the connection state or subscriptions', async () => {
  const h=setup(); h.ordersResult=page([order('order-1',{secret:'never-display',arbitrary:'untrusted'})]);
  assert.equal(await h.connection.connect(),true);
  const serialized=JSON.stringify([h.events,h.connection.getState()]);
  for (const sensitive of [SECRET,'private-passphrase','public-api-key',SIGNATURE,'never-display','arbitrary']) assert.equal(serialized.includes(sensitive),false);
  const copy=h.connection.getState(); copy.balance.balance='0'; copy.orders[0].price='1'; copy.account.address=OTHER;
  assert.equal(h.connection.getState().balance.balance,'123000000'); assert.equal(h.connection.getState().orders[0].price,'0.48'); assert.equal(h.connection.getState().account.address,ACCOUNT);
  h.connection.disconnect(); assert.equal(h.connection.getState().owner,null); assert.equal(h.timers.size,0);
  assert.equal(await h.connection.refresh(),false); assert.equal(h.calls.length,5);
});

test('missing account guides setup without signing; multiple accounts require an explicit verified selection', async () => {
  const h=setup(); h.accountResult=accounts([]);
  assert.equal(await h.connection.connect(),false); assert.equal(h.connection.getState().phase,'setup_required'); assert.equal(h.signatures.length,0);
  h.accountResult=accounts([account(),account(OTHER,'GNOSIS_SAFE')]);
  assert.equal(await h.connection.connect(),false); assert.equal(h.connection.getState().phase,'choosing'); assert.equal(h.signatures.length,0);
  h.ordersResult=page([]);
  assert.equal(await h.connection.connect({tradingWallet:OTHER}),true);
  assert.equal(h.connection.getState().account.wallet_type,'GNOSIS_SAFE');
  assert.match(h.calls.find(call => call.url.includes('/balance-allowance')).url,/signature_type=2$/);
  h.connection.destroy();
});

test('selection accepts case-insensitive verified accounts and all supported wallet types', async () => {
  for (const [kind,type] of [['DEPOSIT_WALLET',3],['GNOSIS_SAFE',2],['POLY_PROXY',1]]) {
    const h=setup(); h.accountResult=accounts([account(ACCOUNT,kind)]);
    assert.equal(await h.connection.connect({tradingWallet:'0x'+'B'.repeat(40)}),true);
    assert.match(h.calls.find(call => call.url.includes('/balance-allowance')).url,new RegExp('signature_type='+type+'$'));
    h.connection.destroy();
  }
});

test('unverified, undeployed, cross-owner, duplicate, zero-address and unknown account envelopes fail before the signature', async () => {
  const invalid=[null,[],{},accounts([account(OWNER)]),accounts([account('0x'+'0'.repeat(40))]),accounts([account('bad')]),accounts([account(ACCOUNT,'EOA')]),
    accounts([{...account(),deployed:false}]),accounts([account(),account('0x'+'B'.repeat(40))]),accounts([null]),
    {...accounts(),complete:false},{...accounts(),ok:false},{...accounts(),chain_id:'137'},{...accounts(),address:OTHER},
    {...accounts(),accounts:{}},accounts(Array.from({length:5},(_, i) => account('0x'+String(i+1).repeat(40))))];
  for (const value of invalid) {
    const h=setup(); h.accountResult=value;
    assert.equal(await h.connection.connect(),false); assert.equal(h.signatures.length,0); assert.equal(h.calls.length,1); assert.equal(h.connection.getState().connected,false);
  }
});

test('arbitrary selection and ambiguous same-type accounts cannot silently authorize another balance', async () => {
  for (const selected of [OTHER,'bad',{},'0x'+'0'.repeat(40)]) {
    const h=setup(); assert.equal(await h.connection.connect({tradingWallet:selected}),false); assert.equal(h.signatures.length,0);
  }
  const h=setup(); h.accountResult=accounts([account(),account(OTHER)]);
  assert.equal(await h.connection.connect({tradingWallet:ACCOUNT}),false); assert.equal(h.signatures.length,0); assert.match(h.connection.getState().error,/same type/);
});

test('only an exact create-credentials HTTP 400 triggers derive with the same L1 signature', async () => {
  const h=setup(); h.fetchHandler=call => call.url === CLOB+'/auth/api-key' ? response({},400) : undefined;
  assert.equal(await h.connection.connect(),true);
  const calls=h.calls.filter(call => call.url.includes('/auth/') && call.request.headers.POLY_NONCE);
  assert.deepEqual(calls.map(call => [call.url,call.request.method]),[[CLOB+'/auth/api-key','POST'],[CLOB+'/auth/derive-api-key','GET']]);
  assert.deepEqual(calls[0].request.headers,calls[1].request.headers); assert.equal(h.signatures.length,1);
  h.connection.destroy();
});

test('forbidden, unauthorized, rate-limited, malformed, other HTTP and network errors never fall back to derive', async () => {
  for (const failure of [401,403,429,422,500,'network','malformed']) {
    const h=setup(); h.fetchHandler=call => {
      if (call.url !== CLOB+'/auth/api-key') return;
      if (failure === 'network') throw new Error('secret-network-message');
      if (failure === 'malformed') return new Response('{bad',{status:200});
      return response({detail:'secret-upstream-message'},failure);
    };
    assert.equal(await h.connection.connect(),false); assert.equal(h.calls.length,2);
    assert.equal(h.calls.some(call => call.url.includes('derive')),false); assert.equal(h.connection.getState().error.includes('secret'),false);
    if (failure === 403) assert.match(h.connection.getState().error,/cannot bypass/);
  }
});

test('invalid credentials are rejected before private reads, including malformed or short secrets', async () => {
  for (const value of [null,[],{},credentials({apiKey:''}),credentials({apiKey:'x'.repeat(201)}),credentials({passphrase:'bad\nvalue'}),
    credentials({secret:'a'}),credentials({secret:'!'.repeat(32)}),credentials({secret:Buffer.alloc(8).toString('base64')}),credentials({secret:'A'.repeat(513)}),credentials({secret:{}})]) {
    const h=setup(); h.credentialResult=value;
    assert.equal(await h.connection.connect(),false); assert.equal(h.calls.length,2); assert.match(h.connection.getState().error,/credentials/); assert.equal(h.timers.size,0);
  }
});

test('URL-safe unpadded secrets produce the same HMAC as their underlying bytes', async () => {
  const h=setup(); h.credentialResult.secret=Buffer.alloc(32,255).toString('base64url');
  assert.equal(await h.connection.connect(),true);
  const call=h.calls.find(call => new URL(call.url).pathname === '/balance-allowance');
  const expected=createHmac('sha256',Buffer.alloc(32,255)).update(String(CLOCK/1000)+'GET/balance-allowance').digest('base64').replace(/\+/g,'-').replace(/\//g,'_');
  assert.equal(call.request.headers.POLY_SIGNATURE,expected); h.connection.destroy();
});

test('unsupported or cancelled wallet signatures and missing browser crypto never create credentials', async () => {
  for (const signature of [null,'0x12','0x'+'g'.repeat(130),{},'0x'+'1'.repeat(132)]) {
    const h=setup(); h.signHandler=() => signature;
    assert.equal(await h.connection.connect(),false); assert.equal(h.calls.length,1); assert.match(h.connection.getState().error,/signature/);
  }
  for (const code of [4001,'ACTION_REJECTED']) {
    const h=setup(); h.signHandler=() => { throw Object.assign(new Error('wallet-detail'),{code}); };
    assert.equal(await h.connection.connect(),false); assert.match(h.connection.getState().error,/cancelled/); assert.equal(h.calls.length,1);
  }
  const h=setup(signedIn(),{crypto:null}); assert.equal(await h.connection.connect(),false); assert.equal(h.signatures.length,0); assert.equal(h.calls.length,1);
});

test('sign-in and Polygon checks precede account discovery and fresh session authentication is mandatory', async () => {
  const h=setup(signedOut()); assert.equal(await h.connection.connect(),true); assert.equal(h.connectCalls,1); h.connection.destroy();
  for (const state of [signedIn({chainId:1}),signedIn({address:'bad'}),signedIn({expiresAt:CLOCK/1000-1})]) {
    const attempt=setup(state); assert.equal(await attempt.connection.connect(),false); assert.equal(attempt.calls.length,0);
  }
  const cancelled=setup(signedOut()); cancelled.connectHandler=() => false;
  assert.equal(await cancelled.connection.connect(),false); assert.equal(cancelled.calls.length,0);
  const invalid=setup(); invalid.sessionHandler=() => false;
  assert.equal(await invalid.connection.connect(),false); assert.equal(invalid.calls.length,0); assert.match(invalid.connection.getState().error,/sign-in/);
});

test('unsigned decimal balances and allowances reject numeric coercion, overflow, duplicate spenders and invalid shapes', async () => {
  const badAmounts=[-1,0,1.5,'-1','1.1','01','1e6','',null,(1n<<256n).toString()];
  const invalid=[null,[],{},...badAmounts.map(value => ({...balance(),balance:value})),
    ...badAmounts.map(value => ({...balance(),allowances:{[SPENDER]:value}})),
    {...balance(),allowances:[]},{...balance(),allowances:{bad:'1'}},{...balance(),allowances:{[SPENDER]:'1',['0x'+'D'.repeat(40)]:'2'}},
    {...balance(),allowances:Object.fromEntries(Array.from({length:31},(_, i) => ['0x'+(i+1).toString(16).padStart(40,'0'),'1']))}];
  for (const value of invalid) {
    const h=setup(); h.balanceResult=value; assert.equal(await h.connection.connect(),false);
    assert.equal(h.connection.getState().balance,null); assert.equal(h.connection.getState().connected,false); assert.equal(h.timers.size,0);
  }
  const h=setup(); h.balanceResult={balance:'0',allowances:{}}; assert.equal(await h.connection.connect(),true); assert.equal(h.connection.getState().balance.balance,'0'); h.connection.destroy();
});

test('orders must belong to the selected trading wallet and contain valid bounded quantities', async () => {
  const patches=[{maker_address:OWNER},{maker_address:OTHER},{id:''},{id:'x'.repeat(151)},{id:'bad\n'},
    {asset_id:123},{asset_id:'-1'},{asset_id:(1n<<256n).toString()},{market:'bad'},{side:'buy'},
    {price:0.5},{price:'0'},{price:'1.001'},{price:'0.1234567890123456789'},
    {original_size:'-1'},{original_size:20},{size_matched:'21'},{size_matched:'NaN'},
    {status:''},{status:'x'.repeat(41)},{order_type:''}];
  for (const patch of patches) {
    const h=setup(); h.ordersResult=page([order('order-1',patch)]);
    assert.equal(await h.connection.connect(),false); assert.equal(h.connection.getState().orders.length,0); assert.equal(h.connection.getState().ordersComplete,false);
  }
});

test('closed-only status and order pagination metadata cannot be coerced or partially accepted', async () => {
  for (const value of [null,[],{}, {closed_only:0},{closed_only:'false'}]) {
    const h=setup(); h.closedResult=value; assert.equal(await h.connection.connect(),false); assert.equal(h.connection.getState().closedOnly,null);
  }
  const invalid=[null,[],{}, {...page(),data:{}},{...page(),count:'0'},{...page(),count:1},{...page(),limit:1001},{...page(),limit:-1},
    {...page(),next_cursor:''},{...page(),next_cursor:'bad cursor'},{...page(),next_cursor:'A'.repeat(301)},page(Array.from({length:501},(_, i) => order('id-'+i)))];
  for (const value of invalid) {
    const h=setup(); h.ordersResult=value; assert.equal(await h.connection.connect(),false); assert.equal(h.connection.getState().ordersComplete,false);
  }
  const h=setup(); h.closedResult={closed_only:true}; assert.equal(await h.connection.connect(),true); assert.equal(h.connection.getState().closedOnly,true); h.connection.destroy();
});

test('all order pages are read, cursor query is encoded, and duplicates across pages are rejected', async () => {
  const h=setup(); h.fetchHandler=call => {
    const url=new URL(call.url); if (url.pathname !== '/data/orders') return;
    return response(url.searchParams.get('next_cursor') === 'MA==' ? page([order('first')],'a+b/=') : page([order('second')]));
  };
  assert.equal(await h.connection.connect(),true); assert.deepEqual(h.connection.getState().orders.map(value=>value.id),['first','second']);
  assert(h.calls.some(call => call.url === CLOB+'/data/orders?next_cursor=a%2Bb%2F%3D')); h.connection.destroy();
  const duplicate=setup(); duplicate.fetchHandler=call => {
    if (new URL(call.url).pathname !== '/data/orders') return;
    return response(page([order()],new URL(call.url).searchParams.get('next_cursor') === 'MA==' ? 'MQ==' : 'LTE='));
  };
  assert.equal(await duplicate.connection.connect(),false); assert.equal(duplicate.connection.getState().orders.length,0);
});

test('empty, repeated and cyclic pagination cursors stop without claiming a complete order view', async () => {
  for (const style of ['empty','same','cycle']) {
    const h=setup(); let pages=0; h.fetchHandler=call => {
      const url=new URL(call.url); if (url.pathname !== '/data/orders') return;
      pages++;
      if (style === 'empty') return response(page([],'MQ=='));
      if (style === 'same') return response(page([order('id-'+pages)],url.searchParams.get('next_cursor')));
      return response(page([order('id-'+pages)],pages === 1 ? 'MQ==' : 'MA=='));
    };
    assert.equal(await h.connection.connect(),true); assert.equal(h.connection.getState().ordersComplete,false); assert(pages <= 2); h.connection.destroy();
  }
});

test('order reads stop after ten pages and enforce 2,000 records even when a final page overflows', async () => {
  for (const perPage of [1,500,499]) {
    const h=setup(); let pages=0; h.fetchHandler=call => {
      if (new URL(call.url).pathname !== '/data/orders') return;
      pages++;
      const entries=Array.from({length:perPage},(_, i) => order('id-'+pages+'-'+i));
      return response(page(entries,perPage === 499 && pages === 5 ? 'LTE=' : Buffer.from(String(pages)).toString('base64')));
    };
    assert.equal(await h.connection.connect(),true);
    assert.equal(h.connection.getState().ordersComplete,false); assert(h.connection.getState().orders.length <= 2000); assert(pages <= 10);
    assert.equal(h.connection.getState().orders.length,perPage === 1 ? 10 : 2000); h.connection.destroy();
  }
});

test('exactly 2,000 orders with the terminal cursor may be complete; oversized pages still validate every owner', async () => {
  for (const crossWallet of [false,true]) {
    const h=setup(); let pages=0; h.fetchHandler=call => {
      if (new URL(call.url).pathname !== '/data/orders') return;
      pages++;
      const entries=Array.from({length:500},(_, i) => order('id-'+pages+'-'+i, crossWallet && pages===4 && i===499 ? {maker_address:OTHER} : {}));
      return response(page(entries,pages===4 ? 'LTE=' : Buffer.from(String(pages)).toString('base64')));
    };
    assert.equal(await h.connection.connect(),!crossWallet);
    if (!crossWallet) { assert.equal(h.connection.getState().orders.length,2000); assert.equal(h.connection.getState().ordersComplete,true); }
    h.connection.destroy();
  }
});

test('response body size and malformed streamed JSON are bounded before accepting account data', async () => {
  for (const source of ['{broken',' '.repeat(1048577)]) {
    const h=setup(); let cancelled=false;
    h.fetchHandler=call => call.url === API+'/trading/accounts' ? new Response(new ReadableStream({start(controller) { controller.enqueue(new TextEncoder().encode(source)); controller.close(); },cancel() { cancelled=true; }})) : undefined;
    assert.equal(await h.connection.connect(),false); assert.equal(h.signatures.length,0); assert.equal(h.timers.size,0); assert.equal(h.connection.getState().accounts.length,0);
    // A closed stream need not invoke cancel; the oversized response still fails.
    void cancelled;
  }
});

test('a stalled fetch or response stream times out and later data cannot resurrect the connection', async () => {
  for (const stage of ['fetch','body']) {
    const h=setup(), gate=deferred(); let streamController;
    h.fetchHandler=call => {
      if (call.url !== API+'/trading/accounts') return;
      if (stage === 'fetch') return gate.promise;
      return new Response(new ReadableStream({start(controller) { streamController=controller; }}));
    };
    const task=h.connection.connect(); await until(() => h.calls.length===1); h.fire(25000);
    assert.equal(await task,false); assert.match(h.connection.getState().error,/timed out/); assert.equal(h.calls[0].request.signal.aborted,true);
    if (stage === 'fetch') gate.resolve(response(accounts())); else { streamController.enqueue(new TextEncoder().encode(JSON.stringify(accounts()))); streamController.close(); }
    await flush(); assert.equal(h.connection.getState().connected,false); assert.equal(h.signatures.length,0); assert.equal(h.timers.size,0);
  }
});

test('private-read overall deadline covers pagination even if each page is individually fast', async () => {
  const h=setup(), gate=deferred(); let orderCalls=0;
  h.fetchHandler=call => {
    if (new URL(call.url).pathname !== '/data/orders') return;
    orderCalls++; return orderCalls === 1 ? response(page([order()],'MQ==')) : gate.promise;
  };
  const task=h.connection.connect(); await until(() => orderCalls===2); h.fire(25000);
  assert.equal(await task,false); assert.match(h.connection.getState().error,/timed out/); assert.equal(h.connection.getState().orders.length,0);
  gate.resolve(response(page([order('second')]))); await flush(); assert.equal(h.connection.getState().connected,false); assert.equal(h.timers.size,0);
});

test('duplicate connect and refresh clicks share one operation and one wallet prompt', async () => {
  const h=setup(), gate=deferred(); h.signHandler=() => gate.promise;
  const first=h.connection.connect(); const second=h.connection.connect(); assert.equal(first,second);
  await until(() => h.signatures.length===1); assert.equal(h.connection.refresh(),first); gate.resolve(SIGNATURE);
  assert.equal(await first,true); assert.equal(h.signatures.length,1); assert.equal(h.calls.length,5); h.connection.destroy();
});

test('wallet, network, logout and sign-in expiry changes invalidate each asynchronous connection stage', async () => {
  for (const stage of ['session','accounts','signature','credentials','private']) {
    for (const next of [signedIn({address:OTHER}),signedIn({chainId:1}),signedOut(),signedIn({expiresAt:CLOCK/1000-1})]) {
      const h=setup(), gate=deferred(); let reached=false;
      if (stage==='session') h.sessionHandler=() => { reached=true; return gate.promise; };
      if (stage==='signature') h.signHandler=() => { reached=true; return gate.promise; };
      h.fetchHandler=call => {
        const path=new URL(call.url).pathname;
        if ((stage==='accounts' && path==='/trading/accounts') || (stage==='credentials' && path==='/auth/api-key') || (stage==='private' && path==='/balance-allowance')) { reached=true; return gate.promise; }
      };
      const task=h.connection.connect(); await until(() => reached); h.setWallet(next);
      gate.resolve(stage==='session' ? true : stage==='signature' ? SIGNATURE : response(stage==='accounts' ? accounts() : stage==='credentials' ? credentials() : balance()));
      assert.equal(await task,false); await flush();
      const state=h.connection.getState(); assert.equal(state.connected,false); assert.equal(state.owner,null); assert.equal(state.balance,null); assert.equal(h.timers.size,0);
    }
  }
});

test('an unannounced wallet change is caught after an asynchronous boundary before credentials are sent', async () => {
  const h=setup(), gate=deferred(); h.signHandler=() => gate.promise;
  const task=h.connection.connect(); await until(() => h.signatures.length===1); h.setWallet(signedIn({address:OTHER}),false); gate.resolve(SIGNATURE);
  assert.equal(await task,false); assert.equal(h.calls.length,1); assert.equal(h.connection.getState().connected,false);
  assert.equal(h.connection.getState().owner,null); assert.equal(h.connection.getState().phase,'error');
});

test('wallet change during real WebCrypto import or signing cannot leak a later private request', async () => {
  for (const stage of ['import','sign']) {
    const gate=deferred(); let reached=false, rawBytes, importedKey;
    const crypto={subtle:{
      async importKey(...args) {
        rawBytes=args[1]; assert.equal(args[3],false); assert.deepEqual(args[4],['sign']);
        importedKey=await webcrypto.subtle.importKey(...args);
        if (stage==='import') { reached=true; await gate.promise; }
        return importedKey;
      },
      async sign(...args) {
        const value=await webcrypto.subtle.sign(...args);
        if (stage==='sign') { reached=true; await gate.promise; }
        return value;
      },
    }};
    const h=setup(signedIn(),{crypto}); const task=h.connection.connect(); await until(() => reached);
    h.setWallet(signedOut()); gate.resolve(); assert.equal(await task,false);
    assert.equal(importedKey.extractable,false); assert(rawBytes.every(value => value===0));
    assert.equal(h.calls.length,2); assert.equal(h.connection.getState().owner,null); assert.equal(h.connection.getState().connected,false); assert.equal(h.timers.size,0);
  }
});

test('a wallet change during a stalled private response body cannot adopt the former wallet balance', async () => {
  const h=setup(); let body;
  h.fetchHandler=call => new URL(call.url).pathname==='/balance-allowance' ? new Response(new ReadableStream({start(controller) { body=controller; }})) : undefined;
  const task=h.connection.connect(); await until(() => body); h.setWallet(signedIn({address:OTHER}));
  assert.equal(await task,false); body.enqueue(new TextEncoder().encode(JSON.stringify(balance()))); body.close(); await flush();
  assert.equal(h.connection.getState().balance,null); assert.equal(h.connection.getState().owner,null); assert.equal(h.connection.getState().connected,false); assert.equal(h.timers.size,0);
});

test('disconnect or destruction while awaiting a signature prevents any later auth request or resurrection', async () => {
  for (const action of ['disconnect','destroy']) {
    const h=setup(), gate=deferred(); h.signHandler=() => gate.promise;
    const task=h.connection.connect(); await until(() => h.signatures.length===1); h.connection[action](); gate.resolve(SIGNATURE);
    assert.equal(await task,false); assert.equal(h.calls.length,1); assert.equal(h.connection.getState().phase,'idle'); assert.equal(h.timers.size,0);
    if (action==='destroy') { assert.equal(h.listenerCount(),0); assert.equal(await h.connection.connect(),false); }
  }
});

test('an older disconnected attempt cannot override a newer successful connection', async () => {
  const h=setup(), old=deferred(); h.signHandler=() => old.promise;
  const first=h.connection.connect(); await until(() => h.signatures.length===1); h.connection.disconnect(); h.signHandler=null;
  assert.equal(await h.connection.connect(),true); old.resolve(SIGNATURE); assert.equal(await first,false);
  assert.equal(h.connection.getState().connected,true); assert.equal(h.calls.filter(call => call.url===CLOB+'/auth/api-key').length,1); h.connection.destroy();
});

test('connections expire by the earlier sign-in expiry or 30-minute bound, including an in-flight refresh', async () => {
  for (const duration of [60000,1800000]) {
    const h=setup(signedIn({expiresAt:CLOCK/1000+duration/1000})); assert.equal(await h.connection.connect(),true);
    const gate=deferred(); h.fetchHandler=call => new URL(call.url).pathname==='/balance-allowance' ? gate.promise : undefined;
    const task=h.connection.refresh(); await until(() => h.calls.length>=8); h.fire(duration);
    assert.equal(await task,false); assert.equal(h.connection.getState().owner,null); assert.match(h.connection.getState().error,/expired/);
    gate.resolve(response(balance())); await flush(); assert.equal(h.connection.getState().connected,false); assert.equal(h.timers.size,0);
  }
});

test('a delayed owner signature expires after 90 seconds and never creates credentials', async () => {
  const h=setup(), gate=deferred(); h.signHandler=() => gate.promise;
  const task=h.connection.connect(); await until(() => h.signatures.length===1); h.clock+=91000; gate.resolve(SIGNATURE);
  assert.equal(await task,false); assert.match(h.connection.getState().error,/timed out/); assert.equal(h.calls.length,1);
});

test('refresh reuses only in-memory credentials, rechecks sign-in, and hides the previous balance while loading', async () => {
  const h=setup(); assert.equal(await h.connection.connect(),true); h.clock+=10000;
  const gate=deferred(); h.fetchHandler=call => new URL(call.url).pathname==='/balance-allowance' ? gate.promise : undefined;
  const task=h.connection.refresh(); await until(() => h.connection.getState().phase==='loading');
  assert.equal(h.connection.getState().connected,false); assert.equal(h.connection.getState().balance,null); assert.equal(h.connection.getState().updatedAt,null);
  gate.resolve(response({...balance(),balance:'456000000'})); assert.equal(await task,true);
  assert.equal(h.signatures.length,1); assert.equal(h.sessionChecks,2); assert.equal(h.calls.length,8); assert.equal(h.connection.getState().balance.balance,'456000000'); assert.equal(h.connection.getState().updatedAt,CLOCK+10000);
  const reads=h.calls.slice(5); assert(reads.every(call => call.request.headers.POLY_TIMESTAMP===String(CLOCK/1000+10)));
  h.connection.destroy();
});

test('failed refresh clears credentials and stale balances; explicit reconnect is required afterward', async () => {
  for (const reason of ['session','unauthorized','malformed']) {
    const h=setup(); assert.equal(await h.connection.connect(),true);
    if (reason==='session') h.sessionHandler=() => false;
    else h.fetchHandler=call => new URL(call.url).pathname==='/balance-allowance' ? response(reason==='unauthorized' ? {} : {balance:'oops'},reason==='unauthorized' ? 401 : 200) : undefined;
    assert.equal(await h.connection.refresh(),false); assert.equal(h.connection.getState().balance,null); assert.equal(h.connection.getState().connected,false); assert.equal(h.timers.size,0);
    const previous=h.calls.length; h.sessionHandler=null; h.fetchHandler=null;
    assert.equal(await h.connection.refresh(),false); assert.equal(h.calls.length,previous);
    assert.equal(await h.connection.connect(),true); assert.equal(h.signatures.length,2); h.connection.destroy();
  }
});
