(function () {
  'use strict';
  // Wallet connection identifies a public address. It is not a signed login.
  function createWallet(env) {
    const KEY = 'oddsrail.wallet.connected';
    let state = {address:'', status:'disconnected', error:'', portfolio:null, portfolioStatus:'idle', portfolioError:''};
    let provider = null, listeners = [], lifecycle = 0, requestVersion = 0, controller = null, pending = null;
    const subscribers = new Set();
    const validAddress = value => typeof value === 'string' && /^0x[a-fA-F0-9]{40}$/.test(value) && !/^0x0{40}$/i.test(value);
    function remember(connected) { try { if (connected) env.storage.setItem(KEY, 'true'); else env.storage.removeItem(KEY); } catch (_) {} }
    function emit(patch) { state = {...state, ...patch}; subscribers.forEach(fn => fn({...state})); }
    function cancelPortfolio() { ++requestVersion; if (controller) controller.abort(); controller = null; }
    function detach() { listeners.forEach(([event, handler]) => { try { provider?.removeListener?.(event, handler); } catch (_) {} }); listeners = []; provider = null; }
    function clear(error = '') {
      ++lifecycle; pending = null; cancelPortfolio(); detach(); remember(false);
      emit({address:'',status:'disconnected',error,portfolio:null,portfolioStatus:'idle',portfolioError:''});
    }
    async function refresh() {
      if (state.status !== 'connected' || !state.address) return;
      cancelPortfolio();
      const version = requestVersion, address = state.address;
      const request = new AbortController(); controller = request;
      emit({portfolio:null,portfolioStatus:'loading',portfolioError:''});
      const timer = env.setTimeout(() => request.abort(), 25000);
      try {
        const response = await env.fetch((env.api || 'https://mcp.oddsrail.app') + '/portfolio?address=' + encodeURIComponent(address), {signal:request.signal,credentials:'omit',cache:'no-store'});
        const data = await response.json();
        if (version !== requestVersion || address !== state.address || state.status !== 'connected') return;
        if (request.signal.aborted || !response.ok || !data || data.ok !== true || typeof data.address !== 'string' || data.address.toLowerCase() !== address.toLowerCase() || !data.summary || !data.account) throw new Error('unavailable');
        emit({portfolio:data,portfolioStatus:'ready',portfolioError:''});
      } catch (_) {
        if (version === requestVersion && address === state.address && state.status === 'connected') emit({portfolio:null,portfolioStatus:'error',portfolioError:'Account data is unavailable. Your wallet is still connected. Try refreshing.'});
      } finally { env.clearTimeout(timer); if (controller === request) controller = null; }
    }
    function accept(accounts) {
      const address = Array.isArray(accounts) && validAddress(accounts[0]) ? accounts[0] : '';
      if (!address) { clear(); return false; }
      cancelPortfolio(); remember(true);
      emit({address,status:'connected',error:'',portfolio:null,portfolioStatus:'idle',portfolioError:''});
      void refresh(); return true;
    }
    function attach(next) {
      detach(); provider = next;
      if (typeof next.on !== 'function') return;
      const generation = lifecycle;
      const onAccounts = accounts => { if (provider !== next || generation !== lifecycle) return; accept(accounts); };
      const onDisconnect = () => { if (provider === next && generation === lifecycle) clear('The wallet disconnected. Connect again when it is available.'); };
      const onChain = () => { if (provider === next && generation === lifecycle && state.status === 'connected') void refresh(); };
      listeners = [['accountsChanged',onAccounts],['disconnect',onDisconnect],['chainChanged',onChain]];
      listeners.forEach(([event, handler]) => next.on(event, handler));
    }
    function connect(interactive = true) {
      if (pending) return pending;
      if (state.status === 'connected') return Promise.resolve(true);
      let next; try { next = env.provider(); } catch (_) {}
      if (!next || typeof next.request !== 'function') {
        clear(interactive ? 'Open OddsRail in a wallet browser or enable your browser wallet extension. WalletConnect is not available yet.' : '');
        return Promise.resolve(false);
      }
      const version = ++lifecycle;
      emit({status:'connecting',error:'',address:'',portfolio:null,portfolioStatus:'idle',portfolioError:''});
      try { attach(next); } catch (_) { clear('The wallet could not connect. Check your wallet and try again.'); return Promise.resolve(false); }
      const task = Promise.resolve().then(async () => {
        try {
          const accounts = await next.request({method:interactive ? 'eth_requestAccounts' : 'eth_accounts'});
          if (version !== lifecycle || provider !== next) return false;
          // An account event can be newer than this request's response.
          if (state.status === 'connected') return true;
          return accept(accounts);
        } catch (error) {
          if (version !== lifecycle || provider !== next) return false;
          if (state.status === 'connected') return true;
          clear(interactive ? (error?.code === 4001 ? 'Connection cancelled. You can connect when you are ready.' : 'The wallet could not connect. Check your wallet and try again.') : '');
          return false;
        } finally { if (pending === task) pending = null; }
      });
      pending = task;
      return task;
    }
    function restore() { let known = false; try { known = env.storage.getItem(KEY) === 'true'; } catch (_) {} return known ? connect(false) : Promise.resolve(false); }
    return {connect:() => connect(true), disconnect:() => clear(), refresh, restore, getState:() => ({...state}), subscribe(fn) { subscribers.add(fn); fn({...state}); return () => subscribers.delete(fn); }};
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {createWallet};
  if (typeof window === 'undefined') return;
  window.OddsRailWallet = createWallet({provider:() => window.ethereum, get storage() { return window.sessionStorage; }, fetch:(...args) => window.fetch(...args), api:window.ODDSRAIL_API, setTimeout:window.setTimeout.bind(window), clearTimeout:window.clearTimeout.bind(window)});
  void window.OddsRailWallet.restore();
})();
