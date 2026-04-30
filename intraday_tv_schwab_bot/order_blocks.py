# SPDX-License-Identifier: MIT
"""Order block detection for retest-based entries.

An order block (OB) is the last opposite-direction candle before a
strong move that breaks structure. For a bullish OB, this is the last
bearish candle before a sequence that punches a new local high. The OB
zone is that candle's high-to-low range.

Two detection modes:

- **loose**: last bearish bar before any 1m close that exceeds the
  prior N-bar high (and mirror for bearish). Cheap, noisy on
  choppy tape but catches every "down-bar then up-thrust" pattern.
- **strict**: classic ICT/SMC. Find swing highs (using the same
  pivot detector as `support_resistance`); for each swing-high
  break-of-structure (BoS = a later bar's close exceeds the swing
  high), walk back to find the last bearish candle before the BoS.
  Mirror for bearish swings.

Both modes emit `OrderBlock` records that are state-comparable to
`HTFFairValueGap` (same lower/upper/midpoint/size/direction/
filled_pct fields), so the existing `_fvg_gap_state` lifecycle
machinery in `BaseStrategy` works on order blocks unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging

import pandas as pd

from .support_resistance import _pivot_points
from .utils import ensure_ohlcv_frame, ensure_standard_indicator_frame

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class OrderBlock:
    direction: str
    lower: float
    upper: float
    midpoint: float
    size: float
    first_seen: str | None = None
    last_seen: str | None = None
    filled_pct: float = 0.0
    source: str = "ob_loose"  # or "ob_strict"


@dataclass(slots=True)
class OrderBlockContext:
    timeframe_minutes: int
    current_price: float
    bullish_obs: list[OrderBlock] = field(default_factory=list)
    bearish_obs: list[OrderBlock] = field(default_factory=list)
    nearest_bullish_ob: OrderBlock | None = None
    nearest_bearish_ob: OrderBlock | None = None
    mode: str = "loose"


def empty_order_block_context(
    current_price: float = 0.0,
    *,
    timeframe_minutes: int = 1,
    mode: str = "loose",
) -> OrderBlockContext:
    return OrderBlockContext(
        timeframe_minutes=int(timeframe_minutes),
        current_price=float(current_price or 0.0),
        mode=str(mode or "loose").strip().lower() or "loose",
    )


def _ob_distance(direction: str, ob: OrderBlock, ref_close: float) -> float:
    """Closest edge of the OB zone to current price. Used for sort+dedup."""
    if direction == "bullish":
        if ref_close < ob.lower:
            return float(ob.lower - ref_close)
        if ref_close > ob.upper:
            return float(ref_close - ob.upper)
        return 0.0
    # bearish — mirror
    if ref_close > ob.upper:
        return float(ref_close - ob.upper)
    if ref_close < ob.lower:
        return float(ob.lower - ref_close)
    return 0.0


def _filled_pct_for_bullish(ob_lower: float, ob_upper: float, min_close_after: float) -> float:
    """Fraction of bullish OB zone that has been retraced by *closes* — wicks
    that pierce below ``ob_lower`` but close back inside are tolerated. Once
    a bar closes below ``ob_lower``, the OB is invalidated (filled_pct=1.0)."""
    size = max(ob_upper - ob_lower, 1e-9)
    fill_top = min(ob_upper, max(ob_lower, min_close_after))
    return max(0.0, min(1.0, (ob_upper - fill_top) / size))


def _filled_pct_for_bearish(ob_lower: float, ob_upper: float, max_close_after: float) -> float:
    """Mirror of bullish version — invalidation requires a close above the
    OB upper, not just a wick."""
    size = max(ob_upper - ob_lower, 1e-9)
    fill_top = min(ob_upper, max(ob_lower, max_close_after))
    return max(0.0, min(1.0, (fill_top - ob_lower) / size))


def _merge_order_blocks(obs: list[OrderBlock], *, tolerance: float) -> list[OrderBlock]:
    """Merge OBs whose zones overlap within tolerance, keeping the wider one and the
    older first_seen / newer last_seen."""
    if not obs:
        return []
    obs_sorted = sorted(obs, key=lambda o: o.lower)
    merged: list[OrderBlock] = []
    for ob in obs_sorted:
        if not merged:
            merged.append(ob)
            continue
        last = merged[-1]
        if last.direction != ob.direction:
            merged.append(ob)
            continue
        # Overlap check: last.upper >= ob.lower - tolerance
        if last.upper >= ob.lower - tolerance:
            merged_lower = min(last.lower, ob.lower)
            merged_upper = max(last.upper, ob.upper)
            merged_size = merged_upper - merged_lower
            merged[-1] = OrderBlock(
                direction=last.direction,
                lower=merged_lower,
                upper=merged_upper,
                midpoint=(merged_lower + merged_upper) / 2.0,
                size=merged_size,
                first_seen=last.first_seen if last.first_seen else ob.first_seen,
                last_seen=ob.last_seen if ob.last_seen else last.last_seen,
                filled_pct=max(last.filled_pct, ob.filled_pct),
                source=last.source,
            )
        else:
            merged.append(ob)
    return merged


def _detect_order_blocks_loose(
    frame: pd.DataFrame,
    *,
    new_high_lookback: int,
    min_size: float,
    eps: float,
    max_distance_back: int = 5,
) -> tuple[list[OrderBlock], list[OrderBlock]]:
    """Find last opposite-color candle before each "new local high/low" bar."""
    bullish_raw: list[OrderBlock] = []
    bearish_raw: list[OrderBlock] = []
    n = len(frame)
    if n < (new_high_lookback + 2):
        return bullish_raw, bearish_raw
    high_arr = frame["high"].to_numpy(dtype=float, copy=False)
    low_arr = frame["low"].to_numpy(dtype=float, copy=False)
    open_arr = frame["open"].to_numpy(dtype=float, copy=False)
    close_arr = frame["close"].to_numpy(dtype=float, copy=False)
    index_values = frame.index
    last_index_label = index_values[-1] if n > 0 else None
    last_seen_str = (
        last_index_label.isoformat() if hasattr(last_index_label, "isoformat") else str(last_index_label)
        if last_index_label is not None
        else ""
    )
    for idx in range(new_high_lookback, n):
        current_close = float(close_arr[idx])
        # Bullish OB: this bar prints a new local-high close vs prior N closes
        prior_close_max = max(close_arr[max(0, idx - new_high_lookback):idx].tolist() or [current_close])
        if current_close > prior_close_max + eps:
            # Walk back up to max_distance_back bars to find last bearish candle
            for back in range(1, min(idx + 1, max_distance_back + 1)):
                k = idx - back
                if k < 0:
                    break
                if close_arr[k] < open_arr[k]:  # bearish
                    lower = float(low_arr[k])
                    upper = float(high_arr[k])
                    size = upper - lower
                    if size < min_size:
                        break
                    # Compute filled_pct from CLOSES after k — wicks that
                    # pierce the OB but close back inside are tolerated.
                    after = close_arr[k + 1:]
                    min_close_after = float(after.min()) if after.size else upper
                    filled_pct = _filled_pct_for_bullish(lower, upper, min_close_after)
                    if filled_pct >= 1.0 - 1e-9:
                        # OB has been completely filled — invalidated, skip
                        break
                    anchor_ts = index_values[k]
                    bullish_raw.append(
                        OrderBlock(
                            direction="bullish",
                            lower=lower,
                            upper=upper,
                            midpoint=(lower + upper) / 2.0,
                            size=size,
                            first_seen=anchor_ts.isoformat() if hasattr(anchor_ts, "isoformat") else str(anchor_ts),
                            last_seen=last_seen_str,
                            filled_pct=filled_pct,
                            source="ob_loose",
                        )
                    )
                    break
        # Bearish OB: this bar prints a new local-low close
        prior_close_min = min(close_arr[max(0, idx - new_high_lookback):idx].tolist() or [current_close])
        if current_close < prior_close_min - eps:
            for back in range(1, min(idx + 1, max_distance_back + 1)):
                k = idx - back
                if k < 0:
                    break
                if close_arr[k] > open_arr[k]:  # bullish
                    lower = float(low_arr[k])
                    upper = float(high_arr[k])
                    size = upper - lower
                    if size < min_size:
                        break
                    after = close_arr[k + 1:]
                    max_close_after = float(after.max()) if after.size else lower
                    filled_pct = _filled_pct_for_bearish(lower, upper, max_close_after)
                    if filled_pct >= 1.0 - 1e-9:
                        break
                    anchor_ts = index_values[k]
                    bearish_raw.append(
                        OrderBlock(
                            direction="bearish",
                            lower=lower,
                            upper=upper,
                            midpoint=(lower + upper) / 2.0,
                            size=size,
                            first_seen=anchor_ts.isoformat() if hasattr(anchor_ts, "isoformat") else str(anchor_ts),
                            last_seen=last_seen_str,
                            filled_pct=filled_pct,
                            source="ob_loose",
                        )
                    )
                    break
    return bullish_raw, bearish_raw


def _detect_order_blocks_strict(
    frame: pd.DataFrame,
    *,
    pivot_span: int,
    min_size: float,
    max_distance_back: int = 8,
) -> tuple[list[OrderBlock], list[OrderBlock]]:
    """Strict ICT/SMC: find swing highs/lows via pivot detector, then locate the
    last opposite-color bar before each break-of-structure event."""
    bullish_raw: list[OrderBlock] = []
    bearish_raw: list[OrderBlock] = []
    n = len(frame)
    if n < (pivot_span * 2 + 4):
        return bullish_raw, bearish_raw
    swing_highs, swing_lows = _pivot_points(frame, pivot_span)
    high_arr = frame["high"].to_numpy(dtype=float, copy=False)
    low_arr = frame["low"].to_numpy(dtype=float, copy=False)
    open_arr = frame["open"].to_numpy(dtype=float, copy=False)
    close_arr = frame["close"].to_numpy(dtype=float, copy=False)
    index_values = frame.index
    last_index_label = index_values[-1] if n > 0 else None
    last_seen_str = (
        last_index_label.isoformat() if hasattr(last_index_label, "isoformat") else str(last_index_label)
        if last_index_label is not None
        else ""
    )
    # Bullish OBs: for each swing high, find the first later bar whose close
    # exceeds the swing high (BoS), then walk back from BoS to find last
    # bearish candle.
    for swing_idx, _ts, swing_price in swing_highs:
        bos_idx = None
        for j in range(swing_idx + pivot_span + 1, n):
            if close_arr[j] > swing_price:
                bos_idx = j
                break
        if bos_idx is None:
            continue
        for back in range(1, max_distance_back + 1):
            k = bos_idx - back
            if k <= swing_idx:
                break
            if close_arr[k] < open_arr[k]:
                lower = float(low_arr[k])
                upper = float(high_arr[k])
                size = upper - lower
                if size < min_size:
                    break
                after = close_arr[k + 1:]
                min_close_after = float(after.min()) if after.size else upper
                filled_pct = _filled_pct_for_bullish(lower, upper, min_close_after)
                if filled_pct >= 1.0 - 1e-9:
                    break
                anchor_ts = index_values[k]
                bullish_raw.append(
                    OrderBlock(
                        direction="bullish",
                        lower=lower,
                        upper=upper,
                        midpoint=(lower + upper) / 2.0,
                        size=size,
                        first_seen=anchor_ts.isoformat() if hasattr(anchor_ts, "isoformat") else str(anchor_ts),
                        last_seen=last_seen_str,
                        filled_pct=filled_pct,
                        source="ob_strict",
                    )
                )
                break
    # Bearish OBs: mirror with swing lows
    for swing_idx, _ts, swing_price in swing_lows:
        bos_idx = None
        for j in range(swing_idx + pivot_span + 1, n):
            if close_arr[j] < swing_price:
                bos_idx = j
                break
        if bos_idx is None:
            continue
        for back in range(1, max_distance_back + 1):
            k = bos_idx - back
            if k <= swing_idx:
                break
            if close_arr[k] > open_arr[k]:
                lower = float(low_arr[k])
                upper = float(high_arr[k])
                size = upper - lower
                if size < min_size:
                    break
                after = close_arr[k + 1:]
                max_close_after = float(after.max()) if after.size else lower
                filled_pct = _filled_pct_for_bearish(lower, upper, max_close_after)
                if filled_pct >= 1.0 - 1e-9:
                    break
                anchor_ts = index_values[k]
                bearish_raw.append(
                    OrderBlock(
                        direction="bearish",
                        lower=lower,
                        upper=upper,
                        midpoint=(lower + upper) / 2.0,
                        size=size,
                        first_seen=anchor_ts.isoformat() if hasattr(anchor_ts, "isoformat") else str(anchor_ts),
                        last_seen=last_seen_str,
                        filled_pct=filled_pct,
                        source="ob_strict",
                    )
                )
                break
    return bullish_raw, bearish_raw


def build_order_block_context(
    frame: pd.DataFrame | None,
    *,
    timeframe_minutes: int = 1,
    current_price: float | None = None,
    mode: str = "loose",
    max_per_side: int = 4,
    min_block_atr_mult: float = 0.05,
    min_block_pct: float = 0.0005,
    pivot_span: int = 2,
    new_high_lookback: int = 8,
) -> OrderBlockContext:
    mode_norm = (str(mode or "loose").strip().lower() or "loose")
    if mode_norm not in {"loose", "strict"}:
        mode_norm = "loose"
    if frame is None or frame.empty:
        return empty_order_block_context(float(current_price or 0.0), timeframe_minutes=timeframe_minutes, mode=mode_norm)
    base = ensure_standard_indicator_frame(ensure_ohlcv_frame(frame.copy()))
    if base.empty or len(base) < 5:
        return empty_order_block_context(float(current_price or 0.0), timeframe_minutes=timeframe_minutes, mode=mode_norm)
    if current_price is None or current_price <= 0:
        try:
            current_price = float(base.iloc[-1]["close"])
        except Exception:
            current_price = 0.0
    ref_close = float(current_price or 0.0)
    atr_fallback = max(ref_close * 0.0015, 0.01)
    if "atr14" in base.columns:
        atr_clean = base["atr14"].dropna()
        atr = float(atr_clean.iloc[-1]) if not atr_clean.empty else atr_fallback
    else:
        atr = atr_fallback
    min_size = max(float(atr) * float(min_block_atr_mult), float(ref_close) * float(min_block_pct), 1e-8)
    eps = max(min_size * 0.05, ref_close * 1e-6, 1e-8)
    if mode_norm == "strict":
        bullish_raw, bearish_raw = _detect_order_blocks_strict(
            base,
            pivot_span=max(1, int(pivot_span)),
            min_size=min_size,
        )
    else:
        bullish_raw, bearish_raw = _detect_order_blocks_loose(
            base,
            new_high_lookback=max(2, int(new_high_lookback)),
            min_size=min_size,
            eps=eps,
        )
    merge_tol = max(min_size * 0.25, ref_close * 0.00025, 1e-8)
    bullish = _merge_order_blocks(bullish_raw, tolerance=merge_tol)
    bearish = _merge_order_blocks(bearish_raw, tolerance=merge_tol)
    bullish.sort(key=lambda ob: (_ob_distance("bullish", ob, ref_close), -float(ob.upper)))
    bearish.sort(key=lambda ob: (_ob_distance("bearish", ob, ref_close), float(ob.lower)))
    cap = max(0, int(max_per_side or 0))
    bullish = bullish[:cap] if cap > 0 else []
    bearish = bearish[:cap] if cap > 0 else []
    return OrderBlockContext(
        timeframe_minutes=max(1, int(timeframe_minutes)),
        current_price=ref_close,
        bullish_obs=bullish,
        bearish_obs=bearish,
        nearest_bullish_ob=bullish[0] if bullish else None,
        nearest_bearish_ob=bearish[0] if bearish else None,
        mode=mode_norm,
    )
