# Intraday TradingView + Schwabdev Bot

Version: see [`version.txt`](version.txt) · Changelog: [`CHANGELOG.md`](CHANGELOG.md) · License: MIT with Commons Clause — see [`LICENSE`](LICENSE)

This README documents the **live config surface that the bot actually loads today** from `intraday_tv_schwab_bot/config.py`, plus the shipped top-level presets under `configs/` and plugin manifests under `intraday_tv_schwab_bot/_strategies/`.

Three important notes up front:

1. **Top-level block tables below show code defaults**. The strategy-by-strategy sections later in this file reflect the shipped top-level `configs/config.<strategy>.yaml` presets and matching manifest defaults that tune each bundled strategy.
2. Strategy params are split between **strategy-specific knobs** and **shared reusable groups** like anti-chase, FVG confluence, and adaptive trade management.
3. **Dashboard zoom on 1080p and lower displays**: the dashboard is laid out for 1440p+. On 1080p (or smaller), set browser zoom to **75%** so all panels fit without scrolling and chart overlays render at the intended scale. Chrome/Edge: `Ctrl + -` twice from default. Firefox: `Ctrl + -` twice, then per-domain zoom is remembered.

See also:

- `README_STRATEGY_START_TIMES.md` — when to launch each strategy during the day
- `configs/config.example.yaml` — canonical full-config template and scaffold base

## Supported strategies

### Stock strategies

- `momentum_close` — late-day small-cap continuation
- `mean_reversion` — intraday pullback / rebound in strong names
- `closing_reversal` — late-day rebound setup
- `rth_trend_pullback` — all-session trend-pullback continuation
- `volatility_squeeze_breakout` — liquid-stock compression breakout strategy
- `pairs_residual` — relative-value pair divergence strategy
- `opening_range_breakout` — opening-range breakout strategy
- `microcap_gap_orb` — long-only opening-range breakout tuned for cheap (≤$20) names that gap 20%+ at the open; subclasses `opening_range_breakout` with a custom screener and a 09:37–13:30 entry window so late-morning gappers still get evaluated
- `peer_confirmed_key_levels` — peer-confirmed hourly key-level/zone strategy with 5-minute triggers, optional macro confirmation, and ladder-aware post-entry management when `adaptive_ladder` is enabled
- `peer_confirmed_key_levels_1m` — faster 1-minute peer-confirmed hourly key-level/zone variant tuned as a compromise between aggressive and balanced confirmation
- `peer_confirmed_trend_continuation` — peer-confirmed trend continuation strategy that trades controlled pullbacks and re-expansion without waiting for key-level touches
- `peer_confirmed_htf_pivots` — peer-confirmed higher-timeframe pivot S/R scalp strategy with switchable reclaim, rejection, and continuation entry families
- `top_tier_adaptive` — multi-regime adaptive strategy for top-tier liquid stocks across Technology, Consumer Discretionary, and Communication Services with index confirmation and sector concentration guard

### 0DTE ETF option strategies

- `zero_dte_etf_options` — defined-risk vertical spread mode
- `zero_dte_etf_long_options` — long-premium calls / puts only

## Running

Requires **Python 3.11+**. Install dependencies (strictly pinned in `requirements.txt`), seed a runtime config, and launch:

```bash
pip install -r requirements.txt
cp configs/config.example.yaml configs/config.yaml
python main.py --config configs/config.yaml --strategy zero_dte_etf_options
```

Alternatively, `pip install -e .` consumes `pyproject.toml` and registers the
`intraday-tv-schwab-bot` console script. The package version is pulled from
`version.txt` at the repo root and exposed as `intraday_tv_schwab_bot.__version__`.

`requirements.txt` pins `TA-Lib==0.6.8`, which backs the standard indicator and candlestick-pattern layer. On Windows, `pip install TA-Lib==0.6.8` is self-contained — the 0.6.x wheels on PyPI statically bundle the underlying C library (cp39–cp313, win_amd64). On macOS and Linux, install the native C library first (`brew install ta-lib` on macOS, `apt install libta-lib0-dev` on Debian/Ubuntu) before `pip install`.

Tests are maintained in a private source tree (not shipped with this repository).

Change `--strategy` or the top-level `strategy:` key to switch strategies.

Runtime config precedence is now intentionally simple:

- manifest/code defaults
- the selected top-level YAML file (for example `configs/config.peer_confirmed_htf_pivots.yaml`)
- CLI strategy override via `--strategy`


For runtime plugin behavior, the engine now prefers strategy hooks and manifest capabilities over hard-coded strategy-name branches. The most common extension points are `dashboard_tradable_symbols()`, `restore_eligible_symbols()`, `requires_hybrid_startup_restore_metadata()`, `signal_priority_key(...)`, `dashboard_candidate_limit()`, `dashboard_allow_generic_level_fallback()`, `dashboard_level_context_spec()`, `dashboard_candidate_label()`, `dashboard_candidate_sources()`, `active_watchlist(...)`, and `quote_watchlist(...)`. New plugins can now often declare those behaviors in `manifest.json` under `capabilities`, including watchlist/quote universe rules and dashboard level-context overrides, and only fall back to Python hooks when they need something more custom.

The standardized plugin runtime contract now uses `Candidate.activity_score`, `Candidate.directional_bias`, and signal metadata fields such as `final_priority_score`, `selection_quality_score`, `activity_score`, `setup_quality_score`, and `execution_quality_score`.

`manifest.json` now supports `schema_version: 1`, and the loader rejects unsupported top-level keys early so malformed plugins fail closer to the source of the mistake. New plugins are fully self-contained: the scaffold emits plain-string `strategy_name` values, and no central strategy-name helper or `models.py` edit is required.

A scaffold helper is included for new plugins. It clones `configs/config.example.yaml` as a full runnable preset, swaps in the new strategy name, and writes `configs/config.<strategy>.yaml`:

```bash
python scripts/scaffold_strategy_plugin.py my_new_strategy
```

A basic plugin conformance test suite also ships under `tests/test_strategy_plugin_conformance.py`.

## How the YAML is organized

The top-level blocks are:

- `strategy`
- `schwab`
- `tradingview`
- `risk`
- `runtime`
- `execution`
- `candles`
- `chart_patterns`
- `paper`
- `dashboard`
- `support_resistance`
- `technical_levels`
- `shared_entry`
- `shared_exit`
- `options`
- `pairs`

Times are interpreted in `runtime.timezone`, which defaults to `America/New_York`.

Use the selected top-level config file as the single runtime source of truth. Shipped presets live under `configs/config.<strategy>.yaml`.

## Top-level config reference

### `strategy`

The active strategy name.

Valid values:

- `momentum_close`
- `mean_reversion`
- `closing_reversal`
- `rth_trend_pullback`
- `volatility_squeeze_breakout`
- `pairs_residual`
- `opening_range_breakout`
- `microcap_gap_orb`
- `zero_dte_etf_options`
- `zero_dte_etf_long_options`
- `peer_confirmed_key_levels`
- `peer_confirmed_key_levels_1m`
- `peer_confirmed_trend_continuation`
- `peer_confirmed_htf_pivots`
- `top_tier_adaptive`

Changing `strategy` switches the live strategy only. The standard example/main config is now a full runnable template and includes an explicit `strategies.<name>` block. Shipped full presets do the same so each preset is portable as a standalone runtime config. The selected top-level file is now the runtime authority for `strategies.<name>`.

### Secrets via `.env`

Schwab API credentials and the TradingView session cookie are sourced from a `.env` file at the repo root so they never have to live in a committed yaml config.

1. Copy `.env.example` to `.env` (both files are at the repo root).
2. Fill in the real values:
   - `SCHWAB_APP_KEY` and `SCHWAB_APP_SECRET` — required; obtained from your Schwab developer app.
   - `SCHWAB_ACCOUNT_HASH` — optional; set only if you want to pin trading to a specific linked account. Leave blank to auto-resolve.
   - `SCHWAB_ENCRYPTION_KEY` — optional (strongly recommended); Fernet key that encrypts the Schwab token DB at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and paste the 44-char base64 output into `.env`.
   - `TRADINGVIEW_SESSIONID` — optional; grab the `sessionid` cookie from a logged-in TradingView browser session.
3. `.env` is listed in `.gitignore`, so the file stays local to your machine.

Resolution precedence when the bot loads a config:

1. A real value in the yaml (`schwab.app_key`, `schwab.app_secret`, `schwab.account_hash`, `schwab.encryption`, `tradingview.sessionid`) always wins. Placeholder strings like `YOUR_APP_KEY` are treated as unset.
2. Otherwise the matching environment variable is used (`SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_ACCOUNT_HASH`, `SCHWAB_ENCRYPTION_KEY`, `TRADINGVIEW_SESSIONID`).
3. If the env var isn't set in the real process environment, the value is read from `.env` (process env always wins over `.env`).
4. If neither yaml nor env provides the Schwab key or secret, `load_config` raises with a clear error pointing at `.env.example`. `account_hash`, `encryption`, and `sessionid` are optional; the bot runs without them (`encryption` unset means tokens are stored unencrypted).

The `.env` parser is intentionally minimal (no `python-dotenv` dependency): one `KEY=VALUE` per line, `#` for comments, optional surrounding quotes.

### `schwab`

Use this block for broker auth, token storage, and dry-run behavior.

| Option         | Code default            |
|----------------|-------------------------|
| `app_key`      | `REQUIRED` (via `.env`) |
| `app_secret`   | `REQUIRED` (via `.env`) |
| `callback_url` | `https://127.0.0.1`     |
| `tokens_db`    | `.schwabdev/tokens.db`  |
| `encryption`   | `null` (via `.env`)     |
| `timeout`      | `10`                    |
| `account_hash` | `null` (via `.env`)     |
| `dry_run`      | `true`                  |

How these fields behave:

- `app_key` / `app_secret`: required Schwab developer credentials. Supply via the `.env` file at the repo root (`SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`) — see [Secrets via `.env`](#secrets-via-env). Setting them in this yaml block is still honored and overrides `.env`, but keeping them out of yaml avoids committing credentials.
- `callback_url`: OAuth callback URL registered with Schwab.
- `tokens_db`: local token-store path.
- `encryption`: Fernet key that encrypts the token DB at rest. Supply via the `.env` file (`SCHWAB_ENCRYPTION_KEY`) — see [Secrets via `.env`](#secrets-via-env). Leave unset (null/blank) to leave the DB unencrypted. Generate a key with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- `timeout`: HTTP timeout in seconds for Schwab calls.
- `account_hash`: explicit account hash. Supply via the `.env` file (`SCHWAB_ACCOUNT_HASH`) when you need to pin trading to a specific linked account — see [Secrets via `.env`](#secrets-via-env). Leave unset (null/blank) to auto-resolve the linked account.
- `dry_run`: when `true`, simulate broker actions instead of sending live orders.

### `tradingview`

This block is used by stock strategies and by the 0DTE ETF screen/ranker.

| Option                     | Code default        |
|----------------------------|---------------------|
| `sessionid`                | `null` (via `.env`) |
| `market`                   | `america`           |
| `max_candidates`           | `5`                 |
| `screener_refresh_seconds` | `90`                |
| `min_market_cap`           | `30000000`          |
| `max_market_cap`           | `2000000000`        |
| `min_volume`               | `750000`            |
| `min_value_traded_1m`      | `150000.0`          |
| `min_volume_1m`            | `25000`             |

Behavior and valid values:

- `sessionid`: TradingView session cookie. Supply via the `.env` file (`TRADINGVIEW_SESSIONID`) — see [Secrets via `.env`](#secrets-via-env). Leaving it unset (null) is allowed; screener results may be delayed ~15 minutes without it.
- `market`: TradingView screener market namespace. Typical value: `america`.
- `max_candidates`: max candidates kept after each screener refresh. Higher values widen the watchlist.
- `screener_refresh_seconds`: TradingView screener cache life. Lower values refresh the screen more often.
- `min_market_cap` / `max_market_cap`: market-cap bounds for stock screeners.
- `min_volume`: minimum daily share volume for stock screening.
- `min_value_traded_1m`: minimum one-minute dollar value. Higher values force cleaner intrabar liquidity.
- `min_volume_1m`: minimum one-minute share volume. Higher values also make the screener more selective.

### `risk`

This block controls shared position sizing, daily guardrails, re-entry behavior, and open-trade management mode.

| Option                            | Code default      |
|-----------------------------------|-------------------|
| `max_positions`                   | `2`               |
| `risk_per_trade_frac_of_notional` | `0.004`           |
| `max_notional_per_trade`          | `4000.0`          |
| `max_total_notional`              | `8000.0`          |
| `max_daily_loss`                  | `400.0`           |
| `default_stop_pct`                | `0.018`           |
| `default_target_pct`              | `0.038`           |
| `trailing_stop_pct`               | `0.014`           |
| `trade_management_mode`           | `adaptive_ladder` |
| `allow_short`                     | `false`           |
| `cooldown_minutes`                | `20`              |
| `reentry_policy`                  | `cooldown`        |
| `cooldown_direction_aware`        | `true`            |
| `same_level_block_minutes`        | `30`              |
| `same_level_block_atr_mult`       | `0.3`             |
| `time_stop_minutes`               | `45`              |
| `time_stop_min_return_pct`        | `0.003`           |
| `peak_giveback_enabled`           | `true`            |
| `peak_giveback_min_r`             | `1.0`             |

Behavior and valid values:

- `max_positions`: max simultaneous open bot positions.
- `risk_per_trade_frac_of_notional`: risk budget as a **fraction of `max_notional_per_trade`** (not of account equity). Dollar risk per trade = `max_notional_per_trade × risk_per_trade_frac_of_notional`. Example: with `max_notional_per_trade: 16000` and `risk_per_trade_frac_of_notional: 0.008`, each trade risks at most **$128** (16000 × 0.008). Raising `max_notional_per_trade` raises the dollar risk budget proportionally.
- `max_notional_per_trade`: hard cap on one stock position's notional size.
- `max_total_notional`: cap across all open stock positions.
- `max_daily_loss`: daily stop-trading threshold in account currency.
- `default_stop_pct` / `default_target_pct`: fallback stock stop/target distances when a strategy does not derive its own levels.
- `trailing_stop_pct`: base trailing-stop fraction used only by `adaptive`, and by `adaptive_ladder` when the active strategy does not emit ladder metadata. Smaller values trail tighter; larger values give trades more room.
- `trade_management_mode`: valid values are `adaptive`, `adaptive_ladder`, `sr_flip`, or `none`. These modes control equity-style post-entry management. Option strategies continue to use their own fixed stop/target and session-flatten logic.
  - `adaptive`: use the newer R-multiple-based management layer, including adaptive runner extension and base trailing-stop behavior.
  - `sr_flip`: use support/resistance flip management only. Generic adaptive and trailing-stop management are disabled in this mode.
  - `adaptive_ladder`: for ladder-enabled strategies, keep adaptive breakeven/profit-lock protection available, but let ladder promotion own target advancement and structural stop ratcheting. Generic runner target extension and generic trailing-stop management are suppressed for ladder-enabled trades. Strategies that do not emit ladder metadata automatically fall back to normal adaptive behavior.
  - `none`: disable adaptive, ladder, S/R flip, and generic trailing-stop management. Only the fixed stop/target exit levels remain active.
- `allow_short`: enables short equity entries for strategies that support them.
- `cooldown_minutes`: only used when `reentry_policy: cooldown`.
- `reentry_policy`: valid values are `cooldown`, `immediate`, or `rest_of_day`.
  - `cooldown`: block re-entry for `cooldown_minutes` after exit.
  - `immediate`: allow same-symbol re-entry immediately.
  - `rest_of_day`: block re-entry until the next trading day.
- `cooldown_direction_aware`: when `true` (the recommended default), the cooldown is keyed by `(symbol, side)` — a LONG exit on `NVDA` only blocks LONG re-entries on `NVDA`; a SHORT can still fire immediately if a genuine bearish setup develops. When `false`, the cooldown blocks both directions on the symbol for the full `cooldown_minutes` window. Has no effect under `reentry_policy: immediate` or `rest_of_day`.
- `same_level_block_minutes`: after a stop-out, block **same-direction** re-entry on the same symbol for this many minutes. Targets the breakout-chase pattern where the bot enters → stops → re-enters at the same level → stops again. Set to `0` to disable. Independent of `cooldown_minutes`; both blocks apply.
- `same_level_block_atr_mult`: the same-level block only fires when the new entry sits within `same_level_block_atr_mult × ATR` of the prior stop price. Lower values block a tighter price band around the prior stop; higher values widen it. Fib-pullback entries (where the new entry sits in the [0.5, 0.786] retracement zone of a tagged anchor) override the block.
- `time_stop_minutes`: scratch-exit a trade held this long with `|return_pct| < time_stop_min_return_pct`. Targets dead-capital trades that aren't moving. Set to `0` to disable.
- `time_stop_min_return_pct`: the absolute return threshold under which the time-stop fires (e.g. `0.003` = 0.3%). Trades outside this band aren't time-stopped regardless of duration.
- `peak_giveback_enabled`: when `true`, fires `peak_giveback:peakXR_floorYR` once `max_favorable_r` crosses `peak_giveback_min_r` and `current_r` retraces past a tiered floor (50% at 1R peak, 40% at 2R, 30% at 3R+). Complements the protective-BE logic at +0.5R: BE catches 0.5–1R winners, peak-giveback catches 1R+ runners that give back too much.
- `peak_giveback_min_r`: minimum peak R-multiple before the peak-giveback floor activates. Set to `0` together with `peak_giveback_enabled: false` to disable entirely.

### `runtime`

This block controls loop timing, quote/history refresh cadence, stream fallback behavior, startup reconciliation, and log/state paths.

| Option                               | Code default                              |
|--------------------------------------|-------------------------------------------|
| `timezone`                           | `America/New_York`                        |
| `loop_sleep_seconds`                 | `2.0`                                     |
| `history_poll_seconds`               | `150`                                     |
| `quote_poll_seconds`                 | `6`                                       |
| `quote_cache_seconds`                | `6`                                       |
| `quote_batch_size`                   | `20`                                      |
| `history_lookback_minutes`           | `390`                                     |
| `use_extended_hours_history`         | `true`                                    |
| `use_rth_session_indicators`         | `true`                                    |
| `warmup_minutes`                     | `90`                                      |
| `prewarm_before_windows_minutes`     | `5`                                       |
| `log_dir`                            | `.logs`                                   |
| `stream_fields`                      | `[0, 1, 2, 3, 4, 5, 6, 7, 8]`             |
| `stream_connect_timeout_seconds`     | `20`                                      |
| `stream_fallback_poll_seconds`       | `25`                                      |
| `stream_stale_fallback_seconds`      | `60`                                      |
| `stream_health_log_seconds`          | `90`                                      |
| `reconcile_on_startup`               | `true`                                    |
| `startup_reconcile_mode`             | `block`                                   |
| `startup_order_lookback_days`        | `2`                                       |
| `startup_reconcile_ignore_symbols`   | `[]`                                      |
| `startup_reconcile_metadata_db_path` | `.logs/startup_reconcile_metadata.sqlite` |
| `auto_exit_after_session`            | `false`                                   |
| `cycle_precompute_workers`           | `4`                                       |
| `max_consecutive_quote_failures`     | `5`                                       |
| `export_session_archive`             | `true`                                    |

Behavior and valid values:

- `timezone`: IANA timezone string. All configured times are interpreted in this timezone.
- `loop_sleep_seconds`: base engine sleep between iterations.
- `history_poll_seconds`: cadence for history refreshes.
- `quote_poll_seconds`: cadence for quote refreshes when polling is used.
- `quote_cache_seconds`: max age of cached quotes before forcing a refresh.
- `quote_batch_size`: max symbols grouped into one quote request.
- `history_lookback_minutes`: intraday history depth retained for signal generation.
- `use_extended_hours_history`: include premarket/after-hours minute bars in warmup and backfill.
- `use_rth_session_indicators`: use regular-session-only EMA/VWAP during RTH, while premarket and postmarket continue using all-session indicator values.
- Screener queries are session-aware: canonical `close`, `change_from_open`, and `volume` map to `premarket_*` fields before 09:30 ET, regular-session fields during RTH, and `postmarket_*` fields after 16:00 ET. Returned screener rows are normalized back to the canonical column names so strategy code keeps reading `close`, `change_from_open`, and `volume` consistently across sessions.
- `warmup_minutes`: minimum history seeded when a symbol is first watched. The bot now also respects each strategy's required bar warmup and will request a deeper preload when the active strategy needs more bars than the current session has provided yet.
- Startup before premarket history is available now schedules a one-shot retry at **7:01 AM ET** for that session, so aliases/index-like symbols can recover promptly once Schwab starts serving candles.
- Dashboard/API state now includes a `warmup` summary and per-symbol readiness payloads so the UI can show `Not Ready`, `Loading`, and `Ready` without digging through skip logs.
- `prewarm_before_windows_minutes`: outside all active windows, skip routine refresh work until the next active window is this close.
- `log_dir`: log/state directory.
- `stream_fields`: Schwab stream field IDs to subscribe to.
- `stream_connect_timeout_seconds`: how long to wait for the stream to come up before treating it as unavailable.
- `stream_fallback_poll_seconds`: polling cadence when streaming is unavailable.
- `stream_stale_fallback_seconds`: base regular-session stale-stream threshold. For 1-minute `CHART_EQUITY`, the live stale check is floored by the stream-health policy (currently about 130 seconds) so healthy minute-close streams do not false-fallback every loop.
- `stream_health_log_seconds`: throttle interval for stream-health logging.
- `reconcile_on_startup`: whether to inspect broker positions/orders at startup.
- `startup_reconcile_mode`: valid values are `ignore`, `block`, `log_only`, `restore_basic`, `restore_hybrid`.
  - `ignore`: skip startup reconciliation entirely.
  - `block`: block new entries if broker positions / working orders are found.
  - `log_only`: log findings but do not block.
  - `restore_basic`: restore broker stock positions into bot memory without metadata help.
  - `restore_hybrid`: restore broker stock positions and also consult the metadata SQLite store for richer restore state.
- `startup_order_lookback_days`: broker order lookup window used during startup reconciliation.
- `startup_reconcile_ignore_symbols`: symbol list ignored during startup reconcile. Ignored open symbols are still blocked from new entries.
- `startup_reconcile_metadata_db_path`: SQLite metadata path used by hybrid restore.
- `auto_exit_after_session`: when `true`, the bot shuts down cleanly after the trading session ends and all positions are closed. Exits after the latest of RTH close and any configured strategy window end. On non-trading days (weekends/holidays), exits immediately. Designed for use with Windows Task Scheduler or cron to start the bot daily.
- `cycle_precompute_workers`: thread-pool size used to precompute per-symbol indicator/structure context in parallel each engine cycle. Higher values reduce per-cycle latency on wide watchlists at the cost of CPU; lower values trade latency for less contention.
- `max_consecutive_quote_failures`: per-symbol quote-fetch failure threshold. After a symbol fails this many consecutive quote refreshes (typically symbol-specific Schwab 401/403/404 such as restricted-security responses), it is silenced from quote refresh for the rest of the session. The counter resets on any successful fetch; the blacklist clears on bot restart. Set to `0` to disable (always retry — pre-2026-04-29 behavior). The default `5` catches symbol-specific permission errors without triggering on transient hiccups. Other endpoints (history, stream) for the same symbol are unaffected.
- `export_session_archive`: when `true`, on session shutdown the engine writes a per-day archive to `{log_dir}/sessions/{YYYY-MM-DD}/` containing `bars/{SYMBOL}.csv` for every active watchlist symbol (RTH only, with indicators), `trades.csv` filtered to the day, and `manifest.json` with strategy + summary stats. Useful for trade audits and post-session analysis. Disable to save disk space if running without dashboard/analysis needs.

### Session report

When the bot shuts down (auto-exit, manual interrupt, or non-trading day), it writes an end-of-session summary along five analytical axes so you can tune strategies and configs from the log:

- **Log summary**: `SESSION REPORT <date>: strategy=... pnl=... trades=... wins=... losses=... win_rate=... pf=... avg_trade=... max_drawdown=...` followed by a per-trade breakdown.
- **Aggregate tables** (human-readable, in the log):
  - **Per regime** — count, wins/losses, net PnL, avg PnL, win rate, best, worst per regime (trend / pullback / range / ...)
  - **Per symbol** — same columns aggregated by ticker — surfaces concentration issues and high-variance names
  - **Per exit reason** — same columns aggregated by exit reason (normalized: `resistance_break_exit:311.59` rolls up into `resistance_break_exit`) — flags leaky exits
  - **Per hour (entry)** — same columns bucketed by entry hour (ET) — identifies dead zones in the trading day
  - **MAE/MFE** — max adverse and max favorable excursion in R-multiples (`avg_MAE=0.26R avg_MFE=0.39R max_MAE=1.33R`) plus heat/runup threshold hits (`trades>1R_heat=N`, `trades>2R_runup=N`) — surfaces stops that are too tight or exits that leave profit on the table
  - **Filter rejections** — tally of each skip reason the engine logged, top 10 shown (`cooldown: 175 / short_no_qualifying_regime: 89 / htf_bias_bearish: 12 / ...`) — shows which filters did the most work and whether any are over- or under-firing
- **Structured JSON**: `SESSION_REPORT {...}` at TRADEFLOW level. Carries the same aggregates as first-class keys: `per_regime`, `per_symbol`, `per_exit_reason`, `per_hour`, `mae_mfe`, `filter_rejections`. Any downstream tool (dashboard, spreadsheet import, analyzer) can consume it directly without scraping the human log.
- **Persistent CSV**: `.logs/trades.csv` — one row per closed trade, appended across sessions. Columns: `date, symbol, strategy, side, qty, entry_price, exit_price, entry_time, exit_time, realized_pnl, return_pct, hold_minutes, reason, asset_type, partial_exit, fill_price_estimated, broker_recovered, regime, initial_risk_per_unit, max_favorable_pnl, max_adverse_pnl, entry_slippage_pct`.

The CSV file accumulates over time — open it in Excel or load with `pd.read_csv(".logs/trades.csv")` for multi-day analysis.

**Schema rotation** — if the CSV column set ever changes (e.g., after an upgrade that adds diagnostic columns), the existing file is rotated to `.logs/trades.archive-<date>.csv` and a fresh `.logs/trades.csv` is started with the new header. A WARNING is logged so the operator sees the rotation. Historical data is preserved in the archive file.

### `execution`

This block controls how equity orders are priced and managed after submission.

| Option                            | Code default |
|-----------------------------------|--------------|
| `entry_limit_min_buffer`          | `0.03`       |
| `entry_limit_max_buffer`          | `0.05`       |
| `entry_limit_spread_frac`         | `0.1`        |
| `entry_live_fill_timeout_seconds` | `3.0`        |
| `entry_live_poll_seconds`         | `0.5`        |
| `entry_live_reprice_attempts`     | `1`          |
| `entry_live_reprice_step_frac`    | `0.5`        |
| `extended_hours_enabled`          | `true`       |
| `market_exit_regular_hours`       | `true`       |

Behavior and valid values:

- `entry_limit_min_buffer` / `entry_limit_max_buffer`: lower and upper limit-price offsets used for marketable-limit stock entries.
- `entry_limit_spread_frac`: spread fraction used when converting the current quote into a limit price.
- `entry_live_fill_timeout_seconds`: how long to wait for an equity entry fill before cancel/reprice logic can kick in.
- `entry_live_poll_seconds`: polling interval while waiting on an equity entry.
- `entry_live_reprice_attempts`: number of live reprice attempts before giving up.
- `entry_live_reprice_step_frac`: size of each reprice step as a fraction of the entry buffer.
- `extended_hours_enabled`: allow equity orders outside regular hours when the broker permits it.
- `market_exit_regular_hours`: when `true`, stock exits during regular hours can use market orders.

### `candles`

Candlestick pattern filters used by stock strategies and some shared confluence logic. The shipped candle engine only evaluates the latest **3 bars**, so only **1-bar, 2-bar, and 3-bar** patterns are registered.

| Option                         | Code default                                 |
|--------------------------------|----------------------------------------------|
| `bullish_patterns`             | `['bullish_1c', 'bullish_2c', 'bullish_3c']` |
| `bearish_patterns`             | `['bearish_1c', 'bearish_2c', 'bearish_3c']` |
| `opposing_net_score_threshold` | `0.70`                                       |

Valid pattern tokens use the exact TA-Lib candlestick function names, plus group shortcuts:

- Group shortcuts: `bullish_1c`, `bullish_2c`, `bullish_3c`, `bearish_1c`, `bearish_2c`, `bearish_3c`, `all`
- Bullish 1c: `CDLDRAGONFLYDOJI`, `CDLHAMMER`, `CDLINVERTEDHAMMER`, `CDLTAKURI`, `CDLBELTHOLD`, `CDLCLOSINGMARUBOZU`, `CDLLONGLINE`, `CDLMARUBOZU`
- Bearish 1c: `CDLGRAVESTONEDOJI`, `CDLHANGINGMAN`, `CDLSHOOTINGSTAR`, `CDLBELTHOLD`, `CDLCLOSINGMARUBOZU`, `CDLLONGLINE`, `CDLMARUBOZU`
- Bullish 2c: `CDLHOMINGPIGEON`, `CDLMATCHINGLOW`, `CDLPIERCING`, `TWEEZER_BOTTOM`, `CDLCOUNTERATTACK`, `CDLDOJISTAR`, `CDLENGULFING`, `CDLHARAMI`, `CDLHARAMICROSS`, `CDLKICKING`, `CDLKICKINGBYLENGTH`, `CDLSEPARATINGLINES`
- Bearish 2c: `CDLDARKCLOUDCOVER`, `CDLINNECK`, `CDLONNECK`, `CDLTHRUSTING`, `TWEEZER_TOP`, `CDLCOUNTERATTACK`, `CDLDOJISTAR`, `CDLENGULFING`, `CDLHARAMI`, `CDLHARAMICROSS`, `CDLKICKING`, `CDLKICKINGBYLENGTH`, `CDLSEPARATINGLINES`
- Bullish 3c: `CDL3STARSINSOUTH`, `CDL3WHITESOLDIERS`, `CDLMORNINGDOJISTAR`, `CDLMORNINGSTAR`, `CDLSTICKSANDWICH`, `CDLUNIQUE3RIVER`, `CDL3INSIDE`, `CDL3OUTSIDE`, `CDLABANDONEDBABY`, `CDLGAPSIDESIDEWHITE`, `CDLHIKKAKE`, `CDLTASUKIGAP`, `CDLTRISTAR`, `CDLXSIDEGAP3METHODS`
- Bearish 3c: `CDL2CROWS`, `CDL3BLACKCROWS`, `CDLADVANCEBLOCK`, `CDLEVENINGDOJISTAR`, `CDLEVENINGSTAR`, `CDLIDENTICAL3CROWS`, `CDLSTALLEDPATTERN`, `CDLUPSIDEGAP2CROWS`, `CDL3INSIDE`, `CDL3OUTSIDE`, `CDLABANDONEDBABY`, `CDLGAPSIDESIDEWHITE`, `CDLHIKKAKE`, `CDLTASUKIGAP`, `CDLTRISTAR`, `CDLXSIDEGAP3METHODS`

Behavior:

- `bullish_patterns`: list of bullish candlestick patterns allowed for bullish pattern checks.
- `bearish_patterns`: list of bearish candlestick patterns allowed for bearish pattern checks.
- `opposing_net_score_threshold`: minimum opposing `net_score` required for `shared_entry.use_opposing_candle_filter` to block an entry or `shared_exit.use_candle_pattern_exit` to fire. `0.70` matches the "solid" confirm tier (≥2 corroborating candles); raise toward `1.0` for strong-only, lower for more aggressive filtering.
- Using a shorter list makes the bot more selective.
- Using `all` enables every registered pattern on that side.
- Candle groups stay underscore-only, while individual candle names use exact TA-Lib `CDL...` names plus the custom `TWEEZER_TOP` / `TWEEZER_BOTTOM` tokens.
- The weighted candle summary treats **3-bar > 2-bar > 1-bar**, uses the strongest same-side hit as the anchor, adds only a small corroboration bonus for extra same-side hits, and penalizes conflicting opposite-side hits instead of fully stacking overlaps.
- Presets that are candle-driven now ship with all `1c/2c/3c` groups enabled, while presets for strategies that do not consume candle logic directly leave those lists empty.

### `chart_patterns`

Intraday structure / chart-pattern detection used as entry filters, confluence, and exits.

| Option                  | Code default                                                                                                                                                                                                                                      |
|-------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `enabled`               | `true`                                                                                                                                                                                                                                            |
| `lookback_bars`         | `32`                                                                                                                                                                                                                                              |
| `bullish_patterns`      | `['bullish_double_bottom', 'bullish_inverse_head_and_shoulders', 'bullish_falling_wedge', 'bullish_broadening_bottom', 'bullish_triple_bottom', 'bullish_flag', 'bullish_pennant', 'bullish_ascending_triangle', 'bullish_symmetrical_triangle']` |
| `bearish_patterns`      | `['bearish_double_top', 'bearish_head_and_shoulders', 'bearish_rising_wedge', 'bearish_broadening_top', 'bearish_triple_top', 'bearish_flag', 'bearish_pennant', 'bearish_descending_triangle', 'bearish_symmetrical_triangle']`                  |

Valid pattern tokens:

- Bullish patterns: `bullish_ascending_triangle`, `bullish_broadening_bottom`, `bullish_double_bottom`, `bullish_falling_wedge`, `bullish_flag`, `bullish_inverse_head_and_shoulders`, `bullish_pennant`, `bullish_symmetrical_triangle`, `bullish_triple_bottom`
- Bearish patterns: `bearish_broadening_top`, `bearish_descending_triangle`, `bearish_double_top`, `bearish_flag`, `bearish_head_and_shoulders`, `bearish_pennant`, `bearish_rising_wedge`, `bearish_symmetrical_triangle`, `bearish_triple_top`
- Group shortcuts: `bullish`, `bullish_reversal`, `bullish_continuation`, `bullish_all`, `bearish`, `bearish_reversal`, `bearish_continuation`, `bearish_all`, `all`

Behavior:

- `enabled`: master on/off for chart-pattern detection.
- `lookback_bars`: bars inspected when scanning for patterns.
- `bullish_patterns` / `bearish_patterns`: allowed pattern lists. Shorter lists make the filter narrower.
- Entry/exit toggles for opposing-pattern gating live in `shared_entry.use_opposing_chart_filter` and `shared_exit.use_chart_pattern_exit`.

### `paper`

Paper-account storage and dashboard-history settings.

| Option              | Code default |
|---------------------|--------------|
| `starting_equity`   | `25000.0`    |
| `max_equity_points` | `2000`       |
| `max_trade_history` | `200`        |

Behavior:

- `starting_equity`: starting paper-equity balance used in dry-run/paper mode. In live mode, the dashboard tracked-capital baseline uses `max_total_notional` and is labeled `Allocated Capital`.
- `max_equity_points`: max equity-curve points retained for the dashboard.
- `max_trade_history`: max closed trades kept in the paper account history.

### `dashboard`

Controls the local dashboard server and its charting profiles.

| Option         | Code default                 |
|----------------|------------------------------|
| `enabled`      | `true`                       |
| `host`         | `127.0.0.1`                  |
| `port`         | `8765`                       |
| `refresh_ms`   | `2000`                       |
| `state_path`   | `.logs/dashboard_state.json` |
| `theme`        | `default`                    |
| `https`        | `false`                      |
| `ssl_certfile` | `""`                         |
| `ssl_keyfile`  | `""`                         |

Behavior:

- `enabled`: turn the dashboard server on or off.
- `host` / `port`: bind address and port.
- `refresh_ms`: browser refresh interval in milliseconds.
- `state_path`: JSON state snapshot used by the dashboard.
- `theme`: dashboard theme. Set to the folder name of any theme under `intraday_tv_schwab_bot/dashboard_assets/themes/`. Shipped themes:
  - `default` — blue-tinted dark with glow gradients (the original look).
  - `dark` — pure black background with translucent glass panels and subtle white edge lighting.
  - `light` — clean white background with light panels and dark text.
  - `nexus` — near-black with mint/teal accent, soft radial glows.
  - `solstice` — near-black with warm amber/coral accent.
  - `nebula` — near-black with violet/purple accent.
  - `example_custom` — starter template showing how to build a fully custom dashboard (see "Custom themes" below).

  If the configured theme folder is missing or the name is malformed, the server logs a warning and falls back to `default`. Chart colors are not affected by theme tokens.

##### Custom themes

Themes are plugin folders. Drop `intraday_tv_schwab_bot/dashboard_assets/themes/<your_name>/` in place and set `dashboard.theme: <your_name>` in config. The folder can contain:

| File          | Purpose                                                                                                                                                                                                                                                                          |
|---------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `theme.css`   | Color/visual tokens (`--accent`, `--bg`, `--good`, …). Loaded **after** the base `dashboard.css`, so any selector here overrides it.                                                                                                                                             |
| `theme.js`    | Optional JS hook. Loaded after `dashboard.js` with `defer onerror="this.remove()"` — if the file is missing the tag quietly self-removes.                                                                                                                                        |
| `index.html`  | Optional full template override for the desktop dashboard. When present, replaces the base `dashboard.html` entirely; the theme then owns the whole page and only talks to the backend via `/api/state`, `/api/chart`, `/health`.                                                |
| `mobile.html` | Same as above but for `/mobile`.                                                                                                                                                                                                                                                 |
| `assets/…`    | Theme-owned images / fonts / extra CSS / JS, served at `/themes/<your_name>/assets/<path>`. Extensions are whitelisted (`png`, `jpg`, `jpeg`, `webp`, `gif`, `svg`, `ico`, `woff`, `woff2`, `ttf`, `otf`, `css`, `js`, `json`, `map`, `mp3`, `wav`) — anything else returns 415. |

Template substitutions applied to both the base and any per-theme `index.html` / `mobile.html`:
`__REFRESH_MS__`, `__IMAGES__` (JSON), `__BRAND_BADGE__` (data URI), `__THEME__` (the active theme folder name).

Theme folder names must match `^[a-z0-9_-]{1,40}$`. Copy `themes/example_custom/` as a starting point — it has a minimal `index.html`, `theme.css`, and `theme.js` showing the substitutions and a `fetch('/api/state')` poll loop.
- `https`: serve the dashboard over HTTPS instead of HTTP. Requires `ssl_certfile`.
- `ssl_certfile`: path to the PEM-encoded SSL certificate file.
- `ssl_keyfile`: path to the PEM-encoded SSL private key file. If the key is included in the certfile, this can be left empty.
- `charting`: nested chart settings with two layers:
  - `compact`: settings for the embedded chart
  - `expanded`: settings for the click-out expanded chart

#### `dashboard.charting` option reference

Use the same option names under `shared`, `compact`, or `expanded`.

| Option                                | Code default |
|---------------------------------------|--------------|
| `max_bars`                            | `90`         |
| `show_volume`                         | `false`      |
| `show_moving_averages`                | `true`       |
| `show_vwap`                           | `true`       |
| `show_support_resistance`             | `true`       |
| `show_next_support_resistance`        | `true`       |
| `show_full_support_resistance_ladder` | `false`      |
| `show_key_level_zones`                | `true`       |
| `show_key_level_zone_labels`          | `true`       |
| `show_bollinger_bands`                | `false`      |
| `show_anchored_vwap`                  | `false`      |
| `show_fib_extensions`                 | `false`      |
| `show_channel`                        | `false`      |
| `show_trendlines`                     | `false`      |
| `show_htf_fair_value_gaps`            | `false`      |
| `show_1m_fair_value_gaps`             | `false`      |
| `show_htf_order_blocks`               | `false`      |
| `show_1m_order_blocks`                | `false`      |
| `show_trade_markers`                  | `true`       |
| `tooltip_show_returns`                | `true`       |
| `tooltip_show_support_resistance`     | `true`       |
| `tooltip_show_structure`              | `true`       |
| `tooltip_show_volatility`             | `true`       |
| `tooltip_show_orderflow`              | `true`       |
| `tooltip_show_patterns`               | `true`       |

How charting options behave:

- `max_bars`: how many bars to draw. Compact and expanded profiles clamp to sane limits.
- `show_volume`: show or hide the volume panel.
- `show_moving_averages`, `show_vwap`: core price overlays.
- `show_support_resistance`, `show_next_support_resistance`, `show_full_support_resistance_ladder`: how much of the support/resistance map to draw.
- `show_key_level_zones`, `show_key_level_zone_labels`: peer-confirmed zone overlays.
- `show_bollinger_bands`, `show_anchored_vwap`, `show_fib_extensions`, `show_channel`, `show_trendlines`: heavier technical overlays.
- `show_htf_fair_value_gaps`, `show_1m_fair_value_gaps`: higher-timeframe and one-minute FVG overlays. Cross-timeframe protection is enforced automatically, so HTF FVGs do not render on 1m charts and 1m FVGs do not render on HTF charts.
- `show_htf_order_blocks`, `show_1m_order_blocks`: higher-timeframe and one-minute order block overlays. Render with a dashed-line border around a very faint fill so they are visually distinct from the solid-filled FVG overlays. Same green/red bullish/bearish color semantics. Driven by `support_resistance.htf_order_blocks_enabled` / `one_minute_order_blocks_enabled` and the shared OB tuning knobs (`order_block_mode`, etc.). Cross-timeframe protection is enforced the same way as for FVGs.
- `show_trade_markers`: entry/exit markers on the chart.
- `tooltip_show_*`: toggle tooltip sections individually.
- In `compact` and `expanded`, using `null` means “inherit from `shared`.”

### `support_resistance`

Higher-timeframe support/resistance, prior-day/week levels, FVG mapping, flip handling, and market-structure context.

| Option                                   | Code default |
|------------------------------------------|--------------|
| `enabled`                                | `true`       |
| `timeframe_minutes`                      | `15`         |
| `lookback_days`                          | `10`         |
| `refresh_seconds`                        | `480`        |
| `pivot_span`                             | `2`          |
| `max_levels_per_side`                    | `3`          |
| `atr_tolerance_mult`                     | `0.6`        |
| `pct_tolerance`                          | `0.003`      |
| `same_side_min_gap_atr_mult`             | `0.1`        |
| `same_side_min_gap_pct`                  | `0.0015`     |
| `fallback_reference_max_drift_atr_mult`  | `1.0`        |
| `fallback_reference_max_drift_pct`       | `0.01`       |
| `proximity_atr_mult`                     | `0.7`        |
| `breakout_atr_mult`                      | `0.3`        |
| `breakout_buffer_pct`                    | `0.0012`     |
| `stop_buffer_atr_mult`                   | `0.25`       |
| `entry_min_clearance_atr`                | `0.85`       |
| `entry_min_clearance_pct`                | `0.0038`     |
| `entry_proximity_scoring_enabled`        | `true`       |
| `entry_bias_score_weight`                | `0.5`        |
| `entry_favorable_proximity_bonus`        | `0.3`        |
| `entry_opposing_proximity_penalty`       | `0.3`        |
| `use_prior_day_high_low`                 | `true`       |
| `use_prior_week_high_low`                | `true`       |
| `htf_fair_value_gaps_enabled`            | `true`       |
| `one_minute_fair_value_gaps_enabled`     | `true`       |
| `fair_value_gap_max_per_side`            | `3`          |
| `fair_value_gap_min_atr_mult`            | `0.06`       |
| `fair_value_gap_min_pct`                 | `0.0006`     |
| `dashboard_flip_confirmation_1m_bars`    | `1`          |
| `trading_flip_confirmation_1m_bars`      | `2`          |
| `trading_flip_confirmation_5m_bars`      | `1`          |
| `flip_stop_buffer_atr_mult`              | `0.25`       |
| `flip_target_requires_momentum_confirm`  | `true`       |
| `regime_weight`                          | `0.7`        |
| `structure_enabled`                      | `true`       |
| `structure_1m_pivot_span`                | `2`          |
| `structure_eq_atr_mult`                  | `0.25`       |
| `structure_1m_weight`                    | `0.65`       |
| `structure_htf_weight`                   | `0.85`       |
| `structure_event_lookback_bars`          | `6`          |
| `structure_exit_grace_minutes`           | `10`         |
| `structure_exit_min_post_entry_pivots`   | `2`          |
| `orb_entry_exit_grace_minutes`           | `20`         |

How the groups work:

- Core HTF level map:
  - `enabled`, `timeframe_minutes`, `lookback_days`, `refresh_seconds`, `pivot_span`, `max_levels_per_side`
  - Use these to decide how the level map is built and refreshed.
- Level width and proximity:
  - `atr_tolerance_mult`, `pct_tolerance`, `same_side_min_gap_atr_mult`, `same_side_min_gap_pct`, `fallback_reference_max_drift_atr_mult`, `fallback_reference_max_drift_pct`, `proximity_atr_mult`, `breakout_atr_mult`, `breakout_buffer_pct`, `stop_buffer_atr_mult`
  - Larger values make levels and breakout/stop buffers looser; smaller values make them tighter.
  - `same_side_min_gap_*` adds an extra upstream minimum-separation pass so near-duplicate same-side ladder levels collapse before they reach the engine payloads and dashboard. This now applies consistently to both the support/resistance builder and the HTF level map.
  - `fallback_reference_max_drift_*` guards prior day/week fallback side classification against stale or detached live prices by snapping that fallback-only reference back to the latest bar close when the drift is too large.
- Entry clearance and scoring:
  - `entry_min_clearance_atr`, `entry_min_clearance_pct`, `entry_proximity_scoring_enabled`, `entry_bias_score_weight`, `entry_favorable_proximity_bonus`, `entry_opposing_proximity_penalty`
  - These decide how strongly nearby HTF levels help or hurt an entry.
- Static reference levels:
  - `use_prior_day_high_low`, `use_prior_week_high_low`
  - Allow prior-day / prior-week highs and lows as fallback levels only when a side ends up empty after normal S/R detection and cleanup. Those fallback levels are admitted on the correct side of the guarded fallback reference price, can still participate in normal flip handling, and flow through the normal S/R ladder logic. If no prior-day/week fallback is eligible, the builders can fall back to the frame extreme for that side.
- FVG detection:
  - `htf_fair_value_gaps_*` controls HTF FVG generation.
  - `one_minute_fair_value_gaps_*` controls 1-minute FVG generation.
  - Raising the min ATR or min percent thresholds makes FVG detection more selective.
- Level-loss and flip behavior:
  - `dashboard_flip_confirmation_1m_bars`, `trading_flip_confirmation_1m_bars`, `trading_flip_confirmation_5m_bars` tune how many bars confirm a flip for display vs trading.
  - Whether confirmed level-loss breaks trigger an exit is controlled by `shared_exit.use_sr_loss_exit`.
  - `flip_stop_buffer_atr_mult` controls how far beyond a flipped level the stop is anchored when `risk.trade_management_mode: sr_flip` is active.
  - `flip_target_requires_momentum_confirm` prevents target extension on weak flips.
- Regime and structure:
  - `regime_weight` controls how strongly the S/R regime influences scoring.
  - `structure_enabled`, `structure_1m_pivot_span`, `structure_eq_atr_mult`, `structure_1m_weight`, `structure_htf_weight`, `structure_event_lookback_bars` control the mixed-timeframe structure layer. The CHoCH-exit toggle is `shared_exit.use_structure_exit`.
- Structure-exit grace windows:
  - `structure_exit_grace_minutes` (default `10`) suppresses `structure_bearish_exit` / `structure_bullish_exit` for the first N minutes after entry. Prevents a minor EQL/LL pivot forming in the first few minutes from exiting an otherwise-healthy trade. CHoCH exits still fire.
  - `structure_exit_min_post_entry_pivots` (default `2`) requires at least N new 1m pivots to form AFTER entry before structure-based bias exits can fire. Complements the time grace.
  - `orb_entry_exit_grace_minutes` (default `20`) extends the grace specifically for positions entered during the ORB window (09:35 to `orb_end_time`). Suppresses BOTH `structure_bearish/bullish_exit` AND `chart_pattern_exit` for the first N minutes of ORB-entered trades. ORB pullbacks frequently look like bearish structure breaks but continue higher once the opening flush resolves. Set to `0` to disable. CHoCH exits still fire.

### `technical_levels`

Optional technical overlays used as confluence, refinement, and exits.

| Option                                | Code default                            |
|---------------------------------------|-----------------------------------------|
| `enabled`                             | `true`                                  |
| `fib_enabled`                         | `true`                                  |
| `fib_lookback_bars`                   | `120`                                   |
| `fib_min_impulse_atr`                 | `1.25`                                  |
| `fib_near_extension_pct`              | `0.0055`                                |
| `anchored_vwap_impulse_lookback_bars` | `null -> fib_lookback_bars`             |
| `anchored_vwap_min_impulse_atr`       | `null -> fib_min_impulse_atr`           |
| `anchored_vwap_pivot_span`            | `null -> support_resistance pivot span` |
| `channel_enabled`                     | `true`                                  |
| `channel_lookback_bars`               | `120`                                   |
| `channel_min_touches`                 | `3`                                     |
| `channel_atr_tolerance_mult`          | `0.35`                                  |
| `channel_parallel_slope_frac`         | `0.12`                                  |
| `channel_min_gap_atr_mult`            | `0.8`                                   |
| `channel_min_gap_pct`                 | `0.0025`                                |
| `channel_near_edge_pct`               | `0.16`                                  |
| `trendline_enabled`                   | `true`                                  |
| `trendline_lookback_bars`             | `120`                                   |
| `trendline_min_touches`               | `3`                                     |
| `trendline_atr_tolerance_mult`        | `0.35`                                  |
| `trendline_breakout_buffer_atr_mult`  | `0.15`                                  |
| `adx_enabled`                         | `true`                                  |
| `adx_length`                          | `14`                                    |
| `adx_min_strength`                    | `18.0`                                  |
| `adx_entry_bonus`                     | `0.2`                                   |
| `adx_rising_bonus`                    | `0.08`                                  |
| `adx_weak_penalty`                    | `0.12`                                  |
| `anchored_vwap_enabled`               | `true`                                  |
| `anchored_vwap_entry_bonus`           | `0.2`                                   |
| `anchored_vwap_entry_penalty`         | `0.18`                                  |
| `atr_context_enabled`                 | `true`                                  |
| `atr_expansion_lookback`              | `5`                                     |
| `atr_expansion_min_mult`              | `0.8`                                   |
| `atr_expansion_bonus`                 | `0.12`                                  |
| `atr_stretch_penalty_mult`            | `2.6`                                   |
| `atr_stretch_penalty`                 | `0.2`                                   |
| `obv_enabled`                         | `true`                                  |
| `obv_ema_length`                      | `20`                                    |
| `obv_entry_bonus`                     | `0.1`                                   |
| `obv_entry_penalty`                   | `0.08`                                  |
| `divergence_enabled`                  | `true`                                  |
| `divergence_rsi_length`               | `14`                                    |
| `divergence_rsi_min_delta`            | `2.5`                                   |
| `divergence_obv_min_volume_frac`      | `0.65`                                  |
| `divergence_counter_rsi_penalty`      | `0.12`                                  |
| `divergence_counter_obv_penalty`      | `0.1`                                   |
| `divergence_block_dual_counter`       | `true`                                  |
| `bollinger_enabled`                   | `true`                                  |
| `bollinger_length`                    | `20`                                    |
| `bollinger_std_mult`                  | `2.0`                                   |
| `bollinger_squeeze_width_pct`         | `0.06`                                  |
| `bollinger_entry_bonus_midband`       | `0.16`                                  |
| `bollinger_entry_penalty_outer_band`  | `0.22`                                  |
| `target_use_bollinger`                | `false`                                 |
| `target_use_fib`                      | `true`                                  |
| `target_use_channel`                  | `true`                                  |
| `target_use_trendline`                | `true`                                  |
| `stop_use_trendline`                  | `true`                                  |
| `entry_bonus_channel_alignment`       | `0.2`                                   |
| `entry_bonus_trendline_respect`       | `0.2`                                   |
| `entry_penalty_near_extension`        | `0.4`                                   |

How the groups work:

- Global enable:
  - `enabled` turns the whole block on or off.
- Fib extensions:
  - `fib_*` controls extension detection and how close price must be before fib levels matter.
- Channels:
  - `channel_*` controls pivot channel detection, tolerance, and how near price must be to a channel edge.
- Trend lines:
  - `trendline_*` controls pivot trendline detection and breakout sensitivity.
- ADX / trend quality:
  - `adx_*` adds bonus/penalty based on trend strength and whether ADX is rising.
- Anchored VWAP:
  - `anchored_vwap_*` rewards entries aligned with anchored VWAP and can also contribute to exits.
  - `anchored_vwap_impulse_lookback_bars`, `anchored_vwap_min_impulse_atr`, and `anchored_vwap_pivot_span` optionally decouple impulse-anchor selection from Fib settings while preserving old behavior when left `null`.
- ATR context:
  - `atr_context_*`, `atr_expansion_*`, and `atr_stretch_*` reward healthy expansion and penalize stretched entries.
- OBV / participation:
  - `obv_*` rewards or penalizes order-flow participation.
- Divergence:
  - `divergence_*` controls RSI/OBV divergence filters. Higher penalties make countertrend divergence more restrictive.
- Bollinger context:
  - `bollinger_*` controls band calculation, squeeze detection, entry confluence, optional targeting, and optional exits.
- Target/stop refinement:
  - `target_use_bollinger`, `target_use_fib`, `target_use_channel`, `target_use_trendline`, `stop_use_trendline`
- Entry bonuses/penalties:
  - `entry_bonus_channel_alignment`, `entry_bonus_trendline_respect`, `entry_penalty_near_extension`
- Exit toggles live in `shared_exit.use_trendline_break`, `use_channel_break`, `use_bollinger_reject`, `use_anchored_vwap_loss` (detectors still owned here; when to fire is decided there).

### `shared_entry`

Global entry-side helper toggles. These replace the old per-strategy logic-override system.

| Option                                 | Code default |
|----------------------------------------|--------------|
| `use_fvg_context`                      | `true`       |
| `use_divergence_filter`                | `true`       |
| `use_technical_entry_adjustment`       | `true`       |
| `use_technical_stop_target_refinement` | `true`       |
| `use_structure_filter`                 | `true`       |
| `use_sr_filter`                        | `true`       |
| `use_sr_stop_target_refinement`        | `true`       |
| `use_opposing_chart_filter`            | `true`       |
| `use_opposing_candle_filter`           | `false`      |
| `min_target_rr`                        | `1.0`        |

Behavior:

- All boolean fields default to `true`. `true` enables that shared entry helper globally; `false` disables it globally.
- `use_fvg_context`: allow FVG context to adjust entry scoring.
- `use_divergence_filter`: allow RSI/OBV divergence to penalize or block entries.
- `use_technical_entry_adjustment`: let `technical_levels` adjust entry quality scoring.
- `use_technical_stop_target_refinement`: allow `technical_levels` to refine stops/targets.
- `use_structure_filter`: allow the mixed-timeframe structure layer to help or hurt entries.
- `use_sr_filter`: allow HTF support/resistance clearance and regime context to affect entries.
- `use_sr_stop_target_refinement`: allow support/resistance to refine initial stops/targets.
- `use_opposing_chart_filter`: block entries when opposing chart patterns are present.
- `use_opposing_candle_filter`: block entries when opposing-direction candle patterns cluster above `candles.opposing_net_score_threshold` (default 0.70 = "solid" tier). Reuses the cached candle context — no extra ta-lib calls.
- `min_target_rr` (float, default `1.0`): risk-to-reward floor enforced by **all four** target-refinement passes (`_refine_bullish_sr_levels`, `_refine_bearish_sr_levels`, `_refine_bullish_technical_levels`, `_refine_bearish_technical_levels`). When a refine pass would cap the target so close to entry that R:R drops below this threshold, the cap is rejected and the strategy's original target is kept. Protects against the "$0.10 target" failure mode where nearby S/R or technical levels collapse R:R toward zero. The check is independent of `risk.trade_management_mode` — it applies whether you run `adaptive`, `adaptive_ladder`, `none`, `sr_flip`, or any other mode. Set higher (e.g. `1.5` or `2.0`) for stricter quality, lower (or `0`) to disable.

### `shared_exit`

Global exit-side helper toggles and tape-confirmation thresholds.

| Option                             | Code default |
|------------------------------------|--------------|
| `use_technical_exit`               | `true`       |
| `use_trendline_break`              | `true`       |
| `use_channel_break`                | `true`       |
| `use_bollinger_reject`             | `false`      |
| `use_anchored_vwap_loss`           | `true`       |
| `use_chart_pattern_exit`           | `false`      |
| `use_candle_pattern_exit`          | `false`      |
| `use_structure_exit`               | `true`       |
| `use_sr_loss_exit`                 | `true`       |
| `confirm_with_ema9`                | `true`       |
| `confirm_with_ema20`               | `true`       |
| `confirm_with_vwap`                | `true`       |
| `confirm_with_close_position`      | `true`       |
| `bullish_close_position_max`       | `0.46`       |
| `bearish_close_position_min`       | `0.54`       |
| `bullish_close_position_loose_max` | `0.38`       |
| `bearish_close_position_loose_min` | `0.62`       |

Behavior:

- The `use_*` fields are booleans.
- `use_technical_exit`: master enable for technical exits.
- `use_trendline_break`, `use_channel_break`, `use_bollinger_reject`, `use_anchored_vwap_loss`: finer control over which technical exits are allowed.
- `use_chart_pattern_exit`: allow opposing chart patterns to help trigger exits.
- `use_candle_pattern_exit`: fire `candle_pattern_exit:<pattern>` when an opposing-direction candle cluster crosses `candles.opposing_net_score_threshold` and the tape confirms (via `confirm_with_*` thresholds below). Reuses cached candle context.
- `use_structure_exit`: allow CHOCH / structure-loss exits.
- `use_sr_loss_exit`: allow exits on confirmed loss of important support/resistance.
- `confirm_with_ema9`, `confirm_with_ema20`, `confirm_with_vwap`, `confirm_with_close_position`: tape-confirmation requirements applied before shared exits are accepted.
- `bullish_close_position_max` / `bearish_close_position_min`: strict candle close-location thresholds used when confirming bearish exits from long trades or bullish exits from short trades.
- `bullish_close_position_loose_max` / `bearish_close_position_loose_min`: looser fallback thresholds for the same confirmation family.

### `options`

Shared 0DTE ETF option-engine settings. Both option strategies use this block.

| Option                               | Code default                                                                                                 |
|--------------------------------------|--------------------------------------------------------------------------------------------------------------|
| `enabled`                            | `true`                                                                                                       |
| `underlyings`                        | `['SPY', 'QQQ']`                                                                                             |
| `confirmation_symbols`               | `{'SPY': '$SPX', 'QQQ': '$COMPX', 'IWM': '$RUT'}`                                                            |
| `volatility_symbol`                  | `VIX`                                                                                                        |
| `styles`                             | `['orb_debit_spread', 'trend_debit_spread', 'midday_credit_spread', 'orb_long_option', 'trend_long_option']` |
| `min_underlying_price`               | `100.0`                                                                                                      |
| `min_option_volume`                  | `300`                                                                                                        |
| `min_open_interest`                  | `600`                                                                                                        |
| `max_bid_ask_spread_pct`             | `0.1`                                                                                                        |
| `max_leg_spread_dollars`             | `0.08`                                                                                                       |
| `max_net_spread_pct`                 | `0.2`                                                                                                        |
| `max_net_spread_price`               | `2.8`                                                                                                        |
| `min_net_mid_price`                  | `0.25`                                                                                                       |
| `target_long_delta`                  | `0.38`                                                                                                       |
| `target_short_delta`                 | `0.23`                                                                                                       |
| `target_single_delta`                | `0.28`                                                                                                       |
| `max_single_option_price`            | `2.25`                                                                                                       |
| `option_limit_mode`                  | `mid`                                                                                                        |
| `strike_width_by_symbol`             | `{'SPY': 2.0, 'QQQ': 2.0, 'IWM': 1.0}`                                                                       |
| `max_contracts_per_trade`            | `1`                                                                                                          |
| `max_loss_per_trade`                 | `200.0`                                                                                                      |
| `debit_stop_frac`                    | `0.45`                                                                                                       |
| `debit_target_mult`                  | `1.45`                                                                                                       |
| `credit_stop_mult`                   | `1.65`                                                                                                       |
| `credit_target_frac`                 | `0.32`                                                                                                       |
| `single_stop_frac`                   | `0.38`                                                                                                       |
| `single_target_mult`                 | `1.5`                                                                                                        |
| `force_flatten_time`                 | `15:18`                                                                                                      |
| `max_vix`                            | `22.5`                                                                                                       |
| `vix_spike_pct`                      | `0.011`                                                                                                      |
| `vertical_limit_mode`                | `mid`                                                                                                        |
| `quote_stability_checks`             | `3`                                                                                                          |
| `quote_stability_pause_ms`           | `500`                                                                                                        |
| `max_mid_drift_pct`                  | `0.06`                                                                                                       |
| `max_quote_age_seconds`              | `6`                                                                                                          |
| `dry_run_replace_attempts`           | `2`                                                                                                          |
| `dry_run_step_frac`                  | `0.25`                                                                                                       |
| `event_blackout_file`                | `./macro_events.auto.yaml`                                                                                   |
| `event_blackouts`                    | `[]`                                                                                                         |
| `option_chain_cache_seconds`         | `6`                                                                                                          |
| `option_chain_cache_max_entries`     | `24`                                                                                                         |
| `options_breakeven_enabled`          | `false`                                                                                                      |
| `options_breakeven_mark_mult`        | `1.25`                                                                                                       |
| `options_breakeven_stop_mult`        | `1.05`                                                                                                       |
| `options_profit_lock_enabled`        | `false`                                                                                                      |
| `options_profit_lock_mark_mult`      | `1.40`                                                                                                       |
| `options_profit_lock_stop_mult`      | `1.15`                                                                                                       |
| `debit_target_time_decay_enabled`    | `false`                                                                                                      |
| `debit_target_time_decay_start`      | `10:30`                                                                                                      |
| `debit_target_time_decay_end`        | `14:00`                                                                                                      |
| `debit_target_time_decay_min_scale`  | `0.70`                                                                                                       |
| `debit_stop_time_decay_widen_factor` | `0.30`                                                                                                       |
| `delta_time_shift_enabled`           | `false`                                                                                                      |
| `delta_time_shift_per_hour`          | `0.025`                                                                                                      |
| `delta_time_shift_max`               | `0.15`                                                                                                       |
| `delta_time_shift_start`             | `10:00`                                                                                                      |
| `trend_momentum_filter_enabled`      | `false`                                                                                                      |
| `trend_min_atr_expansion`            | `0.85`                                                                                                       |
| `trend_min_volume_ratio`             | `0.90`                                                                                                       |
| `credit_distance_gate_enabled`       | `false`                                                                                                      |
| `min_credit_distance_atr`            | `1.8`                                                                                                        |
| `adaptive_width_enabled`             | `false`                                                                                                      |
| `adaptive_width_max_scale`           | `1.5`                                                                                                        |

Behavior and valid values:

- Underlying universe and symbols:
  - `enabled`: master on/off.
  - `underlyings`: list of ETF underlyings the option engine may trade.
  - `confirmation_symbols`: mapping from underlying to confirmation index symbol.
  - `volatility_symbol`: symbol used as the volatility regime input.
  - `styles`: valid values are `orb_debit_spread`, `trend_debit_spread`, `midday_credit_spread`, `orb_long_option`, `trend_long_option`. The spread strategy uses the spread styles; the long-option strategy uses only `orb_long_option` and `trend_long_option` for its two directional style gates.
- Basic chain quality filters:
  - `min_underlying_price`, `min_option_volume`, `min_open_interest` filter the option universe.
  - `max_bid_ask_spread_pct`, `max_leg_spread_dollars`, `max_net_spread_pct`, `max_net_spread_price`, `min_net_mid_price` filter quote quality.
- Target deltas / structure:
  - `target_long_delta`, `target_short_delta` are used for vertical spreads.
  - `target_single_delta` is used for long-premium single legs.
  - `strike_width_by_symbol` optionally overrides vertical width by symbol.
- Position sizing and loss limits:
  - `max_contracts_per_trade`, `max_loss_per_trade`
- Exit math:
  - `debit_stop_frac`, `debit_target_mult`, `credit_stop_mult`, `credit_target_frac`, `single_stop_frac`, `single_target_mult`
- Entry pricing:
  - `option_limit_mode` and `vertical_limit_mode` valid values are `mid`, `natural`, `bid`.
    - `mid`: price off mid when possible.
    - `natural`: pay/receive the natural side first, then fall back.
    - `bid`: price more defensively.
- Time controls:
  - `force_flatten_time`: 24-hour `HH:MM` time string used by option strategies to flatten before the close.
- Volatility guards:
  - `max_vix`, `vix_spike_pct`
- Quote-stability checks:
  - `quote_stability_checks`, `quote_stability_pause_ms`, `max_mid_drift_pct`, `max_quote_age_seconds`
  - These make the bot re-check quotes before finalizing a trade.
- Dry-run replace controls:
  - `dry_run_replace_attempts`, `dry_run_step_frac`
- Macro-event blackout controls:
  - `event_blackout_file`: YAML file with recurring or dated blackout windows.
  - `event_blackouts`: inline list of blackout rows. File rows and inline rows are both loaded.
  - Each blackout row can use: `enabled`, `label`, `date`, `weekday`, `start`, `end`, `block_new_entries`, `force_flatten`.
- Chain cache:
  - `option_chain_cache_seconds`, `option_chain_cache_max_entries`
- Premium ratchet (post-entry stop management). All four premium-ratchet families are off by default; enable the ones you want active.
  - `options_breakeven_enabled` / `options_breakeven_mark_mult` / `options_breakeven_stop_mult`: when the option mark crosses `entry × options_breakeven_mark_mult`, ratchet the stop up to `entry × options_breakeven_stop_mult`. Locks a small protective gain on debit trades that go through their first push.
  - `options_profit_lock_enabled` / `options_profit_lock_mark_mult` / `options_profit_lock_stop_mult`: a second, looser ratchet that activates at a higher mark multiple and locks a larger fraction of the move. Stacks with `options_breakeven_*`.
- Time-decay-aware stop/target scaling for debit trades:
  - `debit_target_time_decay_enabled`: master toggle. When `true`, the debit target shrinks linearly between `debit_target_time_decay_start` and `debit_target_time_decay_end` (HH:MM in `runtime.timezone`), and the debit stop widens proportionally so theta-decayed trades aren't stopped on noise.
  - `debit_target_time_decay_start` / `debit_target_time_decay_end`: scaling window. Outside this window, the standard `debit_target_mult` and `debit_stop_frac` apply unchanged.
  - `debit_target_time_decay_min_scale`: lower bound on the target scale at the end of the window (e.g. `0.70` = target collapses to 70% of `debit_target_mult` by `debit_target_time_decay_end`).
  - `debit_stop_time_decay_widen_factor`: how much the stop widens at the end of the window relative to the target shrink (e.g. `0.30` = stop loosens by 30% × the target shrink).
- Time-aware delta selection (long-leg side of debit verticals and singles):
  - `delta_time_shift_enabled`: master toggle. When `true`, after `delta_time_shift_start` the bot adds `delta_time_shift_per_hour × hours_elapsed` to `target_long_delta` / `target_single_delta`, capped at `delta_time_shift_max`. Picks deeper-ITM strikes later in the day to fight theta.
  - `delta_time_shift_per_hour`: shift-per-hour added to the target delta after `delta_time_shift_start`.
  - `delta_time_shift_max`: hard cap on cumulative delta shift.
  - `delta_time_shift_start`: HH:MM at which the shift begins accumulating.
- Trend entry momentum filter (applies to `trend_*` styles):
  - `trend_momentum_filter_enabled`: when `true`, requires both ATR expansion and volume confirmation before a trend entry fires.
  - `trend_min_atr_expansion`: minimum recent-ATR / baseline-ATR ratio required.
  - `trend_min_volume_ratio`: minimum recent-volume / baseline-volume ratio required.
- Credit strike distance gate (applies to `midday_credit_spread`):
  - `credit_distance_gate_enabled`: when `true`, requires the short strike to sit at least `min_credit_distance_atr × ATR` from the current underlying price before a credit spread is allowed.
  - `min_credit_distance_atr`: ATR multiple defining the minimum strike-to-spot distance.
- VIX-adaptive strike width:
  - `adaptive_width_enabled`: when `true`, scales `strike_width_by_symbol` up by a VIX-driven factor capped at `adaptive_width_max_scale`. Wider verticals when VIX is elevated, baseline width when VIX is normal.
  - `adaptive_width_max_scale`: hard upper bound on the per-symbol width multiplier.

## `strategies.<name>` block reference

The standard example/main config does not need a top-level `strategies:` section, but shipped portable presets include one so each preset fully describes the selected strategy. The selected top-level config file is the runtime authority for its own `strategies.<name>` block.

The block uses this outer structure:

- `entry_windows`: list of `[start, end]` windows when the strategy may open new positions.
- `management_windows`: list of `[start, end]` windows used for flat-time background work and default force-flatten timing.
- `screener_windows`: list of `[start, end]` windows when the screener may refresh.
- `params`: strategy-specific tuning dictionary that overrides the manifest defaults for that strategy only.

For TradingView screener percentage fields used by stock strategies, this bot expects **whole percent units** in YAML.

Examples:

- `4.0` means `4%`
- `16.0` means `16%`
- for sub-1% thresholds, use a quoted string like `"0.5%"`

Legacy decimal values such as `0.04` are still accepted and normalized to `4.0` with a warning.

## Shared stock-strategy parameter groups

These groups are reused across several stock strategies.

### Force-flatten

- `force_flatten.long`
- `force_flatten.short`
- Valid values: `true` or `false` for each side
- When a side is `true`, the bot auto-flattens open stock positions on that side at the end of the final `management_windows` block for that strategy.
- When a side is `false`, that side may hold overnight.

### Anti-chase / exhaustion filter

Used by the continuation-style stock strategies.

- `entry_exhaustion_filter_enabled`
- `max_entry_vwap_extension_atr`
- `max_entry_ema9_extension_atr`
- `max_entry_bar_range_atr`
- `max_entry_upper_wick_frac`
- `max_entry_lower_wick_frac`
- `entry_wick_close_position_guard`

Behavior:

- These fields decide whether the last trigger bar is too extended or too rejective to enter immediately.
- Smaller extension or wick thresholds make the bot more conservative.
- Larger thresholds allow more aggressive continuation entries.

### Anti-chase FVG retest defer logic

Used by the continuation-style stock strategies.

- `anti_chase_fvg_retest_enabled`
- `anti_chase_fvg_retest_lookback_bars`
- `anti_chase_fvg_retest_max_gap_distance_pct`
- `anti_chase_fvg_retest_max_opposing_distance_pct`
- `anti_chase_fvg_retest_min_close_position`
- `anti_chase_fvg_retest_stop_buffer_gap_frac`

Behavior:

- When enabled, an overextended continuation entry can shift from **enter now** to **wait for a same-direction 1-minute FVG retest**.
- Larger distance thresholds make the bot accept looser FVG retests.
- Higher `anti_chase_fvg_retest_min_close_position` requires a stronger reclaim candle on the retest.
- `anti_chase_fvg_retest_stop_buffer_gap_frac` controls how tight the stop anchors around the defended FVG.

### Stock FVG confluence

Used by all stock strategies.

- `htf_fvg_entry_weight`
- `one_minute_fvg_entry_weight`
- `opposing_fvg_entry_penalty_mult`
- `fvg_runner_rr_bonus`

Behavior:

- Higher `htf_fvg_entry_weight` or `one_minute_fvg_entry_weight` makes same-direction FVG context matter more.
- Higher `opposing_fvg_entry_penalty_mult` makes nearby opposing FVGs more punitive.
- `fvg_runner_rr_bonus` adds extra room to stronger trades when FVG continuation context is favorable.

### Adaptive stock trade management

Used when `risk.trade_management_mode: adaptive` or `risk.trade_management_mode: adaptive_ladder`.

- `adaptive_breakeven_rr`
- `adaptive_profit_lock_rr`
- `adaptive_profit_lock_stop_rr`
- `adaptive_runner_trigger_rr`

Behavior:

- `adaptive_breakeven_rr`: progress threshold that can move the stop to breakeven.
- `adaptive_profit_lock_rr`: progress threshold that can lock some profit.
- `adaptive_profit_lock_stop_rr`: how much profit is locked once the previous threshold is met.
- `adaptive_runner_trigger_rr`: progress threshold that can activate runner behavior.
- Lower thresholds protect faster; higher thresholds give trades more room.

## Strategy-by-strategy reference

### `momentum_close`

Purpose: late-day continuation in strong small-cap movers.

Default windows:

- `entry_windows`: `[['13:45', '15:39']]`
- `management_windows`: `[['13:30', '15:54']]`
- `screener_windows`: `[['10:30', '11:20'], ['13:30', '15:25']]`

Strategy-specific knobs:

- `min_change_from_open`: minimum session-aware move from the active session open in whole percent units.
- `max_change_from_open`: maximum session-aware move from the active session open in whole percent units.
- `min_rvol`: minimum relative volume.
- `breakout_lookback_bars`: lookback used to define the breakout trigger.

Also uses these shared stock groups:

- force-flatten
- anti-chase / exhaustion
- anti-chase FVG retest defer logic
- stock FVG confluence
- adaptive stock trade management

Current package defaults:

| Option                                            | Current package default         |
|---------------------------------------------------|---------------------------------|
| `min_change_from_open`                            | `4.0`                           |
| `max_change_from_open`                            | `14.0`                          |
| `min_rvol`                                        | `2.4`                           |
| `breakout_lookback_bars`                          | `6`                             |
| `entry_exhaustion_filter_enabled`                 | `True`                          |
| `max_entry_vwap_extension_atr`                    | `0.85`                          |
| `max_entry_ema9_extension_atr`                    | `0.68`                          |
| `max_entry_bar_range_atr`                         | `1.55`                          |
| `max_entry_upper_wick_frac`                       | `0.27`                          |
| `max_entry_lower_wick_frac`                       | `0.27`                          |
| `entry_wick_close_position_guard`                 | `0.66`                          |
| `anti_chase_fvg_retest_enabled`                   | `True`                          |
| `anti_chase_fvg_retest_lookback_bars`             | `5`                             |
| `anti_chase_fvg_retest_max_gap_distance_pct`      | `0.0028`                        |
| `anti_chase_fvg_retest_max_opposing_distance_pct` | `0.002`                         |
| `anti_chase_fvg_retest_min_close_position`        | `0.64`                          |
| `anti_chase_fvg_retest_stop_buffer_gap_frac`      | `0.15`                          |
| `htf_fvg_entry_weight`                            | `0.46`                          |
| `one_minute_fvg_entry_weight`                     | `0.28`                          |
| `opposing_fvg_entry_penalty_mult`                 | `1.0`                           |
| `fvg_runner_rr_bonus`                             | `0.2`                           |
| `adaptive_breakeven_rr`                           | `0.88`                          |
| `adaptive_profit_lock_rr`                         | `1.22`                          |
| `adaptive_profit_lock_stop_rr`                    | `0.26`                          |
| `adaptive_runner_trigger_rr`                      | `1.15`                          |
| `force_flatten`                                   | `{'long': True, 'short': True}` |

### `rth_trend_pullback`

Purpose: full-session continuation strategy that looks for pullbacks holding support/resistance and then re-expanding.

Default windows:

- `entry_windows`: `[['09:38', '15:45']]`
- `management_windows`: `[['09:33', '15:58']]`
- `screener_windows`: `[['09:33', '15:45']]`

Strategy-specific knobs:

- `min_change_from_open` / `max_change_from_open`: whole-percent session-strength bounds using the canonical active-session move field.
- `min_rvol`: minimum relative volume.
- `min_bars`: bars required before evaluation.
- `support_lookback_bars`: recent bars used to define the pullback support/resistance zone.
- `trigger_lookback_bars`: bars used to define the local re-expansion trigger.
- `support_hold_pct`: tolerance used to decide whether the pullback held support or stayed capped by resistance.
- `max_extension_from_vwap_pct`: absolute VWAP-extension cap before entry.
- `min_bar_close_position`: minimum close-location quality for the trigger candle.
- `trend_min_ret5` / `trend_min_ret15`: short-horizon trend-strength thresholds.
- `target_rr`: initial reward-to-risk target before further refinement.
- `strong_trend_runner_enabled`: allow the strongest trend setups to aim farther.
- `strong_trend_target_rr`: target RR used for those strong-runner cases.

Also uses these shared stock groups:

- force-flatten
- anti-chase / exhaustion
- anti-chase FVG retest defer logic
- stock FVG confluence
- adaptive stock trade management

Current package defaults:

| Option                                            | Current package default         |
|---------------------------------------------------|---------------------------------|
| `min_change_from_open`                            | `1.8`                           |
| `max_change_from_open`                            | `22.0`                          |
| `min_rvol`                                        | `1.5`                           |
| `min_bars`                                        | `35`                            |
| `support_lookback_bars`                           | `10`                            |
| `trigger_lookback_bars`                           | `4`                             |
| `support_hold_pct`                                | `0.012`                         |
| `max_extension_from_vwap_pct`                     | `0.018`                         |
| `min_bar_close_position`                          | `0.6`                           |
| `trend_min_ret5`                                  | `0.0002`                        |
| `trend_min_ret15`                                 | `0.0004`                        |
| `target_rr`                                       | `2.0`                           |
| `entry_exhaustion_filter_enabled`                 | `True`                          |
| `max_entry_vwap_extension_atr`                    | `0.95`                          |
| `max_entry_ema9_extension_atr`                    | `0.78`                          |
| `max_entry_bar_range_atr`                         | `1.65`                          |
| `max_entry_upper_wick_frac`                       | `0.3`                           |
| `max_entry_lower_wick_frac`                       | `0.3`                           |
| `entry_wick_close_position_guard`                 | `0.62`                          |
| `anti_chase_fvg_retest_enabled`                   | `True`                          |
| `anti_chase_fvg_retest_lookback_bars`             | `5`                             |
| `anti_chase_fvg_retest_max_gap_distance_pct`      | `0.003`                         |
| `anti_chase_fvg_retest_max_opposing_distance_pct` | `0.0021`                        |
| `anti_chase_fvg_retest_min_close_position`        | `0.62`                          |
| `anti_chase_fvg_retest_stop_buffer_gap_frac`      | `0.15`                          |
| `strong_trend_runner_enabled`                     | `True`                          |
| `strong_trend_target_rr`                          | `2.35`                          |
| `htf_fvg_entry_weight`                            | `0.52`                          |
| `one_minute_fvg_entry_weight`                     | `0.32`                          |
| `opposing_fvg_entry_penalty_mult`                 | `1.0`                           |
| `fvg_runner_rr_bonus`                             | `0.24`                          |
| `adaptive_breakeven_rr`                           | `0.95`                          |
| `adaptive_profit_lock_rr`                         | `1.28`                          |
| `adaptive_profit_lock_stop_rr`                    | `0.28`                          |
| `adaptive_runner_trigger_rr`                      | `1.14`                          |
| `force_flatten`                                   | `{'long': True, 'short': True}` |

### `volatility_squeeze_breakout`

Purpose: trade liquid-stock volatility compression that resolves with a directional breakout and expansion.

Default windows:

- `entry_windows`: `[['09:48', '15:38']]`
- `management_windows`: `[['09:33', '15:58']]`
- `screener_windows`: `[['09:33', '15:38']]`

Strategy-specific knobs:

- `min_change_from_open` / `max_change_from_open`: whole-percent session-strength bounds used by the screener's canonical active-session move field.
- `min_rvol`: minimum relative volume.
- `min_bars`: bars required before evaluation.
- `squeeze_lookback_bars`: bars used to define the compression box.
- `squeeze_baseline_bars`: bars used to measure whether current volatility is compressed relative to recent history.
- `max_squeeze_range_pct`: maximum allowed box height as a percentage of price.
- `max_squeeze_range_atr`: maximum allowed box height in ATR units.
- `max_squeeze_width_pct`: maximum median Bollinger width percentage allowed inside the squeeze.
- `max_squeeze_width_ratio`: maximum squeeze-width ratio versus the baseline window.
- `breakout_buffer_pct`: extra breakout buffer above / below the squeeze box.
- `min_bar_close_position`: minimum close-location quality required on the trigger bar.
- `min_breakout_volume_ratio`: minimum current-bar volume ratio versus median squeeze-box volume.
- `min_atr_expansion_mult`: minimum ATR-expansion confirmation.
- `min_pressure_drift_pct`: minimum drift required in rising lows / falling highs inside the box.
- `require_vwap_alignment`: require price to break in the same direction as VWAP bias.
- `require_avwap_alignment`: require price to agree with anchored VWAP impulse context when available.
- `prefer_bollinger_squeeze_flag`: when enabled, prefer the built-in Bollinger squeeze flag to agree with the custom compression checks.
- `target_rr`: initial reward-to-risk target before refinements.
- `runner_enabled`: allow the strongest squeeze breakouts to use the farther target logic.
- `runner_target_rr`: target RR used for those runner cases.

Also uses these shared stock groups:

- force-flatten
- anti-chase / exhaustion
- anti-chase FVG retest defer logic
- stock FVG confluence
- adaptive stock trade management

Current package defaults:

| Option                                            | Current package default         |
|---------------------------------------------------|---------------------------------|
| `min_change_from_open`                            | `0.9`                           |
| `max_change_from_open`                            | `7.5`                           |
| `min_rvol`                                        | `1.35`                          |
| `min_bars`                                        | `60`                            |
| `squeeze_lookback_bars`                           | `16`                            |
| `squeeze_baseline_bars`                           | `22`                            |
| `max_squeeze_range_pct`                           | `0.011`                         |
| `max_squeeze_range_atr`                           | `2.2`                           |
| `max_squeeze_width_pct`                           | `0.05`                          |
| `max_squeeze_width_ratio`                         | `0.74`                          |
| `breakout_buffer_pct`                             | `0.0008`                        |
| `min_bar_close_position`                          | `0.63`                          |
| `min_breakout_volume_ratio`                       | `1.12`                          |
| `min_atr_expansion_mult`                          | `1.0`                           |
| `min_pressure_drift_pct`                          | `0.0011`                        |
| `require_vwap_alignment`                          | `True`                          |
| `require_avwap_alignment`                         | `True`                          |
| `prefer_bollinger_squeeze_flag`                   | `True`                          |
| `target_rr`                                       | `2.05`                          |
| `runner_enabled`                                  | `True`                          |
| `runner_target_rr`                                | `2.4`                           |
| `entry_exhaustion_filter_enabled`                 | `True`                          |
| `max_entry_vwap_extension_atr`                    | `0.88`                          |
| `max_entry_ema9_extension_atr`                    | `0.68`                          |
| `max_entry_bar_range_atr`                         | `1.42`                          |
| `max_entry_upper_wick_frac`                       | `0.25`                          |
| `max_entry_lower_wick_frac`                       | `0.25`                          |
| `entry_wick_close_position_guard`                 | `0.66`                          |
| `anti_chase_fvg_retest_enabled`                   | `True`                          |
| `anti_chase_fvg_retest_lookback_bars`             | `5`                             |
| `anti_chase_fvg_retest_max_gap_distance_pct`      | `0.0028`                        |
| `anti_chase_fvg_retest_max_opposing_distance_pct` | `0.0018`                        |
| `anti_chase_fvg_retest_min_close_position`        | `0.66`                          |
| `anti_chase_fvg_retest_stop_buffer_gap_frac`      | `0.14`                          |
| `htf_fvg_entry_weight`                            | `0.44`                          |
| `one_minute_fvg_entry_weight`                     | `0.24`                          |
| `opposing_fvg_entry_penalty_mult`                 | `1.0`                           |
| `fvg_runner_rr_bonus`                             | `0.16`                          |
| `adaptive_breakeven_rr`                           | `0.9`                           |
| `adaptive_profit_lock_rr`                         | `1.2`                           |
| `adaptive_profit_lock_stop_rr`                    | `0.26`                          |
| `adaptive_runner_trigger_rr`                      | `1.08`                          |
| `force_flatten`                                   | `{'long': True, 'short': True}` |

### `mean_reversion`

Purpose: buy pullbacks in strong names after bullish reversal evidence appears.

Default windows:

- `entry_windows`: `[['09:39', '10:55'], ['13:07', '14:45']]`
- `management_windows`: `[['09:34', '15:10']]`
- `screener_windows`: `[['09:39', '10:55'], ['13:07', '14:45']]`

Strategy-specific knobs:

- `min_day_strength` / `max_day_strength`: whole-percent session-strength bounds using the canonical active-session move field.
- `min_rvol`: minimum relative volume.
- `max_pullback_from_high`: max allowed pullback from the recent high.
- `min_reversal_close_position`: minimum candle close-position quality required for the reversal candle.
- `require_positive_reversal_ret5`: when `true`, require short-horizon return confirmation for the reversal.

Also uses these shared stock groups:

- force-flatten
- stock FVG confluence
- adaptive stock trade management

Current package defaults:

| Option                            | Current package default         |
|-----------------------------------|---------------------------------|
| `min_day_strength`                | `5.2`                           |
| `max_day_strength`                | `15.5`                          |
| `min_rvol`                        | `2.4`                           |
| `max_pullback_from_high`          | `0.027`                         |
| `min_reversal_close_position`     | `0.58`                          |
| `require_positive_reversal_ret5`  | `True`                          |
| `htf_fvg_entry_weight`            | `0.34`                          |
| `one_minute_fvg_entry_weight`     | `0.2`                           |
| `opposing_fvg_entry_penalty_mult` | `0.88`                          |
| `fvg_runner_rr_bonus`             | `0.12`                          |
| `adaptive_breakeven_rr`           | `0.72`                          |
| `adaptive_profit_lock_rr`         | `0.96`                          |
| `adaptive_profit_lock_stop_rr`    | `0.15`                          |
| `force_flatten`                   | `{'long': True, 'short': True}` |

### `closing_reversal`

Purpose: late-day rebound in strong names that have pulled back but are showing reversal quality.

Default windows:

- `entry_windows`: `[['15:33', '15:54']]`
- `management_windows`: `[['15:10', '15:57']]`
- `screener_windows`: `[['15:10', '15:52']]`

Strategy-specific knobs:

- `min_day_strength` / `max_day_strength`: whole-percent session-strength bounds using the canonical active-session move field.
- `min_rvol`: minimum relative volume.
- `max_pullback_from_high`: max allowed pullback from the recent high.
- `min_reversal_close_position`: reversal-candle close quality requirement.
- `require_positive_reversal_ret5`: require positive short-horizon confirmation when enabled.

Also uses these shared stock groups:

- force-flatten
- stock FVG confluence
- adaptive stock trade management

Current package defaults:

| Option                            | Current package default         |
|-----------------------------------|---------------------------------|
| `min_day_strength`                | `6.0`                           |
| `max_day_strength`                | `16.5`                          |
| `min_rvol`                        | `3.0`                           |
| `max_pullback_from_high`          | `0.05`                          |
| `min_reversal_close_position`     | `0.6`                           |
| `require_positive_reversal_ret5`  | `True`                          |
| `htf_fvg_entry_weight`            | `0.38`                          |
| `one_minute_fvg_entry_weight`     | `0.24`                          |
| `opposing_fvg_entry_penalty_mult` | `0.88`                          |
| `fvg_runner_rr_bonus`             | `0.12`                          |
| `adaptive_breakeven_rr`           | `0.72`                          |
| `adaptive_profit_lock_rr`         | `0.98`                          |
| `adaptive_profit_lock_stop_rr`    | `0.15`                          |
| `force_flatten`                   | `{'long': True, 'short': True}` |

### `pairs_residual`

Purpose: trade one side of a configured pair when the primary symbol diverges enough from its reference symbol.

Default windows:

- `entry_windows`: `[['10:10', '14:10']]`
- `management_windows`: `[['09:55', '15:20']]`
- `screener_windows`: `[['09:55', '14:10']]`

Strategy-specific knobs:

- `zscore_entry`: minimum residual z-score required to trigger.
- `max_zscore_entry`: do not chase if the residual is already too stretched.
- `lookback_bars`: rolling history window used in the residual/z-score calculation.
- `min_rvol`: minimum relative volume for the traded symbol.
- `min_day_strength`: minimum whole-percent session-strength threshold for the primary symbol.

Also uses these shared stock groups:

- force-flatten
- anti-chase / exhaustion
- stock FVG confluence
- adaptive stock trade management

Current package defaults:

| Option                            | Current package default         |
|-----------------------------------|---------------------------------|
| `zscore_entry`                    | `1.25`                          |
| `max_zscore_entry`                | `2.1`                           |
| `lookback_bars`                   | `90`                            |
| `min_rvol`                        | `1.8`                           |
| `min_day_strength`                | `3.0`                           |
| `entry_exhaustion_filter_enabled` | `True`                          |
| `max_entry_vwap_extension_atr`    | `0.95`                          |
| `max_entry_ema9_extension_atr`    | `0.82`                          |
| `max_entry_bar_range_atr`         | `1.65`                          |
| `max_entry_upper_wick_frac`       | `0.3`                           |
| `max_entry_lower_wick_frac`       | `0.3`                           |
| `entry_wick_close_position_guard` | `0.62`                          |
| `htf_fvg_entry_weight`            | `0.28`                          |
| `one_minute_fvg_entry_weight`     | `0.16`                          |
| `opposing_fvg_entry_penalty_mult` | `0.95`                          |
| `fvg_runner_rr_bonus`             | `0.1`                           |
| `adaptive_breakeven_rr`           | `0.86`                          |
| `adaptive_profit_lock_rr`         | `1.08`                          |
| `adaptive_profit_lock_stop_rr`    | `0.22`                          |
| `adaptive_runner_trigger_rr`      | `1.1`                           |
| `force_flatten`                   | `{'long': True, 'short': True}` |

### `opening_range_breakout`

Purpose: opening-range breakout in active small-cap names.

Default windows:

- `entry_windows`: `[['09:37', '10:05']]`
- `management_windows`: `[['09:30', '10:50']]`
- `screener_windows`: `[['08:10', '09:29']]`

Strategy-specific knobs:

- `orb_watchlist_mode`: valid values are `premarket`, `early_session`, or `none`.
  - `premarket`: build the watchlist before 09:30 ET and keep it frozen during the ORB window.
  - `early_session`: let watchlist logic continue into the open, if the screener windows also extend into RTH.
  - `none`: skip the watchlist-strength filter and just use the ORB entry logic.
- `watchlist_min_change`: watchlist strength threshold in whole percent units.
- `watchlist_min_volume`: watchlist volume threshold.
- `opening_range_minutes`: size of the opening range in minutes.
- `min_breakout_buffer_pct`: extra percentage buffer above/below the opening range before entry.

Also uses these shared stock groups:

- force-flatten
- anti-chase / exhaustion
- anti-chase FVG retest defer logic
- stock FVG confluence
- adaptive stock trade management

Current package defaults:

| Option                                            | Current package default         |
|---------------------------------------------------|---------------------------------|
| `orb_watchlist_mode`                              | `'premarket'`                   |
| `watchlist_min_change`                            | `5.5`                           |
| `watchlist_min_volume`                            | `800000`                        |
| `opening_range_minutes`                           | `5`                             |
| `min_breakout_buffer_pct`                         | `0.0011`                        |
| `entry_exhaustion_filter_enabled`                 | `True`                          |
| `max_entry_vwap_extension_atr`                    | `0.85`                          |
| `max_entry_ema9_extension_atr`                    | `0.68`                          |
| `max_entry_bar_range_atr`                         | `1.45`                          |
| `max_entry_upper_wick_frac`                       | `0.25`                          |
| `max_entry_lower_wick_frac`                       | `0.25`                          |
| `entry_wick_close_position_guard`                 | `0.68`                          |
| `anti_chase_fvg_retest_enabled`                   | `True`                          |
| `anti_chase_fvg_retest_lookback_bars`             | `4`                             |
| `anti_chase_fvg_retest_max_gap_distance_pct`      | `0.0028`                        |
| `anti_chase_fvg_retest_max_opposing_distance_pct` | `0.0019`                        |
| `anti_chase_fvg_retest_min_close_position`        | `0.66`                          |
| `anti_chase_fvg_retest_stop_buffer_gap_frac`      | `0.15`                          |
| `htf_fvg_entry_weight`                            | `0.46`                          |
| `one_minute_fvg_entry_weight`                     | `0.28`                          |
| `opposing_fvg_entry_penalty_mult`                 | `1.0`                           |
| `fvg_runner_rr_bonus`                             | `0.2`                           |
| `adaptive_breakeven_rr`                           | `0.86`                          |
| `adaptive_profit_lock_rr`                         | `1.18`                          |
| `adaptive_profit_lock_stop_rr`                    | `0.26`                          |
| `adaptive_runner_trigger_rr`                      | `1.1`                           |
| `force_flatten`                                   | `{'long': True, 'short': True}` |

### `peer_confirmed_trend_continuation`

A peer-confirmed continuation strategy that reuses the peer/macro confirmation model from `peer_confirmed_key_levels`, but replaces key-level touch entries with trend-aligned pullback and re-expansion triggers. It prefers symbols already trending with peers aligned, then enters on a controlled pullback that holds continuation structure and resolves back in the trend direction.

Purpose: join an existing intraday trend after a controlled pullback and a fresh continuation trigger, while peers and optional macro symbols still agree with the move.

Default windows:

- `entry_windows`: `[['07:10', '11:50'], ['12:55', '15:40']]`
- `management_windows`: `[['07:00', '15:58']]`
- `screener_windows`: `[['07:00', '15:40']]`

Strategy-specific knobs:

- Universe and HTF map:
  - `tradable`, `peers`
  - `htf_timeframe_minutes`, `htf_lookback_days`, `htf_refresh_seconds`, `htf_pivot_span`, `htf_max_levels_per_side`, `htf_atr_tolerance_mult`, `htf_pct_tolerance`, `htf_stop_buffer_atr_mult`, `htf_ema_fast_span`, `htf_ema_slow_span`
- Trigger frame and warmup:
  - `trigger_timeframe_minutes`, `min_bars`, `min_trigger_bars`
- Continuation scoring and pullback quality:
  - `min_total_score`, `min_trigger_score`, `min_adx14`
  - `min_pullback_bars`, `max_pullback_bars`, `max_pullback_depth_atr`, `pullback_hold_atr`, `max_countertrend_volume_ratio`
- Re-expansion trigger detail:
  - `breakout_buffer_pct`, `min_trigger_close_position`, `min_trigger_volume_ratio`
- Anti-chase / extension controls:
  - `max_extension_from_vwap_atr`, `max_extension_from_ema9_atr`
- Peer and macro confirmation:
  - `min_peer_agreement`, `min_peer_score`
  - `enable_macro_confirmation`, `require_macro_agreement_count`, `dollar_symbol`, `bond_symbol`, `volatility_symbol`
- R:R and adaptive management:
  - `min_rr`, `target_rr`, `runner_target_rr`, `stop_buffer_atr_mult`
  - `strong_setup_runner_enabled`, `adaptive_breakeven_rr`, `adaptive_profit_lock_rr`, `adaptive_profit_lock_stop_rr`, `adaptive_runner_trigger_rr`
- Context overlays:
  - `htf_fvg_entry_weight`, `one_minute_fvg_entry_weight`, `opposing_fvg_entry_penalty_mult`, `fvg_runner_rr_bonus`
  - `use_sr_veto` (disabled by default so the strategy does not hard-block on S/R proximity)

Also uses these shared stock groups:

- force-flatten
- stock FVG confluence
- adaptive stock trade management

Current package defaults:

| Option                            | Current package default                   |
|-----------------------------------|-------------------------------------------|
| `tradable`                        | `['AAPL', 'NVDA', 'GOOG', 'AMD', 'INTC']` |
| `peers`                           | `['QQQ', 'AVGO', 'MU', 'TSM']`            |
| `trigger_timeframe_minutes`       | `5`                                       |
| `min_bars`                        | `85`                                      |
| `min_trigger_bars`                | `18`                                      |
| `htf_timeframe_minutes`           | `60`                                      |
| `htf_lookback_days`               | `60`                                      |
| `htf_refresh_seconds`             | `120`                                     |
| `htf_pivot_span`                  | `2`                                       |
| `htf_max_levels_per_side`         | `6`                                       |
| `htf_atr_tolerance_mult`          | `0.35`                                    |
| `htf_pct_tolerance`               | `0.003`                                   |
| `htf_stop_buffer_atr_mult`        | `0.25`                                    |
| `htf_ema_fast_span`               | `34`                                      |
| `htf_ema_slow_span`               | `200`                                     |
| `min_peer_agreement`              | `2`                                       |
| `min_peer_score`                  | `2`                                       |
| `enable_macro_confirmation`       | `True`                                    |
| `require_macro_agreement_count`   | `1`                                       |
| `dollar_symbol`                   | `'NYICDX'`                                |
| `bond_symbol`                     | `'TLT'`                                   |
| `volatility_symbol`               | `'VIX'`                                   |
| `min_total_score`                 | `5.5`                                     |
| `min_trigger_score`               | `2.5`                                     |
| `min_adx14`                       | `13.5`                                    |
| `max_pullback_bars`               | `6`                                       |
| `min_pullback_bars`               | `2`                                       |
| `max_pullback_depth_atr`          | `1.05`                                    |
| `pullback_hold_atr`               | `0.38`                                    |
| `max_countertrend_volume_ratio`   | `1.28`                                    |
| `breakout_buffer_pct`             | `0.0007`                                  |
| `min_trigger_close_position`      | `0.58`                                    |
| `min_trigger_volume_ratio`        | `1.02`                                    |
| `max_extension_from_vwap_atr`     | `1.05`                                    |
| `max_extension_from_ema9_atr`     | `0.88`                                    |
| `min_rr`                          | `1.8`                                     |
| `target_rr`                       | `2.05`                                    |
| `runner_target_rr`                | `2.45`                                    |
| `stop_buffer_atr_mult`            | `0.5`                                     |
| `strong_setup_runner_enabled`     | `True`                                    |
| `adaptive_breakeven_rr`           | `0.92`                                    |
| `adaptive_profit_lock_rr`         | `1.2`                                     |
| `adaptive_profit_lock_stop_rr`    | `0.34`                                    |
| `adaptive_runner_trigger_rr`      | `1.12`                                    |
| `htf_fvg_entry_weight`            | `0.34`                                    |
| `one_minute_fvg_entry_weight`     | `0.16`                                    |
| `opposing_fvg_entry_penalty_mult` | `1.0`                                     |
| `fvg_runner_rr_bonus`             | `0.12`                                    |
| `use_sr_veto`                     | `False`                                   |
| `activity_score_weight`           | `0.12`                                    |
| `macro_bonus`                     | `0.7`                                     |
| `macro_miss_penalty`              | `0.3`                                     |
| `extension_penalty_per_atr`       | `0.72`                                    |
| `extension_hard_cap_mult`         | `1.45`                                    |
| `force_flatten`                   | `{'long': False, 'short': False}`         |

### `peer_confirmed_key_levels`

Purpose: trade around hourly key levels/zones only when a tradable symbol, its peer basket, and optional macro symbols agree strongly enough, then ride the cleaned S/R ladder while price action still defends the last reclaimed/broken rung.

Default windows:

- `entry_windows`: `[['07:10', '15:35']]`
- `management_windows`: `[['07:00', '15:58']]`
- `screener_windows`: `[['07:00', '15:35']]`

Strategy-specific knobs:

- Universe and HTF map:
  - `tradable`, `peers`
  - `htf_timeframe_minutes`, `htf_lookback_days`, `htf_refresh_seconds`, `htf_pivot_span`, `htf_max_levels_per_side`, `htf_atr_tolerance_mult`, `htf_pct_tolerance`, `htf_stop_buffer_atr_mult`, `htf_ema_fast_span`, `htf_ema_slow_span`
- Trigger frame and warmup:
  - `trigger_timeframe_minutes`, `min_bars`, `min_trigger_bars`
  - Stronger signals are prioritized lexicographically by trigger quality, level quality, peer confirmation, vote edge, and clearance before smaller additive bonuses are allowed to break ties.
- Zone, score, and R:R:
  - `zone_atr_mult`, `zone_pct`, `min_level_score`, `min_trigger_score`, `min_rr`, `stop_buffer_atr_mult`
  - `trigger_quality_bonus_enabled`, `trigger_quality_max_bonus`, `trigger_reclaim_quality_bonus_cap`, `trigger_zone_interaction_bonus_cap`, `trigger_candle_quality_bonus_cap`, `trigger_volume_quality_bonus_cap`, `trigger_range_expansion_bonus_cap`
- Peer confirmation:
  - `min_peer_agreement`, `min_peer_score`
- Macro confirmation:
  - `enable_macro_confirmation`, `require_macro_agreement_count`, `dollar_symbol`, `bond_symbol`, `volatility_symbol`
- Level scoring detail:
  - `level_round_number_tolerance_pct`
- Strong-setup runner / ladder logic:
  - `strong_setup_runner_enabled`, `strong_setup_min_trigger_score`, `strong_setup_min_level_score`, `strong_setup_min_peer_score`, `strong_setup_min_hourly_vote_edge`, `strong_setup_target_level_offset`
  - When `risk.trade_management_mode: adaptive_ladder` is active, this strategy stores rung metadata at entry and promotes targets one rung at a time while ratcheting stops behind defended S/R levels/zones. Non-ladder strategies safely fall back to adaptive management.

Also uses these shared stock groups:

- force-flatten
- stock FVG confluence
- adaptive stock trade management

Current package defaults:

| Option                               | Current package default                   |
|--------------------------------------|-------------------------------------------|
| `tradable`                           | `['AAPL', 'NVDA', 'GOOG', 'AMD', 'INTC']` |
| `peers`                              | `['QQQ', 'AVGO', 'MU', 'TSM']`            |
| `htf_timeframe_minutes`              | `60`                                      |
| `htf_lookback_days`                  | `60`                                      |
| `htf_refresh_seconds`                | `120`                                     |
| `htf_pivot_span`                     | `2`                                       |
| `htf_max_levels_per_side`            | `6`                                       |
| `htf_atr_tolerance_mult`             | `0.35`                                    |
| `htf_pct_tolerance`                  | `0.003`                                   |
| `htf_stop_buffer_atr_mult`           | `0.25`                                    |
| `htf_ema_fast_span`                  | `34`                                      |
| `htf_ema_slow_span`                  | `200`                                     |
| `trigger_timeframe_minutes`          | `5`                                       |
| `min_bars`                           | `80`                                      |
| `min_trigger_bars`                   | `18`                                      |
| `zone_atr_mult`                      | `0.22`                                    |
| `zone_pct`                           | `0.0016`                                  |
| `min_level_score`                    | `2.9`                                     |
| `min_trigger_score`                  | `2.5`                                     |
| `trigger_quality_bonus_enabled`      | `True`                                    |
| `trigger_quality_max_bonus`          | `2.0`                                     |
| `trigger_reclaim_quality_bonus_cap`  | `0.8`                                     |
| `trigger_zone_interaction_bonus_cap` | `0.5`                                     |
| `trigger_candle_quality_bonus_cap`   | `0.5`                                     |
| `trigger_volume_quality_bonus_cap`   | `0.4`                                     |
| `trigger_range_expansion_bonus_cap`  | `0.4`                                     |
| `min_rr`                             | `1.75`                                    |
| `stop_buffer_atr_mult`               | `0.68`                                    |
| `min_peer_agreement`                 | `2`                                       |
| `min_peer_score`                     | `2`                                       |
| `enable_macro_confirmation`          | `True`                                    |
| `require_macro_agreement_count`      | `1`                                       |
| `dollar_symbol`                      | `'NYICDX'`                                |
| `bond_symbol`                        | `'TLT'`                                   |
| `volatility_symbol`                  | `'VIX'`                                   |
| `level_round_number_tolerance_pct`   | `0.002`                                   |
| `strong_setup_runner_enabled`        | `True`                                    |
| `strong_setup_min_trigger_score`     | `3.2`                                     |
| `strong_setup_min_level_score`       | `3.4`                                     |
| `strong_setup_min_peer_score`        | `2`                                       |
| `strong_setup_min_hourly_vote_edge`  | `1`                                       |
| `strong_setup_target_level_offset`   | `1`                                       |
| `activity_score_weight`              | `0.11`                                    |
| `htf_fvg_entry_weight`               | `0.36`                                    |
| `one_minute_fvg_entry_weight`        | `0.2`                                     |
| `opposing_fvg_entry_penalty_mult`    | `1.0`                                     |
| `fvg_runner_rr_bonus`                | `0.14`                                    |
| `adaptive_breakeven_rr`              | `0.9`                                     |
| `adaptive_profit_lock_rr`            | `1.25`                                    |
| `adaptive_profit_lock_stop_rr`       | `0.32`                                    |
| `adaptive_runner_trigger_rr`         | `1.12`                                    |
| `force_flatten`                      | `{'long': False, 'short': False}`         |

### `peer_confirmed_key_levels_1m`

Purpose: 1-minute peer-confirmed hourly key-level/zone strategy variant that keeps the same HTF map and peer/macro confirmation framework as `peer_confirmed_key_levels`, but is now tuned as a compromise between aggressive and balanced confirmation so entries can form earlier without using the older looser gates.

Default windows:

- `entry_windows`: `[['07:05', '15:40']]`
- `management_windows`: `[['07:00', '15:58']]`
- `screener_windows`: `[['07:00', '15:40']]`

Key differences vs the 5-minute base strategy:

- `trigger_timeframe_minutes: 1`
- deeper trigger warmup with `min_bars: 90` and `min_trigger_bars: 45`
- compromise 1-minute gates that are still faster than the 5-minute base but no longer use the older aggressive thresholds: `min_level_score: 2.5`, `min_rr: 1.6`, `min_peer_agreement: 2`, `min_peer_score: 2`
- tighter trigger-zone sizing and slightly faster adaptive management to suit 1-minute execution while keeping confirmation more balanced
- heavier weighting on one-minute FVG participation while still retaining the hourly map and macro confirmation checks
- inherits the base strategy's capped trigger-quality bonus layer so clean 1-minute reclaims / rejects can outrank weaker touches without raising `min_trigger_score`

Use `configs/config.peer_confirmed_key_levels_1m.yaml` for the shipped full preset.

Current package defaults:

| Option                               | Current package default                   |
|--------------------------------------|-------------------------------------------|
| `tradable`                           | `['AAPL', 'NVDA', 'GOOG', 'AMD', 'INTC']` |
| `peers`                              | `['QQQ', 'AVGO', 'MU', 'TSM']`            |
| `htf_timeframe_minutes`              | `60`                                      |
| `htf_lookback_days`                  | `60`                                      |
| `htf_refresh_seconds`                | `120`                                     |
| `htf_pivot_span`                     | `2`                                       |
| `htf_max_levels_per_side`            | `6`                                       |
| `htf_atr_tolerance_mult`             | `0.35`                                    |
| `htf_pct_tolerance`                  | `0.003`                                   |
| `htf_stop_buffer_atr_mult`           | `0.25`                                    |
| `htf_ema_fast_span`                  | `34`                                      |
| `htf_ema_slow_span`                  | `200`                                     |
| `trigger_timeframe_minutes`          | `1`                                       |
| `min_bars`                           | `100`                                     |
| `min_trigger_bars`                   | `55`                                      |
| `zone_atr_mult`                      | `0.17`                                    |
| `zone_pct`                           | `0.0013`                                  |
| `min_level_score`                    | `2.5`                                     |
| `min_trigger_score`                  | `2.4`                                     |
| `trigger_quality_bonus_enabled`      | `True`                                    |
| `trigger_quality_max_bonus`          | `2.0`                                     |
| `trigger_reclaim_quality_bonus_cap`  | `0.8`                                     |
| `trigger_zone_interaction_bonus_cap` | `0.5`                                     |
| `trigger_candle_quality_bonus_cap`   | `0.5`                                     |
| `trigger_volume_quality_bonus_cap`   | `0.4`                                     |
| `trigger_range_expansion_bonus_cap`  | `0.4`                                     |
| `min_rr`                             | `1.6`                                     |
| `stop_buffer_atr_mult`               | `0.56`                                    |
| `min_peer_agreement`                 | `2`                                       |
| `min_peer_score`                     | `2`                                       |
| `enable_macro_confirmation`          | `True`                                    |
| `require_macro_agreement_count`      | `1`                                       |
| `dollar_symbol`                      | `'NYICDX'`                                |
| `bond_symbol`                        | `'TLT'`                                   |
| `volatility_symbol`                  | `'VIX'`                                   |
| `level_round_number_tolerance_pct`   | `0.002`                                   |
| `strong_setup_runner_enabled`        | `True`                                    |
| `strong_setup_min_trigger_score`     | `3.0`                                     |
| `strong_setup_min_level_score`       | `3.0`                                     |
| `strong_setup_min_peer_score`        | `2`                                       |
| `strong_setup_min_hourly_vote_edge`  | `1`                                       |
| `strong_setup_target_level_offset`   | `1`                                       |
| `activity_score_weight`              | `0.12`                                    |
| `htf_fvg_entry_weight`               | `0.32`                                    |
| `one_minute_fvg_entry_weight`        | `0.26`                                    |
| `opposing_fvg_entry_penalty_mult`    | `1.0`                                     |
| `fvg_runner_rr_bonus`                | `0.14`                                    |
| `adaptive_breakeven_rr`              | `0.82`                                    |
| `adaptive_profit_lock_rr`            | `1.08`                                    |
| `adaptive_profit_lock_stop_rr`       | `0.28`                                    |
| `adaptive_runner_trigger_rr`         | `1.02`                                    |
| `force_flatten`                      | `{'long': False, 'short': False}`         |

### `peer_confirmed_htf_pivots`

Purpose: trade around higher-timeframe support/resistance pivot battlegrounds instead of generic hourly key-level votes. The strategy can enter in reclaim, rejection, or continuation mode, but it is now explicitly tuned as an S/R scalp strategy that prefers longs around support, shorts around resistance, and uses the next opposing S/R level as the first target reference.

Default windows:

- `entry_windows`: `[['09:35', '11:15'], ['13:00', '14:45']]`
- `management_windows`: `[['09:01', '15:55']]`
- `screener_windows`: `[['09:01', '11:25'], ['12:45', '15:55']]`

Strategy-specific knobs:

- Universe and HTF pivot map:
  - `tradable`, `peers`
  - `htf_timeframe_minutes`, `htf_lookback_days`, `htf_refresh_seconds`, `htf_pivot_span`, `htf_max_levels_per_side`, `htf_atr_tolerance_mult`, `htf_pct_tolerance`, `htf_stop_buffer_atr_mult`, `htf_ema_fast_span`, `htf_ema_slow_span`
- Trigger frame and warmup:
  - `trigger_timeframe_minutes`, `min_bars`, `min_trigger_bars`
- Entry-family selection:
  - `entry_family` with `auto`, `pivot_reclaim`, `pivot_rejection`, and `pivot_continuation`
- Regime / trigger scoring:
  - `min_regime_score`, `min_trigger_score`, `min_total_score`, `min_peer_agreement`, `min_peer_score`
  - `enable_macro_confirmation`, `require_macro_agreement_count`, `dollar_symbol`, `bond_symbol`, `volatility_symbol`
- Pivot-zone sizing and family detail:
  - `pivot_zone_atr_mult`, `pivot_zone_pct`
  - `pivot_reclaim_buffer_pct`, `pivot_reclaim_zone_frac`
  - `pivot_rejection_min_wick_frac`, `pivot_rejection_allows_neutral_ltf_structure`
  - `pivot_continuation_breakout_buffer_pct`, `pivot_continuation_interaction_lookback_bars`, `pivot_continuation_max_distance_atr`
- Trigger quality and anti-chase:
  - `min_trigger_close_position`, `min_trigger_volume_ratio`, `min_adx14`
  - `max_reclaim_distance_from_pivot_atr`, `max_rejection_distance_from_pivot_atr`, `max_continuation_distance_from_pivot_atr`
  - `entry_exhaustion_filter_enabled`, `max_entry_vwap_extension_atr`, `max_entry_ema9_extension_atr`, `max_entry_bar_range_atr`, `max_entry_upper_wick_frac`, `max_entry_lower_wick_frac`
  - `use_sr_veto` (disabled by default so the strategy stays anchored to the HTF pivot model rather than generic S/R vetoes)
- R:R and adaptive management:
  - `min_rr`, `target_rr`, `runner_target_rr`, `stop_buffer_atr_mult`
  - `strong_setup_runner_enabled`, `adaptive_breakeven_rr`, `adaptive_profit_lock_rr`, `adaptive_profit_lock_stop_rr`, `adaptive_runner_trigger_rr`
- Context overlays:
  - `htf_fvg_entry_weight`, `one_minute_fvg_entry_weight`, `opposing_fvg_entry_penalty_mult`, `fvg_runner_rr_bonus`

Also uses these shared stock groups:

- force-flatten
- stock FVG confluence
- adaptive stock trade management

Current package defaults:

| Option                                         | Current package default                         |
|------------------------------------------------|-------------------------------------------------|
| `tradable`                                     | `['AAPL', 'NVDA', 'GOOG', 'AMD', 'INTC', 'MU']` |
| `peers`                                        | `['QQQ', 'AVGO', 'TSM']`                        |
| `trigger_timeframe_minutes`                    | `5`                                             |
| `min_bars`                                     | `90`                                            |
| `min_trigger_bars`                             | `20`                                            |
| `htf_timeframe_minutes`                        | `60`                                            |
| `htf_lookback_days`                            | `60`                                            |
| `htf_refresh_seconds`                          | `120`                                           |
| `htf_pivot_span`                               | `2`                                             |
| `htf_max_levels_per_side`                      | `6`                                             |
| `htf_atr_tolerance_mult`                       | `0.35`                                          |
| `htf_pct_tolerance`                            | `0.003`                                         |
| `htf_stop_buffer_atr_mult`                     | `0.25`                                          |
| `htf_ema_fast_span`                            | `34`                                            |
| `htf_ema_slow_span`                            | `200`                                           |
| `entry_family`                                 | `'auto'`                                        |
| `min_regime_score`                             | `4`                                             |
| `min_trigger_score`                            | `2.5`                                           |
| `min_total_score`                              | `5`                                             |
| `min_peer_agreement`                           | `2`                                             |
| `min_peer_score`                               | `2`                                             |
| `enable_macro_confirmation`                    | `True`                                          |
| `require_macro_agreement_count`                | `1`                                             |
| `use_sr_veto`                                  | `False`                                         |
| `dollar_symbol`                                | `'NYICDX'`                                      |
| `bond_symbol`                                  | `'TLT'`                                         |
| `volatility_symbol`                            | `'VIX'`                                         |
| `pivot_zone_atr_mult`                          | `0.24`                                          |
| `pivot_zone_pct`                               | `0.0018`                                        |
| `pivot_reclaim_buffer_pct`                     | `0.00045`                                       |
| `pivot_reclaim_zone_frac`                      | `0.09`                                          |
| `pivot_rejection_min_wick_frac`                | `0.24`                                          |
| `pivot_rejection_allows_neutral_ltf_structure` | `True`                                          |
| `pivot_continuation_breakout_buffer_pct`       | `0.0009`                                        |
| `pivot_continuation_interaction_lookback_bars` | `9`                                             |
| `pivot_continuation_max_distance_atr`          | `1.45`                                          |
| `min_trigger_close_position`                   | `0.6`                                           |
| `min_trigger_volume_ratio`                     | `1.0`                                           |
| `min_adx14`                                    | `12.5`                                          |
| `max_reclaim_distance_from_pivot_atr`          | `0.9`                                           |
| `max_rejection_distance_from_pivot_atr`        | `0.82`                                          |
| `max_continuation_distance_from_pivot_atr`     | `1.35`                                          |
| `entry_exhaustion_filter_enabled`              | `True`                                          |
| `max_entry_vwap_extension_atr`                 | `1.05`                                          |
| `max_entry_ema9_extension_atr`                 | `0.85`                                          |
| `max_entry_bar_range_atr`                      | `1.65`                                          |
| `max_entry_upper_wick_frac`                    | `0.3`                                           |
| `max_entry_lower_wick_frac`                    | `0.3`                                           |
| `min_rr`                                       | `1.65`                                          |
| `target_rr`                                    | `1.95`                                          |
| `runner_target_rr`                             | `2.45`                                          |
| `stop_buffer_atr_mult`                         | `0.5`                                           |
| `strong_setup_runner_enabled`                  | `True`                                          |
| `adaptive_breakeven_rr`                        | `0.9`                                           |
| `adaptive_profit_lock_rr`                      | `1.18`                                          |
| `adaptive_profit_lock_stop_rr`                 | `0.32`                                          |
| `adaptive_runner_trigger_rr`                   | `1.1`                                           |
| `htf_fvg_entry_weight`                         | `0.28`                                          |
| `one_minute_fvg_entry_weight`                  | `0.14`                                          |
| `opposing_fvg_entry_penalty_mult`              | `1.0`                                           |
| `fvg_runner_rr_bonus`                          | `0.1`                                           |
| `activity_score_weight`                        | `0.1`                                           |
| `macro_bonus`                                  | `0.75`                                          |
| `macro_miss_penalty`                           | `0.28`                                          |
| `force_flatten`                                | `{'long': True, 'short': True}`                 |

### `top_tier_adaptive`

Purpose: multi-regime adaptive strategy for a fixed universe of 15 top-tier liquid stocks across Technology (AAPL, MSFT, NVDA, INTC, AMD), Consumer Discretionary (AMZN, TSLA, HD, LOW, UBER), and Communication Services (GOOG, META, NFLX, RBLX, TMUS).

Default windows:

- `entry_windows`: `[["09:35", "15:00"]]`
- `management_windows`: `[["09:30", "15:55"]]`
- `screener_windows`: `[["09:30", "15:00"]]`

Strategy-specific knobs:

- `tradable`: the fixed list of symbols to trade.
- `index_symbols`: index ETFs for directional confirmation (default SPY, QQQ).
- `require_index_confirmation`: gate trend/pullback entries on index agreement.
- `min_trend_score` / `min_pullback_score` / `min_range_score`: minimum regime score to qualify.
- `min_pullback_trend_score`: minimum trend score required before pullback scoring begins.
- `min_score_gap`: minimum gap between the winning and runner-up regime.
- `trend_target_rr` / `pullback_target_rr` / `range_target_rr`: initial R:R targets per regime.
- `orb_end_time` / `midday_start_time` / `midday_end_time` / `afternoon_start_time` / `no_new_entries_after`: time-of-day regime window boundaries.
- `sector_groups`: GICS sector groupings for the concentration guard.
- `max_same_sector_same_direction`: max same-direction positions per sector.

Also uses these shared stock groups:

- force-flatten (configurable per side)
- entry exhaustion filters
- stock FVG confluence
- adaptive stock trade management
- chart pattern entry/exit gates
- market structure entry/exit gates
- S/R entry/exit gates and level refinement

Current code defaults:

| Option                            | Default                                                                                |
|-----------------------------------|----------------------------------------------------------------------------------------|
| `tradable`                        | `AAPL, MSFT, NVDA, INTC, AMD, AMZN, TSLA, HD, LOW, UBER, GOOG, META, NFLX, RBLX, TMUS` |
| `index_symbols`                   | `SPY, QQQ`                                                                             |
| `require_index_confirmation`      | `true`                                                                                 |
| `min_bars`                        | `60`                                                                                   |
| `trigger_timeframe_minutes`       | `5`                                                                                    |
| `htf_timeframe_minutes`           | `15`                                                                                   |
| `min_trigger_bars`                | `15`                                                                                   |
| `min_trend_score`                 | `3.5`                                                                                  |
| `min_pullback_score`              | `3.5`                                                                                  |
| `min_pullback_trend_score`        | `3.0`                                                                                  |
| `min_range_score`                 | `3.5`                                                                                  |
| `min_score_gap`                   | `1.2`                                                                                  |
| `min_adx14`                       | `15.0`                                                                                 |
| `trend_target_rr`                 | `2.0`                                                                                  |
| `pullback_target_rr`              | `2.0`                                                                                  |
| `range_target_rr`                 | `1.5`                                                                                  |
| `stop_buffer_atr_mult`            | `0.25`                                                                                 |
| `orb_end_time`                    | `10:05`                                                                                |
| `midday_start_time`               | `11:30`                                                                                |
| `midday_end_time`                 | `13:00`                                                                                |
| `afternoon_start_time`            | `13:00`                                                                                |
| `no_new_entries_after`            | `15:00`                                                                                |
| `max_same_sector_same_direction`  | `2`                                                                                    |
| `adaptive_breakeven_rr`           | `1.00`                                                                                 |
| `adaptive_profit_lock_rr`         | `1.30`                                                                                 |
| `adaptive_runner_trigger_rr`      | `1.15`                                                                                 |
| `force_flatten`                   | `{'long': true, 'short': true}`                                                        |

### `zero_dte_etf_options`

Purpose: 0DTE ETF strategy that can trade debit spreads and, when enabled, midday credit spreads.

Default windows:

- `entry_windows`: `[['09:40', '14:20']]`
- `management_windows`: `[['09:30', '15:15']]`
- `screener_windows`: `[['09:35', '14:20']]`

Special behavior:

- `options.styles` decides which spread styles are allowed:
  - `orb_debit_spread`
  - `trend_debit_spread`
  - `midday_credit_spread`
- `credit_start_time` / `credit_end_time` are used only by this spread strategy.

Current package defaults:

| Option                        | Current package default |
|-------------------------------|-------------------------|
| `orb_end_time`                | `'10:05'`               |
| `trend_start_time`            | `'10:05'`               |
| `trend_end_time`              | `'13:40'`               |
| `credit_start_time`           | `'11:10'`               |
| `credit_end_time`             | `'13:40'`               |
| `no_new_entries_after`        | `'14:15'`               |
| `min_bars`                    | `40`                    |
| `min_confirm_bars`            | `28`                    |
| `trend_vwap_lookback`         | `10`                    |
| `flip_lookback`               | `14`                    |
| `range_lookback`              | `25`                    |
| `min_candidate_rvol`          | `1.18`                  |
| `trend_rvol`                  | `1.3`                   |
| `credit_min_rvol`             | `0.95`                  |
| `credit_max_rvol`             | `1.65`                  |
| `trend_vwap_distance_pct`     | `0.0015`                |
| `trend_ema_gap_pct`           | `0.0007`                |
| `trend_above_vwap_frac`       | `0.76`                  |
| `trend_min_ret5`              | `0.0009`                |
| `trend_min_ret15`             | `0.0015`                |
| `range_vwap_distance_pct`     | `0.0017`                |
| `range_ema_gap_pct`           | `0.0007`                |
| `range_max_intraday_move_pct` | `0.0085`                |
| `credit_max_day_move_pct`     | `0.0075`                |
| `credit_max_vix_change_pct`   | `0.009`                 |
| `chop_flip_min`               | `4`                     |
| `chop_flip_max_for_trend`     | `3`                     |
| `chaos_intraday_range_pct`    | `0.015`                 |
| `min_trend_score`             | `4.9`                   |
| `min_range_score`             | `4.65`                  |
| `min_score_gap`               | `1.6`                   |
| `orb_breakout_buffer_pct`     | `0.0008`                |
| `require_index_confirmation`  | `True`                  |
| `candle_weight`               | `0.5`                   |
| `candle_sr_weight`            | `0.35`                  |
| `candle_trend_follow_weight`  | `0.25`                  |
| `candle_range_penalty`        | `0.3`                   |
| `candle_mixed_penalty`        | `0.18`                  |
| `use_htf_trend_confirmation`  | `True`                  |
| `require_htf_alignment`       | `True`                  |
| `htf_timeframe_minutes`       | `15`                    |
| `htf_lookback_days`           | `15`                    |
| `htf_refresh_seconds`         | `300`                   |
| `htf_min_bars`                | `20`                    |
| `htf_vwap_distance_pct`       | `0.0009`                |
| `htf_ema_gap_pct`             | `0.0007`                |
| `htf_min_ret3`                | `0.0009`                |
| `htf_range_vwap_distance_pct` | `0.002`                 |
| `htf_range_ema_gap_pct`       | `0.001`                 |
| `htf_score_bonus`             | `0.65`                  |
| `htf_score_penalty`           | `0.65`                  |
| `fvg_context_weight_scale`    | `0.9`                   |

### `zero_dte_etf_long_options`

Purpose: 0DTE ETF strategy that buys a single long call or long put and never sells naked premium or opens a spread.

Default windows:

- `entry_windows`: `[['09:40', '14:10']]`
- `management_windows`: `[['09:30', '15:20']]`
- `screener_windows`: `[['09:35', '14:10']]`

Long-option-only parameters:

- `options.styles` can selectively enable `orb_long_option` and `trend_long_option`.
- `long_option_min_trend_score`: stricter trend-score threshold before buying the option.
- `long_option_min_score_gap`: stricter score-gap requirement before buying the option.
- `long_option_max_vwap_extension_pct`: max extension from VWAP before the long option is considered too late.
- `long_option_max_ema_gap_pct`: max EMA gap before the long option is considered too extended.
- `long_option_max_ret5` / `long_option_max_ret15`: short-horizon spike filters that reduce chase entries.

Current package defaults:

| Option                               | Current package default |
|--------------------------------------|-------------------------|
| `orb_end_time`                       | `'10:05'`               |
| `trend_start_time`                   | `'10:05'`               |
| `trend_end_time`                     | `'13:30'`               |
| `no_new_entries_after`               | `'13:45'`               |
| `min_bars`                           | `90`                    |
| `min_confirm_bars`                   | `30`                    |
| `trend_vwap_lookback`                | `10`                    |
| `flip_lookback`                      | `14`                    |
| `range_lookback`                     | `25`                    |
| `min_candidate_rvol`                 | `1.15`                  |
| `trend_rvol`                         | `1.25`                  |
| `credit_min_rvol`                    | `0.88`                  |
| `credit_max_rvol`                    | `1.9`                   |
| `trend_vwap_distance_pct`            | `0.0014`                |
| `trend_ema_gap_pct`                  | `0.0007`                |
| `trend_above_vwap_frac`              | `0.74`                  |
| `trend_min_ret5`                     | `0.0006`                |
| `trend_min_ret15`                    | `0.0013`                |
| `range_vwap_distance_pct`            | `0.0018`                |
| `range_ema_gap_pct`                  | `0.0007`                |
| `range_max_intraday_move_pct`        | `0.009`                 |
| `credit_max_day_move_pct`            | `0.008`                 |
| `credit_max_vix_change_pct`          | `0.01`                  |
| `chop_flip_min`                      | `4`                     |
| `chop_flip_max_for_trend`            | `3`                     |
| `chaos_intraday_range_pct`           | `0.016`                 |
| `min_trend_score`                    | `5.1`                   |
| `min_range_score`                    | `4.6`                   |
| `min_score_gap`                      | `1.6`                   |
| `long_option_min_trend_score`        | `5.0`                   |
| `long_option_min_score_gap`          | `1.5`                   |
| `long_option_max_vwap_extension_pct` | `0.0026`                |
| `long_option_max_ema_gap_pct`        | `0.0015`                |
| `long_option_max_ret5`               | `0.002`                 |
| `long_option_max_ret15`              | `0.0044`                |
| `orb_breakout_buffer_pct`            | `0.0008`                |
| `require_index_confirmation`         | `True`                  |
| `candle_weight`                      | `0.5`                   |
| `candle_sr_weight`                   | `0.35`                  |
| `candle_trend_follow_weight`         | `0.25`                  |
| `candle_range_penalty`               | `0.3`                   |
| `candle_mixed_penalty`               | `0.18`                  |
| `use_htf_trend_confirmation`         | `True`                  |
| `require_htf_alignment`              | `True`                  |
| `htf_timeframe_minutes`              | `15`                    |
| `htf_lookback_days`                  | `60`                    |
| `htf_refresh_seconds`                | `180`                   |
| `htf_min_bars`                       | `20`                    |
| `htf_vwap_distance_pct`              | `0.0009`                |
| `htf_ema_gap_pct`                    | `0.0007`                |
| `htf_min_ret3`                       | `0.0009`                |
| `htf_range_vwap_distance_pct`        | `0.002`                 |
| `htf_range_ema_gap_pct`              | `0.001`                 |
| `htf_score_bonus`                    | `0.65`                  |
| `htf_score_penalty`                  | `0.65`                  |
| `fvg_context_weight_scale`           | `0.9`                   |

## `pairs` block

Used only by `pairs_residual`.

Each row supports:

- `symbol`: primary tradable symbol
- `reference`: comparison symbol used to compute residual divergence
- `side_preference`: `long`, `short`, or `both`
- `sector`: optional label only
- `industry`: optional label only

Changing `side_preference` restricts the directions that pair may trade without changing the rest of the strategy logic.

The shipped `configs/config.pairs_residual.yaml` preset now includes two editable example pairs so the strategy is immediately runnable. Replace those examples with the pairs you actually want to trade.

## Which top-level blocks matter to which strategies

- All strategies use: `strategy`, `schwab`, `risk`, `runtime`, `paper`, `dashboard`, and `execution`.
- Stock strategies also use: `tradingview`, `candles`, `chart_patterns`, `support_resistance`, `technical_levels`, `shared_entry`, `shared_exit`, and their own `strategies.<name>` params from the selected top-level config file.
- `pairs_residual` also uses: `pairs`.
- Option strategies also use: `options`, `support_resistance`, `shared_entry`, and their own `strategies.<name>` params from the selected top-level config file.

## Practical tuning notes

- `risk.trade_management_mode: adaptive_ladder` is now the safe default recommendation. Ladder-aware strategies opt in via metadata; strategies without ladder metadata automatically behave like normal adaptive management.
- If you want fewer chase entries, tighten the anti-chase thresholds before tightening the whole strategy universe.
- If you want more trade frequency, loosen the screener/liquidity filters before loosening stop logic.
- If you want stronger FVG influence, raise the strategy's FVG weights instead of turning more global filters on.
- If you want the dashboard lighter, turn off the heavier chart overlays before reducing `max_bars`.


## Strategy-specific top-level presets

Prebuilt top-level presets are included under `configs/config.<strategy>.yaml` for every strategy. Each preset is intended to be the complete runtime source of truth for that strategy, while manifests remain the built-in fallback defaults.

Shipped preset files:

| Preset                                                  | Strategy                            |
|---------------------------------------------------------|-------------------------------------|
| `configs/config.momentum_close.yaml`                    | `momentum_close`                    |
| `configs/config.mean_reversion.yaml`                    | `mean_reversion`                    |
| `configs/config.closing_reversal.yaml`                  | `closing_reversal`                  |
| `configs/config.rth_trend_pullback.yaml`                | `rth_trend_pullback`                |
| `configs/config.volatility_squeeze_breakout.yaml`       | `volatility_squeeze_breakout`       |
| `configs/config.pairs_residual.yaml`                    | `pairs_residual`                    |
| `configs/config.opening_range_breakout.yaml`            | `opening_range_breakout`            |
| `configs/config.microcap_gap_orb.yaml`                  | `microcap_gap_orb`                  |
| `configs/config.peer_confirmed_key_levels.yaml`         | `peer_confirmed_key_levels`         |
| `configs/config.peer_confirmed_key_levels_1m.yaml`      | `peer_confirmed_key_levels_1m`      |
| `configs/config.peer_confirmed_trend_continuation.yaml` | `peer_confirmed_trend_continuation` |
| `configs/config.peer_confirmed_htf_pivots.yaml`         | `peer_confirmed_htf_pivots`         |
| `configs/config.top_tier_adaptive.yaml`                 | `top_tier_adaptive`                 |
| `configs/config.zero_dte_etf_options.yaml`              | `zero_dte_etf_options`              |
| `configs/config.zero_dte_etf_long_options.yaml`         | `zero_dte_etf_long_options`         |

Plus two non-strategy files:

- `configs/config.example.yaml` — canonical full-config template, used as the scaffold base by `scripts/scaffold_strategy_plugin.py`.
- `configs/config.yaml` — the default file `main.py` loads when `--config` is omitted.

To run a shipped preset:

```bash
python main.py --config configs/config.<strategy>.yaml --strategy <strategy>
```

For per-strategy parameter tuning see [Strategy-by-strategy reference](#strategy-by-strategy-reference). For preset-directory conventions see [`configs/README_PRESETS.md`](configs/README_PRESETS.md).
