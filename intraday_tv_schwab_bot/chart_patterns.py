# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import logging
from typing import Any

import numpy as np
import pandas as pd


LOG = logging.getLogger(__name__)

PatternFunc = Callable[[pd.DataFrame], bool]


@dataclass(slots=True)
class ChartPatternContext:
    matched_bullish: set[str] = field(default_factory=set)
    matched_bullish_reversal: set[str] = field(default_factory=set)
    matched_bullish_continuation: set[str] = field(default_factory=set)
    matched_bearish: set[str] = field(default_factory=set)
    matched_bearish_reversal: set[str] = field(default_factory=set)
    matched_bearish_continuation: set[str] = field(default_factory=set)
    invalidated_bullish: set[str] = field(default_factory=set)
    invalidated_bearish: set[str] = field(default_factory=set)
    bias_score: float = 0.0
    regime_hint: str = "neutral"


_CHART_CLEAN_SENTINEL = "_chart_patterns_clean"

# Module-level memoization cache for per-frame computations.
# Keyed by (func_name, id(frame), *args). Populated lazily and cleared at
# the end of each analyze_chart_pattern_context() call so entries don't
# survive past the analysis session they were built for.
#
# Rationale: we cannot stash this dict in frame.attrs because pandas'
# DataFrame.__finalize__ does a deepcopy of attrs on every slice/copy op,
# which would infinitely recurse through any DataFrame values stored in the
# cache. A module-level dict keyed by id() avoids that entirely.
_CHART_HELPER_CACHE: dict[tuple, Any] = {}


def _clear_chart_cache() -> None:
    """Drop all memoized chart pattern computations.

    Called at the end of analyze_chart_pattern_context() so the cache does
    not accumulate entries across ticks.
    """
    _CHART_HELPER_CACHE.clear()


def _clean_price_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    # Fast path 1: if we already normalized this frame in the current call
    # chain, re-use it instead of redoing sort + to_numeric + dropna + copy.
    frame_attrs = getattr(frame, "attrs", None)
    if frame_attrs is not None and frame_attrs.get(_CHART_CLEAN_SENTINEL):
        return frame
    # Fast path 2: frames produced by add_indicators() are already sorted,
    # numeric, and NaN-free on OHLCV. Detect that by the presence of the
    # standard indicator column set and skip the slow clean work entirely.
    # This was the dominant cost at the top of every analyze_chart_pattern_context
    # call because strategies pass frames built by add_indicators().
    try:
        from .utils import has_standard_indicator_columns
        if has_standard_indicator_columns(frame):
            if frame_attrs is not None:
                frame_attrs[_CHART_CLEAN_SENTINEL] = True
            return frame
    except Exception:
        pass
    out = frame.copy()
    if "datetime" in out.columns:
        try:
            out = out.sort_values("datetime")
        except Exception:
            LOG.debug("Failed to sort price frame by datetime; using original order.", exc_info=True)
    else:
        try:
            out = out.sort_index()
        except Exception:
            LOG.debug("Failed to sort price frame by index; using original order.", exc_info=True)
    for col in ("open", "high", "low", "close", "volume"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    required = [col for col in ("open", "high", "low", "close") if col in out.columns]
    if required:
        out = out.dropna(subset=required)
    out = out.copy()
    out.attrs[_CHART_CLEAN_SENTINEL] = True
    return out


def _tail(frame: pd.DataFrame, n: int) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    # Cache repeated _tail(frame, n) calls: many pattern functions slice the
    # same parent frame by the same lookback, so returning the SAME DataFrame
    # object means downstream helpers (find_pivots, mean_range, etc.) that
    # cache by id(frame) reuse the cached result instead of recomputing.
    cache_key = ("_tail", id(frame), int(n))
    cached = _CHART_HELPER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    cleaned = _clean_price_frame(frame)
    if cleaned.empty:
        _CHART_HELPER_CACHE[cache_key] = pd.DataFrame()
        return _CHART_HELPER_CACHE[cache_key]
    tailed = cleaned.tail(n).copy()
    # Keep the sentinel so downstream calls in this call chain short-circuit
    # the re-cleaning work.
    tailed.attrs[_CHART_CLEAN_SENTINEL] = True
    _CHART_HELPER_CACHE[cache_key] = tailed
    return tailed


def _head(frame: pd.DataFrame, n: int) -> pd.DataFrame:
    """Cached head slice. Many pattern functions call frame.head(N) on the
    same frame with the same N (to check the prior impulse before the
    pattern), so sharing the resulting DataFrame object means downstream
    helpers cache-hit by id()."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    cache_key = ("_head", id(frame), int(n))
    cached = _CHART_HELPER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    head_frame = frame.head(n).copy()
    head_frame.attrs[_CHART_CLEAN_SENTINEL] = True
    _CHART_HELPER_CACHE[cache_key] = head_frame
    return head_frame


def _close(frame: pd.DataFrame, idx: int = -1) -> float:
    # Hot path: called thousands of times per tick with idx=-1. Memoize the
    # last-close lookup using a numpy-backed fast path and an id(frame) cache
    # to avoid pandas' expensive .iloc + Series wrapping.
    if idx == -1:
        key = ("_close_last", id(frame))
        cached = _CHART_HELPER_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            val = float(frame["close"].to_numpy(dtype=np.float64, copy=False)[-1])
        except Exception:
            val = float(frame.iloc[idx].close)
        _CHART_HELPER_CACHE[key] = val
        return val
    return float(frame.iloc[idx].close)


def _open(frame: pd.DataFrame, idx: int = 0) -> float:
    if idx == 0:
        key = ("_open_first", id(frame))
        cached = _CHART_HELPER_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            val = float(frame["open"].to_numpy(dtype=np.float64, copy=False)[0])
        except Exception:
            val = float(frame.iloc[idx].open)
        _CHART_HELPER_CACHE[key] = val
        return val
    return float(frame.iloc[idx].open)


def _ema(frame: pd.DataFrame, col: str, fallback: str = "close") -> float:
    if col in frame.columns and not frame[col].empty:
        try:
            value = float(frame.iloc[-1][col])
            if np.isfinite(value):
                return value
        except Exception:
            LOG.debug("Failed to coerce optional price column %s to float; falling back to close.", col, exc_info=True)
    return float(frame.iloc[-1][fallback])


def _mean_range(frame: pd.DataFrame) -> float:
    if frame is None or frame.empty:
        return 0.0
    key = ("_mean_range", id(frame))
    cached = _CHART_HELPER_CACHE.get(key)
    if cached is not None:
        return cached
    # numpy path — skip astype(float) when the columns are already float64
    # (the common case on indicator frames), and use numpy element-wise ops.
    high_arr = frame["high"].to_numpy(dtype=np.float64, copy=False)
    low_arr = frame["low"].to_numpy(dtype=np.float64, copy=False)
    spans = high_arr - low_arr
    np.maximum(spans, 0.0, out=spans)
    tail_n = min(spans.shape[0], 14)
    if tail_n == 0:
        result = 0.0
    else:
        tail_slice = spans[-tail_n:]
        mean_val = float(tail_slice.mean())
        result = mean_val if mean_val == mean_val else 0.0  # NaN check
    _CHART_HELPER_CACHE[key] = result
    return result


def _atr_pct(frame: pd.DataFrame) -> float:
    if frame is None or frame.empty:
        return 0.0
    key = ("_atr_pct", id(frame))
    cached = _CHART_HELPER_CACHE.get(key)
    if cached is not None:
        return cached
    close = max(abs(_close(frame)), 1e-9)
    result = _mean_range(frame) / close
    _CHART_HELPER_CACHE[key] = result
    return result


def _level_tolerance(frame: pd.DataFrame, price: float) -> float:
    base = abs(float(price)) * 0.015
    noise = _mean_range(frame) * 0.90
    return max(0.02, base, noise)


def _recent(chunk: list[tuple[str, int, float]] | None, size: int, max_age: int = 14) -> bool:
    return bool(chunk and (size - 1 - int(chunk[-1][1])) <= max_age)


def _normalize_token(value: str) -> str:
    return str(value).strip().lower()


def _ret_pct(frame: pd.DataFrame) -> float:
    if frame is None or frame.empty:
        return 0.0
    start = _open(frame, 0)
    if start <= 0:
        return 0.0
    return (_close(frame, -1) - start) / start


def _line_fit(values: pd.Series) -> tuple[float, float]:
    # Closed-form OLS for a degree-1 fit. np.polyfit(x, y, 1) is general-purpose
    # and handles arbitrary degree via Vandermonde + lstsq, which has significant
    # overhead for a simple 2-coefficient line fit. Computing slope/intercept
    # directly from sums is ~5-8x faster and numerically stable for the small
    # series this function receives (~6-40 points).
    vals = values.to_numpy(dtype=np.float64, copy=False) if values.dtype != object else values.astype(float).to_numpy(dtype=np.float64)
    n = vals.shape[0]
    if n < 2:
        return 0.0, float(vals[-1]) if n else 0.0
    # x = 0, 1, ..., n-1  ⇒  sum(x) = n(n-1)/2, sum(x²) = (n-1)n(2n-1)/6
    n_f = float(n)
    x_mean = (n_f - 1.0) / 2.0
    y_mean = float(vals.mean())
    # sum((x_i - x_mean)(y_i - y_mean)) / sum((x_i - x_mean)²)
    x = np.arange(n, dtype=np.float64)
    x_dev = x - x_mean
    # sum((x_i - x_mean) * y_i) == sum(x_dev * (y_i - y_mean)) because sum(x_dev) = 0,
    # so the y_mean term cancels out and we can dot x_dev with vals directly.
    num = float(np.dot(x_dev, vals))
    denom_sq = float(np.dot(x_dev, x_dev))
    if denom_sq <= 0.0:
        return 0.0, y_mean
    slope = num / denom_sq
    intercept = y_mean - slope * x_mean
    return float(slope), float(intercept)


def _bar_close_position(frame: pd.DataFrame, idx: int = -1) -> float:
    if frame is None or frame.empty:
        return 0.5
    row = frame.iloc[idx]
    low = float(row.low)
    high = float(row.high)
    if high <= low:
        return 0.5
    return (float(row.close) - low) / (high - low)


def _body_fraction(frame: pd.DataFrame, idx: int = -1) -> float:
    if frame is None or frame.empty:
        return 0.0
    row = frame.iloc[idx]
    rng = max(float(row.high) - float(row.low), 1e-9)
    return abs(float(row.close) - float(row.open)) / rng


def _recent_move(frame: pd.DataFrame, bars: int = 3) -> float:
    f = _tail(frame, bars)
    return _ret_pct(f)


def _has_volume_expansion(frame: pd.DataFrame, ratio: float = 1.15, bars: int = 3, baseline: int = 10) -> bool:
    if frame is None or frame.empty or "volume" not in frame.columns:
        return True
    vols = pd.Series(pd.to_numeric(frame["volume"], errors="coerce"), index=frame.index, dtype="float64").fillna(0.0)
    if len(vols) < max(4, baseline):
        return True
    recent = float(vols.tail(bars).mean() or 0.0)
    base = float(vols.iloc[:-bars].tail(baseline).mean() or 0.0)
    if base <= 0:
        return True
    return recent >= base * ratio


def _prior_impulse(frame: pd.DataFrame, direction: str, bars: int = 8) -> bool:
    f = _tail(frame, bars)
    move = _ret_pct(f)
    threshold = max(0.012, _atr_pct(f) * 1.8)
    return move >= threshold if direction == "up" else move <= -threshold


@dataclass(slots=True)
class _TrendStats:
    slope_high: float
    slope_low: float
    range_start: float
    range_end: float


def _trend_stats(frame: pd.DataFrame) -> _TrendStats | None:
    if frame is None or len(frame) < 6:
        return None
    cache_key = ("_trend_stats", id(frame))
    cached = _CHART_HELPER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    slope_high, intercept_high = _line_fit(frame["high"])
    slope_low, intercept_low = _line_fit(frame["low"])
    x_end = len(frame) - 1
    range_start = intercept_high - intercept_low
    range_end = (slope_high * x_end + intercept_high) - (slope_low * x_end + intercept_low)
    result = _TrendStats(
        slope_high=float(slope_high),
        slope_low=float(slope_low),
        range_start=float(range_start),
        range_end=float(range_end),
    )
    _CHART_HELPER_CACHE[cache_key] = result
    return result


def _pivot_order(frame: pd.DataFrame) -> int:
    if frame is None or len(frame) < 14:
        return 1
    key = ("_pivot_order", id(frame))
    cached = _CHART_HELPER_CACHE.get(key)
    if cached is not None:
        return cached
    vol = _atr_pct(frame.tail(min(len(frame), 10)))
    if vol >= 0.03:
        result = 3
    else:
        result = 2
    _CHART_HELPER_CACHE[key] = result
    return result


def _find_pivots(frame: pd.DataFrame, order: int | None = None) -> list[tuple[str, int, float]]:
    order = _pivot_order(frame) if order is None else max(1, int(order))
    if frame is None or len(frame) < (order * 2 + 3):
        return []
    key = ("_find_pivots", id(frame), int(order))
    cached = _CHART_HELPER_CACHE.get(key)
    if cached is not None:
        return cached
    # Drop to numpy arrays; skip astype(float).tolist() round-trip when the
    # columns are already float64 (common on indicator frames). Raw lists
    # keep the inner loop Python-native so max()/min() stay fast.
    highs = frame["high"].to_numpy(dtype=np.float64, copy=False).tolist()
    lows = frame["low"].to_numpy(dtype=np.float64, copy=False).tolist()
    prominence = max(_mean_range(frame) * 0.05, abs(_close(frame)) * 0.0005)
    raw: list[tuple[str, int, float]] = []
    for idx in range(order, len(frame) - order):
        hi_window = highs[idx - order: idx + order + 1]
        lo_window = lows[idx - order: idx + order + 1]
        hi_neighbors = hi_window[:order] + hi_window[order + 1:]
        lo_neighbors = lo_window[:order] + lo_window[order + 1:]
        if highs[idx] >= max(hi_window) and highs[idx] > max(hi_neighbors) + prominence:
            raw.append(("H", idx, highs[idx]))
        if lows[idx] <= min(lo_window) and lows[idx] < min(lo_neighbors) - prominence:
            raw.append(("L", idx, lows[idx]))
    raw.sort(key=lambda item: item[1])
    if not raw:
        _CHART_HELPER_CACHE[key] = []
        return []
    out: list[tuple[str, int, float]] = []
    for kind, idx, price in raw:
        if out and out[-1][0] == kind:
            _, _, prev_price = out[-1]
            more_extreme = price >= prev_price if kind == "H" else price <= prev_price
            if more_extreme:
                out[-1] = (kind, idx, price)
        else:
            out.append((kind, idx, price))
    _CHART_HELPER_CACHE[key] = out
    return out


def _last_matching_pivots(pivots: list[tuple[str, int, float]], sequence: str) -> list[tuple[str, int, float]] | None:
    n = len(sequence)
    for start in range(len(pivots) - n, -1, -1):
        chunk = pivots[start:start + n]
        if "".join(kind for kind, _, _ in chunk) == sequence:
            return chunk
    return None


def _reversal_chunk(frame: pd.DataFrame, sequence: str) -> list[tuple[str, int, float]] | None:
    order = _pivot_order(frame)
    chunk = _last_matching_pivots(_find_pivots(frame, order=order), sequence)
    if chunk is None and order > 1:
        chunk = _last_matching_pivots(_find_pivots(frame, order=order - 1), sequence)
    return chunk


def _bullish_breakout_ready(frame: pd.DataFrame, level: float, tol: float) -> bool:
    if frame is None or frame.empty:
        return False
    close = _close(frame)
    near_high = _bar_close_position(frame) >= 0.58
    directional = _recent_move(frame, 3) >= -0.002
    trend_ok = close >= _ema(frame, "ema9") * 0.997
    return bool(close >= level - tol * 0.20 and near_high and directional and trend_ok and _has_volume_expansion(frame, ratio=1.08))


def _bearish_breakdown_ready(frame: pd.DataFrame, level: float, tol: float) -> bool:
    if frame is None or frame.empty:
        return False
    close = _close(frame)
    near_low = _bar_close_position(frame) <= 0.42
    directional = _recent_move(frame, 3) <= 0.002
    trend_ok = close <= _ema(frame, "ema9") * 1.003
    return bool(close <= level + tol * 0.20 and near_low and directional and trend_ok and _has_volume_expansion(frame, ratio=1.08))


def _bullish_reversal_breakout_ready(frame: pd.DataFrame, level: float, tol: float) -> bool:
    if frame is None or frame.empty:
        return False
    close = _close(frame)
    close_pos = _bar_close_position(frame)
    directional = _recent_move(frame, 3) >= -0.004
    trend_ok = close >= _ema(frame, "ema9") * 0.994 or close >= _ema(frame, "vwap") * 0.998
    reclaim_ok = close >= level - tol * 0.35
    candle_ok = close_pos >= 0.52 or _body_fraction(frame) >= 0.28
    volume_ok = _has_volume_expansion(frame, ratio=1.03)
    return bool(reclaim_ok and directional and trend_ok and candle_ok and volume_ok)


def _bearish_reversal_breakdown_ready(frame: pd.DataFrame, level: float, tol: float) -> bool:
    if frame is None or frame.empty:
        return False
    close = _close(frame)
    close_pos = _bar_close_position(frame)
    directional = _recent_move(frame, 3) <= 0.004
    trend_ok = close <= _ema(frame, "ema9") * 1.006 or close <= _ema(frame, "vwap") * 1.002
    reclaim_ok = close <= level + tol * 0.35
    candle_ok = close_pos <= 0.48 or _body_fraction(frame) >= 0.28
    volume_ok = _has_volume_expansion(frame, ratio=1.03)
    return bool(reclaim_ok and directional and trend_ok and candle_ok and volume_ok)


def bearish_double_top(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 36)
    chunk = _reversal_chunk(f, "HLH")
    if not _recent(chunk, len(f), max_age=12):
        return False
    (_, i1, h1), (_, _, neckline), (_, i3, h2) = chunk
    tol = _level_tolerance(f, (h1 + h2) / 2.0)
    depth = min(h1, h2) - neckline
    return bool(i3 - i1 >= 3 and abs(h1 - h2) <= tol and depth >= tol * 1.15 and _bearish_reversal_breakdown_ready(f, neckline, tol))


def bullish_double_bottom(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 36)
    chunk = _reversal_chunk(f, "LHL")
    if not _recent(chunk, len(f), max_age=12):
        return False
    (_, i1, l1), (_, _, neckline), (_, i3, l2) = chunk
    tol = _level_tolerance(f, (l1 + l2) / 2.0)
    height = neckline - max(l1, l2)
    return bool(i3 - i1 >= 3 and abs(l1 - l2) <= tol and height >= tol * 1.15 and _bullish_reversal_breakout_ready(f, neckline, tol))


def bearish_triple_top(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 40)
    chunk = _reversal_chunk(f, "HLHLH")
    if not _recent(chunk, len(f), max_age=14):
        return False
    highs = [chunk[0][2], chunk[2][2], chunk[4][2]]
    valleys = [chunk[1][2], chunk[3][2]]
    tol = _level_tolerance(f, sum(highs) / len(highs))
    neckline = max(valleys)
    return bool(max(highs) - min(highs) <= tol and min(highs) - neckline >= tol * 1.15 and _bearish_reversal_breakdown_ready(f, neckline, tol))


def bullish_triple_bottom(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 40)
    chunk = _reversal_chunk(f, "LHLHL")
    if not _recent(chunk, len(f), max_age=14):
        return False
    lows = [chunk[0][2], chunk[2][2], chunk[4][2]]
    peaks = [chunk[1][2], chunk[3][2]]
    tol = _level_tolerance(f, sum(lows) / len(lows))
    neckline = min(peaks)
    return bool(max(lows) - min(lows) <= tol and neckline - max(lows) >= tol * 1.15 and _bullish_reversal_breakout_ready(f, neckline, tol))


def bearish_head_and_shoulders(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 40)
    chunk = _reversal_chunk(f, "HLHLH")
    if not _recent(chunk, len(f), max_age=14):
        return False
    (_, _, left_shoulder), (_, _, low1), (_, _, head), (_, _, low2), (_, _, right_shoulder) = chunk
    shoulder_tol = _level_tolerance(f, (left_shoulder + right_shoulder) / 2.0)
    neckline = (low1 + low2) / 2.0
    return bool(
        head > max(left_shoulder, right_shoulder) + shoulder_tol
        and abs(left_shoulder - right_shoulder) <= shoulder_tol * 1.1
        and abs(low1 - low2) <= shoulder_tol * 1.3
        and _bearish_reversal_breakdown_ready(f, neckline, shoulder_tol)
    )


def bullish_inverse_head_and_shoulders(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 40)
    chunk = _reversal_chunk(f, "LHLHL")
    if not _recent(chunk, len(f), max_age=14):
        return False
    (_, _, left_shoulder), (_, _, high1), (_, _, head), (_, _, high2), (_, _, right_shoulder) = chunk
    shoulder_tol = _level_tolerance(f, (left_shoulder + right_shoulder) / 2.0)
    neckline = (high1 + high2) / 2.0
    return bool(
        head < min(left_shoulder, right_shoulder) - shoulder_tol
        and abs(left_shoulder - right_shoulder) <= shoulder_tol * 1.1
        and abs(high1 - high2) <= shoulder_tol * 1.3
        and _bullish_reversal_breakout_ready(f, neckline, shoulder_tol)
    )


def bearish_rising_wedge(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 20)
    stats = _trend_stats(f)
    if stats is None or len(f) < 12:
        return False
    weak_close = _close(f) < _ema(f, "ema9") and _bar_close_position(f) <= 0.48
    return bool(
        _prior_impulse(_head(f, max(8, len(f) // 2)), "up")
        and stats.slope_high > 0
        and stats.slope_low > 0
        and stats.slope_low > stats.slope_high * 1.08
        and 0 < stats.range_end < stats.range_start * 0.78
        and weak_close
    )


def bullish_falling_wedge(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 20)
    stats = _trend_stats(f)
    if stats is None or len(f) < 12:
        return False
    strong_close = _close(f) > _ema(f, "ema9") and _bar_close_position(f) >= 0.52
    return bool(
        _prior_impulse(_head(f, max(8, len(f) // 2)), "down")
        and stats.slope_high < stats.slope_low < 0 < stats.range_end < stats.range_start * 0.78
        and strong_close
    )


def bearish_broadening_top(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 26)
    stats = _trend_stats(f)
    pivots = _find_pivots(f)
    if stats is None or len(pivots) < 4:
        return False
    prior_trend = _ret_pct(_head(f, max(8, len(f) // 2)))
    midrange = (float(f.iloc[-1].high) + float(f.iloc[-1].low)) / 2.0
    return bool(
        prior_trend > 0.015
        and stats.slope_high > 0 > stats.slope_low
        and stats.range_end > stats.range_start * 1.18
        and _close(f) <= midrange
        and _bar_close_position(f) <= 0.45
    )


def bullish_broadening_bottom(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 26)
    stats = _trend_stats(f)
    pivots = _find_pivots(f)
    if stats is None or len(pivots) < 4:
        return False
    prior_trend = _ret_pct(_head(f, max(8, len(f) // 2)))
    midrange = (float(f.iloc[-1].high) + float(f.iloc[-1].low)) / 2.0
    return bool(
        prior_trend < -0.015
        and stats.slope_high > 0 > stats.slope_low
        and stats.range_end > stats.range_start * 1.18
        and _close(f) >= midrange
        and _bar_close_position(f) >= 0.55
    )


def _impulse_and_consolidation(frame: pd.DataFrame, bars: int = 16, impulse_bars: int = 5) -> tuple[pd.DataFrame, pd.DataFrame] | tuple[None, None]:
    f = _tail(frame, bars)
    if len(f) < bars:
        return None, None
    impulse = _head(f, impulse_bars)
    consolidation = _tail(f, bars - impulse_bars)
    return impulse, consolidation


def bullish_flag(frame: pd.DataFrame) -> bool:
    impulse, flag = _impulse_and_consolidation(frame, bars=16, impulse_bars=5)
    if impulse is None or flag is None:
        return False
    impulse_move = _close(impulse) - _open(impulse, 0)
    min_impulse = max(_mean_range(impulse) * 3.0, _open(impulse, 0) * 0.02)
    if impulse_move <= min_impulse:
        return False
    flag_slope, _ = _line_fit(flag["close"])
    retrace = _close(impulse) - float(flag["low"].min())
    prior_flag_high = float(flag["high"].iloc[:-1].max()) if len(flag) > 1 else float(flag["high"].max())
    contraction = float(flag["high"].max() - flag["low"].min()) <= impulse_move * 0.65
    breakout_ready = _bullish_breakout_ready(flag, prior_flag_high, _level_tolerance(flag, prior_flag_high))
    return bool(flag_slope <= max(0.04, impulse_move / max(len(flag), 1) * 0.18) and 0 <= retrace <= impulse_move * 0.55 and contraction and breakout_ready)


def bearish_flag(frame: pd.DataFrame) -> bool:
    impulse, flag = _impulse_and_consolidation(frame, bars=16, impulse_bars=5)
    if impulse is None or flag is None:
        return False
    impulse_move = _open(impulse, 0) - _close(impulse)
    min_impulse = max(_mean_range(impulse) * 3.0, _open(impulse, 0) * 0.02)
    if impulse_move <= min_impulse:
        return False
    flag_slope, _ = _line_fit(flag["close"])
    retrace = float(flag["high"].max()) - _close(impulse)
    prior_flag_low = float(flag["low"].iloc[:-1].min()) if len(flag) > 1 else float(flag["low"].min())
    contraction = float(flag["high"].max() - flag["low"].min()) <= impulse_move * 0.65
    breakdown_ready = _bearish_breakdown_ready(flag, prior_flag_low, _level_tolerance(flag, prior_flag_low))
    return bool(flag_slope >= min(-0.04, -impulse_move / max(len(flag), 1) * 0.18) and 0 <= retrace <= impulse_move * 0.55 and contraction and breakdown_ready)


def bullish_pennant(frame: pd.DataFrame) -> bool:
    impulse, pennant = _impulse_and_consolidation(frame, bars=16, impulse_bars=5)
    if impulse is None or pennant is None:
        return False
    impulse_move = _close(impulse) - _open(impulse, 0)
    if impulse_move <= max(_mean_range(impulse) * 3.0, _open(impulse, 0) * 0.02):
        return False
    stats = _trend_stats(pennant)
    if stats is None:
        return False
    prior_high = float(pennant["high"].iloc[:-1].max()) if len(pennant) > 1 else float(pennant["high"].max())
    tol = _level_tolerance(pennant, prior_high)
    return bool(
        stats.slope_high < 0 < stats.slope_low and stats.range_end < stats.range_start * 0.72 and _bullish_breakout_ready(pennant, prior_high, tol))


def bearish_pennant(frame: pd.DataFrame) -> bool:
    impulse, pennant = _impulse_and_consolidation(frame, bars=16, impulse_bars=5)
    if impulse is None or pennant is None:
        return False
    impulse_move = _open(impulse, 0) - _close(impulse)
    if impulse_move <= max(_mean_range(impulse) * 3.0, _open(impulse, 0) * 0.02):
        return False
    stats = _trend_stats(pennant)
    if stats is None:
        return False
    prior_low = float(pennant["low"].iloc[:-1].min()) if len(pennant) > 1 else float(pennant["low"].min())
    tol = _level_tolerance(pennant, prior_low)
    return bool(
        stats.slope_high < 0 < stats.slope_low and stats.range_end < stats.range_start * 0.72 and _bearish_breakdown_ready(pennant, prior_low, tol))


def bullish_ascending_triangle(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 24)
    pivots = _find_pivots(f)
    highs = [item for item in pivots if item[0] == "H"][-3:]
    lows = [item for item in pivots if item[0] == "L"][-3:]
    if len(highs) < 2 or len(lows) < 2:
        return False
    resistance = sum(p[2] for p in highs[-2:]) / 2.0
    tol = _level_tolerance(f, resistance)
    highs_flat = max(abs(p[2] - resistance) for p in highs[-2:]) <= tol
    rising_lows = all(b[2] > a[2] + tol * 0.05 for a, b in zip(lows, lows[1:]))
    return bool(_prior_impulse(_head(f, max(8, len(f) // 2)), "up") and highs_flat and rising_lows and _bullish_breakout_ready(f, resistance, tol))


def bearish_descending_triangle(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 24)
    pivots = _find_pivots(f)
    highs = [item for item in pivots if item[0] == "H"][-3:]
    lows = [item for item in pivots if item[0] == "L"][-3:]
    if len(highs) < 2 or len(lows) < 2:
        return False
    support = sum(p[2] for p in lows[-2:]) / 2.0
    tol = _level_tolerance(f, support)
    lows_flat = max(abs(p[2] - support) for p in lows[-2:]) <= tol
    falling_highs = all(b[2] < a[2] - tol * 0.05 for a, b in zip(highs, highs[1:]))
    return bool(_prior_impulse(_head(f, max(8, len(f) // 2)), "down") and lows_flat and falling_highs and _bearish_breakdown_ready(f, support, tol))


def bullish_symmetrical_triangle(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 24)
    pivots = _find_pivots(f)
    highs = [item for item in pivots if item[0] == "H"][-3:]
    lows = [item for item in pivots if item[0] == "L"][-3:]
    stats = _trend_stats(f.tail(min(len(f), 14)))
    if len(highs) < 2 or len(lows) < 2 or stats is None:
        return False
    descending_highs = all(b[2] < a[2] for a, b in zip(highs, highs[1:]))
    ascending_lows = all(b[2] > a[2] for a, b in zip(lows, lows[1:]))
    prior_ret = _ret_pct(_head(f, max(8, len(f) // 2)))
    upper = max(p[2] for p in highs[-2:])
    tol = _level_tolerance(f, upper)
    return bool(descending_highs and ascending_lows and prior_ret > 0.012 and stats.range_end < stats.range_start * 0.78 and _bullish_breakout_ready(f, upper, tol))


def bearish_symmetrical_triangle(frame: pd.DataFrame) -> bool:
    f = _tail(frame, 24)
    pivots = _find_pivots(f)
    highs = [item for item in pivots if item[0] == "H"][-3:]
    lows = [item for item in pivots if item[0] == "L"][-3:]
    stats = _trend_stats(f.tail(min(len(f), 14)))
    if len(highs) < 2 or len(lows) < 2 or stats is None:
        return False
    descending_highs = all(b[2] < a[2] for a, b in zip(highs, highs[1:]))
    ascending_lows = all(b[2] > a[2] for a, b in zip(lows, lows[1:]))
    prior_ret = _ret_pct(_head(f, max(8, len(f) // 2)))
    lower = min(p[2] for p in lows[-2:])
    tol = _level_tolerance(f, lower)
    return bool(descending_highs and ascending_lows and prior_ret < -0.012 and stats.range_end < stats.range_start * 0.78 and _bearish_breakdown_ready(f, lower, tol))


BULLISH_CHART_PATTERN_REGISTRY: dict[str, PatternFunc] = {
    "bullish_double_bottom": bullish_double_bottom,
    "bullish_inverse_head_and_shoulders": bullish_inverse_head_and_shoulders,
    "bullish_falling_wedge": bullish_falling_wedge,
    "bullish_broadening_bottom": bullish_broadening_bottom,
    "bullish_triple_bottom": bullish_triple_bottom,
    "bullish_flag": bullish_flag,
    "bullish_pennant": bullish_pennant,
    "bullish_ascending_triangle": bullish_ascending_triangle,
    "bullish_symmetrical_triangle": bullish_symmetrical_triangle,
}

BEARISH_CHART_PATTERN_REGISTRY: dict[str, PatternFunc] = {
    "bearish_double_top": bearish_double_top,
    "bearish_head_and_shoulders": bearish_head_and_shoulders,
    "bearish_rising_wedge": bearish_rising_wedge,
    "bearish_broadening_top": bearish_broadening_top,
    "bearish_triple_top": bearish_triple_top,
    "bearish_flag": bearish_flag,
    "bearish_pennant": bearish_pennant,
    "bearish_descending_triangle": bearish_descending_triangle,
    "bearish_symmetrical_triangle": bearish_symmetrical_triangle,
}

BULLISH_REVERSAL_CHART_PATTERNS = (
    "bullish_double_bottom",
    "bullish_inverse_head_and_shoulders",
    "bullish_falling_wedge",
    "bullish_broadening_bottom",
    "bullish_triple_bottom",
)

BEARISH_REVERSAL_CHART_PATTERNS = (
    "bearish_double_top",
    "bearish_head_and_shoulders",
    "bearish_rising_wedge",
    "bearish_broadening_top",
    "bearish_triple_top",
)

BULLISH_CONTINUATION_CHART_PATTERNS = (
    "bullish_flag",
    "bullish_pennant",
    "bullish_ascending_triangle",
    "bullish_symmetrical_triangle",
)

BEARISH_CONTINUATION_CHART_PATTERNS = (
    "bearish_flag",
    "bearish_pennant",
    "bearish_descending_triangle",
    "bearish_symmetrical_triangle",
)

DEFAULT_BULLISH_CHART_PATTERNS = list(BULLISH_CHART_PATTERN_REGISTRY.keys())
DEFAULT_BEARISH_CHART_PATTERNS = list(BEARISH_CHART_PATTERN_REGISTRY.keys())


def chart_pattern_group_tokens(*, bullish: bool) -> tuple[str, ...]:
    side = "bullish" if bullish else "bearish"
    return side, f"{side}_reversal", f"{side}_continuation", f"{side}_all", "all"


def chart_pattern_allowed_tokens(*, bullish: bool) -> tuple[str, ...]:
    registry = BULLISH_CHART_PATTERN_REGISTRY if bullish else BEARISH_CHART_PATTERN_REGISTRY
    return tuple(sorted(set(chart_pattern_group_tokens(bullish=bullish)) | set(registry.keys())))


def invalid_allowed_chart_patterns(allowed_patterns: Iterable[str] | None, *, bullish: bool) -> list[str]:
    if allowed_patterns is None:
        return []
    allowed_tokens = set(chart_pattern_allowed_tokens(bullish=bullish))
    invalid: set[str] = set()
    for raw in allowed_patterns:
        token = _normalize_token(raw)
        if token and token not in allowed_tokens:
            invalid.add(token)
    return sorted(invalid)


def _normalize_allowed_patterns(allowed_patterns: Iterable[str] | None, bullish: bool) -> set[str]:
    registry = BULLISH_CHART_PATTERN_REGISTRY if bullish else BEARISH_CHART_PATTERN_REGISTRY
    reversal = set(BULLISH_REVERSAL_CHART_PATTERNS if bullish else BEARISH_REVERSAL_CHART_PATTERNS)
    continuation = set(BULLISH_CONTINUATION_CHART_PATTERNS if bullish else BEARISH_CONTINUATION_CHART_PATTERNS)
    defaults = set(DEFAULT_BULLISH_CHART_PATTERNS if bullish else DEFAULT_BEARISH_CHART_PATTERNS)
    side = "bullish" if bullish else "bearish"
    groups = {
        side: set(registry.keys()),
        f"{side}_reversal": reversal,
        f"{side}_continuation": continuation,
        f"{side}_all": set(registry.keys()),
        "all": set(registry.keys()),
    }
    if allowed_patterns is None:
        return defaults
    raw = {_normalize_token(p) for p in allowed_patterns if str(p).strip()}
    if not raw:
        return set()
    selected: set[str] = set()
    for token in raw:
        if token in groups:
            selected.update(groups[token])
        elif token in registry:
            selected.add(token)
    return selected


def detect_bullish_chart_patterns(frame: pd.DataFrame, allowed_patterns: Iterable[str] | None = None, lookback_bars: int = 40) -> set[str]:
    f = _tail(frame, max(lookback_bars, 12))
    allowed = _normalize_allowed_patterns(allowed_patterns, bullish=True)
    return {name for name in allowed if BULLISH_CHART_PATTERN_REGISTRY[name](f)} if allowed else set()


def detect_bearish_chart_patterns(frame: pd.DataFrame, allowed_patterns: Iterable[str] | None = None, lookback_bars: int = 40) -> set[str]:
    f = _tail(frame, max(lookback_bars, 12))
    allowed = _normalize_allowed_patterns(allowed_patterns, bullish=False)
    return {name for name in allowed if BEARISH_CHART_PATTERN_REGISTRY[name](f)} if allowed else set()


def _recent_range_position(frame: pd.DataFrame, bars: int) -> tuple[float, float, float, float]:
    f = _tail(frame, max(4, bars))
    if f is None or f.empty:
        return 0.5, 0.0, 0.0, 0.0
    low = float(f["low"].astype(float).min())
    high = float(f["high"].astype(float).max())
    span = max(high - low, 1e-9)
    close = _close(f)
    return (close - low) / span, low, high, span


def _bullish_pattern_still_valid(frame: pd.DataFrame, *, continuation: bool) -> bool:
    if frame is None or frame.empty:
        return False
    bars = 8 if continuation else 10
    pos, recent_low, recent_high, span = _recent_range_position(frame, bars)
    f = _tail(frame, max(6, bars))
    close = _close(f)
    ema9 = _ema(f, "ema9")
    vwap = _ema(f, "vwap")
    atr_pct = max(_atr_pct(f), 0.0015)
    recent_move = _recent_move(f, 3)
    tolerance = _level_tolerance(f, close)
    support_floor = recent_low + (span * (0.14 if continuation else 0.10))
    decisive_break = close < support_floor - max(tolerance * 0.10, span * 0.02)
    below_trend = close < ema9 * (0.993 if continuation else 0.990) and close < vwap * (0.997 if continuation else 0.995)
    adverse_move = recent_move <= -max(0.0045 if continuation else 0.0060, atr_pct * (0.80 if continuation else 1.00))
    stale_position = pos < (0.34 if continuation else 0.24)
    return not ((decisive_break and below_trend) or (below_trend and adverse_move and stale_position))


def _bearish_pattern_still_valid(frame: pd.DataFrame, *, continuation: bool) -> bool:
    if frame is None or frame.empty:
        return False
    bars = 8 if continuation else 10
    pos, recent_low, recent_high, span = _recent_range_position(frame, bars)
    f = _tail(frame, max(6, bars))
    close = _close(f)
    ema9 = _ema(f, "ema9")
    vwap = _ema(f, "vwap")
    atr_pct = max(_atr_pct(f), 0.0015)
    recent_move = _recent_move(f, 3)
    tolerance = _level_tolerance(f, close)
    resistance_ceiling = recent_high - (span * (0.14 if continuation else 0.10))
    decisive_break = close > resistance_ceiling + max(tolerance * 0.10, span * 0.02)
    above_trend = close > ema9 * (1.007 if continuation else 1.010) and close > vwap * (1.003 if continuation else 1.005)
    adverse_move = recent_move >= max(0.0045 if continuation else 0.0060, atr_pct * (0.80 if continuation else 1.00))
    stale_position = pos > (0.66 if continuation else 0.76)
    return not ((decisive_break and above_trend) or (above_trend and adverse_move and stale_position))


def _filter_active_patterns(frame: pd.DataFrame, patterns: set[str], *, bullish: bool) -> tuple[set[str], set[str]]:
    active: set[str] = set()
    invalidated: set[str] = set()
    continuation_patterns = set(BULLISH_CONTINUATION_CHART_PATTERNS if bullish else BEARISH_CONTINUATION_CHART_PATTERNS)
    for name in patterns:
        continuation = name in continuation_patterns
        still_valid = _bullish_pattern_still_valid(frame, continuation=continuation) if bullish else _bearish_pattern_still_valid(frame, continuation=continuation)
        if still_valid:
            active.add(name)
        else:
            invalidated.add(name)
    return active, invalidated


def analyze_chart_pattern_context(
    frame: pd.DataFrame,
    bullish_allowed: Iterable[str] | None = None,
    bearish_allowed: Iterable[str] | None = None,
    lookback_bars: int = 40,
) -> ChartPatternContext:
    try:
        clean_frame = _clean_price_frame(frame)
        bullish_detected = detect_bullish_chart_patterns(clean_frame, allowed_patterns=bullish_allowed, lookback_bars=lookback_bars)
        bearish_detected = detect_bearish_chart_patterns(clean_frame, allowed_patterns=bearish_allowed, lookback_bars=lookback_bars)
        bullish, invalidated_bullish = _filter_active_patterns(clean_frame, bullish_detected, bullish=True)
        bearish, invalidated_bearish = _filter_active_patterns(clean_frame, bearish_detected, bullish=False)
        bullish_reversal = bullish & set(BULLISH_REVERSAL_CHART_PATTERNS)
        bullish_continuation = bullish & set(BULLISH_CONTINUATION_CHART_PATTERNS)
        bearish_reversal = bearish & set(BEARISH_REVERSAL_CHART_PATTERNS)
        bearish_continuation = bearish & set(BEARISH_CONTINUATION_CHART_PATTERNS)
        bias_score = (
            0.65 * len(bullish_reversal)
            + 1.10 * len(bullish_continuation)
            - 0.65 * len(bearish_reversal)
            - 1.10 * len(bearish_continuation)
        )
        regime_hint = "neutral"
        if bias_score >= 1.0:
            regime_hint = "bullish"
        elif bias_score <= -1.0:
            regime_hint = "bearish"
        return ChartPatternContext(
            matched_bullish=bullish,
            matched_bullish_reversal=bullish_reversal,
            matched_bullish_continuation=bullish_continuation,
            matched_bearish=bearish,
            matched_bearish_reversal=bearish_reversal,
            matched_bearish_continuation=bearish_continuation,
            invalidated_bullish=invalidated_bullish,
            invalidated_bearish=invalidated_bearish,
            bias_score=bias_score,
            regime_hint=regime_hint,
        )
    finally:
        # Clear the per-call memoization cache so entries don't accumulate
        # across ticks (id()-based keys can collide with recycled ids).
        _clear_chart_cache()
