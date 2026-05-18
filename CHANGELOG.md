# Changelog

All notable changes to `intraday-tv-schwab-bot` will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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
