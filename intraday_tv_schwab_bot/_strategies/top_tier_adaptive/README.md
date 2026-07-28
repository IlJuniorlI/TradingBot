# Top-Tier Adaptive

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is a **multi-regime adaptive intraday strategy** for a fixed universe of top-tier liquid stocks across six Tier 1 GICS sectors (Technology, Consumer Discretionary, Communication Services, Financials, Healthcare, Consumer Staples). It detects whether each symbol is trending, pulling back, ranging, breaking out of a volatility squeeze, sustaining momentum from the session open, or scalping between HTF support/resistance zones, then applies the appropriate entry style. Trades both long and short across the full RTH session with time-of-day regime gating.

**Timeframes (1m LTF, 2026-05-29).** The trend/pullback "LTF" runs on **1-minute bars** (`ltf_minutes: 1`) so entries and exits act on the freshest close instead of waiting up to 5 minutes for a 5m bar to print. To keep the *behavior* identical to the prior 5m tune, that 1m LTF's indicators are stretched ×5 via `ltf_indicator_span_scale: 5` — `add_indicators(span_scale=5)` makes its ema9/ema20/atr14/adx14/rsi14/ret5/ret15 effectively 45/100/70/70/70/25/75-bar, i.e. the same wall-clock horizons the 5m frame had. Consequently every ATR-based stop/buffer and every score threshold keeps its 5m calibration unchanged; only the bar granularity got finer. Two LTF lookbacks that *count* bars were scaled to match (`pullback_lookback_bars` 5→25, `side_decision_recent_lookback_bars` 6→30). The `range`/`vol_squeeze`/`momentum` regimes, the `technical_levels` context, and chart-pattern detection all read the **base 1m frame** (unchanged before and after this switch), so their lookbacks did *not* change. HTF stays 15m; the structure-pivot frame stays 5m (`support_resistance.structure_ltf_timeframe_minutes: 5`).

### 1. It trades a fixed universe, not a dynamic screener

Unlike the dynamic-discovery strategies, this one operates on a predefined list of 23 top-tier symbols configured in `params.tradable`. The screener fetches those symbols from TradingView and ranks them by absolute intraday move weighted by relative volume. This means the bot always knows exactly what it is watching, and the screener simply decides which ones are most active right now.

### 2. It scores six regimes for every candidate

For each symbol and each direction (long/short), regime scores are computed for whichever regimes are allowed at the current time:

- **Trend**: close vs VWAP, EMA alignment, momentum (ret5/ret15), ADX strength, index confirmation. Max score 6.0.
- **Pullback**: requires underlying trend first, then checks for EMA20/VWAP touch, support/resistance hold, EMA9 reclaim, close quality, volume expansion. Max score 5.0.
- **Range**: VWAP proximity, EMA convergence, VWAP cross count, tight intraday range, index neutrality. Max score 5.5.
- **Vol-squeeze** *(added 2026-05-12)*: detects a tight Bollinger compression box across `vol_squeeze_lookback_bars` (default 12), then scores breakout magnitude, confirming volume ratio, bar close position within the breakout candle, VWAP/EMA alignment. Allowed in the primary and afternoon windows.
- **Momentum** *(added 2026-05-12, widened from afternoon-only and renamed from `momentum_close`)*: momentum-from-open continuation. Computes day_strength live from session open + current close, requires `momentum_min_day_strength` (default 1.5%) with the trade side, scores N-bar breakout + alignment. **Allowed post-ORB through close** (`orb_end_time` → `no_new_entries_after`) including midday — the day_strength hard gate is what filters chop, not the time window.
- **Sr-scalp** *(added 2026-05-12)*: HTF S/R mean-reversion scalp. Uses the bot's existing `sr_ctx.nearest_support` (HS) and `nearest_resistance` (HR) as level prices and zone bands matching the dashboard's `key_level_zones` — NO strategy-local level creation. A distance gate requires the inner zone gap to clear BOTH `sr_scalp_min_distance_pct` (default 0.8% of close) AND `sr_scalp_min_distance_atr` (default 2.5x ATR); too-close zones reject at build time as `htf_zones_too_close` so other regimes can fall through. A proximity gate requires close to be inside the entry-side zone or within `sr_scalp_max_distance_from_zone_atr` of its inner edge. **Allowed post-ORB through close** (`orb_end_time` → `no_new_entries_after`). Index-confirmation EXEMPT (mean-reversion thesis, same as range). Theoretical max score 5.0, but the **empirical ceiling is ~3.9** across 13.2k observed cycles (the +1.5 rejection-wick component rarely co-occurs with all three neutral/chop components). **Keep `min_sr_scalp_score` ≤ 3.9 or the regime can never qualify** — it was silently dead from 2026-05-12 to 2026-05-27 with a 4.0 threshold (0 entries ever); lowered to 3.0 on 2026-05-27 so it can fire.

Build-time fall-through: as of 2026-05-12 each side stores an ordered list of qualifying regimes (score-descending). The build phase iterates the list and tries each regime in turn — if a regime's build fails (e.g. trend's `no_fresh_breakout`, sr_scalp's `htf_zones_too_close`), the next qualifying regime on the same side gets a chance. Across sides, the higher-scored side's full build_order is tried first.

Each regime can be globally disabled via its own opt-out knob: `disable_trend_regime`, `disable_pullback_regime`, `disable_range_regime`, `disable_vol_squeeze_regime`, `disable_momentum_regime`, `disable_sr_scalp_regime` — all default `false`. The 7th regime, **orb** (the true Opening Range Breakout, sole regime in the opening window), can be skipped two ways: `disable_orb_window` skips the opening window entirely (start trading at `orb_end_time`), while `disable_orb_regime` drops the ORB regime *and* its opening-range carve-out so the normal regime mix runs continuously from the open (used by the `small_cap_squeeze` subclass). An 8th regime, **vwap_reclaim** (a long re-entry on a VWAP flush-and-reclaim), is **opt-in** via `enable_vwap_reclaim_regime` (default `false`, so `top_tier_adaptive` is unaffected) — see the `small_cap_squeeze` README for its knobs.

### 3. Time-of-day gating controls which regimes are allowed

Not all regimes fire at all times. All boundaries are param-driven (no hard-coded times):

- **09:30 - opening-range-end (range formation)**: NO entries — the opening range (first `orb_range_minutes`, default 15 → 09:30-09:45) is still forming
- **opening-range-end - `orb_end_time` (ORB window)**: **orb only** — a true Opening Range Breakout. Trades a break of the opening range (stop = opposite range edge, target = measured move). As of 2026-05-29 this replaced the old "trend regime with bypasses" approach.
- **`orb_end_time` - `midday_start_time` (primary)**: trend, pullback, range, vol_squeeze, momentum, sr_scalp
- **`midday_start_time` - `midday_end_time` (midday)**: pullback, momentum, sr_scalp
- **`afternoon_start_time` - `no_new_entries_after` (afternoon)**: trend, pullback, range, vol_squeeze, momentum, sr_scalp (range was disabled pre-2026-04-22; re-enabled so afternoon range-bound tapes get mean-reversion entries — disable with `afternoon_include_range: false`)
- **After `no_new_entries_after`**: no new entries

Default boundary values: opening range `09:30`-`09:45` (`orb_range_minutes: 15`), ORB end `10:05`, midday `11:30`-`13:00`, afternoon `13:00`-`no_new_entries_after` (`15:00` in the shipped RTH-only preset; `19:30` if extended-hours trading is enabled).

Midday still favors pullbacks because top-tier stocks tend to chop during the lunch hour, but the momentum and sr_scalp regimes are allowed alongside — the `momentum_min_day_strength` hard gate (default 1.5%) and the sr_scalp HTF zone-gap floor filter out non-qualifying names automatically. As of 2026-05-12 the momentum regime is post-ORB-through-close (renamed from `momentum_close` and widened from afternoon-only) and sr_scalp is post-ORB-through-close.

Per-regime opt-out via params: each of the six regimes has its own `disable_*_regime` boolean knob (all default `false`). Disabling a regime strips it from every window. The afternoon-range sub-knob `afternoon_include_range` still works for window-scoped exclusion.

ORB-window opt-out: set `disable_orb_window: true` (default `false`) to skip the entire opening window (09:30 → `orb_end_time`, i.e. range formation + the ORB regime) and start trading at `orb_end_time` — distinct from the `orb_bypass_*` family which loosen the shared finalize filters DURING the ORB window. Useful on tapes where the opening 30 minutes are too whippy.

### 4. Index confirmation gates directional entries

Per-sector index ETFs (via `sector_index_map`) are used for confirmation — each stock is gated by its own sector's tape, not an arbitrary broad-market ETF. For example AAPL is confirmed by XLK; XOM by XLE; FCX by XLB. The default mapping is the canonical SPDR Select Sector ETFs (XLK / XLC / XLY / XLF / XLV / XLI / XLE / XLP / XLB / XLRE / XLU). Symbols whose sector isn't mapped fall back to the universe-wide `index_symbols` list. For trend, pullback, vol_squeeze, and momentum entries, at least one mapped index must agree with the trade direction:

- Long: index close > VWAP and EMA9 >= EMA20
- Short: index close < VWAP and EMA9 <= EMA20

Range and sr_scalp entries do not require index confirmation (both are mean-reversion theses). Range entries get a bonus when indices are neutral (both near VWAP); sr_scalp's index exemption matches range's reasoning — index direction is orthogonal to a between-zone scalp.

### 5. Each regime builds a different signal

- **Trend signal**: requires a breakout above (long) or breakdown below (short) the recent swing high/low. Stop at the recent low/high + ATR buffer. Target at the configured R:R ratio.
- **Pullback signal**: stop at the recent extreme + buffer. Target extended to the prior swing point or the R:R target, whichever is more aggressive.
- **Range signal**: enters near range low (long) or range high (short). Stop outside the range boundary. Target at the opposite range boundary.
- **Vol-squeeze signal**: stop just outside the compression box (low for long, high for short) buffered by `max(0.12·ATR, 0.10%·price, 0.22·box_range)` so tight squeezes don't get over-wide ATR-based stops. Target at `vol_squeeze_target_rr` (default 2.05).
- **Momentum signal**: requires a fresh N-bar breakout (1m frame, `momentum_breakout_lookback_bars`, default 6) on the active session frame. Stop anchors below the recent swing low (long) / above recent swing high (short) with an ATR cushion (0.08·ATR) so single-bar wicks during midday/afternoon thin liquidity don't trigger the stop. Target at `momentum_target_rr` (default 2.0).
- **Sr-scalp signal**: enters near the entry-side HTF zone — `HS` (`sr_ctx.nearest_support`) for long, `HR` (`sr_ctx.nearest_resistance`) for short. Stop just outside the entry-side zone: `HS_zone_lower − level_buffer` (long) / `HR_zone_upper + level_buffer` (short), where `level_buffer = sr_ctx.level_buffer * vol_widening` — the same buffer `_refine_*_sr_levels` uses to nudge stops past structural levels. Target at the inner edge of the opposite zone: `HR_zone_lower − level_buffer` (long) / `HS_zone_upper + level_buffer` (short) — exits at the inside of the opposite zone, matching the bot's structural-exit conventions elsewhere. No fixed R:R target — the zone gap (filtered by the distance gate) provides the reward.

All six pass through the shared finalization pipeline (HTF bias, structure, S/R, exhaustion, chart pattern, FVG, adaptive management).

### 6. Shared gates apply to every signal

Before any signal is emitted, the finalize pipeline applies:

- **HTF bias alignment (`require_htf_bias_alignment`, default true)**: reject longs when 15m market structure is bearish, and shorts when 15m is bullish. Neutral HTF never blocks. Prevents counter-trend entries that look good on the 1m/5m chart but fight the 15m trend. Set `false` if you want the bot to take setups regardless of the higher timeframe.
- **ORB HTF bypass (`orb_bypass_htf_bias`, default true)**: skip the HTF bias check during the ORB window (through `orb_end_time`). At the open, the 15m chart has zero or one completed bars from today — the structure is stale (yesterday's pivots). The ORB regime's range-break proves direction. After the ORB window, the filter resumes with 2-3 closed 15m bars.
- **ORB exhaustion bypass (`orb_bypass_exhaustion`, default true)**: skip the VWAP/EMA extension filters during the ORB window. After an opening dump, VWAP is artificially depressed and recoveries look "extended" when they're really the trend establishing itself. After the ORB window, VWAP reflects today's action and the filter becomes meaningful.
- **ORB structure-block bypass (`orb_bypass_structure_entry`, default true)**: skip the LTF market-structure block during the ORB window. (The LTF structure runs on the 5m frame under `support_resistance.structure_ltf_timeframe_minutes: 5` — Fix D, 2026-05-27.) The opening dump candle registers as CHoCH_down on the LTF chart and blocks LONG entries for several bars even after the recovery. The ORB regime's range-break already proves direction. Caveat: more aggressive than HTF bias since the LTF structure is immediate, not stale. Set `false` to respect the LTF bearish signal during ORB.
- **ORB SR-block bypass (`orb_bypass_sr_entry`, default true)**: skip the S/R `breakdown_below_support` (or `breakout_above_resistance` for shorts) block during the ORB window. An opening dump that breaks yesterday's low flips `breakdown_below_support` true, blocking LONG entries until the reclaim confirmation completes — often several bars after the recovery is already underway. Set `false` to respect the breakdown flag during ORB.
- **ORB screener-bias bypass (`orb_bypass_screener_bias`, default true)**: restore fallthrough to the opposite side during ORB so Fix A doesn't block gap-reversal trades. `change_from_open` is dominated by the opening gap in the first 30 min — a gap-down day that reverses (TSLA 2026-04-15 $367→$362→$394) correctly belongs to LONG even though the screener tagged SHORT. Post-ORB the screener's directional read is respected. Set `false` to enforce screener bias during ORB too.
- Market structure veto on the LTF frame (bearish LTF structure blocks longs, bullish blocks shorts) — LTF structure is the 5m frame here (Fix D)
- Support/resistance veto (too close to opposing level)
- S/R and technical level refinement of stop/target
- Entry exhaustion filters (VWAP extension, EMA9 extension, bar range, wick fraction — bypassed during ORB when `orb_bypass_exhaustion` is true)
- Chart pattern scoring (continuation/reversal bonus)
- FVG confluence scoring
- Adaptive management metadata (breakeven, profit lock, runner extension thresholds)

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

**ORB-window bypass companions.** The surviving `orb_bypass_*` flags loosen the *shared* finalize filters that read stale at the open and apply to whatever runs in the ORB window (now the ORB regime): `orb_bypass_htf_bias`, `orb_bypass_exhaustion`, `orb_bypass_structure_entry`, `orb_bypass_sr_entry` (all default `true`), plus `orb_bypass_screener_bias` / `orb_bypass_side_decision` / `orb_bypass_relative_strength` which let both break directions through at the gap-dominated open. The Fix D#1 (`orb_bypass_stretched_filter`), Fix D#2 (`orb_bypass_tech_bias_contradiction`), `orb_bypass_index_confirmation`, `orb_bypass_entry_confirmation_bar`, and `orb_bypass_oversized_entry_bar` companions were removed (2026-05-29) — the ORB regime isn't in those gates' regime sets, so those bypasses were dead code.

#### 6b. 2026-04-23 gates

Post-mortem on the first dry-run (19 trades, 26% WR, -$231 on range-heavy afternoon tape) added four more filters:

- **`reject_entry_near_broken_level`** (default `true`). Entry-side mirror of the `resistance_break_exit` / `support_break_exit` gates in `strategy_base.position_exit_signal`. Rejects SHORT when `sr_ctx.broken_resistance` sits above entry within `broken_level_min_clearance_pct` (default `0.0025` = 0.25%) OR `broken_level_min_clearance_atr` (default `0.72`). Symmetric for LONG on `broken_support`. Fires across all regimes. Would have blocked 2026-04-23 NVDA 09:35 SHORT (level $0.04 above entry) and HD 14:12 SHORT (level $0.26 above entry), a combined -$62.54 of avoidable losses.
- **`trailing_bias_enabled`** (default `true`). Adds per-symbol trailing-bias memory to Fix A. The strategy keeps a `deque(maxlen=trailing_bias_lookback)` (default 10) of recent `candidate_directional_bias` values. When the screener reports `None` for the current bar but ≥70% (`trailing_bias_majority_threshold`) of recent directional observations were one side, Fix A infers that side as the effective bias and restricts `preferred_sides` accordingly. Blocks the 2026-04-23 GOOG 12:51 LONG pullback that fired into 10 consecutive SHORT-biased bars.
- **`adaptive_partial_breakeven_rr` / `adaptive_partial_breakeven_offset_r`** (defaults `0.5` / `0.0`). A third adaptive-management tier sitting below the existing breakeven (`1.0R`) and profit_lock (`1.3R`). Moves the stop to `entry + offset * initial_risk` when `max_favorable_r` first crosses the threshold. Only 3 of 19 trades on 2026-04-23 reached the 1.0R breakeven gate, leaving modest-peak winners (AVGO 0.82R, RBLX 10:00 0.56R, COST 09:51 0.56R) unprotected. Set `adaptive_partial_breakeven_rr: null` to disable.
- **`range_require_prev_bar_confirmation`** (default `true`). Applies to `_build_range_signal` only. Requires the last COMPLETED bar's close (`session_frame.iloc[-2]`) to also sit in the entry zone — filters single-tick whipsaws where an in-progress bar briefly crosses the range-edge threshold but closes back mid-range. All 7 red-from-tick-one losers on 2026-04-23 (AMZN 10:07, COST 11:09/13:02/15:15, LOW 13:08, HD 14:12 SHORT, V 14:14 SHORT) fit this pattern.

All four are toggleable in `params` and default `true` for `top_tier_adaptive`. The partial-breakeven tier is also exposed via `strategy_base._build_adaptive_management_metadata` so other strategies can opt in.

#### 6c. 2026-04-24 exit-side fixes

First live-session post-mortem surfaced three exit bugs (not strategy-specific, but they bit top_tier_adaptive hardest because it runs in the ORB window):

- **Candle detection window (candles.py)**. Callers were pre-slicing `frame.tail(3)` before handing to `detect_candle_context`. TA-Lib candle functions build internal body-average/trend context from preceding bars; with 3 inputs it returned zeros even for textbook patterns. Fixed by adding `CANDLE_CONTEXT_BARS = 30` and having `detect_candle_context` slice internally, plus scanning `values[-1..-3]` in `_talib_pattern_value_from_key` so a pattern completing at bar N-1 (values[-2]) stays reportable for 1-2 cycles after it forms. Before: INTC 10:08 bullish engulfing was only reported during the single minute when 10:08 was the latest bar. After: stays visible through 10:10.
- **Anchored-VWAP instant exit (strategy_base._technical_exit_signal)**. AMZN 10:59 LONG exited at 13 s, META 09:35 SHORT at 55 s — both because entry fill was already on the wrong side of the AVWAP level, so the first tick triggered `anchored_vwap_loss_exit` / `reclaim_exit`. Fixed by adding an armed-guard: LONG requires `position.highest_price >= avwap_floor + buffer`, SHORT requires `position.lowest_price <= avwap_ceiling - buffer` before the exit can fire. Mirrors `trail_armed`. `_technical_exit_signal` now takes `position` as a parameter (threaded through both call sites).
- **ORB-entry exit grace (`orb_entry_exit_grace_minutes`, default `20`)**. 5 of 6 ORB-window entries on 2026-04-24 exited at a loss during pullbacks, with price recovering after. INTC at 2.0m via chart_pattern_exit, AMD at 11.2m via structure_bearish_exit. Added a config-gated grace window that suppresses `chart_pattern_exit` entirely AND OR's the ORB hold-check into `structure_exit_gated` for positions with `orb_window_entry=True` in metadata. CHoCH exits still fire (genuine reversals). Set `0` to disable.
- **Pullback-regime exit grace + BoS confirmation (`structure_exit_grace_minutes_pullback` default `15`, `structure_exit_require_bos_confirmation` default `true`)**. 2026-05-14 AMD 14:36 LONG (pullback) was killed at hold=10.2m via `structure_bearish_exit:EQL` — the exit barely cleared both legacy gates (10min/2-pivot). The LTF formed a single EQL pivot, bias flipped bearish, exit fired. Price recovered to ~$452 (past R1 $450.10) shortly after. Two layered fixes: (1) pullback regime gets a longer grace (15min) because pullback by design enters into LTF chop; (2) the bias-flip exit now additionally requires an active BoS event (`bos_down` for long, `bos_up` for short), not just bias flipping on a single pivot. CHoCH exits remain unaffected. Both knobs live on `support_resistance` and apply across all strategies — the pullback-specific grace fires only when `position.metadata.regime == "pullback"`, so non-pullback regimes / non-top_tier_adaptive strategies see no change from the grace gate (BoS confirmation applies globally).

#### 6d. 2026-04-24 PM — Fix G: target-inside-SR gate (entry-side)

Morning 2026-04-24 session surfaced COST LONG at 1013 exiting via `time_stop:45m` for -$53: 1.06 ATR clearance below 15m resistance PASSED `entry_min_clearance_atr: 0.72` (a *floor* on SR clearance) but the computed target sat 2 ATR above close — past the resistance. `_refine_bullish_sr_levels` tried to cap the target at `resistance - level_buffer` but the capped target failed `_target_meets_min_rr`, so the un-capped original was kept and the trade entered with zero head-room.

- **`reject_target_beyond_sr`** (default `true`). A *ceiling* complement to `entry_min_clearance_atr`. For **trend entries only**, computes `dist_to_target = |target - close|` and `dist_to_sr = |opposing_sr_price - close|` (nearest_resistance for LONG, nearest_support for SHORT) and rejects when `dist_to_target > dist_to_sr * target_max_sr_ratio`. Range regime is exempt (range targets ARE the opposite SR by design). Pullback regime is exempt per initial scoping; can extend later if the pattern shows up there.
- **`target_max_sr_ratio`** (default `0.8`). The ceiling — `0.8` enforces a 20% head-room buffer (target must fit within 80% of the distance to SR). Tighten to `0.5` for a 50% buffer; relax to `1.0` to only reject targets strictly past SR (not recommended — at-resistance targets still need to punch through).

**Placement note.** Fix G runs AFTER `_apply_ladder_if_enabled` and the runner-override so `target` is the trade's FINAL take-profit: `None` (runner mode → gate inert), `rungs[0]["price"]` (ladder active → checks the actual rung), or refined initial (non-ladder mode). The gate does not kill runner-eligible trades — runners trail out via stop, so the SR ceiling doesn't apply.

No ORB bypass — structural soundness of target vs. SR is timing-independent.

#### 6e. 2026-04-24 PM — Fix H: reject range entries during Bollinger squeeze

Afternoon live-session trade (NFLX 13:22 SHORT, -$11.34 in 2.2 min) surfaced a structural mismatch: the range regime qualified and prev-bar confirmation passed, but the underlying tape was in a `bollinger_squeeze` (compressed volatility). Range mean-reversion needs oscillating vol; a squeeze typically resolves via breakout in the opposite direction. NFLX entry context showed `bollinger_width_pct: 0.0015` (0.155%), `atr14: 0.044` on a $92 stock — a 12-cent range where stops and targets are both 1-2 ticks away. R:R math was fine (2.27) but absolute edge was swallowed by noise.

- **`reject_range_during_squeeze`** (default `true`). In `_build_range_signal`, after the insufficient-bars check, read `tech_ctx.bollinger_squeeze`. If true, skip the entry with reason `range_bollinger_squeeze(width_pct=X)`. Disable via `reject_range_during_squeeze: false`.

No ORB bypass — squeeze is a volatility state, not a time-of-day artifact.

#### 6f. 2026-05-26 entry-quality gates + low-tier peak-giveback

Post-mortem on 2026-05-26 (7 LONGs on a +1.2% XLK day, 1W/6L, -$177.66) surfaced three entry-side leaks and one exit-side leak. The session's losing pattern: stocks were under-performing their sectors (INTC at +0.21% vs XLK +1.27%, NEM at -0.19% vs XLB +0.67%) and the bot bought "pullbacks" that were actually rollovers. The new gates are designed to surface those structurally weak entries before they reach scoring.

- **`bias_penalty_saturate_at` tightened: 2.0 → 0.75** (manifest default). The original 2.0 saturation assumed daily moves regularly hit ±2%; intraday reality on most days is 0.3-1.0%, where the penalty produced was 0.15-0.50 — not enough to filter weak-bias setups. At 0.75, a -1% day applies full 1.0 penalty (was 0.5); a -0.5% day applies 0.67 (was 0.25). The HIGH_VOL preset overrides this back to 2.5 because high-vol days routinely produce 2-3% day_strength. *(Note: with `require_explicit_side_decision: true` — default — only one side gets evaluated per candidate, so the soft penalty applies to a side that never gets scored. Kept active as fallback when the explicit decision is disabled.)*
- **`relative_strength_block_threshold_pct`** (default `0.5`). Filters `preferred_sides` based on stock-vs-sector intraday relative strength. Computes `rel_strength = day_strength − sector_day_strength` (sector ETF from `_indices_for_symbol(symbol)`'s first available frame). When `rel_strength ≤ -threshold`, LONG is removed from `preferred_sides`; when `≥ +threshold`, SHORT is removed. If `preferred_sides` is empty afterward, the candidate is skipped. Companion `orb_bypass_relative_strength` (default `true`) — the first 30 minutes of trading are too noisy for a stock-vs-sector divergence read. Catches the 5/26 INTC/NEM pattern where the symbol was drifting at +0.1% while its sector was up +1%. The lone winner that day (META, +RS 0.25%) passes through.
- **`stretched_cooldown_minutes`** (default `3.0`). Hysteresis on `reject_stretched_entries` (section 6a, Fix D#1). The `stretched_percent_b_max` / `stretched_atr_mult_max` thresholds are crisp — a single tick across relaxes them while the structural condition (price stretched above EMA20 / pinned to upper Bollinger band) is still active. AMZN on 5/26 was rejected at 10:11:41 with `pct_b=0.851` then entered 46 s later as the close ticked back across, losing $28. The cooldown stamps the failure timestamp and rejects subsequent stretched checks within the window. Per-symbol regardless of side. Disable with `stretched_cooldown_minutes: 0`.
- **`pullback_require_fresh_leg`** (default `true`; 2026-05-27 addition). Pullback works when the prior leg is YOUNG and the retracement shallow; it fails when the trend is stale and price has given back most of the move. Reject pullback when BOTH `(bars_since_session_extreme * ltf_minutes) > pullback_max_minutes_since_session_extreme` (default `45`) AND `retracement_from_extreme_pct > pullback_max_leg_retrace_pct` (default `50`). AND-logic on purpose — fresh-but-deep retracements and old-but-shallow ones still trade. NEM on 5/27 LONG at 14:48 was 400 min past session high with 140% retracement (price had fallen below the anchor low) — exactly the "multi-hour rollover dressed as a pullback" pattern this gate catches. Implemented via `_pullback_leg_context` which estimates `minutes_since_extreme` from bar count × ltf_minutes (avoids per-bar timestamp arithmetic) and computes the retracement against the lowest-low-at-or-before the high bar (mirror for SHORT). Disable with `pullback_require_fresh_leg: false`.
- **`require_explicit_side_decision`** (default `true`; 2026-05-27 addition — **Fix A**). Replaces the implicit "evaluate both sides per regime, pick the highest-scoring (side, regime) pair" with an evidence-based side decision computed BEFORE regime scoring. The old approach could pick SHORT just because the SHORT regime score was 0.5 higher even when every meaningful current-action signal said LONG. The new flow: `_decide_side` votes across four CURRENT price-action signals and either filters `preferred_sides` to a single decided side OR skips the candidate when signals are mixed. The wrong side is never evaluated. Votes contributed by: (1) recent return over `side_decision_recent_lookback_bars` (default `6` = 30 min at 5m), threshold `side_decision_recent_threshold_pct` (default `0.1`); (2) close vs session VWAP with `side_decision_vwap_buffer_pct` dead-band (default `0.0005` = 0.05%); (3) EMA9 vs EMA20 on the LTF; (4) last 3 LTF bars' green-count (≥2 = LONG vote, ≤1 = SHORT vote). Decision: side wins when its votes ≥ `side_decision_min_votes` (default `3`) AND opposing ≤ `side_decision_max_opposing` (default `1`). Companion `orb_bypass_side_decision` (default `true`) skips the gate during the ORB window (through `orb_end_time`) (early-session signals are gap-dominated). 5/27 dry-run: NVDA SHORT (1-2 votes — mixed) and NEM LONG (2-2 tied) skipped, saved $114 net. Reuses `_recent_momentum_pct` for vote #1.
- **`require_entry_confirmation_bar`** (default `true`; 2026-05-27 addition — **Fix B**). Companion to Fix A. For direction-following regimes (trend / pullback / momentum / vol_squeeze), the LAST FULLY CLOSED LTF bar must confirm direction before `_build_<regime>_signal` is called: `last_closed.close > last_closed.open` AND `last_closed.close > prev_closed.close` for LONG (mirror for SHORT). Catches single-bar fakeouts where the in-progress bar tipped a score threshold but the actual completed bar didn't carry. Range and sr_scalp are EXEMPT — both are mean-reversion theses where the last closed bar moves AGAINST the entry direction by design. Implemented via `_entry_bar_confirms`. The ORB regime is also exempt (not in the confirmation-bar regime set) — its range-break is the confirmation.
- **Low-tier peak-giveback** — exit-side fix in `RiskConfig` (not strategy-side; see section 14). Catches 0.7-1.0R MFE trades that round-trip to BE before the main tier's `peak_giveback_min_r: 1.0` arms.

All gates are independently toggleable via the manifest (or `params` in `configs/config.top_tier_adaptive.yaml`). The relative-strength gate has its own `relative_strength_block_threshold_pct: 0.0` off-switch. The pullback maturity check disables via `pullback_require_fresh_leg: false`. Fix A/B disable via `require_explicit_side_decision: false` / `require_entry_confirmation_bar: false`.

**Cleanup note (2026-05-27 PM):** the originally-shipped hard-screener-bias-veto, pullback-bounce-confirmation, and recent-momentum-disagreement gates were removed as redundant with the explicit side-decision approach (Fix A subsumes the bias-vetting and recent-momentum signals via voting; Fix B subsumes the bounce confirmation via the last-closed-bar check applied to all direction-following regimes). On the 5/26 batch the RS gate + stretched cooldown still independently block NEM, INTC×2 and AMZN; FCX#1's rejection now routes through Fix A/B (current-action side vote + confirmation bar) rather than the removed in-progress-bar bounce check.

### 7. Candle pattern confirmation boosts signal priority

The last 3 bars of the 1-minute frame are evaluated for TA-Lib candlestick patterns. A confirmed pattern adds a priority bonus to the signal score:

- **strong_3c** (Morning Star, 3 White Soldiers, etc.): +0.40
- **solid_2c** (Engulfing, Piercing, Kicking, etc.): +0.25
- **weak_1c** (Hammer, Marubozu, Dragonfly Doji, etc.): +0.10

Candle patterns do not block entries — they only boost priority when multiple symbols compete for limited position slots. A clean regime + index confirmation + breakout is sufficient without candle confirmation.

### 8. Index symbols are automatically added to the watchlist

The sector ETFs configured in `index_symbols` (e.g. XLK, XLC, XLY, XLE, XLB depending on which sectors your universe touches) are added to the active watchlist so they receive history fetching, streaming, and appear in the bars dict. Without this, index confirmation would silently fail because `bars.get("XLK")` would return None for an AAPL trade.

### 9. Sector concentration guard prevents correlated stacking

The strategy defines sector groups aligned to GICS sectors. A configurable limit (`max_same_sector_same_direction`, default 2) prevents more than N same-direction positions in the same sector. For example, you cannot hold 3 LONG tech positions simultaneously.

All 11 GICS sectors are pre-defined in the manifest so new symbols can be dropped into the correct group without code changes.

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

If the S/R context produces no qualifying rungs (e.g. nearest resistance is below `ladder_min_target_rr`), the signal drops the fixed target entirely and becomes a **pure trail runner** — managed by trailing stop, breakeven/profit-lock ratchets, and structural exits (CHoCH, S/R loss). Runner extension is also disabled so it cannot recreate a fixed target later. This prevents a modest 2R target from prematurely closing a trend-day move (e.g. TSLA 2026-04-15: $365→$394 run that a 2R target would have exited at $372).

Exits can also be triggered by:

- **Stop/target hit**: the primary exit mechanism.
- **Chart pattern exit**: opposing reversal or continuation pattern + tape weakness (disabled by default, enable via `shared_exit.use_chart_pattern_exit`).
- **Market structure exit**: CHoCH (Change of Character) in the opposing direction + tape weakness.
- **S/R level loss**: price breaks through a confirmed support/resistance level.
- **Force flatten**: fires `force_flatten_buffer_minutes` (default 5) before the management window closes, or earlier on early-close days (Jul 3, Black Friday, Christmas Eve).

### 13. Sector groups

The default sector groupings cover all 11 GICS sectors. Symbols are assigned to their proper sector so the concentration guard fires correctly:

| Sector                     | Symbols                                     |
|----------------------------|---------------------------------------------|
| **Technology**             | AAPL, MSFT, NVDA, INTC, AMD, AVGO, TSM, CRM |
| **Consumer Discretionary** | AMZN, TSLA, HD, LOW, UBER                   |
| **Communication Services** | GOOG, META, NFLX, RBLX, TMUS                |
| **Financials**             | JPM, GS, V                                  |
| **Healthcare**             | LLY                                         |
| **Consumer Staples**       | COST                                        |
| Industrials                | *(empty — ready for additions)*             |
| Energy                     | *(empty)*                                   |
| Materials                  | *(empty)*                                   |
| Real Estate                | *(empty)*                                   |
| Utilities                  | *(empty)*                                   |

With `max_same_sector_same_direction: 2`, you can hold at most 2 LONG per sector. Across the now-6 populated sectors that's up to 12 LONG positions if perfectly diversified (but capped by `risk.max_positions`). Adding a symbol to the tradable list requires also adding it to the correct sector group, otherwise it bypasses the concentration guard.

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
- The ORB regime window opens at 09:45 (= 09:30 + `orb_range_minutes`, once the opening range has formed), but practical entries begin once `min_bars` (150 one-minute bars) and `min_ltf_bars` (120 one-minute LTF bars) are met from the loaded history. With `required_bars: 150`, both gates clear on cold start from the prior session's data.
- With `runtime.auto_exit_after_session: true`, the bot shuts down cleanly after market close once all positions are flat. Designed for Windows Task Scheduler or cron to start the bot daily without manual shutdown.

## Shipped reference

Purpose: multi-regime adaptive strategy for a fixed list of top-tier liquid stocks across Technology, Consumer Discretionary, and Communication Services.

Default windows:

- `entry_windows`: `[["09:45", "15:00"]]` (entries open when the ORB regime can first fire = `09:30` + `orb_range_minutes`)
- `management_windows`: `[["09:30", "15:55"]]`
- `screener_windows`: `[["09:30", "15:00"]]`

Strategy-specific knobs:

- `tradable`: the fixed list of symbols to trade.
- `index_symbols`: index ETFs streamed for directional confirmation. Default is the SPDR Select Sector ETFs that cover the default tradable universe's sectors (XLK / XLC / XLY / XLF / XLV / XLP). Must include every ETF referenced by `sector_index_map` for actively-traded sectors.
- `sector_index_map`: per-GICS-sector mapping → list of index ETFs to consult for confirming trades on symbols in that sector (default uses the canonical SPDR Select Sector ETFs). Falls back to OR-ing across all `index_symbols` when a sector has no mapping.
- `require_index_confirmation`: gate trend/pullback/vol_squeeze/momentum entries on index agreement. Range and sr_scalp are exempt (mean-reversion theses).
- `require_htf_bias_alignment`: reject longs against bearish HTF (15m) structure and shorts against bullish HTF structure. Neutral never blocks. Default `true` — prevents counter-trend entries on days when the higher-timeframe structure is pinned against the trade direction. Set `false` to allow counter-HTF setups (the bot will still score them normally, but won't outright block).
- `orb_bypass_htf_bias`: skip the HTF bias check during the ORB window (through `orb_end_time`). Default `true`. Set `false` to enforce HTF bias filtering even at the open.
- `orb_bypass_exhaustion`: skip the VWAP/EMA extension exhaustion filters during the ORB window. Default `true`. Set `false` to enforce exhaustion filtering even at the open.
- `orb_bypass_structure_entry`: skip the LTF market-structure block during the ORB window. Default `true`. Set `false` to respect CHoCH_down / bearish-bias-without-BOS_up signals on the LTF chart during the open. (LTF structure is the 5m frame under `structure_ltf_timeframe_minutes: 5` — Fix D.)
- `orb_bypass_sr_entry`: skip the S/R breakdown/breakout block during the ORB window. Default `true`. Set `false` to respect the `breakdown_below_support` flag (or `breakout_above_resistance` for shorts) during the open.
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
- `vol_squeeze_lookback_bars` / `vol_squeeze_max_range_pct` / `vol_squeeze_max_range_atr` / `vol_squeeze_max_width_pct` / `vol_squeeze_breakout_buffer_pct` / `vol_squeeze_min_breakout_volume_ratio` / `vol_squeeze_min_bar_close_position`: vol_squeeze qualification knobs.
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
- `side_decision_min_votes`: minimum votes a side must accumulate to be decided (default `3` of 4 possible).
- `side_decision_max_opposing`: maximum opposing votes allowed for a decision (default `1`). Tighter values demand cleaner consensus.
- `orb_bypass_side_decision`: skip the explicit side decision during the ORB window (through `orb_end_time`) (default `true`).
- `require_entry_confirmation_bar`: require the last fully closed LTF bar to confirm direction (green AND > prior close for LONG; mirror for SHORT) before entry on trend/pullback/momentum/vol_squeeze regimes (default `true`). See section 6f Fix B.
- `sector_groups`: GICS sector groupings for concentration guard.
- `max_same_sector_same_direction`: max same-direction positions per sector.

Also uses these shared stock groups:

- force-flatten (configurable per side)
- entry exhaustion filters
- stock FVG confluence
- adaptive stock trade management
- chart pattern entry/exit gates
- market structure entry/exit gates
- S/R entry/exit gates and level refinement

Current code defaults:

| Option                               | Default                                                                                                                       |
|--------------------------------------|-------------------------------------------------------------------------------------------------------------------------------|
| `tradable`                           | `AAPL, MSFT, NVDA, INTC, AMD, AVGO, TSM, CRM, AMZN, TSLA, HD, LOW, UBER, COST, GOOG, META, NFLX, RBLX, TMUS, JPM, GS, V, LLY` |
| `index_symbols`                      | `XLK, XLC, XLY, XLF, XLV, XLP`                                                                                                |
| `sector_index_map`                   | All 11 GICS sectors mapped to their canonical SPDR Select Sector ETF (XLK, XLC, XLY, XLF, XLV, XLI, XLE, XLP, XLRE, XLU). Materials maps to `[XLB, GDX, COPX]` (XLB is chemicals-heavy so gold/copper miners need GDX/COPX for proper alignment) |
| `early_session_stop_widening_enabled`| `true`                                                                                                                        |
| `early_session_stop_widening_until`  | `10:30`                                                                                                                       |
| `early_session_stop_widening_mult`   | `1.3`                                                                                                                         |
| `require_index_confirmation`         | `true`                                                                                                                        |
| `require_htf_bias_alignment`         | `true`                                                                                                                        |
| `orb_bypass_htf_bias`                | `true`                                                                                                                        |
| `orb_bypass_exhaustion`              | `true`                                                                                                                        |
| `orb_bypass_structure_entry`         | `true`                                                                                                                        |
| `orb_bypass_sr_entry`                | `true`                                                                                                                        |
| `orb_bypass_screener_bias`           | `true`                                                                                                                        |
| `reject_entry_near_broken_level`     | `true`                                                                                                                        |
| `broken_level_min_clearance_pct`     | `0.0025`                                                                                                                      |
| `broken_level_min_clearance_atr`     | `0.72`                                                                                                                        |
| `reject_target_beyond_sr`            | `true`                                                                                                                        |
| `target_max_sr_ratio`                | `0.8`                                                                                                                         |
| `reject_range_during_squeeze`        | `true`                                                                                                                        |
| `min_bars`                           | `150`                                                                                                                         |
| `ltf_minutes`          | `1`                                                                                                                           |
| `ltf_indicator_span_scale` | `5`                                                                                                                       |
| `ltf_ema_fast_span`        | `45`                                                                                                                      |
| `ltf_ema_slow_span`        | `100`                                                                                                                     |
| `htf_minutes`              | `15`                                                                                                                          |
| `min_ltf_bars`                   | `120`                                                                                                                         |
| `min_trend_score`                    | `3.5`                                                                                                                         |
| `min_pullback_score`                 | `3.5`                                                                                                                         |
| `min_pullback_trend_score`           | `3.0`                                                                                                                         |
| `min_range_score`                    | `3.5`                                                                                                                         |
| `min_vol_squeeze_score`              | `4.0`                                                                                                                         |
| `min_momentum_score`                 | `4.0`                                                                                                                         |
| `min_sr_scalp_score`                 | `3.5`                                                                                                                         |
| `min_adx14`                          | `15.0`                                                                                                                        |
| `pullback_ema_touch_atr_mult`        | `0.35`                                                                                                                        |
| `pullback_hold_atr_mult`             | `0.40`                                                                                                                        |
| `pullback_lookback_bars`             | `25`                                                                                                                          |
| `range_max_vwap_dist_pct`            | `0.0020`                                                                                                                      |
| `range_max_ema_gap_pct`              | `0.0008`                                                                                                                      |
| `range_min_flip_count`               | `3`                                                                                                                           |
| `range_lookback_bars`                | `20`                                                                                                                          |
| `trend_target_rr`                    | `2.0`                                                                                                                         |
| `pullback_target_rr`                 | `2.0`                                                                                                                         |
| `vol_squeeze_target_rr`              | `2.05`                                                                                                                        |
| `momentum_target_rr`                 | `2.0`                                                                                                                         |
| `vol_squeeze_lookback_bars`          | `12`                                                                                                                          |
| `vol_squeeze_max_range_pct`          | `0.012`                                                                                                                       |
| `vol_squeeze_max_range_atr`          | `1.8`                                                                                                                         |
| `vol_squeeze_max_width_pct`          | `0.05`                                                                                                                        |
| `vol_squeeze_breakout_buffer_pct`    | `0.0008`                                                                                                                      |
| `vol_squeeze_min_breakout_volume_ratio` | `1.12`                                                                                                                    |
| `vol_squeeze_min_bar_close_position` | `0.63`                                                                                                                        |
| `momentum_breakout_lookback_bars`    | `6`                                                                                                                           |
| `momentum_min_day_strength`          | `1.5`                                                                                                                         |
| `sr_scalp_min_distance_pct`          | `0.008`                                                                                                                       |
| `sr_scalp_min_distance_atr`          | `2.5`                                                                                                                         |
| `sr_scalp_max_distance_from_zone_atr` | `0.5`                                                                                                                        |
| `disable_orb_window`                 | `false`                                                                                                                       |
| `directional_bias_min_day_strength`  | `0.20`                                                                                                                        |
| `disable_trend_regime`               | `false`                                                                                                                       |
| `disable_pullback_regime`            | `false`                                                                                                                       |
| `disable_range_regime`               | `false`                                                                                                                       |
| `disable_vol_squeeze_regime`         | `false`                                                                                                                       |
| `disable_momentum_regime`            | `false`                                                                                                                       |
| `disable_sr_scalp_regime`            | `false`                                                                                                                       |
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
| `max_same_sector_same_direction`     | `2`                                                                                                                           |
| `force_flatten`                      | `{'long': true, 'short': true}`                                                                                               |

## Files in this folder

- `manifest.json` defines the plugin registration metadata and factory defaults.
- `configs/config.top_tier_adaptive.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` fetches the fixed tradable universe from TradingView and ranks by activity.
- `strategy.py` contains the regime scoring, signal building, and entry logic.
