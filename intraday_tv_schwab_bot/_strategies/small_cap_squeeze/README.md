# small_cap_squeeze

A **long-only, multi-regime small-cap "squeeze" strategy**. It hunts low-float
stocks that are already running premarket on heavy volume — names that are
squeezing or setting up to squeeze — and trades them with the
`top_tier_adaptive` multi-regime engine, taking quick scalps or riding the big
moves.

## How it works

### 1. Dynamic screener universe (no fixed list)
Unlike `top_tier_adaptive` (a fixed mega-cap list), this strategy has **no
`tradable` list** — it screens TradingView for a fresh universe. With the
shipped `watchlist_mode: premarket_lock_rth_live`, premarket gappers are
**sticky** (locked once they qualify, so the pre-open universe doesn't churn);
at the 09:30 open it switches to a live RTH re-screen **unioned** with the
locked premarket names (kept warm for a VWAP reclaim), capped to
`tradingview.max_candidates` by activity. Set `watchlist_mode: none` to
re-screen every cycle with no lock. Filters (long bias, ranked by gap% × clipped RVOL):

| Filter | Value | TV field |
|---|---|---|
| Price | $2 – $20 | `close` (→ `premarket_close` pre-RTH) |
| **Float** | **400K – 20M shares** | `float_shares_outstanding_current` |
| Relative volume | ≥ 2.0 | `relative_volume_10d_calc` |
| Change from open | ≥ 5% | `change_from_open` (→ `premarket_change` pre-RTH = premarket gap from prior close) |
| Volume | ≥ 5M | `volume` (→ `premarket_volume` pre-RTH) |

The three canonical fields (`close` / `change_from_open` / `volume`)
auto-resolve to their **premarket** variants before 09:30 ET, so the same
thresholds mean *premarket* close/change/volume during the 08:05–09:30 window
and *RTH* values after the open. Low **float** is the squeeze fuel.

### 2. Regimes
Narrowed (2026-06-02, after two dry-runs) to the **momentum-continuation** set:
**trend, momentum**, plus the opt-in **vwap_reclaim** regime
(`disable_vwap_reclaim_regime: false`) — a long re-entry when price flushes below
session VWAP and reclaims it on a volume pop (the squeeze re-igniting), the entry
that trend (needs `close>VWAP` *and* `ema9>ema20`) and momentum (needs a new
N-bar high) miss. **pullback, range, vol_squeeze, ORB, and sr_scalp are off**
(`disable_*_regime`) — pullback bled on both dry-runs, and the mean-reversion /
breakout regimes don't fit a squeeze-continuation thesis. With ORB disabled the
opening-range carve-out is removed too, so the open trades the continuation mix
continuously — entries run **08:05 → 11:50** with no gap.

### 3. What's removed vs top_tier_adaptive
- **No index/ETF confirmation** (`require_index_confirmation: false`, empty `index_symbols`). The empty list means no index ETF anywhere: none is streamed, no sector beta is computed, and the signal's `confirmation_indices` stamp is empty. Until 2026-09-26 the engine read the empty list as SPY / QQQ, which this strategy never streams.
- **No sector support** — empty `sector_groups` / `sector_index_map`, so the sector concentration guard no-ops.
- **No relative-strength gate** (`relative_strength_block_threshold_pct: 0`).
- **No HTF-structure gating** (`require_htf_bias_alignment: false`) — squeezes break prior structure. Its S/R levels still read the stored 15m frame, so a missing or stale frame refuses its entries (section 4).
- **Long only** (`risk.allow_short: false`).
- **Protective exits on the mark**: top_tier can judge its stops at or past the entry (break-even, profit lock) and its peak giveback floors on the bid instead of the mark (`protective_exits_on_executable_side`, top_tier README section 12). This strategy keeps them on the mark: the manifest declares the knob `false`, and the load refuses `true` here until halt handling ships, because a halted small cap's quote can keep moving while its last trade is frozen, and an exit started on it cancels the broker's disaster stop.

### 4. Timeframe + the key squeeze tuning
- **1m LTF, native indicators** (`ltf_indicator_span_scale: 1` → 14 ATR; `ltf_ema_fast_span: 9` / `ltf_ema_slow_span: 20` EMAs) — responsive, with tight ATR-based stops that suit fast scalps. **Raise `ltf_indicator_span_scale` toward 3–5 to smooth signals and widen stops** (primary tuning knob); it no longer moves the EMAs, which the two `ltf_ema_*_span` knobs set on their own.
- **What it inherits from top_tier's 5m setups** (2026-10-13; top_tier README, *Timeframes*): its preset keeps the 1m LTF (`ltf_minutes: 1`, span scale 1), so its windows, ATR, ADX and EMAs read 1m bars as before, and its windows start where its frames do, at its 07:00 extended session; its index confirmation and relative-strength gate are off. What reaches it: the entry pass leaves out a last 1m bar whose minute is still trading (its step frames hold closed bars, so there is seldom one to drop); a window-anchored stop also clears the current bucket's 1m bars, on a 1m LTF the trigger bar's own extreme (`stop_anchor`); the HTF frame refusal: it runs neither HTF gate, but its S/R levels (the shared S/R veto and refinement) read the stored 15m frame, so its trend, momentum and vwap_reclaim entries are refused while the frame is missing (`<regime>_htf_frame_missing(tf=15m)`) or, in RTH, stale, its newest bar ended more than two bars, 30 minutes, ago (`<regime>_htf_frame_stale(...)`; top_tier README section 6, *HTF bias alignment*); the knob checks at load (a strict `true` / `false`, a finite number in its bounds, present in the params; its manifest declares them); and the opening warm-up: a setup needs three closed LTF buckets of today's session, on its 1m LTF three 1m bars from 07:00, counted in bars and not minutes, so a name that prints its first bars late waits for its third (a name with prints at 08:20 and 08:24 is skipped at 08:25 as `ltf_warmup(2/3)`). Its volume baselines count its session's bars, from 07:00, where its frames start, so they read as before; the vol_squeeze stop buffer's box term keeps 0.22 (`vol_squeeze_stop_box_mult`), its 1m box being the 12-minute one the term was tested on (vol_squeeze is off here). Replayed on 2026-06-03 (09:30-11:30) it is identical to the previous version in every recorded category.
- **The confirmation bar is the last closed 1m bar** (2026-10-12; `require_entry_confirmation_bar`, top_tier README section 6f): trend and momentum confirm on the bar they enter at the close of, and a bar whose minute is still trading by the clock never confirms. Until then the gate judged the bar before it, so every trend and momentum entry here confirmed a bar late (vwap_reclaim is exempt from the gate), but the few whose step frame held a forming last bar (in H:'s archived decision events, 14 of 330 on 06-01, 7 of 444 on 06-02 and 12 of 423 on 06-03). On stepped replays of 06-02 and 06-03 it changed one entry: ZJYL's trend long on 06-02 came 19 minutes earlier (10:10, +23.40, where the 10:29 one lost -44.22). The armed retest's own changes (top_tier README section 24) do not apply here: `armed_retest_enabled` is false.
- **A squeezer is *always* extended** (high %B, far above VWAP, big bars), so the mega-cap "don't chase extension" gates are **off**: `reject_stretched_entries`, `entry_exhaustion_filter_enabled`, `reject_oversized_entry_bar`, `reject_tech_bias_contradiction`. Leaving them on would block nearly every squeeze entry. The S/R and target-beyond-SR gates stay on (those keep you from buying straight into resistance), and unlike top_tier the S/R veto still reaches `vwap_reclaim` (see Architecture). The target-beyond-SR gate (`reject_target_beyond_sr`, `target_max_sr_ratio: 0.8`, trend only) passes a first ladder rung ON the nearest resistance since 2026-09-25 — the ladder manages that level; until then the rung builder's rung 1 always sat at or past it, the gate refused every laddered trend entry, and trend traded only as a trail runner (top_tier README section 6d). The broken-level guard (`shared_entry.use_broken_level_guard`, this preset's 0.0025 / 0.72 thresholds carried) ships off since 2026-09-24, with the S/R-loss exit it guarded against.
- **The entry spread guard at top_tier's thresholds** (2026-10-12): the preset sets `max_entry_spread_r: 0.33` and `max_entry_spread_target_frac: 0.5`, so an entry whose quote at the order has a spread of at least 0.33 × its risk |limit − stop| or 0.5 × its target distance |target − limit| is skipped, and so is one whose spread cannot be read (top_tier README section 6g: the guard and its stream book watch). The manifest keeps both at `0` (off); each is checked at load, and `0` turns a test off. Small-cap stops are wide, so it rarely binds: on the 55 small-cap entries of 2026-06-01..06-03 (June code) it would have skipped 3 (HKIT, ZJYL ×2), which lost −44.95 (1 win, 2 losses). It is insurance against the rare wide book at entry, and changes no exit; the small-cap danger in the archive is halts and gaps after entry.
- **Scalp management**: the first S/R rung is the target, a plain take-profit. The stop manages the trade short of it (S1 exit study, 2026-09-26): break-even at 1.2R (`adaptive_breakeven_rr`), the profit lock at 1.8R moving the stop to +1.0R (`adaptive_profit_lock_rr` / `adaptive_profit_lock_stop_rr`), and, for an entry with no ladder rung, the 5% trail (`trailing_stop_pct`, a median 0.32R wide on those entries). Peak giveback manages the runner only above 2.5R on most entries: the high-conviction override (`peak_giveback_high_conviction_min_r: 2.5` at a 5% day strength, which also turns the low tier off) was on 37 of the 43 replayed small_cap entries. Without it the giveback keeps 60% / 70% / 78% of a 1R-2R / 2R-3R / 3R+ peak, and the low tier arms at 0.7R.
  - The adaptive ladder rolls the target up the rungs only with `shared_exit.adaptive_ladder_touch_hold` on (off in the preset): the bar that touches a rung then decides whether to promote it (stop just under the rung, target to the next rung, a runner past the last) or exit (top_tier README, "Adaptive ladder"). Until 2026-09-27 this note described the two-bar zone-flip promotion, removed with the target-exit suppression; no ladder adjustment was logged from 2026-05-01 to 2026-09-25. The replay's one small_cap runner (DXST 2026-06-02) is the case the hold exists for.
  - The adaptive ladder does **not** scale out: a promotion moves the stop and the target, and the position rides **full size**. `partial_exit` on a trade record comes from a broker partial fill or exit recovery, never from a rung. (Corrected 2026-09-19 — an earlier note here and in commit `4af0bf7` described rungs as partial exits.)

### 5. Extended hours
`equity_session_indicator_window: extended` + `extended_hours_tradable_all: true`
— every screened symbol is eligible for premarket entries (the universe is
dynamic, so a hand-listed sublist can't enumerate it). Premarket orders require
`execution.extended_hours_enabled: true`.

## Architecture
`SmallCapSqueezeStrategy` is a thin subclass of `TopTierAdaptiveStrategy` that
only sets `strategy_name` — all behavior is the shared engine, driven by config.
Its entries go through the same shared entry stage (`shared_entry.*`, see the
top_tier README section 6), and its manifest mirrors top_tier's shared-entry
declarations (2026-09-24) with one deliberate difference:
`capabilities.shared_entry.exemptions: {orb: [structure, sr], range:
[structure], pullback: [structure], sr_scalp: [structure]}` and
`capabilities.signal_priority` `{primary_field: regime_score_normalized,
shared_score_weight: 1.0, rank_unit_field: regime_rank_unit}`, so the shared
entry-context + FVG score ranks competing signals at each regime's own scale.

- The range / pullback / sr_scalp structure exemptions are user decisions of
  2026-09-25, shared with top_tier (its README section 6 has the evidence).
  Like the orb one, they are inert here while the preset runs only trend,
  momentum and vwap_reclaim.
- The difference: `vwap_reclaim` keeps the S/R veto here. top_tier's
  manifest exempts it (2026-09-25), because on large caps the veto refused
  15 of 16 reclaim entries, 11 of them winners. In this strategy it refused
  only losers: 3 of the 8 June vwap_reclaim entries, -2.45R. Add
  `vwap_reclaim: [sr]` to this manifest to follow top_tier.

The retired
`orb_bypass_structure_entry` / `orb_bypass_sr_entry` /
`reject_entry_near_broken_level` / `broken_level_min_clearance_*` params fail
at load if a preset still carries them.
The two backward-compatible base-engine flags it depends on
(`disable_orb_regime`, `extended_hours_tradable_all`) default off, so
`top_tier_adaptive` is unchanged. The screener (`screener.py`) is the
microcap-style universe screener with this strategy's filters.

## Tuning
The shipped params are a **starting point** for elevated small-cap volatility,
not a backtested optimum. Dial in over a beta dry-run — most likely levers:
`ltf_indicator_span_scale` (signal speed + stop width), `ltf_ema_fast_span` / `ltf_ema_slow_span` (the entry-scoring EMAs alone), `risk.default_stop_pct`
and `stop_buffer_atr_mult` (stop room), the `adaptive_*` / `peak_giveback_*`
management (scalp-vs-runner balance), and the screener thresholds
(`min_change_from_open`, `min_rvol`, `min_volume`, float band) to widen or
tighten the universe.

Run with: `--config configs/config.small_cap_squeeze.yaml`.
