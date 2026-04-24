# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
    LOG,
    Side,
)
from ..screener_base import BaseStrategyScreener

class ORBScreener(BaseStrategyScreener):
    strategy_name = 'opening_range_breakout'

    def watchlist_mode(self) -> str:
        params = self.config.strategies[self.strategy_name].params
        return str(params.get("orb_watchlist_mode", "premarket") or "premarket").strip().lower() or "premarket"

    def cached_candidates(self, now, cached: list[Candidate] | None, last_refresh) -> list[Candidate] | None:
        mode = self.watchlist_mode()
        if mode != "premarket" or now.time().hour < 9 or (now.time().hour == 9 and now.time().minute < 30):
            return None
        if cached is not None and last_refresh is not None and last_refresh.date() == now.date():
            return cached
        LOG.warning(
            "ORB premarket watchlist requested after 09:30 ET without a same-day cached premarket candidate list; returning no candidates. "
            "Start before the open or use orb_watchlist_mode=early_session/none."
        )
        return []

    def run(self) -> list[Candidate]:
        params = self.config.strategies[self.strategy_name].params
        mode = self.watchlist_mode() or "premarket"
        c = self._column
        conditions = [*self._small_cap_base_conditions(2, 20)]
        if mode in {"premarket", "early_session"}:
            conditions.extend(
                [
                    c("change_from_open") >= params.get("watchlist_min_change", 5.0),
                    c("volume") >= params.get("watchlist_min_volume", 500_000),
                ]
            )
        q = (
            self._base_query()
            .select(*self._select_fields(
                "name",
                "description",
                "exchange",
                "close",
                "volume",
                "market_cap_basic",
                "change_from_open",
                "relative_volume_10d_calc",
            ))
            .where(*conditions)
        )
        if mode == "none":
            q = q.order_by(self._order_field("volume"), ascending=False)
        else:
            q = q.order_by(self._order_field("change_from_open"), ascending=False)
        df = self._execute(q)
        if mode == "none":
            return self._candidate_rows(
                df,
                self.strategy_name,
                directional_bias_fn=lambda row: Side.LONG,
                activity_score_fn=lambda row: float(row.get("relative_volume_10d_calc", 1.0) or 1.0) * float(row.get("volume", 0.0) or 0.0),
            )
        # Score by change_from_open weighted by RVOL — a 6% premarket mover
        # with 3x RVOL is a better ORB candidate than a 10% mover with 0.8x.
        return self._candidate_rows(
            df,
            self.strategy_name,
            directional_bias_fn=lambda row: Side.LONG,
            activity_score_fn=lambda row: (
                float(row.get("change_from_open", 0.0) or 0.0)
                * max(0.5, min(float(row.get("relative_volume_10d_calc", 1.0) or 1.0), 3.0))
            ),
        )
