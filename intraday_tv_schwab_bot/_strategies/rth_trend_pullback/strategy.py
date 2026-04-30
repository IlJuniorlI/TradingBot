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
    pd,
)
from ..strategy_base import BaseStrategy

class RTHTrendPullbackStrategy(BaseStrategy):
    strategy_name = 'rth_trend_pullback'

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        min_bars = int(self.params.get("min_bars", 35))
        support_lookback = int(self.params.get("support_lookback_bars", 10))
        trigger_lookback = int(self.params.get("trigger_lookback_bars", 4))
        return max(min_bars, support_lookback + trigger_lookback + 5)
    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        min_bars = int(self.params.get("min_bars", 35))
        support_lookback = int(self.params.get("support_lookback_bars", 10))
        trigger_lookback = int(self.params.get("trigger_lookback_bars", 4))
        min_change = float(self.params.get("min_change_from_open", 1.8))
        max_extension = float(self.params.get("max_extension_from_vwap_pct", 0.018))
        support_hold_pct = float(self.params.get("support_hold_pct", 0.012))
        min_bar_close_position = float(self.params.get("min_bar_close_position", 0.60))
        trend_min_ret5 = float(self.params.get("trend_min_ret5", 0.0002))
        trend_min_ret15 = float(self.params.get("trend_min_ret15", 0.0004))
        target_rr = max(1.0, float(self.params.get("target_rr", 2.0)))
        allow_short = bool(self.config.risk.allow_short)
        history_bars = max(min_bars, support_lookback + trigger_lookback + 5)
        for c in candidates:
            reasons: list[str] = []
            frame = bars.get(c.symbol)
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue
            if frame is None or len(frame) < history_bars:
                self._record_entry_decision(c.symbol, "skipped", [insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), history_bars)])
                continue
            last = frame.iloc[-1]
            day_strength = _safe_float(c.metadata.get("change_from_open"), 0.0)
            directional_bias = c.directional_bias if c.directional_bias in {Side.LONG, Side.SHORT} else (Side.LONG if day_strength >= 0 else Side.SHORT)
            recent = frame.tail(max(support_lookback + trigger_lookback + 1, trigger_lookback + 3))
            prior = recent.iloc[:-1]
            trigger_slice = prior.tail(max(2, trigger_lookback))
            support_slice = prior.tail(max(3, support_lookback))
            pullback_slice = prior.tail(max(3, trigger_lookback + 2))
            last_close = _safe_float(last["close"])
            last_vwap = _safe_float(last["vwap"], last_close)
            last_ret5 = _safe_float(last["ret5"], 0.0)
            last_ret15 = _safe_float(last["ret15"], 0.0)
            last_ema9 = _safe_float(last["ema9"], last_close)
            last_ema20 = _safe_float(last["ema20"], last_close)
            extension_pct = abs((last_close / last_vwap) - 1.0) if last_vwap > 0 else 0.0
            close_pos = _bar_close_position(frame)
            trigger_high = _safe_float(trigger_slice["high"].max(), last_close)
            trigger_low = _safe_float(trigger_slice["low"].min(), last_close)
            support_low = _safe_float(support_slice["low"].min(), last_close)
            resistance_high = _safe_float(support_slice["high"].max(), last_close)
            pullback_low = _safe_float(pullback_slice["low"].min(), last_close)
            pullback_high = _safe_float(pullback_slice["high"].max(), last_close)
            ctx = self._chart_context(frame)
            sr_ctx = self._sr_context(c.symbol, frame, data)
            ms_ctx = self._structure_context(frame, "1m")
            tech_ctx = self._technical_context(frame)
            metadata = {
                "trigger_high": trigger_high,
                "trigger_low": trigger_low,
                "support_low": support_low,
                "resistance_high": resistance_high,
                "pullback_low": pullback_low,
                "pullback_high": pullback_high,
                "extension_from_vwap_pct": extension_pct,
                **self._chart_lists(ctx),
                **self._structure_lists(ms_ctx, prefix="ms1m"),
                **self._sr_lists(sr_ctx),
                **self._technical_lists(tech_ctx),
            }
            if directional_bias == Side.LONG:
                long_support_ref = min(last_vwap, last_ema20)
                bullish_trigger = bool(self._structure_event_recent(getattr(ms_ctx, "bos_up_age_bars", None)) and getattr(ms_ctx, "bos_up", False)) or last_close > trigger_high
                retest_plan = self._continuation_fvg_retest_plan(Side.LONG, c.symbol, frame, data, trigger_level=trigger_high, breakout_active=bool(bullish_trigger), close=last_close, vwap=last_vwap, ema9=last_ema9)
                pullback_hold_ok = pullback_low >= (long_support_ref * (1.0 - support_hold_pct)) if long_support_ref > 0 else True
                if day_strength < min_change:
                    reasons.append(_reason_with_values("weak_day_strength", current=day_strength, required=min_change, op=">=", digits=4))
                if last_close <= last_vwap:
                    reasons.append(_reason_with_values("below_vwap", current=last_close, required=last_vwap, op=">", digits=4))
                if last_ema9 < last_ema20:
                    reasons.append(_reason_with_values("ema9_below_ema20", current=last_ema9, required=last_ema20, op=">=", digits=4))
                if last_ret5 < trend_min_ret5:
                    reasons.append(_reason_with_values("weak_ret5", current=last_ret5, required=trend_min_ret5, op=">=", digits=4))
                if last_ret15 < trend_min_ret15:
                    reasons.append(_reason_with_values("weak_ret15", current=last_ret15, required=trend_min_ret15, op=">=", digits=4))
                if extension_pct > max_extension:
                    reasons.append(_reason_with_values("too_extended_from_vwap", current=extension_pct, required=max_extension, op="<=", digits=4))
                if not pullback_hold_ok:
                    reasons.append(_reason_with_values("pullback_lost_support", current=pullback_low, required=long_support_ref * (1.0 - support_hold_pct), op=">=", digits=4))
                if not bullish_trigger:
                    reasons.append(_reason_with_values("no_reexpansion_trigger", current=last_close, required=trigger_high, op=">", digits=4))
                if close_pos < min_bar_close_position:
                    reasons.append(_reason_with_values("weak_bar_close", current=close_pos, required=min_bar_close_position, op=">=", digits=4))
                if not reasons and self._shared_entry_enabled("use_opposing_chart_filter", True) and self._blocks_bullish_entry(ctx):
                    reasons.append("chart_pattern_opposed")
                reasons = self._apply_continuation_fvg_retest_plan(reasons, retest_plan, deferrable_prefixes={"too_extended_from_vwap", "no_reexpansion_trigger", "weak_bar_close", "too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "upper_wick_rejection", "expansion_bar_too_large"})
                if not reasons:
                    reasons.extend(self._entry_exhaustion_reasons(Side.LONG, frame, close=last_close, vwap=last_vwap, ema9=last_ema9))
                    reasons = self._apply_continuation_fvg_retest_plan(reasons, retest_plan, deferrable_prefixes={"too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "upper_wick_rejection", "expansion_bar_too_large"})
                if not reasons:
                    divergence_reason = self._dual_counter_divergence_reason(Side.LONG, tech_ctx)
                    if divergence_reason:
                        reasons.append(divergence_reason)
                if not reasons:
                    stop = min(pullback_low, long_support_ref * (1.0 - support_hold_pct)) if long_support_ref > 0 else pullback_low
                    stop = min(stop, last_close * (1.0 - self.config.risk.default_stop_pct))
                    risk_per_share = max(0.01, last_close - stop)
                    effective_target_rr = target_rr
                    if bool(self.params.get("strong_trend_runner_enabled", True)) and getattr(ms_ctx, "bias", "neutral") == "bullish" and bool(getattr(ms_ctx, "bos_up", False)) and ctx.matched_bullish_continuation:
                        effective_target_rr = max(target_rr, float(self.params.get("strong_trend_target_rr", target_rr + 0.3)))
                    target = last_close + risk_per_share * effective_target_rr
                    if self._blocks_bullish_structure_entry(ms_ctx):
                        reasons.append(self._bullish_structure_block_reason(ms_ctx))
                    elif self._blocks_bullish_sr_entry(sr_ctx):
                        reasons.append(self._bullish_sr_block_reason(sr_ctx))
                    else:
                        stop, target = self._refine_bullish_sr_levels(last_close, stop, target, sr_ctx)
                        stop, target = self._refine_bullish_technical_levels(last_close, stop, target, tech_ctx, frame)
                        stop = self._apply_retest_stop_anchor(Side.LONG, last_close, stop, retest_plan)
                        structure_bonus = 0.75 if getattr(ms_ctx, "bias", "neutral") == "bullish" else 0.0
                        if bool(getattr(ms_ctx, "bos_up", False)) and self._structure_event_recent(getattr(ms_ctx, "bos_up_age_bars", None)):
                            structure_bonus += 0.5
                        pattern_bonus = 0.35 if ctx.matched_bullish_continuation else 0.15 if ctx.matched_bullish_reversal else 0.0
                        adjustments = self._entry_adjustment_components(Side.LONG, sr_ctx=sr_ctx, tech_ctx=tech_ctx)
                        fvg_adjustments = self._fvg_entry_adjustment_components(Side.LONG, c.symbol, frame, data)
                        runner_allowed = bool((effective_target_rr > target_rr or float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0) >= 0.35) and getattr(ms_ctx, "bias", "neutral") == "bullish")
                        management = self._adaptive_management_components(Side.LONG, last_close, stop, target, style="trend", runner_allowed=runner_allowed, continuation_bias=float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0), strong_setup=bool(effective_target_rr > target_rr))
                        final_priority_score = float(c.activity_score) + (max(0.0, last_ret5) * 50.0) + (max(0.0, last_ret15) * 100.0) + structure_bonus + pattern_bonus + adjustments["entry_context_adjustment"] + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
                        metadata["final_priority_score"] = round(final_priority_score, 4)
                        metadata.update(adjustments)
                        metadata.update(fvg_adjustments)
                        metadata.update(management)
                        metadata.update(retest_plan.get("metadata", {}))
                        metadata.update(self._technical_lists(tech_ctx))
                        reason = "rth_trend_pullback_long_fvg_retest" if str(retest_plan.get("status", "none") or "none") == "allow" else "rth_trend_pullback_long"
                        if ctx.matched_bullish_continuation:
                            reason += f":{'+'.join(sorted(ctx.matched_bullish_continuation))}"
                        out.append(Signal(symbol=c.symbol, strategy=self.strategy_name, side=Side.LONG, reason=reason, stop_price=stop, target_price=target, metadata=metadata))
                        self._record_entry_decision(c.symbol, "signal", [reason])
                        continue
            else:
                if not allow_short:
                    self._record_entry_decision(c.symbol, "skipped", ["shorts_disabled"])
                    continue
                short_res_ref = max(last_vwap, last_ema20)
                bearish_trigger = bool(self._structure_event_recent(getattr(ms_ctx, "bos_down_age_bars", None)) and getattr(ms_ctx, "bos_down", False)) or last_close < trigger_low
                retest_plan = self._continuation_fvg_retest_plan(Side.SHORT, c.symbol, frame, data, trigger_level=trigger_low, breakout_active=bool(bearish_trigger), close=last_close, vwap=last_vwap, ema9=last_ema9)
                pullback_hold_ok = pullback_high <= (short_res_ref * (1.0 + support_hold_pct)) if short_res_ref > 0 else True
                if day_strength > -min_change:
                    reasons.append(_reason_with_values("weak_day_weakness", current=day_strength, required=-min_change, op="<=", digits=4))
                if last_close >= last_vwap:
                    reasons.append(_reason_with_values("above_vwap", current=last_close, required=last_vwap, op="<", digits=4))
                if last_ema9 > last_ema20:
                    reasons.append(_reason_with_values("ema9_above_ema20", current=last_ema9, required=last_ema20, op="<=", digits=4))
                if last_ret5 > -trend_min_ret5:
                    reasons.append(_reason_with_values("weak_ret5", current=last_ret5, required=-trend_min_ret5, op="<=", digits=4))
                if last_ret15 > -trend_min_ret15:
                    reasons.append(_reason_with_values("weak_ret15", current=last_ret15, required=-trend_min_ret15, op="<=", digits=4))
                if extension_pct > max_extension:
                    reasons.append(_reason_with_values("too_extended_from_vwap", current=extension_pct, required=max_extension, op="<=", digits=4))
                if not pullback_hold_ok:
                    reasons.append(_reason_with_values("bounce_lost_resistance", current=pullback_high, required=short_res_ref * (1.0 + support_hold_pct), op="<=", digits=4))
                if not bearish_trigger:
                    reasons.append(_reason_with_values("no_reexpansion_trigger", current=last_close, required=trigger_low, op="<", digits=4))
                if close_pos > (1.0 - min_bar_close_position):
                    reasons.append(_reason_with_values("weak_bar_close", current=close_pos, required=1.0 - min_bar_close_position, op="<=", digits=4))
                if not reasons and self._shared_entry_enabled("use_opposing_chart_filter", True) and self._blocks_bearish_entry(ctx):
                    reasons.append("chart_pattern_opposed")
                reasons = self._apply_continuation_fvg_retest_plan(reasons, retest_plan, deferrable_prefixes={"too_extended_from_vwap", "no_reexpansion_trigger", "weak_bar_close", "too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "lower_wick_rejection", "expansion_bar_too_large"})
                if not reasons:
                    reasons.extend(self._entry_exhaustion_reasons(Side.SHORT, frame, close=last_close, vwap=last_vwap, ema9=last_ema9))
                    reasons = self._apply_continuation_fvg_retest_plan(reasons, retest_plan, deferrable_prefixes={"too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "lower_wick_rejection", "expansion_bar_too_large"})
                if not reasons:
                    divergence_reason = self._dual_counter_divergence_reason(Side.SHORT, tech_ctx)
                    if divergence_reason:
                        reasons.append(divergence_reason)
                if not reasons:
                    stop = max(pullback_high, short_res_ref * (1.0 + support_hold_pct)) if short_res_ref > 0 else pullback_high
                    stop = max(stop, last_close * (1.0 + self.config.risk.default_stop_pct))
                    risk_per_share = max(0.01, stop - last_close)
                    effective_target_rr = target_rr
                    if bool(self.params.get("strong_trend_runner_enabled", True)) and getattr(ms_ctx, "bias", "neutral") == "bearish" and bool(getattr(ms_ctx, "bos_down", False)) and ctx.matched_bearish_continuation:
                        effective_target_rr = max(target_rr, float(self.params.get("strong_trend_target_rr", target_rr + 0.3)))
                    target = max(0.01, last_close - risk_per_share * effective_target_rr)
                    if self._blocks_bearish_structure_entry(ms_ctx):
                        reasons.append(self._bearish_structure_block_reason(ms_ctx))
                    elif self._blocks_bearish_sr_entry(sr_ctx):
                        reasons.append(self._bearish_sr_block_reason(sr_ctx))
                    else:
                        stop, target = self._refine_bearish_sr_levels(last_close, stop, target, sr_ctx)
                        stop, target = self._refine_bearish_technical_levels(last_close, stop, target, tech_ctx, frame)
                        stop = self._apply_retest_stop_anchor(Side.SHORT, last_close, stop, retest_plan)
                        structure_bonus = 0.75 if getattr(ms_ctx, "bias", "neutral") == "bearish" else 0.0
                        if bool(getattr(ms_ctx, "bos_down", False)) and self._structure_event_recent(getattr(ms_ctx, "bos_down_age_bars", None)):
                            structure_bonus += 0.5
                        pattern_bonus = 0.35 if ctx.matched_bearish_continuation else 0.15 if ctx.matched_bearish_reversal else 0.0
                        adjustments = self._entry_adjustment_components(Side.SHORT, sr_ctx=sr_ctx, tech_ctx=tech_ctx)
                        fvg_adjustments = self._fvg_entry_adjustment_components(Side.SHORT, c.symbol, frame, data)
                        runner_allowed = bool((effective_target_rr > target_rr or float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0) >= 0.35) and getattr(ms_ctx, "bias", "neutral") == "bearish")
                        management = self._adaptive_management_components(Side.SHORT, last_close, stop, target, style="trend", runner_allowed=runner_allowed, continuation_bias=float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0), strong_setup=bool(effective_target_rr > target_rr))
                        final_priority_score = float(c.activity_score) + (max(0.0, -last_ret5) * 50.0) + (max(0.0, -last_ret15) * 100.0) + structure_bonus + pattern_bonus + adjustments["entry_context_adjustment"] + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
                        metadata["final_priority_score"] = round(final_priority_score, 4)
                        metadata.update(adjustments)
                        metadata.update(fvg_adjustments)
                        metadata.update(management)
                        metadata.update(retest_plan.get("metadata", {}))
                        metadata.update(self._technical_lists(tech_ctx))
                        reason = "rth_trend_pullback_short_fvg_retest" if str(retest_plan.get("status", "none") or "none") == "allow" else "rth_trend_pullback_short"
                        if ctx.matched_bearish_continuation:
                            reason += f":{'+'.join(sorted(ctx.matched_bearish_continuation))}"
                        out.append(Signal(symbol=c.symbol, strategy=self.strategy_name, side=Side.SHORT, reason=reason, stop_price=stop, target_price=target, metadata=metadata))
                        self._record_entry_decision(c.symbol, "signal", [reason])
                        continue
            self._record_entry_decision(c.symbol, "skipped", reasons or ["no_setup"])
        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
