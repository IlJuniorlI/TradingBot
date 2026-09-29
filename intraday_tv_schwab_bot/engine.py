# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Iterable

from schwabdev import Client
import pandas as pd

from .audit_logger import AuditLogger
from .dashboard_cache import DashboardCache
from .config import BotConfig
from .cycle_gate import CycleGate, CycleGateState
from .dashboard import DashboardServer
from .data_feed import MarketDataStore
from .entry_gatekeeper import EntryGatekeeper
from .execution import SchwabExecutor
from .models import Candidate, Position, StrategySchedule
from .paper_account import PaperAccount
from .position_manager import PositionManager
from .position_store import ReconcileMetadataStore, SessionRiskStateStore
from .risk import RiskManager
from .screener_client import TradingViewScreenerClient
from .startup_reconciler import StartupReconciler
from .warmup_tracker import WarmupTracker
from ._strategies.factory import build_strategy
from .session_archive import export_session_archive
from .session_report import write_session_report
from .schwab_api import SchwabdevApiUsageTracker, register_schwab_api_tracker
from .log_setup import TRADEFLOW_LEVEL, setup_logging
from .sessions import EQUITY_STREAM_END, equity_session_state
from . import __version__, sessions

if TYPE_CHECKING:
    from ._strategies.strategy_base import BaseStrategy

LOG = logging.getLogger(__name__)

# How long the engine waits after a failed broker reconcile before the next
# attempt, measured from the end of the failed one. It doubles with each
# failure in a row, up to RECONCILE_RETRY_MAX_SECONDS. An attempt is at least
# two Schwab reads (account_details and account_orders), plus order-state
# reads for working exit orders and foreign orders; a read that hangs takes
# ~30s (schwabdev retries a timed-out read), all of it on the engine thread.
RECONCILE_RETRY_SECONDS = 60.0
RECONCILE_RETRY_MAX_SECONDS = 300.0
# The retry delay while the settle holds a position it could not settle (its
# bracket not confirmed down): the manager sends nothing for it until then, so
# the first retry after a hold comes sooner, whatever failed before it, and
# doubles only over the attempts that keep holding it.
RECONCILE_SETTLE_RETRY_SECONDS = 10.0
# A dashboard update that fails logs its traceback on the first failure in a
# row and on every DASHBOARD_TRACEBACK_EVERY-th after it (about once a minute
# at the 2 s cycle), and a DEBUG line in between; see _publish_state.
DASHBOARD_TRACEBACK_EVERY = 30


def _unique_symbol_keys(symbols: Iterable[str]) -> list[str]:
    """``symbols`` upper-cased and stripped, blanks and repeats dropped, in
    their first order: the keys of the step's watchlist and of every
    per-symbol map (``IntradayBot._compute_symbol_map`` and
    ``_fetch_symbol_map``)."""
    keys: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        key = str(symbol or "").upper().strip()
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


class _CycleTimer:
    """Where one pass of the engine loop spent its wall time, phase by phase:
    the CYCLE_TIMING record (``IntradayBot._log_cycle_timing``).

    ``enter`` starts a phase and closes the one before it, so the phases are
    contiguous and add up to the pass, from the reconcile check to the end of
    the sleep; a phase entered again adds to its time. ``fail`` names the
    phase a failed pass raised in and starts the error path's. ``facts`` are
    the pass's counts (the watchlist, the positions management ran for, the
    time since the last management pass). Durations are ``time.monotonic``;
    only ``start`` is the wall clock.
    """

    def __init__(self) -> None:
        self.started_at = sessions.now_et()
        self.facts: dict[str, Any] = {}
        self._seconds: dict[str, float] = {}
        self._phase: str | None = None
        self._since = self._started = time.monotonic()

    def enter(self, phase: str) -> float:
        """Start ``phase``; returns the monotonic time it started at."""
        now = time.monotonic()
        if self._phase is not None:
            self._seconds[self._phase] = self._seconds.get(self._phase, 0.0) + (now - self._since)
        self._phase, self._since = phase, now
        return now

    def fail(self) -> None:
        self.facts["failed_phase"] = self._phase
        self.enter("error")

    def payload(self) -> dict[str, Any]:
        """The record: ``start``, ``total_s``, a ``<phase>_s`` for each phase
        that took a millisecond or more (one under that, or one the pass never
        reached, is absent), and the facts. Counts the phase in progress up to
        now."""
        end = time.monotonic()
        seconds = dict(self._seconds)
        if self._phase is not None:
            seconds[self._phase] = seconds.get(self._phase, 0.0) + (end - self._since)
        record: dict[str, Any] = {
            "start": self.started_at.isoformat(timespec="milliseconds"),
            "total_s": round(end - self._started, 3),
        }
        record.update({f"{phase}_s": round(value, 3) for phase, value in seconds.items() if round(value, 3) > 0})
        record.update(self.facts)
        return record


class _StopSignals:
    """SIGINT, SIGTERM, SIGHUP and SIGBREAK for the life of ``IntradayBot.run``.

    The first one raises KeyboardInterrupt, so `kill <pid>` and `systemctl
    stop` take the same shutdown path as Ctrl+C, and starts the hold: from
    then on, until ``run`` returns, a signal is only recorded. A second one
    used to raise inside the shutdown cleanup and abandon it half-done (the
    session report half-appended to trades.csv); under systemd the SIGKILL at
    `TimeoutStopSec` still ends a cleanup that hangs. The handler logs
    nothing, since logging from a handler can re-enter a stream write it
    interrupted, so ``run`` reports what it ignored once the cleanup is done.

    SIGHUP is the terminal hanging up: an SSH disconnect or a killed tmux
    pane with the bot in the foreground. Until 2026-09-26 it killed the bot
    without the cleanup. It is taken only when it is not already ignored:
    `nohup` starts the bot with SIGHUP ignored so that a hangup leaves it
    running, and it stays ignored. Windows has no SIGHUP.

    SIGBREAK is Windows' Ctrl+Break, which also killed the bot without the
    cleanup until 2026-09-26. It is taken the same way, where the platform
    has it and it is not ignored. On Windows only SIGINT wakes
    ``time.sleep``, so a Ctrl+Break in the sleep between cycles takes effect
    when that sleep ends. Closing the console window (CTRL_CLOSE_EVENT)
    arrives as SIGBREAK as well, but Windows ends the process as soon as the
    C runtime's console handler returns, so the cleanup cannot finish.
    """

    def __init__(self) -> None:
        self.held = False
        self.ignored: list[str] = []
        self._previous: dict[signal.Signals, Any] = {}

    def __enter__(self) -> _StopSignals:
        signums = [signal.SIGINT, signal.SIGTERM]
        for name in ("SIGHUP", "SIGBREAK"):
            optional = getattr(signal, name, None)
            if optional is not None and signal.getsignal(optional) is not signal.SIG_IGN:
                signums.append(optional)
        for signum in signums:
            try:
                self._previous[signum] = signal.signal(signum, self._handle)
            except ValueError:
                LOG.debug("Could not install the %s handler (non-main thread?)", signum.name, exc_info=True)
        return self

    def __exit__(self, *_exc_info: object) -> None:
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)

    def hold(self) -> None:
        self.held = True

    def _handle(self, signum: int, _frame: Any) -> None:
        if self.held:
            self.ignored.append(signal.Signals(signum).name)
            return
        self.held = True
        raise KeyboardInterrupt()


class IntradayBot:
    def __init__(self, config: BotConfig):
        self.config = config
        setup_logging(config.runtime.log_dir)
        # Logged as soon as there is a log, ahead of anything that can fail
        # (the Schwab client, the strategy build), so the day's log names the
        # code that wrote it.
        LOG.info("intraday-tv-schwab-bot %s", __version__)
        self.audit = AuditLogger(config.strategy)
        self.api_usage = SchwabdevApiUsageTracker()
        self.client = Client(
            app_key=config.schwab.app_key,
            app_secret=config.schwab.app_secret,
            callback_url=config.schwab.callback_url,
            tokens_db=config.schwab.tokens_db,
            encryption=config.schwab.encryption,
            timeout=config.schwab.timeout,
            open_browser_for_auth=config.schwab.open_browser_for_auth,
        )
        register_schwab_api_tracker(self.client, self.api_usage)
        self.screener = TradingViewScreenerClient(config)
        self.data = MarketDataStore(self.client, config)
        self.executor = SchwabExecutor(self.client, config)
        # Per-day risk tallies (realized P&L, cooldowns, same-level blocks)
        # persist across restarts in the same sqlite file as the reconcile
        # metadata. Passed at construction so the first cycle already sees the
        # restored numbers rather than a flat day.
        self.session_risk_state_store = SessionRiskStateStore(config.runtime.startup_reconcile_metadata_db_path)
        self.risk = RiskManager(config, state_store=self.session_risk_state_store)
        self.strategy: BaseStrategy = build_strategy(config)
        self.account = PaperAccount(
            starting_equity=self._tracked_capital_baseline(),
            max_equity_points=config.paper.max_equity_points,
            max_trade_history=config.paper.max_trade_history,
        )
        self.dashboard_cache = DashboardCache(
            config, data=self.data, strategy=self.strategy, account=self.account,
        )
        self.dashboard = (
            DashboardServer(
                host=config.dashboard.host,
                port=config.dashboard.port,
                refresh_ms=config.dashboard.refresh_ms,
                state_path=config.dashboard.state_path,
                theme=config.dashboard.theme,
                https=config.dashboard.https,
                ssl_certfile=config.dashboard.ssl_certfile,
                ssl_keyfile=config.dashboard.ssl_keyfile,
                chart_payload_provider=self.dashboard_cache.chart_payload,
            )
            if config.dashboard.enabled
            else None
        )
        self.positions: dict[str, Position] = {}
        self.reconcile_metadata_store = ReconcileMetadataStore(config.runtime.startup_reconcile_metadata_db_path)
        self.started_at = sessions.now_et()
        self.last_candidates: list[Candidate] = []
        self.last_watchlist: list[str] = []
        self.last_quote_watchlist: list[str] = []
        self.last_error: str | None = None
        # Dashboard updates that failed in a row; see _publish_state.
        self._dashboard_failures = 0
        # The loop pass in progress, whose phases step() times (_run_cycles
        # starts one per pass), and when the last management pass started
        # (time.monotonic), for the gap CYCLE_TIMING reports.
        self._cycle_timer = _CycleTimer()
        self._last_manage_monotonic: float | None = None
        # Memory-pressure prune cadence. Symbol-keyed state in
        # MarketDataStore + DashboardCache grows unbounded across cycles
        # as the screener returns new symbols day to day. Every
        # `runtime.symbol_state_prune_seconds` we evict per-symbol entries
        # for symbols no longer in the active set (streaming + watchlist
        # + held positions). Default: every 30 minutes.
        self._last_symbol_prune_monotonic: float = 0.0
        # ET session date of the most recent SUCCESSFUL broker reconcile.
        # The startup reconcile sets it when it succeeds; the
        # session-boundary reconcile in `_maybe_session_reconcile`
        # re-runs at the start of each new ET trading day so an
        # always-on bot catches broker-side state changes that
        # happened during the 8pm-7am gap (manual position closes
        # via the Schwab app, server-side stop fills, etc.).
        self._last_reconcile_session_date: date | None = None
        # Failed reconcile attempts in a row; while it is non-zero,
        # `_maybe_session_reconcile` retries on `_reconcile_retry_delay()`.
        self._reconcile_failures = 0
        # Failed attempts in a row that left a position held
        # (``settle_pending``); see _reconcile_retry_delay.
        self._settle_hold_failures = 0
        self._last_reconcile_attempt_monotonic: float = 0.0
        # ET session date of the most recent daily session-archive
        # export. `_maybe_export_session_archive` fires once per ET
        # trading day after the stream window closes (8pm ET) so an
        # always-on bot writes a per-day archive on a per-day cadence
        # instead of waiting for shutdown. Shutdown still writes its
        # own archive (potentially overwriting today's) for the final
        # state — that path also updates this field for symmetry.
        self._last_session_archive_date: date | None = None
        # ET session date last seen by `_maybe_session_rollover_reset`.
        # Used to clear `entry_gatekeeper.session_skip_counts` when the
        # ET date rolls. Without this, an always-on bot accumulates
        # skip-reason counts across days so each daily archive shows
        # the cumulative tally instead of just that day's. Initialized
        # to None so the first cycle stamps today's date without
        # firing a (no-op) reset.
        self._last_skip_counts_reset_date: date | None = None
        self.entry_gatekeeper = EntryGatekeeper(
            config,
            client=self.client,
            data=self.data,
            executor=self.executor,
            risk=self.risk,
            audit=self.audit,
            account=self.account,
            strategy=self.strategy,
            positions=self.positions,
            position_manager=None,  # set below once PositionManager exists
            save_reconcile_metadata=self._save_reconcile_metadata,
            is_startup_reconcile_entry_blocked=lambda symbol: self.startup_reconciler.is_entry_blocked(symbol),
        )
        self.position_manager = PositionManager(
            config,
            data=self.data,
            executor=self.executor,
            risk=self.risk,
            audit=self.audit,
            account=self.account,
            strategy=self.strategy,
            positions=self.positions,
            save_reconcile_metadata=self._save_reconcile_metadata,
        )
        self.startup_reconciler = StartupReconciler(
            config,
            executor=self.executor,
            data=self.data,
            account=self.account,
            risk=self.risk,
            strategy=self.strategy,
            positions=self.positions,
            reconcile_metadata_store=self.reconcile_metadata_store,
            save_reconcile_metadata=self._save_reconcile_metadata,
            book_bracket_cancel_fills=self.position_manager.book_bracket_cancel_fills,
            settle_unsettled_entry_orders=self.entry_gatekeeper.settle_unsettled_entry_orders,
            unsettled_entry_order_ids=self.entry_gatekeeper.unsettled_entry_order_ids,
        )
        # Close the cycle: EntryGatekeeper also needs a PositionManager ref
        # (for initialize_position_diagnostics + underlying_price_for_position
        # + SR-timeframe accessors used by _candidate_snapshot).
        self.entry_gatekeeper.position_manager = self.position_manager
        self.cycle_gate = CycleGate(
            config,
            positions=self.positions,
            executor=self.executor,
            startup_reconciler=self.startup_reconciler,
        )
        self.warmup_tracker = WarmupTracker(
            config,
            data=self.data,
            strategy=self.strategy,
            positions=self.positions,
            audit=self.audit,
        )

    def _tracked_capital_baseline(self) -> float:
        if self.config.schwab.dry_run:
            return float(self.config.paper.starting_equity)
        configured = float(self.config.risk.max_total_notional)
        if configured > 0:
            return configured
        return float(self.config.paper.starting_equity)

    def _tracked_capital_label(self) -> str:
        return "Net Liq" if self.config.schwab.dry_run else "Allocated Capital"


    def _save_reconcile_metadata(self) -> None:
        # Until a reconcile has succeeded, self.positions may be only part of
        # what the broker holds: a failed startup attempt restores some
        # positions, or none. Replacing every row with it wiped the rows the
        # retry restores from (the first management cycle did it); write what
        # is tracked over them instead and delete none. `_reconcile_broker`
        # replaces them all on success.
        self.reconcile_metadata_store.save_if_changed(
            self.positions, replace_all=self._last_reconcile_session_date is not None,
        )

    def _trade_management_mode(self) -> str:
        return self.config.risk.trade_management_mode

    def run(self) -> None:
        """Start up, run cycles until a stop signal or the auto-exit, then
        shut down once and return, so the process exits 0.

        Only the cycle itself used to sit inside the `except
        KeyboardInterrupt`. A signal anywhere else (start-up, the auto-exit
        check, the housekeeping, the error path, and the inter-cycle sleep,
        where the loop spends most of its time) raised out of ``run`` with a
        traceback and skipped the cleanup, so no session report was written
        (2026-09-26).

        An error that ends the run (one raised by the start-up, or by the
        loop outside a cycle) still escapes ``run``, so the process exits
        nonzero (``cli`` logs it) and ``Restart=on-failure`` restarts the
        bot, but it no longer skips the cleanup: the dashboard and the stream
        stop and the session report is written first, with stop signals held
        (2026-09-28).
        """
        with _StopSignals() as stop_signals:
            try:
                self._start_up()
                self._run_cycles()
                # Still inside the try: a signal up to here raises and is
                # caught below; from here on one is only recorded.
                stop_signals.hold()
            except KeyboardInterrupt:
                # A KeyboardInterrupt the handler did not raise has not
                # started the hold.
                stop_signals.hold()
                LOG.info("Interrupted, shutting down.")
            finally:
                # An error escaping the start-up or the loop has started no
                # hold either.
                stop_signals.hold()
                self._shutdown_cleanup()
                if stop_signals.ignored:
                    LOG.warning("Ignored %s during the shutdown", ", ".join(stop_signals.ignored))
                LOG.info("Shutdown complete.")

    def _start_up(self) -> None:
        """The dashboard, the start-up reconcile and the start-up log lines."""
        if self.dashboard is not None:
            try:
                self.dashboard.start()
            except Exception as exc:
                # Broad catch: OSError for port/cert file failures, ValueError
                # for misconfiguration (e.g. https=true without ssl_certfile),
                # ssl.SSLError for bad certs. Any of these should leave the
                # bot running headlessly rather than refuse to start.
                LOG.exception("Could not start dashboard on %s:%s: %s", self.config.dashboard.host, self.config.dashboard.port, exc)
                self.dashboard = None
        # A successful startup reconcile counts as today's reconcile: the
        # session-boundary reconcile in `_maybe_session_reconcile` won't fire
        # again until the ET date rolls over. A failed one is retried there.
        self._reconcile_broker(sessions.now_et().date())
        LOG.info("Starting bot with strategy=%s dry_run=%s", self.config.strategy, self.config.schwab.dry_run)
        risk_budget_dollars = float(
            self.config.risk.max_notional_per_trade * self.config.risk.risk_per_trade_frac_of_notional
        )
        LOG.info(
            "Risk config: max_positions=%s risk_per_trade_frac_of_notional=%.4f "
            "(= $%.2f risk per trade at $%.0f notional) max_daily_loss=%.0f "
            "stop=%.3f target=%.3f management=%s",
            self.config.risk.max_positions,
            self.config.risk.risk_per_trade_frac_of_notional,
            risk_budget_dollars,
            self.config.risk.max_notional_per_trade,
            self.config.risk.max_daily_loss,
            self.config.risk.default_stop_pct,
            self.config.risk.default_target_pct,
            self.config.risk.trade_management_mode,
        )
        # Fix C — silent-fallback warning. When config requests
        # adaptive_ladder but the active strategy class explicitly opts out
        # via supports_adaptive_ladder=False, the engine silently falls back
        # to trailing-stop behavior (see TradeManager.update_position's
        # ladder_management_enabled gate). Surface that at startup so the
        # operator knows ladder mechanics aren't actually running.
        if self._trade_management_mode() == "adaptive_ladder":
            strategy_cls = type(self.strategy)
            if not bool(getattr(strategy_cls, "supports_adaptive_ladder", True)):
                LOG.warning(
                    "trade_management_mode=adaptive_ladder is configured but strategy '%s' "
                    "does not support ladder management — open positions will be managed with "
                    "trailing-stop (adaptive) behavior instead. Set trade_management_mode=adaptive "
                    "in your config to silence this warning.",
                    self.config.strategy,
                )
        if self.config.active_is_option:
            styles = [str(style) for style in (self.config.options.styles or [])]
            underlyings = [str(symbol) for symbol in (self.config.options.underlyings or [])]
            LOG.info(
                "Options startup config underlyings=%s styles=%s volatility_symbol=%s",
                ",".join(underlyings) if underlyings else "none",
                ",".join(styles) if styles else "none",
                self.config.options.volatility_symbol,
            )

    def _run_cycles(self) -> None:
        """The cycle loop; returns when the auto-exit decides to stop, and
        leaves the shutdown to ``run``."""
        auto_exit = bool(self.config.runtime.auto_exit_after_session)
        consecutive_errors = 0
        while True:
            # One CYCLE_TIMING record per pass, however it ends: a stop
            # signal or the auto-exit included.
            timer = self._cycle_timer = _CycleTimer()
            try:
                try:
                    # Ahead of the cycle: at 07:00 the first premarket cycle
                    # otherwise managed, and sent exits for, positions closed in
                    # the app overnight before the session-boundary reconcile
                    # dropped them (2026-09-25).
                    timer.enter("reconcile")
                    self._maybe_session_reconcile()
                    self.step()
                    self.last_error = None
                    consecutive_errors = 0
                except Exception as exc:
                    timer.fail()
                    consecutive_errors += 1
                    self.last_error = str(exc)
                    # Throttle log volume during sustained API outages: full
                    # tracebacks for the first few errors and every 10th after,
                    # otherwise a one-line warning. Keeps a single flapping API
                    # from filling the log file overnight.
                    first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                    if consecutive_errors <= 3 or consecutive_errors % 10 == 0:
                        LOG.exception("Unhandled engine error (consecutive=%d): %s", consecutive_errors, exc)
                    else:
                        LOG.warning("Engine error (consecutive=%d): %s", consecutive_errors, first_line)
                    # Escalation. The backoff below keeps retrying forever, which
                    # is right, but a sustained outage with positions open means
                    # nothing is managing them and a throttled WARNING is easy to
                    # miss. Say so loudly, and name what is exposed.
                    status_message = f"Error: {exc}"
                    escalation_message = self._error_escalation_message(consecutive_errors, first_line)
                    if escalation_message is not None:
                        LOG.critical("%s", escalation_message)
                        status_message = escalation_message
                    # The status publish evaluates the gate itself and logs its
                    # own failure (_publish_state). Until 2026-09-26 one that
                    # failed here escaped run() and stopped the bot on an error it
                    # only meant to report.
                    self._publish_state(
                        sessions.now_et(),
                        status_message,
                        screening_active=False,
                        streaming_active=self.data.has_stream_symbols(),
                        management_active=False,
                    )
                timer.enter("housekeeping")
                if auto_exit and not self.positions:
                    now = sessions.now_et()
                    now_t = now.time()
                    session = equity_session_state(now)
                    if not session.is_trading_day:
                        # Non-trading day (weekend/holiday) — exit immediately
                        LOG.info("Auto-exit: non-trading day, no open positions — shutting down")
                        return
                    schedule = self.config.active_strategy.schedule()
                    # Exit after the latest of: RTH close, management window end,
                    # entry window end, screener window end.  This respects
                    # post-market windows configured in the strategy schedule.
                    all_ends = [session.rth_close_time]
                    for w in schedule.management_windows + schedule.entry_windows + schedule.screener_windows:
                        all_ends.append(w.end)
                    exit_after = max(all_ends)
                    if now_t > exit_after:
                        LOG.info("Auto-exit: all windows closed at %s, no open positions — shutting down", exit_after.strftime("%H:%M"))
                        return
                self._maybe_export_session_archive()
                self._maybe_session_rollover_reset()
                self._maybe_prune_inactive_symbols()
                sleep_secs = self._cycle_sleep_seconds()
                if consecutive_errors > 0:
                    # Exponential backoff: 2× per consecutive error, capped at
                    # 60s. Prevents tight-loop hammering of a flapping API.
                    # Each successful step() resets `consecutive_errors` to 0
                    # above, returning to normal cadence immediately.
                    backoff = min(60.0, sleep_secs * (2.0 ** min(consecutive_errors - 1, 5)))
                    sleep_secs = max(sleep_secs, backoff)
                timer.enter("sleep")
                time.sleep(sleep_secs)
            finally:
                self._log_cycle_timing(timer)

    def _log_cycle_timing(self, timer: _CycleTimer) -> None:
        """One CYCLE_TIMING record for the pass ``timer`` timed, at DEBUG (the
        log file, not the console): where the pass spent its time, phase by
        phase, and ``manage_gap_s``, the time between two management passes,
        which is how often an open position's stop and target are checked
        (study F, stage 0). The phases, in the order they run: ``reconcile``
        (the session reconcile check), ``screener`` (the cycle gate and the
        screener), ``watchlist``, ``history`` (the 1m history fetch),
        ``daily_history`` (the daily-history prefetch), ``htf_refresh`` (the
        HTF fetch, entered at the start of the cycle and again before
        management, the entries and the publish: the four add up),
        ``stream``, ``frame`` (the step's merged frames), ``sr`` and
        ``contexts`` (their precompute), ``warmup``, ``quotes`` (the quote
        batch and the account marks), ``manage`` (the entry-order settle and
        ``manage_positions``, a missed live exit's re-sends included),
        ``entries``, ``publish`` (the dashboard), ``error`` (the error path
        of a pass whose step raised, in ``failed_phase``), ``housekeeping``
        (the auto-exit check, the archive, the rollover, the prune) and
        ``sleep``. The session archive copies the records into
        events.jsonl."""
        self.audit.log_structured("CYCLE_TIMING", timer.payload(), level=logging.DEBUG)

    def _error_escalation_message(self, consecutive_errors: int, first_line: str) -> str | None:
        """Alarm text once the engine has failed ``error_escalation_cycles`` in
        a row, or ``None`` while it is still below the threshold.

        Fires on the threshold cycle and every multiple after, so a long
        outage keeps re-announcing itself rather than scrolling away once.
        Open positions are named explicitly — that is the part that costs
        money while nothing is managing them.
        """
        threshold = self.config.runtime.error_escalation_cycles
        if threshold <= 0 or consecutive_errors < threshold:
            return None
        if consecutive_errors % threshold != 0:
            return None
        open_symbols = sorted(self.positions)
        exposure = (
            f"{len(open_symbols)} OPEN POSITION(S) UNMANAGED: {', '.join(open_symbols)}"
            if open_symbols else "no open positions"
        )
        return (
            f"ENGINE DEGRADED — {consecutive_errors} consecutive failed cycles ({exposure}). "
            f"Last error: {first_line}"
        )

    def _maybe_session_reconcile(self) -> None:
        """Re-run startup reconcile when a new ET trading day begins.

        Closes the always-on overnight gap: between 8pm and 7am the bot
        cannot submit orders or receive streamed ticks, but the user can
        still close positions via the Schwab app, and (when broker-side
        stops are wired up later) the broker can fire stops directly.
        Without this, the bot wakes at 7am still believing those
        positions are open and tries to manage phantoms.

        Runs at most once per ET trading day, gated on:
        - It's a trading day (not weekend/holiday)
        - Stream session is open (i.e., we've actually crossed 7am ET)
        - Today's date != date of last successful reconcile

        First run is satisfied by a successful startup reconcile, which
        sets `_last_reconcile_session_date`. So a fresh bot start at 9am
        Tuesday won't double-reconcile.

        A failed reconcile (startup or session-boundary) is retried, first
        RECONCILE_RETRY_SECONDS after the failed attempt ends and then at a
        doubling interval capped at RECONCILE_RETRY_MAX_SECONDS, until one
        succeeds. The failure blocks entries (`startup_reconcile_failed` in
        the blocking modes) and the success clears the block, so one blip no
        longer blocks the day. The retry runs even with
        `session_reconcile_on_resume: false`, which disables only the new-day
        reconcile.

        Disable the new-day reconcile via `runtime.session_reconcile_on_resume: false`.
        """
        # Honor the same gate the startup reconcile honors.
        if not self.config.runtime.reconcile_on_startup:
            return
        if self.config.runtime.startup_reconcile_mode == "ignore":
            return
        retrying = self._reconcile_failures > 0
        if not retrying and not self.config.runtime.session_reconcile_on_resume:
            return
        now = sessions.now_et()
        state = equity_session_state(
            now,
            extended_hours_enabled=bool(self.config.execution.extended_hours_enabled),
        )
        if not state.is_trading_day:
            return
        if not state.stream_available:
            return
        today = now.date()
        if retrying:
            # An attempt that failed only because entry orders were still
            # settling retries as soon as the gatekeeper has booked them: until
            # then the engine manages the grown or adopted quantity against an
            # account it has not reconciled, and entries stay blocked.
            deferred = bool(self.startup_reconciler.result.get("unsettled_entry_orders"))
            entries_settled = not self.entry_gatekeeper.unsettled_entry_orders
            if not (deferred and entries_settled) and \
                    time.monotonic() - self._last_reconcile_attempt_monotonic < self._reconcile_retry_delay():
                return
            LOG.info("Retrying the failed broker reconcile")
        elif self._last_reconcile_session_date == today:
            return
        else:
            LOG.info(
                "Session-boundary reconcile: new ET trading day %s (last=%s) — syncing broker positions/orders",
                today, self._last_reconcile_session_date,
            )
        self._reconcile_broker(today)

    def _reconcile_broker(self, session_date: date) -> None:
        """Run the broker reconcile once. Success stamps ``session_date`` as
        reconciled and saves the reconcile metadata; failure leaves both and
        schedules a retry."""
        ok = self.startup_reconciler.reconcile()
        # The retry delay runs from the END of the attempt, so an attempt that
        # hangs for longer than the delay is not re-fired at once.
        self._last_reconcile_attempt_monotonic = time.monotonic()
        if ok:
            self._last_reconcile_session_date = session_date
            self._reconcile_failures = 0
            self._settle_hold_failures = 0
            # A full replace, even when the positions are unchanged: saves
            # before the first success only upserted, so stale rows remain
            # (the store never skips a replace that follows an upsert).
            self._save_reconcile_metadata()
            return
        self._reconcile_failures += 1
        held = any(isinstance(position.metadata, dict) and position.metadata.get("settle_pending")
                   for position in self.positions.values())
        self._settle_hold_failures = self._settle_hold_failures + 1 if held else 0
        LOG.warning("Broker reconcile failed (%d in a row); retrying in %.0fs",
                    self._reconcile_failures, self._reconcile_retry_delay())

    def _reconcile_retry_delay(self) -> float:
        if self._settle_hold_failures:
            doublings = self._settle_hold_failures - 1
            base = RECONCILE_SETTLE_RETRY_SECONDS
        else:
            doublings = max(0, self._reconcile_failures - 1)
            base = RECONCILE_RETRY_SECONDS
        return min(RECONCILE_RETRY_MAX_SECONDS, base * 2.0 ** min(doublings, 16))

    def _maybe_session_rollover_reset(self) -> None:
        """Clear per-session counters when the ET trading date rolls.

        Mirrors what ``RiskManager._reset_if_new_session`` does for
        ``realized_pnl``: an always-on bot otherwise accumulates state
        across days so each daily archive shows the cumulative tally
        instead of just that day's. Currently scoped to
        ``entry_gatekeeper.session_skip_counts`` (the only
        cross-day-leaky per-session dict that's surfaced into archives
        + session reports).

        Fires unconditionally — does not depend on
        ``session_reconcile_on_resume`` since the user can disable
        reconcile while still wanting per-day count semantics.

        Order in the main loop: placed AFTER ``_maybe_export_session_archive``
        but the ordering is incidental — the two helpers fire in
        non-overlapping time windows. The archive only writes when
        ``now.time() >= EQUITY_STREAM_END`` (8pm) and the rollover only
        fires when the ET date differs from ``_last_skip_counts_reset_date``,
        which happens at midnight, ~4 hours later. So in practice
        archive_for_day_N happens at ~8pm on day N, then rollover_for_day_N+1
        fires shortly after midnight, and counts collected on day N+1
        accumulate from zero through to ~8pm day N+1's archive.
        """
        today = sessions.now_et().date()
        last = self._last_skip_counts_reset_date
        if last is None:
            self._last_skip_counts_reset_date = today
            return
        if today == last:
            return
        existing = len(self.entry_gatekeeper.session_skip_counts)
        if existing:
            LOG.info(
                "Skip-counts session rollover %s -> %s: resetting %d counters",
                last, today, existing,
            )
        self.entry_gatekeeper.session_skip_counts.clear()
        self._last_skip_counts_reset_date = today

    def _maybe_export_session_archive(self) -> None:
        """Write a per-day session archive once the ET trading day ends.

        Closes the always-on archive gap: pre-always-on, the archive
        only fired on shutdown, so a bot that ran continuously through
        many sessions never produced per-day archives. This fires once
        per ET trading day after the stream window closes (8pm ET) so
        each day gets its own ``{log_dir}/sessions/{YYYY-MM-DD}/``
        bundle while the bot is still running.

        Trigger conditions:
        - ``runtime.export_session_archive`` is true (master switch).
        - It's a trading day (not weekend/holiday).
        - Current ET time is at or past ``EQUITY_STREAM_END`` (20:00) —
          i.e. the stream window has closed for the day.
        - Today's date != ``_last_session_archive_date``.

        On success, ``_last_session_archive_date`` is updated to today
        so the daily write doesn't fire again until the date rolls.
        Shutdown still always writes its own archive (potentially
        overwriting today's bundle with a fresher snapshot) and stamps
        this field too — so callers that read ``_last_session_archive_date``
        always see the truth regardless of which path wrote last.
        """
        if not bool(self.config.runtime.export_session_archive):
            return
        now = sessions.now_et()
        state = equity_session_state(
            now,
            extended_hours_enabled=bool(self.config.execution.extended_hours_enabled),
        )
        if not state.is_trading_day:
            return
        if now.time() < EQUITY_STREAM_END:
            return
        today = now.date()
        if self._last_session_archive_date == today:
            return
        LOG.info(
            "Daily session archive: ET trading day %s ended — exporting bars/trades/manifest",
            today,
        )
        try:
            self._export_session_archive()
            self._last_session_archive_date = today
        except Exception:
            # Archive export is a debug aid — never let it crash the
            # main loop. Log full traceback once; a sustained failure
            # will retry next cycle but won't spam.
            LOG.exception("Daily session archive failed (will retry next cycle)")

    def _maybe_prune_inactive_symbols(self) -> None:
        """Evict per-symbol state for symbols no longer in the active set.

        Cadence is gated by `runtime.symbol_state_prune_seconds` (default
        1800 = 30 min) to keep the per-cycle overhead negligible. The
        active set is the union of:
        - currently streamed symbols (live tick consumers)
        - last cycle's watchlist (anything we'd refresh)
        - all open-position symbols (must keep history for management)

        A symbol that drops out of all three has no consumer; safe to evict.
        Re-fetched cleanly on revival via the warmup tracker.
        """
        ttl = self.config.runtime.symbol_state_prune_seconds
        if ttl <= 0:
            return
        now = time.monotonic()
        if now - self._last_symbol_prune_monotonic < ttl:
            return
        self._last_symbol_prune_monotonic = now
        # Symbols we still care about — anything that could need state.
        active: set[str] = set(self.data.stream_symbols)
        active.update(self.last_watchlist or [])
        active.update(self.last_quote_watchlist or [])
        active.update(self.positions.keys() if self.positions else [])
        # Pruning hits the data feed (frames + caches) and the dashboard
        # snapshot/chart caches. Both no-op when active is empty
        # (would otherwise nuke everything).
        if not active:
            return
        try:
            n_data = self.data.prune_inactive_symbols(active)
            n_dash = self.dashboard_cache.prune_inactive_symbols(active)
            if n_data or n_dash:
                LOG.info(
                    "Symbol-state prune evicted data_feed=%d, dashboard=%d (active set: %d symbols)",
                    n_data, n_dash, len(active),
                )
        except Exception:
            LOG.debug("prune_inactive_symbols failed (non-fatal)", exc_info=True)

    def _cycle_sleep_seconds(self) -> float:
        """Pick the right sleep interval for the next loop iteration.

        Default is `runtime.loop_sleep_seconds` (2.0). The fast cadence
        only matters during the equity stream window (7am–8pm ET on
        trading days), when streamed ticks need prompt processing and
        the broker accepts orders. Outside that window the bot can do
        nothing actionable — Schwab won't accept orders before 7am or
        after 8pm — so the loop should idle at
        `runtime.idle_sleep_seconds` (60.0 default).

        Two cadences:
        - **Stream window (7am–8pm ET on trading days)**: fast cycle.
          Entries fire, management runs, dashboard updates from live
          ticks.
        - **Outside the stream window (8pm–7am, weekends, holidays)**:
          idle. Both streaming and order acceptance are off; even with
          open positions, `can_close_position_now()` returns False so
          management is gated off regardless of cycle cadence. Was
          previously burning 2s cycles overnight.

        Note: every Schwab equity order session (regular_session, AM
        extended, PM extended) lies entirely inside the 7am–8pm stream
        window, so "stream off" implies "no order session" — there is
        no third cadence to handle.

        Set `idle_sleep_seconds <= loop_sleep_seconds` to disable the
        optimization entirely.
        """
        # Both finite, the loop's above 0 and the idle one at least 0, checked
        # at load: this runs between cycles, outside their error handling, so
        # an unreadable, NaN or infinite value here ended run() without its
        # shutdown. A null or 0 idle sleep read as 60 until 2026-09-26.
        base = self.config.runtime.loop_sleep_seconds
        idle = self.config.runtime.idle_sleep_seconds
        if idle <= base:
            return base
        state = equity_session_state(
            sessions.now_et(),
            extended_hours_enabled=bool(self.config.execution.extended_hours_enabled),
        )
        # Stream available: fast cycle — live ticks + order session both
        # gated by the same 7am-8pm window, so any actionable work
        # happens here.
        if state.stream_available:
            return base
        # Outside the stream window: nothing actionable. Idle even with
        # open positions; management is blocked anyway.
        return idle

    def _shutdown_cleanup(self) -> None:
        """Three-step cleanup with per-step isolation so a failure in one
        (e.g. disk full during session report) doesn't skip the rest.
        Stop dashboard first so HTTP handlers can't reach into data_feed
        state being torn down by stop_streaming. ``run`` calls it once, with
        stop signals held, wherever start-up or the loop stopped, so a step
        must also cope with a start-up that never reached it (the stop calls
        are no-ops then)."""
        if self.dashboard is not None:
            try:
                self.dashboard.stop()
            except Exception:
                LOG.exception("Dashboard stop failed during shutdown")
        try:
            self.data.stop_streaming()
        except Exception:
            LOG.exception("Stream stop failed during shutdown")
        try:
            self._write_session_report()
        except Exception:
            LOG.exception("Session report write failed during shutdown")

    def _session_report_bars(self, symbol: str):
        """1m bars for the post-stop continuation aggregate.

        Deliberately 1m regardless of the strategy's LTF: the question is how
        far price ran in the half hour after a stop, and the finest available
        resolution gives the truest high/low for that window. Returns None on
        any failure — the aggregate treats a missing frame as unevaluable
        rather than as zero continuation, so a data gap cannot quietly look
        like "the stop was right".
        """
        try:
            return self.data.get_merged(str(symbol), timeframe="1min", with_indicators=False)
        except Exception:
            LOG.debug("Session-report bar lookup failed for %s", symbol, exc_info=True)
            return None

    def _write_session_report(self) -> None:
        write_session_report(
            self.account,
            self.positions,
            strategy=self.config.strategy,
            dry_run=self.config.schwab.dry_run,
            log_dir=self.config.runtime.log_dir,
            structured_logger=self.audit.log_structured,
            skip_counts=dict(self.entry_gatekeeper.session_skip_counts),
            # Post-stop continuation needs bars AFTER each stop-out. Passed as
            # a callable so session_report stays a pure aggregator over
            # TradeRecords and doesn't take a DataFeed dependency.
            bars_for=self._session_report_bars,
        )
        if bool(self.config.runtime.export_session_archive):
            try:
                self._export_session_archive()
                # Stamp today as archived so a shutdown that happens
                # after the daily fire (or before it, on a same-day
                # restart) keeps `_last_session_archive_date` truthful.
                # An overwrite is fine — the freshest snapshot wins,
                # and the daily fire on a subsequent trading day still
                # uses date inequality (today != last) to gate.
                self._last_session_archive_date = sessions.now_et().date()
            except Exception as exc:
                # Archive export is a debug aid — never let it crash shutdown.
                LOG.warning("Session archive export failed: %s", exc, exc_info=True)

    def _export_session_archive(self) -> None:
        """Thin wrapper that delegates to ``session_archive.export_session_archive``.

        Kept on the engine so the call site in ``_write_session_report``
        can stay symmetric with ``write_session_report``. All the actual
        I/O lives in ``session_archive.py``.
        """
        export_session_archive(
            log_dir=self.config.runtime.log_dir,
            strategy_name=self.config.strategy,
            dry_run=self.config.schwab.dry_run,
            data=self.data,
            account=self.account,
            positions=self.positions,
            strategy=self.strategy,
            last_candidates=self.last_candidates,
            session_skip_counts=dict(self.entry_gatekeeper.session_skip_counts),
            config=self.config,
        )

    def step(self) -> None:
        # The phases CYCLE_TIMING reports (_log_cycle_timing).
        timer = self._cycle_timer
        timer.enter("screener")
        self.data.begin_cycle()
        try:
            now = sessions.now_et()
            schedule = self.config.active_strategy.schedule()
            gate_state = self.cycle_gate.evaluate(now, schedule)
            if gate_state.screening_active:
                self.last_candidates = self.screener.get_candidates(self.config.strategy)
                candidate_symbols = [c.symbol for c in self.last_candidates]
                self.audit.log_cycle(
                    f"candidates:{self.config.strategy}",
                    ",".join(candidate_symbols),
                    f"Candidate cycle strategy={self.config.strategy} count={len(candidate_symbols)} symbols={','.join(candidate_symbols) if candidate_symbols else 'none'}",
                    interval=45.0,
                )
            timer.enter("watchlist")
            watchlist = self.strategy.active_watchlist(self.last_candidates, self.positions)
            # Normalized at the source with the per-symbol maps' own keying
            # (_unique_symbol_keys), so every downstream consumer (the maps,
            # the warmup tracker, the API state) sees the same canonical form
            # and the step frames are keyed by exactly these symbols: a symbol
            # missing from `bars` is one whose frame build failed.
            if gate_state.idle_closed_market:
                self.last_watchlist = []
            else:
                self.last_watchlist = sorted(_unique_symbol_keys(watchlist))
            if gate_state.idle_closed_market:
                self.audit.log_cycle(
                    f"watchlist_idle:{self.config.strategy}",
                    "closed_market",
                    f"Watchlist idle strategy={self.config.strategy} reason=market_closed_outside_broker_session",
                    interval=300.0,
                    level=logging.INFO,
                )
            else:
                watchlist_trace = self.strategy.watchlist_trace("active", self.last_candidates, self.positions)
                self.audit.log_watchlist_trace("active", watchlist_trace)
            timer.facts["watchlist"] = len(self.last_watchlist)
            timer.enter("history")

            # Per-symbol history fetch decisions are made on the engine
            # thread (they read warmup_tracker state and are cheap), each
            # symbol isolated (_compute_symbol_map): until 2026-09-28 one that
            # raised failed the cycle before management. The HTTP fetches run
            # on the fetch pool (_fetch_symbol_map). Both maps key the
            # symbols alike, so the lambda's dict lookup matches the symbol
            # it receives.
            def _history_lookback(symbol: str) -> int | None:
                should_fetch, _required_bars = self.warmup_tracker.should_fetch_symbol_history(
                    symbol,
                    context_refresh_active=gate_state.context_refresh_active,
                    streaming_active=gate_state.streaming_active,
                )
                if not should_fetch:
                    return None
                return self.warmup_tracker.history_fetch_lookback_minutes(
                    now,
                    streaming_active=gate_state.streaming_active,
                    required_bars=self.warmup_tracker.desired_history_bars(symbol),
                )

            decided = self._compute_symbol_map(self.last_watchlist, _history_lookback, label="History fetch decision")
            history_fetch_targets = {symbol: minutes for symbol, minutes in decided.items() if minutes is not None}
            if history_fetch_targets:
                self._fetch_symbol_map(
                    list(history_fetch_targets),
                    lambda symbol: self.data.fetch_history(
                        symbol,
                        lookback_minutes=history_fetch_targets[symbol],
                    ),
                    label="History fetch",
                )

            timer.enter("daily_history")
            self._prefetch_daily_history(gate_state, now, schedule)
            # The cycle's HTF fetches: here, and again before management, the
            # entries and the publish, so a bar that closes mid-cycle is
            # fetched at the next of them. Each symbol at most once a cycle.
            timer.enter("htf_refresh")
            htf_attempted: set[str] = set()
            self._refresh_htf_frames(gate_state, htf_attempted, where="cycle start")

            timer.enter("stream")
            if gate_state.streaming_active and self.last_watchlist:
                self.data.start_streaming(self.last_watchlist)
            else:
                self.data.stop_streaming()

            timer.enter("frame")
            # The step frames. A symbol whose build raises is left out of
            # `bars` (logged, PRECOMPUTE_FAILURES): it gets no pre-warm, no
            # entry and no frame-based exit this cycle, and a position in it
            # is still managed on its quote (the stop, the target, force
            # flatten).
            bars = self._compute_symbol_map(
                self.last_watchlist,
                lambda symbol: self.data.get_merged(symbol, with_indicators=True),
                label="Merged frame precompute",
            )
            timer.enter("sr")
            self._prime_cycle_support_cache(bars)
            timer.enter("contexts")
            self._prime_cycle_context_cache(bars)
            timer.enter("warmup")
            warmup_summary = self.warmup_tracker.warmup_summary(self.last_watchlist, bars=bars)
            self.warmup_tracker.log_warmup_summary(warmup_summary)
            timer.enter("quotes")

            if gate_state.idle_closed_market:
                self.last_quote_watchlist = []
            else:
                quote_trace = self.strategy.watchlist_trace("quote", self.last_candidates, self.positions, bars=bars, active_symbols=set(self.last_watchlist))
                self.audit.log_watchlist_trace("quote", quote_trace)
                quote_symbols = sorted(self.strategy.quote_watchlist(self.last_candidates, self.positions, bars))
                self.last_quote_watchlist = quote_symbols
                if gate_state.context_refresh_active and quote_symbols:
                    self.data.fetch_quotes(quote_symbols, source="engine:quote_watchlist")

            self.account.mark_prices(self._extract_last_prices(bars))
            self.account.mark_prices(self._extract_position_marks())
            timer.enter("htf_refresh")
            self._refresh_htf_frames(gate_state, htf_attempted, where="before management", bars=bars)
            timer.facts["managed"] = gate_state.management_active
            if gate_state.management_active:
                manage_started = timer.enter("manage")
                if self._last_manage_monotonic is not None:
                    timer.facts["manage_gap_s"] = round(manage_started - self._last_manage_monotonic, 3)
                self._last_manage_monotonic = manage_started
                # Settle entry orders an earlier cycle could not, first, so a
                # position they turn out to have opened is managed this cycle.
                self.entry_gatekeeper.settle_unsettled_entry_orders()
                timer.facts["positions"] = len(self.positions)
                self.position_manager.manage_positions(now, bars)
            timer.enter("htf_refresh")
            self._refresh_htf_frames(gate_state, htf_attempted, where="before entries", bars=bars)
            timer.enter("entries")
            if self.startup_reconciler.trading_blocked_reason:
                candidate_symbols = [c.symbol for c in self.last_candidates]
                reasons: list[str] = []
                if not candidate_symbols:
                    reasons.append("no_candidates")
                reasons.extend([part for part in str(self.startup_reconciler.trading_blocked_reason).split(",") if part])
                reason_text = ",".join(reasons) if reasons else str(self.startup_reconciler.trading_blocked_reason)
                self.audit.log_cycle(
                    f"entry_gate:{self.config.strategy}",
                    reason_text,
                    f"Entry cycle strategy={self.config.strategy} action=skipped reasons={reason_text}",
                    interval=60.0,
                    level=TRADEFLOW_LEVEL,
                )
            elif gate_state.intraday_session_day and gate_state.entry_actionable:
                self.entry_gatekeeper.open_positions(self.last_candidates, bars)
            elif gate_state.intraday_session_day and gate_state.entry_window_open:
                self.audit.log_cycle(
                    f"entry_gate:{self.config.strategy}",
                    "entry_session_closed",
                    f"Entry cycle strategy={self.config.strategy} action=skipped reasons=entry_session_closed",
                    interval=60.0,
                    level=TRADEFLOW_LEVEL,
                )
            elif gate_state.intraday_session_day and not gate_state.entry_window_open:
                # Downgraded to DEBUG: this fires every ~60 seconds before
                # the entry window opens and after it closes. It's expected
                # state during those hours, not useful TRADEFLOW signal —
                # keeping it at TRADEFLOW just adds hundreds of noise lines
                # per day. All other skip reasons (cooldown, filter blocks,
                # insufficient bars, etc.) stay at TRADEFLOW.
                self.audit.log_cycle(
                    f"entry_gate:{self.config.strategy}",
                    "outside_entry_window",
                    f"Entry cycle strategy={self.config.strategy} action=skipped reasons=outside_entry_window",
                    interval=60.0,
                    level=logging.DEBUG,
                )

            timer.enter("htf_refresh")
            self._refresh_htf_frames(gate_state, htf_attempted, where="before publish", bars=bars)
            timer.enter("publish")
            # Re-mark only position marks — bar closes haven't changed since the earlier
            # mark_prices call above. Skipping _extract_last_prices here avoids iterating
            # the full bars dict a second time per tick.
            self.account.mark_prices(self._extract_position_marks())
            message = self.cycle_gate.runtime_status_message(
                screening_active=gate_state.screening_active,
                management_active=gate_state.management_active,
                streaming_active=gate_state.streaming_active,
                context_refresh_active=gate_state.context_refresh_active,
                idle_closed_market=gate_state.idle_closed_market,
                position_monitoring_active=gate_state.position_monitoring_active,
            )
            # Last, and inside the cycle, so it reads the frames the cycle
            # cached. It cannot fail the cycle: a failure is the dashboard's
            # (_publish_state).
            self._publish_state(
                now,
                message,
                screening_active=gate_state.screening_active,
                streaming_active=gate_state.streaming_active,
                management_active=gate_state.management_active,
                gate_state=gate_state,
                warmup_summary=warmup_summary,
            )
        finally:
            self.data.end_cycle()

    @staticmethod
    def _extract_last_prices(bars: dict[str, Any]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for symbol, frame in bars.items():
            if frame is None or frame.empty:
                continue
            prices[symbol] = float(frame.iloc[-1].close)
        return prices

    def _extract_position_marks(self) -> dict[str, float]:
        prices: dict[str, float] = {}
        for key, position in self.positions.items():
            mark = self.strategy.position_mark_price(position, self.data)
            if mark is not None:
                prices[key] = float(mark)
        return prices

    def _compute_symbol_map(self, symbols: Iterable[str], func, *, label: str) -> dict[str, Any]:
        """``func(symbol)`` for each symbol, one at a time on the engine
        thread, keyed by ``_unique_symbol_keys``: the cycle's CPU work (the
        history-fetch decisions, the step frames, the S/R and context
        pre-warms), which never waits on the network.

        Until 2026-09-28 this work ran on the fetches' thread pool
        (``runtime.cycle_precompute_workers``, 4 in every preset). It is
        pandas / numpy / TA-Lib on small frames and holds the GIL nearly all
        the time, so the pool never overlapped it and made it slower: replayed
        on four archived top_tier days, the three maps took 5.4 s a step with
        4 workers and 2.4 s run this way, the means of the four days' medians
        (``CHANGELOG.md``, 2026-09-28).

        A symbol whose call raises is logged with its error's type and
        traceback and left out of the result, and the cycle's failures are
        named in one PRECOMPUTE_FAILURES audit event; the other symbols still
        run. The pool's one-worker branch had no such isolation: one raising
        symbol failed the cycle.
        """
        keys = _unique_symbol_keys(symbols)
        results: dict[str, Any] = {}
        failed: list[str] = []
        for symbol in keys:
            try:
                results[symbol] = func(symbol)
            except Exception as exc:
                failed.append(symbol)
                LOG.warning("%s failed for %s: %s: %s", label, symbol, type(exc).__name__, exc, exc_info=True)
        self._audit_symbol_map_failures(label, len(keys), failed)
        return results

    def _fetch_symbol_map(self, symbols: Iterable[str], func, *, label: str) -> dict[str, Any]:
        """``func(symbol)`` for each symbol on a pool of up to
        ``runtime.cycle_fetch_workers`` threads, keyed by
        ``_unique_symbol_keys``: the cycle's network fetches (the 1m history,
        the HTF refresh points and the daily-history prefetch), which spend
        their time waiting on Schwab, so the pool overlaps them. The pool
        runs whatever the count, one worker included. A symbol whose call
        raises is isolated as in ``_compute_symbol_map``. The results are
        read in the symbols' order."""
        keys = _unique_symbol_keys(symbols)
        if not keys:
            return {}
        results: dict[str, Any] = {}
        failed: list[str] = []
        workers = min(self.config.runtime.cycle_fetch_workers, len(keys))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bot-fetch") as executor:
            futures = {symbol: executor.submit(func, symbol) for symbol in keys}
            for symbol, future in futures.items():
                try:
                    results[symbol] = future.result()
                except Exception as exc:
                    failed.append(symbol)
                    LOG.warning("%s failed for %s: %s: %s", label, symbol, type(exc).__name__, exc, exc_info=True)
        self._audit_symbol_map_failures(label, len(keys), failed)
        return results

    def _audit_symbol_map_failures(self, label: str, total: int, failed: list[str]) -> None:
        """One audit event per map that dropped symbols, so operators can see
        a cycle skipping them; each failure's own WARNING carries its
        traceback."""
        if failed:
            self.audit.log_structured(
                "PRECOMPUTE_FAILURES",
                {"label": label, "total": total, "failed_count": len(failed), "failed_symbols": failed},
            )

    def _htf_symbols(self) -> list[str]:
        """Every symbol whose HTF frame a read can ask for this cycle: the
        watchlist (the step frames, the entries and the peers they vote
        with), the quote watchlist and the candidates (the dashboard's
        symbols, the entry gatekeeper's candidate snapshots) and each
        position's underlying and reference symbol (its management, the
        dashboard's position rows). The data feed keeps HTF frames for S/R
        symbols only (``MarketDataStore.htf_refresh_due``)."""
        symbols = set(self.last_watchlist) | set(self.last_quote_watchlist)
        symbols.update(candidate.symbol for candidate in self.last_candidates)
        for position in self.positions.values():
            symbols.add(str(position.metadata.get("underlying") or position.symbol))
            if position.reference_symbol:
                symbols.add(position.reference_symbol)
        return sorted(_unique_symbol_keys(symbols))

    def _refresh_htf_frames(self, gate_state: CycleGateState, attempted: set[str], *, where: str,
                            bars: dict[str, pd.DataFrame] | None = None) -> None:
        """Fetch, on the fetch pool, the HTF frame of every ``_htf_symbols``
        symbol whose HTF bar has closed (``MarketDataStore.htf_refresh_due``
        on the strategy's ``htf_minutes()``: 10 s after the boundary) and that
        this cycle has not tried yet (``attempted``, which it extends): the
        only HTF fetch (``MarketDataStore.refresh_htf_frame``); no read
        fetches.

        ``step`` calls it at the start of the cycle and again before
        management, the entries and the publish (``where``), so a bar that
        closes mid-cycle reaches the rest of the cycle at the next of those
        points. Until 2026-09-28 the cycle fetched at its start only, and a
        read after the boundary fetched its own symbol, one at a time: most
        of a boundary's fetches ran in the dashboard publish, which held the
        next management 15-30 s (study F, section 1.3). A symbol whose fetch
        failed is not retried until the next cycle, as each read used to
        retry it: an outage costs one fetch per symbol per cycle.

        Mid-cycle (``bars`` given) the refreshed symbols' S/R contexts are
        built again as the cycle's pre-warm builds them
        (``_prime_cycle_support_cache``): the refresh dropped them, and the
        cycle serves every later read the build of its first read. Only
        while the gate refreshes market context, as the cycle's fetch always
        was."""
        if not gate_state.context_refresh_active:
            return
        tf = self.strategy.htf_minutes()
        due = self.data.htf_refresh_due([symbol for symbol in self._htf_symbols() if symbol not in attempted], tf)
        if not due:
            return
        attempted.update(due)
        lookback_days = self.strategy.htf_lookback_days()
        started = time.monotonic()
        stored = self._fetch_symbol_map(
            due,
            lambda symbol: self.data.refresh_htf_frame(symbol, timeframe_minutes=tf, lookback_days=lookback_days),
            label="HTF refresh",
        )
        refreshed = [symbol for symbol in due if stored.get(symbol) is True]
        failed = [symbol for symbol in due if stored.get(symbol) is not True]
        LOG.info("HTF refresh (%s): %d/%d %sm frame(s) in %.2fs%s", where, len(refreshed), len(due), tf,
                 time.monotonic() - started, f"; failed: {','.join(failed)}" if failed else "")
        if bars is not None and refreshed:
            self._prime_cycle_support_cache({symbol: bars[symbol] for symbol in refreshed if symbol in bars})

    def _prefetch_daily_history(self, gate_state: CycleGateState, now: datetime, schedule: StrategySchedule) -> None:
        """Fetch, on the fetch pool, the daily history the strategy's
        entries read (``strategy.daily_history_symbols``) for each such
        symbol not fetched yet today (``MarketDataStore.daily_history_due``):
        from the prewarm on, so the day's first entry pass finds it cached,
        and later for a symbol that joins the watchlist; nothing once every
        symbol has it. Until 2026-09-28 top_tier fetched each symbol's 180
        days inside ``entry_signals``, one at a time: the day's first entry
        pass (09:35) waited about 10 s on 28 fetches, nothing managed
        meanwhile. Only while the gate refreshes market context.

        A fetch that raised is fetched again, ``DAILY_HISTORY_RETRY_SECONDS``
        apart, until the day's first entry window opens
        (``StrategySchedule.before_first_entry``); from then on it stays
        cached for the day, as a read keeps it. On the top_tier preset the
        prewarm starts 20 minutes before that window (09:15, 09:35), room for
        a transient failure to clear; until then a failure was cached for the
        day at the first fetch."""
        if not gate_state.context_refresh_active:
            return
        retry_failed = schedule.before_first_entry(now.time())
        due = [symbol for symbol in self.strategy.daily_history_symbols(self.last_watchlist)
               if self.data.daily_history_due(symbol, retry_failed=retry_failed)]
        if due:
            self._fetch_symbol_map(due, self.data.fetch_daily_history, label="Daily history fetch")

    def _prime_cycle_support_cache(self, bars: dict[str, pd.DataFrame]) -> None:
        sr_cfg = getattr(self.config, "support_resistance", None)
        if sr_cfg is None or not bool(sr_cfg.enabled):
            return
        symbols = [symbol for symbol, frame in bars.items() if frame is not None and not frame.empty]
        if not symbols:
            return

        def _compute(symbol: str) -> Any:
            frame = bars.get(symbol)
            if frame is None or frame.empty:
                return None
            current_price = float(frame.iloc[-1].get("close", 0.0) or 0.0)
            return self.data.get_support_resistance(
                symbol,
                current_price=current_price,
                flip_frame=frame,
                mode="trading",
                timeframe_minutes=self.strategy.htf_minutes(),
                use_prior_day_high_low=bool(getattr(sr_cfg, "use_prior_day_high_low", True)),
                use_prior_week_high_low=bool(getattr(sr_cfg, "use_prior_week_high_low", True)),
            )

        self._compute_symbol_map(symbols, _compute, label="Support/resistance precompute")

    def _prime_cycle_context_cache(self, bars: dict[str, pd.DataFrame]) -> None:
        """Pre-warm strategy chart/structure/technical caches, one symbol
        at a time (``_compute_symbol_map``).

        Strategies populate three per-symbol context caches lazily inside
        their per-candidate entry_signals loop: TA-Lib chart-pattern
        detection, pivot/ATR market-structure analysis, and the
        Fibonacci/trendline/channel/Bollinger technical-levels stack.
        Building them here, before entry_signals starts, lets the entry pass
        read them from the caches. Until 2026-09-28 the builds ran on
        a four-worker thread pool; they hold the GIL nearly all the time, and
        the pool made them slower, not faster (``_compute_symbol_map``).

        Auto-detect: each context builder (`_strategies/contexts.py`)
        records its call signature in its strategy class's
        `_observed_contexts` (a class-level set of tuples like
        `("structure", "ltf")`) -- but only for a call on one of THIS
        cycle's bars frames, which this method hands the strategy first
        (`set_prewarm_frames`). The caches key on id(frame), so a build on
        any other frame (the peers' 5m LTF, key_levels_1m's get_merged copy)
        could never read what this pre-warm puts on the 1m bars frames; until
        2026-09-24 such calls were recorded too, and key_levels paid a chart
        and technical build per watchlist symbol per cycle that nothing read.
        On cycle 1 the set is empty and nothing is pre-warmed — the strategy
        runs lazy. From cycle 2 onward, only the contexts the strategy
        actually invokes on its bars frames are pre-warmed, across the
        watchlist. New code paths that hit a previously-unseen context
        register on first invocation and join the pre-warm set thereafter
        (self-healing).

        Cache writes inside each builder stay guarded by per-cache RLocks.
        """
        self.strategy.set_prewarm_frames(bars.values())
        # Cycle-boundary reset — owned by the engine now, NOT by entry_signals.
        # Strategies still call _reset_entry_decisions() at the top of
        # entry_signals(), but that method no longer touches the 3 context
        # caches the engine just (or is about to) pre-warm. It runs whether or
        # not anything is observed: every entry pins its frame, and a
        # strategy whose builds are all on non-bars frames observes nothing.
        self.strategy.reset_context_caches()
        # Snapshot the observed set so the loop iterating it can't trip on a
        # mutation if a builder records a previously-unseen tuple mid-cycle
        # (a set changing size during its iteration raises). In practice,
        # pre-warm only replays known entries (idempotent set.add → no size
        # change), but a frozenset removes the doubt for the cost of one
        # shallow copy.
        observed = frozenset(type(self.strategy)._observed_contexts)
        if not observed:
            return
        symbols = [symbol for symbol, frame in bars.items() if frame is not None and not frame.empty]
        if not symbols:
            return

        def _warm(symbol: str) -> Any:
            frame = bars.get(symbol)
            if frame is None or frame.empty:
                return None
            self.strategy.prime_cycle_contexts(frame, observed)
            return None

        self._compute_symbol_map(symbols, _warm, label="Strategy context precompute")

    def _publish_state(self, now: datetime, message: str, *, screening_active: bool, streaming_active: bool, management_active: bool, gate_state: CycleGateState | None = None, warmup_summary: dict[str, Any] | None = None) -> None:
        """Build the dashboard state and publish it. The gate is evaluated
        here when the caller has none (the error path).

        The dashboard shows the cycle; it is not part of it. Until 2026-09-26
        an error building the state (a snapshot or S/R row read that raises)
        escaped ``step()`` after the cycle's management and entries had run
        and failed the cycle, and the error path's own publish then failed
        the same way, which stopped the bot. Now the failure is logged, with
        the traceback on the first in a row and every
        ``DASHBOARD_TRACEBACK_EVERY``-th, the loop keeps its cadence, and the
        dashboard keeps its last state, marked ``stale`` (``error`` while
        the cycles themselves fail) with a message saying what failed and
        when; its ``last_update`` stays the time of that state. Broad
        because the build runs strategy hooks; a stop signal
        (KeyboardInterrupt) is not an Exception and reaches the shutdown.
        """
        try:
            if gate_state is None:
                gate_state = self.cycle_gate.evaluate(now, self.config.active_strategy.schedule())
            payload = self._dashboard_state(
                now,
                message,
                screening_active=screening_active,
                streaming_active=streaming_active,
                management_active=management_active,
                gate_state=gate_state,
                warmup_summary=warmup_summary,
            )
        except Exception as exc:
            self._dashboard_failures += 1
            failures = self._dashboard_failures
            first_line = str(exc).splitlines()[0] if str(exc) else ""
            error = f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__
            if failures == 1 or failures % DASHBOARD_TRACEBACK_EVERY == 0:
                LOG.warning("Dashboard update failed (consecutive=%d); it keeps its last state, marked stale: %s",
                            failures, error, exc_info=True)
            else:
                LOG.debug("Dashboard update failed (consecutive=%d): %s", failures, error)
            if self.dashboard is not None:
                self.dashboard.publish_stale(
                    "stale" if self.last_error is None else "error",
                    f"{message} · dashboard update failed ({failures} in a row, the last at "
                    f"{now:%H:%M:%S}: {error}); the rest of this page is from its last update",
                )
            return
        if self._dashboard_failures:
            LOG.info("Dashboard update recovered after %d failed update(s)", self._dashboard_failures)
            self._dashboard_failures = 0
        if self.dashboard is not None:
            self.dashboard.publish(payload)

    def _dashboard_state(self, now: datetime, message: str, *, screening_active: bool, streaming_active: bool, management_active: bool, gate_state: CycleGateState, warmup_summary: dict[str, Any] | None) -> dict[str, Any]:
        """The dashboard state ``_publish_state`` publishes: the engine's
        status fields, and the symbol part ``DashboardCache.build_payload``
        builds from the state the engine hands it."""
        if warmup_summary is None:
            warmup_summary = self.warmup_tracker.warmup_summary(self.last_watchlist)
        symbol_state = self.dashboard_cache.build_payload(
            positions=self.positions,
            last_candidates=self.last_candidates,
            watchlist=self.last_watchlist,
            quote_watchlist=self.last_quote_watchlist,
            entry_decisions=self.entry_gatekeeper.last_entry_decisions,
            warmup_summary=warmup_summary,
        )
        return {
            "status": "running" if self.last_error is None else "error",
            "entry_window_active": gate_state.entry_actionable,
            "management_window_active": gate_state.intraday_session_day and gate_state.management_window_open,
            "management_active": management_active,
            "position_monitoring_active": gate_state.position_monitoring_active,
            "message": message,
            "strategy": self.config.strategy,
            "dry_run": self.config.schwab.dry_run,
            "last_update": now.isoformat(),
            "started_at": self.started_at.isoformat(),
            "screening_active": screening_active,
            "streaming_active": streaming_active,
            "trading_blocked_reason": self.startup_reconciler.trading_blocked_reason,
            "startup_reconcile": self.startup_reconciler.result,
            "active_watchlist": self.last_watchlist,
            "quote_watchlist": self.last_quote_watchlist,
            "data": symbol_state["data"],
            "warmup": warmup_summary,
            # Read after the cycle's last HTF refresh (before the publish), so
            # it counts the Schwab calls that made.
            "api_usage": self.api_usage.snapshot(now),
            "performance": symbol_state["performance"],
            "tracked_capital_label": self._tracked_capital_label(),
            "candidates": symbol_state["candidates"],
            "symbol_exchanges": symbol_state["symbol_exchanges"],
            "dashboard_charting": symbol_state["dashboard_charting"],
            "dashboard_symbols": symbol_state["dashboard_symbols"],
        }
