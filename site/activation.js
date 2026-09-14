(function () {
  'use strict';
  const IDS = ['wallet','configuration','trading_account','funding','permission','execution'];
  const STATUSES = ['ready','action_required','unavailable'];
  const address = value => typeof value === 'string' && /^0x[0-9a-f]{40}$/i.test(value);
  const identity = state => state?.authenticated === true && state.status === 'connected' && address(state.address) ? state.address.toLowerCase() + ':' + state.chainId : '';
  function validReport(data, owner) {
    if (!data || data.ok !== true || data.can_activate !== false || !address(data.address) || data.address.toLowerCase() !== owner.toLowerCase() || !Array.isArray(data.checks) || data.checks.length !== IDS.length || !Array.isArray(data.blockers) || data.blockers.length > IDS.length) return false;
    const seen = new Set();
    for (const check of data.checks) {
      if (!check || !IDS.includes(check.id) || seen.has(check.id) || !STATUSES.includes(check.status) || typeof check.label !== 'string' || check.label.length > 100 || typeof check.detail !== 'string' || check.detail.length > 800 || (check.href && check.href !== 'https://polymarket.com' && check.href !== 'https://polymarket.com/')) return false;
      seen.add(check.id);
    }
    const blockers = data.checks.filter(c => c.status !== 'ready').map(c => c.id);
    if (new Set(data.blockers).size !== data.blockers.length || data.blockers.length !== blockers.length || data.blockers.some(id=>typeof id!=='string'||!blockers.includes(id))) return false;
    return data.checks.find(c => c.id === 'wallet').status === 'ready' && data.checks.find(c => c.id === 'configuration').status === 'ready' && data.checks.find(c => c.id === 'funding').status !== 'ready' && data.checks.find(c => c.id === 'permission').status !== 'ready' && data.checks.find(c => c.id === 'execution').status !== 'ready';
  }
  function createActivationChecker(env) {
    let state = {phase:'idle',report:null,error:''}, revision = 0, request = null;
    let currentIdentity = identity(env.wallet?.getState()), stopped = false;
    const subscribers = new Set();
    const emit = patch => { state={...state,...patch}; subscribers.forEach(fn=>fn({...state})); };
    function invalidate() { ++revision; request?.abort(); request=null; if (!stopped) emit({phase:'idle',report:null,error:''}); }
    const unsubscribeWallet = env.wallet?.subscribe(next => {
      const changed = currentIdentity !== identity(next); currentIdentity = identity(next);
      if (changed && state.phase !== 'connecting') invalidate();
    });
    async function check() {
      if (stopped || ['connecting','checking'].includes(state.phase)) return false;
      const error = env.validate?.();
      if (error) { invalidate(); emit({phase:'error',error}); return false; }
      const config = JSON.stringify(env.getConfig()), version = ++revision;
      emit({phase:'connecting',report:null,error:''});
      try {
        if (!env.wallet) throw Error('Wallet sign-in is unavailable. Reload and try again.');
        if (!identity(env.wallet.getState()) && !await env.wallet.connect()) throw Error('Sign in with your wallet before checking activation.');
        if (stopped || version !== revision) return false;
        const walletState = env.wallet.getState(), owner = walletState.address, selectedIdentity = identity(walletState);
        if (!selectedIdentity) throw Error('Your wallet sign-in could not be verified. Connect again.');
        emit({phase:'checking'});
        const controller = new AbortController(); request=controller;
        const timer = env.setTimeout(()=>controller.abort(),25000);
        try {
          const response = await env.fetch((env.api || 'https://mcp.oddsrail.app') + '/activation/check', {
            method:'POST',credentials:'include',cache:'no-store',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({config:JSON.parse(config)}),signal:controller.signal
          });
          let data;
          try { data = await response.json(); } catch (_) { data = null; }
          if (stopped || version !== revision || selectedIdentity !== identity(env.wallet.getState())) return false;
          if (controller.signal.aborted) throw Error('The activation check timed out. Try again.');
          if (!response.ok || data?.ok !== true) {
            if (response.status === 401) { void env.wallet.checkSession?.(); throw Error('Your sign-in expired. Connect again to check activation.'); }
            if (response.status === 429 || response.status === 503) throw Error('Activation checks are busy. Try again shortly.');
            throw Error(typeof data?.detail === 'string' ? data.detail.slice(0,400) : 'The activation check is unavailable. Reload and try again.');
          }
          if (!validReport(data,owner)) throw Error('The activation result could not be verified. No agent was activated.');
          emit({phase:'blocked',report:data,error:''}); return true;
        } finally { env.clearTimeout(timer); if(request===controller)request=null; }
      } catch(error) {
        if (!stopped && version===revision) emit({phase:'error',report:null,error:error?.name==='AbortError'?'The activation check timed out. Try again.':error?.message || 'Activation could not be checked. Try again.'});
        return false;
      }
    }
    return {check,invalidate,getState:()=>({...state}),subscribe(fn){subscribers.add(fn);fn({...state});return()=>subscribers.delete(fn);},destroy(){stopped=true;invalidate();unsubscribeWallet?.();subscribers.clear();}};
  }
  if (typeof module !== 'undefined' && module.exports) module.exports={createActivationChecker,validReport};
  if (typeof window !== 'undefined') window.OddsRailActivation={createActivationChecker};
})();
