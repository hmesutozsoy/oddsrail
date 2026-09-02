(function () {
  var CODE = '0xa576c5ce9fabba322d8fa3a8d16738221d1b6b2b0c57b544f757fa9e45a09a90';
  // Mirrors DEFAULT_MAINTAINER_WALLETS in oddsrail/trading.py.
  var MAINTAINER = ['0x69cd073d80d640b10818b0513e7237ac8688d48d'];
  var FEED = 'https://clob.polymarket.com/builder/trades?builder_code=' + CODE;
  var LB = 'https://data-api.polymarket.com/v1/builders/leaderboard?timePeriod=WEEK&limit=50&offset=';

  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function usd(v) { v = +v; return '$' + (v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : v.toFixed(2)); }
  function short(a) { return a.slice(0, 6) + '…' + a.slice(-4); }
  function day(ts) { return new Date(ts * 1000).toISOString().slice(0, 10); }
  function weekStart(ts) {
    var d = new Date(ts * 1000); d.setUTCHours(0, 0, 0, 0);
    d.setUTCDate(d.getUTCDate() - d.getUTCDay());           // back to Sunday
    return d.toISOString().slice(0, 10);
  }
  function thisWeek() { return weekStart(Math.floor(Date.now() / 1000)); }

  function fetchAll(cursor, acc) {
    acc = acc || [];
    var url = FEED + (cursor ? '&next_cursor=' + encodeURIComponent(cursor) : '');
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (d) {
      var rows = Array.isArray(d) ? d : (d.data || []);
      acc = acc.concat(rows);
      var nc = Array.isArray(d) ? null : d.next_cursor;
      if (nc && nc !== 'LTE=' && rows.length && acc.length < 50000) return fetchAll(nc, acc);
      return acc;
    });
  }

  function aggregate(rows) {
    var maint = {}; MAINTAINER.forEach(function (w) { maint[w.toLowerCase()] = true; });
    var weeks = {}, wallets = {};
    rows.forEach(function (r) {
      var w = String(r.maker || r.owner || '').toLowerCase();
      var v = +r.sizeUsdc || (+r.size || 0) * (+r.price || 0);
      var ts = +r.matchTime || +r.createdAt || 0;
      var wk = ts ? weekStart(ts) : 'unknown';
      var m = !!maint[w];
      var W = weeks[wk] || (weeks[wk] = { week: wk, trades: 0, vol: 0, wallets: {}, ext: {}, extVol: 0, maintVol: 0 });
      W.trades++; W.vol += v; W.wallets[w] = 1;
      if (m) W.maintVol += v; else { W.ext[w] = 1; W.extVol += v; }
      var A = wallets[w] || (wallets[w] = { wallet: w, m: m, trades: 0, vol: 0, first: ts, last: ts, tx: r.transactionHash });
      A.trades++; A.vol += v; A.first = Math.min(A.first, ts); A.last = Math.max(A.last, ts);
    });
    return {
      weeks: Object.keys(weeks).sort().reverse().map(function (k) { var W = weeks[k]; return { week: W.week, trades: W.trades, vol: W.vol, wallets: Object.keys(W.wallets).length, ext: Object.keys(W.ext).length, extVol: W.extVol, maintVol: W.maintVol }; }),
      wallets: Object.keys(wallets).map(function (k) { return wallets[k]; }).sort(function (a, b) { return b.vol - a.vol; })
    };
  }

  fetchAll().then(function (rows) {
    var agg = aggregate(rows);
    $('fetched').textContent = new Date().toISOString().replace('T', ' ').slice(0, 16) + ' UTC, ' + rows.length + ' trades on the feed';
    var tw = thisWeek();
    var cur = agg.weeks.filter(function (w) { return w.week === tw; })[0] || { week: tw, trades: 0, vol: 0, wallets: 0, ext: 0, extVol: 0, maintVol: 0 };
    $('ext-wallets').innerHTML = cur.ext + '<small>this week</small>';
    $('ext-kvs').innerHTML = '<dt>external volume</dt><dd>' + usd(cur.extVol) + '</dd><dt>all wallets</dt><dd>' + cur.wallets + '</dd><dt>all volume</dt><dd>' + usd(cur.vol) + '</dd>';
    var pct = cur.vol ? Math.round(100 * cur.maintVol / cur.vol) : 0;
    $('plain').textContent = cur.vol
      ? usd(cur.maintVol) + ' of ' + usd(cur.vol) + ' this week (' + pct + '%) is the maintainer\'s own bot. ' + cur.ext + ' other wallet' + (cur.ext === 1 ? '' : 's') + ' routed through the code.'
      : 'No attributed trades yet this week.';
    var wt = $('weeks-tbl').querySelector('tbody'); wt.innerHTML = '';
    agg.weeks.forEach(function (w) {
      var tr = document.createElement('tr');
      tr.innerHTML = '<th scope="row">' + esc(w.week) + '</th><td>' + w.trades + '</td><td>' + usd(w.vol) + '</td><td>' + w.wallets + '</td><td class="us">' + w.ext + '</td><td class="us">' + usd(w.extVol) + '</td><td>' + usd(w.maintVol) + '</td>';
      wt.appendChild(tr);
    });
    var at = $('wallets-tbl').querySelector('tbody'); at.innerHTML = '';
    if (!agg.wallets.length) at.innerHTML = '<tr><td colspan="7">no trades on the feed</td></tr>';
    agg.wallets.forEach(function (a) {
      var tr = document.createElement('tr');
      tr.innerHTML = '<th scope="row"><a href="https://polygonscan.com/address/' + esc(a.wallet) + '">' + esc(short(a.wallet)) + '</a></th>' +
        '<td>' + (a.m ? 'maintainer (excluded)' : 'external') + '</td><td>' + a.trades + '</td><td>' + usd(a.vol) + '</td><td>' + day(a.first) + '</td><td>' + day(a.last) + '</td>' +
        '<td>' + (a.tx ? '<a href="https://polygonscan.com/tx/' + esc(a.tx) + '">' + esc(a.tx.slice(0, 10)) + '…</a>' : '') + '</td>';
      at.appendChild(tr);
    });
  }).catch(function (e) {
    $('fetched').textContent = 'could not reach the feed (' + e.message + ')';
    $('plain').textContent = 'The feed could not be fetched from your browser; the raw data link at the bottom of the page still works.';
  });

  // leaderboard row for this code, paging by offset
  function get(off) { return fetch(LB + off, { cache: 'no-store' }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }); }
  function find(rows) { for (var i = 0; i < rows.length; i++) if ((rows[i].builderCode || '').toLowerCase() === CODE) return rows[i]; return null; }
  (function step(off) {
    get(off).then(function (p) {
      var h = find(p);
      if (h) {
        $('lb-big').innerHTML = '#' + esc(h.rank) + '<small>this week</small>';
        $('lb-kvs').innerHTML = '<dt>attributed volume</dt><dd>' + usd(h.volume) + '</dd><dt>active users</dt><dd>' + esc(h.activeUsers) + '</dd><dt>status</dt><dd>' + (h.verified ? 'verified builder' : 'unverified') + '</dd>';
        $('lb-meta').textContent = 'data-api.polymarket.com, fetched ' + new Date().toISOString().slice(11, 16) + ' UTC';
      } else if (p.length && off < 500) step(off + 50);
      else { $('lb-big').innerHTML = 'outside top ' + (off + p.length) + '<small>this week</small>'; }
    }).catch(function () { $('lb-big').textContent = 'unavailable'; });
  })(0);
})();
