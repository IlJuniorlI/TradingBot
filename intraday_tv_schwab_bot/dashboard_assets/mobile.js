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

  // The ETag of the state on screen, sent as If-None-Match: while it is the
  // current state the server answers 304 with no body, and nothing on this
  // page moves with the clock, so there is nothing to redraw (2026-10-05).
  let stateEtag = null;

  // ---- renderers (mirror dashboard.js but only for mobile DOM) ----

  // The top bar's PF, Trades, W / L and Day PnL are the day's
  // (``performance.day``), each label tagged with the day on another day;
  // the KPI card's Realized, Drawdown and Win Rate are the run's (as the
  // desktop's).
  function renderTopbar(data) {
    const perf = data?.performance || {};
    const day = perf.day || {};
    const rawStatus = String(data?.status || 'idle').toLowerCase();
    const botActive = !!(data?.management_active || data?.screening_active || data?.streaming_active || data?.position_monitoring_active);
    const status = rawStatus === 'running' && !botActive ? 'idle' : rawStatus;

    const statusWrap = document.getElementById('status-badge-wrap');
    const dayPnlEl = document.getElementById('metric-daypnl');
    const pfEl = document.getElementById('metric-pf');
    const tradesEl = document.getElementById('metric-trades');
    const wlEl = document.getElementById('metric-wl');
    const warmupEl = document.getElementById('metric-warmup');
    const apiCpmEl = document.getElementById('metric-api-cpm');
    const updatedEl = document.getElementById('metric-updated');
    const sublineEl = document.getElementById('top-subline');
    const netLiqLabelEl = document.getElementById('kpi-netliq-label');

    if (statusWrap) statusWrap.innerHTML = `${statusBadge(status)}${modeBadge(!!data?.dry_run)}`;
    if (netLiqLabelEl) netLiqLabelEl.textContent = safe(data?.tracked_capital_label) || (data?.dry_run ? 'Net Liq' : 'Allocated Capital');

    applyDayTags(day);
    if (dayPnlEl) dayPnlEl.innerHTML = `<span class="${pnlClass(day.pnl)}">${fmtMoney(day.pnl)}</span>`;
    if (pfEl) pfEl.textContent = fmtNum(day.profit_factor, 2);
    if (tradesEl) tradesEl.textContent = fmtInteger(day.total_trades);
    if (wlEl) wlEl.textContent = `${fmtInteger(day.wins)} / ${fmtInteger(day.losses)}`;

    const warmup = data?.warmup || {};
    if (warmupEl) warmupEl.textContent = warmup.total ? `${fmtInteger(warmup.ready_count || 0)} / ${fmtInteger(warmup.total || 0)}` : '—';

    // Schwabdev API rate-limit telemetry (mirrors desktop's metric-api-cpm).
    // ``calls_per_minute_5m`` is the calls a minute over the last 5 minutes
    // (over the uptime while that is shorter); the rate-limit
    // ceiling is ~120/min, so a single number here is enough on mobile.
    if (apiCpmEl) apiCpmEl.textContent = fmtNum(data?.api_usage?.calls_per_minute_5m, 1);

    // Full date+time on the Updated footer pill (it spans the full
     // topbar row now, plenty of room for "2026-05-13 14:32:15").
    if (updatedEl) updatedEl.textContent = safe(data?.last_update).replace('T', ' ').slice(0, 19);

    if (sublineEl) {
      // Mobile subline is trimmed vs desktop. Two omissions:
      //   * Warmup ``ready X/Y · loading Z`` segment dropped — that data
      //     is already in the dedicated READY pill above; repeating it
      //     here was noise.
      //   * ``streaming N symbols`` in ``data.message`` abbreviated to
      //     ``N streams`` so the line fits a phone width without wrap.
      // Strategy + abbreviated message + watchlist count is enough
      // context for the ambient subline.
      const abbreviatedMessage = safe(data?.message).replace(
        /streaming\s+(\d+)\s+symbols?/i, '$1 streams',
      );
      sublineEl.textContent = `${safe(data?.strategy)} · ${abbreviatedMessage} · watchlist ${safe(data?.active_watchlist?.length || 0)}`;
    }
  }

  function renderKpiAndGauges(data) {
    const perf = data?.performance || {};
    const day = perf.day || {};
    const positions = Array.isArray(perf.positions) ? perf.positions : [];
    const totalEquity = numOrNull(perf.total_equity) || 0;
    const grossExposure = numOrNull(perf.gross_market_value) ??
      positions.reduce((acc, pos) => acc + Math.abs(numOrNull(pos?.market_value) || 0), 0);
    const grossMaxRisk = numOrNull(perf.gross_max_risk) ??
      positions.reduce((acc, pos) => acc + Math.abs(numOrNull(pos?.max_risk) || 0), 0);
    // ``Pct`` (clamped 0-100) drives the ring's visual fill; ``PctTrue``
    // (uncapped) drives the text readout so over-leverage (e.g. 128% on
    // a long+short portfolio) is honestly visible. Warn tone fires when
    // ratio > 100% — same pattern as the desktop gauge.
    const exposurePctTrue = totalEquity > 0 ? (grossExposure / totalEquity) * 100 : 0;
    const riskPctTrue = totalEquity > 0 ? (grossMaxRisk / totalEquity) * 100 : 0;
    // The run's max drawdown in % of the run's peak equity (the payload's).
    const ddPctTrue = numOrNull(perf.max_drawdown_pct);
    const exposurePct = clamp(exposurePctTrue, 0, 100);
    const riskPct = clamp(riskPctTrue, 0, 100);
    const ddPct = clamp(ddPctTrue ?? 0, 0, 100);

    const kpiNet = document.getElementById('kpi-netliq');
    const kpiReal = document.getElementById('kpi-realized');
    const kpiUnreal = document.getElementById('kpi-unrealized');
    const kpiDraw = document.getElementById('kpi-drawdown');
    const kpiMeta = document.getElementById('kpi-meta');

    if (kpiNet) kpiNet.textContent = fmtMoney(totalEquity);
    if (kpiReal) kpiReal.innerHTML = `<span class="${pnlClass(perf.realized_pnl)}">${fmtMoney(perf.realized_pnl)}</span>`;
    if (kpiUnreal) kpiUnreal.innerHTML = `<span class="${pnlClass(perf.unrealized_pnl)}">${fmtMoney(perf.unrealized_pnl)}</span>`;
    if (kpiDraw) {
      const drawdownTone = numOrNull(perf.drawdown) ? 'warn' : '';
      kpiDraw.innerHTML = `<span class="${drawdownTone}">${fmtMoney(perf.drawdown)}</span>`;
    }
    const kpiWinrate = document.getElementById('kpi-winrate');
    if (kpiWinrate) kpiWinrate.textContent = perf.win_rate == null ? '—' : fmtPctFromRatio(perf.win_rate);
    if (kpiMeta) kpiMeta.textContent = kpiMetaText(day, perf.cash);

    // Day change pill: the day's P&L in % of the day's opening equity,
    // color-coded, labelled with the day.
    const chgEl = document.getElementById('kpi-day-chg');
    const chgText = document.getElementById('kpi-day-chg-text');
    if (chgEl && chgText) {
      const dayPnl = numOrNull(day.pnl) ?? 0;
      const tone = dayPnl > 0 ? 'good' : (dayPnl < 0 ? 'bad' : 'neutral');
      const arrow = dayPnl > 0 ? '▲' : (dayPnl < 0 ? '▼' : '—');
      chgEl.className = `kpi-hero-chg ${tone}`;
      const arrowEl = chgEl.querySelector('.arrow');
      if (arrowEl) arrowEl.textContent = arrow;
      chgText.textContent = dayChangeText(day);
    }

    // The day's equity curve (a point a minute from the day's opening; on a
    // weekend or a holiday the last trading day's): green/red based on net
    // direction; empty wrapper (CSS :empty rule) collapses the strip until
    // we have ≥2 points.
    const kpiSpark = document.getElementById('kpi-spark');
    if (kpiSpark) {
      const equityValues = (Array.isArray(day.curve) ? day.curve : [])
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

    if (exposureRing) {
      exposureRing.style.setProperty('--pct', `${exposurePct}%`);
      // Warn tone when over 100% — over-leverage signal.
      exposureRing.className = `gauge-ring${exposurePctTrue > 100 ? ' warn' : ''}`;
    }
    if (winRing) winRing.style.setProperty('--pct', `${riskPct}%`);
    if (ddRing) ddRing.style.setProperty('--pct', `${ddPct}%`);
    // Text readouts use the TRUE (unclamped) values so readings >100%
    // surface honestly. Ring visual stays capped at 100% fill.
    if (exposureText) exposureText.textContent = fmtPctSmart(exposurePctTrue);
    if (winText) winText.textContent = fmtPctSmart(riskPctTrue);
    if (ddText) ddText.textContent = fmtPctSmart(ddPctTrue);

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
    if (ddValueEl) ddValueEl.textContent = fmtGauge(perf.max_drawdown);
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

  function renderCandidates(data) {
    // Compact mobile candidates list: one row per ranked candidate. Drops
    // the desktop's sparkline + score-ring + structure-event chip — those
    // belong on a workstation viewport, not a phone. Keeps the essentials:
    // symbol, directional bias, day%, last price, and the activity score
    // (0-100 scale, accent color). Live-first display matches the desktop
    // renderer: prefer streamed Schwab quote over the 60s-stale screener
    // values.
    const rows = data?.candidates || [];
    const meta = document.getElementById('m-candidates-meta');
    const target = document.getElementById('m-candidate-list');
    // ``dashboard_symbols`` is an array of per-symbol snapshot objects
    // (same payload the desktop uses via ``buildSnapshotMap`` at
    // dashboard.js:348). Index by symbol for O(1) lookup below.
    const snapshotsByKey = new Map();
    (data?.dashboard_symbols || []).forEach(item => {
      if (item && item.symbol) snapshotsByKey.set(String(item.symbol).toUpperCase(), item);
    });
    if (meta) meta.textContent = rows.length ? `${rows.length} ranked` : 'No candidates';
    if (!target) return;
    if (!rows.length) {
      target.innerHTML = `<div class="empty-state">No candidate data yet.</div>`;
      return;
    }
    target.innerHTML = rows.map(row => {
      const sym = String(row.symbol || '').toUpperCase();
      const snap = snapshotsByKey.get(sym) || {};
      const bias = String(row.directional_bias || '').toUpperCase();
      const tone = bias === 'SHORT' ? 'tone-short' : (bias === 'LONG' ? 'tone-long' : 'tone-neutral');
      // Activity score is an unbounded multiplier (live tape can push it
      // well above 1.0 for option strategies — see DashboardCache.build_payload
      // and ZeroDteEtfOptionsStrategy.live_activity_score). Use the
      // shared log-scaled scorePct() so the mobile readout matches the
      // desktop candidate ring and stays legible across the full range.
      const score = numOrNull(row.activity_score);
      const scoreDisplay = score === null ? '—' : Math.round(scorePct(score) * 100);
      // Live-first display: prefer streamed Schwab quote when present.
      const liveChange = numOrNull(snap?.quote?.percent_change) ?? numOrNull(row.change) ?? numOrNull(row.change_from_open);
      const liveLast = numOrNull(snap?.quote?.last) ?? numOrNull(row.close);
      return `<div class="m-candidate-row ${tone}" data-symbol="${escapeHtml(sym)}">
        <div class="m-sym">${escapeHtml(sym)}</div>
        <div class="m-bias">${escapeHtml(bias || 'NEUTRAL')}</div>
        <div class="m-pct ${pnlClass(liveChange)}">${fmtPct(liveChange)}</div>
        <div class="m-price">${fmtNum(liveLast, 2)}</div>
        <div class="m-score">${scoreDisplay}</div>
      </div>`;
    }).join('');
  }

  function renderTrades(data) {
    // Compact mobile completed-trades list: one row per recent close.
    // Top line: symbol + side + realized P&L (with % in muted sub-span).
    // Bottom line: exit time (HH:MM, MM-DD HH:MM when not today) + exit
    // reason (truncated). Pulls from ``performance.recent_trades`` — the
    // same shape the desktop dock-trades table renders (the last 20 closed
    // trades, the day's from before a restart among them), so payload
    // parity is automatic. The meta says what the list holds (the last N)
    // and the day's count of closed trades, tagged as the day's figures are
    // (``dayTag``: a partial day's says what it lacks).
    const perf = data?.performance || {};
    const day = perf.day || {};
    const trades = perf.recent_trades || [];
    const meta = document.getElementById('m-trades-meta');
    const target = document.getElementById('m-trade-list');
    const leftOut = perf.recent_trades_error ? ' · earlier trades of the day left out' : '';
    if (meta) {
      meta.textContent = trades.length
        ? `last ${fmtInteger(trades.length)} · ${fmtInteger(day.trades)} ${day.label || 'today'}${day.partial_label ? ` · ${day.partial_label}` : ''}${leftOut}`
        : `No closed trades${leftOut}`;
    }
    if (!target) return;
    if (!trades.length) {
      target.innerHTML = `<div class="empty-state">No closed trades yet.</div>`;
      return;
    }
    // Newest exit first, as the payload orders them.
    target.innerHTML = trades.map(t => {
      const side = String(t.side || '').toUpperCase();
      const tone = side === 'SHORT' ? 'tone-short' : 'tone-long';
      // Exit time HH:MM (no seconds), with its MM-DD when the exit was
      // not today (``exit_today``, the payload's). Bottom-meta is just
      // exit-time + reason — strategy name dropped (every trade on
      // this dashboard runs the same active strategy, so listing it
      // per-row is noise) and return-% dropped from the top-right
      // (realized $ alone is the signal that matters on a phone).
      const exitStamp = String(t.exit_time || '');
      const exitTime = t.exit_today ? exitStamp.slice(11, 16) : `${exitStamp.slice(5, 10)} ${exitStamp.slice(11, 16)}`.trim();
      const reason = String(t.reason || '').slice(0, 36);
      const metaParts = [exitTime, reason].filter(Boolean);
      return `<div class="m-trade-row ${tone}">
        <div class="m-trade-top">
          <div class="m-trade-sym">${escapeHtml(String(t.symbol || '').toUpperCase())}</div>
          <div class="m-trade-side">${escapeHtml(fmtSide(t))}</div>
          <div class="m-trade-pnl">${fmtMoney(t.realized_pnl)}</div>
        </div>
        <div class="m-trade-meta">${escapeHtml(metaParts.join(' · '))}</div>
      </div>`;
    }).join('');
  }

  // Whether every section drew.
  function render(data) {
    let ok = true;
    try { renderTopbar(data); } catch (err) { ok = false; console.error('renderTopbar failed', err); }
    try { renderKpiAndGauges(data); } catch (err) { ok = false; console.error('renderKpiAndGauges failed', err); }
    try { renderPositions(data); } catch (err) { ok = false; console.error('renderPositions failed', err); }
    try { renderCandidates(data); } catch (err) { ok = false; console.error('renderCandidates failed', err); }
    try { renderTrades(data); } catch (err) { ok = false; console.error('renderTrades failed', err); }
    return ok;
  }

  function renderDisconnectedBadge(message) {
    // The next poll's 200 redraws the badge (a 304 would leave it).
    stateEtag = null;
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
      const headers = stateEtag ? { 'If-None-Match': stateEtag } : {};
      const resp = await fetch('/api/state', { cache: 'no-store', credentials: 'same-origin', signal: abortCtl.signal, headers });
      // Before !resp.ok, which a 304 is not.
      if (resp.status === 304 && stateEtag) return;
      if (!resp.ok) {
        console.warn('mobile dashboard: /api/state returned', resp.status);
        renderDisconnectedBadge(`HTTP ${resp.status}`);
        return;
      }
      const etag = resp.headers.get('ETag');
      const data = await resp.json();
      stateEtag = null;
      // Only once every section drew: one that failed draws again on the
      // next poll's 200.
      if (render(data)) stateEtag = etag;
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
