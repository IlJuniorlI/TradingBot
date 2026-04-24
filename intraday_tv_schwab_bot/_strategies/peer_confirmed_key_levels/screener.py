# SPDX-License-Identifier: MIT
from ..shared import (
    Any,
    Candidate,
    LOG,
)
from ..screener_base import BaseStrategyScreener
from ..rvol import rvol_profile_for_symbol

class PeerConfirmedKeyLevelsScreener(BaseStrategyScreener):
    strategy_name = 'peer_confirmed_key_levels'

    def _active_strategy_name(self) -> str:
        return str(self.strategy_name or self.config.strategy).strip().lower()

    def _universe_label(self) -> str:
        return self._active_strategy_name()

    def run(self) -> list[Candidate]:
        strategy_name = self._active_strategy_name()
        params = self.config.strategies[strategy_name].params
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
            metadata["universe"] = self._universe_label()
            metadata["tv_query_name"] = returned_name
            metadata["tv_query_ticker"] = f"{exchange}:{returned_name}" if exchange else returned_name
            by_symbol[resolved_symbol] = metadata

        missing = [sym for sym in configured_symbols if sym not in by_symbol]
        if missing:
            LOG.warning(
                "%s screener missing %d/%d configured symbols: %s",
                self._universe_label(),
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
                    "universe": self._universe_label(),
                    "tv_query_ticker": symbol,
                }
            day_change = abs(float(metadata.get("change_from_open", 0.0) or 0.0))
            raw_relative_volume = float(metadata.get("relative_volume_10d_calc", 1.0) or 1.0)
            effective_relative_volume = self._effective_relative_volume(symbol, raw_relative_volume, params, cap_default=2.5, standard_floor=0.5)
            focus_score = day_change * effective_relative_volume
            metadata["configured_order"] = int(configured_order.get(symbol, 9_999))
            metadata["peer_focus_score"] = float(focus_score)
            metadata["activity_relative_volume"] = float(effective_relative_volume)
            metadata["raw_relative_volume_10d_calc"] = float(raw_relative_volume)
            metadata["rvol_profile"] = rvol_profile_for_symbol(symbol, params or {})
            rows.append(
                Candidate(
                    symbol=symbol,
                    strategy=strategy_name,
                    rank=0,
                    activity_score=float(focus_score),
                    directional_bias=None,
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
