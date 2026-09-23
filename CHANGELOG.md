# Changelog

All notable changes to `intraday-tv-schwab-bot` will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A skipped entry decision now records which regime it came from.**
  *2026-09-19* — `decisions.csv` carries a `family` column sourced from
  `entry_gatekeeper._decision_entry_family`, which reads
  `details['entry_family']`. top_tier's success path stamped `regime`; the
  skip path passed no details at all, so `family` was `none` on every skipped
  row — and skips are essentially every row (4,316 decisions, 0 trades, in one
  archived session).

  The cost was concrete: a post-mortem could establish that ~20% of RTH
  decisions (22,231 of 109,840 across 13 sessions) die on `no_fresh_breakout`,
  but not whether that was `trend` (25-bar lookback on the LTF, 3 trades ever)
  or `momentum` (6-bar on the base frame, 0 trades) — the difference between a
  tuning problem and a dead regime.

  The skip branch now stamps `entry_family` (the regime that came closest to
  producing a signal, or `none_qualified` when nothing cleared its score
  threshold — a different failure worth telling apart), plus `regime_score`,
  `regime_score_norm` and `regimes_tried`, which separate "nothing was close"
  from "it missed by 0.1 and the threshold may be wrong".

  `entry_family` is the key the gatekeeper already reads, so this populates the
  EXISTING `family=` log field and CSV column — no log-format, parser or schema
  change.

- **`trend` and `momentum` now arm on qualification and enter on the retest.**
  *2026-09-20* — both trigger on `close > max(high of the previous N bars)` (25
  LTF bars, 6 base-1m bars), so the fill sits at the highest price in 25 or 6
  minutes BY CONSTRUCTION, the stop lands inside the retrace that normally
  follows, and `same_level_block_minutes` then bars re-entry at the level where
  the next leg starts.

  The new `entry_timing` block measured it: a trend fill was followed by a
  retrace covering 85% of the way to its stop, against 21% from an arbitrary
  moment in the same session (+0.641R over baseline, 9 trades). `pullback` was
  +0.213R and `vol_squeeze` -0.024R.

  Qualifying now records the level that was cleared and waits for price to come
  back to it (`_armed_retest_verdict`, `ARMED_RETEST_REGIMES`). The retest
  fires the entry; if none comes inside `armed_retest_max_minutes` (12) it
  enters at market, which is the previous behaviour. **That fallback is
  deliberate** — a strong trend day never offers the retest, and forfeiting
  those setups would deepen the "some days it doesn't trade at all" problem
  rather than fix the entry. `armed_retest_enabled: false` is the A/B.

  Only `trend` and `momentum` arm, decided on the same measurement: `pullback`
  already requires a 25-50% leg retracement before it fires, `range` and
  `vwap_reclaim` enter against the move by design, and `vol_squeeze` showed no
  retrace above baseline at all.

  A `wait` short-circuits BEFORE the build method, and leaves the rest of the
  build queue alone — arming `trend` does not stop `pullback` firing on the
  same symbol in the same cycle.

  **Stop rules are unchanged on a retest entry.** The gain is a fill at a level
  price has already tested and held rather than at a fresh extreme; deriving
  the stop from the retest low would mean bypassing `default_stop_pct` /
  `min_stop_atr_mult`, which are risk floors and a separate decision. The
  retest low is stamped in metadata so that can be settled from data.

  Two things found while building it. `_breakout_reference` was extracted so
  the builder's fresh-breakout check and the armed level come from ONE
  computation — two copies would drift the moment either lookback was retuned
  and the bot would arm on one level and enter against another; the extraction
  is behaviour-neutral and the suite confirms it. And invalidation was
  initially measured against the CURRENT N-bar reference, which walks up as new
  highs print: that moved the invalidation line away from price on the setups
  still working and dragged it behind a rolling-over one. Now anchored to the
  armed level. Caught by the tests, not by review.

  All five knobs are declared in both the shipped preset and the manifest, so
  nothing resolves off a code default. Coverage: `tests/test_armed_retest.py`
  (42).

- **The armed retest is now answerable after the fact.** *2026-09-20* — an
  audit before the first live week found the change shipping blind. Its one
  question is "did the retest path fire, or did everything fall back to a
  market entry", and nothing could answer it: `armed_retest_status` was stamped
  on the Signal, but `EntryGatekeeper.structured_metadata_snapshot` filters
  metadata through an allow-list and `armed_retest_` was not a permitted
  prefix, so all three keys were dropped before `events.jsonl`. `TradeRecord`
  had no field for it either, so `trades.csv` had no column and no aggregate
  could group by it. A week would have produced one blended PnL number that
  reads identically whether good fallback entries carried poor retest entries
  or the reverse.

  Four additions, all observability — no decision logic touched:
  * `armed_retest_` added to the ENTRY_CONTEXT prefix allow-list.
  * `TradeRecord.armed_retest_status` / `.armed_retest_waited_minutes`,
    populated from position metadata at exit. `TRADE_CSV_COLUMNS` derives from
    the dataclass, so both reach `trades.csv` (and the archive copy)
    automatically; the existing schema-drift guard rotates the old file rather
    than writing a malformed one.
  * `per_entry_path` in the report and the EOD log — the same columns as
    `per_regime`, bucketed `retest` / `market_fallback` / `immediate`. The
    fallback IS the pre-change behaviour, so it is the control group and gets
    its own bucket rather than being folded in with non-arming regimes.
  * `entry_timing.by_entry_path`, each path carrying its own baseline. This is
    the mechanism test rather than the outcome one: a retest entry should
    retrace less after the fill than a market fallback, because the retrace
    already happened. Equal numbers mean the feature is not working whatever
    the PnL says.

  Found while writing the smoke test, not by the unit tests: the entry-timing
  log line printed `6670.0%`. `_fmt_pct_opt` formats a FRACTION as a percent
  and these fields are already percentages, so they were multiplied by 100
  twice. Every unit test passed because none asserted on the rendered string,
  which is the only thing an operator reads. There are now two tests over the
  rendered line, one of which simply asserts no percentage in it exceeds 100.

- **The session report measures whether entries are chasing.** *2026-09-20* —
  a new `entry_timing` block in the `SESSION_REPORT` payload and the EOD log:
  for every trade, the deepest retrace in the 15 minutes AFTER the fill,
  expressed as a fraction of that trade's own entry-to-stop distance, split by
  regime.

  That denominator is what makes it readable without a second unit — 0.4 means
  the retrace covered 40% of the way to the stop, so a patient entry down there
  would have been 0.4R better with the same stop, and 1.0 means price reached
  the stop, which is what being stopped out IS.

  The point is the regime split. `trend`, `momentum` and `vol_squeeze` can only
  fill at an N-bar extreme (`close > max(high of the previous N bars)`, 25 bars
  and 6 bars respectively), while `pullback` refuses to fire until price has
  given back 25-50% of the leg (`pullback_require_real_dip`). If the bot is
  entering ahead of the natural pullback rather than on it, those two groups
  separate.

  **The baseline is the whole measurement.** Price dips below any given price
  most of the time, so a raw count of "a better entry existed" measures nothing
  — the mistake the first version of `_gate_attribution` made, which ranked a
  gate with no edge as the costliest one. Each trade is therefore scored
  against arbitrary moments in the SAME symbol's session carrying THAT TRADE'S
  risk distance, which controls for the symbol's volatility and the trade's own
  stop width at once.

  On the archived sessions (old code and the pre-retarget universe, so not a
  verdict on the current bot) the baseline is 0.358R against an observed
  0.666R, and the regimes separate as predicted: `trend` +0.641R over baseline
  across 9 trades, `pullback` +0.213R across 8, and `vol_squeeze` -0.024R
  across 11 — no edge at all, consistent with its 0.25R median MAE, the lowest
  of any regime.

  Same honesty guards as `gate_attribution`: labelled NOT a backtest in the
  payload, the docstring and the README, because it says a better price existed
  and not that the fill was reachable. A trade with a non-finite or
  non-positive `initial_risk_per_unit`, a bad entry price, a missing frame or a
  raising bar lookup counts as unevaluated rather than as zero retrace —
  scoring it zero would read as evidence AGAINST chasing, which is the claim on
  trial. A regime under `min_regime_samples` (5) is kept but marked
  `low_sample` and sorted last; without it a regime with a single archived fill
  topped the table at +7.4R.

  No engine change: `bars_for` was already wired for `post_stop_continuation`.
  Coverage: `tests/test_entry_timing.py` (32).

- **The session archive measures what each gate cost.** *2026-09-20* — a new
  `gate_attribution` block in `manifest.json`: for every SKIPPED decision, the
  net forward price move over 30 minutes toward the side the bot was about to
  take, in ATR, bucketed by skip reason and split by the regime that produced
  it.

  Skip reasons were only ever counts. `no_fresh_breakout` fired 22,231 times
  across 13 sessions — 20.2% of all RTH decisions — and a count says how much
  work a gate did, never whether the work was worth doing. Every gate was added
  in response to a specific loss and none had been measured since.

  Two things make it readable, both learned by running it on real archives
  rather than by design:

  The metric is the NET close-to-close move, not the excursion. The first
  version scored "did price move 1 ATR our way" and ranked the
  highest-volume gate as costliest — but 77% of RANDOM 30-minute windows on
  this universe touch 1 ATR up, because a 1m ATR-14 measures fourteen minutes
  of range and the window is thirty. That gate turned out to have no
  directional edge at all. Net movement has a real baseline; excursion does
  not.

  And the baseline is sampled from the SAME session's bars. A trending day
  lifts every LONG-side number, so only the gap above an arbitrary moment in
  the same session counts as evidence.

  Labelled `NOT a backtest` in the payload, the docstring and the README: price
  movement only, no stop, target, sizing or slippage. Gates below
  `min_samples` (20) stay in `by_reason` but are kept out of the ranking — a
  reason seen once has a "median" of that single observation, and on real data
  those produce the largest numbers in the table purely because nothing
  averages out.

  `_regime_call_outcomes` already asked a version of this question of the same
  files, so its bar loader and forward-excursion maths were extracted into
  shared helpers rather than copied — one place for the tz-aware/naive
  timestamp normalisation to live.

  Coverage: `tests/test_gate_attribution.py`, 22 tests.

- **The EOD session report measures post-stop continuation.** *2026-09-19* —
  a new `post_stop_continuation` aggregate (structured payload + log table)
  answering the one question every other aggregate misses: when a trade was
  stopped out, how far did price then run the trade's way? Every other section
  scores the trade as it was closed, which cannot separate "the stop was
  right" from "we were right and got shaken out".

  Measured in R off `initial_risk_per_unit` so it is comparable across symbols
  and sizes, over a `window_minutes` (default 30) bound after the exit —
  deliberately the same span as `same_level_block_minutes`, so the number
  reads directly as the cost of the re-entry lockout. Reports counts reaching
  1R and 2R, avg/median/max R, and `opportunity_usd`.

  `opportunity_usd` is labelled an UPPER BOUND in both the log line and the
  docstring: it assumes re-entry at the stop price and an exit at the window's
  best tick. It is "the move left on the table", not money the bot would have
  made.

  A trade whose `initial_risk_per_unit` is absent, non-positive or NON-FINITE,
  a missing frame, an infinite bar, or a raising bar lookup all count as
  UNEVALUATED rather than as zero continuation — otherwise a data gap would
  quietly read as "the stop was right" and bias the whole aggregate toward
  vindicating stops. The finiteness checks are not belt-and-braces: NaN
  survives `<= 0` because every comparison against NaN is False, and
  `ensure_ohlcv_frame` drops NaN OHLC but NOT inf — both were found by
  fuzzing this function after it was written, having reached the arithmetic
  and turned `opportunity_usd` into NaN and every R aggregate into inf.

  Bars arrive through a `bars_for(symbol)` callable supplied by the engine, so
  `session_report` stays a pure aggregator over `TradeRecord`s with no
  DataFeed dependency. The engine passes 1m bars regardless of the strategy's
  LTF — the finest resolution gives the truest high/low for the window.

  Validated against the archived sessions (old code, so not a verdict on the
  current bot): of 30 stop exits, 27 evaluable, **16 ran at least 1R the
  trade's way within 30 minutes of being stopped**.

- **The dashboard redirects phones to the mobile layout.** *2026-09-19* — a
  document request from a phone User-Agent gets `302 -> /mobile`; tablets and
  desktops are unchanged, since a tablet has the width for the desktop layout.
  Detection is server-side so there is no flash of desktop content. `?desktop=1`
  forces the desktop layout on a phone and is bookmarkable. The redirect fires
  only on the document fallthrough — `/assets/*`, `/api/*`, `/health`, `/mobile`
  and `/m` all return earlier, so the mobile page still loads its own JS and
  polls `/api/state` from a phone UA — and it is a 302 rather than a 301 because
  the response depends on the device, not the URL.

- **`top_tier_adaptive` can now trade a reversal.** *2026-09-18* — a session
  that flushed and then turned produced zero entries on the recovering side.
  Driving a 3% flush that retraced 87% through the gates bar by bar: of 90 bars
  on the recovery leg, 45 blocked on `index_not_confirmed`, 41 had no regime
  qualify at all, 0 signals — both directions. The stale `day_strength` bias is
  not the cause: `_decide_side` voted the correct side on 75 of 90 bars, and an
  explicit side decision already forces `bias_penalty` to 0.0. Two changes,
  which only work as a pair (gate alone 0 signals, builder alone 2, both 18
  LONG / 15 SHORT).
  - **Leg-anchored confirmation** (`leg_anchored_confirmation`, default
    `false`, `true` in the preset). `_frame_agrees` measured `close > vwap`
    against SESSION VWAP — a whole-day average that after a flush sits far
    above price, so the confirmation only turned true long after the reversal
    was running. Applied to every PEER, the breadth gate inherited the lag: on
    the test tape session VWAP was reclaimed 44 minutes after the low with half
    the move gone, a VWAP anchored at the low after 1 minute. New
    `_leg_anchor_vwap` anchors at today's more recent extreme, guarded by
    `leg_anchor_min_age_bars` (20) and `leg_anchor_min_impulse_pct` (0.005) so
    an ordinary pullback is not read as a reversal, and falls back to session
    VWAP when no leg is established. Measured deltas in confirmed bars: pure
    trend 0, grind-with-pullbacks 0, chop +1, V-reversal LONG +22, inverted-V
    SHORT +22. Default `false` because `SmallCapSqueezeStrategy` subclasses
    this strategy and inherits the method.
  - **`vwap_reclaim` enabled in the preset.** Fixing the gate alone still
    produced 0 signals — the blocker moved to `no_fresh_breakout` on 38 of 90
    bars. Every other surviving regime triggers on a BREAKOUT (trend/momentum
    need a fresh N-bar high, pullback an established aligned trend); a reversal
    is a reclaim, so no builder recognised the shape. Knobs adapted from
    `config.small_cap_squeeze.yaml` where the regime is already tuned;
    `vwap_reclaim_buffer_pct` rescaled 0.0025 -> 0.0015 because it runs through
    `_pct_param` and small caps carry a far larger ADR. Entries land at the
    VWAP reclaim — around the midpoint of the move, not at the low.

### Changed

- **Config / manifest / README drift pass.** *2026-09-22* — every param the
  code reads checked against its preset and manifest, every config field
  against `config.example.yaml` and the README, and every README defaults
  table against the manifests. No effective value changes: each edited preset
  loads to the same config before and after.
  - **Undeclared params.** `config.small_cap_squeeze.yaml` now declares the 89
    top_tier-engine params it reads, at the values it was already running --
    it inherits the engine, but only its own knobs had ever been declared, so
    any code-default edit silently retuned it. The two `peer_confirmed_*`
    subclass presets declare the 19 inherited `peer_confirmed_key_levels`
    params each; `closing_reversal` / `mean_reversion` declare
    `screener_min_pullback_from_high`. `test_param_declaration_drift.py` now
    covers small_cap_squeeze as well as top_tier.
  - **Dead `runner_target_rr` removed** from the `peer_confirmed_htf_pivots`
    and `peer_confirmed_trend_continuation` manifests, presets, the example
    config and their README tables. Nothing read it: the runner target comes
    from `_adaptive_management_components` and `adaptive_runner_target_rr`.
    `volatility_squeeze_breakout` does read its own and keeps it.
  - **`config.example.yaml`** carries the 28 config fields it was missing
    (slippage/overage risk, daily-loss open risk, bracket orders, escalation,
    IV rank, ...). Three keys sat under the next section's header comment,
    and the peak-giveback comment still quoted the pre-2026-05-27 tiers.
  - **README.** 20 undocumented fields documented, plus a new `events`
    section: the options table still listed `event_blackout_file` /
    `event_blackouts`, keys removed on 2026-09-18. Package-default rows
    corrected where they had drifted from the manifest (vsb `min_rvol` /
    `target_rr` / `runner_target_rr`, closing_reversal `min_rvol`, the
    peer_confirmed `force_flatten` rows, top_tier `min_sr_scalp_score`), and
    the top_tier universe prose, `index_symbols` and `sector_index_map`
    brought up to the 2026-09-18 Tech/AI retarget -- they still described 23
    names across six GICS sectors. top_tier README sections renumbered (two
    were numbered 19).
  - `no_new_entries_after: 13:45` quoted in
    `config.zero_dte_etf_long_options.yaml`. Unquoted, YAML 1.1 reads it as
    the sexagesimal integer 825; `parse_hhmm` converts that back, but the
    value should not depend on it.

- **`vol_squeeze` now runs at midday.** *2026-09-21* — its thesis is
  compression resolving into expansion, and the lunchtime tape IS the
  compression, so it had been excluded from the one window where its setup is
  most common. The midday carve-out predates the regime's reinstatement and was
  written about the regimes that existed then.

  The measurement that prompted it, from the live session: across the day's
  five biggest movers (INTC, META, AMD, QCOM, NFLX — all +3% to +6%), **51% of
  midday skips were "no regime qualified"**, and of the three regimes then
  offered, not one came within half a point of its floor — pullback peaked at
  3.00 against a 3.5 floor, momentum at 3.00 against 4.0, vwap_reclaim at 0.00.
  Ninety minutes a day in which effectively nothing could fire. The morning
  profile is completely different: only 4% of blocks were score-related, the
  rest being downstream gates.

  `trend` and `range` stay out of midday — this is not a reversal of the
  carve-out, only of the part that excluded the regime whose setup midday
  produces.

- **README drift: both READMEs said "six regimes".** *2026-09-20* — there have
  been eight since `vwap_reclaim` and `orb` were added; the strategy README's
  regime-defaults table was also missing `disable_vwap_reclaim_regime` and
  `disable_orb_regime`. Corrected, and the table now carries the armed-retest
  knobs too.

- **`vol_squeeze` re-enabled in the `top_tier_adaptive` preset.** *2026-09-20* —
  the 2026-09-18 regime trim turned it off on the grounds that it "had the
  highest score ceiling (6.5) so it won more auctions than its edge justified".
  That was true of RAW score sorting, and `_normalized_regime_score` +
  `REGIME_SCORE_CEILINGS` shipped in the SAME change — the two are consecutive
  bullets in this file's 2026-09-18 Added section. Ranking is now
  `(score - floor) / (ceiling - floor)`, under which a 6.5 ceiling over a 4.0
  floor is the WIDEST headroom of any enabled regime (trend/momentum 2.0,
  pullback/range/vwap_reclaim 1.5), so the property cited as the reason to
  disable it now holds it back rather than flattering it: a vol_squeeze at 5.5
  ranks level with a pullback at 4.4.

  The second reason is a hand-off that had nowhere to go. `bollinger_squeeze`
  is read in exactly two places in the strategy — this regime's scorer
  (`_score_vol_squeeze`) and `range`'s `reject_range_during_squeeze` rejection
  — and they partition the same condition: `range` declines squeeze setups
  *because* vol_squeeze is meant to take them. With vol_squeeze off, `range`
  kept declining and nothing picked them up. The new `gate_attribution` block
  puts a number on it: on the 2026-09-18 archive
  `long_build_failed_range_bollinger_squeeze` blocked 149 decisions at +0.853
  ATR above the same-session baseline.

  No code change and no conflict with the other regimes: the build queue is
  flat and cross-side (a regime cannot block another, within or across sides),
  and `vol_squeeze` is not referenced in `strategy_base.py`, `engine.py` or
  `risk.py` at all. Three interactions are real but pre-existing and shared by
  every regime — one more competitor for `risk.max_positions` (4) slots, one
  more producer of `same_level_block_minutes` re-entry lockouts (which are
  symbol + side + level scoped, regime-agnostic), and no runner treatment on
  exit (that path is trend/pullback only). The regime is exempt from
  `reject_oversized_entry_bar`, as it has been since 2026-05-14, because a big
  bar IS the setup.

  This is a measurement, not a verdict. The trim's small/mid-cap objection
  still stands and is untested on this universe: with the flag off the regime
  is never scored and is omitted from the `no_qualifying_regime` skip line, so
  there is no record of how often it would have qualified on mega caps. All 12
  `vol_squeeze_*` knobs are already declared explicitly in the preset, so
  nothing resolves off a code default.

- **`enable_vwap_reclaim_regime` -> `disable_vwap_reclaim_regime`.**
  *2026-09-20* — all eight regime knobs now read the same way. The flag shipped
  2026-05-30 as an opt-IN (default off) so that adding the regime to the shared
  engine could not silently switch it on for `SmallCapSqueezeStrategy`, which
  subclasses `TopTierAdaptiveStrategy`. That protection is spent:
  small_cap_squeeze sets the flag explicitly in its own manifest, so nothing
  depended on the inverted default, while the odd polarity left one knob
  reading backwards from the other seven.

  Behaviour is unchanged — both shipped presets ran the regime before and run
  it after, verified by resolving the loaded config for each. One reader
  (`strategy.py`), two yaml presets, one manifest; renamed and inverted in one
  cut with no alias.

  Two absent declarations surfaced while doing it, both cases of a CODE default
  doing load-bearing work: `top_tier_adaptive/manifest.json` declared neither
  `disable_vwap_reclaim_regime` nor `disable_orb_regime`, so a config-less run
  resolved both off `params.get(..., False)` rather than anything declared.
  Both are now declared at their existing effective values, so the defaults are
  inert. Note the baseline that exposes: the manifest says ORB **on** while
  every shipped preset turns it off — worth a separate decision.

  Coverage: `tests/test_regime_flag_polarity.py`, 55 tests. Pins the property
  rather than the instance — a ninth regime added with an `enable_*` knob fails
  across every manifest, every shipped config and the strategy's own param
  reads, instead of being discovered by someone reading a config and getting
  the sense of it backwards.

### Fixed

- **Whole-project sweep: order handling, options, data, reports and
  screeners.** *2026-09-22* — every `.py` outside the eight level/pattern
  modules reviewed earlier today. Each fix fails its tests when reverted
  (60-mutation matrix, all caught); new coverage in
  `tests/test_sweep_fixes.py` (87 tests) plus the rewritten resample /
  completion tests in `test_properties.py` and `test_bug_regressions.py`.

  *Live orders*
  - **Sub-penny limits.** Entry/exit limits were the touch plus a spread-scaled
    buffer rounded to 4dp, and the reprice loop multiplies that buffer by
    1.33 / 1.66, so every live reprice on a stock at/above $1 was sub-penny
    (150.1599) and rejected under Rule 612 -- dry-run has no tick check.
    Limits now round to the tick away from the touch (a buy up, a sell down).
  - **A second exit for the same shares.** A MARKET exit that did not fill in
    its poll window (a halt, an LULD pause) was left working and nothing
    tracked it; the next cycle sent another, and both filled on the resume.
    `OrderResult.may_still_be_working` now marks every order that reached the
    broker without a confirmed terminal state, and the position manager
    tracks it (`working_exit_order`), books its fills from the order's own
    record each cycle, re-cancels a live limit, and sends nothing else for
    the position until it is terminal.
  - **A partial fill read as "cancelled".** The post-cancel check returned
    success for any order with fills, so a partially filled order whose
    remainder was still live was treated as done. It now waits for a
    terminal status.
  - **Recovery read a stale positions snapshot.** Order-uncertainty recovery
    read broker positions through a snapshot cached once per cycle; every
    recovery after the cycle's first read positions from before its own
    order (a filled exit looked unfilled, a filled entry looked absent), and
    a failed read was taken as "no position" and booked a full exit. Entries
    and exits are now settled from the order's own fills, the snapshot
    (`broker_position_row(s)`, `begin_cycle`/`end_cycle`) is gone, and a
    filled entry whose fill no longer fits the signal's levels is tracked
    with fallback levels instead of left untracked.
  - **A filled exit with no price was re-sent.** With neither a broker fill
    price nor a last price the booking was skipped "to retry next cycle" --
    for shares already sold. It is booked at entry, flagged estimated.
  - **Vertical fill prices averaged the legs.** A 2.00/1.00 debit spread read
    back as 1.50; the net is now the signed sum of the leg prices. Its
    filled QUANTITY had the same flaw where an order carries no
    `filledQuantity`: the executions arrive once per leg, so summing them
    read a 2-lot vertical as 4 filled. It is now the least-filled leg per
    unit of order quantity.
  - **Messages that needed a recheck and did not get one:**
    `partial_fill_*` and `bracket_*` failures now count as having reached
    the broker.

  *Broker-side brackets (off by default)*
  - **Replace kept the old id.** Schwab's replace cancels the child and
    creates a new order; the bracket kept the dead id, so later replaces
    were rejected (the broker stop froze while the engine deferred to it)
    and the replacement's fill was never booked. The bracket now follows the
    new id.
  - **Cancel before an engine exit ignored child fills**, and cancelled only
    the OCO wrapper (a replaced child may not be under it). It now takes
    down every tracked child and reports what filled first; the engine books
    that and exits only the rest. After a child fills, anything left of the
    bracket is cancelled.
  - **Adoption never resized** (a dead key comparison), recorded the engine's
    levels instead of the broker's, and on a restart adopted off the parent's
    original children -- REPLACED after any move -- then stacked fresh
    protection on the live replacement. It now compares the resting quantity,
    records what the broker holds, and adopts the ids the bot last tracked
    (or, for restore_basic, the stop found resting at startup).
  - **Own stops blocked all entries after a restart.** A restored position's
    protective orders counted as foreign working orders
    (`working_orders_present` for the whole session).

  *Session state*
  - **Positions closed overnight stayed tracked.** The new-day reconcile only
    added positions; one closed in the Schwab app kept a slot, fed the
    correlation guard and open risk, and sent rejected exits every cycle. It
    is now booked as `closed_outside_bot` at the last mark (estimated,
    broker-recovered), dropped or cut to what the broker holds, and its
    resting bracket cancelled. Skipped in dry-run, where positions are
    simulated and the real account holding none of them is not a close --
    the first cut wiped every paper position held into a new session.
  - **1m frames grew without bound** for always-active symbols; frames now
    keep the latest session day whole plus the depth of the deepest history
    fetch (the deepest, so one short response cannot trim good history).
  - **Dec 31 was a holiday** when New Year's Day falls on a Saturday (NYSE
    stays open; next 2027-12-31).

  *Options*
  - **Exits compared the underlying with option premium.** R, the time stop,
    the S/R-break guards and the anchored-VWAP arming read the underlying's
    frame against premium-space entry/extremes: a debit position read as
    about +7R (discretionary exits always armed), a credit spread about -5R
    (never), and the time stop could never fire. R now uses the option's
    mark; the rest use `underlying_entry` and the underlying's range since
    entry, both tracked by the position manager. Option entries also record
    their opening stop, so R no longer re-bases after a ratchet.
  - **`natural` / `bid` vertical pricing ignored direction** -- `natural`
    took the ask for a credit open and a debit close (the far side). Both
    modes now follow whether the order buys or sells the spread.

  *Data and reports*
  - **Resampled bars were one source bar late, and end-labelled.**
    `resample_bars` ran `closed="right", label="right"` on start-labelled
    bars: the 5m bar labelled 09:35 held 09:31-09:35 and the 09:30 bar mixed
    four premarket minutes into the open. And since everything downstream
    reads a timestamp as a bar START (the RTH mask behind session VWAP/EMA,
    session-open helpers, the ORB follow-through gate), an end-labelled
    premarket bar counted as RTH. Bars are now `[T, T + rule)` labelled `T`,
    the broker's own convention, anchored on the 09:30 open (clock bars for
    5/15/30m, session hours for 60m); completion is `T + tf <= now` for
    every frame, and the `time_label` / `source_bar_minutes` attrs are gone.
    This moves top_tier's 5m LTF structure bars by one minute.
  - **Partial exits vanished from every report.** The EOD report,
    `trades.csv`, the manifest and the account snapshot kept only final
    exit slices (100 shares out 40 + 60 at +$1 reported $60). Slices now
    fold into one row per trade.
  - **Fractional score thresholds were rounded up** (`min_ltf_score` 2.5 -> 3,
    `min_total_score` 5.5 -> 6) in the peer_confirmed presets.

  *Screeners*
  - **ARM and TSM never reached top_tier** (0 of 526 candidate cycles on
    2026-09-21): the curated list ran through the open-screen filters --
    ADRs are TradingView type `dr`, and TSM is not a primary listing. The
    same filters ran on the peer_confirmed lists. Curated lists now filter
    only to their names, off OTC; top_tier logs any configured name the
    screen does not return.
  - **The library's default universe dropped every ETF.** Its `filter2`
    survives `where()`, so pairs_residual's QQQ reference never came back
    (and it is the "0 rows for ETFs" the 0DTE screener routed around on
    2026-05-19). Each screen now states its own instrument conditions.
  - **The description filters were no-ops.** `not_like("%ETF%")` and nine
    siblings matched nothing -- TradingView takes the `%` literally (checked
    live) -- so 56 preferred shares passed the open screens; without the `%`
    "Unit" would drop UnitedHealth, United Airlines, UPS and URI. Replaced by
    `typespecs has_none_of ["preferred"]`.

  *Strategies*
  - **top_tier: an expired armed retest died on a side vote gone
    undecided.** The expiry fallback is exempt from the per-cycle gates
    (they were all satisfied at arm time -- the INTC 2026-09-21 fix), but
    the `side_undecided` gate lacked the exemption, so a vote flickering to
    undecided while the arm waited dropped the entry silently.
  - **microcap_pm_breakout: the blowoff guard measured R off the trailing
    stop.** Once the stop reached breakeven, R divided by ~1e-8 and
    `blowoff_min_rr` cleared on any wide bar at +0.1R. It now uses the
    opening stop, like every other R in the exit path.

  *Levels*
  - **A swing whose extreme printed twice had no pivot.** The shared
    `pivot_points` disqualified every bar of a window holding a tie, so a
    flat top or a two-bar bottom registered nothing -- 4.4% of swing
    highs/lows on archived 5m RTH bars (180 of ~4,130 over 138
    symbol-days), exact penny ties that are routine at round numbers, and
    more on 1m bars (the SPY 1m snapshot gains 50 structure pivots, 282 ->
    332). A tied V-bottom lost its low entirely and market structure read an
    older pivot as the reference low -- surfaced when the corrected 5m
    buckets put a tied top_tier reversal bottom in two bars. A tied extreme
    is now one pivot, at its first bar. S/R, HTF and technical-level
    snapshots regenerated (touch counts up, level prices move by cents).
  - **top_tier: the trailing-bias memory counted loop cycles, not bars.**
    One observation was appended per `entry_signals` call, so
    `trailing_bias_lookback: 10` spanned 20-30 seconds at a 2s loop
    (varying with cycle time), and the memory written for the GOOG
    2026-04-23 case -- a LONG into ten SHORT-biased bars -- had faded long
    before it mattered. It now keeps one observation per LTF bar (a later
    cycle on the same bar updates it) and starts empty each session. The
    penalty it feeds scales with the day's move, so inside the neutral band
    it stays small.

- **`bollinger_squeeze_width_pct` was a constant in every preset but
  top_tier.** *2026-09-22* — the same units error top_tier's value was
  corrected for earlier today: `bollinger_width_pct` is `(bb_upper - bb_lower)
  / bb_mid`, a FRACTION of price, and `0.06` sits above nearly every bar the
  bot computes it on, so `bollinger_squeeze` was simply on. Each preset now
  carries the p25 of RTH BB(20,2) widths on the timeframe that strategy
  actually builds its technical context on, over its own universe's archived
  bars:

      universe / timeframe      value    presets                                   0.06 flagged -> now
      large caps, 1m            0.0025   top_tier + 7 other 1m large-cap presets,   99.9% -> 25.0%
                                         peer_confirmed_key_levels_1m
      large caps, 5m LTF        0.0061   peer_confirmed_htf_pivots /                98.4% -> 25.0%
                                         _trend_continuation / _key_levels
      SPY / QQQ, 1m             0.0014   both zero_dte presets                     100.0% -> 26.9%
      small caps, 1m            0.076    small_cap_squeeze, both microcap presets   16.4% -> 25.1%

  Large-cap values use the pooled definition top_tier's `0.0025` was set with,
  which includes the sector ETFs and indices in the archive; on candidate
  stocks alone `0.0025` flags 19.6% of bars and their own p25 is `0.0028`.
  `peer_confirmed_key_levels` builds its trading context on 1m but only reads
  Bollinger REJECT flags there; the dashboard shows its squeeze flag on the 5m
  LTF, so it is calibrated on 5m. Six presets have no trading path that reads
  the flag (microcap_gap_orb, both zero_dte, peer_confirmed_key_levels, `_1m`,
  and peer_confirmed_htf_pivots, whose only use is the target cap it
  disables); they are set for consistency and say so.

  **Small caps were not broken.** Their 1m bands are wide (median width 0.117
  of price), so `0.06` flagged 16.4% of bars, a working threshold. They move
  to their p25, `0.076`, so the flag means the tightest quarter everywhere, on
  thin evidence: 1,646 bars from 9 symbol-days of dry-run momentum names,
  per-symbol-day p25 0.043-0.135.

  **`volatility_squeeze_breakout` barely moves.** Expected to lose candidates
  sharply once the flag stopped being constant; it does not. The flag is one
  of three alternatives inside its `no_valid_squeeze` gate, and replaying the
  real `entry_signals` over the same 17 sessions (29,916 evaluations) takes
  that gate from 18,367 rejections to 18,368 and
  `bollinger_squeeze_not_confirmed` from 22 to 97. It produced zero signals at
  either value: `weak_day_strength` alone rejects ~99%. Its other gates were
  deliberately not touched.

  Where the flag is live it now also stops suppressing two shared terms most
  of the time: the Bollinger target cap (only closing_reversal and
  mean_reversion enable `target_use_bollinger`) and the weak-ADX entry penalty
  in `_technical_entry_adjustment`, a priority/score input.
  `configs/config.yaml` was left at `0.06` on purpose (runtime config), as was
  the code default, which only fits small caps.

- **Ten defects in the level and pattern modules.** *2026-09-22* — from a
  review of sr_ladder, chart_patterns, candles, htf_levels, levels_shared,
  order_blocks, support_resistance and technical_levels. Each was reproduced
  before it was fixed; the numbers below are from replaying the archived
  2026-09-21 session (28 symbols, real 1m bars) before and after.

  **`anchored_vwap_open` turned into a rolling VWAP by late morning.** It was
  computed after `build_technical_levels_context` had cut the frame to its
  longest lookback (~120 bars), so once 09:30 left that window the session
  anchor silently became the window's first bar. Against the archived
  `vwap_rth` it was 0.00 ATR off at 11:15, 3.4 ATR (median) at 12:00 and 6.1
  at 13:30, with a 20.9 ATR worst case. It drives `anchored_vwap_loss_exit`.
  It is now computed on the untrimmed frame: 0.000 ATR at every checkpoint
  from 10:30 to 15:00.

  **`atr_expansion_mult` measured displacement, not volatility.** It was
  `|close - close[-6]| / atr14`, so a clean trend read as an ATR expansion and
  a violent chop with no net move read as none. Every consumer documents it as
  volatility against the ATR the stops were sized on, and it is now exactly
  that: the mean true range of the last `atr_expansion_lookback` bars over the
  `atr14` just before them. The literal reading of the docs, `atr14` over its
  own 5-bar mean, was measured and rejected: ATR14 is smoothed so heavily that
  the ratio sits at 1.00 ± 0.06. How often each consumer's threshold is met
  (9,498 RTH bars; the second pair is the 453 bars that close out of a <= 2.8
  ATR 12-bar box):

      threshold  consumer                                all bars      squeeze breakouts
                                                         old    new      old    new
      >= 0.80    shared entry-score ATR bonus            54%    70%      90%    73%
      >= 1.00    volatility_squeeze_breakout gate        46%    37%      83%    37%
      >= 1.12    volatility_squeeze_breakout runner      40%    23%      78%    20%
      >= 1.20    top_tier Tier 2a stop widening          37%    16%      75%    13%
      >= 1.25    volatility_squeeze_breakout quality     35%    13%      72%    10%

  Tier 2a's full widening (1.8x in top_tier, reached at twice the threshold)
  now applies on 0.6% of bars, down from 8.8%. **`volatility_squeeze_breakout`
  was NOT retuned.** Its `min_atr_expansion_mult: 1.0` gate was, in effect,
  tuned against displacement, which is naturally large on a breakout, and it
  now passes 37% of squeeze breakouts instead of 83%. That preset is not in
  the live rotation; retune it against outcomes before running it again.

  **A right-labelled HTF bar was treated as complete while still forming.**
  `resample_bars` labels with `closed="right"`, so the bar labelled T includes
  the source bar STARTING at T, and it is complete at T + one source bar, not
  at T. `_completed_htf_frame` admitted it at T. At 11:10, the "completed" 60m
  frame built from 30m bars carried an 11:00 bar whose 11:00-11:30 half was
  still forming, and HTF data is fetched once per bar, so that partial bar was
  used for the rest of the hour. `resample_bars` now records
  `source_bar_minutes` in `attrs`, the HTF cache carries it through its
  merges, and completion waits for the last source bar. A frame without it
  waits a whole HTF bar: late, never early.

  **One structure-event window was sizing two timeframes.**
  `structure_event_lookback_bars` judged both LTF structure events and the S/R
  context's HTF ones. top_tier halved it 8 -> 4 for 5m structure bars on
  2026-05-27, and that silently cut the 15m HTF window from 120 minutes to 60.
  The new `support_resistance.htf_structure_event_lookback_bars` (`null` =
  same as the LTF knob) sizes the HTF window. top_tier_adaptive and
  small_cap_squeeze pin it at 8, and every other preset resolves as before.
  The builders, the dashboard's HTF overlay and every age check that reads the
  S/R context's `market_structure` now use it. That means
  peer_confirmed_key_levels' ladder exits and zero_dte_etf_options' HTF
  structure score (6/6 there, so its value does not change). A test scans the
  whole package for any age check on that structure without `htf=True`. The
  first version of this fix missed zero_dte's four checks, and the scan is
  what found them.

  **Divergence paired the newest swing with the OLDEST qualifying one.** Lows
  at 100 (RSI 20), 96 (RSI 35) and 95 (RSI 30) were reported as a bullish
  divergence 100 -> 95. That stepped over the 96 swing, where RSI had in fact
  CONFIRMED the new low (30 < 35). `find_divergence` now takes the latest
  pivot and the nearest earlier one that makes the pattern's price move. Noise
  pivots inside `price_move_frac` are skipped. An intervening pivot on the
  wrong side of the latest one means there is no divergence to report: for
  regular divergence the latest pivot is then not the extreme, and for hidden
  divergence it broke the swing it claims to hold above (or below). The first
  cut applied that rule to regular divergence only, so lows 95 -> 103 -> 101
  still read as hidden bullish 95 -> 101 across the 103 swing; a bug check
  over the day's changes caught it before commit. One unit test had asserted
  the old pairing (lows 100 -> 99 -> 99.5
  reported as divergence 100 -> 99.5). It now asserts none. The span-scale
  test moved from seed 39 to 309, because seed 39's "divergence" was exactly
  that bogus pairing.

  **`CDLGRAVESTONEDOJI` could never match.** It sits in the bearish list, and
  TA-Lib emits it as +100 (9,181 times across 400 real frames, never
  negative), while bearish matching required a negative value. Fixed-direction
  patterns now match on any non-zero value, and sign-dependent ones still read
  the sign. Every other fixed pattern was checked and is already signed to
  match its list. The gravestone now appears on 1.15% of RTH bars.

  **Chart-pattern thresholds were sized for small caps.** Each
  percent-of-price constant was paired with an ATR-relative one, and the pairs
  coincide at a ~0.67% mean 1m bar range. On liquid mega caps (~0.1%) the
  percent floors were ~7x too wide. The module fired 7 times in 1,850
  evaluations, and 16 of 18 patterns never fired. Those constants now scale by
  the frame's range over that reference, clamped to [0.1, 1.0], so a name at
  or above it keeps exactly the thresholds it had: 97 fires across 10
  patterns, and none above 1.35% of evaluations. The six strategies that gate
  entries on an opposing pattern (closing_reversal, mean_reversion,
  momentum_close, opening_range_breakout, rth_trend_pullback,
  volatility_squeeze_breakout) will now block on patterns they almost never
  saw; top_tier does not use that filter. The flag's slope allowance also had
  an absolute $0.04/bar floor, which let a steadily rising consolidation pass
  as a flag on a $5 name and nothing on a $500 one. It is now 0.1 bar ranges,
  which gives the same verdict on the same shape at any price.

  **Order blocks were born filled.** Fill was measured from the closes after
  the OB candle, which included the bars between it and the breakout, and
  those are the zone forming, not price returning to it. One block was "64%
  filled" at birth. Fill is now measured from the closes after price first
  closes beyond the zone following the OB candle, or after the breakout if it
  has not yet. The first cut measured from whichever breakout found the
  candle, but the same candle is found again from every new extreme within 5
  bars: a breakout, a gap-down close below the zone, then a second breakout
  returned the zone fresh at 0% filled, a block the pre-review code had
  correctly dropped. The same bug check caught it before commit.

  **A trendline price had cut through still counted.** Touches were counted
  and nothing else was checked, so a "support" carried a pivot low 11 points
  below it mid-span. A pivot beyond the line by more than the tolerance
  between its first point and its last touch now disqualifies it. A break
  after the last touch does not; `trendline_break_*` reports that.

  Also: `technical_levels._pivot_points` had become a copy of the shared
  `pivot_points`, and it is now a thin wrapper around it. The HTF FVG detector
  replaced a NaN high/low with 0.0, which would have made a bullish gap
  reaching from zero up to price. The NaN now fails its comparison.

  **Net effect on top_tier, 2026-09-21.** The real `entry_signals` was driven
  over every 5-minute cycle twice, once with every fix above reverted and once
  as shipped. A code fingerprint in each arm confirmed which code had loaded.
  Both arms produced the same 9 entries (same symbols, times, sides and
  regimes) and the same 4,031 skips. 6 of the 9 stops moved, all through Tier
  2a and in both directions: GOOG's `vwap_reclaim` long went 348.73 -> 345.41,
  and AMZN's short 259.91 -> 259.75. The HTF fixes cannot show in this replay,
  because it runs without a data feed. The AVWAP fix acts on exits
  (`anchored_vwap_loss_exit`), not entries.

  Coverage: 1,851 passing (+40). `test_levels_review_fixes.py` holds one class
  per defect, plus divergence cases in `test_levels_shared_divergence.py` and
  O3-O5 in `test_properties.py`. Each fix was reverted in turn, and 15 of 15
  reverts are caught; the two follow-ups were reverted both to their first
  cut and, for order blocks, to the pre-review window, and all are caught. O5
  initially wasn't: `pd.concat` keeps `attrs` only when
  all inputs match, so it now merges a fetch without a source size into a
  cached frame that has one. The three technical-levels snapshots were
  regenerated. Only `anchored_vwap_open` and `atr_expansion_mult` changed, and
  both were checked against an independent computation first.

- **`range` had lost its only counter-trend filter.** *2026-09-22* — found
  on a bug pass over this week's mean-reversion work, and caused by it:
  nothing here was new code, but making `range` able to qualify turned a
  latent interaction into a live one.

  `range` is exempt from the `_decide_side` vote because a fade has to enter
  against the move (2026-09-18). Its counter-trend protection is therefore
  the soft `_bias_penalty` — which is skipped whenever the vote picks a side,
  on the grounds that "the explicit decision already chose the side"
  (2026-05-27). That reasoning is right for SIDE_DECISION_REGIMES, which the
  build queue holds to the voted side. It is wrong for MEAN_REVERSION_REGIMES,
  which do not follow the vote: scored on the side AGAINST it, `range` got no
  penalty precisely because the vote had decided something `range` ignores.
  The two changes were written four months apart and compose badly; with the
  regime unable to qualify, nobody could see it.

  Driving the real `entry_signals` over 2026-09-18, 09-21 and 05-28, 6 of 11
  `range` signals faded their own symbol's day by >= 0.30%, three by >= 1% —
  QCOM LONG on a -6.85% day, META LONG on -2.61%, GOOG LONG on -1.59%. When
  the vote decides, the penalty now still applies to MEAN_REVERSION_REGIMES
  (scaled by the day's move exactly as `_bias_penalty` documents; zero in the
  neutral band and for a fade WITH the day), and remains skipped for the
  vote-bound regimes. Same sessions after: 5 signals, 0 fading their own day
  by >= 0.30% — the remaining five are neutral-day fades or trend-aligned
  ones (buying a dip on an up day). It is logged as `mr_bias_pen`, separately
  from `bias_pen`, so a skip line does not read as though trend was docked.

  Two things this does not do, stated so they are not assumed. The penalty
  caps at 1.0 and `range`'s headroom is exactly 1.0 (5.0 - 4.0), so a PERFECT
  5.0 fade still clears at the floor on a deep down day — the penalty's own
  docstring says it filters "most" counter-bias setups, not all; none of the
  three sessions produced one. And it keys on the symbol's own move, not the
  sector's: two of the five survivors were flat names on a -0.9% sector day,
  which reads as relative strength rather than a knife.

  Also on this pass: one new test took 14s a side because its tape builder
  reloaded the preset ~420 times (now cached, ~2s); the preset's headroom
  comment still gave `range` 1.5 after its floor moved to 4.0; and the
  README's Range bullet predated both the scorer's squeeze gate and the new
  floor. The 81 "no regime qualified" lines that printed `range` at or above
  4.0 were NOT a scoring mismatch: SHORTs clear `min_range_score` plus
  `short_min_score_premium` (0.5), so a short `range` needs 4.5 and the regime
  now leans long by design.

  Coverage: 1,811 passing (+5). The tests drive `entry_signals` itself, since
  the defect was in how it composed the penalty with the vote rather than in
  `_bias_penalty`, which was already unit-tested and correct. Each revert is
  caught by the test aimed at it: removing the fix fails the deep-down-day and
  skip-line tests, and over-applying it to every regime fails the one that
  pins trend's exemption.

- **vol_squeeze's compression gate was calibrated in the wrong units, and a
  bug pass found a fourth score/build mismatch.** *2026-09-22*

  **`vol_squeeze_max_range_atr` compares a TWELVE-bar box against a ONE-bar
  `atr14`** — the same units mismatch as `orb_max_range_atr_mult`, and worse.
  Measured over 167,447 RTH 1m bars across 18 archived sessions
  (2026-05-01 .. 09-22) the ratio runs:

      p5 2.17   p25 2.81   median 3.44   p75 4.26   p95 5.75   max 15.71

  The shipped `1.8` sits BELOW the 5th percentile, so 0.79% of bars could ever
  be "compressed" and the regime reached its full gate stack 3.4 times a
  session across 28 symbols. A random walk puts the expected 12-bar range near
  3.3x a 1-bar ATR, so the measured median is the NEUTRAL box width, not a
  tight one. Now `2.8`, the p25 — the same definition
  `bollinger_squeeze_width_pct` uses at its own p25, so the regime's two
  compression tests finally mean the same thing. Replaying 2026-09-21 takes
  the regime from 3 qualifying candidates to 44, none of which the builder
  would reject for a weak breakout. The breakout buffer is deliberately NOT
  touched in the same change: this regime's archive is 11 trades for +$9.58
  that becomes -$120 without one TSLA winner, so it gets one variable at a
  time.

  `vol_squeeze_max_range_pct: 0.012` admits 95.59% of bars and never binds. It
  is kept as the absolute backstop for a name whose `atr14` is unusually small
  relative to price, and is now documented as such rather than reading like a
  live gate.

  **`_score_range` still ignored one of its builder's gates.**
  `_build_range_signal` refuses outright when the tape is in a Bollinger
  squeeze (`reject_range_during_squeeze`) and the scorer never looked, so a
  squeezed bar could score the full 5.0, win the auction and die on
  `range_bollinger_squeeze` — 25 of those on 2026-09-21 even after the
  threshold recalibration, and 387 of 387 before it. Fourth instance of this
  shape in a week, after ORB's bare-break +2.5, the range regime's optional
  prev-bar confirmation and vol_squeeze's three breakout bonuses. `_score_range`
  now takes `tech_ctx` (required, not optional — an optional one is a caller
  that can silently forget it) and mirrors the gate, flag and all.

  **`min_range_score` re-derived, 3.5 -> 4.0.** A score floor only means
  something relative to the scorer it gates, and the scorer was replaced. At
  3.5 the minimum qualifying setup was zone + prev-bar + rejection wick and
  nothing else: a fade off the range edge with NEITHER piece of evidence that
  the tape is ranging rather than trending, which is the falling knife the two
  +0.5 context components exist to tell apart. 19 of the 28 range setups on
  2026-09-21 and 9 of 26 on 09-18 scored exactly that. 4.0 requires 1.5 beyond
  the zone — the wick plus any one +0.5, or all three together. 4.5 measures
  back to 0 setups on 09-21 and 2 on 09-18, i.e. the dead-regime state this
  week's work exists to fix, which is the `min_pullback_score: 4.0` and
  `min_sr_scalp_score: 4.0` mistake in the other direction.

  **Left alone, and worth stating plainly: `range` now takes most of the
  book.** Driving the real `entry_signals` over full archived sessions with
  all 28 frames, it is 62.5% of the signals on 2026-09-21 (down from 85%
  before the squeeze gate and the floor) and 97.0% on 09-18. That is NOT the
  auction normalisation — it is that `range` is the only regime exempt from
  BOTH `require_entry_confirmation_bar` and `require_index_confirmation`, and
  those two account for ~700 of the directional regimes' rejections across the
  two sessions. The exemption is deliberate and documented; it simply never
  mattered while the regime could not qualify. Narrowing it, or capping
  concurrent mean-reversion positions, is a strategy decision and not
  something a bug pass should make on its own.

  Coverage: 1,806 passing (+9). The new assertions pin the calibration the way
  the squeeze-threshold ones do — the ATR cap must sit above the 5th
  percentile of the ratio it gates and below its median, or it is either
  always-false or not selective; the pct gate must stay above the tape's own
  p95 so its backstop role is a conscious choice; and the range floor must be
  both unreachable-by-a-bare-fade and reachable by a real one.

- **`vol_squeeze` could qualify a setup its own builder was certain to
  reject.** *2026-09-22* — the preset records the 2026-05-14 change as "convert
  vol_ratio / close_pos / buffer from +0.5 bonuses to HARD gates". Only
  `_build_vol_squeeze_signal` was changed. `_score_vol_squeeze` kept all three
  as optional bonuses, so a setup with none of them scored 2.0 box compression
  + 1.0 BB compression + 0.5 both-agree + 0.5 VWAP alignment = exactly
  `min_vol_squeeze_score` (4.0) — it cleared the floor, won its place in the
  build queue and then died on `vol_squeeze_weak_breakout_*`.

  Measured: **1,838** such rejections across the 2026-09-21 and 09-22 sessions,
  every one of them `weak_breakout_buffer` — i.e. a setup that qualified as a
  squeeze BREAKOUT with no break at all. The queue falls through to the next
  regime, so this did not lose trades outright; it inflated the normalised
  score that orders the auction, and made a guaranteed-fail regime the
  `entry_family` recorded against the skip — corrupting the attribution added
  on 2026-09-19 to tell a tuning problem from a dead regime.

  Third instance of this shape this month, after ORB's bare-break +2.5 and the
  range regime's optional prev-bar confirmation, so the fix follows the same
  pattern: one derivation, `_vol_squeeze_breakout_quality`, read by the scorer
  and the builder. Clearing all three gates is now the +1.5, and the freed
  bonuses re-point at DEGREE — +0.5 for a break at twice
  `vol_squeeze_breakout_buffer_pct` and +0.5 for volume at 1.5x the required
  ratio — so the score still discriminates among qualifying setups. The ceiling
  stays 6.5, which matters more here than elsewhere: vol_squeeze has the widest
  headroom of any enabled regime, so its ceiling drives its ranking. Compression
  alone now caps at 3.5, below the floor. `vol_squeeze_hard_breakout_gates:
  false` reverts BOTH, because the point is that they agree under either
  setting.

  Coverage: 1,797 passing (+24). `tests/test_vol_squeeze_regime.py` sweeps the
  grid of break strength x breakout volume x bar close position and asserts
  nothing the scorer qualifies is rejected by the builder for a weak breakout,
  on both sides and under both settings of the flag, with the builder's failure
  tags pinned so re-deriving them from the shared helper cannot silently rename
  one.

- **Neither mean-reversion regime could take a mean-reversion trade.**
  *2026-09-22* — the strategy declares two, `range` and `sr_scalp`, against a
  preset whose own universe thesis is that these names are "VWAP-reverting with
  occasional trend days". Three independent defects, one per stage of the
  funnel, and all three had to go for a support bounce or a resistance
  rejection to reach an order.

  **`_score_range` measured close to the opposite of what its builder gates
  on.** `_build_range_signal` enters only from the outer 35% of the lookback
  range; the scorer never looked at where in the range price was, and paid its
  largest single component (+1.5 of a 5.0 ceiling) for sitting within 0.20% of
  VWAP — the middle. Replayed over 10,162 real 1m bars (28 symbols,
  2026-09-21) it correlated **-0.17** with distance from mid-range, averaged
  1.94 at an edge against 2.19 mid-range, and blocked 6,485 of the 7,477 bars
  that were actually at an edge. It also took no `side`, so LONG and SHORT
  scored identically and the auction could not tell which edge price was on —
  every other regime scorer is side-aware.

  This is the defect `_score_sr_scalp` was redesigned out of on 2026-05-29
  ("measured chop character ... UNCORRELATED with the level geometry the
  builder actually gates on"), in the other mean-reversion regime; the lesson
  was never carried across. Same fix: score the builder's geometry, keep the
  character checks as corroboration. The fade zone now comes from one helper,
  `_range_entry_zone`, that both the scorer and the builder call — the
  `_breakout_reference` precedent from two days ago. Prev-bar confirmation
  moved from an optional +0.5 to part of the hard +2.0, because the builder
  demands it: a single-bar poke scored 3.5, cleared the floor, won its place in
  the build queue and then died on `not_near_range_low_prev_bar`. The freed
  +0.5 marks a deep rather than marginal fade. After: correlation **+0.68**,
  mean 3.04 at an edge against 0.50 mid-range, and 100% of qualifying bars in
  the builder's entry zone. `range_max_vwap_dist_pct` is removed.

  **`bollinger_squeeze_width_pct: 0.06` was not a threshold on this universe.**
  `bollinger_width_pct` is `(bb_upper - bb_lower) / bb_mid`, a FRACTION of
  price. Across 162,056 RTH 1m bars over 17 archived sessions (2026-05-01 ..
  09-21) it runs median 0.0041, p95 0.0167, max 0.236 — so 0.06 flagged
  **99.87%** of all bars as "in a squeeze". The flag PARTITIONS the two
  regimes: `_score_vol_squeeze` reads it as the compression to trade and
  `_build_range_signal` reads it as the condition to refuse. At 99.87% that
  partition was degenerate — vol_squeeze owned every bar and `range` owned
  none, which is why one session logged 387 of 387 range build failures on
  `bollinger_squeeze` and the regime has a single trade in the whole archive.
  The preset now sets 0.0025, the p25 of the measured distribution, and the
  flag fires on 25.7% of bars. `vol_squeeze_max_width_pct` moves 0.05 ->
  0.0035 for the same units reason: it is the OR-branch of the same test, and
  at 0.05 it was true on ~100% of bars, making `_score_vol_squeeze`'s +1.0
  compression point and its +0.5 "both signals agree" bonus free — 1.5 of a
  4.0 floor paid for nothing. **Preset only**; the code default in `config.py`
  is untouched, so no other strategy is affected.

  **`sr_scalp`'s stop floor bound on every setup, not the noisy ones.** The
  flat `sr_scalp_min_stop_atr_mult: 2.5` installed on 2026-07-29 after META
  07-28 (three shorts into a band the tape chopped 11.9 ATR through, stops
  1.09-2.70 ATR away, 63% of the window's bars trading through them) was the
  right diagnosis with the wrong instrument: the geometric stop here is at most
  proximity 0.4 + zone half-width 0.2 + `level_buffer` ~0.3 = about 0.9 ATR
  from entry, so 2.5 always won. The level picked the direction and was then
  discarded on the risk side, which is not what an S/R scalp is. It also made
  `sr_scalp_min_distance_atr: 2.0` a fiction — a flat 2.5 ATR risk against a
  target pinned to the opposing zone needs 2.4-3.2 ATR of gap to clear
  `min_target_rr`, so a setup at the documented floor could never build. Two
  floors with one silently dominant is the shape that also hid inside ORB.

  The floor is now measured against the level's own violation history: how far
  price has PIERCED it over `sr_scalp_noise_lookback_bars` (new, 20). A level
  that has been holding keeps its own tight stop; a level being cut through
  pushes the stop out past the breaches and, because the reward cannot stretch,
  the R:R check then rejects the setup — the META case, rejected for the right
  reason. `sr_scalp_min_stop_atr_mult` drops to 0.5 as an absolute backstop,
  and the rejection reason now names which floor bound (`bound_by=pierce|atr`).

  A same-day bug check found the first cut counting pierces from the start of
  the window, which includes a fresh flip's approach from the far side.
  FLIP-CONTINUATION leans on a confirmed-flipped level by definition, so it
  died on `stop_floor_kills_rr` whenever the break was inside the lookback
  (reproduced: `bound_by=pierce`, R:R 0.97 on a clean flip). Pierces now count
  from the first close on the entry side of the level, the crossing bar itself
  excluded; a flip that is later closed back through is still priced in. Found
  with the regime switched off and fixed anyway, so re-enabling it does not
  inherit it. Pinned by `TestSrScalpPierceStartsWhenTheLevelTookItsRole`, with
  the old window and two plausible wrong fixes (counting the crossing bar,
  restarting at the last reclaim) each caught.

  `disable_sr_scalp_regime` stays `true`. The geometry defect is fixed, so
  re-enabling it is now a trading decision rather than a bug fix, and the
  current week is already measuring the armed retest. `range` is enabled and
  its fixes are live.

  Coverage: 1,773 passing (+27). `tests/test_mean_reversion_regimes.py` pins
  the properties rather than the instances — that everything the scorer
  qualifies is in the builder's entry zone across the whole band of positions,
  that both follow a retuned `range_entry_zone_frac` together, that the squeeze
  threshold sits inside the measured distribution in both directions, and that
  a setup at the advertised gap floor builds — with a control that restores the
  old flat 2.5 and confirms the same setup is rejected, so the test cannot pass
  against the unfixed code.

- **Four logic defects in the ORB regime.** *2026-09-22* — found while
  evaluating why it has never worked. The headline answer is that **it has
  never run**: every attempt in the archive died on `orb_range_too_wide`, 3,403
  of them, with not one of any other ORB failure reason ever logged. That is a
  calibration problem — `orb_max_range_atr_mult: 4.0` compares a 15-bar opening
  range against a 1-bar `atr14`, and across 462 real symbol-days the median
  ratio is 4.64, i.e. the cap sits below the median. Left alone; enabling ORB
  is a trading decision and it ships disabled.

  The four logic defects underneath it were fixed, since all would have
  surfaced the moment the cap was corrected:

  * `_score_orb` awarded its +2.5 for a BARE break while `_build_orb_signal`
    required the break to clear `orb_breakout_buffer_atr_mult`. A one-tick poke
    scored 4.0, cleared the 3.5 floor, won its slot in the build queue and died
    on `orb_no_break_above` — a guaranteed-fail path, not an optimistic one.
    The +2.5 now requires the buffered break; the +1.0 marks a decisive break
    at 2x the buffer; the ceiling stays 5.0.
  * `_time_in_range` is inclusive at both ends, so the range-formation window
    overlapped the ORB window at `orb_range_end`, and formation returns first —
    making that minute unreachable. The window ran 09:46-10:05 while
    `entry_windows` opened at 09:45. The formation check is now half-open.
  * `orb_range_minutes` and `orb_end_time` had an unvalidated implicit
    ordering. Violating it silently shrank the window rather than erroring: 30
    minutes left 5 tradeable minutes, 34 left 1, 35 left none, with no error
    and no log line. `_validate_orb_window` raises at construction, only when
    the regime is enabled.
  * The range-end derivation was written out in three places. They agreed, but
    three copies of one rule is how the bot forms the range over one span and
    opens the window against another.

  Coverage: `tests/test_orb_regime.py` (22). Each fix was reverted
  individually and confirmed to fail its own tests — the first attempt at that
  check silently overwrote its own backup after a crash and left one fix
  reverted in the tree, so the verification is now driven from an immutable
  copy.

- **The EOD report's headline described the ACCOUNT, not the session.**
  *2026-09-21* — the same bug fixed in `manifest.realized_pnl` on 2026-09-19,
  in the call site that fix missed one function over.
  `account.capture_snapshot()` returns `account.realized_pnl`, a lifetime
  accumulator set to 0.0 once in `PaperAccount.__init__` and only ever
  incremented, and every headline number in `SESSION_REPORT` and the human log
  line read from it — pnl, wins, losses, win_rate, profit_factor,
  average_trade — while `trades` counted the list beside them. Two scopes in
  one line, the wrong one as the headline, which is the exact wording of the
  earlier fix.

  `closed` was also filtered only to final exits, never to the session date, so
  on a bot running across midnight without restarting the persistent
  `trades.csv` **re-appended the prior day's rows under today's date**. Both
  now use the same date filter `export_session_archive` already applied, so the
  report, the CSV it writes and the archive all describe one day. The headline
  sums the ROUNDED per-row values, so `realized_pnl` equals the sum of
  `trades.csv` exactly rather than to within a cent.

  A trade whose `exit_time` cannot be read is dropped rather than kept. The
  first version kept it, on the "unevaluable is not absent" principle — and a
  test written to assert that failed, because `_trade_csv_row` calls
  `exit_time.isoformat()` and one bad record raises inside the broad
  try/except around the whole block, costing the entire report. Losing a row
  beats losing the session.

- **An unreadable timestamp could take down the report and the dashboard.**
  *2026-09-21* — found while testing the above, and upstream of it.
  `PaperAccount._trade_to_dict` called `trade.exit_time.isoformat()`
  unguarded, and it runs inside `capture_snapshot`, which is the FIRST thing
  `write_session_report` does — so the session-date filter could never protect
  against it. One record with a bad timestamp destroyed the whole EOD report,
  and the dashboard snapshot shares the same path. Both timestamps now
  serialize to null instead of raising.

- **An expired arm could never produce its entry.** *2026-09-21* — found on
  day one of the armed retest, from a live miss. The market fallback exists so
  a runaway move that never offers the retest still gets traded; it was
  evaluated inside the build queue, AFTER the side / index-confirmation /
  confirmation-bar gates. The moment any of those lapsed while the arm waited,
  `_armed_retest_verdict` stopped being called and the 12-minute expiry was
  never reached.

  INTC: armed 09:58 at 118.39 on a setup that had passed every gate — only the
  arm held it back — the sector index lapsed at 10:03, and the stock ran 117.26
  → 124.64 with no entry. `touched=0` throughout, so the retest genuinely never
  came and the fallback was the whole point. It never fired once.

  The asymmetry that made it possible: index confirmation is an ENTRY gate, and
  once in a position it no longer applies. Arming holds the trade OUT across
  exactly the window where a lapse can lock it out, so a setup that had already
  cleared the gates is re-validated against them and can fail. The fallback has
  to be judged on the arm-time decision or it is not a fallback.

  Expiry now lives in `_expired_armed_retests`, which runs before the queue and
  is exempt from those three gates; the verdict keeps `none` / `wait` / `enter`
  and no longer reports expiry at all. The arm carries the `regime_score` it
  was validated with, so the fallback builds from the score the setup HAD
  rather than a fresh one that asks a different question. Still enforced: the
  regime must be offered in the current window, and the builder's own checks
  run unchanged (`breakout_confirmed` False → a faded setup still fails
  `no_fresh_breakout`; `_finalize_signal` still applies the stretched / SR /
  structure rejections).

  `skip_details` now reads the combined queue rather than `build_queue`, so a
  cycle whose only attempt was a fallback reports the regime that was tried
  instead of `none_qualified`.

  Note on the test: the first version of the regression passed against the
  UNFIXED code, because the synthetic decision for an expired arm set
  `index_ok: True` — a fake value that smuggled the exemption through the gate
  and made the test vacuous. It now carries `index_ok: False` and the exemption
  is `pre_validated`, explicitly, so reverting the fix fails the test.

  Coverage: 1,712 passing (+7).

- **An armed retest could go stale and then justify a market entry.**
  *2026-09-20* — two holes in the arm lifecycle, both found by walking the
  state transitions rather than the happy path. An arm past its window is a
  licence to enter at market on the next qualifying cycle, so how long one can
  survive is a trading question, not bookkeeping.

  * **A held position did not clear the symbol's arms.** An arm can never
    produce the entry it was created for once a position exists, and
    `_prune_armed_retests` kept it well past its window — so when the position
    closed, twenty minutes later and at a different level, the next qualifying
    cycle found an EXPIRED arm and took the market fallback immediately. The
    re-entry, which is the most chase-prone entry there is, was the one entry
    guaranteed to skip the wait. `entry_signals` now drops a symbol's arms
    when it is already held.
  * **The reaping window was 3x (42 minutes).** The regime can go a long time
    without qualifying — index confirmation lapses, the score dips — so a
    fallback entry could be justified by a breakout most of an hour earlier at
    a level the tape had moved away from. Now 2x the configured wait: past
    that the arm is dropped and the next qualifying cycle arms again, waiting
    rather than entering on stale evidence.

  Both are in the safe direction — the bot waits where it previously would
  have entered — and `per_entry_path` will show the effect as a shift from
  `market_fallback` toward `retest`. Coverage: 9 tests over the lifecycle,
  including that `AA` must not drop `AAPL`'s arms.

- **`entry_timing`'s baseline sampled the wrong tape.** *2026-09-20* — found
  in a second bug pass, after the metric had already been published. The
  docstring claimed a same-session comparison, but `bars_for` hands back the
  MULTI-DAY history frame and the sampler walked all of it — folding quiet
  overnight and prior-session bars into the baseline, depressing it, and
  inflating every edge measured above it.

  The whole metric is the gap between observed and baseline, so a baseline
  drawn from a different tape measures nothing. Now scoped to the trade's own
  session date. Every published number moved:

  | metric | before | after |
  |---|---|---|
  | overall baseline | 0.352R | 0.358R |
  | `trend` edge | +0.715R | **+0.641R** |
  | `sr_scalp` edge | +0.432R | +0.410R |
  | `pullback` edge | +0.249R | +0.213R |
  | `vol_squeeze` edge | +0.008R | **-0.024R** |
  | baseline samples | 8,140 | 3,477 |

  The conclusions hold and sharpen — `trend` still chases by a wide margin and
  `vol_squeeze` now reads as slightly BELOW an arbitrary moment, i.e. no edge
  at all rather than a sliver of one. Every citation of the old figures in the
  READMEs, the preset comment, the strategy docstrings and this file was
  corrected in the same change; leaving docs quoting a measurement the code no
  longer produces is how a number outlives the analysis that made it.

- **A confirmed armed retest could never build a signal.** *2026-09-20* —
  found in the final bug pass on the same day the feature shipped, before any
  dry-run. `_build_trend_signal` / `_build_momentum_signal` reject unless
  `close > max(high of the previous N bars)`. On the retest cycle that window
  STILL CONTAINS THE BREAKOUT BAR, whose high is above the reclaim close —
  which is what a retest *is*. So every confirmed retest died on
  `no_fresh_breakout`, and the feature could only ever produce expiry/market
  fills: precisely the chasing behaviour it was built to remove.

  Both builders now take `breakout_confirmed`, set only when
  `_armed_retest_verdict` returns `enter`. The arm already recorded the
  breakout and the verdict only confirms with close back above that level, so
  the gate is satisfied by construction rather than skipped — an EXPIRED arm
  passes `False` and must still clear it on its own, or a faded setup would
  enter where the bot would never have traded before.

  The end-to-end tests missed it because they stubbed the builder to isolate
  the wiring. `tests/test_armed_retest.py::TestTheRetestCanActuallyBuild` now
  drives the real builder on a real retest tape, and pins the rejection with
  the flag off so the fix cannot silently regress.

- **The armed retest would have switched itself on for `small_cap_squeeze`.**
  *2026-09-20* — `SmallCapSqueezeStrategy` subclasses `TopTierAdaptiveStrategy`
  and runs `trend` and `momentum` as its only two entry regimes, so a code
  default of `True` would have converted both of its entry paths to
  arm-and-wait with nobody choosing that — the same trap the vwap_reclaim knob
  shipped opt-IN to avoid on 2026-05-30, and its 2026-06-02 dry-run is the
  baseline for the next one. Now declared `false` in both its preset and its
  manifest, with a test that pins the premise (that it still runs the two
  arming regimes) alongside the opt-out, so the guard fails loudly rather than
  passing vacuously if that changes.

- **Eight params resolved off a code default.** *2026-09-20* — `zone_pct`,
  `zone_atr_mult`, `range_max_intraday_range_pct`,
  `range_require_prev_bar_confirmation`, `trailing_bias_enabled`,
  `trailing_bias_lookback`, `trailing_bias_majority_threshold` and
  `extended_hours_tradable_all` were read by the strategy and declared in
  neither the shipped preset nor the manifest. Live gates whose state could
  only be discovered by grepping the strategy, and editing any of those
  defaults would have silently retuned every preset at once — how
  `disable_orb_regime` came to say ORB-on as the config-less baseline while
  every preset turned it off. All declared at the value they were already
  resolving to, so behaviour is unchanged and the defaults are now inert.

  `tests/test_param_declaration_drift.py` pins the property rather than the
  list: every `self.params.get(...)` name must appear in the preset or the
  manifest, with a guard test that fails if the scan itself stops matching.


- **The session manifest's realized PnL described the account, not the
  session.** *2026-09-19* — `manifest.realized_pnl` read
  `account.realized_pnl`, a LIFETIME accumulator: set to 0.0 once in
  `PaperAccount.__init__` and only ever incremented, with no per-day reset.
  Beside it, `trades_today` and `trades.csv` are filtered to the session date.
  Two scopes in one manifest, and the wrong one is the headline number.

  Across the archived sessions that traded, **5 of 10 disagreed with their own
  `trades.csv`**, in both directions. 2026-07-31 shows the mechanism
  arithmetically: manifest −80.77 against a CSV summing to −60.22, a gap of
  exactly −20.55 — the previous session's PnL, carried forward by a bot that
  ran across both days without restarting. Four other sessions reported 0.00
  while holding real trades, understating a +13.59 day and a −122.80 day
  alike.

  The manifest now sums the same date-filtered list that produces `trades.csv`
  and `trades_today`, so `manifest.realized_pnl == sum(trades.csv)` holds by
  construction. Summing the ROUNDED per-row values (rather than rounding the
  sum) is deliberate — that is what `_trade_csv_row` writes, so the equality is
  exact rather than within a cent.

  A failed CSV export now reports `null` and a new `trades_export_error` field
  rather than leaving the initialized 0.0 in place. `_trade_csv_row` reads
  every `TradeRecord` field by name, so one record missing a field added later
  raises and is swallowed as a warning — and a wrong-but-plausible zero is
  worse than an absent value, because nobody investigates a zero. This is the
  likeliest explanation for the four 0.00 sessions.

- **A screener row with no usable ticker became a candidate.** *2026-09-19* —
  `_candidate_rows` built its symbol with `str(row.get("name"))`, which turns a
  missing value into the literal symbol `"None"` and a NaN into `"nan"`. The
  engine drops an EMPTY symbol from the watchlist but not a non-empty junk
  one, so the bot would fetch history and quotes for a ticker that does not
  exist and show it on the dashboard. Every screener's rows arrive from an
  external API, so the ticker is not ours to trust. New `_candidate_symbol`
  rejects genuinely absent values and normalizes the rest; dropped rows are
  counted and logged, because silently discarding screener rows would hide a
  TradingView schema change. Deliberately no blacklist of junk-looking tokens
  — that risks excluding a real ticker, so a ticker with no exchange prefix is
  still kept.

- **A raising strategy callback took down the whole screener run.**
  *2026-09-19* — `_candidate_rows` wrapped only the `float()` conversion in its
  try, leaving the `activity_score_fn(row)` CALL unguarded, and
  `directional_bias_fn` had no guard at all. Both belong to the strategy
  plugin, so one bad row's exception aborted the entire candidate list rather
  than that candidate. Both are now wrapped, warning once per cause per
  process — they fire per candidate per cycle, so unthrottled they would flood
  the log.

- **An unscored candidate list came back in reverse.** *2026-09-19* — with no
  `activity_score_fn` the score defaulted to the row's ordinal, and
  `_candidate_rows` sorts DESCENDING, so the last row of a screener's
  "best first" `order_by` came out on top. Unscored candidates now all get
  0.0 and the existing `-candidate_query_order` tiebreak restores the
  screener's own order. Latent — all 12 shipped call sites pass
  `activity_score_fn` — but it is reachable by any plugin that omits the
  callback, and it was also the fallback path the fix above just made
  reachable.

- **`small_cap_squeeze` emitted stale and duplicated candidate ranks.**
  *2026-09-19* — `_candidate_rows` numbers candidates 1..N within a single
  screen. `_merge_premarket_lock` then unions the premarket-locked set with
  the live RTH screen, re-sorts by `activity_score` and caps to
  `max_candidates` — but never re-ranked, so the output carried ranks like
  `[1, 2, 2, 3, 3]`: three candidates claiming rank 2 or 3. `rank` is the
  final tiebreak in `entry_gatekeeper._signal_priority_key` and is what the
  dashboard candidate card and the audit log's `candidate_rank` display, so
  the visible damage is diagnostic rather than directional — it only reaches
  execution order on an exact score tie. The merge now re-ranks and uses the
  same `(score, -query_order)` tiebreak `_candidate_rows` applies, instead of
  a plain stable sort that put locked-but-faded names ahead of equally scored
  fresh ones. The only screener with the problem: the other 17 either sort and
  cap the DataFrame BEFORE `_candidate_rows`, or build candidates manually and
  reassign `rank` after sorting.

- **`top_tier_adaptive`'s screener read the ACTIVE strategy's params.**
  *2026-09-19* — `config.active_strategy.params` rather than
  `config.strategies[self.strategy_name].params`, the sole outlier among 18
  screeners. Identical today: `get_candidates` has exactly one caller
  (`engine._run_cycle`, passing `config.strategy`), so the screener is only
  ever built for the running strategy. But the failure mode if that stops
  holding is silent — another strategy's params carry no `tradable`, so `run()`
  returns an empty universe with no error.

- **Indicator lengths now mean the same thing on a span-scaled frame.**
  *2026-09-19* — `add_indicators(span_scale=N)` stretches every bar-count
  lookback but keeps the nominal column NAMES, so on `top_tier_adaptive`'s 1m
  LTF (`ltf_indicator_span_scale: 5`) `adx14` is a 70-bar ADX and `bb_*` are
  100-bar bands. `build_technical_levels_context` reads those columns when the
  requested length matches the nominal one and recomputes otherwise — and the
  recompute ran at the NATIVE length. `adx_length: 14` therefore meant ADX-70
  while `adx_length: 15` meant a true ADX-15: a one-digit config change
  swapping the lookback by 5x with nothing to warn on. Four parameters had the
  shape — `adx_length`, `bollinger_length`, `obv_ema_length` and
  `divergence_rsi_length`. On a 400-bar span_scale=5 tape, 14 vs 15 drifted
  59.7% on ADX and 86.4% on Bollinger `width_pct` (band width off by ~7x);
  after the fix, 8.9% and 2.3% — neighbouring lookbacks, which is what one more
  bar should mean. `add_indicators` now stamps the applied scale on
  `frame.attrs` and each `*_length` is treated as nominal, with the fallback
  computed at `nominal x span_scale`. The stamp is written unconditionally,
  including at 1.0: pandas propagates `attrs` through `__finalize__`, so a
  frame resampled off a stretched one inherits the stale scale while losing the
  stretched columns, and only an unconditional write on rebuild clears it. New
  `scaled_span` is the single definition of that arithmetic — `add_indicators`
  builds with it and every consumer recomputes with it, so producer and
  consumer cannot drift. LATENT when found, on two independent axes: all 19
  shipped configs and the `config.py` defaults sit on 14 / 20 / 2.0 so the fast
  path always won, AND no shipped call path hands a stretched frame to
  `build_technical_levels_context` in the first place — `top_tier_adaptive`
  passes the base 1m frame (as its README records), both `peer_confirmed_*`
  strategies build their LTF at scale 1.0, and the dashboard's `get_merged`
  call does not pass a scale. Nothing changes at `span_scale` 1.0; this closes
  the trap before either axis moves, and the `small_cap_squeeze` README
  actively invites one of them ("Raise `ltf_indicator_span_scale` toward 3-5").
  Deliberately NOT scaled: the
  `*_lookback_bars` / `pivot_span` window sizes, which have a single
  computation path and therefore no second path to disagree with, and
  `bollinger_std_mult`, which multiplies a standard deviation rather than a bar
  count. `add_indicators` also now rejects a zero, negative or non-finite
  `span_scale` by name instead of letting it surface as "TA_BBANDS function
  failed with error code 2: Bad Parameter" from inside TA-Lib.

- **Chart pattern detection could report a pattern that was not there.**
  *2026-09-19* — `chart_patterns._CHART_HELPER_CACHE` keys on `id(frame)`,
  which only identifies an object while it is alive: CPython hands a freed
  address to the next allocation of the same size. Three call sites built a
  slice that died inside the helper it was passed to (`_pivot_order`'s
  `frame.tail(10)`, and `f.tail(14)` in both symmetrical-triangle detectors),
  so a later `_tail(frame, 40)` could land on the dead slice's address and read
  ITS `_mean_range` — which sets `_level_tolerance` and the `_find_pivots`
  prominence floor. Instrumented over 400 analysis calls, 21 served at least
  one stale value; against a cache-free reference over 1500 frames, one frame
  gained a `bullish_ascending_triangle` the bars did not support, taking the
  context from bias 0.0 "neutral" to 1.1 "bullish". Nondeterministic — the same
  bars give a different answer depending on heap layout, which is why it never
  showed up as a reproducible complaint. Fixed structurally rather than site by
  site: the cache now pins every frame it keys on, so no address can be
  recycled while an entry names it. Benchmarked at no measurable cost (-1.9%,
  within noise, over 200 calls).

- **Bollinger context fields returned NaN where they promised `float | None`.**
  *2026-09-19* — the guard was `not series.dropna().empty` — "does this series
  hold a value ANYWHERE" — followed by an unconditional `.iloc[-1]`. Ten
  instances in `technical_levels.py`. A symbol that stops ticking for
  `bollinger_length` bars has zero rolling std, so `bb_width` is 0 and
  `percent_b` / `zscore` divide by it, and `ctx.bollinger_percent_b` came back
  as `float('nan')` rather than `None`. NaN fails every comparison, so
  `strategy_base`'s `bb_pct is None or float(bb_pct) <= 0.82` became *neither*
  and silently dropped the mid-band entry-score bonus; it also made the audit
  log invalid JSON. Replaced with a `_last_value` helper that checks the value
  actually read.

- **The exported candle detectors were starved of context.** *2026-09-19* —
  `detect_bullish_patterns` / `detect_bearish_patterns` sliced to 3 bars while
  `detect_candle_context` used `CANDLE_CONTEXT_BARS` (30). TA-Lib builds
  average-body and trend state from preceding bars and emits 0 inside its
  warmup, so 3 bars starves nearly every pattern: over 600 tapes the 3-bar
  slice fired on 88 where the 30-bar slice fired on 441. Neither has an
  in-repo caller, but both are re-exported from `_strategies/shared.py`, so a
  plugin author reaching for them inherited the shortfall with no error to go
  on. Both now use `CANDLE_CONTEXT_BARS` and agree with `detect_candle_context`
  exactly.

- **Tweezer patterns expired three times faster than every other 2-bar
  pattern.** *2026-09-19* — TA-Lib patterns stay reportable for three bars
  after completing (`_talib_pattern_value_from_key` scans outputs -1, -2, -3),
  but the two custom tweezers only ever compared the final pair, so they
  vanished one bar after completing. Same tier, same registry, a third of the
  lifespan. It reached the tier cascade, which returns only the longest tier
  that fired: a tweezer ageing out early let a 1-bar match win and downgraded
  the confirm tier from `solid_2c` (weight 0.70) to `weak_1c` (0.35) on bars
  where a TA-Lib 2-bar pattern would still have held. Both now share a
  `CANDLE_PERSISTENCE_BARS` constant that the TA-Lib scan loop derives its own
  window from, so the two cannot drift. The per-bar dashboard map stays
  completion-only — persistence belongs to the snapshot path, and smearing a
  match forward would mark bars the pattern did not complete on.

- **`adaptive_ladder` left the target behind price on fast moves.** *2026-09-19*
  — `_adaptive_ladder_management` stepped `ladder_active_index` to
  `active_index + 1` unconditionally, while the guard beside it correctly
  refused to set a target UNDER price. When one cycle cleared several rungs at
  once the index walked forward and the target stayed on the first rung, so
  `update_position` saw `last_price >= target_price` and exited the remainder.
  Walked a LONG from 100.5 to 104.9 against rungs at 101/102/103/104: the index
  stepped 1, 2, 3 while the target stayed 101.00 the whole way. The ladder
  therefore cut the position at rung 1 on exactly the fast moves it exists to
  ride, which matches the 2026-06-01 dry run where runners came in around 1R
  against 3-4R of MFE. New `_next_unpassed_rung` advances to the first rung
  price has NOT passed, and promotes straight to a runner when price has
  outrun the whole ladder. The orderly one-rung-at-a-time path is byte
  identical. Found by walking the state machine, not by inspection.

- **`adaptive_ladder` could promote a stop the quote had already passed.**
  *2026-09-19* — the promotion guard validated the candidate stop against
  `close`, taken from the management frame, while the exit check in
  `RiskManager.update_position` runs against the quote snapshot. Those are two
  different sources and diverge on a fast move, so a rung price had already
  fallen back through still promoted, and update_position stopped the trade out
  on the same cycle at a price well past the new stop: rung 1 at 101.00 with
  the bar closing 101.50 and a 99.00 quote promoted the stop to 100.69 and
  exited immediately, where the original 98.00 stop would have held. The guard
  now takes the tighter of bar close and live price — `min` for LONG, `max` for
  SHORT — so such a rung simply does not promote and the position keeps its
  existing stop. Promotions where the quote is still beyond the candidate stop
  are unchanged.

- **`force_flatten` overwrote the real exit reason.** *2026-09-19* — the
  force-flatten branch in `manage_positions` ran unconditionally and reassigned
  `reason`, so every stop, target or peak-giveback that fired inside the
  force-flatten window was recorded as `force_flatten`. The position closed
  correctly either way, but the `per_exit_reason` table that tuning is read
  from had its force_flatten bucket inflated and its `stop` / `target` buckets
  hollowed out at exactly the end-of-session hour when those fire most. Force
  flatten still guarantees the exit; it now only supplies the reason when no
  other exit already did.

- **0DTE option entries now reconcile risk against the actual fill.**
  *2026-09-19* — `qty` is sized from a `max_loss_per_contract` computed before
  the order goes out, off the previewed limit, and nothing recomputed it once
  the fill was known. The equity path has done this since 2026-09-18 via
  `realized_entry_risk`; options had no counterpart. It bites hardest in dry
  runs, where `submit_option_vertical` passes `allow_natural_fill=True` to model
  a chase, so fills land worse than the sizing assumed: sweeping the shipped
  gates over 60,000 quote pairs found 20 contracts booked at $25 of max loss
  each that actually risked $38.79 each, $776 against a $500 budget. Live limit
  orders cannot fill worse than their limit, so this was a measurement problem —
  the dry-run risk figures the strategy is tuned from were quietly optimistic.
  New `realized_max_loss_per_contract` + `RiskManager.realized_option_risk`;
  results land on position metadata with a WARNING past
  `risk.risk_overage_warn_frac`. Detection only.
- **`max_net_price_frac_of_width` (new, default 0.90) gates the net price
  against the strike width.** *2026-09-19* — `max_net_spread_price` is one
  dollar cap while widths differ per symbol, and at its shipped 2.40 it sat
  above every configured width (SPY/QQQ $200, IWM $100), so it could not reject
  a quote implying a credit at or above the spread itself. That books
  `max_loss = 0`; `size_option_position` refuses it, so the outcome was a silent
  no-trade rather than a blow-up, but a degenerate chain looked identical to "no
  setup today". The new gate removed all 53 credit-above-width cases on the same
  sweep and cut worst measured exposure from +55% to +36% of budget.

- **Strategy manifests validate their window TIMES, not just their shape.**
  *2026-09-19* — `_coerce_windows` checked that each window was a list of two
  non-empty values but never that those values were parseable times, so `"9am"`
  (or a bare integer, which `str()` coerces to `"930"`) passed manifest load and
  `parse_hhmm` raised inside a trading cycle instead. Each end now parses at
  load and the rejection names the field, index, end and value. All 17 shipped
  manifests were verified clean first — 57 windows, 114 time values — so this
  only affects newly authored plugins. Overnight windows (`start > end`, which
  wrap past midnight by design) remain valid.

### Changed

- **`_decide_side`'s VWAP arm reads the same reference as `_frame_agrees`.**
  *2026-09-19* — the confirmation gate moved off session VWAP while the side
  vote did not, so the two layers disagreed about what "reclaimed VWAP" meant
  for the same symbol on the same bar. Auditing each arm against the tape's
  real direction on a V-reversal: the VWAP arm was wrong on 54 of 120 bars and
  the EMA arm on 59, while the recent-return arm — the only genuinely
  current-action signal — was wrong on 14. The two lagging arms are also the
  two that vote most reliably, because `recent` and `bars3` carry dead-bands
  and abstain often. Under `leg_anchored_confirmation` the arm now reads
  `_leg_anchor_vwap`: the vote goes from 66 ok / 18 undecided to 98 ok / 1
  undecided on a V-reversal (and 66 / 15 to 98 / 3 inverted), with trends,
  grinds and chop unchanged. The breakdown token becomes `legvwap` so a
  `side_undecided(...)` line names the reference. The EMA arm is deliberately
  untouched — its span is shared with the trend filters.
- `_frame_agrees` is no longer a `@staticmethod` (it reads `self.params`). Both
  call sites already invoked it through `self`, so no call site changed.
- `_leg_anchor_vwap` scans from the session open the rest of the strategy uses
  (`EQUITY_RTH_OPEN`, or `EQUITY_STREAM_START` in extended-indicator mode),
  matching `_day_strength_session_open`. Scanning from midnight let a thin
  pre-market print become the leg anchor while the rest of the strategy was
  still measuring from 09:30: on a frame carrying an 08:00 dump that moved the
  anchor off the session low entirely and flipped the verdict.
- `_leg_anchor_vwap` takes those bars by position rather than through
  `_same_day_mask`, which maps a Python lambda over every bar of the merged
  frame. At 12 peers x 2 sides that was ~15ms of per-candidate overhead for a
  slice `searchsorted` does in microseconds; the leg anchor now costs ~4ms.
- `EQUITY_RTH_OPEN` is re-exported from `_strategies.shared` alongside
  `EQUITY_STREAM_START`.

- **`top_tier_adaptive` retargeted at a mega-cap universe.** *2026-09-18* — the
  preset traded 23 mega caps with parameters written as if the universe were
  homogeneous. It is not: sector betas run roughly 0.5 (COST/V/TMUS) to 1.8
  (NVDA/AMD/TSLA) and ADR spans about 0.9% to 3%+, so one number was a
  different gate on every name. Seven changes, plus the two prerequisites they
  depended on.
  - **Per-symbol volatility scaling** (`daily_stats.py`, new). Every
    percent-of-price threshold is multiplied by the symbol's 20-day ADR (true
    range, so gaps count) over `reference_adr_pct`, clamped to
    `[volatility_scale_min, volatility_scale_max]`. Scaled:
    `default_stop_pct`, `momentum_min_day_strength`,
    `sr_scalp_min_distance_pct`, `relative_strength_block_threshold_pct`,
    `range_max_vwap_dist_pct`, `range_max_intraday_range_pct`,
    `vol_squeeze_max_range_pct`, `broken_level_min_clearance_pct`. ATR-multiple
    params are deliberately NOT scaled (already volatility-relative; scaling
    would square the adjustment). `momentum_min_day_strength: 2.0` was about a
    0.6-sigma move on TSLA and a 2-sigma move on COST, which made the momentum
    regime structurally a five-name strategy. Backed by
    `MarketDataStore.get_daily_history` — one Schwab daily `price_history` call
    per symbol per ET day; `vol_scale` is 1.0 (thresholds exactly as written)
    when stats are unavailable.
  - **Relative strength is now a beta residual**, `day_strength - beta *
    sector_ds` rather than a raw difference. The old form assumed beta 1.0 for
    every name and so measured beta, not alpha: on a −1.0% XLK day NVDA at
    −1.6% is performing exactly to a 1.6 beta — zero alpha — yet scored −0.6%
    and had LONG blocked. The gate was blocking high-beta names in the
    direction the tape was already moving while low-beta names essentially
    never tripped it. Beta comes from a 60-day daily regression against the
    symbol's sector ETF; when it is unavailable the gate is skipped rather than
    falling back to 1.0.
  - **Sector-breadth index confirmation.** `_index_confirms` now counts how
    many of a symbol's `sector_groups` peers lean the trade's way
    (`index_breadth_min_peers` / `index_breadth_min_agree_frac`), falling back
    to the sector ETF only for single-member sectors. The ETF test is close to
    circular on this universe — AAPL+MSFT+NVDA+AVGO are ~45% of XLK,
    GOOG+META ~45% of XLC — so it substantially asked AAPL about AAPL and
    failed in the one case that matters: the mega cap moving against its
    sector.
  - **Correlation-group concentration guard.** New `correlation_groups` /
    `max_same_correlation_group_same_direction`, replacing `sector_groups` /
    `max_same_sector_same_direction` for risk purposes (`sector_groups` stays,
    at GICS granularity, for ETF routing and peer breadth). Two LONG per sector
    across six sectors permitted up to 10 same-direction positions in names
    that run ~0.85 correlated on any macro day — one leveraged index bet
    wearing four tickers, with `max_daily_loss` reached in one move instead of
    four independent ones. The preset collapses tech + communication +
    consumer discretionary into one `mega_beta` bucket.
  - **Scheduled-event blackouts for equity strategies**
    (`event_blackouts.py`, new; top-level `events:` config section). Macro
    windows (CPI/FOMC, now optionally scoped with `symbols: [...]`) and a
    per-symbol **earnings** calendar blocking a weekend-aware
    `earnings_block_sessions_before`/`_after` window. Across 23 mega caps that
    is roughly 92 scheduled events a year.
  - **Cross-regime score normalisation.** `_normalized_regime_score` maps a
    score to its fraction of `(ceiling - threshold)` via the new
    `REGIME_SCORE_CEILINGS` table, and drives both the per-candidate build
    order and — through a new `signal_priority_key` override — the
    cross-signal slot auction.
  - **Regime trim**: `vol_squeeze`, `sr_scalp` and `orb` disabled in the
    preset, leaving trend / pullback / range / momentum.

### Changed

- **Dependency bump: schwabdev 4.0.0, TA-Lib 0.8.0, pandas 3.0.6,
  tradingview-screener 3.2.2.** *2026-09-18* — plus the security-relevant
  transitives (cryptography 46.0.7 -> 50.0.1, aiohttp 3.13.5 -> 3.14.3,
  websockets 16.0 -> 17.1, urllib3 2.6.3 -> 2.8.0, requests 2.33.1 -> 2.34.2,
  certifi 2026.2.25 -> 2026.7.22). See the requirements.txt header for the
  verification detail. Highlights: schwabdev 4.0.0 changes no API the bot
  touches, but adds request parameter validation ON by default — both
  price_history call shapes and the account-hash length rule were checked
  against it. TA-Lib 0.8.0's BBANDS/APO/PPO default changes do not reach this
  codebase (all BBANDS call sites pass their parameters explicitly); indicator
  regression showed worst absolute drift 7.5e-09 confined to three Bollinger
  columns, and two technical_levels snapshots were regenerated for the same
  round-off. **websockets 16 -> 17 is a major bump under schwabdev's streaming
  layer that the test suite cannot exercise — verify the stream on beta.**
- **Transitive dependencies are now pinned in `constraints.txt`.**
  *2026-09-18* — requirements.txt pinned only the 6 direct packages while
  schwabdev declares its four dependencies unbounded, so a rebuilt box
  resolved whatever was newest that day. Install with
  `pip install -r requirements.txt -c constraints.txt`.
- **Test and lint tooling is declared in `[project.optional-dependencies].dev`.**
  *2026-09-18* — pytest, hypothesis, flake8, vermin, vulture, pandas-stubs and
  build were installed in the working venv but declared nowhere, so
  `pip install -e .` produced a package that could not be tested. Install with
  `pip install -e ".[dev]"`.

- **`events.blackout_file` / `events.blackouts` replace
  `options.event_blackout_file` / `options.event_blackouts`.** *2026-09-18* —
  the blackout calendar lived inside `ZeroDteOptionsConfig` and was reachable
  only from the 0DTE options strategy. **Breaking for existing configs**: move
  those two keys from the `options:` block to the new top-level `events:`
  block. All shipped presets are updated. `EventBlackoutCalendar` now owns
  loading, mtime-based reload and matching; the 0DTE strategy's private copy
  is gone.
- **`_decide_side` is scoped to direction-following regimes.** *2026-09-18* —
  it collapsed `preferred_sides` for the whole candidate before any regime was
  scored, which silently removed both mean-reversion regimes: `range` enters
  within the bottom 35% of the range and `sr_scalp` at a support price has just
  fallen into, exactly where the vote's trend-following signals say the
  opposite. Simulated over a clean oscillating range with the shipped params,
  the vote agreed with the range regime's own entry zone on 3 of 54 in-zone
  bars (5.5%). The index and confirmation-bar gates already exempted these
  regimes; the vote running candidate-wide made those exemptions unreachable.
  Now gated per (side, regime) against `SIDE_DECISION_REGIMES`. New skip
  reason: `<side>_build_failed_<regime>_side_decision_opposed`.

### Changed

- **`top_tier_adaptive` retargeted at mega-cap Tech + AI, tuned for both
  directions.** *2026-09-18*
  - **Universe**: 25 names in three co-movement blocks — `ai_hardware`
    (NVDA/AVGO/AMD/TSM/MU/QCOM/ARM/MRVL/INTC/ANET/VRT/DELL), `platforms`
    (AAPL/MSFT/GOOG/AMZN/META/NFLX/ORCL/TSLA) and `software`
    (CRM/ADBE/NOW/PLTR/PANW). Dropped JPM/GS/V, LLY, COST, HD/LOW/UBER,
    TMUS/RBLX — none are Tech/AI and each dragged in an ETF that had to be
    streamed for one name's confirmation. `index_symbols` shrinks from six
    ETFs to three (SMH / IGV / XLK).
  - **Groups are by co-movement, not GICS.** META/GOOG/NFLX are Communication
    Services and AMZN/TSLA Consumer Discretionary, but across this universe
    they trade as part of the mega-cap compute complex, and peer breadth over
    that complex is a better confirmation than XLC or XLY. Every group has
    >= 5 members, so breadth (not the circular ETF check) is the live path for
    every symbol.
  - **`correlation_groups`** collapse to `ai_complex` (20) + `software` (5) at
    a cap of 2 — semis and platforms move together on any AI-narrative day.
  - **`reference_adr_pct` 0.018 -> 0.022.** The reference must sit near the
    median ADR of the traded universe or every percent threshold is
    systematically mis-scaled; the old value centred a mixed book that
    included ~1% names. `volatility_scale_min` 0.6 -> 0.55 so AAPL/MSFT are
    not clamped off the bottom, `_max` 2.2 -> 2.0.
  - **`momentum_min_day_strength` 2.0 -> 1.8**, now meaning "what a
    median-volatility name in this universe must move" — roughly 1.1% on AAPL
    and 3.4% on PLTR after scaling. The flat 2.0 made momentum a
    high-beta-only regime.

### Added

- **Long/short asymmetry knobs for `top_tier_adaptive`.** *2026-09-18* — every
  threshold was previously shared between the sides, which assumes they are
  mirror images. On equities they are not: squeezes are faster than flushes,
  the market drifts up, and short profits are less durable. Three multipliers
  encode exactly those asymmetries instead of duplicating the parameter block
  per side, and all are neutral by default so existing presets are unchanged:
  - `short_min_score_premium` (0.5) raises the regime floor for SHORTs — and
    raises the normalisation denominator too, so a short that barely clears
    its higher bar still ranks as marginal in the cross-regime auction rather
    than being flattered by the long-side floor.
  - `short_stop_buffer_mult` (1.25) widens the ATR cushion on shorts. It never
    moves the structural level the strategy chose. Dollar risk per trade is
    unchanged — the wider stop simply sizes to fewer shares.
  - `short_target_rr_mult` (0.85) banks short profits sooner: a 2.0R long
    target becomes 1.7R short.

### Added

- **Test coverage for every previously-untested module.** *2026-09-18* — the
  full-bot review found seven modules with no direct tests, several of them in
  the money path. Now covered: `startup_reconciler` (541 LOC — what the bot
  owns after a restart: ignore lists, restore eligibility, metadata matching,
  level reconstruction, the entry block and its broker recheck, and the
  restore loop), `cycle_gate` (which subsystems may run this cycle),
  `warmup_tracker` (history-fetch scheduling and readiness), `position_store`
  (sqlite round-trip, replace semantics, pruning), `_sr_ladder` (rung spacing
  and next-level selection for adaptive_ladder), `dashboard`
  (NaN/numpy-safe serialization, disk-state signature, theme-name handling,
  concurrent state access) and `audit_logger`. Every module in the package now
  has direct coverage.

### Fixed

- **ORB could emit a target on the wrong side of its own entry.**
  *2026-09-18* — found by property-testing every builder over randomised
  opening ranges. `_build_orb_signal` anchors its measured move to the RANGE
  EDGE (`edge + range_height * orb_target_range_mult`), not to the entry, so a
  break that had already run past that level produced a LONG whose take-profit
  sat BELOW its entry — 57 of 514 builder invocations, e.g. close 107.26 with
  a target of 101.01. The gatekeeper's `_entry_levels_valid` refused those
  signals so nothing traded, and the ORB regime is currently disabled in the
  shipped preset; but a builder should not emit a structurally invalid setup
  and rely on a downstream guard. It now rejects with
  `orb_measured_move_exhausted` when the target cannot clear
  `shared_entry.min_target_rr` from the current close — the same way sr_scalp
  rejects when its zone gap cannot pay for its stop. A re-run shows 0
  violations across 457 invocations of all seven builders, both sides.

- **Risk controls that failed open now say so.** *2026-09-18* —
  `open_risk_to_stops` skipped positions whose levels would not parse, which
  UNDER-counts open risk and makes the daily-loss projection more permissive;
  and `can_open` swallowed a strategy-params lookup failure, silently
  disabling the correlation concentration guard entirely (`max_group` -> 0).
  Both now log. A zero-quantity position stays silent — that is normal, not a
  data problem.

- **`_strategies/rvol.py`: three defects in the liquidity-profile module.**
  *2026-09-18* — six strategies route their volume gating and focus scoring
  through it and it had no direct coverage.
  - **`_symbol_set` mishandled every Mapping spelling.** It read `.values()`,
    so the natural YAML set `{AAPL: true, MSFT: true}` collapsed to the single
    token `TRUE` and silently discarded the symbols, while
    `{tech: [AAPL, MSFT]}` stringified the list into one bogus token. Now the
    key is the symbol when the value is scalar and the values are the symbols
    when the value is a container; nested structures flatten and the recursion
    is depth-bounded.
  - **A `rvol_score_floor` above `rvol_score_cap` silently returned a
    constant.** `min(cap, max(floor, raw))` with an inverted pair yields `cap`
    for every input, so the volume term stopped distinguishing a dead tape
    from a 5x surge and every `focus_score` built on it degenerated to a
    scaled day-change. The floor is now DROPPED rather than clamped to the cap
    (clamping leaves `floor == cap`, still a constant), and the
    misconfiguration is logged once per process — it fires from a per-symbol
    hot path.
  - **The hard-coded liquidity lists had drifted.** ORCL, PLTR, ARM, MU, ANET,
    QCOM, ADBE, NOW, PANW, MRVL and DELL were all absent, so they were gated
    at the full threshold while AAPL got an 80% relaxation — a 5x gap between
    comparably liquid mega caps. `IGV` was missing from the benchmark list
    while `SMH` and `XLK` were present. Lists refreshed, and
    `rvol_profile_for_symbol` now takes an optional `dollar_volume` so a
    symbol above `rvol_high_liquidity_dollar_volume` (default $1B) is treated
    as liquid regardless of the list. Threaded through all six consuming
    screeners, which already select `close` and `volume`.
  - 50 new tests, including one asserting no caller ever gates on the floored
    value — the separation that stops a benchmark ETF's 0.90 score floor from
    carrying it through its own volume gate on a dead tape.

- **`audit_logger` fallbacks could themselves raise.** *2026-09-18* — found by
  the new tests. `log_structured`'s except branch called `str(payload)`, which
  re-raises for a payload whose `__repr__` throws, so the safety net
  propagated into the caller — and the callers are the entry and exit flows
  (`ENTRY_CONTEXT` / `EXIT_CONTEXT`). `_json_ready`'s final `str(value)` had
  the same hazard, and it also feeds the sqlite position-metadata write. Both
  now degrade to `<unserializable {type}>` instead of throwing.
- **The engine now escalates persistent failures.** *2026-09-18* — the main
  loop backs off exponentially and never gives up, which is right, but a
  sustained outage during the management window left open positions unmanaged
  behind a throttled WARNING. After `runtime.error_escalation_cycles`
  consecutive failed cycles (default 10, roughly 10 minutes at the capped 60s
  backoff) the engine logs CRITICAL naming the exposed positions and the
  dashboard status carries the same alarm text. Re-announces on each multiple
  so a long outage does not scroll away. Set to 0 to disable.

- **`max_daily_loss` now projects open risk instead of comparing realized P&L
  alone.** *2026-09-18* — the gate blocked new entries once REALIZED P&L
  crossed the limit, and never flattened anything. With `max_positions` open
  at full risk at that moment, the day could finish at roughly twice the
  configured cap. `can_open` now subtracts `RiskManager.open_risk_to_stops`
  (each position's remaining loss to its CURRENT stop, so a stop trailed to
  breakeven contributes zero) before comparing. Set
  `risk.daily_loss_includes_open_risk: false` for the old realized-only
  comparison.
- **Per-day risk state survives a restart.** *2026-09-18* — `RiskState` was
  memory-only, so a crash or restart mid-session reset `realized_pnl` to 0.0
  and dropped every cooldown and same-level block. A bot restarted after
  losing most of its `max_daily_loss` came back believing the day was flat and
  could lose the limit again — and a restart is most likely exactly when
  something has already gone wrong. Open positions were always recovered from
  the broker; the counters that decide whether to open MORE were not. New
  `position_store.SessionRiskStateStore` keeps them in the same sqlite file as
  the reconcile metadata, keyed by ET session date so the daily reset is just
  the absence of a matching row. `RiskManager` takes the store at construction
  and restores before the first cycle; persistence failures log and never
  raise.

- **Entry sizing now accounts for slippage, and realized risk is reconciled
  after the fill.** *2026-09-18* — `qty` is computed before the order from the
  previewed limit price, but realized risk is `qty * |fill - stop|`, so an
  adverse fill risked more than `max_notional_per_trade *
  risk_per_trade_frac_of_notional`. `size_position` now takes a
  `slippage_allowance` that widens the SIZING distance only (never the actual
  stop), derived from the live spread via `RiskManager.entry_slippage_allowance`
  and capped at `entry_slippage_allowance_max_pct` of price.
  `RiskManager.realized_entry_risk` then reconciles what the trade actually
  risks against the budget; anything beyond `risk_overage_warn_frac` is logged
  and stamped on the position as `entry_risk_overage_frac`. Detection only on
  that half — the shares are already bought, so the sizing allowance is the
  preventive control.
  - Scope note: the exposure is bounded by whichever constraint sized the
    trade. The risk budget only binds when stop distance exceeds
    `budget / max_notional` as a fraction of price (0.800% on the top_tier
    preset); below that the notional cap decides the size and leaves large
    slack. The worst case is therefore a LOW-priced name with a stop just past
    that crossover (a $50 name on a 1% stop, where a 10c slip reaches ~20% over
    budget), not the tightest structural stops — at 0.11% of price the notional
    cap holds realized risk ~80% UNDER budget.
- **Equity entries re-validate their levels after the fill.** *2026-09-18* —
  the pre-order check ran against the previewed price, so a fill that slipped
  through its own stop left a position whose entry was already past its stop
  with no warning (`initial_risk` uses `abs()`, so it still read positive and
  the stop simply fired on the next management cycle). The options path had
  always revalidated here; equities now match it, falling back to
  `default_stop_pct` / `default_target_pct` distances from the actual fill
  rather than orphaning the position or keeping levels the fill invalidated.
- **Entry slippage is now watched, not just recorded.** *2026-09-18* —
  `entry_slippage_pct` reached `paper_account` and the end-of-day report and
  nothing else, so a routing or liquidity degradation surfaced only if someone
  diffed reports by hand. Breaches of `entry_slippage_warn_pct` are logged and
  flagged on the position. `realized_entry_risk`, `entry_risk_budget` and
  `entry_risk_overage_frac` are carried on `TradeRecord` and serialized into
  the session report alongside it.
- **`_in_orb_window` is bounded at both ends.** *2026-09-18* — it read
  `now <= orb_end_time` with no lower bound and no check that the ORB regime
  existed, so the seven `orb_bypass_*` relaxations (HTF bias, structure, S/R,
  exhaustion, side decision, relative strength, screener bias) applied to
  entries with no ORB thesis behind them. Harmless on the RTH top_tier preset
  (09:45 is its first entry), but live on `small_cap_squeeze`, which sets
  `equity_session_indicator_window: extended` and `disable_orb_regime: true`:
  its entries from 08:05 to 10:05 — the majority of an 08:05-11:50 window —
  ran with those gates off. Now `[opening-range end, orb_end]`, and always
  `False` when `disable_orb_regime` is set.
- **`daily_stats.compute_adr_pct` dropped the window's first bar.** Its
  `prev_close` is NaN and the row-wise max skips NaN rather than propagating
  it, so that bar contributed a high-low value instead of a true range and a
  20-day lookback returned 21 samples.

### Added

- **Broker-side bracket orders: the entry, stop, and target go out as one
  Schwab first-triggers-OCO order.** *2026-07-27* — the protective exit now
  rests AT THE BROKER instead of waiting for the engine's management poll to
  observe the level and fire a marketable limit. At `quote_poll_seconds: 6`
  that removed up to ~6s of latency on exactly the fast moves where it costs
  most. Opt-in via `execution.bracket_orders_enabled` (default `false`), so
  every existing preset keeps today's fully engine-managed exits.
  - **Spec** (`execution.py::build_bracket_order`): a `TRIGGER` parent whose
    child OCO carries a `LIMIT` target and a `STOP`/`STOP_LIMIT` protective
    stop. A lone stop is attached directly rather than wrapped in a
    single-child OCO. Levels round to valid ticks (penny at/above $1) biased
    so a stop never lands *tighter* than intended and a target never lands
    further away — sub-penny prices are rejected outright on stop legs.
  - **`execution.bracket_legs`** — `stop_and_target` rests both;
    **`stop_only`** rests only the stop and leaves the target engine-side.
    `stop_only` is *required* with `trade_management_mode: adaptive_ladder`
    and is enforced at config load: a resting target limit fills through
    `adaptive_ladder_suppress_target_exit` (the ladder declining a target tag
    so it can roll to the next rung) and through the final-rung runner
    (`target_price` cleared to `None`), so the ladder could never extend.
  - **`execution.bracket_sync_mode`** — `static` submits once and never
    touches the order; `replace` keeps the engine managing and pushes every
    stop/target move onto the resting child via `replace_order`, debounced by
    `bracket_replace_min_price_delta` so a per-cycle trail ratchet cannot burn
    the Schwab rate budget. `static` is refused at load for the ratcheting
    management modes, which would otherwise leave the broker on the entry-time
    stop for the life of the trade.
  - **Ownership split** — `RiskManager.update_position` suppresses only the
    exits actually resting at the broker, keyed on the live child order ids
    (not the config) so a bracket that failed to establish correctly falls
    back to engine exits. Engine-only exits (peak giveback, trailing, time
    stop, CHoCH, force flatten) still fire, and `PositionManager` cancels the
    resting orders **before** marketing out. A failed cancel *defers* the exit
    rather than double-filling — the protective stop is still resting, so the
    position is not left unguarded.
  - **Phantom-position reconciliation** — `_reconcile_bracket_fills` runs at
    the TOP of `manage_positions`, booking any exit the broker already
    executed before anything can manage or exit a position that no longer
    exists. Uses one `account_orders` call for all positions, not per-position
    `order_details`: at 4 positions and `loop_sleep_seconds: 2.0` the latter
    would be 120 req/min, the entire Schwab budget. An unreadable broker
    response is treated as "unknown", never as "nothing filled".
  - **Short-flip guard** — children are submitted for the *requested* entry
    quantity, so a partial fill leaves an oversized resting exit that would
    take a long-only strategy net short when it triggers. The partial path
    cancels the parent first (so the resize target cannot move underneath),
    then replaces both children at the filled quantity; if the children never
    materialised, standalone protection is submitted for the shares actually
    held.
  - **Adoption** — `ensure_position_protected` adopts still-working children
    and submits fresh protection only when they are dead, so broker entry
    recovery and startup restore can never stack a second protective order.
    Startup restore also rewrites stale `bracket` metadata: left alone, a dict
    whose children died overnight would suppress the engine's stop exit for a
    position with nothing resting at the broker.
  - **Dry run** keeps exits engine-side and stamps the intended bracket for
    inspection. Live fills at the resting limit will beat these poll-priced
    exits, so dry-run *understates* the benefit.
  - No reprice loop on a bracketed entry: cancel/replace churn on a `TRIGGER`
    parent with live children is how brackets get orphaned, and an entry
    needing several reprices has already left the level it was built on.
  - 40 tests in `tests/test_bracket_orders.py`.

### Fixed

- **Same-level retry block measured from the wrong price and ignored winners.**
  *2026-07-31* — `StopoutRecord` is now `RecentExitRecord` and the block keys off
  the prior ENTRY price, the level actually being re-tried, instead of the exit.
  A stopped-out SHORT exits roughly 1R ABOVE its entry, so re-entering the same
  level always sat ~1R from that exit and cleared any sane threshold. AAPL
  2026-07-30 took FIVE shorts inside $0.37 (331.46 / 331.57 / 331.66 / 331.78 /
  331.83): consecutive entries were 0.05-0.12 apart (0.1-0.25 ATR) while the
  exit-referenced distances were 0.50-1.02 — four to ten times larger. Only one
  of the five was ever blocked, and widening `same_level_block_atr_mult` to 1.5
  the day before had not helped because the reference point was wrong.
  Additionally, every exit is now recorded rather than losses only: the
  loser-only rule left a re-entry after a WIN completely unchecked, which is how
  that run alternated W/L/W/L/L through one price for three hours. All four
  `position_manager` call sites pass `entry_price`; a test asserts none can
  regress.

- **Low-tier peak-giveback retained almost nothing.** *2026-07-31* — with the
  partial-breakeven tier disabled the day before, `peak_giveback_low_tier`
  became the earliest ratchet, and at `giveback_frac: 0.7` it kept only 30% of
  peak. NEM 2026-07-31 peaked 0.85R and booked +0.17R. Across 07-30/31 winners
  realised 39% of their peak (avg +0.58R against a 1.39R median MFE) while
  losers ran -1.23R, which needs a 68% win rate to break even; 55% was achieved.
  `peak_giveback_low_tier_giveback_frac` 0.7 -> 0.45 retains 55%, bridging to
  the main tier's 0.65 retain at 1R+.

- **Side-decision gate rejected shorts throughout a tech selloff.** *2026-07-29*
  - Over 2026-07-27..29 the tech universe fell 4-12% (AMD -12.3%, NVDA -6.2%,
    INTC -4.6%, TSLA -4.4%, XLK -3.9%) and `top_tier_adaptive` won zero shorts.
    `side_undecided` was the single largest blocker on those names — 3,506 of
    12,935 decision rows — ahead of every regime and quality gate.
  - Two defects in `_decide_side`. **(a)** `side_decision_min_votes` was an
    ABSOLUTE count against a variable number of voters, while `recent` and
    `vwap` both carry neutral dead-bands and abstain ~35% of the time; a
    unanimous 2-0 read therefore failed a 3-vote bar. **(b)** the last-3-bars
    colour signal had no neutral band (`greens >= 2` LONG else SHORT), so with
    three bars it always voted, and in a downtrend the constant two-green
    bounce bars voted LONG against the trend while carrying the same weight as
    the EMA structure — it split 1,021 L / 1,053 S, a coin flip.
  - Net effect: the reliably-achievable SHORT tally in a downtrend was 2
    (vwap + ema), so requiring 3 only admitted shorts once price was already
    making a new low — at local exhaustion, right before the bounce that swept
    the stop. This is the same root cause as the "entered late, stopped on the
    reversal" pattern (XOM 07-29 entered at 98.7% of its leg).
  - `side_decision_min_votes` is REMOVED. The threshold is now a majority of
    the signals that actually voted:
    `required = max(side_decision_min_agreeing, ceil(participating × side_decision_majority_frac))`,
    defaults `2` and `0.6`. The 3-bar signal votes only when unanimous (3 green
    / 0 green) and otherwise abstains. Replaying the 3,506 undecided rows:
    07-28 (bounce, XLK +0.23%) 49% LONG / 19% SHORT; 07-29 (selloff,
    XLK -2.10%) 37% LONG / 38% SHORT — shorts go from unavailable to available
    on the down day, with the long-lean preserved on the bounce day. A lone
    structural signal still abstains (`min_agreeing` floor).

- **`min_pullback_score: 4.0` was above the regime's reachable ceiling.**
  *2026-07-29* — raised from 3.5 two days earlier on a 48-trade sample. Live
  pullback scores top out at 3.5, so the floor silently disabled the regime
  (185 of 185 otherwise-qualifying tech shorts blocked) — the same failure the
  config comment warns about for `min_sr_scalp_score`. Reverted to 3.5.
  `min_trend_score` 4.5 -> 4.0 as well: 4.5 blocked 384 of the 631 tech shorts
  that cleared the old floor. A regression test now asserts both floors sit
  below their observed ceilings.

- **sr_scalp built stops inside the noise band.** *2026-07-29* — zone geometry
  can park the stop a fraction of an ATR from entry regardless of how wide the
  tape is. On 07-28 META chopped 11.9 ATR between 13:50-14:50 while sr_scalp
  shorted the FLOOR of that band three times with stops 1.09-2.70 ATR out; 63%
  of the window's bars traded above those stops. All three needed 2.7-4.3 ATR
  to survive and all three resolved in the trade's direction after stopping
  out. `shared_entry.min_stop_atr_mult` does not cover this — it bounds only
  the refinement passes, never a builder's own stop. New
  `sr_scalp_min_stop_atr_mult` (default 2.5) floors the builder stop, and when
  widening drops R:R below `shared_entry.min_target_rr` the setup is rejected
  outright, since sr_scalp's reward is capped by the opposing zone.

- **Partial-breakeven tier scratched working trades.** *2026-07-29* — it moved
  the stop to entry + `adaptive_partial_breakeven_offset_r` (0.0 = exactly
  entry) once `max_favorable_r` crossed 0.5R. Median trade MFE is ~0.5R, so it
  armed at the median trade's peak and a normal retrace then scratched it.
  NFLX 07-28 SHORT: a 5.9-ATR stop it never came near (needed 1.1), ratcheted
  to entry at a 0.52R peak, shaken out for -$2.18 — then ran to +2.47R. The
  tier existed to bridge the gap between the trail and a 1.0R breakeven; with
  breakeven now at 0.60R that gap is gone and the two sat 0.1R apart doing the
  same thing. Disabled (`adaptive_partial_breakeven_rr: null`).

- **Same-level re-entry block was too narrow to catch repeats.** *2026-07-29* —
  `same_level_block_atr_mult: 0.3` is $0.14 on META, so the three 07-28 SHORTs
  at 593.05 / 593.82 / 593.17 (13:55, 14:20, 14:39, all stopped at ~594.3) read
  as three different levels, and the 12-minute cooldown was cleared by the
  19-25 minute gaps. Widened to 1.5 ATR, which folds that pocket into one level.

- **`BaseStrategy._position_r_multiple` is now a `@staticmethod`.**
  *2026-07-27* — it never referenced `self`. Matches the neighbouring
  `_frame_atr14`; both call sites are unchanged.

- **Stop-refinement had no floor and silently overrode every builder's
  `default_stop_pct` backstop.** *2026-07-27*
  - `_refine_bullish_sr_levels` / `_refine_bearish_sr_levels` /
    `_refine_bullish_technical_levels` / `_refine_bearish_technical_levels`
    each moved the stop TOWARD entry whenever a support/resistance level or
    trendline sat inside the strategy's structural stop, with no lower bound.
    A level a few cents from entry produced a few-cent stop. `min_target_rr`
    could not catch it: tightening a stop RAISES reward/risk, so the R:R guard
    that protects the *target* cap never binds on the stop side.
  - Measured over 2026-05-12..29 on `top_tier_adaptive`: **46 of 57 entries had
    their stop pinned exactly to `nearest_support - level_buffer`**, a median 2x
    (worst 10x — META 2026-05-26 got 0.11% of price against the 1.0%
    `default_stop_pct` floor) tighter than the builder intended, parking the
    stop on the single price most likely to be swept.
  - New `shared_entry.min_stop_atr_mult` (default `1.5`) floors the refined
    stop in ATR14 units, via `BaseStrategy._clamp_refined_stop`. An over-tight
    proposal is *clamped back* to the floor rather than discarded: discarding
    reverts to the flat `default_stop_pct`, which on a quiet symbol is enormous
    in ATR terms (one logged case reached 11 ATR) and would push R out far
    enough that nothing priced in R — breakeven, profit-lock, runner,
    peak-giveback, `discretionary_exit_min_r` — could ever arm. The clamp never
    widens past the incoming stop, so builders that deliberately choose a
    tighter stop (range / sr_scalp / momentum) keep it.
  - Replayed against all 57 logged entries: 9 stops adjusted, all to exactly
    1.5 ATR; median stop width unchanged at 3.87 ATR.
  - The two SR helpers now take the bar `frame` (to read ATR14); all 16 call
    sites across 9 strategies updated.

- **Discretionary exits carried no R condition and were displacing the stop.**
  *2026-07-27*
  - The bias-based structure exits, every branch of `_technical_exit_signal`
    (trendline / channel / bollinger / anchored-VWAP) and the S/R break exits
    are pattern reads on the tape gated only by grace windows and tape
    confirmation. On a trade still hovering around entry they acted as an
    arbitrary tightened stop.
  - Over 2026-05-12..29 they closed **19 of 48 `top_tier_adaptive` trades at a
    median MFE of 0.09-0.29R for -$831 combined**, and among trades whose stop
    sat beyond 4 ATR, **0 of 22 ever reached that stop** — one of these got
    there first, every time.
  - New `shared_exit.discretionary_exit_min_r` (default `0.5`) gates the family
    on open profit in initial-risk R units, via
    `BaseStrategy._discretionary_exit_allowed` / `_position_r_multiple`. R is
    measured against `metadata['initial_stop_price']`, not the trailing
    `position.stop_price`, so it does not drift as management moves the stop.
    CHoCH exits stay exempt — a true change-of-character is a reversal signal,
    not noise.
  - Counterfactual replay of the 21 suppressed exits against the archived 1m
    bars: **-$722 under the full post-fix ladder vs -$859 actually booked
    (+$137)**, and -$874 under a deliberately pessimistic bound that caps all
    upside at the moment the gate opens. 7 of the 21 go on to hit a full stop —
    the discretionary exits were partly doing useful work, so the gain is
    materially smaller than the -$831 those exits booked. A threshold sweep
    (0.25 / 0.5 / 0.75 / 1.0R) favours the gate at every non-zero value under
    the full-ladder replay, but one-trade differences swing the ranking at
    n=22, so the sample cannot tune it further.

- **`sector_index_map` could silently disable five regimes for a whole sector.**
  *2026-07-27*
  - `TopTierAdaptiveStrategy.active_watchlist` streams bars for `index_symbols`
    and nothing else, while `_indices_for_symbol` resolves a candidate's sector
    through `sector_index_map`. When the two disagreed, `_index_confirms` got
    `None` bars for every mapped ETF, fell through its loop and returned False —
    permanently blocking every symbol in that sector from the five
    index-confirmed regimes (trend / pullback / vol_squeeze / momentum /
    vwap_reclaim). The only symptom was a `..._index_not_confirmed` skip line,
    indistinguishable from the index genuinely disagreeing.
  - `config.load_config` now runs `_validate_sector_index_map` and fails loudly
    at load time. Only sectors that actually have members are checked — mapping
    a sector you have not populated yet is harmless, and presets routinely carry
    a full 11-GICS map against a narrower traded universe.

- **README `top_tier_adaptive` defaults table had drifted from the manifest.**
  *2026-07-27* — `min_bars` 60→150, `ltf_minutes` 5→1 (the 1m-LTF migration
  landed 2026-05-29 but the table kept the 5m-era numbers), `min_ltf_bars`
  15→120, `min_sr_scalp_score` 3.5→3.0.

### Changed

- **Dependency pins bumped to latest.** *2026-07-27*
  - `schwabdev` 3.0.4 → 3.0.5, `tradingview-screener` 3.2.0 → 3.2.1,
    `pandas` 2.3.3 → **3.0.5**, `numpy` 2.4.4 → 2.4.6, `TA-Lib` 0.6.8 → **0.7.1**.
    `PyYAML` stays at 6.0.3 (already latest). `requires-python = ">=3.11"` is
    unchanged — pandas 3 and numpy both floor at 3.11.
  - pandas 3 makes **Copy-on-Write the default**, the usual breaking point for
    this upgrade. The package is CoW-safe by construction: zero `inplace=True`
    across the codebase, no chained DataFrame assignment, no `applymap` /
    `DataFrame.append` / `.ix` / `iteritems`. No migration was needed.
  - Verified numerically rather than by test pass alone. The session archive's
    `bars/1m/*.csv` carry indicator columns computed under the OLD stack
    (pandas 2.3.3 + TA-Lib 0.6.8), so they serve as a regression oracle:
    recomputing `vwap / ema9 / ema20 / atr14 / rsi14 / adx14 / ±DI /
    bollinger / obv / ret5 / ret15` from raw OHLCV under the new stack over
    8 symbols × 15 columns (~8,100 bars) gives a worst relative drift of
    **9.2e-13**, confined to the `ret5`/`ret15` percentage columns — float64
    round-off, not a behaviour change. This covers the TA-Lib bump too.
  - schwabdev surface re-checked against the string-dispatch call sites: all 9
    methods the bot invokes (`account_details`, `account_orders`,
    `cancel_order`, `linked_accounts`, `option_chains`, `order_details`,
    `place_order`, `price_history`, `quote`) exist in 3.0.5, and
    `Client.__init__` still accepts `open_browser_for_auth`, which
    `SchwabConfig` depends on. `Stream.chart_equity/send/start/stop` intact.
  - 471 tests pass, `pip check` clean, all 19 shipped configs load and build
    their strategy. Not yet exercised against the live broker or the live
    TradingView endpoint — the beta dry-run this file's header calls for still
    applies before prod.

- **`config.small_cap_squeeze.yaml` declares the two new shared knobs.**
  *2026-07-27* — `min_target_rr: 1.0`, `min_stop_atr_mult: 1.5` and
  `discretionary_exit_min_r: 0.5` are now explicit, matching this preset's
  fully-explicit convention. Functionally a no-op — the dataclass defaults
  already supplied those values — but the preset should show what it runs. Its
  adaptive ladder is deliberately left alone (breakeven 1.2 / lock 1.8 /
  lock-stop 1.0 / runner 1.3): those were tuned UP for small-caps, the opposite
  direction from the top_tier retune, which was calibrated on large-cap tape.

- **`top_tier_adaptive` regime mix and profit ladder retuned from measured
  excursion.** *2026-07-27*
  - Measuring excursion in ATR units (independent of stop placement, so the
    regimes are comparable) over 2026-05-12..29: `vol_squeeze` MFE 1.45 / MAE
    0.75 ATR = **1.93** edge; `pullback` 1.65 / 2.04 = **0.81**; `trend` 1.79 /
    2.36 = **0.76**. The two sub-1.0 regimes were carrying 60% of trade volume
    (pullback alone 24 of 48). `min_trend_score` 3.5 → **4.5**,
    `min_pullback_score` 3.5 → **4.0**; `vol_squeeze` untouched at 4.0. Sized
    against the score ceilings (`_score_trend` caps at 6.0, `_score_pullback` at
    5.0, `bias_penalty` subtracts at most 1.0) so neither becomes unreachable —
    cf. `min_sr_scalp_score`, which once sat above its own ceiling and fired
    zero times ever. This cuts VOLUME, not per-trade edge: `regime_score` was
    not stamped into signal metadata during those sessions, so the score/outcome
    curve is unknown. It is stamped now, so the next dry-run can set these from
    data.
  - Every profit-protection trigger sat at or above 1.0R while **79% of trades
    (38 of 48) never reached 1R at all**, so the ladder almost never armed and
    sub-1R trades ran unprotected back to the stop (GOOG 2026-05-14 peaked at
    0.97R and closed at -0.01R; INTC 2026-05-26 0.80R → -0.02R). Excursion is
    MFE p25 0.43 / median 1.55 / p75 3.05 ATR against a typical ~3 ATR stop, so
    the median trade peaks near 0.5R. `adaptive_breakeven_rr` 1.00 → **0.60**,
    `adaptive_profit_lock_rr` 1.20 → **0.85**, `adaptive_runner_trigger_rr`
    1.10 → **0.90** (`adaptive_profit_lock_stop_rr` unchanged at 0.45).
    Breakeven now arms on 40% of trades instead of 21%. Ordering is deliberate:
    `discretionary_exit_min_r` 0.50 < breakeven 0.60 <
    `peak_giveback_low_tier_min_r` 0.70 < profit-lock 0.85 < runner 0.90 <
    `peak_giveback_min_r` 1.00.

### Removed

- **Dead config parameters.** *2026-07-27*
  - `min_score_gap` from `top_tier_adaptive` (manifest, preset, README, strategy
    README). The primary-vs-fallback selection paths it gated were collapsed
    into the flat score-ordered build queue on 2026-05-12; the code had carried
    a comment saying it was "silently ignored" ever since. Still live and read
    by `zero_dte_etf_options` / `zero_dte_etf_long_options`, which are untouched.
  - `range_target_rr` from `top_tier_adaptive` and `small_cap_squeeze` — no code
    in the package ever read it. `_build_range_signal` targets
    `range_high - buffer`, never an R multiple.

### Added

- **`small_cap_squeeze` strategy — long-only small-cap squeeze.** *2026-05-30*
  - New plugin (`_strategies/small_cap_squeeze/`): a long-only, multi-regime
    small-cap "squeeze" strategy. A dynamic TradingView screener replaces the
    fixed tradable list — float 400K-20M, change-from-open ≥5%, RVOL ≥2.0,
    volume ≥5M, price $2-20, no market-cap cap (float + price define the small
    bias). The canonical `close`/`change_from_open`/`volume` fields auto-resolve
    to premarket variants pre-09:30; float via `float_shares_outstanding_current`.
    Ranked by gap% × clipped RVOL, bias always LONG. Watchlist mode
    `premarket_lock_rth_live`: premarket gappers are sticky-locked (no pre-open
    churn), then at 09:30 a live RTH re-screen is unioned with the locked set
    (faded gappers kept warm for a VWAP reclaim), capped to max_candidates.
  - Thin subclass of `TopTierAdaptiveStrategy` (sets `strategy_name` only) — all
    behavior is the shared engine, driven by config. Regimes (narrowed 2026-06-02
    after two dry-runs): trend / momentum + opt-in `vwap_reclaim` ONLY — pullback /
    range / vol_squeeze / ORB / sr_scalp off (pullback bled both runs; the mean-
    reversion / breakout regimes don't fit a squeeze-continuation thesis). With ORB
    off the opening carve-out is removed, so the open trades the mix continuously
    08:05-11:50. No index / sector / relative-strength
    confirmation. 1m LTF with native indicators (`ltf_indicator_span_scale: 1`).
    Long-only via `risk.allow_short: false`. Premarket/extended-hours eligible.
  - Shipped preset `config.small_cap_squeeze.yaml` is a fully-explicit preset
    (every engine section declared, mirroring `config.top_tier_adaptive.yaml`).
    Beyond the strategy params it tunes: widened execution marketable-limit
    buffers (wider/faster small-cap books — live-fill only, dry-run fills at the
    natural price), zeroed the `technical_levels` soft extension penalties (a
    squeezer is always extended — the matching hard gates are off too), and
    looser SR entry clearance for buy-through-resistance breakouts. The tuning is
    a starting point pending a beta dry-run.
  - New `tests/test_small_cap_squeeze.py` (registration, config, allowed_regimes,
    vwap_reclaim scorer).

- **Shared engine: opt-in `vwap_reclaim` regime + two base flags.** *2026-05-30*
  - **`vwap_reclaim` regime** (`enable_vwap_reclaim_regime`, default `false` →
    `top_tier_adaptive` byte-for-byte unchanged). A long re-entry when price dips
    below session VWAP (a flush that shakes out weak longs) then reclaims it on a
    volume pop — the squeeze re-igniting. Catches the entry that trend (needs
    `close>VWAP` & `ema9>ema20`), pullback (needs to hold above ema20), and
    momentum (needs a new N-bar high) all miss. New `_score_vwap_reclaim` +
    `_build_vwap_reclaim_signal` (stop below the flush low, target rides toward
    the session high floored to `vwap_reclaim_target_rr`); exempt from the
    confirmation-bar gate (it enters on the reclaim bar by design). Knobs:
    `min_vwap_reclaim_score`, `vwap_reclaim_lookback_bars`,
    `vwap_reclaim_min_volume_ratio`, `vwap_reclaim_buffer_pct`,
    `vwap_reclaim_target_rr`.
  - **`disable_orb_regime`** (default `false`) — drops the ORB regime AND its
    opening-range carve-out, so the normal regime mix runs continuously from the
    open. Distinct from `disable_orb_window` (which skips the opening window and
    starts at `orb_end_time`).
  - **`extended_hours_tradable_all`** (default `false`) — treat every screened
    symbol as extended-hours eligible instead of gating on the
    `extended_hours_tradable` sublist. For dynamic-universe strategies where a
    hand-listed sublist can't enumerate the names.
  - All three default off, so `top_tier_adaptive` and every other preset are
    unchanged.

- **top_tier_adaptive: 1-minute LTF with horizon-preserving indicator scaling.** *2026-05-29*
  - The trend/pullback **LTF moved from 5m to 1m** (`ltf_minutes: 5 → 1`) so
    entries/exits act on the freshest 1m close instead of waiting up to 5 min
    for a 5m bar to print. **Behavior is preserved**, not changed: the 1m LTF's
    indicators are stretched ×5 so their wall-clock horizons match the old 5m
    frame.
  - New shared capability — `add_indicators(frame, *, span_scale=1.0)` multiplies
    every bar-count lookback (ema9→45, ema20→100, bb→100, atr14→70, ±DI/adx→70,
    obv_ema→100, rsi14→70, ret5→25, ret15→75). **Default `1.0` is byte-for-byte
    unchanged** for every existing caller. Threaded through
    `ensure_standard_indicator_frame` → `DataFeed.get_merged` (the enriched
    cache is now keyed by `span_scale`, so top_tier's scaled 1m frame never
    collides with the shared `span_scale=1.0` frame the engine bars / dashboard /
    other strategies read) → `_resampled_frame`. New `TestSpanScale`.
  - top_tier params: `ltf_indicator_span_scale: 5`; bar-count LTF lookbacks
    scaled to match (`pullback_lookback_bars` 5→25, `side_decision_recent_lookback_bars`
    6→30); warmup gates bumped (`min_bars` 90→150, `min_ltf_bars` 15→120,
    `history.required_bars` 90→150). The `range`/`vol_squeeze`/`momentum`
    regimes, `technical_levels`, and chart-pattern detection read the **base 1m
    frame** (unchanged by the switch), so their lookbacks were left as-is; the
    structure-pivot frame stays 5m. Every ATR-mult/pct/score threshold is
    unchanged (the whole point of preserving horizons).
  - Dashboard: the compact (LTF) chart redraws its EMA lines at the scaled
    spans (`ltf_ema_fast_span: 45` / `ltf_ema_slow_span: 100`, defaulting to
    base×scale), mirroring the existing HTF-chart EMA parity, so the chart
    matches what the bot evaluates.

- **top_tier_adaptive: true Opening Range Breakout (ORB) regime.** *2026-05-29*
  - The opening window used to run the **trend** regime with ~11
    `orb_bypass_*` flags loosening its filters — there was no opening range
    computed at all (the "breakout" was a rolling 5-bar-high). Replaced with a
    real ORB regime:
    - The **opening range** = high/low of the first `orb_range_minutes` of RTH
      (default 15 → 09:30-09:45), computed on the raw 1m frame.
    - **No entries while the range forms** (09:30 → range-end); from range-end
      → `orb_end_time` the ORB regime is the ONLY regime, trading a break.
    - **Entry:** close breaks the range edge by `orb_breakout_buffer_atr_mult`
      × ATR. **Stop:** the OPPOSITE range edge. **Target:** a measured move
      (range height × `orb_target_range_mult`, default 1.5×), then capped to
      HTF levels / floored to min R:R by the shared finalize path.
    - Range size is sanity-bounded (`orb_min_range_atr_mult` /
      `orb_max_range_atr_mult`) so noise ranges and untradeable wide ranges
      are skipped. Score floor `min_orb_score` (default 3.5).
  - New scorer `_score_orb` + builder `_build_orb_signal` + helper
    `_opening_range`; wired into the regime scoring/selection/dispatch and
    `_allowed_regimes` (ORB window now returns `{"orb"}`, not `{"trend"}`).
    The `orb_bypass_*` flags still apply (they loosen the shared finalize
    filters that are stale at the open) and `disable_orb_window` still skips
    the whole open. Regression tests in `TestORBRegime`.

- **top_tier_adaptive extended-hours trading (07:00-20:00 ET).** *2026-05-28*
  - New opt-in `runtime.equity_session_indicator_window: "rth" | "extended"`
    (default `"rth"`). In `"extended"`, `add_indicators` anchors the
    per-session VWAP/EMA reset to the 07:00-20:00 equity-stream window (via
    `is_equity_stream_session`) instead of RTH 09:30-16:00, so pre/post-market
    bars carry meaningful session indicators. Every RTH-only strategy/preset
    is byte-for-byte unchanged (verified: default mode still resets VWAP at
    09:30; 219+ regression tests green).
  - `_session_open_price` gained a `session_start` anchor; top_tier's
    `day_strength` bias keys off the 07:00 open in extended mode (matching the
    VWAP reset) via `_day_strength_session_open`.
  - `_allowed_regimes` opens the full non-ORB regime mix pre-market (<09:30)
    in extended mode; ORB stays RTH-anchored (range-end → orb_end). After-RTH
    is covered by the afternoon window once `no_new_entries_after` is extended.
  - Extended-hours universe gate (`params.extended_hours_tradable`): outside
    RTH only the configured liquid names may enter (thinner names trade RTH
    only); empty list => no extended-hours entries.
  - The shipped preset `config.top_tier_adaptive.yaml` defaults to **RTH-only**
    (`equity_session_indicator_window: rth`, entry window `09:45-15:00` — entries
    open when the ORB regime can first fire — management `09:30-15:55`,
    `no_new_entries_after: 15:00`). The `extended_hours_tradable` list is retained
    but inactive in RTH mode. To run extended hours, set the mode to `extended`
    and widen the entry/management/screener windows (e.g. 07:00-19:30/19:55/19:45,
    `no_new_entries_after: 19:30`).

### Removed

- **top_tier_adaptive: dead `orb_bypass_*` params (5).** *2026-05-29* Now that
  the ORB window runs the dedicated `orb` regime (not trend-with-bypasses),
  `orb_bypass_index_confirmation`, `orb_bypass_entry_confirmation_bar`,
  `orb_bypass_stretched_filter`, `orb_bypass_oversized_entry_bar`, and
  `orb_bypass_tech_bias_contradiction` were dead — their gates are keyed to
  regime sets (`{trend,pullback,vol_squeeze,momentum}` / `{...,sr_scalp}`) that
  exclude `orb`, and those regimes no longer run in the ORB window, so the
  bypasses never fired. Removed from `strategy.py` (vars + the `and not
  orb_*_bypass` clauses), `config.top_tier_adaptive.yaml`, `manifest.json`, and
  the README. Surviving ORB bypasses (`htf_bias`, `exhaustion`,
  `structure_entry`, `sr_entry`, `screener_bias`, `side_decision`,
  `relative_strength`) DO apply to the ORB regime and were kept.

### Fixed

- **Dashboard HTF chart now draws the EMA 50/200 the bot actually uses (top_tier_adaptive).** *2026-05-28*
  - The HTF trend context computes EMA 50/200 on the 15m frame
    (`_default_htf_context_for_score` hardcodes `ema_fast_span=50,
    ema_slow_span=200`), but the preset didn't set
    `htf_ema_fast_span`/`htf_ema_slow_span`, so the dashboard HTF chart fell
    back to ema9/ema20 (fast EMAs of the 15m bars) — misrepresenting the HTF
    trend the bot evaluates. Added `htf_ema_fast_span: 50` /
    `htf_ema_slow_span: 200` to the preset so the HTF chart EMA override
    (`DashboardCache.chart_payload`) renders 50/200. Chart-only — entry logic
    is unchanged (top_tier's HTF direction is structure-based via
    `require_htf_bias_alignment`, not an EMA cross; the 50/200 `htf_trend_bias`
    is recorded as context but not gated on).

- **Dashboard TradingView ticker deep-links were broken for top_tier_adaptive.** *2026-05-28*
  - Ticker chips link to `tradingview.com/symbols/<EXCHANGE>-<SYMBOL>/`.
    Two causes left `<EXCHANGE>` wrong for the entire top_tier universe:
    - `top_tier_adaptive/screener.py` was the ONLY equity screener that did
      not `select("exchange")`, so its candidates carried no exchange and
      the dashboard fell back to the live Schwab quote.
    - `dashboard_quote_exchange` read Schwab's single-letter `exchange`
      code (`"q"`/`"n"`/`"a"`/`"p"`) BEFORE the full `exchangeName`
      (`"NASDAQ"`/`"NYSE"`). Single letters aren't valid TradingView
      exchanges (not in `_EXCHANGE_ALIASES`), so the builder produced
      `symbols/N-XOM/` (404) for NYSE names; NASDAQ names (`"q"` → `''`
      in the front-end guard) got no link at all.
  - Fix: top_tier screener now selects `exchange` (matching every other
    equity screener — `_row_metadata` carries it straight into
    `metadata["exchange"]`); `dashboard_quote_exchange` now prefers the
    full `exchangeName`/`primaryExchangeName` over the single-letter code,
    falling back to the short code only as a last resort.
  - Verified: `n+NYSE→NYSE`, `q+NASDAQ→NASDAQ`, `p+NYSE Arca→AMEX`,
    `a+NYSE American→AMEX`, `q+"NASDAQ Global Select"→NASDAQ`; simulated
    front-end URLs resolve to `symbols/NYSE-XOM/` and `symbols/NASDAQ-NVDA/`.
    New regression coverage in
    `tests/test_bug_regressions.py::TestTradingViewExchangeLinks2026_05_28`.

- **Dashboard structure overlay now mirrors the strategy's LTF structure params.** *2026-05-27 PM*
  - `DashboardCache.current_structure_overlay` built its CHoCH/BOS/EQH/EQL
    annotation with base params (`pivot_span`, no `min_pivot_gap_bars`,
    base `pct_tolerance`) regardless of timeframe, so after Fix B/D the
    LTF chart showed denser, noisier pivots than the bot actually acts on.
  - Now, when the overlay's display timeframe equals the strategy's
    effective LTF structure timeframe (`structure_ltf_timeframe_minutes`,
    falling back to `params.ltf_minutes`), it applies the same LTF
    overrides the strategy uses: `structure_ltf_pivot_span`, the 0.60x
    `pct_tolerance`, and `structure_min_pivot_gap_bars`. HTF / other
    timeframes keep the original base-param behavior unchanged. Mirrors
    the existing "keep the chart faithful to the strategy" precedent (the
    HTF-EMA override in the chart payload).
  - Verified: on NVDA 5/27 the 5m overlay bias now matches the bot's LTF
    structure read; the 15m overlay is byte-for-byte unchanged. Inherent
    residual: a 1m chart can't match (the bot no longer computes 1m
    structure under Fix D) — only the LTF (5m) and HTF (15m) charts do.

### Changed

- **Market structure: minimum pivot-gap filter (Fix B) + LTF-frame resampling (Fix D).** *2026-05-27 PM*
  - The pivot detector (`_reduced_pivots`) merged consecutive same-kind
    pivots but had NO rule against an alternating H↔L registering 1-2
    bars apart. On the raw 1m LTF structure frame with a 2-bar fractal,
    that produced a new "swing" every ~5 min (≈⅓ of pivots within 2 min
    of each other on NVDA), churning the HH/LH/EQH/LL/HL/EQL labels and
    the BOS/CHoCH/structure-exit signals keyed on them. The swings were
    genuine (median 1.9-5.2 ATR), so the problem was temporal density,
    not amplitude.
  - **Fix B** — new `structure_min_pivot_gap_bars` (default `0` = off).
    When > 0, an alternating pivot closer than N bars to the prior kept
    pivot is skipped as noise within the current leg. Added to
    `analyze_market_structure` and threaded from `_structure_context`.
  - **Fix D** — new `structure_ltf_timeframe_minutes` (default `0` = off).
    When > 0, `_structure_context` resamples the LTF structure frame to
    that timeframe before pivot analysis, so structure tracks the bars
    the strategy trades (params.ltf_minutes) instead of the 1m stream.
    Entry, exit, and HTF-alignment paths all pick it up.
  - Both default OFF, so every strategy that doesn't set them keeps the
    exact prior behavior. top_tier_adaptive opts in:
    `structure_min_pivot_gap_bars: 3`, `structure_ltf_timeframe_minutes: 5`,
    and the companion `structure_event_lookback_bars: 8 → 4` (bar-based
    settings now count 5m bars; halved to keep BOS/CHoCH event freshness
    near ~20 min instead of 40).
  - Measured on NVDA 5/27 through the real code path: 92 → 8 structure
    pivots, and the bias resolved from "neutral" (conflicted HH/EQL on
    noise) to a clean "bullish" HH/HL read. Watch top_tier's first 2-3
    sessions — structure feeds entry bias, structure exits, and HTF
    alignment simultaneously.

- **top_tier_adaptive: lowered min_sr_scalp_score 4.0 → 3.0 (sr_scalp was structurally dead).** *2026-05-27 PM*
  - `_score_sr_scalp` has a theoretical max of 5.0 but an empirical
    ceiling of **3.9** across 13.2k observed cycles (the +1.5
    rejection-wick component rarely co-occurs with all three
    neutral/chop components). The preset's `min_sr_scalp_score: 4.0`
    was therefore *unreachable* — the regime qualified 0 times and
    fired 0 entries from its 2026-05-12 introduction through 2026-05-27.
  - Lowered the manifest default (3.5 → 3.0) and the preset (4.0 → 3.0)
    so the regime can actually qualify and its real-world edge can be
    evaluated. Documented the 3.9 ceiling in the README so the
    threshold isn't set above it again.
  - Note: a separate live runtime `config.yaml` (e.g. on the
    user-managed H: deployment) carries its own `min_sr_scalp_score`
    and must be updated independently.
- **top_tier_adaptive: static-analysis cleanup.** *2026-05-27 PM*
  - `_recent_momentum_pct`, `_entry_bar_confirms`, `_pullback_leg_context`
    converted to `@staticmethod` (no instance state used). Call sites
    via `self.` are unaffected.
  - `_peak_giveback_triggered` low-tier guard simplified to a single
    chained comparison `0.0 < low_tier_min_r <= peak_r < min_r`.

- **top_tier_adaptive: tightened main-tier peak-giveback retain fractions.** *2026-05-27 PM*
  - `RiskManager._peak_giveback_floor_r` retain fractions raised from the
    hardcoded 0.50/0.60/0.70 (1-2R / 2-3R / 3R+ tiers) to configurable
    0.65/0.72/0.78 via new `RiskConfig.peak_giveback_retain_1to2r` /
    `_2to3r` / `_3r_plus`. `_peak_giveback_floor_r` changed from a
    `@staticmethod` to an instance method to read the config.
  - Rationale from intra-trade R-path reconstruction (1m bars, 35
    trades 5/12-5/27): winners captured only **44% of their MFE**
    ($433 realized vs $978 of peak favorable excursion), and made NO
    new highs after their interim peak in-sample — so the loose floors
    were donating realized gains back to the market rather than
    protecting runner upside. Worst cases: AVGO captured 30% of a
    2.31R peak, COP 22-28%, META 29%.
  - Modeled effect: +3.7R additional capture across 8 winners (~$465
    at full size), no winners clipped (none recovered post-peak in
    the sample). At a 2R peak the floor now sits at 1.3R (was 1.0R);
    at 2.5R it sits at 1.8R (was 1.5R).
  - Risk acknowledged: tighter floors exit sooner on a retrace, so a
    future trade that dips into the [old-floor, new-floor] band and
    then recovers to a bigger peak would be clipped. The fractions are
    configurable — tune down if runner-clipping shows up in live data.
  - Updated `TestPeakGivebackFloor` assertions for the new fractions.

### Fixed

- **top_tier_adaptive: soft bias penalty no longer second-guesses the explicit side decision.** *2026-05-27 PM*
  - When `require_explicit_side_decision` makes a pick, `_decide_side`
    has already chosen the side from current-action signals (recent
    return, VWAP, EMA, bar direction). The soft `_bias_penalty` then
    ran on that decided side and docked its regime scores when the
    side disagreed with `effective_bias` (the chg_open-derived bias).
    On a reversal setup — stock down on the day but recovering, where
    Fix A correctly picks LONG — the penalty could push the LONG
    score below `min_pullback_score` and skip the exact entry Fix A
    was built to catch. Now the penalty is skipped entirely when an
    explicit side decision was made this cycle; it remains the sole
    bias mechanism when `require_explicit_side_decision: false`.

### Removed

- **top_tier_adaptive: dead-code cleanup after Fix A + B.** *2026-05-27 PM*
  - Removed the hard screener-bias veto block from `entry_signals`
    (and its `screener_bias_counter_to_tradable_sides` skip reason).
    Fix A's explicit side decision uses current-action signals to pick
    side; re-overriding that with the screener's session-time bias
    (which can be minutes stale) re-introduced the backward-looking
    decision Fix A was designed to replace. `respect_screener_bias`
    param remains — still used by the soft-penalty / trailing-bias
    fallback path when `require_explicit_side_decision: false`.
  - Removed the recent-momentum disagreement gate (Fix E from
    earlier today). Fix A's vote #1 IS the recent-return signal (at
    0.1% threshold). Fix E hard-blocked on the same signal (at 0.3%
    threshold). After Fix A narrows `preferred_sides` to one side,
    Fix E was a no-op in every case except an unreachable edge.
    Removed params: `recent_momentum_lookback_bars`,
    `recent_momentum_disagree_threshold_pct`,
    `orb_bypass_recent_momentum`. The `_recent_momentum_pct` helper
    stays — Fix A's `_decide_side` still uses it.
  - Removed the pullback-bounce-confirmation gate from
    `_build_pullback_signal`. Fix B (confirmation-bar) does the same
    check more reliably on the LAST FULLY CLOSED bar (not the
    in-progress bar) and applies it to all direction-following
    regimes uniformly. Removed params:
    `pullback_require_bounce_confirmation`,
    `pullback_bounce_close_position_min`.
  - Net: -3 gates per cycle, same coverage, single source of truth
    for side selection (`_decide_side`) + post-decision filters
    (RS, pullback maturity, stretched cooldown, confirmation bar).

### Added

- **top_tier_adaptive: explicit side decision + confirmation-bar entry (Fix A + B).** *2026-05-27*
  - **Fix A — Explicit side decision before regime scoring.** Replaces
    the implicit "evaluate both sides per regime, pick highest score"
    with an evidence-based vote across CURRENT price-action signals.
    The old approach could pick SHORT just because the SHORT regime
    score was 0.5 higher even when every meaningful current-action
    signal said LONG. New `_decide_side(ltf, close, vwap, ema9, ema20)`
    votes across: (1) recent return over `side_decision_recent_lookback_bars`
    (default `6` = 30 min at 5m), threshold
    `side_decision_recent_threshold_pct` (default `0.1`); (2) close
    vs session VWAP with `side_decision_vwap_buffer_pct` dead-band
    (default `0.0005`); (3) EMA9 vs EMA20; (4) last 3 LTF bars'
    green-count. Side wins when votes ≥ `side_decision_min_votes`
    (default `3`) AND opposing ≤ `side_decision_max_opposing`
    (default `1`). Mixed → skip the candidate. The wrong side is
    never evaluated, so all downstream filters only see the decided
    side. Bypassed during ORB via `orb_bypass_side_decision: true`
    (early-session signals are gap-dominated). Disable with
    `require_explicit_side_decision: false`.
  - **Fix B — Confirmation-bar entry trigger.** Companion to Fix A.
    For direction-following regimes (trend / pullback / momentum /
    vol_squeeze), the LAST FULLY CLOSED LTF bar must confirm direction
    before `_build_<regime>_signal` is called: green AND > prior close
    for LONG (mirror for SHORT). Catches single-bar fakeouts where the
    in-progress bar tipped a score threshold but the actual completed
    bar didn't carry the move. Range and sr_scalp regimes are EXEMPT
    — both are mean-reversion theses where the last closed bar moves
    AGAINST the entry direction by design. New `_entry_bar_confirms`
    helper reads `ltf.iloc[-2]` (last closed) and `ltf.iloc[-3]`
    (prior closed). Bypassed during ORB via
    `orb_bypass_entry_confirmation_bar: true`. Disable with
    `require_entry_confirmation_bar: false`.
  - Modeled 5/27 effect: 3 of 6 trades skipped (NVDA SHORT — votes
    1L/2S mixed, NEM LONG — votes 2L/2S tied, COP LONG — votes 2L/0S
    below min_votes=3). 3 enter (NFLX LONG +$67 winner, CVX SHORT
    −$64 strong consensus, DOW SHORT −$41 strong consensus). Net
    P&L: −$38.14 vs −$135.55 original (−71.9% loss reduction). Trade
    count cut in half. The two unblocked losers had unanimous SHORT
    votes — they lost from post-entry reversals, not pre-entry
    direction errors.

- **top_tier_adaptive: recent-momentum disagreement gate (Fix E).** *2026-05-27*
  - The strategy's entire bias chain (`screener.directional_bias`,
    `live_bias`, `trailing_bias`, soft `_bias_penalty`, relative-strength
    gate) derives from `change_from_open` — a session-wide, backward-
    looking quantity. On stocks that have reversed intraday, day_strength
    still reflects the original direction while recent price action has
    flipped. Forensic of 5/27 NVDA SHORT @ 12:36: chg_open −1.66% (all
    signals SHORT), MSHTF bearish, XLK confirmed SHORT — but NVDA had
    bounced 21% off the 11:00 low; bot shorted into the recovering tape.
  - New `_recent_momentum_pct(ltf, lookback_bars)` helper returns the
    percent change between the LTF frame's current close and the close
    `lookback_bars` bars earlier. Returns `None` when there aren't
    enough bars or the prior close is non-positive.
  - New gate in `entry_signals` after the relative-strength filter:
    when `recent_pct >= recent_momentum_disagree_threshold_pct`, SHORT
    is removed from `preferred_sides`; when `recent_pct <= -threshold`,
    LONG is removed. If `preferred_sides` becomes empty, the candidate
    is skipped with reason `recent_momentum_disagrees_local_(up|down)(...)`.
    Defaults: `recent_momentum_lookback_bars: 6` (= 30 min at
    `ltf_minutes: 5`), `recent_momentum_disagree_threshold_pct: 0.3`
    (above typical 5m bar noise ~0.1-0.2% on mega-caps, catches
    reversals not random ticks). `orb_bypass_recent_momentum: true`
    skips the gate during 09:35-`orb_end_time` (early-session momentum
    is gap-dominated and not informative).
  - Defaults are conservative — dry-run on 5/27 showed no winners
    blocked and no losers blocked either at 30 min / 0.3% (today's
    losers had local momentum AGREEING with the losing side at entry;
    they failed via post-entry reversals, not pre-entry disagreement).
    Tune `recent_momentum_lookback_bars` longer (e.g. 12 = 60 min) to
    catch slower reversals like NVDA's 90-min bounce, at the cost of
    a more aggressive filter that may block legitimate trades on
    other days.

- **top_tier_adaptive: pullback maturity check (Fix C).** *2026-05-27*
  - New `_pullback_leg_context(side, session_ltf, current_close, ltf_minutes)`
    helper on `TopTierAdaptiveStrategy` returns `(minutes_since_extreme,
    retrace_pct)` for the side's session extreme. For LONG: extreme =
    session high, anchor = lowest low at-or-before the high bar,
    `leg_size = high − anchor`, `retrace_pct = (high − current_close) /
    leg_size`. Mirror for SHORT. `minutes_since_extreme` is estimated as
    `bars_since_extreme * ltf_minutes` (LTF bar grid is uniform within
    the session, avoids per-bar timestamp arithmetic). Returns
    `(None, None)` when context can't be computed reliably (single-bar
    session, non-positive leg_size).
  - New `_build_pullback_signal` gate at the top of the build: when
    `pullback_require_fresh_leg` (default `true`), reject when BOTH
    `minutes_since_extreme > pullback_max_minutes_since_session_extreme`
    (default `45.0`) AND `retrace_pct > pullback_max_leg_retrace_pct`
    (default `50.0`). AND-logic on purpose: fresh-but-deep retracements
    and old-but-shallow ones both still trade. Targets the 5/27 NEM
    pattern (LONG at 14:48, session high at 07:45 = 400 min stale,
    retracement 140% off the peak — the entire leg gone) which the
    other entry-quality gates didn't catch.
  - Modeled 5/27 effect: blocks NEM (-$36.75 saved). NFLX (+$67) and
    COP (+$17) winners pass cleanly (25 min / 34% and 45 min / 38%
    respectively — neither condition triggered). Holds NVDA SHORT and
    DOW SHORT as "stale but shallow" (95 min / 21% and 255 min / 26%);
    those need a different signal.

- **top_tier_adaptive: four entry-quality gates + low-tier peak-giveback.** *2026-05-26*
  - **Hard screener-bias veto.** Gated by the existing `respect_screener_bias`,
    skips a candidate entirely when `c.directional_bias` is set and lies
    outside `preferred_sides` (e.g., screener=SHORT with `allow_short:
    false`). The Fix A soft penalty (section 6a) drags counter-bias scores
    down but doesn't outright filter weak-bias setups; on 5/26 the per-cycle
    `bias_pen` ranged 0.14-0.21 and gated nothing, despite the screener
    flagging 3,959 SHORT vs 2,533 LONG candidates that day.
  - **Relative-strength gate.** New manifest knobs
    `relative_strength_block_threshold_pct` (default `0.5`) and
    `orb_bypass_relative_strength` (default `true`). Filters
    `preferred_sides` based on `(candidate.day_strength −
    sector_ETF.day_strength)`. Catches stocks under-performing their
    sector — 5/26 INTC at +0.21% vs XLK +1.27% (rel −1.06%, lost $3),
    INTC at 14:04 at +0.10% vs XLK +0.90% (rel −0.80%, lost $64), NEM at
    −0.19% vs XLB +0.67% (rel −0.86%, lost $10). META (the lone winner)
    had rel +0.25% and passes the gate.
  - **Stretched-cooldown hysteresis.** New `stretched_cooldown_minutes`
    (default `3.0`). The `reject_stretched_entries` thresholds
    (`stretched_percent_b_max`, `stretched_atr_mult_max`) are crisp — a
    single tick across relaxes them while the structural condition is
    still active. AMZN on 5/26 was rejected at 10:11:41 with pct_b=0.851
    then entered 46 s later as the close ticked back across (lost $28).
    The cooldown stamps the failure timestamp and rejects subsequent
    checks within the window. Per-symbol regardless of side.
  - **Pullback bounce confirmation.** New `pullback_require_bounce_confirmation`
    (default `true`) and `pullback_bounce_close_position_min` (default
    `0.5`) in `_build_pullback_signal`. Requires the entry bar's close
    to be in the directional half (close_pos ≥ threshold for LONG; ≤
    1-threshold for SHORT) AND on the favorable side of the prior 5m
    bar's close. Blocks the "pullback that's actually a rollover" pattern
    — FCX on 5/26 entered at 10:12:26 with the in-progress 5m bar
    showing close_pos 0.186 (close in the bottom 19% of the bar's
    range), then never bounced and lost $44.
  - **Low-tier peak-giveback.** Three new `RiskConfig` fields:
    `peak_giveback_low_tier_enabled` (default `True`),
    `peak_giveback_low_tier_min_r` (default `0.7`),
    `peak_giveback_low_tier_giveback_frac` (default `0.7`). Catches the
    0.7-1.0R MFE "purgatory" trades that round-trip to BE / fixed stop
    before the main tier (`peak_giveback_min_r: 1.0`) arms. Uses a
    fixed giveback fraction (not the main tier's peak-size-dependent
    ladder) because at sub-1R peaks the run-vs-noise signal is weaker.
    At 0.9R peak with default 0.7 frac, exits when current_r ≤ 0.27R.
    Skipped when the high-conviction `peak_giveback_min_r_override` is
    active (those trades want the wider main-tier leash).
  - Modeled 5/26 effect: 5 of 7 trades blocked (NEM, INTC×2 by RS gate;
    AMZN by stretched cooldown; FCX#1 by pullback bounce), 2 enter
    (META +$10.40 winner, FCX#2 -$38.87 residual). Net P&L: -$28.47 vs
    -$177.66 original (-84% loss reduction).

### Changed

- **top_tier_adaptive: `bias_penalty_saturate_at` 2.0 → 0.75 (manifest default).** *2026-05-26*
  - The original 2.0 saturation assumed daily moves regularly hit ±2%;
    intraday reality on most days is 0.3-1.0%, where the penalty
    produced was 0.15-0.50 — not enough to filter `min_pullback_score:
    3.5` candidates with raw scores 3.75+. At 0.75, a -1% day applies
    full 1.0 penalty (was 0.5); a -0.5% day applies 0.67 (was 0.25).
  - The `config.top_tier_adaptive.yaml` HIGH_VOL preset still overrides
    to 2.5 (high-vol days produce 2-3% day_strength regularly, where
    the manifest default would saturate too fast).

- **0DTE option strategies: option_chain_cache_seconds 4 → 60.** *2026-05-21*
  - Both 0DTE configs previously overrode the default 6s chain-cache
    TTL down to 4s — almost every entry cycle re-fetched the chain.
    For a 6-hour session running 3 underlyings × ~100-200 entry cycles
    that's 300-600 Schwab `option_chains()` calls.
  - The chain content the strategy reads (strike list, OI, volume
    buckets, deltas) doesn't materially change in 60s. Post-selection
    leg quotes get refreshed independently via `fetch_quotes` (still
    on the 4-6s `quote_cache_seconds` TTL) plus the per-build stability
    check loop, so entry pricing freshness is unchanged.
  - Bumped both 0DTE yamls to 60s, with the rationale documented
    inline. Estimated savings: 60-150 redundant chain fetches per
    session (15-25% reduction in option-chain API load).
  - Other audit findings (per-position force=True quote re-fetches,
    dual quote calls between position_manager + execution) were
    considered but not changed: the force pattern exists for fresh
    stop/target evaluation and stale leg quotes have tail risk, and
    the dual-fetch overlap is bounded to fill events (rare). Worth
    revisiting after a few sessions of observation if API load is
    still the binding constraint.

- **Scaffold generator: plugin-type-aware templates aligned with today's contract.** *2026-05-19*
  - `scripts/scaffold_strategy_plugin.py` had drifted from the current
    plugin contract in four places:
    1. Scaffolded **equity** yamls inherited a dead `options:` block
       from the `config.example.yaml` template — directly undoing the
       same-day cleanup that stripped that block from all 14 existing
       equity yamls.
    2. `--plugin-type=option` produced an equity-style screener (TV
       `Query().where(...rvol >= min_rvol)`) instead of the
       local-synthesis pattern both real 0DTE strategies use.
    3. `--plugin-type=option` produced an equity-style `entry_signals`
       that read `candidate.metadata.relative_volume_10d_calc` (zero
       under local synthesis) and didn't include
       `live_activity_score` / `dashboard_directional_bias` public
       hooks — a new option strategy would render with the stub-33%-red
       dashboard ring until manually wired.
    4. Manifest's `capabilities.dashboard.tradable_symbols_source`
       hardcoded `"params.symbols"` for both plugin types — wrong for
       option strategies (should be `"options.underlyings"`).
  - Refactor: split the templates into stock- and option-specific
    variants. `_stock_strategy_py` / `_option_strategy_py`,
    `_stock_screener_py` / `_option_screener_py`,
    `_stock_manifest_params` / `_option_manifest_params`,
    `_stock_manifest_capabilities` / `_option_manifest_capabilities`,
    `_full_config_yaml(name, plugin_type)` strips the `options:` block
    from stock-type scaffolds and keeps it for option-type.
  - Option-type template produces:
    - Local-synthesis screener (mirrors `zero_dte_etf_options/screener
      .py` — synthesizes candidates from `config.options.underlyings`,
      no TV call).
    - Strategy class with `live_activity_score(frame)` and
      `dashboard_directional_bias(frame)` public hooks stubbed with
      the same fail-open + threshold-based pattern the real 0DTE
      strategies use.
    - Manifest with `tradable_symbols_source: options.underlyings`,
      watchlist `active_sources` declaring all the standard option
      sources (`options.underlyings`,
      `options.confirmation_symbols`, `options.volatility_symbol`,
      plus position-metadata descriptors keyed to the new strategy
      name), and quote sources for volatility + valuation legs.
    - YAML scaffolded with the `options:` block kept (option
      strategies actively need it).
  - Stock-type template unchanged in shape; only the YAML generation
    drops the `options:` block.
  - Fixed a latent encoding bug in `Path.write_text(content)` —
    without `encoding="utf-8"` Windows writes em-dashes as cp1252
    bytes (0x97), then py_compile reads as UTF-8 and chokes. New
    option template uses em-dashes in its docstrings; explicit
    UTF-8 encoding now applied to all write_text + read_text calls.
  - Smoke-test asserts: both scaffolds compile, both manifests are
    valid JSON, both yamls are valid YAML, all 12 contract checks
    pass (options-block presence, manifest source declarations,
    public hook presence, local-synthesis vs TV-query).

- **Plugin abstraction: live-publish hooks promoted to public + duck-typed dispatch.** *2026-05-19*
  - The candidate dashboard-publish resolver (added earlier today) had
    a polymorphism leak: `engine._publish_state` gated the live
    activity-score / directional-bias compute behind an
    `is_option_strategy(self.config.strategy)` type check, then used
    `getattr(self.strategy, '_live_activity_score', None)` against
    underscore-prefixed (private) method names. Two issues:
    1. The type check restricted the feature to option strategies even
       though there's no semantic reason an equity strategy couldn't
       provide live overrides if its screener can't populate real
       values at screen time.
    2. Private (underscore-prefixed) method names are not appropriate
       for plugin extension points — `BaseStrategy`'s other public
       hooks (`signal_priority_key`, `dashboard_level_context_spec`,
       `dashboard_candidate_label`, etc.) all use public names.
  - Clean break:
    - Renamed `_live_activity_score` → `live_activity_score` and
      `_dashboard_directional_bias` → `dashboard_directional_bias` on
      `ZeroDteEtfOptionsStrategy` (long-options strategy inherits).
    - Updated internal call site in `_regime_confirm` and all docstring
      / comment references in strategy.py, screener.py, mobile.js, and
      the two strategy READMEs.
    - Dropped `is_option_strategy` from `engine.py` imports and from
      the publish block. `engine._publish_state` now resolves the
      hooks via pure `getattr(self.strategy, 'method_name', None)` —
      any strategy that defines these methods opts into live publish
      automatically, no plugin-type dispatch needed.
  - Documented the new hooks in `_strategies/README.md` under the
    "extension hooks" bulleted list, describing return contracts,
    fail-open semantics, and the duck-typed dispatch model.
  - Net result: option vs equity asymmetry is now SOURCE-ONLY (where
    the activity_score / directional_bias come from — screener time
    vs publish time) rather than DISPATCH (engine doesn't know or
    care which is which).

### Removed

- **Equity strategy configs: drop dead `options:` block.** *2026-05-19*
  - 14 equity-strategy YAML configs each carried a ~54-line `options:`
    block that no equity strategy reads. Validation
    (`config.py:1308`) only requires `options.underlyings` when
    `is_option_strategy(strategy)` is true; every
    `self.config.options.*` reader in the codebase is gated behind an
    option-strategy check (risk.py:226, engine.py:296,
    execution.py option order paths, position_manager.py option
    quote freshness, entry_gatekeeper.py option-chain validation,
    plus the 0DTE strategy + screeners themselves). So for equity
    strategies the block was pure noise.
  - Cleaned `configs/`:
    - `config.closing_reversal.yaml`, `config.mean_reversion.yaml`,
      `config.opening_range_breakout.yaml`,
      `config.momentum_close.yaml`, `config.pairs_residual.yaml`,
      `config.rth_trend_pullback.yaml`,
      `config.volatility_squeeze_breakout.yaml`,
      `config.peer_confirmed_htf_pivots.yaml`,
      `config.peer_confirmed_key_levels.yaml`,
      `config.peer_confirmed_key_levels_1m.yaml`,
      `config.peer_confirmed_trend_continuation.yaml`,
      `config.microcap_pm_breakout.yaml`,
      `config.microcap_gap_orb.yaml`,
      `config.top_tier_adaptive.yaml`.
    - Net `783 lines` of dead config removed (most blocks were 54
      lines; `microcap_gap_orb` had 79 because it carried all the
      newer optional features like `options_breakeven_*` and
      `delta_time_shift_*`; `top_tier_adaptive` had 57).
  - Kept intentionally: `configs/config.yaml` (runtime template — you
    might switch the active strategy at runtime) and
    `configs/config.example.yaml` (documentation example demonstrating
    every block).
  - Loader tolerance verified: `config.py:1270` uses
    `raw.get("options", {})` default, then `ZeroDteOptionsConfig(**{})`
    constructs from dataclass field defaults. Smoke-loaded each
    cleaned config to confirm `cfg.options.underlyings` resolves to
    the default `['SPY', 'QQQ']` and other fields to their dataclass
    defaults (e.g. `max_vix=22.5`). The 0DTE configs continue to
    surface their YAML-supplied values (e.g. `max_vix=24.0` for the
    credit-spread parent).

- **0DTE option strategies: dead RVOL pipeline + stub metadata.** *2026-05-19*
  - The legacy RVOL pipeline (`min_candidate_rvol`, `trend_rvol`,
    `credit_min_rvol`, `credit_max_rvol`, plus the derived
    `candidate_rvol`, `candidate_effective_rvol`,
    `candidate_rvol_profile`, `min_candidate_rvol_required`) was
    superseded by `_live_activity_score` on 2026-05-14 but the dead
    code, dead config keys, and dead metadata-stamped fields were
    left in place "for visibility". After the local-synthesis
    screener switch (2026-05-19) the rvol values became stub-only
    (always `1.0`) — the visibility argument no longer held.
  - Clean break removals (no aliases, no fallback shims):
    - `strategy.py`: 4 dead computations (lines 555-557, 583-584) and
      4 dead metadata-dict entries (lines 922-925), plus the unused
      `rvol_profile_for_symbol` import.
    - Both manifests: `min_candidate_rvol`, `trend_rvol`,
      `credit_min_rvol`, `credit_max_rvol` removed from `params`.
    - Both YAML configs: same 4 keys dropped (plus the trailing
      "NO LONGER GATING" inline comments).
    - Both READMEs + project root README: param tables + "RVOL / tape
      filters" sections updated to describe the live-activity-score
      thresholds (`min_activity_for_entry`, `trend_activity_threshold`,
      `credit_activity_min`, `credit_activity_max`) that actually
      drive the gates.
  - 0DTE screeners: dropped the stub `change_from_open: 0.0` and
    `relative_volume_10d_calc: 1.0` from candidate metadata. No
    downstream consumer reads them anymore (strategy uses `u_day_ret`
    and `_live_activity_score(frame)` directly).
  - `_live_activity_score` got an `frame.empty` check for parity
    with `_dashboard_directional_bias` — functionally equivalent
    (len(empty) == 0 < 20 was already safe), but consistent.

### Changed

- **0DTE option configs: audit pass for liquidity, VIX, and HTF gates.** *2026-05-14*
  - Found three mis-tuned areas after systematic param diff between
    `config.zero_dte_etf_long_options.yaml` and the credit-spread parent
    `config.zero_dte_etf_options.yaml`:
  - **VIX caps were inverted**: long-options had `max_vix: 25.0` while
    parent had `22.5`. Long premium suffers MORE in high VIX (expensive
    entry + vega risk on IV crush) so it should have a LOWER cap.
    Swapped to enforce the `long_max <= credit_max` hierarchy:
    - long_options `max_vix: 25.0 → 22.0`
    - credit-spread parent `max_vix: 22.5 → 24.0`
  - **Liquidity gates were too loose** for 0DTE quality. SPY/QQQ ATM
    0DTE typically trades 5k-20k contracts/day with OI 10k-50k+ and
    bid-ask spreads of 1-3%. Old defaults (vol 500, OI 900, spread 7%)
    admitted illiquid OTM strikes with poor fills. Tightened in BOTH
    configs:
    - `min_option_volume: 500 → 1000`
    - `min_open_interest: 900 → 2000`
    - `max_bid_ask_spread_pct: 0.07 (7%) → 0.04 (4%)`
  - **`htf_lookback_days: 60`** on long-options was excessive. At 15-min
    HTF that's ~1500 bars — way more than needed for HTF structure
    detection. Reduced to 15 (matching parent) for ~390 bars.

### Added

- **Session report: regime-call outcome tracker.** *2026-05-21*
  - New `manifest.json -> regime_call_outcomes` block embedded in
    every per-day session archive. Classifies each
    `ambiguous_regime` decision against the 30-min forward price
    move and aggregates by outcome + hour, so a day-over-day
    comparison surfaces drift in regime-scoring quality without
    anyone running ad-hoc post-mortems.
  - Classifier per call:
    - `right`: top was `*_trend` and price moved >= 1 ATR in that
      direction within 30 min.
    - `wrong`: opposite direction had larger excursion.
    - `flat`: neither direction reached 1 ATR (or top was `range`).
    - `unclear`: insufficient forward bars or missing ATR.
  - Output structure: `total_unique_calls`, `by_outcome`
    (right/wrong/flat/unclear counts), `right_pct_of_directional`
    (a quick health metric), and `by_hour` (HH → outcome breakdown).
    Dedupes by `(symbol, minute, top_score)` so a single decision
    repeated in many cycles within one minute is counted once.
  - Reads from the already-written `decisions.csv` + `bars/1m/` in
    the same archive directory. Returns `{}` on any I/O / parse
    failure — never crashes the manifest write.
  - Validation against 2026-05-21 archive: 31 unique calls, 2
    right, 11 wrong, 18 flat (right% = 6.45%). Matches the manual
    post-mortem numbers exactly. Hourly breakdown shows the 10am
    whipsaw window where 10 of the 11 "wrong" calls landed.

- **0DTE credit spreads: pivot-buffer gate.** *2026-05-21*
  - New `OptionsConfig` knobs:
    - `credit_pivot_buffer_gate_enabled` (default `False`)
    - `min_short_strike_pivot_buffer_atr` (default `1.0`)
  - When enabled, rejects credit-spread entries whose short strike is
    within `buffer_atr * atr` of the recent market-structure pivot:
    - bear_call: max(LTF / HTF reference_high) → short must sit >=
      buffer ATR ABOVE
    - bull_put: min(LTF / HTF reference_low) → short must sit >=
      buffer ATR BELOW
  - Diagnosis from 2026-05-21 session post-mortem: both bearish
    credit entries at 11:13 had the short strike essentially AT the
    most recent pivot high (SPY short 741 / mshtf_reference_high
    740.615 → $0.39 cushion = 0.71 ATR; QQQ short 712 / reference_high
    711.89 → $0.11 cushion = 0.15 ATR). Both stopped within 30
    seconds on resistance_break_exit for -$60 combined.
  - The existing `credit_distance_gate_enabled` (1.8 ATR from
    CURRENT spot) passed both setups because price was a few strikes
    away — but missed that the short was sitting ON the pivot. The
    new gate measures from the pivot itself.
  - Verified counterfactual: with gate active today, both BEAR
    entries would have been blocked; the 3 BULL setups (all with
    cushion 4.3-5.0 ATR) would have passed unchanged. Net PnL +$52
    instead of -$8.
  - Enabled in `config.zero_dte_etf_options.yaml` with default
    threshold. Long-options yaml left untouched (long premium
    doesn't have a "short strike" in the same sense).

### Fixed

- **Session archive: trades.csv empty when daily fire runs before bot shutdown.** *2026-05-20*
  - Diagnosis from 2026-05-20 session: bot took 2 SPY credit-spread
    trades (both stopped, -$2 each → -$4 realized). Both appeared in
    `events.jsonl`, `bot_*.log`, and the account snapshot — but the
    archive's `trades.csv` was empty (only header) and
    `manifest.trades_today` read 0.
  - Root cause: `_maybe_export_session_archive` (engine.py:493) fires
    once per ET trading day at ~16:00 ET when the equity session
    ends, calling `_export_session_archive` directly. But the
    cumulative `.logs/trades.csv` is only appended-to by
    `write_session_report`, which **only runs on bot shutdown**
    (engine.py:627). So the daily archive reads from a CSV last
    touched at the previous bot shutdown — empty for today.
  - Fix: `export_session_archive` (session_report.py:834+) now reads
    closed trades **directly from `account.trades`** (filtered to
    today's ET date via `exit_time.astimezone(now_et().tzinfo).date()`)
    instead of filtering the stale cumulative CSV. The `account`
    parameter was already passed in but was being ignored for this
    purpose. Cumulative CSV append on shutdown still happens
    unchanged — daily archive no longer depends on it.

- **0DTE credit spreads: 183 `no_hedge_leg` skips per session due to chain truncation + fractional widths.** *2026-05-20*
  - Two root causes stacking, both surfaced on 2026-05-20 session:
    1. `_fetch_raw_option_chain` requested `strikeCount=12` — for
       SPY at 740, that returns strikes 734-745. Short leg picked by
       0.23-delta lands at ~735 (edge of chain). Hedge target 2.5
       below = 732.5, **outside the 12-strike window**, so
       `choose_nearest_strike("lower", 732.5)` returned None.
    2. `_adaptive_strike_width` rounded scaled widths to nearest
       $0.50 — produced fractional targets (732.5, 701.5, 702.5)
       even when the chain only has integer strikes for these ETFs.
  - Fixes:
    - `strikeCount: 12 → 24` — ±12 around ATM gives credit-spread
      hedges room without meaningfully changing API cost or
      liquidity-filter processing time.
    - `_adaptive_strike_width` now snaps to whole dollars via
      explicit `int(scaled + 0.5)` (ceiling-on-half so 2.5 → 3, not
      banker's-rounded 2) and clamps `>= base_width` so the gate-up
      step never silently collapses to a no-op when scaling is just
      above 1.0.
  - Together: today's chain returned (with strikeCount=24) would
    have included strike 732 — `choose_nearest_strike("lower", 732)`
    finds it cleanly. No fractional targets generated. Should
    eliminate the `no_hedge_leg` skip class entirely.

- **Dashboard focus card: long skip reasons no longer push pills off the right edge.** *2026-05-20*
  - The compact decision-label on the focus-meta line (top-left of
    main focus card) could read e.g. `"skipped: option quote
    unstable"` or `"skipped: insufficient underlying bars"` — long
    enough to crowd the right-side `Last / Change / Spread / Vol`
    pill stack and break the chart-head layout.
  - Two-layer fix:
    - **JS abbreviation map** (`COMPACT_DECISION_REASON_LABELS` in
      `dashboard.js`) — 28 entries collapsing the most verbose
      tokens (`insufficient_underlying_bars → low bars`,
      `option_quote_unstable → quote unstable`,
      `no_contract_near_target_delta → no delta match`, etc.).
      Saves 10-30 chars per token. Unmapped tokens still fall
      through to the existing underscore→space humanizer.
    - **Drop the `"<action>: "` prefix in compact form** —
      `entryDecisionLabelCompact` no longer prepends
      `"skipped: "` / `"error: "`. The trade-not-taken state is
      implied by the muted styling and the absence of an active-
      position chip elsewhere on the card. Full-form
      `entryDecisionLabel` keeps the prefix for roomier surfaces.
    - **CSS safety net** — `.focus-meta` gets
      `white-space: nowrap; overflow: hidden; text-overflow:
      ellipsis; max-width: 100%`. `.chart-head > div:first-child`
      gets `min-width: 0; flex: 0 1 auto; overflow: hidden` so the
      left wrapper can shrink. Any future verbose token that
      bypasses the abbreviation map truncates with `…` instead of
      breaking layout.

- **0DTE strategy: KeyError 'nearest_bullish' when `use_fvg_context` disabled.** *2026-05-19*
  - `_regime_confirm` had a partial fallback dict at strategy.py:707-708:
    `{"bull_score": 0.0, "bear_score": 0.0, "directional_pressure": 0.0}`
    — but the entry-context metadata stamping at lines 952-961 reads
    four more keys unconditionally (`nearest_bullish`, `nearest_bearish`
    on both htf_fvg_score and fvg_ltf_score). When `use_fvg_context`
    was False, accessing `htf_fvg_score["nearest_bullish"]` raised
    `KeyError: 'nearest_bullish'`, which engine.py caught and
    rendered as `"Error: 'nearest_bullish'"` in the status banner.
  - Fix: padded the disabled-fallback dict to mirror the full shape
    that `_score_fvg_context` returns when enabled (adds
    `timeframe_minutes: 0`, `nearest_bullish: {}`, `nearest_bearish:
    {}`). The empty dicts are safe — the downstream
    `.get("state", "none")` and `.get("midpoint")` calls gracefully
    resolve to the disabled-state defaults.
  - Test suite missed the bug because no test exercises the
    `use_fvg_context=False` path; only surfaced when a runtime config
    disabled FVG context.

- **0DTE candidate / watchlist tiles: Day% now resolved at publish time when live quote is missing.** *2026-05-19*
  - The same-day local-synthesis cleanup removed the
    `change_from_open: 0.0` stub from 0DTE candidate metadata
    (rightly — a misleading 0.00% pretending to be a real value). But
    the dashboard fallback chain
    (`q.percent_change ?? row.change ?? row.change_from_open`)
    now lands on `None` whenever the live Schwab quote's
    `percent_change` is momentarily missing (gap between cycle quote
    refreshes and stream ticks), rendering `"—"` instead of the real
    tape value.
  - Fix mirrors the live_activity_score / dashboard_directional_bias
    pattern: added a third optional public hook
    `dashboard_change_from_open(frame) -> float | None` on
    `ZeroDteEtfOptionsStrategy`. Returns the session day-return as
    PERCENT (e.g. `1.23` for +1.23%, matching the unit produced by
    TradingView's `change_from_open` and the Schwab quote's
    `percent_change` that the dashboard already prefers). Computed
    via the same `_session_open_price` RTH-first / extended-fallback
    helper `_regime_confirm` uses internally.
  - `engine._publish_state` resolves the new hook via the same
    duck-typed `getattr` dispatch as the other two — same single
    `data.get_merged` frame fetch per candidate (cycle cache means no
    extra API call), same `math.isfinite` finite-check guard.
  - Documented in `_strategies/README.md` extension-hooks bullet
    (now lists all three resolvers together with their return
    contracts and unit conventions).

- **Dashboard chart: drop canvas-drawn plot-area grid.** *2026-05-19*
  - The chart paint loop was drawing 5 horizontal + 6 vertical grid
    lines on the canvas at fixed proportions of the plot area. With
    the radial-masked CSS overlay grid on `.chart-wrap::before` (32px-
    spaced, theme-aware) the two grids stacked at different spacings
    and the outermost canvas lines doubled as a fake inner border —
    "extra grid + duplicate border" against the new gradient chart
    background.
  - Removed the canvas grid; CSS now owns the grid + outer border
    exclusively. Canvas draws data, axes, and overlays only. The
    `tintRgb` resolution stayed (still used by the time-axis ticks).
  - `dashboard.js` `paint()` loses 15 lines of strokeStyle/lineWidth
    + two for-loops. No data path or tooltip/hover math changed.

- **Mobile candidate activity-score display: use shared scorePct().** *2026-05-19*
  - Mobile was using `Math.round(clamp(score, 0, 1) * 100)` for the
    score readout. With the new tape-aware live activity score (often
    1.5–3.0+ for option strategies), every value clamped to 1.0 and
    every candidate showed 100. Same log-scaled mapping the desktop
    candidate ring uses is now shared via `helpers.js`.
  - Promoted `scorePct(score)` from `dashboard.js` to `helpers.js` so
    both renderers reference one implementation. `dashboard.js`
    references the shared version (definition removed locally).

- **Engine `_publish_state`: NaN/Side type guards on live candidate scoring.** *2026-05-19*
  - The newly-added live activity_score and directional_bias
    resolvers (see Added below) had two latent crash paths:
    1. `float(live_score_fn(frame))` accepted NaN/+Inf silently; those
       would propagate through `_json_safe` → `null` and break the
       candidate ring render instead of falling back to the stub.
       Added `math.isfinite` guard; non-finite results keep the
       candidate stub (1.0).
    2. `directional_bias_for_row.value` was outside the try/except
       block — a subclass returning a string (`"LONG"`) instead of
       `Side.LONG` would crash the entire publish loop and freeze
       dashboard updates until restart. Added `isinstance(_, Side)`
       guard; non-Side returns keep the candidate's existing bias.

- **0DTE strategy `_regime_confirm`: reuse `u_day_ret` for change_from_open.** *2026-05-19*
  - The local-synthesis screener change added a duplicate day-move
    computation using `_same_day_mask` to derive
    `candidate_day_move`. That implementation didn't distinguish RTH
    vs extended-hours and reimplemented logic that already existed
    eight lines above as `u_day_ret` (using `_session_open_price`
    with proper RTH-first / extended fallback).
  - Collapsed 18 lines to 1: `candidate_day_move = u_day_ret`. Strict
    correctness improvement — the canonical helper handles session
    edge cases the duplicate didn't.

- **0DTE option strategies: bypass TV screener — synthesize candidates locally.** *2026-05-19*
  - Diagnosis: 2026-05-19 session ran the bot on
    ``zero_dte_etf_options`` and took 0 trades. Investigation showed
    the TV screener silently returned 0 rows every cycle (visible as
    ``Candidate cycle ... count=0 symbols=none`` log lines). The
    screener uses ``Query().set_markets("america")`` and ``where(c
    ("name").isin(["SPY", "QQQ"]))`` to pull SPY / QQQ. That pattern
    works for stocks but appears to silently return empty rows for
    ETFs under some conditions (no Schwab errors, no TV exceptions —
    just an empty DataFrame).
  - The 0DTE screener has been unchanged since the initial commit and
    was never noticed broken because the bot has been running
    ``top_tier_adaptive`` for the past sessions. Switching strategies
    today exposed the bug.
  - Fix: replaced both screeners (parent + long_options) with PURE
    LOCAL SYNTHESIS — no TV call. The universe is fixed
    (``options.underlyings: [SPY, QQQ]``) and these ETFs are always
    liquid, so a TV existence-check adds zero value. Bypassing also
    eliminates the silent-failure mode entirely.
  - Downstream is unaffected: ``_regime_confirm`` already uses
    ``bars[underlying]`` (Schwab data feed) for all metrics. Live
    ``change_from_open`` is now computed from today's session bars in
    the strategy (was previously sourced from the candidate metadata
    that the TV screener populated). ``relative_volume_10d_calc`` was
    already deprecated by the live-activity-score replacement (ce2f009).
  - Together with the live-activity-score work, the 0DTE strategy is
    now fully Schwab-driven for decisioning — TV is only consulted
    indirectly via the bars feed (which uses Schwab).

### Added

- **Dashboard candidates card: live activity_score + directional bias for option strategies.** *2026-05-19*
  - With the local-synthesis screener, the 0DTE Candidate objects ship
    with `activity_score=1.0` and `directional_bias=None` stubs because
    the screener has no access to streamed bars to compute live values.
    Every SPY/QQQ/IWM candidate tile rendered with a fixed 33% red
    score ring and a permanent neutral tone — visually identical
    regardless of tape state.
  - `engine._publish_state` now resolves both values from the active
    strategy when it's an option strategy and the strategy exposes
    `_live_activity_score` / `_dashboard_directional_bias`. Single
    `data.get_merged` frame fetch shared between both compute paths
    (cycle cache means no API call).
  - Added `_dashboard_directional_bias(frame)` to the parent
    `ZeroDteEtfOptionsStrategy`. Returns `Side.LONG` / `Side.SHORT`
    when VWAP-distance, EMA9-EMA20 gap, and day-return all align;
    `None` otherwise. Long-options strategy inherits.
  - Equity strategies fall through unchanged — their screeners already
    set real activity_score and directional_bias values.
  - Equivalent display now: tape-aware ring fill (log-scaled via
    `helpers.js::scorePct` so the bounded `0..1` ratio maps a 0.5–3.0+
    multiplier into a readable arc) and LONG/SHORT/neutral tone
    matching the underlying's current lean.

- **0DTE option strategies: live activity score replaces TV cumulative RVOL.** *2026-05-14*
  - TradingView's `relative_volume_10d_calc` is session-cumulative
    (today_so_far / 10d-avg-daily), so SPY/QQQ structurally read low
    all morning and only approach normal RVOL late in the day. That
    made the legacy `trend_rvol: 1.25` threshold effectively
    unreachable for benchmark ETFs during 0DTE entry windows — the
    trend-confirmation bonus was disabled-by-accident for the very
    symbols this strategy targets.
  - Added `_live_activity_score(frame)` to the parent zero_dte_etf_
    options strategy. Computes from streamed bars:
    - 60% volume momentum: `sum(last 5 bars) / (sum(prior 15) / 3)`
      — recent vs prior on a per-5-bars-equivalent basis
    - 40% ATR expansion: `current_atr14 / median(last 20 atr14)`
    - 1.0 = neutral (normal pace for this symbol's last 20 bars).
  - Replaced four use sites in `_regime_confirm`:
    - Hard gate `weak_relative_volume` → `dead_tape` (default
      `min_activity_for_entry: 0.0` = disabled; SPY/QQQ are always
      live so the gate is opt-in)
    - Trend bonus (bull/bear) → `activity_score >=
      trend_activity_threshold` (default 1.15)
    - Credit bonus → `activity_score >= credit_activity_min` (0.80)
    - Credit penalty → `activity_score >= credit_activity_max` (1.40)
  - New OptionsConfig knobs surface in both 0DTE yamls. Strategy-
    specific tuning:
    - `config.zero_dte_etf_long_options.yaml`: `trend_activity_
      threshold: 1.20` (long premium needs more elevation than
      credit spreads)
    - `config.zero_dte_etf_options.yaml`: `trend_activity_threshold:
      1.15`, `credit_activity_min: 0.80`, `credit_activity_max: 1.30`
  - `candidate_rvol` / `candidate_effective_rvol` are still stamped
    in metadata for dashboard / log visibility. They no longer
    influence entry decisions for 0DTE — replaced wholesale by the
    self-normalizing activity score.

- **0DTE option strategies: IV-rank gate + adaptive credit-spread width.** *2026-05-14*
  - **IV-rank gate** — normalizes current VIX against a user-provided
    52-week range rather than using absolute VIX level. Useful because
    VIX 20 means different things in a 12-18 regime vs a 25-35 regime.
    New `OptionsConfig` knobs (defaults disable the gate):
    - `vix_52w_low: 12.0` / `vix_52w_high: 30.0` — the 52-week range
      (refresh quarterly when VIX environment shifts; no live fetching)
    - `min_iv_rank: 0.0` (disabled) — entries blocked when rank below
    - `max_iv_rank: 1.0` (disabled) — entries blocked when rank above
    - Computation: `iv_rank = clamp((vix_last − vix_52w_low) /
      (vix_52w_high − vix_52w_low), 0, 1)`
    - Skip reasons: `iv_rank_too_low`, `iv_rank_too_high`
  - Strategy-specific defaults:
    - `config.zero_dte_etf_long_options.yaml`: `max_iv_rank: 0.75`
      (don't buy expensive premium when VIX is in top 25% of range)
    - `config.zero_dte_etf_options.yaml`: `min_iv_rank: 0.30`
      (don't sell skinny premium when VIX is in bottom 30% of range)
  - Enabled **adaptive_width_enabled: true** in the credit-spread
    parent yaml. `_adaptive_strike_width` scales vertical-spread strike
    widths with `current_atr / 20-bar_median_atr` clamped to `[1.0,
    adaptive_width_max_scale]`. High-vol days → wider strikes (more
    credit, more cushion); quiet days → tighter strikes. Capped at
    1.5× base width (`adaptive_width_max_scale: 1.5`). Long-options
    don't use this — single-leg.

- **`options.min_vix` lower-bound VIX floor for long-premium strategies.** *2026-05-14*
  - New `OptionsConfig.min_vix` (default `0.0` = disabled, preserves
    legacy behaviour). When set > 0 and VIX is below the floor at
    entry-decision time, `_option_entry_block_reason` rejects with
    `vix_below_floor`.
  - Mirrors the existing `max_vix` cap. Together they let you define
    a tradable VIX range: above `max_vix` premium is too expensive
    (vega risk); below `min_vix` daily range is too thin to overcome
    theta + commissions on long premium.
  - Shipped defaults:
    - `config.zero_dte_etf_long_options.yaml`: `min_vix: 12.0`
      (long-premium strategy — needs movement to win)
    - `config.zero_dte_etf_options.yaml`: `min_vix: 0.0` (explicit
      no-op — credit spreads actually want low VIX)
  - Backward compatible: any config without `min_vix` defaults to
    `0.0` and the gate is fully bypassed.

### Fixed

- **zero_dte_etf_long_options: ORB path bug audit fixes.** *2026-05-14*
  - **B1 (real)**: ORB entry path now respects structural and S/R vetoes,
    mirroring the trend-window gate behaviour. Previously, ORB-window
    entries (09:35-10:05) could fire even when LTF structure was bearish
    on a bullish ORB or when SR was broken below the entry price — risky
    for 0DTE long premium (theta bleeds fast on mis-aligned setups).
    Toggleable via `orb_apply_structure_veto` (default `true`) and
    `orb_apply_sr_veto` (default `true`). Set both to `false` to restore
    legacy "fire on any breakout" behaviour.
  - **B2**: Aligned manifest-default and strategy.py-fallback values
    that had silently drifted: `trend_end_time` fallback `"13:25"` →
    `"13:30"` to match manifest; `min_bars` runtime-entry fallback
    `35` → `90` to match `required_history_bars()` init fallback. The
    manifest values were never the issue (they always load), but the
    fallbacks were a silent-drift hazard if manifest loading ever
    failed partially.
  - **B3 + B4**: ORB window endpoints + opening-range window are now
    BOTH configurable, decoupled. New params:
    - `orb_start_time` (default `"09:35"`) — was previously hardcoded
    - `orb_opening_window_start` (default `"09:30"`) — was hardcoded
    - `orb_opening_window_end` (default `"09:34"`) — was hardcoded
    Previously, a user setting `orb_end_time: "11:00"` to extend the
    trading window would still derive or_high/or_low from the
    09:30-09:34 5-min opening — stale references the breakout was
    measured against. Now the user can extend BOTH together.
  - **C1**: Defensive column access — `last["close"]` → `last.get("close")`
    in the per-candidate setup. If a frame somehow ships without a
    standard indicator column (rare warmup / data-gap edge case), the
    `_safe_float` default kicks in instead of a `KeyError`.
  - **C2**: New `orb_opening_min_bars` (default `3`). The opening
    range now requires ≥ N bars in the configured opening window
    before deriving `or_high` / `or_low`. Previously a single
    09:34 bar would have been treated as the "opening range" and
    produced trivially-passable breakout checks (`last_close > 0.0 *
    1.0008` for empty references).
  - Manifest + yaml preset + README updated with all new params and
    explanatory comments.

- **volatility_squeeze_breakout screener: drop per-minute liquidity filters to stop candidate churn.** *2026-05-14*
  - Setting `min_value_traded_1m: 0.0` and `min_volume_1m: 0` in
    `configs/config.volatility_squeeze_breakout.yaml` disables the
    `Value.Traded|1` and `volume|1` TradingView filters (gated on
    `> 0` in `screener_client._liquid_equity_conditions`).
  - Symptom: per-minute volume metrics flicker around any non-zero
    threshold each cycle (a stock trading 9k shares one minute and
    7k the next would oscillate around an 8k floor), causing symbols
    to drop in/out of the candidate list every refresh. That thrashes
    the watchlist/warmup state and produces unstable screener output.
  - Replaced by relying on the session-level `min_volume: 750,000`
    floor (stable across the day) plus the strategy's own per-bar
    `min_breakout_volume_ratio: 1.12 × box-median` check at entry
    decision time.
  - Other strategies (top_tier_adaptive, pairs_residual, opening_
    range_breakout, etc.) retain their per-minute filters — those
    strategies may have valid reasons (e.g. opening-range setups
    need pre-market activity confirmation). Scoped change to
    vol_squeeze_breakout only.

- **volatility_squeeze_breakout screener: resolved squeeze-paradox liquidity filters.** *2026-05-14*
  - After fixing the math-conflicting session_range cap (see entry below),
    the screener was still returning zero symbols. Root cause: the per-
    minute liquidity gates compounded with `min_rvol: 1.35` to filter
    OUT the very setups the strategy is built to find. A SQUEEZE is
    defined by volume CONTRACTING (RVOL drops below 1.0 pre-breakout),
    so requiring elevated minute-by-minute activity at screen time was
    paradoxical. The strategy already has its own breakout-bar volume
    check (`min_breakout_volume_ratio: 1.12 × box-median` in
    `_score_vol_squeeze`) — that's the right place for the "elevated
    volume" gate.
  - Relaxed liquidity and RVOL gates in
    `configs/config.volatility_squeeze_breakout.yaml`:
    - `min_volume: 1,800,000 → 750,000` (still liquid for entry/exit)
    - `min_value_traded_1m: 350,000 → 75,000` (~5x looser)
    - `min_volume_1m: 45,000 → 8,000` (~5.6x looser; allows compressed-
      tape stocks)
    - `min_rvol: 1.35 → 1.00` (normal-or-better volume; pre-breakout
      squeezes can have RVOL down to 0.6-0.9 so 1.0 is the practical
      floor below which the stock is illiquid)
  - Documented in the yaml that `min_market_cap` / `max_market_cap` are
    inert for this strategy (only `_small_cap_base_conditions` enforces
    them; vol_squeeze uses `_liquid_equity_conditions` which doesn't).
  - Manifest default `min_rvol` updated 1.35 → 1.00 to match.

- **volatility_squeeze_breakout screener: relaxed math-conflicting filters.** *2026-05-14*
  - Initial 2026-05-14 tightening set `screener_max_session_range_pct`
    to 0.018 (1.8%), which was mathematically inconsistent with
    `max_change_from_open: 4.5%`: a stock up 2% from open MUST have
    session_range >= 2% (the price moved at least that much), so the
    1.8% cap effectively dropped the change_from_open band from
    0.45-4.5% to ~0.45-1.5% and the screener returned zero symbols
    in normal market conditions.
  - Revised defaults that preserve the screener's "no excess noise"
    intent without the math conflict:
    - `screener_max_session_range_pct: 0.018 → 0.035` (must EXCEED
      `max_change_from_open` to act as a noise filter, not a hard
      contradiction). A 2.5% mover with session_range 3% is clean
      (kept); same mover with 5% session_range is choppy (rejected).
    - `screener_min_price: 12.0 → 10.0` (mild relaxation; still
      filters the smallest low-float volatility traps).
  - Updated manifest defaults + yaml preset + README guidance with
    the math-conflict note so future tightening attempts don't
    repeat the mistake.

### Added

- **Dashboard watchlist: "IX" chip on index-confirmation ETF cards.** *2026-05-14*
  - New blue "IX" chip on watchlist cards for symbols that are streamed
    purely for directional confirmation (XLK / XLC / XLY / XLE / XLB /
    GDX / COPX / etc) rather than as tradable entry symbols. Sits
    alongside the existing green "TR" (Tradeable) and amber "NS"
    (Non-streamable) chips on `symbol-title-row`.
  - Implementation:
    - New `BaseStrategy.dashboard_index_symbols()` method that returns
      the union of `params.index_symbols` + every ETF referenced under
      `params.sector_index_map`. Subclasses can override.
    - New `DashboardCache.index_symbols()` method that delegates to the
      strategy method with a defensive param-walking fallback (mirrors
      the existing `tradable_symbols()` pattern).
    - `engine.py` adds `"index_symbols": dashboard_cache.index_symbols()`
      to the `data` block of the published payload.
    - `dashboard.js` adds `getDashboardIndexSymbols(data)` helper and
      renders the chip in `renderWatchlist()`. The chip is suppressed
      when the same symbol is ALSO tradable (TR wins).
    - `dashboard.css` adds `.index-chip` rule joined with the existing
      `.tradeable-chip` / `.ns-chip` shared sizing block. Color
      hardcoded to sky-blue (`#6ab7ff`) rather than `var(--accent)` —
      `--accent` is mint on nexus / amber on solstice / violet on
      nebula and would visually collide with the green TR or amber NS
      chips on those themes.
  - Mobile dashboard unchanged: `mobile.js` doesn't render a per-symbol
    watchlist (only a count in the subline), so no chip surface area
    there.
  - Strategies that don't use index confirmation return `[]` from
    `dashboard_index_symbols()`, so no chips render for those configs.

### Changed

- **top_tier_adaptive: materials sector now maps to [XLB, GDX, COPX].** *2026-05-14*
  - The `materials` entry in `sector_index_map` was previously `[XLB]`
    only. XLB is dominated by chemicals (LIN/SHW/APD/ECL ~50% weight),
    so the pure-miner symbols in the default tradable universe — NEM
    (gold miner) and FCX (copper miner) — correlate weakly with XLB
    and would get false `pullback_index_not_confirmed` rejections when
    gold/copper were aligned with the trade but chemicals were flat.
  - Now maps to `[XLB, GDX, COPX]` with OR semantics: a NEM LONG
    confirms when GDX (gold miners) OR XLB OR COPX is bullish on the
    sector confirmation gate. CTVA / DOW (true chemicals) still
    confirm via XLB.
  - `H:\TradingBot\configs\config.top_tier_adaptive.yaml`:
    `index_symbols` updated from `[XLK, XLC, XLY, XLE, XLB]` to
    `[XLK, XLC, XLY, XLE, XLB, GDX, COPX]` so the new ETFs are
    streamed. E: tuned preset's materials map updated for consistency
    (no `index_symbols` change since E:'s tradable universe has no
    materials symbols).
  - Manifest default updated to the multi-ETF mapping.

- **volatility_squeeze_breakout: 3-tier targets + tighter screener.** *2026-05-14*
  - **Three-tier target structure** replaces the prior 2-tier (standard /
    runner) system in `_build_*` of the standalone strategy:
    - Standard `target_rr: 2.05 → 1.95` — every qualifying setup
    - Runner `runner_target_rr: 2.4 → 2.6` — promoted when ANY of: msltf
      BoS in side direction, atr_expansion ≥ `min_atr_expansion_mult +
      0.12`, OR strong-quality breakout (ATR exp ≥ 1.25, vol ratio ≥
      1.5, close_pos ≥ 0.78)
    - **Premium `premium_target_rr: 3.2`** (NEW) — strong-quality AND
      msltf BoS event AND `tech_ctx.bollinger_squeeze` flag
  - New params on the strategy:
    `premium_target_rr` (default 3.2), `tiered_targets_enabled`
    (default true), `tier_atr_expansion_floor` (1.25),
    `tier_volume_ratio_floor` (1.5), `tier_close_position_floor` (0.78).
    Set `tiered_targets_enabled: false` to revert to 2-tier behavior.
  - Signal metadata now stamps `squeeze_tier_label` ("standard" /
    "runner" / "premium") and `squeeze_effective_target_rr` for log
    visibility — post-session analysis can slice trade outcomes by tier.
  - **Screener tightening** (more probable symbols, fewer noise traps):
    - `screener_min_price: 8.0 → 12.0` (now param-tunable, was
      hardcoded). Filters low-float volatility traps where small flows
      move the tape disproportionately.
    - `screener_max_session_range_pct: 0.025 → 0.018`. Stocks already
      showing >1.8% intraday range have used most of the day's energy.
    - `max_change_from_open: 7.5 → 4.5`. Stocks already up 5%+ rarely
      have clean continuation runway out of a squeeze.
    - New `screener_rvol_bonus_*` (threshold 1.8, scale 2.0, cap 5.0):
      RVOL-tier bonus added to `_squeeze_focus_score`. Each unit of
      `_effective_relative_volume` above 1.8 adds 2.0 to the score
      (capped at +5.0). Strong-accumulation names rise to the top of
      the ranked candidate list.
  - Motivation: the strategy was overall restrictive (lots of hard
    filters) but the targets were uniform across breakout quality —
    a marginal setup that barely passed all gates was rewarded the
    same as one with full ATR expansion + BoS + Bollinger squeeze.
    Tier system creates linear reward for breakout quality. Screener
    tightening reduces raw candidate count by ~40-50% while focusing
    on the genuinely compressed, mid-cap-and-up names where squeeze
    breakouts have the highest historical success rate.
  - LONG-side and SHORT-side tier logic mirror each other. SHORT
    strong-quality requires `close_pos <= 1.0 - tier_close_position_
    floor` (close near bar's LOW) instead of upper-bar close.
  - `allow_short: false` in the shipped config preserved per user
    direction — code path unchanged.

- **top_tier_adaptive: vol_squeeze hard gates + setup-quality filters.** *2026-05-14*
  - Built `_build_vol_squeeze_signal` with two complementary gate types
    that keep today's known winners (TSLA +$130, COP +$14, XOM +$33)
    while filtering ~5 of 8 losers identified in the ENTRY_CONTEXT log.
  - **Hard breakout-quality gates** (toggle via `vol_squeeze_hard_
    breakout_gates: true`, default `true`) — convert the prior +0.5
    scoring bonuses into HARD gates. Compression-strong setups (3.5
    base) + a marginal breakout (+1.5 = 5.0) used to pass
    `min_vol_squeeze_score: 4.0` even when the post-breakout bar had
    weak volume / wicky body / barely cleared the box. Now hard-reject:
    - Volume: `bar_volume / box_volume_median >= 1.25` (was scoring
      bonus only at 1.20)
    - Close position: `close_pos >= 0.65` for LONG (mirror for SHORT)
    - Breakout buffer: `last_close >= box_high * (1 + 0.0012)` for LONG
  - **Setup-quality gates** (NEW, separate threshold params):
    - `vol_squeeze_min_sr_bias_alignment` (default `0.20`): rejects
      LONG when `sr_bias_score < -0.20` (HTF SR favors the opposite
      side); mirror for SHORT. Today's losers included AMD 10:06
      (sr_bias −0.75), NFLX 13:35 (−0.60), GOOG 10:08 SHORT (+0.75
      against a SHORT). All 3 winners had `sr_bias_score >= +0.15`.
    - `vol_squeeze_min_pct_b_directional` (default `0.50`): LONG
      requires `tech_bollinger_percent_b >= 0.50` (upper half of BBs
      at the breakout bar); SHORT requires `<= 0.50`. Today's AMD
      10:06 LONG had pct_b 0.31 (lower band), AAPL/GOOG SHORTs had
      0.40/0.44 (mid — not at lower band). All 3 winners had
      pct_b ≥ 0.60.
    - Set either threshold to `0.0` to disable that gate.
  - Skip-reason format surfaces the actual values:
    - `long_vol_squeeze_weak_breakout_buffer(close=X<required=Y)`
    - `vol_squeeze_weak_breakout_volume(ratio=X<1.25)`
    - `long_vol_squeeze_weak_bar_close(pos=X<0.65)`
    - `long_vol_squeeze_sr_against(bias=−0.75<−0.20)`
    - `long_vol_squeeze_pct_b_below_mid(pct_b=0.31<0.50)`
  - Motivation: 2026-05-14 session showed 14 vol_squeeze entries with
    3W/8L (27% wr), +$9.58 net — without TSLA winner −$120 net. The
    earlier attempt at raising scoring bonus thresholds was cosmetic
    because the bonuses only add +0.5; most setups passed `min_score:
    4.0` on compression + breakout alone, never needing the bonuses.
    Hard gates close that loophole, AND the new setup-quality gates
    add data-derived filtering that proved to discriminate winners
    from losers in the session log.
  - Earlier "1.40 vol_ratio + 0.75 close_pos" hard gates were aggressive
    enough to risk blocking the TSLA winner. Softened to 1.25 / 0.65
    in this iteration — winners likely pass both, the SR + pct_b gates
    do the heavy lifting on quality filtering.
  - `disable_vol_squeeze_regime` remains `false` on both E: tuned preset
    and H: running config. Manifest defaults updated to match.

### Added

- **Tight EQH+EQL bias suppression (`structure_min_range_atr_mult`).** *2026-05-14*
  - New `support_resistance` knob `structure_min_range_atr_mult` (default `1.5`).
    When EQH and EQL flags both fire on `analyze_market_structure` AND the
    spread between `reference_high` and `reference_low` is below N×ATR, the
    bias resolver short-circuits to `"neutral"` — preventing the midpoint /
    pivot-bias / recent-event paths from flipping bias on noise within a
    tight consolidation. EQL/HH pivot labels remain on the context so
    range-regime entries (which key on EQ flags for mean-reversion setups)
    still see them.
  - Genuine BoS through `reference_high` / `reference_low` (real breakout
    beyond breakout_buffer) still fires bias bullish/bearish — that check
    runs BEFORE the tight-range short-circuit. CHoCH exits unaffected.
  - Two new fields on `MarketStructureContext` surfaced for log analysis:
    - `structure_range_atr`: spread / ATR (always populated when both
      reference levels present, regardless of tightness flag).
    - `tight_structure_range`: bool flag indicating the guard is active.
  - Both fields auto-surface in `ENTRY_CONTEXT` / `EXIT_CONTEXT` /
    `SKIP_SUMMARY` JSONs via the `msltf_` / `mshtf_` prefix in
    `strategy_base._structure_lists`.
  - Motivation: user observation that "EQL and EQH shouldn't be allowed
    to happen right next to each other — there has to be a gap between
    them or they produce false signals." Specifically: AMD 14:36 LONG
    pullback (2026-05-14) was killed at hold=10.2m via
    `structure_bearish_exit:EQL` on a chop range where bias was
    oscillating noisily. With this guard active and `min_range_atr_mult`
    set to 1.5, the bias resolves to neutral inside the tight range and
    the exit doesn't fire on midpoint-bias noise.
  - Threaded through 3 call sites: `strategy_base._structure_context`,
    `data_feed.build_support_resistance_context` (via SR builder kwarg),
    and `dashboard_cache.analyze_market_structure`. Tests in
    `tests/test_bug_regressions.py::TestTightStructureRangeBias2026_05_14`.
- **top_tier_adaptive: oversized entry bar gate.** *2026-05-14*
  - New params on `top_tier_adaptive` to reject entries when the latest
    LTF 5m bar has range or body far above ATR — catches the "5m close
    lag" chase pattern where the bot waits for a large bar to close and
    enters near its high/low (a $X move already done):
    - `reject_oversized_entry_bar` (default `true`): master switch.
    - `entry_bar_range_max_atr_mult` (default `1.8`): skip when
      `(high - low) / atr14 >= 1.8`.
    - `entry_bar_body_max_atr_mult` (default `1.4`): skip when
      `|close - open| / atr14 >= 1.4`.
    - `orb_bypass_oversized_entry_bar` (default `true`): opening flush
      bars are always huge — bypass during ORB window.
  - Applies to `trend` / `pullback` / `sr_scalp` regimes only. `range`,
    `vol_squeeze`, and `momentum` are exempt because big bars ARE the
    setup for those regimes (range = mean-reversion at extremes;
    squeeze + momentum = expansion-driven).
  - Independent of `reject_stretched_entries` (which keys on Bollinger
    %B + ATR-stretch from EMA20). The stretched gate didn't catch
    AMD-style "big bar but price isn't far from MAs" entries because
    EMAs follow the move; this gate looks at the bar's OWN size.
  - Skip-reason format: `long_oversized_entry_bar(range=X.XX>=R.RR,body=Y.YY>=B.BB)`
    surfaces both metrics so the active condition is identifiable.
  - Implementation in `_finalize_signal` right before the existing
    `reject_stretched_entries` block. Tests in
    `tests/test_bug_regressions.py::TestOversizedEntryBarGate2026_05_14`.
- **Structure-exit pullback grace + BoS confirmation gate.** *2026-05-14*
  - Two new ``support_resistance`` knobs that layer onto the existing
    ``structure_exit_grace_minutes`` / ``structure_exit_min_post_entry_pivots``
    gates that suppress ``structure_bearish_exit`` / ``structure_bullish_exit``
    early in a trade's life:
    - ``structure_exit_grace_minutes_pullback`` (default ``15``): extends
      the grace specifically for the pullback regime (``position.metadata
      .regime == "pullback"``). Pullback by design enters into LTF chop —
      the first EQL/LL pivot 10 minutes in is almost always noise, not
      reversal. Other regimes still use the global grace (10 min).
    - ``structure_exit_require_bos_confirmation`` (default ``true``): the
      bias-flip exit now additionally requires an active BoS event
      (``bos_down`` for long-exit, ``bos_up`` for short-exit). Without
      this, bias flips on a single EQL/HH pivot — a noisy, weak signal.
      With it, the bot waits for actual structural break (price below a
      prior swing low / above a prior swing high). CHoCH exits remain
      unaffected — those are already strong signals.
  - Motivation: AMD 14:36 LONG (pullback regime, 2026-05-14) was killed
    at hold=10.2m via ``structure_bearish_exit:EQL``. The exit barely
    cleared both legacy gates (10min/2-pivot); the LTF formed a single
    EQL pivot, bias flipped bearish, exit fired. Price recovered to
    ~$452 (past R1 $450.10, toward R2 $454.65) shortly after — a
    winnable trade aborted on noise.
  - Per-regime grace is implemented in ``strategy_base.position_exit_signal``
    by branching on ``position.metadata.regime``. BoS confirmation is
    applied to both LONG and SHORT bias-flip paths. Tests added in
    ``tests/test_bug_regressions.py::TestPullbackGraceAndBoSConfirmation2026_05_14``.
- **top_tier_adaptive: per-sector index confirmation map.** *2026-05-14*
  - New ``sector_index_map`` param routes each candidate to a sector-
    specific list of index ETFs for entry confirmation, replacing the
    "OR across all ``index_symbols``" behavior. Prevents e.g. an AAPL
    LONG from being confirmed by XLE just because energy happened to
    be bullish-aligned.
  - Default mapping covers all 11 GICS sectors with the canonical SPDR
    Select Sector ETFs: ``tech: [XLK]``, ``consumer_discretionary: [XLY]``,
    ``communication: [XLC]``, ``financials: [XLF]``, ``healthcare: [XLV]``,
    ``industrials: [XLI]``, ``energy: [XLE]``, ``consumer_staples: [XLP]``,
    ``materials: [XLB]``, ``real_estate: [XLRE]``, ``utilities: [XLU]``.
  - New strategy helper ``_indices_for_symbol(symbol)`` walks
    ``sector_groups`` to find the symbol's sector, then reads
    ``sector_index_map[sector]``. Falls back to the broad
    ``index_symbols`` list when no per-sector mapping exists
    (backward-compat for legacy configs).
  - ``_index_confirms`` and ``_index_neutral`` now take a ``symbol``
    parameter; called per-candidate inside ``entry_signals`` (was
    hoisted to once-per-cycle under the broad SPY/QQQ design).
  - Default ``index_symbols`` updated to the SPDR Select Sector ETFs
    covering the default tradable universe's sectors (XLK / XLC / XLY
    / XLF / XLV / XLP). SPY + QQQ removed — they're no longer in any
    sector's map entry, so streaming them was wasted quote bandwidth.
- **top_tier_adaptive: early-session stop widening (Tier 2a companion).**
  *2026-05-14*
  - New params: ``early_session_stop_widening_enabled`` (default true),
    ``early_session_stop_widening_until`` (default ``"10:30"``),
    ``early_session_stop_widening_mult`` (default 1.3).
  - ``_volatility_widening_factor`` now combines two orthogonal
    triggers: (1) the existing ATR-expansion check (RELATIVE), and
    (2) a time-of-day check (ABSOLUTE) that fires during the
    post-open high-vol window. Final factor = ``max(expansion_factor,
    time_factor)`` capped at ``atr_widening_max_factor`` — the two
    don't compound to avoid over-widening on explosive opens.
  - Motivation: an AMD 10:10 LONG was stopped out at $444.46 (entry
    $445.95, $1.49 risk) on a single 1m wick that reversed to $449+
    five minutes later. Tier 2a's relative-expansion check read
    "normal" because all the post-open bars were noisy together. With
    the 1.3x absolute multiplier, the stop would have been ~$444.02 —
    below the dip — and the trade catches the $3+ recovery.
- **Adaptive ladder: triple-gate suppress decision** *2026-05-14*.
  Target-exit suppression in ``position_manager._adaptive_ladder_management``
  now requires THREE confirmations before holding through the multi-
  bar zone flip (previously a single intra-bar tick at target was
  enough to lock the position for 2+ minutes):
  - **Strength gate** (``_ladder_target_strength_confirmed``): the
    last FULLY CLOSED bar must close at/past target with a strong
    directional body (close in the upper/lower 55% of bar range for
    LONG/SHORT). Filters single-tick wicks that revert.
  - **Index re-alignment gate** (``_ladder_indices_still_aligned``):
    re-checks the trade's entry-time ``confirmation_indices`` (newly
    stamped on signal metadata at entry) and verifies at least one
    sector ETF is STILL aligned with the trade direction. If the
    sector tape has flipped since entry, suppress is denied and the
    target exit fires normally — avoids holding through sector
    reversals.
  - **Rung-not-confirmed gate** (existing): the multi-bar zone flip
    hasn't completed yet.
  - Suppress fires only when target_reached + breakout_strength +
    indices_aligned + (NOT rung_confirmed). Any failure → exit at
    target.

### Changed

- **paper_account: per-trade R/R now uses initial stop/target.**
  *2026-05-14*
  - ``_position_to_dict`` reads ``metadata.initial_stop_price`` and
    ``metadata.initial_target_price`` (stamped at entry by
    ``entry_gatekeeper.py:677-678/1215-1216``, immutable thereafter)
    for ``max_risk`` and ``max_reward`` calculation. Falls back to
    live ``stop_price``/``target_price`` for legacy positions.
  - Was: max_risk used the live ``position.stop_price``, so when
    ``adaptive_breakeven_rr`` ratcheted the stop to entry,
    ``max(0, entry - stop) = 0``, max_risk became 0, and the
    dashboard's R/R rendered as ``—`` for every winning trade past
    breakeven (which is most of them).
  - Same payload now also exposes ``initial_stop_price`` +
    ``initial_target_price`` as first-class fields so the
    dashboard's progress bar can keep a stable range as adaptive
    management ratchets the live stop/target.
- **Dashboard: position progress bar uses initial stop/target.**
  *2026-05-14*
  - ``positionRangeSpec`` in dashboard.js now reads
    ``pos.initial_stop_price`` / ``pos.initial_target_price`` with
    fallback to the live values via ``??``. Bar layout stays stable
    through adaptive ratchets (breakeven trail, final-rung clearing
    target to None) so the "where's my stop?" gap doesn't appear.
- **Dashboard: chart marker labels merge on price collision.**
  *2026-05-14*
  - ``pushMarkerLine`` merges labels when a new marker lands at the
    same price as an existing one (e.g. Stop ratchets to entry →
    "Entry / Stop" / "E·ST" combined label) instead of silently
    dropping the second line as a duplicate. The dropped-Stop case
    made it look like the position had no stop on the chart.
- **Dashboard: trade table column "Strategy" → "Regime".**
  *2026-05-14*
  - ``TradeRecord.regime`` (stamped on exit from
    ``position.metadata.regime``) is now the displayed value, with
    fallback chain ``trade.regime || trade.strategy || '—'`` for
    pre-stamp trades. Identifies which of the 6 regimes (trend /
    pullback / range / vol_squeeze / momentum / sr_scalp) produced
    each closed trade.
- **Dashboard: exposure gauge honesty over 100%.** *2026-05-14*
  - Ring fill stays clamped at 100% (preserves the gauge metaphor)
    but the text readout now uses the UNCLAMPED ratio, so 128%
    exposure on a long+short portfolio reads as ``128%`` instead of
    ``100%``. Ring tone flips to ``warn`` (orange) when ratio > 100%.
    Same fix applied to desktop dashboard.js + mobile.js.
- **Mobile dashboard: align topbar with sibling panels + many polish
  tweaks.** *2026-05-13/14*
  - Topbar padding (16px) + box-shadow (var(--shadow)) match the
    ``.panel`` cards below. Inner-pill layout is 3-col grid with
    inline ``label: value`` chips; status row spans full width with
    chip + mode badge left-aligned. Trimmed top padding to compensate
    for ``brand-title`` line-height whitespace.
  - Subline trimmed: drop redundant ``ready X/Y · loading Z`` (already
    in READY pill), abbreviate ``streaming N symbols`` → ``N streams``.
  - New ``API/min`` pill wired to ``data.api_usage.calls_per_minute_5m``.
  - Added Candidates card + Completed Trades card (mobile-only
    compact list views).
  - Removed inner ``overflow-y: auto`` from ``.positions-scroll`` —
    swipes on position cards now pass through to the page scroll
    instead of being eaten by the inner scroll container.
  - Day-% color coding on candidate rows (green/red); trade-row
    dollar amounts intentionally uncolored per user preference.
- **Mobile dashboard: tooltip theme matches active theme.**
  *2026-05-13*
  - ``.chart-tooltip`` ``background`` and ``box-shadow`` switched
    from hardcoded dark blue to ``var(--panel-bg)`` and
    ``var(--shadow)``. Works correctly across all 6 themes
    (default / dark / light / nexus / solstice / nebula).

### Changed

- **top_tier_adaptive config: high-volatility retune.** *2026-05-13*
  - ``configs/config.top_tier_adaptive.yaml`` retuned for elevated-VIX
    tapes. Manifest (``_strategies/top_tier_adaptive/manifest.json``)
    LEFT UNTOUCHED — manifest preserves the shipped low/mid-vol defaults
    so the baseline isn't lost. The yaml is now the deployed high-vol
    preset.
  - **Theme**: bars/extensions are larger in high vol, so
    absolute-distance filters LOOSEN; chop is worse so score gates +
    sr_scalp distances TIGHTEN; giveback is faster so profit-lock
    engages SOONER and locks MORE; ATR expansion triggers stop-widening
    SOONER and goes FURTHER. Soft-bias and high-conviction thresholds
    re-scaled to the bigger day_strength swings high vol produces.
  - **Score / selectivity gates**:
    * ``min_score_gap``: 1.4 → 1.5 (scores noisier; bigger gap for
      decisive regime selection)
    * ``min_adx14``: 16.0 → 18.0 (ADX naturally higher in high vol;
      demand stronger trend reading)
  - **Buffers (absolute distance — bars are larger)**:
    * ``stop_buffer_atr_mult``: 0.25 → 0.30 (wider base buffer; Tier 2a
      scales this further when ATR expands)
    * ``pullback_ema_touch_atr_mult``: 0.35 → 0.45
    * ``pullback_hold_atr_mult``: 0.40 → 0.50
    * ``max_entry_vwap_extension_atr``: 1.50 → 1.80
    * ``max_entry_ema9_extension_atr``: 1.20 → 1.50
    * ``max_entry_bar_range_atr``: 1.80 → 2.20
  - **Stretched filter (bands widen in high vol)**:
    * ``stretched_percent_b_max``: 0.80 → 0.85
    * ``stretched_atr_mult_max``: 1.1 → 1.3
  - **Broken-level clearance (broken levels noisier)**:
    * ``broken_level_min_clearance_pct``: 0.0025 → 0.0035
    * ``broken_level_min_clearance_atr``: 0.72 → 0.90
  - **Target conservatism (SR targets fail more)**:
    * ``target_max_sr_ratio``: 0.8 → 0.7 (30% head-room vs 20%)
  - **Adaptive profit protection (giveback faster)**:
    * ``adaptive_profit_lock_rr``: 1.30 → 1.20 (engage sooner)
    * ``adaptive_profit_lock_stop_rr``: 0.35 → 0.45 (lock more)
  - **Vol-squeeze regime (false breakouts more common)**:
    * ``vol_squeeze_breakout_buffer_pct``: 0.0008 → 0.0012
    * ``vol_squeeze_min_breakout_volume_ratio``: 1.12 → 1.20
  - **Momentum regime (1.5% day strength is common in high vol)**:
    * ``momentum_min_day_strength``: 1.5 → 2.0
  - **sr_scalp regime (S/R failures more common; zones need to be
    further apart and closer-to-edge entries only)**:
    * ``min_sr_scalp_score``: 3.5 → 4.0
    * ``sr_scalp_min_distance_pct``: 0.008 → 0.012 (1.2% zone gap floor)
    * ``sr_scalp_min_distance_atr``: 2.5 → 3.0
    * ``sr_scalp_max_distance_from_zone_atr``: 0.5 → 0.4
  - **Bias (intraday swings bigger; raise thresholds to match)**:
    * ``directional_bias_min_day_strength``: 0.20 → 0.30
    * ``bias_penalty_saturate_at``: 2.0 → 2.5
  - **Tier 2a — ATR-aware stop widening (ATR expansion the norm)**:
    * ``atr_widening_threshold``: 1.3 → 1.2 (trigger sooner)
    * ``atr_widening_max_factor``: 1.5 → 1.8 (more headroom)
  - **Tier 3b — high-conviction peak-giveback override (2.0% is common
    in high vol; raise bar; give conviction trades more runway)**:
    * ``peak_giveback_high_conviction_day_strength_pct``: 2.0 → 2.5
    * ``peak_giveback_high_conviction_min_r``: 2.0 → 2.5
  - **Untouched** (deliberately): score floors per regime
    (``min_trend_score``, ``min_pullback_score``, ``min_range_score``,
    ``min_vol_squeeze_score``, ``min_momentum_score``); regime time
    windows; FVG weights; ladder builder; sector concentration cap; all
    runtime/risk block values (``max_positions``, ``risk_per_trade_*``,
    ``cooldown_minutes`` — runtime-level changes deferred so they
    remain explicit user choices not implicit in a strategy preset).
  - 36 strategy tests still pass. Six tests updated to be insulated
    from yaml preset retunes (read ``bias_penalty_base/saturate_at`` and
    ``atr_widening_threshold/max_factor`` from ``strategy.params``
    dynamically, then verify the formula rather than hardcoded numerical
    outputs). Two pre-existing stale tests (
    ``test_midday_window_allows_pullback_and_momentum``,
    ``test_disable_pullback_removes_it_from_all_windows``) updated to
    include ``sr_scalp`` in the midday allowed-regime set — sr_scalp's
    window (orb_end → no_new) legitimately spans midday, the prior
    expectations predated the 2026-05-12 sr_scalp add.

### Added

- **top_tier_adaptive: new `sr_scalp` regime — HTF S/R mean-reversion
  scalp.** *2026-05-12*
  - 6th regime in the auction. Mean-reversion BETWEEN the bot's existing
    HTF support / resistance zones — NO strategy-local level creation.
    All inputs come from the same sources the rest of the bot uses:
    * Level prices: ``sr_ctx.nearest_support`` (HS) and
      ``sr_ctx.nearest_resistance`` (HR), same fields the dashboard
      labels HS/HR and ``_refine_*_sr_levels`` consume.
    * Zone bands: ``zone_atr_mult * atr`` or ``zone_pct * close`` (max),
      defaulting to the bot-wide 0.20*atr / 0.15%*close. Same formula
      as the dashboard's ``key_level_zones``.
    * Stop nudge: ``sr_ctx.level_buffer`` (with ``vol_widening``).
      Same buffer ``_refine_bullish_sr_levels`` and other S/R code
      use to nudge stops past structural levels.
  - **Distance gate**: the INNER gap ``(HR_zone_lower − HS_zone_upper)``
    must clear BOTH floors (max wins):
    ``sr_scalp_min_distance_pct * close`` (default 0.8%) AND
    ``sr_scalp_min_distance_atr * atr`` (default 2.5x). Too-close zones
    get rejected at build time as ``htf_zones_too_close``; the
    build-queue fall-through then tries other regimes on the same /
    opposite side.
  - **Proximity gate**: close must be inside the entry-side zone OR
    within ``sr_scalp_max_distance_from_zone_atr * atr`` (default 0.5x)
    of its inner edge. Mid-range candles don't qualify.
  - **Permissive scoring**: ``_score_sr_scalp`` rewards bar character
    (lower-wick rejection for LONG, upper for SHORT), VWAP/EMA
    neutrality, low ADX. Max score 5.0; ``min_sr_scalp_score`` default
    3.5. The strict HTF zone check runs at build time, not scoring.
  - **Index-confirmation exempt** (same as range — mean-reversion).
  - **Allowed windows**: orb_end → no_new_entries_after. Skipped during
    ORB to avoid morning level-break chop.
  - **Stop**: ``HS_zone_lower − level_buffer`` (LONG) /
    ``HR_zone_upper + level_buffer`` (SHORT).
  - **Target**: ``HR_zone_lower − level_buffer`` (LONG) /
    ``HS_zone_upper + level_buffer`` (SHORT) — exits at the inner edge
    of the opposite zone, matching the bot's structural-exit
    conventions elsewhere.
  - 4 new tests in ``TestSRScalpRegime`` (36 total in
    ``test_top_tier_adaptive_new_regimes.py``).

- **top_tier_adaptive: Tier 2a — volatility-aware stop widening.**
  *2026-05-12*
  - On trend-day regimes (when current ATR has expanded past
    ``atr_widening_threshold`` × its 5-bar average, default 1.3x), all
    ATR-based stop buffers scale up linearly to ``atr_widening_max_factor``
    (default 1.5x) at 2x the threshold.
  - Risk-per-share widens; risk manager downsizes share count so dollar
    risk per trade stays constant. Effect: fewer false stops from
    trend-day noise, more winners captured without raising trade risk.
  - Applies to all five regime builders (trend / pullback / range /
    vol_squeeze / momentum). Each multiplies its ATR-based buffer
    and the ``default_stop_pct`` floor by the per-candidate widening
    factor.
  - New strategy method: ``_volatility_widening_factor(tech_ctx)``.
  - New config params: ``atr_aware_stop_enabled`` (default ``true``),
    ``atr_widening_threshold`` (1.3), ``atr_widening_max_factor`` (1.5).
  - Stamped on signal metadata as ``vol_widening_factor`` (when >1) for
    post-mortem debugging.

- **top_tier_adaptive: Tier 3b — high-conviction peak-giveback
  loosening.** *2026-05-12*
  - When the candidate's live ``day_strength`` magnitude exceeds
    ``peak_giveback_high_conviction_day_strength_pct`` (default 2.0%)
    at entry, the signal is stamped with
    ``metadata["peak_giveback_min_r_override"] = peak_giveback_high_conviction_min_r``
    (default 2.0).
  - ``risk.py:_peak_giveback_triggered`` reads the override from
    ``position.metadata`` and uses it instead of the global default
    (``config.risk.peak_giveback_min_r``, typically 1.0).
  - Effect: a 2R+ runner on a trend day won't get cut by a normal 50%
    retracement — it has runway to recover and extend. Low-conviction
    trades retain the conservative 1.0R threshold.
  - Per-trade stamp (not session-wide), so each candidate gets its own
    conviction assessment at entry time.
  - 6 new tests in ``TestVolatilityWideningFactor`` (32 total in
    ``test_top_tier_adaptive_new_regimes.py``).

### Changed

- **top_tier_adaptive: regime-to-regime fall-through at build time.**
  *2026-05-12*
  - Old behavior: each side selected ONE regime (the top-scoring one
    after primary + fallback selection paths). If that regime's build
    method failed (e.g. trend's ``no_fresh_breakout``, range's
    ``bollinger_squeeze`` rejection), the side failed entirely — other
    qualifying regimes on the same side were silently ignored.
  - New behavior: each side stores an ordered LIST of qualifying
    regimes (those meeting their ``min_*_score`` threshold) in
    post-penalty score-descending order. The build phase iterates this
    list and tries each regime's build in turn. First successful build
    wins; build failures fall through to the next qualifying regime
    on the same side. Across sides, the higher-scored side gets its
    full build_order tried first.
  - Effect: a high-scoring trend regime that misses its breakout gate
    no longer blocks a qualifying pullback or vol_squeeze from firing
    on the same side. Multiple regimes can coexist on a candidate;
    they no longer compete winner-takes-all for the single slot.
  - ``min_score_gap`` config param is now unused — the primary-vs-fallback
    selection paths it gated are collapsed into the unified
    build-order iteration. Param retained for backwards compat with
    existing configs (silently ignored).

- **top_tier_adaptive: Fix A refactored from hard lockout to soft score
  penalty.** *2026-05-12*
  - Old behavior: when the candidate's live bias was set (e.g. SHORT),
    `preferred_sides` was hard-locked to that single side. The strategy
    never scored or evaluated the opposite side, silently ignoring
    legitimate counter-bias setups (e.g. a bullish BOS + breakout on a
    stock with mildly-negative day_strength).
  - New behavior: both sides are always evaluated. When the side
    disagrees with the live bias, each regime score for that side is
    reduced by `bias_penalty_base * min(1.0, |day_strength| /
    bias_penalty_saturate_at)` before the score-gap auction. Weak
    counter-bias setups are filtered (penalty drags them below
    `min_*_score`); strong structural ones still qualify.
  - Two new params: `bias_penalty_base` (default `1.0`) +
    `bias_penalty_saturate_at` (default `2.0%`).
  - Worked example: a stock with `day_strength = -0.5%` (mild SHORT
    bias) has LONG-side regime scores reduced by 0.25. A trend score of
    5.0 → 4.75 (still above `min_trend_score: 3.5`, qualifies). A trend
    score of 4.0 → 3.75 (still qualifies but margin thinner).
  - 2026-04-20 protection preserved: a stock with `day_strength = -2.0%`
    applies the full 1.0 penalty to LONG-side regimes, blocking weak
    LONG bounces. Stocks with `|day_strength| > saturate_at` get the
    full penalty (no further scaling).
  - Trailing-bias memory unchanged — still infers the bias from recent
    cycles when current `live_bias` is None.
  - `entry_decision` log includes `bias_pen=X.XX` in the failure reason
    when the penalty contributed to no-qualifying-regime, so
    post-mortem can distinguish soft-bias filtering from raw-weak
    scores.

- **top_tier_adaptive: `momentum_close` regime renamed to `momentum`
  and widened from afternoon-only to post-ORB through close.**
  *2026-05-12*
  - Old behavior: regime was restricted to the afternoon window
    (`afternoon_start_time` → `no_new_entries_after`) — i.e., a
    ride-the-bell continuation pattern only.
  - New behavior: regime is allowed in primary
    (`orb_end_time` → `midday_start_time`), midday
    (`midday_start_time` → `midday_end_time`), AND afternoon
    (`afternoon_start_time` → `no_new_entries_after`). The
    `momentum_min_day_strength` hard gate (default 1.5%) is what
    filters chop — stocks without enough intraday move score zero,
    so the time window doesn't need to do the filtering.
  - Methods renamed: `_score_momentum_close` → `_score_momentum`,
    `_build_momentum_close_signal` → `_build_momentum_signal`.
  - Params renamed (clean break, no compat shim):
    `min_momentum_close_score` → `min_momentum_score`,
    `momentum_close_breakout_lookback_bars` →
    `momentum_breakout_lookback_bars`,
    `momentum_close_min_day_strength` → `momentum_min_day_strength`,
    `momentum_close_target_rr` → `momentum_target_rr`,
    `disable_momentum_close_regime` → `disable_momentum_regime`.
  - Regime string in code/logs: `"momentum_close"` → `"momentum"`.
  - **Note for H:\\TradingBot users**: the old param names in
    user-managed configs will silently fall through to defaults
    after upgrading. Rename the keys when you sync.
  - Tests in `tests/test_top_tier_adaptive_new_regimes.py` updated
    for the new name + window.
  - The standalone `momentum_close` strategy
    (`_strategies/momentum_close/`) is unchanged — only the
    top_tier integration was renamed.

- **top_tier_adaptive: directional bias is now computed LIVE in the
  strategy.** *2026-05-12*
  - `_compute_live_directional_bias(frame, close)` reads
    `session_open` from the LTF frame and computes
    `day_strength = (close − session_open) / session_open * 100`.
    Returns `Side.LONG` / `Side.SHORT` / `None` based on a configurable
    threshold (`directional_bias_min_day_strength`, default `0.20%`).
  - Replaces the previous Fix A flow that read the screener's
    pre-computed `c.directional_bias`. The screener value was up to
    ~60s stale and (before this change) was derived from `change`
    (prior-close-relative), which mis-tagged gap-fade days — a stock
    that gapped +2% and faded to flat would read LONG by `change`
    but is actually neutral / SHORT-intent intraday.
  - Trailing-bias memory now records the live bias (not the screener
    bias) so the inferred-bias fallback reflects what live day_strength
    has been doing across recent cycles.
  - The screener (`top_tier_adaptive/screener.py`) now queries BOTH
    `change` and `change_from_open` from TradingView. The
    `directional_bias_fn` and `activity_score_fn` use
    `change_from_open` (matching the strategy's intraday semantic) so
    the gatekeeper's per-side cooldown lookup stays aligned with what
    the strategy will actually evaluate. The previous compat alias
    `rows["change_from_open"] = rows["change"]` (a clean-break
    violation flagged in the prior review) is removed.
  - Dashboard candidate "Day %" continues to display the live Schwab
    `quote.percent_change` (prior-close-relative); the screener
    fallback path is rarely hit during RTH live trading.

### Added

- **top_tier_adaptive: two new regimes (vol_squeeze, momentum_close).** *2026-05-12*
  - **`vol_squeeze`**: Bollinger-squeeze breakout regime. Detects an
    N-bar compression box via `vol_squeeze_lookback_bars` (default 12)
    where `bb_width_pct` and box range are both below configurable
    ceilings, then scores breakout magnitude, confirming volume ratio,
    bar-close position within the breakout candle, and VWAP/EMA
    alignment. Allowed in the primary window (`orb_end_time` →
    `midday_start_time`) and the afternoon (`afternoon_start_time` →
    `no_new_entries_after`).
  - **`momentum_close`**: ride-the-bell continuation regime. Computes
    `day_strength` LIVE from session open + current close (not from
    the screener's `change_from_open`), hard-gates on
    `momentum_close_min_day_strength` (default 1.5%), then scores
    tier-based magnitude + N-bar breakout (1m frame) + alignment.
    **Restricted to the afternoon window only** per user spec —
    pre-afternoon momentum is already covered by trend/pullback.
  - Both regimes compete with trend/pullback/range via the same
    score-gap auction. Independent min-score thresholds
    (`min_vol_squeeze_score: 4.0`, `min_momentum_close_score: 4.0`).
    Independent R:R targets (`vol_squeeze_target_rr: 2.05`,
    `momentum_close_target_rr: 2.0`).
  - **Per-regime opt-out knobs** added for all five regimes:
    `disable_trend_regime`, `disable_pullback_regime`,
    `disable_range_regime`, `disable_vol_squeeze_regime`,
    `disable_momentum_close_regime` (all default `false`). Stripping
    any one removes it from every time window.
  - **Whole-window ORB opt-out** added: `disable_orb_window`
    (default `false`) skips the entire 09:35 → `orb_end_time` window.
    Distinct from the existing `orb_bypass_*` flags which loosen
    filters within the ORB window — this one skips it entirely. Useful
    on tapes where the opening 30 minutes are too whippy and the bot
    should start taking entries at `orb_end_time` instead.
  - All time-of-day boundaries are param-driven; no hard-coded times.
    momentum_close gating reads `afternoon_start_time` and
    `no_new_entries_after` from params (defaults `13:00` / `15:00`).
  - 14 new smoke tests in `tests/test_top_tier_adaptive_new_regimes.py`
    cover regime-window allowance, all five regime disable flags, the
    ORB-window disable flag, and score method robustness on minimal-bar
    frames.

- **LTF/HTF separation cleanup.** The previous code conflated LTF
  (lower-timeframe / trigger frame) and HTF (higher-timeframe / SR
  context) via a silent override pattern: `support_resistance.timeframe_minutes`
  was treated as LTF in name but routinely used as HTF whenever a
  strategy declared `params.htf_timeframe_minutes`. This made it
  impossible to read a config and know what timeframe each block was
  really driving. Cleanup:
  - **Strategy params renamed** for clarity: `htf_timeframe_minutes`
    → `htf_minutes`, `trigger_timeframe_minutes` → `ltf_minutes`. The
    older names are removed entirely (per project's clean-breaks
    convention) — manifests, yaml configs, READMEs all migrated. 35
    files updated.
  - **`support_resistance.timeframe_minutes` is now the default HTF**
    used by SR detection, key-level zones, dashboard sidebar S/R list,
    and engine entry/exit gating. Strategies that operate on a
    different HTF override per-strategy via `params.htf_minutes`.
  - **LTF defaults to 1-minute streaming bars** when a strategy doesn't
    declare `params.ltf_minutes`. Strategies with a distinct intraday
    trigger candle (e.g. `peer_confirmed_key_levels` uses 5-min
    triggers) declare it explicitly.
  - **Helper rename**: `_active_sr_*` / `active_sr_*` / `_sr_*`
    accessors → `_active_htf_*` / `active_htf_*` / `_htf_*`. Each is
    explicit about reading HTF; the old names hid that. New parallel
    `_active_ltf_minutes` / `_ltf_minutes` accessors expose the LTF
    timeframe. The override fallback pattern (read params first, fall
    back to support_resistance block) is preserved — only the names
    are now honest.
  - **Dashboard chart "ltf" mode** renders at the strategy's LTF
    instead of hardcoded 1-minute bars. For `peer_confirmed_key_levels`
    that's 5-min bars; for simpler strategies still 1-min. Mode value
    `"1m"` is accepted as a back-compat alias for `"ltf"` for one
    release, then dropped. Frontend `dashboard.js` migrated to the
    canonical `"ltf"` value.
  - **No trading behavior change for stops/exits/risk** — these were
    already getting HTF via the override; now they get HTF explicitly.
    Streaming responsiveness preserved (flip frame is always 1m,
    live price for stop trigger is always 1m, regardless of HTF).
- **Always-on operation.** Bot now runs continuously across days instead of
  exiting at session close. Three new `RuntimeConfig` knobs:
  `idle_sleep_seconds` (default `60.0`, ~95% overnight CPU savings via
  outside-stream-window cadence), `symbol_state_prune_seconds` (default
  `1800.0`, evicts per-symbol state for inactive symbols on the configured
  cadence — `MarketDataStore.prune_inactive_symbols` /
  `DashboardCache.prune_inactive_symbols`), and `session_reconcile_on_resume`
  (default `true`, re-runs startup reconcile on the first cycle of each new
  ET trading day to catch overnight position changes). Daily session archive
  now fires once per ET trading day after the stream window closes (8pm ET)
  in addition to shutdown. Engine main-loop resilience: exponential backoff
  (2× per consecutive `step()` error, capped at 60s) plus log throttling
  replaces the previous tight 2s retry. Session-rollover hook clears
  `entry_gatekeeper.session_skip_counts` on ET trading-date change so daily
  archives reflect that day's tally only.
- **Order blocks** (`order_blocks.py`). Detection at both 1-minute and HTF
  timeframes with two modes (`loose` / `strict`). Eight knobs in
  `SupportResistanceConfig`: `{ltf,htf}_order_blocks_enabled` enable
  flags plus shared `order_block_mode`, `order_block_max_per_side`,
  `order_block_min_atr_mult`, `order_block_min_pct`,
  `order_block_min_thrust_atr_mult` (default `0.75` — break-of-structure
  thrust filter), `order_block_pivot_span`, and
  `order_block_new_high_lookback`. Strength-based ranking (thrust × size ×
  age × validity) when `max_per_side` clips. New `BaseStrategy` methods:
  `_ltf_order_block_context`, `_htf_order_block_context`,
  `_continuation_ob_retest_plan`, and `_apply_continuation_zone_retest_plans`
  (OR-combine FVG + OB plans). Dashboard chart overlays (dashed border, faint
  fill) via per-profile `show_htf_order_blocks` / `show_ltf_order_blocks` flags;
  cross-timeframe protection mirrors FVG behavior. All 18 shipped presets
  expose the eight OB knobs (defaults safe-off); `peer_confirmed_key_levels`
  ships with OB detection disabled since its custom entry pipeline doesn't
  consume OBs. Reuses every `anti_chase_fvg_retest_*` knob for bar-confirmation.
- **Heal-propagation hook** (`data_feed.py fetch_history`). A successful
  1m heal now invalidates `last_htf_refresh` and the cycle-scoped HTF
  cache so the HTF rebuild fires immediately on the healed 1m frame
  instead of waiting until the next bar boundary. Skipped on empty heals
  (REST returned no candles) since the existing HTF derivation is still
  valid.
- **Quote alias caching** (`data_feed.py`). New `_resolved_quote_alias` cache
  resolves index-like symbols (`NYICDX`→`$NYICDX`, `VIX`→`$VIX`) once and
  routes them through batched `fetch_quotes` instead of issuing a per-cycle
  one-off `quote()` call. Cuts ~1 call/cycle per index symbol.
- **Sliding-window API tracker** (`utils.py SchwabdevApiUsageTracker`).
  Replaces lifetime average with deque-backed sliding windows at 1m / 5m /
  15m / 30m granularities. Snapshot exposes `calls_per_minute_{1m,5m,15m,30m}`,
  raw `calls_window_*` counts, and `lifetime_calls_per_minute`. The legacy
  `avg_calls_per_minute` field is removed entirely — dashboard.js consumers
  read `calls_per_minute_5m` directly so the "Schwabdev Calls / Min (5m)"
  chip reflects current activity instead of being poisoned by overnight
  idle hours. Per project's clean-breaks-over-shims convention. Dashboard
  signature filter excludes the 9 transient rate fields under the
  `('api_usage',)` path.
- **Dashboard chart UX**. Touch-input via pointer events (tap shows tooltip,
  drag moves it, tap persists until next gesture); `touch-action: pan-y` on
  `#market-chart`. Small-phone fallback at `≤480px` (single column, 44×44px
  tap targets, table cells wrap). Hardcoded `DASHBOARD_TIMEZONE =
  'America/New_York'` passed to all chart timestamp formatters. Tab
  `visibilitychange` listeners force immediate refresh on tab return.
- `runtime.max_consecutive_quote_failures` (default `5`): per-symbol
  quote-fetch failure threshold. Symbol is silenced from quote refresh after
  the threshold; recovers on bot restart. Set `0` for legacy always-retry.
- `_strategies.insufficient_bars_reason` promoted to public API. Cross-package
  consumers should `from ._strategies import insufficient_bars_reason`
  instead of reaching into `_strategies.helpers` directly.
- `anti_chase_fvg_retest_skip_vwap_ema9_reclaim` strategy param (default
  `false`). Drops the trend-MA half of the FVG `reclaimed` clause for
  microcap squeeze entries on deep retests where VWAP/EMA9 lag well above
  the FVG zone.

### Added

- **Fib retracement chart overlays (38.2% / 50% / 61.8% / 78.6%).**
  Pullback support levels within a bullish impulse range and bounce
  resistance levels within a bearish impulse range, drawn as dashed
  horizontal lines. Companion to the existing fib extension overlays
  (127.2% / 161.8%). New `show_fib_retracements` chart toggle in the
  `DashboardChartConfig` schema (default `false` compact, `true`
  expanded — paired with `show_fib_extensions`). 8 new
  `fib_bullish_382/500/618/786` and `fib_bearish_382/500/618/786`
  fields on `TechnicalLevelsContext`, computed alongside the
  existing extensions in `technical_levels.py` (no extra impulse
  detection — the same `bullish_impulse` / `bearish_impulse`
  segments drive both extension and retracement levels).

### Changed

- **Technical-levels overlays now follow the strategy's LTF.** The
  dashboard's `symbol_snapshot` previously built the technical_levels
  context (fibs, AVWAP, Bollinger, ADX, channels, trendlines, ATR
  context, OBV, RSI, divergences) from the 1m streamed frame
  unconditionally. With LTF/HTF separation done across the rest of
  the codebase, this was the last pinned-1m surface for derived
  overlays. Now uses the strategy's `params.ltf_minutes` frame
  (resampled via `data.get_merged(symbol, timeframe=f"{ltf_min}min")`
  when LTF != 1, otherwise the 1m streamed frame). For
  `peer_confirmed_key_levels` (LTF=5m) the chart's fib extensions /
  retracements / AVWAP / Bollinger / channels / trendlines all align
  with the 5m bars displayed in LTF chart mode. For default-LTF
  strategies behavior is unchanged.
- **`hourly_*` → `htf_*` rename (HTF concept, no shims).** All
  `hourly_*` strategy params, output keys, methods, and reason codes
  refer to the HTF context (HTF EMAs, HTF zone votes, HTF bias
  alignment) — not literally "the 1-hour timeframe". Renamed for
  consistency with the rest of the codebase's HTF/LTF naming:
  - **Strategy params (2)**: `require_hourly_bias_alignment` →
    `require_htf_bias_alignment`, `strong_setup_min_hourly_vote_edge` →
    `strong_setup_min_htf_vote_edge`.
  - **Method**: `_hourly_bias` → `_htf_bias`.
  - **Output keys (5)**: `hourly_bias` → `htf_bias`,
    `hourly_bull_votes` → `htf_bull_votes`, `hourly_bear_votes` →
    `htf_bear_votes`, `hourly_vote_edge` → `htf_vote_edge`,
    `hourly_vote_bonus` → `htf_vote_bonus`.
  - **Reason codes (5)**: `hourly_bias_bearish` → `htf_bias_bearish`,
    `hourly_bias_not_bullish` → `htf_bias_not_bullish`,
    `hourly_bias_bullish` → `htf_bias_bullish`,
    `hourly_bias_not_bearish` → `htf_bias_not_bearish`,
    `price_not_in_hourly_zone` → `price_not_in_htf_zone`.
  - Touched: 2 manifests (peer_confirmed_key_levels,
    peer_confirmed_key_levels_1m), 2 yaml configs, 3 strategy.py files
    (peer_confirmed_key_levels, peer_confirmed_trend_continuation,
    entry_gatekeeper), 2 READMEs.
- **Dashboard chart: AVWAP renders on HTF charts.** The expanded HTF
  chart was suppressing `show_anchored_vwap` along with the
  `show_ltf_*` toggles. AVWAP is a price-level overlay (horizontal
  line drawn at the anchored-VWAP price) that's valid regardless of
  chart bar timeframe — no reason to hide it on HTF. Removed
  `show_anchored_vwap` from the HTF suppression list in
  `dashboard.js`.
- **Diagnostics tab: bot uptime added.** The bottom-dock Diagnostics
  panel now shows "Bot Uptime" (formatted as `Nd HH:MM:SS` for runs ≥1
  day, `HH:MM:SS` otherwise) and "Started At" (raw ISO timestamp).
  Both derive from `data.started_at` which the engine has been
  emitting in the snapshot payload all along; the dashboard just
  wasn't surfacing it. New `fmtUptime()` helper in `dashboard.js`.
- **All hardcoded "1-minute" paths now follow the strategy's LTF.** Audit
  found four classes of stale 1m hardcodes after the LTF/HTF split, all
  cleaned in one cut:
  - **Real bugs (8 sites)**:
    - `dashboard_cache.py` chart-payload code passed
      `timeframe_minutes=1` and labelled FVGs/OBs `"1m"` even when the
      strategy's LTF was 5m. Both the FVG path (line 961) and the OB
      path (line 1033) now read `self._active_ltf_minutes()` and label
      payloads `f"{ltf_min}m"`.
    - `BaseStrategy._score_fvg_context` was called with
      `timeframe_minutes=1` from `strategy_base.py` (line 2634) and
      `zero_dte_etf_options/strategy.py` (line 577) — both now pass
      `self._ltf_minutes()`.
    - `dashboard.js` `ltfVisibilityFilter` rejected items whose
      `timeframe` label wasn't `'1m'`. With LTF=5m, every LTF FVG/OB
      had label `"5m"` and got dropped from the chart. Filter now
      compares against `ltfTimeframeLabel` derived from
      `chart.timeframe_minutes`. HTF FVG/OB filters likewise switched
      from `timeframe !== '1m'` to `timeframe !== ltfTimeframeLabel`.
  - **`_structure_context(frame, "1m")` → `_structure_context(frame, "ltf")`**
    in 13 strategy files (entry_gatekeeper, strategy_base, mean_reversion,
    pairs_residual, momentum_close, microcap_pm_breakout, closing_reversal,
    opening_range_breakout, rth_trend_pullback, top_tier_adaptive,
    volatility_squeeze_breakout, zero_dte_etf_options ×2). Default value
    of `_structure_context`'s `timeframe` parameter also flipped from
    `"1m"` to `"ltf"`. Strategies with LTF=1m get identical behavior;
    strategies with LTF≠1 (none today, but future-safe) get LTF-aware
    structure analysis automatically.
  - **Stale internal vars** renamed for consistency: `fvg1_score` →
    `fvg_ltf_score` (strategy_base, zero_dte_etf_options),
    `fvg1_ctx` → `fvg_ltf_ctx`, `ms1_ctx` → `ms_ltf_ctx`, `ms1_weight` →
    `ms_ltf_weight`, `ms1_fields` → `ms_ltf_fields`. Output keys
    `fvg_1m_*` → `fvg_ltf_*` (8 keys). Entry-decision metadata key
    prefix `'ms1m'` → `'msltf'` (used by `_structure_lists(prefix=...)`
    in 3 strategies + entry_gatekeeper). Test fixtures in
    `tests/test_bug_regressions.py` migrated to the new
    `msltf_pivot_count` key.
  - **Stale comments/docstrings** updated: `data_feed.py`
    `get_order_block_context` docstring now describes "LTF OBs" instead
    of "1m OBs"; `engine.py` and `strategy_base.py` example tuples for
    `_observed_contexts` use `("structure", "ltf")` instead of
    `("structure", "1m")`.
  - **Genuinely 1m-specific paths kept** (the literal 1-minute frame is
    correct in these): Schwab API `frequencyType="minute"` /
    `frequency=1` for streaming history; `support_resistance.py:563`
    `now_ts.floor("1min")` for the dual-frame flip cutoff;
    `utils.py:658` `ts.floor("1min")` utility; back-compat aliases for
    legacy `"1m"` chart-mode URL parameter (`dashboard.py`,
    `dashboard_cache.py`, `dashboard.html`); `session_report.py:786`
    already LTF-aware.
- **`trigger_*` → `ltf_*` rename (LTF-frame meaning only).** The
  `trigger_*` prefix was overloaded — sometimes meaning "the entry-trigger
  event" (verb), sometimes meaning "the LTF candle / trigger frame"
  (noun). Renamed only the noun-meaning items, with no shims:
  - **Strategy params (12)**: `trigger_quality_bonus_enabled` →
    `ltf_quality_bonus_enabled`, `trigger_quality_max_bonus` →
    `ltf_quality_max_bonus`, `trigger_reclaim_quality_bonus_cap` →
    `ltf_reclaim_quality_bonus_cap`, `trigger_zone_interaction_bonus_cap` →
    `ltf_zone_interaction_bonus_cap`, `trigger_candle_quality_bonus_cap` →
    `ltf_candle_quality_bonus_cap`, `trigger_volume_quality_bonus_cap` →
    `ltf_volume_quality_bonus_cap`, `trigger_range_expansion_bonus_cap` →
    `ltf_range_expansion_bonus_cap`, `min_trigger_score` → `min_ltf_score`,
    `min_trigger_close_position` → `min_ltf_close_position`,
    `min_trigger_volume_ratio` → `min_ltf_volume_ratio`,
    `min_trigger_bar_volume` → `min_ltf_bar_volume`,
    `strong_setup_min_trigger_score` → `strong_setup_min_ltf_score`.
  - **Entry-decision metadata keys (18)**: `trigger_score` → `ltf_score`,
    `trigger_base_score` → `ltf_base_score`, `trigger_quality_*` →
    `ltf_quality_*`, all `trigger_candle_*` (matches / anchor / score /
    net_score / opposite_score / regime_hint) → `ltf_candle_*`,
    `trigger_score_required` → `ltf_score_required`, `trigger_reasons` →
    `ltf_reasons`, `strong_setup_trigger_score_required` →
    `strong_setup_ltf_score_required`, `selection_trigger_score` →
    `selection_ltf_score`. **Skip-reason codes** also renamed:
    `weak_trigger_score` → `weak_ltf_score`, `trigger_score_below_min` →
    `ltf_score_below_min`, `trigger_bar_volume_below_min` →
    `ltf_bar_volume_below_min`.
  - **`BaseStrategy` methods (5)**: `_trigger_score` → `_ltf_score`,
    `_trigger_quality_bonus` → `_ltf_quality_bonus`, `_trigger_quality_caps`
    → `_ltf_quality_caps`, `_configured_trigger_candle_summary` →
    `_configured_ltf_candle_summary`, `_configured_trigger_candle_match`
    → `_configured_ltf_candle_match`.
  - **Internal vars** in the renamed methods: `trigger_min_score`,
    `trigger_window`, `trigger_sweep_window`, `trigger_preview`,
    `level_selection_trigger_score` → `ltf_*` equivalents.
  - **Kept (verb meaning)**: `adaptive_runner_trigger_rr`,
    `exit_trigger_level`, `_pullback_trigger_signal`,
    `_no_style_trigger_reason`, `trigger_lookback_bars` (rth_trend_pullback
    re-expansion trigger event), `trigger_high` / `trigger_low`,
    `trigger_level=` kwarg, locals `trigger_level` / `trigger_broke` /
    `trigger_kind` / `trigger_ref` / `trigger_slice` / `trigger_lookback`,
    `anti_chase_fvg_retest_trigger_tolerance_pct`. These all really mean
    "the thing that triggers entry" (verb), not the LTF frame.
  - 26 files touched in one cut: 5 manifests, 7 yaml configs, 4 strategy.py
    files (peer_confirmed_*, microcap_pm_breakout), `strategy_base.py`,
    `entry_gatekeeper.py`, 7 strategy READMEs + main README, 2 test files,
    `scripts/scaffold_strategy_plugin.py`. No back-compat aliases — old
    names removed entirely (per project's clean-breaks rule).
- **Unified flip-confirmation gate.** The two-mode design
  (`mode="dashboard"` for snappy 1-bar 1m feedback vs `mode="trading"`
  for the strict 2-bar-1m / 1-bar-5m dual-frame OR gate) collapses to a
  single trading-strict gate now that every consumer of the SR context
  uses the same flip strictness:
  - **Dashboard chart** zone-flip detection (`dashboard_cache.py` key
    level zones) switched from `dashboard_flip_confirmation_1m_bars=1` /
    `5m=0` to the trading values. Chart, sidebar (`sr_row()`), entry
    gatekeeper, position management, and strategy entries (`peer_confirmed_*`,
    `top_tier_adaptive`) now all see the same flip status — no path
    where the chart shows a level as broken before the strategy treats
    it as broken.
  - **Entry gatekeeper** (`entry_gatekeeper.py:414`) switched from
    `mode="dashboard"` to `mode="trading"`. Behaviorally a no-op (the
    gatekeeper only reads `sr_ctx.market_structure`, which is computed
    by `analyze_market_structure()` and doesn't depend on flip values),
    but consolidates the cycle-cache (`_cycle_sr_cache`) so the
    gatekeeper and `position_manager` share a single SR context build
    per `(symbol, tf)` instead of two.
  - **`mode="dashboard"` branch removed** from
    `MarketDataStore.get_support_resistance` — only `"trading"` and
    `"default"` modes remain. The `mode` parameter could be retired
    entirely in a follow-up.
  - **`SupportResistanceConfig.dashboard_flip_confirmation_1m_bars`
    removed entirely** (per project's clean-breaks-over-shims rule).
    The orphaned knob is gone from `config.py`, all 18 yaml configs,
    and the SR-config table in README.md.
- **LTF FVG / OB / structure retargeting.** Five SR-config knobs and one
  strategy param were renamed AND re-targeted from "always 1-minute"
  to "the strategy's LTF":
  - `support_resistance.one_minute_fair_value_gaps_enabled` →
    `ltf_fair_value_gaps_enabled`
  - `support_resistance.one_minute_order_blocks_enabled` →
    `ltf_order_blocks_enabled`
  - `support_resistance.structure_1m_pivot_span` → `structure_ltf_pivot_span`
  - `support_resistance.structure_1m_weight` → `structure_ltf_weight`
  - `dashboard.charting.{compact,expanded}.show_1m_fair_value_gaps` →
    `show_ltf_fair_value_gaps` (companion: `show_1m_order_blocks` →
    `show_ltf_order_blocks`)
  - Strategy param `one_minute_fvg_entry_weight` → `ltf_fvg_entry_weight`
  - **Behavior change**: FVG/OB/structure analysis now runs on the
    strategy's `params.ltf_minutes` frame (defaults to 1m streaming
    bars when not declared). For `peer_confirmed_key_levels` (LTF=5m),
    FVGs and OBs are now detected on 5m bars instead of 1m. For
    `peer_confirmed_key_levels_1m` and other strategies that default
    to 1m LTF, behavior is unchanged.
  - **Method renames**: `BaseStrategy._one_minute_fvg_context` →
    `_ltf_fvg_context`; `_one_minute_order_block_context` →
    `_ltf_order_block_context`.
  - **`MarketDataStore.get_fair_value_gap_context`** previously
    accepted a `timeframe_minutes` label but only ever computed on the
    1-minute merged frame. Now it resamples to the requested timeframe
    before building the FVG context (mirroring `get_order_block_context`).
  - **Structure-context gate**: `_structure_context` matches against
    the strategy's LTF via the new `_is_ltf_token()` helper instead of
    the hardcoded `{"1m","1min","minute","execution"}` set. The
    `structure_ltf_*` overrides now apply when the strategy is computing
    structure on its LTF frame regardless of LTF value.
  - **Dashboard chart payload keys**: `one_minute_fair_value_gaps` →
    `ltf_fair_value_gaps`; `one_minute_order_blocks` → `ltf_order_blocks`.
    Frontend `dashboard.js` migrated to read the new keys.
- **Bar-aligned HTF refresh.** `MarketDataStore.should_refresh_htf_context`
  no longer uses an elapsed-time throttle; it now refreshes on HTF bar
  boundaries with a 10-second settle buffer. New HTF data only arrives
  at HTF bar boundaries — within a single bar window the broker has
  nothing new to give us. For HTF=60m (base_freq=30m), at the 11:00
  boundary both 30m constituents of the just-closed 10:00-11:00 60m bar
  are already complete on the broker side, so a single fetch + resample
  produces the closed bar (no need for two 30m-aligned fetches). API
  reduction per symbol per HTF: 5m → 60% fewer fetches/hr; 15m → 87%
  fewer; 30m → 93% fewer; 60m → 97% fewer; 240m → 99% fewer.
  - **`htf_refresh_seconds` removed entirely.** Strategy params,
    manifests, yamls, READMEs, accessors, and the `refresh_seconds`
    parameter on `data_feed.get_*` / `prefetch_htf_contexts` /
    `should_refresh_*` all gone (per project's clean-breaks
    convention). Failure retries work naturally because
    `last_htf_refresh[key]` is only stamped on successful fetch+merge —
    a failed fetch leaves the bar window "due" so the next tick retries.
  - **Cycle-cache key on `get_support_resistance` simplified** —
    `refresh_seconds` dropped from the cycle key tuple since it no
    longer parameterizes behavior.
  - **`current_structure_overlay` no longer rebuilds a full SR context.**
    Calls `support_resistance.analyze_market_structure(frame, ...)`
    directly to extract the CHOCH/BOS overlay without re-running pivot
    detection, S/R clustering, prior-day/week, FVG checks, broken-level
    reconciliation, or proximity metrics — all the work that the
    overlay path threw away. Eliminates a duplicate per-render rebuild
    on every dashboard chart payload.
- **Engine cycle parallelization.** Per-symbol Schwab fetches (history, S/R
  refresh, quote fallback) now run via `_parallel_symbol_map` and
  `_parallel_quote_fetch`. Strategy context caches (`_chart_context`,
  `_structure_context`, `_technical_context`) pre-warm in parallel via the
  new `prime_cycle_contexts(frame, observed)` hook;
  `BaseStrategy._observed_contexts` lazily records context shapes on first
  invocation. Three `RLock`s guard the per-context caches.
  `cycle_precompute_workers` runtime knob controls the thread pool size.
  Per-cycle API rate is unchanged — only burst pattern compressed. 0DTE
  strategies parallel-prefetch option chains (up to 4 workers, scaled to
  miss count) via the new `_fetch_raw_option_chain` helper that splits I/O
  + cache from put/call + liquidity filtering. `startup_reconciler.reconcile`
  parallelizes `account_details` + `account_orders` (~200-400ms boot stall
  saved).
- **Cycle-scoped broker positions cache.** `entry_gatekeeper` fetches
  `account_details` at most once per `engine.step()` regardless of how many
  `broker_position_row` / `broker_position_rows` consumers run inside the
  cycle. New `force_refresh=True` keyword bypasses the cache; backed by a
  `_fetch_broker_positions_uncached()` helper that's the single source of
  truth for the underlying call shape. Failure latches per-cycle to avoid
  retry storms during Schwab outages. New `begin_cycle()` / `end_cycle()`
  lifecycle hooks mirror per-cycle FVG/OB/S-R caches in `data_feed.py`.
  Order block context is also cycle-cached on `MarketDataStore` via
  `_cycle_ob_cache` and `get_order_block_context()`, eliminating ~260
  redundant `build_order_block_context` calls per minute when strategy +
  dashboard both consume OBs.
- **Dashboard HTTPS perf.** HTTP/1.1 keep-alive (`protocol_version =
  "HTTP/1.1"`) with 30s idle timeout — one TCP+TLS connection per browser
  tab instead of fresh pair per request, eliminating first-load stall under
  HTTPS. TLS handshake offloaded to per-request worker thread
  (`do_handshake_on_connect=False` + 5s handshake timeout) so concurrent
  clients handshake in parallel. New `ReusableThreadingHTTPServer.handle_error`
  silences common transport-layer exceptions (`ssl.SSLError`,
  `ConnectionError`, `BrokenPipeError`, `socket.timeout`) at DEBUG level
  instead of dumping full tracebacks to journald.
- **FVG knob consolidation.** Six `htf_*` / `ltf_*` FVG knobs
  collapsed into three shared knobs (`fair_value_gap_max_per_side`,
  `fair_value_gap_min_atr_mult`, `fair_value_gap_min_pct`); both timeframes
  read the same fields. Enable flags
  (`htf_fair_value_gaps_enabled`, `ltf_fair_value_gaps_enabled`)
  remain timeframe-specific. Per project's clean-breaks-over-shims
  convention, the old field names are removed entirely from
  `SupportResistanceConfig`. Mirrors the OB knob consolidation.
- **Helpers.py extraction.** Pure stateless helpers (numeric coercion,
  bar/DataFrame shape utilities, premium clamping, symbol normalization,
  reason formatters, structured-logging payload builder, dashboard
  zone-width policy) extracted from `BaseStrategy` into `_strategies/helpers.py`
  (~622 LOC, 29 functions in 7 sections). `BaseStrategy` shrunk by ~350 LOC.
  ~570 callsites updated across 16 files to import via `..shared`. Class-level
  delegation methods removed entirely. Test patches must switch from
  `patch.object(strategy, "_method", …)` to
  `patch("intraday_tv_schwab_bot._strategies.strategy_base._method", …)`
  (patch the module function, not the class attribute).
- **`peer_confirmed_key_levels` retune.** Always-on profile:
  `auto_exit_after_session: false`, `startup_reconcile_mode: restore_hybrid`,
  entry/management/screener windows expanded to `07:00-19:55` ET,
  `time_stop_minutes: 0`. API-cost retune for extended hours:
  `history_poll_seconds: 300`, `stream_stale_fallback_seconds: 180`.
  HTF refresh is bar-aligned (one Schwab call per HTF bar boundary), so
  the prior `htf_refresh_seconds` knob is gone — see the bar-aligned
  HTF refresh note in **Changed**. Strategy quality filters (min trigger
  score, peer agreement, macro net bias) gate extended-hours candidates
  organically.
- **Dashboard render polish.** `dashboard_recent_trade_markers()` and
  `dashboard_symbol_trade_signature()` filter trades by symbol BEFORE
  slicing (a fresh fill on a long-quiet symbol could otherwise be
  invisible); marker function also filters to today's ET session date.
  Chart payload `last_update` re-stamps on every cache hit so frontend
  timestamp doesn't freeze. iOS `:hover` rules wrapped in `@media (hover:
  hover)` so taps don't stick. Mobile poll cadence floor raised to 4000ms
  (cellular radio savings); honors server-provided `dashboard.refresh_ms`
  when slower than the floor. Order block ranking is strength-based
  (thrust × size × age × validity) when `max_per_side` clips —
  `nearest_bullish_ob` / `nearest_bearish_ob` accessors still resolve
  nearest-by-price for retest-plan consumers. `OrderBlock` enrichment uses
  `dataclasses.replace()` on the slotted dataclass.
- **Plugin scaffold + package hygiene.** `scripts/scaffold_strategy_plugin.py`
  emits 9 FVG knobs + `force_flatten` in the generated manifest, plus SPDX
  headers in scaffolded `__init__.py` / `strategy.py` / `screener.py`. Plugin
  templates updated to call `insufficient_bars_reason(...)` and
  `_safe_float(...)` as free functions (post-helpers.py extraction).
  `top_tier_adaptive` imports `now_et` / `parse_hhmm` from `..shared`.
  `intraday_tv_schwab_bot.__all__` trimmed from 29 unreachable entries to
  `["__version__"]`. `config.__all__` switched to mechanical
  "no-leading-underscore = public" rule (20 entries). `.gitattributes`
  upgraded `* text=auto` → `* text=auto eol=lf` to stop Windows
  phantom-modified states under `core.autocrlf=true`.
- Removed `BaseStrategy._apply_continuation_fvg_retest_plan` (the
  single-plan apply helper that predated OR-combine). All 12 callers across
  4 strategies migrated to `_apply_continuation_zone_retest_plans` with a
  single-element plan list. Removed redundant `dashboard_candidate_levels`
  override in `peer_confirmed_htf_pivots` (returned `[]` matching base
  default). Engine `_cycle_sleep_seconds` collapsed from three branches to
  two (stream-on / stream-off).

### Fixed

- **HTF prior-day/week levels now always candidates, not just fallbacks**
  (`htf_levels.py build_htf_context`). The previous flow only injected
  `prior_day_low` / `prior_week_low` (and the high counterparts) when
  pivot detection produced an empty result. In strong directional moves
  a stock can rally for weeks with no proper pivot lows in the rally
  portion (each bar's low > the surrounding bars' lows by definition of
  an uptrend), so pivot-only support detection surfaces only the ancient
  base. AMD example: rally from $258 → $346 over a week with no pivot
  lows in the rally; the dashboard showed first support at $254 (a
  pivot low from the base period weeks earlier) instead of yesterday's
  $340 low. Both prior-day and prior-week levels now merge into the
  candidate pool alongside pivot levels via `_extend_unique_levels`,
  then compete in `_collapse_same_side_levels`. Their `source_priority`
  of 2.0 (prior_day) / 3.0 (prior_week) outranks pivot's 1.0 in
  `_level_preference`, so when a prior-day/week level overlaps a
  same-cluster pivot, the prior-day/week level wins the picker. The
  second-chance fallback (when filtered candidates are empty) and
  frame-extreme fallback (when both pivots and prior-day/week are
  empty) are preserved as-is for fully-empty edge cases.
- **HTF level scoring now time-aware** (`htf_levels.py _cluster_levels`).
  The previous formula computed `score = touches + min(1.5, 0.15 * touches)`
  — a misleadingly-named "recency_bonus" that was actually a touches
  multiplier with no time component at all. With a 60-day HTF lookback,
  ancient base levels with many touches accumulated during long
  consolidations dominated the top-N selection, evicting recent close-
  to-price swing lows before they reached `_collapse_same_side_levels`.
  AMD example: current price $346.50 with first support showing at
  $257.73 (a 30+-day-old base) instead of the recent $320-$343 swing
  lows. Replaced with the time-aware formula already used in
  `support_resistance.py _cluster_levels`: `recency_factor` decays
  linearly from `1.0` (newest) to `0.10` (oldest) across the cluster
  window, `effective_touches = touches * recency_factor`, plus a
  persistence bonus that rewards levels held across a sustained portion
  of the window. A 30-day-old 8-touch base now contributes ~4 effective
  touches — comparable to a fresh 4-touch swing low — so both survive
  top-N selection and the dashboard renders the full ladder of recent
  + historical levels.
- **HTF in-memory resample reverted.** An earlier attempt at this release
  added a path that rebuilt HTF bars by resampling the in-memory 1m frame
  with a periodic Schwab audit (`htf_audit_refresh_seconds: 3600`).
  Reverted because the convention mismatch between the in-memory path
  (1m bars resampled with `closed="right"` → bars represent ~10:01-11:00
  data) and the Schwab path (30m bars from `price_history` resampled the
  same way → bars represent 10:30-11:30 data) produced inconsistent OHLC
  in the merged HTF frame. Pivot detection on the inconsistent frame
  surfaced wildly stale support/resistance levels (e.g., AMD with current
  price $346 showing first support at $258 from a 30-day-old base). The
  audit knob, `_try_resample_htf_from_live_1m`, `_htf_audit_due`, and
  `last_htf_audit_refresh` tracker are removed entirely. The
  heal-propagation hook on `fetch_history` is preserved (Added section)
  since it's useful regardless of the rebuild path.
- **Strategy correctness.** `peer_confirmed_key_levels._select_level`
  "touched zone" check replaced with per-bar overlap (window-wide
  `low.min()`/`high.max()` could pass when no individual bar's range
  overlapped the zone — fires during news/fast-spike conditions). OB
  detection walk-back uses `continue` instead of `break` so a small doji at
  idx-1 doesn't abort the search for a real OB at idx-2+;
  `_merge_order_blocks` first/last_seen now uses explicit `_earlier`/`_later`
  ISO-timestamp helpers (sort is by price, not chronology). `_optional_int`
  parses float-strings like `"3.7"` → `3` (was returning default).
- **Dashboard chart.** `paint()` uses `activeIndex` consistently (latent
  crash on bar-pinning land — `bars[hoverIndex]` was a stale global).
  `renderEmpty` cancels pending hover-RAF before detaching pointer handlers
  (previously a queued `requestAnimationFrame` from a previous chart could
  draw ghost data after canvas clear). Tap-and-release tooltip persists
  until next gesture (was clearing on finger lift). Volume bars use
  `parseFinite()` (was `Number()` which coerced `"NaN"` strings to `NaN`).
  Theme `<link>` `onerror="this.remove()"` falls back to base styling on
  404. Spread pill no longer flickers visible/hidden between stream ticks
  (now matches sibling pills with `—` placeholder).
- **Mobile dashboard.** `.position-card` cursor override (was `pointer`
  from desktop with no click handler). `.panel-meta` text-wrap fix for
  7-figure equity. `.positions-panel` explicit `position: relative` (no
  longer dependent on desktop's `≤1400px` breakpoint). Qty rendering uses
  `fmtInteger()` (was `escapeHtml()` producing literal `"null"`).
- **Memory + state hygiene.** `data_feed.prune_inactive_symbols` evicts
  the `last_htf_refresh` tuple-keyed dict alongside the rest of the
  per-symbol state.
- **Config + manifest hygiene.** All 18 prod presets + `config.example.yaml`
  expose the eight OB knobs (defaults safe-off) and the three always-on
  knobs (`idle_sleep_seconds`, `symbol_state_prune_seconds`,
  `session_reconcile_on_resume`). README runtime table + behavior
  section updated with the new knobs.
- **Logging + cosmetic.** `dashboard_cache.py log_component_failure` calls
  in OB blocks pass symbol as arg (was printf message). ORB `none`-mode
  activity score rescaled to `rvol × volume / 1_000_000` so log magnitudes
  match other branches. Dashboard `focus-meta` uses compact entry-decision
  label so long ETF skip reasons don't push live-data chips off the card.
  Three IDE / type-checker warnings cleaned up (redundant
  `self.stream = None`, two unused `_ltf_order_block_context` params).

## [1.0.0] — 2026-04-24

Initial public release.

### Infrastructure

- `requirements.txt` strictly pinned to verified versions.
- `pyproject.toml` with setuptools build backend and dynamic version
  from `version.txt`.
- Tests maintained privately in the source tree; not shipped with this
  repository.

[Unreleased]: https://github.com/OWNER/REPO/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/OWNER/REPO/releases/tag/v1.0.0
