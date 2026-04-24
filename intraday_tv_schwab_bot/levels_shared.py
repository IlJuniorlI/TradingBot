# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import pandas as pd


TLevel = TypeVar("TLevel")


def build_special_level(
    kind: str,
    price: float,
    *,
    source: str,
    source_priority: float,
    level_factory: Callable[..., TLevel],
    score: float | None = None,
) -> TLevel:
    level_score = float(score if score is not None else source_priority)
    return level_factory(
        kind=kind,
        price=float(price),
        touches=1,
        score=level_score,
        source=str(source),
        source_priority=float(source_priority),
    )


def fallback_prior_side_levels(
    *,
    side: str,
    current_price: float,
    include_prior_day: bool,
    include_prior_week: bool,
    prior_day_high: float | None,
    prior_day_low: float | None,
    prior_week_high: float | None,
    prior_week_low: float | None,
    level_factory: Callable[..., TLevel],
) -> list[TLevel]:
    candidates: list[tuple[str, float, float, float]] = []
    if include_prior_day and prior_day_low is not None:
        candidates.append(("prior_day_low", float(prior_day_low), 2.0, 2.0))
    if include_prior_day and prior_day_high is not None:
        candidates.append(("prior_day_high", float(prior_day_high), 2.0, 2.0))
    if include_prior_week and prior_week_low is not None:
        candidates.append(("prior_week_low", float(prior_week_low), 3.0, 2.5))
    if include_prior_week and prior_week_high is not None:
        candidates.append(("prior_week_high", float(prior_week_high), 3.0, 2.5))
    if not candidates:
        return []
    eps = max(abs(float(current_price or 0.0)) * 1e-6, 1e-8)
    if str(side).strip().lower() == "support":
        filtered = [item for item in candidates if float(item[1]) < float(current_price) - eps]
        filtered.sort(key=lambda item: float(item[1]), reverse=True)
    else:
        filtered = [item for item in candidates if float(item[1]) > float(current_price) + eps]
        filtered.sort(key=lambda item: float(item[1]))
    return [
        build_special_level(
            side,
            price,
            source=source,
            source_priority=source_priority,
            score=score,
            level_factory=level_factory,
        )
        for source, price, source_priority, score in filtered
    ]


def same_side_min_gap_threshold(
    atr: float,
    current_price: float,
    *,
    min_gap_atr_mult: float,
    min_gap_pct: float,
) -> float:
    atr_component = max(0.0, float(atr or 0.0)) * max(0.0, float(min_gap_atr_mult or 0.0))
    pct_component = abs(float(current_price or 0.0)) * max(0.0, float(min_gap_pct or 0.0))
    return max(atr_component, pct_component, 0.0)


def safe_reference_price_for_fallback(
    frame: pd.DataFrame,
    current_price: float | None,
    *,
    atr: float,
    max_drift_atr_mult: float,
    max_drift_pct: float,
) -> float:
    if frame is None or frame.empty:
        return float(current_price or 0.0)
    last_close = float(frame.iloc[-1].get("close", 0.0) or 0.0)
    live_price = float(current_price if current_price is not None else last_close)
    if last_close <= 0.0 or live_price <= 0.0:
        return live_price if live_price > 0.0 else last_close
    max_drift = max(
        max(0.0, float(atr or 0.0)) * max(0.0, float(max_drift_atr_mult or 0.0)),
        abs(last_close) * max(0.0, float(max_drift_pct or 0.0)),
        1e-8,
    )
    return last_close if abs(live_price - last_close) > max_drift else live_price


_SESSION_TZ = "America/New_York"


def datetime_index(index: pd.Index) -> pd.DatetimeIndex:
    if isinstance(index, pd.DatetimeIndex):
        return index
    return pd.DatetimeIndex(index)


def session_datetime_index(index: pd.Index) -> pd.DatetimeIndex:
    """ET-session-localized DatetimeIndex.

    Converts to ``America/New_York`` and then strips tz so that ``.date`` and
    ``.to_period('W-FRI')`` bucket bars by the ET trading day/week. Required
    for prior-day / prior-week computation because a plain UTC-date bucketing
    would misclassify, e.g., a Mon 7:00 PM ET post-market bar during EST as
    belonging to Tuesday (because 7 PM ET EST = 00:00 UTC the next day).

    A tz-naive input is returned unchanged (assumed to already be ET-local).
    """
    dt_index = datetime_index(index)
    if dt_index.tz is None:
        return dt_index
    return dt_index.tz_convert(_SESSION_TZ).tz_localize(None)


def session_dates(index: pd.Index) -> pd.Index:
    """Return ET session dates for each timestamp in ``index``."""
    return pd.Index(session_datetime_index(index).date)


def loc_frame(frame: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    result = frame.loc[mask]
    if isinstance(result, pd.DataFrame):
        return result
    return pd.DataFrame(columns=frame.columns)


def prior_day_levels(frame: pd.DataFrame) -> tuple[float | None, float | None]:
    if frame is None or frame.empty:
        return None, None
    df = frame.copy()
    # Classify bars by ET session date, not UTC date — see session_dates().
    day_key: pd.Series = pd.Series(session_dates(df.index), index=df.index)
    current_day = day_key.iloc[-1]
    prior = loc_frame(df, day_key < current_day)
    if prior.empty:
        return None, None
    prior_day_key: pd.Series = pd.Series(session_dates(prior.index), index=prior.index)
    last_day = prior_day_key.iloc[-1]
    prior_day = loc_frame(prior, prior_day_key == last_day)
    if prior_day.empty:
        return None, None
    return float(prior_day["high"].max()), float(prior_day["low"].min())


def prior_week_levels(frame: pd.DataFrame) -> tuple[float | None, float | None]:
    if frame is None or frame.empty:
        return None, None
    # Bucket by ET session week; see session_datetime_index() for why UTC
    # bucketing misclassifies Fri post-market bars during EST.
    session_index = session_datetime_index(frame.index)
    week_key: pd.Series = pd.Series(session_index.to_period("W-FRI"), index=frame.index)
    current_week = week_key.iloc[-1]
    prior = loc_frame(frame, week_key < current_week)
    if prior.empty:
        return None, None
    prior_week = week_key.loc[week_key < current_week].iloc[-1]
    prior_frame = loc_frame(frame, week_key == prior_week)
    if prior_frame.empty:
        return None, None
    return float(prior_frame["high"].max()), float(prior_frame["low"].min())
