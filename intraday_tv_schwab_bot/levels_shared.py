# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Literal, overload

import numpy as np
import pandas as pd

from .sessions import session_datetime_index, session_mask, session_segment_ids

if TYPE_CHECKING:
    from .config import SupportResistanceConfig
    from .support_resistance import SupportResistanceContext


@dataclass(slots=True)
class Level:
    """A support or resistance level from either level builder
    (``htf_levels.build_htf_context``,
    ``support_resistance.build_support_resistance_context``): a cluster of
    pivots (``cluster_levels``), a prior-day/week high or low
    (``fallback_prior_side_levels``), or one of those re-emitted as the other
    kind (``clone_level``). Until 2026-09-27 each builder had its own class
    with these eight fields, HTFLevel and SupportResistanceLevel, and every
    helper here took the one to build as a ``level_factory`` argument."""

    kind: str
    price: float
    touches: int = 1
    score: float = 1.0
    first_seen: str | None = None
    last_seen: str | None = None
    source: str = "pivot"
    source_priority: float = 1.0


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
    back to an older pivot as the reference low. Shared by every level
    builder (htf_levels, support_resistance, technical_levels, order_blocks)
    so the pivot-detection semantics can't drift between them (the kind of
    bug we debugged through the AMD/INTC support-list mismatch).

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


def reduce_pivots(
    highs: Iterable[tuple[int, pd.Timestamp, float]],
    lows: Iterable[tuple[int, pd.Timestamp, float]],
    *,
    min_gap_bars: int = 0,
) -> list[tuple[str, int, pd.Timestamp, float]]:
    """Merge ``pivot_points(..., include_idx=True)`` highs and lows into one
    alternating swing sequence of ``(kind, pos, ts, price)``, ``kind`` "H" or
    "L", in bar order.

    A run of same-kind pivots keeps its extreme (the later one on a tie). An
    alternating pivot closer than ``min_gap_bars`` to the prior kept pivot is
    noise within the current leg (2026-05-27, Fix B): it is skipped so the leg
    continues instead of registering a 1-2-bar swing that churns the
    structure labels. ``min_gap_bars <= 0`` keeps every alternation.

    Market structure (``support_resistance.analyze_market_structure``) passes
    the gap; the technical levels' swings (``technical_levels``) keep every
    alternation. Each module carried its own copy of this loop.
    """
    raw: list[tuple[str, int, pd.Timestamp, float]] = []
    for pos, ts, price in highs:
        raw.append(("H", int(pos), ts, float(price)))
    for pos, ts, price in lows:
        raw.append(("L", int(pos), ts, float(price)))
    raw.sort(key=lambda item: item[1])
    reduced: list[tuple[str, int, pd.Timestamp, float]] = []
    for kind, pos, ts, price in raw:
        if not reduced:
            reduced.append((kind, pos, ts, price))
            continue
        prev_kind, prev_pos, _prev_ts, prev_price = reduced[-1]
        if kind != prev_kind:
            if min_gap_bars > 0 and (pos - prev_pos) < min_gap_bars:
                continue
            reduced.append((kind, pos, ts, price))
            continue
        keep_current = price >= prev_price if kind == "H" else price <= prev_price
        if keep_current:
            reduced[-1] = (kind, pos, ts, price)
    return reduced


def cluster_levels(
    points: Iterable[tuple[pd.Timestamp, float]],
    kind: str,
    tolerance: float,
) -> list[Level]:
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

    This is the single source of truth for cluster scoring, so a fix
    landing here reaches both builders (HTF and LTF SR contexts). A copy
    local to support_resistance once used an additive recency bonus
    (``touches * 1.15 + 0.60 * recency_factor``), which let ancient
    high-touch bases outrank close-to-price recent swings (the AMD/INTC
    debug).

    No count cap. Until 2026-09-23 the builders kept the top
    ``2 * max_levels_per_side`` clusters by score here, BEFORE knowing which
    side of price each one sits on: a fresh single-touch swing scores 1.0 and
    lost to any older multi-touch cluster, so after a gap or trend day the
    level nearest price was cut and every survivor sat on the far side
    (AVGO 2026-05-28: nearest resistance 430.40 at 1.1-1.4 ATR instead of
    427.96 at 0.0-0.6 ATR, flipping the long clearance gate). The builders
    now keep the nearest ``max_levels_per_side`` per side after the side
    split (``collapse_same_side_levels``).
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
    levels: list[Level] = []
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
            Level(
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
    return confirm_by_values(confirm_tail(frame, field, count), comparator, level_price, eps)


def confirm_tail(frame: pd.DataFrame | None, field: str, count: int) -> np.ndarray | None:
    """The values ``confirm_by_bars(frame, field, ..., count, ...)`` tests:
    the last ``count`` of ``frame[field]`` as float64, or None where it
    confirms nothing (no count, no frame, no column, too few bars). A caller
    testing many levels against one frame takes it once."""
    if count <= 0 or frame is None or frame.empty or field not in frame.columns:
        return None
    values = frame[field].to_numpy(dtype=np.float64)
    if len(values) < int(count):
        return None
    return values[-int(count):]


def confirm_by_values(values: np.ndarray | None, comparator: str, level_price: float, eps: float) -> bool:
    """``confirm_by_bars`` on the tail ``confirm_tail`` took."""
    if values is None:
        return False
    cmp = str(comparator).strip().lower()
    if cmp in (">", "above"):
        return bool((values > float(level_price) + eps).all())
    if cmp in ("<", "below"):
        return bool((values < float(level_price) - eps).all())
    if cmp == ">=":
        return bool((values >= float(level_price) + eps).all())
    if cmp == "<=":
        return bool((values <= float(level_price) - eps).all())
    return False


def clone_level(
    level: Level,
    kind: str,
    *,
    source: str | None = None,
) -> Level:
    """Re-emit ``level`` as ``kind``.

    When ``source`` is ``None`` (default) the cloned level inherits the
    original level's ``source``; pass an explicit ``source`` to relabel
    (e.g. ``"broken_htf_resistance"``). Replaces the per-module
    ``_clone_level`` helpers that previously duplicated the same body
    across htf_levels and support_resistance.
    """
    inherited_source = str(getattr(level, "source", "pivot") or "pivot")
    return Level(
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
) -> list[Level]:
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
        return cluster_levels([point], "support", tolerance)
    pos = int(frame["high"].astype(float).values.argmax())
    point = (pd.Timestamp(frame.index[pos]), float(frame["high"].iloc[pos]))
    return cluster_levels([point], "resistance", tolerance)


def cluster_levels_by_tolerance(levels: list[Level], tolerance: float) -> list[list[Level]]:
    """Group ``levels`` into price-proximity clusters.

    Each cluster's anchor is its running mean — a level joins the prior
    cluster when ``abs(level.price - anchor) <= tolerance``, otherwise it
    starts a new one. Pure grouping with no per-cluster reduction; the
    caller chooses whether to pick a representative (htf_levels) or
    merge attributes (support_resistance) through
    ``collapse_same_side_levels``' ``reduce``.
    """
    if not levels:
        return []
    ordered = sorted(levels, key=lambda lv: float(lv.price))
    tol = max(float(tolerance), 1e-9)
    groups: list[list[Level]] = []
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


# The ladder steps both S/R builders run (htf_levels.build_htf_context and
# support_resistance.build_support_resistance_context): side assignment by
# flip, the partition against price, the per-side collapse, the confirmed
# flips and the pending levels. Where the builders differ on purpose, the
# difference is an argument each passes at its call site: the cluster
# reducer (``reduce``), the flipped levels' ``source`` (``relabel``) and the
# broken-level gate (``gate_tol``). Only support_resistance re-drops the
# ladder near its confirmed flips (``_reconcile_flipped_levels``).

# ``check(level_price, direction)``: is the level's flip confirmed?
# ``direction`` is "loss" (a support price has broken below) or "reclaim"
# (a resistance price has been taken back). Each builder makes its own from
# its confirmation bars.
FlipCheck = Callable[[float, str], bool]


def collapse_same_side_levels(
    levels: list[Level],
    tolerance: float,
    current_price: float,
    *,
    reverse: bool,
    max_levels: int | None,
    reduce: Callable[[list[Level], float], Level],
) -> list[Level]:
    """One level per price cluster (``cluster_levels_by_tolerance``), sorted
    by price (descending with ``reverse``, so a support ladder reads nearest
    first) and cut to ``max_levels`` (at least one). ``max_levels=None``
    keeps every rung: support_resistance cuts its ladder only after it drops
    the rungs near its confirmed flips (``_reconcile_flipped_levels``).

    ``reduce(group, current_price)`` makes a cluster's level. htf_levels
    keeps one representative (highest source priority, then score);
    support_resistance merges the cluster (sums touches and score, adds a
    cross-source bonus). The split is deliberate, so each builder passes its
    own and the reducers stay in their modules.
    """
    selected = [reduce(group, current_price) for group in cluster_levels_by_tolerance(levels, tolerance)]
    selected.sort(key=lambda lv: float(lv.price), reverse=bool(reverse))
    if max_levels is None:
        return selected
    return selected[: max(1, int(max_levels))]


def drop_levels_near_price(levels: list[Level], price: float, *, tolerance: float) -> list[Level]:
    """``levels`` without the ones within ``tolerance`` (at least 1e-9) of
    ``price``."""
    tol = max(float(tolerance), 1e-9)
    return [level for level in levels if abs(float(level.price) - float(price)) > tol]


def partition_levels_by_side(
    levels: list[Level],
    current_price: float,
    *,
    side: str,
) -> tuple[list[Level], list[Level]]:
    """Split ``levels`` into those on their side of ``current_price`` --
    supports at or below it (nearest first), resistances at or above it, a
    level within 1e-6 of the price counting as at it -- and those beyond it.
    The builders report the nearest of the latter whose flip is unconfirmed
    as ``pending_support`` / ``pending_resistance`` (``pending_level``)."""
    eps = max(abs(float(current_price)) * 1e-6, 1e-8)
    if side == "support":
        on_side = [lv for lv in levels if float(lv.price) <= float(current_price) + eps]
        on_side.sort(key=lambda lv: lv.price, reverse=True)
        beyond = [lv for lv in levels if float(lv.price) > float(current_price) + eps]
        return on_side, beyond
    on_side = [lv for lv in levels if float(lv.price) >= float(current_price) - eps]
    on_side.sort(key=lambda lv: lv.price)
    beyond = [lv for lv in levels if float(lv.price) < float(current_price) - eps]
    return on_side, beyond


def pending_level(
    beyond: list[Level],
    *,
    side: str,
    close: float,
    broken: Level | None,
    tolerance: float,
    reduce: Callable[[list[Level], float], Level],
) -> Level | None:
    """The nearest level of ``side`` that price has crossed while its flip is
    unconfirmed -- a support now above ``close`` or a resistance now below it.

    ``beyond`` holds the candidates the partition against ``close`` dropped.
    A level within ``tolerance`` of the confirmed flip on that side
    (``broken``) is the same zone, already flipped, so it is not pending.
    ``reduce`` is the builder's cluster reducer (``collapse_same_side_levels``).
    """
    crossed = beyond if broken is None else drop_levels_near_price(beyond, float(broken.price), tolerance=tolerance)
    if not crossed:
        return None
    # Nearest to price first: crossed supports ascend from just above price,
    # crossed resistances descend from just below it.
    return collapse_same_side_levels(
        crossed,
        tolerance,
        close,
        reverse=(side != "support"),
        max_levels=1,
        reduce=reduce,
    )[0]


def split_references_by_flip(
    *,
    support_references: list[Level],
    resistance_references: list[Level],
    flip_check: FlipCheck,
    relabel: tuple[str, str] | None,
) -> tuple[list[Level], list[Level]]:
    """Side-assign reference levels: ``(support_candidates,
    resistance_candidates)``, each a ``clone_level`` copy. A support reference
    whose flip is confirmed lost becomes a resistance candidate, a reclaimed
    resistance reference a support candidate; the rest keep their side.

    ``relabel`` is ``(lost_support_source, reclaimed_resistance_source)`` for
    the flipped copies, or None to keep each reference's ``source``.
    htf_levels relabels (``broken_htf_support`` / ``broken_htf_resistance``:
    the peer strategies read those sources back as the level's role);
    support_resistance keeps the source.
    """
    lost_source, reclaimed_source = relabel if relabel is not None else (None, None)
    support_candidates: list[Level] = []
    resistance_candidates: list[Level] = []
    for level in support_references:
        if flip_check(float(level.price), "loss"):
            resistance_candidates.append(clone_level(level, "resistance", source=lost_source))
        else:
            support_candidates.append(clone_level(level, "support"))
    for level in resistance_references:
        if flip_check(float(level.price), "reclaim"):
            support_candidates.append(clone_level(level, "support", source=reclaimed_source))
        else:
            resistance_candidates.append(clone_level(level, "resistance"))
    return support_candidates, resistance_candidates


def detect_broken_levels(
    *,
    support_references: list[Level],
    resistance_references: list[Level],
    flip_check: FlipCheck,
    close: float,
    gate_tol: float,
    tolerance: float,
    relabel: tuple[str, str] | None,
    reduce: Callable[[list[Level], float], Level],
) -> tuple[Level | None, Level | None]:
    """The confirmed flips nearest price: ``(broken_support,
    broken_resistance)``, each None when there is none.

    ``broken_resistance`` comes from the resistance references whose flip is
    confirmed reclaimed, at or below ``close + gate_tol``; ``broken_support``
    from the lost support references at or above ``close - gate_tol``. Each
    pool collapses like a ladder side (``tolerance``, ``reduce``) and keeps
    the level nearest price. The flipped copies take ``relabel`` as in
    ``split_references_by_flip``.

    ``gate_tol`` is the builder's choice: htf_levels allows float noise (1e-6
    of ``close``), support_resistance its clustering tolerance. Both gate
    against ``close``, the price the ladders are partitioned against: until
    2026-09-23 support_resistance used the fallback reference price whenever
    a side had fallen back to prior levels, so a confirmed-lost pivot support
    between ``close`` and that reference showed as a flipped resistance in
    the ladder with broken_support None.
    """
    lost_source, reclaimed_source = relabel if relabel is not None else (None, None)
    reclaimed = [
        clone_level(level, "support", source=reclaimed_source)
        for level in resistance_references
        if float(level.price) <= close + gate_tol and flip_check(float(level.price), "reclaim")
    ]
    lost = [
        clone_level(level, "resistance", source=lost_source)
        for level in support_references
        if float(level.price) >= close - gate_tol and flip_check(float(level.price), "loss")
    ]
    broken_resistance = collapse_same_side_levels(reclaimed, tolerance, close, reverse=True, max_levels=1, reduce=reduce)
    broken_support = collapse_same_side_levels(lost, tolerance, close, reverse=False, max_levels=1, reduce=reduce)
    return (
        broken_support[0] if broken_support else None,
        broken_resistance[0] if broken_resistance else None,
    )


def _build_special_level(
    kind: str,
    price: float,
    *,
    source: str,
    source_priority: float,
    score: float | None = None,
) -> Level:
    level_score = float(score if score is not None else source_priority)
    return Level(
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
) -> list[Level]:
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
        _build_special_level(
            side,
            price,
            source=source,
            source_priority=source_priority,
            score=score,
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


def side_tolerance(
    atr: float,
    price: float,
    *,
    atr_tolerance_mult: float,
    pct_tolerance: float,
    min_gap_atr_mult: float,
    min_gap_pct: float,
) -> float:
    """How far apart two same-side levels must sit to stay two levels: the
    larger of the merge tolerance (``atr * atr_tolerance_mult``, or ``price *
    pct_tolerance`` when larger) and the same-side minimum gap
    (``same_side_min_gap_threshold``). The S/R and HTF builds collapse their
    ladders at it (the S/R context publishes it as ``side_tolerance``), and
    ``effective_side_tolerance`` spaces the sr_flip target and the
    dashboard's S/R ladder with it."""
    merge_tolerance = max(atr * atr_tolerance_mult, price * pct_tolerance)
    return max(
        merge_tolerance,
        same_side_min_gap_threshold(atr, price, min_gap_atr_mult=min_gap_atr_mult, min_gap_pct=min_gap_pct),
    )


def effective_side_tolerance(
    sr_cfg: SupportResistanceConfig,
    price: float,
    *,
    atr: float = 0.0,
    sr_ctx: SupportResistanceContext | None = None,
) -> float:
    """The S/R context's ``side_tolerance`` when it has one (above 0; an empty
    context carries 0), else ``side_tolerance`` at ``price`` and ``atr`` from
    ``sr_cfg``'s four tolerances (checked at load, above 0). An ``atr`` of 0
    leaves the ATR arms out: the dashboard's ladder has no ATR to give.

    Until 2026-09-27 this was ``_sr_ladder._sr_effective_side_tolerance``,
    which wrote the formula out again with 1e-4 floors of its own (they bound
    only below about $0.03), read a None price or ATR as 0, and fell back to
    the config behind a broad except when the context's tolerance did not
    read."""
    if sr_ctx is not None and sr_ctx.side_tolerance > 0:
        return sr_ctx.side_tolerance
    return side_tolerance(
        atr,
        price,
        atr_tolerance_mult=float(sr_cfg.atr_tolerance_mult),
        pct_tolerance=float(sr_cfg.pct_tolerance),
        min_gap_atr_mult=float(sr_cfg.same_side_min_gap_atr_mult),
        min_gap_pct=float(sr_cfg.same_side_min_gap_pct),
    )


def select_next_distinct_level(
    levels: list[Level] | None,
    anchor_price: float | None,
    *,
    above: bool,
    minimum_gap: float,
) -> Level | None:
    """The first of ``levels`` (nearest first) more than ``minimum_gap``
    above ``anchor_price`` (below it with ``above=False``), skipping a level
    whose price is not positive; the first level when there is no anchor.
    sr_flip management takes the next target past a flipped level with it."""
    if not levels:
        return None
    if anchor_price is None:
        return levels[0]
    tol = max(float(minimum_gap), 1e-6)
    anchor = float(anchor_price)
    for level in levels:
        price = float(level.price)
        if price <= 0:
            continue
        if above:
            if price > anchor + tol:
                return level
        else:
            if price < anchor - tol:
                return level
    return None


def collapse_price_ladder(values: list[float], *, reverse: bool, min_gap: float) -> list[float]:
    """The positive ``values`` in order (descending with ``reverse``), each
    kept only when it sits more than ``min_gap`` from the last one kept: the
    dashboard's S/R ladder rungs."""
    ordered = sorted((float(v) for v in values if float(v) > 0), reverse=reverse)
    collapsed: list[float] = []
    for value in ordered:
        if not collapsed or abs(float(value) - float(collapsed[-1])) > max(float(min_gap), 1e-4):
            collapsed.append(float(value))
    return collapsed


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
        eligible &= session_mask(session_index, "rth")
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
    eligible = session_mask(session_index, "rth") & np.asarray(weeks < pd.Period(as_of, freq="W-FRI"), dtype=bool)
    if not eligible.any():
        return None, None
    last_week = weeks[eligible].max()
    bars = frame.loc[eligible & np.asarray(weeks == last_week, dtype=bool)]
    return float(bars["high"].max()), float(bars["low"].min())
