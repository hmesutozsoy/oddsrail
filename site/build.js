(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const form = $('setup-form');
  const API = window.ODDSRAIL_API || 'https://mcp.oddsrail.app';
  const DRAFTS = 'oddsrail-drafts-v2';
  const login = location.hash.match(/^#session=([A-Za-z0-9_-]{20,})$/);
  if(login){try{localStorage.setItem('oddsrail-session',login[1]);history.replaceState(null,'',location.pathname+location.search);location.replace('/agents');return;}catch(e){history.replaceState(null,'',location.pathname+location.search);}}
  const strategies = [
    {id:'mm', name:'Quote both sides', icon:'⇄', text:'Place a buy quote on each outcome, with the share size you choose.', params:[['shares','Shares per outcome',20,5,500,1],['edge','Price distance (cents)',2,.5,20,.5]]},
    {id:'fade', name:'Fade sharp moves', icon:'↘', text:'Find recent jumps with a history of reverting.', params:[['jump','Minimum price move (cents)',8,1,90,1],['hours','Lookback (hours)',6,1,24,1]]},
    {id:'settle', name:'High-probability outcomes', icon:'◎', text:'Buy likely outcomes inside your price range.', params:[['lo','Minimum price ($ per share)',.9,.5,.96,.01],['hi','Maximum price ($ per share)',.96,.51,.969,.001]]},
    {id:'value', name:'Trade my forecast', icon:'◈', text:'Trade gaps between your probability and the market.', params:[['edge','Minimum price edge (cents)',5,.5,50,.5],['kelly','Kelly fraction',.25,.05,1,.05]]},
    {id:'momentum', name:'Follow momentum', icon:'↗', text:'Follow price moves above your volume floor.', params:[['move','Minimum price move (cents)',10,1,90,1],['hours','Lookback (hours)',6,1,24,1]]}
  ];
  const risks = [
    ['stoploss','Stop loss','pct',25,1,95,'%',true],
    ['takeprofit','Take profit','pct',40,1,500,'%',false],
    ['daily','Daily loss entry limit','usd',30,1,100000,'$',true],
    ['expo','Exposure per market','usd',100,1,100000,'$',true],
    ['noadd','Never add to a losing position',null,null,null,null,'',true],
    ['dispute','Maximum dispute-risk score','score',20,0,100,'/ 100',true],
    ['liquidity','Maximum price slippage','slip',2,.1,20,'%',true],
    ['watch','Recheck book after waiting','sec',20,1,60,'sec',false]
  ];
  const capitals = [['bankroll','Allocated capital ($)',1000,10,100000,1],['perorder','Maximum per order ($)',25,1,500,1],['maxpos','Maximum open positions',5,1,50,1],['minvol','Minimum 24h volume ($)',20000,0,1e12,1],['closing','Avoid markets closing within (hours)',2,0,8760,.5]];
  const categories = [['all','All markets'],['crypto','Crypto'],['politics','Politics'],['geopolitics','Geopolitics'],['economy','Economy'],['business','Business'],['soccer','Football'],['esports','Esports'],['tennis','Tennis'],['us-sports','US sports'],['motorsport','Motorsport']];
  function el(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
  function field(name, label, value, min, max, step) {
    const l = el('label', label, 'field'); const i = el('input'); Object.assign(i, {type:'number',name,value,min,max,step:step || 'any',required:true,className:'input'}); l.append(i); return l;
  }
  const input = name => form.elements.namedItem(name);
  const checked = name => !!input(name).checked;
  strategies.forEach(s => {
    const card=el('div',undefined,'strategy-option');
    const label=el('label',undefined,'strategy-card'), checkbox=el('input');
    Object.assign(checkbox,{type:'checkbox',name:'on_'+s.id,checked:s.id==='mm'});
    checkbox.setAttribute('aria-controls','strategy-settings-'+s.id);
    const title=el('strong',s.name);title.id='strategy-title-'+s.id;
    const copy=el('span');copy.append(el('span',s.icon,'strategy-glyph'),title,el('small',s.text));label.append(checkbox,copy);
    const params=el('div',undefined,'strategy-controls');params.dataset.params=s.id;params.id='strategy-settings-'+s.id;
    params.setAttribute('role','group');params.setAttribute('aria-labelledby',title.id);
    const grid=el('div',undefined,'field-grid');
    s.params.forEach(a=>grid.append(field(s.id+'_'+a[0],...a.slice(1))));
    window.OddsRailStrategyVisuals?.attach(params,s.id);
    params.append(grid);
    if(s.id==='mm'){
      const example=el('p','','muted small');example.id='quote-example';params.append(example);
      params.append(el('p','Share size applies to each outcome. Price distance sets how far below the midpoint to bid. Dollar limits can reduce the order size.','muted small'));
    }
    if(s.id==='value'){
      const fieldLabel=el('label','Your forecasts: market title, then probability','field'), text=el('textarea');
      Object.assign(text,{name:'value_views',rows:3,maxLength:2000,className:'input',placeholder:'Market title: 0.65'});
      fieldLabel.append(text);params.append(fieldLabel,el('p','Use a probability between 0 and 1. The agent must resolve the market before trading.','muted small'));
    }
    card.append(label,params);$('strategy-choices').append(card);
  });
  capitals.forEach(a=>$('capital-fields').append(field(...a)));
  categories.forEach(([id,name])=>{const l=el('label'),i=el('input'); Object.assign(i,{name:'topic_'+id,type:'checkbox',checked:id==='all'}); l.append(i,document.createTextNode(name));$('category-choices').append(l);});
  risks.forEach(([id,name,key,value,min,max,unit,on])=>{
    const row=el('div',undefined,'risk-row'),i=el('input');Object.assign(i,{type:'checkbox',name:'on_'+id,id:'risk-'+id,checked:on});const label=el('label',name);label.htmlFor=i.id;row.append(i,label);
    if(key){const box=el('div',undefined,'risk-value'),v=el('input');Object.assign(v,{type:'number',name:id+'_'+key,value,min,max,step:'any',required:true,className:'input'});v.setAttribute('aria-label',name+' ('+unit+')');box.append(v,el('span',unit));row.append(box);} $('risk-fields').append(row);
  });
  let stage=0, selected=[], busy=false, searchRevision=0;
  const url=new URL(location.href);
  let draftId=url.searchParams.get('draft') || crypto.randomUUID();
  if(!/^[a-zA-Z0-9-]{1,64}$/.test(draftId))draftId=crypto.randomUUID();
  function loadDrafts(){try{const d=JSON.parse(localStorage.getItem(DRAFTS)||'[]');return Array.isArray(d)?d.filter(x=>x&&typeof x.id==='string'&&x.config&&typeof x.config==='object'&&!Array.isArray(x.config)):[];}catch(e){return [];}}
  function read(){
    const c={schema_version:2,mode:'draft',market_mode:input('market_mode').value,market_ids:[],on:{report:true},vals:{},topics:[],keyword:input('keyword').value.trim()};
    capitals.forEach(([id])=>c[id]=Number(input(id).value));
    strategies.forEach(s=>{c.on[s.id]=checked('on_'+s.id);c.vals[s.id]={};s.params.forEach(([id])=>c.vals[s.id][id]=Number(input(s.id+'_'+id).value));});
    c.vals.value.views=input('value_views').value.trim();
    risks.forEach(([id,,key])=>{c.on[id]=checked('on_'+id);c.vals[id]=key?{[key]:Number(input(id+'_'+key).value)}:{};});
    categories.forEach(([id])=>{if(checked('topic_'+id))c.topics.push(id);});
    if(c.market_mode==='specific')c.market_ids=[...new Set(selected.flatMap(m=>m.outcomes.map(o=>o.token_id)))];
    return c;
  }
  function restore(d){
    const c=d.config;if(!c||typeof c!=='object')return;
    input('name').value=d.name||'My agent';input('description').value=d.description||'';input('runner').value=d.runner==='external'?'external':'hosted';
    capitals.forEach(([id])=>{if(c[id]!==undefined)input(id).value=c[id];});
    [...strategies.map(s=>s.id),...risks.map(r=>r[0])].forEach(id=>{input('on_'+id).checked=!!(c.on&&c.on[id]);Object.entries((c.vals||{})[id]||{}).forEach(([k,v])=>{const node=input(id+'_'+k);if(node)node.value=v;});});
    categories.forEach(([id])=>input('topic_'+id).checked=(Array.isArray(c.topics)?c.topics:['all']).includes(id));input('keyword').value=c.keyword||'';
    selected=Array.isArray(d.markets)?d.markets.filter(m=>m&&typeof m.title==='string'&&Array.isArray(m.outcomes)&&m.outcomes.every(o=>o&&typeof o.token_id==='string')):[];
    input('market_mode').value=c.market_mode==='specific'?'specific':'universe';
  }
  const saved=loadDrafts().find(d=>d.id===draftId);if(saved)restore(saved);
  function validate(){
    const c=read();if(!input('name').value.trim())return 'Give your agent a name.';
    if(!strategies.some(s=>c.on[s.id]))return 'Select at least one strategy.';
    const invalid=[...form.querySelectorAll('input[type=number]')].find(n=>!n.disabled&&!n.validity.valid);
    if(invalid){const label=invalid.closest('label');return 'Check '+(invalid.getAttribute('aria-label')||(label?label.textContent:'numeric settings'))+'.';}
    if(c.market_mode==='specific'&&!c.market_ids.length)return 'Add at least one specific market.';
    if(c.market_ids.length>20)return 'Select at most 20 outcomes.';
    if(c.market_mode==='universe'&&!c.topics.length&&!c.keyword)return 'Choose a category or enter a market keyword.';
    if(c.perorder>c.bankroll)return 'The order limit cannot exceed allocated capital.';
    if(c.on.expo&&c.perorder>c.vals.expo.usd)return 'The order limit cannot exceed per-market exposure.';
    if(c.on.settle&&c.vals.settle.lo>=c.vals.settle.hi)return 'The minimum entry price must be below the maximum.';
    if(c.on.value&&!c.vals.value.views)return 'Add your forecast in the strategy parameters.';
    return '';
  }
  const money = n => '$'+Number(n).toLocaleString('en-US',{maximumFractionDigits:2});
  function scopeLabel(c){return c.market_mode==='specific'?selected.length+' selected market'+(selected.length===1?'':'s'):c.topics.map(id=>(categories.find(x=>x[0]===id)||[id,id])[1]).concat(c.keyword?[c.keyword]:[]).join(', ')||'Choose markets';}
  function sync(){
    const c=read();
    strategies.forEach(s=>{const p=form.querySelector('[data-params='+s.id+']');p.hidden=!c.on[s.id];p.querySelectorAll('input,textarea').forEach(n=>n.disabled=!c.on[s.id]);});
    risks.forEach(([id,,key])=>{if(key)input(id+'_'+key).disabled=!c.on[id];});
    $('specific-fields').hidden=c.market_mode!=='specific';$('universe-fields').hidden=c.market_mode==='specific';
    $('summary-name').textContent=input('name').value.trim()||'Untitled agent';$('summary-strategies').replaceChildren(...strategies.filter(s=>c.on[s.id]).map(s=>el('span',s.name)));
    $('summary-market').textContent=scopeLabel(c);$('summary-order').textContent=money(c.perorder);$('summary-exposure').textContent=c.on.expo?money(c.vals.expo.usd):'Not set';$('summary-daily').textContent=c.on.daily?money(c.vals.daily.usd):'Not set';
    $('external-runner').hidden=input('runner').value!=='external';
    $('summary-quote').hidden=!c.on.mm;
    $('summary-shares').textContent=c.vals.mm.shares+' per outcome';
    $('quote-example').textContent='Example: at a 50¢ midpoint, each '+c.vals.mm.shares+'-share bid is priced at '+Number(50-c.vals.mm.edge).toFixed(1)+'¢.';
  }
  function show(next){stage=next;document.querySelectorAll('[data-stage]').forEach(s=>s.hidden=Number(s.dataset.stage)!==stage);document.querySelectorAll('.setup-steps button').forEach(b=>{if(Number(b.dataset.step)===stage)b.setAttribute('aria-current','step');else b.removeAttribute('aria-current');});$('setup-error').textContent='';}
  async function request(path,opts){
    const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),25000);
    try{const r=await fetch(API+path,{...opts,signal:controller.signal});let j;try{j=await r.json();}catch(e){throw Error('This feature needs the updated OddsRail service. Try again after the service is available.');}if(!r.ok||!j.ok)throw Error(j.error||'The service could not complete this request.');return j;}finally{clearTimeout(timeout);}
  }
  function json(body){return {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)};}
  function review(n){
    const c=read(), lines=[];
    if(c.market_mode==='specific'){lines.push('Only selected markets: '+selected.map(m=>m.title).join('; ')+'. Both listed outcomes are allowed.');}else lines.push('Scan '+scopeLabel(c)+'.');
    if(n.on.mm)lines.push('Quote up to '+n.mm.shares+' shares per side, '+(n.mm.edge*100).toFixed(1)+'¢ below the midpoint, subject to shared limits.');
    if(n.on.fade)lines.push('Fade jumps of at least '+(n.fade.jump*100).toFixed(1)+'¢ over '+n.fade.hours+' hours when historical reversion qualifies.');
    if(n.on.settle)lines.push('Consider likely outcomes priced from '+(n.settle.lo*100).toFixed(1)+'% to '+(n.settle.hi*100).toFixed(1)+'%, subject to resolution and upside checks.');
    if(n.on.value)lines.push('Use your forecast text: '+n.value.views+'. Require '+(n.value.edge*100).toFixed(1)+'¢ of price edge and '+n.value.kelly+' Kelly sizing.');
    if(n.on.momentum)lines.push('Follow moves of at least '+(n.momentum.move*100).toFixed(1)+'¢ over '+n.momentum.hours+' hours.');
    lines.push('Allocate '+money(n.bankroll)+'; cap each order at '+money(n.perorder)+' with at most '+n.maxpos+' open positions.');
    if(n.on.expo)lines.push('Limit cost exposure per market to '+money(n.expo)+'.');
    if(n.on.daily)lines.push('Stop new entries after a detected daily loss of '+money(n.daily)+'. This does not guarantee a maximum loss.');
    if(n.on.stoploss)lines.push('At each pass, attempt to exit positions down '+(n.stoploss*100).toFixed(1)+'% from entry.');
    if(n.on.takeprofit)lines.push('At each pass, attempt to exit positions up '+(n.takeprofit*100).toFixed(1)+'% from entry.');
    if(n.on.noadd)lines.push('Do not add to losing positions.');
    if(n.on.dispute)lines.push('Skip dispute-risk scores above '+n.dispute+'.');
    if(n.on.liquidity)lines.push('Limit relative price slippage to '+(n.liquidity*100).toFixed(1)+'%.');
    if(n.on.watch)lines.push('Wait '+n.watch+' seconds and recheck the book before an entry.');
    lines.push('Require '+money(n.minvol)+' of 24h volume and skip markets closing within '+n.closing_h+' hours.');
    lines.push('Apply limits across selected strategies. This configuration stays a draft until live trading is authorized and a runner is ready.');
    $('review-list').replaceChildren(...lines.map(t=>el('li',t)));
    if(c.market_mode==='specific'){const detail=el('details');detail.append(el('summary','Exact allowed outcomes'));selected.forEach(m=>{detail.append(el('p',m.title,'small'));m.outcomes.forEach(o=>detail.append(el('p',o.label+': '+o.token_id,'token-id')));});$('review-list').parentNode.querySelectorAll('details').forEach(d=>d.remove());$('review-list').parentNode.append(detail);}else{$('review-list').parentNode.querySelectorAll('details').forEach(d=>d.remove());}
    const notes=input('description').value.trim();$('review-notes').hidden=!notes;$('review-notes').textContent='Draft notes, not executable instructions: '+notes;
    $('advanced-link').href='/advanced#config='+encodeURIComponent(JSON.stringify({config:c,notes:input('description').value.trim()}));
  }
  async function next(target){
    if(busy)return;
    if(target<stage){show(target);return;}
    if(target===1){if(!input('name').value.trim()||!strategies.some(s=>checked('on_'+s.id))){$('setup-error').textContent='Name your agent and select a strategy.';return;}show(1);return;}
    if(target===2){const error=validate();if(error){$('setup-error').textContent=error;return;}busy=true;$('review-button').disabled=true;$('review-button').textContent='Checking settings…';
      try{const c=read(),res=await request('/config/validate',json({config:c}));if(JSON.stringify(c)!==JSON.stringify(read()))throw Error('Settings changed during validation. Review again.');review(res.normalized);show(2);}catch(e){$('setup-error').textContent=e.message;}finally{busy=false;$('review-button').disabled=false;$('review-button').textContent='Review configuration →';}
    }else show(target);
  }
  function save(){
    try{const ds=loadDrafts(),d={id:draftId,name:input('name').value.trim()||'Untitled agent',description:input('description').value,config:read(),markets:selected,runner:input('runner').value,updated_at:new Date().toISOString(),status:'draft'};const index=ds.findIndex(x=>x.id===draftId);if(index<0)ds.push(d);else ds[index]=d;localStorage.setItem(DRAFTS,JSON.stringify(ds));$('draft-status').textContent='Draft saved in this browser. No agent is trading.';history.replaceState(null,'','/build?draft='+encodeURIComponent(draftId));return true;}catch(e){$('draft-status').textContent='This browser could not save the draft. Allow site storage and try again.';return false;}
  }
  $('save-draft').addEventListener('click',save);$('save-final').addEventListener('click',()=>{if(save())location.href='/portfolio?tab=agents';});
  function renderSelected(){
    $('selected-markets').replaceChildren();if(!selected.length){$('selected-markets').append(el('p','No markets selected yet.','muted small'));return;}
    selected.forEach((m,index)=>{const row=el('div',undefined,'selected-market'),copy=el('div');copy.append(el('h3',m.title),el('small',m.outcomes.map(o=>o.label).join(' + ')));const b=el('button','Remove','pill secondary small');b.type='button';b.setAttribute('aria-label','Remove '+m.title);b.onclick=()=>{selected.splice(index,1);renderSelected();sync();};row.append(copy,b);$('selected-markets').append(row);});
  }
  async function findMarket(value){
    const q=value.trim();if(!q){$('market-status').textContent='Enter a market name or Polymarket URL.';return;}
    const revision=++searchRevision;$('market-status').textContent='Finding markets…';$('market-results').replaceChildren();$('find-market').disabled=true;
    try{const isLink=/^(https?:|www\.|polymarket\.com\/)/i.test(q);const res=await request('/markets/'+(isLink?'resolve':'search')+'?q='+encodeURIComponent(q));if(revision!==searchRevision)return;
      $('market-status').textContent=res.markets.length?'Choose the markets your agent may trade.':'No matching markets. Try a different search.';
      res.markets.forEach(m=>{const row=el('div',undefined,'market-result'),copy=el('div');copy.append(el('h3',m.title),el('small',m.outcomes.map(o=>o.label+(o.price==null?'':' '+Math.round(o.price*100)+'%')).join(' · ')));const b=el('button','Add','pill secondary small');b.type='button';b.setAttribute('aria-label','Add '+m.title);
        const already=()=>selected.some(s=>s.outcomes.some(o=>m.outcomes.some(t=>t.token_id===o.token_id)));
        if(m.closed||m.accepting_orders===false){b.disabled=true;b.textContent='Closed';}else if(already()){b.disabled=true;b.textContent='Added';}
        b.onclick=()=>{if(already())return;const count=new Set([...selected.flatMap(s=>s.outcomes.map(o=>o.token_id)),...m.outcomes.map(o=>o.token_id)]).size;if(count>20){$('market-status').textContent='Maximum 20 outcomes. Remove a market first.';return;}selected.push(m);b.disabled=true;b.textContent='Added';renderSelected();sync();};row.append(copy,b);$('market-results').append(row);
      });
    }catch(e){if(revision===searchRevision)$('market-status').textContent=e.message;}finally{if(revision===searchRevision)$('find-market').disabled=false;}
  }
  $('find-market').onclick=()=>findMarket($('market-search').value);$('market-search').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();findMarket(e.target.value);}});
  let trading=null, renderActivation=()=>{};
  window.OddsRailWallet?.subscribe(state => {
    $('wallet-state').textContent = state.authenticated && state.address ? state.address.slice(0,6)+'…'+state.address.slice(-4)+' · Verified' : 'Not signed in';
    const account = state.portfolio?.account;
    const linked=trading?.getState();
    $('trading-account-state').textContent = linked?.connected && linked.account ? linked.account.address.slice(0,6)+'…'+linked.account.address.slice(-4)+' · API connected' : account?.status === 'resolved' && account.trading_address ? account.trading_address.slice(0,6)+'…'+account.trading_address.slice(-4)+' (public profile)' : 'Not identified';
  });
  form.addEventListener('submit',e=>e.preventDefault());
  document.addEventListener('click',e=>{const b=e.target.closest('button[data-step],button[data-next]');if(b){e.preventDefault();next(Number(b.dataset.next??b.dataset.step));}});
  form.addEventListener('input',sync);
  form.addEventListener('change',e=>{const n=e.target.name||'';if(n.startsWith('topic_')&&e.target.checked){if(n==='topic_all')categories.filter(x=>x[0]!=='all').forEach(([id])=>input('topic_'+id).checked=false);else input('topic_all').checked=false;}sync();});
  const activation = window.OddsRailActivation?.createActivationChecker({
    wallet:window.OddsRailWallet,api:API,getConfig:read,validate,
    fetch:(...args)=>window.fetch(...args),setTimeout:window.setTimeout.bind(window),clearTimeout:window.clearTimeout.bind(window)
  });
  if(activation){
    $('check-activation').onclick=()=>activation.check();
    renderActivation=state=>{
      const working=state.phase==='connecting'||state.phase==='checking';
      $('check-activation').disabled=working;
      $('check-activation').textContent=state.phase==='connecting'?'Waiting for wallet…':state.phase==='checking'?'Checking…':'Check activation readiness';
      $('activation-badge').textContent='Not active';
      $('activation-status').textContent=state.error||(working?'Checking your sign-in and setup…':state.report?'Your agent is not active. Review the remaining steps below.':'Your agent is not active.');
      const checks=$('activation-checks');checks.replaceChildren();checks.hidden=!state.report;
      state.report?.checks.forEach(original=>{
        const check={...original}, connection=trading?.getState();
        if(check.id==='funding'){
          check.detail=connection?.connected?'Your exchange balance and approvals are shown above. Available funds for this agent still require market-specific checks and accounting for existing positions and orders.':'Connect your trading account above to authenticate balance and allowance reads. Public holdings value does not include these funds.';
        }
        const row=el('div',undefined,'activation-check'),title=el('div',undefined,'activation-check-title');
        const label=el('strong',check.label),badge=el('span',check.status==='ready'?'Checked':check.status==='action_required'?'Next step':'Not ready','activation-check-state');
        badge.dataset.status=check.status;title.append(label,badge);row.append(title,el('p',check.detail,'muted small'));
        if(check.href){const link=el('a','Open Polymarket ↗','small');link.href='https://polymarket.com/';link.target='_blank';link.rel='noopener noreferrer';row.append(link);}
        checks.append(row);
      });
    };
    activation.subscribe(renderActivation);
    form.addEventListener('input',()=>activation.invalidate());
    form.addEventListener('change',()=>activation.invalidate());
    // Market selection buttons update scope without a native input event.
    $('selected-markets').addEventListener('click',()=>activation.invalidate());
    $('market-results').addEventListener('click',()=>activation.invalidate());
    // Keep controls usable when Back restores this page from the browser cache.
    window.addEventListener('pagehide',()=>activation.invalidate());
    window.addEventListener('pageshow',()=>activation.invalidate());
  }else{$('check-activation').disabled=true;}
  function units(value){
    if(typeof value!=='string'||!/^\d{1,78}$/.test(value))return 'Unavailable';
    const n=BigInt(value),whole=(n/1000000n).toString().replace(/\B(?=(\d{3})+(?!\d))/g,','),fraction=(n%1000000n).toString().padStart(6,'0');
    return '$'+whole+'.'+(n>0n&&n<10000n?fraction.replace(/0+$/,'').padEnd(2,'0'):fraction.slice(0,2));
  }
  const names={DEPOSIT_WALLET:'Deposit wallet',GNOSIS_SAFE:'Safe wallet',POLY_PROXY:'Proxy wallet'};
  function renderTrading(state){
    const working=['checking','signing','loading'].includes(state.phase);
    $('connect-trading').disabled=working;$('connect-trading').hidden=state.connected;
    $('connect-trading').textContent=state.phase==='signing'?'Confirm in your wallet…':working?'Connecting…':state.phase==='choosing'?'Connect selected account':'Connect trading account';
    $('trading-connection-badge').textContent=state.connected?'API connected':working?'Connecting':'Not connected';
    $('refresh-trading').hidden=!state.connected;$('refresh-trading').disabled=working;
    $('disconnect-trading').hidden=!state.connected&&!working;
    $('trading-account-choice').hidden=state.phase!=='choosing';
    if(state.phase==='choosing'){
      const selected=$('trading-account-select').value;
      $('trading-account-select').replaceChildren(...state.accounts.map(account=>{const option=el('option',names[account.wallet_type]+' · '+account.address);option.value=account.address;return option;}));
      if(state.accounts.some(account=>account.address===selected))$('trading-account-select').value=selected;
    }
    $('trading-connection-status').textContent=state.error||(state.phase==='signing'?'Sign the Polymarket connection message in your wallet. This is separate from OddsRail sign-in.':working?'Verifying your trading account and reading exchange data…':state.phase==='choosing'?'Choose which verified trading account to connect.':state.phase==='setup_required'?'No supported deployed account was found. Finish account setup on Polymarket with this wallet, then try again.':state.connected?'Connected for this tab. The balance below is an exchange snapshot; it is not an agent spending limit.':'Your private key stays in your wallet. Trading API credentials are held in this tab only.');
    const details=$('trading-connection-details');details.replaceChildren();details.hidden=!state.connected;
    if(state.connected){
      const row=(label,value)=>{const r=el('div',undefined,'auth-row');r.append(el('span',label),el('strong',value));details.append(r);};
      row(names[state.account.wallet_type]||'Trading account',state.account.address);
      row('Exchange balance',units(state.balance?.balance));
      row('Account access',state.closedOnly===true?'Closing positions only':state.closedOnly===false?'No close-only restriction reported':'Not verified');
      row('Open orders',state.ordersComplete?String(state.orders.length):'Incomplete — retry before trading');
      const approvals=el('details'),entries=Object.entries(state.balance?.allowances||{});approvals.append(el('summary','Spending approvals · '+entries.length+' returned'));
      if(!entries.length)approvals.append(el('p','No exchange approvals were returned. Complete trading setup on Polymarket.','muted small'));
      entries.forEach(([address,amount])=>{const item=el('p',undefined,'small');item.append(el('code',address),document.createTextNode(' · '+units(amount)));approvals.append(item);});details.append(approvals);
      details.append(el('p','Existing orders can commit part of this balance. Approvals must be checked for the selected market before an order is submitted.','muted small'));
      $('trading-account-state').textContent=state.account.address.slice(0,6)+'…'+state.account.address.slice(-4)+' · API connected';
    }else{
      const account=window.OddsRailWallet?.getState().portfolio?.account;
      $('trading-account-state').textContent=account?.status==='resolved'&&account.trading_address?account.trading_address.slice(0,6)+'…'+account.trading_address.slice(-4)+' (public profile)':'Not identified';
    }
    if(activation)renderActivation(activation.getState());
  }
  if(window.OddsRailTradingConnection&&window.OddsRailWallet){
    trading=window.OddsRailTradingConnection.createTradingConnection({wallet:window.OddsRailWallet,api:API,fetch:(...args)=>window.fetch(...args),crypto:window.crypto,setTimeout:window.setTimeout.bind(window),clearTimeout:window.clearTimeout.bind(window)});
    trading.subscribe(renderTrading);
    $('connect-trading').onclick=()=>trading.connect({tradingWallet:trading.getState().phase==='choosing'?$('trading-account-select').value:undefined});
    $('refresh-trading').onclick=()=>trading.refresh();
    $('disconnect-trading').onclick=()=>trading.disconnect();
    window.addEventListener('pagehide',()=>trading.disconnect());
  }else{$('connect-trading').disabled=true;$('trading-connection-status').textContent='Trading connection is unavailable. Reload and try again.';}
  renderSelected();sync();
  if(url.searchParams.has('market')){input('market_mode').value='specific';$('market-search').value=url.searchParams.get('market');sync();findMarket($('market-search').value);}
})();
