# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

import pandas as pd

from ._strategies.registry import build_screener, normalize_strategy_name
from .config import BotConfig
from .models import Candidate
from .utils import classify_tradingview_market_session, now_et

LOG = logging.getLogger(__name__)

__all__ = ["TradingViewScreenerClient"]


class TradingViewScreenerClient:
    _CANONICAL_SCREEN_FIELDS = {
        "close": {
            "premarket": "premarket_close",
            "postmarket": "postmarket_close",
        },
        "change_from_open": {
            "premarket": "premarket_change_from_open",
            "postmarket": "postmarket_change",
        },
        "volume": {
            "premarket": "premarket_volume",
            "postmarket": "postmarket_volume",
        },
    }

    def __init__(self, config: BotConfig):
        self.config = config
        self.cookies = self._load_cookies()
        self._last_refresh: dict[str, datetime] = {}
        self._cache: dict[str, list[Candidate]] = {}
        self._screeners: dict[str, Any] = {}
        self._run_session_stack: list[str] = []

    def _load_cookies(self):
        if self.config.tradingview.sessionid:
            return {"sessionid": self.config.tradingview.sessionid}

        LOG.warning("TradingView sessionid not being used, screener results may be delayed.")
        return None

    def _build_screener(self, strategy: str):
        strategy = normalize_strategy_name(strategy)
        screener = self._screeners.get(strategy)
        if screener is None:
            screener = build_screener(self, strategy)
            self._screeners[strategy] = screener
        return screener

    def get_candidates(self, strategy: str) -> list[Candidate]:
        strategy = normalize_strategy_name(strategy)
        now = now_et()
        screener = self._build_screener(strategy)
        cached = self._cache.get(strategy)
        last = self._last_refresh.get(strategy)
        override = screener.cached_candidates(now, cached, last)
        if override is not None:
            self._last_refresh[strategy] = now
            self._cache[strategy] = list(override)
            return self._cache[strategy]
        age = (now - last).total_seconds() if last else math.inf
        if age < self.config.tradingview.screener_refresh_seconds and cached is not None:
            return cached
        session = self._active_market_session(now)
        self._run_session_stack.append(session)
        try:
            rows = screener.run()
        finally:
            popped = self._run_session_stack.pop()
            if popped != session:
                LOG.warning("TradingView screener session snapshot stack mismatch: expected %s but popped %s.", session, popped)
        self._last_refresh[strategy] = now
        self._cache[strategy] = rows
        return rows

    def execute(self, query: Any, *, strategy: str | None = None) -> pd.DataFrame:
        return self._execute(query, strategy=strategy)

    def base_query(self, limit: int | None = None):
        return self._base_query(limit)

    def select_fields(self, *fields: str) -> tuple[str, ...]:
        return self._select_fields(*fields, session=self._current_run_session())

    def order_field(self, name: str) -> str:
        return self._order_field(name, session=self._current_run_session())

    def column(self, name: str):
        return self._column(name, session=self._current_run_session())

    def common_equity_conditions(self) -> list:
        return self._common_equity_conditions()

    def liquid_equity_conditions(self, min_price: float = 5.0, max_price: float | None = None):
        return self._liquid_equity_conditions(min_price=min_price, max_price=max_price)

    def small_cap_base_conditions(self, min_price: float = 2.0, max_price: float = 20.0):
        return self._small_cap_base_conditions(min_price=min_price, max_price=max_price)

    def symbol_from_ticker(self, ticker: str) -> str:
        return self._symbol_from_ticker(ticker)

    def row_metadata(self, row: pd.Series) -> dict[str, Any]:
        return self._row_metadata(row, session=self._current_run_session())

    def candidate_rows(self, df: pd.DataFrame, strategy: str, directional_bias_fn=None, activity_score_fn=None) -> list[Candidate]:
        return self._candidate_rows(df, strategy, directional_bias_fn=directional_bias_fn, activity_score_fn=activity_score_fn)

    @classmethod
    def _active_market_session(cls, now: datetime | None = None) -> str:
        ts = now or now_et()
        return classify_tradingview_market_session(ts)

    def _current_run_session(self) -> str | None:
        return self._run_session_stack[-1] if self._run_session_stack else None


    @classmethod
    def _canonical_screen_field(cls, name: str, now: datetime | None = None, session: str | None = None) -> str:
        resolved_session = session or cls._active_market_session(now)
        session_map = cls._CANONICAL_SCREEN_FIELDS.get(str(name), {})
        return str(session_map.get(resolved_session, name))

    @classmethod
    def _preferred_screen_fields(
        cls,
        canonical: str,
        *,
        resolved_session: str | None = None,
        inferred_session: str | None = None,
    ) -> tuple[str, ...]:
        session_fields = cls._CANONICAL_SCREEN_FIELDS.get(str(canonical), {})
        ordered: list[str] = []

        def _add(value: str | None) -> None:
            token = str(value or '').strip()
            if token and token not in ordered:
                ordered.append(token)

        if resolved_session:
            _add(session_fields.get(resolved_session))
        _add(canonical)
        if inferred_session and inferred_session != resolved_session:
            _add(session_fields.get(inferred_session))
        for candidate in session_fields.values():
            _add(candidate)
        return tuple(ordered)

    @classmethod
    def _select_fields(cls, *fields: str, session: str | None = None) -> tuple[str, ...]:
        return tuple(cls._canonical_screen_field(str(field), session=session) for field in fields)

    @classmethod
    def _order_field(cls, name: str, session: str | None = None) -> str:
        return cls._canonical_screen_field(name, session=session)

    @classmethod
    def _normalize_screener_dataframe(cls, df: pd.DataFrame, now: datetime | None = None, session: str | None = None) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        out = df.copy()
        resolved_session = session or cls._active_market_session(now)
        for canonical in cls._CANONICAL_SCREEN_FIELDS:
            chosen_field = next(
                (field for field in cls._preferred_screen_fields(canonical, resolved_session=resolved_session) if field in out.columns),
                None,
            )
            if chosen_field:
                out[canonical] = out[chosen_field]
        return out

    @classmethod
    def _normalize_screen_metadata(cls, metadata: dict[str, Any], now: datetime | None = None, session: str | None = None) -> dict[str, Any]:
        out = dict(metadata)
        inferred_sessions: set[str] = set()
        for session_name in ("premarket", "postmarket"):
            for session_fields in cls._CANONICAL_SCREEN_FIELDS.values():
                field_name = session_fields.get(session_name)
                if field_name and field_name in out:
                    inferred_sessions.add(session_name)
                    break
        inferred_session = next(iter(inferred_sessions)) if len(inferred_sessions) == 1 else None
        resolved_session = session or cls._active_market_session(now)
        if session is None and now is None and inferred_session is not None:
            resolved_session = inferred_session
        out.setdefault("market_session", resolved_session)
        for canonical in cls._CANONICAL_SCREEN_FIELDS:
            chosen_field = next(
                (
                    field
                    for field in cls._preferred_screen_fields(
                        canonical,
                        resolved_session=resolved_session,
                        inferred_session=inferred_session,
                    )
                    if field in out
                ),
                None,
            )
            if chosen_field:
                out[canonical] = out.get(chosen_field)
                out.setdefault(f"session_{canonical}_field", chosen_field)
            else:
                out.setdefault(f"session_{canonical}_field", canonical)
        return out

    def _execute(self, query: Any, *, strategy: str | None = None) -> pd.DataFrame:
        kwargs = {}
        if self.cookies is not None:
            kwargs["cookies"] = self.cookies
        try:
            _, df = query.get_scanner_data(**kwargs)
        except Exception:
            if strategy:
                LOG.exception("TradingView screener query failed for strategy %s.", strategy)
            else:
                LOG.exception("TradingView screener query failed.")
            raise
        return self._normalize_screener_dataframe(df, session=self._current_run_session())

    def _base_query(self, limit: int | None = None):
        from tradingview_screener import Query

        return Query().set_markets(self.config.tradingview.market).limit(limit or self.config.tradingview.max_candidates)

    @classmethod
    def _column(cls, name: str, session: str | None = None):
        from tradingview_screener import Column

        return Column(cls._canonical_screen_field(name, session=session))

    def _common_equity_conditions(self) -> list:
        c = self._column
        return [
            c("type") == "stock",
            c("is_primary") == True,
            c("exchange") != "OTC",
            c("etf_holdings_count").empty(),
            c("expense_ratio").empty(),
            c("description").not_like("%ETF%"),
            c("description").not_like("%Exchange Traded Fund%"),
            c("description").not_like("%Warrant%"),
            c("description").not_like("%Right%"),
            c("description").not_like("%Rights%"),
            c("description").not_like("%Unit%"),
            c("description").not_like("%Units%"),
            c("description").not_like("%Preferred%"),
            c("description").not_like("%Preference%"),
            c("description").not_like("%Depositary Share%"),
        ]

    def _small_cap_base_conditions(self, min_price: float = 2.0, max_price: float = 20.0):
        min_cap = float(self.config.tradingview.min_market_cap)
        max_cap = float(self.config.tradingview.max_market_cap)
        conditions = [
            *self._liquid_equity_conditions(min_price=min_price, max_price=max_price),
            self._column("market_cap_basic").between(min_cap, max_cap),
        ]
        return conditions

    def _liquid_equity_conditions(self, min_price: float = 5.0, max_price: float | None = None):
        min_volume = int(self.config.tradingview.min_volume)
        min_value_traded_1m = float(getattr(self.config.tradingview, "min_value_traded_1m", 0.0) or 0.0)
        min_volume_1m = int(getattr(self.config.tradingview, "min_volume_1m", 0) or 0)
        c = self._column
        conditions = [
            *self._common_equity_conditions(),
            c("close") >= float(min_price),
            c("volume") >= min_volume,
        ]
        if max_price is not None:
            conditions.append(c("close") <= float(max_price))
        if min_value_traded_1m > 0:
            conditions.append(c("Value.Traded|1") >= min_value_traded_1m)
        if min_volume_1m > 0:
            conditions.append(c("volume|1") >= min_volume_1m)
        return conditions

    @staticmethod
    def _symbol_from_ticker(ticker: str) -> str:
        return ticker.split(":", 1)[-1]

    @classmethod
    def _row_metadata(cls, row: pd.Series, session: str | None = None) -> dict[str, Any]:
        return cls._normalize_screen_metadata({str(k): v for k, v in row.to_dict().items()}, session=session)

    def _candidate_rows(self, df: pd.DataFrame, strategy: str, directional_bias_fn=None, activity_score_fn=None) -> list[Candidate]:
        out: list[Candidate] = []
        for ordinal, (_, row) in enumerate(df.iterrows(), start=1):
            symbol = self._symbol_from_ticker(str(row.get("name")))
            directional_bias = directional_bias_fn(row) if directional_bias_fn else None
            raw_activity_score = activity_score_fn(row) if activity_score_fn else ordinal
            try:
                activity_score = float(raw_activity_score)
            except Exception:
                activity_score = float(ordinal)
            if not math.isfinite(activity_score):
                activity_score = 0.0
            metadata = self._row_metadata(row, session=self._current_run_session())
            metadata.setdefault("candidate_query_order", int(ordinal))
            out.append(Candidate(symbol=symbol, strategy=strategy, rank=ordinal, activity_score=activity_score, directional_bias=directional_bias, metadata=metadata))
        def _tiebreak_key(c: Candidate) -> tuple[float, int]:
            qo_raw = c.metadata.get("candidate_query_order")
            if qo_raw is None:
                query_order = 9_999_999
            else:
                try:
                    query_order = int(qo_raw)
                except (TypeError, ValueError):
                    query_order = 9_999_999
            return float(c.activity_score), -query_order

        out.sort(key=_tiebreak_key, reverse=True)
        for rank, candidate in enumerate(out, start=1):
            candidate.rank = rank
        return out
