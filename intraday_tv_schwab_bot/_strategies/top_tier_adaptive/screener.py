# SPDX-License-Identifier: MIT
"""Screener for the top_tier_adaptive strategy.

Since the tradable universe is a fixed list of top top-tier liquid stocks (configured in
params.tradable), the screener simply fetches those symbols from TradingView
and ranks them by absolute intraday move × relative volume.
"""
from ..shared import Candidate, Side
from ..screener_base import BaseStrategyScreener


class TopTierAdaptiveScreener(BaseStrategyScreener):
    strategy_name = "top_tier_adaptive"

    def run(self) -> list[Candidate]:
        params = self.config.active_strategy.params
        tradable = [str(s).upper().strip() for s in (params.get("tradable") or []) if str(s).strip()]
        if not tradable:
            return []
        c = self._column
        query = (
            self._base_query()
            .select(
                *self._select_fields(
                    "name",
                    "description",
                    "close",
                    "volume",
                    "market_cap_basic",
                    "relative_volume_10d_calc",
                    "change_from_open",
                ),
            )
            .where(
                *self._common_equity_conditions(),
                c("name").isin(tradable),
            )
        )
        rows = self._execute(query)
        return self._candidate_rows(
            rows,
            strategy=self.strategy_name,
            directional_bias_fn=lambda row: (
                Side.LONG
                if float(row.get("change_from_open", 0.0) or 0.0) > 0.20
                else (Side.SHORT if float(row.get("change_from_open", 0.0) or 0.0) < -0.20 else None)
            ),
            activity_score_fn=lambda row: (
                abs(float(row.get("change_from_open", 0.0) or 0.0))
                * max(0.5, min(float(row.get("relative_volume_10d_calc", 1.0) or 1.0), 3.0))
            ),
        )
