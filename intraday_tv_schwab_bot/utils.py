# SPDX-License-Identifier: MIT
import logging
import math
import os
import sys
import time as _monotonic_time
from collections.abc import Mapping
from datetime import date as date_cls, datetime, time, timedelta
from pathlib import Path
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional, Union

import numpy.typing as npt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    import talib  # type: ignore
except Exception:  # pragma: no cover - optional until indicators are computed
    talib = None

from .models import DEFAULT_RUNTIME_TZ, StrategySchedule, Window


LOG = logging.getLogger(__name__)

_USE_RTH_SESSION_INDICATORS = True
# Which session window the per-session indicator reset (VWAP/EMA/TA-Lib
# overlay) keys off when _USE_RTH_SESSION_INDICATORS is on. "rth" = the
# 09:30-16:00 regular session (default, original behavior). "extended" =
# the 07:00-20:00 equity stream window, for strategies that trade pre/post
# market. Only affects the session mask predicate in add_indicators.
_SESSION_INDICATOR_WINDOW = "rth"

TRADEFLOW_LEVEL = 25
TRADEFLOW = TRADEFLOW_LEVEL


def register_tradeflow_logging_level() -> None:
    if logging.getLevelName(TRADEFLOW_LEVEL) != 'TRADEFLOW':
        logging.addLevelName(TRADEFLOW_LEVEL, 'TRADEFLOW')
    if not hasattr(logging, 'TRADEFLOW'):
        setattr(logging, 'TRADEFLOW', TRADEFLOW_LEVEL)

    current = getattr(logging.Logger, 'tradeflow', None)
    if callable(current):
        return

    def tradeflow(
        self: logging.Logger,
        message: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if self.isEnabledFor(TRADEFLOW_LEVEL):
            self._log(TRADEFLOW_LEVEL, message, args, **kwargs)

    setattr(logging.Logger, 'tradeflow', tradeflow)


register_tradeflow_logging_level()


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


def _enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        get_std_handle = getattr(kernel32, "GetStdHandle", None)
        get_console_mode = getattr(kernel32, "GetConsoleMode", None)
        set_console_mode = getattr(kernel32, "SetConsoleMode", None)
        if (
            not callable(get_std_handle)
            or not callable(get_console_mode)
            or not callable(set_console_mode)
        ):
            return
        handle = get_std_handle(-11)
        if not handle:
            return
        mode = ctypes.c_uint32()
        if get_console_mode(handle, ctypes.byref(mode)) == 0:
            return
        set_console_mode(handle, mode.value | 0x0004)
    except Exception:
        return


def _console_supports_color(stream: Any) -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("FORCE_COLOR"):
        return True
    is_tty = bool(getattr(stream, "isatty", lambda: False)())
    if not is_tty:
        return False
    term = str(os.getenv("TERM", "")).lower()
    if term == "dumb":
        return False
    return True


class ColorConsoleFormatter(logging.Formatter):
    RESET = "\033[0m"
    DIM = "\033[2m"
    LEVEL_COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[37m",
        TRADEFLOW_LEVEL: "\033[95m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[41;97m",
    }

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        color = self._pick_color(record)
        return f"{color}{base}{self.RESET}" if color else base

    def _pick_color(self, record: logging.LogRecord) -> str:
        message = record.getMessage().lower()
        if record.levelno >= logging.ERROR:
            return self.LEVEL_COLORS[logging.ERROR]
        if record.levelno == logging.WARNING:
            return self.LEVEL_COLORS[logging.WARNING]
        if "paper account entry recorded" in message or " action=entered" in message:
            return "\033[32m"
        if "paper account exit recorded" in message:
            return "\033[35m"
        if "entry_retry_backoff" in message or "cooldown" in message or "underlying_already_open" in message:
            return "\033[90m"
        if " not_filled" in message or "option entry attempt" in message or "exit attempt" in message:
            return "\033[33m"
        if "starting bot" in message or "dashboard listening" in message:
            return "\033[96m"
        if "candidate cycle" in message or "entry cycle" in message:
            return "\033[94m"
        if "fetching price_history" in message or "quote refresh" in message:
            return self.DIM
        return self.LEVEL_COLORS.get(record.levelno, "")

_RUNTIME_TZ_NAME = DEFAULT_RUNTIME_TZ
_RUNTIME_TZ = ZoneInfo(_RUNTIME_TZ_NAME)
UTC = ZoneInfo("UTC")


def set_runtime_timezone(name: Optional[str]) -> None:
    global _RUNTIME_TZ_NAME, _RUNTIME_TZ
    tz_name = str(name or DEFAULT_RUNTIME_TZ)
    _RUNTIME_TZ_NAME = tz_name
    _RUNTIME_TZ = ZoneInfo(tz_name)


def get_runtime_timezone_name() -> str:
    return _RUNTIME_TZ_NAME


def parse_hhmm(value: object) -> time:
    """Parse common config time encodings into a ``datetime.time``.

    Accepts canonical ``"HH:MM"`` strings, ``datetime.time`` objects, and
    integer values that can appear when YAML parses unquoted ``HH:MM`` as
    sexagesimal minutes (for example ``14:15`` -> ``855``).
    """
    if isinstance(value, time):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ivalue = int(value)
        if 0 <= ivalue < 24 * 60:
            return time(hour=ivalue // 60, minute=ivalue % 60)
        raise ValueError(f"Invalid HH:MM numeric value: {value!r}")
    text = str(value).strip()
    hh, mm = text.split(":", 1)
    return time(hour=int(hh), minute=int(mm))


def now_et() -> datetime:
    return datetime.now(tz=_RUNTIME_TZ)


@lru_cache(maxsize=32)
def _easter_sunday(year: int) -> date_cls:
    """Return Gregorian Easter Sunday for the supplied year."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    # `ll` is the classic 'l' variable from the Gauss algorithm; renamed to
    # satisfy PEP 8 E741 (ambiguous single-letter name 'l').
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date_cls(year, month, day)


@lru_cache(maxsize=128)
def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date_cls:
    if n < 1:
        raise ValueError("n must be >= 1")
    first = date_cls(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = 1 + offset + (n - 1) * 7
    return date_cls(year, month, day)


@lru_cache(maxsize=128)
def _last_weekday_of_month(year: int, month: int, weekday: int) -> date_cls:
    if month == 12:
        next_month = date_cls(year + 1, 1, 1)
    else:
        next_month = date_cls(year, month + 1, 1)
    current = next_month - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


@lru_cache(maxsize=128)
def _observed_fixed_holiday(year: int, month: int, day: int) -> date_cls:
    holiday = date_cls(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _new_years_day_observed(year: int) -> date_cls | None:
    """New Year's Day as the exchanges observe it, or None when they don't.

    NYSE Rule 7.2 moves a Saturday holiday to the preceding Friday EXCEPT when
    that Friday ends a monthly or yearly accounting period -- so a Saturday New
    Year's Day is simply not observed. Shifting it back the usual way closed
    Friday Dec 31, which was a full session in 2021 (next hit: 2027-12-31).
    """
    holiday = date_cls(year, 1, 1)
    if holiday.weekday() == 5:
        return None
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


@lru_cache(maxsize=32)
def us_equity_early_close_days(year: int) -> frozenset[date_cls]:
    """Return standard NYSE/Nasdaq early-close days (1:00 PM ET close) for *year*.

    The three recurring early-close days are:
      * The day before Independence Day (Jul 3 when it is a normal trading day)
      * Black Friday (the Friday after Thanksgiving)
      * Christmas Eve (Dec 24 when it falls on a weekday)

    Only dates that are actual trading days (weekday + not a full holiday) are
    returned — if the candidate date is already a weekend or full-day holiday it
    is omitted.
    """
    full_holidays = us_equity_market_holidays(year)
    candidates: set[date_cls] = set()

    # Day before Independence Day — Jul 3 (or the preceding Friday when Jul 4
    # is observed on Friday, making Jul 3 the holiday itself).
    jul3 = date_cls(year, 7, 3)
    if jul3.weekday() < 5 and jul3 not in full_holidays:
        candidates.add(jul3)

    # Black Friday — Friday after Thanksgiving (4th Thursday of November).
    thanksgiving = _nth_weekday_of_month(year, 11, 3, 4)  # 4th Thursday
    black_friday = thanksgiving + timedelta(days=1)
    if black_friday.weekday() < 5 and black_friday not in full_holidays:
        candidates.add(black_friday)

    # Christmas Eve — Dec 24.
    dec24 = date_cls(year, 12, 24)
    if dec24.weekday() < 5 and dec24 not in full_holidays:
        candidates.add(dec24)

    return frozenset(candidates)


EQUITY_EARLY_CLOSE = time(13, 0)  # 1:00 PM ET


@lru_cache(maxsize=32)
def us_equity_market_holidays(year: int) -> frozenset[date_cls]:
    """Return standard full-day U.S. equity market holidays for the supplied year.

    This covers regular NYSE/Nasdaq full-day holidays.  Early-close sessions
    (1:00 PM ET) are modeled separately by ``us_equity_early_close_days``.
    """
    easter = _easter_sunday(year)
    holidays: set[date_cls] = {
        _nth_weekday_of_month(year, 1, 0, 3),
        _nth_weekday_of_month(year, 2, 0, 3),
        easter - timedelta(days=2),
        _last_weekday_of_month(year, 5, 0),
        _observed_fixed_holiday(year, 6, 19),
        _observed_fixed_holiday(year, 7, 4),
        _nth_weekday_of_month(year, 9, 0, 1),
        _nth_weekday_of_month(year, 11, 3, 4),
        _observed_fixed_holiday(year, 12, 25),
    }
    new_year = _new_years_day_observed(year)
    if new_year is not None:
        holidays.add(new_year)
    return frozenset(holidays)


def is_weekday_session_day(ts: datetime | date_cls | pd.Timestamp | None = None) -> bool:
    current = now_et() if ts is None else ts
    try:
        session_day = current.date() if hasattr(current, "date") else current
        if not isinstance(session_day, date_cls):
            return False
        return int(session_day.weekday()) < 5 and session_day not in us_equity_market_holidays(int(session_day.year))
    except Exception:
        return False


EQUITY_PREMARKET_START = time(4, 0)
EQUITY_STREAM_START = time(7, 0)
EQUITY_STREAM_HISTORY_REFRESH_READY = time(7, 1)
EQUITY_RTH_OPEN = time(9, 30)
EQUITY_EXTENDED_AM_ORDER_END = time(9, 25)
EQUITY_RTH_CLOSE = time(16, 0)
EQUITY_EXTENDED_PM_ORDER_START = time(16, 5)
EQUITY_STREAM_END = time(20, 0)


@dataclass(slots=True)
class EquitySessionState:
    timestamp: datetime
    is_trading_day: bool
    tradingview_market_session: str
    stream_available: bool
    regular_session: bool
    equity_order_session: str | None
    order_blackout_reason: str | None = None
    early_close: bool = False
    rth_close_time: time = EQUITY_RTH_CLOSE



def _coerce_session_datetime(ts: datetime | pd.Timestamp | None = None) -> datetime:
    current = now_et() if ts is None else ts
    if isinstance(current, pd.Timestamp):
        current = current.to_pydatetime()
    if not isinstance(current, datetime):
        raise TypeError(f"Expected datetime-like value, got {type(current)!r}")
    return current


def is_time_in_window(current: time, start: time, end: time) -> bool:
    return start <= current <= end


def equity_session_state(
    ts: datetime | pd.Timestamp | None = None,
    *,
    extended_hours_enabled: bool = True,
) -> EquitySessionState:
    current = _coerce_session_datetime(ts)
    is_trading_day = is_weekday_session_day(current)
    current_time = current.time()

    # On early-close days (Jul 3, Black Friday, Christmas Eve) the market
    # closes at 1:00 PM ET instead of 4:00 PM.  All downstream time gates
    # (regular_session, order sessions, postmarket) shift accordingly.
    early_close = False
    rth_close = EQUITY_RTH_CLOSE
    pm_order_start = EQUITY_EXTENDED_PM_ORDER_START
    if is_trading_day:
        session_date = current.date() if hasattr(current, "date") else current
        if isinstance(session_date, date_cls) and session_date in us_equity_early_close_days(int(session_date.year)):
            early_close = True
            rth_close = EQUITY_EARLY_CLOSE             # 13:00
            pm_order_start = time(13, 5)                # 13:05 (5-min gap like normal)

    tradingview_market_session = "regular"
    if is_trading_day:
        if EQUITY_PREMARKET_START <= current_time < EQUITY_RTH_OPEN:
            tradingview_market_session = "premarket"
        elif rth_close <= current_time < EQUITY_STREAM_END:
            tradingview_market_session = "postmarket"

    stream_available = is_trading_day and EQUITY_STREAM_START <= current_time < EQUITY_STREAM_END
    regular_session = is_trading_day and EQUITY_RTH_OPEN <= current_time < rth_close

    equity_order_session: str | None = None
    order_blackout_reason: str | None = None
    if regular_session:
        equity_order_session = "NORMAL"
    elif bool(extended_hours_enabled) and is_trading_day:
        if is_time_in_window(current_time, EQUITY_STREAM_START, EQUITY_EXTENDED_AM_ORDER_END):
            equity_order_session = "AM"
        elif pm_order_start <= current_time < EQUITY_STREAM_END:
            equity_order_session = "PM"
    if equity_order_session is None:
        if not is_trading_day:
            order_blackout_reason = "non_trading_day"
        elif not bool(extended_hours_enabled):
            # Extended hours disabled and we're outside RTH
            if current_time < EQUITY_RTH_OPEN:
                order_blackout_reason = "before_rth_open"
            elif current_time >= rth_close:
                order_blackout_reason = "after_rth_close"
            else:
                order_blackout_reason = "session_closed"
        elif EQUITY_EXTENDED_AM_ORDER_END < current_time < EQUITY_RTH_OPEN:
            # 9:25 — 9:30: Schwab has closed the AM extended window but RTH hasn't opened
            order_blackout_reason = "pre_open_blackout"
        elif rth_close <= current_time < pm_order_start:
            # Normal: 16:00-16:05 / Early close: 13:00-13:05
            order_blackout_reason = "post_close_blackout"
        elif current_time < EQUITY_STREAM_START:
            order_blackout_reason = "before_extended_am"
        elif current_time >= EQUITY_STREAM_END:
            order_blackout_reason = "after_extended_pm"
        else:
            order_blackout_reason = "session_closed"

    return EquitySessionState(
        timestamp=current,
        is_trading_day=is_trading_day,
        tradingview_market_session=tradingview_market_session,
        stream_available=stream_available,
        regular_session=regular_session,
        equity_order_session=equity_order_session,
        order_blackout_reason=order_blackout_reason,
        early_close=early_close,
        rth_close_time=rth_close,
    )



def is_regular_equity_session(ts: datetime | pd.Timestamp | None = None) -> bool:
    return equity_session_state(ts).regular_session



def is_equity_stream_session(ts: datetime | pd.Timestamp | None = None) -> bool:
    return equity_session_state(ts).stream_available



def classify_equity_session(
    ts: datetime | pd.Timestamp | None = None,
    *,
    extended_hours_enabled: bool = True,
) -> str | None:
    return equity_session_state(ts, extended_hours_enabled=extended_hours_enabled).equity_order_session



def classify_tradingview_market_session(ts: datetime | pd.Timestamp | None = None) -> str:
    return equity_session_state(ts).tradingview_market_session


def equity_rth_open_at(ts: datetime | pd.Timestamp | None = None) -> datetime:
    current = _coerce_session_datetime(ts)
    return current.replace(hour=EQUITY_RTH_OPEN.hour, minute=EQUITY_RTH_OPEN.minute, second=0, microsecond=0)


def equity_rth_close_at(ts: datetime | pd.Timestamp | None = None) -> datetime:
    current = _coerce_session_datetime(ts)
    close = equity_session_state(current).rth_close_time
    return current.replace(hour=close.hour, minute=close.minute, second=0, microsecond=0)


def previous_regular_close(anchor: datetime | pd.Timestamp) -> datetime:
    previous = _coerce_session_datetime(anchor) - pd.Timedelta(days=1)
    while not is_weekday_session_day(previous):
        previous -= pd.Timedelta(days=1)
    return equity_rth_close_at(previous)


def build_schedule(entry: list[tuple[str, str]], manage: list[tuple[str, str]], screener: list[tuple[str, str]]) -> StrategySchedule:
    return StrategySchedule(
        entry_windows=[Window(parse_hhmm(a), parse_hhmm(b)) for a, b in entry],
        management_windows=[Window(parse_hhmm(a), parse_hhmm(b)) for a, b in manage],
        screener_windows=[Window(parse_hhmm(a), parse_hhmm(b)) for a, b in screener],
    )


_MAX_MANAGEMENT_ADJUSTMENTS = 200


def append_management_adjustment(meta: dict, entry: dict) -> None:
    """Append a management adjustment to position metadata with a size cap.

    Keeps the most recent ``_MAX_MANAGEMENT_ADJUSTMENTS`` entries so the list
    doesn't grow without bound on very active trades.
    """
    adjustments = meta.setdefault("management_adjustments", [])
    adjustments.append(entry)
    if len(adjustments) > _MAX_MANAGEMENT_ADJUSTMENTS:
        del adjustments[: len(adjustments) - _MAX_MANAGEMENT_ADJUSTMENTS]


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
_RTH_CLOSE_MINUTE = EQUITY_RTH_CLOSE.hour * 60 + EQUITY_RTH_CLOSE.minute
_EARLY_CLOSE_MINUTE = EQUITY_EARLY_CLOSE.hour * 60 + EQUITY_EARLY_CLOSE.minute
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
    """Each timestamp's local (runtime-zone) wall-clock time of day, in
    nanoseconds. Read off the naive local clock: elapsed time since local
    midnight is an hour off the wall clock all day on a DST Sunday."""
    local = idx.tz_convert(get_runtime_timezone_name()).tz_localize(None) if idx.tz is not None else idx
    return (local - local.normalize()).to_numpy(dtype="timedelta64[ns]").astype(np.int64)


def _session_segments(idx: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per timestamp, in integer nanoseconds since its local midnight: the
    wall time, and the start and end of the session segment holding it
    (``session_bucket_bounds``). Integers throughout: float minutes lose the
    sub-second part of a clock reading (11:00:10 floored to 10:29:59.999...).
    """
    minute = _MINUTE_NS
    local = idx.tz_convert(get_runtime_timezone_name()) if idx.tz is not None else idx
    day = local.normalize()
    wall = _wall_ns(idx)
    close = np.full(len(idx), _RTH_CLOSE_MINUTE * minute, dtype=np.int64)
    dates = np.asarray(day.date)
    for session_day in set(dates.tolist()):
        if session_day in us_equity_early_close_days(int(session_day.year)):
            close[dates == session_day] = _EARLY_CLOSE_MINUTE * minute
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
    local = pd.DatetimeIndex(frame.index).tz_convert(get_runtime_timezone_name())
    minute_of_day = local.hour * 60 + local.minute
    window_open = EQUITY_STREAM_START.hour * 60 + EQUITY_STREAM_START.minute
    window_close = EQUITY_STREAM_END.hour * 60 + EQUITY_STREAM_END.minute
    return frame[(minute_of_day >= window_open) & (minute_of_day < window_close)]


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
    """The bars ``add_indicators`` treats as session bars: RTH (09:30-16:00)
    by default, the 07:00-20:00 equity stream window when
    ``equity_session_indicator_window`` is "extended".

    They anchor the per-session VWAP/EMA reset and, with
    ``use_rth_session_indicators`` on, carry the session-only TA-Lib series;
    the other bars carry the all-hours one. A consumer comparing indicator
    values across bars (divergence pivots) needs this mask to keep to one
    series.
    """
    predicate = _indicator_session_predicate()
    return np.fromiter((predicate(ts) for ts in pd.DatetimeIndex(index)), dtype=bool, count=len(index))


def _indicator_session_predicate():
    return is_equity_stream_session if get_session_indicator_window() == "extended" else is_regular_equity_session


def indicator_session_open() -> bool:
    """Whether a reader is inside the session right now: session indicators
    are on and the clock (``now_et``) is inside their window (RTH, or
    07:00-20:00 under "extended").

    Readers that switch to the session-only series in the session and keep
    the all-hours one outside it (``latest_atr14``, the divergence age in
    the technical and HTF builders) gate on this, not on their frame's last
    bar: an HTF frame's last completed bucket is still a premarket one until
    09:45 (15m) or 10:30 (60m), and a reader already in the session must
    not read it as a premarket reader would (2026-09-24). Reads ``now_et``
    through this module, so a test pinning ``utils.now_et`` pins it.
    """
    return get_runtime_indicator_mode() and _indicator_session_predicate()(now_et())


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


def atr_value(frame: pd.DataFrame) -> float:
    if frame is None or frame.empty:
        return 0.0
    if "atr14" not in frame.columns:
        frame = ensure_standard_indicator_frame(frame)
    atr = latest_atr14(frame) or 0.0
    close = float(frame.iloc[-1]["close"]) if not frame.empty else 0.0
    return max(atr, close * 0.0015 if close > 0 else 0.0)


class _ETDailyFileHandler(logging.FileHandler):
    """FileHandler that rotates to ``bot_{YYYY-MM-DD}.log`` on ET-date change,
    regardless of host timezone. `session_report.export_session_archive`
    expects a filename matching the current session date. Date-check is
    throttled via a monotonic timer so the hot log path doesn't pay for a
    tz conversion on every record."""

    _CHECK_INTERVAL_SECONDS = 30.0

    def __init__(self, log_dir: Path, encoding: str = "utf-8") -> None:
        self._log_dir = Path(log_dir)
        self._current_date = now_et().date().isoformat()
        self._last_date_check = _monotonic_time.monotonic()
        super().__init__(self._log_dir / f"bot_{self._current_date}.log", encoding=encoding)

    def emit(self, record: logging.LogRecord) -> None:
        now_mono = _monotonic_time.monotonic()
        if now_mono - self._last_date_check >= self._CHECK_INTERVAL_SECONDS:
            self._last_date_check = now_mono
            today = now_et().date().isoformat()
            if today != self._current_date:
                self.acquire()
                try:
                    if today != self._current_date:
                        if self.stream is not None:
                            self.stream.close()
                        self._current_date = today
                        self.baseFilename = str(self._log_dir / f"bot_{today}.log")
                        self.stream = self._open()
                finally:
                    self.release()
        super().emit(record)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` via tmp+rename so a mid-write crash leaves
    the prior-good file intact instead of truncating it. ``Path.replace`` is
    atomic on both POSIX and Windows for same-volume renames."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text, encoding=encoding)
    tmp_path.replace(path)


def setup_logging(log_dir: Union[str, Path]) -> None:
    register_tradeflow_logging_level()
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            LOG.debug(
                "Unable to reconfigure stdout encoding; "
                "continuing with existing stream settings.",
                exc_info=True,
            )

    file_handler = _ETDailyFileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(stream=stream)
    console_handler.setLevel(logging.INFO)
    _enable_windows_ansi()
    if _console_supports_color(stream):
        console_handler.setFormatter(ColorConsoleFormatter())
    else:
        console_handler.setFormatter(formatter)

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.getLogger("urllib3").setLevel(logging.INFO)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)
    logging.getLogger("websockets").setLevel(logging.INFO)
    logging.getLogger("websockets.client").setLevel(logging.INFO)

    LOG.info("Logging to %s (daily rotation at ET midnight)", file_handler.baseFilename)

