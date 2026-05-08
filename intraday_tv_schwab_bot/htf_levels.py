# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Iterable

import numpy as np
import pandas as pd

from .levels_shared import (
    DivergenceMatch,
    clone_level,
    cluster_levels,
    cluster_levels_by_tolerance,
    confirm_by_bars,
    datetime_index as _datetime_index,
    extend_unique_levels,
    fallback_prior_side_levels,
    find_divergence,
    frame_extreme_side_levels as _frame_extreme_side_levels_shared,
    pivot_points,
    prior_day_levels as _prior_day_levels,
    prior_week_levels as _prior_week_levels,
    safe_reference_price_for_fallback as _safe_reference_price_for_fallback,
    same_side_min_gap_threshold as _same_side_min_gap_threshold,
)
from .position_metrics import safe_float
from .utils import (
    ensure_ohlcv_frame,
    ensure_standard_indicator_frame,
    now_et,
    resolve_current_price,
)


LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class HTFLevel:
    kind: str
    price: float
    touches: int = 1
    score: float = 1.0
    first_seen: str | None = None
    last_seen: str | None = None
    source: str = "pivot"
    source_priority: float = 1.0


@dataclass(slots=True)
class HTFFairValueGap:
    direction: str
    lower: float
    upper: float
    midpoint: float
    size: float
    first_seen: str | None = None
    last_seen: str | None = None
    filled_pct: float = 0.0
    source: str = "htf_fvg"


@dataclass(slots=True)
class FairValueGapContext:
    timeframe_minutes: int
    current_price: float
    bullish_fvgs: list[HTFFairValueGap] = field(default_factory=list)
    bearish_fvgs: list[HTFFairValueGap] = field(default_factory=list)
    nearest_bullish_fvg: HTFFairValueGap | None = None
    nearest_bearish_fvg: HTFFairValueGap | None = None


@dataclass(slots=True)
class HTFContext:
    timeframe_minutes: int
    current_price: float
    supports: list[HTFLevel] = field(default_factory=list)
    resistances: list[HTFLevel] = field(default_factory=list)
    nearest_support: HTFLevel | None = None
    broken_resistance: HTFLevel | None = None
    nearest_resistance: HTFLevel | None = None
    broken_support: HTFLevel | None = None
    prior_day_high: float | None = None
    prior_day_low: float | None = None
    prior_week_high: float | None = None
    prior_week_low: float | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    atr14: float | None = None
    bullish_fvgs: list[HTFFairValueGap] = field(default_factory=list)
    bearish_fvgs: list[HTFFairValueGap] = field(default_factory=list)
    nearest_bullish_fvg: HTFFairValueGap | None = None
    nearest_bearish_fvg: HTFFairValueGap | None = None
    trend_bias: str = "neutral"
    level_buffer: float = 0.0
    # HTF RSI divergence — same shared detector as technical_levels, but
    # walked over HTF pivots so it captures multi-timeframe confluence.
    # Populated when build_htf_context is called with divergence_enabled.
    bullish_rsi_divergence: "DivergenceMatch | None" = None
    bearish_rsi_divergence: "DivergenceMatch | None" = None
    bullish_hidden_rsi_divergence: "DivergenceMatch | None" = None
    bearish_hidden_rsi_divergence: "DivergenceMatch | None" = None


def empty_htf_context(current_price: float = 0.0, *, timeframe_minutes: int = 60) -> HTFContext:
    return HTFContext(timeframe_minutes=int(timeframe_minutes), current_price=float(current_price or 0.0))


def empty_fvg_context(current_price: float = 0.0, *, timeframe_minutes: int = 1) -> FairValueGapContext:
    return FairValueGapContext(timeframe_minutes=int(timeframe_minutes), current_price=float(current_price or 0.0))


def summarize_htf_trend(
    frame: pd.DataFrame | None,
    *,
    min_bars: int = 20,
    vwap_distance_pct: float = 0.0010,
    ema_gap_pct: float = 0.0008,
    min_ret3: float = 0.0010,
    range_vwap_distance_pct: float = 0.0020,
    range_ema_gap_pct: float = 0.0010,
) -> dict[str, object]:
    if frame is None or frame.empty or len(frame) < max(4, int(min_bars)):
        return {"available": False, "reason": "insufficient_htf_bars"}
    recent = ensure_ohlcv_frame(frame).tail(max(4, int(min_bars))).copy()
    try:
        if "datetime" in recent.columns:
            recent = recent.sort_values("datetime").reset_index(drop=True)
    except Exception:
        LOG.debug("Failed to sort recent HTF bars by datetime; continuing with existing order.", exc_info=True)
    recent = ensure_standard_indicator_frame(recent)
    if recent.empty:
        return {"available": False, "reason": "empty_htf_frame"}
    last = recent.iloc[-1]
    close = safe_float(getattr(last, "close", None), safe_float(last.get("close"), 0.0) if hasattr(last, 'get') else 0.0)
    if close <= 0:
        return {"available": False, "reason": "invalid_htf_close"}

    # For higher-timeframe trend classification, prefer continuous indicator fields
    # when they are available. The runtime signal fields can reset by session when
    # RTH-only indicators are enabled, which makes HTF trend summaries look neutral
    # too often even when the broader HTF tape is directional.
    vwap = safe_float(
        getattr(last, "vwap_all", None),
        safe_float(last.get("vwap_all"), safe_float(getattr(last, "vwap", None), safe_float(last.get("vwap"), close) if hasattr(last, 'get') else close)) if hasattr(last, 'get') else safe_float(getattr(last, "vwap", None), close),
    )
    ema9 = safe_float(
        getattr(last, "ema9_all", None),
        safe_float(last.get("ema9_all"), safe_float(getattr(last, "ema9", None), safe_float(last.get("ema9"), close) if hasattr(last, 'get') else close)) if hasattr(last, 'get') else safe_float(getattr(last, "ema9", None), close),
    )
    ema20 = safe_float(
        getattr(last, "ema20_all", None),
        safe_float(last.get("ema20_all"), safe_float(getattr(last, "ema20", None), safe_float(last.get("ema20"), close) if hasattr(last, 'get') else close)) if hasattr(last, 'get') else safe_float(getattr(last, "ema20", None), close),
    )
    ref = close
    if len(recent) >= 4:
        ref_row = recent.iloc[-4]
        ref = safe_float(getattr(ref_row, "close", None), safe_float(ref_row.get("close"), close) if hasattr(ref_row, 'get') else close)
    ret3 = ((close / ref) - 1.0) if ref > 0 else 0.0
    vwap_dist = (close - vwap) / max(close, 1.0)
    ema_gap = (ema9 - ema20) / max(close, 1.0)
    bullish = (
        vwap_dist >= float(vwap_distance_pct)
        and ema_gap >= float(ema_gap_pct)
        and ret3 >= float(min_ret3)
    )
    bearish = (
        vwap_dist <= -float(vwap_distance_pct)
        and ema_gap <= -float(ema_gap_pct)
        and ret3 <= -float(min_ret3)
    )
    rangeish = (
        abs(vwap_dist) <= float(range_vwap_distance_pct)
        and abs(ema_gap) <= float(range_ema_gap_pct)
    )
    state = "bullish" if bullish else ("bearish" if bearish else "neutral")
    label = "Bullish" if bullish else ("Bearish" if bearish else "—")
    return {
        "available": True,
        "reason": "ok",
        "frame": recent,
        "close": float(close),
        "vwap_dist": float(vwap_dist),
        "ema_gap": float(ema_gap),
        "ret3": float(ret3),
        "bullish": bool(bullish),
        "bearish": bool(bearish),
        "range": bool(rangeish),
        "state": state,
        "label": label,
    }


def _pivot_points(frame: pd.DataFrame, span: int) -> tuple[list[tuple[pd.Timestamp, float]], list[tuple[pd.Timestamp, float]]]:
    # Thin wrapper around the shared `pivot_points` helper so the
    # detection semantics can't drift between htf_levels and
    # support_resistance — both modules pivot-detect the same way.
    return pivot_points(frame, span, include_idx=False)


def _cluster_levels(points: Iterable[tuple[pd.Timestamp, float]], kind: str, tolerance: float, max_levels: int) -> list[HTFLevel]:
    # Thin wrapper around the shared `cluster_levels` helper. The
    # time-aware recency scoring (effective_touches = touches *
    # recency_factor + persistence bonus) lives in levels_shared so it
    # can't drift between this module and support_resistance.py — when
    # we improved scoring in the AMD/INTC debug pass, having one source
    # of truth would have made the fix apply everywhere automatically.
    return cluster_levels(points, kind, tolerance, max_levels, level_factory=HTFLevel)

def _clone_level(level: HTFLevel, kind: str, *, source: str | None = None) -> HTFLevel:
    # Thin factory-binding wrapper around the shared `clone_level` helper.
    # Same pattern as `_cluster_levels` / `_pivot_points`: keeps call sites
    # readable without repeating `level_factory=HTFLevel` at every emit.
    return clone_level(level, kind, level_factory=HTFLevel, source=source)


def _frame_extreme_side_levels(
    frame: pd.DataFrame,
    *,
    side: str,
    tolerance: float,
    max_levels: int,
) -> list[HTFLevel]:
    # Thin factory-binding wrapper around `frame_extreme_side_levels`.
    return _frame_extreme_side_levels_shared(
        frame,
        side=side,
        tolerance=tolerance,
        max_levels=max_levels,
        level_factory=HTFLevel,
    )


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
) -> list[HTFLevel]:
    return fallback_prior_side_levels(
        side=side,
        current_price=current_price,
        include_prior_day=include_prior_day,
        include_prior_week=include_prior_week,
        prior_day_high=prior_day_high,
        prior_day_low=prior_day_low,
        prior_week_high=prior_week_high,
        prior_week_low=prior_week_low,
        level_factory=HTFLevel,
    )


def _level_preference(level: HTFLevel, current_price: float) -> tuple[float, float, int, float]:
    return (
        float(getattr(level, "source_priority", 1.0) or 1.0),
        float(level.score),
        int(level.touches),
        -abs(float(level.price) - float(current_price)),
    )



def _collapse_same_side_levels(
    levels: list[HTFLevel],
    tolerance: float,
    current_price: float,
    *,
    reverse: bool,
    max_levels: int,
) -> list[HTFLevel]:
    # HTF picks one representative per cluster (highest source_priority,
    # then score, touches, distance). support_resistance uses the same
    # tolerance grouping but merges all attributes — that's why the
    # grouping lives in levels_shared and the per-cluster reducer stays
    # local to each module.
    groups = cluster_levels_by_tolerance(levels, tolerance)
    if not groups:
        return []
    selected = [max(group, key=lambda lv: _level_preference(lv, current_price)) for group in groups]
    selected.sort(key=lambda lv: float(lv.price), reverse=bool(reverse))
    return selected[: max(1, int(max_levels))]



def _completed_htf_frame(frame: pd.DataFrame, timeframe_minutes: int) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=getattr(frame, "columns", []))
    try:
        base = frame.copy()
        idx = _datetime_index(base.index)
        base.index = idx
        # Anchor "now" in the ET trading timezone so the cutoff matches the
        # frame's ET-localized bar labels, even when the bot runs on a
        # non-ET server. Previously this used `pd.Timestamp.now()` as the
        # tz-naive fallback, which returns the SERVER's wall clock — off by
        # hours if the process isn't running on US Eastern.
        et_now = pd.Timestamp(now_et())
        if idx.tz is not None:
            now_ts = et_now.tz_convert(idx.tz)
        else:
            now_ts = et_now.tz_localize(None)
        cutoff = now_ts.floor(f"{max(1, int(timeframe_minutes))}min")
        time_label = str(getattr(frame, "attrs", {}).get("time_label", "unknown") or "unknown").lower()
        if time_label == "right":
            completed = base[base.index <= cutoff]
        else:
            completed = base[base.index < cutoff]
        return completed if isinstance(completed, pd.DataFrame) else pd.DataFrame(columns=base.columns)
    except Exception:
        return frame.iloc[:-1].copy() if len(frame) > 1 else pd.DataFrame(columns=frame.columns)


def _htf_flip_confirmed(
    frame: pd.DataFrame,
    level_price: float,
    *,
    timeframe_minutes: int,
    confirm_bars: int,
    direction: str,
    eps: float,
) -> bool:
    completed = _completed_htf_frame(frame, timeframe_minutes)
    if direction == "reclaim":
        return confirm_by_bars(completed, "low", "above", level_price, int(confirm_bars or 0), float(eps))
    return confirm_by_bars(completed, "high", "below", level_price, int(confirm_bars or 0), float(eps))


def _htf_flip_active(
    frame: pd.DataFrame,
    level_price: float,
    *,
    timeframe_minutes: int,
    confirm_bars: int,
    direction: str,
    eps: float,
) -> bool:
    if int(confirm_bars or 0) > 0:
        return _htf_flip_confirmed(
            frame,
            level_price,
            timeframe_minutes=int(timeframe_minutes),
            confirm_bars=int(confirm_bars),
            direction=str(direction),
            eps=float(eps),
        )
    if frame is None or frame.empty:
        return False
    last_bar = frame.iloc[-1]
    last_low = float(last_bar.get("low", 0.0) or 0.0)
    last_high = float(last_bar.get("high", 0.0) or 0.0)
    if str(direction).strip().lower() == "reclaim":
        return last_low > float(level_price) + float(eps)
    return last_high < float(level_price) - float(eps)



def _merge_fair_value_gaps(
    gaps: list[HTFFairValueGap],
    *,
    tolerance: float,
    timeframe_minutes: int | None = None,
    max_anchor_gap_bars: int = 4,
) -> list[HTFFairValueGap]:
    if not gaps:
        return []

    resolved_timeframe = max(1, int(timeframe_minutes or 0)) if timeframe_minutes is not None else None
    max_anchor_gap = None
    if resolved_timeframe is not None and max_anchor_gap_bars > 0:
        max_anchor_gap = pd.Timedelta(minutes=resolved_timeframe * int(max_anchor_gap_bars))

    def _parsed_ts(value: str | None) -> pd.Timestamp | None:
        if not value:
            return None
        try:
            parsed = pd.Timestamp(value)
            return parsed.tz_convert(None) if parsed.tzinfo is not None else parsed
        except Exception:
            return None

    ordered = sorted(gaps, key=lambda gap: (float(gap.lower), float(gap.upper)))
    merged: list[HTFFairValueGap] = []
    for gap in ordered:
        if not merged:
            merged.append(gap)
            continue
        prior = merged[-1]
        overlaps_in_price = float(gap.lower) <= float(prior.upper) + float(tolerance)
        merge_allowed = overlaps_in_price
        if merge_allowed and max_anchor_gap is not None:
            prior_ts = _parsed_ts(prior.first_seen)
            gap_ts = _parsed_ts(gap.first_seen)
            if prior_ts is None or gap_ts is None:
                merge_allowed = False
            else:
                merge_allowed = abs(gap_ts - prior_ts) <= max_anchor_gap
        if merge_allowed:
            lower = min(float(prior.lower), float(gap.lower))
            upper = max(float(prior.upper), float(gap.upper))
            first_seen_candidates = [value for value in (prior.first_seen, gap.first_seen) if value]
            first_seen = None
            if first_seen_candidates:
                first_seen = min(first_seen_candidates, key=lambda value: _parsed_ts(value) or pd.Timestamp.max)
            seen_candidates = [value for value in (prior.last_seen, gap.last_seen) if value]
            last_seen = None
            if seen_candidates:
                last_seen = max(seen_candidates, key=lambda value: _parsed_ts(value) or pd.Timestamp.min)
            merged[-1] = HTFFairValueGap(
                direction=str(prior.direction or gap.direction),
                lower=lower,
                upper=upper,
                midpoint=(lower + upper) / 2.0,
                size=max(upper - lower, 0.0),
                first_seen=first_seen,
                last_seen=last_seen,
                filled_pct=min(float(getattr(prior, "filled_pct", 0.0) or 0.0), float(getattr(gap, "filled_pct", 0.0) or 0.0)),
            )
        else:
            merged.append(gap)
    return merged


def _fvg_distance(direction: str, gap: HTFFairValueGap, current_price: float) -> float:
    lower = float(gap.lower)
    upper = float(gap.upper)
    close = float(current_price)
    if lower <= close <= upper:
        return 0.0
    if str(direction).lower() == "bullish":
        if close > upper:
            return close - upper
        return max(lower - close, 0.0)
    if close < lower:
        return lower - close
    return max(close - upper, 0.0)


def _detect_fair_value_gaps(
    frame: pd.DataFrame,
    *,
    timeframe_minutes: int,
    current_price: float,
    max_per_side: int,
    min_gap_atr_mult: float,
    min_gap_pct: float,
) -> tuple[list[HTFFairValueGap], list[HTFFairValueGap], HTFFairValueGap | None, HTFFairValueGap | None]:
    completed = _completed_htf_frame(frame, timeframe_minutes)
    if completed is None or completed.empty or len(completed) < 3:
        return [], [], None, None
    completed = ensure_ohlcv_frame(completed.copy())
    if completed.empty or len(completed) < 3:
        return [], [], None, None
    ref_close = resolve_current_price(completed, current_price)
    atr_fallback = max(ref_close * 0.0015, 0.01)
    if "atr14" in completed.columns:
        atr_clean = completed["atr14"].dropna()
        atr = float(atr_clean.iloc[-1]) if not atr_clean.empty else atr_fallback
    else:
        atr = atr_fallback
    min_gap_size = max(float(atr) * float(min_gap_atr_mult), float(ref_close) * float(min_gap_pct), 1e-8)
    eps = max(min_gap_size * 0.05, ref_close * 1e-6, 1e-8)
    bullish_raw: list[HTFFairValueGap] = []
    bearish_raw: list[HTFFairValueGap] = []
    n = len(completed)
    # Pre-compute reverse-cumulative min/max of low/high in O(n) so the inner "later.min()" /
    # "later.max()" calls become O(1) lookups instead of O(n-idx) each iteration.
    # forward_min_low_after[idx] = min of lows for bars strictly after idx.
    # forward_max_high_after[idx] = max of highs for bars strictly after idx.
    low_col = completed["low"] if "low" in completed.columns else None
    high_col = completed["high"] if "high" in completed.columns else None
    # Use empty arrays (instead of None) when the column is missing — keeps the
    # variables a single non-Optional type so static type narrowing works
    # cleanly through the indexing checks below.
    _empty: np.ndarray = np.array([], dtype=float)
    forward_min_low_after: np.ndarray = (
        low_col.iloc[::-1].cummin().iloc[::-1].shift(-1).to_numpy()
        if (low_col is not None and n > 0)
        else _empty
    )
    forward_max_high_after: np.ndarray = (
        high_col.iloc[::-1].cummax().iloc[::-1].shift(-1).to_numpy()
        if (high_col is not None and n > 0)
        else _empty
    )
    # Cache the raw numpy arrays of high/low for the inner loop to avoid repeated .iloc[].get() calls.
    high_arr = high_col.to_numpy() if high_col is not None else None
    low_arr = low_col.to_numpy() if low_col is not None else None
    index_values = completed.index
    last_index_label = index_values[-1] if n > 0 else None
    last_seen_str = last_index_label.isoformat() if hasattr(last_index_label, "isoformat") else str(last_index_label) if last_index_label is not None else ""
    for idx in range(2, n):
        if high_arr is None or low_arr is None:
            break
        left_high = float(high_arr[idx - 2]) if not (high_arr[idx - 2] != high_arr[idx - 2]) else 0.0
        left_low = float(low_arr[idx - 2]) if not (low_arr[idx - 2] != low_arr[idx - 2]) else 0.0
        right_low = float(low_arr[idx]) if not (low_arr[idx] != low_arr[idx]) else 0.0
        right_high = float(high_arr[idx]) if not (high_arr[idx] != high_arr[idx]) else 0.0
        if right_low > left_high + eps:
            lower = left_high
            upper = right_low
            size = upper - lower
            if size >= min_gap_size:
                # O(1) reverse-cummin lookup instead of O(n-idx) tail().min()
                if idx >= len(forward_min_low_after):
                    min_low_after = upper
                else:
                    raw = forward_min_low_after[idx]
                    min_low_after = upper if pd.isna(raw) else float(raw)  # NaN guard
                if min_low_after > lower + eps:
                    fill_top = min(upper, max(lower, min_low_after))
                    filled_pct = max(0.0, min(1.0, (upper - fill_top) / max(size, 1e-9)))
                    anchor_ts = index_values[idx - 2]
                    bullish_raw.append(
                        HTFFairValueGap(
                            direction="bullish",
                            lower=lower,
                            upper=upper,
                            midpoint=(lower + upper) / 2.0,
                            size=size,
                            first_seen=anchor_ts.isoformat() if hasattr(anchor_ts, "isoformat") else str(anchor_ts),
                            last_seen=last_seen_str,
                            filled_pct=filled_pct,
                        )
                    )
        if right_high < left_low - eps:
            lower = right_high
            upper = left_low
            size = upper - lower
            if size >= min_gap_size:
                if idx >= len(forward_max_high_after):
                    max_high_after = lower
                else:
                    raw = forward_max_high_after[idx]
                    max_high_after = lower if pd.isna(raw) else float(raw)  # NaN guard
                if max_high_after < upper - eps:
                    fill_top = min(upper, max(lower, max_high_after))
                    filled_pct = max(0.0, min(1.0, (fill_top - lower) / max(size, 1e-9)))
                    anchor_ts = index_values[idx - 2]
                    bearish_raw.append(
                        HTFFairValueGap(
                            direction="bearish",
                            lower=lower,
                            upper=upper,
                            midpoint=(lower + upper) / 2.0,
                            size=size,
                            first_seen=anchor_ts.isoformat() if hasattr(anchor_ts, "isoformat") else str(anchor_ts),
                            last_seen=last_seen_str,
                            filled_pct=filled_pct,
                        )
                    )
    merge_tol = max(min_gap_size * 0.25, ref_close * 0.00025, 1e-8)
    bullish = _merge_fair_value_gaps(bullish_raw, tolerance=merge_tol, timeframe_minutes=timeframe_minutes)
    bearish = _merge_fair_value_gaps(bearish_raw, tolerance=merge_tol, timeframe_minutes=timeframe_minutes)
    bullish.sort(key=lambda gap: (_fvg_distance("bullish", gap, ref_close), -float(gap.upper)))
    bearish.sort(key=lambda gap: (_fvg_distance("bearish", gap, ref_close), float(gap.lower)))
    bullish = bullish[: max(0, int(max_per_side or 0))] if int(max_per_side or 0) > 0 else []
    bearish = bearish[: max(0, int(max_per_side or 0))] if int(max_per_side or 0) > 0 else []
    nearest_bullish = bullish[0] if bullish else None
    nearest_bearish = bearish[0] if bearish else None
    return bullish, bearish, nearest_bullish, nearest_bearish


def build_fair_value_gap_context(
    frame: pd.DataFrame | None,
    *,
    timeframe_minutes: int = 1,
    current_price: float | None = None,
    max_per_side: int = 4,
    min_gap_atr_mult: float = 0.05,
    min_gap_pct: float = 0.0005,
) -> FairValueGapContext:
    if frame is None or frame.empty:
        return empty_fvg_context(float(current_price or 0.0), timeframe_minutes=timeframe_minutes)
    time_label = str(getattr(frame, "attrs", {}).get("time_label", "unknown") or "unknown").lower()
    base = ensure_standard_indicator_frame(ensure_ohlcv_frame(frame.copy()))
    base.attrs["time_label"] = time_label
    if base.empty:
        return empty_fvg_context(float(current_price or 0.0), timeframe_minutes=timeframe_minutes)
    close = resolve_current_price(base, current_price)
    bullish_fvgs, bearish_fvgs, nearest_bullish_fvg, nearest_bearish_fvg = _detect_fair_value_gaps(
        base,
        timeframe_minutes=max(1, int(timeframe_minutes)),
        current_price=close,
        max_per_side=max(0, int(max_per_side or 0)),
        min_gap_atr_mult=float(min_gap_atr_mult),
        min_gap_pct=float(min_gap_pct),
    )
    return FairValueGapContext(
        timeframe_minutes=max(1, int(timeframe_minutes)),
        current_price=close,
        bullish_fvgs=bullish_fvgs,
        bearish_fvgs=bearish_fvgs,
        nearest_bullish_fvg=nearest_bullish_fvg,
        nearest_bearish_fvg=nearest_bearish_fvg,
    )


def build_htf_context(
    frame: pd.DataFrame,
    *,
    current_price: float | None = None,
    timeframe_minutes: int = 60,
    pivot_span: int = 2,
    max_levels_per_side: int = 6,
    atr_tolerance_mult: float = 0.35,
    pct_tolerance: float = 0.0030,
    same_side_min_gap_atr_mult: float = 0.10,
    same_side_min_gap_pct: float = 0.0015,
    fallback_reference_max_drift_atr_mult: float = 1.0,
    fallback_reference_max_drift_pct: float = 0.01,
    stop_buffer_atr_mult: float = 0.25,
    ema_fast_span: int = 50,
    ema_slow_span: int = 200,
    flip_confirmation_bars: int = 1,
    use_prior_day_high_low: bool = True,
    use_prior_week_high_low: bool = True,
    include_fair_value_gaps: bool = True,
    fair_value_gap_max_per_side: int = 4,
    fair_value_gap_min_atr_mult: float = 0.05,
    fair_value_gap_min_pct: float = 0.0005,
    divergence_enabled: bool = True,
    divergence_pivot_lookback: int = 4,
    divergence_max_age_bars: int = 6,
    divergence_min_price_move_pct: float = 0.0015,
    divergence_rsi_min_delta: float = 2.5,
) -> HTFContext:
    time_label = str(getattr(frame, "attrs", {}).get("time_label", "unknown") or "unknown").lower()
    frame = ensure_standard_indicator_frame(ensure_ohlcv_frame(frame))
    frame.attrs["time_label"] = time_label
    if frame.empty:
        return empty_htf_context(float(current_price or 0.0), timeframe_minutes=timeframe_minutes)

    close = resolve_current_price(frame, current_price)
    atr_fallback = max(close * 0.0015, 0.01)
    if "atr14" in frame.columns:
        atr_clean = frame["atr14"].dropna()
        atr = float(atr_clean.iloc[-1]) if not atr_clean.empty else atr_fallback
    else:
        atr = atr_fallback
    tolerance = max(atr * float(atr_tolerance_mult), close * float(pct_tolerance))
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
    collapse_tolerance = max(float(tolerance), float(same_side_min_gap))

    highs, lows = _pivot_points(frame, int(pivot_span))
    pivot_resistances = _cluster_levels(highs, "resistance", tolerance, int(max_levels_per_side * 2)) if highs else []
    pivot_supports = _cluster_levels(lows, "support", tolerance, int(max_levels_per_side * 2)) if lows else []

    include_prior_day = bool(use_prior_day_high_low)
    include_prior_week = bool(use_prior_week_high_low)
    prior_day_high, prior_day_low = _prior_day_levels(frame) if include_prior_day else (None, None)
    prior_week_high, prior_week_low = _prior_week_levels(frame) if include_prior_week else (None, None)

    # Prior-day/week levels are FALLBACKS, not always-on candidates. Earlier
    # commit e4abfb1 unconditionally merged them next to pivot-derived levels
    # to handle "strong directional move with no pivot lows in the rally";
    # production showed the cure was worse than the disease — bare price
    # points (no zone bounds) injected regardless of where price was
    # currently trading produced resistance levels rendered beneath support
    # levels and zones drawn as straight lines on the chart. The original
    # symmetric design (used by support_resistance.build_support_resistance_context)
    # is restored: pivots are the primary source; prior_day/week levels
    # only enter the candidate pool when one side comes back empty after
    # pivot detection. The "second-chance" path further down (see
    # `if not support_candidates:` below) covers the strong-directional case
    # by re-injecting prior levels with the safe fallback_reference_price as
    # the side filter — that's where they belong.
    support_references: list[HTFLevel] = list(pivot_supports)
    resistance_references: list[HTFLevel] = list(pivot_resistances)
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
            support_references = _cluster_levels([(pd.Timestamp(frame.index[min_low_pos]), float(frame["low"].iloc[min_low_pos]))], "support", tolerance, int(max_levels_per_side * 2))
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
            resistance_references = _cluster_levels([(pd.Timestamp(frame.index[max_high_pos]), float(frame["high"].iloc[max_high_pos]))], "resistance", tolerance, int(max_levels_per_side * 2))

    eps = max(abs(float(close)) * 1e-6, 1e-8)
    flip_bars = max(0, int(flip_confirmation_bars or 0))
    flip_eps = max(abs(close) * 1e-6, 1e-8)

    support_candidates: list[HTFLevel] = []
    resistance_candidates: list[HTFLevel] = []

    for level in support_references:
        if _htf_flip_active(
            frame,
            float(level.price),
            timeframe_minutes=int(timeframe_minutes),
            confirm_bars=flip_bars,
            direction="loss",
            eps=flip_eps,
        ):
            resistance_candidates.append(_clone_level(level, "resistance", source="broken_htf_support"))
        else:
            support_candidates.append(_clone_level(level, "support"))

    for level in resistance_references:
        if _htf_flip_active(
            frame,
            float(level.price),
            timeframe_minutes=int(timeframe_minutes),
            confirm_bars=flip_bars,
            direction="reclaim",
            eps=flip_eps,
        ):
            support_candidates.append(_clone_level(level, "support", source="broken_htf_resistance"))
        else:
            resistance_candidates.append(_clone_level(level, "resistance"))

    support_candidates = [lv for lv in support_candidates if float(lv.price) <= float(support_filter_price) + eps]
    resistance_candidates = [lv for lv in resistance_candidates if float(lv.price) >= float(resistance_filter_price) - eps]

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
                tolerance=tolerance,
                max_levels=int(max_levels_per_side * 2),
            )
        extend_unique_levels(support_references, second_chance_support_refs)
        for level in second_chance_support_refs:
            if _htf_flip_active(
                frame,
                float(level.price),
                timeframe_minutes=int(timeframe_minutes),
                confirm_bars=flip_bars,
                direction="loss",
                eps=flip_eps,
            ):
                resistance_candidates.append(_clone_level(level, "resistance", source="broken_htf_support"))
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
                tolerance=tolerance,
                max_levels=int(max_levels_per_side * 2),
            )
        extend_unique_levels(resistance_references, second_chance_resistance_refs)
        for level in second_chance_resistance_refs:
            if _htf_flip_active(
                frame,
                float(level.price),
                timeframe_minutes=int(timeframe_minutes),
                confirm_bars=flip_bars,
                direction="reclaim",
                eps=flip_eps,
            ):
                support_candidates.append(_clone_level(level, "support", source="broken_htf_resistance"))
            else:
                resistance_candidates.append(_clone_level(level, "resistance"))

    support_candidates = [lv for lv in support_candidates if float(lv.price) <= float(support_filter_price) + eps]
    resistance_candidates = [lv for lv in resistance_candidates if float(lv.price) >= float(resistance_filter_price) - eps]
    supports = _collapse_same_side_levels(support_candidates, collapse_tolerance, close, reverse=True, max_levels=int(max_levels_per_side))
    resistances = _collapse_same_side_levels(resistance_candidates, collapse_tolerance, close, reverse=False, max_levels=int(max_levels_per_side))

    nearest_support = supports[0] if supports else None
    broken_resistance_candidates = [
        _clone_level(lv, "support", source="broken_htf_resistance")
        for lv in resistance_references
        if lv.price <= resistance_filter_price + eps
        and _htf_flip_active(
            frame,
            float(lv.price),
            timeframe_minutes=int(timeframe_minutes),
            confirm_bars=flip_bars,
            direction="reclaim",
            eps=flip_eps,
        )
    ]
    broken_resistance_levels = _collapse_same_side_levels(
        broken_resistance_candidates,
        collapse_tolerance,
        close,
        reverse=True,
        max_levels=int(max_levels_per_side),
    )
    broken_resistance = broken_resistance_levels[0] if broken_resistance_levels else None
    nearest_resistance = resistances[0] if resistances else None
    broken_support_candidates = [
        _clone_level(lv, "resistance", source="broken_htf_support")
        for lv in support_references
        if lv.price >= support_filter_price - eps
        and _htf_flip_active(
            frame,
            float(lv.price),
            timeframe_minutes=int(timeframe_minutes),
            confirm_bars=flip_bars,
            direction="loss",
            eps=flip_eps,
        )
    ]
    broken_support_levels = _collapse_same_side_levels(
        broken_support_candidates,
        collapse_tolerance,
        close,
        reverse=False,
        max_levels=int(max_levels_per_side),
    )
    broken_support = broken_support_levels[0] if broken_support_levels else None

    ema_fast = float(frame["close"].ewm(span=int(ema_fast_span), adjust=False).mean().iloc[-1]) if len(frame) >= max(5, int(ema_fast_span) // 3) else None
    ema_slow = float(frame["close"].ewm(span=int(ema_slow_span), adjust=False).mean().iloc[-1]) if len(frame) >= int(ema_slow_span) else None

    trend_votes = 0
    if ema_fast is not None:
        if close > ema_fast:
            trend_votes += 1
        elif close < ema_fast:
            trend_votes -= 1
    if ema_fast is not None and ema_slow is not None:
        if ema_fast > ema_slow:
            trend_votes += 1
        elif ema_fast < ema_slow:
            trend_votes -= 1
    if nearest_support is not None and nearest_resistance is not None:
        support_gap = close - float(nearest_support.price)
        resistance_gap = float(nearest_resistance.price) - close
        if resistance_gap > support_gap:
            trend_votes += 1
        elif support_gap > resistance_gap:
            trend_votes -= 1
    trend_bias = "bullish" if trend_votes >= 2 else ("bearish" if trend_votes <= -2 else "neutral")

    bullish_fvgs: list[HTFFairValueGap] = []
    bearish_fvgs: list[HTFFairValueGap] = []
    nearest_bullish_fvg: HTFFairValueGap | None = None
    nearest_bearish_fvg: HTFFairValueGap | None = None
    if bool(include_fair_value_gaps):
        bullish_fvgs, bearish_fvgs, nearest_bullish_fvg, nearest_bearish_fvg = _detect_fair_value_gaps(
            frame,
            timeframe_minutes=int(timeframe_minutes),
            current_price=close,
            max_per_side=max(0, int(fair_value_gap_max_per_side or 0)),
            min_gap_atr_mult=float(fair_value_gap_min_atr_mult),
            min_gap_pct=float(fair_value_gap_min_pct),
        )

    # HTF RSI divergence — multi-timeframe confluence signal. Uses
    # include_idx=True pivots so the shared find_divergence can pull RSI
    # values at exact pivot positions and tag age in HTF bars. RSI series is
    # whatever ensure_standard_indicator_frame populated under "rsi14".
    bullish_rsi_div: DivergenceMatch | None = None
    bearish_rsi_div: DivergenceMatch | None = None
    bullish_hidden_rsi_div: DivergenceMatch | None = None
    bearish_hidden_rsi_div: DivergenceMatch | None = None
    if divergence_enabled and "rsi14" in frame.columns and len(frame) > 0:
        rsi_series = frame["rsi14"].astype(float)
        if not rsi_series.dropna().empty:
            highs_idx, lows_idx = pivot_points(frame, int(pivot_span), include_idx=True)
            last_bar_pos = max(0, len(frame) - 1)
            lookback = max(2, int(divergence_pivot_lookback))
            max_age = max(0, int(divergence_max_age_bars))
            move_frac = max(0.0001, float(divergence_min_price_move_pct))
            rsi_delta = max(0.0, float(divergence_rsi_min_delta))
            bullish_rsi_div = find_divergence(
                lows_idx, rsi_series, kind="regular", direction="bullish",
                indicator_name="rsi", price_move_frac=move_frac,
                indicator_delta=rsi_delta, pivot_lookback=lookback,
                max_age_bars=max_age, last_bar_pos=last_bar_pos,
            )
            bearish_rsi_div = find_divergence(
                highs_idx, rsi_series, kind="regular", direction="bearish",
                indicator_name="rsi", price_move_frac=move_frac,
                indicator_delta=rsi_delta, pivot_lookback=lookback,
                max_age_bars=max_age, last_bar_pos=last_bar_pos,
            )
            bullish_hidden_rsi_div = find_divergence(
                lows_idx, rsi_series, kind="hidden", direction="bullish",
                indicator_name="rsi", price_move_frac=move_frac,
                indicator_delta=rsi_delta, pivot_lookback=lookback,
                max_age_bars=max_age, last_bar_pos=last_bar_pos,
            )
            bearish_hidden_rsi_div = find_divergence(
                highs_idx, rsi_series, kind="hidden", direction="bearish",
                indicator_name="rsi", price_move_frac=move_frac,
                indicator_delta=rsi_delta, pivot_lookback=lookback,
                max_age_bars=max_age, last_bar_pos=last_bar_pos,
            )

    return HTFContext(
        timeframe_minutes=int(timeframe_minutes),
        current_price=close,
        supports=supports,
        resistances=resistances,
        nearest_support=nearest_support,
        broken_resistance=broken_resistance,
        nearest_resistance=nearest_resistance,
        broken_support=broken_support,
        prior_day_high=prior_day_high,
        prior_day_low=prior_day_low,
        prior_week_high=prior_week_high,
        prior_week_low=prior_week_low,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        atr14=atr,
        bullish_fvgs=bullish_fvgs,
        bearish_fvgs=bearish_fvgs,
        nearest_bullish_fvg=nearest_bullish_fvg,
        nearest_bearish_fvg=nearest_bearish_fvg,
        trend_bias=trend_bias,
        level_buffer=max(atr * float(stop_buffer_atr_mult), close * 0.0010),
        bullish_rsi_divergence=bullish_rsi_div,
        bearish_rsi_divergence=bearish_rsi_div,
        bullish_hidden_rsi_divergence=bullish_hidden_rsi_div,
        bearish_hidden_rsi_divergence=bearish_hidden_rsi_div,
    )
