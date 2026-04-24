# SPDX-License-Identifier: MIT
from __future__ import annotations

from typing import Any, TYPE_CHECKING, Callable, cast

import pandas as pd

from ..models import Candidate
from .rvol import effective_relative_volume, relative_volume_gate_threshold

if TYPE_CHECKING:
    from ..screener_client import TradingViewScreenerClient


class BaseStrategyScreener:
    strategy_name: str

    def __init__(self, client: 'TradingViewScreenerClient'):
        self.client = client
        self.config = client.config
        if not str(getattr(self, "strategy_name", "") or "").strip():
            raise ValueError(f"{self.__class__.__name__} must define a non-empty strategy_name")

    def cached_candidates(self, now, cached: list[Candidate] | None, last_refresh) -> list[Candidate] | None:
        return None

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

    def _liquid_equity_conditions(self, min_price: float = 5.0, max_price: float | None = None):
        return self.client.liquid_equity_conditions(min_price=min_price, max_price=max_price)

    def _small_cap_base_conditions(self, min_price: float = 2.0, max_price: float = 20.0):
        return self.client.small_cap_base_conditions(min_price=min_price, max_price=max_price)

    def _symbol_from_ticker(self, ticker: str) -> str:
        return self.client.symbol_from_ticker(ticker)

    def _row_metadata(self, row: pd.Series) -> dict[str, Any]:
        return self.client.row_metadata(row)

    @staticmethod
    def _effective_relative_volume(symbol: str, raw_relative_volume: object, params: dict[str, Any] | None = None, *, cap_default: float = 2.5, standard_floor: float = 0.5) -> float:
        return effective_relative_volume(symbol, raw_relative_volume, params or {}, cap_default=cap_default, standard_floor=standard_floor)

    @staticmethod
    def _relative_volume_gate_threshold(symbol: str, base_threshold: object, params: dict[str, Any] | None = None) -> float:
        return relative_volume_gate_threshold(symbol, base_threshold, params or {})

    def _candidate_rows(self, df: pd.DataFrame, strategy: str, directional_bias_fn=None, activity_score_fn=None) -> list[Candidate]:
        return self.client.candidate_rows(df, strategy, directional_bias_fn=directional_bias_fn, activity_score_fn=activity_score_fn)
