# SPDX-License-Identifier: MIT
from ..shared import Any, Candidate, LOG, Side
from ..screener_base import BaseStrategyScreener
from ..rvol import rvol_profile_for_symbol


class PeerConfirmedHTFPivotsScreener(BaseStrategyScreener):
    strategy_name = 'peer_confirmed_htf_pivots'

    def run(self) -> list[Candidate]:
        params = self.config.strategies[self.strategy_name].params
        configured_symbols = [
            str(sym).upper().strip()
            for sym in params.get("tradable", [])
            if str(sym).strip()
        ]
        if not configured_symbols:
            return []

        select_cols = [
            "name",
            "description",
            "exchange",
            "close",
            "volume",
            "change_from_open",
            "relative_volume_10d_calc",
        ]
        c = self._column
        query = (
            self._base_query(
                limit=max(
                    len(configured_symbols),
                    self.config.tradingview.max_candidates,
                )
            )
            .select(*self._select_fields(*select_cols))
            .where(
                c("name").isin(configured_symbols),
                *self._common_equity_conditions(),
            )
        )
        df = self._execute(query)

        by_symbol: dict[str, dict[str, Any]] = {}
        for _, row in df.iterrows():
            metadata = self._row_metadata(row)
            returned_name = str(metadata.get("name") or "").upper().strip()
            exchange = str(metadata.get("exchange") or "").upper().strip()
            resolved_symbol = self._symbol_from_ticker(returned_name).upper().strip()
            if not resolved_symbol:
                continue
            metadata["source"] = "tradingview_screener"
            metadata["universe"] = "peer_confirmed_htf_pivots"
            metadata["tv_query_name"] = returned_name
            metadata["tv_query_ticker"] = (
                f"{exchange}:{returned_name}" if exchange else returned_name
            )
            by_symbol[resolved_symbol] = metadata

        missing = [sym for sym in configured_symbols if sym not in by_symbol]
        if missing:
            LOG.warning(
                "peer_confirmed_htf_pivots screener missing %d/%d configured symbols: %s",
                len(missing),
                len(configured_symbols),
                ",".join(missing),
            )

        rows: list[Candidate] = []
        configured_order = {
            str(sym).upper().strip(): idx
            for idx, sym in enumerate(configured_symbols, start=1)
        }
        for configured_symbol in configured_symbols:
            symbol = self._symbol_from_ticker(configured_symbol).upper().strip()
            metadata = dict(by_symbol.get(symbol, {}))
            if not metadata:
                metadata = {
                    "source": "tradingview_screener",
                    "universe": "peer_confirmed_htf_pivots",
                    "tv_query_ticker": symbol,
                }
            day_change = float(metadata.get("change_from_open", 0.0) or 0.0)
            abs_day_change = abs(day_change)
            raw_relative_volume = float(metadata.get("relative_volume_10d_calc", 1.0) or 1.0)
            relative_volume_cap = max(0.75, float(params.get("screener_relative_volume_cap", 2.5) or 2.5))
            effective_relative_volume = self._effective_relative_volume(symbol, raw_relative_volume, params, cap_default=relative_volume_cap, standard_floor=0.5)
            move_sweet_spot = max(0.20, float(params.get("screener_activity_move_sweet_spot_pct", 1.25) or 1.25))
            move_cap = max(move_sweet_spot, float(params.get("screener_activity_move_cap_pct", 3.0) or 3.0))
            if abs_day_change <= move_sweet_spot:
                move_fit = 0.40 + (abs_day_change / move_sweet_spot)
            else:
                overshoot = min(1.0, (abs_day_change - move_sweet_spot) / max(move_cap - move_sweet_spot, 1e-9))
                move_fit = max(0.55, 1.40 - (overshoot * 0.80))
            focus_score = move_fit * effective_relative_volume
            bias_threshold = max(0.10, float(params.get("screener_contrarian_bias_threshold_pct", 1.0) or 1.0))
            directional_bias = None
            if day_change >= bias_threshold:
                directional_bias = Side.SHORT
            elif day_change <= (-bias_threshold):
                directional_bias = Side.LONG
            metadata["configured_order"] = int(
                configured_order.get(symbol, 9_999)
            )
            metadata["pivot_focus_score"] = float(focus_score)
            metadata["activity_move_fit"] = float(move_fit)
            metadata["abs_change_from_open"] = float(abs_day_change)
            metadata["activity_relative_volume"] = float(effective_relative_volume)
            metadata["raw_relative_volume_10d_calc"] = float(raw_relative_volume)
            metadata["rvol_profile"] = rvol_profile_for_symbol(symbol, params or {})
            metadata["screener_bias_mode"] = "contrarian_sr_scalp"
            rows.append(
                Candidate(
                    symbol=symbol,
                    strategy=self.strategy_name,
                    rank=0,
                    activity_score=float(focus_score),
                    directional_bias=directional_bias,
                    metadata=metadata,
                )
            )
        rows.sort(
            key=lambda candidate: (
                float(candidate.activity_score),
                float(candidate.metadata.get("activity_move_fit", 0.0) or 0.0),
                float(candidate.metadata.get("activity_relative_volume", 0.0) or 0.0),
                -int(candidate.metadata.get("configured_order", 9_999) or 9_999),
            ),
            reverse=True,
        )
        for idx, candidate in enumerate(rows, start=1):
            candidate.rank = idx
        return rows
