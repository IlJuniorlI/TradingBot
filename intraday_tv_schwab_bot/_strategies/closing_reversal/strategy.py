# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
    Position,
    Side,
    Signal,
    _bar_close_position,
    insufficient_bars_reason,
    _reason_with_values,
    _safe_float,
    _same_day_mask,
    pd,
)
from ... import sessions
from ..shared_entry import EntryContexts, EntryProposal
from ..strategy_base import BaseStrategy


class ClosingReversalStrategy(BaseStrategy):
    """Late-day, long-only rebound in names that were strong all day.

    One LONG proposal per candidate (style / family ``reversal``): the
    setup's own blockers are its pending reasons, the stop is the last three
    bars' low and the target the session high, capped at
    ``min(2%, default_target_pct)`` above the close. Every shared_entry knob
    is applied by ``self.entry_policy.admit``; the runner stays off and the
    management leans on the FVG reversal bias.
    """

    strategy_name = 'closing_reversal'

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        min_day_strength = float(self.params.get("min_day_strength", 6.2))
        max_pullback_from_high = float(self.params.get("max_pullback_from_high", 0.048))
        for c in candidates:
            reasons: list[str] = []
            frame = bars.get(c.symbol)
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue
            if frame is None or len(frame) < 20:
                self._record_entry_decision(c.symbol, "skipped", [insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), 20)])
                continue
            last = frame.iloc[-1]
            last_close = _safe_float(last["close"])
            day_strength = _safe_float(c.metadata.get("change_from_open"), 0.0)
            if day_strength < min_day_strength:
                reasons.append(_reason_with_values("weak_day_strength", current=day_strength, required=min_day_strength, op=">=", digits=4))
            session_frame = frame[_same_day_mask(frame, sessions.now_et().date())]
            session_high = float(session_frame["high"].max()) if not session_frame.empty else 0.0
            if session_high <= 0:
                reasons.append("invalid_session_high")
            pullback_pct = (session_high - last_close) / session_high if session_high > 0 else 0.0
            if session_high > 0 and pullback_pct > max_pullback_from_high:
                reasons.append(_reason_with_values("pullback_too_deep", current=pullback_pct, required=max_pullback_from_high, op="<=", digits=4))
            last3 = frame.tail(3)
            momentum_up = bool(last3["close"].iloc[-1] > last3["close"].iloc[0])
            candle_signal = self._directional_candle_signal(frame, Side.LONG)
            matched_patterns = set(candle_signal.get("matches", []))
            bullish_candle_net_score = float(candle_signal.get("net_score", 0.0) or 0.0)
            bullish_candle_score = float(candle_signal.get("score", 0.0) or 0.0)
            bullish_candle_anchor_pattern = candle_signal.get("anchor_pattern")
            bullish_candle_anchor_bars = int(candle_signal.get("anchor_bars", 0) or 0)
            candle_confirmed = bool(candle_signal.get("confirmed"))
            ctx = self._chart_context(frame)
            chart_ok = bool(ctx.matched_bullish_reversal or ctx.matched_bullish_continuation) or (candle_confirmed and ctx.bias_score >= 0.0)
            if not momentum_up:
                reasons.append(_reason_with_values("no_short_term_bounce", current=_safe_float(last3["close"].iloc[-1]), required=_safe_float(last3["close"].iloc[0]), op=">", digits=4))
            if not (candle_confirmed or ctx.matched_bullish_reversal):
                reasons.append("no_reversal_pattern")
            if not chart_ok:
                reasons.append("chart_pattern_not_supportive")
            ema9 = _safe_float(last["ema9"], last_close)
            last_ret5 = _safe_float(last.get("ret5"), 0.0)
            close_pos = _bar_close_position(frame)
            min_reversal_close_position = float(self.params.get("min_reversal_close_position", 0.61))
            require_positive_ret5 = bool(self.params.get("require_positive_reversal_ret5", True))
            if last_close <= ema9:
                reasons.append(_reason_with_values("below_ema9", current=last_close, required=ema9, op=">", digits=4))
            if close_pos < min_reversal_close_position:
                reasons.append(_reason_with_values("weak_reversal_close", current=close_pos, required=min_reversal_close_position, op=">=", digits=4))
            if require_positive_ret5 and last_ret5 <= 0.0:
                reasons.append(_reason_with_values("reversal_momentum_not_positive", current=last_ret5, required=0.0, op=">", digits=4))
            default_target = last_close * (1 + min(0.02, self.config.risk.default_target_pct))
            proposal = EntryProposal(
                candidate=c,
                direction=Side.LONG,
                style="reversal",
                style_family="reversal",
                close=last_close,
                stop=float(last3["low"].min()),
                target=min(session_high, default_target) if session_high > last_close * 1.003 else default_target,
                gate_frame=frame,
                sr_frame=frame,
                level_frame=frame,
                data=data,
                pending_reasons=tuple(reasons),
                contexts=EntryContexts(chart=ctx),
            )
            admitted = self.entry_policy.admit(proposal)
            if admitted is None:
                refusal = self._consume_build_failure_payload(c.symbol, proposal.style)
                self._record_entry_decision(c.symbol, "skipped", refusal["reasons"])
                continue
            management = self._adaptive_management_components(
                Side.LONG,
                last_close,
                admitted.stop,
                admitted.target,
                style="reversal",
                runner_allowed=False,
                continuation_bias=float(admitted.fvg["fvg_reversal_bias"]),
            )
            reason = f"late_day_bounce_in_strong_smallcap:{'+'.join(sorted(matched_patterns)) if matched_patterns else 'chart_only'}"
            if ctx.matched_bullish_reversal or ctx.matched_bullish_continuation:
                reason += f":{'+'.join(sorted(ctx.matched_bullish_reversal | ctx.matched_bullish_continuation))}"
            # 0.75 x the candle net score: see mean_reversion (the 2026-05-12
            # candle tier cascade retune).
            strategy_score = float(c.activity_score) + (0.25 if momentum_up else 0.0) + 0.75 * bullish_candle_net_score
            out.append(
                self.entry_policy.emit(
                    admitted,
                    reason=reason,
                    strategy_score=strategy_score,
                    management=management,
                    target=admitted.target,
                    metadata={
                        "matched_bullish_patterns": sorted(matched_patterns),
                        "bullish_candle_score": round(float(bullish_candle_score), 4),
                        "bullish_candle_net_score": round(float(bullish_candle_net_score), 4),
                        "bullish_candle_anchor_pattern": bullish_candle_anchor_pattern,
                        "bullish_candle_anchor_bars": int(bullish_candle_anchor_bars),
                    },
                )
            )
            self._record_entry_decision(c.symbol, "signal", [reason])
        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
