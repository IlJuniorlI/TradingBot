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
    # Strength-related diagnostics computed during detection / build:
    anchor_index: int = 0      # bar index where the OB candle was identified
    age_bars: int = 0          # bars from anchor_index → end of frame
    thrust_atr: float = 0.0    # displacement of the BoS move in ATR units
    strength_score: float = 0.0  # composite size × thrust × age × validity


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


def _ob_strength_score(ob: OrderBlock, atr: float) -> float:
    """Composite strength score for ranking. Higher = stronger.

    Four factors, each independently floored / capped so no single
    extreme value dominates:

    - **size** (× ATR): how large the OB candle was. A 1-ATR candle
      scores 2.0 (max); below that scales linearly.
    - **thrust** (× ATR): how impulsive the break-of-structure move
      was. Strong displacement = real institutional intent. Cap at
      2.0 around a 1.5×ATR thrust.
    - **age** (bars): older OBs that haven't been invalidated have
      "earned" their level. Cap at 1.5 around 30 bars old.
    - **validity** (1 - filled_pct): heavily penalize OBs that are
      mostly filled.

    The sort uses ``-strength_score`` so OBs from genuine large moves
    (high size + high thrust) outrank fresh small OBs (low size + low
    thrust + low age) even when the small ones are closer to current
    price. Resolves the proximity-bias bug where weak close-to-price
    OBs displaced strong distant ones in the top-K.
    """
    size_atr = ob.size / max(atr, 1e-9)
    size_score = min(2.0, size_atr / 0.5)            # 1.0 at 0.5×ATR, capped at 2.0
    thrust_score = min(2.0, ob.thrust_atr / 0.75) if ob.thrust_atr > 0 else 0.5
    age_score = min(1.5, max(0.5, ob.age_bars / 20.0))
    validity = max(0.1, 1.0 - ob.filled_pct)
    return float(size_score * thrust_score * age_score * validity)


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


def _earlier(ts_a: str | None, ts_b: str | None) -> str | None:
    """Pick the chronologically earlier of two ISO timestamps; ``None`` is
    treated as missing. Used to keep first_seen monotonically oldest under
    merging that's sorted by price."""
    if ts_a is None or ts_a == "":
        return ts_b
    if ts_b is None or ts_b == "":
        return ts_a
    return ts_a if ts_a <= ts_b else ts_b


def _later(ts_a: str | None, ts_b: str | None) -> str | None:
    if ts_a is None or ts_a == "":
        return ts_b
    if ts_b is None or ts_b == "":
        return ts_a
    return ts_a if ts_a >= ts_b else ts_b


def _merge_order_blocks(obs: list[OrderBlock], *, tolerance: float) -> list[OrderBlock]:
    """Merge OBs whose zones overlap within tolerance.

    Sort is by price (``lower``), so the merged record's first/last_seen
    must be reconciled by timestamp comparison rather than sort order —
    the otherwise-natural ``last.first_seen`` would reflect price ordering,
    not chronology, and produce misleading metadata.
    """
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
            # Preserve strength-relevant fields across merges:
            # - anchor_index: take the EARLIER (smaller) index so age_bars
            #   reflects the OLDEST contributing OB — older + still valid =
            #   stronger signal.
            # - thrust_atr: take the MAX of contributors so the merged OB
            #   inherits the strongest BoS displacement that produced it.
            # Without these, merged OBs lose their strength metadata and
            # always score 0 thrust + age, defeating the strength sort.
            merged[-1] = OrderBlock(
                direction=last.direction,
                lower=merged_lower,
                upper=merged_upper,
                midpoint=(merged_lower + merged_upper) / 2.0,
                size=merged_size,
                first_seen=_earlier(last.first_seen, ob.first_seen),
                last_seen=_later(last.last_seen, ob.last_seen),
                filled_pct=max(last.filled_pct, ob.filled_pct),
                source=last.source,
                anchor_index=min(int(last.anchor_index), int(ob.anchor_index)),
                thrust_atr=max(float(last.thrust_atr), float(ob.thrust_atr)),
            )
        else:
            merged.append(ob)
    return merged


def _detect_order_blocks_loose(
    frame: pd.DataFrame,
    *,
    new_high_lookback: int,
    min_size: float,
    min_thrust: float,
    atr: float,
    eps: float,
    max_distance_back: int = 5,
) -> tuple[list[OrderBlock], list[OrderBlock]]:
    """Find last opposite-color candle before each "new local high/low" bar.

    ``min_thrust`` filters out micro-breakouts: the move from the OB
    candle's close to the BoS bar's close must be at least ``min_thrust``
    in price units (typically 0.75 × ATR). Without this, every minor
    8-bar high triggers OB hunting and small noise OBs displace
    legitimate ones in the post-cap top-K.
    """
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
                        # Tiny doji-ish bearish bar — keep walking back for a
                        # larger meaningful OB instead of giving up.
                        continue
                    # Thrust filter: the BoS bar must have moved meaningfully
                    # away from this OB candle's close. Suppresses 1-cent
                    # micro-breakouts that would otherwise produce a flood of
                    # weak OBs.
                    thrust_distance = float(close_arr[idx] - close_arr[k])
                    if thrust_distance < min_thrust:
                        continue
                    # Compute filled_pct from CLOSES after k — wicks that
                    # pierce the OB but close back inside are tolerated.
                    after = close_arr[k + 1:]
                    min_close_after = float(after.min()) if after.size else upper
                    filled_pct = _filled_pct_for_bullish(lower, upper, min_close_after)
                    if filled_pct >= 1.0 - 1e-9:
                        # This candidate is invalidated. Keep walking for an
                        # older still-valid OB.
                        continue
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
                            anchor_index=int(k),
                            thrust_atr=float(thrust_distance / max(atr, 1e-9)),
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
                        continue
                    # Mirror of bullish thrust filter — BoS must move down
                    # meaningfully from the OB candle's close.
                    thrust_distance = float(close_arr[k] - close_arr[idx])
                    if thrust_distance < min_thrust:
                        continue
                    after = close_arr[k + 1:]
                    max_close_after = float(after.max()) if after.size else lower
                    filled_pct = _filled_pct_for_bearish(lower, upper, max_close_after)
                    if filled_pct >= 1.0 - 1e-9:
                        continue
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
                            anchor_index=int(k),
                            thrust_atr=float(thrust_distance / max(atr, 1e-9)),
                        )
                    )
                    break
    return bullish_raw, bearish_raw


def _detect_order_blocks_strict(
    frame: pd.DataFrame,
    *,
    pivot_span: int,
    min_size: float,
    min_thrust: float,
    atr: float,
    max_distance_back: int = 8,
) -> tuple[list[OrderBlock], list[OrderBlock]]:
    """Strict ICT/SMC: find swing highs/lows via pivot detector, then locate the
    last opposite-color bar before each break-of-structure event.

    ``min_thrust`` filters out weak BoS moves (close-just-above-swing
    by an inch). For a real OB we want the BoS bar to have moved a
    meaningful distance from the OB candle's close — typically
    0.75 × ATR.
    """
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
                    continue
                # Thrust filter — see _detect_order_blocks_loose for context.
                thrust_distance = float(close_arr[bos_idx] - close_arr[k])
                if thrust_distance < min_thrust:
                    continue
                after = close_arr[k + 1:]
                min_close_after = float(after.min()) if after.size else upper
                filled_pct = _filled_pct_for_bullish(lower, upper, min_close_after)
                if filled_pct >= 1.0 - 1e-9:
                    continue
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
                        anchor_index=int(k),
                        thrust_atr=float(thrust_distance / max(atr, 1e-9)),
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
                    continue
                # Mirror of bullish thrust filter.
                thrust_distance = float(close_arr[k] - close_arr[bos_idx])
                if thrust_distance < min_thrust:
                    continue
                after = close_arr[k + 1:]
                max_close_after = float(after.max()) if after.size else lower
                filled_pct = _filled_pct_for_bearish(lower, upper, max_close_after)
                if filled_pct >= 1.0 - 1e-9:
                    continue
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
                        anchor_index=int(k),
                        thrust_atr=float(thrust_distance / max(atr, 1e-9)),
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
    min_thrust_atr_mult: float = 0.75,
    pivot_span: int = 2,
    new_high_lookback: int = 8,
) -> OrderBlockContext:
    """Detect, merge, and rank order blocks for a given frame.

    Pipeline:

    1. Detect raw OBs (loose or strict mode), each filtered by:
       - ``min_block_atr_mult`` / ``min_block_pct``: minimum OB candle SIZE
       - ``min_thrust_atr_mult``: minimum DISPLACEMENT of the BoS move
       Both filters together reject the noise OBs that previously
       displaced strong OBs in the top-K.
    2. Merge overlapping OBs (within ``min_size × 0.25`` tolerance).
    3. Compute ``strength_score`` for each merged OB:
       size × thrust × age × validity. See ``_ob_strength_score``.
    4. Sort by strength descending — strongest survives capping. Ties
       broken by closer-to-price.
    5. Cap to ``max_per_side`` keeping the strongest.
    6. ``nearest_*_ob`` is computed independently as the closest
       survivor by raw price distance, so consumers wanting "what's
       the immediate level" still get the right answer rather than
       "what's the strongest level (which might be far away)".
    """
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
    min_thrust = max(float(atr) * float(min_thrust_atr_mult), 1e-8)
    eps = max(min_size * 0.05, ref_close * 1e-6, 1e-8)
    if mode_norm == "strict":
        bullish_raw, bearish_raw = _detect_order_blocks_strict(
            base,
            pivot_span=max(1, int(pivot_span)),
            min_size=min_size,
            min_thrust=min_thrust,
            atr=atr,
        )
    else:
        bullish_raw, bearish_raw = _detect_order_blocks_loose(
            base,
            new_high_lookback=max(2, int(new_high_lookback)),
            min_size=min_size,
            min_thrust=min_thrust,
            atr=atr,
            eps=eps,
        )
    merge_tol = max(min_size * 0.25, ref_close * 0.00025, 1e-8)
    bullish = _merge_order_blocks(bullish_raw, tolerance=merge_tol)
    bearish = _merge_order_blocks(bearish_raw, tolerance=merge_tol)
    # Compute age + strength for every merged OB. age_bars is
    # bars-since-anchor-to-end-of-frame; older still-valid OBs score
    # higher in strength (proven respect over time).
    n = len(base)
    for ob in bullish + bearish:
        ob.age_bars = max(0, n - 1 - int(ob.anchor_index))
        ob.strength_score = _ob_strength_score(ob, atr)
    # Strength-first sort. Closer-to-price as deterministic tiebreaker
    # so two equally-strong OBs render in distance order.
    bullish.sort(key=lambda ob: (-ob.strength_score, _ob_distance("bullish", ob, ref_close)))
    bearish.sort(key=lambda ob: (-ob.strength_score, _ob_distance("bearish", ob, ref_close)))
    cap = max(0, int(max_per_side or 0))
    bullish = bullish[:cap] if cap > 0 else []
    bearish = bearish[:cap] if cap > 0 else []
    # nearest_*_ob picks the closest-to-current-price OB from the
    # post-cap survivor list. This preserves the "what's the immediate
    # defense level" semantic for callers like
    # `BaseStrategy._continuation_ob_retest_plan` that want the next
    # OB the price would interact with — even if a stronger OB sits
    # further away in the list. Independent of the strength sort.
    nearest_bullish = min(bullish, key=lambda ob: _ob_distance("bullish", ob, ref_close)) if bullish else None
    nearest_bearish = min(bearish, key=lambda ob: _ob_distance("bearish", ob, ref_close)) if bearish else None
    return OrderBlockContext(
        timeframe_minutes=max(1, int(timeframe_minutes)),
        current_price=ref_close,
        bullish_obs=bullish,
        bearish_obs=bearish,
        nearest_bullish_ob=nearest_bullish,
        nearest_bearish_ob=nearest_bearish,
        mode=mode_norm,
    )
