# SPDX-License-Identifier: MIT
"""CycleGate — evaluates what's actionable this cycle.

Extracted from ``IntradayBot`` as a follow-up to StartupReconciler. Each
cycle, ``step()`` asks the gate: given the current time, schedule, open
positions, and session state, which subsystems should run? The answer is
captured in :class:`CycleGateState` and drives the step loop's dispatch.

Design notes:

- ``self.positions`` is a shared-reference dict; the gate reads the
  current set of open positions but never mutates them.
- ``self.executor`` is used only for ``can_close_position_now(position, now)``
  inside ``_positions_management_actionable``.
- ``self.startup_reconciler`` is read by ``runtime_status_message`` to
  surface trading-blocked state on the dashboard.
- ``CycleGateState`` is frozen-ish (no default-factories) to keep its
  ``dataclass(slots=True)`` hash-stable if we ever memoize gate evaluation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .config import BotConfig
from .models import Position
from ._strategies.registry import is_option_strategy
from .utils import equity_session_state, is_weekday_session_day

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


@dataclass(slots=True)
class CycleGateState:
    intraday_session_day: bool
    entry_window_open: bool
    entry_actionable: bool
    management_window_open: bool
    screening_active: bool
    position_monitoring_active: bool
    management_active: bool
    streaming_active: bool
    context_refresh_active: bool
    idle_closed_market: bool


class CycleGate:
    def __init__(
        self,
        config: BotConfig,
        *,
        positions: dict[str, Position],
        executor,
        startup_reconciler,
    ) -> None:
        self.config = config
        self.positions = positions
        self.executor = executor
        self.startup_reconciler = startup_reconciler

    @staticmethod
    def _next_schedule_window_start(now: datetime, schedule: Any) -> datetime | None:
        windows = list(getattr(schedule, "entry_windows", [])) + list(getattr(schedule, "management_windows", [])) + list(getattr(schedule, "screener_windows", []))
        if not windows:
            return None
        next_start: datetime | None = None
        for day_offset in range(0, 8):
            candidate_day = now + timedelta(days=day_offset)
            if not is_weekday_session_day(candidate_day):
                continue
            for window in windows:
                start_time = getattr(window, "start", None)
                if start_time is None:
                    continue
                candidate_start = candidate_day.replace(
                    hour=start_time.hour,
                    minute=start_time.minute,
                    second=getattr(start_time, "second", 0),
                    microsecond=0,
                )
                if candidate_start < now:
                    continue
                if next_start is None or candidate_start < next_start:
                    next_start = candidate_start
        return next_start

    def _should_refresh_market_context(self, now: datetime, schedule: Any, *, screening_active: bool, management_active: bool, streaming_active: bool) -> bool:
        if screening_active or management_active or streaming_active:
            return True
        prewarm_minutes = max(0, int(self.config.runtime.prewarm_before_windows_minutes))
        if prewarm_minutes <= 0:
            return False
        next_start = self._next_schedule_window_start(now, schedule)
        if next_start is None:
            return False
        seconds_until_next = (next_start - now).total_seconds()
        return 0.0 <= seconds_until_next <= float(prewarm_minutes * 60)

    def _positions_management_actionable(self, now: datetime) -> bool:
        if not bool(self.positions):
            return False
        if self.executor is None:
            return False
        try:
            return any(bool(self.executor.can_close_position_now(position, now)) for position in self.positions.values())
        except Exception:
            LOG.debug("Could not evaluate position management session state; falling back to monitor-only mode.", exc_info=True)
            return False

    def _entries_actionable(self, session_state: Any) -> bool:
        if not bool(getattr(session_state, "is_trading_day", False)):
            return False
        if is_option_strategy(self.config.strategy):
            return bool(getattr(session_state, "regular_session", False))
        return getattr(session_state, "equity_order_session", None) is not None

    def _should_idle_closed_market_watchlists(
        self,
        *,
        market_session_open: bool,
        screening_active: bool,
        management_active: bool,
        streaming_active: bool,
        context_refresh_active: bool,
    ) -> bool:
        if bool(self.positions):
            return False
        if market_session_open:
            return False
        return not (screening_active or management_active or streaming_active or context_refresh_active)

    def evaluate(self, now: datetime, schedule: Any) -> CycleGateState:
        session_state = equity_session_state(now, extended_hours_enabled=bool(self.config.execution.extended_hours_enabled))
        current_time = now.time()
        entry_window_open = schedule.can_enter(current_time)
        entry_actionable = entry_window_open and self._entries_actionable(session_state)
        management_window_open = schedule.can_manage(current_time)
        screener_window_open = schedule.should_screen(current_time)
        position_monitoring_active = bool(self.positions)
        screening_active = session_state.is_trading_day and screener_window_open
        if position_monitoring_active:
            management_active = self._positions_management_actionable(now)
        else:
            management_active = session_state.is_trading_day and management_window_open
        streaming_window_open = entry_window_open or management_window_open or position_monitoring_active
        streaming_active = session_state.stream_available and streaming_window_open
        context_refresh_active = self._should_refresh_market_context(
            now,
            schedule,
            screening_active=screening_active,
            management_active=management_active,
            streaming_active=streaming_active,
        )
        idle_closed_market = self._should_idle_closed_market_watchlists(
            market_session_open=session_state.stream_available,
            screening_active=screening_active,
            management_active=management_active,
            streaming_active=streaming_active,
            context_refresh_active=context_refresh_active,
        )
        return CycleGateState(
            intraday_session_day=session_state.is_trading_day,
            entry_window_open=entry_window_open,
            entry_actionable=entry_actionable,
            management_window_open=management_window_open,
            screening_active=screening_active,
            position_monitoring_active=position_monitoring_active,
            management_active=management_active,
            streaming_active=streaming_active,
            context_refresh_active=context_refresh_active,
            idle_closed_market=idle_closed_market,
        )

    def runtime_status_message(
        self,
        *,
        screening_active: bool,
        management_active: bool,
        streaming_active: bool,
        context_refresh_active: bool,
        idle_closed_market: bool,
        position_monitoring_active: bool = False,
    ) -> str:
        if self.startup_reconciler.trading_blocked_message:
            return str(self.startup_reconciler.trading_blocked_message)
        if self.startup_reconciler.trading_blocked_reason:
            return str(self.startup_reconciler.trading_blocked_reason)
        if idle_closed_market:
            return "Market closed"
        if position_monitoring_active and not (screening_active or management_active or streaming_active or context_refresh_active):
            return "Position monitor only"
        if context_refresh_active and not (screening_active or management_active or streaming_active):
            return "Prewarm active"
        if not (screening_active or management_active or streaming_active):
            return "Idle until next session window"
        return "Running"
