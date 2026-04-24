# SPDX-License-Identifier: MIT
from ..shared import (
    Any,
    Candidate,
    HTFContext,
    Position,
    Side,
    Signal,
    _discrete_score_threshold,
    _gate_snapshot,
    _side_prefixed_reason,
    pd,
)
from ..peer_confirmed_key_levels.strategy import PeerConfirmedKeyLevelsStrategy


class PeerConfirmedTrendContinuationStrategy(PeerConfirmedKeyLevelsStrategy):
    strategy_name = 'peer_confirmed_trend_continuation'

    def _use_sr_veto(self) -> bool:
        return bool(self.params.get("use_sr_veto", False))

    def _blocks_bullish_sr_entry(self, sr_ctx) -> bool:
        return super()._blocks_bullish_sr_entry(sr_ctx) if self._use_sr_veto() else False

    def _blocks_bearish_sr_entry(self, sr_ctx) -> bool:
        return super()._blocks_bearish_sr_entry(sr_ctx) if self._use_sr_veto() else False

    def dashboard_overlay_candidates(self, side: Side, close: float, ltf: pd.DataFrame, htf: HTFContext) -> list[dict[str, Any]] | None:
        if ltf is None or ltf.empty:
            return []
        trend = self._trend_signal(side, ltf, htf)
        trigger = self._pullback_trigger_signal(side, ltf, close=trend["close"], ema9=trend["ema9"], ema20=trend["ema20"], atr=trend["atr"])
        trigger_level = self._safe_float(trigger.get("trigger_level"), 0.0)
        pullback_extreme = self._safe_float(trigger.get("pullback_extreme"), 0.0)
        if trigger_level <= 0 or pullback_extreme <= 0:
            return []
        trigger_score = float(trigger.get("score", 0.0) or 0.0)
        trend_score = float(trend.get("score", 0.0) or 0.0)
        trigger_kind = "bullish_continuation_trigger" if side == Side.LONG else "bearish_continuation_trigger"
        anchor_kind = "bullish_pullback_anchor" if side == Side.LONG else "bearish_pullback_anchor"
        return [
            {
                "kind": trigger_kind,
                "price": float(trigger_level),
                "touches": 1,
                "level_score": round(trend_score + trigger_score, 4),
                "source_priority": 2.25,
            },
            {
                "kind": anchor_kind,
                "price": float(pullback_extreme),
                "touches": 1,
                "level_score": round(max(trigger_score - 0.5, 0.0), 4),
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
        trigger_tf = max(1, int(self.params.get("trigger_timeframe_minutes", 5)))
        min_trigger_bars = int(self.params.get("min_trigger_bars", 18))
        max_pullback_bars = int(self.params.get("max_pullback_bars", 5))
        return max(min_bars, trigger_tf * (min_trigger_bars + max_pullback_bars + 4))

    def _trend_signal(self, side: Side, ltf: pd.DataFrame, htf: HTFContext) -> dict[str, Any]:
        last = ltf.iloc[-1]
        close = self._safe_float(last.get("close"), 0.0)
        vwap = self._safe_float(last.get("vwap"), close)
        ema9 = self._safe_float(last.get("ema9"), close)
        ema20 = self._safe_float(last.get("ema20"), close)
        atr = max(self._safe_float(last.get("atr14"), max(close * 0.0015, 0.01)), max(close * 0.0005, 0.01))
        adx = self._safe_float(last.get("adx14"), 0.0)
        htf_bias, bull_votes, bear_votes = self._hourly_bias(htf, close)
        min_adx = float(self.params.get("min_adx14", 13.5))
        score = 0.0
        reasons: list[str] = []
        if side == Side.LONG:
            if close > vwap:
                score += 1.0
            else:
                reasons.append(self._reason_with_values("below_vwap", current=close, required=vwap, op=">", digits=4))
            if ema9 >= ema20:
                score += 1.0
            else:
                reasons.append(self._reason_with_values("ema9_below_ema20", current=ema9, required=ema20, op=">=", digits=4))
            if close >= ema9:
                score += 1.0
            else:
                reasons.append(self._reason_with_values("below_ema9", current=close, required=ema9, op=">=", digits=4))
            if htf_bias == "bullish":
                score += 1.0 + (0.25 if bull_votes >= max(2, bear_votes + 1) else 0.0)
            else:
                reasons.append(f"htf_bias_{htf_bias}")
        else:
            if close < vwap:
                score += 1.0
            else:
                reasons.append(self._reason_with_values("above_vwap", current=close, required=vwap, op="<", digits=4))
            if ema9 <= ema20:
                score += 1.0
            else:
                reasons.append(self._reason_with_values("ema9_above_ema20", current=ema9, required=ema20, op="<=", digits=4))
            if close <= ema9:
                score += 1.0
            else:
                reasons.append(self._reason_with_values("above_ema9", current=close, required=ema9, op="<=", digits=4))
            if htf_bias == "bearish":
                score += 1.0 + (0.25 if bear_votes >= max(2, bull_votes + 1) else 0.0)
            else:
                reasons.append(f"htf_bias_{htf_bias}")
        if adx >= min_adx:
            score += 1.0
        else:
            reasons.append(self._reason_with_values("weak_adx", current=adx, required=min_adx, op=">=", digits=4))
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
            "hourly_bull_votes": bull_votes,
            "hourly_bear_votes": bear_votes,
        }

    def _pullback_trigger_signal(self, side: Side, ltf: pd.DataFrame, *, close: float, ema9: float, ema20: float, atr: float) -> dict[str, Any]:
        max_pullback_bars = max(2, int(self.params.get("max_pullback_bars", 5)))
        min_pullback_bars = max(1, int(self.params.get("min_pullback_bars", 2)))
        trigger_window = max(2, int(self.params.get("min_trigger_bars", 18)))
        breakout_buffer_pct = max(0.0, float(self.params.get("breakout_buffer_pct", 0.0008)))
        min_close_pos = min(0.95, max(0.05, float(self.params.get("min_trigger_close_position", 0.58))))
        min_trigger_vol = max(0.1, float(self.params.get("min_trigger_volume_ratio", 1.02)))
        max_countertrend_vol = max(0.5, float(self.params.get("max_countertrend_volume_ratio", 1.28)))
        max_pullback_depth_atr = max(0.1, float(self.params.get("max_pullback_depth_atr", 1.05)))
        pullback_hold_atr = max(0.0, float(self.params.get("pullback_hold_atr", 0.38)))

        recent = ltf.tail(max(trigger_window, max_pullback_bars + 6))
        prior = recent.iloc[:-1]
        if prior.empty:
            return {"score": 0.0, "reasons": ["no_pullback_context"]}
        pullback = prior.tail(max_pullback_bars)
        trigger_ref = prior.tail(max(min_pullback_bars + 1, 2))
        close_pos = self._bar_close_position(ltf)
        last_vol = self._safe_float(ltf.iloc[-1].get("volume"), 0.0)
        avg_vol = max(self._safe_float(prior["volume"].tail(max(3, max_pullback_bars)).mean(), 0.0), 1.0)
        volume_ratio = last_vol / avg_vol if avg_vol > 0 else 0.0
        pullback_countertrend_volume_ratio = max(self._safe_float(pullback["volume"].mean(), 0.0), 0.0) / max(self._safe_float(prior["volume"].tail(max(6, max_pullback_bars + 1)).mean(), 0.0), 1.0)
        score = 0.0
        reasons: list[str] = []
        if side == Side.LONG:
            trigger_level = self._safe_float(trigger_ref["high"].max(), close)
            pullback_extreme = self._safe_float(pullback["low"].min(), close)
            pullback_depth_atr = max(0.0, trigger_level - pullback_extreme) / atr if atr > 0 else 0.0
            if pullback_depth_atr <= max_pullback_depth_atr:
                score += 1.0
            else:
                reasons.append(self._reason_with_values("pullback_too_deep_atr", current=pullback_depth_atr, required=max_pullback_depth_atr, op="<=", digits=4))
            if pullback_extreme >= (ema20 - (atr * pullback_hold_atr)):
                score += 1.0
            else:
                reasons.append(self._reason_with_values("pullback_lost_ema20", current=pullback_extreme, required=ema20 - (atr * pullback_hold_atr), op=">=", digits=4))
            if close > trigger_level * (1.0 + breakout_buffer_pct):
                score += 1.0
            else:
                reasons.append(self._reason_with_values("no_reexpansion_trigger", current=close, required=trigger_level * (1.0 + breakout_buffer_pct), op=">", digits=4))
            if close >= ema9:
                score += 0.75
            else:
                reasons.append(self._reason_with_values("failed_ema9_reclaim", current=close, required=ema9, op=">=", digits=4))
            if self._safe_float(ltf.iloc[-1].get("close"), close) >= self._safe_float(ltf.iloc[-1].get("open"), close):
                score += 0.5
            else:
                reasons.append("trigger_bar_not_bullish")
            if close_pos >= min_close_pos:
                score += 0.5
            else:
                reasons.append(self._reason_with_values("weak_bar_close", current=close_pos, required=min_close_pos, op=">=", digits=4))
            candle_summary = self._configured_trigger_candle_summary(side, ltf)
            candle_tier = str(candle_summary.get("confirm_tier", "none") or "none")
            candle_bonus = {
                "strong_3c": 0.60,
                "solid_2c": 0.40,
                "weak_1c": 0.20,
            }.get(candle_tier, 0.0)
            score += candle_bonus
            stop_anchor = pullback_extreme
        else:
            trigger_level = self._safe_float(trigger_ref["low"].min(), close)
            pullback_extreme = self._safe_float(pullback["high"].max(), close)
            pullback_depth_atr = max(0.0, pullback_extreme - trigger_level) / atr if atr > 0 else 0.0
            if pullback_depth_atr <= max_pullback_depth_atr:
                score += 1.0
            else:
                reasons.append(self._reason_with_values("pullback_too_deep_atr", current=pullback_depth_atr, required=max_pullback_depth_atr, op="<=", digits=4))
            if pullback_extreme <= (ema20 + (atr * pullback_hold_atr)):
                score += 1.0
            else:
                reasons.append(self._reason_with_values("pullback_lost_ema20", current=pullback_extreme, required=ema20 + (atr * pullback_hold_atr), op="<=", digits=4))
            if close < trigger_level * (1.0 - breakout_buffer_pct):
                score += 1.0
            else:
                reasons.append(self._reason_with_values("no_reexpansion_trigger", current=close, required=trigger_level * (1.0 - breakout_buffer_pct), op="<", digits=4))
            if close <= ema9:
                score += 0.75
            else:
                reasons.append(self._reason_with_values("failed_ema9_reject", current=close, required=ema9, op="<=", digits=4))
            if self._safe_float(ltf.iloc[-1].get("close"), close) <= self._safe_float(ltf.iloc[-1].get("open"), close):
                score += 0.5
            else:
                reasons.append("trigger_bar_not_bearish")
            if close_pos <= (1.0 - min_close_pos):
                score += 0.5
            else:
                reasons.append(self._reason_with_values("weak_bar_close", current=close_pos, required=1.0 - min_close_pos, op="<=", digits=4))
            candle_summary = self._configured_trigger_candle_summary(side, ltf)
            candle_tier = str(candle_summary.get("confirm_tier", "none") or "none")
            candle_bonus = {
                "strong_3c": 0.60,
                "solid_2c": 0.40,
                "weak_1c": 0.20,
            }.get(candle_tier, 0.0)
            score += candle_bonus
            stop_anchor = pullback_extreme
        if volume_ratio >= min_trigger_vol:
            score += 0.5
        else:
            reasons.append(self._reason_with_values("weak_trigger_volume", current=volume_ratio, required=min_trigger_vol, op=">=", digits=4))
        if pullback_countertrend_volume_ratio <= max_countertrend_vol:
            score += 0.5
        else:
            reasons.append(self._reason_with_values("heavy_countertrend_volume", current=pullback_countertrend_volume_ratio, required=max_countertrend_vol, op="<=", digits=4))
        return {
            "score": float(score),
            "reasons": reasons,
            "trigger_level": float(trigger_level),
            "pullback_extreme": float(pullback_extreme),
            "pullback_depth_atr": float(pullback_depth_atr),
            "close_position": float(close_pos),
            "trigger_volume_ratio": float(volume_ratio),
            "countertrend_volume_ratio": float(pullback_countertrend_volume_ratio),
            "stop_anchor": float(stop_anchor),
        }

    def _macro_allows(self, side: Side, macro_ctx: dict[str, Any]) -> bool:
        required = max(0, int(self.params.get("require_macro_agreement_count", 1)))
        if not bool(macro_ctx.get("enabled", True)):
            return True
        if side == Side.LONG:
            return int(macro_ctx.get("long_agree", 0) or 0) >= required
        return int(macro_ctx.get("short_agree", 0) or 0) >= required

    def _build_continuation_signal(self, c: Candidate, frame: pd.DataFrame, ltf: pd.DataFrame, side: Side, peer_ctx: dict[str, Any], macro_ctx: dict[str, Any], data=None, *, sr_ctx=None, ms_ctx=None, tech_ctx=None) -> Signal | None:
        failure_style = f"peer_confirmed_trend_continuation_{side.value.lower()}"
        close = self._safe_float(ltf.iloc[-1].get("close"), 0.0)
        if close <= 0:
            return None
        htf = self._htf_context(
            c.symbol,
            data,
            timeframe_minutes=int(self.params.get("htf_timeframe_minutes", 60)),
            lookback_days=int(self.params.get("htf_lookback_days", 60)),
            pivot_span=int(self.params.get("htf_pivot_span", 2)),
            max_levels_per_side=int(self.params.get("htf_max_levels_per_side", 6)),
            atr_tolerance_mult=float(self.params.get("htf_atr_tolerance_mult", 0.35)),
            pct_tolerance=float(self.params.get("htf_pct_tolerance", 0.0030)),
            stop_buffer_atr_mult=float(self.params.get("htf_stop_buffer_atr_mult", 0.25)),
            ema_fast_span=int(self.params.get("htf_ema_fast_span", 34)),
            ema_slow_span=int(self.params.get("htf_ema_slow_span", 200)),
            refresh_seconds=int(self.params.get("htf_refresh_seconds", 120)),
            current_price=close,
            use_prior_day_high_low=bool(self._support_resistance_setting("use_prior_day_high_low", True)),
            use_prior_week_high_low=bool(self._support_resistance_setting("use_prior_week_high_low", True)),
        )
        trend = self._trend_signal(side, ltf, htf)
        trigger = self._pullback_trigger_signal(side, ltf, close=trend["close"], ema9=trend["ema9"], ema20=trend["ema20"], atr=trend["atr"])
        hard_reasons: list[str] = []
        diagnostics: list[str] = []
        total_score = float(trend.get("score", 0.0) or 0.0)
        total_score += float(trigger.get("score", 0.0) or 0.0)
        peer_score = int(peer_ctx.get("score", 0) or 0)
        directional_peer_score = peer_score if side == Side.LONG else -peer_score
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
            diagnostics.append(self._reason_with_values("weak_peer_score", current=directional_peer_score, required=min_peer_score, op=">=", digits=2))
        if peer_agreement < min_peer_agreement:
            hard_reasons.append(self._reason_with_values("weak_peer_agreement", current=peer_agreement, required=min_peer_agreement, op=">=", digits=2))

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

        min_total = _discrete_score_threshold(self.params.get("min_total_score", 5.5), 6, minimum=1)
        min_trigger_score = _discrete_score_threshold(self.params.get("min_trigger_score", 2.5), 2, minimum=1)
        if float(trigger.get("score", 0.0) or 0.0) < min_trigger_score:
            hard_reasons.append(self._reason_with_values("weak_trigger_score", current=float(trigger.get("score", 0.0) or 0.0), required=min_trigger_score, op=">=", digits=4))

        hard_reasons.extend(self._entry_exhaustion_reasons(side, ltf, close=trend["close"], vwap=trend["vwap"], ema9=trend["ema9"]))

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
        if extension_from_vwap_atr > (max_vwap_ext * extension_hard_cap_mult):
            hard_reasons.append(self._reason_with_values("too_extended_from_vwap_atr", current=extension_from_vwap_atr, required=max_vwap_ext * extension_hard_cap_mult, op="<=", digits=4))
        if extension_from_ema9_atr > (max_ema9_ext * extension_hard_cap_mult):
            hard_reasons.append(self._reason_with_values("too_extended_from_ema9_atr", current=extension_from_ema9_atr, required=max_ema9_ext * extension_hard_cap_mult, op="<=", digits=4))

        if total_score < min_total:
            hard_reasons.append(self._reason_with_values("weak_total_score", current=total_score, required=min_total, op=">=", digits=4))

        gate_snapshots = [
            _gate_snapshot("trend_score", passed=float(trend.get("score", 0.0) or 0.0) > 0.0, current=round(float(trend.get("score", 0.0) or 0.0), 4), required=0.0, op=">"),
            _gate_snapshot("trigger_score", passed=float(trigger.get("score", 0.0) or 0.0) >= min_trigger_score, current=round(float(trigger.get("score", 0.0) or 0.0), 4), required=min_trigger_score, op=">="),
            _gate_snapshot("peer_agreement", passed=peer_agreement >= min_peer_agreement, current=int(peer_agreement), required=int(min_peer_agreement), op=">="),
            _gate_snapshot("directional_peer_score", passed=directional_peer_score >= min_peer_score, current=round(float(directional_peer_score), 4), required=int(min_peer_score), op=">="),
            _gate_snapshot("macro_alignment", passed=macro_aligned, current=int(macro_aligned), required=1, op=">="),
            _gate_snapshot("continuation_total_score", passed=total_score >= min_total, current=round(total_score, 4), required=min_total, op=">="),
        ]
        near_miss_blockers: dict[str, float] = {}
        if float(trigger.get("score", 0.0) or 0.0) < min_trigger_score:
            near_miss_blockers["trigger_score"] = round(min_trigger_score - float(trigger.get("score", 0.0) or 0.0), 4)
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

        # Accept pre-built contexts from the caller to avoid rebuilding them
        # once per side. The entry_signals loop builds these once per candidate
        # and passes them in; the fallbacks here cover direct callers.
        if sr_ctx is None:
            sr_ctx = self._sr_context(c.symbol, frame, data)
        if ms_ctx is None:
            ms_ctx = self._structure_context(ltf, "ltf")
        if tech_ctx is None:
            tech_ctx = self._technical_context(ltf)
        if side == Side.LONG:
            if self._blocks_bullish_structure_entry(ms_ctx):
                hard_reasons.append(self._bullish_structure_block_reason(ms_ctx))
            if self._blocks_bullish_sr_entry(sr_ctx):
                hard_reasons.append(self._bullish_sr_block_reason(sr_ctx))
        else:
            if self._blocks_bearish_structure_entry(ms_ctx):
                hard_reasons.append(self._bearish_structure_block_reason(ms_ctx))
            if self._blocks_bearish_sr_entry(sr_ctx):
                hard_reasons.append(self._bearish_sr_block_reason(sr_ctx))

        if hard_reasons:
            self._set_build_failure(
                c.symbol,
                failure_style,
                hard_reasons[0],
                reasons=hard_reasons,
                details={
                    "peer_universe": list(peer_ctx.get("universe", [])),
                    "peer_details": dict(peer_ctx.get("details", {})),
                    "peer_bullish": int(peer_ctx.get("bullish", 0) or 0),
                    "peer_bearish": int(peer_ctx.get("bearish", 0) or 0),
                    "peer_score": int(peer_ctx.get("score", 0) or 0),
                    "macro_details": dict(macro_ctx.get("details", {})),
                    "macro_long_agree": int(macro_ctx.get("long_agree", 0) or 0),
                    "macro_short_agree": int(macro_ctx.get("short_agree", 0) or 0),
                    "side_eval": side_eval,
                    "primary_blocker": hard_reasons[0],
                    "all_blockers": list(hard_reasons),
                    "near_miss_blockers": near_miss_blockers,
                },
            )
            return None

        stop_buffer_atr = max(0.05, float(self.params.get("stop_buffer_atr_mult", 0.50)))
        if side == Side.LONG:
            stop = float(trigger.get("stop_anchor", trend["close"])) - (trend["atr"] * stop_buffer_atr)
            stop = min(stop, trend["ema20"] - (trend["atr"] * 0.1))
            stop = min(stop, trend["close"] * (1.0 - float(self.config.risk.default_stop_pct)))
            risk_per_share = max(0.01, trend["close"] - stop)
            target_rr = max(float(self.params.get("target_rr", 2.05)), float(self.params.get("min_rr", 1.8)))
            target = trend["close"] + (risk_per_share * target_rr)
            stop, target = self._refine_bullish_sr_levels(trend["close"], stop, target, sr_ctx)
            stop, target = self._refine_bullish_technical_levels(trend["close"], stop, target, tech_ctx, ltf)
        else:
            stop = float(trigger.get("stop_anchor", trend["close"])) + (trend["atr"] * stop_buffer_atr)
            stop = max(stop, trend["ema20"] + (trend["atr"] * 0.1))
            stop = max(stop, trend["close"] * (1.0 + float(self.config.risk.default_stop_pct)))
            risk_per_share = max(0.01, stop - trend["close"])
            target_rr = max(float(self.params.get("target_rr", 2.05)), float(self.params.get("min_rr", 1.8)))
            target = max(0.01, trend["close"] - (risk_per_share * target_rr))
            stop, target = self._refine_bearish_sr_levels(trend["close"], stop, target, sr_ctx)
            stop, target = self._refine_bearish_technical_levels(trend["close"], stop, target, tech_ctx, ltf)

        adjustments = self._entry_adjustment_components(side, sr_ctx=sr_ctx, tech_ctx=tech_ctx)
        fvg_adjustments = self._fvg_entry_adjustment_components(side, c.symbol, ltf, data)
        runner_allowed = bool(self.params.get("strong_setup_runner_enabled", True)) and total_score >= (min_total + 1)
        management = self._adaptive_management_components(
            side,
            trend["close"],
            stop,
            target,
            style="trend",
            runner_allowed=runner_allowed,
            continuation_bias=float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0),
            strong_setup=runner_allowed,
        )
        activity_weight = max(0.0, float(self.params.get("activity_score_weight", 0.12)))
        execution_quality_score = float(adjustments.get("entry_context_adjustment", 0.0) or 0.0) + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
        final_priority_score = total_score + execution_quality_score + (float(c.activity_score) * activity_weight)
        metadata = self._build_signal_metadata(
            entry_price=float(trend["close"]),
            chart_ctx=self._chart_context(ltf),
            ms_ctx=ms_ctx, sr_ctx=sr_ctx, tech_ctx=tech_ctx,
            adjustments=adjustments, fvg_adjustments=fvg_adjustments,
            management=management,
            final_priority_score=final_priority_score,
            ms_prefix="ms_ltf",
            leading={
                "activity_score": float(c.activity_score),
                "setup_quality_score": round(total_score, 4),
                "execution_quality_score": round(execution_quality_score, 4),
                "macro_score": float(macro_ctx.get("long_agree", 0) or 0) if side == Side.LONG else float(macro_ctx.get("short_agree", 0) or 0),
                "trend_score": round(float(trend.get("score", 0.0) or 0.0), 4),
                "trigger_score": round(float(trigger.get("score", 0.0) or 0.0), 4),
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
                "directional_vote_edge": float(abs(int(trend.get("hourly_bull_votes", 0) or 0) - int(trend.get("hourly_bear_votes", 0) or 0))),
                "runner_quality_score": 1.0 if runner_allowed else 0.0,
                "execution_headroom_score": round(float(max(0.0, min(max_vwap_ext - extension_from_vwap_atr, max_ema9_ext - extension_from_ema9_atr))), 4),
                "source_quality_score": 0.0,
                "selection_quality_score": round(final_priority_score, 4),
            },
        )
        reason = "peer_confirmed_trend_continuation_long" if side == Side.LONG else "peer_confirmed_trend_continuation_short"
        return Signal(symbol=c.symbol, strategy=self.strategy_name, side=side, reason=reason, stop_price=float(stop), target_price=float(target), metadata=metadata)

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        history_bars = self.required_history_bars()
        trigger_tf = max(1, int(self.params.get("trigger_timeframe_minutes", 5)))
        allow_short = bool(self.config.risk.allow_short)
        macro_ctx = self._macro_signal(bars, data)
        for c in candidates:
            frame = bars.get(c.symbol)
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue
            if frame is None or len(frame) < history_bars:
                self._record_entry_decision(c.symbol, "skipped", [self._insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), history_bars)])
                continue
            ltf = self._resampled_frame(frame, trigger_tf, symbol=c.symbol, data=data)
            if ltf is None or ltf.empty or len(ltf) < max(10, int(self.params.get("min_trigger_bars", 18)) + 4):
                self._record_entry_decision(c.symbol, "skipped", ["missing_ltf_context"])
                continue
            peer_ctx = self._peer_signal(c.symbol, bars, data)
            if c.directional_bias == Side.LONG:
                preferred_sides = [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]
            elif c.directional_bias == Side.SHORT:
                if not allow_short:
                    self._record_entry_decision(c.symbol, "skipped", ["shorts_disabled"])
                    continue
                preferred_sides = [Side.SHORT, Side.LONG]
            else:
                preferred_sides = [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]
            side_order, evaluated_sides = self._entry_side_context(preferred_sides)
            valid_signals: list[Signal] = []
            fail_reasons: list[str] = []
            side_eval: dict[str, Any] = {}
            all_blockers: list[str] = []
            near_miss_blockers: dict[str, Any] = {}
            # Build side-agnostic contexts ONCE per candidate; pass them into each
            # per-side builder so we don't recompute market-structure / technical
            # contexts twice when both LONG and SHORT are evaluated.
            shared_sr_ctx = self._sr_context(c.symbol, frame, data)
            shared_ms_ctx = self._structure_context(ltf, "ltf")
            shared_tech_ctx = self._technical_context(ltf)
            for side in side_order:
                side_value = str(side.value)
                built_signal = self._build_continuation_signal(
                    c, frame, ltf, side, peer_ctx, macro_ctx, data=data,
                    sr_ctx=shared_sr_ctx, ms_ctx=shared_ms_ctx, tech_ctx=shared_tech_ctx,
                )
                if built_signal is not None:
                    valid_signals.append(built_signal)
                    meta = built_signal.metadata if isinstance(built_signal.metadata, dict) else {}
                    side_eval[side_value.lower()] = meta.get("side_eval")
                    continue
                failure_payload = self._consume_build_failure_payload(
                    c.symbol,
                    f"peer_confirmed_trend_continuation_{side.value.lower()}",
                )
                if isinstance(failure_payload, dict):
                    side_key = side_value.lower()
                    if isinstance(failure_payload.get("details"), dict):
                        side_eval[side_key] = failure_payload.get("details", {}).get("side_eval")
                        for blocker in failure_payload.get("details", {}).get("all_blockers", []):
                            token = _side_prefixed_reason(side, str(blocker))
                            if token and token not in all_blockers:
                                all_blockers.append(token)
                        for key, value in failure_payload.get("details", {}).get("near_miss_blockers", {}).items():
                            near_miss_blockers[f"{side_key}.{key}"] = value
                    for token in self._side_prefixed_reasons(side, failure_payload.get("reasons") or [failure_payload.get("primary_reason") or f"{side.value.lower()}_setup_not_ready"]):
                        if token not in fail_reasons:
                            fail_reasons.append(token)
                    continue
                failure = self._consume_build_failure(
                    c.symbol,
                    f"peer_confirmed_trend_continuation_{side.value.lower()}",
                )
                for token in self._side_prefixed_reasons(side, [failure or f"{side.value.lower()}_setup_not_ready"]):
                    if token not in fail_reasons:
                        fail_reasons.append(token)
            if valid_signals:
                def _signal_key(sig: Signal) -> tuple[float, ...]:
                    meta = sig.metadata if isinstance(sig.metadata, dict) else {}
                    strength = float(meta.get("final_priority_score", 0.0) or 0.0)
                    preferred_side_bonus = 1.0 if c.directional_bias is not None and sig.side == c.directional_bias else 0.0
                    custom_key = self.signal_priority_key(
                        sig,
                        c,
                        metadata=meta,
                        strength=strength,
                        candidate_activity_score=float(c.activity_score),
                        rank=float(c.rank),
                    )
                    if custom_key is not None:
                        return tuple(custom_key) + (preferred_side_bonus,)
                    return (
                        float(meta.get("selection_quality_score", strength) or strength),
                        float(meta.get("directional_peer_score", 0.0) or 0.0),
                        float(meta.get("execution_headroom_score", 0.0) or 0.0),
                        preferred_side_bonus,
                    )
                signal = max(valid_signals, key=_signal_key)
                out.append(signal)
                meta = signal.metadata if isinstance(signal.metadata, dict) else {}
                signal_details = {"side_eval": side_eval or meta.get("side_eval"), "peer_universe": meta.get("peer_universe"), "evaluated_sides": evaluated_sides}
                self._record_entry_decision(c.symbol, "signal", [signal.reason], details=signal_details)
            else:
                blockers = all_blockers or list(fail_reasons)
                skip_details = {"side_eval": side_eval, "peer_universe": list(peer_ctx.get("universe", [])), "peer_details": dict(peer_ctx.get("details", {})), "evaluated_sides": evaluated_sides, "primary_blocker": blockers[0] if blockers else None, "all_blockers": blockers, "near_miss_blockers": near_miss_blockers}
                self._record_entry_decision(c.symbol, "skipped", fail_reasons or ["no_setup"], details=skip_details)
        return out
