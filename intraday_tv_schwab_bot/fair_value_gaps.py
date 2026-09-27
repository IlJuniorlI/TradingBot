# SPDX-License-Identifier: MIT
"""Fair value gaps: the range between the first and third bar of a three-bar
run that neither of them traded, on any timeframe. The HTF context carries
its timeframe's gaps (``htf_levels.build_htf_context``); the strategies and
the dashboard build the LTF ones (``build_fair_value_gap_context``). The
zone arithmetic the gaps share with order blocks is in ``zones``."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .bars import completed_bars, ensure_ohlcv_frame, resolve_current_price
from .indicators import atr_with_floor, ensure_standard_indicator_frame
from .sessions import session_segment_ids
from .zones import zone_distance, zone_filled_pct, zone_sizing


@dataclass(slots=True)
class HTFFairValueGap:
    direction: str
    lower: float
    upper: float
    midpoint: float
    size: float
    # first_seen: the formation triplet's first bar. last_seen: the last
    # completed bar that traded INTO the gap, or the triplet's last bar (the
    # one that completed it) when none has (see detect_fair_value_gaps).
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


def empty_fvg_context(current_price: float = 0.0, *, timeframe_minutes: int = 1) -> FairValueGapContext:
    return FairValueGapContext(timeframe_minutes=int(timeframe_minutes), current_price=float(current_price or 0.0))


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
        # The stamps are isoformat labels of the completed frame's
        # DatetimeIndex (detect_fair_value_gaps' _stamp).
        parsed = pd.Timestamp(value)
        return parsed.tz_convert(None) if parsed.tzinfo is not None else parsed

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


def detect_fair_value_gaps(
    frame: pd.DataFrame,
    *,
    timeframe_minutes: int,
    current_price: float,
    max_per_side: int,
    min_gap_atr_mult: float,
    min_gap_pct: float,
) -> tuple[list[HTFFairValueGap], list[HTFFairValueGap], HTFFairValueGap | None, HTFFairValueGap | None]:
    completed = completed_bars(frame, timeframe_minutes)
    if completed is None or completed.empty or len(completed) < 3:
        return [], [], None, None
    completed = ensure_ohlcv_frame(completed)
    if completed.empty or len(completed) < 3:
        return [], [], None, None
    ref_close = resolve_current_price(completed, current_price)
    atr = atr_with_floor(completed, ref_close, abs_floor=0.01)
    min_gap_size, eps, merge_tol = zone_sizing(atr, ref_close, min_atr_mult=min_gap_atr_mult, min_pct=min_gap_pct)
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
    # A triplet must lie inside one ET session. The frames hold no 20:00-07:00
    # bars, so until 2026-09-23 [D-1 19:30, D-1 19:45, D 07:00] and its
    # neighbours registered any overnight move as an "unfilled" gap that the
    # 24/5 session had in fact traded through -- in the HTF lists at 31% of
    # archived RTH checkpoints and the nearest, scored gap at 15% (AMZN
    # 2026-09-18: bullish 250.65-252.36 anchored 09-17 19:30 moved the LONG
    # fvg_entry_adjustment from 0.0 to +0.17). The 1m frame had the same seam
    # at 19:59 -> 07:00 every day.
    segments = session_segment_ids(index_values)

    def _stamp(pos: int) -> str:
        label = index_values[pos]
        return label.isoformat() if hasattr(label, "isoformat") else str(label)

    def _last_touch(pos: int, touched: np.ndarray) -> str:
        """Last completed bar after the formation triplet ending at ``pos``
        that traded into the gap (``touched`` flags every bar that did), or
        the triplet's last bar, the one that completed the gap, when none
        has: stamped at its first bar, a brand-new gap started its recency
        decay two bars old. Until 2026-09-23 every gap's last_seen was the
        frame's last bar, so _score_fvg_context's recency decay never
        applied: a gap 460 HTF bars old (AMZN, first seen 2026-09-17 19:30)
        scored recency 0.93, the same as one formed an hour earlier, instead
        of decaying to the 0.30 floor."""
        hits = np.flatnonzero(touched[pos + 1:])
        return _stamp(pos + 1 + int(hits[-1])) if hits.size else _stamp(pos)

    for idx in range(2, n):
        if high_arr is None or low_arr is None:
            break
        if segments[idx - 2] != segments[idx]:
            continue
        # No NaN substitution. ensure_ohlcv_frame above has already dropped
        # NaN OHLC rows, and a NaN that did get here fails both comparisons
        # below and yields no gap -- the safe outcome. The old guard replaced
        # NaN with 0.0, which would have made `right_low > 0 + eps` true and
        # manufactured a bullish gap spanning from zero up to price.
        left_high = float(high_arr[idx - 2])
        left_low = float(low_arr[idx - 2])
        right_low = float(low_arr[idx])
        right_high = float(high_arr[idx])
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
                    bullish_raw.append(
                        HTFFairValueGap(
                            direction="bullish",
                            lower=lower,
                            upper=upper,
                            midpoint=(lower + upper) / 2.0,
                            size=size,
                            first_seen=_stamp(idx - 2),
                            last_seen=_last_touch(idx, low_arr < upper),
                            filled_pct=zone_filled_pct(lower, upper, min_low_after, bullish=True),
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
                    bearish_raw.append(
                        HTFFairValueGap(
                            direction="bearish",
                            lower=lower,
                            upper=upper,
                            midpoint=(lower + upper) / 2.0,
                            size=size,
                            first_seen=_stamp(idx - 2),
                            last_seen=_last_touch(idx, high_arr > lower),
                            filled_pct=zone_filled_pct(lower, upper, max_high_after, bullish=False),
                        )
                    )
    bullish = _merge_fair_value_gaps(bullish_raw, tolerance=merge_tol, timeframe_minutes=timeframe_minutes)
    bearish = _merge_fair_value_gaps(bearish_raw, tolerance=merge_tol, timeframe_minutes=timeframe_minutes)
    bullish.sort(key=lambda gap: (zone_distance(gap.lower, gap.upper, ref_close), -float(gap.upper)))
    bearish.sort(key=lambda gap: (zone_distance(gap.lower, gap.upper, ref_close), float(gap.lower)))
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
    base = ensure_standard_indicator_frame(ensure_ohlcv_frame(frame))
    if base.empty:
        return empty_fvg_context(float(current_price or 0.0), timeframe_minutes=timeframe_minutes)
    close = resolve_current_price(base, current_price)
    bullish_fvgs, bearish_fvgs, nearest_bullish_fvg, nearest_bearish_fvg = detect_fair_value_gaps(
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
