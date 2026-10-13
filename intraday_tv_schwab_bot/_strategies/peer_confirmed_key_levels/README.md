# Peer Confirmed Key Levels

This file documents the strategy that lives in this folder. The behavior described here is based on the current shipped strategy code, the matching top-level preset under `configs/`, the manifest defaults, and the package-level README.

## How it works

This is the **peer-confirmed hourly key-level strategy**. It is built around the idea that a stock-level setup becomes more trustworthy when three things line up at the same time:

1. the symbol is reacting to an important higher-timeframe level or zone,
2. its peers are confirming the same directional idea, and
3. optional macro context is not fighting the trade.

It is one of the more structured strategies in the package because it combines level selection, trigger scoring, peer voting, and post-entry ladder management.

### 1. The screener lists the tradable symbols; the side is decided at the level

Every configured `tradable` symbol is a candidate on each refresh (one the TradingView query did not return is still listed, with a warning), scored by |change from open| × effective relative volume and sorted by that score. The screener sets no directional bias: the strategy evaluates both sides at its levels. The confirmation universe is `tradable` + `peers` (the peers that are also tradable dropped), less the candidate itself.

### 2. It builds hourly support/resistance levels and zones

The core map is the symbol's own 60m HTF context (`htf_minutes`): clustered pivots, the flip-confirmed broken levels, and the prior day / week high and low as fallbacks for an empty side. Each candidate level on the trade's side is scored from the symbol's own data, no peer's:

- `level_score` = `level_score_raw_htf_weight` (0.65) × max(the HTF cluster score, base) + 0.35 × (base + local confluence);
- base = max(1.5, the source priority: 3 a prior-week level, 2 a prior-day level, 1.75 an HTF FVG);
- local confluence: +1 for 2+ touches, +1 on a round number, +1 near the HTF fast EMA, +1 near the slow EMA.

Same-side levels within the HTF tolerance collapse into the strongest. Proximity is only a selection tie-break (section 3) and target clearance is a separate gate (section 6).

The zone around a level is max(`zone_atr_mult` × the trigger frame's ATR, `zone_pct` × price), capped by the gaps to the neighbouring levels and floored at `zone_pct` × price. On the large caps this strategy trades, 0.22 × the 5m ATR is about 5 bps against the 16-bps floor, so in practice the zone is the `zone_pct` floor (measured 2026-10-13: the floor binds in 99.9% of RTH minutes).

### 3. It chooses the active level before it evaluates the trigger

The strategy does not first decide “I want to go long” and then find a level. For each side, a candidate level is *touched* when any of the last 4 completed trigger buckets traded into its zone; the touched levels are ranked by (the trigger previewed at that level, `level_score`, source priority, nearness in ATR, the selection score), and only the winner is then gated on `min_level_score` (`<side>_level_score_below_min`). The dashboard marks the same selected level, read on the completed buckets.

### 4. It scores the trigger separately from the level

Once a level is selected, the strategy evaluates the actual interaction on the trigger timeframe (`ltf_minutes`, 5m), on **completed buckets only**. The trigger frame is the 1m step frame resampled; its last bucket is still forming 1-5 minutes into it, and is left out until it completes: a bucket is complete once the step frame holds a bar that ends at the bucket's end, so a bucket whose last minute has not arrived yet is not read on the clock alone. **Each completed bucket is judged once**: the first pass that sees it complete evaluates the whole setup (level, gates, trigger, stop, targets) and an entry can fire on that pass; every later pass repeats that verdict (`ltf_bucket_repeat` in the decision's details; a bucket that signalled reads `ltf_bucket_signalled(bucket=HH:MM)`) until the next bucket completes. Until 2026-10-13 the trigger read the forming bucket — a 1-minute candle against full 5m ones about 82% of the time, with up to five looks a bucket — while the S/R flip and the structure context already left it out.

**A bucket is judged only in time**: while the step frame ends within one 1m step of the bucket's end. On the normal loop the pass that first sees a bucket complete runs a moment after its last minute's bar arrives; a thin name with no print in that minute completes the bucket with its next minute's bar, one step late, and is still judged. A bucket first seen later — the gatekeeper held the symbol back through a cooldown, an open position, a restart or a stream-inactive spell while the step frame moved on — is skipped unjudged as `ltf_bucket_missed(bucket=HH:MM,late=Xm)` (repeated, like a verdict, until the next bucket completes), and the pass waits for the next bucket: a signal never reads evidence that closed minutes before the price it would enter at. The 1m variant's bucket is the step frame's newest bar, so it is never late.

**Nor while its 1m backfill is owed** (2026-10-13): after the stream (re)starts, or reconnects having missed bars, the symbol's 1m frame has a hole until a price_history fetch cut after the first new bar answers (the data feed's `pending_stream_backfill`). The first bar after the gap can complete a bucket before the history phase fetches it (a fetch backed off after failures, or the bar landing after that phase), so the pass records `ltf_bucket_backfill_pending(bucket=HH:MM,first_bar=HH:MM)` (divergence-ineligible) and keeps no verdict: a later pass judges the bucket once the backfill is in, or misses it once it is late. Until then the first look judged the holed bucket and its verdict stood after the backfill repaired it.

**One candidate's error is its own** (2026-10-13): a candidate whose judgement raises is skipped as `entry_pass_error(error=<type>)` (divergence-ineligible), its bucket keeps no verdict, so the next pass judges it again while it is in time, and the pass goes on with the other candidates. The error is logged with its type, the traceback on the first failure of a run for the symbol and every 10th after. Until then the raise ended the pass, and a candidate judged earlier in it kept a verdict for a signal the entry stage never saw.

The **4-part base trigger** (`ltf_base_score`):

- a CHoCH: the close beyond the prior 3 buckets' high (a LONG) / low (a SHORT), +1;
- a volume spike: over 1.5× the prior 20 buckets' mean volume, +1;
- the candle model's confirmation (the shared weighted 1/2/3-bar model): +0.35 / 0.70 / 1.0;
- a **sweep-reclaim / sweep-reject**, +1, only for a real sweep: within the last 4 buckets a low traded *below* the level (a high *above* it, for a SHORT), and the trigger bucket closed back at or above it (at or below it). Until 2026-10-13 a low anywhere inside the zone above the level counted — the very touch that selects the level, on a level that sits on the trade's side of the close by construction — so every selected setup scored it.

`min_ltf_score` gates the **base** score (`ltf_base_score_below_min`; it can be at most 4.0, the base's ceiling). A signal records the gate's input as `ltf_base_score` and its requirement as `ltf_score_required` (= `min_ltf_score`, the requirement on the base score, not on `ltf_score`); the ENTRY_CONTEXT log carries both since 2026-10-13. A capped **trigger-quality bonus** (`ltf_quality_bonus_enabled`, up to `ltf_quality_max_bonus`) grades the reclaim / reject, how far price used the zone, candle quality, volume and range expansion; it only ranks: it adds to `ltf_score` (the gatekeeper's rank field), the level selection's preview and the strong-setup bar, and no longer counts toward `min_ltf_score` (until 2026-10-13 it did, up to 2.0). The bonus's candle, volume and range terms, and the 1-bar candle tier, need a CHoCH, a candle or body confirmation or a real sweep; with the old sweep always present they never waited for one.

So the setup has two different quality dimensions:

- **level quality**: is this an important enough hourly level?
- **trigger quality**: is price interacting with it in a tradable way right now?

A setup can fail either side independently.

### 5. Peer and macro confirmation are part of the gate, not decoration

Once a side's level passes `min_level_score`, the side's gates run in this order, the cheap directional ones before the trigger: the HTF bias (close vs the HTF fast EMA, fast vs slow, and the context's trend: 2 of 3; `require_htf_bias_alignment` refuses anything not aligned), the peers, the macro symbols, then the trigger (section 4), the target clearance and the R:R (section 6).

- **Peers**: each other symbol of the confirmation universe votes bullish, bearish or neutral from its 1m close vs its HTF fast EMA, its 5m posture (close vs VWAP, EMA9 vs EMA20) and its close vs today's session open. The side needs `min_peer_agreement` fresh peers agreeing and a directional score of at least `min_peer_score` (`peer_confirmation_insufficient_<side>`, with `:not_fresh=N` when peers had no fresh vote): the fresh peers agreeing less those disagreeing, less every peer with no fresh vote (below).
- **Macro**: the dollar (`dollar_symbol`, $NYICDX), bonds (`bond_symbol`, TLT) and volatility (`volatility_symbol`, VIX) each agree with longs or shorts, or are neutral. With `enable_macro_confirmation` on, the side needs `require_macro_agreement_count` of them agreeing and, with `require_macro_net_bias`, more agreeing than opposing, every macro symbol with no fresh vote counted as opposing (`macro_net_bias_insufficient_<side>:long=1<=short=0+not_fresh=2`).
- **Age (2026-10-13)**: a vote is only as good as its data. A peer whose newest 1m bar started more than `peer_max_age_minutes` (3) ago, or a macro symbol more than `macro_max_age_minutes` (20) ago, is `stale` in `peer_details` / `macro_details`, and one with no frame `missing`. Neither ever agrees, and each counts AGAINST the side, the worst case: in the peer score and in the macro net bias. Fewer fresh peers than `min_peer_agreement` refuses the side as `peers_stale(fresh=…,required=…,max_age=…,stale=SYM@4.2m|…,missing=…)`, and, with macro confirmation on, fewer fresh macro symbols than `require_macro_agreement_count` as `macro_stale(…)`: the side fails closed on old data. Until 2026-10-13 a stale frame voted as if current: VIX and $NYICDX are polled every `history_poll_seconds` (300 s in the preset) and print nothing after about 16:15 / 17:00, so from then to 19:55 two of the three macro votes were their 15:5x bucket; and while stale votes only abstained, three fresh bullish peers and three stale ones read +3, where the same votes read 0 when current. What this costs: once VIX or $NYICDX is stale a side needs two fresh macro symbols agreeing to clear the net bias, and once both are, no side clears it on TLT alone: with `require_macro_net_bias` on, the preset opens nothing from then to its 19:40 entry cutoff. On the archived days the bot saw their live 1m series end at 15:59 (05-01, 05-04; Schwab's next-day history runs to 16:15 / 16:59), so both read stale from about 16:20.

A stock can be sitting on a good hourly zone and still be skipped because its peer basket or macro basket is not agreeing strongly enough, or is not trading.

### 6. The shared entry stage

Every side that passes these gates — plus key_levels' own target-clearance check, `require_peer_target_clearance` below — is handed to the shared entry stage (`shared_entry.SharedEntryPolicy`, 2026-09-24), like every strategy's setup. It runs the `shared_entry` vetoes the YAML switches on (structure, S/R, chart, candle, dual divergence, broken level), the S/R and technical stop/target refinement, and the score terms (the FVG term, the HTF RSI divergence term on this strategy's own 60m context, and the entry-context terms). Its frames: the vetoes and the structure / technical / chart / candle contexts read the 5m trigger frame the setup was read on, S/R is built on the 1m frame, and the FVG term — and the runner's continuation bias — read the 1m frame, as this strategy always has. Two ATRs follow from that: the refinement's stop floor (`shared_entry.min_stop_atr_mult`) is in ATR of the 5m trigger frame the stop is priced on, while the broken-level guard's `broken_level_min_clearance_atr` — like the divergence entry candidates — reads the 1m S/R frame, the frame top_tier's thresholds were set on (a 1m ATR is about 0.4 of a 5m one on these names: 2.56x in RTH, measured 2026-10-13). A refusal lists every blocker under the side's `long.` / `short.` prefix. Until 2026-09-24 key_levels reached three of those knobs (the FVG term, the HTF divergence term and `use_sr_filter`, which gated its own clearance check); the preset keeps the others off for parity except `use_structure_filter` (user decision: on).

- `require_peer_target_clearance` (default `true`): the side is refused as `too_close_to_overhead_resistance` / `too_close_to_nearby_support` when the NEAREST HTF key level in the trade's direction is inside BOTH `support_resistance.entry_min_clearance_pct` and `entry_min_clearance_atr` (5m ATR); clearing either one passes. It rode on `shared_entry.use_sr_filter` until 2026-09-24; that knob is the shared S/R veto now, which refuses inside EITHER clearance, against the S/R context. `support_resistance.enabled: false` still turns it off.

**The entry spread guard (2026-10-13).** At the order, the entry stage asks the strategy's hook (`entry_spread_refusal`, the strategy-neutral `shared_entry.entry_spread_guard`, the same guard as top_tier's README section 6g): an entry whose quote's spread is at least `max_entry_spread_r` × its risk |limit − stop|, or at least `max_entry_spread_target_frac` × its target distance |target − limit|, is skipped as `entry_spread_too_wide(…)`, and a quote whose spread cannot be read (no positive bid or ask, or crossed) as `entry_spread_unreadable(…)`: the guard fails closed. A refused signal is not offered again: the strategy judges each completed bucket once (section 4), so the gatekeeper's stream-book watch (top_tier README, section 6g) ends for want of a signal, and the next bucket is judged afresh. Both knobs `0` (the manifest's default) turn it off. The shipped preset sets top_tier's `0.33` / `0.5`: it trades 07:00-19:55 on stops sized on the 5m ATR, and in the post-market the median stop is about 14 bps, so a spread there is a large part of R (measured 2026-10-13: spread / R 0.07 in RTH, 0.15+ post-market). Each knob is checked at load.

The strategy proposes its stop and target from the signal bar's close; the R:R is not re-checked at the fill price (queued: the entry stage's R:R at the order price).

The signal carries `regime: key_level`, `entry_style_family: peer`, `strategy_priority_score` (the strategy's own sum), `shared_context_score` and `final_priority_score` = the two added — the total it summed itself before, as long as the YAML keeps the entry-context score terms off (the preset does). `selection_quality_score` is still `final_priority_score + 0.20 × the level's selection score`; the gatekeeper ranks on `ltf_score + 0.5 × shared_context_score` (manifest `signal_priority.shared_score_weight`, user decision 3), then the manifest's tail.

### 7. Stop, target, and ladder behavior

This strategy is especially strong in post-entry management because it knows the surrounding hourly ladder. It can:

- place the initial stop beyond the defended zone with ATR buffering
- target the next qualifying level(s)
- emit rung metadata for `adaptive_ladder` management: the targeted rung is a plain take-profit; with `shared_exit.adaptive_ladder_touch_hold` on, the bar that touches it decides whether to promote the rung (stop just under it, which also becomes the defended level, and the next rung as the target) or exit (top_tier README, "Adaptive ladder")
- exit on the ladder defence (its `strategy_exit_signal` hook, after every shared exit held): the defended zone flipped (`ladder_support_lost` / `ladder_resistance_lost`), or an HTF CHoCH / BoS against the trade with the close through every EMA / VWAP reference that has a value (`ladder_structure_fail_long` / `_short`). The break must have happened after entry — its `htf_minutes` bar closed after the fill. Until 2026-09-24 any break still inside `htf_structure_event_lookback_bars` counted, a whole session of 60m bars, so a trade taken against a morning CHoCH exited on its first cycle with the close under its references.
- judge its protective exits on the executable side (`protective_exits_on_executable_side`, 2026-10-13; manifest `false`, the shipped preset `true`): a stop at or past the entry and the peak giveback floors fire when the bid (a LONG) or the ask (a SHORT) of the pass's quote reaches the level, instead of the mark, with the same guard and record as top_tier's (top_tier README, section 12). Before, stops, the break-even and the giveback were judged on the mark in thin extended-hours books.
- flatten every position from the force-flatten cutoff (`force_flatten`, both sides `true` in the shipped preset since 2026-10-13): the management window's end less `force_flatten_buffer_minutes` (5), 19:50 ET in the preset (12:55 on an early-close day), while the post-market takes orders (to 20:00). No side opens within `entry_cutoff_lead_minutes` (10) of that cutoff, from 19:40 (12:45): the entry pass skips such a side (`<side>.after_entry_cutoff(cutoff=19:40,flatten=19:50)`, or `after_entry_cutoff(…)` for the symbol when every side it trades is past it), since the flatten would close the position within minutes, two crossings of the post-market spread for nothing. A divergence-only entry (`shared_entry.use_divergence_entry_signal`, off in the preset) proposes no such side either (the strategy's `closed_entry_sides`), when only one side is. htf_pivots' and trend_continuation's entry passes make the same check. A position the flatten has not closed by 20:00 (an order unfilled in the thin post-market, an outage, a restart across 19:50-20:00, which `restore_hybrid` restores) is carried overnight and flattened at the next session's first management pass, 07:00 ET in the pre-market, with one WARNING naming it (`should_force_flatten`; a side whose `force_flatten` is off is held, as it asks). Until 2026-10-13 the preset held longs overnight, from 19:55 to the next 07:00, unmanaged on a 5m-ATR stop (the median overnight gap was 1.8× the RTH-median R), then managed a carried position as a new one all the next session; a SHORT could enter between 19:50 and 19:55 to be flattened on its next pass, and the 19:40 bucket, judged at 19:45, open a position the 19:50 flatten closed.

Under `adaptive_ladder` (the shipped preset) some knobs do nothing: the runner extension (`adaptive_runner_trigger_rr`, `fvg_runner_rr_bonus`) and the trail (`risk.trailing_stop_pct`) are off for laddered positions, and the profit lock (1.25R → +0.32R) never binds before the peak giveback's floor, which runs first.

The strategy proposes its stop and the FARTHEST qualifying rung as the target; the shared refinement may pull the stop in and cap the target. The rungs are then re-qualified from the refined stop at `min_rr` — none past the refined target (`no_qualifying_target_rr_after_refine` when none is left) — so the ladder's R multiples are the traded stop's, and the signal targets the first rung (or the strong-setup offset rung).

That makes it more structurally anchored than simple percent-stop / percent-target systems.

### 8. What a strong setup looks like

The best key-level setup usually looks like:

- price is interacting with a meaningful hourly level or zone
- peers are leaning the same way
- macro is not fighting the move
- the trigger bar around the zone is clean enough to score well
- there is enough target clearance to justify the trade

In plain English:

**“This strategy wants an hourly battleground that matters, confirmation that the neighborhood agrees, and a lower-timeframe trigger that proves the level is actually being defended or lost right now.”**

## Shipped reference

Purpose: trade around hourly key levels/zones only when a tradable symbol, its peer basket, and optional macro symbols agree strongly enough, then take profit at the targeted rung of the cleaned S/R ladder, exiting early if price loses the level the entry defends (with `shared_exit.adaptive_ladder_touch_hold` on, a strong close at a rung moves the target to the next rung and the defended level to that rung).

Default windows (the manifest's; the shipped preset runs all three 07:00-19:55):

- `entry_windows`: `[['07:10', '15:15']]`
- `management_windows`: `[['07:00', '15:58']]`
- `screener_windows`: `[['07:00', '15:35']]`

Strategy-specific knobs:

- Universe and HTF map:
  - `tradable`, `peers`
  - `htf_minutes`, `htf_lookback_days`, `htf_pivot_span`, `htf_max_levels_per_side`, `htf_atr_tolerance_mult`, `htf_pct_tolerance`, `htf_stop_buffer_atr_mult`, `htf_ema_fast_span`, `htf_ema_slow_span`
- Trigger frame and warmup:
  - `ltf_minutes`, `min_bars`, `min_ltf_bars`
  - Stronger signals are prioritized lexicographically by trigger quality, level quality, peer confirmation, vote edge, and clearance before smaller additive bonuses are allowed to break ties.
- Zone, score, and R:R:
  - `zone_atr_mult`, `zone_pct`, `min_level_score`, `min_ltf_score` (the base trigger, section 4), `min_rr`, `stop_buffer_atr_mult`
  - `require_peer_target_clearance` (the target clearance, section 6)
  - `ltf_quality_bonus_enabled`, `ltf_quality_max_bonus`, `ltf_reclaim_quality_bonus_cap`, `ltf_zone_interaction_bonus_cap`, `ltf_candle_quality_bonus_cap`, `ltf_volume_quality_bonus_cap`, `ltf_range_expansion_bonus_cap`
- Peer confirmation:
  - `min_peer_agreement`, `min_peer_score`, `peer_max_age_minutes` (section 5)
- Macro confirmation:
  - `enable_macro_confirmation`, `require_macro_agreement_count`, `require_macro_net_bias`, `dollar_symbol`, `bond_symbol`, `volatility_symbol`, `macro_max_age_minutes` (section 5)
- The order and the exits:
  - `max_entry_spread_r`, `max_entry_spread_target_frac` (the entry spread guard, section 6)
  - `protective_exits_on_executable_side`, `force_flatten`, `force_flatten_buffer_minutes`, `entry_cutoff_lead_minutes` (section 7)
- The base strategy's management and ladder reads (declared and checked since 2026-10-13):
  - `adaptive_breakeven_offset_r`, `adaptive_partial_breakeven_rr` (null: off), `adaptive_partial_breakeven_offset_r`, `adaptive_runner_target_rr` and `adaptive_runner_trail_pct` (null: derived from the signal's target and from `risk.trailing_stop_pct`)
  - `ladder_min_target_rr`, `ladder_zone_atr_mult`, `ladder_max_rungs`: the ladder a divergence-only entry is priced on (this strategy's own entries build theirs from the HTF levels, section 7)
- Level scoring detail:
  - `level_round_number_tolerance_pct`
- Strong-setup runner / ladder logic:
  - `strong_setup_runner_enabled`, `strong_setup_min_ltf_score`, `strong_setup_min_level_score`, `strong_setup_min_peer_score`, `strong_setup_min_htf_vote_edge`, `strong_setup_target_level_offset`
  - When `risk.trade_management_mode: adaptive_ladder` is active, this strategy stores rung metadata at entry and defends the entry level (the ladder defence exit). The targeted rung is a take-profit; with `shared_exit.adaptive_ladder_touch_hold` on, a strong touch bar moves the target to the next rung past its close (or clears it past the last rung) and the stop and the defended level to the touched rung. Non-ladder strategies safely fall back to adaptive management.

Also uses these shared stock groups:

- force-flatten
- stock FVG confluence
- adaptive stock trade management

**Checked at load (2026-10-13).** Every knob above, and the shared FVG and adaptive-management knobs the manifest declares, is checked when the config loads (`normalize_params`) and again when the strategy is built (`check_knobs`): present, of its type (a number, a whole number, `true` / `false`, a symbol, a list of symbols, `force_flatten`'s `long:` / `short:` mapping, a side it leaves out being flattened) and inside its range, with `ltf_minutes` one of 1, 2, 3, 5, 6, 10, 15 or 30, `htf_minutes` longer than it, `min_peer_agreement`, `min_peer_score` and `strong_setup_min_peer_score` no more than the peers that can vote for a candidate (`tradable` + `peers`, less the candidate), `require_macro_agreement_count` no more than the macro symbols set (while macro confirmation is on), `min_ltf_score` at most 4.0, both vote age limits at least 2 minutes (a 1m bar is a minute old when its minute closes), and each adaptive stop move under the peak R that arms it (the profit lock's stop, the breakeven's offset, the partial breakeven's while it is on). The bounds against the rest of the config are checked when the strategy is built, at startup before the first cycle (the load sees the params alone): `force_flatten_buffer_minutes`, and with `entry_cutoff_lead_minutes` added, shorter than the last management window, so both cutoffs fall inside it (a buffer of 1500 loaded and clamped the cutoff to 00:00 until 2026-10-13), and, while macro confirmation is on, `macro_max_age_minutes` at least `runtime.history_poll_seconds` / 60 + 2 (7 at the preset's 300 s), so a polled macro symbol is not stale between two polls. A bad value refuses to start, naming the key. The base strategy's reads of these params (`force_flatten_buffer_minutes`, the adaptive management's and the divergence ladder's above) are declared in the manifest at what the base read without them, and checked with the rest: until 2026-10-13 a value among them that is no number raised mid-session (the buffer in the flatten check, at the first management pass of a position held on a flattened side, a management knob at a signal's build) or, the runner trail, read as unset, and silently a fraction was cut to a whole number (the buffer, the rung cap), a negative was clamped and a 0 read as the default (the ladder knobs, the runner target). Until then min_rr -3, a `min_peer_agreement` of `banana` (raised inside the entry pass), a `require_macro_agreement_count` of 7 (it never traded) and an `ltf_minutes` of 7 all loaded. A params key no check knows, such as a misspelled one, still loads unread: the framework has no strategy-params schema yet. htf_pivots and trend_continuation check the family's knobs (the HTF map, the trigger frame, peers, macro, the ages, the spread guard, the protective exits, `force_flatten` and its buffer, the shared management, ladder and FVG knobs), not yet their own.

Current package defaults:

| Option                               | Current package default                   |
|--------------------------------------|-------------------------------------------|
| `tradable`                           | `['AAPL', 'NVDA', 'GOOG', 'AMD', 'INTC']` |
| `peers`                              | `['QQQ', 'AVGO', 'MU', 'TSM']`            |
| `htf_minutes`              | `60`                                      |
| `htf_lookback_days`                  | `60`                                      |
| `htf_pivot_span`                     | `2`                                       |
| `htf_max_levels_per_side`            | `6`                                       |
| `htf_atr_tolerance_mult`             | `0.35`                                    |
| `htf_pct_tolerance`                  | `0.003`                                   |
| `htf_stop_buffer_atr_mult`           | `0.25`                                    |
| `htf_ema_fast_span`                  | `34`                                      |
| `htf_ema_slow_span`                  | `200`                                     |
| `ltf_minutes`          | `5`                                       |
| `min_bars`                           | `80`                                      |
| `min_ltf_bars`                   | `18`                                      |
| `zone_atr_mult`                      | `0.22`                                    |
| `zone_pct`                           | `0.0016`                                  |
| `min_level_score`                    | `2.9`                                     |
| `min_ltf_score`                  | `2.5`                                     |
| `ltf_quality_bonus_enabled`      | `true`                                    |
| `ltf_quality_max_bonus`          | `2.0`                                     |
| `ltf_reclaim_quality_bonus_cap`  | `0.8`                                     |
| `ltf_zone_interaction_bonus_cap` | `0.5`                                     |
| `ltf_candle_quality_bonus_cap`   | `0.5`                                     |
| `ltf_volume_quality_bonus_cap`   | `0.4`                                     |
| `ltf_range_expansion_bonus_cap`  | `0.4`                                     |
| `min_rr`                             | `1.75`                                    |
| `stop_buffer_atr_mult`               | `0.68`                                    |
| `min_peer_agreement`                 | `2`                                       |
| `min_peer_score`                     | `2`                                       |
| `peer_max_age_minutes`               | `3`                                       |
| `enable_macro_confirmation`          | `true`                                    |
| `require_macro_agreement_count`      | `1`                                       |
| `require_peer_target_clearance`      | `true`                                    |
| `dollar_symbol`                      | `NYICDX`                                  |
| `bond_symbol`                        | `TLT`                                     |
| `volatility_symbol`                  | `VIX`                                     |
| `macro_max_age_minutes`              | `20`                                      |
| `level_round_number_tolerance_pct`   | `0.002`                                   |
| `strong_setup_runner_enabled`        | `true`                                    |
| `strong_setup_min_ltf_score`     | `3.2`                                     |
| `strong_setup_min_level_score`       | `3.4`                                     |
| `strong_setup_min_peer_score`        | `2`                                       |
| `strong_setup_min_htf_vote_edge`  | `1`                                       |
| `strong_setup_target_level_offset`   | `1`                                       |
| `activity_score_weight`              | `0.11`                                    |
| `htf_fvg_entry_weight`               | `0.36`                                    |
| `ltf_fvg_entry_weight`        | `0.2`                                     |
| `opposing_fvg_entry_penalty_mult`    | `1.0`                                     |
| `fvg_runner_rr_bonus`                | `0.14`                                    |
| `adaptive_breakeven_rr`              | `0.9`                                     |
| `adaptive_profit_lock_rr`            | `1.25`                                    |
| `adaptive_profit_lock_stop_rr`       | `0.32`                                    |
| `adaptive_runner_trigger_rr`         | `1.12`                                    |
| `adaptive_breakeven_offset_r`        | `0.05`                                    |
| `adaptive_partial_breakeven_rr`      | `null` (off)                              |
| `adaptive_partial_breakeven_offset_r` | `0.0`                                    |
| `adaptive_runner_target_rr`          | `null` (derived)                          |
| `adaptive_runner_trail_pct`          | `null` (derived)                          |
| `ladder_min_target_rr`               | `1.2`                                     |
| `ladder_zone_atr_mult`               | `0.5`                                     |
| `ladder_max_rungs`                   | `4`                                       |
| `level_score_raw_htf_weight`         | `0.65`                                    |
| `max_entry_spread_r`                 | `0.0` (off; the preset `0.33`)            |
| `max_entry_spread_target_frac`       | `0.0` (off; the preset `0.5`)             |
| `protective_exits_on_executable_side` | `false` (the preset `true`)              |
| `force_flatten`                      | `{'long': True, 'short': True}`           |
| `force_flatten_buffer_minutes`       | `5`                                       |
| `entry_cutoff_lead_minutes`          | `10`                                      |

`level_score_raw_htf_weight` (a number in [0, 1], checked at load) weights the raw HTF cluster strength against the level's own base and local confluence in `level_score` (section 2); no peer data enters it. `force_flatten` is the end-of-window flatten policy — both directions auto-flatten at the management window's end less `force_flatten_buffer_minutes` (5), no side opens within `entry_cutoff_lead_minutes` (10) of that cutoff, and a position carried past a session is flattened at the next session's first management pass; the shipped preset keeps both on (section 7).

## Files in this folder

- `manifest.json` defines the plugin registration metadata.
- `configs/config.peer_confirmed_key_levels.yaml` is the matching top-level tuned preset for this strategy.
- `screener.py` builds the candidate list for this strategy.
- `strategy.py` contains the actual entry / exit logic.
