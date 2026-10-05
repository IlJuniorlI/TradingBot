# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

# TA-Lib is a pinned dependency. A missing one (or its C library) fails
# pattern detection where it is used, with the import error as the cause
# (_require_talib); any other import failure (a broken build) raises here.
_TALIB_IMPORT_ERROR: ImportError | None = None
try:
    import talib  # type: ignore
except ImportError as exc:  # pragma: no cover - TA-Lib is installed wherever the suite runs
    talib = None
    _TALIB_IMPORT_ERROR = exc


def _require_talib() -> Any:
    if talib is None:
        raise RuntimeError(
            f"TA-Lib is required for candlestick pattern detection but could not be imported: {_TALIB_IMPORT_ERROR}"
        ) from _TALIB_IMPORT_ERROR
    return talib


# Minimum bars to feed TA-Lib for candle pattern detection. TA-Lib
# candle functions build internal state (average body size, trend
# context) from preceding bars; with only 3 rows of input it can't
# initialize and returns zeros even for textbook patterns. 30 bars
# is enough for every registered pattern's lookback (max ~14 for
# CDLEVENINGDOJISTAR et al) plus warmup. Exposed for callers so the
# cache-key slice and detection slice stay consistent.
CANDLE_CONTEXT_BARS: int = 30

# How many bars back a completed pattern stays reportable. TA-Lib patterns get
# this window for free in ``_talib_pattern_value_from_key``, which scans the
# last three outputs; the two custom tweezers only ever compared the final
# pair, so they vanished one bar after completing while every TA-Lib pattern of
# the same 2-bar tier survived three. That asymmetry reached the tier cascade:
# a tweezer aging out of view let a 1-bar match win the cascade and downgrade
# the confirm tier from solid_2c (0.70) to weak_1c (0.35) on the same bars.
CANDLE_PERSISTENCE_BARS: int = 3


FIXED_BULLISH_1C_PATTERNS: tuple[str, ...] = (
    "CDLDRAGONFLYDOJI",
    "CDLHAMMER",
    "CDLINVERTEDHAMMER",
    "CDLTAKURI",
)
FIXED_BEARISH_1C_PATTERNS: tuple[str, ...] = (
    "CDLGRAVESTONEDOJI",
    "CDLHANGINGMAN",
    "CDLSHOOTINGSTAR",
)
SIGN_DEPENDENT_1C_PATTERNS: tuple[str, ...] = (
    "CDLBELTHOLD",
    "CDLCLOSINGMARUBOZU",
    "CDLLONGLINE",
    "CDLMARUBOZU",
)
FIXED_BULLISH_2C_PATTERNS: tuple[str, ...] = (
    "CDLHOMINGPIGEON",
    "CDLMATCHINGLOW",
    "CDLPIERCING",
    "TWEEZER_BOTTOM",
)
FIXED_BEARISH_2C_PATTERNS: tuple[str, ...] = (
    "CDLDARKCLOUDCOVER",
    "CDLINNECK",
    "CDLONNECK",
    "CDLTHRUSTING",
    "TWEEZER_TOP",
)
SIGN_DEPENDENT_2C_PATTERNS: tuple[str, ...] = (
    "CDLCOUNTERATTACK",
    "CDLDOJISTAR",
    "CDLENGULFING",
    "CDLHARAMI",
    "CDLHARAMICROSS",
    "CDLKICKING",
    "CDLKICKINGBYLENGTH",
    "CDLSEPARATINGLINES",
)

FIXED_BULLISH_3C_PATTERNS: tuple[str, ...] = (
    "CDL3STARSINSOUTH",
    "CDL3WHITESOLDIERS",
    "CDLMORNINGDOJISTAR",
    "CDLMORNINGSTAR",
    "CDLSTICKSANDWICH",
    "CDLUNIQUE3RIVER",
)
FIXED_BEARISH_3C_PATTERNS: tuple[str, ...] = (
    "CDL2CROWS",
    "CDL3BLACKCROWS",
    "CDLADVANCEBLOCK",
    "CDLEVENINGDOJISTAR",
    "CDLEVENINGSTAR",
    "CDLIDENTICAL3CROWS",
    "CDLSTALLEDPATTERN",
    "CDLUPSIDEGAP2CROWS",
)
SIGN_DEPENDENT_3C_PATTERNS: tuple[str, ...] = (
    "CDL3INSIDE",
    "CDL3OUTSIDE",
    "CDLABANDONEDBABY",
    "CDLGAPSIDESIDEWHITE",
    "CDLHIKKAKE",
    "CDLTASUKIGAP",
    "CDLTRISTAR",
    "CDLXSIDEGAP3METHODS",
)

BULLISH_1C_PATTERNS: tuple[str, ...] = FIXED_BULLISH_1C_PATTERNS + SIGN_DEPENDENT_1C_PATTERNS
BEARISH_1C_PATTERNS: tuple[str, ...] = FIXED_BEARISH_1C_PATTERNS + SIGN_DEPENDENT_1C_PATTERNS
BULLISH_2C_PATTERNS: tuple[str, ...] = FIXED_BULLISH_2C_PATTERNS + SIGN_DEPENDENT_2C_PATTERNS
BEARISH_2C_PATTERNS: tuple[str, ...] = FIXED_BEARISH_2C_PATTERNS + SIGN_DEPENDENT_2C_PATTERNS
BULLISH_3C_PATTERNS: tuple[str, ...] = FIXED_BULLISH_3C_PATTERNS + SIGN_DEPENDENT_3C_PATTERNS
BEARISH_3C_PATTERNS: tuple[str, ...] = FIXED_BEARISH_3C_PATTERNS + SIGN_DEPENDENT_3C_PATTERNS

ALL_BULLISH_PATTERNS: tuple[str, ...] = BULLISH_1C_PATTERNS + BULLISH_2C_PATTERNS + BULLISH_3C_PATTERNS
ALL_BEARISH_PATTERNS: tuple[str, ...] = BEARISH_1C_PATTERNS + BEARISH_2C_PATTERNS + BEARISH_3C_PATTERNS

DEFAULT_BULLISH_PATTERNS: list[str] = ["BULLISH_1C", "BULLISH_2C", "BULLISH_3C"]
DEFAULT_BEARISH_PATTERNS: list[str] = ["BEARISH_1C", "BEARISH_2C", "BEARISH_3C"]

PATTERN_LENGTHS: dict[str, int] = {
    **{name: 1 for name in BULLISH_1C_PATTERNS},
    **{name: 1 for name in BEARISH_1C_PATTERNS},
    **{name: 2 for name in BULLISH_2C_PATTERNS},
    **{name: 2 for name in BEARISH_2C_PATTERNS},
    **{name: 3 for name in BULLISH_3C_PATTERNS},
    **{name: 3 for name in BEARISH_3C_PATTERNS},
}

LENGTH_WEIGHTS: dict[int, float] = {
    1: 0.35,
    2: 0.70,
    3: 1.00,
}
CORROBORATION_PER_EXTRA = 0.10
CORROBORATION_CAP = 0.25
OPPOSITE_PENALTY_MULT = 0.75
MIXED_NEUTRAL_THRESHOLD = 0.40

CANDLE_CONFIRM_NONE = "none"
CANDLE_CONFIRM_WEAK = "weak_1c"
CANDLE_CONFIRM_SOLID = "solid_2c"
CANDLE_CONFIRM_STRONG = "strong_3c"
CANDLE_CONFIRM_CONTRIBUTIONS: dict[str, float] = {
    CANDLE_CONFIRM_NONE: 0.0,
    CANDLE_CONFIRM_WEAK: 0.35,
    CANDLE_CONFIRM_SOLID: 0.70,
    CANDLE_CONFIRM_STRONG: 1.00,
}

_SIGN_DEPENDENT_PATTERNS: set[str] = set(SIGN_DEPENDENT_1C_PATTERNS + SIGN_DEPENDENT_2C_PATTERNS + SIGN_DEPENDENT_3C_PATTERNS)
_BULLISH_GROUPS: dict[str, set[str]] = {
    "BULLISH_1C": set(BULLISH_1C_PATTERNS),
    "BULLISH_2C": set(BULLISH_2C_PATTERNS),
    "BULLISH_3C": set(BULLISH_3C_PATTERNS),
    "ALL": set(ALL_BULLISH_PATTERNS),
}
_BEARISH_GROUPS: dict[str, set[str]] = {
    "BEARISH_1C": set(BEARISH_1C_PATTERNS),
    "BEARISH_2C": set(BEARISH_2C_PATTERNS),
    "BEARISH_3C": set(BEARISH_3C_PATTERNS),
    "ALL": set(ALL_BEARISH_PATTERNS),
}


BULLISH_PATTERN_REGISTRY: dict[str, str] = {name: "talib" for name in ALL_BULLISH_PATTERNS}
BULLISH_PATTERN_REGISTRY["TWEEZER_BOTTOM"] = "custom"
BEARISH_PATTERN_REGISTRY: dict[str, str] = {name: "talib" for name in ALL_BEARISH_PATTERNS}
BEARISH_PATTERN_REGISTRY["TWEEZER_TOP"] = "custom"


def candle_group_tokens(*, bullish: bool) -> tuple[str, ...]:
    groups = _BULLISH_GROUPS if bullish else _BEARISH_GROUPS
    return tuple(groups.keys())


def candle_allowed_tokens(*, bullish: bool) -> tuple[str, ...]:
    registry = BULLISH_PATTERN_REGISTRY if bullish else BEARISH_PATTERN_REGISTRY
    groups = _BULLISH_GROUPS if bullish else _BEARISH_GROUPS
    return tuple(sorted(set(groups.keys()) | set(registry.keys())))


def invalid_allowed_patterns(allowed_patterns: Iterable[str] | None, *, bullish: bool) -> list[str]:
    if allowed_patterns is None:
        return []
    allowed_tokens = set(candle_allowed_tokens(bullish=bullish))
    invalid: set[str] = set()
    for raw in allowed_patterns:
        token = _normalize_token(raw)
        if token and token not in allowed_tokens:
            invalid.add(token)
    return sorted(invalid)


def pattern_length(name: str) -> int:
    return int(PATTERN_LENGTHS.get(str(name or "").strip().upper(), 1))


def pattern_weight(name: str) -> float:
    return float(LENGTH_WEIGHTS.get(pattern_length(name), LENGTH_WEIGHTS[1]))


def _normalize_token(value: object) -> str:
    return str(value or "").strip().upper()


def _normalize_allowed_patterns(allowed_patterns: Iterable[str] | None, bullish: bool) -> tuple[str, ...]:
    registry = BULLISH_PATTERN_REGISTRY if bullish else BEARISH_PATTERN_REGISTRY
    defaults = DEFAULT_BULLISH_PATTERNS if bullish else DEFAULT_BEARISH_PATTERNS
    groups = _BULLISH_GROUPS if bullish else _BEARISH_GROUPS
    if allowed_patterns is None:
        selected = set().union(*(groups[token] for token in defaults))
        return tuple(sorted(selected))
    raw = {_normalize_token(pattern) for pattern in allowed_patterns if str(pattern).strip()}
    if not raw:
        return tuple()
    selected: set[str] = set()
    for token in raw:
        if token in groups:
            selected.update(groups[token])
        elif token in registry:
            selected.add(token)
    return tuple(sorted(selected))


_OHLC_COLUMNS = ["open", "high", "low", "close"]

# One bar of a frame key: (open, high, low, close). The key is the hashable
# form of an _ohlc_subset, the lru_cache key of every detector below.
_Bar = tuple[float, float, float, float]
_FrameKey = tuple[_Bar, ...]


def _ohlc_subset(frame: pd.DataFrame | None, lookback: int, *, min_bars: int) -> pd.DataFrame | None:
    """The last ``max(lookback, min_bars)`` bars of ``frame``'s
    open/high/low/close, read as numbers, less every bar with a field that
    is missing or does not read as one; None when no bar is left.

    ``min_bars`` is the caller's floor on ``lookback``: 1 for the
    latest-snapshot key, ``CANDLE_CONTEXT_BARS`` for the per-bar map.
    """
    if frame is None or frame.empty:
        return None
    tail = frame.tail(max(int(lookback), min_bars))
    if all(tail[col].dtype == np.float64 for col in _OHLC_COLUMNS):
        # Already numbers: drop the rows with a missing field on the array
        # (the pandas conversion and dropna cost ~1 ms on these few bars).
        values = np.column_stack([tail[col].to_numpy() for col in _OHLC_COLUMNS])
        keep = ~np.isnan(values).any(axis=1)
        if not keep.any():
            return None
        return pd.DataFrame(values[keep], index=tail.index[keep], columns=_OHLC_COLUMNS)
    subset = tail[_OHLC_COLUMNS].copy()
    for col in _OHLC_COLUMNS:
        subset[col] = pd.to_numeric(subset[col], errors="coerce")
    subset = subset.dropna(subset=_OHLC_COLUMNS)
    return None if subset.empty else subset


def _key_from_subset(subset: pd.DataFrame) -> _FrameKey:
    return tuple(
        (float(open_), float(high), float(low), float(close))
        for open_, high, low, close in subset.itertuples(index=False, name=None)
    )


def _ohlc_frame_key(frame: pd.DataFrame | None, lookback: int = CANDLE_CONTEXT_BARS) -> _FrameKey:
    subset = _ohlc_subset(frame, lookback, min_bars=1)
    return tuple() if subset is None else _key_from_subset(subset)


# A key hits only while its bars are unchanged (inside one minute), so a
# few minutes of keys is enough; each holds its ~11 KB key tuple.
@lru_cache(maxsize=256)
def _ohlc_arrays_from_key(frame_key: _FrameKey) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if not frame_key:
        empty = np.asarray([], dtype=float)
        return empty, empty, empty, empty
    data = np.asarray(frame_key, dtype=float)
    return (
        data[:, 0].astype(float, copy=False),
        data[:, 1].astype(float, copy=False),
        data[:, 2].astype(float, copy=False),
        data[:, 3].astype(float, copy=False),
    )


@lru_cache(maxsize=1024)
def _talib_pattern_value_from_key(
    frame_key: _FrameKey,
    func_name: str,
) -> int:
    if not frame_key:
        return 0
    func = getattr(_require_talib(), func_name)
    opens, highs, lows, closes = _ohlc_arrays_from_key(frame_key)
    values = func(opens, highs, lows, closes)
    if len(values) == 0:
        return 0
    # Scan the tail of the output (most recent first) and return the first
    # non-zero signal. TA-Lib emits the signal on the completion bar of a
    # pattern — e.g. a 2-bar engulfing ending at bar N-1 lands at values[-2],
    # not values[-1]. Reading only values[-1] would discard any pattern that
    # completed on an earlier bar of the CANDLE_PERSISTENCE_BARS window, which
    # made patterns "disappear" from the report as soon as a new bar arrived
    # even though they're still visible on the chart (INTC 2026-04-24 10:08
    # bullish engulfing was lost at the 10:09 cycle). Signed value preserved:
    # +N = bullish, -N = bearish (TA-Lib's ±100 and occasional ±200).
    for idx in range(-1, -CANDLE_PERSISTENCE_BARS - 1, -1):
        if len(values) + idx < 0:
            break
        signal = int(values[idx])
        if signal != 0:
            return signal
    return 0


def _range_from_row(row: _Bar) -> float:
    _open, high, low, _close = row
    return max(0.0, high - low)


def _bull_from_row(row: _Bar) -> bool:
    open_, _high, _low, close = row
    return close > open_


def _bear_from_row(row: _Bar) -> bool:
    open_, _high, _low, close = row
    return close < open_


def _near(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= max(0.0, tolerance)


def _tweezer_bottom_at(pair: tuple[_Bar, _Bar]) -> bool:
    prev, cur = pair
    tolerance = max(_range_from_row(prev), _range_from_row(cur)) * 0.05
    return bool(_bear_from_row(prev) and _bull_from_row(cur) and _near(prev[2], cur[2], tolerance))


def _tweezer_top_at(pair: tuple[_Bar, _Bar]) -> bool:
    prev, cur = pair
    tolerance = max(_range_from_row(prev), _range_from_row(cur)) * 0.05
    return bool(_bull_from_row(prev) and _bear_from_row(cur) and _near(prev[1], cur[1], tolerance))


def _tweezer_pairs(
    frame_key: _FrameKey,
) -> list[tuple[_Bar, _Bar]]:
    """The adjacent bar pairs completing within the persistence window."""
    pairs = []
    for offset in range(CANDLE_PERSISTENCE_BARS):
        end = len(frame_key) - offset
        if end < 2:
            break
        pairs.append((frame_key[end - 2], frame_key[end - 1]))
    return pairs


def _tweezer_bottom_from_key(frame_key: _FrameKey) -> bool:
    return any(_tweezer_bottom_at(pair) for pair in _tweezer_pairs(frame_key))


def _tweezer_top_from_key(frame_key: _FrameKey) -> bool:
    return any(_tweezer_top_at(pair) for pair in _tweezer_pairs(frame_key))


def _talib_value_matches_side(token: str, value: int, *, bullish: bool) -> bool:
    """Does a TA-Lib output ``value`` count as a match for this side?

    SIGN-DEPENDENT patterns (engulfing, harami, marubozu, ...) encode their
    direction in the sign, so the sign decides. FIXED-direction patterns take
    their direction from the list they are in; for them TA-Lib's sign is not
    a direction and any non-zero value is a match.

    Until 2026-09-22 every pattern was matched on its sign, which is only
    correct if TA-Lib always signs a fixed pattern the way its list does. It
    does not: ``CDLGRAVESTONEDOJI`` sits in the BEARISH list and TA-Lib emits
    it as +100 -- 9,181 times across 400 real 1m frames, never once negative
    -- so a bearish match, which required a negative value, was impossible.
    Every other fixed pattern was checked on the same data and is signed to
    match its list; for those this changes nothing.
    """
    if token in _SIGN_DEPENDENT_PATTERNS:
        return value > 0 if bullish else value < 0
    return value != 0


def _evaluate_side_pattern(
    frame_key: _FrameKey,
    name: str,
    *,
    bullish: bool,
) -> bool:
    token = _normalize_token(name)
    if not token:
        return False
    if bullish and token == "TWEEZER_BOTTOM":
        return _tweezer_bottom_from_key(frame_key)
    if (not bullish) and token == "TWEEZER_TOP":
        return _tweezer_top_from_key(frame_key)
    value = _talib_pattern_value_from_key(frame_key, token)
    return _talib_value_matches_side(token, value, bullish=bullish)


def _tier_cascade(matches: Iterable[str]) -> tuple[str, ...]:
    """Longest tier wins: the ``matches`` of the longest pattern length
    (3, then 2, then 1 bars), sorted; empty when nothing matched.

    Rationale: a 3-bar Morning Star describes the same 3 candles that
    also fire 1-bar Marubozu / Belt Hold / Long Line on the 3rd bar. The
    1-bar readings are redundant noise once the 3-bar story is detected.
    The cascade reports the structurally meaningful pattern and drops
    the overlapping shorter ones. When no 3-bar pattern fires, falls
    back to 2-bar; when no 2-bar fires, falls back to 1-bar. Fixes the
    user-facing "4 bullish + 3 bearish patterns" noise where a single
    strong-bodied bar generates a flood of overlapping 1-bar readings.
    """
    names = list(matches)
    if not names:
        return tuple()
    longest = max(pattern_length(name) for name in names)
    return tuple(sorted(name for name in names if pattern_length(name) == longest))


@lru_cache(maxsize=4096)
def _detect_side_patterns_cached(
    frame_key: _FrameKey,
    allowed: tuple[str, ...],
    bullish: bool,
) -> tuple[str, ...]:
    """The ``allowed`` patterns that match the latest bars, through
    ``_tier_cascade``. Each pattern fires via ``_evaluate_side_pattern``
    using the 3-bar persistence window (TA-Lib's completion-bar lookback).
    """
    if not frame_key or not allowed:
        return tuple()
    return _tier_cascade(name for name in allowed if _evaluate_side_pattern(frame_key, name, bullish=bullish))


def summarize_pattern_matches(matches: Iterable[str] | None) -> dict[str, Any]:
    names = sorted({str(name).strip().upper() for name in (matches or []) if str(name).strip()})
    if not names:
        return {
            "matched_patterns": [],
            "pattern_count": 0,
            "anchor_pattern": None,
            "anchor_bars": 0,
            "anchor_weight": 0.0,
            "corroboration_bonus": 0.0,
            "score": 0.0,
        }
    anchor_pattern = max(names, key=lambda name: (pattern_weight(name), pattern_length(name), name))
    anchor_bars = pattern_length(anchor_pattern)
    anchor_weight = pattern_weight(anchor_pattern)
    corroboration_bonus = min(CORROBORATION_CAP, CORROBORATION_PER_EXTRA * max(0, len(names) - 1))
    score = anchor_weight + corroboration_bonus
    return {
        "matched_patterns": names,
        "pattern_count": len(names),
        "anchor_pattern": anchor_pattern,
        "anchor_bars": anchor_bars,
        "anchor_weight": round(float(anchor_weight), 4),
        "corroboration_bonus": round(float(corroboration_bonus), 4),
        "score": round(float(score), 4),
    }


def summarize_candle_context_from_matches(
    bullish_matches: Iterable[str] | None,
    bearish_matches: Iterable[str] | None,
) -> dict[str, Any]:
    bullish = summarize_pattern_matches(bullish_matches)
    bearish = summarize_pattern_matches(bearish_matches)
    bullish_anchor_weight = float(bullish["anchor_weight"])
    bearish_anchor_weight = float(bearish["anchor_weight"])
    bullish_score = float(bullish["score"])
    bearish_score = float(bearish["score"])
    bullish_net_score = max(0.0, bullish_score - (OPPOSITE_PENALTY_MULT * bearish_anchor_weight))
    bearish_net_score = max(0.0, bearish_score - (OPPOSITE_PENALTY_MULT * bullish_anchor_weight))
    candle_bias_score = bullish_net_score - bearish_net_score

    bullish_count = int(bullish["pattern_count"])
    bearish_count = int(bearish["pattern_count"])
    if bullish_count and bearish_count and max(bullish_net_score, bearish_net_score) < MIXED_NEUTRAL_THRESHOLD:
        candle_regime_hint = "mixed"
    elif bullish_count and bearish_count:
        candle_regime_hint = (
            "bullish_reversal"
            if bullish_net_score > bearish_net_score
            else "bearish_reversal"
            if bearish_net_score > bullish_net_score
            else "mixed"
        )
    elif bullish_count:
        candle_regime_hint = "bullish_reversal"
    elif bearish_count:
        candle_regime_hint = "bearish_reversal"
    else:
        candle_regime_hint = "neutral"

    return {
        "matched_bullish_candles": list(bullish["matched_patterns"]),
        "matched_bearish_candles": list(bearish["matched_patterns"]),
        "bullish_candle_score": round(float(bullish_score), 4),
        "bearish_candle_score": round(float(bearish_score), 4),
        "bullish_candle_net_score": round(float(bullish_net_score), 4),
        "bearish_candle_net_score": round(float(bearish_net_score), 4),
        "bullish_candle_anchor_pattern": bullish["anchor_pattern"],
        "bearish_candle_anchor_pattern": bearish["anchor_pattern"],
        "bullish_candle_anchor_bars": int(bullish["anchor_bars"]),
        "bearish_candle_anchor_bars": int(bearish["anchor_bars"]),
        "bullish_candle_anchor_weight": round(float(bullish_anchor_weight), 4),
        "bearish_candle_anchor_weight": round(float(bearish_anchor_weight), 4),
        "bullish_candle_corroboration_bonus": round(float(bullish["corroboration_bonus"]), 4),
        "bearish_candle_corroboration_bonus": round(float(bearish["corroboration_bonus"]), 4),
        "candle_bias_score": round(float(candle_bias_score), 4),
        "candle_net_score": round(float(candle_bias_score), 4),
        "candle_regime_hint": str(candle_regime_hint),
    }


@lru_cache(maxsize=2048)
def _detect_candle_context_cached(
    frame_key: _FrameKey,
    bullish_allowed: tuple[str, ...],
    bearish_allowed: tuple[str, ...],
) -> dict[str, Any]:
    bullish = _detect_side_patterns_cached(frame_key, bullish_allowed, True)
    bearish = _detect_side_patterns_cached(frame_key, bearish_allowed, False)
    return summarize_candle_context_from_matches(bullish, bearish)


def _copy_candle_context(ctx: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in ctx.items():
        if isinstance(value, list):
            out[key] = list(value)
        else:
            out[key] = value
    return out


def detect_candle_context(
    frame: pd.DataFrame,
    bullish_allowed: Iterable[str] | None = None,
    bearish_allowed: Iterable[str] | None = None,
) -> dict[str, Any]:
    # Give TA-Lib enough context to initialize. Callers used to pre-slice
    # to tail(3) which is too narrow — TA-Lib can't compute body-average
    # or trend context from 3 bars, so most patterns returned 0 even on
    # textbook setups. Slice internally now; callers may pass whatever
    # length they have.
    frame_key = _ohlc_frame_key(frame, lookback=CANDLE_CONTEXT_BARS)
    if not frame_key:
        return summarize_candle_context_from_matches(set(), set())
    bullish = _normalize_allowed_patterns(bullish_allowed, bullish=True)
    bearish = _normalize_allowed_patterns(bearish_allowed, bullish=False)
    return _copy_candle_context(_detect_candle_context_cached(frame_key, bullish, bearish))


# Above one pass's working set (dashboard symbols x TA-Lib tokens: 28 x 48
# on top_tier), so a pass without a new bar hits.
@lru_cache(maxsize=4096)
def _talib_pattern_array_from_key(
    frame_key: _FrameKey,
    func_name: str,
) -> tuple[int, ...]:
    """Full per-bar TA-Lib output for ``func_name`` against ``frame_key``.
    Returns a tuple of int signals, one per input bar (length == len(frame_key)).
    +200/+100 are bullish completions, -200/-100 are bearish, 0 otherwise.
    Used by ``detect_per_bar_candle_patterns`` for dashboard per-bar tooltip
    display. The existing latest-snapshot scanner
    ``_talib_pattern_value_from_key`` is unchanged."""
    if not frame_key:
        return tuple()
    func = getattr(_require_talib(), func_name)
    opens, highs, lows, closes = _ohlc_arrays_from_key(frame_key)
    values = func(opens, highs, lows, closes)
    return tuple(int(v) for v in values)


def detect_per_bar_candle_patterns(
    frame: pd.DataFrame,
    bullish_allowed: Iterable[str] | None = None,
    bearish_allowed: Iterable[str] | None = None,
    lookback: int = CANDLE_CONTEXT_BARS,
) -> dict[Any, dict[str, list[str]]]:
    """Per-bar candle pattern map keyed by bar timestamp (the DataFrame's
    index value at the completion bar).

    Each value is ``{"bullish": [names], "bearish": [names]}`` after the
    longest-tier-wins cascade has been applied PER BAR (3-bar pattern at
    a bar suppresses overlapping 2-bar/1-bar patterns at the same bar).

    Patterns appear ONLY on their completion bar — the bar where TA-Lib
    emits the signal. Multi-bar patterns are NOT spread across constituent
    bars; hover the completion bar to see them.

    ``lookback`` controls how many bars from the tail of ``frame`` are fed
    to TA-Lib. Defaults to ``CANDLE_CONTEXT_BARS`` (30, sufficient for
    snapshot-only callers); the dashboard chart payload passes its
    ``max_bars`` (default 90) so per-bar patterns are available across the
    full visible chart, not just the last 30 bars. Clamped to a minimum of
    ``CANDLE_CONTEXT_BARS`` because TA-Lib's pattern functions need at
    least that much context to initialize their internal body-average /
    trend state — the first few outputs of a shorter input are unreliable.

    Used by the dashboard chart payload for honest per-bar tooltip display.
    The existing ``detect_candle_context`` (latest-snapshot semantics) is
    unchanged and remains the source for the strategy's tech_ctx +
    candle_pattern_exit gates.
    """
    subset = _ohlc_subset(frame, lookback, min_bars=CANDLE_CONTEXT_BARS)
    if subset is None:
        return {}
    # The frame_key and the timestamps both come from the one dropna'd
    # subset, so TA-Lib output position i is the bar at timestamps[i]. Taking
    # the timestamps from a raw frame.tail() instead would shift every bar
    # after a NaN-OHLC one.
    frame_key = _key_from_subset(subset)
    timestamps = list(subset.index)
    bullish_allowed_tuple = _normalize_allowed_patterns(bullish_allowed, bullish=True)
    bearish_allowed_tuple = _normalize_allowed_patterns(bearish_allowed, bullish=False)

    n = len(frame_key)
    bullish_by_pos: dict[int, list[str]] = {i: [] for i in range(n)}
    bearish_by_pos: dict[int, list[str]] = {i: [] for i in range(n)}

    for token in bullish_allowed_tuple:
        if not token:
            continue
        if token == "TWEEZER_BOTTOM":
            # 2-bar custom: completion at position i requires bars (i-1, i).
            # Uses the single-pair check, not _tweezer_bottom_from_key: this
            # map is completion-only (as the TA-Lib per-bar arrays are), so
            # the 3-bar persistence window must not smear a match forward.
            for i in range(1, n):
                if _tweezer_bottom_at((frame_key[i - 1], frame_key[i])):
                    bullish_by_pos[i].append(token)
            continue
        values = _talib_pattern_array_from_key(frame_key, token)
        for i, v in enumerate(values):
            if _talib_value_matches_side(token, v, bullish=True):
                bullish_by_pos[i].append(token)

    for token in bearish_allowed_tuple:
        if not token:
            continue
        if token == "TWEEZER_TOP":
            for i in range(1, n):
                if _tweezer_top_at((frame_key[i - 1], frame_key[i])):
                    bearish_by_pos[i].append(token)
            continue
        values = _talib_pattern_array_from_key(frame_key, token)
        for i, v in enumerate(values):
            if _talib_value_matches_side(token, v, bullish=False):
                bearish_by_pos[i].append(token)

    out: dict[Any, dict[str, list[str]]] = {}
    for i, ts in enumerate(timestamps):
        bull = list(_tier_cascade(bullish_by_pos[i]))
        bear = list(_tier_cascade(bearish_by_pos[i]))
        if not bull and not bear:
            continue
        out[ts] = {"bullish": bull, "bearish": bear}
    return out


def directional_candle_signal(candle_ctx: dict[str, Any] | None, *, bullish: bool) -> dict[str, Any]:
    ctx = candle_ctx or {}
    prefix = "bullish" if bullish else "bearish"
    score = float(ctx.get(f"{prefix}_candle_score", 0.0) or 0.0)
    net_score = float(ctx.get(f"{prefix}_candle_net_score", 0.0) or 0.0)
    anchor_pattern = ctx.get(f"{prefix}_candle_anchor_pattern")
    anchor_bars = int(ctx.get(f"{prefix}_candle_anchor_bars", 0) or 0)
    matches = list(ctx.get(f"matched_{prefix}_candles", []) or [])
    regime_hint = str(ctx.get("candle_regime_hint", "neutral") or "neutral")
    mixed = regime_hint == "mixed"
    if net_score >= 1.00:
        confirm_tier = CANDLE_CONFIRM_STRONG
    elif net_score >= 0.70:
        confirm_tier = CANDLE_CONFIRM_SOLID
    elif net_score >= 0.35:
        confirm_tier = CANDLE_CONFIRM_WEAK
    else:
        confirm_tier = CANDLE_CONFIRM_NONE
    return {
        "matches": matches,
        "score": round(score, 4),
        "net_score": round(net_score, 4),
        "anchor_pattern": anchor_pattern,
        "anchor_bars": anchor_bars,
        "anchor_weight": float(ctx.get(f"{prefix}_candle_anchor_weight", 0.0) or 0.0),
        "corroboration_bonus": float(ctx.get(f"{prefix}_candle_corroboration_bonus", 0.0) or 0.0),
        "opposite_score": float(ctx.get(f"{'bearish' if bullish else 'bullish'}_candle_score", 0.0) or 0.0),
        "opposite_net_score": float(ctx.get(f"{'bearish' if bullish else 'bullish'}_candle_net_score", 0.0) or 0.0),
        "regime_hint": regime_hint,
        "mixed": mixed,
        "confirmed": confirm_tier != CANDLE_CONFIRM_NONE,
        "confirm_tier": confirm_tier,
        "confirm_contribution": CANDLE_CONFIRM_CONTRIBUTIONS[confirm_tier],
        "one_candle_only": anchor_bars == 1 and confirm_tier != CANDLE_CONFIRM_NONE,
    }


def detect_bullish_patterns(frame: pd.DataFrame, allowed_patterns: Iterable[str] | None = None) -> set[str]:
    """Bullish candle patterns on the latest bars. Same semantics as the
    ``matched_bullish_candles`` field of ``detect_candle_context``.

    Slices to ``CANDLE_CONTEXT_BARS``, not to 3. TA-Lib's pattern functions
    build average-body and trend state from preceding bars and return 0 for
    anything inside their warmup, so a 3-bar input starves nearly all of
    them: over 600 random tapes the 3-bar slice fired on 88 where the
    30-bar slice fired on 441. Public here for plugin authors (an opt-in
    extension point with no in-package caller), who would otherwise
    inherit that silent shortfall.
    """
    allowed = _normalize_allowed_patterns(allowed_patterns, bullish=True)
    if not allowed:
        return set()
    frame_key = _ohlc_frame_key(frame, lookback=CANDLE_CONTEXT_BARS)
    return set(_detect_side_patterns_cached(frame_key, allowed, True))


def detect_bearish_patterns(frame: pd.DataFrame, allowed_patterns: Iterable[str] | None = None) -> set[str]:
    """Bearish candle patterns on the latest bars. Mirror of
    ``detect_bullish_patterns``; see there for why the slice is
    ``CANDLE_CONTEXT_BARS`` rather than 3."""
    allowed = _normalize_allowed_patterns(allowed_patterns, bullish=False)
    if not allowed:
        return set()
    frame_key = _ohlc_frame_key(frame, lookback=CANDLE_CONTEXT_BARS)
    return set(_detect_side_patterns_cached(frame_key, allowed, False))


