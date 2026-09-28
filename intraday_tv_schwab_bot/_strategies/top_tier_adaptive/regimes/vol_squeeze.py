# SPDX-License-Identifier: MIT
"""The ``vol_squeeze`` regime: breakout quality, scorer and builder."""
from __future__ import annotations

from typing import Any

import pandas as pd

from ....bars import bar_close_position, same_day_mask
from ....indicators import bar_posture
from ....models import Candidate, Side, Signal
from ....numeric import safe_float
from .... import sessions


class VolSqueezeRegimeMixin:
    """Volatility squeeze: a compressed box breaking out. Mixed into
    ``TopTierAdaptiveStrategy`` (``strategy.py``), whose ``BaseStrategy``
    supplies ``params``, ``config`` and the contexts."""

    def _vol_squeeze_breakout_quality(self, side: Side, session_frame: pd.DataFrame,
                                      box: pd.DataFrame, box_high: float, box_low: float,
                                      close: float, vol_scale: float = 1.0) -> dict[str, Any]:
        """The three breakout conditions ``_build_vol_squeeze_signal`` hard-gates
        on, derived once and read by both it and ``_score_vol_squeeze``.

        They used to be derived twice, and the two copies disagreed about what
        they were FOR. The 2026-05-14 change is recorded in the preset as
        "convert vol_ratio / close_pos / buffer from +0.5 bonuses to HARD
        gates", but only the builder was changed -- the scorer kept all three
        as optional bonuses. A setup with none of them scored 2.0 box + 1.0 bb
        + 0.5 agreement + 0.5 VWAP alignment = exactly ``min_vol_squeeze_score``
        (4.0), so it cleared the floor, won its place in the build queue and
        then died on ``vol_squeeze_weak_breakout_*``: a guaranteed-fail path,
        not an optimistic one. ORB and the range regime's prev-bar gate had the
        same shape and were fixed the same way on 2026-09-22.

        Returns the verdicts plus the measured values the builder's failure
        reasons quote, and the two DECISIVE variants the scorer pays a bonus
        for now that clearing the gate is table stakes.
        """
        last = session_frame.iloc[-1]
        last_close = safe_float(last.get("close"), close)
        buffer_pct = self._pct_param("vol_squeeze_breakout_buffer_pct", 0.0008, vol_scale)
        if side == Side.LONG:
            required = box_high * (1.0 + buffer_pct)
            broke_out = last_close >= required
            decisive_break = last_close >= box_high * (1.0 + 2.0 * buffer_pct)
        else:
            required = box_low * (1.0 - buffer_pct)
            broke_out = last_close <= required
            decisive_break = last_close <= box_low * (1.0 - 2.0 * buffer_pct)
        # A box volume that does not read fails the volume gate. Until
        # 2026-09-26 an error here, and a NaN median (max(1.0, nan) is 1.0),
        # made the baseline 1 share, which any bar's volume cleared.
        vol_median = safe_float(box["volume"].median())
        cur_vol = safe_float(last.get("volume"), 0.0)
        vol_ratio = cur_vol / max(1.0, vol_median) if vol_median is not None else 0.0
        min_vol_ratio = float(self.params.get("vol_squeeze_min_breakout_volume_ratio", 1.12))
        close_pos = bar_close_position(session_frame)
        min_close_pos = float(self.params.get("vol_squeeze_min_bar_close_position", 0.63))
        return {
            "close": last_close,
            "broke_out": broke_out,
            "decisive_break": decisive_break,
            "required_clearance": required,
            "volume_ok": vol_ratio >= min_vol_ratio,
            "decisive_volume": vol_ratio >= min_vol_ratio * 1.5,
            "vol_ratio": vol_ratio,
            "min_vol_ratio": min_vol_ratio,
            "close_pos_ok": (close_pos >= min_close_pos if side == Side.LONG
                             else close_pos <= (1.0 - min_close_pos)),
            "close_pos": close_pos,
            "min_close_pos": min_close_pos,
        }

    def _score_vol_squeeze(self, side: Side, close: float, vwap: float, ema9: float,
                           ema20: float, atr: float, frame: pd.DataFrame, tech_ctx,
                           vol_scale: float = 1.0) -> float:
        """Score the Bollinger-squeeze breakout setup: compressed-range
        consolidation followed by a directional break out of the box.
        Compression comes from a tight box_range + low BB width OR an active BB
        squeeze flag on tech_ctx. Ported (lighter) from
        volatility_squeeze_breakout/strategy.py.

        Components (max 6.5):
          * +2.0  the box is compressed on range (``vol_squeeze_max_range_pct``
                  AND ``vol_squeeze_max_range_atr``).
          * +1.0  ...and on Bollinger width (the squeeze flag, or
                  ``vol_squeeze_max_width_pct``).
          * +0.5  both compression signals agree.
          * +1.5  the break clears ALL THREE of the builder's hard gates --
                  buffered break of the box edge, breakout volume, and bar
                  close position. Any one missing => compression points only,
                  which caps at 3.5 and cannot reach ``min_vol_squeeze_score``.
          * +0.5  the break is DECISIVE -- twice ``vol_squeeze_breakout_buffer_pct``
                  past the box edge rather than marginal.
          * +0.5  breakout volume is DECISIVE -- 1.5x the required ratio.
          * +0.5  aligned with VWAP/EMA (``indicators.bar_posture``, cheap
                  continuation confirmation).

        Volume and bar-close position used to be independent +0.5 bonuses here
        while ``_build_vol_squeeze_signal`` rejected outright without them, and
        the buffered break was a +1.5 bonus against a hard gate. A setup with
        none of the three scored 2.0 + 1.0 + 0.5 + 0.5 = exactly the 4.0 floor,
        qualified, and was then guaranteed to die on
        ``vol_squeeze_weak_breakout_*``. See ``_vol_squeeze_breakout_quality``,
        which both now read.

        With ``vol_squeeze_hard_breakout_gates: false`` the builder reverts to
        scoring-only on those three, so this reverts with it -- the two must
        agree under either setting, which is the whole point."""
        lookback = max(6, int(self.params.get("vol_squeeze_lookback_bars", 12)))
        session_frame = frame[same_day_mask(frame, sessions.now_et().date())]
        if len(session_frame) < lookback + 2:
            return 0.0
        # Look at the box (last lookback bars BEFORE the current one). The
        # breakout bar itself is read inside `_vol_squeeze_breakout_quality`.
        prior = session_frame.iloc[:-1]
        box = prior.tail(lookback)
        if len(box) < lookback:
            return 0.0
        box_high = safe_float(box["high"].max(), close)
        box_low = safe_float(box["low"].min(), close)
        box_range = max(0.0, box_high - box_low)
        box_range_pct = (box_range / close) if close > 0 else 0.0
        box_range_atr = (box_range / atr) if atr > 0 else float("inf")
        max_range_pct = self._pct_param("vol_squeeze_max_range_pct", 0.012, vol_scale)
        max_range_atr = float(self.params.get("vol_squeeze_max_range_atr", 1.8))
        compression_box_ok = box_range_pct <= max_range_pct and box_range_atr <= max_range_atr
        bb_squeeze_flag = bool(getattr(tech_ctx, "bollinger_squeeze", False))
        bb_width_pct = safe_float(getattr(tech_ctx, "bollinger_width_pct", None), 0.0)
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

        # The builder's three hard gates, from the one derivation both read.
        q = self._vol_squeeze_breakout_quality(side, session_frame, box, box_high,
                                               box_low, close, vol_scale)
        if bool(self.params.get("vol_squeeze_hard_breakout_gates", True)):
            # All three or nothing, exactly as `_build_vol_squeeze_signal`
            # enforces them. Compression alone caps at 3.5, below the floor.
            if not (q["broke_out"] and q["volume_ok"] and q["close_pos_ok"]):
                return score
            score += 1.5
            if q["decisive_break"]:
                score += 0.5
            if q["decisive_volume"]:
                score += 0.5
        else:
            # The flag reverts the builder to scoring-only on these three, so
            # the scorer reverts with it rather than gating on conditions
            # nothing downstream will check.
            if q["broke_out"]:
                score += 1.5
            if q["volume_ok"]:
                score += 0.5
            if q["close_pos_ok"]:
                score += 0.5

        # Alignment with VWAP/EMA (cheap continuation confirmation): the
        # shared posture test, read on the floats the entry loop resolved.
        if bar_posture({"close": close, "vwap": vwap, "ema9": ema9, "ema20": ema20}) == side:
            score += 0.5
        return score

    def _build_vol_squeeze_signal(self, c: Candidate, side: Side, close: float, atr: float,
                                  frame: pd.DataFrame, regime_score: float,
                                  data=None, vol_widening: float = 1.0, vol_scale: float = 1.0) -> Signal | None:
        """Build a Bollinger-squeeze breakout signal. Stops sit just outside
        the squeeze box (below box_low for LONG / above box_high for SHORT),
        with an ATR-floored buffer to absorb noise around the breakout. Target
        is the standard RR multiple. Shared filters (HTF bias, stretched-entry,
        SR/structure, FVG retest, etc.) run inside ``_finalize_signal``."""
        lookback = max(6, int(self.params.get("vol_squeeze_lookback_bars", 12)))
        session_frame = frame[same_day_mask(frame, sessions.now_et().date())]
        if len(session_frame) < lookback + 2:
            self._set_build_failure(c.symbol, "vol_squeeze", "insufficient_session_bars")
            return None
        prior = session_frame.iloc[:-1]
        box = prior.tail(lookback)
        if len(box) < lookback:
            self._set_build_failure(c.symbol, "vol_squeeze", "insufficient_box_bars")
            return None
        box_high = safe_float(box["high"].max(), close)
        box_low = safe_float(box["low"].min(), close)
        box_range = max(0.0, box_high - box_low)

        # Hard breakout-quality gates (2026-05-14): weak breakout volume, a
        # weak bar close, or a close that did not clear the box edge by the
        # buffer rejects outright. ``_score_vol_squeeze`` enforces the same
        # three through the same ``_vol_squeeze_breakout_quality`` call
        # (2026-09-22), so nothing it qualifies fails them here. Until then
        # it scored them as optional +0.5 bonuses and could qualify a setup
        # on compression alone.
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
        # Unlike the three above, these two are deliberately NOT mirrored in
        # the scorer. A rejection here falls through to the next qualifying
        # regime in the build queue, so scoring them would change only which
        # rejection the skip line reports, not what trades.
        #
        # Set ``vol_squeeze_hard_breakout_gates`` false to revert to
        # scoring-only behavior on the volume / close_pos / buffer gates.
        # The SR-alignment and pct_b gates can be disabled independently
        # via their own threshold params (set to 0.0 to disable).
        if bool(self.params.get("vol_squeeze_hard_breakout_gates", True)):
            # Via `_vol_squeeze_breakout_quality`, the same call `_score_vol_squeeze`
            # makes -- the scorer must not qualify what this rejects.
            q = self._vol_squeeze_breakout_quality(side, session_frame, box, box_high,
                                                   box_low, close, vol_scale)
            if not q["broke_out"]:
                op = "<" if side == Side.LONG else ">"
                self._set_build_failure(
                    c.symbol, "vol_squeeze",
                    f"{'long' if side == Side.LONG else 'short'}_vol_squeeze_weak_breakout_buffer("
                    f"close={q['close']:.4f}{op}required={q['required_clearance']:.4f})",
                )
                return None
            if not q["volume_ok"]:
                self._set_build_failure(
                    c.symbol, "vol_squeeze",
                    f"vol_squeeze_weak_breakout_volume(ratio={q['vol_ratio']:.2f}<{q['min_vol_ratio']:.2f})",
                )
                return None
            if not q["close_pos_ok"]:
                op, bound = (("<", q["min_close_pos"]) if side == Side.LONG
                             else (">", 1.0 - q["min_close_pos"]))
                self._set_build_failure(
                    c.symbol, "vol_squeeze",
                    f"{'long' if side == Side.LONG else 'short'}_vol_squeeze_weak_bar_close("
                    f"pos={q['close_pos']:.2f}{op}{bound:.2f})",
                )
                return None

        # Setup-quality gates that proved to separate winners from losers
        # in the 2026-05-14 session. Independent of hard_breakout_gates
        # switch — set the threshold params to 0.0 to disable each.
        sr_alignment_threshold = float(self.params.get("vol_squeeze_min_sr_bias_alignment", 0.20))
        if sr_alignment_threshold > 0.0:
            sr_ctx = self._sr_context(c.symbol, frame, data)
            sr_bias_score = safe_float(getattr(sr_ctx, "bias_score", None), 0.0)
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
            pct_b = safe_float(getattr(tech_ctx, "bollinger_percent_b", None), 0.5)
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
