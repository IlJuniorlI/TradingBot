# SPDX-License-Identifier: MIT
"""The ``momentum`` regime: scorer and builder."""
from __future__ import annotations

import pandas as pd

from ....models import Candidate, Side, Signal
from ....numeric import safe_float
from ..confirmation import setup_window, stop_anchor


class MomentumRegimeMixin:
    """Momentum from the open: day strength plus a fresh N-bar extreme.
    Mixed into ``TopTierAdaptiveStrategy`` (``strategy.py``), whose
    ``BaseStrategy`` supplies ``params``, ``config`` and the contexts.

    The breakout window is the LTF's (2026-10-13): the last
    ``momentum_breakout_lookback_bars`` of today's closed buckets before the
    current one (6 x 5m = 30 minutes on the shipped preset; 6 one-minute
    bars until then), which the latest 1m close must clear; day strength
    reads the step frame's session open."""

    def _score_momentum(self, side: Side, close: float, vwap: float, ema9: float,
                        ema20: float, ret15: float, ltf: pd.DataFrame, frame: pd.DataFrame,
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

        # N-bar breakout: today's LTF buckets before the current one, all of
        # them (``setup_window``, the window ``_breakout_reference`` reads for
        # the builder); fewer than the lookback is no breakout to score.
        lookback = max(3, int(self.params.get("momentum_breakout_lookback_bars", 6)))
        recent = setup_window(ltf, lookback)
        if len(recent) < lookback:
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
            breakout_level = safe_float(recent["high"].max(), close)
            if close > breakout_level:
                score += 1.5
            if close > vwap:
                score += 1.0
            if ema9 >= ema20:
                score += 0.5
            if ret15 > 0:
                score += 0.5
        else:
            breakout_level = safe_float(recent["low"].min(), close)
            if close < breakout_level:
                score += 1.5
            if close < vwap:
                score += 1.0
            if ema9 <= ema20:
                score += 0.5
            if ret15 < 0:
                score += 0.5
        return score

    def _build_momentum_signal(self, c: Candidate, side: Side, close: float, atr: float,
                               ltf: pd.DataFrame, frame: pd.DataFrame,
                               regime_score: float, data=None,
                               vol_widening: float = 1.0, vol_scale: float = 1.0,
                               breakout_confirmed: bool = False) -> Signal | None:
        """Build a momentum-from-open continuation signal. Stops anchor below
        recent swing low (LONG) / above recent swing high (SHORT) with an
        ATR-cushioned buffer so single-bar wicks (during midday's lower volume
        or afternoon thin liquidity) don't trigger the stop. Target uses
        ``momentum_target_rr``.

        Reads the LTF's setup window for the N-bar breakout check
        (``_breakout_reference``), the window ``_score_momentum`` reads; the
        stop's swing anchor also clears every 1m bar since it
        (``stop_anchor``). Renamed from ``_build_momentum_close_signal``
        2026-05-12 when the regime was generalized from afternoon-only to
        post-ORB through close.

        ``breakout_confirmed`` is set by an armed retest -- a confirmed retest,
        or an expired arm whose breakout still holds -- where the breakout is
        already established and the fresh-breakout gate would reject the fill
        by definition. See ``_build_trend_signal``.
        """
        recent, trigger_level = self._breakout_reference("momentum", side, ltf)
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
            swing = stop_anchor(side, recent, frame, ltf, close) - (atr * 0.08 * vol_widening * self._side_stop_buffer_mult(side))
            stop = max(close * (1.0 - effective_default_stop_pct), swing)
            risk = max(0.01, close - stop)
            target = close + risk * target_rr
        else:
            swing = stop_anchor(side, recent, frame, ltf, close) + (atr * 0.08 * vol_widening * self._side_stop_buffer_mult(side))
            stop = min(close * (1.0 + effective_default_stop_pct), swing)
            risk = max(0.01, stop - close)
            target = max(0.01, close - risk * target_rr)

        return self._finalize_signal(c, side, close, stop, target, "momentum", regime_score, frame, data, vol_scale=vol_scale)
