(() => {
  'use strict';
  const form = document.getElementById('market-search');
  if (!form) return;
  const query = document.getElementById('market-query'), grid = document.getElementById('market-grid'), state = document.getElementById('market-state'), count = document.getElementById('market-count'), heading = document.getElementById('market-section-heading'), refreshed = document.getElementById('market-updated'), refreshButton = document.getElementById('refresh-markets');
  const api = (window.ODDSRAIL_API || 'https://mcp.oddsrail.app').replace(/\/$/,'');
  let category = '', activeRequest = null, requestNumber = 0;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function marketUrl(market) {
    try { const u = new URL(market.url || `https://polymarket.com/event/${encodeURIComponent(market.slug || '')}`); return u.protocol === 'https:' && (u.hostname === 'polymarket.com' || u.hostname === 'www.polymarket.com') && u.pathname !== '/event/' ? u.href : null; } catch (_) { return null; }
  }
  function percent(value) { if (value == null || value === '') return null; const n = Number(value); if (!Number.isFinite(n) || n < 0 || n > 1) return null; if (n > 0 && n < 0.01) return '<1%'; if (n > 0.99 && n < 1) return '>99%'; return new Intl.NumberFormat('en',{style:'percent',maximumFractionDigits:1}).format(n); }
  function card(market,index) {
    const title = String(market.title || '').trim(), url = marketUrl(market);
    if (!title || !url) return '';
    const outcomes = Array.isArray(market.outcomes) ? market.outcomes.filter(outcome => outcome && typeof outcome === 'object') : [];
    const rows = outcomes.slice(0,3).map(o => `<div class="outcome-row"><span>${escape(o.label || 'Outcome')}</span><strong>${escape(percent(o.price) ?? '—')}</strong></div>`).join('');
    const initial = title.replace(/^(will |what |who |which |is |does |can |the )+/i,'').charAt(0).toUpperCase() || 'P';
    const tradable = market.closed === false && market.accepting_orders === true;
    const action = tradable ? `<a class="pill small secondary" href="/build?market=${encodeURIComponent(url)}">Create agent <span aria-hidden="true">↗</span></a>` : `<a class="pill small secondary" href="${escape(url)}" target="_blank" rel="noopener noreferrer">View market <span aria-hidden="true">↗</span></a>`;
    const status = tradable ? 'Make your next move.' : market.closed === true ? 'Market closed' : 'Not accepting orders';
    return `<article class="market-card"><div class="market-card-top"><span class="market-avatar tone-${index % 6}" aria-hidden="true">${escape(initial)}</span><span class="market-venue">Polymarket</span><a class="market-external" href="${escape(url)}" target="_blank" rel="noopener noreferrer" aria-label="View ${escape(title)} on Polymarket"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17 17 7M7 7h10v10"/></svg></a></div><h3><a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(title)}</a></h3><div class="market-outcomes">${rows || '<p class="muted">Outcome prices unavailable</p>'}${outcomes.length > 3 ? `<p class="more-outcomes">+${outcomes.length - 3} more outcomes on Polymarket</p>` : ''}</div><div class="market-card-bottom"><span class="market-card-note">${status}</span>${action}</div></article>`;
  }
  function skeletons() { return Array.from({length:6},() => '<div class="market-card skeleton-card" aria-hidden="true"><div class="skeleton avatar"></div><div class="skeleton title"></div><div class="skeleton short"></div><div class="skeleton row"></div><div class="skeleton row"></div><div class="skeleton action"></div></div>').join(''); }
  async function loadMarkets() {
    activeRequest?.abort(); const controller = new AbortController(); activeRequest = controller;
    const request = ++requestNumber, search = query.value.trim(), q = [category,search].filter(Boolean).join(' ');
    const label = document.querySelector(`[data-category="${category}"]`)?.textContent || '';
    heading.textContent = search ? `Results for “${search}”` : category ? `${label} markets` : 'Discover markets';
    state.innerHTML = '<p class="loading-message">Finding markets…</p>'; count.textContent = ''; grid.innerHTML = skeletons(); grid.setAttribute('aria-busy','true'); refreshButton.disabled = true;
    const timeout = setTimeout(() => controller.abort(),20000);
    try {
      const response = await fetch(`${api}/markets/search?q=${encodeURIComponent(q)}`,{signal:controller.signal,headers:{Accept:'application/json'}});
      if (!response.ok) throw new Error('request_failed');
      const data = await response.json();
      if (data.ok !== true || !Array.isArray(data.markets)) throw new Error('invalid_response');
      if (request !== requestNumber) return;
      const seen = new Set();
      const cards = data.markets.filter(m => { if (!m || typeof m !== 'object') return false; const id = m.condition_id || m.slug || m.url; if (!id || seen.has(id)) return false; seen.add(id); return true; }).map(card).filter(Boolean);
      grid.innerHTML = cards.join(''); count.textContent = `${cards.length} market${cards.length === 1 ? '' : 's'}`; refreshed.textContent = `Updated ${new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}`;
      state.innerHTML = cards.length ? '' : '<div class="market-empty"><span class="empty-symbol" aria-hidden="true">⌕</span><h3>No markets found</h3><p>Try a different topic or a shorter search.</p><button type="button" class="pill secondary" id="clear-market-search">Clear search</button></div>';
      document.getElementById('clear-market-search')?.addEventListener('click',() => { query.value = ''; selectCategory(''); loadMarkets(); });
    } catch (_) {
      if (request !== requestNumber) return;
      grid.innerHTML = ''; refreshed.textContent = 'Market data unavailable';
      state.innerHTML = '<div class="market-empty"><span class="empty-symbol" aria-hidden="true">↻</span><h3>Markets couldn’t load</h3><p>The market feed is unavailable right now. Try again in a moment.</p><button type="button" class="pill" id="retry-market-search">Try again <span aria-hidden="true">↗</span></button></div>';
      document.getElementById('retry-market-search')?.addEventListener('click',loadMarkets);
    } finally { clearTimeout(timeout); if (request === requestNumber) { grid.setAttribute('aria-busy','false'); refreshButton.disabled = false; } }
  }
  function selectCategory(value) { category = value; document.querySelectorAll('[data-category]').forEach(b => { const selected = b.dataset.category === value; b.classList.toggle('active',selected); b.setAttribute('aria-pressed',String(selected)); }); }
  form.addEventListener('submit',event => { event.preventDefault(); loadMarkets(); });
  query.addEventListener('search',() => { if (!query.value) loadMarkets(); });
  refreshButton.addEventListener('click',loadMarkets);
  document.querySelectorAll('[data-category]').forEach(b => b.addEventListener('click',() => { selectCategory(b.dataset.category); loadMarkets(); }));
  loadMarkets();
})();
