# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
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
    latest_session_date,
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
    # When each event happened, on the analysis frame's own index (2026-09-24).
    # ``pivot_times`` holds every reduced pivot's bar timestamp, oldest first;
    # the event stamps are the bar the break crossed on (``*_age_bars`` bars
    # back), None when there is no such cross. They are bar LABELS -- the
    # bar's start, like the frame -- so a consumer comparing one with a
    # wall-clock moment must use the bar's close
    # (shared_exit.bar_closed_after). The exit side counts pivots and
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
    supports: list[SupportResistanceLevel] = field(default_factory=list)
    resistances: list[SupportResistanceLevel] = field(default_factory=list)
    nearest_support: SupportResistanceLevel | None = None
    nearest_resistance: SupportResistanceLevel | None = None
    broken_resistance: SupportResistanceLevel | None = None
    broken_support: SupportResistanceLevel | None = None
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
    pending_support: SupportResistanceLevel | None = None
    pending_resistance: SupportResistanceLevel | None = None
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

def _pivot_points(frame: pd.DataFrame, span: int) -> tuple[list[tuple[int, pd.Timestamp, float]], list[tuple[int, pd.Timestamp, float]]]:
    # Thin wrapper around the shared `pivot_points` helper. Uses
    # include_idx=True so structure_event detection downstream can
    # anchor a pivot to its bar position. htf_levels uses include_idx=False.
    return pivot_points(frame, span, include_idx=True)


def _cluster_levels(points: Iterable[tuple[pd.Timestamp, float]], kind: str, tolerance: float) -> list[SupportResistanceLevel]:
    # Thin wrapper around the shared `cluster_levels` helper — same
    # multiplicative-recency formula as htf_levels so the two builders
    # can't drift in their scoring. The previous module-local version
    # used an additive recency bonus (touches * 1.15 + 0.60 *
    # recency_factor) which let ancient high-touch bases dominate
    # close-to-price recent swings — the bug we found in the AMD/INTC
    # debug. Effective touches via multiplication keeps the rank
    # ordering stable while letting recency genuinely weight selection.
    return cluster_levels(points, kind, tolerance, level_factory=SupportResistanceLevel)


def _reduced_pivots(
    frame: pd.DataFrame, span: int, min_gap_bars: int = 0,
) -> list[tuple[str, int, pd.Timestamp, float]]:
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
        prev_kind, prev_pos, _prev_ts, prev_price = reduced[-1]
        if kind != prev_kind:
            # Minimum-gap filter (2026-05-27, Fix B): an alternating pivot
            # closer than ``min_gap_bars`` to the prior kept pivot is noise
            # within the current leg — skip it so the leg continues instead
            # of registering a 1-2-bar swing that churns the structure
            # labels. Disabled when min_gap_bars <= 0.
            if min_gap_bars > 0 and (pos - prev_pos) < min_gap_bars:
                continue
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
            return "bullish" if up_age < down_age else "bearish"
    elif above_high:
        return "bullish"
    elif below_low:
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
    min_pivot_gap_bars: int = 0,
    last_bar_forming: bool = False,
) -> MarketStructureContext:
    frame = ensure_standard_indicator_frame(frame)
    if frame.empty:
        return empty_market_structure_context(float(current_price or 0.0))
    close = resolve_current_price(frame, current_price)
    atr = atr_value(frame)
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
    pivots = _reduced_pivots(frame.iloc[:-1] if last_bar_forming else frame, int(pivot_span), int(min_pivot_gap_bars))
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
) -> list[SupportResistanceLevel]:
    # Thin factory-binding wrapper around `frame_extreme_side_levels`.
    return _frame_extreme_side_levels_shared(
        frame,
        side=side,
        tolerance=tolerance,
        level_factory=SupportResistanceLevel,
    )


def _partition_levels_by_side(
    levels: list[SupportResistanceLevel],
    current_price: float,
    *,
    side: str,
) -> tuple[list[SupportResistanceLevel], list[SupportResistanceLevel]]:
    """Split ``levels`` into those on their side of ``current_price`` --
    supports at or below it (nearest first), resistances at or above it --
    and those beyond it. The builder reports the nearest of the latter whose
    flip is unconfirmed as ``pending_support`` / ``pending_resistance``."""
    eps = max(abs(float(current_price)) * 1e-6, 1e-8)
    if side == "support":
        on_side = [lv for lv in levels if float(lv.price) <= float(current_price) + eps]
        on_side.sort(key=lambda lv: lv.price, reverse=True)
        beyond = [lv for lv in levels if float(lv.price) > float(current_price) + eps]
        return on_side, beyond
    on_side = [lv for lv in levels if float(lv.price) >= float(current_price) - eps]
    on_side.sort(key=lambda lv: lv.price)
    beyond = [lv for lv in levels if float(lv.price) < float(current_price) - eps]
    return on_side, beyond


def _pending_level(
    beyond: list[SupportResistanceLevel],
    *,
    side: str,
    close: float,
    broken: SupportResistanceLevel | None,
    tolerance: float,
) -> SupportResistanceLevel | None:
    """The nearest level of ``side`` that price has crossed while its flip is
    unconfirmed -- a support now above ``close`` or a resistance now below it.

    ``beyond`` holds the candidates the partition against ``close`` dropped.
    A level within ``tolerance`` of the confirmed flip on that side
    (``broken``) is the same zone, already flipped, so it is not pending."""
    crossed = list(beyond)
    if broken is not None:
        crossed = _drop_levels_near_price(crossed, float(broken.price), tolerance=tolerance)
    if not crossed:
        return None
    # Nearest to price first: crossed supports ascend from just above price,
    # crossed resistances descend from just below it.
    return _collapse_same_side_levels(
        crossed,
        tolerance,
        close,
        reverse=(side != "support"),
        max_levels=1,
    )[0]



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



def _completed_1m_bars(flip_frame: pd.DataFrame | None) -> pd.DataFrame:
    """The flip frame's 1m bars that have completed at ``now_et()``."""
    base = ensure_ohlcv_frame(flip_frame if flip_frame is not None else pd.DataFrame())
    if base.empty:
        return base
    return base[base.index < pd.Timestamp(now_et()).floor("1min")]


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


def _completed_flip_frames(flip_frame: pd.DataFrame | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    completed_1m = _completed_1m_bars(flip_frame)
    if completed_1m.empty:
        return completed_1m, pd.DataFrame(columns=completed_1m.columns)
    one_min_cutoff = pd.Timestamp(now_et()).floor("1min")
    completed_5m = resample_bars(completed_1m, "5min")
    if not completed_5m.empty:
        # A 5m bar labelled T holds the 1m bars starting T .. T+4 (see
        # resample_bars), so it is complete once its last constituent, T+4,
        # is: T + 5min <= one_min_cutoff. The bucket still filling when the
        # completed 1m bars run out fails that and is dropped, so flip
        # confirmation never reads a partial 5m bar.
        completed_5m = completed_5m[completed_5m.index + pd.Timedelta(minutes=5) <= one_min_cutoff]
    return completed_1m, completed_5m


FlipCheck = Callable[[float, str], bool]


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

    def check(level_price: float, direction: str) -> bool:
        field_name, comparator = ("low", "above") if direction == "reclaim" else ("high", "below")
        if (
            confirm_by_bars(completed_1m, field_name, comparator, level_price, bars_1m, tol)
            or confirm_by_bars(completed_5m, field_name, comparator, level_price, bars_5m, tol)
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


def _split_references_by_flip(
    *,
    support_references: list,
    resistance_references: list,
    flip_check: FlipCheck,
) -> tuple[list, list]:
    """Initial side-assignment for every reference level. Support refs that
    have been decisively lost move to the resistance side; resistance refs
    that have been reclaimed move to the support side. Everything else
    retains its original side. Extracted from
    build_support_resistance_context for Phase 3b decomposition."""
    support_candidates: list = []
    resistance_candidates: list = []
    for level in support_references:
        if flip_check(float(level.price), "loss"):
            resistance_candidates.append(_clone_level(level, "resistance"))
        else:
            support_candidates.append(_clone_level(level, "support"))
    for level in resistance_references:
        if flip_check(float(level.price), "reclaim"):
            support_candidates.append(_clone_level(level, "support"))
        else:
            resistance_candidates.append(_clone_level(level, "resistance"))
    return support_candidates, resistance_candidates


def _detect_broken_levels(
    *,
    support_references: list,
    resistance_references: list,
    flip_check: FlipCheck,
    merge_tol: float,
    side_tolerance: float,
    close: float,
    max_levels_per_side: int,
):
    """Detect levels that have flipped direction: former resistance now acting
    as support (reclaim) and former support now acting as resistance (loss).

    Returns (broken_support, broken_resistance) — each the top collapsed level
    on that side, or None. A reclaimed resistance must sit at or below
    ``close`` (a lost support at or above it), within ``merge_tol``: the same
    price the ladders are partitioned against. Until 2026-09-23 it was the
    fallback reference price whenever a side had fallen back to prior levels,
    so a confirmed-lost pivot support between ``close`` and that reference
    showed as a flipped resistance in the ladder with broken_support None.
    Extracted from build_support_resistance_context for Phase 3b
    decomposition."""
    broken_resistance_candidates = [
        _clone_level(level, "support")
        for level in resistance_references
        if flip_check(float(level.price), "reclaim")
        and float(level.price) <= close + merge_tol
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
        if flip_check(float(level.price), "loss")
        and float(level.price) >= close - merge_tol
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
    # near_* is about nearest_support / nearest_resistance, which always sit
    # on their own side of price; the breakdown / breakout flags are about a
    # broken level on the far side (a support now above price, a resistance
    # now below it). Until 2026-09-23 either flag switched the near term
    # off, so on about half of all checkpoints an unrelated old level below
    # price dropped the -0.35 resistance-pressure term (and the mirror).
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
    # None means the clock's (``latest_session_date(now_et())``).
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
    raw_pivot_resistances = _cluster_levels(highs_for_cluster, "resistance", merge_tol) if highs_for_cluster else []
    raw_pivot_supports = _cluster_levels(lows_for_cluster, "support", merge_tol) if lows_for_cluster else []

    include_prior_day = bool(use_prior_day_high_low)
    include_prior_week = bool(use_prior_week_high_low)
    session_day = as_of if as_of is not None else latest_session_date(now_et())
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
    support_references: list[SupportResistanceLevel] = list(raw_pivot_supports)
    resistance_references: list[SupportResistanceLevel] = list(raw_pivot_resistances)
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
        if not support_references:
            min_low_pos = int(frame["low"].astype(float).values.argmin())
            support_references = _cluster_levels(
                [(pd.Timestamp(frame.index[min_low_pos]), float(frame["low"].iloc[min_low_pos]))],
                "support",
                merge_tol,
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
        if not resistance_references:
            max_high_pos = int(frame["high"].astype(float).values.argmax())
            resistance_references = _cluster_levels(
                [(pd.Timestamp(frame.index[max_high_pos]), float(frame["high"].iloc[max_high_pos]))],
                "resistance",
                merge_tol,
            )

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

    support_candidates, resistance_candidates = _split_references_by_flip(
        support_references=support_references,
        resistance_references=resistance_references,
        flip_check=flip_check,
    )

    # Candidates beyond their side of price keep their role while the flip is
    # unconfirmed -- they are the pending_support / pending_resistance pool.
    support_candidates, supports_beyond = _partition_levels_by_side(support_candidates, close, side="support")
    resistance_candidates, resistances_beyond = _partition_levels_by_side(resistance_candidates, close, side="resistance")

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
                refs = _fallback_prior_side_levels(
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
                refs = _frame_extreme_side_levels(frame, side=side, tolerance=merge_tol)
            if not refs:
                continue
            # A ref already among the side's references (the frame extreme
            # when it is a pivot cluster, a prior level that was the first
            # fallback) is already a candidate: adding it again doubled its
            # touches and score when the copies merged.
            added = extend_unique_levels(support_references if side == "support" else resistance_references, refs)
            for level in added:
                if side == "support" and flip_check(float(level.price), "loss"):
                    resistance_candidates.append(_clone_level(level, "resistance"))
                elif side == "resistance" and flip_check(float(level.price), "reclaim"):
                    support_candidates.append(_clone_level(level, "support"))
                elif side == "support":
                    support_candidates.append(_clone_level(level, "support"))
                else:
                    resistance_candidates.append(_clone_level(level, "resistance"))
            support_candidates, more_supports_beyond = _partition_levels_by_side(support_candidates, close, side="support")
            resistance_candidates, more_resistances_beyond = _partition_levels_by_side(resistance_candidates, close, side="resistance")
            supports_beyond += more_supports_beyond
            resistances_beyond += more_resistances_beyond

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
        flip_check=flip_check,
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
    pending_support = _pending_level(
        supports_beyond,
        side="support",
        close=close,
        broken=broken_support,
        tolerance=side_tolerance,
    )
    pending_resistance = _pending_level(
        resistances_beyond,
        side="resistance",
        close=close,
        broken=broken_resistance,
        tolerance=side_tolerance,
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
