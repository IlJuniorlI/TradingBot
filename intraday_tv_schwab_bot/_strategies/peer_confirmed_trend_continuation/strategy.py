# SPDX-License-Identifier: MIT
from typing import Any, ClassVar

import pandas as pd

from ...htf_levels import HTFContext
from ...models import Candidate, Position, Side, Signal
from ...numeric import safe_float
from ...reasons import insufficient_bars_reason, reason_with_values
from ...bars import bar_close_position
from ...indicators import last_bar_atr
from ..peer_confirmed_key_levels.strategy import (
    FAMILY_KNOBS,
    LTF_BUCKET_MAX_LATE,
    Knob,
    PeerConfirmedKeyLevelsStrategy,
    _BucketVerdict,
    _gate_snapshot,
    _score_threshold,
)
from ..shared_entry import EntryContexts, EntryProposal
from ... import sessions


# trend_continuation's own knobs checked at load (2026-11-08: the pullback
# detector's and the extension band's; the rest of its own knobs are still
# read unchecked, the whole-bot review's R-079).
TREND_CONTINUATION_KNOBS: dict[str, Knob] = {
    "min_pullback_bars": Knob("count", low=1),
    "max_pullback_bars": Knob("count", low=1),
    "min_pullback_depth_atr": Knob("number", low=0),
    "max_pullback_depth_atr": Knob("number", low=0, low_open=True),
    "pullback_hold_atr": Knob("number", low=0),
    "max_countertrend_volume_ratio": Knob("number", low=0, low_open=True),
    "max_extension_from_vwap_atr": Knob("number", low=0, low_open=True),
    "max_extension_from_ema9_atr": Knob("number", low=0, low_open=True),
    "extension_penalty_per_atr": Knob("number", low=0),
    "extension_hard_cap_mult": Knob("number", low=1),
    # The base exhaustion filter's reads (``BaseStrategy._entry_exhaustion_reasons``),
    # declared so the band's penalty is live (R-070).
    "entry_exhaustion_filter_enabled": Knob("bool"),
    "max_entry_vwap_extension_atr": Knob("number", low=0, low_open=True),
    "max_entry_ema9_extension_atr": Knob("number", low=0, low_open=True),
}


class PeerConfirmedTrendContinuationStrategy(PeerConfirmedKeyLevelsStrategy):

    @classmethod
    def _own_knob_errors(cls, params) -> list[str]:
        """The pullback's bounds ordered, and the base exhaustion filter no
        tighter than the strategy's extension band: the filter refuses
        ``too_extended_from_*_atr`` at its own threshold, so with it under
        the band the penalty between the band and the hard cap never
        reached a decision (R-070, 2026-11-08: the undeclared defaults 0.95 /
        0.75 sat under the band's 1.05 / 0.88)."""
        name = cls.strategy_name
        errors: list[str] = []
        if params["max_pullback_bars"] < params["min_pullback_bars"]:
            errors.append(f"strategies.{name}.params.max_pullback_bars ({params['max_pullback_bars']}) must be at least "
                          f"min_pullback_bars ({params['min_pullback_bars']})")
        if params["max_pullback_depth_atr"] <= params["min_pullback_depth_atr"]:
            errors.append(f"strategies.{name}.params.max_pullback_depth_atr ({params['max_pullback_depth_atr']}) must be "
                          f"above min_pullback_depth_atr ({params['min_pullback_depth_atr']})")
        for base_key, band_key in (("max_entry_vwap_extension_atr", "max_extension_from_vwap_atr"),
                                   ("max_entry_ema9_extension_atr", "max_extension_from_ema9_atr")):
            if params["entry_exhaustion_filter_enabled"] and params[base_key] < params[band_key]:
                errors.append(f"strategies.{name}.params.{base_key} ({params[base_key]}) must be at least {band_key} "
                              f"({params[band_key]}): the base exhaustion filter would refuse before the extension "
                              f"penalty could count")
        return errors

    strategy_name = 'peer_confirmed_trend_continuation'
    # The family's knobs, checked at load and at the build
    # (``PeerConfirmedKeyLevelsStrategy.check_knobs``); this strategy's own
    # trend / pullback knobs are not checked yet.
    KNOBS: ClassVar[dict[str, Knob]] = {**FAMILY_KNOBS, **TREND_CONTINUATION_KNOBS}

    def dashboard_overlay_candidates(self, side: Side, close: float, ltf: pd.DataFrame, htf: HTFContext) -> list[dict[str, Any]] | None:
        """The trigger and the pullback anchor the entry pass would read, on
        the dashboard's LTF cut to its completed buckets (``_completed_ltf``
        on the ET clock, as key_levels' ``dashboard_select_level``; the
        dashboard hands no step frame). None drawn without a pullback."""
        ltf = self._completed_ltf(ltf, int(self.params.get("ltf_minutes", 5)), sessions.now_et())
        if ltf is None or ltf.empty:
            return []
        trend = self._trend_signal(side, ltf, htf)
        trigger = self._pullback_trigger_signal(side, ltf, close=trend["close"], ema9=trend["ema9"], ema20=trend["ema20"], atr=trend["atr"])
        trigger_level = safe_float(trigger.get("trigger_level"), 0.0)
        pullback_extreme = safe_float(trigger.get("pullback_extreme"), 0.0)
        if trigger_level <= 0 or pullback_extreme <= 0:
            return []
        ltf_score = float(trigger.get("score", 0.0) or 0.0)
        trend_score = float(trend.get("score", 0.0) or 0.0)
        trigger_kind = "bullish_continuation_trigger" if side == Side.LONG else "bearish_continuation_trigger"
        anchor_kind = "bullish_pullback_anchor" if side == Side.LONG else "bearish_pullback_anchor"
        return [
            {
                "kind": trigger_kind,
                "price": float(trigger_level),
                "touches": 1,
                "level_score": round(trend_score + ltf_score, 4),
                "source_priority": 2.25,
            },
            {
                "kind": anchor_kind,
                "price": float(pullback_extreme),
                "touches": 1,
                "level_score": round(max(ltf_score - 0.5, 0.0), 4),
                "source_priority": 1.5,
            },
        ]

    def dashboard_candidate_levels(self, close: float, htf: HTFContext, side: Side) -> list[dict[str, Any]]:
        return []

    def dashboard_select_level(self, side: Side, close: float, ltf: pd.DataFrame, htf: HTFContext) -> dict[str, Any] | None:
        candidates = self.dashboard_overlay_candidates(side, close, ltf, htf) or []
        for candidate in candidates:
            if str(candidate.get("kind") or "").endswith("continuation_trigger"):
                return candidate
        return candidates[0] if candidates else None

    def dashboard_zone_width_for_level(
        self,
        side: Side,
        close: float,
        atr: float,
        level_price: float,
        htf: HTFContext,
        candidate: dict[str, Any] | None = None,
    ) -> float | None:
        capability_width = super().dashboard_zone_width_for_level(side, close, atr, level_price, htf, candidate)
        if capability_width is not None:
            return capability_width
        kind = str((candidate or {}).get("kind") or "")
        min_zone = max(float(close) * 0.0006, 0.01)
        if "continuation_trigger" in kind:
            return max(float(atr) * 0.20, min_zone)
        if "pullback_anchor" in kind:
            return max(float(atr) * 0.32, min_zone)
        return max(float(atr) * 0.24, min_zone)

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        min_bars = int(self.params.get("min_bars", 85))
        ltf_min = max(1, int(self.params.get("ltf_minutes", 5)))
        min_ltf_bars = int(self.params.get("min_ltf_bars", 18))
        max_pullback_bars = int(self.params.get("max_pullback_bars", 5))
        return max(min_bars, ltf_min * (min_ltf_bars + max_pullback_bars + 4))

    def _trend_signal(self, side: Side, ltf: pd.DataFrame, htf: HTFContext) -> dict[str, Any]:
        last = ltf.iloc[-1]
        close = safe_float(last.get("close"), 0.0)
        vwap = safe_float(last.get("vwap"), close)
        ema9 = safe_float(last.get("ema9"), close)
        ema20 = safe_float(last.get("ema20"), close)
        atr = last_bar_atr(ltf, close, floor_pct=0.0005)
        adx = safe_float(last.get("adx14"), 0.0)
        htf_bias, bull_votes, bear_votes = self._htf_bias(htf, close)
        min_adx = float(self.params.get("min_adx14", 13.5))
        score = 0.0
        reasons: list[str] = []
        if side == Side.LONG:
            if close > vwap:
                score += 1.0
            else:
                reasons.append(reason_with_values("below_vwap", current=close, required=vwap, op=">", digits=4))
            if ema9 >= ema20:
                score += 1.0
            else:
                reasons.append(reason_with_values("ema9_below_ema20", current=ema9, required=ema20, op=">=", digits=4))
            if close >= ema9:
                score += 1.0
            else:
                reasons.append(reason_with_values("below_ema9", current=close, required=ema9, op=">=", digits=4))
            if htf_bias == "bullish":
                score += 1.0 + (0.25 if bull_votes >= max(2, bear_votes + 1) else 0.0)
            else:
                reasons.append(f"htf_bias_{htf_bias}")
        else:
            if close < vwap:
                score += 1.0
            else:
                reasons.append(reason_with_values("above_vwap", current=close, required=vwap, op="<", digits=4))
            if ema9 <= ema20:
                score += 1.0
            else:
                reasons.append(reason_with_values("ema9_above_ema20", current=ema9, required=ema20, op="<=", digits=4))
            if close <= ema9:
                score += 1.0
            else:
                reasons.append(reason_with_values("above_ema9", current=close, required=ema9, op="<=", digits=4))
            if htf_bias == "bearish":
                score += 1.0 + (0.25 if bear_votes >= max(2, bull_votes + 1) else 0.0)
            else:
                reasons.append(f"htf_bias_{htf_bias}")
        if adx >= min_adx:
            score += 1.0
        else:
            reasons.append(reason_with_values("weak_adx", current=adx, required=min_adx, op=">=", digits=4))
        return {
            "score": float(score),
            "reasons": reasons,
            "close": close,
            "vwap": vwap,
            "ema9": ema9,
            "ema20": ema20,
            "atr": atr,
            "adx": adx,
            "htf_bias": htf_bias,
            "htf_bull_votes": bull_votes,
            "htf_bear_votes": bear_votes,
        }

    def _pullback_trigger_signal(self, side: Side, ltf: pd.DataFrame, *, close: float, ema9: float, ema20: float, atr: float) -> dict[str, Any]:
        """The pullback and its re-expansion on ``ltf``'s last bar (the entry
        pass hands the completed buckets), for a LONG (a SHORT mirrored):

        - the impulse's extreme is the highest high among the last
          ``max_pullback_bars`` + 1 completed bars before the trigger bar;
          the bars after it are the pullback, ``min_pullback_bars`` to
          ``max_pullback_bars`` of them -- fewer, and there is no pullback
          (``no_pullback``: score 0, so ``weak_ltf_score`` refuses the
          side; a straight grind to new highs puts the extreme on the last
          bar);
        - its depth, the extreme less the pullback's low in ATR, within
          [``min_pullback_depth_atr``, ``max_pullback_depth_atr``] +1
          (``pullback_too_shallow_atr`` / ``pullback_too_deep_atr`` withhold
          the point); the low holding EMA20 less ``pullback_hold_atr`` ATR
          +1; the trigger bar closing over the pullback's highs by
          ``breakout_buffer_pct`` +1 (``no_reexpansion_trigger``);
        - the bar's own quality: EMA9 reclaimed 0.75, a bullish body 0.5,
          the close position 0.5, the candle model's tier up to 0.6, the
          trigger's volume over the prior bars' mean 0.5, and the
          pullback's mean volume no more than ``max_countertrend_volume_ratio``
          x the impulse's (the same count of bars ending at the extreme)
          0.5 (``heavy_countertrend_volume``).

        The stop anchor is the pullback's low. Until 2026-11-08 (the
        whole-bot review's R-013, R-069) the "pullback" was the last
        ``max_pullback_bars`` bars' low against a 3-bar high, with no swing
        and no bar count, so a 6-bar grind to new highs scored the full
        pullback and a fast impulse-then-pullback read as too deep with the
        stop at the impulse's origin; and the countertrend ratio was a
        6-bar mean over a 7-bar mean containing it, at most 1.17, so the
        1.28 knob never fired and its 0.5 was a constant."""
        max_pullback_bars = int(self.params["max_pullback_bars"])
        min_pullback_bars = int(self.params["min_pullback_bars"])
        ltf_window = max(2, int(self.params.get("min_ltf_bars", 18)))
        breakout_buffer_pct = max(0.0, float(self.params.get("breakout_buffer_pct", 0.0008)))
        min_close_pos = min(0.95, max(0.05, float(self.params.get("min_ltf_close_position", 0.58))))
        min_trigger_vol = max(0.1, float(self.params.get("min_ltf_volume_ratio", 1.02)))
        max_countertrend_vol = float(self.params["max_countertrend_volume_ratio"])
        min_pullback_depth_atr = float(self.params["min_pullback_depth_atr"])
        max_pullback_depth_atr = float(self.params["max_pullback_depth_atr"])
        pullback_hold_atr = float(self.params["pullback_hold_atr"])
        long = side == Side.LONG

        def _refused(reason: str) -> dict[str, Any]:
            # No pullback: no trigger level and no extreme to draw (the
            # dashboard overlay leaves them out); the stop anchor falls back
            # to the close, which the builder never reaches (score 0).
            return {"score": 0.0, "reasons": [reason], "trigger_level": None, "pullback_extreme": None,
                    "pullback_depth_atr": 0.0, "pullback_bars": 0, "close_position": 0.0, "trigger_volume_ratio": 0.0,
                    "countertrend_volume_ratio": 0.0, "stop_anchor": float(close)}

        recent = ltf.tail(max(ltf_window, max_pullback_bars * 2 + 2))
        prior = recent.iloc[:-1]
        if len(prior) <= min_pullback_bars:
            return _refused("no_pullback_context")
        # The search holds one bar more than the longest pullback: an extreme
        # on its first bar means the pullback runs longer than
        # max_pullback_bars (or the swing lies farther back still), so the
        # duration is bounded both ways (R-013's verifier, 2026-11-08: with
        # a window of max + 1 bars, 24% of the detected pullbacks on H: were
        # a longer decline read from inside).
        search = prior.tail(max_pullback_bars + 2)
        extreme_values = search["high" if long else "low"].to_numpy(dtype=float)
        swing_pos = int(extreme_values.argmax() if long else extreme_values.argmin())
        swing_extreme = float(extreme_values[swing_pos])
        pullback = search.iloc[swing_pos + 1:]
        pullback_bars = len(pullback)
        if len(search) > max_pullback_bars + 1 and swing_pos == 0:
            return _refused(reason_with_values("pullback_too_long", current=pullback_bars, required=max_pullback_bars, op="<=", digits=0))
        if pullback_bars < min_pullback_bars:
            return _refused(reason_with_values("no_pullback", current=pullback_bars, required=min_pullback_bars, op=">=", digits=0))
        # The impulse: the same count of bars, ending at the extreme.
        impulse = prior.iloc[:len(prior) - pullback_bars].tail(max(1, pullback_bars))
        close_pos = bar_close_position(ltf)
        last_vol = safe_float(ltf.iloc[-1].get("volume"), 0.0)
        avg_vol = max(safe_float(prior["volume"].tail(max(3, max_pullback_bars)).mean(), 0.0), 1.0)
        volume_ratio = last_vol / avg_vol if avg_vol > 0 else 0.0
        pullback_countertrend_volume_ratio = (max(safe_float(pullback["volume"].mean(), 0.0), 0.0)
                                              / max(safe_float(impulse["volume"].mean(), 0.0), 1.0))
        score = 0.0
        reasons: list[str] = []
        if long:
            trigger_level = safe_float(pullback["high"].max(), close)
            pullback_extreme = safe_float(pullback["low"].min(), close)
            pullback_depth_atr = max(0.0, swing_extreme - pullback_extreme) / atr if atr > 0 else 0.0
            held = pullback_extreme >= (ema20 - (atr * pullback_hold_atr))
            hold_level = ema20 - (atr * pullback_hold_atr)
            reexpanded = close > trigger_level * (1.0 + breakout_buffer_pct)
            reexpansion_level = trigger_level * (1.0 + breakout_buffer_pct)
            ema9_ok, ema9_reason = close >= ema9, "failed_ema9_reclaim"
            body_ok, body_reason = safe_float(ltf.iloc[-1].get("close"), close) >= safe_float(ltf.iloc[-1].get("open"), close), "trigger_bar_not_bullish"
            close_ok = close_pos >= min_close_pos
        else:
            trigger_level = safe_float(pullback["low"].min(), close)
            pullback_extreme = safe_float(pullback["high"].max(), close)
            pullback_depth_atr = max(0.0, pullback_extreme - swing_extreme) / atr if atr > 0 else 0.0
            held = pullback_extreme <= (ema20 + (atr * pullback_hold_atr))
            hold_level = ema20 + (atr * pullback_hold_atr)
            reexpanded = close < trigger_level * (1.0 - breakout_buffer_pct)
            reexpansion_level = trigger_level * (1.0 - breakout_buffer_pct)
            ema9_ok, ema9_reason = close <= ema9, "failed_ema9_reject"
            body_ok, body_reason = safe_float(ltf.iloc[-1].get("close"), close) <= safe_float(ltf.iloc[-1].get("open"), close), "trigger_bar_not_bearish"
            close_ok = close_pos <= (1.0 - min_close_pos)
        if pullback_depth_atr < min_pullback_depth_atr:
            reasons.append(reason_with_values("pullback_too_shallow_atr", current=pullback_depth_atr, required=min_pullback_depth_atr, op=">=", digits=4))
        elif pullback_depth_atr > max_pullback_depth_atr:
            reasons.append(reason_with_values("pullback_too_deep_atr", current=pullback_depth_atr, required=max_pullback_depth_atr, op="<=", digits=4))
        else:
            score += 1.0
        if held:
            score += 1.0
        else:
            reasons.append(reason_with_values("pullback_lost_ema20", current=pullback_extreme, required=hold_level, op=">=" if long else "<=", digits=4))
        if reexpanded:
            score += 1.0
        else:
            reasons.append(reason_with_values("no_reexpansion_trigger", current=close, required=reexpansion_level, op=">" if long else "<", digits=4))
        if ema9_ok:
            score += 0.75
        else:
            reasons.append(reason_with_values(ema9_reason, current=close, required=ema9, op=">=" if long else "<=", digits=4))
        if body_ok:
            score += 0.5
        else:
            reasons.append(body_reason)
        if close_ok:
            score += 0.5
        else:
            reasons.append(reason_with_values("weak_bar_close", current=close_pos, required=min_close_pos if long else 1.0 - min_close_pos, op=">=" if long else "<=", digits=4))
        candle_summary = self._configured_ltf_candle_summary(side, ltf)
        candle_tier = str(candle_summary.get("confirm_tier", "none") or "none")
        score += {"strong_3c": 0.60, "solid_2c": 0.40, "weak_1c": 0.20}.get(candle_tier, 0.0)
        if volume_ratio >= min_trigger_vol:
            score += 0.5
        else:
            reasons.append(reason_with_values("weak_trigger_volume", current=volume_ratio, required=min_trigger_vol, op=">=", digits=4))
        if pullback_countertrend_volume_ratio <= max_countertrend_vol:
            score += 0.5
        else:
            reasons.append(reason_with_values("heavy_countertrend_volume", current=pullback_countertrend_volume_ratio, required=max_countertrend_vol, op="<=", digits=4))
        return {
            "score": float(score),
            "reasons": reasons,
            "trigger_level": float(trigger_level),
            "pullback_extreme": float(pullback_extreme),
            "pullback_depth_atr": float(pullback_depth_atr),
            "pullback_bars": int(pullback_bars),
            "swing_extreme": float(swing_extreme),
            "close_position": float(close_pos),
            "trigger_volume_ratio": float(volume_ratio),
            "countertrend_volume_ratio": float(pullback_countertrend_volume_ratio),
            "stop_anchor": float(pullback_extreme),
        }

    def _build_continuation_signal(self, c: Candidate, frame: pd.DataFrame, ltf: pd.DataFrame, side: Side, peer_ctx: dict[str, Any], macro_ctx: dict[str, Any], data=None, *, sr_ctx=None, ms_ctx=None, tech_ctx=None) -> Signal | None:
        failure_style = self._failure_style_name(side)
        close = safe_float(ltf.iloc[-1].get("close"), 0.0)
        if close <= 0:
            return None
        htf = self._htf_context(c.symbol, data, current_price=close, **self._symbol_htf_request())
        trend = self._trend_signal(side, ltf, htf)
        trigger = self._pullback_trigger_signal(side, ltf, close=trend["close"], ema9=trend["ema9"], ema20=trend["ema20"], atr=trend["atr"])
        # This setup's own blockers. They go to the shared entry stage with
        # the proposal, which refuses on them and on every switched-on veto
        # at once (the S/R veto: shared_entry.use_sr_filter, which replaced
        # params.use_sr_veto on 2026-09-24).
        pending_reasons: list[str] = []
        diagnostics: list[str] = []
        total_score = float(trend.get("score", 0.0) or 0.0)
        total_score += float(trigger.get("score", 0.0) or 0.0)
        peer_score = int(peer_ctx.get("score", 0) or 0)
        # Every peer with no fresh vote counts against the side (the family's
        # ``_directional_peer_score``, 2026-10-13).
        directional_peer_score = self._directional_peer_score(side, peer_ctx)
        # Parenthesize the side-selection so the `or 0` fallback applies to BOTH
        # branches (the ternary + `or 0` was an operator-precedence gotcha: the
        # LONG branch was using `int(bullish)` with no fallback while the SHORT
        # branch got `int(bearish or 0)`, so a None/missing bullish count would
        # crash the LONG path with TypeError instead of degrading to zero).
        peer_agreement_raw = peer_ctx.get("bullish", 0) if side == Side.LONG else peer_ctx.get("bearish", 0)
        peer_agreement = int(peer_agreement_raw or 0)
        min_peer_score = int(self.params.get("min_peer_score", 2))
        min_peer_agreement = int(self.params.get("min_peer_agreement", 2))
        peer_bonus = 0.0
        if directional_peer_score >= min_peer_score:
            peer_bonus = min(2.0, 0.5 + (0.35 * peer_agreement))
            total_score += peer_bonus
        else:
            diagnostics.append(reason_with_values("weak_peer_score", current=directional_peer_score, required=min_peer_score, op=">=", digits=2))
        if peer_agreement < min_peer_agreement:
            pending_reasons.append(reason_with_values("weak_peer_agreement", current=peer_agreement, required=min_peer_agreement, op=">=", digits=2))

        macro_bonus = max(0.0, float(self.params.get("macro_bonus", 0.70)))
        macro_miss_penalty = max(0.0, float(self.params.get("macro_miss_penalty", 0.30)))
        macro_aligned = self._macro_allows(side, macro_ctx)
        if macro_aligned:
            total_score += macro_bonus
        else:
            total_score -= macro_miss_penalty
            diagnostics.append("macro_not_aligned")
        diagnostics.extend([r for r in trend.get("reasons", []) if r not in diagnostics])
        diagnostics.extend([r for r in trigger.get("reasons", []) if r not in diagnostics])

        min_total = _score_threshold(self.params.get("min_total_score", 5.5), 5.5, minimum=1.0)
        min_ltf_score = _score_threshold(self.params.get("min_ltf_score", 2.5), 2.5, minimum=1.0)
        if float(trigger.get("score", 0.0) or 0.0) < min_ltf_score:
            pending_reasons.append(reason_with_values("weak_ltf_score", current=float(trigger.get("score", 0.0) or 0.0), required=min_ltf_score, op=">=", digits=4))

        pending_reasons.extend(self._entry_exhaustion_reasons(side, ltf, close=trend["close"], vwap=trend["vwap"], ema9=trend["ema9"]))

        extension_from_vwap_atr = max(0.0, abs(trend["close"] - trend["vwap"]) / max(trend["atr"], 1e-9))
        extension_from_ema9_atr = max(0.0, abs(trend["close"] - trend["ema9"]) / max(trend["atr"], 1e-9))
        max_vwap_ext = max(0.1, float(self.params.get("max_extension_from_vwap_atr", 1.05)))
        max_ema9_ext = max(0.1, float(self.params.get("max_extension_from_ema9_atr", 0.88)))
        extension_penalty_per_atr = max(0.0, float(self.params.get("extension_penalty_per_atr", 0.72)))
        extension_hard_cap_mult = max(1.0, float(self.params.get("extension_hard_cap_mult", 1.45)))
        vwap_extension_over = max(0.0, extension_from_vwap_atr - max_vwap_ext)
        ema9_extension_over = max(0.0, extension_from_ema9_atr - max_ema9_ext)
        extension_penalty = ((vwap_extension_over + ema9_extension_over) * extension_penalty_per_atr)
        if extension_penalty > 0:
            total_score -= extension_penalty
            diagnostics.append(f"extension_penalty:{extension_penalty:.4f}")
        # The cap reads the absolute distance, so it also refuses a close that
        # far on the wrong side of the line: a LONG below it, a SHORT above.
        # That refusal is wrong_side_hard_cap_*; too_extended_hard_cap_* is a
        # close stretched past the line on the side's own side. Until
        # 2026-09-26 both were too_extended_hard_cap_*.
        long = side == Side.LONG
        if extension_from_vwap_atr > (max_vwap_ext * extension_hard_cap_mult):
            vwap_cap_reason = "too_extended_hard_cap_vwap_atr" if (trend["close"] > trend["vwap"]) == long else "wrong_side_hard_cap_vwap_atr"
            pending_reasons.append(reason_with_values(vwap_cap_reason, current=extension_from_vwap_atr, required=max_vwap_ext * extension_hard_cap_mult, op="<=", digits=4))
        if extension_from_ema9_atr > (max_ema9_ext * extension_hard_cap_mult):
            ema9_cap_reason = "too_extended_hard_cap_ema9_atr" if (trend["close"] > trend["ema9"]) == long else "wrong_side_hard_cap_ema9_atr"
            pending_reasons.append(reason_with_values(ema9_cap_reason, current=extension_from_ema9_atr, required=max_ema9_ext * extension_hard_cap_mult, op="<=", digits=4))

        if total_score < min_total:
            pending_reasons.append(reason_with_values("weak_total_score", current=total_score, required=min_total, op=">=", digits=4))

        gate_snapshots = [
            _gate_snapshot("trend_score", passed=float(trend.get("score", 0.0) or 0.0) > 0.0, current=round(float(trend.get("score", 0.0) or 0.0), 4), required=0.0, op=">"),
            _gate_snapshot("ltf_score", passed=float(trigger.get("score", 0.0) or 0.0) >= min_ltf_score, current=round(float(trigger.get("score", 0.0) or 0.0), 4), required=min_ltf_score, op=">="),
            _gate_snapshot("peer_agreement", passed=peer_agreement >= min_peer_agreement, current=int(peer_agreement), required=int(min_peer_agreement), op=">="),
            _gate_snapshot("directional_peer_score", passed=directional_peer_score >= min_peer_score, current=round(float(directional_peer_score), 4), required=int(min_peer_score), op=">="),
            _gate_snapshot("macro_alignment", passed=macro_aligned, current=int(macro_aligned), required=1, op=">="),
            _gate_snapshot("continuation_total_score", passed=total_score >= min_total, current=round(total_score, 4), required=min_total, op=">="),
        ]
        near_miss_blockers: dict[str, float] = {}
        if float(trigger.get("score", 0.0) or 0.0) < min_ltf_score:
            near_miss_blockers["ltf_score"] = round(min_ltf_score - float(trigger.get("score", 0.0) or 0.0), 4)
        if peer_agreement < min_peer_agreement:
            near_miss_blockers["peer_agreement"] = round(float(min_peer_agreement - peer_agreement), 4)
        if directional_peer_score < min_peer_score:
            near_miss_blockers["directional_peer_score"] = round(float(min_peer_score - directional_peer_score), 4)
        if total_score < min_total:
            near_miss_blockers["continuation_total_score"] = round(min_total - total_score, 4)
        side_eval = {
            "side": side.value,
            "gates": gate_snapshots,
            "diagnostics": diagnostics,
        }

        stop_buffer_atr = max(0.05, float(self.params.get("stop_buffer_atr_mult", 0.50)))
        if side == Side.LONG:
            stop = float(trigger.get("stop_anchor", trend["close"])) - (trend["atr"] * stop_buffer_atr)
            stop = min(stop, trend["ema20"] - (trend["atr"] * 0.1))
            stop = min(stop, trend["close"] * (1.0 - float(self.config.risk.default_stop_pct)))
            risk_per_share = max(0.01, trend["close"] - stop)
            target_rr = max(float(self.params.get("target_rr", 2.05)), float(self.params.get("min_rr", 1.8)))
            target = trend["close"] + (risk_per_share * target_rr)
        else:
            stop = float(trigger.get("stop_anchor", trend["close"])) + (trend["atr"] * stop_buffer_atr)
            stop = max(stop, trend["ema20"] + (trend["atr"] * 0.1))
            stop = max(stop, trend["close"] * (1.0 + float(self.config.risk.default_stop_pct)))
            risk_per_share = max(0.01, stop - trend["close"])
            target_rr = max(float(self.params.get("target_rr", 2.05)), float(self.params.get("min_rr", 1.8)))
            target = max(0.01, trend["close"] - (risk_per_share * target_rr))

        # The shared entry stage (2026-09-24): the vetoes, the refinement and
        # the score terms, gated on the 5m LTF the setup was read on
        # (amendment 4) with S/R on the 1m frame, the entry-context score's
        # HTF divergence term on this strategy's own HTF context. A refusal
        # lands under this side's failure key with the gate snapshots and the
        # near-miss payload.
        admitted = self.entry_policy.admit(EntryProposal(
            candidate=c, direction=side, style="trend_continuation", style_family="continuation",
            close=float(trend["close"]), stop=stop, target=target,
            gate_frame=ltf, sr_frame=frame, level_frame=ltf, data=data,
            pending_reasons=tuple(pending_reasons),
            htf_ctx=htf,
            contexts=EntryContexts(sr=sr_ctx, ms=ms_ctx, tech=tech_ctx),
            failure_details={
                "peer_universe": list(peer_ctx.get("universe", [])),
                "peer_details": dict(peer_ctx.get("details", {})),
                "peer_bullish": int(peer_ctx.get("bullish", 0) or 0),
                "peer_bearish": int(peer_ctx.get("bearish", 0) or 0),
                "peer_score": int(peer_ctx.get("score", 0) or 0),
                "macro_details": dict(macro_ctx.get("details", {})),
                "macro_long_agree": int(macro_ctx.get("long_agree", 0) or 0),
                "macro_short_agree": int(macro_ctx.get("short_agree", 0) or 0),
                "side_eval": side_eval,
                "near_miss_blockers": near_miss_blockers,
            },
            failure_key=failure_style,
        ))
        if admitted is None:
            return None
        runner_allowed = bool(self.params.get("strong_setup_runner_enabled", True)) and total_score >= (min_total + 1)
        management = self._adaptive_management_components(
            side,
            trend["close"],
            float(admitted.stop),
            admitted.target,
            style="trend",
            runner_allowed=runner_allowed,
            continuation_bias=float(admitted.fvg["fvg_continuation_bias"]),
            strong_setup=runner_allowed,
        )
        activity_weight = max(0.0, float(self.params.get("activity_score_weight", 0.12)))
        # What this strategy calls its execution quality -- the entry-context
        # and FVG terms -- is the shared context score now; emit adds it to
        # the strategy's own score, so final_priority_score is total +
        # execution + activity as it was before 2026-09-24.
        execution_quality_score = float(admitted.shared_context_score)
        strategy_score = total_score + (float(c.activity_score) * activity_weight)
        return self.entry_policy.emit(
            admitted,
            reason="peer_confirmed_trend_continuation_long" if side == Side.LONG else "peer_confirmed_trend_continuation_short",
            strategy_score=strategy_score,
            management=management,
            target=admitted.target,
            metadata={
                "activity_score": float(c.activity_score),
                "setup_quality_score": round(total_score, 4),
                "execution_quality_score": round(execution_quality_score, 4),
                "macro_score": float(macro_ctx.get("long_agree", 0) or 0) if side == Side.LONG else float(macro_ctx.get("short_agree", 0) or 0),
                "trend_score": round(float(trend.get("score", 0.0) or 0.0), 4),
                "ltf_score": round(float(trigger.get("score", 0.0) or 0.0), 4),
                "peer_score": float(peer_score),
                "directional_peer_score": float(directional_peer_score),
                "peer_agreement": int(peer_agreement),
                "peer_bullish": int(peer_ctx.get("bullish", 0) or 0),
                "peer_bearish": int(peer_ctx.get("bearish", 0) or 0),
                "peer_details": dict(peer_ctx.get("details", {})),
                "peer_universe": list(peer_ctx.get("universe", [])),
                "peer_bonus": round(float(peer_bonus), 4),
                "macro_bonus_applied": round(float(macro_bonus if macro_aligned else -macro_miss_penalty), 4),
                "extension_penalty": round(float(extension_penalty), 4),
                "activity_score_weight": float(activity_weight),
                "macro_long_agree": int(macro_ctx.get("long_agree", 0) or 0),
                "macro_short_agree": int(macro_ctx.get("short_agree", 0) or 0),
                "macro_details": dict(macro_ctx.get("details", {})),
                "continuation_total_score": round(total_score, 4),
                "diagnostics": diagnostics,
                "side_eval": side_eval,
                "primary_blocker": None,
                "all_blockers": [],
                "near_miss_blockers": near_miss_blockers,
                "trigger_level": float(trigger.get("trigger_level", trend["close"]) or trend["close"]),
                "pullback_extreme": float(trigger.get("pullback_extreme", trend["close"]) or trend["close"]),
                "pullback_depth_atr": float(trigger.get("pullback_depth_atr", 0.0) or 0.0),
                "trigger_volume_ratio": float(trigger.get("trigger_volume_ratio", 0.0) or 0.0),
                "countertrend_volume_ratio": float(trigger.get("countertrend_volume_ratio", 0.0) or 0.0),
                "trend_htf_bias": str(trend.get("htf_bias", "neutral")),
                # HTF votes FOR this side net of those against, as key_levels
                # ranks. Until 2026-09-24 it was the absolute difference, and
                # with no HTF hard gate here a short against a 3v0 bullish HTF
                # took the maximum ranking credit.
                "directional_vote_edge": float(self._side_vote_edge(
                    side, int(trend.get("htf_bull_votes", 0) or 0), int(trend.get("htf_bear_votes", 0) or 0))),
                "runner_quality_score": 1.0 if runner_allowed else 0.0,
                "execution_headroom_score": round(float(max(0.0, min(max_vwap_ext - extension_from_vwap_atr, max_ema9_ext - extension_from_ema9_atr))), 4),
                "source_quality_score": 0.0,
                # = final_priority_score, as it was: the rank tail reads it.
                "selection_quality_score": round(strategy_score + execution_quality_score, 4),
            },
        )

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        """Each candidate's continuation setup on the preferred sides it may
        open: none past its entry cutoff, ``entry_cutoff_lead_minutes`` (10)
        before its force-flatten cutoff (``_entry_sides_now``, the check
        key_levels' pass makes; with every side past it the symbol is
        skipped as ``after_entry_cutoff(cutoff=HH:MM,flatten=HH:MM)``, and a
        refusal ends with ``<side>.after_entry_cutoff(...)`` for a side past
        it): 15:43 in the preset, after its 15:40 window ends, and 12:45 on
        an early-close day, before its 12:55 afternoon window opens. Until
        2026-10-13 the preset flattened neither side, so such an entry was
        held overnight."""
        self._reset_entry_decisions()
        out: list[Signal] = []
        history_bars = self.required_history_bars()
        ltf_min = max(1, int(self.params.get("ltf_minutes", 5)))
        macro_ctx = self._macro_signal(bars, data)
        tradable_symbols = set(self._tradable_symbols())
        open_sides, cutoffs_passed = self._entry_sides_now()
        closed_side_reasons = self._closed_side_reasons(cutoffs_passed)
        def judge(c: Candidate) -> Signal | None:
            """One candidate's judgement; a raise is its own (``_entry_pass_failed``)."""
            if self._skip_untradable(c.symbol, tradable_symbols):
                return None
            frame = bars.get(c.symbol)
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                return None
            if self._skip_past_cutoff(c.symbol, open_sides, cutoffs_passed):
                return None
            if frame is None or len(frame) < history_bars:
                self._record_entry_decision(c.symbol, "skipped", [insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), history_bars)])
                return None
            data_clock = self._step_frame_end(frame)
            ltf = self._completed_ltf(self._resampled_frame(frame, ltf_min), ltf_min, data_clock)
            if ltf is None or ltf.empty or len(ltf) < max(10, int(self.params.get("min_ltf_bars", 18)) + 4):
                self._record_entry_decision(c.symbol, "skipped", ["missing_ltf_context"])
                return None
            # The setup is judged on the last COMPLETED trigger bucket, once
            # (the family's rule, key_levels since 2026-10-13, this strategy
            # since 2026-11-08, the whole-bot review's R-009): the first pass
            # that sees a bucket complete judges it, every later pass repeats
            # its verdict until the next bucket completes, and a bucket first
            # seen more than LTF_BUCKET_MAX_LATE past its end is missed
            # unjudged. Until then every trigger term read the forming
            # bucket, 1-4 minutes of a 5m candle against full ones, up to
            # five looks a bucket.
            bucket = pd.Timestamp(ltf.index[-1])
            judged = self._judged_buckets.get(c.symbol)
            if judged is not None and judged.bucket == bucket:
                self._repeat_bucket_verdict(c.symbol, judged)
                return None
            late = self._bucket_lateness(ltf, ltf_min, data_clock)
            if late > LTF_BUCKET_MAX_LATE:
                self._miss_bucket(c.symbol, bucket, late)
                return None
            bucket_details = {"ltf_bucket": bucket.isoformat()}
            if data is not None:
                owed = data.pending_stream_backfill(c.symbol)
                if owed is not None:
                    self._record_entry_decision(
                        c.symbol, "skipped",
                        [f"ltf_bucket_backfill_pending(bucket={bucket:%H:%M},first_bar={pd.Timestamp(owed):%H:%M})"],
                        details=dict(bucket_details))
                    return None
            peer_ctx = self._peer_signal(c.symbol, bars, data)
            preferred_sides = self._preferred_sides(c)
            if preferred_sides is None:
                self._record_entry_decision(c.symbol, "skipped", ["shorts_disabled"])
                return None
            side_order, evaluated_sides = self._entry_side_context(
                [side for side in preferred_sides if side in open_sides])
            # Build side-agnostic contexts ONCE per candidate; pass them into each
            # per-side builder so we don't recompute market-structure / technical
            # contexts twice when both LONG and SHORT are evaluated.
            shared_sr_ctx = self._sr_context(c.symbol, frame, data)
            shared_ms_ctx = self._structure_context(ltf, "ltf")
            shared_tech_ctx = self._technical_context(ltf)
            sides = self._evaluate_sides(c, side_order, lambda side: self._build_continuation_signal(
                c, frame, ltf, side, peer_ctx, macro_ctx, data=data,
                sr_ctx=shared_sr_ctx, ms_ctx=shared_ms_ctx, tech_ctx=shared_tech_ctx,
            ))
            if sides.signals:
                signal = self._pick_side_signal(sides.signals, c)
                meta = signal.metadata if isinstance(signal.metadata, dict) else {}
                signal_details = {"side_eval": sides.side_eval or meta.get("side_eval"), "peer_universe": meta.get("peer_universe"), "evaluated_sides": evaluated_sides, **bucket_details}
                self._record_entry_decision(c.symbol, "signal", [signal.reason], details=signal_details)
                self._judged_buckets[c.symbol] = _BucketVerdict(bucket, True, [signal.reason], signal_details)
                return signal
            else:
                fail_reasons = sides.fail_reasons + closed_side_reasons
                skip_details = {"side_eval": sides.side_eval, "peer_universe": list(peer_ctx.get("universe", [])), "peer_details": dict(peer_ctx.get("details", {})), "evaluated_sides": evaluated_sides, "primary_blocker": fail_reasons[0] if fail_reasons else None, "all_blockers": list(fail_reasons), "near_miss_blockers": sides.near_miss_blockers, **bucket_details}
                self._record_entry_decision(c.symbol, "skipped", fail_reasons or ["no_setup"], details=skip_details)
                self._judged_buckets[c.symbol] = _BucketVerdict(bucket, False, list(fail_reasons or ["no_setup"]), skip_details)
                return None

        # A candidate whose judgement raises is skipped alone, fail closed,
        # and the pass goes on (key_levels' rule since 2026-10-13, this
        # strategy's since 2026-11-08): until then the raise ended the pass,
        # so a signal built earlier in it never reached the entry stage while
        # its bucket's verdict read ltf_bucket_signalled.
        for c in candidates:
            try:
                signal = judge(c)
            except Exception as exc:
                self._entry_pass_failed(c.symbol, exc)
                continue
            self._entry_error_streaks.pop(c.symbol, None)
            if signal is not None:
                out.append(signal)
        return out
