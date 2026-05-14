# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
    Side,
    pd,
)
from ..screener_base import BaseStrategyScreener
from ..rvol import rvol_profile_for_symbol

class VolatilitySqueezeBreakoutScreener(BaseStrategyScreener):
    strategy_name = 'volatility_squeeze_breakout'

    def run(self) -> list[Candidate]:
        params = self.config.strategies[self.strategy_name].params
        min_change = float(params.get("min_change_from_open", 0.8))
        max_change = float(params.get("max_change_from_open", 8.0))
        min_rvol = float(params.get("min_rvol", 1.35))
        # 2026-05-14: tighter screener for higher-probability squeeze setups.
        #   * min_price floor lifted 8 → 12 (param-tunable now) to filter
        #     low-float volatility traps where a single 50k-share order
        #     can move 2%+ on its own.
        #   * default upper-band of change_from_open clamped further by
        #     the strategy yaml (4.5% vs prior 7.5%) — stocks already up
        #     5%+ rarely have clean continuation room out of a squeeze.
        #   * session-range cap default tightened 2.5% → 1.8% (yaml).
        screener_min_price = float(params.get("screener_min_price", 12.0))
        # Select 'high' and 'low' so we can compute an intraday-range proxy
        # for compression: tight (high - low) / close = likely squeeze context.
        select_cols = ("name", "description", "exchange", "close", "high", "low", "volume", "market_cap_basic", "relative_volume_10d_calc", "change_from_open")

        c = self._column
        query_limit = max(40, self.config.tradingview.max_candidates * 6)
        # Lower the change_from_open floor to 0.3% to catch tighter squeezes
        # that have barely started moving. The old 0.8% floor missed early-
        # stage breakouts from very tight compression.
        screener_floor = max(0.3, min_change * 0.5)
        long_q = (
            self._base_query(limit=query_limit)
            .select(*self._select_fields(*select_cols))
            .where(
                *self._liquid_equity_conditions(min_price=screener_min_price),
                c("change_from_open").between(screener_floor, max_change),
            )
            .order_by(self._order_field("change_from_open"), ascending=False)
        )
        short_q = (
            self._base_query(limit=query_limit)
            .select(*self._select_fields(*select_cols))
            .where(
                *self._liquid_equity_conditions(min_price=screener_min_price),
                c("change_from_open").between(-max_change, -screener_floor),
            )
            .order_by(self._order_field("change_from_open"), ascending=True)
        )

        long_df = self._execute(long_q)
        short_df = self._execute(short_q)
        if long_df.empty and short_df.empty:
            return []
        if long_df.empty:
            df = short_df.copy()
        elif short_df.empty:
            df = long_df.copy()
        else:
            df = pd.concat([long_df, short_df], ignore_index=True)
        df = df.reindex(columns=list(select_cols))
        df = df.drop_duplicates(subset=["name"], keep="first")
        if df.empty:
            return []

        # Post-filter: prefer stocks with tight intraday range (compression proxy).
        # (high - low) / close is a rough measure of session volatility.
        # True squeezes have small intraday ranges relative to price.
        # 2026-05-14: default tightened 2.5% → 1.8%. Stocks already showing
        # >1.8% intraday range have already used much of the day's energy
        # — less probable to produce a clean expansion-phase breakout.
        max_session_range_pct = float(params.get("screener_max_session_range_pct", 0.018))
        if "high" in df.columns and "low" in df.columns and "close" in df.columns:
            session_range_pct = ((df["high"].astype(float) - df["low"].astype(float)) / df["close"].astype(float).clip(lower=0.01)).clip(lower=0.0)
            df = df[session_range_pct <= max_session_range_pct].copy()
            if df.empty:
                return []

        df["_symbol"] = df["name"].map(lambda value: self._symbol_from_ticker(str(value)).upper().strip())
        df["_raw_relative_volume"] = df["relative_volume_10d_calc"].fillna(0.0).astype(float)
        df["_rvol_required"] = df["_symbol"].map(lambda symbol: self._relative_volume_gate_threshold(symbol, min_rvol, params))
        df = df[df["_raw_relative_volume"] >= df["_rvol_required"]].copy()
        if df.empty:
            return []
        df["_effective_relative_volume"] = df.apply(lambda row: self._effective_relative_volume(str(row.get("_symbol") or ""), row.get("_raw_relative_volume", 0.0), params, cap_default=2.5, standard_floor=0.5), axis=1)
        df["_rvol_profile"] = df["_symbol"].map(lambda symbol: rvol_profile_for_symbol(symbol, params or {}))
        abs_change = df["change_from_open"].fillna(0.0).astype(float).abs()
        # Squeeze focus: prioritize small moves (about to break out) + high RVOL
        # + tight session range. Tight range stocks get a bonus.
        session_range = ((df["high"].astype(float) - df["low"].astype(float)) / df["close"].astype(float).clip(lower=0.01)).clip(lower=0.0) if "high" in df.columns else pd.Series(0.0, index=df.index)
        range_tightness_bonus = (max_session_range_pct - session_range).clip(lower=0.0) * 50.0
        # 2026-05-14: RVOL tier bonus. The base score uses linear RVOL
        # weighting (×2.0). Squeeze breakouts tend to fire from
        # accumulation phases — stocks with strongly elevated RVOL
        # (≥1.8×) are far more likely to have institutional positioning
        # building, which is what fuels the post-breakout expansion.
        # Bonus is +2.0 above the 1.8× threshold scaled by how much
        # above, capped at +5.0 (= effective_rvol 4.3+).
        screener_rvol_bonus_threshold = float(params.get("screener_rvol_bonus_threshold", 1.8))
        screener_rvol_bonus_scale = float(params.get("screener_rvol_bonus_scale", 2.0))
        screener_rvol_bonus_cap = float(params.get("screener_rvol_bonus_cap", 5.0))
        rvol_excess = (df["_effective_relative_volume"].astype(float) - screener_rvol_bonus_threshold).clip(lower=0.0)
        rvol_tier_bonus = (rvol_excess * screener_rvol_bonus_scale).clip(upper=screener_rvol_bonus_cap)
        df["_squeeze_focus_score"] = (
            (df["_effective_relative_volume"].astype(float) * 2.0)
            + (max_change - abs_change).clip(lower=0.0)
            + range_tightness_bonus
            + rvol_tier_bonus
        )
        df = df.sort_values(["_squeeze_focus_score", "_effective_relative_volume", "volume"], ascending=[False, False, False]).head(self.config.tradingview.max_candidates)
        return self._candidate_rows(
            df,
            self.strategy_name,
            directional_bias_fn=lambda row: Side.LONG if float(row.get("change_from_open", 0.0) or 0.0) >= 0 else Side.SHORT,
            activity_score_fn=lambda row: float(row.get("_squeeze_focus_score", 0.0) or 0.0),
        )
