# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
    Side,
)
from ..screener_base import BaseStrategyScreener

class MomentumIntoCloseScreener(BaseStrategyScreener):
    strategy_name = 'momentum_close'

    def run(self) -> list[Candidate]:
        params = self.config.strategies[self.strategy_name].params
        c = self._column
        q = (
            self._base_query()
            .select(*self._select_fields("name", "description", "exchange", "close", "volume", "market_cap_basic", "relative_volume_10d_calc", "change_from_open"))
            .where(
                *self._small_cap_base_conditions(2, 20),
                c("relative_volume_10d_calc") >= params.get("min_rvol", 2.0),
                c("change_from_open").between(params.get("min_change_from_open", 3.0), params.get("max_change_from_open", 30.0)),
            )
            .order_by(self._order_field("change_from_open"), ascending=False)
        )
        df = self._execute(q)
        return self._candidate_rows(
            df,
            self.strategy_name,
            directional_bias_fn=lambda row: Side.LONG,
            activity_score_fn=lambda row: row.get("change_from_open", 0.0),
        )
