# SPDX-License-Identifier: MIT
"""Screener for the top_tier_adaptive strategy.

The tradable universe is a fixed list of mega-cap liquid stocks (configured
in params.tradable) so the screener just fetches their RTH quotes and ranks
them by |change| × volume.

Bypasses the TradingViewScreenerClient._CANONICAL_SCREEN_FIELDS mapping that
session-routes `close`/`change_from_open`/`volume` to `premarket_*` or
`postmarket_*` variants during 04:00-09:30 and 16:00-20:00. Mega-cap
pre/postmarket prints aren't useful signal for this strategy — sticking
with the regular-session `change` / `volume` / `close` keeps the ranking
stable across all sessions.
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
        # Raw field names (no _select_fields canonical mapping) so the
        # query never substitutes premarket_*/postmarket_* variants.
        query = (
            self._base_query()
            .select("name", "close", "volume", "change", "market_cap_basic")
            .where(
                *self._common_equity_conditions(),
                c("name").isin(tradable),
            )
        )
        rows = self._execute(query)
        # Alias `change` to `change_from_open` in the candidate row metadata so
        # downstream consumers (dashboard candidate cards, watchlist tiles,
        # selected-symbol meta, dashboard_cache, entry_gatekeeper) that read
        # `change_from_open` keep working. The value is % from prior close
        # (what TradingView/Yahoo show as "Change %"), not the strict
        # "% from today's open" the field name implies — but the dashboard
        # labels it generically as "Day %" so the display remains accurate.
        if "change" in rows.columns:
            rows["change_from_open"] = rows["change"]
        return self._candidate_rows(
            rows,
            strategy=self.strategy_name,
            directional_bias_fn=lambda row: (
                Side.LONG
                if float(row.get("change", 0.0) or 0.0) > 0.20
                else (Side.SHORT if float(row.get("change", 0.0) or 0.0) < -0.20 else None)
            ),
            activity_score_fn=lambda row: (
                abs(float(row.get("change", 0.0) or 0.0))
                * max(0.5, float(row.get("volume", 0.0) or 0.0) / 1_000_000.0)
            ),
        )
