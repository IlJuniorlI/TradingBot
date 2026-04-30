# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
    Position,
    Side,
    Signal,
    insufficient_bars_reason,
    _reason_with_values,
    _safe_float,
    pd,
)
from ..strategy_base import BaseStrategy

class MomentumIntoCloseStrategy(BaseStrategy):
    strategy_name = 'momentum_close'
    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        lookback = int(self.params.get("breakout_lookback_bars", 6))
        min_day_strength = float(self.params.get("min_change_from_open", 4.2))
        for c in candidates:
            reasons: list[str] = []
            frame = bars.get(c.symbol)
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue
            if frame is None or len(frame) < 30:
                self._record_entry_decision(c.symbol, "skipped", [insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), 30)])
                continue
            last = frame.iloc[-1]
            recent = frame.tail(lookback + 1).iloc[:-1]
            breakout = _safe_float(last["close"]) > float(recent["high"].max())
            day_strength = _safe_float(c.metadata.get("change_from_open"), 0.0)
            ctx = self._chart_context(frame)
            sr_ctx = self._sr_context(c.symbol, frame, data)
            ms_ctx = self._structure_context(frame, "1m")
            tech_ctx = self._technical_context(frame)
            pattern_ok = bool(ctx.matched_bullish_continuation or ctx.matched_bullish_reversal) or ctx.bias_score >= 0.0
            last_close = _safe_float(last["close"])
            last_vwap = _safe_float(last["vwap"], last_close)
            last_ret15 = _safe_float(last["ret15"], 0.0)
            last_ema9 = _safe_float(last["ema9"], last_close)
            last_ema20 = _safe_float(last["ema20"], last_close)
            breakout_level = float(recent["high"].max())
            retest_plan = self._continuation_fvg_retest_plan(Side.LONG, c.symbol, frame, data, trigger_level=breakout_level, breakout_active=bool(breakout), close=last_close, vwap=last_vwap, ema9=last_ema9)
            if not breakout:
                reasons.append(_reason_with_values("no_breakout", current=last_close, required=breakout_level, op=">", digits=4))
            if day_strength < min_day_strength:
                reasons.append(_reason_with_values("weak_day_strength", current=day_strength, required=min_day_strength, op=">=", digits=4))
            if last_close <= last_vwap:
                reasons.append(_reason_with_values("below_vwap", current=last_close, required=last_vwap, op=">", digits=4))
            if last_ret15 <= 0:
                reasons.append(_reason_with_values("weak_ret15", current=last_ret15, required=0.0, op=">", digits=4))
            if last_ema9 < last_ema20:
                reasons.append(_reason_with_values("ema9_below_ema20", current=last_ema9, required=last_ema20, op=">=", digits=4))
            if not pattern_ok:
                reasons.append("chart_pattern_not_supportive")
            if not reasons and self._shared_entry_enabled("use_opposing_chart_filter", True) and self._blocks_bullish_entry(ctx):
                reasons.append("chart_pattern_opposed")
            reasons = self._apply_continuation_fvg_retest_plan(reasons, retest_plan, deferrable_prefixes={"no_breakout", "too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "upper_wick_rejection", "expansion_bar_too_large"})
            if not reasons:
                reasons.extend(self._entry_exhaustion_reasons(Side.LONG, frame, close=last_close, vwap=last_vwap, ema9=last_ema9))
                reasons = self._apply_continuation_fvg_retest_plan(reasons, retest_plan, deferrable_prefixes={"too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "upper_wick_rejection", "expansion_bar_too_large"})
            if not reasons:
                divergence_reason = self._dual_counter_divergence_reason(Side.LONG, tech_ctx)
                if divergence_reason:
                    reasons.append(divergence_reason)
            if not reasons:
                # ATR-anchored stop: rebase below the recent swing low by
                # 8% of ATR so noisy single-bar wicks don't trigger the stop
                # on an otherwise valid breakout. Still bounded by the
                # default_stop_pct floor so we never risk more than the
                # configured percentage. Non-restrictive — only LOOSENS the
                # stop slightly on high-conviction momentum setups.
                last_atr = _safe_float(last.get("atr14"), 0.0)
                swing_low = float(recent["low"].min())
                if last_atr > 0:
                    swing_low = swing_low - (last_atr * 0.08)
                stop = max(last_close * (1.0 - self.config.risk.default_stop_pct), swing_low)
                target = last_close * (1.0 + self.config.risk.default_target_pct)
                if self._blocks_bullish_structure_entry(ms_ctx):
                    reasons.append(self._bullish_structure_block_reason(ms_ctx))
                elif self._blocks_bullish_sr_entry(sr_ctx):
                    reasons.append(self._bullish_sr_block_reason(sr_ctx))
                else:
                    stop, target = self._refine_bullish_sr_levels(last_close, stop, target, sr_ctx)
                    stop, target = self._refine_bullish_technical_levels(last_close, stop, target, tech_ctx, frame)
                    stop = self._apply_retest_stop_anchor(Side.LONG, last_close, stop, retest_plan)
                    breakout_pct = max(0.0, (last_close - breakout_level) / breakout_level) if breakout_level > 0 else 0.0
                    ms_bias = getattr(ms_ctx, "bias", "neutral")
                    structure_bonus = 0.75 if ms_bias == "bullish" else 0.0
                    if bool(getattr(ms_ctx, "bos_up", False)) and self._structure_event_recent(getattr(ms_ctx, "bos_up_age_bars", None)):
                        structure_bonus += 0.5
                    pattern_bonus = 0.35 if ctx.matched_bullish_continuation else 0.15 if ctx.matched_bullish_reversal else 0.0
                    adjustments = self._entry_adjustment_components(Side.LONG, sr_ctx=sr_ctx, tech_ctx=tech_ctx)
                    fvg_adjustments = self._fvg_entry_adjustment_components(Side.LONG, c.symbol, frame, data)
                    fvg_continuation_bias = float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0)
                    runner_allowed = bool(fvg_continuation_bias >= 0.35 and (ctx.matched_bullish_continuation or ms_bias == "bullish"))
                    management = self._adaptive_management_components(Side.LONG, last_close, stop, target, style="momentum", runner_allowed=runner_allowed, continuation_bias=fvg_continuation_bias)
                    final_priority_score = float(c.activity_score) + (max(0.0, last_ret15) * 100.0) + (breakout_pct * 200.0) + structure_bonus + pattern_bonus + adjustments["entry_context_adjustment"] + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
                    metadata = self._build_signal_metadata(
                        entry_price=last_close,
                        chart_ctx=ctx, ms_ctx=ms_ctx, sr_ctx=sr_ctx, tech_ctx=tech_ctx,
                        adjustments=adjustments, fvg_adjustments=fvg_adjustments,
                        management=management, retest_plan=retest_plan,
                        final_priority_score=final_priority_score,
                    )
                    reason = "smallcap_breakout_fvg_retest" if str(retest_plan.get("status", "none") or "none") == "allow" else "smallcap_breakout_above_vwap"
                    if ctx.matched_bullish_continuation:
                        reason += f":{'+'.join(sorted(ctx.matched_bullish_continuation))}"
                    out.append(Signal(symbol=c.symbol, strategy=self.strategy_name, side=Side.LONG, reason=reason, stop_price=stop, target_price=target, metadata=metadata))
                    self._record_entry_decision(c.symbol, "signal", [reason])
                    continue
            self._record_entry_decision(c.symbol, "skipped", reasons or ["no_setup"])
        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
