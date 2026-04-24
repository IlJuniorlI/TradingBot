# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
    Side,
)
from ..screener_base import BaseStrategyScreener
from ..rvol import rvol_profile_for_symbol

class PairsResidualScreener(BaseStrategyScreener):
    strategy_name = 'pairs_residual'

    def run(self) -> list[Candidate]:
        pairs = self.config.pairs
        if not pairs:
            return []
        tickers = sorted({str(p.symbol).upper().strip() for p in pairs} | {str(p.reference).upper().strip() for p in pairs})
        params = self.config.strategies[self.strategy_name].params
        min_rvol = float(params.get("min_rvol", 1.5))
        c = self._column
        q = (
            self._base_query(limit=max(5, len(tickers)))
            .select(*self._select_fields("name", "description", "exchange", "close", "volume", "market_cap_basic", "relative_volume_10d_calc", "change_from_open"))
            .where(
                c("name").isin(tickers),
                c("is_primary") == True,
                c("exchange") != "OTC",
            )
        )
        df = self._execute(q)
        by_symbol = {self._symbol_from_ticker(str(row.get("name"))).upper().strip(): self._row_metadata(row) for _, row in df.iterrows()}
        min_day_strength = float(params.get("min_day_strength", 2.0))
        rows: list[Candidate] = []
        for pair in pairs:
            symbol = str(pair.symbol).upper().strip()
            reference = str(pair.reference).upper().strip()
            row = by_symbol.get(symbol)
            ref_row = by_symbol.get(reference)
            if not row or not ref_row:
                continue
            traded_rvol = float(row.get("relative_volume_10d_calc", 0.0) or 0.0)
            required_rvol = self._relative_volume_gate_threshold(symbol, min_rvol, params)
            if traded_rvol < required_rvol:
                continue
            effective_traded_rvol = self._effective_relative_volume(symbol, traded_rvol, params, cap_default=2.5, standard_floor=0.5)
            day_strength = float(row.get("change_from_open", 0.0) or 0.0)
            if abs(day_strength) < min_day_strength:
                continue
            reference_strength = float(ref_row.get("change_from_open", 0.0) or 0.0)
            relative_strength_gap = day_strength - reference_strength
            pref = str(pair.side_preference or "both").strip().lower()
            if pref == "long":
                directional_bias = Side.LONG
            elif pref == "short":
                directional_bias = Side.SHORT
            else:
                directional_bias = Side.LONG if relative_strength_gap >= 0 else Side.SHORT
            focus_score = abs(relative_strength_gap) * effective_traded_rvol
            metadata = {
                **row,
                "pair_focus_score": focus_score,
                "pair_relative_change_from_open": relative_strength_gap,
                "pair_reference_change_from_open": reference_strength,
                "pair_reference_relative_volume_10d_calc": float(ref_row.get("relative_volume_10d_calc", 0.0) or 0.0),
                "activity_relative_volume": float(effective_traded_rvol),
                "raw_relative_volume_10d_calc": float(traded_rvol),
                "rvol_profile": rvol_profile_for_symbol(symbol, params or {}),
                "relative_volume_gate_required": float(required_rvol),
                "pair": {
                    "symbol": symbol,
                    "reference": reference,
                    "side_preference": pair.side_preference,
                    "sector": pair.sector,
                    "industry": pair.industry,
                },
            }
            rows.append(Candidate(symbol=symbol, strategy=self.strategy_name, rank=0, activity_score=focus_score, directional_bias=directional_bias, metadata=metadata))
        rows.sort(
            key=lambda c: (
                float(c.activity_score),
                abs(float(c.metadata.get("pair_relative_change_from_open", 0.0) or 0.0)),
                abs(float(c.metadata.get("change_from_open", 0.0) or 0.0)),
                float(c.metadata.get("activity_relative_volume", 0.0) or 0.0),
            ),
            reverse=True,
        )
        for idx, candidate in enumerate(rows, start=1):
            candidate.rank = idx
        return rows[: self.config.tradingview.max_candidates]
