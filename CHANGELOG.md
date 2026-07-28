# Changelog

All notable changes to `intraday-tv-schwab-bot` will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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
