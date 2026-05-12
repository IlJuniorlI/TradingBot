// Minimal mobile dashboard renderer.
//
// Shares DOM IDs with dashboard.html, but only renders the four sections
// that exist on the mobile page: topbar, KPI card, Risk / Utilization
// card, and Open Positions card. The state payload is fetched from the
// same /api/state endpoint that the desktop dashboard uses.
(function () {
  'use strict';

  // Mobile defaults to a slower cadence than desktop. The desktop value
  // (typically 2s) is intended for an always-on dashboard window where
  // freshness matters more than battery; on a phone in a pocket or
  // briefly checked on the go, the cellular radio cycling every 2s is a
  // real battery cost. The mobile floor is 4s. The server-provided
  // refreshMs only takes effect when it's already SLOWER than the mobile
  // floor — a server value below 4000 will be raised to 4000. Set
  // dashboard.refresh_ms higher in config to throttle mobile further.
  const DEFAULT_REFRESH_MS = 2000;
  const MOBILE_REFRESH_MS = 4000;
  const cfg = (window.DASHBOARD_CONFIG || {});
  const serverRefreshMs = Number(cfg.refreshMs) || DEFAULT_REFRESH_MS;
  const refreshMs = Math.max(serverRefreshMs, MOBILE_REFRESH_MS);

  // Shared utilities (numOrNull, clamp, escapeHtml, fmt*, pnlClass,
  // sparklineSVG, fmtSide, statusBadge, modeBadge) live in helpers.js —
  // loaded before this script in mobile.html and referenced as globals.
  //
  // mobile-only `safe` returns '' for null/undefined (dashboard.js's
  // returns '—'); divergent on purpose so empty fallbacks like
  // `safe(x) || 'fallback'` still trigger when x is missing.
  function safe(value) {
    if (value === null || value === undefined) return '';
    return String(value);
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
    const kpiWinrate = document.getElementById('kpi-winrate');
    if (kpiWinrate) kpiWinrate.textContent = perf.win_rate == null ? '—' : fmtPctFromRatio(perf.win_rate);
    if (kpiMeta) kpiMeta.textContent = `Day PnL ${fmtMoney(dayPnl)} · cash ${fmtMoney(perf.cash)}`;

    // Day change pill: percent change from starting equity, color-coded.
    const chgEl = document.getElementById('kpi-day-chg');
    const chgText = document.getElementById('kpi-day-chg-text');
    if (chgEl && chgText) {
      const dayPct = starting > 0 ? (dayPnl / starting) * 100 : 0;
      const tone = dayPnl > 0 ? 'good' : (dayPnl < 0 ? 'bad' : 'neutral');
      const arrow = dayPnl > 0 ? '▲' : (dayPnl < 0 ? '▼' : '—');
      chgEl.className = `kpi-hero-chg ${tone}`;
      const arrowEl = chgEl.querySelector('.arrow');
      if (arrowEl) arrowEl.textContent = arrow;
      chgText.textContent = starting > 0 ? `${fmtPct(dayPct, 2)} today` : '— today';
    }

    // Equity curve sparkline: green/red based on net direction; empty wrapper
    // (CSS :empty rule) collapses the strip until we have ≥2 points.
    const kpiSpark = document.getElementById('kpi-spark');
    if (kpiSpark) {
      const equityValues = (Array.isArray(perf.equity_curve) ? perf.equity_curve : [])
        .map(point => numOrNull(point?.equity))
        .filter(v => v !== null);
      if (equityValues.length < 2) {
        kpiSpark.innerHTML = '';
      } else {
        const first = equityValues[0];
        const last = equityValues[equityValues.length - 1];
        const tone = last > first ? 'tone-good' : (last < first ? 'tone-bad' : 'tone-neutral');
        kpiSpark.innerHTML = sparklineSVG(equityValues, tone);
      }
    }

    const exposureRing = document.getElementById('gauge-exposure-ring');
    const winRing = document.getElementById('gauge-win-ring');
    const ddRing = document.getElementById('gauge-dd-ring');
    const exposureText = document.getElementById('gauge-exposure-text');
    const winText = document.getElementById('gauge-win-text');
    const ddText = document.getElementById('gauge-dd-text');

    if (exposureRing) exposureRing.style.setProperty('--pct', `${exposurePct}%`);
    if (winRing) winRing.style.setProperty('--pct', `${riskPct}%`);
    if (ddRing) ddRing.style.setProperty('--pct', `${ddPct}%`);
    if (exposureText) exposureText.textContent = fmtPctSmart(exposurePct);
    if (winText) winText.textContent = fmtPctSmart(riskPct);
    if (ddText) ddText.textContent = fmtPctSmart(ddPct);

    // Dollar values shown under each gauge ring. Tight currency format
    // (no decimals) so they fit in the narrow gauge-card column.
    const fmtGauge = (v) => {
      const n = numOrNull(v);
      if (n === null) return '—';
      return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(n);
    };
    const expValue = document.getElementById('gauge-exposure-value');
    const winValue = document.getElementById('gauge-win-value');
    const ddValueEl = document.getElementById('gauge-dd-value');
    if (expValue) expValue.textContent = fmtGauge(grossExposure);
    if (winValue) winValue.textContent = fmtGauge(grossMaxRisk);
    if (ddValueEl) ddValueEl.textContent = fmtGauge(ddValue);
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
            <div class="position-sub">${escapeHtml(fmtSide(pos))} · ${escapeHtml(pos.asset_type || '—')} · Qty ${fmtInteger(pos.qty)}</div>
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
    // Browsers throttle setInterval to ~1Hz when the tab is hidden;
    // on phones this means re-opening the app shows stale data for
    // up to one full refreshMs cycle. Force an immediate fetch on
    // visibility return. The fetch path is idempotent so this is
    // safe to fire alongside an in-flight interval tick.
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) fetchState();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
