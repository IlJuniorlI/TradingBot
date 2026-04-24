# Momentum Close

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is a **late-day, long-only continuation breakout strategy**. Its job is to find stocks that are already leaders on the day and then participate only when that strength starts expanding again into the close.

### 1. It screens for leaders first

The screener looks for small-cap / active movers with:

- enough relative volume
- strong session-aware move from the active session open
- a high ranking based on session strength

So the strategy is not trying to “discover” strength. It starts with names that are already visibly in play.

### 2. It defines a breakout trigger from recent structure

On the chart side, the strategy uses a recent lookback window to define the breakout level. The candidate has to prove it is pushing through that level now.

For the long setup, it wants:

- price above the recent breakout level
- price above VWAP
- EMA9 at or above EMA20
- positive recent momentum (`ret15`)
- supportive chart context

This is a true **continuation** model, not a dip-buying model.

### 3. It actively tries not to chase bad breakouts

A big part of the strategy is not the breakout itself, but the quality control around it:

- anti-chase exhaustion checks
- wick / expansion-bar checks
- optional FVG retest deferment logic
- divergence checks from the technical layer
- opposing chart-pattern filter

So if the breakout is already stretched, messy, or too one-sided, the strategy can defer or block it instead of blindly buying the push.

### 4. If the setup is valid, it anchors the trade to breakout structure

The initial stop comes from a mix of:

- recent breakout structure lows
- the default stop framework
- optional FVG retest anchoring if the setup is being treated as a better-quality retest entry

The initial target starts from the default target framework, then gets refined by:

- support/resistance
- technical levels
- FVG context
- adaptive management metadata

### 5. Signal strength and management

The signal-strength score is built from:

- the original screener score
- recent momentum (`ret15`)
- how far the breakout is past the trigger
- structure bonuses
- chart-pattern bonuses
- shared context adjustments

That means the strategy prefers **clean, expanding breakouts** over marginal ones that barely poke above the level.

### 6. What a good setup looks like

The best momentum-close setup usually looks like:

- a stock has already led all day
- it consolidates or pauses without fully breaking trend
- price pushes back through a recent high late in the session
- VWAP / EMA alignment still agree with the move
- the breakout is not overly extended or obviously exhausted

In plain English:

**“I want the day’s strong leader to prove it can break higher again late, but I do not want to buy the most stretched or sloppiest version of that move.”**

## Shipped reference

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

Current code defaults:

| Option                                            | Current code default            |
|---------------------------------------------------|---------------------------------|
| `min_change_from_open`                            | `4.0`                           |
| `max_change_from_open`                            | `14.0`                          |
| `min_rvol`                                        | `2.4`                           |
| `breakout_lookback_bars`                          | `6`                             |
| `entry_exhaustion_filter_enabled`                 | `true`                          |
| `max_entry_vwap_extension_atr`                    | `0.85`                          |
| `max_entry_ema9_extension_atr`                    | `0.68`                          |
| `max_entry_bar_range_atr`                         | `1.55`                          |
| `max_entry_upper_wick_frac`                       | `0.27`                          |
| `max_entry_lower_wick_frac`                       | `0.27`                          |
| `entry_wick_close_position_guard`                 | `0.66`                          |
| `anti_chase_fvg_retest_enabled`                   | `true`                          |
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
## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.momentum_close.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
