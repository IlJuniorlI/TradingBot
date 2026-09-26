# SPDX-License-Identifier: MIT
"""Bar frames: OHLCV normalization, the session bucket grid and resampling,
the equity stream-window slice, and the live-price read."""
import logging
import math
from datetime import datetime

import numpy as np
import pandas as pd

from .sessions import (
    EQUITY_PREMARKET_START,
    EQUITY_RTH_OPEN,
    EQUITY_STREAM_END,
    EQUITY_STREAM_START,
    EXCHANGE_TZ,
    rth_close_minute,
)


LOG = logging.getLogger(__name__)


def floor_minute(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.floor("1min")


def ensure_ohlcv_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    frame = df.copy()
    frame = frame.sort_index()
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in frame.columns:
            frame[col] = math.nan
        else:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    # Schwab's price_history endpoint can return the same minute twice when
    # called in dates-mode (explicit startDate/endDate). This is a
    # dates-mode artifact independent of needPreviousClose: variants
    # tested {needPC=true, needPC=false} both produced the same duplicate
    # pattern, while period-mode (periodType+period, no dates) returned
    # clean unique bars. The dupes are once from the consolidated NMS tape
    # and once from the full reportable tape — OHLC is identical between
    # the copies but volume differs by 0-4% (the full tape includes odd-lot
    # and off-exchange prints). Pick the higher-volume copy so
    # activity_score, rel_vol gates, and OBV-style indicators all see the
    # most complete print for each minute, instead of inheriting whichever
    # copy the original sort happened to land last.
    if frame.index.has_duplicates:
        frame = frame.sort_values("volume", ascending=False, kind="stable", na_position="last")
        frame = frame.sort_index(kind="stable")
        frame = frame[~frame.index.duplicated(keep="first")]
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    if frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    frame["volume"] = frame["volume"].fillna(0.0)
    return frame[["open", "high", "low", "close", "volume"] + [c for c in frame.columns if c not in {"open", "high", "low", "close", "volume"}]]


_SESSION_BIN_OFFSET = pd.Timedelta(hours=EQUITY_RTH_OPEN.hour, minutes=EQUITY_RTH_OPEN.minute)
_PREMARKET_OPEN_MINUTE = EQUITY_PREMARKET_START.hour * 60 + EQUITY_PREMARKET_START.minute
_RTH_OPEN_MINUTE = EQUITY_RTH_OPEN.hour * 60 + EQUITY_RTH_OPEN.minute
_STREAM_END_MINUTE = EQUITY_STREAM_END.hour * 60 + EQUITY_STREAM_END.minute


def _rule_minutes(rule: str) -> float:
    return pd.Timedelta(pd.tseries.frequencies.to_offset(rule)).total_seconds() / 60.0


def _on_the_open_grid(minutes: float) -> bool:
    """Whether ``minutes``-long buckets anchored on 09:30 already start on
    every session boundary (00:00, 04:00, 09:30, 13:00/16:00, 20:00): every
    length that divides 30 minutes."""
    return minutes > 0 and (30.0 / minutes).is_integer()


def session_bucket_bounds(index: pd.DatetimeIndex | pd.Index, minutes: float) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    """``(starts, ends)`` of the ``minutes``-long bucket each timestamp of
    ``index`` falls in, on the grid ``resample_bars`` aggregates to.

    Buckets are laid out from the start of each ET session segment -- 00:00,
    the 04:00 premarket, the 09:30 open, the close (16:00, 13:00 on an
    early-close day) and 20:00 -- and the last bucket of a segment ends at the
    next boundary. So no bar mixes regular-session and extended-hours prints:
    the 60m grid is 07:00, 08:00, 09:00 (to 09:30), 09:30 ... 15:30 (to
    16:00), 16:00 ... 19:00, as charting platforms draw it. Every length that
    divides 30 minutes is the plain 09:30-anchored grid, unchanged.

    Until 2026-09-23 every length ran on one 09:30 grid, so the 60m bar
    labelled 15:30 held 15:30-16:29: the post-market set 60m PDH/PDL and
    counted as regular-session in the session indicators, and the premarket
    bar labelled 06:30 held 07:00-07:29 under a label outside the stream
    window. The grid was also anchored on the frame's first day and stepped
    in absolute time, so a length that does not divide 60 drifted an hour off
    the local grid across a DST change.

    A tz-naive index is read as ET wall time. Offsets are taken in wall
    time within one segment, which a DST change (02:00) can only split in the
    00:00-04:00 overnight segment.
    """
    idx = pd.DatetimeIndex(index)
    step = float(minutes)
    if step <= 0:
        raise ValueError(f"bucket length must be positive, got {minutes!r}")
    if len(idx) == 0:
        return idx, idx
    step_ns = int(round(step * _MINUTE_NS))
    if _on_the_open_grid(step):
        offset = np.mod(_wall_ns(idx) - _RTH_OPEN_MINUTE * _MINUTE_NS, step_ns)
        starts = idx - pd.to_timedelta(offset, unit="ns")
        return starts, starts + pd.Timedelta(step_ns, unit="ns")
    wall, seg_start, seg_end = _session_segments(idx)
    bucket_start = seg_start + ((wall - seg_start) // step_ns) * step_ns
    bucket_end = np.minimum(bucket_start + step_ns, seg_end)
    starts = idx - pd.to_timedelta(wall - bucket_start, unit="ns")
    return starts, starts + pd.to_timedelta(bucket_end - bucket_start, unit="ns")


_MINUTE_NS = 60_000_000_000


def _wall_ns(idx: pd.DatetimeIndex) -> np.ndarray:
    """Each timestamp's New York wall-clock time of day, in
    nanoseconds. Read off the naive local clock: elapsed time since local
    midnight is an hour off the wall clock all day on a DST Sunday."""
    local = idx.tz_convert(EXCHANGE_TZ).tz_localize(None) if idx.tz is not None else idx
    return (local - local.normalize()).to_numpy(dtype="timedelta64[ns]").astype(np.int64)


def _session_segments(idx: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per timestamp, in integer nanoseconds since its local midnight: the
    wall time, and the start and end of the session segment holding it
    (``session_bucket_bounds``). Integers throughout: float minutes lose the
    sub-second part of a clock reading (11:00:10 floored to 10:29:59.999...).
    """
    minute = _MINUTE_NS
    local = idx.tz_convert(EXCHANGE_TZ) if idx.tz is not None else idx
    wall = _wall_ns(idx)
    close = rth_close_minute(local) * minute
    # Each row's segment boundaries; a bar's segment runs from the last one
    # at or before it to the first one after it.
    bounds = np.column_stack([
        np.zeros(len(idx), dtype=np.int64),
        np.full(len(idx), _PREMARKET_OPEN_MINUTE * minute, dtype=np.int64),
        np.full(len(idx), _RTH_OPEN_MINUTE * minute, dtype=np.int64),
        close,
        np.full(len(idx), _STREAM_END_MINUTE * minute, dtype=np.int64),
        np.full(len(idx), 1440 * minute, dtype=np.int64),
    ])
    at_or_before = bounds <= wall[:, None]
    seg_start = np.where(at_or_before, bounds, np.iinfo(np.int64).min).max(axis=1)
    seg_end = np.where(at_or_before, np.iinfo(np.int64).max, bounds).min(axis=1)
    return wall, seg_start, seg_end


def resample_bars(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate start-labelled bars into ``rule`` bars, labelled at their START.

    The same convention as the source bars (Schwab price_history candles,
    CHART_EQUITY) and the broker's own coarser bars, so every consumer reads
    a timestamp the same way whatever frame it came from: bar ``T`` holds the
    source bars starting in ``[T, end)`` where ``end`` is
    ``session_bucket_bounds``' bucket end (``T + rule`` except for a
    segment's last, shorter bucket). Buckets are laid out per ET session
    segment (see ``session_bucket_bounds``): identical to clock buckets for
    every rule that divides 30 minutes (5/15/30m match the broker's bars), and
    for 60m 09:30, 10:30, ... 15:30 (to 16:00) in the regular session.

    Until 2026-09-22 this ran ``closed="right", label="right"``: the source
    bar starting AT the label joined the bucket, so the 5m bar labelled 09:35
    held 09:31-09:35 and the one labelled 09:30 folded four premarket
    minutes into the opening minute (10,162 premarket shares on AAPL
    2026-09-21). And because everything downstream -- the RTH mask behind
    session VWAP/EMA, session-open and same-day helpers, the ORB
    follow-through gate -- reads a timestamp as a bar START, an end label
    counted a premarket bar as RTH and dropped the last RTH bar.
    """
    frame = ensure_ohlcv_frame(frame)
    if frame.empty:
        return frame
    agg_spec = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    minutes = _rule_minutes(rule)
    if _on_the_open_grid(minutes):
        agg = frame.resample(rule, label="left", closed="left", origin="start_day", offset=_SESSION_BIN_OFFSET).agg(agg_spec)
    else:
        starts, _ends = session_bucket_bounds(frame.index, minutes)
        agg = frame[list(agg_spec)].groupby(starts).agg(agg_spec)
        agg.index = pd.DatetimeIndex(agg.index, name=frame.index.name)
    return agg.dropna(subset=["open", "high", "low", "close"])


def session_bucket_floor(ts: datetime | pd.Timestamp, minutes: int) -> pd.Timestamp:
    """Start of the ``minutes`` bucket holding ``ts`` on ``resample_bars``'
    grid (``session_bucket_bounds``). A clock floor put regular-session 60m
    boundaries on the hour, half a bar from the XX:30 bars it gates."""
    starts, _ends = session_bucket_bounds(pd.DatetimeIndex([pd.Timestamp(ts)]), max(1, int(minutes)))
    return starts[0]


def session_bucket_ends(index: pd.DatetimeIndex | pd.Index, minutes: int) -> pd.DatetimeIndex:
    """When each bar of a ``minutes`` frame labelled at ``index`` completes:
    ``T + minutes``, or the session boundary that cuts a segment's last
    bucket short (a 60m bar labelled 15:30 is complete at 16:00). Read from
    the label itself, so a bar off ``resample_bars``' grid (a clock-aligned
    11:00 60m bar) still ends an hour after it starts."""
    idx = pd.DatetimeIndex(index)
    length = max(1, int(minutes))
    if len(idx) == 0 or _on_the_open_grid(length):
        return idx + pd.Timedelta(minutes=length)
    wall, _seg_start, seg_end = _session_segments(idx)
    return idx + pd.to_timedelta(np.minimum(seg_end - wall, length * _MINUTE_NS), unit="ns")


def frame_bar_minutes(index: pd.DatetimeIndex | pd.Index) -> int:
    """Bar length, in whole minutes, of a frame labelled at ``index``: its
    smallest positive label step. The smallest, not the typical one: a thin
    name prints no bar for minutes at a time (2-13% of extended-hours 1m
    steps run longer than 2 minutes), and reading such a 1m frame as 2m would
    call its completed last bar still forming. Any two consecutive minutes in
    the frame give 1. With no step to read (fewer than two labels) it is the
    canonical 1m stream.

    A step into a label that opens a session segment (09:30, the close,
    20:00; ``session_bucket_bounds``) is left out: the bucket before it is
    its segment's last, which the grid cuts short. A native 60m frame steps
    09:00 -> 09:30 and 15:30 -> 16:00, so the plain smallest step would read
    it as 30m and call a forming bucket complete halfway through
    (2026-09-25). When every step is such a step, they all count."""
    idx = pd.DatetimeIndex(index)
    if len(idx) < 2:
        return 1
    steps = np.asarray((idx[1:] - idx[:-1]).total_seconds())
    positive = np.unique(steps[steps > 0])
    if len(positive) == 0:
        return 1
    # A day has five segment opens, so a step length more steps share than
    # the frame's days allow cannot be only steps into one. The segment read
    # runs only on the few labels left: over a whole 1m frame it cost ~3 ms
    # per structure build.
    max_opens = 5 * ((idx[-1] - idx[0]).days + 2)
    for step in positive:
        at_step = steps == step
        if int(at_step.sum()) > max_opens:
            return max(1, int(round(float(step) / 60.0)))
        wall, seg_start, _seg_end = _session_segments(idx[1:][at_step])
        if bool((wall != seg_start).any()):
            return max(1, int(round(float(step) / 60.0)))
    return max(1, int(round(float(positive[0]) / 60.0)))


def equity_stream_window_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """The bars of ``frame`` that start inside the 07:00-20:00 equity
    stream window.

    HTF frames are fetched with extended hours, and Schwab's price_history
    lags about a trading day on overnight (20:00-07:00) bars: every older
    night came back, the latest never did, and a full refetch after a
    restart brought back a night an incremental run never had. So PDH/PDL,
    pivots, ATR and the HTF chart all depended on which nights happened to be
    in the frame. Since 2026-09-23 no overnight bar is ever stored.

    Apply it to SOURCE bars, before ``resample_bars``: a bucket that
    straddles 07:00 (a 120m bucket from 06:00) holds in-window bars under a
    label outside the window, so windowing the buckets would drop them.
    """
    if frame.empty:
        return frame
    local = pd.DatetimeIndex(frame.index).tz_convert(EXCHANGE_TZ)
    minute_of_day = local.hour * 60 + local.minute
    window_open = EQUITY_STREAM_START.hour * 60 + EQUITY_STREAM_START.minute
    window_close = EQUITY_STREAM_END.hour * 60 + EQUITY_STREAM_END.minute
    return frame[(minute_of_day >= window_open) & (minute_of_day < window_close)]


def resolve_current_price(
    frame: pd.DataFrame | None,
    current_price: float | None,
    *,
    context: str = "",
) -> float:
    if current_price is not None:
        try:
            value = float(current_price)
            if value > 0.0 and pd.notna(value):
                return value
        except Exception:
            label = f" {context}" if context else ""
            LOG.debug(
                "Failed to coerce%s current_price override; falling back to frame-derived price.",
                label,
                exc_info=True,
            )
    if frame is None or frame.empty:
        return 0.0
    try:
        last = frame.iloc[-1]
        last_close = float(last.get("close", 0.0) if hasattr(last, "get") else last.close)
    except Exception:
        last_close = 0.0
    return last_close if pd.notna(last_close) and last_close > 0.0 else 0.0
