# SPDX-License-Identifier: MIT
"""Screener for the top_tier_adaptive strategy.

The tradable universe is a fixed list of mega-cap liquid stocks (configured
in params.tradable) so the screener just fetches their RTH quotes and ships
both ``change`` (prior-close %, dashboard "Day %" display) and
``change_from_open`` (today-open %, decision metric).

Two distinct metrics carried alongside each other:

  * ``change`` (prior-close-relative) — TradingView's "Change %". What
    Yahoo / TradingView / most brokers show by default. Used for the
    dashboard candidate card's "Day %" display fallback when the live
    Schwab quote ``percent_change`` is unavailable.
  * ``change_from_open`` (today-open-relative) — pure intraday move from
    the session open. The right metric for an intraday strategy's
    directional bias + activity ranking, because the strategy can only
    trade the intraday move (the overnight gap already happened).
    Drives ``directional_bias_fn`` + ``activity_score_fn`` here.

The bias from ``change_from_open`` matches the live ``day_strength``
computation the strategy does itself via ``_compute_live_directional_bias``
in ``strategy.py`` — that method is the authoritative bias for entry
decisions; the screener-emitted bias is what the gatekeeper consults for
cooldown lookups before the strategy runs.
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
        bias_threshold = float(params.get("directional_bias_min_day_strength", 0.20))
        # Raw field names (no _select_fields canonical mapping). Both
        # ``change`` and ``change_from_open`` are pulled so display +
        # decision metrics stay separated. Top_tier's screener_windows
        # is RTH-only ([09:30, 15:00]) so the canonical pre/post-market
        # variants for ``change_from_open`` aren't a real concern, but
        # bypassing keeps the values stable if a cached candidate
        # survives a session transition.
        query = (
            self._base_query()
            .select("name", "close", "volume", "change", "change_from_open", "market_cap_basic")
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
                if float(row.get("change_from_open", 0.0) or 0.0) > bias_threshold
                else (Side.SHORT if float(row.get("change_from_open", 0.0) or 0.0) < -bias_threshold else None)
            ),
            activity_score_fn=lambda row: (
                abs(float(row.get("change_from_open", 0.0) or 0.0))
                * max(0.5, float(row.get("volume", 0.0) or 0.0) / 1_000_000.0)
            ),
        )
