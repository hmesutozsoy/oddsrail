(function(){'use strict';
const $=id=>document.getElementById(id);
function el(tag,text,cls){const n=document.createElement(tag);n.textContent=text||'';if(cls)n.className=cls;return n;}
let drafts=[];try{const d=JSON.parse(localStorage.getItem('oddsrail-drafts-v2')||'[]');if(Array.isArray(d))drafts=d.filter(x=>x&&typeof x.id==='string'&&x.config);}catch(e){}
$('no-drafts').hidden=!!drafts.length;
drafts.sort((a,b)=>String(b.updated_at||'').localeCompare(String(a.updated_at||''))).forEach(d=>{const card=el('article','','panel draft-card');card.append(el('span','Draft','status-chip'),el('h2',d.name||'Untitled agent'));const strategies={mm:'Quote both sides',fade:'Fade sharp moves',settle:'High-probability outcomes',value:'My forecast',momentum:'Momentum'};const names=Object.keys(strategies).filter(k=>(d.config.on||{})[k]).map(k=>strategies[k]);card.append(el('p',names.join(' · ')||'No strategy selected','muted'));card.append(el('p',d.config.market_mode==='specific'?(d.markets||[]).length+' selected markets':(Array.isArray(d.config.topics)?d.config.topics:[]).join(', '),'muted small'));const link=el('a','Continue editing →','pill secondary');link.href='/build?draft='+encodeURIComponent(d.id);card.append(link);$('drafts').append(card);});
})();
