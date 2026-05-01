# SPDX-License-Identifier: MIT
import json
import logging
import math
import os
import sys
import time as _monotonic_time
from threading import RLock
from urllib.parse import urlsplit
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

from .models import DEFAULT_RUNTIME_TZ, Side, StrategySchedule, Window


LOG = logging.getLogger(__name__)

_USE_RTH_SESSION_INDICATORS = True

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


class SchwabdevApiUsageTracker:
    def __init__(self) -> None:
        self.started_at = now_et()
        self.total_calls = 0
        self.last_call_at: Optional[datetime] = None
        self.method_counts: dict[str, int] = {}
        self._lock = RLock()

    def record_call(self, method_name: str) -> None:
        name = str(method_name or 'unknown')
        with self._lock:
            self.total_calls += 1
            self.last_call_at = now_et()
            self.method_counts[name] = int(self.method_counts.get(name, 0)) + 1

    def snapshot(self, now: Optional[datetime] = None) -> dict[str, Any]:
        current = now or now_et()
        with self._lock:
            elapsed_minutes = max(
                (current - self.started_at).total_seconds() / 60.0,
                1.0 / 60.0,
            )
            avg_calls = (
                float(self.total_calls) / elapsed_minutes
                if self.total_calls > 0
                else 0.0
            )
            snapshot = {
                'started_at': self.started_at.isoformat(),
                "last_call_at": (
                    self.last_call_at.isoformat()
                    if self.last_call_at is not None
                    else None
                ),
                'total_calls': int(self.total_calls),
                'avg_calls_per_minute': avg_calls,
                'method_counts': dict(self.method_counts),
            }
        return snapshot


_SCHWAB_CLIENT_TRACKERS: dict[int, SchwabdevApiUsageTracker] = {}


def register_schwab_api_tracker(client: Any, tracker: SchwabdevApiUsageTracker) -> None:
    _SCHWAB_CLIENT_TRACKERS[id(client)] = tracker


def get_schwab_api_tracker(client: Any) -> Optional[SchwabdevApiUsageTracker]:
    return _SCHWAB_CLIENT_TRACKERS.get(id(client))


def _truncate_log_text(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def call_schwab_client(client: Any, method_name: str, *args: Any, **kwargs: Any) -> Any:
    tracker = get_schwab_api_tracker(client)
    if tracker is not None:
        tracker.record_call(method_name)
    method = getattr(client, method_name)
    response = method(*args, **kwargs)
    status = getattr(response, "status_code", None)
    if status is not None and not 200 <= int(status) < 300:
        request = getattr(response, "request", None)
        request_method = str(getattr(request, "method", "") or "")
        request_url = str(getattr(request, "url", "") or "")
        request_path = urlsplit(request_url).path if request_url else ""
        response_body = _truncate_log_text(getattr(response, "text", ""))
        response_reason = str(getattr(response, "reason", "") or "")
        log_payload = {
            "method_name": str(method_name or "unknown"),
            "status_code": int(status),
            "reason": response_reason,
            "request_method": request_method,
            "request_path": request_path,
            "args": [_truncate_log_text(arg, 120) for arg in args],
            "kwargs": {str(k): _truncate_log_text(v, 120) for k, v in kwargs.items()},
            "response_text": response_body,
        }
        if int(status) >= 400:
            LOG.warning(
                "Schwab HTTP non-2xx response: %s",
                json.dumps(log_payload, default=str),
            )
        else:
            LOG.debug(
                "Schwab HTTP non-2xx response: %s",
                json.dumps(log_payload, default=str),
            )
    return response


def set_runtime_indicator_mode(enabled: bool) -> None:
    global _USE_RTH_SESSION_INDICATORS
    _USE_RTH_SESSION_INDICATORS = bool(enabled)


def get_runtime_indicator_mode() -> bool:
    return bool(_USE_RTH_SESSION_INDICATORS)


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
        _observed_fixed_holiday(year, 1, 1),
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
    next_new_year_observed = _observed_fixed_holiday(year + 1, 1, 1)
    if next_new_year_observed.year == year:
        holidays.add(next_new_year_observed)
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
    frame = frame[~frame.index.duplicated(keep="last")]
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in frame.columns:
            frame[col] = math.nan
        else:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    if frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    frame["volume"] = frame["volume"].fillna(0.0)
    return frame[["open", "high", "low", "close", "volume"] + [c for c in frame.columns if c not in {"open", "high", "low", "close", "volume"}]]


def resample_bars(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    frame = ensure_ohlcv_frame(frame)
    if frame.empty:
        return frame
    agg = frame.resample(rule, label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    out = agg.dropna(subset=["open", "high", "low", "close"])
    out.attrs["time_label"] = "right"
    return out


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


def ensure_standard_indicator_frame(frame: pd.DataFrame) -> pd.DataFrame:
    # Fast path: if the frame already carries every standard indicator column,
    # it was produced by add_indicators() upstream which itself calls
    # ensure_ohlcv_frame internally. Re-running ensure_ohlcv_frame here on the
    # hot path (copy + sort + 5x to_numeric + dropna + reorder) is the single
    # biggest overhead in build_technical_levels_context / analyze_market_structure
    # when the frame is already clean. Skip it by trusting the indicator marker.
    if frame is not None and not frame.empty and has_standard_indicator_columns(frame):
        return frame
    cleaned = ensure_ohlcv_frame(frame)
    if cleaned.empty:
        return cleaned
    if has_standard_indicator_columns(cleaned):
        return cleaned
    return add_indicators(cleaned)


FloatArray = npt.NDArray[np.float64]


def _to_float64_array(series: pd.Series) -> FloatArray:
    return np.asarray(series.to_numpy(dtype=np.float64), dtype=np.float64)


def _series_from_talib(index: pd.Index, values: Any) -> pd.Series:
    return pd.Series(np.asarray(values, dtype=np.float64), index=index, dtype=float)


def _require_talib() -> Any:
    if talib is None:
        raise RuntimeError("TA-Lib is required for indicator calculation but is not installed")
    return talib


def _talib_ema(series: pd.Series, span: int) -> pd.Series:
    ta = _require_talib()
    return _series_from_talib(series.index, ta.EMA(_to_float64_array(series), timeperiod=int(span)))


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    frame = ensure_ohlcv_frame(frame)
    if frame.empty:
        return frame
    out = frame.copy()
    ta = _require_talib()
    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    volume = out["volume"].fillna(0.0).astype(float)

    session_keys = pd.Index(out.index.map(lambda ts: ts.date()), name="session_date")
    tpv = ((high + low + close) / 3.0) * volume
    cum_vol = volume.groupby(session_keys).cumsum().replace(0, math.nan)
    cum_tpv = tpv.groupby(session_keys).cumsum()
    out["vwap_all"] = cum_tpv / cum_vol
    out["ema9_all"] = _talib_ema(close, span=9)
    out["ema20_all"] = _talib_ema(close, span=20)

    index_dt = pd.DatetimeIndex(out.index)
    rth_mask = pd.Series(
        [is_regular_equity_session(ts) for ts in index_dt],
        index=out.index,
        dtype=bool,
    )
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

    out["ema9_rth"] = _session_rth_ema(close, span=9)
    out["ema20_rth"] = _session_rth_ema(close, span=20)
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
    upper, middle, lower_band = ta.BBANDS(
        _to_float64_array(close),
        timeperiod=20,
        nbdevup=2.0,
        nbdevdn=2.0,
        matype=ta.MA_Type.SMA,
    )
    out["bb_mid"] = _series_from_talib(out.index, middle)
    out["bb_upper"] = _series_from_talib(out.index, upper)
    out["bb_lower"] = _series_from_talib(out.index, lower_band)
    out["bb_width"] = out["bb_upper"] - out["bb_lower"]
    out["bb_width_pct"] = out["bb_width"] / out["bb_mid"].replace(0.0, math.nan)
    out["bb_percent_b"] = (close - out["bb_lower"]) / out["bb_width"].replace(0.0, math.nan)
    rolling_std = close.rolling(20, min_periods=10).std(ddof=0)
    out["bb_zscore"] = (close - out["bb_mid"]) / rolling_std.replace(0.0, math.nan)

    out["atr14"] = _series_from_talib(out.index, ta.ATR(_to_float64_array(high), _to_float64_array(low), _to_float64_array(close), timeperiod=14))
    out["plus_di14"] = _series_from_talib(out.index, ta.PLUS_DI(_to_float64_array(high), _to_float64_array(low), _to_float64_array(close), timeperiod=14))
    out["minus_di14"] = _series_from_talib(out.index, ta.MINUS_DI(_to_float64_array(high), _to_float64_array(low), _to_float64_array(close), timeperiod=14))
    out["adx14"] = _series_from_talib(out.index, ta.ADX(_to_float64_array(high), _to_float64_array(low), _to_float64_array(close), timeperiod=14))

    out["obv"] = _series_from_talib(out.index, ta.OBV(_to_float64_array(close), _to_float64_array(volume)))
    out["obv_ema20"] = _talib_ema(out["obv"], span=20)
    out["obv_delta5"] = out["obv"].diff(5)
    out["rsi14"] = _series_from_talib(out.index, ta.RSI(_to_float64_array(close), timeperiod=14))

    out["ret1"] = close.pct_change()
    out["ret5"] = close.pct_change(5)
    out["ret15"] = close.pct_change(15)

    # --- RTH session-reset overlay for TA-Lib indicators ---
    # When use_rth_session_indicators is enabled, recompute indicators using
    # only today's RTH bars.  Each indicator group activates independently
    # once enough RTH bars exist for its lookback period.  Before that
    # threshold, the all-hours values above are used as-is (no wasted
    # computation).  This eliminates pre-market contamination from the signal
    # indicators while keeping all-hours values for chart display on non-RTH
    # bars.
    if get_runtime_indicator_mode():
        last_day = index_dt[-1].normalize()
        today_rth = out[rth_mask & (index_dt.normalize() == last_day)]
        n_rth = len(today_rth)

        def _overlay(col: str, rth_series: pd.Series) -> None:
            """Overlay RTH values onto the main frame, preserving the all-hours
            fallback for bars where the RTH computation produces NaN (leading
            lookback period)."""
            valid = rth_series.dropna()
            if not valid.empty:
                out.loc[valid.index, col] = valid

        if n_rth >= 2:
            rth_close = today_rth["close"].astype(float)
            rth_high = today_rth["high"].astype(float)
            rth_low = today_rth["low"].astype(float)
            rth_volume = today_rth["volume"].fillna(0.0).astype(float)

            # Returns — clean from bar 2 onward
            _overlay("ret1", rth_close.pct_change())
            if n_rth >= 5:
                _overlay("ret5", rth_close.pct_change(5))
            if n_rth >= 15:
                _overlay("ret15", rth_close.pct_change(15))

            # OBV — clean from bar 2 onward
            rth_obv = _series_from_talib(today_rth.index, ta.OBV(_to_float64_array(rth_close), _to_float64_array(rth_volume)))
            _overlay("obv", rth_obv)
            if n_rth >= 20:
                _overlay("obv_ema20", _talib_ema(rth_obv, span=20))
            _overlay("obv_delta5", rth_obv.diff(5))

            # ATR, DI, RSI — clean from bar 14+; ADX needs ~28
            if n_rth >= 14:
                rth_h = _to_float64_array(rth_high)
                rth_l = _to_float64_array(rth_low)
                rth_c = _to_float64_array(rth_close)
                _overlay("atr14", _series_from_talib(today_rth.index, ta.ATR(rth_h, rth_l, rth_c, timeperiod=14)))
                _overlay("plus_di14", _series_from_talib(today_rth.index, ta.PLUS_DI(rth_h, rth_l, rth_c, timeperiod=14)))
                _overlay("minus_di14", _series_from_talib(today_rth.index, ta.MINUS_DI(rth_h, rth_l, rth_c, timeperiod=14)))
                _overlay("adx14", _series_from_talib(today_rth.index, ta.ADX(rth_h, rth_l, rth_c, timeperiod=14)))
                _overlay("rsi14", _series_from_talib(today_rth.index, ta.RSI(_to_float64_array(rth_close), timeperiod=14)))

            # Bollinger Bands — clean from bar 20 onward
            if n_rth >= 20:
                r_upper, r_middle, r_lower = ta.BBANDS(
                    _to_float64_array(rth_close), timeperiod=20,
                    nbdevup=2.0, nbdevdn=2.0, matype=ta.MA_Type.SMA,
                )
                rth_bb_mid = _series_from_talib(today_rth.index, r_middle)
                rth_bb_upper = _series_from_talib(today_rth.index, r_upper)
                rth_bb_lower = _series_from_talib(today_rth.index, r_lower)
                rth_bb_width = rth_bb_upper - rth_bb_lower
                _overlay("bb_mid", rth_bb_mid)
                _overlay("bb_upper", rth_bb_upper)
                _overlay("bb_lower", rth_bb_lower)
                _overlay("bb_width", rth_bb_width)
                _overlay("bb_width_pct", rth_bb_width / rth_bb_mid.replace(0.0, math.nan))
                _overlay("bb_percent_b", (rth_close - rth_bb_lower) / rth_bb_width.replace(0.0, math.nan))
                rth_std = rth_close.rolling(20, min_periods=10).std(ddof=0)
                _overlay("bb_zscore", (rth_close - rth_bb_mid) / rth_std.replace(0.0, math.nan))

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
    atr = 0.0
    if "atr14" in frame.columns:
        atr_clean = frame["atr14"].dropna()
        if not atr_clean.empty:
            atr = float(atr_clean.iloc[-1])
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


def opposite_side(side: Side) -> Side:
    return Side.SHORT if side == Side.LONG else Side.LONG
