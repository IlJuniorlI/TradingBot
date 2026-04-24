# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
    Position,
    Side,
    Signal,
    now_et,
    pd,
)
from ..strategy_base import BaseStrategy

class MeanReversionStrategy(BaseStrategy):
    strategy_name = 'mean_reversion'
    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        min_day_strength = float(self.params.get("min_day_strength", 5.5))
        max_pullback_from_high = float(self.params.get("max_pullback_from_high", 0.027))
        for c in candidates:
            reasons: list[str] = []
            frame = bars.get(c.symbol)
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue
            if frame is None or len(frame) < 20:
                self._record_entry_decision(c.symbol, "skipped", [self._insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), 20)])
                continue
            last = frame.iloc[-1]
            last_close = self._safe_float(last["close"])
            session_frame = frame[self._same_day_mask(frame, now_et().date())]
            recent20 = session_frame.tail(20)
            recent3 = frame.tail(3)
            day_strength = self._safe_float(c.metadata.get("change_from_open"), 0.0)
            if day_strength < min_day_strength:
                reasons.append(self._reason_with_values("weak_day_strength", current=day_strength, required=min_day_strength, op=">=", digits=4))
            high20 = float(recent20["high"].max()) if not recent20.empty else 0.0
            if high20 <= 0:
                reasons.append("invalid_session_high")
            pullback_pct = (high20 - last_close) / high20 if high20 > 0 else 0.0
            if high20 > 0 and pullback_pct > max_pullback_from_high:
                reasons.append(self._reason_with_values("pullback_too_deep", current=pullback_pct, required=max_pullback_from_high, op="<=", digits=4))
            candle_signal = self._directional_candle_signal(frame, Side.LONG)
            matched_patterns = set(candle_signal.get("matches", []))
            bullish_candle_net_score = float(candle_signal.get("net_score", 0.0) or 0.0)
            bullish_candle_score = float(candle_signal.get("score", 0.0) or 0.0)
            bullish_candle_anchor_pattern = candle_signal.get("anchor_pattern")
            bullish_candle_anchor_bars = int(candle_signal.get("anchor_bars", 0) or 0)
            candle_confirmed = bool(candle_signal.get("confirmed"))
            ctx = self._chart_context(frame)
            sr_ctx = self._sr_context(c.symbol, frame, data)
            ms_ctx = self._structure_context(frame, "1m")
            tech_ctx = self._technical_context(frame)
            reversal_support = bool(ctx.matched_bullish_reversal or (candle_confirmed and ctx.bias_score >= 0.0))
            if not (candle_confirmed or ctx.matched_bullish_reversal):
                reasons.append("no_reversal_pattern")
            if not reversal_support:
                reasons.append("chart_pattern_not_supportive")
            ema9 = self._safe_float(last["ema9"], last_close)
            last_ret5 = self._safe_float(last.get("ret5"), 0.0)
            close_pos = self._bar_close_position(frame)
            min_reversal_close_position = float(self.params.get("min_reversal_close_position", 0.58))
            require_positive_ret5 = bool(self.params.get("require_positive_reversal_ret5", True))
            if last_close <= ema9:
                reasons.append(self._reason_with_values("below_ema9", current=last_close, required=ema9, op=">", digits=4))
            if close_pos < min_reversal_close_position:
                reasons.append(self._reason_with_values("weak_reversal_close", current=close_pos, required=min_reversal_close_position, op=">=", digits=4))
            if require_positive_ret5 and last_ret5 <= 0.0:
                reasons.append(self._reason_with_values("reversal_momentum_not_positive", current=last_ret5, required=0.0, op=">", digits=4))
            if not reasons and self._shared_entry_enabled("use_opposing_chart_filter", True) and self._blocks_bullish_entry(ctx):
                reasons.append("chart_pattern_opposed")
            if not reasons:
                stop = float(recent3["low"].min())
                target = max(float(high20), last_close * (1.0 + self.config.risk.default_target_pct))
                if self._blocks_bullish_structure_entry(ms_ctx):
                    reasons.append(self._bullish_structure_block_reason(ms_ctx))
                elif self._blocks_bullish_sr_entry(sr_ctx):
                    reasons.append(self._bullish_sr_block_reason(sr_ctx))
                else:
                    stop, target = self._refine_bullish_sr_levels(last_close, stop, target, sr_ctx)
                    stop, target = self._refine_bullish_technical_levels(last_close, stop, target, tech_ctx, frame)
                    candle_part = '+'.join(sorted(matched_patterns)) if matched_patterns else 'chart_only'
                    chart_part = '+'.join(sorted(ctx.matched_bullish_reversal | ctx.matched_bullish_continuation))
                    reason = f"strong_name_pullback_reversal:{candle_part}"
                    if chart_part:
                        reason += f":{chart_part}"
                    out.append(
                        self._build_bullish_reversal_signal(
                            candidate=c,
                            frame=frame,
                            data=data,
                            reason=reason,
                            matched_patterns=matched_patterns,
                            bullish_candle_score=bullish_candle_score,
                            bullish_candle_net_score=bullish_candle_net_score,
                            bullish_candle_anchor_pattern=bullish_candle_anchor_pattern,
                            bullish_candle_anchor_bars=bullish_candle_anchor_bars,
                            chart_ctx=ctx,
                            sr_ctx=sr_ctx,
                            ms_ctx=ms_ctx,
                            tech_ctx=tech_ctx,
                            stop=stop,
                            target=target,
                            extra_priority=max(0.0, (1.0 - pullback_pct)) + (0.25 if ctx.matched_bullish_reversal else 0.0),
                        )
                    )
                    self._record_entry_decision(c.symbol, "signal", [reason])
                    continue
            self._record_entry_decision(c.symbol, "skipped", reasons or ["no_setup"])
        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
