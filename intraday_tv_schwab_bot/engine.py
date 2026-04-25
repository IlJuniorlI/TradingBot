# SPDX-License-Identifier: MIT
from __future__ import annotations

import copy
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from schwabdev import Client
import pandas as pd

from .audit_logger import AuditLogger, _json_ready
from .dashboard_cache import (
    DashboardCache,
    dashboard_normalize_exchange,
    dashboard_quote_exchange,
)
from .config import BotConfig
from .cycle_gate import CycleGate, CycleGateState
from .dashboard import DashboardServer
from .data_feed import MarketDataStore, NON_STREAMABLE
from .entry_gatekeeper import EntryGatekeeper
from .execution import SchwabExecutor
from .models import Candidate, Position
from ._strategies.registry import option_strategy_names
from .paper_account import PaperAccount
from .position_manager import PositionManager
from .position_metrics import safe_float
from .position_store import ReconcileMetadataStore
from .risk import RiskManager
from .screener_client import TradingViewScreenerClient
from .startup_reconciler import StartupReconciler
from .warmup_tracker import WarmupTracker
from ._strategies.registry import build_strategy
from ._strategies.strategy_base import BaseStrategy
from .session_report import export_session_archive, write_session_report
from .utils import TRADEFLOW_LEVEL, SchwabdevApiUsageTracker, equity_session_state, now_et, register_schwab_api_tracker, setup_logging

LOG = logging.getLogger(__name__)


class IntradayBot:
    def __init__(self, config: BotConfig):
        self.config = config
        setup_logging(config.runtime.log_dir)
        self.audit = AuditLogger(config.strategy)
        self.api_usage = SchwabdevApiUsageTracker()
        self.client = Client(
            app_key=config.schwab.app_key,
            app_secret=config.schwab.app_secret,
            callback_url=config.schwab.callback_url,
            tokens_db=config.schwab.tokens_db,
            encryption=config.schwab.encryption,
            timeout=config.schwab.timeout,
        )
        register_schwab_api_tracker(self.client, self.api_usage)
        self.screener = TradingViewScreenerClient(config)
        self.data = MarketDataStore(self.client, config)
        self.executor = SchwabExecutor(self.client, config)
        self.risk = RiskManager(config)
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
        self.started_at = now_et()
        self.last_candidates: list[Candidate] = []
        self.last_watchlist: list[str] = []
        self.last_quote_watchlist: list[str] = []
        self.last_error: str | None = None
        self._last_reconcile_metadata_signature: str | None = None
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
        self.startup_reconciler = StartupReconciler(
            config,
            client=self.client,
            executor=self.executor,
            data=self.data,
            account=self.account,
            strategy=self.strategy,
            positions=self.positions,
            reconcile_metadata_store=self.reconcile_metadata_store,
            save_reconcile_metadata=self._save_reconcile_metadata,
            stock_position_trail_pct=self.entry_gatekeeper.stock_position_trail_pct,
        )
        self.position_manager = PositionManager(
            config,
            data=self.data,
            executor=self.executor,
            risk=self.risk,
            audit=self.audit,
            account=self.account,
            strategy=self.strategy,
            dashboard_cache=self.dashboard_cache,
            positions=self.positions,
            save_reconcile_metadata=self._save_reconcile_metadata,
            broker_position_row=self.entry_gatekeeper.broker_position_row,
            broker_position_rows=self.entry_gatekeeper.broker_position_rows,
            structured_metadata_snapshot=self.entry_gatekeeper.structured_metadata_snapshot,
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
        try:
            configured = float(self.config.risk.max_total_notional)
        except Exception:
            configured = 0.0
        if configured > 0:
            return configured
        return float(self.config.paper.starting_equity)

    def _tracked_capital_label(self) -> str:
        return "Net Liq" if self.config.schwab.dry_run else "Allocated Capital"


    def _reconcile_metadata_signature(self) -> str:
        payload = []
        for key, position in sorted(self.positions.items(), key=lambda item: str(item[0])):
            payload.append({
                "key": str(key),
                "symbol": str(position.symbol),
                "strategy": str(position.strategy),
                "side": str(position.side.value),
                "qty": int(position.qty),
                "entry_price": float(position.entry_price),
                "entry_time": position.entry_time.isoformat(),
                "stop_price": float(position.stop_price),
                "target_price": safe_float(position.target_price, None),
                "trail_pct": safe_float(position.trail_pct, None),
                "highest_price": safe_float(position.highest_price, None),
                "lowest_price": safe_float(position.lowest_price, None),
                "pair_id": position.pair_id,
                "reference_symbol": position.reference_symbol,
                "metadata": _json_ready(position.metadata or {}),
            })
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _save_reconcile_metadata(self) -> None:
        signature = self._reconcile_metadata_signature()
        if signature == self._last_reconcile_metadata_signature:
            return
        try:
            self.reconcile_metadata_store.save_positions(self.positions)
            self._last_reconcile_metadata_signature = signature
        except Exception as exc:
            LOG.warning("Could not save startup reconcile metadata: %s", exc)

    def _trade_management_mode(self) -> str:
        return self.config.risk.trade_management_mode

    def run(self) -> None:
        # Route SIGTERM through KeyboardInterrupt so `kill <pid>` hits the
        # same clean-shutdown path as Ctrl+C.
        import signal as _signal
        def _raise_keyboard_interrupt(_sig, _frame):
            raise KeyboardInterrupt()
        if hasattr(_signal, "SIGTERM"):
            try:
                _signal.signal(_signal.SIGTERM, _raise_keyboard_interrupt)
            except (ValueError, OSError):
                LOG.debug("Could not install SIGTERM handler (non-main thread?)", exc_info=True)
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
        self.startup_reconciler.reconcile()
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
        # to trailing-stop behavior (see risk.py:289). Surface that at
        # startup so the operator knows ladder mechanics aren't actually
        # running.
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
        if self.config.strategy in option_strategy_names():
            styles = [str(style) for style in (self.config.options.styles or [])]
            underlyings = [str(symbol) for symbol in (self.config.options.underlyings or [])]
            LOG.info(
                "Options startup config underlyings=%s styles=%s volatility_symbol=%s",
                ",".join(underlyings) if underlyings else "none",
                ",".join(styles) if styles else "none",
                self.config.options.volatility_symbol,
            )
        auto_exit = bool(self.config.runtime.auto_exit_after_session)
        while True:
            try:
                self.step()
                self.last_error = None
            except KeyboardInterrupt:
                LOG.info("Interrupted, shutting down.")
                self._shutdown_cleanup()
                break
            except Exception as exc:
                self.last_error = str(exc)
                LOG.exception("Unhandled engine error: %s", exc)
                now = now_et()
                gate_state = self.cycle_gate.evaluate(now, self.config.active_strategy.schedule())
                self._publish_state(
                    now,
                    screening_active=False,
                    streaming_active=self.data.has_stream_symbols(),
                    management_active=False,
                    message=f"Error: {exc}",
                    context_refresh_active=gate_state.context_refresh_active,
                    gate_state=gate_state,
                )
            if auto_exit and not self.positions:
                now = now_et()
                now_t = now.time()
                session = equity_session_state(now)
                if not session.is_trading_day:
                    # Non-trading day (weekend/holiday) — exit immediately
                    LOG.info("Auto-exit: non-trading day, no open positions — shutting down")
                    self._shutdown_cleanup()
                    break
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
                    self._shutdown_cleanup()
                    break
            time.sleep(self.config.runtime.loop_sleep_seconds)

    def _shutdown_cleanup(self) -> None:
        """Three-step cleanup with per-step isolation so a failure in one
        (e.g. disk full during session report) doesn't skip the rest.
        Stop dashboard first so HTTP handlers can't reach into data_feed
        state being torn down by stop_streaming."""
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

    def _write_session_report(self) -> None:
        write_session_report(
            self.account,
            self.positions,
            strategy=self.config.strategy,
            dry_run=self.config.schwab.dry_run,
            log_dir=self.config.runtime.log_dir,
            structured_logger=self.audit.log_structured,
            skip_counts=dict(self.entry_gatekeeper.session_skip_counts),
        )
        if bool(self.config.runtime.export_session_archive):
            try:
                self._export_session_archive()
            except Exception as exc:
                # Archive export is a debug aid — never let it crash shutdown.
                LOG.warning("Session archive export failed: %s", exc, exc_info=True)

    def _export_session_archive(self) -> None:
        """Thin wrapper that delegates to ``session_report.export_session_archive``.

        Kept on the engine so the call site in ``_write_session_report``
        can stay symmetric with ``write_session_report``. All the actual
        I/O lives in ``session_report.py``.
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

        self.data.begin_cycle()
        try:
            now = now_et()
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
            watchlist = self.strategy.active_watchlist(self.last_candidates, self.positions)
            # Normalize symbols to upper().strip() at the source so every
            # downstream consumer (parallel maps, bars.setdefault fallback,
            # warmup tracker, API state) sees the same canonical form.
            # Without this, a non-uppercase symbol from a strategy could
            # produce two bars dict entries — one keyed uppercase from
            # _parallel_symbol_map, one keyed original-case from
            # bars.setdefault — with different frame ids and so different
            # context-cache keys, causing pre-warm hits to miss in
            # entry_signals' per-candidate loop.
            if gate_state.idle_closed_market:
                self.last_watchlist = []
            else:
                normalized: list[str] = []
                seen: set[str] = set()
                for sym in watchlist:
                    key = str(sym or "").upper().strip()
                    if key and key not in seen:
                        seen.add(key)
                        normalized.append(key)
                self.last_watchlist = sorted(normalized)
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

            # Per-symbol history fetch decisions are made serially (they read
            # warmup_tracker state and are cheap), but the actual HTTP fetches
            # run in parallel — the bot was previously paying ~watchlist_size
            # network round-trips serially during refresh cycles.
            #
            # Keys are normalized to the same uppercase+strip form that
            # _parallel_symbol_map applies before dispatching, so the lambda's
            # dict lookup is guaranteed to match the symbol it receives.
            history_fetch_targets: dict[str, int] = {}
            for symbol in self.last_watchlist:
                symbol_key = str(symbol or "").upper().strip()
                if not symbol_key:
                    continue
                should_fetch, required_bars = self.warmup_tracker.should_fetch_symbol_history(
                    symbol,
                    context_refresh_active=gate_state.context_refresh_active,
                    streaming_active=gate_state.streaming_active,
                )
                if should_fetch:
                    history_fetch_targets[symbol_key] = self.warmup_tracker.history_fetch_lookback_minutes(
                        now,
                        streaming_active=gate_state.streaming_active,
                        required_bars=self.warmup_tracker.desired_history_bars(symbol),
                    )
            if history_fetch_targets:
                self._parallel_symbol_map(
                    list(history_fetch_targets),
                    lambda symbol: self.data.fetch_history(
                        symbol,
                        lookback_minutes=history_fetch_targets[symbol],
                    ),
                    label="History fetch",
                )

            if gate_state.context_refresh_active and getattr(self.config, "support_resistance", None) is not None and bool(self.config.support_resistance.enabled):
                sr_tf = self.position_manager.active_sr_timeframe_minutes()
                sr_refresh = self.position_manager.active_sr_refresh_seconds()
                sr_lookback = self.position_manager.active_sr_lookback_days()
                # Pre-normalize so the should_refresh check and the threaded
                # fetch agree on cache keys (data_feed._symbol_key applies the
                # same upper().strip()).
                sr_fetch_targets: list[str] = []
                for symbol in self.last_watchlist:
                    symbol_key = str(symbol or "").upper().strip()
                    if not symbol_key:
                        continue
                    if self.data.should_refresh_support_resistance(
                        symbol_key, timeframe_minutes=sr_tf, refresh_seconds=sr_refresh
                    ):
                        sr_fetch_targets.append(symbol_key)
                if sr_fetch_targets:
                    self._parallel_symbol_map(
                        sr_fetch_targets,
                        lambda symbol: self.data.fetch_support_resistance(
                            symbol,
                            timeframe_minutes=sr_tf,
                            lookback_days=sr_lookback,
                            refresh_seconds=sr_refresh,
                        ),
                        label="Support/resistance fetch",
                    )

            if gate_state.streaming_active and self.last_watchlist:
                self.data.start_streaming(self.last_watchlist)
            else:
                self.data.stop_streaming()

            bars = self._parallel_symbol_map(
                self.last_watchlist,
                lambda symbol: self.data.get_merged(symbol, with_indicators=True),
                label="Merged frame precompute",
            )
            for symbol in self.last_watchlist:
                bars.setdefault(symbol, self.data.get_merged(symbol, with_indicators=True))
            self._prime_cycle_support_cache(bars, allow_refresh=False)
            self._prime_cycle_context_cache(bars)
            warmup_summary = self.warmup_tracker.warmup_summary(self.last_watchlist, bars=bars)
            self.warmup_tracker.log_warmup_summary(warmup_summary)

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
            if gate_state.management_active:
                self.position_manager.manage_positions(now, bars)
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
            self._publish_state(
                now,
                screening_active=gate_state.screening_active,
                streaming_active=gate_state.streaming_active,
                management_active=gate_state.management_active,
                message=message,
                context_refresh_active=gate_state.context_refresh_active,
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

    def _cycle_precompute_workers(self) -> int:
        try:
            configured = int(getattr(getattr(self.config, "runtime", None), "cycle_precompute_workers", 4) or 4)
        except Exception:
            configured = 4
        return max(1, configured)

    def _parallel_symbol_map(self, symbols: list[str], func, *, label: str) -> dict[str, Any]:
        ordered: list[str] = []
        seen: set[str] = set()
        for sym in symbols:
            key = str(sym or "").upper().strip()
            if key and key not in seen:
                seen.add(key)
                ordered.append(key)
        if not ordered:
            return {}
        workers = min(self._cycle_precompute_workers(), len(ordered))
        if workers <= 1:
            return {symbol: func(symbol) for symbol in ordered}
        results: dict[str, Any] = {}
        failed: list[str] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bot-precompute") as executor:
            futures = {executor.submit(func, symbol): symbol for symbol in ordered}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    results[symbol] = future.result()
                except Exception as exc:
                    failed.append(symbol)
                    LOG.warning("%s failed for %s: %s", label, symbol, exc, exc_info=True)
        if failed:
            # Emit an aggregate audit event so operators can see when a
            # precompute cycle is silently dropping symbols. Individual
            # per-symbol log lines above carry the traceback for debugging.
            self.audit.log_structured(
                "PRECOMPUTE_FAILURES",
                {"label": label, "total": len(ordered), "failed_count": len(failed), "failed_symbols": failed},
            )
        return results

    def _prime_cycle_support_cache(self, bars: dict[str, pd.DataFrame], *, allow_refresh: bool) -> None:
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
                timeframe_minutes=self.position_manager.active_sr_timeframe_minutes(),
                lookback_days=self.position_manager.active_sr_lookback_days(),
                refresh_seconds=self.position_manager.active_sr_refresh_seconds(),
                allow_refresh=allow_refresh,
                use_prior_day_high_low=bool(getattr(sr_cfg, "use_prior_day_high_low", True)),
                use_prior_week_high_low=bool(getattr(sr_cfg, "use_prior_week_high_low", True)),
            )

        self._parallel_symbol_map(symbols, _compute, label="Support/resistance precompute")

    def _prime_cycle_context_cache(self, bars: dict[str, pd.DataFrame]) -> None:
        """Pre-warm strategy chart/structure/technical caches in parallel.

        Strategies populate three per-symbol context caches lazily inside
        their per-candidate entry_signals loop. Each context build does
        non-trivial GIL-releasing work — TA-Lib chart-pattern detection,
        pivot/ATR market-structure analysis, and the
        Fibonacci/trendline/channel/Bollinger technical-levels stack —
        and the per-candidate frame is a fresh `.copy()` from
        get_merged() so different candidates always cache-miss. Running
        the builders in parallel across the watchlist before
        entry_signals starts amortizes that work across cycle_precompute
        workers instead of paying it serially in the entry-window
        critical path.

        Auto-detect: each context builder records its call signature in
        `BaseStrategy._observed_contexts` (a class-level set of tuples like
        `("structure", "1m")`). On cycle 1 the set is empty and this method
        is a no-op — the strategy runs lazy. From cycle 2 onward, only the
        contexts the strategy actually invokes are pre-warmed, in parallel
        across the watchlist via `_parallel_symbol_map`. New code paths
        that hit a previously-unseen context register on first invocation
        and join the pre-warm set thereafter (self-healing).

        Cache writes inside each builder are guarded by per-cache RLocks,
        so distinct workers writing distinct keys don't race.
        """
        # Snapshot the observed set so workers iterating it can't trip on
        # a concurrent mutation if a builder happens to record a previously-
        # unseen tuple mid-cycle. In practice, pre-warm only replays known
        # entries (idempotent set.add → no size change), but a frozenset
        # eliminates any race-window doubt for the cost of one shallow copy.
        observed = frozenset(getattr(type(self.strategy), "_observed_contexts", ()))
        if not observed:
            return
        symbols = [symbol for symbol, frame in bars.items() if frame is not None and not frame.empty]
        if not symbols:
            return
        # Cycle-boundary reset — owned by the engine now, NOT by entry_signals.
        # Strategies still call _reset_entry_decisions() at the top of
        # entry_signals(), but that method no longer touches the 3 context
        # caches the engine just (or is about to) pre-warm.
        self.strategy.reset_context_caches()

        def _warm(symbol: str) -> Any:
            frame = bars.get(symbol)
            if frame is None or frame.empty:
                return None
            self.strategy.prime_cycle_contexts(frame, observed)
            return None

        self._parallel_symbol_map(symbols, _warm, label="Strategy context precompute")

    def _publish_state(self, now: datetime, screening_active: bool, streaming_active: bool, management_active: bool, message: str, context_refresh_active: bool = False, gate_state: CycleGateState | None = None, warmup_summary: dict[str, Any] | None = None) -> None:
        performance = self.account.snapshot_copy(self.positions)
        candidates = []
        entry_decision_by_symbol = {str(symbol or '').upper().strip(): copy.deepcopy(payload) for symbol, payload in (self.entry_gatekeeper.last_entry_decisions or {}).items() if str(symbol or '').upper().strip()}
        candidate_limit = self.dashboard_cache.candidate_limit()
        symbol_exchanges: dict[str, str] = {}

        def remember_exchange(symbol_value: Any, exchange_value: Any = None) -> None:
            symbol_key = str(symbol_value or '').upper().strip()
            if not symbol_key:
                return
            normalized_exchange = dashboard_normalize_exchange(exchange_value)
            if normalized_exchange is None:
                normalized_exchange = dashboard_quote_exchange(self.data.get_quote(symbol_key) or {})
            if normalized_exchange:
                symbol_exchanges[symbol_key] = normalized_exchange

        all_candidate_rows: list[dict[str, Any]] = []
        for c in self.last_candidates:
            remember_exchange(c.symbol, c.metadata.get("exchange"))
            exchange = dashboard_normalize_exchange(c.metadata.get("exchange"))
            row = {
                "symbol": c.symbol,
                "rank": c.rank,
                "activity_score": c.activity_score,
                "exchange": exchange or None,
                "change_from_open": c.metadata.get("change_from_open"),
                "close": c.metadata.get("close"),
                "volume": c.metadata.get("volume"),
                "directional_bias": c.directional_bias.value if c.directional_bias else None,
            }
            all_candidate_rows.append(row)
            if len(candidates) < candidate_limit:
                candidates.append(copy.deepcopy(row))

        sr_symbols: list[str] = []
        seen: set[str] = set()
        for row in performance.get("positions", []):
            sym = str(row.get("underlying") or row.get("symbol") or "").upper().strip()
            if sym and sym not in seen:
                seen.add(sym)
                sr_symbols.append(sym)
        for sym in list(self.last_quote_watchlist) + list(self.last_watchlist) + [c.get("symbol") for c in candidates]:
            symbol = str(sym or "").upper().strip()
            if symbol and symbol not in seen:
                seen.add(symbol)
                sr_symbols.append(symbol)
        sr_symbols = sr_symbols[:12]

        sr_levels = []
        sr_by_symbol: dict[str, dict[str, Any]] = {}
        for symbol in sr_symbols:
            row = self.dashboard_cache.sr_row(symbol, allow_refresh=context_refresh_active)
            if row is None:
                continue
            sr_levels.append(row)
            sr_by_symbol[symbol] = row

        if performance.get("positions"):
            enriched_positions = []
            for row in performance["positions"]:
                symbol = str(row.get("underlying") or row.get("symbol") or "").upper().strip()
                sr_row = sr_by_symbol.get(symbol) or self.dashboard_cache.sr_row(symbol, allow_refresh=context_refresh_active)
                new_row = copy.deepcopy(row)
                if sr_row is not None:
                    new_row.update({
                        "sr_symbol": symbol,
                        "sr_timeframe": sr_row.get("timeframe"),
                        "sr_nearest_support": sr_row.get("nearest_support"),
                        "sr_nearest_resistance": sr_row.get("nearest_resistance"),
                        "sr_support_distance_pct": sr_row.get("support_distance_pct"),
                        "sr_resistance_distance_pct": sr_row.get("resistance_distance_pct"),
                        "sr_regime_hint": sr_row.get("regime_hint"),
                        "sr_state": sr_row.get("state"),
                    })
                enriched_positions.append(new_row)
            performance["positions"] = enriched_positions

        candidate_by_symbol = {str(row.get("symbol") or "").upper().strip(): row for row in all_candidate_rows}
        position_by_symbol: dict[str, dict[str, Any]] = {}
        for row in performance.get("positions", []):
            base_symbol = str(row.get("underlying") or row.get("symbol") or "").upper().strip()
            if base_symbol and base_symbol not in position_by_symbol:
                position_by_symbol[base_symbol] = row

        if warmup_summary is None:
            warmup_summary = self.warmup_tracker.warmup_summary(self.last_watchlist)
        warmup_by_symbol = {
            str(item.get('symbol') or '').upper().strip(): item
            for item in (warmup_summary.get('symbols') or [])
            if str(item.get('symbol') or '').upper().strip()
        }

        dashboard_symbol_order: list[str] = []
        seen_dashboard_symbols: set[str] = set()
        for bucket in (
            [str(row.get("underlying") or row.get("symbol") or "").upper().strip() for row in performance.get("positions", [])],
            [str(sym or "").upper().strip() for sym in self.last_watchlist],
            [str(sym or "").upper().strip() for sym in self.last_quote_watchlist],
            [str(row.get("symbol") or "").upper().strip() for row in candidates],
            [str(row.get("symbol") or "").upper().strip() for row in sr_levels],
        ):
            for symbol in bucket:
                if symbol and symbol not in seen_dashboard_symbols:
                    seen_dashboard_symbols.add(symbol)
                    dashboard_symbol_order.append(symbol)

        for row in performance.get("positions", []):
            remember_exchange(row.get("underlying") or row.get("symbol"))
        for trade in performance.get("recent_trades", []):
            remember_exchange(trade.get("underlying") or trade.get("symbol"))
        for symbol in dashboard_symbol_order:
            remember_exchange(symbol)

        dashboard_symbols = [
            self.dashboard_cache.symbol_snapshot(
                symbol,
                exchange=symbol_exchanges.get(symbol),
                sr_row=sr_by_symbol.get(symbol),
                candidate_row=candidate_by_symbol.get(symbol),
                position_row=position_by_symbol.get(symbol),
                entry_decision=entry_decision_by_symbol.get(symbol),
                warmup=warmup_by_symbol.get(symbol),
                allow_refresh=context_refresh_active,
            )
            for symbol in dashboard_symbol_order
        ]
        for snapshot in dashboard_symbols:
            remember_exchange(snapshot.get('symbol'), snapshot.get('exchange'))
        active_dashboard_symbol_set = {str(symbol or '').upper().strip() for symbol in dashboard_symbol_order if str(symbol or '').upper().strip()}
        with self.dashboard_cache.lock:
            if self.dashboard_cache.snapshot_cache:
                self.dashboard_cache.snapshot_cache = {
                    key: value
                    for key, value in self.dashboard_cache.snapshot_cache.items()
                    if key in active_dashboard_symbol_set
                }
            if self.dashboard_cache.chart_cache:
                self.dashboard_cache.chart_cache = {
                    key: value
                    for key, value in self.dashboard_cache.chart_cache.items()
                    if key[0] in active_dashboard_symbol_set
                }

        runtime_gate_state = gate_state or self.cycle_gate.evaluate(now, self.config.active_strategy.schedule())
        payload = {
            "status": "running" if self.last_error is None else "error",
            "entry_window_active": runtime_gate_state.entry_actionable,
            "management_window_active": runtime_gate_state.intraday_session_day and runtime_gate_state.management_window_open,
            "management_active": management_active,
            "position_monitoring_active": runtime_gate_state.position_monitoring_active,
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
            "data": {
                **self.data.dashboard_data_snapshot(),
                "non_streamable_symbols": sorted(NON_STREAMABLE),
                "tradable_symbols": self.dashboard_cache.tradable_symbols(),
            },
            "warmup": warmup_summary,
            "api_usage": self.api_usage.snapshot(now),
            "performance": performance,
            "tracked_capital_label": self._tracked_capital_label(),
            "candidates": candidates,
            "symbol_exchanges": symbol_exchanges,
            "dashboard_charting": self.dashboard_cache.charting_settings(),
            "dashboard_symbols": dashboard_symbols,
        }
        if self.dashboard is not None:
            self.dashboard.publish(payload)
