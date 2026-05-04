# Changelog

All notable changes to `intraday-tv-schwab-bot` will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **LTF/HTF separation cleanup.** The previous code conflated LTF
  (lower-timeframe / trigger frame) and HTF (higher-timeframe / SR
  context) via a silent override pattern: `support_resistance.timeframe_minutes`
  was treated as LTF in name but routinely used as HTF whenever a
  strategy declared `params.htf_timeframe_minutes`. This made it
  impossible to read a config and know what timeframe each block was
  really driving. Cleanup:
  - **Strategy params renamed** for clarity: `htf_timeframe_minutes`
    → `htf_minutes`, `trigger_timeframe_minutes` → `ltf_minutes`. The
    older names are removed entirely (per project's clean-breaks
    convention) — manifests, yaml configs, READMEs all migrated. 35
    files updated.
  - **`support_resistance.timeframe_minutes` is now the default HTF**
    used by SR detection, key-level zones, dashboard sidebar S/R list,
    and engine entry/exit gating. Strategies that operate on a
    different HTF override per-strategy via `params.htf_minutes`.
  - **LTF defaults to 1-minute streaming bars** when a strategy doesn't
    declare `params.ltf_minutes`. Strategies with a distinct intraday
    trigger candle (e.g. `peer_confirmed_key_levels` uses 5-min
    triggers) declare it explicitly.
  - **Helper rename**: `_active_sr_*` / `active_sr_*` / `_sr_*`
    accessors → `_active_htf_*` / `active_htf_*` / `_htf_*`. Each is
    explicit about reading HTF; the old names hid that. New parallel
    `_active_ltf_minutes` / `_ltf_minutes` accessors expose the LTF
    timeframe. The override fallback pattern (read params first, fall
    back to support_resistance block) is preserved — only the names
    are now honest.
  - **Dashboard chart "ltf" mode** renders at the strategy's LTF
    instead of hardcoded 1-minute bars. For `peer_confirmed_key_levels`
    that's 5-min bars; for simpler strategies still 1-min. Mode value
    `"1m"` is accepted as a back-compat alias for `"ltf"` for one
    release, then dropped. Frontend `dashboard.js` migrated to the
    canonical `"ltf"` value.
  - **No trading behavior change for stops/exits/risk** — these were
    already getting HTF via the override; now they get HTF explicitly.
    Streaming responsiveness preserved (flip frame is always 1m,
    live price for stop trigger is always 1m, regardless of HTF).
- **Always-on operation.** Bot now runs continuously across days instead of
  exiting at session close. Three new `RuntimeConfig` knobs:
  `idle_sleep_seconds` (default `60.0`, ~95% overnight CPU savings via
  outside-stream-window cadence), `symbol_state_prune_seconds` (default
  `1800.0`, evicts per-symbol state for inactive symbols on the configured
  cadence — `MarketDataStore.prune_inactive_symbols` /
  `DashboardCache.prune_inactive_symbols`), and `session_reconcile_on_resume`
  (default `true`, re-runs startup reconcile on the first cycle of each new
  ET trading day to catch overnight position changes). Daily session archive
  now fires once per ET trading day after the stream window closes (8pm ET)
  in addition to shutdown. Engine main-loop resilience: exponential backoff
  (2× per consecutive `step()` error, capped at 60s) plus log throttling
  replaces the previous tight 2s retry. Session-rollover hook clears
  `entry_gatekeeper.session_skip_counts` on ET trading-date change so daily
  archives reflect that day's tally only.
- **Order blocks** (`order_blocks.py`). Detection at both 1-minute and HTF
  timeframes with two modes (`loose` / `strict`). Eight knobs in
  `SupportResistanceConfig`: `{ltf,htf}_order_blocks_enabled` enable
  flags plus shared `order_block_mode`, `order_block_max_per_side`,
  `order_block_min_atr_mult`, `order_block_min_pct`,
  `order_block_min_thrust_atr_mult` (default `0.75` — break-of-structure
  thrust filter), `order_block_pivot_span`, and
  `order_block_new_high_lookback`. Strength-based ranking (thrust × size ×
  age × validity) when `max_per_side` clips. New `BaseStrategy` methods:
  `_ltf_order_block_context`, `_htf_order_block_context`,
  `_continuation_ob_retest_plan`, and `_apply_continuation_zone_retest_plans`
  (OR-combine FVG + OB plans). Dashboard chart overlays (dashed border, faint
  fill) via per-profile `show_htf_order_blocks` / `show_ltf_order_blocks` flags;
  cross-timeframe protection mirrors FVG behavior. All 18 shipped presets
  expose the eight OB knobs (defaults safe-off); `peer_confirmed_key_levels`
  ships with OB detection disabled since its custom entry pipeline doesn't
  consume OBs. Reuses every `anti_chase_fvg_retest_*` knob for bar-confirmation.
- **Heal-propagation hook** (`data_feed.py fetch_history`). A successful
  1m heal now invalidates `last_htf_refresh` and the cycle-scoped HTF
  cache so the HTF rebuild fires immediately on the healed 1m frame
  instead of waiting until the next bar boundary. Skipped on empty heals
  (REST returned no candles) since the existing HTF derivation is still
  valid.
- **Quote alias caching** (`data_feed.py`). New `_resolved_quote_alias` cache
  resolves index-like symbols (`NYICDX`→`$NYICDX`, `VIX`→`$VIX`) once and
  routes them through batched `fetch_quotes` instead of issuing a per-cycle
  one-off `quote()` call. Cuts ~1 call/cycle per index symbol.
- **Sliding-window API tracker** (`utils.py SchwabdevApiUsageTracker`).
  Replaces lifetime average with deque-backed sliding windows at 1m / 5m /
  15m / 30m granularities. Snapshot exposes `calls_per_minute_{1m,5m,15m,30m}`,
  raw `calls_window_*` counts, and `lifetime_calls_per_minute`. The legacy
  `avg_calls_per_minute` field is removed entirely — dashboard.js consumers
  read `calls_per_minute_5m` directly so the "Schwabdev Calls / Min (5m)"
  chip reflects current activity instead of being poisoned by overnight
  idle hours. Per project's clean-breaks-over-shims convention. Dashboard
  signature filter excludes the 9 transient rate fields under the
  `('api_usage',)` path.
- **Dashboard chart UX**. Touch-input via pointer events (tap shows tooltip,
  drag moves it, tap persists until next gesture); `touch-action: pan-y` on
  `#market-chart`. Small-phone fallback at `≤480px` (single column, 44×44px
  tap targets, table cells wrap). Hardcoded `DASHBOARD_TIMEZONE =
  'America/New_York'` passed to all chart timestamp formatters. Tab
  `visibilitychange` listeners force immediate refresh on tab return.
- `runtime.max_consecutive_quote_failures` (default `5`): per-symbol
  quote-fetch failure threshold. Symbol is silenced from quote refresh after
  the threshold; recovers on bot restart. Set `0` for legacy always-retry.
- `_strategies.insufficient_bars_reason` promoted to public API. Cross-package
  consumers should `from ._strategies import insufficient_bars_reason`
  instead of reaching into `_strategies.helpers` directly.
- `anti_chase_fvg_retest_skip_vwap_ema9_reclaim` strategy param (default
  `false`). Drops the trend-MA half of the FVG `reclaimed` clause for
  microcap squeeze entries on deep retests where VWAP/EMA9 lag well above
  the FVG zone.

### Added

- **Fib retracement chart overlays (38.2% / 50% / 61.8% / 78.6%).**
  Pullback support levels within a bullish impulse range and bounce
  resistance levels within a bearish impulse range, drawn as dashed
  horizontal lines. Companion to the existing fib extension overlays
  (127.2% / 161.8%). New `show_fib_retracements` chart toggle in the
  `DashboardChartConfig` schema (default `false` compact, `true`
  expanded — paired with `show_fib_extensions`). 8 new
  `fib_bullish_382/500/618/786` and `fib_bearish_382/500/618/786`
  fields on `TechnicalLevelsContext`, computed alongside the
  existing extensions in `technical_levels.py` (no extra impulse
  detection — the same `bullish_impulse` / `bearish_impulse`
  segments drive both extension and retracement levels).

### Changed

- **Technical-levels overlays now follow the strategy's LTF.** The
  dashboard's `symbol_snapshot` previously built the technical_levels
  context (fibs, AVWAP, Bollinger, ADX, channels, trendlines, ATR
  context, OBV, RSI, divergences) from the 1m streamed frame
  unconditionally. With LTF/HTF separation done across the rest of
  the codebase, this was the last pinned-1m surface for derived
  overlays. Now uses the strategy's `params.ltf_minutes` frame
  (resampled via `data.get_merged(symbol, timeframe=f"{ltf_min}min")`
  when LTF != 1, otherwise the 1m streamed frame). For
  `peer_confirmed_key_levels` (LTF=5m) the chart's fib extensions /
  retracements / AVWAP / Bollinger / channels / trendlines all align
  with the 5m bars displayed in LTF chart mode. For default-LTF
  strategies behavior is unchanged.
- **`hourly_*` → `htf_*` rename (HTF concept, no shims).** All
  `hourly_*` strategy params, output keys, methods, and reason codes
  refer to the HTF context (HTF EMAs, HTF zone votes, HTF bias
  alignment) — not literally "the 1-hour timeframe". Renamed for
  consistency with the rest of the codebase's HTF/LTF naming:
  - **Strategy params (2)**: `require_hourly_bias_alignment` →
    `require_htf_bias_alignment`, `strong_setup_min_hourly_vote_edge` →
    `strong_setup_min_htf_vote_edge`.
  - **Method**: `_hourly_bias` → `_htf_bias`.
  - **Output keys (5)**: `hourly_bias` → `htf_bias`,
    `hourly_bull_votes` → `htf_bull_votes`, `hourly_bear_votes` →
    `htf_bear_votes`, `hourly_vote_edge` → `htf_vote_edge`,
    `hourly_vote_bonus` → `htf_vote_bonus`.
  - **Reason codes (5)**: `hourly_bias_bearish` → `htf_bias_bearish`,
    `hourly_bias_not_bullish` → `htf_bias_not_bullish`,
    `hourly_bias_bullish` → `htf_bias_bullish`,
    `hourly_bias_not_bearish` → `htf_bias_not_bearish`,
    `price_not_in_hourly_zone` → `price_not_in_htf_zone`.
  - Touched: 2 manifests (peer_confirmed_key_levels,
    peer_confirmed_key_levels_1m), 2 yaml configs, 3 strategy.py files
    (peer_confirmed_key_levels, peer_confirmed_trend_continuation,
    entry_gatekeeper), 2 READMEs.
- **Dashboard chart: AVWAP renders on HTF charts.** The expanded HTF
  chart was suppressing `show_anchored_vwap` along with the
  `show_ltf_*` toggles. AVWAP is a price-level overlay (horizontal
  line drawn at the anchored-VWAP price) that's valid regardless of
  chart bar timeframe — no reason to hide it on HTF. Removed
  `show_anchored_vwap` from the HTF suppression list in
  `dashboard.js`.
- **Diagnostics tab: bot uptime added.** The bottom-dock Diagnostics
  panel now shows "Bot Uptime" (formatted as `Nd HH:MM:SS` for runs ≥1
  day, `HH:MM:SS` otherwise) and "Started At" (raw ISO timestamp).
  Both derive from `data.started_at` which the engine has been
  emitting in the snapshot payload all along; the dashboard just
  wasn't surfacing it. New `fmtUptime()` helper in `dashboard.js`.
- **All hardcoded "1-minute" paths now follow the strategy's LTF.** Audit
  found four classes of stale 1m hardcodes after the LTF/HTF split, all
  cleaned in one cut:
  - **Real bugs (8 sites)**:
    - `dashboard_cache.py` chart-payload code passed
      `timeframe_minutes=1` and labelled FVGs/OBs `"1m"` even when the
      strategy's LTF was 5m. Both the FVG path (line 961) and the OB
      path (line 1033) now read `self._active_ltf_minutes()` and label
      payloads `f"{ltf_min}m"`.
    - `BaseStrategy._score_fvg_context` was called with
      `timeframe_minutes=1` from `strategy_base.py` (line 2634) and
      `zero_dte_etf_options/strategy.py` (line 577) — both now pass
      `self._ltf_minutes()`.
    - `dashboard.js` `ltfVisibilityFilter` rejected items whose
      `timeframe` label wasn't `'1m'`. With LTF=5m, every LTF FVG/OB
      had label `"5m"` and got dropped from the chart. Filter now
      compares against `ltfTimeframeLabel` derived from
      `chart.timeframe_minutes`. HTF FVG/OB filters likewise switched
      from `timeframe !== '1m'` to `timeframe !== ltfTimeframeLabel`.
  - **`_structure_context(frame, "1m")` → `_structure_context(frame, "ltf")`**
    in 13 strategy files (entry_gatekeeper, strategy_base, mean_reversion,
    pairs_residual, momentum_close, microcap_pm_breakout, closing_reversal,
    opening_range_breakout, rth_trend_pullback, top_tier_adaptive,
    volatility_squeeze_breakout, zero_dte_etf_options ×2). Default value
    of `_structure_context`'s `timeframe` parameter also flipped from
    `"1m"` to `"ltf"`. Strategies with LTF=1m get identical behavior;
    strategies with LTF≠1 (none today, but future-safe) get LTF-aware
    structure analysis automatically.
  - **Stale internal vars** renamed for consistency: `fvg1_score` →
    `fvg_ltf_score` (strategy_base, zero_dte_etf_options),
    `fvg1_ctx` → `fvg_ltf_ctx`, `ms1_ctx` → `ms_ltf_ctx`, `ms1_weight` →
    `ms_ltf_weight`, `ms1_fields` → `ms_ltf_fields`. Output keys
    `fvg_1m_*` → `fvg_ltf_*` (8 keys). Entry-decision metadata key
    prefix `'ms1m'` → `'msltf'` (used by `_structure_lists(prefix=...)`
    in 3 strategies + entry_gatekeeper). Test fixtures in
    `tests/test_bug_regressions.py` migrated to the new
    `msltf_pivot_count` key.
  - **Stale comments/docstrings** updated: `data_feed.py`
    `get_order_block_context` docstring now describes "LTF OBs" instead
    of "1m OBs"; `engine.py` and `strategy_base.py` example tuples for
    `_observed_contexts` use `("structure", "ltf")` instead of
    `("structure", "1m")`.
  - **Genuinely 1m-specific paths kept** (the literal 1-minute frame is
    correct in these): Schwab API `frequencyType="minute"` /
    `frequency=1` for streaming history; `support_resistance.py:563`
    `now_ts.floor("1min")` for the dual-frame flip cutoff;
    `utils.py:658` `ts.floor("1min")` utility; back-compat aliases for
    legacy `"1m"` chart-mode URL parameter (`dashboard.py`,
    `dashboard_cache.py`, `dashboard.html`); `session_report.py:786`
    already LTF-aware.
- **`trigger_*` → `ltf_*` rename (LTF-frame meaning only).** The
  `trigger_*` prefix was overloaded — sometimes meaning "the entry-trigger
  event" (verb), sometimes meaning "the LTF candle / trigger frame"
  (noun). Renamed only the noun-meaning items, with no shims:
  - **Strategy params (12)**: `trigger_quality_bonus_enabled` →
    `ltf_quality_bonus_enabled`, `trigger_quality_max_bonus` →
    `ltf_quality_max_bonus`, `trigger_reclaim_quality_bonus_cap` →
    `ltf_reclaim_quality_bonus_cap`, `trigger_zone_interaction_bonus_cap` →
    `ltf_zone_interaction_bonus_cap`, `trigger_candle_quality_bonus_cap` →
    `ltf_candle_quality_bonus_cap`, `trigger_volume_quality_bonus_cap` →
    `ltf_volume_quality_bonus_cap`, `trigger_range_expansion_bonus_cap` →
    `ltf_range_expansion_bonus_cap`, `min_trigger_score` → `min_ltf_score`,
    `min_trigger_close_position` → `min_ltf_close_position`,
    `min_trigger_volume_ratio` → `min_ltf_volume_ratio`,
    `min_trigger_bar_volume` → `min_ltf_bar_volume`,
    `strong_setup_min_trigger_score` → `strong_setup_min_ltf_score`.
  - **Entry-decision metadata keys (18)**: `trigger_score` → `ltf_score`,
    `trigger_base_score` → `ltf_base_score`, `trigger_quality_*` →
    `ltf_quality_*`, all `trigger_candle_*` (matches / anchor / score /
    net_score / opposite_score / regime_hint) → `ltf_candle_*`,
    `trigger_score_required` → `ltf_score_required`, `trigger_reasons` →
    `ltf_reasons`, `strong_setup_trigger_score_required` →
    `strong_setup_ltf_score_required`, `selection_trigger_score` →
    `selection_ltf_score`. **Skip-reason codes** also renamed:
    `weak_trigger_score` → `weak_ltf_score`, `trigger_score_below_min` →
    `ltf_score_below_min`, `trigger_bar_volume_below_min` →
    `ltf_bar_volume_below_min`.
  - **`BaseStrategy` methods (5)**: `_trigger_score` → `_ltf_score`,
    `_trigger_quality_bonus` → `_ltf_quality_bonus`, `_trigger_quality_caps`
    → `_ltf_quality_caps`, `_configured_trigger_candle_summary` →
    `_configured_ltf_candle_summary`, `_configured_trigger_candle_match`
    → `_configured_ltf_candle_match`.
  - **Internal vars** in the renamed methods: `trigger_min_score`,
    `trigger_window`, `trigger_sweep_window`, `trigger_preview`,
    `level_selection_trigger_score` → `ltf_*` equivalents.
  - **Kept (verb meaning)**: `adaptive_runner_trigger_rr`,
    `exit_trigger_level`, `_pullback_trigger_signal`,
    `_no_style_trigger_reason`, `trigger_lookback_bars` (rth_trend_pullback
    re-expansion trigger event), `trigger_high` / `trigger_low`,
    `trigger_level=` kwarg, locals `trigger_level` / `trigger_broke` /
    `trigger_kind` / `trigger_ref` / `trigger_slice` / `trigger_lookback`,
    `anti_chase_fvg_retest_trigger_tolerance_pct`. These all really mean
    "the thing that triggers entry" (verb), not the LTF frame.
  - 26 files touched in one cut: 5 manifests, 7 yaml configs, 4 strategy.py
    files (peer_confirmed_*, microcap_pm_breakout), `strategy_base.py`,
    `entry_gatekeeper.py`, 7 strategy READMEs + main README, 2 test files,
    `scripts/scaffold_strategy_plugin.py`. No back-compat aliases — old
    names removed entirely (per project's clean-breaks rule).
- **Unified flip-confirmation gate.** The two-mode design
  (`mode="dashboard"` for snappy 1-bar 1m feedback vs `mode="trading"`
  for the strict 2-bar-1m / 1-bar-5m dual-frame OR gate) collapses to a
  single trading-strict gate now that every consumer of the SR context
  uses the same flip strictness:
  - **Dashboard chart** zone-flip detection (`dashboard_cache.py` key
    level zones) switched from `dashboard_flip_confirmation_1m_bars=1` /
    `5m=0` to the trading values. Chart, sidebar (`sr_row()`), entry
    gatekeeper, position management, and strategy entries (`peer_confirmed_*`,
    `top_tier_adaptive`) now all see the same flip status — no path
    where the chart shows a level as broken before the strategy treats
    it as broken.
  - **Entry gatekeeper** (`entry_gatekeeper.py:414`) switched from
    `mode="dashboard"` to `mode="trading"`. Behaviorally a no-op (the
    gatekeeper only reads `sr_ctx.market_structure`, which is computed
    by `analyze_market_structure()` and doesn't depend on flip values),
    but consolidates the cycle-cache (`_cycle_sr_cache`) so the
    gatekeeper and `position_manager` share a single SR context build
    per `(symbol, tf)` instead of two.
  - **`mode="dashboard"` branch removed** from
    `MarketDataStore.get_support_resistance` — only `"trading"` and
    `"default"` modes remain. The `mode` parameter could be retired
    entirely in a follow-up.
  - **`SupportResistanceConfig.dashboard_flip_confirmation_1m_bars`
    removed entirely** (per project's clean-breaks-over-shims rule).
    The orphaned knob is gone from `config.py`, all 18 yaml configs,
    and the SR-config table in README.md.
- **LTF FVG / OB / structure retargeting.** Five SR-config knobs and one
  strategy param were renamed AND re-targeted from "always 1-minute"
  to "the strategy's LTF":
  - `support_resistance.one_minute_fair_value_gaps_enabled` →
    `ltf_fair_value_gaps_enabled`
  - `support_resistance.one_minute_order_blocks_enabled` →
    `ltf_order_blocks_enabled`
  - `support_resistance.structure_1m_pivot_span` → `structure_ltf_pivot_span`
  - `support_resistance.structure_1m_weight` → `structure_ltf_weight`
  - `dashboard.charting.{compact,expanded}.show_1m_fair_value_gaps` →
    `show_ltf_fair_value_gaps` (companion: `show_1m_order_blocks` →
    `show_ltf_order_blocks`)
  - Strategy param `one_minute_fvg_entry_weight` → `ltf_fvg_entry_weight`
  - **Behavior change**: FVG/OB/structure analysis now runs on the
    strategy's `params.ltf_minutes` frame (defaults to 1m streaming
    bars when not declared). For `peer_confirmed_key_levels` (LTF=5m),
    FVGs and OBs are now detected on 5m bars instead of 1m. For
    `peer_confirmed_key_levels_1m` and other strategies that default
    to 1m LTF, behavior is unchanged.
  - **Method renames**: `BaseStrategy._one_minute_fvg_context` →
    `_ltf_fvg_context`; `_one_minute_order_block_context` →
    `_ltf_order_block_context`.
  - **`MarketDataStore.get_fair_value_gap_context`** previously
    accepted a `timeframe_minutes` label but only ever computed on the
    1-minute merged frame. Now it resamples to the requested timeframe
    before building the FVG context (mirroring `get_order_block_context`).
  - **Structure-context gate**: `_structure_context` matches against
    the strategy's LTF via the new `_is_ltf_token()` helper instead of
    the hardcoded `{"1m","1min","minute","execution"}` set. The
    `structure_ltf_*` overrides now apply when the strategy is computing
    structure on its LTF frame regardless of LTF value.
  - **Dashboard chart payload keys**: `one_minute_fair_value_gaps` →
    `ltf_fair_value_gaps`; `one_minute_order_blocks` → `ltf_order_blocks`.
    Frontend `dashboard.js` migrated to read the new keys.
- **Bar-aligned HTF refresh.** `MarketDataStore.should_refresh_htf_context`
  no longer uses an elapsed-time throttle; it now refreshes on HTF bar
  boundaries with a 10-second settle buffer. New HTF data only arrives
  at HTF bar boundaries — within a single bar window the broker has
  nothing new to give us. For HTF=60m (base_freq=30m), at the 11:00
  boundary both 30m constituents of the just-closed 10:00-11:00 60m bar
  are already complete on the broker side, so a single fetch + resample
  produces the closed bar (no need for two 30m-aligned fetches). API
  reduction per symbol per HTF: 5m → 60% fewer fetches/hr; 15m → 87%
  fewer; 30m → 93% fewer; 60m → 97% fewer; 240m → 99% fewer.
  - **`htf_refresh_seconds` removed entirely.** Strategy params,
    manifests, yamls, READMEs, accessors, and the `refresh_seconds`
    parameter on `data_feed.get_*` / `prefetch_htf_contexts` /
    `should_refresh_*` all gone (per project's clean-breaks
    convention). Failure retries work naturally because
    `last_htf_refresh[key]` is only stamped on successful fetch+merge —
    a failed fetch leaves the bar window "due" so the next tick retries.
  - **Cycle-cache key on `get_support_resistance` simplified** —
    `refresh_seconds` dropped from the cycle key tuple since it no
    longer parameterizes behavior.
  - **`current_structure_overlay` no longer rebuilds a full SR context.**
    Calls `support_resistance.analyze_market_structure(frame, ...)`
    directly to extract the CHOCH/BOS overlay without re-running pivot
    detection, S/R clustering, prior-day/week, FVG checks, broken-level
    reconciliation, or proximity metrics — all the work that the
    overlay path threw away. Eliminates a duplicate per-render rebuild
    on every dashboard chart payload.
- **Engine cycle parallelization.** Per-symbol Schwab fetches (history, S/R
  refresh, quote fallback) now run via `_parallel_symbol_map` and
  `_parallel_quote_fetch`. Strategy context caches (`_chart_context`,
  `_structure_context`, `_technical_context`) pre-warm in parallel via the
  new `prime_cycle_contexts(frame, observed)` hook;
  `BaseStrategy._observed_contexts` lazily records context shapes on first
  invocation. Three `RLock`s guard the per-context caches.
  `cycle_precompute_workers` runtime knob controls the thread pool size.
  Per-cycle API rate is unchanged — only burst pattern compressed. 0DTE
  strategies parallel-prefetch option chains (up to 4 workers, scaled to
  miss count) via the new `_fetch_raw_option_chain` helper that splits I/O
  + cache from put/call + liquidity filtering. `startup_reconciler.reconcile`
  parallelizes `account_details` + `account_orders` (~200-400ms boot stall
  saved).
- **Cycle-scoped broker positions cache.** `entry_gatekeeper` fetches
  `account_details` at most once per `engine.step()` regardless of how many
  `broker_position_row` / `broker_position_rows` consumers run inside the
  cycle. New `force_refresh=True` keyword bypasses the cache; backed by a
  `_fetch_broker_positions_uncached()` helper that's the single source of
  truth for the underlying call shape. Failure latches per-cycle to avoid
  retry storms during Schwab outages. New `begin_cycle()` / `end_cycle()`
  lifecycle hooks mirror per-cycle FVG/OB/S-R caches in `data_feed.py`.
  Order block context is also cycle-cached on `MarketDataStore` via
  `_cycle_ob_cache` and `get_order_block_context()`, eliminating ~260
  redundant `build_order_block_context` calls per minute when strategy +
  dashboard both consume OBs.
- **Dashboard HTTPS perf.** HTTP/1.1 keep-alive (`protocol_version =
  "HTTP/1.1"`) with 30s idle timeout — one TCP+TLS connection per browser
  tab instead of fresh pair per request, eliminating first-load stall under
  HTTPS. TLS handshake offloaded to per-request worker thread
  (`do_handshake_on_connect=False` + 5s handshake timeout) so concurrent
  clients handshake in parallel. New `ReusableThreadingHTTPServer.handle_error`
  silences common transport-layer exceptions (`ssl.SSLError`,
  `ConnectionError`, `BrokenPipeError`, `socket.timeout`) at DEBUG level
  instead of dumping full tracebacks to journald.
- **FVG knob consolidation.** Six `htf_*` / `ltf_*` FVG knobs
  collapsed into three shared knobs (`fair_value_gap_max_per_side`,
  `fair_value_gap_min_atr_mult`, `fair_value_gap_min_pct`); both timeframes
  read the same fields. Enable flags
  (`htf_fair_value_gaps_enabled`, `ltf_fair_value_gaps_enabled`)
  remain timeframe-specific. Per project's clean-breaks-over-shims
  convention, the old field names are removed entirely from
  `SupportResistanceConfig`. Mirrors the OB knob consolidation.
- **Helpers.py extraction.** Pure stateless helpers (numeric coercion,
  bar/DataFrame shape utilities, premium clamping, symbol normalization,
  reason formatters, structured-logging payload builder, dashboard
  zone-width policy) extracted from `BaseStrategy` into `_strategies/helpers.py`
  (~622 LOC, 29 functions in 7 sections). `BaseStrategy` shrunk by ~350 LOC.
  ~570 callsites updated across 16 files to import via `..shared`. Class-level
  delegation methods removed entirely. Test patches must switch from
  `patch.object(strategy, "_method", …)` to
  `patch("intraday_tv_schwab_bot._strategies.strategy_base._method", …)`
  (patch the module function, not the class attribute).
- **`peer_confirmed_key_levels` retune.** Always-on profile:
  `auto_exit_after_session: false`, `startup_reconcile_mode: restore_hybrid`,
  entry/management/screener windows expanded to `07:00-19:55` ET,
  `time_stop_minutes: 0`. API-cost retune for extended hours:
  `history_poll_seconds: 300`, `stream_stale_fallback_seconds: 180`.
  HTF refresh is bar-aligned (one Schwab call per HTF bar boundary), so
  the prior `htf_refresh_seconds` knob is gone — see the bar-aligned
  HTF refresh note in **Changed**. Strategy quality filters (min trigger
  score, peer agreement, macro net bias) gate extended-hours candidates
  organically.
- **Dashboard render polish.** `dashboard_recent_trade_markers()` and
  `dashboard_symbol_trade_signature()` filter trades by symbol BEFORE
  slicing (a fresh fill on a long-quiet symbol could otherwise be
  invisible); marker function also filters to today's ET session date.
  Chart payload `last_update` re-stamps on every cache hit so frontend
  timestamp doesn't freeze. iOS `:hover` rules wrapped in `@media (hover:
  hover)` so taps don't stick. Mobile poll cadence floor raised to 4000ms
  (cellular radio savings); honors server-provided `dashboard.refresh_ms`
  when slower than the floor. Order block ranking is strength-based
  (thrust × size × age × validity) when `max_per_side` clips —
  `nearest_bullish_ob` / `nearest_bearish_ob` accessors still resolve
  nearest-by-price for retest-plan consumers. `OrderBlock` enrichment uses
  `dataclasses.replace()` on the slotted dataclass.
- **Plugin scaffold + package hygiene.** `scripts/scaffold_strategy_plugin.py`
  emits 9 FVG knobs + `force_flatten` in the generated manifest, plus SPDX
  headers in scaffolded `__init__.py` / `strategy.py` / `screener.py`. Plugin
  templates updated to call `insufficient_bars_reason(...)` and
  `_safe_float(...)` as free functions (post-helpers.py extraction).
  `top_tier_adaptive` imports `now_et` / `parse_hhmm` from `..shared`.
  `intraday_tv_schwab_bot.__all__` trimmed from 29 unreachable entries to
  `["__version__"]`. `config.__all__` switched to mechanical
  "no-leading-underscore = public" rule (20 entries). `.gitattributes`
  upgraded `* text=auto` → `* text=auto eol=lf` to stop Windows
  phantom-modified states under `core.autocrlf=true`.
- Removed `BaseStrategy._apply_continuation_fvg_retest_plan` (the
  single-plan apply helper that predated OR-combine). All 12 callers across
  4 strategies migrated to `_apply_continuation_zone_retest_plans` with a
  single-element plan list. Removed redundant `dashboard_candidate_levels`
  override in `peer_confirmed_htf_pivots` (returned `[]` matching base
  default). Engine `_cycle_sleep_seconds` collapsed from three branches to
  two (stream-on / stream-off).

### Fixed

- **HTF prior-day/week levels now always candidates, not just fallbacks**
  (`htf_levels.py build_htf_context`). The previous flow only injected
  `prior_day_low` / `prior_week_low` (and the high counterparts) when
  pivot detection produced an empty result. In strong directional moves
  a stock can rally for weeks with no proper pivot lows in the rally
  portion (each bar's low > the surrounding bars' lows by definition of
  an uptrend), so pivot-only support detection surfaces only the ancient
  base. AMD example: rally from $258 → $346 over a week with no pivot
  lows in the rally; the dashboard showed first support at $254 (a
  pivot low from the base period weeks earlier) instead of yesterday's
  $340 low. Both prior-day and prior-week levels now merge into the
  candidate pool alongside pivot levels via `_extend_unique_levels`,
  then compete in `_collapse_same_side_levels`. Their `source_priority`
  of 2.0 (prior_day) / 3.0 (prior_week) outranks pivot's 1.0 in
  `_level_preference`, so when a prior-day/week level overlaps a
  same-cluster pivot, the prior-day/week level wins the picker. The
  second-chance fallback (when filtered candidates are empty) and
  frame-extreme fallback (when both pivots and prior-day/week are
  empty) are preserved as-is for fully-empty edge cases.
- **HTF level scoring now time-aware** (`htf_levels.py _cluster_levels`).
  The previous formula computed `score = touches + min(1.5, 0.15 * touches)`
  — a misleadingly-named "recency_bonus" that was actually a touches
  multiplier with no time component at all. With a 60-day HTF lookback,
  ancient base levels with many touches accumulated during long
  consolidations dominated the top-N selection, evicting recent close-
  to-price swing lows before they reached `_collapse_same_side_levels`.
  AMD example: current price $346.50 with first support showing at
  $257.73 (a 30+-day-old base) instead of the recent $320-$343 swing
  lows. Replaced with the time-aware formula already used in
  `support_resistance.py _cluster_levels`: `recency_factor` decays
  linearly from `1.0` (newest) to `0.10` (oldest) across the cluster
  window, `effective_touches = touches * recency_factor`, plus a
  persistence bonus that rewards levels held across a sustained portion
  of the window. A 30-day-old 8-touch base now contributes ~4 effective
  touches — comparable to a fresh 4-touch swing low — so both survive
  top-N selection and the dashboard renders the full ladder of recent
  + historical levels.
- **HTF in-memory resample reverted.** An earlier attempt at this release
  added a path that rebuilt HTF bars by resampling the in-memory 1m frame
  with a periodic Schwab audit (`htf_audit_refresh_seconds: 3600`).
  Reverted because the convention mismatch between the in-memory path
  (1m bars resampled with `closed="right"` → bars represent ~10:01-11:00
  data) and the Schwab path (30m bars from `price_history` resampled the
  same way → bars represent 10:30-11:30 data) produced inconsistent OHLC
  in the merged HTF frame. Pivot detection on the inconsistent frame
  surfaced wildly stale support/resistance levels (e.g., AMD with current
  price $346 showing first support at $258 from a 30-day-old base). The
  audit knob, `_try_resample_htf_from_live_1m`, `_htf_audit_due`, and
  `last_htf_audit_refresh` tracker are removed entirely. The
  heal-propagation hook on `fetch_history` is preserved (Added section)
  since it's useful regardless of the rebuild path.
- **Strategy correctness.** `peer_confirmed_key_levels._select_level`
  "touched zone" check replaced with per-bar overlap (window-wide
  `low.min()`/`high.max()` could pass when no individual bar's range
  overlapped the zone — fires during news/fast-spike conditions). OB
  detection walk-back uses `continue` instead of `break` so a small doji at
  idx-1 doesn't abort the search for a real OB at idx-2+;
  `_merge_order_blocks` first/last_seen now uses explicit `_earlier`/`_later`
  ISO-timestamp helpers (sort is by price, not chronology). `_optional_int`
  parses float-strings like `"3.7"` → `3` (was returning default).
- **Dashboard chart.** `paint()` uses `activeIndex` consistently (latent
  crash on bar-pinning land — `bars[hoverIndex]` was a stale global).
  `renderEmpty` cancels pending hover-RAF before detaching pointer handlers
  (previously a queued `requestAnimationFrame` from a previous chart could
  draw ghost data after canvas clear). Tap-and-release tooltip persists
  until next gesture (was clearing on finger lift). Volume bars use
  `parseFinite()` (was `Number()` which coerced `"NaN"` strings to `NaN`).
  Theme `<link>` `onerror="this.remove()"` falls back to base styling on
  404. Spread pill no longer flickers visible/hidden between stream ticks
  (now matches sibling pills with `—` placeholder).
- **Mobile dashboard.** `.position-card` cursor override (was `pointer`
  from desktop with no click handler). `.panel-meta` text-wrap fix for
  7-figure equity. `.positions-panel` explicit `position: relative` (no
  longer dependent on desktop's `≤1400px` breakpoint). Qty rendering uses
  `fmtInteger()` (was `escapeHtml()` producing literal `"null"`).
- **Memory + state hygiene.** `data_feed.prune_inactive_symbols` evicts
  the `last_htf_refresh` tuple-keyed dict alongside the rest of the
  per-symbol state.
- **Config + manifest hygiene.** All 18 prod presets + `config.example.yaml`
  expose the eight OB knobs (defaults safe-off) and the three always-on
  knobs (`idle_sleep_seconds`, `symbol_state_prune_seconds`,
  `session_reconcile_on_resume`). README runtime table + behavior
  section updated with the new knobs.
- **Logging + cosmetic.** `dashboard_cache.py log_component_failure` calls
  in OB blocks pass symbol as arg (was printf message). ORB `none`-mode
  activity score rescaled to `rvol × volume / 1_000_000` so log magnitudes
  match other branches. Dashboard `focus-meta` uses compact entry-decision
  label so long ETF skip reasons don't push live-data chips off the card.
  Three IDE / type-checker warnings cleaned up (redundant
  `self.stream = None`, two unused `_ltf_order_block_context` params).

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
