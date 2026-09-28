# SPDX-License-Identifier: MIT
"""The ``range`` regime: the fade zone, scorer and builder."""
from __future__ import annotations

import pandas as pd

from ....bars import bar_wick_fractions, same_day_mask
from ....models import Candidate, Side, Signal
from ....numeric import safe_float
from .... import sessions


class RangeRegimeMixin:
    """Range: fade the edge of the lookback range. Mixed into
    ``TopTierAdaptiveStrategy`` (``strategy.py``), whose ``BaseStrategy``
    supplies ``params``, ``config`` and the contexts."""

    def _range_entry_zone(self, side: Side, recent: pd.DataFrame,
                          close: float) -> tuple[bool, float, float, float]:
        """Is *close* inside the fade zone at the range edge *side* trades?

        ONE definition, read by both ``_score_range`` and
        ``_build_range_signal``. Returns
        ``(in_zone, range_low, range_high, threshold)``; the threshold is the
        inner edge of the zone, which LONG enters at or below and SHORT at or
        above. Extracted for the same reason as ``_breakout_reference``: a
        scorer and a builder that derive the same geometry separately drift,
        and here they had drifted all the way apart (see ``_score_range``).
        """
        range_high = safe_float(recent["high"].max(), close)
        range_low = safe_float(recent["low"].min(), close)
        span = max(0.0, range_high - range_low)
        frac = float(self.params.get("range_entry_zone_frac", 0.35))
        if side == Side.LONG:
            threshold = range_low + span * frac
            return close <= threshold, range_low, range_high, threshold
        threshold = range_high - span * frac
        return close >= threshold, range_low, range_high, threshold

    def _score_range(self, side: Side, close: float, ema9: float, ema20: float,
                     frame: pd.DataFrame, tech_ctx, index_neutral: bool,
                     vol_scale: float = 1.0) -> float:
        """Score the range fade on the geometry ``_build_range_signal`` gates
        on: price sitting in the fade zone at the range edge this side trades.

        The previous scorer measured range CHARACTER and never looked at where
        in the range price was -- VWAP proximity (+1.5), EMA gap, VWAP flip
        count, intraday range width, index neutrality -- while the builder
        enters ONLY from the outer ``range_entry_zone_frac`` of the range.
        Those are close to opposites: the largest single component rewarded
        sitting ON VWAP, which is the middle of the range, so a mid-range bar
        scored HIGHER than a bar on the edge. Replayed over 10,162 real 1m
        bars (28 symbols, 2026-09-21) the old scorer correlated -0.17 with
        distance from mid-range, averaged 1.94 at an edge against 2.19
        mid-range, and blocked 6,485 of the 7,477 bars that WERE at an edge.
        The regime could not take the trade it exists to take.

        Same defect ``_score_sr_scalp`` was redesigned out of on 2026-05-29
        ("measured chop character ... UNCORRELATED with the level geometry the
        builder actually gates on"), in the other mean-reversion regime; the
        lesson was never carried across. Same fix: score the builder's
        geometry and keep the character checks as corroboration rather than
        as the thesis.

        Now SIDE-AWARE. It took no ``side`` before, so LONG and SHORT scored
        identically and the auction could not tell which edge price was on --
        every other regime scorer is side-aware.

        Components (max 5.0):
          * +0.5  base (regime in play). Also the ceiling when the tape is in
                  a Bollinger squeeze and ``reject_range_during_squeeze`` is
                  on, because the builder refuses those outright -- compressed
                  volatility resolves by breaking, which is the opposite of
                  what a fade needs. That gate was enforced only at build time
                  until 2026-09-22, so a squeezed bar could score the full 5.0,
                  win the auction and die on ``range_bollinger_squeeze``.
          * +2.0  close is in the fade zone at THIS side's edge AND, when
                  ``range_require_prev_bar_confirmation`` is on, so is the
                  last COMPLETED bar -- both of the builder's entry gates,
                  enforced together. Either missing => base only, which
                  cannot reach ``min_range_score``. The prev-bar half used to
                  be an optional +0.5 while the builder demanded it, so a
                  single-bar poke into the zone scored 3.5, cleared the floor,
                  won its place in the build queue and then died on
                  ``not_near_range_low_prev_bar``: a guaranteed-fail path, not
                  an optimistic one. ORB had the same shape, fixed the same
                  way two days ago.
          * +1.0  the bar rejected the edge (LONG: lower wick >= 0.30 of bar
                  range; SHORT: upper wick >= 0.30).
          * +0.5  the fade is DEEP rather than marginal -- close is in the
                  inner half of the zone, nearer the extreme than the
                  threshold.
          * +0.5  the tape is two-sided: at least ``range_min_flip_count``
                  VWAP crosses in the lookback AND the lookback range is
                  within ``range_max_intraday_range_pct``.
          * +0.5  nothing is trending against the fade: the per-symbol indices
                  are VWAP-neutral AND ema9/ema20 are within
                  ``range_max_ema_gap_pct``.
        """
        lookback = max(8, int(self.params.get("range_lookback_bars", 20)))
        session_frame = frame[same_day_mask(frame, sessions.now_et().date())]
        recent = session_frame.tail(lookback)
        # Same floor the builder rejects on (`insufficient_range_bars`), so a
        # frame too short to define a range cannot qualify here either.
        if len(recent) < 8:
            return 0.0

        score = 0.5
        # The builder's first gate. Checked here for the same reason the
        # prev-bar gate is: without it this is a guaranteed-fail path, not an
        # optimistic one.
        if bool(self.params.get("reject_range_during_squeeze", True)) and bool(
                getattr(tech_ctx, "bollinger_squeeze", False)):
            return score

        in_zone, range_low, range_high, threshold = self._range_entry_zone(side, recent, close)
        if not in_zone:
            return score
        if bool(self.params.get("range_require_prev_bar_confirmation", True)):
            # `recent` is at least 8 bars by the guard above, so iloc[-2] exists.
            prev_close = safe_float(recent.iloc[-2].get("close"))
            if prev_close is None or not (
                    prev_close <= threshold if side == Side.LONG else prev_close >= threshold):
                return score
        score += 2.0

        upper_wick, lower_wick, _body, bar_range = bar_wick_fractions(recent)
        if bar_range > 0:
            wick = lower_wick if side == Side.LONG else upper_wick
            if wick >= 0.30:
                score += 1.0

        span = max(0.0, range_high - range_low)
        if span > 0:
            depth = ((threshold - close) if side == Side.LONG else (close - threshold)) / span
            if depth >= float(self.params.get("range_entry_zone_frac", 0.35)) / 2.0:
                score += 0.5

        flips = -1
        if "vwap" in recent.columns:
            above = recent["close"].astype(float) > recent["vwap"].astype(float)
            flips = int((above != above.shift()).sum()) - 1
        range_pct = (range_high - range_low) / max(close, 1.0)
        if (flips >= int(self.params.get("range_min_flip_count", 3))
                and range_pct <= self._pct_param("range_max_intraday_range_pct", 0.012, vol_scale)):
            score += 0.5

        ema_gap_ok = close > 0 and abs((ema9 - ema20) / close) <= float(
            self.params.get("range_max_ema_gap_pct", 0.0008))
        if index_neutral and ema_gap_ok:
            score += 0.5
        return score

    def _build_range_signal(self, c: Candidate, side: Side, close: float, atr: float,
                            frame: pd.DataFrame, regime_score: float, data=None,
                            vol_widening: float = 1.0, vol_scale: float = 1.0) -> Signal | None:
        lookback = max(8, int(self.params.get("range_lookback_bars", 20)))
        # Scope to today's session so range_high/range_low are not polluted
        # by prior-session bars during early RTH.
        session_frame = frame[same_day_mask(frame, sessions.now_et().date())]
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
        # Via `_range_entry_zone`, the same call `_score_range` makes -- the
        # scorer and this builder must agree on where the fade zone is.
        in_zone, range_low, range_high, threshold = self._range_entry_zone(side, recent, close)
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
            prev_close = safe_float(recent.iloc[-2].get("close"))

        if side == Side.LONG:
            # Enter near range low
            if not in_zone:
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
            if not in_zone:
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
