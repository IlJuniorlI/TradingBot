# SPDX-License-Identifier: MIT
"""Scheduled-event blackouts — macro windows and per-symbol earnings.

Two kinds of known-in-advance event stop new entries:

  * **Macro windows** — CPI at 08:30, FOMC at 14:00 and friends. Time-of-day
    windows on a given date or weekday, optionally scoped to a symbol list.
  * **Earnings** — a per-symbol date list. An earnings print resets a
    symbol's volatility regime, so the ATR-derived stops and the
    session-open-anchored ``day_strength`` that the strategies rely on are
    both calibrated to a distribution that no longer applies. Blocks the
    configured number of trading sessions either side of the date.

This machinery previously lived inside the 0DTE options strategy, which made
it unavailable to every equity strategy — top_tier trades 23 mega caps with
roughly 92 scheduled earnings a year between them and had no event awareness
at all. It now lives here and is driven by the top-level ``events:`` config
section, so options and equity strategies share one calendar.

The files are re-read when their mtime changes, so a blackout can be added to
a running bot without a restart. A file that cannot be read, is not YAML, has
the wrong shape or carries a bad row refuses startup, naming it; the same
edit made while the bot runs is logged as an ERROR and the rows last read
stay in force.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from .serialization import read_yaml
from .sessions import is_hhmm, is_time_in_window
from .symbols import ticker_quote_hint
from . import sessions

LOG = logging.getLogger(__name__)

_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
# The spellings a row's weekday takes, in any case: MON-SUN, MONDAY-SUNDAY,
# or 0-6 from Monday.
_WEEKDAY_TOKENS = {
    **{day: day for day in _WEEKDAYS},
    **{str(index): day for index, day in enumerate(_WEEKDAYS)},
    "MONDAY": "MON", "TUESDAY": "TUE", "WEDNESDAY": "WED", "THURSDAY": "THU",
    "FRIDAY": "FRI", "SATURDAY": "SAT", "SUNDAY": "SUN",
}
# A row field read as a switch: true / false, nothing else (the string
# "false" is truthy).
_ROW_SWITCHES = ("enabled", "block_new_entries", "force_flatten")
# Every key a row takes; blackout_row_errors refuses any other, so a typo
# (``force_flaten: true``) is not a switch left at its default.
_ROW_KEYS = ("label", "date", "weekday", "start", "end", *_ROW_SWITCHES, "symbols")
# A row or earnings date written as text: YYYY-MM-DD and nothing else
# (date.fromisoformat also reads 20260930 and 2026-W40-3).
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def weekday_token(value: Any) -> str | None:
    """``MON``-``SUN`` for a weekday spelling (``0``, ``MONDAY``, ``Mon``);
    ``None`` for any other value, which ``blackout_row_errors`` refuses
    (``Monkey`` and ``SATURN`` are no weekdays)."""
    return _WEEKDAY_TOKENS.get(str(value).strip().upper())


def _resolve_path(path_value: str) -> Path:
    """Resolve a configured path against cwd, then the package and project
    roots — the same search the 0DTE loader used, so existing relative
    ``./macro_events.auto.yaml`` settings keep working from any cwd."""
    raw = Path(path_value).expanduser()
    candidates = [raw]
    if not raw.is_absolute():
        package_root = Path(__file__).resolve().parent
        project_root = Path(__file__).resolve().parents[1]
        for base in (package_root, project_root):
            alt = (base / raw).resolve()
            if alt not in candidates:
                candidates.append(alt)
    return next((c for c in candidates if c.exists()), candidates[0])


def blackout_row_errors(rows: Any, where: str) -> list[str]:
    """One message per problem in a list of macro-window rows, naming it
    ``{where}[index].start``: a list that is not one (``None`` reads as
    empty), a row that is not a mapping, a key a row does not take, a
    ``start`` or ``end`` that is not an HH:MM time, a ``date`` that is not
    a YYYY-MM-DD date, a ``weekday`` that is no weekday or not the date's,
    an ``enabled`` / ``block_new_entries`` / ``force_flatten`` that is not
    true or false, and a ``symbols`` that is not a list of tickers. An
    absent or null ``date`` / ``weekday`` is no filter; each of the others
    would make the window block nothing, or block where the row says it
    does not, without a word."""
    if rows is None:
        return []
    if not isinstance(rows, list | tuple):
        return [f"{where} must be a list of event rows, got {rows!r}"]
    errors: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"{where}[{index}] must be a mapping of event fields, got {row!r}")
            continue
        field = f"{where}[{index}]"
        errors += [
            f"{field} has an unknown key {key!r}: a row takes {', '.join(_ROW_KEYS)}"
            for key in row if key not in _ROW_KEYS
        ]
        for key in ("start", "end"):
            value = row.get(key)
            if not is_hhmm(value):
                errors.append(f"{field}.{key} must be an HH:MM time, got {value!r}")
        event_date: date | None = None
        if row.get("date") is not None:
            try:
                event_date = _parse_date(row["date"])
            except ValueError:
                errors.append(f"{field}.date must be a YYYY-MM-DD date, got {row['date']!r}")
        if row.get("weekday") is not None:
            weekday = weekday_token(row["weekday"])
            if weekday not in _WEEKDAYS:
                errors.append(
                    f"{field}.weekday must be a weekday (MON-SUN, MONDAY-SUNDAY or 0-6 from Monday), "
                    f"got {row['weekday']!r}"
                )
            elif event_date is not None and weekday != _WEEKDAYS[event_date.weekday()]:
                errors.append(
                    f"{field}.weekday {row['weekday']!r} is not the weekday of its date "
                    f"{event_date.isoformat()} ({_WEEKDAYS[event_date.weekday()]}), so the window never applies"
                )
        for key in _ROW_SWITCHES:
            if key in row and not isinstance(row[key], bool):
                errors.append(f"{field}.{key} must be true or false, got {row[key]!r}")
        symbols = row.get("symbols")
        if symbols is not None and not isinstance(symbols, list | tuple):
            errors.append(f"{field}.symbols must be a list of symbols, got {symbols!r}")
        elif symbols is not None:
            errors += [
                f"{field}.symbols[{position}] must be a ticker, got {symbol!r}{ticker_quote_hint(symbol)}"
                for position, symbol in enumerate(symbols)
                if not isinstance(symbol, str) or not symbol.strip()
            ]
    return errors


def earnings_errors(mapping: Any, where: str) -> list[str]:
    """One message per problem in a ``{SYMBOL: [YYYY-MM-DD, ...]}`` earnings
    map, naming it ``{where}.SYMBOL[index]``: a map that is not one (``None``
    reads as empty), a symbol that is blank or not a string (an unquoted
    ``ON:`` is ``True``, so ON's dates were filed under ``TRUE``), dates that
    are not a list, and a date that is not a YYYY-MM-DD date."""
    if mapping is None:
        return []
    if not isinstance(mapping, dict):
        return [f"{where} must be a mapping of SYMBOL: [YYYY-MM-DD, ...], got {mapping!r}"]
    errors: list[str] = []
    for symbol, dates in mapping.items():
        if not isinstance(symbol, str):
            errors.append(f"{where} has a symbol that is not a ticker, got {symbol!r}{ticker_quote_hint(symbol)}")
            continue
        if not symbol.strip():
            errors.append(f"{where} has a blank symbol, got {symbol!r}")
            continue
        if dates is None:
            continue
        if not isinstance(dates, list | tuple):
            errors.append(f"{where}.{symbol} must be a list of YYYY-MM-DD dates, got {dates!r}")
            continue
        for index, raw in enumerate(dates):
            try:
                _parse_date(raw)
            except ValueError:
                errors.append(f"{where}.{symbol}[{index}] must be a YYYY-MM-DD date, got {raw!r}")
    return errors


def _earnings_map(mapping: Any) -> dict[str, set[date]]:
    """``{SYMBOL: {dates}}`` from a map ``earnings_errors`` passes."""
    parsed: dict[str, set[date]] = {}
    for symbol, dates in (mapping or {}).items():
        parsed.setdefault(str(symbol).upper().strip(), set()).update(_parse_date(raw) for raw in (dates or []))
    return parsed


def _blackout_file_rows(payload: Any, path: Path) -> list[dict[str, Any]]:
    """The rows of a parsed ``blackout_file``: a list of rows, or a mapping
    that holds them under ``events:``; an empty file has none. ``ValueError``
    naming the file and every bad row otherwise."""
    if isinstance(payload, dict) and "events" in payload:
        payload = payload["events"]
    elif payload is not None and not isinstance(payload, list):
        raise ValueError(f"{path}: must be a list of event rows or a mapping with an events: list, got {payload!r}")
    errors = blackout_row_errors(payload, f"{path}: events")
    if errors:
        raise ValueError("\n  ".join(errors))
    return [dict(row) for row in (payload or [])]


def _earnings_file_dates(payload: Any, path: Path) -> dict[str, set[date]]:
    """The dates of a parsed ``earnings_file``: a ``{SYMBOL: [dates]}`` map,
    bare or under ``earnings:``; an empty file has none. ``ValueError``
    naming the file and every bad entry otherwise."""
    if isinstance(payload, dict) and "earnings" in payload:
        payload = payload["earnings"]
    errors = earnings_errors(payload, f"{path}: earnings")
    if errors:
        raise ValueError("\n  ".join(errors))
    return _earnings_map(payload)


def _read_yaml(path: Path) -> Any:
    """The YAML in *path* (``serialization.read_yaml``); ``None`` for an
    empty file, or a missing one (with a warning). ``ValueError`` naming the
    file when it cannot be read, is not YAML or holds an unquoted date that
    cannot exist, naming its line."""
    if not path.exists():
        LOG.warning("Event calendar file not found: %s", path)
        return None
    return read_yaml(path)


def _sessions_between(start: date, end: date) -> int:
    """Signed count of trading sessions from *start* to *end*, weekends
    excluded. Positive when *end* is after *start*.

    Weekday-based, so market holidays count as sessions. That errs toward
    blacklisting a day too many around an earnings date, which is the safe
    direction — and a half-day's worth of extra caution around a print is
    not worth wiring a holiday calendar in for.
    """
    return int(np.busday_count(start, end))


class EventBlackoutCalendar:
    """Loads and evaluates the blackout calendar for one bot process.

    A single instance is shared by every strategy through
    ``BaseStrategy._event_calendar``. Reads are cheap: the YAML files are
    only re-parsed when their mtime changes.
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        # File-sourced data is kept SEPARATE from the inline config rows and
        # the two are composed on read. Merging them into one list and trying
        # to tell them apart afterwards meant an inline entry deleted from the
        # config lived on forever in the merged copy, and it stamped a marker
        # key into the user's own rows.
        self._macro_file_events: list[dict[str, Any]] = []
        self._earnings_file: dict[str, set[date]] = {}
        self._macro_events: list[dict[str, Any]] = []
        self._earnings: dict[str, set[date]] = {}
        # (resolved path, mtime) each file was last read at, by config field.
        self._file_tokens: dict[str, tuple[str, float | None]] = {}
        # The files as they stand at startup are read here, so a broken one
        # fails naming itself or its row rather than reading as empty or
        # raising on the event's date (load_config checks the inline rows). A
        # later edit is re-read on its mtime, and a broken one keeps the rows
        # last read: raising there would stop the entry cycle, and for the
        # 0DTE strategies the force-flatten check.
        self._refresh_macro(startup=True)
        self._refresh_earnings(startup=True)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    @property
    def _events_cfg(self) -> Any:
        return self._config.events

    def _reload_file(self, field_name: str, rows_of: Callable[[Any, Path], Any], *, startup: bool) -> Any | None:
        """*rows_of* over the file ``events.<field_name>`` names, when its
        mtime moved since the last read; ``None`` when it did not.

        A file that cannot be read, is not YAML, or that *rows_of* refuses
        (``ValueError``: a wrong shape or a bad row) raises at startup, naming
        it. On a later reload it is logged as an ERROR and ``None`` returned,
        so the rows last read stay in force. The file is not re-read until its
        mtime moves again, so the ERROR comes once per edit.
        """
        chosen = _resolve_path(str(getattr(self._events_cfg, field_name)))
        mtime: float | None = None
        if chosen.exists():
            try:
                mtime = float(chosen.stat().st_mtime)
            except OSError:
                mtime = None
        token = (str(chosen), mtime)
        if self._file_tokens.get(field_name) == token:
            return None
        self._file_tokens[field_name] = token
        try:
            return rows_of(_read_yaml(chosen), chosen)
        except ValueError as exc:
            if startup:
                raise ValueError(f"invalid events.{field_name}:\n  {exc}") from exc
            LOG.error("invalid events.%s, keeping the rows last read from it:\n  %s", field_name, exc)
            return None

    def _refresh_macro(self, *, startup: bool = False) -> None:
        """Recompose ``_macro_events`` from the inline rows plus the file.

        The file is only re-parsed when its mtime moves (``_reload_file``,
        also for a file that does not read); the inline rows are re-read
        every time, so a config change is always reflected.
        """
        cfg = self._events_cfg
        inline = [dict(row) for row in (cfg.blackouts or [])]
        if not cfg.blackout_file:
            self._macro_file_events = []
        else:
            rows = self._reload_file("blackout_file", _blackout_file_rows, startup=startup)
            if rows is not None:
                self._macro_file_events = rows
        self._macro_events = inline + self._macro_file_events

    def _refresh_earnings(self, *, startup: bool = False) -> None:
        """Recompose ``_earnings`` from the inline map plus the file, same
        separation as ``_refresh_macro``."""
        cfg = self._events_cfg
        if not cfg.earnings_file:
            self._earnings_file = {}
        else:
            dates = self._reload_file("earnings_file", _earnings_file_dates, startup=startup)
            if dates is not None:
                self._earnings_file = dates
        merged: dict[str, set[date]] = {sym: set(dates) for sym, dates in self._earnings_file.items()}
        for sym, dates in _earnings_map(cfg.earnings).items():
            merged.setdefault(sym, set()).update(dates)
        self._earnings = merged

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def matching_macro_event(self, symbol: str | None = None, now_dt: datetime | None = None) -> dict[str, Any] | None:
        """First enabled macro window covering *now_dt* (and *symbol*, when the
        event carries a ``symbols`` list). ``None`` when nothing matches."""
        self._refresh_macro()
        now_dt = now_dt or sessions.now_et()
        today = now_dt.date()
        today_weekday = _WEEKDAYS[today.weekday()]
        now_t = now_dt.time()
        symbol_key = str(symbol).upper().strip() if symbol else None
        # Every row passed blackout_row_errors (inline ones at load, file
        # ones on each read), so its date parses, its weekday is one, its
        # switches are booleans and its symbols are tickers. The date is
        # compared as a date: compared as text with today's ISO string, a
        # YAML datetime never matched, and the window blocked nothing
        # (2026-09-26).
        for event in self._macro_events:
            if not event.get("enabled", True):
                continue
            if event.get("date") is not None and _parse_date(event["date"]) != today:
                continue
            if event.get("weekday") is not None and weekday_token(event["weekday"]) != today_weekday:
                continue
            scoped = event.get("symbols")
            if scoped:
                allowed = {s.upper().strip() for s in scoped}
                if symbol_key is None or symbol_key not in allowed:
                    continue
            if is_time_in_window(now_t, event.get("start"), event.get("end")):
                return event
        return None

    def earnings_block_reason(self, symbol: str, now_dt: datetime | None = None) -> str | None:
        """Reason string when *symbol* is inside its earnings blackout, else
        ``None``.

        A date ``D`` blocks the ``earnings_block_sessions_before`` sessions up
        to it and the ``earnings_block_sessions_after`` sessions following it,
        ``D`` itself always included.
        """
        cfg = self._events_cfg
        self._refresh_earnings()
        key = str(symbol).upper().strip()
        dates = self._earnings.get(key)
        if not dates:
            return None
        today = (now_dt or sessions.now_et()).date()
        # Integers >= 0, checked at load.
        before = cfg.earnings_block_sessions_before
        after = cfg.earnings_block_sessions_after
        for event_date in sorted(dates):
            offset = _sessions_between(event_date, today)
            if -before <= offset <= after:
                when = "on" if offset == 0 else ("before" if offset < 0 else "after")
                return f"earnings_blackout({key} {event_date.isoformat()},{when},offset={offset:+d}session)"
        return None

    def entry_block_reason(self, symbol: str | None = None, now_dt: datetime | None = None) -> str | None:
        """Combined gate: the reason new entries are blocked right now, or
        ``None``. Macro windows are checked first, then earnings."""
        if not self._events_cfg.enabled:
            return None
        event = self.matching_macro_event(symbol=symbol, now_dt=now_dt)
        if event is not None and event.get("block_new_entries", True):
            return str(event.get("label") or "event_blackout")
        if symbol:
            return self.earnings_block_reason(symbol, now_dt=now_dt)
        return None

    def force_flatten_event(self, symbol: str | None = None, now_dt: datetime | None = None) -> dict[str, Any] | None:
        """The matching macro event when it demands open positions be flattened."""
        if not self._events_cfg.enabled:
            return None
        event = self.matching_macro_event(symbol=symbol, now_dt=now_dt)
        if event is not None and event.get("force_flatten", False):
            return event
        return None


def _parse_date(value: Any) -> date:
    """A YAML date (an unquoted ``2026-10-29``), a datetime, or a
    ``YYYY-MM-DD`` string; ``ValueError`` otherwise, for ``20260930`` and
    ``2026-W40-3`` too, which ``date.fromisoformat`` reads."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DATE.fullmatch(value.strip()):
        return date.fromisoformat(value.strip())
    raise ValueError(f"not a YYYY-MM-DD date: {value!r}")
