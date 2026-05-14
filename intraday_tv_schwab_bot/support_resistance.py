# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Iterable

import pandas as pd

from .levels_shared import (
    clone_level,
    cluster_levels,
    cluster_levels_by_tolerance,
    confirm_by_bars,
    extend_unique_levels,
    fallback_prior_side_levels,
    frame_extreme_side_levels as _frame_extreme_side_levels_shared,
    pivot_points,
    prior_day_levels as _prior_day_levels,
    prior_week_levels as _prior_week_levels,
    safe_reference_price_for_fallback as _safe_reference_price_for_fallback,
    same_side_min_gap_threshold as _same_side_min_gap_threshold,
)
from .utils import (
    atr_value,
    ensure_ohlcv_frame,
    ensure_standard_indicator_frame,
    now_et,
    resample_bars,
    resolve_current_price,
)


LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class SupportResistanceLevel:
    kind: str
    price: float
    touches: int
    score: float
    first_seen: str | None = None
    last_seen: str | None = None
    source: str = "pivot"
    source_priority: float = 1.0


@dataclass(slots=True)
class MarketStructureContext:
    current_price: float
    reference_high: float | None = None
    reference_low: float | None = None
    last_high_label: str | None = None
    last_low_label: str | None = None
    last_pivot_kind: str | None = None
    last_pivot_label: str | None = None
    pivot_bias: str = "neutral"
    bias: str = "neutral"
    bos_up: bool = False
    bos_down: bool = False
    choch_up: bool = False
    choch_down: bool = False
    bos_up_age_bars: int | None = None
    bos_down_age_bars: int | None = None
    choch_up_age_bars: int | None = None
    choch_down_age_bars: int | None = None
    eqh: bool = False
    eql: bool = False
    structure_age_bars: int | None = None
    event_age_bars: int | None = None
    pivot_count: int = 0
    # Spread between reference_high and reference_low expressed in ATR units.
    # 0.0 when one of the reference pivots is missing. Surfaced for visibility
    # so post-session analysis can correlate exit outcomes with structure
    # range tightness.
    structure_range_atr: float = 0.0
    # True when both EQH and EQL flags are set AND the H-L spread is below
    # ``structure_min_range_atr_mult``. Signals that bias-derived structure
    # exits should be suppressed (already enforced in _resolve_structure_bias
    # which returns "neutral" when this is True). Range / vol_squeeze
    # entries can still inspect the eqh/eql flags directly.
    tight_structure_range: bool = False
    reason: str = "insufficient_pivots"


@dataclass(slots=True)
class SupportResistanceContext:
    current_price: float
    timeframe_minutes: int = 15
    supports: list[SupportResistanceLevel] = field(default_factory=list)
    resistances: list[SupportResistanceLevel] = field(default_factory=list)
    nearest_support: SupportResistanceLevel | None = None
    nearest_resistance: SupportResistanceLevel | None = None
    broken_resistance: SupportResistanceLevel | None = None
    broken_support: SupportResistanceLevel | None = None
    prior_day_high: float | None = None
    prior_day_low: float | None = None
    prior_week_high: float | None = None
    prior_week_low: float | None = None
    support_distance_pct: float | None = None
    resistance_distance_pct: float | None = None
    support_distance_atr: float | None = None
    resistance_distance_atr: float | None = None
    current_atr: float = 0.0
    same_side_min_gap: float = 0.0
    side_tolerance: float = 0.0
    level_buffer: float = 0.0
    breakout_above_resistance: bool = False
    breakdown_below_support: bool = False
    near_support: bool = False
    near_resistance: bool = False
    bias_score: float = 0.0
    regime_hint: str = "neutral"
    market_structure: MarketStructureContext = field(default_factory=lambda: MarketStructureContext(current_price=0.0))


def empty_market_structure_context(current_price: float = 0.0) -> MarketStructureContext:
    return MarketStructureContext(current_price=float(current_price or 0.0))


def empty_support_resistance_context(current_price: float = 0.0, *, timeframe_minutes: int = 15) -> SupportResistanceContext:
    return SupportResistanceContext(
        current_price=float(current_price or 0.0),
        timeframe_minutes=int(timeframe_minutes or 15),
        market_structure=empty_market_structure_context(current_price),
    )

def _pivot_points(frame: pd.DataFrame, span: int) -> tuple[list[tuple[int, pd.Timestamp, float]], list[tuple[int, pd.Timestamp, float]]]:
    # Thin wrapper around the shared `pivot_points` helper. Uses
    # include_idx=True so structure_event detection downstream can
    # anchor a pivot to its bar position. htf_levels uses include_idx=False.
    return pivot_points(frame, span, include_idx=True)


def _cluster_levels(points: Iterable[tuple[pd.Timestamp, float]], kind: str, tolerance: float, max_levels: int) -> list[SupportResistanceLevel]:
    # Thin wrapper around the shared `cluster_levels` helper — same
    # multiplicative-recency formula as htf_levels so the two builders
    # can't drift in their scoring. The previous module-local version
    # used an additive recency bonus (touches * 1.15 + 0.60 *
    # recency_factor) which let ancient high-touch bases dominate
    # close-to-price recent swings — the bug we found in the AMD/INTC
    # debug. Effective touches via multiplication keeps the rank
    # ordering stable while letting recency genuinely weight selection.
    return cluster_levels(points, kind, tolerance, max_levels, level_factory=SupportResistanceLevel)


def _reduced_pivots(frame: pd.DataFrame, span: int) -> list[tuple[str, int, pd.Timestamp, float]]:
    highs, lows = _pivot_points(frame, span)
    if frame is None or frame.empty:
        return []
    # _pivot_points now returns (pos, ts, price) tuples so we no longer need
    # to rebuild a pos_by_ts dict via iterating frame.index (which is very
    # slow for DatetimeIndex — 36ms/100 calls in the profile).
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
        prev_kind, _, _, prev_price = reduced[-1]
        if kind != prev_kind:
            reduced.append((kind, pos, ts, price))
            continue
        keep_current = price >= prev_price if kind == "H" else price <= prev_price
        if keep_current:
            reduced[-1] = (kind, pos, ts, price)
    return reduced


def _classify_high(current_price: float, prior_price: float | None, tolerance: float) -> str | None:
    if prior_price is None:
        return None
    if current_price > prior_price + tolerance:
        return "HH"
    if current_price < prior_price - tolerance:
        return "LH"
    return "EQH"


def _classify_low(current_price: float, prior_price: float | None, tolerance: float) -> str | None:
    if prior_price is None:
        return None
    if current_price > prior_price + tolerance:
        return "HL"
    if current_price < prior_price - tolerance:
        return "LL"
    return "EQL"


def _structure_bias_from_labels(last_high_label: str | None, last_low_label: str | None) -> str:
    if last_high_label == "HH" and last_low_label == "HL":
        return "bullish"
    if last_low_label == "HL" and last_high_label == "HH":
        return "bullish"
    if last_low_label == "LL" and last_high_label == "LH":
        return "bearish"
    if last_high_label == "LH" and last_low_label == "LL":
        return "bearish"
    return "neutral"


def _structure_event_active(age_bars: int | None, max_event_age_bars: int | None) -> bool:
    if age_bars is None:
        return False
    if max_event_age_bars is None:
        return True
    return int(age_bars) <= max(0, int(max_event_age_bars))


def _resolve_structure_bias(
    *,
    pivot_bias: str,
    close: float,
    reference_high: float | None,
    reference_low: float | None,
    breakout_buffer: float,
    eq_tol: float,
    bos_up_age: int | None,
    bos_down_age: int | None,
    max_event_age_bars: int | None,
    tight_structure_range: bool = False,
) -> str:
    if reference_high is not None and close >= float(reference_high) + breakout_buffer:
        return "bullish"
    if reference_low is not None and close <= float(reference_low) - breakout_buffer:
        return "bearish"

    # Tight EQH+EQL consolidation suppresses the midpoint / pivot / recent-
    # event bias paths — a 0.3-ATR range produces noise-driven bias flips
    # (single bar can swing bias bearish→bullish). Genuine BoS through the
    # reference high/low above already returned bullish/bearish, so we still
    # catch real breakouts. CHoCH is computed in analyze_market_structure
    # from bos_up/down + pivot_bias, also unaffected.
    if tight_structure_range:
        return "neutral"

    midpoint_bias = "neutral"
    if reference_high is not None and reference_low is not None and float(reference_high) > float(reference_low):
        midpoint = (float(reference_high) + float(reference_low)) / 2.0
        midpoint_buffer = max(eq_tol, breakout_buffer * 0.35)
        if close >= midpoint + midpoint_buffer:
            midpoint_bias = "bullish"
        elif close <= midpoint - midpoint_buffer:
            midpoint_bias = "bearish"

    recent_event_bias = "neutral"
    active_bos_up_age = bos_up_age if _structure_event_active(bos_up_age, max_event_age_bars) else None
    active_bos_down_age = bos_down_age if _structure_event_active(bos_down_age, max_event_age_bars) else None
    if active_bos_up_age is not None or active_bos_down_age is not None:
        if active_bos_up_age is None:
            recent_event_bias = "bearish"
        elif active_bos_down_age is None:
            recent_event_bias = "bullish"
        elif active_bos_up_age < active_bos_down_age:
            recent_event_bias = "bullish"
        elif active_bos_down_age < active_bos_up_age:
            recent_event_bias = "bearish"

    if midpoint_bias != "neutral":
        return midpoint_bias
    if recent_event_bias != "neutral":
        return recent_event_bias
    return pivot_bias
def _last_cross_age(series: list[float], threshold: float, direction: str) -> int | None:
    if len(series) < 2:
        return None
    if direction == "above":
        if series[-1] <= threshold:
            return None
        for idx in range(len(series) - 1, 0, -1):
            if series[idx] > threshold >= series[idx - 1]:
                return len(series) - 1 - idx
        return len(series) - 1 if series[0] > threshold else None
    if series[-1] >= threshold:
        return None
    for idx in range(len(series) - 1, 0, -1):
        if series[idx] < threshold <= series[idx - 1]:
            return len(series) - 1 - idx
    return len(series) - 1 if series[0] < threshold else None


def analyze_market_structure(
    frame: pd.DataFrame,
    *,
    current_price: float | None = None,
    pivot_span: int = 2,
    eq_atr_mult: float = 0.25,
    pct_tolerance: float = 0.0030,
    breakout_atr_mult: float = 0.35,
    breakout_buffer_pct: float = 0.0015,
    structure_event_max_age_bars: int | None = 6,
    min_range_atr_mult: float = 1.5,
) -> MarketStructureContext:
    frame = ensure_standard_indicator_frame(frame)
    if frame.empty:
        return empty_market_structure_context(float(current_price or 0.0))
    close = resolve_current_price(frame, current_price)
    atr = atr_value(frame)
    eq_tol = max(atr * float(eq_atr_mult), close * float(pct_tolerance))
    pivots = _reduced_pivots(frame, int(pivot_span))
    if not pivots:
        return MarketStructureContext(current_price=close, reason="no_confirmed_pivots")

    last_high_label: str | None = None
    last_low_label: str | None = None
    last_high_pos: int | None = None
    last_low_pos: int | None = None
    last_pivot_kind: str | None = None
    last_pivot_label: str | None = None
    last_pivot_pos: int | None = None
    reference_high: float | None = None
    reference_low: float | None = None
    prior_high: float | None = None
    prior_low: float | None = None

    for kind, pos, _ts, price in pivots:
        if kind == "H":
            label = _classify_high(price, prior_high, eq_tol)
            prior_high = price
            if label is not None:
                reference_high = price
                last_high_label = label
                last_high_pos = pos
                last_pivot_kind = kind
                last_pivot_label = label
                last_pivot_pos = pos
        else:
            label = _classify_low(price, prior_low, eq_tol)
            prior_low = price
            if label is not None:
                reference_low = price
                last_low_label = label
                last_low_pos = pos
                last_pivot_kind = kind
                last_pivot_label = label
                last_pivot_pos = pos

    pivot_bias = _structure_bias_from_labels(last_high_label, last_low_label)
    breakout_buffer = max(atr * float(breakout_atr_mult), close * float(breakout_buffer_pct))
    closes = frame["close"].astype(float).tolist()
    bos_up_age = _last_cross_age(closes, float(reference_high) + breakout_buffer, "above") if reference_high is not None else None
    bos_down_age = _last_cross_age(closes, float(reference_low) - breakout_buffer, "below") if reference_low is not None else None
    bos_up = _structure_event_active(bos_up_age, structure_event_max_age_bars)
    bos_down = _structure_event_active(bos_down_age, structure_event_max_age_bars)
    choch_up = bool(bos_up and pivot_bias == "bearish")
    choch_down = bool(bos_down and pivot_bias == "bullish")

    # Tight EQH+EQL consolidation detector (2026-05-14). When both EQH and
    # EQL flags are set, check the spread between the most recent reference
    # high and low. If it's below ``min_range_atr_mult`` ATR, the pivot
    # range is too tight to produce meaningful structure-derived bias —
    # midpoint and pivot-bias signals become noise (a single bar can flip
    # bias bearish→bullish within the consolidation). When tight, the bias
    # resolver short-circuits to "neutral" so structure_bearish_exit /
    # structure_bullish_exit don't fire. EQH/EQL labels remain on the
    # context so range-regime entries (which key on those labels) still
    # see them.
    structure_range = 0.0
    if reference_high is not None and reference_low is not None:
        structure_range = float(reference_high) - float(reference_low)
    structure_range_atr = (structure_range / atr) if (atr > 0.0 and structure_range > 0.0) else 0.0
    tight_structure_range = bool(
        last_high_label == "EQH"
        and last_low_label == "EQL"
        and reference_high is not None
        and reference_low is not None
        and atr > 0.0
        and min_range_atr_mult > 0.0
        and structure_range_atr < float(min_range_atr_mult)
    )

    bias = _resolve_structure_bias(
        pivot_bias=pivot_bias,
        close=close,
        reference_high=reference_high,
        reference_low=reference_low,
        breakout_buffer=breakout_buffer,
        eq_tol=eq_tol,
        bos_up_age=bos_up_age,
        bos_down_age=bos_down_age,
        max_event_age_bars=structure_event_max_age_bars,
        tight_structure_range=tight_structure_range,
    )

    event_ages = [
        age
        for age in (bos_up_age if bos_up else None, bos_down_age if bos_down else None)
        if age is not None
    ]
    last_positions = [pos for pos in (last_high_pos, last_low_pos, last_pivot_pos) if pos is not None]
    structure_age = (len(frame) - 1 - max(last_positions)) if last_positions else None

    return MarketStructureContext(
        current_price=close,
        reference_high=float(reference_high) if reference_high is not None else None,
        reference_low=float(reference_low) if reference_low is not None else None,
        last_high_label=last_high_label,
        last_low_label=last_low_label,
        last_pivot_kind=last_pivot_kind,
        last_pivot_label=last_pivot_label,
        pivot_bias=pivot_bias,
        bias=bias,
        bos_up=bos_up,
        bos_down=bos_down,
        choch_up=choch_up,
        choch_down=choch_down,
        bos_up_age_bars=bos_up_age,
        bos_down_age_bars=bos_down_age,
        choch_up_age_bars=bos_up_age if choch_up else None,
        choch_down_age_bars=bos_down_age if choch_down else None,
        eqh=last_high_label == "EQH",
        eql=last_low_label == "EQL",
        structure_age_bars=structure_age,
        event_age_bars=min(event_ages) if event_ages else None,
        pivot_count=len(pivots),
        structure_range_atr=float(structure_range_atr),
        tight_structure_range=bool(tight_structure_range),
        reason="ok" if (last_high_label is not None or last_low_label is not None) else "insufficient_pivots",
    )


def _clone_level(level: SupportResistanceLevel, kind: str) -> SupportResistanceLevel:
    # Thin factory-binding wrapper around the shared `clone_level` helper.
    # SR always preserves the original `source` field (HTF allows override
    # via a `source=` kwarg, but SR's flip semantics never relabel).
    return clone_level(level, kind, level_factory=SupportResistanceLevel)


def _frame_extreme_side_levels(
    frame: pd.DataFrame,
    *,
    side: str,
    tolerance: float,
    max_levels: int,
) -> list[SupportResistanceLevel]:
    # Thin factory-binding wrapper around `frame_extreme_side_levels`.
    return _frame_extreme_side_levels_shared(
        frame,
        side=side,
        tolerance=tolerance,
        max_levels=max_levels,
        level_factory=SupportResistanceLevel,
    )


def _filter_levels_by_side(
    levels: list[SupportResistanceLevel],
    current_price: float,
    *,
    side: str,
) -> list[SupportResistanceLevel]:
    if not levels:
        return []
    eps = max(abs(float(current_price)) * 1e-6, 1e-8)
    if side == "support":
        filtered = [lv for lv in levels if float(lv.price) <= float(current_price) + eps]
        filtered.sort(key=lambda lv: lv.price, reverse=True)
        return filtered
    filtered = [lv for lv in levels if float(lv.price) >= float(current_price) - eps]
    filtered.sort(key=lambda lv: lv.price)
    return filtered



def _fallback_prior_side_levels(
    *,
    side: str,
    current_price: float,
    include_prior_day: bool,
    include_prior_week: bool,
    prior_day_high: float | None,
    prior_day_low: float | None,
    prior_week_high: float | None,
    prior_week_low: float | None,
) -> list[SupportResistanceLevel]:
    return fallback_prior_side_levels(
        side=side,
        current_price=current_price,
        include_prior_day=include_prior_day,
        include_prior_week=include_prior_week,
        prior_day_high=prior_day_high,
        prior_day_low=prior_day_low,
        prior_week_high=prior_week_high,
        prior_week_low=prior_week_low,
        level_factory=SupportResistanceLevel,
    )


def _level_preference(level: SupportResistanceLevel, current_price: float) -> tuple[float, int, float, float]:
    blended_strength = float(level.score) + (0.20 * max(0.0, float(getattr(level, "source_priority", 1.0) or 1.0) - 1.0))
    return (
        blended_strength,
        int(level.touches),
        float(getattr(level, "source_priority", 1.0) or 1.0),
        -abs(float(level.price) - float(current_price)),
    )


def _merge_level_group(group: list[SupportResistanceLevel], current_price: float) -> SupportResistanceLevel:
    if not group:
        raise ValueError("group must not be empty")
    representative = max(group, key=lambda lv: _level_preference(lv, current_price))
    merged_touches = sum(max(1, int(level.touches)) for level in group)
    merged_score = sum(max(0.0, float(level.score)) for level in group)
    distinct_sources = {
        str(getattr(level, "source", "pivot") or "pivot")
        for level in group
    }
    merged_score += 0.30 * max(0, len(distinct_sources) - 1)
    first_seen_candidates = [str(level.first_seen) for level in group if getattr(level, "first_seen", None)]
    last_seen_candidates = [str(level.last_seen) for level in group if getattr(level, "last_seen", None)]
    return SupportResistanceLevel(
        kind=str(getattr(representative, "kind", "support") or "support"),
        price=float(representative.price),
        touches=int(merged_touches),
        score=float(merged_score),
        first_seen=min(first_seen_candidates) if first_seen_candidates else getattr(representative, "first_seen", None),
        last_seen=max(last_seen_candidates) if last_seen_candidates else getattr(representative, "last_seen", None),
        source=str(getattr(representative, "source", "pivot") or "pivot"),
        source_priority=max(float(getattr(level, "source_priority", 1.0) or 1.0) for level in group),
    )



def _collapse_same_side_levels(
    levels: list[SupportResistanceLevel],
    tolerance: float,
    current_price: float,
    *,
    reverse: bool,
    max_levels: int,
) -> list[SupportResistanceLevel]:
    # SR merges every level in a cluster (sums touches, sums score, adds
    # cross-source bonus). htf_levels uses the same tolerance grouping
    # but picks a single representative — that's why the grouping lives
    # in levels_shared and the per-cluster reducer stays local.
    groups = cluster_levels_by_tolerance(levels, tolerance)
    if not groups:
        return []
    selected = [_merge_level_group(group, current_price) for group in groups]
    selected.sort(key=lambda lv: float(lv.price), reverse=bool(reverse))
    return selected[: max(1, int(max_levels))]


def _drop_levels_near_price(
    levels: list[SupportResistanceLevel],
    target_price: float | None,
    *,
    tolerance: float,
) -> list[SupportResistanceLevel]:
    if not levels or target_price is None:
        return list(levels)
    tol = max(float(tolerance), 1e-9)
    target = float(target_price)
    return [level for level in levels if abs(float(level.price) - target) > tol]


def _reconcile_flipped_levels(
    supports: list[SupportResistanceLevel],
    resistances: list[SupportResistanceLevel],
    *,
    broken_support: SupportResistanceLevel | None,
    broken_resistance: SupportResistanceLevel | None,
    tolerance: float,
    current_price: float,
    max_levels: int,
) -> tuple[list[SupportResistanceLevel], list[SupportResistanceLevel]]:
    reconciled_supports = list(supports)
    reconciled_resistances = list(resistances)
    if broken_support is not None:
        reconciled_supports = _drop_levels_near_price(
            reconciled_supports,
            float(broken_support.price),
            tolerance=tolerance,
        )
    if broken_resistance is not None:
        reconciled_resistances = _drop_levels_near_price(
            reconciled_resistances,
            float(broken_resistance.price),
            tolerance=tolerance,
        )
    tol = max(float(tolerance), 1e-9)
    reconciled_supports = _collapse_same_side_levels(
        reconciled_supports,
        tol,
        current_price,
        reverse=True,
        max_levels=max_levels,
    ) if reconciled_supports else []
    reconciled_resistances = _collapse_same_side_levels(
        reconciled_resistances,
        tol,
        current_price,
        reverse=False,
        max_levels=max_levels,
    ) if reconciled_resistances else []
    return reconciled_supports, reconciled_resistances



def _completed_flip_frames(flip_frame: pd.DataFrame | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    base = ensure_ohlcv_frame(flip_frame if flip_frame is not None else pd.DataFrame())
    if base.empty:
        return base, base
    now_ts = pd.Timestamp(now_et())
    one_min_cutoff = now_ts.floor("1min")
    completed_1m = base[base.index < one_min_cutoff]
    if completed_1m.empty:
        return completed_1m, pd.DataFrame(columns=completed_1m.columns)
    completed_5m = resample_bars(completed_1m, "5min")
    if not completed_5m.empty:
        # A 5m bar labeled T is built (via label='right', closed='right') from
        # 1m bars in (T-5min, T). The last required 1m bar is T itself, which
        # must be strictly below one_min_cutoff to be a completed bar — i.e.,
        # we can only trust the 5m bar labeled T once now >= T + 1min. Using
        # `now_ts.floor('5min')` as the cutoff would incorrectly admit a 5m
        # bar that's still missing its final 1m constituent, letting flip
        # confirmation fire on partial data in the 1-minute window right
        # after a 5-minute boundary.
        completed_5m = completed_5m[completed_5m.index < one_min_cutoff]
    return completed_1m, completed_5m


def _flip_confirmed(
    level_price: float,
    *,
    flip_frame: pd.DataFrame | None,
    confirm_1m_bars: int,
    confirm_5m_bars: int,
    direction: str,
    fallback_bar: tuple[float, float] | None = None,
    eps: float = 0.0,
) -> bool:
    completed_1m, completed_5m = _completed_flip_frames(flip_frame)
    overlay_requested = flip_frame is not None and (confirm_1m_bars > 0 or confirm_5m_bars > 0)
    if direction == "reclaim":
        confirmed = (
            confirm_by_bars(completed_1m, "low", "above", level_price, int(confirm_1m_bars or 0), float(eps))
            or confirm_by_bars(completed_5m, "low", "above", level_price, int(confirm_5m_bars or 0), float(eps))
        )
        if confirmed:
            return True
        if overlay_requested:
            return False
        if fallback_bar is None:
            return False
        _, fallback_low = fallback_bar
        return float(fallback_low) > float(level_price) + float(eps)
    confirmed = (
        confirm_by_bars(completed_1m, "high", "below", level_price, int(confirm_1m_bars or 0), float(eps))
        or confirm_by_bars(completed_5m, "high", "below", level_price, int(confirm_5m_bars or 0), float(eps))
    )
    if confirmed:
        return True
    if overlay_requested:
        return False
    if fallback_bar is None:
        return False
    fallback_high, _ = fallback_bar
    return float(fallback_high) < float(level_price) - float(eps)


def zone_flip_confirmed(
    kind: str,
    lower: float,
    upper: float,
    *,
    flip_frame: pd.DataFrame | None,
    confirm_1m_bars: int,
    confirm_5m_bars: int,
    fallback_bar: tuple[float, float] | None = None,
    eps: float = 0.0,
) -> bool:
    zone_kind = str(kind or '').strip().lower()
    if zone_kind == 'support':
        return _flip_confirmed(
            float(lower),
            flip_frame=flip_frame,
            confirm_1m_bars=confirm_1m_bars,
            confirm_5m_bars=confirm_5m_bars,
            direction='loss',
            fallback_bar=fallback_bar,
            eps=eps,
        )
    if zone_kind == 'resistance':
        return _flip_confirmed(
            float(upper),
            flip_frame=flip_frame,
            confirm_1m_bars=confirm_1m_bars,
            confirm_5m_bars=confirm_5m_bars,
            direction='reclaim',
            fallback_bar=fallback_bar,
            eps=eps,
        )
    return False


def _split_references_by_flip(
    *,
    support_references: list,
    resistance_references: list,
    flip_frame,
    flip_confirmation_1m_bars: int,
    flip_confirmation_5m_bars: int,
    fallback_bar: tuple[float, float],
    flip_eps: float,
) -> tuple[list, list]:
    """Initial side-assignment for every reference level. Support refs that
    have been decisively lost move to the resistance side; resistance refs
    that have been reclaimed move to the support side. Everything else
    retains its original side. Extracted from
    build_support_resistance_context for Phase 3b decomposition."""
    support_candidates: list = []
    resistance_candidates: list = []
    for level in support_references:
        if _flip_confirmed(
            float(level.price),
            flip_frame=flip_frame,
            confirm_1m_bars=flip_confirmation_1m_bars,
            confirm_5m_bars=flip_confirmation_5m_bars,
            direction="loss",
            fallback_bar=fallback_bar,
            eps=flip_eps,
        ):
            resistance_candidates.append(_clone_level(level, "resistance"))
        else:
            support_candidates.append(_clone_level(level, "support"))
    for level in resistance_references:
        if _flip_confirmed(
            float(level.price),
            flip_frame=flip_frame,
            confirm_1m_bars=flip_confirmation_1m_bars,
            confirm_5m_bars=flip_confirmation_5m_bars,
            direction="reclaim",
            fallback_bar=fallback_bar,
            eps=flip_eps,
        ):
            support_candidates.append(_clone_level(level, "support"))
        else:
            resistance_candidates.append(_clone_level(level, "resistance"))
    return support_candidates, resistance_candidates


def _detect_broken_levels(
    *,
    support_references: list,
    resistance_references: list,
    flip_frame,
    flip_confirmation_1m_bars: int,
    flip_confirmation_5m_bars: int,
    fallback_bar: tuple[float, float],
    flip_eps: float,
    support_filter_price: float,
    resistance_filter_price: float,
    merge_tol: float,
    side_tolerance: float,
    close: float,
    max_levels_per_side: int,
):
    """Detect levels that have flipped direction: former resistance now acting
    as support (reclaim) and former support now acting as resistance (loss).

    Returns (broken_support, broken_resistance) — each the top collapsed level
    on that side, or None. Extracted from build_support_resistance_context
    for Phase 3b decomposition."""
    broken_resistance_candidates = [
        _clone_level(level, "support")
        for level in resistance_references
        if _flip_confirmed(
            float(level.price),
            flip_frame=flip_frame,
            confirm_1m_bars=flip_confirmation_1m_bars,
            confirm_5m_bars=flip_confirmation_5m_bars,
            direction="reclaim",
            fallback_bar=fallback_bar,
            eps=flip_eps,
        )
        and float(level.price) <= resistance_filter_price + merge_tol
    ]
    broken_resistance_levels = _collapse_same_side_levels(
        broken_resistance_candidates,
        side_tolerance,
        close,
        reverse=True,
        max_levels=max_levels_per_side,
    )
    broken_resistance = broken_resistance_levels[0] if broken_resistance_levels else None
    broken_support_candidates = [
        _clone_level(level, "resistance")
        for level in support_references
        if _flip_confirmed(
            float(level.price),
            flip_frame=flip_frame,
            confirm_1m_bars=flip_confirmation_1m_bars,
            confirm_5m_bars=flip_confirmation_5m_bars,
            direction="loss",
            fallback_bar=fallback_bar,
            eps=flip_eps,
        )
        and float(level.price) >= support_filter_price - merge_tol
    ]
    broken_support_levels = _collapse_same_side_levels(
        broken_support_candidates,
        side_tolerance,
        close,
        reverse=False,
        max_levels=max_levels_per_side,
    )
    broken_support = broken_support_levels[0] if broken_support_levels else None
    return broken_support, broken_resistance


def _compute_level_proximity_metrics(
    *,
    supports: list,
    resistances: list,
    broken_support,
    broken_resistance,
    close: float,
    atr: float,
    last_low: float,
    last_high: float,
    flip_eps: float,
    stop_buffer_atr_mult: float,
    breakout_buffer_pct: float,
    breakout_atr_mult: float,
    proximity_atr_mult: float,
) -> dict:
    """Compute distance, proximity, and breakout flags from support/resistance
    lists + broken-level detections. Returns a dict used by both the bias
    computation and the final SupportResistanceContext.
    Extracted from build_support_resistance_context."""
    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None
    support_distance_pct = ((close - nearest_support.price) / close) if nearest_support and close > 0 else None
    resistance_distance_pct = ((nearest_resistance.price - close) / close) if nearest_resistance and close > 0 else None
    support_distance_atr = ((close - nearest_support.price) / atr) if nearest_support and atr > 0 else None
    resistance_distance_atr = ((nearest_resistance.price - close) / atr) if nearest_resistance and atr > 0 else None
    level_buffer = max(atr * float(stop_buffer_atr_mult), close * float(breakout_buffer_pct) * 0.5)
    breakout_buffer = max(atr * float(breakout_atr_mult), close * float(breakout_buffer_pct))
    breakout_above_resistance = bool(
        broken_resistance
        and close >= float(broken_resistance.price) + breakout_buffer
        and last_low > float(broken_resistance.price) + flip_eps
    )
    breakdown_below_support = bool(
        broken_support
        and close <= float(broken_support.price) - breakout_buffer
        and last_high < float(broken_support.price) - flip_eps
    )
    near_support = bool(
        nearest_support
        and support_distance_atr is not None
        and support_distance_atr <= float(proximity_atr_mult)
        and close >= nearest_support.price - level_buffer
    )
    near_resistance = bool(
        nearest_resistance
        and resistance_distance_atr is not None
        and resistance_distance_atr <= float(proximity_atr_mult)
        and close <= nearest_resistance.price + level_buffer
    )
    return {
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "support_distance_pct": support_distance_pct,
        "resistance_distance_pct": resistance_distance_pct,
        "support_distance_atr": support_distance_atr,
        "resistance_distance_atr": resistance_distance_atr,
        "level_buffer": level_buffer,
        "breakout_above_resistance": breakout_above_resistance,
        "breakdown_below_support": breakdown_below_support,
        "near_support": near_support,
        "near_resistance": near_resistance,
    }


def _compute_bias_and_regime(proximity: dict) -> tuple[float, str]:
    """Score the directional bias and pick a regime hint string from the
    breakout / proximity flags in ``proximity``. Pure function of the metrics
    dict. Extracted from build_support_resistance_context."""
    nearest_support = proximity["nearest_support"]
    nearest_resistance = proximity["nearest_resistance"]
    support_distance_atr = proximity["support_distance_atr"]
    resistance_distance_atr = proximity["resistance_distance_atr"]
    breakout_above_resistance = proximity["breakout_above_resistance"]
    breakdown_below_support = proximity["breakdown_below_support"]
    near_support = proximity["near_support"]
    near_resistance = proximity["near_resistance"]

    bias = 0.0
    if breakout_above_resistance:
        bias += 0.75
    if breakdown_below_support:
        bias -= 0.75
    if near_support and not breakdown_below_support:
        bias += 0.35
    if near_resistance and not breakout_above_resistance:
        bias -= 0.35
    if (
        nearest_support and nearest_resistance
        and support_distance_atr is not None and resistance_distance_atr is not None
        and support_distance_atr >= 0 and resistance_distance_atr >= 0
    ):
        # Only compare distances when price is BETWEEN the two levels (both distances non-negative).
        # If price has broken through a level, one distance goes negative and the ordering comparison
        # below would produce the wrong bias.
        if resistance_distance_atr > support_distance_atr + 0.75:
            bias += 0.15
        elif support_distance_atr > resistance_distance_atr + 0.75:
            bias -= 0.15

    if breakout_above_resistance:
        regime_hint = "bullish_breakout"
    elif breakdown_below_support:
        regime_hint = "bearish_breakdown"
    elif near_support and not near_resistance:
        regime_hint = "support_hold"
    elif near_resistance and not near_support:
        regime_hint = "resistance_pressure"
    elif nearest_support and nearest_resistance:
        regime_hint = "range_between_levels"
    else:
        regime_hint = "neutral"
    return float(bias), regime_hint


def build_support_resistance_context(
    frame: pd.DataFrame,
    *,
    current_price: float | None = None,
    pivot_span: int = 2,
    max_levels_per_side: int = 3,
    atr_tolerance_mult: float = 0.60,
    pct_tolerance: float = 0.0030,
    same_side_min_gap_atr_mult: float = 0.10,
    same_side_min_gap_pct: float = 0.0015,
    fallback_reference_max_drift_atr_mult: float = 1.0,
    fallback_reference_max_drift_pct: float = 0.01,
    proximity_atr_mult: float = 0.75,
    breakout_atr_mult: float = 0.35,
    breakout_buffer_pct: float = 0.0015,
    stop_buffer_atr_mult: float = 0.25,
    structure_eq_atr_mult: float = 0.25,
    structure_event_max_age_bars: int | None = 6,
    structure_min_range_atr_mult: float = 1.5,
    use_prior_day_high_low: bool = True,
    use_prior_week_high_low: bool = True,
    flip_frame: pd.DataFrame | None = None,
    flip_confirmation_1m_bars: int = 0,
    flip_confirmation_5m_bars: int = 0,
    timeframe_minutes: int = 15,
) -> SupportResistanceContext:
    frame = ensure_standard_indicator_frame(frame)
    if frame.empty:
        return empty_support_resistance_context(float(current_price or 0.0), timeframe_minutes=timeframe_minutes)
    close = resolve_current_price(frame, current_price)
    atr = atr_value(frame)
    merge_tol = max(atr * float(atr_tolerance_mult), close * float(pct_tolerance))
    same_side_min_gap = _same_side_min_gap_threshold(
        atr,
        close,
        min_gap_atr_mult=float(same_side_min_gap_atr_mult),
        min_gap_pct=float(same_side_min_gap_pct),
    )
    fallback_reference_price = _safe_reference_price_for_fallback(
        frame,
        close,
        atr=atr,
        max_drift_atr_mult=float(fallback_reference_max_drift_atr_mult),
        max_drift_pct=float(fallback_reference_max_drift_pct),
    )
    highs, lows = _pivot_points(frame, int(pivot_span))
    # _cluster_levels expects (ts, price) tuples; strip the leading pos.
    highs_for_cluster = [(ts, price) for _, ts, price in highs]
    lows_for_cluster = [(ts, price) for _, ts, price in lows]
    raw_pivot_resistances = _cluster_levels(highs_for_cluster, "resistance", merge_tol, max(max_levels_per_side * 2, 2)) if highs_for_cluster else []
    raw_pivot_supports = _cluster_levels(lows_for_cluster, "support", merge_tol, max(max_levels_per_side * 2, 2)) if lows_for_cluster else []

    include_prior_day = bool(use_prior_day_high_low)
    include_prior_week = bool(use_prior_week_high_low)
    prior_day_high, prior_day_low = _prior_day_levels(frame) if include_prior_day else (None, None)
    prior_week_high, prior_week_low = _prior_week_levels(frame) if include_prior_week else (None, None)

    support_references: list[SupportResistanceLevel] = list(raw_pivot_supports)
    resistance_references: list[SupportResistanceLevel] = list(raw_pivot_resistances)
    support_filter_price = close
    resistance_filter_price = close
    if not support_references:
        support_references = _fallback_prior_side_levels(
            side="support",
            current_price=fallback_reference_price,
            include_prior_day=include_prior_day,
            include_prior_week=include_prior_week,
            prior_day_high=prior_day_high,
            prior_day_low=prior_day_low,
            prior_week_high=prior_week_high,
            prior_week_low=prior_week_low,
        )
        if support_references:
            support_filter_price = fallback_reference_price
        else:
            min_low_pos = int(frame["low"].astype(float).values.argmin())
            support_references = _cluster_levels(
                [(pd.Timestamp(frame.index[min_low_pos]), float(frame["low"].iloc[min_low_pos]))],
                "support",
                merge_tol,
                max(max_levels_per_side * 2, 2),
            )
    if not resistance_references:
        resistance_references = _fallback_prior_side_levels(
            side="resistance",
            current_price=fallback_reference_price,
            include_prior_day=include_prior_day,
            include_prior_week=include_prior_week,
            prior_day_high=prior_day_high,
            prior_day_low=prior_day_low,
            prior_week_high=prior_week_high,
            prior_week_low=prior_week_low,
        )
        if resistance_references:
            resistance_filter_price = fallback_reference_price
        else:
            max_high_pos = int(frame["high"].astype(float).values.argmax())
            resistance_references = _cluster_levels(
                [(pd.Timestamp(frame.index[max_high_pos]), float(frame["high"].iloc[max_high_pos]))],
                "resistance",
                merge_tol,
                max(max_levels_per_side * 2, 2),
            )

    last_bar = frame.iloc[-1]
    last_low = float(last_bar.low)
    last_high = float(last_bar.high)
    fallback_bar = (last_high, last_low)
    flip_eps = max(abs(close) * 1e-6, 1e-8)

    support_candidates, resistance_candidates = _split_references_by_flip(
        support_references=support_references,
        resistance_references=resistance_references,
        flip_frame=flip_frame,
        flip_confirmation_1m_bars=flip_confirmation_1m_bars,
        flip_confirmation_5m_bars=flip_confirmation_5m_bars,
        fallback_bar=fallback_bar,
        flip_eps=flip_eps,
    )

    support_candidates = _filter_levels_by_side(support_candidates, support_filter_price, side="support")
    resistance_candidates = _filter_levels_by_side(resistance_candidates, resistance_filter_price, side="resistance")

    if not support_candidates:
        second_chance_support_refs = _fallback_prior_side_levels(
            side="support",
            current_price=fallback_reference_price,
            include_prior_day=include_prior_day,
            include_prior_week=include_prior_week,
            prior_day_high=prior_day_high,
            prior_day_low=prior_day_low,
            prior_week_high=prior_week_high,
            prior_week_low=prior_week_low,
        )
        if second_chance_support_refs:
            support_filter_price = fallback_reference_price
        else:
            second_chance_support_refs = _frame_extreme_side_levels(
                frame,
                side="support",
                tolerance=merge_tol,
                max_levels=max(max_levels_per_side * 2, 2),
            )
        extend_unique_levels(support_references, second_chance_support_refs)
        for level in second_chance_support_refs:
            if _flip_confirmed(
                float(level.price),
                flip_frame=flip_frame,
                confirm_1m_bars=flip_confirmation_1m_bars,
                confirm_5m_bars=flip_confirmation_5m_bars,
                direction="loss",
                fallback_bar=fallback_bar,
                eps=flip_eps,
            ):
                resistance_candidates.append(_clone_level(level, "resistance"))
            else:
                support_candidates.append(_clone_level(level, "support"))

    if not resistance_candidates:
        second_chance_resistance_refs = _fallback_prior_side_levels(
            side="resistance",
            current_price=fallback_reference_price,
            include_prior_day=include_prior_day,
            include_prior_week=include_prior_week,
            prior_day_high=prior_day_high,
            prior_day_low=prior_day_low,
            prior_week_high=prior_week_high,
            prior_week_low=prior_week_low,
        )
        if second_chance_resistance_refs:
            resistance_filter_price = fallback_reference_price
        else:
            second_chance_resistance_refs = _frame_extreme_side_levels(
                frame,
                side="resistance",
                tolerance=merge_tol,
                max_levels=max(max_levels_per_side * 2, 2),
            )
        extend_unique_levels(resistance_references, second_chance_resistance_refs)
        for level in second_chance_resistance_refs:
            if _flip_confirmed(
                float(level.price),
                flip_frame=flip_frame,
                confirm_1m_bars=flip_confirmation_1m_bars,
                confirm_5m_bars=flip_confirmation_5m_bars,
                direction="reclaim",
                fallback_bar=fallback_bar,
                eps=flip_eps,
            ):
                support_candidates.append(_clone_level(level, "support"))
            else:
                resistance_candidates.append(_clone_level(level, "resistance"))

    support_candidates = _filter_levels_by_side(support_candidates, support_filter_price, side="support")
    resistance_candidates = _filter_levels_by_side(resistance_candidates, resistance_filter_price, side="resistance")

    side_tolerance = max(merge_tol, same_side_min_gap)
    supports = _collapse_same_side_levels(
        support_candidates,
        side_tolerance,
        close,
        reverse=True,
        max_levels=max_levels_per_side,
    )
    resistances = _collapse_same_side_levels(
        resistance_candidates,
        side_tolerance,
        close,
        reverse=False,
        max_levels=max_levels_per_side,
    )

    broken_support, broken_resistance = _detect_broken_levels(
        support_references=support_references,
        resistance_references=resistance_references,
        flip_frame=flip_frame,
        flip_confirmation_1m_bars=flip_confirmation_1m_bars,
        flip_confirmation_5m_bars=flip_confirmation_5m_bars,
        fallback_bar=fallback_bar,
        flip_eps=flip_eps,
        support_filter_price=support_filter_price,
        resistance_filter_price=resistance_filter_price,
        merge_tol=merge_tol,
        side_tolerance=side_tolerance,
        close=close,
        max_levels_per_side=max_levels_per_side,
    )
    supports, resistances = _reconcile_flipped_levels(
        supports,
        resistances,
        broken_support=broken_support,
        broken_resistance=broken_resistance,
        tolerance=side_tolerance,
        current_price=close,
        max_levels=max_levels_per_side,
    )
    proximity = _compute_level_proximity_metrics(
        supports=supports,
        resistances=resistances,
        broken_support=broken_support,
        broken_resistance=broken_resistance,
        close=close,
        atr=atr,
        last_low=last_low,
        last_high=last_high,
        flip_eps=flip_eps,
        stop_buffer_atr_mult=float(stop_buffer_atr_mult),
        breakout_buffer_pct=float(breakout_buffer_pct),
        breakout_atr_mult=float(breakout_atr_mult),
        proximity_atr_mult=float(proximity_atr_mult),
    )
    nearest_support = proximity["nearest_support"]
    nearest_resistance = proximity["nearest_resistance"]
    support_distance_pct = proximity["support_distance_pct"]
    resistance_distance_pct = proximity["resistance_distance_pct"]
    support_distance_atr = proximity["support_distance_atr"]
    resistance_distance_atr = proximity["resistance_distance_atr"]
    level_buffer = proximity["level_buffer"]
    breakout_above_resistance = proximity["breakout_above_resistance"]
    breakdown_below_support = proximity["breakdown_below_support"]
    near_support = proximity["near_support"]
    near_resistance = proximity["near_resistance"]
    bias, regime_hint = _compute_bias_and_regime(proximity)
    market_structure = analyze_market_structure(
        frame,
        current_price=close,
        pivot_span=int(pivot_span),
        eq_atr_mult=float(structure_eq_atr_mult),
        pct_tolerance=float(pct_tolerance),
        breakout_atr_mult=float(breakout_atr_mult),
        breakout_buffer_pct=float(breakout_buffer_pct),
        structure_event_max_age_bars=structure_event_max_age_bars,
        min_range_atr_mult=float(structure_min_range_atr_mult),
    )
    return SupportResistanceContext(
        current_price=close,
        timeframe_minutes=int(timeframe_minutes or 15),
        supports=supports,
        resistances=resistances,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        broken_resistance=broken_resistance,
        broken_support=broken_support,
        prior_day_high=prior_day_high,
        prior_day_low=prior_day_low,
        prior_week_high=prior_week_high,
        prior_week_low=prior_week_low,
        support_distance_pct=support_distance_pct,
        resistance_distance_pct=resistance_distance_pct,
        support_distance_atr=support_distance_atr,
        resistance_distance_atr=resistance_distance_atr,
        current_atr=float(atr),
        same_side_min_gap=float(same_side_min_gap),
        side_tolerance=float(side_tolerance),
        level_buffer=level_buffer,
        breakout_above_resistance=breakout_above_resistance,
        breakdown_below_support=breakdown_below_support,
        near_support=near_support,
        near_resistance=near_resistance,
        bias_score=float(bias),
        regime_hint=regime_hint,
        market_structure=market_structure,
    )
