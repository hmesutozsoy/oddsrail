(function () {
  'use strict';
  var CODE = '0xa576c5ce9fabba322d8fa3a8d16738221d1b6b2b0c57b544f757fa9e45a09a90';
  var FEED = 'https://clob.polymarket.com/builder/trades?builder_code=' + CODE;
  var DATA = 'https://data-api.polymarket.com';
  var PAPER = ['https://mcp.oddsrail.app/arena/paper.json', 'https://151-241-155-39.sslip.io/arena/paper.json'];
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function usd(v) { if (v == null || isNaN(+v)) return ''; v = +v; var s = v < 0 ? '-' : ''; v = Math.abs(v); return s + '$' + (v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : v.toFixed(2)); }
  function pct(v) { if (v == null || isNaN(+v)) return ''; return (v > 0 ? '+' : '') + (+v).toFixed(2) + '%'; }
  function short(a) { return a.slice(0, 6) + '…' + a.slice(-4); }
  function day(ts) { return ts ? new Date(ts * 1000).toISOString().slice(0, 10) : ''; }
  function cls(v) { return v > 0 ? 'pos' : v < 0 ? 'neg' : ''; }

  // ------------------------------- paper board -------------------------------
  function fetchFirst(urls) {
    return fetch(urls[0], { cache: 'no-store' }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .catch(function (e) { if (urls.length > 1) return fetchFirst(urls.slice(1)); throw e; });
  }
  fetchFirst(PAPER).then(function (d) {
    var rows = d.entries || [];
    $('paper-meta').textContent = rows.filter(function (r) { return !r.house; }).length + ' registered, computed ' + String(d.computed_at || '').replace('T', ' ').slice(0, 16) + ' UTC';
    $('paper-note').textContent = d.note || '';
    var tb = $('paper-tbl').querySelector('tbody'); tb.innerHTML = '';
    if (!rows.filter(function (r) { return !r.house; }).length) { var tr0 = document.createElement('tr'); tr0.innerHTML = '<td colspan="10">No registered agents yet, only the house reference below. Be the first: press Run on the <a href="/build">builder</a>, keep the agent, and tick the board.</td>'; tb.appendChild(tr0); }
    rows.forEach(function (r) {
      var tr = document.createElement('tr'); if (r.house) tr.className = 'house';
      tr.innerHTML = '<td>' + (r.house ? 'ref' : (r.rank || '')) + '</td><th scope="row">' + esc(r.name) + '</th><td class="strat">' + esc(r.strategy) + '</td>' +
        '<td class="' + cls(r.return_pct) + '">' + pct(r.return_pct) + '</td><td>' + usd(r.equity) + '</td><td class="' + cls(r.realized_pnl) + '">' + usd(r.realized_pnl) + '</td>' +
        '<td class="' + cls(r.unrealized_pnl) + '">' + usd(r.unrealized_pnl) + '</td><td>' + (r.positions == null ? '' : r.positions) + '</td><td>' + (r.fills == null ? '' : r.fills) + '</td><td>' + esc((r.since || '').slice(0, 10)) + '</td>';
      tb.appendChild(tr);
    });
  }).catch(function (e) {
    $('paper-meta').textContent = 'unavailable (' + e.message + ')';
    $('paper-tbl').querySelector('tbody').innerHTML = '<tr><td colspan="10">The hosted server did not answer. Try again in a minute.</td></tr>';
  });

  // -------------------------------- live board -------------------------------
  function fetchAll(cursor, acc) {
    acc = acc || [];
    var url = FEED + (cursor ? '&next_cursor=' + encodeURIComponent(cursor) : '');
    return fetch(url, { cache: 'no-store' }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }).then(function (d) {
      var rows = Array.isArray(d) ? d : (d.data || []); acc = acc.concat(rows);
      var nc = Array.isArray(d) ? null : d.next_cursor;
      if (nc && nc !== 'LTE=' && rows.length && acc.length < 50000) return fetchAll(nc, acc);
      return acc;
    });
  }
  function wallets(rows) {
    var W = {};
    rows.forEach(function (r) {
      var w = String(r.maker || r.owner || '').toLowerCase(); if (!w) return;
      var v = +r.sizeUsdc || (+r.size || 0) * (+r.price || 0), ts = +r.matchTime || +r.createdAt || 0;
      var a = W[w] || (W[w] = { wallet: w, vol: 0, trades: 0, markets: {}, first: ts, last: ts });
      a.vol += v; a.trades++; if (r.market) a.markets[r.market] = 1; a.first = Math.min(a.first, ts); a.last = Math.max(a.last, ts);
    });
    return W;
  }
  function pnl(w) {
    return Promise.all([
      fetch(DATA + '/positions?user=' + w + '&limit=500', { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : []; }).catch(function () { return []; }),
      fetch(DATA + '/value?user=' + w, { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : []; }).catch(function () { return []; })
    ]).then(function (res) {
      var pos = Array.isArray(res[0]) ? res[0] : [], realized = 0, open = 0;
      pos.forEach(function (p) { realized += +p.realizedPnl || 0; open += +p.cashPnl || 0; });
      var val = Array.isArray(res[1]) && res[1][0] ? +res[1][0].value : null;
      return { realized: realized, open: open, value: val, n: pos.length };
    });
  }
  Promise.all([fetch('/arena/agents.json', { cache: 'no-store' }).then(function (r) { return r.json(); }), fetchAll()]).then(function (res) {
    var reg = res[0].agents || [], W = wallets(res[1]);
    var byW = {}; reg.forEach(function (a) { byW[a.wallet.toLowerCase()] = a; });
    var list = Object.keys(W).map(function (k) { var a = W[k], r = byW[k]; return { w: k, a: a, r: r, house: !!(r && r.house) }; });
    reg.forEach(function (r) { if (!W[r.wallet.toLowerCase()]) list.push({ w: r.wallet.toLowerCase(), a: { vol: 0, trades: 0, markets: {}, last: 0 }, r: r, house: !!r.house }); });
    list.sort(function (x, y) { return (x.house - y.house) || (y.a.vol - x.a.vol); });
    var ranked = list.filter(function (x) { return !x.house && x.a.trades > 0; });
    $('live-meta').textContent = ranked.length + ' on the feed, ' + reg.filter(function (a) { return !a.house; }).length + ' registered, ' + res[1].length + ' attributed fills total';
    var tb = $('live-tbl').querySelector('tbody'); tb.innerHTML = '';
    if (!list.length) tb.innerHTML = '<tr><td colspan="10">No attributed fills on the feed yet.</td></tr>';
    var rank = 0;
    list.forEach(function (x) {
      var tr = document.createElement('tr'); if (x.house) tr.className = 'house';
      var name = x.r ? esc(x.r.name) : '<a href="https://polygonscan.com/address/' + esc(x.w) + '">' + esc(short(x.w)) + '</a> <span class="muted">unregistered</span>';
      if (x.r && x.r.url) name = '<a href="' + esc(x.r.url) + '">' + name + '</a>';
      var rk = x.house ? 'ref' : (x.a.trades > 0 ? String(++rank) : '');
      tr.innerHTML = '<td>' + rk + '</td><th scope="row">' + name + '</th><td class="strat">' + esc(x.r ? x.r.strategy : '') + '</td><td>' + usd(x.a.vol) + '</td><td>' + x.a.trades + '</td><td>' + Object.keys(x.a.markets).length + '</td>' +
        '<td class="pnl-r">…</td><td class="pnl-o">…</td><td class="pnl-v">…</td><td>' + day(x.a.last) + '</td>';
      tb.appendChild(tr);
      if (x.r) pnl(x.w).then(function (p) {
        tr.querySelector('.pnl-r').textContent = usd(p.realized); tr.querySelector('.pnl-r').className = 'pnl-r ' + cls(p.realized);
        tr.querySelector('.pnl-o').textContent = usd(p.open); tr.querySelector('.pnl-o').className = 'pnl-o ' + cls(p.open);
        tr.querySelector('.pnl-v').textContent = usd(p.value);
      }); else { tr.querySelector('.pnl-r').textContent = ''; tr.querySelector('.pnl-o').textContent = ''; tr.querySelector('.pnl-v').textContent = ''; }
    });
  }).catch(function (e) {
    $('live-meta').textContent = 'unavailable (' + e.message + ')';
    $('live-tbl').querySelector('tbody').innerHTML = '<tr><td colspan="10">Polymarket\'s feed did not answer. Try again in a minute.</td></tr>';
  });

  // ------------------------------ registration -------------------------------
  var reg = $('reg');
  function entry() {
    var fd = new FormData(reg);
    return { name: String(fd.get('name') || '').trim(), wallet: String(fd.get('wallet') || '').trim().toLowerCase(), strategy: String(fd.get('strategy') || '').trim(), url: String(fd.get('url') || '').trim(), since: new Date().toISOString().slice(0, 10) };
  }
  function update() {
    var e = entry(), ok = /^0x[0-9a-f]{40}$/.test(e.wallet) && e.name.length >= 3;
    $('reg-status').textContent = ok ? '' : 'name (3+ characters) and a 0x wallet address are required';
    var body = 'Please add this agent to the live division.\n\n```json\n' + JSON.stringify(e, null, 2) + '\n```\n\nI confirm this wallet trades through oddsrail with the builder code attached.';
    $('issue').href = 'https://github.com/hmesutozsoy/oddsrail/issues/new?title=' + encodeURIComponent('Arena registration: ' + (e.name || 'agent')) + '&body=' + encodeURIComponent(body);
    $('issue').classList.toggle('disabled', !ok);
  }
  reg.addEventListener('input', update); update();
  $('copyjson').addEventListener('click', function () { navigator.clipboard.writeText(JSON.stringify(entry(), null, 2)).then(function () { $('reg-status').textContent = 'copied'; }); });
})();
