# SPDX-License-Identifier: MIT
"""The ``vwap_reclaim`` regime: scorer and builder."""
from __future__ import annotations

import pandas as pd

from ....bars import bar_wick_fractions
from ....models import Candidate, Side, Signal
from ....numeric import safe_float
from ..confirmation import session_window, stop_anchor, today_bars


def dip_minute_bars(frame: pd.DataFrame, ltf: pd.DataFrame, dip: pd.DataFrame) -> pd.DataFrame:
    """The step frame's 1m bars of the dip window: from its first bucket to
    the current one (``ltf``'s last row)."""
    if dip.empty:
        return frame.iloc[0:0]
    return frame.iloc[frame.index.searchsorted(dip.index[0]):frame.index.searchsorted(ltf.index[-1])]


class VwapReclaimRegimeMixin:
    """VWAP reclaim: a flush through session VWAP reclaimed on volume. Mixed
    into ``TopTierAdaptiveStrategy`` (``strategy.py``), whose
    ``BaseStrategy`` supplies ``params``, ``config`` and the contexts.

    The dip is set up on the LTF (2026-10-13): a bucket among the last
    ``vwap_reclaim_lookback_bars`` of the session's closed ones before the
    current one (``session_window``; 6 x 5m = 30 minutes on the shipped
    preset, so from 10:00) closed through the step frame's session VWAP as
    it stood at that bucket's close. The reclaim is the latest 1m close
    against the session VWAP, the volume pop and the wick the last 1m
    bar's, a closed bar (``_read_candidate`` drops one still trading). One
    VWAP for the dip and the reclaim: the step frame's session VWAP. A
    premarket bucket, whose VWAP is the all-hours one, is no dip (user
    decision, 2026-10-13); the HOD / LOD target still counts the premarket.
    Until then the dip window was the last 6 one-minute bars."""

    def _score_vwap_reclaim(self, side: Side, close: float, vwap: float, ema9: float,
                            ema20: float, atr: float, ltf: pd.DataFrame, frame: pd.DataFrame,
                            vol_scale: float = 1.0) -> float:
        """Score a VWAP-reclaim momentum re-entry (2026-05-30).

        Thesis (LONG; SHORT mirrors): a running name dips BELOW session VWAP — a
        quick flush that shakes out weak longs / trips stops — then RECLAIMS VWAP
        on a volume pop, the move (often a squeeze) re-igniting. That exact moment
        falls between trend (needs close>VWAP AND ema9>ema20), pullback (needs to
        HOLD above ema20), and momentum (needs a fresh N-bar high) — none of them
        catch it. ``vwap`` is the step frame's session VWAP, and so is the LTF's
        ``vwap`` column (``vwap_at_bucket_close``).

        Components (max ~5.0):
          * +0.5  base
          * +2.0  reclaim confirmed — a bucket closed below VWAP within the
                  last ``vwap_reclaim_lookback_bars`` of the session's LTF
                  buckets (``session_window``: the window must be whole)
                  AND the latest close is back above VWAP by a small
                  buffer. No reclaim => 0 (regime sits out).
          * +1.0  volume pop on the reclaim bar (the last 1m bar's volume >=
                  the dip window's mean 1m volume x ratio)
          * +0.7  trend intact — close > ema9
          * +0.3  structure aligned — ema9 >= ema20
          * +0.5  bounce character — the last 1m bar's wick on the flush
                  side (bought the dip)
        """
        if vwap <= 0 or close <= 0 or atr <= 0 or frame is None or frame.empty:
            return 0.0
        lookback = max(2, int(self.params.get("vwap_reclaim_lookback_bars", 6)))
        if ltf is None or "vwap" not in ltf.columns:
            return 0.0
        # The session's closed buckets before the current one, whole: each
        # one's VWAP is the session VWAP the reclaim reads.
        recent = session_window(ltf, lookback)
        if len(recent) < lookback:
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
        cur_vol = safe_float(frame.iloc[-1].get("volume"), 0.0)
        dip_minutes = dip_minute_bars(frame, ltf, recent)
        vol_mean = safe_float(dip_minutes["volume"].mean(), 0.0) if not dip_minutes.empty else 0.0
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
        upper_wick, lower_wick, _body, bar_range = bar_wick_fractions(frame)
        if bar_range > 0:
            wick = lower_wick if side == Side.LONG else upper_wick
            if wick >= 0.25:
                score += 0.5
        return score

    def _build_vwap_reclaim_signal(self, c: Candidate, side: Side, close: float, atr: float,
                                   ltf: pd.DataFrame, frame: pd.DataFrame, regime_score: float,
                                   data=None, vol_widening: float = 1.0, vol_scale: float = 1.0) -> Signal | None:
        """Build a VWAP-reclaim signal (2026-05-30). Stop sits below the flush —
        LONG: below min(VWAP, the dip window's low, every 1m bar since it)
        − buffer; SHORT mirror — because losing VWAP again is the
        invalidation. The dip window is the scorer's (``session_window``).
        Target rides toward the day's high/low (HOD/LOD, today's bars from
        midnight, premarket included: ``today_bars``) so a re-igniting
        squeeze gets room, floored to the regime R:R (the runner/ladder
        management extends past it)."""
        session_frame = today_bars(frame)
        lookback = max(2, int(self.params.get("vwap_reclaim_lookback_bars", 6)))
        recent = session_window(ltf, lookback)
        if recent.empty or session_frame.empty:
            self._set_build_failure(c.symbol, "vwap_reclaim", "insufficient_session_history")
            return None
        vwap = safe_float(session_frame.iloc[-1].get("vwap"), close)
        buffer = atr * float(self.params.get("stop_buffer_atr_mult", 0.25)) * vol_widening * self._side_stop_buffer_mult(side)
        effective_default_stop_pct = self.config.risk.default_stop_pct * vol_widening * vol_scale
        target_rr = float(self.params.get("vwap_reclaim_target_rr", 2.0)) * self._side_target_rr_mult(side)

        if side == Side.LONG:
            dip_low = stop_anchor(side, recent, frame, ltf, close)
            stop = min(dip_low, vwap) - buffer
            stop = min(stop, close * (1.0 - effective_default_stop_pct))
            risk = max(0.01, close - stop)
            session_high = safe_float(session_frame["high"].max(), close + risk * target_rr)
            target = max(close + risk * target_rr, session_high)
        else:
            dip_high = stop_anchor(side, recent, frame, ltf, close)
            stop = max(dip_high, vwap) + buffer
            stop = max(stop, close * (1.0 + effective_default_stop_pct))
            risk = max(0.01, stop - close)
            session_low = safe_float(session_frame["low"].min(), close - risk * target_rr)
            target = max(0.01, min(close - risk * target_rr, session_low))

        return self._finalize_signal(c, side, close, stop, target, "vwap_reclaim", regime_score, frame, data, vol_scale=vol_scale)
