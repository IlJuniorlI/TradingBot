# SPDX-License-Identifier: MIT
"""The session clock and calendar: the exchange zone and the one reading of
the time, HH:MM parsing, the NYSE holiday and early-close calendar, the
``EQUITY_*`` session times, the per-instant session state, and the ET
session-date index helpers.

Every reader calls ``sessions.now_et()`` through this module, never a name
bound by ``from ... import now_et``, so a test pins the whole bot's clock
with one patch (``tests/support/clock.freeze_et``). The other names are
imported by name."""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np
import numpy.typing as npt
import pandas as pd

# The exchange clock. The bot trades US equity and option sessions only: every
# configured time (windows, blackouts, HH:MM knobs) and every EQUITY_* constant
# is a New York wall time, so there is no timezone setting (runtime.timezone
# was retired 2026-09-26).
EXCHANGE_TZ = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def now_et() -> datetime:
    return datetime.now(tz=EXCHANGE_TZ)


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


@lru_cache(maxsize=32)
def _easter_sunday(year: int) -> date:
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
    return date(year, month, day)


@lru_cache(maxsize=128)
def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    if n < 1:
        raise ValueError("n must be >= 1")
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = 1 + offset + (n - 1) * 7
    return date(year, month, day)


@lru_cache(maxsize=128)
def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    current = next_month - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


@lru_cache(maxsize=128)
def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _new_years_day_observed(year: int) -> date | None:
    """New Year's Day as the exchanges observe it, or None when they don't.

    NYSE Rule 7.2 moves a Saturday holiday to the preceding Friday EXCEPT when
    that Friday ends a monthly or yearly accounting period -- so a Saturday New
    Year's Day is simply not observed. Shifting it back the usual way closed
    Friday Dec 31, which was a full session in 2021 (next hit: 2027-12-31).
    """
    holiday = date(year, 1, 1)
    if holiday.weekday() == 5:
        return None
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


@lru_cache(maxsize=32)
def us_equity_early_close_days(year: int) -> frozenset[date]:
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
    candidates: set[date] = set()

    # Day before Independence Day — Jul 3 (or the preceding Friday when Jul 4
    # is observed on Friday, making Jul 3 the holiday itself).
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 5 and jul3 not in full_holidays:
        candidates.add(jul3)

    # Black Friday — Friday after Thanksgiving (4th Thursday of November).
    thanksgiving = _nth_weekday_of_month(year, 11, 3, 4)  # 4th Thursday
    black_friday = thanksgiving + timedelta(days=1)
    if black_friday.weekday() < 5 and black_friday not in full_holidays:
        candidates.add(black_friday)

    # Christmas Eve — Dec 24.
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5 and dec24 not in full_holidays:
        candidates.add(dec24)

    return frozenset(candidates)


EQUITY_EARLY_CLOSE = time(13, 0)  # 1:00 PM ET


@lru_cache(maxsize=32)
def us_equity_market_holidays(year: int) -> frozenset[date]:
    """Return standard full-day U.S. equity market holidays for the supplied year.

    This covers regular NYSE/Nasdaq full-day holidays.  Early-close sessions
    (1:00 PM ET) are modeled separately by ``us_equity_early_close_days``.
    """
    easter = _easter_sunday(year)
    holidays: set[date] = {
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


def is_weekday_session_day(ts: datetime | date | pd.Timestamp | None = None) -> bool:
    current = now_et() if ts is None else ts
    try:
        session_day = current.date() if hasattr(current, "date") else current
        if not isinstance(session_day, date):
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


def is_time_in_window(current: time, start: time | str | int, end: time | str | int) -> bool:
    """``start <= current <= end``, inclusive at both ends. Each end is
    anything ``parse_hhmm`` reads: a ``time``, "HH:MM", or YAML's
    sexagesimal minutes."""
    return parse_hhmm(start) <= current <= parse_hhmm(end)


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
        if isinstance(session_date, date) and session_date in us_equity_early_close_days(int(session_date.year)):
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


_SESSION_WINDOWS = ("rth", "extended")


def _per_session_day(index: pd.DatetimeIndex, value: Callable[[date], object], dtype: type) -> np.ndarray:
    """``value(day)`` for each timestamp's wall-clock date, computed once per date."""
    codes, days = pd.factorize(index.normalize())
    return np.array([value(day.date()) for day in days], dtype=dtype)[codes]


def _close_minute(day: date) -> int:
    close = EQUITY_EARLY_CLOSE if day in us_equity_early_close_days(day.year) else EQUITY_RTH_CLOSE
    return close.hour * 60 + close.minute


def rth_close_minute(dates: pd.Index) -> npt.NDArray[np.int64]:
    """Per timestamp of ``dates``: the regular-session close of its wall-clock
    date as a minute of the day, 960 (16:00) or 780 (13:00) on an early-close
    day. A weekend or holiday gets 960: the bucket grid
    (``session_bucket_bounds``) lays those days out too."""
    return _per_session_day(pd.DatetimeIndex(dates), _close_minute, np.int64)


def session_mask(index: pd.Index, window: str) -> npt.NDArray[np.bool_]:
    """Bars that START inside a trading day's session window: "rth" is 09:30
    up to the close (16:00, 13:00 on an early-close day), "extended" the
    07:00-20:00 equity stream window. Weekends and full holidays hold no
    session bar. Read on the index's own wall clock, as
    ``equity_session_state`` reads a timestamp: a naive index is ET wall time,
    and an ET-local one (``session_datetime_index``) is what the prior-day
    and prior-week levels pass."""
    if window not in _SESSION_WINDOWS:
        raise ValueError(f"session window must be one of {_SESSION_WINDOWS}, got {window!r}")
    idx = pd.DatetimeIndex(index)
    minute = np.asarray(idx.hour * 60 + idx.minute, dtype=np.int64)
    trading = _per_session_day(idx, is_weekday_session_day, bool)
    if window == "extended":
        start = EQUITY_STREAM_START.hour * 60 + EQUITY_STREAM_START.minute
        end = EQUITY_STREAM_END.hour * 60 + EQUITY_STREAM_END.minute
        return trading & (minute >= start) & (minute < end)
    rth_open = EQUITY_RTH_OPEN.hour * 60 + EQUITY_RTH_OPEN.minute
    return trading & (minute >= rth_open) & (minute < rth_close_minute(idx))


def datetime_index(index: pd.Index) -> pd.DatetimeIndex:
    if isinstance(index, pd.DatetimeIndex):
        return index
    return pd.DatetimeIndex(index)


def session_datetime_index(index: pd.Index) -> pd.DatetimeIndex:
    """ET-session-localized DatetimeIndex.

    Converts to ``America/New_York`` and then strips tz so that ``.date`` and
    ``.to_period('W-FRI')`` bucket bars by the ET trading day/week. Required
    for prior-day / prior-week computation because a plain UTC-date bucketing
    would misclassify, e.g., a Mon 7:00 PM ET post-market bar during EST as
    belonging to Tuesday (because 7 PM ET EST = 00:00 UTC the next day).

    A tz-naive input is returned unchanged (assumed to already be ET-local).
    """
    dt_index = datetime_index(index)
    if dt_index.tz is None:
        return dt_index
    return dt_index.tz_convert(EXCHANGE_TZ).tz_localize(None)


def session_segment_ids(index: pd.Index) -> np.ndarray:
    """Run id per bar that advances at every change of ET session date.

    The bar frames hold only the 07:00-20:00 ET stream window, so two
    neighbouring bars on different ET dates are separated by trading nobody
    observed. Detectors that compare neighbouring bars -- pivots,
    fair-value-gap triplets, order blocks -- require their window to lie
    inside one run.

    The boundary is the ET date, not a time step. Within a session a thin
    name routinely prints no bar for minutes, and a minute without a trade is
    not missing data: nothing traded. Archived 1m bars (2026-05..09) show
    2-13% of pre/post-market steps longer than 2 minutes and same-day steps
    up to 209 minutes, so a "step > 2 x timeframe" rule would have dropped
    real extended-hours pivots. Every step across an ET date is at least
    11 hours (19:59 -> 07:00).
    """
    if len(index) == 0:
        return np.zeros(0, dtype=np.int64)
    days = session_datetime_index(index).normalize().to_numpy()
    changes = np.concatenate(([0], (days[1:] != days[:-1]).astype(np.int64)))
    return np.cumsum(changes)


def latest_session_date(now: datetime) -> date:
    """The ET date of ``now``, rolled back to the latest trading day on or
    before it (a Saturday resolves to Friday, a holiday Monday to Friday).

    The builders pass this as ``as_of`` to ``prior_day_levels`` /
    ``prior_week_levels`` when the caller gives none."""
    stamp = pd.Timestamp(now)
    day = (stamp.tz_convert(EXCHANGE_TZ) if stamp.tzinfo is not None else stamp).date()
    while not is_weekday_session_day(day):
        day -= timedelta(days=1)
    return day
