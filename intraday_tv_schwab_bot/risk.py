# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .config import BotConfig
from .models import Position, Side, Signal, is_option_asset
from .numeric import first_float, safe_float
from .trade_management import trail_allowed
from . import sessions

LOG = logging.getLogger(__name__)

# ``can_open``'s daily-loss refusals (``RiskManager.daily_loss_refusal``).
# The day is over: its realized P&L has left no room for one full trade's
# risk under max_daily_loss (the latch, ``DailyLossLatch``), so entries have
# ended for the session.
DAILY_LOSS_REACHED = "daily_loss_limit_reached"
# This entry's stop-out would take the day's worst case to -max_daily_loss.
DAILY_LOSS_PROJECTED = "daily_loss_limit"
# The candidate's own risk to its stop cannot be read, so whether it fits
# under the limit is unknown.
DAILY_LOSS_RISK_UNREADABLE = "daily_loss_risk_unreadable"
# The session's stored risk state could not be read at the start, so the
# day's realized P&L and its latch are unknown (``RiskManager.risk_state_error``).
RISK_STATE_UNREADABLE = "risk_state_unreadable"
# How often a stored risk state the start could not read is read again
# (``RiskManager.risk_state_unread``).
RISK_STATE_RETRY_SECONDS = 60.0
# How long that retry's read waits for a lock another connection holds on
# the file (sqlite's default is 5 s, which the start's own read keeps): it
# runs on the engine's pass, ahead of management (``CycleGate.evaluate``), so
# a file locked by another process costs a pass this much at most, as the day
# ledger's reads and writes do (``DAY_LEDGER_BUSY_SECONDS``). The merge's
# write-back after a read that succeeds is the tally's ordinary save, which
# can still wait sqlite's 5 s once, as every booking's save can.
RISK_STATE_RETRY_BUSY_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class DailyLossLatch:
    """The day over: when (ET) the session's realized P&L first left no
    room for one full trade's risk under ``max_daily_loss``, that realized
    P&L, and the ``max_daily_loss`` and the one trade's risk
    (``RiskManager.trade_risk_budget``) it set under. Set once a session
    (``RiskManager._latch_if_reached``) and kept until the session resets
    (``_reset_if_new_session``), whatever the P&L does after it; persisted
    with the session's tallies (``SessionRiskStateStore``), and cleared by a
    start whose configuration leaves room for a full trade
    (``RiskManager._restored_latch``)."""
    at: datetime
    realized_pnl: float
    limit: float
    trade_risk: float


@dataclass(frozen=True, slots=True)
class ClearedDailyLossLatch:
    """A daily loss latch a start cleared (``RiskManager._restored_latch``):
    the latch, when (ET) the start cleared it, the ``max_daily_loss`` and
    the one trade's risk it started with, which left room for a full trade,
    and the day's realized P&L then. Kept with the session's tallies until
    the session resets, so the day's report still says when the day reached
    its limit, and when and why entries reopened."""
    latch: DailyLossLatch
    at: datetime
    limit: float
    trade_risk: float
    realized_pnl: float


@dataclass(slots=True)
class RecentExitRecord:
    """One record per recent exit, used for the same-level retry block.

    ``entry_price`` is the LEVEL the block keys off — the price we already
    tried. It replaced ``exit_price`` on 2026-07-31: measuring from the exit
    made the block structurally unable to catch a repeat. A stopped-out SHORT
    exits roughly 1R ABOVE its entry, so re-entering the SAME level always
    sits ~1R away from that exit and cleared any sane threshold. Observed on
    AAPL 2026-07-30, five shorts inside $0.37 (331.46 / 331.57 / 331.66 /
    331.78 / 331.83): consecutive entries were 0.05-0.12 apart (0.1-0.25 ATR)
    while the exit-referenced distances were 0.50-1.02 — four to ten times
    larger. Only one of the five was ever blocked.

    ``exit_price`` is retained for logging: it is what the operator sees in
    the block message and is useful when reading the decision back.

    Every exit is recorded, not only losses. The old loser-only rule left a
    re-entry after a WIN completely unchecked, which is how that AAPL run
    alternated win/loss/win/loss/loss through one price for three hours.
    Kept in a list (not a dict keyed by symbol) so multiple exits on the same
    symbol within the window all participate in the check.

    ``side`` and ``entry_price`` are ``RiskManager.same_level_anchor``: for
    an option the UNDERLYING's market direction and its price at entry, and
    ``exit_price`` the underlying's price at exit, so every field sits in the
    price space of ``atr`` (the underlying's). Until 2026-09-25 an option was
    recorded by its premium and its order side.
    """
    symbol: str
    side: Side
    entry_price: float
    exit_price: float
    atr: float
    timestamp: datetime


@dataclass
class RiskState:
    """Per-RiskManager mutable state.

    `slots=True` is intentionally OFF here even though the rest of the
    codebase prefers it. PyCharm's static checker has a known bug where
    field access on `@dataclass(slots=True)` instances is mis-reported as
    "object has no attribute '<field>'" for every slot field. RiskState is
    a singleton (one per RiskManager, one per bot run), so the slots
    memory benefit is ~50 bytes on a single instance — not worth the IDE
    noise on every read/write of these fields.
    """
    realized_pnl: float = 0.0
    # Direction-aware cooldown keyed by (SYMBOL_UPPER, Side). A LONG exit
    # only blocks same-direction re-entry; opposite-direction entries remain
    # allowed so the bot can flip short on a genuine bearish reversal.
    # When ``config.risk.cooldown_direction_aware`` is False, writes mirror
    # both directions to preserve the legacy behavior.
    cooldown_until: dict[tuple[str, "Side"], datetime] = field(default_factory=dict)
    # Recent stopouts for the same-level retry block. Pruned lazily inside
    # can_open; cap at ~100 records in pathological scenarios.
    recent_exits: list[RecentExitRecord] = field(default_factory=list)
    # ET session date that owns the current realized_pnl tally. When the date
    # rolls over, realized_pnl is reset to 0.0 so that max_daily_loss behaves
    # as a per-day gate rather than a per-lifetime cap. Eager-initialized to
    # today's ET date so the field has a concrete `date` type and
    # `_reset_if_new_session` doesn't need a lazy-init branch.
    session_date: date = field(default_factory=lambda: sessions.now_et().date())
    # The session's daily loss latch: None until the realized P&L leaves no
    # room for one full trade's risk under -max_daily_loss, then when and
    # where it did, until the session resets. New entries end for the day
    # once it is set, even if a position still open then closes at a profit.
    daily_loss_latch: DailyLossLatch | None = None
    # The session's latches a start cleared, oldest first, until the session
    # resets: the day's record of them (``daily_loss_limit_report``).
    daily_loss_cleared: list[ClearedDailyLossLatch] = field(default_factory=list)


class RiskManager:
    def __init__(self, config: BotConfig, state_store: Any = None):
        self.config = config
        self.state = RiskState()
        # Optional SessionRiskStateStore. When attached, the per-day tallies
        # survive a restart (see attach_state_store).
        self._state_store = None
        # While the stored state the start could not read stays unread: the
        # read's error, when the next read is due, and the reads that failed
        # (risk_state_unread).
        self._state_unread: str | None = None
        self._state_retry_at: datetime | None = None
        self._state_read_failures = 0
        if state_store is not None:
            self.attach_state_store(state_store)

    # ------------------------------------------------------------------
    # Cross-restart persistence
    # ------------------------------------------------------------------
    def attach_state_store(self, store: Any) -> None:
        """Attach a ``SessionRiskStateStore`` and restore today's tallies.

        Without this the risk counters are memory-only: a restart reset
        ``realized_pnl`` to 0.0 and dropped every cooldown and same-level
        block, so a bot restarted after most of ``max_daily_loss`` was gone
        came back able to lose the whole limit again. Open positions were
        already recovered from the broker; this recovers the counters that
        decide whether to open more.

        Only a row matching today's ET session date is restored, so the daily
        reset still happens by itself. So is the row's daily loss latch: a
        restart keeps new entries ended for the day, unless this start's
        configuration leaves room for a full trade (``_restored_latch``). A
        row whose realized P&L leaves no such room and that holds no latch
        latches at the restore. A row that cannot be read fails closed: new
        entries are blocked (``RISK_STATE_UNREADABLE``) until a read
        succeeds (``risk_state_unread``).
        """
        self._state_store = store
        self._read_stored_state(retry=False)

    def _read_stored_state(self, *, retry: bool) -> None:
        """Read the session's stored row and restore it
        (``_restore_stored_state``). A read that fails leaves the state
        unread, with one WARNING for the run of failures: new entries stay
        blocked (``RISK_STATE_UNREADABLE``), the tallies are not written over
        the row (``_persist_state``), no latch sets (``_latch_if_reached``),
        and the read is tried again ``RISK_STATE_RETRY_SECONDS`` later
        (``risk_state_unread``; ``retry``), waiting at most
        ``RISK_STATE_RETRY_BUSY_SECONDS`` for a lock. Until 2026-10-12 a read
        error started the day flat, its realized P&L read as 0, so a restart
        after a losing day reopened entries."""
        day = self.state.session_date
        try:
            if retry:
                payload = self._state_store.load(day.isoformat(), busy_seconds=RISK_STATE_RETRY_BUSY_SECONDS)
            else:
                payload = self._state_store.load(day.isoformat())
        except Exception as exc:
            self._state_read_failures += 1
            error = f"{type(exc).__name__}: {exc}"
            if self._state_unread is None:
                LOG.warning("Could not read the session risk state for %s (%s): new entries are blocked (%s) until "
                            "it is read, tried again every %.0f s; exits, management and disaster stops run as usual.",
                            day.isoformat(), error, RISK_STATE_UNREADABLE, RISK_STATE_RETRY_SECONDS)
            else:
                LOG.debug("The session risk state for %s is still unreadable (%s), %d reads", day.isoformat(), error,
                          self._state_read_failures)
            self._state_unread = error
            self._state_retry_at = sessions.now_et() + timedelta(seconds=RISK_STATE_RETRY_SECONDS)
            return
        was_unread = self._state_unread is not None
        self._state_unread = None
        self._state_retry_at = None
        if was_unread:
            LOG.info("Read the session risk state for %s after %d failed read(s); new entries are no longer blocked "
                     "by it", day.isoformat(), self._state_read_failures)
            self._state_read_failures = 0
        self._restore_stored_state(payload, merge=was_unread)

    def _restore_stored_state(self, payload: Mapping[str, Any] | None, *, merge: bool) -> None:
        """Restore the session's stored row (``payload``, None when there is
        none). ``merge``: a read after a start that could not read it, so
        memory already holds what the day booked since; that adds to the
        row (its realized P&L, its cooldowns, the later of each, and its
        recent exits), and the merged state is written back. Memory holds
        no latch then (``_latch_if_reached`` sets none while the state is
        unread), and no cleared one (only a restore clears one), so the
        row's latch, or the merged tally, decides it, and the row's cleared
        latches are the day's."""
        if payload:
            self.state.realized_pnl += float(payload.get("realized_pnl", 0.0) or 0.0)
            self.state.daily_loss_cleared = self._readable_cleared(payload["daily_loss_cleared"], "Restore")
            stored_latch = payload.get("daily_loss_latch")
            latch = None if stored_latch is None else self._restored_latch(stored_latch, self.state.realized_pnl)
            if latch is not None:
                self.state.daily_loss_latch = latch
            elif stored_latch is not None:
                merge = True          # cleared: the row keeps it as cleared
            restored_cooldowns = 0
            for row in payload.get("cooldowns") or []:
                try:
                    key = (str(row["symbol"]).upper(), Side(row["side"]))
                    until = datetime.fromisoformat(row["until"])
                    held = self.state.cooldown_until.get(key)
                    self.state.cooldown_until[key] = until if held is None else max(held, until)
                    restored_cooldowns += 1
                except Exception:
                    LOG.debug("Skipping unparseable restored cooldown row: %r", row, exc_info=True)
            restored_exits: list[RecentExitRecord] = []
            for row in payload.get("recent_exits") or []:
                try:
                    restored_exits.append(
                        RecentExitRecord(
                            symbol=str(row["symbol"]).upper(),
                            side=Side(row["side"]),
                            entry_price=float(row["entry_price"]),
                            exit_price=float(row["exit_price"]),
                            atr=float(row["atr"]),
                            timestamp=datetime.fromisoformat(row["timestamp"]),
                        )
                    )
                except Exception:
                    LOG.debug("Skipping unparseable restored exit row: %r", row, exc_info=True)
            self.state.recent_exits = restored_exits + self.state.recent_exits
            latched = self.state.daily_loss_latch
            LOG.info(
                "Restored session risk state for %s: realized_pnl=%.2f cooldowns=%d recent_exits=%d%s",
                self.state.session_date.isoformat(), self.state.realized_pnl, restored_cooldowns,
                len(restored_exits),
                "" if latched is None else
                f" daily_loss_limit_reached={latched.at:%H:%M:%S} (realized {latched.realized_pnl:.2f})",
            )
        if merge:
            self._persist_state()
        self._latch_if_reached(restored=True)

    def _restored_latch(self, row: Mapping[str, Any], day_pnl: float) -> DailyLossLatch | None:
        """The latch a stored row holds, or None when this start's
        configuration leaves room for a full trade: the lower of the
        latch's realized P&L and the day's (``day_pnl``) still above
        ``-(max_daily_loss - trade_risk_budget)``, as after a raised
        ``max_daily_loss``. That clears it with a WARNING naming both
        limits, and the session keeps it as cleared
        (``RiskState.daily_loss_cleared``: when, under which limits, at
        what P&L), for the day's report; a configuration that leaves no room
        keeps it, so a restart alone, or a profit after the latch, never
        lifts it. One whose fields cannot be read is kept (logged with the
        error's type), latched as of now at the day's realized P&L and this
        start's limits."""
        limit, trade_risk = self._daily_loss_limits()
        try:
            latch = self._stored_latch(row)
        except (KeyError, TypeError, ValueError) as exc:
            LOG.warning("The stored daily loss latch %r could not be read (%s: %s); new entries stay ended for "
                        "the day, latched as of the restore", row, type(exc).__name__, exc)
            return DailyLossLatch(at=sessions.now_et(), realized_pnl=float(day_pnl), limit=limit,
                                  trade_risk=trade_risk)
        worst = min(latch.realized_pnl, float(day_pnl))
        if self._no_full_trade_fits(worst, limit, trade_risk):
            return latch
        LOG.warning(
            "The daily loss latch set at %s (realized P&L %.2f under max_daily_loss %.2f with one trade's risk "
            "%.2f) is cleared: this start's max_daily_loss %.2f with one trade's risk %.2f leaves room for a full "
            "trade at the day's realized P&L %.2f. New entries reopen.",
            f"{latch.at:%H:%M:%S}", latch.realized_pnl, latch.limit, latch.trade_risk, limit, trade_risk,
            float(day_pnl),
        )
        self.state.daily_loss_cleared.append(ClearedDailyLossLatch(
            latch=latch, at=sessions.now_et(), limit=limit, trade_risk=trade_risk, realized_pnl=float(day_pnl)))
        return None

    @staticmethod
    def _finite_fields(row: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, float]:
        """``row``'s ``names`` as finite numbers; raises ``KeyError``,
        ``TypeError`` or ``ValueError`` when one is missing or not one."""
        numbers = {}
        for name in names:
            value = float(row[name])
            if not math.isfinite(value):
                raise ValueError(f"{name} is {value!r}")
            numbers[name] = value
        return numbers

    @classmethod
    def _stored_latch(cls, row: Mapping[str, Any]) -> DailyLossLatch:
        """A stored latch's fields; raises ``KeyError``, ``TypeError`` or
        ``ValueError`` when one is missing or not a finite number or time."""
        return DailyLossLatch(at=datetime.fromisoformat(str(row["at"])),
                              **cls._finite_fields(row, ("realized_pnl", "limit", "trade_risk")))

    @classmethod
    def _stored_cleared(cls, row: Mapping[str, Any]) -> ClearedDailyLossLatch:
        """A stored cleared latch (``_cleared_record``); raises as
        ``_stored_latch`` does."""
        cleared = row["cleared"]
        return ClearedDailyLossLatch(latch=cls._stored_latch(row), at=datetime.fromisoformat(str(cleared["at"])),
                                     **cls._finite_fields(cleared, ("limit", "trade_risk", "realized_pnl")))

    @classmethod
    def _readable_cleared(cls, rows: list[Any], where: str) -> list[ClearedDailyLossLatch]:
        """The stored cleared latches (``SessionRiskStateStore.load``'s
        ``daily_loss_cleared``) that can be read; one that cannot is left
        out with a WARNING naming it and the error's type (``where`` says
        who reads them). It gates nothing: a cleared latch is the day's
        record, not a block."""
        readable = []
        for row in rows:
            try:
                readable.append(cls._stored_cleared(row))
            except (KeyError, TypeError, ValueError) as exc:
                LOG.warning("%s: a stored cleared daily loss latch %r could not be read (%s: %s), so the day's "
                            "report leaves it out", where, row, type(exc).__name__, exc)
        return readable

    @staticmethod
    def _latch_record(latch: DailyLossLatch) -> dict[str, Any]:
        """``latch`` as the store keeps it."""
        return {"at": latch.at.isoformat(), "realized_pnl": latch.realized_pnl, "limit": latch.limit,
                "trade_risk": latch.trade_risk}

    @classmethod
    def _cleared_record(cls, cleared: ClearedDailyLossLatch) -> dict[str, Any]:
        """``cleared`` as the store keeps it: its latch's record and, under
        ``cleared``, when, under which limits and at what P&L a start
        cleared it."""
        return {**cls._latch_record(cleared.latch),
                "cleared": {"at": cleared.at.isoformat(), "limit": cleared.limit, "trade_risk": cleared.trade_risk,
                            "realized_pnl": cleared.realized_pnl}}

    @classmethod
    def _cleared_report(cls, cleared: ClearedDailyLossLatch) -> dict[str, Any]:
        """``cleared`` for the SESSION REPORT: its record, P&L to the cent."""
        record = cls._cleared_record(cleared)
        return {**record, "realized_pnl": round(record["realized_pnl"], 2),
                "cleared": {**record["cleared"], "realized_pnl": round(cleared.realized_pnl, 2)}}

    def trade_risk_budget(self) -> float:
        """One full trade's risk: the most the active strategy sizes an
        entry to risk, which the day must still have room for under
        ``max_daily_loss`` (``_latch_if_reached``). An equity strategy's is
        the per-trade risk budget (``entry_risk_budget``:
        ``max_notional_per_trade x risk_per_trade_frac_of_notional``, 120 on
        H: at 15,000 x 0.008); an option strategy's is
        ``options.max_loss_per_trade``, what ``size_option_position`` sizes
        its contracts to. A run enters one kind: an option strategy's
        signals are options (the shared entry stage refuses any other), and
        no equity strategy builds an option signal."""
        if self.config.active_is_option:
            return float(self.config.options.max_loss_per_trade)
        return self.entry_risk_budget()

    def _daily_loss_limits(self) -> tuple[float, float]:
        """``(max_daily_loss, trade_risk_budget)`` as configured now."""
        return abs(float(self.config.risk.max_daily_loss)), self.trade_risk_budget()

    @staticmethod
    def _no_full_trade_fits(realized_pnl: float, limit: float, trade_risk: float) -> bool:
        """Whether ``realized_pnl`` leaves no room for one full trade's risk
        under ``limit``: ``realized_pnl <= -(limit - trade_risk)``. A trade
        risking exactly the room left would take the worst case to the
        limit, which the daily-loss projection refuses."""
        return realized_pnl <= -(limit - trade_risk)

    def _latch_if_reached(self, *, restored: bool = False) -> None:
        """Set the session's daily loss latch the first time its realized
        P&L leaves no room for one full trade's risk under
        ``max_daily_loss`` (``_no_full_trade_fits``; on H: 500 and 120, at
        -380), with one WARNING, and persist it. ``restored``: the tally
        came back from the store that way. Not at the limit itself: an
        entry the daily-loss projection admits rarely takes the realized
        P&L there, and a day without room for a full trade can only refuse
        signals or take smaller ones (H: 2026-10-12 left 84.13 at 10:06:43,
        took four more entries and ended at -521.46).

        Not while the stored state is unread (``risk_state_unread``): memory
        then holds only what the day booked since the start, which may be
        no part of the day's loss, and new entries are blocked anyway. The
        read that succeeds decides on the whole tally
        (``_restore_stored_state``)."""
        if self.state.daily_loss_latch is not None or self._state_unread is not None:
            return
        limit, trade_risk = self._daily_loss_limits()
        if not self._no_full_trade_fits(self.state.realized_pnl, limit, trade_risk):
            return
        self.state.daily_loss_latch = DailyLossLatch(at=sessions.now_et(), realized_pnl=self.state.realized_pnl,
                                                     limit=limit, trade_risk=trade_risk)
        LOG.warning(
            "Daily loss limit reached%s: realized P&L %.2f leaves %.2f under max_daily_loss %.2f, no room for one "
            "trade's risk of %.2f. New entries end for the day (%s) until the session resets; open positions are "
            "managed as usual.",
            " (the restored tally)" if restored else "", self.state.realized_pnl,
            max(0.0, limit + self.state.realized_pnl), limit, trade_risk, DAILY_LOSS_REACHED,
        )
        self._persist_state()

    def daily_loss_latch(self) -> DailyLossLatch | None:
        """The session's daily loss latch (``DailyLossLatch``), None while
        the day's realized P&L leaves room for a full trade. A new session
        resets it first (``_reset_if_new_session``)."""
        self._reset_if_new_session()
        return self.state.daily_loss_latch

    @property
    def risk_state_error(self) -> str | None:
        """The error the session's stored risk state last failed to read
        with, while it stays unread (``risk_state_unread``); None once it is
        read (or when no store is attached)."""
        return self._state_unread

    def risk_state_unread(self) -> bool:
        """Whether the session's stored risk state is still unread after a
        start that could not read it: its realized P&L and latch are
        unknown, so new entries stay blocked (``RISK_STATE_UNREADABLE``). A
        read is tried again here once ``RISK_STATE_RETRY_SECONDS`` have
        passed since the last that failed, waiting at most
        ``RISK_STATE_RETRY_BUSY_SECONDS`` for a lock, since the cycle gate
        calls this ahead of management; one that succeeds restores the row
        onto what the day booked since (``_restore_stored_state``)."""
        if self._state_unread is None:
            return False
        self._reset_if_new_session()
        if sessions.now_et() >= self._state_retry_at:
            self._read_stored_state(retry=True)
        return self._state_unread is not None

    def daily_loss_limit_report(self, day: date) -> dict[str, Any]:
        """``day``'s daily loss limit for its SESSION REPORT. ``reached``:
        whether the day's realized P&L left no room for one full trade
        under ``max_daily_loss``; ``at``, ``realized_pnl``, ``limit`` and
        ``trade_risk``: when (ET), at what P&L and under which limit and
        one trade's risk its latest latch set (this configuration's limits
        when none did); ``cleared``: None while that latch holds, else when
        a start cleared it, the limits it started with and the day's P&L
        then (``ClearedDailyLossLatch``); ``earlier``: the day's earlier
        latches, each one a start cleared, oldest first (a raised limit
        reopened entries and a later loss set the latch again).

        The session's own from memory; an earlier day's from its stored row
        (a day without one booked no P&L, so it never set). ``reached`` None
        when neither can be read: no store, a row that cannot be read, or
        the session's own while its stored state is unread
        (``risk_state_unread``). A stored latch whose fields cannot be read
        is reached, at an unknown time (``at`` None), with a WARNING; a
        cleared one that cannot be read is left out with a WARNING."""
        limit, trade_risk = self._daily_loss_limits()
        unknown = {"limit": limit, "trade_risk": trade_risk, "reached": None, "at": None, "realized_pnl": None,
                   "cleared": None, "earlier": []}
        if day == self.state.session_date:
            if self._state_unread is not None:
                return unknown
            latch, cleared = self.state.daily_loss_latch, list(self.state.daily_loss_cleared)
        elif self._state_store is None:
            return unknown
        else:
            try:
                row = self._state_store.load(day.isoformat())
            except Exception as exc:
                LOG.warning("Session report %s: its stored risk state could not be read (%s: %s), so whether the "
                            "day reached its daily loss limit is unknown", day.isoformat(), type(exc).__name__, exc)
                return unknown
            if row is None:
                return {**unknown, "reached": False}
            cleared = self._readable_cleared(row["daily_loss_cleared"], f"Session report {day.isoformat()}")
            stored = row["daily_loss_latch"]
            try:
                latch = None if stored is None else self._stored_latch(stored)
            except (KeyError, TypeError, ValueError) as exc:
                LOG.warning("Session report %s: its stored daily loss latch %r could not be read (%s: %s); the day "
                            "reached its daily loss limit at an unknown time", day.isoformat(), stored,
                            type(exc).__name__, exc)
                return {**unknown, "reached": True,
                        "realized_pnl": safe_float(stored.get("realized_pnl"), None, finite=True),
                        "earlier": [self._cleared_report(record) for record in cleared]}
        earlier = [self._cleared_report(record) for record in cleared]
        if latch is not None:
            record = self._latch_record(latch)
            return {"reached": True, **record, "realized_pnl": round(record["realized_pnl"], 2), "cleared": None,
                    "earlier": earlier}
        if earlier:
            return {"reached": True, **earlier[-1], "earlier": earlier[:-1]}
        return {**unknown, "reached": False}

    def _persist_state(self) -> None:
        """Write the current tallies. No-op when no store is attached, and
        while the stored state is unread (``risk_state_unread``): memory then
        holds only what the day booked since the start, which must not be
        written over the row."""
        if self._state_store is None or self._state_unread is not None:
            return
        cooldowns = [
            {"symbol": symbol, "side": side.value, "until": until.isoformat()}
            for (symbol, side), until in self.state.cooldown_until.items()
        ]
        recent_exits = [
            {
                "symbol": record.symbol,
                "side": record.side.value,
                "entry_price": record.entry_price,
                "exit_price": record.exit_price,
                "atr": record.atr,
                "timestamp": record.timestamp.isoformat(),
            }
            for record in self.state.recent_exits
        ]
        latch = self.state.daily_loss_latch
        self._state_store.save(
            self.state.session_date.isoformat(), self.state.realized_pnl, cooldowns, recent_exits,
            None if latch is None else self._latch_record(latch),
            [self._cleared_record(cleared) for cleared in self.state.daily_loss_cleared],
        )

    @staticmethod
    def floor_discrete_units(budget: float, unit_cost: float) -> int:
        # No units unless both are finite numbers: math.floor raised on a NaN
        # or infinite ratio, which ended the whole entry cycle (2026-09-26).
        budget_value = safe_float(budget, finite=True)
        unit_value = safe_float(unit_cost, finite=True)
        if budget_value is None or unit_value is None or budget_value <= 0 or unit_value <= 0:
            return 0
        ratio = budget_value / unit_value
        return max(0, int(math.floor(ratio + 1e-9)))

    @staticmethod
    def _symbol_key(symbol: str | None) -> str:
        return str(symbol or "").upper().strip()

    def is_symbol_on_cooldown(self, symbol: str, side: "Side | None" = None) -> bool:
        """Check whether ``symbol`` is on cooldown for ``side``.

        With ``cooldown_direction_aware: true`` (the shipped default), the
        write side only stamps the direction that just exited, so reads
        should pass the candidate's intended ``side`` when known to avoid
        blocking the opposite direction. When ``side`` is None — strategies
        whose screener doesn't pre-classify direction (e.g.
        peer_confirmed_key_levels) or test fixtures — this returns True if
        EITHER direction is on cooldown, the conservative fallback.
        """
        key = self._symbol_key(symbol)
        now = sessions.now_et()
        if side is not None:
            until = self.state.cooldown_until.get((key, side))
            return bool(until and now < until)
        for (stored_key, _stored_side), until in self.state.cooldown_until.items():
            if stored_key == key and until and now < until:
                return True
        return False

    def _reset_if_new_session(self) -> None:
        """Reset per-day realized P&L, and the daily loss latch and the
        latches a start cleared, when the ET session date rolls over.

        Without this, ``max_daily_loss`` accumulates across days on continuous
        multi-day runs and one losing session blocks all future entries until
        the process is restarted. ``session_date`` is eager-initialized in
        ``RiskState`` to today's ET date, so there is no ``None`` initial
        state to handle here.
        """
        current_date = sessions.now_et().date()
        if current_date != self.state.session_date:
            latch = self.state.daily_loss_latch
            if self.state.realized_pnl != 0.0 or latch is not None:
                LOG.info(
                    "Risk session rollover %s -> %s: resetting realized_pnl=%.2f%s",
                    self.state.session_date,
                    current_date,
                    self.state.realized_pnl,
                    "" if latch is None else f" and the daily loss limit reached at {latch.at:%H:%M:%S}",
                )
            self.state.realized_pnl = 0.0
            self.state.daily_loss_latch = None
            self.state.daily_loss_cleared = []
            self.state.session_date = current_date
            self._persist_state()

    def register_realized_pnl(self, pnl: float) -> None:
        """Add *pnl* to today's realized tally and persist it.

        Persisting HERE rather than only in ``register_exit`` matters: a
        PARTIAL exit books P&L through this method directly and never reaches
        ``register_exit`` -- a broker fill of part of an exit order or of a
        bracket child, and a deliberate scale-out (the shared divergence
        partial exit, ``ExitDecision.fraction < 1``). Persisting only on full
        exits would leave those slices' P&L unrecoverable after a restart,
        which is precisely the hole the state store exists to close. (The
        ``adaptive_ladder`` rungs are NOT partials: they move the stop and
        target and never scale out -- this docstring said otherwise until
        2026-09-24.) ``register_exit`` persists again once it has also
        updated the cooldown and same-level log; an extra sqlite write per
        exit is not worth avoiding.
        """
        self._reset_if_new_session()
        self.state.realized_pnl += float(pnl)
        self._persist_state()
        self._latch_if_reached()

    def register_exit(
        self,
        symbol: str,
        pnl: float,
        *,
        additional_symbol: str | None = None,
        side: "Side | None" = None,
        level: "tuple[Side, float] | None" = None,
        exit_price: float | None = None,
        atr: float | None = None,
    ) -> None:
        """Record an exit: update realized_pnl, cooldown, and recent-exit log.

        ``side`` — when provided, drives direction-aware cooldown (the ORDER
        side, which is what ``can_open`` checks). Callers that pass only
        symbol/pnl get both-direction cooldown.
        ``level`` + ``atr`` — when provided, a ``RecentExitRecord`` is
        appended so ``can_open`` can enforce the same-level retry block.
        ``level`` is ``same_level_anchor`` of the closed position: the
        direction and the price already tried (an option's are the
        underlying's). ``exit_price`` is carried for logging only, in the same
        price space. Every exit is recorded, win or loss.
        """
        self.register_realized_pnl(pnl)
        key = self._symbol_key(symbol)
        extra_key = self._symbol_key(additional_symbol) if additional_symbol else None
        # cooldown, immediate or rest_of_day (checked at load, _CHOICES).
        policy = self.config.risk.reentry_policy
        direction_aware = self.config.risk.cooldown_direction_aware
        sides_to_write: list[Side]
        if side is None or not direction_aware:
            sides_to_write = [Side.LONG, Side.SHORT]
        else:
            sides_to_write = [side]

        def _apply(cooldown_ts: datetime | None) -> None:
            keys = [key] + ([extra_key] if extra_key and extra_key != key else [])
            for k in keys:
                for s in sides_to_write:
                    if cooldown_ts is None:
                        self.state.cooldown_until.pop((k, s), None)
                    else:
                        self.state.cooldown_until[(k, s)] = cooldown_ts

        if policy == "immediate":
            _apply(None)
        elif policy == "rest_of_day":
            _apply((sessions.now_et() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
        else:
            _apply(sessions.now_et() + timedelta(minutes=self.config.risk.cooldown_minutes))

        # Record the exit for the same-level retry block. Needs the LEVEL (the
        # direction and entry price being re-tried) and an ATR to scale the
        # tolerance; callers without them pass None and the block skips this
        # exit. Every exit is recorded regardless of P&L — see
        # RecentExitRecord for why the old loser-only rule could not catch a
        # repeat sequence.
        if level is not None and atr is not None:
            level_side, level_price = level
            try:
                record = RecentExitRecord(
                    symbol=key,
                    side=level_side,
                    entry_price=float(level_price),
                    exit_price=float(exit_price) if exit_price is not None else float(level_price),
                    atr=max(1e-6, float(atr)),
                    timestamp=sessions.now_et(),
                )
                self.state.recent_exits.append(record)
                # Trim: keep only records within the block window (plus a small
                # margin) to cap memory in pathological sessions.
                window_minutes = max(1, self.config.risk.same_level_block_minutes)
                cutoff = sessions.now_et() - timedelta(minutes=window_minutes * 2)
                self.state.recent_exits = [r for r in self.state.recent_exits if r.timestamp >= cutoff]
            except Exception:
                LOG.debug("Could not record exit for same-level block", exc_info=True)

        # One write per exit — the only point where realized_pnl, the
        # cooldowns and the same-level log all change together.
        self._persist_state()

    @staticmethod
    def open_risk_to_stops(positions: dict[str, Position]) -> float:
        """Total remaining loss if every open position stopped out right now.

        Measured to each position's CURRENT stop, so a stop trailed to
        breakeven or beyond contributes 0.0 rather than a negative (that is
        locked-in profit, not risk, and netting it off would understate the
        downside).

        A position whose levels cannot be read contributes 0.0 and is LOGGED.
        Skipping it silently would under-count open risk, which pushes the
        daily-loss projection in the permissive direction — the one case where
        a quiet failure lets the bot keep opening trades it should not.
        """
        total = 0.0
        for key, position in (positions or {}).items():
            try:
                entry = float(position.entry_price)
                stop = float(position.stop_price)
                qty = max(0, int(position.qty))
            except (TypeError, ValueError):
                LOG.warning(
                    "Open-risk projection could not read levels for %s "
                    "(entry=%r stop=%r qty=%r); it contributes 0 to the daily-loss "
                    "projection, which UNDER-counts risk.",
                    key, getattr(position, "entry_price", None),
                    getattr(position, "stop_price", None), getattr(position, "qty", None),
                )
                continue
            if qty <= 0:
                continue
            if entry <= 0 or stop <= 0:
                LOG.warning(
                    "Open-risk projection skipping %s: non-positive entry=%.4f or stop=%.4f. "
                    "Its risk is NOT counted against max_daily_loss.",
                    key, entry, stop,
                )
                continue
            per_unit = (entry - stop) if position.side == Side.LONG else (stop - entry)
            total += max(0.0, per_unit) * qty
        return total

    @staticmethod
    def entry_risk_to_stop(qty: int, entry_price: float, stop_price: float) -> float:
        """What an entry of ``qty`` at ``entry_price`` loses if its stop at
        ``stop_price`` fills: ``qty * |entry - stop|``. A price that is not
        a finite number gives a risk that is not one either, which the
        daily-loss check refuses (``DAILY_LOSS_RISK_UNREADABLE``)."""
        return max(0, int(qty)) * abs(float(entry_price) - float(stop_price))

    def candidate_risk(self, signal: Signal) -> float | None:
        """The candidate's own risk to its stop at the size it would take, as
        the signal tells it before its quote (``can_open``):

        - an option: its max loss per contract (``max_loss_per_contract``)
          times the contracts ``size_option_position`` gives it, the size
          the entry takes;
        - an equity: its stop distance from its intended entry
          (``_signal_entry_price``) times the shares ``size_position`` gives
          it there; its size waits for its quote, and the entry gatekeeper
          checks it again at that limit and size (``daily_loss_refusal``).
          The per-trade risk budget (``entry_risk_budget``, the most any
          equity entry is sized to risk) when the signal names no entry
          price, so its size is not known yet.

        None when it cannot be read (the option's max loss or the equity's
        stop is not a finite number): the daily-loss check then refuses the
        entry (``DAILY_LOSS_RISK_UNREADABLE``)."""
        meta = signal.metadata if isinstance(signal.metadata, dict) else {}
        if is_option_asset(meta):
            per_contract = safe_float(meta.get("max_loss_per_contract"), None, finite=True)
            if per_contract is None:
                return None
            return self.size_option_position(per_contract) * max(0.0, per_contract)
        stop = safe_float(signal.stop_price, None, finite=True)
        if stop is None:
            return None
        entry = self._signal_entry_price(signal)
        if entry is None:
            return self.entry_risk_budget()
        return self.entry_risk_to_stop(self.size_position(entry, stop), entry, stop)

    def daily_loss_refusal(self, positions: dict[str, Position], own_risk: float | None, *,
                           label: str) -> str | None:
        """Why the day cannot take an entry whose own risk to its stop is
        ``own_risk`` (``candidate_risk``, or at its quote's limit and size),
        or None when it can. ``label`` names the entry in the log.

        - ``DAILY_LOSS_REACHED``: the day is over, its realized P&L having
          left no room for one full trade's risk under ``-max_daily_loss``
          (the latch, ``daily_loss_latch``): entries have ended for the day,
          whatever the P&L does after it. Logged once, when it sets.
        - ``RISK_STATE_UNREADABLE``: the session's stored risk state could
          not be read at the start and is still unread
          (``risk_state_unread``), so the day's realized P&L is unknown.
          Logged once, when the read fails.
        - ``DAILY_LOSS_RISK_UNREADABLE``: ``own_risk`` is None or not a
          finite number at or above 0. A WARNING.
        - ``DAILY_LOSS_PROJECTED``: the day's worst case with this entry,
          its realized P&L less the open positions' risk to their current
          stops (``open_risk_to_stops``, when
          ``daily_loss_includes_open_risk`` is on) less ``own_risk``, is at
          or past ``-max_daily_loss``. A WARNING. Comparing realized P&L
          alone let the bot keep opening trades until the realized damage
          reached the limit, with max_positions still open at full risk; and
          until 2026-10-12 the entry's own risk was left out, so an entry
          could take the worst case past the limit by up to its risk (H:
          2026-10-07: META SHORT to about -501 and NOW SHORT to about -508
          against 500; 2026-10-12: VRT LONG at -457.60 with 54.93 to its
          stop, stopped for -63.86, -521.46)."""
        # The read of an unread state first (it may restore the latch).
        unread = self.risk_state_unread()
        if self.daily_loss_latch() is not None:
            return DAILY_LOSS_REACHED
        if unread:
            return RISK_STATE_UNREADABLE
        limit = abs(self.config.risk.max_daily_loss)
        own = safe_float(own_risk, None, finite=True)
        if own is None or own < 0:
            LOG.warning("Daily loss check: the own risk to its stop of %s cannot be read (%r); refusing it, since "
                        "whether it fits under max_daily_loss %.2f is unknown", label, own_risk, limit)
            return DAILY_LOSS_RISK_UNREADABLE
        open_risk = (
            self.open_risk_to_stops(positions)
            if self.config.risk.daily_loss_includes_open_risk
            else 0.0
        )
        projected_pnl = self.state.realized_pnl - open_risk - own
        if projected_pnl <= -limit:
            LOG.warning(
                "Daily loss limit: realized_pnl=%.2f open_risk=%.2f own_risk=%.2f projected=%.2f "
                "limit=%.2f — blocking %s",
                self.state.realized_pnl, open_risk, own, projected_pnl, limit, label,
            )
            return DAILY_LOSS_PROJECTED
        return None

    def can_open(self, signal: Signal, positions: dict[str, Position]) -> tuple[bool, str]:
        # Daily loss gate first (daily_loss_refusal): the latch, then the
        # day's worst case with this entry's own risk at the size it would
        # take (candidate_risk).
        refusal = self.daily_loss_refusal(positions, self.candidate_risk(signal),
                                          label=f"{signal.symbol} {signal.side.value}")
        if refusal is not None:
            return False, refusal
        max_positions = self.config.risk.max_positions
        if is_option_asset(signal.metadata):
            max_positions = min(max_positions, len(self.config.options.underlyings))
        active_slots: set[str] = set()
        for pos in positions.values():
            pair_id = str(pos.pair_id).strip() if pos.pair_id is not None else ""
            if pair_id:
                active_slots.add(f"pair:{pair_id}")
            else:
                active_slots.add(f"symbol:{pos.symbol}")
        signal_pair_id = str(signal.pair_id).strip() if signal.pair_id is not None else ""
        raw_key = signal.metadata.get("position_key") if isinstance(signal.metadata, dict) else None
        key = self._symbol_key(raw_key or signal.symbol)
        signal_slot = f"pair:{signal_pair_id}" if signal_pair_id else f"symbol:{key}"
        if signal_slot not in active_slots and len(active_slots) >= max_positions:
            return False, "max_positions"
        # Correlation concentration guard — block when too many same-direction
        # positions already sit in the same correlated group.
        #
        # Configured per-strategy in params.correlation_groups /
        # params.max_same_correlation_group_same_direction. These are
        # DELIBERATELY separate from params.sector_groups, which exists to
        # route a symbol to its confirmation ETF and must stay at GICS
        # granularity. Risk grouping wants the opposite: on a mega-cap
        # universe the tech, communication and consumer-discretionary names
        # trade as one book (roughly 0.85 correlated on any macro day), so
        # treating them as three independent sectors let one directional bet
        # fill every position slot while appearing diversified.
        try:
            strategy_params = self.config.strategies.get(signal.strategy, self.config.active_strategy).params or {}
        except Exception:
            # A guard that cannot read its settings refuses. Until 2026-09-26
            # this logged and switched the guard off for the signal
            # (max_group fell to 0): loud, but a risk control failing open.
            LOG.warning(
                "Could not read strategy params for %s; refusing the signal rather than "
                "open it without the correlation concentration guard.", signal.strategy, exc_info=True,
            )
            return False, "correlation_guard_unavailable"
        max_group = int(strategy_params.get("max_same_correlation_group_same_direction", 0) or 0)
        if max_group > 0:
            correlation_groups = strategy_params.get("correlation_groups") or {}
            signal_symbol = self._symbol_key(signal.symbol)
            signal_group = None
            for group, members in correlation_groups.items():
                if signal_symbol in {self._symbol_key(m) for m in (members or [])}:
                    signal_group = group
                    break
            if signal_group is not None:
                group_members = {self._symbol_key(m) for m in correlation_groups.get(signal_group, [])}
                same_group_same_dir = sum(
                    1 for pos in positions.values()
                    if pos.side == signal.side and self._symbol_key(pos.symbol) in group_members
                )
                if same_group_same_dir >= max_group:
                    LOG.warning(
                        "Correlation concentration limit: %s %s blocked — %d/%d %s positions in group '%s'",
                        signal.symbol, signal.side.value, same_group_same_dir, max_group,
                        signal.side.value, signal_group,
                    )
                    return False, "correlation_concentration"
        if key in positions:
            return False, "already_in_position"
        # Direction-aware cooldown: a LONG exit only blocks a LONG re-entry;
        # the opposite direction remains allowed so the bot can flip on a
        # genuine reversal. When config.risk.cooldown_direction_aware=False,
        # both directions were written at exit time so this still blocks
        # everything (legacy).
        if self.is_symbol_on_cooldown(signal.symbol, signal.side):
            return False, "cooldown"
        if raw_key and raw_key != signal.symbol and self.is_symbol_on_cooldown(raw_key, signal.side):
            return False, "cooldown"
        # Same-level retry block: if a same-direction exit on this symbol is
        # recent AND the current entry candidate is within N*ATR of that
        # exit's entry level, block unless the fib-pullback override applies.
        blocked, block_reason = self._same_level_block_check(signal)
        if blocked:
            return False, block_reason
        if signal.side == Side.SHORT and not self.config.risk.allow_short and not is_option_asset(signal.metadata):
            return False, "shorts_disabled"
        return True, "ok"

    def _same_level_block_check(self, signal: Signal) -> tuple[bool, str]:
        """Enforce the same-level retry block with a fib-pullback exception.

        Returns ``(blocked, reason)``. Iterates recent exits; a record
        blocks the signal iff:
          - same symbol (or its underlying key)
          - same direction (``same_level_anchor``: an option's market
            direction, never its order side)
          - within ``same_level_block_minutes`` of the prior exit
          - |signal level - prior level| <= same_level_block_atr_mult * atr,
            the level being the entry price (an option's underlying's)

        If all four hold, the fib-pullback check runs: if the signal's entry
        price is inside the [0.5, 0.786] retracement band of the swing
        captured in signal.metadata (tech_fib_anchor_low/high), the block
        is overridden — the entry is a proper pullback, not a breakout
        chase.
        """
        window_minutes = self.config.risk.same_level_block_minutes
        atr_mult = self.config.risk.same_level_block_atr_mult
        if window_minutes <= 0 or atr_mult <= 0 or not self.state.recent_exits:
            return False, "ok"
        key = self._symbol_key(signal.symbol)
        meta = signal.metadata if isinstance(signal.metadata, dict) else {}
        raw_key = meta.get("position_key") if isinstance(meta, dict) else None
        raw_key_norm = self._symbol_key(raw_key) if raw_key else None
        anchor = self.same_level_anchor(signal.side, signal.metadata, self._signal_entry_price(signal))
        if anchor is None:
            return False, "ok"
        signal_side, signal_entry = anchor
        cutoff = sessions.now_et() - timedelta(minutes=window_minutes)
        for record in reversed(self.state.recent_exits):
            if record.timestamp < cutoff:
                continue
            if record.side != signal_side:
                continue
            if record.symbol != key and record.symbol != (raw_key_norm or key):
                continue
            threshold = atr_mult * record.atr
            if threshold <= 0:
                continue
            # Distance to the LEVEL WE ALREADY TRIED, not to where that
            # attempt was closed out. See RecentExitRecord.
            if abs(signal_entry - record.entry_price) > threshold:
                continue
            # Fib-pullback override: allow the entry if it sits inside the
            # [0.5, 0.786] retracement band of the most-recent swing stored
            # in signal metadata. A LONG retrace back to the 0.5-0.786 zone
            # of a bullish impulse is a proper pullback entry, not a chase.
            if self._fib_pullback_override(signal, signal_entry):
                LOG.info(
                    "Same-level block overridden by fib-pullback for %s %s entry=%.4f "
                    "prior_entry=%.4f prior_exit=%.4f atr=%.4f",
                    signal.symbol, signal.side.value, signal_entry,
                    record.entry_price, record.exit_price, record.atr,
                )
                return False, "ok"
            return True, "same_level_retry_block"
        return False, "ok"

    @staticmethod
    def market_side(side: Side, metadata: Mapping[str, Any]) -> Side | None:
        """The way a signal, or the position it became, bets on its symbol's
        price. An equity: its side. An option (``is_option_asset``): the
        UNDERLYING's market direction (``metadata['direction']``, bullish* /
        bearish*, stamped by every option signal builder and carried onto the
        position), since its side is the ORDER side: a bear put debit is
        bought and a bull put credit sold. None when an option's direction
        cannot be read.
        """
        if not is_option_asset(metadata):
            return side
        market = str(metadata.get("direction") or "").strip().lower()
        if market.startswith("bullish"):
            return Side.LONG
        if market.startswith("bearish"):
            return Side.SHORT
        return None

    @staticmethod
    def same_level_anchor(side: Side, metadata: Mapping[str, Any], price: Any) -> tuple[Side, float] | None:
        """The (direction, price level) the same-level retry block keys on,
        read the same way from a signal and from the position it became.

        The direction is ``market_side``. The level is an equity's ``price``
        (the signal's intended entry, the position's fill) and an option's
        underlying's price at entry (``metadata['underlying_entry']``,
        stamped by every option signal builder and carried onto the
        position). An option's premium is not a level -- the ATR the block
        scales by is the underlying's -- and matching on its order side
        blocked flips and missed the same bet through a different structure
        (fixed 2026-09-25). None when either cannot be read; the block then
        skips.
        """
        side = RiskManager.market_side(side, metadata)
        if side is None:
            return None
        if is_option_asset(metadata):
            price = metadata.get("underlying_entry")
        try:
            value = float(price)
        except (TypeError, ValueError):
            return None
        return (side, value) if math.isfinite(value) and value > 0 else None

    @staticmethod
    def _signal_entry_price(signal: Signal) -> float | None:
        return first_float(signal.metadata, "entry_price", "limit_price", "mark_price_hint", positive=True, finite=True)

    @staticmethod
    def _fib_pullback_override(signal: Signal, entry_price: float) -> bool:
        """True iff ``entry_price`` is inside the [0.5, 0.786] retracement
        band of the swing anchored by
        ``signal.metadata['tech_fib_anchor_low']`` and
        ``['tech_fib_anchor_high']``. Direction-aware: for a LONG entry we
        expect a bullish swing (low→high) with pullback DOWN; for SHORT we
        expect a bearish swing (high→low) with pullback UP.

        Keys are ``tech_``-prefixed because the shared entry stage stamps the
        ``_technical_lists(ctx, prefix="tech")`` of the frame a proposal was
        gated on onto every signal it emits.

        Never for an option signal. Its side is the ORDER side (a bull put
        credit spread is sold), so the direction check against the swing
        means nothing for it and could lift a legitimate block. The caller
        passes the underlying's entry (``same_level_anchor``, 2026-09-25), the
        same price space as the anchors. Option signals started carrying the
        anchors when their entries moved onto the shared stage (2026-09-24).
        """
        if is_option_asset(signal.metadata):
            return False
        meta = signal.metadata if isinstance(signal.metadata, dict) else {}
        try:
            anchor_low = float(meta.get("tech_fib_anchor_low"))
            anchor_high = float(meta.get("tech_fib_anchor_high"))
        except (TypeError, ValueError):
            return False
        # Chained: anchor_low > 0 and anchor_high > anchor_low implies anchor_high > 0.
        if not 0 < anchor_low < anchor_high:
            return False
        swing = anchor_high - anchor_low
        direction = str(meta.get("tech_fib_direction") or "").strip().lower()
        if signal.side == Side.LONG:
            if direction and direction != "bullish":
                return False
            # Pullback zone for LONG entry on bullish swing: below 0.5 ret,
            # above 0.786 ret. i.e. entry between (high - 0.786*swing) and
            # (high - 0.5*swing).
            lower = anchor_high - 0.786 * swing
            upper = anchor_high - 0.5 * swing
        else:
            if direction and direction != "bearish":
                return False
            # Pullback zone for SHORT on bearish swing: above 0.5 ret, below
            # 0.786 ret. i.e. between (low + 0.5*swing) and (low + 0.786*swing).
            lower = anchor_low + 0.5 * swing
            upper = anchor_low + 0.786 * swing
        if lower > upper:
            lower, upper = upper, lower
        # Generous tolerance — within 5% of swing on either side still counts
        # as a pullback. Tighter bands produce too many false blocks on small
        # intraday swings where tick-level precision doesn't matter.
        tolerance = max(0.01, 0.05 * swing)
        return (lower - tolerance) <= entry_price <= (upper + tolerance)

    @staticmethod
    def position_notional(position: Position) -> float:
        return max(0.0, abs(float(position.entry_price)) * abs(int(position.qty)))

    def current_stock_notional(self, positions: dict[str, Position]) -> float:
        total = 0.0
        for position in positions.values():
            if is_option_asset(position.metadata):
                continue
            total += self.position_notional(position)
        return total

    def remaining_stock_notional_capacity(self, positions: dict[str, Position]) -> float:
        return max(0.0, float(self.config.risk.max_total_notional) - self.current_stock_notional(positions))

    def can_add_stock_notional(self, positions: dict[str, Position], proposed_notional: float) -> tuple[bool, str]:
        if proposed_notional <= 0:
            return False, "invalid_notional"
        if self.current_stock_notional(positions) + proposed_notional > float(self.config.risk.max_total_notional):
            return False, "max_total_notional"
        return True, "ok"

    def entry_risk_budget(self) -> float:
        """Dollar risk allowed on a single trade.

        ``max_notional_per_trade * risk_per_trade_frac_of_notional`` — a
        fraction of the per-trade notional cap, NOT of account equity. See the
        RiskConfig docstring in config.py for the full convention.
        """
        return float(self.config.risk.max_notional_per_trade) * float(
            self.config.risk.risk_per_trade_frac_of_notional
        )

    def entry_slippage_allowance(self, bid: float | None, ask: float | None, reference_price: float) -> float:
        """Expected adverse slippage on an entry, in price units.

        Sizing happens against the previewed limit price, but realized risk is
        ``qty * |fill - stop|``. Padding the stop distance by this amount makes
        the size conservative enough that a fill slipping by up to this much
        still lands inside ``entry_risk_budget``.

        Derived from the live spread, which is the actual cost of crossing,
        and capped at ``entry_slippage_allowance_max_pct`` of price so a single
        pathological quote cannot shrink a position to nothing. Returns 0.0
        when the spread is unusable or the feature is switched off — which
        restores the previous "size on the raw stop distance" behaviour.
        """
        spread_frac = float(self.config.risk.entry_slippage_allowance_spread_frac)
        if spread_frac <= 0.0 or reference_price <= 0:
            return 0.0
        try:
            spread = float(ask) - float(bid)
        except (TypeError, ValueError):
            return 0.0
        if not (spread > 0.0):
            return 0.0
        cap = float(self.config.risk.entry_slippage_allowance_max_pct) * float(reference_price)
        allowance = spread * spread_frac
        return max(0.0, min(allowance, cap) if cap > 0 else allowance)

    def size_position(self, entry_price: float, stop_price: float, *, slippage_allowance: float = 0.0) -> int:
        """Share count for an entry, sized so the trade risks at most
        ``entry_risk_budget`` even if the fill slips by ``slippage_allowance``.

        ``slippage_allowance`` widens the stop distance used for sizing only —
        it never moves the actual stop, which stays where the strategy put it.
        Default 0.0 keeps the original behaviour for callers that do not have
        a spread to work from.
        """
        if entry_price <= 0 or stop_price <= 0:
            return 0
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return 0
        sizing_distance = stop_distance + max(0.0, float(slippage_allowance))
        qty = self.floor_discrete_units(self.entry_risk_budget(), sizing_distance)
        max_qty_by_notional = self.floor_discrete_units(self.config.risk.max_notional_per_trade, entry_price)
        return max(0, min(qty, max_qty_by_notional))

    def realized_entry_risk(self, qty: int, fill_price: float, stop_price: float) -> dict[str, float]:
        """Reconcile the risk actually taken against the budget.

        Returns ``{"risk", "budget", "overage_frac"}`` where ``overage_frac``
        is the fraction ABOVE budget (0.0 when at or under it). Detection
        only: by the time this runs the shares are bought, so there is nothing
        to reject — ``size_position``'s slippage allowance is the preventive
        half of the pair.
        """
        try:
            risk = abs(float(fill_price) - float(stop_price)) * max(0, int(qty))
        except (TypeError, ValueError):
            return {"risk": 0.0, "budget": 0.0, "overage_frac": 0.0}
        budget = self.entry_risk_budget()
        overage = (risk / budget - 1.0) if budget > 0 else 0.0
        return {"risk": risk, "budget": budget, "overage_frac": max(0.0, overage)}

    def realized_option_risk(self, qty: int, max_loss_per_contract: float) -> dict[str, float]:
        """Reconcile an option entry against ``options.max_loss_per_trade``.

        The equity counterpart is ``realized_entry_risk``; options had none, so
        a fill worse than the previewed limit went unnoticed. It matters most
        in dry runs, where ``submit_option_vertical`` deliberately walks the
        price toward the natural to model a chase — sweeping the shipped gates
        produced a worst case of 20 contracts booked at $25 of max loss each
        that actually risked $38.79 each, $776 against a $500 budget.

        Detection only: by the time this runs the contracts are filled.
        """
        try:
            risk = max(0.0, float(max_loss_per_contract)) * max(0, int(qty))
        except (TypeError, ValueError):
            return {"risk": 0.0, "budget": 0.0, "overage_frac": 0.0}
        budget = float(self.config.options.max_loss_per_trade)
        overage = (risk / budget - 1.0) if budget > 0 else 0.0
        return {"risk": risk, "budget": budget, "overage_frac": max(0.0, overage)}

    def size_option_position(self, max_loss_per_contract: float) -> int:
        if max_loss_per_contract <= 0:
            return 0
        qty = self.floor_discrete_units(self.config.options.max_loss_per_trade, max_loss_per_contract)
        return max(0, min(qty, self.config.options.max_contracts_per_trade))

    def stock_position_trail_pct(self, metadata: Mapping[str, Any] | None = None,
                                 existing_trail_pct: float | None = None) -> float | None:
        """The ``trail_pct`` a stock position opens with (``EntryGatekeeper``)
        or is restored with (``StartupReconciler``): a restored position's
        own (``existing_trail_pct``), else ``risk.trailing_stop_pct``; None
        when the position does not trail (``trail_allowed``) or the pct is
        null or 0."""
        meta = metadata if isinstance(metadata, dict) else {}
        if not trail_allowed(self.config.risk.trade_management_mode, meta, options=False):
            return None
        candidate = existing_trail_pct
        if candidate is None:
            candidate = self.config.risk.trailing_stop_pct
        if candidate is None:
            return None
        # A finite number: a restored position's trail_pct is read with
        # safe_float, and load_config refuses any risk.trailing_stop_pct but
        # a finite number >= 0 or null (config._NUMBER_CHECKS); 0 is off.
        candidate = float(candidate)
        return candidate if candidate > 0 else None
