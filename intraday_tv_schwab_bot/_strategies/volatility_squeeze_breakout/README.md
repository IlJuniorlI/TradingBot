# Volatility Squeeze Breakout

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is the **compression breakout strategy**. It is designed to find names that have tightened up, built directional pressure inside a squeeze, and are now breaking out or breaking down of that compression with enough quality to trade.

### 1. It screens for liquid movers that are active enough to matter

The screener starts from liquid equities with:

- a minimum session-aware move from the active session open
- enough relative volume
- a manageable overall day range

So the candidates are active, but not necessarily already parabolic.

### 2. It defines a squeeze box, not just a simple breakout line

The strategy looks back over a configurable window and measures whether the recent action is actually compressed. It checks several things, including:

- absolute squeeze range as a percentage of price
- squeeze range in ATR units
- Bollinger-width percentage
- Bollinger-width ratio versus a baseline window
- pressure drift from rising lows or falling highs

That means the setup is not just “new high.” It is specifically “new high/new low out of prior compression.”

### 3. It supports both long and short breakouts

For the long side, the strategy wants:

- the squeeze to be real
- price above VWAP when configured
- EMA9 above EMA20
- breakout above the squeeze high by a buffer
- a strong close on the trigger bar
- enough breakout-volume ratio
- enough ATR expansion

The short side mirrors that logic with bearish alignment and a break below the squeeze low.

### 4. It still tries to avoid low-quality expansion bars

Even after the breakout condition is met, the strategy can block the trade because of:

- anti-chase / exhaustion filters
- FVG retest deferment logic
- opposing chart-pattern filters
- technical divergence checks
- structure or S/R vetoes

This matters because squeeze breakouts often fail by triggering late after the easy part of the move is gone.

### 5. How the trade is framed

The stop is anchored beyond the breakout box with ATR and risk-floor protection. The target starts from reward-to-risk and can extend in stronger runner cases. Then both are refined by:

- support/resistance
- technical levels
- FVG context
- adaptive management metadata

The signal-strength score rewards things like:

- tighter compression
- stronger breakout volume
- clear structure support
- better context alignment

### 6. What a strong setup looks like

A strong squeeze breakout usually means:

- the stock compressed first
- pressure built in one direction while volatility stayed contained
- the breakout bar expands with volume and a good close
- VWAP / EMA structure support the move
- the trade is not being entered after the breakout is already overextended

In plain English:

**“This strategy wants the stock to coil first, then expand out of that coil with enough proof that the expansion is real.”**

## Shipped reference

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

Current code defaults:

| Option                                            | Current code default            |
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
| `require_vwap_alignment`                          | `true`                          |
| `require_avwap_alignment`                         | `true`                          |
| `prefer_bollinger_squeeze_flag`                   | `true`                          |
| `target_rr`                                       | `2.05`                          |
| `runner_enabled`                                  | `true`                          |
| `runner_target_rr`                                | `2.4`                           |
| `entry_exhaustion_filter_enabled`                 | `true`                          |
| `max_entry_vwap_extension_atr`                    | `0.88`                          |
| `max_entry_ema9_extension_atr`                    | `0.68`                          |
| `max_entry_bar_range_atr`                         | `1.42`                          |
| `max_entry_upper_wick_frac`                       | `0.25`                          |
| `max_entry_lower_wick_frac`                       | `0.25`                          |
| `entry_wick_close_position_guard`                 | `0.66`                          |
| `anti_chase_fvg_retest_enabled`                   | `true`                          |
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
## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.volatility_squeeze_breakout.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
