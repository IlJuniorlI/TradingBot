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
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from typing import Any

from ..shared import (
    Candidate,
    Position,
    Side,
    Signal,
    _bar_close_position,
    _bar_wick_fractions,
    insufficient_bars_reason,
    _optional_float,
    _safe_float,
    _same_day_mask,
    _session_open_price,
    EQUITY_RTH_OPEN,
    EQUITY_STREAM_START,
    equity_session_state,
    get_session_indicator_window,
    now_et,
    parse_hhmm,
    pd,
)
from ..strategy_base import BaseStrategy
from ...daily_stats import SymbolDailyStats, build_symbol_stats, volatility_scale

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

# Regimes that ARM instead of entering the moment they qualify. Both can only
# fill at an N-bar extreme -- `close > max(high of the previous N bars)` -- so
# the fill sits at the highest price in 25 (trend) or 6 (momentum) minutes by
# construction, and the stop then lands inside the ordinary retrace band. The
# other four are excluded on their own evidence: `pullback` already requires a
# 25-50% leg retracement before it fires, `range` and `vwap_reclaim` enter
# AGAINST the move by design, and `vol_squeeze` measured no post-entry retrace
# above baseline at all (-0.024R across 11 archived trades, against trend's
# +0.641R across 9) -- arming it would add latency for nothing.
ARMED_RETEST_REGIMES = frozenset({"trend", "momentum"})

# Per-regime score ceilings — the maximum each _score_* method can return.
# Used to normalise scores onto a common 0..1 scale before the build-order
# auction and the cross-signal slot auction compare them. Without this a
# trend at 4.5/6.0 (25% of its headroom) outranks an sr_scalp at 4.4/4.5
# (93% of its headroom) purely because trend's scorer has more components.
# Keep in sync with the _score_* methods; ``test_regime_score_ceilings``
# asserts each scorer cannot exceed its entry here.
REGIME_SCORE_CEILINGS = {
    "trend": 6.0,
    "pullback": 5.0,
    "range": 5.0,
    "vol_squeeze": 6.5,
    "momentum": 6.0,
    "sr_scalp": 5.0,
    "orb": 5.0,
    "vwap_reclaim": 5.0,
}

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


class TopTierAdaptiveStrategy(BaseStrategy):
    strategy_name = "top_tier_adaptive"

    def __init__(self, config):
        super().__init__(config)
        # Per-symbol rolling window of recent LIVE directional bias
        # observations (output of ``_compute_live_directional_bias``).
        # Trailing-bias memory for Fix A (2026-04-23): when the current
        # bar's live bias is None (day_strength within the neutral band),
        # infer the effective side from recent observations if one side
        # dominates.
        self._recent_directional_bias: dict[str, deque[Side | None]] = {}
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

    # ------------------------------------------------------------------
    # Watchlist — include all configured index confirmation ETFs so they
    # get history fetching, streaming, and appear in the bars dict. The
    # specific ETFs depend on which sectors the active universe touches
    # (see ``sector_index_map`` in config) — could be sector ETFs
    # (XLK/XLE/XLB/...) and/or broad-market (SPY/QQQ).
    # ------------------------------------------------------------------
    def active_watchlist(self, candidates: list[Candidate], positions: dict[str, Position]) -> set[str]:
        symbols = super().active_watchlist(candidates, positions)
        index_symbols = [str(s).upper().strip() for s in (self.params.get("index_symbols") or []) if str(s).strip()]
        symbols.update(index_symbols)
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

    # ------------------------------------------------------------------
    # Index confirmation
    # ------------------------------------------------------------------
    def _indices_for_symbol(self, symbol: str) -> list[str]:
        """Return the index ETFs to consult when confirming trades on
        *symbol*. Walks ``sector_groups`` to find which sector owns the
        symbol, then reads ``sector_index_map[sector]`` for the per-sector
        ETF list.

        Falls back to the universe-wide ``index_symbols`` when the symbol
        isn't in any sector OR the sector has no per-sector ETF mapping.
        Backward-compat: dropping ``sector_index_map`` from config = the
        old "OR across every index_symbols entry" behavior.

        The fallback is intentional, not lazy — it lets a user opt-in
        sector-by-sector instead of forcing them to map all 11 GICS sectors
        upfront. Sectors without a map entry retain the broad-market
        confirmation path.
        """
        fallback = [
            str(s).upper().strip()
            for s in (self.params.get("index_symbols") or ["SPY", "QQQ"])
            if str(s).strip()
        ]
        sector_groups = self.params.get("sector_groups") or {}
        sector_index_map = self.params.get("sector_index_map") or {}
        if not sector_groups or not sector_index_map:
            return fallback
        symbol_upper = str(symbol).upper()
        for sector_name, members in sector_groups.items():
            if not isinstance(members, (list, tuple)):
                continue
            if symbol_upper in {str(m).upper() for m in members if m}:
                mapped = sector_index_map.get(sector_name)
                if mapped:
                    cleaned = [str(s).upper().strip() for s in mapped if str(s).strip()]
                    if cleaned:
                        return cleaned
                break
        return fallback

    def _leg_anchor_vwap(self, frame: pd.DataFrame) -> float | None:
        """VWAP anchored at the CURRENT LEG's origin instead of at 09:30.

        Session VWAP is a whole-day average, so after a morning flush it sits
        far above price and "price has reclaimed VWAP" only becomes true long
        after a reversal is under way. Measured on a 3% flush that retraced
        87%: session VWAP was reclaimed 44 minutes after the low with half the
        move already gone, while a VWAP anchored at the low was reclaimed
        after 1 minute. Anchoring to the leg is what lets the confirmation
        describe the move that is actually happening.

        The anchor is today's more recent extreme — the low when we are in an
        up-leg, the high when we are in a down-leg. Two guards stop it
        chasing every wiggle, which would read an ordinary pullback as a
        reversal and confirm the wrong side:

          * ``leg_anchor_min_age_bars`` — the extreme must be far enough back
            to be a confirmed pivot. A pullback high in a grind is only a few
            bars old; a reversal pivot is not. This is the guard that does
            the work: at 15 bars an ordinary grind-with-pullbacks produced 12
            spurious counter-trend confirmations, at 20 it produced none.
          * ``leg_anchor_min_impulse_pct`` — price must have travelled far
            enough from the anchor for the leg to mean anything.

        Returns ``None`` when there is no established leg, in which case the
        caller uses session VWAP — the correct reference when the session has
        not yet turned.
        """
        if frame is None or frame.empty or "volume" not in frame.columns:
            return None
        # Scan from where the REST of the strategy's session logic starts
        # (mirrors `_day_strength_session_open`): the RTH open, or the 07:00
        # equity-stream open in extended-indicator mode. Scanning from
        # midnight instead lets a thin pre-market print become the anchor —
        # on a frame carrying an 08:00 dump that moved the anchor off the
        # session low entirely and flipped the verdict, while the rest of the
        # strategy was still measuring from 09:30.
        #
        # Taken by POSITION: today's bars are a contiguous tail of a
        # time-ordered frame, and `_same_day_mask` maps a Python lambda over
        # EVERY bar of the merged frame. This runs once per peer per side, so
        # on a 12-peer group that was ~15ms of per-candidate overhead for a
        # slice `searchsorted` does in microseconds. `tz=index.tz` covers
        # tz-aware and naive indexes identically.
        index = frame.index
        session_start = (
            EQUITY_STREAM_START if get_session_indicator_window() == "extended"
            else EQUITY_RTH_OPEN
        )
        opened_at = pd.Timestamp(datetime.combine(now_et().date(), session_start), tz=index.tz)
        session = frame.iloc[index.searchsorted(opened_at):]
        if len(session) < 5:
            return None
        closes = session["close"].to_numpy(dtype=float)
        pos = max(int(closes.argmin()), int(closes.argmax()))
        if (len(closes) - 1 - pos) < int(self.params.get("leg_anchor_min_age_bars", 20)):
            return None
        anchor_px = closes[pos]
        if anchor_px <= 0:
            return None
        min_impulse = float(self.params.get("leg_anchor_min_impulse_pct", 0.005))
        if abs(closes[-1] - anchor_px) / anchor_px < min_impulse:
            return None
        leg = session.iloc[pos:]
        volume = leg["volume"].to_numpy(dtype=float)
        total = float(volume.sum())
        if not total > 0:
            return None
        typical = (leg["high"].to_numpy(dtype=float)
                   + leg["low"].to_numpy(dtype=float)
                   + leg["close"].to_numpy(dtype=float)) / 3.0
        return float((typical * volume).sum() / total)

    def _frame_agrees(self, side: Side, frame: pd.DataFrame | None) -> bool | None:
        """Does *frame*'s latest bar lean *side*? ``None`` when unreadable.

        The shared posture test — close vs a VWAP reference plus EMA9/EMA20
        alignment — used for both the sector ETF and each sector peer so the
        two confirmation paths answer the same question.

        The reference is session VWAP, or the current leg's anchored VWAP
        when ``leg_anchored_confirmation`` is on and a leg is established
        (see ``_leg_anchor_vwap``). On a trending or choppy session the two
        agree; they diverge only after the session has turned, which is
        exactly where session VWAP describes the wrong move.
        """
        if frame is None or frame.empty:
            return None
        last = frame.iloc[-1]
        close = _safe_float(last["close"])
        reference = None
        if bool(self.params.get("leg_anchored_confirmation", False)):
            reference = self._leg_anchor_vwap(frame)
        if reference is None:
            reference = _safe_float(last.get("vwap"), close)
        ema9 = _safe_float(last.get("ema9"), close)
        ema20 = _safe_float(last.get("ema20"), close)
        if side == Side.LONG:
            return bool(close > reference and ema9 >= ema20)
        return bool(close < reference and ema9 <= ema20)

    def _index_confirms(self, side: Side, symbol: str, bars: dict[str, pd.DataFrame], _data=None) -> bool:
        """Return True when *symbol*'s sector tape agrees with *side*.

        Two paths, chosen by how much of the sector the symbol itself is:

        **Breadth** (preferred). Counts how many of the symbol's OTHER
        ``sector_groups`` members lean *side*, and requires at least
        ``index_breadth_min_agree_frac`` of them. Used whenever at least
        ``index_breadth_min_peers`` peers have readable bars.

        **Sector ETF** (fallback). The original check — at least one mapped
        ETF leaning *side*. Used when the symbol has too few mapped peers to
        measure breadth (single-member sectors like healthcare/staples here).

        Breadth exists because the ETF test is close to circular on a mega-cap
        universe: AAPL+MSFT+NVDA+AVGO are roughly 45% of XLK, GOOG+META about
        45% of XLC, AMZN+TSLA about 40% of XLY. Asking XLK whether AAPL's
        move is confirmed substantially asks AAPL about AAPL, and it fails in
        the one case that matters — the mega cap moving against the rest of
        its sector. Peer breadth excludes the symbol itself, so it cannot
        confirm a move with that move.
        """
        if not bool(self.params.get("require_index_confirmation", True)):
            return True
        peers = self._sector_peers(symbol)
        min_peers = max(1, int(self.params.get("index_breadth_min_peers", 3)))
        if len(peers) >= min_peers:
            agree = 0
            readable = 0
            for peer in peers:
                verdict = self._frame_agrees(side, bars.get(peer))
                if verdict is None:
                    continue
                readable += 1
                if verdict:
                    agree += 1
            if readable >= min_peers:
                min_frac = float(self.params.get("index_breadth_min_agree_frac", 0.5))
                return (agree / readable) >= min_frac
        for sym in self._indices_for_symbol(symbol):
            if self._frame_agrees(side, bars.get(sym)) is True:
                return True
        return False

    def _index_neutral(self, symbol: str, bars: dict[str, pd.DataFrame]) -> bool:
        """Return True if the per-symbol indices (see ``_indices_for_symbol``)
        are not strongly directional — i.e. all are within 0.25% of their
        own VWAP. Used by the range regime's scoring bonus."""
        index_symbols = self._indices_for_symbol(symbol)
        for sym in index_symbols:
            frame = bars.get(sym)
            if frame is None or frame.empty:
                continue
            last = frame.iloc[-1]
            close = _safe_float(last["close"])
            vwap = _safe_float(last.get("vwap"), close)
            if vwap > 0 and abs((close - vwap) / vwap) >= 0.0025:
                return False
        return True

    def _sector_day_strength(self, symbol: str, bars: dict[str, pd.DataFrame]) -> float | None:
        """Return the first available sector ETF's day_strength (close vs
        session_open, in percent) for *symbol*. Used by the relative-strength
        gate in entry_signals to compute how much the candidate is
        leading/lagging its sector. Returns ``None`` when no sector ETF
        bars are loaded or all session-open lookups fail."""
        for sym in self._indices_for_symbol(symbol):
            frame = bars.get(sym)
            if frame is None or frame.empty:
                continue
            try:
                close = _safe_float(frame.iloc[-1]["close"])
            except Exception:
                continue
            _, ds = self._compute_live_bias_and_day_strength(frame, close)
            if ds is not None:
                return ds
        return None

    # ------------------------------------------------------------------
    # Daily statistics — per-symbol volatility scale + sector beta
    # ------------------------------------------------------------------
    def _symbol_daily_stats(self, symbol: str, data=None) -> SymbolDailyStats | None:
        """Cached :class:`SymbolDailyStats` for *symbol*, or ``None``.

        Mega-cap universes span a wide volatility range (COST/V/TMUS around
        0.9% ADR, NVDA/TSLA/AMD around 3%+), so a parameter written as a flat
        percentage is a different gate on every name. This resolves the two
        daily-horizon numbers that let the strategy state thresholds in
        symbol-relative terms: ``adr_pct`` (the scale) and ``beta`` versus the
        symbol's own sector ETF (the expected share of a sector move).

        Costs one Schwab daily ``price_history`` call per symbol per ET day
        via ``MarketDataStore.get_daily_history``; everything after that is a
        dict lookup. Returns ``None`` when the feed is unavailable or the
        fetch failed — callers must gate on that explicitly instead of
        assuming a default ADR or a beta of 1.0.
        """
        if data is None or not hasattr(data, "get_daily_history"):
            return None
        today = now_et().date()
        if self._daily_stats_date != today:
            self._daily_stats.clear()
            self._daily_stats_date = today
        key = str(symbol).upper().strip()
        cached = self._daily_stats.get(key)
        if cached is not None:
            return cached
        try:
            symbol_daily = data.get_daily_history(key)
        except Exception:
            return None
        if symbol_daily is None or symbol_daily.empty:
            return None
        benchmark = None
        benchmark_daily = None
        indices = self._indices_for_symbol(key)
        if indices:
            benchmark = indices[0]
            try:
                benchmark_daily = data.get_daily_history(benchmark)
            except Exception:
                benchmark_daily = None
            if benchmark_daily is None or benchmark_daily.empty:
                benchmark_daily = None
        stats = build_symbol_stats(
            key,
            symbol_daily,
            benchmark_symbol=benchmark if benchmark_daily is not None else None,
            benchmark_frame=benchmark_daily,
            adr_lookback_days=int(self.params.get("adr_lookback_days", 20)),
            beta_lookback_days=int(self.params.get("beta_lookback_days", 60)),
        )
        self._daily_stats[key] = stats
        return stats

    def _vol_scale(self, symbol: str, data=None) -> float:
        """Multiplier that converts a percent-of-price parameter tuned on a
        reference-volatility name into this symbol's equivalent.

        ``reference_adr_pct`` is the ADR the existing absolute thresholds were
        written against; a symbol running twice that gets 2.0. Returns 1.0
        (parameters unchanged) when scaling is disabled or the symbol has no
        usable daily stats, so a feed outage degrades to the previous
        behaviour rather than to an arbitrary number.
        """
        if not bool(self.params.get("volatility_scaled_thresholds", True)):
            return 1.0
        stats = self._symbol_daily_stats(symbol, data)
        return volatility_scale(
            stats,
            float(self.params.get("reference_adr_pct", 0.018)),
            min_scale=float(self.params.get("volatility_scale_min", 0.6)),
            max_scale=float(self.params.get("volatility_scale_max", 2.2)),
        )

    @staticmethod
    def _normalized_regime_score(regime: str, score: float, threshold: float) -> float:
        """Fraction of a regime's own headroom that *score* used, in 0..1.

        ``(score - threshold) / (ceiling - threshold)`` — 0.0 sits exactly on
        the regime's qualifying floor, 1.0 at the most its scorer can return.
        This is the only form in which two regimes' scores may be compared:
        raw scores are on per-regime scales (see REGIME_SCORE_CEILINGS).

        A regime missing from REGIME_SCORE_CEILINGS, or one whose configured
        threshold is at or above its ceiling, degenerates to 0.0 rather than
        raising — a mis-set threshold should surface as "never wins the
        auction", not as a crash mid-cycle.
        """
        ceiling = REGIME_SCORE_CEILINGS.get(regime)
        if ceiling is None:
            return 0.0
        headroom = float(ceiling) - float(threshold)
        if headroom <= 0.0:
            return 0.0
        return max(0.0, min(1.0, (float(score) - float(threshold)) / headroom))

    def signal_priority_key(self, signal, candidate, *, metadata, strength,
                            candidate_activity_score, rank):
        """Rank competing signals by normalised regime score.

        The gatekeeper's generic path sorts on raw ``regime_score``, which is
        not comparable across this strategy's regimes (see
        ``_normalized_regime_score``) — so with more signals than free
        position slots, slots went to whichever regime's scorer had the
        highest ceiling rather than to the best setup. ``final_priority_score``
        — which carries the structure / pattern / candle / S/R / FVG quality
        work — only ever acted as a tiebreak between identical raw scores.

        Ordering here: normalised regime score, then ``final_priority_score``,
        then screener activity, then candidate rank.
        """
        _ = signal, candidate
        return (
            float(_safe_float(metadata.get("regime_score_normalized"), 0.0) or 0.0),
            float(strength),
            float(candidate_activity_score),
            -float(rank),
        )

    # ------------------------------------------------------------------
    # Side asymmetry
    #
    # Every threshold in this strategy is shared between LONG and SHORT, which
    # assumes the two sides are mirror images. On an equity universe they are
    # not:
    #   * Upside moves in crowded names are faster and sharper than downside
    #     ones — a short covering into a squeeze needs more stop room to
    #     survive the same amount of "normal" adverse movement.
    #   * Equities drift up over time, so a short is fighting the base rate
    #     and deserves a higher bar to fire at all.
    #   * That same drift means short profits are less durable, so taking them
    #     sooner is worth more than holding for the last fraction of R.
    # These three multipliers encode exactly those three asymmetries rather
    # than duplicating the whole parameter block per side. All default to
    # symmetric (premium 0.0, mults 1.0), so a preset that does not set them
    # behaves exactly as before.
    # ------------------------------------------------------------------
    def _short_score_premium(self, side: Side) -> float:
        """Extra regime score a SHORT must clear beyond its normal floor."""
        if side != Side.SHORT:
            return 0.0
        return max(0.0, float(self.params.get("short_min_score_premium", 0.0)))

    def _side_stop_buffer_mult(self, side: Side) -> float:
        """Stop-buffer multiplier for *side* — widens shorts against squeezes."""
        if side != Side.SHORT:
            return 1.0
        return max(0.1, float(self.params.get("short_stop_buffer_mult", 1.0)))

    def _side_target_rr_mult(self, side: Side) -> float:
        """Target-R:R multiplier for *side* — banks short profits sooner."""
        if side != Side.SHORT:
            return 1.0
        return max(0.1, float(self.params.get("short_target_rr_mult", 1.0)))

    def _pct_param(self, name: str, default: float, vol_scale: float) -> float:
        """A percent-of-price parameter, rescaled to the symbol's own ADR.

        Only percent-of-price thresholds go through here. Parameters already
        expressed in ATR multiples (``*_atr_mult``) are volatility-relative by
        construction and must NOT be scaled again — doing so would square the
        adjustment.
        """
        return float(self.params.get(name, default)) * float(vol_scale)

    def _sector_peers(self, symbol: str) -> list[str]:
        """Other members of *symbol*'s ``sector_groups`` entry.

        Feeds the breadth confirmation in ``_index_breadth_confirms``. Returns
        an empty list when the symbol is unmapped or alone in its sector.
        """
        symbol_upper = str(symbol).upper().strip()
        for members in (self.params.get("sector_groups") or {}).values():
            if not isinstance(members, (list, tuple)):
                continue
            cleaned = [str(m).upper().strip() for m in members if str(m or "").strip()]
            if symbol_upper in cleaned:
                return [m for m in cleaned if m != symbol_upper]
        return []

    @staticmethod
    def _recent_momentum_pct(ltf: pd.DataFrame, lookback_bars: int) -> float | None:
        """Return the percent change over the last ``lookback_bars`` of the
        LTF frame: ``(last_close - close_lookback_bars_ago) / close_ago * 100``.

        Used by the recent-momentum disagreement gate in ``entry_signals``
        to detect when the LOCAL trend (last N bars) contradicts the
        session-wide bias signals (which all derive from ``change_from_open``,
        a backward-looking quantity). On stocks that have reversed intraday,
        ``day_strength`` still reflects the original direction while recent
        price action has flipped; the gap is the signal.

        Returns ``None`` when there aren't enough bars or the prior close
        is non-positive (defensive against bad data)."""
        if ltf is None or len(ltf) < lookback_bars + 1:
            return None
        try:
            last_close = float(ltf.iloc[-1]["close"])
            prior_close = float(ltf.iloc[-(lookback_bars + 1)]["close"])
        except (KeyError, ValueError, TypeError, IndexError):
            return None
        if prior_close <= 0:
            return None
        return (last_close - prior_close) / prior_close * 100.0

    def _decide_side(
        self,
        ltf: pd.DataFrame,
        close: float,
        vwap: float,
        ema9: float,
        ema20: float,
    ) -> tuple[Side | None, dict[str, Any]]:
        """Pick the trade side from current price action (Fix A, 2026-05-27).

        Counts LONG and SHORT votes across four current-action signals:
          1. Recent return over the last N LTF bars (default 30 = 30 min at 1m)
          2. Close vs VWAP — the current leg's anchored VWAP under
             ``leg_anchored_confirmation``, session VWAP otherwise
          3. EMA9 vs EMA20
          4. Last 3 LTF bars' net direction (green-count vs red-count)

        Each signal contributes one LONG or one SHORT vote, or abstains
        when its read is genuinely neutral (recent return inside the noise
        threshold, price inside the VWAP dead-band, a mixed 3-bar colour
        count). Decision threshold is a MAJORITY OF THE SIGNALS THAT VOTED:
        a side wins when its votes ≥
        ``max(side_decision_min_agreeing, ceil(participating × side_decision_majority_frac))``
        AND opposing ≤ ``side_decision_max_opposing``. Scaling to the
        participating count matters because two of the four signals abstain
        often — an absolute threshold rejected unanimous 2-0 reads. Mixed
        reads return ``None`` and the candidate is skipped.

        This replaces the previous "evaluate both sides per regime, pick
        highest-scoring" implicit side selection. The old approach could
        pick SHORT just because the SHORT regime score was 0.5 higher
        even when every meaningful current-action signal said LONG.
        """
        long_votes = 0
        short_votes = 0
        breakdown: list[str] = []

        recent_lookback = max(1, int(self.params.get("side_decision_recent_lookback_bars", 6)))
        recent_threshold = float(self.params.get("side_decision_recent_threshold_pct", 0.1))
        recent_pct = self._recent_momentum_pct(ltf, recent_lookback)
        if recent_pct is not None:
            if recent_pct >= recent_threshold:
                long_votes += 1
                breakdown.append(f"recent+{recent_pct:.2f}%>L")
            elif recent_pct <= -recent_threshold:
                short_votes += 1
                breakdown.append(f"recent{recent_pct:.2f}%>S")
            else:
                breakdown.append(f"recent{recent_pct:+.2f}%>neutral")

        # VWAP arm. Under ``leg_anchored_confirmation`` this reads the SAME
        # reference ``_frame_agrees`` does — the current leg's anchored VWAP —
        # rather than session VWAP.
        #
        # Session VWAP is a whole-day average, so after a flush it sits far
        # above price and this arm keeps voting for the OLD direction well
        # into the reversal. Auditing each arm against the tape's actual
        # direction on a V-reversal: this one was wrong on 54 of 120 bars and
        # the EMA arm on 59, while the recent-return arm — the only genuinely
        # current-action signal — was wrong on 14. The two lagging arms are
        # also the two that vote most reliably (``recent`` and ``bars3`` carry
        # dead-bands and abstain often), so they outvote the accurate one.
        #
        # Leaving the layers on different references was also incoherent: the
        # vote and the confirmation gate disagreed about what "reclaimed VWAP"
        # meant for the same symbol on the same bar.
        vwap_buffer = float(self.params.get("side_decision_vwap_buffer_pct", 0.0005))
        vwap_reference = None
        if bool(self.params.get("leg_anchored_confirmation", False)):
            vwap_reference = self._leg_anchor_vwap(ltf)
        # Label the breakdown so an operator reading a `side_undecided(...)`
        # line can tell which reference produced the vote.
        vwap_label = "legvwap" if vwap_reference is not None else "vwap"
        if vwap_reference is None:
            vwap_reference = vwap
        if vwap_reference > 0:
            vwap_dist = (close - vwap_reference) / vwap_reference
            if vwap_dist > vwap_buffer:
                long_votes += 1
                breakdown.append(f"close>{vwap_label}>L")
            elif vwap_dist < -vwap_buffer:
                short_votes += 1
                breakdown.append(f"close<{vwap_label}>S")
            else:
                breakdown.append(f"close~{vwap_label}>neutral")

        if ema9 > ema20:
            long_votes += 1
            breakdown.append("ema9>ema20>L")
        elif ema9 < ema20:
            short_votes += 1
            breakdown.append("ema9<ema20>S")

        # Last-3-bars direction. Requires UNANIMITY to vote (2026-07-29).
        # The old rule (greens >= 2 -> LONG, else SHORT) had no neutral band,
        # so with three bars it ALWAYS voted — and in a downtrend the constant
        # two-green bounce bars voted LONG against the trend while carrying
        # the same weight as the EMA structure. Across 2,074 undecided tech
        # rows on 2026-07-28/29 it split 1,021 LONG / 1,053 SHORT: a coin
        # flip. Three consecutive same-colour bars is a real read; 2-of-3 is
        # not, so mixed counts now abstain like the other two signals.
        if ltf is not None and len(ltf) >= 3:
            try:
                last_3 = ltf.iloc[-3:]
                greens = sum(
                    1 for _, b in last_3.iterrows()
                    if float(b.get("close", 0)) > float(b.get("open", 0))
                )
            except (KeyError, ValueError, TypeError):
                greens = -1
            if greens == 3:
                long_votes += 1
                breakdown.append(f"3b:{greens}G>L")
            elif greens == 0:
                short_votes += 1
                breakdown.append(f"3b:{greens}G>S")
            elif greens > 0:
                breakdown.append(f"3b:{greens}G>neutral")

        # Decision threshold is a MAJORITY OF THE SIGNALS THAT ACTUALLY VOTED,
        # not an absolute count (2026-07-29). ``recent`` and ``vwap`` both
        # carry neutral dead-bands and abstain ~35% of the time, so the old
        # absolute ``side_decision_min_votes: 3`` was unreachable whenever two
        # signals sat out — a UNANIMOUS 2-0 read was rejected for having only
        # two votes. Observed directly on 2026-07-28:
        #   NVDA long=0 short=2 [recent-0.01%>neutral, close~vwap>neutral,
        #                        ema9<ema20>S, 3b:1G>S]   -> rejected
        # In a downtrend the reliably-achievable SHORT tally is 2 (vwap +
        # ema), so requiring 3 only admitted shorts when price was already
        # making a new low — i.e. at local exhaustion, right before the
        # bounce that then swept the stop. side_undecided was the single
        # largest blocker across the tech universe (3,506 of 12,935 rows)
        # during a week those names fell 4-12%.
        participating = long_votes + short_votes
        min_agreeing = int(self.params.get("side_decision_min_agreeing", 2))
        majority_frac = float(self.params.get("side_decision_majority_frac", 0.6))
        max_opposing = int(self.params.get("side_decision_max_opposing", 1))
        required = max(min_agreeing, math.ceil(participating * majority_frac))
        vote_info = {
            "long": long_votes,
            "short": short_votes,
            "participating": participating,
            "required": required,
            "breakdown": breakdown,
        }
        if long_votes >= required and short_votes <= max_opposing:
            return Side.LONG, vote_info
        if short_votes >= required and long_votes <= max_opposing:
            return Side.SHORT, vote_info
        return None, vote_info

    @staticmethod
    def _entry_bar_confirms(side: Side, ltf: pd.DataFrame) -> bool:
        """Confirmation-bar trigger (Fix B, 2026-05-27): the most recent
        FULLY CLOSED LTF bar must confirm direction before we enter.

        For LONG: ``last_closed.close > last_closed.open`` (green bar) AND
        ``last_closed.close > prev_closed.close`` (higher-high tape).
        Mirror for SHORT. Uses ``iloc[-2]`` for the last closed bar (since
        ``iloc[-1]`` is the in-progress bar) and ``iloc[-3]`` for the prior
        closed bar. Returns ``True`` when not enough bars (don't block on
        thin early-session data — other gates handle that case)."""
        if ltf is None or len(ltf) < 3:
            return True
        try:
            last_closed = ltf.iloc[-2]
            prev_closed = ltf.iloc[-3]
            last_open = float(last_closed.get("open", 0))
            last_close = float(last_closed.get("close", 0))
            prev_close = float(prev_closed.get("close", 0))
        except (KeyError, ValueError, TypeError, IndexError):
            return True
        if side == Side.LONG:
            return last_close > last_open and last_close > prev_close
        return last_close < last_open and last_close < prev_close

    # ------------------------------------------------------------------
    # Regime scoring
    # ------------------------------------------------------------------
    def _score_trend(self, side: Side, close: float, vwap: float, ema9: float, ema20: float,
                     adx: float, ret5: float, ret15: float, index_ok: bool) -> float:
        score = 0.0
        if side == Side.LONG:
            if close > vwap:
                score += 1.0
            if ema9 > ema20:
                score += 1.0
            if close > ema9:
                score += 1.0
            if ret5 > 0:
                score += 0.5
            if ret15 > 0:
                score += 0.5
        else:
            if close < vwap:
                score += 1.0
            if ema9 < ema20:
                score += 1.0
            if close < ema9:
                score += 1.0
            if ret5 < 0:
                score += 0.5
            if ret15 < 0:
                score += 0.5
        if adx >= float(self.params.get("min_adx14", 15.0)):
            score += 1.0
        if index_ok:
            score += 1.0
        return score

    def _opening_range(self, frame: pd.DataFrame) -> tuple[float | None, float | None]:
        """Today's opening range — the high/low of the first
        ``orb_range_minutes`` of RTH (from 09:30). Returns ``(None, None)``
        when the window has no bars yet (pre-open / range not formed).
        Computed on the raw (1m) frame for true extremes; pre-market bars are
        excluded by the 09:30 start so the range is RTH-anchored even in
        extended-hours mode."""
        if frame is None or frame.empty:
            return None, None
        range_min = max(1, int(self.params.get("orb_range_minutes", 15)))
        today = frame[_same_day_mask(frame, now_et().date())]
        if today.empty:
            return None, None
        rth_open = parse_hhmm("09:30")
        end_total = 9 * 60 + 30 + range_min
        range_end = parse_hhmm(f"{end_total // 60:02d}:{end_total % 60:02d}")
        in_window = today.index.to_series().map(lambda ts: rth_open <= ts.time() < range_end)
        window = today[in_window.values]
        if window.empty:
            return None, None
        return float(window["high"].max()), float(window["low"].min())

    def _score_orb(self, side: Side, close: float, atr: float, frame: pd.DataFrame) -> float:
        """Score the Opening Range Breakout. Fires only on a genuine break of
        today's opening range (above the high for LONG, below the low for
        SHORT). The hard range-validity + measured-move geometry runs at
        build time in ``_build_orb_signal``.

        Components (max 5.0):
          * +0.5  base (regime in play)
          * +2.5  price has broken the opening range on the trade side. No
                  break => base only (the bot waits for the break, it does
                  not trade inside the range).
          * +1.0  breakout conviction — the break clears the edge by at least
                  a small ATR buffer (not a 1-tick poke).
          * +1.0  range is a sane, tradeable size (>= orb_min_range_atr_mult
                  ATR and, when capped, <= orb_max_range_atr_mult ATR).
        """
        if atr <= 0 or close <= 0:
            return 0.0
        or_high, or_low = self._opening_range(frame)
        if or_high is None or or_low is None or or_high <= or_low:
            return 0.5
        score = 0.5
        broke = close > or_high if side == Side.LONG else close < or_low
        if not broke:
            return score
        score += 2.5
        buffer = float(self.params.get("orb_breakout_buffer_atr_mult", 0.05)) * atr
        clears = close > or_high + buffer if side == Side.LONG else close < or_low - buffer
        if clears:
            score += 1.0
        range_height = or_high - or_low
        min_range = float(self.params.get("orb_min_range_atr_mult", 0.5)) * atr
        max_range_mult = float(self.params.get("orb_max_range_atr_mult", 4.0))
        max_range = max_range_mult * atr if max_range_mult > 0 else float("inf")
        if min_range <= range_height <= max_range:
            score += 1.0
        return score

    def _score_pullback(self, side: Side, close: float, vwap: float, ema9: float, ema20: float,
                        adx: float, atr: float, trend_score: float, ltf: pd.DataFrame) -> float:
        min_trend = float(self.params.get("min_pullback_trend_score", 3.0))
        if trend_score < min_trend:
            return 0.0
        session_ltf = ltf[_same_day_mask(ltf, now_et().date())]
        score = 0.0
        touch_mult = float(self.params.get("pullback_ema_touch_atr_mult", 0.35))
        touch_dist = atr * touch_mult
        lookback = max(2, int(self.params.get("pullback_lookback_bars", 5)))
        recent = session_ltf.tail(lookback + 1).iloc[:-1] if len(session_ltf) > lookback else session_ltf.iloc[:-1]
        if recent.empty:
            return 0.0

        if side == Side.LONG:
            recent_low = _safe_float(recent["low"].min(), close)
            touched_ema20 = recent_low <= ema20 + touch_dist
            touched_vwap = recent_low <= vwap + touch_dist
            if touched_ema20 or touched_vwap:
                score += 1.5
            hold_mult = float(self.params.get("pullback_hold_atr_mult", 0.40))
            if recent_low >= ema20 - (atr * hold_mult):
                score += 1.0
            if close > ema9:
                score += 1.0
            close_pos = _bar_close_position(session_ltf if not session_ltf.empty else ltf)
            if close_pos >= 0.60:
                score += 0.5
        else:
            recent_high = _safe_float(recent["high"].max(), close)
            touched_ema20 = recent_high >= ema20 - touch_dist
            touched_vwap = recent_high >= vwap - touch_dist
            if touched_ema20 or touched_vwap:
                score += 1.5
            hold_mult = float(self.params.get("pullback_hold_atr_mult", 0.40))
            if recent_high <= ema20 + (atr * hold_mult):
                score += 1.0
            if close < ema9:
                score += 1.0
            close_pos = _bar_close_position(session_ltf if not session_ltf.empty else ltf)
            if close_pos <= 0.40:
                score += 0.5

        # Volume expansion on current bar
        vol_src = session_ltf if not session_ltf.empty else ltf
        vol = _safe_float(vol_src.iloc[-1].get("volume"), 0.0)
        vol_mean = _safe_float(recent["volume"].mean(), 1.0)
        if vol_mean > 0 and vol / vol_mean >= 1.10:
            score += 0.5
        if adx >= float(self.params.get("min_adx14", 15.0)):
            score += 0.5
        return score

    def _score_range(self, close: float, vwap: float, ema9: float, ema20: float,
                     frame: pd.DataFrame, index_neutral: bool, vol_scale: float = 1.0) -> float:
        session_frame = frame[_same_day_mask(frame, now_et().date())]
        score = 0.0
        max_vwap_dist = self._pct_param("range_max_vwap_dist_pct", 0.0020, vol_scale)
        max_ema_gap = float(self.params.get("range_max_ema_gap_pct", 0.0008))
        min_flips = int(self.params.get("range_min_flip_count", 3))
        lookback = max(8, int(self.params.get("range_lookback_bars", 20)))

        if vwap > 0 and abs((close - vwap) / vwap) <= max_vwap_dist:
            score += 1.5
        if close > 0 and abs((ema9 - ema20) / close) <= max_ema_gap:
            score += 1.0

        # Count VWAP crosses in the lookback
        recent = session_frame.tail(lookback)
        if "vwap" in recent.columns and len(recent) >= 4:
            closes = recent["close"].astype(float)
            vwaps = recent["vwap"].astype(float)
            above = closes > vwaps
            flips = int((above != above.shift()).sum()) - 1
            if flips >= min_flips:
                score += 1.0

        # Tight intraday range
        if len(recent) >= 8:
            range_pct = (float(recent["high"].max()) - float(recent["low"].min())) / max(close, 1.0)
            if range_pct <= self._pct_param("range_max_intraday_range_pct", 0.012, vol_scale):
                score += 1.0

        if index_neutral:
            score += 0.5
        return score

    def _score_vol_squeeze(self, side: Side, close: float, vwap: float, ema9: float,
                           ema20: float, atr: float, frame: pd.DataFrame, tech_ctx,
                           vol_scale: float = 1.0) -> float:
        """Score the Bollinger-squeeze breakout setup. Looks for compressed-range
        consolidation followed by a directional break out of the box. Compression
        comes from a tight box_range + low BB width OR an active BB squeeze flag
        on tech_ctx; the breakout comes from current close clearing the
        box-high (LONG) / box-low (SHORT) with a buffer. Volume and bar-close
        position add confirmation points. Ported (lighter) from
        volatility_squeeze_breakout/strategy.py."""
        lookback = max(6, int(self.params.get("vol_squeeze_lookback_bars", 12)))
        session_frame = frame[_same_day_mask(frame, now_et().date())]
        if len(session_frame) < lookback + 2:
            return 0.0
        # Look at the box (last lookback bars BEFORE the current one)
        last = session_frame.iloc[-1]
        prior = session_frame.iloc[:-1]
        box = prior.tail(lookback)
        if len(box) < lookback:
            return 0.0
        box_high = _safe_float(box["high"].max(), close)
        box_low = _safe_float(box["low"].min(), close)
        box_range = max(0.0, box_high - box_low)
        box_range_pct = (box_range / close) if close > 0 else 0.0
        box_range_atr = (box_range / atr) if atr > 0 else float("inf")
        max_range_pct = self._pct_param("vol_squeeze_max_range_pct", 0.012, vol_scale)
        max_range_atr = float(self.params.get("vol_squeeze_max_range_atr", 1.8))
        compression_box_ok = box_range_pct <= max_range_pct and box_range_atr <= max_range_atr
        bb_squeeze_flag = bool(getattr(tech_ctx, "bollinger_squeeze", False))
        bb_width_pct = _safe_float(getattr(tech_ctx, "bollinger_width_pct", None), 0.0)
        max_width_pct = float(self.params.get("vol_squeeze_max_width_pct", 0.05))
        compression_bb_ok = bb_squeeze_flag or (0.0 < bb_width_pct <= max_width_pct)

        score = 0.0
        if compression_box_ok:
            score += 2.0
        if compression_bb_ok:
            score += 1.0
        if compression_box_ok and bb_squeeze_flag:
            # Both compression signals agreeing — strong setup
            score += 0.5

        # Breakout detection (side-aware)
        buffer = self._pct_param("vol_squeeze_breakout_buffer_pct", 0.0008, vol_scale)
        if side == Side.LONG:
            broke_out = _safe_float(last["close"]) >= box_high * (1.0 + buffer)
        else:
            broke_out = _safe_float(last["close"]) <= box_low * (1.0 - buffer)
        if broke_out:
            score += 1.5

        # Volume confirmation: current bar volume vs box-median
        try:
            vol_baseline = max(1.0, float(box["volume"].median()))
        except Exception:
            vol_baseline = 1.0
        cur_vol = _safe_float(last.get("volume"), 0.0)
        min_vol_ratio = float(self.params.get("vol_squeeze_min_breakout_volume_ratio", 1.12))
        if cur_vol >= vol_baseline * min_vol_ratio:
            score += 0.5

        # Bar close position
        close_pos = _bar_close_position(session_frame)
        min_close_pos = float(self.params.get("vol_squeeze_min_bar_close_position", 0.63))
        if side == Side.LONG and close_pos >= min_close_pos:
            score += 0.5
        if side == Side.SHORT and close_pos <= (1.0 - min_close_pos):
            score += 0.5

        # Alignment with VWAP/EMA (cheap continuation confirmation)
        if side == Side.LONG and close > vwap and ema9 >= ema20:
            score += 0.5
        if side == Side.SHORT and close < vwap and ema9 <= ema20:
            score += 0.5
        return score

    def _score_momentum(self, side: Side, close: float, vwap: float, ema9: float,
                        ema20: float, ret15: float, frame: pd.DataFrame,
                        vol_scale: float = 1.0) -> float:
        """Score the momentum-from-open setup. The thesis is a stock with
        strong day-direction (live ``day_strength`` from session open) that's
        breaking out of a recent N-bar high (or low for SHORT), still in a
        trend-aligned posture, with ret15 confirming acceleration. Uses live
        frame data to compute ``day_strength`` rather than the screener's
        stale change_from_open. Renamed from ``_score_momentum_close``
        2026-05-12 when the regime was generalized from afternoon-only to
        post-ORB through close (skip-ORB) — the day_strength hard gate is
        what filters out chop, not the time window. Scoring logic ported
        (lighter) from the standalone ``momentum_close`` strategy."""
        # Compute live day_strength from session open + current close
        today_open = self._day_strength_session_open(frame)
        if today_open is None or today_open <= 0:
            return 0.0
        day_strength = (close - today_open) / today_open * 100.0
        min_day = self._pct_param("momentum_min_day_strength", 1.5, vol_scale)

        # Hard gate: side-correct day strength magnitude
        if side == Side.LONG and day_strength < min_day:
            return 0.0
        if side == Side.SHORT and day_strength > -min_day:
            return 0.0

        # N-bar breakout (use today's bars only)
        lookback = max(3, int(self.params.get("momentum_breakout_lookback_bars", 6)))
        session_frame = frame[_same_day_mask(frame, now_et().date())]
        if len(session_frame) < lookback + 1:
            return 0.0
        recent = session_frame.tail(lookback + 1).iloc[:-1]
        if recent.empty:
            return 0.0

        score = 0.0
        # day_strength magnitude scoring (tier-based)
        ds_abs = abs(day_strength)
        if ds_abs >= min_day:
            score += 1.0
        if ds_abs >= min_day * 2.0:
            score += 1.0
        if ds_abs >= min_day * 3.0:
            score += 0.5

        # Breakout above N-bar high (LONG) / below low (SHORT)
        if side == Side.LONG:
            breakout_level = _safe_float(recent["high"].max(), close)
            if close > breakout_level:
                score += 1.5
            if close > vwap:
                score += 1.0
            if ema9 >= ema20:
                score += 0.5
            if ret15 > 0:
                score += 0.5
        else:
            breakout_level = _safe_float(recent["low"].min(), close)
            if close < breakout_level:
                score += 1.5
            if close < vwap:
                score += 1.0
            if ema9 <= ema20:
                score += 0.5
            if ret15 < 0:
                score += 0.5
        return score

    def _score_vwap_reclaim(self, side: Side, close: float, vwap: float, ema9: float,
                            ema20: float, atr: float, frame: pd.DataFrame,
                            vol_scale: float = 1.0) -> float:
        """Score a VWAP-reclaim momentum re-entry (2026-05-30).

        Thesis (LONG; SHORT mirrors): a running name dips BELOW session VWAP — a
        quick flush that shakes out weak longs / trips stops — then RECLAIMS VWAP
        on a volume pop, the move (often a squeeze) re-igniting. That exact moment
        falls between trend (needs close>VWAP AND ema9>ema20), pullback (needs to
        HOLD above ema20), and momentum (needs a fresh N-bar high) — none of them
        catch it. Computed on the base 1m ``frame`` (VWAP is session-cumulative).

        Components (max ~5.0):
          * +0.5  base
          * +2.0  reclaim confirmed — a recent bar closed below VWAP within the
                  last ``vwap_reclaim_lookback_bars`` AND current close is back
                  above VWAP by a small buffer. No reclaim => 0 (regime sits out).
          * +1.0  volume pop on the reclaim bar (cur vol >= recent avg x ratio)
          * +0.7  trend intact — close > ema9
          * +0.3  structure aligned — ema9 >= ema20
          * +0.5  bounce character — wick on the flush side (bought the dip)
        """
        if vwap <= 0 or close <= 0 or atr <= 0 or frame is None or frame.empty:
            return 0.0
        session_frame = frame[_same_day_mask(frame, now_et().date())]
        lookback = max(2, int(self.params.get("vwap_reclaim_lookback_bars", 6)))
        if len(session_frame) < lookback + 1 or "vwap" not in session_frame.columns:
            return 0.0
        recent = session_frame.tail(lookback + 1).iloc[:-1]  # bars before the current one
        if recent.empty:
            return 0.0
        buffer = self._pct_param("vwap_reclaim_buffer_pct", 0.0005, vol_scale) * close
        recent_close = recent["close"].astype(float)
        recent_vwap = recent["vwap"].astype(float)
        if side == Side.LONG:
            dipped = bool((recent_close < recent_vwap).any())
            reclaimed = close > vwap + buffer
        else:
            dipped = bool((recent_close > recent_vwap).any())
            reclaimed = close < vwap - buffer
        if not (dipped and reclaimed):
            return 0.0
        score = 0.5 + 2.0
        cur_vol = _safe_float(session_frame.iloc[-1].get("volume"), 0.0)
        vol_mean = _safe_float(recent["volume"].mean(), 1.0)
        if vol_mean > 0 and cur_vol / vol_mean >= float(self.params.get("vwap_reclaim_min_volume_ratio", 1.2)):
            score += 1.0
        if side == Side.LONG:
            if close > ema9:
                score += 0.7
            if ema9 >= ema20:
                score += 0.3
        else:
            if close < ema9:
                score += 0.7
            if ema9 <= ema20:
                score += 0.3
        upper_wick, lower_wick, _body, bar_range = _bar_wick_fractions(session_frame)
        if bar_range > 0:
            wick = lower_wick if side == Side.LONG else upper_wick
            if wick >= 0.25:
                score += 0.5
        return score

    def _score_sr_scalp(self, side: Side, close: float, atr: float,
                        frame: pd.DataFrame, sr_ctx, vol_scale: float = 1.0) -> float:
        """Score the HTF S/R scalp on ACTUAL level interaction (2026-05-29
        redesign).

        Thesis: enter LONG at/just off a support zone that is HOLDING (price
        above it, not broken through), OR on the CONTINUATION after a
        resistance zone has confirm-flipped to support and is holding (close
        back above the broken resistance) — then ride toward the next
        resistance up the ladder. SHORT mirrors: at/off a holding resistance,
        or continuation after a confirmed support break, riding down to the
        next support.

        The previous scorer measured chop character (wick / VWAP-neutral /
        EMA-neutral / low-ADX), which is UNCORRELATED with the level geometry
        the builder actually gates on — so the regime effectively never
        qualified (max score 2.5 vs the 3.0 threshold across the entire
        2026-05-29 RTH session). This scorer measures the SAME geometry the
        builder enforces, so a genuine support-hold / flip-continuation
        scores high enough to win the regime auction; the hard zone-gap +
        proximity + not-broken-through gates still run at build time.

        Components (max 5.0):
          * +0.5  base (regime in play)
          * +2.0  price is at/just off the HOLDING entry-side zone — the
                  nearest support (LONG) / resistance (SHORT), OR a confirmed
                  flip level (LONG: close just above broken_resistance ;
                  SHORT: close just below broken_support). No level
                  interaction => base only (won't qualify).
          * +0.5  continuation bonus when the proximity is a confirmed flip
                  rather than a fresh nearest-level zone
          * +1.0  bounce/rejection bar character (LONG: lower wick >= 0.30 ;
                  SHORT: upper wick >= 0.30)
          * +1.0  room to ride: inner gap to the opposite nearest zone clears
                  the build's required distance (a ladder exists to ride to)
        """
        if sr_ctx is None or atr <= 0 or close <= 0:
            return 0.0
        sup = getattr(sr_ctx, "nearest_support", None)
        res = getattr(sr_ctx, "nearest_resistance", None)
        bres = getattr(sr_ctx, "broken_resistance", None)
        bsup = getattr(sr_ctx, "broken_support", None)
        sup_px = float(getattr(sup, "price", 0.0) or 0.0) if sup is not None else 0.0
        res_px = float(getattr(res, "price", 0.0) or 0.0) if res is not None else 0.0
        bres_px = float(getattr(bres, "price", 0.0) or 0.0) if bres is not None else 0.0
        bsup_px = float(getattr(bsup, "price", 0.0) or 0.0) if bsup is not None else 0.0

        zone_hw = max(
            float(self.params.get("zone_atr_mult", 0.20)) * atr,
            close * float(self.params.get("zone_pct", 0.0015)),
            0.01,
        )
        prox = float(self.params.get("sr_scalp_max_distance_from_zone_atr", 0.5)) * atr

        def _near_support(level_px: float) -> bool:
            return level_px > 0.0 and (level_px - zone_hw) < close <= (level_px + zone_hw + prox)

        def _near_resistance(level_px: float) -> bool:
            return level_px > 0.0 and (level_px - zone_hw - prox) <= close < (level_px + zone_hw)

        score = 0.5
        proximity_hit = False
        flip_hit = False
        if side == Side.LONG:
            if _near_support(sup_px):
                proximity_hit = True
            if 0.0 < bres_px < close and _near_support(bres_px):
                flip_hit = True
        else:
            if _near_resistance(res_px):
                proximity_hit = True
            if bsup_px > 0.0 and close < bsup_px and _near_resistance(bsup_px):
                flip_hit = True

        if not (proximity_hit or flip_hit):
            return score
        score += 2.0
        if flip_hit and not proximity_hit:
            score += 0.5

        upper_wick, lower_wick, _body, bar_range = _bar_wick_fractions(frame)
        if bar_range > 0:
            wick = lower_wick if side == Side.LONG else upper_wick
            if wick >= 0.30:
                score += 1.0

        if 0.0 < sup_px < res_px:
            inner_gap = (res_px - zone_hw) - (sup_px + zone_hw)
            required_gap = max(
                self._pct_param("sr_scalp_min_distance_pct", 0.008, vol_scale) * close,
                float(self.params.get("sr_scalp_min_distance_atr", 2.5)) * atr,
            )
            if inner_gap >= required_gap:
                score += 1.0
        return score

    # ------------------------------------------------------------------
    # Live directional bias
    # ------------------------------------------------------------------
    def _compute_live_bias_and_day_strength(
        self, frame: pd.DataFrame, close: float,
    ) -> tuple[Side | None, float | None]:
        """Single-source computation: returns ``(bias, day_strength_pct)``.

        ``day_strength_pct = (close − session_open) / session_open * 100``.
        Bias is ``Side.LONG`` when day_strength exceeds
        ``+directional_bias_min_day_strength`` (default 0.20%), ``Side.SHORT``
        below the negative threshold, ``None`` within the neutral band.

        Both values share one ``_session_open_price`` call so the soft-bias
        penalty path doesn't repeat the session-open lookup. Used by
        ``entry_signals`` (needs both bias for side selection AND magnitude
        for penalty scaling) and by ``_compute_live_directional_bias`` (the
        single-return wrapper kept for test compatibility).

        Returns ``(None, None)`` when the session_open is unavailable
        (warmup frame, missing data, etc.).
        """
        session_open = self._day_strength_session_open(frame)
        if session_open is None or session_open <= 0:
            return None, None
        try:
            day_strength = (float(close) - session_open) / session_open * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            return None, None
        threshold = float(self.params.get("directional_bias_min_day_strength", 0.20))
        if day_strength > threshold:
            return Side.LONG, day_strength
        if day_strength < -threshold:
            return Side.SHORT, day_strength
        return None, day_strength

    def _compute_live_directional_bias(self, frame: pd.DataFrame, close: float) -> Side | None:
        """Bias-only wrapper around ``_compute_live_bias_and_day_strength``.
        Kept as the public API for tests + any caller that doesn't need
        the day_strength magnitude. Same semantics as before: returns
        ``Side.LONG`` / ``Side.SHORT`` / ``None`` based on day_strength
        vs. ``directional_bias_min_day_strength``."""
        bias, _ = self._compute_live_bias_and_day_strength(frame, close)
        return bias

    def _volatility_widening_factor(self, tech_ctx: Any, current_time: Any = None) -> float:
        """Stop-buffer widening multiplier — combines TWO orthogonal triggers:

        1. **ATR expansion (Tier 2a, existing)** — when the current bar's
           ATR is large vs its 5-bar average (``tech_ctx.atr_expansion_mult
           > atr_widening_threshold``), stops scale up linearly to
           ``atr_widening_max_factor``. Catches trend-day RELATIVE
           volatility surges.

        2. **Early-session time-of-day widening (new)** — applies an
           ABSOLUTE multiplier when ``current_time`` is before
           ``early_session_stop_widening_until``. Catches the post-open
           high-vol window where ATR may not be "expanding" relative to
           recent bars (which are all noisy) but absolute vol is high.
           Without this, a trade taken at 10:10 on AMD got stopped by a
           single 1m wick that recovered 5 min later — the stop was set
           by Tier 2a's RELATIVE-expansion logic, which read normal at
           the time even though absolute volatility was elevated.

        The factor returned is ``max(expansion_factor, time_factor)``,
        capped at ``atr_widening_max_factor``. Tier 2a + time-of-day
        don't compound (multiplying both would over-widen on volatile
        morning opens); the trade gets whichever cushion is larger.

        Returns 1.0 when:
          * ``atr_aware_stop_enabled`` is False (master toggle)
          * No expansion AND not in early session
          * ``tech_ctx`` is missing or has no ``atr_expansion_mult`` AND
            ``current_time`` is unknown
        """
        if not bool(self.params.get("atr_aware_stop_enabled", True)):
            return 1.0
        max_factor = float(self.params.get("atr_widening_max_factor", 1.5))

        # --- Trigger 1: ATR expansion (Tier 2a) ---
        expansion_factor = 1.0
        expansion_mult = float(getattr(tech_ctx, "atr_expansion_mult", 1.0) or 1.0) if tech_ctx is not None else 1.0
        threshold = float(self.params.get("atr_widening_threshold", 1.3))
        if expansion_mult > threshold:
            # Linear scale from 1.0 (at threshold) to max_factor (at 2x threshold)
            scale_range = max(0.1, threshold)
            progress = min(1.0, (expansion_mult - threshold) / scale_range)
            expansion_factor = 1.0 + (max_factor - 1.0) * progress

        # --- Trigger 2: Early-session time-of-day widening ---
        time_factor = 1.0
        if current_time is not None and bool(self.params.get("early_session_stop_widening_enabled", True)):
            try:
                cutoff = parse_hhmm(str(self.params.get("early_session_stop_widening_until", "10:30")))
                if current_time <= cutoff:
                    time_factor = float(self.params.get("early_session_stop_widening_mult", 1.3))
            except Exception:
                pass

        # Take the LARGER widening (not compound) so morning + ATR
        # expansion don't double-multiply into an unrealistic stop.
        return min(max(expansion_factor, time_factor), max_factor)

    def _bias_penalty(self, side: Side, day_strength: float | None,
                      effective_bias: Side | None, respect_bias: bool) -> float:
        """Soft-bias score adjustment applied to a side's regime scores
        when the side disagrees with ``effective_bias``.

        Returns 0.0 when:
          * ``respect_bias`` is False (master toggle off / ORB bypass active)
          * ``effective_bias`` is None (no bias set)
          * ``side`` agrees with ``effective_bias``

        Otherwise returns ``bias_penalty_base * magnitude_factor`` where
        ``magnitude_factor = min(1.0, |day_strength| / bias_penalty_saturate_at)``.
        Default base 1.0, saturate at 2.0% day_strength — so a −0.5% day with
        SHORT bias applies only a 0.25 penalty to LONG-side regimes (a strong
        structural LONG setup can still qualify), while a −3% deep-down day
        applies the full 1.0 penalty (filters most LONG-side setups).

        Replaces Fix A's previous HARD lockout (``preferred_sides = [Side]``).
        Both sides are now always evaluated; the penalty filters weak
        counter-bias setups while letting strong structural ones through.
        Preserves the 2026-04-20 protection (deep day_strength → full
        penalty) and the trailing-bias memory (inferred bias still
        contributes to the penalty).
        """
        if not respect_bias:
            return 0.0
        if effective_bias is None or effective_bias == side:
            return 0.0
        penalty_base = float(self.params.get("bias_penalty_base", 1.0))
        saturate_at = max(0.1, float(self.params.get("bias_penalty_saturate_at", 2.0)))
        ds = float(day_strength) if day_strength is not None else 0.0
        magnitude_factor = min(1.0, abs(ds) / saturate_at)
        return penalty_base * magnitude_factor

    # ------------------------------------------------------------------
    # Time-of-day gating
    # ------------------------------------------------------------------
    @staticmethod
    def _time_in_range(now_t, start: str, end: str) -> bool:
        return parse_hhmm(start) <= now_t <= parse_hhmm(end)

    def _orb_range_end(self) -> str:
        """HH:MM at which today's opening range finishes forming."""
        total = 9 * 60 + 30 + max(1, int(self.params.get("orb_range_minutes", 15)))
        return f"{total // 60:02d}:{total % 60:02d}"

    def _in_orb_window(self, now_t) -> bool:
        """Is *now_t* inside the ORB window — the span where the ORB regime
        is the only regime allowed?

        The window is BOUNDED AT BOTH ENDS: ``[opening-range end, orb_end]``,
        and it does not exist at all when ``disable_orb_regime`` is set.

        This drives every ``orb_bypass_*`` flag (HTF bias, structure, S/R,
        exhaustion, side decision, relative strength, screener bias) — seven
        gates that are relaxed on the argument that the opening range-break
        is its own directional proof. An open-ended "before orb_end" reading
        hands those bypasses to entries that have no ORB thesis behind them:
        with ``equity_session_indicator_window: extended`` the whole
        pre-market session qualifies, and with ``disable_orb_regime: true``
        (where there IS no ORB regime) so does every entry from the open
        through orb_end.
        """
        if bool(self.params.get("disable_orb_regime", False)):
            return False
        return self._time_in_range(now_t, self._orb_range_end(), str(self.params.get("orb_end_time", "10:05")))

    def _allowed_regimes(self, now_t) -> set[str]:
        """Return which regimes are allowed at the current time.

        Window cutoffs are param-driven (orb_end_time, midday_start_time,
        midday_end_time, afternoon_start_time, no_new_entries_after) — no
        hard-coded times.

        Seven regimes:
          - orb: true Opening Range Breakout. The opening range forms over
            the first ``orb_range_minutes`` of RTH (09:30 →); the ORB regime
            is the ONLY regime allowed in the window from range-end →
            orb_end_time, and it trades a break of that range (stop = opposite
            range edge, target = measured move). 09:30 → range-end is a
            no-entry zone (the range is still forming).
          - trend / pullback / range: primary scoring regimes
          - vol_squeeze: Bollinger-squeeze breakout. Allowed in the primary
            window (orb_end → midday_start), midday (2026-09-21 — the
            lunchtime tape IS the compression its thesis is about) and the
            afternoon (afternoon_start → no_new).
          - momentum: momentum-from-open continuation. Allowed post-ORB
            through close (orb_end → no_new). Includes midday because the
            ``momentum_min_day_strength`` hard gate filters out chop —
            stocks without enough intraday move score zero. Renamed from
            ``momentum_close`` 2026-05-12 when the window was widened from
            afternoon-only.
          - sr_scalp: HTF S/R mean-reversion scalp. Allowed post-ORB
            through close (orb_end → no_new). Excluded from the ORB
            window because morning chop near recent levels often breaks
            through; the build-time distance gate
            (``sr_scalp_min_distance_pct`` / ``sr_scalp_min_distance_atr``)
            rejects when the HTF zones are too close to be worth the
            round-trip.

        Each regime has its own opt-out knob via params:
          disable_trend_regime / disable_pullback_regime /
          disable_range_regime / disable_vol_squeeze_regime /
          disable_momentum_regime / disable_sr_scalp_regime.

        The opening window (09:30 → orb_end_time) has a separate whole-window
        opt-out (``disable_orb_window``) that skips the ORB window entirely
        — different from ``orb_bypass_*`` flags (which loosen filters
        within the ORB window). Use this when the opening 30 minutes
        are too whippy and you'd rather start trading at ``orb_end_time``.

        Score thresholds (min_*_score) gate each regime independently and
        the score-gap auction picks the winner.
        """
        orb_end = self.params.get("orb_end_time", "10:05")
        midday_start = self.params.get("midday_start_time", "11:30")
        midday_end = self.params.get("midday_end_time", "13:00")
        afternoon_start = self.params.get("afternoon_start_time", "13:00")
        no_new = self.params.get("no_new_entries_after", "15:00")
        orb_window_enabled = not bool(self.params.get("disable_orb_window", False))
        trend_enabled = not bool(self.params.get("disable_trend_regime", False))
        pullback_enabled = not bool(self.params.get("disable_pullback_regime", False))
        range_enabled = not bool(self.params.get("disable_range_regime", False))
        vol_squeeze_enabled = not bool(self.params.get("disable_vol_squeeze_regime", False))
        momentum_enabled = not bool(self.params.get("disable_momentum_regime", False))
        sr_scalp_enabled = not bool(self.params.get("disable_sr_scalp_regime", False))
        # A momentum-family regime — available wherever momentum is, stripped
        # otherwise.
        #
        # This was `enable_vwap_reclaim_regime` (opt-IN, default off) when it
        # shipped, so adding it to the shared engine could not silently switch
        # it on for `SmallCapSqueezeStrategy`, which subclasses this class.
        # That protection is spent: small_cap_squeeze now opts in explicitly in
        # its own manifest, so nothing depended on the inverted default while
        # the odd polarity made it the one regime flag that reads backwards.
        vwap_reclaim_enabled = not bool(self.params.get("disable_vwap_reclaim_regime", False))

        def _filter(regimes: set[str]) -> set[str]:
            """Strip regimes whose disable knob is set."""
            if not trend_enabled:
                regimes.discard("trend")
            if not pullback_enabled:
                regimes.discard("pullback")
            if not range_enabled:
                regimes.discard("range")
            if not vol_squeeze_enabled:
                regimes.discard("vol_squeeze")
            if not momentum_enabled:
                regimes.discard("momentum")
            if not sr_scalp_enabled:
                regimes.discard("sr_scalp")
            if not vwap_reclaim_enabled:
                regimes.discard("vwap_reclaim")
            return regimes

        if now_t > parse_hhmm(no_new):
            return set()
        # ORB regime + its opening-range carve-out. ``disable_orb_regime``
        # (off by default) removes BOTH: no 09:30→range-end no-entry zone and
        # no ORB-only window — the open just trades the normal regime mix
        # continuously (the primary window starts at 09:30 instead of orb_end).
        # Used by momentum/squeeze strategies that want to trade the open
        # directly. Distinct from ``disable_orb_window`` (which keeps the
        # carve-out but skips entries until orb_end_time).
        orb_disabled = bool(self.params.get("disable_orb_regime", False))
        orb_range_min = max(1, int(self.params.get("orb_range_minutes", 15)))
        orb_range_end_total = 9 * 60 + 30 + orb_range_min
        orb_range_end = f"{orb_range_end_total // 60:02d}:{orb_range_end_total % 60:02d}"
        if not orb_disabled:
            # Opening range forms over the first ``orb_range_minutes`` of RTH
            # (09:30 →). NO entries while it forms — the true-ORB thesis waits
            # for the range, it does not trade the opening chaos. (In extended
            # mode pre-market 07:00-09:30 still trades via the fallthrough
            # below; 09:30→range-end is reserved for range formation.)
            if self._time_in_range(now_t, "09:30", orb_range_end):
                return set()
            if self._time_in_range(now_t, orb_range_end, orb_end):
                # ORB window: opening-range breakout only. Whole-window opt-out
                # via disable_orb_window (skip the open, start at orb_end_time).
                if not orb_window_enabled:
                    return set()
                return _filter({"orb"})
        # Primary window. With ORB enabled it starts at orb_end; with the ORB
        # regime disabled it starts at 09:30 so the open trades the normal mix.
        primary_start = "09:30" if orb_disabled else orb_end
        if self._time_in_range(now_t, primary_start, midday_start):
            # Full regime mix including momentum + sr_scalp. The day_strength
            # gate filters momentum; the distance gate filters sr_scalp. Neither
            # blocks the trend / pullback / range / vol_squeeze regimes.
            return _filter({"trend", "pullback", "range", "vol_squeeze", "momentum", "sr_scalp", "vwap_reclaim"})
        if self._time_in_range(now_t, midday_start, midday_end):
            # Midday: pullbacks remain the default fit for top-tier chop,
            # but momentum is allowed because day_strength >= threshold
            # implies a stock is genuinely trending despite the lunchtime
            # tape. sr_scalp is also allowed — midday's low-volatility
            # chop is often the cleanest scalp environment between HTF
            # zones (when the gap qualifies).
            #
            # vol_squeeze added 2026-09-21. Its thesis is compression
            # resolving into expansion, and the lunchtime tape IS the
            # compression — it was excluded from the one window where its
            # setup is most common. The measurement that prompted it: on
            # 2026-09-21, 51% of midday skips across the session's five
            # biggest movers (INTC/META/AMD/QCOM/NFLX, all +3% to +6%) were
            # "no regime qualified", and of the three regimes offered, NONE
            # came within half a point of its floor — pullback peaked at
            # 3.00 against 3.5, momentum at 3.00 against 4.0, vwap_reclaim
            # at 0.00. Ninety minutes a day in which nothing could fire.
            return _filter({"pullback", "momentum", "sr_scalp", "vwap_reclaim", "vol_squeeze"})
        if self._time_in_range(now_t, afternoon_start, no_new):
            # Range regime is included in afternoon by default because
            # afternoon tapes are often range-bound and forcing trend/pullback
            # entries there produces late-in-move longs. Range regime handles
            # mean-reversion at the extremes. Disable via
            # ``afternoon_include_range: false`` in params (or globally via
            # ``disable_range_regime: true``).
            if bool(self.params.get("afternoon_include_range", True)):
                regimes = {"trend", "pullback", "range", "vol_squeeze", "momentum", "sr_scalp", "vwap_reclaim"}
            else:
                regimes = {"trend", "pullback", "vol_squeeze", "momentum", "sr_scalp", "vwap_reclaim"}
            return _filter(regimes)
        if get_session_indicator_window() == "extended" and now_t <= parse_hhmm(no_new):
            # Extended-hours opt-in (07:00-20:00 trading): times not matched by
            # the RTH windows above — pre-market (<09:30) and any RTH gap — get
            # the full non-ORB regime mix. ORB stays RTH-anchored (range-end →
            # orb_end above). After-RTH (>16:00) is already covered by the afternoon
            # branch when no_new_entries_after is pushed past the close. The
            # per-regime score gates + the extended-hours universe gate in
            # entry_signals self-filter; this just opens the time window.
            return _filter({"trend", "pullback", "range", "vol_squeeze", "momentum", "sr_scalp", "vwap_reclaim"})
        return set()

    @staticmethod
    def _day_strength_session_open(frame: pd.DataFrame) -> float | None:
        """Session-open anchor for the live ``day_strength`` bias. In
        extended-hours indicator mode the VWAP/EMA session reset and this
        anchor both key off the 07:00 equity-stream open; otherwise the RTH
        09:30 open (original behavior)."""
        if get_session_indicator_window() == "extended":
            return _session_open_price(frame, session_start=EQUITY_STREAM_START)
        return _session_open_price(frame)

    def _extended_hours_tradable_set(self) -> set[str]:
        """Symbols eligible for pre/post-market entries (extended-indicator
        mode only). Empty => no extended-hours entries (RTH only)."""
        raw = self.params.get("extended_hours_tradable", []) or []
        return {str(s).upper().strip() for s in raw if str(s).strip()}

    # ------------------------------------------------------------------
    # Signal building per regime
    # ------------------------------------------------------------------
    def _prune_armed_retests(self) -> None:
        """Drop arms from a previous session, and any that outlived their
        window without being consumed. Called once per ``entry_signals`` so a
        symbol that stops appearing in the watchlist cannot leak an entry."""
        if not self._armed_retests:
            return
        now = now_et()
        today = now.date()
        max_minutes = float(self.params.get("armed_retest_max_minutes", 12.0))
        # Twice the window. Expiry itself is handled in the verdict, where it
        # produces the market entry; this reaps arms nothing came back for.
        #
        # The bound matters. An arm past its window is a licence to enter at
        # market on the next qualifying cycle, and the regime can go a long
        # time without qualifying -- index confirmation lapses, the score
        # dips. Keeping arms for 40+ minutes meant a fallback entry could be
        # justified by a breakout that happened most of an hour earlier, at a
        # level the tape had moved away from. Past 2x, the arm is dropped and
        # the next qualifying cycle arms again, which waits rather than
        # entering on stale evidence.
        stale_after = max_minutes * 2.0
        for key, arm in list(self._armed_retests.items()):
            if arm.get("session_date") != today:
                del self._armed_retests[key]
                continue
            armed_at = arm.get("armed_at")
            if armed_at is None:
                del self._armed_retests[key]
                continue
            if (now - armed_at).total_seconds() / 60.0 >= stale_after:
                del self._armed_retests[key]

    def _drop_armed_retests(self, symbol: str) -> None:
        """Forget every arm on *symbol*, both sides and both regimes."""
        if not self._armed_retests:
            return
        prefix = f"{symbol}|"
        for key in [k for k in self._armed_retests if k.startswith(prefix)]:
            del self._armed_retests[key]

    def _expired_armed_retests(
        self, symbol: str, allowed_regimes: set[str],
    ) -> list[tuple[Side, str, dict[str, Any]]]:
        """Arms whose wait ran out, popped and returned for immediate entry.

        This is the market fallback, and it runs BEFORE the build queue and
        outside the per-cycle gates on purpose.

        Those gates -- the ``_decide_side`` vote, index confirmation, the
        confirmation bar -- were ALL satisfied at arm time; that is the only
        way an arm gets created. Re-imposing them at expiry means an arm can
        only take its fallback on a cycle where the setup happens to fully
        re-qualify, and if it does not, the trade is silently dropped.

        That is not hypothetical. INTC, 2026-09-21: armed 09:58 at 118.39 on a
        setup that passed every gate, index confirmation lapsed at 10:03, and
        the stock ran to 124.64 without a single entry. The retest never came
        (``touched=0`` throughout), so the fallback was the whole point, and it
        never fired once -- the expiry check sat behind the gate that had
        failed.

        Index confirmation is an ENTRY gate: once in a position it no longer
        applies. Arming holds the trade OUT across exactly the window where a
        lapse can lock it out, so the setup is re-validated against conditions
        it had already cleared. The fallback has to be judged on the arm-time
        decision or it is not a fallback.

        What still applies: the regime must still be offered in this window
        (a trend arm does not fire at midday), and the builder's own checks run
        unchanged -- ``breakout_confirmed`` is False on this path, so a faded
        setup fails ``no_fresh_breakout``, and ``_finalize_signal`` still
        applies the stretched / SR / structure rejections. A runaway that has
        gone too far to chase is still declined, by the gate that exists for
        that.
        """
        if not self._armed_retests:
            return []
        now = now_et()
        max_minutes = float(self.params.get("armed_retest_max_minutes", 12.0))
        prefix = f"{symbol}|"
        out: list[tuple[Side, str, dict[str, Any]]] = []
        for key in [k for k in self._armed_retests if k.startswith(prefix)]:
            arm = self._armed_retests[key]
            armed_at = arm.get("armed_at")
            if armed_at is None or arm.get("session_date") != now.date():
                del self._armed_retests[key]
                continue
            if (now - armed_at).total_seconds() / 60.0 < max_minutes:
                continue
            try:
                _sym, side_token, regime = key.split("|", 2)
            except ValueError:
                del self._armed_retests[key]
                continue
            del self._armed_retests[key]
            # The time window is a real constraint, not a gate the arm can
            # carry past: a trend arm must not fire during midday, when the
            # regime is not offered at all.
            if regime not in allowed_regimes:
                continue
            side = Side.LONG if str(side_token).upper() == "LONG" else Side.SHORT
            arm["waited_minutes"] = (now - armed_at).total_seconds() / 60.0
            out.append((side, regime, arm))
        return out

    def _armed_retest_verdict(
        self, symbol: str, side: Side, regime: str, close: float, atr: float,
        ltf: pd.DataFrame, frame: pd.DataFrame,
        *, regime_score: float = 0.0, regime_norm: float = 0.0,
    ) -> dict[str, Any]:
        """Arm on qualification; enter on the retest, or at market on expiry.

        The problem: ``trend`` and ``momentum`` fill at an N-bar extreme by
        construction, so the entry is the top of the move so far and the stop
        sits inside the retrace that normally follows. Measured on the archived
        sessions, a trend fill was followed by a retrace covering 85% of the way
        to its stop, against 21% from an arbitrary moment in the same session.

        So qualification no longer means "enter". It means "remember the level
        that was cleared and wait for price to come back to it". Four outcomes:

        ``none``   feature off, or no usable trigger level -- behave as before.
        ``wait``   armed, retest not yet confirmed. The cycle skips; other
                   regimes in the build queue are unaffected, so arming trend
                   does not stop a pullback firing on the same symbol.
        ``enter``  price returned to within ``armed_retest_zone_atr`` of the
                   level and closed back through it on a bar with the right
                   shape. This is the entry the regime was waiting for.

        EXPIRY IS NOT HANDLED HERE. It lives in ``_expired_armed_retests``,
        which runs before the build queue, because this method is only reached
        once a setup has re-cleared the side / index / confirmation-bar gates
        -- and an arm whose index confirmation lapsed while it waited would
        then never reach its own expiry. See that method for the INTC case
        that proved it.

        The stop rules are NOT changed on a retest entry. The gain is that the
        fill sits at a level price has already tested and held instead of at a
        fresh extreme; re-deriving the stop from a retest low would mean
        bypassing ``default_stop_pct`` / ``min_stop_atr_mult``, which are risk
        floors and a separate decision. The retest low is stamped in metadata
        so the question can be answered from data later.
        """
        out: dict[str, Any] = {"status": "none", "reason": None, "metadata": {}}
        if regime not in ARMED_RETEST_REGIMES:
            return out
        if not bool(self.params.get("armed_retest_enabled", True)):
            return out
        if not math.isfinite(close) or close <= 0 or not math.isfinite(atr) or atr <= 0:
            return out
        _recent, trigger_level = self._breakout_reference(regime, side, ltf, frame)
        if trigger_level is None or not math.isfinite(trigger_level) or trigger_level <= 0:
            return out

        now = now_et()
        key = f"{symbol}|{side.value}|{regime}"
        arm = self._armed_retests.get(key)
        zone_atr = max(0.0, float(self.params.get("armed_retest_zone_atr", 0.35)))
        invalidation_atr = max(0.0, float(self.params.get("armed_retest_invalidation_atr", 0.75)))
        max_minutes = float(self.params.get("armed_retest_max_minutes", 12.0))

        # A close well back through the level means the breakout failed; the
        # arm is dead. No rejection is raised here -- the builder's own
        # fresh-breakout check owns that message, and duplicating it would put
        # two different reasons on the same condition.
        #
        # Measured against the ARMED level, not the current one. The N-bar
        # reference walks up as new highs print, so testing against it would
        # move the invalidation line away from price on exactly the setups
        # that are still working, and drag it along behind a rolling-over one.
        # The level we are waiting for is the level we armed on.
        reference = float(arm["trigger_level"]) if arm is not None else float(trigger_level)
        if side == Side.LONG:
            invalidated = close < reference - invalidation_atr * atr
        else:
            invalidated = close > reference + invalidation_atr * atr
        if invalidated:
            self._armed_retests.pop(key, None)
            return out

        if arm is None or arm.get("session_date") != now.date():
            self._armed_retests[key] = {
                "armed_at": now,
                "session_date": now.date(),
                "trigger_level": float(trigger_level),
                # Carried so the expiry sweep can build the signal from the
                # score the setup had WHEN IT WAS VALIDATED. Re-scoring at
                # expiry would ask a different question -- the trade was
                # justified at arm time, the wait was only about price.
                "regime_score": float(regime_score),
                "regime_score_norm": float(regime_norm),
            }
            out.update({
                "status": "wait",
                "reason": (f"armed_awaiting_retest(level={trigger_level:.4f},"
                           f"close={close:.4f},wait={max_minutes:.0f}m)"),
            })
            return out

        armed_at = arm["armed_at"]
        level = float(arm["trigger_level"])
        waited_minutes = (now - armed_at).total_seconds() / 60.0

        # Did price come back to the level while we waited? Measured on the
        # base 1m frame regardless of the regime's own timeframe -- the finest
        # resolution gives the truest extreme for the window.
        touched = False
        extreme: float | None = None
        if frame is not None and not frame.empty:
            since = frame[frame.index > armed_at]
            if not since.empty:
                extreme = (float(since["low"].min()) if side == Side.LONG
                           else float(since["high"].max()))
        if extreme is not None and math.isfinite(extreme):
            if side == Side.LONG:
                touched = extreme <= level + zone_atr * atr
            else:
                touched = extreme >= level - zone_atr * atr

        reclaimed = close > level if side == Side.LONG else close < level
        min_close_pos = min(0.95, max(0.05, float(
            self.params.get("armed_retest_min_close_position", 0.60))))
        close_pos = _bar_close_position(frame)
        bar_ok = (close_pos >= min_close_pos if side == Side.LONG
                  else close_pos <= (1.0 - min_close_pos))

        base_meta = {
            "armed_retest_regime": regime,
            "armed_retest_level": round(level, 4),
            "armed_retest_waited_minutes": round(waited_minutes, 2),
            "armed_retest_extreme": (round(extreme, 4) if extreme is not None
                                     and math.isfinite(extreme) else None),
        }

        if touched and reclaimed and bar_ok:
            self._armed_retests.pop(key, None)
            out.update({
                "status": "enter",
                "metadata": {**base_meta, "armed_retest_status": "retest_confirmed"},
            })
            return out

        out.update({
            "status": "wait",
            "reason": (f"armed_awaiting_retest(level={level:.4f},"
                       f"waited={waited_minutes:.1f}m/{max_minutes:.0f}m,"
                       f"touched={int(bool(touched))},reclaimed={int(bool(reclaimed))})"),
        })
        return out

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
        session = source[_same_day_mask(source, now_et().date())]
        recent = session.tail(lookback + 1).iloc[:-1] if len(session) > lookback else session.iloc[:-1]
        if recent.empty:
            return None, None
        if side == Side.LONG:
            level = _safe_float(recent["high"].max(), float("nan"))
        else:
            level = _safe_float(recent["low"].min(), float("nan"))
        if level is None or not math.isfinite(float(level)):
            return recent, None
        return recent, float(level)

    def _build_trend_signal(self, c: Candidate, side: Side, close: float, atr: float,
                            ltf: pd.DataFrame, frame: pd.DataFrame, regime_score: float,
                            data=None, vol_widening: float = 1.0, vol_scale: float = 1.0,
                            breakout_confirmed: bool = False) -> Signal | None:
        """``breakout_confirmed`` is set only by a CONFIRMED armed retest.

        The fresh-breakout gate below asks "is close above the last N bars'
        high". On a retest cycle that window still holds the breakout bar,
        whose high is above the reclaim close -- which is what a retest IS --
        so the gate would reject every armed entry and leave the feature able
        to produce nothing but expiry/market fills. The arm already recorded
        the breakout, and ``_armed_retest_verdict`` only returns ``enter`` with
        close back above that level, so the condition is satisfied by
        construction here rather than skipped.
        """
        recent, trigger_level = self._breakout_reference("trend", side, ltf, frame)
        if recent is None or recent.empty:
            self._set_build_failure(c.symbol, "trend", "insufficient_ltf_history")
            return None
        # ATR buffer + default_stop_pct floor both scale with vol_widening
        # (Tier 2a) — trend-day capture: wider noise tolerance, same dollar
        # risk per trade (risk manager downsizes share count).
        buffer = atr * float(self.params.get("stop_buffer_atr_mult", 0.25)) * vol_widening * self._side_stop_buffer_mult(side)
        effective_default_stop_pct = self.config.risk.default_stop_pct * vol_widening * vol_scale
        target_rr = float(self.params.get("trend_target_rr", 2.0)) * self._side_target_rr_mult(side)

        if side == Side.LONG:
            trigger_high = trigger_level if trigger_level is not None else close
            if not breakout_confirmed and close <= trigger_high:
                self._set_build_failure(
                    c.symbol, "trend",
                    f"no_fresh_breakout(close={close:.4f}<=recent_high={trigger_high:.4f})",
                )
                return None
            stop = _safe_float(recent["low"].min(), close) - buffer
            stop = min(stop, close * (1.0 - effective_default_stop_pct))
            risk = max(0.01, close - stop)
            target = close + risk * target_rr
        else:
            trigger_low = _safe_float(recent["low"].min(), close)
            if not breakout_confirmed and close >= trigger_low:
                self._set_build_failure(
                    c.symbol, "trend",
                    f"no_fresh_breakdown(close={close:.4f}>=recent_low={trigger_low:.4f})",
                )
                return None
            stop = _safe_float(recent["high"].max(), close) + buffer
            stop = max(stop, close * (1.0 + effective_default_stop_pct))
            risk = max(0.01, stop - close)
            target = max(0.01, close - risk * target_rr)

        return self._finalize_signal(c, side, close, stop, target, "trend", regime_score, frame, data, vol_scale=vol_scale)

    def _build_orb_signal(self, c: Candidate, side: Side, close: float, atr: float,
                          frame: pd.DataFrame, regime_score: float,
                          data=None, vol_widening: float = 1.0, vol_scale: float = 1.0) -> Signal | None:
        """Build a true Opening Range Breakout signal.

        Entry: a confirmed break of today's opening range (close above
        ``or_high`` + buffer for LONG; below ``or_low`` − buffer for SHORT).
        Stop: the OPPOSITE range edge (LONG: or_low − buffer; SHORT:
        or_high + buffer) — a failed breakout returns through the range.
        Target: a measured move — the range height projected from the broken
        edge (``orb_target_range_mult`` × range, default 1.5×). The shared
        ``_finalize_signal`` then caps the target to nearby HTF levels and
        enforces the min R:R floor. Range size is sanity-bounded so noise
        ranges (too tight) and untradeable ranges (too wide) are skipped."""
        or_high, or_low = self._opening_range(frame)
        if or_high is None or or_low is None or or_high <= or_low:
            self._set_build_failure(c.symbol, "orb", "orb_no_opening_range")
            return None
        range_height = or_high - or_low
        min_range = float(self.params.get("orb_min_range_atr_mult", 0.5)) * atr
        max_range_mult = float(self.params.get("orb_max_range_atr_mult", 4.0))
        if range_height < min_range:
            self._set_build_failure(
                c.symbol, "orb",
                f"orb_range_too_tight(height={range_height:.4f}<{min_range:.4f})",
            )
            return None
        if max_range_mult > 0 and range_height > max_range_mult * atr:
            self._set_build_failure(
                c.symbol, "orb",
                f"orb_range_too_wide(height={range_height:.4f}>{max_range_mult * atr:.4f})",
            )
            return None
        breakout_buffer = float(self.params.get("orb_breakout_buffer_atr_mult", 0.05)) * atr
        stop_buffer = atr * float(self.params.get("stop_buffer_atr_mult", 0.25)) * vol_widening * self._side_stop_buffer_mult(side)
        target_mult = float(self.params.get("orb_target_range_mult", 1.5))
        if side == Side.LONG:
            if close <= or_high + breakout_buffer:
                self._set_build_failure(
                    c.symbol, "orb",
                    f"orb_no_break_above(close={close:.4f}<=or_high={or_high:.4f}+buf={breakout_buffer:.4f})",
                )
                return None
            stop = or_low - stop_buffer
            target = or_high + range_height * target_mult
        else:
            if close >= or_low - breakout_buffer:
                self._set_build_failure(
                    c.symbol, "orb",
                    f"orb_no_break_below(close={close:.4f}>=or_low={or_low:.4f}-buf={breakout_buffer:.4f})",
                )
                return None
            stop = or_high + stop_buffer
            target = or_low - range_height * target_mult

        # The measured move is anchored to the RANGE EDGE, not to the entry, so
        # a break that has already run past `edge + range_height * mult` yields
        # a target on the WRONG SIDE of the close — a LONG whose take-profit
        # sits below its entry. Observed across randomised opening ranges: 57
        # of 514 builder invocations produced an inverted target, e.g. close
        # 107.26 with a LONG target of 101.01.
        #
        # The gatekeeper's `_entry_levels_valid` would refuse such a signal, so
        # nothing traded, but a builder should not emit a structurally invalid
        # setup for a downstream guard to catch. When the measured move is
        # already exhausted the ORB thesis is simply spent — reject, the same
        # way sr_scalp rejects when its zone gap cannot pay for its stop.
        if not self._target_meets_min_rr(side, close, stop, target):
            risk = abs(close - stop)
            reward = (target - close) if side == Side.LONG else (close - target)
            self._set_build_failure(
                c.symbol, "orb",
                f"{'long' if side == Side.LONG else 'short'}_orb_measured_move_exhausted("
                f"close={close:.4f},target={target:.4f},reward={reward:.4f},risk={risk:.4f})",
            )
            return None

        return self._finalize_signal(c, side, close, stop, target, "orb", regime_score, frame, data, vol_scale=vol_scale)

    @staticmethod
    def _pullback_leg_context(
        side: Side, session_ltf: pd.DataFrame, current_close: float, ltf_minutes: int,
    ) -> tuple[float | None, float | None]:
        """Return ``(minutes_since_extreme, retrace_pct)`` for the pullback
        maturity check in ``_build_pullback_signal``.

        For LONG: extreme = session high, anchor = lowest low at-or-before
        the high bar, leg_size = high - anchor, retrace_pct = how far
        ``current_close`` has dropped from the high as a percentage of the
        leg.  Mirror for SHORT (extreme = session low, anchor = highest
        high at-or-before the low bar, retrace_pct = how far close has
        risen back).

        Returns ``(None, None)`` when context can't be computed reliably
        (empty session, single-bar session, or non-positive leg_size).
        ``minutes_since_extreme`` is estimated as ``bars_since_extreme *
        ltf_minutes`` (the LTF bar grid is uniform within the session)
        which avoids per-bar timestamp arithmetic.
        """
        if session_ltf is None or session_ltf.empty:
            return None, None
        n = len(session_ltf)
        if n < 2:
            return None, None
        try:
            if side == Side.LONG:
                high_values = session_ltf["high"].values
                extreme_pos = int(high_values.argmax())
                extreme_value = float(high_values[extreme_pos])
                anchor = float(session_ltf["low"].iloc[: extreme_pos + 1].min())
                leg_size = extreme_value - anchor
                retrace = extreme_value - current_close
            else:
                low_values = session_ltf["low"].values
                extreme_pos = int(low_values.argmin())
                extreme_value = float(low_values[extreme_pos])
                anchor = float(session_ltf["high"].iloc[: extreme_pos + 1].max())
                leg_size = anchor - extreme_value
                retrace = current_close - extreme_value
        except (KeyError, ValueError, TypeError):
            return None, None
        if leg_size <= 0.0:
            return None, None
        bars_since_extreme = max(0, n - 1 - extreme_pos)
        minutes_since_extreme = float(bars_since_extreme * max(1, int(ltf_minutes)))
        retrace_pct = max(0.0, (retrace / leg_size) * 100.0)
        return minutes_since_extreme, retrace_pct

    def _build_pullback_signal(self, c: Candidate, side: Side, close: float, atr: float,
                               ltf: pd.DataFrame, frame: pd.DataFrame, regime_score: float,
                               data=None, vol_widening: float = 1.0, vol_scale: float = 1.0) -> Signal | None:
        lookback = max(3, int(self.params.get("pullback_lookback_bars", 5)))
        # ltf is resampled from the full multi-day history frame, so tail(N)
        # crosses session boundary during early RTH. Scope swing/stop lookups
        # to today's session bars only.
        session_ltf = ltf[_same_day_mask(ltf, now_et().date())]
        recent = session_ltf.tail(lookback + 1).iloc[:-1] if len(session_ltf) > lookback else session_ltf.iloc[:-1]
        if recent.empty:
            self._set_build_failure(c.symbol, "pullback", "insufficient_ltf_history")
            return None

        # Pullback maturity check (2026-05-27). The pullback regime works
        # when the prior leg is YOUNG and the retracement shallow; it fails
        # when the trend is stale and price has given back most of the
        # move. Session 2026-05-27 NEM LONG entered at 14:48 — NEM's
        # session high was ~10:30 (4+ hours earlier) and price had
        # retraced ~89% off the peak (a multi-hour rollover dressed as a
        # pullback), lost $37. NVDA SHORT at 12:36 was the mirror — a
        # bounce in a multi-hour bleed (close_pos 0.90, top of bar), the
        # bot sold the relief rally back into the trend that was already
        # turning, lost $77. Reject pullback when BOTH age AND retracement
        # exceed their thresholds — either alone is fine (fresh-but-deep
        # pullbacks and old-but-shallow ones still trade).
        # Pullback leg context — computed once and consumed by BOTH the
        # stale-leg check (rejects too-old, too-deep retraces) and the
        # too-shallow check (rejects "pullbacks" that aren't really
        # pullbacks). _pullback_leg_context returns ``(None, None)`` when
        # no swing extreme exists yet (early session, insufficient bars);
        # both checks short-circuit in that case.
        ltf_min = max(1, int(self.params.get("ltf_minutes", 5)))
        minutes_since, retrace_pct = self._pullback_leg_context(side, session_ltf, close, ltf_min)

        if bool(self.params.get("pullback_require_fresh_leg", True)):
            max_minutes = float(self.params.get("pullback_max_minutes_since_session_extreme", 45.0))
            max_retrace_pct = float(self.params.get("pullback_max_leg_retrace_pct", 50.0))
            if (
                minutes_since is not None
                and retrace_pct is not None
                and minutes_since > max_minutes
                and retrace_pct > max_retrace_pct
            ):
                side_prefix = "long" if side == Side.LONG else "short"
                self._set_build_failure(
                    c.symbol, "pullback",
                    f"{side_prefix}_pullback_stale_leg("
                    f"minutes_since_extreme={minutes_since:.0f}>{max_minutes:.0f},"
                    f"retrace_pct={retrace_pct:.1f}>{max_retrace_pct:.1f})",
                )
                return None

        # Minimum-depth requirement (2026-05-29). A "pullback" needs to be
        # an actual pullback. Session 2026-05-29 had AVGO LONG enter at
        # 98% of the 30m range (-$17.64) and AAPL LONG at 76% (-$30.50)
        # because the regime fired on tiny micro-dips at the very top of
        # the up-leg — no real retracement to support, just a single bar's
        # breath before the next push. The retrace_pct returned by
        # _pullback_leg_context is exactly the % of the prior leg that
        # price has given back; below this floor there is no real
        # pullback to buy, so the regime must not fire. The opposite-end
        # gate above (max_retrace_pct=50.0) rejects DEEP retraces (multi-
        # hour rollovers dressed as pullbacks); together they bracket
        # what counts as a tradeable pullback. Disable via
        # ``pullback_require_real_dip: false``.
        if bool(self.params.get("pullback_require_real_dip", True)):
            min_retrace_pct = float(self.params.get("pullback_min_leg_retrace_pct", 25.0))
            if (
                minutes_since is not None
                and retrace_pct is not None
                and retrace_pct < min_retrace_pct
            ):
                side_prefix = "long" if side == Side.LONG else "short"
                self._set_build_failure(
                    c.symbol, "pullback",
                    f"{side_prefix}_pullback_too_shallow("
                    f"retrace_pct={retrace_pct:.1f}<{min_retrace_pct:.1f})",
                )
                return None

        # vol_widening applied to both ATR buffer and default_stop_pct floor (Tier 2a).
        buffer = atr * float(self.params.get("stop_buffer_atr_mult", 0.25)) * vol_widening * self._side_stop_buffer_mult(side)
        effective_default_stop_pct = self.config.risk.default_stop_pct * vol_widening * vol_scale
        target_rr = float(self.params.get("pullback_target_rr", 2.0)) * self._side_target_rr_mult(side)
        # Swing-target window ~100 wall-clock minutes (was a hardcoded 20 bars on
        # the old 5m LTF = 100 min). Scaled by ltf_minutes so the 1m LTF uses
        # ~100 bars, preserving the swing horizon the target extension was tuned
        # to — without this the 1m target only reached back 20 min and stopped
        # extending to meaningful prior swings.
        swing_bars = max(8, round(100 / ltf_min))

        if side == Side.LONG:
            stop = _safe_float(recent["low"].min(), close) - buffer
            stop = min(stop, close * (1.0 - effective_default_stop_pct))
            risk = max(0.01, close - stop)
            swing_high = _safe_float(session_ltf.tail(swing_bars)["high"].max(), close + risk * target_rr)
            target = max(close + risk * target_rr, swing_high)
        else:
            stop = _safe_float(recent["high"].max(), close) + buffer
            stop = max(stop, close * (1.0 + effective_default_stop_pct))
            risk = max(0.01, stop - close)
            swing_low = _safe_float(session_ltf.tail(swing_bars)["low"].min(), close - risk * target_rr)
            target = max(0.01, min(close - risk * target_rr, swing_low))

        return self._finalize_signal(c, side, close, stop, target, "pullback", regime_score, frame, data, vol_scale=vol_scale)

    def _build_range_signal(self, c: Candidate, side: Side, close: float, atr: float,
                            frame: pd.DataFrame, regime_score: float, data=None,
                            vol_widening: float = 1.0, vol_scale: float = 1.0) -> Signal | None:
        lookback = max(8, int(self.params.get("range_lookback_bars", 20)))
        # Scope to today's session so range_high/range_low are not polluted
        # by prior-session bars during early RTH.
        session_frame = frame[_same_day_mask(frame, now_et().date())]
        recent = session_frame.tail(lookback)
        if len(recent) < 8:
            self._set_build_failure(c.symbol, "range", f"insufficient_range_bars({len(recent)}<8)")
            return None
        # Reject range entries during a Bollinger squeeze. Squeeze = compressed
        # volatility, typically resolves via breakout — the opposite of what
        # range mean-reversion needs. NFLX 2026-04-24 13:22 SHORT fired on a
        # 12-cent range inside a squeeze (bollinger_width_pct 0.155%,
        # ATR14 0.044 on $92) and stopped at -$11.34 in 2.2 min.
        if bool(self.params.get("reject_range_during_squeeze", True)):
            tech_ctx = self._technical_context(frame)
            if bool(getattr(tech_ctx, "bollinger_squeeze", False)):
                width_pct = float(getattr(tech_ctx, "bollinger_width_pct", 0.0) or 0.0)
                self._set_build_failure(
                    c.symbol, "range",
                    f"range_bollinger_squeeze(width_pct={width_pct:.4f})",
                )
                return None
        range_high = _safe_float(recent["high"].max(), close)
        range_low = _safe_float(recent["low"].min(), close)
        # vol_widening applied (Tier 2a). Note: in range, the buffer also
        # pulls in the target (target = range_high - buffer for LONG) so
        # both stop room AND target conservatism scale with volatility,
        # which is the correct direction (wider noise needs both).
        buffer = atr * float(self.params.get("stop_buffer_atr_mult", 0.25)) * vol_widening * self._side_stop_buffer_mult(side)
        # Previous-bar confirmation — 2026-04-23 red-from-tick-one bucket
        # (AMZN 10:07, COST 11:09/13:02/15:15, LOW 13:08, HD 14:12 SHORT,
        # V 14:14 SHORT) all fired on an in-progress bar whose live tick
        # happened to cross the range-edge threshold, but the bar itself
        # closed at a mid-range value and the next bar moved adversely.
        # When enabled, require the last COMPLETED bar's close (iloc[-2])
        # to also sit in the entry zone — filters single-tick whipsaws.
        require_prev_bar = bool(self.params.get("range_require_prev_bar_confirmation", True))
        prev_close = None
        if require_prev_bar and len(recent) >= 2:
            prev_close = _optional_float(recent.iloc[-2].get("close"), None)

        if side == Side.LONG:
            # Enter near range low
            threshold = range_low + (range_high - range_low) * 0.35
            if close > threshold:
                self._set_build_failure(
                    c.symbol, "range",
                    f"not_near_range_low(close={close:.4f}>{threshold:.4f},range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            if require_prev_bar and prev_close is not None and prev_close > threshold:
                self._set_build_failure(
                    c.symbol, "range",
                    f"not_near_range_low_prev_bar(prev_close={prev_close:.4f}>{threshold:.4f},"
                    f"range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            stop = range_low - buffer
            target = range_high - buffer
        else:
            # Enter near range high
            threshold = range_high - (range_high - range_low) * 0.35
            if close < threshold:
                self._set_build_failure(
                    c.symbol, "range",
                    f"not_near_range_high(close={close:.4f}<{threshold:.4f},range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            if require_prev_bar and prev_close is not None and prev_close < threshold:
                self._set_build_failure(
                    c.symbol, "range",
                    f"not_near_range_high_prev_bar(prev_close={prev_close:.4f}<{threshold:.4f},"
                    f"range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            stop = range_high + buffer
            target = max(0.01, range_low + buffer)

        return self._finalize_signal(c, side, close, stop, target, "range", regime_score, frame, data, vol_scale=vol_scale)

    def _build_vol_squeeze_signal(self, c: Candidate, side: Side, close: float, atr: float,
                                  frame: pd.DataFrame, regime_score: float,
                                  data=None, vol_widening: float = 1.0, vol_scale: float = 1.0) -> Signal | None:
        """Build a Bollinger-squeeze breakout signal. Stops sit just outside
        the squeeze box (below box_low for LONG / above box_high for SHORT),
        with an ATR-floored buffer to absorb noise around the breakout. Target
        is the standard RR multiple. Shared filters (HTF bias, stretched-entry,
        SR/structure, FVG retest, etc.) run inside ``_finalize_signal``."""
        lookback = max(6, int(self.params.get("vol_squeeze_lookback_bars", 12)))
        session_frame = frame[_same_day_mask(frame, now_et().date())]
        if len(session_frame) < lookback + 2:
            self._set_build_failure(c.symbol, "vol_squeeze", "insufficient_session_bars")
            return None
        prior = session_frame.iloc[:-1]
        box = prior.tail(lookback)
        if len(box) < lookback:
            self._set_build_failure(c.symbol, "vol_squeeze", "insufficient_box_bars")
            return None
        box_high = _safe_float(box["high"].max(), close)
        box_low = _safe_float(box["low"].min(), close)
        box_range = max(0.0, box_high - box_low)

        # Hard breakout-quality gates (2026-05-14). Same threshold values
        # that earn +0.5 scoring bonuses in ``_score_vol_squeeze`` are
        # also enforced HERE as HARD gates — a trade with weak breakout
        # volume, weak bar body, or close that didn't clear the box-edge
        # by enough buffer is rejected outright instead of just losing
        # the bonus point. Without hard gates, a setup with strong
        # compression (3.5) + breakout (+1.5) = 5.0 passes
        # min_vol_squeeze_score (4.0) even on a weak post-breakout bar.
        #
        # Augmented 2026-05-14 with two SETUP-quality gates that proved
        # to separate winners from losers in the session log:
        #   * SR-alignment: vol_squeeze LONG rejected when sr_bias_score
        #     < -threshold (HTF SR favors the opposite side). Mirror for
        #     SHORT. Blocks AMD 10:06 (sr -0.75), NFLX 13:35 (-0.60),
        #     GOOG 10:08 SHORT (+0.75) — none of the 3 known winners
        #     would have been blocked (TSLA +0.75, COP +0.15, XOM +0.15).
        #   * pct_b directional: LONG requires close to be in the upper
        #     half of Bollinger Bands (pct_b ≥ threshold); SHORT requires
        #     lower half. AMD 10:06 LONG had pct_b 0.31, AAPL/GOOG SHORTs
        #     had 0.40/0.44 — all losers. Winners all had pct_b ≥ 0.60.
        #
        # Set ``vol_squeeze_hard_breakout_gates`` false to revert to
        # scoring-only behavior on the volume / close_pos / buffer gates.
        # The SR-alignment and pct_b gates can be disabled independently
        # via their own threshold params (set to 0.0 to disable).
        if bool(self.params.get("vol_squeeze_hard_breakout_gates", True)):
            last_bar = session_frame.iloc[-1]
            last_close = _safe_float(last_bar.get("close"), close)
            buffer_pct = self._pct_param("vol_squeeze_breakout_buffer_pct", 0.0008, vol_scale)
            if side == Side.LONG:
                required_clearance = box_high * (1.0 + buffer_pct)
                if last_close < required_clearance:
                    self._set_build_failure(
                        c.symbol, "vol_squeeze",
                        f"long_vol_squeeze_weak_breakout_buffer(close={last_close:.4f}<required={required_clearance:.4f})",
                    )
                    return None
            else:
                required_clearance = box_low * (1.0 - buffer_pct)
                if last_close > required_clearance:
                    self._set_build_failure(
                        c.symbol, "vol_squeeze",
                        f"short_vol_squeeze_weak_breakout_buffer(close={last_close:.4f}>required={required_clearance:.4f})",
                    )
                    return None
            try:
                vol_baseline = max(1.0, float(box["volume"].median()))
            except Exception:
                vol_baseline = 1.0
            cur_vol = _safe_float(last_bar.get("volume"), 0.0)
            min_vol_ratio = float(self.params.get("vol_squeeze_min_breakout_volume_ratio", 1.12))
            actual_vol_ratio = (cur_vol / vol_baseline) if vol_baseline > 0.0 else 0.0
            if actual_vol_ratio < min_vol_ratio:
                self._set_build_failure(
                    c.symbol, "vol_squeeze",
                    f"vol_squeeze_weak_breakout_volume(ratio={actual_vol_ratio:.2f}<{min_vol_ratio:.2f})",
                )
                return None
            close_pos = _bar_close_position(session_frame)
            min_close_pos = float(self.params.get("vol_squeeze_min_bar_close_position", 0.63))
            if side == Side.LONG and close_pos < min_close_pos:
                self._set_build_failure(
                    c.symbol, "vol_squeeze",
                    f"long_vol_squeeze_weak_bar_close(pos={close_pos:.2f}<{min_close_pos:.2f})",
                )
                return None
            if side == Side.SHORT and close_pos > (1.0 - min_close_pos):
                self._set_build_failure(
                    c.symbol, "vol_squeeze",
                    f"short_vol_squeeze_weak_bar_close(pos={close_pos:.2f}>{1.0 - min_close_pos:.2f})",
                )
                return None

        # Setup-quality gates that proved to separate winners from losers
        # in the 2026-05-14 session. Independent of hard_breakout_gates
        # switch — set the threshold params to 0.0 to disable each.
        sr_alignment_threshold = float(self.params.get("vol_squeeze_min_sr_bias_alignment", 0.20))
        if sr_alignment_threshold > 0.0:
            sr_ctx = self._sr_context(c.symbol, frame, data)
            sr_bias_score = _safe_float(getattr(sr_ctx, "bias_score", None), 0.0)
            if side == Side.LONG and sr_bias_score < -sr_alignment_threshold:
                self._set_build_failure(
                    c.symbol, "vol_squeeze",
                    f"long_vol_squeeze_sr_against(bias={sr_bias_score:+.2f}<{-sr_alignment_threshold:+.2f})",
                )
                return None
            if side == Side.SHORT and sr_bias_score > sr_alignment_threshold:
                self._set_build_failure(
                    c.symbol, "vol_squeeze",
                    f"short_vol_squeeze_sr_against(bias={sr_bias_score:+.2f}>{+sr_alignment_threshold:+.2f})",
                )
                return None
        pct_b_threshold = float(self.params.get("vol_squeeze_min_pct_b_directional", 0.50))
        if pct_b_threshold > 0.0:
            tech_ctx = self._technical_context(frame)
            pct_b = _safe_float(getattr(tech_ctx, "bollinger_percent_b", None), 0.5)
            if side == Side.LONG and pct_b < pct_b_threshold:
                self._set_build_failure(
                    c.symbol, "vol_squeeze",
                    f"long_vol_squeeze_pct_b_below_mid(pct_b={pct_b:.2f}<{pct_b_threshold:.2f})",
                )
                return None
            if side == Side.SHORT and pct_b > (1.0 - pct_b_threshold):
                self._set_build_failure(
                    c.symbol, "vol_squeeze",
                    f"short_vol_squeeze_pct_b_above_mid(pct_b={pct_b:.2f}>{1.0 - pct_b_threshold:.2f})",
                )
                return None

        target_rr = float(self.params.get("vol_squeeze_target_rr", 2.05)) * self._side_target_rr_mult(side)
        # Stop buffer scales with box range so tighter squeezes don't get
        # over-wide ATR-based stops. Mirrors source strategy logic.
        # vol_widening (Tier 2a) applies on top of the max() so all three
        # buffer floors expand together in trend-day regimes.
        stop_buffer = max(atr * 0.12, close * 0.0010, box_range * 0.22) * vol_widening * self._side_stop_buffer_mult(side)
        effective_default_stop_pct = self.config.risk.default_stop_pct * vol_widening * vol_scale

        if side == Side.LONG:
            stop = box_low - stop_buffer
            stop = min(stop, close * (1.0 - effective_default_stop_pct))
            risk = max(0.01, close - stop)
            target = close + risk * target_rr
        else:
            stop = box_high + stop_buffer
            stop = max(stop, close * (1.0 + effective_default_stop_pct))
            risk = max(0.01, stop - close)
            target = max(0.01, close - risk * target_rr)

        return self._finalize_signal(c, side, close, stop, target, "vol_squeeze", regime_score, frame, data, vol_scale=vol_scale)

    def _build_momentum_signal(self, c: Candidate, side: Side, close: float, atr: float,
                               frame: pd.DataFrame,
                               regime_score: float, data=None,
                               vol_widening: float = 1.0, vol_scale: float = 1.0,
                               breakout_confirmed: bool = False) -> Signal | None:
        """Build a momentum-from-open continuation signal. Stops anchor below
        recent swing low (LONG) / above recent swing high (SHORT) with an
        ATR-cushioned buffer so single-bar wicks (during midday's lower volume
        or afternoon thin liquidity) don't trigger the stop. Target uses
        ``momentum_target_rr``.

        Uses the 1m ``frame`` for the N-bar breakout check so it stays
        consistent with ``_score_momentum`` and with the source standalone
        momentum_close strategy. Renamed from ``_build_momentum_close_signal``
        2026-05-12 when the regime was generalized from afternoon-only to
        post-ORB through close.

        ``breakout_confirmed`` is set only by a CONFIRMED armed retest, where
        the breakout is already established and the fresh-breakout gate would
        reject the retest fill by definition. See ``_build_trend_signal``.
        """
        recent, trigger_level = self._breakout_reference("momentum", side, frame, frame)
        if recent is None or recent.empty:
            self._set_build_failure(c.symbol, "momentum", "insufficient_session_history")
            return None

        # Fresh-breakout gate (matches the source strategy's check)
        if side == Side.LONG:
            breakout_level = trigger_level if trigger_level is not None else close
            if not breakout_confirmed and close <= breakout_level:
                self._set_build_failure(
                    c.symbol, "momentum",
                    f"no_fresh_breakout(close={close:.4f}<=recent_high={breakout_level:.4f})",
                )
                return None
        else:
            breakout_level = trigger_level if trigger_level is not None else close
            if not breakout_confirmed and close >= breakout_level:
                self._set_build_failure(
                    c.symbol, "momentum",
                    f"no_fresh_breakdown(close={close:.4f}>=recent_low={breakout_level:.4f})",
                )
                return None

        target_rr = float(self.params.get("momentum_target_rr", 2.0)) * self._side_target_rr_mult(side)
        # ATR-cushioned swing anchor (mirrors standalone momentum_close/strategy.py:76-79).
        # vol_widening (Tier 2a) widens the ATR cushion AND the
        # default_stop_pct floor so momentum trades on trend days don't
        # get knocked out by expanded per-bar noise.
        effective_default_stop_pct = self.config.risk.default_stop_pct * vol_widening * vol_scale
        if side == Side.LONG:
            swing = _safe_float(recent["low"].min(), close) - (atr * 0.08 * vol_widening * self._side_stop_buffer_mult(side))
            stop = max(close * (1.0 - effective_default_stop_pct), swing)
            risk = max(0.01, close - stop)
            target = close + risk * target_rr
        else:
            swing = _safe_float(recent["high"].max(), close) + (atr * 0.08 * vol_widening * self._side_stop_buffer_mult(side))
            stop = min(close * (1.0 + effective_default_stop_pct), swing)
            risk = max(0.01, stop - close)
            target = max(0.01, close - risk * target_rr)

        return self._finalize_signal(c, side, close, stop, target, "momentum", regime_score, frame, data, vol_scale=vol_scale)

    def _build_vwap_reclaim_signal(self, c: Candidate, side: Side, close: float, atr: float,
                                   frame: pd.DataFrame, regime_score: float,
                                   data=None, vol_widening: float = 1.0, vol_scale: float = 1.0) -> Signal | None:
        """Build a VWAP-reclaim signal (2026-05-30). Stop sits below the flush —
        LONG: below min(VWAP, the recent dip low) − buffer; SHORT mirror —
        because losing VWAP again is the invalidation. Target rides toward the
        session high/low (HOD/LOD) so a re-igniting squeeze gets room, floored to
        the regime R:R (the runner/ladder management extends past it)."""
        session_frame = frame[_same_day_mask(frame, now_et().date())]
        lookback = max(2, int(self.params.get("vwap_reclaim_lookback_bars", 6)))
        recent = session_frame.tail(lookback + 1).iloc[:-1] if len(session_frame) > lookback else session_frame.iloc[:-1]
        if recent.empty:
            self._set_build_failure(c.symbol, "vwap_reclaim", "insufficient_session_history")
            return None
        vwap = _safe_float(session_frame.iloc[-1].get("vwap"), close)
        buffer = atr * float(self.params.get("stop_buffer_atr_mult", 0.25)) * vol_widening * self._side_stop_buffer_mult(side)
        effective_default_stop_pct = self.config.risk.default_stop_pct * vol_widening * vol_scale
        target_rr = float(self.params.get("vwap_reclaim_target_rr", 2.0)) * self._side_target_rr_mult(side)

        if side == Side.LONG:
            dip_low = _safe_float(recent["low"].min(), close)
            stop = min(dip_low, vwap) - buffer
            stop = min(stop, close * (1.0 - effective_default_stop_pct))
            risk = max(0.01, close - stop)
            session_high = _safe_float(session_frame["high"].max(), close + risk * target_rr)
            target = max(close + risk * target_rr, session_high)
        else:
            dip_high = _safe_float(recent["high"].max(), close)
            stop = max(dip_high, vwap) + buffer
            stop = max(stop, close * (1.0 + effective_default_stop_pct))
            risk = max(0.01, stop - close)
            session_low = _safe_float(session_frame["low"].min(), close - risk * target_rr)
            target = max(0.01, min(close - risk * target_rr, session_low))

        return self._finalize_signal(c, side, close, stop, target, "vwap_reclaim", regime_score, frame, data, vol_scale=vol_scale)

    def _build_sr_scalp_signal(self, c: Candidate, side: Side, close: float, atr: float,
                               frame: pd.DataFrame, regime_score: float,
                               data=None, vol_widening: float = 1.0, vol_scale: float = 1.0) -> Signal | None:
        """Build an HTF S/R scalp signal (2026-05-29 redesign).

        Two LONG setups (SHORT mirrors), both riding to the next rung:
          A. BOUNCE — price at/just off a HOLDING nearest support zone,
             target the nearest resistance above (the next rung up).
          B. FLIP-CONTINUATION — price holding just above a confirmed-
             flipped resistance (``sr_ctx.broken_resistance``, now acting
             as support), target the nearest resistance above. SHORT uses
             ``broken_support`` (a confirmed support break, now resistance).
        The higher (more immediate) of the two floors is used when both are
        in proximity. SHORT is the exact mirror with ceilings.

        Uses the bot's existing S/R machinery — NO strategy-local level
        creation. Level prices come from ``sr_ctx.nearest_support`` /
        ``nearest_resistance`` / ``broken_resistance`` / ``broken_support``;
        zone bands from ``zone_atr_mult*atr`` / ``zone_pct*close`` (max);
        stop nudge from ``sr_ctx.level_buffer × vol_widening``.

        Build-time gates (per side):
          1. An entry-side floor (LONG) / ceiling (SHORT) exists in
             proximity — either the nearest level or a confirmed flip level
             (within ``sr_scalp_max_distance_from_zone_atr*atr`` of its edge).
          2. A next rung exists in the trade direction (LONG: a resistance
             ABOVE close; SHORT: a support BELOW close).
          3. Inner gap from the floor/ceiling zone to the target rung clears
             BOTH the % floor (``sr_scalp_min_distance_pct*close``) and the
             ATR floor (``sr_scalp_min_distance_atr*atr``).
          4. Price hasn't broken through the floor/ceiling zone (holding,
             not breaking).

        Stop = floor_zone_lower − buffer (LONG) / ceiling_zone_upper + buffer
        (SHORT). Target = the next rung's inner edge ∓ buffer — matching the
        bot's structural-exit conventions everywhere else.
        """
        sr_ctx = self._sr_context(c.symbol, frame, data)
        sup = getattr(sr_ctx, "nearest_support", None)
        res = getattr(sr_ctx, "nearest_resistance", None)
        bres = getattr(sr_ctx, "broken_resistance", None)
        bsup = getattr(sr_ctx, "broken_support", None)
        sup_px = float(getattr(sup, "price", 0.0) or 0.0) if sup is not None else 0.0
        res_px = float(getattr(res, "price", 0.0) or 0.0) if res is not None else 0.0
        bres_px = float(getattr(bres, "price", 0.0) or 0.0) if bres is not None else 0.0
        bsup_px = float(getattr(bsup, "price", 0.0) or 0.0) if bsup is not None else 0.0

        # Zone band half-width — bot's existing zone construction (same
        # formula as the dashboard's key_level_zones via
        # dashboard_level_context_spec). Reads ``zone_atr_mult`` / ``zone_pct``.
        zone_half_width = max(
            float(self.params.get("zone_atr_mult", 0.20)) * atr,
            close * float(self.params.get("zone_pct", 0.0015)),
            0.01,
        )
        proximity_buffer = float(self.params.get("sr_scalp_max_distance_from_zone_atr", 0.5)) * atr
        # Stop nudge — bot's existing ``sr_ctx.level_buffer`` (same buffer the
        # _refine_*_sr_levels paths use). Scales with vol_widening (Tier 2a).
        level_buffer = float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0) * vol_widening * self._side_stop_buffer_mult(side)
        if level_buffer <= 0.0:
            level_buffer = max(atr * 0.05, 0.01) * vol_widening
        # Inner-gap floor (tradeable distance to the next rung). Max of the
        # % and ATR floors, same as before.
        required_gap = max(
            self._pct_param("sr_scalp_min_distance_pct", 0.008, vol_scale) * close,
            float(self.params.get("sr_scalp_min_distance_atr", 2.5)) * atr,
        )

        if side == Side.LONG:
            # Entry-side floor: the nearest support (mean-reversion bounce) OR
            # a confirmed-flipped resistance now acting as support
            # (continuation). Prefer the higher (more immediate) floor. The
            # proximity bounds below enforce "holding" (close stays above the
            # floor zone low), so no separate broken-through guard is needed.
            floor_px = 0.0
            if sup_px > 0.0 and (sup_px - zone_half_width) < close <= (sup_px + zone_half_width + proximity_buffer):
                floor_px = sup_px
            if 0.0 < bres_px < close <= (bres_px + zone_half_width + proximity_buffer) and bres_px > floor_px:
                floor_px = bres_px
            if floor_px <= 0.0:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"long_no_holding_support_or_flip(close={close:.4f},sup={sup_px:.4f},flipped_res={bres_px:.4f})",
                )
                return None
            # Target = nearest resistance ABOVE close (the next rung up the ladder).
            if res_px <= close:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"long_no_resistance_above(res={res_px:.4f}<=close={close:.4f})",
                )
                return None
            inner_gap = (res_px - zone_half_width) - (floor_px + zone_half_width)
            if inner_gap < required_gap:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"htf_zones_too_close(inner_gap={inner_gap:.4f}<{required_gap:.4f})",
                )
                return None
            stop = (floor_px - zone_half_width) - level_buffer
            target = (res_px - zone_half_width) - level_buffer
        else:
            # Entry-side ceiling: the nearest resistance (rejection) OR a
            # confirmed-flipped support now acting as resistance
            # (continuation). Prefer the lower (more immediate) ceiling. The
            # proximity bounds below enforce "holding" (close stays below the
            # ceiling zone high), so no separate broken-through guard is needed.
            ceil_px = 0.0
            if res_px > 0.0 and (res_px - zone_half_width - proximity_buffer) <= close < (res_px + zone_half_width):
                ceil_px = res_px
            if bsup_px > 0.0 and (bsup_px - zone_half_width - proximity_buffer) <= close < bsup_px and (ceil_px <= 0.0 or bsup_px < ceil_px):
                ceil_px = bsup_px
            if ceil_px <= 0.0:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"short_no_holding_resistance_or_flip(close={close:.4f},res={res_px:.4f},flipped_sup={bsup_px:.4f})",
                )
                return None
            # Target = nearest support BELOW close (the next rung down the ladder).
            if sup_px <= 0.0 or sup_px >= close:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"short_no_support_below(sup={sup_px:.4f}>=close={close:.4f})",
                )
                return None
            inner_gap = (ceil_px - zone_half_width) - (sup_px + zone_half_width)
            if inner_gap < required_gap:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"htf_zones_too_close(inner_gap={inner_gap:.4f}<{required_gap:.4f})",
                )
                return None
            stop = (ceil_px + zone_half_width) + level_buffer
            target = (sup_px + zone_half_width) + level_buffer

        # Noise floor on the scalp stop (2026-07-29). Zone geometry can park
        # the stop a fraction of an ATR from entry when the S/R zones are
        # tight — fine only if the tape is equally tight, which is not what
        # the gates above check. On 2026-07-28 META chopped 11.9 ATR between
        # 13:50-14:50 while sr_scalp shorted the FLOOR of that band three
        # times with stops 1.09-2.70 ATR away; 63% of the window's bars traded
        # above those stops, so being swept was closer to certain than not.
        # All three needed 2.7-4.3 ATR to survive, and all three resolved in
        # the trade's direction AFTER stopping out.
        #
        # ``shared_entry.min_stop_atr_mult`` does not cover this: that clamp
        # bounds only the SR/technical REFINEMENT passes and deliberately
        # never widens a builder's own stop. This is the builder's own stop,
        # so the regime needs its own floor. Left as a plain ATR multiple —
        # ``atr`` already tracks current volatility, and unlike the other
        # builders sr_scalp does not scale its geometry by ``vol_widening``.
        min_stop_atr = float(self.params.get("sr_scalp_min_stop_atr_mult", 2.5))
        if min_stop_atr > 0 and atr > 0:
            floor_distance = min_stop_atr * atr
            if side == Side.LONG:
                stop = min(stop, close - floor_distance)
            else:
                stop = max(stop, close + floor_distance)
            # Widening the stop costs R:R, and sr_scalp cannot extend its
            # reward to compensate — the target IS the opposing zone. When the
            # zone gap can no longer pay for the tape's noise the setup simply
            # isn't tradeable, so reject instead of taking a sub-floor R:R.
            if not self._target_meets_min_rr(side, close, stop, target):
                risk = abs(close - stop)
                reward = abs(target - close)
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"{'long' if side == Side.LONG else 'short'}_stop_floor_kills_rr("
                    f"stop_atr={min_stop_atr:.2f},risk={risk:.4f},reward={reward:.4f},"
                    f"rr={(reward / risk) if risk > 0 else 0.0:.2f})",
                )
                return None

        return self._finalize_signal(c, side, close, stop, target, "sr_scalp", regime_score, frame, data, vol_scale=vol_scale)

    def _finalize_signal(self, c: Candidate, side: Side, close: float, stop: float,
                         target: float, regime: str, regime_score: float,
                         frame: pd.DataFrame, data=None, vol_scale: float = 1.0) -> Signal | None:
        """Apply shared gates (structure, S/R, exhaustion, chart patterns) and
        build the final Signal with adaptive management metadata."""
        sr_ctx = self._sr_context(c.symbol, frame, data)
        ms_ctx = self._structure_context(frame, "ltf")
        tech_ctx = self._technical_context(frame)
        ctx = self._chart_context(frame)
        htf_ctx = self._default_htf_context_for_score(c.symbol, data)

        # Single ORB-window flag reused by all _finalize_signal ORB-bypasses
        # (Fix D, HTF bias, ORB 5m follow-through, structure entry, SR entry,
        # exhaustion, entered_in_orb_window metadata). Computed once here to
        # avoid duplicate now_et() calls with potential clock-skew at the
        # 10:05 boundary.
        orb_end = self.params.get("orb_end_time", "10:05")
        in_orb_window = self._in_orb_window(now_et().time())

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
                atr14 = _optional_float(getattr(tech_ctx, "atr14", None))
                if frame is not None and not frame.empty and atr14 is not None and atr14 > 0.0:
                    last_bar = frame.iloc[-1]
                    bar_high = _optional_float(last_bar.get("high"))
                    bar_low = _optional_float(last_bar.get("low"))
                    bar_open = _optional_float(last_bar.get("open"))
                    bar_close = _optional_float(last_bar.get("close"))
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
                        elapsed_min = (now_et() - last_fail).total_seconds() / 60.0
                        if elapsed_min < cooldown_min:
                            side_prefix = "long" if side == Side.LONG else "short"
                            self._set_build_failure(
                                c.symbol, regime,
                                f"{side_prefix}_stretched_cooldown("
                                f"elapsed={elapsed_min:.1f}m<{cooldown_min:.1f}m)",
                            )
                            return None
                pct_b = _optional_float(getattr(tech_ctx, "bollinger_percent_b", None))
                atr_stretch = _optional_float(getattr(tech_ctx, "atr_stretch_ema20_mult", None))
                pct_b_max = float(self.params.get("stretched_percent_b_max", 0.80))
                stretch_max = float(self.params.get("stretched_atr_mult_max", 1.1))
                if (
                    side == Side.LONG
                    and pct_b is not None and atr_stretch is not None
                    and pct_b >= pct_b_max and atr_stretch >= stretch_max
                ):
                    self._stretched_failure_time[c.symbol] = now_et()
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
                    self._stretched_failure_time[c.symbol] = now_et()
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

        # Entry-side mirror of resistance_break_exit / support_break_exit
        # in strategy_base.position_exit_signal. Those exits fire on
        # bar-close through sr_ctx.broken_resistance (SHORT) or
        # broken_support (LONG). If entry happens right below/above such
        # a level, the exit triggers on the first reclaim and the trade
        # never had head-room. Thresholds mirror the HTF S/R entry gate
        # (_bearish_sr_block_reason): require both pct and ATR clearance
        # so the stop and exit are separated by a non-trivial band.
        if bool(self.params.get("reject_entry_near_broken_level", True)):
            min_pct = self._pct_param("broken_level_min_clearance_pct", 0.0025, vol_scale)
            min_atr = float(self.params.get("broken_level_min_clearance_atr", 0.72))
            atr_local = _safe_float(
                frame.iloc[-1].get("atr14") if (frame is not None and not frame.empty and "atr14" in frame.columns) else None,
                max(close * 0.0015, 0.01),
            )
            if side == Side.SHORT:
                broken_res = getattr(sr_ctx, "broken_resistance", None)
                res_price = float(getattr(broken_res, "price", 0.0) or 0.0) if broken_res is not None else 0.0
                if res_price > close:
                    pct = (res_price - close) / max(close, 1e-9)
                    atr_dist = (res_price - close) / max(atr_local, 1e-9)
                    if pct <= min_pct or atr_dist <= min_atr:
                        self._set_build_failure(
                            c.symbol, regime,
                            f"short_near_broken_resistance(level={res_price:.4f},"
                            f"pct={pct:.4f}<={min_pct:.4f},atr={atr_dist:.2f}<={min_atr:.2f})",
                        )
                        return None
            else:
                broken_sup = getattr(sr_ctx, "broken_support", None)
                sup_price = float(getattr(broken_sup, "price", 0.0) or 0.0) if broken_sup is not None else 0.0
                if 0.0 < sup_price < close:
                    pct = (close - sup_price) / max(close, 1e-9)
                    atr_dist = (close - sup_price) / max(atr_local, 1e-9)
                    if pct <= min_pct or atr_dist <= min_atr:
                        self._set_build_failure(
                            c.symbol, regime,
                            f"long_near_broken_support(level={sup_price:.4f},"
                            f"pct={pct:.4f}<={min_pct:.4f},atr={atr_dist:.2f}<={min_atr:.2f})",
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
            try:
                frame_5m = self._resampled_frame(frame, 5, symbol=c.symbol, data=data)
            except Exception:
                frame_5m = None
            if frame_5m is not None and not frame_5m.empty:
                now_dt = now_et()
                session_start = now_dt.replace(hour=9, minute=30, second=0, microsecond=0)
                today_bars = frame_5m[frame_5m.index >= session_start] if hasattr(frame_5m, "index") else frame_5m
                # Use iloc[-2] (previous completed bar) when ≥2 exist.
                # iloc[-1] is the currently-forming bar.
                if hasattr(today_bars, "iloc") and len(today_bars) >= 2:
                    last_closed = today_bars.iloc[-2]
                    bar_open = _optional_float(last_closed.get("open"), None)
                    bar_close = _optional_float(last_closed.get("close"), None)
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
                # Layer 1 — explicit opposing bias (applies to all regimes)
                if side == Side.LONG and htf_bias == "bearish":
                    self._set_build_failure(
                        c.symbol, regime,
                        f"htf_bias_bearish(last_high={last_high or 'na'},"
                        f"last_low={last_low or 'na'})",
                    )
                    return None
                if side == Side.SHORT and htf_bias == "bullish":
                    self._set_build_failure(
                        c.symbol, regime,
                        f"htf_bias_bullish(last_high={last_high or 'na'},"
                        f"last_low={last_low or 'na'})",
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

        # ORB-window bypasses for 1m structure and S/R blocks. Both signals
        # are backward-looking from the opening action: a 9:30 dump candle
        # registers as CHoCH_down on the 1m chart and flips
        # `breakdown_below_support` to true, blocking LONG entries for
        # several bars even after the recovery. The trend regime's own
        # fresh-breakout gate (`close > recent LTF highs`) already proves
        # direction during the ORB window. After the window, these checks
        # resume normally.
        orb_structure_bypass = bool(self.params.get("orb_bypass_structure_entry", True)) and in_orb_window
        orb_sr_bypass = bool(self.params.get("orb_bypass_sr_entry", True)) and in_orb_window
        # Narrow the SR bypass: it still bypasses the noisy "close to level"
        # checks that the ORB bypass exists to suppress, BUT re-engages when
        # the *opposing* level (resistance for LONG, support for SHORT) is
        # dangerously close — within orb_opposing_sr_atr_mult * ATR. 2026-04-17
        # NFLX was shorted at 95.26 with support that had just broken at 95.90,
        # 0.64 away; AAPL was long'd at 269.61 with resistance 268.54 just
        # above (false break). These are exactly the "entry into the teeth
        # of opposing level" trades that the bypass shouldn't let through.
        orb_opposing_atr_mult = float(self.params.get("orb_opposing_sr_atr_mult", 0.5) or 0.0)
        atr_for_orb = _safe_float(
            frame.iloc[-1].get("atr14") if (frame is not None and not frame.empty and "atr14" in frame.columns) else None,
            max(close * 0.0015, 0.01),
        )
        opposing_sr_block = False
        if orb_sr_bypass and orb_opposing_atr_mult > 0 and atr_for_orb > 0:
            threshold = orb_opposing_atr_mult * atr_for_orb
            if side == Side.LONG:
                nearest_res = getattr(sr_ctx, "nearest_resistance", None)
                if nearest_res is not None:
                    res_price = float(getattr(nearest_res, "price", 0.0) or 0.0)
                    if res_price > 0 and 0 <= (res_price - close) <= threshold:
                        opposing_sr_block = True
            else:
                nearest_sup = getattr(sr_ctx, "nearest_support", None)
                if nearest_sup is not None:
                    sup_price = float(getattr(nearest_sup, "price", 0.0) or 0.0)
                    # Only count supports BELOW entry (proper floor); a support
                    # that sits above entry is a recently-broken level acting
                    # differently and handled by the SR engine's "breakdown" state.
                    if sup_price > 0 and 0 <= (close - sup_price) <= threshold:
                        opposing_sr_block = True
        effective_sr_bypass = orb_sr_bypass and not opposing_sr_block
        if side == Side.LONG:
            if not orb_structure_bypass and self._blocks_bullish_structure_entry(ms_ctx):
                self._set_build_failure(c.symbol, regime, self._bullish_structure_block_reason(ms_ctx))
                return None
            if not effective_sr_bypass and self._blocks_bullish_sr_entry(sr_ctx):
                self._set_build_failure(c.symbol, regime, self._bullish_sr_block_reason(sr_ctx))
                return None
            if opposing_sr_block:
                self._set_build_failure(c.symbol, regime, f"long_orb_opposing_resistance_within_{orb_opposing_atr_mult:.2f}atr")
                return None
            stop, target = self._refine_bullish_sr_levels(close, stop, target, sr_ctx, frame)
            stop, target = self._refine_bullish_technical_levels(close, stop, target, tech_ctx, frame)
        else:
            if not orb_structure_bypass and self._blocks_bearish_structure_entry(ms_ctx):
                self._set_build_failure(c.symbol, regime, self._bearish_structure_block_reason(ms_ctx))
                return None
            if not effective_sr_bypass and self._blocks_bearish_sr_entry(sr_ctx):
                self._set_build_failure(c.symbol, regime, self._bearish_sr_block_reason(sr_ctx))
                return None
            if opposing_sr_block:
                self._set_build_failure(c.symbol, regime, f"short_orb_opposing_support_within_{orb_opposing_atr_mult:.2f}atr")
                return None
            stop, target = self._refine_bearish_sr_levels(close, stop, target, sr_ctx, frame)
            stop, target = self._refine_bearish_technical_levels(close, stop, target, tech_ctx, frame)

        # Entry exhaustion check — skipped during the ORB window because
        # VWAP and EMA9 haven't equilibrated after the open. A sharp
        # V-reversal (e.g. TSLA 2026-04-15 open dump $367→$362 then run
        # to $394) artificially depresses VWAP, making the recovery look
        # "extended" when it's really the trend establishing itself.
        # After the ORB window, VWAP reflects today's action and the
        # filter becomes meaningful.
        orb_exhaustion_bypass = bool(self.params.get("orb_bypass_exhaustion", True)) and in_orb_window
        if not orb_exhaustion_bypass:
            vwap = _safe_float(frame.iloc[-1].get("vwap"), close)
            ema9 = _safe_float(frame.iloc[-1].get("ema9"), close)
            exhaustion = self._entry_exhaustion_reasons(side, frame, close=close, vwap=vwap, ema9=ema9)
            if exhaustion:
                self._set_build_failure(c.symbol, regime, exhaustion[0])
                return None

        # Scoring
        structure_bonus = 0.75 if getattr(ms_ctx, "bias", "neutral") == ("bullish" if side == Side.LONG else "bearish") else 0.0
        if side == Side.LONG and getattr(ms_ctx, "bos_up", False) and self._structure_event_recent(getattr(ms_ctx, "bos_up_age_bars", None)):
            structure_bonus += 0.5
        elif side == Side.SHORT and getattr(ms_ctx, "bos_down", False) and self._structure_event_recent(getattr(ms_ctx, "bos_down_age_bars", None)):
            structure_bonus += 0.5
        if side == Side.LONG:
            pattern_bonus = 0.35 if ctx.matched_bullish_continuation else (0.15 if ctx.matched_bullish_reversal else 0.0)
        else:
            pattern_bonus = 0.35 if ctx.matched_bearish_continuation else (0.15 if ctx.matched_bearish_reversal else 0.0)

        # Candle pattern confirmation on the trigger frame.
        # directional_candle_signal returns opposite_score/opposite_net_score
        # from the SAME @lru_cache'd context — no extra ta-lib calls.
        # _candle_context slices internally to CANDLE_CONTEXT_BARS so TA-Lib
        # has enough context to initialize its internal state.
        candle_signal = self._directional_candle_signal(frame, side)
        # Entry filter: reject when the opposing-direction candle cluster is
        # at or above candles.opposing_net_score_threshold (default 0.70 =
        # "solid" tier). Mirrors shared_exit.use_candle_pattern_exit on the
        # entry side.
        if self._shared_entry_enabled("use_opposing_candle_filter", False):
            opposing_net = float(candle_signal.get("opposite_net_score", 0.0) or 0.0)
            threshold = float(self._candles_setting("opposing_net_score_threshold", 0.70))
            if opposing_net >= threshold:
                # _candle_context is cached per strategy instance — second
                # call on same frame returns the stored dict, no detection.
                cc = self._candle_context(frame)
                opp_prefix = "bearish" if side == Side.LONG else "bullish"
                opp_matches = ",".join(
                    str(m) for m in sorted(cc.get(f"matched_{opp_prefix}_candles", []) or [])[:3]
                )
                self._set_build_failure(
                    c.symbol, regime,
                    f"{'long' if side == Side.LONG else 'short'}_opposing_candle"
                    f"(net_score={opposing_net:.2f}>={threshold:.2f},"
                    f"matches={opp_matches or 'na'})",
                )
                return None
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

        adjustments = self._entry_adjustment_components(side, sr_ctx=sr_ctx, tech_ctx=tech_ctx, htf_ctx=htf_ctx)
        fvg_adjustments = self._fvg_entry_adjustment_components(side, c.symbol, frame, data)
        fvg_cont_bias = float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0)
        runner_allowed = bool(fvg_cont_bias >= 0.35 and structure_bonus >= 0.75)

        # Apply adaptive_ladder rungs when configured. The helper falls back
        # to the original target when ladder mode isn't active, the regime
        # opts out (range), or no qualifying rungs exist — so this call is
        # safe to make unconditionally.
        atr_for_ladder = _safe_float(
            frame.iloc[-1].get("atr14") if (frame is not None and not frame.empty and "atr14" in frame.columns) else None,
            max(close * 0.0015, 0.01),
        )
        target, ladder_meta = self._apply_ladder_if_enabled(
            side, close, stop, target,
            regime=regime, sr_ctx=sr_ctx, atr=atr_for_ladder,
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
        # complements entry_min_clearance_atr (FLOOR on SR clearance).
        # Placement invariant: runs AFTER ladder + runner override so
        # `target` is the trade's FINAL take-profit (None in runner mode →
        # gate inert). Trend-only: range targets ARE opposing SR by design.
        if (
            regime == "trend"
            and target is not None
            and bool(self.params.get("reject_target_beyond_sr", True))
        ):
            target_max_sr_ratio = float(self.params.get("target_max_sr_ratio", 0.8))
            tgt = float(target)
            if side == Side.LONG:
                near = getattr(sr_ctx, "nearest_resistance", None)
                level_price = float(getattr(near, "price", 0.0) or 0.0)
                valid = level_price > close
                dist_to_sr = level_price - close if valid else 0.0
                dist_to_target = tgt - close
                level_name = "resistance"
                reason_prefix = "long_target_beyond_resistance"
            else:
                near = getattr(sr_ctx, "nearest_support", None)
                level_price = float(getattr(near, "price", 0.0) or 0.0)
                valid = 0.0 < level_price < close
                dist_to_sr = close - level_price if valid else 0.0
                dist_to_target = close - tgt
                level_name = "support"
                reason_prefix = "short_target_beyond_support"
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
        activity_weight = float(self.params.get("activity_score_weight", 0.12))
        final_priority_score = (
            regime_score
            + (float(c.activity_score) * activity_weight)
            + structure_bonus
            + pattern_bonus
            + candle_bonus
            + adjustments["entry_context_adjustment"]
            + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
        )

        reason = f"top_tier_{regime}_{'long' if side == Side.LONG else 'short'}"
        # Tag entries that used an ORB-window bypass so post-session analysis
        # can slice performance by ORB vs post-ORB entries. 2026-04-17 ORB
        # entries were 1W/4T (-$120); post-ORB 10:05-11:00 window was 2W/5T
        # (+$126) thanks to TSLA. Without an explicit tag we reconstruct from
        # entry timestamps, which conflates 10:00-10:05 edge cases.
        entered_in_orb_window = bool(in_orb_window)
        metadata = self._build_signal_metadata(
            entry_price=close,
            chart_ctx=ctx, ms_ctx=ms_ctx, sr_ctx=sr_ctx, tech_ctx=tech_ctx,
            adjustments=adjustments, fvg_adjustments=fvg_adjustments,
            management=management, ladder_meta=ladder_meta,
            final_priority_score=final_priority_score,
            leading={
                "regime": regime,
                "regime_score": round(regime_score, 4),
                "structure_bonus": round(structure_bonus, 4),
                "pattern_bonus": round(pattern_bonus, 4),
                "candle_bonus": round(candle_bonus, 4),
                "candle_confirmed": candle_confirmed,
                "candle_tier": candle_signal.get("confirm_tier"),
                "candle_anchor": candle_signal.get("anchor_pattern"),
                "candle_matches": candle_signal.get("matches", []),
                "orb_window_entry": entered_in_orb_window,
                "orb_end_time": str(orb_end),
            },
        )
        return Signal(
            symbol=c.symbol, strategy=self.strategy_name, side=side,
            reason=reason, stop_price=float(stop),
            target_price=None if target is None else float(target),
            metadata=metadata,
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
        self._reset_entry_decisions()
        self._prune_armed_retests()
        out: list[Signal] = []
        min_bars = int(self.params.get("min_bars", 60) or 60)
        ltf_min = max(1, int(self.params.get("ltf_minutes", 5)))
        # Indicator-span stretch for the LTF frame. With a 1m LTF, span_scale=5
        # makes ema9/ema20/atr14/adx14/rsi14/ret5/ret15 behave like the old 5m
        # frame (ema9->45, atr14->70, ...) so entry/exit logic keeps its 5m
        # wall-clock horizons while acting on finer 1m bars/closes. Default 1.0
        # leaves the canonical spans untouched.
        ltf_span_scale = float(self.params.get("ltf_indicator_span_scale", 1.0))
        min_ltf_bars = int(self.params.get("min_ltf_bars", 15))
        allow_short = bool(self.config.risk.allow_short)
        now_t = now_et().time()
        allowed_regimes = self._allowed_regimes(now_t)
        if not allowed_regimes:
            for c in candidates:
                self._record_entry_decision(c.symbol, "skipped", ["outside_entry_window"])
            return out
        # ORB window flag — consumed by the surviving orb_bypass_* gates
        # (htf_bias / structure / sr / exhaustion / screener_bias /
        # side_decision / relative_strength), all of which apply to the orb
        # regime at the open when their inputs (15m structure, sector-ETF
        # VWAP, etc.) are still stale. The orb regime is not in the index-
        # confirmation / confirmation-bar regime sets, so it never hits those
        # gates — the old orb_bypass_index_confirmation / _entry_confirmation_bar
        # companions were removed (2026-05-29).
        in_orb_window = self._in_orb_window(now_t)

        # Index ok / neutral are now PER-CANDIDATE because of the
        # ``sector_index_map``-driven per-sector ETF lookup (see
        # ``_indices_for_symbol``). An AAPL LONG checks XLK; an XOM LONG
        # checks XLE; FCX/NEM check XLB; etc. Moved into the per-
        # candidate loop below since the result varies by ``c.symbol``
        # even within a single cycle.
        sides_to_evaluate = [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]

        # Extended-hours universe gate: outside RTH (07:00-09:30 / 16:00-20:00)
        # only the configured liquid names may enter; thinner names skip pre/
        # post-market entries where spreads/fills are poor. Active only in
        # extended-indicator mode (RTH-only presets never hit this). Session
        # state is evaluated once per cycle.
        ext_hours_now = (
            get_session_indicator_window() == "extended"
            and not equity_session_state(now_et()).regular_session
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

        for c in candidates:
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
                continue
            # Per-symbol earnings blackout. An earnings print resets the
            # symbol's volatility regime, so the ATR-derived stops and the
            # session-open-anchored day_strength this strategy relies on are
            # both calibrated to a distribution that no longer holds. Across
            # 23 mega caps that is roughly 92 scheduled events a year,
            # clustered into three weeks a quarter.
            earnings_block = self._event_calendar.earnings_block_reason(c.symbol)
            if earnings_block is not None:
                self._record_entry_decision(c.symbol, "skipped", [earnings_block])
                continue
            if ext_hours_now and not ext_all and c.symbol.upper().strip() not in ext_allowed:
                self._record_entry_decision(c.symbol, "skipped", ["extended_hours_not_eligible"])
                continue
            frame = bars.get(c.symbol)
            if frame is None or len(frame) < min_bars:
                self._record_entry_decision(c.symbol, "skipped", [
                    insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), min_bars)])
                continue

            ltf = self._resampled_frame(frame, ltf_min, symbol=c.symbol, data=data, span_scale=ltf_span_scale)
            if ltf is None or ltf.empty or len(ltf) < min_ltf_bars:
                self._record_entry_decision(c.symbol, "skipped", ["missing_ltf_context"])
                continue

            # Per-candidate index lookup — uses ``sector_index_map`` to route
            # AAPL → XLK, XOM → XLE, FCX → XLB, etc. Falls back to
            # ``index_symbols`` when no sector mapping exists for the symbol.
            index_ok_by_side = {side: self._index_confirms(side, c.symbol, bars, data) for side in sides_to_evaluate}
            idx_neutral = self._index_neutral(c.symbol, bars)

            last = ltf.iloc[-1]
            close = _safe_float(last["close"])
            vwap = _safe_float(last.get("vwap"), close)
            ema9 = _safe_float(last.get("ema9"), close)
            ema20 = _safe_float(last.get("ema20"), close)
            adx = _safe_float(last.get("adx14"), 0.0)
            ret5 = _safe_float(last.get("ret5"), 0.0)
            ret15 = _safe_float(last.get("ret15"), 0.0)
            atr = max(_safe_float(last.get("atr14"), close * 0.0015), close * 0.0005, 0.01)

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
            # magnitude for penalty scaling, sharing one _session_open_price
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
            # has actually been doing across recent cycles.
            hist = self._recent_directional_bias.get(c.symbol)
            if hist is None or hist.maxlen != trailing_lookback:
                existing = list(hist) if hist is not None else []
                hist = deque(existing[-trailing_lookback:], maxlen=trailing_lookback)
                self._recent_directional_bias[c.symbol] = hist
            hist.append(live_bias)

            # Both sides are always evaluated under soft bias. The penalty
            # applied per-side inside the loop below filters out weak
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
            # queue below against SIDE_DECISION_REGIMES. Bypassed inside the
            # ORB window when ``orb_bypass_side_decision`` is true — the
            # opening tape is gap-dominated.
            side_decision_orb_bypass = (
                bool(self.params.get("orb_bypass_side_decision", True)) and in_orb_window
            )
            # Tracks whether ``_decide_side`` made an explicit, current-
            # action side pick this cycle. When True, the soft bias
            # penalty below is skipped — the explicit decision already
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
                        continue

            best_signal: Signal | None = None
            fail_reasons: list[str] = []

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
                    self._score_range(close, vwap, ema9, ema20, frame, idx_neutral, vol_scale)
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

                # Per-side BUILD ORDER: list of qualifying regimes in score
                # order. A regime qualifies if it's in allowed_regimes AND
                # its post-penalty score meets its own min_*_score threshold.
                # The build phase iterates ACROSS sides AND regimes in
                # cross-side score order so no regime blocks another (within
                # OR across sides). Both sides' qualifiers compete in the
                # same flat queue.
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
                }))

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
                    penalty_suffix = (
                        f",bias_pen={decision['bias_penalty']:.2f}"
                        if decision["bias_penalty"] > 0.0 else ""
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
                # own window bypass. See the side-decision block above.
                if regime_name in SIDE_DECISION_REGIMES and explicit_side_decided is False and side_vote_note:
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
                # rejected anyway never arms.
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
                    retest_meta = {
                        "armed_retest_regime": regime_name,
                        "armed_retest_level": round(float(arm["trigger_level"]), 4),
                        "armed_retest_waited_minutes": round(
                            float(arm.get("waited_minutes", 0.0)), 2),
                        "armed_retest_status": "expired_market_entry",
                    }
                    # `breakout_confirmed` stays False: the fallback has to
                    # clear the builder's own fresh-breakout check on its own.
                    # If price faded while we waited, there is no trade.
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
                    # Only a CONFIRMED retest satisfies the builder's
                    # fresh-breakout gate in advance.
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
                    # per-trade based on day_strength at ENTRY; risk.py
                    # reads it from position.metadata at management time.
                    # Falls back to the global config default when not set.
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
                    # entry. ``position_manager._adaptive_ladder_management``
                    # re-checks these at target-hit time so a sector ETF
                    # that has flipped against the trade can short-circuit
                    # the multi-bar zone-flip wait (target exits at the
                    # rung price instead of riding through a sector
                    # reversal). Read in
                    # ``_ladder_indices_still_aligned``.
                    if isinstance(sig.metadata, dict):
                        sig.metadata["confirmation_indices"] = list(self._indices_for_symbol(c.symbol))
                        # Cross-regime-comparable score. ``signal_priority_key``
                        # ranks competing signals on this when there are more
                        # signals than free position slots; the raw
                        # ``regime_score`` next to it stays for reporting.
                        sig.metadata["regime_score_normalized"] = round(float(regime_norm), 4)
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

            if best_signal is not None:
                out.append(best_signal)
                # Stamp the soft-bias penalty value on the success path too
                # so post-mortem can see whether a winner was nearly killed
                # by bias drag. Sourced from the winning side's decision.
                detail_payload: dict[str, Any] = {}
                if winning_decision is not None:
                    bp = float(winning_decision.get("bias_penalty", 0.0) or 0.0)
                    if bp > 0.0:
                        detail_payload["bias_pen"] = round(bp, 4)
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
        return out

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------
    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
