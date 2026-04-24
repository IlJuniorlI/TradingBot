# 0DTE ETF Long Options

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is the **0DTE long-premium ETF strategy**. It trades a single long call or long put only. It does **not** sell naked premium and it does **not** open spreads. That makes it the simpler and more directional of the two 0DTE ETF strategies in the package.

### 1. It begins by classifying the underlying regime

The strategy does not start with the option chain. It starts with the underlying ETF and asks what kind of day is in progress. The regime logic looks at things like:

- opening-range behavior
- VWAP and EMA relationships
- short-horizon returns
- confirmation bars
- chop / range behavior
- optional higher-timeframe context

Only after the underlying regime passes does the strategy attempt to build an option trade.

### 2. It supports two directional long-premium styles

Depending on the regime and time of day, the strategy can attempt:

- **ORB long option** logic for early directional expansion
- **trend long option** logic for later continuation

For bullish conditions it looks for a long call. For bearish conditions it looks for a long put. It does not mix both sides at once.

### 3. It blocks a lot of bad environments before touching the chain

Before building the option signal, the strategy can skip the trade because of:

- event blackout windows
- entry cutoff time
- an existing open option tied to the same underlying
- insufficient underlying bars
- regime classification failure

That is by design. It wants the underlying day type to be right first, then the option implementation second.

### 4. Then it has to find a usable contract

Once the underlying setup is valid, the strategy filters for a same-day contract that meets the configured quality rules. If it cannot find a clean enough contract or quote, the trade is skipped even if the underlying chart looked good.

### 5. How the option trade is framed

Because this is long premium, the risk model is simpler:

- the debit paid is the maximum contract loss
- the stop is based on a fraction of the premium
- the target is based on a multiple of the premium
- the levels are clamped so the contract-level stop/target remain sensible

The resulting signal still carries metadata about the underlying regime and the selected option so the engine can manage it coherently.

### 6. What a strong setup looks like

A strong long-premium setup usually means:

- the underlying ETF has a clean directional regime
- the time-of-day style matches that regime
- the option chain offers a clean enough contract to express it
- the trade is not blocked by blackout windows or stale quote conditions

In plain English:

**“This strategy first asks whether the ETF itself is having the right kind of day, then buys a same-day call or put only if the option contract is liquid enough to express that directional view cleanly.”**

## Shipped reference

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

Current code defaults:

| Option                               | Current code default |
|--------------------------------------|----------------------|
| `orb_end_time`                       | `10:05`              |
| `trend_start_time`                   | `10:05`              |
| `trend_end_time`                     | `13:30`              |
| `no_new_entries_after`               | `13:45`              |
| `min_bars`                           | `90`                 |
| `min_confirm_bars`                   | `30`                 |
| `trend_vwap_lookback`                | `10`                 |
| `flip_lookback`                      | `14`                 |
| `range_lookback`                     | `25`                 |
| `min_candidate_rvol`                 | `1.15`               |
| `trend_rvol`                         | `1.25`               |
| `credit_min_rvol`                    | `0.88`               |
| `credit_max_rvol`                    | `1.9`                |
| `trend_vwap_distance_pct`            | `0.0014`             |
| `trend_ema_gap_pct`                  | `0.0007`             |
| `trend_above_vwap_frac`              | `0.74`               |
| `trend_min_ret5`                     | `0.0006`             |
| `trend_min_ret15`                    | `0.0013`             |
| `range_vwap_distance_pct`            | `0.0018`             |
| `range_ema_gap_pct`                  | `0.0007`             |
| `range_max_intraday_move_pct`        | `0.009`              |
| `credit_max_day_move_pct`            | `0.008`              |
| `credit_max_vix_change_pct`          | `0.01`               |
| `chop_flip_min`                      | `4`                  |
| `chop_flip_max_for_trend`            | `3`                  |
| `chaos_intraday_range_pct`           | `0.016`              |
| `min_trend_score`                    | `5.1`                |
| `min_range_score`                    | `4.6`                |
| `min_score_gap`                      | `1.6`                |
| `long_option_min_trend_score`        | `5.0`                |
| `long_option_min_score_gap`          | `1.5`                |
| `long_option_max_vwap_extension_pct` | `0.0026`             |
| `long_option_max_ema_gap_pct`        | `0.0015`             |
| `long_option_max_ret5`               | `0.002`              |
| `long_option_max_ret15`              | `0.0044`             |
| `orb_breakout_buffer_pct`            | `0.0008`             |
| `require_index_confirmation`         | `true`               |
| `candle_weight`                      | `0.5`                |
| `candle_sr_weight`                   | `0.35`               |
| `candle_trend_follow_weight`         | `0.25`               |
| `candle_range_penalty`               | `0.3`                |
| `candle_mixed_penalty`               | `0.18`               |
| `use_htf_trend_confirmation`         | `true`               |
| `require_htf_alignment`              | `true`               |
| `htf_timeframe_minutes`              | `15`                 |
| `htf_lookback_days`                  | `60`                 |
| `htf_refresh_seconds`                | `180`                |
| `htf_min_bars`                       | `20`                 |
| `htf_vwap_distance_pct`              | `0.0009`             |
| `htf_ema_gap_pct`                    | `0.0007`             |
| `htf_min_ret3`                       | `0.0009`             |
| `htf_range_vwap_distance_pct`        | `0.002`              |
| `htf_range_ema_gap_pct`              | `0.001`              |
| `htf_score_bonus`                    | `0.65`               |
| `htf_score_penalty`                  | `0.65`               |
| `fvg_context_weight_scale`           | `0.9`                |

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
- `configs/config.zero_dte_etf_long_options.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
