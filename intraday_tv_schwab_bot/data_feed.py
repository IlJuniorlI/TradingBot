# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from threading import RLock, Thread
from typing import Any, Callable, Iterable, Mapping, NamedTuple

import pandas as pd
from schwabdev import Client, Stream

from .config import BotConfig
from .support_resistance import SupportResistanceContext, build_support_resistance_context, flip_frame_clock_key
from .htf_levels import HTFContext, build_htf_context
from .fair_value_gaps import FairValueGapContext, build_fair_value_gap_context, empty_fvg_context
from .order_blocks import OrderBlockContext, build_order_block_context, empty_order_block_context
from .numeric import first_float, safe_float
from .symbols import QUOTE_SYMBOL_ALIASES, STREAMABLE_EQUITY_RE, is_streamable_equity, is_support_resistance_symbol
from .schwab_api import call_schwab_client, call_schwab_json, response_ok
from .stream_quotes import (
    LEVELONE_EQUITIES,
    STREAM_QUOTE_FIELDS,
    StreamQuoteLockTimeout,
    StreamQuoteRead,
    StreamQuoteState,
    rest_payload,
)
from .bars import (
    completed_bucket_mask,
    ensure_ohlcv_frame,
    equity_stream_window_bars,
    floor_minute,
    forming_positions,
    frame_version,
    next_source_generation,
    register_frame_source,
    resample_bars,
    retain_derived_frames,
    session_bucket_floor,
)
from .indicators import (
    ensure_standard_indicator_frame,
    get_runtime_indicator_mode,
    get_session_indicator_window,
    indicator_session_open,
    resolve_ema_spans,
)
from . import sessions
from .context_memo import ContextMemo, indicator_clock_key
from .sessions import (
    EQUITY_STREAM_HISTORY_REFRESH_READY,
    EXCHANGE_TZ,
    is_equity_stream_session,
    is_regular_equity_session,
    is_weekday_session_day,
    latest_session_date,
)

LOG = logging.getLogger(__name__)

# The price keys a quote-cache entry (``MarketDataStore._normalize_quote``
# holds bid, ask, mid, mark, last and close) is read by, the first above
# zero winning (``numeric.first_float(quote, *keys, positive=True)``). Stop
# and target decisions read the mark first: in a wide spread the bid or ask
# can trigger a stop the traded price never reached. Pricing an equity limit
# order reads the last trade first, as does the dashboard, which falls back
# as far as the bid and ask.
MANAGEMENT_PRICE_KEYS = ("mark", "last", "close")
EXECUTION_LAST_KEYS = ("last", "mark", "close")
DISPLAY_PRICE_KEYS = ("last", "mark", "mid", "close", "bid", "ask")

# A daily-history fetch that raised is fetched again this long after it by
# the engine's prefetch while the day's first entry window has not opened
# (``MarketDataStore.daily_history_due``, ``IntradayBot._prefetch_daily_history``);
# from the window on it stays cached for the day.
DAILY_HISTORY_RETRY_SECONDS = 60.0

# The longest a stream message, packet or item quoted in a log line gets.
STREAM_LOG_TEXT_CHARS = 400

# After a stream subscription send fails, none is tried again for this long,
# nor while the stream is not active (``MarketDataStore._stream_send_due``).
STREAM_SEND_RETRY_SECONDS = 60.0

# A stream quote transition (live to silent, or back) is logged at WARNING
# (silent) or INFO (live) unless one of its kind was logged that loudly this
# long before; then at DEBUG (settled L6: flaps in quiet periods).
STREAM_QUOTE_TRANSITION_QUIET_SECONDS = 300.0

# The stream quotes' REST shadow (MarketDataStore.run_stream_quote_shadow)
# skips for STREAM_QUOTE_SHADOW_BACKOFF_SECONDS after a REST quote request
# that failed (raised or answered a non-2xx status) or took longer than
# STREAM_QUOTE_SHADOW_SLOW_SECONDS (settled M4): in a REST outage it adds no
# request to wait out.
STREAM_QUOTE_SHADOW_BACKOFF_SECONDS = 60.0
STREAM_QUOTE_SHADOW_SLOW_SECONDS = 2.0

# A CHART_EQUITY bar is stamped with its minute's start, so a real bar's
# chart time is before its receipt; one stamped more than this after it is
# malformed (the allowance covers a local clock up to a minute behind
# Schwab's). Kept, such a bar would be the frame's last bar from then on:
# every later bar sorts before it, and the symbol's latest bar never ages.
STREAM_BAR_MAX_LEAD_SECONDS = 60.0


def _check_text(value: Any) -> str:
    """A price on the ``Stream quote check`` line: its repr, or ``na``."""
    return "na" if value is None else repr(value)


def _check_delta(rest: Any, stream: Any) -> str:
    """Stream minus REST on the ``Stream quote check`` line, or ``na``."""
    try:
        return f"{float(stream) - float(rest):+.4f}"
    except (TypeError, ValueError):
        return "na"


def _quote_time(value: Any) -> int | None:
    """A quote time in epoch milliseconds, or None when ``value`` is not a
    finite number (a bool is not)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(value)


def _stream_log_text(value: Any) -> str:
    """``value``'s repr for a log line, cut to ``STREAM_LOG_TEXT_CHARS``."""
    text = repr(value)
    return text if len(text) <= STREAM_LOG_TEXT_CHARS else text[:STREAM_LOG_TEXT_CHARS] + "..."


@dataclass(slots=True)
class MergeStats:
    history_rows: int = 0
    stream_rows: int = 0


class _HTFCacheEntry(NamedTuple):
    """An HTF context, the ``history_htf`` frame object it was built from,
    the session date its prior day/week were measured back from, and whether
    it was built inside the session (``indicators.indicator_session_open``).

    ``get_htf_context`` serves ``context`` only while ``frame`` IS still the
    stored frame (identity, not equality), ``as_of`` is still the latest
    session date and the session state is unchanged. Every refresh stores a
    new frame object, so every cache key rebuilds from it on its next read,
    whichever caller's read triggered the refresh; and a read after midnight
    rebuilds even before the first refresh of the day, or the dashboard's
    reads served yesterday's prior day/week until the prewarm.

    The session state is in it because a build reads the clock (2026-09-24):
    inside the session the ATR and the divergence age come from session bars
    only, outside it from every bar. At the open the stored frame object is
    unchanged until the first refresh after the 09:30 boundary (and its 10 s
    settle) succeeds, so a context built on the premarket clock was served
    into the session until then, and for as long as that refresh failed.
    """

    frame: pd.DataFrame
    as_of: date
    session_open: bool
    context: HTFContext


class MarketDataStore:
    def __init__(self, client: Client, config: BotConfig):
        self.client = client
        self.config = config
        try:
            self.stream = Stream(client)
        except TypeError:
            self.stream = Stream()
        self.history: dict[str, pd.DataFrame] = {}
        # Daily OHLC bars, one fetch per symbol per ET date (see
        # get_daily_history). A None value is a cached failure for that
        # date, not "not fetched yet" — last_daily_refresh distinguishes.
        self.daily_history: dict[str, pd.DataFrame | None] = {}
        self.last_daily_refresh: dict[str, date] = {}
        # When today's fetch of a symbol raised, while no later fetch has
        # answered (daily_history_due retries it).
        self.daily_history_failed_at: dict[str, datetime] = {}
        self.live: dict[str, pd.DataFrame] = {}
        self.quote_cache: dict[str, dict] = {}
        # One HTF frame per (symbol, tf): completed bars only, inside the
        # 07:00-20:00 equity stream window (see _refresh_htf_frame).
        self.history_htf: dict[tuple[str, int], pd.DataFrame] = {}
        # Contexts per _htf_context_cache_key, each tagged with the frame it
        # was built from (see _HTFCacheEntry / get_htf_context).
        self.htf_cache: dict[tuple, _HTFCacheEntry] = {}
        # derive_from_htf_frame's results per ((symbol, tf), slot): (the
        # stored frame object it was built from, the indicator settings, the
        # result).
        self._htf_derived_memo: dict[tuple[tuple[str, int], str], tuple[pd.DataFrame | None, tuple, Any]] = {}
        self.last_htf_refresh: dict[tuple[str, int], datetime] = {}
        self.last_quote_refresh: dict[str, datetime] = {}
        # Per-symbol quote-failure tracking. Counter increments on each
        # quote-fetch failure, resets on success. Blacklist captures the
        # session-time when a symbol crossed the
        # `runtime.max_consecutive_quote_failures` threshold and silences
        # further fetch attempts for that symbol until bot restart.
        self._consecutive_quote_failures: dict[str, int] = {}
        self._quote_blacklist: dict[str, datetime] = {}
        self.merge_stats: dict[str, MergeStats] = defaultdict(MergeStats)
        self.last_history_refresh: dict[str, datetime] = {}
        self.last_stream_update: dict[str, datetime] = {}
        self.last_stream_bar_time: dict[str, pd.Timestamp] = {}
        self.last_empty_history_refresh: dict[str, datetime] = {}
        # Most bars any price_history fetch returned per symbol: the depth a
        # fresh start would hold. The deepest, not the latest, so one short
        # response (an API hiccup) cannot trim away good history; bounded all
        # the same, since every fetch is bounded by its lookback. See
        # _retain_window.
        self._history_window_rows: dict[str, int] = {}
        self.stream_symbols: set[str] = set()
        self.stream_start_requested_at: datetime | None = None
        self._stream_seen_symbols: set[str] = set()
        # Timestamp of each symbol's first CHART_EQUITY bar since the stream
        # (re)started or the symbol was (re)subscribed. History fetched before
        # that bar leaves a hole up to it; should_backfill_stream_symbol
        # refetches once to close it.
        self._stream_first_bar_time: dict[str, pd.Timestamp] = {}
        self.last_stream_health_log: dict[str, datetime] = {}
        # The thread of this store's last Stream.start (schwabdev's private
        # Stream._thread, read right after it): see _stream_running.
        self._stream_thread: Thread | None = None
        # The open run of failed stream sends: the last failure's time and
        # how many in a row (main thread only; see _stream_send_due).
        self._stream_send_failed_at: datetime | None = None
        self._stream_send_failures = 0
        # The LEVELONE_EQUITIES subscription and its quote books
        # (runtime.stream_quotes): the stream thread merges each message into
        # them under their own lock, never this store's.
        self.stream_quotes = StreamQuoteState()
        # Whether the stream quotes were live at the last non-forced read
        # (None: nothing to judge, no login or nothing subscribed), the
        # transitions since, and when each kind was last logged loudly
        # (_log_stream_quote_transition).
        self._stream_quotes_live: bool | None = None
        self._stream_quote_transitions = 0
        self._stream_quote_transition_logged: dict[bool, datetime] = {}
        # Whether the entitlement line (_log_quote_entitlement) was logged:
        # once, from the first REST quote of a streamable equity.
        self._quote_entitlement_logged = False
        # The stream quotes' REST shadow (run_stream_quote_shadow): the
        # publications of stream quotes since its last check and the symbols
        # they served; the last REST quote request in trouble (when, what),
        # and whether the shadow's skip for it was logged.
        self._stream_quote_publications = 0
        self._stream_quote_served: set[str] = set()
        self._rest_quote_trouble: tuple[datetime, str] | None = None
        self._stream_quote_shadow_skipping = False
        self._lock = RLock()
        self.started_at = sessions.now_et()
        self._forced_premarket_history_refresh_date: dict[str, date] = {}
        self._cycle_active = False
        # Keys: base OHLCV = (symbol, tf, False); enriched =
        # (symbol, tf, True, span_scale, (ema fast, ema slow)). Values:
        # (frame, the (source token, variant) it was built as; see get_merged).
        self._cycle_merged_cache: dict[tuple, tuple[pd.DataFrame, tuple]] = {}
        # get_merged's frames across cycles, under the cycle cache's keys:
        # (history frame, live frame, indicator settings, frame). Every write
        # to `history` / `live` stores a new object and none writes into one
        # (fetch_history, on_stream_message, prune_inactive_symbols), so the
        # same two objects hold the same bars and the frame built from them
        # is the one a rebuild would make. The entry holds both objects, so
        # their ids cannot be reused while it lives.
        self._merged_memo: dict[tuple, tuple[pd.DataFrame | None, pd.DataFrame | None, tuple, pd.DataFrame]] = {}
        # symbol -> (history frame, live frame, generation): the provenance
        # generation of the symbol's current pair (bars.register_frame_source).
        self._source_generations: dict[str, tuple[pd.DataFrame | None, pd.DataFrame | None, int]] = {}
        self._cycle_htf_context_cache: dict[tuple, HTFContext | None] = {}
        self._cycle_fvg_cache: dict[tuple, FairValueGapContext] = {}
        self._cycle_ob_cache: dict[tuple, OrderBlockContext] = {}
        self._cycle_sr_cache: dict[tuple, SupportResistanceContext | None] = {}
        # The S/R, FVG and order-block builds across cycles (context_memo):
        # slots ("sr" | "fvg" | "ob", symbol, *request), each keyed on the
        # frame its build read and the clock as the build reads it.
        self._level_memo = ContextMemo("levels", shadow_every=config.runtime.context_memo_shadow_every)
        # Resolved Schwab API alias cache: original_symbol_upper -> resolved_alias.
        # Populated lazily after the first successful single-symbol fetch
        # (quote or price_history). Lets the batch quote path substitute
        # macro symbols (NYICDX, VIX) with their resolved aliases ($NYICDX,
        # $VIX) so they piggyback on the streamable batch instead of
        # firing separate per-cycle alias-retry calls.
        self._resolved_quote_alias: dict[str, str] = {}

    @staticmethod
    def is_regular_session(now: datetime | None = None) -> bool:
        return is_regular_equity_session(now)

    @staticmethod
    def is_equity_stream_session(now: datetime | None = None) -> bool:
        """Return whether Schwab equity chart streaming should be allowed.

        This is intentionally broader than regular session so configured
        premarket/postmarket entry or management windows can use streaming,
        while still preventing the stream from running overnight.
        """
        return is_equity_stream_session(now)

    def _should_force_7am_history_refresh(self, symbol: str, now: datetime, last: datetime | None) -> bool:
        refresh_ready_at = EQUITY_STREAM_HISTORY_REFRESH_READY
        if not is_weekday_session_day(now):
            return False
        if now.time() < refresh_ready_at:
            return False
        key = self._symbol_key(symbol)
        if self._forced_premarket_history_refresh_date.get(key) == now.date():
            return False
        if last is None or last.date() != now.date():
            return False
        if last.time() >= refresh_ready_at:
            self._forced_premarket_history_refresh_date[key] = now.date()
            return False
        return True

    def should_refresh_history(self, symbol: str) -> bool:
        key = self._symbol_key(symbol)
        now = sessions.now_et()
        last = self.last_history_refresh.get(key)
        if last is None:
            return True
        if self._should_force_7am_history_refresh(symbol, now, last):
            self._forced_premarket_history_refresh_date[key] = now.date()
            return True
        interval = float(self.config.runtime.history_poll_seconds)
        last_empty = self.last_empty_history_refresh.get(key)
        if last_empty is not None and last_empty == last and not self.is_regular_session(now):
            interval = max(interval, 900.0)
        return (now - last).total_seconds() >= interval

    def _quote_ttl(self) -> float:
        """The quote cache's TTL: ``runtime.quote_cache_seconds``, at least
        1 s. A cached quote this old is fetched again by the engine's refresh
        (``should_refresh_quote``)."""
        return max(1.0, float(self.config.runtime.quote_cache_seconds))

    def should_refresh_quote(self, symbol: str) -> bool:
        key = self._symbol_key(symbol)
        last = self.last_quote_refresh.get(key)
        if last is None:
            return True
        return (sessions.now_et() - last).total_seconds() >= self._quote_ttl()

    @staticmethod
    def _symbol_key(symbol: str) -> str:
        return str(symbol).upper().strip()

    @staticmethod
    def _htf_key(symbol: str, timeframe_minutes: int) -> tuple[str, int]:
        return str(symbol).upper().strip(), int(timeframe_minutes)

    def prune_inactive_symbols(self, active_symbols: set[str]) -> int:
        """Drop per-symbol state for symbols not in ``active_symbols``.

        Long-lived multi-day runs accumulate per-symbol entries (history
        frames, HTF/SR/quote caches) for every symbol the screener has
        ever returned. Without pruning, RSS grows unbounded as different
        symbols rotate through the watchlist day to day. The 1m frame
        alone is ~240KB per symbol at the default 1080-min lookback —
        500 symbols over a month would be ~120MB just for history.

        Called by the engine after each cycle's watchlist is determined.
        ``active_symbols`` is the union of streaming symbols + current
        watchlist + held positions — anything we still care about. A
        symbol that drops out of all three has no consumer; safe to evict.
        If it returns to the watchlist later the warmup tracker re-fetches
        from `price_history` (one round-trip per revival).

        Returns the number of distinct symbols evicted.
        """
        active = {self._symbol_key(s) for s in (active_symbols or set()) if s}
        # Capture victim symbol set from the primary `history` keyspace —
        # any symbol with state but not active. Then remove it from every
        # per-symbol dict in one pass so we never leave dangling entries.
        retain_derived_frames(active)
        # The stream quote books of inactive symbols go too, under the books'
        # own lock (never inside this store's).
        try:
            self.stream_quotes.prune(active)
        except StreamQuoteLockTimeout as exc:
            self._reset_stream_quotes("prune", exc)
        with self._lock:
            # get_merged's memo and provenance generations can hold a symbol
            # the store does not (a read of an unknown symbol, or one in
            # flight across a prune), so they keep only the active symbols.
            self._merged_memo = {k: v for k, v in self._merged_memo.items() if k[0] in active}
            self._source_generations = {k: v for k, v in self._source_generations.items() if k in active}
            self._level_memo.retain(lambda slot: slot[1] in active)
            stale = {sym for sym in self.history.keys() if sym not in active}
            stale |= {sym for sym in self.live.keys() if sym not in active}
            stale |= {sym for sym in self.quote_cache.keys() if sym not in active}
            if not stale:
                return 0
            # Single-key dicts
            for sym in stale:
                self.history.pop(sym, None)
                self.live.pop(sym, None)
                self.quote_cache.pop(sym, None)
                self.last_history_refresh.pop(sym, None)
                self.last_quote_refresh.pop(sym, None)
                self.last_stream_update.pop(sym, None)
                self.last_stream_bar_time.pop(sym, None)
                self.last_stream_health_log.pop(sym, None)
                self.last_empty_history_refresh.pop(sym, None)
                self._history_window_rows.pop(sym, None)
                self._consecutive_quote_failures.pop(sym, None)
                self._quote_blacklist.pop(sym, None)
                self._forced_premarket_history_refresh_date.pop(sym, None)
                self.merge_stats.pop(sym, None)
                self._stream_seen_symbols.discard(sym)
                self._stream_first_bar_time.pop(sym, None)
            # Tuple-keyed dicts: drop any (sym, *) entry where sym is stale.
            self.history_htf = {k: v for k, v in self.history_htf.items() if k[0] not in stale}
            self.htf_cache = {k: v for k, v in self.htf_cache.items() if k[0] not in stale}
            self._htf_derived_memo = {k: v for k, v in self._htf_derived_memo.items() if k[0][0] not in stale}
            self.last_htf_refresh = {k: v for k, v in self.last_htf_refresh.items() if k[0] not in stale}
        return len(stale)

    def begin_cycle(self) -> None:
        self._level_memo.next_generation()
        with self._lock:
            self._cycle_active = True
            self._cycle_merged_cache.clear()
            self._cycle_htf_context_cache.clear()
            self._cycle_fvg_cache.clear()
            self._cycle_ob_cache.clear()
            self._cycle_sr_cache.clear()

    def end_cycle(self) -> None:
        with self._lock:
            self._cycle_active = False
            self._cycle_merged_cache.clear()
            self._cycle_htf_context_cache.clear()
            self._cycle_fvg_cache.clear()
            self._cycle_ob_cache.clear()
            self._cycle_sr_cache.clear()

    def _invalidate_cycle_symbol(self, symbol: str) -> None:
        cache_key = self._symbol_key(symbol)
        with self._lock:
            # In-place delete avoids rebuilding the entire dict on every stream bar.
            merged_victims = [k for k in self._cycle_merged_cache if k[0] == cache_key]
            for k in merged_victims:
                del self._cycle_merged_cache[k]
            fvg_victims = [k for k in self._cycle_fvg_cache if k[0] == cache_key]
            for k in fvg_victims:
                del self._cycle_fvg_cache[k]
            ob_victims = [k for k in self._cycle_ob_cache if k[0] == cache_key]
            for k in ob_victims:
                del self._cycle_ob_cache[k]

    def _invalidate_cycle_htf(self, symbol: str, timeframe_minutes: int | None = None) -> None:
        cache_key = self._symbol_key(symbol)
        with self._lock:
            if timeframe_minutes is None:
                htf_victims = [k for k in self._cycle_htf_context_cache if k[0] == cache_key]
                for k in htf_victims:
                    del self._cycle_htf_context_cache[k]
                sr_victims = [k for k in self._cycle_sr_cache if k[0] == cache_key]
                for k in sr_victims:
                    del self._cycle_sr_cache[k]
                return
            tf = int(timeframe_minutes)
            htf_victims = [k for k in self._cycle_htf_context_cache if k[0] == cache_key and k[1] == tf]
            for k in htf_victims:
                del self._cycle_htf_context_cache[k]
            sr_victims = [k for k in self._cycle_sr_cache if k[0] == cache_key and k[1] == tf]
            for k in sr_victims:
                del self._cycle_sr_cache[k]

    @staticmethod
    def _htf_context_cache_key(symbol: str, timeframe_minutes: int, build_kwargs: Mapping[str, Any]) -> tuple:
        """``(symbol, tf, *every build_htf_context argument the caller sets)``.

        A cached context is a function of the stored (symbol, tf) frame and
        exactly these arguments, so two callers share an entry only when a
        fresh build would give them the same context. Until 2026-09-23 the
        key held only the prior-day/week and FVG flags, so a caller with
        different pivot/level/tolerance/EMA/flip settings was served a
        context built with another caller's.
        """
        return (
            str(symbol).upper().strip(),
            int(timeframe_minutes),
            *(
                (name, round(value, 6) if isinstance(value, float) else value)
                for name, value in sorted(build_kwargs.items())
            ),
        )

    @staticmethod
    def _direct_history_frequency(timeframe_minutes: int) -> int:
        tf = max(1, int(timeframe_minutes))
        direct = {1, 5, 10, 15, 30}
        if tf in direct:
            return tf
        for base in (30, 15, 10, 5, 1):
            if tf % base == 0:
                return base
        return 1

    def htf_refresh_due(self, symbols: Iterable[str], timeframe_minutes: int) -> list[str]:
        """The bar-aligned HTF refresh gate: the ``symbols`` (in their order)
        whose ``timeframe_minutes`` frame is due, the clock and its bucket
        read once. A symbol is due once an HTF bar has closed (+10 s) since
        its frame was fetched, or when none was; never a symbol with no S/R
        (``is_support_resistance_symbol``: a market internal such as $TICK,
        an option), which keeps no HTF frame. The engine's HTF refresh
        (``IntradayBot._refresh_htf_frames``) asks for every symbol a read
        can ask for at each refresh point, and acts on it; no read does
        (``refresh_htf_frame``). A bucket floor costs about 0.3 ms.

        New HTF data only arrives at HTF bar boundaries — within a single bar
        window the broker has nothing new to give us. This replaces the prior
        time-elapsed throttle (every N seconds regardless of bar timing) with
        a true "1 fetch per HTF bar" cadence.

        For HTF=60m, the underlying ``price_history`` call is at base
        frequency 30m and resampled to 60m. At the 60m boundary, both 30m
        constituents of the just-closed 60m bar are already complete on the
        broker side, so a single fetch + resample produces the closed bar.
        Boundaries are ``resample_bars``' own (``session_bucket_floor``): in
        the regular session 60m bars close at XX:30, and a clock floor
        refetched at XX:00, half a bar before each one closed, leaving the
        newest 60m bar out of every context for 30 minutes.

        A 10-second settle buffer is applied so we don't fetch at exactly
        ``:30:00`` — gives the broker time to aggregate the just-closed bar.
        A frame fetched before the current bucket started is fetched in an
        earlier bucket: the buckets tile the day, so that is the comparison
        of the two buckets' starts, with one floor fewer. (Until 2026-09-29
        ``should_refresh_htf_context`` asked it for one symbol; after the
        refresh points no production code did.)"""
        tf_min = max(1, int(timeframe_minutes))
        now = sessions.now_et()
        now_bucket = session_bucket_floor(now, tf_min)
        settled = (now - now_bucket) >= timedelta(seconds=10)
        due: list[str] = []
        for symbol in symbols:
            if not is_support_resistance_symbol(symbol):
                continue
            with self._lock:
                last = self.last_htf_refresh.get(self._htf_key(symbol, tf_min))
            if last is None or (settled and last < now_bucket):
                due.append(symbol)
        return due

    @staticmethod
    def _ohlcv_columns(frame: pd.DataFrame | None) -> pd.DataFrame:
        if frame is None or getattr(frame, "empty", True):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        cols = [col for col in ("open", "high", "low", "close", "volume") if col in frame.columns]
        if len(cols) < 5:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return frame.loc[:, ["open", "high", "low", "close", "volume"]].copy()

    @staticmethod
    def _merge_htf_frames(existing: pd.DataFrame | None, incoming: pd.DataFrame | None) -> pd.DataFrame:
        base = MarketDataStore._ohlcv_columns(existing)
        update = MarketDataStore._ohlcv_columns(incoming)
        if base.empty:
            return update
        if update.empty:
            return base
        combined = pd.concat([base, update]).sort_index()
        return combined[~combined.index.duplicated(keep="last")]

    @staticmethod
    def _htf_window_start(end: datetime, lookback_days: int) -> pd.Timestamp:
        """Oldest bar an HTF frame keeps: ``lookback_days`` (at least 5)
        before ``end``, or the start of the prior W-FRI week when that is
        earlier.

        ``prior_week_levels`` takes PWH/PWL from the last complete W-FRI week
        in the frame. A plain 10-day window from ``end`` starts inside that
        week from Thursday on and loses its whole Monday by Friday, so
        PWH/PWL silently described Tue-Fri (fixed 2026-09-23). The current
        week is the one holding ``latest_session_date(end)``, the date the
        builders pass ``prior_week_levels`` as ``as_of``: a weekend or holiday
        ``end`` counts from the trading day before it. Rolling back weekends
        only put a holiday Monday in the new week while the builders still
        read Friday's, and the trim cut their prior week down to its last
        afternoon.
        """
        end_ts = pd.Timestamp(end).tz_convert(EXCHANGE_TZ)
        session_day = pd.Timestamp(latest_session_date(end_ts))
        prior_week_start = (session_day.to_period("W-FRI") - 1).start_time.tz_localize(EXCHANGE_TZ)
        return min(end_ts - pd.Timedelta(days=max(5, int(lookback_days))), prior_week_start)

    @staticmethod
    def _trim_frame_to_days(frame: pd.DataFrame, end: datetime, lookback_days: int) -> pd.DataFrame:
        if frame.empty:
            return frame
        return frame.loc[frame.index >= MarketDataStore._htf_window_start(end, lookback_days)].copy()

    @staticmethod
    def _completed_bars(frame: pd.DataFrame, bar_minutes: int, requested_at: datetime) -> pd.DataFrame:
        """``frame`` without the bar still forming at ``requested_at``.

        Bars are labelled at their start, so bar T is complete once its
        bucket has ended (``session_bucket_ends``: ``T + bar_minutes``, or the
        session boundary that cuts a 60m bar short). price_history requested up to "now"
        returns the bar that opened seconds earlier (the HTF refresh runs
        about 10 s into each bucket, the 09:15 prewarm 3 s into the minute),
        and it used to be stored as if complete: a near-zero-range 15m bar
        confirming pivots, cutting ATR by ~7% and setting breakout flags and
        HTF structure for a whole bucket, and a 3-second 09:15 1m bar that
        nothing ever replaced (2026-09-23).
        """
        if frame.empty:
            return frame
        return frame[completed_bucket_mask(frame.index, int(bar_minutes), requested_at)]

    @staticmethod
    def _htf_incremental_start(
            cached_frame: pd.DataFrame | None,
        *,
        end: datetime,
        lookback_days: int,
        base_frequency_minutes: int,
    ) -> datetime | None:
        if cached_frame is None or cached_frame.empty:
            return None
        # The stored frame's index is ET-aware: _history_candles_to_frame
        # converts every stamp.
        last_idx = pd.Timestamp(cached_frame.index.max())
        if pd.isna(last_idx):
            return None
        overlap_minutes = max(base_frequency_minutes * 4, 240)
        overlap_days = max(2, int(math.ceil(overlap_minutes / 1440.0)))
        recent_window_days = min(max(lookback_days, 5), max(7, overlap_days))
        min_start = end - timedelta(days=recent_window_days)
        last_dt = last_idx.to_pydatetime()
        if last_dt < min_start:
            return None
        return max(last_dt - timedelta(minutes=overlap_minutes), min_start)

    def refresh_htf_frame(self, symbol: str, *, timeframe_minutes: int, lookback_days: int) -> bool:
        """Fetch ``symbol``'s ``timeframe_minutes`` HTF bars now and store
        them (``_refresh_htf_frame``): True when stored, False when the fetch
        failed.

        The only HTF fetch. The engine runs it on its fetch pool over every
        symbol a read path reads, at each refresh point of its cycle, once
        the symbol's HTF bar has closed (``htf_refresh_due``;
        ``IntradayBot._refresh_htf_frames``). Every read (``get_htf_frame``,
        ``get_htf_context``, ``get_support_resistance``) returns what is
        stored and never fetches. Until 2026-09-28 a read could fetch: the
        first read of a symbol after the boundary fetched it, one symbol at a
        time, and the dashboard publish did most of a boundary's fetches,
        which held the next management pass 15-30 s.

        ``lookback_days`` sizes the window a full fetch requests and the
        frame keeps (the strategy's ``htf_lookback_days()``). A failure is
        logged with its type and absorbed, so one symbol's failed fetch fails
        neither the cycle nor another symbol's fetch: the frame keeps its
        bars, its refresh stays due, and the next cycle retries it. Broad
        because the fetch goes through the Schwab client, which raises any
        type (``_fetch_price_history_payload_with_aliases`` re-raises the
        last one).
        """
        try:
            self._refresh_htf_frame(symbol, timeframe_minutes, lookback_days)
        except Exception as exc:
            LOG.warning("HTF frame refresh failed for %s (%sm): %s: %s", symbol, timeframe_minutes, type(exc).__name__, exc)
            return False
        return True

    def _refresh_htf_frame(self, symbol: str, timeframe_minutes: int, lookback_days: int) -> None:
        """Fetch the (symbol, tf) HTF frame from Schwab and store it.

        The stored frame holds completed bars only (``_completed_bars``) and
        only bars inside the 07:00-20:00 equity stream window
        (``equity_stream_window_bars``, on the base bars before any resample),
        both applied before the merge so the
        next incremental fetch's 4 h overlap replaces the dropped forming bar
        with its completed version. Storing a new frame object is what makes
        every HTF context rebuild (see ``get_htf_context``); this method
        builds none itself.
        """
        tf = max(1, int(timeframe_minutes))
        key = self._htf_key(symbol, tf)
        with self._lock:
            cached_frame = self.history_htf.get(key)
        base_freq = self._direct_history_frequency(tf)
        end = sessions.now_et()
        start = self._htf_incremental_start(
            cached_frame,
            end=end,
            lookback_days=int(lookback_days),
            base_frequency_minutes=base_freq,
        )
        incremental_refresh = start is not None
        if start is None:
            start = self._htf_window_start(end, int(lookback_days)).to_pydatetime()
        # Fetch from a bucket start. A 60m bucket starts on the hour outside
        # the regular session and on the half hour inside it, so the 4 h
        # overlap before the last stored label could land mid-bucket: the
        # refetch then built that bucket from its second 30m bar alone, and
        # the merge (newest copy wins) replaced the complete bar with the half
        # for good -- later refreshes never reach back that far.
        start = session_bucket_floor(start, tf).to_pydatetime()
        mode = "incremental" if incremental_refresh else "full"
        LOG.info("Fetching %sm HTF price_history for %s from %s to %s (base=%sm mode=%s)", tf, symbol, start, end, base_freq, mode)
        payload, source_symbol = self._fetch_price_history_payload_with_aliases(
            symbol,
            frequencyType="minute",
            frequency=base_freq,
            startDate=start,
            endDate=end,
            needExtendedHoursData=bool(self.config.runtime.use_extended_hours_history),
            needPreviousClose=True,
        )
        if str(source_symbol).upper().strip() != str(symbol).upper().strip():
            LOG.debug("Resolved HTF price_history alias for %s via %s", symbol, source_symbol)
        df = equity_stream_window_bars(self._history_candles_to_frame(payload.get("candles", [])))
        if base_freq != tf and not df.empty:
            df = resample_bars(df, f"{tf}min")
        df = self._completed_bars(df, tf, end)
        if incremental_refresh and cached_frame is not None and not cached_frame.empty:
            df = self._merge_htf_frames(cached_frame, df)
        else:
            df = self._ohlcv_columns(df)
        df = self._trim_frame_to_days(df, end, int(lookback_days))
        df = ensure_standard_indicator_frame(df)
        with self._lock:
            self.history_htf[key] = df
            # Stamped with ``end``, the time the bars were cut at, not the
            # clock after the response: a refresh requested at 09:59:59 and
            # answered after 10:00 holds nothing past 09:30, and stamped in
            # the 10:00 bucket it told htf_refresh_due the 09:45 bar was
            # in, keeping it out of every context until 10:15.
            self.last_htf_refresh[key] = end
        self._invalidate_cycle_htf(symbol, tf)

    def _htf_context_from_stored_frame(
        self,
        symbol: str,
        timeframe_minutes: int,
        cache_key: tuple,
        build_kwargs: Mapping[str, Any],
    ) -> HTFContext | None:
        """The context for ``cache_key``, rebuilt from the stored frame (no
        API call) when the cached one was built from an older frame, for an
        earlier session date or on the other side of the session open/close;
        None while no frame has been stored for (symbol, tf)."""
        key = self._htf_key(symbol, timeframe_minutes)
        with self._lock:
            frame = self.history_htf.get(key)
            entry = self.htf_cache.get(cache_key)
        if frame is None:
            return None
        as_of = latest_session_date(sessions.now_et())
        session_open = indicator_session_open()
        if entry is not None and entry.frame is frame and entry.as_of == as_of and entry.session_open == session_open:
            return entry.context
        current = None
        merged = self.get_merged(symbol, with_indicators=False)
        if merged is not None and not merged.empty:
            current = float(merged.iloc[-1].close)
        sr_cfg = self.config.support_resistance
        ctx = build_htf_context(
            frame,
            current_price=current,
            timeframe_minutes=int(timeframe_minutes),
            # Checked at load (above 0); a 0 read as 0.10 / 0.0015 until
            # 2026-09-26.
            same_side_min_gap_atr_mult=float(sr_cfg.same_side_min_gap_atr_mult),
            same_side_min_gap_pct=float(sr_cfg.same_side_min_gap_pct),
            fallback_reference_max_drift_atr_mult=float(getattr(sr_cfg, "fallback_reference_max_drift_atr_mult", 1.0) or 1.0),
            fallback_reference_max_drift_pct=float(getattr(sr_cfg, "fallback_reference_max_drift_pct", 0.01) or 0.01),
            as_of=as_of,
            **build_kwargs,
        )
        with self._lock:
            self.htf_cache[cache_key] = _HTFCacheEntry(frame, as_of, session_open, ctx)
        return ctx

    def get_htf_context(
        self,
        symbol: str,
        *,
        timeframe_minutes: int,
        pivot_span: int = 2,
        max_levels_per_side: int = 6,
        atr_tolerance_mult: float = 0.35,
        pct_tolerance: float = 0.0030,
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
    ) -> HTFContext | None:
        """HTF context for ``symbol`` built from its stored (symbol, tf) frame.

        A read: it never fetches. The engine refreshes the frame once per HTF
        bar (``refresh_htf_frame``); every read gets a context built from the
        frame currently stored, rebuilt without an API call the first time
        its cache key is read after the frame changed, and None while no
        frame is stored. Until 2026-09-28 a read passing ``allow_refresh``
        (every caller but the score context and the archive) fetched the
        frame itself when its HTF bar had closed, one symbol at a time.

        Until 2026-09-23 a fetch rebuilt only the fetching caller's cache key
        but stamped the refresh clock every key shares, so every other key
        kept its first build: top_tier's configured-FVG context (strategy FVG
        scoring, runner eligibility, the dashboard's HTF FVG/divergence
        overlays and trend label) was the ~09:15 premarket build all session,
        because the engine's default-FVG S/R refresh won every boundary.

        The HTF RSI divergence knobs are global: they are read here, from
        ``technical_levels``, not passed by the caller, so every strategy
        and the dashboard build the same divergence (2026-09-24). Until then
        no caller passed them, and every build ran with divergence on and a
        6-bar age whatever the config said.

        The HTF pairs its pivots with the LTF's thresholds
        (``divergence_pivot_lookback``, ``divergence_min_price_move_pct``,
        ``divergence_rsi_min_delta``) and its own age limit
        (``htf_divergence_max_age_bars``). Until 2026-09-25 the thresholds
        were the builder's defaults, so retuning them moved only the LTF
        divergence; every preset ships those defaults (4 / 0.0015 / 2.5), so
        no build changes. ``divergence_rsi_length`` stays LTF-only: the HTF
        reads its frame's ``rsi14``.
        """
        tf = max(1, int(timeframe_minutes))
        tl_cfg = self.config.technical_levels
        build_kwargs: dict[str, Any] = {
            "pivot_span": int(pivot_span),
            "max_levels_per_side": int(max_levels_per_side),
            "atr_tolerance_mult": float(atr_tolerance_mult),
            "pct_tolerance": float(pct_tolerance),
            "stop_buffer_atr_mult": float(stop_buffer_atr_mult),
            "ema_fast_span": int(ema_fast_span),
            "ema_slow_span": int(ema_slow_span),
            "flip_confirmation_bars": int(flip_confirmation_bars),
            "use_prior_day_high_low": bool(use_prior_day_high_low),
            "use_prior_week_high_low": bool(use_prior_week_high_low),
            "include_fair_value_gaps": bool(include_fair_value_gaps),
            "fair_value_gap_max_per_side": int(fair_value_gap_max_per_side),
            "fair_value_gap_min_atr_mult": float(fair_value_gap_min_atr_mult),
            "fair_value_gap_min_pct": float(fair_value_gap_min_pct),
            # In build_kwargs, so they are part of the cache key. On with the
            # LTF divergence's switches (technical_levels.enabled and
            # divergence_enabled); a configured age of 0 is honoured, and so
            # is a configured 0 threshold (the builder clamps each the way
            # the LTF builder does).
            "divergence_enabled": bool(tl_cfg.enabled and tl_cfg.divergence_enabled),
            "divergence_max_age_bars": int(tl_cfg.htf_divergence_max_age_bars),
            "divergence_pivot_lookback": int(tl_cfg.divergence_pivot_lookback),
            "divergence_min_price_move_pct": float(tl_cfg.divergence_min_price_move_pct),
            "divergence_rsi_min_delta": float(tl_cfg.divergence_rsi_min_delta),
        }
        cache_key = self._htf_context_cache_key(symbol, tf, build_kwargs)
        with self._lock:
            if self._cycle_active and cache_key in self._cycle_htf_context_cache:
                return self._cycle_htf_context_cache[cache_key]
        ctx = self._htf_context_from_stored_frame(symbol, tf, cache_key, build_kwargs)
        with self._lock:
            if self._cycle_active:
                self._cycle_htf_context_cache[cache_key] = ctx
        return ctx


    def get_fair_value_gap_context(
        self,
        symbol: str,
        *,
        timeframe_minutes: int = 1,
        current_price: float | None = None,
        max_per_side: int = 4,
        min_gap_atr_mult: float = 0.05,
        min_gap_pct: float = 0.0005,
    ) -> FairValueGapContext:
        cache_key = (
            self._symbol_key(symbol),
            int(timeframe_minutes),
            None if current_price is None else round(float(current_price), 8),
            int(max_per_side),
            round(float(min_gap_atr_mult), 6),
            round(float(min_gap_pct), 6),
        )
        with self._lock:
            if self._cycle_active and cache_key in self._cycle_fvg_cache:
                return self._cycle_fvg_cache[cache_key]
        # Fetch the right frame: 1m via get_merged(no timeframe), LTF/HTF via the
        # explicit timeframe lookup that triggers internal resampling. Mirrors
        # `get_order_block_context` so a 5m FVG context computes on 5m bars,
        # not 1m bars labeled "5m".
        tf = max(1, int(timeframe_minutes))
        if tf == 1:
            merged = self.get_merged(symbol, with_indicators=True)
        else:
            merged = self.get_merged(symbol, timeframe=f"{tf}min", with_indicators=True)
        if merged is None or merged.empty:
            ctx = empty_fvg_context(float(current_price or 0.0), timeframe_minutes=tf)
        else:
            close = float(current_price if current_price is not None else merged.iloc[-1].get('close', 0.0) or 0.0)

            def build() -> FairValueGapContext:
                return build_fair_value_gap_context(
                    merged,
                    timeframe_minutes=tf,
                    current_price=close,
                    max_per_side=max(0, int(max_per_side or 0)),
                    min_gap_atr_mult=float(min_gap_atr_mult),
                    min_gap_pct=float(min_gap_pct),
                )

            # Keyed on the frame read (its version), never on the store's
            # current objects: a bar landing while get_merged built it leaves
            # the cycle holding the pre-bar frame, and its context must not be
            # filed under the post-bar store. The build reads the clock only
            # through completed_bars and the ATR's session switch.
            version = frame_version(merged)
            if version is None:
                ctx = build()
            else:
                ctx = self._level_memo.serve(
                    ("fvg", *cache_key),
                    (version,),
                    lambda: (forming_positions(merged.index, tf, sessions.now_et()), indicator_clock_key()),
                    lambda _clock: build(),
                )
        # Shared, not copied: no reader writes into a context (the HTF
        # contexts have been shared across cycles all along).
        with self._lock:
            if self._cycle_active:
                self._cycle_fvg_cache[cache_key] = ctx
        return ctx

    def get_order_block_context(
        self,
        symbol: str,
        *,
        timeframe_minutes: int = 1,
        current_price: float | None = None,
        mode: str = "loose",
        max_per_side: int = 4,
        min_block_atr_mult: float = 0.05,
        min_block_pct: float = 0.0005,
        min_thrust_atr_mult: float = 0.75,
        pivot_span: int = 2,
        new_high_lookback: int = 8,
    ) -> OrderBlockContext:
        """Cycle-cached order block context — mirror of `get_fair_value_gap_context`.

        For LTF OBs (``timeframe_minutes`` matches the strategy's
        ``params.ltf_minutes``, default 1m), uses the merged frame at that
        timeframe. For HTF OBs (e.g. 15m), the merged frame is fetched at
        the HTF timeframe via ``get_merged(symbol, timeframe=f'{tf}min')``.

        Cache invalidates per-symbol on every new stream bar via
        ``_invalidate_cycle_symbol`` and on cycle boundaries via
        ``begin_cycle`` / ``end_cycle``. Identical lifetime to the FVG cache.

        ``min_thrust_atr_mult`` is part of the cache key — different
        thrust thresholds produce different OB sets, so callers passing
        different values get separate cache entries.
        """
        cache_key = (
            self._symbol_key(symbol),
            int(timeframe_minutes),
            None if current_price is None else round(float(current_price), 8),
            mode,
            int(max_per_side),
            round(float(min_block_atr_mult), 6),
            round(float(min_block_pct), 6),
            round(float(min_thrust_atr_mult), 6),
            int(pivot_span),
            int(new_high_lookback),
        )
        with self._lock:
            if self._cycle_active and cache_key in self._cycle_ob_cache:
                return self._cycle_ob_cache[cache_key]
        # Fetch the right frame: 1m via get_merged(no timeframe), HTF via the
        # explicit timeframe lookup that triggers internal resampling.
        tf = max(1, int(timeframe_minutes))
        if tf == 1:
            merged = self.get_merged(symbol, with_indicators=True)
        else:
            merged = self.get_merged(symbol, timeframe=f"{tf}min", with_indicators=True)
        if merged is None or merged.empty:
            ctx = empty_order_block_context(
                float(current_price or 0.0),
                timeframe_minutes=tf,
                mode=mode,
            )
        else:
            close = float(current_price if current_price is not None else merged.iloc[-1].get("close", 0.0) or 0.0)

            def build() -> OrderBlockContext:
                return build_order_block_context(
                    merged,
                    timeframe_minutes=tf,
                    current_price=close,
                    mode=mode,
                    max_per_side=max(0, int(max_per_side or 0)),
                    min_block_atr_mult=float(min_block_atr_mult),
                    min_block_pct=float(min_block_pct),
                    min_thrust_atr_mult=float(min_thrust_atr_mult),
                    pivot_span=int(pivot_span),
                    new_high_lookback=int(new_high_lookback),
                )

            # Keyed on the frame read, as get_fair_value_gap_context; the
            # build reads the clock only through the ATR's session switch.
            version = frame_version(merged)
            if version is None:
                ctx = build()
            else:
                ctx = self._level_memo.serve(("ob", *cache_key), (version,), indicator_clock_key, lambda _clock: build())
        with self._lock:
            if self._cycle_active:
                self._cycle_ob_cache[cache_key] = ctx
        return ctx

    def get_htf_frame(self, symbol: str, *, timeframe_minutes: int) -> pd.DataFrame | None:
        """Copy of the stored (symbol, tf) HTF frame: completed bars built
        from 07:00-20:00 bars only (see ``_refresh_htf_frame``), or None
        while none is stored. A read: it never fetches (``refresh_htf_frame``).
        Until 2026-09-28 a read passing ``allow_refresh`` fetched the frame
        when its HTF bar had closed, with level parameters and a lookback
        that only that fetch used."""
        with self._lock:
            frame = self.history_htf.get(self._htf_key(symbol, timeframe_minutes))
        return frame.copy(deep=False) if frame is not None else None

    def derive_from_htf_frame(
        self,
        symbol: str,
        *,
        timeframe_minutes: int,
        slot: str,
        build: Callable[[pd.DataFrame | None], Any],
    ) -> Any:
        """``build`` of the stored (symbol, tf) HTF frame (a shallow copy of
        it, or None while none is stored), kept per ``slot`` until the stored
        frame object or the indicator settings change. Every refresh stores a
        new object and nothing writes into a stored one
        (``_refresh_htf_frame``), so ``build`` must read nothing but the
        frame's bars and those settings: no clock, no other feed state. The
        entry holds the object it was built from (compared by identity, so a
        freed object's id never matches) until the next read of the slot or
        the prune."""
        key = self._htf_key(symbol, timeframe_minutes)
        settings = (get_runtime_indicator_mode(), get_session_indicator_window())
        with self._lock:
            stored = self.history_htf.get(key)
            entry = self._htf_derived_memo.get((key, slot))
        if entry is not None and entry[0] is stored and entry[1] == settings:
            return entry[2]
        result = build(None if stored is None else stored.copy(deep=False))
        with self._lock:
            self._htf_derived_memo[(key, slot)] = (stored, settings, result)
        return result

    def _stream_history_due(self, symbol: str) -> bool:
        now = sessions.now_et()
        key = self._symbol_key(symbol)
        last = self.last_history_refresh.get(key)
        if last is None:
            return True
        interval = max(10, int(self.config.runtime.stream_fallback_poll_seconds))
        return (now - last).total_seconds() >= interval

    def _stream_log_due(self, symbol: str) -> bool:
        now = sessions.now_et()
        key = self._symbol_key(symbol)
        last = self.last_stream_health_log.get(key)
        if last is None:
            return True
        interval = max(15, int(self.config.runtime.stream_health_log_seconds))
        return (now - last).total_seconds() >= interval

    def _log_stream_health(self, symbol: str, message: str, level: int = logging.WARNING) -> None:
        if not self._stream_log_due(symbol):
            return
        key = self._symbol_key(symbol)
        self.last_stream_health_log[key] = sessions.now_et()
        LOG.log(level, "%s [%s]", message, key)

    def _stream_stale_after_seconds(self) -> int:
        connect_timeout = max(5, int(self.config.runtime.stream_connect_timeout_seconds))
        configured = max(connect_timeout, int(self.config.runtime.stream_stale_fallback_seconds))
        # Schwab CHART_EQUITY commonly behaves like minute-close bar delivery rather than
        # continuous intrabar updates. Requiring multiple missed bar intervals avoids false
        # stale detections for healthy 1-minute streams that only emit once per bar.
        expected_bar_interval_seconds = 60
        missed_bar_grace_seconds = 10
        minimum_stream_window = (expected_bar_interval_seconds * 2) + missed_bar_grace_seconds
        return max(configured, minimum_stream_window)

    def _latest_cached_bar_timestamp(self, symbol: str) -> pd.Timestamp | None:
        key = self._symbol_key(symbol)
        with self._lock:
            live_frame = self.live.get(key)
            history_frame = self.history.get(key)
            stream_bar = self.last_stream_bar_time.get(key)

        candidates: list[pd.Timestamp] = []
        if stream_bar is not None:
            candidates.append(pd.Timestamp(stream_bar))
        for frame in (history_frame, live_frame):
            if frame is None or getattr(frame, "empty", True):
                continue
            candidates.append(pd.Timestamp(frame.index[-1]))
        if not candidates:
            return None
        latest = max(candidates)
        if latest.tzinfo is None:
            latest = latest.tz_localize(EXCHANGE_TZ)
        return latest

    def _latest_cached_bar_age_seconds(self, symbol: str, now: datetime | None = None) -> float | None:
        latest = self._latest_cached_bar_timestamp(symbol)
        if latest is None:
            return None
        reference = now if now is not None else sessions.now_et()
        latest_dt = latest.to_pydatetime() if hasattr(latest, "to_pydatetime") else latest
        return max(0.0, (reference - latest_dt).total_seconds())

    def _is_fresh_stream_bar_timestamp(self, ts: pd.Timestamp, *, now: datetime | None = None) -> bool:
        reference = now if now is not None else sessions.now_et()
        ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        age_seconds = max(0.0, (reference - ts_dt).total_seconds())
        return age_seconds <= float(self._stream_stale_after_seconds())

    def live_entry_bar_status(self, symbol: str, *, now: datetime | None = None) -> dict[str, object]:
        """Return whether a symbol has a fresh live 1m stream bar suitable for entries."""
        key = self._symbol_key(symbol)
        reference = now if now is not None else sessions.now_et()
        requires_live_entry_bar = is_streamable_equity(key) and self.is_equity_stream_session(reference)
        stale_after = float(self._stream_stale_after_seconds())
        with self._lock:
            stream_subscribed = key in self.stream_symbols
            stream_active = bool(getattr(self.stream, "active", False))
            stream_seen = key in self._stream_seen_symbols
            last_stream_update = self.last_stream_update.get(key)
            last_stream_bar_time = self.last_stream_bar_time.get(key)
        age_seconds: float | None = None
        if last_stream_bar_time is not None:
            bar_ts = pd.Timestamp(last_stream_bar_time)
            if bar_ts.tzinfo is None:
                bar_ts = bar_ts.tz_localize(EXCHANGE_TZ)
            bar_dt = bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts
            age_seconds = max(0.0, (reference - bar_dt).total_seconds())
        ready = True
        reason: str | None = None
        if requires_live_entry_bar:
            if not stream_subscribed:
                ready = False
                reason = "live_1m_not_subscribed"
            elif not stream_active:
                ready = False
                reason = "live_1m_stream_inactive"
            elif not stream_seen or last_stream_bar_time is None:
                ready = False
                reason = "awaiting_first_live_1m_bar"
            elif age_seconds is None or age_seconds > stale_after:
                ready = False
                reason = "live_1m_bar_stale"
        return {
            "symbol": key,
            "requires_live_entry_bar": bool(requires_live_entry_bar),
            "ready": bool(ready),
            "reason": reason,
            "stream_subscribed": bool(stream_subscribed),
            "stream_active": bool(stream_active),
            "stream_seen": bool(stream_seen),
            "stale_after_seconds": stale_after,
            "last_stream_update": last_stream_update.isoformat() if hasattr(last_stream_update, "isoformat") else last_stream_update,
            "last_stream_bar_time": last_stream_bar_time.isoformat() if hasattr(last_stream_bar_time, "isoformat") else last_stream_bar_time,
            "last_stream_bar_age_seconds": age_seconds,
        }

    def should_backfill_stream_symbol(self, symbol: str) -> bool:
        """Return True when a streamable symbol needs a history repair/backfill."""
        if not is_streamable_equity(symbol):
            return self.should_refresh_history(symbol)
        cache_key = self._symbol_key(symbol)
        if cache_key not in self.stream_symbols:
            return False

        now = sessions.now_et()
        if not self.is_equity_stream_session(now):
            return False
        connect_timeout = max(5, int(self.config.runtime.stream_connect_timeout_seconds))
        stale_after = self._stream_stale_after_seconds()
        since_start = None
        if self.stream_start_requested_at is not None:
            since_start = (now - self.stream_start_requested_at).total_seconds()
        history_due = self._stream_history_due(symbol)

        if not self.stream.active:
            if since_start is not None and since_start >= connect_timeout and history_due:
                self._log_stream_health(symbol, f"Schwab stream still inactive after {connect_timeout}s; falling back to price_history")
                return True
            return False

        if cache_key not in self._stream_seen_symbols:
            if since_start is not None and since_start >= connect_timeout and history_due:
                self._log_stream_health(symbol, f"No fresh CHART_EQUITY bars received after {connect_timeout}s; falling back to price_history")
                return True
            return False

        # History fetched before the stream's first bar cannot reach it: a
        # bar is complete only after it closes. The stream opens with the
        # entry/management window (09:30) while the prewarm fetched at 09:15
        # and nothing refetched a warm frame, so 09:15-09:28 was missing
        # every day (and on a multi-day run the whole premarket). One fetch
        # cut after the first bar started closes the hole; its cut time
        # (fetch_history stamps ``end``) then passes this check for good
        # (2026-09-23).
        first_bar = self._stream_first_bar_time.get(cache_key)
        last_history = self.last_history_refresh.get(cache_key)
        if first_bar is not None and history_due and (last_history is None or pd.Timestamp(last_history) < first_bar):
            self._log_stream_health(symbol, f"price_history predates the first CHART_EQUITY bar {first_bar:%H:%M}; backfilling the gap", level=logging.INFO)
            return True

        last_stream = self.last_stream_update.get(cache_key)
        if last_stream is not None:
            stream_age_seconds = (now - last_stream).total_seconds()
            if stream_age_seconds >= stale_after and history_due:
                self._log_stream_health(symbol, f"CHART_EQUITY bars stale for {stream_age_seconds:.0f}s; falling back to price_history")
                return True

        latest_bar_age_seconds = self._latest_cached_bar_age_seconds(symbol, now=now)
        if latest_bar_age_seconds is not None and latest_bar_age_seconds >= stale_after and history_due:
            self._log_stream_health(symbol, f"Latest cached 1m bar stale for {latest_bar_age_seconds:.0f}s; falling back to price_history")
            return True
        return False

    def fetch_history(self, symbol: str, lookback_minutes: int | None = None) -> pd.DataFrame:
        cache_key = self._symbol_key(symbol)
        lookback = lookback_minutes or self.config.runtime.history_lookback_minutes
        end = sessions.now_et()
        start = end - timedelta(minutes=lookback)
        LOG.info("Fetching price_history for %s from %s to %s", symbol, start, end)
        payload, source_symbol = self._fetch_price_history_payload_with_aliases(
            symbol,
            frequencyType="minute",
            frequency=1,
            startDate=start,
            endDate=end,
            needExtendedHoursData=bool(self.config.runtime.use_extended_hours_history),
            needPreviousClose=True,
        )
        if str(source_symbol).upper().strip() != str(symbol).upper().strip():
            LOG.debug("Resolved price_history alias for %s via %s", symbol, source_symbol)
        df = self._completed_bars(self._history_candles_to_frame(payload.get("candles", [])), 1, end)
        fetched_at = sessions.now_et()
        if not df.empty:
            latest_bar = pd.Timestamp(df.index[-1])
            if latest_bar.tzinfo is None:
                latest_bar = latest_bar.tz_localize(EXCHANGE_TZ)
            latest_bar_age_seconds = max(0.0, (fetched_at - latest_bar.to_pydatetime()).total_seconds())
            if self.is_regular_session(fetched_at) and latest_bar_age_seconds >= float(self._stream_stale_after_seconds()):
                self._log_stream_health(symbol, f"price_history latest 1m bar stale for {latest_bar_age_seconds:.0f}s after repair fetch", level=logging.INFO)
        with self._lock:
            if not df.empty:
                self._history_window_rows[cache_key] = max(len(df), self._history_window_rows.get(cache_key, 0))
            keep_rows = self._history_window_rows.get(cache_key)
            self.history[cache_key] = self._retain_window(self._merge_frames(self.history.get(cache_key), df), keep_rows)
            if cache_key in self.live:
                self.live[cache_key] = self._retain_window(self.live[cache_key], keep_rows)
            # ``end``, the time the bars were cut at, is what the frame
            # covers. The clock after the response can have passed a minute
            # boundary the cut did not: a poll requested at 11:04:59.5 and
            # answered at 11:05:00.7 holds bars through 11:03, and stamped
            # 11:05:00.7 it satisfied the first-bar backfill check for an
            # 11:05 first stream bar, so 11:04 was never fetched.
            self.last_history_refresh[cache_key] = end
            if df.empty:
                self.last_empty_history_refresh[cache_key] = end
            else:
                self.last_empty_history_refresh.pop(cache_key, None)
            self.merge_stats[cache_key].history_rows = len(self.history[cache_key])
        self._invalidate_cycle_symbol(symbol)
        # NOTE: a previous version of this method also popped last_htf_refresh
        # entries and called _invalidate_cycle_htf when the 1m frame healed,
        # under the (incorrect) premise that "1m heal -> HTF must rebuild
        # against the healed 1m frame". HTF data is *not* derived from the
        # 1m stream — _refresh_htf_frame calls Schwab REST price_history at
        # base_freq (5/15/30m for the supported HTFs) and resamples to the
        # target tf. A 1m stream stale has no bearing on HTF freshness.
        # Popping last_htf_refresh defeated the bar-aligned gate every time
        # CHART_EQUITY went stale, which is frequent in low-volume after-
        # hours windows: production logs showed 135 stream stales -> 575
        # HTF fetches in a single 18:00 hour even though all of those calls
        # were inside one 60m HTF bucket. The bar-aligned gate already
        # handles cross-bucket recovery on its own (now_bucket > last_bucket
        # triggers a refresh); within the same bucket no new HTF bar exists
        # to fetch, so the pop only added wasted Schwab API calls without
        # improving data integrity.
        if df.empty and not self.is_regular_session(fetched_at):
            LOG.info("price_history returned no candles for %s outside regular session; using slower retry cadence", symbol)
        return self.get_merged(symbol)

    def daily_history_due(self, symbol: str, *, retry_failed: bool) -> bool:
        """True while ``symbol``'s daily history is not fetched for today's
        ET date, and, with ``retry_failed``, while today's fetch raised
        ``DAILY_HISTORY_RETRY_SECONDS`` or more ago and nothing has answered
        since: the symbols the engine's daily prefetch fetches
        (``IntradayBot._prefetch_daily_history``, which retries a failure
        until the day's first entry window opens). A response with no
        completed session is an answer, cached for the day."""
        key = self._symbol_key(symbol)
        now = sessions.now_et()
        with self._lock:
            if self.last_daily_refresh.get(key) != now.date():
                return True
            failed_at = self.daily_history_failed_at.get(key)
        return retry_failed and failed_at is not None and (now - failed_at).total_seconds() >= DAILY_HISTORY_RETRY_SECONDS

    def get_daily_history(self, symbol: str, calendar_days: int = 180) -> pd.DataFrame | None:
        """Daily OHLC bars for *symbol*: what ``fetch_daily_history`` stored
        for today's ET date (in memory; a new date fetches again), fetched
        now when it has not run today.

        Feeds ``daily_stats`` (per-symbol ADR scale + benchmark beta), which
        needs a horizon the intraday store cannot provide — ``history`` spans
        ``runtime.history_lookback_minutes`` (hours, not months).

        Returns ``None`` on a failed or empty fetch; the caller must treat
        that as "no stats available" rather than substituting a default. A
        read never fetches again on the day of a failed fetch, so a delisted
        or mis-typed symbol does not retry on every read; the engine's
        prefetch retries a failure until the day's first entry window
        (``daily_history_due``).
        """
        key = self._symbol_key(symbol)
        with self._lock:
            if self.last_daily_refresh.get(key) == sessions.now_et().date():
                cached = self.daily_history.get(key)
                return None if cached is None else cached.copy()
        return self.fetch_daily_history(symbol, calendar_days)

    def fetch_daily_history(self, symbol: str, calendar_days: int = 180) -> pd.DataFrame | None:
        """Fetch *symbol*'s daily OHLC bars now (one Schwab ``price_history``
        call) and store them for today's ET date; returns them, or ``None``.

        A payload whose candles do not read is a failed fetch; until
        2026-09-26 its error escaped uncached, so the fetch was retried every
        cycle. A failed fetch is logged with its type and stored as ``None``
        for the day with the time it failed (``daily_history_due``); one
        with no completed session is logged and stored as ``None`` too, as
        an answer. Broad because the fetch goes through the Schwab client,
        which raises any type.

        Completed sessions only: the bars dated before today (ET). During the
        session Schwab's daily response ends with today's forming bar (the
        archived 09:35 fetches of 2026-09-18..25 each counted it), and the
        bar was cached for the day as of the fetch, so the ADR and the beta
        took whatever the session had printed by then: five minutes of it at
        the 09:35 entry pass, three hours after a 12:35 restart. Since
        2026-09-28 the engine fetches in the prewarm, before the open
        (``IntradayBot._prefetch_daily_history``), so the frame no longer
        depends on when the fetch ran.
        """
        key = self._symbol_key(symbol)
        now = sessions.now_et()
        today = now.date()
        start = now - timedelta(days=max(1, int(calendar_days)))
        try:
            payload, source_symbol = self._fetch_price_history_payload_with_aliases(
                symbol,
                periodType="year",
                frequencyType="daily",
                frequency=1,
                startDate=start,
                endDate=now,
                needExtendedHoursData=False,
                needPreviousClose=False,
            )
            frame = self._history_candles_to_frame(payload.get("candles", []))
        except Exception as exc:
            LOG.warning("Daily price_history fetch failed for %s: %s: %s; no daily stats for it until a fetch succeeds.",
                        symbol, type(exc).__name__, exc)
            with self._lock:
                self.daily_history[key] = None
                self.last_daily_refresh[key] = today
                self.daily_history_failed_at[key] = now
            return None
        if not frame.empty:
            frame = frame.loc[frame.index < pd.Timestamp(today).tz_localize(EXCHANGE_TZ)]
        if frame.empty:
            LOG.warning("Daily price_history returned no completed session for %s (via %s).", symbol, source_symbol)
            frame_or_none = None
        else:
            frame_or_none = frame
            LOG.info("Daily history for %s: %d sessions (via %s).", symbol, len(frame), source_symbol)
        with self._lock:
            self.daily_history[key] = frame_or_none
            self.last_daily_refresh[key] = today
            self.daily_history_failed_at.pop(key, None)
        return None if frame_or_none is None else frame_or_none.copy()

    def get_support_resistance(
        self,
        symbol: str,
        current_price: float | None = None,
        *,
        flip_frame: pd.DataFrame | None = None,
        mode: str = "default",
        timeframe_minutes: int | None = None,
        use_prior_day_high_low: bool | None = None,
        use_prior_week_high_low: bool | None = None,
    ) -> SupportResistanceContext | None:
        """``symbol``'s S/R context on its stored (symbol, tf) HTF frame, or
        None while none is stored (or support_resistance is off).

        A read: it never fetches (``refresh_htf_frame``). Cached for the
        cycle per (symbol, tf, mode, prior day, prior week): the first read
        of a cycle builds it, with its ``current_price`` and ``flip_frame``,
        and every later read in the cycle gets that build, until a refresh
        of the frame drops it (``_invalidate_cycle_htf``). Until 2026-09-28
        a read passing ``allow_refresh`` fetched the frame when its HTF bar
        had closed, and a default-mode read without a price could be served
        a context the step's own fetch had built (``sr_cache``, gone with
        that fetch).

        Across cycles the build is kept in the level memo (context_memo),
        keyed on the stored frame object, the price, the flip frame's version
        and what the build reads of the clock (the flip frame's completed
        bars, the session date, the ATR's session switch): a cycle without a
        new bar or HTF refresh reads the last build instead of redoing it."""
        cfg = getattr(self.config, "support_resistance", None)
        if cfg is None or not bool(cfg.enabled):
            return None
        tf = int(timeframe_minutes or getattr(cfg, "timeframe_minutes", 15) or 15)
        normalized_mode = str(mode or "default").strip().lower()
        resolved_use_prior_day_high_low = bool(getattr(cfg, "use_prior_day_high_low", True) if use_prior_day_high_low is None else use_prior_day_high_low)
        resolved_use_prior_week_high_low = bool(getattr(cfg, "use_prior_week_high_low", True) if use_prior_week_high_low is None else use_prior_week_high_low)
        cycle_key = (
            self._symbol_key(symbol),
            tf,
            normalized_mode,
            resolved_use_prior_day_high_low,
            resolved_use_prior_week_high_low,
        )
        with self._lock:
            if self._cycle_active and cycle_key in self._cycle_sr_cache:
                return self._cycle_sr_cache[cycle_key]
            # Read once: the memo keys on this object and the build reads a
            # copy of it, so the two cannot come from different refreshes.
            stored = self.history_htf.get(self._htf_key(symbol, tf))
        if stored is None or stored.empty:
            with self._lock:
                if self._cycle_active:
                    self._cycle_sr_cache[cycle_key] = None
            return None
        flip_1m, flip_5m = cfg.flip_confirmation_bars() if normalized_mode == "trading" else (0, 0)
        flip_version = None if flip_frame is None else frame_version(flip_frame)

        def build() -> SupportResistanceContext:
            return self._build_support_resistance(stored, cfg, tf, current_price, flip_frame, flip_1m, flip_5m,
                                                  resolved_use_prior_day_high_low, resolved_use_prior_week_high_low)

        def clock_key() -> tuple:
            now = sessions.now_et()
            return flip_frame_clock_key(flip_frame, now), latest_session_date(now), indicator_clock_key()

        if flip_frame is not None and flip_version is None:
            # A flip frame that is no get_merged hand-out has no version to
            # key on: built every cycle, as before the memo.
            ctx = build()
        else:
            # The stored HTF object is keyed by id and pinned by the entry, so
            # its id cannot be reused while the entry lives; every refresh
            # stores a new object (_refresh_htf_frame).
            ctx = self._level_memo.serve(
                ("sr", *cycle_key),
                (id(stored), None if current_price is None else float(current_price), flip_version, flip_1m, flip_5m),
                clock_key,
                lambda _clock: build(),
                pins=(stored,),
            )
        with self._lock:
            if self._cycle_active:
                self._cycle_sr_cache[cycle_key] = ctx
        return ctx

    @staticmethod
    def _build_support_resistance(
        stored: pd.DataFrame,
        cfg: Any,
        tf: int,
        current_price: float | None,
        flip_frame: pd.DataFrame | None,
        flip_1m: int,
        flip_5m: int,
        use_prior_day_high_low: bool,
        use_prior_week_high_low: bool,
    ) -> SupportResistanceContext:
        return build_support_resistance_context(
            stored.copy(deep=False),
            current_price=current_price,
            pivot_span=int(cfg.pivot_span),
            max_levels_per_side=int(cfg.max_levels_per_side),
            atr_tolerance_mult=float(cfg.atr_tolerance_mult),
            pct_tolerance=float(cfg.pct_tolerance),
            same_side_min_gap_atr_mult=float(cfg.same_side_min_gap_atr_mult),
            same_side_min_gap_pct=float(cfg.same_side_min_gap_pct),
            fallback_reference_max_drift_atr_mult=float(getattr(cfg, "fallback_reference_max_drift_atr_mult", 1.0) or 1.0),
            fallback_reference_max_drift_pct=float(getattr(cfg, "fallback_reference_max_drift_pct", 0.01) or 0.01),
            proximity_atr_mult=float(cfg.proximity_atr_mult),
            breakout_atr_mult=float(cfg.breakout_atr_mult),
            breakout_buffer_pct=float(cfg.breakout_buffer_pct),
            stop_buffer_atr_mult=float(cfg.stop_buffer_atr_mult),
            structure_eq_atr_mult=float(getattr(cfg, "structure_eq_atr_mult", 0.25)),
            structure_event_max_age_bars=cfg.htf_structure_event_lookback(),
            structure_min_range_atr_mult=float(getattr(cfg, "structure_min_range_atr_mult", 1.5) or 0.0),
            use_prior_day_high_low=use_prior_day_high_low,
            use_prior_week_high_low=use_prior_week_high_low,
            flip_frame=flip_frame,
            flip_confirmation_1m_bars=flip_1m,
            flip_confirmation_5m_bars=flip_5m,
            timeframe_minutes=tf,
        )


    def _quote_batch_chunks(self, symbols: list[str]) -> list[list[str]]:
        batch_size = max(1, int(self.config.runtime.quote_batch_size))
        return [symbols[idx: idx + batch_size] for idx in range(0, len(symbols), batch_size)]

    def _parallel_quote_fetch(self, symbols: list[str]) -> tuple[dict[str, dict], list[str]]:
        """Run `_fetch_single_quote_with_aliases` across symbols in parallel.

        Cache writes inside the worker are guarded by `self._lock`, so multiple
        workers can refresh distinct symbols safely. Returns the per-symbol
        normalized payloads on success, plus the list of symbols that raised.
        """
        fetched: dict[str, dict] = {}
        failed: list[str] = []
        if not symbols:
            return fetched, failed

        # Per-symbol blacklist gate. After a symbol exceeds
        # `runtime.max_consecutive_quote_failures` consecutive quote-fetch
        # failures (typically symbol-specific Schwab 401/403/404 such as
        # restricted-security responses), skip it silently for the rest
        # of the session. Recovers on bot restart. The count is validated
        # at load (an integer >= 0; 0 turns the gate off).
        failure_threshold = self.config.runtime.max_consecutive_quote_failures
        if failure_threshold > 0 and self._quote_blacklist:
            symbols = [s for s in symbols if s not in self._quote_blacklist]
            if not symbols:
                return fetched, failed

        def _record_success(sym: str) -> None:
            if failure_threshold > 0:
                self._consecutive_quote_failures.pop(sym, None)

        def _record_failure(sym: str, exc: Exception) -> None:
            LOG.warning("Quote fetch failed for %s: %s", sym, exc)
            failed.append(sym)
            if failure_threshold <= 0:
                return
            count = self._consecutive_quote_failures.get(sym, 0) + 1
            self._consecutive_quote_failures[sym] = count
            if count >= failure_threshold and sym not in self._quote_blacklist:
                self._quote_blacklist[sym] = sessions.now_et()
                LOG.warning(
                    "Blacklisting %s from quote refresh after %d consecutive failures (last: %s); will retry on bot restart",
                    sym, failure_threshold, exc,
                )

        # runtime.cycle_fetch_workers, which also sizes the engine's fetch
        # pool; validated at load: an integer >= 1.
        workers = min(self.config.runtime.cycle_fetch_workers, len(symbols))
        if workers < 2:
            for sym in symbols:
                try:
                    fetched[sym] = self._fetch_single_quote_with_aliases(sym)
                    _record_success(sym)
                except Exception as exc:
                    _record_failure(sym, exc)
            return fetched, failed
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bot-quote-fetch") as executor:
            futures = {executor.submit(self._fetch_single_quote_with_aliases, sym): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    fetched[sym] = future.result()
                    _record_success(sym)
                except Exception as exc:
                    _record_failure(sym, exc)
        return fetched, failed

    def _quote_aliases(self, symbol: str) -> list[str]:
        # If a previous fetch resolved this symbol to a specific Schwab
        # alias (e.g. NYICDX -> $NYICDX), short-circuit to a single-element
        # list so the caller skips the multi-alias retry loop entirely.
        # The fetcher (quote or price_history) hits the right alias on the
        # first attempt; the batch quote path also routes the symbol into
        # batch_pending (since len([cached]) == 1) instead of alias_pending.
        sym = str(symbol).strip()
        cached = self._resolved_quote_alias.get(sym.upper())
        if cached:
            return [cached]
        aliases = QUOTE_SYMBOL_ALIASES.get(sym.upper(), [sym])
        out: list[str] = []
        for item in aliases:
            token = str(item or "").strip()
            if token and token not in out:
                out.append(token)
        if sym and sym not in out:
            out.append(sym)
        return out

    def _record_resolved_alias(self, symbol: str, request_symbol: str) -> None:
        # Cache only when the resolved alias actually differs from the
        # original — there's nothing to gain from caching identity
        # resolutions, and skipping them keeps the cache focused on
        # symbols that genuinely need substitution (NYICDX/VIX, etc.).
        sym = str(symbol).strip()
        request = str(request_symbol).strip()
        if not sym or not request or sym.upper() == request.upper():
            return
        self._resolved_quote_alias[sym.upper()] = request

    def _fetch_price_history_payload_with_aliases(self, symbol: str, **kwargs) -> tuple[dict, str]:
        last_exc: Exception | None = None
        fallback_payload: dict | None = None
        fallback_symbol = str(symbol)
        saw_success = False
        aliases = self._quote_aliases(symbol)
        if len(aliases) > 1:
            LOG.debug("price_history alias attempts for %s: %s", symbol, aliases)
        for request_symbol in aliases:
            try:
                payload = call_schwab_json(self.client, "price_history", symbol=request_symbol, **kwargs)
                if not isinstance(payload, dict):
                    payload = {}
                candles = payload.get("candles", [])
                if candles:
                    if request_symbol != str(symbol):
                        LOG.debug("price_history alias success for %s via %s candles=%d", symbol, request_symbol, len(candles))
                        self._record_resolved_alias(symbol, request_symbol)
                    return payload, request_symbol
                if len(aliases) > 1:
                    LOG.info("price_history alias returned no candles for %s via %s", symbol, request_symbol)
                if not saw_success:
                    fallback_payload = payload
                    fallback_symbol = request_symbol
                    saw_success = True
            except Exception as exc:
                last_exc = exc
                if len(aliases) > 1:
                    LOG.warning("price_history alias failed for %s via %s: %s", symbol, request_symbol, exc)
                continue
        if saw_success:
            if len(aliases) > 1:
                LOG.warning("price_history alias fallback used for %s via %s with empty candles", symbol, fallback_symbol)
            return fallback_payload or {}, fallback_symbol
        raise last_exc or RuntimeError(f"price_history fetch failed for {symbol}")

    def _fetch_single_quote_with_aliases(self, symbol: str) -> dict:
        last_exc: Exception | None = None
        aliases = self._quote_aliases(symbol)
        if len(aliases) > 1:
            LOG.debug("Quote alias attempts for %s: %s", symbol, aliases)
        for request_symbol in aliases:
            try:
                payload = self._quote_request(f"quote {request_symbol}", call_schwab_json, self.client, "quote",
                                              request_symbol)
                extracted = self._extract_quote_payloads(payload, [request_symbol, symbol])
                quote_payload = extracted.get(symbol) or extracted.get(request_symbol)
                if quote_payload is None and isinstance(payload, dict):
                    quote_payload = payload
                normalized = self._normalize_quote(symbol, quote_payload)
                normalized["source_symbol"] = request_symbol
                if request_symbol != str(symbol):
                    LOG.debug("Quote alias success for %s via %s", symbol, request_symbol)
                    self._record_resolved_alias(symbol, request_symbol)
                return normalized
            except Exception as exc:
                last_exc = exc
                if len(aliases) > 1:
                    LOG.warning("quote alias failed for %s via %s: %s", symbol, request_symbol, exc)
                continue
        raise last_exc or RuntimeError(f"Quote fetch failed for {symbol}")

    @staticmethod
    def _extract_quote_payloads(payload, requested: list[str]) -> dict[str, dict]:
        requested_map = {str(symbol).upper(): str(symbol) for symbol in requested}
        out: dict[str, dict] = {}

        def _maybe_store(sym, value):
            if sym is None or not isinstance(value, dict):
                return
            key = str(sym).upper()
            wanted = requested_map.get(key)
            if wanted is not None:
                out[wanted] = value

        if isinstance(payload, dict):
            for sym in requested:
                if sym in payload and isinstance(payload.get(sym), dict):
                    out[sym] = payload[sym]
                elif sym.upper() in payload and isinstance(payload.get(sym.upper()), dict):
                    out[sym] = payload[sym.upper()]
            if len(out) == len(requested):
                return out
            nested = payload.get("quotes") or payload.get("securities") or payload.get("instruments")
            if isinstance(nested, dict):
                for sym, value in nested.items():
                    _maybe_store(sym, value)
            elif isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        sym = item.get("symbol") or item.get("assetMainType")
                        _maybe_store(sym, item)
            if len(out) == len(requested):
                return out
            for sym, value in payload.items():
                _maybe_store(sym, value)
        elif isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                sym = item.get("symbol") or item.get("key")
                _maybe_store(sym, item)
        return out

    def _quote_request(self, what: str, call: Callable[..., Any], *args: Any) -> Any:
        """One REST quote request, ``call(*args)``, timed. A request that
        raises, answers a non-2xx status or takes longer than
        ``STREAM_QUOTE_SHADOW_SLOW_SECONDS`` is noted
        (``_rest_quote_trouble``), and the stream quotes' REST shadow skips
        for ``STREAM_QUOTE_SHADOW_BACKOFF_SECONDS`` after it. A TypeError is
        not noted: it is the client refusing the argument's form before any
        request goes out, which ``_try_batch_quote_request`` answers with the
        next form. It runs on the fetch pool's threads too (the single-quote
        fallbacks): the note is one attribute assignment."""
        started = time.monotonic()
        try:
            result = call(*args)
        except TypeError:
            raise
        except Exception as exc:
            self._rest_quote_trouble = (sessions.now_et(), f"{what} raised {type(exc).__name__}")
            raise
        elapsed = time.monotonic() - started
        status = getattr(result, "status_code", None)
        if status is not None and not response_ok(result):
            self._rest_quote_trouble = (sessions.now_et(), f"{what} answered status {status}")
        elif elapsed > STREAM_QUOTE_SHADOW_SLOW_SECONDS:
            self._rest_quote_trouble = (sessions.now_et(), f"{what} took {elapsed:.1f} s")
        return result

    def _try_batch_quote_request(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}

        methods: list[str] = []
        if hasattr(self.client, "quotes"):
            methods.append("quotes")
        if len(symbols) > 1 and hasattr(self.client, "quote"):
            methods.append("quote")

        for method_name in methods:
            attempts: list[list[str] | str] = [symbols]
            joined = ",".join(symbols)
            if joined:
                attempts.append(joined)

            for arg in attempts:
                try:
                    response = self._quote_request(f"{method_name} of {len(symbols)}", call_schwab_client, self.client,
                                                   method_name, arg)
                    if not response_ok(response):
                        status_code = getattr(response, "status_code", None)
                        body_preview = str(getattr(response, "text", "") or "")[:240]
                        loud = not isinstance(status_code, int) or status_code in {401, 403, 404, 429} or status_code >= 500
                        log_fn = LOG.warning if loud else LOG.debug
                        log_fn(
                            "Batch quote request via %s returned status=%s for %s%s",
                            method_name,
                            status_code,
                            symbols,
                            f": {body_preview}" if body_preview else "",
                        )
                        continue
                    payload = response.json()
                    extracted = self._extract_quote_payloads(payload, symbols)
                    if extracted:
                        LOG.debug("Fetched %d quotes via batch %s", len(extracted), method_name)
                        return extracted
                    LOG.debug("Batch quote request via %s returned no extractable quotes for %s", method_name, symbols)
                except TypeError:
                    continue
                except Exception as exc:
                    LOG.debug("Batch quote request via %s failed for %s: %s", method_name, symbols, exc)
                    break

        return {}

    def fetch_quotes(
        self,
        symbols: Iterable[str],
        force: bool = False,
        min_force_interval_seconds: float | None = None,
        source: str | None = None,
    ) -> dict[str, dict]:
        """Quotes for ``symbols``, cached in ``quote_cache``. A non-forced
        call (the engine's refresh) serves each requested symbol whose stream
        quote book is fresh from the book (``_serve_stream_quote``, no TTL
        wait); every other symbol is served from the cache while its quote is
        younger than ``_quote_ttl`` and fetched by REST otherwise. A forced
        call fetches REST (a symbol fetched less than
        ``min_force_interval_seconds`` ago, which only option callers pass, is
        served from the cache). One ``Quote refresh`` INFO line per call
        counts each way."""
        out: dict[str, dict] = {}
        requested = sorted({self._symbol_key(s) for s in symbols if str(s).strip()})
        pending: list[str] = []
        cached_hits = 0
        batch_hits = 0
        fallback_hits = 0
        failures = 0
        force_cooldown_hits = 0
        stream_hits = 0
        rest_stored: list[str] = []
        stream_served: list[str] = []
        stream = self._stream_quote_read(requested, force=force)
        for symbol in requested:
            if stream is not None:
                published = self._serve_stream_quote(symbol, stream)
                if published is not None:
                    out[symbol] = published
                    stream_hits += 1
                    stream_served.append(symbol)
                    continue
            if force:
                cached = self.quote_cache.get(symbol)
                last_refresh = self.last_quote_refresh.get(symbol)
                if cached is not None and last_refresh is not None and min_force_interval_seconds is not None:
                    age = (sessions.now_et() - last_refresh).total_seconds()
                    if age < max(0.0, float(min_force_interval_seconds)):
                        out[symbol] = cached
                        cached_hits += 1
                        force_cooldown_hits += 1
                        continue
            if not force and not self.should_refresh_quote(symbol):
                cached = self.quote_cache.get(symbol)
                if cached is not None:
                    out[symbol] = cached
                    cached_hits += 1
                continue
            pending.append(symbol)

        # Symbols with a multi-alias list AND no cached resolution yet have
        # to go through the single-quote fetch path so we can discover
        # which alias actually works. Symbols with a cached alias (or a
        # 1-element alias list to begin with) can ride the batch path,
        # with the cached alias substituted into the actual request.
        alias_pending = [symbol for symbol in pending if len(self._quote_aliases(symbol)) > 1]
        batch_pending = [symbol for symbol in pending if symbol not in alias_pending]

        for chunk in self._quote_batch_chunks(batch_pending):
            # Substitute resolved aliases into the request list so macro
            # symbols (NYICDX -> $NYICDX, VIX -> $VIX, etc.) piggyback on
            # the streamable batch instead of needing per-cycle single
            # fetches. request_to_original maps the resolved alias back
            # to the original cache key for response remapping.
            request_chunk: list[str] = []
            request_to_original: dict[str, str] = {}
            for symbol in chunk:
                resolved = self._resolved_quote_alias.get(str(symbol).upper(), symbol)
                request_chunk.append(resolved)
                if resolved != symbol:
                    request_to_original[resolved] = symbol
            fetched: dict[str, dict] = {}
            if len(request_chunk) > 1:
                raw_fetched = self._try_batch_quote_request(request_chunk)
                if request_to_original:
                    # Schwab returns quotes keyed by request_symbol; remap
                    # to the original cache key so downstream lookups
                    # (and the existing fallback `if symbol not in fetched`
                    # check) see the symbols the rest of the bot uses.
                    fetched = {request_to_original.get(req, req): payload for req, payload in raw_fetched.items()}
                else:
                    fetched = raw_fetched
            fetched_at = sessions.now_et()
            for symbol, quote_payload in fetched.items():
                out[symbol] = self._store_rest_quote(symbol, self._normalize_quote(symbol, quote_payload), fetched_at)
                rest_stored.append(symbol)
                batch_hits += 1
            fallback_targets = [symbol for symbol in chunk if symbol not in fetched]
            fallback_results, fallback_failed = self._parallel_quote_fetch(fallback_targets)
            for symbol, normalized in fallback_results.items():
                out[symbol] = self._store_rest_quote(symbol, normalized, sessions.now_et())
                rest_stored.append(symbol)
                fallback_hits += 1
            for symbol in fallback_failed:
                failures += 1
                cached = self.quote_cache.get(symbol)
                if cached is not None:
                    out[symbol] = cached

        alias_results, alias_failed = self._parallel_quote_fetch(alias_pending)
        for symbol, normalized in alias_results.items():
            out[symbol] = self._store_rest_quote(symbol, normalized, sessions.now_et())
            rest_stored.append(symbol)
            fallback_hits += 1
        for symbol in alias_failed:
            failures += 1
            cached = self.quote_cache.get(symbol)
            if cached is not None:
                out[symbol] = cached

        if force:
            self._warn_forced_fetch_fallbacks(source, pending, rest_stored)
            self._log_stream_quote_checks(source, rest_stored)
        if stream_served and self.config.runtime.stream_quote_shadow_every > 0:
            # Counted here for the REST shadow, which never runs here, in the
            # quotes phase (run_stream_quote_shadow).
            self._stream_quote_publications += 1
            self._stream_quote_served.update(stream_served)
        if requested:
            served = not pending and not failures and cached_hits + stream_hits == len(requested)
            mode = ("stream" if stream_hits else "all_cached") if served else "refresh"
            LOG.info(
                "Quote refresh source=%s mode=%s requested=%d cached=%d pending=%d batch=%d fallback=%d failed=%d force=%s force_cooldown_cached=%d stream=%d",
                str(source or "unspecified"),
                mode,
                len(requested),
                cached_hits,
                len(pending),
                batch_hits,
                fallback_hits,
                failures,
                force,
                force_cooldown_hits,
                stream_hits,
            )
        return out

    def _stream_quote_read(self, requested: list[str], *, force: bool) -> StreamQuoteRead | None:
        """The stream quote books a non-forced ``fetch_quotes`` may serve:
        a read of the fresh books among ``requested``
        (``StreamQuoteState.read``, at ``_quote_ttl``), after which the
        stream's silent/live transition is logged. None for a forced fetch,
        with ``runtime.stream_quotes`` off or nothing requested, and when the
        read fails: logged with the error's type, the call then serves REST
        (settled L2; a lock timeout replaces the books, ``_reset_stream_quotes``)."""
        if force or not requested or not self.config.runtime.stream_quotes:
            return None
        now = sessions.now_et()
        ttl = self._quote_ttl()
        try:
            read = self.stream_quotes.read(requested, now, ttl)
        except StreamQuoteLockTimeout as exc:
            self._reset_stream_quotes("the quote read", exc)
            return None
        except Exception as exc:
            LOG.error("Stream quotes: the quote read failed (%s: %s); this refresh serves REST", type(exc).__name__, exc)
            return None
        self._log_stream_quote_transition(read, now, ttl, len(requested))
        try:
            if self.stream_quotes.maybe_log_health(now, ttl, self._stream_quote_transitions):
                self._stream_quote_transitions = 0
        except StreamQuoteLockTimeout as exc:
            self._reset_stream_quotes("the health line", exc)
        except Exception as exc:
            LOG.error("Stream quotes: the health line failed (%s: %s)", type(exc).__name__, exc)
        return read

    def _warn_forced_fetch_fallbacks(self, source: str | None, pending: list[str], rest_stored: list[str]) -> None:
        """A forced fetch that stored no REST quote for a symbol (its
        request failed, or the symbol is blacklisted) leaves the cache as it
        was, and its readers (an entry's market read, the management
        snapshot) take a cached quote younger than ``_quote_ttl``: since the
        stream quotes, usually the stream's. One WARNING per such symbol
        naming that quote's source and age (settled U2); none for a cached
        quote too old for them."""
        stored = set(rest_stored)
        missed = [symbol for symbol in pending if symbol not in stored]
        if not missed:
            return
        now = sessions.now_et()
        ttl = self._quote_ttl()
        for symbol in missed:
            with self._lock:
                cached = self.quote_cache.get(symbol)
            fetched_at = (cached or {}).get("fetched_at")
            if fetched_at is None:
                continue
            age = max(0.0, (now - fetched_at).total_seconds())
            if age > ttl:
                continue
            LOG.warning("Forced quote fetch failed for %s (source=%s); the cached %s quote, %.1f s old, stands in for "
                        "it (limit %.0f s)", symbol, str(source or "unspecified"), cached.get("quote_source", "unknown"),
                        age, ttl)

    def _log_stream_quote_checks(self, source: str | None, symbols: list[str]) -> None:
        """For each symbol a forced fetch stored by REST whose stream book is
        fresh now, one INFO line setting the two side by side: ``Stream quote
        check symbol=... source=... epoch=... stream_age_s=... bid=REST/stream
        ask=... last=... mark=... d_bid=... d_ask=... d_last=... d_mark=...
        quote_time_lag_ms=... exchange=REST/stream`` (``d_`` is stream minus
        REST; the lag is REST's ``quoteTime`` minus the book's field 34; the
        exchanges are the two raw names). It never fails the fetch: an error
        is logged with its type."""
        if not symbols or not self.config.runtime.stream_quotes:
            return
        now = sessions.now_et()
        try:
            read = self.stream_quotes.read(symbols, now, self._quote_ttl())
        except StreamQuoteLockTimeout as exc:
            self._reset_stream_quotes("the forced-fetch check", exc)
            return
        except Exception as exc:
            LOG.error("Stream quotes: the forced-fetch check failed (%s: %s)", type(exc).__name__, exc)
            return
        for symbol in symbols:
            values = read.books.get(symbol)
            if values is None or read.at is None:
                continue
            try:
                with self._lock:
                    rest = dict(self.quote_cache.get(symbol) or {})
                raw = rest.get("raw") if isinstance(rest.get("raw"), dict) else {}
                pairs = [(name, rest.get(name), values.get(fid)) for name, fid in
                         (("bid", "1"), ("ask", "2"), ("last", "3"), ("mark", "33"))]
                rest_time = (raw.get("quote") or {}).get("quoteTime") if isinstance(raw.get("quote"), dict) else None
                stream_time = values.get("34")
                lag = (str(int(rest_time) - int(stream_time)) if isinstance(rest_time, (int, float))
                       and not isinstance(rest_time, bool) and stream_time is not None else "na")
                reference = raw.get("reference") if isinstance(raw.get("reference"), dict) else {}
                LOG.info("Stream quote check symbol=%s source=%s epoch=%d stream_age_s=%.2f %s %s "
                         "quote_time_lag_ms=%s exchange=%s/%s", symbol, str(source or "unspecified"), read.epoch,
                         max(0.0, (now - read.at).total_seconds()),
                         " ".join(f"{name}={_check_text(r)}/{_check_text(s)}" for name, r, s in pairs),
                         " ".join(f"d_{name}={_check_delta(r, s)}" for name, r, s in pairs), lag,
                         reference.get("exchangeName"), values.get("25"))
            except Exception as exc:
                LOG.error("Stream quotes: the forced-fetch check of %s failed (%s: %s)", symbol, type(exc).__name__,
                          exc)

    def run_stream_quote_shadow(self) -> None:
        """The stream quotes' REST shadow (settled Q2, with M4's fix), which
        the engine runs once a pass after management and the entries, never
        in the quotes phase. Every ``runtime.stream_quote_shadow_every``-th
        publication of stream quotes (0: never), the symbols the stream served
        since the last check are fetched by REST in uncached batches and
        compared with their books (``_check_stream_quotes_against_rest``).
        Within ``STREAM_QUOTE_SHADOW_BACKOFF_SECONDS`` of a REST quote request
        that failed or was slow (``_quote_request``) it skips, logging that
        once per run of skips, and checks at the first pass after. It never
        fails the pass: an error is logged with its type (a lock timeout
        replaces the books, ``_reset_stream_quotes``)."""
        every = self.config.runtime.stream_quote_shadow_every
        if every <= 0 or self._stream_quote_publications < every:
            return
        now = sessions.now_et()
        trouble = self._rest_quote_trouble
        if trouble is not None and (now - trouble[0]).total_seconds() < STREAM_QUOTE_SHADOW_BACKOFF_SECONDS:
            if not self._stream_quote_shadow_skipping:
                self._stream_quote_shadow_skipping = True
                LOG.info("Stream quote shadow: skipped while REST quotes are in trouble (%s at %s); it checks %.0f s "
                         "after the last such request", trouble[1], trouble[0].strftime("%H:%M:%S"),
                         STREAM_QUOTE_SHADOW_BACKOFF_SECONDS)
            return
        self._stream_quote_shadow_skipping = False
        symbols = sorted(self._stream_quote_served)
        self._stream_quote_publications = 0
        self._stream_quote_served = set()
        try:
            self._check_stream_quotes_against_rest(symbols)
        except StreamQuoteLockTimeout as exc:
            self._reset_stream_quotes("the REST shadow", exc)
        except Exception as exc:
            LOG.error("Stream quote shadow: the check failed (%s: %s)", type(exc).__name__, exc)

    def _check_stream_quotes_against_rest(self, symbols: list[str]) -> None:
        """The shadow's check of ``symbols`` (the stream served each since the
        last check): one REST batch per ``quote_batch_size`` chunk
        (``_try_batch_quote_request``: no single-quote fallback, nothing
        cached), then the books read at once (the closest instant to REST's
        answer). For each symbol with both, the four prices' differences
        (stream minus REST) and the quote-time lag (REST's ``quoteTime``
        minus the book's field 34, when both carry one). A book that lags by
        more than ``_quote_ttl`` is dropped before it is served again and
        REST's quote is cached in its place (WARNING each): a frozen book is
        then neither published nor served from the cache, and serves again
        once its bid, ask, last and mark arrive again. One INFO line per
        check, ``Stream quote shadow: checked=... lagging=... rest_missing=...
        not_fresh=... differ=... max_abs_d_bid=... max_abs_d_ask=...
        max_abs_d_last=... max_abs_d_mark=... max_lag_ms=... rest_s=...``
        (each largest difference with its symbol when it is not zero)."""
        ttl = self._quote_ttl()
        started = time.monotonic()
        rest: dict[str, dict] = {}
        for chunk in self._quote_batch_chunks(symbols):
            rest.update(self._try_batch_quote_request(chunk))
        rest_seconds = time.monotonic() - started
        now = sessions.now_et()
        read = self.stream_quotes.read(symbols, now, ttl)
        largest: dict[str, tuple[float, str]] = {}
        max_lag: tuple[int, str] | None = None
        lagging: list[tuple[str, int, int, int, dict]] = []
        checked = rest_missing = not_fresh = differ = 0
        for symbol in symbols:
            payload = rest.get(symbol)
            values = read.books.get(symbol)
            if payload is None:
                rest_missing += 1
                continue
            if values is None:
                not_fresh += 1
                continue
            checked += 1
            normalized = self._normalize_quote(symbol, payload)
            differs = False
            for name, fid in (("bid", "1"), ("ask", "2"), ("last", "3"), ("mark", "33")):
                delta = abs(float(values[fid]) - float(normalized[name]))
                differs = differs or delta != 0.0
                if name not in largest or delta > largest[name][0]:
                    largest[name] = (delta, symbol)
            if differs:
                differ += 1
            section = payload.get("quote") if isinstance(payload.get("quote"), Mapping) else {}
            rest_time, stream_time = _quote_time(section.get("quoteTime")), values.get("34")
            if rest_time is not None and stream_time is not None:
                lag = rest_time - int(stream_time)
                if max_lag is None or lag > max_lag[0]:
                    max_lag = (lag, symbol)
                if lag > ttl * 1000.0:
                    lagging.append((symbol, lag, int(stream_time), rest_time, normalized))
        if lagging:
            # REST's quotes first: a lock timeout on the drop (which replaces
            # the books) leaves the cache holding them all the same.
            for symbol, _lag, _stream_time, _rest_time, normalized in lagging:
                self._store_rest_quote(symbol, normalized, now)
            self.stream_quotes.drop([entry[0] for entry in lagging])
            for symbol, lag, stream_time, rest_time, _normalized in lagging:
                LOG.warning("Stream quote shadow: %s lags REST by %d ms (quote time %d against REST's %d, limit "
                            "%.0f s); its book is dropped and REST's quote cached in its place, and it serves again "
                            "once its bid, ask, last and mark arrive again", symbol, lag, stream_time, rest_time, ttl)

        def _largest(name: str) -> str:
            if name not in largest:
                return "na"
            delta, which = largest[name]
            return f"{delta:.4f}@{which}" if delta > 0 else f"{delta:.4f}"

        LOG.info("Stream quote shadow: checked=%d of %d served symbols (epoch %d) lagging=%d rest_missing=%d "
                 "not_fresh=%d differ=%d max_abs_d_bid=%s max_abs_d_ask=%s max_abs_d_last=%s max_abs_d_mark=%s "
                 "max_lag_ms=%s rest_s=%.2f", checked, len(symbols), read.epoch, len(lagging), rest_missing, not_fresh,
                 differ, _largest("bid"), _largest("ask"), _largest("last"), _largest("mark"),
                 "na" if max_lag is None else f"{max_lag[0]}@{max_lag[1]}", rest_seconds)

    def _log_stream_quote_transition(self, read: StreamQuoteRead, now: datetime, ttl: float, requested: int) -> None:
        """Log the stream quotes going silent (no LEVELONE_EQUITIES data
        within ``ttl``: every symbol takes the REST path) or live again,
        judged once per non-forced read. Nothing is judged before a login or
        with nothing subscribed (the state is None, as after a stop), and the
        wait for an epoch's first data is no transition. A transition logs at
        WARNING (silent) or INFO (live), or at DEBUG when one of its kind was
        logged that loudly less than ``STREAM_QUOTE_TRANSITION_QUIET_SECONDS``
        before (settled L6: quiet periods flap); each counts in
        ``_stream_quote_transitions``."""
        if read.epoch == 0 or read.subscribed == 0:
            self._stream_quotes_live = None
            return
        previous, self._stream_quotes_live = self._stream_quotes_live, read.live
        if previous == read.live or (previous is None and not read.live):
            return
        self._stream_quote_transitions += 1
        last = self._stream_quote_transition_logged.get(read.live)
        loud = last is None or (now - last).total_seconds() >= STREAM_QUOTE_TRANSITION_QUIET_SECONDS
        if loud:
            self._stream_quote_transition_logged[read.live] = now
        if read.live:
            LOG.log(logging.INFO if loud else logging.DEBUG, "Stream quotes live: epoch %d, serving %d of %d requested "
                    "symbols", read.epoch, len(read.books), requested)
            return
        since = "since the login" if read.at is None else f"for {(now - read.at).total_seconds():.1f} s"
        LOG.log(logging.WARNING if loud else logging.DEBUG, "Stream quotes silent: no LEVELONE_EQUITIES data %s "
                "(limit %.0f s, epoch %d); %d requested symbols take the REST path", since, ttl, read.epoch, requested)

    def _serve_stream_quote(self, symbol: str, read: StreamQuoteRead) -> dict | None:
        """``symbol``'s fresh book published (``_publish_stream_quote``), or
        None: no fresh book, a newer cached quote, or a publication that
        failed. A failure is logged with its type and drops the book
        (settled L2), so the symbol takes the REST path and its book serves
        again once its bid, ask, last and mark have arrived again."""
        values = read.books.get(symbol)
        if values is None or read.at is None:
            return None
        try:
            return self._publish_stream_quote(symbol, values, read.at)
        except Exception as exc:
            LOG.error("Stream quotes: publishing %s failed (%s: %s); its book is dropped and REST serves it", symbol,
                      type(exc).__name__, exc)
            try:
                self.stream_quotes.drop([symbol])
            except StreamQuoteLockTimeout as again:
                self._reset_stream_quotes("the book drop", again)
            return None

    def _publish_stream_quote(self, symbol: str, values: Mapping[str, Any], at: datetime) -> dict | None:
        """Cache ``symbol``'s stream book as its quote, through
        ``_normalize_quote`` like a REST quote (the book in Schwab's REST
        names, ``stream_quotes.rest_payload``, a display field it lacks
        carried over from the cached quote's raw payload: settled L7),
        stamped ``fetched_at`` and ``last_quote_refresh`` with ``at`` (the
        receipt of the stream's last LEVELONE_EQUITIES data message, so the
        quote's age is honest and never more than ``_quote_ttl``) and
        ``quote_source`` "stream". Never over a newer quote: when the cached
        one's ``last_quote_refresh`` is later than ``at`` (a forced REST
        fetch after the stream's last message) nothing is written and None
        is returned; the symbol then takes the TTL path, which serves it."""
        with self._lock:
            cached = self.quote_cache.get(symbol)
            last = self.last_quote_refresh.get(symbol)
        if last is not None and last > at:
            return None
        normalized = self._normalize_quote(symbol, rest_payload(symbol, values,
                                                                (cached or {}).get("raw")))
        normalized["fetched_at"] = at
        normalized["quote_source"] = "stream"
        with self._lock:
            last = self.last_quote_refresh.get(symbol)
            if last is not None and last > at:
                return None
            self.quote_cache[symbol] = normalized
            self.last_quote_refresh[symbol] = at
        return normalized

    def _store_rest_quote(self, symbol: str, normalized: dict, fetched_at: datetime) -> dict:
        """Cache one REST quote (``_normalize_quote``'s dict): stamped
        ``fetched_at`` and ``quote_source`` "rest", with the symbol's
        ``last_quote_refresh``, under the store's lock. Every REST write of
        ``fetch_quotes`` (the batch, the single-quote fallback, the alias
        fetch) goes through it, each with its own ``fetched_at``; the first
        of a streamable equity logs the entitlement line
        (``_log_quote_entitlement``). Returns the stored dict."""
        normalized["fetched_at"] = fetched_at
        normalized["quote_source"] = "rest"
        with self._lock:
            self.quote_cache[symbol] = normalized
            self.last_quote_refresh[symbol] = fetched_at
        if not self._quote_entitlement_logged and is_streamable_equity(symbol):
            self._log_quote_entitlement(symbol, normalized.get("raw"))
        return normalized

    def _log_quote_entitlement(self, symbol: str, raw: Any) -> None:
        """Once per store (the bot has one), from its first REST quote of a
        streamable equity (settled L8: an index's or an option's payload
        names other values): ``Schwab quote entitlement (first REST quote,
        SYM): realtime=... quoteType=... top_keys=[...] quote_keys=[...]
        reference_keys=[...]``, what Schwab says the account's equity quotes
        are and the names its payload carries (the payload's own keys, its
        ``quote`` section's and its ``reference`` section's: the names the
        stream's adapter and ``_normalize_quote`` rely on, such as
        ``netPercentChange``; ``none`` for a section the payload lacks). INFO
        when ``realtime`` is True, WARNING otherwise (a delayed entitlement,
        or a payload without the flag)."""
        self._quote_entitlement_logged = True
        payload = raw if isinstance(raw, Mapping) else {}

        def keys(section: Any) -> str:
            return f"[{','.join(sorted(str(key) for key in section))}]" if isinstance(section, Mapping) else "none"

        realtime = payload.get("realtime")
        LOG.log(logging.INFO if realtime is True else logging.WARNING,
                "Schwab quote entitlement (first REST quote, %s): realtime=%s quoteType=%s top_keys=%s quote_keys=%s "
                "reference_keys=%s", symbol, realtime, payload.get("quoteType"), keys(raw), keys(payload.get("quote")),
                keys(payload.get("reference")))

    def get_quote(self, symbol: str) -> dict | None:
        # Shallow copy is sufficient: callers only read top-level scalar keys
        # (bid/ask/mark/fetched_at/etc). Shallow .copy() is ~10x faster than
        # copy.deepcopy() on the typical ~20-key quote dict, and get_quote is
        # called in multiple hot paths (execution, strategy spread validation).
        with self._lock:
            quote = self.quote_cache.get(self._symbol_key(symbol))
            return quote.copy() if quote is not None else None

    def has_stream_symbols(self) -> bool:
        with self._lock:
            return bool(self.stream_symbols)

    def dashboard_data_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "history_symbols": len(self.history),
                "stream_symbols": sorted(self.stream_symbols),
                "quote_symbols": sorted(self.quote_cache.keys()),
            }

    def symbol_data_state(self, symbol: str) -> dict[str, object]:
        key = self._symbol_key(symbol)
        with self._lock:
            history = self.history.get(key)
            live = self.live.get(key)
            stats = self.merge_stats.get(key, MergeStats())
            return {
                "symbol": key,
                "history_rows": 0 if history is None else len(history),
                "live_rows": 0 if live is None else len(live),
                "merged_rows": max(int(getattr(stats, "history_rows", 0) or 0), 0) + max(int(getattr(stats, "stream_rows", 0) or 0), 0),
                "quote_cached": key in self.quote_cache,
                "stream_subscribed": key in self.stream_symbols,
                "last_history_refresh": self.last_history_refresh.get(key),
                "last_empty_history_refresh": self.last_empty_history_refresh.get(key),
                "last_stream_update": self.last_stream_update.get(key),
                "last_stream_bar_time": self.last_stream_bar_time.get(key),
                "last_quote_refresh": self.last_quote_refresh.get(key),
                "forced_premarket_refresh_date": self._forced_premarket_history_refresh_date.get(key),
                "live_entry_bar_status": self.live_entry_bar_status(key),
            }


    def quote_age_seconds(self, symbol: str) -> float | None:
        # Read fetched_at directly under the lock; avoid the deepcopy that
        # get_quote() does. For freshness checks we only need one field.
        key = self._symbol_key(symbol)
        with self._lock:
            quote = self.quote_cache.get(key)
            if not quote:
                return None
            fetched_at = quote.get("fetched_at")
        if fetched_at is None:
            return None
        # fetch_quotes stamps every cached quote with sessions.now_et().
        return max(0.0, (sessions.now_et() - fetched_at).total_seconds())

    def quotes_are_fresh(self, symbols: Iterable[str], max_age_seconds: float) -> bool:
        # Single lock acquire + single sessions.now_et() call, plus early exit on first
        # stale symbol. Avoids N deepcopies + N lock acquires from the prior
        # implementation that called quote_age_seconds() per symbol.
        limit = float(max_age_seconds)
        current = sessions.now_et()
        with self._lock:
            for symbol in symbols:
                quote = self.quote_cache.get(self._symbol_key(str(symbol)))
                if not quote:
                    return False
                fetched_at = quote.get("fetched_at")
                if fetched_at is None:
                    return False
                age = max(0.0, (current - fetched_at).total_seconds())
                if age > limit:
                    return False
        return True

    def stream_quote_fresh(self, symbol: str) -> bool:
        """Whether ``symbol``'s stream quote book would be served now: the
        freshness every non-forced ``fetch_quotes`` publishes by
        (``StreamQuoteState.read`` at ``_quote_ttl``), for one symbol; False
        with ``runtime.stream_quotes`` off. The management snapshot asks it
        before it forces a REST quote. It logs no silent/live transition and
        no health line (the engine's refresh does). A lock timeout replaces
        the books (``_reset_stream_quotes``) and any other failure is logged
        with its type: both answer False, so the caller takes REST (settled
        M2)."""
        if not self.config.runtime.stream_quotes:
            return False
        key = self._symbol_key(symbol)
        try:
            read = self.stream_quotes.read([key], sessions.now_et(), self._quote_ttl())
        except StreamQuoteLockTimeout as exc:
            self._reset_stream_quotes("the freshness check", exc)
            return False
        except Exception as exc:
            LOG.error("Stream quotes: the freshness check of %s failed (%s: %s); it takes REST", key,
                      type(exc).__name__, exc)
            return False
        return key in read.books

    @staticmethod
    def _normalize_quote(symbol: str, payload: dict | None) -> dict:
        payload = payload or {}
        quote = payload.get("quote") if isinstance(payload.get("quote"), dict) else payload
        reference = payload.get("reference") if isinstance(payload.get("reference"), dict) else {}
        bid = first_float(quote, "bidPrice", "bid", "bidPriceInDouble", default=0.0, finite=True)
        ask = first_float(quote, "askPrice", "ask", "askPriceInDouble", default=0.0, finite=True)
        mark = first_float(quote, "mark", "markPrice", "lastPrice", "closePrice", default=0.0, finite=True)
        last = first_float(quote, "lastPrice", "last", default=mark, finite=True)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (mark or last)
        total_volume = first_float(
            quote,
            "totalVolume",
            "total_volume",
            "regularMarketVolume",
            "tradeVolume",
            "volume",
            "totalVolumeTraded",
            "accumulatedVolume",
            default=0.0,
            finite=True,
        )
        return {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "mark": mark,
            "last": last,
            "close": first_float(quote, "closePrice", finite=True),
            "open": first_float(quote, "openPrice", finite=True),
            "net_change": first_float(quote, "netChange", finite=True),
            "percent_change": first_float(quote, "netPercentChangeInDouble", "percentChange", finite=True),
            "total_volume": total_volume,
            "description": payload.get("description") or reference.get("description"),
            "raw": payload,
        }

    @staticmethod
    def _history_candles_to_frame(candles: list[dict]) -> pd.DataFrame:
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame.from_records(candles)
        timestamps = pd.DatetimeIndex(pd.to_datetime(df["datetime"], unit="ms", utc=True)).tz_convert(EXCHANGE_TZ)
        df = df.set_index(timestamps.floor("1min").rename("timestamp")).drop(columns=["timestamp", "datetime"], errors="ignore")
        return ensure_ohlcv_frame(df)

    def _stream_running(self) -> bool:
        """Whether a stream thread runs, so ``start_streaming`` starts none.

        schwabdev's ``Stream.active`` alone does not say. It turns False when
        a reconnect's backoff ends and True again at the new connection's
        LOGIN response, a window that spans the streamer-info fetch (25 s on
        2026-10-02, 11:12:38-11:13:03), and ``Stream.start`` refuses only
        while ``active``: a start in that window ran a second thread and
        connection over the first one's websocket and event loop (until
        2026-10-06). The thread says: the one this store's last start made,
        schwabdev's private ``Stream._thread`` read right after
        ``Stream.start`` (pinned against schwabdev 4.0.0 by
        tests/market_data/test_stream_lifecycle.py). The store keeps its own
        reference because ``Stream.stop`` drops schwabdev's after a 5 s join,
        while a thread sleeping out a reconnect backoff lives on and would
        wake into the next start's loop."""
        if self.stream.active:
            return True
        thread = self._stream_thread
        return thread is not None and thread.is_alive()

    def _stream_send_due(self, now: datetime) -> bool:
        """Whether a stream subscription send may go out (every send to the
        stream shares this): always, unless the last one failed; then not
        for ``STREAM_SEND_RETRY_SECONDS`` after that failure, nor while the
        stream is not active. A send fails while it is not: schwabdev builds
        each request from its streamer info, which a failed reconnect leaves
        None, and ``basic_request`` then fetches it on the calling thread (a
        blocking REST call, ahead of management) and raises
        ``ConnectionError("Streamer info unavailable")`` when that fails;
        schwabdev's own reconnect fetches it on the stream's thread."""
        failed_at = self._stream_send_failed_at
        if failed_at is None:
            return True
        return bool(self.stream.active) and (now - failed_at).total_seconds() >= STREAM_SEND_RETRY_SECONDS

    def _stream_send_failed(self, what: str, exc: Exception, now: datetime) -> None:
        """A failed stream send: the next waits (``_stream_send_due``); a
        WARNING with the error's type for the first failure of a run, DEBUG
        for the rest."""
        self._stream_send_failed_at = now
        self._stream_send_failures += 1
        if self._stream_send_failures == 1:
            LOG.warning("Schwab stream send failed (%s: %s): %s; no stream send is tried for %.0f s, nor while the "
                        "stream is not active", type(exc).__name__, exc, what, STREAM_SEND_RETRY_SECONDS)
        else:
            LOG.debug("Schwab stream send failed again (%s: %s): %s; %d failures in a row", type(exc).__name__, exc,
                      what, self._stream_send_failures)

    def _stream_send_succeeded(self, what: str) -> None:
        """A stream send went out: it ends a run of failures."""
        if self._stream_send_failures:
            LOG.info("Schwab stream send succeeded after %d failures: %s", self._stream_send_failures, what)
        self._stream_send_failed_at = None
        self._stream_send_failures = 0

    def start_streaming(self, symbols: Iterable[str], *, stream_quote_symbols: Iterable[str]) -> None:
        """Start the stream when none runs (``_stream_running``), then bring
        its CHART_EQUITY subscription (the 1m bars) to the streamable equities
        of ``symbols`` and, with ``runtime.stream_quotes``, its
        LEVELONE_EQUITIES subscription (the quote books,
        ``_subscribe_stream_quotes``) to those of ``stream_quote_symbols``.

        With no streamable equity in ``symbols`` it returns at once: nothing
        starts and neither subscription changes, LEVELONE_EQUITIES included
        (each keeps its symbols). The engine streams only with a watchlist
        and stops the stream without one."""
        # Lock only wraps state mutations — network I/O (stream.start/send)
        # is kept outside so the schwabdev callback thread (which reads this
        # same state inside self._lock) isn't blocked waiting for Schwab.
        symbols = sorted({self._symbol_key(s) for s in set(symbols) if is_streamable_equity(s)})
        if not symbols:
            return
        if not self._stream_running():
            with self._lock:
                self.stream_start_requested_at = sessions.now_et()
                self._stream_seen_symbols.clear()
                self._stream_first_bar_time.clear()
            # Each service's key exists before the stream's thread replays the
            # recorded subscriptions at its LOGIN response (schwabdev 4.0.0
            # ``stream.py:95-105`` iterates ``subscriptions`` across awaits):
            # a send that recorded a new service there, the first
            # LEVELONE_EQUITIES one behind a slow pass, changed the dict's size
            # mid-iteration, and schwabdev reconnected. An empty service sends
            # nothing (``stream.py:102``).
            self.stream.subscriptions.setdefault("CHART_EQUITY", {})
            if self.config.runtime.stream_quotes:
                self.stream.subscriptions.setdefault(LEVELONE_EQUITIES, {})
            LOG.info("Starting Schwab stream for symbols: %s", symbols)
            self.stream.start(receiver=self.on_stream_message)
            self._stream_thread = self.stream._thread
        elif not self.stream.active:
            LOG.debug("Schwab stream not active while its thread runs (reconnecting, or ending after a stop): no "
                      "second stream started")
        wanted = set(symbols)
        with self._lock:
            current = set(self.stream_symbols)
            self._stream_seen_symbols.intersection_update(wanted)
            for stale_symbol in sorted(current - wanted):
                self.last_stream_update.pop(stale_symbol, None)
                self.last_stream_bar_time.pop(stale_symbol, None)
                self._stream_first_bar_time.pop(stale_symbol, None)
        add = sorted(wanted - current)
        remove = sorted(current - wanted)
        if add or remove:
            now = sessions.now_et()
            if not self._stream_send_due(now):
                return
            command = "ADD" if current else "SUBS"
            what = "CHART_EQUITY " + ", ".join(f"{name} of {len(keys)}" for name, keys in ((command, add),
                                                                                         ("UNSUBS", remove)) if keys)
            try:
                if add:
                    self.stream.send(self.stream.chart_equity(add, self.config.runtime.stream_fields, command=command))
                if remove:
                    self.stream.send(self.stream.chart_equity(remove, self.config.runtime.stream_fields,
                                                              command="UNSUBS"))
            except Exception as exc:
                # ``stream_symbols`` keeps the subscription as it was, so the
                # same change goes out at the next pass the send is due.
                self._stream_send_failed(what, exc, now)
                return
            self._stream_send_succeeded(what)
        with self._lock:
            # Atomic replacement — Python attribute assignment is atomic, so
            # any lock-free reader (e.g. should_backfill_stream_symbol at
            # line 834) always observes either the old set or the new set,
            # never a mid-update partial. All known callers do fresh
            # `self.stream_symbols` lookups rather than caching the ref.
            self.stream_symbols = set(wanted)
        if self.config.runtime.stream_quotes:
            self._subscribe_stream_quotes(stream_quote_symbols)

    def _subscribe_stream_quotes(self, symbols: Iterable[str]) -> None:
        """Bring the LEVELONE_EQUITIES subscription to the streamable
        equities of ``symbols``, on CHART_EQUITY's rule: SUBS when nothing is
        subscribed (it also replaces whatever Schwab kept), else ADD and
        UNSUBS for the difference, in one send, under the stream sends'
        shared back-off (``_stream_send_due``). The change is committed to the
        books before the send, so the snapshot Schwab sends after a SUBS or
        ADD lands in the symbol's new book and a removed symbol's book is
        dropped at once; a failed send reverts it, so the next pass sends it
        again."""
        wanted = frozenset(self._symbol_key(s) for s in symbols if is_streamable_equity(s))
        try:
            add, remove, was_empty = self.stream_quotes.diff(wanted)
            if not (add or remove):
                return
            now = sessions.now_et()
            if not self._stream_send_due(now):
                return
            self.stream_quotes.apply(add=add, remove=remove)
        except StreamQuoteLockTimeout as exc:
            self._reset_stream_quotes("the subscription change", exc)
            return
        command = "SUBS" if was_empty else "ADD"
        what = LEVELONE_EQUITIES + " " + ", ".join(f"{name} of {len(keys)}" for name, keys in ((command, add),
                                                                                             ("UNSUBS", remove)) if keys)
        try:
            stream_requests = []
            if add:
                stream_requests.append(self.stream.level_one_equities(add, list(STREAM_QUOTE_FIELDS), command=command))
            if remove:
                stream_requests.append(self.stream.level_one_equities(remove, list(STREAM_QUOTE_FIELDS),
                                                                      command="UNSUBS"))
            self.stream.send(stream_requests)
        except Exception as exc:
            try:
                self.stream_quotes.apply(add=remove, remove=add)
            except StreamQuoteLockTimeout as again:
                self._reset_stream_quotes("the subscription change's revert", again)
            self._stream_send_failed(what, exc, now)
            return
        self._stream_send_succeeded(what)
        LOG.debug("Stream quotes: %s sent (added %s, removed %s)", what, add, remove)

    def _reset_stream_quotes(self, what: str, exc: Exception) -> None:
        """The engine thread could not take the stream quote books' lock
        within ``stream_quotes.LOCK_TIMEOUT_SECONDS``: a stuck stream thread
        holds it. Replace the books with an empty state (nothing subscribed,
        no book; the epoch count kept: ``StreamQuoteState.successor``), so no
        later pass waits on that lock: every quote is REST until the next pass
        subscribes afresh (SUBS) and Schwab's snapshot fills the books."""
        self.stream_quotes = self.stream_quotes.successor()
        LOG.error("Stream quotes: %s failed (%s: %s); the books are replaced by empty ones, and the next pass "
                  "subscribes afresh", what, type(exc).__name__, exc)

    def stop_streaming(self) -> None:
        # In schwabdev's reconnect window ``active`` is False while its thread
        # runs (_stream_running). A stop that checked ``active`` alone skipped
        # it there, and the thread reconnected with the recorded subscriptions
        # and its bars kept merging into ``live`` (until 2026-10-06).
        thread = self.stream._thread
        if self.stream.active or (thread is not None and thread.is_alive()):
            LOG.info("Stopping Schwab stream")
            self.stream.stop(clear_subscriptions=True)
            if thread is not None and thread.is_alive():
                LOG.warning("Schwab stream thread still running after the stop (schwabdev waits 5 s for it); no "
                            "stream starts until it ends")
        with self._lock:
            self.stream_symbols.clear()
            self.stream_start_requested_at = None
            self._stream_seen_symbols.clear()
            self._stream_first_bar_time.clear()
        self._stream_quotes_live = None
        # Whatever ran: an item after the stop finds nothing subscribed.
        try:
            self.stream_quotes.clear()
        except StreamQuoteLockTimeout as exc:
            self._reset_stream_quotes("the stop's clear", exc)

    def on_stream_message(self, message: str) -> None:
        """schwabdev's receiver: each stream message, on the stream thread.

        It never raises. schwabdev reads an exception out of its receiver as
        a broken connection (schwabdev 4.0.0 ``stream.py:133-136``): it logs
        "Stream unknown exception", tears the websocket down and reconnects
        after its backoff, so every subscription's data stops until the new
        connection replays them. A malformed item is dropped with a WARNING
        naming the error's type (``_merge_chart_equity``); anything else that
        raises (a defect, not the data) ends the message's handling there,
        with an ERROR naming the type. With ``runtime.stream_quotes`` the
        message goes to the quote books first
        (``stream_quotes.StreamQuoteState.on_message``, which never raises
        and never waits for this store's lock)."""
        try:
            payload = json.loads(message)
        except Exception as exc:
            LOG.debug("Ignoring non-json stream payload (%s: %s): %s", type(exc).__name__, exc,
                      _stream_log_text(message))
            return
        if self.config.runtime.stream_quotes:
            self.stream_quotes.on_message(payload, sessions.now_et())
        try:
            self._merge_chart_equity(payload)
        except Exception as exc:
            LOG.error("Stream message handling failed (%s: %s); the rest of the message is dropped and the stream "
                      "stays connected: %s", type(exc).__name__, exc, _stream_log_text(message))

    def _merge_chart_equity(self, payload: Any) -> None:
        """Merge one stream message's CHART_EQUITY bars into ``live``.

        Each item is parsed on its own: a malformed one (an item that is not
        a JSON object, a chart time that is not epoch milliseconds or is more
        than ``STREAM_BAR_MAX_LEAD_SECONDS`` after the message's receipt) is
        dropped with a WARNING naming the error's type, and the message's
        other items merge as they would without it. A message that is not a
        JSON object, ``data`` that is not a list, and a packet or ``content``
        of the wrong type are dropped the same way. Until 2026-10-06 these
        raised into schwabdev, which reconnected the stream, except the chart
        times past the receipt and the two (+-2**63 ms) that parse to NaT,
        which were merged into the frame. An item without a symbol or chart
        time, or whose symbol is not an equity ticker, is skipped without a
        word, as before."""
        if not isinstance(payload, dict):
            LOG.warning("Ignoring a stream message that is not a JSON object (%s): %s", type(payload).__name__,
                        _stream_log_text(payload))
            return
        data = payload.get("data") or []
        if not data:
            return
        if not isinstance(data, list):
            LOG.warning("Ignoring a stream message whose data is not a list (%s): %s", type(data).__name__,
                        _stream_log_text(payload))
            return
        received_at = sessions.now_et()
        # Parse and merge outside the lock to avoid blocking the main bot loop.
        parsed_updates: list[tuple[str, pd.DataFrame, pd.Timestamp]] = []
        stale_symbols: list[tuple[str, pd.Timestamp]] = []
        for packet in data:
            if not isinstance(packet, dict):
                LOG.warning("Ignoring a stream data packet that is not a JSON object (%s): %s",
                            type(packet).__name__, _stream_log_text(packet))
                continue
            if packet.get("service") != "CHART_EQUITY":
                continue
            content = packet.get("content", [])
            if not isinstance(content, list):
                LOG.warning("Ignoring a CHART_EQUITY packet whose content is not a list (%s): %s",
                            type(content).__name__, _stream_log_text(packet))
                continue
            for item in content:
                try:
                    bar = self._chart_item_to_row(item)
                    if bar is None:
                        continue
                    symbol, ts, row = bar
                    cache_key = self._symbol_key(symbol)
                    lead_seconds = (ts - received_at).total_seconds()
                    if lead_seconds > STREAM_BAR_MAX_LEAD_SECONDS:
                        raise ValueError(f"chart time {ts} is {lead_seconds:.0f} s after its receipt")
                    if not self._is_fresh_stream_bar_timestamp(ts, now=received_at):
                        stale_symbols.append((cache_key, ts))
                        continue
                    new_df = pd.DataFrame([row], index=[ts])
                    parsed_updates.append((cache_key, new_df, pd.Timestamp(ts)))
                except Exception as exc:
                    LOG.warning("Dropped a malformed CHART_EQUITY item (%s: %s): %s", type(exc).__name__, exc,
                                _stream_log_text(item))
        for cache_key, ts in stale_symbols:
            LOG.warning("Ignoring stale CHART_EQUITY candle for %s ts=%s", cache_key, ts)
        if not parsed_updates:
            return
        # Acquire lock only for the cache mutation phase.
        with self._lock:
            for cache_key, new_df, bar_ts in parsed_updates:
                self.live[cache_key] = self._retain_window(
                    self._merge_frames(self.live.get(cache_key), new_df),
                    self._history_window_rows.get(cache_key),
                )
                self.merge_stats[cache_key].stream_rows = len(self.live[cache_key])
                self.last_stream_update[cache_key] = received_at
                self.last_stream_bar_time[cache_key] = bar_ts
                if cache_key not in self._stream_seen_symbols:
                    self._stream_seen_symbols.add(cache_key)
                    self._stream_first_bar_time[cache_key] = bar_ts
                    LOG.info("CHART_EQUITY first candle received: %s", cache_key)
                if self.stream_start_requested_at is not None and self.stream_symbols and self._stream_seen_symbols.issuperset(self.stream_symbols):
                    self.stream_start_requested_at = None
                self._invalidate_cycle_symbol(cache_key)

    @staticmethod
    def _chart_item_to_row(item: dict) -> tuple[str, pd.Timestamp, dict] | None:
        symbol = item.get("key") or item.get("0")
        ts_ms = item.get("7") or item.get("Chart Time")
        if symbol is None or ts_ms is None:
            return None
        # Validate symbol matches expected equity ticker format.
        sym = str(symbol).upper().strip()
        if not STREAMABLE_EQUITY_RE.match(sym):
            return None
        ts = floor_minute(pd.to_datetime(int(ts_ms), unit="ms", utc=True).tz_convert(EXCHANGE_TZ))
        if pd.isna(ts):
            # +-2**63 ms parse to NaT without raising; a NaT row in ``live``
            # sorts last, and every later indicator read of the symbol's
            # frame raises on it.
            raise ValueError(f"chart time {ts_ms!r} is not a timestamp")
        row = {
            "sequence": safe_float(item.get("1", 0.0), 0.0, finite=True),
            "open": safe_float(item.get("2", 0.0), 0.0, finite=True),
            "high": safe_float(item.get("3", 0.0), 0.0, finite=True),
            "low": safe_float(item.get("4", 0.0), 0.0, finite=True),
            "close": safe_float(item.get("5", 0.0), 0.0, finite=True),
            "volume": safe_float(item.get("6", 0.0), 0.0, finite=True),
            "source": "stream",
        }
        return sym, ts, row

    @staticmethod
    def _retain_window(frame: pd.DataFrame, keep_rows: int | None) -> pd.DataFrame:
        """Bound a 1m frame: every bar of the latest session day, plus the
        last ``keep_rows`` bars overall.

        Neither the history merge nor the stream append ever trimmed, and
        ``prune_inactive_symbols`` only evicts symbols that LEFT the active
        set, so an always-on run over a fixed universe (top_tier's 28) grew
        every frame by a day of bars per day -- and ``get_merged`` recomputes
        indicators over the whole frame every cycle. ``keep_rows`` is what
        the deepest price_history fetch returned, i.e. the depth a fresh
        start would have; the latest day is kept whole regardless, because session
        VWAP, the opening range and the day's extremes read all of it.
        No fetch yet (``keep_rows`` None): left alone.
        """
        if keep_rows is None or frame is None or len(frame) <= keep_rows:
            return frame
        index = pd.DatetimeIndex(frame.index)
        cutoff = min(index[-1].normalize(), index[-int(keep_rows)])
        return frame[index >= cutoff]

    @staticmethod
    def _merge_frames(left: pd.DataFrame | None, right: pd.DataFrame | None) -> pd.DataFrame:
        if left is None or left.empty:
            return ensure_ohlcv_frame(right if right is not None else pd.DataFrame())
        if right is None or right.empty:
            # left is already normalized from a previous merge/fetch
            return left
        merged = pd.concat([left, right]).sort_index()
        merged = merged[~merged.index.duplicated(keep="last")]
        # Both inputs were already normalized; only need dedup and column filter.
        ohlcv_cols = [c for c in ("open", "high", "low", "close", "volume") if c in merged.columns]
        extra_cols = [c for c in merged.columns if c not in {"open", "high", "low", "close", "volume"}]
        if len(ohlcv_cols) == 5:
            return merged[ohlcv_cols + extra_cols]
        return ensure_ohlcv_frame(merged)

    def history_warmup_counts(self, symbol: str) -> tuple[bool, bool, int]:
        """``(history known, history has rows, merged 1m bars)``: what
        ``get_history`` and ``get_merged(symbol, with_indicators=False)``
        report for the warm-up decision. The history is read in place, not
        copied; the merged count comes from ``get_merged`` itself, so its
        merge lands in the cycle cache as it did."""
        cache_key = self._symbol_key(symbol)
        with self._lock:
            history_frame = self.history.get(cache_key)
            history_known = history_frame is not None
            history_has_rows = history_known and not history_frame.empty
        merged = self.get_merged(symbol, with_indicators=False)
        return history_known, history_has_rows, len(merged)

    def get_history(self, symbol: str) -> pd.DataFrame | None:
        with self._lock:
            frame = self.history.get(self._symbol_key(symbol))
        return None if frame is None else frame.copy()

    def get_merged(
        self,
        symbol: str,
        timeframe: str | None = None,
        with_indicators: bool = True,
        span_scale: float = 1.0,
        ema_spans: tuple[int, int] | None = None,
    ) -> pd.DataFrame:
        cache_key = self._symbol_key(symbol)
        tf = str(timeframe or "1min")
        # Base (OHLCV-only) cache is span-independent and stays shared. The
        # enriched cache is keyed by span_scale and the resolved EMA spans so a
        # caller asking for stretched indicators or its own EMAs (top_tier's 1m
        # LTF: span_scale 5, ltf_ema_*_span) gets its own entry without
        # clobbering the canonical frame the engine bars, dashboard, and other
        # strategies read. Resolving None keeps an explicit canonical request
        # (9/20 at scale 1) on that same entry.
        base_key = (cache_key, tf, False)
        indicator_key = (cache_key, tf, True, float(span_scale), resolve_ema_spans(span_scale, ema_spans))
        key = indicator_key if with_indicators else base_key
        settings = (get_runtime_indicator_mode(), get_session_indicator_window())
        # Every frame handed out is a shallow copy registered with its
        # version: the source token of the store frames it was built from and
        # the variant it is (this key's request and the indicator settings).
        # Copy-on-write (always on in pandas 3) keeps a caller's writes out of
        # the cached frame, and the version travels with a cycle-cache entry,
        # so a frame built before a stream bar landed never carries the token
        # of the store after it.
        with self._lock:
            if self._cycle_active:
                cached = self._cycle_merged_cache.get(key)
                if cached is not None:
                    return self._hand_out(cached[0], cached[1])
            history_frame = self.history.get(cache_key)
            live_frame = self.live.get(cache_key)
            source = self._source_generations.get(cache_key)
            if source is None or source[0] is not history_frame or source[1] is not live_frame:
                source = (history_frame, live_frame, next_source_generation())
                self._source_generations[cache_key] = source
            token = (cache_key, source[2], tf)
            memo = self._merged_memo.get(key)
            if memo is not None and memo[0] is history_frame and memo[1] is live_frame and memo[2] == settings:
                version = (token, (key[2:], memo[2]))
                if self._cycle_active:
                    self._cycle_merged_cache[key] = (memo[3], version)
                return self._hand_out(memo[3], version)
            base_memo = self._merged_memo.get(base_key) if with_indicators else None
        base_version = (token, (base_key[2:], settings))
        if base_memo is not None and base_memo[0] is history_frame and base_memo[1] is live_frame:
            merged = base_memo[3]
        else:
            merged = self._merge_frames(history_frame, live_frame)
            if tf != "1min":
                rule = {"5min": "5min", "15min": "15min", "30min": "30min"}.get(tf, tf)
                merged = resample_bars(merged, rule)
            with self._lock:
                self._merged_memo[base_key] = (history_frame, live_frame, settings, merged)
                if self._cycle_active:
                    self._cycle_merged_cache[base_key] = (merged, base_version)
        if not with_indicators:
            return self._hand_out(merged, base_version)
        enriched = ensure_standard_indicator_frame(merged, span_scale=span_scale, ema_spans=ema_spans)
        version = (token, (indicator_key[2:], settings))
        with self._lock:
            self._merged_memo[indicator_key] = (history_frame, live_frame, settings, enriched)
            if self._cycle_active:
                self._cycle_merged_cache[indicator_key] = (enriched, version)
        return self._hand_out(enriched, version)

    @staticmethod
    def _hand_out(frame: pd.DataFrame, version: tuple) -> pd.DataFrame:
        out = frame.copy(deep=False)
        register_frame_source(out, version[0], version[1], frame)
        return out
