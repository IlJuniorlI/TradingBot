# SPDX-License-Identifier: MIT
from ..shared import Any, Candidate, LOG, Side
from ..screener_base import BaseStrategyScreener
from ..rvol import rvol_profile_for_symbol


class PeerConfirmedTrendContinuationScreener(BaseStrategyScreener):
    strategy_name = 'peer_confirmed_trend_continuation'

    def run(self) -> list[Candidate]:
        params = self.config.strategies[self.strategy_name].params
        configured_symbols = [str(sym).upper().strip() for sym in params.get("tradable", []) if str(sym).strip()]
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
        q = (
            self._base_query(limit=max(len(configured_symbols), self.config.tradingview.max_candidates))
            .select(*self._select_fields(*select_cols))
            .where(
                c("name").isin(configured_symbols),
                *self._common_equity_conditions(),
            )
        )
        df = self._execute(q)

        by_symbol: dict[str, dict[str, Any]] = {}
        for _, row in df.iterrows():
            metadata = self._row_metadata(row)
            returned_name = str(metadata.get("name") or "").upper().strip()
            exchange = str(metadata.get("exchange") or "").upper().strip()
            resolved_symbol = self._symbol_from_ticker(returned_name).upper().strip()
            if not resolved_symbol:
                continue
            metadata["source"] = "tradingview_screener"
            metadata["universe"] = "peer_confirmed_trend_continuation"
            metadata["tv_query_name"] = returned_name
            metadata["tv_query_ticker"] = f"{exchange}:{returned_name}" if exchange else returned_name
            by_symbol[resolved_symbol] = metadata

        missing = [sym for sym in configured_symbols if sym not in by_symbol]
        if missing:
            LOG.warning(
                "peer_confirmed_trend_continuation screener missing %d/%d configured symbols: %s",
                len(missing),
                len(configured_symbols),
                ",".join(missing),
            )

        rows: list[Candidate] = []
        configured_order = {str(sym).upper().strip(): idx for idx, sym in enumerate(configured_symbols, start=1)}
        for configured_symbol in configured_symbols:
            symbol = self._symbol_from_ticker(configured_symbol).upper().strip()
            metadata = dict(by_symbol.get(symbol, {}))
            if not metadata:
                metadata = {
                    "source": "tradingview_screener",
                    "universe": "peer_confirmed_trend_continuation",
                    "tv_query_ticker": symbol,
                }
            day_change = float(metadata.get("change_from_open", 0.0) or 0.0)
            raw_relative_volume = float(metadata.get("relative_volume_10d_calc", 1.0) or 1.0)
            effective_relative_volume = self._effective_relative_volume(symbol, raw_relative_volume, params, cap_default=2.5, standard_floor=0.5)
            focus_score = abs(day_change) * effective_relative_volume
            directional_bias = None
            if day_change > 0.30:
                directional_bias = Side.LONG
            elif day_change < -0.30:
                directional_bias = Side.SHORT
            metadata["configured_order"] = int(configured_order.get(symbol, 9_999))
            metadata["trend_focus_score"] = float(focus_score)
            metadata["activity_relative_volume"] = float(effective_relative_volume)
            metadata["raw_relative_volume_10d_calc"] = float(raw_relative_volume)
            metadata["rvol_profile"] = rvol_profile_for_symbol(symbol, params or {})
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
            key=lambda c: (
                float(c.activity_score),
                abs(float(c.metadata.get("change_from_open", 0.0) or 0.0)),
                float(c.metadata.get("activity_relative_volume", 0.0) or 0.0),
                -int(c.metadata.get("configured_order", 9_999) or 9_999),
            ),
            reverse=True,
        )
        for idx, candidate in enumerate(rows, start=1):
            candidate.rank = idx
        return rows
