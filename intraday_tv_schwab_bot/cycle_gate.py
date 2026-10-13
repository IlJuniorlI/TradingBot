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
- ``self.risk`` gives the session's daily loss latch
  (``RiskManager.daily_loss_latch``) and whether its stored risk state is
  still unread (``RiskManager.risk_state_unread``), and
  ``self.day_held_open`` what the engine still holds
  (``IntradayBot._day_held_open``): once the latch is set entries end for
  the day, and with nothing held the gate idles until the next session
  (``evaluate``); while the state is unread entries are blocked.
- ``CycleGateState`` is frozen-ish (no default-factories) to keep its
  ``dataclass(slots=True)`` hash-stable if we ever memoize gate evaluation.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .config import BotConfig
from .models import Position
from .risk import DAILY_LOSS_PROJECTED, RISK_STATE_UNREADABLE, DailyLossLatch, RiskManager
from .sessions import equity_session_state, is_weekday_session_day

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
    # Nothing consumes the watchlist: no position, and no screener,
    # management, stream or prewarm (_should_idle_closed_market_watchlists).
    # The market may be open: since 2026-10-05 the gate idles inside the
    # broker's 07:00-20:00 stream window too.
    idle_closed_market: bool
    # The session's daily loss latch (``RiskManager.daily_loss_latch``),
    # None while the day's realized P&L leaves room for one full trade under
    # -max_daily_loss: from then on no entry is actionable for the day.
    daily_loss_latch: DailyLossLatch | None
    # The latch is set and nothing is held (``IntradayBot._day_held_open``):
    # the gate idles until the next session, as overnight (2026-10-12).
    day_over_idle: bool
    # The session's stored risk state could not be read at the start and is
    # still unread (``RiskManager.risk_state_unread``): no entry is
    # actionable until it is (2026-10-12).
    risk_state_unread: bool


class CycleGate:
    def __init__(
        self,
        config: BotConfig,
        *,
        positions: dict[str, Position],
        executor,
        startup_reconciler,
        risk: RiskManager,
        day_held_open: Callable[[], list[str]],
    ) -> None:
        self.config = config
        self.positions = positions
        self.executor = executor
        self.startup_reconciler = startup_reconciler
        self.risk = risk
        self.day_held_open = day_held_open

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
        if self.config.active_is_option:
            return bool(getattr(session_state, "regular_session", False))
        return getattr(session_state, "equity_order_session", None) is not None

    def _should_idle_closed_market_watchlists(
        self,
        *,
        screening_active: bool,
        management_active: bool,
        streaming_active: bool,
        context_refresh_active: bool,
    ) -> bool:
        """Idle the watchlist when nothing consumes it: no position, and no
        screener, management, stream or prewarm. Inside the broker's
        07:00-20:00 stream window too: until 2026-10-05 that window kept the
        watchlist, and 15:55-20:00 (and 07:00 to the prewarm after an
        overnight run) ran the frame, S/R and context maps on frames nothing
        changed or read, about 70% of a core."""
        if bool(self.positions):
            return False
        return not (screening_active or management_active or streaming_active or context_refresh_active)

    def idle_wake_at(self, now: datetime, schedule: Any, *, day_over: bool) -> datetime | None:
        """When an idle gate next needs a pass: the next window's start, less
        the prewarm (``_should_refresh_market_context``); None with no window
        in the next week. The idle cadence sleeps no later than this, so the
        prewarm, or the window itself with no prewarm, starts on time.
        ``day_over`` (the day-over idle, ``CycleGateState.day_over_idle``):
        the next session's, from tomorrow, since today's windows take no
        entry."""
        if day_over:
            now = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        next_start = self._next_schedule_window_start(now, schedule)
        if next_start is None:
            return None
        prewarm_minutes = max(0, int(self.config.runtime.prewarm_before_windows_minutes))
        return next_start - timedelta(minutes=prewarm_minutes)

    def evaluate(self, now: datetime, schedule: Any) -> CycleGateState:
        """The cycle's gate. Once the session's daily loss latch is set
        (``RiskManager.daily_loss_latch``) no entry is actionable for the
        day; while it is set and nothing is held (``day_held_open``: no
        position, no working entry order, no exit order left to sweep, and
        live, a broker account reconciled to hold nothing more), the gate
        idles until the next session, as it idles overnight: no screening,
        management, stream or prewarm, so no watchlist (2026-10-12). Until
        then a day past ``max_daily_loss`` ran every pass of the rest of it
        (1,441 entry cycles with no signal by 12:19 on H: 2026-10-12). A
        position, an order or a new session ends the idle. While the
        session's stored risk state is unread
        (``RiskManager.risk_state_unread``, which tries the read again when
        it is due) no entry is actionable either."""
        session_state = equity_session_state(now, extended_hours_enabled=bool(self.config.execution.extended_hours_enabled))
        current_time = now.time()
        entry_window_open = schedule.can_enter(current_time)
        management_window_open = schedule.can_manage(current_time)
        # The read of an unread risk state first: it may restore the latch.
        risk_state_unread = self.risk.risk_state_unread()
        daily_loss_latch = self.risk.daily_loss_latch()
        if daily_loss_latch is not None and not self.day_held_open():
            return CycleGateState(
                intraday_session_day=session_state.is_trading_day,
                entry_window_open=entry_window_open,
                entry_actionable=False,
                management_window_open=management_window_open,
                screening_active=False,
                position_monitoring_active=False,
                management_active=False,
                streaming_active=False,
                context_refresh_active=False,
                idle_closed_market=True,
                daily_loss_latch=daily_loss_latch,
                day_over_idle=True,
                risk_state_unread=risk_state_unread,
            )
        entry_actionable = (entry_window_open and self._entries_actionable(session_state)
                            and daily_loss_latch is None and not risk_state_unread)
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
            daily_loss_latch=daily_loss_latch,
            day_over_idle=False,
            risk_state_unread=risk_state_unread,
        )

    def trading_blocked_reason(self, daily_loss_latch: DailyLossLatch | None,
                               risk_state_error: str | None) -> str | None:
        """The dashboard's ``trading_blocked_reason``: the startup
        reconcile's block, then ``risk_state_unreadable`` while the
        session's stored risk state is unread (``risk_state_error``, its
        read's error), then ``daily_loss_limit`` while the session's daily
        loss latch is set; None when none blocks."""
        reasons = [part for part in str(self.startup_reconciler.trading_blocked_reason or "").split(",") if part]
        if risk_state_error is not None:
            reasons.append(RISK_STATE_UNREADABLE)
        if daily_loss_latch is not None:
            reasons.append(DAILY_LOSS_PROJECTED)
        return ",".join(reasons) or None

    @staticmethod
    def daily_loss_text(latch: DailyLossLatch) -> str:
        """``daily loss limit reached at 10:06:43 (realized -415.87, limit
        500, one trade's risk 120)``: when (ET) ``latch`` set, the realized
        P&L that set it, and the ``max_daily_loss`` and the one trade's risk
        it set under. The time tells that P&L from the day's, which a later
        close moves (the dashboard's Day P&L beside it)."""
        def shown(value: float) -> str:
            return f"{value:.0f}" if value == round(value) else f"{value:.2f}"
        return (f"daily loss limit reached at {latch.at:%H:%M:%S} (realized {latch.realized_pnl:.2f}, "
                f"limit {shown(latch.limit)}, one trade's risk {shown(latch.trade_risk)})")

    def runtime_status_message(
        self,
        *,
        screening_active: bool,
        management_active: bool,
        streaming_active: bool,
        context_refresh_active: bool,
        position_monitoring_active: bool = False,
        daily_loss_latch: DailyLossLatch | None,
        day_over_idle: bool,
        risk_state_error: str | None,
    ) -> str:
        """The dashboard's status message. An idle gate (no position,
        screener, management, stream or prewarm) reads "Idle until next
        session window", day and night. Until 2026-10-05 the night read
        "Market closed"; the gate now idles inside the stream window too
        (15:55-20:00 on top_tier, with the market open until 16:00), where
        that would be wrong.

        After the startup reconcile's block, the daily loss latch
        (``daily_loss_text``): "Idle until next session: daily loss limit
        reached at HH:MM:SS (...)" in the day-over idle, and "Entries ended
        for the day: daily loss limit reached at HH:MM:SS (...)" while
        something is still held and managed. Until 2026-10-12 such a day read "Running". Then, while the
        session's stored risk state is unread (``risk_state_error``):
        "Entries blocked: the session risk state could not be read (...)"."""
        if self.startup_reconciler.trading_blocked_message:
            return str(self.startup_reconciler.trading_blocked_message)
        if self.startup_reconciler.trading_blocked_reason:
            return str(self.startup_reconciler.trading_blocked_reason)
        if daily_loss_latch is not None:
            prefix = "Idle until next session" if day_over_idle else "Entries ended for the day"
            return f"{prefix}: {self.daily_loss_text(daily_loss_latch)}"
        if risk_state_error is not None:
            return f"Entries blocked: the session risk state could not be read ({risk_state_error})"
        if position_monitoring_active and not (screening_active or management_active or streaming_active or context_refresh_active):
            return "Position monitor only"
        if context_refresh_active and not (screening_active or management_active or streaming_active):
            return "Prewarm active"
        if not (screening_active or management_active or streaming_active):
            return "Idle until next session window"
        return "Running"
