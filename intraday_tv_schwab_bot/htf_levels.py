# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import logging

import numpy as np
import pandas as pd

from .divergence import DivergenceMatch, divergence_inputs, find_divergence
from .levels_shared import (
    FlipCheck,
    Level,
    cluster_levels,
    collapse_same_side_levels,
    confirm_by_bars,
    detect_broken_levels,
    extend_unique_levels,
    fallback_prior_side_levels,
    frame_extreme_side_levels,
    partition_levels_by_side,
    pending_level,
    pivot_points,
    prior_day_levels as _prior_day_levels,
    prior_week_levels as _prior_week_levels,
    safe_reference_price_for_fallback as _safe_reference_price_for_fallback,
    side_tolerance,
    split_references_by_flip,
)
from .fair_value_gaps import HTFFairValueGap, detect_fair_value_gaps
from .sessions import latest_session_date
from .numeric import safe_float
from .bars import completed_bars, ensure_ohlcv_frame, resolve_current_price
from .indicators import (
    ensure_standard_indicator_frame,
    floor_atr,
    get_runtime_indicator_mode,
    has_standard_indicator_columns,
    indicator_session_mask,
    latest_atr14,
)
from . import sessions


LOG = logging.getLogger(__name__)

_OHLCV = ("open", "high", "low", "close", "volume")


@dataclass(slots=True)
class HTFContext:
    timeframe_minutes: int
    current_price: float
    supports: list[Level] = field(default_factory=list)
    resistances: list[Level] = field(default_factory=list)
    nearest_support: Level | None = None
    broken_resistance: Level | None = None
    nearest_resistance: Level | None = None
    broken_support: Level | None = None
    # The nearest support price has crossed below / resistance crossed above
    # whose flip is not yet confirmed, still in its original role -- the HTF
    # twin of SupportResistanceContext.pending_*, with the same history.
    pending_support: Level | None = None
    pending_resistance: Level | None = None
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


def _level_preference(level: Level, current_price: float) -> tuple[float, float, int, float]:
    return (
        float(getattr(level, "source_priority", 1.0) or 1.0),
        float(level.score),
        int(level.touches),
        -abs(float(level.price) - float(current_price)),
    )


def _representative_level(group: list[Level], current_price: float) -> Level:
    # The HTF cluster reducer (levels_shared.collapse_same_side_levels): one
    # representative per cluster (highest source_priority, then score,
    # touches, distance). support_resistance merges its clusters instead;
    # the split is deliberate, so each builder keeps its own reducer.
    return max(group, key=lambda lv: _level_preference(lv, current_price))


# The ``source`` of a flipped HTF level (levels_shared.split_references_by_flip
# / detect_broken_levels ``relabel``): a lost support, a reclaimed resistance.
# A contract: the peer strategies read a level's source as its zone kind, and
# ``broken_htf_*`` marks a flipped level and names the side it came from
# (peer_confirmed_htf_pivots ``_non_fvg_zone_original_kind``).
_BROKEN_HTF_SOURCES = ("broken_htf_support", "broken_htf_resistance")


def _htf_flip_checker(
    frame: pd.DataFrame,
    *,
    completed: pd.DataFrame | None,
    confirm_bars: int,
    eps: float,
) -> FlipCheck:
    """Return ``check(level_price, direction)``: is the level's flip active?

    With ``confirm_bars > 0``, ``"reclaim"`` needs the last ``confirm_bars``
    of the ``completed`` HTF bars' lows above the level and ``"loss"`` the
    matching highs below it; with 0 the frame's last bar decides. The
    completed frame is cut ONCE per build (``htf_context_at``): each level
    used to copy and cut the whole frame again, and removing the pre-split
    cluster cap on 2026-09-23 multiplied the levels.
    """
    bars = max(0, int(confirm_bars or 0))
    tol = float(eps)
    if bars > 0:
        def confirmed(level_price: float, direction: str) -> bool:
            if direction == "reclaim":
                return confirm_by_bars(completed, "low", "above", level_price, bars, tol)
            return confirm_by_bars(completed, "high", "below", level_price, bars, tol)

        return confirmed
    last_low = last_high = None
    if frame is not None and not frame.empty:
        last_bar = frame.iloc[-1]
        last_low = float(last_bar.get("low", 0.0) or 0.0)
        last_high = float(last_bar.get("high", 0.0) or 0.0)

    def last_bar_beyond(level_price: float, direction: str) -> bool:
        if last_low is None or last_high is None:
            return False
        if direction == "reclaim":
            return last_low > float(level_price) + tol
        return last_high < float(level_price) - tol

    return last_bar_beyond


@dataclass(frozen=True, slots=True)
class HTFLevelInputs:
    """What an HTF context build reads of its frame before it needs a
    price (``prepare_htf_levels``): the frame's unfloored ATR, the pivots,
    the prior day / week levels, the EMAs and the RSI divergences, and the
    flip confirmation they were prepared for. Every field is a function of
    the frame, these arguments and the clock as ``as_of`` and
    ``context_memo.indicator_clock_key`` read it, never of a price:
    ``htf_context_at`` builds the context of the frame at any price from
    it. It holds no frame, so two of them compare in about 2 ms (the memo
    shadow compares a rebuild with the kept one field by field; with the
    frame in it, a comparison took 0.7 s).

    The data feed keeps it in the level memo, in a slot of its own, while
    its frame is the stored one and the clock reads the same
    (``MarketDataStore._htf_context_from_stored_frame``), so a context at a
    new price (each 1m bar) redoes only the price-dependent part, and the
    memo's shadow rebuilds it in full."""

    timeframe_minutes: int
    atr14: float | None
    pivot_highs: list[tuple[int, pd.Timestamp, float]]
    pivot_lows: list[tuple[int, pd.Timestamp, float]]
    include_prior_day: bool
    include_prior_week: bool
    prior_day_high: float | None
    prior_day_low: float | None
    prior_week_high: float | None
    prior_week_low: float | None
    ema_fast: float | None
    ema_slow: float | None
    flip_confirmation_bars: int
    bullish_rsi_divergence: DivergenceMatch | None
    bearish_rsi_divergence: DivergenceMatch | None
    bullish_hidden_rsi_divergence: DivergenceMatch | None
    bearish_hidden_rsi_divergence: DivergenceMatch | None


def _htf_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """The frame an HTF build reads: ``frame``'s OHLCV bars, cleaned
    (``ensure_ohlcv_frame``), with the standard indicators.
    ``prepare_htf_levels`` and ``htf_context_at`` each read it, so the
    positions in one are the positions in the other.

    A frame that already is one -- OHLCV leading, unique columns, strictly
    increasing labels, float64 OHLC and a numeric volume without a NaN, the
    standard indicator columns: every frame the data feed stores
    (``_refresh_htf_frame``) -- is read through a shallow copy, which holds
    the values and dtypes the rebuild gives, for about a tenth of its cost
    (the rebuild keeps an integer volume as it is, so ``ensure_ohlcv_frame``
    alone does not take its own shortcut on it); ``htf_context_at`` reads
    one per context at a price."""
    if _is_htf_frame(frame):
        return frame.copy(deep=False)
    return ensure_standard_indicator_frame(ensure_ohlcv_frame(frame))


def _is_htf_frame(frame: pd.DataFrame) -> bool:
    if not has_standard_indicator_columns(frame) or tuple(frame.columns[:5]) != _OHLCV:
        return False
    if not (frame.columns.is_unique and frame.index.is_monotonic_increasing and frame.index.is_unique):
        return False
    for column in _OHLCV:
        values = frame[column]
        if column == "volume" and values.dtype == np.int64:
            continue
        if values.dtype != np.float64 or np.isnan(values.to_numpy()).any():
            return False
    return True


def prepare_htf_levels(
    frame: pd.DataFrame,
    *,
    timeframe_minutes: int = 60,
    pivot_span: int = 2,
    ema_fast_span: int = 50,
    ema_slow_span: int = 200,
    flip_confirmation_bars: int = 1,
    use_prior_day_high_low: bool = True,
    use_prior_week_high_low: bool = True,
    divergence_enabled: bool = True,
    divergence_pivot_lookback: int = 4,
    divergence_max_age_bars: int = 6,
    divergence_min_price_move_pct: float = 0.0015,
    divergence_rsi_min_delta: float = 2.5,
    as_of: date | None = None,
) -> HTFLevelInputs | None:
    """The price-free part of an HTF context build of ``frame``
    (``HTFLevelInputs``), or None for an empty frame. ``as_of`` is the
    session date the prior day / week are measured back from; None means
    the clock's (``latest_session_date(sessions.now_et())``)."""
    frame = _htf_frame(frame)
    if frame.empty:
        return None
    # The session mask the ATR and the divergence read, computed once.
    in_session = indicator_session_mask(frame.index) if get_runtime_indicator_mode() else None
    # One detection pass serves the levels and the RSI divergence below: the
    # divergence reads each pivot's bar position, clustering only its
    # (timestamp, price).
    pivot_highs, pivot_lows = pivot_points(frame, int(pivot_span), include_idx=True)
    include_prior_day = bool(use_prior_day_high_low)
    include_prior_week = bool(use_prior_week_high_low)
    session_day = as_of if as_of is not None else latest_session_date(sessions.now_et())
    prior_day_high, prior_day_low = _prior_day_levels(frame, session_day) if include_prior_day else (None, None)
    prior_week_high, prior_week_low = _prior_week_levels(frame, session_day) if include_prior_week else (None, None)
    confirm_bars = max(0, int(flip_confirmation_bars or 0))
    ema_fast = float(frame["close"].ewm(span=int(ema_fast_span), adjust=False).mean().iloc[-1]) if len(frame) >= max(5, int(ema_fast_span) // 3) else None
    ema_slow = float(frame["close"].ewm(span=int(ema_slow_span), adjust=False).mean().iloc[-1]) if len(frame) >= int(ema_slow_span) else None

    # HTF RSI divergence — multi-timeframe confluence signal. Uses the
    # level pivots' bar positions so the shared find_divergence can pull RSI
    # values at exact pivot positions and tag age in HTF bars (session bars
    # under the session clock: divergence.divergence_inputs). RSI series is
    # whatever ensure_standard_indicator_frame populated under "rsi14".
    # The thresholds and the switch arrive from technical_levels through the
    # data feed (MarketDataStore.get_htf_context, 2026-09-25 for the
    # thresholds); the signature defaults are the values every preset
    # ships, for the callers that build a context directly.
    bullish_rsi_div: DivergenceMatch | None = None
    bearish_rsi_div: DivergenceMatch | None = None
    bullish_hidden_rsi_div: DivergenceMatch | None = None
    bearish_hidden_rsi_div: DivergenceMatch | None = None
    if divergence_enabled and "rsi14" in frame.columns and len(frame) > 0:
        rsi_series = frame["rsi14"].astype(float)
        if not rsi_series.dropna().empty:
            highs_idx, lows_idx, bar_clock, price_scale = divergence_inputs(
                frame, pivot_highs, pivot_lows, in_session=in_session,
            )
            last_bar_pos = max(0, len(frame) - 1)
            move_frac = max(0.0001, float(divergence_min_price_move_pct))
            rsi_delta = max(0.0, float(divergence_rsi_min_delta))
            bullish_rsi_div = find_divergence(
                lows_idx, rsi_series, kind="regular", direction="bullish",
                indicator_name="rsi", price_move_frac=move_frac,
                indicator_delta=rsi_delta, pivot_lookback=divergence_pivot_lookback,
                max_age_bars=divergence_max_age_bars, last_bar_pos=last_bar_pos,
                price_scale=price_scale, bar_clock=bar_clock,
            )
            bearish_rsi_div = find_divergence(
                highs_idx, rsi_series, kind="regular", direction="bearish",
                indicator_name="rsi", price_move_frac=move_frac,
                indicator_delta=rsi_delta, pivot_lookback=divergence_pivot_lookback,
                max_age_bars=divergence_max_age_bars, last_bar_pos=last_bar_pos,
                price_scale=price_scale, bar_clock=bar_clock,
            )
            bullish_hidden_rsi_div = find_divergence(
                lows_idx, rsi_series, kind="hidden", direction="bullish",
                indicator_name="rsi", price_move_frac=move_frac,
                indicator_delta=rsi_delta, pivot_lookback=divergence_pivot_lookback,
                max_age_bars=divergence_max_age_bars, last_bar_pos=last_bar_pos,
                price_scale=price_scale, bar_clock=bar_clock,
            )
            bearish_hidden_rsi_div = find_divergence(
                highs_idx, rsi_series, kind="hidden", direction="bearish",
                indicator_name="rsi", price_move_frac=move_frac,
                indicator_delta=rsi_delta, pivot_lookback=divergence_pivot_lookback,
                max_age_bars=divergence_max_age_bars, last_bar_pos=last_bar_pos,
                price_scale=price_scale, bar_clock=bar_clock,
            )

    return HTFLevelInputs(
        timeframe_minutes=int(timeframe_minutes),
        atr14=latest_atr14(frame, in_session=in_session),
        pivot_highs=pivot_highs,
        pivot_lows=pivot_lows,
        include_prior_day=include_prior_day,
        include_prior_week=include_prior_week,
        prior_day_high=prior_day_high,
        prior_day_low=prior_day_low,
        prior_week_high=prior_week_high,
        prior_week_low=prior_week_low,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        flip_confirmation_bars=confirm_bars,
        bullish_rsi_divergence=bullish_rsi_div,
        bearish_rsi_divergence=bearish_rsi_div,
        bullish_hidden_rsi_divergence=bullish_hidden_rsi_div,
        bearish_hidden_rsi_divergence=bearish_hidden_rsi_div,
    )


def htf_context_at(
    frame: pd.DataFrame,
    inputs: HTFLevelInputs | None,
    current_price: float | None,
    *,
    timeframe_minutes: int = 60,
    max_levels_per_side: int = 6,
    atr_tolerance_mult: float = 0.35,
    pct_tolerance: float = 0.0030,
    same_side_min_gap_atr_mult: float = 0.10,
    same_side_min_gap_pct: float = 0.0015,
    fallback_reference_max_drift_atr_mult: float = 1.0,
    fallback_reference_max_drift_pct: float = 0.01,
    stop_buffer_atr_mult: float = 0.25,
    include_fair_value_gaps: bool = True,
    fair_value_gap_max_per_side: int = 4,
    fair_value_gap_min_atr_mult: float = 0.05,
    fair_value_gap_min_pct: float = 0.0005,
) -> HTFContext:
    """The HTF context of ``frame`` at ``current_price`` on its
    ``prepare_htf_levels`` inputs (the empty context for None, an empty
    frame): everything that reads the price -- the ATR's floors and the
    tolerances sized from them, the level clusters, the fallbacks, the
    flips, which side of price each level is on, the nearest / broken /
    pending levels, the FVGs (their sizes floor at a share of the price),
    the trend bias and the level buffer. ``inputs`` must be prepared from
    ``frame``. A price that is not positive reads as the frame's last close
    (``resolve_current_price``). ``timeframe_minutes`` labels the empty
    context only; a built one carries its inputs'."""
    if inputs is None:
        return empty_htf_context(float(current_price or 0.0), timeframe_minutes=timeframe_minutes)
    frame = _htf_frame(frame)
    timeframe_minutes = inputs.timeframe_minutes
    close = resolve_current_price(frame, current_price)
    atr = floor_atr(inputs.atr14, close, abs_floor=0.01)
    tolerance = max(atr * float(atr_tolerance_mult), close * float(pct_tolerance))
    fallback_reference_price = _safe_reference_price_for_fallback(
        frame,
        close,
        atr=atr,
        max_drift_atr_mult=float(fallback_reference_max_drift_atr_mult),
        max_drift_pct=float(fallback_reference_max_drift_pct),
    )
    collapse_tolerance = side_tolerance(
        atr,
        close,
        atr_tolerance_mult=float(atr_tolerance_mult),
        pct_tolerance=float(pct_tolerance),
        min_gap_atr_mult=float(same_side_min_gap_atr_mult),
        min_gap_pct=float(same_side_min_gap_pct),
    )

    pivot_highs, pivot_lows = inputs.pivot_highs, inputs.pivot_lows
    pivot_resistances = cluster_levels([(ts, price) for _pos, ts, price in pivot_highs], "resistance", tolerance) if pivot_highs else []
    pivot_supports = cluster_levels([(ts, price) for _pos, ts, price in pivot_lows], "support", tolerance) if pivot_lows else []

    include_prior_day = inputs.include_prior_day
    include_prior_week = inputs.include_prior_week
    prior_day_high, prior_day_low = inputs.prior_day_high, inputs.prior_day_low
    prior_week_high, prior_week_low = inputs.prior_week_high, inputs.prior_week_low

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
    # pivot detection. The "second-chance" pass further down covers the
    # strong-directional case by re-injecting them -- that's where they
    # belong. The fallback reference price only picks which prior levels
    # stand in for a side; every partition and the broken-level test are
    # against ``close`` (see support_resistance.build_support_resistance_context).
    support_references: list[Level] = list(pivot_supports)
    resistance_references: list[Level] = list(pivot_resistances)
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
            support_references = frame_extreme_side_levels(frame, side="support", tolerance=tolerance)
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
            resistance_references = frame_extreme_side_levels(frame, side="resistance", tolerance=tolerance)

    eps = max(abs(close) * 1e-6, 1e-8)
    confirm_bars = inputs.flip_confirmation_bars
    flip_active = _htf_flip_checker(
        frame,
        # Cut once per build, not per level (``_htf_flip_checker``).
        completed=completed_bars(frame, int(timeframe_minutes)) if confirm_bars > 0 else None,
        confirm_bars=confirm_bars,
        eps=eps,
    )

    support_candidates, resistance_candidates = split_references_by_flip(
        support_references=support_references,
        resistance_references=resistance_references,
        flip_check=flip_active,
        relabel=_BROKEN_HTF_SOURCES,
    )

    # Candidates beyond their side of price keep their role while the flip is
    # unconfirmed -- they are the pending_support / pending_resistance pool.
    support_candidates, supports_beyond = partition_levels_by_side(support_candidates, close, side="support")
    resistance_candidates, resistances_beyond = partition_levels_by_side(resistance_candidates, close, side="resistance")

    # A side left empty takes the prior-day/week levels, then, if those are
    # all across price too, the frame extreme (as in
    # support_resistance.build_support_resistance_context).
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
                refs = frame_extreme_side_levels(frame, side=side, tolerance=tolerance)
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
                flip_check=flip_active,
                relabel=_BROKEN_HTF_SOURCES,
            )
            support_candidates, more_supports_beyond = partition_levels_by_side(support_candidates + more_supports, close, side="support")
            resistance_candidates, more_resistances_beyond = partition_levels_by_side(resistance_candidates + more_resistances, close, side="resistance")
            supports_beyond += more_supports_beyond
            resistances_beyond += more_resistances_beyond
    # One representative per cluster, no reconcile pass (support_resistance
    # re-drops its ladder near the confirmed flips; HTF does not).
    supports = collapse_same_side_levels(
        support_candidates,
        collapse_tolerance,
        close,
        reverse=True,
        max_levels=int(max_levels_per_side),
        reduce=_representative_level,
    )
    resistances = collapse_same_side_levels(
        resistance_candidates,
        collapse_tolerance,
        close,
        reverse=False,
        max_levels=int(max_levels_per_side),
        reduce=_representative_level,
    )
    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None

    # A confirmed flip is broken only once price is through the level: within
    # float noise of ``close`` (support_resistance allows its merge tolerance).
    broken_support, broken_resistance = detect_broken_levels(
        support_references=support_references,
        resistance_references=resistance_references,
        flip_check=flip_active,
        close=close,
        gate_tol=eps,
        tolerance=collapse_tolerance,
        relabel=_BROKEN_HTF_SOURCES,
        reduce=_representative_level,
    )
    pending_support = pending_level(
        supports_beyond,
        side="support",
        close=close,
        broken=broken_support,
        tolerance=collapse_tolerance,
        reduce=_representative_level,
    )
    pending_resistance = pending_level(
        resistances_beyond,
        side="resistance",
        close=close,
        broken=broken_resistance,
        tolerance=collapse_tolerance,
        reduce=_representative_level,
    )

    ema_fast, ema_slow = inputs.ema_fast, inputs.ema_slow

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
        bullish_fvgs, bearish_fvgs, nearest_bullish_fvg, nearest_bearish_fvg = detect_fair_value_gaps(
            frame,
            timeframe_minutes=int(timeframe_minutes),
            current_price=close,
            max_per_side=max(0, int(fair_value_gap_max_per_side or 0)),
            min_gap_atr_mult=float(fair_value_gap_min_atr_mult),
            min_gap_pct=float(fair_value_gap_min_pct),
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
        pending_support=pending_support,
        pending_resistance=pending_resistance,
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
        bullish_rsi_divergence=inputs.bullish_rsi_divergence,
        bearish_rsi_divergence=inputs.bearish_rsi_divergence,
        bullish_hidden_rsi_divergence=inputs.bullish_hidden_rsi_divergence,
        bearish_hidden_rsi_divergence=inputs.bearish_hidden_rsi_divergence,
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
    as_of: date | None = None,
) -> HTFContext:
    """The HTF context of ``frame`` at ``current_price`` in one call:
    ``prepare_htf_levels`` then ``htf_context_at``. ``as_of`` is the session
    date the prior day / week are measured back from; None means the
    clock's."""
    inputs = prepare_htf_levels(
        frame,
        timeframe_minutes=timeframe_minutes,
        pivot_span=pivot_span,
        ema_fast_span=ema_fast_span,
        ema_slow_span=ema_slow_span,
        flip_confirmation_bars=flip_confirmation_bars,
        use_prior_day_high_low=use_prior_day_high_low,
        use_prior_week_high_low=use_prior_week_high_low,
        divergence_enabled=divergence_enabled,
        divergence_pivot_lookback=divergence_pivot_lookback,
        divergence_max_age_bars=divergence_max_age_bars,
        divergence_min_price_move_pct=divergence_min_price_move_pct,
        divergence_rsi_min_delta=divergence_rsi_min_delta,
        as_of=as_of,
    )
    return htf_context_at(
        frame,
        inputs,
        current_price,
        timeframe_minutes=timeframe_minutes,
        max_levels_per_side=max_levels_per_side,
        atr_tolerance_mult=atr_tolerance_mult,
        pct_tolerance=pct_tolerance,
        same_side_min_gap_atr_mult=same_side_min_gap_atr_mult,
        same_side_min_gap_pct=same_side_min_gap_pct,
        fallback_reference_max_drift_atr_mult=fallback_reference_max_drift_atr_mult,
        fallback_reference_max_drift_pct=fallback_reference_max_drift_pct,
        stop_buffer_atr_mult=stop_buffer_atr_mult,
        include_fair_value_gaps=include_fair_value_gaps,
        fair_value_gap_max_per_side=fair_value_gap_max_per_side,
        fair_value_gap_min_atr_mult=fair_value_gap_min_atr_mult,
        fair_value_gap_min_pct=fair_value_gap_min_pct,
    )
