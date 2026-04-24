# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

try:
    import talib  # type: ignore
except Exception:  # pragma: no cover - optional until pattern detection is used
    talib = None


# Minimum bars to feed TA-Lib for candle pattern detection. TA-Lib
# candle functions build internal state (average body size, trend
# context) from preceding bars; with only 3 rows of input it can't
# initialize and returns zeros even for textbook patterns. 30 bars
# is enough for every registered pattern's lookback (max ~14 for
# CDLEVENINGDOJISTAR et al) plus warmup. Exposed for callers so the
# cache-key slice and detection slice stay consistent.
CANDLE_CONTEXT_BARS: int = 30


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
CUSTOM_2C_PATTERNS: tuple[str, ...] = ("TWEEZER_BOTTOM", "TWEEZER_TOP")

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


def _safe_float_token(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except Exception:
        return None


def _ohlc_frame_key(frame: pd.DataFrame | None, lookback: int = CANDLE_CONTEXT_BARS) -> tuple[tuple[float | None, float | None, float | None, float | None], ...]:
    if frame is None or frame.empty:
        return tuple()
    subset = frame[["open", "high", "low", "close"]].tail(max(int(lookback), 1)).copy()
    if subset.empty:
        return tuple()
    for col in ("open", "high", "low", "close"):
        subset[col] = pd.to_numeric(subset[col], errors="coerce")
    subset = subset.dropna(subset=["open", "high", "low", "close"])
    if subset.empty:
        return tuple()
    out: list[tuple[float | None, float | None, float | None, float | None]] = []
    for row in subset.itertuples(index=False, name=None):
        open_, high_, low_, close_ = row
        out.append(
            (
                _safe_float_token(open_),
                _safe_float_token(high_),
                _safe_float_token(low_),
                _safe_float_token(close_),
            )
        )
    return tuple(out)


@lru_cache(maxsize=4096)
def _ohlc_arrays_from_key(frame_key: tuple[tuple[float | None, float | None, float | None, float | None], ...]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
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
    frame_key: tuple[tuple[float | None, float | None, float | None, float | None], ...],
    func_name: str,
) -> int:
    if not frame_key:
        return 0
    if talib is None:
        raise RuntimeError("TA-Lib is required for candlestick pattern detection but is not installed")
    func = getattr(talib, func_name)
    opens, highs, lows, closes = _ohlc_arrays_from_key(frame_key)
    values = func(opens, highs, lows, closes)
    if len(values) == 0:
        return 0
    # Scan the tail of the output (most recent first) and return the first
    # non-zero signal. TA-Lib emits the signal on the completion bar of a
    # pattern — e.g. a 2-bar engulfing ending at bar N-1 lands at values[-2],
    # not values[-1]. Reading only values[-1] would discard any pattern that
    # completed on an earlier bar of the tail(3) window, which made patterns
    # "disappear" from the report as soon as a new bar arrived even though
    # they're still visible on the chart (INTC 2026-04-24 10:08 bullish
    # engulfing was lost at the 10:09 cycle). Signed value preserved:
    # +N = bullish, -N = bearish (TA-Lib's ±100 and occasional ±200).
    for idx in (-1, -2, -3):
        if len(values) + idx < 0:
            break
        try:
            signal = int(values[idx])
        except Exception:
            continue
        if signal != 0:
            return signal
    return 0


def _range_from_row(row: tuple[float | None, float | None, float | None, float | None]) -> float:
    _open, high, low, _close = row
    if high is None or low is None:
        return 0.0
    return max(0.0, float(high) - float(low))


def _bull_from_row(row: tuple[float | None, float | None, float | None, float | None]) -> bool:
    open_, _high, _low, close = row
    return open_ is not None and close is not None and float(close) > float(open_)


def _bear_from_row(row: tuple[float | None, float | None, float | None, float | None]) -> bool:
    open_, _high, _low, close = row
    return open_ is not None and close is not None and float(close) < float(open_)


def _near(a: float | None, b: float | None, tolerance: float) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= max(0.0, float(tolerance))


def _tweezer_bottom_from_key(frame_key: tuple[tuple[float | None, float | None, float | None, float | None], ...]) -> bool:
    if len(frame_key) < 2:
        return False
    prev, cur = frame_key[-2], frame_key[-1]
    tolerance = max(_range_from_row(prev), _range_from_row(cur)) * 0.05
    return bool(_bear_from_row(prev) and _bull_from_row(cur) and _near(prev[2], cur[2], tolerance))


def _tweezer_top_from_key(frame_key: tuple[tuple[float | None, float | None, float | None, float | None], ...]) -> bool:
    if len(frame_key) < 2:
        return False
    prev, cur = frame_key[-2], frame_key[-1]
    tolerance = max(_range_from_row(prev), _range_from_row(cur)) * 0.05
    return bool(_bull_from_row(prev) and _bear_from_row(cur) and _near(prev[1], cur[1], tolerance))


def _evaluate_side_pattern(
    frame_key: tuple[tuple[float | None, float | None, float | None, float | None], ...],
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
    return value > 0 if bullish else value < 0


@lru_cache(maxsize=4096)
def _detect_side_patterns_cached(
    frame_key: tuple[tuple[float | None, float | None, float | None, float | None], ...],
    allowed: tuple[str, ...],
    bullish: bool,
) -> tuple[str, ...]:
    if not frame_key or not allowed:
        return tuple()
    matches = [name for name in allowed if _evaluate_side_pattern(frame_key, name, bullish=bullish)]
    return tuple(sorted(matches))


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
    frame_key: tuple[tuple[float | None, float | None, float | None, float | None], ...],
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
    allowed = _normalize_allowed_patterns(allowed_patterns, bullish=True)
    if not allowed:
        return set()
    frame_key = _ohlc_frame_key(frame, lookback=3)
    return set(_detect_side_patterns_cached(frame_key, allowed, True))


def detect_bearish_patterns(frame: pd.DataFrame, allowed_patterns: Iterable[str] | None = None) -> set[str]:
    allowed = _normalize_allowed_patterns(allowed_patterns, bullish=False)
    if not allowed:
        return set()
    frame_key = _ohlc_frame_key(frame, lookback=3)
    return set(_detect_side_patterns_cached(frame_key, allowed, False))


