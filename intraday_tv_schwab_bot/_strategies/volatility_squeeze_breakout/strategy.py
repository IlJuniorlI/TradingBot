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
from ..shared_entry import EntryContexts, EntryProposal, RetestTrigger
from ..strategy_base import BaseStrategy

# The pending reasons an FVG retest of the squeeze box edge may clear, per
# side: no break of the box yet, a weak trigger bar, or the anti-chase
# exhaustion checks (the wick check is side-specific). Until 2026-09-24 two
# passes cleared them -- the own reasons first, the exhaustion ones only
# once nothing else was pending -- and one pass over the union decides the
# same.
_RETEST_DEFERRABLE = {
    Side.LONG: frozenset({
        "weak_bar_close", "no_squeeze_breakout",
        "too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "upper_wick_rejection", "expansion_bar_too_large",
    }),
    Side.SHORT: frozenset({
        "weak_bar_close", "no_squeeze_breakdown",
        "too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "lower_wick_rejection", "expansion_bar_too_large",
    }),
}

# The three target tiers, weakest first (2026-05-14).
_TIERS = ("standard", "runner", "premium")


class VolatilitySqueezeBreakoutStrategy(BaseStrategy):
    """Breakout of a volatility compression box, either side.

    Per candidate one proposal per side (style / family ``vol_squeeze``;
    SHORT only with ``risk.allow_short``): the squeeze and side blockers and
    the anti-chase exhaustion checks are its pending reasons, and an FVG
    retest of the box edge may clear the break / trigger-bar / exhaustion
    ones. The stop sits a compression-scaled buffer beyond the far side of
    the box (at least ``default_stop_pct`` away), the target at the tier's
    R:R (standard / runner / premium by breakout quality). Every
    shared_entry knob -- the vetoes, the refinement, the retest stop anchor,
    the score terms -- is applied by ``self.entry_policy.admit``; a side it
    refuses leaves the other side to win, and when both survive the higher
    ``final_priority_score`` (shared terms included) takes the candidate.
    """

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

    @staticmethod
    def _refined_tier(close: float, risk: float, target: float, tier_rrs: tuple[float, float, float], requested: int) -> tuple[str, float]:
        """The tier label and target R:R the ADMITTED target delivers.

        The breakout quality requests a tier (``requested``, an index into
        ``_TIERS``); its R:R times ``risk`` -- the proposal's own stop
        distance, the unit the tiers are defined in -- sets the proposal's
        target, and the shared refinement may then cap that target at a
        level. The R:R is the admitted target's distance in that unit (the
        requested tier's R:R unless a level capped it), and the label is the
        highest tier up to the requested one that it still reaches (the
        weakest when none; the cap keeps a tier that shares its R:R with a
        higher one from being promoted). It is deliberately not measured
        against the refined stop: the refinement may pull the stop far in
        without moving the target, and that realized R:R (routinely 10R and
        more) says nothing about which tier the target still reaches. Until
        2026-09-24 both were stamped from the requested tier, so a
        premium-quality break whose target a resistance capped at 2R was
        still logged as a 3.2R premium trade.
        """
        achieved = abs(float(target) - float(close)) / float(risk)
        label = _TIERS[0]
        for index in range(requested + 1):
            if achieved >= tier_rrs[index] - 1e-9:
                label = _TIERS[index]
        return label, achieved

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
        premium_target_rr = max(runner_target_rr, float(self.params.get("premium_target_rr", 3.2)))
        tier_rrs = (target_rr, runner_target_rr, premium_target_rr)
        # Set tiered_targets_enabled false to revert to the 2-tier behavior.
        tiered_targets_enabled = bool(self.params.get("tiered_targets_enabled", True)) and runner_enabled
        tier_atr_floor = float(self.params.get("tier_atr_expansion_floor", 1.25))
        tier_vol_floor = float(self.params.get("tier_volume_ratio_floor", 1.50))
        tier_close_floor = float(self.params.get("tier_close_position_floor", 0.78))
        allow_short = bool(self.config.risk.allow_short)
        sides = (Side.LONG, Side.SHORT) if allow_short else (Side.LONG,)
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
                # of TA-Lib's BBANDS because BBANDS poisons the entire
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
            ms_ctx = self._structure_context(frame, "ltf")
            tech_ctx = self._technical_context(frame)
            squeeze_meta = {
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
            }
            atr_expansion_mult_val = _safe_float(getattr(tech_ctx, "atr_expansion_mult", None), 0.0)
            bollinger_squeeze_flag = bool(getattr(tech_ctx, "bollinger_squeeze", False))

            compression_ok = bool(
                box_range_pct <= max_range_pct
                and box_range_atr <= max_range_atr
                and box_width_pct <= max_width_pct
                and (
                    width_ratio <= max_width_ratio
                    or box_width_pct <= (max_width_pct * 0.70)
                    or bollinger_squeeze_flag
                )
            )
            if not compression_ok:
                reasons.append(_reason_with_values("no_valid_squeeze", current=box_range_pct, required=max_range_pct, op="<=", digits=4, extras={"range_atr": (box_range_atr, "<=", max_range_atr), "width_pct": (box_width_pct, "<=", max_width_pct), "width_ratio": (width_ratio, "<=", max_width_ratio)}))
            if breakout_volume_ratio < min_breakout_volume_ratio:
                reasons.append(_reason_with_values("breakout_volume_too_light", current=breakout_volume_ratio, required=min_breakout_volume_ratio, op=">=", digits=4))
            if atr_expansion_mult_val < min_atr_expansion_mult:
                reasons.append(_reason_with_values("no_atr_expansion", current=atr_expansion_mult_val, required=min_atr_expansion_mult, op=">=", digits=4))
            if prefer_bollinger_flag and not bollinger_squeeze_flag and box_width_pct > max_width_pct * 0.90:
                reasons.append("bollinger_squeeze_not_confirmed")
            # Compression-aware stop buffer: for TIGHT squeezes (narrow
            # box_range_pct), using pure ATR can over-widen the stop. Scale
            # the buffer by the compression width — tighter squeezes get a
            # proportionally tighter stop beyond the box, wider squeezes keep
            # the ATR floor.
            stop_buffer = max(atr * 0.12, last_close * 0.0010, box_range_pct * last_close * 0.22)

            signals: list[Signal] = []
            side_reasons: dict[Side, list[str]] = {}
            for side in sides:
                long = side == Side.LONG
                side_blockers = list(reasons)
                if long:
                    trigger_level = breakout_high
                    breakout_fired = last_close >= breakout_high * (1.0 + breakout_buffer_pct)
                    bullish_avwap = max(_safe_float(getattr(tech_ctx, "anchored_vwap_open", None), 0.0), _safe_float(getattr(tech_ctx, "anchored_vwap_bullish_impulse", None), 0.0))
                    if day_strength < min_change:
                        side_blockers.append(_reason_with_values("weak_day_strength", current=day_strength, required=min_change, op=">=", digits=4))
                    if require_vwap_alignment and last_close <= last_vwap:
                        side_blockers.append(_reason_with_values("below_vwap", current=last_close, required=last_vwap, op=">", digits=4))
                    if last_ema9 < last_ema20:
                        side_blockers.append(_reason_with_values("ema9_below_ema20", current=last_ema9, required=last_ema20, op=">=", digits=4))
                    if require_avwap_alignment and bullish_avwap > 0 and last_close <= bullish_avwap:
                        side_blockers.append(_reason_with_values("below_bullish_avwap", current=last_close, required=bullish_avwap, op=">", digits=4))
                    if not rising_lows_ok:
                        side_blockers.append("pressure_not_building_up")
                    if not breakout_fired:
                        side_blockers.append(_reason_with_values("no_squeeze_breakout", current=last_close, required=breakout_high * (1.0 + breakout_buffer_pct), op=">=", digits=4))
                    if close_pos < min_close_pos:
                        side_blockers.append(_reason_with_values("weak_bar_close", current=close_pos, required=min_close_pos, op=">=", digits=4))
                    stop = min(breakout_low - stop_buffer, last_close * (1.0 - self.config.risk.default_stop_pct))
                    bos_active = bool(getattr(ms_ctx, "bos_up", False))
                    # Strong quality needs the bar to close in its top
                    # (1 - tier_close_floor) for a LONG.
                    strong_close = close_pos >= tier_close_floor
                else:
                    trigger_level = breakout_low
                    breakout_fired = last_close <= breakout_low * (1.0 - breakout_buffer_pct)
                    bearish_avwap_vals = [v for v in [_safe_float(getattr(tech_ctx, "anchored_vwap_open", None), 0.0), _safe_float(getattr(tech_ctx, "anchored_vwap_bearish_impulse", None), 0.0)] if v > 0]
                    bearish_avwap = min(bearish_avwap_vals) if bearish_avwap_vals else 0.0
                    if day_strength > -min_change:
                        side_blockers.append(_reason_with_values("weak_day_weakness", current=day_strength, required=-min_change, op="<=", digits=4))
                    if require_vwap_alignment and last_close >= last_vwap:
                        side_blockers.append(_reason_with_values("above_vwap", current=last_close, required=last_vwap, op="<", digits=4))
                    if last_ema9 > last_ema20:
                        side_blockers.append(_reason_with_values("ema9_above_ema20", current=last_ema9, required=last_ema20, op="<=", digits=4))
                    if require_avwap_alignment and 0 < bearish_avwap <= last_close:
                        side_blockers.append(_reason_with_values("above_bearish_avwap", current=last_close, required=bearish_avwap, op="<", digits=4))
                    if not falling_highs_ok:
                        side_blockers.append("pressure_not_building_down")
                    if not breakout_fired:
                        side_blockers.append(_reason_with_values("no_squeeze_breakdown", current=last_close, required=breakout_low * (1.0 - breakout_buffer_pct), op="<=", digits=4))
                    if close_pos > (1.0 - min_close_pos):
                        side_blockers.append(_reason_with_values("weak_bar_close", current=1.0 - close_pos, required=min_close_pos, op=">=", digits=4))
                    stop = max(breakout_high + stop_buffer, last_close * (1.0 + self.config.risk.default_stop_pct))
                    bos_active = bool(getattr(ms_ctx, "bos_down", False))
                    # ... and in its bottom (1 - tier_close_floor) for a SHORT.
                    strong_close = close_pos <= (1.0 - tier_close_floor)
                side_blockers.extend(self._entry_exhaustion_reasons(side, frame, close=last_close, vwap=last_vwap, ema9=last_ema9))
                # 3-tier target structure (2026-05-14): standard / runner /
                # premium. Standard catches the bulk of qualifying setups at a
                # realistic target. Runner extends when the breakout passes
                # higher quality thresholds (ATR + volume + bar body all
                # strong). Premium extends further when ALL of runner-quality
                # + an active BoS event in the side's direction + the
                # Bollinger squeeze flag agree — the setup is exceptionally
                # aligned and deserves more runway.
                strong_quality = (
                    atr_expansion_mult_val >= tier_atr_floor
                    and breakout_volume_ratio >= tier_vol_floor
                    and strong_close
                )
                if tiered_targets_enabled and strong_quality and bos_active and bollinger_squeeze_flag:
                    requested_tier = 2
                elif runner_enabled and (
                    bos_active
                    or atr_expansion_mult_val >= (min_atr_expansion_mult + 0.12)
                    or (tiered_targets_enabled and strong_quality)
                ):
                    requested_tier = 1
                else:
                    requested_tier = 0
                risk_per_share = max(0.01, (last_close - stop) if long else (stop - last_close))
                reward = risk_per_share * tier_rrs[requested_tier]
                proposal = EntryProposal(
                    candidate=c,
                    direction=side,
                    style="vol_squeeze",
                    style_family="vol_squeeze",
                    close=last_close,
                    stop=stop,
                    target=(last_close + reward) if long else (last_close - reward),
                    gate_frame=frame,
                    sr_frame=frame,
                    level_frame=frame,
                    data=data,
                    pending_reasons=tuple(side_blockers),
                    deferrable=_RETEST_DEFERRABLE[side],
                    retest=RetestTrigger(trigger_level, bool(breakout_fired), last_vwap, last_ema9),
                    contexts=EntryContexts(ms=ms_ctx, tech=tech_ctx, chart=ctx),
                )
                admitted = self.entry_policy.admit(proposal)
                if admitted is None:
                    side_reasons[side] = self._consume_build_failure_payload(c.symbol, proposal.style)["reasons"]
                    continue
                squeeze_tier_label, effective_target_rr = self._refined_tier(last_close, risk_per_share, admitted.target, tier_rrs, requested_tier)
                management = self._adaptive_management_components(
                    side, last_close, admitted.stop, admitted.target,
                    style="trend", runner_allowed=bool(runner_enabled), continuation_bias=float(admitted.fvg["fvg_continuation_bias"]),
                )
                strategy_score = float(c.activity_score) + (0.45 if bollinger_squeeze_flag else 0.0) + max(0.0, 1.0 - min(1.0, width_ratio)) + max(0.0, breakout_volume_ratio - 1.0) + (0.35 if bos_active else 0.0)
                signals.append(self.entry_policy.emit(
                    admitted,
                    reason=f"volatility_squeeze_breakout_{'long' if long else 'short'}",
                    strategy_score=strategy_score,
                    management=management,
                    target=admitted.target,
                    metadata={
                        **squeeze_meta,
                        "squeeze_tier_label": squeeze_tier_label,
                        "squeeze_effective_target_rr": round(float(effective_target_rr), 4),
                    },
                ))

            if not signals:
                # Every refused side's blockers, LONG first (the decision
                # record de-duplicates the ones both sides share).
                self._record_entry_decision(c.symbol, "skipped", [token for side in sides for token in side_reasons[side]])
                continue

            # The side pick stays on final_priority_score, which carries the
            # shared context terms: they chose the side before 2026-09-24 too.
            best = max(signals, key=lambda sig: (float(sig.metadata["final_priority_score"]), float(sig.metadata["breakout_volume_ratio"])))
            out.append(best)
            self._record_entry_decision(c.symbol, "signal", [best.reason])
        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
