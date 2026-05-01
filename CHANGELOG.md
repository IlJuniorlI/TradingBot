# Changelog

All notable changes to `intraday-tv-schwab-bot` will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Changed (continued)

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
  This mirrors the OB knob consolidation (commit 28558f4).

### Changed

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
