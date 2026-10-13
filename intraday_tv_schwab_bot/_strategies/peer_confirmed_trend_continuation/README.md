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

The configured `tradable` and `peers` lists are used the same way as in the key-level family. A candidate outside `tradable` is skipped as `symbol_not_tradable`, as in key_levels and htf_pivots (2026-09-26). The strategy still wants:

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

Even with a good trend and a good pullback, the trade can still be blocked by its own anti-chase / extension, score and peer rules — and, since 2026-09-24, by the shared entry stage (`shared_entry.SharedEntryPolicy`) every strategy's setup goes through. It refuses on the strategy's own blockers and on every `shared_entry` veto the YAML switches on in one pass — structure (`use_structure_filter`), S/R (`use_sr_filter`, which replaced this strategy's `use_sr_veto` param), chart, candle, dual divergence, broken level — and records every blocker under the side's key with the gate snapshots and the near-miss payload. The vetoes and the structure / technical / chart / candle contexts read the 5m trigger frame; S/R is built on the 1m frame. So the refinement's stop floor (`shared_entry.min_stop_atr_mult`) is in 5m ATR, while the broken-level guard's `broken_level_min_clearance_atr` and the divergence entry candidates read the 1m S/R frame, the frame top_tier's thresholds were set on (a 1m ATR is about 0.4 of a 5m one on the family's large caps: 2.56x in RTH, measured 2026-10-13).

So the strategy is continuation-first, but still disciplined about not entering at the worst possible location.

### 6. Stops, targets, and management

The stop is anchored to the pullback structure with ATR buffering. The target starts from reward-to-risk logic and can extend for stronger continuation setups. The shared entry stage can refine both (`shared_entry.use_sr_stop_target_refinement` / `use_technical_stop_target_refinement`), and shared adaptive management — on the refined levels and the FVG term's continuation bias — can then give the stronger trends more room when the context supports it.

The signal carries `regime: trend_continuation`, `entry_style_family: continuation`, the structure fields as `msltf_*` (`ms_ltf_*` until 2026-09-24), `strategy_priority_score` (the total score plus the activity term), `shared_context_score` (the entry-context and FVG terms — its `execution_quality_score` — with the HTF divergence term on this strategy's own HTF context) and `final_priority_score` = the two added, the same total as before (this strategy always summed the entry-context terms). `selection_quality_score` equals `final_priority_score`; the gatekeeper ranks on `ltf_score + 0.5 × shared_context_score` (manifest `signal_priority.shared_score_weight`, user decision 3), then the manifest's tail.

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
  - both read the close's absolute distance from VWAP / EMA9 in ATR, so a close that far on the wrong side of the line (a LONG below it, a SHORT above) pays the same penalty and is refused too, as `wrong_side_hard_cap_vwap_atr` / `wrong_side_hard_cap_ema9_atr` (before 2026-09-26 under the `too_extended_hard_cap_*` tokens); the base exhaustion filter measures only the side's own direction
- Peer and macro confirmation:
  - `min_peer_agreement`, `min_peer_score`, `peer_max_age_minutes`
  - `enable_macro_confirmation`, `require_macro_agreement_count`, `dollar_symbol`, `bond_symbol`, `volatility_symbol`, `macro_max_age_minutes`
- The order and the exits (the family's, below):
  - `max_entry_spread_r`, `max_entry_spread_target_frac`, `protective_exits_on_executable_side`, `force_flatten`, `entry_cutoff_lead_minutes`
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

**The family's engine (2026-10-13).** This strategy subclasses `peer_confirmed_key_levels` and runs its peer and macro votes, its entry spread guard, its protective exits and its knob checks (base README, sections 5-7):

- a peer whose newest 1m bar started more than `peer_max_age_minutes` (3) ago, or a macro symbol more than `macro_max_age_minutes` (20) ago, is `stale` in `peer_details` / `macro_details` (one with no frame `missing`): it never agrees, and a peer counts against the side in its directional score, the worst case (2026-10-13; while stale peers only abstained, a stale peer that would have opposed the trade raised the score). So stale or missing data can only count against an entry here: fewer agreeing peers (`weak_peer_agreement`), a lower score (no peer bonus, `weak_peer_score`) or no macro alignment (the macro miss). Unlike key_levels it refuses no side on too few fresh votes (no `peers_stale` / `macro_stale`);
- the entry spread guard (`max_entry_spread_r` / `max_entry_spread_target_frac`, manifest `0`, off; the shipped preset `0.33` / `0.5`) skips an entry whose quote's spread is too wide against its risk or target distance, or cannot be read;
- `protective_exits_on_executable_side` (manifest `false`, the preset `true`) judges a stop at or past the entry and the peak giveback floors on the bid / ask instead of the mark;
- the family's knobs are checked at load and at the build (the HTF map, the trigger frame, peers, macro, the ages, the spread guard, the protective exits, `force_flatten`, `force_flatten_buffer_minutes` and `entry_cutoff_lead_minutes`, with the bounds against the windows and the macro poll at the build, the shared management, ladder and FVG knobs, among them the base strategy's own reads: `adaptive_breakeven_offset_r`, the partial breakeven and the runner's target and trail, `ladder_min_target_rr` / `ladder_zone_atr_mult` / `ladder_max_rungs`, each declared in the manifest at what the base read without it); this strategy's own knobs are not checked yet.

The shipped preset flattens both sides from 15:53 ET (the management window's 15:58 end less `force_flatten_buffer_minutes`, 5; 12:55 on an early-close day) since 2026-10-13, the flatten retrying in the post-market to 20:00; until then it held both sides overnight. Its entry pass opens no side within `entry_cutoff_lead_minutes` (10) of that cutoff, the check key_levels' makes (`after_entry_cutoff(cutoff=15:43,flatten=15:53)` for the symbol when every side it trades is past it, `<side>.after_entry_cutoff(…)` after a refused side's blockers otherwise): from 15:43, after its 15:40 entry window ends, and from 12:45 on an early-close day, before its 12:55 afternoon window opens. A divergence-only entry skips such a side too. A position still open at 20:00 is carried overnight and flattened at the next session's first management pass, with a WARNING naming it, as key_levels' (base README, section 7); the preset's `startup_reconcile_mode: ignore` does not restore one across a restart. Unlike key_levels, its trigger still reads the forming 5m bucket.

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
| `enable_macro_confirmation`       | `true`                                    |
| `require_macro_agreement_count`   | `1`                                       |
| `dollar_symbol`                   | `NYICDX`                                  |
| `bond_symbol`                     | `TLT`                                     |
| `volatility_symbol`               | `VIX`                                     |
| `peer_max_age_minutes`            | `3`                                       |
| `macro_max_age_minutes`           | `20`                                      |
| `max_entry_spread_r`              | `0.0` (off; the preset `0.33`)            |
| `max_entry_spread_target_frac`    | `0.0` (off; the preset `0.5`)             |
| `protective_exits_on_executable_side` | `false` (the preset `true`)          |
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
| `strong_setup_runner_enabled`     | `true`                                    |
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
| `force_flatten`                   | `{'long': True, 'short': True}`           |
| `force_flatten_buffer_minutes`    | `5`                                       |
| `entry_cutoff_lead_minutes`       | `10`                                      |
| `adaptive_breakeven_offset_r`     | `0.05`                                    |
| `adaptive_partial_breakeven_rr` / `_offset_r` | `null` (off) / `0.0`          |
| `adaptive_runner_target_rr` / `adaptive_runner_trail_pct` | `null` / `null` (derived) |
| `ladder_min_target_rr` / `ladder_zone_atr_mult` / `ladder_max_rungs` | `1.2` / `0.5` / `4` (a divergence-only entry's ladder) |

`force_flatten` is the default end-of-window flatten policy — both directions auto-flatten at the management window's end less `force_flatten_buffer_minutes` (5), and no side opens within `entry_cutoff_lead_minutes` (10) of that cutoff; the shipped preset keeps both on since 2026-10-13 (it flattened neither before).

## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.peer_confirmed_trend_continuation.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
