// ── Hedgehog Dashboard ──
// Vanilla JS — no frameworks.

(function () {
  'use strict';

  // ── State ──
  var state = {
    portfolio: null,
    opportunities: null,
    activeTab: 'opportunities',
    expandedGroups: {},
    hedgeSortCol: 'notional',
    hedgeSortAsc: false,
    oppSortCol: 'score',
    oppSortAsc: false,
    posSortCol: 'notional',
    posSortAsc: false,
    lastPortfolioFetch: null,
    lastOppFetch: null,
    refreshTimer: null,
  };

  // ── Helpers ──
  function $(id) { return document.getElementById(id); }

  function esc(s) {
    if (s == null) return '';
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  function fmtUsd(v) {
    if (v == null || isNaN(v)) return '--';
    var abs = Math.abs(v);
    var sign = v < 0 ? '-' : '';
    if (abs >= 1000) return sign + '$' + abs.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (abs >= 1) return sign + '$' + abs.toFixed(2);
    if (abs >= 0.01) return sign + '$' + abs.toFixed(4);
    return sign + '$' + abs.toFixed(6);
  }

  function fmtPct(v) {
    if (v == null || isNaN(v)) return '--';
    return v.toFixed(2) + '%';
  }

  function fmtApy(v) {
    if (v == null || isNaN(v)) return '--';
    var abs = Math.abs(v);
    if (abs >= 1000) return (v > 0 ? '+' : '-') + abs.toFixed(0) + '%';
    if (abs >= 100) return (v > 0 ? '+' : '') + v.toFixed(1) + '%';
    return (v > 0 ? '+' : '') + v.toFixed(2) + '%';
  }

  function fmtVol(vol) {
    if (vol == null || isNaN(vol)) return '--';
    if (vol >= 1e9) return '$' + (vol / 1e9).toFixed(1) + 'B';
    if (vol >= 1e6) return '$' + (vol / 1e6).toFixed(1) + 'M';
    if (vol >= 1e3) return '$' + (vol / 1e3).toFixed(0) + 'k';
    return '$' + vol.toFixed(0);
  }

  function fmtSize(v) {
    if (v == null || isNaN(v)) return '--';
    if (v >= 1000) return v.toLocaleString('en-US', { maximumFractionDigits: 2 });
    if (v >= 1) return v.toFixed(4);
    return v.toFixed(6);
  }

  function fmtHours(h) {
    if (h == null || isNaN(h) || !isFinite(h)) return '--';
    if (h < 1) return (h * 60).toFixed(0) + 'm';
    if (h < 24) return h.toFixed(1) + 'h';
    return (h / 24).toFixed(1) + 'd';
  }

  function fmtLev(v) {
    if (v == null || isNaN(v) || !isFinite(v)) return '--';
    return v.toFixed(1) + 'x';
  }

  function fmtPrice(v) {
    if (v == null || isNaN(v)) return '--';
    if (v >= 10000) return '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 });
    if (v >= 100) return '$' + v.toFixed(2);
    if (v >= 1) return '$' + v.toFixed(4);
    return '$' + v.toFixed(6);
  }

  function valClass(v) {
    if (v == null || isNaN(v) || v === 0) return 'val-zero';
    return v > 0 ? 'val-pos' : 'val-neg';
  }

  function sideClass(s) {
    return s === 'LONG' ? 'side-long' : 'side-short';
  }

  function badgeClass(status) {
    return 'badge badge-' + (status || 'dust');
  }

  function utilColor(pct) {
    if (pct > 70) return 'var(--danger)';
    if (pct > 40) return 'var(--warn)';
    return 'var(--accent)';
  }

  function fundingClass(apy) {
    if (apy == null) return 'funding-neutral';
    var abs = Math.abs(apy);
    if (abs > 50) return apy > 0 ? 'funding-hot' : 'funding-cold';
    if (abs > 15) return apy > 0 ? 'funding-warm' : 'funding-cool';
    return 'funding-neutral';
  }

  function timeAgo(isoStr) {
    if (!isoStr) return '';
    var d = new Date(isoStr);
    var s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 5) return 'just now';
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    return Math.floor(s / 3600) + 'h ago';
  }

  // ── Fetch ──
  var PORTFOLIO_INTERVAL = 30000;
  var OPP_INTERVAL = 60000;

  async function fetchPortfolio() {
    try {
      var resp = await fetch('/api/portfolio');
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      state.portfolio = await resp.json();
      state.lastPortfolioFetch = new Date().toISOString();
      renderTopBar();
      renderHedgeGroups();
      renderBalances();
      renderPositionsTab();
      updateRefreshInfo();
    } catch (e) {
      console.error('Portfolio fetch error:', e);
      updateRefreshInfo('error');
    }
  }

  async function fetchOpportunities() {
    try {
      var resp = await fetch('/api/opportunities');
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      state.opportunities = await resp.json();
      state.lastOppFetch = new Date().toISOString();
      renderOpportunities();
      renderFundingMatrix();
      updateRefreshInfo();
    } catch (e) {
      console.error('Opportunities fetch error:', e);
    }
  }

  function updateRefreshInfo(status) {
    var dot = $('status-dot');
    var txt = $('refresh-text');
    if (status === 'error') {
      dot.className = 'status-dot error';
      txt.textContent = 'Connection error';
      return;
    }
    if (state.lastPortfolioFetch) {
      dot.className = 'status-dot live';
      txt.textContent = 'Updated ' + timeAgo(state.lastPortfolioFetch);
    }
  }

  // ── Render: Top Bar ──
  function renderTopBar() {
    var p = state.portfolio;
    if (!p) return;
    $('m-equity').textContent = fmtUsd(p.total_equity);
    $('m-margin-used').textContent = fmtUsd(p.total_margin_used);
    $('m-margin-free').textContent = fmtUsd(p.total_margin_free);
    $('m-util').textContent = fmtPct(p.total_margin_util_pct);
    $('m-positions').textContent = p.total_positions;
    var upnlEl = $('m-upnl');
    upnlEl.textContent = fmtUsd(p.total_upnl);
    upnlEl.className = 'metric-value ' + valClass(p.total_upnl);
  }

  // ── Render: Hedge Groups ──
  function renderHedgeGroups() {
    var p = state.portfolio;
    if (!p) return;
    var groups = p.hedge_groups || [];
    $('hedge-count').textContent = groups.length + ' group' + (groups.length !== 1 ? 's' : '');

    if (groups.length === 0) {
      $('hedge-body').innerHTML = '<div class="empty-state">No open positions</div>';
      return;
    }

    // Sort groups
    var sorted = groups.slice().sort(function (a, b) {
      var col = state.hedgeSortCol;
      var va = col === 'symbol' ? a.symbol : (col === 'notional' ? a.total_notional : (col === 'upnl' ? a.net_upnl : a.total_notional));
      var vb = col === 'symbol' ? b.symbol : (col === 'notional' ? b.total_notional : (col === 'upnl' ? b.net_upnl : b.total_notional));
      if (typeof va === 'string') {
        var cmp = va.localeCompare(vb);
        return state.hedgeSortAsc ? cmp : -cmp;
      }
      return state.hedgeSortAsc ? va - vb : vb - va;
    });

    var html = '<table class="data-table"><thead><tr>';
    var cols = [
      { key: 'symbol', label: 'Symbol', align: '' },
      { key: 'status', label: 'Status', align: '' },
      { key: 'short', label: 'Short @', align: '' },
      { key: 'long', label: 'Long @', align: '' },
      { key: 'notional', label: 'Notional', align: 'text-right' },
      { key: 'upnl', label: 'uPnL', align: 'text-right' },
      { key: 'legs', label: 'Legs', align: 'text-center' },
    ];
    cols.forEach(function (c) {
      var cls = (state.hedgeSortCol === c.key ? ' sorted' : '') + (c.align ? ' ' + c.align : '');
      var arrow = state.hedgeSortCol === c.key ? (state.hedgeSortAsc ? ' ▲' : ' ▼') : '';
      html += '<th class="' + cls + '" data-sort="hedge:' + c.key + '">' + c.label + '<span class="sort-arrow">' + arrow + '</span></th>';
    });
    html += '</tr></thead><tbody>';

    sorted.forEach(function (g) {
      var expanded = state.expandedGroups[g.symbol];
      var chevron = '<span class="group-chevron ' + (expanded ? 'expanded' : '') + '">&#9654;</span>';

      html += '<tr class="group-row" data-symbol="' + esc(g.symbol) + '">';
      html += '<td><span class="group-symbol">' + chevron + esc(g.symbol) + '</span></td>';
      html += '<td><span class="' + badgeClass(g.status) + '">' + esc(g.status) + '</span></td>';
      html += '<td>' + esc(g.short_venues.join(', ') || '--') + '</td>';
      html += '<td>' + esc(g.long_venues.join(', ') || '--') + '</td>';
      html += '<td class="text-right">' + fmtUsd(g.total_notional) + '</td>';
      html += '<td class="text-right ' + valClass(g.net_upnl) + '">' + fmtUsd(g.net_upnl) + '</td>';
      html += '<td class="text-center">' + g.leg_count + '</td>';
      html += '</tr>';

      if (expanded) {
        g.legs.forEach(function (l) {
          html += '<tr class="leg-row">';
          html += '<td><span class="' + sideClass(l.side) + '">' + esc(l.side) + '</span></td>';
          html += '<td><span class="venue-tag">' + esc(l.venue) + '</span></td>';
          html += '<td colspan="1">' + fmtSize(l.size) + '</td>';
          html += '<td>Entry ' + fmtPrice(l.entry_price) + '</td>';
          html += '<td class="text-right">' + fmtUsd(l.notional) + '</td>';
          html += '<td class="text-right ' + valClass(l.unrealized_pnl) + '">' + fmtUsd(l.unrealized_pnl) + '</td>';
          html += '<td class="text-center">' + fmtLev(l.leverage) + '</td>';
          html += '</tr>';
        });
      }
    });

    html += '</tbody></table>';
    $('hedge-body').innerHTML = html;

    // Bind click handlers
    $('hedge-body').querySelectorAll('.group-row').forEach(function (row) {
      row.addEventListener('click', function () {
        var sym = row.getAttribute('data-symbol');
        state.expandedGroups[sym] = !state.expandedGroups[sym];
        renderHedgeGroups();
      });
    });

    $('hedge-body').querySelectorAll('th[data-sort]').forEach(function (th) {
      th.addEventListener('click', function () {
        var parts = th.getAttribute('data-sort').split(':');
        var col = parts[1];
        if (state.hedgeSortCol === col) {
          state.hedgeSortAsc = !state.hedgeSortAsc;
        } else {
          state.hedgeSortCol = col;
          state.hedgeSortAsc = col === 'symbol';
        }
        renderHedgeGroups();
      });
    });
  }

  // ── Render: Balances ──
  function renderBalances() {
    var p = state.portfolio;
    if (!p) return;
    var venues = (p.venues || []).filter(function (v) { return v.status !== 'skip'; });
    $('venue-count').textContent = venues.length + ' venue' + (venues.length !== 1 ? 's' : '');

    if (venues.length === 0) {
      $('balance-grid').innerHTML = '<div class="empty-state">No venues configured</div>';
      return;
    }

    // Sort: ok first, then by equity descending
    venues.sort(function (a, b) {
      if (a.status === 'ok' && b.status !== 'ok') return -1;
      if (a.status !== 'ok' && b.status === 'ok') return 1;
      return (b.balance?.equity || 0) - (a.balance?.equity || 0);
    });

    var html = '';
    venues.forEach(function (v) {
      var b = v.balance || {};
      var util = b.margin_util_pct || 0;
      var posCount = (v.positions || []).length;

      html += '<div class="balance-card">';
      html += '<div class="venue-name"><span class="venue-dot ' + esc(v.status) + '"></span>' + esc(v.venue) + '</div>';

      if (v.status === 'ok') {
        html += '<div class="bal-equity">' + fmtUsd(b.equity) + '</div>';
        html += '<div class="bal-row"><span class="bal-label">Used</span><span class="bal-value">' + fmtUsd(b.margin_used) + '</span></div>';
        html += '<div class="bal-row"><span class="bal-label">Free</span><span class="bal-value">' + fmtUsd(b.margin_free) + '</span></div>';
        html += '<div class="bal-row"><span class="bal-label">Util</span><span class="bal-value">' + fmtPct(util) + '</span></div>';
        html += '<div class="bal-row"><span class="bal-label">uPnL</span><span class="bal-value ' + valClass(b.unrealized_pnl) + '">' + fmtUsd(b.unrealized_pnl) + '</span></div>';
        html += '<div class="bal-row"><span class="bal-label">Positions</span><span class="bal-value">' + posCount + '</span></div>';
        html += '<div class="util-bar"><div class="util-bar-fill" style="width:' + Math.min(util, 100) + '%;background:' + utilColor(util) + '"></div></div>';
      } else if (v.status === 'error') {
        html += '<div class="error-msg">' + esc(v.error || 'Unknown error') + '</div>';
      } else {
        html += '<div class="empty-state" style="padding:12px;font-size:11px">No credentials</div>';
      }

      html += '</div>';
    });

    $('balance-grid').innerHTML = html;
  }

  // ── Render: Opportunities ──
  function renderOpportunities() {
    var o = state.opportunities;
    if (!o) return;
    var pairs = o.pairs || [];

    if (pairs.length === 0) {
      $('tab-opportunities').innerHTML = '<div class="empty-state">No opportunities found</div>';
      return;
    }

    // Sort
    var sorted = pairs.slice().sort(function (a, b) {
      var col = state.oppSortCol;
      var va, vb;
      if (col === 'symbol') { va = a.symbol; vb = b.symbol; }
      else if (col === 'spread') { va = a.spread; vb = b.spread; }
      else if (col === 'ema') { va = a.ema_spread; vb = b.ema_spread; }
      else if (col === 'be') { va = a.be_hours; vb = b.be_hours; }
      else { va = a.score; vb = b.score; }
      if (typeof va === 'string') {
        var cmp = va.localeCompare(vb);
        return state.oppSortAsc ? cmp : -cmp;
      }
      return state.oppSortAsc ? va - vb : vb - va;
    });

    var html = '<table class="data-table"><thead><tr>';
    var cols = [
      { key: 'rank', label: '#', align: 'text-center' },
      { key: 'score', label: 'Score', align: 'text-right' },
      { key: 'symbol', label: 'Symbol', align: '' },
      { key: 'short_venue', label: 'Short @', align: '' },
      { key: 'short_apy', label: 'S.APY', align: 'text-right' },
      { key: 'short_vol', label: 'S.Vol', align: 'text-right' },
      { key: 'long_venue', label: 'Long @', align: '' },
      { key: 'long_apy', label: 'L.APY', align: 'text-right' },
      { key: 'long_vol', label: 'L.Vol', align: 'text-right' },
      { key: 'spread', label: 'Net APY', align: 'text-right' },
      { key: 'ema', label: 'EMA', align: 'text-right' },
      { key: 'fee', label: 'Fee', align: 'text-right' },
      { key: 'be', label: 'BE(h)', align: 'text-right' },
    ];
    cols.forEach(function (c) {
      var cls = (state.oppSortCol === c.key ? ' sorted' : '') + (c.align ? ' ' + c.align : '');
      var arrow = state.oppSortCol === c.key ? (state.oppSortAsc ? ' ▲' : ' ▼') : '';
      html += '<th class="' + cls + '" data-sort="opp:' + c.key + '">' + c.label + '<span class="sort-arrow">' + arrow + '</span></th>';
    });
    html += '</tr></thead><tbody>';

    sorted.forEach(function (p, i) {
      var scoreClass = p.score >= 50 ? 'opp-score' : 'opp-score low';
      html += '<tr>';
      html += '<td class="text-center">' + (i + 1) + '</td>';
      html += '<td class="text-right ' + scoreClass + '">' + p.score.toFixed(0) + '</td>';
      html += '<td><strong>' + esc(p.symbol) + '</strong></td>';
      html += '<td><span class="venue-tag">' + esc(p.short_venue) + '</span></td>';
      html += '<td class="text-right ' + valClass(p.short_apy) + '">' + fmtApy(p.short_apy) + '</td>';
      html += '<td class="text-right">' + fmtVol(p.short_vol) + '</td>';
      html += '<td><span class="venue-tag">' + esc(p.long_venue) + '</span></td>';
      html += '<td class="text-right ' + valClass(p.long_apy) + '">' + fmtApy(p.long_apy) + '</td>';
      html += '<td class="text-right">' + fmtVol(p.long_vol) + '</td>';
      html += '<td class="text-right ' + valClass(p.spread) + '">' + fmtApy(p.spread) + '</td>';
      html += '<td class="text-right ' + valClass(p.ema_spread) + '">' + fmtApy(p.ema_spread) + '</td>';
      html += '<td class="text-right">' + p.fee_bps.toFixed(0) + 'bp</td>';
      html += '<td class="text-right">' + fmtHours(p.be_hours) + '</td>';
      html += '</tr>';
    });

    html += '</tbody></table>';
    html += '<div style="padding:8px 16px;color:var(--muted);font-size:10px">' + pairs.length + ' pairs found &middot; Updated ' + timeAgo(o.timestamp) + '</div>';
    $('tab-opportunities').innerHTML = html;

    // Bind sort
    $('tab-opportunities').querySelectorAll('th[data-sort]').forEach(function (th) {
      th.addEventListener('click', function () {
        var col = th.getAttribute('data-sort').split(':')[1];
        if (state.oppSortCol === col) {
          state.oppSortAsc = !state.oppSortAsc;
        } else {
          state.oppSortCol = col;
          state.oppSortAsc = col === 'symbol';
        }
        renderOpportunities();
      });
    });
  }

  // ── Render: All Positions ──
  function renderPositionsTab() {
    var p = state.portfolio;
    if (!p) return;

    var allPos = [];
    (p.venues || []).forEach(function (v) {
      (v.positions || []).forEach(function (pos) {
        allPos.push(pos);
      });
    });

    if (allPos.length === 0) {
      $('positions-body').innerHTML = '<div class="empty-state">No open positions</div>';
      return;
    }

    // Sort
    allPos.sort(function (a, b) {
      var col = state.posSortCol;
      var va, vb;
      if (col === 'symbol') { va = a.symbol; vb = b.symbol; }
      else if (col === 'venue') { va = a.venue; vb = b.venue; }
      else if (col === 'side') { va = a.side; vb = b.side; }
      else if (col === 'upnl') { va = a.unrealized_pnl; vb = b.unrealized_pnl; }
      else { va = a.notional; vb = b.notional; }
      if (typeof va === 'string') {
        var cmp = va.localeCompare(vb);
        return state.posSortAsc ? cmp : -cmp;
      }
      return state.posSortAsc ? va - vb : vb - va;
    });

    var html = '<table class="data-table"><thead><tr>';
    var cols = [
      { key: 'venue', label: 'Venue', align: '' },
      { key: 'symbol', label: 'Symbol', align: '' },
      { key: 'side', label: 'Side', align: '' },
      { key: 'size', label: 'Size', align: 'text-right' },
      { key: 'notional', label: 'Notional', align: 'text-right' },
      { key: 'entry', label: 'Entry', align: 'text-right' },
      { key: 'mark', label: 'Mark', align: 'text-right' },
      { key: 'upnl', label: 'uPnL', align: 'text-right' },
      { key: 'lev', label: 'Lev', align: 'text-right' },
    ];
    cols.forEach(function (c) {
      var cls = (state.posSortCol === c.key ? ' sorted' : '') + (c.align ? ' ' + c.align : '');
      var arrow = state.posSortCol === c.key ? (state.posSortAsc ? ' ▲' : ' ▼') : '';
      html += '<th class="' + cls + '" data-sort="pos:' + c.key + '">' + c.label + '<span class="sort-arrow">' + arrow + '</span></th>';
    });
    html += '</tr></thead><tbody>';

    allPos.forEach(function (pos) {
      html += '<tr>';
      html += '<td><span class="venue-tag">' + esc(pos.venue) + '</span></td>';
      html += '<td><strong>' + esc(pos.symbol) + '</strong></td>';
      html += '<td><span class="' + sideClass(pos.side) + '">' + esc(pos.side) + '</span></td>';
      html += '<td class="text-right">' + fmtSize(pos.size) + '</td>';
      html += '<td class="text-right">' + fmtUsd(pos.notional) + '</td>';
      html += '<td class="text-right">' + fmtPrice(pos.entry_price) + '</td>';
      html += '<td class="text-right">' + fmtPrice(pos.mark_price) + '</td>';
      html += '<td class="text-right ' + valClass(pos.unrealized_pnl) + '">' + fmtUsd(pos.unrealized_pnl) + '</td>';
      html += '<td class="text-right">' + fmtLev(pos.leverage) + '</td>';
      html += '</tr>';
    });

    html += '</tbody></table>';
    $('positions-body').innerHTML = html;

    // Bind sort
    $('positions-body').querySelectorAll('th[data-sort]').forEach(function (th) {
      th.addEventListener('click', function () {
        var col = th.getAttribute('data-sort').split(':')[1];
        if (state.posSortCol === col) {
          state.posSortAsc = !state.posSortAsc;
        } else {
          state.posSortCol = col;
          state.posSortAsc = col === 'symbol' || col === 'venue';
        }
        renderPositionsTab();
      });
    });
  }

  // ── Render: Funding Matrix ──
  function renderFundingMatrix() {
    var o = state.opportunities;
    if (!o || !o.funding_matrix) {
      $('funding-body').innerHTML = '<div class="empty-state">No funding data</div>';
      return;
    }

    var matrix = o.funding_matrix;
    var symbols = Object.keys(matrix).sort();

    // Collect all venues
    var venueSet = {};
    symbols.forEach(function (sym) {
      Object.keys(matrix[sym]).forEach(function (v) { venueSet[v] = true; });
    });
    var venues = Object.keys(venueSet).sort();

    if (symbols.length === 0) {
      $('funding-body').innerHTML = '<div class="empty-state">No funding data</div>';
      return;
    }

    // Only show symbols with at least 2 venues and reasonable volume
    var filtered = symbols.filter(function (sym) {
      return Object.keys(matrix[sym]).length >= 2;
    });

    if (filtered.length > 80) {
      filtered = filtered.slice(0, 80);
    }

    var html = '<div style="overflow-x:auto"><table class="data-table"><thead><tr>';
    html += '<th>Symbol</th>';
    venues.forEach(function (v) {
      html += '<th class="text-right">' + esc(v) + '</th>';
    });
    html += '<th class="text-right">Spread</th>';
    html += '</tr></thead><tbody>';

    filtered.forEach(function (sym) {
      var rates = matrix[sym];
      var vals = Object.values(rates);
      var maxRate = Math.max.apply(null, vals);
      var minRate = Math.min.apply(null, vals);
      var spread = maxRate - minRate;

      html += '<tr>';
      html += '<td><strong>' + esc(sym) + '</strong></td>';
      venues.forEach(function (v) {
        var apy = rates[v];
        if (apy != null) {
          html += '<td class="funding-cell ' + fundingClass(apy) + '">' + fmtApy(apy) + '</td>';
        } else {
          html += '<td class="funding-cell funding-neutral">--</td>';
        }
      });
      html += '<td class="funding-cell ' + valClass(spread) + '" style="font-weight:600">' + fmtApy(spread) + '</td>';
      html += '</tr>';
    });

    html += '</tbody></table></div>';
    html += '<div style="padding:8px 16px;color:var(--muted);font-size:10px">' + filtered.length + ' symbols shown &middot; APY = annualized funding rate</div>';
    $('funding-body').innerHTML = html;
  }

  // ── Tab switching ──
  function initTabs() {
    document.querySelectorAll('.tab-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var tab = btn.getAttribute('data-tab');
        state.activeTab = tab;
        document.querySelectorAll('.tab-btn').forEach(function (b) { b.classList.remove('active'); });
        document.querySelectorAll('.tab-content').forEach(function (c) { c.classList.remove('active'); });
        btn.classList.add('active');
        var content = $('tab-' + tab);
        if (content) content.classList.add('active');
      });
    });
  }

  // ── Refresh timer (updates "X ago" text) ──
  function startRefreshTimer() {
    setInterval(function () {
      updateRefreshInfo();
    }, 5000);
  }

  // ── Init ──
  function init() {
    initTabs();
    startRefreshTimer();

    // Initial fetch
    fetchPortfolio();
    fetchOpportunities();

    // Polling
    setInterval(fetchPortfolio, PORTFOLIO_INTERVAL);
    setInterval(fetchOpportunities, OPP_INTERVAL);
  }

  // Start when DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
