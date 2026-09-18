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

The file is re-read when its mtime changes, so a blackout can be added to a
running bot without a restart.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from .utils import now_et, parse_hhmm

LOG = logging.getLogger(__name__)

_WEEKDAY_TOKENS = {
    "0": "MON", "1": "TUE", "2": "WED", "3": "THU", "4": "FRI", "5": "SAT", "6": "SUN",
    "MONDAY": "MON", "TUESDAY": "TUE", "WEDNESDAY": "WED", "THURSDAY": "THU",
    "FRIDAY": "FRI", "SATURDAY": "SAT", "SUNDAY": "SUN",
}


def weekday_token(value: Any) -> str | None:
    """Normalise a weekday spelling (``0``/``MONDAY``/``Mon``) to ``MON``."""
    if value is None:
        return None
    token = str(value).strip().upper()
    return _WEEKDAY_TOKENS.get(token, token[:3] if token else None)


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
        self._macro_source: tuple[str, float | None] | None = None
        self._earnings_source: tuple[str, float | None] | None = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    @property
    def _events_cfg(self) -> Any:
        return getattr(self._config, "events", None)

    def _load_yaml(self, path_value: str, source_attr: str) -> Any | None:
        """Read *path_value* when its mtime moved; ``None`` means unchanged."""
        chosen = _resolve_path(path_value)
        mtime: float | None = None
        if chosen.exists():
            try:
                mtime = float(chosen.stat().st_mtime)
            except OSError:
                mtime = None
        token = (str(chosen), mtime)
        if getattr(self, source_attr) == token:
            return None
        setattr(self, source_attr, token)
        if not chosen.exists():
            LOG.warning("Event calendar file not found: %s", chosen)
            return []
        try:
            return yaml.safe_load(chosen.read_text()) or []
        except Exception as exc:
            LOG.warning("Failed to load event calendar file %s: %s", chosen, exc)
            return []

    def _refresh_macro(self) -> None:
        """Recompose ``_macro_events`` from the inline rows plus the file.

        The file is only re-parsed when its mtime moves; the inline rows are
        re-read every time, so a config change is always reflected.
        """
        cfg = self._events_cfg
        if cfg is None:
            return
        inline = [dict(row) for row in (getattr(cfg, "blackouts", None) or []) if isinstance(row, dict)]
        path_value = getattr(cfg, "blackout_file", None)
        if not path_value:
            self._macro_file_events = []
            self._macro_events = inline
            return
        payload = self._load_yaml(str(path_value), "_macro_source")
        if payload is not None:
            if isinstance(payload, dict):
                payload = payload.get("events", [])
            self._macro_file_events = [dict(row) for row in (payload or []) if isinstance(row, dict)]
        self._macro_events = inline + self._macro_file_events

    def _refresh_earnings(self) -> None:
        """Recompose ``_earnings`` from the inline map plus the file, same
        separation as ``_refresh_macro``."""
        cfg = self._events_cfg
        if cfg is None:
            return

        def _absorb(mapping: Any, into: dict[str, set[date]]) -> None:
            if not isinstance(mapping, dict):
                return
            for symbol, dates in mapping.items():
                key = str(symbol).upper().strip()
                if not key:
                    continue
                bucket = into.setdefault(key, set())
                for raw in (dates or []):
                    parsed = _coerce_date(raw)
                    if parsed is None:
                        LOG.warning("Ignoring unparseable earnings date %r for %s", raw, key)
                        continue
                    bucket.add(parsed)

        path_value = getattr(cfg, "earnings_file", None)
        if not path_value:
            self._earnings_file = {}
        else:
            payload = self._load_yaml(str(path_value), "_earnings_source")
            if payload is not None:
                if isinstance(payload, dict):
                    payload = payload.get("earnings", payload)
                fresh: dict[str, set[date]] = {}
                _absorb(payload, fresh)
                self._earnings_file = fresh

        merged: dict[str, set[date]] = {sym: set(dates) for sym, dates in self._earnings_file.items()}
        _absorb(getattr(cfg, "earnings", None), merged)
        self._earnings = merged

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def matching_macro_event(self, symbol: str | None = None, now_dt: datetime | None = None) -> dict[str, Any] | None:
        """First enabled macro window covering *now_dt* (and *symbol*, when the
        event carries a ``symbols`` list). ``None`` when nothing matches."""
        self._refresh_macro()
        now_dt = now_dt or now_et()
        today_iso = now_dt.date().isoformat()
        today_weekday = weekday_token(now_dt.weekday())
        now_t = now_dt.time()
        symbol_key = str(symbol).upper().strip() if symbol else None
        for event in self._macro_events:
            if not bool(event.get("enabled", True)):
                continue
            event_date = event.get("date")
            if event_date and str(event_date) != today_iso:
                continue
            event_weekday = weekday_token(event.get("weekday")) if event.get("weekday") is not None else None
            if event_weekday and event_weekday != today_weekday:
                continue
            scoped = event.get("symbols")
            if scoped:
                allowed = {str(s).upper().strip() for s in scoped if str(s or "").strip()}
                if symbol_key is None or symbol_key not in allowed:
                    continue
            start = event.get("start")
            end = event.get("end")
            if not start or not end:
                continue
            if parse_hhmm(str(start)) <= now_t <= parse_hhmm(str(end)):
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
        if cfg is None:
            return None
        self._refresh_earnings()
        key = str(symbol).upper().strip()
        dates = self._earnings.get(key)
        if not dates:
            return None
        today = (now_dt or now_et()).date()
        before = max(0, int(getattr(cfg, "earnings_block_sessions_before", 1) or 0))
        after = max(0, int(getattr(cfg, "earnings_block_sessions_after", 1) or 0))
        for event_date in sorted(dates):
            offset = _sessions_between(event_date, today)
            if -before <= offset <= after:
                when = "on" if offset == 0 else ("before" if offset < 0 else "after")
                return f"earnings_blackout({key} {event_date.isoformat()},{when},offset={offset:+d}session)"
        return None

    def entry_block_reason(self, symbol: str | None = None, now_dt: datetime | None = None) -> str | None:
        """Combined gate: the reason new entries are blocked right now, or
        ``None``. Macro windows are checked first, then earnings."""
        cfg = self._events_cfg
        if cfg is None or not bool(getattr(cfg, "enabled", True)):
            return None
        event = self.matching_macro_event(symbol=symbol, now_dt=now_dt)
        if event is not None and bool(event.get("block_new_entries", True)):
            return str(event.get("label") or "event_blackout")
        if symbol:
            return self.earnings_block_reason(symbol, now_dt=now_dt)
        return None

    def force_flatten_event(self, symbol: str | None = None, now_dt: datetime | None = None) -> dict[str, Any] | None:
        """The matching macro event when it demands open positions be flattened."""
        cfg = self._events_cfg
        if cfg is None or not bool(getattr(cfg, "enabled", True)):
            return None
        event = self.matching_macro_event(symbol=symbol, now_dt=now_dt)
        if event is not None and bool(event.get("force_flatten", False)):
            return event
        return None


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        return None
