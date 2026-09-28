# SPDX-License-Identifier: MIT
"""The ``orb`` regime (opening range breakout): the opening range, scorer and
builder."""
from __future__ import annotations

import pandas as pd

from ....bars import opening_range
from ....models import Candidate, Side, Signal
from ....sessions import EQUITY_RTH_OPEN
from .... import sessions


class OrbRegimeMixin:
    """Opening range breakout: a buffered break of today's opening range.
    Mixed into ``TopTierAdaptiveStrategy`` (``strategy.py``), whose
    ``BaseStrategy`` supplies ``params``, ``config`` and the contexts."""

    def _opening_range(self, frame: pd.DataFrame) -> tuple[float, float, int] | None:
        """Today's opening range, ``(high, low, bars)`` over the first
        ``orb_range_minutes`` of RTH: [09:30, ``_orb_range_end()``). None
        when the window has no bars yet (pre-open / range not formed) or no
        price in them. Computed on the raw (1m) frame for true extremes;
        pre-market bars are excluded by the 09:30 start so the range is
        RTH-anchored even in extended-hours mode."""
        # Via `_orb_range_minutes` (schedule.py), the clamp `_orb_range_end`
        # reads too. The same derivation used to appear here, in
        # `_orb_range_end` and in `_allowed_regimes`; three copies of one rule
        # is how the bot ends up forming the range over one span and opening
        # the window against another.
        return opening_range(frame, sessions.now_et().date(), start=EQUITY_RTH_OPEN,
                             minutes=self._orb_range_minutes(), min_bars=1)

    def _score_orb(self, side: Side, close: float, atr: float, frame: pd.DataFrame) -> float:
        """Score the Opening Range Breakout. Fires only on a genuine break of
        today's opening range (above the high for LONG, below the low for
        SHORT). The hard range-validity + measured-move geometry runs at
        build time in ``_build_orb_signal``.

        Components (max 5.0):
          * +0.5  base (regime in play)
          * +2.5  price has broken the opening range on the trade side BY AT
                  LEAST ``orb_breakout_buffer_atr_mult`` x ATR -- the same
                  condition ``_build_orb_signal`` enforces. No break => base
                  only (the bot waits for the break, it does not trade inside
                  the range).
          * +1.0  breakout conviction — the break clears the edge by TWICE
                  that buffer, i.e. decisively rather than marginally.
          * +1.0  range is a sane, tradeable size (>= orb_min_range_atr_mult
                  ATR and, when capped, <= orb_max_range_atr_mult ATR).

        The +2.5 used to be awarded for a BARE break, with clearing the buffer
        as the optional +1.0 -- while the builder rejected anything that did
        not clear it. A one-tick poke therefore scored 4.0, beat the 3.5 floor,
        won its place in the build queue and then died on
        ``orb_no_break_above``: a guaranteed-fail path, not merely an
        optimistic one. `vol_squeeze` had the same shape and was fixed the
        same way on 2026-05-14, by making the scoring bonuses hard gates.
        """
        if atr <= 0 or close <= 0:
            return 0.0
        opening = self._opening_range(frame)
        if opening is None or opening[0] <= opening[1]:
            return 0.5
        or_high, or_low, _ = opening
        score = 0.5
        buffer = float(self.params.get("orb_breakout_buffer_atr_mult", 0.05)) * atr
        broke = (close > or_high + buffer if side == Side.LONG
                 else close < or_low - buffer)
        if not broke:
            return score
        score += 2.5
        decisive = (close > or_high + 2.0 * buffer if side == Side.LONG
                    else close < or_low - 2.0 * buffer)
        if decisive:
            score += 1.0
        range_height = or_high - or_low
        min_range = float(self.params.get("orb_min_range_atr_mult", 0.5)) * atr
        max_range_mult = float(self.params.get("orb_max_range_atr_mult", 4.0))
        max_range = max_range_mult * atr if max_range_mult > 0 else float("inf")
        if min_range <= range_height <= max_range:
            score += 1.0
        return score

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
        entry stage (``_finalize_signal`` -> ``entry_policy.admit``) first
        refuses a measured move that no longer clears the min R:R floor, then
        caps the target to nearby HTF levels. Range size is sanity-bounded so
        noise ranges (too tight) and untradeable ranges (too wide) are
        skipped."""
        opening = self._opening_range(frame)
        if opening is None or opening[0] <= opening[1]:
            self._set_build_failure(c.symbol, "orb", "orb_no_opening_range")
            return None
        or_high, or_low, _ = opening
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
        # way sr_scalp rejects when its zone gap cannot pay for its stop. The
        # check is the shared entry stage's raw R:R gate (2026-09-24: it was a
        # builder-local min_target_rr read), refused as
        # `<side>_orb_measured_move_exhausted(...)` on the RAW levels before
        # any refinement could move them.
        return self._finalize_signal(c, side, close, stop, target, "orb", regime_score, frame, data,
                                     vol_scale=vol_scale, raw_rr_gate="orb_measured_move_exhausted")
