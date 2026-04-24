# SPDX-License-Identifier: MIT
import logging
import math
from zoneinfo import ZoneInfo

from ..shared import (
    Candidate,
    Position,
    Side,
    Signal,
    datetime,
    now_et,
    pd,
    time,
    timedelta,
)
from ..strategy_base import BaseStrategy

_ET_ZONE = ZoneInfo("America/New_York")

LOG = logging.getLogger(__name__)
_VALID_ORB_WATCHLIST_MODES = {"none", "premarket", "early_session"}

class ORBStrategy(BaseStrategy):
    strategy_name = 'opening_range_breakout'

    @classmethod
    def normalize_params(cls, params: dict[str, object]) -> dict[str, object]:
        out = super().normalize_params(params)
        mode = str(out.get("orb_watchlist_mode", "premarket")).strip().lower()
        if mode not in _VALID_ORB_WATCHLIST_MODES:
            LOG.warning("Unsupported ORB orb_watchlist_mode=%r; using 'premarket'. Valid values: %s", mode, sorted(_VALID_ORB_WATCHLIST_MODES))
            mode = "premarket"
        out["orb_watchlist_mode"] = mode
        return out
    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        min_bars = int(self.params.get("min_bars", 30) or 30)
        opening_range_minutes = int(self.params.get("opening_range_minutes", 5))
        buffer_pct = float(self.params.get("min_breakout_buffer_pct", 0.0012))
        for c in candidates:
            reasons: list[str] = []
            frame = bars.get(c.symbol)
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue
            if frame is None or len(frame) < min_bars:
                self._record_entry_decision(c.symbol, "skipped", [self._insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), min_bars)])
                continue
            day = now_et().date()
            session = frame[self._same_day_mask(frame, day)]
            if len(session) < opening_range_minutes + 2:
                self._record_entry_decision(c.symbol, "skipped", [self._insufficient_bars_reason("opening_range_incomplete", len(session), opening_range_minutes + 2)])
                continue
            opening_start_time = time(9, 30)
            after_start_time = (datetime.combine(day, opening_start_time, tzinfo=_ET_ZONE) + timedelta(minutes=max(0, opening_range_minutes))).time()
            times_series = session.index.to_series().map(lambda ts: ts.time())
            opening_mask = (times_series >= opening_start_time) & (times_series < after_start_time)
            opening = session[opening_mask.to_numpy()]
            after = session[self._time_gte_mask(session, after_start_time)]
            if opening.empty or after.empty:
                self._record_entry_decision(
                    c.symbol,
                    "skipped",
                    [
                        self._reason_with_values(
                            "opening_range_incomplete",
                            current=len(opening),
                            required=1,
                            op=">=",
                            digits=0,
                            extras={"after_bars": (len(after), ">=", 1)},
                        )
                    ],
                )
                continue
            or_high = float(opening["high"].max())
            or_low = float(opening["low"].min())
            if math.isnan(or_high) or math.isnan(or_low):
                self._record_entry_decision(c.symbol, "skipped", ["opening_range_values_nan"])
                continue
            last = after.iloc[-1]
            trigger = or_high * (1.0 + buffer_pct)
            ctx = self._chart_context(frame)
            sr_ctx = self._sr_context(c.symbol, frame, data)
            ms_ctx = self._structure_context(frame, "1m")
            tech_ctx = self._technical_context(frame)
            pattern_ok = bool(ctx.matched_bullish_continuation or ctx.matched_bullish_reversal) or ctx.bias_score >= 0.0
            last_close = self._safe_float(last["close"])
            last_vwap = self._safe_float(last["vwap"], last_close)
            last_ema9 = self._safe_float(last["ema9"], last_close)
            last_ema20 = self._safe_float(last["ema20"], last_close)
            retest_plan = self._continuation_fvg_retest_plan(Side.LONG, c.symbol, frame, data, trigger_level=trigger, breakout_active=bool(last_close > trigger), close=last_close, vwap=last_vwap, ema9=last_ema9)
            if last_close <= trigger:
                reasons.append(self._reason_with_values("no_orb_breakout", current=last_close, required=trigger, op=">", digits=4))
            if last_close <= last_vwap:
                reasons.append(self._reason_with_values("below_vwap", current=last_close, required=last_vwap, op=">", digits=4))
            if last_ema9 < last_ema20:
                reasons.append(self._reason_with_values("ema9_below_ema20", current=last_ema9, required=last_ema20, op=">=", digits=4))
            if not pattern_ok:
                reasons.append("chart_pattern_not_supportive")
            if not reasons and self._shared_entry_enabled("use_opposing_chart_filter", True) and self._blocks_bullish_entry(ctx):
                reasons.append("chart_pattern_opposed")
            reasons = self._apply_continuation_fvg_retest_plan(reasons, retest_plan, deferrable_prefixes={"no_orb_breakout", "too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "upper_wick_rejection", "expansion_bar_too_large"})
            if not reasons:
                reasons.extend(self._entry_exhaustion_reasons(Side.LONG, frame, close=last_close, vwap=last_vwap, ema9=last_ema9))
                reasons = self._apply_continuation_fvg_retest_plan(reasons, retest_plan, deferrable_prefixes={"too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "upper_wick_rejection", "expansion_bar_too_large"})
            if not reasons:
                divergence_reason = self._dual_counter_divergence_reason(Side.LONG, tech_ctx)
                if divergence_reason:
                    reasons.append(divergence_reason)
            if not reasons:
                stop = or_low
                # Adaptive target RR: base 2.0, extended to 2.5 when the
                # breakout is visibly strong (>1.5% above trigger) AND HTF
                # structure bias confirms bullish. Non-restrictive — never
                # TIGHTENS the target, only lets strong breakouts run farther.
                base_rr = 2.0
                preview_breakout_pct = max(0.0, (last_close - trigger) / trigger) if trigger > 0 else 0.0
                preview_ms_bias = getattr(ms_ctx, "bias", "neutral")
                if preview_breakout_pct >= 0.015 and preview_ms_bias == "bullish":
                    base_rr = 2.5
                target = last_close + (last_close - stop) * base_rr
                if self._blocks_bullish_structure_entry(ms_ctx):
                    reasons.append(self._bullish_structure_block_reason(ms_ctx))
                elif self._blocks_bullish_sr_entry(sr_ctx):
                    reasons.append(self._bullish_sr_block_reason(sr_ctx))
                else:
                    stop, target = self._refine_bullish_sr_levels(last_close, stop, target, sr_ctx)
                    stop, target = self._refine_bullish_technical_levels(last_close, stop, target, tech_ctx, frame)
                    stop = self._apply_retest_stop_anchor(Side.LONG, last_close, stop, retest_plan)
                    breakout_pct = max(0.0, (last_close - trigger) / trigger) if trigger > 0 else 0.0
                    ms_bias = getattr(ms_ctx, "bias", "neutral")
                    structure_bonus = 0.75 if ms_bias == "bullish" else 0.0
                    pattern_bonus = 0.35 if ctx.matched_bullish_continuation else 0.15 if ctx.matched_bullish_reversal else 0.0
                    adjustments = self._entry_adjustment_components(Side.LONG, sr_ctx=sr_ctx, tech_ctx=tech_ctx)
                    fvg_adjustments = self._fvg_entry_adjustment_components(Side.LONG, c.symbol, frame, data)
                    fvg_continuation_bias = float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0)
                    runner_allowed = bool(fvg_continuation_bias >= 0.35 and (ctx.matched_bullish_continuation or ms_bias == "bullish"))
                    management = self._adaptive_management_components(Side.LONG, last_close, stop, target, style="breakout", runner_allowed=runner_allowed, continuation_bias=fvg_continuation_bias)
                    final_priority_score = float(c.activity_score) + (breakout_pct * 200.0) + structure_bonus + pattern_bonus + adjustments["entry_context_adjustment"] + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
                    metadata = self._build_signal_metadata(
                        entry_price=last_close,
                        chart_ctx=ctx, ms_ctx=ms_ctx, sr_ctx=sr_ctx, tech_ctx=tech_ctx,
                        adjustments=adjustments, fvg_adjustments=fvg_adjustments,
                        management=management, retest_plan=retest_plan,
                        final_priority_score=final_priority_score,
                        leading={"or_high": or_high, "or_low": or_low},
                    )
                    reason = "smallcap_orb_fvg_retest" if str(retest_plan.get("status", "none") or "none") == "allow" else "smallcap_orb_breakout"
                    if ctx.matched_bullish_continuation:
                        reason += f":{'+'.join(sorted(ctx.matched_bullish_continuation))}"
                    out.append(Signal(symbol=c.symbol, strategy=self.strategy_name, side=Side.LONG, reason=reason, stop_price=stop, target_price=target, metadata=metadata))
                    self._record_entry_decision(c.symbol, "signal", [reason])
                    continue
            self._record_entry_decision(c.symbol, "skipped", reasons or ["no_setup"])
        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
