# SPDX-License-Identifier: MIT
from ..shared import (
    Any,
    Candidate,
    HTFContext,
    Position,
    Side,
    Signal,
    _discrete_score_threshold,
    pd,
)
from ..strategy_base import BaseStrategy
from ...support_resistance import zone_flip_confirmed

class PeerConfirmedKeyLevelsStrategy(BaseStrategy):
    strategy_name = 'peer_confirmed_key_levels'

    def _active_strategy_name(self) -> str:
        return str(self.strategy_name or self.config.strategy).strip().lower()

    def _failure_style_name(self, side: Side) -> str:
        suffix = 'long' if side == Side.LONG else 'short'
        return f"{self._active_strategy_name()}_{suffix}"

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        min_bars = int(self.params.get("min_bars", 80))
        trigger_tf = max(1, int(self.params.get("trigger_timeframe_minutes", 5)))
        min_trigger_bars = int(self.params.get("min_trigger_bars", 18))
        return max(min_bars, trigger_tf * min_trigger_bars)

    @classmethod
    def normalize_params(cls, params: dict[str, Any]) -> dict[str, Any]:
        out = super().normalize_params(params)
        tradable = cls._dedupe_symbols([str(sym) for sym in out.get("tradable", []) if str(sym).strip()])
        peers = cls._dedupe_symbols([str(sym) for sym in out.get("peers", []) if str(sym).strip()])
        if tradable:
            tradable_set = set(tradable)
            peers = [symbol for symbol in peers if symbol not in tradable_set]
        out["tradable"] = tradable
        out["peers"] = peers
        return out

    @staticmethod
    def _dedupe_symbols(values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        invalid_tokens = {"NONE", "NULL", "NAN"}
        for raw in values:
            if raw is None:
                continue
            token = str(raw).upper().strip()
            if not token or token in invalid_tokens or token in seen:
                continue
            seen.add(token)
            out.append(token)
        return out

    def _tradable_symbols(self) -> list[str]:
        return self._dedupe_symbols([str(sym) for sym in self.params.get("tradable", []) if str(sym).strip()])

    def _peer_symbols(self) -> list[str]:
        tradable = set(self._tradable_symbols())
        peers = self._dedupe_symbols([str(sym) for sym in self.params.get("peers", []) if str(sym).strip()])
        return [symbol for symbol in peers if symbol not in tradable]

    def _confirmation_universe(self) -> list[str]:
        return self._dedupe_symbols(self._tradable_symbols() + self._peer_symbols())


    def strategy_logic_default(self, section: str, key: str, default: Any) -> Any:
        if section == "shared_exit" and key in {"use_chart_pattern_exit", "use_structure_exit", "use_sr_loss_exit"}:
            return False
        return default

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)

    def _trade_management_mode(self) -> str:
        return self.config.risk.trade_management_mode

    @staticmethod
    def _ladder_bounds(price: float, zone_width: float) -> tuple[float, float]:
        width = max(float(zone_width or 0.0), 0.0)
        return float(price) - width, float(price) + width

    def _ladder_exit_signal(self, position: Position, frame: pd.DataFrame, close: float, ema9: float, ema20: float, vwap: float, data=None) -> tuple[bool, str]:
        if self._trade_management_mode() != "adaptive_ladder":
            return False, "hold"
        metadata = position.metadata if isinstance(position.metadata, dict) else {}
        if not bool(metadata.get("ladder_management_enabled")):
            return False, "hold"
        defense_price = self._optional_float(metadata.get("ladder_defense_price"))
        if defense_price is None or defense_price <= 0:
            return False, "hold"
        defense_zone_width = max(0.0, self._optional_float(metadata.get("ladder_defense_zone_width"), 0.0) or 0.0)
        symbol = str(metadata.get("underlying") or position.symbol)
        sr_ctx = None
        if data is not None and hasattr(data, "get_support_resistance"):
            try:
                sr_ctx = data.get_support_resistance(
                    symbol,
                    current_price=close,
                    flip_frame=frame,
                    mode="trading",
                    timeframe_minutes=int(self.params.get("htf_timeframe_minutes", 60)),
                    lookback_days=int(self.params.get("htf_lookback_days", 60)),
                    refresh_seconds=int(self.params.get("htf_refresh_seconds", 120)),
                    use_prior_day_high_low=bool(self._support_resistance_setting("use_prior_day_high_low", True)),
                    use_prior_week_high_low=bool(self._support_resistance_setting("use_prior_week_high_low", True)),
                    allow_refresh=True,
                )
            except Exception:
                sr_ctx = None
        buffer = max(
            defense_zone_width * 0.25,
            float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0),
            close * 0.0005,
        )
        eps = max(float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0) * 0.15, close * 0.0001, 1e-6)
        confirm_1m = max(0, int(self._support_resistance_setting("trading_flip_confirmation_1m_bars", 2) or 2))
        confirm_5m = max(0, int(self._support_resistance_setting("trading_flip_confirmation_5m_bars", 1) or 1))
        lower, upper = self._ladder_bounds(defense_price, defense_zone_width)
        if position.side == Side.LONG:
            zone_lost = zone_flip_confirmed("support", lower, upper, flip_frame=frame, confirm_1m_bars=confirm_1m, confirm_5m_bars=confirm_5m, fallback_bar=None, eps=eps)
            if zone_lost and close <= lower - buffer:
                return True, f"ladder_support_lost:{defense_price:.4f}"
            ms = getattr(sr_ctx, "market_structure", None)
            if ms is not None and (self._active_structure_break(bool(getattr(ms, "choch_down", False)), getattr(ms, "choch_down_age_bars", None)) or self._active_structure_break(bool(getattr(ms, "bos_down", False)), getattr(ms, "bos_down_age_bars", None))) and close < min(ema9, ema20, vwap):
                return True, f"ladder_structure_fail_long:{defense_price:.4f}"
        else:
            zone_lost = zone_flip_confirmed("resistance", lower, upper, flip_frame=frame, confirm_1m_bars=confirm_1m, confirm_5m_bars=confirm_5m, fallback_bar=None, eps=eps)
            if zone_lost and close >= upper + buffer:
                return True, f"ladder_resistance_lost:{defense_price:.4f}"
            ms = getattr(sr_ctx, "market_structure", None)
            if ms is not None and (self._active_structure_break(bool(getattr(ms, "choch_up", False)), getattr(ms, "choch_up_age_bars", None)) or self._active_structure_break(bool(getattr(ms, "bos_up", False)), getattr(ms, "bos_up_age_bars", None))) and close > max(ema9, ema20, vwap):
                return True, f"ladder_structure_fail_short:{defense_price:.4f}"
        return False, "hold"

    @staticmethod
    def _round_number_hit(price: float, tolerance_pct: float) -> bool:
        if price <= 0:
            return False
        if price >= 1000:
            step = 50.0
        elif price >= 300:
            step = 10.0
        elif price >= 100:
            step = 5.0
        else:
            step = 1.0
        anchor = round(price / step) * step
        return abs(price - anchor) <= max(step * 0.1, price * tolerance_pct)

    @staticmethod
    def _clamp_weight(value: float, default: float) -> float:
        try:
            numeric = float(value)
        except Exception:
            numeric = float(default)
        return max(0.0, min(1.0, numeric))

    def _level_score_raw_htf_weight(self) -> float:
        return self._clamp_weight(self.params.get("level_score_raw_htf_weight", 0.65), 0.60)

    def _hourly_bias(self, htf: HTFContext, close: float) -> tuple[str, int, int]:
        bull = 0
        bear = 0
        ema_fast = self._optional_float(getattr(htf, "ema_fast", None))
        ema_slow = self._optional_float(getattr(htf, "ema_slow", None))
        if ema_fast is not None:
            if close > ema_fast:
                bull += 1
            elif close < ema_fast:
                bear += 1
        if ema_fast is not None and ema_slow is not None:
            if ema_fast > ema_slow:
                bull += 1
            elif ema_fast < ema_slow:
                bear += 1
        trend_bias = str(getattr(htf, "trend_bias", "neutral"))
        if trend_bias == "bullish":
            bull += 1
        elif trend_bias == "bearish":
            bear += 1
        return "bullish" if bull >= 2 else ("bearish" if bear >= 2 else "neutral"), bull, bear

    def _peer_signal(self, symbol: str, bars: dict[str, pd.DataFrame], data) -> dict[str, Any]:
        universe = self._confirmation_universe()
        tf = int(self.params.get("htf_timeframe_minutes", 60))
        lookback_days = int(self.params.get("htf_lookback_days", 60))
        pivot_span = int(self.params.get("htf_pivot_span", 2))
        max_lvls = int(self.params.get("htf_max_levels_per_side", 6))
        atr_tol = float(self.params.get("htf_atr_tolerance_mult", 0.35))
        pct_tol = float(self.params.get("htf_pct_tolerance", 0.0030))
        stop_atr = float(self.params.get("htf_stop_buffer_atr_mult", 0.25))
        ema_fast_span = int(self.params.get("htf_ema_fast_span", 50))
        ema_slow_span = int(self.params.get("htf_ema_slow_span", 200))
        refresh_seconds = int(self.params.get("htf_refresh_seconds", 120))
        score = 0
        bullish = 0
        bearish = 0
        details: dict[str, str] = {}
        for peer in universe:
            if peer == symbol:
                continue
            frame = bars.get(peer)
            if frame is None or frame.empty:
                details[peer] = "missing"
                continue
            close = self._safe_float(frame.iloc[-1]["close"])
            ltf = self._resampled_frame(frame, int(self.params.get("trigger_timeframe_minutes", 5)), symbol=peer, data=data)
            htf = self._htf_context(
                peer,
                data,
                timeframe_minutes=tf,
                lookback_days=lookback_days,
                pivot_span=pivot_span,
                max_levels_per_side=max_lvls,
                atr_tolerance_mult=atr_tol,
                pct_tolerance=pct_tol,
                stop_buffer_atr_mult=stop_atr,
                ema_fast_span=ema_fast_span,
                ema_slow_span=ema_slow_span,
                refresh_seconds=refresh_seconds,
                current_price=close,
                use_prior_day_high_low=bool(self._support_resistance_setting("use_prior_day_high_low", True)),
                use_prior_week_high_low=bool(self._support_resistance_setting("use_prior_week_high_low", True)),
            )
            bull_votes = 0
            bear_votes = 0
            ema_fast = self._optional_float(getattr(htf, "ema_fast", None))
            if ema_fast is not None:
                if close > ema_fast:
                    bull_votes += 1
                elif close < ema_fast:
                    bear_votes += 1
            if ltf is not None and not ltf.empty:
                last = ltf.iloc[-1]
                ltf_close = self._safe_float(last["close"], close)
                ltf_vwap = self._safe_float(last.get("vwap"), ltf_close)
                ema9 = self._safe_float(last.get("ema9"), ltf_close)
                ema20 = self._safe_float(last.get("ema20"), ltf_close)
                if ltf_close > ltf_vwap and ema9 >= ema20:
                    bull_votes += 1
                elif ltf_close < ltf_vwap and ema9 <= ema20:
                    bear_votes += 1
            session_open = self._session_open_price(frame)
            if session_open is not None:
                if close > session_open:
                    bull_votes += 1
                elif close < session_open:
                    bear_votes += 1
            if bull_votes >= 2 and bull_votes > bear_votes:
                score += 1
                bullish += 1
                details[peer] = "bullish"
            elif bear_votes >= 2 and bear_votes > bull_votes:
                score -= 1
                bearish += 1
                details[peer] = "bearish"
            else:
                details[peer] = "neutral"
        return {"score": score, "bullish": bullish, "bearish": bearish, "details": details, "universe": [peer for peer in universe if peer != symbol]}

    def _macro_signal(self, bars: dict[str, pd.DataFrame], data=None) -> dict[str, Any]:
        if not bool(self.params.get("enable_macro_confirmation", True)):
            return {"long_agree": 0, "short_agree": 0, "details": {}, "enabled": False}
        details: dict[str, str] = {}
        long_agree = 0
        short_agree = 0
        trigger_tf = int(self.params.get("trigger_timeframe_minutes", 5))
        for key, bullish_when_up in (("dollar_symbol", False), ("bond_symbol", True), ("volatility_symbol", False)):
            symbol = str(self.params.get(key, "")).upper().strip()
            if not symbol:
                continue
            frame = bars.get(symbol)
            if frame is None or frame.empty:
                details[key] = "missing"
                continue
            macro = self._resampled_frame(frame, trigger_tf, symbol=symbol, data=data)
            if macro is None or macro.empty:
                details[key] = "missing"
                continue
            last = macro.iloc[-1]
            close = self._safe_float(last["close"])
            vwap = self._optional_float(last.get("vwap"))
            volume = self._optional_float(last.get("volume"))
            ema9 = self._safe_float(last.get("ema9"), close)
            ema20 = self._safe_float(last.get("ema20"), close)
            ret5 = self._safe_float(last.get("ret5"), 0.0)
            use_vwap = vwap is not None and volume is not None and volume > 0.0
            if use_vwap:
                up = close > float(vwap) and ema9 >= ema20 and ret5 >= 0.0
                down = close < float(vwap) and ema9 <= ema20 and ret5 <= 0.0
            else:
                session_slice = macro
                try:
                    normalized_index = pd.to_datetime(macro.index, errors="coerce")
                    valid_index = normalized_index[normalized_index.notna()]
                    if len(valid_index) > 0:
                        session_mask = normalized_index.normalize() == valid_index[-1].normalize()
                        if bool(session_mask.any()):
                            session_slice = macro.loc[session_mask]
                except Exception:
                    session_slice = macro
                session_open = self._safe_float(session_slice.iloc[0].get("open"), close) if session_slice is not None and not session_slice.empty else close
                recent_window = macro.tail(6)
                prior_bars = recent_window.iloc[:-1] if len(recent_window) > 1 else recent_window.iloc[0:0]
                recent_5bar_high = self._safe_float(prior_bars["high"].max(), close) if not prior_bars.empty and "high" in prior_bars.columns else close
                recent_5bar_low = self._safe_float(prior_bars["low"].min(), close) if not prior_bars.empty and "low" in prior_bars.columns else close
                bull_votes = 0
                bear_votes = 0
                if ema9 > ema20:
                    bull_votes += 1
                elif ema9 < ema20:
                    bear_votes += 1
                if ret5 > 0.0:
                    bull_votes += 1
                elif ret5 < 0.0:
                    bear_votes += 1
                bull_price_action = close > session_open or close > ema9 or close > recent_5bar_high
                bear_price_action = close < session_open or close < ema9 or close < recent_5bar_low
                if bull_price_action:
                    bull_votes += 1
                if bear_price_action:
                    bear_votes += 1
                up = bull_votes >= 2 and bull_votes > bear_votes
                down = bear_votes >= 2 and bear_votes > bull_votes
            if bullish_when_up:
                if up:
                    long_agree += 1
                    details[key] = "bullish_for_longs"
                elif down:
                    short_agree += 1
                    details[key] = "bullish_for_shorts"
                else:
                    details[key] = "neutral"
            else:
                if down:
                    long_agree += 1
                    details[key] = "bullish_for_longs"
                elif up:
                    short_agree += 1
                    details[key] = "bullish_for_shorts"
                else:
                    details[key] = "neutral"
        return {"long_agree": long_agree, "short_agree": short_agree, "details": details, "enabled": True}

    @staticmethod
    def _peer_level_source_priority(kind: str) -> float:
        name = str(kind or "").strip().lower()
        if "prior_week" in name:
            return 3.0
        if "prior_day" in name:
            return 2.0
        if "fvg" in name:
            return 1.75
        return 1.0

    def _collapse_peer_levels(self, candidates: list[dict[str, Any]], close: float, htf: HTFContext) -> list[dict[str, Any]]:
        if not candidates:
            return []
        atr = self._optional_float(getattr(htf, "atr14", None)) or max(float(close) * 0.0015, 0.01)
        tolerance = max(
            float(atr) * float(self.params.get("htf_atr_tolerance_mult", 0.35)),
            float(close) * float(self.params.get("htf_pct_tolerance", 0.0030)),
        )
        ordered = sorted(candidates, key=lambda item: float(item.get("price", 0.0) or 0.0))
        groups: list[list[dict[str, Any]]] = []
        for candidate in ordered:
            if not groups:
                groups.append([candidate])
                continue
            prior_prices = [float(item.get("price", 0.0) or 0.0) for item in groups[-1]]
            anchor = sum(prior_prices) / len(prior_prices)
            if abs(float(candidate.get("price", 0.0) or 0.0) - anchor) <= max(float(tolerance), 1e-9):
                groups[-1].append(candidate)
            else:
                groups.append([candidate])

        def _preference(item: dict[str, Any]) -> tuple[float, float, int, float, float]:
            return (
                float(item.get("level_score", 0.0) or 0.0),
                float(item.get("raw_htf_score", item.get("level_score", 0.0)) or 0.0),
                int(item.get("touches", 1) or 1),
                float(item.get("source_priority", 1.0) or 1.0),
                -abs(float(item.get("price", 0.0) or 0.0) - float(close)),
            )

        collapsed = [max(group, key=_preference) for group in groups]
        collapsed.sort(key=lambda item: float(item.get("price", 0.0) or 0.0))
        return collapsed

    def dashboard_candidate_levels(self, close: float, htf: HTFContext, side: Side) -> list[dict[str, Any]]:
        return self._candidate_levels(close, htf, side)

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
        return self._zone_width_for_level(side, close, atr, level_price, htf, candidate)

    def _candidate_levels(self, close: float, htf: HTFContext, side: Side) -> list[dict[str, Any]]:
        tolerance_pct = float(self.params.get("level_round_number_tolerance_pct", 0.0020))
        raw_weight = self._level_score_raw_htf_weight()
        levels: list[dict[str, Any]] = []

        def _append(
            kind: str,
            price: float | None,
            touches: int = 1,
            base_score: float = 1.0,
            zone_lower: float | None = None,
            zone_upper: float | None = None,
            zone_midpoint: float | None = None,
            raw_htf_score: float | None = None,
            source_priority: float | None = None,
        ):
            if price is None or price <= 0:
                return
            resolved_source_priority = float(source_priority if source_priority is not None else self._peer_level_source_priority(kind))
            local_confluence_score = 0.0
            if touches >= 2:
                local_confluence_score += 1.0
            if self._round_number_hit(float(price), tolerance_pct):
                local_confluence_score += 1.0
            ema_fast = self._optional_float(getattr(htf, "ema_fast", None))
            ema_slow = self._optional_float(getattr(htf, "ema_slow", None))
            if ema_fast is not None and abs(float(price) - ema_fast) <= max(float(close) * tolerance_pct, float(getattr(htf, "atr14", 0.0) or 0.0) * 0.25):
                local_confluence_score += 1.0
            if ema_slow is not None and abs(float(price) - ema_slow) <= max(float(close) * tolerance_pct, float(getattr(htf, "atr14", 0.0) or 0.0) * 0.25):
                local_confluence_score += 1.0
            raw_strength = max(float(raw_htf_score if raw_htf_score is not None else base_score), float(base_score), 0.0)
            local_quality_score = float(base_score) + local_confluence_score
            blended_level_score = (raw_strength * raw_weight) + (local_quality_score * (1.0 - raw_weight))
            payload = {
                "kind": kind,
                "price": float(price),
                "touches": int(touches),
                "level_score": float(blended_level_score),
                "blended_level_score": float(blended_level_score),
                "raw_htf_score": float(raw_strength),
                "local_confluence_score": float(local_confluence_score),
                "local_quality_score": float(local_quality_score),
                "level_score_raw_htf_weight": float(raw_weight),
                "source_priority": float(resolved_source_priority),
            }
            if zone_lower is not None and zone_upper is not None:
                payload["zone_lower"] = float(zone_lower)
                payload["zone_upper"] = float(zone_upper)
                payload["zone_midpoint"] = float(zone_midpoint if zone_midpoint is not None else (float(zone_lower) + float(zone_upper)) / 2.0)
            levels.append(payload)

        def _append_level_obj(level_obj: Any | None, default_kind: str) -> None:
            if level_obj is None:
                return
            kind = str(getattr(level_obj, "source", None) or default_kind)
            source_priority = float(getattr(level_obj, "source_priority", self._peer_level_source_priority(kind)) or self._peer_level_source_priority(kind))
            _append(
                kind,
                self._optional_float(getattr(level_obj, "price", None)),
                touches=int(getattr(level_obj, "touches", 1) or 1),
                base_score=max(1.5, source_priority),
                raw_htf_score=self._optional_float(getattr(level_obj, "score", None)),
                source_priority=source_priority,
            )

        if side == Side.LONG:
            for level_obj in (getattr(htf, "supports", []) or []):
                _append_level_obj(level_obj, "nearest_htf_support")
            _append_level_obj(getattr(htf, "broken_resistance", None), "broken_htf_resistance")
            for gap in (getattr(htf, "bullish_fvgs", []) or []):
                lower = self._optional_float(getattr(gap, "lower", None))
                upper = self._optional_float(getattr(gap, "upper", None))
                midpoint = self._optional_float(getattr(gap, "midpoint", None))
                if lower is None or upper is None or midpoint is None:
                    continue
                if lower > close and upper > close:
                    continue
                _append("bullish_htf_fvg", midpoint, base_score=1.75, zone_lower=lower, zone_upper=upper, zone_midpoint=midpoint)
        else:
            for level_obj in (getattr(htf, "resistances", []) or []):
                _append_level_obj(level_obj, "nearest_htf_resistance")
            _append_level_obj(getattr(htf, "broken_support", None), "broken_htf_support")
            for gap in (getattr(htf, "bearish_fvgs", []) or []):
                lower = self._optional_float(getattr(gap, "lower", None))
                upper = self._optional_float(getattr(gap, "upper", None))
                midpoint = self._optional_float(getattr(gap, "midpoint", None))
                if lower is None or upper is None or midpoint is None:
                    continue
                if lower < close and upper < close:
                    continue
                _append("bearish_htf_fvg", midpoint, base_score=1.75, zone_lower=lower, zone_upper=upper, zone_midpoint=midpoint)
        return self._collapse_peer_levels(levels, close, htf)

    def _zone_width_for_level(self, side: Side, close: float, atr: float, level_price: float, htf: HTFContext, candidate: dict[str, Any] | None = None) -> float:
        min_zone = max(close * float(self.params.get("zone_pct", 0.0015)), 0.01)
        zone_floor = float(min_zone)
        zone_width = max(float(self.params.get("zone_atr_mult", 0.21)) * atr, min_zone)
        if isinstance(candidate, dict):
            zone_lower = self._optional_float(candidate.get("zone_lower"))
            zone_upper = self._optional_float(candidate.get("zone_upper"))
            if zone_lower is not None and zone_upper is not None and zone_upper >= zone_lower:
                # Use the FVG span to raise the zone FLOOR (so very small FVGs
                # don't shrink below min_zone), but do NOT let a tall FVG blow
                # the zone out to the full gap height. The zone around the
                # midpoint should stay ATR-proportional, same as pivot zones.
                fvg_half_height = max((float(zone_upper) - float(zone_lower)) / 2.0, min_zone)
                atr_based_cap = max(zone_width, min_zone)
                explicit_zone_half_width = min(fvg_half_height, atr_based_cap)
                zone_floor = max(zone_floor, explicit_zone_half_width)
        opposite_side = Side.SHORT if side == Side.LONG else Side.LONG
        opposite_levels = self._candidate_levels(close, htf, opposite_side)
        opposing_gaps = []
        for candidate in opposite_levels:
            other_price = float(candidate["price"])
            gap = (other_price - float(level_price)) if side == Side.LONG else (float(level_price) - other_price)
            if gap > 0:
                opposing_gaps.append(gap)
        if opposing_gaps:
            zone_width = min(zone_width, max(zone_floor, min(opposing_gaps) * 0.33))
        same_side_prices = [float(candidate["price"]) for candidate in self._candidate_levels(close, htf, side)]
        same_side_gaps = [abs(other_price - float(level_price)) for other_price in same_side_prices if abs(other_price - float(level_price)) > 1e-9]
        if same_side_gaps:
            zone_width = min(zone_width, max(zone_floor, min(same_side_gaps) * 0.45))
        return max(zone_floor, zone_width)

    def _peer_level_selection_details(self, side: Side, candidate: dict[str, Any], ltf: pd.DataFrame, close: float, atr: float) -> dict[str, float]:
        level_price = float(candidate.get("price", 0.0) or 0.0)
        zone_width = float(candidate.get("zone_width", 0.0) or 0.0)
        source_priority = float(candidate.get("source_priority", self._peer_level_source_priority(str(candidate.get("kind") or ""))) or 0.0)
        trigger_preview = float(self._trigger_score(side, ltf, level_price, zone_width).get("score", 0.0) or 0.0)
        distance = abs(float(close) - level_price)
        distance_atr = distance / max(float(atr), 1e-9)
        selection_score = (
            float(candidate.get("level_score", 0.0) or 0.0)
            + (float(trigger_preview) * 0.85)
            + (source_priority * 0.45)
            - (min(distance_atr, 2.5) * 0.10)
        )
        return {
            "selection_trigger_score": float(trigger_preview),
            "selection_source_priority": source_priority,
            "selection_distance_atr": float(distance_atr),
            "selection_score": float(selection_score),
            "selection_level_score": float(candidate.get("level_score", 0.0) or 0.0),
            "selection_raw_htf_score": float(candidate.get("raw_htf_score", candidate.get("level_score", 0.0)) or 0.0),
            "selection_local_quality_score": float(candidate.get("local_quality_score", candidate.get("level_score", 0.0)) or 0.0),
        }

    @staticmethod
    def _level_selection_priority_key(payload: dict[str, Any]) -> tuple[float, float, float, float, float]:
        return (
            float(payload.get("selection_trigger_score", 0.0) or 0.0),
            float(payload.get("level_score", 0.0) or 0.0),
            float(payload.get("selection_source_priority", payload.get("source_priority", 0.0)) or 0.0),
            -float(payload.get("selection_distance_atr", float("inf")) or float("inf")),
            float(payload.get("selection_score", 0.0) or 0.0),
        )

    def dashboard_select_level(self, side: Side, close: float, ltf: pd.DataFrame, htf: HTFContext) -> dict[str, Any] | None:
        return self._select_level(side, close, ltf, htf)

    def _select_level(self, side: Side, close: float, ltf: pd.DataFrame, htf: HTFContext) -> dict[str, Any] | None:
        if ltf is None or ltf.empty:
            return None
        atr = self._safe_float(ltf.iloc[-1].get("atr14"), self._optional_float(getattr(htf, "atr14", None)) or max(close * 0.0015, 0.01))
        recent = ltf.tail(4)
        best: dict[str, Any] | None = None
        for candidate in self._candidate_levels(close, htf, side):
            price = float(candidate["price"])
            zone = self._zone_width_for_level(side, close, atr, price, htf, candidate)
            touched = bool(float(recent["low"].min()) <= price + zone and float(recent["high"].max()) >= price - zone)
            if not touched:
                continue
            distance = abs(close - price)
            payload = {**candidate, "zone_width": zone, "distance": distance}
            payload.update(self._peer_level_selection_details(side, payload, ltf, close, atr))
            if best is None:
                best = payload
                continue

            if self._level_selection_priority_key(payload) > self._level_selection_priority_key(best):
                best = payload
        return best

    def _configured_trigger_candle_summary(self, side: Side, ltf: pd.DataFrame) -> dict[str, Any]:
        # _directional_candle_signal slices internally to CANDLE_CONTEXT_BARS
        # so TA-Lib has enough context — pass the full ltf frame.
        frame = ltf if ltf is not None and not ltf.empty else pd.DataFrame()
        summary = self._directional_candle_signal(frame, side)
        summary["matches"] = set(summary.get("matches", []))
        return summary

    def _configured_trigger_candle_match(self, side: Side, ltf: pd.DataFrame) -> bool:
        return bool(self._configured_trigger_candle_summary(side, ltf).get("confirmed"))

    @staticmethod
    def _clamp01(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    def _trigger_quality_caps(self) -> dict[str, float]:
        return {
            "reclaim_reject": max(0.0, float(self.params.get("trigger_reclaim_quality_bonus_cap", 0.80))),
            "zone_interaction": max(0.0, float(self.params.get("trigger_zone_interaction_bonus_cap", 0.50))),
            "candle_quality": max(0.0, float(self.params.get("trigger_candle_quality_bonus_cap", 0.50))),
            "volume_quality": max(0.0, float(self.params.get("trigger_volume_quality_bonus_cap", 0.40))),
            "range_expansion": max(0.0, float(self.params.get("trigger_range_expansion_bonus_cap", 0.40))),
            "max_total": max(0.0, float(self.params.get("trigger_quality_max_bonus", 2.00))),
        }

    def _trigger_quality_bonus(
        self,
        side: Side,
        ltf: pd.DataFrame,
        *,
        level_price: float,
        zone_width: float,
        choch: bool,
        volume_ratio: float,
        pattern_or_body: bool,
        reaction_confirmed: bool,
        sweep: bool,
    ) -> dict[str, Any]:
        if not bool(self.params.get("trigger_quality_bonus_enabled", True)):
            return {"quality_bonus": 0.0, "quality_reasons": [], "quality_breakdown": {}}
        if ltf is None or ltf.empty:
            return {"quality_bonus": 0.0, "quality_reasons": [], "quality_breakdown": {}}
        last = ltf.iloc[-1]
        prior = ltf.iloc[:-1]
        if prior.empty:
            return {"quality_bonus": 0.0, "quality_reasons": [], "quality_breakdown": {}}

        close = self._safe_float(last.get("close"), level_price)
        open_ = self._safe_float(last.get("open"), close)
        atr = max(self._safe_float(last.get("atr14"), max(abs(float(close)) * 0.0015, 0.01)), max(abs(float(close)) * 0.0005, 0.01))
        upper_wick_frac, lower_wick_frac, body_frac, bar_range = self._bar_wick_fractions(ltf)
        close_pos = self._bar_close_position(ltf)
        recent_ranges = (prior["high"] - prior["low"]).tail(10) if {"high", "low"}.issubset(prior.columns) else pd.Series(dtype=float)
        avg_range = self._safe_float(recent_ranges.mean(), bar_range if bar_range > 0 else atr)
        avg_range = max(avg_range, atr * 0.35, 1e-9)
        range_ratio = float(bar_range) / avg_range if avg_range > 0 else 0.0
        zone_scale = max(float(zone_width), atr * 0.15, abs(float(level_price)) * 0.0005, 1e-6)
        caps = self._trigger_quality_caps()
        components: dict[str, float] = {}
        sweep_window = ltf.tail(4)

        context_active = bool(reaction_confirmed or choch or sweep or pattern_or_body)
        directional_close_score = self._clamp01((close_pos - 0.45) / 0.55) if side == Side.LONG else self._clamp01((0.55 - close_pos) / 0.55)
        wick_score = self._clamp01(1.0 - ((upper_wick_frac if side == Side.LONG else lower_wick_frac) / 0.45))
        body_score = self._clamp01((body_frac - 0.40) / 0.40)
        directional_body = max(0.0, close - open_) if side == Side.LONG else max(0.0, open_ - close)
        directional_body_score = self._clamp01(directional_body / max(bar_range, 1e-9))

        if context_active and pattern_or_body:
            candle_bonus = caps["candle_quality"] * (
                (directional_body_score * 0.35)
                + (body_score * 0.30)
                + (directional_close_score * 0.20)
                + (wick_score * 0.15)
            )
            if candle_bonus > 0.0:
                components["candle_quality_bonus"] = candle_bonus

        if context_active and volume_ratio > 1.0:
            volume_bonus = caps["volume_quality"] * self._clamp01((float(volume_ratio) - 1.0) / 0.80)
            if volume_bonus > 0.0:
                components["volume_quality_bonus"] = volume_bonus

        if context_active and range_ratio > 1.0:
            range_bonus = caps["range_expansion"] * self._clamp01((float(range_ratio) - 1.0) / 0.80)
            if range_bonus > 0.0:
                components["range_expansion_bonus"] = range_bonus

        if side == Side.LONG:
            sweep_extreme = self._safe_float(sweep_window["low"].min(), close) if "low" in sweep_window.columns else close
            recovery_distance = max(0.0, close - float(level_price))
            penetration_ratio = max(0.0, float(level_price) - float(sweep_extreme)) / zone_scale
        else:
            sweep_extreme = self._safe_float(sweep_window["high"].max(), close) if "high" in sweep_window.columns else close
            recovery_distance = max(0.0, float(level_price) - close)
            penetration_ratio = max(0.0, float(sweep_extreme) - float(level_price)) / zone_scale

        if reaction_confirmed:
            reclaim_reject_bonus = caps["reclaim_reject"] * (
                (self._clamp01(penetration_ratio / 1.25) * 0.35)
                + (self._clamp01(recovery_distance / zone_scale) * 0.30)
                + (directional_close_score * 0.20)
                + (wick_score * 0.15)
            )
            if reclaim_reject_bonus > 0.0:
                components["reclaim_reject_quality_bonus"] = reclaim_reject_bonus

        if sweep:
            interaction_center = max(0.0, 1.0 - (abs(penetration_ratio - 0.85) / 1.25))
            overshoot_penalty = self._clamp01((penetration_ratio - 2.25) / 1.00)
            zone_bonus = caps["zone_interaction"] * interaction_center * (1.0 - (overshoot_penalty * 0.65))
            if zone_bonus > 0.0:
                components["zone_interaction_bonus"] = zone_bonus

        total_bonus = min(sum(components.values()), caps["max_total"])
        quality_reasons = [name for name, value in components.items() if value > 0.0]
        rounded_breakdown = {name: round(float(value), 4) for name, value in components.items() if value > 0.0}
        return {
            "quality_bonus": round(float(total_bonus), 4),
            "quality_reasons": quality_reasons,
            "quality_breakdown": rounded_breakdown,
        }

    def _trigger_score(self, side: Side, ltf: pd.DataFrame, level_price: float, zone_width: float) -> dict[str, Any]:
        if ltf is None or len(ltf) < 5:
            return {"score": 0.0, "base_score": 0.0, "quality_bonus": 0.0, "reasons": [], "quality_reasons": [], "quality_breakdown": {}}
        last = ltf.iloc[-1]
        prior = ltf.iloc[:-1]
        prev3 = prior.tail(3)
        body = abs(float(last["close"]) - float(last["open"]))
        avg_body = abs(prior["close"] - prior["open"]).tail(20).mean() if len(prior) >= 2 else body
        avg_vol_raw = prior["volume"].tail(20).mean() if "volume" in prior.columns and len(prior) >= 2 else float(last.get("volume", 0.0))
        last_vol = float(last.get("volume", 0.0))
        if avg_vol_raw == avg_vol_raw and float(avg_vol_raw) > 0:
            avg_vol_valid = float(avg_vol_raw)
            volume_ratio = last_vol / avg_vol_valid
            vol_spike = last_vol > avg_vol_valid * 1.5
        else:
            volume_ratio = 1.0
            vol_spike = False
        strong_body = body > max(0.01, float(avg_body) * 1.2)
        reasons: list[str] = []
        base_score = 0.0
        trigger_sweep_window = ltf.tail(4)
        pattern_summary = self._configured_trigger_candle_summary(side, ltf)
        candle_score = float(pattern_summary.get("score", 0.0) or 0.0)
        candle_net_score = float(pattern_summary.get("net_score", 0.0) or 0.0)
        anchor_bars = int(pattern_summary.get("anchor_bars", 0) or 0)
        anchor_pattern = pattern_summary.get("anchor_pattern")
        anchor_label = "bullish_pattern" if side == Side.LONG else "bearish_pattern"
        if side == Side.LONG:
            choch = float(last["close"]) > float(prev3["high"].max()) if not prev3.empty else False
            reclaim = float(last["close"]) >= float(level_price)
            sweep = float(trigger_sweep_window["low"].min()) <= float(level_price) + zone_width if "low" in trigger_sweep_window.columns else False
            bullish_body = float(last["close"]) > float(last["open"]) and strong_body
            reaction_confirmed = bool(reclaim and sweep)
            candle_tier = str(pattern_summary.get("confirm_tier", "none") or "none")
            weak_candle = candle_tier == "weak_1c"
            candle_confirmed = (not weak_candle and bool(pattern_summary.get("confirmed"))) or (weak_candle and bool(choch or sweep or bullish_body or reaction_confirmed))
            pattern_or_body = candle_confirmed or bullish_body
            if choch:
                base_score += 1.0
                reasons.append("choch_up")
            if vol_spike:
                base_score += 1.0
                reasons.append("volume_spike")
            if candle_confirmed:
                candle_contribution = float(pattern_summary.get("confirm_contribution", 0.0) or 0.0)
                base_score += candle_contribution
                reasons.append(f"{anchor_label}_{anchor_bars}c" if anchor_bars > 0 else anchor_label)
            elif bullish_body:
                reasons.append("bullish_body_confirm")
            if reaction_confirmed:
                base_score += 1.0
                reasons.append("sweep_reclaim")
        else:
            choch = float(last["close"]) < float(prev3["low"].min()) if not prev3.empty else False
            reject = float(last["close"]) <= float(level_price)
            sweep = float(trigger_sweep_window["high"].max()) >= float(level_price) - zone_width if "high" in trigger_sweep_window.columns else False
            bearish_body = float(last["close"]) < float(last["open"]) and strong_body
            reaction_confirmed = bool(reject and sweep)
            candle_tier = str(pattern_summary.get("confirm_tier", "none") or "none")
            weak_candle = candle_tier == "weak_1c"
            candle_confirmed = (not weak_candle and bool(pattern_summary.get("confirmed"))) or (weak_candle and bool(choch or sweep or bearish_body or reaction_confirmed))
            pattern_or_body = candle_confirmed or bearish_body
            if choch:
                base_score += 1.0
                reasons.append("choch_down")
            if vol_spike:
                base_score += 1.0
                reasons.append("volume_spike")
            if candle_confirmed:
                candle_contribution = float(pattern_summary.get("confirm_contribution", 0.0) or 0.0)
                base_score += candle_contribution
                reasons.append(f"{anchor_label}_{anchor_bars}c" if anchor_bars > 0 else anchor_label)
            elif bearish_body:
                reasons.append("bearish_body_confirm")
            if reaction_confirmed:
                base_score += 1.0
                reasons.append("sweep_reject")
        quality = self._trigger_quality_bonus(
            side,
            ltf,
            level_price=float(level_price),
            zone_width=float(zone_width),
            choch=bool(choch),
            volume_ratio=float(volume_ratio),
            pattern_or_body=bool(pattern_or_body),
            reaction_confirmed=bool(reaction_confirmed),
            sweep=bool(sweep),
        )
        score = float(base_score) + float(quality.get("quality_bonus", 0.0) or 0.0)
        return {
            "score": round(float(score), 4),
            "base_score": round(float(base_score), 4),
            "quality_bonus": round(float(quality.get("quality_bonus", 0.0) or 0.0), 4),
            "reasons": reasons,
            "quality_reasons": list(quality.get("quality_reasons", [])),
            "quality_breakdown": dict(quality.get("quality_breakdown", {})),
            "trigger_candle_matches": sorted(pattern_summary.get("matches", set())),
            "trigger_candle_anchor_pattern": anchor_pattern,
            "trigger_candle_anchor_bars": int(anchor_bars),
            "trigger_candle_score": round(float(candle_score), 4),
            "trigger_candle_net_score": round(float(candle_net_score), 4),
            "trigger_candle_opposite_score": round(float(pattern_summary.get("opposite_score", 0.0) or 0.0), 4),
            "trigger_candle_regime_hint": str(pattern_summary.get("regime_hint", "neutral") or "neutral"),
        }

    def _sorted_target_levels(self, side: Side, close: float, htf: HTFContext) -> list[dict[str, Any]]:
        raw_candidates: list[dict[str, Any]] = []

        def _append(kind: str, price: float | None, *, touches: int = 1) -> None:
            if price is None:
                return
            try:
                level_price = float(price)
            except Exception:
                return
            if level_price <= 0:
                return
            raw_candidates.append({
                "kind": kind,
                "price": level_price,
                "touches": int(touches),
                "level_score": float(self._peer_level_source_priority(kind)),
                "source_priority": float(self._peer_level_source_priority(kind)),
            })

        def _append_level_obj(level_obj: Any | None, default_kind: str) -> None:
            if level_obj is None:
                return
            kind = str(getattr(level_obj, "source", None) or default_kind)
            _append(kind, getattr(level_obj, "price", None), touches=int(getattr(level_obj, "touches", 1) or 1))

        for level in getattr(htf, "resistances", []) or []:
            _append_level_obj(level, "resistance")
        for level in getattr(htf, "supports", []) or []:
            _append_level_obj(level, "support")
        _append_level_obj(getattr(htf, "broken_support", None), "broken_htf_support")
        _append_level_obj(getattr(htf, "broken_resistance", None), "broken_htf_resistance")
        for gap in (getattr(htf, "bearish_fvgs", []) or []):
            _append("bearish_htf_fvg", getattr(gap, "midpoint", None))
        for gap in (getattr(htf, "bullish_fvgs", []) or []):
            _append("bullish_htf_fvg", getattr(gap, "midpoint", None))

        if side == Side.LONG:
            candidates = [item for item in raw_candidates if float(item.get("price", 0.0) or 0.0) > float(close)]
        else:
            candidates = [item for item in raw_candidates if float(item.get("price", 0.0) or 0.0) < float(close)]

        collapsed = self._collapse_peer_levels(candidates, close, htf)
        collapsed = [item for item in collapsed if float(item.get("price", 0.0) or 0.0) > 0]
        collapsed.sort(key=lambda item: float(item.get("price", 0.0) or 0.0), reverse=(side != Side.LONG))
        return collapsed


    def _qualifying_target_levels(self, side: Side, close: float, stop: float, htf: HTFContext, atr: float, rr_required: float) -> list[dict[str, Any]]:
        qualifying: list[dict[str, Any]] = []
        for candidate in self._sorted_target_levels(side, close, htf):
            price = float(candidate.get("price", 0.0) or 0.0)
            if price <= 0:
                continue
            if side == Side.LONG:
                risk = max(0.01, close - stop)
                reward = price - close
            else:
                risk = max(0.01, stop - close)
                reward = close - price
            if reward <= 0:
                continue
            rr = reward / risk
            if rr < rr_required:
                continue
            zone_width = self._zone_width_for_level(side, close, atr, price, htf, candidate)
            lower, upper = self._ladder_bounds(price, zone_width)
            qualifying.append({**candidate, "price": float(price), "rr": float(rr), "zone_width": float(zone_width), "lower": float(lower), "upper": float(upper)})
        return qualifying

    @staticmethod
    def _ladder_management_metadata(side: Side, level: dict[str, Any], qualifying_target_levels: list[dict[str, Any]], selected_index: int) -> dict[str, Any]:
        rungs = [{"price": round(float(item.get("price", 0.0) or 0.0), 6), "kind": str(item.get("kind") or "target"), "zone_width": round(float(item.get("zone_width", 0.0) or 0.0), 6), "lower": round(float(item.get("lower", 0.0) or 0.0), 6), "upper": round(float(item.get("upper", 0.0) or 0.0), 6), "rr": round(float(item.get("rr", 0.0) or 0.0), 4)} for item in qualifying_target_levels]
        defense_price = float(level.get("price", 0.0) or 0.0)
        defense_zone_width = float(level.get("zone_width", 0.0) or 0.0)
        return {"ladder_management_enabled": True, "ladder_direction": "long" if side == Side.LONG else "short", "ladder_active_index": int(selected_index), "ladder_rungs": rungs, "ladder_defense_price": round(defense_price, 6), "ladder_defense_zone_width": round(defense_zone_width, 6), "ladder_defense_kind": str(level.get("kind") or "entry_level"), "ladder_entry_level_price": round(defense_price, 6), "ladder_entry_level_zone_width": round(defense_zone_width, 6), "ladder_entry_level_kind": str(level.get("kind") or "entry_level"), "ladder_final_rung_cleared": False, "adaptive_ladder_suppress_target_exit": False}

    def _peer_target_clearance(self, side: Side, close: float, htf: HTFContext, atr: float) -> dict[str, Any] | None:
        targets = self._sorted_target_levels(side, close, htf)
        if not targets:
            return None
        nearest = targets[0]
        level_price = float(nearest.get("price", 0.0) or 0.0)
        if level_price <= 0 or close <= 0:
            return None
        distance = (level_price - close) if side == Side.LONG else (close - level_price)
        if distance <= 0:
            return None
        clearance_atr = distance / max(float(atr), 1e-9)
        return {
            "kind": str(nearest.get("kind") or "target"),
            "price": level_price,
            "distance": distance,
            "clearance_pct": distance / close,
            "clearance_atr": clearance_atr,
        }

    def _build_equity_signal(self, c: Candidate, frame: pd.DataFrame, ltf: pd.DataFrame, htf: HTFContext, side: Side, level: dict[str, Any], peer_ctx: dict[str, Any], macro_ctx: dict[str, Any], data=None) -> Signal | None:
        close = self._safe_float(frame.iloc[-1]["close"])
        failure_style = self._failure_style_name(side)
        # Cheap directional gates run BEFORE expensive trigger scoring so that
        # symbols mis-sided against the hourly/peer/macro tape short-circuit out
        # instead of burning candle-pattern + quality-bonus compute and then
        # logging a misleading ``trigger_score_below_min:0.0000`` reason.
        bias, bull_votes, bear_votes = self._hourly_bias(htf, close)
        require_hourly_alignment = bool(self.params.get("require_hourly_bias_alignment", True))
        if side == Side.LONG:
            if bias == "bearish":
                self._set_build_failure(c.symbol, failure_style, "hourly_bias_bearish")
                return None
            if require_hourly_alignment and bias != "bullish":
                self._set_build_failure(c.symbol, failure_style, f"hourly_bias_not_bullish:{bias}({bull_votes}v{bear_votes})")
                return None
        if side == Side.SHORT:
            if bias == "bullish":
                self._set_build_failure(c.symbol, failure_style, "hourly_bias_bullish")
                return None
            if require_hourly_alignment and bias != "bearish":
                self._set_build_failure(c.symbol, failure_style, f"hourly_bias_not_bearish:{bias}({bull_votes}v{bear_votes})")
                return None
        peer_score = int(peer_ctx.get("score", 0))
        min_peer_score = _discrete_score_threshold(self.params.get("min_peer_score", 2), 2, minimum=0)
        min_peer_agreement = int(self.params.get("min_peer_agreement", 2))
        if side == Side.LONG:
            if peer_score < min_peer_score or int(peer_ctx.get("bullish", 0)) < min_peer_agreement:
                self._set_build_failure(c.symbol, failure_style, "peer_confirmation_insufficient_long")
                return None
        else:
            if peer_score > -min_peer_score or int(peer_ctx.get("bearish", 0)) < min_peer_agreement:
                self._set_build_failure(c.symbol, failure_style, "peer_confirmation_insufficient_short")
                return None
        macro_confirmation_enabled = bool(self.params.get("enable_macro_confirmation", True))
        required_macro = int(self.params.get("require_macro_agreement_count", 1))
        require_macro_net_bias = bool(self.params.get("require_macro_net_bias", True))
        long_agree = int(macro_ctx.get("long_agree", 0))
        short_agree = int(macro_ctx.get("short_agree", 0))
        if macro_confirmation_enabled:
            if side == Side.LONG and long_agree < required_macro:
                self._set_build_failure(c.symbol, failure_style, "macro_confirmation_insufficient_long")
                return None
            if side == Side.SHORT and short_agree < required_macro:
                self._set_build_failure(c.symbol, failure_style, "macro_confirmation_insufficient_short")
                return None
            if require_macro_net_bias:
                if side == Side.LONG and long_agree <= short_agree:
                    self._set_build_failure(c.symbol, failure_style, f"macro_net_bias_insufficient_long:long={long_agree}<=short={short_agree}")
                    return None
                if side == Side.SHORT and short_agree <= long_agree:
                    self._set_build_failure(c.symbol, failure_style, f"macro_net_bias_insufficient_short:short={short_agree}<=long={long_agree}")
                    return None
        # Expensive trigger-score compute (candle patterns + quality bonus).
        # Only runs after the cheap directional gates above have accepted.
        trigger = self._trigger_score(side, ltf, float(level["price"]), float(level["zone_width"]))
        trigger_min_score = max(0.0, float(self.params.get("min_trigger_score", 2.5)))
        trigger_score = float(trigger.get("score", 0.0) or 0.0)
        if trigger_score < trigger_min_score:
            self._set_build_failure(c.symbol, failure_style, f"trigger_score_below_min:{trigger_score:.4f}<{trigger_min_score:.4f}")
            return None
        last5 = ltf.iloc[-1]
        atr = self._safe_float(last5.get("atr14"), self._optional_float(getattr(htf, "atr14", None)) or max(close * 0.0015, 0.01))
        target_clearance = self._peer_target_clearance(side, close, htf, atr)
        if self._shared_entry_enabled("use_sr_filter", True) and bool(self._support_resistance_setting("enabled", True)) and target_clearance is not None:
            min_clearance_pct = float(self._support_resistance_setting("entry_min_clearance_pct", 0.0038))
            min_clearance_atr = float(self._support_resistance_setting("entry_min_clearance_atr", 0.85))
            clearance_pct = float(target_clearance["clearance_pct"])
            clearance_atr = float(target_clearance["clearance_atr"])
            clearance_pct_blocked = clearance_pct <= min_clearance_pct
            clearance_atr_blocked = clearance_atr <= min_clearance_atr
            if clearance_pct_blocked and clearance_atr_blocked:
                self._set_build_failure(c.symbol, failure_style, "too_close_to_overhead_resistance" if side == Side.LONG else "too_close_to_nearby_support")
                return None
        swing_window = ltf.tail(5)
        buffer = max(atr * float(self.params.get("stop_buffer_atr_mult", 0.68)), float(level["zone_width"]) * 0.35)
        if side == Side.LONG:
            swing = float(swing_window["low"].min()) if not swing_window.empty else close
            stop = min(float(level["price"]) - buffer, swing - (buffer * 0.5))
        else:
            swing = float(swing_window["high"].max()) if not swing_window.empty else close
            stop = max(float(level["price"]) + buffer, swing + (buffer * 0.5))
        rr_required = float(self.params.get("min_rr", 1.75))
        qualifying_target_levels = self._qualifying_target_levels(side, close, stop, htf, atr, rr_required)
        if not qualifying_target_levels:
            self._set_build_failure(c.symbol, failure_style, f"no_qualifying_target_rr:{rr_required:.2f}")
            return None
        target_selection_index = 0
        target = float(qualifying_target_levels[target_selection_index]["price"])
        strong_vote_edge = (bull_votes - bear_votes) if side == Side.LONG else (bear_votes - bull_votes)
        directional_peer_score = peer_score if side == Side.LONG else -peer_score
        strong_setup_trigger_min = max(0.0, float(self.params.get("strong_setup_min_trigger_score", 3.2)))
        strong_setup_peer_min = _discrete_score_threshold(self.params.get("strong_setup_min_peer_score", 2), 3, minimum=0)
        strong_setup = bool(self.params.get("strong_setup_runner_enabled", True)) and trigger_score >= strong_setup_trigger_min and float(level["level_score"]) >= float(self.params.get("strong_setup_min_level_score", 3.4)) and abs(peer_score) >= strong_setup_peer_min and strong_vote_edge >= int(self.params.get("strong_setup_min_hourly_vote_edge", 1))
        target_offset = max(0, int(self.params.get("strong_setup_target_level_offset", 1)))
        if strong_setup and len(qualifying_target_levels) > target_offset:
            target_selection_index = int(target_offset)
            target = float(qualifying_target_levels[target_selection_index]["price"])
        fvg_adjustments = self._fvg_entry_adjustment_components(side, c.symbol, frame, data)
        runner_allowed = bool(strong_setup or float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0) >= 0.40)
        management = self._adaptive_management_components(side, close, stop, target, style="peer", runner_allowed=runner_allowed, continuation_bias=float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0), strong_setup=strong_setup)
        macro_agreement_count = long_agree if side == Side.LONG else short_agree
        source_priority = float(level.get("source_priority", self._peer_level_source_priority(str(level.get("kind") or ""))) or 0.0)
        level_selection_score = float(level.get("selection_score", 0.0) or 0.0)
        level_selection_trigger_score = float(level.get("selection_trigger_score", trigger_score) or trigger_score)
        extra_clearance_atr = 0.0 if target_clearance is None else max(0.0, float(target_clearance.get("clearance_atr", 0.0) or 0.0) - float(self._support_resistance_setting("entry_min_clearance_atr", 0.85)))
        hourly_vote_bonus = max(0.0, float(strong_vote_edge)) * 0.30
        macro_bonus = (float(macro_agreement_count) * 0.15) if macro_confirmation_enabled else 0.0
        clearance_bonus = min(extra_clearance_atr, 2.5) * 0.20
        source_priority_bonus = source_priority * 0.35
        strong_setup_bonus = 0.60 if strong_setup else 0.0
        activity_weight = max(0.0, float(self.params.get("activity_score_weight", 0.11)))
        # peer_score is signed (positive=bullish consensus, negative=bearish).
        # For a LONG signal the upstream gate at line ~943 requires
        # peer_score >= min_peer_score (positive); for a SHORT signal it
        # requires peer_score <= -min_peer_score (negative). So by the time
        # we reach this ranking code, abs(peer_score) is equivalent to
        # directional_peer_score for the chosen side — safely monotonic with
        # "how strongly peers agree with this direction".
        final_priority_score = (
            float(level["level_score"])
            + float(trigger["score"])
            + (abs(peer_score) * 0.25)
            + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
            + source_priority_bonus
            + hourly_vote_bonus
            + macro_bonus
            + clearance_bonus
            + strong_setup_bonus
            + (float(c.activity_score) * activity_weight)
        )
        selection_quality_score = final_priority_score + (level_selection_score * 0.20)
        reason = f"peer_confirmed_key_level_{'long' if side == Side.LONG else 'short'}"
        metadata = self._build_signal_metadata(
            entry_price=close,
            fvg_adjustments=fvg_adjustments,
            management=management,
            final_priority_score=final_priority_score,
            leading={
                "level_kind": str(level["kind"]),
                "level_price": float(level["price"]),
                "level_score": float(level["level_score"]),
                "blended_level_score": float(level.get("blended_level_score", level["level_score"]) or level["level_score"]),
                "raw_htf_score": float(level.get("raw_htf_score", level["level_score"]) or level["level_score"]),
                "local_confluence_score": float(level.get("local_confluence_score", 0.0) or 0.0),
                "local_quality_score": float(level.get("local_quality_score", level["level_score"]) or level["level_score"]),
                "level_score_raw_htf_weight": float(level.get("level_score_raw_htf_weight", self._level_score_raw_htf_weight()) or self._level_score_raw_htf_weight()),
                "source_priority": source_priority,
                "selection_score": level_selection_score,
                "selection_trigger_score": round(float(level_selection_trigger_score), 4),
                "selection_distance_atr": float(level.get("selection_distance_atr", 0.0) or 0.0),
                "trigger_score": round(float(trigger_score), 4),
                "trigger_base_score": round(float(trigger.get("base_score", trigger_score) or trigger_score), 4),
                "trigger_quality_bonus": round(float(trigger.get("quality_bonus", 0.0) or 0.0), 4),
                "trigger_quality_reasons": list(trigger.get("quality_reasons", [])),
                "trigger_quality_breakdown": dict(trigger.get("quality_breakdown", {})),
                "trigger_candle_matches": list(trigger.get("trigger_candle_matches", [])),
                "trigger_candle_anchor_pattern": trigger.get("trigger_candle_anchor_pattern"),
                "trigger_candle_anchor_bars": int(trigger.get("trigger_candle_anchor_bars", 0) or 0),
                "trigger_candle_score": float(trigger.get("trigger_candle_score", 0.0) or 0.0),
                "trigger_candle_net_score": float(trigger.get("trigger_candle_net_score", 0.0) or 0.0),
                "trigger_candle_opposite_score": float(trigger.get("trigger_candle_opposite_score", 0.0) or 0.0),
                "trigger_candle_regime_hint": str(trigger.get("trigger_candle_regime_hint", "neutral") or "neutral"),
                "trigger_score_required": float(trigger_min_score),
                "trigger_reasons": list(trigger["reasons"]),
                "min_peer_score_required": min_peer_score,
                "strong_setup_trigger_score_required": float(strong_setup_trigger_min),
                "strong_setup_peer_score_required": strong_setup_peer_min,
                "hourly_bias": bias,
                "hourly_bull_votes": bull_votes,
                "hourly_bear_votes": bear_votes,
                "hourly_vote_edge": int(strong_vote_edge),
                "peer_score": peer_score,
                "directional_peer_score": directional_peer_score,
                "peer_bullish": int(peer_ctx.get("bullish", 0)),
                "peer_bearish": int(peer_ctx.get("bearish", 0)),
                "peer_details": dict(peer_ctx.get("details", {})),
                "peer_universe": list(peer_ctx.get("universe", [])),
                "macro_long_agree": int(macro_ctx.get("long_agree", 0)),
                "macro_short_agree": int(macro_ctx.get("short_agree", 0)),
                "macro_agreement_count": int(macro_agreement_count),
                "macro_details": dict(macro_ctx.get("details", {})),
                "nearest_target_level_kind": None if target_clearance is None else str(target_clearance.get("kind") or "target"),
                "nearest_target_level_price": None if target_clearance is None else float(target_clearance.get("price", 0.0) or 0.0),
                "nearest_target_clearance_pct": None if target_clearance is None else float(target_clearance.get("clearance_pct", 0.0) or 0.0),
                "nearest_target_clearance_atr": None if target_clearance is None else float(target_clearance.get("clearance_atr", 0.0) or 0.0),
                "activity_score": float(c.activity_score),
                "activity_score_weight": float(activity_weight),
                "setup_quality_score": round(float(level["level_score"]) + float(trigger["score"]) + (abs(peer_score) * 0.25), 4),
                "execution_quality_score": round(float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0) + source_priority_bonus + hourly_vote_bonus + macro_bonus + clearance_bonus + strong_setup_bonus, 4),
                "macro_score": round(float(macro_bonus), 4),
                "selection_quality_score": float(selection_quality_score),
                "regime_score": float(level["level_score"]),
                "selection_level_score": float(level.get("selection_level_score", level.get("level_score", 0.0)) or level.get("level_score", 0.0)),
                "selection_raw_htf_score": float(level.get("selection_raw_htf_score", level.get("raw_htf_score", level.get("level_score", 0.0))) or level.get("raw_htf_score", level.get("level_score", 0.0))),
                "selection_local_quality_score": float(level.get("selection_local_quality_score", level.get("local_quality_score", level.get("level_score", 0.0))) or level.get("local_quality_score", level.get("level_score", 0.0))),
                "directional_vote_edge": int(strong_vote_edge),
                "execution_headroom_score": 0.0 if target_clearance is None else float(target_clearance.get("clearance_atr", 0.0) or 0.0),
                "source_quality_score": float(source_priority),
                "runner_quality_score": 1 if strong_setup else 0,
                "runner_target_applied": bool(strong_setup and len(qualifying_target_levels) > target_offset),
                "qualifying_target_count": int(len(qualifying_target_levels)),
            },
            extras={
                **self._ladder_management_metadata(side, level, qualifying_target_levels, target_selection_index),
                **self._htf_lists(htf),
            },
        )
        return Signal(symbol=c.symbol, strategy=self._active_strategy_name(), side=side, reason=reason, stop_price=float(stop), target_price=float(target), metadata=metadata)

    def prefetch_entry_market_data(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], data=None) -> None:
        if data is None or not hasattr(data, "prefetch_htf_contexts"):
            return
        if not candidates:
            return
        universe = [
            symbol
            for symbol in self._confirmation_universe()
            if symbol in bars and bars.get(symbol) is not None and not bars.get(symbol).empty
        ]
        if not universe:
            return
        data.prefetch_htf_contexts(
            universe,
            timeframe_minutes=int(self.params.get("htf_timeframe_minutes", 60)),
            lookback_days=int(self.params.get("htf_lookback_days", 60)),
            pivot_span=int(self.params.get("htf_pivot_span", 2)),
            max_levels_per_side=int(self.params.get("htf_max_levels_per_side", 6)),
            atr_tolerance_mult=float(self.params.get("htf_atr_tolerance_mult", 0.35)),
            pct_tolerance=float(self.params.get("htf_pct_tolerance", 0.0030)),
            stop_buffer_atr_mult=float(self.params.get("htf_stop_buffer_atr_mult", 0.25)),
            ema_fast_span=int(self.params.get("htf_ema_fast_span", 50)),
            ema_slow_span=int(self.params.get("htf_ema_slow_span", 200)),
            refresh_seconds=int(self.params.get("htf_refresh_seconds", 120)),
            use_prior_day_high_low=bool(self._support_resistance_setting("use_prior_day_high_low", True)),
            use_prior_week_high_low=bool(self._support_resistance_setting("use_prior_week_high_low", True)),
        )

    def position_exit_signal(self, position: Position, bars: dict[str, pd.DataFrame], data=None) -> tuple[bool, str]:
        symbol = str(position.metadata.get("underlying") or position.symbol)
        frame = bars.get(symbol)
        if frame is None or frame.empty:
            return False, "hold"
        last = frame.iloc[-1]
        close = self._safe_float(last["close"])
        ema9 = self._safe_float(last["ema9"], close) if "ema9" in frame.columns else close
        ema20 = self._safe_float(last["ema20"], close) if "ema20" in frame.columns else close
        vwap = self._safe_float(last["vwap"], close) if "vwap" in frame.columns else close
        should_exit, reason = self._ladder_exit_signal(position, frame, close, ema9, ema20, vwap, data=data)
        if should_exit:
            return should_exit, reason
        direction = self._direction_token(position)
        close_pos = self._bar_close_position(frame)
        return self._technical_exit_signal(direction, frame, close, ema9, ema20, vwap, close_pos, position)

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        min_level_score = float(self.params.get("min_level_score", 2.9))
        allow_short = bool(self.config.risk.allow_short)
        tf = int(self.params.get("htf_timeframe_minutes", 60))
        lookback_days = int(self.params.get("htf_lookback_days", 60))
        pivot_span = int(self.params.get("htf_pivot_span", 2))
        max_lvls = int(self.params.get("htf_max_levels_per_side", 6))
        atr_tol = float(self.params.get("htf_atr_tolerance_mult", 0.35))
        pct_tol = float(self.params.get("htf_pct_tolerance", 0.0030))
        stop_atr = float(self.params.get("htf_stop_buffer_atr_mult", 0.25))
        ema_fast_span = int(self.params.get("htf_ema_fast_span", 50))
        ema_slow_span = int(self.params.get("htf_ema_slow_span", 200))
        refresh_seconds = int(self.params.get("htf_refresh_seconds", 120))
        trigger_tf = int(self.params.get("trigger_timeframe_minutes", 5))
        min_bars = int(self.params.get("min_bars", 80))
        min_trigger_bars = int(self.params.get("min_trigger_bars", 18))
        macro_ctx = self._macro_signal(bars, data=data)
        tradable_symbols = set(self._tradable_symbols())
        for c in candidates:
            if tradable_symbols and c.symbol not in tradable_symbols:
                self._record_entry_decision(c.symbol, "skipped", ["symbol_not_tradable"])
                continue
            reasons: list[str] = []
            frame = bars.get(c.symbol)
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue
            if frame is None or len(frame) < min_bars:
                self._record_entry_decision(c.symbol, "skipped", [self._insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), min_bars)])
                continue
            ltf = self._resampled_frame(frame, trigger_tf, symbol=c.symbol, data=data)
            if ltf is None or len(ltf) < min_trigger_bars:
                self._record_entry_decision(c.symbol, "skipped", [self._insufficient_bars_reason("insufficient_trigger_bars", 0 if ltf is None else len(ltf), min_trigger_bars)])
                continue
            close = self._safe_float(frame.iloc[-1]["close"])
            htf = self._htf_context(
                c.symbol,
                data,
                timeframe_minutes=tf,
                lookback_days=lookback_days,
                pivot_span=pivot_span,
                max_levels_per_side=max_lvls,
                atr_tolerance_mult=atr_tol,
                pct_tolerance=pct_tol,
                stop_buffer_atr_mult=stop_atr,
                ema_fast_span=ema_fast_span,
                ema_slow_span=ema_slow_span,
                refresh_seconds=refresh_seconds,
                current_price=close,
                use_prior_day_high_low=bool(self._support_resistance_setting("use_prior_day_high_low", True)),
                use_prior_week_high_low=bool(self._support_resistance_setting("use_prior_week_high_low", True)),
            )
            symbol_peer_ctx = self._peer_signal(c.symbol, bars, data)
            short_side_enabled = bool(allow_short)
            long_level = self._select_level(Side.LONG, close, ltf, htf)
            short_level = self._select_level(Side.SHORT, close, ltf, htf) if short_side_enabled else None
            evaluated_sides = ["LONG"] + (["SHORT"] if short_side_enabled else [])
            signals: list[Signal] = []
            if long_level is not None:
                long_level_score = float(long_level["level_score"])
                if long_level_score >= min_level_score:
                    sig = self._build_equity_signal(c, frame, ltf, htf, Side.LONG, long_level, symbol_peer_ctx, macro_ctx, data=data)
                    if sig is not None:
                        signals.append(sig)
                else:
                    reasons.append(f"long_level_score_below_min:{long_level_score:.2f}<{min_level_score:.2f}")
            if short_level is not None:
                short_level_score = float(short_level["level_score"])
                if short_level_score >= min_level_score:
                    sig = self._build_equity_signal(c, frame, ltf, htf, Side.SHORT, short_level, symbol_peer_ctx, macro_ctx, data=data)
                    if sig is not None:
                        signals.append(sig)
                else:
                    reasons.append(f"short_level_score_below_min:{short_level_score:.2f}<{min_level_score:.2f}")
            if not signals:
                if long_level is None and short_level is None:
                    reasons.append("price_not_in_hourly_zone")
                else:
                    long_failure = self._consume_build_failure(c.symbol, self._failure_style_name(Side.LONG))
                    short_failure = self._consume_build_failure(c.symbol, self._failure_style_name(Side.SHORT))
                    for side, failure in ((Side.LONG, long_failure), (Side.SHORT, short_failure)):
                        for token in self._side_prefixed_reasons(side, [failure] if failure else []):
                            if token and token not in reasons:
                                reasons.append(token)
                    if not reasons:
                        reasons.append("setup_not_confirmed")
                decision_details = {"peer_universe": list(symbol_peer_ctx.get("universe", [])), "peer_details": dict(symbol_peer_ctx.get("details", {})), "evaluated_sides": evaluated_sides}
                self._record_entry_decision(c.symbol, "skipped", reasons, details=decision_details)
                continue
            best = max(
                signals,
                key=lambda sig: (
                    float(sig.metadata.get("final_priority_score", 0.0) or 0.0),
                    float(sig.metadata.get("selection_quality_score", sig.metadata.get("final_priority_score", 0.0)) or 0.0),
                ),
            )
            out.append(best)
            meta = best.metadata if isinstance(best.metadata, dict) else {}
            signal_details = {"peer_universe": meta.get("peer_universe"), "entry_family": meta.get("entry_family"), "evaluated_sides": evaluated_sides}
            self._record_entry_decision(c.symbol, "signal", [best.reason], details=signal_details)
        return out
