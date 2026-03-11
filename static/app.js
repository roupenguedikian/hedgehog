// ── RHGBot Trading Terminal ──
// Pure vanilla JS — no frameworks, no build tools.

(function () {
  'use strict';

  // ── State ──
  var state = {
    snapshot: null,
    activeTab: 'entry',
    sortCol: 'symbol',
    sortAsc: true,
    expandedGroups: {},       // symbol -> boolean
    activityFilter: 'all',
    activitySymbolFilter: null,
    activityLog: [],          // {ts, cat, msg, symbol?}
    prevPositionSymbols: {},  // symbol -> true (for new-highlight detection)
    newSymbols: {},           // symbol -> timestamp
    stopConfirmPending: false,
    stopConfirmTimer: null,
    configSchema: null,
    configEdits: {},          // key -> value
    ws: null,
    wsReconnectDelay: 1000,
  };

  // ── Helpers ──
  function $(id) { return document.getElementById(id); }

  function tokenHeaders() {
    var token = localStorage.getItem('rhgbot_token');
    var h = { 'Content-Type': 'application/json' };
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
  }

  function fmtUsd(v) {
    if (v == null || isNaN(v)) return '--';
    var abs = Math.abs(v);
    var sign = v < 0 ? '-' : '';
    if (abs >= 1000) return sign + '$' + abs.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (abs >= 1) return sign + '$' + abs.toFixed(2);
    return sign + '$' + abs.toFixed(4);
  }

  function fmtPct(v, digits) {
    if (v == null || isNaN(v)) return '--';
    return (v * 100).toFixed(digits || 3) + '%';
  }

  function fmtDpy(v) {
    if (v == null || isNaN(v)) return '--';
    return (v * 100).toFixed(4) + '%';
  }

  function dpyToApy(v) {
    if (v == null || isNaN(v)) return null;
    if (v <= -1) return -1;
    return Math.pow(1 + v, 365) - 1;
  }

  function fmtApy(v) {
    if (v == null || isNaN(v)) return '--';
    return (v * 100).toFixed(2) + '%';
  }

  function fmtDpyApy(v) {
    if (v == null || isNaN(v)) return '--';
    return fmtDpy(v) + ' / ' + fmtApy(dpyToApy(v));
  }

  function fmtLeverage(v) {
    if (v == null || isNaN(v) || !isFinite(v)) return '--';
    return v.toFixed(2) + 'x';
  }

  function fmtHours(h) {
    if (h == null || isNaN(h)) return '--';
    if (h < 1) return (h * 60).toFixed(0) + 'm';
    if (h < 24) return h.toFixed(1) + 'h';
    return (h / 24).toFixed(1) + 'd';
  }

  function fmtTime(iso) {
    if (!iso) return '--';
    try {
      var d = new Date(iso);
      return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (e) { return '--'; }
  }

  function fmtShortTime(iso) {
    if (!iso) return '';
    try {
      var d = new Date(iso);
      return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
    } catch (e) { return ''; }
  }

  function dpyClass(v) {
    if (v == null || isNaN(v)) return '';
    var abs = Math.abs(v);
    if (v > 0 && abs > 0.01) return 'dpy-bright-positive';
    if (v > 0) return 'dpy-positive';
    if (v < 0 && abs > 0.01) return 'dpy-bright-negative';
    if (v < 0) return 'dpy-negative';
    return '';
  }

  function pnlClass(v) {
    if (v == null || isNaN(v) || v === 0) return '';
    return v > 0 ? 'pnl-positive' : 'pnl-negative';
  }

  function esc(s) {
    if (s == null) return '';
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  function exchangeShort(name) {
    if (!name) return '--';
    var s = String(name);
    var map = {
      'Hyperliquid': 'HL',
      'Drift': 'Drift',
      'DydxV4': 'dYdX',
      'Injective': 'INJ',
      'Paradex': 'PDX',
      'ApexOmni': 'Apex',
      'Lighter': 'LTR',
      'Ethereal': 'ETH',
      'Aster': 'Aster',
    };
    return map[s] || s;
  }

  // ── Auth ──
  function ensureAuth() {
    if (localStorage.getItem('rhgbot_token') != null) return;
    $('authModal').style.display = 'flex';
  }

  window.saveToken = function () {
    var token = $('authTokenInput').value.trim();
    if (token) localStorage.setItem('rhgbot_token', token);
    else localStorage.setItem('rhgbot_token', '');
    $('authModal').style.display = 'none';
    connectWs();
  };

  window.skipAuth = function () {
    localStorage.setItem('rhgbot_token', '');
    $('authModal').style.display = 'none';
    connectWs();
  };

  // ── WebSocket ──
  function connectWs() {
    if (state.ws) {
      try { state.ws.close(); } catch (e) { /* ignore */ }
    }
    var proto = location.protocol === 'https:' ? 'wss' : 'ws';
    var token = localStorage.getItem('rhgbot_token');
    var url = proto + '://' + location.host + '/api/v1/stream';
    if (token) url += '?token=' + encodeURIComponent(token);

    var ws = new WebSocket(url);
    state.ws = ws;

    ws.onopen = function () {
      state.wsReconnectDelay = 1000;
      updateStatePill('connecting', 'connected');
    };

    ws.onmessage = function (ev) {
      try {
        var msg = JSON.parse(ev.data);
        if (msg.event === 'snapshot.full' || msg.event === 'snapshot.initial' || msg.event === 'snapshot.tick') {
          // Full replace (backward compatible with old snapshot.tick format)
          state.snapshot = msg.payload || msg;
          processSnapshot(state.snapshot);
          render();
        } else if (msg.event === 'snapshot.delta') {
          // Merge only changed sections into existing snapshot
          if (state.snapshot && msg.payload) {
            var keys = msg.changed || Object.keys(msg.payload);
            for (var i = 0; i < keys.length; i++) {
              state.snapshot[keys[i]] = msg.payload[keys[i]];
            }
            if (msg.seq != null) state.snapshot.seq = msg.seq;
            if (msg.ts != null) state.snapshot.generated_at = msg.ts;
            processSnapshot(state.snapshot);
            render();
          }
        } else if (msg.payload) {
          // Fallback for unknown event types that carry a payload
          state.snapshot = msg.payload;
          processSnapshot(msg.payload);
          render();
        }
      } catch (e) { /* ignore parse errors */ }
    };

    ws.onclose = function () {
      updateStatePill('stopped', 'disconnected');
      setTimeout(function () {
        state.wsReconnectDelay = Math.min(state.wsReconnectDelay * 1.5, 10000);
        connectWs();
      }, state.wsReconnectDelay);
    };

    ws.onerror = function () {
      updateStatePill('error', 'ws error');
    };
  }

  // ── Process Snapshot ──
  function processSnapshot(snap) {
    // Track new position symbols for highlight
    var currentSymbols = {};
    var positions = snap.positions || [];
    for (var i = 0; i < positions.length; i++) {
      var sym = positions[i].normalized_symbol || positions[i].symbol || '';
      currentSymbols[sym] = true;
      if (!state.prevPositionSymbols[sym]) {
        state.newSymbols[sym] = Date.now();
      }
    }
    state.prevPositionSymbols = currentSymbols;

    // Expire highlights after 5s
    var now = Date.now();
    for (var s in state.newSymbols) {
      if (now - state.newSymbols[s] > 5000) {
        delete state.newSymbols[s];
      }
    }

    // Parse dashboard_monitor into activity entries
    var monitor = snap.dashboard_monitor;
    if (monitor) {
      addMonitorActivity('entry', monitor.entry);
      addMonitorActivity('hedge', monitor.hedge);
      addMonitorActivity('rotation', monitor.rotation);
      addMonitorActivity('exit', monitor.exit);
      if (monitor.health) {
        var hlevel = (monitor.health.level || '').toLowerCase();
        var hcat = hlevel === 'red' ? 'health' : (hlevel === 'yellow' ? 'health' : 'health');
        addActivityEntry(hcat, monitor.health.summary || 'health ok');
      }
    }
  }

  function addMonitorActivity(cat, proc) {
    if (!proc) return;
    if (proc.next_move && proc.next_move !== '-' && proc.next_move !== '') {
      addActivityEntry(cat, proc.next_move);
    }
    var seeing = proc.seeing || [];
    for (var i = 0; i < seeing.length; i++) {
      addActivityEntry(cat, seeing[i]);
    }
    var errors = proc.errors || [];
    for (var j = 0; j < errors.length; j++) {
      addActivityEntry('health', errors[j]);
    }
  }

  function addActivityEntry(cat, msg) {
    if (!msg) return;
    // Deduplicate: don't add if last entry has same cat+msg
    var last = state.activityLog[0];
    if (last && last.cat === cat && last.msg === msg) return;

    // Extract symbol from message if possible
    var symMatch = msg.match(/\b([A-Z]{2,10})\b/);
    var symbol = symMatch ? symMatch[1] : null;
    // Filter out common non-symbol words
    var nonSymbols = ['ALL', 'OK', 'NO', 'USD', 'DPY', 'BPS', 'ETH', 'BTC'];
    // Actually ETH and BTC are valid symbols, keep them

    state.activityLog.unshift({
      ts: new Date().toISOString(),
      cat: cat,
      msg: msg,
      symbol: symbol,
    });

    // Cap at 500
    if (state.activityLog.length > 500) {
      state.activityLog.length = 500;
    }
  }

  // ── State Pill ──
  function updateStatePill(stateVal, text) {
    var pill = $('statePill');
    if (!pill) return;
    pill.setAttribute('data-state', stateVal || 'stopped');
    var textEl = pill.querySelector('.state-text');
    if (textEl) textEl.textContent = text || stateVal || 'unknown';
  }

  // ── Runtime Actions ──
  window.runtimeAction = function (action) {
    fetch('/api/v1/runtime/' + action, {
      method: 'POST',
      headers: tokenHeaders(),
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (!data.ok) {
          alert('Action failed: ' + (data.error || 'unknown'));
        }
      })
      .catch(function (e) {
        alert('Action failed: ' + e.message);
      });
  };

  window.handleStop = function () {
    if (!state.stopConfirmPending) {
      state.stopConfirmPending = true;
      var btn = $('btnStop');
      if (btn) {
        btn.classList.add('confirm-pending');
        btn.textContent = 'Confirm Stop?';
      }
      state.stopConfirmTimer = setTimeout(function () {
        state.stopConfirmPending = false;
        if (btn) {
          btn.classList.remove('confirm-pending');
          btn.textContent = 'Stop';
        }
      }, 3000);
      return;
    }
    // Double-click confirmed
    clearTimeout(state.stopConfirmTimer);
    state.stopConfirmPending = false;
    var btn2 = $('btnStop');
    if (btn2) {
      btn2.classList.remove('confirm-pending');
      btn2.textContent = 'Stop';
    }
    runtimeAction('stop');
  };

  // ── Render ──
  function render() {
    var snap = state.snapshot;
    if (!snap) return;

    renderTopBar(snap);
    renderPortfolioSummary(snap);
    renderPositions(snap);
    renderActivityFeed();
    renderSecondaryTab(snap);
  }

  // ── Top Bar ──
  function renderTopBar(snap) {
    var runtime = snap.runtime_status || {};
    var status = snap.status || {};
    var summary = buildPortfolioSummary(snap);

    var stateVal = runtime.state || 'stopped';
    var stateText = stateVal;
    if (status.paused) { stateVal = 'paused'; stateText = 'paused'; }
    if (status.kill_switch) stateText += ' [KILL]';
    if (status.dry_run || (snap.config && snap.config.dry_run)) stateText += ' [DRY]';
    updateStatePill(stateVal, stateText);

    $('metricPositions').textContent = String(summary.groupCount);
    $('metricBalance').textContent = fmtUsd(summary.totalBalance);
    $('metricUsed').textContent = fmtUsd(summary.totalUsed);
    $('metricLeverage').textContent = fmtLeverage(summary.totalLeverage);
    $('metricAdpy').textContent = fmtDpyApy(summary.totalAdpy);
    $('metricAdpy').className = 'metric-value ' + dpyClass(summary.totalAdpy);
    $('metricPayHr').textContent = fmtUsd(summary.totalPayHr) + '/hr';
    $('metricPayHr').className = 'metric-value ' + pnlClass(summary.totalPayHr);
    $('metricUpnl').textContent = fmtUsd(summary.totalUpnl);
    $('metricUpnl').className = 'metric-value ' + pnlClass(summary.totalUpnl);

    $('positionCount').textContent = String(summary.groupCount);
  }

  // ── Group Positions by normalized_symbol ──
  function groupPositions(positions) {
    var groups = {};
    for (var i = 0; i < positions.length; i++) {
      var p = positions[i];
      var sym = p.normalized_symbol || p.symbol || 'UNKNOWN';
      if (!groups[sym]) groups[sym] = [];
      groups[sym].push(p);
    }
    return groups;
  }

  // ── Compute DPY for a group ──
  function fundingDpyForGroup(legs) {
    // DPY = sum of (-sign * notional * funding_rate_hr * 24) / sum of notional
    var totalPay = 0;
    var totalNotional = 0;
    for (var i = 0; i < legs.length; i++) {
      var p = legs[i];
      var sign = (p.side || '').toUpperCase() === 'LONG' ? 1.0 : -1.0;
      var notional = Math.abs(p.notional || 0);
      totalPay += -sign * notional * (p.funding_rate_hr || 0) * 24;
      totalNotional += notional;
    }
    if (totalNotional === 0) return 0;
    return totalPay / totalNotional;
  }

  // ── Compute Net Pay/hr for a group ──
  function netPayHrForGroup(legs) {
    var total = 0;
    for (var i = 0; i < legs.length; i++) {
      var p = legs[i];
      var sign = (p.side || '').toUpperCase() === 'LONG' ? 1.0 : -1.0;
      total += -sign * Math.abs(p.notional || 0) * (p.funding_rate_hr || 0);
    }
    return total;
  }

  function buildExchangeStateMaps(snap) {
    var maps = { active: {}, closeOnly: {}, disabled: {} };
    var status = snap.status || {};
    var connectors = snap.connectors || [];

    function exchangeName(entry) {
      if (Array.isArray(entry)) return entry[0];
      if (entry && typeof entry === 'object') return entry.exchange;
      return entry;
    }

    var active = status.active_exchanges || [];
    for (var i = 0; i < active.length; i++) {
      maps.active[String(active[i])] = true;
    }

    var closeOnly = status.close_only_exchanges || [];
    for (var j = 0; j < closeOnly.length; j++) {
      maps.closeOnly[String(exchangeName(closeOnly[j]))] = true;
    }

    var disabled = status.disabled_exchanges || [];
    for (var k = 0; k < disabled.length; k++) {
      maps.disabled[String(exchangeName(disabled[k]))] = true;
    }

    for (var c = 0; c < connectors.length; c++) {
      var connector = connectors[c];
      var name = String(connector.exchange);
      if (connector.disabled_reason) {
        maps.disabled[name] = true;
      } else if (connector.close_only) {
        maps.closeOnly[name] = true;
      } else if (connector.enabled) {
        maps.active[name] = true;
      }
    }

    return maps;
  }

  function buildPortfolioSummary(snap) {
    var positions = snap.positions || [];
    var balances = snap.balances || [];
    var orders = snap.orders || [];
    var groups = groupPositions(positions);
    var stateMaps = buildExchangeStateMaps(snap);
    var rows = {};
    var totalBalance = 0;
    var totalFree = 0;
    var totalNotional = 0;
    var totalPayHr = 0;
    var totalUpnl = 0;

    function ensureRow(exchange) {
      var key = String(exchange || 'Unknown');
      if (!rows[key]) {
        rows[key] = {
          exchange: key,
          state: stateMaps.disabled[key] ? 'DISABLED' : (stateMaps.closeOnly[key] ? 'CLOSE_ONLY' : (stateMaps.active[key] ? 'ACTIVE' : 'UNKNOWN')),
          balance: null,
          free: null,
          used: null,
          notional: 0,
          leverage: null,
          positionCount: 0,
          openOrders: 0,
          payHr: 0,
          upnl: 0,
          adpy: null,
          apy: null,
        };
      }
      return rows[key];
    }

    var activeList = Object.keys(stateMaps.active);
    for (var a = 0; a < activeList.length; a++) ensureRow(activeList[a]);
    var closeList = Object.keys(stateMaps.closeOnly);
    for (var b = 0; b < closeList.length; b++) ensureRow(closeList[b]);
    var disabledList = Object.keys(stateMaps.disabled);
    for (var d = 0; d < disabledList.length; d++) ensureRow(disabledList[d]);

    for (var i = 0; i < balances.length; i++) {
      var balance = balances[i];
      var balanceRow = ensureRow(balance.exchange);
      var equity = Number(balance.usd_value || 0);
      var free = Number(balance.free_collateral || 0);
      balanceRow.balance = (balanceRow.balance == null ? 0 : balanceRow.balance) + equity;
      balanceRow.free = (balanceRow.free == null ? 0 : balanceRow.free) + free;
      totalBalance += Math.max(equity, 0);
      totalFree += Math.max(free, 0);
    }

    for (var p = 0; p < positions.length; p++) {
      var position = positions[p];
      var positionRow = ensureRow(position.exchange);
      var notional = Math.abs(Number(position.notional || 0));
      var side = String(position.side || '').toUpperCase();
      var sideSign = side === 'LONG' ? 1.0 : (side === 'SHORT' ? -1.0 : 0.0);
      var payHr = -sideSign * notional * Number(position.funding_rate_hr || 0);

      positionRow.notional += notional;
      positionRow.positionCount += 1;
      positionRow.payHr += payHr;
      positionRow.upnl += Number(position.unrealized_pnl || 0);

      totalNotional += notional;
      totalPayHr += payHr;
      totalUpnl += Number(position.unrealized_pnl || 0);
    }

    for (var o = 0; o < orders.length; o++) {
      ensureRow(orders[o].exchange).openOrders += 1;
    }

    var totalUsed = Math.max(totalBalance - totalFree, 0);
    var totalLeverage = totalBalance > 0 ? totalNotional / totalBalance : null;
    var totalAdpy = totalNotional > 0 ? (totalPayHr * 24) / totalNotional : null;
    var totalApy = dpyToApy(totalAdpy);

    var exchangeRows = [];
    for (var exchangeKey in rows) {
      if (!Object.prototype.hasOwnProperty.call(rows, exchangeKey)) continue;
      var row = rows[exchangeKey];
      if (row.balance != null && row.free != null) {
        row.used = Math.max(row.balance - row.free, 0);
        row.leverage = row.balance > 0 ? row.notional / row.balance : null;
      }
      if (row.notional > 0) {
        row.adpy = (row.payHr * 24) / row.notional;
        row.apy = dpyToApy(row.adpy);
      }
      exchangeRows.push(row);
    }

    var rank = { ACTIVE: 0, CLOSE_ONLY: 1, DISABLED: 2, UNKNOWN: 3 };
    exchangeRows.sort(function (left, right) {
      var stateCmp = (rank[left.state] || 99) - (rank[right.state] || 99);
      if (stateCmp !== 0) return stateCmp;
      return exchangeShort(left.exchange).localeCompare(exchangeShort(right.exchange));
    });

    return {
      groupCount: Object.keys(groups).length,
      totalBalance: totalBalance,
      totalFree: totalFree,
      totalUsed: totalUsed,
      totalNotional: totalNotional,
      totalLeverage: totalLeverage,
      totalPayHr: totalPayHr,
      totalUpnl: totalUpnl,
      totalAdpy: totalAdpy,
      totalApy: totalApy,
      totalPositions: positions.length,
      totalOpenOrders: orders.length,
      exchangeRows: exchangeRows,
    };
  }

  function exchangeStateHtml(stateName) {
    var cls = 'unknown';
    var label = 'Unknown';
    if (stateName === 'ACTIVE') {
      cls = 'active';
      label = 'Active';
    } else if (stateName === 'CLOSE_ONLY') {
      cls = 'close-only';
      label = 'Close';
    } else if (stateName === 'DISABLED') {
      cls = 'disabled';
      label = 'Off';
    }
    return '<span class="exchange-state ' + cls + '">' + esc(label) + '</span>';
  }

  function renderPortfolioSummary(snap) {
    var container = $('portfolioSummary');
    if (!container) return;

    var summary = buildPortfolioSummary(snap);
    if (summary.totalBalance <= 0 && summary.totalNotional <= 0 && summary.totalOpenOrders === 0 && summary.exchangeRows.length === 0) {
      container.innerHTML = '';
      return;
    }

    var html = '<div class="portfolio-summary-grid">';
    html += '<div class="summary-card"><span class="summary-label">Balance</span><span class="summary-value">' + fmtUsd(summary.totalBalance) + '</span></div>';
    html += '<div class="summary-card"><span class="summary-label">Used</span><span class="summary-value">' + fmtUsd(summary.totalUsed) + '</span></div>';
    html += '<div class="summary-card"><span class="summary-label">Free</span><span class="summary-value">' + fmtUsd(summary.totalFree) + '</span></div>';
    html += '<div class="summary-card"><span class="summary-label">Leverage</span><span class="summary-value">' + fmtLeverage(summary.totalLeverage) + '</span></div>';
    html += '<div class="summary-card summary-card-wide"><span class="summary-label">aDPY / APY</span><span class="summary-value ' + dpyClass(summary.totalAdpy) + '">' + fmtDpyApy(summary.totalAdpy) + '</span></div>';
    html += '<div class="summary-card"><span class="summary-label">Open Orders</span><span class="summary-value">' + String(summary.totalOpenOrders) + '</span></div>';
    html += '<div class="summary-card"><span class="summary-label">Net Pay/hr</span><span class="summary-value ' + pnlClass(summary.totalPayHr) + '">' + fmtUsd(summary.totalPayHr) + '/hr</span></div>';
    html += '<div class="summary-card"><span class="summary-label">uPnL</span><span class="summary-value ' + pnlClass(summary.totalUpnl) + '">' + fmtUsd(summary.totalUpnl) + '</span></div>';
    html += '</div>';

    if (summary.exchangeRows.length > 0) {
      html += '<div class="portfolio-subheader">Exchange Snapshot</div>';
      html += '<div class="portfolio-table-wrap"><table class="portfolio-table"><thead><tr>';
      html += '<th>Exchange</th><th>Status</th><th class="text-right">Balance</th><th class="text-right">Used</th><th class="text-right">Free</th>';
      html += '<th class="text-right">Notional</th><th class="text-right">Lev</th><th class="text-right">Pos</th><th class="text-right">Orders</th>';
      html += '<th class="text-right">aDPY / APY</th><th class="text-right">Net Pay/hr</th><th class="text-right">uPnL</th>';
      html += '</tr></thead><tbody>';

      for (var r = 0; r < summary.exchangeRows.length; r++) {
        var row = summary.exchangeRows[r];
        html += '<tr>';
        html += '<td>' + esc(exchangeShort(row.exchange)) + '</td>';
        html += '<td>' + exchangeStateHtml(row.state) + '</td>';
        html += '<td class="text-right">' + (row.balance == null ? '--' : fmtUsd(row.balance)) + '</td>';
        html += '<td class="text-right">' + (row.used == null ? '--' : fmtUsd(row.used)) + '</td>';
        html += '<td class="text-right">' + (row.free == null ? '--' : fmtUsd(row.free)) + '</td>';
        html += '<td class="text-right">' + (row.notional > 0 ? fmtUsd(row.notional) : '--') + '</td>';
        html += '<td class="text-right">' + fmtLeverage(row.leverage) + '</td>';
        html += '<td class="text-right">' + String(row.positionCount) + '</td>';
        html += '<td class="text-right">' + String(row.openOrders) + '</td>';
        html += '<td class="text-right ' + dpyClass(row.adpy) + '">' + fmtDpyApy(row.adpy) + '</td>';
        html += '<td class="text-right ' + pnlClass(row.payHr) + '">' + fmtUsd(row.payHr) + '/hr</td>';
        html += '<td class="text-right ' + pnlClass(row.upnl) + '">' + fmtUsd(row.upnl) + '</td>';
        html += '</tr>';
      }

      html += '</tbody></table></div>';
    }

    container.innerHTML = html;
  }

  // ── Canonical group hedge status ──
  function groupHedgeStatus(legs) {
    var sawHedged = false;
    var sawDrift = false;
    for (var i = 0; i < legs.length; i++) {
      var s = (legs[i].hedge_status || '').toUpperCase();
      if (s === 'HEDGED') sawHedged = true;
      if (s === 'DRIFT_DETECTED') sawDrift = true;
    }
    if (sawDrift) return 'DRIFT';
    if (sawHedged) return 'HEDGED';
    if (legs.length >= 2) return 'UNHEDGED';
    return 'UNHEDGED';
  }

  // ── Status badge ──
  function statusBadgeHtml(status) {
    var cls = 'hedged';
    var text = status;
    switch (status) {
      case 'HEDGED': cls = 'hedged'; text = 'Active'; break;
      case 'UNHEDGED': cls = 'unhedged'; text = 'Unhedged'; break;
      case 'DRIFT': cls = 'drift'; text = 'Drift'; break;
      case 'EXITING': cls = 'exiting'; text = 'Exiting'; break;
      case 'ROTATING': cls = 'rotating'; text = 'Rotating'; break;
      case 'DUST': cls = 'dust'; text = 'Dust'; break;
    }
    return '<span class="status-badge ' + cls + '">' + esc(text) + '</span>';
  }

  // ── Positions Table ──
  function renderPositions(snap) {
    var positions = snap.positions || [];
    var container = $('positionsTable');

    if (positions.length === 0) {
      container.innerHTML = '<div class="empty-state">No open positions</div>';
      return;
    }

    var groups = groupPositions(positions);
    var groupList = [];
    for (var sym in groups) {
      var legs = groups[sym];
      var gNotional = 0;
      var gUpnl = 0;
      var gPayHr = netPayHrForGroup(legs);
      var gDpy = fundingDpyForGroup(legs);
      var gBestDpy = null;
      var gStatus = groupHedgeStatus(legs);
      var longEx = '--';
      var shortEx = '--';

      for (var i = 0; i < legs.length; i++) {
        gNotional += Math.abs(legs[i].notional || 0);
        gUpnl += (legs[i].unrealized_pnl || 0);
        if ((legs[i].side || '').toUpperCase() === 'LONG') longEx = exchangeShort(legs[i].exchange);
        if ((legs[i].side || '').toUpperCase() === 'SHORT') shortEx = exchangeShort(legs[i].exchange);
        if (legs[i].best_symbol_dpy != null) {
          if (gBestDpy == null || legs[i].best_symbol_dpy > gBestDpy) {
            gBestDpy = legs[i].best_symbol_dpy;
          }
        }
      }

      groupList.push({
        symbol: sym,
        legs: legs,
        longEx: longEx,
        shortEx: shortEx,
        notional: gNotional,
        upnl: gUpnl,
        payHr: gPayHr,
        dpy: gDpy,
        bestDpy: gBestDpy,
        status: gStatus,
      });
    }

    // Sort
    groupList.sort(function (a, b) {
      var va = a[state.sortCol];
      var vb = b[state.sortCol];
      if (va == null) va = '';
      if (vb == null) vb = '';
      var cmp = 0;
      if (typeof va === 'number' && typeof vb === 'number') {
        cmp = va - vb;
      } else {
        cmp = String(va).localeCompare(String(vb));
      }
      return state.sortAsc ? cmp : -cmp;
    });

    var cols = [
      { key: 'symbol', label: 'Symbol' },
      { key: 'notional', label: 'Size USD', right: true },
      { key: 'dpy', label: 'aDPY / APY', right: true },
      { key: 'payHr', label: 'Net Pay/hr', right: true },
      { key: 'upnl', label: 'uPnL', right: true },
      { key: 'status', label: 'Status' },
    ];

    var html = '<table class="pos-table"><thead><tr>';
    for (var c = 0; c < cols.length; c++) {
      var col = cols[c];
      var arrow = '';
      if (state.sortCol === col.key) {
        arrow = '<span class="sort-arrow">' + (state.sortAsc ? '\u25B2' : '\u25BC') + '</span>';
      }
      html += '<th class="' + (col.right ? 'text-right' : '') + '" data-sort="' + col.key + '">';
      html += esc(col.label) + arrow + '</th>';
    }
    html += '</tr></thead><tbody>';

    for (var g = 0; g < groupList.length; g++) {
      var grp = groupList[g];
      var isNew = state.newSymbols[grp.symbol] ? ' new-highlight' : '';
      var isExpanded = state.expandedGroups[grp.symbol];

      html += '<tr class="group-row' + isNew + '" data-symbol="' + esc(grp.symbol) + '">';
      html += '<td>' + esc(grp.symbol) + ' <span class="leg-exchanges">' + esc(grp.longEx) + ' / ' + esc(grp.shortEx) + '</span></td>';
      html += '<td class="text-right">' + fmtUsd(grp.notional) + '</td>';
      html += '<td class="text-right ' + dpyClass(grp.dpy) + '">' + fmtDpyApy(grp.dpy) + '</td>';
      html += '<td class="text-right ' + pnlClass(grp.payHr) + '">' + fmtUsd(grp.payHr) + '</td>';
      html += '<td class="text-right ' + pnlClass(grp.upnl) + '">' + fmtUsd(grp.upnl) + '</td>';
      html += '<td>' + statusBadgeHtml(grp.status) + '</td>';
      html += '</tr>';

      // Always show legs with rotation info
      for (var l = 0; l < grp.legs.length; l++) {
        var leg = grp.legs[l];
        var legSide = (leg.side || '').toUpperCase();
        var legSign = legSide === 'LONG' ? 1.0 : -1.0;
        var legNotional = Math.abs(leg.notional || 0);
        var legPayHr = -legSign * legNotional * (leg.funding_rate_hr || 0);
        var legDpy = legNotional > 0 ? (legPayHr * 24) / legNotional : 0;

        // Build rotation candidate string
        var rotInfo = '--';
        var rotClass = '';
        if (leg.best_alternative_exchange != null && String(leg.best_alternative_exchange) !== String(leg.exchange)) {
          var bestAltRate = leg.best_alternative_funding_rate_hr;
          var currentRate = leg.funding_rate_hr || 0;
          var altPayHr = -legSign * legNotional * (bestAltRate || 0);
          var currentPayHr = legPayHr;
          var improvementHr = altPayHr - currentPayHr;
          var feePct = leg.best_alternative_rotation_fee_pct;
          var feeCost = feePct != null ? feePct * legNotional : 0;
          var breakeven = improvementHr > 0 ? feeCost / improvementHr : Infinity;

          if (improvementHr > 0) {
            rotInfo = exchangeShort(leg.best_alternative_exchange);
            rotInfo += ' +' + fmtUsd(improvementHr) + '/hr';
            if (feePct != null) {
              rotInfo += ' (fee ' + fmtUsd(feeCost) + ', be ' + (breakeven < 999 ? breakeven.toFixed(1) + 'h' : '--') + ')';
            }
            rotClass = breakeven < 10 ? ' rot-good' : ' rot-marginal';
          } else {
            rotInfo = exchangeShort(leg.best_alternative_exchange) + ' (best)';
            rotClass = ' rot-at-best';
          }
        } else if (leg.best_alternative_exchange != null) {
          rotInfo = 'at best';
          rotClass = ' rot-at-best';
        }

        html += '<tr class="leg-row">';
        html += '<td><span class="leg-side ' + legSide.toLowerCase() + '">' + esc(legSide) + '</span> ' + esc(exchangeShort(leg.exchange)) + '</td>';
        html += '<td class="text-right">' + fmtUsd(legNotional) + '</td>';
        html += '<td class="text-right">' + fmtPct(leg.funding_rate_hr, 4) + '/hr</td>';
        html += '<td class="text-right ' + pnlClass(legPayHr) + '">' + fmtUsd(legPayHr) + '</td>';
        html += '<td class="text-right ' + pnlClass(leg.unrealized_pnl) + '">' + fmtUsd(leg.unrealized_pnl) + '</td>';
        html += '<td class="rot-candidate' + rotClass + '">' + rotInfo + '</td>';
        html += '</tr>';
      }
    }

    html += '</tbody></table>';
    container.innerHTML = html;

    // Bind sort click
    var ths = container.querySelectorAll('th[data-sort]');
    for (var t = 0; t < ths.length; t++) {
      ths[t].addEventListener('click', handleSortClick);
    }

    // Bind row expand click
    var rows = container.querySelectorAll('.group-row');
    for (var r = 0; r < rows.length; r++) {
      rows[r].addEventListener('click', handleRowClick);
    }
  }

  function handleSortClick(e) {
    var col = e.currentTarget.getAttribute('data-sort');
    if (state.sortCol === col) {
      state.sortAsc = !state.sortAsc;
    } else {
      state.sortCol = col;
      state.sortAsc = true;
    }
    render();
  }

  function handleRowClick(e) {
    var sym = e.currentTarget.getAttribute('data-symbol');
    if (sym) {
      state.expandedGroups[sym] = !state.expandedGroups[sym];
      // Also set symbol filter on activity
      state.activitySymbolFilter = state.expandedGroups[sym] ? sym : null;
      render();
    }
  }

  // ── Activity Feed ──
  function renderActivityFeed() {
    var container = $('activityFeed');
    var filter = state.activityFilter;
    var symFilter = state.activitySymbolFilter;
    var entries = state.activityLog;
    var html = '';
    var count = 0;

    for (var i = 0; i < entries.length && count < 200; i++) {
      var entry = entries[i];
      if (filter !== 'all' && entry.cat !== filter) continue;
      if (symFilter && entry.symbol !== symFilter) continue;

      html += '<div class="activity-item">';
      html += '<span class="activity-time">' + fmtShortTime(entry.ts) + '</span>';
      html += '<span class="activity-cat ' + esc(entry.cat) + '">' + esc(entry.cat) + '</span>';
      html += '<span class="activity-msg">' + formatActivityMsg(entry.msg) + '</span>';
      html += '</div>';
      count++;
    }

    if (count === 0) {
      html = '<div class="empty-state">No activity' + (filter !== 'all' ? ' for ' + filter : '') + '</div>';
    }

    container.innerHTML = html;
  }

  function formatActivityMsg(msg) {
    if (!msg) return '';
    // Highlight symbols (2-10 uppercase letters)
    return esc(msg).replace(/\b([A-Z]{2,10})\b/g, function (match) {
      return '<span class="symbol-link" onclick="filterActivityBySymbol(\'' + match + '\')">' + match + '</span>';
    });
  }

  window.filterActivityBySymbol = function (sym) {
    state.activitySymbolFilter = (state.activitySymbolFilter === sym) ? null : sym;
    renderActivityFeed();
  };

  // ── Activity Filters ──
  function setupActivityFilters() {
    var btns = document.querySelectorAll('#activityFilters .filter-btn');
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', function (e) {
        var cat = e.currentTarget.getAttribute('data-cat');
        state.activityFilter = cat;
        state.activitySymbolFilter = null;
        // Update active class
        var all = document.querySelectorAll('#activityFilters .filter-btn');
        for (var j = 0; j < all.length; j++) {
          all[j].classList.toggle('active', all[j].getAttribute('data-cat') === cat);
        }
        renderActivityFeed();
      });
    }
  }

  // ── Tab Bar ──
  function setupTabs() {
    var tabs = document.querySelectorAll('#tabBar .tab');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener('click', function (e) {
        var tab = e.currentTarget.getAttribute('data-tab');
        state.activeTab = tab;
        var all = document.querySelectorAll('#tabBar .tab');
        for (var j = 0; j < all.length; j++) {
          all[j].classList.toggle('active', all[j].getAttribute('data-tab') === tab);
        }
        render();
      });
    }
  }

  // ── Secondary Tabs ──
  function renderSecondaryTab(snap) {
    var container = $('tabContent');
    var tab = state.activeTab;

    switch (tab) {
      case 'entry': renderEntryBoard(container, snap); break;
      case 'orders': renderOrders(container, snap); break;
      case 'fills': renderFills(container, snap); break;
      case 'funding': renderFunding(container, snap); break;
      case 'connectors': renderConnectors(container, snap); break;
      case 'config': renderConfig(container, snap); break;
      default: container.innerHTML = '<div class="empty-state">Unknown tab</div>';
    }
  }

  // ── Entry Board ──
  function renderEntryBoard(container, snap) {
    var entries = snap.entry_leaderboard || [];

    if (entries.length === 0) {
      container.innerHTML = '<div class="empty-state">No entry opportunities</div>';
      return;
    }

    var html = '<table class="entry-table"><thead><tr>';
    html += '<th>#</th><th>Symbol</th><th>Long</th><th>Short</th>';
    html += '<th class="text-right">bDPY</th><th class="text-right">Breakeven</th>';
    html += '<th class="text-right">Fees</th><th>Skip Reason</th>';
    html += '</tr></thead><tbody>';

    for (var i = 0; i < entries.length; i++) {
      var e = entries[i];
      var skipped = e.skip_reason != null && e.skip_reason !== '';
      html += '<tr' + (skipped ? ' style="opacity:0.5"' : '') + '>';
      html += '<td>' + esc(e.rank) + '</td>';
      html += '<td>' + esc(e.symbol) + '</td>';
      html += '<td>' + esc(exchangeShort(e.long_exchange)) + '</td>';
      html += '<td>' + esc(exchangeShort(e.short_exchange)) + '</td>';
      html += '<td class="text-right ' + dpyClass(e.best_dpy) + '">' + fmtDpy(e.best_dpy) + '</td>';
      html += '<td class="text-right">' + fmtHours(e.breakeven_hours) + '</td>';
      html += '<td class="text-right">' + fmtPct(e.fees, 4) + '</td>';
      html += '<td>' + (skipped ? '<span class="skip-reason">' + esc(e.skip_reason) + '</span>' : '--') + '</td>';
      html += '</tr>';
    }

    html += '</tbody></table>';
    container.innerHTML = html;
  }

  // ── Orders ──
  function renderOrders(container, snap) {
    var open = snap.orders || [];
    var recent = snap.recent_orders || [];
    var all = open.concat(recent);

    if (all.length === 0) {
      container.innerHTML = '<div class="empty-state">No orders</div>';
      return;
    }

    var html = '<table class="orders-table"><thead><tr>';
    html += '<th>Exchange</th><th>Symbol</th><th>Side</th><th>Type</th>';
    html += '<th class="text-right">Size</th><th class="text-right">Filled</th>';
    html += '<th class="text-right">Price</th><th>Status</th><th>Time</th>';
    html += '</tr></thead><tbody>';

    for (var i = 0; i < all.length; i++) {
      var o = all[i];
      var isRecent = i >= open.length;
      html += '<tr' + (isRecent ? ' style="opacity:0.5"' : '') + '>';
      html += '<td>' + esc(exchangeShort(o.exchange)) + '</td>';
      html += '<td>' + esc(o.symbol) + '</td>';
      html += '<td>' + esc(o.side) + '</td>';
      html += '<td>' + esc(o.order_type) + '</td>';
      html += '<td class="text-right">' + (o.size != null ? o.size.toFixed(4) : '--') + '</td>';
      html += '<td class="text-right">' + (o.filled_size != null ? o.filled_size.toFixed(4) : '--') + '</td>';
      html += '<td class="text-right">' + fmtUsd(o.price) + '</td>';
      html += '<td>' + esc(o.status) + '</td>';
      html += '<td>' + fmtTime(o.created_at) + '</td>';
      html += '</tr>';
    }

    html += '</tbody></table>';
    container.innerHTML = html;
  }

  // ── Fills (Trade History) ──
  function renderFills(container, snap) {
    var fills = snap.trade_history || [];

    if (fills.length === 0) {
      container.innerHTML = '<div class="empty-state">No trade history</div>';
      return;
    }

    var html = '<table class="fills-table"><thead><tr>';
    html += '<th>Time</th><th>Exchange</th><th>Symbol</th><th>Side</th>';
    html += '<th class="text-right">Size</th><th class="text-right">Price</th>';
    html += '<th class="text-right">Fee</th><th>Service</th>';
    html += '</tr></thead><tbody>';

    for (var i = 0; i < fills.length; i++) {
      var f = fills[i];
      html += '<tr>';
      html += '<td>' + fmtTime(f.timestamp) + '</td>';
      html += '<td>' + esc(exchangeShort(f.exchange)) + '</td>';
      html += '<td>' + esc(f.symbol) + '</td>';
      html += '<td>' + esc(f.side) + '</td>';
      html += '<td class="text-right">' + (f.size != null ? f.size.toFixed(4) : '--') + '</td>';
      html += '<td class="text-right">' + fmtUsd(f.price) + '</td>';
      html += '<td class="text-right">' + fmtUsd(f.fee) + '</td>';
      html += '<td>' + esc(f.service || '--') + '</td>';
      html += '</tr>';
    }

    html += '</tbody></table>';
    container.innerHTML = html;
  }

  // ── Funding Matrix ──
  function renderFunding(container, snap) {
    var rates = snap.funding_rates || [];

    if (rates.length === 0) {
      container.innerHTML = '<div class="empty-state">No funding rate data</div>';
      return;
    }

    // Build matrix: symbols as rows, exchanges as columns
    var exchanges = {};
    var symbols = {};
    var matrix = {}; // symbol -> exchange -> rate

    for (var i = 0; i < rates.length; i++) {
      var r = rates[i];
      var sym = r.symbol || 'UNKNOWN';
      var ex = r.exchange || 'Unknown';
      exchanges[ex] = true;
      symbols[sym] = true;
      if (!matrix[sym]) matrix[sym] = {};
      matrix[sym][ex] = r.funding_rate_hr;
    }

    var exList = Object.keys(exchanges).sort();
    var symList = Object.keys(symbols).sort();

    var html = '<table class="funding-matrix"><thead><tr>';
    html += '<th>Symbol</th>';
    for (var e = 0; e < exList.length; e++) {
      html += '<th>' + esc(exchangeShort(exList[e])) + '</th>';
    }
    html += '</tr></thead><tbody>';

    for (var s = 0; s < symList.length; s++) {
      html += '<tr>';
      html += '<td>' + esc(symList[s]) + '</td>';
      for (var ex2 = 0; ex2 < exList.length; ex2++) {
        var rate = matrix[symList[s]] && matrix[symList[s]][exList[ex2]];
        if (rate != null) {
          var cls = 'funding-cell';
          var abs = Math.abs(rate);
          if (rate > 0 && abs > 0.0005) cls += ' rate-hot';
          else if (rate > 0) cls += ' rate-positive';
          else if (rate < 0 && abs > 0.0005) cls += ' rate-cold';
          else if (rate < 0) cls += ' rate-negative';
          html += '<td><span class="' + cls + '">' + fmtPct(rate, 5) + '</span></td>';
        } else {
          html += '<td style="color:var(--muted)">--</td>';
        }
      }
      html += '</tr>';
    }

    html += '</tbody></table>';
    container.innerHTML = html;
  }

  // ── Connectors ──
  function renderConnectors(container, snap) {
    var connectors = snap.connectors || [];

    if (connectors.length === 0) {
      container.innerHTML = '<div class="empty-state">No connector data</div>';
      return;
    }

    var html = '<div class="connector-grid">';

    for (var i = 0; i < connectors.length; i++) {
      var c = connectors[i];
      var dotClass = 'disabled';
      if (c.enabled) {
        dotClass = 'healthy';
        if (c.close_only) dotClass = 'degraded';
      }
      if (c.disabled_reason) dotClass = 'unhealthy';

      html += '<div class="connector-card">';
      html += '<div class="connector-card-header">';
      html += '<span class="connector-dot ' + dotClass + '"></span>';
      html += '<span class="connector-name">' + esc(c.exchange) + '</span>';
      html += '</div>';
      html += '<div class="connector-detail">';
      html += 'Enabled: ' + (c.enabled ? 'Yes' : 'No') + '<br>';
      if (c.close_only) html += 'Close-only: ' + esc(c.close_only_reason || 'yes') + '<br>';
      if (c.disabled_reason) html += 'Disabled: ' + esc(c.disabled_reason) + '<br>';
      if (c.disabled_at) html += 'Since: ' + fmtTime(c.disabled_at) + '<br>';
      html += '</div></div>';
    }

    html += '</div>';
    container.innerHTML = html;
  }

  // ── Config ──
  function renderConfig(container, snap) {
    var config = snap.config || {};

    var keys = [
      { key: 'dry_run', label: 'Dry Run', type: 'bool' },
      { key: 'kill_switch', label: 'Kill Switch', type: 'bool' },
      { key: 'mdpy', label: 'Min DPY (mDPY)', type: 'number' },
      { key: 'entry_bdpy', label: 'Entry bDPY', type: 'number' },
      { key: 'entry_breakeven_hours', label: 'Entry Breakeven Hours', type: 'number' },
      { key: 'unit_usd', label: 'Execution Size USD', type: 'number' },
      { key: 'min_leg_size_usd', label: 'Min Leg Size USD', type: 'number' },
      { key: 'max_usd', label: 'Max Leg Size USD', type: 'number' },
      { key: 'max_market_slippage_bps', label: 'Max Slippage BPS', type: 'number' },
      { key: 'connector_disable_on_geoblock', label: 'Disable on Geoblock', type: 'bool' },
    ];

    var html = '<table class="config-table"><thead><tr>';
    html += '<th>Setting</th><th>Current Value</th><th>New Value</th><th></th>';
    html += '</tr></thead><tbody>';

    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      var currentVal = config[k.key];
      var displayVal = currentVal != null ? String(currentVal) : '--';

      html += '<tr>';
      html += '<td>' + esc(k.label) + '</td>';
      html += '<td style="color:var(--muted)">' + esc(displayVal) + '</td>';
      html += '<td>';

      // Map config key names to env var names for the PUT API
      var envKey = configKeyToEnvKey(k.key);

      if (k.type === 'bool') {
        var checked = state.configEdits[k.key] != null ? state.configEdits[k.key] === 'true' : currentVal;
        html += '<select class="config-input" data-config-key="' + esc(k.key) + '" data-env-key="' + esc(envKey) + '">';
        html += '<option value="true"' + (checked ? ' selected' : '') + '>true</option>';
        html += '<option value="false"' + (!checked ? ' selected' : '') + '>false</option>';
        html += '</select>';
      } else {
        var editVal = state.configEdits[k.key] != null ? state.configEdits[k.key] : (currentVal != null ? String(currentVal) : '');
        html += '<input class="config-input" type="text" data-config-key="' + esc(k.key) + '" data-env-key="' + esc(envKey) + '" value="' + esc(editVal) + '" />';
      }

      html += '</td>';
      html += '<td><button class="config-save-btn" data-config-key="' + esc(k.key) + '" data-env-key="' + esc(envKey) + '">Save</button></td>';
      html += '</tr>';
    }

    html += '</tbody></table>';
    container.innerHTML = html;

    // Bind input changes
    var inputs = container.querySelectorAll('.config-input');
    for (var j = 0; j < inputs.length; j++) {
      inputs[j].addEventListener('change', function (e) {
        var key = e.target.getAttribute('data-config-key');
        state.configEdits[key] = e.target.value;
      });
      inputs[j].addEventListener('input', function (e) {
        var key = e.target.getAttribute('data-config-key');
        state.configEdits[key] = e.target.value;
      });
    }

    // Bind save buttons
    var saveBtns = container.querySelectorAll('.config-save-btn');
    for (var b = 0; b < saveBtns.length; b++) {
      saveBtns[b].addEventListener('click', function (e) {
        var configKey = e.currentTarget.getAttribute('data-config-key');
        var envKey = e.currentTarget.getAttribute('data-env-key');
        saveConfigValue(configKey, envKey);
      });
    }
  }

  function configKeyToEnvKey(key) {
    // Map config snapshot field names to the env var names the PUT API expects
    var map = {
      'dry_run': 'BOT_DRY_RUN',
      'kill_switch': 'BOT_KILL_SWITCH',
      'mdpy': 'ENTRY_BDPY',
      'entry_bdpy': 'ENTRY_BDPY',
      'entry_breakeven_hours': 'ENTRY_BREAKEVEN_HOURS',
      'unit_usd': 'MIN_LEG_SIZE',
      'min_leg_size_usd': 'MIN_LEG_SIZE',
      'max_usd': 'MAX_LEG_SIZE',
      'max_market_slippage_bps': 'BOT_MAX_MARKET_SLIPPAGE_BPS',
      'connector_disable_on_geoblock': 'BOT_CONNECTOR_DISABLE_ON_GEOBLOCK',
    };
    return map[key] || key.toUpperCase();
  }

  function saveConfigValue(configKey, envKey) {
    var input = document.querySelector('.config-input[data-config-key="' + configKey + '"]');
    if (!input) return;
    var val = input.value;

    var body = { values: {} };
    body.values[envKey] = val;

    fetch('/api/v1/config/effective', {
      method: 'PUT',
      headers: tokenHeaders(),
      body: JSON.stringify(body),
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data.ok) {
          addActivityEntry('recon', 'Config saved: ' + envKey + ' = ' + val);
          if (data.payload && data.payload.restart_required && data.payload.restart_required.length > 0) {
            addActivityEntry('health', 'Restart required for: ' + data.payload.restart_required.join(', '));
          }
          delete state.configEdits[configKey];
        } else {
          alert('Config save failed: ' + (data.error || 'unknown'));
        }
      })
      .catch(function (e) {
        alert('Config save failed: ' + e.message);
      });
  }

  // ── Load Config Schema (for future use) ──
  function loadConfigSchema() {
    fetch('/api/v1/config/schema', { headers: tokenHeaders() })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data.ok) {
          state.configSchema = data.payload;
        }
      })
      .catch(function () { /* ignore */ });
  }

  // ── Init ──
  function init() {
    ensureAuth();
    setupTabs();
    setupActivityFilters();
    loadConfigSchema();

    // If token already exists, connect immediately
    if (localStorage.getItem('rhgbot_token') != null) {
      connectWs();
    }
  }

  // Wait for DOM
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
