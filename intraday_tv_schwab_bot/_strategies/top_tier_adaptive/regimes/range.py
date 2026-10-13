# SPDX-License-Identifier: MIT
"""The ``range`` regime: the fade zone, scorer and builder."""
from __future__ import annotations

import pandas as pd

from ....bars import bar_wick_fractions
from ....models import Candidate, Side, Signal
from ....numeric import safe_float
from ..confirmation import session_bars, setup_window, stop_anchor, today_bars

# The fewest buckets a range is measured on (the zone, ``setup_window``):
# 40 minutes of 5m buckets.
RANGE_MIN_ZONE_BARS = 8


class RangeRegimeMixin:
    """Range: fade the edge of the lookback range. Mixed into
    ``TopTierAdaptiveStrategy`` (``strategy.py``), whose ``BaseStrategy``
    supplies ``params``, ``config`` and the contexts.

    The range is set up on the LTF (2026-10-13): the zone is the last
    ``range_lookback_bars`` of today's closed buckets before the current one
    (``setup_window``; 20 x 5m = 100 minutes on the shipped preset). The
    entry is timed on the step frame's closed 1m bars (``_read_candidate``
    drops one still trading): the latest 1m close must be in the fade zone,
    the bar before it too under ``range_require_prev_bar_confirmation``,
    and the rejection wick is the last 1m bar's. Until then the zone was the
    last 20 one-minute bars, the current bar among them."""

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

        The zone ends at the range's own edge (2026-10-13): a close below
        ``range_low`` (LONG; above ``range_high`` for a SHORT) has broken the
        range rather than reached its edge, and is no fade. With the range
        measured on the buckets before the current one, the close can lie
        outside it; while the range held the current bar it could not.
        """
        range_high = safe_float(recent["high"].max(), close)
        range_low = safe_float(recent["low"].min(), close)
        span = max(0.0, range_high - range_low)
        frac = float(self.params.get("range_entry_zone_frac", 0.35))
        if side == Side.LONG:
            threshold = range_low + span * frac
            return range_low <= close <= threshold, range_low, range_high, threshold
        threshold = range_high - span * frac
        return threshold <= close <= range_high, range_low, range_high, threshold

    def _range_prev_close(self, frame: pd.DataFrame) -> float | None:
        """The close of the 1m bar before the trigger bar (today's bars,
        ``today_bars``), which ``range_require_prev_bar_confirmation`` asks to
        sit in the fade zone too; None when there is none or it does not read.

        The pass's step frame holds closed bars only (``_read_candidate``
        drops a last bar still trading), so its last bar is the last closed
        one and this is the one before it: the reading asks for the last TWO
        closed 1m bars in the zone. The 2026-04-23 design wrote it against a
        forming last bar, where it asked for the last closed bar alone, the
        one the zone test already reads; that reading is
        ``range_require_prev_bar_confirmation: false``. The default keeps the
        two-bar reading (the user's call, 2026-10-13: a replay A/B of 10-07
        and 10-08 rested on one stop, not enough to choose between them)."""
        day = today_bars(frame)
        if day is None or len(day) < 2:
            return None
        return safe_float(day.iloc[-2].get("close"))

    def _score_range(self, side: Side, close: float, ema9: float, ema20: float,
                     ltf: pd.DataFrame, frame: pd.DataFrame, tech_ctx, index_neutral: bool,
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
                  1m bar before it (``_range_prev_close``) -- both of the
                  builder's entry gates, enforced together. Either missing
                  => base only, which cannot reach ``min_range_score``. The
                  close must also be inside the range (a close past its edge
                  has broken it, ``_range_entry_zone``). The prev-bar half used to
                  be an optional +0.5 while the builder demanded it, so a
                  single-bar poke into the zone scored 3.5, cleared the floor,
                  won its place in the build queue and then died on
                  ``not_near_range_low_prev_bar``: a guaranteed-fail path, not
                  an optimistic one. ORB had the same shape, fixed the same
                  way two days ago.
          * +1.0  the last 1m bar rejected the edge (LONG: lower wick >= 0.30
                  of bar range; SHORT: upper wick >= 0.30).
          * +0.5  the fade is DEEP rather than marginal -- close is in the
                  inner half of the zone, nearer the extreme than the
                  threshold.
          * +0.5  the tape is two-sided: at least ``range_min_flip_count``
                  closes across VWAP (each bucket's close against the step
                  frame's session VWAP at it) in the zone's session buckets
                  (``session_bars``: a premarket bucket's VWAP is the
                  all-hours one, so its close is no flip, and neither is
                  the switch to the session VWAP at the open; 2026-10-13)
                  AND the zone's range is within
                  ``range_max_intraday_range_pct``.
          * +0.5  nothing is trending against the fade: the per-symbol indices
                  are VWAP-neutral AND ema9/ema20 are within
                  ``range_max_ema_gap_pct``.
        """
        lookback = max(RANGE_MIN_ZONE_BARS, int(self.params.get("range_lookback_bars", 20)))
        recent = setup_window(ltf, lookback)
        # Same floor the builder rejects on (`insufficient_range_bars`), so a
        # frame too short to define a range cannot qualify here either.
        if len(recent) < RANGE_MIN_ZONE_BARS:
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
            prev_close = self._range_prev_close(frame)
            if prev_close is None or not (
                    prev_close <= threshold if side == Side.LONG else prev_close >= threshold):
                return score
        score += 2.0

        upper_wick, lower_wick, _body, bar_range = bar_wick_fractions(frame)
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
            session = session_bars(recent)
            above = session["close"].astype(float) > session["vwap"].astype(float)
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
                            ltf: pd.DataFrame, frame: pd.DataFrame, regime_score: float, data=None,
                            vol_widening: float = 1.0, vol_scale: float = 1.0) -> Signal | None:
        """Fade the range edge: LONG in the bottom ``range_entry_zone_frac``
        of the zone (``setup_window`` of ``range_lookback_bars`` of today's
        LTF buckets), SHORT in the top. The stop goes beyond the range edge by
        ``stop_buffer_atr_mult`` x the LTF ATR, and beyond every 1m bar of
        the current bucket (``stop_anchor``: a spring through the edge that
        closed back inside is under it), and at least
        ``range_min_stop_atr_mult`` x the LTF ATR from the close: no
        floorless stop (2026-10-13; the stop sat range_low - buffer, which a
        close at the edge put a buffer away). The target is the far edge less
        the buffer."""
        lookback = max(RANGE_MIN_ZONE_BARS, int(self.params.get("range_lookback_bars", 20)))
        # Today's buckets only, so range_high/range_low are not polluted by
        # prior-session bars during early RTH.
        recent = setup_window(ltf, lookback)
        if len(recent) < RANGE_MIN_ZONE_BARS:
            self._set_build_failure(c.symbol, "range",
                                    f"insufficient_range_bars({len(recent)}<{RANGE_MIN_ZONE_BARS})")
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
        # When enabled, the 1m bar before the trigger bar must also close in
        # the entry zone (``_range_prev_close``: on closed bars that asks for
        # two closed bars).
        require_prev_bar = bool(self.params.get("range_require_prev_bar_confirmation", True))
        prev_close = self._range_prev_close(frame) if require_prev_bar else None
        prev_text = "na" if prev_close is None else f"{prev_close:.4f}"
        # A floor on the stop's distance from the close, in LTF ATR.
        min_stop = float(self.params.get("range_min_stop_atr_mult", 0.5)) * atr

        if side == Side.LONG:
            # Enter near range low; a close under it has broken the range.
            if close < range_low:
                self._set_build_failure(
                    c.symbol, "range",
                    f"below_range_low(close={close:.4f}<{range_low:.4f},range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            if not in_zone:
                self._set_build_failure(
                    c.symbol, "range",
                    f"not_near_range_low(close={close:.4f}>{threshold:.4f},range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            if require_prev_bar and (prev_close is None or prev_close > threshold):
                self._set_build_failure(
                    c.symbol, "range",
                    f"not_near_range_low_prev_bar(prev_close={prev_text}>{threshold:.4f},"
                    f"range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            stop = min(stop_anchor(side, recent, frame, ltf, close) - buffer, close - min_stop)
            target = range_high - buffer
        else:
            # Enter near range high; a close over it has broken the range.
            if close > range_high:
                self._set_build_failure(
                    c.symbol, "range",
                    f"above_range_high(close={close:.4f}>{range_high:.4f},range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            if not in_zone:
                self._set_build_failure(
                    c.symbol, "range",
                    f"not_near_range_high(close={close:.4f}<{threshold:.4f},range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            if require_prev_bar and (prev_close is None or prev_close < threshold):
                self._set_build_failure(
                    c.symbol, "range",
                    f"not_near_range_high_prev_bar(prev_close={prev_text}<{threshold:.4f},"
                    f"range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            stop = max(stop_anchor(side, recent, frame, ltf, close) + buffer, close + min_stop)
            target = max(0.01, range_low + buffer)

        return self._finalize_signal(c, side, close, stop, target, "range", regime_score, frame, data, vol_scale=vol_scale)
