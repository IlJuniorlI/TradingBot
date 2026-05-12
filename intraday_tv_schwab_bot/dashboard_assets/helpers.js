// Shared formatting / DOM utilities used by both dashboard.js and mobile.js.
//
// Loaded BEFORE dashboard.js / mobile.js via `<script src="/assets/helpers.js" defer>`.
// All identifiers here are global (no IIFE), so consumers reference them
// directly: `numOrNull(x)`, `fmtNum(x, 2)`, `sparklineSVG(vals, tone)`.
//
// Canonical versions are sourced from the pre-extraction dashboard.js.
// mobile.js previously had simplified ports; those have been deleted in
// favor of these versions so both views render the same.

function parseFinite(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function numOrNull(value) {
  return parseFinite(value);
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
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

function fmtUptime(startedAt) {
  // Format elapsed time since `startedAt` (ISO string) as "Nd HH:MM:SS"
  // for runs ≥1 day, "HH:MM:SS" for sub-day, or "—" if not parseable.
  if (!startedAt) return '—';
  const startMs = Date.parse(String(startedAt));
  if (!Number.isFinite(startMs)) return '—';
  const elapsedSec = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
  const days = Math.floor(elapsedSec / 86400);
  const hh = String(Math.floor((elapsedSec % 86400) / 3600)).padStart(2, '0');
  const mm = String(Math.floor((elapsedSec % 3600) / 60)).padStart(2, '0');
  const ss = String(elapsedSec % 60).padStart(2, '0');
  return days > 0 ? `${days}d ${hh}:${mm}:${ss}` : `${hh}:${mm}:${ss}`;
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

function pnlClass(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return '';
  return n > 0 ? 'good' : 'bad';
}

// Class names must stay in sync with dashboard.css — ".mode-chip" and
// "status-starting" are NOT real classes there, so don't invent them.
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
  // vector-effect="non-scaling-stroke" keeps the line at 2.4 CSS px regardless of
  // how the viewBox stretches to fit the container. Without it, a 52px tall
  // watchlist sparkline and a 110px tall KPI sparkline would render at totally
  // different visual stroke widths (because preserveAspectRatio="none" stretches
  // strokes along with the coordinate system).
  return `<svg viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"></polyline><polygon points="${area}" fill="${stroke}" opacity="0.10"></polygon></svg>`;
}
