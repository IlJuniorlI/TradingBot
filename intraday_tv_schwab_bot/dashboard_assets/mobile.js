// Minimal mobile dashboard renderer.
//
// Shares DOM IDs with dashboard.html, but only renders the four sections
// that exist on the mobile page: topbar, KPI card, Risk / Utilization
// card, and Open Positions card. The state payload is fetched from the
// same /api/state endpoint that the desktop dashboard uses.
(function () {
  'use strict';

  const DEFAULT_REFRESH_MS = 2000;
  const MIN_REFRESH_MS = 500;
  const cfg = (window.DASHBOARD_CONFIG || {});
  const refreshMs = Math.max(MIN_REFRESH_MS, Number(cfg.refreshMs) || DEFAULT_REFRESH_MS);

  // ---- formatting helpers (ports of the ones in dashboard.js) ----

  function numOrNull(value) {
    if (value === null || value === undefined || value === '') return null;
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  function safe(value) {
    if (value === null || value === undefined) return '';
    return String(value);
  }

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function fmtMoney(value) {
    const num = numOrNull(value);
    if (num === null) return '—';
    const sign = num < 0 ? '-' : '';
    const abs = Math.abs(num);
    return sign + '$' + abs.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function fmtNum(value, digits) {
    const num = numOrNull(value);
    if (num === null) return '—';
    return num.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function fmtInteger(value) {
    const num = numOrNull(value);
    if (num === null) return '—';
    return Math.round(num).toLocaleString('en-US');
  }

  function fmtPctSmart(value) {
    const num = numOrNull(value);
    if (num === null) return '—';
    return num.toFixed(num >= 10 ? 0 : 1) + '%';
  }

  function fmtPctFromRatio(value) {
    const num = numOrNull(value);
    if (num === null) return '—';
    return (num * 100).toFixed(1) + '%';
  }

  function fmtPct(value, digits = 2) {
    const num = numOrNull(value);
    if (num === null) return '—';
    return num.toFixed(digits) + '%';
  }

  function fmtSide(entity) {
    const side = String(entity?.side || '').trim();
    if (!side) return '—';
    const opt = String(entity?.option_type || '').trim().toUpperCase();
    return opt ? `${side} · ${opt}` : side;
  }

  function pnlClass(value) {
    const num = numOrNull(value);
    if (num === null || num === 0) return 'pnl-flat';
    return num > 0 ? 'pnl-pos' : 'pnl-neg';
  }

  // NOTE: these two helpers are ports of dashboard.js's versions. Keep the
  // class names in sync with dashboard.css — .mode-chip / status-starting
  // are NOT real classes there, so don't invent them here.
  function statusBadge(status) {
    const value = String(status || '').toLowerCase();
    const tone = value === 'running'
      ? 'status-running'
      : (value === 'error' || value === 'stopped' || value === 'disconnected'
        ? 'status-error'
        : (value === 'starting' ? 'status-warning' : 'status-idle'));
    return `<span class="status-chip ${tone}">${escapeHtml(value || 'idle')}</span>`;
  }

  function modeBadge(isDryRun) {
    const label = isDryRun ? 'DRY RUN / PAPER' : 'LIVE ORDERS';
    const tone = isDryRun ? 'paper' : 'live';
    return `<span class="mode-pill inline-mode ${tone}">${escapeHtml(label)}</span>`;
  }

  // ---- renderers (mirror dashboard.js but only for mobile DOM) ----

  function renderTopbar(data) {
    const perf = data?.performance || {};
    const rawStatus = String(data?.status || 'idle').toLowerCase();
    const botActive = !!(data?.management_active || data?.screening_active || data?.streaming_active || data?.position_monitoring_active);
    const status = rawStatus === 'running' && !botActive ? 'idle' : rawStatus;

    const statusWrap = document.getElementById('status-badge-wrap');
    const dayPnlEl = document.getElementById('metric-daypnl');
    const pfEl = document.getElementById('metric-pf');
    const tradesEl = document.getElementById('metric-trades');
    const wlEl = document.getElementById('metric-wl');
    const winRateEl = document.getElementById('metric-winrate');
    const warmupEl = document.getElementById('metric-warmup');
    const updatedEl = document.getElementById('metric-updated');
    const sublineEl = document.getElementById('top-subline');
    const netLiqLabelEl = document.getElementById('kpi-netliq-label');

    if (statusWrap) statusWrap.innerHTML = `${statusBadge(status)}${modeBadge(!!data?.dry_run)}`;
    if (netLiqLabelEl) netLiqLabelEl.textContent = safe(data?.tracked_capital_label) || (data?.dry_run ? 'Net Liq' : 'Allocated Capital');

    const dayPnl = (perf.total_equity || 0) - (perf.starting_equity || 0);
    if (dayPnlEl) dayPnlEl.innerHTML = `<span class="${pnlClass(dayPnl)}">${fmtMoney(dayPnl)}</span>`;
    if (pfEl) pfEl.textContent = fmtNum(perf.profit_factor, 2);

    const openPositions = Array.isArray(perf.positions) ? perf.positions.length : (Number(perf.open_positions) || 0);
    const totalTrades = Number(perf.total_trades);
    if (tradesEl) tradesEl.textContent = fmtInteger(Number.isFinite(totalTrades) ? totalTrades : ((Number(perf.closed_trades) || 0) + openPositions));
    if (wlEl) wlEl.textContent = `${fmtInteger(perf.wins ?? 0)} / ${fmtInteger(perf.losses ?? 0)}`;
    if (winRateEl) winRateEl.textContent = perf.win_rate == null ? '—' : fmtPctFromRatio(perf.win_rate);

    const warmup = data?.warmup || {};
    if (warmupEl) warmupEl.textContent = warmup.total ? `${fmtInteger(warmup.ready_count || 0)} / ${fmtInteger(warmup.total || 0)}` : '—';
    if (updatedEl) updatedEl.textContent = safe(data?.last_update).replace('T', ' ').slice(0, 19);

    if (sublineEl) {
      const blocked = Number(warmup.blocked_count || 0);
      const warmupText = warmup.total
        ? `ready ${fmtInteger(warmup.ready_count || 0)}/${fmtInteger(warmup.total || 0)}${blocked ? ` · loading ${fmtInteger(blocked)}` : ''}`
        : 'ready —';
      sublineEl.textContent = `${safe(data?.strategy)} · ${safe(data?.message)} · ${warmupText} · watchlist ${safe(data?.active_watchlist?.length || 0)}`;
    }
  }

  function renderKpiAndGauges(data) {
    const perf = data?.performance || {};
    const positions = Array.isArray(perf.positions) ? perf.positions : [];
    const totalEquity = numOrNull(perf.total_equity) || 0;
    const starting = numOrNull(perf.starting_equity) || 0;
    const grossExposure = numOrNull(perf.gross_market_value) ??
      positions.reduce((acc, pos) => acc + Math.abs(numOrNull(pos?.market_value) || 0), 0);
    const grossMaxRisk = numOrNull(perf.gross_max_risk) ??
      positions.reduce((acc, pos) => acc + Math.abs(numOrNull(pos?.max_risk) || 0), 0);
    const exposurePct = totalEquity > 0 ? clamp((grossExposure / totalEquity) * 100, 0, 100) : 0;
    const riskPct = totalEquity > 0 ? clamp((grossMaxRisk / totalEquity) * 100, 0, 100) : 0;
    const ddValue = numOrNull(perf.max_drawdown) || numOrNull(perf.drawdown) || 0;
    const ddPct = totalEquity > 0 ? clamp((ddValue / totalEquity) * 100, 0, 100) : 0;
    const dayPnl = totalEquity - starting;

    const kpiNet = document.getElementById('kpi-netliq');
    const kpiReal = document.getElementById('kpi-realized');
    const kpiUnreal = document.getElementById('kpi-unrealized');
    const kpiDraw = document.getElementById('kpi-drawdown');
    const kpiMeta = document.getElementById('kpi-meta');

    if (kpiNet) kpiNet.textContent = fmtMoney(totalEquity);
    if (kpiReal) kpiReal.innerHTML = `<span class="${pnlClass(perf.realized_pnl)}">${fmtMoney(perf.realized_pnl)}</span>`;
    if (kpiUnreal) kpiUnreal.innerHTML = `<span class="${pnlClass(perf.unrealized_pnl)}">${fmtMoney(perf.unrealized_pnl)}</span>`;
    if (kpiDraw) kpiDraw.textContent = fmtMoney(perf.drawdown);
    if (kpiMeta) kpiMeta.textContent = `Day PnL ${fmtMoney(dayPnl)} · cash ${fmtMoney(perf.cash)}`;

    const exposureRing = document.getElementById('gauge-exposure-ring');
    const winRing = document.getElementById('gauge-win-ring');
    const ddRing = document.getElementById('gauge-dd-ring');
    const exposureFill = document.getElementById('gauge-exposure-fill');
    const winFill = document.getElementById('gauge-win-fill');
    const ddFill = document.getElementById('gauge-dd-fill');
    const exposureText = document.getElementById('gauge-exposure-text');
    const winText = document.getElementById('gauge-win-text');
    const ddText = document.getElementById('gauge-dd-text');

    if (exposureRing) exposureRing.style.setProperty('--pct', `${exposurePct}%`);
    if (winRing) winRing.style.setProperty('--pct', `${riskPct}%`);
    if (ddRing) ddRing.style.setProperty('--pct', `${ddPct}%`);
    if (exposureFill) exposureFill.style.width = `${exposurePct}%`;
    if (winFill) winFill.style.width = `${riskPct}%`;
    if (ddFill) ddFill.style.width = `${ddPct}%`;
    if (exposureText) exposureText.textContent = fmtPctSmart(exposurePct);
    if (winText) winText.textContent = fmtPctSmart(riskPct);
    if (ddText) ddText.textContent = fmtPctSmart(ddPct);
  }

  function positionRangeMarkup(pos) {
    // Bar shows distance traveled from stop toward target.
    //   progress = 0   -> price is at stop  (about to exit at loss)
    //   progress = 1   -> price is at target (about to exit at target)
    // Side-aware so LONG (stop<target) and SHORT (target<stop) both
    // render with the same "bigger fill = more progress toward profit"
    // semantic. Fill is tinted red when the position is currently in
    // loss to match the desktop `.bar-fill.bad` convention.
    const stop = numOrNull(pos?.stop_price);
    const target = numOrNull(pos?.target_price);
    const last = numOrNull(pos?.last_price);
    const side = String(pos?.side || '').toUpperCase();
    if (stop === null || target === null || last === null) return '';

    let span, progress;
    if (side === 'SHORT') {
      span = stop - target;
      if (span <= 0) return '';
      progress = clamp((stop - last) / span, 0, 1);
    } else {
      span = target - stop;
      if (span <= 0) return '';
      progress = clamp((last - stop) / span, 0, 1);
    }
    const pct = progress * 100;
    const fillTone = Number(pos?.return_pct) < 0 ? 'bad' : '';
    return `<div class="bar-track" style="margin-top:10px;"><div class="bar-fill ${fillTone}" style="width:${pct.toFixed(1)}%;"></div></div>`;
  }

  function renderPositions(data) {
    const perf = data?.performance || {};
    const positions = perf.positions || [];
    const metaEl = document.getElementById('positions-meta');
    const target = document.getElementById('positions-cards');
    if (metaEl) metaEl.textContent = `${positions.length} open positions`;
    if (!target) return;
    if (!positions.length) {
      target.innerHTML = `<div class="empty-state">No open positions.</div>`;
      return;
    }
    target.innerHTML = positions.map(pos => {
      const baseSymbol = String(pos.underlying || pos.symbol || '').toUpperCase();
      const tone = String(pos.side || '').toUpperCase() === 'SHORT' ? 'tone-short' : 'tone-long';
      const rr = numOrNull(pos.max_reward) && numOrNull(pos.max_risk)
        ? (Number(pos.max_reward) / Math.max(Number(pos.max_risk), 0.0001))
        : null;
      const rangeMarkup = positionRangeMarkup(pos);
      return `<div class="position-card ${tone}" data-symbol="${escapeHtml(baseSymbol)}">
        <div class="position-top">
          <div>
            <div class="position-name">${escapeHtml(baseSymbol)}</div>
            <div class="position-sub">${escapeHtml(fmtSide(pos))} · ${escapeHtml(pos.asset_type || '—')} · Qty ${escapeHtml(pos.qty)}</div>
          </div>
          <div class="price-stack">
            <div class="price-main ${pnlClass(pos.unrealized_pnl)}">${fmtMoney(pos.unrealized_pnl)}</div>
            <div class="price-change ${pnlClass(pos.return_pct)}">${fmtPct(pos.return_pct)}</div>
          </div>
        </div>
        <div class="position-stats">
          <div class="tiny-kv"><div class="tiny-label">Entry</div><div class="v">${fmtNum(pos.entry_price, 2)}</div></div>
          <div class="tiny-kv"><div class="tiny-label">Last</div><div class="v">${fmtNum(pos.last_price, 2)}</div></div>
          <div class="tiny-kv"><div class="tiny-label">R/R</div><div class="v">${fmtNum(rr, 2)}</div></div>
        </div>
        ${rangeMarkup}
      </div>`;
    }).join('');
  }

  function render(data) {
    try { renderTopbar(data); } catch (err) { console.error('renderTopbar failed', err); }
    try { renderKpiAndGauges(data); } catch (err) { console.error('renderKpiAndGauges failed', err); }
    try { renderPositions(data); } catch (err) { console.error('renderPositions failed', err); }
  }

  function renderDisconnectedBadge(message) {
    const statusWrap = document.getElementById('status-badge-wrap');
    if (!statusWrap) return;
    // statusBadge() already routes 'disconnected' to .status-error styling.
    statusWrap.innerHTML = `<span title="${escapeHtml(message)}">${statusBadge('disconnected')}</span>`;
  }

  async function fetchState() {
    // Abort the fetch after 5× refresh interval so a stuck backend surfaces
    // as a visible disconnect instead of silently showing stale data.
    const abortCtl = new AbortController();
    const timeoutMs = Math.max(4000, refreshMs * 5);
    const timeoutId = setTimeout(() => abortCtl.abort(), timeoutMs);
    try {
      const resp = await fetch('/api/state', { cache: 'no-store', credentials: 'same-origin', signal: abortCtl.signal });
      if (!resp.ok) {
        console.warn('mobile dashboard: /api/state returned', resp.status);
        renderDisconnectedBadge(`HTTP ${resp.status}`);
        return;
      }
      const data = await resp.json();
      render(data);
    } catch (err) {
      const isAbort = err && (err.name === 'AbortError' || String(err).includes('abort'));
      console.error('mobile dashboard fetch failed', err);
      renderDisconnectedBadge(isAbort ? `Fetch timed out after ${Math.round(timeoutMs / 1000)}s` : String(err));
    } finally {
      clearTimeout(timeoutId);
    }
  }

  function start() {
    fetchState();
    setInterval(fetchState, refreshMs);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
