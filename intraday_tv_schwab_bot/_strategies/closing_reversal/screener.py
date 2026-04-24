# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
    Side,
)
from ..screener_base import BaseStrategyScreener

class ClosingReversalScreener(BaseStrategyScreener):
    strategy_name = 'closing_reversal'

    def run(self) -> list[Candidate]:
        params = self.config.strategies[self.strategy_name].params
        c = self._column
        # Select 'high' so we can compute pullback-from-session-high post-query.
        q = (
            self._base_query()
            .select(*self._select_fields("name", "description", "exchange", "close", "high", "volume", "market_cap_basic", "relative_volume_10d_calc", "change_from_open"))
            .where(
                *self._small_cap_base_conditions(2, 20),
                c("relative_volume_10d_calc") >= params.get("min_rvol", 2.5),
                c("change_from_open").between(params.get("min_day_strength", 5.0), params.get("max_day_strength", 40.0)),
            )
            .order_by(self._order_field("change_from_open"), ascending=False)
        )
        df = self._execute(q)
        if not df.empty and "high" in df.columns and "close" in df.columns:
            # Filter: stock must have pulled back from session high (not still
            # at highs = no reversal context) but not pulled back too far
            # (broken trend = not a bounce candidate).
            session_high = df["high"].astype(float)
            close = df["close"].astype(float)
            pullback_pct = ((session_high - close) / session_high.clip(lower=0.01)).clip(lower=0.0)
            max_pullback = float(params.get("max_pullback_from_high", 0.05))
            min_pullback = float(params.get("screener_min_pullback_from_high", 0.003))
            df = df[(pullback_pct >= min_pullback) & (pullback_pct <= max_pullback)].copy()
        return self._candidate_rows(
            df,
            self.strategy_name,
            directional_bias_fn=lambda row: Side.LONG,
            activity_score_fn=lambda row: row.get("change_from_open", 0.0),
        )
