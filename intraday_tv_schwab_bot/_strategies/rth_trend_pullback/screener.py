# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
    Side,
    pd,
)
from ..screener_base import BaseStrategyScreener
from ..rvol import rvol_profile_for_symbol

class RTHTrendPullbackScreener(BaseStrategyScreener):
    strategy_name = 'rth_trend_pullback'

    def run(self) -> list[Candidate]:
        params = self.config.strategies[self.strategy_name].params
        min_change = float(params.get("min_change_from_open", 2.0))
        max_change = float(params.get("max_change_from_open", 40.0))
        min_rvol = float(params.get("min_rvol", 1.5))
        select_cols = ("name", "description", "exchange", "close", "volume", "market_cap_basic", "relative_volume_10d_calc", "change_from_open")

        c = self._column
        query_limit = max(40, self.config.tradingview.max_candidates * 6)
        long_q = (
            self._base_query(limit=query_limit)
            .select(*self._select_fields(*select_cols))
            .where(
                *self._liquid_equity_conditions(min_price=5.0),
                c("change_from_open").between(min_change, max_change),
            )
            .order_by(self._order_field("change_from_open"), ascending=False)
        )
        short_q = (
            self._base_query(limit=query_limit)
            .select(*self._select_fields(*select_cols))
            .where(
                *self._liquid_equity_conditions(min_price=5.0),
                c("change_from_open").between(-max_change, -min_change),
            )
            .order_by(self._order_field("change_from_open"), ascending=True)
        )

        long_df = self._execute(long_q)
        short_df = self._execute(short_q)
        if long_df.empty and short_df.empty:
            df = pd.DataFrame(columns=list(select_cols))
        elif long_df.empty:
            df = short_df.copy()
        elif short_df.empty:
            df = long_df.copy()
        else:
            df = pd.concat([long_df, short_df], ignore_index=True)

        df = df.reindex(columns=list(select_cols))
        if df.empty:
            return []
        df = df.drop_duplicates(subset=["name"], keep="first")
        df["_symbol"] = df["name"].map(lambda value: self._symbol_from_ticker(str(value)).upper().strip())
        df["_raw_relative_volume"] = df["relative_volume_10d_calc"].fillna(0.0).astype(float)
        df["_rvol_required"] = df["_symbol"].map(lambda symbol: self._relative_volume_gate_threshold(symbol, min_rvol, params))
        df = df[df["_raw_relative_volume"] >= df["_rvol_required"]].copy()
        if df.empty:
            return []
        df["_effective_relative_volume"] = df.apply(lambda row: self._effective_relative_volume(str(row.get("_symbol") or ""), row.get("_raw_relative_volume", 0.0), params, cap_default=2.5, standard_floor=0.5), axis=1)
        df["_rvol_profile"] = df["_symbol"].map(lambda symbol: rvol_profile_for_symbol(symbol, params or {}))
        df["_trend_score"] = df["change_from_open"].astype(float).abs() * df["_effective_relative_volume"].astype(float)
        df = df.sort_values(["_trend_score", "_effective_relative_volume", "volume"], ascending=[False, False, False]).head(self.config.tradingview.max_candidates)
        return self._candidate_rows(
            df,
            self.strategy_name,
            directional_bias_fn=lambda row: Side.LONG if float(row.get("change_from_open", 0.0) or 0.0) >= 0 else Side.SHORT,
            activity_score_fn=lambda row: float(row.get("_trend_score", 0.0) or 0.0),
        )
