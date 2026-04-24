# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
    Side,
    pd,
)
from ..screener_base import BaseStrategyScreener
from ..rvol import rvol_profile_for_symbol

class ZeroDteEtfOptionsScreener(BaseStrategyScreener):
    strategy_name = 'zero_dte_etf_options'

    def run(self) -> list[Candidate]:
        if not bool(self.config.options.enabled):
            return []
        underlyings = sorted(set(self.config.options.underlyings))
        if not underlyings:
            return []
        c = self._column
        q = (
            self._base_query(limit=max(10, len(underlyings)))
            .select(*self._select_fields("name", "description", "exchange", "close", "volume", "relative_volume_10d_calc", "change_from_open"))
            .where(
                c("name").isin(underlyings),
                c("close") >= self.config.options.min_underlying_price,
                c("exchange") != "OTC",
                c("volume") >= 100_000,
            )
        )
        df = self._execute(q)
        if df.empty:
            return []
        rows = []
        for _, row in df.iterrows():
            rec = self._row_metadata(row)
            underlying_symbol = self._symbol_from_ticker(str(rec.get("name") or "")).upper().strip()
            rec["confirm_index"] = self.config.options.confirmation_symbols.get(underlying_symbol, None)
            raw_relative_volume = float(rec.get("relative_volume_10d_calc", 1.0) or 1.0)
            effective_relative_volume = self._effective_relative_volume(underlying_symbol, raw_relative_volume, None, cap_default=2.5, standard_floor=1.0)
            rec["rvol_profile"] = rvol_profile_for_symbol(underlying_symbol, {})
            rec["activity_relative_volume"] = float(effective_relative_volume)
            rec["raw_relative_volume_10d_calc"] = float(raw_relative_volume)
            rec["activity_score"] = abs(float(rec.get("change_from_open", 0.0))) * effective_relative_volume
            rows.append(rec)
        sdf = pd.DataFrame(rows).sort_values(["activity_score", "change_from_open"], ascending=[False, False]).head(self.config.tradingview.max_candidates)
        out: list[Candidate] = []
        for rank, (_, row) in enumerate(sdf.iterrows(), start=1):
            symbol = self._symbol_from_ticker(str(row.get("name")))
            metadata = self._row_metadata(row)
            directional_bias = Side.LONG if float(metadata.get("change_from_open", 0.0)) >= 0 else Side.SHORT
            out.append(Candidate(symbol=symbol, strategy=self.strategy_name, rank=rank, activity_score=float(metadata.get("activity_score", 0.0)), directional_bias=directional_bias, metadata=metadata))
        return out
