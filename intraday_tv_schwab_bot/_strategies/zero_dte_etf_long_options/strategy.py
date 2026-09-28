# SPDX-License-Identifier: MIT
from dataclasses import asdict
from typing import Any

import pandas as pd

from ...models import ASSET_TYPE_OPTION_SINGLE, Candidate, Position, Side, Signal, asset_type_of
from ...options_mode import (
    build_single_option_order,
    build_single_option_position_label,
    clamp_long_premium_levels,
    choose_by_delta,
    single_option_limit_price,
)
from ...numeric import first_float, safe_float
from ...reasons import reason_with_values
from ..zero_dte_etf_options.strategy import ZeroDteEtfOptionsStrategy, _EntryStyle

class ZeroDteEtfLongOptionsStrategy(ZeroDteEtfOptionsStrategy):
    """0DTE long calls / puts on the inherited regime engine.

    Both styles -- ORB and trend -- hand a premium proposal (family
    ``option_long``, the underlying's direction) to the shared entry stage
    before the chain is read, so shared_entry.use_structure_filter /
    use_sr_filter veto both the same way; the trend style's own extension
    and conviction blockers (``_long_option_style_gate``) ride along as the
    proposal's pending reasons. The ORB path's own switches
    (``orb_apply_structure_veto`` / ``orb_apply_sr_veto``) were retired on
    2026-09-24. The entry loop is the base's (``entry_signals``) on this
    style table; it reads each chain in the build, with no prefetch.
    """

    strategy_name = 'zero_dte_etf_long_options'
    time_params = ("no_new_entries_after", "orb_start_time", "orb_end_time", "orb_opening_window_start",
                   "orb_opening_window_end", "trend_start_time", "trend_end_time")
    _PREFETCH_OPTION_CHAINS = False

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        return max(0, int(self.params.get("min_bars", 90)))

    def _entry_styles(self) -> tuple[_EntryStyle, ...]:
        """A long call / put on the ORB, then on the trend, whose own
        extension and conviction blockers (``_long_option_style_gate``) ride
        on its premium proposal."""
        return (
            _EntryStyle("orb_long_option", "orb", self._build_single_option_signal),
            _EntryStyle("trend_long_option", "trend", self._build_single_option_signal, self._long_option_style_gate),
        )

    def _long_option_style_gate(self, bullish: bool, frame: pd.DataFrame, regime: dict[str, Any]) -> list[str]:
        """zero_dte_etf_long_options' own trend-entry blockers (conviction,
        score gap, extension, spike). They are the premium proposal's pending
        reasons: the structure and S/R vetoes this gate also ran until
        2026-09-24 are the shared entry stage's, recorded with them."""
        p = self.params
        reasons: list[str] = []
        if frame is None or frame.empty:
            return ["insufficient_underlying_bars"]
        last = frame.iloc[-1]
        last_close = safe_float(last["close"], 0.0)
        last_vwap = safe_float(last["vwap"], last_close)
        last_ema9 = safe_float(last["ema9"], last_close)
        last_ema20 = safe_float(last["ema20"], last_close)
        last_ret5 = safe_float(last["ret5"], 0.0)
        last_ret15 = safe_float(last["ret15"], 0.0)
        vwap_dist = (last_close - last_vwap) / max(last_close, 1.0)
        ema_gap = (last_ema9 - last_ema20) / max(last_close, 1.0)
        scores = regime.get("scores") or {}
        top_score = float(regime.get("scores", {}).get(regime.get("regime"), 0.0) or 0.0)
        ranked = sorted(((str(k), float(v)) for k, v in scores.items()), key=lambda kv: kv[1], reverse=True)
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0

        min_style_score = float(p.get("long_option_min_trend_score", max(float(p.get("min_trend_score", 4.9)), 4.25)))
        min_style_gap = float(p.get("long_option_min_score_gap", max(float(p.get("min_score_gap", 1.6)), 2.10)))
        max_vwap_extension = float(p.get("long_option_max_vwap_extension_pct", max(float(p.get("trend_vwap_distance_pct", 0.0016)) * 2.25, 0.0035)))
        max_ema_extension = float(p.get("long_option_max_ema_gap_pct", max(float(p.get("trend_ema_gap_pct", 0.00075)) * 2.5, 0.0020)))
        max_ret5 = float(p.get("long_option_max_ret5", max(float(p.get("trend_min_ret5", 0.0008)) * 5.0, 0.0025)))
        max_ret15 = float(p.get("long_option_max_ret15", max(float(p.get("trend_min_ret15", 0.0014)) * 5.0, 0.0055)))

        if top_score < min_style_score:
            reasons.append(reason_with_values("trend_long_option_low_conviction", current=top_score, required=min_style_score, op=">=", digits=2))
        if (top_score - second_score) < min_style_gap:
            reasons.append(reason_with_values("trend_long_option_score_gap_too_small", current=top_score - second_score, required=min_style_gap, op=">=", digits=2))

        if bullish:
            if vwap_dist > max_vwap_extension:
                reasons.append(reason_with_values("trend_long_option_too_extended_from_vwap", current=vwap_dist, required=max_vwap_extension, op="<=", digits=4))
            if ema_gap > max_ema_extension:
                reasons.append(reason_with_values("trend_long_option_ema_gap_too_large", current=ema_gap, required=max_ema_extension, op="<=", digits=4))
            if last_ret5 > max_ret5:
                reasons.append(reason_with_values("trend_long_option_short_term_spike", current=last_ret5, required=max_ret5, op="<=", digits=4))
            if last_ret15 > max_ret15:
                reasons.append(reason_with_values("trend_long_option_already_extended", current=last_ret15, required=max_ret15, op="<=", digits=4))
        else:
            if vwap_dist < -max_vwap_extension:
                reasons.append(reason_with_values("trend_long_option_too_extended_from_vwap", current=abs(vwap_dist), required=max_vwap_extension, op="<=", digits=4))
            if ema_gap < -max_ema_extension:
                reasons.append(reason_with_values("trend_long_option_ema_gap_too_large", current=abs(ema_gap), required=max_ema_extension, op="<=", digits=4))
            if last_ret5 < -max_ret5:
                reasons.append(reason_with_values("trend_long_option_short_term_spike", current=abs(last_ret5), required=max_ret5, op="<=", digits=4))
            if last_ret15 < -max_ret15:
                reasons.append(reason_with_values("trend_long_option_already_extended", current=abs(last_ret15), required=max_ret15, op="<=", digits=4))
        return reasons

    def _build_single_option_signal(self, candidate: Candidate, bullish: bool, client, data, frame: pd.DataFrame, last_underlying: float, style: str, confirm_index: str | None, regime: dict[str, Any], *, pending_reasons: tuple[str, ...] | list[str] = ()) -> Signal | None:
        underlying = candidate.symbol
        admitted = self._admit_premium_entry(candidate, bullish, frame, data, last_underlying, style, "option_long", regime,
                                             pending_reasons=pending_reasons)
        if admitted is None:
            return None
        if self._underlying_below_min_price(underlying, style, last_underlying):
            return None
        put_call = "CALL" if bullish else "PUT"
        contracts = self._fetch_filtered_contracts(client, underlying, put_call)
        if contracts is None:
            self._set_build_failure(underlying, style, "option_chain_unavailable")
            return None
        if not contracts:
            self._set_build_failure(underlying, style, "option_chain_empty")
            return None
        base_delta = float(self.optcfg.target_single_delta)
        adjusted_delta = self._time_adjusted_delta(base_delta)
        contract = choose_by_delta(contracts, adjusted_delta)
        if contract is None:
            self._set_build_failure(underlying, style, "no_contract_near_target_delta")
            return None
        stable, instability = self._stabilize_quotes(data, (contract,), validate=self._validate_single_option_market,
                                                     failure_detail=self._single_option_market_failure_detail,
                                                     source="strategies:option_quote_stability_single")
        if stable is None:
            self._set_build_failure(underlying, style, f"quote_not_stable({instability})")
            return None
        (contract,) = stable
        market = self._validate_single_option_market(contract)
        if market is None:
            self._set_build_failure(underlying, style, "option_spread_too_wide")
            return None
        nat_bid, nat_ask, quoted_mid = market
        entry_limit = single_option_limit_price(contract, mode=self.optcfg.option_limit_mode, opening=True)
        entry_value = entry_limit * 100.0
        single_stop_frac = max(0.01, min(0.99, float(self.optcfg.single_stop_frac)))
        single_target_mult = max(1.01, float(self.optcfg.single_target_mult))
        time_decay_scale = self._compute_time_decay_scale()
        if time_decay_scale < 1.0:
            single_target_mult = max(1.01, 1.0 + (single_target_mult - 1.0) * time_decay_scale)
            widen = self.optcfg.debit_stop_time_decay_widen_factor
            single_stop_frac = max(0.01, min(0.99, single_stop_frac * (1.0 + (1.0 - time_decay_scale) * widen)))
        stop = entry_value * single_stop_frac
        target = entry_value * single_target_mult
        stop, target = clamp_long_premium_levels(entry_value, stop, target)
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
            "mark_price_hint": quoted_mid * 100.0,
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
        # A long put is bought too: the order side is LONG, the proposal's
        # (the underlying's) direction bullish / bearish.
        return self.entry_policy.emit(
            admitted,
            reason=f"{style}_{'bull' if bullish else 'bear'}",
            strategy_score=self._option_strategy_score(candidate, regime, "bullish_trend" if bullish else "bearish_trend"),
            management={},
            target=target,
            metadata=metadata,
            order_side=Side.LONG,
            reference_symbol=confirm_index,
            premium_stop=stop,
        )

    def position_mark_price(self, position: Position, data) -> float | None:
        if position.strategy != self.strategy_name:
            return super().position_mark_price(position, data)
        if asset_type_of(position.metadata) != ASSET_TYPE_OPTION_SINGLE:
            return None
        symbol = str(position.metadata.get("option_symbol") or "")
        q = data.get_quote(symbol) if data and symbol else None
        if not q:
            return None
        if data is not None and not data.quotes_are_fresh([symbol], self.optcfg.max_quote_age_seconds):
            return None
        mark = first_float(q, "mid", "mark", "last", positive=True)
        if mark is None:
            return None
        return max(0.0, mark * 100.0)
