# SPDX-License-Identifier: MIT
"""The dashboard's stateless payload helpers.

``DashboardCache`` builds the snapshot and chart payloads from these:
exchange names (``normalize_exchange``, ``quote_exchange``), the chart's bar
rows (``bars_from_frame``) and the HTF chart's frame (``htf_chart_frame``),
the FVG / order-block and trendline payloads, today's trade markers, and
the signatures the snapshot and chart caches key on. Until 2026-09-27 they
were ``dashboard_cache`` module functions named with a ``dashboard_`` prefix
(refactor cut C40).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from . import sessions
from .bars import equity_stream_window_bars, last_bucket_forming, resample_bars, session_bucket_ends
from .indicators import ensure_standard_indicator_frame
from .numeric import safe_float

if TYPE_CHECKING:
    from .paper_account import PaperAccount

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


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


def normalize_exchange(value: Any) -> str | None:
    token = str(value or "").upper().strip()
    if not token:
        return None
    normalized = " ".join(token.replace("-", " ").replace("/", " ").split())
    return _EXCHANGE_ALIASES.get(normalized, _EXCHANGE_ALIASES.get(token, token or None))


def quote_exchange(quote: Mapping[str, Any] | None) -> str | None:
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
        normalized = normalize_exchange(value)
        if normalized:
            return normalized
    return None


def technical_line_payload(line: Any) -> dict[str, Any] | None:
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


def fvg_payload(gap: Any) -> dict[str, Any] | None:
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
        return str(value)

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


def fvg_anchor_abs_index(frame: pd.DataFrame | None, first_seen: Any) -> int | None:
    if frame is None or getattr(frame, "empty", True) or first_seen in (None, ""):
        return None
    index = getattr(frame, "index", None)
    if not isinstance(index, pd.DatetimeIndex) or index.empty:
        return None
    # The builders stamp first_seen with the bar's isoformat, or str() of an
    # index label that is not a timestamp: that one has no chart anchor.
    try:
        anchor_ts = pd.Timestamp(first_seen)
    except ValueError:
        return None
    if getattr(anchor_ts, "tzinfo", None) is not None:
        anchor_ts = anchor_ts.tz_convert(None)
    index_for_search = index.tz_convert(None) if getattr(index, "tz", None) is not None else index
    pos = int(index_for_search.searchsorted(anchor_ts, side="left"))
    if pos < 0 or pos >= len(index_for_search):
        return None
    return pos


def cache_json_signature(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        # sort_keys cannot order a dict mixing key types (TypeError), and
        # json refuses a circular container (ValueError).
        return repr(value)


def frame_signature(frame: pd.DataFrame | None) -> tuple[Any, ...]:
    if frame is None or getattr(frame, "empty", True):
        return 0, None, None, None, None, None, None
    index = getattr(frame, "index", None)
    first_idx = index[0] if index is not None and len(index) else None
    last_idx = index[-1] if index is not None and len(index) else None
    last_row = frame.iloc[-1]

    def _ts(value: Any) -> str | None:
        if value is None:
            return None
        return pd.Timestamp(value).isoformat()

    return (
        int(len(frame)),
        _ts(first_idx),
        _ts(last_idx),
        safe_float(last_row.get("close")) if hasattr(last_row, "get") else None,
        safe_float(last_row.get("high")) if hasattr(last_row, "get") else None,
        safe_float(last_row.get("low")) if hasattr(last_row, "get") else None,
        safe_float(last_row.get("volume")) if hasattr(last_row, "get") else None,
    )


def recent_trade_markers(account: PaperAccount, symbol: str) -> list[dict[str, Any]]:
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
    for trade in list(account.trades):
        if str(getattr(trade, "symbol", "") or "").upper().strip() != key:
            continue
        # Today-filter: a trade belongs to today's chart if either side
        # of the round-trip happened today. Exit-time wins when present;
        # fall back to entry_time so paper-account entries that haven't
        # exited yet still surface.
        exit_time = getattr(trade, "exit_time", None)
        entry_time = getattr(trade, "entry_time", None)
        ref_time = exit_time if exit_time is not None else entry_time
        if ref_time is None or ref_time.date() != today:
            continue
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
        if len(out) >= 12:
            break
    return out


def symbol_trade_signature(account: PaperAccount, symbol: str) -> tuple[Any, ...]:
    """Build a cache-key signature capturing the last trade state for
    ``symbol`` on ``account``.

    Same correctness fix as ``recent_trade_markers``: filter
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
    for trade in list(account.trades):
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


# The frame columns ``bars_from_frame`` reads, each once per call.
_BAR_COLUMNS = ("open", "high", "low", "close", "volume", "ema9", "ema20", "vwap", "atr14", "ret1", "ret5", "ret15",
                "bb_mid", "bb_upper", "bb_lower", "bb_width_pct", "bb_percent_b", "bb_zscore", "adx14",
                "plus_di14", "minus_di14", "obv", "obv_ema20")


def bars_from_frame(
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
    tail = frame.tail(capped_bars)
    n = len(tail)
    tail_offset = max(0, len(frame) - n)
    per_bar_candles = per_bar_candles or {}
    # One read per column instead of a row Series per bar; a column the
    # frame lacks reads as None on every bar.
    present = tail.columns
    col = {name: ([safe_float(v) for v in tail[name].tolist()] if name in present else [None] * n)
           for name in _BAR_COLUMNS}
    opens, highs, lows, closes, volumes = col["open"], col["high"], col["low"], col["close"], col["volume"]
    ema9s, ema20s, vwaps, atr14s = col["ema9"], col["ema20"], col["vwap"], col["atr14"]
    ret1s, ret5s, ret15s = col["ret1"], col["ret5"], col["ret15"]
    bb_mids, bb_uppers, bb_lowers = col["bb_mid"], col["bb_upper"], col["bb_lower"]
    bb_widths, bb_percent_bs, bb_zscores = col["bb_width_pct"], col["bb_percent_b"], col["bb_zscore"]
    adxs, plus_dis, minus_dis, obvs, obv_emas = (col["adx14"], col["plus_di14"], col["minus_di14"], col["obv"],
                                                 col["obv_ema20"])
    for rel_idx, idx in enumerate(tail.index):
        close_val = closes[rel_idx]
        atr14 = atr14s[rel_idx]
        plus_di = plus_dis[rel_idx]
        minus_di = minus_dis[rel_idx]
        obv = obvs[rel_idx]
        obv_ema = obv_emas[rel_idx]
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
            "open": opens[rel_idx],
            "high": highs[rel_idx],
            "low": lows[rel_idx],
            "close": close_val,
            "volume": volumes[rel_idx],
            "ema9": ema9s[rel_idx],
            "ema20": ema20s[rel_idx],
            "vwap": vwaps[rel_idx],
            "atr14": atr14,
            "atr_pct": (atr14 / close_val) if atr14 is not None and close_val not in (None, 0.0) else None,
            "ret1": ret1s[rel_idx],
            "ret5": ret5s[rel_idx],
            "ret15": ret15s[rel_idx],
            "bb_mid": bb_mids[rel_idx],
            "bb_upper": bb_uppers[rel_idx],
            "bb_lower": bb_lowers[rel_idx],
            "bb_width_pct": bb_widths[rel_idx],
            "bb_percent_b": bb_percent_bs[rel_idx],
            "bb_zscore": bb_zscores[rel_idx],
            "adx": adxs[rel_idx],
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


def htf_chart_frame(
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
    forming = pd.Timestamp(buckets.index[-1]) if last_bucket_forming(buckets.index, int(timeframe_minutes), now) else None
    return frame, forming
