
const DASHBOARD_CONFIG = window.DASHBOARD_CONFIG || {};
const REFRESH_MS = Number(DASHBOARD_CONFIG.refreshMs || 2000);
const IMAGE_ASSETS = DASHBOARD_CONFIG.images || {};
const CHART_BADGE_IMAGES = IMAGE_ASSETS;
// Hardcoded ET because the bot only trades US equities/options sessions
// and all server-side bar timestamps are emitted as ET-localized
// `pd.Timestamp.isoformat()`. Without forcing this, browsers in other
// timezones would render bar labels in their local TZ, mislabeling the
// market clock. If the bot ever supports non-US markets, plumb
// `runtime.timezone` through `DASHBOARD_CONFIG` and read it here.
const DASHBOARD_TIMEZONE = 'America/New_York';
// Cached Intl.DateTimeFormat instances. Per-call `toLocaleString({timeZone:...})`
// is significantly slower than reusing a pre-built formatter — each invocation
// re-parses options and constructs the underlying ICU resolver. Charts call
// these per axis tick (10-20× per render), so the cache shaves real time off
// every chart paint. Day-key formatter uses en-CA's YYYY-MM-DD output so
// string compare cleanly identifies trading-day rollovers in ET.
const CHART_TS_FMT = new Intl.DateTimeFormat([], {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  timeZone: DASHBOARD_TIMEZONE,
});
const TIME_AXIS_FMT = new Intl.DateTimeFormat([], {
  hour: 'numeric',
  minute: '2-digit',
  timeZone: DASHBOARD_TIMEZONE,
});
const DAY_AXIS_FMT = new Intl.DateTimeFormat('en-US', {
  month: '2-digit',
  day: '2-digit',
  timeZone: DASHBOARD_TIMEZONE,
});
const DAY_KEY_FMT = new Intl.DateTimeFormat('en-CA', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  timeZone: DASHBOARD_TIMEZONE,
});
const EXPANDED_CHART_CACHE_MAX = 6;
const EXPANDED_CHART_CACHE_TTL_MS = 20 * 60 * 1000;
const COMPACT_CHART_CACHE_MAX = 8;
const COMPACT_CHART_CACHE_TTL_MS = 20 * 60 * 1000;
const appState = {
  data: null,
  snapshotMap: new Map(),
  selectedSymbol: null,
  filter: 'all',
  dockTab: 'events',
  refreshInFlight: false,
  mainPanelExpanded: false,
  expandedChart: {
    symbol: null,
    bars: null,
    maxBars: 360,
    timeframeMode: '1m',
    sourceKey: null,
    requestSeq: 0,
    lastBarTs: null,
    timeframeLabel: '1M',
    htfRefreshToken: null,
    pinnedBarId: null,
    refreshRafId: 0,
    pendingForceRefresh: false,
    stateLastUpdate: null,
    isLoading: false,
    cache: new Map(),
  },
  compactChart: {
    symbol: null,
    bars: null,
    maxBars: 90,
    timeframeMode: '1m',
    sourceKey: null,
    requestSeq: 0,
    lastBarTs: null,
    timeframeLabel: '1M',
    htfRefreshToken: null,
    patterns: {},
    structureOverlay: {},
    stateLastUpdate: null,
    isLoading: false,
    cache: new Map(),
  },
};

function safe(value) {
  return value === null || value === undefined || value === '' ? '—' : String(value);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function parseFinite(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function fmtNum(value, digits = 2) {
  const n = parseFinite(value);
  return n !== null ? n.toFixed(digits) : '—';
}

function fmtMoney(value) {
  const n = parseFinite(value);
  if (n === null) return '—';
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(n);
}

function fmtPct(value, digits = 2) {
  const n = parseFinite(value);
  return n !== null ? `${n.toFixed(digits)}%` : '—';
}

// Format side for display: stocks show just "LONG"/"SHORT", but options also
// append the contract type ("LONG · CALL", "SHORT · PUT") so the put/call
// dimension is visible without having to inspect asset_type or spread_type.
function fmtSide(entity) {
  const side = String(entity?.side || '').trim();
  if (!side) return '—';
  const opt = String(entity?.option_type || '').trim().toUpperCase();
  return opt ? `${side} · ${opt}` : side;
}

function fmtPctFromRatio(value, digits = 2) {
  const n = parseFinite(value);
  return n !== null ? fmtPct(n * 100, digits) : '—';
}

function fmtPctSmart(value) {
  const n = parseFinite(value);
  if (n === null) return '—';
  const abs = Math.abs(n);
  if (abs >= 10) return `${n.toFixed(0)}%`;
  if (abs >= 1) return `${n.toFixed(1)}%`;
  if (abs > 0) return `${n.toFixed(2)}%`;
  return '0%';
}

function fmtCompact(value) {
  const n = parseFinite(value);
  if (n === null) return '—';
  return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(n);
}

function fmtInteger(value) {
  const n = parseFinite(value);
  return n !== null ? new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(n) : '—';
}

function fmtChartTs(value) {
  if (!value) return '—';
  const dt = new Date(value);
  if (Number.isFinite(dt.getTime())) {
    return CHART_TS_FMT.format(dt);
  }
  return safe(value).replace('T', ' ').slice(0, 16);
}

function numOrNull(value) {
  return parseFinite(value);
}

function positiveOrNull(value) {
  const n = numOrNull(value);
  return n !== null && n > 0 ? n : null;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function positionRangeSpec(pos) {
  const entry = numOrNull(pos?.entry_price);
  const stop = numOrNull(pos?.stop_price);
  const target = numOrNull(pos?.target_price);
  const last = numOrNull(pos?.last_price);
  const side = String(pos?.side || '').toUpperCase();
  if (entry === null || stop === null || target === null || last === null) return null;
  const adverseSpan = Math.abs(entry - stop);
  const favorableSpan = Math.abs(target - entry);
  if (!(adverseSpan > 0) || !(favorableSpan > 0)) return null;
  const favorableMove = side === 'SHORT' ? (entry - last) : (last - entry);
  const towardTarget = favorableMove >= 0;
  const span = towardTarget ? favorableSpan : adverseSpan;
  if (!(span > 0)) return null;
  const progress = clamp(Math.abs(favorableMove) / span, 0, 1);
  const widthPct = progress * 50;
  const markerPct = towardTarget ? 50 + widthPct : 50 - widthPct;
  const destination = towardTarget ? 'target' : 'stop';
  const statusTone = towardTarget ? 'good' : 'bad';
  const progressPct = progress * 100;
  const remainingPct = (1 - progress) * 100;
  return {
    leftPct: towardTarget ? 50 : 50 - widthPct,
    widthPct,
    markerPct: clamp(markerPct, 0, 100),
    tone: towardTarget ? 'good' : 'bad',
    progressPct,
    destination,
    statusTone,
    progressLabel: `${fmtPct(progressPct / 100, 1)} to ${destination}`,
    remainingLabel: `${fmtPct(remainingPct / 100, 1)} remaining`,
  };
}

function positionRangeMarkup(pos) {
  const spec = positionRangeSpec(pos);
  if (!spec) {
    // pos.return_pct comes in as an already-multiplied percentage from
    // paper_account._return_pct (e.g. 25 for +25%), so use ×4 so that a
    // 25% return fills the bar. (Was ×400 under the old ratio assumption,
    // which kept the bar permanently clamped to 100.)
    const pnlBar = numOrNull(pos?.return_pct) === null ? 0 : clamp(Math.abs(Number(pos.return_pct)) * 4, 0, 100);
    const fillTone = Number(pos?.return_pct) < 0 ? 'bad' : '';
    return `<div style="margin-top:12px;" class="bar-track"><div class="bar-fill ${fillTone}" style="width:${pnlBar}%;"></div></div>`;
  }
  const fillTone = spec.tone === 'bad' ? 'bad' : '';
  return `<div class="trade-range">
    <div class="trade-range-track">
      <div class="trade-range-fill ${fillTone}" style="left:${spec.leftPct}%; width:${spec.widthPct}%;"></div>
      <div class="trade-range-center"></div>
    </div>
    <div class="trade-range-status">
      <div class="tiny-label">Progress</div>
      <div class="emph ${fillTone}">${escapeHtml(spec.progressLabel)}</div>
      <div class="tiny-label">${escapeHtml(spec.remainingLabel)}</div>
    </div>
    <div class="trade-range-labels">
      <div class="trade-range-label left"><div class="tiny-label">Stop</div><div class="v">${fmtNum(pos?.stop_price, 2)}</div></div>
      <div class="trade-range-label right"><div class="tiny-label">Target</div><div class="v">${fmtNum(pos?.target_price, 2)}</div></div>
    </div>
  </div>`;
}

function pnlTone(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return 'tone-neutral';
  return n > 0 ? 'tone-good' : 'tone-bad';
}

function pnlClass(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return '';
  return n > 0 ? 'good' : 'bad';
}

function selectedSymbolTone(snapshot) {
  if (!snapshot) return 'tone-neutral';
  const bias = String(snapshot?.candidate?.directional_bias || snapshot?.position?.side || '').toUpperCase();
  const trend = String(snapshot?.support_resistance?.trend_state || snapshot?.support_resistance?.structure_bias || '').toLowerCase();
  if (bias === 'LONG' || trend === 'bullish') return 'tone-long';
  if (bias === 'SHORT' || trend === 'bearish') return 'tone-short';
  return 'tone-neutral';
}

function eventTone(name) {
  const token = String(name || '').toUpperCase();
  if (token.includes('BOS↑') || token.includes('CHOCH↑') || token.includes('EQL') || token.includes('BREAKOUT') || token.includes('NEAR_SUPPORT')) return 'event-bullish';
  if (token.includes('BOS↓') || token.includes('CHOCH↓') || token.includes('EQH') || token.includes('BREAKDOWN') || token.includes('NEAR_RESISTANCE')) return 'event-bearish';
  if (token.includes('ERROR') || token.includes('BLOCKED') || token.includes('DISCONNECTED')) return 'event-bearish';
  if (token.includes('WARNING') || token.includes('PAUSED')) return 'tone-warn';
  return 'event-neutral';
}

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

function tinyChip(text, tone = 'tone-neutral') {
  return `<span class="tiny-chip ${tone}">${escapeHtml(text)}</span>`;
}

function iconForState(state, fallbackLabel, contextLabel = 'State') {
  const token = String(state || fallbackLabel || '').toLowerCase();
  const src = token.includes('bull') ? CHART_BADGE_IMAGES.bullish : (token.includes('bear') ? CHART_BADGE_IMAGES.bearish : CHART_BADGE_IMAGES.neutral);
  const label = token.includes('bull') ? 'Bullish' : (token.includes('bear') ? 'Bearish' : 'Neutral');
  const fullLabel = `${label} ${contextLabel}`.trim();
  return `<div class="icon-badge" title="${fullLabel}"><img src="${src}" alt="${fullLabel}" title="${fullLabel}" /></div>`;
}

function structureEventChip(value) {
  const text = safe(value);
  return `<span class="event-chip ${eventTone(text)}">${escapeHtml(text)}</span>`;
}

function stateChip(value) {
  const token = String(value || 'neutral');
  return `<span class="status-chip ${eventTone(token)}">${escapeHtml(token.replace(/_/g, ' '))}</span>`;
}

function humanizeDecisionToken(value) {
  const raw = String(value || '').trim();
  if (!raw) return '—';
  const [head, ...rest] = raw.split(':');
  const humanHead = head.replace(/_/g, ' ');
  return rest.length ? `${humanHead}: ${rest.join(':')}` : humanHead;
}

// Compact form: drops the parameter detail after the first colon.
// Used in tight UI spots (e.g. the focus-meta line next to the symbol name)
// where long ETF-options skip reasons like
// `option_quote_unstable:bid=1.05 ask=1.20 mid=1.13 stability_pct=14.2`
// would push the live-price/change/volume chips off the right edge.
// The full detail still appears in the score-sub line below.
function humanizeDecisionTokenCompact(value) {
  const raw = String(value || '').trim();
  if (!raw) return '—';
  const head = raw.split(':')[0];
  return head.replace(/_/g, ' ');
}

function entryDecisionForSnapshot(snapshot) {
  if (!snapshot) return null;
  const direct = snapshot.entry_decision;
  return direct && typeof direct === 'object' ? direct : null;
}

function entryDecisionLabel(decision) {
  if (!decision || typeof decision !== 'object') return '';
  const action = String(decision.action || '').trim().toLowerCase();
  const primary = humanizeDecisionToken(decision.primary_reason || (Array.isArray(decision.reasons) ? decision.reasons[0] : ''));
  if (!action && !primary) return '';
  if (!primary || primary === '—') return action || '';
  if (!action || action === 'signal') return primary;
  return `${action}: ${primary}`;
}

// Compact variant for tight UI surfaces. Same shape as
// entryDecisionLabel() but uses the head-only humanizer so long
// parameter strings (e.g. option-strategy quote-stability values)
// don't blow out the focus-meta line layout.
function entryDecisionLabelCompact(decision) {
  if (!decision || typeof decision !== 'object') return '';
  const action = String(decision.action || '').trim().toLowerCase();
  const primary = humanizeDecisionTokenCompact(decision.primary_reason || (Array.isArray(decision.reasons) ? decision.reasons[0] : ''));
  if (!action && !primary) return '';
  if (!primary || primary === '—') return action || '';
  if (!action || action === 'signal') return primary;
  return `${action}: ${primary}`;
}

function normalizeTradingViewSymbol(symbol) {
  const raw = String(symbol || '').toUpperCase().trim();
  if (!raw) return '';
  if (raw.startsWith('Q-') && raw.length > 2) return raw.slice(2).trim();
  return raw;
}

function normalizeTradingViewExchange(exchange) {
  const raw = String(exchange || '').toUpperCase().trim();
  if (!raw) return '';
  if (raw === 'Q' || raw === 'Q-' || raw.startsWith('Q-')) return '';
  return raw;
}

function tradingViewSymbolMeta(symbol, exchangeMap, rowExchange) {
  const rawSymbol = String(symbol || '').toUpperCase().trim();
  const normalizedSymbol = normalizeTradingViewSymbol(rawSymbol);
  const mappedExchange = exchangeMap && (exchangeMap[rawSymbol] || exchangeMap[normalizedSymbol]);
  const exchange = normalizeTradingViewExchange(rowExchange) || normalizeTradingViewExchange(mappedExchange) || '';
  return { rawSymbol, normalizedSymbol, exchange };
}

function symbolLink(symbol, exchangeMap, displaySymbol, rowExchange) {
  const { rawSymbol, normalizedSymbol, exchange } = tradingViewSymbolMeta(symbol, exchangeMap, rowExchange);
  if (!rawSymbol) return '—';
  const rawLabel = String(displaySymbol || normalizedSymbol || rawSymbol).toUpperCase().trim();
  const label = normalizeTradingViewSymbol(rawLabel) || normalizedSymbol || rawSymbol;
  if (!exchange || !normalizedSymbol) return escapeHtml(label);
  const href = `https://www.tradingview.com/symbols/${encodeURIComponent(exchange)}-${encodeURIComponent(normalizedSymbol)}/`;
  return `<a class="tv-link" href="${href}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
}

function tradeSymbolCell(trade, exchangeMap) {
  const assetType = String(trade?.asset_type || '').toUpperCase().trim();
  const tradeSymbol = String(trade?.symbol || '').toUpperCase().trim();
  const underlying = String(trade?.underlying || '').toUpperCase().trim();
  if (assetType.startsWith('OPTION')) {
    if (underlying) {
      const underlyingLink = symbolLink(underlying, exchangeMap, underlying);
      return `${underlyingLink}<div class="table-sub">${escapeHtml(tradeSymbol || assetType)}</div>`;
    }
    return escapeHtml(tradeSymbol || '—');
  }
  return symbolLink(tradeSymbol, exchangeMap, tradeSymbol, trade?.exchange);
}

function sparklineSVG(values, tone) {
  const nums = (values || []).map(numOrNull).filter(v => v !== null);
  if (nums.length < 2) return '<svg viewBox="0 0 100 28" preserveAspectRatio="none"></svg>';
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = Math.max(max - min, 1e-9);
  const pts = nums.map((v, idx) => {
    const x = (idx / Math.max(nums.length - 1, 1)) * 100;
    const y = 26 - ((v - min) / span) * 22;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
  const area = `0,28 ${pts} 100,28`;
  const stroke = tone === 'tone-bad' ? '#ff6b82' : (tone === 'tone-good' ? '#6ce3a2' : '#79d4ff');
  return `<svg viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"></polyline><polygon points="${area}" fill="${stroke}" opacity="0.10"></polygon></svg>`;
}

function warmupTone(warmup) {
  if (!warmup || typeof warmup !== 'object') return 'tone-neutral';
  const current = numOrNull(warmup.current_bars) ?? 0;
  if (warmup.ready) return 'tone-good';
  if (current <= 0) return 'tone-bad';
  return 'tone-warn';
}

function warmupLabel(warmup) {
  if (!warmup || typeof warmup !== 'object') return '';
  const current = numOrNull(warmup.current_bars) ?? 0;
  if (warmup.ready) return 'Ready';
  if (current <= 0) return 'Not Ready';
  return 'Loading';
}

function warmupChip(warmup) {
  const label = warmupLabel(warmup);
  if (!label) return '';
  return tinyChip(label, warmupTone(warmup));
}


function buildSnapshotMap(data) {
  const out = new Map();
  (data?.dashboard_symbols || []).forEach(item => {
    if (item && item.symbol) out.set(String(item.symbol).toUpperCase(), item);
  });
  return out;
}

function activeSnapshotMap(data = appState.data) {
  if (data === appState.data && appState.snapshotMap instanceof Map && appState.snapshotMap.size) return appState.snapshotMap;
  return buildSnapshotMap(data);
}

function rawDashboardCharting(data = appState.data) {
  const raw = data && data.dashboard_charting;
  return raw && typeof raw === 'object' ? raw : {};
}

function dashboardChartProfile(mode = 'compact', data = appState.data) {
  const raw = rawDashboardCharting(data);
  if (raw && (raw.compact || raw.expanded)) {
    const profile = raw[mode] || raw.compact || raw.expanded || {};
    return profile && typeof profile === 'object' ? profile : {};
  }
  return raw;
}

function currentChartProfile(data = appState.data) {
  return dashboardChartProfile(appState.mainPanelExpanded ? 'expanded' : 'compact', data);
}

function normalizedExpandedChartTimeframeMode(value) {
  return String(value || '1m').trim().toLowerCase() === 'htf' ? 'htf' : '1m';
}

function compactChartTimeframeMode(data = appState.data) {
  const raw = rawDashboardCharting(data);
  return String(raw?.compact_chart_timeframe || 'ltf').trim().toLowerCase() === 'htf' ? 'htf' : '1m';
}

function chartMaxBarsForMode(viewMode = 'compact', data = appState.data) {
  const profile = dashboardChartProfile(viewMode, data);
  const fallback = String(viewMode || '').toLowerCase() === 'expanded' ? 360 : 90;
  return Math.max(1, Math.min(Number(profile?.max_bars) || fallback, 480));
}

function expandedChartMaxBars(data = appState.data) {
  return chartMaxBarsForMode('expanded', data);
}

function compactChartMaxBars(data = appState.data) {
  return chartMaxBarsForMode('compact', data);
}

function expandedChartTimeframeMode() {
  return normalizedExpandedChartTimeframeMode(appState.expandedChart?.timeframeMode);
}

function selectedSnapshot(data = appState.data, symbol = appState.selectedSymbol) {
  const normalizedSymbol = String(symbol || '').toUpperCase();
  if (!normalizedSymbol) return null;
  return activeSnapshotMap(data).get(normalizedSymbol) || null;
}

function currentHtfTimeframeLabel(data = appState.data, symbol = appState.selectedSymbol) {
  const snapshot = selectedSnapshot(data, symbol);
  const snapshotLabel = String(snapshot?.support_resistance?.timeframe || '').trim();
  if (snapshotLabel) return snapshotLabel;
  const normalizedSymbol = String(symbol || '').toUpperCase();
  if (normalizedSymbol) {
    if (normalizedExpandedChartTimeframeMode(appState.expandedChart?.timeframeMode) === 'htf'
      && String(appState.expandedChart?.symbol || '').toUpperCase() === normalizedSymbol) {
      const expandedLabel = String(appState.expandedChart?.timeframeLabel || '').trim();
      if (expandedLabel) return expandedLabel;
    }
    if (normalizedExpandedChartTimeframeMode(appState.compactChart?.timeframeMode) === 'htf'
      && String(appState.compactChart?.symbol || '').toUpperCase() === normalizedSymbol) {
      const compactLabel = String(appState.compactChart?.timeframeLabel || '').trim();
      if (compactLabel) return compactLabel;
    }
  }
  return 'HTF';
}

function currentHtfRefreshToken(data = appState.data, symbol = appState.selectedSymbol) {
  const snapshot = selectedSnapshot(data, symbol);
  const snapshotToken = String(snapshot?.support_resistance?.htf_refresh_token || '').trim();
  if (snapshotToken) return snapshotToken;
  const normalizedSymbol = String(symbol || '').toUpperCase();
  if (normalizedSymbol) {
    if (normalizedExpandedChartTimeframeMode(appState.expandedChart?.timeframeMode) === 'htf'
      && String(appState.expandedChart?.symbol || '').toUpperCase() === normalizedSymbol) {
      const expandedToken = String(appState.expandedChart?.htfRefreshToken || '').trim();
      if (expandedToken) return expandedToken;
    }
    if (normalizedExpandedChartTimeframeMode(appState.compactChart?.timeframeMode) === 'htf'
      && String(appState.compactChart?.symbol || '').toUpperCase() === normalizedSymbol) {
      const compactToken = String(appState.compactChart?.htfRefreshToken || '').trim();
      if (compactToken) return compactToken;
    }
  }
  return '';
}

function expandedChartTimeframeLabel(data = appState.data, mode = expandedChartTimeframeMode()) {
  if (normalizedExpandedChartTimeframeMode(mode) === 'htf') {
    return currentHtfTimeframeLabel(data);
  }
  return '1M';
}

function chartBarIdentity(bar, fallbackIndex = -1) {
  const absIndex = numOrNull(bar?.abs_index);
  if (absIndex !== null) return `a:${absIndex}`;
  const ts = String(bar?.ts || '').trim();
  if (ts) return `t:${ts}`;
  if (fallbackIndex >= 0) return `i:${fallbackIndex}`;
  return null;
}

function compareChartBars(left, right) {
  const leftAbs = numOrNull(left?.abs_index);
  const rightAbs = numOrNull(right?.abs_index);
  if (leftAbs !== null || rightAbs !== null) {
    return (leftAbs ?? Number.NEGATIVE_INFINITY) - (rightAbs ?? Number.NEGATIVE_INFINITY);
  }
  return String(left?.ts || '').localeCompare(String(right?.ts || ''));
}

function chartBarsSignature(bars) {
  if (!Array.isArray(bars) || !bars.length) return '0';
  const tail = bars.slice(-3).map((bar) => [
    chartBarIdentity(bar),
    safe(bar?.ts),
    safe(bar?.open),
    safe(bar?.high),
    safe(bar?.low),
    safe(bar?.close),
    safe(bar?.volume),
  ].join('|')).join('||');
  return `${bars.length}|${tail}`;
}
function writeExpandedChartCache(sourceKey, cacheEntry) {
  if (!sourceKey || !cacheEntry || !Array.isArray(cacheEntry.bars) || !cacheEntry.bars.length) return;
  const symbol = String(cacheEntry.symbol || '').toUpperCase();
  const maxBars = Math.max(1, Number(cacheEntry.maxBars) || 90);
  appState.expandedChart.cache.set(sourceKey, {
    ...cacheEntry,
    symbol,
    maxBars,
    updatedAt: Number(cacheEntry.updatedAt) || Date.now(),
  });
  pruneExpandedChartCache({ preserveSourceKey: sourceKey });
}

function pruneExpandedChartCache({ preserveSourceKey = null } = {}) {
  const cache = appState.expandedChart.cache;
  if (!(cache instanceof Map) || !cache.size) return;
  const now = Date.now();
  const preserved = new Set([preserveSourceKey, appState.expandedChart.sourceKey].filter(Boolean));
  const latestByPrefix = new Map();
  for (const [key, entry] of cache.entries()) {
    if (!entry || !Array.isArray(entry.bars) || !entry.bars.length) continue;
    const updatedAt = Number(entry.updatedAt) || now;
    if (!preserved.has(key) && (now - updatedAt) > EXPANDED_CHART_CACHE_TTL_MS) continue;
    const symbol = String(entry.symbol || '').toUpperCase();
    const maxBars = Math.max(1, Number(entry.maxBars) || Number(entry.bars?.length) || 90);
    const timeframeMode = normalizedExpandedChartTimeframeMode(entry.timeframeMode);
    const prefix = `${symbol}|${maxBars}|${timeframeMode}`;
    const current = latestByPrefix.get(prefix);
    const shouldReplace = !current || preserved.has(key) || (!preserved.has(current[0]) && updatedAt >= (Number(current[1]?.updatedAt) || 0));
    if (shouldReplace) latestByPrefix.set(prefix, [key, { ...entry, symbol, maxBars, updatedAt }]);
  }
  let entries = Array.from(latestByPrefix.values());
  entries.sort((left, right) => (Number(right[1]?.updatedAt) || 0) - (Number(left[1]?.updatedAt) || 0));
  if (entries.length > EXPANDED_CHART_CACHE_MAX) {
    const keepKeys = new Set(entries.slice(0, EXPANDED_CHART_CACHE_MAX).map(([key]) => key));
    preserved.forEach((key) => {
      if (!keepKeys.has(key) && cache.has(key)) {
        const entry = cache.get(key);
        if (entry && Array.isArray(entry.bars) && entry.bars.length) {
          keepKeys.add(key);
          entries.push([key, entry]);
        }
      }
    });
    entries = entries.filter(([key]) => keepKeys.has(key));
  }
  const nextCache = new Map();
  entries
    .sort((left, right) => (Number(left[1]?.updatedAt) || 0) - (Number(right[1]?.updatedAt) || 0))
    .forEach(([key, entry]) => nextCache.set(key, entry));
  appState.expandedChart.cache = nextCache;
}

function mergeChartBars(existingBars, incomingBars, maxBars) {
  const merged = new Map();
  (Array.isArray(existingBars) ? existingBars : []).forEach((bar, idx) => {
    const key = chartBarIdentity(bar, idx);
    if (!key) return;
    merged.set(key, bar);
  });
  (Array.isArray(incomingBars) ? incomingBars : []).forEach((bar, idx) => {
    const key = chartBarIdentity(bar, idx);
    if (!key) return;
    const previous = merged.get(key) || {};
    merged.set(key, { ...previous, ...bar });
  });
  return Array.from(merged.values())
    .sort(compareChartBars)
    .slice(-Math.max(1, Number(maxBars) || 90));
}

function syncExpandedChartFromBaseSnapshot(data = appState.data) {
  if (!appState.mainPanelExpanded || !appState.selectedSymbol) return false;
  if (expandedChartTimeframeMode() !== '1m') return false;
  const symbol = String(appState.selectedSymbol || '').toUpperCase();
  if (!symbol) return false;
  const snapshot = activeSnapshotMap(data).get(symbol) || null;
  const baseBars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
  if (!baseBars.length) return false;
  const maxBars = expandedChartMaxBars(data);
  const sourceKey = expandedChartSourceKey(symbol, maxBars, data);
  const cached = appState.expandedChart.cache.get(sourceKey) || null;
  const currentBars = (
    String(appState.expandedChart.symbol || '').toUpperCase() === symbol &&
    Array.isArray(appState.expandedChart.bars) &&
    appState.expandedChart.bars.length
  ) ? appState.expandedChart.bars : (Array.isArray(cached?.bars) ? cached.bars : []);
  if (!currentBars.length) return false;
  const mergedBars = mergeChartBars(currentBars, baseBars, maxBars);
  if (!mergedBars.length) return false;
  const changed = chartBarsSignature(mergedBars) !== chartBarsSignature(currentBars);
  const mode = expandedChartTimeframeMode();
  const cacheEntry = {
    bars: mergedBars,
    lastBarTs: safe(mergedBars[mergedBars.length - 1]?.ts),
    symbol,
    maxBars,
    timeframeMode: mode,
    timeframeLabel: expandedChartTimeframeLabel(data, mode),
    patterns: snapshot?.chart?.patterns || {},
    structureOverlay: snapshot?.chart?.structure_overlay || {},
    htfRefreshToken: null,
    stateLastUpdate: safe(data?.last_update),
    updatedAt: Date.now(),
  };
  writeExpandedChartCache(sourceKey, cacheEntry);
  appState.expandedChart.maxBars = maxBars;
  appState.expandedChart.timeframeMode = mode;
  appState.expandedChart.symbol = symbol;
  appState.expandedChart.bars = mergedBars;
  appState.expandedChart.sourceKey = sourceKey;
  appState.expandedChart.lastBarTs = cacheEntry.lastBarTs;
  appState.expandedChart.timeframeLabel = cacheEntry.timeframeLabel;
  appState.expandedChart.htfRefreshToken = cacheEntry.htfRefreshToken || null;
  appState.expandedChart.patterns = cacheEntry.patterns || {};
  appState.expandedChart.structureOverlay = cacheEntry.structureOverlay || {};
  appState.expandedChart.stateLastUpdate = cacheEntry.stateLastUpdate || safe(appState.data?.last_update);
  return changed;
}

function syncCompactChartFromBaseSnapshot(data = appState.data) {
  if (appState.mainPanelExpanded || !appState.selectedSymbol) return false;
  if (compactChartTimeframeMode(data) !== '1m') return false;
  const symbol = String(appState.selectedSymbol || '').toUpperCase();
  if (!symbol) return false;
  const snapshot = activeSnapshotMap(data).get(symbol) || null;
  const baseBars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
  if (!baseBars.length) return false;
  const maxBars = compactChartMaxBars(data);
  const sourceKey = compactChartSourceKey(symbol, maxBars, '1m');
  const cached = appState.compactChart.cache.get(sourceKey) || null;
  const currentBars = (
    String(appState.compactChart.symbol || '').toUpperCase() === symbol
    && normalizedExpandedChartTimeframeMode(appState.compactChart.timeframeMode) === '1m'
    && Array.isArray(appState.compactChart.bars)
    && appState.compactChart.bars.length
  ) ? appState.compactChart.bars : (Array.isArray(cached?.bars) ? cached.bars : []);
  if (!currentBars.length) return false;
  const mergedBars = mergeChartBars(currentBars, baseBars, maxBars);
  if (!mergedBars.length) return false;
  const changed = chartBarsSignature(mergedBars) !== chartBarsSignature(currentBars)
    || appState.compactChart.sourceKey !== sourceKey;
  const cacheEntry = {
    bars: mergedBars,
    lastBarTs: safe(mergedBars[mergedBars.length - 1]?.ts),
    symbol,
    maxBars,
    timeframeMode: '1m',
    timeframeLabel: expandedChartTimeframeLabel(data, '1m'),
    patterns: snapshot?.chart?.patterns || appState.compactChart.patterns || {},
    structureOverlay: snapshot?.chart?.structure_overlay || appState.compactChart.structureOverlay || {},
    htfRefreshToken: null,
    stateLastUpdate: safe(data?.last_update),
    updatedAt: Date.now(),
  };
  writeCompactChartCache(sourceKey, cacheEntry);
  appState.compactChart.maxBars = maxBars;
  appState.compactChart.timeframeMode = '1m';
  appState.compactChart.symbol = symbol;
  appState.compactChart.bars = mergedBars;
  appState.compactChart.sourceKey = sourceKey;
  appState.compactChart.lastBarTs = cacheEntry.lastBarTs;
  appState.compactChart.timeframeLabel = cacheEntry.timeframeLabel;
  appState.compactChart.htfRefreshToken = null;
  appState.compactChart.patterns = cacheEntry.patterns || {};
  appState.compactChart.structureOverlay = cacheEntry.structureOverlay || {};
  appState.compactChart.stateLastUpdate = cacheEntry.stateLastUpdate || safe(data?.last_update);
  return changed;
}

function expandedChartSourceKey(symbol, maxBars, data = appState.data, mode = expandedChartTimeframeMode()) {
  const normalizedSymbol = String(symbol || '').toUpperCase();
  const timeframeMode = normalizedExpandedChartTimeframeMode(mode);
  if (timeframeMode === 'htf') {
    return `${normalizedSymbol}|${Math.max(1, Number(maxBars) || 90)}|${timeframeMode}`;
  }
  const snapshot = activeSnapshotMap(data).get(normalizedSymbol) || null;
  const baseBars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
  const lastBarTs = baseBars.length ? safe(baseBars[baseBars.length - 1]?.ts) : safe(data?.last_update);
  return `${normalizedSymbol}|${Math.max(1, Number(maxBars) || 90)}|${timeframeMode}|${lastBarTs}`;
}

function currentChartBars(snapshot) {
  const baseBars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
  const expanded = appState.expandedChart || {};
  const compact = appState.compactChart || {};
  if (!snapshot?.symbol) return baseBars;
  if (!appState.mainPanelExpanded) {
    const targetSymbol = String(snapshot.symbol || '').toUpperCase();
    const compactMode = compactChartTimeframeMode(appState.data);
    const targetMaxBars = compactChartMaxBars(appState.data);
    const targetSourceKey = compactChartSourceKey(targetSymbol, targetMaxBars, compactMode);
    const cachedCompact = appState.compactChart.cache.get(targetSourceKey) || null;
    const cachedCompactBars = Array.isArray(cachedCompact?.bars) ? cachedCompact.bars : [];
    const compactSymbol = String(compact.symbol || '').toUpperCase();
    const compactBars = Array.isArray(compact.bars) ? compact.bars : [];
    const compactModeMatchesTarget = compactSymbol === targetSymbol
      && normalizedExpandedChartTimeframeMode(compact.timeframeMode) === compactMode
      && compactBars.length;
    const compactMatchesTarget = compactModeMatchesTarget
      && compact.sourceKey === targetSourceKey;
    if (compactMatchesTarget) {
      if (compactMode !== '1m' || !baseBars.length) return compactBars;
      return mergeChartBars(compactBars, baseBars, targetMaxBars);
    }
    if (compactModeMatchesTarget) {
      if (compactMode !== '1m' || !baseBars.length) return compactBars;
      return mergeChartBars(compactBars, baseBars, targetMaxBars);
    }
    if (cachedCompactBars.length) {
      if (compactMode !== '1m' || !baseBars.length) return cachedCompactBars;
      return mergeChartBars(cachedCompactBars, baseBars, targetMaxBars);
    }
    const loadingSelected = !!compact.isLoading && String(appState.selectedSymbol || '').toUpperCase() === targetSymbol;
    if (loadingSelected && compactMode === '1m' && baseBars.length && baseBars.length < targetMaxBars) {
      return [];
    }
    return baseBars;
  }
  const targetSymbol = String(snapshot.symbol || '').toUpperCase();
  const targetMode = expandedChartTimeframeMode();
  const targetMaxBars = expandedChartMaxBars(appState.data);
  const targetSourceKey = expandedChartSourceKey(targetSymbol, targetMaxBars, appState.data, targetMode);
  const cachedExpanded = appState.expandedChart.cache.get(targetSourceKey) || null;
  const cachedExpandedBars = Array.isArray(cachedExpanded?.bars) ? cachedExpanded.bars : [];
  if (String(expanded.symbol || '').toUpperCase() !== targetSymbol) {
    return cachedExpandedBars.length ? cachedExpandedBars : [];
  }
  if (normalizedExpandedChartTimeframeMode(expanded.timeframeMode) !== targetMode) {
    return cachedExpandedBars.length ? cachedExpandedBars : [];
  }
  if (!Array.isArray(expanded.bars) || !expanded.bars.length) {
    return cachedExpandedBars.length ? cachedExpandedBars : [];
  }
  if (targetMode !== '1m' || !baseBars.length) return expanded.bars;
  return mergeChartBars(expanded.bars, baseBars, targetMaxBars);
}

function chartLoadingState(snapshot) {
  const symbol = String(snapshot?.symbol || '').toUpperCase();
  if (!symbol) return { active: false, message: 'Loading chart…' };
  if (appState.mainPanelExpanded) {
    const active = !!appState.expandedChart?.isLoading && String(appState.selectedSymbol || '').toUpperCase() === symbol;
    return { active, message: 'Loading expanded chart…' };
  }
  const compactMode = compactChartTimeframeMode(appState.data);
  const active = !!appState.compactChart?.isLoading && String(appState.selectedSymbol || '').toUpperCase() === symbol;
  return { active, message: 'Loading chart…' };
}

function renderChartTimeframeToggle(data = appState.data) {
  const toggle = document.getElementById('chart-timeframe-toggle');
  if (!toggle) return;
  const mode = expandedChartTimeframeMode();
  const htfLabel = expandedChartTimeframeLabel(data, 'htf');
  toggle.querySelectorAll('.chart-timeframe-btn[data-timeframe-mode]').forEach((btn) => {
    const btnMode = normalizedExpandedChartTimeframeMode(btn.dataset.timeframeMode);
    btn.classList.toggle('active', btnMode === mode);
    if (btnMode === 'htf') btn.textContent = `HTF ${htfLabel}`;
    if (btnMode === '1m') btn.textContent = `${expandedChartTimeframeLabel(data, '1m')} LTF`;
  });
}

function setExpandedChartTimeframeMode(mode) {
  const next = normalizedExpandedChartTimeframeMode(mode);
  if (appState.expandedChart.timeframeMode === next) {
    renderChartTimeframeToggle(appState.data);
    if (appState.mainPanelExpanded) scheduleExpandedChartRefresh(true);
    return;
  }
  appState.expandedChart.timeframeMode = next;
  appState.expandedChart.isLoading = true;
  appState.expandedChart.bars = null;
  appState.expandedChart.sourceKey = null;
  appState.expandedChart.lastBarTs = null;
  appState.expandedChart.timeframeLabel = '1M';
  appState.expandedChart.htfRefreshToken = null;
  appState.expandedChart.patterns = {};
  appState.expandedChart.structureOverlay = {};
  appState.expandedChart.pinnedBarId = null;
  if (appState.data) renderSelectedSymbol();
  else renderChartTimeframeToggle();
  if (appState.mainPanelExpanded) scheduleExpandedChartRefresh(true);
}

function cancelScheduledExpandedChartRefresh() {
  const rafId = Number(appState.expandedChart?.refreshRafId || 0);
  if (rafId) {
    window.cancelAnimationFrame(rafId);
    appState.expandedChart.refreshRafId = 0;
  }
  appState.expandedChart.pendingForceRefresh = false;
}

function resetExpandedChartCache() {
  cancelScheduledExpandedChartRefresh();
  appState.expandedChart.symbol = null;
  appState.expandedChart.bars = null;
  appState.expandedChart.sourceKey = null;
  appState.expandedChart.lastBarTs = null;
  appState.expandedChart.timeframeLabel = '1M';
  appState.expandedChart.htfRefreshToken = null;
  appState.expandedChart.patterns = {};
  appState.expandedChart.structureOverlay = {};
  appState.expandedChart.stateLastUpdate = null;
  appState.expandedChart.pinnedBarId = null;
  appState.expandedChart.isLoading = false;
  pruneExpandedChartCache();
}

function compactChartSourceKey(symbol, maxBars, mode = compactChartTimeframeMode(appState.data)) {
  const normalizedSymbol = String(symbol || '').toUpperCase();
  const timeframeMode = normalizedExpandedChartTimeframeMode(mode);
  if (timeframeMode === 'htf') return `${normalizedSymbol}|${Math.max(1, Number(maxBars) || 90)}|${timeframeMode}`;
  const snapshot = activeSnapshotMap(appState.data).get(normalizedSymbol) || null;
  const baseBars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
  const lastBarTs = baseBars.length ? safe(baseBars[baseBars.length - 1]?.ts) : safe(appState.data?.last_update);
  return `${normalizedSymbol}|${Math.max(1, Number(maxBars) || 90)}|${timeframeMode}|${lastBarTs}`;
}

function writeCompactChartCache(sourceKey, cacheEntry) {
  if (!sourceKey || !cacheEntry || !Array.isArray(cacheEntry.bars) || !cacheEntry.bars.length) return;
  const symbol = String(cacheEntry.symbol || '').toUpperCase();
  const maxBars = Math.max(1, Number(cacheEntry.maxBars) || 90);
  appState.compactChart.cache.set(sourceKey, {
    ...cacheEntry,
    symbol,
    maxBars,
    updatedAt: Number(cacheEntry.updatedAt) || Date.now(),
  });
  pruneCompactChartCache({ preserveSourceKey: sourceKey });
}

function pruneCompactChartCache({ preserveSourceKey = null } = {}) {
  const cache = appState.compactChart.cache;
  if (!(cache instanceof Map) || !cache.size) return;
  const now = Date.now();
  const preserved = new Set([preserveSourceKey, appState.compactChart.sourceKey].filter(Boolean));
  const latestByPrefix = new Map();
  for (const [key, entry] of cache.entries()) {
    if (!entry || !Array.isArray(entry.bars) || !entry.bars.length) continue;
    const updatedAt = Number(entry.updatedAt) || now;
    if (!preserved.has(key) && (now - updatedAt) > COMPACT_CHART_CACHE_TTL_MS) continue;
    const symbol = String(entry.symbol || '').toUpperCase();
    const maxBars = Math.max(1, Number(entry.maxBars) || Number(entry.bars?.length) || 90);
    const timeframeMode = normalizedExpandedChartTimeframeMode(entry.timeframeMode);
    const prefix = `${symbol}|${maxBars}|${timeframeMode}`;
    const current = latestByPrefix.get(prefix);
    const shouldReplace = !current || preserved.has(key) || (!preserved.has(current[0]) && updatedAt >= (Number(current[1]?.updatedAt) || 0));
    if (shouldReplace) latestByPrefix.set(prefix, [key, { ...entry, symbol, maxBars, updatedAt }]);
  }
  let entries = Array.from(latestByPrefix.values());
  entries.sort((left, right) => (Number(right[1]?.updatedAt) || 0) - (Number(left[1]?.updatedAt) || 0));
  if (entries.length > COMPACT_CHART_CACHE_MAX) {
    const keepKeys = new Set(entries.slice(0, COMPACT_CHART_CACHE_MAX).map(([key]) => key));
    preserved.forEach((key) => {
      if (!keepKeys.has(key) && cache.has(key)) {
        const entry = cache.get(key);
        if (entry && Array.isArray(entry.bars) && entry.bars.length) {
          keepKeys.add(key);
          entries.push([key, entry]);
        }
      }
    });
    entries = entries.filter(([key]) => keepKeys.has(key));
  }
  const nextCache = new Map();
  entries
    .sort((left, right) => (Number(left[1]?.updatedAt) || 0) - (Number(right[1]?.updatedAt) || 0))
    .forEach(([key, entry]) => nextCache.set(key, entry));
  appState.compactChart.cache = nextCache;
}

function remoteChartCacheNeedsRefresh(entry, data = appState.data, timeframeMode = '1m') {
  if (!entry || !Array.isArray(entry.bars) || !entry.bars.length) return true;
  const normalizedMode = normalizedExpandedChartTimeframeMode(timeframeMode);
  if (normalizedMode !== 'htf') return false;
  const currentToken = currentHtfRefreshToken(data, entry?.symbol || appState.selectedSymbol);
  const entryToken = String(entry?.htfRefreshToken || '').trim();
  if (currentToken || entryToken) return currentToken !== entryToken;
  return false;
}

async function ensureCompactChartBars(force = false) {
  if (appState.mainPanelExpanded || !appState.selectedSymbol) return null;
  const timeframeMode = compactChartTimeframeMode(appState.data);
  const symbol = String(appState.selectedSymbol || '').toUpperCase();
  const maxBars = compactChartMaxBars(appState.data);
  const sourceKey = compactChartSourceKey(symbol, maxBars, timeframeMode);
  const cached = appState.compactChart.cache.get(sourceKey) || null;
  if (!force
    && appState.compactChart.symbol === symbol
    && appState.compactChart.sourceKey === sourceKey
    && Array.isArray(appState.compactChart.bars)
    && appState.compactChart.bars.length
    && !remoteChartCacheNeedsRefresh(appState.compactChart, appState.data, timeframeMode)) {
    appState.compactChart.isLoading = false;
    return appState.compactChart.bars;
  }
  if (!force && cached && !remoteChartCacheNeedsRefresh(cached, appState.data, timeframeMode)) {
    appState.compactChart.symbol = symbol;
    appState.compactChart.bars = cached.bars;
    appState.compactChart.timeframeMode = timeframeMode;
    appState.compactChart.sourceKey = sourceKey;
    appState.compactChart.lastBarTs = cached.lastBarTs;
    appState.compactChart.timeframeLabel = cached.timeframeLabel || expandedChartTimeframeLabel(appState.data, timeframeMode);
    appState.compactChart.htfRefreshToken = cached.htfRefreshToken || null;
    appState.compactChart.patterns = cached.patterns || {};
    appState.compactChart.structureOverlay = cached.structureOverlay || {};
    appState.compactChart.stateLastUpdate = cached.stateLastUpdate || safe(appState.data?.last_update);
    appState.compactChart.isLoading = false;
    if (appState.data) renderSelectedSymbol();
    return cached.bars;
  }
  const requestSeq = (appState.compactChart.requestSeq || 0) + 1;
  appState.compactChart.requestSeq = requestSeq;
  appState.compactChart.isLoading = true;
  if (appState.data) renderSelectedSymbol();
  let res;
  try {
    res = await fetch(`/api/chart?symbol=${encodeURIComponent(symbol)}&bars=${encodeURIComponent(maxBars)}&timeframe=${encodeURIComponent(timeframeMode)}&ts=${Date.now()}`, { cache: 'no-store' });
  } catch (err) {
    if (appState.compactChart.requestSeq === requestSeq) appState.compactChart.isLoading = false;
    throw err;
  }
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const payload = await res.json();
  if (appState.mainPanelExpanded || appState.compactChart.requestSeq !== requestSeq || String(appState.selectedSymbol || '').toUpperCase() !== symbol) {
    if (appState.compactChart.requestSeq === requestSeq) appState.compactChart.isLoading = false;
    return null;
  }
  const bars = Array.isArray(payload?.bars) ? payload.bars : [];
  const cacheEntry = {
    bars,
    lastBarTs: safe(payload?.last_bar_ts || (bars.length ? bars[bars.length - 1]?.ts : null)),
    symbol,
    maxBars,
    timeframeMode,
    timeframeLabel: String(payload?.timeframe_label || expandedChartTimeframeLabel(appState.data, timeframeMode)),
    htfRefreshToken: String(payload?.htf_refresh_token || currentHtfRefreshToken(appState.data, symbol) || '').trim() || null,
    patterns: payload?.patterns || {},
    structureOverlay: payload?.structure_overlay || {},
    stateLastUpdate: safe(appState.data?.last_update),
    updatedAt: Date.now(),
  };
  writeCompactChartCache(sourceKey, cacheEntry);
  appState.compactChart.symbol = symbol;
  appState.compactChart.bars = bars;
  appState.compactChart.timeframeMode = timeframeMode;
  appState.compactChart.sourceKey = sourceKey;
  appState.compactChart.lastBarTs = cacheEntry.lastBarTs;
  appState.compactChart.timeframeLabel = cacheEntry.timeframeLabel;
  appState.compactChart.htfRefreshToken = cacheEntry.htfRefreshToken;
  appState.compactChart.patterns = cacheEntry.patterns || {};
  appState.compactChart.structureOverlay = cacheEntry.structureOverlay || {};
  appState.compactChart.stateLastUpdate = cacheEntry.stateLastUpdate;
  appState.compactChart.isLoading = false;
  renderSelectedSymbol();
  return bars;
}

function resetCompactChartCache() {
  appState.compactChart.symbol = null;
  appState.compactChart.bars = null;
  appState.compactChart.sourceKey = null;
  appState.compactChart.lastBarTs = null;
  appState.compactChart.timeframeLabel = '1M';
  appState.compactChart.htfRefreshToken = null;
  appState.compactChart.patterns = {};
  appState.compactChart.structureOverlay = {};
  appState.compactChart.stateLastUpdate = null;
  appState.compactChart.isLoading = false;
  pruneCompactChartCache();
}

async function ensureExpandedChartBars(force = false) {
  if (!appState.mainPanelExpanded || !appState.selectedSymbol) return null;
  const symbol = String(appState.selectedSymbol || '').toUpperCase();
  if (!symbol) return null;
  const maxBars = expandedChartMaxBars(appState.data);
  const timeframeMode = expandedChartTimeframeMode();
  const sourceKey = expandedChartSourceKey(symbol, maxBars, appState.data, timeframeMode);
  if (!force && appState.expandedChart.symbol === symbol && appState.expandedChart.sourceKey === sourceKey && Array.isArray(appState.expandedChart.bars) && appState.expandedChart.bars.length && !remoteChartCacheNeedsRefresh(appState.expandedChart, appState.data, timeframeMode)) {
    appState.expandedChart.isLoading = false;
    return appState.expandedChart.bars;
  }
  if (!force && appState.expandedChart.cache.has(sourceKey)) {
    const cached = appState.expandedChart.cache.get(sourceKey);
    if (!remoteChartCacheNeedsRefresh(cached, appState.data, timeframeMode)) {
      writeExpandedChartCache(sourceKey, { ...cached, symbol, maxBars, timeframeMode, timeframeLabel: expandedChartTimeframeLabel(appState.data, timeframeMode), updatedAt: Date.now() });
      appState.expandedChart.symbol = symbol;
      appState.expandedChart.bars = cached.bars;
      appState.expandedChart.timeframeMode = timeframeMode;
      appState.expandedChart.sourceKey = sourceKey;
      appState.expandedChart.lastBarTs = cached.lastBarTs;
      appState.expandedChart.timeframeLabel = cached.timeframeLabel || expandedChartTimeframeLabel(appState.data, timeframeMode);
      appState.expandedChart.htfRefreshToken = cached.htfRefreshToken || null;
      appState.expandedChart.patterns = cached.patterns || {};
      appState.expandedChart.structureOverlay = cached.structureOverlay || {};
      appState.expandedChart.stateLastUpdate = cached.stateLastUpdate || safe(appState.data?.last_update);
      appState.expandedChart.isLoading = false;
      renderSelectedSymbol();
      return cached.bars;
    }
  }
  const requestSeq = (appState.expandedChart.requestSeq || 0) + 1;
  appState.expandedChart.requestSeq = requestSeq;
  appState.expandedChart.isLoading = true;
  if (appState.data) renderSelectedSymbol();
  let res;
  try {
    res = await fetch(`/api/chart?symbol=${encodeURIComponent(symbol)}&bars=${encodeURIComponent(maxBars)}&timeframe=${encodeURIComponent(timeframeMode)}&ts=${Date.now()}`, { cache: 'no-store' });
  } catch (err) {
    if (appState.expandedChart.requestSeq === requestSeq) appState.expandedChart.isLoading = false;
    throw err;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const payload = await res.json();
  if (!appState.mainPanelExpanded || appState.expandedChart.requestSeq !== requestSeq || String(appState.selectedSymbol || '').toUpperCase() !== symbol) {
    if (appState.expandedChart.requestSeq === requestSeq) appState.expandedChart.isLoading = false;
    return null;
  }
  const bars = Array.isArray(payload?.bars) ? payload.bars : [];
  const cacheEntry = {
    bars,
    lastBarTs: payload?.last_bar_ts || null,
    symbol,
    maxBars,
    timeframeMode,
    timeframeLabel: String(payload?.timeframe_label || expandedChartTimeframeLabel(appState.data, timeframeMode)),
    htfRefreshToken: String(payload?.htf_refresh_token || currentHtfRefreshToken(appState.data, symbol) || '').trim() || null,
    patterns: payload?.patterns || {},
    structureOverlay: payload?.structure_overlay || {},
    stateLastUpdate: safe(appState.data?.last_update),
    updatedAt: Date.now(),
  };
  writeExpandedChartCache(sourceKey, cacheEntry);
  appState.expandedChart.symbol = symbol;
  appState.expandedChart.bars = bars;
  appState.expandedChart.timeframeMode = timeframeMode;
  appState.expandedChart.sourceKey = sourceKey;
  appState.expandedChart.lastBarTs = cacheEntry.lastBarTs;
  appState.expandedChart.timeframeLabel = cacheEntry.timeframeLabel;
  appState.expandedChart.htfRefreshToken = cacheEntry.htfRefreshToken;
  appState.expandedChart.patterns = cacheEntry.patterns || {};
  appState.expandedChart.structureOverlay = cacheEntry.structureOverlay || {};
  appState.expandedChart.stateLastUpdate = cacheEntry.stateLastUpdate;
  appState.expandedChart.isLoading = false;
  renderSelectedSymbol();
  return bars;
}

function scheduleExpandedChartRefresh(force = false) {
  if (!appState.mainPanelExpanded) return;
  appState.expandedChart.pendingForceRefresh = !!appState.expandedChart.pendingForceRefresh || !!force;
  if (appState.expandedChart.refreshRafId) return;
  appState.expandedChart.refreshRafId = window.requestAnimationFrame(() => {
    appState.expandedChart.refreshRafId = 0;
    const nextForce = !!appState.expandedChart.pendingForceRefresh;
    appState.expandedChart.pendingForceRefresh = false;
    ensureExpandedChartBars(nextForce).catch(err => console.warn('Expanded chart fetch failed', err));
  });
}

function chooseDefaultSymbol(data) {
  const candidates = [];
  const positions = data?.performance?.positions || [];
  positions.forEach(row => candidates.push(String(row.underlying || row.symbol || '').toUpperCase()));
  (data?.candidates || []).forEach(row => candidates.push(String(row.symbol || '').toUpperCase()));
  (data?.active_watchlist || []).forEach(sym => candidates.push(String(sym || '').toUpperCase()));
  (data?.dashboard_symbols || []).forEach(row => candidates.push(String(row.symbol || '').toUpperCase()));
  return candidates.find(Boolean) || null;
}

function syncSelectedSymbol(data) {
  const map = activeSnapshotMap(data);
  if (appState.selectedSymbol && map.has(appState.selectedSymbol)) return appState.selectedSymbol;
  appState.selectedSymbol = chooseDefaultSymbol(data);
  return appState.selectedSymbol;
}

function setSelectedSymbol(symbol) {
  if (!symbol) return;
  const nextSymbol = String(symbol).toUpperCase();
  if (appState.selectedSymbol === nextSymbol && !appState.mainPanelExpanded) return;
  appState.selectedSymbol = nextSymbol;
  if (appState.expandedChart.symbol && appState.expandedChart.symbol !== nextSymbol) resetExpandedChartCache();
  if (appState.compactChart.symbol && appState.compactChart.symbol !== nextSymbol) resetCompactChartCache();
  if (appState.mainPanelExpanded) {
    appState.expandedChart.isLoading = true;
  } else {
    const timeframeMode = compactChartTimeframeMode(appState.data);
    const maxBars = compactChartMaxBars(appState.data);
    const sourceKey = compactChartSourceKey(nextSymbol, maxBars, timeframeMode);
    const cached = appState.compactChart.cache.get(sourceKey) || null;
    if (cached && !remoteChartCacheNeedsRefresh(cached, appState.data, timeframeMode)) {
      appState.compactChart.symbol = nextSymbol;
      appState.compactChart.bars = Array.isArray(cached.bars) ? cached.bars : null;
      appState.compactChart.timeframeMode = timeframeMode;
      appState.compactChart.sourceKey = sourceKey;
      appState.compactChart.lastBarTs = cached.lastBarTs || null;
      appState.compactChart.timeframeLabel = cached.timeframeLabel || expandedChartTimeframeLabel(appState.data, timeframeMode);
      appState.compactChart.htfRefreshToken = cached.htfRefreshToken || null;
      appState.compactChart.patterns = cached.patterns || {};
      appState.compactChart.structureOverlay = cached.structureOverlay || {};
      appState.compactChart.stateLastUpdate = cached.stateLastUpdate || safe(appState.data?.last_update);
      appState.compactChart.isLoading = false;
    } else {
      appState.compactChart.isLoading = true;
    }
  }
  renderApp();
  if (appState.mainPanelExpanded) scheduleExpandedChartRefresh(true);
  else ensureCompactChartBars(true).catch(err => console.warn('Compact chart fetch failed', err));
}

function mainPanelElement() {
  return document.querySelector('.main-panel.chart-panel');
}

function pointInsideRect(pointX, pointY, rect) {
  if (!rect) return false;
  return pointX >= rect.left && pointX <= rect.right && pointY >= rect.top && pointY <= rect.bottom;
}

function isPointerInsideExpandedPanel(event) {
  const panel = mainPanelElement();
  if (!panel || !event) return false;
  return pointInsideRect(event.clientX, event.clientY, panel.getBoundingClientRect());
}

function scheduleMainPanelChartRerender() {
  window.requestAnimationFrame(() => {
    if (appState.data) renderSelectedSymbol();
  });
}

function lockExpandedSidePanelHeights() {
  const rootStyle = document.documentElement.style;
  const watchlist = document.querySelector('.watchlist-panel');
  const candidates = document.querySelector('.candidates-panel');
  const positionsSlot = document.querySelector('.positions-slot');
  const positionsPanel = document.querySelector('.positions-panel');
  if (watchlist) rootStyle.setProperty('--watchlist-locked-h', `${Math.round(watchlist.getBoundingClientRect().height)}px`);
  if (candidates) rootStyle.setProperty('--candidates-locked-h', `${Math.round(candidates.getBoundingClientRect().height)}px`);
  const positionsHeight = positionsPanel?.getBoundingClientRect().height || positionsSlot?.getBoundingClientRect().height || 0;
  if (positionsHeight > 0) rootStyle.setProperty('--positions-locked-h', `${Math.round(positionsHeight)}px`);
}

function unlockExpandedSidePanelHeights() {
  const rootStyle = document.documentElement.style;
  rootStyle.removeProperty('--watchlist-locked-h');
  rootStyle.removeProperty('--candidates-locked-h');
  rootStyle.removeProperty('--positions-locked-h');
}

function setMainPanelExpanded(expanded) {
  const next = Boolean(expanded);
  if (appState.mainPanelExpanded === next) {
    if (next) scheduleExpandedChartRefresh(false);
    else {
      cancelScheduledExpandedChartRefresh();
      pruneExpandedChartCache();
    }
    return;
  }
  if (next) {
    lockExpandedSidePanelHeights();
    appState.expandedChart.timeframeMode = '1m';
    appState.expandedChart.isLoading = true;
    renderChartTimeframeToggle(appState.data);
  } else {
    cancelScheduledExpandedChartRefresh();
    unlockExpandedSidePanelHeights();
  }
  appState.mainPanelExpanded = next;
  document.documentElement.classList.toggle('main-panel-expanded', next);
  window.requestAnimationFrame(() => {
    syncWatchlistViewport();
    syncCandidatesViewport();
    syncPositionsViewport();
    syncDockViewport();
  });
  const tooltip = document.getElementById('chart-tooltip');
  if (tooltip) {
    tooltip.classList.remove('active');
    tooltip.innerHTML = '';
  }
  if (!next) {
    appState.expandedChart.isLoading = false;
    appState.expandedChart.pinnedBarId = null;
    pruneExpandedChartCache();
  }
  if (appState.data) renderSelectedSymbol();
  scheduleMainPanelChartRerender();
  if (next) scheduleExpandedChartRefresh(false);
}

function handleExpandedPanelPointerMove(event) {
  if (!appState.mainPanelExpanded) return;
  if (isPointerInsideExpandedPanel(event)) return;
  setMainPanelExpanded(false);
}

function bindMainPanelHover() {
  const panel = mainPanelElement();
  if (!panel || panel.dataset.expandBound === '1') return;
  panel.dataset.expandBound = '1';
  const chartWrap = panel.querySelector('.chart-wrap');
  if (chartWrap) {
    chartWrap.addEventListener('click', (event) => {
      if (appState.mainPanelExpanded) return;
      event.preventDefault();
      event.stopPropagation();
      appState.expandedChart.pinnedBarId = null;
      setMainPanelExpanded(true);
    });
  }
  window.addEventListener('pointermove', handleExpandedPanelPointerMove, true);
}

function setFilter(filter) {
  appState.filter = filter;
  document.querySelectorAll('.toggle-btn[data-filter]').forEach(btn => btn.classList.toggle('active', btn.dataset.filter === filter));
  renderWatchlist();
}

function activeDockScroller() {
  const page = document.querySelector('.dock-pane.active');
  if (!page) return null;
  return page.querySelector('.dock-scroll, .table-scroll');
}

function wireDockWheel() {
  const dock = document.querySelector('.dock');
  if (dock && dock.dataset.wheelBound !== '1') {
    dock.dataset.wheelBound = '1';
    dock.addEventListener('wheel', (event) => {
      const scroller = activeDockScroller();
      if (!scroller || scroller.scrollHeight <= scroller.clientHeight) return;
      scroller.scrollBy({ top: event.deltaY, left: event.deltaX, behavior: 'auto' });
      event.preventDefault();
    }, { passive: false });
  }
  document.querySelectorAll('.dock .dock-scroll, .dock .table-scroll').forEach(node => {
    if (node.dataset.wheelBound === '1') return;
    node.dataset.wheelBound = '1';
    node.addEventListener('wheel', (event) => {
      if (node.scrollHeight <= node.clientHeight && Math.abs(event.deltaX) < Math.abs(event.deltaY)) return;
      node.scrollBy({ top: event.deltaY, left: event.deltaX, behavior: 'auto' });
      event.preventDefault();
    }, { passive: false });
  });
}

function setDockTab(tab) {
  appState.dockTab = tab;
  document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
  document.querySelectorAll('.dock-pane').forEach(page => page.classList.toggle('active', page.dataset.page === tab));
  wireDockWheel();
}

function scorePct(score) {
  const n = Number(score);
  if (!Number.isFinite(n)) return 0.18;
  const scaled = Math.max(12, Math.min(96, Math.log10(Math.abs(n) + 10) * 32));
  return scaled / 100;
}

function rangePct(price, support, resistance) {
  const p = positiveOrNull(price); const s = positiveOrNull(support); const r = positiveOrNull(resistance);
  if (p === null || s === null || r === null || r <= s) return null;
  return clamp(((p - s) / (r - s)) * 100, 0, 100);
}

function getDashboardNonStreamableSymbols(data) {
  const raw = Array.isArray(data?.data?.non_streamable_symbols) ? data.data.non_streamable_symbols : [];
  return raw.map(sym => String(sym || '').toUpperCase()).filter(Boolean);
}

function getDashboardTradableSymbols(data) {
  const raw = Array.isArray(data?.data?.tradable_symbols) ? data.data.tradable_symbols : [];
  return raw.map(sym => String(sym || '').toUpperCase()).filter(Boolean);
}

function buildWatchlistRows(data) {
  const snapshotMap = buildSnapshotMap(data);
  const activeOrder = (data?.active_watchlist || []).map(sym => String(sym || '').toUpperCase()).filter(Boolean);
  const rows = activeOrder
    .map(sym => snapshotMap.get(sym))
    .filter(Boolean);
  if (appState.filter === 'all') return rows;
  return rows.filter(item => {
    const tone = selectedSymbolTone(item);
    return appState.filter === 'long' ? tone === 'tone-long' : tone === 'tone-short';
  });
}

function renderWatchlist() {
  const data = appState.data;
  const activeSymbols = (data?.active_watchlist || []).map(sym => String(sym || '').toUpperCase()).filter(Boolean);
  const rows = buildWatchlistRows(data);
  document.getElementById('watchlist-meta').textContent = `${activeSymbols.length} symbols`;
  const exchangeMap = data?.symbol_exchanges || {};
  const nonStreamSet = new Set(getDashboardNonStreamableSymbols(data));
  const tradableSet = new Set(getDashboardTradableSymbols(data));
  const html = rows.length ? rows.map(item => {
    const tone = selectedSymbolTone(item);
    const q = item.quote || {};
    const bars = item.bars || [];
    const values = bars.map(bar => numOrNull(bar.close)).filter(v => v !== null);
    const last = q.last ?? (values.length ? values[values.length - 1] : null);
    const change = numOrNull(q.percent_change) ?? numOrNull(item?.candidate?.change_from_open);
    const warmup = item?.warmup || null;
    const lowerBase = item.description || `${safe(item?.candidate?.directional_bias || item?.position?.side || item?.support_resistance?.state || 'watchlist')}`;
    const lower = warmup && !warmup.ready ? `${lowerBase} · ${warmupLabel(warmup)}` : lowerBase;
    const isActive = appState.selectedSymbol === item.symbol;
    const symbolKey = String(item?.symbol || '').toUpperCase();
    const isNonStreamable = nonStreamSet.has(symbolKey);
    const isTradable = tradableSet.has(symbolKey);
    const tradableChip = isTradable ? '<span class="tradeable-chip" title="Tradeable: configured as an eligible entry symbol for this strategy">TR</span>' : '';
    const nonStreamChip = isNonStreamable ? '<span class="ns-chip" title="Non-streamable: included in the active watchlist but not in the Schwab equity stream">NS</span>' : '';
    return `<div class="symbol-card ${tone} ${isActive ? 'active' : ''}" data-symbol="${escapeHtml(item.symbol)}">
      <div class="symbol-top">
        <div>
          <div class="symbol-title-row"><div class="symbol-name">${escapeHtml(item.symbol)}</div>${tradableChip}${nonStreamChip}</div>
          <div class="symbol-sub">${escapeHtml(lower)}</div>
        </div>
        <div class="price-stack">
          <div class="price-main">${fmtNum(last, 2)}</div>
          <div class="price-change ${pnlClass(change)}">${fmtPct(change, 2)}</div>
        </div>
      </div>
      <div class="sparkline-wrap">${sparklineSVG(values, change > 0 ? 'tone-good' : (change < 0 ? 'tone-bad' : 'tone-neutral'))}</div>
      <div class="card-bottom">
        <div class="mini-meta">${symbolLink(item.symbol, exchangeMap, item.symbol, item.exchange)}</div>
        <div style="display:flex; gap:8px; align-items:center;">${iconForState(item?.support_resistance?.trend_state, item?.support_resistance?.trend, 'Trend')}${structureEventChip(item?.support_resistance?.structure_event || '—')}</div>
      </div>
    </div>`;
  }).join('') : `<div class="empty-state">No watchlist symbols are available for the current filter.</div>`;
  const target = document.getElementById('watchlist-list');
  target.innerHTML = html;
  target.querySelectorAll('.symbol-card[data-symbol]').forEach(card => card.addEventListener('click', () => setSelectedSymbol(card.dataset.symbol)));
}

function renderCandidates() {
  const data = appState.data;
  const snapshots = buildSnapshotMap(data);
  const rows = data?.candidates || [];
  document.getElementById('candidate-meta').textContent = `${rows.length} shown · ${safe(data?.candidates?.length || 0)} ranked`;
  const html = rows.length ? rows.map(row => {
    const snap = snapshots.get(String(row.symbol || '').toUpperCase());
    const values = (snap?.bars || []).map(bar => numOrNull(bar.close)).filter(v => v !== null);
    const pct = scorePct(row.activity_score);
    const bias = String(row.directional_bias || '').toUpperCase();
    const tone = bias === 'SHORT' ? 'tone-short' : (bias === 'LONG' ? 'tone-long' : 'tone-neutral');
    const isActive = appState.selectedSymbol === String(row.symbol || '').toUpperCase();
    const warmup = snap?.warmup || null;
    const candidateSub = `Rank #${safe(row.rank)} · ${escapeHtml(row.directional_bias || 'neutral')}${warmup ? ` · ${escapeHtml(warmupLabel(warmup))}` : ''}`;
    const bottomMeta = `Vol ${fmtCompact(row.volume)}`;
    return `<div class="candidate-tile ${tone} ${isActive ? 'active' : ''}" data-symbol="${escapeHtml(row.symbol)}">
      <div class="candidate-top">
        <div>
          <div class="candidate-name">${escapeHtml(row.symbol)}</div>
          <div class="candidate-sub">${candidateSub}</div>
        </div>
        ${iconForState(snap?.support_resistance?.trend_state, snap?.support_resistance?.trend, 'Trend')}
      </div>
      <div class="candidate-score">
        <div>
          <div class="tiny-label">Day % / Price</div>
          <div class="score ${pnlClass(row.change_from_open)}">${fmtPct(row.change_from_open)} · ${fmtNum(row.close, 2)}</div>
        </div>
        <div class="score-ring" style="--pct:${(pct * 100).toFixed(1)}%;"><span>${Math.round(pct * 100)}</span></div>
      </div>
      <div class="sparkline-wrap">${sparklineSVG(values, tone === 'tone-short' ? 'tone-bad' : 'tone-good')}</div>
      <div class="card-bottom"><div class="mini-meta">${bottomMeta}</div>${structureEventChip(snap?.support_resistance?.structure_event || '—')}</div>
    </div>`;
  }).join('') : `<div class="empty-state">No candidate data yet.</div>`;
  const target = document.getElementById('candidate-grid');
  target.innerHTML = html;
  target.querySelectorAll('.candidate-tile[data-symbol]').forEach(card => card.addEventListener('click', () => setSelectedSymbol(card.dataset.symbol)));
}

function renderTopbar(data) {
  const perf = data?.performance || {};
  const rawStatus = String(data?.status || 'idle').toLowerCase();
  const botActive = !!(data?.management_active || data?.screening_active || data?.streaming_active || data?.position_monitoring_active);
  const status = rawStatus === 'running' && !botActive ? 'idle' : rawStatus;
  const statusWrap = document.getElementById('status-badge-wrap');
  const dayPnlEl = document.getElementById('metric-daypnl');
  const pfEl = document.getElementById('metric-pf');
  const netLiqLabelEl = document.getElementById('kpi-netliq-label');
  const tradesEl = document.getElementById('metric-trades');
  const wlEl = document.getElementById('metric-wl');
  const winRateEl = document.getElementById('metric-winrate');
  const apiCpmEl = document.getElementById('metric-api-cpm');
  const updatedEl = document.getElementById('metric-updated');
  const warmupEl = document.getElementById('metric-warmup');
  const sublineEl = document.getElementById('top-subline');
  if (statusWrap) statusWrap.innerHTML = `${statusBadge(status)}${modeBadge(!!data?.dry_run)}`;
  if (netLiqLabelEl) netLiqLabelEl.textContent = safe(data?.tracked_capital_label) || (data?.dry_run ? 'Net Liq' : 'Allocated Capital');
  if (dayPnlEl) dayPnlEl.innerHTML = `<span class="${pnlClass((perf.total_equity || 0) - (perf.starting_equity || 0))}">${fmtMoney((perf.total_equity || 0) - (perf.starting_equity || 0))}</span>`;
  if (pfEl) pfEl.textContent = fmtNum(perf.profit_factor, 2);
  const openPositions = Array.isArray(perf.positions) ? perf.positions.length : (Number(perf.open_positions) || 0);
  const totalTrades = Number(perf.total_trades);
  if (tradesEl) tradesEl.textContent = fmtInteger(Number.isFinite(totalTrades) ? totalTrades : ((Number(perf.closed_trades) || 0) + openPositions));
  if (wlEl) wlEl.textContent = `${fmtInteger(perf.wins ?? 0)} / ${fmtInteger(perf.losses ?? 0)}`;
  if (winRateEl) winRateEl.textContent = perf.win_rate == null ? '—' : fmtPctFromRatio(perf.win_rate);
  if (apiCpmEl) apiCpmEl.textContent = fmtNum(data?.api_usage?.avg_calls_per_minute, 1);
  if (updatedEl) updatedEl.textContent = safe(data?.last_update).replace('T', ' ').slice(0, 19);
  const warmup = data?.warmup || {};
  if (warmupEl) warmupEl.textContent = warmup.total ? `${fmtInteger(warmup.ready_count || 0)} / ${fmtInteger(warmup.total || 0)}` : '—';
  if (sublineEl) {
    const blocked = Number(warmup.blocked_count || 0);
    const warmupText = warmup.total ? `ready ${fmtInteger(warmup.ready_count || 0)}/${fmtInteger(warmup.total || 0)}${blocked ? ` · loading ${fmtInteger(blocked)}` : ''}` : 'ready —';
    sublineEl.textContent = `${safe(data?.strategy)} · ${safe(data?.message)} · ${warmupText} · watchlist ${safe(data?.active_watchlist?.length || 0)}`;
  }
}

function renderKpiAndGauges(data) {
  const perf = data?.performance || {};
  const positions = Array.isArray(perf.positions) ? perf.positions : [];
  const totalEquity = numOrNull(perf.total_equity) || 0;
  const starting = numOrNull(perf.starting_equity) || 0;
  const grossExposure = numOrNull(perf.gross_market_value) ?? positions.reduce((acc, pos) => acc + Math.abs(numOrNull(pos?.market_value) || 0), 0);
  const grossMaxRisk = numOrNull(perf.gross_max_risk) ?? positions.reduce((acc, pos) => acc + Math.abs(numOrNull(pos?.max_risk) || 0), 0);
  const exposurePct = totalEquity > 0 ? clamp((grossExposure / totalEquity) * 100, 0, 100) : 0;
  const riskPct = totalEquity > 0 ? clamp((grossMaxRisk / totalEquity) * 100, 0, 100) : 0;
  const ddPct = totalEquity > 0 ? clamp(((numOrNull(perf.max_drawdown) || numOrNull(perf.drawdown) || 0) / totalEquity) * 100, 0, 100) : 0;
  const dayPnl = totalEquity - starting;

  document.getElementById('kpi-netliq').textContent = fmtMoney(totalEquity);
  document.getElementById('kpi-realized').innerHTML = `<span class="${pnlClass(perf.realized_pnl)}">${fmtMoney(perf.realized_pnl)}</span>`;
  document.getElementById('kpi-unrealized').innerHTML = `<span class="${pnlClass(perf.unrealized_pnl)}">${fmtMoney(perf.unrealized_pnl)}</span>`;
  document.getElementById('kpi-drawdown').textContent = fmtMoney(perf.drawdown);
  document.getElementById('kpi-meta').textContent = `Day PnL ${fmtMoney(dayPnl)} · cash ${fmtMoney(perf.cash)}`;

  const exposureRing = document.getElementById('gauge-exposure-ring');
  const winRing = document.getElementById('gauge-win-ring');
  const ddRing = document.getElementById('gauge-dd-ring');
  exposureRing.style.setProperty('--pct', `${exposurePct}%`);
  winRing.style.setProperty('--pct', `${riskPct}%`);
  ddRing.style.setProperty('--pct', `${ddPct}%`);
  document.getElementById('gauge-exposure-fill').style.width = `${exposurePct}%`;
  document.getElementById('gauge-win-fill').style.width = `${riskPct}%`;
  document.getElementById('gauge-dd-fill').style.width = `${ddPct}%`;
  document.getElementById('gauge-exposure-text').textContent = fmtPctSmart(exposurePct);
  document.getElementById('gauge-win-text').textContent = fmtPctSmart(riskPct);
  document.getElementById('gauge-dd-text').textContent = fmtPctSmart(ddPct);
}

function renderPositions(data) {
  const perf = data?.performance || {};
  const positions = perf.positions || [];
  const exchangeMap = data?.symbol_exchanges || {};
  document.getElementById('positions-meta').textContent = `${positions.length} open positions`;
  const html = positions.length ? positions.map(pos => {
    const baseSymbol = String(pos.underlying || pos.symbol || '').toUpperCase();
    const active = appState.selectedSymbol === baseSymbol;
    const tone = String(pos.side || '').toUpperCase() === 'SHORT' ? 'tone-short' : 'tone-long';
    const rr = numOrNull(pos.max_reward) && numOrNull(pos.max_risk) ? (Number(pos.max_reward) / Math.max(Number(pos.max_risk), 0.0001)) : null;
    const rangeMarkup = positionRangeMarkup(pos);
    return `<div class="position-card ${tone} ${active ? 'active' : ''}" data-symbol="${escapeHtml(baseSymbol)}">
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
  }).join('') : `<div class="empty-state">No open positions right now.</div>`;
  const target = document.getElementById('positions-cards');
  target.innerHTML = html;
  target.querySelectorAll('.position-card[data-symbol]').forEach(card => card.addEventListener('click', () => setSelectedSymbol(card.dataset.symbol)));
}

function latestBar(snapshot) {
  const bars = currentChartBars(snapshot);
  return bars.length ? bars[bars.length - 1] : null;
}

function renderSelectedSymbol() {
  const data = appState.data;
  const map = activeSnapshotMap(data);
  const snapshot = map.get(appState.selectedSymbol) || null;
  const exchangeMap = data?.symbol_exchanges || {};
  if (!snapshot) {
    document.getElementById('selected-symbol').textContent = '—';
    document.getElementById('selected-description').textContent = 'No symbol selected.';
    document.getElementById('selected-trend-icon').innerHTML = iconForState('neutral', null, 'Trend');
    document.getElementById('selected-structure-icon').innerHTML = iconForState('neutral', null, 'Structure');
    document.getElementById('selected-price').className = 'tiny-chip tone-neutral';
    document.getElementById('selected-price').textContent = 'Last —';
    document.getElementById('selected-change').className = 'tiny-chip tone-neutral';
    document.getElementById('selected-change').textContent = 'Change —';
    document.getElementById('selected-spread').classList.add('hidden');
    document.getElementById('selected-volume').textContent = 'Vol —';
    renderChartTimeframeToggle(data);
    drawSelectedChart(null);
    return;
  }

  const q = snapshot.quote || {};
  const sr = snapshot.support_resistance || {};
  const cand = snapshot.candidate || {};
  const pos = snapshot.position || {};
  const bar = latestBar(snapshot) || {};
  const nearestSupport = positiveOrNull(sr.nearest_support);
  const nearestResistance = positiveOrNull(sr.nearest_resistance);
  const supportDistancePct = numOrNull(snapshot?.chart?.levels?.support_distance_pct ?? sr.support_distance_pct);
  const resistanceDistancePct = numOrNull(snapshot?.chart?.levels?.resistance_distance_pct ?? sr.resistance_distance_pct);
  const spread = numOrNull(q.ask) !== null && numOrNull(q.bid) !== null ? Number(q.ask) - Number(q.bid) : null;
  const rgPct = rangePct(q.last ?? bar.close ?? sr.price, nearestSupport, nearestResistance);
  const extension = numOrNull(bar.vwap) !== null && numOrNull(q.last ?? bar.close) !== null && Number(bar.vwap) !== 0
    ? Math.abs(((Number(q.last ?? bar.close) - Number(bar.vwap)) / Number(bar.vwap)) * 100)
    : null;

  document.getElementById('selected-symbol').textContent = snapshot.symbol;
  const decision = entryDecisionForSnapshot(snapshot);
  // Full label (used in scoreSub below) carries the parameter detail;
  // compact label (used in focus-meta next to the symbol name) drops it
  // so long ETF-options skip reasons don't push the live-data chips off
  // the right edge.
  const decisionLabel = entryDecisionLabel(decision);
  const decisionLabelCompact = entryDecisionLabelCompact(decision);
  const warmup = snapshot.warmup || null;
  const selectedDescriptionBase = snapshot.description || `${safe(cand.directional_bias || pos.side || sr.regime_hint || 'watchlist')} · ${safe(data?.strategy)}`;
  const selectedDescription = [selectedDescriptionBase, (!warmup || warmup.ready) ? '' : warmupLabel(warmup), decisionLabelCompact].filter(Boolean).join(' · ');
  document.getElementById('selected-description').textContent = selectedDescription || selectedDescriptionBase;
  document.getElementById('selected-trend-icon').innerHTML = iconForState(sr.trend_state, sr.trend, 'Trend');
  document.getElementById('selected-structure-icon').innerHTML = iconForState(sr.structure_bias, sr.structure_bias, 'Structure');
  const selectedLast = numOrNull(q.last ?? bar.close ?? sr.price);
  const selectedClose = numOrNull(q.close ?? sr.price);
  const selectedNetChange = numOrNull(q.net_change) ?? ((selectedLast !== null && selectedClose !== null) ? (selectedLast - selectedClose) : null);
  document.getElementById('selected-price').className = `tiny-chip ${pnlTone(selectedNetChange)}`;
  document.getElementById('selected-price').textContent = `Last ${fmtNum(selectedLast, 2)}`;
  const selectedChange = numOrNull(q.percent_change) ?? numOrNull(cand.change_from_open);
  document.getElementById('selected-change').className = `tiny-chip ${pnlClass(selectedChange) === 'good' ? 'tone-good' : (pnlClass(selectedChange) === 'bad' ? 'tone-bad' : 'tone-neutral')}`;
  document.getElementById('selected-change').textContent = `Change ${fmtPct(selectedChange)}`;
  // Spread pill stays visible across renders to match the sibling pills
  // (selected-price, selected-change, selected-volume) which always show
  // their `—` placeholder. Toggling `.hidden` between renders caused the
  // pill to flicker on/off whenever ask/bid went transiently stale
  // between stream ticks.
  const spreadEl = document.getElementById('selected-spread');
  spreadEl.classList.remove('hidden');
  spreadEl.textContent = (spread !== null && Number.isFinite(spread) && spread > 0)
    ? `Spread ${fmtNum(spread, 3)}`
    : 'Spread —';
  document.getElementById('selected-volume').textContent = `Vol ${fmtCompact(q.total_volume)}`;
  renderChartTimeframeToggle(data);
  const selectedTvMeta = tradingViewSymbolMeta(snapshot.symbol, exchangeMap, snapshot.exchange);
  document.getElementById('selected-link').href = `https://www.tradingview.com/symbols/${encodeURIComponent(selectedTvMeta.exchange || 'NASDAQ')}-${encodeURIComponent(selectedTvMeta.normalizedSymbol || selectedTvMeta.rawSymbol)}/`;

  const strip = [
    stateChip(sr.state || 'neutral'),
    structureEventChip(sr.structure_event || '—'),
    tinyChip(`Trend ${safe(sr.trend || sr.trend_state || 'neutral')}`, pnlTone(String(sr.trend_state || '').toLowerCase().includes('bear') ? -1 : (String(sr.trend_state || '').toLowerCase().includes('bull') ? 1 : 0))),
    tinyChip(`Structure ${safe(sr.structure_bias || 'neutral')}`, pnlTone(String(sr.structure_bias || '').toLowerCase().includes('bear') ? -1 : (String(sr.structure_bias || '').toLowerCase().includes('bull') ? 1 : 0))),
    tinyChip(`Bias ${safe(cand.directional_bias || pos.side || 'neutral')}`, pnlTone(String(cand.directional_bias || pos.side || '').toUpperCase() === 'SHORT' ? -1 : (String(cand.directional_bias || pos.side || '').toUpperCase() === 'LONG' ? 1 : 0))),
    warmupChip(warmup),
  ].filter(Boolean);
  document.getElementById('selected-structure-strip').innerHTML = strip.join('');

  const levelStrip = [];
  if (nearestSupport !== null) levelStrip.push(`<span class="sr-chip support">S ${fmtNum(nearestSupport, 2)}</span>`);
  if (nearestResistance !== null) levelStrip.push(`<span class="sr-chip resistance">R ${fmtNum(nearestResistance, 2)}</span>`);
  levelStrip.push(tinyChip(`Support Δ ${fmtPctFromRatio(nearestSupport === null ? null : supportDistancePct)}`, 'tone-neutral'));
  levelStrip.push(tinyChip(`Res Δ ${fmtPctFromRatio(nearestResistance === null ? null : resistanceDistancePct)}`, 'tone-neutral'));
  // Intentionally keep the selected-level strip focused on the generic S/R ladder and distance chips.
  // Key-level zones remain visible on the chart itself, but we do not duplicate them here as extra chips.
  if (!levelStrip.length) levelStrip.push(tinyChip('S/R unavailable', 'tone-neutral'));
  document.getElementById('selected-level-strip').innerHTML = levelStrip.join('');

  const scoreLabelEl = document.getElementById('detail-score-label');
  const scoreValue = numOrNull(cand.activity_score);
  const changeValue = numOrNull(q.percent_change);
  let scoreLabel = 'Activity Score';
  let scoreText = '—';
  let scoreBasis = scoreValue;
  const liveVolume = numOrNull(q.total_volume);
  let scoreSub = cand.rank ? `Strategy screener rank #${cand.rank}${liveVolume !== null ? ` · live volume ${fmtCompact(liveVolume)}` : ''}` : `Live quote and structure snapshot`;
  if (decisionLabel) scoreSub = `${scoreSub} · ${decisionLabel}`;
  if (scoreValue !== null) {
    scoreText = fmtNum(scoreValue, 1);
  } else if (changeValue !== null) {
    scoreLabel = 'Day Change';
    scoreText = fmtPct(changeValue, 1);
    scoreBasis = changeValue;
    scoreSub = decisionLabel ? `No candidate activity score available; showing current day change. · ${decisionLabel}` : 'No candidate activity score available; showing current day change.';
  } else {
    scoreLabel = 'Activity Score';
    scoreSub = decisionLabel ? `No candidate activity score available for this symbol. · ${decisionLabel}` : 'No candidate activity score available for this symbol.';
  }
  scoreLabelEl.textContent = scoreLabel;
  document.getElementById('detail-score').textContent = scoreText;
  document.getElementById('detail-score-ring').style.setProperty('--pct', `${(scorePct(scoreBasis) * 100).toFixed(1)}%`);
  document.getElementById('detail-score-ring').innerHTML = `<span>${Math.round(scorePct(scoreBasis) * 100)}</span>`;
  document.getElementById('detail-score-sub').textContent = scoreSub;
  document.getElementById('detail-bias').textContent = safe(cand.directional_bias || pos.side || 'neutral');
  document.getElementById('detail-state').textContent = safe(sr.state || 'neutral').replace(/_/g, ' ');
  document.getElementById('detail-regime').textContent = safe(sr.regime_hint || '—');
  document.getElementById('detail-support').textContent = fmtNum(nearestSupport, 2);
  document.getElementById('detail-resistance').textContent = fmtNum(nearestResistance, 2);
  document.getElementById('detail-vwap').textContent = fmtNum(bar.vwap, 2);
  document.getElementById('detail-bias-score').textContent = fmtNum(sr.bias_score, 2);
  document.getElementById('detail-ema9').textContent = fmtNum(bar.ema9, 2);
  document.getElementById('detail-ema20').textContent = fmtNum(bar.ema20, 2);
  const bidValue = numOrNull(q.bid);
  const askValue = numOrNull(q.ask);
  const midValue = numOrNull(q.mid);
  const markValue = numOrNull(q.mark);
  const lastValue = numOrNull(q.last ?? bar.close ?? sr.price);
  const hasBidAsk = bidValue !== null && askValue !== null && bidValue > 0 && askValue > 0;
  document.getElementById('detail-quote-left-label').textContent = hasBidAsk ? 'Bid' : 'Last';
  document.getElementById('detail-quote-right-label').textContent = hasBidAsk ? 'Ask' : 'Mark';
  document.getElementById('detail-bid').textContent = hasBidAsk ? fmtNum(bidValue, 2) : fmtNum(lastValue, 2);
  document.getElementById('detail-ask').textContent = hasBidAsk ? fmtNum(askValue, 2) : fmtNum(markValue ?? midValue ?? lastValue, 2);
  document.getElementById('detail-range-fill').style.width = `${rgPct === null ? 0 : rgPct}%`;
  document.getElementById('detail-range-text').textContent = rgPct === null ? 'Range position unavailable.' : `Price sits at ${fmtNum(rgPct, 1)}% of the active S/R range.`;
  document.getElementById('detail-momentum-fill').style.width = `${extension === null ? 0 : clamp(extension * 12, 0, 100)}%`;
  document.getElementById('detail-momentum-text').textContent = extension === null ? 'VWAP extension unavailable.' : `VWAP extension ${fmtPct(extension, 2)} · EMA9 ${fmtNum(bar.ema9, 2)} / EMA20 ${fmtNum(bar.ema20, 2)}.`;

  drawSelectedChart(snapshot);
}

function drawSelectedChart(snapshot) {
  const canvas = document.getElementById('market-chart');
  const legend = document.getElementById('chart-legend');
  const tooltip = document.getElementById('chart-tooltip');
  const loadingOverlay = document.getElementById('chart-loading');
  const wrap = canvas?.parentElement || document.getElementById('chart-wrap') || document.querySelector('.chart-wrap') || canvas;
  if (tooltip && tooltip.parentElement !== document.body) {
    document.body.appendChild(tooltip);
  }
  let lastTooltipLeft = null;
  let lastTooltipTop = null;
  // Hoist hover state to the top of drawSelectedChart so renderEmpty
  // (defined below at ~line 1763) can read/write them on early-return
  // paths. Previously declared further down — caused a TDZ
  // ReferenceError when the chart payload had no bars and renderEmpty
  // ran before the original `let` line was reached.
  let hoverFrameRequest = 0;
  let pendingHoverState = null;
  const chartCfg = currentChartProfile(appState.data);
  const isExpandedView = !!appState.mainPanelExpanded;
  const activeViewMode = isExpandedView ? 'expanded' : 'compact';
  const activeConfiguredMaxBars = Math.max(1, Number(chartCfg?.max_bars) || chartMaxBarsForMode(activeViewMode, appState.data));
  const chartTimeframeMode = isExpandedView ? expandedChartTimeframeMode() : compactChartTimeframeMode(appState.data);
  const isOneMinuteChart = chartTimeframeMode === '1m';
  const isHtfChart = chartTimeframeMode === 'htf';
  const show = (key, fallback = true) => {
    if (isOneMinuteChart && (key === 'show_htf_fair_value_gaps' || key === 'show_htf_order_blocks')) {
      return false;
    }
    if (isHtfChart && (
      key === 'show_1m_fair_value_gaps'
      || key === 'show_1m_order_blocks'
      || key === 'show_anchored_vwap'
    )) {
      return false;
    }
    return chartCfg[key] === undefined ? fallback : !!chartCfg[key];
  };
  const ratio = Math.max(window.devicePixelRatio || 1, 1);
  const width = Math.max(640, Math.floor(wrap.clientWidth));
  const height = Math.max(340, Math.floor(wrap.clientHeight || 360));
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  legend.innerHTML = '';

  function setChartLoading(active, message = 'Loading chart…') {
    if (!loadingOverlay) return;
    const textEl = loadingOverlay.querySelector('.chart-loading-text');
    if (textEl) textEl.textContent = message;
    loadingOverlay.classList.toggle('active', !!active);
    loadingOverlay.setAttribute('aria-hidden', active ? 'false' : 'true');
  }

  function hideTooltip() {
    if (!tooltip) return;
    tooltip.classList.remove('active');
    tooltip.innerHTML = '';
    lastTooltipLeft = null;
    lastTooltipTop = null;
  }

  function renderEmpty(message, loading = false) {
    ctx.clearRect(0, 0, width, height);
    setChartLoading(loading, message || 'Loading chart…');
    if (!loading) {
      ctx.fillStyle = '#92a0bf';
      ctx.font = '14px sans-serif';
      ctx.fillText(message, 22, 30);
    }
    // Cancel any pending hover-RAF queued by the previous chart's
    // pointer handler. Without this, the queued RAF fires after we've
    // cleared the canvas and detached handlers, calling the stale
    // closure's paint/updateTooltip and drawing ghost data over the
    // new chart's blank state. Mirrors clearPointerActivity but runs
    // even when the cancel didn't come from a leave gesture.
    if (hoverFrameRequest) {
      window.cancelAnimationFrame(hoverFrameRequest);
      hoverFrameRequest = 0;
    }
    pendingHoverState = null;
    hideTooltip();
    // Detach pointer/mouse handlers so a stale chart's idx mapping
    // doesn't fire while the new chart's bars are still loading.
    // hideTooltip stays wired on leave so dragging off the canvas
    // dismisses any lingering tooltip during the transition.
    canvas.onpointermove = null;
    canvas.onpointerdown = null;
    canvas.onpointerleave = hideTooltip;
    canvas.onpointercancel = hideTooltip;
  }

  function prettyLabel(value) {
    return safe(value).replace(/_/g, ' ').replace(/\\b\\w/g, ch => ch.toUpperCase());
  }

  function lineValueAt(line, absIndex) {
    if (!line || numOrNull(line.slope) === null || numOrNull(line.intercept) === null) return null;
    return Number(line.slope) * Number(absIndex) + Number(line.intercept);
  }

  function nearestIndexForTs(ts, barsInput) {
    if (!ts || !barsInput.length) return null;
    const target = Date.parse(ts);
    if (!Number.isFinite(target)) return null;
    let bestIdx = null;
    let bestGap = Number.POSITIVE_INFINITY;
    barsInput.forEach((bar, idx) => {
      const barTs = Date.parse(bar.ts);
      if (!Number.isFinite(barTs)) return;
      const gap = Math.abs(barTs - target);
      if (gap < bestGap) {
        bestGap = gap;
        bestIdx = idx;
      }
    });
    return bestIdx;
  }

  function uniqPatternList(values) {
    const out = [];
    const seen = new Set();
    (Array.isArray(values) ? values : []).forEach(value => {
      const token = String(value || '').trim();
      if (!token) return;
      const key = token.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      out.push(token);
    });
    return out;
  }

  function tagHtml(values, toneClass = '') {
    const items = uniqPatternList(values).slice(0, 8);
    if (!items.length) return '<span class="tt-tag">—</span>';
    return items.map(item => `<span class="tt-tag ${toneClass}">${escapeHtml(prettyLabel(item))}</span>`).join('');
  }

  function signedBiasToken(value) {
    const token = String(value || '').toLowerCase();
    if (!token) return 0;
    if (token.includes('bull') || token.includes('up') || token.includes('breakout') || token.includes('support_hold')) return 1;
    if (token.includes('bear') || token.includes('down') || token.includes('breakdown') || token.includes('resistance')) return -1;
    return 0;
  }

  function deriveCandleFallback(idx) {
    const start = Math.max(0, Number(idx || 0) - 2);
    const slice = bars.slice(start, Number(idx || 0) + 1);
    let score = 0;
    let bullishCount = 0;
    let bearishCount = 0;
    const bullish = [];
    const bearish = [];
    slice.forEach(item => {
      const open = numOrNull(item.open);
      const high = numOrNull(item.high);
      const low = numOrNull(item.low);
      const close = numOrNull(item.close);
      if ([open, high, low, close].some(v => v === null)) return;
      const range = Math.max(Number(high) - Number(low), 0.000001);
      const bodyScore = (Number(close) - Number(open)) / range;
      score += bodyScore;
      if (bodyScore > 0.15) bullishCount += 1;
      if (bodyScore < -0.15) bearishCount += 1;
    });
    if (bullishCount >= 2) bullish.push('bullish_3bar_flow');
    if (bearishCount >= 2) bearish.push('bearish_3bar_flow');
    if (score >= 0.45) bullish.push('positive_body_bias');
    if (score <= -0.45) bearish.push('negative_body_bias');
    let regime = 'neutral';
    if (score >= 0.60 || (bullishCount >= 2 && score > 0.20)) regime = 'bullish';
    else if (score <= -0.60 || (bearishCount >= 2 && score < -0.20)) regime = 'bearish';
    else if (bullishCount > 0 && bearishCount > 0) regime = 'mixed';
    return { score, regime, bullish, bearish };
  }

  function deriveChartFallback(idx) {
    const bar = bars[idx] || {};
    const close = numOrNull(bar.close);
    const ema9 = numOrNull(bar.ema9);
    const ema20 = numOrNull(bar.ema20);
    const vwap = numOrNull(bar.vwap);
    const ret15 = numOrNull(bar.ret15);
    let score = 0;
    const bullish = [];
    const bearish = [];

    if (close !== null && vwap !== null) {
      if (close >= vwap) {
        score += 0.75;
        bullish.push('above_vwap');
      } else {
        score -= 0.75;
        bearish.push('below_vwap');
      }
    }
    if (ema9 !== null && ema20 !== null) {
      if (ema9 >= ema20) {
        score += 0.90;
        bullish.push('ema_stack_bullish');
      } else {
        score -= 0.90;
        bearish.push('ema_stack_bearish');
      }
    }

    const trendBias = signedBiasToken(bar.trend_state);
    const structureBias = signedBiasToken(bar.structure_bias);
    const dmiBias = signedBiasToken(bar.dmi_bias);
    const obvBias = signedBiasToken(bar.obv_bias);
    const avwapBias = signedBiasToken(bar.anchored_vwap_bias);

    if (trendBias > 0) bullish.push('trend_bullish');
    if (trendBias < 0) bearish.push('trend_bearish');
    if (structureBias > 0) bullish.push('structure_bullish');
    if (structureBias < 0) bearish.push('structure_bearish');
    if (dmiBias > 0) bullish.push('dmi_bullish');
    if (dmiBias < 0) bearish.push('dmi_bearish');
    if (obvBias > 0) bullish.push('obv_bullish');
    if (obvBias < 0) bearish.push('obv_bearish');
    if (avwapBias > 0) bullish.push('avwap_bullish');
    if (avwapBias < 0) bearish.push('avwap_bearish');
    if (bar.breakout_above_resistance) bullish.push('breakout_above_resistance');
    if (bar.breakdown_below_support) bearish.push('breakdown_below_support');
    if (bar.near_support) bullish.push('near_support');
    if (bar.near_resistance) bearish.push('near_resistance');

    score += trendBias * 0.80;
    score += structureBias * 0.70;
    score += dmiBias * 0.50;
    score += obvBias * 0.35;
    score += avwapBias * 0.25;
    if (ret15 !== null) score += Math.max(-0.50, Math.min(0.50, Number(ret15) * 20));

    let regime = 'neutral';
    if (score >= 1.00) regime = 'bullish';
    else if (score <= -1.00) regime = 'bearish';
    else if (bullish.length && bearish.length) regime = 'mixed';
    return { score, regime, bullish, bearish };
  }

  if (!snapshot || !(snapshot.bars || []).length) {
    setChartLoading(false);
    renderEmpty('No recent chart bars available for the selected symbol yet.');
    return;
  }

  const bars = currentChartBars(snapshot)
    .filter(bar => [bar.open, bar.high, bar.low, bar.close].every(v => numOrNull(v) !== null))
    .map((bar, idx) => ({ ...bar, abs_index: numOrNull(bar.abs_index) ?? idx }))
    .slice(-activeConfiguredMaxBars);
  if (!bars.length) {
    const loadingState = chartLoadingState(snapshot);
    renderEmpty(loadingState.active ? loadingState.message : 'Chart bars are present but could not be rendered.', loadingState.active);
    return;
  }
  setChartLoading(false);

  const sr = snapshot.support_resistance || {};
  const chart = snapshot.chart || {};
  const levels = chart.levels || {};
  const technicals = chart.technicals || {};
  const keyLevelZones = Array.isArray(levels.key_level_zones) ? levels.key_level_zones : [];
  const normalizeDashboardFvgs = (gaps, maxPerDirection = 1) => {
    const source = Array.isArray(gaps) ? gaps : [];
    const kept = [];
    const counts = { bullish: 0, bearish: 0 };
    source.forEach(gap => {
      const direction = String(gap?.direction || '').toLowerCase();
      if (direction !== 'bullish' && direction !== 'bearish') return;
      const lower = numOrNull(gap?.lower);
      const upper = numOrNull(gap?.upper);
      const filledPct = numOrNull(gap?.filled_pct);
      if (lower === null || upper === null || upper <= lower) return;
      if (filledPct !== null && filledPct >= 0.90) return;
      if ((counts[direction] || 0) >= maxPerDirection) return;
      counts[direction] = (counts[direction] || 0) + 1;
      kept.push(gap);
    });
    return kept;
  };
  const visibleAbsStart = Number(bars[0].abs_index || 0);
  const visibleAbsEnd = Number(bars[bars.length - 1].abs_index || (bars.length - 1));
  const htfFairValueGaps = isOneMinuteChart ? [] : normalizeDashboardFvgs(levels.htf_fair_value_gaps, 1).filter(gap => {
    const timeframe = String(gap?.timeframe || '').trim().toLowerCase();
    return !!timeframe && timeframe !== '1m';
  });
  const oneMinuteFairValueGaps = isHtfChart ? [] : normalizeDashboardFvgs(levels.one_minute_fair_value_gaps, 1).filter(gap => {
    const timeframe = String(gap?.timeframe || '').trim().toLowerCase();
    if (timeframe !== '1m') return false;
    const anchorAbsIndex = Number(gap?.anchor_abs_index);
    if (Number.isFinite(anchorAbsIndex)) return anchorAbsIndex >= visibleAbsStart && anchorAbsIndex <= visibleAbsEnd;
    const startMillis = Date.parse(gap?.first_seen || '');
    const firstBarMillis = Date.parse(bars[0]?.ts || '');
    const lastBarMillis = Date.parse(bars[bars.length - 1]?.ts || '');
    if (!Number.isFinite(startMillis)) return false;
    if (Number.isFinite(firstBarMillis) && startMillis < firstBarMillis) return false;
    if (Number.isFinite(lastBarMillis) && startMillis > lastBarMillis) return false;
    return true;
  });
  // Order blocks share the FVG payload shape but render with dashed stroke
  // + minimal fill so they're visually distinguishable from FVGs.
  const htfOrderBlocks = isOneMinuteChart ? [] : normalizeDashboardFvgs(levels.htf_order_blocks, 1).filter(ob => {
    const timeframe = String(ob?.timeframe || '').trim().toLowerCase();
    return !!timeframe && timeframe !== '1m';
  });
  const oneMinuteOrderBlocks = isHtfChart ? [] : normalizeDashboardFvgs(levels.one_minute_order_blocks, 1).filter(ob => {
    const timeframe = String(ob?.timeframe || '').trim().toLowerCase();
    if (timeframe !== '1m') return false;
    const anchorAbsIndex = Number(ob?.anchor_abs_index);
    if (Number.isFinite(anchorAbsIndex)) return anchorAbsIndex >= visibleAbsStart && anchorAbsIndex <= visibleAbsEnd;
    const startMillis = Date.parse(ob?.first_seen || '');
    const firstBarMillis = Date.parse(bars[0]?.ts || '');
    const lastBarMillis = Date.parse(bars[bars.length - 1]?.ts || '');
    if (!Number.isFinite(startMillis)) return false;
    if (Number.isFinite(firstBarMillis) && startMillis < firstBarMillis) return false;
    if (Number.isFinite(lastBarMillis) && startMillis > lastBarMillis) return false;
    return true;
  });
  const basePatterns = chart.patterns || {};
  const compactPatterns = (!isExpandedView
    && String(appState.compactChart?.symbol || '').toUpperCase() === String(snapshot?.symbol || '').toUpperCase()
    && normalizedExpandedChartTimeframeMode(appState.compactChart?.timeframeMode) === chartTimeframeMode) ? (appState.compactChart.patterns || {}) : {};
  const expandedPatterns = (isExpandedView && String(appState.expandedChart?.symbol || '').toUpperCase() === String(snapshot?.symbol || '').toUpperCase()) ? (appState.expandedChart.patterns || {}) : {};
  const patterns = Object.keys(expandedPatterns || {}).length ? expandedPatterns : (Object.keys(compactPatterns || {}).length ? compactPatterns : basePatterns);
  const structureOverlayBase = chart.structure_overlay || {};
  const compactStructureOverlay = (!isExpandedView
    && String(appState.compactChart?.symbol || '').toUpperCase() === String(snapshot?.symbol || '').toUpperCase()
    && normalizedExpandedChartTimeframeMode(appState.compactChart?.timeframeMode) === chartTimeframeMode) ? (appState.compactChart.structureOverlay || {}) : {};
  const expandedStructureOverlay = (isExpandedView && String(appState.expandedChart?.symbol || '').toUpperCase() === String(snapshot?.symbol || '').toUpperCase()) ? (appState.expandedChart.structureOverlay || {}) : {};
  const activeStructureOverlay = Object.keys(expandedStructureOverlay || {}).length
    ? expandedStructureOverlay
    : (Object.keys(compactStructureOverlay || {}).length ? compactStructureOverlay : structureOverlayBase);
  const positionMarkers = chart.position_markers || {};
  const recentTrades = Array.isArray(chart.recent_trades) ? chart.recent_trades : [];
  const highs = bars.map(bar => Number(bar.high));
  const lows = bars.map(bar => Number(bar.low));

  const seriesDefs = [];
  if (show('show_moving_averages', true)) {
    seriesDefs.push({ key: 'ema9', label: 'EMA9', color: '#44e7ff', width: 2.0 });
    seriesDefs.push({ key: 'ema20', label: 'EMA20', color: '#7b7dff', width: 2.0 });
  }
  if (show('show_vwap', true)) {
    seriesDefs.push({ key: 'vwap', label: 'VWAP', color: '#ffbf3c', width: 2.0 });
  }
  if (show('show_bollinger_bands', false)) {
    seriesDefs.push({ key: 'bb_upper', label: 'BB Upper', color: '#ff4fa3', width: 1.5 });
    seriesDefs.push({ key: 'bb_mid', label: 'BB Mid', color: '#c98cff', width: 1.2 });
    seriesDefs.push({ key: 'bb_lower', label: 'BB Lower', color: '#24d6b2', width: 1.5 });
  }

  const seriesValueMap = new Map();
  seriesDefs.forEach(def => {
    seriesValueMap.set(def.key, bars.map(bar => numOrNull(bar[def.key])));
  });
  const bbUpperValues = seriesValueMap.get('bb_upper') || [];
  const bbLowerValues = seriesValueMap.get('bb_lower') || [];

  const horizontalLines = [];
  const nearestSupport = positiveOrNull(levels.nearest_support ?? sr.nearest_support);
  const nearestResistance = positiveOrNull(levels.nearest_resistance ?? sr.nearest_resistance);
  const supportDistancePct = numOrNull(levels.support_distance_pct ?? sr.support_distance_pct);
  const resistanceDistancePct = numOrNull(levels.resistance_distance_pct ?? sr.resistance_distance_pct);
  if (show('show_support_resistance', true)) {
    if (nearestSupport !== null) horizontalLines.push({ value: nearestSupport, label: 'Support', shortLabel: 'S', color: '#2fdc8c', dash: [8, 6] });
    if (nearestResistance !== null) horizontalLines.push({ value: nearestResistance, label: 'Resistance', shortLabel: 'R', color: '#ff5269', dash: [8, 6] });
  }
  if (show('show_next_support_resistance', true)) {
    const nextSupport = positiveOrNull(levels.next_support);
    const nextResistance = positiveOrNull(levels.next_resistance);
    if (nextSupport !== null) horizontalLines.push({ value: nextSupport, label: 'Next Support', shortLabel: 'S2', color: 'rgba(13, 192, 143, 0.82)', dash: [4, 6] });
    if (nextResistance !== null) horizontalLines.push({ value: nextResistance, label: 'Next Resistance', shortLabel: 'R2', color: 'rgba(255, 82, 105, 0.82)', dash: [4, 6] });
  }
  if (show('show_full_support_resistance_ladder', false)) {
    (levels.supports || []).forEach(value => {
      const v = positiveOrNull(value);
      if (v !== null && (nearestSupport === null || Math.abs(v - nearestSupport) > 1e-9) && (numOrNull(levels.next_support) === null || Math.abs(v - Number(levels.next_support)) > 1e-9)) {
        horizontalLines.push({ value: v, label: 'Support Ladder', shortLabel: 'SUP', color: 'rgba(18, 178, 124, 0.40)', dash: [2, 6], labelMode: 'inline' });
      }
    });
    (levels.resistances || []).forEach(value => {
      const v = positiveOrNull(value);
      if (v !== null && (nearestResistance === null || Math.abs(v - nearestResistance) > 1e-9) && (numOrNull(levels.next_resistance) === null || Math.abs(v - Number(levels.next_resistance)) > 1e-9)) {
        horizontalLines.push({ value: v, label: 'Resistance Ladder', shortLabel: 'RES', color: 'rgba(255, 82, 105, 0.40)', dash: [2, 6], labelMode: 'inline' });
      }
    });
  }
  if (show('show_anchored_vwap', false)) {
    [
      { value: technicals.anchored_vwap_open, label: 'AVWAP Open', shortLabel: 'AVO', color: '#17c8ff' },
      { value: technicals.anchored_vwap_bullish_impulse, label: 'AVWAP Bull', shortLabel: 'AVB', color: '#47db72' },
      { value: technicals.anchored_vwap_bearish_impulse, label: 'AVWAP Bear', shortLabel: 'AVA', color: '#ff5b9a' },
    ].forEach(item => {
      const v = positiveOrNull(item.value);
      if (v !== null) horizontalLines.push({ value: v, label: item.label, shortLabel: item.shortLabel, color: item.color, dash: [3, 5] });
    });
  }
  if (show('show_fib_extensions', false)) {
    [
      { value: technicals.fib_bullish_1272, label: 'Fib 127.2%↑', shortLabel: '127.2%↑', color: '#a76bff' },
      { value: technicals.fib_bullish_1618, label: 'Fib 161.8%↑', shortLabel: '161.8%↑', color: '#6d47ff' },
      { value: technicals.fib_bearish_1272, label: 'Fib 127.2%↓', shortLabel: '127.2%↓', color: '#ffb000' },
      { value: technicals.fib_bearish_1618, label: 'Fib 161.8%↓', shortLabel: '161.8%↓', color: '#ff7a00' },
    ].forEach(item => {
      const v = positiveOrNull(item.value);
      if (v !== null) horizontalLines.push({ value: v, label: item.label, shortLabel: item.shortLabel, color: item.color, dash: [5, 5], labelMode: 'inline' });
    });
  }
  const diagonalLines = [];
  if (show('show_channel', false) && technicals.channel && technicals.channel.valid) {
    const lowerLine = technicals.channel.lower_line || null;
    const midLine = technicals.channel.mid_line || null;
    const upperLine = technicals.channel.upper_line || null;
    if (lowerLine) diagonalLines.push({ line: lowerLine, label: 'Channel Low', color: '#4a9bff', dash: [], width: 1.7, useChannelRange: true });
    if (midLine) diagonalLines.push({ line: midLine, label: 'Channel Mid', color: 'rgba(74, 155, 255, 0.82)', dash: [5, 6], width: 1.2, useChannelRange: true });
    if (upperLine) diagonalLines.push({ line: upperLine, label: 'Channel High', color: '#4a9bff', dash: [], width: 1.7, useChannelRange: true });
  }
  if (show('show_trendlines', false)) {
    const supportLine = technicals.support_trendline || null;
    const resistanceLine = technicals.resistance_trendline || null;
    if (supportLine) diagonalLines.push({ line: supportLine, label: 'Support TL', color: '#69c779', dash: [10, 4, 2, 4] });
    if (resistanceLine) diagonalLines.push({ line: resistanceLine, label: 'Resistance TL', color: '#d96a78', dash: [10, 4, 2, 4] });
  }

  const markerLines = [];
  if (show('show_trade_markers', true) && positionMarkers) {
    const approxEqual = (left, right) => {
      const a = numOrNull(left);
      const b = numOrNull(right);
      if (a === null || b === null) return false;
      const scale = Math.max(Math.abs(a), Math.abs(b), 1);
      return Math.abs(a - b) <= Math.max(0.0005 * scale, 0.01);
    };
    const pushMarkerLine = (value, label, shortLabel, color, dash) => {
      const numericValue = positiveOrNull(value);
      if (numericValue === null) return;
      const duplicate = markerLines.some(existing => approxEqual(existing.value, numericValue));
      if (duplicate) return;
      markerLines.push({ value: numericValue, label, shortLabel, color, dash });
    };
    const entryValue = positionMarkers.show_underlying_lines ? positiveOrNull(positionMarkers.entry) : null;
    const stopValue = positionMarkers.show_underlying_lines ? positiveOrNull(positionMarkers.stop) : null;
    const targetValue = positionMarkers.show_underlying_lines ? positiveOrNull(positionMarkers.target) : null;
    // Breakeven is in underlying-price units for both stocks and options, so
    // it's drawn regardless of show_underlying_lines (which only gates stock's
    // entry/stop/target that are stock-price units).
    const breakevenValue = positiveOrNull(positionMarkers.breakeven);
    pushMarkerLine(entryValue, 'Entry', 'E', '#8cf4ff', [3, 3]);
    pushMarkerLine(stopValue, 'Stop', 'ST', '#ff365f', [2, 4]);
    pushMarkerLine(targetValue, 'Target', 'TG', '#7eff64', [2, 4]);
    if (!approxEqual(breakevenValue, entryValue)) {
      pushMarkerLine(breakevenValue, 'Breakeven', 'BE', '#ffe268', [1, 5]);
    }
    // Option position strikes — the bot's stop/target are in option-price
    // units and can't be drawn on the underlying chart. Strikes + breakeven
    // give the "profit zone" view that matters for verticals and singles.
    if (String(positionMarkers?.asset_type || '').startsWith('OPTION')) {
      const optType = String(positionMarkers?.option_type || '').toUpperCase();
      const longStrike = positiveOrNull(positionMarkers.long_strike ?? positionMarkers.option_strike);
      const shortStrike = positiveOrNull(positionMarkers.short_strike);
      const longLabel = optType === 'PUT' ? 'Long Put Strike' : (optType === 'CALL' ? 'Long Call Strike' : 'Long Strike');
      const shortLabel = optType === 'PUT' ? 'Short Put Strike' : (optType === 'CALL' ? 'Short Call Strike' : 'Short Strike');
      pushMarkerLine(longStrike, longLabel, 'LK', '#44e7ff', [6, 4]);
      pushMarkerLine(shortStrike, shortLabel, 'SK', '#ff7a00', [6, 4]);
    }
  }

  const coreRangeValues = [];
  bars.forEach(bar => {
    coreRangeValues.push(Number(bar.high), Number(bar.low));
    seriesDefs.forEach(def => {
      const v = numOrNull(bar[def.key]);
      if (v !== null) coreRangeValues.push(v);
    });
  });
  const lastClose = Number(bars[bars.length - 1].close);
  const coreMaxY = Math.max(...coreRangeValues);
  const coreMinY = Math.min(...coreRangeValues);
  const coreSpan = Math.max(coreMaxY - coreMinY, Math.max(Math.abs(lastClose) * 0.0025, 0.01));
  const overlayVisibilityPad = Math.max(coreSpan * 0.75, Math.abs(lastClose) * 0.0035, 0.05);
  const overlayMinY = coreMinY - overlayVisibilityPad;
  const overlayMaxY = coreMaxY + overlayVisibilityPad;

  function isValueInFocus(value) {
    const v = numOrNull(value);
    return v !== null && Number(v) >= overlayMinY && Number(v) <= overlayMaxY;
  }

  function isZoneInFocus(lowerValue, upperValue) {
    const lower = numOrNull(lowerValue);
    const upper = numOrNull(upperValue);
    if (lower === null || upper === null) return false;
    const lo = Math.min(lower, upper);
    const hi = Math.max(lower, upper);
    return hi >= overlayMinY && lo <= overlayMaxY;
  }

  function isDiagonalLineInFocus(line) {
    if (!line) return false;
    const startValue = lineValueAt(line, visibleAbsStart);
    const endValue = lineValueAt(line, visibleAbsEnd);
    if (numOrNull(startValue) === null || numOrNull(endValue) === null) return false;
    const lo = Math.min(Number(startValue), Number(endValue));
    const hi = Math.max(Number(startValue), Number(endValue));
    return hi >= overlayMinY && lo <= overlayMaxY;
  }

  function firstIndexAtOrAfterAbs(absIndex) {
    const target = numOrNull(absIndex);
    if (target === null) return null;
    for (let idx = 0; idx < bars.length; idx += 1) {
      const abs = numOrNull(bars[idx]?.abs_index);
      if (abs !== null && Number(abs) >= Number(target)) return idx;
    }
    return null;
  }

  function lastIndexAtOrBeforeAbs(absIndex) {
    const target = numOrNull(absIndex);
    if (target === null) return null;
    for (let idx = bars.length - 1; idx >= 0; idx -= 1) {
      const abs = numOrNull(bars[idx]?.abs_index);
      if (abs !== null && Number(abs) <= Number(target)) return idx;
    }
    return null;
  }

  function resolveChannelRenderRange(channelCtx) {
    const upperLine = channelCtx?.upper_line || null;
    const lowerLine = channelCtx?.lower_line || null;
    if (!upperLine || !lowerLine || !bars.length) return null;
    const startCandidates = [numOrNull(upperLine.start_pos), numOrNull(lowerLine.start_pos), visibleAbsStart]
      .filter(value => value !== null)
      .map(value => Number(value));
    const endCandidates = [numOrNull(upperLine.end_pos), numOrNull(lowerLine.end_pos), visibleAbsEnd]
      .filter(value => value !== null)
      .map(value => Number(value));
    if (!startCandidates.length || !endCandidates.length) return null;
    const startAbs = Math.max(...startCandidates);
    let endAbs = Math.min(...endCandidates);
    if (!Number.isFinite(startAbs) || !Number.isFinite(endAbs) || endAbs < startAbs) return null;
    for (let idx = 0; idx < bars.length; idx += 1) {
      const bar = bars[idx];
      const abs = numOrNull(bar?.abs_index);
      if (abs === null) continue;
      const absNum = Number(abs);
      if (absNum < startAbs || absNum > visibleAbsEnd) continue;
      const upperValue = lineValueAt(upperLine, absNum);
      const lowerValue = lineValueAt(lowerLine, absNum);
      const closeValue = numOrNull(bar?.close);
      if (numOrNull(upperValue) === null || numOrNull(lowerValue) === null || closeValue === null) continue;
      const upperBound = Math.max(Number(upperValue), Number(lowerValue));
      const lowerBound = Math.min(Number(upperValue), Number(lowerValue));
      if (Number(closeValue) > upperBound || Number(closeValue) < lowerBound) {
        endAbs = Math.min(endAbs, absNum);
        break;
      }
    }
    const startIdx = firstIndexAtOrAfterAbs(startAbs);
    const endIdx = lastIndexAtOrBeforeAbs(endAbs);
    if (startIdx === null || endIdx === null || endIdx < startIdx) return null;
    return { startAbs, endAbs, startIdx, endIdx };
  }

  const visibleHorizontalLines = horizontalLines.filter(line => isValueInFocus(line?.value));
  const visibleMarkerLines = markerLines.filter(line => isValueInFocus(line?.value));
  const visibleKeyLevelZones = keyLevelZones.filter(zone => isZoneInFocus(zone?.lower, zone?.upper));
  const visibleDiagonalLines = diagonalLines.filter(item => isDiagonalLineInFocus(item?.line));
  const keyLevelFocusPrice = numOrNull(snapshot?.quote?.last ?? bars[bars.length - 1]?.close ?? snapshot?.support_resistance?.price);
  const prioritizedVisibleKeyLevelZones = visibleKeyLevelZones.slice().sort((a, b) => {
    const selectedDelta = Number(!!b?.selected_for_entry) - Number(!!a?.selected_for_entry);
    if (selectedDelta !== 0) return selectedDelta;
    const passingDelta = Number(!!b?.passes_min_level_score) - Number(!!a?.passes_min_level_score);
    if (passingDelta !== 0) return passingDelta;
    const aPrice = numOrNull(a?.price);
    const bPrice = numOrNull(b?.price);
    const aDist = keyLevelFocusPrice === null || aPrice === null ? Number.POSITIVE_INFINITY : Math.abs(Number(aPrice) - Number(keyLevelFocusPrice));
    const bDist = keyLevelFocusPrice === null || bPrice === null ? Number.POSITIVE_INFINITY : Math.abs(Number(bPrice) - Number(keyLevelFocusPrice));
    if (aDist !== bDist) return aDist - bDist;
    return Number(aPrice ?? 0) - Number(bPrice ?? 0);
  });
  const channelUpperLine = technicals.channel?.upper_line || null;
  const channelLowerLine = technicals.channel?.lower_line || null;
  const channelRenderRange = resolveChannelRenderRange(technicals.channel);
  const channelUpperValues = channelUpperLine && channelRenderRange
    ? bars.map((bar, idx) => (idx >= channelRenderRange.startIdx && idx <= channelRenderRange.endIdx)
      ? lineValueAt(channelUpperLine, Number(bar.abs_index))
      : null)
    : [];
  const channelLowerValues = channelLowerLine && channelRenderRange
    ? bars.map((bar, idx) => (idx >= channelRenderRange.startIdx && idx <= channelRenderRange.endIdx)
      ? lineValueAt(channelLowerLine, Number(bar.abs_index))
      : null)
    : [];

  const rangeValues = [...coreRangeValues];
  visibleHorizontalLines.forEach(line => { if (numOrNull(line.value) !== null) rangeValues.push(Number(line.value)); });
  visibleMarkerLines.forEach(line => { if (numOrNull(line.value) !== null) rangeValues.push(Number(line.value)); });
  visibleKeyLevelZones.forEach(zone => {
    const lower = numOrNull(zone?.lower);
    const upper = numOrNull(zone?.upper);
    if (lower !== null) rangeValues.push(Number(lower));
    if (upper !== null) rangeValues.push(Number(upper));
  });
  visibleDiagonalLines.forEach(item => {
    const startValue = lineValueAt(item.line, visibleAbsStart);
    const endValue = lineValueAt(item.line, visibleAbsEnd);
    if (numOrNull(startValue) !== null) rangeValues.push(startValue);
    if (numOrNull(endValue) !== null) rangeValues.push(endValue);
  });
  const maxY = Math.max(...rangeValues);
  const minY = Math.min(...rangeValues);
  const span = Math.max(maxY - minY, 0.01);
  const pad = { left: 62, right: 84, top: 26, bottom: 48 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const slotW = plotW / Math.max(bars.length, 1);
  const rawVolumes = bars
    .map(bar => Number(bar.volume || 0))
    .filter(value => Number.isFinite(value) && value > 0)
    .sort((a, b) => a - b);
  const volumeMax = Math.max(...rawVolumes, 1);
  const volumeRenderMax = Math.max(volumeMax, 1);
  const candleW = Math.max(4, slotW * 0.58);

  function yFor(value) {
    return pad.top + (1 - ((Number(value) - minY) / span)) * plotH;
  }

  function xFor(idx) {
    return pad.left + (idx + 0.5) * slotW;
  }

  function tooltipAnchorForIndex(idx) {
    if (idx === null || idx < 0 || idx >= bars.length) return null;
    const rect = canvas.getBoundingClientRect();
    return {
      clientX: rect.left + xFor(idx),
      clientY: rect.top + clamp(yFor(bars[idx].close), pad.top + 12, height - pad.bottom - 12),
    };
  }
  const tooltipHtmlCache = new Map();
  // hoverFrameRequest / pendingHoverState are hoisted to the top of
  // drawSelectedChart (around line ~1721) so renderEmpty can access
  // them on early-return paths without hitting the TDZ.
  let pinnedIndex = -1;

  function formatTimeAxisLabel(value) {
    if (!value) return '';
    const dt = new Date(value);
    if (!Number.isFinite(dt.getTime())) return '';
    return TIME_AXIS_FMT.format(dt);
  }

  function formatDayAxisLabel(value) {
    if (!value) return '';
    const dt = new Date(value);
    if (!Number.isFinite(dt.getTime())) return '';
    return DAY_AXIS_FMT.format(dt);
  }

  function inferredBarMinutes() {
    if (bars.length < 2) return 1;
    const deltas = [];
    for (let i = 1; i < Math.min(bars.length, 12); i += 1) {
      const prev = Date.parse(bars[i - 1]?.ts);
      const curr = Date.parse(bars[i]?.ts);
      if (!Number.isFinite(prev) || !Number.isFinite(curr)) continue;
      const diffMinutes = Math.round((curr - prev) / 60000);
      if (diffMinutes > 0) deltas.push(diffMinutes);
    }
    if (!deltas.length) return 1;
    deltas.sort((a, b) => a - b);
    return Math.max(1, deltas[Math.floor(deltas.length / 2)] || deltas[0] || 1);
  }

  function timeAxisStepBars() {
    const barMinutes = inferredBarMinutes();
    const totalMinutes = Math.max(barMinutes * Math.max(bars.length - 1, 1), barMinutes);
    const targetMinutes = Math.max(barMinutes, totalMinutes / 6);
    const niceMinuteSteps = [5, 10, 15, 30, 60, 120, 240, 390];
    const stepMinutes = niceMinuteSteps.find(value => value >= targetMinutes)
      || (Math.ceil(targetMinutes / barMinutes) * barMinutes);
    return Math.max(1, Math.round(stepMinutes / barMinutes));
  }

  const timeAxisTickData = (() => {
    const stepBars = timeAxisStepBars();
    const lastIdx = bars.length - 1;
    const anchorOffset = stepBars > 0 ? (lastIdx % stepBars) : 0;
    const tickIndexes = new Set([0, lastIdx]);
    for (let idx = anchorOffset; idx <= lastIdx; idx += stepBars) tickIndexes.add(idx);
    let lastX = Number.NEGATIVE_INFINITY;
    const ticks = [];
    Array.from(tickIndexes).sort((a, b) => a - b).forEach(idx => {
      if (idx < 0 || idx >= bars.length) return;
      const label = formatTimeAxisLabel(bars[idx]?.ts);
      if (!label) return;
      const x = xFor(idx);
      if ((x - lastX) < 38) return;
      ticks.push({ idx, label, x });
      lastX = x;
    });
    return ticks;
  })();

  const dayAxisLabelData = (() => {
    const labels = [];
    const seen = new Set();
    for (let idx = 1; idx < bars.length; idx += 1) {
      const prev = new Date(bars[idx - 1]?.ts || '');
      const curr = new Date(bars[idx]?.ts || '');
      if (!Number.isFinite(prev.getTime()) || !Number.isFinite(curr.getTime())) continue;
      // Day-boundary keys must use the same timezone as the labels —
      // DAY_AXIS_FMT renders ET, so a browser-local prev.getDate() vs
      // curr.getDate() comparison would put the rollover marker at the
      // wrong x for any viewer outside ET. DAY_KEY_FMT yields ISO
      // YYYY-MM-DD via the en-CA locale, perfect for string equality.
      const prevKey = DAY_KEY_FMT.format(prev);
      const currKey = DAY_KEY_FMT.format(curr);
      if (prevKey === currKey || seen.has(currKey)) continue;
      const label = formatDayAxisLabel(bars[idx]?.ts);
      if (!label) continue;
      labels.push({ idx, label, x: (xFor(idx - 1) + xFor(idx)) / 2 });
      seen.add(currKey);
    }
    return labels;
  })();

  function drawShadedBand(upperValues, lowerValues, fillStyle) {
    if (!Array.isArray(upperValues) || !Array.isArray(lowerValues)) return;
    const points = [];
    upperValues.forEach((value, idx) => {
      if (numOrNull(value) === null || numOrNull(lowerValues[idx]) === null) return;
      points.push({ idx, upper: Number(value), lower: Number(lowerValues[idx]) });
    });
    if (points.length < 2) return;
    ctx.save();
    ctx.fillStyle = fillStyle;
    ctx.beginPath();
    ctx.moveTo(xFor(points[0].idx), yFor(points[0].upper));
    for (let i = 1; i < points.length; i += 1) ctx.lineTo(xFor(points[i].idx), yFor(points[i].upper));
    for (let i = points.length - 1; i >= 0; i -= 1) ctx.lineTo(xFor(points[i].idx), yFor(points[i].lower));
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  function drawHorizontalZone(upperValue, lowerValue, fillStyle, strokeStyle = null, lineWidth = 1) {
    if (numOrNull(upperValue) === null || numOrNull(lowerValue) === null) return;
    const clampedUpper = clamp(Math.max(upperValue, lowerValue), minY, maxY);
    const clampedLower = clamp(Math.min(upperValue, lowerValue), minY, maxY);
    const topY = yFor(clampedUpper);
    const bottomY = yFor(clampedLower);
    const zoneHeight = bottomY - topY;
    const zoneWidth = width - pad.left - pad.right;
    ctx.save();
    ctx.fillStyle = fillStyle;
    ctx.fillRect(pad.left, topY, zoneWidth, zoneHeight);
    if (strokeStyle) {
      ctx.strokeStyle = strokeStyle;
      ctx.lineWidth = lineWidth;
      ctx.strokeRect(pad.left + 0.5, topY + 0.5, Math.max(zoneWidth - 1, 0), Math.max(zoneHeight - 1, 0));
    }
    ctx.restore();
  }

  function drawTimedZone(startIdx, endIdx, upperValue, lowerValue, fillStyle) {
    if (numOrNull(upperValue) === null || numOrNull(lowerValue) === null) return;
    const start = Math.max(0, Math.min(bars.length - 1, Number(startIdx)));
    const end = Math.max(start, Math.min(bars.length - 1, Number(endIdx)));
    if (!Number.isFinite(start) || !Number.isFinite(end)) return;
    const clampedUpper = clamp(Math.max(upperValue, lowerValue), minY, maxY);
    const clampedLower = clamp(Math.min(upperValue, lowerValue), minY, maxY);
    const topY = yFor(clampedUpper);
    const bottomY = yFor(clampedLower);
    const leftX = xFor(start) - (slotW / 2);
    const rightX = xFor(end) + (slotW / 2);
    const zoneLeft = clamp(leftX, pad.left, width - pad.right);
    const zoneRight = clamp(rightX, pad.left, width - pad.right);
    if (zoneRight <= zoneLeft) return;
    ctx.save();
    ctx.fillStyle = fillStyle;
    ctx.fillRect(zoneLeft, topY, zoneRight - zoneLeft, bottomY - topY);
    ctx.restore();
  }

  // Order-block rendering helper. Visually distinct from `drawTimedZone`
  // (which is solid filled) — OBs render with a dashed-line border around
  // a very faint fill so the two zone types (FVG vs OB) don't collapse
  // into a single visual at a glance.
  function drawTimedDashedZone(startIdx, endIdx, upperValue, lowerValue, fillStyle, strokeStyle, lineWidth = 1.2, dashPattern = [5, 4]) {
    if (numOrNull(upperValue) === null || numOrNull(lowerValue) === null) return;
    const start = Math.max(0, Math.min(bars.length - 1, Number(startIdx)));
    const end = Math.max(start, Math.min(bars.length - 1, Number(endIdx)));
    if (!Number.isFinite(start) || !Number.isFinite(end)) return;
    const clampedUpper = clamp(Math.max(upperValue, lowerValue), minY, maxY);
    const clampedLower = clamp(Math.min(upperValue, lowerValue), minY, maxY);
    const topY = yFor(clampedUpper);
    const bottomY = yFor(clampedLower);
    const leftX = xFor(start) - (slotW / 2);
    const rightX = xFor(end) + (slotW / 2);
    const zoneLeft = clamp(leftX, pad.left, width - pad.right);
    const zoneRight = clamp(rightX, pad.left, width - pad.right);
    if (zoneRight <= zoneLeft) return;
    ctx.save();
    if (fillStyle) {
      ctx.fillStyle = fillStyle;
      ctx.fillRect(zoneLeft, topY, zoneRight - zoneLeft, bottomY - topY);
    }
    if (strokeStyle) {
      ctx.strokeStyle = strokeStyle;
      ctx.lineWidth = lineWidth;
      ctx.setLineDash(dashPattern);
      ctx.strokeRect(zoneLeft, topY, zoneRight - zoneLeft, bottomY - topY);
      ctx.setLineDash([]);
    }
    ctx.restore();
  }

  function resolveTimedZoneRange(startTs, endTs = null, spanBars = 15, maxSpanBars = null, options = {}) {
    const span = Math.max(1, Number(spanBars) || 15);
    const maxSpan = Math.max(1, Number(maxSpanBars) || span);
    const requireVisibleStart = !!options?.requireVisibleStart;
    const startMillis = Date.parse(startTs || '');
    if (!Number.isFinite(startMillis)) return null;
    const firstBarMillis = Date.parse(bars[0]?.ts || '');
    const lastBarMillis = Date.parse(bars[bars.length - 1]?.ts || '');
    if (requireVisibleStart && Number.isFinite(firstBarMillis) && startMillis < firstBarMillis) return null;
    if (Number.isFinite(lastBarMillis) && startMillis > lastBarMillis) return null;
    let startIdx = -1;
    for (let i = 0; i < bars.length; i += 1) {
      const barTs = Date.parse(bars[i]?.ts || '');
      if (!Number.isFinite(barTs)) continue;
      if (barTs >= startMillis) {
        startIdx = i;
        break;
      }
    }
    if (startIdx < 0) return null;
    const endMillis = Date.parse(endTs || '');
    if (Number.isFinite(endMillis) && endMillis >= startMillis) {
      let endIdx = bars.length - 1;
      for (let i = startIdx; i < bars.length; i += 1) {
        const barTs = Date.parse(bars[i]?.ts || '');
        if (!Number.isFinite(barTs)) continue;
        if (barTs >= endMillis) {
          endIdx = i;
          break;
        }
      }
      endIdx = Math.min(endIdx, startIdx + maxSpan - 1);
      return { startIdx, endIdx: Math.max(startIdx, endIdx) };
    }
    const endIdx = Math.min(bars.length - 1, startIdx + span - 1);
    return { startIdx, endIdx };
  }

  function resolveIndexedZoneRange(anchorAbsIndex, spanBars = 15, maxSpanBars = null) {
    const anchor = Number(anchorAbsIndex);
    if (!Number.isFinite(anchor)) return null;
    if (anchor < visibleAbsStart || anchor > visibleAbsEnd) return null;
    const span = Math.max(1, Number(spanBars) || 15);
    const maxSpan = Math.max(1, Number(maxSpanBars) || span);
    let startIdx = bars.findIndex(bar => Number(bar?.abs_index) >= anchor);
    if (startIdx < 0) return null;
    let endIdx = Math.min(bars.length - 1, startIdx + span - 1);
    const boundedMax = Math.min(bars.length - 1, startIdx + maxSpan - 1);
    endIdx = Math.min(endIdx, boundedMax);
    if (endIdx < startIdx) return null;
    return { startIdx, endIdx };
  }

  let pendingHorizontalLabels = [];

  function drawHorizontalLabel(text, value, color) {
    if (numOrNull(value) === null) return;
    const y = yFor(value);
    const labelText = `${text} ${fmtNum(value, 2)}`;
    ctx.save();
    ctx.font = '11px sans-serif';
    const measured = ctx.measureText(labelText).width;
    ctx.restore();
    const boxX = width - pad.right + 6;
    const boxW = clamp(Math.ceil(measured) + 10, 52, 128);
    const boxH = 20;
    const desiredBoxY = clamp(Math.round(y - (boxH / 2)), pad.top, height - pad.bottom - boxH);
    pendingHorizontalLabels.push({
      labelText,
      color,
      value: Number(value),
      targetY: clamp(Number(y), pad.top + (boxH / 2), height - pad.bottom - (boxH / 2)),
      desiredBoxY,
      boxX,
      boxW,
      boxH,
    });
  }
  function renderHorizontalLabels() {
    if (!pendingHorizontalLabels.length) return;
    const topLimit = pad.top;
    const bottomLimit = height - pad.bottom;
    const minGap = 4;
    const labels = pendingHorizontalLabels
      .slice()
      .sort((a, b) => (a.targetY - b.targetY) || String(a.labelText).localeCompare(String(b.labelText)));

    labels.forEach((label, idx) => {
      const maxBoxY = bottomLimit - label.boxH;
      let boxY = clamp(label.desiredBoxY, topLimit, maxBoxY);
      if (idx > 0) {
        const prev = labels[idx - 1];
        boxY = Math.max(boxY, prev.boxY + prev.boxH + minGap);
      }
      label.boxY = clamp(boxY, topLimit, maxBoxY);
    });

    for (let idx = labels.length - 2; idx >= 0; idx -= 1) {
      const label = labels[idx];
      const next = labels[idx + 1];
      const maxBoxY = next.boxY - label.boxH - minGap;
      label.boxY = clamp(Math.min(label.boxY, maxBoxY), topLimit, bottomLimit - label.boxH);
    }

    ctx.save();
    ctx.font = '11px sans-serif';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 1;
    labels.forEach(label => {
      const boxCenterY = label.boxY + (label.boxH / 2);
      const connectorStartX = width - pad.right;
      const connectorEndX = label.boxX - 3;
      ctx.strokeStyle = label.color;
      ctx.beginPath();
      ctx.moveTo(connectorStartX, label.targetY);
      ctx.lineTo(connectorEndX, boxCenterY);
      ctx.stroke();
      ctx.fillStyle = label.color;
      ctx.fillRect(label.boxX, label.boxY, label.boxW, label.boxH);
      ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() || '#09111f';
      ctx.fillText(label.labelText, label.boxX + 4, boxCenterY + 0.5);
    });
    ctx.restore();
  }

  function zoneLabelText(zone) {
    const rawLabels = Array.isArray(zone?.labels) ? zone.labels : [];
    const labels = rawLabels
      .map(value => String(value || '').trim())
      .filter(Boolean)
      .slice(0, 3);
    const kind = String(zone?.kind || '').toLowerCase();
    const sideBase = kind === 'support' ? 'Support Zone' : 'Resistance Zone';
    const genericLabels = new Set(['s', 's2', 'r', 'r2', 'support', 'resistance', 'support zone', 'resistance zone']);
    const meaningfulLabels = labels.filter(label => !genericLabels.has(label.toLowerCase()));
    const pendingState = String(zone?.pending_state || '').toLowerCase();
    const flipState = String(zone?.flip_state || 'original').toLowerCase();
    const originalKind = String(zone?.original_kind || '').toLowerCase();
    let base = labels.length ? labels.join(' · ') : sideBase;
    if (pendingState === 'pending_break' || pendingState === 'pending_reclaim') {
      base = meaningfulLabels.length
        ? `${sideBase} · ${meaningfulLabels.join(' · ')}`
        : sideBase;
      if (pendingState === 'pending_break') {
        base += ' · Pending Break';
      } else {
        base += ' · Pending Reclaim';
      }
      return base;
    }
    const originalBase = originalKind === 'support'
      ? 'Support Zone'
      : (originalKind === 'resistance' ? 'Resistance Zone' : sideBase);
    if (flipState === 'confirmed_flip' && originalKind && originalKind !== kind) {
      const flipText = originalKind === 'support' ? 'Flipped from Support' : 'Flipped from Resistance';
      base = meaningfulLabels.length
        ? `${sideBase} · ${meaningfulLabels.join(' · ')} · ${flipText}`
        : `${sideBase} · ${flipText}`;
    } else if (originalKind && originalKind === kind) {
      const originText = originalBase === sideBase ? 'Original' : `Original ${originalBase}`;
      base = meaningfulLabels.length
        ? `${sideBase} · ${meaningfulLabels.join(' · ')} · ${originText}`
        : `${sideBase} · ${originText}`;
    }
    return base;
  }

  function fitTextToWidth(text, maxWidth) {
    const source = String(text || '').trim();
    if (!source) return '';
    if (maxWidth <= 0) return '';
    if (ctx.measureText(source).width <= maxWidth) return source;
    const ellipsis = '…';
    if (ctx.measureText(ellipsis).width > maxWidth) return '';
    let lo = 0;
    let hi = source.length;
    let best = ellipsis;
    while (lo <= hi) {
      const mid = Math.floor((lo + hi) / 2);
      const candidate = `${source.slice(0, mid).trimEnd()}${ellipsis}`;
      if (ctx.measureText(candidate).width <= maxWidth) {
        best = candidate;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return best;
  }

  function drawKeyLevelZoneLabel(zone) {
    const lower = numOrNull(zone?.lower);
    const upper = numOrNull(zone?.upper);
    if (lower === null || upper === null) return;
    const labelText = zoneLabelText(zone);
    if (!labelText) return;
    const clampedUpper = clamp(Math.max(upper, lower), minY, maxY);
    const clampedLower = clamp(Math.min(upper, lower), minY, maxY);
    const topY = yFor(clampedUpper);
    const bottomY = yFor(clampedLower);
    const zoneCenterY = (topY + bottomY) / 2;
    const boxH = 18;
    const textPadX = 7;
    ctx.save();
    ctx.font = '600 11px sans-serif';
    const plotWidth = Math.max(0, width - pad.left - pad.right);
    const plotInnerMargin = 10;
    const maxBoxWTarget = clamp(Math.round(plotWidth * 0.34), 88, 192);
    const maxBoxWCanvas = Math.max(44, Math.floor(plotWidth - (plotInnerMargin * 2)));
    const maxBoxW = Math.max(44, Math.min(maxBoxWTarget, maxBoxWCanvas));
    const maxTextW = Math.max(0, maxBoxW - (textPadX * 2));
    const fittedText = fitTextToWidth(labelText, maxTextW);
    if (!fittedText) {
      ctx.restore();
      return;
    }
    const measured = ctx.measureText(fittedText).width;
    const boxW = Math.max(44, Math.min(Math.round(measured + (textPadX * 2)), maxBoxW));
    const isSupport = String(zone?.kind || '').toLowerCase() === 'support';
    const minBoxX = pad.left + plotInnerMargin;
    const maxBoxX = Math.max(minBoxX, width - pad.right - boxW - plotInnerMargin);
    const boxX = isSupport
      ? minBoxX
      : maxBoxX;
    const boxY = clamp(Math.round(zoneCenterY - (boxH / 2)), pad.top + 2, height - pad.bottom - boxH - 2);
    ctx.fillStyle = isSupport ? 'rgba(10, 48, 28, 0.78)' : 'rgba(58, 18, 24, 0.80)';
    ctx.strokeStyle = isSupport ? 'rgba(76, 214, 128, 0.85)' : 'rgba(255, 92, 92, 0.85)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.roundRect(boxX, boxY, boxW, boxH, 7);
    ctx.fill();
    ctx.stroke();
    ctx.save();
    ctx.beginPath();
    ctx.roundRect(boxX + 1, boxY + 1, Math.max(0, boxW - 2), Math.max(0, boxH - 2), 6);
    ctx.clip();
    ctx.fillStyle = isSupport ? '#baf5cd' : '#ffd0d5';
    ctx.textBaseline = 'alphabetic';
    ctx.fillText(fittedText, boxX + textPadX, boxY + 12.5);
    ctx.restore();
    ctx.restore();
  }

  function paint(hoverIndex = null) {
    const activeIndex = hoverIndex !== null && hoverIndex !== undefined ? hoverIndex : pinnedIndex;
    pendingHorizontalLabels = [];
    ctx.clearRect(0, 0, width, height);
    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i += 1) {
      const y = pad.top + (plotH / 4) * i;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
    }
    for (let i = 0; i < 6; i += 1) {
      const x = pad.left + (plotW / 5) * i;
      ctx.beginPath();
      ctx.moveTo(x, pad.top);
      ctx.lineTo(x, height - pad.bottom);
      ctx.stroke();
    }

    if (activeIndex !== null && activeIndex >= 0 && activeIndex < bars.length) {
      const x = xFor(activeIndex);
      ctx.fillStyle = 'rgba(121,212,255,0.08)';
      ctx.fillRect(x - slotW / 2, pad.top, slotW, plotH);
    }

    function drawVolumeBars() {
      if (!show('show_volume', true)) return;
      const volumeBottomInset = 8;
      const volumeTopInset = 4;
      const volumeBaseY = height - pad.bottom - volumeBottomInset;
      const maxVolumeHeight = Math.max((plotH * 0.19) - volumeTopInset, 10);
      const sqrtVolumeMax = Math.sqrt(Math.max(volumeRenderMax, 1));
      bars.forEach((bar, idx) => {
        const x = xFor(idx);
        const rising = Number(bar.close) >= Number(bar.open);
        // `Number(bar.volume || 0)` would coerce the string "NaN"
        // (truthy) to NaN, which then propagates through Math.sqrt and
        // skips the bar silently. Use parseFinite (defined near the top)
        // to convert non-numeric / null / "NaN" to a safe 0.
        const rawVolume = parseFinite(bar.volume);
        const volumeValue = Math.max(rawVolume === null ? 0 : rawVolume, 0);
        const scaledVolume = volumeValue > 0 ? (Math.sqrt(volumeValue) / sqrtVolumeMax) : 0;
        const volH = volumeValue > 0 ? Math.max(1.5, scaledVolume * maxVolumeHeight) : 0;
        ctx.fillStyle = rising ? 'rgba(108,227,162,0.34)' : 'rgba(255,107,130,0.36)';
        if (volH > 0) ctx.fillRect(x - candleW / 2, volumeBaseY - volH, candleW, volH);
      });
    }

    function drawPriceAxisLabels() {
      ctx.save();
      ctx.fillStyle = '#92a0bf';
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      for (let i = 0; i <= 4; i += 1) {
        const value = maxY - (span / 4) * i;
        const y = pad.top + (plotH / 4) * i;
        const labelY = clamp(y, pad.top + 1, height - pad.bottom - 1);
        ctx.fillText(fmtNum(value, 2), pad.left - 8, labelY);
      }
      ctx.restore();
    }

    function drawTimeAxisLabels() {
      ctx.save();
      ctx.fillStyle = '#92a0bf';
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      const tickY = height - pad.bottom;
      const labelY = tickY + 8;
      const occupiedRanges = [];
      timeAxisTickData.forEach(tick => {
        ctx.strokeStyle = 'rgba(255,255,255,0.12)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(tick.x, tickY);
        ctx.lineTo(tick.x, tickY + 5);
        ctx.stroke();
        ctx.fillText(tick.label, tick.x, labelY);
        const width = ctx.measureText(tick.label).width;
        occupiedRanges.push([tick.x - (width / 2) - 6, tick.x + (width / 2) + 6]);
      });
      ctx.fillStyle = '#7483a6';
      ctx.font = '600 10px sans-serif';
      dayAxisLabelData.forEach(dayTick => {
        const width = ctx.measureText(dayTick.label).width;
        const range = [dayTick.x - (width / 2) - 6, dayTick.x + (width / 2) + 6];
        const overlaps = occupiedRanges.some(([start, end]) => !(range[1] < start || range[0] > end));
        if (overlaps) return;
        ctx.fillText(dayTick.label, dayTick.x, labelY);
        occupiedRanges.push(range);
      });
      ctx.restore();
    }

    if (show('show_bollinger_bands', false)) {
      drawShadedBand(bbUpperValues, bbLowerValues, 'rgba(184, 109, 255, 0.08)');
    }

    if (show('show_channel', false) && technicals.channel && technicals.channel.valid) {
      if (channelUpperLine && channelLowerLine) {
        drawShadedBand(channelUpperValues, channelLowerValues, 'rgba(74, 155, 255, 0.07)');
      }
    }

    if (show('show_htf_fair_value_gaps', false)) {
      htfFairValueGaps.forEach(gap => {
        const lower = numOrNull(gap?.lower);
        const upper = numOrNull(gap?.upper);
        if (lower === null || upper === null) return;
        const fill = String(gap?.direction || '').toLowerCase() === 'bullish'
          ? 'rgba(76, 214, 128, 0.14)'
          : 'rgba(255, 92, 92, 0.14)';
        const timedRange = resolveTimedZoneRange(gap?.first_seen, null, 8, 8);
        if (!timedRange) return;
        drawTimedZone(timedRange.startIdx, timedRange.endIdx, upper, lower, fill);
      });
    }

    if (show('show_1m_fair_value_gaps', false)) {
      oneMinuteFairValueGaps.forEach(gap => {
        const lower = numOrNull(gap?.lower);
        const upper = numOrNull(gap?.upper);
        if (lower === null || upper === null) return;
        const fill = String(gap?.direction || '').toLowerCase() === 'bullish'
          ? 'rgba(76, 214, 128, 0.18)'
          : 'rgba(255, 92, 92, 0.18)';
        const timedRange = Number.isFinite(Number(gap?.anchor_abs_index))
          ? resolveIndexedZoneRange(gap?.anchor_abs_index, 15, 15)
          : resolveTimedZoneRange(gap?.first_seen, null, 15, 15, { requireVisibleStart: true });
        if (!timedRange) return;
        drawTimedZone(timedRange.startIdx, timedRange.endIdx, upper, lower, fill);
      });
    }

    // Order blocks render after FVGs so any overlapping zones show the OB
    // dashed-border on top, making both visible. Bullish=green / bearish=red
    // matches the FVG color semantics; the dashed stroke + low fill opacity
    // is what differentiates OBs from FVGs visually.
    if (show('show_htf_order_blocks', false)) {
      htfOrderBlocks.forEach(ob => {
        const lower = numOrNull(ob?.lower);
        const upper = numOrNull(ob?.upper);
        if (lower === null || upper === null) return;
        const isBullish = String(ob?.direction || '').toLowerCase() === 'bullish';
        const fill = isBullish ? 'rgba(76, 214, 128, 0.06)' : 'rgba(255, 92, 92, 0.06)';
        const stroke = isBullish ? 'rgba(76, 214, 128, 0.85)' : 'rgba(255, 92, 92, 0.85)';
        const timedRange = resolveTimedZoneRange(ob?.first_seen, null, 8, 8);
        if (!timedRange) return;
        drawTimedDashedZone(timedRange.startIdx, timedRange.endIdx, upper, lower, fill, stroke, 1.4, [6, 4]);
      });
    }

    if (show('show_1m_order_blocks', false)) {
      oneMinuteOrderBlocks.forEach(ob => {
        const lower = numOrNull(ob?.lower);
        const upper = numOrNull(ob?.upper);
        if (lower === null || upper === null) return;
        const isBullish = String(ob?.direction || '').toLowerCase() === 'bullish';
        const fill = isBullish ? 'rgba(76, 214, 128, 0.07)' : 'rgba(255, 92, 92, 0.07)';
        const stroke = isBullish ? 'rgba(76, 214, 128, 0.90)' : 'rgba(255, 92, 92, 0.90)';
        const timedRange = Number.isFinite(Number(ob?.anchor_abs_index))
          ? resolveIndexedZoneRange(ob?.anchor_abs_index, 15, 15)
          : resolveTimedZoneRange(ob?.first_seen, null, 15, 15, { requireVisibleStart: true });
        if (!timedRange) return;
        drawTimedDashedZone(timedRange.startIdx, timedRange.endIdx, upper, lower, fill, stroke, 1.2, [5, 4]);
      });
    }

    if (show('show_key_level_zones', true)) {
      visibleKeyLevelZones.forEach(zone => {
        const lower = numOrNull(zone?.lower);
        const upper = numOrNull(zone?.upper);
        if (lower === null || upper === null) return;
        const isSupport = String(zone?.kind || '').toLowerCase() === 'support';
        const selected = !!zone?.selected_for_entry;
        const passes = !!zone?.passes_min_level_score;
        const fill = isSupport
          ? (selected ? 'rgba(76, 214, 128, 0.26)' : (passes ? 'rgba(76, 214, 128, 0.16)' : 'rgba(76, 214, 128, 0.10)'))
          : (selected ? 'rgba(255, 92, 92, 0.26)' : (passes ? 'rgba(255, 92, 92, 0.16)' : 'rgba(255, 92, 92, 0.10)'));
        const stroke = isSupport
          ? (selected ? 'rgba(150, 255, 191, 0.92)' : (passes ? 'rgba(76, 214, 128, 0.62)' : 'rgba(76, 214, 128, 0.34)'))
          : (selected ? 'rgba(255, 186, 193, 0.94)' : (passes ? 'rgba(255, 92, 92, 0.66)' : 'rgba(255, 92, 92, 0.34)'));
        drawHorizontalZone(upper, lower, fill, stroke, selected ? 1.5 : 1.0);
      });
      if (show('show_key_level_zone_labels', true)) {
        prioritizedVisibleKeyLevelZones.forEach(zone => drawKeyLevelZoneLabel(zone));
      }
    }

    drawVolumeBars();

    bars.forEach((bar, idx) => {
      const x = xFor(idx);
      const openY = yFor(bar.open);
      const closeY = yFor(bar.close);
      const highY = yFor(bar.high);
      const lowY = yFor(bar.low);
      const rising = Number(bar.close) >= Number(bar.open);
      const isHover = idx === activeIndex;
      ctx.strokeStyle = rising ? '#6ce3a2' : '#ff6b82';
      ctx.lineWidth = isHover ? 2.2 : 1.4;
      ctx.beginPath();
      ctx.moveTo(x, highY);
      ctx.lineTo(x, lowY);
      ctx.stroke();
      ctx.fillStyle = rising ? 'rgba(108,227,162,0.82)' : 'rgba(255,107,130,0.84)';
      const bodyTop = Math.min(openY, closeY);
      const bodyH = Math.max(2, Math.abs(closeY - openY));
      ctx.fillRect(x - candleW / 2, bodyTop, candleW, bodyH);
      if (isHover) {
        ctx.strokeStyle = '#cfe8ff';
        ctx.lineWidth = 1.1;
        ctx.strokeRect(x - candleW / 2 - 1, bodyTop - 1, candleW + 2, bodyH + 2);
      }
    });

    const legendChips = [`<span class="legend-chip"><span class="swatch" style="background:${appState.mainPanelExpanded ? '#79d4ff' : '#6ce3a2'};"></span>${bars.length} bars</span>`];
    const lastBar = bars[bars.length - 1];

    seriesDefs.forEach(def => {
      const vals = seriesValueMap.get(def.key) || [];
      if (!vals.some(v => v !== null)) return;
      ctx.strokeStyle = def.color;
      ctx.lineWidth = def.width || 2;
      ctx.beginPath();
      let started = false;
      vals.forEach((value, idx) => {
        if (value === null) return;
        const x = xFor(idx);
        const y = yFor(value);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      ctx.stroke();
      if (activeIndex !== null && activeIndex >= 0 && vals[activeIndex] !== null) {
        const hx = xFor(activeIndex);
        const hy = yFor(vals[activeIndex]);
        ctx.fillStyle = def.color;
        ctx.beginPath();
        ctx.arc(hx, hy, 3.4, 0, Math.PI * 2);
        ctx.fill();
      }
      legendChips.push(`<span class="legend-chip"><span class="swatch" style="background:${def.color};"></span>${def.label} ${fmtNum(vals[vals.length - 1], 2)}</span>`);
    });

    visibleHorizontalLines.forEach((line, idx) => {
      if (numOrNull(line.value) === null) return;
      const y = yFor(line.value);
      ctx.setLineDash(line.dash || [6, 6]);
      ctx.strokeStyle = line.color;
      ctx.lineWidth = 1.3;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
      ctx.setLineDash([]);
      if (line.labelMode === 'inline' && line.shortLabel) {
        // Draw label inline on the line, anchored to the left
        ctx.save();
        ctx.font = '10px sans-serif';
        const text = line.shortLabel;
        const tw = ctx.measureText(text).width;
        const lx = pad.left + 6;
        const ly = Math.round(y) - 5;
        ctx.fillStyle = line.color;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(lx - 3, ly - 9, tw + 6, 13);
        ctx.globalAlpha = 1.0;
        ctx.fillStyle = '#000000';
        ctx.textBaseline = 'middle';
        ctx.fillText(text, lx, ly - 2.5);
        ctx.restore();
      } else if (line.shortLabel) {
        drawHorizontalLabel(line.shortLabel, line.value, line.color);
      }
    });

    visibleMarkerLines.forEach(line => {
      if (numOrNull(line.value) === null) return;
      const y = yFor(line.value);
      ctx.setLineDash(line.dash || [3, 4]);
      ctx.strokeStyle = line.color;
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
      ctx.setLineDash([]);
      drawHorizontalLabel(line.shortLabel || line.label, line.value, line.color);
    });

    visibleDiagonalLines.forEach(item => {
      const line = item.line;
      if (!line) return;
      const startAbs = item.useChannelRange && channelRenderRange
        ? channelRenderRange.startAbs
        : Math.max(visibleAbsStart, numOrNull(line.start_pos) ?? visibleAbsStart);
      const endAbs = item.useChannelRange && channelRenderRange
        ? channelRenderRange.endAbs
        : Math.max(startAbs, Math.min(visibleAbsEnd, numOrNull(line.end_pos) ?? visibleAbsEnd));
      const startIdx = item.useChannelRange && channelRenderRange
        ? channelRenderRange.startIdx
        : clamp(Math.round(startAbs - visibleAbsStart), 0, bars.length - 1);
      const endIdx = item.useChannelRange && channelRenderRange
        ? channelRenderRange.endIdx
        : clamp(Math.round(endAbs - visibleAbsStart), 0, bars.length - 1);
      const startVal = lineValueAt(line, Number(bars[startIdx].abs_index));
      const endVal = lineValueAt(line, Number(bars[endIdx].abs_index));
      if (numOrNull(startVal) === null || numOrNull(endVal) === null) return;
      ctx.setLineDash(item.dash || [7, 5]);
      ctx.strokeStyle = item.color;
      ctx.lineWidth = item.width || 1.4;
      ctx.beginPath();
      ctx.moveTo(xFor(startIdx), yFor(startVal));
      ctx.lineTo(xFor(endIdx), yFor(endVal));
      ctx.stroke();
      ctx.setLineDash([]);
      // On-chart label at the right end of the diagonal line
      const diagLabelText = item.label || '';
      if (diagLabelText) {
        ctx.save();
        ctx.font = '10px sans-serif';
        const diagTw = ctx.measureText(diagLabelText).width;
        const diagX = xFor(endIdx) - diagTw - 8;
        const diagY = Math.round(yFor(endVal)) - 5;
        ctx.fillStyle = item.color;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(diagX - 3, diagY - 9, diagTw + 6, 13);
        ctx.globalAlpha = 1.0;
        ctx.fillStyle = '#000000';
        ctx.textBaseline = 'middle';
        ctx.fillText(diagLabelText, diagX, diagY - 2.5);
        ctx.restore();
      }
      legendChips.push(`<span class="legend-chip"><span class="swatch" style="background:${item.color};"></span>${item.label}</span>`);
    });
    // Key-level zones stay on-chart only; omit their extra legend chips to reduce clutter.

    if (show('show_trade_markers', true)) {
      const eventMarkers = [];
      const entryIdx = nearestIndexForTs(positionMarkers.entry_time, bars);
      if (entryIdx !== null) {
        eventMarkers.push({ idx: entryIdx, kind: 'entry', side: safe(positionMarkers.side).toUpperCase(), color: '#8cf4ff' });
      }
      recentTrades.slice(0, 6).forEach(trade => {
        const entryTradeIdx = nearestIndexForTs(trade.entry_time, bars);
        const exitTradeIdx = nearestIndexForTs(trade.exit_time, bars);
        if (entryTradeIdx !== null) eventMarkers.push({ idx: entryTradeIdx, kind: 'entry', side: safe(trade.side).toUpperCase() });
        if (exitTradeIdx !== null) eventMarkers.push({ idx: exitTradeIdx, kind: 'exit', side: safe(trade.side).toUpperCase(), y: yFor(bars[exitTradeIdx].close) });
      });
      const drawDiamondMarker = (marker) => {
        const bar = bars[marker.idx] || null;
        if (!bar) return;
        const x = xFor(marker.idx);
        const isShort = String(marker.side || '').toUpperCase() === 'SHORT';
        const isExit = marker.kind === 'exit';
        const anchorY = isExit ? numOrNull(marker.y) ?? yFor(bar.close) : (isShort ? yFor(bar.high) : yFor(bar.low));
        const markerOffset = 12;
        const stemLength = 6;
        const markerY = isExit ? anchorY : (isShort ? anchorY - markerOffset : anchorY + markerOffset);
        const stemStartY = isShort ? anchorY - 1 : anchorY + 1;
        const stemEndY = isShort ? markerY + stemLength : markerY - stemLength;
        const outline = isExit ? '#ff365f' : (isShort ? '#ff6f86' : '#56d98a');
        const fill = isExit ? 'rgba(255, 54, 95, 0.18)' : (isShort ? 'rgba(255, 111, 134, 0.18)' : 'rgba(86, 217, 138, 0.18)');
        const half = 5;
        ctx.save();
        ctx.lineWidth = 1.1;
        ctx.strokeStyle = outline;
        ctx.shadowColor = outline;
        ctx.shadowBlur = 7;
        if (!isExit) {
          ctx.beginPath();
          ctx.moveTo(x, stemStartY);
          ctx.lineTo(x, stemEndY);
          ctx.stroke();
        }
        ctx.beginPath();
        ctx.moveTo(x, markerY - half);
        ctx.lineTo(x + half, markerY);
        ctx.lineTo(x, markerY + half);
        ctx.lineTo(x - half, markerY);
        ctx.closePath();
        ctx.fillStyle = fill;
        ctx.fill();
        ctx.strokeStyle = outline;
        ctx.stroke();
        ctx.restore();
      };
      eventMarkers.forEach(marker => {
        drawDiamondMarker(marker);
      });
    }

    if (activeIndex !== null && activeIndex >= 0 && activeIndex < bars.length) {
      // Use activeIndex (resolved from hoverIndex || pinnedIndex above)
      // for both the bar reference and the x-coordinate. Today
      // pinnedIndex is declared at line ~2337 but never reassigned, so
      // activeIndex always equals hoverIndex in practice — but if a
      // future change wires bar-pinning, paint(null) with a pinned bar
      // would dereference bars[null]/xFor(null) and crash here. Keep
      // the references symmetrically on activeIndex so that landmine
      // is closed before pinning lands.
      const hoverBar = bars[activeIndex];
      const hoverX = xFor(activeIndex);
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = 'rgba(207,232,255,0.28)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(hoverX, pad.top);
      ctx.lineTo(hoverX, height - pad.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#cfe8ff';
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText(fmtNum(hoverBar.close, 2), 8, clamp(yFor(hoverBar.close), pad.top + 6, height - pad.bottom - 6));
    }

    drawPriceAxisLabels();
    drawTimeAxisLabels();
    renderHorizontalLabels();
    legend.innerHTML = legendChips.join('');
  }

  function updateTooltip(idx, clientX = null, clientY = null) {
    if (!tooltip || idx === null || idx < 0 || idx >= bars.length) {
      hideTooltip();
      return;
    }
    const bar = bars[idx];
    const delta = numOrNull(bar.close) !== null && numOrNull(bar.open) !== null ? Number(bar.close) - Number(bar.open) : null;
    const deltaPct = delta !== null && Number(bar.open) ? (delta / Number(bar.open)) * 100 : null;
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
    const structureState = sr.state || 'neutral';
    const structureTrend = bar.trend_state ?? sr.trend ?? sr.trend_state ?? 'neutral';
    const structureBias = sr.structure_bias || 'neutral';
    const structureEvent = sr.structure_event || '—';
    const sections = [];
    if (show('tooltip_show_returns', true)) {
      sections.push(`
        <div class="tt-section">
          <div class="tt-section-title">Returns</div>
          <div class="tt-grid">
            <div class="tt-kv"><span>Ret1</span><strong>${fmtPctFromRatio(bar.ret1, 2)}</strong></div>
            <div class="tt-kv"><span>Ret5</span><strong>${fmtPctFromRatio(bar.ret5, 2)}</strong></div>
            <div class="tt-kv"><span>Ret15</span><strong>${fmtPctFromRatio(bar.ret15, 2)}</strong></div>
            <div class="tt-kv"><span>ATR14</span><strong>${fmtNum(bar.atr14, 2)}</strong></div>
          </div>
        </div>
      `);
    }
    if (show('tooltip_show_support_resistance', true)) {
      const supportValue = sr.nearest_support;
      const resistanceValue = sr.nearest_resistance;
      const nextSupportValue = levels.next_support;
      const nextResistanceValue = levels.next_resistance;
      sections.push(`
        <div class="tt-section">
          <div class="tt-section-title">Support / Resistance</div>
          <div class="tt-grid">
            <div class="tt-kv"><span>Support</span><strong>${fmtNum(supportValue, 2)}</strong></div>
            <div class="tt-kv"><span>Resistance</span><strong>${fmtNum(resistanceValue, 2)}</strong></div>
            <div class="tt-kv"><span>Next Support</span><strong>${fmtNum(nextSupportValue, 2)}</strong></div>
            <div class="tt-kv"><span>Next Resistance</span><strong>${fmtNum(nextResistanceValue, 2)}</strong></div>
          </div>
        </div>
      `);
    }
    if (show('tooltip_show_structure', true)) {
      sections.push(`
        <div class="tt-section">
          <div class="tt-section-title">Structure</div>
          <div class="tt-grid">
            <div class="tt-kv"><span>State</span><strong>${escapeHtml(prettyLabel(structureState || 'neutral'))}</strong></div>
            <div class="tt-kv"><span>Trend</span><strong>${escapeHtml(prettyLabel(structureTrend || 'neutral'))}</strong></div>
            <div class="tt-kv"><span>Bias</span><strong>${escapeHtml(prettyLabel(structureBias || 'neutral'))}</strong></div>
            <div class="tt-kv"><span>Event</span><strong>${escapeHtml(structureEvent || '—')}</strong></div>
          </div>
        </div>
      `);
    }
    if (show('tooltip_show_volatility', true)) {
      sections.push(`
        <div class="tt-section">
          <div class="tt-section-title">Volatility / Trend</div>
          <div class="tt-grid">
            <div class="tt-kv"><span>ATR %</span><strong>${fmtPctFromRatio(bar.atr_pct, 2)}</strong></div>
            <div class="tt-kv"><span>ADX</span><strong>${fmtNum(bar.adx ?? technicals.adx, 2)}</strong></div>
            <div class="tt-kv"><span>+DI / -DI</span><strong>${fmtNum(bar.plus_di ?? technicals.plus_di, 1)} / ${fmtNum(bar.minus_di ?? technicals.minus_di, 1)}</strong></div>
            <div class="tt-kv"><span>DMI Bias</span><strong>${escapeHtml(prettyLabel(bar.dmi_bias || technicals.dmi_bias || 'neutral'))}</strong></div>
            <div class="tt-kv"><span>BB Width</span><strong>${fmtPctFromRatio(bar.bb_width_pct ?? technicals.bollinger_width_pct, 2)}</strong></div>
            <div class="tt-kv"><span>%B / Z</span><strong>${fmtNum(bar.bb_percent_b ?? technicals.bollinger_percent_b, 2)} / ${fmtNum(bar.bb_zscore ?? technicals.bollinger_zscore, 2)}</strong></div>
          </div>
        </div>
      `);
    }
    if (show('tooltip_show_orderflow', true)) {
      sections.push(`
        <div class="tt-section">
          <div class="tt-section-title">Orderflow / Context</div>
          <div class="tt-grid">
            <div class="tt-kv"><span>OBV Bias</span><strong>${escapeHtml(prettyLabel(bar.obv_bias || technicals.obv_bias || 'neutral'))}</strong></div>
            <div class="tt-kv"><span>OBV</span><strong>${fmtCompact(bar.obv ?? technicals.obv)}</strong></div>
            <div class="tt-kv"><span>OBV EMA</span><strong>${fmtCompact(bar.obv_ema ?? technicals.obv_ema)}</strong></div>
            <div class="tt-kv"><span>AVWAP Bias</span><strong>${escapeHtml(prettyLabel(bar.anchored_vwap_bias || technicals.anchored_vwap_bias || 'neutral'))}</strong></div>
          </div>
        </div>
      `);
    }
    if (show('tooltip_show_patterns', true)) {      const explicitBullishTags = uniqPatternList([
        ...(patterns.chart_bullish_continuation || []),
        ...(patterns.chart_bullish_reversal || []),
        ...(patterns.chart_bullish || []),
        ...(patterns.candles_bullish || []),
      ]);
      const explicitBearishTags = uniqPatternList([
        ...(patterns.chart_bearish_continuation || []),
        ...(patterns.chart_bearish_reversal || []),
        ...(patterns.chart_bearish || []),
        ...(patterns.candles_bearish || []),
      ]);
      const chartFallback = deriveChartFallback(idx);
      const candleFallback = deriveCandleFallback(idx);
      const rawChartRegime = String((patterns.chart_regime_hint) || 'neutral').toLowerCase();
      const rawCandleRegime = String((patterns.candle_regime_hint) || 'neutral').toLowerCase();
      const rawChartBias = numOrNull(patterns.chart_bias_score);
      const rawCandleBias = numOrNull(patterns.candle_bias_score);
      const useExplicitChart = explicitBullishTags.length > 0 || explicitBearishTags.length > 0 || rawChartRegime !== 'neutral' || (rawChartBias !== null && Math.abs(rawChartBias) > 0.0001);
      const useExplicitCandle = (patterns.candles_bullish || []).length > 0 || (patterns.candles_bearish || []).length > 0 || rawCandleRegime !== 'neutral' || (rawCandleBias !== null && Math.abs(rawCandleBias) > 0.0001);
      const chartRegime = useExplicitChart ? ((patterns.chart_regime_hint) || 'neutral') : chartFallback.regime;
      const chartBias = useExplicitChart ? patterns.chart_bias_score : chartFallback.score;
      const candleRegime = useExplicitCandle ? ((patterns.candle_regime_hint) || 'neutral') : candleFallback.regime;
      const candleBias = useExplicitCandle ? patterns.candle_bias_score : candleFallback.score;
      const bullishTags = uniqPatternList([
        ...explicitBullishTags,
        ...chartFallback.bullish,
        ...candleFallback.bullish,
      ]);
      const bearishTags = uniqPatternList([
        ...explicitBearishTags,
        ...chartFallback.bearish,
        ...candleFallback.bearish,
      ]);
      sections.push(`
        <div class="tt-section">
          <div class="tt-section-title">Patterns</div>
          <div class="tt-grid">
            <div class="tt-kv"><span>Chart Regime</span><strong>${escapeHtml(prettyLabel(chartRegime || 'neutral'))}</strong></div>
            <div class="tt-kv"><span>Chart Bias</span><strong>${fmtNum(chartBias, 2)}</strong></div>
            <div class="tt-kv"><span>Candle Regime</span><strong>${escapeHtml(prettyLabel(candleRegime || 'neutral'))}</strong></div>
            <div class="tt-kv"><span>Candle Bias</span><strong>${fmtNum(candleBias, 2)}</strong></div>
          </div>
          <div class="tt-section-title" style="margin-top:8px;">Bullish</div>
          <div class="tt-tags">${tagHtml(bullishTags, 'good')}</div>
          <div class="tt-section-title" style="margin-top:8px;">Bearish</div>
          <div class="tt-tags">${tagHtml(bearishTags, 'bad')}</div>
        </div>
      `);
    }
    const tooltipCacheKey = `${idx}|${bar.ts}`;
    let tooltipHtml = tooltipHtmlCache.get(tooltipCacheKey);
    if (!tooltipHtml) {
      tooltipHtml = `
      <div class="tt-head">
        <span>${escapeHtml(snapshot.symbol)} · ${escapeHtml(fmtChartTs(bar.ts))}</span>
        <span class="${delta === null ? '' : (delta >= 0 ? 'good' : 'bad')}">${delta === null ? '—' : fmtPct(deltaPct, 2)}</span>
      </div>
      <div class="tt-grid">
        <div class="tt-kv"><span>Open</span><strong>${fmtNum(bar.open, 2)}</strong></div>
        <div class="tt-kv"><span>High</span><strong>${fmtNum(bar.high, 2)}</strong></div>
        <div class="tt-kv"><span>Low</span><strong>${fmtNum(bar.low, 2)}</strong></div>
        <div class="tt-kv"><span>Close</span><strong>${fmtNum(bar.close, 2)}</strong></div>
        <div class="tt-kv"><span>Change</span><strong>${delta === null ? '—' : fmtNum(delta, 2)}</strong></div>
        <div class="tt-kv"><span>Volume</span><strong>${fmtInteger(bar.volume)}</strong></div>
        <div class="tt-kv"><span>EMA9</span><strong>${fmtNum(bar.ema9, 2)}</strong></div>
        <div class="tt-kv"><span>EMA20</span><strong>${fmtNum(bar.ema20, 2)}</strong></div>
        <div class="tt-kv"><span>VWAP</span><strong>${fmtNum(bar.vwap, 2)}</strong></div>
        <div class="tt-kv"><span>Range</span><strong>${numOrNull(bar.high) !== null && numOrNull(bar.low) !== null ? fmtNum(Number(bar.high) - Number(bar.low), 2) : '—'}</strong></div>
      </div>
      ${sections.join('')}
    `;
      tooltipHtmlCache.set(tooltipCacheKey, tooltipHtml);
    }
    tooltip.innerHTML = tooltipHtml;
    tooltip.classList.add('active');
    const anchor = tooltipAnchorForIndex(idx);
    const resolvedClientX = Number.isFinite(clientX) ? clientX : (anchor?.clientX ?? lastTooltipLeft ?? 0);
    const resolvedClientY = Number.isFinite(clientY) ? clientY : (anchor?.clientY ?? lastTooltipTop ?? 0);
    const margin = 14;
    const cursorGapX = 28;
    const cursorGapY = 22;
    const ttRect = tooltip.getBoundingClientRect();
    const canvasRect = canvas.getBoundingClientRect();
    const leftAxisSafeRight = canvasRect.left + pad.left + 12;
    let left = resolvedClientX - ttRect.width - cursorGapX;
    let top = resolvedClientY + cursorGapY;
    if (left < leftAxisSafeRight) left = resolvedClientX + cursorGapX;
    if (left < margin) left = resolvedClientX + cursorGapX;
    if (left + ttRect.width > viewportWidth - margin) left = viewportWidth - margin - ttRect.width;
    if (left < margin) left = margin;
    if (top + ttRect.height > viewportHeight - margin) top = resolvedClientY - ttRect.height - cursorGapY;
    if (top < margin) top = margin;
    left = Math.round(left);
    top = Math.round(top);
    lastTooltipLeft = left;
    lastTooltipTop = top;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }

  paint(null);

  canvas.onclick = null;
  // Unified pointer-driven hover: single handler covers mouse, pen, and
  // touch. `pointermove` fires on mouse motion AND on dragging-finger;
  // for taps we also wire `pointerdown` so the tooltip appears
  // immediately on first contact (instead of only after movement).
  // `pointerleave` mirrors the legacy mouseleave semantics. Touch events
  // are gated to events with pointerType === 'touch' so we don't double-
  // process the synthetic mouse events that follow a tap on some browsers.
  const handlePointerActivity = (clientX, clientY) => {
    pendingHoverState = { clientX, clientY };
    if (hoverFrameRequest) return;
    hoverFrameRequest = window.requestAnimationFrame(() => {
      hoverFrameRequest = 0;
      const latest = pendingHoverState;
      pendingHoverState = null;
      if (!latest) return;
      const rect = canvas.getBoundingClientRect();
      const x = latest.clientX - rect.left;
      if (x < pad.left || x > width - pad.right) {
        paint(null);
        hideTooltip();
        return;
      }
      const idx = clamp(Math.floor((x - pad.left) / slotW), 0, bars.length - 1);
      paint(idx);
      updateTooltip(idx, latest.clientX, latest.clientY);
    });
  };
  const clearPointerActivity = () => {
    if (hoverFrameRequest) {
      window.cancelAnimationFrame(hoverFrameRequest);
      hoverFrameRequest = 0;
    }
    pendingHoverState = null;
    paint(null);
    hideTooltip();
  };
  canvas.onpointermove = (event) => handlePointerActivity(event.clientX, event.clientY);
  canvas.onpointerdown = (event) => {
    // Only react to touch/pen down — mouse hover is already covered by
    // pointermove. This avoids a redundant repaint on every mouse click.
    if (event.pointerType === 'mouse') return;
    handlePointerActivity(event.clientX, event.clientY);
  };
  canvas.onpointerleave = (event) => {
    // For touch/pen, lifting the finger fires pointerleave/pointercancel
    // immediately — clearing the tooltip would create a flash on every
    // tap. Keep the tooltip visible until the next gesture (which will
    // either redraw it via pointerdown or move it via pointermove).
    // Mouse cursors leaving the canvas should still clear immediately.
    if (event && event.pointerType && event.pointerType !== 'mouse') return;
    clearPointerActivity();
  };
  canvas.onpointercancel = (event) => {
    // pointercancel fires for touch interrupted by scroll, pinch, or
    // multi-touch. Apply the same touch-keeps-tooltip rule so a stray
    // gesture interruption doesn't blank a deliberately tapped bar.
    if (event && event.pointerType && event.pointerType !== 'mouse') return;
    clearPointerActivity();
  };
}

function buildEvents(data, snapshot) {
  const events = [];
  if (snapshot) {
    const sr = snapshot.support_resistance || {};
    if (snapshot.position) {
      events.push({
        tone: pnlTone(snapshot.position.unrealized_pnl),
        title: `${snapshot.symbol} ${safe(snapshot.position.side)} position`,
        time: safe(snapshot.position.entry_time).replace('T', ' ').slice(0, 16),
        text: `${safe(snapshot.position.side)} · qty ${safe(snapshot.position.qty)} · entry ${fmtNum(snapshot.position.entry_price, 2)} · last ${fmtNum(snapshot.position.last_price, 2)} · unrealized ${fmtMoney(snapshot.position.unrealized_pnl)}.`
      });
    }
    if (snapshot.candidate) {
      events.push({
        tone: (() => { const bias = String(snapshot.candidate.directional_bias || '').toUpperCase(); return bias === 'SHORT' ? 'tone-bad' : (bias === 'LONG' ? 'tone-good' : 'tone-neutral'); })(),
        title: `${snapshot.symbol} candidate rank #${safe(snapshot.candidate.rank)}`,
        time: safe(data?.last_update).replace('T', ' ').slice(0, 16),
        text: `Activity ${fmtNum(snapshot.candidate.activity_score, 2)} · day ${fmtPct(snapshot.quote?.percent_change ?? snapshot.candidate.change_from_open)} · volume ${fmtCompact(snapshot.quote?.total_volume ?? snapshot.candidate.volume)}.`
      });
    }
    if (sr.structure_event && sr.structure_event !== '—') {
      events.push({
        tone: eventTone(sr.structure_event),
        title: `${snapshot.symbol} market structure`,
        time: safe(data?.last_update).replace('T', ' ').slice(0, 16),
        text: `${safe(sr.structure_event)} · ${safe(sr.structure_bias)} bias · ${safe(sr.state)} state · regime ${safe(sr.regime_hint)}.`
      });
    }
    const nearestSupport = positiveOrNull(sr.nearest_support);
    const nearestResistance = positiveOrNull(sr.nearest_resistance);
    const supportDistancePct = numOrNull(snapshot?.chart?.levels?.support_distance_pct ?? sr.support_distance_pct);
    const resistanceDistancePct = numOrNull(snapshot?.chart?.levels?.resistance_distance_pct ?? sr.resistance_distance_pct);
    if (nearestSupport !== null || nearestResistance !== null) {
      events.push({
        tone: 'tone-accent',
        title: `${snapshot.symbol} support / resistance`,
        time: safe(data?.last_update).replace('T', ' ').slice(0, 16),
        text: `Support ${fmtNum(nearestSupport, 2)} (${fmtPctFromRatio(nearestSupport === null ? null : supportDistancePct)}) · resistance ${fmtNum(nearestResistance, 2)} (${fmtPctFromRatio(nearestResistance === null ? null : resistanceDistancePct)}).`
      });
    }
  }
  const trades = (data?.performance?.recent_trades || []).filter(item => !snapshot || String(item.symbol || '').toUpperCase() === snapshot.symbol).slice(0, 4);
  trades.forEach(trade => {
    events.push({
      tone: pnlTone(trade.realized_pnl),
      title: `${trade.symbol} ${trade.strategy}`,
      time: safe(trade.exit_time).replace('T', ' ').slice(0, 16),
      text: `${fmtSide(trade)} · closed ${safe(trade.qty)} @ ${fmtNum(trade.exit_price, 2)} · realized ${fmtMoney(trade.realized_pnl)} · return ${fmtPct(trade.return_pct)} · ${safe(trade.reason)}.`
    });
  });
  events.push({
    tone: String(data?.status || '').toLowerCase() === 'running' ? 'tone-good' : 'tone-bad',
    title: `Bot ${safe(data?.status)}`,
    time: safe(data?.last_update).replace('T', ' ').slice(0, 16),
    text: `${safe(data?.message)} · streaming ${data?.streaming_active ? 'on' : 'off'} · screening ${data?.screening_active ? 'on' : 'off'}.`
  });
  return events;
}

function renderEventsAndDock() {
  const data = appState.data;
  const snapshot = activeSnapshotMap(data).get(appState.selectedSymbol) || null;
  const events = buildEvents(data, snapshot);
  document.getElementById('dock-meta').textContent = snapshot ? `${snapshot.symbol} focus · ${events.length} generated events` : 'Global bot view';
  document.getElementById('events-list').innerHTML = events.length ? events.map(event => `
    <div class="event-card">
      <div class="event-top">
        <div class="event-title ${event.tone === 'tone-good' ? 'good' : (event.tone === 'tone-bad' ? 'bad' : '')}">${escapeHtml(event.title)}</div>
        <div class="event-time">${escapeHtml(event.time)}</div>
      </div>
      <div class="event-text">${escapeHtml(event.text)}</div>
    </div>
  `).join('') : `<div class="empty-state">No event context available yet.</div>`;

  const exchangeMap = data?.symbol_exchanges || {};
  const trades = data?.performance?.recent_trades || [];
  document.getElementById('trades-table-body').innerHTML = trades.length ? trades.map(trade => `
    <tr>
      <td>${escapeHtml(safe(trade.exit_time).replace('T',' ').slice(0,16))}</td>
      <td>${tradeSymbolCell(trade, exchangeMap)}</td>
      <td>${escapeHtml(trade.strategy)}</td>
      <td>${escapeHtml(fmtSide(trade))}</td>
      <td>${escapeHtml(safe(trade.qty))}</td>
      <td>${fmtNum(trade.entry_price, 2)}</td>
      <td>${fmtNum(trade.exit_price, 2)}</td>
      <td><span class="${pnlClass(trade.realized_pnl)}">${fmtMoney(trade.realized_pnl)}</span></td>
      <td>${fmtPct(trade.return_pct)}</td>
      <td>${fmtNum(trade.hold_minutes, 1)}</td>
      <td>${escapeHtml(safe(trade.reason))}</td>
    </tr>
  `).join('') : `<tr><td colspan="11"><div class="empty-state" style="min-height:96px;">No closed trades yet.</div></td></tr>`;

  const perf = data?.performance || {};
  const diag = [
    ['Strategy', safe(data?.strategy)],
    ['Message', safe(data?.message)],
    ['Trading Block', safe(data?.trading_blocked_reason)],
    ['Management Active', data?.management_active ? 'Yes' : 'No'],
    ['Screening Active', data?.screening_active ? 'Yes' : 'No'],
    ['Streaming Active', data?.streaming_active ? 'Yes' : 'No'],
    ['History Symbols', safe(data?.data?.history_symbols)],
    ['Stream Symbols', safe((data?.data?.stream_symbols || []).join(', '))],
    ['Quote Symbols', safe((data?.data?.quote_symbols || []).length)],
    ['Schwabdev Calls / Min', fmtNum(data?.api_usage?.avg_calls_per_minute, 1)],
    ['Schwabdev Calls Total', safe(data?.api_usage?.total_calls)],
    ['Last Schwabdev Call', safe(data?.api_usage?.last_call_at).replace('T', ' ').slice(0, 19)],
    ['Closed Trades', safe(perf.closed_trades)],
    ['Wins / Losses', `${safe(perf.wins)} / ${safe(perf.losses)}`],
    ['Average Trade', fmtMoney(perf.average_trade)],
    ['Max Drawdown', fmtMoney(perf.max_drawdown)],
  ];
  document.getElementById('diagnostics-grid').innerHTML = diag.map(([k, v]) => `
    <div class="detail-card">
      <div class="tiny-label">${escapeHtml(k)}</div>
      <div class="value-big" style="font-size:22px; margin-top:8px;">${escapeHtml(v)}</div>
    </div>
  `).join('');
  syncDockViewport();
  wireDockWheel();
}

function syncDockViewport() {
  const dock = document.querySelector('.dock');
  const body = dock?.querySelector('.dock-body');
  const head = dock?.querySelector('.panel-head');
  if (!dock || !body || !head) return;
  const innerHeight = dock.clientHeight;
  const headHeight = head.offsetHeight + 12;
  const bodyHeight = Math.max(0, innerHeight - 32 - headHeight);
  body.style.height = `${bodyHeight}px`;
  body.style.maxHeight = `${bodyHeight}px`;
}

function initDockAutoResize() {
  const dock = document.querySelector('.dock');
  if (!dock) return;
  if (dock.dataset.hoverSyncBound !== '1') {
    dock.dataset.hoverSyncBound = '1';
    const resync = () => window.requestAnimationFrame(() => syncDockViewport());
    dock.addEventListener('mouseenter', resync);
    dock.addEventListener('mouseleave', resync);
  }
  if (dock.dataset.resizeObserverBound !== '1' && 'ResizeObserver' in window) {
    dock.dataset.resizeObserverBound = '1';
    const observer = new ResizeObserver(() => syncDockViewport());
    observer.observe(dock);
  }
}

function syncWatchlistViewport() {
  const panel = document.querySelector('.watchlist-panel');
  const rail = panel?.closest('.rail');
  if (!panel || !rail || window.innerWidth <= 1400) {
    if (panel) {
      panel.style.removeProperty('--watchlist-base-h');
      panel.style.removeProperty('--watchlist-hover-h');
    }
    return;
  }
  if (appState.mainPanelExpanded && getComputedStyle(document.documentElement).getPropertyValue('--watchlist-locked-h').trim()) {
    return;
  }
  const railStyles = getComputedStyle(rail);
  const gap = parseFloat(railStyles.rowGap || railStyles.gap || '0') || 0;
  const baseHeight = Math.max(0, (rail.clientHeight - gap) / 2);
  const rect = panel.getBoundingClientRect();
  const hoverHeight = appState.mainPanelExpanded
    ? baseHeight
    : Math.max(baseHeight, window.innerHeight - rect.top - 14);
  panel.style.setProperty('--watchlist-base-h', `${Math.round(baseHeight)}px`);
  panel.style.setProperty('--watchlist-hover-h', `${Math.round(hoverHeight)}px`);
}

function initWatchlistAutoResize() {
  const panel = document.querySelector('.watchlist-panel');
  const rail = panel?.closest('.rail');
  if (!panel || !rail) return;
  if (panel.dataset.hoverSyncBound !== '1') {
    panel.dataset.hoverSyncBound = '1';
    let collapseUnlockTimer = null;
    const root = document.documentElement;
    const clearCollapseState = () => {
      window.clearTimeout(collapseUnlockTimer);
      collapseUnlockTimer = null;
      root.classList.remove('watchlist-collapsing');
    };
    panel.addEventListener('mouseenter', () => {
      clearCollapseState();
      window.requestAnimationFrame(syncWatchlistViewport);
    });
    panel.addEventListener('mouseleave', () => {
      root.classList.add('watchlist-collapsing');
      window.requestAnimationFrame(syncWatchlistViewport);
      window.clearTimeout(collapseUnlockTimer);
      collapseUnlockTimer = window.setTimeout(() => {
        root.classList.remove('watchlist-collapsing');
        collapseUnlockTimer = null;
      }, 220);
    });
  }
  if (panel.dataset.resizeObserverBound !== '1' && 'ResizeObserver' in window) {
    panel.dataset.resizeObserverBound = '1';
    const observer = new ResizeObserver(() => syncWatchlistViewport());
    observer.observe(rail);
  }
  syncWatchlistViewport();
}

function syncCandidatesViewport() {
  const panel = document.querySelector('.candidates-panel');
  const rail = panel?.closest('.rail');
  if (!panel || !rail || window.innerWidth <= 1400) {
    if (panel) {
      panel.style.removeProperty('--candidates-base-h');
      panel.style.removeProperty('--candidates-hover-h');
    }
    return;
  }
  if (appState.mainPanelExpanded && getComputedStyle(document.documentElement).getPropertyValue('--candidates-locked-h').trim()) {
    return;
  }
  const railStyles = getComputedStyle(rail);
  const gap = parseFloat(railStyles.rowGap || railStyles.gap || '0') || 0;
  const baseHeight = Math.max(0, (rail.clientHeight - gap) / 2);
  const railRect = rail.getBoundingClientRect();
  const rect = panel.getBoundingClientRect();
  const hoverHeight = appState.mainPanelExpanded
    ? baseHeight
    : Math.max(baseHeight, rect.bottom - railRect.top);
  panel.style.setProperty('--candidates-base-h', `${Math.round(baseHeight)}px`);
  panel.style.setProperty('--candidates-hover-h', `${Math.round(hoverHeight)}px`);
}

function initCandidatesAutoResize() {
  const panel = document.querySelector('.candidates-panel');
  const rail = panel?.closest('.rail');
  if (!panel || !rail) return;
  if (panel.dataset.hoverSyncBound !== '1') {
    panel.dataset.hoverSyncBound = '1';
    panel.addEventListener('mouseenter', () => window.requestAnimationFrame(syncCandidatesViewport));
    panel.addEventListener('mouseleave', () => window.requestAnimationFrame(syncCandidatesViewport));
  }
  if (panel.dataset.resizeObserverBound !== '1' && 'ResizeObserver' in window) {
    panel.dataset.resizeObserverBound = '1';
    const observer = new ResizeObserver(() => syncCandidatesViewport());
    observer.observe(rail);
    observer.observe(panel);
  }
  syncCandidatesViewport();
}

function syncPositionsViewport() {
  const panel = document.querySelector('.positions-panel');
  const slot = document.querySelector('.positions-slot');
  const rail = slot?.closest('.right-rail');
  if (!panel || !slot || !rail || window.innerWidth <= 1400) {
    if (slot) {
      slot.style.removeProperty('--positions-base-h');
      slot.style.removeProperty('--positions-hover-h');
    }
    return;
  }
  if (appState.mainPanelExpanded && getComputedStyle(document.documentElement).getPropertyValue('--positions-locked-h').trim()) {
    return;
  }
  const railRect = rail.getBoundingClientRect();
  const railStyles = getComputedStyle(rail);
  const gap = parseFloat(railStyles.rowGap || railStyles.gap || '0') || 0;
  const prev = slot.previousElementSibling;
  const prevRect = prev ? prev.getBoundingClientRect() : railRect;
  const baseTop = prev === null ? railRect.top : Math.min(railRect.bottom, prevRect.bottom + gap);
  const baseHeight = Math.max(0, Math.min(railRect.height, railRect.bottom - baseTop));
  const hoverHeight = appState.mainPanelExpanded ? baseHeight : Math.max(baseHeight, railRect.height);
  slot.style.setProperty('--positions-base-h', `${Math.round(baseHeight)}px`);
  slot.style.setProperty('--positions-hover-h', `${Math.round(hoverHeight)}px`);
}

function initPositionsAutoResize() {
  const panel = document.querySelector('.positions-panel');
  const slot = document.querySelector('.positions-slot');
  const rail = slot?.closest('.right-rail');
  if (!panel || !slot || !rail) return;
  if (slot.dataset.hoverSyncBound !== '1') {
    slot.dataset.hoverSyncBound = '1';
    slot.addEventListener('mouseenter', () => window.requestAnimationFrame(syncPositionsViewport));
    slot.addEventListener('mouseleave', () => window.requestAnimationFrame(syncPositionsViewport));
  }
  if (slot.dataset.resizeObserverBound !== '1' && 'ResizeObserver' in window) {
    slot.dataset.resizeObserverBound = '1';
    const observer = new ResizeObserver(() => syncPositionsViewport());
    observer.observe(rail);
    Array.from(rail.children).forEach((child) => observer.observe(child));
    observer.observe(panel);
  }
  syncPositionsViewport();
}

function renderDisconnected(message) {
  appState.data = null;
  appState.snapshotMap = new Map();
  resetExpandedChartCache();
  resetCompactChartCache();
  document.getElementById('app-root').classList.add('status-disconnected');
  renderTopbar({ status: 'disconnected', dry_run: true, message: message || 'Dashboard disconnected' });
  renderKpiAndGauges(null);
  document.getElementById('watchlist-list').innerHTML = `<div class="empty-state">${escapeHtml(message || 'Dashboard disconnected.')}</div>`;
  document.getElementById('candidate-grid').innerHTML = `<div class="empty-state">No candidate data.</div>`;
  document.getElementById('positions-cards').innerHTML = `<div class="empty-state">No position data.</div>`;
  document.getElementById('events-list').innerHTML = `<div class="empty-state">${escapeHtml(message || 'Dashboard disconnected.')}</div>`;
  document.getElementById('trades-table-body').innerHTML = `<tr><td colspan="11"><div class="empty-state">No trade data.</div></td></tr>`;
  document.getElementById('diagnostics-grid').innerHTML = `<div class="empty-state">No diagnostics available.</div>`;
  renderSelectedSymbol();
  drawSelectedChart(null);
}

function renderApp() {
  const data = appState.data;
  if (!data) return;
  appState.snapshotMap = buildSnapshotMap(data);
  appState.expandedChart.maxBars = expandedChartMaxBars(data);
  appState.compactChart.maxBars = compactChartMaxBars(data);
  document.getElementById('app-root').classList.remove('status-disconnected');
  syncSelectedSymbol(data);
  syncExpandedChartFromBaseSnapshot(data);
  syncCompactChartFromBaseSnapshot(data);
  renderTopbar(data);
  renderWatchlist();
  renderCandidates();
  renderKpiAndGauges(data);
  renderPositions(data);
  renderSelectedSymbol();
  if (!appState.mainPanelExpanded) ensureCompactChartBars(false).catch(err => console.warn('Compact chart fetch failed', err));
  renderEventsAndDock();
  syncWatchlistViewport();
  syncCandidatesViewport();
  syncPositionsViewport();
}

async function refresh() {
  if (appState.refreshInFlight) return;
  appState.refreshInFlight = true;
  // Without a timeout a stuck backend blocks refresh forever — the
  // refreshInFlight guard becomes a permanent UI freeze with no visual
  // indication. Abort at 5× refresh so a missed tick surfaces as a
  // disconnect state that the user can see.
  const abortCtl = new AbortController();
  const timeoutMs = Math.max(4000, REFRESH_MS * 5);
  const timeoutId = setTimeout(() => abortCtl.abort(), timeoutMs);
  try {
    const res = await fetch('/api/state?ts=' + Date.now(), { cache: 'no-store', signal: abortCtl.signal });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    appState.data = data;
    try {
      renderApp();
      pruneExpandedChartCache();
      if (appState.mainPanelExpanded && (!Array.isArray(appState.expandedChart.bars) || !appState.expandedChart.bars.length)) {
        scheduleExpandedChartRefresh(false);
      }
    } catch (renderErr) {
      console.error('Dashboard render failed', renderErr);
      renderDisconnected('Dashboard render failed: ' + renderErr);
    }
  } catch (err) {
    const isAbort = err && (err.name === 'AbortError' || String(err).includes('abort'));
    renderDisconnected(isAbort ? `Dashboard fetch timed out after ${Math.round(timeoutMs / 1000)}s` : ('Dashboard connection failed: ' + err));
  } finally {
    clearTimeout(timeoutId);
    appState.refreshInFlight = false;
  }
}

document.querySelectorAll('.toggle-btn[data-filter]').forEach(btn =>
  btn.addEventListener('click', () => setFilter(btn.dataset.filter))
);
document.querySelectorAll('.tab-btn[data-tab]').forEach(btn =>
  btn.addEventListener('click', () => setDockTab(btn.dataset.tab))
);
document.querySelectorAll('.chart-timeframe-btn[data-timeframe-mode]').forEach(btn =>
  btn.addEventListener('click', () => setExpandedChartTimeframeMode(btn.dataset.timeframeMode))
);
wireDockWheel();
initDockAutoResize();
initWatchlistAutoResize();
initCandidatesAutoResize();
initPositionsAutoResize();
bindMainPanelHover();
let viewportResizeUnlockTimer = null;
let positionsHoverResumeHandlerBound = false;
let lastPointerClientX = null;
let lastPointerClientY = null;
function recordPointerPosition(event) {
  if (!event) return;
  if (Number.isFinite(event.clientX)) lastPointerClientX = event.clientX;
  if (Number.isFinite(event.clientY)) lastPointerClientY = event.clientY;
}
function pointerIsOutsidePositionsSlot(event = null) {
  const slot = document.querySelector('.positions-slot');
  if (!slot) return true;
  const clientX = Number.isFinite(event?.clientX) ? event.clientX : lastPointerClientX;
  const clientY = Number.isFinite(event?.clientY) ? event.clientY : lastPointerClientY;
  if (!Number.isFinite(clientX) || !Number.isFinite(clientY)) return true;
  const rect = slot.getBoundingClientRect();
  return clientX < rect.left || clientX > rect.right || clientY < rect.top || clientY > rect.bottom;
}
function resumePositionsHoverAfterPointerMove(event) {
  const root = document.documentElement;
  if (!root.classList.contains('positions-hover-paused')) {
    window.removeEventListener('pointermove', resumePositionsHoverAfterPointerMove, true);
    positionsHoverResumeHandlerBound = false;
    return;
  }
  if (!event || !pointerIsOutsidePositionsSlot(event)) {
    return;
  }
  root.classList.remove('positions-hover-paused');
  syncPositionsViewport();
  window.removeEventListener('pointermove', resumePositionsHoverAfterPointerMove, true);
  positionsHoverResumeHandlerBound = false;
}
function handleViewportResize() {
  const root = document.documentElement;
  root.classList.add('viewport-resizing');
  root.classList.add('positions-hover-paused');
  if (appState.mainPanelExpanded) {
    unlockExpandedSidePanelHeights();
  }
  syncDockViewport();
  syncWatchlistViewport();
  syncCandidatesViewport();
  syncPositionsViewport();
  if (appState.mainPanelExpanded) {
    window.requestAnimationFrame(() => {
      if (appState.mainPanelExpanded) lockExpandedSidePanelHeights();
    });
  }
  if (appState.data) renderSelectedSymbol();
  window.clearTimeout(viewportResizeUnlockTimer);
  viewportResizeUnlockTimer = window.setTimeout(() => {
    root.classList.remove('viewport-resizing');
    if (appState.mainPanelExpanded) {
      unlockExpandedSidePanelHeights();
      syncWatchlistViewport();
      syncCandidatesViewport();
      syncPositionsViewport();
      window.requestAnimationFrame(() => {
        if (appState.mainPanelExpanded) lockExpandedSidePanelHeights();
      });
    } else {
      syncPositionsViewport();
    }
    if (pointerIsOutsidePositionsSlot()) {
      root.classList.remove('positions-hover-paused');
      syncPositionsViewport();
    } else if (!positionsHoverResumeHandlerBound) {
      window.addEventListener('pointermove', resumePositionsHoverAfterPointerMove, true);
      positionsHoverResumeHandlerBound = true;
    }
  }, 180);
}
window.addEventListener('pointermove', recordPointerPosition, true);
window.addEventListener('resize', handleViewportResize);
setDockTab('events');
syncDockViewport();
syncWatchlistViewport();
syncCandidatesViewport();
syncPositionsViewport();
renderChartTimeframeToggle();
refresh();
setInterval(refresh, REFRESH_MS);
// Force an immediate refresh when the user returns to a hidden tab.
// Browsers throttle setInterval to ~1Hz when hidden; without this,
// users see stale data for up to one full REFRESH_MS cycle on focus.
// `refreshInFlight` already guards against doubling.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});
