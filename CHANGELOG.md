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
