# SPDX-License-Identifier: MIT
from __future__ import annotations

import copy
import json
import logging
import re
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from threading import RLock
from typing import Any, Iterable

import pandas as pd
try:
    from schwabdev import Client, Stream
except Exception:  # pragma: no cover - test/import fallback when schwabdev is unavailable
    class Client:  # type: ignore[no-redef]
        pass

    class Stream:  # type: ignore[no-redef]
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.active = False

        def start(self, _receiver: Any | None = None) -> None:
            self.active = True

        def stop(self, _clear_subscriptions: bool = True) -> None:
            self.active = False

        @staticmethod
        def chart_equity(symbols: list[str], fields: Any, command: str = "SUBS") -> dict[str, Any]:
            return {"symbols": list(symbols), "fields": fields, "command": command}

        @staticmethod
        def send(_request: Any) -> None:
            return None

from .config import BotConfig
from .support_resistance import SupportResistanceContext, build_support_resistance_context
from .htf_levels import HTFContext, FairValueGapContext, build_fair_value_gap_context, build_htf_context, empty_fvg_context
from .utils import EQUITY_STREAM_HISTORY_REFRESH_READY, call_schwab_client, ensure_ohlcv_frame, ensure_standard_indicator_frame, floor_minute, get_runtime_timezone_name, is_equity_stream_session, is_regular_equity_session, is_weekday_session_day, now_et, resample_bars

LOG = logging.getLogger(__name__)
STREAMABLE_EQUITY_RE = re.compile(r"^[A-Z]{1,6}$")
NON_STREAMABLE = {"VIX", "$VIX", "$VIX.X", "DXY", "$DXY", "$DXY.X", "NYICDX", "$NYICDX", "$NYICDX.X", "SPX", "$SPX", "$SPX.X", "$COMPX", "COMPX", "NDX", "$NDX", "RUT", "$RUT", "$DJI", "DJI"}
SR_SYMBOL_ALIASES = {
    "VIX": "VIX",
    "$VIX": "VIX",
    "$VIX.X": "VIX",
    "DXY": "NYICDX",
    "$DXY": "NYICDX",
    "$DXY.X": "NYICDX",
    "NYICDX": "NYICDX",
    "$NYICDX": "NYICDX",
    "$NYICDX.X": "NYICDX",
}
MARKET_INTERNAL_SYMBOLS = {
    "TICK", "$TICK", "$TICK.X", "TICKQ", "$TICKQ", "$TICKQ.X",
    "ADD", "$ADD", "$ADD.X", "ADDQ", "$ADDQ", "$ADDQ.X",
    "VOLD", "$VOLD", "$VOLD.X", "VOLDQ", "$VOLDQ", "$VOLDQ.X",
    "TRIN", "$TRIN", "$TRIN.X", "TRINQ", "$TRINQ", "$TRINQ.X",
}
QUOTE_SYMBOL_ALIASES = {
    "VIX": ["$VIX", "$VIX.X", "VIX"],
    "$VIX": ["$VIX", "$VIX.X", "VIX"],
    "$VIX.X": ["$VIX.X", "$VIX", "VIX"],
    "DXY": ["$NYICDX", "NYICDX", "$DXY", "$DXY.X", "DXY"],
    "$DXY": ["$NYICDX", "NYICDX", "$DXY", "$DXY.X", "DXY"],
    "$DXY.X": ["$NYICDX", "NYICDX", "$DXY.X", "$DXY", "DXY"],
    "NYICDX": ["$NYICDX", "NYICDX", "$DXY", "$DXY.X", "DXY"],
    "$NYICDX": ["$NYICDX", "NYICDX", "$DXY", "$DXY.X", "DXY"],
    "$NYICDX.X": ["$NYICDX.X", "$NYICDX", "NYICDX", "$DXY", "$DXY.X", "DXY"],
}


@dataclass(slots=True)
class MergeStats:
    history_rows: int = 0
    stream_rows: int = 0


class MarketDataStore:
    def __init__(self, client: Client, config: BotConfig):
        self.client = client
        self.config = config
        try:
            self.stream = Stream(client)
        except TypeError:
            self.stream = Stream()
        self.history: dict[str, pd.DataFrame] = {}
        self.live: dict[str, pd.DataFrame] = {}
        self.quote_cache: dict[str, dict] = {}
        self.sr_cache: dict[tuple[str, int], SupportResistanceContext] = {}
        self.history_htf: dict[tuple[str, int], pd.DataFrame] = {}
        self.htf_cache: dict[tuple, HTFContext] = {}
        self.last_htf_refresh: dict[tuple[str, int], datetime] = {}
        self.last_quote_refresh: dict[str, datetime] = {}
        self.merge_stats: dict[str, MergeStats] = defaultdict(MergeStats)
        self.last_history_refresh: dict[str, datetime] = {}
        self.last_stream_update: dict[str, datetime] = {}
        self.last_stream_bar_time: dict[str, pd.Timestamp] = {}
        self.last_empty_history_refresh: dict[str, datetime] = {}
        self.stream_symbols: set[str] = set()
        self.stream_start_requested_at: datetime | None = None
        self._stream_seen_symbols: set[str] = set()
        self.last_stream_health_log: dict[str, datetime] = {}
        self._lock = RLock()
        self.started_at = now_et()
        self._forced_premarket_history_refresh_date: dict[str, date] = {}
        self._cycle_active = False
        self._cycle_merged_cache: dict[tuple[str, str, bool], pd.DataFrame] = {}
        self._cycle_htf_context_cache: dict[tuple, HTFContext | None] = {}
        self._cycle_fvg_cache: dict[tuple, FairValueGapContext] = {}
        self._cycle_sr_cache: dict[tuple, SupportResistanceContext | None] = {}

    @staticmethod
    def is_streamable_equity(symbol: str) -> bool:
        sym = str(symbol).upper().strip()
        if sym in NON_STREAMABLE:
            return False
        if sym.startswith("$") or " " in sym or "/" in sym:
            return False
        return bool(STREAMABLE_EQUITY_RE.match(sym))

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
        now = now_et()
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

    def should_refresh_quote(self, symbol: str) -> bool:
        key = self._symbol_key(symbol)
        last = self.last_quote_refresh.get(key)
        if last is None:
            return True
        ttl = max(1.0, float(self.config.runtime.quote_cache_seconds))
        return (now_et() - last).total_seconds() >= ttl

    @staticmethod
    def normalize_context_symbol(symbol: str) -> str:
        sym = str(symbol).upper().strip()
        if not sym:
            return ""
        return SR_SYMBOL_ALIASES.get(sym, sym)

    @classmethod
    def is_market_internal_symbol(cls, symbol: str) -> bool:
        sym = cls.normalize_context_symbol(symbol)
        raw = str(symbol).upper().strip()
        return sym in MARKET_INTERNAL_SYMBOLS or raw in MARKET_INTERNAL_SYMBOLS

    @classmethod
    def is_support_resistance_symbol(cls, symbol: str) -> bool:
        raw = str(symbol).upper().strip()
        if not raw:
            return False
        if " " in raw or "/" in raw:
            return False
        if cls.is_market_internal_symbol(raw):
            return False
        normalized = cls.normalize_context_symbol(raw)
        if raw.startswith("$") and normalized == raw:
            return False
        return True

    @staticmethod
    def _symbol_key(symbol: str) -> str:
        return str(symbol).upper().strip()

    def should_refresh_support_resistance(self, symbol: str, *, timeframe_minutes: int | None = None, refresh_seconds: int | None = None) -> bool:
        cfg = getattr(self.config, "support_resistance", None)
        if cfg is None or not bool(cfg.enabled) or not self.is_support_resistance_symbol(symbol):
            return False
        tf = int(timeframe_minutes or getattr(cfg, "timeframe_minutes", 15) or 15)
        ttl = int(refresh_seconds or getattr(cfg, "refresh_seconds", 600) or 600)
        return self.should_refresh_htf_context(symbol, tf, ttl)


    @staticmethod
    def _htf_key(symbol: str, timeframe_minutes: int) -> tuple[str, int]:
        return str(symbol).upper().strip(), int(timeframe_minutes)

    def begin_cycle(self) -> None:
        with self._lock:
            self._cycle_active = True
            self._cycle_merged_cache.clear()
            self._cycle_htf_context_cache.clear()
            self._cycle_fvg_cache.clear()
            self._cycle_sr_cache.clear()

    def end_cycle(self) -> None:
        with self._lock:
            self._cycle_active = False
            self._cycle_merged_cache.clear()
            self._cycle_htf_context_cache.clear()
            self._cycle_fvg_cache.clear()
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
    def _htf_context_cache_key(
        symbol: str,
        timeframe_minutes: int,
        *,
        use_prior_day_high_low: bool = True,
        use_prior_week_high_low: bool = True,
        include_fair_value_gaps: bool = True,
        fair_value_gap_max_per_side: int = 4,
        fair_value_gap_min_atr_mult: float = 0.05,
        fair_value_gap_min_pct: float = 0.0005,
    ) -> tuple:
        return (
            str(symbol).upper().strip(),
            int(timeframe_minutes),
            bool(use_prior_day_high_low),
            bool(use_prior_week_high_low),
            bool(include_fair_value_gaps),
            int(fair_value_gap_max_per_side),
            round(float(fair_value_gap_min_atr_mult), 6),
            round(float(fair_value_gap_min_pct), 6),
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

    def should_refresh_htf_context(self, symbol: str, timeframe_minutes: int, refresh_seconds: int | None = None) -> bool:
        key = self._htf_key(symbol, timeframe_minutes)
        last = self.last_htf_refresh.get(key)
        if last is None:
            return True
        cfg = getattr(self.config, "support_resistance", None)
        ttl = int(refresh_seconds or getattr(cfg, "refresh_seconds", 180) or 180)
        return (now_et() - last).total_seconds() >= max(30, ttl)

    @staticmethod
    def _ohlcv_columns(frame: pd.DataFrame | None) -> pd.DataFrame:
        if frame is None or getattr(frame, "empty", True):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        cols = [col for col in ("open", "high", "low", "close", "volume") if col in frame.columns]
        if len(cols) < 5:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        out = frame.loc[:, ["open", "high", "low", "close", "volume"]].copy()
        time_label = getattr(frame, "attrs", {}).get("time_label")
        if time_label is not None:
            out.attrs["time_label"] = time_label
        return out

    @staticmethod
    def _merge_htf_frames(existing: pd.DataFrame | None, incoming: pd.DataFrame | None) -> pd.DataFrame:
        base = MarketDataStore._ohlcv_columns(existing)
        update = MarketDataStore._ohlcv_columns(incoming)
        if base.empty:
            return update
        if update.empty:
            return base
        combined = pd.concat([base, update]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        time_label = getattr(update, "attrs", {}).get("time_label") or getattr(base, "attrs", {}).get("time_label")
        if time_label is not None:
            combined.attrs["time_label"] = time_label
        return combined

    @staticmethod
    def _trim_frame_to_days(frame: pd.DataFrame, end: datetime, lookback_days: int) -> pd.DataFrame:
        if frame is None or frame.empty:
            return frame
        cutoff = end - timedelta(days=max(5, int(lookback_days)))
        try:
            trimmed = frame.loc[frame.index >= cutoff]
        except Exception:
            trimmed = frame
        return trimmed.copy() if trimmed is not None else frame

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
        try:
            last_idx = pd.Timestamp(cached_frame.index.max())
        except Exception:
            return None
        if pd.isna(last_idx):
            return None
        if last_idx.tzinfo is None:
            try:
                last_idx = last_idx.tz_localize(end.tzinfo)
            except Exception:
                return None
        overlap_minutes = max(base_frequency_minutes * 4, 240)
        overlap_days = max(2, int(math.ceil(overlap_minutes / 1440.0)))
        recent_window_days = min(max(lookback_days, 5), max(7, overlap_days))
        min_start = end - timedelta(days=recent_window_days)
        last_dt = last_idx.to_pydatetime()
        if last_dt < min_start:
            return None
        return max(last_dt - timedelta(minutes=overlap_minutes), min_start)

    def fetch_htf_context(
        self,
        symbol: str,
        *,
        timeframe_minutes: int,
        lookback_days: int = 60,
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
        if not self.is_support_resistance_symbol(symbol):
            return None
        tf = max(1, int(timeframe_minutes))
        key = self._htf_key(symbol, tf)
        cache_key = self._htf_context_cache_key(
            symbol,
            tf,
            use_prior_day_high_low=bool(use_prior_day_high_low),
            use_prior_week_high_low=bool(use_prior_week_high_low),
            include_fair_value_gaps=bool(include_fair_value_gaps),
            fair_value_gap_max_per_side=int(fair_value_gap_max_per_side),
            fair_value_gap_min_atr_mult=float(fair_value_gap_min_atr_mult),
            fair_value_gap_min_pct=float(fair_value_gap_min_pct),
        )
        with self._lock:
            cached_frame = self.history_htf.get(key)
        base_freq = self._direct_history_frequency(tf)
        end = now_et()
        start = self._htf_incremental_start(
            cached_frame,
            end=end,
            lookback_days=int(lookback_days),
            base_frequency_minutes=base_freq,
        )
        incremental_refresh = start is not None
        if start is None:
            start = end - timedelta(days=max(5, int(lookback_days)))
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
        candles = payload.get("candles", [])
        df = self._history_candles_to_frame(candles)
        if base_freq != tf and not df.empty:
            df = resample_bars(df, f"{tf}min")
        if incremental_refresh and cached_frame is not None and not cached_frame.empty:
            df = self._merge_htf_frames(cached_frame, df)
        else:
            df = self._ohlcv_columns(df)
        df = self._trim_frame_to_days(df, end, int(lookback_days))
        df = ensure_standard_indicator_frame(df)
        current = None
        merged = self.get_merged(symbol, with_indicators=False)
        if merged is not None and not merged.empty:
            current = float(merged.iloc[-1].close)
        sr_cfg = getattr(self.config, "support_resistance", None)
        ctx = build_htf_context(
            df,
            current_price=current,
            timeframe_minutes=tf,
            pivot_span=int(pivot_span),
            max_levels_per_side=int(max_levels_per_side),
            atr_tolerance_mult=float(atr_tolerance_mult),
            pct_tolerance=float(pct_tolerance),
            same_side_min_gap_atr_mult=float(getattr(sr_cfg, "same_side_min_gap_atr_mult", 0.10) or 0.10),
            same_side_min_gap_pct=float(getattr(sr_cfg, "same_side_min_gap_pct", 0.0015) or 0.0015),
            fallback_reference_max_drift_atr_mult=float(getattr(sr_cfg, "fallback_reference_max_drift_atr_mult", 1.0) or 1.0),
            fallback_reference_max_drift_pct=float(getattr(sr_cfg, "fallback_reference_max_drift_pct", 0.01) or 0.01),
            stop_buffer_atr_mult=float(stop_buffer_atr_mult),
            ema_fast_span=int(ema_fast_span),
            ema_slow_span=int(ema_slow_span),
            flip_confirmation_bars=int(flip_confirmation_bars),
            use_prior_day_high_low=bool(use_prior_day_high_low),
            use_prior_week_high_low=bool(use_prior_week_high_low),
            include_fair_value_gaps=bool(include_fair_value_gaps),
            fair_value_gap_max_per_side=int(fair_value_gap_max_per_side),
            fair_value_gap_min_atr_mult=float(fair_value_gap_min_atr_mult),
            fair_value_gap_min_pct=float(fair_value_gap_min_pct),
        )
        with self._lock:
            self.history_htf[key] = df
            self.htf_cache[cache_key] = ctx
            self.last_htf_refresh[key] = now_et()
            self.sr_cache.pop(key, None)
        self._invalidate_cycle_htf(symbol, tf)
        return ctx

    def prefetch_htf_contexts(
        self,
        symbols: Iterable[str],
        *,
        timeframe_minutes: int,
        lookback_days: int = 60,
        pivot_span: int = 2,
        max_levels_per_side: int = 6,
        atr_tolerance_mult: float = 0.35,
        pct_tolerance: float = 0.0030,
        stop_buffer_atr_mult: float = 0.25,
        ema_fast_span: int = 50,
        ema_slow_span: int = 200,
        refresh_seconds: int | None = None,
        flip_confirmation_bars: int = 1,
        use_prior_day_high_low: bool = True,
        use_prior_week_high_low: bool = True,
        include_fair_value_gaps: bool = True,
        fair_value_gap_max_per_side: int = 4,
        fair_value_gap_min_atr_mult: float = 0.05,
        fair_value_gap_min_pct: float = 0.0005,
    ) -> None:
        requested = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        if not requested:
            return
        stale_symbols = [
            symbol
            for symbol in requested
            if self.should_refresh_htf_context(symbol, int(timeframe_minutes), refresh_seconds)
        ]
        if not stale_symbols:
            return
        LOG.info("Prefetching HTF context timeframe=%sm symbols=%s", int(timeframe_minutes), ",".join(stale_symbols))
        for symbol in stale_symbols:
            try:
                self.fetch_htf_context(
                    symbol,
                    timeframe_minutes=int(timeframe_minutes),
                    lookback_days=int(lookback_days),
                    pivot_span=int(pivot_span),
                    max_levels_per_side=int(max_levels_per_side),
                    atr_tolerance_mult=float(atr_tolerance_mult),
                    pct_tolerance=float(pct_tolerance),
                    stop_buffer_atr_mult=float(stop_buffer_atr_mult),
                    ema_fast_span=int(ema_fast_span),
                    ema_slow_span=int(ema_slow_span),
                    flip_confirmation_bars=int(flip_confirmation_bars),
                    use_prior_day_high_low=bool(use_prior_day_high_low),
                    use_prior_week_high_low=bool(use_prior_week_high_low),
                    include_fair_value_gaps=bool(include_fair_value_gaps),
                    fair_value_gap_max_per_side=int(fair_value_gap_max_per_side),
                    fair_value_gap_min_atr_mult=float(fair_value_gap_min_atr_mult),
                    fair_value_gap_min_pct=float(fair_value_gap_min_pct),
                )
            except Exception as exc:
                LOG.warning("HTF prefetch failed for %s (%sm): %s", symbol, timeframe_minutes, exc)

    def get_htf_context(
        self,
        symbol: str,
        *,
        timeframe_minutes: int,
        lookback_days: int = 60,
        pivot_span: int = 2,
        max_levels_per_side: int = 6,
        atr_tolerance_mult: float = 0.35,
        pct_tolerance: float = 0.0030,
        stop_buffer_atr_mult: float = 0.25,
        ema_fast_span: int = 50,
        ema_slow_span: int = 200,
        refresh_seconds: int | None = None,
        flip_confirmation_bars: int = 1,
        allow_refresh: bool = True,
        use_prior_day_high_low: bool = True,
        use_prior_week_high_low: bool = True,
        include_fair_value_gaps: bool = True,
        fair_value_gap_max_per_side: int = 4,
        fair_value_gap_min_atr_mult: float = 0.05,
        fair_value_gap_min_pct: float = 0.0005,
    ) -> HTFContext | None:
        cache_key = self._htf_context_cache_key(
            symbol,
            timeframe_minutes,
            use_prior_day_high_low=bool(use_prior_day_high_low),
            use_prior_week_high_low=bool(use_prior_week_high_low),
            include_fair_value_gaps=bool(include_fair_value_gaps),
            fair_value_gap_max_per_side=int(fair_value_gap_max_per_side),
            fair_value_gap_min_atr_mult=float(fair_value_gap_min_atr_mult),
            fair_value_gap_min_pct=float(fair_value_gap_min_pct),
        )
        with self._lock:
            if self._cycle_active and cache_key in self._cycle_htf_context_cache:
                return self._cycle_htf_context_cache[cache_key]
            cached = self.htf_cache.get(cache_key)
        if cached is not None and (not allow_refresh or not self.should_refresh_htf_context(symbol, timeframe_minutes, refresh_seconds)):
            with self._lock:
                if self._cycle_active:
                    self._cycle_htf_context_cache[cache_key] = cached
            return cached
        if not allow_refresh:
            with self._lock:
                if self._cycle_active:
                    self._cycle_htf_context_cache[cache_key] = cached
            return cached
        try:
            ctx = self.fetch_htf_context(
                symbol,
                timeframe_minutes=timeframe_minutes,
                lookback_days=lookback_days,
                pivot_span=pivot_span,
                max_levels_per_side=max_levels_per_side,
                atr_tolerance_mult=atr_tolerance_mult,
                pct_tolerance=pct_tolerance,
                stop_buffer_atr_mult=stop_buffer_atr_mult,
                ema_fast_span=ema_fast_span,
                ema_slow_span=ema_slow_span,
                flip_confirmation_bars=flip_confirmation_bars,
                use_prior_day_high_low=bool(use_prior_day_high_low),
                use_prior_week_high_low=bool(use_prior_week_high_low),
                include_fair_value_gaps=bool(include_fair_value_gaps),
                fair_value_gap_max_per_side=int(fair_value_gap_max_per_side),
                fair_value_gap_min_atr_mult=float(fair_value_gap_min_atr_mult),
                fair_value_gap_min_pct=float(fair_value_gap_min_pct),
            )
            with self._lock:
                if self._cycle_active:
                    self._cycle_htf_context_cache[cache_key] = ctx
            return ctx
        except Exception as exc:
            LOG.warning("HTF context refresh failed for %s (%sm): %s", symbol, timeframe_minutes, exc)
            with self._lock:
                if self._cycle_active:
                    self._cycle_htf_context_cache[cache_key] = cached
            return cached


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
                return copy.deepcopy(self._cycle_fvg_cache[cache_key])
        merged = self.get_merged(symbol, with_indicators=True)
        if merged is None or merged.empty:
            ctx = empty_fvg_context(float(current_price or 0.0), timeframe_minutes=timeframe_minutes)
        else:
            close = float(current_price if current_price is not None else merged.iloc[-1].get('close', 0.0) or 0.0)
            ctx = build_fair_value_gap_context(
                merged,
                timeframe_minutes=max(1, int(timeframe_minutes)),
                current_price=close,
                max_per_side=max(0, int(max_per_side or 0)),
                min_gap_atr_mult=float(min_gap_atr_mult),
                min_gap_pct=float(min_gap_pct),
            )
        with self._lock:
            if self._cycle_active:
                self._cycle_fvg_cache[cache_key] = copy.deepcopy(ctx)
        return copy.deepcopy(ctx)

    def get_htf_frame(
        self,
        symbol: str,
        *,
        timeframe_minutes: int,
        lookback_days: int = 60,
        pivot_span: int = 2,
        max_levels_per_side: int = 6,
        atr_tolerance_mult: float = 0.35,
        pct_tolerance: float = 0.0030,
        stop_buffer_atr_mult: float = 0.25,
        ema_fast_span: int = 50,
        ema_slow_span: int = 200,
        refresh_seconds: int | None = None,
        flip_confirmation_bars: int = 1,
        allow_refresh: bool = True,
    ) -> pd.DataFrame | None:
        key = self._htf_key(symbol, timeframe_minutes)
        with self._lock:
            frame = self.history_htf.get(key)
        if frame is not None and not getattr(frame, "empty", True) and (not allow_refresh or not self.should_refresh_htf_context(symbol, timeframe_minutes, refresh_seconds)):
            return frame.copy()
        if not allow_refresh:
            return frame.copy() if frame is not None else None
        self.get_htf_context(
            symbol,
            timeframe_minutes=timeframe_minutes,
            lookback_days=lookback_days,
            pivot_span=pivot_span,
            max_levels_per_side=max_levels_per_side,
            atr_tolerance_mult=atr_tolerance_mult,
            pct_tolerance=pct_tolerance,
            stop_buffer_atr_mult=stop_buffer_atr_mult,
            ema_fast_span=ema_fast_span,
            ema_slow_span=ema_slow_span,
            refresh_seconds=refresh_seconds,
            flip_confirmation_bars=flip_confirmation_bars,
            allow_refresh=allow_refresh,
        )
        with self._lock:
            refreshed = self.history_htf.get(key)
        return refreshed.copy() if refreshed is not None else None

    def _stream_history_due(self, symbol: str) -> bool:
        now = now_et()
        key = self._symbol_key(symbol)
        last = self.last_history_refresh.get(key)
        if last is None:
            return True
        interval = max(10, int(self.config.runtime.stream_fallback_poll_seconds))
        return (now - last).total_seconds() >= interval

    def _stream_log_due(self, symbol: str) -> bool:
        now = now_et()
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
        self.last_stream_health_log[key] = now_et()
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
            try:
                last_idx = frame.index[-1]
            except Exception:
                continue
            candidates.append(pd.Timestamp(last_idx))
        if not candidates:
            return None
        latest = max(candidates)
        if latest.tzinfo is None:
            latest = latest.tz_localize(get_runtime_timezone_name())
        return latest

    def _latest_cached_bar_age_seconds(self, symbol: str, now: datetime | None = None) -> float | None:
        latest = self._latest_cached_bar_timestamp(symbol)
        if latest is None:
            return None
        reference = now if now is not None else now_et()
        latest_dt = latest.to_pydatetime() if hasattr(latest, "to_pydatetime") else latest
        return max(0.0, (reference - latest_dt).total_seconds())

    def _is_fresh_stream_bar_timestamp(self, ts: pd.Timestamp, *, now: datetime | None = None) -> bool:
        reference = now if now is not None else now_et()
        ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        age_seconds = max(0.0, (reference - ts_dt).total_seconds())
        return age_seconds <= float(self._stream_stale_after_seconds())

    def live_entry_bar_status(self, symbol: str, *, now: datetime | None = None) -> dict[str, object]:
        """Return whether a symbol has a fresh live 1m stream bar suitable for entries."""
        key = self._symbol_key(symbol)
        reference = now if now is not None else now_et()
        requires_live_entry_bar = self.is_streamable_equity(key) and self.is_equity_stream_session(reference)
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
                bar_ts = bar_ts.tz_localize(get_runtime_timezone_name())
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
        if not self.is_streamable_equity(symbol):
            return self.should_refresh_history(symbol)
        cache_key = self._symbol_key(symbol)
        if cache_key not in self.stream_symbols:
            return False

        now = now_et()
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
        end = now_et()
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
        candles = payload.get("candles", [])
        df = self._history_candles_to_frame(candles)
        fetched_at = now_et()
        if not df.empty:
            latest_bar = pd.Timestamp(df.index[-1])
            if latest_bar.tzinfo is None:
                latest_bar = latest_bar.tz_localize(get_runtime_timezone_name())
            latest_bar_age_seconds = max(0.0, (fetched_at - latest_bar.to_pydatetime()).total_seconds())
            if self.is_regular_session(fetched_at) and latest_bar_age_seconds >= float(self._stream_stale_after_seconds()):
                self._log_stream_health(symbol, f"price_history latest 1m bar stale for {latest_bar_age_seconds:.0f}s after repair fetch", level=logging.INFO)
        with self._lock:
            self.history[cache_key] = self._merge_frames(self.history.get(cache_key), df)
            self.last_history_refresh[cache_key] = fetched_at
            if df.empty:
                self.last_empty_history_refresh[cache_key] = fetched_at
            else:
                self.last_empty_history_refresh.pop(cache_key, None)
            self.merge_stats[cache_key].history_rows = len(self.history[cache_key])
        self._invalidate_cycle_symbol(symbol)
        if df.empty and not self.is_regular_session(fetched_at):
            LOG.info("price_history returned no candles for %s outside regular session; using slower retry cadence", symbol)
        return self.get_merged(symbol)

    def fetch_support_resistance(
        self,
        symbol: str,
        *,
        timeframe_minutes: int | None = None,
        lookback_days: int | None = None,
        refresh_seconds: int | None = None,
        use_prior_day_high_low: bool | None = None,
        use_prior_week_high_low: bool | None = None,
        allow_refresh: bool = True,
    ) -> SupportResistanceContext | None:
        cfg = getattr(self.config, "support_resistance", None)
        if cfg is None or not bool(cfg.enabled) or not self.is_support_resistance_symbol(symbol):
            return None
        tf = int(timeframe_minutes or getattr(cfg, "timeframe_minutes", 15) or 15)
        frame = self.get_htf_frame(
            symbol,
            timeframe_minutes=tf,
            lookback_days=int(lookback_days or getattr(cfg, "lookback_days", 10) or 10),
            pivot_span=int(getattr(cfg, "pivot_span", 2) or 2),
            max_levels_per_side=int(getattr(cfg, "max_levels_per_side", 3) or 3),
            atr_tolerance_mult=float(getattr(cfg, "atr_tolerance_mult", 0.60) or 0.60),
            pct_tolerance=float(getattr(cfg, "pct_tolerance", 0.0030) or 0.0030),
            stop_buffer_atr_mult=float(getattr(cfg, "stop_buffer_atr_mult", 0.25) or 0.25),
            refresh_seconds=int(refresh_seconds or getattr(cfg, "refresh_seconds", 600) or 600),
            allow_refresh=allow_refresh,
        )
        key = self._htf_key(symbol, tf)
        resolved_use_prior_day_high_low = bool(
            getattr(cfg, "use_prior_day_high_low", True)
            if use_prior_day_high_low is None
            else use_prior_day_high_low
        )
        resolved_use_prior_week_high_low = bool(
            getattr(cfg, "use_prior_week_high_low", True)
            if use_prior_week_high_low is None
            else use_prior_week_high_low
        )
        use_cache = use_prior_day_high_low is None and use_prior_week_high_low is None
        if frame is None or frame.empty:
            if not use_cache:
                return None
            with self._lock:
                return self.sr_cache.get(key)
        current = None
        merged = self.get_merged(symbol, with_indicators=False)
        if merged is not None and not merged.empty:
            current = float(merged.iloc[-1].close)
        ctx = build_support_resistance_context(
            frame,
            current_price=current,
            pivot_span=int(cfg.pivot_span),
            max_levels_per_side=int(cfg.max_levels_per_side),
            atr_tolerance_mult=float(cfg.atr_tolerance_mult),
            pct_tolerance=float(cfg.pct_tolerance),
            same_side_min_gap_atr_mult=float(getattr(cfg, "same_side_min_gap_atr_mult", 0.10) or 0.10),
            same_side_min_gap_pct=float(getattr(cfg, "same_side_min_gap_pct", 0.0015) or 0.0015),
            fallback_reference_max_drift_atr_mult=float(getattr(cfg, "fallback_reference_max_drift_atr_mult", 1.0) or 1.0),
            fallback_reference_max_drift_pct=float(getattr(cfg, "fallback_reference_max_drift_pct", 0.01) or 0.01),
            proximity_atr_mult=float(cfg.proximity_atr_mult),
            breakout_atr_mult=float(cfg.breakout_atr_mult),
            breakout_buffer_pct=float(cfg.breakout_buffer_pct),
            stop_buffer_atr_mult=float(cfg.stop_buffer_atr_mult),
            structure_eq_atr_mult=float(getattr(cfg, "structure_eq_atr_mult", 0.25)),
            structure_event_max_age_bars=int(getattr(cfg, "structure_event_lookback_bars", 6) or 6),
            use_prior_day_high_low=resolved_use_prior_day_high_low,
            use_prior_week_high_low=resolved_use_prior_week_high_low,
            timeframe_minutes=tf,
        )
        if use_cache:
            with self._lock:
                self.sr_cache[key] = ctx
        return ctx


    def get_support_resistance(
        self,
        symbol: str,
        current_price: float | None = None,
        *,
        flip_frame: pd.DataFrame | None = None,
        mode: str = "default",
        timeframe_minutes: int | None = None,
        lookback_days: int | None = None,
        refresh_seconds: int | None = None,
        use_prior_day_high_low: bool | None = None,
        use_prior_week_high_low: bool | None = None,
        allow_refresh: bool = True,
    ) -> SupportResistanceContext | None:
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
            None if lookback_days is None else int(lookback_days),
            None if refresh_seconds is None else int(refresh_seconds),
            resolved_use_prior_day_high_low,
            resolved_use_prior_week_high_low,
        )
        with self._lock:
            if self._cycle_active and cycle_key in self._cycle_sr_cache:
                return self._cycle_sr_cache[cycle_key]
        key = self._htf_key(symbol, tf)
        frame = self.get_htf_frame(
            symbol,
            timeframe_minutes=tf,
            lookback_days=int(lookback_days or getattr(cfg, "lookback_days", 10) or 10),
            pivot_span=int(getattr(cfg, "pivot_span", 2) or 2),
            max_levels_per_side=int(getattr(cfg, "max_levels_per_side", 3) or 3),
            atr_tolerance_mult=float(getattr(cfg, "atr_tolerance_mult", 0.60) or 0.60),
            pct_tolerance=float(getattr(cfg, "pct_tolerance", 0.0030) or 0.0030),
            stop_buffer_atr_mult=float(getattr(cfg, "stop_buffer_atr_mult", 0.25) or 0.25),
            refresh_seconds=int(refresh_seconds or getattr(cfg, "refresh_seconds", 600) or 600),
            allow_refresh=allow_refresh,
        )
        use_cache = use_prior_day_high_low is None and use_prior_week_high_low is None
        with self._lock:
            cached = self.sr_cache.get(key) if use_cache else None
        if frame is None or frame.empty:
            result = cached
            with self._lock:
                if self._cycle_active:
                    self._cycle_sr_cache[cycle_key] = result
            return result
        if use_cache and current_price is None and cached is not None and normalized_mode == "default" and flip_frame is None and not self.should_refresh_support_resistance(symbol, timeframe_minutes=tf, refresh_seconds=refresh_seconds):
            with self._lock:
                if self._cycle_active:
                    self._cycle_sr_cache[cycle_key] = cached
            return cached
        flip_1m = 0
        flip_5m = 0
        if normalized_mode == "dashboard":
            flip_1m = max(0, int(getattr(cfg, "dashboard_flip_confirmation_1m_bars", 1) or 1))
        elif normalized_mode == "trading":
            flip_1m = max(0, int(getattr(cfg, "trading_flip_confirmation_1m_bars", 2) or 2))
            flip_5m = max(0, int(getattr(cfg, "trading_flip_confirmation_5m_bars", 1) or 1))
        ctx = build_support_resistance_context(
            frame,
            current_price=current_price,
            pivot_span=int(cfg.pivot_span),
            max_levels_per_side=int(cfg.max_levels_per_side),
            atr_tolerance_mult=float(cfg.atr_tolerance_mult),
            pct_tolerance=float(cfg.pct_tolerance),
            same_side_min_gap_atr_mult=float(getattr(cfg, "same_side_min_gap_atr_mult", 0.10) or 0.10),
            same_side_min_gap_pct=float(getattr(cfg, "same_side_min_gap_pct", 0.0015) or 0.0015),
            fallback_reference_max_drift_atr_mult=float(getattr(cfg, "fallback_reference_max_drift_atr_mult", 1.0) or 1.0),
            fallback_reference_max_drift_pct=float(getattr(cfg, "fallback_reference_max_drift_pct", 0.01) or 0.01),
            proximity_atr_mult=float(cfg.proximity_atr_mult),
            breakout_atr_mult=float(cfg.breakout_atr_mult),
            breakout_buffer_pct=float(cfg.breakout_buffer_pct),
            stop_buffer_atr_mult=float(cfg.stop_buffer_atr_mult),
            structure_eq_atr_mult=float(getattr(cfg, "structure_eq_atr_mult", 0.25)),
            structure_event_max_age_bars=int(getattr(cfg, "structure_event_lookback_bars", 6) or 6),
            use_prior_day_high_low=resolved_use_prior_day_high_low,
            use_prior_week_high_low=resolved_use_prior_week_high_low,
            flip_frame=flip_frame,
            flip_confirmation_1m_bars=flip_1m,
            flip_confirmation_5m_bars=flip_5m,
            timeframe_minutes=tf,
        )
        if use_cache and normalized_mode == "default" and flip_frame is None:
            with self._lock:
                self.sr_cache[key] = ctx
        with self._lock:
            if self._cycle_active:
                self._cycle_sr_cache[cycle_key] = ctx
        return ctx


    def _quote_batch_chunks(self, symbols: list[str]) -> list[list[str]]:
        batch_size = max(1, int(self.config.runtime.quote_batch_size))
        return [symbols[idx: idx + batch_size] for idx in range(0, len(symbols), batch_size)]

    @staticmethod
    def _quote_aliases(symbol: str) -> list[str]:
        sym = str(symbol).strip()
        aliases = QUOTE_SYMBOL_ALIASES.get(sym.upper(), [sym])
        out: list[str] = []
        for item in aliases:
            token = str(item or "").strip()
            if token and token not in out:
                out.append(token)
        if sym and sym not in out:
            out.append(sym)
        return out

    @classmethod
    def _history_aliases(cls, symbol: str) -> list[str]:
        return cls._quote_aliases(symbol)

    def _fetch_price_history_payload_with_aliases(self, symbol: str, **kwargs) -> tuple[dict, str]:
        last_exc: Exception | None = None
        fallback_payload: dict | None = None
        fallback_symbol = str(symbol)
        saw_success = False
        aliases = self._history_aliases(symbol)
        if len(aliases) > 1:
            LOG.debug("price_history alias attempts for %s: %s", symbol, aliases)
        for request_symbol in aliases:
            try:
                response = call_schwab_client(self.client, "price_history", symbol=request_symbol, **kwargs)
                status = getattr(response, "status_code", 200)
                if status >= 400:
                    body = getattr(response, "text", "")
                    raise RuntimeError(f"status={status} body={body}")
                payload = response.json()
                if not isinstance(payload, dict):
                    payload = {}
                candles = payload.get("candles", [])
                if candles:
                    if request_symbol != str(symbol):
                        LOG.debug("price_history alias success for %s via %s candles=%d", symbol, request_symbol, len(candles))
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
                response = call_schwab_client(self.client, "quote", request_symbol)
                status = getattr(response, "status_code", 200)
                if status >= 400:
                    body = getattr(response, "text", "")
                    raise RuntimeError(f"status={status} body={body}")
                payload = response.json()
                extracted = self._extract_quote_payloads(payload, [request_symbol, symbol])
                quote_payload = extracted.get(symbol) or extracted.get(request_symbol)
                if quote_payload is None and isinstance(payload, dict):
                    quote_payload = payload
                normalized = self._normalize_quote(symbol, quote_payload)
                normalized["source_symbol"] = request_symbol
                if request_symbol != str(symbol):
                    LOG.debug("Quote alias success for %s via %s", symbol, request_symbol)
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
                    response = call_schwab_client(self.client, method_name, arg)
                    status_code = int(getattr(response, "status_code", 200) or 200)
                    if not 200 <= status_code < 300:
                        try:
                            body_preview = str(getattr(response, "text", "") or "")[:240]
                        except Exception:
                            body_preview = ""
                        log_fn = LOG.warning if status_code in {401, 403, 404, 429} or status_code >= 500 else LOG.debug
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
        out: dict[str, dict] = {}
        requested = sorted({self._symbol_key(s) for s in symbols if str(s).strip()})
        pending: list[str] = []
        cached_hits = 0
        batch_hits = 0
        fallback_hits = 0
        failures = 0
        force_cooldown_hits = 0
        for symbol in requested:
            if force:
                cached = self.quote_cache.get(symbol)
                last_refresh = self.last_quote_refresh.get(symbol)
                if cached is not None and last_refresh is not None and min_force_interval_seconds is not None:
                    age = (now_et() - last_refresh).total_seconds()
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

        alias_pending = [symbol for symbol in pending if len(self._quote_aliases(symbol)) > 1]
        batch_pending = [symbol for symbol in pending if symbol not in alias_pending]

        for chunk in self._quote_batch_chunks(batch_pending):
            fetched: dict[str, dict] = {}
            if len(chunk) > 1:
                fetched = self._try_batch_quote_request(chunk)
            fetched_at = now_et()
            for symbol, quote_payload in fetched.items():
                normalized = self._normalize_quote(symbol, quote_payload)
                normalized["fetched_at"] = fetched_at
                with self._lock:
                    self.quote_cache[symbol] = normalized
                    self.last_quote_refresh[symbol] = fetched_at
                out[symbol] = normalized
                batch_hits += 1
            for symbol in chunk:
                if symbol in fetched:
                    continue
                try:
                    normalized = self._fetch_single_quote_with_aliases(symbol)
                    fetched_at = now_et()
                    normalized["fetched_at"] = fetched_at
                    with self._lock:
                        self.quote_cache[symbol] = normalized
                        self.last_quote_refresh[symbol] = fetched_at
                    out[symbol] = normalized
                    fallback_hits += 1
                except Exception as exc:
                    failures += 1
                    LOG.warning("Quote fetch failed for %s: %s", symbol, exc)
                    cached = self.quote_cache.get(symbol)
                    if cached is not None:
                        out[symbol] = cached

        for symbol in alias_pending:
            try:
                normalized = self._fetch_single_quote_with_aliases(symbol)
                fetched_at = now_et()
                normalized["fetched_at"] = fetched_at
                with self._lock:
                    self.quote_cache[symbol] = normalized
                    self.last_quote_refresh[symbol] = fetched_at
                out[symbol] = normalized
                fallback_hits += 1
            except Exception as exc:
                failures += 1
                LOG.warning("Quote fetch failed for %s: %s", symbol, exc)
                cached = self.quote_cache.get(symbol)
                if cached is not None:
                    out[symbol] = cached

        if requested:
            mode = "all_cached" if not pending and not failures and cached_hits == len(requested) else "refresh"
            LOG.info(
                "Quote refresh source=%s mode=%s requested=%d cached=%d pending=%d batch=%d fallback=%d failed=%d force=%s force_cooldown_cached=%d",
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
            )
        return out

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
        try:
            return max(0.0, (now_et() - fetched_at).total_seconds())
        except Exception:
            return None

    def quotes_are_fresh(self, symbols: Iterable[str], max_age_seconds: float) -> bool:
        # Single lock acquire + single now_et() call, plus early exit on first
        # stale symbol. Avoids N deepcopies + N lock acquires from the prior
        # implementation that called quote_age_seconds() per symbol.
        limit = float(max_age_seconds)
        current = now_et()
        with self._lock:
            for symbol in symbols:
                quote = self.quote_cache.get(self._symbol_key(str(symbol)))
                if not quote:
                    return False
                fetched_at = quote.get("fetched_at")
                if fetched_at is None:
                    return False
                try:
                    age = max(0.0, (current - fetched_at).total_seconds())
                except Exception:
                    return False
                if age > limit:
                    return False
        return True

    @staticmethod
    def _normalize_quote(symbol: str, payload: dict | None) -> dict:
        payload = payload or {}
        quote = payload.get("quote") if isinstance(payload.get("quote"), dict) else payload
        reference = payload.get("reference") if isinstance(payload.get("reference"), dict) else {}

        def _first_float(*values) -> float:
            for value in values:
                if value in (None, ""):
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
            return 0.0

        def _first_optional_float(*values) -> float | None:
            for value in values:
                if value in (None, ""):
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
            return None

        bid = _first_float(quote.get("bidPrice"), quote.get("bid"), quote.get("bidPriceInDouble"))
        ask = _first_float(quote.get("askPrice"), quote.get("ask"), quote.get("askPriceInDouble"))
        mark = _first_float(quote.get("mark"), quote.get("markPrice"), quote.get("lastPrice"), quote.get("closePrice"))
        last = _first_float(quote.get("lastPrice"), quote.get("last"), mark)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (mark or last)
        total_volume = _first_float(
            quote.get("totalVolume"),
            quote.get("total_volume"),
            quote.get("regularMarketVolume"),
            quote.get("tradeVolume"),
            quote.get("volume"),
            quote.get("totalVolumeTraded"),
            quote.get("accumulatedVolume"),
        )
        return {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "mark": mark,
            "last": last,
            "close": _first_optional_float(quote.get("closePrice")),
            "open": _first_optional_float(quote.get("openPrice")),
            "net_change": _first_optional_float(quote.get("netChange")),
            "percent_change": _first_optional_float(quote.get("netPercentChangeInDouble"), quote.get("percentChange")),
            "total_volume": total_volume,
            "description": payload.get("description") or reference.get("description"),
            "raw": payload,
        }

    @staticmethod
    def _history_candles_to_frame(candles: list[dict]) -> pd.DataFrame:
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame.from_records(candles)
        timestamps = pd.DatetimeIndex(pd.to_datetime(df["datetime"], unit="ms", utc=True)).tz_convert(get_runtime_timezone_name())
        df["timestamp"] = timestamps
        df = df.rename(columns={"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"})
        df = df.set_index(df["timestamp"].map(floor_minute)).drop(columns=["timestamp", "datetime"], errors="ignore")
        out = ensure_ohlcv_frame(df)
        out.attrs["time_label"] = "unknown"
        return out

    def start_streaming(self, symbols: Iterable[str]) -> None:
        # Lock only wraps state mutations — network I/O (stream.start/send)
        # is kept outside so the schwabdev callback thread (which reads this
        # same state inside self._lock) isn't blocked waiting for Schwab.
        symbols = sorted({self._symbol_key(s) for s in set(symbols) if self.is_streamable_equity(s)})
        if not symbols:
            return
        if not self.stream.active:
            with self._lock:
                self.stream_start_requested_at = now_et()
                self._stream_seen_symbols.clear()
            LOG.info("Starting Schwab stream for symbols: %s", symbols)
            self.stream.start(receiver=self.on_stream_message)
        wanted = set(symbols)
        with self._lock:
            current = set(self.stream_symbols)
            self._stream_seen_symbols.intersection_update(wanted)
            for stale_symbol in sorted(current - wanted):
                self.last_stream_update.pop(stale_symbol, None)
                self.last_stream_bar_time.pop(stale_symbol, None)
        add = sorted(wanted - current)
        remove = sorted(current - wanted)
        if add:
            req = self.stream.chart_equity(add, self.config.runtime.stream_fields, command="ADD" if current else "SUBS")
            self.stream.send(req)
        if remove:
            req = self.stream.chart_equity(remove, self.config.runtime.stream_fields, command="UNSUBS")
            self.stream.send(req)
        with self._lock:
            # Atomic replacement — Python attribute assignment is atomic, so
            # any lock-free reader (e.g. should_backfill_stream_symbol at
            # line 834) always observes either the old set or the new set,
            # never a mid-update partial. All known callers do fresh
            # `self.stream_symbols` lookups rather than caching the ref.
            self.stream_symbols = set(wanted)

    def stop_streaming(self) -> None:
        if self.stream.active:
            LOG.info("Stopping Schwab stream")
            self.stream.stop(clear_subscriptions=True)
        with self._lock:
            self.stream_symbols.clear()
            self.stream_start_requested_at = None
            self._stream_seen_symbols.clear()

    def on_stream_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except Exception:
            LOG.debug("Ignoring non-json stream payload: %s", message)
            return
        data = payload.get("data") or []
        if not data:
            return
        received_at = now_et()
        # Parse and merge outside the lock to avoid blocking the main bot loop.
        parsed_updates: list[tuple[str, pd.DataFrame, pd.Timestamp]] = []
        stale_symbols: list[tuple[str, pd.Timestamp]] = []
        for packet in data:
            if packet.get("service") != "CHART_EQUITY":
                continue
            for item in packet.get("content", []):
                bar = self._chart_item_to_row(item)
                if bar is None:
                    continue
                symbol, ts, row = bar
                cache_key = self._symbol_key(symbol)
                if not self._is_fresh_stream_bar_timestamp(ts, now=received_at):
                    stale_symbols.append((cache_key, ts))
                    continue
                new_df = pd.DataFrame([row], index=[ts])
                parsed_updates.append((cache_key, new_df, pd.Timestamp(ts)))
        for cache_key, ts in stale_symbols:
            LOG.warning("Ignoring stale CHART_EQUITY candle for %s ts=%s", cache_key, ts)
        if not parsed_updates:
            return
        # Acquire lock only for the cache mutation phase.
        with self._lock:
            for cache_key, new_df, bar_ts in parsed_updates:
                self.live[cache_key] = self._merge_frames(self.live.get(cache_key), new_df)
                self.merge_stats[cache_key].stream_rows = len(self.live[cache_key])
                self.last_stream_update[cache_key] = received_at
                self.last_stream_bar_time[cache_key] = bar_ts
                if cache_key not in self._stream_seen_symbols:
                    self._stream_seen_symbols.add(cache_key)
                    LOG.info("CHART_EQUITY first candle received: %s", cache_key)
                if self.stream_start_requested_at is not None and self.stream_symbols and self._stream_seen_symbols.issuperset(self.stream_symbols):
                    self.stream_start_requested_at = None
                self._invalidate_cycle_symbol(cache_key)

    @staticmethod
    def _safe_stream_float(value: Any, default: float = 0.0) -> float:
        """Convert a stream field to float, rejecting NaN/Infinity."""
        try:
            f = float(value)
            return f if math.isfinite(f) else default
        except (TypeError, ValueError):
            return default

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
        ts = floor_minute(pd.to_datetime(int(ts_ms), unit="ms", utc=True).tz_convert(get_runtime_timezone_name()))
        _sf = MarketDataStore._safe_stream_float
        row = {
            "sequence": _sf(item.get("1", 0.0)),
            "open": _sf(item.get("2", 0.0)),
            "high": _sf(item.get("3", 0.0)),
            "low": _sf(item.get("4", 0.0)),
            "close": _sf(item.get("5", 0.0)),
            "volume": _sf(item.get("6", 0.0)),
            "source": "stream",
        }
        return sym, ts, row

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

    def get_history(self, symbol: str) -> pd.DataFrame | None:
        with self._lock:
            frame = self.history.get(self._symbol_key(symbol))
        return None if frame is None else frame.copy()

    def get_merged(self, symbol: str, timeframe: str | None = None, with_indicators: bool = True) -> pd.DataFrame:
        cache_key = self._symbol_key(symbol)
        tf = str(timeframe or "1min")
        base_key = (cache_key, tf, False)
        indicator_key = (cache_key, tf, True)
        with self._lock:
            if self._cycle_active:
                cached = self._cycle_merged_cache.get(indicator_key if with_indicators else base_key)
                if cached is not None:
                    return cached.copy()
            history_frame = self.history.get(cache_key)
            live_frame = self.live.get(cache_key)
        merged = self._merge_frames(history_frame, live_frame)
        if tf != "1min":
            rule = {"5min": "5min", "15min": "15min", "30min": "30min"}.get(tf, tf)
            merged = resample_bars(merged, rule)
        with self._lock:
            if self._cycle_active:
                self._cycle_merged_cache[base_key] = merged.copy()
        if not with_indicators:
            return merged.copy()
        enriched = ensure_standard_indicator_frame(merged)
        with self._lock:
            if self._cycle_active:
                self._cycle_merged_cache[indicator_key] = enriched.copy()
        return enriched.copy()
