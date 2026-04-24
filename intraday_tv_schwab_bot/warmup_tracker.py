# SPDX-License-Identifier: MIT
"""WarmupTracker — owns history-fetch scheduling and warmup readiness.

Extracted from ``IntradayBot`` as a follow-up to CycleGate. Each cycle,
``step()`` asks the tracker two things:

  1. For each symbol in the watchlist, should we fetch more history this
     cycle? (``should_fetch_symbol_history``)
  2. What's the warmup state of the watchlist — ready symbols vs blocked,
     current bars vs required, timing of last refresh? (``warmup_summary``
     + ``log_warmup_summary``)

The tracker reads config (warmup_minutes, history_lookback_minutes,
history_poll_seconds, chart expanded.max_bars), queries ``MarketDataStore``
(history/merged frames, stream/backfill status, data state), and asks the
strategy how many bars it needs (``required_history_bars(symbol, positions)``).

Design notes:

- ``self.positions`` is a shared-reference dict; the tracker never mutates
  it — only passes to ``strategy.required_history_bars`` which reads the
  current position count for per-symbol requirement calculation.
- ``_symbol_warmup_snapshot`` output is consumed by ``_publish_state`` +
  EntryGatekeeper's candidate-level entry gate (via live_entry_bar_status
  on MarketDataStore, not directly from the tracker).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from .audit_logger import AuditLogger
from .config import BotConfig
from .data_feed import MarketDataStore
from .models import Position
from ._strategies.strategy_base import BaseStrategy
from .utils import equity_rth_open_at, is_regular_equity_session, is_weekday_session_day, now_et, previous_regular_close

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


class WarmupTracker:
    def __init__(
        self,
        config: BotConfig,
        *,
        data: MarketDataStore,
        strategy: BaseStrategy,
        positions: dict[str, Position],
        audit: AuditLogger,
    ) -> None:
        self.config = config
        self.data = data
        self.strategy = strategy
        self.positions = positions
        self.audit = audit

    # ------------------------------------------------------------------
    # History-fetch scheduling (per-symbol per-cycle decision).
    # ------------------------------------------------------------------

    def _initial_history_lookback_minutes(self, now: datetime, *, streaming_active: bool) -> int | None:
        if not streaming_active:
            return None
        base_lookback = max(1, int(self.config.runtime.warmup_minutes))
        if not is_weekday_session_day(now):
            return base_lookback
        rth_open = equity_rth_open_at(now)
        if not is_regular_equity_session(now):
            return base_lookback
        session_minutes = int(max(0.0, (now - rth_open).total_seconds()) // 60) + 5
        return max(base_lookback, session_minutes)

    def _required_history_bars(self, symbol: str) -> int:
        try:
            return max(0, int(self.strategy.required_history_bars(symbol=symbol, positions=self.positions) or 0))
        except Exception:
            return 0

    def desired_history_bars(self, symbol: str) -> int:
        """Bars to *request* from the API — includes chart needs so the
        expanded dashboard chart has full data, but does NOT gate warmup
        readiness (that uses ``_required_history_bars`` which only needs
        the strategy's min_bars)."""
        required = self._required_history_bars(symbol)
        try:
            chart_bars = int(getattr(self.config.dashboard.charting.expanded, "max_bars", 0) or 0)
        except Exception:
            chart_bars = 0
        return max(required, chart_bars)

    def history_fetch_lookback_minutes(self, now: datetime, *, streaming_active: bool, required_bars: int = 0) -> int | None:
        configured = max(1, int(self.config.runtime.history_lookback_minutes))
        initial = self._initial_history_lookback_minutes(now, streaming_active=streaming_active)
        desired = max(configured, 0 if initial is None else int(initial))
        required = max(0, int(required_bars or 0))
        if required > 0 and is_weekday_session_day(now):
            desired = max(desired, required + 5)
            # If the current session doesn't have enough bars yet, bridge
            # the overnight gap so the fetch reaches into the previous
            # day's RTH session.  This applies regardless of the extended-
            # hours setting because extended-hours bars are sparse and
            # rarely fill the full requirement on their own.
            rth_open = equity_rth_open_at(now)
            session_minutes = 0
            if is_regular_equity_session(now):
                session_minutes = int(max(0.0, (now - rth_open).total_seconds()) // 60) + 1
            if session_minutes < required:
                overnight_gap = int(max(0.0, (rth_open - previous_regular_close(rth_open)).total_seconds()) // 60)
                desired = max(desired, overnight_gap + required + 5)
        elif required > 0:
            desired = max(desired, required + 5)
        return desired

    def should_fetch_symbol_history(self, symbol: str, *, context_refresh_active: bool, streaming_active: bool) -> tuple[bool, int]:
        history_frame = self.data.get_history(symbol)
        merged_frame = self.data.get_merged(symbol, with_indicators=False)
        history_known = history_frame is not None
        history_has_rows = history_known and not history_frame.empty
        merged_bars = 0 if merged_frame is None else len(merged_frame)
        required_bars = self._required_history_bars(symbol)
        should_fetch = context_refresh_active and not history_known
        if context_refresh_active and not should_fetch and required_bars > 0 and merged_bars < required_bars:
            # Below required bars — retry aggressively (every 10s) instead of
            # waiting the full history_poll_seconds (90s) so warmup completes
            # before the entry window opens.
            last_refresh = self.data.last_history_refresh.get(str(symbol).upper().strip())
            if last_refresh is None:
                should_fetch = True
            else:
                age = (now_et() - last_refresh).total_seconds()
                should_fetch = age >= 10.0
        if context_refresh_active and not should_fetch:
            if streaming_active:
                if self.data.is_streamable_equity(symbol) and history_has_rows:
                    should_fetch = self.data.should_backfill_stream_symbol(symbol)
                else:
                    should_fetch = self.data.should_refresh_history(symbol)
            else:
                should_fetch = (not history_has_rows) and self.data.should_refresh_history(symbol)
        return should_fetch, required_bars

    # ------------------------------------------------------------------
    # Per-symbol + watchlist-wide warmup snapshots (for dashboard + logs).
    # ------------------------------------------------------------------

    def _symbol_warmup_snapshot(
        self,
        symbol: str,
        *,
        frame: pd.DataFrame | None = None,
        data_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = str(symbol or '').upper().strip()
        merged_frame = frame if frame is not None else (self.data.get_merged(key, with_indicators=False) if key else None)
        merged_rows = 0 if merged_frame is None else len(merged_frame)
        required_bars = max(0, int(self._required_history_bars(key) or 0))
        ready = required_bars <= 0 or merged_rows >= required_bars
        state = dict(data_state or (self.data.symbol_data_state(key) if key else {}))
        history_rows = max(0, int(state.get("history_rows", 0) or 0))
        live_rows = max(0, int(state.get("live_rows", 0) or 0))
        history_last = state.get("last_history_refresh")
        empty_last = state.get("last_empty_history_refresh")
        stream_last = state.get("last_stream_update")
        next_retry_due = None
        retry_delay_seconds = None
        try:
            if not ready and history_last is not None:
                interval = float(self.config.runtime.history_poll_seconds)
                if empty_last is not None and empty_last == history_last and not self.data.is_regular_session(now_et()):
                    interval = max(interval, 900.0)
                retry_delay_seconds = max(0.0, interval - max(0.0, (now_et() - history_last).total_seconds()))
                next_retry_due = history_last + timedelta(seconds=max(interval, 0.0))
        except Exception:
            next_retry_due = None
            retry_delay_seconds = None
        blocking_reason = None if ready else self.strategy.insufficient_bars_reason("insufficient_bars", merged_rows, required_bars)
        live_entry_state = dict(state.get("live_entry_bar_status") or {})
        return {
            "symbol": key,
            "ready": bool(ready),
            "state": "ready" if ready else "warming_up",
            "required_bars": required_bars,
            "current_bars": merged_rows,
            "missing_bars": max(0, required_bars - merged_rows),
            "history_rows": history_rows,
            "live_rows": live_rows,
            "stream_subscribed": bool(state.get("stream_subscribed", False)),
            "quote_cached": bool(state.get("quote_cached", False)),
            "blocking_reason": blocking_reason,
            "entry_live_ready": bool(live_entry_state.get("ready", True)),
            "entry_live_blocker": live_entry_state.get("reason"),
            "last_stream_bar_time": live_entry_state.get("last_stream_bar_time") or (state.get("last_stream_bar_time").isoformat() if hasattr(state.get("last_stream_bar_time"), "isoformat") else state.get("last_stream_bar_time")),
            "last_stream_bar_age_seconds": live_entry_state.get("last_stream_bar_age_seconds"),
            "last_history_refresh": history_last.isoformat() if hasattr(history_last, "isoformat") else history_last,
            "last_empty_history_refresh": empty_last.isoformat() if hasattr(empty_last, "isoformat") else empty_last,
            "last_stream_update": stream_last.isoformat() if hasattr(stream_last, "isoformat") else stream_last,
            "next_retry_due": next_retry_due.isoformat() if hasattr(next_retry_due, "isoformat") else next_retry_due,
            "retry_delay_seconds": retry_delay_seconds,
            "forced_premarket_refresh_date": (lambda _v: _v.isoformat() if hasattr(_v, "isoformat") else _v)(state.get("forced_premarket_refresh_date")),
        }

    def warmup_summary(self, symbols: list[str], bars: Mapping[str, pd.DataFrame | None] | None = None) -> dict[str, Any]:
        ordered = [str(symbol or '').upper().strip() for symbol in symbols if str(symbol or '').upper().strip()]
        snapshots: list[dict[str, Any]] = []
        for symbol in ordered:
            frame = None if bars is None else bars.get(symbol)
            snapshots.append(self._symbol_warmup_snapshot(symbol, frame=frame))
        total = len(snapshots)
        ready_count = sum(1 for item in snapshots if bool(item.get("ready")))
        blocked = [item for item in snapshots if not bool(item.get("ready"))]
        blocked_labels = [f'{item["symbol"]}({item["current_bars"]}/{item["required_bars"]})' for item in blocked]
        summary = {
            "ready": ready_count >= total if total else True,
            "ready_count": ready_count,
            "total": total,
            "blocked_count": len(blocked),
            "symbols": snapshots,
            "blocked_symbols": [item.get("symbol") for item in blocked],
            "summary": f"{ready_count}/{total} ready" if total else "0/0 ready",
            "blocked_summary": ", ".join(blocked_labels) if blocked_labels else "",
        }
        return summary

    def log_warmup_summary(self, summary: Mapping[str, Any]) -> None:
        total = int(summary.get("total", 0) or 0)
        if total <= 0:
            return
        ready_count = int(summary.get("ready_count", 0) or 0)
        blocked_summary = str(summary.get("blocked_summary") or "").strip()
        signature = f"{ready_count}/{total}|{blocked_summary}"
        message = f"Warmup status strategy={self.config.strategy} ready={ready_count}/{total}"
        if blocked_summary:
            message += f" blocked={blocked_summary}"
        self.audit.log_cycle(
            f"warmup:{self.config.strategy}",
            signature,
            message,
            interval=30.0,
            level=logging.DEBUG,
        )
