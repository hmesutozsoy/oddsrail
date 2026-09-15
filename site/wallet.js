(function () {
  'use strict';
  const STATEMENT = 'Sign in to OddsRail. This does not authorize trades or access to your funds.';
  const validAddress = value => typeof value === 'string' && /^0x[a-fA-F0-9]{40}$/.test(value) && !/^0x0{40}$/i.test(value);
  const sameAddress = (a, b) => validAddress(a) && validAddress(b) && a.toLowerCase() === b.toLowerCase();
  const chainNumber = value => typeof value === 'string' && /^0x[0-9a-f]+$/i.test(value) ? Number.parseInt(value, 16) : NaN;
  const CLOB_STATEMENT = 'This message attests that I control the given wallet';
  const CLOB_FIELDS = [{name:'address',type:'address'},{name:'timestamp',type:'string'},{name:'nonce',type:'uint256'},{name:'message',type:'string'}];
  const DOMAIN_FIELDS = [{name:'name',type:'string'},{name:'version',type:'string'},{name:'chainId',type:'uint256'}];
  const exactKeys = (value, keys) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
  function validateClobAuth(data, owner, now) {
    if (!exactKeys(data,['domain','types','primaryType','message']) || data.primaryType !== 'ClobAuth' || !exactKeys(data.domain,['name','version','chainId']) || data.domain.name !== 'ClobAuthDomain' || data.domain.version !== '1' || data.domain.chainId !== 137 || !exactKeys(data.message,['address','timestamp','nonce','message']) || !sameAddress(data.message.address,owner) || data.message.message !== CLOB_STATEMENT || ![0,'0'].includes(data.message.nonce) || typeof data.message.timestamp !== 'string' || !/^\d{10}$/.test(data.message.timestamp)) return false;
    if (!exactKeys(data.types,['ClobAuth']) && !exactKeys(data.types,['ClobAuth','EIP712Domain'])) return false;
    if (JSON.stringify(data.types.ClobAuth) !== JSON.stringify(CLOB_FIELDS) || (Object.prototype.hasOwnProperty.call(data.types,'EIP712Domain') && JSON.stringify(data.types.EIP712Domain) !== JSON.stringify(DOMAIN_FIELDS))) return false;
    return Math.abs(Number(data.message.timestamp)*1000-now) <= 90000;
  }
  function validateChallenge(data, address, chain, origin, now) {
    if (!data || data.ok !== true || typeof data.message !== 'string' || data.message.length > 1500) return false;
    const lines = data.message.split('\n');
    if (lines.length !== 11 || lines[0] !== origin + ' wants you to sign in with your Ethereum account:' || !sameAddress(lines[1], address) || lines[2] !== '' || lines[3] !== STATEMENT || lines[4] !== '' || lines[5] !== 'URI: ' + origin + '/' || lines[6] !== 'Version: 1' || lines[7] !== 'Chain ID: ' + chain || !/^Nonce: [a-f0-9]{32}$/.test(lines[8]) || !/^Issued At: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(lines[9]) || !/^Expiration Time: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(lines[10])) return false;
    const issued = Date.parse(lines[9].slice(11)), expires = Date.parse(lines[10].slice(17));
    return Number.isInteger(data.expires_at) && expires === data.expires_at * 1000 && expires - issued === 300000 && issued <= now + 60000 && issued >= now - 360000 && expires > now;
  }
  function createWallet(env) {
    const KEY = 'oddsrail.wallet.connected', now = env.now || Date.now;
    let state = {address:'', status:'disconnected', authenticated:false, chainId:null, expiresAt:null, error:'', portfolio:null, portfolioStatus:'idle', portfolioError:''};
    let provider = null, listeners = [], lifecycle = 0, requestVersion = 0, controller = null, pending = null;
    let candidate = '', candidateChain = null, expiryTimer = null, authTail = Promise.resolve(), rechecking = null, tradingPrompt = false;
    const subscribers = new Set(), active = version => version === lifecycle;
    function remember(connected) { try { if (connected) env.storage.setItem(KEY, 'true'); else env.storage.removeItem(KEY); } catch (_) {} }
    function emit(patch) { state = {...state, ...patch}; subscribers.forEach(fn => fn({...state})); }
    function cancelPortfolio() { ++requestVersion; if (controller) controller.abort(); controller = null; }
    function detach() { listeners.forEach(([event, handler]) => { try { provider?.removeListener?.(event, handler); } catch (_) {} }); listeners = []; provider = null; }
    function clear(error = '') {
      ++lifecycle; pending = null; rechecking = null; candidate = ''; candidateChain = null;
      cancelPortfolio(); detach(); remember(false); env.clearTimeout(expiryTimer); expiryTimer = null;
      emit({address:'',status:'disconnected',authenticated:false,chainId:null,expiresAt:null,error,signOutPending:false,portfolio:null,portfolioStatus:'idle',portfolioError:''});
    }
    // Serialize auth requests: an old verification response must finish before
    // this tab's logout or a subsequent sign-in can change the session cookie.
    function auth(path, body, version) {
      const task = authTail.catch(() => {}).then(async () => {
        if (version !== undefined && !active(version)) throw new Error('stale');
        const request = new AbortController(), timer = env.setTimeout(() => request.abort(), 25000);
        try {
          const response = await env.fetch((env.api || 'https://mcp.oddsrail.app') + '/auth/wallet/' + path, {
            method:body === undefined ? 'GET' : 'POST', credentials:'include', cache:'no-store', signal:request.signal,
            ...(body === undefined ? {} : {headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
          });
          const data = await response.json();
          if (request.signal.aborted) throw new Error('auth_timeout');
          if (!response.ok || !data || data.ok !== true) { const error = new Error('auth_failed'); error.status = response.status; throw error; }
          return data;
        } finally { env.clearTimeout(timer); }
      });
      authTail = task.catch(() => {}); return task;
    }
    function notify() { try { env.notify?.(); } catch (_) {} }
    function disconnect(error = '') {
      clear(error); const version = lifecycle;
      return auth('logout', {}).then(() => { notify(); return true; }).catch(() => {
        if (active(version)) emit({error:'Disconnected here, but server sign-out could not be confirmed. Retry sign-out to revoke the session.',signOutPending:true});
        return false;
      });
    }
    function validSession(data, address, chain) {
      return data?.ok === true && data.authenticated === true && sameAddress(data.address, address) && data.chain_id === chain && Number.isInteger(data.expires_at) && data.expires_at * 1000 > now() && data.expires_at * 1000 <= now() + 8 * 3600000 + 60000;
    }
    async function refreshPortfolio() {
      if (state.status !== 'connected' || !state.authenticated || !state.address) return;
      cancelPortfolio(); const version = requestVersion, address = state.address;
      const request = new AbortController(); controller = request;
      emit({portfolio:null,portfolioStatus:'loading',portfolioError:''});
      const timer = env.setTimeout(() => request.abort(), 25000);
      try {
        const response = await env.fetch((env.api || 'https://mcp.oddsrail.app') + '/portfolio?address=' + encodeURIComponent(address), {signal:request.signal,credentials:'omit',cache:'no-store'});
        const data = await response.json();
        if (version !== requestVersion || !sameAddress(address, state.address) || state.status !== 'connected') return;
        if (request.signal.aborted || !response.ok || !data || data.ok !== true || !sameAddress(data.address, address) || !data.summary || !data.account) throw new Error('unavailable');
        emit({portfolio:data,portfolioStatus:'ready',portfolioError:''});
      } catch (_) {
        if (version === requestVersion && sameAddress(address, state.address) && state.status === 'connected') emit({portfolio:null,portfolioStatus:'error',portfolioError:'Account data is unavailable. You are still signed in. Try refreshing.'});
      } finally { env.clearTimeout(timer); if (controller === request) controller = null; }
    }
    function accept(data) {
      remember(true); candidate = data.address; candidateChain = data.chain_id;
      emit({address:data.address,status:'connected',authenticated:true,chainId:data.chain_id,expiresAt:data.expires_at,error:'',signOutPending:false,portfolio:null,portfolioStatus:'idle',portfolioError:''});
      env.clearTimeout(expiryTimer);
      expiryTimer = env.setTimeout(() => { clear('Your sign-in expired. Connect your wallet to sign in again.'); }, Math.max(0, data.expires_at * 1000 - now()));
      void refreshPortfolio(); return true;
    }
    function attach(next) {
      detach(); provider = next;
      if (typeof next.on !== 'function') return;
      const generation = lifecycle;
      const onAccounts = accounts => {
        if (provider !== next || !active(generation)) return;
        const address = Array.isArray(accounts) ? accounts[0] : '';
        // Some wallets emit the initial account before their prompt resolves.
        if (!candidate && state.status === 'connecting' && validAddress(address)) { candidate = address; return; }
        if (sameAddress(address, candidate)) return;
        void disconnect('The wallet account changed. Connect again to sign in with the selected account.');
      };
      const onDisconnect = () => { if (provider === next && active(generation)) void disconnect('The wallet disconnected. Connect again when it is available.'); };
      const onChain = value => {
        if (provider !== next || !active(generation)) return;
        const chain = chainNumber(value);
        if (candidateChain === null && state.status === 'connecting') return;
        if (chain !== candidateChain) void disconnect('The wallet network changed. Connect again to verify your sign-in.');
      };
      listeners = [['accountsChanged',onAccounts],['disconnect',onDisconnect],['chainChanged',onChain]];
      listeners.forEach(([event, handler]) => next.on(event, handler));
    }
    async function checkSession() {
      if (rechecking) return rechecking;
      if (state.status !== 'connected') return false;
      const version = lifecycle, address = state.address, chain = state.chainId;
      const task = (async () => {
        try {
          const data = await auth('session', undefined, version);
          if (!active(version)) return false;
          if (!validSession(data, address, chain)) { clear('Your sign-in ended or changed in another tab. Connect again to sign in.'); return false; }
          return true;
        } catch (_) { if (active(version)) clear('Your sign-in could not be verified. Connect again to retry.'); return false; }
        finally { if (rechecking === task) rechecking = null; }
      })();
      rechecking = task; return task;
    }
    // A narrowly scoped CLOB credential signature, never a generic signer.
    // Orders, approvals and delegated permissions need separate reviewed flows.
    async function signClobAuth(value) {
      if (tradingPrompt) throw new Error('A wallet request is already open. Finish it before trying again.');
      const version=lifecycle, owner=state.address, next=provider;
      if (!state.authenticated || state.status !== 'connected' || !next) throw new Error('Sign in with your wallet first.');
      if (state.chainId !== 137) throw new Error('Switch your wallet to Polygon, then reconnect to OddsRail.');
      let data;
      try { data=JSON.parse(JSON.stringify(value)); } catch (_) {}
      if (!validateClobAuth(data,owner,now())) throw new Error('The Polymarket connection message could not be verified.');
      // RPC wallets do not add EIP712Domain the way higher-level SDKs do.
      data.types.EIP712Domain=DOMAIN_FIELDS.map(field=>({...field}));
      tradingPrompt=true;
      const current=()=>active(version)&&provider===next&&state.authenticated&&state.status==='connected'&&sameAddress(state.address,owner)&&state.chainId===137;
      async function selected() {
        const accounts=await next.request({method:'eth_accounts'}), chain=chainNumber(await next.request({method:'eth_chainId'}));
        return current() && sameAddress(accounts?.[0],owner) && chain===137;
      }
      try {
        if (!await checkSession() || !await selected() || !current()) throw new Error('Your wallet or sign-in changed. Reconnect and try again.');
        if (!validateClobAuth(data,owner,now())) throw new Error('The connection request expired. Try again.');
        const signature=await next.request({method:'eth_signTypedData_v4',params:[owner,JSON.stringify(data)]});
        if (!await checkSession() || !await selected() || !current()) throw new Error('Your wallet or sign-in changed. No trading connection was retained.');
        if (!validateClobAuth(data,owner,now())) throw new Error('The connection request expired. Try again.');
        if (typeof signature !== 'string' || !/^0x[a-f0-9]{130}$/i.test(signature)) throw new Error('This wallet signature is not supported.');
        return signature;
      } finally { tradingPrompt=false; }
    }
    function connect(interactive = true) {
      if (pending) return pending;
      if (state.status === 'connected') return checkSession();
      let next; try { next = env.provider(); } catch (_) {}
      if (!next || typeof next.request !== 'function') {
        clear(interactive ? 'Open OddsRail in a wallet browser or enable your browser wallet extension. WalletConnect is not available yet.' : '');
        return Promise.resolve(false);
      }
      const version = ++lifecycle; candidate = ''; candidateChain = null;
      emit({status:'connecting',authenticated:false,error:'',signOutPending:false,address:'',chainId:null,expiresAt:null,portfolio:null,portfolioStatus:'idle',portfolioError:''});
      try { attach(next); } catch (_) { clear('The wallet could not connect. Check your wallet and try again.'); return Promise.resolve(false); }
      const task = Promise.resolve().then(async () => {
        try {
          const accounts = await next.request({method:interactive ? 'eth_requestAccounts' : 'eth_accounts'});
          if (!active(version)) return false;
          const address = Array.isArray(accounts) ? accounts[0] : '';
          if (!validAddress(address) || (candidate && !sameAddress(address, candidate))) { clear(); return false; }
          candidate = address;
          const chain = chainNumber(await next.request({method:'eth_chainId'}));
          if (!active(version)) return false;
          if (![1,137].includes(chain)) { clear(interactive ? 'Select Ethereum or Polygon in your wallet, then connect again.' : ''); return false; }
          candidateChain = chain;
          const existing = await auth('session', undefined, version);
          if (!active(version)) return false;
          if (validSession(existing, address, chain)) return accept(existing);
          if (!interactive) { clear(); return false; }
          const challenge = await auth('challenge', {address,chain_id:chain}, version);
          if (!active(version)) return false;
          if (!validateChallenge(challenge, address, chain, env.origin, now())) throw new Error('unsafe_challenge');
          emit({status:'signing'});
          const hex = '0x' + Array.from(new TextEncoder().encode(challenge.message), b => b.toString(16).padStart(2,'0')).join('');
          const signature = await next.request({method:'personal_sign',params:[hex,address]});
          if (!active(version)) return false;
          if (typeof signature !== 'string' || !/^0x[a-f0-9]{130}$/i.test(signature)) throw new Error('unsupported_signature');
          // Check again after the prompt, even if a wallet omitted change events.
          const selected = await next.request({method:'eth_accounts'});
          const selectedChain = chainNumber(await next.request({method:'eth_chainId'}));
          if (!active(version)) return false;
          if (!sameAddress(selected?.[0], address) || selectedChain !== chain) { await disconnect('The wallet account or network changed. Connect again to sign in.'); return false; }
          emit({status:'verifying'});
          const signed = await auth('verify', {message:challenge.message,signature}, version);
          if (!active(version)) return false;
          if (!validSession(signed, address, chain)) throw new Error('unverified');
          // Require proof that the browser actually retained the session cookie.
          const confirmed = await auth('session', undefined, version);
          if (!active(version)) return false;
          if (!validSession(confirmed, address, chain)) throw new Error('cookies_blocked');
          notify(); return accept(confirmed);
        } catch (error) {
          if (!active(version)) return false;
          const message = !interactive ? '' : error?.code === 4001 ? 'Sign-in cancelled. You can connect when you are ready.' : error?.message === 'unsafe_challenge' ? 'The sign-in message could not be verified. No signature was requested. Reload and try again.' : error?.message === 'cookies_blocked' ? 'Your browser did not keep the sign-in session. Allow essential cookies for OddsRail and try again.' : error?.message === 'unsupported_signature' ? 'This wallet signature is not supported. Use an Ethereum or Polygon wallet account that can sign messages.' : error?.status === 429 ? 'Too many sign-in attempts. Wait a little and try again.' : 'Wallet sign-in could not be completed. Check your wallet and connection, then try again.';
          clear(message);
          // Revoke a session that may have been created before a lost reply.
          void auth('logout', {}).catch(() => {}); return false;
        } finally { if (pending === task) pending = null; }
      });
      pending = task; return task;
    }
    function restore() { let known = false; try { known = env.storage.getItem(KEY) === 'true'; } catch (_) {} return known ? connect(false) : Promise.resolve(false); }
    return {connect:() => connect(true), disconnect, refresh:async () => { if (await checkSession()) await refreshPortfolio(); }, restore, checkSession, signClobAuth, getState:() => ({...state}), subscribe(fn) { subscribers.add(fn); fn({...state}); return () => subscribers.delete(fn); }};
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {createWallet, validateChallenge, validateClobAuth};
  if (typeof window === 'undefined') return;
  let channel; try { channel = new BroadcastChannel('oddsrail.wallet.auth'); } catch (_) {}
  const wallet = window.OddsRailWallet = createWallet({provider:() => window.ethereum, get storage() { return window.sessionStorage; }, fetch:(...args) => window.fetch(...args), api:window.ODDSRAIL_API, origin:window.location.origin, setTimeout:window.setTimeout.bind(window), clearTimeout:window.clearTimeout.bind(window), notify:() => channel?.postMessage('changed')});
  if (channel) channel.onmessage = () => { void wallet.checkSession(); };
  window.addEventListener('focus', () => { void wallet.checkSession(); });
  void wallet.restore();
})();
