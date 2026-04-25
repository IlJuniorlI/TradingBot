# Peer Confirmed HTF Pivots

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This strategy trades like an **S/R battleground scalp strategy** around higher-timeframe support and resistance references. The basic idea is:

**find the nearest meaningful support for longs or resistance for shorts, decide whether price is reclaiming it, rejecting it, or continuing cleanly away from it, and only trade when peers and optional macro context agree.**

### 1. It starts with a simple directional candidate list

The screener works from the configured `tradable` symbols and ranks them by a **moderate-extension, liquidity-weighted focus score** rather than blindly rewarding the biggest move from open. It also uses a **contrarian S/R scalp bias** for the candidate hint: stronger green extension tilts the hint toward short, while stronger red extension tilts the hint toward long. That only creates the candidate queue and side hint. The real decision still does not happen until the strategy builds its higher-timeframe pivot map.

### 2. It builds an S/R battleground context

The strategy now starts from the support/resistance map and uses market structure as context, not as the sole anchor. In practice, that gives it:

- nearest support / resistance
- flip levels like broken resistance acting as support or broken support acting as resistance
- higher-timeframe directional bias and pivot bias
- recent BOS / CHOCH information
- a tradeable zone around the chosen battleground level

For longs, the important anchor is the nearest tradeable support battleground. For shorts, it is the nearest tradeable resistance battleground.

### 3. It scores the broader regime before the entry family

Before picking the actual entry pattern, the strategy asks whether the regime is good enough. The regime score uses things like:

- HTF bias / pivot bias
- VWAP alignment
- EMA9 vs EMA20
- price vs EMA9
- ADX quality
- recent structure-event support

So even a nice-looking pivot interaction can still fail if the broader context is too weak.

It also now checks **non-FVG zone flip alignment** around the chosen battleground:

- original support zones should still be holding for longs
- original resistance zones should still be holding for shorts
- broken resistance used as new support must have its zone flip **confirmed** before it gets full credit
- broken support used as new resistance must have its zone flip **confirmed** before it gets full credit

That means a level can no longer look good on price alone while its matching non-FVG zone state is still unconfirmed or already flipped against the trade.

### 4. It can express the setup in three different entry families

The actual trigger can come from one of three families:

- **pivot reclaim**: price sweeps or trades into the pivot zone and then reclaims it
- **pivot rejection**: price tests the pivot zone and visibly rejects it
- **pivot continuation**: price bases around the pivot and then continues away from it in the expected direction

When `entry_family=auto`, the strategy evaluates all three, slightly **prefers reclaim first, then rejection, then continuation** when raw scores are close, and otherwise falls back to the best weighted fit. That means `auto` is still a structured best-fit selector, but it is now intentionally tuned toward S/R reactions before breakout continuation.

### 5. Peer and macro confirmation are real gates

After the pivot-family logic, the strategy still requires:

- enough peer agreement
- enough directional peer score
- optional macro agreement from the configured dollar / bond / volatility symbols

That keeps it from taking a pivot setup just because the single chart looks good. The broader neighborhood still has to support the idea.

### 6. It still uses shared anti-chase and veto logic

Even after the family is chosen, the trade can still be blocked by:

- trigger-quality thresholds
- total-score thresholds
- pivot-distance rules
- exhaustion / extension checks
- structure vetoes
- optional S/R vetoes
- FVG context rules

That is why this strategy can be aggressive around pivots without turning into a blind breakout-chase system.

### 7. How the trade is framed

The stop is anchored to the chosen support / resistance battleground with ATR buffering. The first target now starts from the **next opposing S/R level** when that gives enough room, and only falls back to reward-to-risk expansion logic when needed. It can then be refined by:

- support/resistance
- technical levels
- FVG context
- adaptive / runner management

So the pivot is not just an entry reference. It is also the core anchor for the trade’s risk structure.

### 8. What a strong setup looks like

A strong HTF-pivot setup usually means:

- the hourly structure identifies a meaningful pivot
- the broader regime is supportive
- one of reclaim / rejection / continuation scores cleanly
- peers agree with the direction
- macro is not obviously fighting the move
- price is not already too stretched from the pivot when the trigger appears

In plain English:

**“This strategy wants a stock reacting to a real hourly pivot, with a clean lower-timeframe trigger around that pivot, and enough peer/macro agreement that the reaction is worth trusting.”**

## Shipped reference

Purpose: trade around higher-timeframe pivot references instead of generic hourly key-level votes. The strategy can enter in reclaim, rejection, or continuation mode, while still requiring peer confirmation and optional macro alignment.

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
  - `pivot_reclaim_family_bonus`, `pivot_rejection_family_bonus`, `pivot_continuation_family_bonus` for `auto` family weighting
- Regime / trigger scoring:
  - `min_regime_score`, `min_trigger_score`, `min_total_score`, `min_peer_agreement`, `min_peer_score`
  - `enable_macro_confirmation`, `require_macro_agreement_count`, `dollar_symbol`, `bond_symbol`, `volatility_symbol`
- Pivot-zone sizing and family detail:
  - `pivot_zone_atr_mult`, `pivot_zone_pct`
  - `pivot_confirmed_flip_zone_bonus`, `pivot_original_zone_hold_bonus`, `pivot_pending_flip_zone_penalty`, `pivot_invalidated_zone_penalty`
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
- Screener shaping:
  - `screener_contrarian_bias_threshold_pct`, `screener_activity_move_sweet_spot_pct`, `screener_activity_move_cap_pct`, `screener_relative_volume_cap`
- Context overlays — fair-value-gap (FVG) entry adjustment knobs (read by `strategy_base._fvg_entry_adjustment_components`, gated by `shared_entry.use_fvg_context`). All shipped per-strategy in this strategy's `manifest.json`:
  - `htf_fvg_entry_weight`, `one_minute_fvg_entry_weight` — multipliers applied to the HTF and 1-minute FVG bull/bear scores when computing the entry adjustment.
  - `opposing_fvg_entry_penalty_mult` — multiplier on the opposing-direction FVG penalty (1.0 = full penalty; lower = more tolerant of trades against an active gap).
  - `fvg_runner_rr_bonus` — extra R:R credit when the trade direction aligns with a same-direction continuation FVG.
  - `same_direction_fvg_validated_bonus`, `same_direction_fvg_active_bonus` — entry-score bonuses when a same-direction gap has been validated (price interacted) or is still active (untouched).
  - `opposing_fvg_validated_penalty`, `opposing_fvg_active_penalty` — entry-score penalties when an opposing-direction gap has been validated or is still active.
  - `invalidated_opposing_fvg_bonus` — bonus when the opposing-direction gap has been filled (cleared the bias).
  - `same_direction_fvg_invalidated_penalty` — penalty when the same-direction gap has been filled. Manifest default `0.102`, which equals the historical `opposing_fvg_active_penalty * 0.85` derivation; tune independently per strategy.

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
| `entry_family`                                 | `auto`                                          |
| `min_regime_score`                             | `4`                                             |
| `min_trigger_score`                            | `2.5`                                           |
| `min_total_score`                              | `5`                                             |
| `min_peer_agreement`                           | `2`                                             |
| `min_peer_score`                               | `2`                                             |
| `enable_macro_confirmation`                    | `true`                                          |
| `require_macro_agreement_count`                | `1`                                             |
| `use_sr_veto`                                  | `false`                                         |
| `dollar_symbol`                                | `NYICDX`                                        |
| `bond_symbol`                                  | `TLT`                                           |
| `volatility_symbol`                            | `VIX`                                           |
| `pivot_zone_atr_mult`                          | `0.24`                                          |
| `pivot_zone_pct`                               | `0.0018`                                        |
| `pivot_battleground_max_distance_atr`          | `1.1`                                           |
| `pivot_target_min_clearance_atr`               | `1.1`                                           |
| `pivot_confirmed_flip_zone_bonus`              | `0.55`                                          |
| `pivot_original_zone_hold_bonus`               | `0.18`                                          |
| `pivot_pending_flip_zone_penalty`              | `0.5`                                           |
| `pivot_invalidated_zone_penalty`               | `1.0`                                           |
| `pivot_reclaim_buffer_pct`                     | `0.00045`                                       |
| `pivot_reclaim_zone_frac`                      | `0.09`                                          |
| `pivot_rejection_min_wick_frac`                | `0.24`                                          |
| `pivot_rejection_allows_neutral_ltf_structure` | `true`                                          |
| `pivot_continuation_breakout_buffer_pct`       | `0.0009`                                        |
| `pivot_continuation_interaction_lookback_bars` | `9`                                             |
| `pivot_continuation_max_distance_atr`          | `1.45`                                          |
| `min_trigger_close_position`                   | `0.6`                                           |
| `min_trigger_volume_ratio`                     | `1.0`                                           |
| `min_adx14`                                    | `12.5`                                          |
| `max_reclaim_distance_from_pivot_atr`          | `0.9`                                           |
| `max_rejection_distance_from_pivot_atr`        | `0.82`                                          |
| `max_continuation_distance_from_pivot_atr`     | `1.35`                                          |
| `entry_exhaustion_filter_enabled`              | `true`                                          |
| `max_entry_vwap_extension_atr`                 | `1.05`                                          |
| `max_entry_ema9_extension_atr`                 | `0.85`                                          |
| `max_entry_bar_range_atr`                      | `1.65`                                          |
| `max_entry_upper_wick_frac`                    | `0.3`                                           |
| `max_entry_lower_wick_frac`                    | `0.3`                                           |
| `min_rr`                                       | `1.65`                                          |
| `target_rr`                                    | `1.95`                                          |
| `runner_target_rr`                             | `2.45`                                          |
| `stop_buffer_atr_mult`                         | `0.5`                                           |
| `strong_setup_runner_enabled`                  | `true`                                          |
| `adaptive_breakeven_rr`                        | `0.9`                                           |
| `adaptive_profit_lock_rr`                      | `1.18`                                          |
| `adaptive_profit_lock_stop_rr`                 | `0.32`                                          |
| `adaptive_runner_trigger_rr`                   | `1.1`                                           |
| `htf_fvg_entry_weight`                         | `0.28`                                          |
| `one_minute_fvg_entry_weight`                  | `0.14`                                          |
| `pivot_reclaim_family_bonus`                   | `0.25`                                          |
| `pivot_rejection_family_bonus`                 | `0.12`                                          |
| `pivot_continuation_family_bonus`              | `-0.12`                                         |
| `screener_contrarian_bias_threshold_pct`       | `1.25`                                          |
| `screener_activity_move_sweet_spot_pct`        | `1.0`                                           |
| `screener_activity_move_cap_pct`               | `2.4`                                           |
| `screener_relative_volume_cap`                 | `2.2`                                           |
| `opposing_fvg_entry_penalty_mult`              | `1.0`                                           |
| `fvg_runner_rr_bonus`                          | `0.1`                                           |
| `activity_score_weight`                        | `0.1`                                           |
| `macro_bonus`                                  | `0.75`                                          |
| `macro_miss_penalty`                           | `0.28`                                          |
| `force_flatten`                                | `{'long': True, 'short': True}`                 |
## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.peer_confirmed_htf_pivots.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
