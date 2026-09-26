# SPDX-License-Identifier: MIT
"""Dashboard cache state container.

Extracted from ``IntradayBot`` as the first step of the Phase 5 engine split.
Owns the four pieces of dashboard-related state that used to live as
``self._dashboard_snapshot_cache`` / ``_dashboard_chart_cache`` /
``_dashboard_cache_lock`` / ``_dashboard_error_log_times`` on the bot:

  - ``snapshot_cache``: per-symbol dashboard snapshot payloads, keyed by
    upper-cased symbol. Values are ``{"signature": tuple, "payload": dict}``
    entries that callers compare against a freshly-computed signature to
    decide whether to return the cached payload or recompute.
  - ``chart_cache``: per-(symbol, timeframe_mode, max_bars) chart payloads,
    same signature-keyed shape.
  - ``lock``: single ``RLock`` guarding both caches. Held briefly around
    get/set operations so concurrent dashboard polls don't corrupt state.
  - ``log_component_failure``: rate-limited (60s) component-error logger
    used across dashboard payload builders. Emits WARNING once per minute
    per component; DEBUG otherwise.

Future Phase 5 steps will grow this into a full ``DashboardPublisher`` that
absorbs the payload-building methods too. This first step just relocates
the state so subsequent extractions have a settled home.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from threading import RLock
from typing import TYPE_CHECKING, Any

import pandas as pd

import copy

from .candles import detect_candle_context, detect_per_bar_candle_patterns
from .chart_patterns import analyze_chart_pattern_context
from .config import DashboardChartConfig, DashboardChartingConfig, flip_confirmation_bars, htf_structure_event_lookback
from .htf_levels import summarize_htf_trend
from .models import Side
from .numeric import safe_float
from .support_resistance import analyze_market_structure, zone_flip_confirmed
from .technical_levels import build_technical_levels_context
from .bars import equity_stream_window_bars, resample_bars, session_bucket_ends
from .indicators import ensure_standard_indicator_frame, htf_ema_spans, ltf_ema_spans
from . import sessions
from ._sr_ladder import _collapse_price_ladder, _sr_effective_side_tolerance

if TYPE_CHECKING:
    from .config import BotConfig

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


# ---------------------------------------------------------------------------
# Pure static dashboard helpers (Phase 5 Step 2 extraction).
# Previously @staticmethod on IntradayBot; moved here as module-level
# functions so payload-builder code can be relocated without dragging the
# full engine surface along.
# ---------------------------------------------------------------------------

_EXCHANGE_ALIASES = {
    "NASDAQ": "NASDAQ",
    "NSDQ": "NASDAQ",
    "NASD": "NASDAQ",
    "NASDAQ GLOBAL MARKET": "NASDAQ",
    "NASDAQ GLOBAL SELECT": "NASDAQ",
    "NASDAQ CAPITAL MARKET": "NASDAQ",
    "NMS": "NASDAQ",
    "NGM": "NASDAQ",
    "NCM": "NASDAQ",
    "NGS": "NASDAQ",
    "NYSE": "NYSE",
    "NEW YORK STOCK EXCHANGE": "NYSE",
    "NYSE AMERICAN": "AMEX",
    "NYSE MKT": "AMEX",
    "AMEX": "AMEX",
    "NYSE ARCA": "AMEX",
    "ARCA": "AMEX",
    "BATS": "BATS",
    "CBOE BZX": "BATS",
    "BZX": "BATS",
    "IEX": "IEX",
}


def dashboard_normalize_exchange(value: Any) -> str | None:
    token = str(value or "").upper().strip()
    if not token:
        return None
    normalized = " ".join(token.replace("-", " ").replace("/", " ").split())
    return _EXCHANGE_ALIASES.get(normalized, _EXCHANGE_ALIASES.get(token, token or None))


def dashboard_quote_exchange(quote: Mapping[str, Any] | None) -> str | None:
    if not isinstance(quote, Mapping):
        return None
    raw_payload = quote.get("raw") if isinstance(quote.get("raw"), dict) else {}
    raw_quote = raw_payload.get("quote") if isinstance(raw_payload.get("quote"), dict) else raw_payload
    raw_reference = raw_payload.get("reference") if isinstance(raw_payload.get("reference"), dict) else {}
    # Prefer the full exchange NAME fields ("NASDAQ" / "NYSE" / "NYSE Arca")
    # over Schwab's single-letter ``exchange`` code ("q" / "n" / "a" / "p").
    # The single letters aren't valid TradingView exchanges and aren't in
    # _EXCHANGE_ALIASES, so they pass through raw and build broken deep-links
    # (symbols/N-XOM/ -> 404). The full names map cleanly via _EXCHANGE_ALIASES,
    # so they must win when present; the short codes stay only as a last resort.
    candidates = [
        raw_quote.get("exchangeName"),
        raw_quote.get("primaryExchangeName"),
        raw_reference.get("exchangeName"),
        raw_reference.get("primaryExchangeName"),
        raw_reference.get("listingExchange"),
        quote.get("exchange"),
        raw_quote.get("exchange"),
        raw_quote.get("primaryExchange"),
        raw_reference.get("exchange"),
        raw_reference.get("primaryExchange"),
    ]
    for value in candidates:
        normalized = dashboard_normalize_exchange(value)
        if normalized:
            return normalized
    return None


def dashboard_technical_line_payload(line: Any) -> dict[str, Any] | None:
    """A trendline / channel edge for the chart. Its positions (start_pos,
    end_pos, and the intercept at position 0) are in the coordinate space of
    the frame handed to ``build_technical_levels_context`` -- the dashboard
    hands it the chart's own frame, so they are the chart bars' abs_index
    and ``slope * abs_index + intercept`` at the newest bar is
    ``current_value``. Until 2026-09-23 they were positions in the builder's
    internal 120-280 bar tail, and every line drew as a zero-length stub at
    the chart's left edge."""
    if line is None:
        return None
    try:
        return {
            "kind": str(getattr(line, "kind", "line") or "line"),
            "slope": float(getattr(line, "slope", 0.0) or 0.0),
            "intercept": float(getattr(line, "intercept", 0.0) or 0.0),
            "touches": int(getattr(line, "touches", 0) or 0),
            "start_pos": int(getattr(line, "start_pos", 0) or 0),
            "end_pos": int(getattr(line, "end_pos", 0) or 0),
            "current_value": float(getattr(line, "current_value", 0.0) or 0.0),
            "direction": str(getattr(line, "direction", "neutral") or "neutral"),
        }
    except Exception:
        return None


def dashboard_fvg_payload(gap: Any) -> dict[str, Any] | None:
    if gap is None:
        return None

    def _ts(value: Any) -> str | None:
        if value is None:
            return None
        try:
            iso = getattr(value, "isoformat", None)
            if callable(iso):
                return str(iso())
        except Exception:
            LOG.debug("Failed to serialize value via isoformat in dashboard payload; falling back to string.", exc_info=True)
        try:
            return str(value)
        except Exception:
            return None

    try:
        lower = float(getattr(gap, "lower", 0.0) or 0.0)
        upper = float(getattr(gap, "upper", 0.0) or 0.0)
        midpoint = float(getattr(gap, "midpoint", (lower + upper) / 2.0) or ((lower + upper) / 2.0))
        if upper <= lower or lower <= 0:
            return None
        return {
            "direction": str(getattr(gap, "direction", "neutral") or "neutral"),
            "lower": lower,
            "upper": upper,
            "midpoint": midpoint,
            "size": float(getattr(gap, "size", upper - lower) or (upper - lower)),
            "filled_pct": float(getattr(gap, "filled_pct", 0.0) or 0.0),
            "first_seen": _ts(getattr(gap, "first_seen", None)),
            "last_seen": _ts(getattr(gap, "last_seen", None)),
        }
    except Exception:
        return None


def dashboard_fvg_anchor_abs_index(frame: pd.DataFrame | None, first_seen: Any) -> int | None:
    if frame is None or getattr(frame, "empty", True) or first_seen in (None, ""):
        return None
    try:
        index = getattr(frame, "index", None)
        if not isinstance(index, pd.DatetimeIndex) or index.empty:
            return None
        anchor_ts = pd.Timestamp(first_seen)
        if getattr(anchor_ts, "tzinfo", None) is not None:
            anchor_ts = anchor_ts.tz_convert(None)
        index_for_search = index.tz_convert(None) if getattr(index, "tz", None) is not None else index
        pos = int(index_for_search.searchsorted(anchor_ts, side="left"))
        if pos < 0 or pos >= len(index_for_search):
            return None
        return pos
    except Exception:
        return None


def dashboard_cache_json_signature(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return repr(value)


def dashboard_frame_signature(frame: pd.DataFrame | None) -> tuple[Any, ...]:
    if frame is None or getattr(frame, "empty", True):
        return 0, None, None, None, None, None, None
    try:
        index = getattr(frame, "index", None)
        first_idx = index[0] if index is not None and len(index) else None
        last_idx = index[-1] if index is not None and len(index) else None
        last_row = frame.iloc[-1]

        def _ts(value: Any) -> str | None:
            if value is None:
                return None
            try:
                return pd.Timestamp(value).isoformat()
            except Exception:
                return str(value)

        return (
            int(len(frame)),
            _ts(first_idx),
            _ts(last_idx),
            safe_float(last_row.get("close")) if hasattr(last_row, "get") else None,
            safe_float(last_row.get("high")) if hasattr(last_row, "get") else None,
            safe_float(last_row.get("low")) if hasattr(last_row, "get") else None,
            safe_float(last_row.get("volume")) if hasattr(last_row, "get") else None,
        )
    except Exception:
        return int(len(frame)), None, None, None, None, None, None


def dashboard_recent_trade_markers(account: Any, symbol: str) -> list[dict[str, Any]]:
    """Return up to 12 dashboard-shaped trade rows for ``symbol`` from
    today's ``account.trades`` (filtered by current ET session date).

    Two correctness fixes vs. the original Phase-5 extraction:
    1. The symbol filter runs BEFORE the slice. The trades deque is
       LIFO-ordered (newest at index 0); pre-slicing to [:12] would make
       a fresh fill on a long-quiet symbol invisible if 12 other tickers
       traded after it.
    2. Multi-day filter: ``account.trades`` is a multi-day deque
       (maxlen=200). A naked iteration leaks yesterday's exits onto
       today's chart. We restrict to trades whose ``exit_time`` falls
       on the current ET trading date (or, for still-open positions
       that emit a marker, ``entry_time``).
    """
    out: list[dict[str, Any]] = []
    key = str(symbol or "").upper().strip()
    if not key:
        return out
    today = sessions.now_et().date()
    for trade in list(getattr(account, "trades", [])):
        if str(getattr(trade, "symbol", "") or "").upper().strip() != key:
            continue
        # Today-filter: a trade belongs to today's chart if either side
        # of the round-trip happened today. Exit-time wins when present;
        # fall back to entry_time so paper-account entries that haven't
        # exited yet still surface.
        exit_time = getattr(trade, "exit_time", None)
        entry_time = getattr(trade, "entry_time", None)
        ref_time = exit_time if exit_time is not None else entry_time
        try:
            if ref_time is None or ref_time.date() != today:
                continue
        except Exception:
            continue
        try:
            out.append({
                "symbol": key,
                "side": str(getattr(trade, "side", "") or ""),
                "qty": int(getattr(trade, "qty", 0) or 0),
                "entry_price": safe_float(getattr(trade, "entry_price", None)),
                "exit_price": safe_float(getattr(trade, "exit_price", None)),
                "entry_time": entry_time.isoformat() if entry_time is not None else None,
                "exit_time": exit_time.isoformat() if exit_time is not None else None,
                "realized_pnl": safe_float(getattr(trade, "realized_pnl", None)),
                "return_pct": safe_float(getattr(trade, "return_pct", None)),
                "reason": str(getattr(trade, "reason", "") or ""),
            })
        except Exception:
            continue
        if len(out) >= 12:
            break
    return out


def dashboard_symbol_trade_signature(account: Any, symbol: str) -> tuple[Any, ...]:
    """Build a cache-key signature capturing the last trade state for
    ``symbol`` on ``account``.

    Same correctness fix as ``dashboard_recent_trade_markers``: filter
    by symbol BEFORE slicing. Without this, a fresh fill on a
    long-quiet symbol won't change the signature when 24 other tickers
    have traded after it, so the cached snapshot stays stale.

    The signature is intentionally NOT date-filtered — cache invalidation
    must catch any newly-recorded trade for the symbol regardless of
    session date, even if the chart payload itself filters to today.
    """
    key = str(symbol or "").upper().strip()
    if not key:
        return 0, None, None, None
    count = 0
    latest_exit: str | None = None
    latest_entry: str | None = None
    latest_reason: str | None = None
    matched = 0
    for trade in list(getattr(account, "trades", [])):
        if str(getattr(trade, "symbol", "") or "").upper().strip() != key:
            continue
        count += 1
        if latest_exit is None:
            exit_time = getattr(trade, "exit_time", None)
            entry_time = getattr(trade, "entry_time", None)
            latest_exit = exit_time.isoformat() if exit_time is not None else None
            latest_entry = entry_time.isoformat() if entry_time is not None else None
            latest_reason = str(getattr(trade, "reason", "") or "")
        matched += 1
        if matched >= 24:
            break
    return count, latest_exit, latest_entry, latest_reason


def dashboard_bars_from_frame(
    frame: pd.DataFrame | None,
    *,
    max_bars: int = 90,
    per_bar_candles: dict[Any, dict[str, list[str]]] | None = None,
) -> list[dict[str, Any]]:
    """Convert the last ``max_bars`` OHLCV rows of ``frame`` into a list of
    JSON-serializable dicts for the dashboard chart payload. Returns [] for
    None / empty frames.

    Carries per-bar indicator values so the dashboard tooltip can honestly
    display the hovered bar's state (instead of silently falling back to a
    global latest-snapshot value). Per-bar fields:
      * adx / plus_di / minus_di / dmi_bias (derived from DI lines)
      * obv / obv_ema / obv_bias (derived from OBV vs OBV-EMA)
      * candles_bullish / candles_bearish (from ``per_bar_candles`` map,
        completion-bar only, with tier cascade applied)
    """
    capped_bars = max(1, min(int(max_bars or 90), 480))
    bars: list[dict[str, Any]] = []
    if frame is None or frame.empty:
        return bars
    tail = frame.tail(capped_bars).copy()
    tail_offset = max(0, len(frame) - len(tail))
    per_bar_candles = per_bar_candles or {}
    for rel_idx, (idx, row) in enumerate(tail.iterrows()):
        close_val = safe_float(row.get("close"))
        atr14 = safe_float(row.get("atr14"))
        plus_di = safe_float(row.get("plus_di14"))
        minus_di = safe_float(row.get("minus_di14"))
        obv = safe_float(row.get("obv"))
        obv_ema = safe_float(row.get("obv_ema20"))
        # DMI bias: bullish if +DI > -DI, bearish if -DI > +DI, else neutral.
        # None when either reading is unavailable (warmup bars).
        if plus_di is not None and minus_di is not None:
            if plus_di > minus_di:
                dmi_bias = "bullish"
            elif minus_di > plus_di:
                dmi_bias = "bearish"
            else:
                dmi_bias = "neutral"
        else:
            dmi_bias = None
        # OBV bias: bullish if OBV > OBV-EMA, bearish if below, else neutral.
        if obv is not None and obv_ema is not None:
            if obv > obv_ema:
                obv_bias = "bullish"
            elif obv < obv_ema:
                obv_bias = "bearish"
            else:
                obv_bias = "neutral"
        else:
            obv_bias = None
        bar_candle_match = per_bar_candles.get(idx, {})
        candles_bullish = list(bar_candle_match.get("bullish", []))
        candles_bearish = list(bar_candle_match.get("bearish", []))
        bars.append({
            "ts": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
            "abs_index": tail_offset + rel_idx,
            # True only on a chart's still-forming last bucket, which
            # chart_payload marks; every bar built here is complete.
            "in_progress": False,
            "open": safe_float(row.get("open")),
            "high": safe_float(row.get("high")),
            "low": safe_float(row.get("low")),
            "close": close_val,
            "volume": safe_float(row.get("volume")),
            "ema9": safe_float(row.get("ema9")),
            "ema20": safe_float(row.get("ema20")),
            "vwap": safe_float(row.get("vwap")),
            "atr14": atr14,
            "atr_pct": (atr14 / close_val) if atr14 is not None and close_val not in (None, 0.0) else None,
            "ret1": safe_float(row.get("ret1")),
            "ret5": safe_float(row.get("ret5")),
            "ret15": safe_float(row.get("ret15")),
            "bb_mid": safe_float(row.get("bb_mid")),
            "bb_upper": safe_float(row.get("bb_upper")),
            "bb_lower": safe_float(row.get("bb_lower")),
            "bb_width_pct": safe_float(row.get("bb_width_pct")),
            "bb_percent_b": safe_float(row.get("bb_percent_b")),
            "bb_zscore": safe_float(row.get("bb_zscore")),
            "adx": safe_float(row.get("adx14")),
            "plus_di": plus_di,
            "minus_di": minus_di,
            "dmi_bias": dmi_bias,
            "obv": obv,
            "obv_ema": obv_ema,
            "obv_bias": obv_bias,
            "candles_bullish": candles_bullish,
            "candles_bearish": candles_bearish,
        })
    return bars


# TA-Lib's candle functions read at most 14 bars before the bar they score
# (CDLBREAKAWAY, CDLLADDERBOTTOM, CDLMATHOLD and CDLRISEFALL3METHODS; the
# custom tweezers read 1), so a per-bar pattern map fed this many bars ahead
# of the ones it shows scores every shown bar exactly as a full-history run
# does. Until 2026-09-23 the snapshot fed TA-Lib only its 48 shown bars and the
# chart only its 90/360: on 2026-09-18/21/22 (10 symbols x 6 times) 1,836 of
# 24,840 shown bars carried different tags than a 400-bar run, all in the
# oldest bars of the window, and the snapshot's starved tags overwrote the
# chart's on the newest 48 bars. 12 extra bars already matched on every bar.
_CANDLE_PATTERN_WARMUP_BARS = 14

# Key-level zone kinds for a level price has crossed: broken_* once the flip
# is confirmed, pending_* while it is not. Each is drawn as its own zone, in
# its flipped role once confirmed and marked pending until then.
_FLIP_CANDIDATE_LEVEL_KINDS = frozenset({
    "broken_htf_support",
    "broken_htf_resistance",
    "pending_htf_support",
    "pending_htf_resistance",
})


def dashboard_htf_chart_frame(
    completed: pd.DataFrame | None,
    minute_frame: pd.DataFrame | None,
    *,
    timeframe_minutes: int,
    now: datetime,
) -> tuple[pd.DataFrame | None, pd.Timestamp | None]:
    """The HTF chart's frame, and the start of its still-forming bucket (None
    when every bucket in it is complete).

    ``completed`` is the stored HTF frame: completed bars only, refreshed once
    per bucket, so on its own the chart ends at the last bucket completed
    before that refresh. The buckets after it are built here from the live 1m
    frame -- cut to the 07:00-20:00 window like the stored bars, then
    resampled on the same session grid -- and the one holding ``now`` is
    the forming bucket. Until 2026-09-23 the chart plotted the stored frame
    as-is, whose last row was the bucket Schwab returned seconds after it
    opened, drawn as if complete and frozen at that stub for the whole
    bucket.
    """
    if completed is None or completed.empty or minute_frame is None or minute_frame.empty:
        return completed, None
    ohlcv = ["open", "high", "low", "close", "volume"]
    completed_end = session_bucket_ends(completed.index[-1:], int(timeframe_minutes))[0]
    after = minute_frame.loc[minute_frame.index >= completed_end, ohlcv]
    if after.empty:
        return completed, None
    after = equity_stream_window_bars(after)
    if after.empty:
        return completed, None
    buckets = resample_bars(after, f"{int(timeframe_minutes)}min")
    if buckets.empty:
        return completed, None
    frame = ensure_standard_indicator_frame(pd.concat([completed[ohlcv], buckets[ohlcv]]))
    last_start = pd.Timestamp(buckets.index[-1])
    last_end = session_bucket_ends(buckets.index[-1:], int(timeframe_minutes))[0]
    forming = last_start if last_end > pd.Timestamp(now) else None
    return frame, forming


def dashboard_structure_event_label(ms_ctx: Any) -> str:
    if ms_ctx is None:
        return "—"
    candidates: list[tuple[int, int, str]] = []
    choch_up_age = getattr(ms_ctx, "choch_up_age_bars", None)
    choch_down_age = getattr(ms_ctx, "choch_down_age_bars", None)
    bos_up_age = getattr(ms_ctx, "bos_up_age_bars", None)
    bos_down_age = getattr(ms_ctx, "bos_down_age_bars", None)
    if bool(getattr(ms_ctx, "choch_up", False)) and choch_up_age is not None:
        candidates.append((int(choch_up_age), 0, "CHOCH↑"))
    if bool(getattr(ms_ctx, "choch_down", False)) and choch_down_age is not None:
        candidates.append((int(choch_down_age), 0, "CHOCH↓"))
    if bool(getattr(ms_ctx, "bos_up", False)) and bos_up_age is not None:
        candidates.append((int(bos_up_age), 1, "BOS↑"))
    if bool(getattr(ms_ctx, "bos_down", False)) and bos_down_age is not None:
        candidates.append((int(bos_down_age), 1, "BOS↓"))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        return candidates[0][2]
    return "—"


class DashboardCache:
    """Dashboard-side state container + config-bound helpers.

    Owns snapshot/chart caches, the rate-limited error logger, and the
    small config-reading helpers that resolve chart profile / max-bars /
    candidate-limit from ``config.dashboard`` and ``config.tradingview``.
    """

    def __init__(
        self,
        config: BotConfig,
        *,
        data: Any = None,
        strategy: Any = None,
        account: Any = None,
    ) -> None:
        self.config = config
        self.data = data
        self.strategy = strategy
        self.account = account
        self.snapshot_cache: dict[str, dict[str, Any]] = {}
        self.chart_cache: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.lock = RLock()
        self._error_log_times: dict[str, float] = {}

    def prune_inactive_symbols(self, active_symbols: set[str]) -> int:
        """Drop cached snapshot + chart payloads for symbols no longer in the
        active set. Mirrors `MarketDataStore.prune_inactive_symbols` on the
        dashboard side. Each entry is a deep-copied serialized payload —
        kilobytes each — so a long-running bot with high symbol churn
        accumulates real memory here too. Returns count evicted."""
        active = {str(s).upper().strip() for s in (active_symbols or set()) if s}
        with self.lock:
            snap_stale = {sym for sym in self.snapshot_cache.keys() if str(sym).upper().strip() not in active}
            chart_stale = {key for key in self.chart_cache.keys() if str(key[0]).upper().strip() not in active}
            for sym in snap_stale:
                self.snapshot_cache.pop(sym, None)
            for key in chart_stale:
                self.chart_cache.pop(key, None)
        # Distinct symbol count, not entry count, so the engine has a
        # consistent figure to log alongside the data_feed prune count.
        return len({str(sym).upper().strip() for sym in (snap_stale | {k[0] for k in chart_stale})})

    def log_component_failure(self, component: str, message: str, *message_args: Any) -> None:
        """Rate-limited component error logger.

        Emits ``LOG.warning(message, ..., exc_info=True)`` at most once per
        60 seconds per ``component``; intermediate failures go to DEBUG so
        they're still captured but don't spam the WARNING stream."""
        key = str(component or "dashboard")
        now_ts = time.monotonic()
        last_ts = float(self._error_log_times.get(key, 0.0) or 0.0)
        if now_ts - last_ts >= 60.0:
            self._error_log_times[key] = now_ts
            LOG.warning(message, *message_args, exc_info=True)
        else:
            LOG.debug(message, *message_args, exc_info=True)

    # ---------------------------------------------------------------------
    # Chart-profile helpers (Phase 5 Step 3 extraction).
    # Previously instance methods on IntradayBot.
    # ---------------------------------------------------------------------

    def chart_profile(self, mode: str = "compact") -> DashboardChartConfig:
        cfg = getattr(self.config.dashboard, "charting", None)
        if isinstance(cfg, DashboardChartingConfig):
            return cfg.resolved_profile(mode)
        return DashboardChartConfig()

    def chart_max_bars(self, mode: str = "compact") -> int:
        profile = self.chart_profile(mode)
        fallback_profile = DashboardChartingConfig().resolved_profile(mode)
        fallback_max_bars = int(getattr(fallback_profile, "max_bars", 90) or 90)
        try:
            return max(1, min(int(getattr(profile, "max_bars", fallback_max_bars) or fallback_max_bars), 480))
        except Exception:
            return fallback_max_bars

    def snapshot_max_bars(self) -> int:
        try:
            return max(12, min(self.chart_max_bars("compact"), 48))
        except Exception:
            return 48

    def charting_settings(self) -> dict[str, Any]:
        charting_cfg = getattr(self.config.dashboard, "charting", None)
        compact_timeframe = "ltf"
        if isinstance(charting_cfg, DashboardChartingConfig):
            compact_timeframe = charting_cfg.normalized_compact_chart_timeframe()
        return {
            "compact_chart_timeframe": compact_timeframe,
            "compact": asdict(self.chart_profile("compact")),
            "expanded": asdict(self.chart_profile("expanded")),
        }

    def candidate_limit(self, strategy: Any = None) -> int:
        """Resolve the max candidate rows to emit on the dashboard.

        Base limit is ``config.tradingview.max_candidates``; a strategy may
        override via ``dashboard_candidate_limit(base)``."""
        strategy = strategy if strategy is not None else self.strategy
        limit = max(1, int(self.config.tradingview.max_candidates))
        if strategy is None:
            return limit
        try:
            return max(1, int(strategy.dashboard_candidate_limit(limit)))
        except Exception:
            return limit

    # ---------------------------------------------------------------------
    # Payload builders that need data/strategy/account (Phase 5 Step 5).
    # ---------------------------------------------------------------------

    def _active_htf_minutes(self) -> int:
        """HTF (higher timeframe) for SR detection, key level zones, sidebar
        ladder, engine SR/stops/exits. Strategies with their own HTF concept
        declare it via `params.htf_minutes`; otherwise inherits the shared
        default from `support_resistance.timeframe_minutes`."""
        cfg = getattr(self.config, "support_resistance", None)
        fallback = int(getattr(cfg, "timeframe_minutes", 15)) if cfg is not None else 15
        params = getattr(self.strategy, "params", {}) or {}
        return int(params.get("htf_minutes", fallback))

    def _active_htf_lookback_days(self) -> int:
        cfg = getattr(self.config, "support_resistance", None)
        fallback = int(getattr(cfg, "lookback_days", 10)) if cfg is not None else 10
        params = getattr(self.strategy, "params", {}) or {}
        return int(params.get("htf_lookback_days", fallback))

    def _chart_htf_level_request(self) -> dict[str, Any]:
        """Level arguments of the HTF context the chart's HTF FVGs and RSI
        divergence lines are drawn from: the strategy's own HTF build
        (``dashboard_level_context_spec``, which its level zones use and
        which matches the context its HTF divergence score reads), else
        support_resistance. Until 2026-09-24 it was support_resistance with
        EMA 50/200 whatever the strategy: the peer family scores divergence
        on ``htf_pivot_span``, so a preset changing it would chart
        divergences the score did not apply (and miss ones it did)."""
        sr_cfg = self.config.support_resistance
        spec = self.strategy.dashboard_level_context_spec() if self.strategy is not None else None
        spec = spec if isinstance(spec, dict) else {}
        default_fast, default_slow = htf_ema_spans({})
        return {
            "pivot_span": int(spec.get("pivot_span", sr_cfg.pivot_span)),
            "max_levels_per_side": int(spec.get("max_levels_per_side", sr_cfg.max_levels_per_side)),
            "atr_tolerance_mult": float(spec.get("atr_tolerance_mult", sr_cfg.atr_tolerance_mult)),
            "pct_tolerance": float(spec.get("pct_tolerance", sr_cfg.pct_tolerance)),
            "stop_buffer_atr_mult": float(spec.get("stop_buffer_atr_mult", sr_cfg.stop_buffer_atr_mult)),
            "ema_fast_span": int(spec.get("ema_fast_span", default_fast)),
            "ema_slow_span": int(spec.get("ema_slow_span", default_slow)),
        }

    def _active_ltf_minutes(self) -> int:
        """LTF (lower timeframe / trigger frame). Strategies with a distinct
        intraday trigger candle declare `params.ltf_minutes` (e.g.
        peer_confirmed_key_levels uses 5-min trigger candles). Otherwise
        defaults to 1-minute streamed bars."""
        params = getattr(self.strategy, "params", {}) or {}
        return int(params.get("ltf_minutes", 1))

    def _per_bar_candle_map(self, frame: pd.DataFrame, shown_bars: int) -> dict[Any, dict[str, list[str]]]:
        """Per-bar candle tags for the last ``shown_bars`` bars of ``frame``,
        each scored with full TA-Lib context (``_CANDLE_PATTERN_WARMUP_BARS``).
        The snapshot and the chart payload both use it, so the snapshot bars
        the client merges over the chart's carry the chart's tags."""
        return detect_per_bar_candle_patterns(
            frame,
            bullish_allowed=self.config.candles.bullish_patterns,
            bearish_allowed=self.config.candles.bearish_patterns,
            lookback=int(shown_bars) + _CANDLE_PATTERN_WARMUP_BARS,
        )

    def _apply_strategy_ltf_emas(
        self,
        symbol: str,
        frame: pd.DataFrame,
        bars: list[dict[str, Any]],
        *,
        timeframe: str,
    ) -> tuple[int, int]:
        """Put the strategy's own LTF fast/slow EMA on ``bars`` (the tail of
        ``frame``, a ``timeframe`` frame) and return the two spans.

        The strategy reads ema9/ema20 off ``get_merged(timeframe,
        span_scale=ltf_indicator_span_scale, ema_spans=ltf_ema_spans(params))``:
        on top_tier's 1m LTF that is a 45/100-bar EMA (its
        ltf_ema_fast_span / ltf_ema_slow_span) that restarts on each session's
        first RTH bar. Until 2026-09-23 the chart drew a continuous 45/100 EWM across the
        prior day and premarket instead (the opposite stack to the bot's on 93
        of 480 bars between 09:30 and 10:30 across 8 symbols on 2026-09-22),
        and the snapshot bars, merged over the chart's newest 48, carried the
        native 9/20 -- so the lines labelled EMA45/EMA100 turned into EMA9/20
        partway along the chart. The snapshot and the chart both go through
        here.
        """
        params = getattr(self.strategy, "params", {}) or {}
        scale = float(params.get("ltf_indicator_span_scale", 1.0))
        spans = ltf_ema_spans(params)
        # The frame the caller built is canonical (scale 1, EMA 9/20); fetch
        # the strategy's own whenever either differs.
        if bars and (scale != 1.0 or spans != (9, 20)):
            scaled = self.data.get_merged(symbol, timeframe=timeframe, with_indicators=True,
                                          span_scale=scale, ema_spans=spans)
            emas = scaled[["ema9", "ema20"]].reindex(frame.index[-len(bars):])
            for bar, fast, slow in zip(bars, emas["ema9"], emas["ema20"]):
                bar["ema9"] = safe_float(fast)
                bar["ema20"] = safe_float(slow)
        return spans

    def htf_trend(self, symbol: str, *, allow_refresh: bool = True) -> dict[str, Any]:
        tf = self._active_htf_minutes()
        lookback_days = self._active_htf_lookback_days()
        frame = None
        if self.data is not None and hasattr(self.data, "get_htf_frame"):
            frame = self.data.get_htf_frame(
                symbol,
                timeframe_minutes=tf,
                lookback_days=lookback_days,
                allow_refresh=allow_refresh,
            )
        summary = summarize_htf_trend(
            frame,
            min_bars=20,
            vwap_distance_pct=0.0010,
            ema_gap_pct=0.0008,
            min_ret3=0.0010,
            range_vwap_distance_pct=0.0020,
            range_ema_gap_pct=0.0010,
        )
        return {
            "label": str(summary.get("label", "—")),
            "state": str(summary.get("state", "neutral")),
            "vwap_dist": float(summary.get("vwap_dist", 0.0) or 0.0),
            "ema_gap": float(summary.get("ema_gap", 0.0) or 0.0),
            "ret3": float(summary.get("ret3", 0.0) or 0.0),
            "timeframe": f"{tf}m",
        }

    def symbol_price(self, symbol: str) -> float | None:
        quote = self.data.get_quote(symbol) or {} if self.data is not None else {}
        for key in ("last", "mark", "mid", "close", "bid", "ask"):
            value = quote.get(key)
            try:
                if value is not None and float(value) > 0:
                    return float(value)
            except Exception:
                continue
        if self.data is not None:
            try:
                frame = self.data.get_merged(symbol, with_indicators=False)
                if frame is not None and not frame.empty:
                    return float(frame.iloc[-1].close)
            except Exception:
                LOG.debug(
                    "Failed to read merged frame last price for %s; falling back to cached/account.",
                    symbol, exc_info=True,
                )
        if self.account is not None:
            cached = getattr(self.account, "last_prices", {}).get(symbol)
            if cached is not None:
                try:
                    return float(cached)
                except Exception:
                    return None
        return None

    @staticmethod
    def _normalize_symbol_list(values: object) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        invalid_tokens = {"NONE", "NULL", "NAN"}
        for raw in values if isinstance(values, list | tuple | set) else []:
            if raw is None:
                continue
            token = str(raw).upper().strip()
            if not token or token in invalid_tokens or token in seen:
                continue
            seen.add(token)
            out.append(token)
        return out

    def symbol_snapshot(
        self,
        symbol: str,
        exchange: str | None = None,
        sr_row: dict[str, Any] | None = None,
        candidate_row: dict[str, Any] | None = None,
        position_row: dict[str, Any] | None = None,
        entry_decision: dict[str, Any] | None = None,
        warmup: dict[str, Any] | None = None,
        *,
        allow_refresh: bool = True,
    ) -> dict[str, Any]:
        """Assemble the full dashboard snapshot payload for a single symbol.
        Largest of the dashboard payload builders — combines quote, SR,
        levels, technicals, bars, patterns, and position markers into one
        cache-keyed dict. Extracted from IntradayBot."""
        symbol = str(symbol or "").upper().strip()
        quote = self.data.get_quote(symbol) or {}
        max_quote_age = max(1.0, float(self.config.runtime.quote_cache_seconds))
        quote_is_fresh = bool(symbol and quote and self.data.quotes_are_fresh([symbol], max_quote_age))
        if sr_row is None and symbol:
            sr_row = self.sr_row(symbol, allow_refresh=allow_refresh)
        frame = self.data.get_merged(symbol, with_indicators=True) if symbol else None
        snapshot_signature = self.symbol_snapshot_signature(
            symbol,
            frame,
            quote=quote,
            quote_is_fresh=quote_is_fresh,
            sr_row=sr_row,
            candidate_row=candidate_row,
            position_row=position_row,
            entry_decision=entry_decision,
            warmup=warmup,
            allow_refresh=allow_refresh,
        )
        with self.lock:
            cached_snapshot = self.snapshot_cache.get(symbol)
            if cached_snapshot is not None and cached_snapshot.get("signature") == snapshot_signature and not self.snapshot_should_bypass_cache(symbol, allow_refresh=allow_refresh):
                # Shallow copy on cache hit instead of deepcopy. The
                # snapshot is a flat-ish dict of pre-computed values;
                # downstream serialization (`_json_safe`) creates new
                # containers rather than mutating, so sharing inner
                # references is safe. Saves ~5ms per cache hit on
                # busy multi-symbol watchlists where dashboard polls
                # this for every snapshot every refresh cycle.
                return dict(cached_snapshot["payload"])
        # Per-bar candle pattern map (completion-bar only, tier cascade).
        # Drives the tooltip's "Candle Patterns (this bar)" section. Computed
        # before bars are built so each bar dict can carry its own matched
        # patterns, for every bar the snapshot carries.
        snapshot_bars_count = self.snapshot_max_bars()
        snapshot_per_bar_candles: dict[Any, dict[str, list[str]]] = {}
        if frame is not None and not frame.empty:
            try:
                snapshot_per_bar_candles = self._per_bar_candle_map(frame, snapshot_bars_count)
            except Exception:
                self.log_component_failure(
                    "per_bar_candles",
                    "Per-bar candle pattern detection failed for %s",
                    symbol,
                )
                snapshot_per_bar_candles = {}
        bars = dashboard_bars_from_frame(
            frame,
            max_bars=snapshot_bars_count,
            per_bar_candles=snapshot_per_bar_candles,
        )
        # Snapshot bars are 1m bars; they are the strategy's LTF bars (and are
        # merged into the LTF chart) only when its LTF is 1m.
        snapshot_ema_spans = (9, 20)
        if bars and self._active_ltf_minutes() == 1:
            snapshot_ema_spans = self._apply_strategy_ltf_emas(symbol, frame, bars, timeframe="1min")
        latest_bar: dict[str, Any] = bars[-1] if bars else {}
        session_total_volume: float | None = None
        if frame is not None and not frame.empty:
            try:
                if isinstance(frame.index, pd.DatetimeIndex) and "volume" in frame.columns:
                    session_index = pd.DatetimeIndex(frame.index)
                    session_anchor = pd.Timestamp(session_index[-1]).normalize()
                    same_session_mask = session_index.normalize() == session_anchor
                    if bool(getattr(same_session_mask, "any", lambda: False)()):
                        session_volume_values = frame.loc[same_session_mask, "volume"]
                        session_volume_series = pd.Series(session_volume_values, copy=False)
                        session_volume_numeric_values = pd.to_numeric(session_volume_series, errors="coerce")
                        session_volume_numeric = pd.Series(session_volume_numeric_values, copy=False)
                        session_volume = session_volume_numeric.fillna(0.0).sum()
                        session_total_volume = safe_float(session_volume)
            except Exception:
                session_total_volume = None

        quote_last = safe_float(quote.get("last")) if quote_is_fresh else None
        quote_bid = safe_float(quote.get("bid")) if quote_is_fresh else None
        quote_ask = safe_float(quote.get("ask")) if quote_is_fresh else None
        quote_mark = safe_float(quote.get("mark")) if quote_is_fresh else None
        quote_mid = safe_float(quote.get("mid")) if quote_is_fresh else None
        quote_open = safe_float(quote.get("open"))
        quote_close = safe_float(quote.get("close"))
        quote_total_volume = safe_float(quote.get("total_volume")) if quote_is_fresh else None
        # data_feed._normalize_quote reads percent_change / net_change with
        # numeric.first_float, which yields None (not 0.0) when both Schwab
        # fields are absent or NaN, so a 0.0 here is always a real
        # flat-session reading rather than a sentinel.
        cached_percent_change = safe_float(quote.get("percent_change"))
        cached_net_change = safe_float(quote.get("net_change"))
        candidate_percent_change = safe_float((candidate_row or {}).get("change_from_open"))
        candidate_close = safe_float((candidate_row or {}).get("close"))
        regular_session_active = self.data.is_regular_session(sessions.now_et())
        display_total_volume = quote_total_volume
        if display_total_volume is None:
            display_total_volume = session_total_volume
        last_price = quote_last
        if last_price is None:
            last_price = safe_float(latest_bar.get("close"))
        display_close = quote_close
        if not regular_session_active and candidate_close is not None:
            display_close = candidate_close
        if display_close is None and candidate_close is not None:
            display_close = candidate_close
        if display_close is None and len(bars) >= 2:
            display_close = safe_float(bars[-2].get("close"))
        session_reference_close = quote_close
        if not regular_session_active and candidate_percent_change is not None:
            percent_change = candidate_percent_change
        else:
            percent_change = cached_percent_change
        if percent_change is None:
            percent_change = candidate_percent_change
        if percent_change is None and last_price not in (None, 0.0) and session_reference_close not in (None, 0.0):
            percent_change = ((last_price - session_reference_close) / session_reference_close) * 100.0
        net_change = cached_net_change
        if net_change is None and last_price is not None and session_reference_close not in (None, 0.0):
            net_change = last_price - session_reference_close
        display_mark = quote_mark if quote_mark is not None else last_price
        display_mid = quote_mid if quote_mid is not None else display_mark

        current_price = last_price
        if current_price is None:
            current_price = safe_float((sr_row or {}).get("price"))
        if current_price is None:
            current_price = safe_float(latest_bar.get("close"))

        support_prices: list[float] = []
        resistance_prices: list[float] = []
        next_support = None
        next_resistance = None
        ladder_min_gap = safe_float((sr_row or {}).get("side_tolerance")) or _sr_effective_side_tolerance(self.config, current_price)
        technical_payload: dict[str, Any] = {}
        nearest_support = None
        nearest_resistance = None
        if sr_row:
            nearest_support = safe_float(sr_row.get("nearest_support"))
            nearest_resistance = safe_float(sr_row.get("nearest_resistance"))

            support_prices = sorted(
                [float(v) for v in (sr_row.get("supports") or []) if safe_float(v) not in (None, 0.0)],
                reverse=True,
            )
            resistance_prices = sorted(
                [float(v) for v in (sr_row.get("resistances") or []) if safe_float(v) not in (None, 0.0)]
            )

            if nearest_support is not None:
                support_prices.append(float(nearest_support))
            if nearest_resistance is not None:
                resistance_prices.append(float(nearest_resistance))

            support_prices = _collapse_price_ladder(support_prices, reverse=True, min_gap=ladder_min_gap)
            resistance_prices = _collapse_price_ladder(resistance_prices, reverse=False, min_gap=ladder_min_gap)
            support_anchor_prices = list(support_prices)
            resistance_anchor_prices = list(resistance_prices)

            if nearest_support is None and support_prices:
                nearest_support = support_prices[0]
            if nearest_resistance is None and resistance_prices:
                nearest_resistance = resistance_prices[0]

            support_prices = [
                price for price in support_anchor_prices
                if nearest_support is None or abs(price - nearest_support) > max(1e-9, ladder_min_gap)
            ]
            resistance_prices = [
                price for price in resistance_anchor_prices
                if nearest_resistance is None or abs(price - nearest_resistance) > max(1e-9, ladder_min_gap)
            ]

            next_support = support_prices[0] if support_prices else None
            next_resistance = resistance_prices[0] if resistance_prices else None

        # Build technical levels (fib extensions/retracements, AVWAP,
        # Bollinger, ADX, channels, trendlines, etc.) on the strategy's LTF
        # frame so all overlays render at LTF-derived prices. Strategies
        # with default LTF=1 keep using the 1m streamed frame; strategies
        # with non-1m LTF (e.g. peer_confirmed_key_levels at LTF=5m) get
        # 5m-derived fibs/AVWAP/etc. matching the LTF chart bars.
        ltf_min_for_tech = self._active_ltf_minutes()
        if ltf_min_for_tech == 1:
            tech_frame = frame
        elif self.data is not None and symbol:
            tech_frame = self.data.get_merged(symbol, timeframe=f"{ltf_min_for_tech}min", with_indicators=True)
        else:
            tech_frame = frame
        tech_ctx = None  # Stays None when tech_frame is empty (warmup path) or build_technical_levels_context raises; downstream readers (technical_payload, divergence_lines) all guard on `tech_ctx is not None`.
        if tech_frame is not None and not tech_frame.empty:
            tl_cfg = self.config.technical_levels
            sr_cfg = self.config.support_resistance
            try:
                # tech_frame itself, not a filtered copy: the lines come back
                # positioned in the frame passed, and tech_frame is the frame
                # the LTF chart's bars (and their abs_index) are cut from.
                tech_ctx = build_technical_levels_context(
                    tech_frame,
                    current_price=current_price,
                    pivot_span=int(getattr(sr_cfg, "structure_ltf_pivot_span", getattr(sr_cfg, "pivot_span", 2)) or 2),
                    fib_lookback_bars=int(getattr(tl_cfg, "fib_lookback_bars", 120) or 120),
                    fib_min_impulse_atr=float(getattr(tl_cfg, "fib_min_impulse_atr", 1.25) or 1.25),
                    anchored_vwap_impulse_lookback_bars=(int(getattr(tl_cfg, "anchored_vwap_impulse_lookback_bars")) if getattr(tl_cfg, "anchored_vwap_impulse_lookback_bars", None) is not None else None),
                    anchored_vwap_min_impulse_atr=(float(getattr(tl_cfg, "anchored_vwap_min_impulse_atr")) if getattr(tl_cfg, "anchored_vwap_min_impulse_atr", None) is not None else None),
                    anchored_vwap_pivot_span=(int(getattr(tl_cfg, "anchored_vwap_pivot_span")) if getattr(tl_cfg, "anchored_vwap_pivot_span", None) is not None else None),
                    trendline_lookback_bars=int(getattr(tl_cfg, "trendline_lookback_bars", 120) or 120),
                    trendline_min_touches=int(getattr(tl_cfg, "trendline_min_touches", 3) or 3),
                    trendline_atr_tolerance_mult=float(getattr(tl_cfg, "trendline_atr_tolerance_mult", 0.35) or 0.35),
                    trendline_breakout_buffer_atr_mult=float(getattr(tl_cfg, "trendline_breakout_buffer_atr_mult", 0.65)),
                    channel_lookback_bars=int(getattr(tl_cfg, "channel_lookback_bars", 120) or 120),
                    channel_min_touches=int(getattr(tl_cfg, "channel_min_touches", 3) or 3),
                    channel_atr_tolerance_mult=float(getattr(tl_cfg, "channel_atr_tolerance_mult", 0.35) or 0.35),
                    channel_parallel_slope_frac=float(getattr(tl_cfg, "channel_parallel_slope_frac", 0.12) or 0.12),
                    channel_min_gap_atr_mult=float(getattr(tl_cfg, "channel_min_gap_atr_mult", 0.80) or 0.80),
                    channel_min_gap_pct=float(getattr(tl_cfg, "channel_min_gap_pct", 0.0025) or 0.0025),
                    bollinger_length=int(getattr(tl_cfg, "bollinger_length", 20) or 20),
                    bollinger_std_mult=float(getattr(tl_cfg, "bollinger_std_mult", 2.0) or 2.0),
                    bollinger_squeeze_width_pct=float(getattr(tl_cfg, "bollinger_squeeze_width_pct", 0.060) or 0.060),
                    atr_expansion_lookback=int(getattr(tl_cfg, "atr_expansion_lookback", 5) or 5),
                    adx_length=int(getattr(tl_cfg, "adx_length", 14) or 14),
                    obv_ema_length=int(getattr(tl_cfg, "obv_ema_length", 20) or 20),
                    divergence_rsi_length=int(getattr(tl_cfg, "divergence_rsi_length", 14) or 14),
                    # As configured, as the strategy and the HTF build read
                    # them: `or <default>` drew a configured 0 at the default
                    # (2026-09-25), and a null fails the build.
                    divergence_rsi_min_delta=float(tl_cfg.divergence_rsi_min_delta),
                    divergence_obv_min_volume_frac=float(getattr(tl_cfg, "divergence_obv_min_volume_frac", 0.50) or 0.50),
                    divergence_pivot_lookback=int(tl_cfg.divergence_pivot_lookback),
                    # As configured: `or 8` drew a configured 0 at age 8 (2026-09-24).
                    divergence_max_age_bars=int(tl_cfg.divergence_max_age_bars),
                    divergence_min_price_move_pct=float(tl_cfg.divergence_min_price_move_pct),
                    fib_enabled=bool(getattr(tl_cfg, "fib_enabled", True)),
                    channel_enabled=bool(getattr(tl_cfg, "channel_enabled", True)),
                    trendline_enabled=bool(getattr(tl_cfg, "trendline_enabled", True)),
                    adx_enabled=bool(getattr(tl_cfg, "adx_enabled", True)),
                    anchored_vwap_enabled=bool(getattr(tl_cfg, "anchored_vwap_enabled", True)),
                    atr_context_enabled=bool(getattr(tl_cfg, "atr_context_enabled", True)),
                    obv_enabled=bool(getattr(tl_cfg, "obv_enabled", True)),
                    divergence_enabled=bool(getattr(tl_cfg, "divergence_enabled", True)),
                    bollinger_enabled=bool(getattr(tl_cfg, "bollinger_enabled", True)),
                )
            except Exception:
                self.log_component_failure(
                    "technical_overlay",
                    "Dashboard technical overlay build failed for %s",
                    symbol,
                )
                tech_ctx = None
            if tech_ctx is not None:
                technical_payload = {
                    "fib_direction": str(getattr(tech_ctx, "fib_direction", "neutral") or "neutral"),
                    "fib_bullish_1272": safe_float(getattr(tech_ctx, "fib_bullish_1272", None)),
                    "fib_bullish_1618": safe_float(getattr(tech_ctx, "fib_bullish_1618", None)),
                    "fib_bearish_1272": safe_float(getattr(tech_ctx, "fib_bearish_1272", None)),
                    "fib_bearish_1618": safe_float(getattr(tech_ctx, "fib_bearish_1618", None)),
                    "fib_bullish_382": safe_float(getattr(tech_ctx, "fib_bullish_382", None)),
                    "fib_bullish_500": safe_float(getattr(tech_ctx, "fib_bullish_500", None)),
                    "fib_bullish_618": safe_float(getattr(tech_ctx, "fib_bullish_618", None)),
                    "fib_bullish_786": safe_float(getattr(tech_ctx, "fib_bullish_786", None)),
                    "fib_bearish_382": safe_float(getattr(tech_ctx, "fib_bearish_382", None)),
                    "fib_bearish_500": safe_float(getattr(tech_ctx, "fib_bearish_500", None)),
                    "fib_bearish_618": safe_float(getattr(tech_ctx, "fib_bearish_618", None)),
                    "fib_bearish_786": safe_float(getattr(tech_ctx, "fib_bearish_786", None)),
                    "anchored_vwap_open": safe_float(getattr(tech_ctx, "anchored_vwap_open", None)),
                    "anchored_vwap_bullish_impulse": safe_float(getattr(tech_ctx, "anchored_vwap_bullish_impulse", None)),
                    "anchored_vwap_bearish_impulse": safe_float(getattr(tech_ctx, "anchored_vwap_bearish_impulse", None)),
                    "anchored_vwap_bias": str(getattr(tech_ctx, "anchored_vwap_bias", "neutral") or "neutral"),
                    "adx": safe_float(getattr(tech_ctx, "adx", None)),
                    "plus_di": safe_float(getattr(tech_ctx, "plus_di", None)),
                    "minus_di": safe_float(getattr(tech_ctx, "minus_di", None)),
                    "dmi_bias": str(getattr(tech_ctx, "dmi_bias", "neutral") or "neutral"),
                    "adx_rising": bool(getattr(tech_ctx, "adx_rising", False)),
                    "atr14": safe_float(getattr(tech_ctx, "atr14", None)),
                    "atr_pct": safe_float(getattr(tech_ctx, "atr_pct", None)),
                    "atr_expansion_mult": safe_float(getattr(tech_ctx, "atr_expansion_mult", None)),
                    "atr_stretch_vwap_mult": safe_float(getattr(tech_ctx, "atr_stretch_vwap_mult", None)),
                    "atr_stretch_ema20_mult": safe_float(getattr(tech_ctx, "atr_stretch_ema20_mult", None)),
                    "obv": safe_float(getattr(tech_ctx, "obv", None)),
                    "obv_ema": safe_float(getattr(tech_ctx, "obv_ema", None)),
                    "obv_bias": str(getattr(tech_ctx, "obv_bias", "neutral") or "neutral"),
                    "rsi14": safe_float(getattr(tech_ctx, "rsi14", None)),
                    "bullish_rsi_divergence": getattr(tech_ctx, "bullish_rsi_divergence", None) is not None,
                    "bearish_rsi_divergence": getattr(tech_ctx, "bearish_rsi_divergence", None) is not None,
                    "bullish_obv_divergence": getattr(tech_ctx, "bullish_obv_divergence", None) is not None,
                    "bearish_obv_divergence": getattr(tech_ctx, "bearish_obv_divergence", None) is not None,
                    "bullish_hidden_rsi_divergence": getattr(tech_ctx, "bullish_hidden_rsi_divergence", None) is not None,
                    "bearish_hidden_rsi_divergence": getattr(tech_ctx, "bearish_hidden_rsi_divergence", None) is not None,
                    "bullish_hidden_obv_divergence": getattr(tech_ctx, "bullish_hidden_obv_divergence", None) is not None,
                    "bearish_hidden_obv_divergence": getattr(tech_ctx, "bearish_hidden_obv_divergence", None) is not None,
                    "counter_divergence_bias": str(getattr(tech_ctx, "counter_divergence_bias", "neutral") or "neutral"),
                    "bollinger_mid": safe_float(getattr(tech_ctx, "bollinger_mid", None)),
                    "bollinger_upper": safe_float(getattr(tech_ctx, "bollinger_upper", None)),
                    "bollinger_lower": safe_float(getattr(tech_ctx, "bollinger_lower", None)),
                    "bollinger_width_pct": safe_float(getattr(tech_ctx, "bollinger_width_pct", None)),
                    "bollinger_percent_b": safe_float(getattr(tech_ctx, "bollinger_percent_b", None)),
                    "bollinger_zscore": safe_float(getattr(tech_ctx, "bollinger_zscore", None)),
                    "bollinger_squeeze": bool(getattr(tech_ctx, "bollinger_squeeze", False)),
                    "bollinger_upper_reject": bool(getattr(tech_ctx, "bollinger_upper_reject", False)),
                    "bollinger_lower_reject": bool(getattr(tech_ctx, "bollinger_lower_reject", False)),
                    "channel": {
                        "valid": bool(getattr(getattr(tech_ctx, "channel", None), "valid", False)),
                        "bias": str(getattr(getattr(tech_ctx, "channel", None), "bias", "neutral") or "neutral"),
                        "lower": safe_float(getattr(getattr(tech_ctx, "channel", None), "lower", None)),
                        "upper": safe_float(getattr(getattr(tech_ctx, "channel", None), "upper", None)),
                        "mid": safe_float(getattr(getattr(tech_ctx, "channel", None), "mid", None)),
                        "position_pct": safe_float(getattr(getattr(tech_ctx, "channel", None), "position_pct", None)),
                        "lower_line": dashboard_technical_line_payload(getattr(getattr(tech_ctx, "channel", None), "lower_line", None)),
                        "upper_line": dashboard_technical_line_payload(getattr(getattr(tech_ctx, "channel", None), "upper_line", None)),
                        "mid_line": dashboard_technical_line_payload(getattr(getattr(tech_ctx, "channel", None), "mid_line", None)),
                    },
                    "support_trendline": dashboard_technical_line_payload(getattr(tech_ctx, "support_trendline", None)),
                    "resistance_trendline": dashboard_technical_line_payload(getattr(tech_ctx, "resistance_trendline", None)),
                    "trendline_break_up": bool(getattr(tech_ctx, "trendline_break_up", False)),
                    "trendline_break_down": bool(getattr(tech_ctx, "trendline_break_down", False)),
                    "support_respected": bool(getattr(tech_ctx, "support_respected", False)),
                    "resistance_respected": bool(getattr(tech_ctx, "resistance_respected", False)),
                }

        asset_type = str((position_row or {}).get("asset_type") or "").upper().strip()
        is_option = asset_type.startswith("OPTION")
        allows_underlying_markers = bool(position_row) and not is_option
        position_markers = {
            "asset_type": asset_type or None,
            "show_underlying_lines": allows_underlying_markers,
            "side": (position_row or {}).get("side"),
            "entry": safe_float((position_row or {}).get("entry_price")) if allows_underlying_markers else None,
            "stop": safe_float((position_row or {}).get("stop_price")) if allows_underlying_markers else None,
            "target": safe_float((position_row or {}).get("target_price")) if allows_underlying_markers else None,
            # Breakeven is in underlying-price units for both stocks (from entry)
            # and options (via metadata['breakeven_underlying']), so it's safe to
            # draw on the underlying chart regardless of asset_type.
            "breakeven": safe_float((position_row or {}).get("breakeven")),
            "entry_time": (position_row or {}).get("entry_time"),
            # Option-specific: strikes in underlying-price units. Drawn on the
            # underlying chart when asset_type starts with OPTION_ because the
            # bot's stop_price/target_price are in OPTION-price units and can't
            # be plotted on the underlying's axis.
            "option_type": (position_row or {}).get("option_type") if is_option else None,
            "long_strike": safe_float((position_row or {}).get("long_strike")) if is_option else None,
            "short_strike": safe_float((position_row or {}).get("short_strike")) if is_option else None,
            "option_strike": safe_float((position_row or {}).get("option_strike")) if is_option else None,
        }

        zone_support_prices = [nearest_support] if nearest_support not in (None, 0.0) else []
        zone_resistance_prices = [nearest_resistance] if nearest_resistance not in (None, 0.0) else []
        key_level_zones = self.strategy_level_zones(
            symbol,
            frame,
            current_price,
            support_prices=zone_support_prices,
            resistance_prices=zone_resistance_prices,
            broken_support_price=safe_float((sr_row or {}).get("broken_support")),
            broken_resistance_price=safe_float((sr_row or {}).get("broken_resistance")),
            pending_support_price=safe_float((sr_row or {}).get("pending_support")),
            pending_resistance_price=safe_float((sr_row or {}).get("pending_resistance")),
            allow_htf_refresh=allow_refresh,
        )
        htf_fair_value_gaps: list[dict[str, Any]] = []
        compact_chart_profile = self.chart_profile("compact")
        expanded_chart_profile = self.chart_profile("expanded")
        # Hoisted HTF context — built once if any consumer needs it (FVG
        # rendering, divergence trendlines). Kept outside the FVG try/except
        # so the divergence block below can read it without re-fetching.
        htf_ctx = None
        chart_wants_rsi_div = bool(compact_chart_profile.show_rsi_divergence) or bool(expanded_chart_profile.show_rsi_divergence)
        try:
            sr_cfg = getattr(self.config, "support_resistance", None)
            include_fair_value_gaps = bool(getattr(sr_cfg, "htf_fair_value_gaps_enabled", True)) if sr_cfg is not None else True
            chart_wants_htf_fvgs = bool(compact_chart_profile.show_htf_fair_value_gaps) or bool(expanded_chart_profile.show_htf_fair_value_gaps)
            need_htf_ctx = (include_fair_value_gaps and chart_wants_htf_fvgs) or chart_wants_rsi_div
            if need_htf_ctx and self.data is not None:
                htf_ctx = self.data.get_htf_context(
                    symbol,
                    timeframe_minutes=self._active_htf_minutes(),
                    lookback_days=self._active_htf_lookback_days(),
                    **self._chart_htf_level_request(),
                    allow_refresh=allow_refresh,
                    use_prior_day_high_low=bool(getattr(self.config.support_resistance, "use_prior_day_high_low", True)),
                    use_prior_week_high_low=bool(getattr(self.config.support_resistance, "use_prior_week_high_low", True)),
                    include_fair_value_gaps=include_fair_value_gaps,
                    fair_value_gap_max_per_side=int(getattr(self.config.support_resistance, "fair_value_gap_max_per_side", 4) or 4),
                    fair_value_gap_min_atr_mult=float(getattr(self.config.support_resistance, "fair_value_gap_min_atr_mult", 0.05) or 0.05),
                    fair_value_gap_min_pct=float(getattr(self.config.support_resistance, "fair_value_gap_min_pct", 0.0005) or 0.0005),
                )
                if include_fair_value_gaps and chart_wants_htf_fvgs and htf_ctx is not None:
                    htf_min = self._active_htf_minutes()
                    htf_tf_minutes = int(getattr(htf_ctx, "timeframe_minutes", htf_min) or htf_min)
                    for gap in list(getattr(htf_ctx, "bullish_fvgs", []) or []) + list(getattr(htf_ctx, "bearish_fvgs", []) or []):
                        payload_fvg = dashboard_fvg_payload(gap)
                        if payload_fvg is not None:
                            payload_fvg["timeframe"] = f"{htf_tf_minutes}m"
                            htf_fair_value_gaps.append(payload_fvg)
        except Exception:
            htf_fair_value_gaps = []
            htf_ctx = None

        ltf_fair_value_gaps: list[dict[str, Any]] = []
        try:
            sr_cfg = getattr(self.config, "support_resistance", None)
            include_ltf_fvgs = bool(getattr(sr_cfg, "ltf_fair_value_gaps_enabled", False)) if sr_cfg is not None else False
            chart_wants_ltf_fvgs = bool(compact_chart_profile.show_ltf_fair_value_gaps) or bool(expanded_chart_profile.show_ltf_fair_value_gaps)
            if include_ltf_fvgs and chart_wants_ltf_fvgs and self.data is not None:
                ltf_min_for_fvg = self._active_ltf_minutes()
                fvg_ctx = self.data.get_fair_value_gap_context(
                    symbol,
                    timeframe_minutes=ltf_min_for_fvg,
                    current_price=current_price,
                    max_per_side=int(getattr(self.config.support_resistance, "fair_value_gap_max_per_side", 4) or 4),
                    min_gap_atr_mult=float(getattr(self.config.support_resistance, "fair_value_gap_min_atr_mult", 0.05) or 0.05),
                    min_gap_pct=float(getattr(self.config.support_resistance, "fair_value_gap_min_pct", 0.0005) or 0.0005),
                )
                if fvg_ctx is not None:
                    if ltf_min_for_fvg == 1:
                        anchor_frame = frame if frame is not None and not frame.empty else self.data.get_merged(symbol, with_indicators=True)
                    else:
                        anchor_frame = self.data.get_merged(symbol, timeframe=f"{ltf_min_for_fvg}min", with_indicators=True)
                    for gap in list(getattr(fvg_ctx, "bullish_fvgs", []) or []) + list(getattr(fvg_ctx, "bearish_fvgs", []) or []):
                        payload_fvg = dashboard_fvg_payload(gap)
                        if payload_fvg is not None:
                            payload_fvg["timeframe"] = f"{ltf_min_for_fvg}m"
                            payload_fvg["anchor_abs_index"] = dashboard_fvg_anchor_abs_index(anchor_frame, payload_fvg.get("first_seen"))
                            ltf_fair_value_gaps.append(payload_fvg)
        except Exception:
            ltf_fair_value_gaps = []

        # Order blocks. Same payload shape as FVGs (lower/upper/midpoint/size/
        # direction/filled_pct/first_seen/last_seen) — `dashboard_fvg_payload`
        # is reused since it's shape-driven, not type-driven. Frontend reads
        # `htf_order_blocks` and `ltf_order_blocks` separately and renders
        # them with dashed-stroke styling vs FVGs' solid-fill styling.
        # Pull tuning knobs once for both blocks below.
        sr_cfg = getattr(self.config, "support_resistance", None)
        ob_kwargs = dict(
            mode=str(getattr(sr_cfg, "order_block_mode", "loose") or "loose"),
            max_per_side=int(getattr(sr_cfg, "order_block_max_per_side", 4) or 4),
            min_block_atr_mult=float(getattr(sr_cfg, "order_block_min_atr_mult", 0.05) or 0.05),
            min_block_pct=float(getattr(sr_cfg, "order_block_min_pct", 0.0005) or 0.0005),
            min_thrust_atr_mult=float(getattr(sr_cfg, "order_block_min_thrust_atr_mult", 0.75) or 0.75),
            pivot_span=int(getattr(sr_cfg, "order_block_pivot_span", 2) or 2),
            new_high_lookback=int(getattr(sr_cfg, "order_block_new_high_lookback", 8) or 8),
        ) if sr_cfg is not None else None

        htf_order_blocks: list[dict[str, Any]] = []
        try:
            include_htf_obs = bool(getattr(sr_cfg, "htf_order_blocks_enabled", False)) if sr_cfg is not None else False
            chart_wants_htf_obs = bool(compact_chart_profile.show_htf_order_blocks) or bool(expanded_chart_profile.show_htf_order_blocks)
            if include_htf_obs and chart_wants_htf_obs and self.data is not None and ob_kwargs is not None:
                htf_minutes = self._active_htf_minutes()
                # Cycle-cached: hits get_order_block_context's cache when the
                # strategy already computed it earlier in the same cycle.
                ob_ctx_htf = self.data.get_order_block_context(
                    symbol,
                    timeframe_minutes=htf_minutes,
                    current_price=current_price,
                    **ob_kwargs,
                )
                for ob in list(getattr(ob_ctx_htf, "bullish_obs", []) or []) + list(getattr(ob_ctx_htf, "bearish_obs", []) or []):
                    payload_ob = dashboard_fvg_payload(ob)
                    if payload_ob is not None:
                        payload_ob["timeframe"] = f"{int(htf_minutes)}m"
                        payload_ob["kind"] = "ob"
                        payload_ob["mode"] = str(getattr(ob_ctx_htf, "mode", "loose") or "loose")
                        htf_order_blocks.append(payload_ob)
        except Exception:
            self.log_component_failure(
                "htf_order_blocks_collect",
                "Dashboard HTF order blocks collect failed for %s",
                symbol,
            )
            htf_order_blocks = []

        ltf_order_blocks: list[dict[str, Any]] = []
        try:
            include_ltf_obs = bool(getattr(sr_cfg, "ltf_order_blocks_enabled", False)) if sr_cfg is not None else False
            chart_wants_ltf_obs = bool(compact_chart_profile.show_ltf_order_blocks) or bool(expanded_chart_profile.show_ltf_order_blocks)
            if include_ltf_obs and chart_wants_ltf_obs and self.data is not None and ob_kwargs is not None:
                # Cycle-cached: same cache as the strategy uses when it calls
                # `_ltf_order_block_context` during entry evaluation.
                ltf_min_for_ob = self._active_ltf_minutes()
                ob_ctx_ltf = self.data.get_order_block_context(
                    symbol,
                    timeframe_minutes=ltf_min_for_ob,
                    current_price=current_price,
                    **ob_kwargs,
                )
                # We still need an in-scope LTF frame for the anchor_abs_index
                # lookup that drives chart placement; the OB context alone
                # doesn't carry frame indices.
                if ltf_min_for_ob == 1:
                    ltf_frame = frame if frame is not None and not frame.empty else self.data.get_merged(symbol, with_indicators=True)
                else:
                    ltf_frame = self.data.get_merged(symbol, timeframe=f"{ltf_min_for_ob}min", with_indicators=True)
                for ob in list(getattr(ob_ctx_ltf, "bullish_obs", []) or []) + list(getattr(ob_ctx_ltf, "bearish_obs", []) or []):
                    payload_ob = dashboard_fvg_payload(ob)
                    if payload_ob is not None:
                        payload_ob["timeframe"] = f"{ltf_min_for_ob}m"
                        payload_ob["kind"] = "ob"
                        payload_ob["mode"] = str(getattr(ob_ctx_ltf, "mode", "loose") or "loose")
                        payload_ob["anchor_abs_index"] = dashboard_fvg_anchor_abs_index(ltf_frame, payload_ob.get("first_seen"))
                        ltf_order_blocks.append(payload_ob)
        except Exception:
            self.log_component_failure(
                "ltf_order_blocks_collect",
                "Dashboard LTF order blocks collect failed for %s",
                symbol,
            )
            ltf_order_blocks = []

        # Divergence trendlines (RSI / OBV, regular / hidden, bullish / bearish)
        # for the price chart. LTF divergences come from tech_ctx (built off
        # the strategy's primary frame), HTF divergences come from htf_ctx
        # (hoisted above; populated when divergence rendering or FVG rendering
        # is enabled). Each entry is a DivergenceMatch.to_payload() dict —
        # frontend draws a line connecting the two pivot points and color-
        # codes by direction (green=bullish/red=bearish), kind (solid=regular,
        # dashed=hidden), indicator (RSI heavier stroke than OBV).
        ltf_divergence_lines: list[dict[str, Any]] = []
        htf_divergence_lines: list[dict[str, Any]] = []
        try:
            chart_wants_obv_div = bool(compact_chart_profile.show_obv_divergence) or bool(expanded_chart_profile.show_obv_divergence)
            if tech_ctx is not None and (chart_wants_rsi_div or chart_wants_obv_div):
                for attr_name in (
                    "bullish_rsi_divergence", "bearish_rsi_divergence",
                    "bullish_hidden_rsi_divergence", "bearish_hidden_rsi_divergence",
                    "bullish_obv_divergence", "bearish_obv_divergence",
                    "bullish_hidden_obv_divergence", "bearish_hidden_obv_divergence",
                ):
                    match = getattr(tech_ctx, attr_name, None)
                    if match is None:
                        continue
                    indicator = getattr(match, "indicator", "rsi")
                    if indicator == "rsi" and not chart_wants_rsi_div:
                        continue
                    if indicator == "obv" and not chart_wants_obv_div:
                        continue
                    line = match.to_payload()
                    line["timeframe"] = "ltf"
                    ltf_divergence_lines.append(line)
            # HTF divergence lines — only RSI is computed at HTF level
            # (build_htf_context populates the four HTF RSI fields; OBV
            # divergence is intentionally LTF-only since OBV is volume-driven
            # and HTF resampling smears the signal).
            if htf_ctx is not None and chart_wants_rsi_div:
                for attr_name in (
                    "bullish_rsi_divergence", "bearish_rsi_divergence",
                    "bullish_hidden_rsi_divergence", "bearish_hidden_rsi_divergence",
                ):
                    match = getattr(htf_ctx, attr_name, None)
                    if match is None:
                        continue
                    line = match.to_payload()
                    line["timeframe"] = "htf"
                    htf_divergence_lines.append(line)
        except Exception:
            self.log_component_failure(
                "divergence_lines_collect",
                "Dashboard divergence-line collect failed for %s",
                symbol,
            )
            ltf_divergence_lines = []
            htf_divergence_lines = []

        chart_payload = {
            "levels": {
                "nearest_support": nearest_support,
                "nearest_resistance": nearest_resistance,
                "support_distance_pct": safe_float((sr_row or {}).get("support_distance_pct")),
                "resistance_distance_pct": safe_float((sr_row or {}).get("resistance_distance_pct")),
                "supports": support_prices,
                "resistances": resistance_prices,
                "next_support": next_support,
                "next_resistance": next_resistance,
                "broken_support": safe_float((sr_row or {}).get("broken_support")),
                "broken_resistance": safe_float((sr_row or {}).get("broken_resistance")),
                "pending_support": safe_float((sr_row or {}).get("pending_support")),
                "pending_resistance": safe_float((sr_row or {}).get("pending_resistance")),
                "key_level_zones": key_level_zones,
                "htf_fair_value_gaps": htf_fair_value_gaps,
                "ltf_fair_value_gaps": ltf_fair_value_gaps,
                "htf_order_blocks": htf_order_blocks,
                "ltf_order_blocks": ltf_order_blocks,
                "ltf_divergence_lines": ltf_divergence_lines,
                "htf_divergence_lines": htf_divergence_lines,
            },
            "technicals": technical_payload,
            "position_markers": position_markers,
            "recent_trades": dashboard_recent_trade_markers(self.account, symbol),
            # Spans of the snapshot bars' ema9 / ema20, for labelling them
            # before (or without) a chart payload.
            "ema_fast_span": snapshot_ema_spans[0],
            "ema_slow_span": snapshot_ema_spans[1],
        }

        payload = {
            "symbol": symbol,
            "exchange": (
                dashboard_normalize_exchange(exchange)
                or dashboard_normalize_exchange((candidate_row or {}).get("exchange"))
                or dashboard_quote_exchange(quote)
            ),
            "description": quote.get("description"),
            "quote": {
                "last": last_price,
                "bid": quote_bid,
                "ask": quote_ask,
                "mid": display_mid,
                "mark": display_mark,
                "open": quote_open,
                "close": display_close,
                "net_change": net_change,
                "percent_change": percent_change,
                "total_volume": display_total_volume,
                "is_fresh": quote_is_fresh,
                "age_seconds": self.data.quote_age_seconds(symbol) if quote else None,
            },
            "candidate": copy.deepcopy(candidate_row) if candidate_row else None,
            "entry_decision": copy.deepcopy(entry_decision) if entry_decision else None,
            "warmup": copy.deepcopy(warmup) if warmup else None,
            "position": copy.deepcopy(position_row) if position_row else None,
            "support_resistance": copy.deepcopy(sr_row) if sr_row else None,
            "bars": bars,
            "chart": chart_payload,
        }
        with self.lock:
            self.snapshot_cache[symbol] = {"signature": snapshot_signature, "payload": copy.deepcopy(payload)}
        return payload

    def strategy_level_zones(
        self,
        symbol: str,
        frame: pd.DataFrame | None,
        current_price: float | None,
        support_prices: list[float] | None = None,
        resistance_prices: list[float] | None = None,
        broken_support_price: float | None = None,
        broken_resistance_price: float | None = None,
        pending_support_price: float | None = None,
        pending_resistance_price: float | None = None,
        allow_htf_refresh: bool = True,
    ) -> list[dict[str, Any]]:
        """Build strategy-specific dashboard level zones (support + resistance
        with flip confirmation, score, selection). Extracted from IntradayBot."""
        strategy_obj = self.strategy
        if strategy_obj is None or self.data is None:
            return []
        try:
            level_ctx = strategy_obj.dashboard_level_context_spec() or {}
        except Exception:
            # Reported, not replaced by the generic 60m / 60-day build below:
            # that build refreshes a key nothing else keeps, so every symbol
            # fetched from Schwab each hour on a spec error.
            self.log_component_failure("level_context_spec", "Level-context spec failed for %s", symbol)
            return []
        if not isinstance(level_ctx, dict):
            level_ctx = {}

        # Generic-fallback anchors from the S/R row, each tagged with its role
        # and the S/R builder's own verdict on its flip: the nearest levels
        # hold their role, broken_* flipped on the builder's trading-mode
        # confirmation, pending_* have been crossed with the flip still
        # unconfirmed (they keep their original role). Until 2026-09-23 every
        # support anchor was tagged nearest_htf_support, so a confirmed
        # breakout-retest level drew as an ordinary "HS · Original" support,
        # and pending levels were not drawn at all. A flipped or pending level
        # is listed ahead of a plain one at the same price, which it labels
        # more precisely.
        def _anchors(entries: list[tuple[float | None, str, bool]]) -> list[tuple[float, str, bool]]:
            deduped: list[tuple[float, str, bool]] = []
            seen: set[float] = set()
            for price, kind_name, flip_confirmed in entries:
                value = safe_float(price)
                if value is None or round(value, 4) <= 0 or round(value, 4) in seen:
                    continue
                seen.add(round(value, 4))
                deduped.append((value, kind_name, flip_confirmed))
            return deduped

        support_anchors = _anchors([
            (broken_resistance_price, "broken_htf_resistance", True),
            (pending_support_price, "pending_htf_support", False),
            *((price, "nearest_htf_support", False) for price in (support_prices or [])),
        ])
        resistance_anchors = _anchors([
            (broken_support_price, "broken_htf_support", True),
            (pending_resistance_price, "pending_htf_resistance", False),
            *((price, "nearest_htf_resistance", False) for price in (resistance_prices or [])),
        ])

        close = safe_float(current_price)
        if close is None and frame is not None and not frame.empty:
            close = safe_float(frame.iloc[-1].get("close"))
        if close is None or close <= 0:
            return []

        tf = max(1, int(level_ctx.get("timeframe_minutes", 60) or 60))
        lookback_days = max(1, int(level_ctx.get("lookback_days", 60) or 60))
        pivot_span = max(1, int(level_ctx.get("pivot_span", 2) or 2))
        max_lvls = max(1, int(level_ctx.get("max_levels_per_side", 6) or 6))
        atr_tol = float(level_ctx.get("atr_tolerance_mult", 0.35) or 0.35)
        pct_tol = float(level_ctx.get("pct_tolerance", 0.0030) or 0.0030)
        stop_atr = float(level_ctx.get("stop_buffer_atr_mult", 0.25) or 0.25)
        ema_fast_span = max(1, int(level_ctx.get("ema_fast_span", 50) or 50))
        ema_slow_span = max(1, int(level_ctx.get("ema_slow_span", 200) or 200))
        sr_cfg = getattr(self.config, "support_resistance", None)
        use_prior_day_high_low = bool(getattr(sr_cfg, "use_prior_day_high_low", True)) if sr_cfg is not None else True
        use_prior_week_high_low = bool(getattr(sr_cfg, "use_prior_week_high_low", True)) if sr_cfg is not None else True
        include_fair_value_gaps = bool(getattr(sr_cfg, "htf_fair_value_gaps_enabled", True)) if sr_cfg is not None else True
        fair_value_gap_max_per_side = int(getattr(sr_cfg, "fair_value_gap_max_per_side", 4) or 4) if sr_cfg is not None else 4
        fair_value_gap_min_atr_mult = float(getattr(sr_cfg, "fair_value_gap_min_atr_mult", 0.05) or 0.05) if sr_cfg is not None else 0.05
        fair_value_gap_min_pct = float(getattr(sr_cfg, "fair_value_gap_min_pct", 0.0005) or 0.0005) if sr_cfg is not None else 0.0005

        htf = self.data.get_htf_context(
            symbol,
            timeframe_minutes=tf,
            lookback_days=lookback_days,
            pivot_span=pivot_span,
            max_levels_per_side=max_lvls,
            atr_tolerance_mult=atr_tol,
            pct_tolerance=pct_tol,
            stop_buffer_atr_mult=stop_atr,
            ema_fast_span=ema_fast_span,
            ema_slow_span=ema_slow_span,
            allow_refresh=allow_htf_refresh,
            use_prior_day_high_low=use_prior_day_high_low,
            use_prior_week_high_low=use_prior_week_high_low,
            include_fair_value_gaps=include_fair_value_gaps,
            fair_value_gap_max_per_side=fair_value_gap_max_per_side,
            fair_value_gap_min_atr_mult=fair_value_gap_min_atr_mult,
            fair_value_gap_min_pct=fair_value_gap_min_pct,
        )
        if htf is None:
            return []

        ltf_min = max(1, int(level_ctx.get("ltf_minutes", 5) or 5))
        ltf = None
        try:
            if self.data is not None:
                timeframe = "1min" if ltf_min <= 1 else f"{ltf_min}min"
                ltf = self.data.get_merged(symbol, timeframe=timeframe, with_indicators=True)
            elif frame is not None and not frame.empty:
                if ltf_min <= 1:
                    ltf = frame.copy()
                else:
                    ltf = resample_bars(frame, f"{ltf_min}min")
        except Exception:
            ltf = None

        atr = None
        try:
            if ltf is not None and not ltf.empty:
                atr = safe_float(ltf.iloc[-1].get("atr14"))
        except Exception:
            atr = None
        if atr is None:
            atr = safe_float(getattr(htf, "atr14", None))
        if atr is None or atr <= 0:
            atr = max(float(close) * 0.0015, 0.01)
        min_level_score = float(level_ctx.get("min_level_score", 4.0) or 4.0)
        tolerance_pct = float(level_ctx.get("level_round_number_tolerance_pct", 0.0020) or 0.0020)
        base_zone_half_width = max(
            float(level_ctx.get("base_zone_atr_mult", 0.20) or 0.20) * float(atr),
            float(close) * float(level_ctx.get("base_zone_pct", 0.0015) or 0.0015),
            0.01,
        )

        long_candidates: list[dict[str, Any]] = []
        short_candidates: list[dict[str, Any]] = []
        selected_long_price = None
        selected_short_price = None
        selected_zone_match_tolerance = max(float(base_zone_half_width) * 0.75, float(close) * float(tolerance_pct) * 0.5, 0.01)

        try:
            if strategy_obj is not None:
                if ltf is not None and not ltf.empty:
                    overlay_long = strategy_obj.dashboard_overlay_candidates(Side.LONG, float(close), ltf, htf)
                    overlay_short = strategy_obj.dashboard_overlay_candidates(Side.SHORT, float(close), ltf, htf)
                    if overlay_long is not None:
                        long_candidates = list(overlay_long or [])
                    else:
                        long_candidates = list(strategy_obj.dashboard_candidate_levels(float(close), htf, Side.LONG) or [])
                    if overlay_short is not None:
                        short_candidates = list(overlay_short or [])
                    else:
                        short_candidates = list(strategy_obj.dashboard_candidate_levels(float(close), htf, Side.SHORT) or [])
                    selected_long = strategy_obj.dashboard_select_level(Side.LONG, float(close), ltf, htf)
                    selected_short = strategy_obj.dashboard_select_level(Side.SHORT, float(close), ltf, htf)
                    selected_long_price = safe_float((selected_long or {}).get("price")) if isinstance(selected_long, dict) else None
                    selected_short_price = safe_float((selected_short or {}).get("price")) if isinstance(selected_short, dict) else None
                else:
                    long_candidates = list(strategy_obj.dashboard_candidate_levels(float(close), htf, Side.LONG) or [])
                    short_candidates = list(strategy_obj.dashboard_candidate_levels(float(close), htf, Side.SHORT) or [])
        except Exception:
            long_candidates = []
            short_candidates = []
            selected_long_price = None
            selected_short_price = None

        allow_level_fallback = bool(getattr(strategy_obj, "dashboard_allow_generic_level_fallback", lambda: False)())
        if allow_level_fallback and not long_candidates and support_anchors:
            long_candidates = [
                {"kind": kind_name, "price": price, "touches": 1, "level_score": 0.0, "source_priority": 0.0, "builder_flip_confirmed": flip_confirmed}
                for price, kind_name, flip_confirmed in support_anchors
            ]
        if allow_level_fallback and not short_candidates and resistance_anchors:
            short_candidates = [
                {"kind": kind_name, "price": price, "touches": 1, "level_score": 0.0, "source_priority": 0.0, "builder_flip_confirmed": flip_confirmed}
                for price, kind_name, flip_confirmed in resistance_anchors
            ]

        def _candidate_zone_payload(side: Side, candidate: dict[str, Any]) -> dict[str, Any] | None:
            price = safe_float(candidate.get("price"))
            if price is None or price <= 0:
                return None
            zone_kind = "support" if side == Side.LONG else "resistance"
            try:
                if strategy_obj is not None:
                    zone_width_override = strategy_obj.dashboard_zone_width_for_level(side, float(close), float(atr), float(price), htf, candidate)
                    zone_half_width = float(zone_width_override) if zone_width_override is not None else float(base_zone_half_width)
                else:
                    zone_half_width = float(base_zone_half_width)
            except Exception:
                zone_half_width = float(base_zone_half_width)
            zone_half_width = max(float(zone_half_width), 0.01)
            raw_lower = safe_float(candidate.get("zone_lower"))
            raw_upper = safe_float(candidate.get("zone_upper"))
            if raw_lower is not None and raw_upper is not None and raw_upper >= raw_lower:
                zone_lower = float(raw_lower)
                zone_upper = float(raw_upper)
                zone_half_width = max(float(zone_half_width), (zone_upper - zone_lower) / 2.0)
            else:
                zone_lower = max(0.0, float(price) - float(zone_half_width))
                zone_upper = float(price) + float(zone_half_width)
            kind_name = str(candidate.get("kind") or "").strip()
            selected_anchor_price = selected_long_price if side == Side.LONG else selected_short_price
            return {
                "kind": zone_kind,
                "price": float(price),
                "lower": float(zone_lower),
                "upper": float(zone_upper),
                "score": float(candidate.get("level_score", 0.0) or 0.0),
                "touches": int(candidate.get("touches", 1) or 1),
                "labels": [strategy_obj.dashboard_candidate_label(kind_name, zone_kind)],
                "sources": strategy_obj.dashboard_candidate_sources(kind_name, zone_kind),
                "timeframe": f"{tf}m",
                "zone_half_width": float(zone_half_width),
                "engine_level_kind": kind_name or None,
                "engine_source_priority": float(candidate.get("source_priority", 0.0) or 0.0),
                "engine_level_score": float(candidate.get("level_score", 0.0) or 0.0),
                "passes_min_level_score": bool(float(candidate.get("level_score", 0.0) or 0.0) >= float(min_level_score)),
                "selected_for_entry": bool(selected_anchor_price is not None and abs(float(price) - float(selected_anchor_price)) <= float(selected_zone_match_tolerance)),
                # The S/R builder's verdict on a generic-fallback level's flip;
                # absent on a strategy's own candidates, whose flips the zone
                # check below decides.
                "builder_flip_confirmed": candidate.get("builder_flip_confirmed"),
            }

        support_zones = [zone for zone in (_candidate_zone_payload(Side.LONG, candidate) for candidate in long_candidates) if zone is not None]
        resistance_zones = [zone for zone in (_candidate_zone_payload(Side.SHORT, candidate) for candidate in short_candidates) if zone is not None]

        # Use trading-mode flip confirmation (flip_confirmation_bars) so the
        # chart's zone classification matches what position management and
        # strategy entries see. The previous code used loose dashboard mode
        # (1m_bars=1, 5m_bars=0) for snappier visual feedback, but that meant
        # a chart zone could flip color before the strategy itself treated it
        # as flipped — confusing when the dashboard sidebar (which already
        # uses trading mode via `sr_row()`) and the chart disagreed about
        # the same level.
        zone_flip_1m, zone_flip_5m = flip_confirmation_bars(self.config.support_resistance)
        fallback_bar = None
        if frame is not None and not frame.empty:
            try:
                last_bar = frame.iloc[-1]
                fallback_bar = (float(last_bar.get("high")), float(last_bar.get("low")))
            except Exception:
                fallback_bar = None
        zone_eps = max(abs(float(close)) * 1e-6, 1e-8)

        def _zone_level_kind(zone: dict[str, Any]) -> str:
            return str(zone.get("engine_level_kind", "") or "").strip().lower()

        def _is_fvg_zone(zone: dict[str, Any]) -> bool:
            kind_name = _zone_level_kind(zone)
            return kind_name in {"bullish_htf_fvg", "bearish_htf_fvg"} or "fvg" in kind_name

        def _zone_original_kind(zone: dict[str, Any]) -> str | None:
            kind_name = _zone_level_kind(zone)
            if not kind_name or _is_fvg_zone(zone):
                return None
            if kind_name in {"broken_htf_support", "pending_htf_support"}:
                return "support"
            if kind_name in {"broken_htf_resistance", "pending_htf_resistance"}:
                return "resistance"
            if kind_name in {"prior_day_low", "prior_week_low"} or kind_name.endswith("_low"):
                return "support"
            if kind_name in {"prior_day_high", "prior_week_high"} or kind_name.endswith("_high"):
                return "resistance"
            if "support" in kind_name and "resistance" not in kind_name:
                return "support"
            if "resistance" in kind_name and "support" not in kind_name:
                return "resistance"
            return None

        def _zone_flipped_kind(kind_name: str | None) -> str | None:
            if kind_name == "support":
                return "resistance"
            if kind_name == "resistance":
                return "support"
            return None

        def _apply_zone_confirmation_state(zone: dict[str, Any]) -> dict[str, Any]:
            original_kind = _zone_original_kind(zone)
            if original_kind is None:
                return zone
            flipped_kind = _zone_flipped_kind(original_kind)
            if flipped_kind is None:
                return zone
            level_kind = _zone_level_kind(zone)
            builder_verdict = zone.get("builder_flip_confirmed")
            if builder_verdict is None:
                confirmed = zone_flip_confirmed(
                    original_kind,
                    float(zone.get("lower", 0.0) or 0.0),
                    float(zone.get("upper", 0.0) or 0.0),
                    flip_frame=frame,
                    confirm_1m_bars=zone_flip_1m,
                    confirm_5m_bars=zone_flip_5m,
                    fallback_bar=fallback_bar,
                    eps=zone_eps,
                )
            else:
                # The builder confirmed (broken_*) or has yet to confirm
                # (pending_*, nearest) this flip on the level price; the
                # zone-edge check above answers a different question and
                # could relabel a confirmed breakout-retest level as pending.
                confirmed = bool(builder_verdict)
            sources = list(zone.get("sources", []) or [])
            zone["original_kind"] = str(original_kind)
            zone["confirmed_flip"] = False
            zone["flip_state"] = "original"
            zone["pending_flip"] = False
            zone["pending_state"] = ""
            zone["flip_target_kind"] = ""
            if level_kind in _FLIP_CANDIDATE_LEVEL_KINDS:
                if confirmed:
                    zone["kind"] = flipped_kind
                    zone["confirmed_flip"] = True
                    zone["flip_state"] = "confirmed_flip"
                    zone["sources"] = list(dict.fromkeys([*sources, f"confirmed_broken_{original_kind}_zone"]))
                else:
                    zone["kind"] = original_kind
                    zone["flip_state"] = "pending_flip"
                    zone["pending_flip"] = True
                    zone["pending_state"] = "pending_break" if original_kind == "support" else "pending_reclaim"
                    zone["flip_target_kind"] = flipped_kind
                    zone["sources"] = list(dict.fromkeys([*sources, f"pending_broken_{original_kind}"]))
                return zone
            if confirmed:
                zone["kind"] = flipped_kind
                zone["confirmed_flip"] = True
                zone["flip_state"] = "confirmed_flip"
                zone["sources"] = list(dict.fromkeys([*sources, f"confirmed_flipped_{original_kind}_zone"]))
            else:
                zone["kind"] = original_kind
                zone["sources"] = list(dict.fromkeys(sources))
            return zone

        all_zones = [_apply_zone_confirmation_state(zone) for zone in (support_zones + resistance_zones)]

        def _zone_rank_key(zone: dict[str, Any]) -> tuple[float, ...]:
            level_kind = _zone_level_kind(zone)
            return (
                1.0 if bool(zone.get("selected_for_entry", False)) else 0.0,
                1.0 if not bool(zone.get("pending_flip", False)) else 0.0,
                1.0 if level_kind in _FLIP_CANDIDATE_LEVEL_KINDS else 0.0,
                float(zone.get("engine_level_score", 0.0) or 0.0),
                float(zone.get("score", 0.0) or 0.0),
                float(int(zone.get("touches", 0) or 0)),
            )

        def _collapse_duplicate_zones(zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
            collapsed: dict[tuple[str, float], dict[str, Any]] = {}
            for zone in zones:
                try:
                    key = (str(zone.get("kind", "") or ""), round(float(zone.get("price", 0.0) or 0.0), 6))
                except Exception:
                    continue
                existing = collapsed.get(key)
                if existing is None:
                    collapsed[key] = zone
                    continue
                existing_key = _zone_rank_key(existing)
                zone_key = _zone_rank_key(zone)
                if zone_key > existing_key:
                    best, other = zone, existing
                else:
                    best, other = existing, zone
                best["labels"] = list(dict.fromkeys([*list(best.get("labels", []) or []), *list(other.get("labels", []) or [])]))
                best["sources"] = list(dict.fromkeys([*list(best.get("sources", []) or []), *list(other.get("sources", []) or [])]))
                best["selected_for_entry"] = bool(best.get("selected_for_entry", False) or other.get("selected_for_entry", False))
                collapsed[key] = best
            return list(collapsed.values())

        all_zones = _collapse_duplicate_zones(all_zones)
        support_zones = [item for item in all_zones if str(item.get("kind")) == "support"]
        resistance_zones = [item for item in all_zones if str(item.get("kind")) == "resistance"]

        # Overlapping support / resistance zones split the gap at its
        # midpoint. Only a support BELOW a resistance is such a pair: a
        # pending level is drawn in its original role on the far side of
        # price (a pending support above a nearer resistance), and trimming
        # that crossed pair collapsed both zones, the strategy's own nearest
        # level included, to zero width (2026-09-23).
        for support in support_zones:
            support_price = float(support.get("price", 0.0) or 0.0)
            for resistance in resistance_zones:
                resistance_price = float(resistance.get("price", 0.0) or 0.0)
                if support_price >= resistance_price:
                    continue
                support_upper = float(support.get("upper", 0.0) or 0.0)
                resistance_lower = float(resistance.get("lower", 0.0) or 0.0)
                if support_upper < resistance_lower:
                    continue
                midpoint = (support_price + resistance_price) / 2.0
                support_half_width = max(0.0, min(float(support.get("zone_half_width", 0.0) or 0.0), midpoint - support_price))
                resistance_half_width = max(0.0, min(float(resistance.get("zone_half_width", 0.0) or 0.0), resistance_price - midpoint))
                support["lower"] = max(0.0, support_price - support_half_width)
                support["upper"] = support_price + support_half_width
                resistance["lower"] = max(0.0, resistance_price - resistance_half_width)
                resistance["upper"] = resistance_price + resistance_half_width

        support_zones = [item for item in support_zones if float(item.get("upper", 0.0) or 0.0) >= float(item.get("price", 0.0) or 0.0)]
        resistance_zones = [item for item in resistance_zones if float(item.get("lower", 0.0) or 0.0) <= float(item.get("price", 0.0) or 0.0)]
        ordered = sorted((support_zones + resistance_zones), key=lambda item: (float(item["price"]), item["kind"]))

        def _zone_sort_key(zone: dict[str, Any]) -> tuple[float, float, float]:
            price = float(zone.get("price", 0.0) or 0.0)
            selected_delta = 0.0 if bool(zone.get("selected_for_entry", False)) else 1.0
            distance = abs(price - float(close))
            return selected_delta, distance, -float(zone.get("engine_level_score", 0.0) or 0.0)

        selected_zones = sorted([item for item in ordered if bool(item.get("selected_for_entry", False))], key=_zone_sort_key)
        display_zones: list[dict[str, Any]]
        if selected_zones:
            primary_selected = selected_zones[0]
            primary_kind = str(primary_selected.get("kind", "") or "")
            opposite_kind = "resistance" if primary_kind == "support" else "support"
            opposite_candidates = [item for item in ordered if str(item.get("kind", "") or "") == opposite_kind and not bool(item.get("selected_for_entry", False))]
            if opposite_kind == "resistance":
                above = [item for item in opposite_candidates if float(item.get("price", 0.0) or 0.0) >= float(close)]
                preferred_pool = above if above else opposite_candidates
                opposite_candidates = sorted(preferred_pool, key=lambda item: (float(item.get("price", 0.0) or 0.0), -float(item.get("engine_level_score", 0.0) or 0.0)))
            else:
                below = [item for item in opposite_candidates if float(item.get("price", 0.0) or 0.0) <= float(close)]
                preferred_pool = below if below else opposite_candidates
                opposite_candidates = sorted(preferred_pool, key=lambda item: (-float(item.get("price", 0.0) or 0.0), -float(item.get("engine_level_score", 0.0) or 0.0)))
            display_zones = [primary_selected]
            if opposite_candidates:
                display_zones.append(opposite_candidates[0])
            display_zones = sorted(display_zones, key=lambda item: (float(item["price"]), item["kind"]))
        else:
            # The nearest plain zone of each kind, plus every broken / pending
            # level as its own zone. A flipped level no longer competes with
            # the nearest one for the single support / resistance slot (until
            # 2026-09-23 the S/R row folded a broken resistance into the
            # support ladder, so the zone drawn was whichever of the two was
            # nearer, not the level the strategy reads).
            plain_zones = [item for item in ordered if _zone_level_kind(item) not in _FLIP_CANDIDATE_LEVEL_KINDS]
            nearest_support = sorted([item for item in plain_zones if str(item.get("kind", "") or "") == "support"], key=lambda item: abs(float(item.get("price", 0.0) or 0.0) - float(close)))
            nearest_resistance = sorted([item for item in plain_zones if str(item.get("kind", "") or "") == "resistance"], key=lambda item: abs(float(item.get("price", 0.0) or 0.0) - float(close)))
            display_zones = [item for item in ordered if _zone_level_kind(item) in _FLIP_CANDIDATE_LEVEL_KINDS]
            if nearest_support:
                display_zones.append(nearest_support[0])
            if nearest_resistance:
                display_zones.append(nearest_resistance[0])
            display_zones = sorted(display_zones, key=lambda item: (float(item["price"]), item["kind"]))

        return [
            {
                "kind": str(item["kind"]),
                "price": float(item["price"]),
                "lower": float(item["lower"]),
                "upper": float(item["upper"]),
                "score": float(item["score"]),
                "touches": int(item["touches"]),
                "labels": list(item["labels"]),
                "sources": list(item["sources"]),
                "timeframe": f"{tf}m",
                "zone_half_width": float(item.get("zone_half_width", 0.0) or 0.0),
                "pending_flip": bool(item.get("pending_flip", False)),
                "pending_state": str(item.get("pending_state", "") or ""),
                "flip_target_kind": str(item.get("flip_target_kind", "") or ""),
                "confirmed_flip": bool(item.get("confirmed_flip", False)),
                "flip_state": str(item.get("flip_state", "original") or "original"),
                "original_kind": str(item.get("original_kind", "") or ""),
                "engine_level_kind": item.get("engine_level_kind"),
                "engine_source_priority": float(item.get("engine_source_priority", 0.0) or 0.0),
                "engine_level_score": float(item.get("engine_level_score", 0.0) or 0.0),
                "passes_min_level_score": bool(item.get("passes_min_level_score", False)),
                "selected_for_entry": bool(item.get("selected_for_entry", False)),
            }
            for item in display_zones
        ]

    def sr_row(self, symbol: str, price: float | None = None, *, allow_refresh: bool = True) -> dict[str, Any] | None:
        """Build the support/resistance row payload for the dashboard ladder.
        Extracted from IntradayBot as part of Phase 5 Step 6."""
        cfg = getattr(self.config, "support_resistance", None)
        if cfg is None or not bool(cfg.enabled):
            return None
        current_price = price if price is not None else self.symbol_price(symbol)
        # mode="trading" gives the sidebar the same flip-confirmation
        # strictness (config.flip_confirmation_bars) that position management,
        # the chart's zone-flip detection, and the entry gatekeeper all
        # use. A single "trading" mode means the sidebar / chart /
        # gatekeeper / strategy agree on which side of a level price is
        # currently sitting on — no path where one consumer sees a level
        # as broken before another does.
        ctx = self.data.get_support_resistance(
            symbol,
            current_price=current_price,
            flip_frame=self.data.get_merged(symbol, with_indicators=False),
            mode="trading",
            timeframe_minutes=self._active_htf_minutes(),
            lookback_days=self._active_htf_lookback_days(),
            allow_refresh=allow_refresh,
        )
        if ctx is None:
            return None
        display_price: float | None = None
        try:
            candidate_price = current_price if current_price is not None else getattr(ctx, "current_price", None)
            if candidate_price is not None and float(candidate_price) > 0:
                display_price = float(candidate_price)
        except Exception:
            display_price = None
        state = "neutral"

        def _level_price(level: Any) -> float | None:
            return None if level is None else float(level.price)

        trend_row = self.htf_trend(symbol, allow_refresh=allow_refresh)
        htf_trend_bias = "neutral"
        # The strategy's own HTF trend -- the read its gates and scores use --
        # when it has one; the generic 50/200 read below only for the rest.
        own_trend = None
        own_trend_hook = getattr(self.strategy, "dashboard_htf_trend", None)
        trend_price = display_price if display_price is not None else float(getattr(ctx, "current_price", 0.0) or 0.0)
        if callable(own_trend_hook) and self.data is not None and trend_price:
            try:
                own_trend = own_trend_hook(symbol, self.data, trend_price, allow_refresh=allow_refresh)
            except Exception:
                LOG.debug("Failed to read the strategy's HTF trend for %s; using the generic read.", symbol, exc_info=True)
        try:
            if self.data is not None and own_trend is None:
                sr_cfg = getattr(self.config, "support_resistance", None)
                if sr_cfg is not None:
                    htf_ctx = self.data.get_htf_context(
                        symbol,
                        timeframe_minutes=self._active_htf_minutes(),
                        lookback_days=self._active_htf_lookback_days(),
                        pivot_span=int(getattr(sr_cfg, "pivot_span", 2) or 2),
                        max_levels_per_side=int(getattr(sr_cfg, "max_levels_per_side", 3) or 3),
                        atr_tolerance_mult=float(getattr(sr_cfg, "atr_tolerance_mult", 0.60) or 0.60),
                        pct_tolerance=float(getattr(sr_cfg, "pct_tolerance", 0.0030) or 0.0030),
                        stop_buffer_atr_mult=float(getattr(sr_cfg, "stop_buffer_atr_mult", 0.25) or 0.25),
                        ema_fast_span=50,
                        ema_slow_span=200,
                        allow_refresh=allow_refresh,
                        use_prior_day_high_low=bool(getattr(sr_cfg, "use_prior_day_high_low", True)),
                        use_prior_week_high_low=bool(getattr(sr_cfg, "use_prior_week_high_low", True)),
                        include_fair_value_gaps=bool(getattr(sr_cfg, "htf_fair_value_gaps_enabled", True)),
                        fair_value_gap_max_per_side=int(getattr(sr_cfg, "fair_value_gap_max_per_side", 4) or 4),
                        fair_value_gap_min_atr_mult=float(getattr(sr_cfg, "fair_value_gap_min_atr_mult", 0.05) or 0.05),
                        fair_value_gap_min_pct=float(getattr(sr_cfg, "fair_value_gap_min_pct", 0.0005) or 0.0005),
                    )
                    htf_trend_bias = str(getattr(htf_ctx, "trend_bias", "neutral") or "neutral").strip().lower()
        except Exception:
            LOG.debug("Failed to read HTF trend bias context for %s; falling back to summarize_htf_trend().", symbol, exc_info=True)
        trend_state = str(trend_row.get("state", "neutral") or "neutral").strip().lower()
        trend_label = str(trend_row.get("label", "—") or "—")
        if own_trend is not None:
            trend_state = str(own_trend.get("state", "neutral") or "neutral").strip().lower()
            trend_label = str(own_trend.get("label", "—") or "—")
        elif htf_trend_bias in {"bullish", "bearish"}:
            trend_state = htf_trend_bias
            trend_label = "Bullish" if htf_trend_bias == "bullish" else "Bearish"
        ms_ctx = getattr(ctx, "market_structure", None)
        structure_bias = str(getattr(ms_ctx, "bias", "neutral") or "neutral") if ms_ctx is not None else "neutral"
        structure_event = dashboard_structure_event_label(ms_ctx)

        bullish_structure = structure_bias == "bullish" or structure_event in {"BOS↑", "CHOCH↑"}
        bearish_structure = structure_bias == "bearish" or structure_event in {"BOS↓", "CHOCH↓"}
        bullish_conflict = bearish_structure or trend_state == "bearish"
        bearish_conflict = bullish_structure or trend_state == "bullish"

        if ctx.breakout_above_resistance and not bullish_conflict and (bullish_structure or trend_state == "bullish"):
            state = "breakout"
        elif ctx.breakdown_below_support and not bearish_conflict and (bearish_structure or trend_state == "bearish"):
            state = "breakdown"
        elif ctx.near_support and not ctx.near_resistance:
            state = "near_support"
        elif ctx.near_resistance and not ctx.near_support:
            state = "near_resistance"
        elif ctx.near_support and ctx.near_resistance:
            state = "compressed"
        elif ctx.breakout_above_resistance and not bullish_conflict:
            state = "breakout_watch"
        elif ctx.breakdown_below_support and not bearish_conflict:
            state = "breakdown_watch"

        htf_min_active = self._active_htf_minutes()
        timeframe_minutes = int(getattr(ctx, "timeframe_minutes", htf_min_active) or htf_min_active)
        symbol_key = str(symbol or "").upper().strip()
        htf_refresh = self.data.last_htf_refresh.get((symbol_key, timeframe_minutes)) if symbol_key else None
        ltf_min = max(1, self._active_ltf_minutes())

        return {
            "symbol": symbol,
            "timeframe": f"{timeframe_minutes}m",
            # LTF label exposed alongside HTF so the dashboard's expanded-chart
            # LTF toggle button can render "5m LTF" (or whatever ltf_minutes
            # resolves to) instead of the hardcoded "1M LTF" fallback.
            "ltf_timeframe": f"{ltf_min}m",
            "price": display_price,
            "htf_refresh_token": htf_refresh.isoformat() if htf_refresh is not None else None,
            "side_tolerance": safe_float(getattr(ctx, "side_tolerance", None)),
            # The strategy's own levels, as it reads them (2026-09-23): the
            # nearest support / resistance and their distances are ctx's, the
            # ladders are ctx's (nearest first), and broken / pending levels
            # travel in their own fields for the chart to draw as their own
            # zones. Until then the row folded broken_resistance into the
            # support ladder (broken_support into the resistance one) and
            # published the NEAREST price of the result, while ctx keeps the
            # STRONGEST member of each side_tolerance group: in 11% of
            # archived samples the sidebar and top_tier's support zone showed
            # another level than sr_ctx.nearest_support (AAPL 2026-09-22 10:05:
            # 338.58 drawn, 338.42 used by the strategy), next to a distance
            # measured to the strategy's level.
            "nearest_support": _level_price(ctx.nearest_support),
            "nearest_resistance": _level_price(ctx.nearest_resistance),
            "support_distance_pct": ctx.support_distance_pct,
            "resistance_distance_pct": ctx.resistance_distance_pct,
            "support_distance_atr": ctx.support_distance_atr,
            "resistance_distance_atr": ctx.resistance_distance_atr,
            "breakout_above_resistance": bool(ctx.breakout_above_resistance),
            "breakdown_below_support": bool(ctx.breakdown_below_support),
            "near_support": bool(ctx.near_support),
            "near_resistance": bool(ctx.near_resistance),
            "regime_hint": str(ctx.regime_hint),
            "trend": trend_label,
            "trend_state": trend_state,
            "structure_bias": structure_bias,
            "structure_event": structure_event,
            "structure_last_high_label": getattr(ms_ctx, "last_high_label", None) if ms_ctx is not None else None,
            "structure_last_low_label": getattr(ms_ctx, "last_low_label", None) if ms_ctx is not None else None,
            "bias_score": float(ctx.bias_score),
            "state": state,
            "supports": [float(level.price) for level in ctx.supports],
            "resistances": [float(level.price) for level in ctx.resistances],
            "broken_support": _level_price(ctx.broken_support),
            "broken_resistance": _level_price(ctx.broken_resistance),
            "pending_support": _level_price(ctx.pending_support),
            "pending_resistance": _level_price(ctx.pending_resistance),
        }

    def snapshot_should_bypass_cache(self, symbol: str, *, allow_refresh: bool) -> bool:
        """True when support-resistance or HTF context needs a fresh refresh.

        Callers (dashboard snapshot builders) use this to decide whether a
        cached snapshot can be returned or must be recomputed."""
        if not allow_refresh:
            return False
        try:
            sr_tf = self._active_htf_minutes()
            if self.data.should_refresh_support_resistance(symbol, timeframe_minutes=sr_tf):
                return True
            if self.data.should_refresh_htf_context(symbol, sr_tf):
                return True
        except Exception:
            return True
        return False

    def symbol_snapshot_signature(
        self,
        symbol: str,
        frame: pd.DataFrame | None,
        *,
        quote: Mapping[str, Any] | None,
        quote_is_fresh: bool,
        sr_row: Mapping[str, Any] | None,
        candidate_row: Mapping[str, Any] | None,
        position_row: Mapping[str, Any] | None,
        entry_decision: Mapping[str, Any] | None,
        warmup: Mapping[str, Any] | None,
        allow_refresh: bool,
    ) -> tuple[Any, ...]:
        """Tuple signature for the per-symbol dashboard snapshot cache. Any
        change in timestamps, quote, SR row, candidate, position, or trades
        invalidates the cached snapshot."""
        symbol_key = str(symbol or "").upper().strip()
        quote_refresh = self.data.last_quote_refresh.get(symbol_key) if symbol_key else None
        history_refresh = self.data.last_history_refresh.get(symbol_key) if symbol_key else None
        stream_refresh = self.data.last_stream_update.get(symbol_key) if symbol_key else None
        htf_refresh = self.data.last_htf_refresh.get((symbol_key, self._active_htf_minutes())) if symbol_key else None
        quote_body = quote or {}
        return (
            dashboard_frame_signature(frame),
            bool(quote_is_fresh),
            quote_refresh.isoformat() if quote_refresh is not None else None,
            history_refresh.isoformat() if history_refresh is not None else None,
            stream_refresh.isoformat() if stream_refresh is not None else None,
            htf_refresh.isoformat() if htf_refresh is not None else None,
            safe_float(quote_body.get("last")) if isinstance(quote_body, Mapping) else None,
            safe_float(quote_body.get("bid")) if isinstance(quote_body, Mapping) else None,
            safe_float(quote_body.get("ask")) if isinstance(quote_body, Mapping) else None,
            safe_float(quote_body.get("mark")) if isinstance(quote_body, Mapping) else None,
            safe_float(quote_body.get("total_volume")) if isinstance(quote_body, Mapping) else None,
            dashboard_cache_json_signature(sr_row or {}),
            dashboard_cache_json_signature(candidate_row or {}),
            dashboard_cache_json_signature(position_row or {}),
            dashboard_cache_json_signature(entry_decision or {}),
            dashboard_cache_json_signature(warmup or {}),
            dashboard_symbol_trade_signature(self.account, symbol_key),
            bool(allow_refresh),
        )

    def current_pattern_payload(self, frame: pd.DataFrame | None) -> dict[str, Any]:
        """Build the candle + chart-pattern dashboard payload from ``frame``.
        Uses strategy's ``dashboard_candle_context`` if exposed, else falls
        back to default detect_candle_context with configured pattern lists."""
        payload: dict[str, Any] = {
            "candles_bullish": [],
            "candles_bearish": [],
            "candle_bias_score": None,
            "candle_net_score": None,
            "candle_regime_hint": "neutral",
            "bullish_candle_score": 0.0,
            "bearish_candle_score": 0.0,
            "bullish_candle_net_score": 0.0,
            "bearish_candle_net_score": 0.0,
            "bullish_candle_anchor_pattern": None,
            "bearish_candle_anchor_pattern": None,
            "bullish_candle_anchor_bars": 0,
            "bearish_candle_anchor_bars": 0,
            "chart_bullish": [],
            "chart_bearish": [],
            "chart_bullish_reversal": [],
            "chart_bullish_continuation": [],
            "chart_bearish_reversal": [],
            "chart_bearish_continuation": [],
            "chart_bias_score": None,
            "chart_regime_hint": "neutral",
        }
        if frame is None or frame.empty:
            return payload
        frame_for_analysis = frame.copy()
        for col in ("open", "high", "low", "close", "volume"):
            if col in frame_for_analysis.columns:
                frame_for_analysis[col] = pd.to_numeric(frame_for_analysis[col], errors="coerce")
        frame_for_analysis = frame_for_analysis.dropna(subset=[col for col in ("open", "high", "low", "close") if col in frame_for_analysis.columns]).copy()
        if frame_for_analysis.empty:
            return payload
        try:
            candle_builder = getattr(self.strategy, "dashboard_candle_context", None)
            if callable(candle_builder):
                candle_ctx = candle_builder(frame_for_analysis)
            else:
                candle_ctx = detect_candle_context(
                    frame_for_analysis,
                    bullish_allowed=self.config.candles.bullish_patterns,
                    bearish_allowed=self.config.candles.bearish_patterns,
                )
        except Exception:
            candle_ctx = detect_candle_context(pd.DataFrame())
        payload["candles_bullish"] = list(candle_ctx.get("matched_bullish_candles", []))
        payload["candles_bearish"] = list(candle_ctx.get("matched_bearish_candles", []))
        payload["candle_bias_score"] = float(candle_ctx.get("candle_bias_score", 0.0) or 0.0)
        payload["candle_regime_hint"] = str(candle_ctx.get("candle_regime_hint", "neutral") or "neutral")
        payload["candle_net_score"] = float(candle_ctx.get("candle_net_score", payload["candle_bias_score"]) or payload["candle_bias_score"])
        payload["bullish_candle_score"] = float(candle_ctx.get("bullish_candle_score", 0.0) or 0.0)
        payload["bearish_candle_score"] = float(candle_ctx.get("bearish_candle_score", 0.0) or 0.0)
        payload["bullish_candle_net_score"] = float(candle_ctx.get("bullish_candle_net_score", 0.0) or 0.0)
        payload["bearish_candle_net_score"] = float(candle_ctx.get("bearish_candle_net_score", 0.0) or 0.0)
        payload["bullish_candle_anchor_pattern"] = candle_ctx.get("bullish_candle_anchor_pattern")
        payload["bearish_candle_anchor_pattern"] = candle_ctx.get("bearish_candle_anchor_pattern")
        payload["bullish_candle_anchor_bars"] = int(candle_ctx.get("bullish_candle_anchor_bars", 0) or 0)
        payload["bearish_candle_anchor_bars"] = int(candle_ctx.get("bearish_candle_anchor_bars", 0) or 0)
        if bool(getattr(self.config.chart_patterns, "enabled", True)):
            try:
                chart_ctx = analyze_chart_pattern_context(
                    frame_for_analysis,
                    bullish_allowed=self.config.chart_patterns.bullish_patterns,
                    bearish_allowed=self.config.chart_patterns.bearish_patterns,
                    lookback_bars=int(getattr(self.config.chart_patterns, "lookback_bars", 32) or 32),
                )
                payload["chart_bullish"] = sorted(list(getattr(chart_ctx, "matched_bullish", set()) or []))
                payload["chart_bearish"] = sorted(list(getattr(chart_ctx, "matched_bearish", set()) or []))
                payload["chart_bullish_reversal"] = sorted(list(getattr(chart_ctx, "matched_bullish_reversal", set()) or []))
                payload["chart_bullish_continuation"] = sorted(list(getattr(chart_ctx, "matched_bullish_continuation", set()) or []))
                payload["chart_bearish_reversal"] = sorted(list(getattr(chart_ctx, "matched_bearish_reversal", set()) or []))
                payload["chart_bearish_continuation"] = sorted(list(getattr(chart_ctx, "matched_bearish_continuation", set()) or []))
                payload["chart_bias_score"] = safe_float(getattr(chart_ctx, "bias_score", None))
                payload["chart_regime_hint"] = str(getattr(chart_ctx, "regime_hint", "neutral") or "neutral")
            except Exception:
                LOG.debug("Failed to attach chart-pattern payload to dashboard response; returning partial payload.", exc_info=True)
        return payload

    def current_structure_overlay(self, frame: pd.DataFrame | None, *, timeframe_minutes: int,
                                  last_bar_forming: bool = False) -> dict[str, Any]:
        """Build the market-structure overlay payload (CHOCH/BOS event, age,
        level) from ``frame`` at the given timeframe. ``last_bar_forming``:
        ``frame``'s last bar is a bucket still trading, which confirms no
        pivot (as in the strategy's ``_structure_context``).

        Calls ``analyze_market_structure`` directly instead of building a
        full ``SupportResistanceContext`` — the overlay only consumes
        ``market_structure`` and skipping the surrounding S/R clustering,
        prior-day/week, FVG checks, broken-level reconciliation, and
        proximity metrics is roughly an order-of-magnitude speedup per
        chart render. Returns neutral payload on any failure (with
        rate-limited warning via log_component_failure)."""
        payload: dict[str, Any] = {
            "event": "—",
            "age_bars": None,
            # Start timestamp of the bar the event fired on, so the chart can
            # mark it on the matching bar whatever bars it has merged since.
            "event_ts": None,
            "level": None,
            "bias": "neutral",
            "pivot_bias": "neutral",
        }
        if frame is None or frame.empty:
            return payload
        frame_for_analysis = frame.copy()
        for col in ("open", "high", "low", "close", "volume"):
            if col in frame_for_analysis.columns:
                frame_for_analysis[col] = pd.to_numeric(frame_for_analysis[col], errors="coerce")
        frame_for_analysis = frame_for_analysis.dropna(subset=[col for col in ("open", "high", "low", "close") if col in frame_for_analysis.columns]).copy()
        if frame_for_analysis.empty:
            return payload
        close_val = safe_float(frame_for_analysis["close"].iloc[-1])
        if close_val is None:
            return payload
        try:
            sr_cfg = self.config.support_resistance
            # Match the strategy's LTF-vs-HTF structure params so the overlay
            # reflects what the bot actually computes — same "keep the chart
            # faithful to the strategy" principle as the HTF-EMA override in
            # the chart payload. The LTF structure context applies
            # structure_ltf_pivot_span, a 0.60x pct_tolerance, and the
            # min-pivot-gap filter (Fix B/D, 2026-05-27); the HTF/base context
            # does not. Detect the LTF chart by matching the display timeframe
            # to the strategy's effective LTF structure timeframe
            # (structure_ltf_timeframe_minutes, falling back to
            # params.ltf_minutes). HTF / other timeframes keep the original
            # base-param behavior unchanged.
            strat_params = getattr(self.strategy, "params", {}) or {}
            ltf_struct_tf = int(getattr(sr_cfg, "structure_ltf_timeframe_minutes", 0) or 0) or int(strat_params.get("ltf_minutes", 1) or 1)
            is_ltf_chart = int(timeframe_minutes or 1) == ltf_struct_tf
            overlay_pivot_span = (
                int(getattr(sr_cfg, "structure_ltf_pivot_span", 2) or 2)
                if is_ltf_chart
                else int(getattr(sr_cfg, "pivot_span", 2) or 2)
            )
            overlay_pct_tolerance = float(getattr(sr_cfg, "pct_tolerance", 0.0030) or 0.0030)
            if is_ltf_chart:
                overlay_pct_tolerance *= 0.60
            overlay_gap_bars = (
                int(getattr(sr_cfg, "structure_min_pivot_gap_bars", 0) or 0) if is_ltf_chart else 0
            )
            ms_ctx = analyze_market_structure(
                frame_for_analysis,
                current_price=close_val,
                pivot_span=overlay_pivot_span,
                eq_atr_mult=float(getattr(sr_cfg, "structure_eq_atr_mult", 0.25) or 0.25),
                pct_tolerance=overlay_pct_tolerance,
                breakout_atr_mult=float(getattr(sr_cfg, "breakout_atr_mult", 0.35) or 0.35),
                breakout_buffer_pct=float(getattr(sr_cfg, "breakout_buffer_pct", 0.0015) or 0.0015),
                # LTF overlay counts LTF bars, HTF overlay counts HTF bars --
                # the same split the pivot gap above already makes.
                structure_event_max_age_bars=(
                    int(getattr(sr_cfg, "structure_event_lookback_bars", 6) or 6)
                    if is_ltf_chart else htf_structure_event_lookback(sr_cfg)
                ),
                min_range_atr_mult=float(getattr(sr_cfg, "structure_min_range_atr_mult", 1.5) or 0.0),
                min_pivot_gap_bars=overlay_gap_bars,
                last_bar_forming=last_bar_forming,
            )
        except Exception:
            self.log_component_failure(
                "structure_overlay",
                "Dashboard structure overlay build failed for timeframe=%sm",
                int(timeframe_minutes or 1),
            )
            return payload
        event = dashboard_structure_event_label(ms_ctx)
        age = None
        level = None
        if event == "CHOCH↑":
            age = int(getattr(ms_ctx, "choch_up_age_bars", 0) or 0)
            level = safe_float(getattr(ms_ctx, "reference_high", None))
        elif event == "CHOCH↓":
            age = int(getattr(ms_ctx, "choch_down_age_bars", 0) or 0)
            level = safe_float(getattr(ms_ctx, "reference_low", None))
        elif event == "BOS↑":
            age = int(getattr(ms_ctx, "bos_up_age_bars", 0) or 0)
            level = safe_float(getattr(ms_ctx, "reference_high", None))
        elif event == "BOS↓":
            age = int(getattr(ms_ctx, "bos_down_age_bars", 0) or 0)
            level = safe_float(getattr(ms_ctx, "reference_low", None))
        payload.update({
            "event": event,
            "age_bars": age,
            "event_ts": None if age is None else frame_for_analysis.index[len(frame_for_analysis) - 1 - age].isoformat(),
            "level": level,
            "bias": str(getattr(ms_ctx, "bias", "neutral") or "neutral"),
            "pivot_bias": str(getattr(ms_ctx, "pivot_bias", "neutral") or "neutral"),
        })
        return payload

    def chart_payload(self, symbol: str, *, max_bars: int = 90, timeframe_mode: str = "ltf") -> dict[str, Any]:
        """Build the dashboard chart payload for ``symbol`` — bars + patterns
        + structure overlay + chart config. This is the callable passed to
        ``DashboardServer`` as ``chart_payload_provider``.

        ``timeframe_mode``: ``"ltf"`` renders at the strategy's LTF
        (``params.ltf_minutes``, defaults to 1m streaming bars). ``"htf"``
        renders at the strategy's HTF (``params.htf_minutes`` or the shared
        ``support_resistance.timeframe_minutes`` default). Anything other
        than ``"htf"`` is normalized to ``"ltf"``."""
        from dataclasses import asdict
        resolved_mode = str(timeframe_mode or "ltf").strip().lower()
        if resolved_mode != "htf":
            resolved_mode = "ltf"
        symbol_key = str(symbol or "").upper().strip()
        try:
            capped_bars = max(1, min(int(max_bars or 90), 480))
        except (TypeError, ValueError):
            capped_bars = 90
        ltf_min = max(1, self._active_ltf_minutes())
        htf_min = self._active_htf_minutes()
        if resolved_mode == "htf":
            timeframe_minutes = htf_min
            timeframe_label = f"{htf_min}m"
        else:
            timeframe_minutes = ltf_min
            timeframe_label = f"{ltf_min}m" if ltf_min > 1 else "1m"
        # ``frame`` is what the chart plots, and ``forming_start`` the start
        # of its still-forming last bucket (None when every bar is complete).
        # ``completed_frame`` is ``frame`` without that bucket: the per-bar
        # candle tags are read from it, so every bar drawn as complete is
        # tagged and the forming one is not. ``context_frame`` is what the
        # chart patterns and structure overlay read. On the LTF chart that is
        # ``frame``, forming bucket included, because the strategy reads them
        # off that same frame (get_merged resamples the live 1m stream and
        # keeps the partial bucket). The chart patterns read the forming
        # bucket like any other bar; the structure overlay reads its close
        # and breaks but confirms no pivot with it, as the strategy's
        # _structure_context has since 2026-09-25. On the HTF chart it is
        # ``completed_frame``: the strategy's HTF contexts read completed
        # buckets only, and no strategy reads chart patterns off HTF bars --
        # there they describe the bars drawn. ``minute_frame`` is the 1m
        # frame the payload is current as of: its newest bar is
        # ``source_bar_ts``, which the client compares with the snapshot's
        # newest bar to know when this payload is stale.
        frame: pd.DataFrame | None = None
        stored_frame: pd.DataFrame | None = None
        minute_frame: pd.DataFrame | None = None
        forming_start: pd.Timestamp | None = None
        if resolved_mode == "htf" and symbol_key:
            # HTTP handler path: only read cached HTF data, never trigger a
            # Schwab fetch here. Forcing a refresh from the HTTP thread races
            # with the engine's per-cycle prefetch (the HTF frame refresh runs
            # under self._lock on the engine thread) and risks rate-limit
            # hits. If the cache is empty, return an empty chart — the next
            # engine cycle will populate it and the next poll will render.
            stored_frame = self.data.get_htf_frame(
                symbol_key,
                timeframe_minutes=htf_min,
                lookback_days=self._active_htf_lookback_days(),
                allow_refresh=False,
            )
            minute_frame = self.data.get_merged(symbol_key, with_indicators=False)
            frame, forming_start = dashboard_htf_chart_frame(
                stored_frame,
                minute_frame,
                timeframe_minutes=htf_min,
                now=sessions.now_et(),
            )
        elif symbol_key:
            # LTF path: when ltf_min is 1 fetch the streaming 1m frame
            # directly (no resample); for ltf_min > 1 (e.g. 5-min trigger
            # candles) get_merged resamples 1m -> ltf via resample_bars.
            if ltf_min > 1:
                # The 1m frame first: a stream bar landing between the two
                # reads then makes the payload name the OLDER bar, and the
                # client refetches once the snapshot shows the new one. Read
                # second, source_bar_ts could name a minute the plotted
                # bucket does not hold, and nothing would ask again.
                minute_frame = self.data.get_merged(symbol_key, with_indicators=False)
                frame = self.data.get_merged(symbol_key, timeframe=f"{ltf_min}min", with_indicators=True)
                # The resampled frame keeps the partial last bucket. Until
                # 2026-09-23 it was drawn as complete and candle-tagged off
                # its first minutes (AAPL 09-22 10:03: a 3-minute 10:00 5m bar
                # tagged CDLHAMMER; complete, it tags as a bearish marubozu).
                if frame is not None and not frame.empty and session_bucket_ends(frame.index[-1:], ltf_min)[0] > pd.Timestamp(sessions.now_et()):
                    forming_start = pd.Timestamp(frame.index[-1])
            else:
                frame = self.data.get_merged(symbol_key, with_indicators=True)
                minute_frame = frame
        completed_frame = frame.iloc[:-1] if frame is not None and forming_start is not None else frame
        context_frame = frame if resolved_mode == "ltf" else completed_frame
        source_bar_ts = minute_frame.index[-1].isoformat() if minute_frame is not None and not minute_frame.empty else None
        htf_refresh = self.data.last_htf_refresh.get((symbol_key, timeframe_minutes)) if resolved_mode == "htf" and symbol_key else None
        frame_signature = (
            dashboard_frame_signature(frame),
            htf_refresh.isoformat() if htf_refresh is not None else None,
            source_bar_ts,
            forming_start.isoformat() if forming_start is not None else None,
        )
        cache_key = (symbol_key, resolved_mode, capped_bars)
        with self.lock:
            cache_entry = self.chart_cache.get(cache_key)
            if cache_entry is not None and cache_entry.get("signature") == frame_signature:
                # Shallow copy of the top-level dict — we only mutate
                # `last_update` on the returned object. A `copy.deepcopy`
                # here costs ~4ms per call on a 360-bar payload (measured)
                # and was the dominant cost of every chart refresh.
                # Safe because:
                #   1. dashboard.py's `_json_safe` recursively builds new
                #      dicts/lists for serialization rather than mutating
                #      the input — inner references can be shared.
                #   2. We only assign to a top-level key on the new shallow
                #      dict, so the cached entry's bars/levels/structure
                #      payloads stay isolated from the caller.
                # Re-stamping `last_update` keeps the frontend timestamp
                # advancing while the underlying frame_signature is
                # unchanged.
                cached_payload = dict(cache_entry["payload"])
                cached_payload["last_update"] = sessions.now_et().isoformat()
                return cached_payload
        # Per-bar candle pattern map for the tooltip's per-bar candle section,
        # for every chart bar (see dashboard_bars_from_frame docstring +
        # detect_per_bar_candle_patterns). Read from completed_frame, so the
        # forming bucket gets none and every other bar is scored -- on the
        # HTF chart that includes the buckets completed since the stored
        # frame's last refresh, which until 2026-09-23 were drawn untagged
        # until it ran (at least 10 s into each bucket).
        chart_per_bar_candles: dict[Any, dict[str, list[str]]] = {}
        if completed_frame is not None and not completed_frame.empty:
            try:
                chart_per_bar_candles = self._per_bar_candle_map(completed_frame, capped_bars)
            except Exception:
                self.log_component_failure(
                    "per_bar_candles",
                    "Per-bar candle pattern detection failed for %s",
                    symbol_key,
                )
                chart_per_bar_candles = {}
        bars = dashboard_bars_from_frame(
            frame,
            max_bars=capped_bars,
            per_bar_candles=chart_per_bar_candles,
        )
        forming_ends_at: str | None = None
        if forming_start is not None:
            # The forming bucket is the plotted frame's last row. Its end goes
            # on the payload: the client refetches once it has passed, since
            # a bucket whose last minutes print nothing brings no newer 1m
            # bar (the LTF chart's only other refetch trigger) and would stay
            # drawn as forming until the next trade.
            bars[-1]["in_progress"] = True
            forming_ends_at = session_bucket_ends(frame.index[-1:], timeframe_minutes)[0].isoformat()
        # Default EMA spans rendered on the chart (matches what
        # `ensure_standard_indicator_frame` populates as ema9/ema20 columns).
        ema_fast_span = 9
        ema_slow_span = 20
        # In HTF mode, draw the HTF EMAs the strategy reads, not the frame's
        # session-reset ema9 / ema20 it never looks at. Field names stay
        # `ema9` / `ema20` for renderer compatibility; the legend uses the
        # spans in this payload.
        #  * A strategy whose HTF trend reads frame columns directly names
        #    them (zero_dte: the continuous ema9_all / ema20_all).
        #  * Otherwise, when the strategy declares htf_ema_fast_span /
        #    htf_ema_slow_span, the continuous EWM of those spans that
        #    build_htf_context computes -- also at 9/20, which until
        #    2026-09-24 skipped the override -- blanked, like the bot's own
        #    value, while the stored frame is too short for it (the bot's
        #    ema_slow is None under `span` bars, its ema_fast under
        #    max(5, span // 3)).
        if resolved_mode == "htf" and frame is not None and not getattr(frame, "empty", True):
            params = getattr(self.strategy, "params", {}) or {}
            columns_hook = getattr(self.strategy, "dashboard_htf_ema_columns", None)
            columns = columns_hook() if callable(columns_hook) else None
            try:
                tail = frame.tail(len(bars))
                if columns is not None:
                    fast_col, slow_col = columns
                    for bar, (_idx, row) in zip(bars, tail.iterrows()):
                        bar["ema9"] = safe_float(row.get(fast_col))
                        bar["ema20"] = safe_float(row.get(slow_col))
                elif "htf_ema_fast_span" in params or "htf_ema_slow_span" in params:
                    htf_fast, htf_slow = htf_ema_spans(params)
                    built_from = len(stored_frame) if stored_frame is not None else len(frame)
                    fast_ok = built_from >= max(5, htf_fast // 3)
                    slow_ok = built_from >= htf_slow
                    ema_fast_series = frame["close"].ewm(span=htf_fast, adjust=False).mean()
                    ema_slow_series = frame["close"].ewm(span=htf_slow, adjust=False).mean()
                    for bar, (idx, _row) in zip(bars, tail.iterrows()):
                        bar["ema9"] = safe_float(ema_fast_series.loc[idx]) if fast_ok else None
                        bar["ema20"] = safe_float(ema_slow_series.loc[idx]) if slow_ok else None
                    ema_fast_span = htf_fast
                    ema_slow_span = htf_slow
            except Exception:
                LOG.debug("Failed to compute HTF strategy EMAs for %s; chart falls back to default ema9/ema20.", symbol_key, exc_info=True)
        # In LTF mode, draw the EMAs the strategy reads off its LTF frame (for
        # top_tier_adaptive's 1m LTF at ltf_indicator_span_scale 5, the
        # session-reset EMA45 / EMA100), exactly as the snapshot bars merged
        # over these carry them.
        elif resolved_mode == "ltf" and bars:
            ema_fast_span, ema_slow_span = self._apply_strategy_ltf_emas(symbol_key, frame, bars, timeframe=f"{ltf_min}min")
        pattern_payload = self.current_pattern_payload(context_frame)
        # Rendered on the chart as the event marker + reference level line
        # (until 2026-09-23 it was computed per payload and never drawn).
        structure_overlay = self.current_structure_overlay(
            context_frame,
            timeframe_minutes=timeframe_minutes,
            last_bar_forming=forming_start is not None and resolved_mode == "ltf",
        )
        chart_config_profile = asdict(self.chart_profile("compact"))
        chart_config_expanded = asdict(self.chart_profile("expanded"))
        payload = {
            "symbol": symbol_key,
            "bars": bars,
            "bar_count": len(bars),
            "max_bars": capped_bars,
            "timeframe_mode": resolved_mode,
            "timeframe_label": timeframe_label,
            "timeframe_minutes": timeframe_minutes,
            "ema_fast_span": ema_fast_span,
            "ema_slow_span": ema_slow_span,
            "htf_refresh_token": htf_refresh.isoformat() if htf_refresh is not None else None,
            "last_bar_ts": str(bars[-1].get("ts")) if bars else None,
            "source_bar_ts": source_bar_ts,
            "forming_ends_at": forming_ends_at,
            "last_update": sessions.now_et().isoformat(),
            "patterns": pattern_payload,
            "structure_overlay": structure_overlay,
            "chart_config": {
                "compact": chart_config_profile,
                "expanded": chart_config_expanded,
            },
        }
        # Isolate cache entry from the outgoing payload so concurrent
        # pollers that hit this cache_key can't observe/mutate each other.
        # Matches the ordering in symbol_snapshot at line 964.
        with self.lock:
            self.chart_cache[cache_key] = {"signature": frame_signature, "payload": copy.deepcopy(payload)}
        return payload

    def tradable_symbols(self) -> list[str]:
        strategy_obj = self.strategy
        if strategy_obj is not None:
            try:
                return self._normalize_symbol_list(strategy_obj.dashboard_tradable_symbols())
            except Exception:
                pass
        params = getattr(strategy_obj, "params", {}) or {}
        raw_symbols = None
        if isinstance(params, dict):
            raw_symbols = params.get("tradable")
            if raw_symbols is None:
                raw_symbols = params.get("symbols")
        return self._normalize_symbol_list(raw_symbols)

    def index_symbols(self) -> list[str]:
        """Index ETFs used for directional confirmation (top_tier_adaptive's
        ``index_symbols`` + sector_index_map entries). Surfaced to the
        dashboard payload so watchlist cards can render an "IX" tag for
        these symbols (mirrors the "TR"/"NS" tagging for tradable /
        non-streamable)."""
        strategy_obj = self.strategy
        if strategy_obj is not None:
            try:
                return self._normalize_symbol_list(strategy_obj.dashboard_index_symbols())
            except Exception:
                pass
        params = getattr(strategy_obj, "params", {}) or {}
        if not isinstance(params, dict):
            return []
        merged: set[str] = set()
        raw_index = params.get("index_symbols")
        if raw_index:
            merged.update(self._normalize_symbol_list(raw_index))
        sector_map = params.get("sector_index_map")
        if isinstance(sector_map, dict):
            for tickers in sector_map.values():
                merged.update(self._normalize_symbol_list(tickers))
        return sorted(merged)
