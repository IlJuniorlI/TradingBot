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
    math,
    pd,
)
from ..strategy_base import BaseStrategy

class VolatilitySqueezeBreakoutStrategy(BaseStrategy):
    strategy_name = 'volatility_squeeze_breakout'

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        min_bars = int(self.params.get("min_bars", 60))
        squeeze_lookback = int(self.params.get("squeeze_lookback_bars", 16))
        baseline_bars = max(8, int(self.params.get("squeeze_baseline_bars", 20)))
        return max(min_bars, squeeze_lookback + baseline_bars + 25)
    @staticmethod
    def _safe_series_median(series: pd.Series, fallback: float = 0.0) -> float:
        clean = pd.Series(pd.to_numeric(series, errors="coerce"), index=series.index, copy=False).dropna()
        if clean.empty:
            return float(fallback)
        try:
            return float(clean.median())
        except Exception:
            return float(fallback)

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        min_change = float(self.params.get("min_change_from_open", 0.9))
        min_bars = int(self.params.get("min_bars", 60))
        squeeze_lookback = max(6, int(self.params.get("squeeze_lookback_bars", 12)))
        baseline_bars = max(8, int(self.params.get("squeeze_baseline_bars", 20)))
        max_range_pct = float(self.params.get("max_squeeze_range_pct", 0.011))
        max_range_atr = float(self.params.get("max_squeeze_range_atr", 1.8))
        max_width_pct = float(self.params.get("max_squeeze_width_pct", 0.05))
        max_width_ratio = float(self.params.get("max_squeeze_width_ratio", 0.74))
        breakout_buffer_pct = float(self.params.get("breakout_buffer_pct", 0.0008))
        min_close_pos = float(self.params.get("min_bar_close_position", 0.63))
        min_breakout_volume_ratio = float(self.params.get("min_breakout_volume_ratio", 1.12))
        min_atr_expansion_mult = float(self.params.get("min_atr_expansion_mult", 1.00))
        min_pressure_drift_pct = float(self.params.get("min_pressure_drift_pct", 0.0011))
        require_vwap_alignment = bool(self.params.get("require_vwap_alignment", True))
        require_avwap_alignment = bool(self.params.get("require_avwap_alignment", True))
        prefer_bollinger_flag = bool(self.params.get("prefer_bollinger_squeeze_flag", True))
        target_rr = max(1.0, float(self.params.get("target_rr", 2.05)))
        runner_enabled = bool(self.params.get("runner_enabled", True))
        runner_target_rr = max(target_rr, float(self.params.get("runner_target_rr", target_rr + 0.35)))
        allow_short = bool(self.config.risk.allow_short)
        history_bars = max(min_bars, squeeze_lookback + baseline_bars + 25)

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
            prior = frame.iloc[:-1]
            box_slice = prior.tail(squeeze_lookback)
            baseline_slice = prior.iloc[-(squeeze_lookback + baseline_bars):-squeeze_lookback] if len(prior) >= (squeeze_lookback + baseline_bars) else prior.head(0)
            if len(box_slice) < squeeze_lookback or len(baseline_slice) < baseline_bars:
                self._record_entry_decision(c.symbol, "skipped", [insufficient_bars_reason("insufficient_squeeze_history", len(prior), squeeze_lookback + baseline_bars)])
                continue

            last_close = _safe_float(last["close"])
            day_strength = _safe_float(c.metadata.get("change_from_open"), 0.0)
            last_vwap = _safe_float(last.get("vwap"), last_close)
            last_ema9 = _safe_float(last.get("ema9"), last_close)
            last_ema20 = _safe_float(last.get("ema20"), last_close)
            atr = max(_safe_float(last.get("atr14"), last_close * 0.0015), max(last_close * 0.0015, 0.01))
            close_pos = _bar_close_position(frame)
            breakout_high = _safe_float(box_slice["high"].max(), last_close)
            breakout_low = _safe_float(box_slice["low"].min(), last_close)
            box_range = max(0.0, breakout_high - breakout_low)
            box_range_pct = (box_range / last_close) if last_close > 0 else 0.0
            box_range_atr = (box_range / atr) if atr > 0 else math.inf
            volume_baseline = max(1.0, self._safe_series_median(box_slice["volume"], fallback=1.0))
            breakout_volume_ratio = _safe_float(last.get("volume"), 0.0) / volume_baseline
            pressure_split = max(2, squeeze_lookback // 2)
            first_half = box_slice.iloc[:pressure_split]
            second_half = box_slice.iloc[-pressure_split:]
            rising_lows_ok = _safe_float(second_half["low"].min(), breakout_low) >= _safe_float(first_half["low"].min(), breakout_low) * (1.0 + min_pressure_drift_pct)
            falling_highs_ok = _safe_float(second_half["high"].max(), breakout_high) <= _safe_float(first_half["high"].max(), breakout_high) * (1.0 - min_pressure_drift_pct)

            bb_len = int(self._technical_level_setting("bollinger_length", 20) or 20)
            bb_mult = float(self._technical_level_setting("bollinger_std_mult", 2.0) or 2.0)
            use_shared_bb_width = bb_len == 20 and abs(bb_mult - 2.0) <= 1e-9 and "bb_width_pct" in frame.columns
            if use_shared_bb_width:
                bb_width_pct = pd.Series(pd.to_numeric(frame["bb_width_pct"], errors="coerce"), index=frame.index, copy=False)
            else:
                # Manual rolling Bollinger fallback when bb_len != 20 or
                # bb_mult != 2.0 (shipped configs always use 20/2.0 so this
                # branch is dead code in production). Kept on pandas instead
                # of talib_bbands because TA-Lib's BBANDS poisons the entire
                # output once it sees a NaN in close, while pandas rolling
                # recovers as soon as the window moves past the NaN — same
                # NaN-tolerance the rest of this strategy assumes.
                mid = pd.Series(pd.to_numeric(frame["close"], errors="coerce"), index=frame.index, copy=False).rolling(bb_len, min_periods=bb_len).mean()
                std = pd.Series(pd.to_numeric(frame["close"], errors="coerce"), index=frame.index, copy=False).rolling(bb_len, min_periods=bb_len).std(ddof=0)
                upper = mid + (bb_mult * std)
                lower = mid - (bb_mult * std)
                bb_width_pct = ((upper - lower) / mid.abs().replace(0.0, pd.NA)).astype(float)
            box_width_pct = self._safe_series_median(bb_width_pct.reindex(box_slice.index), fallback=max_width_pct * 2.0)
            baseline_width_pct = self._safe_series_median(bb_width_pct.reindex(baseline_slice.index), fallback=box_width_pct)
            width_ratio = (box_width_pct / baseline_width_pct) if baseline_width_pct > 0 else math.inf

            ctx = self._chart_context(frame)
            sr_ctx = self._sr_context(c.symbol, frame, data)
            ms_ctx = self._structure_context(frame, "1m")
            tech_ctx = self._technical_context(frame)
            metadata = {
                "squeeze_breakout_high": breakout_high,
                "squeeze_breakout_low": breakout_low,
                "squeeze_range": box_range,
                "squeeze_range_pct": box_range_pct,
                "squeeze_range_atr": box_range_atr,
                "squeeze_width_pct": box_width_pct,
                "squeeze_width_ratio": width_ratio,
                "breakout_volume_ratio": breakout_volume_ratio,
                "compression_rising_lows": bool(rising_lows_ok),
                "compression_falling_highs": bool(falling_highs_ok),
                **self._chart_lists(ctx),
                **self._structure_lists(ms_ctx, prefix="ms1m"),
                **self._sr_lists(sr_ctx),
                **self._technical_lists(tech_ctx),
            }

            compression_ok = bool(
                box_range_pct <= max_range_pct
                and box_range_atr <= max_range_atr
                and box_width_pct <= max_width_pct
                and (
                    width_ratio <= max_width_ratio
                    or box_width_pct <= (max_width_pct * 0.70)
                    or bool(getattr(tech_ctx, "bollinger_squeeze", False))
                )
            )
            if not compression_ok:
                reasons.append(_reason_with_values("no_valid_squeeze", current=box_range_pct, required=max_range_pct, op="<=", digits=4, extras={"range_atr": (box_range_atr, "<=", max_range_atr), "width_pct": (box_width_pct, "<=", max_width_pct), "width_ratio": (width_ratio, "<=", max_width_ratio)}))
            if breakout_volume_ratio < min_breakout_volume_ratio:
                reasons.append(_reason_with_values("breakout_volume_too_light", current=breakout_volume_ratio, required=min_breakout_volume_ratio, op=">=", digits=4))
            if _safe_float(getattr(tech_ctx, "atr_expansion_mult", None), 0.0) < min_atr_expansion_mult:
                reasons.append(_reason_with_values("no_atr_expansion", current=_safe_float(getattr(tech_ctx, "atr_expansion_mult", None), 0.0), required=min_atr_expansion_mult, op=">=", digits=4))
            if prefer_bollinger_flag and not bool(getattr(tech_ctx, "bollinger_squeeze", False)) and box_width_pct > max_width_pct * 0.90:
                reasons.append("bollinger_squeeze_not_confirmed")

            signals: list[Signal] = []
            shared_reasons = list(reasons)

            long_reasons = list(shared_reasons)
            bullish_breakout = last_close >= breakout_high * (1.0 + breakout_buffer_pct)
            bullish_avwap = max(_safe_float(getattr(tech_ctx, "anchored_vwap_open", None), 0.0), _safe_float(getattr(tech_ctx, "anchored_vwap_bullish_impulse", None), 0.0))
            long_retest_plan = self._continuation_fvg_retest_plan(Side.LONG, c.symbol, frame, data, trigger_level=breakout_high, breakout_active=bool(bullish_breakout), close=last_close, vwap=last_vwap, ema9=last_ema9)
            if day_strength < min_change:
                long_reasons.append(_reason_with_values("weak_day_strength", current=day_strength, required=min_change, op=">=", digits=4))
            if require_vwap_alignment and last_close <= last_vwap:
                long_reasons.append(_reason_with_values("below_vwap", current=last_close, required=last_vwap, op=">", digits=4))
            if last_ema9 < last_ema20:
                long_reasons.append(_reason_with_values("ema9_below_ema20", current=last_ema9, required=last_ema20, op=">=", digits=4))
            if require_avwap_alignment and bullish_avwap > 0 and last_close <= bullish_avwap:
                long_reasons.append(_reason_with_values("below_bullish_avwap", current=last_close, required=bullish_avwap, op=">", digits=4))
            if not rising_lows_ok:
                long_reasons.append("pressure_not_building_up")
            if not bullish_breakout:
                long_reasons.append(_reason_with_values("no_squeeze_breakout", current=last_close, required=breakout_high * (1.0 + breakout_buffer_pct), op=">=", digits=4))
            if close_pos < min_close_pos:
                long_reasons.append(_reason_with_values("weak_bar_close", current=close_pos, required=min_close_pos, op=">=", digits=4))
            if not long_reasons and self._shared_entry_enabled("use_opposing_chart_filter", True) and self._blocks_bullish_entry(ctx):
                long_reasons.append("chart_pattern_opposed")
            long_reasons = self._apply_continuation_zone_retest_plans(long_reasons, [long_retest_plan], deferrable_prefixes={"weak_bar_close", "no_squeeze_breakout", "too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "upper_wick_rejection", "expansion_bar_too_large"})
            if not long_reasons:
                long_reasons.extend(self._entry_exhaustion_reasons(Side.LONG, frame, close=last_close, vwap=last_vwap, ema9=last_ema9))
                long_reasons = self._apply_continuation_zone_retest_plans(long_reasons, [long_retest_plan], deferrable_prefixes={"too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "upper_wick_rejection", "expansion_bar_too_large"})
            if not long_reasons:
                divergence_reason = self._dual_counter_divergence_reason(Side.LONG, tech_ctx)
                if divergence_reason:
                    long_reasons.append(divergence_reason)
            if not long_reasons:
                # Compression-aware stop anchor: for TIGHT squeezes (narrow
                # box_range_pct), using pure ATR can over-widen the stop.
                # Scale the stop buffer by the compression width — tighter
                # squeezes get a proportionally tighter stop below the box,
                # wider squeezes keep the ATR floor.
                stop_buffer = max(atr * 0.12, last_close * 0.0010, box_range_pct * last_close * 0.22)
                stop = breakout_low - stop_buffer
                stop = min(stop, last_close * (1.0 - self.config.risk.default_stop_pct))
                risk_per_share = max(0.01, last_close - stop)
                effective_target_rr = runner_target_rr if runner_enabled and (bool(getattr(ms_ctx, "bos_up", False)) or bool(getattr(tech_ctx, "atr_expansion_mult", 0.0) >= (min_atr_expansion_mult + 0.12))) else target_rr
                target = last_close + risk_per_share * effective_target_rr
                if self._blocks_bullish_structure_entry(ms_ctx):
                    long_reasons.append(self._bullish_structure_block_reason(ms_ctx))
                elif self._blocks_bullish_sr_entry(sr_ctx):
                    long_reasons.append(self._bullish_sr_block_reason(sr_ctx))
                else:
                    stop, target = self._refine_bullish_sr_levels(last_close, stop, target, sr_ctx)
                    stop, target = self._refine_bullish_technical_levels(last_close, stop, target, tech_ctx, frame)
                    stop = self._apply_retest_stop_anchor(Side.LONG, last_close, stop, long_retest_plan)
                    adjustments = self._entry_adjustment_components(Side.LONG, sr_ctx=sr_ctx, tech_ctx=tech_ctx)
                    fvg_adjustments = self._fvg_entry_adjustment_components(Side.LONG, c.symbol, frame, data)
                    management = self._adaptive_management_components(Side.LONG, last_close, stop, target, style="trend", runner_allowed=bool(runner_enabled), continuation_bias=float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0))
                    final_priority_score = float(c.activity_score) + (0.45 if bool(getattr(tech_ctx, "bollinger_squeeze", False)) else 0.0) + max(0.0, 1.0 - min(1.0, width_ratio)) + max(0.0, breakout_volume_ratio - 1.0) + (0.35 if bool(getattr(ms_ctx, "bos_up", False)) else 0.0) + adjustments["entry_context_adjustment"] + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
                    reason = "volatility_squeeze_breakout_long"
                    meta = {**metadata, "final_priority_score": round(final_priority_score, 4), **adjustments, **fvg_adjustments, **management}
                    signals.append(Signal(symbol=c.symbol, strategy=self.strategy_name, side=Side.LONG, reason=reason, stop_price=float(stop), target_price=float(target), metadata=meta))

            short_reasons = list(shared_reasons)
            bearish_breakout = last_close <= breakout_low * (1.0 - breakout_buffer_pct)
            bearish_avwap_vals = [v for v in [_safe_float(getattr(tech_ctx, "anchored_vwap_open", None), 0.0), _safe_float(getattr(tech_ctx, "anchored_vwap_bearish_impulse", None), 0.0)] if v > 0]
            bearish_avwap = min(bearish_avwap_vals) if bearish_avwap_vals else 0.0
            short_retest_plan = self._continuation_fvg_retest_plan(Side.SHORT, c.symbol, frame, data, trigger_level=breakout_low, breakout_active=bool(bearish_breakout), close=last_close, vwap=last_vwap, ema9=last_ema9)
            if not allow_short:
                short_reasons.append("shorts_disabled")
            if day_strength > -min_change:
                short_reasons.append(_reason_with_values("weak_day_weakness", current=day_strength, required=-min_change, op="<=", digits=4))
            if require_vwap_alignment and last_close >= last_vwap:
                short_reasons.append(_reason_with_values("above_vwap", current=last_close, required=last_vwap, op="<", digits=4))
            if last_ema9 > last_ema20:
                short_reasons.append(_reason_with_values("ema9_above_ema20", current=last_ema9, required=last_ema20, op="<=", digits=4))
            if require_avwap_alignment and 0 < bearish_avwap <= last_close:
                short_reasons.append(_reason_with_values("above_bearish_avwap", current=last_close, required=bearish_avwap, op="<", digits=4))
            if not falling_highs_ok:
                short_reasons.append("pressure_not_building_down")
            if not bearish_breakout:
                short_reasons.append(_reason_with_values("no_squeeze_breakdown", current=last_close, required=breakout_low * (1.0 - breakout_buffer_pct), op="<=", digits=4))
            if close_pos > (1.0 - min_close_pos):
                short_reasons.append(_reason_with_values("weak_bar_close", current=1.0 - close_pos, required=min_close_pos, op=">=", digits=4))
            if not short_reasons and self._shared_entry_enabled("use_opposing_chart_filter", True) and self._blocks_bearish_entry(ctx):
                short_reasons.append("chart_pattern_opposed")
            short_reasons = self._apply_continuation_zone_retest_plans(short_reasons, [short_retest_plan], deferrable_prefixes={"weak_bar_close", "no_squeeze_breakdown", "too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "lower_wick_rejection", "expansion_bar_too_large"})
            if not short_reasons:
                short_reasons.extend(self._entry_exhaustion_reasons(Side.SHORT, frame, close=last_close, vwap=last_vwap, ema9=last_ema9))
                short_reasons = self._apply_continuation_zone_retest_plans(short_reasons, [short_retest_plan], deferrable_prefixes={"too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "lower_wick_rejection", "expansion_bar_too_large"})
            if not short_reasons:
                divergence_reason = self._dual_counter_divergence_reason(Side.SHORT, tech_ctx)
                if divergence_reason:
                    short_reasons.append(divergence_reason)
            if not short_reasons:
                # Symmetric compression-aware stop for SHORT side.
                stop_buffer_short = max(atr * 0.12, last_close * 0.0010, box_range_pct * last_close * 0.22)
                stop = breakout_high + stop_buffer_short
                stop = max(stop, last_close * (1.0 + self.config.risk.default_stop_pct))
                risk_per_share = max(0.01, stop - last_close)
                effective_target_rr = runner_target_rr if runner_enabled and (bool(getattr(ms_ctx, "bos_down", False)) or bool(getattr(tech_ctx, "atr_expansion_mult", 0.0) >= (min_atr_expansion_mult + 0.12))) else target_rr
                target = last_close - risk_per_share * effective_target_rr
                if self._blocks_bearish_structure_entry(ms_ctx):
                    short_reasons.append(self._bearish_structure_block_reason(ms_ctx))
                elif self._blocks_bearish_sr_entry(sr_ctx):
                    short_reasons.append(self._bearish_sr_block_reason(sr_ctx))
                else:
                    stop, target = self._refine_bearish_sr_levels(last_close, stop, target, sr_ctx)
                    stop, target = self._refine_bearish_technical_levels(last_close, stop, target, tech_ctx, frame)
                    stop = self._apply_retest_stop_anchor(Side.SHORT, last_close, stop, short_retest_plan)
                    adjustments = self._entry_adjustment_components(Side.SHORT, sr_ctx=sr_ctx, tech_ctx=tech_ctx)
                    fvg_adjustments = self._fvg_entry_adjustment_components(Side.SHORT, c.symbol, frame, data)
                    management = self._adaptive_management_components(Side.SHORT, last_close, stop, target, style="trend", runner_allowed=bool(runner_enabled), continuation_bias=float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0))
                    final_priority_score = float(c.activity_score) + (0.45 if bool(getattr(tech_ctx, "bollinger_squeeze", False)) else 0.0) + max(0.0, 1.0 - min(1.0, width_ratio)) + max(0.0, breakout_volume_ratio - 1.0) + (0.35 if bool(getattr(ms_ctx, "bos_down", False)) else 0.0) + adjustments["entry_context_adjustment"] + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
                    reason = "volatility_squeeze_breakout_short"
                    meta = {**metadata, "final_priority_score": round(final_priority_score, 4), **adjustments, **fvg_adjustments, **management}
                    signals.append(Signal(symbol=c.symbol, strategy=self.strategy_name, side=Side.SHORT, reason=reason, stop_price=float(stop), target_price=float(target), metadata=meta))

            if not signals:
                reason_stream = list(long_reasons) if not allow_short else (list(long_reasons) + list(short_reasons))
                merged: list[str] = []
                for token in reason_stream:
                    if token and token not in merged:
                        merged.append(token)
                self._record_entry_decision(c.symbol, "skipped", merged or ["no_setup"])
                continue

            best = max(signals, key=lambda sig: (float(sig.metadata.get("final_priority_score", 0.0) or 0.0), float(sig.metadata.get("breakout_volume_ratio", breakout_volume_ratio) or breakout_volume_ratio)))
            out.append(best)
            self._record_entry_decision(c.symbol, "signal", [best.reason])
        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
