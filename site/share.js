(function () {
  'use strict';
  var HOSTS = ['https://mcp.oddsrail.app', 'https://151-241-155-39.sslip.io'];
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function usd(v) { if (v == null || isNaN(+v)) return ''; v = +v; var s = v < 0 ? '-' : ''; v = Math.abs(v); return s + '$' + v.toFixed(2); }
  function signed(v) { if (v == null || isNaN(+v)) return ''; return (v > 0 ? '+' : '') + usd(v).replace('$-', '-$'); }
  function cls(v) { return v > 0 ? 'pos' : v < 0 ? 'neg' : ''; }
  function get(path) {
    function attempt(i) {
      if (i >= HOSTS.length) return Promise.reject(new Error('no host answered'));
      return fetch(HOSTS[i] + path, { cache: 'no-store' })
        .then(function (r) { return r.json().then(function (j) { if (!r.ok && i + 1 < HOSTS.length && r.status !== 404) return attempt(i + 1); return j; }); })
        .catch(function () { return attempt(i + 1); });
    }
    return attempt(0);
  }
  function fact(dt, dd) { return '<div><dt>' + dt + '</dt><dd>' + dd + '</dd></div>'; }

  function decisionRows(decisions) {
    if (!decisions || !decisions.length) return '<tr><td colspan="5">No decisions in this pass. Nothing qualified under the rules.</td></tr>';
    return decisions.map(function (d) {
      var order = d.side ? d.side + ' ' + (d.outcome || '') + (d.size ? ' ' + (+d.size).toFixed(2) + ' @ ' + d.price : '') : '';
      var res = d.result === 'filled' ? 'filled' + (d.avg_price ? ' @ ' + d.avg_price : '') : d.result === 'partial' ? 'partial ' + d.filled : d.result;
      return '<tr class="r-' + esc(d.result) + '"><td class="strat">' + esc(d.market || '') + (d.why ? '<small>' + esc(d.why) + '</small>' : '') + '</td>' +
        '<td>' + esc(d.strategy || '') + '</td><td class="mono">' + esc(order) + '</td><td>' + esc(d.verdict || '') + '</td>' +
        '<td>' + esc(res) + (d.detail ? '<small>' + esc(d.detail) + '</small>' : '') + '</td></tr>';
    }).join('');
  }

  function sparkline(points, bankroll) {
    if (!points.length) return '<p class="muted">No scheduled passes yet.</p>';
    var W = 720, H = 180, pad = 24;
    var vals = points.map(function (p) { return p.equity; }).filter(function (v) { return v != null; });
    if (!vals.length) return '<p class="muted">No equity recorded yet.</p>';
    var lo = Math.min.apply(null, vals.concat([bankroll])), hi = Math.max.apply(null, vals.concat([bankroll]));
    if (hi - lo < 1) { hi += 1; lo -= 1; }
    var n = Math.max(vals.length - 1, 1);
    function x(i) { return pad + (i / n) * (W - 2 * pad); }
    function y(v) { return H - pad - ((v - lo) / (hi - lo)) * (H - 2 * pad); }
    var d = vals.map(function (v, i) { return (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1); }).join(' ');
    var base = y(bankroll).toFixed(1);
    var last = vals[vals.length - 1];
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" height="180" role="img" aria-label="equity curve">' +
      '<line x1="' + pad + '" y1="' + base + '" x2="' + (W - pad) + '" y2="' + base + '" stroke="#232b38" stroke-dasharray="4 4"/>' +
      '<path d="' + d + '" fill="none" stroke="' + (last >= bankroll ? '#6cc38a' : '#e06c6c') + '" stroke-width="2"/>' +
      '<text x="' + pad + '" y="14" fill="#7d8797" font-size="11" font-family="ui-monospace,Menlo,monospace">' + usd(hi) + '</text>' +
      '<text x="' + pad + '" y="' + (H - 6) + '" fill="#7d8797" font-size="11" font-family="ui-monospace,Menlo,monospace">' + usd(lo) + '</text>' +
      '</svg>';
  }

  // ------------------------------- run page --------------------------------
  if ($('run-facts')) {
    var id = (new URLSearchParams(location.search)).get('id') || '';
    if (!/^[A-Za-z0-9_-]{6,32}$/.test(id)) {
      $('run-lede').textContent = 'This link has no run id. Press Run on the builder to make one.';
      $('dec-tbl').querySelector('tbody').innerHTML = '<tr><td colspan="5">nothing to show</td></tr>';
    } else {
      get('/runs/' + id + '.json').then(function (j) {
        if (!j || !j.ok) { $('run-lede').textContent = (j && j.error) || 'This run could not be loaded.'; $('dec-tbl').querySelector('tbody').innerHTML = '<tr><td colspan="5">not found</td></tr>'; return; }
        var r = j.result || {}, led = r.ledger || {}, u = r.universe || {};
        document.title = (j.agent ? j.agent + ': ' : '') + (r.orders_placed || 0) + ' orders on Polymarket, paper';
        $('run-kicker').textContent = j.agent ? 'Paper pass by ' + j.agent : 'Paper pass';
        $('h-title').textContent = j.agent ? j.agent : ((r.orders_placed || 0) + ' order' + ((r.orders_placed || 0) === 1 ? '' : 's') + ' from ' + (r.decisions || []).length + ' decisions');
        $('run-lede').textContent = 'A deterministic pass over ' + (u.scanned || 0) + ' Polymarket markets on ' +
          String(j.created || '').replace('T', ' ').slice(0, 16) + ' UTC. ' + (r.decisions || []).length + ' decisions, ' +
          (r.orders_placed || 0) + ' orders placed, ' + (r.seconds || 0) + ' seconds. No model was consulted.';
        $('run-facts').innerHTML = fact('markets', (u.candidates || 0) + ' of ' + (u.scanned || 0)) +
          fact('pieces', (r.strategies || []).join(', ') || 'none') +
          fact('equity after', usd(led.equity)) +
          fact('P&amp;L', '<span class="' + cls((+led.realized_pnl || 0) + (+led.unrealized_pnl || 0)) + '">' + signed((+led.realized_pnl || 0) + (+led.unrealized_pnl || 0)) + '</span>') +
          fact('positions', (led.positions || []).length) + fact('resting', (led.open_orders || []).length);
        $('dec-tbl').querySelector('tbody').innerHTML = decisionRows(r.decisions);
        $('steps').textContent = (r.steps || []).join('\n');
        $('steps-wrap').hidden = !(r.steps || []).length;
        var cfg = j.config || {}, chips = [];
        (cfg.topics || []).forEach(function (t) { chips.push(t); });
        if (cfg.keyword) chips.push('keyword: ' + cfg.keyword);
        Object.keys(cfg.on || {}).forEach(function (k) { if (cfg.on[k]) chips.push(k); });
        $('cfg-chips').innerHTML = chips.map(function (c) { return '<span class="cat"><span>' + esc(c) + '</span></span>'; }).join('') || '<span class="muted">not recorded</span>';
        try { $('rebuild').href = '/build#config=' + encodeURIComponent(JSON.stringify(cfg)); } catch (e) {}
      }).catch(function (e) { $('run-lede').textContent = 'Could not reach the server (' + e.message + ').'; });
    }
  }

  // ------------------------------ agent page -------------------------------
  if ($('a-facts')) {
    var name = (new URLSearchParams(location.search)).get('name') || '';
    if (!name) {
      $('h-title').textContent = 'No agent named';
      $('a-strategy').innerHTML = 'Open an agent from the <a href="/arena">board</a>.';
    } else {
      get('/arena/agent/' + encodeURIComponent(name) + '.json').then(function (j) {
        if (!j || !j.ok) { $('h-title').textContent = 'Agent not found'; $('a-strategy').textContent = (j && j.error) || ''; return; }
        var led = j.ledger || {}, bank = +led.bankroll || 1000;
        var ret = led.equity != null ? (100 * (led.equity - bank) / bank) : null;
        document.title = j.name + ' on the oddsrail arena';
        $('h-title').textContent = j.name;
        $('a-strategy').textContent = j.strategy || 'No strategy line given.';
        $('a-facts').innerHTML = fact('return', '<span class="' + cls(ret) + '">' + (ret == null ? '' : (ret > 0 ? '+' : '') + ret.toFixed(2) + '%') + '</span>') +
          fact('equity', usd(led.equity)) + fact('passes', j.runs || 0) +
          fact('schedule', j.schedule === 'hourly' ? 'hourly' : 'manual') +
          fact('since', String(j.since || '').slice(0, 10)) +
          fact('positions', (led.positions || []).length);
        $('a-curve').innerHTML = sparkline(j.history || [], bank);
        $('curve-note').textContent = (j.history || []).length ? ((j.history || []).length + ' passes recorded. ' + j.note) : j.note;
        var last = j.last_pass;
        $('last-tbl').querySelector('tbody').innerHTML = last ? decisionRows(last.decisions) : '<tr><td colspan="5">no pass recorded yet</td></tr>';
        if (j.last_run_id) {
          var p = document.createElement('p'); p.className = 'note';
          p.innerHTML = 'Permalink to this pass: <a href="/run?id=' + esc(j.last_run_id) + '">/run?id=' + esc(j.last_run_id) + '</a>';
          $('last-tbl').parentNode.parentNode.insertBefore(p, $('last-tbl').parentNode.nextSibling);
        }
      }).catch(function (e) { $('h-title').textContent = 'Could not load'; $('a-strategy').textContent = e.message; });
    }
  }
})();
