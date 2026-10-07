# SPDX-License-Identifier: MIT
"""Multi-regime adaptive intraday strategy for top-tier liquid stocks.

Detects whether each symbol is trending, pulling back, ranging, breaking
out of a volatility squeeze, sustaining momentum from the session open,
or scalping between HTF S/R zones — then applies the appropriate entry
style. Index confirmation uses a per-sector ETF map (see
``sector_index_map`` + ``_indices_for_symbol``) so each symbol is gated
by its actual sector tape, not an arbitrary broad-market ETF. Trades
both long and short across the full RTH session with time-of-day regime
gating.

The engine is one class spread over this package, each part a mixin of
``TopTierAdaptiveStrategy``: ``schedule.py`` (the time-of-day windows),
``confirmation.py`` (sector confirmation, daily statistics, the side vote,
the live bias), ``armed_retest.py`` (the wait for a breakout's retest) and
``regimes/`` (one scorer and builder per regime). This module holds the
class, the gate chain every builder ends in (``_finalize_signal``) and the
entry loop.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import time
from typing import Any

import pandas as pd

from ...bars import same_day_mask
from ...indicators import get_session_indicator_window, last_bar_atr, ltf_ema_spans
from ...models import Candidate, Position, Side, Signal
from ...sessions import EQUITY_RTH_OPEN, equity_session_state, parse_hhmm
from ...numeric import safe_float
from ...reasons import insufficient_bars_reason
from ...support_resistance import role_levels
from ... import sessions
from ..shared_entry import EntryContexts, EntryProposal
from ..strategy_base import BaseStrategy
from ...daily_stats import SymbolDailyStats
from .armed_retest import ARMED_RETEST_REGIMES, ArmedRetestMixin
from .confirmation import ConfirmationMixin
from .regimes.momentum import MomentumRegimeMixin
from .regimes.orb import OrbRegimeMixin
from .regimes.pullback import PullbackRegimeMixin
from .regimes.range import RangeRegimeMixin
from .regimes.sr_scalp import SrScalpRegimeMixin
from .regimes.trend import TrendRegimeMixin
from .regimes.vol_squeeze import VolSqueezeRegimeMixin
from .regimes.vwap_reclaim import VwapReclaimRegimeMixin
from .schedule import ScheduleMixin

LOG = logging.getLogger(__name__)

# Regime families. Several gates apply to one family and deliberately exempt
# another, so the membership lives here once instead of being re-spelled at
# each call site.
#
# MEAN_REVERSION_REGIMES enter AGAINST current price action by design (buy the
# range low, buy the support bounce). Every gate that asks "is price already
# moving my way?" — index confirmation, the confirmation bar, and the
# _decide_side vote — must exempt them or the regimes cannot fire at all.
MEAN_REVERSION_REGIMES = frozenset({"range", "sr_scalp"})
# Need a market-aligned tape.
INDEX_CONFIRMED_REGIMES = frozenset({"trend", "pullback", "vol_squeeze", "momentum", "vwap_reclaim"})
# Need the last CLOSED bar to point the trade's way. vwap_reclaim is excluded
# because its prior closed bar is the flush; orb because its range-break is
# the confirmation.
CONFIRMATION_BAR_REGIMES = frozenset({"trend", "pullback", "vol_squeeze", "momentum"})
# Regimes whose side must match the _decide_side vote. Everything except the
# mean-reversion pair and orb (which carries its own bypass because the
# opening tape is gap-dominated).
SIDE_DECISION_REGIMES = frozenset({"trend", "pullback", "vol_squeeze", "momentum", "vwap_reclaim"})

# Short labels for the per-regime scores in the
# ``<side>_unqualified_no_qualifying_regime(...)`` skip reason. Keys must
# cover every key of the per-side ``scores`` dict; iteration order here is
# the order the scores are printed in.
REGIME_SKIP_LABELS = {
    "trend": "trend",
    "pullback": "pb",
    "range": "range",
    "vol_squeeze": "squeeze",
    "momentum": "mom",
    "sr_scalp": "sr_scalp",
    "orb": "orb",
    "vwap_reclaim": "vwap_reclaim",
}


@dataclass
class _EntryCycle:
    """What ``entry_signals`` reads once per cycle for every candidate."""
    now_t: time
    allowed_regimes: set[str]
    in_orb_window: bool
    allow_short: bool
    sides_to_evaluate: list[Side]
    ext_hours_now: bool
    ext_all: bool
    ext_allowed: set[str]
    min_bars: int
    ltf_min: int
    ltf_span_scale: float
    ltf_ema_pair: tuple[int, int]
    min_ltf_bars: int


@dataclass
class _CandidateRead:
    """One candidate's reads for this cycle (``_read_candidate``): what the
    scoring pass and the builds share."""
    c: Candidate
    frame: pd.DataFrame
    ltf: pd.DataFrame
    close: float
    vwap: float
    ema9: float
    ema20: float
    adx: float
    ret5: float
    ret15: float
    atr: float
    vol_scale: float
    index_ok_by_side: dict[Side, bool]
    htf_ema_read: tuple[str, int, int]
    idx_neutral: bool
    day_strength: float | None
    effective_bias: Side | None
    respect_bias: bool
    preferred_sides: list[Side]
    explicit_side_decided: bool
    decided_side: Side | None
    side_vote_note: str
    thresholds: dict[str, float]
    tech_ctx_for_candidate: Any
    vol_widening: float


class TopTierAdaptiveStrategy(ScheduleMixin, ConfirmationMixin, ArmedRetestMixin,
                              TrendRegimeMixin, OrbRegimeMixin, PullbackRegimeMixin, RangeRegimeMixin,
                              VolSqueezeRegimeMixin, MomentumRegimeMixin, VwapReclaimRegimeMixin,
                              SrScalpRegimeMixin, BaseStrategy):
    strategy_name = "top_tier_adaptive"
    time_params = ("orb_end_time", "midday_start_time", "midday_end_time", "afternoon_start_time",
                   "no_new_entries_after", "early_session_stop_widening_until")

    def __init__(self, config):
        super().__init__(config)
        # Per-symbol rolling window of recent LIVE directional bias
        # observations (output of ``_compute_live_directional_bias``).
        # Trailing-bias memory for Fix A (2026-04-23): when the current
        # bar's live bias is None (day_strength within the neutral band),
        # infer the effective side from recent observations if one side
        # dominates. One observation per LTF bar -- the bar it was read on
        # is kept alongside so a later cycle on the same bar updates it.
        self._recent_directional_bias: dict[str, deque[Side | None]] = {}
        self._recent_directional_bias_bar: dict[str, pd.Timestamp] = {}
        # Per-symbol timestamp of the most recent stretched_at_top
        # (or short stretched_at_bottom) build failure. Drives the
        # hysteresis gate in ``_finalize_signal`` so a candidate that
        # just failed the stretched check can't immediately re-arm
        # on a single tick across the threshold. Session 2026-05-26:
        # AMZN rejected at 10:11:41 with pct_b=0.851 (0.001 over the
        # 0.85 cutoff), entered 46 s later as the bar's close ticked
        # back across. ``Any`` to avoid pulling in datetime at module
        # scope under ``from __future__ import annotations``.
        self._stretched_failure_time: dict[str, Any] = {}
        # Per-symbol daily stats (ADR scale + sector beta), rebuilt when the
        # ET date rolls. Keyed by symbol; ``_daily_stats_date`` is the ET
        # date the cache was built for.
        self._daily_stats: dict[str, SymbolDailyStats] = {}
        self._daily_stats_date: Any = None
        # Armed breakout triggers, keyed ``symbol|SIDE|regime``. A regime in
        # ARMED_RETEST_REGIMES that qualifies does NOT enter on that cycle; it
        # records the level it cleared and waits for price to come back and
        # retest it. Survives across cycles by design -- this is the only
        # cross-cycle entry state in the strategy -- and is pruned on session
        # rollover and on expiry.
        #
        # In memory only. A restart mid-session loses every arm, and the
        # affected symbols re-arm on their next qualifying cycle -- so the
        # worst case is one ``armed_retest_max_minutes`` delay on the first
        # trend/momentum entry after a restart, which the market fallback
        # bounds. Not worth persisting: a stale arm reloaded against a level
        # that has since been swept is worse than re-deriving it.
        self._armed_retests: dict[str, dict[str, Any]] = {}
        self._validate_orb_window()
        # A bad ltf_ema_fast_span / ltf_ema_slow_span or
        # require_htf_ema_alignment fails here, naming the key, rather than on
        # the first entry cycle.
        ltf_ema_spans(self.params)
        self._htf_ema_gated_sides()

    def _htf_ema_gated_sides(self) -> frozenset[Side]:
        return self._htf_ema_alignment_sides(self.params.get("require_htf_ema_alignment", "disabled"))

    def _htf_ema_score_term(self, side: Side, bias: str, in_orb_window: bool) -> float:
        """``htf_ema_alignment_score`` added to (aligned) or taken from
        (opposed) a side's regime scores; 0 inside the ORB-window HTF
        bypass, like the gate."""
        weight = float(self.params.get("htf_ema_alignment_score", 0.0))
        if not weight or (bool(self.params.get("orb_bypass_htf_bias", True)) and in_orb_window):
            return 0.0
        if (side == Side.LONG and bias == "bullish") or (side == Side.SHORT and bias == "bearish"):
            return weight
        if (side == Side.LONG and bias == "bearish") or (side == Side.SHORT and bias == "bullish"):
            return -weight
        return 0.0

    def dashboard_htf_trend(self, symbol: str, data, price: float) -> dict[str, str] | None:
        """The HTF EMA trend the gate and score read, off the same context
        (``_default_htf_context_for_score``)."""
        if data is None or not price:
            return None
        return self._htf_trend_row(*self._htf_bias(self._default_htf_context_for_score(symbol, data), float(price)))

    def daily_history_symbols(self, watchlist: list[str]) -> list[str]:
        """The symbols ``_symbol_daily_stats`` reads the daily history of,
        for a candidate from ``watchlist``: the symbol itself and its
        benchmark, the first of its index ETFs (``_indices_for_symbol``).
        The engine fetches them on its fetch pool from the prewarm on
        (``IntradayBot._prefetch_daily_history``); until 2026-09-28 the
        day's first entry pass fetched them, one at a time."""
        symbols: set[str] = set()
        for symbol in watchlist:
            key = str(symbol).upper().strip()
            if not key:
                continue
            symbols.add(key)
            indices = self._indices_for_symbol(key)
            if indices:
                symbols.add(indices[0])
        return sorted(symbols)

    # ------------------------------------------------------------------
    # Watchlist — include all configured index confirmation ETFs so they
    # get history fetching, streaming, and appear in the bars dict. The
    # specific ETFs depend on which sectors the active universe touches
    # (see ``sector_index_map`` in config) — could be sector ETFs
    # (XLK/XLE/XLB/...) and/or broad-market (SPY/QQQ).
    # ------------------------------------------------------------------
    def active_watchlist(self, candidates: list[Candidate], positions: dict[str, Position]) -> set[str]:
        symbols = super().active_watchlist(candidates, positions)
        symbols.update(self._index_symbols())
        return symbols

    # ------------------------------------------------------------------
    # Adaptive-ladder rung override
    #
    # Trend and pullback regimes lend themselves to laddering — the trade
    # thesis is "ride momentum through successive resistance levels" so the
    # generic S/R-based rung builder works as-is. Range trades are the
    # opposite: the thesis is mean-reversion bounded by range_low and
    # range_high. Laddering past the range high would chase a breakout
    # that contradicts the entry, so we return [] and let the signal keep
    # its single, range-bounded target.
    # ------------------------------------------------------------------
    def _build_ladder_rungs(self, side, close, stop, atr, sr_ctx, *, regime=None):
        if str(regime or "").strip().lower() == "range":
            return []
        return super()._build_ladder_rungs(side, close, stop, atr, sr_ctx, regime=regime)

    # ------------------------------------------------------------------
    # required_history_bars
    # ------------------------------------------------------------------
    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        return max(0, int(self.params.get("min_bars", 60) or 60))

    def _breakout_reference(
        self, regime: str, side: Side, ltf: pd.DataFrame, frame: pd.DataFrame,
    ) -> tuple[pd.DataFrame | None, float | None]:
        """The N-bar extreme a breakout regime has to clear, and the window it
        came from.

        Single source for two readers that must agree: the builder's own
        fresh-breakout check, and the armed-retest level in
        ``_armed_retest_verdict``. A second copy of this arithmetic would drift
        the moment either lookback was retuned, and the failure would be
        silent — the bot would arm on one level and enter against another.

        ``trend`` reads the LTF (25 bars at ``ltf_minutes: 1``); ``momentum``
        reads the base 1m frame (6 bars), matching the standalone
        momentum_close strategy it was generalised from. Both are scoped to
        today's session, because the resampled LTF crosses the session
        boundary during early RTH.
        """
        if regime == "trend":
            source, lookback = ltf, max(3, int(self.params.get("pullback_lookback_bars", 5)))
        elif regime == "momentum":
            source, lookback = frame, max(3, int(self.params.get("momentum_breakout_lookback_bars", 6)))
        else:
            return None, None
        if source is None or source.empty:
            return None, None
        session = source[same_day_mask(source, sessions.now_et().date())]
        recent = session.tail(lookback + 1).iloc[:-1] if len(session) > lookback else session.iloc[:-1]
        if recent.empty:
            return None, None
        if side == Side.LONG:
            level = safe_float(recent["high"].max(), float("nan"))
        else:
            level = safe_float(recent["low"].min(), float("nan"))
        if level is None or not math.isfinite(float(level)):
            return recent, None
        return recent, float(level)

    def _finalize_signal(self, c: Candidate, side: Side, close: float, stop: float,
                         target: float, regime: str, regime_score: float,
                         frame: pd.DataFrame, data=None, vol_scale: float = 1.0,
                         raw_rr_gate: str | None = None) -> Signal | None:
        """Run top_tier's own gates, hand the entry to the shared entry stage,
        then build the ladder / runner / management on the levels it admitted
        and emit the Signal.

        Order: the top_tier-only gates (Fix D, stretched / tech bias, the ORB
        5m follow-through, HTF bias / pivot, HTF EMA, the ORB opposing-level
        block) -> ``entry_policy.admit`` (the raw R:R gate the builder asked
        for with ``raw_rr_gate``, the switched-on shared vetoes minus the
        manifest's exemptions for the regime, the S/R and technical stop /
        target refinement, the shared score terms) -> entry exhaustion ->
        bonuses, ladder, trail runner, Fix G, management -> ``emit``. Every
        refusal lands under ``regime`` (the proposal's style), which the
        queue loop in ``_run_build_queue`` consumes before falling through
        to the next regime. The broken-level guard that sat here until
        2026-09-24 (``reject_entry_near_broken_level``) is the shared
        ``shared_entry.use_broken_level_guard`` veto now.
        """
        sr_ctx = self._sr_context(c.symbol, frame, data)
        ms_ctx = self._structure_context(frame, "ltf")
        tech_ctx = self._technical_context(frame)
        ctx = self._chart_context(frame)
        htf_ctx = self._default_htf_context_for_score(c.symbol, data)

        # Single ORB-window flag reused by the _finalize_signal ORB-bypasses
        # (HTF bias, HTF EMA, ORB 5m follow-through, exhaustion). Computed
        # once here to avoid duplicate sessions.now_et() calls with potential
        # clock-skew at the 10:05 boundary.
        orb_end = parse_hhmm(self.params.get("orb_end_time", "10:05"))
        in_orb_window = self._in_orb_window(sessions.now_et().time())

        # Fix D — reject stretched / contradicted entries before expensive
        # signal refinement. Applies to trend / pullback / momentum only;
        # range + sr_scalp are mean-reversion ("stretched at top" IS the
        # setup), and the orb regime's range-break is its own directional
        # proof. None of {range, sr_scalp, orb} are subject to these gates, so
        # the old orb_bypass_stretched_filter / _tech_bias_contradiction /
        # _oversized_entry_bar companions were removed (2026-05-29) — they only
        # ever loosened these gates for trend/pullback/(sr_scalp), which no
        # longer run in the ORB window now that orb is its own regime.
        # Bar-size gate (2026-05-14) — reject entries when the latest 1m entry
        # bar's range or body is far above its recent ATR (measured on the base
        # 1m frame via tech_ctx.atr14). A violent expansion bar is a poor entry:
        # it tends to mean-revert and you fill near its high/low — an already-
        # done move. Catches both the
        # "big total spread / wicky" bar and the "big directional thrust"
        # bar via separate range/body thresholds. Applies to regimes that
        # need clean entries (trend / pullback / sr_scalp); range /
        # vol_squeeze / momentum are exempt because big bars ARE the setup
        # for those. ORB-bypassed by default — opening flush bars are
        # always huge and other ORB-stretch gates already handle that
        # window.
        if regime in {"trend", "pullback", "sr_scalp"}:
            if bool(self.params.get("reject_oversized_entry_bar", True)):
                atr14 = safe_float(getattr(tech_ctx, "atr14", None))
                if frame is not None and not frame.empty and atr14 is not None and atr14 > 0.0:
                    last_bar = frame.iloc[-1]
                    bar_high = safe_float(last_bar.get("high"))
                    bar_low = safe_float(last_bar.get("low"))
                    bar_open = safe_float(last_bar.get("open"))
                    bar_close = safe_float(last_bar.get("close"))
                    range_atr = (
                        (bar_high - bar_low) / atr14
                        if (bar_high is not None and bar_low is not None)
                        else 0.0
                    )
                    body_atr = (
                        abs(bar_close - bar_open) / atr14
                        if (bar_close is not None and bar_open is not None)
                        else 0.0
                    )
                    range_max = float(self.params.get("entry_bar_range_max_atr_mult", 1.8))
                    body_max = float(self.params.get("entry_bar_body_max_atr_mult", 1.4))
                    range_violated = range_atr >= range_max
                    body_violated = body_atr >= body_max
                    if range_violated or body_violated:
                        side_prefix = "long" if side == Side.LONG else "short"
                        self._set_build_failure(
                            c.symbol, regime,
                            f"{side_prefix}_oversized_entry_bar(range={range_atr:.2f}>={range_max:.2f},body={body_atr:.2f}>={body_max:.2f})",
                        )
                        return None
        # 2026-05-29: momentum added to the gated set. The momentum regime
        # bypassed this entire block, so AVGO 11:13 LONG entered at
        # pct_b=0.890 / atr_stretch=2.26 (well above the 0.85 / 1.30
        # thresholds) — stopped out -$91.80 in 8 minutes. Trend/pullback
        # were correctly cooldown-rejecting in the same cycle; momentum was
        # the only regime that could fire. The momentum-from-open thesis is
        # "strong day_strength + breakout from N-bar high", but at extreme
        # pct_b/stretch the breakout is already over-extended and the chase
        # gets stuffed. Now momentum honors the same hysteresis + cooldown
        # as trend/pullback.
        if regime in {"trend", "pullback", "momentum"}:
            if bool(self.params.get("reject_stretched_entries", True)):
                # Hysteresis (2026-05-26). Once a candidate has failed the
                # stretched check within the cooldown window, keep rejecting
                # without re-evaluating thresholds. The 0.85 pct_b / 1.30
                # ATR-stretch cutoffs are crisp — a single tick across the
                # threshold relaxes them while the structural condition
                # (price stretched above EMA20 / pinned near upper Bollinger
                # band) is still active. Session 2026-05-26 AMZN was
                # rejected at 10:11:41 with pct_b=0.851 then entered 46 s
                # later as the bar's close ticked back across, losing $28.
                # The cooldown is per-symbol regardless of side — opposite-
                # side entries during this window are rare in practice
                # (a stretched-top symbol won't pass stretched-bottom) and
                # keeping a single timestamp avoids extra state.
                cooldown_min = float(self.params.get("stretched_cooldown_minutes", 3.0))
                if cooldown_min > 0:
                    last_fail = self._stretched_failure_time.get(c.symbol)
                    if last_fail is not None:
                        elapsed_min = (sessions.now_et() - last_fail).total_seconds() / 60.0
                        if elapsed_min < cooldown_min:
                            side_prefix = "long" if side == Side.LONG else "short"
                            self._set_build_failure(
                                c.symbol, regime,
                                f"{side_prefix}_stretched_cooldown("
                                f"elapsed={elapsed_min:.1f}m<{cooldown_min:.1f}m)",
                            )
                            return None
                pct_b = safe_float(getattr(tech_ctx, "bollinger_percent_b", None))
                atr_stretch = safe_float(getattr(tech_ctx, "atr_stretch_ema20_mult", None))
                pct_b_max = float(self.params.get("stretched_percent_b_max", 0.80))
                stretch_max = float(self.params.get("stretched_atr_mult_max", 1.1))
                if (
                    side == Side.LONG
                    and pct_b is not None and atr_stretch is not None
                    and pct_b >= pct_b_max and atr_stretch >= stretch_max
                ):
                    self._stretched_failure_time[c.symbol] = sessions.now_et()
                    self._set_build_failure(
                        c.symbol, regime,
                        f"long_stretched_at_top(pct_b={pct_b:.3f}>={pct_b_max:.2f},stretch={atr_stretch:.2f}>={stretch_max:.2f})",
                    )
                    return None
                if (
                    side == Side.SHORT
                    and pct_b is not None and atr_stretch is not None
                    and pct_b <= (1.0 - pct_b_max) and atr_stretch >= stretch_max
                ):
                    # atr_stretch_ema20_mult is abs(close-ema20)/atr14 — always
                    # non-negative (computed in build_technical_levels_context).
                    # The direction
                    # (above vs below EMA20) is captured by bollinger_percent_b:
                    # pct_b <= 0.15 = near lower band = stretched below. So the
                    # magnitude threshold stretch_max applies symmetrically to
                    # both sides; pct_b alone disambiguates direction.
                    self._stretched_failure_time[c.symbol] = sessions.now_et()
                    self._set_build_failure(
                        c.symbol, regime,
                        f"short_stretched_at_bottom(pct_b={pct_b:.3f}<={1.0 - pct_b_max:.2f},stretch={atr_stretch:.2f}>={stretch_max:.2f})",
                    )
                    return None
            if bool(self.params.get("reject_tech_bias_contradiction", True)):
                dmi_bias = str(getattr(tech_ctx, "dmi_bias", "neutral") or "neutral").lower()
                obv_bias = str(getattr(tech_ctx, "obv_bias", "neutral") or "neutral").lower()
                if side == Side.LONG and (dmi_bias == "bearish" or obv_bias == "bearish"):
                    self._set_build_failure(
                        c.symbol, regime,
                        f"long_tech_bias_contradicts(dmi={dmi_bias},obv={obv_bias})",
                    )
                    return None
                if side == Side.SHORT and (dmi_bias == "bullish" or obv_bias == "bullish"):
                    self._set_build_failure(
                        c.symbol, regime,
                        f"short_tech_bias_contradicts(dmi={dmi_bias},obv={obv_bias})",
                    )
                    return None

        # HTF bias alignment filter. The higher-timeframe market-structure
        # context (usually 15m) is attached to sr_ctx.market_structure.
        # Two layers:
        #   1. Explicit bias: block if mshtf_bias is opposed to the trade
        #      direction (e.g. LONG vs bearish). This catches confirmed
        #      trend opposition.
        #   2. Pivot pattern: `mshtf_bias` is labeled "bearish" only after
        #      an active BOS/CHoCH, so a stock forming LL/LH pivots may
        #      still read "neutral" while being structurally bearish.
        #      Extend the filter to pullback entries so the AMZN 2026-04-15
        #      pattern is caught (LL pivots, screener bias SHORT, bot
        #      took 3 range/pullback LONGs and lost on all three).
        #   3. Neutral/aligned bias still passes.
        #
        # ORB-window bypass: during the opening window (through orb_end),
        # the 15-min chart has zero or one completed bars from today — the
        # HTF structure is stale (yesterday's pivots). The trend regime
        # already requires a fresh breakout above recent highs, which is
        # its own directional proof. Blocking on stale HTF bias here
        # killed the TSLA 2026-04-15 open-dip-then-run ($362→$394).
        # After the ORB window, 2-3 closed 15-min bars exist and the
        # filter becomes meaningful again.
        # (orb_end / in_orb_window now computed once at the top of this
        # function so Fix D gates share the same reading; see comment there.)
        # ORB follow-through gate: during the ORB window, require the most
        # recent *completed* 5m bar of today's session to have closed in the
        # signal's direction (bullish bar for LONG, bearish for SHORT). This
        # filters the "poke above range then reverse" false breakouts that
        # dominated 2026-04-17's ORB book (AAPL LONG @269.61 rejected at
        # 268.54 resistance; NFLX SHORT @95.26 squeezed back to 96.82, both
        # within minutes). The gate only engages when ≥2 today's 5m bars
        # exist, so the first 5 minutes of the session (before any 5m bar
        # has closed) remain unconstrained — ORB is allowed to fire at 09:36
        # if momentum is obvious, but must survive the 09:40 5m close.
        if in_orb_window and bool(self.params.get("orb_require_5m_followthrough", True)):
            # A 5m frame that cannot be built is a gate that cannot pass.
            # Until 2026-09-26 an error here set the frame to None, which
            # skipped the gate and let the entry through. Broad because the
            # resample runs TA-Lib, which raises a bare Exception.
            try:
                frame_5m = self._resampled_frame(frame, 5, symbol=c.symbol, data=data)
            except Exception as exc:
                LOG.warning("ORB 5m follow-through: could not build %s's 5m frame; refusing the %s %s entry",
                            c.symbol, side.value, regime, exc_info=True)
                self._set_build_failure(c.symbol, regime,
                                        f"{side.value.lower()}_orb_5m_unavailable(error={type(exc).__name__})")
                return None
            if frame_5m is not None and not frame_5m.empty:
                now_dt = sessions.now_et()
                session_start = now_dt.replace(hour=EQUITY_RTH_OPEN.hour, minute=EQUITY_RTH_OPEN.minute, second=0, microsecond=0)
                today_bars = frame_5m[frame_5m.index >= session_start]
                # Use iloc[-2] (previous completed bar) when ≥2 exist.
                # iloc[-1] is the currently-forming bar.
                if len(today_bars) >= 2:
                    last_closed = today_bars.iloc[-2]
                    bar_open = safe_float(last_closed.get("open"))
                    bar_close = safe_float(last_closed.get("close"))
                    if bar_open is not None and bar_close is not None:
                        if side == Side.LONG and bar_close <= bar_open:
                            self._set_build_failure(c.symbol, regime, f"long_orb_5m_not_bullish(open={bar_open:.4f},close={bar_close:.4f})")
                            return None
                        if side == Side.SHORT and bar_close >= bar_open:
                            self._set_build_failure(c.symbol, regime, f"short_orb_5m_not_bearish(open={bar_open:.4f},close={bar_close:.4f})")
                            return None
        orb_htf_bypass = bool(self.params.get("orb_bypass_htf_bias", True)) and in_orb_window
        if bool(self.params.get("require_htf_bias_alignment", True)) and not orb_htf_bypass:
            mshtf_ctx = getattr(sr_ctx, "market_structure", None)
            if mshtf_ctx is not None:
                htf_bias = str(getattr(mshtf_ctx, "bias", "neutral") or "neutral").lower()
                pivot_bias = str(getattr(mshtf_ctx, "pivot_bias", "neutral") or "neutral").lower()
                last_high = str(getattr(mshtf_ctx, "last_high_label", "") or "")
                last_low = str(getattr(mshtf_ctx, "last_low_label", "") or "")
                # The rule that set the bias leads the reason: the labels
                # beside it can read the other way (MRVL 2026-10-06, HH / HL
                # refused as bearish by the midpoint rule).
                bias_source = mshtf_ctx.bias_source
                # Layer 1 — explicit opposing bias (applies to all regimes)
                if side == Side.LONG and htf_bias == "bearish":
                    self._set_build_failure(
                        c.symbol, regime,
                        f"htf_bias_bearish(source={bias_source},"
                        f"last_high={last_high or 'na'},last_low={last_low or 'na'})",
                    )
                    return None
                if side == Side.SHORT and htf_bias == "bullish":
                    self._set_build_failure(
                        c.symbol, regime,
                        f"htf_bias_bullish(source={bias_source},"
                        f"last_high={last_high or 'na'},last_low={last_low or 'na'})",
                    )
                    return None
                # Layer 2 — pullback + trend regimes: block when the HTF
                # pivot pattern itself leans against the trade even though
                # bias is labeled neutral. Extended to trend as Fix E after
                # top_tier INTC 2026-04-20 (-$29) showed mshtf_bias=bullish
                # but mshtf_pivot_bias=bearish let a doomed trend long through.
                # Range regime still skips this — range thesis doesn't
                # presume trend direction.
                # Disable via ``require_htf_pivot_alignment_trend: false``.
                pivot_regimes = {"pullback"}
                if bool(self.params.get("require_htf_pivot_alignment_trend", True)):
                    pivot_regimes.add("trend")
                if regime in pivot_regimes:
                    if side == Side.LONG and last_high == "LH" and last_low in {"LL", "EQL"} and pivot_bias != "bullish":
                        self._set_build_failure(
                            c.symbol, regime,
                            f"htf_pivot_bearish(last_high={last_high},last_low={last_low},"
                            f"pivot_bias={pivot_bias})",
                        )
                        return None
                    if side == Side.SHORT and last_low == "HL" and last_high in {"HH", "EQH"} and pivot_bias != "bearish":
                        self._set_build_failure(
                            c.symbol, regime,
                            f"htf_pivot_bullish(last_high={last_high},last_low={last_low},"
                            f"pivot_bias={pivot_bias})",
                        )
                        return None

        # HTF EMA trend (htf_ema_fast_span / htf_ema_slow_span on the HTF
        # context): the peer strategies' 2-of-3 vote -- close vs the fast
        # EMA, fast vs slow, the context's trend bias. Wired 2026-09-24; until
        # then top_tier read no HTF EMA and the two span knobs only drew the
        # dashboard's HTF chart. require_htf_ema_alignment blocks an entry
        # against it here -- on both sides, long_only / short_only, or
        # neither -- following the ORB-window HTF bypass like the structure
        # gate above; htf_ema_alignment_score acts earlier, on the regime
        # scores in _score_sides (the bonus recorded below is the one applied
        # there).
        htf_ema_bias, htf_ema_bull, htf_ema_bear = self._htf_bias(htf_ctx, close)
        htf_ema_opposed = (
            (side == Side.LONG and htf_ema_bias == "bearish")
            or (side == Side.SHORT and htf_ema_bias == "bullish")
        )
        if side in self._htf_ema_gated_sides() and htf_ema_opposed and not orb_htf_bypass:
            self._set_build_failure(
                c.symbol, regime,
                f"htf_ema_trend_{htf_ema_bias}(votes={htf_ema_bull}v{htf_ema_bear})",
            )
            return None
        htf_ema_bonus = self._htf_ema_score_term(side, htf_ema_bias, in_orb_window)

        # ORB opposing-level block. The orb regime is exempt from the shared
        # structure and S/R vetoes (manifest capabilities.shared_entry.
        # exemptions {orb: [structure, sr]}; the params orb_bypass_structure_
        # entry / orb_bypass_sr_entry until 2026-09-24): both read backward
        # from the opening action -- a 9:30 dump candle registers as
        # CHoCH_down on the 1m chart and flips `breakdown_below_support`,
        # blocking LONG entries for several bars even after the recovery --
        # and the range break is its own directional proof. The exemption must
        # not wave through an entry into the teeth of the OPPOSING level
        # (resistance for a LONG, support for a SHORT) within
        # orb_opposing_sr_atr_mult * ATR. 2026-04-17 NFLX was shorted at 95.26
        # with support that had just broken at 95.90, 0.64 away; AAPL was
        # long'd at 269.61 with resistance 268.54 just above (false break).
        # Both are PENDING levels -- crossed, flip unconfirmed -- which the
        # builder reports as ``pending_support`` / ``pending_resistance``,
        # never as nearest_*. Until 2026-09-23 this check read nearest_*
        # alone, so neither example could trip it; a pending opposing level
        # now always blocks. Keyed on the regime since 2026-09-24 (it rode
        # the in-window S/R bypass): the orb regime exists only inside the
        # ORB window, and the exemption this block backstops keys on it too.
        frame_atr = last_bar_atr(frame, close)
        orb_opposing_atr_mult = float(self.params.get("orb_opposing_sr_atr_mult", 0.5) or 0.0)
        if regime == "orb" and orb_opposing_atr_mult > 0 and frame_atr > 0:
            threshold = orb_opposing_atr_mult * frame_atr
            if side == Side.LONG:
                opposing_sr_block = getattr(sr_ctx, "pending_resistance", None) is not None
                nearest_res = getattr(sr_ctx, "nearest_resistance", None)
                if nearest_res is not None:
                    res_price = float(getattr(nearest_res, "price", 0.0) or 0.0)
                    if res_price > 0 and 0 <= (res_price - close) <= threshold:
                        opposing_sr_block = True
                if opposing_sr_block:
                    self._set_build_failure(c.symbol, regime, f"long_orb_opposing_resistance_within_{orb_opposing_atr_mult:.2f}atr")
                    return None
            else:
                opposing_sr_block = getattr(sr_ctx, "pending_support", None) is not None
                nearest_sup = getattr(sr_ctx, "nearest_support", None)
                if nearest_sup is not None:
                    sup_price = float(getattr(nearest_sup, "price", 0.0) or 0.0)
                    # A support above entry is either pending (above) or a
                    # confirmed break (broken_support), which a SHORT rides.
                    if sup_price > 0 and 0 <= (close - sup_price) <= threshold:
                        opposing_sr_block = True
                if opposing_sr_block:
                    self._set_build_failure(c.symbol, regime, f"short_orb_opposing_support_within_{orb_opposing_atr_mult:.2f}atr")
                    return None

        # The shared entry stage (2026-09-24; each of these used to be a
        # helper call here, and the dual divergence veto never ran): the raw
        # R:R gate a builder asked for, the switched-on vetoes (structure,
        # S/R, broken level, chart, dual divergence, candle -- all of them
        # evaluated, so the refusal payload lists every blocker, of which the
        # queue loop's decision log keeps the primary; the manifest exempts
        # orb from the first two, and since 2026-09-25 range / pullback /
        # sr_scalp from the structure veto and -- top_tier's manifest only --
        # vwap_reclaim from the S/R veto), the S/R then technical stop / target
        # refinement, and the entry-context / FVG score terms. It reuses the
        # contexts the gates above read, on the same 1m frame.
        admitted = self.entry_policy.admit(EntryProposal(
            candidate=c, direction=side, style=regime, style_family=regime,
            close=close, stop=stop, target=target,
            gate_frame=frame, sr_frame=frame, level_frame=frame, data=data,
            raw_rr_gate=raw_rr_gate, htf_ctx=htf_ctx,
            contexts=EntryContexts(sr=sr_ctx, ms=ms_ctx, tech=tech_ctx, chart=ctx),
            vol_scale=vol_scale,
        ))
        if admitted is None:
            return None
        stop, target = admitted.stop, admitted.target

        # Entry exhaustion check — after the refinement, as it always ran;
        # skipped during the ORB window because VWAP and EMA9 haven't
        # equilibrated after the open. A sharp
        # V-reversal (e.g. TSLA 2026-04-15 open dump $367→$362 then run
        # to $394) artificially depresses VWAP, making the recovery look
        # "extended" when it's really the trend establishing itself.
        # After the ORB window, VWAP reflects today's action and the
        # filter becomes meaningful.
        orb_exhaustion_bypass = bool(self.params.get("orb_bypass_exhaustion", True)) and in_orb_window
        if not orb_exhaustion_bypass:
            vwap = safe_float(frame.iloc[-1].get("vwap"), close)
            ema9 = safe_float(frame.iloc[-1].get("ema9"), close)
            exhaustion = self._entry_exhaustion_reasons(side, frame, close=close, vwap=vwap, ema9=ema9)
            if exhaustion:
                self._set_build_failure(c.symbol, regime, exhaustion[0])
                return None

        # Scoring
        ms_ctx = admitted.ms
        structure_bonus = 0.75 if getattr(ms_ctx, "bias", "neutral") == ("bullish" if side == Side.LONG else "bearish") else 0.0
        if side == Side.LONG and getattr(ms_ctx, "bos_up", False) and self._structure_event_recent(getattr(ms_ctx, "bos_up_age_bars", None)):
            structure_bonus += 0.5
        elif side == Side.SHORT and getattr(ms_ctx, "bos_down", False) and self._structure_event_recent(getattr(ms_ctx, "bos_down_age_bars", None)):
            structure_bonus += 0.5
        chart_ctx = admitted.chart
        if side == Side.LONG:
            pattern_bonus = 0.35 if chart_ctx.matched_bullish_continuation else (0.15 if chart_ctx.matched_bullish_reversal else 0.0)
        else:
            pattern_bonus = 0.35 if chart_ctx.matched_bearish_continuation else (0.15 if chart_ctx.matched_bearish_reversal else 0.0)

        # Candle pattern confirmation on the trigger frame -- the directional
        # signal admit read for the shared opposing-candle veto
        # (use_opposing_candle_filter, which moved there from here on
        # 2026-09-24), from the SAME cached candle context: no extra TA-Lib
        # calls.
        candle_signal = admitted.candle_signal
        candle_bonus = 0.0
        candle_confirmed = bool(candle_signal.get("confirmed"))
        if candle_confirmed:
            tier = str(candle_signal.get("confirm_tier", ""))
            if tier == "strong_3c":
                candle_bonus = 0.40
            elif tier == "solid_2c":
                candle_bonus = 0.25
            else:
                candle_bonus = 0.10

        fvg_cont_bias = float(admitted.fvg["fvg_continuation_bias"])
        runner_allowed = bool(fvg_cont_bias >= 0.35 and structure_bonus >= 0.75)

        # Apply adaptive_ladder rungs when configured, from the refined stop.
        # The helper falls back to the admitted target when ladder mode isn't
        # active, the regime opts out (range), or no qualifying rungs exist —
        # so this call is safe to make unconditionally.
        target, ladder_meta = self._apply_ladder_if_enabled(
            side, close, stop, target,
            regime=regime, sr_ctx=admitted.sr, atr=frame_atr,
        )
        # Trail-runner: in adaptive_ladder mode, when no qualifying rungs
        # exist for a trend/pullback entry, drop the fixed target so the
        # trade runs until the trailing stop + structural exits (CHoCH, SR
        # loss) catch it. A fixed 2R target here would prematurely close a
        # trend-day move (e.g. TSLA 2026-04-15: $365→$394 run, 2R target
        # exits at $372). Range regime keeps its single target at
        # range_high — laddering past the range contradicts its thesis.
        ladder_mode_active = self.config.risk.trade_management_mode == "adaptive_ladder"
        if ladder_mode_active and not ladder_meta and regime in {"trend", "pullback"}:
            target = None
            runner_allowed = False

        # Fix G — CEILING on target extension past nearest opposing SR;
        # complements entry_min_clearance_atr (FLOOR on SR clearance). It
        # refuses a take-profit that only pays if price punches through an
        # opposing level the trade does not manage. Placement invariant:
        # runs AFTER ladder + runner override so `target` is the trade's
        # FINAL take-profit -- None in runner mode (gate inert; runners
        # trail out), the first rung in ladder mode, the refined target
        # otherwise. Trend-only: range targets ARE opposing SR by design.
        if (
            regime == "trend"
            and target is not None
            and bool(self.params.get("reject_target_beyond_sr", True))
        ):
            target_max_sr_ratio = float(self.params.get("target_max_sr_ratio", 0.8))
            tgt = float(target)
            # The opposing level is the nearest one playing the role strictly
            # beyond the signal's close (``role_levels``): a confirmed flip
            # between the close and nearest_* -- a lost support under the
            # resistance, a reclaimed resistance over the support -- is the
            # level a target past it must punch through, and the refusal
            # names it (2026-10-07). A flip the close has already passed is
            # no candidate, and one exactly at the close leaves nearest_* to
            # judge, as in the refinement's caps: read alone, it switched
            # the gate off.
            if side == Side.LONG:
                near = next((level for level in role_levels(admitted.sr, "resistance", price=close)
                             if float(level.price) > close), None)
                level_price = float(getattr(near, "price", 0.0) or 0.0)
                valid = level_price > close
                dist_to_sr = level_price - close if valid else 0.0
                dist_to_target = tgt - close
                level_name = "broken_support" if near is not None and near is admitted.sr.broken_support else "resistance"
                reason_prefix = "long_target_beyond_resistance"
            else:
                near = next((level for level in role_levels(admitted.sr, "support", price=close)
                             if 0.0 < float(level.price) < close), None)
                level_price = float(getattr(near, "price", 0.0) or 0.0)
                valid = 0.0 < level_price < close
                dist_to_sr = close - level_price if valid else 0.0
                dist_to_target = close - tgt
                level_name = "broken_resistance" if near is not None and near is admitted.sr.broken_resistance else "support"
                reason_prefix = "short_target_beyond_support"
            # A first ladder rung ON the nearest level is the ladder's own
            # take-profit at that level (with shared_exit's touch hold on,
            # the ladder judges the bar that touches it and may promote to
            # the next rung). The rung builder draws from the list nearest_*
            # heads, so rung 1 sits at or past the nearest level and the
            # ratio is >= 1.00 by construction: at the shipped 0.7 / 0.8
            # ratios this gate refused every laddered trend entry until
            # 2026-09-25, and trend traded only as a trail runner. A nearest
            # level under ladder_min_target_rr that rung 1 skipped is still
            # an unmanaged level short of the target, and the ratio still
            # refuses it. A confirmed flip inside nearest_*'s cluster is no
            # rung, so rung 1 never sits on it and a target past it is
            # refused the same way. The tolerance covers the rung builder's
            # round(price, 6).
            if valid and ladder_meta and abs(tgt - level_price) <= 1e-6:
                valid = False
            if valid and dist_to_target > dist_to_sr * target_max_sr_ratio:
                ratio = dist_to_target / dist_to_sr
                self._set_build_failure(
                    c.symbol, regime,
                    f"{reason_prefix}(target={tgt:.4f},"
                    f"{level_name}={level_price:.4f},ratio={ratio:.2f}>{target_max_sr_ratio:.2f})",
                )
                return None

        management = self._adaptive_management_components(
            side, close, stop, target, style=regime,
            runner_allowed=runner_allowed, continuation_bias=fvg_cont_bias,
        )
        # The strategy's own priority. emit adds the shared entry-context and
        # FVG terms (shared_context_score) to make final_priority_score, the
        # same sum this built itself until 2026-09-24.
        activity_weight = float(self.params.get("activity_score_weight", 0.12))
        strategy_score = (
            regime_score
            + (float(c.activity_score) * activity_weight)
            + structure_bonus
            + pattern_bonus
            + candle_bonus
        )

        reason = f"top_tier_{regime}_{'long' if side == Side.LONG else 'short'}"
        # emit stamps entry_style_family (the regime) and orb_window_entry
        # (the orb family) -- the tag post-session analysis slices ORB vs
        # post-ORB entries by (2026-04-17 ORB entries were 1W/4T (-$120); the
        # post-ORB 10:05-11:00 window 2W/5T (+$126) thanks to TSLA). It was
        # the ORB-window flag until 2026-09-24; the orb regime is the only
        # one the window allows, so the two agree.
        return self.entry_policy.emit(
            admitted,
            reason=reason,
            strategy_score=strategy_score,
            management=management,
            target=target,
            ladder_meta=ladder_meta or None,
            metadata={
                "regime": regime,
                "regime_score": round(regime_score, 4),
                "structure_bonus": round(structure_bonus, 4),
                "pattern_bonus": round(pattern_bonus, 4),
                "candle_bonus": round(candle_bonus, 4),
                "candle_confirmed": candle_confirmed,
                "candle_tier": candle_signal.get("confirm_tier"),
                "candle_anchor": candle_signal.get("anchor_pattern"),
                "candle_matches": candle_signal.get("matches", []),
                "orb_end_time": orb_end.strftime("%H:%M"),
                "htf_ema_trend": htf_ema_bias,
                "htf_ema_votes": f"{htf_ema_bull}v{htf_ema_bear}",
                "htf_ema_bonus": round(htf_ema_bonus, 4),
            },
        )

    # ------------------------------------------------------------------
    # entry_signals — main loop
    # ------------------------------------------------------------------
    def entry_signals(
        self,
        candidates: list[Candidate],
        bars: dict[str, pd.DataFrame],
        positions: dict[str, Position],
        client=None,
        data=None,
    ) -> list[Signal]:
        """One entry cycle. After the cycle-wide checks each candidate goes
        through ``_read_candidate`` (the skips, the bar reads, the bias, the
        side vote and the relative-strength filter), ``_score_sides`` (pass 1),
        ``_queue_builds`` (pass 2) and ``_run_build_queue`` (the per-regime
        gates, the armed retest and the builds; the first signal wins), and
        ``_record_candidate`` records the outcome.

        The pass's frame-read memo (``_pass_memo_read``) is open for exactly
        this call."""
        self._entry_pass_memo = {}
        try:
            return self._entry_pass(candidates, bars, positions, data)
        finally:
            self._entry_pass_memo = None

    def _entry_pass(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame],
                    positions: dict[str, Position], data) -> list[Signal]:
        """``entry_signals``' body, run with the pass memo open."""
        self._reset_entry_decisions()
        self._prune_armed_retests()
        out: list[Signal] = []
        min_bars = int(self.params.get("min_bars", 60) or 60)
        ltf_min = max(1, int(self.params.get("ltf_minutes", 5)))
        # Indicator-span stretch for the LTF frame. With a 1m LTF, span_scale=5
        # makes atr14/adx14/rsi14/ret5/ret15 behave like the old 5m frame
        # (atr14->70, ...) so entry scoring keeps its 5m wall-clock horizons
        # while acting on finer 1m bars/closes. Default 1.0 leaves the
        # canonical spans untouched. The LTF EMAs are set on their own by
        # ltf_ema_fast_span / ltf_ema_slow_span (default: 9 / 20 x span_scale).
        # Exits and the ema9-extension gate read the base 1m frame's native
        # EMA9 / EMA20, not these.
        ltf_span_scale = float(self.params.get("ltf_indicator_span_scale", 1.0))
        ltf_ema_pair = ltf_ema_spans(self.params)
        min_ltf_bars = int(self.params.get("min_ltf_bars", 15))
        allow_short = bool(self.config.risk.allow_short)
        now_t = sessions.now_et().time()
        allowed_regimes = self._allowed_regimes(now_t)
        if not allowed_regimes:
            for c in candidates:
                self._record_entry_decision(c.symbol, "skipped", ["outside_entry_window"])
            return out
        # ORB window flag — consumed by the surviving orb_bypass_* gates
        # (htf_bias / exhaustion / screener_bias / side_decision /
        # relative_strength), all of which apply to the orb regime at the
        # open when their inputs (15m structure, sector-ETF VWAP, etc.) are
        # still stale. (The structure / S/R bypasses are the manifest's
        # shared_entry exemption for the orb regime since 2026-09-24.) The
        # orb regime is not in the index-
        # confirmation / confirmation-bar regime sets, so it never hits those
        # gates — the old orb_bypass_index_confirmation / _entry_confirmation_bar
        # companions were removed (2026-05-29).
        in_orb_window = self._in_orb_window(now_t)

        # Index ok / neutral are now PER-CANDIDATE because of the
        # ``sector_index_map``-driven per-sector ETF lookup (see
        # ``_indices_for_symbol``). An AAPL LONG checks XLK; an XOM LONG
        # checks XLE; FCX/NEM check XLB; etc. Read per candidate
        # (``_read_candidate``) since the result varies by ``c.symbol``
        # even within a single cycle.
        sides_to_evaluate = [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]

        # Extended-hours universe gate: outside RTH (07:00-09:30 / 16:00-20:00)
        # only the configured liquid names may enter; thinner names skip pre/
        # post-market entries where spreads/fills are poor. Active only in
        # extended-indicator mode (RTH-only presets never hit this). Session
        # state is evaluated once per cycle.
        ext_hours_now = (
            get_session_indicator_window() == "extended"
            and not equity_session_state(sessions.now_et()).regular_session
        )
        # ``extended_hours_tradable_all`` (off by default): when set, EVERY
        # candidate is extended-hours eligible. For screener-universe strategies
        # whose tradable set is dynamic each cycle, a hand-listed
        # ``extended_hours_tradable`` sublist can't enumerate the universe.
        ext_all = bool(self.params.get("extended_hours_tradable_all", False))
        ext_allowed = self._extended_hours_tradable_set() if ext_hours_now else set()

        # Macro-window blackout applies to the whole cycle (CPI, FOMC — not
        # symbol-specific), so evaluate it once rather than per candidate.
        macro_block = self._event_calendar.entry_block_reason()
        if macro_block is not None:
            for c in candidates:
                self._record_entry_decision(c.symbol, "skipped", [f"event_blackout({macro_block})"])
            return out

        cycle = _EntryCycle(
            now_t=now_t, allowed_regimes=allowed_regimes, in_orb_window=in_orb_window,
            allow_short=allow_short, sides_to_evaluate=sides_to_evaluate,
            ext_hours_now=ext_hours_now, ext_all=ext_all, ext_allowed=ext_allowed,
            min_bars=min_bars, ltf_min=ltf_min, ltf_span_scale=ltf_span_scale,
            ltf_ema_pair=ltf_ema_pair, min_ltf_bars=min_ltf_bars,
        )
        for c in candidates:
            read = self._read_candidate(cycle, c, bars, positions, data)
            if read is None:
                continue
            fail_reasons: list[str] = []
            side_decisions = self._score_sides(cycle, read, data)
            queue = self._queue_builds(cycle, read, side_decisions, fail_reasons)
            best_signal, winning_decision, winning_regime, winning_norm = self._run_build_queue(
                read, queue, fail_reasons, data)
            if best_signal is not None:
                out.append(best_signal)
            self._record_candidate(read, queue, fail_reasons, best_signal, winning_decision, winning_regime,
                                   winning_norm)
        return out

    def _read_candidate(self, cycle: _EntryCycle, c: Candidate, bars: dict[str, pd.DataFrame],
                        positions: dict[str, Position], data) -> _CandidateRead | None:
        """Everything the scoring pass and the builds read for *c* this cycle,
        or ``None`` when a skip was recorded instead: held, an earnings
        blackout, not extended-hours eligible, too little history or LTF, or
        the relative-strength filter left no side."""
        allow_short, now_t, in_orb_window = cycle.allow_short, cycle.now_t, cycle.in_orb_window
        min_bars, ltf_min, min_ltf_bars = cycle.min_bars, cycle.ltf_min, cycle.min_ltf_bars
        ltf_span_scale, ltf_ema_pair = cycle.ltf_span_scale, cycle.ltf_ema_pair
        ext_hours_now, ext_all, ext_allowed = cycle.ext_hours_now, cycle.ext_all, cycle.ext_allowed
        sides_to_evaluate = cycle.sides_to_evaluate
        if c.symbol in positions:
            # Drop any arm on a symbol we already hold. An arm records
            # "a breakout happened, wait for the retest", and once a
            # position exists it can never produce the entry it was
            # created for. Leaving it is not harmless: `_prune_armed_retests`
            # keeps an arm well past its window, so when the position
            # closes -- 20 minutes later, at a different level -- the next
            # qualifying cycle finds an EXPIRED arm and takes the market
            # fallback immediately, skipping the wait on the re-entry,
            # which is the most chase-prone entry there is.
            self._drop_armed_retests(c.symbol)
            self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
            return None
        # Per-symbol earnings blackout. An earnings print resets the
        # symbol's volatility regime, so the ATR-derived stops and the
        # session-open-anchored day_strength this strategy relies on are
        # both calibrated to a distribution that no longer holds. Across
        # 23 mega caps that is roughly 92 scheduled events a year,
        # clustered into three weeks a quarter.
        earnings_block = self._event_calendar.earnings_block_reason(c.symbol)
        if earnings_block is not None:
            self._record_entry_decision(c.symbol, "skipped", [earnings_block])
            return None
        if ext_hours_now and not ext_all and c.symbol.upper().strip() not in ext_allowed:
            self._record_entry_decision(c.symbol, "skipped", ["extended_hours_not_eligible"])
            return None
        frame = bars.get(c.symbol)
        if frame is None or len(frame) < min_bars:
            self._record_entry_decision(c.symbol, "skipped", [
                insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), min_bars)])
            return None

        ltf = self._resampled_frame(frame, ltf_min, symbol=c.symbol, data=data, span_scale=ltf_span_scale,
                                    ema_spans=ltf_ema_pair)
        if ltf is None or ltf.empty or len(ltf) < min_ltf_bars:
            self._record_entry_decision(c.symbol, "skipped", ["missing_ltf_context"])
            return None

        # Per-candidate index lookup — uses ``sector_index_map`` to route
        # AAPL → XLK, XOM → XLE, FCX → XLB, etc. Falls back to
        # ``index_symbols`` when no sector mapping exists for the symbol.
        index_ok_by_side = {side: self._index_confirms(side, c.symbol, bars, data) for side in sides_to_evaluate}
        # The HTF EMA trend, read once per candidate for the score term
        # (``_score_sides``; the gate reads the same context in
        # _finalize_signal).
        htf_ema_read = (
            self._htf_bias(self._default_htf_context_for_score(c.symbol, data), safe_float(ltf.iloc[-1]["close"], 0.0))
            if float(self.params.get("htf_ema_alignment_score", 0.0)) else ("neutral", 0, 0)
        )
        idx_neutral = self._index_neutral(c.symbol, bars)

        last = ltf.iloc[-1]
        close = safe_float(last["close"], 0.0)
        vwap = safe_float(last.get("vwap"), close)
        ema9 = safe_float(last.get("ema9"), close)
        ema20 = safe_float(last.get("ema20"), close)
        adx = safe_float(last.get("adx14"), 0.0)
        ret5 = safe_float(last.get("ret5"), 0.0)
        ret15 = safe_float(last.get("ret15"), 0.0)
        atr = last_bar_atr(ltf, close, floor_pct=0.0005)

        # Per-symbol volatility scale: this symbol's 20-day ADR relative
        # to ``reference_adr_pct``, the ADR the percent-of-price params
        # were tuned against. Every percent threshold below is multiplied
        # by it so one parameter means one thing across a universe whose
        # ADR spans roughly 0.9% (COST/V/TMUS) to 3%+ (NVDA/TSLA/AMD).
        # Orthogonal to ``vol_widening``: that reacts to TODAY's ATR
        # expansion, this encodes how volatile the name normally is.
        # 1.0 when the daily feed has no stats for the symbol.
        vol_scale = self._vol_scale(c.symbol, data)

        # Soft-bias gating (Fix A, refactored 2026-05-12). The original
        # Fix A hard-locked ``preferred_sides`` to one direction when
        # ``effective_bias`` was set, fully suppressing the opposite
        # side. That correctly blocked the 2026-04-20 META/INTC/TSLA
        # fallthrough losses (deeply-negative day_strength + intraday
        # bounce), but was too rigid for the opposite case: a stock
        # with mild bias and a strong structural setup on the opposite
        # side (fresh BOS↑, breakout above HTF resistance, bullish
        # structure_bias) had its LONG opportunity silently ignored.
        #
        # Replaced with a soft score-penalty in ``_bias_penalty``:
        # both sides are always evaluated, but the side that disagrees
        # with ``effective_bias`` has each of its regime scores reduced
        # by ``bias_penalty_base * min(1.0, |day_strength| / saturate_at)``.
        # A mild −0.5% day applies only ~0.25 penalty (strong setups
        # still pass min_*_score). A deep −3% day applies the full
        # 1.0 penalty (filters all but the strongest setups, preserving
        # the 2026-04-20 protection).
        #
        # The bias is computed LIVE from session_open + current close
        # (``_compute_live_directional_bias``), authoritative for
        # decisions; the screener's pre-computed
        # ``c.directional_bias`` is only used by the gatekeeper's
        # cooldown lookup before entry_signals runs.
        #
        # ORB-window bypass (``orb_bypass_screener_bias``, default
        # ``true``): during the opening window (through orb_end), day_strength is dominated
        # by the opening gap; the bypass disables the penalty entirely
        # so gap-fade entries (TSLA 2026-04-15 $367→$362→$394) can
        # qualify on either side without bias drag.
        orb_screener_bypass = bool(self.params.get("orb_bypass_screener_bias", True)) and in_orb_window
        respect_bias = bool(self.params.get("respect_screener_bias", True)) and not orb_screener_bypass

        # Single computation — bias for side selection + day_strength
        # magnitude for penalty scaling, sharing one session_open_price
        # lookup (instead of the two separate calls the prior code did).
        live_bias, day_strength = self._compute_live_bias_and_day_strength(frame, close)

        # Trailing-bias memory: when current live_bias is None but the
        # recent window of decisions had a strong one-sided read, infer
        # that bias for the penalty calculation. Addresses the
        # 2026-04-23 GOOG 12:51 pullback_long case (current bias None
        # after 10 SHORT-biased bars, lost $22 to counter-trend).
        trailing_enabled = bool(self.params.get("trailing_bias_enabled", True))
        trailing_lookback = max(3, int(self.params.get("trailing_bias_lookback", 10)))
        trailing_threshold = float(self.params.get("trailing_bias_majority_threshold", 0.7))
        effective_bias = live_bias
        if effective_bias is None and trailing_enabled and respect_bias:
            recent = list(self._recent_directional_bias.get(c.symbol, ()))
            long_count = sum(1 for b in recent if b == Side.LONG)
            short_count = sum(1 for b in recent if b == Side.SHORT)
            total_directional = long_count + short_count
            min_directional = max(3, trailing_lookback // 2)
            if total_directional >= min_directional:
                if short_count / total_directional >= trailing_threshold:
                    effective_bias = Side.SHORT
                elif long_count / total_directional >= trailing_threshold:
                    effective_bias = Side.LONG
        # Record the raw live bias (not the trailing-inferred fallback)
        # so the trailing memory reflects what the LIVE day_strength
        # has actually been doing across recent BARS.
        #
        # One observation per LTF bar: a later cycle on the same bar
        # replaces that bar's read. This appended every cycle until
        # 2026-09-22, so `trailing_bias_lookback: 10` meant ten loop
        # iterations -- 20-30 seconds at a 2s loop, varying with cycle
        # time -- and the memory the knob was written for (the GOOG
        # 2026-04-23 LONG into ten SHORT-biased bars) had faded long
        # before it mattered. A new session starts it empty: yesterday's
        # closing read says nothing about this morning's open.
        hist = self._recent_directional_bias.get(c.symbol)
        if hist is None or hist.maxlen != trailing_lookback:
            existing = list(hist) if hist is not None else []
            hist = deque(existing[-trailing_lookback:], maxlen=trailing_lookback)
            self._recent_directional_bias[c.symbol] = hist
        bar_ts = pd.Timestamp(ltf.index[-1])
        last_bar = self._recent_directional_bias_bar.get(c.symbol)
        if last_bar is not None and last_bar.date() != bar_ts.date():
            hist.clear()
        if last_bar == bar_ts and hist:
            hist[-1] = live_bias
        else:
            hist.append(live_bias)
        self._recent_directional_bias_bar[c.symbol] = bar_ts

        # Both sides are always evaluated under soft bias. The penalty
        # applied per side in ``_score_sides`` filters out weak
        # counter-bias setups. Shorts can still be globally disabled
        # via ``allow_short = False``.
        preferred_sides = [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]

        # Explicit side decision (Fix A, 2026-05-27; scoped to
        # direction-following regimes 2026-09-18). An evidence-based vote
        # across CURRENT price-action signals — recent return, close vs
        # VWAP, EMA9/20, last-3-bar colour — replacing the old implicit
        # "score both sides, take the higher" side selection.
        #
        # The vote now GATES REGIMES rather than collapsing
        # ``preferred_sides``. Every one of its four signals is
        # trend-following, so applying it to the whole candidate silently
        # removed the two mean-reversion regimes: ``range`` only enters
        # within the bottom 35% of the range (LONG), and ``sr_scalp``
        # only at a support that price has just fallen into — exactly the
        # conditions under which recent-return and close-vs-VWAP both
        # vote SHORT. Simulated over a clean oscillating range with the
        # shipped params, the vote agreed with the range regime's own
        # entry zone on 3 of 54 in-zone bars (5.5%). The confirmation-bar
        # and index gates already exempt these two regimes for precisely
        # this reason (see CONFIRMATION_BAR_REGIMES / the
        # MEAN_REVERSION_REGIMES comment); the vote running candidate-wide
        # and BEFORE regime scoring made those exemptions unreachable.
        #
        # ``decided_side`` is applied per (side, regime) pair in the build
        # queue (``_run_build_queue``) against SIDE_DECISION_REGIMES. Bypassed inside the
        # ORB window when ``orb_bypass_side_decision`` is true — the
        # opening tape is gap-dominated.
        side_decision_orb_bypass = (
            bool(self.params.get("orb_bypass_side_decision", True)) and in_orb_window
        )
        # Tracks whether ``_decide_side`` made an explicit, current-
        # action side pick this cycle. When True, the soft bias
        # penalty (``_score_sides``) is skipped — the explicit decision already
        # chose the side, so docking it with the stale chg_open bias
        # would re-introduce the backward-looking suppression Fix A
        # replaced.
        explicit_side_decided = False
        decided_side: Side | None = None
        side_vote_note = ""
        if bool(self.params.get("require_explicit_side_decision", True)) and not side_decision_orb_bypass:
            decided_side, votes = self._decide_side(ltf, close, vwap, ema9, ema20)
            side_vote_note = (
                f"long={votes['long']},short={votes['short']},"
                f"votes=[{','.join(votes['breakdown'])}]"
            )
            if decided_side is not None:
                explicit_side_decided = True

        # Relative-strength filter (2026-05-26; beta-adjusted 2026-09-18).
        # Measures the candidate against its sector — a stock drifting at
        # 0% on a +1% sector day is materially weak even when
        # ``day_strength`` alone reads neutral.
        #
        # The comparison is a RESIDUAL, not a difference: a symbol is
        # expected to move ``beta`` times its sector, so only the part of
        # its move that beta does not explain is strength or weakness.
        # The raw ``day_strength - sector_ds`` form assumed beta 1.0 for
        # every name, which on a mega-cap universe (sector betas roughly
        # 0.5 on COST/V/TMUS to 1.8 on NVDA/AMD/TSLA) measured beta
        # instead of alpha: on a −1.0% XLK day NVDA at −1.6% is performing
        # exactly to a 1.6 beta — zero alpha — yet scored −0.6% and had
        # LONG blocked. The gate therefore blocked high-beta names in the
        # direction the tape was already moving, while low-beta names
        # essentially never tripped it.
        #
        # Beta comes from ``_symbol_daily_stats``. When it is unavailable
        # the gate is SKIPPED for that symbol and the reason is recorded —
        # falling back to beta 1.0 would reintroduce the exact bug.
        # When the rel-strength conflicts with a side, that side is
        # removed from ``preferred_sides``; if no sides remain the
        # candidate is skipped. ORB window is bypassed (the opening
        # window is too noisy for a stock-vs-sector divergence read).
        rs_threshold = self._pct_param("relative_strength_block_threshold_pct", 0.5, vol_scale)
        rs_orb_bypass = bool(self.params.get("orb_bypass_relative_strength", True)) and in_orb_window
        if rs_threshold > 0.0 and not rs_orb_bypass and day_strength is not None:
            sector_ds = self._sector_day_strength(c.symbol, bars)
            stats = self._symbol_daily_stats(c.symbol, data)
            beta = stats.beta if (stats is not None and stats.has_beta) else None
            if sector_ds is not None and beta is not None:
                rel_strength = day_strength - (beta * sector_ds)
                blocked_long = rel_strength <= -rs_threshold and Side.LONG in preferred_sides
                blocked_short = rel_strength >= rs_threshold and Side.SHORT in preferred_sides
                if blocked_long:
                    preferred_sides = [s for s in preferred_sides if s != Side.LONG]
                if blocked_short:
                    preferred_sides = [s for s in preferred_sides if s != Side.SHORT]
                if not preferred_sides:
                    direction = "lagging" if blocked_long else "leading"
                    self._record_entry_decision(c.symbol, "skipped", [
                        f"relative_strength_{direction}_sector(resid={rel_strength:+.2f}%,"
                        f"sym={day_strength:+.2f}%,sec={sector_ds:+.2f}%,beta={beta:.2f},"
                        f"threshold={rs_threshold:.2f}%)"
                    ])
                    return None

        # Score thresholds — read once, used for both sides' qualifier
        # filtering.
        min_trend = float(self.params.get("min_trend_score", 4.0))
        min_pullback = float(self.params.get("min_pullback_score", 4.0))
        min_range = float(self.params.get("min_range_score", 3.5))
        min_vol_squeeze = float(self.params.get("min_vol_squeeze_score", 4.0))
        min_momentum = float(self.params.get("min_momentum_score", 4.0))
        min_sr_scalp = float(self.params.get("min_sr_scalp_score", 3.5))
        min_orb = float(self.params.get("min_orb_score", 3.5))
        min_vwap_reclaim = float(self.params.get("min_vwap_reclaim_score", 3.5))
        thresholds = {
            "trend": min_trend,
            "pullback": min_pullback,
            "range": min_range,
            "vol_squeeze": min_vol_squeeze,
            "momentum": min_momentum,
            "sr_scalp": min_sr_scalp,
            "orb": min_orb,
            "vwap_reclaim": min_vwap_reclaim,
        }

        # tech_ctx is built ONCE per candidate (per-frame @lru_cache makes
        # repeated calls O(1)). Used by vol_squeeze scoring AND by
        # _volatility_widening_factor (Tier 2a) for stop widening. Pulled
        # out of the per-side loop since both sides see the same frame
        # state and the same volatility regime.
        tech_ctx_for_candidate = self._technical_context(frame)
        # Pass ``now_t`` so the time-of-day arm of the widening factor
        # can fire during the early-session high-vol window (typically
        # 09:30-10:30 ET). Catches absolute volatility that Tier 2a's
        # relative-expansion check would miss.
        vol_widening = self._volatility_widening_factor(tech_ctx_for_candidate, current_time=now_t)

        return _CandidateRead(
            c=c, frame=frame, ltf=ltf, close=close, vwap=vwap, ema9=ema9, ema20=ema20, adx=adx, ret5=ret5,
            ret15=ret15, atr=atr, vol_scale=vol_scale, index_ok_by_side=index_ok_by_side,
            htf_ema_read=htf_ema_read, idx_neutral=idx_neutral, day_strength=day_strength,
            effective_bias=effective_bias, respect_bias=respect_bias, preferred_sides=preferred_sides,
            explicit_side_decided=explicit_side_decided, decided_side=decided_side,
            side_vote_note=side_vote_note, thresholds=thresholds,
            tech_ctx_for_candidate=tech_ctx_for_candidate, vol_widening=vol_widening,
        )

    def _score_sides(self, cycle: _EntryCycle, read: _CandidateRead, data) -> list[tuple[Side, dict[str, Any]]]:
        """Pass 1: every allowed regime scored for each side, then the side's
        qualifying regimes in normalised-score order."""
        allowed_regimes, in_orb_window = cycle.allowed_regimes, cycle.in_orb_window
        c, frame, ltf = read.c, read.frame, read.ltf
        close, vwap, ema9, ema20 = read.close, read.vwap, read.ema9, read.ema20
        adx, ret5, ret15, atr = read.adx, read.ret5, read.ret15, read.atr
        vol_scale, tech_ctx_for_candidate = read.vol_scale, read.tech_ctx_for_candidate
        index_ok_by_side, idx_neutral, htf_ema_read = read.index_ok_by_side, read.idx_neutral, read.htf_ema_read
        day_strength, effective_bias, respect_bias = read.day_strength, read.effective_bias, read.respect_bias
        preferred_sides, explicit_side_decided = read.preferred_sides, read.explicit_side_decided
        thresholds = read.thresholds

        # Pass 1 — score each side independently, apply bias penalty,
        # build the per-side ``build_order`` list of qualifying regimes.
        # No single "winner" is picked here; pass 2 flattens all sides'
        # build_orders into a cross-side queue sorted by post-penalty
        # score. The deferred-build design lets the highest-scoring
        # qualifying (side, regime) pair go first regardless of which
        # side it's on — no regime blocks another within OR across sides.
        side_decisions: list[tuple[Side, dict[str, Any]]] = []
        for side in preferred_sides:
            index_ok = index_ok_by_side.get(side, False)

            # Trend must always be scored — pullback's min_pullback_trend_score
            # gate reads trend_score as input. Pullback/range are skipped
            # entirely when not in the current time window's allowed_regimes
            # (e.g. ORB window is trend-only → skip pullback + range).
            trend_score = self._score_trend(side, close, vwap, ema9, ema20, adx, ret5, ret15, index_ok)
            pullback_score = (
                self._score_pullback(side, close, vwap, ema9, ema20, adx, atr, trend_score, ltf)
                if "pullback" in allowed_regimes else 0.0
            )
            range_score = (
                self._score_range(side, close, ema9, ema20, frame,
                                  tech_ctx_for_candidate, idx_neutral, vol_scale)
                if "range" in allowed_regimes else 0.0
            )
            # vol_squeeze and momentum: scoring methods read live frame
            # data — vol_squeeze derives compression from session bars +
            # BB-width; momentum derives day_strength from the session
            # open. ``momentum`` was renamed from ``momentum_close`` and
            # widened from afternoon-only to post-ORB through close.
            vol_squeeze_score = (
                self._score_vol_squeeze(side, close, vwap, ema9, ema20, atr, frame, tech_ctx_for_candidate, vol_scale)
                if "vol_squeeze" in allowed_regimes else 0.0
            )
            momentum_score = (
                self._score_momentum(side, close, vwap, ema9, ema20, ret15, frame, vol_scale)
                if "momentum" in allowed_regimes else 0.0
            )
            # sr_scalp: level-aware scoring (2026-05-29). Scores the
            # actual S/R geometry — proximity to a HOLDING support/
            # resistance zone or a confirmed flip level, bounce/rejection
            # bar character, and room to the next rung. sr_ctx is the
            # per-cycle-cached context (same object the builder reads).
            sr_scalp_score = (
                self._score_sr_scalp(side, close, atr, frame, self._sr_context(c.symbol, frame, data), vol_scale)
                if "sr_scalp" in allowed_regimes else 0.0
            )
            # orb: true Opening Range Breakout (2026-05-29). Allowed only
            # in the ORB window (after the opening range forms). Scores a
            # genuine break of today's opening range; the measured-move
            # geometry + range sanity gates run in _build_orb_signal.
            orb_score = (
                self._score_orb(side, close, atr, frame)
                if "orb" in allowed_regimes else 0.0
            )
            # vwap_reclaim (2026-05-30): a VWAP-reclaim momentum
            # re-entry — dipped below session VWAP then reclaimed it on a
            # volume pop. Reads the base 1m frame (VWAP is session-cumulative).
            vwap_reclaim_score = (
                self._score_vwap_reclaim(side, close, vwap, ema9, ema20, atr, frame, vol_scale)
                if "vwap_reclaim" in allowed_regimes else 0.0
            )

            # Soft-bias penalty (Fix A refactored 2026-05-12). See
            # ``_bias_penalty`` docstring for rationale + worked examples.
            # Skipped when an explicit side decision was made this cycle
            # (2026-05-27): ``_decide_side`` already chose the side from
            # current-action signals, so docking that side's score with
            # the stale chg_open-derived bias would re-introduce the
            # backward-looking suppression the explicit decision replaced
            # — e.g. a stock down on the day but recovering, where Fix A
            # picks LONG and this penalty would otherwise drag the LONG
            # score below its min threshold. Stays active as the sole
            # bias mechanism when require_explicit_side_decision is false.
            bias_penalty = (
                0.0 if explicit_side_decided
                else self._bias_penalty(side, day_strength, effective_bias, respect_bias)
            )
            if bias_penalty > 0.0:
                trend_score = max(0.0, trend_score - bias_penalty)
                pullback_score = max(0.0, pullback_score - bias_penalty)
                range_score = max(0.0, range_score - bias_penalty)
                vol_squeeze_score = max(0.0, vol_squeeze_score - bias_penalty)
                momentum_score = max(0.0, momentum_score - bias_penalty)
                sr_scalp_score = max(0.0, sr_scalp_score - bias_penalty)
                orb_score = max(0.0, orb_score - bias_penalty)
                vwap_reclaim_score = max(0.0, vwap_reclaim_score - bias_penalty)

            scores = {
                "trend": trend_score,
                "pullback": pullback_score,
                "range": range_score,
                "vol_squeeze": vol_squeeze_score,
                "momentum": momentum_score,
                "sr_scalp": sr_scalp_score,
                "orb": orb_score,
                "vwap_reclaim": vwap_reclaim_score,
            }

            # The skip above rests on "the vote already chose this side".
            # That holds for SIDE_DECISION_REGIMES, which the build queue
            # holds to the voted side. It does NOT hold for
            # MEAN_REVERSION_REGIMES, exempted from the vote on 2026-09-18
            # because a fade has to enter against the move -- four months
            # after this skip was written (2026-05-27). The two changes
            # compose badly: scored on the side AGAINST the vote, `range`
            # got no penalty precisely because the vote had decided
            # something `range` does not follow, so it had neither the hard
            # gate nor this soft one. A LONG fade on a -2% day went through
            # unfiltered.
            #
            # The penalty is what `_bias_penalty`'s own docstring says it is
            # for: scaled by the day's move, 0.25 on a -0.5% day and a full
            # 1.0 at `bias_penalty_saturate_at`, so a strong fade still
            # clears on a mild day while a deep counter-trend one does not.
            # It needs no vote exemption of its own: `effective_bias` is
            # None inside the neutral band, and a fade WITH the day's bias
            # (buying a dip on an up day) is untouched.
            mr_bias_penalty = (
                self._bias_penalty(side, day_strength, effective_bias, respect_bias)
                if explicit_side_decided else 0.0
            )
            if mr_bias_penalty > 0.0:
                for mr_regime in MEAN_REVERSION_REGIMES:
                    scores[mr_regime] = max(0.0, scores[mr_regime] - mr_bias_penalty)

            # htf_ema_alignment_score: the HTF EMA trend moves every scored
            # regime of this side before it has to clear its floor, so it
            # can decide whether and which regime builds. (Added to the
            # final priority score, as first wired, it could only reorder
            # signals tied on normalised regime score.) A regime that
            # scored 0 found no setup and gets nothing.
            htf_ema_term = self._htf_ema_score_term(side, htf_ema_read[0], in_orb_window)
            if htf_ema_term:
                for regime_name, regime_value in scores.items():
                    if regime_value > 0.0:
                        scores[regime_name] = max(0.0, regime_value + htf_ema_term)

            # Per-side BUILD ORDER: list of qualifying regimes in score
            # order. A regime qualifies if it's in allowed_regimes AND
            # its post-penalty score meets its own min_*_score threshold
            # (``thresholds``, read in ``_read_candidate``).
            # The build phase iterates ACROSS sides AND regimes in
            # cross-side score order so no regime blocks another (within
            # OR across sides). Both sides' qualifiers compete in the
            # same flat queue.
            # Ordering uses the NORMALISED score — how far into its own
            # headroom a regime scored — not the raw one. Raw scores are
            # not comparable across regimes because each scorer has a
            # different ceiling and floor (vol_squeeze tops out at 6.5
            # over a 4.0 floor; sr_scalp at 5.0 over a 3.0 floor). Sorting
            # raw handed the queue to whichever scorer had the most
            # components: a trend at 4.5 (25% of its headroom) outranked
            # an sr_scalp at 4.4 (93% of its headroom, near its ceiling).
            # ``build_order`` carries both — normalised for ordering, raw
            # for the metadata and skip-summary lines operators read.
            # SHORTs clear their floor plus ``short_min_score_premium``.
            # Equities drift up, so a short fights the base rate and needs
            # more evidence than the mirror-image long. The premium raises
            # the floor for BOTH qualification and the normalisation
            # denominator, so a short that only just clears its raised bar
            # still ranks as a marginal setup rather than being flattered
            # by the lower long floor.
            score_premium = self._short_score_premium(side)
            build_order: list[tuple[str, float, float]] = []
            for regime_name, regime_score in scores.items():
                if regime_name not in allowed_regimes:
                    continue
                floor = thresholds.get(regime_name, float("inf"))
                if floor != float("inf"):
                    floor += score_premium
                if regime_score >= floor:
                    build_order.append(
                        (regime_name, regime_score, self._normalized_regime_score(regime_name, regime_score, floor))
                    )
            build_order.sort(key=lambda item: item[2], reverse=True)

            side_decisions.append((side, {
                "build_order": build_order,
                "scores": scores,
                "index_ok": index_ok,
                "bias_penalty": bias_penalty,
                "mr_bias_penalty": mr_bias_penalty,
            }))

        return side_decisions

    def _queue_builds(self, cycle: _EntryCycle, read: _CandidateRead,
                      side_decisions: list[tuple[Side, dict[str, Any]]],
                      fail_reasons: list[str]) -> list[tuple[bool, Side, str, float, float, dict[str, Any]]]:
        """Pass 2: the build queue, expired arms first. A side with no
        qualifying regime adds its reason to *fail_reasons*."""
        allowed_regimes = cycle.allowed_regimes
        c = read.c

        # Pass 2 — record fail reasons for sides with no qualifying
        # regimes, then flatten the remaining (side, regime) pairs into
        # a single cross-side build queue sorted by post-penalty score
        # descending. First successful build wins.
        #
        # The flat queue means a high-scoring SHORT range CAN beat a
        # low-scoring LONG pullback even if LONG's top regime had a
        # higher score (because trend's build failed). Truly "regimes
        # don't block each other" — within OR across sides.
        #
        # Skip-summary buckets:
        #   * ``<side>_unqualified_no_qualifying_regime(...)`` — no
        #     regime cleared its min-score floor for this side. Signal
        #     was never attempted.
        #   * ``<side>_build_failed_<regime>_<reason>`` — regime cleared
        #     its floor and the build method was invoked, but a hard
        #     gate inside the builder rejected. Signal was attempted.
        # Differentiating these matters for tuning: the first wants
        # looser score thresholds; the second wants looser hard gates.
        build_queue: list[tuple[Side, str, float, float, dict[str, Any]]] = []
        for side, decision in side_decisions:
            if not decision["build_order"]:
                # `mr_bias_pen` is separate from `bias_pen` on purpose: it
                # docked only the mean-reversion regimes, and folding it
                # into one number would read as though trend had been
                # penalised too.
                penalty_suffix = (
                    f",bias_pen={decision['bias_penalty']:.2f}"
                    if decision["bias_penalty"] > 0.0 else ""
                ) + (
                    f",mr_bias_pen={decision['mr_bias_penalty']:.2f}"
                    if decision.get("mr_bias_penalty", 0.0) > 0.0 else ""
                )
                # Only the regimes that could actually have fired this
                # cycle. A regime outside ``allowed_regimes`` is never
                # scored and reports a constant 0.0 — whether it was
                # switched off by its ``disable_*_regime`` knob or simply
                # not offered by the current time window. Listing it says
                # nothing about why the side failed and buries the scores
                # that do: top_tier_adaptive disables four of the eight,
                # so half of every line was filler.
                score_detail = ",".join(
                    f"{REGIME_SKIP_LABELS[name]}={score:.1f}"
                    for name, score in decision["scores"].items()
                    if name in allowed_regimes
                )
                fail_reasons.append(
                    f"{side.value.lower()}_unqualified_no_qualifying_regime("
                    f"{score_detail}{penalty_suffix})"
                )
                continue
            for regime_name, regime_score, regime_norm in decision["build_order"]:
                build_queue.append((side, regime_name, regime_score, regime_norm, decision))

        # Stable sort by NORMALISED score desc (see
        # ``_normalized_regime_score``) — ties default to preferred_sides
        # insertion order (LONG before SHORT) since side_decisions was
        # built in that order.
        build_queue.sort(key=lambda item: item[3], reverse=True)

        # Arms whose wait ran out. They go to the FRONT of the queue and
        # skip the per-cycle gates -- see ``_expired_armed_retests``. The
        # gates were all satisfied when the arm was created, and requiring
        # them again at expiry is what let INTC run 117->124 on
        # 2026-09-21 with no entry.
        expired_arms = self._expired_armed_retests(c.symbol, allowed_regimes)
        expired_keys = {(side, regime) for side, regime, _arm in expired_arms}
        queue: list[tuple[bool, Side, str, float, float, dict[str, Any]]] = [
            (True, side, regime,
             float(arm.get("regime_score", 0.0)),
             float(arm.get("regime_score_norm", 0.0)),
             # index_ok stays False: the exemption is `pre_validated`,
             # explicitly, rather than a synthetic True smuggling the
             # behaviour through the gate. A fake value here would also
             # make any test of the exemption pass vacuously.
             {"index_ok": False, "bias_penalty": 0.0,
              "scores": {regime: float(arm.get("regime_score", 0.0))},
              "expired_arm": arm})
            for side, regime, arm in expired_arms
        ]
        # A regime with an expired arm is already represented above; its
        # queue entry would re-arm at a fresh level on the same cycle.
        queue += [
            (False, side, regime, score, norm, decision)
            for side, regime, score, norm, decision in build_queue
            if (side, regime) not in expired_keys
        ]

        return queue

    def _run_build_queue(self, read: _CandidateRead,
                         queue: list[tuple[bool, Side, str, float, float, dict[str, Any]]],
                         fail_reasons: list[str],
                         data) -> tuple[Signal | None, dict[str, Any] | None, str | None, float]:
        """Walk the queue: the per-regime gates, the armed retest, then the
        regime's builder; the first signal wins. Returns ``(signal, its side's
        decision, regime, normalised score)``, or ``(None, None, None, 0.0)``
        with every refusal added to *fail_reasons*."""
        c, frame, ltf = read.c, read.frame, read.ltf
        close, atr, vol_scale, vol_widening = read.close, read.atr, read.vol_scale, read.vol_widening
        day_strength, thresholds = read.day_strength, read.thresholds
        explicit_side_decided, decided_side, side_vote_note = (
            read.explicit_side_decided, read.decided_side, read.side_vote_note)

        best_signal: Signal | None = None
        winning_decision: dict[str, Any] | None = None
        winning_regime: str | None = None
        winning_norm: float = 0.0
        for pre_validated, side, regime_name, regime_score, regime_norm, decision in queue:
            index_ok = decision["index_ok"]

            # An expired arm skips every gate below: all three were
            # satisfied when it armed, and re-imposing them means the
            # fallback only fires on a cycle where the setup happens to
            # fully re-qualify. See ``_expired_armed_retests``.
            #
            # Explicit side decision, applied per regime. Direction-
            # following regimes must match the vote; the mean-reversion
            # pair (range / sr_scalp) is exempt because it enters against
            # current price action by design, and orb is exempt via its
            # own window bypass. See the side-decision block in
            # ``_read_candidate``.
            if (
                not pre_validated
                and regime_name in SIDE_DECISION_REGIMES
                and explicit_side_decided is False
                and side_vote_note
            ):
                fail_reasons.append(
                    f"{side.value.lower()}_build_failed_{regime_name}_side_undecided({side_vote_note})"
                )
                continue
            if not pre_validated and (
                regime_name in SIDE_DECISION_REGIMES
                and decided_side is not None
                and side != decided_side
            ):
                fail_reasons.append(
                    f"{side.value.lower()}_build_failed_{regime_name}_side_decision_opposed"
                    f"(decided={decided_side.value})"
                )
                continue

            # Index confirmation for trend/pullback/vol_squeeze/momentum/
            # vwap_reclaim — these momentum-family regimes need a market-
            # aligned tape. Range AND sr_scalp are
            # exempt — both are mean-reversion theses where a divergent
            # index reads as "the index doesn't dictate intra-symbol
            # rotation between levels." The orb regime is also exempt (not
            # in the set) — its range-break is the directional proof. Index
            # failure on one regime falls through to the next in the queue.
            if not pre_validated and regime_name in INDEX_CONFIRMED_REGIMES and not index_ok:
                fail_reasons.append(
                    f"{side.value.lower()}_build_failed_{regime_name}_index_not_confirmed"
                )
                continue

            # Confirmation-bar trigger (Fix B, 2026-05-27). For
            # direction-following regimes, require the LAST FULLY
            # CLOSED LTF bar to confirm direction before we call the
            # build method (which would otherwise enter at current
            # close on the same bar that scored the regime). Catches
            # single-bar fakeouts where the in-progress bar tipped a
            # score threshold but the move didn't carry. Range and
            # sr_scalp are exempt — both are mean-reversion theses
            # where the LAST CLOSED bar moves AGAINST the entry
            # direction by design. vwap_reclaim is also exempt — its prior
            # closed bar is the flush (red, below VWAP) and would fail the
            # green-bar check; the reclaim's own VWAP-buffer + volume
            # confirmation stands in. The orb regime is also exempt (not in
            # the set) — its range-break is the confirmation.
            if not pre_validated and (
                regime_name in CONFIRMATION_BAR_REGIMES
                and bool(self.params.get("require_entry_confirmation_bar", True))
                and not self._entry_bar_confirms(side, ltf)
            ):
                fail_reasons.append(
                    f"{side.value.lower()}_build_failed_{regime_name}_no_confirmation_bar"
                )
                continue

            # Armed retest (2026-09-20). For trend / momentum,
            # qualifying does not mean entering: the level that was
            # cleared is remembered and the entry waits for price to come
            # back and retest it, or for the wait to expire. See
            # ``_armed_retest_verdict``. Placed AFTER the index and
            # confirmation-bar gates so a setup that would have been
            # rejected anyway never arms; nor does a close that has not
            # crossed the level, which falls through to the builder's
            # fresh-breakout check.
            #
            # That ordering is load-bearing, not incidental. While price
            # is pulling back the last CLOSED bar is against the trade, so
            # ``require_entry_confirmation_bar`` rejects and the verdict is
            # never consulted -- which is correct: there is nothing to
            # decide mid-pullback. The first cycle that reaches the verdict
            # again is the one AFTER a bar closed back the trade's way,
            # which is exactly the reclaim the retest is waiting for. Move
            # this above the confirmation gate and entries would fire
            # partway down the retrace.
            retest_meta: dict[str, Any] = {}
            breakout_confirmed = False
            if pre_validated:
                arm = decision["expired_arm"]
                armed_level = float(arm["trigger_level"])
                retest_meta = {
                    "armed_retest_regime": regime_name,
                    "armed_retest_level": round(armed_level, 4),
                    "armed_retest_waited_minutes": round(
                        float(arm.get("waited_minutes", 0.0)), 2),
                    "armed_retest_status": "expired_market_entry",
                }
                # The fallback enters while the breakout the arm recorded
                # still HOLDS: close on the trade's side of the armed
                # level, the same `reclaimed` test a retest entry passes.
                # Faded means price came back through that level.
                #
                # It used to be left to the builder's fresh-breakout check
                # (`breakout_confirmed` False), which asks a different
                # question -- is THIS bar a new N-bar extreme. A runaway
                # that never retested, the case the fallback exists for,
                # is rarely at a fresh extreme on the exact expiry minute:
                # on 2026-09-22 all three arms that reached expiry (CRM,
                # ADBE, NFLX shorts, touched=0, still through their levels)
                # died on `no_fresh_breakdown` while the moves carried on.
                # The fallback only ever fired by coincidence.
                held = close > armed_level if side == Side.LONG else close < armed_level
                if not held:
                    fail_reasons.append(
                        f"{side.value.lower()}_build_failed_{regime_name}_armed_retest_faded("
                        f"level={armed_level:.4f},close={close:.4f})"
                    )
                    continue
                breakout_confirmed = True
            elif regime_name in ARMED_RETEST_REGIMES:
                verdict = self._armed_retest_verdict(
                    c.symbol, side, regime_name, close, atr, ltf, frame,
                    regime_score=regime_score, regime_norm=regime_norm)
                if verdict["status"] == "wait":
                    fail_reasons.append(
                        f"{side.value.lower()}_build_failed_{regime_name}_"
                        f"{verdict['reason']}"
                    )
                    continue
                retest_meta = dict(verdict.get("metadata") or {})
                # A CONFIRMED retest satisfies the builder's fresh-breakout
                # gate in advance; a wait never reaches the builder.
                breakout_confirmed = verdict["status"] == "enter"

            sig = None
            if regime_name == "trend":
                sig = self._build_trend_signal(c, side, close, atr, ltf, frame, regime_score, data, vol_widening=vol_widening, vol_scale=vol_scale, breakout_confirmed=breakout_confirmed)
            elif regime_name == "pullback":
                sig = self._build_pullback_signal(c, side, close, atr, ltf, frame, regime_score, data, vol_widening=vol_widening, vol_scale=vol_scale)
            elif regime_name == "range":
                sig = self._build_range_signal(c, side, close, atr, frame, regime_score, data, vol_widening=vol_widening, vol_scale=vol_scale)
            elif regime_name == "vol_squeeze":
                sig = self._build_vol_squeeze_signal(c, side, close, atr, frame, regime_score, data, vol_widening=vol_widening, vol_scale=vol_scale)
            elif regime_name == "momentum":
                sig = self._build_momentum_signal(c, side, close, atr, frame, regime_score, data, vol_widening=vol_widening, vol_scale=vol_scale, breakout_confirmed=breakout_confirmed)
            elif regime_name == "sr_scalp":
                sig = self._build_sr_scalp_signal(c, side, close, atr, frame, regime_score, data, vol_widening=vol_widening, vol_scale=vol_scale)
            elif regime_name == "orb":
                sig = self._build_orb_signal(c, side, close, atr, frame, regime_score, data, vol_widening=vol_widening, vol_scale=vol_scale)
            elif regime_name == "vwap_reclaim":
                sig = self._build_vwap_reclaim_signal(c, side, close, atr, frame, regime_score, data, vol_widening=vol_widening, vol_scale=vol_scale)

            if sig is not None:
                # How this entry was reached: on the retest the regime
                # waited for, or at market after the wait expired. Read by
                # the session report to tell the two populations apart.
                if retest_meta and isinstance(sig.metadata, dict):
                    sig.metadata.update(retest_meta)
                # Tier 3b: on high-conviction days, loosen the
                # peak-giveback threshold so a 2R+ runner doesn't get
                # cut by a normal 50% retracement. Override is stamped
                # per-trade based on day_strength at ENTRY; the trade
                # manager (TradeManager._peak_giveback_triggered) reads
                # it from position.metadata at management time. Falls
                # back to the global config default when not set.
                if day_strength is not None:
                    conv_threshold = float(self.params.get("peak_giveback_high_conviction_day_strength_pct", 2.0))
                    if abs(day_strength) >= conv_threshold:
                        override_r = float(self.params.get("peak_giveback_high_conviction_min_r", 2.0))
                        if isinstance(sig.metadata, dict):
                            sig.metadata["peak_giveback_min_r_override"] = override_r
                            sig.metadata["peak_giveback_high_conviction_day_strength"] = round(float(day_strength), 4)
                # Stamp the volatility widening factor for post-mortem.
                # Always stamped when Tier 2a is enabled (even when the
                # factor is 1.0 — disambiguates "feature disabled" from
                # "feature enabled but inactive this cycle").
                if bool(self.params.get("atr_aware_stop_enabled", True)) and isinstance(sig.metadata, dict):
                    sig.metadata["vol_widening_factor"] = round(float(vol_widening), 4)
                # Stamp the per-sector confirmation indices used at
                # entry, for the entry and exit records (ENTRY_CONTEXT,
                # EXIT_CONTEXT): which sector ETFs confirmed the trade.
                # Until 2026-09-27 the adaptive ladder also re-read them
                # at the target, for its target-exit suppression (removed;
                # see TradeManager's adaptive ladder). With no index
                # symbols (small_cap_squeeze) the list is empty.
                if isinstance(sig.metadata, dict):
                    sig.metadata["confirmation_indices"] = list(self._indices_for_symbol(c.symbol))
                    # Cross-regime-comparable score: the manifest's
                    # signal_priority.primary_field, so the shared entry
                    # policy's rank_key ranks competing signals on it when
                    # there are more signals than free position slots
                    # (raw regime scores are on per-regime scales; see
                    # _normalized_regime_score). The raw ``regime_score``
                    # next to it stays for reporting.
                    sig.metadata["regime_score_normalized"] = round(float(regime_norm), 4)
                    # ...and its rank_unit_field (2026-09-24): rank_key
                    # adds shared_score_weight x shared_context_score x
                    # this to it, i.e. the shared terms at the scale of a
                    # raw point of this regime's score over the floor it
                    # was normalised against (a SHORT's includes the
                    # premium, as in ``_score_sides``; an expired arm carries the score
                    # it armed with, normalised against this same floor).
                    floor = thresholds[regime_name] + self._short_score_premium(side)
                    sig.metadata["regime_rank_unit"] = round(self._regime_rank_unit(regime_name, floor), 6)
                    stats = self._symbol_daily_stats(c.symbol, data)
                    if stats is not None:
                        if stats.has_scale:
                            sig.metadata["daily_adr_pct"] = round(float(stats.adr_pct), 5)
                            sig.metadata["vol_scale"] = round(self._vol_scale(c.symbol, data), 4)
                        if stats.has_beta:
                            sig.metadata["sector_beta"] = round(float(stats.beta), 3)
                            sig.metadata["sector_beta_benchmark"] = stats.beta_benchmark
                best_signal = sig
                winning_decision = decision
                winning_regime = regime_name
                winning_norm = regime_norm
                break

            # Build attempted but rejected by a hard gate inside the
            # builder. ``_set_build_failure`` already side-prefixes most
            # rejection tags (long_/short_); only prefix here when the
            # tag isn't already side-tagged to avoid stuttered buckets
            # like ``long_long_below_support_zone`` in the EOD summary.
            failure = self._consume_build_failure(c.symbol, regime_name) or f"{regime_name}_signal_build_failed"
            side_tag = side.value.lower()
            if failure.startswith(("long_", "short_")):
                fail_reasons.append(f"build_failed_{failure}")
            else:
                fail_reasons.append(f"{side_tag}_build_failed_{failure}")

        return best_signal, winning_decision, winning_regime, winning_norm

    def _record_candidate(self, read: _CandidateRead,
                          queue: list[tuple[bool, Side, str, float, float, dict[str, Any]]],
                          fail_reasons: list[str], best_signal: Signal | None,
                          winning_decision: dict[str, Any] | None, winning_regime: str | None,
                          winning_norm: float) -> None:
        """Record the candidate's decision: the signal with its regime and
        score, or the skip with the regime that came closest."""
        c = read.c
        if best_signal is not None:
            # Stamp the soft-bias penalty value on the success path too
            # so post-mortem can see whether a winner was nearly killed
            # by bias drag. Sourced from the winning side's decision.
            detail_payload: dict[str, Any] = {}
            if winning_decision is not None:
                bp = float(winning_decision.get("bias_penalty", 0.0) or 0.0)
                if bp > 0.0:
                    detail_payload["bias_pen"] = round(bp, 4)
                mbp = float(winning_decision.get("mr_bias_penalty", 0.0) or 0.0)
                if mbp > 0.0 and winning_regime in MEAN_REVERSION_REGIMES:
                    detail_payload["mr_bias_pen"] = round(mbp, 4)
                if winning_regime is not None:
                    detail_payload["regime"] = winning_regime
                    scores_dict = winning_decision.get("scores") or {}
                    detail_payload["score"] = round(float(scores_dict.get(winning_regime, 0.0)), 4)
                    detail_payload["score_norm"] = round(float(winning_norm), 4)
            self._record_entry_decision(
                c.symbol, "signal", [best_signal.reason],
                details=detail_payload or None,
            )
        else:
            # Stamp WHICH regime the skip came from. Without this the
            # `family` column in decisions.csv is "none" on every skipped
            # row, so a post-mortem can establish that ~20% of decisions
            # die on `no_fresh_breakout` but not whether that is `trend`
            # or `momentum` — two regimes with very different lookbacks
            # (25 bars on the LTF vs 6 on the base frame) and very
            # different trade counts. The success path above already
            # stamps `regime`; this is the same information on the path
            # that produces almost every row.
            #
            # `entry_family` is the key `_decision_entry_family` reads, so
            # this populates the EXISTING `family=` log field and CSV
            # column — no log-format, parser or schema change.
            skip_details: dict[str, Any] = {}
            if queue:
                # Reads `queue`, not `build_queue`: an expired arm is
                # tried without being in the score-ordered queue, so a
                # cycle whose only attempt was a fallback would otherwise
                # report `none_qualified` while a builder had in fact
                # rejected it.
                #
                # Sorted by normalised score desc, so [0] is the regime
                # that came closest to producing a signal (expired arms
                # sit at the front — they were already validated).
                _pre, _q_side, top_regime, top_score, top_norm, _q_decision = queue[0]
                skip_details["entry_family"] = str(top_regime)
                # How far the best candidate regime was from its
                # threshold — the difference between "nothing was close"
                # and "it missed by 0.1 and the threshold may be wrong".
                skip_details["regime_score"] = round(float(top_score), 3)
                skip_details["regime_score_norm"] = round(float(top_norm), 4)
                skip_details["regimes_tried"] = len(queue)
            else:
                # Nothing cleared its score threshold on either side. A
                # different failure from "a builder rejected it", and the
                # two are worth telling apart in the histogram.
                skip_details["entry_family"] = "none_qualified"
            self._record_entry_decision(
                c.symbol, "skipped", fail_reasons or ["no_setup"],
                details=skip_details,
            )

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------
    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
