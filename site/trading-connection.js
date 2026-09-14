(function () {
  'use strict';

  // The fixed CLOB requests below follow @polymarket/client 0.10.0.
  // Connection creates/derives credentials, then performs private reads only.
  // It never deploys a wallet, approves tokens, signs orders or grants a session key.
  const CLOB = 'https://clob.polymarket.com';
  const TYPES = {DEPOSIT_WALLET:3,GNOSIS_SAFE:2,POLY_PROXY:1};
  const END_CURSOR = 'LTE=';
  const STATEMENT = 'This message attests that I control the given wallet';
  const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const address = value => typeof value === 'string' && /^0x[a-f\d]{40}$/i.test(value) && !/^0x0{40}$/i.test(value);
  const sameAddress = (a,b) => address(a) && address(b) && a.toLowerCase() === b.toLowerCase();
  const integer = value => typeof value === 'string' && /^(0|[1-9]\d{0,77})$/.test(value) && BigInt(value) < (1n << 256n);
  const text = (value,max) => typeof value === 'string' && value.length > 0 && value.length <= max && !/[\u0000-\u001f\u007f]/.test(value);
  const decimal = value => typeof value === 'string' && /^(0|[1-9]\d{0,29})(\.\d{1,18})?$/.test(value);
  const scaled = value => { const [whole,fraction='']=value.split('.'); return BigInt(whole)*10n**18n+BigInt(fraction.padEnd(18,'0')); };
  const clone = value => JSON.parse(JSON.stringify(value));
  function fail(code) { const error = new Error(code); error.code=code; return error; }
  function authData(owner,timestamp) {
    return {domain:{name:'ClobAuthDomain',version:'1',chainId:137},primaryType:'ClobAuth',
      types:{ClobAuth:[{name:'address',type:'address'},{name:'timestamp',type:'string'},{name:'nonce',type:'uint256'},{name:'message',type:'string'}]},
      message:{address:owner,timestamp:String(timestamp),nonce:0,message:STATEMENT}};
  }
  function accountsFrom(data,owner) {
    if (!isObject(data) || data.ok !== true || data.complete !== true || data.chain_id !== 137 || !sameAddress(data.address,owner) || !Array.isArray(data.accounts) || data.accounts.length > 4) throw fail('accounts_invalid');
    const seen=new Set();
    return data.accounts.map(item => {
      if (!isObject(item) || !address(item.address) || sameAddress(item.address,owner) || !Object.hasOwn(TYPES,item.wallet_type) || item.deployed !== true || seen.has(item.address.toLowerCase())) throw fail('accounts_invalid');
      seen.add(item.address.toLowerCase());
      return {address:item.address,wallet_type:item.wallet_type,deployed:true};
    });
  }
  function balanceFrom(data) {
    if (!isObject(data) || !integer(data.balance) || !isObject(data.allowances) || Object.keys(data.allowances).length > 30) throw fail('balance_invalid');
    const allowances={};
    for (const [spender,amount] of Object.entries(data.allowances)) {
      if (!address(spender) || !integer(amount) || Object.hasOwn(allowances,spender.toLowerCase())) throw fail('balance_invalid');
      allowances[spender.toLowerCase()]=amount;
    }
    return {balance:data.balance,allowances};
  }
  function orderFrom(data,account) {
    if (!isObject(data) || !text(data.id,150) || !sameAddress(data.maker_address,account.address) || !integer(data.asset_id) || !/^0x[a-f\d]{64}$/i.test(data.market) || !['BUY','SELL'].includes(data.side) || !decimal(data.price) || scaled(data.price) > 10n**18n || scaled(data.price) <= 0n || !decimal(data.original_size) || !decimal(data.size_matched) || scaled(data.size_matched) > scaled(data.original_size) || !text(data.status,40) || !text(data.order_type,20)) throw fail('orders_invalid');
    // Only the fields needed to display and reserve open orders leave the closure.
    return {id:data.id,maker_address:data.maker_address,asset_id:data.asset_id,market:data.market,
      side:data.side,price:data.price,original_size:data.original_size,size_matched:data.size_matched,
      status:data.status,order_type:data.order_type};
  }
  function createTradingConnection(env) {
    const now=env.now || Date.now, setTimer=env.setTimeout || setTimeout, clearTimer=env.clearTimeout || clearTimeout;
    const api=(env.api || 'https://mcp.oddsrail.app').replace(/\/$/,'');
    let state=blank(), lifecycle=0, pending=null, credentials=null, expiryTimer=null, destroyed=false;
    const subscribers=new Set(), requests=new Set();
    function blank() { return {phase:'idle',owner:null,accounts:[],account:null,balance:null,orders:[],ordersComplete:false,closedOnly:null,connected:false,error:'',updatedAt:null}; }
    function emit(patch) { if (destroyed) return; state={...state,...patch}; for (const fn of subscribers) { try { fn(clone(state)); } catch (_) {} } }
    function validWallet(value,owner) {
      return value?.authenticated === true && value.status === 'connected' && address(value.address) && value.chainId === 137 && (!owner || sameAddress(value.address,owner)) && (!value.expiresAt || value.expiresAt*1000 > now());
    }
    function active(version,owner) { return !destroyed && version === lifecycle && (!owner || validWallet(env.wallet.getState(),owner)); }
    function assertActive(version,owner) { if (!active(version,owner)) throw fail('stale'); }
    function clearConnection(error='') {
      ++lifecycle; pending=null; credentials=null; clearTimer(expiryTimer); expiryTimer=null;
      for (const request of requests) request.abort(); requests.clear();
      emit({...blank(),...(error ? {phase:'error',error} : {})});
    }
    const unsubscribe=env.wallet.subscribe(value => {
      if (state.owner && !validWallet(value,state.owner)) clearConnection('Your wallet or sign-in changed. Reconnect your trading account.');
    });
    function readable(error) {
      const messages={
        wallet_required:'Connect your wallet and sign in to OddsRail first.',
        polygon_required:'Switch your wallet to Polygon, then reconnect to OddsRail.',
        session_invalid:'Your sign-in could not be verified. Reconnect your wallet.',
        accounts_invalid:'Your trading accounts could not be verified. Try again.',
        account_selection:'Choose one of your verified trading accounts.',
        account_ambiguous:'This wallet has multiple trading accounts of the same type. Their balances cannot be matched safely yet.',
        credentials_invalid:'Polymarket returned credentials that could not be verified. Try connecting again.',
        signature_invalid:'Your wallet did not return a supported Polymarket connection signature.',
        signature_cancelled:'You cancelled the Polymarket connection signature. No trading connection was saved.',
        crypto_unavailable:'Secure browser signing is unavailable. Open OddsRail over HTTPS in a supported browser.',
        balance_invalid:'The exchange balance response could not be verified. Refresh to retry.',
        orders_invalid:'Open orders could not be matched and verified for this trading account.',
        closed_invalid:'The account trading status could not be verified. Refresh to retry.',
        unauthorized:'Polymarket authentication expired or was rejected. Connect your trading account again.',
        forbidden:'Polymarket denied this request (403). Check your account and location eligibility on Polymarket; OddsRail cannot bypass the restriction.',
        timeout:'The connection request timed out. Try again.',
        bad_response:'Polymarket returned an unexpected response. Try again.',
        rate_limited:'Polymarket is receiving too many requests. Wait a moment and try again.',
        unavailable:'The trading connection is unavailable. Check your network and try again.',
      };
      return messages[error?.code] || 'The wallet request or trading connection was not completed. Try again.';
    }
    async function readJson(response) {
      if (!response.body?.getReader) return response.json();
      const reader=response.body.getReader(), decoder=new TextDecoder(); let source='',size=0;
      try {
        while (true) {
          const {done,value}=await reader.read(); if (done) break;
          size+=value.byteLength;
          if (size > 1048576) { void reader.cancel(); throw fail('bad_response'); }
          source+=decoder.decode(value,{stream:true});
        }
        source+=decoder.decode(); return JSON.parse(source);
      } finally { reader.releaseLock(); }
    }
    async function request(url,options,version,owner,allow400=false) {
      assertActive(version,owner);
      const controller=new AbortController(); requests.add(controller);
      let timer,abortHandler;
      try {
        const cancelled=new Promise((_,reject) => { abortHandler=() => reject(fail('stale')); controller.signal.addEventListener('abort',abortHandler,{once:true}); });
        const timeout=new Promise((_,reject) => { timer=setTimer(() => { reject(fail('timeout')); controller.abort(); },25000); });
        const fetchAndParse=(async () => {
          let response;
          try { response=await env.fetch(url,{...options,cache:'no-store',redirect:'error',signal:controller.signal}); } catch (_) { throw fail('unavailable'); }
          assertActive(version,owner);
          if (controller.signal.aborted) throw fail('stale');
          if (response.status === 400 && allow400) return {status:400,data:null};
          if (!response.ok) throw fail(({401:'unauthorized',403:'forbidden',429:'rate_limited'})[response.status] || 'unavailable');
          let data; try { data=await readJson(response); } catch (_) { throw fail('bad_response'); }
          assertActive(version,owner);
          if (controller.signal.aborted) throw fail('stale');
          return {status:response.status,data};
        })();
        return await Promise.race([fetchAndParse,timeout,cancelled]);
      } finally { clearTimer(timer); requests.delete(controller); controller.signal.removeEventListener('abort',abortHandler); }
    }
    function secretBytes(value) {
      if (typeof value !== 'string' || value.length < 16 || value.length > 512 || !/^[A-Za-z0-9+/_-]+={0,2}$/.test(value)) throw fail('credentials_invalid');
      try {
        const decoded=atob(value.replace(/-/g,'+').replace(/_/g,'/'));
        if (decoded.length < 16 || decoded.length > 256) throw fail('credentials_invalid');
        return Uint8Array.from(decoded,ch => ch.charCodeAt(0));
      } catch (_) { throw fail('credentials_invalid'); }
    }
    async function establish(owner,version) {
      if (!env.crypto?.subtle) throw fail('crypto_unavailable');
      emit({phase:'signing'});
      const timestamp=Math.floor(now()/1000); let signature;
      try { signature=await env.wallet.signClobAuth(authData(owner,timestamp)); }
      catch (error) { if (error?.code === 4001 || error?.code === 'ACTION_REJECTED') throw fail('signature_cancelled'); throw error; }
      assertActive(version,owner);
      if (typeof signature !== 'string' || !/^0x[a-f\d]{130}$/i.test(signature)) throw fail('signature_invalid');
      if (Math.abs(now()-timestamp*1000) > 90000) throw fail('timeout');
      const headers={POLY_ADDRESS:owner,POLY_SIGNATURE:signature,POLY_TIMESTAMP:String(timestamp),POLY_NONCE:'0'};
      let result=await request(CLOB+'/auth/api-key',{method:'POST',credentials:'omit',headers},version,owner,true);
      // This exact 400 fallback is the official create-or-derive behavior. Never
      // turn a forbidden, rate-limited or network response into another auth attempt.
      if (result.status === 400) result=await request(CLOB+'/auth/derive-api-key',{method:'GET',credentials:'omit',headers},version,owner);
      const data=result.data;
      if (!isObject(data) || !text(data.apiKey,200) || !text(data.passphrase,200)) throw fail('credentials_invalid');
      const bytes=secretBytes(data.secret);
      let key;
      try { key=await env.crypto.subtle.importKey('raw',bytes,{name:'HMAC',hash:'SHA-256'},false,['sign']); }
      finally { bytes.fill(0); data.secret=''; }
      assertActive(version,owner);
      credentials={apiKey:data.apiKey,passphrase:data.passphrase,key};
      clearTimer(expiryTimer);
      const expires=env.wallet.getState().expiresAt*1000 || now()+1800000;
      expiryTimer=setTimer(() => clearConnection('Your trading connection expired. Connect again to refresh it.'),Math.max(0,Math.min(expires-now(),1800000)));
    }
    async function privateRead(path,query,version,owner) {
      assertActive(version,owner);
      if (!credentials) throw fail('unauthorized');
      const saved=credentials, timestamp=String(Math.floor(now()/1000));
      const digest=await env.crypto.subtle.sign('HMAC',saved.key,new TextEncoder().encode(timestamp+'GET'+path));
      assertActive(version,owner);
      if (credentials !== saved) throw fail('stale');
      const signature=btoa(String.fromCharCode(...new Uint8Array(digest))).replace(/\+/g,'-').replace(/\//g,'_');
      const headers={POLY_ADDRESS:owner,POLY_API_KEY:saved.apiKey,POLY_PASSPHRASE:saved.passphrase,POLY_SIGNATURE:signature,POLY_TIMESTAMP:timestamp};
      return (await request(CLOB+path+(query ? '?'+new URLSearchParams(query).toString() : ''),{method:'GET',credentials:'omit',headers},version,owner)).data;
    }
    async function readOrders(account,version,owner) {
      const orders=[], cursors=new Set(), ids=new Set(); let cursor='MA==';
      for (let page=0;page<10;page++) {
        const data=await privateRead('/data/orders',{next_cursor:cursor},version,owner);
        if (!isObject(data) || !Array.isArray(data.data) || data.data.length > 500 || !text(data.next_cursor,300) || !/^[A-Za-z0-9+/_=-]+$/.test(data.next_cursor) || !Number.isSafeInteger(data.count) || data.count < 0 || data.count !== data.data.length || !Number.isSafeInteger(data.limit) || data.limit < 0 || data.limit > 1000) throw fail('orders_invalid');
        let overflow=false;
        for (const raw of data.data) {
          const order=orderFrom(raw,account);
          if (ids.has(order.id)) throw fail('orders_invalid');
          ids.add(order.id);
          if (orders.length < 2000) orders.push(order); else overflow=true;
        }
        if (overflow) return {orders,ordersComplete:false};
        if (data.next_cursor === END_CURSOR) return {orders,ordersComplete:true};
        if (!data.data.length || orders.length >= 2000 || cursors.has(data.next_cursor) || data.next_cursor === cursor) return {orders,ordersComplete:false};
        cursors.add(cursor); cursor=data.next_cursor;
      }
      return {orders,ordersComplete:false};
    }
    async function load(version,owner,account) {
      emit({phase:'loading',connected:false,balance:null,orders:[],ordersComplete:false,closedOnly:null,error:'',updatedAt:null});
      let deadline; let results;
      try {
        results=await Promise.race([Promise.all([
          privateRead('/balance-allowance',{asset_type:'COLLATERAL',signature_type:String(TYPES[account.wallet_type])},version,owner),
          readOrders(account,version,owner),
          privateRead('/auth/ban-status/closed-only',null,version,owner),
        ]),new Promise((_,reject) => { deadline=setTimer(() => reject(fail('timeout')),25000); })]);
      } finally { clearTimer(deadline); }
      const [balanceResult,ordersResult,closedResult]=results;
      assertActive(version,owner);
      const balance=balanceFrom(balanceResult);
      if (!isObject(closedResult) || typeof closedResult.closed_only !== 'boolean') throw fail('closed_invalid');
      emit({phase:'connected',connected:true,balance,...ordersResult,closedOnly:closedResult.closed_only,updatedAt:now(),error:''});
      return true;
    }
    function run(work) {
      if (destroyed) return Promise.resolve(false);
      if (pending) return pending;
      const version=lifecycle;
      const task=Promise.resolve().then(() => work(version)).catch(error => {
        // Provider events normally clear this state first. A passive wallet
        // recheck can also discover the change after an awaited operation.
        if (active(version) && error?.code === 'stale' && state.owner && !validWallet(env.wallet.getState(),state.owner)) {
          clearConnection('Your wallet or sign-in changed. Reconnect your trading account.');
        } else if (active(version) && error?.code !== 'stale') {
          ++lifecycle;
          credentials=null; clearTimer(expiryTimer); expiryTimer=null;
          for (const request of requests) request.abort();
          emit({phase:'error',connected:false,balance:null,orders:[],ordersComplete:false,closedOnly:null,updatedAt:null,error:readable(error)});
        }
        return false;
      }).finally(() => { if (pending === task) pending=null; });
      pending=task; return task;
    }
    function connect(options={}) {
      return run(async version => {
        const selected=options?.tradingWallet;
        if (selected !== undefined && selected !== null && selected !== '' && !address(selected)) throw fail('account_selection');
        credentials=null; clearTimer(expiryTimer); expiryTimer=null;
        emit({...blank(),phase:'checking'});
        let wallet=env.wallet.getState();
        if (wallet?.authenticated !== true || wallet.status !== 'connected') {
          if (!await env.wallet.connect()) throw fail('wallet_required');
          assertActive(version); wallet=env.wallet.getState();
        }
        if (wallet?.chainId !== 137) throw fail('polygon_required');
        if (!validWallet(wallet)) throw fail('wallet_required');
        const owner=wallet.address; emit({owner});
        if (!await env.wallet.checkSession()) throw fail('session_invalid');
        assertActive(version,owner);
        const {data}=await request(api+'/trading/accounts',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:'{}'},version,owner);
        const accounts=accountsFrom(data,owner); emit({accounts});
        if (!accounts.length) { emit({phase:'setup_required'}); return false; }
        let account=selected ? accounts.find(item => sameAddress(item.address,selected)) : accounts.length === 1 ? accounts[0] : null;
        if (selected && !account) throw fail('account_selection');
        if (!account) { emit({phase:'choosing'}); return false; }
        if (accounts.filter(item => item.wallet_type === account.wallet_type).length > 1) throw fail('account_ambiguous');
        emit({account}); await establish(owner,version); return load(version,owner,account);
      });
    }
    function refresh() {
      return run(async version => {
        const owner=state.owner, account=state.account;
        if (!owner || !account || !credentials) throw fail('unauthorized');
        if (!await env.wallet.checkSession()) throw fail('session_invalid');
        assertActive(version,owner);
        return load(version,owner,account);
      });
    }
    return {connect,refresh,disconnect:() => clearConnection(),getState:() => clone(state),
      subscribe(fn) { if (destroyed) return () => {}; subscribers.add(fn); fn(clone(state)); return () => subscribers.delete(fn); },
      destroy() { if (destroyed) return; clearConnection(); destroyed=true; unsubscribe?.(); subscribers.clear(); }};
  }
  const exports={createTradingConnection};
  if (typeof module !== 'undefined' && module.exports) module.exports=exports;
  if (typeof window !== 'undefined') window.OddsRailTradingConnection=exports;
})();
