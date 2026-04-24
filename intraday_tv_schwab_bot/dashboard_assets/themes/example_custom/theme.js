// example_custom — minimal render loop against /api/state.
// Replace the bits below to build your own UI. The backend schema is the
// same one the base dashboard.js consumes; inspect /api/state in a browser
// to see what's available.
(() => {
  const refreshMs = (window.DASHBOARD_CONFIG && window.DASHBOARD_CONFIG.refreshMs) || 2000;

  const fmtMoney = (n) => {
    if (n === null || n === undefined || Number.isNaN(n)) return '—';
    const sign = n >= 0 ? '+' : '';
    return `${sign}$${Math.abs(n).toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
  };
  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  const setClass = (id, cls) => {
    const el = document.getElementById(id);
    if (el) { el.classList.remove('pos', 'neg'); if (cls) el.classList.add(cls); }
  };

  async function tick() {
    try {
      const resp = await fetch('/api/state', { cache: 'no-store' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const state = await resp.json();

      // Top bar
      setText('status', state.status || '—');
      const dayPnl = state.day_pnl ?? state.day_realized_pnl ?? null;
      setText('daypnl', fmtMoney(dayPnl));
      setClass('daypnl', dayPnl == null ? '' : (dayPnl >= 0 ? 'pos' : 'neg'));
      const trades = state.trade_count ?? (state.trades?.length ?? '—');
      setText('trades', trades);
      setText('updated', state.last_update ? `Updated ${state.last_update}` : 'Live');

      // Watchlist dump — trim down to show it's live
      const wl = state.watchlist || state.symbols || [];
      const dump = JSON.stringify(wl.slice ? wl.slice(0, 10) : wl, null, 2);
      const pre = document.getElementById('watchlist-dump');
      if (pre) pre.textContent = dump || '(empty)';
    } catch (err) {
      setText('updated', `Error: ${err.message}`);
    }
  }

  tick();
  setInterval(tick, refreshMs);
})();
