(function () {
  // copy buttons
  document.querySelectorAll('[data-copy]').forEach(function (b) {
    b.addEventListener('click', function () {
      navigator.clipboard.writeText(b.dataset.copy).then(function () {
        b.textContent = 'copied';
        setTimeout(function () { b.textContent = 'copy'; }, 1400);
      });
    });
  });

  // live builder board, straight from Polymarket's public data API
  var API = 'https://data-api.polymarket.com/v1/builders/leaderboard?timePeriod=WEEK&limit=50&offset=';
  var CODE = '0xa576c5ce9fabba322d8fa3a8d16738221d1b6b2b0c57b544f757fa9e45a09a90';
  var lb = document.getElementById('lb'), meta = document.getElementById('lb-meta');
  var tb = document.querySelector('#lb-tbl tbody'), tip = document.getElementById('tip');
  if (!lb) return;

  function fmt(v) {
    v = +v;
    if (v >= 1e6) return '$' + (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return '$' + (v / 1e3).toFixed(v < 1e4 ? 1 : 0) + 'k';
    return '$' + Math.round(v);
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function showTip(e, r) {
    tip.innerHTML = '<b>' + esc(r.builder) + '</b><br><span>volume</span> ' + fmt(r.volume) +
      '<br><span>active users</span> ' + esc(r.activeUsers) + (r.verified ? '<br><span>verified builder</span>' : '');
    tip.style.display = 'block';
    tip.style.left = Math.min(e.clientX + 14, window.innerWidth - 280) + 'px';
    tip.style.top = (e.clientY + 14) + 'px';
  }
  function hideTip() { tip.style.display = 'none'; }
  function get(off) {
    return fetch(API + off, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }
  function stamp() { return new Date().toISOString().slice(11, 16) + ' UTC'; }

  get(0).then(function (page) {
    var top = page.slice(0, 10);
    var max = Math.max.apply(null, top.map(function (r) { return +r.volume; }));
    lb.innerHTML = ''; tb.innerHTML = '';
    top.forEach(function (r) {
      var row = document.createElement('div');
      row.className = 'row'; row.tabIndex = 0;
      var w = Math.max(1, 100 * (+r.volume) / max);
      row.innerHTML = '<div class="rk">' + esc(r.rank) + '</div><div class="nm">' + esc(r.builder) +
        '</div><div class="bar"><i style="width:' + w.toFixed(1) + '%"></i></div><div class="v">' + fmt(r.volume) + '</div>';
      row.addEventListener('mousemove', function (e) { showTip(e, r); });
      row.addEventListener('mouseleave', hideTip);
      row.addEventListener('focus', function () {
        var b = row.getBoundingClientRect(); showTip({ clientX: b.left + 40, clientY: b.top }, r);
      });
      row.addEventListener('blur', hideTip);
      lb.appendChild(row);
      var tr = document.createElement('tr');
      tr.innerHTML = '<td>' + esc(r.rank) + '</td><td>' + esc(r.builder) + '</td><td>' + fmt(r.volume) +
        '</td><td>' + esc(r.activeUsers) + '</td><td>' + (r.verified ? 'yes' : 'no') + '</td>';
      tb.appendChild(tr);
    });
    meta.textContent = 'data-api.polymarket.com, fetched ' + stamp();
    return page;
  }).then(function (first) {
    var mb = document.getElementById('me-big'), mm = document.getElementById('me-meta'), kv = document.getElementById('me-kvs');
    function find(rows) {
      for (var i = 0; i < rows.length; i++) if ((rows[i].builderCode || '').toLowerCase() === CODE) return rows[i];
      return null;
    }
    function render(r, scanned) {
      mm.textContent = 'fetched ' + stamp();
      if (!r) {
        mb.innerHTML = 'outside top ' + scanned + '<small>this week</small>';
        kv.innerHTML = '<dt>status</dt><dd>verified builder, code live on-chain</dd>';
        return;
      }
      mb.innerHTML = fmt(r.volume) + '<small>attributed</small>';
      kv.innerHTML = '<dt>rank</dt><dd>#' + esc(r.rank) + ' of all builders this week</dd>' +
        '<dt>active users</dt><dd>' + esc(r.activeUsers) + '</dd>' +
        '<dt>status</dt><dd>' + (r.verified ? 'verified builder' : 'unverified') + '</dd>';
    }
    var hit = find(first);
    if (hit) return render(hit, 50);
    var off = 50;
    (function step() {
      if (off >= 500) return render(null, off);
      get(off).then(function (p) {
        var h = find(p);
        if (h || !p.length) return render(h, off + p.length);
        off += 50; step();
      }).catch(function () { render(null, off); });
    })();
  }).catch(function (e) {
    meta.textContent = '';
    lb.innerHTML = '<p class="empty">Could not reach data-api.polymarket.com from your browser (' + esc(e.message) +
      '). The live board is at builders.polymarket.com.</p>';
    document.getElementById('me-meta').textContent = '';
    document.getElementById('me-big').textContent = '…';
  });
})();
