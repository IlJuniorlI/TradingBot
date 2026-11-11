# SPDX-License-Identifier: MIT
"""Screener for swing_leader_pullback: the momentum leaders of an index universe.

One TradingView query returns every member of the configured indexes
(``params.index_universe``; the shipped preset's S&P 500 and Nasdaq-100)
with its 1-year and 1-month performance. A member's 12-1 momentum is the
price a month ago over the price a year ago, less 1:
(1 + Perf.Y) / (1 + Perf.1M) - 1, in which today's price cancels, so the
rank does not move with the session. The leaders are the top
``leader_fraction`` of the members by that momentum; each is a LONG
candidate that carries its momentum, its rank and the universe's size. The
strategy reads the pullback itself from daily bars.

Fail closed: an answer with fewer than ``min_universe_size`` rankable
members is no universe to take a top tenth of, and yields no candidate; so
does a query that raises (logged with its type), which the next refresh
asks again.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ...models import Candidate, Side
from ..screener_base import BaseStrategyScreener

LOG = logging.getLogger(__name__)

# Above any index pair a preset names: the S&P 500 and the Nasdaq-100
# together answer about 510 rows.
UNIVERSE_QUERY_LIMIT = 2000
FIELDS = ("name", "exchange", "close", "volume", "market_cap_basic", "Perf.Y", "Perf.1M")


def leader_rows(rows: pd.DataFrame, *, leader_fraction: float, min_universe_size: int) -> tuple[pd.DataFrame, int]:
    """The leaders among ``rows`` (one per index member): (the leaders by
    falling 12-1 momentum, with ``momentum_12_1``, ``leader_rank`` and
    ``universe_size`` added; the count of rankable members). A member whose
    performance fields do not read, or whose month-ago price would not be
    positive, is not rankable. Fewer rankable members than
    ``min_universe_size``: no leaders."""
    if rows is None or rows.empty or "Perf.Y" not in rows.columns or "Perf.1M" not in rows.columns:
        return pd.DataFrame(), 0
    year = pd.to_numeric(rows["Perf.Y"], errors="coerce") / 100.0
    month = pd.to_numeric(rows["Perf.1M"], errors="coerce") / 100.0
    with np.errstate(divide="ignore", invalid="ignore"):
        momentum = (1.0 + year) / (1.0 + month) - 1.0
    rankable = np.isfinite(momentum) & ((1.0 + month) > 0.0) & ((1.0 + year) > 0.0)
    universe = rows.loc[rankable].copy()
    size = len(universe)
    if size < int(min_universe_size):
        return pd.DataFrame(), size
    universe["momentum_12_1"] = momentum[rankable].astype(float)
    leaders = universe.loc[universe["momentum_12_1"].rank(pct=True) >= 1.0 - float(leader_fraction)]
    leaders = leaders.sort_values("momentum_12_1", ascending=False, kind="mergesort").copy()
    leaders["leader_rank"] = range(1, len(leaders) + 1)
    leaders["universe_size"] = size
    return leaders, size


class SwingLeaderPullbackScreener(BaseStrategyScreener):
    strategy_name = 'swing_leader_pullback'
    _last_short_universe: int | None = None

    def cached_candidates(self, now, cached: list[Candidate] | None, last_refresh) -> list[Candidate] | None:
        """The day's first list of leaders stands for the day: the rank does
        not move with the session (module docstring), so a refresh would ask
        the same question again. An empty list (a short universe, or no
        screen yet today) is screened again."""
        if cached and last_refresh is not None and last_refresh.date() == now.date():
            return cached
        return None

    def run(self) -> list[Candidate]:
        params = self.config.strategies[self.strategy_name].params
        indexes = [str(index) for index in params["index_universe"]]
        # Raw field names: the performance fields have no session variant,
        # and the rank must not read a premarket ``close``.
        query = self._base_query(limit=UNIVERSE_QUERY_LIMIT).set_index(*indexes).select(*FIELDS)
        try:
            rows = self._execute(query)
        except Exception as exc:
            # No leaders this pass, like a short universe. The engine does
            # not catch a screener's error, so one raised here would end the
            # cycle before it managed the positions this strategy carries
            # into the open; the client has logged the traceback.
            LOG.warning("swing_leader_pullback screener: the query failed (%s: %s); no leaders this pass.",
                        type(exc).__name__, exc)
            return []
        leaders, size = leader_rows(rows, leader_fraction=float(params["leader_fraction"]),
                                    min_universe_size=int(params["min_universe_size"]))
        if leaders.empty:
            # Logged when the shortfall changes, not on every refresh.
            if size != self._last_short_universe:
                LOG.warning("swing_leader_pullback screener: %d rankable members of %s, under min_universe_size %d; "
                            "no leaders this pass.", size, ", ".join(indexes), int(params["min_universe_size"]))
                self._last_short_universe = size
            return []
        self._last_short_universe = None
        return self._candidate_rows(
            leaders,
            self.strategy_name,
            directional_bias_fn=lambda row: Side.LONG,
            activity_score_fn=lambda row: float(row["momentum_12_1"]),
        )
