# SPDX-License-Identifier: MIT
"""The ``pullback`` regime: scorer, leg context and builder."""
from __future__ import annotations

import pandas as pd

from ....bars import bar_close_position, same_day_mask
from ....models import Candidate, Side, Signal
from ....numeric import safe_float
from .... import sessions


class PullbackRegimeMixin:
    """Pullback: a trend retracing to EMA20 / VWAP on a young, real leg.
    Mixed into ``TopTierAdaptiveStrategy`` (``strategy.py``), whose
    ``BaseStrategy`` supplies ``params``, ``config`` and the contexts."""

    def _score_pullback(self, side: Side, close: float, vwap: float, ema9: float, ema20: float,
                        adx: float, atr: float, trend_score: float, ltf: pd.DataFrame) -> float:
        min_trend = float(self.params.get("min_pullback_trend_score", 3.0))
        if trend_score < min_trend:
            return 0.0
        session_ltf = ltf[same_day_mask(ltf, sessions.now_et().date())]
        score = 0.0
        touch_mult = float(self.params.get("pullback_ema_touch_atr_mult", 0.35))
        touch_dist = atr * touch_mult
        lookback = max(2, int(self.params.get("pullback_lookback_bars", 5)))
        recent = session_ltf.tail(lookback + 1).iloc[:-1] if len(session_ltf) > lookback else session_ltf.iloc[:-1]
        if recent.empty:
            return 0.0

        if side == Side.LONG:
            recent_low = safe_float(recent["low"].min(), close)
            touched_ema20 = recent_low <= ema20 + touch_dist
            touched_vwap = recent_low <= vwap + touch_dist
            if touched_ema20 or touched_vwap:
                score += 1.5
            hold_mult = float(self.params.get("pullback_hold_atr_mult", 0.40))
            if recent_low >= ema20 - (atr * hold_mult):
                score += 1.0
            if close > ema9:
                score += 1.0
            close_pos = bar_close_position(session_ltf if not session_ltf.empty else ltf)
            if close_pos >= 0.60:
                score += 0.5
        else:
            recent_high = safe_float(recent["high"].max(), close)
            touched_ema20 = recent_high >= ema20 - touch_dist
            touched_vwap = recent_high >= vwap - touch_dist
            if touched_ema20 or touched_vwap:
                score += 1.5
            hold_mult = float(self.params.get("pullback_hold_atr_mult", 0.40))
            if recent_high <= ema20 + (atr * hold_mult):
                score += 1.0
            if close < ema9:
                score += 1.0
            close_pos = bar_close_position(session_ltf if not session_ltf.empty else ltf)
            if close_pos <= 0.40:
                score += 0.5

        # Volume expansion on current bar
        vol_src = session_ltf if not session_ltf.empty else ltf
        vol = safe_float(vol_src.iloc[-1].get("volume"), 0.0)
        vol_mean = safe_float(recent["volume"].mean(), 1.0)
        if vol_mean > 0 and vol / vol_mean >= 1.10:
            score += 0.5
        if adx >= float(self.params.get("min_adx14", 15.0)):
            score += 0.5
        return score

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

        A session whose highs / lows do not read raises: both maturity
        checks skip on ``(None, None)``, so until 2026-09-26, when a read
        error returned it, the error let the pullback through.
        """
        if session_ltf is None or session_ltf.empty:
            return None, None
        n = len(session_ltf)
        if n < 2:
            return None, None
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
        session_ltf = ltf[same_day_mask(ltf, sessions.now_et().date())]
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
            stop = safe_float(recent["low"].min(), close) - buffer
            stop = min(stop, close * (1.0 - effective_default_stop_pct))
            risk = max(0.01, close - stop)
            swing_high = safe_float(session_ltf.tail(swing_bars)["high"].max(), close + risk * target_rr)
            target = max(close + risk * target_rr, swing_high)
        else:
            stop = safe_float(recent["high"].max(), close) + buffer
            stop = max(stop, close * (1.0 + effective_default_stop_pct))
            risk = max(0.01, stop - close)
            swing_low = safe_float(session_ltf.tail(swing_bars)["low"].min(), close - risk * target_rr)
            target = max(0.01, min(close - risk * target_rr, swing_low))

        return self._finalize_signal(c, side, close, stop, target, "pullback", regime_score, frame, data, vol_scale=vol_scale)
