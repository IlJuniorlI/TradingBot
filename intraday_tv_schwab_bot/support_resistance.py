# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import logging

import numpy as np
import pandas as pd

from .levels_shared import (
    FlipCheck,
    Level,
    cluster_levels,
    cluster_levels_by_tolerance,
    collapse_same_side_levels,
    confirm_by_values,
    confirm_tail,
    detect_broken_levels,
    drop_levels_near_price,
    extend_unique_levels,
    fallback_prior_side_levels,
    frame_extreme_side_levels,
    partition_levels_by_side,
    pending_level,
    pivot_points,
    prior_day_levels as _prior_day_levels,
    prior_week_levels as _prior_week_levels,
    reduce_pivots,
    safe_reference_price_for_fallback as _safe_reference_price_for_fallback,
    same_side_min_gap_threshold as _same_side_min_gap_threshold,
    side_tolerance as _side_tolerance,
    split_references_by_flip,
)
from .sessions import latest_session_date
from .bars import ensure_ohlcv_frame, resample_bars, resolve_current_price
from .indicators import atr_with_floor, ensure_standard_indicator_frame
from . import sessions


LOG = logging.getLogger(__name__)


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
    # The rule in ``_resolve_structure_bias`` that set ``bias``: "breakout"
    # (the close through the reference high or low), "tight_range" (neutral
    # in a tight EQH+EQL range), "midpoint" (the close's side of the
    # reference pair's midpoint), "recent_bos" (the newer live BoS) or
    # "pivots" (the last high / low labels, ``pivot_bias``). "none" when no
    # structure was analysed. Named in a refusal's reason, since the labels
    # beside it can read the other way (an HH / HL pair below its midpoint
    # reads bearish).
    bias_source: str = "none"
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
    # When each event happened, on the analysis frame's own index (2026-09-24).
    # ``pivot_times`` holds every reduced pivot's bar timestamp, oldest first;
    # the event stamps are the bar the break crossed on (``*_age_bars`` bars
    # back), None when there is no such cross. They are bar LABELS -- the
    # bar's start, like the frame -- so a consumer comparing one with a
    # wall-clock moment must use the bar's close
    # (bars.bar_closed_after). The exit side counts pivots and
    # judges events against the position's entry time with these. Before, it
    # compared ``pivot_count`` -- a count over a ROLLING window, so it could
    # fall as old pivots scrolled off -- against a count stamped at entry that
    # most strategies never stamped, and it fired a CHoCH that had happened
    # before the position was opened.
    pivot_times: tuple[pd.Timestamp, ...] = ()
    bos_up_ts: pd.Timestamp | None = None
    bos_down_ts: pd.Timestamp | None = None
    choch_up_ts: pd.Timestamp | None = None
    choch_down_ts: pd.Timestamp | None = None


@dataclass(slots=True)
class SupportResistanceContext:
    current_price: float
    timeframe_minutes: int = 15
    supports: list[Level] = field(default_factory=list)
    resistances: list[Level] = field(default_factory=list)
    nearest_support: Level | None = None
    nearest_resistance: Level | None = None
    broken_resistance: Level | None = None
    broken_support: Level | None = None
    # A support price has crossed BELOW whose loss is not yet confirmed
    # (pending_resistance: a resistance crossed ABOVE, reclaim unconfirmed) --
    # the nearest one, still in its original role. ``supports`` /
    # ``nearest_support`` keep only levels at or below price and
    # ``broken_support`` needs the confirmation, so until 2026-09-23 such a
    # level was in no list at all: from the first print through it until
    # confirmation, clearance checks, stop/target refinement and sr_scalp's
    # zone test read the next level instead, i.e. treated an unconfirmed break
    # as a confirmed one. Price sat on the wrong side of an unconfirmed pivot
    # level in 12.8% of archived RTH samples.
    pending_support: Level | None = None
    pending_resistance: Level | None = None
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
    # Minutes from the last completed 1m bar that traded at the broken level
    # behind each flag (a low at or below broken_resistance; a high at or
    # above broken_support) to the last completed bar. None while the flag is
    # off, without a flip frame, or when no bar in it touched the level.
    # SHADOW-LOGGED ONLY (entry metadata): as an entry gate the flags showed
    # no edge over 26,447 archived RTH checkpoints, apart from a hint in
    # breaks at most 30 minutes old (-0.11 R, 95% CI crossing 0, 321
    # checkpoints) that needs more sessions to confirm or drop (2026-09-23).
    breakout_age_minutes: float | None = None
    breakdown_age_minutes: float | None = None
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
) -> tuple[str, str]:
    """The structure bias and the rule that set it (``MarketStructureContext.
    bias`` / ``bias_source``), in order: a close through a reference, the
    tight-range neutral, the midpoint, the newer live BoS, the pivot labels."""
    above_high = reference_high is not None and close >= float(reference_high) + breakout_buffer
    below_low = reference_low is not None and close <= float(reference_low) - breakout_buffer
    if above_high and below_low:
        # Both hold only on an inverted pair: the reference low two buffers
        # or more above the reference high. A gap leaves one, because a
        # pivot needs neighbours in its own session: after a gap up the
        # first swing low forms above the old reference high before any
        # high confirms (mirror after a gap down). The later of the two
        # breaks is the live one (a bos age of None: the price passed in is
        # through the reference while the frame's last close is not, so it
        # is the newer). Until 2026-09-25 the high side was tested first,
        # so a gap up that broke its first swing low read bullish, the
        # same as its mirror image, a gap down that broke its first swing
        # high. Equal ages (a degenerate pair) decide nothing here.
        up_age = -1 if bos_up_age is None else int(bos_up_age)
        down_age = -1 if bos_down_age is None else int(bos_down_age)
        if up_age != down_age:
            return ("bullish" if up_age < down_age else "bearish"), "breakout"
    elif above_high:
        return "bullish", "breakout"
    elif below_low:
        return "bearish", "breakout"

    # Tight EQH+EQL consolidation suppresses the midpoint / pivot / recent-
    # event bias paths — a 0.3-ATR range produces noise-driven bias flips
    # (single bar can swing bias bearish→bullish). Genuine BoS through the
    # reference high/low above already returned bullish/bearish, so we still
    # catch real breakouts. CHoCH is computed in analyze_market_structure
    # from bos_up/down + pivot_bias, also unaffected.
    if tight_structure_range:
        return "neutral", "tight_range"

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
        return midpoint_bias, "midpoint"
    if recent_event_bias != "neutral":
        return recent_event_bias, "recent_bos"
    return pivot_bias, "pivots"
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
    min_pivot_gap_bars: int = 0,
    last_bar_forming: bool = False,
) -> MarketStructureContext:
    frame = ensure_standard_indicator_frame(frame)
    if frame.empty:
        return empty_market_structure_context(float(current_price or 0.0))
    close = resolve_current_price(frame, current_price)
    atr = atr_with_floor(frame, float(frame["close"].iloc[-1]))
    eq_tol = max(atr * float(eq_atr_mult), close * float(pct_tolerance))
    # ``last_bar_forming``: the last bar is a bucket still trading (a 5m
    # resample of the 1m stream holds 1-4 minutes of its bucket 4 minutes in
    # 5). Such a bar is no pivot's right-hand neighbour (2026-09-25): until
    # then a pivot could be confirmed by the first minutes of a bucket and be
    # gone by its close -- 9 of the 32 that confirmed a top_tier entry's
    # structure were. Only the pivot search skips it. The close, the ATR and
    # the BoS / CHoCH crosses still read the whole frame, so a break on the
    # forming bucket counts at once. Positions stay valid: only the tail is
    # cut.
    highs, lows = pivot_points(frame.iloc[:-1] if last_bar_forming else frame, int(pivot_span), include_idx=True)
    pivots = reduce_pivots(highs, lows, min_gap_bars=int(min_pivot_gap_bars))
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

    bias, bias_source = _resolve_structure_bias(
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

    def _event_ts(age: int | None) -> pd.Timestamp | None:
        return None if age is None else pd.Timestamp(frame.index[len(frame) - 1 - int(age)])

    bos_up_ts = _event_ts(bos_up_age)
    bos_down_ts = _event_ts(bos_down_age)

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
        bias_source=bias_source,
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
        pivot_times=tuple(pd.Timestamp(ts) for _kind, _pos, ts, _price in pivots),
        bos_up_ts=bos_up_ts,
        bos_down_ts=bos_down_ts,
        choch_up_ts=bos_up_ts if choch_up else None,
        choch_down_ts=bos_down_ts if choch_down else None,
    )


def _level_preference(level: Level, current_price: float) -> tuple[float, int, float, float]:
    blended_strength = float(level.score) + (0.20 * max(0.0, float(getattr(level, "source_priority", 1.0) or 1.0) - 1.0))
    return (
        blended_strength,
        int(level.touches),
        float(getattr(level, "source_priority", 1.0) or 1.0),
        -abs(float(level.price) - float(current_price)),
    )


def _merge_level_group(group: list[Level], current_price: float) -> Level:
    # The SR cluster reducer (levels_shared.collapse_same_side_levels): SR
    # merges every level in a cluster (sums touches, sums score, adds a
    # cross-source bonus). htf_levels uses the same tolerance grouping but
    # picks a single representative; the split is deliberate, so each
    # builder keeps its own reducer.
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
    return Level(
        kind=str(getattr(representative, "kind", "support") or "support"),
        price=float(representative.price),
        touches=int(merged_touches),
        score=float(merged_score),
        first_seen=min(first_seen_candidates) if first_seen_candidates else getattr(representative, "first_seen", None),
        last_seen=max(last_seen_candidates) if last_seen_candidates else getattr(representative, "last_seen", None),
        source=str(getattr(representative, "source", "pivot") or "pivot"),
        source_priority=max(float(getattr(level, "source_priority", 1.0) or 1.0) for level in group),
    )



def _rung_holding(rungs: list[Level], candidates: list[Level], level: Level | None, tolerance: float) -> Level | None:
    """The rung of ``rungs`` -- a ladder side collapsed from ``candidates``
    at ``tolerance`` (``collapse_same_side_levels``) -- whose cluster holds
    the candidate at ``level``'s price. None without ``level``, or when no
    candidate sits at its price (a confirmed flip on the far side of price
    is in no rung of this side). The clusters are contiguous price ranges,
    each published at one of its own members' prices, so the cluster's rung
    is the one inside its range."""
    if level is None:
        return None
    for group in cluster_levels_by_tolerance(candidates, tolerance):
        prices = [float(member.price) for member in group]
        if float(level.price) in prices:
            return next((rung for rung in rungs if prices[0] <= float(rung.price) <= prices[-1]), None)
    return None


def _reconcile_flipped_levels(
    supports: list[Level],
    resistances: list[Level],
    *,
    support_candidates: list[Level],
    resistance_candidates: list[Level],
    broken_support: Level | None,
    broken_resistance: Level | None,
    tolerance: float,
) -> tuple[list[Level], list[Level]]:
    """Drop the support rungs within ``tolerance`` of the lost support
    (``broken_support``) and the resistance rungs within it of the reclaimed
    resistance (``broken_resistance``): the same zone, already flipped. A
    drop only. The builder collapses each side once, on its whole candidate
    pool (``support_candidates`` / ``resistance_candidates``), before this
    step and cuts it to ``max_levels_per_side`` after it.

    A flip's own rung survives the other flip's drop: the support rung whose
    cluster holds the reclaimed resistance, the resistance rung whose cluster
    holds the lost support (``_rung_holding``). When the two flips lie within
    ``tolerance`` of each other either side of price, each one's rung lies
    within the tolerance of the other, and until 2026-10-07 the drop took
    both: GOOG 2026-09-29 10:55 at 336.745, supports [336.298, 334.03] became
    [334.03] and resistances [336.84, 338.19] became [338.19], so every gate
    measured to levels about 1.1 ATR away while the flips sat 0.2-0.3 ATR
    from price (3.1% of 6,500 archived checkpoints). The rung kept is the one
    holding the flip, not every rung within the tolerance of it: a plain
    support between the reclaimed resistance and price, in a cluster of its
    own, sits within the tolerance of both flips and is a stale member of the
    lost zone, so it still goes; and a cluster holding the flip but published
    at a stronger member's price is still the flip's rung, so it stays.

    Until 2026-09-27 the builder cut each side before this step, and this
    step collapsed the survivors a second time, even with no broken level. A
    rung keeps its strongest member's price, not the cluster's mean, so two
    rungs could sit within ``tolerance``; the second pass merged them and
    summed their touches and score again. And a rung dropped here left the
    side one short while deeper candidates existed."""

    def drop_near(rungs: list[Level], flip: Level | None, own: Level | None) -> list[Level]:
        if flip is None:
            return list(rungs)
        survivors = drop_levels_near_price(rungs, float(flip.price), tolerance=tolerance)
        return [rung for rung in rungs if rung is own or rung in survivors]

    return (
        drop_near(supports, broken_support, _rung_holding(supports, support_candidates, broken_resistance, tolerance)),
        drop_near(resistances, broken_resistance, _rung_holding(resistances, resistance_candidates, broken_support, tolerance)),
    )



def _completed_1m_bars(flip_frame: pd.DataFrame | None) -> pd.DataFrame:
    """The flip frame's 1m bars that have completed at ``sessions.now_et()``."""
    base = ensure_ohlcv_frame(flip_frame if flip_frame is not None else pd.DataFrame())
    if base.empty:
        return base
    return base[base.index < pd.Timestamp(sessions.now_et()).floor("1min")]


def _minutes_since_level_touch(completed_1m: pd.DataFrame, level: float, *, price_above: bool) -> float | None:
    """Minutes from the last completed 1m bar that traded at ``level`` -- its
    low at or below it when price is now above it, its high at or above it
    when price is below -- to the last completed bar; None when no bar in
    ``completed_1m`` touched it."""
    if completed_1m.empty:
        return None
    touched = (completed_1m["low"] <= level) if price_above else (completed_1m["high"] >= level)
    hits = completed_1m.index[touched.to_numpy(dtype=bool)]
    if len(hits) == 0:
        return None
    return float((completed_1m.index[-1] - hits[-1]) / pd.Timedelta(minutes=1))


def flip_frame_clock_key(flip_frame: pd.DataFrame | None, now: datetime | pd.Timestamp) -> tuple[int, int] | None:
    """What a build reads of the clock through its flip frame
    (``_completed_flip_frames``, ``_completed_1m_bars``): how many of the
    frame's labels lie before the cutoff minute, which fixes the completed
    1m bars, and the cutoff's 5-minute slot, which fixes whether the last
    5m bucket has completed (bucket ends lie on the 5-minute grid). A memo
    keyed on it misses when a bar completes or a 5m slot turns, not every
    minute. None without a flip frame or with an empty one: the build reads
    no clock through either (``_completed_1m_bars`` returns before its
    comparison). ``get_merged`` hands a symbol whose 1m bars have not
    arrived an empty frame on a ``RangeIndex``, which cannot be compared
    with a stamp: until 2026-10-05 its key raised TypeError there, where a
    build gives the context.
    The cutoff is floored on UTC, which equals the ET floor and cannot raise
    on the fall-back hour."""
    if flip_frame is None or flip_frame.empty:
        return None
    moment = pd.Timestamp(now)
    cutoff = moment.floor("1min") if moment.tzinfo is None else moment.tz_convert("UTC").floor("1min")
    return int(np.count_nonzero(flip_frame.index < cutoff)), int(cutoff.value // 300_000_000_000)


def _completed_flip_frames(flip_frame: pd.DataFrame | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    completed_1m = _completed_1m_bars(flip_frame)
    if completed_1m.empty:
        return completed_1m, pd.DataFrame(columns=completed_1m.columns)
    one_min_cutoff = pd.Timestamp(sessions.now_et()).floor("1min")
    completed_5m = resample_bars(completed_1m, "5min")
    if not completed_5m.empty:
        # A 5m bar labelled T holds the 1m bars starting T .. T+4 (see
        # resample_bars), so it is complete once its last constituent, T+4,
        # is: T + 5min <= one_min_cutoff. The bucket still filling when the
        # completed 1m bars run out fails that and is dropped, so flip
        # confirmation never reads a partial 5m bar.
        completed_5m = completed_5m[completed_5m.index + pd.Timedelta(minutes=5) <= one_min_cutoff]
    return completed_1m, completed_5m


def _flip_checker(
    flip_frame: pd.DataFrame | None,
    *,
    confirm_1m_bars: int,
    confirm_5m_bars: int,
    fallback_bar: tuple[float, float] | None,
    eps: float,
) -> FlipCheck:
    """Return ``check(level_price, direction)``: is the level's flip confirmed?

    ``"reclaim"`` needs the last ``confirm_1m_bars`` completed 1m lows (or the
    last ``confirm_5m_bars`` completed 5m lows) above the level; ``"loss"``
    the matching highs below it. Without a flip overlay the ``fallback_bar``
    (high, low) decides alone.

    The completed 1m/5m frames are cut ONCE per build. A build tests every
    reference level, twice, and each test used to re-resample the whole flip
    frame: ~180 ms per build on a 9-session 15m frame with a two-session 1m
    flip frame, before removing the pre-split cluster cap (2026-09-23) added
    references. Cut once, the uncapped build takes ~20 ms.
    """
    completed_1m, completed_5m = _completed_flip_frames(flip_frame)
    overlay_requested = flip_frame is not None and (confirm_1m_bars > 0 or confirm_5m_bars > 0)
    bars_1m = int(confirm_1m_bars or 0)
    bars_5m = int(confirm_5m_bars or 0)
    tol = float(eps)
    # The bars confirm_by_bars would read, taken once per build too: every
    # reference level is tested twice.
    tails = {
        (field_name, frame_key): confirm_tail(frame, field_name, count)
        for field_name in ("low", "high")
        for frame_key, frame, count in (("1m", completed_1m, bars_1m), ("5m", completed_5m, bars_5m))
    }

    def check(level_price: float, direction: str) -> bool:
        field_name, comparator = ("low", "above") if direction == "reclaim" else ("high", "below")
        if (
            confirm_by_values(tails[(field_name, "1m")], comparator, level_price, tol)
            or confirm_by_values(tails[(field_name, "5m")], comparator, level_price, tol)
        ):
            return True
        if overlay_requested or fallback_bar is None:
            return False
        fallback_high, fallback_low = fallback_bar
        if direction == "reclaim":
            return float(fallback_low) > float(level_price) + tol
        return float(fallback_high) < float(level_price) - tol

    return check


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
    if zone_kind not in ('support', 'resistance'):
        return False
    check = _flip_checker(
        flip_frame,
        confirm_1m_bars=confirm_1m_bars,
        confirm_5m_bars=confirm_5m_bars,
        fallback_bar=fallback_bar,
        eps=eps,
    )
    if zone_kind == 'support':
        return check(float(lower), 'loss')
    return check(float(upper), 'reclaim')


def _role_level(role: str, close: float, nearest: Level | None, broken: Level | None) -> Level | None:
    """The nearest level playing ``role`` on its side of ``close``: for
    ``"resistance"`` the nearer of ``nearest`` (nearest_resistance) and
    ``broken`` (broken_support, a lost support now acting as resistance),
    the flip a candidate when it lies at or above ``close`` and below
    ``nearest`` or there is no ``nearest``; for ``"support"`` the mirror,
    nearest_support and broken_resistance at or below ``close``. A flip at
    ``close`` counts, as a ladder level at price does (no room); a flip on
    the wrong side of price (``detect_broken_levels`` keeps one within the
    merge tolerance across it) is no candidate; a tie goes to ``nearest``.
    ``role_level`` reads it off a built context; the builder's ``near_*``
    read it here."""
    if role == "resistance":
        flip_counts = broken is not None and float(broken.price) >= close and (
            nearest is None or float(broken.price) < float(nearest.price))
    elif role == "support":
        flip_counts = broken is not None and float(broken.price) <= close and (
            nearest is None or float(broken.price) > float(nearest.price))
    else:
        raise ValueError(f"role must be 'support' or 'resistance', not {role!r}")
    return broken if flip_counts else nearest


def role_level(sr_ctx: SupportResistanceContext, role: str) -> Level | None:
    """The nearest level playing ``role`` on its side of price in
    ``sr_ctx`` (``_role_level``): the resistance over a LONG is
    ``nearest_resistance`` or a lost support (``broken_support``) between
    price and it, the support under a SHORT ``nearest_support`` or a
    reclaimed resistance (``broken_resistance``) between it and price.

    A ladder rung is a cluster published at its strongest member's price,
    so a confirmed flip can sit between price and the nearest rung as a
    member of that rung's cluster (TSM 2026-10-06 15:17: broken_support
    485.42 inside nearest_resistance's cluster at 485.62). Of 6,500
    archived checkpoints a lost support sat between price and
    nearest_resistance on 18%, a reclaimed resistance between
    nearest_support and price on 12%. Until 2026-10-07 the gates measuring
    the room toward the opposing level read ``nearest_*`` alone: a LONG at
    484.30 read 0.273% of room to 485.62 and passed the 0.25% minimum,
    where the flip left 0.231%. Readers: the S/R clearance veto and the
    proximity score (``shared_entry._htf_clearance``, after a pending
    level), the refinement's target caps, ``near_*``, top_tier's Fix G and
    sr_scalp's target. The stop anchors, the ladders and the published
    ``nearest_*`` with their distances still read the clusters."""
    if role == "resistance":
        return _role_level(role, float(sr_ctx.current_price), sr_ctx.nearest_resistance, sr_ctx.broken_support)
    return _role_level(role, float(sr_ctx.current_price), sr_ctx.nearest_support, sr_ctx.broken_resistance)


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
    # near_* read the nearest level playing each role (``role_level``): a
    # confirmed flip between price and nearest_* is the level price is near
    # (2026-10-07). The distances above stay nearest_*'s.
    support_role = _role_level("support", close, nearest_support, broken_resistance)
    resistance_role = _role_level("resistance", close, nearest_resistance, broken_support)
    near_support = bool(
        support_role is not None
        and atr > 0
        and (close - support_role.price) / atr <= float(proximity_atr_mult)
        and close >= support_role.price - level_buffer
    )
    near_resistance = bool(
        resistance_role is not None
        and atr > 0
        and (resistance_role.price - close) / atr <= float(proximity_atr_mult)
        and close <= resistance_role.price + level_buffer
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
    # near_* is about the nearest level playing each role on its own side of
    # price (``role_level``: nearest_* or a confirmed flip between price and
    # it); the breakdown / breakout flags are about a broken level that price
    # has crossed (a support now above price, a resistance now below it), so
    # one reclaimed resistance just under price can give both the breakout
    # and the near-support terms. Until 2026-09-23 either flag switched the
    # near term off, so on about half of all checkpoints an unrelated old
    # level below price dropped the -0.35 resistance-pressure term (and the
    # mirror).
    if near_support:
        bias += 0.35
    if near_resistance:
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
    as_of: date | None = None,
) -> SupportResistanceContext:
    # ``as_of`` is the session date the prior day/week are measured back from;
    # None means the clock's (``latest_session_date(sessions.now_et())``).
    frame = ensure_standard_indicator_frame(frame)
    if frame.empty:
        return empty_support_resistance_context(float(current_price or 0.0), timeframe_minutes=timeframe_minutes)
    close = resolve_current_price(frame, current_price)
    atr = atr_with_floor(frame, float(frame["close"].iloc[-1]))
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
    highs, lows = pivot_points(frame, int(pivot_span), include_idx=True)
    # cluster_levels expects (ts, price) tuples; strip the leading pos.
    highs_for_cluster = [(ts, price) for _, ts, price in highs]
    lows_for_cluster = [(ts, price) for _, ts, price in lows]
    raw_pivot_resistances = cluster_levels(highs_for_cluster, "resistance", merge_tol) if highs_for_cluster else []
    raw_pivot_supports = cluster_levels(lows_for_cluster, "support", merge_tol) if lows_for_cluster else []

    include_prior_day = bool(use_prior_day_high_low)
    include_prior_week = bool(use_prior_week_high_low)
    session_day = as_of if as_of is not None else latest_session_date(sessions.now_et())
    prior_day_high, prior_day_low = _prior_day_levels(frame, session_day) if include_prior_day else (None, None)
    prior_week_high, prior_week_low = _prior_week_levels(frame, session_day) if include_prior_week else (None, None)

    # The fallback reference price only picks which prior-day/week levels
    # stand in for a side (``safe_reference_price_for_fallback`` keeps a
    # stray live tick from choosing them). Every partition below, and the
    # broken-level test, is against ``close`` itself: every consumer measures
    # from it, so nearest_support stays at or below ``close`` and
    # nearest_resistance at or above it (IF-1), and a fallback level price
    # has crossed joins the pending pool like any other. Partitioned against
    # the reference instead, a gap morning's PDH came out as a resistance
    # below price, at a negative distance.
    support_references: list[Level] = list(raw_pivot_supports)
    resistance_references: list[Level] = list(raw_pivot_resistances)
    if not support_references:
        support_references = fallback_prior_side_levels(
            side="support",
            current_price=fallback_reference_price,
            include_prior_day=include_prior_day,
            include_prior_week=include_prior_week,
            prior_day_high=prior_day_high,
            prior_day_low=prior_day_low,
            prior_week_high=prior_week_high,
            prior_week_low=prior_week_low,
        )
        if not support_references:
            support_references = frame_extreme_side_levels(frame, side="support", tolerance=merge_tol)
    if not resistance_references:
        resistance_references = fallback_prior_side_levels(
            side="resistance",
            current_price=fallback_reference_price,
            include_prior_day=include_prior_day,
            include_prior_week=include_prior_week,
            prior_day_high=prior_day_high,
            prior_day_low=prior_day_low,
            prior_week_high=prior_week_high,
            prior_week_low=prior_week_low,
        )
        if not resistance_references:
            resistance_references = frame_extreme_side_levels(frame, side="resistance", tolerance=merge_tol)

    last_bar = frame.iloc[-1]
    last_low = float(last_bar.low)
    last_high = float(last_bar.high)
    fallback_bar = (last_high, last_low)
    flip_eps = max(abs(close) * 1e-6, 1e-8)
    flip_check = _flip_checker(
        flip_frame,
        confirm_1m_bars=flip_confirmation_1m_bars,
        confirm_5m_bars=flip_confirmation_5m_bars,
        fallback_bar=fallback_bar,
        eps=flip_eps,
    )

    support_candidates, resistance_candidates = split_references_by_flip(
        support_references=support_references,
        resistance_references=resistance_references,
        flip_check=flip_check,
        relabel=None,
    )

    # Candidates beyond their side of price keep their role while the flip is
    # unconfirmed -- they are the pending_support / pending_resistance pool.
    support_candidates, supports_beyond = partition_levels_by_side(support_candidates, close, side="support")
    resistance_candidates, resistances_beyond = partition_levels_by_side(resistance_candidates, close, side="resistance")

    # A side left empty takes the prior-day/week levels, then, if those are
    # all across price too, the frame extreme. Until 2026-09-23 the extreme
    # was tried only when no prior level existed at all, so with prior levels
    # on, a gap past them left the side empty where the builder with them off
    # reported the frame extreme.
    for side in ("support", "resistance"):
        for source in ("prior", "extreme"):
            if support_candidates if side == "support" else resistance_candidates:
                break
            if source == "prior":
                refs = fallback_prior_side_levels(
                    side=side,
                    current_price=fallback_reference_price,
                    include_prior_day=include_prior_day,
                    include_prior_week=include_prior_week,
                    prior_day_high=prior_day_high,
                    prior_day_low=prior_day_low,
                    prior_week_high=prior_week_high,
                    prior_week_low=prior_week_low,
                )
            else:
                refs = frame_extreme_side_levels(frame, side=side, tolerance=merge_tol)
            if not refs:
                continue
            # A ref already among the side's references (the frame extreme
            # when it is a pivot cluster, a prior level that was the first
            # fallback) is already a candidate: adding it again doubled its
            # touches and score when the copies merged.
            added = extend_unique_levels(support_references if side == "support" else resistance_references, refs)
            more_supports, more_resistances = split_references_by_flip(
                support_references=added if side == "support" else [],
                resistance_references=added if side == "resistance" else [],
                flip_check=flip_check,
                relabel=None,
            )
            support_candidates, more_supports_beyond = partition_levels_by_side(support_candidates + more_supports, close, side="support")
            resistance_candidates, more_resistances_beyond = partition_levels_by_side(resistance_candidates + more_resistances, close, side="resistance")
            supports_beyond += more_supports_beyond
            resistances_beyond += more_resistances_beyond

    side_tolerance = _side_tolerance(
        atr,
        close,
        atr_tolerance_mult=float(atr_tolerance_mult),
        pct_tolerance=float(pct_tolerance),
        min_gap_atr_mult=float(same_side_min_gap_atr_mult),
        min_gap_pct=float(same_side_min_gap_pct),
    )
    supports = collapse_same_side_levels(
        support_candidates,
        side_tolerance,
        close,
        reverse=True,
        max_levels=None,
        reduce=_merge_level_group,
    )
    resistances = collapse_same_side_levels(
        resistance_candidates,
        side_tolerance,
        close,
        reverse=False,
        max_levels=None,
        reduce=_merge_level_group,
    )

    # A confirmed flip is broken while price is within ``merge_tol`` of the
    # level or through it (htf_levels allows float noise only).
    broken_support, broken_resistance = detect_broken_levels(
        support_references=support_references,
        resistance_references=resistance_references,
        flip_check=flip_check,
        close=close,
        gate_tol=merge_tol,
        tolerance=side_tolerance,
        relabel=None,
        reduce=_merge_level_group,
    )
    supports, resistances = _reconcile_flipped_levels(
        supports,
        resistances,
        support_candidates=support_candidates,
        resistance_candidates=resistance_candidates,
        broken_support=broken_support,
        broken_resistance=broken_resistance,
        tolerance=side_tolerance,
    )
    ladder_size = max(1, int(max_levels_per_side))
    supports = supports[:ladder_size]
    resistances = resistances[:ladder_size]
    pending_support = pending_level(
        supports_beyond,
        side="support",
        close=close,
        broken=broken_support,
        tolerance=side_tolerance,
        reduce=_merge_level_group,
    )
    pending_resistance = pending_level(
        resistances_beyond,
        side="resistance",
        close=close,
        broken=broken_resistance,
        tolerance=side_tolerance,
        reduce=_merge_level_group,
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
    breakout_age_minutes = breakdown_age_minutes = None
    if (breakout_above_resistance or breakdown_below_support) and flip_frame is not None:
        completed_1m = _completed_1m_bars(flip_frame)
        if breakout_above_resistance and broken_resistance is not None:
            breakout_age_minutes = _minutes_since_level_touch(completed_1m, float(broken_resistance.price), price_above=True)
        if breakdown_below_support and broken_support is not None:
            breakdown_age_minutes = _minutes_since_level_touch(completed_1m, float(broken_support.price), price_above=False)
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
        pending_support=pending_support,
        pending_resistance=pending_resistance,
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
        breakout_age_minutes=breakout_age_minutes,
        breakdown_age_minutes=breakdown_age_minutes,
        near_support=near_support,
        near_resistance=near_resistance,
        bias_score=float(bias),
        regime_hint=regime_hint,
        market_structure=market_structure,
    )
