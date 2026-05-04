# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Literal, TypeVar, overload

import pandas as pd


TLevel = TypeVar("TLevel")


@overload
def pivot_points(
    frame: pd.DataFrame,
    span: int,
    *,
    include_idx: Literal[False] = False,
) -> tuple[list[tuple[pd.Timestamp, float]], list[tuple[pd.Timestamp, float]]]: ...


@overload
def pivot_points(
    frame: pd.DataFrame,
    span: int,
    *,
    include_idx: Literal[True],
) -> tuple[list[tuple[int, pd.Timestamp, float]], list[tuple[int, pd.Timestamp, float]]]: ...


def pivot_points(
    frame: pd.DataFrame,
    span: int,
    *,
    include_idx: bool = False,
):
    """Detect local-extreme pivots over a ``2*span+1`` rolling window.

    Returns ``(highs, lows)`` where each entry is ``(timestamp, price)``
    by default. With ``include_idx=True`` each entry is
    ``(positional_index, timestamp, price)`` — used by callers that need
    to anchor levels to the bar index for downstream age computation.

    The strict-equality + uniqueness check (``hi == max(hi_window) and
    hi_window.count(hi) == 1``) discards plateaus where multiple bars
    share the extreme — only true pivots count, matching how a trader
    visually identifies a high/low. Shared by ``htf_levels._pivot_points``
    and ``support_resistance._pivot_points`` so the pivot-detection
    semantics can't drift between the two builders (the kind of bug we
    debugged through the AMD/INTC support-list mismatch).
    """
    highs: list = []
    lows: list = []
    if frame is None or len(frame) < (span * 2 + 3):
        return highs, lows
    span = max(1, int(span))
    high_col = frame["high"]
    low_col = frame["low"]
    highs_arr = (
        high_col.to_numpy(dtype=float, copy=False).tolist()
        if high_col.dtype != object
        else high_col.astype(float).tolist()
    )
    lows_arr = (
        low_col.to_numpy(dtype=float, copy=False).tolist()
        if low_col.dtype != object
        else low_col.astype(float).tolist()
    )
    idxs = list(frame.index)
    for i in range(span, len(frame) - span):
        hi = highs_arr[i]
        lo = lows_arr[i]
        hi_window = highs_arr[i - span : i + span + 1]
        lo_window = lows_arr[i - span : i + span + 1]
        if hi == max(hi_window) and hi_window.count(hi) == 1:
            highs.append((i, idxs[i], float(hi)) if include_idx else (idxs[i], float(hi)))
        if lo == min(lo_window) and lo_window.count(lo) == 1:
            lows.append((i, idxs[i], float(lo)) if include_idx else (idxs[i], float(lo)))
    return highs, lows


def cluster_levels(
    points: Iterable[tuple[pd.Timestamp, float]],
    kind: str,
    tolerance: float,
    max_levels: int,
    *,
    level_factory: Callable[..., TLevel],
) -> list[TLevel]:
    """Cluster pivot points by price proximity and return ranked ``TLevel``s.

    Time-aware recency scoring: each cluster's score is
    ``effective_touches + persistence_bonus`` where
    ``effective_touches = touches * recency_factor`` and
    ``recency_factor`` decays linearly from 1.0 (cluster's last touch is
    the newest in the pivot set) down to a floor of 0.10 (cluster's last
    touch is at the oldest end). The persistence bonus rewards clusters
    that span a sustained portion of the lookback window. Together they
    keep recent close-to-price levels visible alongside long-standing
    historical bases.

    ``level_factory`` is the dataclass to construct (HTFLevel /
    SupportResistanceLevel — both have the same field shape). This is the
    single source of truth for cluster scoring so a fix landing here
    propagates to every consumer (HTF and LTF SR contexts).
    """
    ordered = sorted([(ts, float(price)) for ts, price in points], key=lambda x: x[1])
    if not ordered:
        return []
    newest_ts = max((ts for ts, _price in ordered), default=None)
    oldest_ts = min((ts for ts, _price in ordered), default=None)
    total_window_seconds = max(
        1.0,
        float((newest_ts - oldest_ts).total_seconds()) if newest_ts is not None and oldest_ts is not None else 1.0,
    )
    groups: list[list[tuple[pd.Timestamp, float]]] = []
    for point in ordered:
        if not groups:
            groups.append([point])
            continue
        prior_prices = [p for _, p in groups[-1]]
        anchor = sum(prior_prices) / len(prior_prices)
        if abs(point[1] - anchor) <= tolerance:
            groups[-1].append(point)
        else:
            groups.append([point])
    levels: list[TLevel] = []
    for grp in groups:
        grp_sorted = sorted(grp, key=lambda x: x[0])
        prices = [price for _, price in grp_sorted]
        touches = len(grp_sorted)
        cluster_first_ts = grp_sorted[0][0]
        cluster_last_ts = grp_sorted[-1][0]
        active_window_seconds = max(0.0, float((cluster_last_ts - cluster_first_ts).total_seconds()))
        recency_factor = 1.0
        if newest_ts is not None and oldest_ts is not None:
            age_seconds = max(0.0, float((newest_ts - cluster_last_ts).total_seconds()))
            recency_factor = max(0.10, 1.0 - (age_seconds / total_window_seconds))
        persistence_factor = min(1.0, active_window_seconds / total_window_seconds)
        effective_touches = float(touches) * recency_factor
        score = effective_touches + 0.50 * persistence_factor
        levels.append(
            level_factory(
                kind=kind,
                price=float(sum(prices) / len(prices)),
                touches=touches,
                score=score,
                first_seen=cluster_first_ts.isoformat() if grp_sorted else None,
                last_seen=cluster_last_ts.isoformat() if grp_sorted else None,
            )
        )
    levels.sort(key=lambda lv: (lv.score, lv.touches), reverse=True)
    return levels[: max(1, int(max_levels))]


def confirm_by_bars(
    frame: pd.DataFrame,
    field: str,
    comparator: str,
    level_price: float,
    count: int,
    eps: float,
) -> bool:
    """Whether the last ``count`` bars all satisfy ``frame[field] cmp level``.

    Used by both flip-confirmation paths (htf_levels and support_resistance)
    to decide whether a level has been broken/reclaimed for ``count``
    consecutive bars on the given field. Accepts both symbolic
    (``">"`` / ``"<"`` / ``">="`` / ``"<="``) and English
    (``"above"`` / ``"below"``) comparators so the htf and sr modules can
    share a single body without rewriting their direction strings.
    """
    if count <= 0 or frame is None or frame.empty or field not in frame.columns:
        return False
    series = frame[field].astype(float).tail(int(count))
    if len(series) < int(count):
        return False
    cmp = str(comparator).strip().lower()
    if cmp in (">", "above"):
        return bool((series > float(level_price) + eps).all())
    if cmp in ("<", "below"):
        return bool((series < float(level_price) - eps).all())
    if cmp == ">=":
        return bool((series >= float(level_price) + eps).all())
    if cmp == "<=":
        return bool((series <= float(level_price) - eps).all())
    return False


def clone_level(
    level: TLevel,
    kind: str,
    *,
    level_factory: Callable[..., TLevel],
    source: str | None = None,
) -> TLevel:
    """Re-emit ``level`` as ``kind`` via ``level_factory``.

    When ``source`` is ``None`` (default) the cloned level inherits the
    original level's ``source``; pass an explicit ``source`` to relabel
    (e.g. ``"broken_htf_resistance"``). Replaces the per-module
    ``_clone_level`` helpers that previously duplicated the same body
    across htf_levels and support_resistance.
    """
    inherited_source = str(getattr(level, "source", "pivot") or "pivot")
    return level_factory(
        kind=kind,
        price=float(level.price),
        touches=int(level.touches),
        score=float(level.score),
        first_seen=getattr(level, "first_seen", None),
        last_seen=getattr(level, "last_seen", None),
        source=str(source if source is not None else inherited_source),
        source_priority=float(getattr(level, "source_priority", 1.0) or 1.0),
    )


def extend_unique_levels(dest: list, additions: list) -> None:
    """Append unseen levels from ``additions`` to ``dest`` in place.

    Uniqueness key is ``(source, round(price, 8), kind)``. Pure duck-typing
    on the level attributes; no factory required. Replaces the per-module
    ``_extend_unique_levels`` helpers that previously duplicated the same
    body across htf_levels and support_resistance.
    """
    seen = {
        (
            str(getattr(level, "source", "pivot") or "pivot"),
            round(float(level.price), 8),
            str(getattr(level, "kind", "support") or "support"),
        )
        for level in dest
    }
    for level in additions:
        key = (
            str(getattr(level, "source", "pivot") or "pivot"),
            round(float(level.price), 8),
            str(getattr(level, "kind", "support") or "support"),
        )
        if key in seen:
            continue
        dest.append(level)
        seen.add(key)


def frame_extreme_side_levels(
    frame: pd.DataFrame,
    *,
    side: str,
    tolerance: float,
    max_levels: int,
    level_factory: Callable[..., TLevel],
) -> list[TLevel]:
    """Build a single-cluster fallback level from the frame's extreme bar.

    Returns the cluster_levels output from a single ``(timestamp, price)``
    point — used by both modules as a last-resort reference when
    pivot detection and prior-day/week fallbacks all return empty.
    """
    if frame is None or frame.empty:
        return []
    if str(side).strip().lower() == "support":
        pos = int(frame["low"].astype(float).values.argmin())
        point = (pd.Timestamp(frame.index[pos]), float(frame["low"].iloc[pos]))
        return cluster_levels([point], "support", tolerance, max_levels, level_factory=level_factory)
    pos = int(frame["high"].astype(float).values.argmax())
    point = (pd.Timestamp(frame.index[pos]), float(frame["high"].iloc[pos]))
    return cluster_levels([point], "resistance", tolerance, max_levels, level_factory=level_factory)


def cluster_levels_by_tolerance(levels: list[TLevel], tolerance: float) -> list[list[TLevel]]:
    """Group ``levels`` into price-proximity clusters.

    Each cluster's anchor is its running mean — a level joins the prior
    cluster when ``abs(level.price - anchor) <= tolerance``, otherwise it
    starts a new one. Pure grouping with no per-cluster reduction; the
    caller chooses whether to pick a representative (htf_levels) or
    merge attributes (support_resistance). Replaces the duplicated
    grouping loop in both modules' ``_collapse_same_side_levels``.
    """
    if not levels:
        return []
    ordered = sorted(levels, key=lambda lv: float(lv.price))
    tol = max(float(tolerance), 1e-9)
    groups: list[list[TLevel]] = []
    for level in ordered:
        if not groups:
            groups.append([level])
            continue
        prior_prices = [float(item.price) for item in groups[-1]]
        anchor = sum(prior_prices) / len(prior_prices)
        if abs(float(level.price) - anchor) <= tol:
            groups[-1].append(level)
        else:
            groups.append([level])
    return groups


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
