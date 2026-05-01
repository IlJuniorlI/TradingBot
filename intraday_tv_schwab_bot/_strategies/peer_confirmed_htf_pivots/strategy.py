# SPDX-License-Identifier: MIT
from __future__ import annotations

from ..shared import (
    Any,
    Candidate,
    HTFContext,
    Position,
    Side,
    Signal,
    _bar_close_position,
    _bar_wick_fractions,
    _discrete_score_threshold,
    _gate_snapshot,
    insufficient_bars_reason,
    _optional_float,
    _reason_with_values,
    _safe_float,
    _side_prefixed_reason,
    _side_prefixed_reasons,
    pd,
)
from ..peer_confirmed_key_levels.strategy import PeerConfirmedKeyLevelsStrategy
from ...support_resistance import zone_flip_confirmed


class PeerConfirmedHTFPivotsStrategy(PeerConfirmedKeyLevelsStrategy):
    strategy_name = 'peer_confirmed_htf_pivots'
    _ENTRY_FAMILY_ALIASES = {
        "auto": "auto",
        "pivot_reclaim": "pivot_reclaim",
        "reclaim": "pivot_reclaim",
        "pivot_rejection": "pivot_rejection",
        "rejection": "pivot_rejection",
        "pivot_continuation": "pivot_continuation",
        "continuation": "pivot_continuation",
    }

    def required_history_bars(
        self,
        symbol: str | None = None,
        positions: dict[str, Position] | None = None,
    ) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        min_bars = int(self.params.get("min_bars", 90))
        trigger_tf = max(1, int(self.params.get("trigger_timeframe_minutes", 5)))
        min_trigger_bars = int(self.params.get("min_trigger_bars", 20))
        continuation_lookback = int(
            self.params.get("pivot_continuation_interaction_lookback_bars", 10)
        )
        return max(min_bars, trigger_tf * (min_trigger_bars + continuation_lookback + 6))

    def _entry_family(self) -> str:
        raw = str(self.params.get("entry_family", "pivot_reclaim") or "pivot_reclaim")
        return self._ENTRY_FAMILY_ALIASES.get(raw.strip().lower(), "pivot_reclaim")

    def _configured_entry_families(self) -> list[str]:
        family = self._entry_family()
        if family == "auto":
            return [
                "pivot_reclaim",
                "pivot_rejection",
                "pivot_continuation",
            ]
        return [family]

    def _family_preference_bonus(self, family: str) -> float:
        key = str(family or "").strip().lower()
        if key == "pivot_reclaim":
            return float(self.params.get("pivot_reclaim_family_bonus", 0.25))
        if key == "pivot_rejection":
            return float(self.params.get("pivot_rejection_family_bonus", 0.12))
        if key == "pivot_continuation":
            return float(self.params.get("pivot_continuation_family_bonus", -0.12))
        return 0.0

    @staticmethod
    def _family_preference_source_priority(family: str) -> float:
        key = str(family or "").strip().lower()
        if key == "pivot_reclaim":
            return 2.5
        if key == "pivot_rejection":
            return 2.25
        return 1.9

    def _macro_allows(self, side: Side, macro_ctx: dict[str, Any]) -> bool:
        if not bool(self.params.get("enable_macro_confirmation", True)):
            return True
        required = max(
            0,
            int(self.params.get("require_macro_agreement_count", 1)),
        )
        if side == Side.LONG:
            return int(macro_ctx.get("long_agree", 0) or 0) >= required
        return int(macro_ctx.get("short_agree", 0) or 0) >= required

    def _use_sr_veto(self) -> bool:
        return bool(self.params.get("use_sr_veto", False))

    def _blocks_bullish_sr_entry(self, sr_ctx) -> bool:
        return super()._blocks_bullish_sr_entry(sr_ctx) if self._use_sr_veto() else False

    def _blocks_bearish_sr_entry(self, sr_ctx) -> bool:
        return super()._blocks_bearish_sr_entry(sr_ctx) if self._use_sr_veto() else False

    def dashboard_overlay_candidates(
        self,
        side: Side,
        close: float,
        ltf: pd.DataFrame,
        htf: HTFContext,
    ) -> list[dict[str, Any]] | None:
        if ltf is None or ltf.empty:
            return []
        atr = max(
            _optional_float(getattr(htf, "atr14", None)) or max(close * 0.0015, 0.01),
            max(close * 0.0005, 0.01),
        )
        distance_limit = max(0.1, float(self.params.get("pivot_battleground_max_distance_atr", 1.10)))
        zone_width = max(self._pivot_zone_width(close, atr), float(getattr(htf, "level_buffer", 0.0) or 0.0))

        def _candidate(level_obj, fallback_price: float | None, fallback_kind: str) -> dict[str, Any] | None:
            price = _optional_float(getattr(level_obj, "price", None)) if level_obj is not None else fallback_price
            if price is None or price <= 0:
                return None
            if abs(close - price) / max(atr, 1e-9) > distance_limit:
                return None
            source = str(getattr(level_obj, "source", fallback_kind) or fallback_kind) if level_obj is not None else fallback_kind
            source_priority = float(getattr(level_obj, "source_priority", 2.2) or 2.2) if level_obj is not None else 2.2
            raw_score = float(getattr(level_obj, "score", 1.0) or 1.0) if level_obj is not None else 1.0
            touches = int(getattr(level_obj, "touches", 1) or 1) if level_obj is not None else 1
            return {
                "kind": source,
                "price": float(price),
                "touches": int(touches),
                "level_score": round(raw_score + source_priority, 4),
                "source_priority": round(source_priority, 4),
                "zone_width": round(zone_width, 4),
            }

        if side == Side.LONG:
            primary = _candidate(getattr(htf, "broken_resistance", None), _optional_float(getattr(htf, "reference_low", None)), "bullish_sr_support")
            secondary = _candidate(getattr(htf, "nearest_support", None), _optional_float(getattr(htf, "reference_low", None)), "bullish_sr_support")
        else:
            primary = _candidate(getattr(htf, "broken_support", None), _optional_float(getattr(htf, "reference_high", None)), "bearish_sr_resistance")
            secondary = _candidate(getattr(htf, "nearest_resistance", None), _optional_float(getattr(htf, "reference_high", None)), "bearish_sr_resistance")

        candidates = [item for item in [primary, secondary] if item is not None]
        deduped: list[dict[str, Any]] = []
        seen: set[float] = set()
        for candidate in candidates:
            key = round(float(candidate["price"]), 4)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped

    def dashboard_select_level(
        self,
        side: Side,
        close: float,
        ltf: pd.DataFrame,
        htf: HTFContext,
    ) -> dict[str, Any] | None:
        candidates = self.dashboard_overlay_candidates(side, close, ltf, htf) or []
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
        candidate_width = None if not isinstance(candidate, dict) else _optional_float(candidate.get("zone_width"))
        width = max(
            capability_width or 0.0,
            candidate_width or 0.0,
            float(getattr(htf, "level_buffer", 0.0) or 0.0),
            self._pivot_zone_width(close, atr),
        )
        return width if width > 0 else None

    @staticmethod
    def _level_obj_metrics(level_obj: Any | None, fallback_kind: str) -> dict[str, Any] | None:
        if level_obj is None:
            return None
        price = _optional_float(getattr(level_obj, "price", None))
        if price is None or price <= 0:
            return None
        source = str(getattr(level_obj, "source", fallback_kind) or fallback_kind)
        return {
            "price": float(price),
            "kind": source,
            "source": source,
            "touches": int(getattr(level_obj, "touches", 1) or 1),
            "raw_score": max(0.0, float(getattr(level_obj, "score", 0.0) or 0.0)),
            "source_priority": max(0.0, float(getattr(level_obj, "source_priority", 1.0) or 1.0)),
        }

    @staticmethod
    def _non_fvg_zone_original_kind(kind: str | None) -> str | None:
        token = str(kind or "").strip().lower()
        if not token or "fvg" in token:
            return None
        if token in {"broken_htf_support", "broken_support"}:
            return "support"
        if token in {"broken_htf_resistance", "broken_resistance"}:
            return "resistance"
        if "support" in token or token.endswith("_low") or token.endswith("low"):
            return "support"
        if "resistance" in token or token.endswith("_high") or token.endswith("high"):
            return "resistance"
        return None

    def _non_fvg_zone_state(
        self,
        side: Side,
        *,
        kind: str | None,
        price: float,
        zone_width: float,
        flip_frame: pd.DataFrame | None,
        eps: float,
    ) -> dict[str, Any]:
        original_kind = self._non_fvg_zone_original_kind(kind)
        flip_candidate = str(kind or "").strip().lower().startswith("broken_")
        state: dict[str, Any] = {
            "original_kind": original_kind,
            "flip_candidate": bool(flip_candidate),
            "confirmed_flip": False,
            "flip_state": "original" if original_kind is not None else "not_applicable",
            "trade_alignment": True,
            "state_label": "not_applicable" if original_kind is None else "aligned_original",
            "note": None,
        }
        if original_kind is None:
            return state
        confirm_1m = max(0, int(self._support_resistance_setting("trading_flip_confirmation_1m_bars", 2) or 2))
        confirm_5m = max(0, int(self._support_resistance_setting("trading_flip_confirmation_5m_bars", 1) or 1))
        lower = float(price) - max(float(zone_width or 0.0), 0.0)
        upper = float(price) + max(float(zone_width or 0.0), 0.0)
        confirmed_flip = zone_flip_confirmed(
            original_kind,
            lower,
            upper,
            flip_frame=flip_frame,
            confirm_1m_bars=confirm_1m,
            confirm_5m_bars=confirm_5m,
            fallback_bar=None,
            eps=max(float(eps or 0.0), max(abs(float(price)) * 0.0001, 1e-6)),
        )
        state["confirmed_flip"] = bool(confirmed_flip)
        state["flip_state"] = "confirmed_flip" if confirmed_flip else ("pending_flip" if flip_candidate else "original")
        if side == Side.LONG:
            aligned = confirmed_flip if original_kind == "resistance" else not confirmed_flip
        else:
            aligned = confirmed_flip if original_kind == "support" else not confirmed_flip
        state["trade_alignment"] = bool(aligned)
        if flip_candidate:
            if confirmed_flip:
                state["state_label"] = "aligned_confirmed_flip"
                state["note"] = "confirmed_flipped_zone_supports_entry"
            else:
                state["state_label"] = "pending_flip"
                state["note"] = "flipped_level_zone_not_confirmed"
        else:
            if confirmed_flip:
                state["state_label"] = "invalidated_original"
                state["note"] = "original_zone_flipped_against_entry"
            else:
                state["state_label"] = "aligned_original"
                state["note"] = "original_zone_supports_entry"
        return state

    def _zone_alignment_adjustments(self, zone_state: dict[str, Any] | None) -> tuple[float, list[str]]:
        if not isinstance(zone_state, dict) or not zone_state or zone_state.get("original_kind") is None:
            return 0.0, []
        bonus_confirmed = max(0.0, float(self.params.get("pivot_confirmed_flip_zone_bonus", 0.55)))
        bonus_original = max(0.0, float(self.params.get("pivot_original_zone_hold_bonus", 0.18)))
        penalty_pending = max(0.0, float(self.params.get("pivot_pending_flip_zone_penalty", 0.50)))
        penalty_invalidated = max(0.0, float(self.params.get("pivot_invalidated_zone_penalty", 1.00)))
        state_label = str(zone_state.get("state_label") or "")
        note = str(zone_state.get("note") or "")
        if state_label == "aligned_confirmed_flip":
            return bonus_confirmed, [note] if note else []
        if state_label == "aligned_original":
            return bonus_original, [note] if note else []
        if state_label == "pending_flip":
            return -penalty_pending, [note] if note else []
        if state_label == "invalidated_original":
            return -penalty_invalidated, [note] if note else []
        return 0.0, [note] if note else []

    def _sr_battleground(self, side: Side, sr_ctx, close: float, atr: float, flip_frame: pd.DataFrame | None = None) -> dict[str, Any] | None:
        distance_limit = max(0.1, float(self.params.get("pivot_battleground_max_distance_atr", 1.10)))
        zone_width = max(self._pivot_zone_width(close, atr), float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0))
        zone_eps = max(float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0) * 0.15, close * 0.0001, 1e-6)
        candidates: list[dict[str, Any]] = []
        seen: set[float] = set()

        def _append(level_obj: Any | None, fallback_kind: str, role: str, flip: bool = False) -> None:
            metrics = self._level_obj_metrics(level_obj, fallback_kind)
            if metrics is None:
                return
            price = float(metrics["price"])
            if side == Side.LONG and price > close + (atr * distance_limit):
                return
            if side == Side.SHORT and price < close - (atr * distance_limit):
                return
            key = round(price, 6)
            if key in seen:
                return
            seen.add(key)
            distance_atr = abs(close - price) / max(atr, 1e-9)
            proximity_bonus = max(0.0, 1.2 - (distance_atr * 0.65))
            defended_side_bonus = 0.25 if ((side == Side.LONG and close >= price) or (side == Side.SHORT and close <= price)) else 0.10
            flip_bonus = 0.45 if flip else 0.0
            zone_state = self._non_fvg_zone_state(
                side,
                kind=str(metrics.get("kind") or fallback_kind),
                price=price,
                zone_width=zone_width,
                flip_frame=flip_frame,
                eps=zone_eps,
            )
            zone_adjustment, zone_notes = self._zone_alignment_adjustments(zone_state)
            battleground_score = (
                (float(metrics["raw_score"]) * 0.45)
                + (float(metrics["source_priority"]) * 0.45)
                + (float(metrics["touches"]) * 0.25)
                + proximity_bonus
                + defended_side_bonus
                + flip_bonus
                + zone_adjustment
            )
            candidates.append({
                **metrics,
                "role": role,
                "flip_candidate": bool(flip),
                "distance_atr": round(float(distance_atr), 4),
                "zone_width": round(float(zone_width), 4),
                "zone_alignment_adjustment": round(float(zone_adjustment), 4),
                "zone_alignment_notes": zone_notes,
                "zone_original_kind": zone_state.get("original_kind"),
                "zone_confirmed_flip": bool(zone_state.get("confirmed_flip", False)),
                "zone_flip_state": zone_state.get("flip_state"),
                "zone_trade_alignment": bool(zone_state.get("trade_alignment", True)),
                "zone_state_label": zone_state.get("state_label"),
                "battleground_score": round(float(battleground_score), 4),
            })

        if side == Side.LONG:
            _append(getattr(sr_ctx, "broken_resistance", None), "broken_resistance", "flip_support", flip=True)
            _append(getattr(sr_ctx, "nearest_support", None), "support", "support")
            for level in (getattr(sr_ctx, "supports", []) or [])[:3]:
                _append(level, "support", "support")
        else:
            _append(getattr(sr_ctx, "broken_support", None), "broken_support", "flip_resistance", flip=True)
            _append(getattr(sr_ctx, "nearest_resistance", None), "resistance", "resistance")
            for level in (getattr(sr_ctx, "resistances", []) or [])[:3]:
                _append(level, "resistance", "resistance")

        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                float(item.get("battleground_score", 0.0) or 0.0),
                -float(item.get("distance_atr", 0.0) or 0.0),
                float(item.get("raw_score", 0.0) or 0.0),
            ),
            reverse=True,
        )
        return dict(candidates[0])

    def _sr_opposing_target(self, side: Side, sr_ctx, close: float) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        seen: set[float] = set()

        def _append(level_obj: Any | None, fallback_kind: str) -> None:
            metrics = self._level_obj_metrics(level_obj, fallback_kind)
            if metrics is None:
                return
            price = float(metrics["price"])
            if side == Side.LONG and price <= close:
                return
            if side == Side.SHORT and price >= close:
                return
            key = round(price, 6)
            if key in seen:
                return
            seen.add(key)
            candidates.append(metrics)

        if side == Side.LONG:
            _append(getattr(sr_ctx, "nearest_resistance", None), "resistance")
            _append(getattr(sr_ctx, "broken_support", None), "broken_support")
            for level in (getattr(sr_ctx, "resistances", []) or [])[:4]:
                _append(level, "resistance")
        else:
            _append(getattr(sr_ctx, "nearest_support", None), "support")
            _append(getattr(sr_ctx, "broken_resistance", None), "broken_resistance")
            for level in (getattr(sr_ctx, "supports", []) or [])[:4]:
                _append(level, "support")

        if not candidates:
            return None
        candidates.sort(key=lambda item: float(item.get("price", 0.0) or 0.0), reverse=(side != Side.LONG))
        return dict(candidates[0])

    def _pivot_zone_width(self, close: float, atr: float) -> float:
        atr_mult = max(
            0.01,
            float(self.params.get("pivot_zone_atr_mult", 0.22)),
        )
        pct = max(
            0.0001,
            float(self.params.get("pivot_zone_pct", 0.0016)),
        )
        return max(float(atr) * atr_mult, float(close) * pct, 0.01)

    def _regime_signal(self, side: Side, ltf: pd.DataFrame, sr_ctx) -> dict[str, Any]:
        last = ltf.iloc[-1]
        close = _safe_float(last.get("close"), 0.0)
        vwap = _safe_float(last.get("vwap"), close)
        ema9 = _safe_float(last.get("ema9"), close)
        ema20 = _safe_float(last.get("ema20"), close)
        atr = max(
            _safe_float(last.get("atr14"), getattr(sr_ctx, "current_atr", None) or max(close * 0.0015, 0.01)),
            max(close * 0.0005, 0.01),
        )
        adx = _safe_float(last.get("adx14"), 0.0)
        ms_htf = getattr(sr_ctx, "market_structure", None)
        bias = str(getattr(ms_htf, "bias", "neutral") or "neutral")
        pivot_bias = str(getattr(ms_htf, "pivot_bias", "neutral") or "neutral")
        score = 0.0
        reasons: list[str] = []
        min_adx = float(self.params.get("min_adx14", 12.5))
        battleground = self._sr_battleground(side, sr_ctx, close, atr, ltf)
        target_level = self._sr_opposing_target(side, sr_ctx, close)
        zone_width = max(self._pivot_zone_width(close, atr), float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0))
        proximity_limit = max(0.1, float(self.params.get("pivot_battleground_max_distance_atr", 1.10)))
        min_target_clearance_atr = max(0.2, float(self.params.get("pivot_target_min_clearance_atr", 1.1)))
        battleground_price = None if battleground is None else float(battleground.get("price", 0.0) or 0.0)
        battleground_distance_atr = None if battleground is None else float(battleground.get("distance_atr", 0.0) or 0.0)
        battleground_zone_confirmed_flip = bool(battleground.get("zone_confirmed_flip", False)) if battleground is not None else False
        battleground_zone_trade_alignment = bool(battleground.get("zone_trade_alignment", True)) if battleground is not None else True
        battleground_zone_flip_state = None if battleground is None else str(battleground.get("zone_flip_state") or "")
        battleground_zone_original_kind = None if battleground is None else battleground.get("zone_original_kind")
        battleground_zone_alignment_adjustment = 0.0 if battleground is None else float(battleground.get("zone_alignment_adjustment", 0.0) or 0.0)
        target_price = None if target_level is None else float(target_level.get("price", 0.0) or 0.0)
        target_clearance_atr = None
        if target_price is not None and atr > 0:
            target_clearance_atr = abs(target_price - close) / atr

        if battleground_price is None or battleground_price <= 0:
            reasons.append("missing_sr_battleground")
        else:
            if battleground_distance_atr is not None and battleground_distance_atr <= proximity_limit:
                score += 1.5
            elif battleground_distance_atr is not None and battleground_distance_atr <= (proximity_limit + 0.45):
                score += 0.75
                reasons.append(_reason_with_values("late_to_battleground_atr", current=battleground_distance_atr, required=proximity_limit, op="<=", digits=4))
            else:
                reasons.append(_reason_with_values("too_far_from_battleground_atr", current=battleground_distance_atr, required=proximity_limit, op="<=", digits=4))
            score += min(0.75, max(0.0, float(battleground.get("raw_score", 0.0) or 0.0) * 0.12))
            if bool(battleground.get("flip_candidate", False)):
                score += 0.35
            score += battleground_zone_alignment_adjustment
            if battleground_zone_original_kind is not None and not battleground_zone_trade_alignment:
                reasons.append("zone_flipped_against_trade")
            elif bool(battleground.get("flip_candidate", False)) and not battleground_zone_confirmed_flip:
                reasons.append("flip_zone_not_confirmed")
            if side == Side.LONG:
                if close >= (battleground_price - zone_width):
                    score += 0.9
                else:
                    reasons.append(_reason_with_values("support_decisively_lost", current=close, required=battleground_price - zone_width, op=">=", digits=4))
            else:
                if close <= (battleground_price + zone_width):
                    score += 0.9
                else:
                    reasons.append(_reason_with_values("resistance_decisively_lost", current=close, required=battleground_price + zone_width, op="<=", digits=4))

        if target_clearance_atr is not None and target_clearance_atr >= min_target_clearance_atr:
            score += 0.85
        elif target_clearance_atr is not None:
            reasons.append(_reason_with_values("tight_target_clearance_atr", current=target_clearance_atr, required=min_target_clearance_atr, op=">=", digits=4))
        else:
            reasons.append("missing_opposing_sr_target")

        if side == Side.LONG:
            if bool(getattr(sr_ctx, "near_support", False)):
                score += 0.5
            if bias == "bullish":
                score += 0.65
            elif pivot_bias == "bullish":
                score += 0.45
            else:
                reasons.append(f"htf_countertrend_{bias}")
            if close > vwap:
                score += 0.5
            else:
                reasons.append(_reason_with_values("below_vwap", current=close, required=vwap, op=">", digits=4))
            if ema9 >= ema20:
                score += 0.35
            else:
                reasons.append(_reason_with_values("ema9_below_ema20", current=ema9, required=ema20, op=">=", digits=4))
            if close >= ema9:
                score += 0.35
            else:
                reasons.append(_reason_with_values("below_ema9", current=close, required=ema9, op=">=", digits=4))
            if bool(getattr(ms_htf, "bos_up", False)) or bool(getattr(ms_htf, "choch_up", False)):
                score += 0.35
        else:
            if bool(getattr(sr_ctx, "near_resistance", False)):
                score += 0.5
            if bias == "bearish":
                score += 0.65
            elif pivot_bias == "bearish":
                score += 0.45
            else:
                reasons.append(f"htf_countertrend_{bias}")
            if close < vwap:
                score += 0.5
            else:
                reasons.append(_reason_with_values("above_vwap", current=close, required=vwap, op="<", digits=4))
            if ema9 <= ema20:
                score += 0.35
            else:
                reasons.append(_reason_with_values("ema9_above_ema20", current=ema9, required=ema20, op="<=", digits=4))
            if close <= ema9:
                score += 0.35
            else:
                reasons.append(_reason_with_values("above_ema9", current=close, required=ema9, op="<=", digits=4))
            if bool(getattr(ms_htf, "bos_down", False)) or bool(getattr(ms_htf, "choch_down", False)):
                score += 0.35
        if adx >= min_adx:
            score += 0.25
        else:
            reasons.append(_reason_with_values("weak_adx", current=adx, required=min_adx, op=">=", digits=4))
        return {
            "score": float(score),
            "reasons": reasons,
            "close": close,
            "vwap": vwap,
            "ema9": ema9,
            "ema20": ema20,
            "atr": atr,
            "adx": adx,
            "pivot_price": battleground_price,
            "pivot_zone_width": float(zone_width),
            "pivot_role": None if battleground is None else str(battleground.get("role") or "battleground"),
            "pivot_source": None if battleground is None else str(battleground.get("source") or battleground.get("kind") or "battleground"),
            "pivot_kind": None if battleground is None else str(battleground.get("kind") or "battleground"),
            "pivot_raw_score": None if battleground is None else float(battleground.get("raw_score", 0.0) or 0.0),
            "pivot_battleground_score": None if battleground is None else float(battleground.get("battleground_score", 0.0) or 0.0),
            "pivot_distance_atr": battleground_distance_atr,
            "expansion_price": target_price,
            "target_level_price": target_price,
            "target_level_source": None if target_level is None else str(target_level.get("source") or target_level.get("kind") or "target"),
            "target_level_kind": None if target_level is None else str(target_level.get("kind") or "target"),
            "target_clearance_atr": None if target_clearance_atr is None else float(target_clearance_atr),
            "pivot_zone_original_kind": battleground_zone_original_kind,
            "pivot_zone_confirmed_flip": bool(battleground_zone_confirmed_flip),
            "pivot_zone_flip_state": battleground_zone_flip_state or None,
            "pivot_zone_trade_alignment": bool(battleground_zone_trade_alignment),
            "pivot_zone_alignment_adjustment": round(float(battleground_zone_alignment_adjustment), 4),
            "pivot_zone_alignment_notes": list(battleground.get("zone_alignment_notes", [])) if battleground is not None else [],
            "pivot_flip_candidate": bool(battleground.get("flip_candidate", False)) if battleground is not None else False,
            "ms_htf": ms_htf,
            "htf_bias": bias,
            "htf_pivot_bias": pivot_bias,
        }

    @staticmethod
    def _volume_ratio(ltf: pd.DataFrame, lookback: int = 5) -> float:
        if ltf is None or ltf.empty:
            return 0.0
        current = _safe_float(ltf.iloc[-1].get("volume"), 0.0)
        prior = ltf.iloc[:-1]
        if prior.empty:
            return 0.0
        baseline = max(
            _safe_float(prior["volume"].tail(max(2, lookback)).mean(), 0.0),
            1.0,
        )
        return current / baseline if baseline > 0 else 0.0

    @staticmethod
    def _pivot_distance_atr(side: Side, close: float, pivot: float, atr: float) -> float:
        if atr <= 0:
            return 0.0
        if side == Side.LONG:
            return max(0.0, close - pivot) / atr
        return max(0.0, pivot - close) / atr

    @staticmethod
    def _ltf_structure_supports_side(side: Side, ms_ltf) -> bool:
        bias = str(getattr(ms_ltf, "bias", "neutral") or "neutral")
        if side == Side.LONG:
            return bool(getattr(ms_ltf, "bos_up", False)) or bool(getattr(ms_ltf, "choch_up", False)) or bias == "bullish"
        return bool(getattr(ms_ltf, "bos_down", False)) or bool(getattr(ms_ltf, "choch_down", False)) or bias == "bearish"

    def _pivot_reclaim_signal(
        self,
        side: Side,
        ltf: pd.DataFrame,
        *,
        pivot: float,
        zone_width: float,
        close: float,
        ema9: float,
        vwap: float,
        ms_ltf,
    ) -> dict[str, Any]:
        recent = ltf.tail(max(int(self.params.get("min_trigger_bars", 20)), 6))
        prior = recent.iloc[:-1]
        if prior.empty:
            return {"family": "pivot_reclaim", "score": 0.0, "reasons": ["no_reclaim_context"]}
        close_pos = _bar_close_position(ltf)
        min_close_pos = min(0.95, max(0.05, float(self.params.get("min_trigger_close_position", 0.60))))
        reclaim_buffer_pct = max(0.0, float(self.params.get("pivot_reclaim_buffer_pct", 0.0005)))
        zone_frac = max(0.01, float(self.params.get("pivot_reclaim_zone_frac", 0.12)))
        reclaim_offset = max(zone_width * zone_frac, close * reclaim_buffer_pct)
        volume_ratio = self._volume_ratio(ltf)
        min_volume_ratio = max(0.1, float(self.params.get("min_trigger_volume_ratio", 1.0)))
        reasons: list[str] = []
        score = 0.0
        if side == Side.LONG:
            touched = _safe_float(prior["low"].min(), close) <= (pivot + zone_width)
            swept = _safe_float(prior["low"].min(), close) <= pivot
            reclaimed = close >= (pivot + reclaim_offset)
            if touched:
                score += 1.0
            else:
                reasons.append("pivot_not_touched")
            if swept:
                score += 1.0
            else:
                reasons.append("pivot_not_swept")
            if reclaimed:
                score += 1.0
            else:
                reasons.append(_reason_with_values("pivot_not_reclaimed", current=close, required=pivot + reclaim_offset, op=">=", digits=4))
            if _safe_float(ltf.iloc[-1].get("close"), close) >= _safe_float(ltf.iloc[-1].get("open"), close):
                score += 0.5
            else:
                reasons.append("trigger_bar_not_bullish")
            if self._ltf_structure_supports_side(side, ms_ltf):
                score += 0.75
            else:
                reasons.append("ltf_structure_not_bullish")
            if close >= ema9:
                score += 0.25
            if close >= vwap:
                score += 0.25
            stop_anchor = min(pivot, _safe_float(prior["low"].min(), pivot))
        else:
            touched = _safe_float(prior["high"].max(), close) >= (pivot - zone_width)
            swept = _safe_float(prior["high"].max(), close) >= pivot
            reclaimed = close <= (pivot - reclaim_offset)
            if touched:
                score += 1.0
            else:
                reasons.append("pivot_not_touched")
            if swept:
                score += 1.0
            else:
                reasons.append("pivot_not_swept")
            if reclaimed:
                score += 1.0
            else:
                reasons.append(_reason_with_values("pivot_not_reclaimed", current=close, required=pivot - reclaim_offset, op="<=", digits=4))
            if _safe_float(ltf.iloc[-1].get("close"), close) <= _safe_float(ltf.iloc[-1].get("open"), close):
                score += 0.5
            else:
                reasons.append("trigger_bar_not_bearish")
            if self._ltf_structure_supports_side(side, ms_ltf):
                score += 0.75
            else:
                reasons.append("ltf_structure_not_bearish")
            if close <= ema9:
                score += 0.25
            if close <= vwap:
                score += 0.25
            stop_anchor = max(pivot, _safe_float(prior["high"].max(), pivot))
        if close_pos >= min_close_pos if side == Side.LONG else close_pos <= (1.0 - min_close_pos):
            score += 0.5
        else:
            reasons.append(_reason_with_values("weak_bar_close", current=close_pos, required=min_close_pos if side == Side.LONG else (1.0 - min_close_pos), op=">=" if side == Side.LONG else "<=", digits=4))
        if volume_ratio >= min_volume_ratio:
            score += 0.5
        else:
            reasons.append(_reason_with_values("weak_trigger_volume", current=volume_ratio, required=min_volume_ratio, op=">=", digits=4))
        return {
            "family": "pivot_reclaim",
            "score": float(score),
            "reasons": reasons,
            "stop_anchor": float(stop_anchor),
            "trigger_level": float(pivot),
            "volume_ratio": float(volume_ratio),
        }

    def _pivot_rejection_signal(
        self,
        side: Side,
        ltf: pd.DataFrame,
        *,
        pivot: float,
        zone_width: float,
        close: float,
        ema9: float,
        ms_ltf,
    ) -> dict[str, Any]:
        recent = ltf.tail(max(int(self.params.get("min_trigger_bars", 20)), 5))
        prior = recent.iloc[:-1]
        if prior.empty:
            return {"family": "pivot_rejection", "score": 0.0, "reasons": ["no_rejection_context"]}
        close_pos = _bar_close_position(ltf)
        min_close_pos = min(0.95, max(0.05, float(self.params.get("min_trigger_close_position", 0.60))))
        min_wick_frac = min(0.95, max(0.05, float(self.params.get("pivot_rejection_min_wick_frac", 0.24))))
        volume_ratio = self._volume_ratio(ltf)
        min_volume_ratio = max(0.1, float(self.params.get("min_trigger_volume_ratio", 1.0)))
        upper_wick_frac, lower_wick_frac, _, _ = _bar_wick_fractions(ltf)
        allow_neutral = bool(self.params.get("pivot_rejection_allows_neutral_ltf_structure", True))
        reasons: list[str] = []
        score = 0.0
        if side == Side.LONG:
            touched = _safe_float(prior["low"].min(), close) <= (pivot + zone_width)
            held = close >= pivot
            reversal_candle = lower_wick_frac >= min_wick_frac or self._configured_trigger_candle_match(side, ltf)
            if touched:
                score += 1.0
            else:
                reasons.append("pivot_not_touched")
            if held:
                score += 1.0
            else:
                reasons.append(_reason_with_values("pivot_not_held", current=close, required=pivot, op=">=", digits=4))
            if reversal_candle:
                score += 0.75
            else:
                reasons.append("missing_reversal_candle")
            if self._ltf_structure_supports_side(side, ms_ltf):
                score += 0.75
            elif allow_neutral and str(getattr(ms_ltf, "bias", "neutral") or "neutral") != "bearish":
                score += 0.25
            else:
                reasons.append("ltf_structure_not_supportive")
            if close >= ema9:
                score += 0.25
            stop_anchor = min(pivot, _safe_float(prior["low"].min(), pivot))
        else:
            touched = _safe_float(prior["high"].max(), close) >= (pivot - zone_width)
            held = close <= pivot
            reversal_candle = upper_wick_frac >= min_wick_frac or self._configured_trigger_candle_match(side, ltf)
            if touched:
                score += 1.0
            else:
                reasons.append("pivot_not_touched")
            if held:
                score += 1.0
            else:
                reasons.append(_reason_with_values("pivot_not_held", current=close, required=pivot, op="<=", digits=4))
            if reversal_candle:
                score += 0.75
            else:
                reasons.append("missing_reversal_candle")
            if self._ltf_structure_supports_side(side, ms_ltf):
                score += 0.75
            elif allow_neutral and str(getattr(ms_ltf, "bias", "neutral") or "neutral") != "bullish":
                score += 0.25
            else:
                reasons.append("ltf_structure_not_supportive")
            if close <= ema9:
                score += 0.25
            stop_anchor = max(pivot, _safe_float(prior["high"].max(), pivot))
        if close_pos >= min_close_pos if side == Side.LONG else close_pos <= (1.0 - min_close_pos):
            score += 0.5
        else:
            reasons.append(_reason_with_values("weak_bar_close", current=close_pos, required=min_close_pos if side == Side.LONG else (1.0 - min_close_pos), op=">=" if side == Side.LONG else "<=", digits=4))
        if volume_ratio >= min_volume_ratio:
            score += 0.5
        else:
            reasons.append(_reason_with_values("weak_trigger_volume", current=volume_ratio, required=min_volume_ratio, op=">=", digits=4))
        return {
            "family": "pivot_rejection",
            "score": float(score),
            "reasons": reasons,
            "stop_anchor": float(stop_anchor),
            "trigger_level": float(pivot),
            "volume_ratio": float(volume_ratio),
        }

    def _pivot_continuation_signal(
        self,
        side: Side,
        ltf: pd.DataFrame,
        *,
        pivot: float,
        zone_width: float,
        close: float,
        ema9: float,
        vwap: float,
        ms_ltf,
        atr: float,
    ) -> dict[str, Any]:
        interaction_lookback = max(
            4,
            int(self.params.get("pivot_continuation_interaction_lookback_bars", 10)),
        )
        recent = ltf.tail(max(int(self.params.get("min_trigger_bars", 20)), interaction_lookback + 3))
        prior = recent.iloc[:-1]
        if prior.empty:
            return {"family": "pivot_continuation", "score": 0.0, "reasons": ["no_continuation_context"]}
        close_pos = _bar_close_position(ltf)
        min_close_pos = min(0.95, max(0.05, float(self.params.get("min_trigger_close_position", 0.60))))
        breakout_buffer_pct = max(0.0, float(self.params.get("pivot_continuation_breakout_buffer_pct", 0.0009)))
        max_distance_atr = max(0.1, float(self.params.get("pivot_continuation_max_distance_atr", 1.45)))
        volume_ratio = self._volume_ratio(ltf)
        min_volume_ratio = max(0.1, float(self.params.get("min_trigger_volume_ratio", 1.0)))
        reasons: list[str] = []
        score = 0.0
        interaction_slice = prior.tail(interaction_lookback)
        if side == Side.LONG:
            trigger_level = _safe_float(prior["high"].tail(max(3, interaction_lookback // 2)).max(), close)
            interacted = _safe_float(interaction_slice["low"].min(), close) <= (pivot + (zone_width * 1.25))
            pivot_held = _safe_float(prior["low"].min(), close) >= (pivot - zone_width)
            trigger_broke = close >= (trigger_level * (1.0 + breakout_buffer_pct))
            if interacted or self._pivot_distance_atr(side, close, pivot, atr) <= max_distance_atr:
                score += 1.0
            else:
                reasons.append(_reason_with_values("too_far_from_pivot_atr", current=self._pivot_distance_atr(side, close, pivot, atr), required=max_distance_atr, op="<=", digits=4))
            if pivot_held:
                score += 1.0
            else:
                reasons.append("pivot_not_held")
            if trigger_broke:
                score += 1.0
            else:
                reasons.append(_reason_with_values("continuation_trigger_not_broken", current=close, required=trigger_level * (1.0 + breakout_buffer_pct), op=">=", digits=4))
            if self._ltf_structure_supports_side(side, ms_ltf):
                score += 0.75
            else:
                reasons.append("ltf_structure_not_bullish")
            if close >= ema9 and close >= vwap:
                score += 0.5
            else:
                reasons.append("tape_not_aligned")
            stop_anchor = min(pivot, _safe_float(prior["low"].min(), pivot))
        else:
            trigger_level = _safe_float(prior["low"].tail(max(3, interaction_lookback // 2)).min(), close)
            interacted = _safe_float(interaction_slice["high"].max(), close) >= (pivot - (zone_width * 1.25))
            pivot_held = _safe_float(prior["high"].max(), close) <= (pivot + zone_width)
            trigger_broke = close <= (trigger_level * (1.0 - breakout_buffer_pct))
            if interacted or self._pivot_distance_atr(side, close, pivot, atr) <= max_distance_atr:
                score += 1.0
            else:
                reasons.append(_reason_with_values("too_far_from_pivot_atr", current=self._pivot_distance_atr(side, close, pivot, atr), required=max_distance_atr, op="<=", digits=4))
            if pivot_held:
                score += 1.0
            else:
                reasons.append("pivot_not_held")
            if trigger_broke:
                score += 1.0
            else:
                reasons.append(_reason_with_values("continuation_trigger_not_broken", current=close, required=trigger_level * (1.0 - breakout_buffer_pct), op="<=", digits=4))
            if self._ltf_structure_supports_side(side, ms_ltf):
                score += 0.75
            else:
                reasons.append("ltf_structure_not_bearish")
            if close <= ema9 and close <= vwap:
                score += 0.5
            else:
                reasons.append("tape_not_aligned")
            stop_anchor = max(pivot, _safe_float(prior["high"].max(), pivot))
        if close_pos >= min_close_pos if side == Side.LONG else close_pos <= (1.0 - min_close_pos):
            score += 0.5
        else:
            reasons.append(_reason_with_values("weak_bar_close", current=close_pos, required=min_close_pos if side == Side.LONG else (1.0 - min_close_pos), op=">=" if side == Side.LONG else "<=", digits=4))
        if volume_ratio >= min_volume_ratio:
            score += 0.5
        else:
            reasons.append(_reason_with_values("weak_trigger_volume", current=volume_ratio, required=min_volume_ratio, op=">=", digits=4))
        return {
            "family": "pivot_continuation",
            "score": float(score),
            "reasons": reasons,
            "stop_anchor": float(stop_anchor),
            "trigger_level": float(trigger_level),
            "volume_ratio": float(volume_ratio),
        }

    def _family_signal(
        self,
        family: str,
        side: Side,
        ltf: pd.DataFrame,
        *,
        pivot: float,
        zone_width: float,
        close: float,
        ema9: float,
        vwap: float,
        atr: float,
        ms_ltf,
    ) -> dict[str, Any]:
        if family == "pivot_reclaim":
            return self._pivot_reclaim_signal(side, ltf, pivot=pivot, zone_width=zone_width, close=close, ema9=ema9, vwap=vwap, ms_ltf=ms_ltf)
        if family == "pivot_rejection":
            return self._pivot_rejection_signal(side, ltf, pivot=pivot, zone_width=zone_width, close=close, ema9=ema9, ms_ltf=ms_ltf)
        return self._pivot_continuation_signal(side, ltf, pivot=pivot, zone_width=zone_width, close=close, ema9=ema9, vwap=vwap, ms_ltf=ms_ltf, atr=atr)

    def _select_family_payload(
        self,
        side: Side,
        ltf: pd.DataFrame,
        *,
        pivot: float,
        zone_width: float,
        close: float,
        ema9: float,
        vwap: float,
        atr: float,
        ms_ltf,
    ) -> dict[str, Any]:
        payloads = [
            self._family_signal(
                family,
                side,
                ltf,
                pivot=pivot,
                zone_width=zone_width,
                close=close,
                ema9=ema9,
                vwap=vwap,
                atr=atr,
                ms_ltf=ms_ltf,
            )
            for family in self._configured_entry_families()
        ]
        weighted_payloads: list[dict[str, Any]] = []
        family_eval: list[dict[str, Any]] = []
        for payload in payloads:
            family_name = str(payload.get("family", "unknown") or "unknown")
            raw_score = float(payload.get("score", 0.0) or 0.0)
            family_bonus = float(self._family_preference_bonus(family_name))
            selection_score = raw_score + family_bonus
            enriched = dict(payload)
            enriched["family_bonus"] = family_bonus
            enriched["selection_score"] = selection_score
            weighted_payloads.append(enriched)
            family_eval.append(
                {
                    "family": family_name,
                    "score": raw_score,
                    "selection_score": float(selection_score),
                    "family_bonus": float(family_bonus),
                    "pass": not bool(payload.get("reasons")),
                    "reasons": [str(reason) for reason in payload.get("reasons", []) if str(reason)],
                }
            )
        valid = [payload for payload in weighted_payloads if not payload.get("reasons")]
        if valid:
            selected = dict(max(valid, key=lambda payload: (float(payload.get("selection_score", 0.0) or 0.0), float(payload.get("score", 0.0) or 0.0))))
            selected["family_eval"] = family_eval
            selected["selected_pass"] = True
            return selected
        if weighted_payloads:
            selected = dict(max(weighted_payloads, key=lambda payload: (float(payload.get("selection_score", 0.0) or 0.0), float(payload.get("score", 0.0) or 0.0))))
            selected["family_eval"] = family_eval
            selected["selected_pass"] = False
            return selected
        return {
            "family": self._entry_family(),
            "score": 0.0,
            "reasons": ["no_entry_family_match"],
            "family_eval": [
                {"family": self._entry_family(), "score": 0.0, "pass": False, "reasons": ["no_entry_family_match"]}
            ],
            "selected_pass": False,
        }

    def _build_pivot_signal(
        self,
        candidate: Candidate,
        frame: pd.DataFrame,
        ltf: pd.DataFrame,
        side: Side,
        peer_ctx: dict[str, Any],
        macro_ctx: dict[str, Any],
        data=None,
        *,
        sr_ctx=None,
        ms_ltf=None,
        tech_ctx=None,
    ) -> Signal | None:
        # Accept pre-built contexts from the caller to avoid rebuilding them
        # once per side in the entry_signals loop below.
        if sr_ctx is None:
            sr_ctx = self._sr_context(candidate.symbol, frame, data)
        regime = self._regime_signal(side, ltf, sr_ctx)
        pivot_price = _optional_float(regime.get("pivot_price"))
        if pivot_price is None or pivot_price <= 0:
            failure_style = f"peer_confirmed_htf_pivots_{side.value.lower()}"
            self._set_build_failure(
                candidate.symbol,
                failure_style,
                "missing_htf_pivot",
                reasons=["missing_htf_pivot"],
                details={
                    "entry_family": self._entry_family(),
                    "peer_universe": list(peer_ctx.get("universe", [])),
                    "peer_details": dict(peer_ctx.get("details", {})),
                    "macro_details": dict(macro_ctx.get("details", {})),
                    "side_eval": {
                        "side": side.value,
                        "entry_family": self._entry_family(),
                        "gates": [_gate_snapshot("pivot_price", passed=False, note="missing_htf_pivot")],
                    },
                    "primary_blocker": "missing_htf_pivot",
                    "all_blockers": ["missing_htf_pivot"],
                },
            )
            return None
        close = float(regime["close"])
        atr = float(regime["atr"])
        zone_width = max(
            float(regime.get("pivot_zone_width", 0.0) or 0.0),
            self._pivot_zone_width(close, atr),
            float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0),
        )
        distance_atr = self._pivot_distance_atr(side, close, pivot_price, atr)
        if ms_ltf is None:
            ms_ltf = self._structure_context(ltf, "ltf")
        family_payload = self._select_family_payload(
            side,
            ltf,
            pivot=pivot_price,
            zone_width=zone_width,
            close=close,
            ema9=float(regime["ema9"]),
            vwap=float(regime["vwap"]),
            atr=atr,
            ms_ltf=ms_ltf,
        )
        regime_diagnostics = list(dict.fromkeys(str(reason) for reason in regime.get("reasons", []) if str(reason)))
        family_diagnostics = list(dict.fromkeys(str(reason) for reason in family_payload.get("reasons", []) if str(reason)))
        selected_family_pass = bool(family_payload.get("selected_pass", not family_diagnostics))
        hard_reasons: list[str] = []

        min_regime_score = _discrete_score_threshold(self.params.get("min_regime_score", 4), 4, minimum=1)
        min_trigger_score = _discrete_score_threshold(self.params.get("min_trigger_score", 2.5), 3, minimum=1)
        regime_score = float(regime.get("score", 0.0) or 0.0)
        trigger_score = float(family_payload.get("score", 0.0) or 0.0)
        if regime_score < min_regime_score:
            hard_reasons.append(_reason_with_values("weak_regime_score", current=regime_score, required=min_regime_score, op=">=", digits=4))
        if not selected_family_pass:
            if family_diagnostics:
                hard_reasons.extend(family_diagnostics)
            else:
                hard_reasons.append("selected_entry_family_failed")
        if trigger_score < min_trigger_score:
            hard_reasons.append(_reason_with_values("weak_trigger_score", current=trigger_score, required=min_trigger_score, op=">=", digits=4))

        min_peer_agreement = max(0, int(self.params.get("min_peer_agreement", 2)))
        min_peer_score = max(0, int(self.params.get("min_peer_score", 2)))
        peer_agreement = int(peer_ctx.get("bullish", 0) or 0) if side == Side.LONG else int(peer_ctx.get("bearish", 0) or 0)
        peer_score = int(peer_ctx.get("score", 0) or 0)
        directional_peer_score = peer_score if side == Side.LONG else -peer_score
        pivot_zone_trade_alignment = bool(regime.get("pivot_zone_trade_alignment", True))
        pivot_zone_confirmed_flip = bool(regime.get("pivot_zone_confirmed_flip", False))
        pivot_zone_flip_state = str(regime.get("pivot_zone_flip_state") or "")
        pivot_zone_original_kind = str(regime.get("pivot_zone_original_kind") or "")
        pivot_flip_candidate = bool(regime.get("pivot_flip_candidate", False))
        family_key = str(family_payload.get("family", self._entry_family()) or self._entry_family())
        family_preference_bonus = float(family_payload.get("family_bonus", 0.0) or 0.0)
        total_score = regime_score + trigger_score + family_preference_bonus
        if peer_agreement >= min_peer_agreement:
            total_score += 1.0
        else:
            hard_reasons.append(_reason_with_values("weak_peer_agreement", current=peer_agreement, required=min_peer_agreement, op=">=", digits=2))
        if directional_peer_score >= min_peer_score:
            total_score += 0.75
        else:
            hard_reasons.append(_reason_with_values("weak_peer_score", current=directional_peer_score, required=min_peer_score, op=">=", digits=2))
        macro_bonus = max(0.0, float(self.params.get("macro_bonus", 0.75)))
        macro_miss_penalty = max(0.0, float(self.params.get("macro_miss_penalty", 0.28)))
        macro_aligned = self._macro_allows(side, macro_ctx)
        if macro_aligned:
            total_score += macro_bonus
        elif family_key == "pivot_continuation":
            hard_reasons.append("macro_not_aligned")
        else:
            total_score -= macro_miss_penalty
        min_total_score = _discrete_score_threshold(self.params.get("min_total_score", 5.0), 6, minimum=1)
        if total_score < min_total_score:
            hard_reasons.append(_reason_with_values("weak_total_score", current=total_score, required=min_total_score, op=">=", digits=4))
        if family_key == "pivot_reclaim":
            max_distance = max(0.1, float(self.params.get("max_reclaim_distance_from_pivot_atr", 0.85)))
        elif family_key == "pivot_rejection":
            max_distance = max(0.1, float(self.params.get("max_rejection_distance_from_pivot_atr", 0.80)))
        else:
            max_distance = max(0.1, float(self.params.get("max_continuation_distance_from_pivot_atr", 1.35)))
        if distance_atr > max_distance:
            hard_reasons.append(_reason_with_values("too_far_from_pivot_atr", current=distance_atr, required=max_distance, op="<=", digits=4))
        if pivot_zone_original_kind:
            if pivot_flip_candidate and not pivot_zone_confirmed_flip:
                hard_reasons.append("unconfirmed_flipped_pivot_zone")
            elif not pivot_zone_trade_alignment:
                hard_reasons.append("pivot_zone_flipped_against_trade")

        hard_reasons.extend(self._entry_exhaustion_reasons(side, ltf, close=close, vwap=float(regime["vwap"]), ema9=float(regime["ema9"])))

        if tech_ctx is None:
            tech_ctx = self._technical_context(ltf)
        if side == Side.LONG:
            if family_key != "pivot_rejection" and self._blocks_bullish_structure_entry(ms_ltf):
                hard_reasons.append(self._bullish_structure_block_reason(ms_ltf))
            if self._blocks_bullish_sr_entry(sr_ctx):
                hard_reasons.append(self._bullish_sr_block_reason(sr_ctx))
        else:
            if family_key != "pivot_rejection" and self._blocks_bearish_structure_entry(ms_ltf):
                hard_reasons.append(self._bearish_structure_block_reason(ms_ltf))
            if self._blocks_bearish_sr_entry(sr_ctx):
                hard_reasons.append(self._bearish_sr_block_reason(sr_ctx))

        gate_snapshots = [
            _gate_snapshot("regime_score", passed=regime_score >= min_regime_score, current=round(regime_score, 4), required=min_regime_score, op=">="),
            _gate_snapshot("entry_family_pass", passed=selected_family_pass, current=int(selected_family_pass), required=1, op=">=", note=family_key),
            _gate_snapshot("trigger_score", passed=trigger_score >= min_trigger_score, current=round(trigger_score, 4), required=min_trigger_score, op=">="),
            _gate_snapshot("peer_agreement", passed=peer_agreement >= min_peer_agreement, current=int(peer_agreement), required=int(min_peer_agreement), op=">="),
            _gate_snapshot("directional_peer_score", passed=directional_peer_score >= min_peer_score, current=round(float(directional_peer_score), 4), required=int(min_peer_score), op=">="),
            _gate_snapshot("macro_alignment", passed=macro_aligned, current=int(macro_aligned), required=1, op=">=", note="required_for_continuation" if family_key == "pivot_continuation" else None),
            _gate_snapshot("pivot_total_score", passed=total_score >= min_total_score, current=round(total_score, 4), required=min_total_score, op=">="),
            _gate_snapshot("pivot_distance_atr", passed=distance_atr <= max_distance, current=round(distance_atr, 4), required=round(max_distance, 4), op="<="),
            _gate_snapshot("pivot_zone_trade_alignment", passed=pivot_zone_trade_alignment, current=int(pivot_zone_trade_alignment), required=1, op=">=", note=pivot_zone_flip_state or None),
            _gate_snapshot("pivot_zone_flip_confirmation", passed=(pivot_zone_confirmed_flip if pivot_flip_candidate else True), current=int(pivot_zone_confirmed_flip if pivot_flip_candidate else True), required=1, op=">=", note="required_for_flipped_levels" if pivot_flip_candidate else "not_required"),
        ]
        near_miss_blockers: dict[str, float] = {}
        if regime_score < min_regime_score:
            near_miss_blockers["regime_score"] = round(min_regime_score - regime_score, 4)
        if not selected_family_pass:
            near_miss_blockers["entry_family_pass"] = 1.0
        if trigger_score < min_trigger_score:
            near_miss_blockers["trigger_score"] = round(min_trigger_score - trigger_score, 4)
        if peer_agreement < min_peer_agreement:
            near_miss_blockers["peer_agreement"] = round(float(min_peer_agreement - peer_agreement), 4)
        if directional_peer_score < min_peer_score:
            near_miss_blockers["directional_peer_score"] = round(float(min_peer_score - directional_peer_score), 4)
        if total_score < min_total_score:
            near_miss_blockers["pivot_total_score"] = round(min_total_score - total_score, 4)
        if distance_atr > max_distance:
            near_miss_blockers["pivot_distance_atr"] = round(distance_atr - max_distance, 4)
        if pivot_flip_candidate and not pivot_zone_confirmed_flip:
            near_miss_blockers["pivot_zone_flip_confirmation"] = 1.0
        if not pivot_zone_trade_alignment:
            near_miss_blockers["pivot_zone_trade_alignment"] = 1.0
        side_eval = {
            "side": side.value,
            "entry_family": family_key,
            "gates": gate_snapshots,
            "regime_diagnostics": regime_diagnostics,
            "family_diagnostics": family_diagnostics,
        }

        if hard_reasons:
            failure_style = f"peer_confirmed_htf_pivots_{side.value.lower()}"
            self._set_build_failure(
                candidate.symbol,
                failure_style,
                hard_reasons[0],
                reasons=hard_reasons,
                details={
                    "entry_family": family_key,
                    "peer_universe": list(peer_ctx.get("universe", [])),
                    "peer_details": dict(peer_ctx.get("details", {})),
                    "peer_bullish": int(peer_ctx.get("bullish", 0) or 0),
                    "peer_bearish": int(peer_ctx.get("bearish", 0) or 0),
                    "peer_score": int(peer_ctx.get("score", 0) or 0),
                    "macro_details": dict(macro_ctx.get("details", {})),
                    "macro_long_agree": int(macro_ctx.get("long_agree", 0) or 0),
                    "macro_short_agree": int(macro_ctx.get("short_agree", 0) or 0),
                    "family_eval": list(family_payload.get("family_eval", [])),
                    "side_eval": side_eval,
                    "primary_blocker": hard_reasons[0],
                    "all_blockers": list(hard_reasons),
                    "near_miss_blockers": near_miss_blockers,
                    "decision_summary": {
                        "side": side.value,
                        "entry_family": family_key,
                        "primary_blocker": hard_reasons[0],
                    },
                },
            )
            return None

        stop_buffer_atr = max(0.05, float(self.params.get("stop_buffer_atr_mult", 0.52)))
        expansion_price = _optional_float(regime.get("expansion_price"))
        target_level_price = _optional_float(regime.get("target_level_price"))
        target_level_source = str(regime.get("target_level_source") or "")
        target_level_kind = str(regime.get("target_level_kind") or "")
        raw_stop_anchor = float(family_payload.get("stop_anchor", pivot_price) or pivot_price)
        if side == Side.LONG:
            stop = min(raw_stop_anchor, pivot_price) - zone_width - (atr * stop_buffer_atr)
            stop = min(stop, close * (1.0 - float(self.config.risk.default_stop_pct)))
            risk_per_share = max(0.01, close - stop)
            target = None
            if expansion_price is not None and expansion_price > close:
                expansion_target = max(close + 0.01, expansion_price - (zone_width * 0.15))
                if ((expansion_target - close) / risk_per_share) >= float(self.params.get("min_rr", 1.65)):
                    target = expansion_target
            if target is None:
                target = close + (risk_per_share * max(float(self.params.get("target_rr", 2.0)), float(self.params.get("min_rr", 1.65))))
            stop, target = self._refine_bullish_sr_levels(close, stop, target, sr_ctx)
            stop, target = self._refine_bullish_technical_levels(close, stop, target, tech_ctx, ltf)
        else:
            stop = max(raw_stop_anchor, pivot_price) + zone_width + (atr * stop_buffer_atr)
            stop = max(stop, close * (1.0 + float(self.config.risk.default_stop_pct)))
            risk_per_share = max(0.01, stop - close)
            target = None
            if expansion_price is not None and expansion_price < close:
                expansion_target = min(close - 0.01, expansion_price + (zone_width * 0.15))
                if ((close - expansion_target) / risk_per_share) >= float(self.params.get("min_rr", 1.65)):
                    target = expansion_target
            if target is None:
                target = close - (risk_per_share * max(float(self.params.get("target_rr", 2.0)), float(self.params.get("min_rr", 1.65))))
                target = max(0.01, target)
            stop, target = self._refine_bearish_sr_levels(close, stop, target, sr_ctx)
            stop, target = self._refine_bearish_technical_levels(close, stop, target, tech_ctx, ltf)

        fvg_adjustments = self._fvg_entry_adjustment_components(side, candidate.symbol, ltf, data)
        runner_allowed = bool(self.params.get("strong_setup_runner_enabled", True)) and total_score >= (min_total_score + 1)
        management = self._adaptive_management_components(
            side,
            close,
            stop,
            target,
            style="pivot",
            runner_allowed=runner_allowed,
            continuation_bias=float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0),
            strong_setup=runner_allowed,
        )
        activity_weight = max(0.0, float(self.params.get("activity_score_weight", 0.18)))
        execution_quality_score = float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
        final_priority_score = total_score + execution_quality_score + (float(candidate.activity_score) * activity_weight)
        source_priority = self._family_preference_source_priority(family_key)
        metadata = self._build_signal_metadata(
            entry_price=float(close),
            chart_ctx=self._chart_context(ltf),
            ms_ctx=ms_ltf, sr_ctx=sr_ctx, tech_ctx=tech_ctx,
            fvg_adjustments=fvg_adjustments,
            management=management,
            final_priority_score=final_priority_score,
            ms_prefix="ms_ltf",
            leading={
                "activity_score": float(candidate.activity_score),
                "setup_quality_score": round(total_score, 4),
                "execution_quality_score": round(execution_quality_score, 4),
                "macro_score": float(macro_ctx.get("long_agree", 0) or 0) if side == Side.LONG else float(macro_ctx.get("short_agree", 0) or 0),
                "entry_family": family_key,
                "regime_score": round(regime_score, 4),
                "trigger_score": round(trigger_score, 4),
                "family_preference_bonus": round(family_preference_bonus, 4),
                "peer_score": float(peer_score),
                "directional_peer_score": float(directional_peer_score),
                "peer_bullish": int(peer_ctx.get("bullish", 0) or 0),
                "peer_bearish": int(peer_ctx.get("bearish", 0) or 0),
                "peer_details": dict(peer_ctx.get("details", {})),
                "peer_universe": list(peer_ctx.get("universe", [])),
                "activity_score_weight": float(activity_weight),
                "peer_agreement": int(peer_agreement),
                "macro_long_agree": int(macro_ctx.get("long_agree", 0) or 0),
                "macro_short_agree": int(macro_ctx.get("short_agree", 0) or 0),
                "macro_details": dict(macro_ctx.get("details", {})),
                "macro_bonus_applied": round(float(macro_bonus if macro_aligned else (-macro_miss_penalty if family_key != "pivot_continuation" else 0.0)), 4),
                "pivot_total_score": round(total_score, 4),
                "pivot_price": float(pivot_price),
                "pivot_zone_width": float(zone_width),
                "pivot_distance_atr": float(distance_atr),
                "pivot_role": regime.get("pivot_role"),
                "pivot_source": regime.get("pivot_source"),
                "pivot_kind": regime.get("pivot_kind"),
                "pivot_raw_score": regime.get("pivot_raw_score"),
                "pivot_battleground_score": regime.get("pivot_battleground_score"),
                "pivot_zone_original_kind": pivot_zone_original_kind or None,
                "pivot_zone_confirmed_flip": bool(pivot_zone_confirmed_flip),
                "pivot_zone_flip_state": pivot_zone_flip_state or None,
                "pivot_zone_trade_alignment": bool(pivot_zone_trade_alignment),
                "pivot_zone_alignment_adjustment": float(regime.get("pivot_zone_alignment_adjustment", 0.0) or 0.0),
                "pivot_zone_alignment_notes": list(regime.get("pivot_zone_alignment_notes", [])),
                "pivot_expansion_price": expansion_price,
                "pivot_trigger_level": float(family_payload.get("trigger_level", pivot_price) or pivot_price),
                "target_level_price": target_level_price,
                "target_level_source": target_level_source or None,
                "target_level_kind": target_level_kind or None,
                "target_clearance_atr": regime.get("target_clearance_atr"),
                "trigger_volume_ratio": float(family_payload.get("volume_ratio", 0.0) or 0.0),
                "selected_family_pass": bool(selected_family_pass),
                "regime_diagnostics": regime_diagnostics,
                "family_diagnostics": family_diagnostics,
                "family_eval": list(family_payload.get("family_eval", [])),
                "side_eval": side_eval,
                "primary_blocker": None,
                "all_blockers": [],
                "near_miss_blockers": near_miss_blockers,
                "selection_components": {
                    "regime_score": round(regime_score, 4),
                    "trigger_score": round(trigger_score, 4),
                    "family_preference_bonus": round(family_preference_bonus, 4),
                    "peer_bonus": 1.0 if peer_agreement >= min_peer_agreement else 0.0,
                    "directional_peer_bonus": 0.75 if directional_peer_score >= min_peer_score else 0.0,
                    "macro_bonus": round(float(macro_bonus if macro_aligned else (-macro_miss_penalty if family_key != "pivot_continuation" else 0.0)), 4),
                    "execution_quality_score": round(execution_quality_score, 4),
                    "activity_component": round(float(candidate.activity_score) * activity_weight, 4),
                    "pivot_zone_alignment_adjustment": round(float(regime.get("pivot_zone_alignment_adjustment", 0.0) or 0.0), 4),
                },
                "decision_summary": {
                    "side": side.value,
                    "entry_family": family_key,
                    "primary_blocker": None,
                },
                "trend_htf_bias": str(regime.get("htf_bias", "neutral")),
                "directional_vote_edge": float(abs(int(peer_ctx.get("bullish", 0) or 0) - int(peer_ctx.get("bearish", 0) or 0))),
                "runner_quality_score": 1.0 if runner_allowed else 0.0,
                "execution_headroom_score": round(max(0.0, max_distance - distance_atr), 4),
                "source_quality_score": float(source_priority),
                "selection_quality_score": round(final_priority_score, 4),
            },
        )
        reason = f"peer_confirmed_htf_pivots_{family_key}_{side.value.lower()}"
        return Signal(
            symbol=candidate.symbol,
            strategy=self.strategy_name,
            side=side,
            reason=reason,
            stop_price=float(stop),
            target_price=float(target),
            metadata=metadata,
        )

    def entry_signals(
        self,
        candidates: list[Candidate],
        bars: dict[str, pd.DataFrame],
        positions: dict[str, Position],
        client=None,
        data=None,
    ) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        history_bars = self.required_history_bars()
        trigger_tf = max(1, int(self.params.get("trigger_timeframe_minutes", 5)))
        allow_short = bool(self.config.risk.allow_short)
        macro_ctx = self._macro_signal(bars, data=data)
        tradable_symbols = set(self._tradable_symbols())
        for candidate in candidates:
            if tradable_symbols and candidate.symbol not in tradable_symbols:
                self._record_entry_decision(candidate.symbol, "skipped", ["symbol_not_tradable"])
                continue
            frame = bars.get(candidate.symbol)
            if candidate.symbol in positions:
                self._record_entry_decision(candidate.symbol, "skipped", ["already_in_position"])
                continue
            if frame is None or len(frame) < history_bars:
                self._record_entry_decision(
                    candidate.symbol,
                    "skipped",
                    [
                        insufficient_bars_reason(
                            "insufficient_bars",
                            0 if frame is None else len(frame),
                            history_bars,
                        )
                    ],
                )
                continue
            ltf = self._resampled_frame(frame, trigger_tf, symbol=candidate.symbol, data=data)
            if ltf is None or ltf.empty or len(ltf) < max(10, int(self.params.get("min_trigger_bars", 20)) + 3):
                self._record_entry_decision(candidate.symbol, "skipped", ["missing_ltf_context"])
                continue
            peer_ctx = self._peer_signal(candidate.symbol, bars, data)
            if candidate.directional_bias == Side.LONG:
                preferred_sides = [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]
            elif candidate.directional_bias == Side.SHORT:
                if not allow_short:
                    self._record_entry_decision(candidate.symbol, "skipped", ["shorts_disabled"])
                    continue
                preferred_sides = [Side.SHORT, Side.LONG]
            else:
                preferred_sides = [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]
            side_order, evaluated_sides = self._entry_side_context(preferred_sides)
            valid_signals: list[Signal] = []
            fail_reasons: list[str] = []
            side_eval: dict[str, Any] = {}
            family_eval: dict[str, Any] = {}
            all_blockers: list[str] = []
            near_miss_blockers: dict[str, Any] = {}
            # Build side-agnostic contexts ONCE per candidate; passed into each
            # per-side builder to avoid rebuilding them when both LONG and SHORT
            # are evaluated.
            shared_sr_ctx = self._sr_context(candidate.symbol, frame, data)
            shared_ms_ltf = self._structure_context(ltf, "ltf")
            shared_tech_ctx = self._technical_context(ltf)
            for side in side_order:
                side_value = str(side.value)
                built_signal = self._build_pivot_signal(
                    candidate, frame, ltf, side, peer_ctx, macro_ctx, data=data,
                    sr_ctx=shared_sr_ctx, ms_ltf=shared_ms_ltf, tech_ctx=shared_tech_ctx,
                )
                if built_signal is not None:
                    valid_signals.append(built_signal)
                    meta = built_signal.metadata if isinstance(built_signal.metadata, dict) else {}
                    side_eval[side_value.lower()] = meta.get("side_eval")
                    family_eval[side_value.lower()] = meta.get("family_eval")
                    continue
                failure_payload = self._consume_build_failure_payload(
                    candidate.symbol,
                    f"peer_confirmed_htf_pivots_{side.value.lower()}",
                )
                if isinstance(failure_payload, dict):
                    side_key = side_value.lower()
                    side_eval[side_key] = failure_payload.get("details", {}).get("side_eval") if isinstance(failure_payload.get("details"), dict) else None
                    family_eval[side_key] = failure_payload.get("details", {}).get("family_eval") if isinstance(failure_payload.get("details"), dict) else None
                    for blocker in failure_payload.get("details", {}).get("all_blockers", []) if isinstance(failure_payload.get("details"), dict) else []:
                        token = _side_prefixed_reason(side, str(blocker))
                        if token and token not in all_blockers:
                            all_blockers.append(token)
                    for key, value in (failure_payload.get("details", {}).get("near_miss_blockers", {}) if isinstance(failure_payload.get("details"), dict) else {}).items():
                        near_miss_blockers[f"{side_key}.{key}"] = value
                    prefixed = _side_prefixed_reasons(side, failure_payload.get("reasons") or [failure_payload.get("primary_reason") or f"{side.value.lower()}_setup_not_ready"])
                    for token in prefixed:
                        if token not in fail_reasons:
                            fail_reasons.append(token)
                    continue
                failure = self._consume_build_failure(
                    candidate.symbol,
                    f"peer_confirmed_htf_pivots_{side.value.lower()}",
                )
                for token in _side_prefixed_reasons(side, [failure or f"{side.value.lower()}_setup_not_ready"]):
                    if token not in fail_reasons:
                        fail_reasons.append(token)
            if valid_signals:
                def _signal_key(sig: Signal) -> tuple[float, ...]:
                    meta = sig.metadata if isinstance(sig.metadata, dict) else {}
                    strength = float(meta.get("final_priority_score", 0.0) or 0.0)
                    preferred_side_bonus = 1.0 if candidate.directional_bias is not None and sig.side == candidate.directional_bias else 0.0
                    custom_key = self.signal_priority_key(
                        sig,
                        candidate,
                        metadata=meta,
                        strength=strength,
                        candidate_activity_score=float(candidate.activity_score),
                        rank=float(candidate.rank),
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
                self._record_entry_decision(
                    candidate.symbol,
                    "signal",
                    [signal.reason],
                    details={
                        "entry_family": meta.get("entry_family"),
                        "peer_universe": meta.get("peer_universe"),
                        "side_eval": side_eval or meta.get("side_eval"),
                        "family_eval": family_eval or meta.get("family_eval"),
                        "evaluated_sides": evaluated_sides,
                    },
                )
            else:
                blockers = all_blockers or list(fail_reasons)
                self._record_entry_decision(
                    candidate.symbol,
                    "skipped",
                    fail_reasons or ["no_setup"],
                    details={
                        "peer_universe": list(peer_ctx.get("universe", [])),
                        "peer_details": dict(peer_ctx.get("details", {})),
                        "side_eval": side_eval,
                        "family_eval": family_eval,
                        "evaluated_sides": evaluated_sides,
                        "primary_blocker": blockers[0] if blockers else None,
                        "all_blockers": blockers,
                        "near_miss_blockers": near_miss_blockers,
                        "decision_summary": {
                            "primary_blocker": blockers[0] if blockers else None,
                            "evaluated_sides": evaluated_sides,
                        },
                    },
                )
        return out
