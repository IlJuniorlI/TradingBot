# SPDX-License-Identifier: MIT
from ..shared import (
    ASSET_TYPE_OPTION_SINGLE,
    Any,
    Candidate,
    Position,
    Side,
    Signal,
    _clamp_long_premium_levels,
    insufficient_bars_reason,
    _no_style_trigger_reason,
    _positive_quote_value,
    _safe_float,
    _same_day_mask,
    asdict,
    build_single_option_order,
    build_single_option_position_label,
    choose_by_delta,
    now_et,
    parse_hhmm,
    pd,
    single_option_dollars,
    single_option_limit_price,
)
from ..zero_dte_etf_options.strategy import ZeroDteEtfOptionsStrategy

class ZeroDteEtfLongOptionsStrategy(ZeroDteEtfOptionsStrategy):
    strategy_name = 'zero_dte_etf_long_options'

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        return max(0, int(self.params.get("min_bars", 90)))
    def _build_single_option_signal(self, underlying: str, bullish: bool, client, data, last_underlying: float, style: str, confirm_index: str | None, regime: dict[str, Any]) -> Signal | None:
        put_call = "CALL" if bullish else "PUT"
        contracts = self._fetch_filtered_contracts(client, underlying, put_call)
        if not contracts:
            self._set_build_failure(underlying, style, "option_chain_empty")
            return None
        base_delta = float(self.optcfg.target_single_delta)
        adjusted_delta = self._time_adjusted_delta(base_delta)
        contract = choose_by_delta(contracts, adjusted_delta)
        if contract is None:
            self._set_build_failure(underlying, style, "no_contract_near_target_delta")
            return None
        stable = self._stabilize_single_option_quote(data, contract)
        if stable is None:
            self._set_build_failure(underlying, style, "quote_not_stable")
            return None
        contract = stable
        market = self._validate_single_option_market(contract)
        if market is None:
            self._set_build_failure(underlying, style, "option_spread_too_wide")
            return None
        nat_bid, nat_ask, quoted_mid = market
        entry_limit = single_option_limit_price(contract, mode=self.optcfg.option_limit_mode, opening=True)
        _, mark_mid = single_option_dollars(contract)
        if entry_limit <= 0:
            self._set_build_failure(underlying, style, "invalid_limit_price")
            return None
        entry_value = entry_limit * 100.0
        single_stop_frac = max(0.01, min(0.99, float(self.optcfg.single_stop_frac)))
        single_target_mult = max(1.01, float(self.optcfg.single_target_mult))
        time_decay_scale = self._compute_time_decay_scale()
        if time_decay_scale < 1.0:
            single_target_mult = max(1.01, 1.0 + (single_target_mult - 1.0) * time_decay_scale)
            widen = float(getattr(self.optcfg, "debit_stop_time_decay_widen_factor", 0.30))
            single_stop_frac = max(0.01, min(0.99, single_stop_frac * (1.0 + (1.0 - time_decay_scale) * widen)))
        stop = entry_value * single_stop_frac
        target = entry_value * single_target_mult
        stop, target = _clamp_long_premium_levels(entry_value, stop, target)
        position_key = build_single_option_position_label(underlying, style, contract)
        breakeven_underlying = float(contract.strike) + entry_limit if bullish else float(contract.strike) - entry_limit
        metadata = {
            "asset_type": ASSET_TYPE_OPTION_SINGLE,
            "position_key": position_key,
            "underlying": underlying,
            "confirm_index": confirm_index,
            "style": style,
            "regime": regime.get("regime"),
            "regime_scores": regime.get("scores"),
            "regime_metrics": regime.get("metrics"),
            "direction": "bullish" if bullish else "bearish",
            "option_type": put_call,
            "entry_price": entry_value,
            "mark_price_hint": (quoted_mid * 100.0) if quoted_mid else mark_mid,
            "max_loss_per_contract": entry_value,
            "max_profit_per_contract": None,
            "breakeven_underlying": breakeven_underlying,
            "limit_price": entry_limit,
            "natural_bid": nat_bid * 100.0,
            "natural_ask": nat_ask * 100.0,
            "underlying_entry": last_underlying,
            "valuation_legs": [contract.symbol],
            "option_symbol": contract.symbol,
            "option_strike": float(contract.strike),
            "option_leg": asdict(contract),
            "order_spec": build_single_option_order(contract, qty=1, limit_price=entry_limit),
        }
        return Signal(symbol=underlying, strategy=self.strategy_name, side=Side.LONG, reason=f"{style}_{'bull' if bullish else 'bear'}", stop_price=stop, target_price=target, reference_symbol=confirm_index, metadata=metadata)

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        if not self._options_enabled() or client is None or data is None:
            return []
        out: list[Signal] = []
        self._underlying_atr_cache.clear()
        self._underlying_ref_atr_cache.clear()
        now_dt = now_et()
        blackout_reason = self._option_entry_block_reason(now_dt)
        if blackout_reason:
            for c in candidates:
                self._record_entry_decision(c.symbol, "skipped", [blackout_reason])
            return out
        now_t = now_dt.time()
        if now_t > parse_hhmm(self.params.get("no_new_entries_after", "13:45")):
            for c in candidates:
                self._record_entry_decision(c.symbol, "skipped", ["after_entry_cutoff"])
            return out
        for c in candidates:
            reasons: list[str] = []
            if self._underlying_already_open(c.symbol, positions):
                self._record_entry_decision(c.symbol, "skipped", ["underlying_already_open"])
                continue
            frame = bars.get(c.symbol)
            # B2 fix: aligned with required_history_bars() default (90) so the
            # init-time history fetch and the runtime entry gate use the same
            # floor. Manifest ships 90; the param.get fallback also lands at
            # 90 if manifest loading somehow misses (silent-drift guard).
            min_bars = int(self.params.get("min_bars", 90))
            if frame is None or len(frame) < min_bars:
                self._record_entry_decision(c.symbol, "skipped", [insufficient_bars_reason("insufficient_underlying_bars", 0 if frame is None else len(frame), min_bars)])
                continue
            regime = self._regime_confirm(c, bars, data)
            if not regime.get("ok") or regime.get("no_trade"):
                reasons.append(str(regime.get("reason") or "regime_blocked"))
                reasons.extend([str(r) for r in regime.get("reasons", []) if str(r)])
                self._record_entry_decision(c.symbol, "skipped", reasons)
                continue
            confirm_index = regime.get("confirm_index")
            last = frame.iloc[-1]
            self._underlying_atr_cache[c.symbol] = _safe_float(last.get("atr14"), 0.0)
            if "atr14" in frame.columns:
                atr_series = frame["atr14"].dropna().tail(20)
                self._underlying_ref_atr_cache[c.symbol] = float(atr_series.median()) if len(atr_series) >= 5 else 0.0
            # B3 / B4 fix: opening-range bars and ORB-window endpoints are
            # both configurable. The opening window defines or_high/or_low
            # (the levels we trade the breakout against); the ORB window
            # defines WHEN we look for those breakouts. Defaults preserve
            # legacy behaviour (09:30-09:34 opening, 09:35-10:05 trading).
            orb_start_time = str(self.params.get("orb_start_time", "09:35"))
            orb_end_time = str(self.params.get("orb_end_time", "10:05"))
            opening_window_start = str(self.params.get("orb_opening_window_start", "09:30"))
            opening_window_end = str(self.params.get("orb_opening_window_end", "09:34"))
            opening = frame[_same_day_mask(frame, now_et().date())].between_time(opening_window_start, opening_window_end)
            regime_name = str(regime.get("regime") or "unknown")
            bullish = regime_name == "bullish_trend"
            bearish = regime_name == "bearish_trend"
            rangeish = regime_name == "range"
            attempted_style = False
            # C1 fix: use .get() with _safe_float defaults so a frame that
            # somehow ships without a column (rare edge — partial warmup,
            # data gap) returns the default instead of KeyError-ing.
            last_close = _safe_float(last.get("close"), 0.0)
            last_vwap = _safe_float(last.get("vwap"), last_close)
            last_ret5 = _safe_float(last.get("ret5"), 0.0)
            orb_enabled = self._long_option_style_enabled("orb_long_option")
            orb_window = self._time_in_range(now_t, orb_start_time, orb_end_time)
            trend_enabled = self._long_option_style_enabled("trend_long_option")
            # B2 fix: 13:30 default matches manifest.json (was 13:25 — a
            # 5-minute silent drift if the manifest ever didn't apply).
            trend_window = self._time_in_range(now_t, self.params.get("trend_start_time", "10:05"), self.params.get("trend_end_time", "13:30"))
            # C2 fix: require a minimum number of bars in the opening
            # window before deriving or_high / or_low. A single 09:34
            # stream bar would otherwise be treated as the "opening range"
            # and produce trivially-passable breakout checks. Default 3
            # bars keeps things flexible for shortened sessions or late-
            # start data while still requiring real structure.
            opening_min_bars = int(self.params.get("orb_opening_min_bars", 3))
            opening_ready = (not opening.empty) and len(opening) >= opening_min_bars
            or_high = _safe_float(opening["high"].max()) if opening_ready else None
            or_low = _safe_float(opening["low"].min()) if opening_ready else None
            buffer_pct = float(self.params.get("orb_breakout_buffer_pct", 0.0008))
            trend_min_ret5 = float(self.params.get("trend_min_ret5", 0.0006))

            if orb_enabled and orb_window and (bullish or bearish):
                if opening_ready:
                    # B1 fix: optional structural / SR vetoes on the ORB
                    # entry path. Default ON because long premium with
                    # bearish HTF structure or trapped between broken
                    # SR levels bleeds theta even on a clean breakout.
                    # Toggle off via orb_apply_structure_veto / orb_apply
                    # _sr_veto for users who want the legacy "fire on
                    # any breakout" behaviour.
                    orb_structure_veto = bool(self.params.get("orb_apply_structure_veto", True))
                    orb_sr_veto = bool(self.params.get("orb_apply_sr_veto", True))
                    ms_ctx = None
                    sr_ctx = None
                    if orb_structure_veto:
                        ms_ctx = self._structure_context(frame, "ltf")
                    if orb_sr_veto:
                        sr_ctx = self._sr_context(c.symbol, frame, data)
                    if bullish and last_close > _safe_float(or_high) * (1.0 + buffer_pct) and last_close > last_vwap:
                        veto_reason = None
                        if orb_structure_veto and ms_ctx is not None and self._blocks_bullish_structure_entry(ms_ctx):
                            veto_reason = f"orb_long_option_{self._bullish_structure_block_reason(ms_ctx)}"
                        elif orb_sr_veto and sr_ctx is not None and self._blocks_bullish_sr_entry(sr_ctx):
                            veto_reason = f"orb_long_option_{self._bullish_sr_block_reason(sr_ctx)}"
                        if veto_reason:
                            reasons.append(veto_reason)
                        else:
                            attempted_style = True
                            sig = self._build_single_option_signal(c.symbol, True, client, data, last_close, "orb_long_option", confirm_index, regime)
                            if sig:
                                out.append(self._attach_option_final_priority_score(sig, c, regime, bullish=True, rangeish=False))
                                self._record_entry_decision(c.symbol, "signal", [sig.reason])
                                continue
                            reasons.append(self._consume_build_failure(c.symbol, "orb_long_option") or "orb_long_option_unavailable")
                    if bearish and last_close < _safe_float(or_low) * (1.0 - buffer_pct) and last_close < last_vwap:
                        veto_reason = None
                        if orb_structure_veto and ms_ctx is not None and self._blocks_bearish_structure_entry(ms_ctx):
                            veto_reason = f"orb_long_option_{self._bearish_structure_block_reason(ms_ctx)}"
                        elif orb_sr_veto and sr_ctx is not None and self._blocks_bearish_sr_entry(sr_ctx):
                            veto_reason = f"orb_long_option_{self._bearish_sr_block_reason(sr_ctx)}"
                        if veto_reason:
                            reasons.append(veto_reason)
                        else:
                            attempted_style = True
                            sig = self._build_single_option_signal(c.symbol, False, client, data, last_close, "orb_long_option", confirm_index, regime)
                            if sig:
                                out.append(self._attach_option_final_priority_score(sig, c, regime, bullish=False, rangeish=False))
                                self._record_entry_decision(c.symbol, "signal", [sig.reason])
                                continue
                            reasons.append(self._consume_build_failure(c.symbol, "orb_long_option") or "orb_long_option_unavailable")

            if trend_enabled and trend_window and (bullish or bearish):
                momentum_ok = True
                if getattr(self.optcfg, "trend_momentum_filter_enabled", False):
                    atr_current = _safe_float(last.get("atr14"), 0.0)
                    atr_tail = frame.tail(20)["atr14"].dropna() if "atr14" in frame.columns else pd.Series(dtype=float)
                    atr_mean = float(atr_tail.mean()) if len(atr_tail) > 0 else 0.0
                    atr_expansion = atr_current / max(atr_mean, 1e-9) if atr_mean > 0 else 0.0
                    vol_current = _safe_float(last.get("volume"), 0.0)
                    vol_tail = frame.tail(10)["volume"].dropna() if "volume" in frame.columns else pd.Series(dtype=float)
                    vol_mean = float(vol_tail.mean()) if len(vol_tail) > 0 else 1.0
                    volume_ratio = vol_current / max(vol_mean, 1.0)
                    min_atr_exp = float(getattr(self.optcfg, "trend_min_atr_expansion", 0.85))
                    min_vol_ratio = float(getattr(self.optcfg, "trend_min_volume_ratio", 0.90))
                    if atr_expansion < min_atr_exp or volume_ratio < min_vol_ratio:
                        momentum_ok = False
                        reasons.append(f"trend_momentum_filter(atr_exp={atr_expansion:.3f}<{min_atr_exp},vol_ratio={volume_ratio:.3f}<{min_vol_ratio})")
                if momentum_ok and bullish and last_ret5 >= trend_min_ret5:
                    attempted_style = True
                    style_reasons = self._long_option_style_gate(c.symbol, True, frame, regime, data)
                    if not style_reasons:
                        sig = self._build_single_option_signal(c.symbol, True, client, data, last_close, "trend_long_option", confirm_index, regime)
                        if sig:
                            out.append(self._attach_option_final_priority_score(sig, c, regime, bullish=True, rangeish=False))
                            self._record_entry_decision(c.symbol, "signal", [sig.reason])
                            continue
                        reasons.append(self._consume_build_failure(c.symbol, "trend_long_option") or "trend_long_option_unavailable")
                    else:
                        reasons.extend(style_reasons)
                if momentum_ok and bearish and last_ret5 <= -trend_min_ret5:
                    attempted_style = True
                    style_reasons = self._long_option_style_gate(c.symbol, False, frame, regime, data)
                    if not style_reasons:
                        sig = self._build_single_option_signal(c.symbol, False, client, data, last_close, "trend_long_option", confirm_index, regime)
                        if sig:
                            out.append(self._attach_option_final_priority_score(sig, c, regime, bullish=False, rangeish=False))
                            self._record_entry_decision(c.symbol, "signal", [sig.reason])
                            continue
                        reasons.append(self._consume_build_failure(c.symbol, "trend_long_option") or "trend_long_option_unavailable")
                    else:
                        reasons.extend(style_reasons)

            final_reasons = reasons or ([
                _no_style_trigger_reason(
                    regime_name=regime_name,
                    bullish=bullish,
                    bearish=bearish,
                    rangeish=rangeish,
                    orb_enabled=orb_enabled,
                    orb_window=orb_window,
                    trend_enabled=trend_enabled,
                    trend_window=trend_window,
                    credit_enabled=False,
                    credit_window=False,
                    last_close=last_close,
                    last_vwap=last_vwap,
                    last_ret5=last_ret5,
                    trend_min_ret5=trend_min_ret5,
                    or_high=or_high,
                    or_low=or_low,
                    orb_buffer_pct=buffer_pct,
                )
            ] if not attempted_style else ["no_contract_selected"])
            self._record_entry_decision(c.symbol, "skipped", final_reasons)
        return out

    def position_mark_price(self, position: Position, data) -> float | None:
        if position.strategy != self.strategy_name:
            return super().position_mark_price(position, data)
        if position.metadata.get("asset_type") != ASSET_TYPE_OPTION_SINGLE:
            return None
        symbol = str(position.metadata.get("option_symbol") or "")
        q = data.get_quote(symbol) if data and symbol else None
        if not q:
            return None
        if data is not None and not data.quotes_are_fresh([symbol], self.optcfg.max_quote_age_seconds):
            return None
        mark = _positive_quote_value(q, "mid", "mark", "last")
        if mark is None:
            return None
        return max(0.0, mark * 100.0)
