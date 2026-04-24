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
from threading import RLock
from typing import TYPE_CHECKING, Any

import pandas as pd

import copy

from .candles import detect_candle_context
from .chart_patterns import analyze_chart_pattern_context
from .config import DashboardChartConfig, DashboardChartingConfig
from .htf_levels import summarize_htf_trend
from .models import Side
from .support_resistance import build_support_resistance_context, zone_flip_confirmed
from .technical_levels import build_technical_levels_context
from .utils import now_et, resample_bars
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
    candidates = [
        quote.get("exchange"),
        raw_quote.get("exchange"),
        raw_quote.get("exchangeName"),
        raw_quote.get("primaryExchange"),
        raw_quote.get("primaryExchangeName"),
        raw_reference.get("exchange"),
        raw_reference.get("exchangeName"),
        raw_reference.get("listingExchange"),
        raw_reference.get("primaryExchange"),
        raw_reference.get("primaryExchangeName"),
    ]
    for value in candidates:
        normalized = dashboard_normalize_exchange(value)
        if normalized:
            return normalized
    return None


def dashboard_technical_line_payload(line: Any) -> dict[str, Any] | None:
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


def dashboard_safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if out == out else None
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
            dashboard_safe_float(last_row.get("close")) if hasattr(last_row, "get") else None,
            dashboard_safe_float(last_row.get("high")) if hasattr(last_row, "get") else None,
            dashboard_safe_float(last_row.get("low")) if hasattr(last_row, "get") else None,
            dashboard_safe_float(last_row.get("volume")) if hasattr(last_row, "get") else None,
        )
    except Exception:
        return int(len(frame)), None, None, None, None, None, None


def dashboard_recent_trade_markers(account: Any, symbol: str) -> list[dict[str, Any]]:
    """Return up to 12 dashboard-shaped trade rows for ``symbol`` from
    ``account.trades``. Extracted from IntradayBot as part of Phase 5."""
    out: list[dict[str, Any]] = []
    key = str(symbol or "").upper().strip()
    if not key:
        return out
    for trade in list(getattr(account, "trades", []))[:12]:
        if str(getattr(trade, "symbol", "") or "").upper().strip() != key:
            continue
        try:
            out.append({
                "symbol": key,
                "side": str(getattr(trade, "side", "") or ""),
                "qty": int(getattr(trade, "qty", 0) or 0),
                "entry_price": dashboard_safe_float(getattr(trade, "entry_price", None)),
                "exit_price": dashboard_safe_float(getattr(trade, "exit_price", None)),
                "entry_time": getattr(trade, "entry_time", None).isoformat() if getattr(trade, "entry_time", None) is not None else None,
                "exit_time": getattr(trade, "exit_time", None).isoformat() if getattr(trade, "exit_time", None) is not None else None,
                "realized_pnl": dashboard_safe_float(getattr(trade, "realized_pnl", None)),
                "return_pct": dashboard_safe_float(getattr(trade, "return_pct", None)),
                "reason": str(getattr(trade, "reason", "") or ""),
            })
        except Exception:
            continue
    return out


def dashboard_symbol_trade_signature(account: Any, symbol: str) -> tuple[Any, ...]:
    """Build a cache-key signature capturing the last trade state for
    ``symbol`` on ``account``. Extracted from IntradayBot as part of Phase 5."""
    key = str(symbol or "").upper().strip()
    if not key:
        return 0, None, None, None
    count = 0
    latest_exit: str | None = None
    latest_entry: str | None = None
    latest_reason: str | None = None
    for trade in list(getattr(account, "trades", []))[:24]:
        if str(getattr(trade, "symbol", "") or "").upper().strip() != key:
            continue
        count += 1
        if latest_exit is None:
            exit_time = getattr(trade, "exit_time", None)
            entry_time = getattr(trade, "entry_time", None)
            latest_exit = exit_time.isoformat() if exit_time is not None else None
            latest_entry = entry_time.isoformat() if entry_time is not None else None
            latest_reason = str(getattr(trade, "reason", "") or "")
    return count, latest_exit, latest_entry, latest_reason


def dashboard_bars_from_frame(frame: pd.DataFrame | None, *, max_bars: int = 90) -> list[dict[str, Any]]:
    """Convert the last ``max_bars`` OHLCV rows of ``frame`` into a list of
    JSON-serializable dicts for the dashboard chart payload. Returns [] for
    None / empty frames. Extracted from IntradayBot as part of Phase 5."""
    capped_bars = max(1, min(int(max_bars or 90), 480))
    bars: list[dict[str, Any]] = []
    if frame is None or frame.empty:
        return bars
    tail = frame.tail(capped_bars).copy()
    tail_offset = max(0, len(frame) - len(tail))
    for rel_idx, (idx, row) in enumerate(tail.iterrows()):
        close_val = dashboard_safe_float(row.get("close"))
        atr14 = dashboard_safe_float(row.get("atr14"))
        bars.append({
            "ts": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
            "abs_index": tail_offset + rel_idx,
            "open": dashboard_safe_float(row.get("open")),
            "high": dashboard_safe_float(row.get("high")),
            "low": dashboard_safe_float(row.get("low")),
            "close": close_val,
            "volume": dashboard_safe_float(row.get("volume")),
            "ema9": dashboard_safe_float(row.get("ema9")),
            "ema20": dashboard_safe_float(row.get("ema20")),
            "vwap": dashboard_safe_float(row.get("vwap")),
            "atr14": atr14,
            "atr_pct": (atr14 / close_val) if atr14 is not None and close_val not in (None, 0.0) else None,
            "ret1": dashboard_safe_float(row.get("ret1")),
            "ret5": dashboard_safe_float(row.get("ret5")),
            "ret15": dashboard_safe_float(row.get("ret15")),
            "bb_mid": dashboard_safe_float(row.get("bb_mid")),
            "bb_upper": dashboard_safe_float(row.get("bb_upper")),
            "bb_lower": dashboard_safe_float(row.get("bb_lower")),
            "bb_width_pct": dashboard_safe_float(row.get("bb_width_pct")),
            "bb_percent_b": dashboard_safe_float(row.get("bb_percent_b")),
            "bb_zscore": dashboard_safe_float(row.get("bb_zscore")),
        })
    return bars


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

    def _active_sr_timeframe_minutes(self) -> int:
        cfg = getattr(self.config, "support_resistance", None)
        fallback = int(getattr(cfg, "timeframe_minutes", 15)) if cfg is not None else 15
        params = getattr(self.strategy, "params", {}) or {}
        return int(params.get("htf_timeframe_minutes", fallback))

    def _active_sr_lookback_days(self) -> int:
        cfg = getattr(self.config, "support_resistance", None)
        fallback = int(getattr(cfg, "lookback_days", 10)) if cfg is not None else 10
        params = getattr(self.strategy, "params", {}) or {}
        return int(params.get("htf_lookback_days", fallback))

    def _active_sr_refresh_seconds(self) -> int:
        cfg = getattr(self.config, "support_resistance", None)
        fallback = int(getattr(cfg, "refresh_seconds", 600)) if cfg is not None else 600
        params = getattr(self.strategy, "params", {}) or {}
        return int(params.get("htf_refresh_seconds", fallback))

    def htf_trend(self, symbol: str, *, allow_refresh: bool = True) -> dict[str, Any]:
        tf = self._active_sr_timeframe_minutes()
        lookback_days = self._active_sr_lookback_days()
        refresh_seconds = self._active_sr_refresh_seconds()
        frame = None
        if self.data is not None and hasattr(self.data, "get_htf_frame"):
            frame = self.data.get_htf_frame(
                symbol,
                timeframe_minutes=tf,
                lookback_days=lookback_days,
                refresh_seconds=refresh_seconds,
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
                return copy.deepcopy(cached_snapshot["payload"])
        bars = dashboard_bars_from_frame(frame, max_bars=self.snapshot_max_bars())
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
                        session_total_volume = dashboard_safe_float(session_volume)
            except Exception:
                session_total_volume = None

        quote_last = dashboard_safe_float(quote.get("last")) if quote_is_fresh else None
        quote_bid = dashboard_safe_float(quote.get("bid")) if quote_is_fresh else None
        quote_ask = dashboard_safe_float(quote.get("ask")) if quote_is_fresh else None
        quote_mark = dashboard_safe_float(quote.get("mark")) if quote_is_fresh else None
        quote_mid = dashboard_safe_float(quote.get("mid")) if quote_is_fresh else None
        quote_open = dashboard_safe_float(quote.get("open"))
        quote_close = dashboard_safe_float(quote.get("close"))
        quote_total_volume = dashboard_safe_float(quote.get("total_volume")) if quote_is_fresh else None
        # data_feed._build_quote_dict assigns percent_change / net_change via
        # _first_optional_float which yields None (not 0.0) when both Schwab
        # fields are absent, so a 0.0 here is always a real flat-session
        # reading rather than a sentinel.
        cached_percent_change = dashboard_safe_float(quote.get("percent_change"))
        cached_net_change = dashboard_safe_float(quote.get("net_change"))
        candidate_percent_change = dashboard_safe_float((candidate_row or {}).get("change_from_open"))
        candidate_close = dashboard_safe_float((candidate_row or {}).get("close"))
        regular_session_active = self.data.is_regular_session(now_et())
        display_total_volume = quote_total_volume
        if display_total_volume is None:
            display_total_volume = session_total_volume
        last_price = quote_last
        if last_price is None:
            last_price = dashboard_safe_float(latest_bar.get("close"))
        display_close = quote_close
        if not regular_session_active and candidate_close is not None:
            display_close = candidate_close
        if display_close is None and candidate_close is not None:
            display_close = candidate_close
        if display_close is None and len(bars) >= 2:
            display_close = dashboard_safe_float(bars[-2].get("close"))
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
            current_price = dashboard_safe_float((sr_row or {}).get("price"))
        if current_price is None:
            current_price = dashboard_safe_float(latest_bar.get("close"))

        support_prices: list[float] = []
        resistance_prices: list[float] = []
        next_support = None
        next_resistance = None
        ladder_min_gap = dashboard_safe_float((sr_row or {}).get("side_tolerance")) or _sr_effective_side_tolerance(self.config, current_price)
        technical_payload: dict[str, Any] = {}
        nearest_support = None
        nearest_resistance = None
        if sr_row:
            nearest_support = dashboard_safe_float(sr_row.get("nearest_support"))
            nearest_resistance = dashboard_safe_float(sr_row.get("nearest_resistance"))

            support_prices = sorted(
                [float(v) for v in (sr_row.get("supports") or []) if dashboard_safe_float(v) not in (None, 0.0)],
                reverse=True,
            )
            resistance_prices = sorted(
                [float(v) for v in (sr_row.get("resistances") or []) if dashboard_safe_float(v) not in (None, 0.0)]
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

        if frame is not None and not frame.empty:
            frame_for_analysis = frame.copy()
            for col in ("open", "high", "low", "close", "volume"):
                if col in frame_for_analysis.columns:
                    frame_for_analysis[col] = pd.to_numeric(frame_for_analysis[col], errors="coerce")
            frame_for_analysis = frame_for_analysis.dropna(subset=[col for col in ("open", "high", "low", "close") if col in frame_for_analysis.columns]).copy()

            tl_cfg = self.config.technical_levels
            sr_cfg = self.config.support_resistance
            try:
                tech_ctx = build_technical_levels_context(
                    frame_for_analysis,
                    current_price=current_price,
                    pivot_span=int(getattr(sr_cfg, "structure_1m_pivot_span", getattr(sr_cfg, "pivot_span", 2)) or 2),
                    fib_lookback_bars=int(getattr(tl_cfg, "fib_lookback_bars", 120) or 120),
                    fib_min_impulse_atr=float(getattr(tl_cfg, "fib_min_impulse_atr", 1.25) or 1.25),
                    anchored_vwap_impulse_lookback_bars=(int(getattr(tl_cfg, "anchored_vwap_impulse_lookback_bars")) if getattr(tl_cfg, "anchored_vwap_impulse_lookback_bars", None) is not None else None),
                    anchored_vwap_min_impulse_atr=(float(getattr(tl_cfg, "anchored_vwap_min_impulse_atr")) if getattr(tl_cfg, "anchored_vwap_min_impulse_atr", None) is not None else None),
                    anchored_vwap_pivot_span=(int(getattr(tl_cfg, "anchored_vwap_pivot_span")) if getattr(tl_cfg, "anchored_vwap_pivot_span", None) is not None else None),
                    trendline_lookback_bars=int(getattr(tl_cfg, "trendline_lookback_bars", 120) or 120),
                    trendline_min_touches=int(getattr(tl_cfg, "trendline_min_touches", 3) or 3),
                    trendline_atr_tolerance_mult=float(getattr(tl_cfg, "trendline_atr_tolerance_mult", 0.35) or 0.35),
                    trendline_breakout_buffer_atr_mult=float(getattr(tl_cfg, "trendline_breakout_buffer_atr_mult", 0.15) or 0.15),
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
                    divergence_rsi_min_delta=float(getattr(tl_cfg, "divergence_rsi_min_delta", 2.0) or 2.0),
                    divergence_obv_min_volume_frac=float(getattr(tl_cfg, "divergence_obv_min_volume_frac", 0.50) or 0.50),
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
                    "fib_bullish_1272": dashboard_safe_float(getattr(tech_ctx, "fib_bullish_1272", None)),
                    "fib_bullish_1618": dashboard_safe_float(getattr(tech_ctx, "fib_bullish_1618", None)),
                    "fib_bearish_1272": dashboard_safe_float(getattr(tech_ctx, "fib_bearish_1272", None)),
                    "fib_bearish_1618": dashboard_safe_float(getattr(tech_ctx, "fib_bearish_1618", None)),
                    "anchored_vwap_open": dashboard_safe_float(getattr(tech_ctx, "anchored_vwap_open", None)),
                    "anchored_vwap_bullish_impulse": dashboard_safe_float(getattr(tech_ctx, "anchored_vwap_bullish_impulse", None)),
                    "anchored_vwap_bearish_impulse": dashboard_safe_float(getattr(tech_ctx, "anchored_vwap_bearish_impulse", None)),
                    "anchored_vwap_bias": str(getattr(tech_ctx, "anchored_vwap_bias", "neutral") or "neutral"),
                    "adx": dashboard_safe_float(getattr(tech_ctx, "adx", None)),
                    "plus_di": dashboard_safe_float(getattr(tech_ctx, "plus_di", None)),
                    "minus_di": dashboard_safe_float(getattr(tech_ctx, "minus_di", None)),
                    "dmi_bias": str(getattr(tech_ctx, "dmi_bias", "neutral") or "neutral"),
                    "adx_rising": bool(getattr(tech_ctx, "adx_rising", False)),
                    "atr14": dashboard_safe_float(getattr(tech_ctx, "atr14", None)),
                    "atr_pct": dashboard_safe_float(getattr(tech_ctx, "atr_pct", None)),
                    "atr_expansion_mult": dashboard_safe_float(getattr(tech_ctx, "atr_expansion_mult", None)),
                    "atr_stretch_vwap_mult": dashboard_safe_float(getattr(tech_ctx, "atr_stretch_vwap_mult", None)),
                    "atr_stretch_ema20_mult": dashboard_safe_float(getattr(tech_ctx, "atr_stretch_ema20_mult", None)),
                    "obv": dashboard_safe_float(getattr(tech_ctx, "obv", None)),
                    "obv_ema": dashboard_safe_float(getattr(tech_ctx, "obv_ema", None)),
                    "obv_bias": str(getattr(tech_ctx, "obv_bias", "neutral") or "neutral"),
                    "rsi14": dashboard_safe_float(getattr(tech_ctx, "rsi14", None)),
                    "bullish_rsi_divergence": bool(getattr(tech_ctx, "bullish_rsi_divergence", False)),
                    "bearish_rsi_divergence": bool(getattr(tech_ctx, "bearish_rsi_divergence", False)),
                    "bullish_obv_divergence": bool(getattr(tech_ctx, "bullish_obv_divergence", False)),
                    "bearish_obv_divergence": bool(getattr(tech_ctx, "bearish_obv_divergence", False)),
                    "counter_divergence_bias": str(getattr(tech_ctx, "counter_divergence_bias", "neutral") or "neutral"),
                    "bollinger_mid": dashboard_safe_float(getattr(tech_ctx, "bollinger_mid", None)),
                    "bollinger_upper": dashboard_safe_float(getattr(tech_ctx, "bollinger_upper", None)),
                    "bollinger_lower": dashboard_safe_float(getattr(tech_ctx, "bollinger_lower", None)),
                    "bollinger_width_pct": dashboard_safe_float(getattr(tech_ctx, "bollinger_width_pct", None)),
                    "bollinger_percent_b": dashboard_safe_float(getattr(tech_ctx, "bollinger_percent_b", None)),
                    "bollinger_zscore": dashboard_safe_float(getattr(tech_ctx, "bollinger_zscore", None)),
                    "bollinger_squeeze": bool(getattr(tech_ctx, "bollinger_squeeze", False)),
                    "bollinger_upper_reject": bool(getattr(tech_ctx, "bollinger_upper_reject", False)),
                    "bollinger_lower_reject": bool(getattr(tech_ctx, "bollinger_lower_reject", False)),
                    "channel": {
                        "valid": bool(getattr(getattr(tech_ctx, "channel", None), "valid", False)),
                        "bias": str(getattr(getattr(tech_ctx, "channel", None), "bias", "neutral") or "neutral"),
                        "lower": dashboard_safe_float(getattr(getattr(tech_ctx, "channel", None), "lower", None)),
                        "upper": dashboard_safe_float(getattr(getattr(tech_ctx, "channel", None), "upper", None)),
                        "mid": dashboard_safe_float(getattr(getattr(tech_ctx, "channel", None), "mid", None)),
                        "position_pct": dashboard_safe_float(getattr(getattr(tech_ctx, "channel", None), "position_pct", None)),
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
            "entry": dashboard_safe_float((position_row or {}).get("entry_price")) if allows_underlying_markers else None,
            "stop": dashboard_safe_float((position_row or {}).get("stop_price")) if allows_underlying_markers else None,
            "target": dashboard_safe_float((position_row or {}).get("target_price")) if allows_underlying_markers else None,
            # Breakeven is in underlying-price units for both stocks (from entry)
            # and options (via metadata['breakeven_underlying']), so it's safe to
            # draw on the underlying chart regardless of asset_type.
            "breakeven": dashboard_safe_float((position_row or {}).get("breakeven")),
            "entry_time": (position_row or {}).get("entry_time"),
            # Option-specific: strikes in underlying-price units. Drawn on the
            # underlying chart when asset_type starts with OPTION_ because the
            # bot's stop_price/target_price are in OPTION-price units and can't
            # be plotted on the underlying's axis.
            "option_type": (position_row or {}).get("option_type") if is_option else None,
            "long_strike": dashboard_safe_float((position_row or {}).get("long_strike")) if is_option else None,
            "short_strike": dashboard_safe_float((position_row or {}).get("short_strike")) if is_option else None,
            "option_strike": dashboard_safe_float((position_row or {}).get("option_strike")) if is_option else None,
        }

        zone_support_prices = [nearest_support] if nearest_support not in (None, 0.0) else []
        zone_resistance_prices = [nearest_resistance] if nearest_resistance not in (None, 0.0) else []
        key_level_zones = self.strategy_level_zones(
            symbol,
            frame,
            current_price,
            support_prices=zone_support_prices,
            resistance_prices=zone_resistance_prices,
            broken_support_price=dashboard_safe_float((sr_row or {}).get("broken_support")),
            broken_resistance_price=dashboard_safe_float((sr_row or {}).get("broken_resistance")),
            allow_htf_refresh=allow_refresh,
        )
        htf_fair_value_gaps: list[dict[str, Any]] = []
        compact_chart_profile = self.chart_profile("compact")
        expanded_chart_profile = self.chart_profile("expanded")
        try:
            sr_cfg = getattr(self.config, "support_resistance", None)
            include_fair_value_gaps = bool(getattr(sr_cfg, "htf_fair_value_gaps_enabled", True)) if sr_cfg is not None else True
            chart_wants_htf_fvgs = bool(compact_chart_profile.show_htf_fair_value_gaps) or bool(expanded_chart_profile.show_htf_fair_value_gaps)
            if include_fair_value_gaps and chart_wants_htf_fvgs and self.data is not None:
                htf_ctx = self.data.get_htf_context(
                    symbol,
                    timeframe_minutes=self._active_sr_timeframe_minutes(),
                    lookback_days=self._active_sr_lookback_days(),
                    pivot_span=int(getattr(self.config.support_resistance, "pivot_span", 2) or 2),
                    max_levels_per_side=int(getattr(self.config.support_resistance, "max_levels_per_side", 3) or 3),
                    atr_tolerance_mult=float(getattr(self.config.support_resistance, "atr_tolerance_mult", 0.60) or 0.60),
                    pct_tolerance=float(getattr(self.config.support_resistance, "pct_tolerance", 0.0030) or 0.0030),
                    stop_buffer_atr_mult=float(getattr(self.config.support_resistance, "stop_buffer_atr_mult", 0.25) or 0.25),
                    ema_fast_span=50,
                    ema_slow_span=200,
                    refresh_seconds=self._active_sr_refresh_seconds(),
                    allow_refresh=allow_refresh,
                    use_prior_day_high_low=bool(getattr(self.config.support_resistance, "use_prior_day_high_low", True)),
                    use_prior_week_high_low=bool(getattr(self.config.support_resistance, "use_prior_week_high_low", True)),
                    include_fair_value_gaps=include_fair_value_gaps,
                    fair_value_gap_max_per_side=int(getattr(self.config.support_resistance, "htf_fair_value_gap_max_per_side", 4) or 4),
                    fair_value_gap_min_atr_mult=float(getattr(self.config.support_resistance, "htf_fair_value_gap_min_atr_mult", 0.05) or 0.05),
                    fair_value_gap_min_pct=float(getattr(self.config.support_resistance, "htf_fair_value_gap_min_pct", 0.0005) or 0.0005),
                )
                if htf_ctx is not None:
                    for gap in list(getattr(htf_ctx, "bullish_fvgs", []) or []) + list(getattr(htf_ctx, "bearish_fvgs", []) or []):
                        payload_fvg = dashboard_fvg_payload(gap)
                        if payload_fvg is not None:
                            payload_fvg["timeframe"] = f"{int(getattr(htf_ctx, 'timeframe_minutes', self._active_sr_timeframe_minutes()) or self._active_sr_timeframe_minutes())}m"
                            htf_fair_value_gaps.append(payload_fvg)
        except Exception:
            htf_fair_value_gaps = []

        one_minute_fair_value_gaps: list[dict[str, Any]] = []
        try:
            sr_cfg = getattr(self.config, "support_resistance", None)
            include_one_minute_fvgs = bool(getattr(sr_cfg, "one_minute_fair_value_gaps_enabled", False)) if sr_cfg is not None else False
            chart_wants_one_minute_fvgs = bool(compact_chart_profile.show_1m_fair_value_gaps) or bool(expanded_chart_profile.show_1m_fair_value_gaps)
            if include_one_minute_fvgs and chart_wants_one_minute_fvgs and self.data is not None:
                fvg_ctx = self.data.get_fair_value_gap_context(
                    symbol,
                    timeframe_minutes=1,
                    current_price=current_price,
                    max_per_side=int(getattr(self.config.support_resistance, "one_minute_fair_value_gap_max_per_side", 4) or 4),
                    min_gap_atr_mult=float(getattr(self.config.support_resistance, "one_minute_fair_value_gap_min_atr_mult", 0.05) or 0.05),
                    min_gap_pct=float(getattr(self.config.support_resistance, "one_minute_fair_value_gap_min_pct", 0.0005) or 0.0005),
                )
                if fvg_ctx is not None:
                    merged_index_frame = frame if frame is not None and not frame.empty else self.data.get_merged(symbol, with_indicators=True)
                    for gap in list(getattr(fvg_ctx, "bullish_fvgs", []) or []) + list(getattr(fvg_ctx, "bearish_fvgs", []) or []):
                        payload_fvg = dashboard_fvg_payload(gap)
                        if payload_fvg is not None:
                            payload_fvg["timeframe"] = "1m"
                            payload_fvg["anchor_abs_index"] = dashboard_fvg_anchor_abs_index(merged_index_frame, payload_fvg.get("first_seen"))
                            one_minute_fair_value_gaps.append(payload_fvg)
        except Exception:
            one_minute_fair_value_gaps = []

        chart_payload = {
            "levels": {
                "nearest_support": nearest_support,
                "nearest_resistance": nearest_resistance,
                "support_distance_pct": dashboard_safe_float((sr_row or {}).get("support_distance_pct")),
                "resistance_distance_pct": dashboard_safe_float((sr_row or {}).get("resistance_distance_pct")),
                "supports": support_prices,
                "resistances": resistance_prices,
                "next_support": next_support,
                "next_resistance": next_resistance,
                "broken_support": dashboard_safe_float((sr_row or {}).get("broken_support")),
                "broken_resistance": dashboard_safe_float((sr_row or {}).get("broken_resistance")),
                "key_level_zones": key_level_zones,
                "htf_fair_value_gaps": htf_fair_value_gaps,
                "one_minute_fair_value_gaps": one_minute_fair_value_gaps,
            },
            "technicals": technical_payload,
            "position_markers": position_markers,
            "recent_trades": dashboard_recent_trade_markers(self.account, symbol),
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
            level_ctx = {}
        if not isinstance(level_ctx, dict):
            level_ctx = {}

        support_anchor_prices = [float(price) for price in (support_prices or []) if dashboard_safe_float(price) not in (None, 0.0)]
        resistance_anchor_prices = [float(price) for price in (resistance_prices or []) if dashboard_safe_float(price) not in (None, 0.0)]
        broken_support_anchor = dashboard_safe_float(broken_support_price)
        broken_resistance_anchor = dashboard_safe_float(broken_resistance_price)
        if broken_resistance_anchor not in (None, 0.0):
            support_anchor_prices.append(float(broken_resistance_anchor))
        if broken_support_anchor not in (None, 0.0):
            resistance_anchor_prices.append(float(broken_support_anchor))

        def _dedupe_prices(values: list[float]) -> list[float]:
            deduped: list[float] = []
            seen: set[float] = set()
            for value in values:
                rounded = round(float(value), 4)
                if rounded <= 0 or rounded in seen:
                    continue
                seen.add(rounded)
                deduped.append(float(value))
            return deduped

        support_anchor_prices = _dedupe_prices(support_anchor_prices)
        resistance_anchor_prices = _dedupe_prices(resistance_anchor_prices)

        close = dashboard_safe_float(current_price)
        if close is None and frame is not None and not frame.empty:
            close = dashboard_safe_float(frame.iloc[-1].get("close"))
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
        refresh_seconds = max(1, int(level_ctx.get("refresh_seconds", 180) or 180))
        sr_cfg = getattr(self.config, "support_resistance", None)
        use_prior_day_high_low = bool(getattr(sr_cfg, "use_prior_day_high_low", True)) if sr_cfg is not None else True
        use_prior_week_high_low = bool(getattr(sr_cfg, "use_prior_week_high_low", True)) if sr_cfg is not None else True
        include_fair_value_gaps = bool(getattr(sr_cfg, "htf_fair_value_gaps_enabled", True)) if sr_cfg is not None else True
        fair_value_gap_max_per_side = int(getattr(sr_cfg, "htf_fair_value_gap_max_per_side", 4) or 4) if sr_cfg is not None else 4
        fair_value_gap_min_atr_mult = float(getattr(sr_cfg, "htf_fair_value_gap_min_atr_mult", 0.05) or 0.05) if sr_cfg is not None else 0.05
        fair_value_gap_min_pct = float(getattr(sr_cfg, "htf_fair_value_gap_min_pct", 0.0005) or 0.0005) if sr_cfg is not None else 0.0005

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
            refresh_seconds=refresh_seconds,
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

        trigger_tf = max(1, int(level_ctx.get("trigger_timeframe_minutes", 5) or 5))
        ltf = None
        try:
            if self.data is not None:
                timeframe = "1min" if trigger_tf <= 1 else f"{trigger_tf}min"
                ltf = self.data.get_merged(symbol, timeframe=timeframe, with_indicators=True)
            elif frame is not None and not frame.empty:
                if trigger_tf <= 1:
                    ltf = frame.copy()
                else:
                    ltf = resample_bars(frame, f"{trigger_tf}min")
        except Exception:
            ltf = None

        atr = None
        try:
            if ltf is not None and not ltf.empty:
                atr = dashboard_safe_float(ltf.iloc[-1].get("atr14"))
        except Exception:
            atr = None
        if atr is None:
            atr = dashboard_safe_float(getattr(htf, "atr14", None))
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
                    selected_long_price = dashboard_safe_float((selected_long or {}).get("price")) if isinstance(selected_long, dict) else None
                    selected_short_price = dashboard_safe_float((selected_short or {}).get("price")) if isinstance(selected_short, dict) else None
                else:
                    long_candidates = list(strategy_obj.dashboard_candidate_levels(float(close), htf, Side.LONG) or [])
                    short_candidates = list(strategy_obj.dashboard_candidate_levels(float(close), htf, Side.SHORT) or [])
        except Exception:
            long_candidates = []
            short_candidates = []
            selected_long_price = None
            selected_short_price = None

        allow_level_fallback = bool(getattr(strategy_obj, "dashboard_allow_generic_level_fallback", lambda: False)())
        if allow_level_fallback and not long_candidates and support_anchor_prices:
            long_candidates = [
                {"kind": "nearest_htf_support", "price": float(price), "touches": 1, "level_score": 0.0, "source_priority": 0.0}
                for price in support_anchor_prices
                if dashboard_safe_float(price) not in (None, 0.0)
            ]
        if allow_level_fallback and not short_candidates and resistance_anchor_prices:
            short_candidates = [
                {"kind": "nearest_htf_resistance", "price": float(price), "touches": 1, "level_score": 0.0, "source_priority": 0.0}
                for price in resistance_anchor_prices
                if dashboard_safe_float(price) not in (None, 0.0)
            ]

        def _candidate_zone_payload(side: Side, candidate: dict[str, Any]) -> dict[str, Any] | None:
            price = dashboard_safe_float(candidate.get("price"))
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
            raw_lower = dashboard_safe_float(candidate.get("zone_lower"))
            raw_upper = dashboard_safe_float(candidate.get("zone_upper"))
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
            }

        support_zones = [zone for zone in (_candidate_zone_payload(Side.LONG, candidate) for candidate in long_candidates) if zone is not None]
        resistance_zones = [zone for zone in (_candidate_zone_payload(Side.SHORT, candidate) for candidate in short_candidates) if zone is not None]

        zone_flip_1m = max(0, int(getattr(sr_cfg, "dashboard_flip_confirmation_1m_bars", 1) or 1)) if sr_cfg is not None else 1
        zone_flip_5m = 0
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
            if kind_name == "broken_htf_support":
                return "support"
            if kind_name == "broken_htf_resistance":
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
            lower = float(zone.get("lower", 0.0) or 0.0)
            upper = float(zone.get("upper", 0.0) or 0.0)
            confirmed = zone_flip_confirmed(
                original_kind,
                lower,
                upper,
                flip_frame=frame,
                confirm_1m_bars=zone_flip_1m,
                confirm_5m_bars=zone_flip_5m,
                fallback_bar=fallback_bar,
                eps=zone_eps,
            )
            sources = list(zone.get("sources", []) or [])
            zone["original_kind"] = str(original_kind)
            zone["confirmed_flip"] = False
            zone["flip_state"] = "original"
            zone["pending_flip"] = False
            zone["pending_state"] = ""
            zone["flip_target_kind"] = ""
            if level_kind == "broken_htf_support":
                if confirmed:
                    zone["kind"] = "resistance"
                    zone["confirmed_flip"] = True
                    zone["flip_state"] = "confirmed_flip"
                    zone["sources"] = list(dict.fromkeys([*sources, "confirmed_broken_support_zone"]))
                else:
                    zone["kind"] = "support"
                    zone["flip_state"] = "pending_flip"
                    zone["pending_flip"] = True
                    zone["pending_state"] = "pending_break"
                    zone["flip_target_kind"] = "resistance"
                    zone["sources"] = list(dict.fromkeys([*sources, "pending_broken_support"]))
                return zone
            if level_kind == "broken_htf_resistance":
                if confirmed:
                    zone["kind"] = "support"
                    zone["confirmed_flip"] = True
                    zone["flip_state"] = "confirmed_flip"
                    zone["sources"] = list(dict.fromkeys([*sources, "confirmed_broken_resistance_zone"]))
                else:
                    zone["kind"] = "resistance"
                    zone["flip_state"] = "pending_flip"
                    zone["pending_flip"] = True
                    zone["pending_state"] = "pending_reclaim"
                    zone["flip_target_kind"] = "support"
                    zone["sources"] = list(dict.fromkeys([*sources, "pending_broken_resistance"]))
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
                1.0 if level_kind.startswith("broken_htf_") else 0.0,
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

        for support in support_zones:
            support_price = float(support.get("price", 0.0) or 0.0)
            for resistance in resistance_zones:
                resistance_price = float(resistance.get("price", 0.0) or 0.0)
                support_upper = float(support.get("upper", 0.0) or 0.0)
                resistance_lower = float(resistance.get("lower", 0.0) or 0.0)
                if support_upper < resistance_lower:
                    continue
                midpoint = (support_price + max(resistance_price, support_price)) / 2.0 if resistance_price <= support_price else (support_price + resistance_price) / 2.0
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
            nearest_support = sorted([item for item in ordered if str(item.get("kind", "") or "") == "support"], key=lambda item: abs(float(item.get("price", 0.0) or 0.0) - float(close)))
            nearest_resistance = sorted([item for item in ordered if str(item.get("kind", "") or "") == "resistance"], key=lambda item: abs(float(item.get("price", 0.0) or 0.0) - float(close)))
            display_zones = []
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
        ctx = self.data.get_support_resistance(
            symbol,
            current_price=current_price,
            flip_frame=self.data.get_merged(symbol, with_indicators=False),
            mode="dashboard",
            timeframe_minutes=self._active_sr_timeframe_minutes(),
            lookback_days=self._active_sr_lookback_days(),
            refresh_seconds=self._active_sr_refresh_seconds(),
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

        def _valid_level(level: Any) -> Any | None:
            if level is None:
                return None
            try:
                return level if float(level.price) > 0 else None
            except Exception:
                return None

        display_support = _valid_level(ctx.nearest_support)
        display_resistance = _valid_level(ctx.nearest_resistance)
        if display_price is not None and display_price <= 0:
            display_price = None
        if display_price is not None:
            if display_support is not None and float(display_support.price) > float(display_price):
                display_support = None
            if display_resistance is not None and float(display_resistance.price) < float(display_price):
                display_resistance = None
        if display_support is not None and display_resistance is not None and float(display_resistance.price) <= float(display_support.price):
            display_support = None
            display_resistance = None

        support_distance_pct = ctx.support_distance_pct if display_support is not None else None
        if support_distance_pct is None and display_price is not None and display_support is not None and display_price > 0:
            support_distance_pct = abs(display_price - float(display_support.price)) / display_price
        resistance_distance_pct = ctx.resistance_distance_pct if display_resistance is not None else None
        if resistance_distance_pct is None and display_price is not None and display_resistance is not None and display_price > 0:
            resistance_distance_pct = abs(float(display_resistance.price) - display_price) / display_price

        support_distance_atr = ctx.support_distance_atr if display_support is not None else None
        resistance_distance_atr = ctx.resistance_distance_atr if display_resistance is not None else None

        trend_row = self.htf_trend(symbol, allow_refresh=allow_refresh)
        htf_trend_bias = "neutral"
        try:
            if self.data is not None:
                sr_cfg = getattr(self.config, "support_resistance", None)
                if sr_cfg is not None:
                    htf_ctx = self.data.get_htf_context(
                        symbol,
                        timeframe_minutes=self._active_sr_timeframe_minutes(),
                        lookback_days=self._active_sr_lookback_days(),
                        pivot_span=int(getattr(sr_cfg, "pivot_span", 2) or 2),
                        max_levels_per_side=int(getattr(sr_cfg, "max_levels_per_side", 3) or 3),
                        atr_tolerance_mult=float(getattr(sr_cfg, "atr_tolerance_mult", 0.60) or 0.60),
                        pct_tolerance=float(getattr(sr_cfg, "pct_tolerance", 0.0030) or 0.0030),
                        stop_buffer_atr_mult=float(getattr(sr_cfg, "stop_buffer_atr_mult", 0.25) or 0.25),
                        ema_fast_span=50,
                        ema_slow_span=200,
                        refresh_seconds=self._active_sr_refresh_seconds(),
                        allow_refresh=allow_refresh,
                        use_prior_day_high_low=bool(getattr(sr_cfg, "use_prior_day_high_low", True)),
                        use_prior_week_high_low=bool(getattr(sr_cfg, "use_prior_week_high_low", True)),
                        include_fair_value_gaps=bool(getattr(sr_cfg, "htf_fair_value_gaps_enabled", True)),
                        fair_value_gap_max_per_side=int(getattr(sr_cfg, "htf_fair_value_gap_max_per_side", 4) or 4),
                        fair_value_gap_min_atr_mult=float(getattr(sr_cfg, "htf_fair_value_gap_min_atr_mult", 0.05) or 0.05),
                        fair_value_gap_min_pct=float(getattr(sr_cfg, "htf_fair_value_gap_min_pct", 0.0005) or 0.0005),
                    )
                    htf_trend_bias = str(getattr(htf_ctx, "trend_bias", "neutral") or "neutral").strip().lower()
        except Exception:
            LOG.debug("Failed to read HTF trend bias context for %s; falling back to summarize_htf_trend().", symbol, exc_info=True)
        trend_state = str(trend_row.get("state", "neutral") or "neutral").strip().lower()
        trend_label = str(trend_row.get("label", "—") or "—")
        if htf_trend_bias in {"bullish", "bearish"}:
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

        support_prices = [float(round(lv.price, 4)) for lv in ctx.supports if getattr(lv, "price", None) is not None and float(lv.price) > 0]
        resistance_prices = [float(round(lv.price, 4)) for lv in ctx.resistances if getattr(lv, "price", None) is not None and float(lv.price) > 0]
        if ctx.broken_resistance is not None:
            broken_resistance_price = float(round(ctx.broken_resistance.price, 4))
            if broken_resistance_price > 0:
                support_prices.append(broken_resistance_price)
        if ctx.broken_support is not None:
            broken_support_price = float(round(ctx.broken_support.price, 4))
            if broken_support_price > 0:
                resistance_prices.append(broken_support_price)

        ladder_eps = 1e-4
        ladder_reference_price = display_price
        if ladder_reference_price is None:
            try:
                candidate_price = current_price if current_price is not None else getattr(ctx, "current_price", None)
                if candidate_price is not None and float(candidate_price) > 0:
                    ladder_reference_price = float(candidate_price)
            except Exception:
                ladder_reference_price = None
        ladder_min_gap = _sr_effective_side_tolerance(self.config, ladder_reference_price, sr_ctx=ctx)

        def _dedupe_sorted_prices(values: list[float], *, reverse: bool) -> list[float]:
            return _collapse_price_ladder(values, reverse=reverse, min_gap=ladder_eps)

        def _collapse_ladder_prices(values: list[float], *, reverse: bool) -> list[float]:
            return _collapse_price_ladder(values, reverse=reverse, min_gap=ladder_min_gap)

        support_prices = _dedupe_sorted_prices(support_prices, reverse=True)
        resistance_prices = _dedupe_sorted_prices(resistance_prices, reverse=False)
        raw_support_prices = list(support_prices)
        raw_resistance_prices = list(resistance_prices)
        support_anchor_prices = list(raw_support_prices)
        resistance_anchor_prices = list(raw_resistance_prices)

        if display_price is not None and display_price > 0:
            display_price_value = float(display_price)
            filtered_support_prices = [price for price in support_anchor_prices if price <= display_price_value + ladder_eps]
            filtered_resistance_prices = [price for price in resistance_anchor_prices if price >= display_price_value - ladder_eps]
            if not filtered_support_prices and raw_support_prices:
                fallback_supports = [price for price in raw_support_prices if price <= display_price_value + ladder_eps]
                if fallback_supports:
                    filtered_support_prices = [max(fallback_supports)]
            if not filtered_resistance_prices and raw_resistance_prices:
                fallback_resistances = [price for price in raw_resistance_prices if price >= display_price_value - ladder_eps]
                if fallback_resistances:
                    filtered_resistance_prices = [min(fallback_resistances)]
        else:
            filtered_support_prices = list(support_anchor_prices)
            filtered_resistance_prices = list(resistance_anchor_prices)

        support_prices = [price for price in filtered_support_prices if all(abs(price - other) > ladder_eps for other in filtered_resistance_prices)]
        resistance_prices = [price for price in filtered_resistance_prices if all(abs(price - other) > ladder_eps for other in support_prices)]

        if display_support is not None:
            support_prices.append(float(round(display_support.price, 4)))
        if display_resistance is not None:
            resistance_prices.append(float(round(display_resistance.price, 4)))

        support_prices = _collapse_ladder_prices(_dedupe_sorted_prices(support_prices, reverse=True), reverse=True)
        resistance_prices = _collapse_ladder_prices(_dedupe_sorted_prices(resistance_prices, reverse=False), reverse=False)

        nearest_support_price = support_prices[0] if support_prices else (float(display_support.price) if display_support else None)
        nearest_resistance_price = resistance_prices[0] if resistance_prices else (float(display_resistance.price) if display_resistance else None)
        if nearest_support_price is not None and nearest_resistance_price is not None and float(nearest_resistance_price) <= float(nearest_support_price):
            support_prices = [price for price in support_anchor_prices if price < float(nearest_resistance_price) - ladder_eps]
            resistance_prices = [price for price in resistance_anchor_prices if price > float(nearest_support_price) + ladder_eps]
            nearest_support_price = support_prices[0] if support_prices else None
            nearest_resistance_price = resistance_prices[0] if resistance_prices else None

        timeframe_minutes = int(getattr(ctx, "timeframe_minutes", self._active_sr_timeframe_minutes()) or self._active_sr_timeframe_minutes())
        symbol_key = str(symbol or "").upper().strip()
        htf_refresh = self.data.last_htf_refresh.get((symbol_key, timeframe_minutes)) if symbol_key else None

        return {
            "symbol": symbol,
            "timeframe": f"{timeframe_minutes}m",
            "price": display_price,
            "htf_refresh_token": htf_refresh.isoformat() if htf_refresh is not None else None,
            "side_tolerance": dashboard_safe_float(getattr(ctx, "side_tolerance", None)),
            "nearest_support": nearest_support_price,
            "nearest_resistance": nearest_resistance_price,
            "support_distance_pct": None if support_distance_pct is None else float(support_distance_pct),
            "resistance_distance_pct": None if resistance_distance_pct is None else float(resistance_distance_pct),
            "support_distance_atr": None if support_distance_atr is None else float(support_distance_atr),
            "resistance_distance_atr": None if resistance_distance_atr is None else float(resistance_distance_atr),
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
            "supports": support_prices,
            "resistances": resistance_prices,
            "broken_support": float(ctx.broken_support.price) if ctx.broken_support and float(ctx.broken_support.price) > 0 else None,
            "broken_resistance": float(ctx.broken_resistance.price) if ctx.broken_resistance and float(ctx.broken_resistance.price) > 0 else None,
        }

    def snapshot_should_bypass_cache(self, symbol: str, *, allow_refresh: bool) -> bool:
        """True when support-resistance or HTF context needs a fresh refresh.

        Callers (dashboard snapshot builders) use this to decide whether a
        cached snapshot can be returned or must be recomputed."""
        if not allow_refresh:
            return False
        try:
            sr_tf = self._active_sr_timeframe_minutes()
            sr_refresh = self._active_sr_refresh_seconds()
            if self.data.should_refresh_support_resistance(symbol, timeframe_minutes=sr_tf, refresh_seconds=sr_refresh):
                return True
            if self.data.should_refresh_htf_context(symbol, sr_tf, sr_refresh):
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
        htf_refresh = self.data.last_htf_refresh.get((symbol_key, self._active_sr_timeframe_minutes())) if symbol_key else None
        quote_body = quote or {}
        return (
            dashboard_frame_signature(frame),
            bool(quote_is_fresh),
            quote_refresh.isoformat() if quote_refresh is not None else None,
            history_refresh.isoformat() if history_refresh is not None else None,
            stream_refresh.isoformat() if stream_refresh is not None else None,
            htf_refresh.isoformat() if htf_refresh is not None else None,
            dashboard_safe_float(quote_body.get("last")) if isinstance(quote_body, Mapping) else None,
            dashboard_safe_float(quote_body.get("bid")) if isinstance(quote_body, Mapping) else None,
            dashboard_safe_float(quote_body.get("ask")) if isinstance(quote_body, Mapping) else None,
            dashboard_safe_float(quote_body.get("mark")) if isinstance(quote_body, Mapping) else None,
            dashboard_safe_float(quote_body.get("total_volume")) if isinstance(quote_body, Mapping) else None,
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
                payload["chart_bias_score"] = dashboard_safe_float(getattr(chart_ctx, "bias_score", None))
                payload["chart_regime_hint"] = str(getattr(chart_ctx, "regime_hint", "neutral") or "neutral")
            except Exception:
                LOG.debug("Failed to attach chart-pattern payload to dashboard response; returning partial payload.", exc_info=True)
        return payload

    def current_structure_overlay(self, frame: pd.DataFrame | None, *, timeframe_minutes: int) -> dict[str, Any]:
        """Build the market-structure overlay payload (CHOCH/BOS event, age,
        level) from ``frame`` at the given timeframe. Returns neutral payload
        on any failure (with rate-limited warning via log_component_failure)."""
        payload: dict[str, Any] = {
            "event": "—",
            "age_bars": None,
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
        close_val = dashboard_safe_float(frame_for_analysis["close"].iloc[-1])
        if close_val is None:
            return payload
        try:
            sr_cfg = self.config.support_resistance
            sr_ctx = build_support_resistance_context(
                frame_for_analysis,
                current_price=close_val,
                flip_frame=frame_for_analysis,
                timeframe_minutes=int(timeframe_minutes or 1),
                pivot_span=int(getattr(sr_cfg, "pivot_span", 2) or 2),
                max_levels_per_side=int(getattr(sr_cfg, "max_levels_per_side", 3) or 3),
                atr_tolerance_mult=float(getattr(sr_cfg, "atr_tolerance_mult", 0.60) or 0.60),
                pct_tolerance=float(getattr(sr_cfg, "pct_tolerance", 0.0030) or 0.0030),
                same_side_min_gap_atr_mult=float(getattr(sr_cfg, "same_side_min_gap_atr_mult", 0.10) or 0.10),
                same_side_min_gap_pct=float(getattr(sr_cfg, "same_side_min_gap_pct", 0.0015) or 0.0015),
                fallback_reference_max_drift_atr_mult=float(getattr(sr_cfg, "fallback_reference_max_drift_atr_mult", 1.0) or 1.0),
                fallback_reference_max_drift_pct=float(getattr(sr_cfg, "fallback_reference_max_drift_pct", 0.01) or 0.01),
                proximity_atr_mult=float(getattr(sr_cfg, "proximity_atr_mult", 1.0) or 1.0),
                breakout_atr_mult=float(getattr(sr_cfg, "breakout_atr_mult", 0.35) or 0.35),
                breakout_buffer_pct=float(getattr(sr_cfg, "breakout_buffer_pct", 0.0015) or 0.0015),
                stop_buffer_atr_mult=float(getattr(sr_cfg, "stop_buffer_atr_mult", 0.25) or 0.25),
                use_prior_day_high_low=bool(getattr(sr_cfg, "use_prior_day_high_low", True)),
                use_prior_week_high_low=bool(getattr(sr_cfg, "use_prior_week_high_low", True)),
                flip_confirmation_1m_bars=int(getattr(sr_cfg, "flip_confirmation_1m_bars", 2) or 2),
                flip_confirmation_5m_bars=int(getattr(sr_cfg, "flip_confirmation_5m_bars", 1) or 1),
                structure_eq_atr_mult=float(getattr(sr_cfg, "structure_eq_atr_mult", 0.25) or 0.25),
                structure_event_max_age_bars=int(getattr(sr_cfg, "structure_event_lookback_bars", 6) or 6),
            )
        except Exception:
            self.log_component_failure(
                "structure_overlay",
                "Dashboard structure overlay build failed for timeframe=%sm",
                int(timeframe_minutes or 1),
            )
            return payload
        ms_ctx = getattr(sr_ctx, "market_structure", None)
        if ms_ctx is None:
            return payload
        event = dashboard_structure_event_label(ms_ctx)
        age = None
        level = None
        if event == "CHOCH↑":
            age = int(getattr(ms_ctx, "choch_up_age_bars", 0) or 0)
            level = dashboard_safe_float(getattr(ms_ctx, "reference_high", None))
        elif event == "CHOCH↓":
            age = int(getattr(ms_ctx, "choch_down_age_bars", 0) or 0)
            level = dashboard_safe_float(getattr(ms_ctx, "reference_low", None))
        elif event == "BOS↑":
            age = int(getattr(ms_ctx, "bos_up_age_bars", 0) or 0)
            level = dashboard_safe_float(getattr(ms_ctx, "reference_high", None))
        elif event == "BOS↓":
            age = int(getattr(ms_ctx, "bos_down_age_bars", 0) or 0)
            level = dashboard_safe_float(getattr(ms_ctx, "reference_low", None))
        payload.update({
            "event": event,
            "age_bars": age,
            "level": level,
            "bias": str(getattr(ms_ctx, "bias", "neutral") or "neutral"),
            "pivot_bias": str(getattr(ms_ctx, "pivot_bias", "neutral") or "neutral"),
        })
        return payload

    def chart_payload(self, symbol: str, *, max_bars: int = 90, timeframe_mode: str = "1m") -> dict[str, Any]:
        """Build the dashboard chart payload for ``symbol`` — bars + patterns
        + structure overlay + chart config. This is the callable passed to
        ``DashboardServer`` as ``chart_payload_provider``."""
        from dataclasses import asdict
        resolved_mode = str(timeframe_mode or "1m").strip().lower()
        if resolved_mode != "htf":
            resolved_mode = "1m"
        symbol_key = str(symbol or "").upper().strip()
        try:
            capped_bars = max(1, min(int(max_bars or 90), 480))
        except (TypeError, ValueError):
            capped_bars = 90
        timeframe_label = f"{self._active_sr_timeframe_minutes()}m" if resolved_mode == "htf" else "1m"
        timeframe_minutes = self._active_sr_timeframe_minutes() if resolved_mode == "htf" else 1
        if resolved_mode == "htf" and symbol_key:
            # HTTP handler path: only read cached HTF data, never trigger a
            # Schwab fetch here. Forcing a refresh from the HTTP thread races
            # with the engine's per-cycle prefetch (data_feed.py:480 runs
            # under self._lock on the engine thread) and risks rate-limit
            # hits. If the cache is empty, return an empty chart — the next
            # engine cycle will populate it and the next poll will render.
            frame = self.data.get_htf_frame(
                symbol_key,
                timeframe_minutes=self._active_sr_timeframe_minutes(),
                lookback_days=self._active_sr_lookback_days(),
                refresh_seconds=self._active_sr_refresh_seconds(),
                allow_refresh=False,
            )
        else:
            frame = self.data.get_merged(symbol_key, with_indicators=True) if symbol_key else None
        htf_refresh = self.data.last_htf_refresh.get((symbol_key, timeframe_minutes)) if resolved_mode == "htf" and symbol_key else None
        frame_signature = (
            dashboard_frame_signature(frame),
            htf_refresh.isoformat() if htf_refresh is not None else None,
        )
        cache_key = (symbol_key, resolved_mode, capped_bars)
        with self.lock:
            cache_entry = self.chart_cache.get(cache_key)
            if cache_entry is not None and cache_entry.get("signature") == frame_signature:
                return copy.deepcopy(cache_entry["payload"])
        bars = dashboard_bars_from_frame(frame, max_bars=capped_bars)
        pattern_payload = self.current_pattern_payload(frame)
        structure_overlay = self.current_structure_overlay(frame, timeframe_minutes=timeframe_minutes)
        chart_config_profile = asdict(self.chart_profile("compact"))
        chart_config_expanded = asdict(self.chart_profile("expanded"))
        payload = {
            "symbol": symbol_key,
            "bars": bars,
            "bar_count": len(bars),
            "max_bars": capped_bars,
            "timeframe_mode": resolved_mode,
            "timeframe_label": timeframe_label,
            "htf_refresh_token": htf_refresh.isoformat() if htf_refresh is not None else None,
            "last_bar_ts": str(bars[-1].get("ts")) if bars else None,
            "last_update": now_et().isoformat(),
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
