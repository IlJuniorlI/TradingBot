# Peer Confirmed Trend Continuation

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is the **peer-confirmed continuation strategy**. It reuses the peer and macro confirmation framework from the key-level family, but it is not waiting for a direct hourly support/resistance touch. Instead, it is trying to join an already-established trend after a controlled pullback and fresh re-expansion.

### 1. It starts from trend, not from a level touch

The first question this strategy asks is not “which level is price touching?” but rather:

- is the higher-timeframe trend supportive,
- is the lower-timeframe trend still intact,
- are peers still leaning the same way?

That makes it more fluid than the key-level strategy. It is willing to trade continuation structure even when there is no perfect hourly-zone tap.

### 2. It still uses the same confirmation universe

The configured `tradable` and `peers` lists are used the same way as in the key-level family. The strategy still wants:

- peer agreement
- directional peer score
- optional macro alignment

So although the entry model changed, the “peer confirmed” part remains real and central.

### 3. The trend regime and the pullback are scored separately

The setup has two main components:

- a **trend score** that says the symbol is trending properly,
- a **pullback / trigger score** that says the pullback held and the re-expansion has started.

For a good continuation trade, the strategy wants the pullback to be:

- controlled in depth
- limited in duration
- not dominated by aggressive countertrend volume
- still above support / below resistance in a trend-respecting way

### 4. Then it wants a fresh re-expansion trigger

After the pullback quality checks, the strategy waits for the move to restart. The candle contribution now uses the shared weighted 1/2/3-bar model, but it stays a secondary bonus rather than becoming the main driver. That means:

- a breakout/re-expansion buffer in the trend direction
- enough trigger close quality
- enough trigger volume quality
- acceptable distance from VWAP and EMA9 so it is not just buying/selling the most extended bar

This is the key balance of the strategy: **aggressive enough to join continuation, but not so aggressive that it becomes pure chase logic**.

### 5. Shared vetoes still matter

Even with a good trend and a good pullback, the trade can still be blocked by:

- support/resistance vetoes if enabled
- structure vetoes
- anti-chase / extension rules
- FVG / continuation retest deferment logic
- technical divergence checks

So the strategy is continuation-first, but still disciplined about not entering at the worst possible location.

### 6. Stops, targets, and management

The stop is anchored to the pullback structure with ATR buffering. The target starts from reward-to-risk logic and can extend for stronger continuation setups. Shared adaptive management can then give the stronger trends more room when the context supports it.

### 7. What a strong setup looks like

The best continuation setup usually looks like:

- the symbol is already trending
- peers are aligned with that trend
- a pullback happens without truly breaking the structure
- price re-expands in the trend direction
- the trigger bar is not overly stretched

In plain English:

**“This strategy wants to buy or short the trend after a healthy reset, while peers and optional macro context still say the move should continue.”**

## Shipped reference

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
| `enable_macro_confirmation`       | `true`                                    |
| `require_macro_agreement_count`   | `1`                                       |
| `dollar_symbol`                   | `NYICDX`                                  |
| `bond_symbol`                     | `TLT`                                     |
| `volatility_symbol`               | `VIX`                                     |
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
| `strong_setup_runner_enabled`     | `true`                                    |
| `adaptive_breakeven_rr`           | `0.92`                                    |
| `adaptive_profit_lock_rr`         | `1.2`                                     |
| `adaptive_profit_lock_stop_rr`    | `0.34`                                    |
| `adaptive_runner_trigger_rr`      | `1.12`                                    |
| `htf_fvg_entry_weight`            | `0.34`                                    |
| `one_minute_fvg_entry_weight`     | `0.16`                                    |
| `opposing_fvg_entry_penalty_mult` | `1.0`                                     |
| `fvg_runner_rr_bonus`             | `0.12`                                    |
| `use_sr_veto`                     | `false`                                   |
| `activity_score_weight`           | `0.12`                                    |
| `macro_bonus`                     | `0.7`                                     |
| `macro_miss_penalty`              | `0.3`                                     |
| `extension_penalty_per_atr`       | `0.72`                                    |
| `extension_hard_cap_mult`         | `1.45`                                    |
| `force_flatten`                   | `{'long': True, 'short': True}`           |

`force_flatten` is the default end-of-window flatten policy — both directions auto-flatten when the management window closes.

## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.peer_confirmed_trend_continuation.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
