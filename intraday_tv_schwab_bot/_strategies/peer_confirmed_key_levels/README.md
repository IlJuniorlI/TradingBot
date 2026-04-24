# Peer Confirmed Key Levels

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is the **peer-confirmed hourly key-level strategy**. It is built around the idea that a stock-level setup becomes more trustworthy when three things line up at the same time:

1. the symbol is reacting to an important higher-timeframe level or zone,
2. its peers are confirming the same directional idea, and
3. optional macro context is not fighting the trade.

It is one of the more structured strategies in the package because it combines level selection, trigger scoring, peer voting, and post-entry ladder management.

### 1. The screener builds a directional universe, not a blind watchlist

The strategy starts from the configured `tradable` symbols and `peers`. It then builds a confirmation universe that helps it answer:

- which symbols are acting directionally bullish or bearish,
- which hourly levels matter most,
- whether the active symbol is aligned with its neighborhood.

This is not a pure momentum screener. The candidate list is only the first step toward a much more level-centric decision.

### 2. It builds hourly support/resistance levels and zones

The core map is built from higher-timeframe support/resistance. The strategy creates candidate levels and zones using the shared higher-timeframe level builder, then scores those levels using things like:

- level quality
- hourly vote support
- proximity and clearance
- round-number context
- peer-level overlap / confirmation

The result is not just “nearest support” or “nearest resistance.” It is a ranked set of tradeable battlegrounds.

### 3. It chooses the active level before it evaluates the trigger

This is an important design point: the strategy does not first decide “I want to go long” and then find a level afterward. It first identifies the best hourly level/zone to trade around, then asks whether price interaction with that zone produces a usable trigger.

That is why the dashboard and engine can both talk about a selected level, selected zone width, and nearby target ladder in a consistent way.

### 4. It scores the trigger separately from the level

Once a level is selected, the strategy evaluates the actual interaction on the trigger timeframe. The trigger score reflects whether price is behaving correctly around the chosen zone.

The shipped implementation now keeps the original **4-part base trigger** intact, then layers a capped **trigger-quality bonus** on top. That bonus grades the quality of the reclaim / reject, how efficiently price used the zone, candle quality, volume expansion, and range expansion. Candle confirmation now uses the shared weighted 1/2/3-bar model, so stronger multi-bar reversals outrank weaker one-bar hints. This improves ranking and setup separation without raising the minimum trigger gate.

So the setup has two different quality dimensions:

- **level quality**: is this an important enough hourly level?
- **trigger quality**: is price interacting with it in a tradable way right now?

A setup can fail either side independently.

### 5. Peer and macro confirmation are part of the gate, not decoration

After the local level/trigger logic, the strategy checks the surrounding context:

- peer agreement count
- directional peer score
- optional macro confirmation from the dollar, bonds, and volatility symbols

Those checks are not just dashboard cosmetics. They are real gating conditions. A stock can be sitting on a good hourly zone and still be skipped because its peer basket or macro basket is not agreeing strongly enough.

### 6. Stop, target, and ladder behavior

This strategy is especially strong in post-entry management because it knows the surrounding hourly ladder. It can:

- place the initial stop beyond the defended zone with ATR buffering
- target the next qualifying level(s)
- emit rung metadata for `adaptive_ladder` management
- ratchet stops behind defended levels as the trade progresses

That makes it more structurally anchored than simple percent-stop / percent-target systems.

### 7. What a strong setup looks like

The best key-level setup usually looks like:

- price is interacting with a meaningful hourly level or zone
- peers are leaning the same way
- macro is not fighting the move
- the trigger bar around the zone is clean enough to score well
- there is enough target clearance to justify the trade

In plain English:

**“This strategy wants an hourly battleground that matters, confirmation that the neighborhood agrees, and a lower-timeframe trigger that proves the level is actually being defended or lost right now.”**

## Shipped reference

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
| `trigger_quality_bonus_enabled`      | `true`                                    |
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
| `enable_macro_confirmation`          | `true`                                    |
| `require_macro_agreement_count`      | `1`                                       |
| `dollar_symbol`                      | `NYICDX`                                  |
| `bond_symbol`                        | `TLT`                                     |
| `volatility_symbol`                  | `VIX`                                     |
| `level_round_number_tolerance_pct`   | `0.002`                                   |
| `strong_setup_runner_enabled`        | `true`                                    |
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
## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.peer_confirmed_key_levels.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
