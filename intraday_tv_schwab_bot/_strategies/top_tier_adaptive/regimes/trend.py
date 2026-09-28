# SPDX-License-Identifier: MIT
"""The ``trend`` regime: scorer and builder."""
from __future__ import annotations

import pandas as pd

from ....models import Candidate, Side, Signal
from ....numeric import safe_float


class TrendRegimeMixin:
    """Trend: VWAP / EMA posture with ADX and index agreement; enters a
    fresh N-bar extreme. Mixed into ``TopTierAdaptiveStrategy``
    (``strategy.py``), whose ``BaseStrategy`` supplies ``params``,
    ``config`` and the contexts."""

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

    def _build_trend_signal(self, c: Candidate, side: Side, close: float, atr: float,
                            ltf: pd.DataFrame, frame: pd.DataFrame, regime_score: float,
                            data=None, vol_widening: float = 1.0, vol_scale: float = 1.0,
                            breakout_confirmed: bool = False) -> Signal | None:
        """``breakout_confirmed`` is set by an armed retest: a CONFIRMED retest,
        or an expired arm whose breakout still holds (the market fallback).

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
            stop = safe_float(recent["low"].min(), close) - buffer
            stop = min(stop, close * (1.0 - effective_default_stop_pct))
            risk = max(0.01, close - stop)
            target = close + risk * target_rr
        else:
            trigger_low = safe_float(recent["low"].min(), close)
            if not breakout_confirmed and close >= trigger_low:
                self._set_build_failure(
                    c.symbol, "trend",
                    f"no_fresh_breakdown(close={close:.4f}>=recent_low={trigger_low:.4f})",
                )
                return None
            stop = safe_float(recent["high"].max(), close) + buffer
            stop = max(stop, close * (1.0 + effective_default_stop_pct))
            risk = max(0.01, stop - close)
            target = max(0.01, close - risk * target_rr)

        return self._finalize_signal(c, side, close, stop, target, "trend", regime_score, frame, data, vol_scale=vol_scale)
