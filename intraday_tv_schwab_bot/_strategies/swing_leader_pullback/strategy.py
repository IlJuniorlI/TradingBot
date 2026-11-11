# SPDX-License-Identifier: MIT
"""swing_leader_pullback: buy a momentum leader's short pullback, hold for days.

The rule is ``rule.py``'s (README): a leader the screener ranks, whose last
completed session closed above its trend SMA with a short RSI under the bar,
is bought near the next open; the position is sold near a session's close
once the price stands above its exit SMA, or after ``max_hold_sessions``
sessions, and a stop ``stop_atr_mult`` daily ATRs under the entry protects
it meanwhile. No target, no trail, no force flatten: it is held across
sessions.

Dry run only. A live run is refused at build: the live path's resting stop
is a DAY order and a restart restores no multi-day position (README, "What
is not built").
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from ... import sessions
from ...models import Candidate, ExitDecision, Position, Side, Signal
from ...numeric import safe_float
from ..shared_entry import EntryProposal
from ..strategy_base import BaseStrategy
from . import rule

LOG = logging.getLogger(__name__)

# Sessions of the dollar-volume floor's median.
DOLLAR_VOLUME_SESSIONS = 20
# The exit check sits in the regular session's last hour: an HH:MM from
# 15:00 to 15:59, read as that many minutes before the session's close, so
# an early-close day checks as long before its own close.
EXIT_CHECK_EARLIEST, EXIT_CHECK_LATEST = "15:00", "15:59"

# (key, whole number, low, low inclusive, high, high inclusive)
# The session counts are capped at MAX_SESSIONS: each one is days of history
# the engine fetches (``daily_history_calendar_days``), and a count no
# history could fill would switch its rule off without a word.
MAX_SESSIONS = 1000
NUMBER_KNOBS: tuple[tuple[str, bool, float | None, bool, float | None, bool], ...] = (
    ("min_universe_size", True, 1, True, None, True),
    ("leader_fraction", False, 0.0, False, 1.0, True),
    ("min_price", False, 0.0, True, None, True),
    ("min_dollar_volume", False, 0.0, True, None, True),
    ("trend_sma", True, 2, True, MAX_SESSIONS, True),
    ("rsi_length", True, 2, True, MAX_SESSIONS, True),
    ("rsi_max", False, 0.0, False, 100.0, False),
    ("atr_length", True, 2, True, MAX_SESSIONS, True),
    ("stop_atr_mult", False, 0.0, False, None, True),
    ("exit_sma", True, 2, True, MAX_SESSIONS, True),
    ("max_hold_sessions", True, 1, True, MAX_SESSIONS, True),
)


def _is_finite_number(value: Any) -> bool:
    """A finite int or float, no bool. An int too large for a float is not
    one (``math.isfinite`` raises OverflowError on it)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def checked_knobs(params: Mapping[str, Any], strategy_name: str) -> None:
    """Refuse, with a ValueError naming the key, a missing knob, a number
    of ``NUMBER_KNOBS`` that is not finite or in its bounds (a whole number
    where it counts sessions), an ``index_universe`` that is not a list of
    TradingView index ids, an ``exit_check_time`` outside the session's last
    hour, and a ``force_flatten`` block (the strategy holds across sessions
    and never force-flattens, so the key would do nothing). The load runs it
    on every params block a config sets, and the strategy again when built."""
    prefix = f"strategies.{strategy_name}.params"
    for key, whole, low, low_ok, high, high_ok in NUMBER_KNOBS:
        if key not in params:
            raise ValueError(f"{prefix}.{key} is missing: the strategy's manifest declares it")
        value = params[key]
        bad = not _is_finite_number(value)
        if not bad and whole and float(value) != int(value):
            bad = True
        if not bad and low is not None and (value < low or (value == low and not low_ok)):
            bad = True
        if not bad and high is not None and (value > high or (value == high and not high_ok)):
            bad = True
        if bad:
            bounds = []
            if low is not None:
                bounds.append(f"{'>=' if low_ok else '>'} {low}")
            if high is not None:
                bounds.append(f"{'<=' if high_ok else '<'} {high}")
            raise ValueError(f"{prefix}.{key} must be {'a whole number' if whole else 'a finite number'}"
                             f"{' ' + ' and '.join(bounds) if bounds else ''}, got {value!r}")
    if "index_universe" not in params:
        raise ValueError(f"{prefix}.index_universe is missing: the strategy's manifest declares it")
    indexes = params["index_universe"]
    if (not isinstance(indexes, (list, tuple)) or not indexes
            or any(not isinstance(index, str) or not index.startswith("SYML:") or ";" not in index for index in indexes)):
        raise ValueError(f"{prefix}.index_universe must be a non-empty list of TradingView index ids "
                         f"('SYML:<source>;<symbol>'), got {indexes!r}")
    if "exit_check_time" not in params:
        raise ValueError(f"{prefix}.exit_check_time is missing: the strategy's manifest declares it")
    check = params["exit_check_time"]
    if not sessions.is_hhmm(check) or not (sessions.parse_hhmm(EXIT_CHECK_EARLIEST) <= sessions.parse_hhmm(check)
                                           <= sessions.parse_hhmm(EXIT_CHECK_LATEST)):
        raise ValueError(f"{prefix}.exit_check_time must be an HH:MM time from {EXIT_CHECK_EARLIEST} to "
                         f"{EXIT_CHECK_LATEST}, got {check!r}")
    if "force_flatten" in params:
        raise ValueError(f"{prefix}.force_flatten is set, and swing_leader_pullback never force-flattens: it holds "
                         "its positions across sessions. Remove the key.")


def sessions_after(entry_day: date, today: date) -> int:
    """Regular sessions after ``entry_day`` up to and including ``today``
    (0 on the entry session itself), on the exchange calendar."""
    count, day = 0, entry_day
    while day < today:
        day += timedelta(days=1)
        if sessions.is_weekday_session_day(day):
            count += 1
    return count


class SwingLeaderPullbackStrategy(BaseStrategy):
    """Long-only, daily-bar: one proposal per leader with a pullback setup
    (style / family ``pullback``), the stop ``stop_atr_mult`` daily ATRs
    under the price and no target. The exits are the engine's stop and this
    class's ``strategy_exit_signal``."""

    strategy_name = 'swing_leader_pullback'
    time_params = ("exit_check_time",)

    @classmethod
    def normalize_params(cls, params: dict[str, Any]) -> dict[str, Any]:
        out = super().normalize_params(params)
        checked_knobs(out, cls.strategy_name)
        return out

    def __init__(self, config):
        super().__init__(config)
        checked_knobs(self.params, self.strategy_name)
        if not bool(config.schwab.dry_run):
            raise ValueError(
                "swing_leader_pullback runs in a dry run only (schwab.dry_run: true). It holds positions across "
                "sessions, and the live path is not built for that: the resting disaster stop is a DAY order that "
                "expires at each close, and a restart restores no multi-day position with its stop and entry "
                "session. See the strategy's README, 'What is not built'.")
        # The session each symbol was last entered on (from the positions
        # the entry pass sees): one entry a session per symbol, so a
        # position stopped out in the entry window is not bought again on
        # the same setup. The preset's cooldown covers a stop the pass never
        # saw open.
        self._entered_on: dict[str, date] = {}
        # (symbol, day) of the exit checks whose daily history did not read,
        # each warned once.
        self._exit_history_warned: set[tuple[str, date]] = set()

    # -- data the engine fetches for it ------------------------------------

    def daily_history_calendar_days(self) -> int:
        """Calendar days that hold the sessions the rule reads (the trend
        SMA's, the ATR's, the RSI's, the exit SMA's and the dollar-volume
        median's) with ten to spare, at about 250 sessions a year, plus
        three weeks for holidays."""
        need = max(int(self.params["trend_sma"]), int(self.params["atr_length"]) + 1, int(self.params["rsi_length"]) + 1,
                   int(self.params["exit_sma"]), DOLLAR_VOLUME_SESSIONS) + 10
        return int(math.ceil(need * 365.0 / 250.0)) + 21

    def daily_history_symbols(self, watchlist: list[str]) -> list[str]:
        return list(watchlist)

    def active_watchlist(self, candidates: list[Candidate], positions: dict[str, Position]) -> set[str]:
        """The leaders only while one can still be entered today: on a
        session day, from the first screener window's start to the last
        entry window's end. Outside that, the held positions alone, so about
        fifty leaders are not streamed, fetched and given contexts through
        the rest of the day and the next premarket for nothing."""
        now = sessions.now_et()
        schedule = self.config.active_strategy.schedule()
        starts = [window.start for window in schedule.screener_windows]
        ends = [window.end for window in schedule.entry_windows]
        entering = bool(starts and ends and sessions.is_weekday_session_day(now)
                        and min(starts) <= now.time() <= max(ends))
        return super().active_watchlist(candidates if entering else [], positions)

    # -- entries -----------------------------------------------------------

    def _completed_daily(self, symbol: str, data, now: datetime) -> tuple[pd.DataFrame | None, str | None]:
        """The symbol's completed daily bars as the engine's prefetch cached
        them today, or (None, why not): no data store, a fetch that failed
        today, none fetched yet, no bars, or bars that do not end on the
        last completed session."""
        if data is None:
            return None, "daily_history_unavailable"
        if data.daily_history_failed(symbol):
            return None, "daily_history_failed"
        if data.daily_history_due(symbol, retry_failed=False):
            # Not fetched yet today: the engine's prefetch owes it. A read
            # here would fetch on the engine thread, one symbol at a time,
            # with nothing managed meanwhile.
            return None, "daily_history_not_fetched"
        daily = data.get_daily_history(symbol)
        if daily is None or daily.empty:
            return None, "no_daily_history"
        last_day = pd.Timestamp(daily.index[-1]).date()
        expected = sessions.previous_regular_close(now).date()
        if last_day != expected:
            return None, f"daily_history_stale(last={last_day.isoformat()},expected={expected.isoformat()})"
        return daily, None

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position],
                      client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        now = sessions.now_et()
        today = now.date()
        for symbol, position in positions.items():
            if position.strategy == self.strategy_name:
                self._entered_on[symbol] = position.entry_time.date()
        stop_mult = float(self.params["stop_atr_mult"])
        rsi_max = float(self.params["rsi_max"])
        min_price = float(self.params["min_price"])
        min_dollar_volume = float(self.params["min_dollar_volume"])
        for c in candidates:
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue
            if self._entered_on.get(c.symbol) == today:
                self._record_entry_decision(c.symbol, "skipped", ["already_entered_this_session"])
                continue
            frame = bars.get(c.symbol)
            price = None if frame is None or frame.empty else safe_float(frame.iloc[-1].get("close"))
            if price is None or price <= 0.0:
                self._record_entry_decision(c.symbol, "skipped", ["no_intraday_price"])
                continue
            daily, why_not = self._completed_daily(c.symbol, data, now)
            if daily is None:
                self._record_entry_decision(c.symbol, "skipped", [str(why_not)])
                continue
            passes, reason, details = rule.setup_at_close(
                daily, trend_sma=int(self.params["trend_sma"]), rsi_length=int(self.params["rsi_length"]),
                rsi_max=rsi_max, atr_length=int(self.params["atr_length"]))
            atr = details.get("atr")
            if not passes and atr is None:
                # Nothing to hang a stop on: the setup's own reason stands alone.
                self._record_entry_decision(c.symbol, "skipped", [reason])
                continue
            reasons: list[str] = [] if passes else [reason]
            closes = daily["close"].astype(float)
            dollar_volume = float((closes * daily["volume"].astype(float)).iloc[-DOLLAR_VOLUME_SESSIONS:].median())
            if float(closes.iloc[-1]) < min_price:
                reasons.append(f"price_under_min({float(closes.iloc[-1]):.2f}<{min_price:g})")
            if not dollar_volume >= min_dollar_volume:
                reasons.append(f"dollar_volume_under_min({dollar_volume:.0f}<{min_dollar_volume:.0f})")
            stop = price - stop_mult * float(atr)
            if stop <= 0.0:
                # No price level to propose: refused here, with whatever else failed.
                self._record_entry_decision(c.symbol, "skipped", [*reasons, f"stop_not_positive({stop:.4f})"])
                continue
            proposal = EntryProposal(
                candidate=c,
                direction=Side.LONG,
                style="pullback",
                style_family="pullback",
                close=price,
                stop=stop,
                target=None,
                gate_frame=frame,
                sr_frame=frame,
                level_frame=frame,
                data=data,
                pending_reasons=tuple(reasons),
            )
            admitted = self.entry_policy.admit(proposal)
            if admitted is None:
                refusal = self._consume_build_failure_payload(c.symbol, proposal.style)
                self._record_entry_decision(c.symbol, "skipped", refusal["reasons"])
                continue
            management = self._adaptive_management_components(
                Side.LONG, price, admitted.stop, admitted.target, style="pullback", runner_allowed=False,
                continuation_bias=float(admitted.fvg["fvg_continuation_bias"]))
            rsi = float(details["rsi"])
            signal_reason = f"leader_pullback:rsi{rsi:.1f}"
            out.append(self.entry_policy.emit(
                admitted,
                reason=signal_reason,
                # The most oversold first, as the study filled its slots.
                strategy_score=rsi_max - rsi,
                management=management,
                target=admitted.target,
                metadata={
                    "swing_setup_session": pd.Timestamp(daily.index[-1]).date().isoformat(),
                    "swing_entry_session": today.isoformat(),
                    "daily_close": round(float(details["close"]), 4),
                    "daily_trend_sma": round(float(details["trend_sma"]), 4),
                    "daily_rsi": round(rsi, 4),
                    "daily_atr": round(float(atr), 4),
                    "stop_atr_mult": stop_mult,
                    "dollar_volume_median": round(dollar_volume, 0),
                    "momentum_12_1": safe_float(c.metadata.get("momentum_12_1")),
                    "leader_rank": c.metadata.get("leader_rank"),
                    "universe_size": c.metadata.get("universe_size"),
                },
            ))
            self._record_entry_decision(c.symbol, "signal", [signal_reason])
        return out

    # -- exits -------------------------------------------------------------

    def _exit_check_at(self, now: datetime) -> datetime:
        """When today's exit check opens: as long before the session's close
        as ``exit_check_time`` is before 16:00."""
        check = sessions.parse_hhmm(self.params["exit_check_time"])
        lead = timedelta(minutes=16 * 60 - (check.hour * 60 + check.minute))
        return sessions.equity_rth_close_at(now) - lead

    def strategy_exit_signal(self, position: Position, bars: dict[str, pd.DataFrame], tape, data=None) -> ExitDecision | None:
        """The close-time exit (``rule.exit_reason``), judged from the exit
        check on in the regular session: the bounce above the exit SMA, read
        on the completed daily closes and the tape's price for today's, or
        the hold's last session. When the daily closes do not read (logged
        once a day with why), the bounce cannot be judged and only the hold
        limit can exit; the stop is the engine's either way."""
        if position.strategy != self.strategy_name or position.side != Side.LONG:
            return None
        now = sessions.now_et()
        if not sessions.is_regular_equity_session(now) or now < self._exit_check_at(now):
            return None
        today = now.date()
        held = sessions_after(position.entry_time.date(), today)
        daily, why_not = self._completed_daily(position.symbol, data, now)
        if daily is None:
            key = (position.symbol, today)
            if key not in self._exit_history_warned:
                self._exit_history_warned.add(key)
                LOG.warning("swing_leader_pullback: %s's exit check cannot read its daily closes (%s); the bounce "
                            "exit is not judged today, the hold limit and the stop still are.", position.symbol, why_not)
            completed = pd.Series(dtype=float)
        else:
            completed = daily["close"]
        reason = rule.exit_reason(completed, float(tape.close), held, exit_sma=int(self.params["exit_sma"]),
                                  max_hold_sessions=int(self.params["max_hold_sessions"]))
        if reason is None:
            return None
        return ExitDecision(f"swing_exit:{reason}(held={held})", "strategy")
