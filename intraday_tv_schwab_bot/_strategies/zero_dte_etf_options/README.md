# 0DTE ETF Options

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is the **defined-risk 0DTE ETF options strategy**. Unlike the long-premium variant, this one can express the day through multiple spread styles. It is trying to match the option structure to the underlying regime rather than forcing one trade type on every session.

### 1. It starts with the underlying ETF regime, not with the spread

The first stage is regime classification on the underlying ETF. The strategy looks at intraday context such as:

- opening-range behavior
- VWAP / EMA alignment
- short-horizon returns
- range versus trend characteristics
- chop / flip behavior
- confirmation windows and optional higher-timeframe context

It then classifies the underlying broadly as something like bullish trend, bearish trend, or range.

### 2. The active regime determines which spread styles are even allowed

Once the regime is known, the strategy only activates the spread structures that fit that environment. The main styles are:

- **orb_debit_spread** for opening directional expansion
- **trend_debit_spread** for continuation sessions
- **midday_credit_spread** for calmer range / fade conditions when enabled

So this is not one strategy with one spread. It is more like a regime router for several defined-risk 0DTE expressions.

### 3. It blocks bad conditions aggressively before constructing a spread

Before placing a trade, the strategy checks things like:

- options globally enabled or not
- style enabled or not
- event blackout windows
- underlying already open or not
- entry cutoff times
- quote freshness and stability
- sufficient underlying bars
- support/resistance clearance in the intended direction

That prevents it from forcing a spread when the market state or option market is not supportive.

### 4. It then has to pass the option-market quality gate

Even if the underlying looks perfect, the spread still has to be tradeable. The strategy filters the chain, validates spread-market quality, and can stabilize / recheck quotes before allowing entry.

So the actual sequence is:

**underlying regime -> style decision -> contract filtering -> spread quality validation -> signal**

### 5. How risk is expressed

Because the strategy uses defined-risk verticals, the position itself already caps maximum loss and maximum profit. Management is based on:

- spread mark behavior
- session timing
- style-specific exit logic
- defined-risk construction metadata

That makes it structurally different from the equity strategies, which are mostly managing directional stock price with stop/target bands.

### 6. What a strong setup looks like

A strong 0DTE defined-risk setup usually means:

- the underlying ETF clearly fits one regime
- the allowed spread style matches that regime and time window
- the option chain has enough quality to build the structure cleanly
- no blackout, stale quote, or clearance rule is being violated

In plain English:

**“This strategy identifies the ETF’s day type first, then chooses the right defined-risk 0DTE spread for that environment instead of treating every session like the same trade.”**

## Shipped reference

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

Current code defaults:

| Option                        | Current code default |
|-------------------------------|----------------------|
| `orb_end_time`                | `10:05`              |
| `trend_start_time`            | `10:05`              |
| `trend_end_time`              | `13:40`              |
| `credit_start_time`           | `11:10`              |
| `credit_end_time`             | `13:40`              |
| `no_new_entries_after`        | `14:15`              |
| `min_bars`                    | `40`                 |
| `min_confirm_bars`            | `28`                 |
| `trend_vwap_lookback`         | `10`                 |
| `flip_lookback`               | `14`                 |
| `range_lookback`              | `25`                 |
| `min_candidate_rvol`          | `1.18`               |
| `trend_rvol`                  | `1.3`                |
| `credit_min_rvol`             | `0.95`               |
| `credit_max_rvol`             | `1.65`               |
| `trend_vwap_distance_pct`     | `0.0015`             |
| `trend_ema_gap_pct`           | `0.0007`             |
| `trend_above_vwap_frac`       | `0.76`               |
| `trend_min_ret5`              | `0.0009`             |
| `trend_min_ret15`             | `0.0015`             |
| `range_vwap_distance_pct`     | `0.0017`             |
| `range_ema_gap_pct`           | `0.0007`             |
| `range_max_intraday_move_pct` | `0.0085`             |
| `credit_max_day_move_pct`     | `0.0075`             |
| `credit_max_vix_change_pct`   | `0.009`              |
| `chop_flip_min`               | `4`                  |
| `chop_flip_max_for_trend`     | `3`                  |
| `chaos_intraday_range_pct`    | `0.015`              |
| `min_trend_score`             | `4.9`                |
| `min_range_score`             | `4.65`               |
| `min_score_gap`               | `1.6`                |
| `orb_breakout_buffer_pct`     | `0.0008`             |
| `require_index_confirmation`  | `true`               |
| `candle_weight`               | `0.5`                |
| `candle_sr_weight`            | `0.35`               |
| `candle_trend_follow_weight`  | `0.25`               |
| `candle_range_penalty`        | `0.3`                |
| `candle_mixed_penalty`        | `0.18`               |
| `use_htf_trend_confirmation`  | `true`               |
| `require_htf_alignment`       | `true`               |
| `htf_timeframe_minutes`       | `15`                 |
| `htf_lookback_days`           | `15`                 |
| `htf_refresh_seconds`         | `300`                |
| `htf_min_bars`                | `20`                 |
| `htf_vwap_distance_pct`       | `0.0009`             |
| `htf_ema_gap_pct`             | `0.0007`             |
| `htf_min_ret3`                | `0.0009`             |
| `htf_range_vwap_distance_pct` | `0.002`              |
| `htf_range_ema_gap_pct`       | `0.001`              |
| `htf_score_bonus`             | `0.65`               |
| `htf_score_penalty`           | `0.65`               |
| `fvg_context_weight_scale`    | `0.9`                |

## Shared 0DTE regime engine

Both option strategies use a regime engine that mixes ORB timing, trend scoring, range/chop scoring, optional index confirmation, HTF confirmation, weighted 1/2/3-bar candle context, FVG context, and option-chain quality filters.

Common parameter families:

- Session clock:
  - `orb_end_time`, `trend_start_time`, `trend_end_time`, `no_new_entries_after`
- Minimum data:
  - `min_bars`, `min_confirm_bars`, `trend_vwap_lookback`, `flip_lookback`, `range_lookback`
- RVOL / tape filters:
  - `min_candidate_rvol`, `trend_rvol`, `credit_min_rvol`, `credit_max_rvol`
- Trend scoring:
  - `trend_vwap_distance_pct`, `trend_ema_gap_pct`, `trend_above_vwap_frac`, `trend_min_ret5`, `trend_min_ret15`
- Range / chop scoring:
  - `range_vwap_distance_pct`, `range_ema_gap_pct`, `range_max_intraday_move_pct`, `credit_max_day_move_pct`, `credit_max_vix_change_pct`, `chop_flip_min`, `chop_flip_max_for_trend`, `chaos_intraday_range_pct`, `min_range_score`
- Regime separation:
  - `min_trend_score`, `min_score_gap`
- ORB confirmation:
  - `orb_breakout_buffer_pct`
- Index confirmation:
  - `require_index_confirmation`
- Candle / SR / trend-follow weights:
  - `candle_weight`, `candle_sr_weight`, `candle_trend_follow_weight`, `candle_range_penalty`, `candle_mixed_penalty`
- HTF confirmation:
  - `use_htf_trend_confirmation`, `require_htf_alignment`, `htf_timeframe_minutes`, `htf_lookback_days`, `htf_refresh_seconds`, `htf_min_bars`, `htf_vwap_distance_pct`, `htf_ema_gap_pct`, `htf_min_ret3`, `htf_range_vwap_distance_pct`, `htf_range_ema_gap_pct`, `htf_score_bonus`, `htf_score_penalty`
- FVG contribution:
  - `fvg_context_weight_scale`

General behavior:

- Raising score thresholds makes the option engine more selective.
- Raising the HTF bonus/penalty makes HTF alignment matter more.
- Raising `fvg_context_weight_scale` makes one-minute and HTF FVG context matter more to the regime score.
- Tightening the trend-extension caps reduces late chase entries.

## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.zero_dte_etf_options.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
