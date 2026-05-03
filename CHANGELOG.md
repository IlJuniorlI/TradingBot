# Changelog

All notable changes to `intraday-tv-schwab-bot` will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `support_resistance.order_block_min_thrust_atr_mult` (default `0.75`)
  — minimum thrust requirement for OB acceptance. The break-of-structure
  thrust (close-to-close move from the OB candle to the breakout candle)
  must be at least this fraction of ATR. Filters weak setups where a
  small candle randomly broke a recent high. Applies to both `loose`
  and `strict` detection modes, both 1m and HTF timeframes. All 18
  shipped preset configs expose the knob; existing OB tuning is
  unaffected.
- `OrderBlock` dataclass gained four diagnostic fields used for
  strength-based ranking and dashboard introspection:
  `anchor_index` (origin bar position for age computation), `age_bars`
  (how many bars old the OB is), `thrust_atr` (computed thrust
  magnitude), `strength_score` (composite score for ranking when
  `order_block_max_per_side` clips). Internal only — not exposed in
  dashboard payloads, no public API change.
- `_fetch_raw_option_chain(client, symbol)` on the 0DTE option strategy
  classes — pure I/O + cache helper that fetches and caches the full
  unfiltered option chain. `_fetch_filtered_contracts` now delegates to
  it, and the new `_prefetch_option_chains` warm-up path uses it
  directly so the parallel prefetch no longer runs the put/call +
  liquidity filter just to discard the result.
- `entry_gatekeeper.broker_position_row` and `broker_position_rows`
  accept a `force_refresh` keyword. Default returns the cycle-cached
  snapshot (cf. cycle-scoped broker positions cache below);
  `force_refresh=True` bypasses the cache and issues a fresh
  `account_details` fetch for callers that need post-`place_order`
  state without waiting for the next cycle. Backed by a new
  `_fetch_broker_positions_uncached()` helper that also serves as the
  single source of truth for the underlying Schwab call shape (the
  cycle-cache helper now delegates to it).
- Always-on operation: three new `RuntimeConfig` knobs let the bot
  run continuously across days instead of exiting at session close.
  - `runtime.idle_sleep_seconds` (default `60.0`) — outside the 7am-8pm
    ET equity stream window the main loop sleeps this long instead of
    `loop_sleep_seconds`. Cuts overnight CPU by ~95% (60s vs 2s per
    cycle); set ≤ `loop_sleep_seconds` to disable.
  - `runtime.symbol_state_prune_seconds` (default `1800.0`) — cadence
    at which the engine evicts per-symbol state (history frames,
    HTF/SR caches, dashboard snapshot/chart payloads) for symbols no
    longer in the active set (streamed + watchlist + open positions).
    Long-running multi-day bots otherwise accumulate ~240KB per
    dropped 1m frame indefinitely. Set to `0` to disable.
  - `runtime.session_reconcile_on_resume` (default `true`) — re-runs
    the startup reconcile on the first cycle of each new ET trading
    day after streaming returns. Catches positions that closed
    overnight via the Schwab app or broker-side stops; without this,
    an always-on bot would wake at 7am still believing those positions
    are open and try to manage phantoms.
- Daily session archive: `_maybe_export_session_archive` fires once
  per ET trading day after the stream window closes (8pm ET) so an
  always-on bot writes a per-day `{log_dir}/sessions/{YYYY-MM-DD}/`
  archive on a per-day cadence instead of only on shutdown. Reuses
  the existing `runtime.export_session_archive` master switch.
  Shutdown still always writes its own archive (potentially
  overwriting today's bundle with a fresher snapshot).
- Engine main-loop resilience: exponential backoff (2× per
  consecutive `step()` error, capped at 60s) plus log throttling
  (full traceback for the first 3 errors and every 10th after,
  one-line warning otherwise) replaces the previous unconditional
  `LOG.exception` + tight 2s retry. Keeps a flapping API from
  filling the log file overnight.
- Memory-pressure pruning: `MarketDataStore.prune_inactive_symbols`
  and `DashboardCache.prune_inactive_symbols` evict per-symbol
  entries (history, live, HTF/SR caches, quote tracking, snapshot
  cache, chart cache) for symbols that are no longer streamed,
  watchlisted, or held. Engine wires these via
  `_maybe_prune_inactive_symbols` on the configurable cadence above.
- Session-rollover counter reset: `_maybe_session_rollover_reset`
  clears `entry_gatekeeper.session_skip_counts` when the ET trading
  date changes. Mirrors the per-day reset `RiskManager` already does
  for `realized_pnl`, so each daily archive's `session_skip_counts`
  reflects only that day's tally instead of the cumulative total.
- Dashboard chart touch-input support: chart canvas now uses pointer
  events (`onpointermove` / `onpointerdown` / `onpointerleave` /
  `onpointercancel`) so hover/tooltip updates fire for both mouse
  and touch interactions. Tap shows the tooltip; drag moves it; tap
  persists until the next gesture (touch flow does not auto-clear).
  `touch-action: pan-y` on `#market-chart` lets vertical page scroll
  pass through while capturing horizontal swipes for hover.
- Dashboard small-phone fallback: new `@media (max-width: 480px)`
  block in `dashboard.css` collapses topbar/status-strip/grid layouts
  to a single column, bumps interactive elements to 44×44px tap
  targets, and allows table cells to wrap. The dedicated `/mobile`
  route is still the preferred phone experience, but viewers loading
  `/` on a phone now get a sane single-column layout.
- Dashboard chart timezone constant: hardcoded `DASHBOARD_TIMEZONE =
  'America/New_York'` passed to all chart timestamp formatters
  (`fmtChartTs`, `formatTimeAxisLabel`, `formatDayAxisLabel`) so
  labels match the bot's session calendar regardless of viewer
  locale. Without this, browsers in other timezones would render
  bar labels in their local TZ.
- Dashboard tab-visibility refresh: both desktop (`dashboard.js`)
  and mobile (`mobile.js`) now register `visibilitychange`
  listeners that force an immediate `refresh()` on tab return.
  Browsers throttle `setInterval` to ~1Hz when hidden; without this,
  users see stale data for up to one full refresh cycle on focus.
- `runtime.max_consecutive_quote_failures` (default `5`) — per-symbol
  quote-fetch failure threshold on `MarketDataStore`. After the
  threshold is crossed, the symbol is silenced from quote refresh for
  the rest of the session and recovers on bot restart. Set to `0` to
  preserve prior always-retry behavior. Prevents wasted Schwab API
  calls when a symbol returns a persistent symbol-specific error
  (e.g., 401 on a restricted security).
- `_strategies.insufficient_bars_reason` (no leading underscore) is
  now part of the `_strategies` package's documented public API,
  exported from `_strategies/__init__.py`. Cross-package consumers
  should use `from ._strategies import insufficient_bars_reason`
  instead of reaching into `_strategies.helpers` directly.
- `config.__all__` now lists every public dataclass and function
  (20 entries, was 10 hand-curated). Adopts the mechanical
  "no-leading-underscore = public" rule so future additions are
  one-line entries with no judgment call.
- Dashboard chart support for order block overlays via two new
  `DashboardChartConfig` fields (per profile):
  `show_htf_order_blocks` and `show_1m_order_blocks` (both default
  `false`). When enabled — together with the matching
  `support_resistance.htf_order_blocks_enabled` /
  `one_minute_order_blocks_enabled` flag — the chart draws OB zones
  with a **dashed-line border around a very faint fill**, distinct
  from FVGs which render as solid-filled rectangles. Same green/red
  bullish/bearish color semantics so direction is still readable at
  a glance. Same cross-timeframe protection as FVGs (HTF OBs hidden
  on 1m charts, 1m OBs hidden on HTF charts). Backend reuses
  `dashboard_fvg_payload` since `OrderBlock` and `HTFFairValueGap`
  share the same field shape; payload includes `kind: "ob"` and
  `mode` (loose / strict) for downstream consumers.
- Order block detection at both 1-minute and HTF timeframes
  (`intraday_tv_schwab_bot/order_blocks.py`). Each timeframe has its
  own enable flag — `support_resistance.one_minute_order_blocks_enabled`
  and `support_resistance.htf_order_blocks_enabled` — but both share
  the six tuning knobs (`order_block_mode`, `order_block_max_per_side`,
  `order_block_min_atr_mult`, `order_block_min_pct`,
  `order_block_pivot_span`, `order_block_new_high_lookback`). HTF OBs
  use `support_resistance.timeframe_minutes` (default 15m) and resample
  the 1m frame internally. Two detection modes via `order_block_mode`:
  `"loose"` finds the last opposite-color candle before any close that
  prints a new local high/low; `"strict"` requires a formal
  break-of-structure event detected via the same pivot-span swing
  detector used by support/resistance. Disabled by default. Wicks
  below the OB lower (or above OB upper for bearish) are tolerated as
  long as the bar closes back inside the zone — only a close beyond
  the far boundary invalidates the OB. New `BaseStrategy` methods
  `_one_minute_order_block_context`, `_htf_order_block_context`,
  `_continuation_ob_retest_plan` (currently consumes 1m OBs), and
  `_apply_continuation_zone_retest_plans` (OR-combine helper for FVG
  + OB plans). Reuses every `anti_chase_fvg_retest_*` knob for
  bar-confirmation thresholds — same rules as FVG. Mirrors the FVG
  architecture where HTF FVGs are detected for context but only 1m
  FVGs gate retest entries.
- `anti_chase_fvg_retest_skip_vwap_ema9_reclaim` strategy param
  (default `false`). When `true`, drops the trend-MA half of the
  bullish-FVG `reclaimed` clause (and corresponding `<=max(...)` for
  shorts), keeping only the FVG-midpoint reclaim plus `bar_confirm`
  shape. Lets microcap squeeze strategies fire on deep retests where
  VWAP/EMA9 lag well above the FVG zone.

### Changed

- Order block ranking is now strength-based (thrust × size × age ×
  validity) instead of nearest-to-current-price. When
  `order_block_max_per_side` clips, the strongest OBs survive — small-
  move noise OBs no longer displace strong OBs from real moves, which
  was the bug that motivated the new `order_block_min_thrust_atr_mult`
  filter and ranking score. `nearest_bullish_ob` /
  `nearest_bearish_ob` accessors still resolve to the nearest OB by
  price for backward compatibility with `_continuation_ob_retest_plan`
  consumers (whose retest semantics genuinely want "closest to price",
  not "strongest").
- `OrderBlock` enrichment (per-cycle `age_bars` + `strength_score`
  recompute) now uses `dataclasses.replace()` instead of in-place
  attribute assignment. Eliminates PyCharm `__slots__` warnings on the
  slotted dataclass and ensures any future stale-bytecode would fail
  loudly at construction rather than silently dropping new attributes.
  Merged OBs preserve `min(anchor_index)` and `max(thrust_atr)` across
  the merge so strength sort doesn't degenerate when adjacent zones
  collapse.
- `peer_confirmed_key_levels` preset disables 1m + HTF order block
  detection (`one_minute_order_blocks_enabled: false`,
  `htf_order_blocks_enabled: false`) and the matching dashboard
  `show_*_order_blocks` flags in both compact and expanded layouts.
  The strategy's custom entry pipeline doesn't consume OBs at all, so
  detecting and rendering them was wasted compute on every cycle.
- Dashboard HTTP server now uses HTTP/1.1 keep-alive
  (`protocol_version = "HTTP/1.1"`) with a 30-second idle timeout
  (`Handler.timeout = 30`). Polling and asset fetches share one
  TCP+TLS connection per browser tab instead of opening a fresh pair
  per request — eliminates the first-load stall under HTTPS where the
  browser would queue 6 parallel asset fetches behind serial TLS
  handshakes. TLS handshake is now deferred off the accept thread
  (`do_handshake_on_connect=False` on the listening socket + an
  explicit `setup()` override that runs `do_handshake()` with a 5-second
  timeout on the per-request worker thread) so concurrent clients can
  handshake in parallel. New `ReusableThreadingHTTPServer.handle_error`
  override silences common transport-layer exceptions (`ssl.SSLError`,
  `ConnectionError`, `BrokenPipeError`, `socket.timeout`) at DEBUG
  level instead of dumping full tracebacks to journald on every
  port-scanner connection or aborted handshake.
- `entry_gatekeeper` adds a cycle-scoped broker positions cache:
  `account_details` is fetched at most once per `engine.step()`
  regardless of how many `broker_position_row` /
  `broker_position_rows` consumers run inside the cycle (entry gate +
  exit recovery via `position_manager`'s injected callable). 5
  in-cycle signal/position checks = 1 Schwab call instead of 5
  identical fetches. Failure latches per-cycle (`_cycle_positions_failed`)
  to avoid retry-storms during Schwab outages. New `begin_cycle()` /
  `end_cycle()` lifecycle hooks wired into `engine.step()` mirror the
  per-cycle FVG/OB/S-R caches in `data_feed.py`.
- 0DTE strategies (`zero_dte_etf_options`, `zero_dte_etf_long_options`)
  parallel-prefetch option chains for all qualifying candidates at the
  start of each `entry_signals` pass via a `ThreadPoolExecutor` (up to
  4 workers, scaled to the miss count). The sequential per-candidate
  build loop then hits the warm cache. For 3-5 cache-miss candidates
  this collapses ~450-750ms of stacked serial Schwab `option_chains`
  calls into one ~150ms parallel batch. Inherited automatically by
  the long-options subclass.
- `startup_reconciler.reconcile` parallelizes the `account_details`
  (positions) and `account_orders` (working orders) Schwab calls via
  a 2-worker `ThreadPoolExecutor`. Saves ~200-400ms of boot stall
  before the engine enters its first scan cycle. Both calls wrapped
  in lambdas to sidestep PyCharm `ParamSpec` false positives that
  mismatched our args against `schwabdev.Client.place_order`'s
  dict-typed second arg.
- `_cycle_sleep_seconds()` now picks between `runtime.loop_sleep_seconds`
  (fast) inside the 7am-8pm ET stream window and
  `runtime.idle_sleep_seconds` (idle) outside it. Every Schwab equity
  order session lies entirely within the stream window, so "stream
  off" implies "no order session" — no third cadence to handle.
- `dashboard_recent_trade_markers()` and `dashboard_symbol_trade_signature()`
  now filter trades by symbol BEFORE slicing, and the marker function
  also filters to today's ET session date. With the old order, a fresh
  fill on a long-quiet symbol could be invisible (when 12+ other
  tickers had traded after it) AND would not change the snapshot
  signature, leaving the cached snapshot stale. Multi-day deque entries
  also no longer leak yesterday's exits onto today's chart.
- Chart payload `last_update` is now re-stamped on every cache hit so
  the frontend's "last update" timestamp doesn't freeze for the
  lifetime of a cached payload. Was baked into the deep-copied cache
  entry and served unchanged.
- `peer_confirmed_key_levels` preset retuned for always-on operation
  (`auto_exit_after_session: false`, `startup_reconcile_mode:
  restore_hybrid` so overnight long holds survive crashes,
  entry/management/screener windows expanded to `07:00-19:55` ET to
  span the full Schwab stream window, `time_stop_minutes: 0` so the
  45-min "scratch if not moving" rule doesn't close runners that
  cross sessions). The strategy's per-cycle filters (min trigger
  score, peer agreement, macro net bias) gate low-quality
  extended-hours candidates organically.
- All 18 yaml presets now expose the three new always-on knobs
  (`idle_sleep_seconds`, `symbol_state_prune_seconds`,
  `session_reconcile_on_resume`). README runtime table + behavior
  section and `config.example.yaml` updated with explanatory
  comments.
- `:hover` rules on `.symbol-card`, `.candidate-tile`,
  `.position-card`, `.positions-slot`, and `.positions-slot >
  .positions-panel` now wrap in `@media (hover: hover)` so iOS taps
  no longer leave cards stuck in the hover-elevated state until the
  user taps elsewhere. Mouse pointers still get the transitions.
- Mobile dashboard poll cadence floor raised to 4000ms (vs 2000ms
  desktop default) — saves cellular radio cycling on phones in a
  pocket. Server-provided `dashboard.refresh_ms` is honored when
  it's already slower than the mobile floor.
- Removed redundant `dashboard_candidate_levels` override in
  `peer_confirmed_htf_pivots` — it returned `[]` matching the base
  class default. The HTF strategy's actual dashboard payload flows
  through `dashboard_overlay_candidates` (unchanged).
- Removed `BaseStrategy._apply_continuation_fvg_retest_plan` — the
  single-plan apply helper that predated the OR-combine refactor.
  All four remaining callers (`momentum_close`, `opening_range_breakout`,
  `rth_trend_pullback`, `volatility_squeeze_breakout` — 12 call sites)
  now use the multi-plan `_apply_continuation_zone_retest_plans` with
  a single-element plan list. Behavior is identical for the
  single-plan case (verified: any plan `status="allow"` and all
  reasons deferrable → clear; otherwise prefer wait over reject).
  Per the project's clean-breaks-over-shims convention.
- Collapsed the six FVG tuning knobs split across `htf_*` and
  `one_minute_*` prefixes (`htf_fair_value_gap_max_per_side`,
  `htf_fair_value_gap_min_atr_mult`, `htf_fair_value_gap_min_pct`,
  `one_minute_fair_value_gap_max_per_side`,
  `one_minute_fair_value_gap_min_atr_mult`,
  `one_minute_fair_value_gap_min_pct`) into three shared knobs:
  `fair_value_gap_max_per_side`, `fair_value_gap_min_atr_mult`,
  `fair_value_gap_min_pct`. Both 1m and HTF FVG detection now read
  from these single fields. The two enable flags
  (`htf_fair_value_gaps_enabled`, `one_minute_fair_value_gaps_enabled`)
  remain timeframe-specific. All shipped preset configs had identical
  values across the htf/one_minute prefixes, so no existing tuning is
  lost. Per the project's clean-breaks-over-shims convention, the old
  field names are removed entirely from `SupportResistanceConfig` —
  any user configs that still reference them will need to be updated.
  This mirrors the OB knob consolidation in the same release.
- Order block context is now cycle-cached on `MarketDataStore` via a
  new `_cycle_ob_cache` and `get_order_block_context()` method that
  mirrors the existing FVG cache pattern. Both `BaseStrategy`'s
  `_one_minute_order_block_context` / `_htf_order_block_context` and
  `dashboard_cache.py` route through the cache so the same cycle's
  strategy candidate evaluation and dashboard chart payload share a
  single OB build per (symbol, timeframe, knob set). Eliminates
  ~260 redundant `build_order_block_context` calls per minute when
  strategy + dashboard are both consuming OBs. Cache invalidates on
  every stream bar (per-symbol) and at cycle boundaries.
- Engine cycle now threads per-symbol Schwab fetches in parallel.
  History, support/resistance refresh, and quote fallback run via the
  new `_parallel_symbol_map` and `_parallel_quote_fetch` helpers.
  Cache writes remain lock-protected; per-minute API call rate
  unchanged — only the burst pattern is compressed.
- Strategy context caches (`_chart_context`, `_structure_context`,
  `_technical_context`) are now pre-warmed in parallel before each
  cycle. `BaseStrategy._observed_contexts` lazily records the
  context shapes used on first invocation; the engine replays them
  via the parallel symbol map at the start of each cycle. Three
  `RLock`s guard the per-context caches. New public methods
  `reset_context_caches()` and `prime_cycle_contexts(frame, observed)`
  expose the priming hook.
- Strategy plugin scaffold (`scripts/scaffold_strategy_plugin.py`)
  now emits 9 FVG knobs plus `force_flatten` in the generated
  manifest, and SPDX headers in the scaffolded `__init__.py`,
  `strategy.py`, and `screener.py`.
- `top_tier_adaptive` now imports `now_et` and `parse_hhmm` from
  `..shared` like every other strategy instead of reaching into
  `...utils` directly. No behavior change; convention alignment only.
- Pure stateless helpers (numeric coercion, bar/DataFrame shape
  utilities, premium clamping, symbol-list normalization,
  reason-string formatters, structured-logging payload builder,
  dashboard zone-width policy) extracted from `BaseStrategy` into
  a new `_strategies/helpers.py` module (~622 LOC, 29 functions
  organized in 7 sections). `BaseStrategy` shrunk by ~350 LOC.
  ~570 internal callsites updated across 16 files to import the
  helpers from `..shared` (which re-exports them) instead of going
  through `self._method(...)`. Class-level delegation methods were
  removed entirely per the project's clean-breaks-over-shims
  convention. Test patches that previously used
  `patch.object(strategy, "_method", …)` must switch to
  `patch("intraday_tv_schwab_bot._strategies.strategy_base._method", …)`
  — patch the module function, not a class attribute.
- Top-level `intraday_tv_schwab_bot.__all__` trimmed from 29
  entries (28 of which were unreachable submodule names because
  `__init__.py` doesn't import them) down to `["__version__"]` to
  match what's actually exported. Explicit submodule imports
  (`from intraday_tv_schwab_bot import data_feed`, `from
  intraday_tv_schwab_bot.config import load_config`, etc.) continue
  to work as before.
- `.gitattributes` upgraded `* text=auto` → `* text=auto eol=lf`
  to force LF line endings in the index. Stops Windows checkouts
  with `core.autocrlf=true` from generating phantom-modified file
  states (where `git status` shows files as modified but `git diff`
  returns no actual content change).

### Fixed

- `peer_confirmed_key_levels._select_level` "touched zone" check
  was a window-wide `low.min() <= price+zone AND high.max() >=
  price-zone`, which could pass when no individual bar's range
  actually overlapped the zone — e.g. one bar entirely above + one
  entirely below the level both contributing their min/max. Replaced
  with per-bar overlap: `((recent["low"] <= price + zone) &
  (recent["high"] >= price - zone)).any()`. Trips infrequently in
  normal markets but can fire during news/fast-spike conditions and
  cause level selection to pick a zone that wasn't actually tested.
- `dashboard.js paint()` used `bars[hoverIndex]` / `xFor(hoverIndex)`
  inside the `activeIndex !== null` block. Today `pinnedIndex` is
  declared but never reassigned, so the bug doesn't fire — but the
  moment bar-pinning lands, `paint(null)` with a pinned bar would
  dereference `bars[null]` and crash the chart. Now uses
  `activeIndex` consistently for both bar reference and x-coordinate.
- `dashboard_cache.py log_component_failure` calls in the new OB
  collection blocks (added with the OB feature) passed the symbol
  as the printf message instead of as an arg — would log a single
  unhelpful token and TypeError if a symbol contained `%`. Both call
  sites now pass `"...failed for %s", symbol` matching the pattern
  used throughout the file.
- `data_feed.prune_inactive_symbols` now also evicts the
  `last_htf_refresh` tuple-keyed dict. Was missed in the original
  pass — minor leak (one datetime per (symbol, htf_minutes) pair)
  but unbounded for long multi-day runs.
- Engine `_cycle_sleep_seconds` collapsed from three branches to two
  (stream available → fast, else → idle). The third branch — "stream
  off but order session open" — was unreachable: every Schwab equity
  order session lies entirely inside the 7am-8pm stream window.
  Doc-comment narrative referencing a fictional "4am-7am extended-AM"
  window also rewritten; extended-AM actually starts at
  `EQUITY_STREAM_START` (7am).
- Dashboard volume-bar render: `Number(bar.volume || 0)` would coerce
  the truthy string `"NaN"` to `NaN`, propagate through `Math.sqrt`,
  and silently skip the bar via the `if (volH > 0)` guard. Now uses
  `parseFinite(bar.volume)` to convert non-finite inputs to a safe
  `0`. Cosmetic data loss; no crash.
- Chart `renderEmpty` now cancels any pending hover-RAF before
  detaching pointer handlers. Previously a queued
  `requestAnimationFrame` from the previous chart's hover could fire
  AFTER the canvas was cleared and handlers reset, drawing ghost
  data over the new blank state via the stale closure's
  `paint`/`updateTooltip`.
- Tap-and-release on touch devices flashed the chart tooltip briefly
  (pointerdown showed it, pointerleave/pointercancel hid it on finger
  lift). Both leave/cancel handlers now skip `clearPointerActivity`
  when `pointerType !== 'mouse'` so the tooltip persists until the
  next gesture (re-tap or drag). Mouse cursors leaving the canvas
  still clear immediately.
- Theme stylesheet `<link>` 404 would unset every CSS variable
  (`--bg`, `--text`, `--panel-bg`, etc.) and render the page nearly
  unstyled. Both `dashboard.html` and `mobile.html` now have
  `onerror="this.remove()"` on the theme stylesheet link so a
  missing theme.css falls back cleanly to the base styling. Mirrors
  the existing handling on the optional `theme.js` script tag.
- Mobile `.position-card` had `cursor: pointer` from the desktop
  stylesheet but `mobile.js` never wired a click handler — cards
  looked tappable but did nothing. Added `.m-shell .position-card
  { cursor: default; -webkit-tap-highlight-color: transparent; }`
  override, plus `-webkit-tap-highlight-color: transparent` on
  buttons and metric pills to suppress the iOS tap-flash.
- Mobile `.panel-meta` (the KPI sub-line "Day PnL ${...} · cash
  ${...}") wrapped awkwardly with 7-figure equity values. Added
  `white-space: normal; word-break: break-word; line-height: 1.4`
  override on `.m-shell .panel-meta`.
- Mobile `.positions-panel` previously relied on the desktop's
  `≤1400px` breakpoint to neutralize `position: fixed`. Added
  explicit `position: relative` override on `.m-shell
  .positions-panel` so positioning doesn't depend on the desktop's
  breakpoint contract.
- Mobile Qty rendering went through `escapeHtml(pos.qty)` which
  produced literal `"null"` for null qty values. Switched to
  `fmtInteger(pos.qty)` which renders `—` for null/non-numeric.
- Chart timestamp formatters (`fmtChartTs`, `formatTimeAxisLabel`,
  `formatDayAxisLabel`) used implicit browser-local timezone via
  `toLocaleString`/`toLocaleTimeString`. Server-side bar timestamps
  are ET-localized; browsers in other timezones rendered chart
  labels in their local TZ, mislabeling the market clock. All three
  now pass `timeZone: DASHBOARD_TIMEZONE` ('America/New_York').
- ORB screener `none`-mode activity score now uses
  `rvol × volume / 1_000_000` (was `rvol × volume`). Ranking is
  mathematically identical, but the rescaled magnitudes match the
  `premarket` and `early_session` branches and stop polluting logs
  and dashboard candidate lists with 6-7 digit values.
- Dashboard `focus-meta` line (the description under the selected
  symbol name) now uses a compact form of the entry-decision label
  that drops parameter detail after the first colon. Long
  ETF-options skip reasons no longer push the live-data chips on
  the right (Last / Change / Spread / Vol / timeframe toggle /
  TradingView link) off the edge of the card. Full detail still
  appears in the score-sub line below.
- `_optional_int` in `_strategies/helpers.py` now parses
  float-strings like `"3.7"` to `3` (via `int(float(value))`)
  instead of returning the default. Aligns with the wider bot's
  `safe_float→int` chain so a stringly-typed numeric config value
  no longer silently falls through.
- Strategy plugin scaffold and the `_strategies/README.md` plugin
  template both updated to call `insufficient_bars_reason(...)` /
  `_safe_float(...)` as free functions (imported from `..shared`)
  instead of the obsolete `self._insufficient_bars_reason(...)` /
  `self._safe_float(...)` method calls. Generated plugins from the
  scaffold now match the post-helpers.py-extraction architecture.
- Order block detection walk-back no longer aborts on the first
  failed candidate. The `_detect_order_blocks_loose` and
  `_detect_order_blocks_strict` walk-back loops used `break` when
  the immediate-prior opposite-color candle failed the size filter
  or had been invalidated, producing zero detections in cases where
  a tiny doji-bearish bar sits at idx-1 with a real bearish OB at
  idx-2+. Replaced with `continue` so the walk-back keeps searching
  up to `max_distance_back` bars away. Also fixed
  `_merge_order_blocks` first/last_seen reconciliation: sort is by
  price (`lower`), so the natural `last.first_seen` reflected
  price order, not chronology — replaced with explicit
  `_earlier`/`_later` ISO-timestamp helpers. Cosmetic for
  metadata; no functional impact on merged zone bounds.
- Order block knobs were previously defined in
  `SupportResistanceConfig` but never made it into the shipped
  yaml files (only the `show_*_order_blocks` chart toggles were
  added in the OB-rendering commit). The runtime worked via Python
  field defaults, but anyone reading a preset to discover tunable
  knobs would not see them. All 14 prod preset yamls + the example
  yaml now expose the eight OB knobs
  (`one_minute_order_blocks_enabled`, `htf_order_blocks_enabled`,
  `order_block_mode`, `order_block_max_per_side`,
  `order_block_min_atr_mult`, `order_block_min_pct`,
  `order_block_pivot_span`, `order_block_new_high_lookback`),
  defaults safe-off.
- Dashboard `selected-spread` pill no longer flickers between
  visible and hidden on each refresh. The chip toggled `.hidden`
  whenever `q.ask` or `q.bid` went transiently stale between
  stream ticks, causing the surrounding chip strip to jitter.
  Aligned with the sibling pills (`selected-price`,
  `selected-change`, `selected-volume`) which always stay visible
  and show their `—` placeholder when data is missing.
- Three IDE / type-checker warnings cleaned up: redundant
  `self.stream = None` between `close()` and reassignment in the
  date-rolling log handler in `utils.py` (type-checker complaint
  about `None` for `SupportsWrite[str]`); two unused `symbol` /
  `data` parameters on `_one_minute_order_block_context` (the 1m
  OB context now reads from the data store cache when available,
  so the params are wired through). The HTF sibling
  `_htf_order_block_context` continues to consume both params.

## [1.0.0] — 2026-04-24

Initial public release.

### Infrastructure

- `requirements.txt` strictly pinned to verified versions.
- `pyproject.toml` with setuptools build backend and dynamic version
  from `version.txt`.
- Tests maintained privately in the source tree; not shipped with this
  repository.

[Unreleased]: https://github.com/OWNER/REPO/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/OWNER/REPO/releases/tag/v1.0.0
