# Top-Tier Adaptive

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is a **multi-regime adaptive intraday strategy** for a fixed universe of 25 mega-cap Tech / AI stocks in three co-movement groups (`ai_hardware`, `platforms`, `software`). It detects whether each symbol is trending, pulling back, ranging, breaking out of a volatility squeeze, sustaining momentum from the session open, or scalping between HTF support/resistance zones, then applies the appropriate entry style. Trades both long and short across the full RTH session with time-of-day regime gating.

**Timeframes (1m LTF, 2026-05-29).** The trend/pullback "LTF" runs on **1-minute bars** (`ltf_minutes: 1`) so entries and exits act on the freshest close instead of waiting up to 5 minutes for a 5m bar to print. To keep the *behavior* identical to the prior 5m tune, that 1m LTF's indicators are stretched ×5 via `ltf_indicator_span_scale: 5` — `add_indicators(span_scale=5)` makes its atr14/adx14/rsi14/ret5/ret15 effectively 70/70/70/25/75-bar, i.e. the same wall-clock horizons the 5m frame had. Its fast / slow EMAs are set on their own by `ltf_ema_fast_span: 45` / `ltf_ema_slow_span: 100` (the same 5m horizon); change those to move the EMAs the entry scoring reads without touching any other indicator. Exits and the ema9-extension gate read the base 1m frame's native EMA9 / EMA20. Consequently every ATR-based stop/buffer and every score threshold keeps its 5m calibration unchanged; only the bar granularity got finer. Two LTF lookbacks that *count* bars were scaled to match (`pullback_lookback_bars` 5→25, `side_decision_recent_lookback_bars` 6→30). The `range`/`vol_squeeze`/`momentum` regimes, the `technical_levels` context, and chart-pattern detection all read the **base 1m frame** (unchanged before and after this switch), so their lookbacks did *not* change. HTF stays 15m; the structure-pivot frame stays 5m (`support_resistance.structure_ltf_timeframe_minutes: 5`).

### 1. It trades a fixed universe, not a dynamic screener

Unlike the dynamic-discovery strategies, this one operates on a predefined list of 23 top-tier symbols configured in `params.tradable`. The screener fetches those symbols from TradingView and ranks them by absolute intraday move weighted by relative volume. This means the bot always knows exactly what it is watching, and the screener simply decides which ones are most active right now.

### 2. It scores eight regimes for every candidate

For each symbol and each direction (long/short), regime scores are computed for whichever regimes are allowed at the current time:

- **Trend**: close vs VWAP, EMA alignment, momentum (ret5/ret15), ADX strength, index confirmation. Max score 6.0.
- **Pullback**: requires underlying trend first, then checks for EMA20/VWAP touch, support/resistance hold, EMA9 reclaim, close quality, volume expansion. Max score 5.0.
- **Range**: fade the edge of the lookback range. Scored on the geometry `_build_range_signal` gates on — close in the fade zone at this side's edge (the outer `range_entry_zone_frac`, default 35%) with the last completed bar in it too (+2.0, both required), a rejection wick off that edge (+1.0), a deep rather than marginal fade (+0.5), a two-sided tape (+0.5: `range_min_flip_count` VWAP crosses and the lookback range within `range_max_intraday_range_pct`), and nothing trending against it (+0.5: indices VWAP-neutral and ema9/ema20 within `range_max_ema_gap_pct`). Side-aware. A Bollinger squeeze scores base only when `reject_range_during_squeeze` is on, because the builder refuses those outright. Max score 5.0 (`REGIME_SCORE_CEILINGS['range']`); `min_range_score: 4.0` requires 1.5 beyond the zone — the wick plus any one +0.5, or all three together — so a marginal fade with a wick and no evidence the tape is actually ranging does not qualify. Exempt from the `_decide_side` vote (a fade must enter against the move), so it carries the soft `_bias_penalty` even when the vote decides — the only counter-trend filter it has. That penalty was skipped for it until 2026-09-22 (see CHANGELOG), which let live range signals fade their own day by >= 0.30% in 6 of 11 cases, QCOM LONG on a -6.85% day among them.
  *Rewritten 2026-09-22.* The previous scorer measured range CHARACTER — VWAP proximity (+1.5), EMA gap, VWAP cross count, intraday range width, index neutrality — and never looked at where in the range price was, while the builder has only ever entered from the outer 35%. Those are close to opposites: the largest single component paid for sitting on VWAP, i.e. the MIDDLE. Replayed over 10,162 real 1m bars (28 symbols, 2026-09-21) the old scorer correlated **-0.17** with distance from mid-range, averaged 1.94 at an edge against 2.19 mid-range, and blocked 6,485 of the 7,477 bars that were at an edge; the new one correlates **+0.68**, and every bar it qualifies is in the builder's entry zone. `range_max_vwap_dist_pct` was removed with it.
- **Vol-squeeze** *(added 2026-05-12)*: detects a tight Bollinger compression box across `vol_squeeze_lookback_bars` (default 12), then requires the break to clear **all three** of the builder's hard gates — buffered break of the box edge, breakout volume ratio, and bar close position — for +1.5, with +0.5 each for a DECISIVE break (2x the buffer) and DECISIVE volume (1.5x the required ratio), plus +0.5 for VWAP/EMA alignment. *Score/build mismatch fixed 2026-09-22:* those three were HARD gates in `_build_vol_squeeze_signal` from 2026-05-14 but stayed optional bonuses in `_score_vol_squeeze`, so a setup with none of them scored 2.0 + 1.0 + 0.5 + 0.5 = exactly `min_vol_squeeze_score` (4.0), cleared the floor, won its place in the build queue and was then certain to die on `vol_squeeze_weak_breakout_*` — 1,838 of them across the 09-21 and 09-22 sessions, every one an inflated normalised score that also became the `entry_family` recorded for the skip. Both now read `_vol_squeeze_breakout_quality`, and `vol_squeeze_hard_breakout_gates` moves the two together. Compression alone caps at 3.5, below the floor. Allowed in the primary, midday (since 2026-09-21) and afternoon windows. **DISABLED in the shipped preset on 2026-09-18, RE-ENABLED 2026-09-20** (`disable_vol_squeeze_regime: false`): the trim's stated reason was that its 6.5 score ceiling won it more auctions than its edge justified, but `_normalized_regime_score` shipped in the same change and ranks on `(score - floor) / (ceiling - floor)` — its 2.5 headroom is the widest of any enabled regime, so the ceiling now works against it. It is also the only consumer of the squeeze condition that `range` rejects (`reject_range_during_squeeze`), so with it off that hand-off goes nowhere. Whether the regime has edge on mega caps is still unmeasured: with the flag off it was never scored and never appeared in the skip line. See *Cross-regime score normalisation* for the ranking maths.
- **Momentum** *(added 2026-05-12, widened from afternoon-only and renamed from `momentum_close`)*: momentum-from-open continuation. Computes day_strength live from session open + current close, requires `momentum_min_day_strength` (default 1.5%) with the trade side, scores N-bar breakout + alignment. **Allowed post-ORB through close** (`orb_end_time` → `no_new_entries_after`) including midday — the day_strength hard gate is what filters chop, not the time window.
- **Sr-scalp** *(added 2026-05-12)*: HTF S/R mean-reversion scalp. Uses the bot's existing `sr_ctx.nearest_support` (HS) and `nearest_resistance` (HR) as level prices and zone bands matching the dashboard's `key_level_zones` — NO strategy-local level creation. A distance gate requires the inner zone gap to clear BOTH `sr_scalp_min_distance_pct` (default 0.8% of close) AND `sr_scalp_min_distance_atr` (default 2.5x ATR); too-close zones reject at build time as `htf_zones_too_close` so other regimes can fall through. A proximity gate requires close to be inside the entry-side zone or within `sr_scalp_max_distance_from_zone_atr` of its inner edge. **Allowed from `orb_end_time` through close** (`orb_end_time` → `no_new_entries_after`) **whether or not the ORB regime is on** — with `disable_orb_regime` the rest of the mix trades from the open, but sr_scalp still waits for `orb_end_time` (since 2026-09-23), because the reason it skips the opening window is the tape, not ORB. Index-confirmation EXEMPT (mean-reversion thesis, same as range). Max score 5.0 (`REGIME_SCORE_CEILINGS['sr_scalp']`), reached only on the flip-continuation path; the proximity path caps at 4.5. The pre-2026-05-29 chop-character scorer had an empirical ceiling near 3.9 and sat silently dead from 2026-05-12 to 2026-05-27 behind a 4.0 threshold (0 entries ever) — that note no longer describes the current level-geometry scorer. **ENABLED in the shipped preset** since 2026-09-23 (`disable_sr_scalp_regime: false`; it was off 2026-09-18 → 2026-09-23). The geometry that had made it unbuildable was FIXED 2026-09-22: `sr_scalp_min_stop_atr_mult` was a flat 2.5 ATR floor while the geometric stop is at most ~0.9 ATR from entry (proximity 0.4 + zone half-width 0.2 + `level_buffer` ~0.3), so the floor bound on **every** setup — the level picked the direction and was then discarded on the risk side. It also made the reward floor a fiction: a flat 2.5 ATR risk against a target pinned to the opposing zone needs 2.4-3.2 ATR of gap to clear `min_target_rr`, so a setup at the advertised `sr_scalp_min_distance_atr` could never build. The floor is now measured against how far price has PIERCED the level being leaned on over `sr_scalp_noise_lookback_bars`, counted from when it took its current role (a fresh resistance→support flip's approach from below is not a breach, or FLIP-CONTINUATION could never build) — a level that has been holding keeps its own tight stop, a level being cut through pushes the stop past the breaches and is then rejected on R:R, which is the 2026-07-28 META case rejected for the right reason. `sr_scalp_min_stop_atr_mult` drops to 0.5 as an absolute backstop. Its 15 archived trades (-$179.67) all predate that fix.

Build-time fall-through: as of 2026-05-12 each side stores an ordered list of qualifying regimes (score-descending). The build phase iterates the list and tries each regime in turn — if a regime's build fails (e.g. trend's `no_fresh_breakout`, sr_scalp's `htf_zones_too_close`), the next qualifying regime on the same side gets a chance. Across sides, the higher-scored side's full build_order is tried first.

Each regime can be globally disabled via its own opt-out knob: `disable_trend_regime`, `disable_pullback_regime`, `disable_range_regime`, `disable_vol_squeeze_regime`, `disable_momentum_regime`, `disable_sr_scalp_regime`, `disable_vwap_reclaim_regime` — all default `false`. The 7th regime, **orb** (the true Opening Range Breakout, sole regime in the opening window), can be skipped two ways: `disable_orb_window` skips the opening window entirely (start trading at `orb_end_time`), while `disable_orb_regime` drops the ORB regime *and* its opening-range carve-out so the normal regime mix runs continuously from the open (used by the `small_cap_squeeze` subclass). An 8th regime, **vwap_reclaim** (a re-entry on a VWAP flush-and-reclaim), uses the same `disable_vwap_reclaim_regime` opt-out (default `false`) as the rest and is **ENABLED in the shipped preset** as of 2026-09-18 — it is the strategy's only reversal builder, see section 18. It shipped on 2026-05-30 as an opt-IN `enable_vwap_reclaim_regime` so it could not switch itself on for the `small_cap_squeeze` subclass; that subclass now sets the flag explicitly, so the polarity was normalised on 2026-09-20 and all eight regime knobs now read the same way.

### 3. Time-of-day gating controls which regimes are allowed

Not all regimes fire at all times. All boundaries are param-driven (no hard-coded times):

- **09:30 - opening-range-end (range formation)**: NO entries — the opening range (first `orb_range_minutes`, default 15 → 09:30-09:45) is still forming
- **opening-range-end - `orb_end_time` (ORB window)**: **orb only** — a true Opening Range Breakout. Trades a break of the opening range (stop = opposite range edge, target = measured move). As of 2026-05-29 this replaced the old "trend regime with bypasses" approach.
- **`orb_end_time` - `midday_start_time` (primary)**: trend, pullback, range, vol_squeeze, momentum, sr_scalp, vwap_reclaim
- **`midday_start_time` - `midday_end_time` (midday)**: pullback, momentum, sr_scalp, vwap_reclaim, vol_squeeze *(added 2026-09-21 — its thesis is compression resolving into expansion and the lunchtime tape IS the compression; it had been excluded from the one window where its setup is most common. On 2026-09-21, 51% of midday skips on the day's five biggest movers were "no regime qualified", with none of the three regimes then offered coming within half a point of its floor)*
- **`afternoon_start_time` - `no_new_entries_after` (afternoon)**: trend, pullback, range, vol_squeeze, momentum, sr_scalp, vwap_reclaim (range was disabled pre-2026-04-22; re-enabled so afternoon range-bound tapes get mean-reversion entries — disable with `afternoon_include_range: false`)
- **After `no_new_entries_after`**: no new entries
- **With `disable_orb_regime: true`** (the shipped preset): no range-formation zone and no ORB window — the primary mix runs from 09:30 (entries from 09:35, the preset's `entry_windows` open), **except sr_scalp, which still waits for `orb_end_time`**.

Default boundary values: opening range `09:30`-`09:45` (`orb_range_minutes: 15`), ORB end `10:05`, midday `11:30`-`13:00`, afternoon `13:00`-`no_new_entries_after` (`15:00` in the shipped RTH-only preset; `19:30` if extended-hours trading is enabled).

Midday still favors pullbacks because top-tier stocks tend to chop during the lunch hour, but the momentum, sr_scalp and (since 2026-09-21) vol_squeeze regimes are allowed alongside — that chop is itself the compression vol_squeeze trades — the `momentum_min_day_strength` hard gate (default 1.5%) and the sr_scalp HTF zone-gap floor filter out non-qualifying names automatically. As of 2026-05-12 the momentum regime is post-ORB-through-close (renamed from `momentum_close` and widened from afternoon-only) and sr_scalp is post-ORB-through-close.

Per-regime opt-out via params: each of the eight regimes has its own `disable_*_regime` boolean knob (all default `false`). Disabling a regime strips it from every window. The afternoon-range sub-knob `afternoon_include_range` still works for window-scoped exclusion.

ORB-window opt-out: set `disable_orb_window: true` (default `false`) to skip the entire opening window (09:30 → `orb_end_time`, i.e. range formation + the ORB regime) and start trading at `orb_end_time` — distinct from the `orb_bypass_*` family which loosen the shared finalize filters DURING the ORB window. Useful on tapes where the opening 30 minutes are too whippy.

### 4. Index confirmation gates directional entries

Each stock is gated by its own group's tape, not an arbitrary broad-market ETF. The live path is peer breadth across the symbol's `sector_groups` peers (section 17); the group's ETF from `sector_index_map` — SMH for `ai_hardware`, XLK for `platforms`, IGV for `software` — is the fallback when peer bars are missing. Symbols whose group isn't mapped fall back to the universe-wide `index_symbols` list. For trend, pullback, vol_squeeze, and momentum entries, at least one mapped index must agree with the trade direction:

- Long: index close > VWAP and EMA9 >= EMA20
- Short: index close < VWAP and EMA9 <= EMA20

Range and sr_scalp entries do not require index confirmation (both are mean-reversion theses). Range entries get a bonus when indices are neutral (both near VWAP); sr_scalp's index exemption matches range's reasoning — index direction is orthogonal to a between-zone scalp.

### 5. Each regime builds a different signal

- **Trend signal**: requires a breakout above (long) or breakdown below (short) the recent swing high/low. Stop at the recent low/high + ATR buffer. Target at the configured R:R ratio.
- **Pullback signal**: stop at the recent extreme + buffer. Target extended to the prior swing point or the R:R target, whichever is more aggressive.
- **Range signal**: enters near range low (long) or range high (short). Stop outside the range boundary. Target at the opposite range boundary.
- **Vol-squeeze signal**: stop just outside the compression box (low for long, high for short) buffered by `max(0.12·ATR, 0.10%·price, 0.22·box_range)` so tight squeezes don't get over-wide ATR-based stops. Target at `vol_squeeze_target_rr` (default 2.05).
- **Momentum signal**: requires a fresh N-bar breakout (1m frame, `momentum_breakout_lookback_bars`, default 6) on the active session frame. Stop anchors below the recent swing low (long) / above recent swing high (short) with an ATR cushion (0.08·ATR) so single-bar wicks during midday/afternoon thin liquidity don't trigger the stop. Target at `momentum_target_rr` (default 2.0).
- **Sr-scalp signal**: enters near the entry-side HTF zone — `HS` (`sr_ctx.nearest_support`) for long, `HR` (`sr_ctx.nearest_resistance`) for short. Stop just outside the entry-side zone: `HS_zone_lower − level_buffer` (long) / `HR_zone_upper + level_buffer` (short), where `level_buffer = sr_ctx.level_buffer * vol_widening` — the same buffer the shared entry stage's S/R stop refinement uses to nudge stops past structural levels. Target at the inner edge of the opposite zone: `HR_zone_lower − level_buffer` (long) / `HS_zone_upper + level_buffer` (short) — exits at the inside of the opposite zone, matching the bot's structural-exit conventions elsewhere. No fixed R:R target — the zone gap (filtered by the distance gate) provides the reward.

All of them pass through `_finalize_signal`: top_tier's own gates, then the shared entry stage (`entry_policy.admit` — the global `shared_entry` vetoes, the stop / target refinement, the entry-context and FVG score terms), then exhaustion, bonuses, the ladder and adaptive management, and `entry_policy.emit` builds the signal (section 6).

### 6. Shared gates apply to every signal

Before any signal is emitted, the finalize pipeline applies:

- **HTF bias alignment (`require_htf_bias_alignment`, default true)**: reject longs when 15m market structure is bearish, and shorts when 15m is bullish. Neutral HTF never blocks. Prevents counter-trend entries that look good on the 1m/5m chart but fight the 15m trend. Set `false` if you want the bot to take setups regardless of the higher timeframe.
- **ORB HTF bypass (`orb_bypass_htf_bias`, default true)**: skip the HTF bias check during the ORB window (through `orb_end_time`). At the open, the 15m chart has zero or one completed bars from today — the structure is stale (yesterday's pivots). The ORB regime's range-break proves direction. After the ORB window, the filter resumes with 2-3 closed 15m bars.
- **ORB exhaustion bypass (`orb_bypass_exhaustion`, default true)**: skip the VWAP/EMA extension filters during the ORB window. After an opening dump, VWAP is artificially depressed and recoveries look "extended" when they're really the trend establishing itself. After the ORB window, VWAP reflects today's action and the filter becomes meaningful.
- **ORB structure / S/R exemption (manifest `capabilities.shared_entry.exemptions: {orb: [structure, sr]}`)**: the shared LTF market-structure and S/R vetoes skip the orb regime. (The LTF structure runs on the 5m frame under `support_resistance.structure_ltf_timeframe_minutes: 5` — Fix D, 2026-05-27. Since 2026-09-25 that frame's still-forming bucket confirms no swing pivot; its close and any break on it still count at once.) The opening dump candle registers as CHoCH_down on the LTF chart, and a dump through yesterday's low flips `breakdown_below_support`; both block LONG entries for several bars after the recovery is already underway, while the ORB regime's range-break already proves direction. Until 2026-09-24 this was the in-window `orb_bypass_structure_entry` / `orb_bypass_sr_entry` params (retired: a preset still carrying either fails at load); the orb regime only exists in the ORB window, so the exemption keyed on the regime is the same bypass. Remove the entry from the manifest to hold orb to both vetoes.
- **Structure-veto exemption for range / pullback / sr_scalp (manifest `range: [structure]`, `pullback: [structure]`, `sr_scalp: [structure]`; user decision, 2026-09-25)**: the shared LTF market-structure veto skips these three regimes; `trend`, `momentum`, `vol_squeeze` and `vwap_reclaim` keep it. They enter against the short-term swing by design, and the same engine already treats them that way (Fix D exempts range and sr_scalp as mean reversion; the structure exit gives pullback its own grace). The 5m bias the veto reads is mostly a location reading: `_resolve_structure_bias` tests the midpoint of the last swing range before the swing labels, so an HH / HL uptrend that pulls back into the lower half of its last swing reads bearish, and the veto refuses a LONG on bias alone. Over 162 archived entries it blocked 20 (range 7, pullback 6, vol_squeeze 5, trend 1, vwap_reclaim 1) that averaged +0.41R, against -0.14R for the entries it kept (CI of the difference [+0.22, +0.80]); 19 of the 20 predate Fix D, when the 1m structure was nearly inert. With the exemption the veto blocks 7 of them, and about +2.1R of what it releases is not also refused by the S/R or chart veto. The S/R, chart and other vetoes still reach all three. `small_cap_squeeze` carries the same three exemptions (inert there: its preset runs none of the three).
- **S/R-veto exemption for vwap_reclaim (manifest `vwap_reclaim: [sr]`, top_tier only; user decision, 2026-09-25)**: the shared HTF S/R clearance veto skips `vwap_reclaim`. The regime enters on the bar that closes back across VWAP after a flush, which on a large cap sits right next to an HTF level, and the veto reads that bar either as a pending, crossed-but-unconfirmed level (always too close) or as inside the 0.25% / 0.72 ATR minimum clearance. On 09-21..09-24 it refused 15 of the 16 vwap_reclaim entries (11 winners, 4 losers, +6.74R), which left the regime effectively off; the live bot, which never ran this veto state, traded all 16 for +6.94R. The veto still helps the other regimes (-14.95R blocked outside vwap_reclaim over 174 archived entries), so only vwap_reclaim is exempt. The S/R stop / target refinement, the ladder rungs and the structure and chart vetoes still apply to it. `small_cap_squeeze` keeps the veto on vwap_reclaim: there it refused only losers (3 of 8 in June, -2.45R). The evidence is thin (4 sessions, 16 entries, 9 of them on 09-23), so watch vwap_reclaim in the next dry-run.
- **ORB opposing-level block (`orb_opposing_sr_atr_mult`, default `0.5`)**: backstops the exemption. An orb LONG with the nearest resistance (SHORT: support) within `orb_opposing_sr_atr_mult` × ATR — or with a PENDING opposing level, crossed but not yet confirmed as flipped — is refused as `long_orb_opposing_resistance_within_<mult>atr` / `short_orb_opposing_support_within_<mult>atr` (2026-04-17 NFLX short 0.64 under a just-broken support; AAPL long just over a falsely broken resistance). `0` disables it. Keyed on `regime == "orb"` since 2026-09-24.
- **ORB screener-bias bypass (`orb_bypass_screener_bias`, default true)**: restore fallthrough to the opposite side during ORB so Fix A doesn't block gap-reversal trades. `change_from_open` is dominated by the opening gap in the first 30 min — a gap-down day that reverses (TSLA 2026-04-15 $367→$362→$394) correctly belongs to LONG even though the screener tagged SHORT. Post-ORB the screener's directional read is respected. Set `false` to enforce screener bias during ORB too.
- **The shared entry stage** (`entry_policy.admit`, 2026-09-24 — the global `shared_entry` knobs, applied the same way to every strategy): first the raw R:R gate a builder asks for (`<side>_orb_measured_move_exhausted(...)` for orb; `<side>_stop_floor_kills_rr(...)` for sr_scalp when its noise floor moved the stop), then every switched-on veto in order — LTF market structure (the 5m frame here, Fix D), HTF S/R clearance, the broken-level guard, the opposing chart pattern, the dual RSI+OBV divergence, the opposing candle cluster — all of them evaluated, so the refusal carries every blocker, while the queue loop's decision log keeps its primary (one reason per (side, regime) attempt, as before); then the S/R and technical refinement of stop/target and the entry-context + FVG score terms. The shipped preset runs structure, S/R and the chart veto (`use_opposing_chart_filter: true` since 2026-09-24; top_tier never read that knob before); the broken-level guard, dual divergence and candle vetoes ship off. A refusal lands under the regime and the queue loop falls through to the next (side, regime), as for any builder rejection.
- Entry exhaustion filters (VWAP extension, EMA9 extension, bar range, wick fraction — bypassed during ORB when `orb_bypass_exhaustion` is true), after the refinement
- Structure / chart-pattern / candle bonuses, read off the contexts the stage judged
- Adaptive ladder, trail runner, Fix G and the adaptive management metadata (breakeven, profit lock, runner extension thresholds), all from the refined stop and target
- `entry_policy.emit` builds the signal: `strategy_priority_score` (regime score + activity + bonuses), `shared_context_score` (entry context + FVG), `final_priority_score` (their sum — the same total the strategy built itself before), `entry_style_family` (the regime; the exit side's ORB and pullback graces key on it) and `orb_window_entry` (the orb family)

#### 6a. 2026-04-22 quality gates (Fix A/D/E)

A post-mortem on the 2026-04-20 afternoon (4 LONGS, 3 stopped out, 1 time-stop bail) surfaced three systematic patterns. Each is now a config-gated filter:

- **`respect_screener_bias`** (default `true`) — **Fix A (soft bias)**. *Refactored 2026-05-12 from hard lockout to score penalty.* Both sides are always evaluated. When the side being evaluated DISAGREES with the candidate's live bias, each regime score for that side is reduced by `bias_penalty_base * min(1.0, |day_strength| / bias_penalty_saturate_at)` (defaults 1.0 / 2.0%) BEFORE the score-gap auction. Weak counter-bias setups get filtered (penalty drags them below their `min_*_score` threshold); strong structural setups still qualify. Live bias is computed via `_compute_live_directional_bias`: `day_strength = (close − session_open) / session_open * 100`, returning LONG when above `+directional_bias_min_day_strength` (default `0.20%`) and SHORT when below `−directional_bias_min_day_strength`. The screener's pre-computed `c.directional_bias` still drives the gatekeeper's per-side cooldown lookup before `entry_signals` runs. Set `false` to disable the penalty entirely (no bias gating). The previous hard-lockout behavior was too rigid — it blocked legitimate counter-bias entries (e.g., bullish BOS + breakout on a mildly-negative day_strength) silently. The soft penalty preserves the 2026-04-20 fade-protection (deep day_strength → full penalty filters all but the strongest setups) while letting structural overrides through.
- **`bias_penalty_base`** (default `1.0`) — magnitude of the bias penalty applied to each regime score when the side disagrees with live bias. Higher values = stricter (fewer counter-bias entries); lower values = looser. Setting to 0 effectively disables soft bias gating (similar to `respect_screener_bias: false` but keeps trailing-bias memory active).
- **`bias_penalty_saturate_at`** (default `2.0`) — `|day_strength|` magnitude (%) at which the penalty saturates at `bias_penalty_base`. Below this magnitude, the penalty scales linearly. A 0.5% day at saturate_at=2.0 → penalty = 0.25 (mild). A 3% day → penalty = 1.0 (full, since 3.0 > 2.0).
- **`reject_stretched_entries`** (default `true`) — **Fix D#1**. Blocks trend/pullback entries where `tech_bollinger_percent_b` is at the opposite Bollinger band AND `tech_atr_stretch_ema20_mult` is ≥ `stretched_atr_mult_max`. Thresholds (tightened 2026-04-24 after morning session): `stretched_percent_b_max: 0.80` (LONG blocked if pct_b ≥ 0.80 near upper band; SHORT blocked if pct_b ≤ 0.20 near lower band), `stretched_atr_mult_max: 1.1`. Range regime is EXEMPT — range is mean-reversion, "stretched at top" IS the range short setup.
- **`reject_tech_bias_contradiction`** (default `true`) — **Fix D#2**. Blocks trend/pullback LONGS when `tech_dmi_bias == "bearish"` OR `tech_obv_bias == "bearish"`. Mirror for SHORTS. Caught the 2026-04-20 META LONG where DMI and OBV both flashed bearish but the regime scorer still went LONG.
- **`require_htf_pivot_alignment_trend`** (default `true`) — **Fix E**. Extends the pre-existing pullback-only HTF pivot-bias check to trend entries. Blocks LONG when `mshtf_pivot_bias == "bearish"` (LH/LL+EQL pattern) or SHORT when `pivot_bias == "bullish"` (HL+HH/EQH). Trend regime used to skip this check because it already requires a fresh breakout; real-world data showed the fresh breakout can still lose when HTF pivots oppose.
- **`afternoon_include_range`** (default `true`) — re-enables range regime in the 13:00-15:00 window. Pre-2026-04-22 afternoons were `{trend, pullback}` only; range-bound afternoon tapes forced trades into wrong regimes. With range allowed, stretched-at-top setups now generate range SHORTS instead of being misclassified as trend LONGS.

All five are independently toggleable via `params` in `configs/config.top_tier_adaptive.yaml` so you can A/B them across sessions.

**ORB-window bypass companions.** The surviving `orb_bypass_*` flags loosen the *shared* finalize filters that read stale at the open and apply to whatever runs in the ORB window (now the ORB regime): `orb_bypass_htf_bias`, `orb_bypass_exhaustion` (both default `true`; the structure and S/R vetoes are skipped by the manifest exemption above since 2026-09-24), plus `orb_bypass_screener_bias` / `orb_bypass_side_decision` / `orb_bypass_relative_strength` which let both break directions through at the gap-dominated open. The Fix D#1 (`orb_bypass_stretched_filter`), Fix D#2 (`orb_bypass_tech_bias_contradiction`), `orb_bypass_index_confirmation`, `orb_bypass_entry_confirmation_bar`, and `orb_bypass_oversized_entry_bar` companions were removed (2026-05-29) — the ORB regime isn't in those gates' regime sets, so those bypasses were dead code.

#### 6b. 2026-04-23 gates

Post-mortem on the first dry-run (19 trades, 26% WR, -$231 on range-heavy afternoon tape) added four more filters:

- **Broken-level guard** (`reject_entry_near_broken_level` until 2026-09-24; now the shared `shared_entry.use_broken_level_guard`, with the thresholds moved to `shared_entry.broken_level_min_clearance_pct` / `_atr`). Entry-side mirror of the `resistance_break_exit` / `support_break_exit` gates in `shared_exit.SharedExitPolicy`. Rejects SHORT when `sr_ctx.broken_resistance` sits above entry within `broken_level_min_clearance_pct` (default `0.0025` = 0.25%, this preset `0.0035`; scaled by the symbol's `vol_scale`) OR `broken_level_min_clearance_atr` (default `0.72`, this preset `0.90`). Symmetric for LONG on `broken_support`. Fires across all regimes. Would have blocked 2026-04-23 NVDA 09:35 SHORT (level $0.04 above entry) and HD 14:12 SHORT (level $0.26 above entry), a combined -$62.54 of avoidable losses. **Ships off** in every preset since 2026-09-24: it existed to keep entries clear of the S/R-loss exit, which ships off too.
- **`trailing_bias_enabled`** (default `true`). Adds per-symbol trailing-bias memory to Fix A. The strategy keeps a `deque(maxlen=trailing_bias_lookback)` (default 10) of the LIVE bias, one observation per LTF bar (a later cycle on the same bar updates that bar's read; the memory starts empty each session), so the default spans the last 10 bars. When the current live bias is `None` but ≥70% (`trailing_bias_majority_threshold`) of the recent directional reads were one side, that side becomes the effective bias for the soft `_bias_penalty` (it no longer restricts `preferred_sides`). Written for the 2026-04-23 GOOG 12:51 LONG pullback that fired into 10 consecutive SHORT-biased bars. Until 2026-09-22 it appended one observation per loop cycle, so the memory actually spanned 20-30 seconds.
- **`adaptive_partial_breakeven_rr` / `adaptive_partial_breakeven_offset_r`** (defaults `0.5` / `0.0`). A third adaptive-management tier sitting below the existing breakeven (`1.0R`) and profit_lock (`1.3R`). Moves the stop to `entry + offset * initial_risk` when `max_favorable_r` first crosses the threshold. Only 3 of 19 trades on 2026-04-23 reached the 1.0R breakeven gate, leaving modest-peak winners (AVGO 0.82R, RBLX 10:00 0.56R, COST 09:51 0.56R) unprotected. Set `adaptive_partial_breakeven_rr: null` to disable.
- **`range_require_prev_bar_confirmation`** (default `true`). Applies to `_build_range_signal` only. Requires the last COMPLETED bar's close (`session_frame.iloc[-2]`) to also sit in the entry zone — filters single-tick whipsaws where an in-progress bar briefly crosses the range-edge threshold but closes back mid-range. All 7 red-from-tick-one losers on 2026-04-23 (AMZN 10:07, COST 11:09/13:02/15:15, LOW 13:08, HD 14:12 SHORT, V 14:14 SHORT) fit this pattern.

All four are toggleable — the broken-level guard in `shared_entry` (off), the other three in `params` (default `true` for `top_tier_adaptive`). The partial-breakeven tier is also exposed via `strategy_base._build_adaptive_management_metadata` so other strategies can opt in.

#### 6c. 2026-04-24 exit-side fixes

First live-session post-mortem surfaced three exit bugs (not strategy-specific, but they bit top_tier_adaptive hardest because it runs in the ORB window):

- **Candle detection window (candles.py)**. Callers were pre-slicing `frame.tail(3)` before handing to `detect_candle_context`. TA-Lib candle functions build internal body-average/trend context from preceding bars; with 3 inputs it returned zeros even for textbook patterns. Fixed by adding `CANDLE_CONTEXT_BARS = 30` and having `detect_candle_context` slice internally, plus scanning `values[-1..-3]` in `_talib_pattern_value_from_key` so a pattern completing at bar N-1 (values[-2]) stays reportable for 1-2 cycles after it forms. Before: INTC 10:08 bullish engulfing was only reported during the single minute when 10:08 was the latest bar. After: stays visible through 10:10.
- **Anchored-VWAP instant exit (now `shared_exit.SharedExitPolicy._technical_exit`)**. AMZN 10:59 LONG exited at 13 s, META 09:35 SHORT at 55 s — both because entry fill was already on the wrong side of the AVWAP level, so the first tick triggered `anchored_vwap_loss_exit` / `reclaim_exit`. Fixed by adding an armed-guard: LONG requires `position.highest_price >= avwap_floor + buffer`, SHORT requires `position.lowest_price <= avwap_ceiling - buffer` before the exit can fire. Mirrors `trail_armed`. The exit reads the position for the guard.
- **ORB-entry exit grace (`orb_entry_exit_grace_minutes`, default `20`)**. 5 of 6 ORB-window entries on 2026-04-24 exited at a loss during pullbacks, with price recovering after. INTC at 2.0m via chart_pattern_exit, AMD at 11.2m via structure_bearish_exit. Added a config-gated grace window that suppresses `chart_pattern_exit` entirely AND gates the bias structure exit for ORB entries. Since 2026-09-24 it keys on `metadata['entry_style_family'] == 'orb'` (every opening-range entry, not only this strategy's). The CHoCH exit is not graced: it needs only a CHoCH that happened after entry. Set `0` to disable.
- **Pullback-regime exit grace + BoS confirmation (`structure_exit_grace_minutes_pullback` default `15`, `structure_exit_require_bos_confirmation` default `true`)**. 2026-05-14 AMD 14:36 LONG (pullback) was killed at hold=10.2m via `structure_bearish_exit:EQL` — the exit barely cleared both legacy gates (10min/2-pivot). The LTF formed a single EQL pivot, bias flipped bearish, exit fired. Price recovered to ~$452 (past R1 $450.10) shortly after. Two layered fixes: (1) pullback regime gets a longer grace (15min) because pullback by design enters into LTF chop; (2) the bias-flip exit now additionally requires an active BoS event (`bos_down` for long, `bos_up` for short), not just bias flipping on a single pivot. CHoCH exits remain unaffected. Both knobs live on `support_resistance` and apply across all strategies — the pullback-specific grace fires only for pullback entries (`position.metadata.entry_style_family == "pullback"` since 2026-09-24: this strategy's pullback regime and `rth_trend_pullback`; it keyed on `regime == "pullback"` before), so other entries see no change from the grace gate (BoS confirmation applies globally).

#### 6d. 2026-04-24 PM — Fix G: target-inside-SR gate (entry-side)

Written for the morning 2026-04-24 COST LONG at 1013 that exited via `time_stop:45m` for -$53: 1.06 ATR clearance below the 15m resistance (1014.94) PASSED `entry_min_clearance_atr: 0.72` (a *floor* on SR clearance). The original write-up said the computed target sat past that resistance because the S/R refinement's capped target failed `_target_meets_min_rr`. **Corrected 2026-09-25:** the session archive shows COST entered as an adaptive-ladder **trail runner** with no target at all (the resistance sat under the 1.2R `ladder_min_target_rr` floor, so no rung qualified), and Fix G is inert for runners — the gate never covered its own incident. Covering runners would be a new block and is not added.

- **`reject_target_beyond_sr`** (default `true`). A *ceiling* complement to `entry_min_clearance_atr`. For **trend entries only**, computes `dist_to_target = |target - close|` and `dist_to_sr = |opposing_sr_price - close|` (nearest_resistance for LONG, nearest_support for SHORT) and rejects when `dist_to_target > dist_to_sr * target_max_sr_ratio`. Range regime is exempt (range targets ARE the opposite SR by design). Pullback regime is exempt per initial scoping; can extend later if the pattern shows up there. What it refuses is a take-profit that only pays if price punches through an opposing level the trade does not manage.
- **`target_max_sr_ratio`** (default `0.8`; the top_tier preset ships `0.7`, small_cap_squeeze `0.8`). The ceiling — `0.8` enforces a 20% head-room buffer (target must fit within 80% of the distance to SR). Tighten to `0.5` for a 50% buffer; relax to `1.0` to only reject targets strictly past SR (not recommended — at-resistance targets still need to punch through). In `adaptive_ladder` mode it only matters if set to `1.0` or higher (see below).

**Placement note.** Fix G runs AFTER `_apply_ladder_if_enabled` and the runner-override so `target` is the trade's FINAL take-profit: `None` (runner mode → gate inert), `rungs[0]["price"]` (ladder active → checks the actual rung), or refined initial (non-ladder mode). The gate does not kill runner-eligible trades — runners trail out via stop, so the SR ceiling doesn't apply.

**Ladder mode (fixed 2026-09-25).** The rung builder draws its rungs from the same S/R list whose head is `nearest_resistance` / `nearest_support`, so rung 1 always sits AT or PAST the nearest level and the ratio is ≥ 1.00 by construction. At the shipped 0.7 / 0.8 ratios the gate therefore refused every laddered trend entry — live in top_tier since 1.0.0 and in small_cap_squeeze since it shipped — and trend traded only as a trail runner (every one of the 25 live Fix G refusals, 04-27 → 09-21, had a ratio ≥ 1.00; 10 were exactly 1.00). Now:

- A first rung **on** the nearest level passes: it is the ladder's own take-profit at that level, and the ladder manages it (zone-flip stop promotion and the next rung; a strong push suppresses the target exit). A 1e-6 tolerance covers the rung builder's `round(price, 6)`.
- A first rung **past** a nearer level that did not qualify as a rung (its R:R under `ladder_min_target_rr`) is still refused — that nearer level is unmanaged and sits short of the target (ratio > 1.00).
- Non-ladder mode, the runner exemption and the range / pullback exemptions are unchanged.

No ORB bypass — structural soundness of target vs. SR is timing-independent.

#### 6e. 2026-04-24 PM — Fix H: reject range entries during Bollinger squeeze

*Threshold recalibrated 2026-09-22 — see below; the mismatch this section describes was real, but `bollinger_squeeze_width_pct: 0.06` made the guard fire on 99.87% of bars rather than on squeezes.*

Afternoon live-session trade (NFLX 13:22 SHORT, -$11.34 in 2.2 min) surfaced a structural mismatch: the range regime qualified and prev-bar confirmation passed, but the underlying tape was in a `bollinger_squeeze` (compressed volatility). Range mean-reversion needs oscillating vol; a squeeze typically resolves via breakout in the opposite direction. NFLX entry context showed `bollinger_width_pct: 0.0015` (0.155%), `atr14: 0.044` on a $92 stock — a 12-cent range where stops and targets are both 1-2 ticks away. R:R math was fine (2.27) but absolute edge was swallowed by noise.

- **`reject_range_during_squeeze`** (default `true`). In `_build_range_signal`, after the insufficient-bars check, read `tech_ctx.bollinger_squeeze`. If true, skip the entry with reason `range_bollinger_squeeze(width_pct=X)`. Disable via `reject_range_during_squeeze: false`.

**`bollinger_squeeze_width_pct` recalibrated (2026-09-22).** `bollinger_width_pct` is `(bb_upper - bb_lower) / bb_mid`, a FRACTION of price. Across 162,056 RTH 1m bars over 17 archived sessions (2026-05-01 .. 09-21) it runs median `0.0041`, p75 `0.0071`, p95 `0.0167`, max `0.236` — so the shipped `0.06` flagged **99.87%** of all bars as "in a squeeze". The flag PARTITIONS the two regimes (`_score_vol_squeeze` trades compression, `_build_range_signal` refuses it), so at 99.87% vol_squeeze owned the whole tape and `range` owned none: 387 of 387 range build failures in one session were `bollinger_squeeze`, and the regime has one trade in the entire archive. The preset now sets `0.0025`, the p25 of the measured distribution — a squeeze is the tightest quarter of the tape. `vol_squeeze_max_width_pct` moves `0.05` -> `0.0035` for the same units reason: it is the OR-branch of the same test, and at `0.05` it was true on ~100% of bars, making `_score_vol_squeeze`'s +1.0 compression point and +0.5 "both agree" bonus free. This is a preset change only — the code default in `config.py` is untouched, so other strategies are unaffected.

No ORB bypass — squeeze is a volatility state, not a time-of-day artifact.

#### 6f. 2026-05-26 entry-quality gates + low-tier peak-giveback

Post-mortem on 2026-05-26 (7 LONGs on a +1.2% XLK day, 1W/6L, -$177.66) surfaced three entry-side leaks and one exit-side leak. The session's losing pattern: stocks were under-performing their sectors (INTC at +0.21% vs XLK +1.27%, NEM at -0.19% vs XLB +0.67%) and the bot bought "pullbacks" that were actually rollovers. The new gates are designed to surface those structurally weak entries before they reach scoring.

- **`bias_penalty_saturate_at` tightened: 2.0 → 0.75** (manifest default). The original 2.0 saturation assumed daily moves regularly hit ±2%; intraday reality on most days is 0.3-1.0%, where the penalty produced was 0.15-0.50 — not enough to filter weak-bias setups. At 0.75, a -1% day applies full 1.0 penalty (was 0.5); a -0.5% day applies 0.67 (was 0.25). The HIGH_VOL preset overrides this back to 2.5 because high-vol days routinely produce 2-3% day_strength. *(Note: with `require_explicit_side_decision: true` — default — only one side gets evaluated per candidate, so the soft penalty applies to a side that never gets scored. Kept active as fallback when the explicit decision is disabled.)*
- **`relative_strength_block_threshold_pct`** (default `0.5`). Filters `preferred_sides` based on stock-vs-sector intraday relative strength. Computes `rel_strength = day_strength − sector_day_strength` (sector ETF from `_indices_for_symbol(symbol)`'s first available frame). When `rel_strength ≤ -threshold`, LONG is removed from `preferred_sides`; when `≥ +threshold`, SHORT is removed. If `preferred_sides` is empty afterward, the candidate is skipped. Companion `orb_bypass_relative_strength` (default `true`) — the first 30 minutes of trading are too noisy for a stock-vs-sector divergence read. Catches the 5/26 INTC/NEM pattern where the symbol was drifting at +0.1% while its sector was up +1%. The lone winner that day (META, +RS 0.25%) passes through.
- **`stretched_cooldown_minutes`** (default `3.0`). Hysteresis on `reject_stretched_entries` (section 6a, Fix D#1). The `stretched_percent_b_max` / `stretched_atr_mult_max` thresholds are crisp — a single tick across relaxes them while the structural condition (price stretched above EMA20 / pinned to upper Bollinger band) is still active. AMZN on 5/26 was rejected at 10:11:41 with `pct_b=0.851` then entered 46 s later as the close ticked back across, losing $28. The cooldown stamps the failure timestamp and rejects subsequent stretched checks within the window. Per-symbol regardless of side. Disable with `stretched_cooldown_minutes: 0`.
- **`pullback_require_fresh_leg`** (default `true`; 2026-05-27 addition). Pullback works when the prior leg is YOUNG and the retracement shallow; it fails when the trend is stale and price has given back most of the move. Reject pullback when BOTH `(bars_since_session_extreme * ltf_minutes) > pullback_max_minutes_since_session_extreme` (default `45`) AND `retracement_from_extreme_pct > pullback_max_leg_retrace_pct` (default `50`). AND-logic on purpose — fresh-but-deep retracements and old-but-shallow ones still trade. NEM on 5/27 LONG at 14:48 was 400 min past session high with 140% retracement (price had fallen below the anchor low) — exactly the "multi-hour rollover dressed as a pullback" pattern this gate catches. Implemented via `_pullback_leg_context` which estimates `minutes_since_extreme` from bar count × ltf_minutes (avoids per-bar timestamp arithmetic) and computes the retracement against the lowest-low-at-or-before the high bar (mirror for SHORT). Disable with `pullback_require_fresh_leg: false`.
- **`require_explicit_side_decision`** (default `true`; 2026-05-27 addition — **Fix A**). Replaces the implicit "evaluate both sides per regime, pick the highest-scoring (side, regime) pair" with an evidence-based side decision computed BEFORE regime scoring. The old approach could pick SHORT just because the SHORT regime score was 0.5 higher even when every meaningful current-action signal said LONG. The new flow: `_decide_side` votes across four CURRENT price-action signals and either filters `preferred_sides` to a single decided side OR skips the candidate when signals are mixed. The wrong side is never evaluated. Votes contributed by: (1) recent return over `side_decision_recent_lookback_bars` (default `6` = 30 min at 5m), threshold `side_decision_recent_threshold_pct` (default `0.1`); (2) close vs session VWAP with `side_decision_vwap_buffer_pct` dead-band (default `0.0005` = 0.05%); (3) EMA9 vs EMA20 on the LTF; (4) last 3 LTF bars' green-count — **unanimous only** (3 green = LONG, 0 green = SHORT, mixed abstains; 2026-07-29, previously ≥2/≤1 which meant it always voted and let bounce bars out-vote the trend). Decision: a side wins when its votes ≥ `max(side_decision_min_agreeing, ceil(participating × side_decision_majority_frac))` AND opposing ≤ `side_decision_max_opposing` (default `1`). The threshold scales with the number of signals that actually voted — `recent` and `vwap` abstain ~35% of the time, and the previous absolute floor of 3 rejected unanimous 2-0 reads outright. Companion `orb_bypass_side_decision` (default `true`) skips the gate during the ORB window (through `orb_end_time`) (early-session signals are gap-dominated). 5/27 dry-run: NVDA SHORT (1-2 votes — mixed) and NEM LONG (2-2 tied) skipped, saved $114 net. Reuses `_recent_momentum_pct` for vote #1.
- **`require_entry_confirmation_bar`** (default `true`; 2026-05-27 addition — **Fix B**). Companion to Fix A. For direction-following regimes (trend / pullback / momentum / vol_squeeze), the LAST FULLY CLOSED LTF bar must confirm direction before `_build_<regime>_signal` is called: `last_closed.close > last_closed.open` AND `last_closed.close > prev_closed.close` for LONG (mirror for SHORT). Catches single-bar fakeouts where the in-progress bar tipped a score threshold but the actual completed bar didn't carry. Range and sr_scalp are EXEMPT — both are mean-reversion theses where the last closed bar moves AGAINST the entry direction by design. Implemented via `_entry_bar_confirms`. The ORB regime is also exempt (not in the confirmation-bar regime set) — its range-break is the confirmation.
- **Low-tier peak-giveback** — exit-side fix in `RiskConfig` (not strategy-side; see section 14). Catches 0.7-1.0R MFE trades that round-trip to BE before the main tier's `peak_giveback_min_r: 1.0` arms.

All gates are independently toggleable via the manifest (or `params` in `configs/config.top_tier_adaptive.yaml`). The relative-strength gate has its own `relative_strength_block_threshold_pct: 0.0` off-switch. The pullback maturity check disables via `pullback_require_fresh_leg: false`. Fix A/B disable via `require_explicit_side_decision: false` / `require_entry_confirmation_bar: false`.

**Cleanup note (2026-05-27 PM):** the originally-shipped hard-screener-bias-veto, pullback-bounce-confirmation, and recent-momentum-disagreement gates were removed as redundant with the explicit side-decision approach (Fix A subsumes the bias-vetting and recent-momentum signals via voting; Fix B subsumes the bounce confirmation via the last-closed-bar check applied to all direction-following regimes). On the 5/26 batch the RS gate + stretched cooldown still independently block NEM, INTC×2 and AMZN; FCX#1's rejection now routes through Fix A/B (current-action side vote + confirmation bar) rather than the removed in-progress-bar bounce check.

### 7. Candle pattern confirmation boosts signal priority

The last 3 bars of the 1-minute frame are evaluated for TA-Lib candlestick patterns. A confirmed pattern adds a priority bonus to the signal score:

- **strong_3c** (Morning Star, 3 White Soldiers, etc.): +0.40
- **solid_2c** (Engulfing, Piercing, Kicking, etc.): +0.25
- **weak_1c** (Hammer, Marubozu, Dragonfly Doji, etc.): +0.10

Candle patterns do not block entries unless `shared_entry.use_opposing_candle_filter` is on (off in the shipped preset; the shared entry stage's opposing-candle veto since 2026-09-24, top_tier's own filter before) — they only boost priority when multiple symbols compete for limited position slots. A clean regime + index confirmation + breakout is sufficient without candle confirmation.

### 8. Index symbols are automatically added to the watchlist

The ETFs configured in `index_symbols` (SMH / IGV / XLK in the shipped config) are added to the active watchlist so they receive history fetching, streaming, and appear in the bars dict. Without this, index confirmation would silently fail because `bars.get("XLK")` would return None for an AAPL trade.

### 9. Correlation concentration guard prevents correlated stacking

Two groupings exist and they are deliberately different:

- **`sector_groups`** - co-movement granularity (`ai_hardware` / `platforms` / `software`). Routes a symbol to its confirmation ETF (`sector_index_map`) and supplies the peer list for breadth confirmation.
- **`correlation_groups`** - risk granularity, coarser. Drives the concentration guard via `max_same_correlation_group_same_direction` (default 2).

They were one map until 2026-09-18, which under-counted risk: names across the tech / communication / consumer-discretionary line run roughly 0.85 correlated on any macro day, so treating them as independent sectors allowed a single directional bet to fill every `risk.max_positions` slot while appearing diversified - four tickers, one leveraged index bet, and `risk.max_daily_loss` reached in one move instead of four independent ones. On the Tech/AI universe the shipped preset therefore uses two risk buckets: `ai_complex` (semis + platforms, 20 names) and `software` (5), each capped at 2.

Every symbol in `tradable` must appear in some `correlation_groups` entry; an ungrouped symbol bypasses the guard entirely (pinned by `test_every_tradable_symbol_is_grouped`).

### 10. What a good setup looks like

A strong top-tier adaptive entry usually looks like:

- the stock has clear intraday direction confirmed by its sector ETF (per `sector_index_map`)
- the regime is unambiguous (score gap above the runner-up)
- the time of day matches the regime (not trying trend plays in the midday chop)
- the entry is not overextended from VWAP or EMA9
- market structure and S/R levels support the direction
- ADX shows trend strength (for trend/pullback regimes)

In plain English:

**"This strategy picks the strongest-moving top-tier stocks, figures out whether they are trending, pulling back, ranging, breaking out of a volatility squeeze, sustaining a directional move from the session open, or scalping between HTF support and resistance zones, confirms with the broader market (except for the two mean-reversion regimes), and enters only when the setup is clean and the time of day is right."**

### 11. How the screener ranks candidates

The screener fetches the fixed tradable list from TradingView and scores each symbol:

- **Directional bias**: `change_from_open > +0.20%` → LONG bias, `< -0.20%` → SHORT bias, else no bias. Bias determines which side is tried first but both sides are always evaluated.
- **Activity score**: `abs(change_from_open) × min(RVOL, 3.0)`. Higher activity = higher priority. RVOL is capped at 3.0 to prevent one spike from dominating.

Candidates are ranked by activity score and capped at `tradingview.max_candidates`.

### 12. How positions are managed after entry

Once a position is open, it goes through the adaptive management pipeline:

- **Breakeven** (`adaptive_breakeven_rr`): when unrealized hits 1.0R, stop moves to entry price.
- **Profit lock** (`adaptive_profit_lock_rr`): at 1.3R, stop moves to `adaptive_profit_lock_stop_rr` (0.35R above entry).
- **Runner extension** (`adaptive_runner_trigger_rr`): at 1.15R with strong FVG continuation bias + aligned structure, target extends and trailing stop activates.

#### Adaptive ladder (`risk.trade_management_mode: adaptive_ladder`)

When the global trade-management mode is `adaptive_ladder`, top_tier replaces its single target with a series of structural rungs derived from the active S/R context:

- **Rungs are walked outward from entry**: longs use `sr_ctx.resistances`, shorts use `sr_ctx.supports`. Only levels whose risk-to-reward (vs the entry stop) clears `ladder_min_target_rr` (default 1.2) are kept. The list is capped at `ladder_max_rungs` (default 4).
- **Each rung has a confirmation zone** of width `ladder_zone_atr_mult * ATR` (default 0.5 × ATR). The engine waits for price to flip the rung — closing through it on multiple bars — before promoting the trade.
- **On each confirmed rung**: stop advances up to the cleared zone (becoming the new defense), target advances to the next rung. The trade trails through structure rather than exiting at the first profit-take.
- **Final rung cleared** → target is removed and the position runs as a runner with the trailing stop set by the most recently cleared zone.
- **Tight-target paper-fill bug protection**: while price has touched the next rung but the zone hasn't flipped yet, target-hit exits are *suppressed* — the engine waits for structural confirmation instead of firing on transient ticks.

**Range regime is exempt from laddering.** The range thesis is "price oscillates between range_low and range_high" — laddering past range_high would chase a breakout that contradicts the entry. Range trades keep their single target at `range_high − buffer` and exit there.

If the S/R context produces no qualifying rungs (e.g. nearest resistance is below `ladder_min_target_rr`), the signal drops the fixed target entirely and becomes a **pure trail runner** — managed by trailing stop, breakeven/profit-lock ratchets, and structural exits (CHoCH, S/R loss). Runner extension is also disabled so it cannot recreate a fixed target later. This prevents a modest 2R target from prematurely closing a trend-day move (e.g. TSLA 2026-04-15: $365→$394 run that a 2R target would have exited at $372). A trend entry that does get rungs still faces Fix G on its first rung (section 6d): a first rung on the nearest level passes; until 2026-09-25 every laddered trend entry was refused there, so trend traded only as a runner.

Exits can also be triggered by:

- **Stop/target hit**: the primary exit mechanism.
- **Chart pattern exit**: opposing reversal or continuation pattern + tape weakness (disabled by default, enable via `shared_exit.use_chart_pattern_exit`).
- **Market structure exit**: CHoCH (Change of Character) in the opposing direction + tape weakness.
- **S/R level loss**: price breaks through a confirmed support/resistance level.
- **Force flatten**: fires `force_flatten_buffer_minutes` (default 5) before the management window closes, or earlier on early-close days (Jul 3, Black Friday, Christmas Eve).

### 13. Sector groups

Three co-movement blocks covering the 25-name Tech/AI universe. Every symbol in
`tradable` must appear in exactly one, and every block is large enough for peer
breadth (>= `index_breadth_min_peers`) so the ETF is only ever the fallback:

| Group                | ETF | Symbols                                                          |
|----------------------|-----|------------------------------------------------------------------|
| **ai_hardware** (12) | SMH | NVDA, AVGO, AMD, TSM, MU, QCOM, ARM, MRVL, INTC, ANET, VRT, DELL |
| **platforms** (8)    | XLK | AAPL, MSFT, GOOG, AMZN, META, NFLX, ORCL, TSLA                   |
| **software** (5)     | IGV | CRM, ADBE, NOW, PLTR, PANW                                       |

The table above is `sector_groups` - the ETF-routing / peer-breadth map (`ai_hardware` -> SMH, `platforms` -> XLK, `software` -> IGV). The concentration guard reads `correlation_groups` instead (see section 9): `ai_complex` (semis + platforms, 20 names) and `software` (5), each capped at 2 by `max_same_correlation_group_same_direction`. Groups are drawn by CO-MOVEMENT, not GICS: META/GOOG/NFLX are Communication Services and AMZN/TSLA Consumer Discretionary, but across this universe they trade as part of the mega-cap compute complex. Adding a symbol to `tradable` requires adding it to BOTH maps - its sector group (for ETF routing and breadth) and its correlation group (for the risk guard); `test_every_tradable_symbol_is_grouped` fails if you forget.

### 14. Recommended risk config

The shipped preset (`configs/config.top_tier_adaptive.yaml`) uses moderate risk settings tuned for a $25k account trading 23 liquid top-tier stocks:

| Risk param                        | Value   | Rationale                                                                                                                                                                    |
|-----------------------------------|---------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `max_positions`                   | 4       | 15 symbols × 3 sectors, 2 per sector = up to 6 qualify, 4 max open                                                                                                           |
| `risk_per_trade_frac_of_notional` | 0.8%    | Fraction of `max_notional_per_trade` risked per trade. At `max_notional_per_trade: 16000` that's $128 of risk per trade. Raises proportionally if you lift the notional cap. |
| `max_notional_per_trade`          | $16,000 | Hard cap per equity position — set by the shipped config; fits 40 shares of a $390 stock (MSFT).                                                                             |
| `max_total_notional`              | $68,000 | Aggregate cap across open stock positions.                                                                                                                                   |
| `max_daily_loss`                  | $500    | 2% hard stop for the day                                                                                                                                                     |
| `default_stop_pct`                | 1.4%    | Sized for realistic intraday top-tier ranges                                                                                                                                 |
| `default_target_pct`              | 2.8%    | Achievable on strong trend days                                                                                                                                              |
| `cooldown_minutes`                | 8       | Prevents revenge trading after a loss                                                                                                                                        |
| `peak_giveback_enabled`           | true    | Main-tier peak-giveback (50/40/30% tiered floor at 1R/2R/3R+ peaks)                                                                                                          |
| `peak_giveback_min_r`             | 1.0     | Main tier arms once peak crosses 1R. Tier 3b per-trade override (`peak_giveback_high_conviction_*`) raises to 2R on strong-bias days.                                        |
| `peak_giveback_low_tier_enabled`  | true    | **Added 2026-05-26.** Catches 0.7-1.0R MFE trades that the main tier (≥1R gate) misses. Skipped when the high-conviction override is active.                                 |
| `peak_giveback_low_tier_min_r`    | 0.7     | Peak threshold at which the low tier arms.                                                                                                                                   |
| `peak_giveback_low_tier_giveback_frac` | 0.7 | Giveback fraction for the low tier. `0.7` = 70% giveback (floor at 30% of peak). At 0.9R peak, exits when current_r ≤ 0.27R. Conservative to avoid clipping winners mid-run. |
| `peak_giveback_retain_1to2r` | 0.65 | **2026-05-27.** Main-tier retain fraction for 1-2R peaks (was 0.50). Floor = peak × 0.65. Tightened after data showed winners captured only 44% of MFE with no post-peak recovery in-sample. |
| `peak_giveback_retain_2to3r` | 0.72 | Retain fraction for 2-3R peaks (was 0.60). |
| `peak_giveback_retain_3r_plus` | 0.78 | Retain fraction for 3R+ peaks (was 0.70). Tune all three down if runners get clipped on normal pullbacks. |

### 15. When to start the bot

- **Best start time**: 09:20-09:25 ET — gives time for history backfill and sector-ETF data (XLK / XLE / etc.) before open.
- **Minimum practical start**: before 09:30 ET — the screener window opens at 09:30.
- Entries open at 09:35 in the shipped preset (`entry_windows`). The preset runs with `disable_orb_regime: true`, so there is no 09:30-09:45 opening-range carve-out and the normal regime mix is live from the open; with the ORB regime on, the ORB window would open at 09:45 (= 09:30 + `orb_range_minutes`) and nothing could enter before it. Practical entries begin once `min_bars` (150 one-minute bars) and `min_ltf_bars` (120 one-minute LTF bars) are met from the loaded history. With `required_bars: 150`, both gates clear on cold start from the prior session's data.
- With `runtime.auto_exit_after_session: true`, the bot shuts down cleanly after market close once all positions are flat. Designed for Windows Task Scheduler or cron to start the bot daily without manual shutdown.

## Shipped reference

Purpose: multi-regime adaptive strategy for a fixed list of 25 mega-cap Tech / AI stocks (`ai_hardware`, `platforms`, `software`).

Default windows:

- `entry_windows`: `[["09:35", "15:00"]]` in the shipped preset (2026-09-23; was 09:45). The manifest keeps `[["09:45", "15:00"]]` because its defaults leave the ORB regime on, and with ORB on 09:30-09:45 is a no-entry zone anyway
- `management_windows`: `[["09:30", "15:55"]]`
- `screener_windows`: `[["09:30", "15:00"]]`

Strategy-specific knobs:

- `tradable`: the fixed list of symbols to trade.
- `index_symbols`: index ETFs streamed for directional confirmation. Default `SMH` / `IGV` / `XLK`, one per group. Must include every ETF referenced by `sector_index_map`.
- `sector_index_map`: group → list of index ETFs to consult for confirming trades on symbols in that group (default `ai_hardware: [SMH]`, `platforms: [XLK]`, `software: [IGV]`). Falls back to OR-ing across all `index_symbols` when a group has no mapping.
- `require_index_confirmation`: gate trend/pullback/vol_squeeze/momentum/vwap_reclaim entries on index agreement. Range and sr_scalp are exempt (mean-reversion theses).
- `leg_anchored_confirmation`: measure the index/peer agreement test AND `_decide_side`'s VWAP arm against the current leg's anchored VWAP instead of session VWAP. Default `false`; `true` in the shipped preset. See section 18.
- `leg_anchor_min_age_bars` / `leg_anchor_min_impulse_pct`: how old and how large the leg must be before the anchor moves. Guards against reading an ordinary pullback as a reversal.
- `require_htf_bias_alignment`: reject longs against bearish HTF (15m) structure and shorts against bullish HTF structure. Neutral never blocks. Default `true` — prevents counter-trend entries on days when the higher-timeframe structure is pinned against the trade direction. Set `false` to allow counter-HTF setups (the bot will still score them normally, but won't outright block).
- `orb_bypass_htf_bias`: skip the HTF bias check during the ORB window (through `orb_end_time`). Default `true`. Set `false` to enforce HTF bias filtering even at the open.
- `orb_bypass_exhaustion`: skip the VWAP/EMA extension exhaustion filters during the ORB window. Default `true`. Set `false` to enforce exhaustion filtering even at the open.
- The LTF market-structure and S/R vetoes skip the orb regime through the manifest (`capabilities.shared_entry.exemptions: {orb: [structure, sr]}`), which replaced `orb_bypass_structure_entry` / `orb_bypass_sr_entry` on 2026-09-24. `orb_opposing_sr_atr_mult` (default `0.5`, `0` = off) still refuses an orb entry into a close or pending opposing level. Since 2026-09-25 the manifest also exempts `range` / `pullback` / `sr_scalp` from the structure veto and `vwap_reclaim` from the S/R veto (section 6). These are manifest declarations, not knobs: remove an entry to hold the regime to that veto again.
- `orb_bypass_screener_bias`: restore fallthrough to the opposite side during the ORB window so Fix A (`respect_screener_bias`) doesn't block gap-reversal entries. Default `true`. Set `false` to enforce the screener's directional_bias during ORB too.
- `reject_oversized_entry_bar`: reject entries when the last LTF 5m bar's range or body is too large relative to ATR. Default `true`. Catches the "5m close lag" chase pattern where the bot waits for the bar to close and enters near its high/low. Applies to `trend` / `pullback` / `sr_scalp` only; `range` / `vol_squeeze` / `momentum` are exempt because big bars ARE the setup for those regimes. Independent of `reject_stretched_entries` (which is keyed to Bollinger %B + ATR-from-EMA20) — this gate looks at the latest bar's OWN size, not its position relative to indicators.
- `entry_bar_range_max_atr_mult`: max bar range as multiple of ATR14. Default `1.8`. A bar with `(high - low) / atr14 >= 1.8` is rejected. Lower = stricter; relax to `2.5+` for very volatile tape.
- `entry_bar_body_max_atr_mult`: max bar body (|close − open|) as multiple of ATR14. Default `1.4`. Catches directional thrust bars (the ones that "ran") even when wicks are small. Lower = stricter.
- `min_trend_score` / `min_pullback_score` / `min_range_score` / `min_vol_squeeze_score` / `min_momentum_score` / `min_sr_scalp_score`: minimum regime score to qualify.
- `min_pullback_trend_score`: minimum trend score required before pullback scoring begins.
- `min_adx14`: ADX floor for trend/pullback scoring.
- `trend_target_rr` / `pullback_target_rr` / `vol_squeeze_target_rr` / `momentum_target_rr`: initial R:R targets per regime. Range and sr_scalp have no R:R target — range targets the opposite edge of the range, sr_scalp the inner edge of the opposite HTF zone (zone gap provides the reward).
- `stop_buffer_atr_mult`: ATR multiplier for stop buffer beyond the swing level.
- `orb_end_time` / `midday_start_time` / `midday_end_time` / `afternoon_start_time` / `no_new_entries_after`: time-of-day regime window boundaries.
- `vol_squeeze_lookback_bars` / `vol_squeeze_max_range_pct` / `vol_squeeze_max_range_atr` / `vol_squeeze_max_width_pct` / `vol_squeeze_breakout_buffer_pct` / `vol_squeeze_min_breakout_volume_ratio` / `vol_squeeze_min_bar_close_position`: vol_squeeze qualification knobs. **`vol_squeeze_max_range_atr` is the one that binds** — it compares a 12-bar box against a 1-bar `atr14`, the same units mismatch as `orb_max_range_atr_mult`. Measured over 167,447 RTH 1m bars across 18 archived sessions the ratio runs p5 `2.17` / p25 `2.81` / median `3.44` / p95 `5.75`, so the shipped `1.8` sat below the 5th percentile and only 0.79% of bars could ever be "compressed" — 3.4 candidates a session across 28 symbols. Recalibrated to `2.8` (the p25, matching the definition `bollinger_squeeze_width_pct` uses) on 2026-09-22, giving ~40. `vol_squeeze_max_range_pct` admits 95.6% at `0.012` and is an absolute backstop, not a selectivity gate.
- `momentum_breakout_lookback_bars` / `momentum_min_day_strength`: momentum qualification knobs.
- `sr_scalp_min_distance_pct` / `sr_scalp_min_distance_atr`: HTF zone-gap floors. The inner gap between HS and HR zones must clear BOTH (max wins) for the sr_scalp regime to qualify. Defaults `0.008` (0.8% of close) and `2.5` (2.5x ATR).
- `sr_scalp_max_distance_from_zone_atr`: proximity gate — close must be inside the entry-side zone OR within this multiple of ATR of its inner edge (default `0.5`). Mid-range candles don't qualify.
- `disable_trend_regime` / `disable_pullback_regime` / `disable_range_regime` / `disable_vol_squeeze_regime` / `disable_momentum_regime` / `disable_sr_scalp_regime`: per-regime opt-out flags (all default `false`).
- `disable_orb_window`: whole-window opt-out for the opening (range-formation + ORB) window through `orb_end_time` (default `false`). Different from the `orb_bypass_*` family which loosen filters within the window — this skips it entirely.
- `directional_bias_min_day_strength`: threshold (in %) for the live directional bias (default `0.20`). The strategy computes `day_strength = (close − session_open) / session_open * 100` from the LTF frame each cycle; bias = LONG when above `+threshold`, SHORT when below `−threshold`, else None. Drives Fix A side selection.
- `relative_strength_block_threshold_pct`: min absolute `(candidate_chg_from_open − sector_chg_from_open)` in % to hard-block a side (default `0.5`). LONG is removed from `preferred_sides` when rel_strength ≤ −threshold; SHORT when ≥ +threshold. Set to `0` to disable. See section 6f.
- `orb_bypass_relative_strength`: skip the relative-strength gate during the ORB window (through `orb_end_time`) (default `true`). Stock-vs-sector divergence is noisy in the first 30 minutes.
- `stretched_cooldown_minutes`: cooldown (in minutes) after a stretched-at-top/bottom build failure during which subsequent stretched checks hard-reject without re-evaluating thresholds (default `3.0`). Set to `0` to disable. Per-symbol regardless of side. See section 6f.
- `pullback_require_fresh_leg`: reject pullback entries when the prior leg is stale (default `true`; 2026-05-27 addition). Blocks pullbacks where BOTH minutes-since-session-extreme AND retracement % exceed their thresholds. See section 6f.
- `pullback_max_minutes_since_session_extreme`: age cap for pullback maturity check (default `45.0` minutes). Estimated as `bars_since_extreme * ltf_minutes`.
- `pullback_max_leg_retrace_pct`: retracement cap for pullback maturity check (default `50.0` percent of leg size).
- `require_explicit_side_decision`: route side selection through `_decide_side` (vote-based) instead of the implicit "highest regime score" approach (default `true`). See section 6f Fix A.
- `side_decision_recent_lookback_bars`: bars of LTF history used for the recent-return signal vote (default `30` = 30 min at `ltf_minutes: 1`).
- `side_decision_recent_threshold_pct`: threshold for the recent-return vote (default `0.1`). Below = neutral, above = LONG vote, below negated = SHORT vote.
- `side_decision_vwap_buffer_pct`: dead-band around session VWAP for the close-vs-VWAP vote (default `0.0005` = 0.05%). Within band = neutral.
- `side_decision_min_agreeing` / `side_decision_majority_frac`: decision threshold, as a majority of the signals that actually voted — `required = max(min_agreeing, ceil(participating × majority_frac))`, defaults `2` and `0.6`. Replaced the absolute `side_decision_min_votes` on 2026-07-29: with two of the four signals carrying neutral dead-bands, an absolute floor of 3 was unreachable whenever they abstained, which blocked shorts throughout the 2026-07-27..29 tech selloff (`side_undecided` was the single largest blocker, 3,506 of 12,935 tech decision rows).
- `side_decision_max_opposing`: maximum opposing votes allowed for a decision (default `1`). Tighter values demand cleaner consensus.
- `orb_bypass_side_decision`: skip the explicit side decision during the ORB window (through `orb_end_time`) (default `true`).
- `require_entry_confirmation_bar`: require the last fully closed LTF bar to confirm direction (green AND > prior close for LONG; mirror for SHORT) before entry on trend/pullback/momentum/vol_squeeze regimes (default `true`). See section 6f Fix B.
- `armed_retest_enabled` / `armed_retest_max_minutes` / `armed_retest_zone_atr` / `armed_retest_min_close_position` / `armed_retest_invalidation_atr`: the armed-retest trigger for `trend` and `momentum` - qualifying records the level that was cleared and waits for price to retest it, entering at market only if the wait expires (defaults `true` / `12.0` / `0.35` / `0.60` / `0.75`). See section 24.
- `sector_groups`: co-movement groupings - ETF routing (`sector_index_map`) and the peer list for breadth confirmation.
- `correlation_groups`: coarser risk groupings for the concentration guard.
- `max_same_correlation_group_same_direction`: max same-direction positions per correlation group.
- `volatility_scaled_thresholds` / `reference_adr_pct` / `volatility_scale_min` / `volatility_scale_max` / `adr_lookback_days`: per-symbol ADR scaling of every percent-of-price threshold (see section 16).
- `beta_lookback_days`: daily regression window for the sector-beta used by the relative-strength residual.
- `index_breadth_min_peers` / `index_breadth_min_agree_frac`: sector-breadth confirmation (see section 17).

Also uses these shared stock groups:

- force-flatten (configurable per side)
- entry exhaustion filters
- stock FVG confluence
- adaptive stock trade management
- the shared entry stage (`shared_entry.*`: the structure / S/R / broken-level / chart / dual-divergence / candle vetoes, stop/target refinement, entry-context + FVG scoring)
- the shared exit policy (`shared_exit.*`: chart pattern, market structure and S/R exits)

Current code defaults:

| Option                               | Default                                                                                                                       |
|--------------------------------------|-------------------------------------------------------------------------------------------------------------------------------|
| `tradable`                           | `AAPL, MSFT, GOOG, AMZN, META, NFLX, ORCL, TSLA, NVDA, AVGO, AMD, TSM, MU, QCOM, ARM, MRVL, INTC, ANET, VRT, DELL, CRM, ADBE, NOW, PLTR, PANW` |
| `index_symbols`                      | `SMH, IGV, XLK`                                                                                                               |
| `sector_index_map`                   | `{ai_hardware: [SMH], platforms: [XLK], software: [IGV]}`                                                                     |
| `early_session_stop_widening_enabled`| `true`                                                                                                                        |
| `early_session_stop_widening_until`  | `10:30`                                                                                                                       |
| `early_session_stop_widening_mult`   | `1.3`                                                                                                                         |
| `require_index_confirmation`         | `true`                                                                                                                        |
| `require_htf_bias_alignment`         | `true`                                                                                                                        |
| `orb_bypass_htf_bias`                | `true`                                                                                                                        |
| `orb_bypass_exhaustion`              | `true`                                                                                                                        |
| `orb_bypass_screener_bias`           | `true`                                                                                                                        |
| `reject_target_beyond_sr`            | `true`                                                                                                                        |
| `target_max_sr_ratio`                | `0.8`                                                                                                                         |
| `reject_range_during_squeeze`        | `true`                                                                                                                        |
| `min_bars`                           | `150`                                                                                                                         |
| `ltf_minutes`          | `1`                                                                                                                           |
| `ltf_indicator_span_scale` | `5`                                                                                                                       |
| `ltf_ema_fast_span` | `45`                                                                                                                       |
| `ltf_ema_slow_span` | `100`                                                                                                                      |
| `htf_ema_fast_span` | `50`                                                                                                                       |
| `htf_ema_slow_span` | `200`                                                                                                                      |
| `require_htf_ema_alignment` | `disabled` (`enabled` / `true`, `long_only`, `short_only`, `disabled` / `false`)                                 |
| `htf_ema_alignment_score` | `0.0`                                                                                                               |
| `htf_minutes`              | `15`                                                                                                                          |
| `min_ltf_bars`                   | `120`                                                                                                                         |
| `min_trend_score`                    | `3.5`                                                                                                                         |
| `min_pullback_score`                 | `3.5`                                                                                                                         |
| `min_pullback_trend_score`           | `3.0`                                                                                                                         |
| `min_range_score`                    | `4.0`                                                                                                                         |
| `min_vol_squeeze_score`              | `4.0`                                                                                                                         |
| `min_momentum_score`                 | `4.0`                                                                                                                         |
| `min_sr_scalp_score`                 | `3.0`                                                                                                                         |
| `min_adx14`                          | `15.0`                                                                                                                        |
| `pullback_ema_touch_atr_mult`        | `0.35`                                                                                                                        |
| `pullback_hold_atr_mult`             | `0.40`                                                                                                                        |
| `pullback_lookback_bars`             | `25`                                                                                                                          |
| `range_entry_zone_frac`              | `0.35`                                                                                                                        |
| `range_max_ema_gap_pct`              | `0.0008`                                                                                                                      |
| `range_min_flip_count`               | `3`                                                                                                                           |
| `range_lookback_bars`                | `20`                                                                                                                          |
| `trend_target_rr`                    | `2.0`                                                                                                                         |
| `pullback_target_rr`                 | `2.0`                                                                                                                         |
| `vol_squeeze_target_rr`              | `2.05`                                                                                                                        |
| `momentum_target_rr`                 | `2.0`                                                                                                                         |
| `vol_squeeze_lookback_bars`          | `12`                                                                                                                          |
| `vol_squeeze_max_range_pct`          | `0.012`                                                                                                                       |
| `vol_squeeze_max_range_atr`          | `2.8`                                                                                                                         |
| `vol_squeeze_max_width_pct`          | `0.0035`                                                                                                                      |
| `vol_squeeze_breakout_buffer_pct`    | `0.0008`                                                                                                                      |
| `vol_squeeze_min_breakout_volume_ratio` | `1.12`                                                                                                                    |
| `vol_squeeze_min_bar_close_position` | `0.63`                                                                                                                        |
| `momentum_breakout_lookback_bars`    | `6`                                                                                                                           |
| `momentum_min_day_strength`          | `1.5`                                                                                                                         |
| `sr_scalp_min_distance_pct`          | `0.008`                                                                                                                       |
| `sr_scalp_min_distance_atr`          | `2.5`                                                                                                                         |
| `sr_scalp_max_distance_from_zone_atr` | `0.5`                                                                                                                        |
| `sr_scalp_min_stop_atr_mult`         | `0.5`                                                                                                                         |
| `sr_scalp_noise_lookback_bars`       | `20`                                                                                                                          |
| `disable_orb_window`                 | `false`                                                                                                                       |
| `directional_bias_min_day_strength`  | `0.20`                                                                                                                        |
| `disable_trend_regime`               | `false`                                                                                                                       |
| `disable_pullback_regime`            | `false`                                                                                                                       |
| `disable_range_regime`               | `false`                                                                                                                       |
| `disable_vol_squeeze_regime`         | `false`                                                                                                                       |
| `disable_momentum_regime`            | `false`                                                                                                                       |
| `disable_sr_scalp_regime`            | `false`                                                                                                                       |
| `disable_vwap_reclaim_regime`        | `false`                                                                                                                       |
| `disable_orb_regime`                 | `false`                                                                                                                       |
| `armed_retest_enabled`               | `true`                                                                                                                        |
| `armed_retest_max_minutes`           | `12.0`                                                                                                                        |
| `armed_retest_zone_atr`              | `0.35`                                                                                                                        |
| `armed_retest_min_close_position`    | `0.60`                                                                                                                        |
| `armed_retest_invalidation_atr`      | `0.75`                                                                                                                        |
| `stop_buffer_atr_mult`               | `0.25`                                                                                                                        |
| `orb_end_time`                       | `10:05`                                                                                                                       |
| `midday_start_time`                  | `11:30`                                                                                                                       |
| `midday_end_time`                    | `13:00`                                                                                                                       |
| `afternoon_start_time`               | `13:00`                                                                                                                       |
| `no_new_entries_after`               | `15:00`                                                                                                                       |
| `entry_exhaustion_filter_enabled`    | `true`                                                                                                                        |
| `max_entry_vwap_extension_atr`       | `1.50`                                                                                                                        |
| `max_entry_ema9_extension_atr`       | `1.20`                                                                                                                        |
| `max_entry_bar_range_atr`            | `1.80`                                                                                                                        |
| `max_entry_upper_wick_frac`          | `0.30`                                                                                                                        |
| `max_entry_lower_wick_frac`          | `0.30`                                                                                                                        |
| `htf_fvg_entry_weight`               | `0.30`                                                                                                                        |
| `ltf_fvg_entry_weight`        | `0.18`                                                                                                                        |
| `opposing_fvg_entry_penalty_mult`    | `1.0`                                                                                                                         |
| `fvg_runner_rr_bonus`                | `0.15`                                                                                                                        |
| `activity_score_weight`              | `0.12`                                                                                                                        |
| `adaptive_breakeven_rr`              | `1.00`                                                                                                                        |
| `adaptive_profit_lock_rr`            | `1.30`                                                                                                                        |
| `adaptive_profit_lock_stop_rr`       | `0.35`                                                                                                                        |
| `adaptive_runner_trigger_rr`         | `1.15`                                                                                                                        |
| `max_same_correlation_group_same_direction` | `2`                                                                                                                     |
| `force_flatten`                      | `{'long': true, 'short': true}`                                                                                               |

## Files in this folder

- `manifest.json` defines the plugin registration metadata and factory defaults.
- `configs/config.top_tier_adaptive.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` fetches the fixed tradable universe from TradingView and ranks by activity.
- `strategy.py` contains the regime scoring, signal building, and entry logic.

---

## 16. Per-symbol volatility scaling (2026-09-18)

Every percent-of-price threshold is multiplied by `vol_scale` - the symbol's 20-day ADR (true range, so overnight gaps count) divided by `reference_adr_pct`, clamped to `[volatility_scale_min, volatility_scale_max]`.

Without it a single number is a different gate on every name. This universe spans roughly 1.3% ADR (AAPL, MSFT) to 4%+ (PLTR, ARM, MU), so:

- `momentum_min_day_strength` as a flat figure was a routine move on PLTR/ARM and a 2-sigma day on AAPL/MSFT - the momentum regime was structurally a high-beta-only strategy. Scaled, the written 1.8 lands near 1.1% on AAPL and ~3.4% on PLTR.
- `default_stop_pct: 0.010` is a tight stop on PLTR/ARM and a very wide one on AAPL/MSFT.

Scaled params: `default_stop_pct`, `momentum_min_day_strength`, `sr_scalp_min_distance_pct`, `relative_strength_block_threshold_pct`, `range_max_intraday_range_pct`, `vol_squeeze_max_range_pct`, `vol_squeeze_breakout_buffer_pct`, `vwap_reclaim_buffer_pct`, and `shared_entry.broken_level_min_clearance_pct` (the proposal carries the symbol's `vol_scale` into the shared broken-level guard).

**ATR-multiple params (`*_atr_mult`) are NOT scaled** - they are already volatility-relative and scaling them would square the adjustment.

Data comes from `MarketDataStore.get_daily_history` (one Schwab daily `price_history` call per symbol per ET day) via `daily_stats.build_symbol_stats`. When the fetch fails or returns too few sessions, `vol_scale` is 1.0 and thresholds are exactly as written - the pre-scaling behaviour, not an invented default.

This also retires the hand-maintained `HIGH_VOL:` overrides scattered through the preset: those exist because absolute thresholds do not survive a volatility regime change, whereas ADR-relative ones largely do.

## 17. Sector-breadth index confirmation (2026-09-18)

`_index_confirms` prefers **peer breadth** over the sector ETF whenever the symbol has at least `index_breadth_min_peers` peers with readable bars in its `sector_groups` entry: it counts how many of those peers lean the trade's way and requires `index_breadth_min_agree_frac` of them.

The ETF test is close to circular on a mega-cap universe - AAPL+MSFT+NVDA+AVGO are roughly 45% of XLK, GOOG+META about 45% of XLC, AMZN+TSLA about 40% of XLY. Asking XLK whether AAPL's move is confirmed substantially asks AAPL about AAPL, and it fails in the one case that matters: the mega cap moving against the rest of its sector. Breadth excludes the symbol itself.

Single-member sectors (healthcare/LLY, staples/COST here) have no peers and fall back to the original ETF check.

## 18. Reversal handling: leg-anchored confirmation + vwap_reclaim (2026-09-18)

A session that flushed and then turned produced **zero** entries on the recovering side. Driving a 3% flush that retraced 87% through the gates bar by bar, across the 90-bar recovery leg: 45 bars blocked on `index_not_confirmed`, 41 with no regime qualifying at all, 0 signals. Both directions.

The stale `day_strength` bias is *not* the cause, which is worth stating because it is the visible symptom. `day_strength` is anchored to the session open, so it still read SHORT with price 87% of the way back — but `_decide_side` voted the correct side on 75 of 90 bars, and an explicit side decision forces `bias_penalty` to `0.0`. The bias costs nothing.

Two changes, and they only work as a pair.

### The gate: `leg_anchored_confirmation`

`_frame_agrees` tests `close > vwap` — against **session** VWAP, a whole-day average. After a morning flush it sits far above price, so the confirmation only turns true long after the reversal is running. And because the same test is applied to every *peer*, the whole breadth gate inherits the lag.

| reference | reclaimed | move gone |
|---|---|---|
| session VWAP | +44 min after the low | 50% |
| VWAP anchored at the low | +1 min after the low | 1% |

With the flag on, `_leg_anchor_vwap` anchors at today's more recent extreme — the low in an up-leg, the high in a down-leg — and the reference falls back to session VWAP when no leg is established, which is the correct reference for a session that has not turned.

Two guards stop the anchor chasing a pullback and confirming the wrong side. `leg_anchor_min_age_bars` (20) requires the extreme to be old enough to be a confirmed pivot — a pullback high in a grind is only a few bars old, a reversal pivot is not. `leg_anchor_min_impulse_pct` (0.005) requires price to have travelled meaningfully from it.

Measured deltas vs session VWAP, in confirmed bars per tape: pure trend up/down **0**, grind-with-pullbacks **0**, chop **+1**, V-reversal LONG **+22**, inverted-V SHORT **+22**. It changes the answer only where the session actually turned.

`min_age_bars` is the guard that does the work — at 15 an ordinary grind produced 12 spurious counter-trend confirmations, at 20 none. Raising it trades reversal responsiveness for pullback immunity (V-reversal gain at 15/20/25/30 bars: +27/+22/+17/+12). **Known behaviour:** a grind carrying deep (~0.8%) pullbacks still confirms the counter side on some bars (+19 at 20 bars, +9 at 30) — a deep pullback genuinely resembles a reversal. The regime floors, HTF-bias, structure and side-decision gates all still apply downstream.

The flag defaults to `false`: `SmallCapSqueezeStrategy` subclasses this strategy and inherits the method, and its dry-run results must not move.

### The builder: `vwap_reclaim`

Fixing the gate alone still produced **0** signals — the blocker simply moved to `no_fresh_breakout`, 38 of 90 bars. Every other surviving regime triggers on a *breakout*: trend and momentum need a fresh N-bar high, pullback needs an established aligned trend. A reversal is not a breakout, it is a reclaim, so no builder recognised the shape.

`vwap_reclaim` is that builder, and it pairs with the gate — on its own it managed 2 signals, 12 of its bars blocked by the same lagging breadth check. It stays in `INDEX_CONFIRMED_REGIMES`; with the leg anchor in place that gate is no longer what blocks it.

| | LONG reversal | SHORT reversal |
|---|---|---|
| before | 0 signals | 0 signals |
| gate only | 0 | 0 |
| builder only | 2 | 2 |
| both | **18** | **15** |

Because it is momentum-family it is offered in the midday window too, where trend and range are not — which is when sessions most often turn.

The reclaim bar sits next to an HTF level by construction, so the shared S/R veto refused almost every entry (15 of 16 on 09-21..09-24). Since 2026-09-25 the manifest exempts `vwap_reclaim` from that veto (section 6); the structure and chart vetoes still apply.

Entries land at the VWAP reclaim, roughly the midpoint of the move, **not at the low**. Catching the turn itself is not the goal. Signal count is also not edge: 30 bars still die on `no_fresh_breakout`, which is correct behaviour for a trend builder.

Knob values are adapted from `config.small_cap_squeeze.yaml`, where the regime is already tuned and live. `vwap_reclaim_buffer_pct` is the one that needed rescaling — it runs through `_pct_param`, so it is multiplied by the symbol's ADR over `reference_adr_pct`. Small caps use 0.0025 against a far larger ADR; 0.0015 here lands near 0.10% on a low-ADR mega cap and 0.20% on a high-ADR one, enough to reject the one-tick VWAP poke that over-fired on small caps without demanding a mega cap clear VWAP by a small-cap margin.

### The vote reads the same reference

`_decide_side` votes on four current-action signals, and one of them is `close vs VWAP`. It was still measuring against **session** VWAP after the gate had moved off it, so the two layers disagreed about what "reclaimed VWAP" meant for the same symbol on the same bar. Auditing each arm against the tape's real direction:

| arm | wrong bars on a reversal (of 120) |
|---|---|
| `close vs VWAP` (session) | 54 |
| `EMA9 vs EMA20` | 59 |
| recent return | **14** |
| last-3-bar colour | 0 |

The two *level-comparison* arms are the least accurate on a reversal and also the two that vote most reliably — `recent` and `bars3` carry dead-bands and abstain often — so they outvote the one genuinely current-action signal.

Pointing the VWAP arm at `_leg_anchor_vwap` under the same `leg_anchored_confirmation` flag:

| tape | before | after |
|---|---|---|
| V-reversal | 66 ok / 36 wrong / 18 undecided | **98 ok / 21 wrong / 1 undecided** |
| inverted-V | 66 ok / 39 wrong / 15 undecided | **98 ok / 19 wrong / 3 undecided** |
| trend up/down, grind, chop | — | unchanged |

Its VWAP arm's own error count drops 54 → 22. The breakdown token becomes `legvwap` instead of `vwap` so a `side_undecided(...)` line says which reference produced the vote.

The EMA arm is **deliberately left alone**: its span is shared with the trend filters, and changing it would move far more than the side decision.

**Not measured:** `sr_scalp` showed no change on the same tape, but the synthetic tape carries no real S/R structure, so that is inconclusive rather than evidence against enabling it. Re-anchoring `momentum`'s `day_strength` gate was tried and contributed nothing (18 signals with it, 18 without) and was dropped.

## 19. Scheduled-event blackouts (2026-09-18)

Driven by the top-level `events:` config section and `event_blackouts.EventBlackoutCalendar`, shared by every strategy (it previously lived inside the 0DTE options strategy, so equity strategies had no event awareness at all).

- **Macro windows** (`events.blackout_file`, `events.blackouts`) - CPI, FOMC and similar. A window may now carry `symbols: [...]` to scope it. Checked once per cycle; blocks every candidate.
- **Earnings** (`events.earnings_file`, `events.earnings`) - per-symbol dates. Blocks `earnings_block_sessions_before` / `_after` trading sessions either side, weekend-aware. Across 23 mega caps that is roughly 92 scheduled events a year, clustered into three weeks a quarter; an earnings print resets the symbol's volatility regime, so the ATR-derived stops and session-open-anchored `day_strength` are both calibrated to a distribution that no longer holds.

Skip reasons: `event_blackout(<label>)` and `earnings_blackout(<SYM> <date>,<when>,offset=+/-Nsession)`.

## 20. Cross-regime score normalisation (2026-09-18)

Raw regime scores are not comparable: each `_score_*` method has its own ceiling (see `REGIME_SCORE_CEILINGS`) and its own `min_*_score` floor. `_normalized_regime_score` maps a score onto `(score - threshold) / (ceiling - threshold)`, so 0.0 is exactly at the floor and 1.0 at the scorer's maximum.

This drives both auctions:

- The per-candidate **build order** - previously sorted raw, which handed the queue to whichever scorer had the most components. A trend at 4.5/6.0 (25% of its headroom) outranked an sr_scalp at 4.4/5.0 (93% of its headroom).
- The cross-signal **slot auction** - ranks on `regime_score_normalized`, then `final_priority_score`. Previously the gatekeeper's generic path sorted on raw `regime_score`, and `final_priority_score` (which carries the structure / pattern / candle / S/R / FVG quality work) only broke ties between identical raw scores.

Since 2026-09-24 the auction is the manifest's `capabilities.signal_priority`, ranked by `shared_entry.SharedEntryPolicy.rank_key` (it was a `signal_priority_key` override on the strategy): `primary_field: regime_score_normalized` with `shared_score_weight: 1.0` and `rank_unit_field: regime_rank_unit` (user decision). The shared entry-context + FVG score (`shared_context_score`, in raw score points) is added to the normalised score at the regime's own scale: `entry_signals` stamps `regime_rank_unit = 1 / (ceiling - floor)` — the slope of the normalisation, floor including a SHORT's `short_min_score_premium` — next to `regime_score_normalized`, so a shared point moves a signal exactly as far as the same point on its raw regime score would. Before, those terms only broke ties through `final_priority_score`. small_cap_squeeze declares the same.

## 21. Side decision is scoped to direction-following regimes (2026-09-18)

`_decide_side` votes on four trend-following signals (recent return, close vs VWAP, EMA9/20, last-3-bar colour). It used to collapse `preferred_sides` to the winning side for the whole candidate, before any regime was scored.

That silently removed both mean-reversion regimes. `range` only enters within the bottom 35% of the range (LONG), and `sr_scalp` only at a support price has just fallen into - exactly the conditions where recent-return and close-vs-VWAP both vote SHORT. Simulated over a clean oscillating range with the shipped params, the vote agreed with the range regime's own entry zone on 3 of 54 in-zone bars (5.5%).

The vote now gates per (side, regime) against `SIDE_DECISION_REGIMES`, matching the exemptions the index and confirmation-bar gates already made for `MEAN_REVERSION_REGIMES`. New skip reason: `<side>_build_failed_<regime>_side_decision_opposed(decided=...)`.

## 22. Long / short asymmetry (2026-09-18)

Every other threshold is shared between the two sides, which assumes they are
mirror images. On equities they are not, so three multipliers encode the
asymmetries that actually exist. All default neutral, so a preset that does not
set them stays fully symmetric.

| Param | Preset | Effect |
|---|---|---|
| `short_min_score_premium` | `0.5` | Raises the regime floor for SHORTs only |
| `short_stop_buffer_mult` | `1.25` | Widens the ATR cushion on shorts |
| `short_target_rr_mult` | `0.85` | Banks short profits sooner |

Rationale: squeezes are faster than flushes, so the same "normal" adverse move
costs a short more; equities drift up, so a short fights the base rate and
deserves a higher bar; and that drift makes short profits less durable.

Two details worth knowing:

- The premium raises the **normalisation denominator** too, so a short that
  barely clears its higher bar still ranks as marginal in the cross-regime
  auction instead of being flattered by the long-side floor.
- The wider short stop does **not** raise dollar risk. `size_position` divides
  the budget by stop distance, so a 1.25x stop simply sizes to ~20% fewer
  shares. It never moves the structural level the strategy chose - only the
  cushion beyond it.

On the shipped preset that means, for the trend regime:

```
LONG    floor 4.00    stop buffer x1.00    target 2.00R
SHORT   floor 4.50    stop buffer x1.25    target 1.70R
```

## 23. ORB logic fixes, and why the regime has never traded (2026-09-22)

**It has never traded.** Across the whole archive, every ORB attempt died on
`orb_range_too_wide` — 3,403 of them, and not one of any other ORB failure
reason. No ORB signal has ever been built, so the "thin evidence — one session"
note in the preset describes a session that contained no ORB fills.

That is a **calibration** problem and it is still open. `orb_max_range_atr_mult:
4.0` compares a **15-bar** opening range against a **1-bar** `atr14`. Measured
across 462 real symbol-days the opening range is:

| | ratio to 1m ATR |
|---|---|
| min | 2.21 |
| p10 | 3.20 |
| **median** | **4.64** |
| p90 | 6.45 |
| max | 11.24 |

The cap sits *below the median*. `orb_min_range_atr_mult: 0.5` has never bound
and cannot — the minimum observed is 2.21. A band of roughly `[2.5, 8.0]` would
match observed reality (8.0 admits 96.5%). **Not changed**: enabling ORB is a
trading decision, and it is disabled in the shipped preset.

Four **logic** defects found underneath the calibration issue were fixed. None
of them is why ORB does not trade; all four would have produced visible effects
the moment the cap was corrected.

1. **The score qualified on a bare break; the builder demanded a buffered
   one.** `_score_orb` awarded +2.5 for `close > or_high` with no buffer and
   treated clearing `orb_breakout_buffer_atr_mult` as an optional +1.0, while
   `_build_orb_signal` rejected anything that did not clear it. A one-tick poke
   scored 4.0, beat the 3.5 floor, won its place in the build queue and died on
   `orb_no_break_above` — a guaranteed-fail path. The +2.5 now requires the
   buffered break and the +1.0 marks a *decisive* one at 2× the buffer; the
   ceiling stays 5.0. `vol_squeeze` had the same shape and was fixed the same
   way on 2026-05-14.
2. **The window lost a minute.** `_time_in_range` is inclusive at both ends, so
   the range-formation check overlapped the ORB window at `orb_range_end` — and
   since formation returns first, that minute was unreachable. With the default
   15-minute range the window ran 09:46–10:05 while `entry_windows` opened at
   09:45. The formation check is now half-open.
3. **`orb_range_minutes` and `orb_end_time` had an unvalidated ordering.**
   Violating it did not error, it silently shrank the window: 30 minutes left
   5 tradeable minutes, 34 left 1, and 35 left none at all with no error and no
   log line. `_validate_orb_window` now raises at construction, and only when
   the regime is enabled.
4. **The range-end derivation existed in three copies** —
   `_opening_range`, `_orb_range_end` and `_allowed_regimes` each recomputed
   it. They agreed, but three copies of one rule is how the bot ends up forming
   the range over one span and opening the window against another;
   `_breakout_reference` was extracted for exactly this on 2026-09-20.

Two things worth knowing that were **not** changed. ORB's achievable R:R is
capped below its own `orb_target_range_mult: 1.5` — the stop is the *opposite*
range edge plus a buffer while the target is measured from the *broken* edge,
so R:R asymptotes to 1.5 without reaching it (1.07 at a 1-ATR range, 1.37 at
4 ATR) against 2.0+ for every other regime. And if `min_target_rr` were ever
raised to 1.5, ORB would become mathematically incapable of producing a signal,
silently.

## 24. Armed retest: qualifying arms the trigger, the retest fires it (2026-09-20)

`trend` and `momentum` both enter on `close > max(high of the previous N bars)`
— 25 LTF bars and 6 base-1m bars. The fill therefore sits at the highest price
in 25 or 6 minutes **by construction**, and the stop, once the
`default_stop_pct` (1.0%) and `min_stop_atr_mult` (1.5) floors apply, lands
inside the retrace that normally follows. `same_level_block_minutes` (30) then
bars same-direction re-entry within 1.5×ATR of that level — which is usually
where and when the next leg starts.

The `entry_timing` block in the session report put a number on it. Measured
across the archived sessions (old code, pre-retarget universe):

| regime | n | median retrace | baseline | edge |
|---|---|---|---|---|
| `trend` | 9 | 0.847R | 0.206R | **+0.641R** |
| `sr_scalp` | 15 | 1.002R | 0.592R | +0.410R |
| `pullback` | 8 | 0.471R | 0.258R | +0.213R |
| `vol_squeeze` | 11 | 0.398R | 0.422R | **-0.024R** |

A trend fill was followed by a retrace covering 85% of the way to its stop,
against 21% from an arbitrary moment in the same session.

**So qualifying no longer means entering.** A regime in `ARMED_RETEST_REGIMES`
records the level it cleared and waits (`_armed_retest_verdict`). Four
outcomes:

- `none` — feature off, or no usable trigger level. Behaves as before.
- `wait` — armed, retest not confirmed. The cycle records
  `<side>_build_failed_<regime>_armed_awaiting_retest(...)` and skips. Other
  regimes in the build queue are unaffected, so arming `trend` does not stop
  `pullback` firing on the same symbol in the same cycle.
- `enter` — price returned to within `armed_retest_zone_atr` (0.35) ATR of the
  level and closed back through it on a bar closing in the top
  `armed_retest_min_close_position` (60%) of its range.
- `expired` — no retest inside `armed_retest_max_minutes` (12). **Enters at
  market**, which is the pre-2026-09-20 behaviour. Handled by
  `_expired_armed_retests`, which runs BEFORE the build queue and **skips the
  side / index-confirmation / confirmation-bar gates** — see below.

The market fallback is deliberate, not a hedge: a strong trend day never offers
the retest, and those are exactly the setups worth having. Forfeiting them
would deepen the "some days it doesn't trade at all" problem rather than fix
the entry.

Which regimes arm is decided by the same table. `pullback` already requires a
25–50% leg retracement before it fires (`pullback_require_real_dip`), `range`
and `vwap_reclaim` enter against the move by design, and `vol_squeeze` measured
**no** retrace above baseline at all — so arming it would add latency for
nothing. Only `trend` and `momentum` arm.

**The stop rules are not changed on a retest entry.** The gain is that the fill
sits at a level price has already tested and held instead of at a fresh
extreme. Re-deriving the stop from the retest low would mean bypassing
`default_stop_pct` / `min_stop_atr_mult`, which are risk floors and a separate
decision; the retest low is stamped in metadata so that question can be
answered from data later.

**Expiry is evaluated outside the per-cycle gates, and that is load-bearing.**
Those gates were all satisfied when the arm was created — that is the only way
an arm exists. Judging the fallback against them again means it can only fire
on a cycle where the setup happens to fully re-qualify, and if it never does,
the trade is silently dropped.

INTC on 2026-09-21 is the proof: armed 09:58 at 118.39 on a setup that passed
every gate, the sector index lapsed at 10:03, and the stock ran 117.26 → 124.64
with no entry. The retest never came (`touched=0` throughout), so the fallback
was the entire point — and it never fired once, because the expiry check sat
behind the gate that had failed.

The asymmetry underneath it: index confirmation is an **entry** gate. Once in a
position it no longer applies. Arming holds the trade out across exactly the
window where a lapse can lock it out, so a setup that had already cleared the
gates gets re-validated against them and can fail.

What still applies on the fallback path: the regime must still be offered in
the current window (a trend arm does not fire at midday); the breakout must
still **hold** — close on the trade's side of the armed level, the same
`reclaimed` test a retest entry passes, or the arm has faded and is dropped
with `armed_retest_faded(level=...,close=...)`; and `_finalize_signal` still
applies the stretched rejection and the shared entry stage's S/R / structure vetoes. A runaway that has gone too
far to chase is still declined, by the gate that exists for that.

The builder's fresh-N-bar-extreme check is **not** re-run on this path
(2026-09-23). It asks whether *this* bar is a new extreme, and a runaway that
never retested is rarely at one on the exact expiry minute: on 2026-09-22 all
three arms that reached expiry (CRM, ADBE and NFLX shorts, `touched=0`, still
through their levels) died on `no_fresh_breakdown` while the moves continued.
The fallback had been firing only by coincidence.

Two rules bound how stale an arm can get, because an arm past its window is a
licence to enter at market on the next qualifying cycle:

- **A position on the symbol drops its arms.** An arm can never produce the
  entry it was created for once a position exists, and leaving it meant that
  when the position closed the next qualifying cycle found an expired arm and
  took the market fallback immediately — skipping the wait on the re-entry,
  which is the most chase-prone entry there is.
- **Arms are reaped past twice the window.** The regime can go a long time
  without qualifying (index confirmation lapses, the score dips), so without a
  bound a fallback entry could be justified by a breakout most of an hour old
  at a level the tape had moved away from. Past 2x, the arm is dropped and the
  next qualifying cycle arms again — waiting rather than entering on stale
  evidence.

Invalidation is measured against the **armed** level, not the current N-bar
reference. The reference walks up as new highs print, so testing against it
would move the invalidation line away from price on exactly the setups still
working, and drag it along behind a rolling-over one.

Signals carry `armed_retest_status` (`retest_confirmed` / `expired_market_entry`),
`armed_retest_level` and `armed_retest_waited_minutes`, which is what lets the
session report tell the two populations apart. Turn the whole mechanism off
with `armed_retest_enabled: false` — that is the A/B.

`_breakout_reference` is the single source for both the builder's own
fresh-breakout check and the armed level. Two copies would drift the moment
either lookback was retuned, and the bot would arm on one level and enter
against another.
