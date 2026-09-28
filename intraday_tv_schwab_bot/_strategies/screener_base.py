# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, TYPE_CHECKING, Callable, cast

import pandas as pd

from ..models import Candidate
from ..sessions import EQUITY_RTH_OPEN
from .rvol import effective_relative_volume, relative_volume_gate_threshold

if TYPE_CHECKING:
    from ..screener_client import TradingViewScreenerClient

LOG = logging.getLogger(__name__)


def rank_candidates(candidates: Iterable[Candidate], limit: int | None = None) -> list[Candidate]:
    """Candidates best first, cut to ``limit`` (None keeps all), ranked 1..N.

    Best is the higher ``activity_score``, then the earlier
    ``candidate_query_order`` (the row's place in the screener query's own
    ``order_by``; a missing or unreadable one comes after every readable
    one), so ties are broken deterministically rather than by list order;
    the sort is stable after that. ``rank`` is rewritten
    because it is the final tiebreak in
    ``shared_entry.SharedEntryPolicy.rank_key`` and is what the dashboard
    candidate card and the audit log's ``candidate_rank`` display: a screener
    that merges two screens (small_cap_squeeze's premarket lock) would
    otherwise carry each screen's own, duplicated ranks.
    """
    def _key(candidate: Candidate) -> tuple[float, int]:
        raw_order = candidate.metadata.get("candidate_query_order")
        try:
            query_order = int(raw_order) if raw_order is not None else 9_999_999
        except (TypeError, ValueError):
            query_order = 9_999_999
        return float(candidate.activity_score), -query_order

    ranked = sorted(candidates, key=_key, reverse=True)[:limit]
    for rank, candidate in enumerate(ranked, start=1):
        candidate.rank = rank
    return ranked


def gap_rvol_activity(row: pd.Series) -> float:
    """The gap-and-go activity score: ``change_from_open`` (percent) times the
    10-day relative volume clipped to [0.5, 3.0] (a missing or zero RVOL
    counts as 1.0). A 12% gapper at 2x RVOL ranks above a 20% gapper at
    0.6x; the clip keeps one fluke print from dominating, so the gap decides
    otherwise."""
    return (
        float(row.get("change_from_open", 0.0) or 0.0)
        * max(0.5, min(float(row.get("relative_volume_10d_calc", 1.0) or 1.0), 3.0))
    )


class BaseStrategyScreener:
    strategy_name: str

    def __init__(self, client: 'TradingViewScreenerClient'):
        self.client = client
        self.config = client.config
        if not str(getattr(self, "strategy_name", "") or "").strip():
            raise ValueError(f"{self.__class__.__name__} must define a non-empty strategy_name")

    def cached_candidates(self, now, cached: list[Candidate] | None, last_refresh) -> list[Candidate] | None:
        return None

    @staticmethod
    def _premarket_locked_candidates(now, cached: list[Candidate] | None, last_refresh, label: str) -> list[Candidate] | None:
        """``cached_candidates`` for a watchlist frozen before the open (the
        ORB family's ``orb_watchlist_mode: premarket``).

        Before 09:30 ET None, so the screen runs. From the open on, the list
        cached on the same ET day; with none, an empty list and a warning
        naming ``label``: a bot started after the open has no premarket
        list to freeze, and a re-screen would not be one.
        """
        if now.time() < EQUITY_RTH_OPEN:
            return None
        if cached is not None and last_refresh is not None and last_refresh.date() == now.date():
            return cached
        LOG.warning(
            "%s premarket watchlist requested after 09:30 ET without a same-day cached premarket candidate list; returning no candidates. "
            "Start before the open or use orb_watchlist_mode=early_session/none.",
            label,
        )
        return []

    def run(self) -> list[Candidate]:
        raise NotImplementedError

    def _execute(self, query: Any) -> pd.DataFrame:
        return self.client.execute(query, strategy=self.strategy_name)

    def _base_query(self, limit: int | None = None):
        return self.client.base_query(limit)

    def _select_fields(self, *fields: str) -> tuple[str, ...]:
        select_fields = getattr(self.client, "select_fields", None)
        if callable(select_fields):
            typed_select_fields = cast(Callable[..., tuple[str, ...]], select_fields)
            return tuple(str(field) for field in typed_select_fields(*fields))
        return tuple(str(field) for field in fields)

    def _order_field(self, name: str) -> str:
        order_field = getattr(self.client, "order_field", None)
        if callable(order_field):
            return str(order_field(name))
        return str(name)

    def _column(self, name: str):
        return self.client.column(name)

    def _common_equity_conditions(self) -> list:
        return self.client.common_equity_conditions()

    def _curated_symbol_conditions(self, symbols: list[str]) -> list:
        return self.client.curated_symbol_conditions(symbols)

    def _liquid_equity_conditions(self, min_price: float = 5.0, max_price: float | None = None):
        return self.client.liquid_equity_conditions(min_price=min_price, max_price=max_price)

    def _small_cap_base_conditions(self, min_price: float = 2.0, max_price: float = 20.0):
        return self.client.small_cap_base_conditions(min_price=min_price, max_price=max_price)

    def _symbol_from_ticker(self, ticker: str) -> str:
        return self.client.symbol_from_ticker(ticker)

    def _row_metadata(self, row: pd.Series) -> dict[str, Any]:
        return self.client.row_metadata(row)

    @staticmethod
    def _effective_relative_volume(symbol: str, raw_relative_volume: object, params: dict[str, Any] | None = None, *, cap_default: float = 2.5, standard_floor: float = 0.5, dollar_volume: object = None) -> float:
        return effective_relative_volume(symbol, raw_relative_volume, params or {}, cap_default=cap_default, standard_floor=standard_floor, dollar_volume=dollar_volume)

    @staticmethod
    def _relative_volume_gate_threshold(symbol: str, base_threshold: object, params: dict[str, Any] | None = None, *, dollar_volume: object = None) -> float:
        return relative_volume_gate_threshold(symbol, base_threshold, params or {}, dollar_volume=dollar_volume)

    def _candidate_rows(self, df: pd.DataFrame, strategy: str, directional_bias_fn=None, activity_score_fn=None) -> list[Candidate]:
        return self.client.candidate_rows(df, strategy, directional_bias_fn=directional_bias_fn, activity_score_fn=activity_score_fn)
