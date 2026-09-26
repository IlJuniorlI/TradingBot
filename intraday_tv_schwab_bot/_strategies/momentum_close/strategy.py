# SPDX-License-Identifier: MIT
import pandas as pd

from ...models import Candidate, Position, Side, Signal
from ...numeric import safe_float
from ...reasons import insufficient_bars_reason, reason_with_values
from ..shared_entry import EntryContexts, EntryProposal, RetestTrigger
from ..strategy_base import BaseStrategy

# The pending reasons an FVG retest of the breakout level may clear: price
# not through the level yet, or through it but stretched (the anti-chase
# exhaustion checks). Until 2026-09-24 two passes cleared them -- the own
# reasons first, the exhaustion ones only once nothing else was pending --
# and one pass over the union decides the same.
_RETEST_DEFERRABLE = frozenset({
    "no_breakout",
    "too_extended_from_vwap_atr",
    "too_extended_from_ema9_atr",
    "upper_wick_rejection",
    "expansion_bar_too_large",
})


class MomentumIntoCloseStrategy(BaseStrategy):
    """Late-day long-only continuation breakout in the day's leaders.

    One LONG proposal per candidate (style / family ``momentum``): the
    setup's own blockers and the anti-chase exhaustion checks are its pending
    reasons, and an FVG retest of the breakout level may clear the breakout /
    exhaustion ones. The stop sits under the recent swing low (never wider
    than ``default_stop_pct``), the target at ``default_target_pct``. Every
    shared_entry knob -- the vetoes, the refinement, the retest stop anchor,
    the score terms -- is applied by ``self.entry_policy.admit``.
    """

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
            breakout_level = float(recent["high"].max())
            last_close = safe_float(last["close"], 0.0)
            breakout = last_close > breakout_level
            day_strength = safe_float(c.metadata.get("change_from_open"), 0.0)
            ctx = self._chart_context(frame)
            pattern_ok = bool(ctx.matched_bullish_continuation or ctx.matched_bullish_reversal) or ctx.bias_score >= 0.0
            last_vwap = safe_float(last["vwap"], last_close)
            last_ret15 = safe_float(last["ret15"], 0.0)
            last_ema9 = safe_float(last["ema9"], last_close)
            last_ema20 = safe_float(last["ema20"], last_close)
            if not breakout:
                reasons.append(reason_with_values("no_breakout", current=last_close, required=breakout_level, op=">", digits=4))
            if day_strength < min_day_strength:
                reasons.append(reason_with_values("weak_day_strength", current=day_strength, required=min_day_strength, op=">=", digits=4))
            if last_close <= last_vwap:
                reasons.append(reason_with_values("below_vwap", current=last_close, required=last_vwap, op=">", digits=4))
            if last_ret15 <= 0:
                reasons.append(reason_with_values("weak_ret15", current=last_ret15, required=0.0, op=">", digits=4))
            if last_ema9 < last_ema20:
                reasons.append(reason_with_values("ema9_below_ema20", current=last_ema9, required=last_ema20, op=">=", digits=4))
            if not pattern_ok:
                reasons.append("chart_pattern_not_supportive")
            reasons.extend(self._entry_exhaustion_reasons(Side.LONG, frame, close=last_close, vwap=last_vwap, ema9=last_ema9))
            # ATR-anchored stop: rebase below the recent swing low by 8% of
            # ATR so noisy single-bar wicks don't trigger the stop on an
            # otherwise valid breakout. Still bounded by the default_stop_pct
            # floor so we never risk more than the configured percentage.
            # Non-restrictive — only LOOSENS the stop slightly on
            # high-conviction momentum setups.
            last_atr = safe_float(last.get("atr14"), 0.0)
            swing_low = float(recent["low"].min())
            if last_atr > 0:
                swing_low = swing_low - (last_atr * 0.08)
            proposal = EntryProposal(
                candidate=c,
                direction=Side.LONG,
                style="momentum",
                style_family="momentum",
                close=last_close,
                stop=max(last_close * (1.0 - self.config.risk.default_stop_pct), swing_low),
                target=last_close * (1.0 + self.config.risk.default_target_pct),
                gate_frame=frame,
                sr_frame=frame,
                level_frame=frame,
                data=data,
                pending_reasons=tuple(reasons),
                deferrable=_RETEST_DEFERRABLE,
                retest=RetestTrigger(breakout_level, bool(breakout), last_vwap, last_ema9),
                contexts=EntryContexts(chart=ctx),
            )
            admitted = self.entry_policy.admit(proposal)
            if admitted is None:
                refusal = self._consume_build_failure_payload(c.symbol, proposal.style)
                self._record_entry_decision(c.symbol, "skipped", refusal["reasons"])
                continue
            breakout_pct = max(0.0, (last_close - breakout_level) / breakout_level) if breakout_level > 0 else 0.0
            ms_bias = getattr(admitted.ms, "bias", "neutral")
            structure_bonus = 0.75 if ms_bias == "bullish" else 0.0
            if bool(getattr(admitted.ms, "bos_up", False)) and self._structure_event_recent(getattr(admitted.ms, "bos_up_age_bars", None)):
                structure_bonus += 0.5
            pattern_bonus = 0.35 if ctx.matched_bullish_continuation else 0.15 if ctx.matched_bullish_reversal else 0.0
            fvg_continuation_bias = float(admitted.fvg["fvg_continuation_bias"])
            runner_allowed = bool(fvg_continuation_bias >= 0.35 and (ctx.matched_bullish_continuation or ms_bias == "bullish"))
            management = self._adaptive_management_components(
                Side.LONG, last_close, admitted.stop, admitted.target,
                style="momentum", runner_allowed=runner_allowed, continuation_bias=fvg_continuation_bias,
            )
            strategy_score = float(c.activity_score) + (max(0.0, last_ret15) * 100.0) + (breakout_pct * 200.0) + structure_bonus + pattern_bonus
            reason = "smallcap_breakout_fvg_retest" if admitted.admitted_via_retest else "smallcap_breakout_above_vwap"
            if ctx.matched_bullish_continuation:
                reason += f":{'+'.join(sorted(ctx.matched_bullish_continuation))}"
            out.append(self.entry_policy.emit(
                admitted, reason=reason, strategy_score=strategy_score, management=management, target=admitted.target,
            ))
            self._record_entry_decision(c.symbol, "signal", [reason])
        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
