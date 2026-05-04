# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from typing import Iterable

import numpy as np
import pandas as pd

from .utils import (
    atr_value,
    ensure_ohlcv_frame,
    ensure_standard_indicator_frame,
    resolve_current_price,
    talib_obv,
)


LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class TechnicalLine:
    kind: str
    slope: float
    intercept: float
    touches: int
    start_pos: int
    end_pos: int
    current_value: float
    direction: str = "neutral"


@dataclass(slots=True)
class ChannelContext:
    valid: bool = False
    bias: str = "neutral"
    lower: float | None = None
    upper: float | None = None
    mid: float | None = None
    position_pct: float | None = None
    width: float | None = None
    lower_touches: int = 0
    upper_touches: int = 0
    lower_line: TechnicalLine | None = None
    upper_line: TechnicalLine | None = None
    mid_line: TechnicalLine | None = None


class TechnicalLevelsContext:
    __slots__ = (
        "current_price",
        "fib_direction",
        "fib_anchor_low",
        "fib_anchor_high",
        "fib_bullish_1272",
        "fib_bullish_1618",
        "fib_bearish_1272",
        "fib_bearish_1618",
        "nearest_bullish_extension",
        "nearest_bullish_extension_ratio",
        "bullish_extension_distance_pct",
        "nearest_bearish_extension",
        "nearest_bearish_extension_ratio",
        "bearish_extension_distance_pct",
        "anchored_vwap_open",
        "anchored_vwap_bullish_impulse",
        "anchored_vwap_bearish_impulse",
        "anchored_vwap_bias",
        "adx",
        "plus_di",
        "minus_di",
        "dmi_bias",
        "adx_rising",
        "atr14",
        "atr_pct",
        "atr_expansion_mult",
        "atr_stretch_vwap_mult",
        "atr_stretch_ema20_mult",
        "obv",
        "obv_ema",
        "obv_bias",
        "rsi14",
        "bullish_rsi_divergence",
        "bearish_rsi_divergence",
        "bullish_obv_divergence",
        "bearish_obv_divergence",
        "counter_divergence_bias",
        "bollinger_mid",
        "bollinger_upper",
        "bollinger_lower",
        "bollinger_width",
        "bollinger_width_pct",
        "bollinger_percent_b",
        "bollinger_zscore",
        "bollinger_squeeze",
        "bollinger_upper_reject",
        "bollinger_lower_reject",
        "support_trendline",
        "resistance_trendline",
        "channel",
        "trendline_break_up",
        "trendline_break_down",
        "support_respected",
        "resistance_respected",
        "support_distance_pct",
        "resistance_distance_pct",
        "reason",
    )

    current_price: float
    fib_direction: str
    fib_anchor_low: float | None
    fib_anchor_high: float | None
    fib_bullish_1272: float | None
    fib_bullish_1618: float | None
    fib_bearish_1272: float | None
    fib_bearish_1618: float | None
    nearest_bullish_extension: float | None
    nearest_bullish_extension_ratio: float | None
    bullish_extension_distance_pct: float | None
    nearest_bearish_extension: float | None
    nearest_bearish_extension_ratio: float | None
    bearish_extension_distance_pct: float | None
    anchored_vwap_open: float | None
    anchored_vwap_bullish_impulse: float | None
    anchored_vwap_bearish_impulse: float | None
    anchored_vwap_bias: str
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    dmi_bias: str
    adx_rising: bool
    atr14: float | None
    atr_pct: float | None
    atr_expansion_mult: float | None
    atr_stretch_vwap_mult: float | None
    atr_stretch_ema20_mult: float | None
    obv: float | None
    obv_ema: float | None
    obv_bias: str
    rsi14: float | None
    bullish_rsi_divergence: bool
    bearish_rsi_divergence: bool
    bullish_obv_divergence: bool
    bearish_obv_divergence: bool
    counter_divergence_bias: str
    bollinger_mid: float | None
    bollinger_upper: float | None
    bollinger_lower: float | None
    bollinger_width: float | None
    bollinger_width_pct: float | None
    bollinger_percent_b: float | None
    bollinger_zscore: float | None
    bollinger_squeeze: bool
    bollinger_upper_reject: bool
    bollinger_lower_reject: bool
    support_trendline: TechnicalLine | None
    resistance_trendline: TechnicalLine | None
    channel: ChannelContext
    trendline_break_up: bool
    trendline_break_down: bool
    support_respected: bool
    resistance_respected: bool
    support_distance_pct: float | None
    resistance_distance_pct: float | None
    reason: str

    def __init__(
        self,
        current_price: float,
        *,
        fib_direction: str = "neutral",
        fib_anchor_low: float | None = None,
        fib_anchor_high: float | None = None,
        fib_bullish_1272: float | None = None,
        fib_bullish_1618: float | None = None,
        fib_bearish_1272: float | None = None,
        fib_bearish_1618: float | None = None,
        nearest_bullish_extension: float | None = None,
        nearest_bullish_extension_ratio: float | None = None,
        bullish_extension_distance_pct: float | None = None,
        nearest_bearish_extension: float | None = None,
        nearest_bearish_extension_ratio: float | None = None,
        bearish_extension_distance_pct: float | None = None,
        anchored_vwap_open: float | None = None,
        anchored_vwap_bullish_impulse: float | None = None,
        anchored_vwap_bearish_impulse: float | None = None,
        anchored_vwap_bias: str = "neutral",
        adx: float | None = None,
        plus_di: float | None = None,
        minus_di: float | None = None,
        dmi_bias: str = "neutral",
        adx_rising: bool = False,
        atr14: float | None = None,
        atr_pct: float | None = None,
        atr_expansion_mult: float | None = None,
        atr_stretch_vwap_mult: float | None = None,
        atr_stretch_ema20_mult: float | None = None,
        obv: float | None = None,
        obv_ema: float | None = None,
        obv_bias: str = "neutral",
        rsi14: float | None = None,
        bullish_rsi_divergence: bool = False,
        bearish_rsi_divergence: bool = False,
        bullish_obv_divergence: bool = False,
        bearish_obv_divergence: bool = False,
        counter_divergence_bias: str = "neutral",
        bollinger_mid: float | None = None,
        bollinger_upper: float | None = None,
        bollinger_lower: float | None = None,
        bollinger_width: float | None = None,
        bollinger_width_pct: float | None = None,
        bollinger_percent_b: float | None = None,
        bollinger_zscore: float | None = None,
        bollinger_squeeze: bool = False,
        bollinger_upper_reject: bool = False,
        bollinger_lower_reject: bool = False,
        support_trendline: TechnicalLine | None = None,
        resistance_trendline: TechnicalLine | None = None,
        channel: ChannelContext | None = None,
        trendline_break_up: bool = False,
        trendline_break_down: bool = False,
        support_respected: bool = False,
        resistance_respected: bool = False,
        support_distance_pct: float | None = None,
        resistance_distance_pct: float | None = None,
        reason: str = "ok",
    ) -> None:
        self.current_price = float(current_price)
        self.fib_direction = fib_direction
        self.fib_anchor_low = fib_anchor_low
        self.fib_anchor_high = fib_anchor_high
        self.fib_bullish_1272 = fib_bullish_1272
        self.fib_bullish_1618 = fib_bullish_1618
        self.fib_bearish_1272 = fib_bearish_1272
        self.fib_bearish_1618 = fib_bearish_1618
        self.nearest_bullish_extension = nearest_bullish_extension
        self.nearest_bullish_extension_ratio = nearest_bullish_extension_ratio
        self.bullish_extension_distance_pct = bullish_extension_distance_pct
        self.nearest_bearish_extension = nearest_bearish_extension
        self.nearest_bearish_extension_ratio = nearest_bearish_extension_ratio
        self.bearish_extension_distance_pct = bearish_extension_distance_pct
        self.anchored_vwap_open = anchored_vwap_open
        self.anchored_vwap_bullish_impulse = anchored_vwap_bullish_impulse
        self.anchored_vwap_bearish_impulse = anchored_vwap_bearish_impulse
        self.anchored_vwap_bias = anchored_vwap_bias
        self.adx = adx
        self.plus_di = plus_di
        self.minus_di = minus_di
        self.dmi_bias = dmi_bias
        self.adx_rising = adx_rising
        self.atr14 = atr14
        self.atr_pct = atr_pct
        self.atr_expansion_mult = atr_expansion_mult
        self.atr_stretch_vwap_mult = atr_stretch_vwap_mult
        self.atr_stretch_ema20_mult = atr_stretch_ema20_mult
        self.obv = obv
        self.obv_ema = obv_ema
        self.obv_bias = obv_bias
        self.rsi14 = rsi14
        self.bullish_rsi_divergence = bullish_rsi_divergence
        self.bearish_rsi_divergence = bearish_rsi_divergence
        self.bullish_obv_divergence = bullish_obv_divergence
        self.bearish_obv_divergence = bearish_obv_divergence
        self.counter_divergence_bias = counter_divergence_bias
        self.bollinger_mid = bollinger_mid
        self.bollinger_upper = bollinger_upper
        self.bollinger_lower = bollinger_lower
        self.bollinger_width = bollinger_width
        self.bollinger_width_pct = bollinger_width_pct
        self.bollinger_percent_b = bollinger_percent_b
        self.bollinger_zscore = bollinger_zscore
        self.bollinger_squeeze = bollinger_squeeze
        self.bollinger_upper_reject = bollinger_upper_reject
        self.bollinger_lower_reject = bollinger_lower_reject
        self.support_trendline = support_trendline
        self.resistance_trendline = resistance_trendline
        self.channel = channel if channel is not None else ChannelContext()
        self.trendline_break_up = trendline_break_up
        self.trendline_break_down = trendline_break_down
        self.support_respected = support_respected
        self.resistance_respected = resistance_respected
        self.support_distance_pct = support_distance_pct
        self.resistance_distance_pct = resistance_distance_pct
        self.reason = reason


def empty_technical_levels_context(current_price: float = 0.0) -> TechnicalLevelsContext:
    return TechnicalLevelsContext(current_price=float(current_price or 0.0), reason="disabled")

def _pivot_points(frame: pd.DataFrame, span: int) -> tuple[list[tuple[int, pd.Timestamp, float]], list[tuple[int, pd.Timestamp, float]]]:
    highs: list[tuple[int, pd.Timestamp, float]] = []
    lows: list[tuple[int, pd.Timestamp, float]] = []
    if frame is None or len(frame) < (span * 2 + 3):
        return highs, lows
    span = max(1, int(span))
    highs_arr = frame["high"].astype(float).tolist()
    lows_arr = frame["low"].astype(float).tolist()
    idxs = list(frame.index)
    for i in range(span, len(frame) - span):
        hi = highs_arr[i]
        lo = lows_arr[i]
        hi_window = highs_arr[i - span : i + span + 1]
        lo_window = lows_arr[i - span : i + span + 1]
        if hi == max(hi_window) and hi_window.count(hi) == 1:
            highs.append((i, idxs[i], float(hi)))
        if lo == min(lo_window) and lo_window.count(lo) == 1:
            lows.append((i, idxs[i], float(lo)))
    return highs, lows


def _reduced_pivots(
    frame: pd.DataFrame,
    span: int,
    *,
    highs: list[tuple[int, pd.Timestamp, float]] | None = None,
    lows: list[tuple[int, pd.Timestamp, float]] | None = None,
) -> list[tuple[str, int, pd.Timestamp, float]]:
    # Accept pre-computed pivots from the caller so we don't recompute them
    # when the caller already has (highs, lows) from _pivot_points.
    if highs is None or lows is None:
        highs, lows = _pivot_points(frame, span)
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
        prev_kind, _prev_pos, _prev_ts, prev_price = reduced[-1]
        if kind != prev_kind:
            reduced.append((kind, pos, ts, price))
            continue
        keep_current = price >= prev_price if kind == "H" else price <= prev_price
        if keep_current:
            reduced[-1] = (kind, pos, ts, price)
    return reduced


def _line_value(slope: float, intercept: float, pos: int) -> float:
    return float((slope * float(pos)) + intercept)


def _line_direction(slope: float, tol: float) -> str:
    if slope > tol:
        return "up"
    if slope < -tol:
        return "down"
    return "flat"


def _build_best_line(
    points: Iterable[tuple[int, pd.Timestamp, float]],
    *,
    kind: str,
    current_pos: int,
    tolerance: float,
    min_touches: int,
    max_candidates: int = 7,
) -> TechnicalLine | None:
    pts = sorted([(int(pos), ts, float(price)) for pos, ts, price in points], key=lambda x: x[0])
    if len(pts) < 2:
        return None
    pts = pts[-max_candidates:]
    best: tuple[float, TechnicalLine] | None = None
    slope_tol = max(1e-9, tolerance / max(10.0, float(max(1, current_pos))))
    for i in range(len(pts) - 1):
        p1, _, y1 = pts[i]
        for j in range(i + 1, len(pts)):
            p2, _, y2 = pts[j]
            if p2 <= p1:
                continue
            slope = (y2 - y1) / float(p2 - p1)
            intercept = y1 - (slope * float(p1))
            touches = 0
            last_touch_pos = p2
            for pos, _ts, price in pts[i:]:
                line_px = _line_value(slope, intercept, pos)
                if abs(price - line_px) <= tolerance:
                    touches += 1
                    last_touch_pos = pos
            if touches < int(min_touches):
                continue
            current_value = _line_value(slope, intercept, current_pos)
            direction = _line_direction(slope, slope_tol)
            line = TechnicalLine(
                kind=kind,
                slope=float(slope),
                intercept=float(intercept),
                touches=int(touches),
                start_pos=int(p1),
                end_pos=int(last_touch_pos),
                current_value=float(current_value),
                direction=direction,
            )
            span = max(1, line.end_pos - line.start_pos)
            recency = 1.0 / max(1.0, float(current_pos - line.end_pos + 1))
            score = float(touches) * 3.0 + min(2.0, span / 20.0) + recency
            if best is None or score > best[0]:
                best = (score, line)
    return best[1] if best is not None else None


def _line_has_material_slope_over_span(
    line: TechnicalLine | None,
    *,
    start_pos: int,
    end_pos: int,
    tolerance: float,
) -> bool:
    if line is None:
        return False
    try:
        start_pos = int(start_pos)
        end_pos = int(end_pos)
        if end_pos <= start_pos:
            return False
        start_value = _line_value(float(line.slope), float(line.intercept), start_pos)
        end_value = _line_value(float(line.slope), float(line.intercept), end_pos)
        return abs(float(end_value) - float(start_value)) > max(float(tolerance), 1e-9)
    except Exception:
        return False


def _trendline_has_material_slope(line: TechnicalLine | None, *, current_pos: int, tolerance: float) -> bool:
    if line is None:
        return False
    start_pos = int(getattr(line, "start_pos", 0) or 0)
    return _line_has_material_slope_over_span(
        line,
        start_pos=start_pos,
        end_pos=int(current_pos),
        tolerance=float(tolerance),
    )


def _build_channel(
    support_line: TechnicalLine | None,
    resistance_line: TechnicalLine | None,
    *,
    frame: pd.DataFrame | None,
    current_price: float,
    current_pos: int,
    tolerance: float,
    parallel_slope_frac: float,
    min_gap_abs: float,
) -> ChannelContext:
    if support_line is None or resistance_line is None:
        return ChannelContext(valid=False)
    lower = float(support_line.current_value)
    upper = float(resistance_line.current_value)
    if upper <= lower:
        return ChannelContext(valid=False)
    slope_a = float(support_line.slope)
    slope_b = float(resistance_line.slope)
    max_abs = max(abs(slope_a), abs(slope_b), 1e-9)
    if slope_a * slope_b < 0:
        return ChannelContext(valid=False)
    slope_diff = abs(slope_a - slope_b)
    if slope_diff > max_abs * max(0.02, float(parallel_slope_frac)):
        return ChannelContext(valid=False)
    overlap_start = int(max(support_line.start_pos, resistance_line.start_pos))
    overlap_end = int(max(overlap_start, min(support_line.end_pos, resistance_line.end_pos, current_pos)))
    if not _line_has_material_slope_over_span(support_line, start_pos=overlap_start, end_pos=overlap_end, tolerance=tolerance):
        return ChannelContext(valid=False)
    if not _line_has_material_slope_over_span(resistance_line, start_pos=overlap_start, end_pos=overlap_end, tolerance=tolerance):
        return ChannelContext(valid=False)
    overlap_span = max(1, overlap_end - overlap_start)
    min_overlap_span = max(8, min(24, max(int(support_line.touches), int(resistance_line.touches)) * 3))
    if overlap_span < min_overlap_span:
        return ChannelContext(valid=False)
    width_start = _line_value(slope_b, float(resistance_line.intercept), overlap_start) - _line_value(slope_a, float(support_line.intercept), overlap_start)
    width_end = _line_value(slope_b, float(resistance_line.intercept), overlap_end) - _line_value(slope_a, float(support_line.intercept), overlap_end)
    width_now = upper - lower
    width_floor = max(float(min_gap_abs), float(tolerance), current_price * 0.0010)
    if min(width_start, width_end, width_now) <= width_floor:
        return ChannelContext(valid=False)
    width_ceiling = max(width_floor * 8.0, current_price * 0.12)
    if max(width_start, width_end, width_now) >= width_ceiling:
        return ChannelContext(valid=False)
    width_ratio = max(width_start, width_end, width_now) / max(min(width_start, width_end, width_now), 1e-9)
    if width_ratio > 1.45:
        return ChannelContext(valid=False)

    if frame is not None and not frame.empty:
        eval_start = max(overlap_start, len(frame) - min(40, len(frame)))
        eval_end = min(current_pos, len(frame) - 1)
        if eval_end <= eval_start:
            return ChannelContext(valid=False)
        break_tolerance = max(float(tolerance) * 1.35, current_price * 0.0015)
        bars_checked = 0
        bars_inside = 0
        decisive_breaches = 0
        recent_break = False
        recent_window = max(4, min(8, eval_end - eval_start + 1))
        for pos in range(eval_start, eval_end + 1):
            close_value = float(frame.iloc[pos]["close"])
            lower_bound = _line_value(slope_a, float(support_line.intercept), pos)
            upper_bound = _line_value(slope_b, float(resistance_line.intercept), pos)
            bars_checked += 1
            if lower_bound - tolerance <= close_value <= upper_bound + tolerance:
                bars_inside += 1
                continue
            if close_value < lower_bound - break_tolerance or close_value > upper_bound + break_tolerance:
                decisive_breaches += 1
                if pos >= eval_end - recent_window + 1:
                    recent_break = True
        respect_ratio = (bars_inside / float(bars_checked)) if bars_checked > 0 else 0.0
        if respect_ratio < 0.72:
            return ChannelContext(valid=False)
        if decisive_breaches > 1 or recent_break:
            return ChannelContext(valid=False)

    width = width_now
    position_pct = (current_price - lower) / width if width > 0 else None
    avg_slope = (slope_a + slope_b) / 2.0
    avg_intercept = (float(support_line.intercept) + float(resistance_line.intercept)) / 2.0
    if avg_slope > 0:
        bias = "bullish"
    elif avg_slope < 0:
        bias = "bearish"
    else:
        bias = "neutral"
    mid_slope_tol = max(1e-9, tolerance / max(10.0, float(max(1, max(support_line.end_pos, resistance_line.end_pos)))))
    mid_line = TechnicalLine(
        kind="channel_mid",
        slope=float(avg_slope),
        intercept=float(avg_intercept),
        touches=int(min(support_line.touches, resistance_line.touches)),
        start_pos=int(min(support_line.start_pos, resistance_line.start_pos)),
        end_pos=int(max(support_line.end_pos, resistance_line.end_pos)),
        current_value=float((lower + upper) / 2.0),
        direction=_line_direction(avg_slope, mid_slope_tol),
    )
    return ChannelContext(
        valid=True,
        bias=bias,
        lower=float(lower),
        upper=float(upper),
        mid=float((lower + upper) / 2.0),
        position_pct=None if position_pct is None else float(position_pct),
        width=float(width),
        lower_touches=int(support_line.touches),
        upper_touches=int(resistance_line.touches),
        lower_line=support_line,
        upper_line=resistance_line,
        mid_line=mid_line,
    )


def _last_impulse_segment(
    pivots: list[tuple[str, int, pd.Timestamp, float]],
    direction: str,
    *,
    min_range: float,
    lookback_start_pos: int | None = None,
    current_pos: int | None = None,
    max_end_age_bars: int | None = None,
) -> tuple[int, int, float, float] | None:
    if len(pivots) < 2:
        return None
    start_cutoff = max(0, int(lookback_start_pos)) if lookback_start_pos is not None else 0
    inferred_current_pos = max(int(pos) for _kind, pos, _ts, _value in pivots)
    current_bar_pos = int(current_pos) if current_pos is not None else inferred_current_pos
    age_limit = max(1, int(max_end_age_bars)) if max_end_age_bars is not None else None
    denom = max(float(min_range), 1e-9)
    candidates: list[tuple[float, int, int, int, float, float]] = []
    for idx in range(1, len(pivots)):
        k1, p1, _ts1, v1 = pivots[idx - 1]
        k2, p2, _ts2, v2 = pivots[idx]
        if direction == "bullish":
            if k1 != "L" or k2 != "H":
                continue
            start_pos = int(p1)
            end_pos = int(p2)
            start_value = float(v1)
            end_value = float(v2)
            range_abs = end_value - start_value
        else:
            if k1 != "H" or k2 != "L":
                continue
            start_pos = int(p1)
            end_pos = int(p2)
            start_value = float(v1)
            end_value = float(v2)
            range_abs = start_value - end_value
        if range_abs < min_range:
            continue
        if start_pos < start_cutoff or end_pos < start_cutoff or end_pos > current_bar_pos:
            continue
        age = max(0, current_bar_pos - end_pos)
        if age_limit is not None and age > age_limit:
            continue
        span = max(1, end_pos - start_pos)
        recency_window = max(1, age_limit if age_limit is not None else max(1, current_bar_pos - start_cutoff + 1))
        recency_bonus = 1.0 - min(1.0, age / float(recency_window))
        span_bonus = min(0.75, span / 20.0)
        score = (range_abs / denom) + span_bonus + (recency_bonus * 0.5)
        candidates.append((score, end_pos, span, start_pos, start_value, end_value))
    if not candidates:
        return None
    _score, end_pos, _span, start_pos, start_value, end_value = max(candidates, key=lambda item: (item[0], item[1], item[2], item[3]))
    return int(start_pos), int(end_pos), float(start_value), float(end_value)


def _anchored_vwap(frame: pd.DataFrame, start_pos: int) -> float | None:
    if frame is None or frame.empty:
        return None
    n = len(frame)
    start_pos = max(0, min(int(start_pos), n - 1))
    if start_pos >= n:
        return None
    # Drop to numpy once; skip .astype(float) because indicator frames store
    # OHLCV as float64 already. np.nan_to_num handles NaN volume in-place at
    # numpy speed, far faster than pandas .fillna().astype().
    vol_arr = np.nan_to_num(frame["volume"].to_numpy(dtype=np.float64, copy=False)[start_pos:], nan=0.0)
    vol_sum = float(vol_arr.sum())
    if vol_sum <= 0:
        return None
    high_arr = frame["high"].to_numpy(dtype=np.float64, copy=False)[start_pos:]
    low_arr = frame["low"].to_numpy(dtype=np.float64, copy=False)[start_pos:]
    close_arr = frame["close"].to_numpy(dtype=np.float64, copy=False)[start_pos:]
    typical = (high_arr + low_arr + close_arr) / 3.0
    return float((typical * vol_arr).sum() / vol_sum)


def _last_two_pivots(points: list[tuple[int, pd.Timestamp, float]]) -> tuple[tuple[int, pd.Timestamp, float], tuple[int, pd.Timestamp, float]] | None:
    if len(points) < 2:
        return None
    return points[-2], points[-1]


def _pivot_series_value(series: pd.Series, pos: int) -> float | None:
    if pos < 0 or pos >= len(series):
        return None
    value = series.iloc[pos]
    return None if pd.isna(value) else float(value)


def _bullish_divergence(points: list[tuple[int, pd.Timestamp, float]], indicator: pd.Series, *, price_move_frac: float, indicator_delta: float) -> bool:
    pair = _last_two_pivots(points)
    if pair is None:
        return False
    (pos1, _ts1, price1), (pos2, _ts2, price2) = pair
    ind1 = _pivot_series_value(indicator, pos1)
    ind2 = _pivot_series_value(indicator, pos2)
    if ind1 is None or ind2 is None:
        return False
    return bool(price2 < price1 * (1.0 - price_move_frac) and ind2 > ind1 + indicator_delta)


def _bearish_divergence(points: list[tuple[int, pd.Timestamp, float]], indicator: pd.Series, *, price_move_frac: float, indicator_delta: float) -> bool:
    pair = _last_two_pivots(points)
    if pair is None:
        return False
    (pos1, _ts1, price1), (pos2, _ts2, price2) = pair
    ind1 = _pivot_series_value(indicator, pos1)
    ind2 = _pivot_series_value(indicator, pos2)
    if ind1 is None or ind2 is None:
        return False
    return bool(price2 > price1 * (1.0 + price_move_frac) and ind2 < ind1 - indicator_delta)

def _populate_atr_context(
    ctx: TechnicalLevelsContext,
    frame: pd.DataFrame,
    *,
    close: float,
    atr_expansion_lookback: int,
) -> None:
    """Set ATR fields on ctx: atr14, atr_pct, atr_expansion_mult, atr_stretch_*.
    Extracted from build_technical_levels_context for Phase 3a decomposition."""
    last = frame.iloc[-1]
    ctx.atr14 = float(last["atr14"]) if pd.notna(last.get("atr14", math.nan)) else None
    ctx.atr_pct = (ctx.atr14 / close) if ctx.atr14 is not None and close > 0 else None
    if ctx.atr14 and ctx.atr14 > 0:
        expansion_lb = max(1, int(atr_expansion_lookback))
        if len(frame) > expansion_lb:
            prev_close = float(frame.iloc[-(expansion_lb + 1)]["close"])
            ctx.atr_expansion_mult = abs(close - prev_close) / ctx.atr14
        ema20 = float(last["ema20"]) if pd.notna(last.get("ema20", math.nan)) else None
        vwap = float(last["vwap"]) if pd.notna(last.get("vwap", math.nan)) else None
        if ema20 is not None:
            ctx.atr_stretch_ema20_mult = abs(close - ema20) / ctx.atr14
        if vwap is not None:
            ctx.atr_stretch_vwap_mult = abs(close - vwap) / ctx.atr14


def _populate_adx_context(
    ctx: TechnicalLevelsContext,
    frame: pd.DataFrame,
    *,
    adx_length: int,
) -> None:
    """Compute ADX / +DI / -DI / DMI bias / adx_rising on ctx.
    Fast path when adx_length==14 and precomputed columns exist; otherwise
    compute Wilder-smoothed DMI from scratch. Extracted from
    build_technical_levels_context for Phase 3a decomposition."""
    adx_period = max(5, int(adx_length))
    if adx_period == 14 and {"adx14", "plus_di14", "minus_di14"}.issubset(frame.columns):
        adx_series = frame["adx14"].astype(float)
        plus_di_series = frame["plus_di14"].astype(float)
        minus_di_series = frame["minus_di14"].astype(float)
    else:
        alpha = 1.0 / float(adx_period)
        up_move = frame["high"].diff()
        down_move = -frame["low"].diff()
        plus_dm = pd.Series(0.0, index=frame.index)
        minus_dm = pd.Series(0.0, index=frame.index)
        plus_mask = (up_move > down_move) & (up_move > 0)
        minus_mask = (down_move > up_move) & (down_move > 0)
        plus_dm.loc[plus_mask] = up_move.loc[plus_mask].astype(float)
        minus_dm.loc[minus_mask] = down_move.loc[minus_mask].astype(float)
        tr = pd.concat([
            frame["high"] - frame["low"],
            (frame["high"] - frame["close"].shift()).abs(),
            (frame["low"] - frame["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        tr_wilder = tr.ewm(alpha=alpha, adjust=False, min_periods=adx_period).mean()
        plus_dm_wilder = plus_dm.ewm(alpha=alpha, adjust=False, min_periods=adx_period).mean()
        minus_dm_wilder = minus_dm.ewm(alpha=alpha, adjust=False, min_periods=adx_period).mean()
        plus_di_series = 100.0 * (plus_dm_wilder / tr_wilder.replace(0.0, math.nan))
        minus_di_series = 100.0 * (minus_dm_wilder / tr_wilder.replace(0.0, math.nan))
        dx = ((plus_di_series - minus_di_series).abs() / (plus_di_series + minus_di_series).replace(0.0, math.nan)) * 100.0
        adx_series = dx.ewm(alpha=alpha, adjust=False, min_periods=adx_period).mean()
    adx_last = adx_series.iloc[-1]
    plus_di_last = plus_di_series.iloc[-1]
    minus_di_last = minus_di_series.iloc[-1]
    adx = float(adx_last) if pd.notna(adx_last) else None
    plus_di = float(plus_di_last) if pd.notna(plus_di_last) else None
    minus_di = float(minus_di_last) if pd.notna(minus_di_last) else None
    ctx.adx = adx
    ctx.plus_di = plus_di
    ctx.minus_di = minus_di
    if plus_di is not None and minus_di is not None:
        if plus_di > minus_di:
            ctx.dmi_bias = "bullish"
        elif minus_di > plus_di:
            ctx.dmi_bias = "bearish"
        else:
            ctx.dmi_bias = "neutral"
    if len(frame) >= 2 and adx is not None and pd.notna(adx_series.iloc[-2]):
        prev_adx = float(adx_series.iloc[-2])
        ctx.adx_rising = bool(adx > prev_adx)


def _populate_bollinger_bands(
    ctx: TechnicalLevelsContext,
    frame: pd.DataFrame,
    *,
    close: float,
    bollinger_length: int,
    bollinger_std_mult: float,
    bollinger_squeeze_width_pct: float,
) -> None:
    """Set Bollinger fields on ctx: bollinger_mid/upper/lower/width/width_pct/
    percent_b/zscore/squeeze/upper_reject/lower_reject. Fast path when
    length==20 and mult==2.0 and frame already has bb_* columns (shared
    indicator). Extracted from build_technical_levels_context."""
    bb_len = max(5, int(bollinger_length))
    bb_mult = max(0.5, float(bollinger_std_mult))
    use_shared_bbands = bb_len == 20 and abs(bb_mult - 2.0) <= 1e-9 and {"bb_mid", "bb_upper", "bb_lower", "bb_width", "bb_width_pct", "bb_percent_b", "bb_zscore"}.issubset(frame.columns)
    bb_mid_series: pd.Series | None = None
    bb_upper_series: pd.Series | None = None
    bb_lower_series: pd.Series | None = None
    bb_std_series: pd.Series | None = None
    if use_shared_bbands:
        shared_bb_mid_series = frame["bb_mid"].astype(float)
        bb_upper_series = frame["bb_upper"].astype(float)
        bb_lower_series = frame["bb_lower"].astype(float)
        bb_width_series = frame["bb_width"].astype(float)
        bb_width_pct_series = frame["bb_width_pct"].astype(float)
        bb_percent_b_series = frame["bb_percent_b"].astype(float)
        bb_zscore_series = frame["bb_zscore"].astype(float)
        bb_mid = float(shared_bb_mid_series.iloc[-1]) if not shared_bb_mid_series.dropna().empty else None
        bb_upper = float(bb_upper_series.iloc[-1]) if not bb_upper_series.dropna().empty else None
        bb_lower = float(bb_lower_series.iloc[-1]) if not bb_lower_series.dropna().empty else None
        bb_width = float(bb_width_series.iloc[-1]) if not bb_width_series.dropna().empty else None
        ctx.bollinger_width_pct = float(bb_width_pct_series.iloc[-1]) if not bb_width_pct_series.dropna().empty else None
        ctx.bollinger_percent_b = float(bb_percent_b_series.iloc[-1]) if not bb_percent_b_series.dropna().empty else None
        ctx.bollinger_zscore = float(bb_zscore_series.iloc[-1]) if not bb_zscore_series.dropna().empty else None
    else:
        bb_mid_series = frame["close"].rolling(bb_len, min_periods=max(5, bb_len // 2)).mean()
        bb_std_series = frame["close"].rolling(bb_len, min_periods=max(5, bb_len // 2)).std(ddof=0)
        bb_mid = float(bb_mid_series.iloc[-1]) if not bb_mid_series.dropna().empty else None
        bb_std = float(bb_std_series.iloc[-1]) if not bb_std_series.dropna().empty else None
        bb_upper = None
        bb_lower = None
        bb_width = None
        if bb_mid is not None and bb_std is not None and bb_std >= 0.0:
            bb_upper = float(bb_mid + (bb_std * bb_mult))
            bb_lower = float(bb_mid - (bb_std * bb_mult))
            bb_width = max(0.0, bb_upper - bb_lower)
            ctx.bollinger_width_pct = (bb_width / bb_mid) if bb_mid > 0 else None
            if bb_std > 0:
                ctx.bollinger_zscore = float((close - bb_mid) / bb_std)
            if bb_width > 0:
                ctx.bollinger_percent_b = float((close - bb_lower) / bb_width)
    if bb_mid is not None and bb_upper is not None and bb_lower is not None and bb_width is not None and bb_width >= 0.0:
        ctx.bollinger_mid = float(bb_mid)
        ctx.bollinger_upper = float(bb_upper)
        ctx.bollinger_lower = float(bb_lower)
        ctx.bollinger_width = float(bb_width)
        squeeze_threshold = max(0.0, float(bollinger_squeeze_width_pct))
        ctx.bollinger_squeeze = bool(ctx.bollinger_width_pct is not None and ctx.bollinger_width_pct <= squeeze_threshold)
        if len(frame) >= 2:
            prev_close = float(frame.iloc[-2]["close"])
            if use_shared_bbands:
                prev_upper = float(bb_upper_series.iloc[-2]) if pd.notna(bb_upper_series.iloc[-2]) else None
                prev_lower = float(bb_lower_series.iloc[-2]) if pd.notna(bb_lower_series.iloc[-2]) else None
            else:
                prev_mid = float(bb_mid_series.iloc[-2]) if pd.notna(bb_mid_series.iloc[-2]) else None
                prev_std = float(bb_std_series.iloc[-2]) if pd.notna(bb_std_series.iloc[-2]) else None
                if prev_mid is not None and prev_std is not None and prev_std >= 0.0:
                    prev_upper = prev_mid + (prev_std * bb_mult)
                    prev_lower = prev_mid - (prev_std * bb_mult)
                else:
                    prev_upper = None
                    prev_lower = None
            if prev_upper is not None and prev_lower is not None:
                ctx.bollinger_upper_reject = bool(prev_close >= prev_upper and close < bb_upper)
                ctx.bollinger_lower_reject = bool(prev_close <= prev_lower and close > bb_lower)


def build_technical_levels_context(
    frame: pd.DataFrame,
    *,
    current_price: float | None = None,
    pivot_span: int = 2,
    fib_lookback_bars: int = 120,
    fib_min_impulse_atr: float = 1.25,
    anchored_vwap_impulse_lookback_bars: int | None = None,
    anchored_vwap_min_impulse_atr: float | None = None,
    anchored_vwap_pivot_span: int | None = None,
    trendline_lookback_bars: int = 120,
    trendline_min_touches: int = 3,
    trendline_atr_tolerance_mult: float = 0.35,
    trendline_breakout_buffer_atr_mult: float = 0.15,
    channel_lookback_bars: int = 120,
    channel_min_touches: int = 3,
    channel_atr_tolerance_mult: float = 0.35,
    channel_parallel_slope_frac: float = 0.12,
    channel_min_gap_atr_mult: float = 0.80,
    channel_min_gap_pct: float = 0.0025,
    bollinger_length: int = 20,
    bollinger_std_mult: float = 2.0,
    bollinger_squeeze_width_pct: float = 0.060,
    atr_expansion_lookback: int = 5,
    adx_length: int = 14,
    obv_ema_length: int = 20,
    divergence_rsi_length: int = 14,
    divergence_rsi_min_delta: float = 2.0,
    divergence_obv_min_volume_frac: float = 0.50,
    fib_enabled: bool = True,
    channel_enabled: bool = True,
    trendline_enabled: bool = True,
    adx_enabled: bool = True,
    anchored_vwap_enabled: bool = True,
    atr_context_enabled: bool = True,
    obv_enabled: bool = True,
    divergence_enabled: bool = True,
    bollinger_enabled: bool = True,
) -> TechnicalLevelsContext:
    # Fast path: skip ensure_ohlcv_frame (copy+sort+dropna+reorder) when the
    # frame already has the standard indicator columns — which implies it was
    # already normalized upstream by add_indicators. This was the single hottest
    # line in the profile.
    from .utils import has_standard_indicator_columns
    if frame is not None and not frame.empty and has_standard_indicator_columns(frame):
        raw_frame = frame
    else:
        raw_frame = ensure_ohlcv_frame(frame)
    if raw_frame.empty:
        return empty_technical_levels_context(float(current_price or 0.0))

    close = resolve_current_price(raw_frame, current_price)
    if not any([
        bool(fib_enabled),
        bool(channel_enabled),
        bool(trendline_enabled),
        bool(adx_enabled),
        bool(anchored_vwap_enabled),
        bool(atr_context_enabled),
        bool(obv_enabled),
        bool(divergence_enabled),
        bool(bollinger_enabled),
    ]):
        return TechnicalLevelsContext(current_price=close, reason="all_subfeatures_disabled")

    impulse_context_enabled = bool(fib_enabled or anchored_vwap_enabled)
    base_pivot_span = max(1, int(pivot_span))
    avwap_pivot_span = max(1, int(anchored_vwap_pivot_span if anchored_vwap_pivot_span is not None else base_pivot_span))
    fib_lookback = max(8, int(fib_lookback_bars))
    avwap_impulse_lookback = max(8, int(anchored_vwap_impulse_lookback_bars if anchored_vwap_impulse_lookback_bars is not None else fib_lookback))
    fib_min_impulse_mult = float(fib_min_impulse_atr)
    avwap_min_impulse_mult = float(anchored_vwap_min_impulse_atr if anchored_vwap_min_impulse_atr is not None else fib_min_impulse_mult)

    tail_requirements = [20]
    if fib_enabled:
        tail_requirements.append(fib_lookback)
    if anchored_vwap_enabled:
        tail_requirements.append(avwap_impulse_lookback)
    if trendline_enabled:
        tail_requirements.append(int(trendline_lookback_bars))
    if channel_enabled:
        tail_requirements.append(int(channel_lookback_bars))
    if divergence_enabled:
        tail_requirements.append(max(int(divergence_rsi_length or 14) * 4, max(12, base_pivot_span) * 8, 40))

    frame = ensure_standard_indicator_frame(raw_frame)
    frame = frame.tail(max(tail_requirements)).copy()
    close = resolve_current_price(frame, current_price)
    needs_atr = bool(impulse_context_enabled or trendline_enabled or channel_enabled or atr_context_enabled)
    atr = atr_value(frame) if needs_atr else max(close * 0.0015 if close > 0 else 0.0, 0.0)

    base_pivots_needed = bool(fib_enabled or trendline_enabled or channel_enabled or divergence_enabled)
    if base_pivots_needed:
        # Compute pivots ONCE and share between _reduced_pivots and the downstream
        # code that also needs raw (highs, lows). Previously _reduced_pivots
        # internally called _pivot_points and then this function called it again
        # on the next line, doubling pivot-detection work per call.
        highs, lows = _pivot_points(frame, base_pivot_span)
        pivots = _reduced_pivots(frame, base_pivot_span, highs=highs, lows=lows)
    else:
        pivots = []
        highs = []
        lows = []

    avwap_impulse_pivots = pivots
    if anchored_vwap_enabled and avwap_pivot_span != base_pivot_span:
        avwap_impulse_pivots = _reduced_pivots(frame, avwap_pivot_span)

    ctx = TechnicalLevelsContext(current_price=close)

    if atr_context_enabled:
        _populate_atr_context(ctx, frame, close=close, atr_expansion_lookback=atr_expansion_lookback)

    if adx_enabled:
        _populate_adx_context(ctx, frame, adx_length=adx_length)

    obv_series: pd.Series | None = None
    if obv_enabled or divergence_enabled:
        if "obv" in frame.columns:
            obv_series = frame["obv"].astype(float)
        else:
            # talib_obv coerces to float64 internally via _to_float64_array;
            # explicit fillna keeps NaN volume from propagating into the cumsum.
            obv_series = talib_obv(frame["close"], frame["volume"].fillna(0.0))
        if obv_enabled:
            if int(obv_ema_length) == 20 and "obv_ema20" in frame.columns:
                obv_ema_series = frame["obv_ema20"].astype(float)
            else:
                obv_ema_series = obv_series.ewm(span=max(2, int(obv_ema_length)), adjust=False).mean()
            ctx.obv = float(obv_series.iloc[-1]) if not obv_series.empty else None
            ctx.obv_ema = float(obv_ema_series.iloc[-1]) if not obv_ema_series.empty else None
            if ctx.obv is not None and ctx.obv_ema is not None:
                if ctx.obv > ctx.obv_ema:
                    ctx.obv_bias = "bullish"
                elif ctx.obv < ctx.obv_ema:
                    ctx.obv_bias = "bearish"
                else:
                    ctx.obv_bias = "neutral"

    def _build_rsi(series: pd.Series, length: int) -> pd.Series:
        length = max(2, int(length or 14))
        delta = series.diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta.clip(upper=0.0))
        avg_gain = gain.ewm(alpha=1.0 / float(length), adjust=False, min_periods=length).mean()
        avg_loss = loss.ewm(alpha=1.0 / float(length), adjust=False, min_periods=length).mean()
        rs = avg_gain / avg_loss.replace(0.0, math.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        both_flat = (avg_gain.fillna(0.0) <= 1e-12) & (avg_loss.fillna(0.0) <= 1e-12)
        only_gain = (avg_gain.fillna(0.0) > 1e-12) & (avg_loss.fillna(0.0) <= 1e-12)
        only_loss = (avg_gain.fillna(0.0) <= 1e-12) & (avg_loss.fillna(0.0) > 1e-12)
        rsi = rsi.mask(both_flat, 50.0)
        rsi = rsi.mask(only_gain, 100.0)
        rsi = rsi.mask(only_loss, 0.0)
        return rsi

    rsi_series: pd.Series | None = None
    if divergence_enabled:
        if "rsi14" in frame.columns:
            display_rsi_series = frame["rsi14"].astype(float)
        else:
            display_rsi_series = _build_rsi(frame["close"].astype(float), 14)
        ctx.rsi14 = float(display_rsi_series.iloc[-1]) if not display_rsi_series.dropna().empty else None
        rsi_len = max(5, int(divergence_rsi_length))
        if rsi_len == 14:
            rsi_series = display_rsi_series
        else:
            rsi_series = _build_rsi(frame["close"].astype(float), rsi_len)

    if anchored_vwap_enabled:
        index_dt = pd.DatetimeIndex(frame.index)
        last_day = index_dt[-1].normalize()
        day_bars = frame[index_dt.normalize() == last_day]
        if not day_bars.empty:
            # Prefer RTH open (9:30) over pre-market start so the anchored
            # VWAP isn't skewed by thin pre-market volume.
            from datetime import time as _time
            rth_open = _time(9, 30)
            rth_bars = day_bars[day_bars.index.to_series().map(lambda ts: ts.time() >= rth_open)]
            if not rth_bars.empty:
                session_start_pos = len(frame) - len(rth_bars)
            else:
                session_start_pos = len(frame) - len(day_bars)
            ctx.anchored_vwap_open = _anchored_vwap(frame, session_start_pos)

    if bollinger_enabled:
        _populate_bollinger_bands(
            ctx, frame,
            close=close,
            bollinger_length=bollinger_length,
            bollinger_std_mult=bollinger_std_mult,
            bollinger_squeeze_width_pct=bollinger_squeeze_width_pct,
        )

    if divergence_enabled and rsi_series is not None and obv_series is not None:
        price_move_frac = max(0.0010, 0.0015)
        avg_volume = float(frame["volume"].tail(20).fillna(0.0).mean()) if "volume" in frame.columns else 0.0
        obv_delta = max(1.0, avg_volume * max(0.0, float(divergence_obv_min_volume_frac)))
        ctx.bullish_rsi_divergence = _bullish_divergence(lows, rsi_series, price_move_frac=price_move_frac, indicator_delta=max(0.0, float(divergence_rsi_min_delta)))
        ctx.bearish_rsi_divergence = _bearish_divergence(highs, rsi_series, price_move_frac=price_move_frac, indicator_delta=max(0.0, float(divergence_rsi_min_delta)))
        ctx.bullish_obv_divergence = _bullish_divergence(lows, obv_series, price_move_frac=price_move_frac, indicator_delta=obv_delta)
        ctx.bearish_obv_divergence = _bearish_divergence(highs, obv_series, price_move_frac=price_move_frac, indicator_delta=obv_delta)
        if ctx.bearish_rsi_divergence or ctx.bearish_obv_divergence:
            ctx.counter_divergence_bias = "bearish"
        elif ctx.bullish_rsi_divergence or ctx.bullish_obv_divergence:
            ctx.counter_divergence_bias = "bullish"
        else:
            ctx.counter_divergence_bias = "neutral"

    pivot_driven_features_enabled = bool(base_pivots_needed or anchored_vwap_enabled)
    if not pivot_driven_features_enabled:
        return ctx
    if not pivots and not avwap_impulse_pivots:
        ctx.reason = "no_confirmed_pivots"
        return ctx

    current_pos = len(frame) - 1
    bullish_impulse: tuple[int, int, float, float] | None = None
    bearish_impulse: tuple[int, int, float, float] | None = None
    avwap_bullish_impulse: tuple[int, int, float, float] | None = None
    avwap_bearish_impulse: tuple[int, int, float, float] | None = None
    if fib_enabled and pivots:
        fib_min_impulse = max(atr * fib_min_impulse_mult, close * 0.005)
        fib_start_pos = max(0, len(frame) - fib_lookback)
        max_fib_impulse_age_bars = max(12, min(max(1, fib_lookback - 1), fib_lookback // 3))
        bullish_impulse = _last_impulse_segment(
            pivots,
            "bullish",
            min_range=fib_min_impulse,
            lookback_start_pos=fib_start_pos,
            current_pos=current_pos,
            max_end_age_bars=max_fib_impulse_age_bars,
        )
        bearish_impulse = _last_impulse_segment(
            pivots,
            "bearish",
            min_range=fib_min_impulse,
            lookback_start_pos=fib_start_pos,
            current_pos=current_pos,
            max_end_age_bars=max_fib_impulse_age_bars,
        )

    if anchored_vwap_enabled and avwap_impulse_pivots:
        avwap_min_impulse = max(atr * avwap_min_impulse_mult, close * 0.005)
        avwap_start_pos = max(0, len(frame) - avwap_impulse_lookback)
        max_avwap_impulse_age_bars = max(12, min(max(1, avwap_impulse_lookback - 1), avwap_impulse_lookback // 3))
        avwap_bullish_impulse = _last_impulse_segment(
            avwap_impulse_pivots,
            "bullish",
            min_range=avwap_min_impulse,
            lookback_start_pos=avwap_start_pos,
            current_pos=current_pos,
            max_end_age_bars=max_avwap_impulse_age_bars,
        )
        avwap_bearish_impulse = _last_impulse_segment(
            avwap_impulse_pivots,
            "bearish",
            min_range=avwap_min_impulse,
            lookback_start_pos=avwap_start_pos,
            current_pos=current_pos,
            max_end_age_bars=max_avwap_impulse_age_bars,
        )

    if anchored_vwap_enabled:
        if avwap_bullish_impulse is not None:
            low_pos, _high_pos, _low, _high = avwap_bullish_impulse
            ctx.anchored_vwap_bullish_impulse = _anchored_vwap(frame, low_pos)
        if avwap_bearish_impulse is not None:
            high_pos, _low_pos, _high, _low = avwap_bearish_impulse
            ctx.anchored_vwap_bearish_impulse = _anchored_vwap(frame, high_pos)

    if fib_enabled:
        selected_fib_anchor: tuple[float, float] | None = None
        if bullish_impulse is not None:
            low_pos, _high_pos, low, high = bullish_impulse
            rng = max(0.0, high - low)
            if rng > 0:
                ctx.fib_bullish_1272 = float(high + (rng * 0.272))
                ctx.fib_bullish_1618 = float(high + (rng * 0.618))
                bull_candidates = [(1.272, ctx.fib_bullish_1272), (1.618, ctx.fib_bullish_1618)]
                above = [(ratio, px) for ratio, px in bull_candidates if px is not None and px > close]
                if above:
                    ratio, px = min(above, key=lambda item: item[1])
                    ctx.nearest_bullish_extension = float(px)
                    ctx.nearest_bullish_extension_ratio = float(ratio)
                    ctx.bullish_extension_distance_pct = max(0.0, (float(px) - close) / close) if close > 0 else None
        if bearish_impulse is not None:
            high_pos, _low_pos, high, low = bearish_impulse
            rng = max(0.0, high - low)
            if rng > 0:
                ctx.fib_bearish_1272 = float(low - (rng * 0.272))
                ctx.fib_bearish_1618 = float(low - (rng * 0.618))
                bear_candidates = [(1.272, ctx.fib_bearish_1272), (1.618, ctx.fib_bearish_1618)]
                below = [(ratio, px) for ratio, px in bear_candidates if px is not None and px < close]
                if below:
                    ratio, px = max(below, key=lambda item: item[1])
                    ctx.nearest_bearish_extension = float(px)
                    ctx.nearest_bearish_extension_ratio = float(ratio)
                    ctx.bearish_extension_distance_pct = max(0.0, (close - float(px)) / close) if close > 0 else None
        if bullish_impulse is not None and bearish_impulse is None:
            ctx.fib_direction = "bullish"
            _low_pos, _high_pos, low, high = bullish_impulse
            selected_fib_anchor = (float(low), float(high))
        elif bearish_impulse is not None and bullish_impulse is None:
            ctx.fib_direction = "bearish"
            _high_pos, _low_pos, high, low = bearish_impulse
            selected_fib_anchor = (float(low), float(high))
        elif bullish_impulse is not None and bearish_impulse is not None:
            bull_gap = ctx.bullish_extension_distance_pct if ctx.bullish_extension_distance_pct is not None else math.inf
            bear_gap = ctx.bearish_extension_distance_pct if ctx.bearish_extension_distance_pct is not None else math.inf
            if bull_gap <= bear_gap:
                ctx.fib_direction = "bullish"
                _low_pos, _high_pos, low, high = bullish_impulse
                selected_fib_anchor = (float(low), float(high))
            else:
                ctx.fib_direction = "bearish"
                _high_pos, _low_pos, high, low = bearish_impulse
                selected_fib_anchor = (float(low), float(high))
        if selected_fib_anchor is not None:
            ctx.fib_anchor_low = float(selected_fib_anchor[0])
            ctx.fib_anchor_high = float(selected_fib_anchor[1])

    if anchored_vwap_enabled:
        open_avwap = ctx.anchored_vwap_open
        bull_avwap = ctx.anchored_vwap_bullish_impulse
        bear_avwap = ctx.anchored_vwap_bearish_impulse
        bullish_votes = sum(1 for px in [open_avwap, bull_avwap] if px is not None and close >= float(px))
        bearish_votes = sum(1 for px in [open_avwap, bear_avwap] if px is not None and close <= float(px))
        if bullish_votes >= 2:
            ctx.anchored_vwap_bias = "bullish"
        elif bearish_votes >= 2:
            ctx.anchored_vwap_bias = "bearish"
        else:
            ctx.anchored_vwap_bias = "neutral"

    # Track pre-computed lines so that if channel uses the same params as
    # trendline (the common default case), we skip redundant _build_best_line calls.
    raw_support_line: TechnicalLine | None = None
    raw_resistance_line: TechnicalLine | None = None
    trendline_params: tuple | None = None

    if trendline_enabled:
        trendline_lookback = max(10, int(trendline_lookback_bars))
        trendline_start_pos = max(0, len(frame) - trendline_lookback)
        trendline_lows = [pt for pt in lows if pt[0] >= trendline_start_pos]
        trendline_highs = [pt for pt in highs if pt[0] >= trendline_start_pos]
        tl_tol = max(atr * float(trendline_atr_tolerance_mult), close * 0.0025)
        tl_touches = max(2, int(trendline_min_touches))
        raw_support_line = _build_best_line(trendline_lows, kind="support", current_pos=current_pos, tolerance=tl_tol, min_touches=tl_touches)
        raw_resistance_line = _build_best_line(trendline_highs, kind="resistance", current_pos=current_pos, tolerance=tl_tol, min_touches=tl_touches)
        trendline_params = (trendline_lookback, tl_tol, tl_touches)
        support_line = raw_support_line
        resistance_line = raw_resistance_line
        if not _trendline_has_material_slope(support_line, current_pos=current_pos, tolerance=tl_tol):
            support_line = None
        if not _trendline_has_material_slope(resistance_line, current_pos=current_pos, tolerance=tl_tol):
            resistance_line = None
        ctx.support_trendline = support_line
        ctx.resistance_trendline = resistance_line
        buffer = max(atr * float(trendline_breakout_buffer_atr_mult), close * 0.0010)
        if support_line is not None:
            support_value = float(support_line.current_value)
            ctx.support_distance_pct = max(0.0, abs(close - support_value) / close) if close > 0 else None
            ctx.support_respected = support_value - buffer <= close <= support_value + (buffer * 1.5)
            ctx.trendline_break_down = close < support_value - buffer
        if resistance_line is not None:
            resistance_value = float(resistance_line.current_value)
            ctx.resistance_distance_pct = max(0.0, abs(resistance_value - close) / close) if close > 0 else None
            ctx.resistance_respected = resistance_value + buffer >= close >= resistance_value - (buffer * 1.5)
            ctx.trendline_break_up = close > resistance_value + buffer

    if channel_enabled:
        channel_lookback = max(10, int(channel_lookback_bars))
        channel_start_pos = max(0, len(frame) - channel_lookback)
        channel_tol = max(atr * float(channel_atr_tolerance_mult), close * 0.0025)
        channel_touches = max(2, int(channel_min_touches))
        channel_params = (channel_lookback, channel_tol, channel_touches)
        if trendline_params is not None and channel_params == trendline_params:
            # Same lookback+tolerance+touches as trendline → reuse the lines we
            # already computed. Saves 2 full _build_best_line calls per context
            # build (each of which is O(k²) over up to 7 candidate pivots).
            channel_support = raw_support_line
            channel_resistance = raw_resistance_line
        else:
            channel_lows = [pt for pt in lows if pt[0] >= channel_start_pos]
            channel_highs = [pt for pt in highs if pt[0] >= channel_start_pos]
            channel_support = _build_best_line(channel_lows, kind="support", current_pos=current_pos, tolerance=channel_tol, min_touches=channel_touches)
            channel_resistance = _build_best_line(channel_highs, kind="resistance", current_pos=current_pos, tolerance=channel_tol, min_touches=channel_touches)
        channel_min_gap_abs = max(atr * float(channel_min_gap_atr_mult), close * float(channel_min_gap_pct), channel_tol)
        ctx.channel = _build_channel(
            channel_support,
            channel_resistance,
            frame=frame,
            current_price=close,
            current_pos=current_pos,
            tolerance=channel_tol,
            parallel_slope_frac=float(channel_parallel_slope_frac),
            min_gap_abs=float(channel_min_gap_abs),
        )
    return ctx
