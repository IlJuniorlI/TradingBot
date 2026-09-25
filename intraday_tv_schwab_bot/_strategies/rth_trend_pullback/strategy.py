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
from ..shared_entry import EntryContexts, EntryProposal, RetestTrigger
from ..strategy_base import BaseStrategy

# The pending reasons an FVG retest of the re-expansion trigger may clear,
# per side: no re-expansion yet, a stretched or weak trigger bar, or the
# anti-chase exhaustion checks (the wick check is side-specific). Until
# 2026-09-24 two passes cleared them -- the own reasons first, the
# exhaustion ones only once nothing else was pending -- and one pass over
# the union decides the same.
_RETEST_DEFERRABLE = {
    Side.LONG: frozenset({
        "too_extended_from_vwap", "no_reexpansion_trigger", "weak_bar_close",
        "too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "upper_wick_rejection", "expansion_bar_too_large",
    }),
    Side.SHORT: frozenset({
        "too_extended_from_vwap", "no_reexpansion_trigger", "weak_bar_close",
        "too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "lower_wick_rejection", "expansion_bar_too_large",
    }),
}


class RTHTrendPullbackStrategy(BaseStrategy):
    """Regular-session trend pullback re-entry, on the candidate's side.

    One proposal per candidate on its directional bias (style / family
    ``pullback``): the setup's own blockers and the anti-chase exhaustion
    checks are its pending reasons, and an FVG retest of the re-expansion
    trigger may clear the stretched / weak-trigger / exhaustion ones. The
    stop sits beyond the pullback extreme and the VWAP / EMA20 support
    (at least ``default_stop_pct`` away), the target at ``target_rr``
    (``strong_trend_target_rr`` on a structure-confirmed continuation).
    Every shared_entry knob -- the vetoes, the refinement, the retest stop
    anchor, the score terms -- is applied by ``self.entry_policy.admit``.
    The ``pullback`` family puts the entry under the exit side's pullback
    structure grace (``support_resistance.structure_exit_grace_minutes_
    pullback``), which until 2026-09-24 covered only top_tier's pullback
    regime.
    """

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
        strong_trend_runner_enabled = bool(self.params.get("strong_trend_runner_enabled", True))
        strong_trend_target_rr = float(self.params.get("strong_trend_target_rr", target_rr + 0.3))
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
            ms_ctx = self._structure_context(frame, "ltf")
            if directional_bias == Side.LONG:
                side = Side.LONG
                support_ref = min(last_vwap, last_ema20)
                trigger_level = trigger_high
                trigger_fired = bool(self._structure_event_recent(getattr(ms_ctx, "bos_up_age_bars", None)) and getattr(ms_ctx, "bos_up", False)) or last_close > trigger_high
                pullback_hold_ok = pullback_low >= (support_ref * (1.0 - support_hold_pct)) if support_ref > 0 else True
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
                    reasons.append(_reason_with_values("pullback_lost_support", current=pullback_low, required=support_ref * (1.0 - support_hold_pct), op=">=", digits=4))
                if not trigger_fired:
                    reasons.append(_reason_with_values("no_reexpansion_trigger", current=last_close, required=trigger_high, op=">", digits=4))
                if close_pos < min_bar_close_position:
                    reasons.append(_reason_with_values("weak_bar_close", current=close_pos, required=min_bar_close_position, op=">=", digits=4))
                stop = min(pullback_low, support_ref * (1.0 - support_hold_pct)) if support_ref > 0 else pullback_low
                stop = min(stop, last_close * (1.0 - self.config.risk.default_stop_pct))
                risk_per_share = max(0.01, last_close - stop)
                strong_trend = getattr(ms_ctx, "bias", "neutral") == "bullish" and bool(getattr(ms_ctx, "bos_up", False)) and bool(ctx.matched_bullish_continuation)
            else:
                if not allow_short:
                    self._record_entry_decision(c.symbol, "skipped", ["shorts_disabled"])
                    continue
                side = Side.SHORT
                support_ref = max(last_vwap, last_ema20)
                trigger_level = trigger_low
                trigger_fired = bool(self._structure_event_recent(getattr(ms_ctx, "bos_down_age_bars", None)) and getattr(ms_ctx, "bos_down", False)) or last_close < trigger_low
                pullback_hold_ok = pullback_high <= (support_ref * (1.0 + support_hold_pct)) if support_ref > 0 else True
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
                    reasons.append(_reason_with_values("bounce_lost_resistance", current=pullback_high, required=support_ref * (1.0 + support_hold_pct), op="<=", digits=4))
                if not trigger_fired:
                    reasons.append(_reason_with_values("no_reexpansion_trigger", current=last_close, required=trigger_low, op="<", digits=4))
                if close_pos > (1.0 - min_bar_close_position):
                    reasons.append(_reason_with_values("weak_bar_close", current=close_pos, required=1.0 - min_bar_close_position, op="<=", digits=4))
                stop = max(pullback_high, support_ref * (1.0 + support_hold_pct)) if support_ref > 0 else pullback_high
                stop = max(stop, last_close * (1.0 + self.config.risk.default_stop_pct))
                risk_per_share = max(0.01, stop - last_close)
                strong_trend = getattr(ms_ctx, "bias", "neutral") == "bearish" and bool(getattr(ms_ctx, "bos_down", False)) and bool(ctx.matched_bearish_continuation)
            reasons.extend(self._entry_exhaustion_reasons(side, frame, close=last_close, vwap=last_vwap, ema9=last_ema9))
            effective_target_rr = target_rr
            if strong_trend_runner_enabled and strong_trend:
                effective_target_rr = max(target_rr, strong_trend_target_rr)
            if side == Side.LONG:
                target = last_close + risk_per_share * effective_target_rr
            else:
                target = max(0.01, last_close - risk_per_share * effective_target_rr)
            proposal = EntryProposal(
                candidate=c,
                direction=side,
                style="pullback",
                style_family="pullback",
                close=last_close,
                stop=stop,
                target=target,
                gate_frame=frame,
                sr_frame=frame,
                level_frame=frame,
                data=data,
                pending_reasons=tuple(reasons),
                deferrable=_RETEST_DEFERRABLE[side],
                retest=RetestTrigger(trigger_level, bool(trigger_fired), last_vwap, last_ema9),
                contexts=EntryContexts(ms=ms_ctx, chart=ctx),
            )
            admitted = self.entry_policy.admit(proposal)
            if admitted is None:
                refusal = self._consume_build_failure_payload(c.symbol, proposal.style)
                self._record_entry_decision(c.symbol, "skipped", refusal["reasons"])
                continue
            long = side == Side.LONG
            with_bias = getattr(ms_ctx, "bias", "neutral") == ("bullish" if long else "bearish")
            structure_bonus = 0.75 if with_bias else 0.0
            bos_active = bool(getattr(ms_ctx, "bos_up" if long else "bos_down", False))
            if bos_active and self._structure_event_recent(getattr(ms_ctx, "bos_up_age_bars" if long else "bos_down_age_bars", None)):
                structure_bonus += 0.5
            continuation = ctx.matched_bullish_continuation if long else ctx.matched_bearish_continuation
            reversal = ctx.matched_bullish_reversal if long else ctx.matched_bearish_reversal
            pattern_bonus = 0.35 if continuation else 0.15 if reversal else 0.0
            fvg_continuation_bias = float(admitted.fvg["fvg_continuation_bias"])
            strong_setup = bool(effective_target_rr > target_rr)
            runner_allowed = bool((strong_setup or fvg_continuation_bias >= 0.35) and with_bias)
            management = self._adaptive_management_components(
                side, last_close, admitted.stop, admitted.target,
                style="trend", runner_allowed=runner_allowed, continuation_bias=fvg_continuation_bias, strong_setup=strong_setup,
            )
            ret5_term = max(0.0, last_ret5 if long else -last_ret5)
            ret15_term = max(0.0, last_ret15 if long else -last_ret15)
            strategy_score = float(c.activity_score) + (ret5_term * 50.0) + (ret15_term * 100.0) + structure_bonus + pattern_bonus
            reason = f"rth_trend_pullback_{'long' if long else 'short'}"
            if admitted.admitted_via_retest:
                reason += "_fvg_retest"
            if continuation:
                reason += f":{'+'.join(sorted(continuation))}"
            out.append(self.entry_policy.emit(
                admitted, reason=reason, strategy_score=strategy_score, management=management, target=admitted.target,
                metadata={
                    "trigger_high": trigger_high,
                    "trigger_low": trigger_low,
                    "support_low": support_low,
                    "resistance_high": resistance_high,
                    "pullback_low": pullback_low,
                    "pullback_high": pullback_high,
                    "extension_from_vwap_pct": extension_pct,
                },
            ))
            self._record_entry_decision(c.symbol, "signal", [reason])
        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
