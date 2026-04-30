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

class PairsResidualStrategy(BaseStrategy):
    strategy_name = 'pairs_residual'

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        return max(0, int(self.params.get("lookback_bars", 90) or 90))


    def __init__(self, config):
        super().__init__(config)
        self.pairs = config.pairs

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        lookback = int(self.params.get("lookback_bars", 90))
        entry_z = float(self.params.get("zscore_entry", 1.30))
        max_entry_z = max(entry_z, float(self.params.get("max_zscore_entry", 2.0)))
        global_allow_short = bool(self.config.risk.allow_short)
        for candidate in candidates:
            pair = candidate.metadata.get("pair")
            if not pair:
                continue
            symbol = pair.get("symbol") if isinstance(pair, dict) else getattr(pair, "symbol", None)
            reference = pair.get("reference") if isinstance(pair, dict) else getattr(pair, "reference", None)
            if not symbol or not reference:
                continue
            if symbol in positions:
                self._record_entry_decision(symbol, "skipped", ["already_in_position"])
                continue
            left = bars.get(symbol)
            right = bars.get(reference)
            if left is None or right is None or len(left) < lookback or len(right) < lookback:
                self._record_entry_decision(
                    symbol,
                    "skipped",
                    [
                        _reason_with_values(
                            "insufficient_pair_bars",
                            current=min(0 if left is None else len(left), 0 if right is None else len(right)),
                            required=lookback,
                            op=">=",
                            digits=0,
                            extras={
                                "left_bars": (0 if left is None else len(left), ">=", lookback),
                                "right_bars": (0 if right is None else len(right), ">=", lookback),
                            },
                        )
                    ],
                )
                continue
            merged = pd.DataFrame({"left": left["close"], "right": right["close"]}).dropna().tail(lookback)
            if len(merged) < lookback // 2:
                self._record_entry_decision(symbol, "skipped", [insufficient_bars_reason("insufficient_aligned_pair_bars", len(merged), lookback // 2)])
                continue
            left_ret = merged.left.pct_change().fillna(0)
            right_ret = merged.right.pct_change().fillna(0)
            window = max(10, lookback // 3)
            relative = (left_ret - right_ret).rolling(window, min_periods=window).sum().dropna()
            if len(relative) < 2:
                self._record_entry_decision(symbol, "skipped", [insufficient_bars_reason("insufficient_rolling_window", len(relative), 2)])
                continue
            rel_std = float(relative.std(ddof=0))
            if rel_std <= 0 or rel_std != rel_std:
                z = 0.0
            else:
                z = (float(relative.iloc[-1]) - float(relative.mean())) / rel_std
            last = left.iloc[-1]
            reasons: list[str] = []
            side_pref = str((pair.get("side_preference") if isinstance(pair, dict) else getattr(pair, "side_preference", "both")) or "both").strip().lower()
            allow_long = side_pref in {"both", "long"}
            allow_short = global_allow_short and side_pref in {"both", "short"}
            long_ready = allow_long and z >= entry_z
            short_ready = allow_short and z <= -entry_z
            if abs(z) > max_entry_z:
                reasons.append(_reason_with_values("relative_strength_too_extended", current=abs(z), required=max_entry_z, op="<=", digits=4))
            elif not (long_ready or short_ready):
                if abs(z) < entry_z:
                    reasons.append(_reason_with_values("relative_strength_abs_below_threshold", current=abs(z), required=entry_z, op=">=", digits=4))
                elif z <= -entry_z and not global_allow_short and side_pref in {"both", "short"}:
                    reasons.append("shorts_disabled")
                else:
                    reasons.append(f"side_preference_blocked({side_pref})")
            last_close = _safe_float(last["close"])
            sr_ctx = self._sr_context(symbol, left, data)
            ms_ctx = self._structure_context(left, "1m")
            tech_ctx = self._technical_context(left)
            if long_ready:
                if self._blocks_bullish_structure_entry(ms_ctx):
                    reasons.append(self._bullish_structure_block_reason(ms_ctx))
                if not reasons:
                    last_vwap = _safe_float(last.get("vwap"), last_close)
                    last_ema9 = _safe_float(last.get("ema9"), last_close)
                    reasons.extend(self._entry_exhaustion_reasons(Side.LONG, left, close=last_close, vwap=last_vwap, ema9=last_ema9))
                if not reasons:
                    stop = last_close * (1.0 - self.config.risk.default_stop_pct)
                    target = last_close * (1.0 + self.config.risk.default_target_pct)
                    stop, target = self._refine_bullish_sr_levels(last_close, stop, target, sr_ctx)
                    stop, target = self._refine_bullish_technical_levels(last_close, stop, target, tech_ctx, left)
                    structure_bonus = 0.75 if getattr(ms_ctx, "bias", "neutral") == "bullish" else 0.0
                    if bool(getattr(ms_ctx, "bos_up", False)) and self._structure_event_recent(getattr(ms_ctx, "bos_up_age_bars", None)):
                        structure_bonus += 0.5
                    adjustments = self._entry_adjustment_components(Side.LONG, sr_ctx=sr_ctx, tech_ctx=tech_ctx)
                    fvg_adjustments = self._fvg_entry_adjustment_components(Side.LONG, symbol, left, data)
                    # Slightly widened runner gate: allow runners up to 1.9x
                    # the entry threshold when continuation bias is moderate
                    # (0.20+) and HTF structure agrees. This lets extended
                    # residual setups run longer without changing the entry
                    # gate — no new entries, just smarter exits on already-
                    # selected pairs.
                    fvg_cont_bias = float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0)
                    runner_allowed = bool(abs(z) <= (entry_z * 1.9) and fvg_cont_bias >= 0.20 and getattr(ms_ctx, "bias", "neutral") == "bullish")
                    management = self._adaptive_management_components(Side.LONG, last_close, stop, target, style="pairs", runner_allowed=runner_allowed, continuation_bias=fvg_cont_bias)
                    final_priority_score = (abs(z) * 100.0) + (float(candidate.activity_score) * 0.25) + structure_bonus + adjustments["entry_context_adjustment"] + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
                    reason = f"relative_strength_z={z:.2f}"
                    metadata = self._build_signal_metadata(
                        entry_price=last_close,
                        ms_ctx=ms_ctx, sr_ctx=sr_ctx, tech_ctx=tech_ctx,
                        adjustments=adjustments, fvg_adjustments=fvg_adjustments,
                        management=management,
                        final_priority_score=final_priority_score,
                        leading={"benchmark": reference, "zscore": z, "side_preference": side_pref},
                    )
                    out.append(Signal(symbol=symbol, strategy=self.strategy_name, side=Side.LONG, reason=reason, stop_price=stop, target_price=target, reference_symbol=reference, pair_id=f"{symbol}:{reference}", metadata=metadata))
                    self._record_entry_decision(symbol, "signal", [reason])
                    continue
            elif short_ready:
                if self._blocks_bearish_structure_entry(ms_ctx):
                    reasons.append(self._bearish_structure_block_reason(ms_ctx))
                if not reasons:
                    last_vwap = _safe_float(last.get("vwap"), last_close)
                    last_ema9 = _safe_float(last.get("ema9"), last_close)
                    reasons.extend(self._entry_exhaustion_reasons(Side.SHORT, left, close=last_close, vwap=last_vwap, ema9=last_ema9))
                if not reasons:
                    stop = last_close * (1.0 + self.config.risk.default_stop_pct)
                    target = last_close * (1.0 - self.config.risk.default_target_pct)
                    stop, target = self._refine_bearish_sr_levels(last_close, stop, target, sr_ctx)
                    stop, target = self._refine_bearish_technical_levels(last_close, stop, target, tech_ctx, left)
                    structure_bonus = 0.75 if getattr(ms_ctx, "bias", "neutral") == "bearish" else 0.0
                    if bool(getattr(ms_ctx, "bos_down", False)) and self._structure_event_recent(getattr(ms_ctx, "bos_down_age_bars", None)):
                        structure_bonus += 0.5
                    adjustments = self._entry_adjustment_components(Side.SHORT, sr_ctx=sr_ctx, tech_ctx=tech_ctx)
                    fvg_adjustments = self._fvg_entry_adjustment_components(Side.SHORT, symbol, left, data)
                    # Symmetric SHORT-side runner widening (see LONG branch above).
                    fvg_cont_bias = float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0)
                    runner_allowed = bool(abs(z) <= (entry_z * 1.9) and fvg_cont_bias >= 0.20 and getattr(ms_ctx, "bias", "neutral") == "bearish")
                    management = self._adaptive_management_components(Side.SHORT, last_close, stop, target, style="pairs", runner_allowed=runner_allowed, continuation_bias=fvg_cont_bias)
                    final_priority_score = (abs(z) * 100.0) + (float(candidate.activity_score) * 0.25) + structure_bonus + adjustments["entry_context_adjustment"] + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
                    reason = f"relative_weakness_z={z:.2f}"
                    metadata = self._build_signal_metadata(
                        entry_price=last_close,
                        ms_ctx=ms_ctx, sr_ctx=sr_ctx, tech_ctx=tech_ctx,
                        adjustments=adjustments, fvg_adjustments=fvg_adjustments,
                        management=management,
                        final_priority_score=final_priority_score,
                        leading={"benchmark": reference, "zscore": z, "side_preference": side_pref},
                    )
                    out.append(Signal(symbol=symbol, strategy=self.strategy_name, side=Side.SHORT, reason=reason, stop_price=stop, target_price=target, reference_symbol=reference, pair_id=f"{symbol}:{reference}", metadata=metadata))
                    self._record_entry_decision(symbol, "signal", [reason])
                    continue
            self._record_entry_decision(symbol, "skipped", reasons)
        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
