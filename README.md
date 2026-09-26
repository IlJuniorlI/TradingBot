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
- `peer_confirmed_key_levels` — peer-confirmed HTF key-level/zone strategy with 5-minute LTF triggers, optional macro confirmation, and ladder-aware post-entry management when `adaptive_ladder` is enabled
- `peer_confirmed_key_levels_1m` — faster 1-minute LTF peer-confirmed HTF key-level/zone variant tuned as a compromise between aggressive and balanced confirmation
- `peer_confirmed_trend_continuation` — peer-confirmed trend continuation strategy that trades controlled pullbacks and re-expansion without waiting for key-level touches
- `peer_confirmed_htf_pivots` — peer-confirmed higher-timeframe pivot S/R scalp strategy with switchable reclaim, rejection, and continuation entry families
- `top_tier_adaptive` — multi-regime adaptive strategy for 25 mega-cap Tech / AI stocks with peer-breadth index confirmation and a correlation concentration guard; seven core regimes (trend, pullback, range, vol_squeeze, momentum, sr_scalp, vwap_reclaim) compete in a flat score-ordered build queue, plus a dedicated `orb` opening-range-breakout regime in the opening window
- `small_cap_squeeze` — long-only multi-regime small-cap "squeeze" strategy; a dynamic TradingView screener (low float 400K-20M, premarket gap ≥5% on heavy volume, RVOL ≥2) replaces the fixed list. Reuses the `top_tier_adaptive` engine (trend / momentum / vwap_reclaim only since the 2026-06-02 narrowing; ORB, sr_scalp, pullback, range and vol_squeeze off), 1-minute LTF, premarket/extended-hours eligible

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

`requirements.txt` pins `TA-Lib==0.8.0`, which backs the standard indicator and candlestick-pattern layer. Its PyPI wheels (cp39–cp314 for Windows, macOS 13+ Intel and 14+ Apple Silicon, and glibc and musl Linux on x86_64 and aarch64) bundle the TA-Lib C library, so `pip install` needs no separate native install. Only a platform with no wheel builds from source, and that build needs TA-Lib C 0.8.1 installed first.

Tests are maintained in a private source tree (not shipped with this repository).

Change `--strategy` or the top-level `strategy:` key to switch strategies.

Runtime config precedence is now intentionally simple:

- manifest/code defaults
- the selected top-level YAML file (for example `configs/config.peer_confirmed_htf_pivots.yaml`)
- CLI strategy override via `--strategy`


For runtime plugin behavior, the engine now prefers strategy hooks and manifest capabilities over hard-coded strategy-name branches. The most common extension points are `dashboard_tradable_symbols()`, `restore_eligible_symbols()`, `requires_hybrid_startup_restore_metadata()`, `dashboard_candidate_limit()`, `dashboard_allow_generic_level_fallback()`, `dashboard_level_context_spec()`, `dashboard_candidate_label()`, `dashboard_candidate_sources()`, `active_watchlist(...)`, and `quote_watchlist(...)`. New plugins can now often declare those behaviors in `manifest.json` under `capabilities`, including watchlist/quote universe rules and dashboard level-context overrides, and only fall back to Python hooks when they need something more custom.

Entries and exits are not extension points (2026-09-24). Every strategy builds its entries through the shared entry stage (`self.entry_policy.admit` / `emit`, see [`shared_entry`](#shared_entry)), and every position's exits are decided by the shared exit policy (see [`shared_exit`](#shared_exit)). A strategy adds exits of its own only through `strategy_exit_signal(...)`, and declares its ranking in the manifest (`capabilities.signal_priority`). Defining `position_exit_signal`, `shared_exit_signal`, `strategy_logic_default` or `signal_priority_key` raises `TypeError` at import. The author contract is in [`intraday_tv_schwab_bot/_strategies/README.md`](intraday_tv_schwab_bot/_strategies/README.md).

The standardized plugin runtime contract now uses `Candidate.activity_score`, `Candidate.directional_bias`, and signal metadata fields such as `final_priority_score`, `selection_quality_score`, `activity_score`, `setup_quality_score`, and `execution_quality_score`. Since 2026-09-24 `emit` computes `final_priority_score` itself, as `strategy_priority_score` (the strategy's own score) + `shared_context_score` (the shared terms), and stamps `entry_style_family` and `regime`.

`manifest.json` now supports `schema_version: 1`, and the loader rejects unsupported top-level keys early so malformed plugins fail closer to the source of the mistake. New plugins are fully self-contained: the scaffold emits plain-string `strategy_name` values, and no central strategy-name helper or `models.py` edit is required.

A scaffold helper is included for new plugins. It clones `configs/config.example.yaml` as a full runnable preset, swaps in the new strategy name, and writes `configs/config.<strategy>.yaml`. The example's `shared_entry` / `shared_exit` sections, `risk.time_stop_minutes` and `support_resistance.entry_proximity_scoring_enabled` are `peer_confirmed_htf_pivots` parity, so the scaffold writes the config dataclass defaults for those instead (2026-09-24). The generated `strategy.py` already goes through the shared entry stage:

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

All configured times (entry, management and screener windows, blackouts, HH:MM knobs) are New York (ET) wall-clock times; the bot trades US sessions only and has no timezone setting.

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
- `zero_dte_etf_options`
- `zero_dte_etf_long_options`
- `peer_confirmed_key_levels`
- `peer_confirmed_key_levels_1m`
- `peer_confirmed_trend_continuation`
- `peer_confirmed_htf_pivots`
- `top_tier_adaptive`
- `small_cap_squeeze`

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
| `peak_giveback_low_tier_enabled`  | `true`            |
| `peak_giveback_low_tier_min_r`    | `0.7`             |
| `peak_giveback_low_tier_giveback_frac` | `0.7`        |
| `peak_giveback_retain_1to2r`      | `0.65`            |
| `peak_giveback_retain_2to3r`      | `0.72`            |
| `peak_giveback_retain_3r_plus`    | `0.78`            |
| `entry_slippage_allowance_spread_frac` | `1.0`        |
| `entry_slippage_allowance_max_pct` | `0.002`          |
| `risk_overage_warn_frac`          | `0.15`            |
| `entry_slippage_warn_pct`         | `0.0015`          |
| `daily_loss_includes_open_risk`   | `true`            |

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
- `same_level_block_minutes`: after an exit (win or loss), block a **same-direction** re-entry on the same symbol for this many minutes when it would re-try the level the closed trade already tried (see the next bullet). Targets the breakout-chase pattern where the bot enters → stops → re-enters at the same level → stops again. Set to `0` to disable. Independent of `cooldown_minutes`; both blocks apply. It reads the signal's `entry_price`, which the shared entry stage stamps on every price-level signal since 2026-09-24. `rth_trend_pullback` and `volatility_squeeze_breakout` never stamped it before, so the block never reached them; their presets ship `0` to keep that (parity), and turning it on is a go-live decision. `zero_dte_etf_options` and `zero_dte_etf_long_options` ship `0` too (parity, 2026-09-25): until then the block measured an option by its premium and never fired on one, so turning it on there is also a go-live decision.
- `same_level_block_atr_mult`: the same-level block only fires when the new entry sits within `same_level_block_atr_mult × ATR` (the symbol's ATR at the prior exit) of the prior trade's ENTRY price, the level already tried. Lower values block a tighter band around that level; higher values widen it. Fib-pullback entries (where the new entry sits in the [0.5, 0.786] retracement zone of a tagged anchor) override the block. An option signal is measured on its underlying (2026-09-25): the direction is its market direction (`metadata['direction']`, bullish / bearish), never its order side, and the level is the underlying's price at entry (`underlying_entry`), against the underlying's ATR. So a flip between two credit spreads (both sold) is not blocked, and the same bullish bet through a credit spread and then a debit is. Until then the block compared premiums with the underlying's ATR, which only an exact premium match could trip, and matched the order side. The fib-pullback override never applies to an option signal (2026-09-24): its fib anchors are the underlying's swing, and its side is the order side.
- `time_stop_minutes`: scratch-exit a trade held this long with `|return_pct| < time_stop_min_return_pct`. Targets dead-capital trades that aren't moving. An option position's return is its underlying's since entry (`underlying_entry`), the instrument the exit logic reads. Set to `0` to disable. It is the shared exit policy's `time_stop` family, so since 2026-09-24 it reaches every strategy. The peer strategies' old exit override never ran it, and their presets ship `0` to keep that.
- `time_stop_min_return_pct`: the absolute return threshold under which the time-stop fires (e.g. `0.003` = 0.3%). Trades outside this band aren't time-stopped regardless of duration.
- `peak_giveback_enabled`: when `true`, fires `peak_giveback:peakXR_floorYR` once `max_favorable_r` crosses `peak_giveback_min_r` and `current_r` retraces past a tiered floor set by the `peak_giveback_retain_*` fractions below. Complements the protective-BE logic at +0.5R: BE catches 0.5–1R winners, peak-giveback catches 1R+ runners that give back too much.
- `peak_giveback_min_r`: minimum peak R-multiple before the peak-giveback floor activates. Set to `0` together with `peak_giveback_enabled: false` to disable entirely.
- `peak_giveback_retain_1to2r` / `peak_giveback_retain_2to3r` / `peak_giveback_retain_3r_plus`: the fraction of the peak R the main tier keeps before its floor fires, by peak size (1-2R, 2-3R, 3R+). At a 2R peak, `0.65` exits on a retrace to 1.3R. Higher captures more and exits sooner on a retrace; lower leaves more recovery room. Tightened from the original 0.50/0.60/0.70 after the 2026-05-12..27 sample showed winners keeping only 44% of their MFE.
- `peak_giveback_low_tier_enabled` / `peak_giveback_low_tier_min_r` / `peak_giveback_low_tier_giveback_frac`: a second, lower tier for trades that peak between `peak_giveback_low_tier_min_r` and `peak_giveback_min_r` and would otherwise round-trip to the breakeven stop. It arms at the low-tier peak and exits once `current_r` falls below `peak × (1 − giveback_frac)` — a 0.7R peak with the default `0.7` exits at 0.21R. Skipped while the high-conviction override is active, since those trades want the main tier's wider leash.
- `entry_slippage_allowance_spread_frac` / `entry_slippage_allowance_max_pct`: sizing pads the stop distance by an expected-slippage amount — the live spread times `entry_slippage_allowance_spread_frac`, capped at `entry_slippage_allowance_max_pct` of price — so a fill that slips by up to that much still lands inside the risk budget. Without it, realized risk is `qty × |fill − stop|` against a budget sized on the signal price, and the overage scales inversely with stop width (5c of slip is ~2% over on a 1% stop, ~25% over on a stop 10x tighter). Set the spread fraction to `0.0` to size on the raw stop distance.
- `risk_overage_warn_frac`: after a fill, realized risk more than this fraction over budget is logged and stamped on the position. Detection only — the shares are already bought; the sizing allowance above is the preventive half.
- `entry_slippage_warn_pct`: entry slippage beyond this fraction of the signal price is logged and flagged on the position, so a routing or liquidity problem shows up in the log rather than only in an end-of-day report.
- `daily_loss_includes_open_risk`: `max_daily_loss` gates new entries, and open positions are never flattened when it trips. When `true`, `can_open` subtracts the open positions' remaining risk to their CURRENT stop from realized P&L before comparing, so entries stop once the worst case would breach the limit rather than once realized losses already have. A stop trailed to breakeven or better contributes zero. `false` restores the realized-only comparison, under which a day can finish at roughly twice the limit.

### `runtime`

This block controls loop timing, quote/history refresh cadence, stream fallback behavior, startup reconciliation, and log/state paths.

| Option                               | Code default                              |
|--------------------------------------|-------------------------------------------|
| `loop_sleep_seconds`                 | `2.0`                                     |
| `error_escalation_cycles`            | `10`                                      |
| `history_poll_seconds`               | `300`                                     |
| `quote_poll_seconds`                 | `6`                                       |
| `quote_cache_seconds`                | `6`                                       |
| `quote_batch_size`                   | `20`                                      |
| `history_lookback_minutes`           | `390`                                     |
| `use_extended_hours_history`         | `true`                                    |
| `use_rth_session_indicators`         | `true`                                    |
| `equity_session_indicator_window`    | `rth`                                     |
| `warmup_minutes`                     | `90`                                      |
| `prewarm_before_windows_minutes`     | `5`                                       |
| `log_dir`                            | `.logs`                                   |
| `stream_fields`                      | `[0, 1, 2, 3, 4, 5, 6, 7, 8]`             |
| `stream_connect_timeout_seconds`     | `20`                                      |
| `stream_fallback_poll_seconds`       | `25`                                      |
| `stream_stale_fallback_seconds`      | `180`                                     |
| `stream_health_log_seconds`          | `90`                                      |
| `reconcile_on_startup`               | `true`                                    |
| `startup_reconcile_mode`             | `block`                                   |
| `startup_order_lookback_days`        | `2`                                       |
| `startup_reconcile_ignore_symbols`   | `[]`                                      |
| `startup_reconcile_metadata_db_path` | `.logs/startup_reconcile_metadata.sqlite` |
| `auto_exit_after_session`            | `false`                                   |
| `idle_sleep_seconds`                 | `60.0`                                    |
| `symbol_state_prune_seconds`         | `1800.0`                                  |
| `session_reconcile_on_resume`        | `true`                                    |
| `cycle_precompute_workers`           | `4`                                       |
| `max_consecutive_quote_failures`     | `5`                                       |
| `export_session_archive`             | `true`                                    |

Behavior and valid values:

- `loop_sleep_seconds`: base engine sleep between iterations.
- `error_escalation_cycles`: the main loop backs off exponentially on errors and never gives up. After this many consecutive failed cycles it escalates to a CRITICAL log naming the open positions, and the dashboard status turns into an explicit alarm, so a sustained outage during the management window cannot pass as a throttled warning. At the capped 60s backoff, `10` is roughly ten minutes without management. `0` disables the escalation; the backoff is unaffected.
- `history_poll_seconds`: cadence for history refreshes.
- `quote_poll_seconds`: cadence for quote refreshes when polling is used.
- `quote_cache_seconds`: max age of cached quotes before forcing a refresh.
- `quote_batch_size`: max symbols grouped into one quote request.
- `history_lookback_minutes`: intraday history depth retained for signal generation.
- `use_extended_hours_history`: include premarket/after-hours minute bars in warmup and backfill.
  - **Overnight ECN data lag (Schwab API).** Even with `use_extended_hours_history: true`, Schwab's `price_history` minute endpoint does **not** include bars for the most recent weekday overnight (~8:00 PM ET → 7:00 AM ET) at any frequency (verified at `frequency=1`, `5`, `15`, and `30`). Those overnight ECN bars become available with roughly a 1-trading-day lag — older overnights (e.g. `Wed 8 PM → Thu 7 AM`) do return continuous bars at all frequencies once they've aged. The Sunday → Monday transition is an exception: weekend ECN bars are released without the lag and appear immediately under Sunday's evening startDate.
  - **Visual consequence on the dashboard chart.** The LTF chart's window covers ~30 trading hours at most (`tail(360 × ltf_minutes)`); the most recent overnight gap dominates the visible range until Schwab fills it in. Only the LTF chart shows it: the LTF frame holds whatever the fetch returned, so older overnights appear once their lag has resolved. The HTF chart shows no overnight bars at all, by design: since 2026-09-23 the stored HTF frame (the one S/R, HTF levels, HTF FVGs and the HTF chart are built from) keeps only bars starting 07:00-20:00 ET (`equity_stream_window_bars` in `_refresh_htf_frame`), and the live 1m buckets `dashboard_htf_chart_frame` appends are windowed the same way, so no overnight bar, lagged or not, reaches a level or the HTF chart.
  - **No fix planned.** Fetching directly at coarser minute frequencies (`5`/`15`/`30`) doesn't help — they all share the same lag for the most recent overnight. Extending `history_lookback_minutes` past 24h would eventually pick up overnight bars on heal fetches once Schwab releases them, but at the cost of larger fetch payloads on every heal. Current design keeps `history_lookback_minutes: 780` (13h) and accepts that the freshest overnight is invisible on the LTF chart.
- `use_rth_session_indicators`: on session bars, VWAP/EMA reset at each session's open and the TA-Lib columns (ATR, DI/ADX, RSI, OBV, Bollinger) are one session-only series stitched across every session in the frame (overnight gaps removed, so there is no warm-up step from the open); premarket and postmarket bars keep the all-session values, and the returns (`ret1/5/15`) are all-session everywhere. S/R, HTF, FVG and order-block ATR read the latest session bar while the clock is in the session, so the 09:15 premarket 15m bar does not set them between 09:30 and 09:44.
- `equity_session_indicator_window`: which bars count as session bars for `use_rth_session_indicators` (the VWAP/EMA reset and the session TA-Lib series). `rth` (default) is 09:30-16:00; `extended` is the 07:00-20:00 equity stream window, for strategies that enter pre/post market. Leave `rth` for every RTH-only strategy.
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
  - A reconcile that cannot read the broker fails closed (since 2026-09-25). A read that fails, including a JSON error body such as the 401 a lapsed token returns, is never read as empty. An unread account settles, cancels, restores and prunes nothing, and every ignore-list symbol stays blocked until its own recheck reads the account. An unread working-order list still lets the settle run, except that a position with a resting broker bracket that the broker holds fewer shares of, and that keeps some, is held until a retry reads the list (its re-protect needs the list to see a stop still resting for it); in bracket mode the restore waits for the list (restoring without it placed a second stop beside the one still resting), and without bracket mode the restore runs so the engine manages what the broker holds. A working exit or foreign order whose state cannot be read fails the attempt too (a dry run reads no order state, so there a saved position's unread exit simply does not match). In `block`, `restore_basic` and `restore_hybrid` a failed attempt blocks entries (`startup_reconcile_failed`; an unread foreign order keeps `working_orders_present`). The engine retries on trading days inside the 07:00-20:00 ET stream window, 60 seconds after the failed attempt ends, doubling to at most 5 minutes, and the first attempt that reads the broker clears the block. Until a reconcile has succeeded, a metadata save writes the tracked positions over their stored rows and deletes none, so a retry still restores the rest from them; the first success replaces them all. Before, one failed read blocked entries until the next ET day or a restart.
  - The settle (the session-boundary re-run, and every retry) books what the broker no longer holds (since 2026-09-25):
    - A position with a resting broker bracket has it cancelled first. What its children filled, such as a stop that triggered after the last management cycle, is booked as the bracket's exit at the broker's price with the risk manager's registration, and only the rest of the gap is booked `closed_outside_bot` at the last mark, estimated. A cancel that cannot be confirmed leaves the position as it was, with nothing placed beside the bracket, and holds it: the manager sends nothing for it (no exit, no re-protect sized to shares that are gone), and the retry sends the cancel again. The first retry after a hold comes after 10 seconds whatever failed before it, doubling only while the hold lasts, and a hold is lifted only by an attempt that can read the position again. When the cancel reports fills, the account and then the working exit order are read again once the bracket is down, since a child, or the working exit, can fill between the first reads and the cancel. What is left is re-protected, adopting a stop still resting for it (one moved in the app) rather than stacking on it.
    - The estimated loss of a close outside the bot counts toward `max_daily_loss`; an estimated gain does not, since the mark can be stale.
    - A position whose entry order is still settling is left to the entry gatekeeper: the reconcile books unsettled entry orders from their own fill records first, restores nothing and settles nothing against an order still settling, and fails the attempt until it has.
    - The session-boundary reconcile runs before the cycle, so the first premarket cycle no longer manages positions closed overnight.
  - In a dry run, broker protection is simulated: a restore never adopts or resizes a real resting order, and the engine owns the stop and target exits. The simulated bracket keeps the real order's ids, so that order is not counted as foreign. A saved row that tracks a working exit order restores basic in a dry run, which can never settle that order.
  - Bracket mode: a stop that died at the broker without filling (a DAY order that EXPIRED at its session's end, one CANCELED in the app, a REJECTED one) retires its bracket: the rest of it is cancelled, what that cancel reports filled is booked, and the position is re-protected once per bracket, or the engine owns the stop. A REJECTED stop is never placed again (the broker would reject it again), and a cancel that cannot be confirmed leaves the rest of the bracket tracked while the engine owns the stop; once the rest is confirmed down, the dead stop gets its one fresh placement. A child the 8-hour order listing no longer returns is read on its own, at most once a minute while it is live, so a stop placed before 07:00 that fills after 15:00 is booked. A working exit order that closes a position takes its leftover bracket down.
- `startup_reconcile_mode`: valid values are `ignore`, `block`, `log_only`, `restore_basic`, `restore_hybrid`.
  - `ignore`: skip startup reconciliation entirely.
  - `block`: block new entries if broker positions / working orders are found.
  - `log_only`: log findings but do not block.
  - `restore_basic`: restore broker stock positions into bot memory without metadata help.
  - `restore_hybrid`: restore broker stock positions and also consult the metadata SQLite store for richer restore state.
  - A position restored while its exit order is still working at the broker (a slice or a full exit left working through the restart; 2026-09-25): that order is the position's own, not a foreign working order, so it does not block entries, and the first management cycle books its fills from the order's own fill record. A `restore_hybrid` match accepts a quantity gap those fills explain and restores at the saved quantity, keeping the order, its one-shot marker and the engine levels. The session-boundary re-run nets them out instead of booking them as `closed_outside_bot`, and an order whose state cannot be read leaves the position tracked. In bracket mode the resting stop covers only the shares outside a working slice, and none is placed beside a working full exit; an exit order that died while the bot was down (cancelled in the app, a DAY order that expired) covers only its unbooked fills, so every share still held gets the stop at once (an order whose state cannot be read counts as live). A snapshot order this reconcile itself retired (a child the restore resized, or the bracket the session-boundary settle cancelled) is not counted as foreign: its state comes from `account_orders` (an 8-hour lookback) or, for an order entered before that, `order_details`, and an order whose state cannot be read still counts (fail closed). The resting stop a restore adopts is read the same way (since 2026-09-25) and resized to the shares held; one whose state cannot be read is still adopted, never stacked on, and re-issued at the held size. Until then a stop older than the listing was adopted at whatever it rested, so `restore_basic` could leave a 10-share stop against 7 held shares. `restore_basic` cannot see a working exit (it has no saved metadata, and the working-order snapshot carries no quantities): the order stays foreign and blocks entries until it is cleared, and in bracket mode the resting stop covers the full broker quantity beside it.
- `startup_order_lookback_days`: broker order lookup window used during startup reconciliation.
- `startup_reconcile_ignore_symbols`: symbol list ignored during startup reconcile. Ignored open symbols are still blocked from new entries.
- `startup_reconcile_metadata_db_path`: SQLite metadata path used by hybrid restore.
- `auto_exit_after_session`: when `true`, the bot shuts down cleanly after the trading session ends and all positions are closed. Exits after the latest of RTH close and any configured strategy window end. On non-trading days (weekends/holidays), exits immediately. Designed for use with Windows Task Scheduler or cron to start the bot daily.
- `idle_sleep_seconds`: outside the 7am–8pm ET equity stream window (when neither streaming nor order acceptance is available), the loop sleeps this long between iterations instead of `loop_sleep_seconds`. Cuts overnight CPU waste by ~95% on always-on bots — the loop wakes every minute by default to recheck whether streaming has resumed instead of every 2s. Set to a value `<= loop_sleep_seconds` to disable the optimization entirely.
- `symbol_state_prune_seconds`: cadence at which the engine evicts per-symbol state (history frames, HTF/SR caches, dashboard snapshot/chart payloads) for symbols that have dropped out of the active set (streamed symbols + last watchlist + open positions). Long-running multi-day bots otherwise accumulate history dicts (~240KB per 1m frame at default lookback) for every symbol the screener has ever returned. Set to `0` to disable pruning entirely.
- `session_reconcile_on_resume`: when `true`, the engine re-runs the startup reconcile at the first cycle on each new ET trading day where streaming is back online (i.e., the first cycle past 7am ET). Catches positions that closed overnight via the Schwab app or broker-side stops — without this, an always-on bot would wake at 7am still believing those positions are open and try to manage phantoms. Honors the same `reconcile_on_startup` and `startup_reconcile_mode` knobs as the startup reconcile (no separate mode). Set to `false` to disable if you handle reconciliation externally or only run single-day sessions. A failed reconcile is retried either way; this knob turns off only the new-day re-run.
- `cycle_precompute_workers`: thread-pool size used to precompute per-symbol indicator/structure context in parallel each engine cycle. Higher values reduce per-cycle latency on wide watchlists at the cost of CPU; lower values trade latency for less contention.
- Orders whose outcome is unsettled: a live order that neither filled nor confirmed its cancel -- a MARKET exit left working through a halt, a cancel Schwab never acknowledged, a partial fill whose remainder may still be live -- is tracked (`working_exit_order` on the position for exits, `EntryGatekeeper.unsettled_entry_orders` for entries) and settled every management cycle from the order's own fills (`account_orders`, falling back to `order_details`). Nothing else is sent for that position or symbol while the order may still be live: fills are booked as they land (exits), adopted as a position or folded into the one a partial already opened (entries), and a live limit is re-cancelled so a fresh one can follow. This replaced a recovery that read broker *positions* through a snapshot cached once per cycle, which after the cycle's first order was already stale.
- `max_consecutive_quote_failures`: per-symbol quote-fetch failure threshold. After a symbol fails this many consecutive quote refreshes (typically symbol-specific Schwab 401/403/404 such as restricted-security responses), it is silenced from quote refresh for the rest of the session. The counter resets on any successful fetch; the blacklist clears on bot restart. Set to `0` to disable (always retry — pre-2026-04-29 behavior). The default `5` catches symbol-specific permission errors without triggering on transient hiccups. Other endpoints (history, stream) for the same symbol are unaffected.
- `export_session_archive`: when `true`, the engine writes a per-day archive to `{log_dir}/sessions/{YYYY-MM-DD}/` containing, for every active watchlist symbol (plus any symbol traded or held today), `bars/1m/{SYMBOL}.csv` (the full merged 1m frame with indicators: warmup history, pre-market, RTH and post-market, not filtered to today's RTH), `bars/{N}m/{SYMBOL}.csv` resamples of it for the strategy's `ltf_minutes` / `htf_minutes` when above 1, and `bars/htf_{N}m/{SYMBOL}.csv` (the stored HTF frame that S/R, HTF levels, HTF structure and HTF FVGs are built from), plus `trades.csv` filtered to the day, `decisions.csv`, `events.jsonl`, `config_snapshot.yaml`, `account_snapshot.json`, the day's log and `manifest.json` with strategy + summary stats. The archive fires automatically once per ET trading day after the stream window closes (8 PM ET), so an always-on bot produces one archive per session without waiting for shutdown; shutdown still writes its own (potentially overwriting today's bundle with a fresher snapshot). Useful for trade audits and post-session analysis. Disable to save disk space if running without dashboard/analysis needs.

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
  - **Post-stop continuation** — for trades that exited on `stop`, how far price then ran the trade's way inside a 30-minute window, in R (`ran >=1R after the stop: 4/4`, `post-stop R avg=1.54 median=1.6`). Every other table scores a trade as it was CLOSED; this is the only one that separates "the stop was right" from "we were right and got shaken out". The window matches `same_level_block_minutes`, so the figure reads as the cost of the re-entry lockout. `opportunity_usd` is an UPPER BOUND — it assumes re-entry at the stop and an exit at the window's best tick. A trade with no usable initial risk, or no bars, counts as unevaluated rather than as zero continuation
  - **Per entry path** — the same columns as Per regime, bucketed by HOW the entry was reached: `retest` (the armed retest fired), `market_fallback` (the wait expired and it entered at market — the pre-2026-09-20 behaviour, and therefore the control group) and `immediate` (a regime that does not arm). This is the A/B for the armed retest: without it a week produces one blended number that reads identically whether good fallback entries carried poor retest entries or the reverse

  - **Entry timing** — for every trade, the deepest retrace in the 15 minutes AFTER the fill, as a fraction of that trade's own entry-to-stop distance (`median=0.85R edge=+0.64R | reached 1/4 stop 82%, 1/2 stop 57%, full stop 29%`), split by regime **and by entry path** — a retest entry should show a shallower post-fill retrace than a market fallback on the same regime, since the retrace already happened before the fill; if they come out equal the armed retest is not doing what it was built to do, whatever the PnL says. 0.5 means the retrace covered half the way to the stop; 1.0 means it reached the stop. Answers whether the bot is entering before the natural pullback rather than on it — `trend` / `momentum` / `vol_squeeze` can only fill at an N-bar extreme by construction, while `pullback` refuses to fire until price has given back 25-50% of the leg, so the two should separate if entries are being chased.

    The **baseline is sampled from the same session's bars**, using each trade's own risk distance as the denominator, and it is the whole measurement: price dips below any given price most of the time, so a raw "a better entry existed" count is noise. Only the gap above baseline is evidence. A regime with fewer than `min_regime_samples` (5) trades stays in `by_regime` but is marked `low_sample` and sorted last.

    **NOT a backtest**: it says a better price existed, not that the bot could have got it — no fill model, and no claim the setup would still have been valid down there. A trade with no usable initial risk, no entry price or no bars counts as unevaluated rather than as zero retrace
- **Structured JSON**: `SESSION_REPORT {...}` at TRADEFLOW level. Carries the same aggregates as first-class keys: `per_regime`, `per_entry_path`, `per_symbol`, `per_exit_reason`, `per_partial_exit_reason` (the scale-out and partial-fill slices by their own reason; a folded trade reports only its final exit), `per_hour`, `mae_mfe`, `post_stop_continuation`, `entry_timing`, `filter_rejections`. Any downstream tool (dashboard, spreadsheet import, analyzer) can consume it directly without scraping the human log.
- **Gate attribution** (`manifest.json` only, not the log): for every SKIPPED decision, the net forward price move over 30 minutes toward the side the bot was about to take, in ATR, bucketed by skip reason and split by regime. The side is the one the reason names (`short_build_failed_...` is a blocked SHORT); the candidate's screener bias (`side_pref`) stands in only for reasons that name no side. Each entry carries `blocked`, `median_net_atr`, `median_edge_atr` (the same move less its side's baseline), `favourable_pct` and the regimes that produced it; `costliest_gates` ranks by blocks x `median_edge_atr`.

  The **baseline is sampled from the same session's bars, over the span the decisions cover**, and is what makes the numbers readable — a trending day lifts every LONG-side figure, so only the gap above baseline is evidence. It is reported from the LONG viewpoint (`median_net_atr`, `up_pct`) with its mirror as `short_median_net_atr`, and each gate is compared with its own side's. (Until 2026-09-23 every gate was compared with the LONG baseline, drawn from the whole archived frame including the prior session and extended hours; SHORT gates read wrong by twice the day's drift.) The excursion fields beside the net move are near chance at this window (77% of random 30-minute windows touch 1 ATR up, because a 1m ATR-14 measures fourteen minutes of range) and are kept only because a wide favourable excursion against a flat net says "it went our way and came back" — a stop-placement story rather than an entry one.

  **NOT a backtest**: price movement only, with no stop, target, sizing or slippage. `favourable_pct` is not a win rate. Gates with fewer than `min_samples` (20) observations stay in `by_reason` but are excluded from the ranking, since a reason seen once has a "median" of that single observation.
- **Regime-call outcomes** (`manifest.json` only): directional regime calls, classified against the 30-minute forward move as `right` (moved at least 1 ATR its way, and further that way than the other), `wrong`, `flat` or `unclear`, bucketed by hour, regime and side, with the same-span baseline beside it for context. A call is an `ambiguous_regime` top regime (0DTE options) or a regime that QUALIFIED on a side — its build failing on a later gate, or the signal it built entering or being blocked by an engine gate (top_tier_adaptive and its subclass). Every reason on a decision row is read, not only its primary one; a build reason that names no regime (`no_fresh_breakout`, `htf_zones_too_close`, ...) counts only when it is the row's first build reason, whose regime is the row's `family`. A later one cannot be attributed to a regime and is not counted (~11% of build reasons on 2026-09-23). Read `by_side` against the baseline: on a trend day most calls on one side are "right" whatever the regimes did.

- **Persistent CSV**: `.logs/trades.csv` — one row per closed trade, appended across sessions. A trade's partial exits are folded into its row (quantity and P&L summed, prices quantity-weighted, the final exit's reason, the earlier slices' reasons oldest first in `partial_exit_reasons`, `|`-joined), so `partial_exit` is always false here and the session P&L, `trades.csv` and `manifest.json` agree with the account. Columns: `date, symbol, strategy, side, qty, entry_price, exit_price, entry_time, exit_time, realized_pnl, return_pct, hold_minutes, reason, asset_type, partial_exit, fill_price_estimated, broker_recovered, regime, initial_risk_per_unit, max_favorable_pnl, max_adverse_pnl, entry_slippage_pct, ..., partial_exit_reasons` (the full list is `TradeRecord`'s fields). `max_favorable_pnl` / `max_adverse_pnl` are the trade's excursion in dollars at its full size (`initial_qty`), tracked per unit, so a scale-out does not shrink them.

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
| `bracket_orders_enabled`          | `false`      |
| `bracket_sync_mode`               | `static`     |
| `bracket_legs`                    | `stop_and_target` |
| `bracket_stop_order_type`         | `STOP_LIMIT` |
| `bracket_stop_limit_offset_r`     | `0.5`        |
| `bracket_require_normal_session`  | `true`       |
| `bracket_replace_min_price_delta` | `0.01`       |

Behavior and valid values:

- `entry_limit_min_buffer` / `entry_limit_max_buffer`: lower and upper limit-price offsets used for marketable-limit stock entries.
- `entry_limit_spread_frac`: spread fraction used when converting the current quote into a limit price.
- `entry_live_fill_timeout_seconds`: how long to wait for an equity entry fill before cancel/reprice logic can kick in.
- `entry_live_poll_seconds`: polling interval while waiting on an equity entry.
- `entry_live_reprice_attempts`: number of live reprice attempts before giving up.
- `entry_live_reprice_step_frac`: size of each reprice step as a fraction of the entry buffer.
- `extended_hours_enabled`: allow equity orders outside regular hours when the broker permits it.
- `market_exit_regular_hours`: when `true`, stock exits during regular hours can use market orders.
- `bracket_orders_enabled`: submit equity entries as a broker-side bracket. See below.
- `bracket_sync_mode`: `static` | `replace` — who owns the resting levels after entry.
- `bracket_legs`: `stop_and_target` | `stop_only` — which children rest at the broker.
- `bracket_stop_order_type`: `STOP` | `STOP_LIMIT` — the resting protective stop's order type.
- `bracket_stop_limit_offset_r`: `STOP_LIMIT` only; limit offset beyond the trigger, in units of initial R.
- `bracket_require_normal_session`: reject a bracketed entry outside regular hours rather than send it unprotected.
- `bracket_replace_min_price_delta`: `replace` mode debounce, in dollars.

#### Broker-side bracket orders

By default every exit is **engine-side**: the bot holds the stop and target in
memory and acts on them when its management poll observes the level. That poll
runs at `runtime.quote_poll_seconds`, so on a fast move the protective exit can
lag the price by up to one poll interval — exactly when it costs most.

With `bracket_orders_enabled: true`, an equity entry instead goes out as a
single Schwab **first-triggers-OCO** order: a `TRIGGER` parent (the entry)
whose child OCO carries the protective stop and, in `stop_and_target` mode, the
target. The protection then **rests at the broker** and fires without the bot
being involved — or even running.

Off by default, so every existing preset keeps today's fully engine-managed
behaviour.

**Choosing a sync mode**

- `static` — submit once and never touch. The broker owns the resting levels
  for the life of the trade. Correct for fixed-stop/fixed-target scalps that do
  no in-trade level management.
- `replace` — the engine keeps managing levels, and every stop or target move
  issues a `replace_order` against the corresponding child. Required for any
  strategy whose edge is the in-trade ratchet (breakeven moves, profit locks,
  trailing). `bracket_replace_min_price_delta` debounces this so a per-cycle
  trail does not burn the Schwab rate budget on sub-penny adjustments. A
  replace cancels the child and creates a NEW order; the bracket follows the
  new id (the fill reconcile, later replaces, the cancel before an engine exit
  and a restart's adoption all use it).

**Two combinations are refused at config load**

Both are rejected when the config is read, not at order-build time when a live
order would already be in flight.

`bracket_sync_mode: static` with `trade_management_mode: adaptive` **or**
`adaptive_ladder` — the engine ratchets `stop_price` in-trade (breakeven moves,
profit locks, trailing) and `static` never replaces the resting child, so the
broker would sit on the entry-time stop for the life of the trade while the
engine believed it had tightened. Use `replace`.

`bracket_legs: stop_and_target` with `trade_management_mode: adaptive_ladder` —
a resting target limit defeats both of the ladder's defining behaviours: it
deliberately declines a target-tag exit
(`adaptive_ladder_suppress_target_exit`) so it can roll to the next rung, and
on the final rung it clears `target_price` entirely to run a runner. A resting
target fills through both, silently degrading every ladder trade into a rung-1
scalp. Use `stop_only` and let the target stay engine-side.

So on an `adaptive_ladder` preset — which is what `top_tier_adaptive` and
`small_cap_squeeze` ship with — enabling brackets means setting **both**:

```yaml
execution:
  bracket_orders_enabled: true
  bracket_sync_mode: replace      # static is refused with adaptive/adaptive_ladder
  bracket_legs: stop_only         # stop_and_target is refused with adaptive_ladder
```

**Stop order type**

`STOP` fills wherever a flush ends, which is punishing on thin names.
`STOP_LIMIT` bounds that slippage at the cost of a no-fill tail: if price gaps
straight through the limit, the stop does not fill and the position is still
open. `bracket_stop_limit_offset_r` sets how far beyond the trigger the limit
sits, in units of the trade's initial R — wider tolerates more slippage in
exchange for a smaller no-fill risk.

**Extended hours**

Schwab rejects `STOP` orders outside the `NORMAL` session. With
`bracket_require_normal_session: true` (the default) a bracketed entry is
**rejected** pre/post market rather than sent unprotected. Set it to `false`
only if you knowingly accept naked extended-hours entries.

**What the engine still does**

Resting protection does not make the position unmanaged. Each cycle the engine
reconciles the bracket against the broker: it notices when a child filled and
books the exit, keeps the resting levels in step in `replace` mode, and cancels
the children before marketing out for any engine-side reason (time stop,
structure break, force-flatten). While a child is confirmed resting, the risk
manager suppresses the engine-side exit that the broker now owns, so the two
cannot both fire.

Dry runs keep exits engine-side. The bracket is recorded on the `OrderResult`
for parity and inspection, but nothing rests at a broker and the recorded state
is marked `simulated`.

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
- Detection thresholds expressed as a percent of price (equal-high/low tolerance, minimum prior impulse, a flag's pole, breakout readiness) are sized for a mean 1m bar range of ~0.67% of price — small/mid-cap volatility — and scale down with the symbol's own bar range, floored at 0.1×. A name at or above that volatility uses them unchanged. Before 2026-09-22 they were fixed, which on liquid mega caps (~0.1% bars) made them ~7× too wide: one replayed session fired 7 patterns in 1,850 evaluations, 16 of 18 patterns never; 97 fires across 10 patterns after.

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
| `show_ltf_fair_value_gaps`             | `false`      |
| `show_htf_order_blocks`               | `false`      |
| `show_ltf_order_blocks`                | `false`      |
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
- `show_htf_fair_value_gaps`, `show_ltf_fair_value_gaps`: HTF and LTF FVG overlays. Cross-timeframe protection is enforced automatically, so HTF FVGs do not render on the LTF chart and LTF FVGs do not render on the HTF chart.
- `show_htf_order_blocks`, `show_ltf_order_blocks`: HTF and LTF order block overlays. Render with a dashed-line border around a very faint fill so they are visually distinct from the solid-filled FVG overlays. Same green/red bullish/bearish color semantics. Driven by `support_resistance.htf_order_blocks_enabled` / `ltf_order_blocks_enabled` and the shared OB tuning knobs (`order_block_mode`, etc.). Cross-timeframe protection is enforced the same way as for FVGs.
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
| `ltf_fair_value_gaps_enabled`     | `true`       |
| `fair_value_gap_max_per_side`            | `3`          |
| `fair_value_gap_min_atr_mult`            | `0.06`       |
| `fair_value_gap_min_pct`                 | `0.0006`     |
| `ltf_order_blocks_enabled`        | `false`      |
| `htf_order_blocks_enabled`               | `false`      |
| `order_block_mode`                       | `loose`      |
| `order_block_max_per_side`               | `4`          |
| `order_block_min_atr_mult`               | `0.05`       |
| `order_block_min_pct`                    | `0.0005`     |
| `order_block_min_thrust_atr_mult`        | `0.75`       |
| `order_block_pivot_span`                 | `2`          |
| `order_block_new_high_lookback`          | `8`          |
| `trading_flip_confirmation_1m_bars`      | `2`          |
| `trading_flip_confirmation_5m_bars`      | `1`          |
| `flip_stop_buffer_atr_mult`              | `0.25`       |
| `flip_target_requires_momentum_confirm`  | `true`       |
| `regime_weight`                          | `0.7`        |
| `structure_enabled`                      | `true`       |
| `structure_ltf_pivot_span`                | `2`          |
| `structure_eq_atr_mult`                  | `0.25`       |
| `structure_ltf_weight`                    | `0.65`       |
| `structure_htf_weight`                   | `0.85`       |
| `structure_event_lookback_bars`          | `6`          |
| `htf_structure_event_lookback_bars`      | `null`       |
| `structure_min_range_atr_mult`           | `1.5`        |
| `structure_min_pivot_gap_bars`           | `0`          |
| `structure_ltf_timeframe_minutes`        | `0`          |
| `structure_exit_grace_minutes`           | `10`         |
| `structure_exit_min_post_entry_pivots`   | `2`          |
| `structure_exit_grace_minutes_pullback`  | `15`         |
| `structure_exit_require_bos_confirmation` | `true`      |
| `orb_entry_exit_grace_minutes`           | `20`         |

How the groups work:

- Core HTF level map:
  - `enabled`, `timeframe_minutes`, `lookback_days`, `pivot_span`, `max_levels_per_side`
  - Use these to decide how the level map is built. HTF refresh cadence is bar-aligned (one Schwab call per HTF bar boundary, plus a 10-second settle buffer) — there's no longer a refresh-seconds knob.
  - HTF bars are built from 07:00-20:00 bars only and laid out per session segment: a bar never spans the 09:30 open or the close (16:00, 13:00 on early closes). 1/5/15/30m bars are clock-aligned; 60m bars are 07:00, 08:00, 09:00 (to 09:30), 09:30 ... 15:30 (to 16:00), 16:00 ... 19:00.
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
  - Prior day / prior week are the regular sessions (09:30-16:00 ET, 13:00 on early closes) of the last trading day / W-FRI week before the current session date, never extended-hours bars; a premarket build measures back from today, not from the frame's last bar.
  - Allow prior-day / prior-week highs and lows as fallback levels only when a side ends up empty after normal S/R detection and cleanup. Those fallback levels are admitted on the correct side of the guarded fallback reference price, can still participate in normal flip handling, and flow through the normal S/R ladder logic. If no prior-day/week fallback is eligible, the builders can fall back to the frame extreme for that side.
- FVG detection:
  - `htf_fair_value_gaps_enabled` toggles HTF FVG generation; `ltf_fair_value_gaps_enabled` toggles LTF FVG generation. The shared `fair_value_gap_max_per_side`, `fair_value_gap_min_atr_mult`, and `fair_value_gap_min_pct` knobs apply to both timeframes.
  - Raising the min ATR or min percent thresholds makes FVG detection more selective.
- Order block detection:
  - `htf_order_blocks_enabled` and `ltf_order_blocks_enabled` toggle OB generation per timeframe.
  - `order_block_mode`: `loose` declares a break-of-structure when price prints a new N-bar high (`order_block_new_high_lookback`); `strict` requires a pivot-confirmed BoS (`order_block_pivot_span`). Strict produces fewer, higher-quality OBs.
  - `order_block_min_atr_mult` and `order_block_min_pct` set the minimum OB body size (whichever is larger).
  - `order_block_min_thrust_atr_mult` (default `0.75`) requires the BoS thrust — close-to-close move from the OB candle to the breakout candle — to be at least this fraction of ATR. Filters weak setups where a small candle randomly broke a recent high.
  - `order_block_max_per_side` caps OBs per side per timeframe. Ranking is strength-based (thrust × size × age × validity), so the strongest OBs survive when the cap clips — small-move noise OBs no longer displace strong OBs from real moves.
- Level-loss and flip behavior:
  - `trading_flip_confirmation_1m_bars` and `trading_flip_confirmation_5m_bars` tune how many bars confirm a level flip via the dual-frame OR rule (either gate fires confirms). `0` turns that frame's gate off (`(0, 1)` confirms on 5m bars only); at least one must be above 0, and negatives are rejected at load. The dashboard sidebar, dashboard chart zone classification, entry gatekeeper, position manager, and strategy entries all use this same strict gate so every consumer agrees on which side of a level price is sitting on.
  - Whether confirmed level-loss breaks trigger an exit is controlled by `shared_exit.use_sr_loss_exit` (off in every preset; see `shared_exit`).
  - `flip_stop_buffer_atr_mult` controls how far beyond a flipped level the stop is anchored when `risk.trade_management_mode: sr_flip` is active.
  - `flip_target_requires_momentum_confirm` prevents target extension on weak flips.
- Regime and structure:
  - `regime_weight` controls how strongly the S/R regime influences scoring.
  - `structure_enabled`, `structure_ltf_pivot_span`, `structure_eq_atr_mult`, `structure_ltf_weight`, `structure_htf_weight`, `structure_event_lookback_bars` control the mixed-timeframe structure layer. The CHoCH-exit toggle is `shared_exit.use_structure_exit`.
  - `htf_structure_event_lookback_bars` (default `null` = same as `structure_event_lookback_bars`) — BOS/CHoCH freshness window for the HTF structure (the S/R context's `market_structure`, on `timeframe_minutes` bars), counted in HTF bars. `structure_event_lookback_bars` keeps counting LTF structure bars. They were one knob until 2026-09-22, so retuning the LTF window for a resampled LTF frame silently rescaled the HTF one too: top_tier's 8 → 4 for 5m bars halved its 15m HTF window from 120 to 60 minutes. top_tier_adaptive and small_cap_squeeze pin it to `8`.
  - `structure_min_range_atr_mult` (default `1.5`) — when both EQH and EQL flags are set AND the spread between `reference_high` and `reference_low` is below this ATR threshold, the structure-derived `bias` resolves to `"neutral"` instead of firing midpoint / pivot / recent-event bias. Prevents bias-based structure exits (`structure_bearish_exit` / `structure_bullish_exit`) from firing inside a tight consolidation where bias would flip on noise. Genuine BoS through `reference_high` / `reference_low` (price actually broke out) still fires bias unchanged — that check runs BEFORE the tight-range short-circuit. `eqh` / `eql` flags remain set so range-regime entries (which key on the EQ labels for mean-reversion setups) still see them. CHoCH exits unaffected. Set `0.0` to disable.
  - `structure_min_pivot_gap_bars` (default `0`) — minimum bar separation between consecutive *alternating* structure pivots. When `> 0`, an opposite-kind pivot that prints closer than this many bars to the prior kept pivot is treated as intra-leg noise and skipped, so the leg continues instead of registering a 1-2-bar swing. Suppresses the HH/LH/EQH/LL/HL/EQL churn (and the BOS/CHoCH/structure-exit signals keyed on those labels) that volatile names produce on a fine frame. The swings are still ATR-sized, so this is purely a temporal-density filter, not an amplitude one. `0` disables it (original behavior for every strategy that doesn't set it).
  - `structure_ltf_timeframe_minutes` (default `0`) — resample the `"ltf"` structure frame to this many minutes before pivot analysis. The LTF structure context otherwise runs on the raw streaming frame (1m), which is finer than the regime-scoring LTF (`params.ltf_minutes`, often 5m) — making structure pivots ~5x denser than the bars the strategy actually trades on. When `> 0`, entry/exit/HTF-alignment all read structure off the coarser frame. `0` disables it (use the frame as-is). NOTE: raising this rescales every bar-based structure setting — `structure_event_lookback_bars` and `structure_exit_min_post_entry_pivots` are then in units of this timeframe, so lower them proportionally when enabling (e.g. `structure_event_lookback_bars` 6 → 3 for a 1m→5m switch). `htf_structure_event_lookback_bars` counts HTF bars and is not rescaled — pin it before lowering the LTF window.
- Structure-exit grace windows:
  - `structure_exit_grace_minutes` (default `10`) suppresses the bias-based structure exits — `structure_bearish_exit` / `structure_bullish_exit` — for the first N minutes after entry. Prevents a minor EQL/LL pivot from exiting an otherwise-healthy trade. The CHoCH exit is not graced: it needs only a CHoCH that happened after entry (see `shared_exit` below).
  - `structure_exit_min_post_entry_pivots` (default `2`) requires at least N new LTF-structure pivots to form AFTER entry before structure-based bias exits can fire. A pivot counts when its bar closed after the entry time (`MarketStructureContext.pivot_times`); until 2026-09-24 this compared a rolling-window pivot count against an entry-time count only some strategies stamped, so for the peer family it could never open. Complements the time grace; the CHoCH exit is exempt. (The LTF structure frame is 1m by default but follows `structure_ltf_timeframe_minutes` — so under a 5m structure these are 5m pivots and take proportionally longer to accumulate; lower this knob if the coarser frame holds losers too long.)
  - `structure_exit_grace_minutes_pullback` (default `15`) extends the grace for pullback entries (`position.metadata.entry_style_family == "pullback"`, stamped at entry — top_tier's pullback regime and `rth_trend_pullback` alike; until 2026-09-24 it keyed on `regime == "pullback"`, which only top_tier stamped). Pullback by design enters into LTF chop, so the first EQL/LL pivot is almost always noise. Falls back to the global grace when set lower. Added 2026-05-14 after AMD 14:36 LONG pullback was killed at hold=10.2m via `structure_bearish_exit:EQL` then recovered above target.
  - `structure_exit_require_bos_confirmation` (default `true`) additionally requires a recent BoS event (`bos_down` for long-exit, `bos_up` for short-exit) that happened after entry — not just a bias flip — before bias-based structure exits fire. EQL/HH alone is noisy; BoS means price actually broke a prior swing low/high. CHoCH exits are unaffected. Set to `false` to revert to bias-only behavior.
  - `orb_entry_exit_grace_minutes` (default `20`) extends the grace for opening-range entries (`position.metadata.entry_style_family == "orb"`: top_tier's ORB regime, `opening_range_breakout`, `microcap_gap_orb`; until 2026-09-24 it keyed on `orb_window_entry`, which only top_tier stamped). Suppresses `structure_bearish/bullish_exit` AND `chart_pattern_exit` for the first N minutes of the trade (not the CHoCH exit). ORB pullbacks frequently look like bearish structure breaks but continue higher once the opening flush resolves. Set to `0` to disable.

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
| `trendline_breakout_buffer_atr_mult`  | `0.65`                                  |
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
| `divergence_pivot_lookback`           | `4`                                     |
| `divergence_max_age_bars`             | `8`                                     |
| `htf_divergence_max_age_bars`         | `6`                                     |
| `divergence_min_price_move_pct`       | `0.0015`                                |
| `divergence_hidden_bonus_rsi`         | `0.10`                                  |
| `divergence_hidden_bonus_obv`         | `0.08`                                  |
| `htf_divergence_aligned_bonus_rsi`    | `0.20`                                  |
| `htf_divergence_counter_penalty_rsi`  | `0.25`                                  |
| `htf_divergence_hidden_bonus_rsi`     | `0.10`                                  |
| `bollinger_enabled`                   | `true`                                  |
| `bollinger_length`                    | `20`                                    |
| `bollinger_std_mult`                  | `2.0`                                   |
| `bollinger_squeeze_width_pct`         | `0.06` (see note)                       |
| `bollinger_entry_bonus_midband`       | `0.16`                                  |
| `bollinger_entry_penalty_outer_band`  | `0.22`                                  |

> **Note on `bollinger_squeeze_width_pct`.** The value is a FRACTION of price
> (`(bb_upper - bb_lower) / bb_mid`), not a percentage, and the width scales
> with bar size and universe. Every preset sets it to the p25 of RTH BB(20,2)
> widths on the timeframe that strategy builds its technical context on
> (measured 2026-09-22 on archived bars; the $2-$20 screener row on
> 2026-09-23, on that screener's own 2026-04-27 candidates):
>
> | Universe / timeframe | Value | Presets | `0.06` flagged |
> |---|---|---|---|
> | large caps, 1m | `0.0025` | top_tier, `rth_trend_pullback`, `volatility_squeeze_breakout`, `pairs_residual`, `peer_confirmed_key_levels_1m` | 99.9% |
> | large caps, 5m LTF | `0.0061` | `peer_confirmed_htf_pivots` / `_trend_continuation` / `_key_levels` | 98.4% |
> | SPY / QQQ, 1m | `0.0014` | both `zero_dte` presets | 100% |
> | small caps, 1m | `0.076` | `small_cap_squeeze`, both `microcap` presets | 16.4% |
> | $2-$20 screener, 1m | `0.011` | `mean_reversion`, `closing_reversal`, `opening_range_breakout` (`momentum_close` too, where the bands are off) | 90% |
>
> The `0.06` code default only fits wide-banded small caps; everywhere else it
> made `bollinger_squeeze` a constant. The flag is a hard gate for top_tier's
> `range`, a scoring input for `vol_squeeze` and `volatility_squeeze_breakout`,
> and while it is on it suppresses the Bollinger target cap and the weak-ADX
> entry penalty. Re-measure rather than copy when a preset's universe or
> timeframe changes.
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
  - `trendline_*` controls pivot trendline detection and breakout sensitivity. Tolerances and the break buffer are multiples of `atr_with_floor` = max(ATR14, 0.15% of the frame close), with no percent floor of their own; on 1m large caps that ATR floor decides ~70-80% of bars (a 0.0975%-of-price buffer at 0.65), so they scale with volatility only above it. The channel break tolerance keeps its own 0.15%-of-price floor. Trendline and channel windows start at the current session's first bar, so on 5m LTF frames they need today's pivots and are absent for roughly the first hour; fib and anchored-VWAP impulses skip extended-hours pivots instead.
  - A broken line is spent once a close has cleared the break buffer after its last touch and a pivot has then formed on its far side; it retires instead of re-raising its break. A wick, or a dip that closed inside the buffer, does not retire it.
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
  - `divergence_*` controls RSI/OBV divergence detection and the score adjustments derived from it. Detection is multi-pivot (walks the most recent `divergence_pivot_lookback` swing pivots) and age-gated: a match whose newest pivot is more than `divergence_max_age_bars` bars old is dropped. While `use_rth_session_indicators` is on and the clock is inside the session-indicator window, the age counts session bars only, so yesterday's closing pivots stay live across the overnight; a reader outside the window (premarket, post-market) counts every bar. Pivots are always paired on session bars. Until 2026-09-24 the age counted every bar, so the overnight tape aged yesterday's pivots out: on symbol-days with a dense overnight tape the 60m HTF divergence read on 0% of the 09:30-11:00 minutes, and it now reads on 23.6% of RTH minutes instead of 10.4% (15m: 16.4% instead of 14.6%). On 1m frames yesterday's pivots reach the open only while they are still inside the trimmed tail (120 bars at the default lookbacks), so a name printing a bar every 1-2 minutes outside RTH has none at the open; 5m and HTF frames keep them. `divergence_min_price_move_pct` is the price-move floor a pivot pair must clear before a divergence can register.
  - **Two pattern families.** *Regular* divergence (price extends, indicator weakens) signals a likely reversal. *Hidden* divergence (price holds the trend, indicator pulls back) signals continuation. Counter-direction regular divergence on an entry triggers `divergence_counter_*_penalty`; same-direction hidden divergence on an entry triggers `divergence_hidden_bonus_*`. Both are part of the technical score term (`shared_entry.use_technical_entry_adjustment`). The hard veto when RSI AND OBV both diverge against an entry is `shared_entry.use_dual_divergence_veto`.
  - **Two timeframes.** RSI divergence is computed on both the strategy's primary frame (LTF) and the HTF context. HTF-aligned divergence in the trade direction adds `htf_divergence_aligned_bonus_rsi`; HTF counter-direction divergence subtracts `htf_divergence_counter_penalty_rsi`; HTF same-direction hidden divergence adds `htf_divergence_hidden_bonus_rsi`. The `_rsi` suffix on these HTF knobs is deliberate — HTF OBV divergence is intentionally not computed (volume-driven indicators smear under HTF resampling). The score term is `shared_entry.use_htf_divergence_score`. The HTF RSI divergence is built once by the data feed for every strategy and the dashboard, from `enabled` and `divergence_enabled` (false turns it off on both timeframes), `htf_divergence_max_age_bars` (default 6, in HTF bars, on the same session clock) and the thresholds it shares with the LTF divergence: `divergence_pivot_lookback`, `divergence_min_price_move_pct` and `divergence_rsi_min_delta`. No strategy passes them, and they are part of the HTF context cache key. Until 2026-09-24 every HTF build used a hardcoded age of 6 with divergence always on, and until 2026-09-25 it used the builder's defaults for the three thresholds, so retuning them moved only the LTF divergence (every preset ships those defaults, 4 / 0.0015 / 2.5). Both timeframes honour a configured 0 for the three; the LTF readers (the strategy's and the dashboard's) turned it into the default until 2026-09-25 as well. `divergence_rsi_length` is LTF-only; the HTF reads its frame's `rsi14`.
  - **Visual rendering.** Each detected divergence is drawn on the price chart as a trendline connecting the two pivots, color-coded green (bullish) or red (bearish), solid stroke for regular and dashed stroke for hidden. RSI divergences render at full intensity, OBV at lighter weight. A divergence whose older pivot lies left of the chart's first bar is drawn in from the left edge at its true slope, clipped to the plot (the older pivot is placed by its bar gap to the newer one, 2026-09-25); one whose newer pivot is off the chart too is not drawn. Toggle per chart profile via `dashboard.charting.{compact,expanded}.show_rsi_divergence` (default true) and `show_obv_divergence` (default false).
- Bollinger context:
  - `bollinger_*` controls band calculation, squeeze detection, entry confluence, optional targeting, and optional exits.
- Target/stop refinement:
  - `target_use_bollinger`, `target_use_fib`, `target_use_channel`, `target_use_trendline`, `stop_use_trendline`
- Entry bonuses/penalties:
  - `entry_bonus_channel_alignment`, `entry_bonus_trendline_respect`, `entry_penalty_near_extension`
- Exit toggles live in `shared_exit.use_trendline_break`, `use_channel_break`, `use_bollinger_reject`, `use_anchored_vwap_loss` (detectors still owned here; when to fire is decided there).

### `shared_entry`

The entry-side knobs. **Global since 2026-09-24**: every strategy hands each candidate entry to one shared entry stage, `SharedEntryPolicy` (`intraday_tv_schwab_bot/_strategies/shared_entry.py`), and that stage is the only reader of this section. A knob therefore acts on every strategy whose YAML sets it; no strategy reads the section, calls a knob helper, or can rewrite a knob. A strategy keeps its own setup logic, alternatives and selection. For each alternative it builds an `EntryProposal`, calls `self.entry_policy.admit(...)`, runs its own post-admission steps (ladder, runner, management, its own score) on the `AdmittedEntry` it gets back, and builds the signal with `self.entry_policy.emit(...)`, the only place a `Signal` is constructed. A strategy's only say is declarative, in its manifest: `capabilities.shared_entry.exemptions` takes one of its styles out of named vetoes, `capabilities.shared_entry.divergence_entry: false` opts it out of divergence-only entries, and `capabilities.signal_priority` declares its ranking.

Until 2026-09-24 each strategy called the knob helpers it chose to. Most knobs did nothing for most strategies: `peer_confirmed_key_levels` reached three of them, the reversal strategies never met the dual-divergence veto, and only `top_tier_adaptive` met the candle filter. Some strategies re-implemented a knob under a param of their own (`use_sr_veto`, `orb_apply_*_veto`, `orb_bypass_*_entry`, `reject_entry_near_broken_level`), and a `strategy_logic_default` hook let a strategy rewrite any knob. The strategy-author contract is in [`_strategies/README.md`](intraday_tv_schwab_bot/_strategies/README.md). `tests/test_shared_knob_contract.py` enforces it, and `tests/test_knob_reach_matrix.py` switches every veto, the score floor, every score term, both refinements and every exit family on and off for all 17 strategies.

**Code defaults vs shipped values.** The table shows the dataclass defaults, which a scaffolded preset starts from. Every shipped preset instead carries what its strategy effectively ran before the change ("parity": a knob the strategy never read is off), plus three deliberate flips; see [`configs/README_PRESETS.md`](configs/README_PRESETS.md). `use_broken_level_guard` and `use_divergence_entry_signal` are off in every preset.

**The stage, per proposal** (`admit`, in this order):

1. **Raw R:R gate**, only when the builder asks for it (`EntryProposal.raw_rr_gate`). The raw stop and target must clear `min_target_rr` before anything else is built, or the proposal is refused as `<side>_<gate>(close=..,target=..,reward=..,risk=..)`. top_tier's `orb` (`orb_measured_move_exhausted`) and `sr_scalp` (`stop_floor_kills_rr`, only when its noise floor widened the stop) use it.
2. **Contexts** on the frames the proposal pins. The S/R context is built on `sr_frame` (which also supplies the broken-level ATR and the divergence candidates). Market structure, technical levels, chart patterns and candles are built on `gate_frame`. The FVG and order-block reads use `zone_frame` (default: the gate frame), and the refinement clamp reads the ATR of `level_frame`. Contexts are cached per frame; a strategy that already built one for its own logic hands it in (`EntryContexts`).
3. **Retest admission**, only with a `RetestTrigger`. The FVG retest plan, plus the order-block plan when the trigger names `ob` and `support_resistance.ltf_order_blocks_enabled` is on, may clear the strategy's own pending reasons when every one of them is in the proposal's deferrable set. Otherwise a waiting plan's reason, or else a rejecting plan's reason, replaces them. A non-deferrable reason blocks the clearing, and all reasons are kept.
4. **Vetoes**, in `VETO_GATES` order: structure, S/R, broken level, chart, dual divergence, candle. Each is judged on the proposal's MARKET direction; an option's is the underlying's, so a bull put credit spread is a LONG proposal. Every switched-on veto runs, so a refusal lists the strategy's own reasons followed by every veto that fired, the first one primary. A veto the manifest exempts for the proposal's style is skipped and stamped as exempted. The vetoes run after the retest admission, so a retest can no longer carry an entry past one.
5. **Levels** (price-level proposals only): the proposal's `stop_resolver` if it has one (None refuses as `stop_above_entry`), the S/R refinement, the technical refinement, then the retest stop anchor, re-clamped by `min_stop_atr_mult`. A stop on the wrong side of entry is refused as `stop_on_wrong_side(...)`. Option (premium) proposals carry no price stop or target and skip this step.
6. **Score**: `shared_context_score` = `entry_context_adjustment` (the technical, S/R proximity and HTF divergence terms) + `fvg_entry_adjustment` + the divergence confirmation bump. Below `min_shared_context_score` the proposal is refused as `shared_context_below_min(score=..,min=..)`.

A refusal is recorded once, under the proposal's failure key (its style unless it names another), for the strategy's decision log. `emit` then builds the signal from the ADMITTED stop and target. The target may instead be the adaptive ladder's active rung, or None for a runner; anything else raises. It stamps:

- `regime` (defaults to the style), `entry_style_family`, and `orb_window_entry` (true for family `orb`);
- `strategy_priority_score` (the strategy's own score, without the shared terms), `shared_context_score`, and `final_priority_score` (their sum);
- `entry_price` on price-level signals;
- `shared_entry_applied`, `shared_entry_gates_applied`, `shared_entry_gates_exempted` and `shared_entry_admitted_via_retest`;
- the retest-plan, FVG and entry-context fields, the chart / `msltf_*` structure / S/R / `tech_*` lists, and the divergence confirmation or conflict fields.

The exit graces key on `entry_style_family` (see `shared_exit`).

| Option | Code default | Kind | Notes |
|---|---|---|---|
| `use_fvg_context` | `true` | score term, retest | FVG score term, FVG retest plan, FVG runner / management bias |
| `use_structure_filter` | `true` | gate (veto) | also needs `support_resistance.structure_enabled` |
| `use_sr_filter` | `true` | gate (veto) | also needs `support_resistance.enabled` |
| `use_broken_level_guard` | `false` | gate (veto) | new 2026-09-24; off in every preset |
| `broken_level_min_clearance_pct` | `0.0025` | modifier | x the proposal's volatility scale |
| `broken_level_min_clearance_atr` | `0.72` | modifier | ATR of the S/R frame |
| `use_opposing_chart_filter` | `true` | gate (veto) | |
| `use_dual_divergence_veto` | `true` | gate (veto) | renamed from `use_divergence_filter`; also needs `technical_levels.enabled` and `divergence_enabled` |
| `use_opposing_candle_filter` | `false` | gate (veto) | threshold `candles.opposing_net_score_threshold` |
| `use_sr_stop_target_refinement` | `true` | level refinement | runs first |
| `use_technical_stop_target_refinement` | `true` | level refinement | runs second; also needs `technical_levels.enabled` |
| `min_target_rr` | `1.0` | modifier | target-cap floor, raw R:R gate, divergence target; `0` / `null` = off |
| `min_stop_atr_mult` | `1.5` | modifier | stop-pull floor, including the retest anchor; `0` / `null` = off |
| `use_technical_entry_adjustment` | `true` | score term | technical term, including the LTF divergence penalties / hidden bonuses |
| `use_htf_divergence_score` | `true` | score term | renamed from `use_htf_divergence_filter` |
| `min_shared_context_score` | `null` | gate (floor) | new 2026-09-24; `null` in every preset |
| `use_divergence_entry_signal` | `false` | trigger, score term | divergence-only entries plus the confirmation bump; off in every preset |
| `divergence_entry_min_age_bars` | `0` | modifier | |
| `divergence_entry_require_sr_confluence` | `true` | modifier | |
| `divergence_entry_score_floor` | `1.5` | modifier | |
| `divergence_entry_score_bump` | `0.20` | score term | added to a proposal the divergence confirms |

Knobs outside this section feed the stage too. Besides the ones the Notes column names (`support_resistance.enabled` / `structure_enabled`, `technical_levels.enabled` / `divergence_enabled`, and `candles.opposing_net_score_threshold` for the candle veto's threshold):

- `support_resistance.entry_proximity_scoring_enabled` switches the S/R proximity score term, which also needs `support_resistance.enabled`.
- `support_resistance.ltf_order_blocks_enabled` adds the order-block plan to the retest admission (step 3).
- The technical and S/R proximity terms take their weights from `technical_levels` / `support_resistance`, and the technical term honours the per-indicator `technical_levels.*_enabled` switches.
- A divergence candidate reads its age limit from `technical_levels.divergence_max_age_bars` and its stop buffer from `support_resistance.stop_buffer_atr_mult`.

Behavior:

- Every weight and threshold is read directly and None-aware: a configured `0` is `0`, and only `null` falls back where a fallback exists. Until 2026-09-24 the score weights were read as `float(x or default)`, so a `0` meant the default. `small_cap_squeeze`'s three zeroed extension penalties (`technical_levels.atr_stretch_penalty`, `bollinger_entry_penalty_outer_band`, `entry_penalty_near_extension`) were therefore inert and still docked up to 0.75 from a stretched entry. They take effect now.
- `use_fvg_context` (score term, retest) controls three things. The first is the FVG score term `fvg_entry_adjustment`, weighted by the strategy's `htf_fvg_entry_weight` / `ltf_fvg_entry_weight` / `opposing_fvg_entry_penalty_mult` params. The second is the FVG retest plan of the retest admission. The third is the FVG continuation / reversal bias the strategies read for their runner and management. The bias is 0 when the knob is off, so off also means no FVG-driven runner. zero_dte's regime FVG scores read it through `SharedEntryPolicy.fvg_regime_scores`.
- `use_structure_filter` (veto) refuses an entry against the LTF market structure on the gate frame: a fresh opposing CHoCH, or an opposing bias without a fresh same-side BoS (`market_structure_bearish(...)` / `market_structure_bullish(...)`). It only runs while `support_resistance.structure_enabled` is on. top_tier and small_cap_squeeze read it on the 1m frame resampled to `structure_ltf_timeframe_minutes` (5 in their presets), and the peer strategies on their 5m LTF. Both 5m frames end in the still-forming bucket, which since 2026-09-25 confirms no swing pivot (a pivot confirmed by the first minutes of a bucket could be gone at its close); its close and any BoS / CHoCH break on it still count at once. The same-side-BoS escape can only apply on an inverted reference pair (reference low above reference high, which a gap leaves) with the close through both references: the bias is the later of the two breaks, and the earlier one can still be fresh. Until 2026-09-25 the resolver tested the high side first, so every such close read bullish, a gap up that broke its first swing low included, and only a SHORT could use the escape. On the archive the case is rare: 21 of 67,891 5m bars, none at a top_tier entry. Since 2026-09-25 (a user decision) both the top_tier and small_cap_squeeze manifests exempt their `range`, `pullback` and `sr_scalp` regimes from the veto. Those regimes enter against the short-term swing by design, and the 5m bias the veto reads is mostly a location reading: the resolver checks the midpoint of the last swing before the swing labels, so an HH / HL uptrend that pulls back into the lower half of its last swing reads bearish. Over 162 archived top_tier entries the veto had blocked 20, averaging +0.41R against -0.14R for the entries it kept. With the exemption it blocks 7 of them; about +2.1R of what it releases is not also refused by the S/R or chart veto. `trend`, `momentum`, `vol_squeeze` and `vwap_reclaim` keep it.
- `use_sr_filter` (veto) refuses an entry that crowds the HTF level ahead of it: inside `support_resistance.entry_min_clearance_pct` OR `entry_min_clearance_atr` of the resistance over a LONG or the support under a SHORT. A pending, unconfirmed-broken level counts as the nearest one and is always too close. It also refuses an entry through a broken level when no level stands on its own side. The tokens are `too_close_to_htf_resistance(...)`, `too_close_to_htf_support(...)`, `htf_breakdown_below_support(...)` and `htf_breakout_above_resistance(...)`. It only runs while `support_resistance.enabled` is on. It replaced `use_sr_veto` (htf_pivots, trend_continuation) and zero_dte_etf_long_options' `orb_apply_sr_veto`. `peer_confirmed_key_levels` keeps its own AND peer-target clearance as the strategy param `require_peer_target_clearance`. Since 2026-09-25 (a user decision) top_tier's manifest exempts its `vwap_reclaim` regime from this veto, and small_cap_squeeze's does not. vwap_reclaim enters on the bar that closes back across VWAP, which on a large cap sits right next to an HTF level: the veto refused 15 of top_tier's 16 vwap_reclaim entries on 09-21..09-24 (11 winners, 4 losers, +6.74R), and the live bot, which never ran this veto state, traded all 16 for +6.94R. In small_cap_squeeze it refused only losers (3 vwap_reclaim entries in June, -2.45R). The S/R stop / target refinement and the ladder still read the levels for vwap_reclaim.
- `use_broken_level_guard` (veto, off in every preset) refuses an entry within `broken_level_min_clearance_pct` (times the proposal's volatility scale; top_tier passes its own) OR `broken_level_min_clearance_atr` ATR (the S/R frame's) of a confirmed BROKEN level beyond it: `long_near_broken_support(...)` / `short_near_broken_resistance(...)`. It moved out of top_tier's `reject_entry_near_broken_level` on 2026-09-24. It existed to keep an entry clear of the S/R-loss exit's trigger, and that exit ships off. Over 09-17..09-23 it blocked about 3.5 setups per session. Across all 139 archived episodes (04-24..09-23), on a +2R / -1R bracket, the blocked setups made -0.09R against -0.04R for top_tier fills (difference CI [-0.37, +0.30]); the 57 it alone blocked made +0.18R. 110 of 138 closed back through the level within 30 minutes. The top_tier and small_cap_squeeze presets carry their old thresholds (0.0035 / 0.90 and 0.0025 / 0.72) in case it is switched on.
- `use_opposing_chart_filter` (veto) refuses an entry against an opposing chart pattern (`chart_pattern_opposed`). It now also meets retest-admitted entries; it used to run only while no other reason was pending.
- `use_dual_divergence_veto` (veto) refuses an entry when RSI AND OBV both diverge against it on the gate frame (`dual_counter_divergence(rsi=..,obv=..)`). It only runs while `technical_levels.enabled` and `divergence_enabled` are on. It was renamed from `use_divergence_filter`, whose name and docs claimed it drove the LTF counter-divergence penalties; those belong to `use_technical_entry_adjustment`. Its old second switch, `technical_levels.divergence_block_dual_counter`, is gone.
- `use_opposing_candle_filter` (veto) refuses an entry when the opposing candle cluster reaches `candles.opposing_net_score_threshold` (`long_opposing_candle(net_score=..,matches=..)` / `short_...`). It reuses the cached candle context, so there are no extra TA-Lib calls. It abstains when any of the gate frame's last 3 bars is a single print (high == low, or an unreadable high / low) and stamps `shared_entry_candle_abstained: zero_range`: TA-Lib reads single prints as dojis / white candles, so thin premarket tape builds TRISTAR / GAPSIDESIDEWHITE / HARAMICROSS by itself (40% of `microcap_pm_breakout`'s premarket LONG vetoes on archived tape). The exit side's candle family holds on the same tape. Only top_tier read it before 2026-09-24.
- `use_sr_stop_target_refinement` / `use_technical_stop_target_refinement` (level refinement) run S/R first, then technical. The S/R pass pulls the stop in to just beyond the nearest support (resistance), never widening it, and caps the target just short of the next opposing level. The technical pass pulls the stop to a trendline and caps the target at a fib / channel / trendline / Bollinger level (`technical_levels.target_use_*`, `stop_use_trendline`). Everything a strategy does after admission reads the refined levels: the ladder rungs, the runner, top_tier's Fix G and the adaptive management.
- `min_target_rr` (float, default `1.0`) is the R:R floor on every target cap in both refinements. When a cap would drop R:R below it, the cap is rejected and the strategy's target is kept. This protects against the "$0.10 target" failure mode, where a nearby level collapses R:R toward zero. It applies whatever `risk.trade_management_mode` is. It is also the raw R:R gate's threshold and the divergence entry's target multiple. A positive risk and reward are always required; `0` or `null` switches only the ratio floor off (until 2026-09-24 a configured `0` read as `1.0`).
- `min_stop_atr_mult` (float, default `1.5`) is the floor, in ATR14 units, on how far the refinements may pull the **stop** toward entry. `min_target_rr` cannot police the stop side, because a tighter stop *raises* reward/risk. Without an absolute floor, a support level a few cents under entry produced a few-cent stop and silently overrode the `risk.default_stop_pct` backstop: over 2026-05-12..29, 46 of 57 `top_tier_adaptive` entries had their stop pinned to `nearest_support - level_buffer`, a median 2x (worst 10x) tighter than the builder floor. An over-tight stop is clamped back to this distance rather than discarded. Discarding would revert to the flat `default_stop_pct`, which on a quiet symbol is enormous in ATR terms (one logged case reached 11 ATR). The clamp never *widens* past the incoming stop, so a builder that deliberately chose a tighter stop (range / sr_scalp / momentum) keeps it. Since 2026-09-24 it also bounds the FVG / order-block retest stop anchor, which used to be applied after the clamp and could pull a stop to 0.05% from entry. `0` or `null` switches it off. The ATR is the last bar's `atr14` on the proposal's level frame; a bar without one yet reads as max(0.15% of the close, $0.01).
- `use_technical_entry_adjustment` (score term) is the technical term: the channel, trendline, Bollinger, ADX, anchored-VWAP, ATR and OBV bonuses and penalties, plus the LTF counter-divergence penalties and same-side hidden-divergence bonuses (`technical_levels.*`). It only applies while `technical_levels.enabled` is on.
- `support_resistance.entry_proximity_scoring_enabled` (score term, in its own section) is the S/R bias and proximity term (`entry_bias_score_weight`, `entry_favorable_proximity_bonus`, `entry_opposing_proximity_penalty`). It only applies while `support_resistance.enabled` is on.
- `use_htf_divergence_score` (score term) adds `technical_levels.htf_divergence_aligned_bonus_rsi` for an HTF RSI divergence in the trade's direction, subtracts `htf_divergence_counter_penalty_rsi` for a counter one, and adds `htf_divergence_hidden_bonus_rsi` for a same-side hidden one. It never blocks. It was renamed from `use_htf_divergence_filter`, which it never was. It reads the strategy's own HTF context when the proposal brings one (the peers pass their 60m contexts), else the strategy's default HTF build. The HTF divergences are absent whenever `technical_levels.enabled` or `divergence_enabled` is off (see `technical_levels`).
- `min_shared_context_score` (gate, default `null`) is an optional floor on the proposal's shared score. `null` means no floor, as shipped.
- `use_divergence_entry_signal` (trigger, off in every preset) makes divergence an entry trigger of its own; the `divergence_entry_*` knobs tune it.
  - **The candidate.** Once per symbol per cycle the stage reads a divergence candidate for each side on the proposal's S/R frame (the canonical frame for a divergence-only entry). It is the first of the regular, then the hidden, RSI then OBV divergence whose age is inside [`divergence_entry_min_age_bars`, `technical_levels.divergence_max_age_bars`] and whose latest pivot is in the reader's current session; yesterday's pivots are refused, not rescaled across the gap. A hidden one also needs the HTF EMAs aligned. With `divergence_entry_require_sr_confluence`, the pivot must sit within the S/R level buffer of a level on its side.
  - **Score, stop and target.** Score = indicator strength (the delta over twice the indicator's minimum delta, capped at 1) + recency (1 - age / max age) + 0.5 regular / 0.3 hidden + 0.5 confluence; it must reach `divergence_entry_score_floor`. The stop is the pivot minus (plus) `support_resistance.stop_buffer_atr_mult` ATR. The target is `min_target_rr` x risk, capped at the opposing S/R level; a capped target must still clear `min_target_rr`, so a wall inside that room refuses the candidate. With `min_target_rr` off the target is the opposing level, or none (a runner).
  - **Confirmation.** Every strategy proposal is checked against the candidates. One on the proposal's own side adds `divergence_entry_score_bump` to its shared score (`divergence_entry_confirmed`). One only on the other side is stamped as a conflict (`divergence_entry_conflict`) and changes nothing.
  - **Divergence-only entries.** After `entry_signals`, a symbol the strategy produced no signal for may enter on its better takeable side, as far as the candidate's directional bias and `risk.allow_short` allow. It enters as a proposal of style `divergence_regular` (family `reversal`) or `divergence_hidden` (`continuation`), so every veto, the refinement, the score floor, the ladder and the adaptive management apply, and it always ranks behind every strategy signal. A symbol that is held, that the strategy signalled, or that the strategy skipped before evaluating a setup is never entered this way. The last group is `shared_entry.DIVERGENCE_INELIGIBLE_REASONS`: not its symbol, outside its window, not enough data, a macro or earnings blackout, every side it trades switched off, plus any `insufficient_*` token. A refusal is added to the symbol's decision as `divergence_entry_blockers`. `pairs_residual` and both 0DTE strategies opt out in their manifests.
  - Until 2026-09-24 no strategy called the old helper, so the knob did nothing. The helper also read its stop buffer from a key that did not exist, divided recency by 8 whatever the max age, never read OBV, and kept only the better-scoring side, so a SHORT the strategy could not take hid a valid LONG.

**Ranking.** The gatekeeper sorts signals, descending, with `SharedEntryPolicy.rank_key`, built from the strategy manifest's `capabilities.signal_priority`:

`(tier, primary + w x shared_context_score x unit, *metadata_fields, final_priority_score, activity, -rank)`

- `primary_field` defaults to `final_priority_score`.
- `shared_score_weight` (w) defaults to 0.
- `rank_unit_field` names the metadata field that converts one shared-score point into the primary's units (default 1).
- `metadata_fields` is the tail.
- `tier` is 0 for a divergence-only entry and 1 for every strategy signal, so the strategy always wins a contested slot.
- A `strategy_priority_score` primary drops the `final_priority_score` tiebreak. Those strategies were never ranked on the shared terms, not even on a tie.
- The catalogue validates the block at load: unknown keys, duplicate fields, a negative or boolean weight, or a unit field without a positive weight all fail.

Shipped (user decision, 2026-09-24):

- `top_tier_adaptive` and `small_cap_squeeze` rank on `regime_score_normalized` + 1.0 x shared x `regime_rank_unit`. `regime_rank_unit = 1 / (regime ceiling - regime floor)`, the floor including a SHORT's `short_min_score_premium`, so one shared point moves a signal exactly as far as one raw regime-score point.
- The four peers rank on `ltf_score` + 0.5 x shared, then their seven-field tail. `peer_confirmed_htf_pivots` and `peer_confirmed_trend_continuation` pick their side with the same key.
- `microcap_pm_breakout` and both 0DTE strategies rank on `strategy_priority_score`, weight 0.
- Everyone else ranks on the default `final_priority_score`, which already contains the shared terms, weight 0.

`rank_key` replaced three rankers: the gatekeeper's generic key, top_tier's `signal_priority_key` override and the peers' manifest tuple. With every weight at 0 it reproduces their orders exactly, ties included (`tests/fixtures/rank_golden/`). Before the weights, the shared terms only broke near-ties behind top_tier's regime score and the peers' tail.

**Per-strategy wiring.** Frames are gate / S/R / level. "1m" is the strategy's canonical 1-minute frame. Nothing is exempt from a veto unless the manifest says so. An exempted veto is skipped for that style only and stamped in `shared_entry_gates_exempted`; the exemptions change no preset value (the 2026-09-25 ones are user decisions, see `use_structure_filter` / `use_sr_filter` above).

| Strategy | Style -> family | Frames | Manifest `shared_entry` | Rank |
|---|---|---|---|---|
| `top_tier_adaptive` | the regime (`trend`, `pullback`, `range`, `vol_squeeze`, `momentum`, `sr_scalp`, `orb`, `vwap_reclaim`) -> the same | 1m / 1m / 1m (structure resampled per `structure_ltf_timeframe_minutes`) | `exemptions: {orb: [structure, sr], range: [structure], pullback: [structure], sr_scalp: [structure], vwap_reclaim: [sr]}` | `regime_score_normalized`, w 1.0 x `regime_rank_unit` |
| `small_cap_squeeze` | as `top_tier_adaptive` (the same engine) | as `top_tier_adaptive` | `exemptions: {orb: [structure, sr], range: [structure], pullback: [structure], sr_scalp: [structure]}`: vwap_reclaim keeps the S/R veto; only its `trend`, `momentum` and `vwap_reclaim` regimes are on in the preset | as `top_tier_adaptive` |
| `peer_confirmed_key_levels`, `_1m` | `key_level` -> `peer` | LTF / 1m / LTF; FVG reads on 1m (`zone_frame`) | none | `ltf_score`, w 0.5, then the tail |
| `peer_confirmed_htf_pivots` | `pivot_reclaim` / `pivot_rejection` / `pivot_continuation` -> `pivot` | 5m LTF / 1m / 5m LTF | `exemptions: {pivot_rejection: [structure]}` | `ltf_score`, w 0.5, then the tail |
| `peer_confirmed_trend_continuation` | `trend_continuation` -> `continuation` | 5m LTF / 1m / 5m LTF | none | `ltf_score`, w 0.5, then the tail |
| `momentum_close` | `momentum` -> `momentum` | 1m | none | default |
| `opening_range_breakout`, `microcap_gap_orb` | `orb` -> `orb` | 1m | none | default |
| `rth_trend_pullback` | `pullback` -> `pullback` | 1m | none | default |
| `volatility_squeeze_breakout` | `vol_squeeze` -> `vol_squeeze` | 1m | none | default |
| `mean_reversion`, `closing_reversal` | `reversal` -> `reversal` | 1m | none | default |
| `microcap_pm_breakout` | `pm_breakout` -> `breakout` | 1m | none (divergence-only entries stay possible if the knob is switched on) | `strategy_priority_score`, w 0 |
| `pairs_residual` | `pairs` -> `pairs` | the traded leg's frame | `divergence_entry: false` | default |
| `zero_dte_etf_options` | `orb_debit_spread` / `trend_debit_spread` -> `option_debit`; `midday_credit_spread` -> `option_credit` | premium proposal on the underlying (no level frame) | `exemptions: {midday_credit_spread: [structure]}`, `divergence_entry: false` | `strategy_priority_score`, w 0 |
| `zero_dte_etf_long_options` | `orb_long_option` / `trend_long_option` -> `option_long` | premium proposal on the underlying | `divergence_entry: false` | `strategy_priority_score`, w 0 |

On the peer strategies the broken-level guard's ATR and the divergence candidates read the 1m S/R frame, the frame top_tier's broken-level thresholds were set on. The refinement's stop floor is measured in the ATR of their LTF trigger frame.

### `shared_exit`

Global exit knobs. Every strategy gets them, as written in its YAML: they are read by one owner, `shared_exit.SharedExitPolicy`, which the position manager runs for every open position each cycle, and no strategy can override it (`BaseStrategy.__init_subclass__` raises `TypeError` for a strategy that defines `position_exit_signal` or `shared_exit_signal`). A strategy adds exits of its own through the `strategy_exit_signal` hook, which runs only after every shared family held (the peer family's adaptive-ladder defence, `microcap_pm_breakout`'s blowoff guard). Until 2026-09-24 the pipeline was a strategy method: the four `peer_confirmed_*` strategies overrode it and ran only the ladder and the technical exits — `risk.time_stop_minutes`, the chart / candle / structure / S/R exits and the ORB grace never reached them — and on top of that forced `use_chart_pattern_exit`, `use_structure_exit` and `use_sr_loss_exit` off.

Each cycle, in order: the risk exits (stop / target / trail / peak-giveback, `risk.update_position`); then the shared families — `time_stop`, `chart_pattern`, `candle_pattern`, `structure_choch`, `structure_bias`, `technical`, `sr_loss` — the first that fires deciding; then the strategy hook; then the divergence scale-out, last so any full exit wins. Force flatten turns a hold or a pending scale-out into a full exit and keeps any full exit's own reason. The gates in front of each family are one table, `shared_exit.EXIT_FAMILY_GATES`:

| Family | R gate | hold grace | ORB grace | post-entry pivots | post-entry event | bar range | tape |
|---|---|---|---|---|---|---|---|
| `time_stop` | no | no | no | no | no | no | none |
| `chart_pattern` | no | no | yes | no | no | no | strict or loose |
| `candle_pattern` | no | no | no | no | no | yes | strict |
| `structure_choch` | no | no | no | no | yes (the CHoCH) | no | strict |
| `structure_bias` | yes | yes | yes | yes | yes (the confirming BoS) | no | strict |
| `technical` | yes | no | no | no | no | no | strict |
| `sr_loss` | no | no | no | no | no | no | strict |

The graces key on `position.metadata.entry_style_family`, which the entry stamps (`orb` for opening-range entries, `pullback` for pullback entries). "After entry" means the bar CLOSED after the fill: bars are labelled at their start and the frames hold completed bars, so a CHoCH, BoS or pivot on the bar the entry filled in is one the entry never saw (until 2026-09-24 it was compared by its label and read as pre-entry for as long as price stayed through the level; on a 5m structure frame that blind window was up to 5 minutes). "Bar range": the family holds when any bar of the last three (`bars.CANDLE_PATTERN_WINDOW_BARS`, the window the multi-bar patterns read and the one the entry candle veto checks) is a single print (high == low) — TA-Lib reads such bars as dojis / white candles, so thin tape builds TRISTAR / DOJISTAR / HARAMI / HIKKAKE / GAPSIDESIDEWHITE by itself.

The ORB and pullback graces are global since 2026-09-24. They follow the style family the entry stamps, not the strategy: `orb` for top_tier's orb regime, `opening_range_breakout` and `microcap_gap_orb`, `pullback` for top_tier's pullback regime and `rth_trend_pullback`. Before, they keyed on `orb_window_entry` / `regime == "pullback"`, which only top_tier stamped. A position that was open across the upgrade restart has no `entry_style_family` and gets neither grace. The peer strategies now honour their YAML's `risk.time_stop_minutes` and structure / chart / candle / S/R exits; their presets keep them at what the old override effectively ran (time stop 0, those exits off).

**Partial closes.** An exit decision is an `ExitDecision(reason, family, fraction, marker)`; a `fraction` below 1 is a scale-out of the CURRENT quantity. The position manager handles a scale-out as follows:

- It sizes the slice with a floor (`shared_exit.partial_exit_qty`). A 1-lot option or 1-share position cannot scale out, so the trigger is spent without an order.
- It cancels any resting broker bracket first, as for every engine-side exit.
- It sends `execution.close_position(position, qty)`.
- It books the slice as a partial leg: the session report lists slices under `per_partial_exit_reason`, and `trades.csv` folds them into the trade's row.
- The remainder stays open at its stop. In bracket mode it is re-protected at the broker at the current engine levels: at once when the slice books; in full when the slice never reached the broker (a rejected submit, a stale quote); and when the slice is left working at the broker, the shares it does not cover at once, then everything still held once it settles (the bracket placed beside it, or one a restart put on the position, is adopted and resized rather than stacked on). A full exit left working is re-protected the same way once it settles. While a slice works, the engine still runs its risk check on the shares outside it; a risk exit cancels the slice, and the cycle that settles it sends the full exit.
- It appends the decision's one-shot marker to `metadata['<family>_exits']`, so the same trigger cannot scale the position out again.

Force flatten turns a pending scale-out into a full exit. Until 2026-09-24 exits were a `(should_exit, reason)` tuple and every exit closed the whole position. The divergence scale-out is the only shipped user of a partial close, and it is off in every preset.

| Option                             | Code default |
|------------------------------------|--------------|
| `use_technical_exit`               | `true`       |
| `use_trendline_break`              | `true`       |
| `use_channel_break`                | `true`       |
| `use_bollinger_reject`             | `false`      |
| `use_anchored_vwap_loss`           | `true`       |
| `anchored_vwap_exit_require_two_bar_confirm` | `true` |
| `use_chart_pattern_exit`           | `false`      |
| `use_candle_pattern_exit`          | `false`      |
| `use_structure_exit`               | `true`       |
| `use_sr_loss_exit`                 | `false`      |
| `discretionary_exit_min_r`         | `0.5`        |
| `use_divergence_exit_signal`       | `false`      |
| `divergence_exit_partial_frac`     | `0.5`        |
| `divergence_exit_min_age_bars`     | `1`          |
| `divergence_exit_require_in_profit`| `true`       |
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
- `anchored_vwap_exit_require_two_bar_confirm`: require two consecutive closes through the anchored-VWAP floor (long) or ceiling (short) before the `anchored_vwap_loss_exit` / `anchored_vwap_reclaim_exit` fires. One bar through the level is ordinary noise in a trending stock. `false` fires on the first qualifying bar.
- `use_chart_pattern_exit`: allow opposing chart patterns to help trigger exits.
- `use_candle_pattern_exit`: fire `candle_pattern_exit:<pattern>` when an opposing-direction candle cluster crosses `candles.opposing_net_score_threshold` and the tape confirms (via `confirm_with_*` thresholds below). Reuses cached candle context.
- `use_structure_exit`: allow the CHoCH exit and the bias-flip structure exit. The CHoCH exit is an ungated structural stop-tightener: it fires below about -0.4R on roughly 0.5-1.3% of positions, and the 2026-09-24 study measured it neutral on 5m structure (`top_tier_adaptive`, `small_cap_squeeze`) and slightly negative on 1m structure. No candidate guard — an R floor, post-entry pivots, the hold or ORB grace — had a measurable effect, so it has none: it is exempt from the R gate, the graces and the pivot guard, and needs only a CHoCH that happened AFTER entry (`MarketStructureContext.choch_*_ts`) plus a confirming tape. Until 2026-09-24 it fired on a CHoCH up to `structure_event_lookback_bars` old, so a LONG opened just after a 1m CHoCH down exited on its first weak-tape cycle.
- `use_sr_loss_exit` (default `false`, and off in every preset): exit on a confirmed break (`broken_support` / `broken_resistance`) of a level on the adverse side of entry, with the tape confirming. Exempt from `discretionary_exit_min_r`: price is through a level beyond entry, so R < 0 by construction, and until 2026-09-24 the R gate in front of it meant it could never fire at any positive threshold. Reachable now, it ships off on the replay evidence: over 26 sessions / 235 positions it fired on 13 (all `top_tier_adaptive`) at a median -0.72R, and against today's management (the time stop, peak giveback) it cost -0.43R per firing, CI [-0.85, -0.05]; every variant tried — a 5-20 minute grace, a 0.25-0.75R loss cap, no tape confirmation — was negative too. Turning it on is in effect a tighter stop at about -0.7R.
- `discretionary_exit_min_r` (float, default `0.5`): minimum open profit, in initial-risk R units, before the **discretionary** exit families may fire — the bias-based structure exit and every technical exit (trendline / channel / bollinger / anchored-VWAP). They carry no R condition of their own — only grace windows and tape confirmation — so on a trade still hovering around entry they act as an arbitrary tightened stop. Over 2026-05-12..29 they closed 19 of 48 `top_tier_adaptive` trades at a median MFE of 0.09-0.29R for -$831 combined, and among trades whose stop sat beyond 4 ATR, 0 of 22 ever reached that stop because one of these got there first. Below the threshold the protective stop governs the trade. R is measured against `metadata['initial_stop_price']`, not the trailing `position.stop_price`, so it does not drift as management moves the stop (an option's R is on its own mark). The CHoCH exit is exempt, as it always was: it is an ungated structural stop-tightener that typically fires below about -0.4R. The S/R-break exit is exempt since 2026-09-24: for an equity it fires only on the losing side of entry, so the gate had kept it dead. `0` means no gate.
- `use_divergence_exit_signal` (off by default): scale out when a counter-direction REGULAR divergence (RSI, then OBV) forms on a held position — its latest pivot's bar closed after the entry — with its age in `[divergence_exit_min_age_bars, technical_levels.divergence_max_age_bars]`. "Counter" is against the trade's market direction, not its order side (a bull put spread watches for a bearish divergence). Hidden divergence (continuation) is never an exit trigger. `divergence_exit_partial_frac` is the share of the current quantity to close (0 = off; the slice is the floor, so a 1-lot option or 1-share position cannot scale out and the trigger is spent without an order); the rest keeps its stop and, in bracket mode, is re-protected at the broker (see **Partial closes** above). Each divergence pivot acts once, whichever indicator saw it — it is recorded in `metadata['divergence_partial_exits']` when the slice books. RSI and OBV are measured on the same price pivots, so both diverging at one pivot is one event; keyed per indicator it would close half the position and then half the rest (75% at the default fraction). `divergence_exit_require_in_profit` (default true) waits for R > 0. The reason is `divergence_partial_exit:<side>_<indicator>_age<N>b`, where the age counts session bars while session indicators are on and the clock is inside the session (see `technical_levels`); the session report lists the slices under `per_partial_exit_reason`. Configured zeros are honoured (a fraction or minimum age of 0 used to read as 0.5 / 1).
- `confirm_with_ema9`, `confirm_with_ema20`, `confirm_with_vwap`, `confirm_with_close_position`: the tape confirmation the shared exits require: the close through each enabled reference against the trade, and closing in the adverse part of its bar (the peer ladder defence reads the same EMA / VWAP references, every one that has a value). The enabled terms are ANDed. A reference with no value (a missing column, a NaN) and the close position of a zero-range bar abstain rather than veto; if every enabled term abstains the tape confirms nothing. On the first bar of the indicator session (09:30, or 07:00 for an extended-window preset) the session-reset EMAs equal the close, so that bar reads the all-hours EMAs instead, and the session VWAP — which that bar alone defines, at its own typical price — abstains. Until 2026-09-24 that bar, a zero-range bar and a NaN reference each vetoed every tape-confirmed exit in both directions.
- `bullish_close_position_max` / `bearish_close_position_min`: strict candle close-location thresholds used when confirming bearish exits from long trades or bullish exits from short trades.
- `bullish_close_position_loose_max` / `bearish_close_position_loose_min`: looser fallback thresholds for the same confirmation family.

### `events`

Scheduled-event blackouts shared by every strategy, equity and options alike. Moved out of `options:` on 2026-09-18 (the old `options.event_blackout_file` / `options.event_blackouts` keys are gone).

| Option                           | Code default               |
|----------------------------------|----------------------------|
| `enabled`                        | `true`                     |
| `blackout_file`                  | `./macro_events.auto.yaml` |
| `blackouts`                      | `[]`                       |
| `earnings_file`                  | `./earnings.yaml`          |
| `earnings`                       | `{}`                       |
| `earnings_block_sessions_before` | `1`                        |
| `earnings_block_sessions_after`  | `1`                        |

Behavior:

- `enabled`: master switch for both calendars.
- `blackout_file` / `blackouts`: macro windows (CPI, FOMC, ...) from a YAML file and/or inline; both are loaded. Each row needs `start` and `end` (HH:MM ET) and can add `enabled`, `label`, `date` or `weekday`, `symbols`, `block_new_entries` (default `true`) and `force_flatten` (default `false`). `symbols: [...]` scopes a window to those tickers; without it the window applies to every symbol. The file is re-read when its mtime changes, so a blackout can be added to a running bot.
- `earnings_file` / `earnings`: per-symbol earnings dates, `{SYMBOL: [YYYY-MM-DD, ...]}`, from a file and/or inline; both are merged.
- `earnings_block_sessions_before` / `earnings_block_sessions_after`: trading sessions either side of an earnings date that block new entries (the date itself is always blocked). Weekends are skipped; market holidays count as sessions, which errs toward blocking one day too many.

### `options`

Shared 0DTE ETF option-engine settings. Both option strategies use this block.

| Option                           | Code default                                                                                                 |
|----------------------------------|--------------------------------------------------------------------------------------------------------------|
| `enabled`                        | `true`                                                                                                       |
| `underlyings`                    | `['SPY', 'QQQ']`                                                                                             |
| `confirmation_symbols`           | `{'SPY': '$SPX', 'QQQ': '$COMPX', 'IWM': '$RUT'}`                                                            |
| `volatility_symbol`              | `VIX`                                                                                                        |
| `styles`                         | `['orb_debit_spread', 'trend_debit_spread', 'midday_credit_spread', 'orb_long_option', 'trend_long_option']` |
| `min_underlying_price`           | `100.0`                                                                                                      |
| `min_option_volume`              | `300`                                                                                                        |
| `min_open_interest`              | `600`                                                                                                        |
| `max_bid_ask_spread_pct`         | `0.1`                                                                                                        |
| `max_leg_spread_dollars`         | `0.08`                                                                                                       |
| `max_net_spread_pct`             | `0.2`                                                                                                        |
| `max_net_spread_price`           | `2.8`                                                                                                        |
| `max_net_price_frac_of_width`    | `0.9`                                                                                                        |
| `min_net_mid_price`              | `0.25`                                                                                                       |
| `target_long_delta`              | `0.38`                                                                                                       |
| `target_short_delta`             | `0.23`                                                                                                       |
| `target_single_delta`            | `0.28`                                                                                                       |
| `max_single_option_price`        | `2.25`                                                                                                       |
| `option_limit_mode`              | `mid`                                                                                                        |
| `strike_width_by_symbol`         | `{'SPY': 2.0, 'QQQ': 2.0, 'IWM': 1.0}`                                                                       |
| `max_contracts_per_trade`        | `1`                                                                                                          |
| `max_loss_per_trade`             | `200.0`                                                                                                      |
| `debit_stop_frac`                | `0.45`                                                                                                       |
| `debit_target_mult`              | `1.45`                                                                                                       |
| `credit_stop_mult`               | `1.65`                                                                                                       |
| `credit_target_frac`             | `0.32`                                                                                                       |
| `single_stop_frac`               | `0.38`                                                                                                       |
| `single_target_mult`             | `1.5`                                                                                                        |
| `force_flatten_time`             | `15:18`                                                                                                      |
| `max_vix`                        | `22.5`                                                                                                       |
| `min_vix`                        | `0.0`  (disabled by default; set ~12.0 for long-premium strategies)                                          |
| `vix_spike_pct`                  | `0.011`                                                                                                      |
| `vix_52w_low`                    | `12.0`  (used for IV-rank computation; refresh quarterly)                                                    |
| `vix_52w_high`                   | `30.0`  (used for IV-rank computation; refresh quarterly)                                                    |
| `min_iv_rank`                    | `0.0`  (disabled by default; set ~0.30 for credit-spread strategies — only sell premium when VIX is at least 30% up its 52w range) |
| `max_iv_rank`                    | `1.0`  (disabled by default; set ~0.75 for long-premium strategies — don't buy premium when VIX is in the top 25% of its 52w range) |
| `vertical_limit_mode`            | `mid`                                                                                                        |
| `quote_stability_checks`         | `3`                                                                                                          |
| `quote_stability_pause_ms`       | `500`                                                                                                        |
| `max_mid_drift_pct`              | `0.06`                                                                                                       |
| `max_quote_age_seconds`          | `6`                                                                                                          |
| `dry_run_replace_attempts`       | `2`                                                                                                          |
| `dry_run_step_frac`              | `0.25`                                                                                                       |
| `option_chain_cache_seconds`     | `6`                                                                                                          |
| `option_chain_cache_max_entries` | `24`                                                                                                         |
| `options_breakeven_enabled`      | `false`                                                                                                      |
| `options_breakeven_mark_mult`    | `1.25`                                                                                                       |
| `options_breakeven_stop_mult`    | `1.05`                                                                                                       |
| `options_profit_lock_enabled`    | `false`                                                                                                      |
| `options_profit_lock_mark_mult`  | `1.40`                                                                                                       |
| `options_profit_lock_stop_mult`  | `1.15`                                                                                                       |
| `debit_target_time_decay_enabled`     | `false`                                                                                                 |
| `debit_target_time_decay_start`       | `10:30`                                                                                                 |
| `debit_target_time_decay_end`         | `14:00`                                                                                                 |
| `debit_target_time_decay_min_scale`   | `0.70`                                                                                                  |
| `debit_stop_time_decay_widen_factor`  | `0.30`                                                                                                  |
| `delta_time_shift_enabled`       | `false`                                                                                                      |
| `delta_time_shift_per_hour`      | `0.025`                                                                                                      |
| `delta_time_shift_max`           | `0.15`                                                                                                       |
| `delta_time_shift_start`         | `10:00`                                                                                                      |
| `trend_momentum_filter_enabled`  | `false`                                                                                                      |
| `trend_min_atr_expansion`        | `0.85`                                                                                                       |
| `trend_min_volume_ratio`         | `0.90`                                                                                                       |
| `credit_distance_gate_enabled`   | `false`                                                                                                      |
| `min_credit_distance_atr`        | `1.8`                                                                                                        |
| `credit_pivot_buffer_gate_enabled` | `false`                                                                                                    |
| `min_short_strike_pivot_buffer_atr` | `1.0`                                                                                                     |
| `adaptive_width_enabled`         | `false`                                                                                                      |
| `adaptive_width_max_scale`       | `1.5`                                                                                                        |

Behavior and valid values:

- Underlying universe and symbols:
  - `enabled`: master on/off.
  - `underlyings`: list of ETF underlyings the option engine may trade. It must be a YAML list: a scalar (`underlyings: SPY`), mapping or number fails at load; `null` loads as none, which only a stock strategy accepts.
  - `confirmation_symbols`: mapping from underlying to confirmation index symbol.
  - `volatility_symbol`: symbol used as the volatility regime input.
  - `styles`: valid values are `orb_debit_spread`, `trend_debit_spread`, `midday_credit_spread`, `orb_long_option`, `trend_long_option`. The spread strategy uses the spread styles; the long-option strategy uses only `orb_long_option` and `trend_long_option` for its two directional style gates.
- Basic chain quality filters:
  - `min_underlying_price`, `min_option_volume`, `min_open_interest` filter the option universe.
  - `max_bid_ask_spread_pct`, `max_leg_spread_dollars`, `max_net_spread_pct`, `max_net_spread_price`, `min_net_mid_price` filter quote quality.
  - `max_net_price_frac_of_width` caps a vertical's net price as a fraction of its STRIKE WIDTH (default `0.9`, `0.0` disables). `max_net_spread_price` is a single dollar cap while widths differ per symbol, so it cannot police structure: at the shipped 0DTE values it sat above every configured width, which let a quote implying a credit at or above the spread itself through. Such a quote books `max_loss = width - credit = 0`. `size_option_position` refuses a zero max loss so the outcome was a silent no-trade rather than a blow-up, but a degenerate chain then looked identical to "no setup today". Applies to verticals only; single long options are validated by their own price gate.
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
    - `natural`: the side that fills now -- pay the ask when the order buys (a debit open, a credit close), take the bid when it sells (a credit open, a debit close) -- then fall back.
    - `bid`: price more defensively: the passive side to open, the natural side to close, so an exit is never parked where it cannot fill.
- Time controls:
  - `force_flatten_time`: 24-hour `HH:MM` time string used by option strategies to flatten before the close.
- Volatility guards:
  - `max_vix`: hard cap — block ALL option entries when VIX is above this level. Long premium gets too expensive (vega risk); credit spreads also less attractive vs the downside risk.
  - `min_vix` (default `0.0` = disabled): hard floor — block entries when VIX is below this level. Long-premium strategies suffer in dead-grind tape where the typical daily range can't overcome theta + commissions. Set to `~12.0` for long-call/long-put strategies; leave at `0.0` for credit-spread strategies (which benefit from low VIX).
  - `vix_spike_pct`: block when VIX is moving fast (mid-session repricing risk).
  - **IV-rank gate** (`vix_52w_low` / `vix_52w_high` / `min_iv_rank` / `max_iv_rank`): computes `iv_rank = (vix_last − vix_52w_low) / (vix_52w_high − vix_52w_low)` clamped to `[0,1]` and blocks entries outside `[min_iv_rank, max_iv_rank]`. Normalizes against the rolling 52-week range rather than absolute VIX level — VIX 20 might be "high" in a 12-18 regime but "low" in a 25-35 regime. Long-premium strategies should cap `max_iv_rank: 0.75` (don't buy when in top 25% — IV expensive); credit-spread strategies should floor `min_iv_rank: 0.30` (don't sell when in bottom 30% — credits skinny). The `vix_52w_low` / `vix_52w_high` values are user-supplied (refresh quarterly) — no live fetching, deterministic, no extra API calls.
- Quote-stability checks:
  - `quote_stability_checks`, `quote_stability_pause_ms`, `max_mid_drift_pct`, `max_quote_age_seconds`
  - These make the bot re-check quotes before finalizing a trade.
- Dry-run replace controls:
  - `dry_run_replace_attempts`, `dry_run_step_frac`
- Macro-event blackouts are configured in the top-level [`events`](#events) block, which both option strategies read.
- Chain cache:
  - `option_chain_cache_seconds`, `option_chain_cache_max_entries`
  - 0DTE strategies (`zero_dte_etf_options`, `zero_dte_etf_long_options`) parallel-prefetch chains for all qualifying candidates at the start of each `entry_signals` pass; the sequential per-candidate build loop then hits the warm cache. Size `option_chain_cache_max_entries` ≥ the number of underlyings you trade so prefetched chains aren't evicted before consumption.
- Premium ratchet (post-entry stop management). All four premium-ratchet families are off by default; enable the ones you want active.
  - `options_breakeven_enabled` / `options_breakeven_mark_mult` / `options_breakeven_stop_mult`: when the option mark crosses `entry × options_breakeven_mark_mult`, ratchet the stop up to `entry × options_breakeven_stop_mult`. Locks a small protective gain on debit trades that go through their first push.
  - `options_profit_lock_enabled` / `options_profit_lock_mark_mult` / `options_profit_lock_stop_mult`: a second, looser ratchet that activates at a higher mark multiple and locks a larger fraction of the move. Stacks with `options_breakeven_*`.
- Time-decay-aware stop/target scaling for debit trades:
  - `debit_target_time_decay_enabled`: master toggle. When `true`, the debit target shrinks linearly between `debit_target_time_decay_start` and `debit_target_time_decay_end` (HH:MM ET), and the debit stop widens proportionally so theta-decayed trades aren't stopped on noise.
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
- Credit pivot-buffer gate (applies to `midday_credit_spread`; added 2026-05-21):
  - `credit_pivot_buffer_gate_enabled`: when `true`, requires the short strike to sit at least `min_short_strike_pivot_buffer_atr × ATR` beyond the outermost recent market-structure pivot: the higher of the LTF/HTF `reference_high` for a bear call, the lower of the LTF/HTF `reference_low` for a bull put, read from `_regime_confirm`'s `regime['metrics']` (stamped on the signal as `regime_metrics`). It skips when the references or the ATR are unavailable. Catches the "short sitting ON the pivot" failure mode the distance gate above misses — the distance gate measures from current spot, which can pass even when the short is at a recent high. The refusal is `midday_credit_spread_unavailable(reason=short_strike_too_close_to_pivot(...))`.
  - `min_short_strike_pivot_buffer_atr`: ATR multiple defining the minimum cushion from the pivot.
  - **Fixed 2026-09-25; ships disabled in every preset.** Until then the gate read the pivots from the top level of the regime result, found none and never fired, although `config.zero_dte_etf_options.yaml` shipped it `true`. That preset now ships `false`, so nothing changes. Enabled, it would refuse about 32 of 38 credit entries in the fixture replay and 4 of the 8 live ones (2026-05-20..22), so switch it on only after a dry-run.
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

- When enabled, an overextended continuation entry can shift from **enter now** to **wait for a same-direction LTF FVG retest**.
- Larger distance thresholds make the bot accept looser FVG retests.
- Higher `anti_chase_fvg_retest_min_close_position` requires a stronger reclaim candle on the retest.
- `anti_chase_fvg_retest_stop_buffer_gap_frac` controls how tight the stop anchors around the defended FVG.
- Since 2026-09-24 the shared entry stage runs the retest (`shared_entry`, step 3) for the strategy's deferrable reasons, and the anchored stop is held at least `shared_entry.min_stop_atr_mult` ATR from entry. The FVG plan needs `shared_entry.use_fvg_context`.

### Stock FVG confluence

Used by all stock strategies.

- `htf_fvg_entry_weight`
- `ltf_fvg_entry_weight`
- `opposing_fvg_entry_penalty_mult`
- `fvg_runner_rr_bonus`

Behavior:

- Higher `htf_fvg_entry_weight` or `ltf_fvg_entry_weight` makes same-direction FVG context matter more.
- Higher `opposing_fvg_entry_penalty_mult` makes nearby opposing FVGs more punitive.
- `fvg_runner_rr_bonus` adds extra room to stronger trades when FVG continuation context is favorable.
- The shared entry stage computes the FVG term for every admitted entry (`fvg_entry_adjustment`, part of `shared_context_score`); `shared_entry.use_fvg_context: false` switches it, and the FVG runner bias, off.

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
| `ltf_fvg_entry_weight`                     | `0.28`                          |
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
| `ltf_fvg_entry_weight`                     | `0.32`                          |
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
| `min_rvol`                                        | `1.0`                           |
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
| `target_rr`                                       | `1.95`                          |
| `runner_enabled`                                  | `True`                          |
| `runner_target_rr`                                | `2.6`                           |
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
| `ltf_fvg_entry_weight`                     | `0.24`                          |
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
| `ltf_fvg_entry_weight`     | `0.2`                           |
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
| `min_rvol`                        | `2.5` (preset: `3.0`)           |
| `max_pullback_from_high`          | `0.05`                          |
| `min_reversal_close_position`     | `0.6`                           |
| `require_positive_reversal_ret5`  | `True`                          |
| `htf_fvg_entry_weight`            | `0.38`                          |
| `ltf_fvg_entry_weight`     | `0.24`                          |
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

The traded leg meets the shared entry stage on its own frame (style and family `pairs`); the reference symbol is used only for the z-score. The manifest turns divergence-only entries off (`capabilities.shared_entry.divergence_entry: false`). See the strategy README.

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
| `ltf_fvg_entry_weight`     | `0.16`                          |
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
| `ltf_fvg_entry_weight`                     | `0.28`                          |
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
  - `htf_minutes`, `htf_lookback_days`, `htf_pivot_span`, `htf_max_levels_per_side`, `htf_atr_tolerance_mult`, `htf_pct_tolerance`, `htf_stop_buffer_atr_mult`, `htf_ema_fast_span`, `htf_ema_slow_span`
- Trigger frame and warmup:
  - `ltf_minutes`, `min_bars`, `min_ltf_bars`
- Continuation scoring and pullback quality:
  - `min_total_score`, `min_ltf_score`, `min_adx14`
  - `min_pullback_bars`, `max_pullback_bars`, `max_pullback_depth_atr`, `pullback_hold_atr`, `max_countertrend_volume_ratio`
- Re-expansion trigger detail:
  - `breakout_buffer_pct`, `min_ltf_close_position`, `min_ltf_volume_ratio`
- Anti-chase / extension controls:
  - `max_extension_from_vwap_atr`, `max_extension_from_ema9_atr`, `extension_penalty_per_atr`, `extension_hard_cap_mult`
  - past `max_extension_from_*_atr` the score pays `extension_penalty_per_atr` per ATR; past that times `extension_hard_cap_mult` the side is refused as `too_extended_hard_cap_vwap_atr` / `too_extended_hard_cap_ema9_atr` (before 2026-09-26 it reused the base exhaustion filter's `too_extended_from_*_atr`, which `max_entry_*_extension_atr` still drives)
- Peer and macro confirmation:
  - `min_peer_agreement`, `min_peer_score`
  - `enable_macro_confirmation`, `require_macro_agreement_count`, `dollar_symbol`, `bond_symbol`, `volatility_symbol`
- R:R and adaptive management:
  - `min_rr`, `target_rr`, `stop_buffer_atr_mult`
  - `strong_setup_runner_enabled`, `adaptive_breakeven_rr`, `adaptive_profit_lock_rr`, `adaptive_profit_lock_stop_rr`, `adaptive_runner_trigger_rr`
- Context overlays:
  - `htf_fvg_entry_weight`, `ltf_fvg_entry_weight`, `opposing_fvg_entry_penalty_mult`, `fvg_runner_rr_bonus`
  - the S/R veto is `shared_entry.use_sr_filter` (off in the preset, so the strategy does not hard-block on S/R proximity); `use_sr_veto` was retired on 2026-09-24 and a preset still carrying it fails at load

Also uses these shared stock groups:

- force-flatten
- stock FVG confluence
- adaptive stock trade management

Current package defaults:

| Option                            | Current package default                   |
|-----------------------------------|-------------------------------------------|
| `tradable`                        | `['AAPL', 'NVDA', 'GOOG', 'AMD', 'INTC']` |
| `peers`                           | `['QQQ', 'AVGO', 'MU', 'TSM']`            |
| `ltf_minutes`       | `5`                                       |
| `min_bars`                        | `85`                                      |
| `min_ltf_bars`                | `18`                                      |
| `htf_minutes`           | `60`                                      |
| `htf_lookback_days`               | `60`                                      |
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
| `min_ltf_score`               | `2.5`                                     |
| `min_adx14`                       | `13.5`                                    |
| `max_pullback_bars`               | `6`                                       |
| `min_pullback_bars`               | `2`                                       |
| `max_pullback_depth_atr`          | `1.05`                                    |
| `pullback_hold_atr`               | `0.38`                                    |
| `max_countertrend_volume_ratio`   | `1.28`                                    |
| `breakout_buffer_pct`             | `0.0007`                                  |
| `min_ltf_close_position`      | `0.58`                                    |
| `min_ltf_volume_ratio`        | `1.02`                                    |
| `max_extension_from_vwap_atr`     | `1.05`                                    |
| `max_extension_from_ema9_atr`     | `0.88`                                    |
| `min_rr`                          | `1.8`                                     |
| `target_rr`                       | `2.05`                                    |
| `stop_buffer_atr_mult`            | `0.5`                                     |
| `strong_setup_runner_enabled`     | `True`                                    |
| `adaptive_breakeven_rr`           | `0.92`                                    |
| `adaptive_profit_lock_rr`         | `1.2`                                     |
| `adaptive_profit_lock_stop_rr`    | `0.34`                                    |
| `adaptive_runner_trigger_rr`      | `1.12`                                    |
| `htf_fvg_entry_weight`            | `0.34`                                    |
| `ltf_fvg_entry_weight`     | `0.16`                                    |
| `opposing_fvg_entry_penalty_mult` | `1.0`                                     |
| `fvg_runner_rr_bonus`             | `0.12`                                    |
| `activity_score_weight`           | `0.12`                                    |
| `macro_bonus`                     | `0.7`                                     |
| `macro_miss_penalty`              | `0.3`                                     |
| `extension_penalty_per_atr`       | `0.72`                                    |
| `extension_hard_cap_mult`         | `1.45`                                    |
| `force_flatten`                   | `{long: true, short: true}` (preset: both `false`) |

### `peer_confirmed_key_levels`

Purpose: trade around HTF key levels/zones only when a tradable symbol, its peer basket, and optional macro symbols agree strongly enough, then ride the cleaned S/R ladder while price action still defends the last reclaimed/broken rung.

Default windows:

- `entry_windows`: `[['07:10', '15:15']]`
- `management_windows`: `[['07:00', '15:58']]`
- `screener_windows`: `[['07:00', '15:35']]`

Strategy-specific knobs:

- Universe and HTF map:
  - `tradable`, `peers`
  - `htf_minutes`, `htf_lookback_days`, `htf_pivot_span`, `htf_max_levels_per_side`, `htf_atr_tolerance_mult`, `htf_pct_tolerance`, `htf_stop_buffer_atr_mult`, `htf_ema_fast_span`, `htf_ema_slow_span`
- Trigger frame and warmup:
  - `ltf_minutes`, `min_bars`, `min_ltf_bars`
  - Stronger signals are prioritized lexicographically by trigger quality, level quality, peer confirmation, vote edge, and clearance before smaller additive bonuses are allowed to break ties.
- Zone, score, and R:R:
  - `zone_atr_mult`, `zone_pct`, `min_level_score`, `min_ltf_score`, `min_rr`, `stop_buffer_atr_mult`
  - `require_peer_target_clearance` (default `True`): the nearest HTF key level in the trade's direction must clear BOTH `support_resistance.entry_min_clearance_pct` and `_atr`, else `too_close_to_overhead_resistance` / `too_close_to_nearby_support`. It rode on `shared_entry.use_sr_filter` until 2026-09-24; that knob is the shared S/R veto now
  - `ltf_quality_bonus_enabled`, `ltf_quality_max_bonus`, `ltf_reclaim_quality_bonus_cap`, `ltf_zone_interaction_bonus_cap`, `ltf_candle_quality_bonus_cap`, `ltf_volume_quality_bonus_cap`, `ltf_range_expansion_bonus_cap`
- Peer confirmation:
  - `min_peer_agreement`, `min_peer_score`
- Macro confirmation:
  - `enable_macro_confirmation`, `require_macro_agreement_count`, `dollar_symbol`, `bond_symbol`, `volatility_symbol`
- Level scoring detail:
  - `level_round_number_tolerance_pct`
- Strong-setup runner / ladder logic:
  - `strong_setup_runner_enabled`, `strong_setup_min_ltf_score`, `strong_setup_min_level_score`, `strong_setup_min_peer_score`, `strong_setup_min_htf_vote_edge`, `strong_setup_target_level_offset`
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
| `htf_minutes`              | `60`                                      |
| `htf_lookback_days`                  | `60`                                      |
| `htf_pivot_span`                     | `2`                                       |
| `htf_max_levels_per_side`            | `6`                                       |
| `htf_atr_tolerance_mult`             | `0.35`                                    |
| `htf_pct_tolerance`                  | `0.003`                                   |
| `htf_stop_buffer_atr_mult`           | `0.25`                                    |
| `htf_ema_fast_span`                  | `34`                                      |
| `htf_ema_slow_span`                  | `200`                                     |
| `ltf_minutes`          | `5`                                       |
| `min_bars`                           | `80`                                      |
| `min_ltf_bars`                   | `18`                                      |
| `zone_atr_mult`                      | `0.22`                                    |
| `zone_pct`                           | `0.0016`                                  |
| `min_level_score`                    | `2.9`                                     |
| `min_ltf_score`                  | `2.5`                                     |
| `ltf_quality_bonus_enabled`      | `True`                                    |
| `ltf_quality_max_bonus`          | `2.0`                                     |
| `ltf_reclaim_quality_bonus_cap`  | `0.8`                                     |
| `ltf_zone_interaction_bonus_cap` | `0.5`                                     |
| `ltf_candle_quality_bonus_cap`   | `0.5`                                     |
| `ltf_volume_quality_bonus_cap`   | `0.4`                                     |
| `ltf_range_expansion_bonus_cap`  | `0.4`                                     |
| `min_rr`                             | `1.75`                                    |
| `stop_buffer_atr_mult`               | `0.68`                                    |
| `min_peer_agreement`                 | `2`                                       |
| `min_peer_score`                     | `2`                                       |
| `enable_macro_confirmation`          | `True`                                    |
| `require_macro_agreement_count`      | `1`                                       |
| `require_peer_target_clearance`      | `True`                                    |
| `dollar_symbol`                      | `'NYICDX'`                                |
| `bond_symbol`                        | `'TLT'`                                   |
| `volatility_symbol`                  | `'VIX'`                                   |
| `level_round_number_tolerance_pct`   | `0.002`                                   |
| `strong_setup_runner_enabled`        | `True`                                    |
| `strong_setup_min_ltf_score`     | `3.2`                                     |
| `strong_setup_min_level_score`       | `3.4`                                     |
| `strong_setup_min_peer_score`        | `2`                                       |
| `strong_setup_min_htf_vote_edge`  | `1`                                       |
| `strong_setup_target_level_offset`   | `1`                                       |
| `activity_score_weight`              | `0.11`                                    |
| `htf_fvg_entry_weight`               | `0.36`                                    |
| `ltf_fvg_entry_weight`        | `0.2`                                     |
| `opposing_fvg_entry_penalty_mult`    | `1.0`                                     |
| `fvg_runner_rr_bonus`                | `0.14`                                    |
| `adaptive_breakeven_rr`              | `0.9`                                     |
| `adaptive_profit_lock_rr`            | `1.25`                                    |
| `adaptive_profit_lock_stop_rr`       | `0.32`                                    |
| `adaptive_runner_trigger_rr`         | `1.12`                                    |
| `force_flatten`                      | `{long: true, short: true}` (preset: long `false`, short `true`) |

### `peer_confirmed_key_levels_1m`

Purpose: 1-minute LTF peer-confirmed HTF key-level/zone strategy variant that keeps the same HTF map and peer/macro confirmation framework as `peer_confirmed_key_levels`, but is now tuned as a compromise between aggressive and balanced confirmation so entries can form earlier without using the older looser gates.

Default windows:

- `entry_windows`: `[['07:05', '15:20']]`
- `management_windows`: `[['07:00', '15:58']]`
- `screener_windows`: `[['07:00', '15:40']]`

Key differences vs the 5-minute base strategy:

- `ltf_minutes: 1`
- deeper LTF warmup with `min_bars: 90` and `min_ltf_bars: 45`
- compromise 1-minute LTF gates that are still faster than the 5-minute base but no longer use the older aggressive thresholds: `min_level_score: 2.5`, `min_rr: 1.6`, `min_peer_agreement: 2`, `min_peer_score: 2`
- tighter LTF zone sizing and slightly faster adaptive management to suit 1-minute execution while keeping confirmation more balanced
- heavier weighting on LTF FVG participation while still retaining the HTF map and macro confirmation checks
- inherits the base strategy's capped LTF-quality bonus layer so clean 1-minute reclaims / rejects can outrank weaker touches without raising `min_ltf_score`

Use `configs/config.peer_confirmed_key_levels_1m.yaml` for the shipped full preset.

Current package defaults:

| Option                               | Current package default                   |
|--------------------------------------|-------------------------------------------|
| `tradable`                           | `['AAPL', 'NVDA', 'GOOG', 'AMD', 'INTC']` |
| `peers`                              | `['QQQ', 'AVGO', 'MU', 'TSM']`            |
| `htf_minutes`              | `60`                                      |
| `htf_lookback_days`                  | `60`                                      |
| `htf_pivot_span`                     | `2`                                       |
| `htf_max_levels_per_side`            | `6`                                       |
| `htf_atr_tolerance_mult`             | `0.35`                                    |
| `htf_pct_tolerance`                  | `0.003`                                   |
| `htf_stop_buffer_atr_mult`           | `0.25`                                    |
| `htf_ema_fast_span`                  | `34`                                      |
| `htf_ema_slow_span`                  | `200`                                     |
| `ltf_minutes`          | `1`                                       |
| `min_bars`                           | `100`                                     |
| `min_ltf_bars`                   | `55`                                      |
| `zone_atr_mult`                      | `0.17`                                    |
| `zone_pct`                           | `0.0013`                                  |
| `min_level_score`                    | `2.5`                                     |
| `min_ltf_score`                  | `2.4`                                     |
| `ltf_quality_bonus_enabled`      | `True`                                    |
| `ltf_quality_max_bonus`          | `2.0`                                     |
| `ltf_reclaim_quality_bonus_cap`  | `0.8`                                     |
| `ltf_zone_interaction_bonus_cap` | `0.5`                                     |
| `ltf_candle_quality_bonus_cap`   | `0.5`                                     |
| `ltf_volume_quality_bonus_cap`   | `0.4`                                     |
| `ltf_range_expansion_bonus_cap`  | `0.4`                                     |
| `min_rr`                             | `1.6`                                     |
| `stop_buffer_atr_mult`               | `0.56`                                    |
| `min_peer_agreement`                 | `2`                                       |
| `min_peer_score`                     | `2`                                       |
| `enable_macro_confirmation`          | `True`                                    |
| `require_macro_agreement_count`      | `1`                                       |
| `require_peer_target_clearance`      | `True`                                    |
| `dollar_symbol`                      | `'NYICDX'`                                |
| `bond_symbol`                        | `'TLT'`                                   |
| `volatility_symbol`                  | `'VIX'`                                   |
| `level_round_number_tolerance_pct`   | `0.002`                                   |
| `strong_setup_runner_enabled`        | `True`                                    |
| `strong_setup_min_ltf_score`     | `3.0`                                     |
| `strong_setup_min_level_score`       | `3.0`                                     |
| `strong_setup_min_peer_score`        | `2`                                       |
| `strong_setup_min_htf_vote_edge`  | `1`                                       |
| `strong_setup_target_level_offset`   | `1`                                       |
| `activity_score_weight`              | `0.12`                                    |
| `htf_fvg_entry_weight`               | `0.32`                                    |
| `ltf_fvg_entry_weight`        | `0.26`                                    |
| `opposing_fvg_entry_penalty_mult`    | `1.0`                                     |
| `fvg_runner_rr_bonus`                | `0.14`                                    |
| `adaptive_breakeven_rr`              | `0.82`                                    |
| `adaptive_profit_lock_rr`            | `1.08`                                    |
| `adaptive_profit_lock_stop_rr`       | `0.28`                                    |
| `adaptive_runner_trigger_rr`         | `1.02`                                    |
| `force_flatten`                      | `{long: true, short: true}` (preset: long `false`, short `true`) |

### `peer_confirmed_htf_pivots`

Purpose: trade around HTF support/resistance pivot battlegrounds instead of generic HTF key-level votes. The strategy can enter in reclaim, rejection, or continuation mode, but it is now explicitly tuned as an S/R scalp strategy that prefers longs around support, shorts around resistance, and uses the next opposing S/R level as the first target reference.

Default windows:

- `entry_windows`: `[['09:35', '11:15'], ['13:00', '14:45']]`
- `management_windows`: `[['09:01', '15:55']]`
- `screener_windows`: `[['09:01', '11:25'], ['12:45', '15:55']]`

Strategy-specific knobs:

- Universe and HTF pivot map:
  - `tradable`, `peers`
  - `htf_minutes`, `htf_lookback_days`, `htf_pivot_span`, `htf_max_levels_per_side`, `htf_atr_tolerance_mult`, `htf_pct_tolerance`, `htf_stop_buffer_atr_mult`, `htf_ema_fast_span`, `htf_ema_slow_span`
- Trigger frame and warmup:
  - `ltf_minutes`, `min_bars`, `min_ltf_bars`
- Entry-family selection:
  - `entry_family` with `auto`, `pivot_reclaim`, `pivot_rejection`, and `pivot_continuation`
- Regime / trigger scoring:
  - `min_regime_score`, `min_ltf_score`, `min_total_score`, `min_peer_agreement`, `min_peer_score`
  - `enable_macro_confirmation`, `require_macro_agreement_count`, `dollar_symbol`, `bond_symbol`, `volatility_symbol`
- Pivot-zone sizing and family detail:
  - `pivot_zone_atr_mult`, `pivot_zone_pct`
  - `pivot_reclaim_buffer_pct`, `pivot_reclaim_zone_frac`
  - `pivot_rejection_min_wick_frac`, `pivot_rejection_allows_neutral_ltf_structure`
  - `pivot_continuation_breakout_buffer_pct`, `pivot_continuation_interaction_lookback_bars`, `pivot_continuation_max_distance_atr`
- Trigger quality and anti-chase:
  - `min_ltf_close_position`, `min_ltf_volume_ratio`, `min_adx14`
  - `max_reclaim_distance_from_pivot_atr`, `max_rejection_distance_from_pivot_atr`, `max_continuation_distance_from_pivot_atr`
  - `entry_exhaustion_filter_enabled`, `max_entry_vwap_extension_atr`, `max_entry_ema9_extension_atr`, `max_entry_bar_range_atr`, `max_entry_upper_wick_frac`, `max_entry_lower_wick_frac`
  - the S/R veto is `shared_entry.use_sr_filter` (off in the preset, so the strategy stays anchored to the HTF pivot model); `use_sr_veto` was retired on 2026-09-24 and a preset still carrying it fails at load. The structure veto (`shared_entry.use_structure_filter`) exempts `pivot_rejection` through the manifest's `capabilities.shared_entry.exemptions`
- R:R and adaptive management:
  - `min_rr`, `target_rr`, `stop_buffer_atr_mult`
  - `strong_setup_runner_enabled`, `adaptive_breakeven_rr`, `adaptive_profit_lock_rr`, `adaptive_profit_lock_stop_rr`, `adaptive_runner_trigger_rr`
- Context overlays:
  - `htf_fvg_entry_weight`, `ltf_fvg_entry_weight`, `opposing_fvg_entry_penalty_mult`, `fvg_runner_rr_bonus`

Also uses these shared stock groups:

- force-flatten
- stock FVG confluence
- adaptive stock trade management

Current package defaults:

| Option                                         | Current package default                         |
|------------------------------------------------|-------------------------------------------------|
| `tradable`                                     | `['AAPL', 'NVDA', 'GOOG', 'AMD', 'INTC', 'MU']` |
| `peers`                                        | `['QQQ', 'AVGO', 'TSM']`                        |
| `ltf_minutes`                    | `5`                                             |
| `min_bars`                                     | `90`                                            |
| `min_ltf_bars`                             | `20`                                            |
| `htf_minutes`                        | `60`                                            |
| `htf_lookback_days`                            | `60`                                            |
| `htf_pivot_span`                               | `2`                                             |
| `htf_max_levels_per_side`                      | `6`                                             |
| `htf_atr_tolerance_mult`                       | `0.35`                                          |
| `htf_pct_tolerance`                            | `0.003`                                         |
| `htf_stop_buffer_atr_mult`                     | `0.25`                                          |
| `htf_ema_fast_span`                            | `34`                                            |
| `htf_ema_slow_span`                            | `200`                                           |
| `entry_family`                                 | `'auto'`                                        |
| `min_regime_score`                             | `4`                                             |
| `min_ltf_score`                            | `2.5`                                           |
| `min_total_score`                              | `5`                                             |
| `min_peer_agreement`                           | `2`                                             |
| `min_peer_score`                               | `2`                                             |
| `enable_macro_confirmation`                    | `True`                                          |
| `require_macro_agreement_count`                | `1`                                             |
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
| `min_ltf_close_position`                   | `0.6`                                           |
| `min_ltf_volume_ratio`                     | `1.0`                                           |
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
| `stop_buffer_atr_mult`                         | `0.5`                                           |
| `strong_setup_runner_enabled`                  | `True`                                          |
| `adaptive_breakeven_rr`                        | `0.9`                                           |
| `adaptive_profit_lock_rr`                      | `1.18`                                          |
| `adaptive_profit_lock_stop_rr`                 | `0.32`                                          |
| `adaptive_runner_trigger_rr`                   | `1.1`                                           |
| `htf_fvg_entry_weight`                         | `0.28`                                          |
| `ltf_fvg_entry_weight`                  | `0.14`                                          |
| `opposing_fvg_entry_penalty_mult`              | `1.0`                                           |
| `fvg_runner_rr_bonus`                          | `0.1`                                           |
| `activity_score_weight`                        | `0.1`                                           |
| `macro_bonus`                                  | `0.75`                                          |
| `macro_miss_penalty`                           | `0.28`                                          |
| `force_flatten`                                | `{'long': True, 'short': True}`                 |

### `top_tier_adaptive`

Purpose: multi-regime adaptive strategy for a fixed universe of 25 mega-cap Tech / AI stocks in three co-movement groups: `ai_hardware` (NVDA, AVGO, AMD, TSM, MU, QCOM, ARM, MRVL, INTC, ANET, VRT, DELL), `platforms` (AAPL, MSFT, GOOG, AMZN, META, NFLX, ORCL, TSLA) and `software` (CRM, ADBE, NOW, PLTR, PANW). Eight regimes compete in a flat score-ordered build queue: trend, pullback, range, vol_squeeze, momentum, sr_scalp, vwap_reclaim, and orb (which alone owns the ORB window) — each independently togglable via `disable_*_regime` knobs.

Default windows:

- `entry_windows`: `[["09:45", "15:00"]]` (the shipped preset opens at `09:35`: it runs with `disable_orb_regime: true`, so there is no 09:30-09:45 opening-range carve-out to wait out)
- `management_windows`: `[["09:30", "15:55"]]`
- `screener_windows`: `[["09:30", "15:00"]]`

Strategy-specific knobs:

- `tradable`: the fixed list of symbols to trade.
- `index_symbols`: index ETFs streamed for directional confirmation — `SMH`, `IGV`, `XLK`, one per group. Every ETF referenced by `sector_index_map` must be listed here so its bars are streamed. Pair with `sector_index_map` (below) to control which ETFs confirm which symbols.
- `sector_index_map`: group name (matches `sector_groups` keys) → list of index ETFs to consult when confirming trades on symbols in that group. Default `ai_hardware: [SMH]`, `platforms: [XLK]`, `software: [IGV]`. OR semantics across a list. Every group has at least `index_breadth_min_peers` members, so peer breadth is the live confirmation path and the ETF is consulted only when peer bars are missing. A group with no entry falls back to OR-ing across the whole `index_symbols` list.
- `require_index_confirmation`: gate trend/pullback/vol_squeeze/momentum/vwap_reclaim entries on index agreement. Range and sr_scalp are exempt (mean-reversion theses); orb is exempt too (its range break is the directional proof).
- `min_trend_score` / `min_pullback_score` / `min_range_score` / `min_vol_squeeze_score` / `min_momentum_score` / `min_sr_scalp_score`: minimum regime score to qualify.
- `htf_ema_fast_span` / `htf_ema_slow_span` (default `50` / `200`, on the `htf_minutes` bars): the HTF context's EMAs, so the HTF EMA trend below, the HTF chart's lines and the sidebar trend.
- `require_htf_ema_alignment` (default `disabled`; `enabled` / `true`, `long_only`, `short_only`, `disabled` / `false`) / `htf_ema_alignment_score` (default `0.0`): the HTF EMA trend -- close vs the fast EMA, fast vs slow, the HTF context's trend bias, 2 of 3 -- as a gate against entries opposing it on the sides the mode names (`htf_ema_trend_<bias>`) and as a score added to (aligned) or taken from (opposed) every scored regime of the side before it has to clear its floor. Both follow the ORB-window HTF bypass. Shipped off: an archived-session study found a long-side edge but not a short-side one (the case for `long_only`), and realized trades ran the other way.
- `min_pullback_trend_score`: minimum trend score required before pullback scoring begins.
- `trend_target_rr` / `pullback_target_rr` / `vol_squeeze_target_rr` / `momentum_target_rr`: initial R:R targets per regime. Range and sr_scalp have no R:R target — range targets the opposite edge of the range, sr_scalp the inner edge of the opposite HTF zone.
- `sr_scalp_min_distance_pct` / `sr_scalp_min_distance_atr`: HTF zone-gap floors for the sr_scalp regime (defaults `0.008` = 0.8% and `2.5` = 2.5x ATR). The inner gap between HS and HR zones must clear BOTH (max wins).
- `sr_scalp_max_distance_from_zone_atr`: sr_scalp proximity gate — close must be inside the entry-side zone OR within this multiple of ATR of its inner edge (default `0.5`).
- `orb_end_time` / `midday_start_time` / `midday_end_time` / `afternoon_start_time` / `no_new_entries_after`: time-of-day regime window boundaries (all eight regimes use these — no hard-coded times).
- `disable_trend_regime` / `disable_pullback_regime` / `disable_range_regime` / `disable_vol_squeeze_regime` / `disable_momentum_regime` / `disable_sr_scalp_regime` / `disable_vwap_reclaim_regime` / `disable_orb_regime`: per-regime opt-out flags (all default `false`). `disable_orb_regime` also removes the 09:30 → opening-range-end no-entry zone, so the open trades the normal mix.
- `disable_orb_window`: whole-window opt-out for the ORB window, opening-range end (09:30 + `orb_range_minutes`) → `orb_end_time` (default `false`). Different from the surviving `orb_bypass_*` flags (`htf_bias`, `exhaustion`, `screener_bias`, `relative_strength`, `side_decision`) and the manifest's ORB structure / S/R exemption, which loosen filters within the window — this skips it entirely.
- `sector_groups`: co-movement groupings - ETF routing (`sector_index_map`) and the peer list for breadth confirmation.
- `correlation_groups`: coarser risk groupings for the concentration guard (mega caps across tech/communication/consumer-discretionary trade as one beta book, so they share one bucket).
- `max_same_correlation_group_same_direction`: max same-direction positions per correlation group.

Also uses these shared stock groups:

- force-flatten (configurable per side)
- entry exhaustion filters
- stock FVG confluence
- adaptive stock trade management
- the shared entry stage (every `shared_entry` knob; the preset runs structure, S/R and chart vetoes, both refinements and every score term) and the shared exit families (`shared_exit`)

Every regime's candidate passes top_tier's own gates (Fix D, stretched / technical bias, ORB 5m follow-through, HTF bias / pivot, HTF EMA, the ORB opposing-level block) and then the shared entry stage, with style and family = the regime. The manifest exempts the `orb` regime from the structure and S/R vetoes (`capabilities.shared_entry.exemptions: {orb: [structure, sr]}`, replacing `orb_bypass_structure_entry` / `orb_bypass_sr_entry`). Since 2026-09-25 (user decisions) it also exempts `range`, `pullback` and `sr_scalp` from the structure veto and `vwap_reclaim` from the S/R veto; `small_cap_squeeze`'s manifest carries the first three but keeps the S/R veto on vwap_reclaim (see `shared_entry`). The broken-level guard is the shared `shared_entry.use_broken_level_guard`, off in the preset. The slot auction ranks on `regime_score_normalized` + 1.0 x `shared_context_score` x `regime_rank_unit` (see `shared_entry`). A refusal's payload carries every blocker, but top_tier's decision log still records one reason per (side, regime) attempt. See the strategy README, section 6.

Current code defaults:

| Option                            | Default                                                                                |
|-----------------------------------|----------------------------------------------------------------------------------------|
| `tradable`                        | `AAPL, MSFT, GOOG, AMZN, META, NFLX, ORCL, TSLA, NVDA, AVGO, AMD, TSM, MU, QCOM, ARM, MRVL, INTC, ANET, VRT, DELL, CRM, ADBE, NOW, PLTR, PANW` |
| `index_symbols`                   | `SMH, IGV, XLK`                                                                        |
| `sector_index_map`                | `{ai_hardware: [SMH], platforms: [XLK], software: [IGV]}`                              |
| `require_index_confirmation`      | `true`                                                                                 |
| `min_bars`                        | `150`                                                                                  |
| `ltf_minutes`       | `1`                                                                                    |
| `htf_minutes`           | `15`                                                                                   |
| `min_ltf_bars`                | `120`                                                                                  |
| `min_trend_score`                 | `3.5`                                                                                  |
| `min_pullback_score`              | `3.5`                                                                                  |
| `min_pullback_trend_score`        | `3.0`                                                                                  |
| `min_range_score`                 | `4.0`                                                                                  |
| `min_vol_squeeze_score`           | `4.0`                                                                                  |
| `min_momentum_score`              | `4.0`                                                                                  |
| `min_sr_scalp_score`              | `3.0`                                                                                  |
| `sr_scalp_min_distance_pct`       | `0.008`                                                                                |
| `sr_scalp_min_distance_atr`       | `2.5`                                                                                  |
| `sr_scalp_max_distance_from_zone_atr` | `0.5`                                                                              |
| `min_adx14`                       | `15.0`                                                                                 |
| `trend_target_rr`                 | `2.0`                                                                                  |
| `pullback_target_rr`              | `2.0`                                                                                  |
| `vol_squeeze_target_rr`           | `2.05`                                                                                 |
| `momentum_target_rr`              | `2.0`                                                                                  |
| `disable_orb_window`              | `false`                                                                                |
| `disable_trend_regime`            | `false`                                                                                |
| `disable_pullback_regime`         | `false`                                                                                |
| `disable_range_regime`            | `false`                                                                                |
| `disable_vol_squeeze_regime`      | `false`                                                                                |
| `disable_momentum_regime`         | `false`                                                                                |
| `disable_sr_scalp_regime`         | `false`                                                                                |
| `stop_buffer_atr_mult`            | `0.25`                                                                                 |
| `orb_end_time`                    | `10:05`                                                                                |
| `midday_start_time`               | `11:30`                                                                                |
| `midday_end_time`                 | `13:00`                                                                                |
| `afternoon_start_time`            | `13:00`                                                                                |
| `no_new_entries_after`            | `15:00`                                                                                |
| `max_same_correlation_group_same_direction` | `2`                                                                      |
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
- Every style meets the shared entry stage as a premium proposal on the underlying, in the underlying's market direction (a bull put credit spread is LONG), before the option chain is read. The vetoes are the `shared_entry` knobs (the preset runs the structure veto). `midday_credit_spread` is exempt from the structure veto through the manifest, and ranking is on `strategy_priority_score`. See the strategy README's "Shared entry stage (2026-09-24)" section.

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
| `htf_minutes`       | `15`                    |
| `htf_lookback_days`           | `15`                    |
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
- Both styles meet the shared entry stage as premium proposals (family `option_long`). `shared_entry.use_structure_filter` / `use_sr_filter` (both on in the preset) veto the ORB path and the trend path alike; they replaced `orb_apply_structure_veto` / `orb_apply_sr_veto`, which now fail at load. See the strategy README's "Shared entry stage (2026-09-24)" section.

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
| `htf_minutes`              | `15`                    |
| `htf_lookback_days`                  | `60`                    |
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
- Option strategies also use: `options`, `support_resistance`, `technical_levels`, `candles`, `chart_patterns`, `shared_entry` and `shared_exit` (the shared entry stage and exit families read the underlying's frame), and their own `strategies.<name>` params from the selected top-level config file.

## Practical tuning notes

- `risk.trade_management_mode: adaptive_ladder` is now the safe default recommendation. Ladder-aware strategies opt in via metadata; strategies without ladder metadata automatically behave like normal adaptive management.
- If you want fewer chase entries, tighten the anti-chase thresholds before tightening the whole strategy universe.
- If you want more trade frequency, loosen the screener/liquidity filters before loosening stop logic.
- If you want stronger FVG influence, raise the strategy's FVG weights instead of turning more global filters on.
- If you want the dashboard lighter, turn off the heavier chart overlays before reducing `max_bars`.


## Strategy-specific top-level presets

Prebuilt top-level presets are included under `configs/config.<strategy>.yaml` for every strategy. Each preset is intended to be the complete runtime source of truth for that strategy, while manifests remain the built-in fallback defaults.

Shipped preset files:

| Preset                                          | Strategy                              |
|-------------------------------------------------|---------------------------------------|
| `configs/config.momentum_close.yaml`            | `momentum_close`                      |
| `configs/config.mean_reversion.yaml`            | `mean_reversion`                      |
| `configs/config.closing_reversal.yaml`          | `closing_reversal`                    |
| `configs/config.rth_trend_pullback.yaml`        | `rth_trend_pullback`                  |
| `configs/config.volatility_squeeze_breakout.yaml` | `volatility_squeeze_breakout`       |
| `configs/config.pairs_residual.yaml`            | `pairs_residual`                      |
| `configs/config.opening_range_breakout.yaml`    | `opening_range_breakout`              |
| `configs/config.peer_confirmed_key_levels.yaml` | `peer_confirmed_key_levels`           |
| `configs/config.peer_confirmed_key_levels_1m.yaml` | `peer_confirmed_key_levels_1m`     |
| `configs/config.peer_confirmed_trend_continuation.yaml` | `peer_confirmed_trend_continuation` |
| `configs/config.peer_confirmed_htf_pivots.yaml` | `peer_confirmed_htf_pivots`           |
| `configs/config.top_tier_adaptive.yaml`         | `top_tier_adaptive`                   |
| `configs/config.small_cap_squeeze.yaml`         | `small_cap_squeeze`                   |
| `configs/config.zero_dte_etf_options.yaml`      | `zero_dte_etf_options`                |
| `configs/config.zero_dte_etf_long_options.yaml` | `zero_dte_etf_long_options`           |

Plus two non-strategy files:

- `configs/config.example.yaml` — canonical full-config template, used as the scaffold base by `scripts/scaffold_strategy_plugin.py`. Its shared-knob values are `peer_confirmed_htf_pivots` parity; the scaffold resets them to the dataclass defaults (see `configs/README_PRESETS.md`).
- `configs/config.yaml` — the default file `main.py` loads when `--config` is omitted.

To run a shipped preset:

```bash
python main.py --config configs/config.<strategy>.yaml --strategy <strategy>
```

For per-strategy parameter tuning see [Strategy-by-strategy reference](#strategy-by-strategy-reference). For preset-directory conventions see [`configs/README_PRESETS.md`](configs/README_PRESETS.md).
