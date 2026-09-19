(() => {
  'use strict';
  function publicHoldings(state) {
    const address = /^0x[0-9a-f]{40}$/i;
    if (!state || state.status !== 'connected' || state.authenticated !== true || !address.test(state.address || '') || state.portfolioStatus !== 'ready') return null;
    const portfolio = state.portfolio;
    if (!portfolio || typeof portfolio.address !== 'string' || portfolio.address.toLowerCase() !== state.address.toLowerCase() || portfolio.account?.status !== 'resolved' || !address.test(portfolio.account.trading_address || '')) return null;
    const value = portfolio.summary?.holdings_value_usd;
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {publicHoldings};
  if (typeof document === 'undefined') return;
  const icons = {
    markets: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    portfolio: '<rect x="3" y="6" width="18" height="15" rx="3"/><path d="M8 6V3h8v3M3 12h18M10 12v3h4v-3"/>',
    wallet: '<path d="M20 8V5a2 2 0 0 0-2-2L5 6a3 3 0 0 0-2 3v10a2 2 0 0 0 2 2h15V8H6M20 12h-5v5h5M16 14.5h.01"/>',
    agents: '<rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 3v4M9 13h.01M15 13h.01M9 17h6"/>',
    arena: '<path d="M8 3h8v6a4 4 0 0 1-8 0V3ZM8 5H4v3a4 4 0 0 0 4 4m8-7h4v3a4 4 0 0 1-4 4m-4 1v5m-4 3h8m-7 0v-3h6v3"/>',
    more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    arrow: '<path d="M7 17 17 7M7 7h10v10"/>',
    support: '<path d="M4 12a8 8 0 0 1 16 0v5a3 3 0 0 1-3 3h-3"/><rect x="3" y="11" width="4" height="7" rx="2"/><rect x="17" y="11" width="4" height="7" rx="2"/>',
    docs: '<path d="M4 4h6a2 2 0 0 1 2 2v15a3 3 0 0 0-3-2H4V4Zm16 0h-6a2 2 0 0 0-2 2v15a3 3 0 0 1 3-2h5V4Z"/>',
    moon: '<path d="M20 14a8 8 0 0 1-10-10 8.3 8.3 0 1 0 10 10Z"/>',
    terms: '<path d="M6 3h9l4 4v14H6V3Zm9 0v5h4M9 12h7M9 16h7"/>'
  };
  const svg = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.arrow}</svg>`;
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    document.querySelectorAll('[data-theme-toggle]').forEach(b => b.setAttribute('aria-checked', String(theme === 'dark')));
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#18191b' : '#f0efea');
    try { localStorage.setItem('oddsrail.theme', theme); } catch (_) { /* Storage may be unavailable. */ }
  }
  let initialTheme = 'dark';
  try { if (localStorage.getItem('oddsrail.theme') === 'light') initialTheme = 'light'; } catch (_) { /* Use default. */ }
  document.documentElement.dataset.theme = initialTheme;
  function mount() {
    const host = document.querySelector('[data-shell]');
    if (!host || host.dataset.mounted) return;
    host.dataset.mounted = 'true';
    const currentPage = document.body.dataset.page || 'markets';
    const page = currentPage === 'agents' ? 'portfolio' : currentPage;
    const items = [['markets','/','Markets'],['portfolio','/portfolio','Dashboard'],['arena','/arena','Competition']];
    const links = mobile => items.map(([id,href,label]) => `<a href="${href}" class="${page === id ? 'active' : ''}"${page === id ? ' aria-current="page"' : ''}>${mobile ? svg(id) : ''}<span>${label}</span></a>`).join('');
    host.innerHTML = `<a class="skip-link" href="#main-content">Skip to content</a><header class="shell-header"><div class="shell-bar"><a class="shell-brand" href="/" aria-label="OddsRail home"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>oddsrail</a><nav class="shell-nav" aria-label="Main navigation">${links(false)}</nav><div class="shell-actions"><button class="icon-button desktop-more" data-more-toggle type="button" aria-label="More options" aria-controls="shell-menu" aria-expanded="false">${svg('more')}</button><a href="/advanced" class="pill secondary small agent-link">Use with Claude</a><a href="/build" class="pill secondary small create-agent">Create agent <span aria-hidden="true">↗</span></a><div class="shell-account-summary" id="shell-balances" hidden><a href="/portfolio" class="shell-balance"><span>Holdings</span><strong id="shell-holdings-value">—</strong></a></div><button class="pill small shell-connect" id="shell-connect" type="button">Connect wallet</button><button id="shell-wallet-toggle" class="wallet-avatar" type="button" aria-label="Wallet options" aria-controls="shell-wallet-menu" aria-expanded="false" hidden>${svg('wallet')}</button></div></div></header><p id="shell-wallet-status" class="shell-wallet-status" role="status" hidden></p><div id="shell-wallet-menu" class="shell-wallet-menu" hidden><p class="menu-label">Signed-in wallet</p><p id="shell-wallet-address" class="wallet-address"></p><p class="wallet-caption">Wallet ownership verified. Trading is not authorized.</p><a href="/portfolio">Dashboard <span aria-hidden="true">↗</span></a><a href="/portfolio?tab=agents">Agents <span aria-hidden="true">↗</span></a><a href="/portfolio?tab=history">History <span aria-hidden="true">↗</span></a><div class="menu-divider"></div><button id="shell-disconnect" type="button">Sign out of OddsRail</button></div><div id="shell-menu" class="shell-menu" hidden><p class="menu-label">More from OddsRail</p><a href="https://github.com/hmesutozsoy/oddsrail/issues" target="_blank" rel="noopener noreferrer">${svg('support')}<span>Support</span>${svg('arrow')}</a><a href="https://github.com/hmesutozsoy/oddsrail#readme" target="_blank" rel="noopener noreferrer">${svg('docs')}<span>Documentation</span>${svg('arrow')}</a><a href="/advanced">${svg('docs')}<span>Use with your own agent</span>${svg('arrow')}</a><a href="/notes/">${svg('docs')}<span>Notes on the venue APIs</span>${svg('arrow')}</a><a href="/arena">${svg('arena')}<span>Competition</span>${svg('arrow')}</a><div class="menu-divider"></div><button type="button" data-theme-toggle role="switch" aria-label="Dark mode" aria-checked="true">${svg('moon')}<span>Dark mode</span><span class="theme-switch" aria-hidden="true"></span></button><a href="/terms">${svg('terms')}<span>Terms</span>${svg('arrow')}</a><p class="menu-footnote">Open source. Built on Polymarket.</p></div>`;
    const footer = document.createElement('footer');
    footer.className = 'shell-footer';
    footer.innerHTML = '<span>© '+new Date().getFullYear()+' OddsRail</span><span class="footer-builder"><span class="verified-glyph" aria-hidden="true">✓</span> Verified Polymarket builder</span><a href="https://github.com/hmesutozsoy/oddsrail" target="_blank" rel="noopener noreferrer">Built in the open ↗</a>';
    document.body.append(footer);
    const mobile = document.createElement('nav');
    mobile.className = 'mobile-nav'; mobile.setAttribute('aria-label','Mobile navigation');
    mobile.innerHTML = links(true)+`<button type="button" data-more-toggle aria-controls="shell-menu" aria-expanded="false">${svg('more')}<span>More</span></button>`;
    document.body.append(mobile);
    const menu = document.getElementById('shell-menu');
    const walletMenu = document.getElementById('shell-wallet-menu');
    const walletToggle = document.getElementById('shell-wallet-toggle');
    function closeWallet(restoreFocus = false) { walletMenu.hidden = true; walletToggle.setAttribute('aria-expanded','false'); if (restoreFocus) walletToggle.focus(); }
    let openingButton = null;
    function closeMenu(restoreFocus = false) { menu.hidden = true; document.querySelectorAll('[data-more-toggle]').forEach(b => b.setAttribute('aria-expanded','false')); if (restoreFocus) openingButton?.focus(); }
    document.querySelectorAll('[data-more-toggle]').forEach(button => button.addEventListener('click', event => {
      if (!menu.hidden) { closeMenu(); return; }
      closeWallet(); openingButton = button; menu.hidden = false;
      document.querySelectorAll('[data-more-toggle]').forEach(b => b.setAttribute('aria-expanded','true'));
      if (event.detail === 0) menu.querySelector('a, button')?.focus();
    }));
    document.addEventListener('click', event => { if (!walletMenu.hidden && !walletMenu.contains(event.target) && !walletToggle.contains(event.target)) closeWallet(); if (!menu.hidden && !menu.contains(event.target) && !event.target.closest('[data-more-toggle]')) closeMenu(); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') { if (!menu.hidden) closeMenu(true); if (!walletMenu.hidden) closeWallet(true); } });
    document.querySelectorAll('[data-theme-toggle]').forEach(b => b.addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light')));
    walletToggle.addEventListener('click', event => {
      if (!walletMenu.hidden) { closeWallet(); return; }
      closeMenu(); walletMenu.hidden = false; walletToggle.setAttribute('aria-expanded','true');
      if (event.detail === 0) walletMenu.querySelector('a')?.focus();
    });
    const connect = document.getElementById('shell-connect'), status = document.getElementById('shell-wallet-status');
    const wallet = window.OddsRailWallet;
    if (wallet) {
      connect.addEventListener('click', async () => { if (wallet.getState().signOutPending) { await wallet.disconnect(); return; } const connected = await wallet.connect(); if (connected && location.pathname === '/') location.assign('/portfolio'); });
      document.getElementById('shell-disconnect').addEventListener('click', () => { closeWallet(); wallet.disconnect(); connect.focus(); });
      const money = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
      wallet.subscribe(state => {
        const connected = state.status === 'connected' && state.authenticated === true;
        const labels = {connecting:'Connecting…',signing:'Sign in your wallet…',verifying:'Verifying…'};
        const busy = Boolean(labels[state.status]);
        document.body.dataset.walletConnected = String(connected);
        connect.hidden = connected; connect.disabled = busy; connect.textContent = labels[state.status] || (state.signOutPending ? 'Retry sign-out' : 'Connect wallet');
        walletToggle.hidden = !connected; document.getElementById('shell-balances').hidden = !connected;
        if (!connected) closeWallet();
        walletToggle.setAttribute('aria-label', connected ? 'Wallet options for ' + state.address : 'Wallet options');
        document.getElementById('shell-wallet-address').textContent = state.address;
        const holdings = publicHoldings(state), loading = connected && state.portfolioStatus === 'loading';
        const value = document.getElementById('shell-holdings-value');
        value.textContent = loading ? '…' : money(holdings);
        value.title = loading ? 'Loading reported holdings' : holdings !== null ? 'Reported holdings: ' + money(holdings) + '. Excludes cash.' : 'Reported holdings value is unavailable. Excludes cash.';
        const progress = state.status === 'signing' ? 'Sign the OddsRail message in your wallet. It confirms ownership without granting trading permission.' : state.status === 'verifying' ? 'Verifying your signature…' : '';
        status.textContent = state.error || progress; status.hidden = !status.textContent;
      });
    } else { connect.disabled = true; status.textContent = 'Wallet connection is unavailable. Reload to try again.'; status.hidden = false; }
    setTheme(initialTheme);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
