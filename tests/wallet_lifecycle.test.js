'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {createWallet, validateChallenge, validateClobAuth} = require('../site/wallet.js');

const A = '0x' + 'a'.repeat(40);
const B = '0x' + 'b'.repeat(40);
const SIGNATURE = '0x' + '12'.repeat(65);
const KNOWN = 'oddsrail.wallet.connected';
const ORIGIN = 'https://oddsrail.app';
const NOW = Date.parse('2026-09-13T12:00:00Z');
const STATEMENT = 'Sign in to OddsRail. This does not authorize trades or access to your funds.';
const flush = () => new Promise(resolve => setImmediate(resolve));
const stamp = ms => new Date(ms).toISOString().replace('.000Z', 'Z');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

function challenge(address = A, chain = 137, now = NOW) {
  return {ok:true, expires_at:(now + 300000) / 1000, message:[
    ORIGIN + ' wants you to sign in with your Ethereum account:', address, '', STATEMENT, '',
    'URI: ' + ORIGIN + '/', 'Version: 1', 'Chain ID: ' + chain,
    'Nonce: ' + 'f1'.repeat(16), 'Issued At: ' + stamp(now), 'Expiration Time: ' + stamp(now + 300000)
  ].join('\n')};
}
function session(address = A, chain = 137, now = NOW) {
  return {ok:true,authenticated:true,address,chain_id:chain,expires_at:now / 1000 + 8 * 3600};
}
function response(data, status = 200) {
  return {ok:status >= 200 && status < 300,status,json:async () => data};
}
function makeProvider() {
  const listeners = new Map();
  return {
    address:A, chain:'0x89', requests:[], handlers:{},
    async request(args) {
      this.requests.push(args);
      if (this.handlers[args.method]) return this.handlers[args.method](args);
      if (args.method === 'eth_accounts' || args.method === 'eth_requestAccounts') return [this.address];
      if (args.method === 'eth_chainId') return this.chain;
      if (args.method === 'personal_sign') return SIGNATURE;
      throw new Error('Unexpected wallet request: ' + args.method);
    },
    on(event, fn) { if (!listeners.has(event)) listeners.set(event, new Set()); listeners.get(event).add(fn); },
    removeListener(event, fn) { listeners.get(event)?.delete(fn); },
    emit(event, payload) { for (const fn of [...(listeners.get(event) || [])]) fn(payload); },
    listenerCount() { return [...listeners.values()].reduce((sum, items) => sum + items.size, 0); }
  };
}
function setup(values = {}) {
  const storage = new Map(Object.entries(values)), calls = [], handlers = {}, timers = new Map();
  let timerId = 0, currentProvider = makeProvider(), serverSession = null, currentTime = NOW;
  const h = {
    storage,calls,handlers,timers,notifications:0,
    get provider() { return currentProvider; }, setProvider(next) { currentProvider = next; },
    get serverSession() { return serverSession; }, set serverSession(next) { serverSession = next; },
    advance(ms) { currentTime += ms; },
    fireTimer(ms) { const timer = [...timers.entries()].find(([, value]) => value.ms === ms); assert(timer, 'Expected pending timer at ' + ms + 'ms'); timers.delete(timer[0]); timer[1].fn(); },
    authCalls(path) { return calls.filter(call => call.path === path); },
    portfolioCalls() { return calls.filter(call => call.path === 'portfolio'); }
  };
  const env = h.env = {
    origin:ORIGIN, api:'https://public.example', now:() => currentTime,
    provider:() => currentProvider,
    storage:{getItem:key => storage.get(key) ?? null,setItem:(key,value) => storage.set(key,value),removeItem:key => storage.delete(key)},
    async fetch(url, options) {
      const path = url.includes('/auth/wallet/') ? url.split('/auth/wallet/')[1] : 'portfolio';
      const call = {url,options,path,body:options.body ? JSON.parse(options.body) : undefined}; calls.push(call);
      let result;
      if (handlers[path]) result = await handlers[path](call);
      else if (path === 'session') result = serverSession || {ok:true,authenticated:false};
      else if (path === 'challenge') result = challenge(call.body.address,call.body.chain_id,currentTime);
      else if (path === 'verify') {
        const lines=call.body.message.split('\n'); serverSession=session(lines[1],Number(lines[7].slice(10)),currentTime); result=serverSession;
      } else if (path === 'logout') { serverSession=null; result={ok:true}; }
      else if (path === 'portfolio') result={ok:true,address:new URL(url).searchParams.get('address'),account:{},summary:{value:1}};
      else throw new Error('Unexpected route: ' + path);
      return result && typeof result.json === 'function' ? result : response(result);
    },
    setTimeout(fn,ms) { timers.set(++timerId,{fn,ms}); return timerId; }, clearTimeout(id) { timers.delete(id); },
    notify() { h.notifications++; }
  };
  h.wallet=createWallet(env); return h;
}
async function connected(h, address = A, chain = '0x89') {
  h.provider.address=address; h.provider.chain=chain;
  assert.equal(await h.wallet.connect(),true);
  await flush();
  assert.equal(h.wallet.getState().authenticated,true);
}
async function untilSigning(h) {
  const prompt=deferred(); h.provider.handlers.personal_sign=() => prompt.promise;
  const pending=h.wallet.connect(); await flush();
  assert.equal(h.wallet.getState().status,'signing'); return {pending,prompt};
}
async function untilVerifying(h) {
  const verification=deferred(); h.handlers.verify=() => verification.promise;
  const pending=h.wallet.connect(); await flush();
  assert.equal(h.wallet.getState().status,'verifying'); return {pending,verification};
}

function clobAuth() {
  return {domain:{name:'ClobAuthDomain',version:'1',chainId:137},primaryType:'ClobAuth',types:{ClobAuth:[{name:'address',type:'address'},{name:'timestamp',type:'string'},{name:'nonce',type:'uint256'},{name:'message',type:'string'}]},message:{address:A,timestamp:String(NOW/1000),nonce:0,message:'This message attests that I control the given wallet'}};
}
function clobAuthRPC() {
  const data=clobAuth();
  data.types.EIP712Domain=[{name:'name',type:'string'},{name:'version',type:'string'},{name:'chainId',type:'uint256'}];
  return data;
}
test('CLOB authentication only accepts its exact owner-bound credential message', () => {
  assert.equal(validateClobAuth(clobAuth(),A,NOW),true);
  for (const change of [d=>d.domain.chainId=1,d=>d.domain.verifyingContract=B,d=>d.message.address=B,d=>d.message.nonce=1,d=>d.message.timestamp=String(NOW/1000-91),d=>d.message.timestamp=String(NOW/1000+91),d=>d.message.message='Approve spending',d=>d.primaryType='Order',d=>d.types.Order=[],d=>d.types.ClobAuth[0].type='string',d=>d.message.calls=[],d=>d.extra=true]) {
    const data=clobAuth(); change(data); assert.equal(validateClobAuth(data,A,NOW),false);
  }
});
test('CLOB domain types must be absent or exactly canonical, including falsy values', async () => {
  const canonical=clobAuthRPC();
  assert.equal(validateClobAuth(canonical,A,NOW),true);
  for (const invalid of [null,false,0,'',[],{}]) {
    const data=clobAuth(); data.types.EIP712Domain=invalid;
    assert.equal(validateClobAuth(data,A,NOW),false);
    const h=setup(); await connected(h);
    await assert.rejects(h.wallet.signClobAuth(data),/could not be verified/);
    assert.equal(h.provider.requests.filter(r=>r.method==='eth_signTypedData_v4').length,0);
  }
});
test('CLOB signature checks session and selected wallet before and after the prompt', async () => {
  const h=setup(); await connected(h);
  h.provider.handlers.eth_signTypedData_v4=()=>SIGNATURE;
  assert.equal(await h.wallet.signClobAuth(clobAuth()),SIGNATURE);
  const request=h.provider.requests.find(r=>r.method==='eth_signTypedData_v4');
  assert.deepEqual(request.params,[A,JSON.stringify(clobAuthRPC())]);
  assert.equal(h.authCalls('session').length,4);
  assert.equal(h.provider.requests.filter(r=>r.method==='eth_sendTransaction').length,0);
});
test('CLOB signature rejects arbitrary typed data without opening a prompt', async () => {
  const h=setup(); await connected(h);
  const data=clobAuth(); data.message.address=B;
  await assert.rejects(h.wallet.signClobAuth(data),/could not be verified/);
  assert.equal(h.provider.requests.filter(r=>r.method==='eth_signTypedData_v4').length,0);
});
test('CLOB signature requires Polygon and never switches the network automatically', async () => {
  const h=setup(); await connected(h,A,'0x1');
  await assert.rejects(h.wallet.signClobAuth(clobAuth()),/Switch your wallet to Polygon/);
  assert.equal(h.provider.requests.filter(r=>r.method==='wallet_switchEthereumChain').length,0);
});
test('CLOB signature rejects a silent wallet change before the prompt', async () => {
  const h=setup(); await connected(h); h.provider.address=B;
  await assert.rejects(h.wallet.signClobAuth(clobAuth()),/wallet or sign-in changed/);
  assert.equal(h.provider.requests.filter(r=>r.method==='eth_signTypedData_v4').length,0);
});
test('CLOB signature rejects wallet changes, logout or expiry while signing', async () => {
  for (const change of [h=>{h.provider.address=B;},h=>{h.provider.chain='0x1';},h=>h.wallet.disconnect(),h=>{h.serverSession=null;},h=>{h.advance(91000); }]) {
    const h=setup(); await connected(h);
    const prompt=deferred(); h.provider.handlers.eth_signTypedData_v4=()=>prompt.promise;
    const signing=h.wallet.signClobAuth(clobAuth()); await flush();
    await change(h); prompt.resolve(SIGNATURE);
    await assert.rejects(signing,/changed|expired/);
  }
});
test('CLOB signature rechecks the selected account and network after the final session response', async () => {
  for (const change of [h=>{h.provider.address=B;},h=>{h.provider.chain='0x1';}]) {
    const h=setup(); await connected(h);
    h.provider.handlers.eth_signTypedData_v4=()=>SIGNATURE;
    let sessionChecks=0,changed=false;
    h.handlers.session=()=>{
      if (++sessionChecks===2) { change(h); changed=true; }
      return h.serverSession;
    };
    await assert.rejects(h.wallet.signClobAuth(clobAuth()),/wallet or sign-in changed/);
    assert.equal(changed,true);
    assert.equal(h.provider.requests.filter(r=>r.method==='eth_signTypedData_v4').length,1);
  }
});
test('CLOB signing checks lifecycle after the selected-wallet await before prompting or returning', async () => {
  for (const changeAtRead of [1,2]) {
    const h=setup(); await connected(h);
    h.provider.handlers.eth_signTypedData_v4=()=>SIGNATURE;
    let chainReads=0,changed=false;
    h.provider.handlers.eth_chainId=()=>{
      if (++chainReads===changeAtRead) {
        // Deliver a provider event after selected() computes true but before
        // the caller resumes from awaiting its result.
        queueMicrotask(()=>queueMicrotask(()=>{
          changed=true; h.provider.emit('accountsChanged',[B]);
        }));
      }
      return '0x89';
    };
    await assert.rejects(h.wallet.signClobAuth(clobAuth()),/wallet or sign-in changed/);
    assert.equal(changed,true);
    assert.equal(h.wallet.getState().authenticated,false);
    assert.equal(h.provider.requests.filter(r=>r.method==='eth_signTypedData_v4').length,changeAtRead===1?0:1);
  }
});
test('CLOB signatures serialize prompts and copy the approved message before async work', async () => {
  const h=setup(); await connected(h);
  const prompt=deferred(); h.provider.handlers.eth_signTypedData_v4=()=>prompt.promise;
  const data=clobAuth(), signing=h.wallet.signClobAuth(data); data.message.address=B;
  await flush();
  await assert.rejects(h.wallet.signClobAuth(clobAuth()),/already open/);
  prompt.resolve(SIGNATURE); assert.equal(await signing,SIGNATURE);
  assert.equal(JSON.parse(h.provider.requests.find(r=>r.method==='eth_signTypedData_v4').params[1]).message.address,A);
});
test('CLOB wallet rejection is retryable and cannot produce a retained signature', async () => {
  const h=setup(); await connected(h);
  h.provider.handlers.eth_signTypedData_v4=()=>{const error=new Error('Cancelled');error.code=4001;throw error;};
  await assert.rejects(h.wallet.signClobAuth(clobAuth()),/Cancelled/);
  h.provider.handlers.eth_signTypedData_v4=()=>SIGNATURE;
  assert.equal(await h.wallet.signClobAuth(clobAuth()),SIGNATURE);
});

test('signed sign-in proves ownership, confirms the cookie, and reads portfolio without credentials', async () => {
  const h=setup(); await connected(h);
  assert.deepEqual(h.provider.requests.map(r=>r.method),['eth_requestAccounts','eth_chainId','personal_sign','eth_accounts','eth_chainId']);
  const sign=h.provider.requests.find(r=>r.method==='personal_sign');
  assert.equal(sign.params[1],A);
  assert.equal(Buffer.from(sign.params[0].slice(2),'hex').toString('utf8'),challenge().message);
  assert.deepEqual(h.calls.map(c=>c.path),['session','challenge','verify','session','portfolio']);
  assert.deepEqual(h.authCalls('challenge')[0].body,{address:A,chain_id:137});
  assert.deepEqual(h.authCalls('verify')[0].body,{message:challenge().message,signature:SIGNATURE});
  for(const call of h.calls.filter(c=>c.path!=='portfolio')) {
    assert.equal(call.options.credentials,'include'); assert.equal(call.options.cache,'no-store');
    assert.equal(call.options.method,call.path==='session'?'GET':'POST');
  }
  const read=h.portfolioCalls()[0]; assert.equal(read.options.credentials,'omit'); assert.equal(read.options.cache,'no-store');
  assert.equal(h.wallet.getState().address,A); assert.equal(h.wallet.getState().chainId,137);
  assert.equal(h.wallet.getState().portfolioStatus,'ready');
  assert.deepEqual([...h.storage.entries()],[[KNOWN,'true']]); assert.equal(h.notifications,1);
});

test('Ethereum and Polygon sign-ins bind the actual selected chain', async () => {
  for(const [hex,id] of [['0x1',1],['0x89',137]]) {
    const h=setup(); await connected(h,A,hex);
    assert.equal(h.wallet.getState().chainId,id); assert.equal(h.authCalls('challenge')[0].body.chain_id,id);
  }
});

test('fresh and invalid stored flags never read accounts or prompt a wallet', async () => {
  for(const saved of [undefined,'false',A,'1']) {
    const h=setup(saved===undefined?{}:{[KNOWN]:saved});
    assert.equal(await h.wallet.restore(),false); assert.equal(h.provider.requests.length,0); assert.equal(h.calls.length,0);
  }
});

test('passive restoration needs a matching server session and never requests a signature', async () => {
  const h=setup({[KNOWN]:'true'}); h.provider.address=B; h.serverSession=session(B);
  assert.equal(await h.wallet.restore(),true); await flush();
  assert.deepEqual(h.provider.requests.map(r=>r.method),['eth_accounts','eth_chainId']);
  assert.deepEqual(h.calls.map(c=>c.path),['session','portfolio']);
  assert.equal(h.wallet.getState().address,B); assert.equal(h.storage.get(KNOWN),'true');
});

test('passive restoration forgets absent, expired, wrong-account, and wrong-chain sessions', async () => {
  for(const saved of [null,session(B),session(A,1),{...session(),expires_at:NOW/1000}]) {
    const h=setup({[KNOWN]:'true'}); h.serverSession=saved;
    assert.equal(await h.wallet.restore(),false); await flush();
    assert.equal(h.wallet.getState().error,''); assert.equal(h.storage.has(KNOWN),false);
    assert.equal(h.authCalls('challenge').length,0); assert.equal(h.portfolioCalls().length,0);
    assert(!h.provider.requests.some(r=>r.method==='personal_sign'));
  }
});

test('revoked or failed passive wallet access stays silent and cannot restore identity', async () => {
  for(const handler of [() => [],() => {throw {code:4100};}]) {
    const h=setup({[KNOWN]:'true'}); h.provider.handlers.eth_accounts=handler;
    assert.equal(await h.wallet.restore(),false); await flush();
    assert.equal(h.wallet.getState().error,''); assert.equal(h.storage.has(KNOWN),false);
    assert.equal(h.authCalls('challenge').length,0);
  }
});

test('existing matching sign-in can reconnect without signing a second challenge', async () => {
  const h=setup(); h.serverSession=session(); await connected(h);
  assert.deepEqual(h.provider.requests.map(r=>r.method),['eth_requestAccounts','eth_chainId']);
  assert.equal(h.authCalls('challenge').length,0);
});

test('repeated Connect clicks share one prompt and connected clicks only recheck the session', async () => {
  const h=setup(); const {pending,prompt}=await untilSigning(h);
  assert.equal(h.wallet.connect(),pending); assert.equal(h.wallet.connect(),pending);
  prompt.resolve(SIGNATURE); assert.equal(await pending,true); await flush();
  assert.equal(await h.wallet.connect(),true);
  assert.equal(h.provider.requests.filter(r=>r.method==='personal_sign').length,1);
  assert.equal(h.authCalls('session').length,3);
});

test('account-access or signing cancellation clears state and a later attempt succeeds', async () => {
  for(const method of ['eth_requestAccounts','personal_sign']) {
    const h=setup(); h.provider.handlers[method]=() => {throw {code:4001};};
    assert.equal(await h.wallet.connect(),false); await flush();
    assert.match(h.wallet.getState().error,/cancelled/i); assert.equal(h.provider.listenerCount(),0);
    assert.equal(h.storage.has(KNOWN),false); delete h.provider.handlers[method]; await connected(h);
  }
});

test('unsupported chains and malformed account access cannot reach challenge signing', async () => {
  for(const hex of ['0xa','0x0','137','not-a-chain']) {
    const h=setup(); h.provider.chain=hex;
    assert.equal(await h.wallet.connect(),false); assert.match(h.wallet.getState().error,/Ethereum or Polygon/);
    assert.equal(h.authCalls('challenge').length,0);
  }
  for(const accounts of [[],null,A,{},['bad'],['0x'+'0'.repeat(40)]]) {
    const h=setup(); h.provider.handlers.eth_requestAccounts=() => accounts;
    assert.equal(await h.wallet.connect(),false); assert.equal(h.authCalls('challenge').length,0);
  }
});

test('canonical SIWE validation rejects altered authority, account, chain, nonce, and terms', () => {
  assert.equal(validateChallenge(challenge(),A,137,ORIGIN,NOW),true);
  const edits = [
    [0,'https://attacker.example wants you to sign in with your Ethereum account:'],[1,B],
    [2,'not blank'],[3,'Authorize all trades and fund transfers.'],[4,'not blank'],
    [5,'URI: https://attacker.example/'],[6,'Version: 2'],[7,'Chain ID: 1'],[8,'Nonce: bad'],
    [9,'Issued At: invalid'],[10,'Expiration Time: invalid']
  ];
  for(const [index,text] of edits) {
    const data=challenge(), lines=data.message.split('\n');lines[index]=text;data.message=lines.join('\n');
    assert.equal(validateChallenge(data,A,137,ORIGIN,NOW),false,'line '+index);
  }
  for(const data of [null,{...challenge(),ok:'true'},{...challenge(),message:challenge().message+'\n'},
    {...challenge(),message:'a'.repeat(1501)},{...challenge(),expires_at:0},
    {...challenge(),expires_at:String(challenge().expires_at)},challenge(A,137,NOW-300000),challenge(A,137,NOW+61000)]) {
    assert.equal(validateChallenge(data,A,137,ORIGIN,NOW),false);
  }
});

test('untrusted or expired challenge messages never reach the wallet signature prompt', async () => {
  for(const data of [{...challenge(),message:challenge().message.replace(STATEMENT,'Transfer funds.')},challenge(A,137,NOW-300000)]) {
    const h=setup(); h.handlers.challenge=() => data;
    assert.equal(await h.wallet.connect(),false); await flush();
    assert.match(h.wallet.getState().error,/No signature was requested/);
    assert(!h.provider.requests.some(r=>r.method==='personal_sign')); assert.equal(h.authCalls('verify').length,0);
  }
});

test('malformed signatures are not sent for verification, and rejected signatures do not authenticate', async () => {
  for(const signature of [null,'0x123','12'.repeat(65),'0x'+'zz'.repeat(65)]) {
    const h=setup();h.provider.handlers.personal_sign=() => signature;
    assert.equal(await h.wallet.connect(),false); await flush();
    assert.match(h.wallet.getState().error,/signature is not supported/);assert.equal(h.authCalls('verify').length,0);
  }
  const h=setup();h.handlers.verify=() => response({ok:false,error:'invalid_signature'},401);
  assert.equal(await h.wallet.connect(),false);await flush();
  assert.equal(h.wallet.getState().authenticated,false); assert.equal(h.storage.has(KNOWN),false);assert.equal(h.portfolioCalls().length,0);
});

test('server rejection and rate limiting produce actionable errors and no authenticated identity', async () => {
  for(const [path,status] of [['session',503],['challenge',429],['verify',429]]) {
    const h=setup();h.handlers[path]=() => response({ok:false},status);
    assert.equal(await h.wallet.connect(),false);await flush();
    assert.equal(h.wallet.getState().authenticated,false);assert.equal(h.portfolioCalls().length,0);
    assert.match(h.wallet.getState().error,status===429?/Too many sign-in attempts/:/could not be completed/);
  }
});

test('verification must match address, chain, and bounded expiry before accepting a cookie', async () => {
  for(const signed of [session(B),session(A,1),{...session(),authenticated:false},{...session(),expires_at:NOW/1000},
    {...session(),expires_at:NOW/1000+9*3600},{...session(),expires_at:'9999999999'}]) {
    const h=setup();h.handlers.verify=() => signed;
    assert.equal(await h.wallet.connect(),false);await flush();
    assert.equal(h.wallet.getState().authenticated,false);assert.equal(h.portfolioCalls().length,0);
  }
});

test('successful verification without a retained session cookie cannot log in', async () => {
  const h=setup();h.handlers.session=() => ({ok:true,authenticated:false});
  assert.equal(await h.wallet.connect(),false);await flush();
  assert.match(h.wallet.getState().error,/essential cookies/);assert.equal(h.storage.has(KNOWN),false);
  assert.equal(h.portfolioCalls().length,0);assert.equal(h.authCalls('logout').length,1);
});

test('account or chain change during the signature prompt cancels the old sign-in', async () => {
  for(const [event,payload] of [['accountsChanged',[B]],['chainChanged','0x1']]) {
    const h=setup();const {pending,prompt}=await untilSigning(h);
    h.provider.emit(event,payload);prompt.resolve(SIGNATURE);assert.equal(await pending,false);await flush();
    assert.equal(h.wallet.getState().status,'disconnected');assert.equal(h.authCalls('verify').length,0);
    assert.equal(h.provider.listenerCount(),0);assert.equal(h.authCalls('logout').length,1);
  }
});

test('silent account and chain changes are caught by the post-signature provider read', async () => {
  for(const changed of ['account','chain']) {
    const h=setup();const {pending,prompt}=await untilSigning(h);
    if(changed==='account')h.provider.address=B;else h.provider.chain='0x1';
    prompt.resolve(SIGNATURE);assert.equal(await pending,false);await flush();
    assert.match(h.wallet.getState().error,/account or network changed/);assert.equal(h.authCalls('verify').length,0);
  }
});

test('account and chain changes while verification is pending revoke the resulting server session', async () => {
  for(const [event,payload] of [['accountsChanged',[B]],['chainChanged','0x1']]) {
    const h=setup();const {pending,verification}=await untilVerifying(h);
    h.provider.emit(event,payload);await flush();assert.equal(h.authCalls('logout').length,0);
    verification.resolve(session());assert.equal(await pending,false);await flush();
    assert.equal(h.authCalls('logout').length,1);assert.equal(h.wallet.getState().authenticated,false);
    assert.equal(h.portfolioCalls().length,0);
  }
});

test('disconnect during verification clears UI immediately but serializes server logout after verification', async () => {
  const h=setup();const {pending,verification}=await untilVerifying(h);
  const disconnected=h.wallet.disconnect();assert.equal(h.wallet.getState().status,'disconnected');
  await flush();assert.equal(h.authCalls('logout').length,0);
  verification.resolve(session());assert.equal(await pending,false);assert.equal(await disconnected,true);
  assert.deepEqual(h.calls.map(c=>c.path),['session','challenge','verify','logout']);
  assert.equal(h.portfolioCalls().length,0);assert.equal(h.storage.has(KNOWN),false);
});

test('new sign-in cannot overtake an old verification and its queued logout', async () => {
  const h=setup();const {pending,verification}=await untilVerifying(h);
  const disconnected=h.wallet.disconnect();h.setProvider(makeProvider());h.provider.address=B;
  const second=h.wallet.connect();await flush();assert.equal(h.authCalls('session').length,1);
  delete h.handlers.verify;verification.resolve(session());
  assert.equal(await pending,false);assert.equal(await disconnected,true);assert.equal(await second,true);await flush();
  assert.equal(h.wallet.getState().address,B);
  assert.deepEqual(h.calls.map(c=>c.path),['session','challenge','verify','logout','session','challenge','verify','session','portfolio']);
});

test('disconnect invalidates pending account access and late provider events', async () => {
  const h=setup(), access=deferred(), old=h.provider;old.handlers.eth_requestAccounts=() => access.promise;
  const pending=h.wallet.connect();await flush();await h.wallet.disconnect();
  old.emit('accountsChanged',[B]);access.resolve([A]);assert.equal(await pending,false);
  assert.equal(h.wallet.getState().address,'');assert.equal(old.listenerCount(),0);assert.equal(h.portfolioCalls().length,0);
});

test('conflicting initial account events cannot authenticate a stale account response', async () => {
  const h=setup(), access=deferred();h.provider.handlers.eth_requestAccounts=() => access.promise;
  const pending=h.wallet.connect();await flush();h.provider.emit('accountsChanged',[B]);access.resolve([A]);
  assert.equal(await pending,false);assert.equal(h.authCalls('challenge').length,0);assert.equal(h.wallet.getState().address,'');
});

test('case-only account events and unchanged chain events keep an authenticated session', async () => {
  const h=setup();await connected(h);
  h.provider.emit('accountsChanged',['0x'+A.slice(2).toUpperCase()]);h.provider.emit('chainChanged','0x89');await flush();
  assert.equal(h.wallet.getState().authenticated,true);assert.equal(h.authCalls('logout').length,0);
});

test('invalid account events and provider disconnect clear loaded balances and sign-in permission', async () => {
  for(const accounts of [[],null,A,{},['invalid'],['0x'+'0'.repeat(40)],[null,A]]) {
    const h=setup();await connected(h);h.provider.emit('accountsChanged',accounts);await flush();
    const state=h.wallet.getState();assert.equal(state.address,'');assert.equal(state.portfolio,null);assert.equal(state.authenticated,false);
    assert.equal(h.storage.has(KNOWN),false);assert.equal(h.provider.listenerCount(),0);
  }
  const h=setup();await connected(h);h.provider.emit('disconnect',{code:4900});await flush();
  assert.equal(h.wallet.getState().authenticated,false);assert.match(h.wallet.getState().error,/disconnected/);
});

test('failed logout stays actionable and a successful retry clears its pending warning', async () => {
  const h=setup();await connected(h);h.handlers.logout=() => {throw new Error('offline');};
  assert.equal(await h.wallet.disconnect(),false);
  assert.equal(h.wallet.getState().status,'disconnected');assert.equal(h.wallet.getState().signOutPending,true);
  assert.match(h.wallet.getState().error,/Retry sign-out/);delete h.handlers.logout;
  assert.equal(await h.wallet.disconnect(),true);
  assert.equal(h.wallet.getState().signOutPending,false);assert.equal(h.wallet.getState().error,'');
});

test('local expiry clears identity and cached portfolio without requesting another signature', async () => {
  const h=setup();await connected(h);const count=h.provider.requests.length;
  h.advance(8*3600000);h.fireTimer(8*3600000);
  assert.equal(h.wallet.getState().authenticated,false);assert.equal(h.wallet.getState().portfolio,null);
  assert.equal(h.storage.has(KNOWN),false);assert.match(h.wallet.getState().error,/expired/);assert.equal(h.provider.requests.length,count);
});

test('focus-style session checks clear changed, expired, or unavailable sessions without signing', async () => {
  for(const changed of [null,session(B),session(A,1),{...session(),expires_at:NOW/1000},'offline']) {
    const h=setup();await connected(h);const count=h.provider.requests.length;
    if(changed==='offline')h.handlers.session=() => {throw new Error('offline');};else h.serverSession=changed;
    assert.equal(await h.wallet.checkSession(),false);
    assert.equal(h.wallet.getState().authenticated,false);assert.equal(h.wallet.getState().portfolio,null);
    assert.equal(h.storage.has(KNOWN),false);assert.equal(h.provider.requests.length,count);
  }
});

test('concurrent session rechecks coalesce and stale recheck responses cannot restore a disconnected user', async () => {
  const h=setup();await connected(h);const check=deferred();h.handlers.session=() => check.promise;
  const first=h.wallet.checkSession(),second=h.wallet.checkSession();await flush();
  assert.equal(h.authCalls('session').length,3);const disconnected=h.wallet.disconnect();
  check.resolve(session());assert.equal(await first,false);assert.equal(await second,false);assert.equal(await disconnected,true);
  assert.equal(h.wallet.getState().authenticated,false);
});

test('late portfolio success or failure after disconnect cannot restore data', async () => {
  for(const reject of [false,true]) {
    const h=setup(),read=deferred();h.handlers.portfolio=() => read.promise;await connected(h);
    const request=h.portfolioCalls()[0];await h.wallet.disconnect();assert.equal(request.options.signal.aborted,true);
    if(reject)read.reject(new Error('late failure'));else read.resolve({ok:true,address:A,account:{},summary:{value:42}});
    await flush();const state=h.wallet.getState();assert.equal(state.portfolio,null);assert.equal(state.portfolioStatus,'idle');assert.equal(state.portfolioError,'');
  }
});

test('A to B to A sign-ins ignore old portfolio responses even when the address matches again', async () => {
  const h=setup(),first=deferred(),second=deferred();
  h.handlers.portfolio=() => first.promise;await connected(h,A);await h.wallet.disconnect();
  h.handlers.portfolio=() => second.promise;await connected(h,B);await h.wallet.disconnect();
  delete h.handlers.portfolio;await connected(h,A);
  first.resolve({ok:true,address:A,account:{},summary:{value:10}});second.resolve({ok:true,address:B,account:{},summary:{value:20}});await flush();
  assert.equal(h.wallet.getState().address,A);assert.equal(h.wallet.getState().portfolio.summary.value,1);
  assert(h.portfolioCalls().slice(0,2).every(call=>call.options.signal.aborted));
});

test('refresh verifies the session before fetching balances, and an older failure cannot overwrite new data', async () => {
  const h=setup(),old=deferred();h.handlers.portfolio=() => old.promise;await connected(h);
  delete h.handlers.portfolio;const refreshed=h.wallet.refresh();await refreshed;
  assert.deepEqual(h.calls.slice(-2).map(c=>c.path),['session','portfolio']);assert.equal(h.portfolioCalls()[0].options.signal.aborted,true);
  old.reject(new Error('old failure'));await flush();assert.equal(h.wallet.getState().portfolioStatus,'ready');
  h.serverSession=null;const count=h.portfolioCalls().length;await h.wallet.refresh();assert.equal(h.portfolioCalls().length,count);
});

test('portfolio failures preserve sign-in, reject wrong-account envelopes, and recover through refresh', async () => {
  for(const extra of [{address:B},{ok:false},{ok:'true'},{ok:undefined},{account:null},{summary:null}]) {
    const h=setup();h.handlers.portfolio=() => ({ok:true,address:A,account:{},summary:{},...extra});await connected(h);
    assert.equal(h.wallet.getState().portfolioStatus,'error');assert.equal(h.wallet.getState().portfolio,null);assert.equal(h.wallet.getState().authenticated,true);
    delete h.handlers.portfolio;await h.wallet.refresh();assert.equal(h.wallet.getState().portfolioStatus,'ready');
  }
});

test('a portfolio timeout prevents late successful responses from rendering', async () => {
  const h=setup(),read=deferred();h.handlers.portfolio=() => read.promise;await connected(h);
  h.fireTimer(25000);assert.equal(h.portfolioCalls()[0].options.signal.aborted,true);
  read.resolve({ok:true,address:A,account:{},summary:{value:99}});await flush();
  assert.equal(h.wallet.getState().portfolioStatus,'error');assert.equal(h.wallet.getState().portfolio,null);assert.equal(h.wallet.getState().authenticated,true);
});

test('missing, throwing, and unsubscribable providers fail safely and can recover', async () => {
  for(const provider of [null,{}, {request(){throw new Error('synchronous failure');}}, {request:async()=>[A],on(){throw new Error('cannot subscribe');}}]) {
    const h=setup();h.setProvider(provider);assert.equal(await h.wallet.connect(),false);await flush();
    assert.equal(h.wallet.getState().authenticated,false);h.setProvider(makeProvider());await connected(h);
  }
  const h=setup();h.setProvider(null);assert.equal(await h.wallet.connect(),false);assert.match(h.wallet.getState().error,/wallet browser/);
});

test('wallet auth stores only its connection flag and leaves drafts and unrelated login data untouched', async () => {
  const drafts='[{"id":"draft","config":{"vals":{"fade":{"jump":8,"hours":6}}}}]';
  const h=setup({'oddsrail-drafts-v2':drafts,'oddsrail-session':'unrelated-login'});await connected(h);
  assert.deepEqual([...h.storage.entries()],[['oddsrail-drafts-v2',drafts],['oddsrail-session','unrelated-login'],[KNOWN,'true']]);
  await h.wallet.disconnect();assert.deepEqual([...h.storage.entries()],[['oddsrail-drafts-v2',drafts],['oddsrail-session','unrelated-login']]);
});

test('blocked browser storage does not prevent explicit signature authentication', async () => {
  const h=setup();h.env.storage={getItem(){throw new Error('blocked');},setItem(){throw new Error('blocked');},removeItem(){throw new Error('blocked');}};
  assert.equal(await h.wallet.restore(),false);await connected(h);await h.wallet.disconnect();assert.equal(h.wallet.getState().authenticated,false);
});

test('subscribers observe authentication transitions and cannot replace the exposed identity snapshot', async () => {
  const h=setup(),states=[];const unsubscribe=h.wallet.subscribe(state=>states.push(state));states[0].address=B;
  assert.equal(h.wallet.getState().address,'');await connected(h);
  for(const status of ['connecting','signing','verifying','connected'])assert(states.some(s=>s.status===status),status);
  unsubscribe();const count=states.length;await h.wallet.disconnect();assert.equal(states.length,count);
});
