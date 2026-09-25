# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Literal, TypeVar, overload

import numpy as np
import pandas as pd

from .utils import (
    EQUITY_EARLY_CLOSE,
    EQUITY_RTH_CLOSE,
    EQUITY_RTH_OPEN,
    is_weekday_session_day,
    us_equity_early_close_days,
)


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

    A bar is a pivot when it holds its window's extreme and no bar in the
    LEFT half of the window ties it: a run of bars that share the extreme
    (a flat top, a two-bar bottom) is ONE pivot, at its first bar. Until
    2026-09-22 a tie anywhere in the window disqualified every bar in it,
    so a swing whose extreme printed twice registered no pivot at all --
    4.4% of swing highs/lows on archived 5m RTH bars (180 of ~4,130 over
    138 symbol-days), exact penny ties that are routine at a round number.
    A V-bottom that tied lost its low entirely and market structure fell
    back to an older pivot as the reference low. Shared by
    ``htf_levels._pivot_points`` and ``support_resistance._pivot_points``
    so the pivot-detection semantics can't drift between the two builders
    (the kind of bug we debugged through the AMD/INTC support-list
    mismatch).

    The whole window must lie in ONE ET session (``session_segment_ids``).
    The frames hold no 20:00-07:00 bars, so until 2026-09-23 a 19:45 bar was
    "confirmed" as a swing high by the next morning's 07:00/07:15 bars, eleven
    hours of unobserved trading later -- QCOM's 195.88 post-market print on
    2026-09-21 became the HTF resistance, the HH structure reference and a
    LONG's target that way. A bar at a session edge has no observed
    neighbours on one side, so it is never a pivot.
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
    segments = session_segment_ids(frame.index)
    for i in range(span, len(frame) - span):
        if segments[i - span] != segments[i + span]:
            continue
        hi = highs_arr[i]
        lo = lows_arr[i]
        hi_window = highs_arr[i - span : i + span + 1]
        lo_window = lows_arr[i - span : i + span + 1]
        if hi == max(hi_window) and hi not in hi_window[:span]:
            highs.append((i, idxs[i], float(hi)) if include_idx else (idxs[i], float(hi)))
        if lo == min(lo_window) and lo not in lo_window[:span]:
            lows.append((i, idxs[i], float(lo)) if include_idx else (idxs[i], float(lo)))
    return highs, lows


def cluster_levels(
    points: Iterable[tuple[pd.Timestamp, float]],
    kind: str,
    tolerance: float,
    *,
    level_factory: Callable[..., TLevel],
) -> list[TLevel]:
    """Cluster pivot points by price proximity and return EVERY cluster,
    ranked by score.

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

    No count cap. Until 2026-09-23 the builders kept the top
    ``2 * max_levels_per_side`` clusters by score here, BEFORE knowing which
    side of price each one sits on: a fresh single-touch swing scores 1.0 and
    lost to any older multi-touch cluster, so after a gap or trend day the
    level nearest price was cut and every survivor sat on the far side
    (AVGO 2026-05-28: nearest resistance 430.40 at 1.1-1.4 ATR instead of
    427.96 at 0.0-0.6 ATR, flipping the long clearance gate). The builders
    now keep the nearest ``max_levels_per_side`` per side after the side
    split (``_collapse_same_side_levels``).
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
    return levels


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


def extend_unique_levels(dest: list, additions: list) -> list:
    """Append unseen levels from ``additions`` to ``dest`` in place, and
    return the ones appended.

    Uniqueness key is ``(source, round(price, 8), kind)``. Pure duck-typing
    on the level attributes; no factory required. Replaces the per-module
    ``_extend_unique_levels`` helpers that previously duplicated the same
    body across htf_levels and support_resistance.
    """
    appended: list = []
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
        appended.append(level)
        seen.add(key)
    return appended


def frame_extreme_side_levels(
    frame: pd.DataFrame,
    *,
    side: str,
    tolerance: float,
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
        return cluster_levels([point], "support", tolerance, level_factory=level_factory)
    pos = int(frame["high"].astype(float).values.argmax())
    point = (pd.Timestamp(frame.index[pos]), float(frame["high"].iloc[pos]))
    return cluster_levels([point], "resistance", tolerance, level_factory=level_factory)


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


def session_segment_ids(index: pd.Index) -> np.ndarray:
    """Run id per bar that advances at every change of ET session date.

    The bar frames hold only the 07:00-20:00 ET stream window, so two
    neighbouring bars on different ET dates are separated by trading nobody
    observed. Detectors that compare neighbouring bars -- pivots,
    fair-value-gap triplets, order blocks -- require their window to lie
    inside one run.

    The boundary is the ET date, not a time step. Within a session a thin
    name routinely prints no bar for minutes, and a minute without a trade is
    not missing data: nothing traded. Archived 1m bars (2026-05..09) show
    2-13% of pre/post-market steps longer than 2 minutes and same-day steps
    up to 209 minutes, so a "step > 2 x timeframe" rule would have dropped
    real extended-hours pivots. Every step across an ET date is at least
    11 hours (19:59 -> 07:00).
    """
    if len(index) == 0:
        return np.zeros(0, dtype=np.int64)
    days = session_datetime_index(index).normalize().to_numpy()
    changes = np.concatenate(([0], (days[1:] != days[:-1]).astype(np.int64)))
    return np.cumsum(changes)


def latest_session_date(now: datetime) -> date:
    """The ET date of ``now``, rolled back to the latest trading day on or
    before it (a Saturday resolves to Friday, a holiday Monday to Friday).

    The builders pass this as ``as_of`` to ``prior_day_levels`` /
    ``prior_week_levels`` when the caller gives none."""
    stamp = pd.Timestamp(now)
    day = (stamp.tz_convert(_SESSION_TZ) if stamp.tzinfo is not None else stamp).date()
    while not is_weekday_session_day(day):
        day -= timedelta(days=1)
    return day


def _rth_bar_mask(session_index: pd.DatetimeIndex) -> np.ndarray:
    """Bars that START inside a trading day's regular session: 09:30 up to
    16:00 ET, or 13:00 on an early-close day. ``session_index`` is already
    ET-local (``session_datetime_index``)."""
    if len(session_index) == 0:
        return np.zeros(0, dtype=bool)
    days = session_index.normalize()
    close_minute_by_day: dict[pd.Timestamp, int] = {}
    for day in days.unique():
        session_day = day.date()
        if not is_weekday_session_day(session_day):
            close_minute_by_day[day] = -1
            continue
        close = EQUITY_EARLY_CLOSE if session_day in us_equity_early_close_days(session_day.year) else EQUITY_RTH_CLOSE
        close_minute_by_day[day] = close.hour * 60 + close.minute
    close_minutes = np.asarray(days.map(close_minute_by_day), dtype=np.int64)
    minutes = np.asarray(session_index.hour * 60 + session_index.minute, dtype=np.int64)
    open_minute = EQUITY_RTH_OPEN.hour * 60 + EQUITY_RTH_OPEN.minute
    return (minutes >= open_minute) & (minutes < close_minutes)


def prior_day_levels(frame: pd.DataFrame, as_of: date, *, regular_session_only: bool = True) -> tuple[float | None, float | None]:
    """High/low of the regular session of the last trading day before ``as_of``
    (of all its bars with ``regular_session_only=False``: microcap_pm_breakout
    floors its premarket high on the prior day's post-market too).

    Two changes on 2026-09-23. (1) The session day is the caller's ``as_of``
    (the builders use the clock's session date), not the date of the frame's
    last bar. The frame never holds the most recent night, so one fetched
    before a symbol's first print of the day ended at yesterday's 19:45 bar:
    "today" became yesterday and PDH/PDL came from two sessions back -- wrong
    on 53 of 53 archived symbol-days in that state, median PDH error 1.37%,
    max 15.4% (ARM 2026-09-22: 276.50 instead of 327.02). (2) Only 09:30-16:00
    bars count. The ET calendar-date bucket took extended hours and whatever
    overnight prints the fetch happened to hold (PDH sat above the RTH high on
    25 of 81 symbol-days, PDL below the RTH low on 28), so the level depended
    on restart timing and was not the conventional PDH/PDL the dashboard labels.
    """
    if frame is None or frame.empty:
        return None, None
    session_index = session_datetime_index(frame.index)
    days = session_index.normalize()
    eligible = np.asarray(days < pd.Timestamp(as_of), dtype=bool)
    if regular_session_only:
        eligible &= _rth_bar_mask(session_index)
    if not eligible.any():
        return None, None
    last_day = days[eligible].max()
    bars = frame.loc[eligible & np.asarray(days == last_day, dtype=bool)]
    return float(bars["high"].max()), float(bars["low"].min())


def prior_week_levels(frame: pd.DataFrame, as_of: date) -> tuple[float | None, float | None]:
    """High/low of the regular sessions of the last W-FRI week before the one
    holding ``as_of``. Same two rules as ``prior_day_levels``: a Monday
    premarket frame ends on Friday, so keying the week off the last bar made
    "prior week" two weeks back until the first Monday print, and extended /
    overnight bars no longer count. Weeks are ET-bucketed (see
    ``session_datetime_index``)."""
    if frame is None or frame.empty:
        return None, None
    session_index = session_datetime_index(frame.index)
    weeks = session_index.to_period("W-FRI")
    eligible = _rth_bar_mask(session_index) & np.asarray(weeks < pd.Period(as_of, freq="W-FRI"), dtype=bool)
    if not eligible.any():
        return None, None
    last_week = weeks[eligible].max()
    bars = frame.loc[eligible & np.asarray(weeks == last_week, dtype=bool)]
    return float(bars["high"].max()), float(bars["low"].min())


# ---------------------------------------------------------------------------
# Divergence detection (shared by technical_levels.py + htf_levels.py)
#
# A divergence is a misalignment between a price pivot pair and the
# corresponding indicator (RSI, OBV) pivot values:
#
#   regular bullish: at pivot lows, price prints LL (price2 < price1 by
#       price_move_frac) but indicator prints HL (ind2 > ind1 + delta).
#       Reversal-likely from a downtrend.
#
#   regular bearish: at pivot highs, price prints HH but indicator prints
#       LH. Reversal-likely from an uptrend.
#
#   hidden bullish: at pivot lows, price prints HL but indicator prints
#       LL. Continuation in an uptrend.
#
#   hidden bearish: at pivot highs, price prints LH but indicator prints
#       HH. Continuation in a downtrend.
#
# ``b`` is always the most recent pivot (and must be within
# ``max_age_bars``, counted on the caller's bar clock -- session bars while
# session indicators are on and the clock is inside the session); ``a`` is
# the nearest earlier pivot, within ``pivot_lookback``, that satisfies the
# price half of the pattern, and a pivot in between that contradicts it
# means there is no divergence. See
# ``find_divergence`` for why it is the nearest and not merely any.
# ---------------------------------------------------------------------------


_DivergencePoint = tuple[int, "pd.Timestamp", float]


class DivergenceMatch:
    """A confirmed divergence between two pivot points and a momentum
    indicator series.

    Stored as a small immutable record so consumers (engine score
    adjustments, dashboard chart overlay, shared entry candidate builder)
    can read pivot positions, indicator values, and freshness without
    re-deriving them. ``__slots__`` keeps memory footprint tight when many
    contexts hold these — typically 8 fields per TechnicalLevelsContext.

    ``age_bars`` is how many bars have closed since ``b``, counted on the
    ``bar_clock`` ``find_divergence`` was given. The builders pass the
    session-bar clock while session indicators are on and the clock is
    inside the session (``utils.indicator_session_open``), so it is SESSION
    bars there: the overnight between yesterday's last pivot and today's
    open is not part of the age. Without a clock (session indicators off, or
    a reader outside the session) it is every bar. The pivot positions are
    frame positions either way.
    """

    __slots__ = (
        "kind",
        "direction",
        "indicator",
        "pivot_a_pos",
        "pivot_a_ts",
        "pivot_a_price",
        "pivot_a_indicator",
        "pivot_b_pos",
        "pivot_b_ts",
        "pivot_b_price",
        "pivot_b_indicator",
        "indicator_delta",
        "age_bars",
    )

    kind: str
    direction: str
    indicator: str
    pivot_a_pos: int
    pivot_a_ts: pd.Timestamp
    pivot_a_price: float
    pivot_a_indicator: float
    pivot_b_pos: int
    pivot_b_ts: pd.Timestamp
    pivot_b_price: float
    pivot_b_indicator: float
    indicator_delta: float
    age_bars: int

    def __init__(
        self,
        *,
        kind: str,
        direction: str,
        indicator: str,
        pivot_a_pos: int,
        pivot_a_ts: pd.Timestamp,
        pivot_a_price: float,
        pivot_a_indicator: float,
        pivot_b_pos: int,
        pivot_b_ts: pd.Timestamp,
        pivot_b_price: float,
        pivot_b_indicator: float,
        indicator_delta: float,
        age_bars: int,
    ) -> None:
        self.kind = str(kind)
        self.direction = str(direction)
        self.indicator = str(indicator)
        self.pivot_a_pos = int(pivot_a_pos)
        self.pivot_a_ts = pivot_a_ts
        self.pivot_a_price = float(pivot_a_price)
        self.pivot_a_indicator = float(pivot_a_indicator)
        self.pivot_b_pos = int(pivot_b_pos)
        self.pivot_b_ts = pivot_b_ts
        self.pivot_b_price = float(pivot_b_price)
        self.pivot_b_indicator = float(pivot_b_indicator)
        self.indicator_delta = float(indicator_delta)
        self.age_bars = int(age_bars)

    def __bool__(self) -> bool:
        # All DivergenceMatch instances are truthy. Lets existing callers
        # that test ``bool(getattr(ctx, "bullish_rsi_divergence", False))``
        # keep working — None is falsy, a match is truthy.
        return True

    def __repr__(self) -> str:
        return (
            f"DivergenceMatch({self.kind} {self.direction} {self.indicator} "
            f"a@{self.pivot_a_ts}={self.pivot_a_price:.4f}/{self.pivot_a_indicator:.4f} "
            f"b@{self.pivot_b_ts}={self.pivot_b_price:.4f}/{self.pivot_b_indicator:.4f} "
            f"age={self.age_bars}b delta={self.indicator_delta:.4f})"
        )

    def to_payload(self) -> dict[str, object]:
        """Render to a JSON-friendly dict for chart / metadata export."""
        return {
            "kind": self.kind,
            "direction": self.direction,
            "indicator": self.indicator,
            "pivot_a": {
                "pos": int(self.pivot_a_pos),
                "ts": str(self.pivot_a_ts),
                "price": float(self.pivot_a_price),
                "indicator": float(self.pivot_a_indicator),
            },
            "pivot_b": {
                "pos": int(self.pivot_b_pos),
                "ts": str(self.pivot_b_ts),
                "price": float(self.pivot_b_price),
                "indicator": float(self.pivot_b_indicator),
            },
            "indicator_delta": float(self.indicator_delta),
            "age_bars": int(self.age_bars),
        }


def _pivot_indicator_value(indicator: pd.Series, pos: int) -> float | None:
    if pos < 0 or pos >= len(indicator):
        return None
    value = indicator.iloc[pos]
    if pd.isna(value):
        return None
    return float(value)


def _b_above_a(kind: str, direction: str) -> bool:
    """Does the pattern need the latest pivot ``b`` ABOVE the earlier ``a``?
    Regular bearish (HH) and hidden bullish (HL) do; regular bullish (LL) and
    hidden bearish (LH) need it below."""
    return (kind == "regular") == (direction == "bearish")


def _price_condition(*, kind: str, direction: str, price_a: float, price_b: float,
                     price_move_frac: float) -> bool:
    """The price half of ``_qualifies`` -- does ``b`` make the swing the
    pattern needs against ``a``, by at least ``price_move_frac``?"""
    if _b_above_a(kind, direction):
        return price_b > price_a * (1.0 + price_move_frac)
    return price_b < price_a * (1.0 - price_move_frac)


def _contradicts(*, kind: str, direction: str, price_a: float, price_b: float) -> bool:
    """Is ``a`` on the wrong side of ``b`` for this pattern -- so that, against
    the swing ``a`` marks, ``b`` made the OPPOSITE move to the one claimed?

    Regular bullish: a lower low than ``b`` (``b`` is not the extreme).
    Hidden bullish: a higher low than ``b`` (``b`` broke it -- not a higher
    low). The bearish two mirror them. A pivot on the right side of ``b`` but
    inside ``price_move_frac`` is neither: it is noise, and skipped."""
    if _b_above_a(kind, direction):
        return price_a > price_b
    return price_a < price_b


def _qualifies(
    *,
    kind: str,
    direction: str,
    price_a: float,
    price_b: float,
    ind_a: float,
    ind_b: float,
    price_move_frac: float,
    indicator_delta: float,
) -> tuple[bool, float]:
    """Return ``(matched, |ind_b - ind_a|)`` for the requested pattern.

    Pattern matrix (all four cases reduce to the same shape):

      regular bullish (lows):  price_b < price_a*(1-frac), ind_b > ind_a + delta
      regular bearish (highs): price_b > price_a*(1+frac), ind_b < ind_a - delta
      hidden bullish  (lows):  price_b > price_a*(1+frac), ind_b < ind_a - delta
      hidden bearish  (highs): price_b < price_a*(1-frac), ind_b > ind_a + delta
    """
    if kind == "regular" and direction == "bullish":
        price_ok = price_b < price_a * (1.0 - price_move_frac)
        ind_ok = ind_b > ind_a + indicator_delta
    elif kind == "regular" and direction == "bearish":
        price_ok = price_b > price_a * (1.0 + price_move_frac)
        ind_ok = ind_b < ind_a - indicator_delta
    elif kind == "hidden" and direction == "bullish":
        price_ok = price_b > price_a * (1.0 + price_move_frac)
        ind_ok = ind_b < ind_a - indicator_delta
    elif kind == "hidden" and direction == "bearish":
        price_ok = price_b < price_a * (1.0 - price_move_frac)
        ind_ok = ind_b > ind_a + indicator_delta
    else:
        return False, 0.0
    return bool(price_ok and ind_ok), abs(ind_b - ind_a)


def find_divergence(
    points: list[_DivergencePoint],
    indicator: pd.Series,
    *,
    kind: str,
    direction: str,
    indicator_name: str,
    price_move_frac: float,
    indicator_delta: float,
    pivot_lookback: int,
    max_age_bars: int,
    last_bar_pos: int,
    price_scale: np.ndarray | None = None,
    bar_clock: np.ndarray | None,
) -> DivergenceMatch | None:
    """Divergence between the latest swing and the swing it is measured
    against, or None.

    ``bar_clock`` (one value per bar of the frame the pivot positions index,
    non-decreasing) is what ``b``'s age is counted on: ``clock[last_bar_pos]
    - clock[pos_b]``. None counts every bar (``last_bar_pos - pos_b``). It
    has no default, so no caller falls back to the all-bar age by leaving it
    out. The builders pass ``np.cumsum`` of the indicator session mask while
    session indicators are on and the clock is inside the session
    (``utils.indicator_session_open``), so the age is session bars. Until
    2026-09-24 it was always every bar: with pivots paired only on session
    bars, the post- and pre-market bars aged yesterday's last session pivot
    past the limit overnight. On the divergence-age study's symbol-days with
    a dense overnight tape (447 over 27 sessions on 60m, 554 over 29 on 15m)
    the 60m HTF divergence read on none of the minutes from 09:30 to 11:00;
    on session-bar age it reads on 23.6% of RTH minutes instead of 10.4%
    (15m: 16.4% instead of 14.6%).

    ``price_scale`` (``utils.session_price_scale`` of the frame the pivot
    positions index) puts the pivot prices on the scale the indicator was
    computed on before they are compared; the match still reports the raw
    prices. The session rsi14 / obv are stitched across the overnight gap,
    so compared raw a gap alone made a "higher high" the RSI never saw: 371
    of 571 cross-session HTF RSI divergences over 10 archived sessions were
    the gap (2026-09-23).

    ``points`` is a list of ``(pos, ts, price)`` tuples -- pivot lows for
    bullish patterns, pivot highs for bearish ones.

    ``b`` is the MOST RECENT pivot, and it must be within ``max_age_bars``.
    A newer pivot that does not diverge supersedes an older one that did:
    reporting the older one would describe a state the tape has moved past.

    ``a`` is the NEAREST earlier pivot (within ``pivot_lookback``) that
    satisfies the price half of the pattern, and only that pair has its
    indicator tested. Pivots nearer than it that miss the minimum price move
    are skipped as noise. A pivot on the WRONG side of ``b`` ends the scan
    with no divergence (``_contradicts``): for regular divergence ``b`` is then
    not the extreme of the swing, and for hidden divergence ``b`` broke the
    swing it would be a higher low (or lower high) against. The regular half
    of that rule landed first; hidden divergence kept skipping such pivots,
    so lows 95 -> 103 -> 101 were reported as a hidden bullish divergence
    95 -> 101 across the 103 swing that 101 broke.

    Until 2026-09-22 this paired ``b`` with the OLDEST qualifying pivot in
    the window. Lows at 100 (RSI 20), 96 (RSI 35), 95 (RSI 30) were reported
    as a bullish divergence 100 -> 95, stepping over the 96 swing at which
    RSI had in fact CONFIRMED the new low (30 < 35). Divergence is a claim
    about the swing price just made against the one before it; an older
    pivot is only a valid reference when nothing in between contradicts it.

    Returns None when no pair qualifies or the indicator is missing at
    either pivot.
    """
    if not points or len(points) < 2:
        return None
    pivot_lookback = max(2, int(pivot_lookback))
    max_age_bars = max(0, int(max_age_bars))
    candidates = points[-pivot_lookback:]
    if len(candidates) < 2:
        return None
    def _on_scale(pos: int, price: float) -> float:
        return float(price) * (float(price_scale[int(pos)]) if price_scale is not None else 1.0)

    pos_b, ts_b, price_b = candidates[-1]
    if bar_clock is None:
        age = max(0, int(last_bar_pos) - int(pos_b))
    else:
        age = max(0, int(bar_clock[int(last_bar_pos)]) - int(bar_clock[int(pos_b)]))
    if age > max_age_bars:
        return None
    ind_b = _pivot_indicator_value(indicator, pos_b)
    if ind_b is None:
        return None
    cmp_b = _on_scale(pos_b, price_b)
    for pos_a, ts_a, price_a in reversed(candidates[:-1]):
        cmp_a = _on_scale(pos_a, price_a)
        if _contradicts(kind=kind, direction=direction, price_a=cmp_a, price_b=cmp_b):
            return None
        if not _price_condition(kind=kind, direction=direction, price_a=cmp_a,
                                price_b=cmp_b, price_move_frac=float(price_move_frac)):
            continue
        ind_a = _pivot_indicator_value(indicator, pos_a)
        if ind_a is None:
            return None
        matched, delta = _qualifies(
            kind=kind,
            direction=direction,
            price_a=cmp_a,
            price_b=cmp_b,
            ind_a=ind_a,
            ind_b=ind_b,
            price_move_frac=float(price_move_frac),
            indicator_delta=float(indicator_delta),
        )
        if not matched:
            return None
        return DivergenceMatch(
            kind=kind,
            direction=direction,
            indicator=indicator_name,
            pivot_a_pos=int(pos_a),
            pivot_a_ts=ts_a,
            pivot_a_price=float(price_a),
            pivot_a_indicator=float(ind_a),
            pivot_b_pos=int(pos_b),
            pivot_b_ts=ts_b,
            pivot_b_price=float(price_b),
            pivot_b_indicator=float(ind_b),
            indicator_delta=float(delta),
            age_bars=int(age),
        )
    return None
