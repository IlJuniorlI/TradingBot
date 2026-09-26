# SPDX-License-Identifier: MIT
"""Indicators: the process-wide session-indicator mode, EMA span scaling,
the TA-Lib wrappers, the session stitch and masks, ATR reads, and
``add_indicators``."""
import math
from collections.abc import Mapping
from datetime import time
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

try:
    import talib  # type: ignore
except Exception:  # pragma: no cover - optional until indicators are computed
    talib = None

from . import sessions
from .bars import ensure_ohlcv_frame
from .models import Side
from .numeric import safe_float
from .sessions import EQUITY_RTH_OPEN, EQUITY_STREAM_START, session_mask

_USE_RTH_SESSION_INDICATORS = True
# Which session window the per-session indicator reset (VWAP/EMA/TA-Lib
# overlay) keys off when _USE_RTH_SESSION_INDICATORS is on. "rth" = the
# 09:30-16:00 regular session (default, original behavior). "extended" =
# the 07:00-20:00 equity stream window, for strategies that trade pre/post
# market. Only affects the session mask predicate in add_indicators.
_SESSION_INDICATOR_WINDOW = "rth"


def set_runtime_indicator_mode(enabled: bool) -> None:
    global _USE_RTH_SESSION_INDICATORS
    _USE_RTH_SESSION_INDICATORS = bool(enabled)


def get_runtime_indicator_mode() -> bool:
    return bool(_USE_RTH_SESSION_INDICATORS)


def set_session_indicator_window(window: str) -> None:
    global _SESSION_INDICATOR_WINDOW
    w = str(window or "rth").strip().lower()
    _SESSION_INDICATOR_WINDOW = "extended" if w == "extended" else "rth"


def get_session_indicator_window() -> str:
    return _SESSION_INDICATOR_WINDOW


def indicator_session_start() -> time:
    """The time of day the indicator session starts: the 09:30 open, or the
    07:00 equity-stream open under the "extended" window. The session
    VWAP / EMA reset keys off it, so a reader anchoring "the session" to
    that reset (the session open, the leg-anchor scan) starts here too."""
    return EQUITY_STREAM_START if get_session_indicator_window() == "extended" else EQUITY_RTH_OPEN


STANDARD_INDICATOR_COLUMNS: tuple[str, ...] = (
    "vwap_all",
    "ema9_all",
    "ema20_all",
    "vwap_rth",
    "ema9_rth",
    "ema20_rth",
    "vwap_signal",
    "ema9_signal",
    "ema20_signal",
    "vwap",
    "ema9",
    "ema20",
    "bb_mid",
    "bb_upper",
    "bb_lower",
    "bb_width",
    "bb_width_pct",
    "bb_percent_b",
    "bb_zscore",
    "atr14",
    "plus_di14",
    "minus_di14",
    "adx14",
    "obv",
    "obv_ema20",
    "obv_delta5",
    "rsi14",
    "ret1",
    "ret5",
    "ret15",
)


def has_standard_indicator_columns(frame: pd.DataFrame) -> bool:
    return frame is not None and not frame.empty and all(col in frame.columns for col in STANDARD_INDICATOR_COLUMNS)


def ensure_standard_indicator_frame(
    frame: pd.DataFrame,
    *,
    span_scale: float = 1.0,
    ema_spans: tuple[int, int] | None = None,
) -> pd.DataFrame:
    # Fast path: if the frame already carries every standard indicator column,
    # it was produced by add_indicators() upstream which itself calls
    # ensure_ohlcv_frame internally. Re-running ensure_ohlcv_frame here on the
    # hot path (copy + sort + 5x to_numeric + dropna + reorder) is the single
    # biggest overhead in build_technical_levels_context / analyze_market_structure
    # when the frame is already clean. Skip it by trusting the indicator marker.
    # Only a caller asking for the canonical columns (span_scale 1.0, EMAs
    # 9/20) may take it: a caller asking for stretched spans or other EMA
    # spans must (re)compute, because it has no way to tell from the column
    # names whether existing columns already carry what it wants.
    #
    # Note what this does NOT do: a frame stretched upstream keeps its stretched
    # columns here even though the default arguments ask for canonical ones.
    # Read INDICATOR_SPAN_SCALE_ATTR (via indicator_span_scale) rather than
    # assuming the returned frame is native.
    canonical = span_scale == 1.0 and resolve_ema_spans(1.0, ema_spans) == (9, 20)
    if canonical and frame is not None and not frame.empty and has_standard_indicator_columns(frame):
        return frame
    cleaned = ensure_ohlcv_frame(frame)
    if cleaned.empty:
        return cleaned
    if canonical and has_standard_indicator_columns(cleaned):
        return cleaned
    return add_indicators(cleaned, span_scale=span_scale, ema_spans=ema_spans)


FloatArray = npt.NDArray[np.float64]

# ``add_indicators`` stamps the span_scale it applied onto its output frame.
# Column NAMES keep their nominal suffix while the EFFECTIVE span is
# ``suffix x span_scale``, so a consumer that wants to recompute an indicator
# at a DIFFERENT length has to know the scale or it will silently compute a
# native-timeframe indicator where every shared column is stretched. Without
# this, `adx_length: 14` read a 70-period ADX off the frame while
# `adx_length: 15` computed a true 15-period one — a one-digit config change
# swapping the lookback by 5x with nothing to warn on.
INDICATOR_SPAN_SCALE_ATTR = "indicator_span_scale"


def scaled_span(base: int, span_scale: float) -> int:
    """Stretch a nominal bar-count lookback by ``span_scale``.

    The single definition of that arithmetic: ``add_indicators`` builds its
    columns with it and every consumer recomputing an indicator at another
    length must use the same rounding, or the two paths disagree by a bar.
    """
    return max(1, int(round(int(base) * float(span_scale))))


def resolve_ema_spans(span_scale: float, ema_spans: tuple[int, int] | None = None) -> tuple[int, int]:
    """The (fast, slow) spans of the ema9 / ema20 columns: ``ema_spans`` when
    given, else the nominal 9 / 20 stretched by ``span_scale``."""
    if ema_spans is None:
        return scaled_span(9, span_scale), scaled_span(20, span_scale)
    fast, slow = ema_spans
    return int(fast), int(slow)


def ltf_ema_spans(params: Any) -> tuple[int, int]:
    """The EMA spans a strategy's LTF frame carries in its ema9 / ema20
    columns: ``ltf_ema_fast_span`` / ``ltf_ema_slow_span`` when declared,
    else 9 / 20 stretched by ``ltf_indicator_span_scale``. The strategy and
    the dashboard both resolve them here, so the chart draws the EMAs the
    strategy scores on.

    Until 2026-09-23 the only way to change them was
    ``ltf_indicator_span_scale``, which also moves ATR, ADX, RSI, Bollinger
    and the returns (and the stops and thresholds calibrated on them).
    """
    params = params if isinstance(params, Mapping) else {}
    scale = float(params.get("ltf_indicator_span_scale", 1.0))
    default_fast, default_slow = resolve_ema_spans(scale)
    spans = []
    for key, default in (("ltf_ema_fast_span", default_fast), ("ltf_ema_slow_span", default_slow)):
        raw = params.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) != int(raw) or int(raw) < 1:
            raise ValueError(f"{key} must be a whole number of bars >= 1, got {raw!r}")
        spans.append(int(raw))
    fast, slow = spans
    if fast >= slow:
        raise ValueError(f"ltf_ema_fast_span ({fast}) must be shorter than ltf_ema_slow_span ({slow})")
    return fast, slow


def indicator_span_scale(frame: pd.DataFrame | None) -> float:
    """The span_scale ``frame``'s indicator columns were built with.

    Returns 1.0 for a frame that did not come from ``add_indicators`` — that
    is the honest answer (nothing was stretched), not a fallback: such a
    frame has no stretched columns to disagree with.
    """
    if frame is None:
        return 1.0
    attrs = getattr(frame, "attrs", None) or {}
    try:
        scale = float(attrs.get(INDICATOR_SPAN_SCALE_ATTR, 1.0))
    except (TypeError, ValueError):
        return 1.0
    return scale if math.isfinite(scale) and scale > 0.0 else 1.0


def _to_float64_array(series: pd.Series) -> FloatArray:
    return np.asarray(series.to_numpy(dtype=np.float64), dtype=np.float64)


def _series_from_talib(index: pd.Index, values: Any) -> pd.Series:
    return pd.Series(np.asarray(values, dtype=np.float64), index=index, dtype=float)


def _require_talib() -> Any:
    if talib is None:
        raise RuntimeError("TA-Lib is required for indicator calculation but is not installed")
    return talib


def talib_ema(series: pd.Series, span: int) -> pd.Series:
    """EMA via TA-Lib. Preserves the input's index on the returned series."""
    ta = _require_talib()
    return _series_from_talib(series.index, ta.EMA(_to_float64_array(series), timeperiod=int(span)))


def talib_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """OBV via TA-Lib.

    Numerically identical to ``cumsum(direction × volume)``: each bar adds
    ``+volume`` when ``close > prior close``, ``-volume`` when
    ``close < prior close``, and zero on a tie. The index of ``close`` is
    preserved on the returned series. The caller should fill volume NaNs
    before calling (TA-Lib treats NaN volume as a propagation source).
    """
    ta = _require_talib()
    return _series_from_talib(
        close.index,
        ta.OBV(_to_float64_array(close), _to_float64_array(volume)),
    )


def _session_stitch_factor(
    open_: FloatArray,
    close: FloatArray,
    day_ns: npt.NDArray[np.int64],
) -> FloatArray:
    """Per-bar multiplier that stitches consecutive sessions into one series
    with the overnight gaps taken out.

    ``open_``, ``close`` and ``day_ns`` (each bar's session day) describe the
    session bars only, in time order. Every bar of a session is multiplied by
    the product of the gap ratios (next session's first open / this session's
    last close) of all LATER sessions, so each session's last close lands
    exactly on the next session's first open and the latest session keeps
    factor 1. The first bar of a session then has true range high - low and
    an open-to-close change, as it had in the today-only overlay this
    replaced: a whole overnight gap inside one 1m bar's true range would
    otherwise inflate the 1m ATR (stops, sizing) for the first hour.

    Multiplicative, not additive: an additive shift after a large gap-down
    can drive older prices negative. Every indicator built on the stitched
    series is either scale-invariant (RSI, DI/ADX, returns, %B, z-score, OBV
    direction) or linear in price (ATR, band levels), and dividing a linear
    one by its bar's own factor yields exactly what the stitch gives when
    that bar's session is the latest, so past bars keep their values when a
    new session opens.
    """
    n = len(close)
    starts = np.flatnonzero(np.r_[True, day_ns[1:] != day_ns[:-1]])
    ratios = np.ones(len(starts), dtype=np.float64)
    ratios[1:] = open_[starts[1:]] / close[starts[1:] - 1]
    later = np.r_[np.cumprod(ratios[::-1])[::-1][1:], 1.0]
    return np.repeat(later, np.diff(np.r_[starts, n]))


def session_price_scale(frame: pd.DataFrame) -> npt.NDArray[np.float64]:
    """Per-bar multiplier that puts each bar's prices on the scale the
    session TA-Lib columns were computed on: the gap-free stitch
    (``_session_stitch_factor``) on session bars, 1.0 on the others (which
    carry the all-hours series) and everywhere while session indicators are
    off. Same mask and factor as ``add_indicators``, so ``price * scale``
    at a session bar is the price its rsi14 / obv were computed from.

    Anything that compares prices ACROSS sessions against those indicators
    (divergence pivots) must compare on this scale: raw, an overnight gap
    alone reads as a higher high or lower low that the gap-free RSI never
    saw. Only ratios between bars matter, and a ratio depends only on the
    gaps between them, so any frame holding both bars gives the same one.
    """
    scale = np.ones(len(frame), dtype=np.float64)
    if frame.empty or not get_runtime_indicator_mode():
        return scale
    index_dt = pd.DatetimeIndex(frame.index)
    pos = np.flatnonzero(indicator_session_mask(index_dt))
    if len(pos):
        scale[pos] = _session_stitch_factor(
            _to_float64_array(frame["open"])[pos],
            _to_float64_array(frame["close"])[pos],
            index_dt.normalize().asi8[pos],
        )
    return scale


def indicator_session_mask(index: pd.Index) -> npt.NDArray[np.bool_]:
    """The bars ``add_indicators`` treats as session bars: RTH (09:30-16:00,
    13:00 on an early close) by default, the 07:00-20:00 equity stream window
    when ``equity_session_indicator_window`` is "extended"; a weekend or
    holiday holds none (``sessions.session_mask``).

    They anchor the per-session VWAP/EMA reset and, with
    ``use_rth_session_indicators`` on, carry the session-only TA-Lib series;
    the other bars carry the all-hours one. A consumer comparing indicator
    values across bars (divergence pivots) needs this mask to keep to one
    series.
    """
    return session_mask(index, get_session_indicator_window())


def indicator_session_open() -> bool:
    """Whether a reader is inside the session right now: session indicators
    are on and the clock (``sessions.now_et``) is inside their window (RTH, or
    07:00-20:00 under "extended").

    Readers that switch to the session-only series in the session and keep
    the all-hours one outside it (``latest_atr14``, the divergence age in
    the technical and HTF builders) gate on this, not on their frame's last
    bar: an HTF frame's last completed bucket is still a premarket one until
    09:45 (15m) or 10:30 (60m), and a reader already in the session must
    not read it as a premarket reader would (2026-09-24).
    """
    return get_runtime_indicator_mode() and bool(
        session_mask(pd.DatetimeIndex([sessions.now_et()]), get_session_indicator_window())[0])


def latest_atr14(frame: pd.DataFrame) -> float | None:
    """The frame's current ``atr14``: at its latest bar, or at its latest
    SESSION bar while session indicators are on and the clock is inside the
    session. None when the frame has no ``atr14`` value.

    With session indicators on, atr14 is the session-only series on session
    bars and the all-hours one elsewhere (``add_indicators``). An RTH reader
    whose frame ends outside the session must not take the thin all-hours
    value: from 09:30 to 09:44 the 15m frame's last completed bar is the
    09:15 premarket bucket, whose all-hours atr14 ran a median 0.82x (p10
    0.57x) of the session ATR it switches to at 09:45 (2026-09-23). A
    premarket reader keeps the all-hours value its own bars carry, not
    yesterday's close.
    """
    if frame is None or frame.empty or "atr14" not in frame.columns:
        return None
    series = frame["atr14"]
    if indicator_session_open():
        in_session = indicator_session_mask(frame.index)
        if in_session.any():
            series = series[in_session]
    clean = series.dropna()
    return float(clean.iloc[-1]) if not clean.empty else None


def atr_with_floor(
    frame: pd.DataFrame | None,
    price: float,
    *,
    floor_pct: float = 0.0015,
    abs_floor: float = 0.0,
) -> float:
    """The frame's current ATR (``latest_atr14``: session-aware, last non-NaN),
    floored at ``price * floor_pct`` and ``abs_floor``. A missing, all-NaN or
    zero ATR falls to the floors; a price that is not positive (or is NaN)
    adds no percent floor. Computes no indicators: a frame without ``atr14``
    gets the floors.

    Every level builder sizes its ATR multiples through this helper with its
    own floor base (LV-6, 2026-09-26): support_resistance and technical_levels
    pass the frame's last close with no absolute floor; htf_levels (context
    and fair-value gaps) and order_blocks pass the live price with
    ``abs_floor=0.01``. Until then those three used the raw ATR and floored
    only a missing one, so on a quiet name (ATR under 0.15% of price) the
    HTF atr14 and the order-block thrust ran below the S/R floor.
    """
    return max(latest_atr14(frame) or 0.0, price * floor_pct if price > 0 else 0.0, abs_floor)


def last_bar_atr(
    frame: pd.DataFrame | None,
    close: float,
    *,
    fallback_pct: float = 0.0015,
    floor_pct: float | None = None,
    floor_abs: float = 0.01,
    fallback_atr: float | None = None,
) -> float:
    """The ``atr14`` on ``frame``'s last bar: the ATR a strategy sizes its
    stop clamps, buffers and extension gates with (ST-5, 2026-09-26).

    No reading -- no frame, an empty one, no ``atr14`` column, or a NaN or
    infinite last value (fewer than 15 bars of warm-up) -- falls back to
    ``fallback_atr`` when that is a finite positive number (a context's
    ATR), else to ``max(close * fallback_pct, floor_abs)``. With
    ``floor_pct``, the result is floored at ``max(close * floor_pct,
    floor_abs)``; without it a reading is used as read.

    Not ``latest_atr14``: that one is session-aware and skips back past NaN
    to an older bar; a strategy reads the bar it decides on. Until
    2026-09-26 ``BaseStrategy._frame_atr14`` (the refinement clamp, the
    retest anchor, the technical exit buffer, the divergence ladder) read a
    NaN as ``close * 0.0015`` with no $0.01 floor: under a cent below $6.67,
    while a missing column there already read as $0.01.
    """
    atr = None
    if frame is not None and not frame.empty and "atr14" in frame.columns:
        atr = safe_float(frame["atr14"].iloc[-1], finite=True)
    if atr is None:
        context_atr = safe_float(fallback_atr, finite=True)
        atr = context_atr if context_atr is not None and context_atr > 0 else max(close * fallback_pct, floor_abs)
    if floor_pct is not None:
        atr = max(atr, max(close * floor_pct, floor_abs))
    return atr


def bar_posture(last: Mapping[str, Any] | pd.Series, reference: float | None = None) -> Side | None:
    """Which way ``last`` leans: LONG when its close is above ``reference``
    and EMA9 >= EMA20, SHORT when the close is below it and EMA9 <= EMA20,
    else None. ``reference`` defaults to the bar's VWAP. A missing VWAP or
    EMA stands in as the close; a bar with no close has no posture."""
    close = safe_float(last.get("close"))
    if close is None:
        return None
    level = safe_float(last.get("vwap"), close) if reference is None else float(reference)
    ema9 = safe_float(last.get("ema9"), close)
    ema20 = safe_float(last.get("ema20"), close)
    if close > level and ema9 >= ema20:
        return Side.LONG
    if close < level and ema9 <= ema20:
        return Side.SHORT
    return None


def htf_ema_spans(params: Any) -> tuple[int, int]:
    """The (fast, slow) EMA spans of a strategy's HTF context:
    ``htf_ema_fast_span`` / ``htf_ema_slow_span``, default 50 / 200. Every
    consumer -- the strategy's HTF contexts, its prefetch, the dashboard's HTF
    chart and level zones -- resolves them here, so none of them can fall
    back to a different default than the others (until 2026-09-24 they fell
    back to 50/200, 34/200 and 9/20)."""
    params = params if isinstance(params, Mapping) else {}
    spans = []
    for key, default in (("htf_ema_fast_span", 50), ("htf_ema_slow_span", 200)):
        raw = params.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) != int(raw) or int(raw) < 1:
            raise ValueError(f"{key} must be a whole number of bars >= 1, got {raw!r}")
        spans.append(int(raw))
    fast, slow = spans
    if fast >= slow:
        raise ValueError(f"htf_ema_fast_span ({fast}) must be shorter than htf_ema_slow_span ({slow})")
    return fast, slow


def add_indicators(
    frame: pd.DataFrame,
    *,
    span_scale: float = 1.0,
    ema_spans: tuple[int, int] | None = None,
) -> pd.DataFrame:
    # Reject a nonsensical scale here, where the offending value is still in
    # hand. Every span collapses to max(1, ...) below, so a zero or negative
    # scale used to surface as "TA_BBANDS function failed with error code 2:
    # Bad Parameter" from inside TA-Lib, which names neither span_scale nor
    # the config key (`ltf_indicator_span_scale`) that set it.
    try:
        span_scale = float(span_scale)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"span_scale must be a number, got {span_scale!r}") from exc
    if not math.isfinite(span_scale) or span_scale <= 0.0:
        raise ValueError(
            f"span_scale must be a positive finite number, got {span_scale!r} "
            "(check the strategy's ltf_indicator_span_scale)"
        )
    frame = ensure_ohlcv_frame(frame)
    if frame.empty:
        return frame
    out = frame.copy()
    # Stamp the scale before anything else so every return path carries it and
    # a consumer can tell a stretched frame from a native one. Set
    # unconditionally, including at 1.0: a frame resampled from a stretched one
    # inherits its attrs through pandas' __finalize__, so only an unconditional
    # write clears a stale scale when the indicators are rebuilt natively.
    out.attrs[INDICATOR_SPAN_SCALE_ATTR] = float(span_scale)
    ta = _require_talib()
    # ``span_scale`` stretches every bar-count lookback so a finer timeframe can
    # preserve a coarser timeframe's wall-clock horizon. Default 1.0 = the
    # canonical 9/20/14/5/15-bar spans (byte-for-byte unchanged for every
    # existing caller). top_tier_adaptive's 1m LTF passes span_scale=5 so its
    # indicators behave like the old 5m frame (ema9->45, ema20->100, atr14->70,
    # rsi14->70, bb20->100, ret5->25, ret15->75). The column NAMES keep their
    # nominal numeric suffix; the EFFECTIVE span is suffix x span_scale.
    # ``ema_spans`` sets the ema9 / ema20 spans on their own (a strategy's
    # ltf_ema_fast_span / ltf_ema_slow_span); nothing else follows it, and
    # obv_ema20 keeps its own span.
    def _span(base: int) -> int:
        return scaled_span(base, span_scale)
    ema_fast_span, ema_slow_span = resolve_ema_spans(span_scale, ema_spans)
    bb_length = _span(20)
    bb_warmup_min = max(2, bb_length // 2)
    atr_period = _span(14)
    di_period = _span(14)
    obv_ema_span = _span(20)
    obv_delta_period = _span(5)
    rsi_period = _span(14)
    ret_fast_period = _span(5)
    ret_slow_period = _span(15)
    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    volume = out["volume"].fillna(0.0).astype(float)

    session_keys = pd.Index(out.index.map(lambda ts: ts.date()), name="session_date")
    tpv = ((high + low + close) / 3.0) * volume
    cum_vol = volume.groupby(session_keys).cumsum().replace(0, math.nan)
    cum_tpv = tpv.groupby(session_keys).cumsum()
    out["vwap_all"] = cum_tpv / cum_vol
    out["ema9_all"] = talib_ema(close, span=ema_fast_span)
    out["ema20_all"] = talib_ema(close, span=ema_slow_span)

    index_dt = pd.DatetimeIndex(out.index)
    # Session mask for the per-session VWAP/EMA reset and the TA-Lib session
    # overlay below. Variable name kept as rth_mask — it is the "session"
    # mask downstream regardless of which window defines it.
    rth_mask = pd.Series(indicator_session_mask(index_dt), index=out.index, dtype=bool)
    rth_volume = volume.where(rth_mask, 0.0)
    rth_tpv = tpv.where(rth_mask, 0.0)
    rth_cum_vol = rth_volume.groupby(session_keys).cumsum().replace(0, math.nan)
    rth_cum_tpv = rth_tpv.groupby(session_keys).cumsum()
    out["vwap_rth"] = rth_cum_tpv / rth_cum_vol

    def _session_rth_ema(series: pd.Series, span: int) -> pd.Series:
        result = pd.Series(math.nan, index=series.index, dtype=float)
        grouped = pd.Series(session_keys, index=series.index)
        for _, idx in grouped.groupby(grouped).groups.items():
            session_series = series.loc[idx]
            session_mask = rth_mask.loc[idx]
            session_rth = session_series.loc[session_mask]
            if session_rth.empty:
                continue
            # Keep the session-reset EMA path aligned with the bot's historical behavior:
            # reset on the first RTH bar of each session and produce values immediately,
            # instead of inheriting TA-Lib's leading-lookback NaNs for this custom signal EMA.
            result.loc[session_rth.index] = session_rth.astype(float).ewm(span=int(span), adjust=False).mean()
        return result

    out["ema9_rth"] = _session_rth_ema(close, span=ema_fast_span)
    out["ema20_rth"] = _session_rth_ema(close, span=ema_slow_span)
    rth_only_vwap = out["vwap_rth"].combine_first(out["vwap_all"])
    rth_only_ema9 = out["ema9_rth"].combine_first(out["ema9_all"])
    rth_only_ema20 = out["ema20_rth"].combine_first(out["ema20_all"])
    out["vwap_signal"] = out["vwap_all"].where(~rth_mask, rth_only_vwap)
    out["ema9_signal"] = out["ema9_all"].where(~rth_mask, rth_only_ema9)
    out["ema20_signal"] = out["ema20_all"].where(~rth_mask, rth_only_ema20)
    if get_runtime_indicator_mode():
        out["vwap"] = out["vwap_signal"]
        out["ema9"] = out["ema9_signal"]
        out["ema20"] = out["ema20_signal"]
    else:
        out["vwap"] = out["vwap_all"]
        out["ema9"] = out["ema9_all"]
        out["ema20"] = out["ema20_all"]

    # --- All-hours TA-Lib indicators (always computed) ---
    # Bollinger Bands: TA-Lib's BBANDS uses a strict 20-bar warmup and
    # returns NaN for bars 0-18 of the input. On a fresh session (no
    # carry-over from prior days) that leaves the first 19 minutes of
    # today's chart without a visible BB line. Fill the leading NaNs
    # with a min_periods=10 pandas computation — same semantic as the
    # ``technical_levels.py:813-814`` fallback path used by the strategy
    # when shared bb_* columns aren't available. After bar 19 the values
    # match TA-Lib exactly (full 20-bar window); before that they use
    # whatever bars are available, with the std dev floor at 10 samples
    # to keep the band statistically meaningful.
    upper, middle, lower_band = ta.BBANDS(
        _to_float64_array(close),
        timeperiod=bb_length,
        nbdevup=2.0,
        nbdevdn=2.0,
        matype=ta.MA_Type.SMA,
    )
    out["bb_mid"] = _series_from_talib(out.index, middle)
    out["bb_upper"] = _series_from_talib(out.index, upper)
    out["bb_lower"] = _series_from_talib(out.index, lower_band)
    bb_warmup_mid = close.rolling(bb_length, min_periods=bb_warmup_min).mean()
    bb_warmup_std = close.rolling(bb_length, min_periods=bb_warmup_min).std(ddof=0)
    bb_warmup_upper = bb_warmup_mid + 2.0 * bb_warmup_std
    bb_warmup_lower = bb_warmup_mid - 2.0 * bb_warmup_std
    out["bb_mid"] = out["bb_mid"].fillna(bb_warmup_mid)
    out["bb_upper"] = out["bb_upper"].fillna(bb_warmup_upper)
    out["bb_lower"] = out["bb_lower"].fillna(bb_warmup_lower)
    out["bb_width"] = out["bb_upper"] - out["bb_lower"]
    out["bb_width_pct"] = out["bb_width"] / out["bb_mid"].replace(0.0, math.nan)
    out["bb_percent_b"] = (close - out["bb_lower"]) / out["bb_width"].replace(0.0, math.nan)
    out["bb_zscore"] = (close - out["bb_mid"]) / bb_warmup_std.replace(0.0, math.nan)

    out["atr14"] = _series_from_talib(out.index, ta.ATR(_to_float64_array(high), _to_float64_array(low), _to_float64_array(close), timeperiod=atr_period))
    out["plus_di14"] = _series_from_talib(out.index, ta.PLUS_DI(_to_float64_array(high), _to_float64_array(low), _to_float64_array(close), timeperiod=di_period))
    out["minus_di14"] = _series_from_talib(out.index, ta.MINUS_DI(_to_float64_array(high), _to_float64_array(low), _to_float64_array(close), timeperiod=di_period))
    out["adx14"] = _series_from_talib(out.index, ta.ADX(_to_float64_array(high), _to_float64_array(low), _to_float64_array(close), timeperiod=di_period))

    out["obv"] = _series_from_talib(out.index, ta.OBV(_to_float64_array(close), _to_float64_array(volume)))
    out["obv_ema20"] = talib_ema(out["obv"], span=obv_ema_span)
    out["obv_delta5"] = out["obv"].diff(obv_delta_period)
    out["rsi14"] = _series_from_talib(out.index, ta.RSI(_to_float64_array(close), timeperiod=rsi_period))

    out["ret1"] = close.pct_change()
    out["ret5"] = close.pct_change(ret_fast_period)
    out["ret15"] = close.pct_change(ret_slow_period)

    # --- Session overlay for the TA-Lib indicators ---
    # When use_rth_session_indicators is enabled, every session bar (RTH, or
    # the 07:00-20:00 stream window in "extended" mode) carries indicators
    # computed over session bars ALONE, across every session in the frame,
    # with the overnight gaps stitched out (_session_stitch_factor). Non-
    # session bars keep the all-hours values above: chart display, and the
    # premarket reads of strategies that trade outside the session window.
    # The column therefore holds two series, and anything comparing values
    # ACROSS bars (divergence pivots) must keep to one (indicator_session_mask).
    #
    # Until 2026-09-23 this recomputed from TODAY's session bars only and
    # switched each indicator on once today held enough bars for its
    # lookback. Before the switch the column was the all-hours series, thinned
    # by quiet pre/post-market bars (the 15m S/R ATR ran ~0.8-0.9x a multi-day
    # RTH ATR all morning); at the switch it stepped (median x1.11 for the 15m
    # atr14 at the 13:15 decision, x1.26 for the 1m atr14 at 09:45, x1.32 for
    # the span-5 LTF atr70 at 10:41, over 17 archived sessions), moving every
    # ATR-denominated threshold with no change in the market. The
    # 15m rsi14 changed series mid-afternoon, so an HTF divergence pivot pair
    # straddling the switch compared two different RSIs, and obv sat on
    # today's RTH-only cumsum while obv_ema20 was still the all-hours EMA.
    # Seeded from prior sessions, the series has nothing left to warm up, so
    # nothing switches. Only a frame holding fewer session bars than a
    # lookback keeps all-hours values on those leading bars, where the
    # stitched series is still NaN.
    if get_runtime_indicator_mode():
        session_pos = np.flatnonzero(rth_mask.to_numpy())
        if len(session_pos):
            s_index = out.index[session_pos]
            factor = _session_stitch_factor(
                _to_float64_array(out["open"])[session_pos],
                _to_float64_array(close)[session_pos],
                index_dt.normalize().asi8[session_pos],
            )
            s_h = _to_float64_array(high)[session_pos] * factor
            s_l = _to_float64_array(low)[session_pos] * factor
            s_c = _to_float64_array(close)[session_pos] * factor
            s_close = pd.Series(s_c, index=s_index, dtype=float)
            # ATR and the band levels are linear in price: dividing by the
            # bar's factor puts them back on that bar's own price level.
            unscale = pd.Series(factor, index=s_index, dtype=float)

            def _overlay(columns: dict[str, pd.Series], anchor: pd.Series) -> None:
                """Write ``columns`` onto the session bars where ``anchor`` is
                valid. Columns read against each other (obv vs obv_ema20, the
                band family) share one anchor so no bar pairs a stitched value
                with an all-hours one."""
                valid = anchor.notna().to_numpy()
                pos = session_pos[valid]
                for col, series in columns.items():
                    values = out[col].to_numpy(dtype=np.float64, copy=True)
                    values[pos] = series.to_numpy(dtype=np.float64)[valid]
                    out[col] = values

            s_obv = _series_from_talib(s_index, ta.OBV(s_c, _to_float64_array(volume)[session_pos]))
            s_obv_ema = talib_ema(s_obv, span=obv_ema_span)
            s_plus_di = _series_from_talib(s_index, ta.PLUS_DI(s_h, s_l, s_c, timeperiod=di_period))
            s_minus_di = _series_from_talib(s_index, ta.MINUS_DI(s_h, s_l, s_c, timeperiod=di_period))
            # Returns are NOT overlaid. They are price-true momentum ("how far
            # did price move over the last N bars"), and on contiguous session
            # bars the all-hours pct_change already equals a session-only one;
            # at the open it measures against the real premarket prices, which
            # is what the old today-only overlay produced too. Stitching them
            # would compare today's opening bars with yesterday's close with the
            # gap divided out -- a move that never happened.
            for col, series in (
                ("obv_delta5", s_obv.diff(obv_delta_period)),
                ("atr14", _series_from_talib(s_index, ta.ATR(s_h, s_l, s_c, timeperiod=atr_period)) / unscale),
                ("adx14", _series_from_talib(s_index, ta.ADX(s_h, s_l, s_c, timeperiod=di_period))),
                ("rsi14", _series_from_talib(s_index, ta.RSI(s_c, timeperiod=rsi_period))),
            ):
                _overlay({col: series}, series)
            _overlay({"obv": s_obv, "obv_ema20": s_obv_ema}, s_obv_ema)
            _overlay({"plus_di14": s_plus_di, "minus_di14": s_minus_di}, s_plus_di)

            s_upper, s_middle, s_lower = ta.BBANDS(
                s_c, timeperiod=bb_length,
                nbdevup=2.0, nbdevdn=2.0, matype=ta.MA_Type.SMA,
            )
            s_bb_mid = _series_from_talib(s_index, s_middle)
            s_bb_upper = _series_from_talib(s_index, s_upper)
            s_bb_lower = _series_from_talib(s_index, s_lower)
            s_bb_width = s_bb_upper - s_bb_lower
            s_std = s_close.rolling(bb_length, min_periods=bb_warmup_min).std(ddof=0)
            _overlay(
                {
                    "bb_mid": s_bb_mid / unscale,
                    "bb_upper": s_bb_upper / unscale,
                    "bb_lower": s_bb_lower / unscale,
                    "bb_width": s_bb_width / unscale,
                    "bb_width_pct": s_bb_width / s_bb_mid.replace(0.0, math.nan),
                    "bb_percent_b": (s_close - s_bb_lower) / s_bb_width.replace(0.0, math.nan),
                    "bb_zscore": (s_close - s_bb_mid) / s_std.replace(0.0, math.nan),
                },
                s_bb_mid,
            )

    return out
