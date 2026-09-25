# RTH Trend Pullback

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is a **regular-session trend-pullback strategy** for liquid equities. It can trade both long and short, and its goal is to join an already-active intraday move after a controlled pullback holds and the trend begins re-expanding again.

### 1. It screens for strong directional movers first

The screener looks for liquid names with:

- enough relative volume
- enough absolute move from the active session open
- either strong upside or strong downside session strength

Then it ranks them by a trend score built from the absolute day move and relative volume. So the strategy begins with names that are already proving they are in play.

### 2. It defines both pullback structure and re-expansion triggers

For each candidate, the strategy carves the recent bars into:

- a trigger slice
- a support/resistance slice
- a pullback slice

That lets it ask three separate questions:

- where is the short-term breakout / breakdown level,
- where is the immediate support or resistance reference,
- did the pullback hold in a healthy way?

### 3. The trend regime has to remain intact

For longs, it wants things like:

- enough positive session strength
- price above VWAP
- EMA9 above EMA20
- minimum recent return thresholds
- limited extension from VWAP
- the pullback low to keep holding relative support
- a fresh re-expansion trigger
- a strong close on the trigger bar

Shorts use the bearish mirror image.

This is not a “buy because it dipped” system. It is a **buy because the trend pulled back, held, and restarted** system.

### 4. Anti-chase and FVG retest logic matter here too

Because continuation entries can easily become chase entries, the anti-chase exhaustion checks (extension from VWAP / EMA9 in ATR, the side's wick rejection, an oversized expansion bar) join the setup's own blockers as the pending reasons of one proposal on the candidate's side (style and family `pullback`), which the strategy hands to the shared entry stage (`_strategies/shared_entry.py`). There:

- an FVG retest of the re-expansion trigger (`use_fvg_context`, the `anti_chase_fvg_retest_*` knobs) can clear the missing-trigger, stretched and weak-bar reasons; the entry then comes in as `rth_trend_pullback_<side>_fvg_retest`
- every `shared_entry` veto the preset switches on applies, also to a retest entry: market structure, support/resistance, broken level, opposing chart pattern, dual RSI+OBV counter-divergence, opposing candle cluster

A skipped candidate's decision lists every blocker, the strategy's own first. Until 2026-09-24 the strategy called these itself: the chart filter ran only while nothing else was pending (so an entry the retest admitted never met it), structure and S/R were an either/or, and the candle veto and the broken-level guard never reached it.

That is why a trend-looking chart can still be skipped: the strategy wants the pullback-and-restart pattern, not just a visually strong name.

### 5. How the trade is framed

The stop is anchored beyond the pullback extreme and the VWAP / EMA20 support (resistance for a short), at least `risk.default_stop_pct` away. The target is `target_rr` times that risk, `strong_trend_target_rr` when the structure bias, a break of structure and a continuation pattern all agree with the side.

The shared stage then refines both (support/resistance, then technical levels, bounded by `min_target_rr` and `min_stop_atr_mult`); on a retest entry it pulls the stop to the retest zone's anchor, held at least `min_stop_atr_mult` ATR from entry (since 2026-09-24; the anchor used to bypass that floor). Adaptive management runs in the `trend` style, with the runner open to a strong-trend target or an FVG continuation bias of at least 0.35 when the structure bias agrees.

The strategy's own priority (`strategy_priority_score`: screener score, the side's `ret5` / `ret15`, structure and pattern bonuses) plus the shared context terms (`shared_context_score`) is the `final_priority_score` the gatekeeper ranks on.

The signal is stamped `entry_style_family: pullback`, which gives the position the exit side's longer pullback structure grace (`support_resistance.structure_exit_grace_minutes_pullback`). Until 2026-09-24 that grace covered only top_tier's pullback regime.

The signal also carries `entry_price`, which the shared stage stamps on every signal since 2026-09-24 and which `risk.py`'s same-level retry block needs. This strategy never stamped one before, so the block never reached it and the preset's 30 minutes were inert; the preset ships `risk.same_level_block_minutes: 0` to keep that (parity). Turning the block on for this strategy is a separate go-live decision, for a beta dry-run.

### 6. What a strong setup looks like

A strong RTH trend-pullback setup usually looks like:

- the stock already has a clear intraday trend
- the pullback does not break the underlying trend support/resistance
- price regains momentum through a fresh trigger
- VWAP, EMA alignment, and short-horizon returns still agree
- the entry bar is not overextended

In plain English:

**“This strategy wants a stock that already has a real intraday trend, takes a healthy pause, and then gives a fresh go signal before the move is too extended.”**

## Shipped reference

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

Current code defaults:

| Option                                            | Current code default            |
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
| `entry_exhaustion_filter_enabled`                 | `true`                          |
| `max_entry_vwap_extension_atr`                    | `0.95`                          |
| `max_entry_ema9_extension_atr`                    | `0.78`                          |
| `max_entry_bar_range_atr`                         | `1.65`                          |
| `max_entry_upper_wick_frac`                       | `0.3`                           |
| `max_entry_lower_wick_frac`                       | `0.3`                           |
| `entry_wick_close_position_guard`                 | `0.62`                          |
| `anti_chase_fvg_retest_enabled`                   | `true`                          |
| `anti_chase_fvg_retest_lookback_bars`             | `5`                             |
| `anti_chase_fvg_retest_max_gap_distance_pct`      | `0.003`                         |
| `anti_chase_fvg_retest_max_opposing_distance_pct` | `0.0021`                        |
| `anti_chase_fvg_retest_min_close_position`        | `0.62`                          |
| `anti_chase_fvg_retest_stop_buffer_gap_frac`      | `0.15`                          |
| `strong_trend_runner_enabled`                     | `true`                          |
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
## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.rth_trend_pullback.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
