# SPDX-License-Identifier: MIT
"""LEVELONE_EQUITIES quote books, kept from the Schwab stream (2026-10-06).

The websocket that carries CHART_EQUITY also subscribes LEVELONE_EQUITIES for
the stream's quote symbols (``MarketDataStore.start_streaming``). Schwab sends
a snapshot of each symbol's requested fields after its subscription, then only
the fields that change; ``StreamQuoteState`` merges every item into one book
per subscribed symbol.

Threads. schwabdev calls the store's receiver on the stream's thread, which
hands each message to ``StreamQuoteState.on_message``; the engine's thread
subscribes, reads, prunes and clears. One ``threading.Lock`` guards all of
it, and it is the books' own: it is never taken with the store's lock held,
nor the store's with it, and no log call or I/O runs under it (each method
collects its log lines and emits them after releasing it). The stream thread
waits for it; an engine-thread acquire waits ``LOCK_TIMEOUT_SECONDS`` at most
and raises ``StreamQuoteLockTimeout`` instead, so no stream-thread defect can
stall the engine (the store then replaces the state with an empty one).

Epochs. Every ADMIN LOGIN response starts an epoch (schwabdev passes it to the
receiver before any data of its connection, ``stream.py:91``): every book is
emptied, so nothing from an earlier connection is served. ``on_message`` never
raises: schwabdev reads an exception out of its receiver as a broken
connection and reconnects (``stream.py:133-136``), CHART_EQUITY with it. A
malformed item empties its symbol's book; data that cannot be attributed to a
symbol empties every book; anything else that raises empties every book and
logs an ERROR. A book serves (``read``) only while complete: bid, ask, last and
mark received in this epoch, Schwab's ``"delayed": false`` seen, and a
LEVELONE_EQUITIES data message within the caller's limit.

Layer 1 (market data): the standard library only; the caller passes the clock.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Iterator, Mapping

LOG = logging.getLogger(__name__)

LEVELONE_EQUITIES = "LEVELONE_EQUITIES"

# The LEVELONE_EQUITIES fields the books keep: the L1 field id (schwabdev 4.0.0
# ``translate.stream_fields["LEVELONE_EQUITIES"]``, the index in its list) ->
# (the section of Schwab's REST quote that names the same value, its name
# there, its kind). The core four (bid, ask, last, mark), the fields
# ``MarketDataStore._normalize_quote`` reads (volume, close, open, net change,
# net percent change, description), the exchange name the dashboard prefers
# and the quote time. Net change and net percent change are signed: a symbol
# below its previous close sends negative ones.
FIELDS: dict[str, tuple[str, str, str]] = {
    "1": ("quote", "bidPrice", "price"),
    "2": ("quote", "askPrice", "price"),
    "3": ("quote", "lastPrice", "price"),
    "8": ("quote", "totalVolume", "count"),
    "12": ("quote", "closePrice", "price"),
    "15": ("reference", "description", "text"),
    "17": ("quote", "openPrice", "price"),
    "18": ("quote", "netChange", "signed"),
    "25": ("reference", "exchangeName", "text"),
    "33": ("quote", "mark", "price"),
    "34": ("quote", "quoteTime", "ms"),
    "42": ("quote", "netPercentChange", "signed"),
}
# The fields requested: 0 (the symbol) and the kept ones.
STREAM_QUOTE_FIELDS: tuple[int, ...] = (0, *sorted(int(fid) for fid in FIELDS))
# A book serves only with every one of these received in its epoch.
CORE_FIELDS = ("1", "2", "3", "33")
# The fields a book may lack that its symbol's previous cached quote fills in
# when the book is published: open, close, description, exchange name, net
# change and net percent change. They are display fields, except that the
# 0DTE regime reads its volatility symbol's close, net change and percent
# change (``RegimeMixin._vix_read``): a carried value is then the previous
# quote's (the shipped presets' VIX is an index, which never streams). A book
# can complete from deltas alone (after a rejected item or a prune, or an ADD
# Schwab answers without a snapshot), and these rarely change. The core prices
# never carry over: a book serves only with all four received in its epoch.
CARRY_OVER_FIELDS = ("12", "15", "17", "18", "25", "42")
# Schwab's success codes: 0 (LOGIN, LOGOUT), 26-29 (SUBS, UNSUBS, ADD, VIEW).
STREAM_OK_CODES = frozenset({0, 26, 27, 28, 29})
# The longest an engine-thread acquire of the books' lock waits.
LOCK_TIMEOUT_SECONDS = 0.5
# The health line's cadence (``StreamQuoteState.maybe_log_health``).
HEALTH_LOG_SECONDS = 300.0
# The longest a message, item or notice quoted in a log line gets.
LOG_TEXT_CHARS = 400

_ABSENT = object()


class MalformedStreamItem(ValueError):
    """A LEVELONE_EQUITIES item with a field the book cannot take."""


class StreamQuoteLockTimeout(RuntimeError):
    """An engine-thread acquire of the books' lock did not get it within
    ``LOCK_TIMEOUT_SECONDS``."""


def _clip(text: str) -> str:
    return text if len(text) <= LOG_TEXT_CHARS else text[:LOG_TEXT_CHARS] + "..."


def _log_text(value: Any) -> str:
    """``value`` for a log line: its JSON when it has one (a message's parts
    are JSON), else its repr, cut to ``LOG_TEXT_CHARS``."""
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        text = repr(value)
    return _clip(text)


def _checked(fid: str, kind: str, value: Any) -> Any:
    """``value`` as field ``fid`` of ``kind`` may hold it, ``_ABSENT`` for a
    text field's null; anything else raises ``MalformedStreamItem``."""
    if kind == "text":
        if value is None:
            return _ABSENT
        if not isinstance(value, str):
            raise MalformedStreamItem(f"field {fid} is not text: {value!r}")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedStreamItem(f"field {fid} is not a number: {value!r}")
    if kind == "ms":
        if not isinstance(value, int) or value <= 0:
            raise MalformedStreamItem(f"field {fid} is not a positive integer: {value!r}")
        return value
    number = float(value)
    if not math.isfinite(number):
        raise MalformedStreamItem(f"field {fid} is not finite: {value!r}")
    if kind in ("price", "count") and number < 0:
        raise MalformedStreamItem(f"field {fid} is negative: {value!r}")
    return value


def rest_payload(symbol: str, values: Mapping[str, Any], previous: Any = None) -> dict:
    """A book's values as Schwab's REST quote names them (``{"symbol",
    "quote": {...}, "reference": {...}}``), so ``MarketDataStore._normalize_quote``
    reads a stream quote exactly as a REST one. Each ``CARRY_OVER_FIELDS``
    field the book lacks is taken from ``previous`` (the raw payload of the
    symbol's previous cached quote, REST- or stream-made) where it holds one."""
    payload: dict[str, Any] = {"symbol": symbol, "quote": {}, "reference": {}}
    for fid, value in values.items():
        spec = FIELDS.get(fid)
        if spec is not None:
            payload[spec[0]][spec[1]] = value
    if isinstance(previous, Mapping):
        for fid in CARRY_OVER_FIELDS:
            if fid in values:
                continue
            section, name, _kind = FIELDS[fid]
            source = previous.get(section)
            if isinstance(source, Mapping) and source.get(name) is not None:
                payload[section][name] = source[name]
    return payload


def _item_symbol(item: Any) -> str | None:
    """The symbol an item names (its ``key``, else its field ``0``, as the
    CHART_EQUITY parser reads), upper-cased; None when it names none."""
    if not isinstance(item, dict):
        return None
    for name in ("key", "0"):
        value = item.get(name)
        if isinstance(value, str) and value.strip():
            return value.upper().strip()
    return None


@dataclass(slots=True)
class StreamQuoteBook:
    """One symbol's LEVELONE_EQUITIES fields in this epoch.

    ``values`` holds the kept fields by L1 id; ``delayed`` the last
    ``"delayed"`` flag seen (None before any); ``announced`` whether the
    first-complete line was logged."""

    values: dict[str, Any] = field(default_factory=dict)
    delayed: bool | None = None
    announced: bool = False

    @property
    def complete(self) -> bool:
        """Bid, ask, last and mark received, and Schwab's ``"delayed": false``
        the last flag seen."""
        return self.delayed is False and all(fid in self.values for fid in CORE_FIELDS)

    def merge(self, item: Mapping[str, Any]) -> None:
        """Apply one item's fields whole or not at all: every kept field is
        checked first (``MalformedStreamItem`` names the field and its value).
        The item's ``"delayed"`` flag is taken before its fields are checked:
        a ``"delayed": true`` empties the values whatever else the item holds,
        and none accumulate until a ``"delayed": false`` arrives (its item's
        fields with it); a ``"delayed": false`` counts even when another field
        of its item is rejected, so a snapshot rejected for one field (Schwab
        sends the flag in the snapshot only) leaves its book to complete from
        the deltas that follow. Field ids the books do not keep and the item's
        other keys (``key``, ``assetMainType``, ...) are ignored."""
        delayed = item.get("delayed", _ABSENT)
        if delayed is not _ABSENT and not isinstance(delayed, bool):
            raise MalformedStreamItem(f"delayed is not true or false: {delayed!r}")
        if delayed is True:
            self.values.clear()
            self.delayed = True
            return
        if delayed is False:
            self.delayed = False
        updates: dict[str, Any] = {}
        for fid, value in item.items():
            spec = FIELDS.get(fid) if isinstance(fid, str) else None
            if spec is None:
                continue
            checked = _checked(fid, spec[2], value)
            if checked is not _ABSENT:
                updates[fid] = checked
        if self.delayed is True:
            return
        self.values.update(updates)

    def drop(self) -> None:
        """Empty the values, keeping the delayed flag (Schwab may send it in
        the snapshot only): the book completes again once bid, ask, last and
        mark have each arrived again."""
        self.values.clear()


@dataclass(slots=True)
class HealthCounters:
    """What the stream delivered since the last health line: messages of any
    kind, LEVELONE_EQUITIES data packets, heartbeats, responses; items
    merged into a book, for a symbol not subscribed, rejected or without a
    symbol (bad), and delayed; the longest gap between two messages, two
    LEVELONE_EQUITIES data packets and two heartbeats (each measured when
    the later one arrives); and the largest lag of a data packet's receipt
    behind its Schwab timestamp (None before any)."""

    messages: int = 0
    data: int = 0
    heartbeats: int = 0
    responses: int = 0
    items: int = 0
    unsubscribed_items: int = 0
    bad_items: int = 0
    delayed_items: int = 0
    max_gap_s: float = 0.0
    max_data_gap_s: float = 0.0
    max_heartbeat_gap_s: float = 0.0
    max_lag_ms: float | None = None


def _gap(previous: datetime | None, now: datetime) -> float:
    return 0.0 if previous is None else max(0.0, (now - previous).total_seconds())


@dataclass(frozen=True, slots=True)
class StreamQuoteRead:
    """One read of the books: ``books`` holds a copy of the values of each
    requested symbol that is fresh; ``at`` is the receipt time of the last
    LEVELONE_EQUITIES data message of this epoch (None before any); ``live``
    whether that is within the read's limit; ``epoch`` the LOGIN responses
    seen; ``subscribed`` how many symbols are subscribed."""

    books: dict[str, dict[str, Any]]
    at: datetime | None
    live: bool
    epoch: int
    subscribed: int


class StreamQuoteState:
    """One stream's LEVELONE_EQUITIES subscription mirror and books (the
    module docstring has the threads, the epochs and the lock's rules).

    Invariant: ``_books`` has exactly the subscribed symbols as keys."""

    def __init__(self, epoch: int = 0) -> None:
        self._lock = threading.Lock()
        self._symbols: frozenset[str] = frozenset()
        self._books: dict[str, StreamQuoteBook] = {}
        self._epoch = epoch
        self._last_data_at: datetime | None = None
        self._warned: set[tuple[str, str]] = set()
        self._first_item_logged = False
        # The health line: the period's counters, when the last line was
        # logged, and the receipt of the last message, data packet and
        # heartbeat (for the gaps; a LOGIN does not reset them).
        self._period = HealthCounters()
        self._last_health_at: datetime | None = None
        self._last_message_at: datetime | None = None
        self._last_data_received_at: datetime | None = None
        self._last_heartbeat_at: datetime | None = None

    def successor(self) -> StreamQuoteState:
        """An empty state to replace this one when its lock is stuck
        (``MarketDataStore._reset_stream_quotes``): nothing subscribed, no
        book, but this state's epoch count, read without the lock (the stuck
        thread holds it; one int read is atomic), and at least 1. The
        connection whose login started that epoch is still the stream's, so
        once the next subscription's snapshot fills the new books they serve,
        with no new login. At least 1: when the stuck thread is inside the
        stream's first LOGIN response, the count read is still 0, and a state
        at 0 would serve nothing until the next reconnect; a new state's books
        fill only from data that arrives after it exists, which flows only on
        a logged-in connection."""
        return StreamQuoteState(epoch=max(1, self._epoch))

    # ------------------------------------------------------------ engine thread
    @contextmanager
    def _engine_lock(self, what: str) -> Iterator[None]:
        if not self._lock.acquire(timeout=LOCK_TIMEOUT_SECONDS):
            raise StreamQuoteLockTimeout(f"{what}: the stream quote books' lock was not free within "
                                         f"{LOCK_TIMEOUT_SECONDS} s")
        try:
            yield
        finally:
            self._lock.release()

    def diff(self, wanted: Iterable[str]) -> tuple[list[str], list[str], bool]:
        """The subscription change ``wanted`` needs: the symbols to add and to
        remove (each sorted), and whether nothing is subscribed now (so the
        add is a SUBS)."""
        wanted = frozenset(wanted)
        with self._engine_lock("diff"):
            current = self._symbols
        return sorted(wanted - current), sorted(current - wanted), not current

    def apply(self, *, add: Iterable[str], remove: Iterable[str]) -> None:
        """Commit a subscription change before it is sent: each added symbol
        gets an empty book (the snapshot that follows the request lands in
        it), each removed one's book is dropped at once (an item for it from
        then on is dropped). Swapping ``add`` and ``remove`` reverts it."""
        add, remove = frozenset(add), frozenset(remove)
        with self._engine_lock("subscription change"):
            for symbol in remove:
                self._books.pop(symbol, None)
            for symbol in add:
                self._books[symbol] = StreamQuoteBook()
            self._symbols = (self._symbols - remove) | add

    def clear(self) -> None:
        """Nothing subscribed and no book (a stop): an item after it is
        dropped. The epoch count stays."""
        with self._engine_lock("clear"):
            self._symbols = frozenset()
            self._books = {}
            self._last_data_at = None
            self._last_data_received_at = None
            self._last_heartbeat_at = None
            self._last_message_at = None

    def prune(self, active: Iterable[str]) -> int:
        """Empty the book of every subscribed symbol outside ``active`` (the
        store's prune): it serves again only once its bid, ask, last and mark
        have each arrived again. The subscription is the next diff's to
        change. Returns how many books held values."""
        active = frozenset(active)
        with self._engine_lock("prune"):
            dropped = 0
            for symbol, book in self._books.items():
                if symbol not in active and book.values:
                    book.drop()
                    dropped += 1
            return dropped

    def drop(self, symbols: Iterable[str]) -> None:
        """Empty these symbols' books (a publication that failed): each
        serves again once its bid, ask, last and mark have arrived again."""
        with self._engine_lock("drop"):
            for symbol in symbols:
                book = self._books.get(symbol)
                if book is not None:
                    book.drop()

    def subscribed(self) -> frozenset[str]:
        with self._engine_lock("subscribed"):
            return self._symbols

    def maybe_log_health(self, now: datetime, ttl: float, transitions: int) -> bool:
        """Every ``HEALTH_LOG_SECONDS`` after a login while anything is
        subscribed, one INFO line (``Stream quotes health: ...``) of the
        period's counters and the state of every book: the symbols that
        cannot serve and why (``waiting`` for their snapshot,
        ``partial:<the core ids missing>``, ``no-delayed-flag``, ``delayed``);
        ``live=no`` means none serves (no LEVELONE_EQUITIES data within
        ``ttl``). ``transitions`` is the store's count of silent/live changes
        in the period. True when it logged (the counters start over)."""
        with self._engine_lock("health"):
            if self._epoch < 1 or not self._symbols:
                return False
            if self._last_health_at is not None and (now - self._last_health_at).total_seconds() < HEALTH_LOG_SECONDS:
                return False
            period, self._period = self._period, HealthCounters()
            self._last_health_at = now
            at = self._last_data_at
            live = at is not None and (now - at).total_seconds() < ttl
            rest = []
            complete = 0
            for symbol in sorted(self._symbols):
                book = self._books[symbol]
                if book.complete:
                    complete += 1
                elif book.delayed is True:
                    rest.append(f"{symbol}(delayed)")
                elif not book.values:
                    rest.append(f"{symbol}(waiting)")
                else:
                    missing = ",".join(fid for fid in CORE_FIELDS if fid not in book.values)
                    rest.append(f"{symbol}(partial:{missing})" if missing else f"{symbol}(no-delayed-flag)")
            args = (self._epoch, "yes" if live else "no",
                    "na" if at is None else f"{(now - at).total_seconds():.1f}", len(self._symbols), complete,
                    ",".join(rest), period.messages, period.data, period.heartbeats, period.responses, period.items,
                    period.unsubscribed_items, period.bad_items, period.delayed_items, period.max_gap_s,
                    period.max_data_gap_s, period.max_heartbeat_gap_s,
                    "na" if period.max_lag_ms is None else f"{period.max_lag_ms:.0f}", transitions)
        _emit([(logging.INFO, "Stream quotes health: epoch=%d live=%s data_age_s=%s subscribed=%d complete=%d "
                              "rest=[%s] messages=%d data=%d heartbeats=%d responses=%d items=%d "
                              "unsubscribed_items=%d bad_items=%d delayed_items=%d max_gap_s=%.1f "
                              "max_data_gap_s=%.1f max_heartbeat_gap_s=%.1f max_lag_ms=%s transitions=%d", args)])
        return True

    def read(self, symbols: Iterable[str], now: datetime, ttl: float) -> StreamQuoteRead:
        """The fresh books among ``symbols`` (each a copy made under the lock):
        a symbol is fresh while it is subscribed, a LOGIN started this epoch,
        its book is complete in it, and the last LEVELONE_EQUITIES data
        message arrived less than ``ttl`` seconds before ``now``."""
        with self._engine_lock("read"):
            at = self._last_data_at
            live = self._epoch >= 1 and at is not None and (now - at).total_seconds() < ttl
            books: dict[str, dict[str, Any]] = {}
            if live:
                for symbol in symbols:
                    book = self._books.get(symbol)
                    if book is not None and book.complete:
                        books[symbol] = dict(book.values)
            return StreamQuoteRead(books=books, at=at, live=live, epoch=self._epoch, subscribed=len(self._symbols))

    # ------------------------------------------------------------ stream thread
    def on_message(self, payload: Any, now: datetime) -> None:
        """Merge one stream message (the parsed JSON) received at ``now``.
        Never raises (the module docstring says why)."""
        lines: list[tuple[int, str, tuple]] = []
        try:
            with self._lock:
                self._on_message_locked(payload, now, lines)
        except Exception as exc:
            # The lock is free here (``with`` released it): take it again to
            # drop every book, since the failure may have left any of them
            # half merged.
            try:
                with self._lock:
                    self._drop_all_locked()
            except Exception as again:
                lines.append((logging.ERROR, "Stream quotes: %s dropping every book after a failure (%s)",
                              (type(again).__name__, again)))
            lines.append((logging.ERROR, "Stream quotes: %s handling a stream message (%s); every book dropped: %s",
                          (type(exc).__name__, exc, _log_text(payload))))
        _emit(lines)

    def _on_message_locked(self, payload: Any, now: datetime, lines: list) -> None:
        if not isinstance(payload, dict):
            return                       # not a Schwab message: the CHART_EQUITY handler warns
        period = self._period
        period.messages += 1
        period.max_gap_s = max(period.max_gap_s, _gap(self._last_message_at, now))
        self._last_message_at = now
        responses = payload.get("response")
        if responses is not None:
            self._on_responses_locked(responses, lines)
        notices = payload.get("notify")
        if notices is not None:
            self._on_notices_locked(notices, now, lines)
        data = payload.get("data")
        if data:
            self._on_data_locked(data, now, lines)

    def _on_responses_locked(self, responses: Any, lines: list) -> None:
        if not isinstance(responses, list):
            lines.append((logging.WARNING, "Schwab stream response not understood: %s", (_log_text(responses),)))
            return
        for response in responses:
            self._period.responses += 1
            if not isinstance(response, dict):
                lines.append((logging.WARNING, "Schwab stream response not understood: %s", (_log_text(response),)))
                continue
            content = response.get("content") if isinstance(response.get("content"), dict) else {}
            code, msg = content.get("code"), content.get("msg")
            # Schwab's codes are integers; anything else is unknown (WARNING).
            known = isinstance(code, int) and not isinstance(code, bool)
            if response.get("service") == "ADMIN" and response.get("command") == "LOGIN":
                self._new_epoch_locked()
                level = logging.INFO if known and code == 0 else logging.WARNING
                lines.append((level, "Schwab stream login: epoch %d code=%r msg=%s; stream quotes wait for %d "
                                     "symbols' snapshots", (self._epoch, code, msg, len(self._symbols))))
            else:
                level = logging.INFO if known and code in STREAM_OK_CODES else logging.WARNING
                lines.append((level, "Schwab stream response service=%s command=%s code=%r msg=%s",
                              (response.get("service"), response.get("command"), code, msg)))

    def _on_notices_locked(self, notices: Any, now: datetime, lines: list) -> None:
        if not isinstance(notices, list):
            lines.append((logging.WARNING, "Schwab stream notice: %s", (_log_text(notices),)))
            return
        for notice in notices:
            if isinstance(notice, dict) and "heartbeat" in notice:
                period = self._period
                period.heartbeats += 1
                period.max_heartbeat_gap_s = max(period.max_heartbeat_gap_s, _gap(self._last_heartbeat_at, now))
                self._last_heartbeat_at = now
                continue
            lines.append((logging.WARNING, "Schwab stream notice: %s", (_log_text(notice),)))

    def _on_data_locked(self, data: Any, now: datetime, lines: list) -> None:
        if not isinstance(data, list):
            self._drop_all_locked()
            self._period.bad_items += 1
            lines.append((logging.WARNING, "Stream quotes: unattributable stream data (not a list); every book "
                                           "dropped: %s", (_log_text(data),)))
            return
        for packet in data:
            if not isinstance(packet, dict):
                self._drop_all_locked()
                self._period.bad_items += 1
                lines.append((logging.WARNING, "Stream quotes: unattributable stream data (a packet that is not an "
                                               "object); every book dropped: %s", (_log_text(packet),)))
                continue
            if packet.get("service") != LEVELONE_EQUITIES:
                continue                 # CHART_EQUITY is the store's
            content = packet.get("content")
            if not isinstance(content, list):
                self._drop_all_locked()
                self._period.bad_items += 1
                lines.append((logging.WARNING, "Stream quotes: unattributable LEVELONE_EQUITIES data (content is "
                                               "not a list); every book dropped: %s", (_log_text(packet),)))
                continue
            self._last_data_at = now
            period = self._period
            period.data += 1
            period.max_data_gap_s = max(period.max_data_gap_s, _gap(self._last_data_received_at, now))
            self._last_data_received_at = now
            stamp = packet.get("timestamp")
            if isinstance(stamp, int) and not isinstance(stamp, bool) and stamp > 0:
                lag = now.timestamp() * 1000.0 - stamp
                period.max_lag_ms = lag if period.max_lag_ms is None else max(period.max_lag_ms, lag)
            for item in content:
                self._on_item_locked(item, lines)

    def _on_item_locked(self, item: Any, lines: list) -> None:
        symbol = _item_symbol(item)
        if symbol is None:
            # A missed delta could leave any book stale.
            self._drop_all_locked()
            self._period.bad_items += 1
            lines.append((logging.WARNING, "Stream quotes: unattributable LEVELONE_EQUITIES item (no symbol); every "
                                           "book dropped: %s", (_log_text(item),)))
            return
        if not self._first_item_logged:
            self._first_item_logged = True
            lines.append((logging.INFO, "Stream quotes: first LEVELONE_EQUITIES item of epoch %d: %s",
                          (self._epoch, _log_text(item))))
        book = self._books.get(symbol)
        if book is None:
            self._period.unsubscribed_items += 1
            return                       # not subscribed (an UNSUBS in flight, or one that failed)
        self._period.items += 1
        if item.get("delayed") is True:
            self._period.delayed_items += 1
        was_delayed = book.delayed is True
        try:
            book.merge(item)
        except Exception as exc:
            book.drop()
            self._period.bad_items += 1
            self._warn_once_locked(symbol, "rejected", lines,
                                   "Stream quotes: %s item rejected (%s: %s); its book waits for its fields again",
                                   (symbol, type(exc).__name__, exc))
            return
        if book.delayed is True and not was_delayed:
            self._warn_once_locked(symbol, "delayed", lines, "Stream quotes: %s is delayed; its quotes stay on REST",
                                   (symbol,))
        if not book.announced and book.complete:
            book.announced = True
            lines.append((logging.INFO, "Stream quotes: first complete LEVELONE_EQUITIES book for %s (epoch %d)",
                          (symbol, self._epoch)))

    def _new_epoch_locked(self) -> None:
        self._epoch += 1
        self._books = {symbol: StreamQuoteBook() for symbol in self._symbols}
        self._last_data_at = None
        self._warned.clear()
        self._first_item_logged = False

    def _drop_all_locked(self) -> None:
        for book in self._books.values():
            book.drop()

    def _warn_once_locked(self, symbol: str, kind: str, lines: list, fmt: str, args: tuple) -> None:
        """A WARNING the first time (``symbol``, ``kind``) happens in this
        epoch, DEBUG after."""
        level = logging.DEBUG if (symbol, kind) in self._warned else logging.WARNING
        self._warned.add((symbol, kind))
        lines.append((level, fmt, args))


def _emit(lines: list[tuple[int, str, tuple]]) -> None:
    """Log the collected lines, after the lock is released. On the stream
    thread nothing may raise, a failing log handler included."""
    for level, fmt, args in lines:
        try:
            LOG.log(level, fmt, *args)
        except Exception:
            continue
