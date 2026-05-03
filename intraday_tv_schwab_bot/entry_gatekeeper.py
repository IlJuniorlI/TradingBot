# SPDX-License-Identifier: MIT
"""EntryGatekeeper — owns the entry cycle.

Extracted from ``IntradayBot`` as Phase 5 Step 10 of the Phase 5 engine split.
Holds the logic that evaluates candidates each cycle, builds signals, runs
risk gates, submits entry orders, and reconciles broker state when an
order result is ambiguous. Also owns entry-decision logging and the per-
cycle summary that the EOD session report consumes.

Design notes:

- ``self.positions`` is a **shared-reference** dict with the owning
  ``IntradayBot`` / ``PositionManager``. Entry insertions here
  (``self.positions[symbol] = position``) are immediately visible to the
  position-management cycle on the next iteration.
- ``last_entry_decisions`` + ``session_skip_counts`` are owned by
  EntryGatekeeper; engine reads them via ``self.entry_gatekeeper.*`` for
  dashboard publish and EOD session report.
- ``save_reconcile_metadata`` injected as callable because the exit path
  (PositionManager) also calls it. Single source of truth on engine.
- ``is_startup_reconcile_entry_blocked`` injected as callable — the
  startup-reconcile state (`_startup_reconcile_entry_block_symbols`) lives
  on engine because startup reconciliation is engine-level bootstrap.
- Broker-query helpers (``broker_position_row`` / ``broker_position_rows``)
  live here because entry recovery is their primary consumer.
  ``PositionManager`` receives them as callables (its exit-recovery path
  is the only other consumer).
- Config accessor ``stock_position_trail_pct`` moved here even though
  ``_restore_levels_for_stock_position`` on engine still needs it —
  engine dispatches through ``self.entry_gatekeeper.stock_position_trail_pct``.
"""
from __future__ import annotations

import copy
import logging
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Callable

from schwabdev import Client

from .audit_logger import AuditLogger
from .broker_positions import (
    broker_position_side_qty,
    extract_broker_positions,
    order_result_needs_broker_recheck,
)
from .config import BotConfig
from .data_feed import MarketDataStore
from .execution import SchwabExecutor
from .models import (
    ASSET_TYPE_OPTION_SINGLE,
    ASSET_TYPE_OPTION_VERTICAL,
    OPTION_ASSET_TYPES,
    Candidate,
    Position,
    Side,
)
from .paper_account import PaperAccount
from .position_manager import PositionManager
from .position_metrics import safe_float
from .risk import RiskManager
from ._strategies.strategy_base import BaseStrategy
from .utils import TRADEFLOW_LEVEL, call_schwab_client, now_et

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


# Skip reasons that fire on almost every cycle while a position is open and
# aren't useful signal. They still count in session_skip_counts (visible in
# the EOD filter-rejection tally) but are logged at DEBUG and suppressed
# from structured SKIP_SUMMARY so the TRADEFLOW log stays focused on real
# decision events. Cosmetic filtering only — no functional effect.
_NOISY_SKIP_REASONS: frozenset[str] = frozenset({
    "already_in_position",
})


class EntryGatekeeper:
    def __init__(
        self,
        config: BotConfig,
        *,
        client: Client,
        data: MarketDataStore,
        executor: SchwabExecutor,
        risk: RiskManager,
        audit: AuditLogger,
        account: PaperAccount,
        strategy: BaseStrategy,
        positions: dict[str, Position],
        position_manager: PositionManager | None,
        save_reconcile_metadata: Callable[[], None],
        is_startup_reconcile_entry_blocked: Callable[[str], bool],
    ) -> None:
        self.config = config
        self.client = client
        self.data = data
        self.executor = executor
        self.risk = risk
        self.audit = audit
        self.account = account
        self.strategy = strategy
        self.positions = positions
        self.position_manager = position_manager
        self._save_reconcile_metadata = save_reconcile_metadata
        self._is_startup_reconcile_entry_blocked = is_startup_reconcile_entry_blocked
        # Per-session state (was on IntradayBot before Phase 5 Step 10).
        self.last_entry_decisions: dict[str, dict[str, Any]] = {}
        self._last_entry_decision_log: dict[str, tuple[str, tuple[str, ...], float, str | None]] = {}
        self.session_skip_counts: dict[str, int] = {}
        self._option_entry_retry_until: dict[str, datetime] = {}
        self._option_entry_retry_counts: dict[str, int] = {}
        # Per-cycle broker-positions cache. Without this, every signal in a
        # cycle (entry checks + exit recovery) issues its own account_details
        # call returning identical data; 5 signals = 5 redundant Schwab
        # fetches. The cache is cleared at begin_cycle() and end_cycle() to
        # match data_feed.py's lifecycle pattern. _cycle_positions_failed
        # latches a failed fetch so we don't retry-storm Schwab if the API
        # is unhealthy mid-cycle.
        self._cycle_positions_cache: list[dict[str, Any]] | None = None
        self._cycle_positions_failed: bool = False

    @staticmethod
    def _safe_series_last(frame, field: str, default: float | None = None) -> float | None:
        try:
            if frame is None or frame.empty or field not in frame.columns:
                return default
            value = frame.iloc[-1][field]
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    # ------------------------------------------------------------------
    # Retry-backoff for option entries that failed to fill.
    # ------------------------------------------------------------------

    @staticmethod
    def _option_entry_retry_key(symbol: str, metadata: dict[str, Any] | None = None) -> str:
        meta = metadata or {}
        return str(meta.get("position_key") or symbol)

    def _is_option_entry_retry_blocked(self, symbol: str, metadata: dict[str, Any] | None = None) -> bool:
        key = self._option_entry_retry_key(symbol, metadata)
        until = self._option_entry_retry_until.get(key)
        current = now_et()
        if until and current < until:
            return True
        if until and current >= until:
            self._option_entry_retry_until.pop(key, None)
        return False

    def _register_option_entry_retry_backoff(self, symbol: str, metadata: dict[str, Any] | None = None, seconds: float | None = None) -> None:
        key = self._option_entry_retry_key(symbol, metadata)
        retry_count = int(self._option_entry_retry_counts.get(key, 0)) + 1
        self._option_entry_retry_counts[key] = retry_count
        if seconds is None:
            poll = float(self.config.runtime.quote_poll_seconds)
            if retry_count <= 1:
                seconds = max(6.0, poll * 1.25)
            else:
                seconds = max(10.0, poll * 2.0)
        self._option_entry_retry_until[key] = now_et() + timedelta(seconds=max(1.0, float(seconds)))

    def _clear_option_entry_retry_backoff(self, symbol: str, metadata: dict[str, Any] | None = None) -> None:
        key = self._option_entry_retry_key(symbol, metadata)
        self._option_entry_retry_until.pop(key, None)
        self._option_entry_retry_counts.pop(key, None)

    # ------------------------------------------------------------------
    # Pre-trade level materialization and validation.
    # ------------------------------------------------------------------

    def _materialize_option_position_levels(self, signal, entry_price: float) -> tuple[float, float | None, dict[str, Any]]:
        metadata = dict(signal.metadata or {})
        asset_type = str(metadata.get("asset_type") or "")
        entry_value = max(0.01, float(entry_price))
        if asset_type == ASSET_TYPE_OPTION_VERTICAL and signal.side.value == "LONG":
            debit_stop_frac = float(self.config.options.debit_stop_frac)
            debit_target_mult = float(self.config.options.debit_target_mult)
            # Apply time-decay scaling if the signal builder stored it.
            tds = safe_float(metadata.get("time_decay_scale"), 1.0) or 1.0
            if tds < 1.0:
                debit_target_mult = max(1.01, 1.0 + (debit_target_mult - 1.0) * tds)
                widen = float(getattr(self.config.options, "debit_stop_time_decay_widen_factor", 0.30) or 0.30)
                debit_stop_frac = max(0.01, min(0.99, debit_stop_frac * (1.0 + (1.0 - tds) * widen)))
            stop = entry_value * debit_stop_frac
            target = entry_value * debit_target_mult
            stop = max(0.01, min(stop, entry_value - 0.01))
            target = max(entry_value + 0.01, target)
            metadata["entry_price"] = entry_value
            metadata["max_loss_per_contract"] = entry_value
            width = float(metadata.get("strike_width_dollars") or 0.0)
            if width > 0:
                metadata["max_profit_per_contract"] = max(0.0, width - entry_value)
            return stop, target, metadata
        if asset_type == ASSET_TYPE_OPTION_VERTICAL and signal.side.value == "SHORT":
            width = float(metadata.get("strike_width_dollars") or 0.0)
            adjusted_max_loss = max(0.0, width - entry_value) if width > 0 else float(metadata.get("max_loss_per_contract") or 0.0)
            stop = min(width, entry_value * float(self.config.options.credit_stop_mult)) if width > 0 else entry_value * float(self.config.options.credit_stop_mult)
            stop = max(entry_value + 0.01, stop)
            target = entry_value * float(self.config.options.credit_target_frac)
            target = max(0.01, min(target, entry_value - 0.01))
            metadata["entry_price"] = entry_value
            metadata["entry_credit"] = entry_value
            metadata["max_loss_per_contract"] = adjusted_max_loss
            metadata["max_profit_per_contract"] = max(0.0, entry_value)
            return stop, target, metadata
        if asset_type == ASSET_TYPE_OPTION_SINGLE:
            single_stop_frac = float(self.config.options.single_stop_frac)
            single_target_mult = float(self.config.options.single_target_mult)
            tds = safe_float(metadata.get("time_decay_scale"), 1.0) or 1.0
            if tds < 1.0:
                single_target_mult = max(1.01, 1.0 + (single_target_mult - 1.0) * tds)
                widen = float(getattr(self.config.options, "debit_stop_time_decay_widen_factor", 0.30) or 0.30)
                single_stop_frac = max(0.01, min(0.99, single_stop_frac * (1.0 + (1.0 - tds) * widen)))
            stop = entry_value * single_stop_frac
            target = entry_value * single_target_mult
            stop = max(0.01, min(stop, entry_value - 0.01))
            target = max(entry_value + 0.01, target)
            metadata["entry_price"] = entry_value
            metadata["max_loss_per_contract"] = entry_value
            return stop, target, metadata
        return signal.stop_price, signal.target_price, metadata

    @staticmethod
    def _entry_levels_valid(side, entry_price: float, stop_price: float, target_price: float | None) -> tuple[bool, str | None]:
        try:
            entry = float(entry_price)
            stop = float(stop_price)
        except Exception:
            return False, "invalid_entry_or_stop"
        if entry <= 0 or stop <= 0:
            return False, "invalid_entry_or_stop"
        target = None
        if target_price is not None:
            try:
                target = float(target_price)
            except Exception:
                return False, "invalid_target"
        if str(side.value if hasattr(side, "value") else side).upper() == "LONG":
            if stop >= entry:
                return False, "stop_not_below_entry"
            if target is not None and target <= entry:
                return False, "target_not_above_entry"
        else:
            if stop <= entry:
                return False, "stop_not_above_entry"
            if target is not None and target >= entry:
                return False, "target_not_below_entry"
        return True, None

    def stock_position_trail_pct(self, metadata: dict[str, Any] | None = None, existing_trail_pct: float | None = None) -> float | None:
        mode = self.config.risk.trade_management_mode
        meta = metadata if isinstance(metadata, dict) else {}
        ladder_enabled = mode == "adaptive_ladder" and bool(meta.get("ladder_management_enabled"))
        trail_allowed = mode == "adaptive" or (mode == "adaptive_ladder" and not ladder_enabled)
        if not trail_allowed:
            return None
        candidate = existing_trail_pct
        if candidate is None:
            candidate = getattr(self.config.risk, "trailing_stop_pct", None)
        try:
            candidate = float(candidate)
        except Exception:
            return None
        return float(candidate) if candidate > 0 else None

    @staticmethod
    def _scaled_order_spec(spec: dict[str, Any], qty: int) -> dict[str, Any]:
        payload = copy.deepcopy(spec)
        for leg in payload.get("orderLegCollection", []):
            leg["quantity"] = qty
        return payload

    # ------------------------------------------------------------------
    # Broker position-query helpers (wrap schwabdev calls). Exit recovery
    # in PositionManager also uses these — injected as callables there.
    # By default, all callers within a single engine.step() share one
    # cached snapshot via _get_cycle_positions(); cache is invalidated at
    # begin_cycle() and end_cycle(). Pass force_refresh=True to bypass
    # the cache and issue a fresh account_details fetch.
    # ------------------------------------------------------------------

    def begin_cycle(self) -> None:
        # Mirrors data_feed.MarketDataStore.begin_cycle(). Called from
        # engine.step() so per-cycle caches start fresh each tick.
        self._cycle_positions_cache = None
        self._cycle_positions_failed = False

    def end_cycle(self) -> None:
        # Symmetric clear so a stale snapshot can never leak into the
        # gap between cycles (defensive — begin_cycle() also clears).
        self._cycle_positions_cache = None
        self._cycle_positions_failed = False

    def _fetch_broker_positions_uncached(self) -> list[dict[str, Any]] | None:
        # Issue an unconditional account_details fetch, bypassing the
        # cycle cache. Used by force_refresh callers and as the underlying
        # implementation of the cycle-cached path. Returns None on Schwab
        # failure to match the legacy broker_position_row contract.
        try:
            account = call_schwab_client(self.client, "account_details", self.executor.account_hash, fields="positions").json()
            return extract_broker_positions(account)
        except Exception as exc:
            LOG.warning("Could not query broker position snapshot: %s", exc)
            return None

    def _get_cycle_positions(self) -> list[dict[str, Any]] | None:
        # Return the broker positions list for the current cycle, fetching
        # once if not yet cached. Returns None if the fetch failed (caller
        # treats as 'no broker data', matching the legacy behaviour where
        # an exception in broker_position_row returned None). A failed
        # fetch latches via _cycle_positions_failed so subsequent callers
        # within the same cycle don't retry-storm Schwab.
        if self._cycle_positions_failed:
            return None
        if self._cycle_positions_cache is not None:
            return self._cycle_positions_cache
        rows = self._fetch_broker_positions_uncached()
        if rows is None:
            self._cycle_positions_failed = True
            return None
        self._cycle_positions_cache = rows
        return rows

    def broker_position_row(self, symbol: str, *, force_refresh: bool = False) -> dict[str, Any] | None:
        # Default: returns the row from the cycle-cached snapshot
        # (account_details fetched at most once per engine.step()).
        # force_refresh=True bypasses the cache and issues a fresh fetch
        # — use only when the cycle-start snapshot would be misleading
        # (e.g., post-place_order verification before the next cycle).
        symbol_upper = str(symbol or "").upper().strip()
        if not symbol_upper:
            return None
        rows = self._fetch_broker_positions_uncached() if force_refresh else self._get_cycle_positions()
        if rows is None:
            return None
        for row in rows:
            row_symbol = str(row.get("symbol") or "").upper().strip()
            if row_symbol == symbol_upper:
                return row
        return None

    def broker_position_rows(self, symbols: list[str], *, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
        # Same snapshot semantics as broker_position_row — see its
        # docstring for the force_refresh contract.
        wanted = {str(symbol or "").upper().strip() for symbol in (symbols or []) if str(symbol or "").strip()}
        if not wanted:
            return {}
        rows = self._fetch_broker_positions_uncached() if force_refresh else self._get_cycle_positions()
        if rows is None:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            row_symbol = str(row.get("symbol") or "").upper().strip()
            if row_symbol in wanted:
                out[row_symbol] = row
        return out

    # ------------------------------------------------------------------
    # Entry-time snapshot builders (context logged at entry decision).
    # ------------------------------------------------------------------

    def _candidate_snapshot(self, candidate: Candidate | None, bars) -> dict[str, Any]:
        if candidate is None:
            return {}
        frame = bars.get(candidate.symbol) if bars else None
        meta = dict(candidate.metadata or {})
        bar_time = None
        try:
            if frame is not None and not frame.empty:
                bar_idx = frame.index[-1]
                if hasattr(bar_idx, 'isoformat'):
                    bar_time = bar_idx.isoformat()
                else:
                    bar_time = str(bar_idx)
        except Exception:
            bar_time = None
        live_entry_status = self.data.live_entry_bar_status(candidate.symbol)
        out = {
            'symbol': candidate.symbol,
            'candidate_rank': candidate.rank,
            'candidate_activity_score': float(candidate.activity_score),
            'candidate_directional_bias': candidate.directional_bias.value if candidate.directional_bias else None,
            'exchange': meta.get('exchange'),
            'bar_time': bar_time,
            'close': self._safe_series_last(frame, 'close', safe_float(meta.get('close'), None)),
            'vwap': self._safe_series_last(frame, 'vwap', None),
            'ret5': self._safe_series_last(frame, 'ret5', None),
            'ret15': self._safe_series_last(frame, 'ret15', None),
            'ema9': self._safe_series_last(frame, 'ema9', None),
            'ema20': self._safe_series_last(frame, 'ema20', None),
            'volume': self._safe_series_last(frame, 'volume', safe_float(meta.get('volume'), None)),
            'change_from_open': safe_float(meta.get('change_from_open'), None),
            'relative_volume': safe_float(meta.get('relative_volume_10d_calc') or meta.get('relative_volume'), None),
            'live_entry_ready': bool(live_entry_status.get('ready', True)),
            'live_entry_blocker': live_entry_status.get('reason'),
            'last_live_stream_bar_time': live_entry_status.get('last_stream_bar_time'),
            'last_live_stream_bar_age_seconds': live_entry_status.get('last_stream_bar_age_seconds'),
        }
        try:
            structure_lists = getattr(self.strategy, '_structure_lists', None)
            structure_context = getattr(self.strategy, '_structure_context', None)
            if callable(structure_lists) and callable(structure_context):
                ms1_ctx = structure_context(frame, '1m')
                ms1_fields = structure_lists(ms1_ctx, prefix='ms1m')
                if isinstance(ms1_fields, Mapping):
                    out.update({k: v for k, v in ms1_fields.items() if v is not None})
                current_price = safe_float(out.get('close'), None)
                sr_ctx = self.data.get_support_resistance(candidate.symbol, current_price=current_price, flip_frame=frame, mode="dashboard", timeframe_minutes=self.position_manager.active_sr_timeframe_minutes(), lookback_days=self.position_manager.active_sr_lookback_days(), refresh_seconds=self.position_manager.active_sr_refresh_seconds())
                mshtf_ctx = getattr(sr_ctx, 'market_structure', None) if sr_ctx is not None else None
                if mshtf_ctx is not None:
                    mshtf_fields = structure_lists(mshtf_ctx, prefix='mshtf')
                    if isinstance(mshtf_fields, Mapping):
                        out.update({k: v for k, v in mshtf_fields.items() if v is not None})
        except Exception:
            LOG.debug("Failed to enrich dashboard candidate structure payload; returning partial payload.", exc_info=True)
        return {k: v for k, v in out.items() if v is not None}

    @staticmethod
    def structured_metadata_snapshot(meta: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(meta, Mapping):
            return {}
        include_keys = {
            'benchmark', 'zscore', 'side_preference', 'runner_target_applied', 'qualifying_target_count',
            'trigger_score_required', 'min_peer_score_required', 'strong_setup_trigger_score_required', 'strong_setup_peer_score_required',
            'or_high', 'or_low', 'pullback_high', 'pullback_low', 'trigger_high', 'trigger_low',
            'support_low', 'resistance_high', 'extension_from_vwap_pct', 'peer_details', 'macro_details',
            'spread_side', 'spread_style', 'spread_type', 'entry_price_points', 'entry_credit',
            'bought_leg_symbol', 'sold_leg_symbol', 'bought_strike', 'sold_strike',
            'htf_timeframe_minutes', 'nearest_htf_support', 'nearest_htf_resistance',
            'broken_htf_support', 'broken_htf_resistance', 'prior_day_high', 'prior_day_low',
            'prior_week_high', 'prior_week_low', 'htf_ema_fast', 'htf_ema_slow', 'htf_atr14',
            'htf_trend_bias', 'htf_level_buffer', 'nearest_htf_bullish_fvg', 'nearest_htf_bearish_fvg',
            'source_priority', 'selection_score', 'selection_trigger_score', 'hourly_vote_edge',
            'macro_agreement_count', 'selection_quality_score', 'activity_score', 'setup_quality_score',
            'execution_quality_score', 'macro_score', 'entry_family', 'peer_universe', 'side_eval',
            'family_eval', 'evaluated_sides', 'primary_blocker', 'all_blockers', 'near_miss_blockers',
            'selection_components', 'candidate_reason', 'decision_summary',
        }
        include_prefixes = (
            'fvg_', 'htf_fvg_', 'adaptive_', 'anti_chase_fvg_retest_',
            'ms1m_', 'mshtf_', 'sr_', 'tech_', 'matched_', 'chart_pattern_',
            'decision_', 'gate_', 'peak_giveback_', 'orb_',
        )
        exclude_keys = {
            'order_spec', 'long_leg', 'short_leg', 'option_leg', 'valuation_legs',
            'htf_bullish_fvgs', 'htf_bearish_fvgs',
        }
        out: dict[str, Any] = {}
        for key, value in meta.items():
            if value is None or key in exclude_keys:
                continue
            if key in include_keys or any(str(key).startswith(prefix) for prefix in include_prefixes):
                out[str(key)] = value
        return out

    def _signal_snapshot(
        self,
        signal,
        qty: int | None,
        entry_preview: float | None,
        result_message: str | None = None,
        market_snapshot: Mapping[str, Any] | None = None,
        order_intent: Any | None = None,
    ) -> dict[str, Any]:
        meta = dict(signal.metadata or {})
        out = {
            'strategy': signal.strategy,
            'signal_symbol': signal.symbol,
            'signal_side': signal.side.value,
            'signal_reason': signal.reason,
            'stop_price': safe_float(signal.stop_price, None),
            'target_price': safe_float(signal.target_price, None),
            'qty': qty,
            'entry_preview': safe_float(entry_preview, None),
            'decision_price': safe_float(entry_preview, None),
            'result_message': result_message,
            'order_intent': getattr(order_intent, 'value', order_intent),
            'decision_source': market_snapshot.get('source') if isinstance(market_snapshot, Mapping) else None,
            'decision_bid': safe_float(market_snapshot.get('bid'), None) if isinstance(market_snapshot, Mapping) else None,
            'decision_ask': safe_float(market_snapshot.get('ask'), None) if isinstance(market_snapshot, Mapping) else None,
            'decision_last': safe_float(market_snapshot.get('last'), None) if isinstance(market_snapshot, Mapping) else None,
            'asset_type': meta.get('asset_type'),
            'style': meta.get('style'),
            'direction': meta.get('direction'),
            'regime': meta.get('regime'),
            'regime_scores': meta.get('regime_scores'),
            'regime_metrics': meta.get('regime_metrics'),
            'confirm_index': meta.get('confirm_index'),
            'underlying_entry': safe_float(meta.get('underlying_entry'), None),
            'breakeven_underlying': safe_float(meta.get('breakeven_underlying'), None),
            'max_loss_per_contract': safe_float(meta.get('max_loss_per_contract'), None),
            'max_profit_per_contract': safe_float(meta.get('max_profit_per_contract'), None),
            'limit_price': safe_float(meta.get('limit_price'), None),
            'mark_price_hint': safe_float(meta.get('mark_price_hint'), None),
            'natural_bid': safe_float(meta.get('natural_bid'), None),
            'natural_ask': safe_float(meta.get('natural_ask'), None),
            'option_type': meta.get('option_type'),
            'option_symbol': meta.get('option_symbol'),
            'option_strike': safe_float(meta.get('option_strike'), None),
            'long_leg_symbol': meta.get('long_leg_symbol'),
            'short_leg_symbol': meta.get('short_leg_symbol'),
            'long_strike': safe_float(meta.get('long_strike'), None),
            'short_strike': safe_float(meta.get('short_strike'), None),
            'strike_width_dollars': safe_float(meta.get('strike_width_dollars'), None),
            'entry_price_model': safe_float(meta.get('entry_price'), None),
            'position_key': meta.get('position_key'),
            'final_priority_score': safe_float(meta.get('final_priority_score'), None),
            'selection_quality_score': safe_float(meta.get('selection_quality_score'), None),
            'activity_score': safe_float(meta.get('activity_score'), None),
            'setup_quality_score': safe_float(meta.get('setup_quality_score'), None),
            'execution_quality_score': safe_float(meta.get('execution_quality_score'), None),
            'sr_entry_adjustment': safe_float(meta.get('sr_entry_adjustment'), None),
            'sr_directional_bias': safe_float(meta.get('sr_directional_bias'), None),
            'sr_bias_component': safe_float(meta.get('sr_bias_component'), None),
            'sr_favorable_proximity_score': safe_float(meta.get('sr_favorable_proximity_score'), None),
            'sr_opposing_proximity_score': safe_float(meta.get('sr_opposing_proximity_score'), None),
            'technical_entry_adjustment': safe_float(meta.get('technical_entry_adjustment'), None),
            'entry_context_adjustment': safe_float(meta.get('entry_context_adjustment'), None),
            'level_kind': meta.get('level_kind'),
            'level_price': safe_float(meta.get('level_price'), None),
            'level_score': safe_float(meta.get('level_score'), None),
            'trigger_score': safe_float(meta.get('trigger_score'), None),
            'trigger_reasons': meta.get('trigger_reasons'),
            'hourly_bias': meta.get('hourly_bias'),
            'hourly_bull_votes': safe_float(meta.get('hourly_bull_votes'), None),
            'hourly_bear_votes': safe_float(meta.get('hourly_bear_votes'), None),
            'peer_score': safe_float(meta.get('peer_score'), None),
            'peer_bullish': safe_float(meta.get('peer_bullish'), None),
            'peer_bearish': safe_float(meta.get('peer_bearish'), None),
            'peer_universe': meta.get('peer_universe'),
            'entry_family': meta.get('entry_family'),
            'side_eval': meta.get('side_eval'),
            'family_eval': meta.get('family_eval'),
            'macro_long_agree': safe_float(meta.get('macro_long_agree'), None),
            'macro_short_agree': safe_float(meta.get('macro_short_agree'), None),
            'nearest_target_level_kind': meta.get('nearest_target_level_kind'),
            'nearest_target_level_price': safe_float(meta.get('nearest_target_level_price'), None),
            'nearest_target_clearance_pct': safe_float(meta.get('nearest_target_clearance_pct'), None),
            'nearest_target_clearance_atr': safe_float(meta.get('nearest_target_clearance_atr'), None),
        }
        extra = self.structured_metadata_snapshot(meta)
        out.update({k: v for k, v in extra.items() if k not in out and v is not None})
        return {k: v for k, v in out.items() if v is not None}

    def _risk_snapshot(self, signal, entry_price: float | None, qty: int | None) -> dict[str, Any]:
        entry = float(entry_price) if entry_price is not None else None
        stop = safe_float(signal.stop_price, None)
        qty_i = int(qty) if qty is not None else None
        out: dict[str, Any] = {}
        if signal.metadata.get('asset_type') in {'OPTION_VERTICAL', 'OPTION_SINGLE'}:
            per_contract = safe_float(signal.metadata.get('max_loss_per_contract'), None)
            out.update({
                'risk_option_budget': float(self.config.options.max_loss_per_trade),
                'risk_max_contracts_per_trade': int(self.config.options.max_contracts_per_trade),
                'risk_max_loss_per_contract': per_contract,
                'risk_max_loss_total': (per_contract * qty_i) if per_contract is not None and qty_i is not None else None,
            })
        elif entry is not None and stop is not None and qty_i is not None:
            stop_distance = abs(entry - stop)
            out.update({
                'risk_stop_distance': stop_distance,
                'risk_budget_dollars': float(self.config.risk.max_notional_per_trade * self.config.risk.risk_per_trade_frac_of_notional),
                'risk_notional': entry * qty_i,
                'risk_est_max_loss': stop_distance * qty_i,
                'risk_max_notional_per_trade': float(self.config.risk.max_notional_per_trade),
                'risk_per_trade_frac_of_notional': float(self.config.risk.risk_per_trade_frac_of_notional),
            })
        return {k: v for k, v in out.items() if v is not None}

    def _entry_context_payload(
        self,
        signal,
        candidate: Candidate | None,
        bars,
        qty: int | None,
        entry_price: float | None,
        result_message: str | None = None,
        result=None,
        market_snapshot: Mapping[str, Any] | None = None,
        order_intent: Any | None = None,
    ) -> dict[str, Any]:
        payload = {
            **self._candidate_snapshot(candidate, bars),
            **self._signal_snapshot(signal, qty, entry_price, result_message, market_snapshot=market_snapshot, order_intent=order_intent),
            **self._risk_snapshot(signal, entry_price, qty),
        }
        if result is not None:
            payload.update({
                "attempt_status": "filled" if bool(getattr(result, "ok", False)) else "not_filled",
                "fill_price": safe_float(getattr(result, "fill_price", None), None),
                "filled_qty": int(getattr(result, "filled_qty", 0) or 0) if getattr(result, "filled_qty", None) is not None else None,
            })
        return {k: v for k, v in payload.items() if v is not None}

    def _signal_priority_key(self, signal, candidate: Candidate | None) -> tuple[float, ...]:
        meta = dict(signal.metadata or {})
        explicit_strength = safe_float(meta.get("final_priority_score"), None)
        priority_tiebreak = safe_float(meta.get("selection_quality_score"), None)
        candidate_activity_score = float(candidate.activity_score) if candidate is not None else 0.0
        strength = float(explicit_strength) if explicit_strength is not None else candidate_activity_score
        rank = float(candidate.rank) if candidate is not None else 9_999.0
        secondary = candidate_activity_score
        tertiary = 0.0
        strategy_obj = getattr(self, "strategy", None)
        if strategy_obj is not None:
            try:
                custom_key = strategy_obj.signal_priority_key(
                    signal,
                    candidate,
                    metadata=meta,
                    strength=strength,
                    candidate_activity_score=candidate_activity_score,
                    rank=rank,
                )
            except Exception:
                custom_key = None
            if custom_key is not None:
                return tuple(float(item) for item in custom_key)
        priority_fields = (
            "trigger_score",
            "regime_score",
            "directional_peer_score",
            "peer_score",
            "directional_vote_edge",
            "runner_quality_score",
            "execution_headroom_score",
            "source_quality_score",
            "selection_quality_score",
        )
        if any(field in meta for field in priority_fields):
            directional_peer_score = meta.get("directional_peer_score", meta.get("peer_score"))
            return (
                float(safe_float(meta.get("trigger_score"), 0.0) or 0.0),
                float(safe_float(meta.get("regime_score"), 0.0) or 0.0),
                float(safe_float(directional_peer_score, 0.0) or 0.0),
                float(safe_float(meta.get("directional_vote_edge"), 0.0) or 0.0),
                float(safe_float(meta.get("runner_quality_score"), 0.0) or 0.0),
                float(safe_float(meta.get("execution_headroom_score"), 0.0) or 0.0),
                float(safe_float(meta.get("source_quality_score"), 0.0) or 0.0),
                float(safe_float(meta.get("selection_quality_score"), priority_tiebreak if priority_tiebreak is not None else strength) or 0.0),
                strength,
                candidate_activity_score,
                -rank,
            )
        return strength, secondary, tertiary, -rank

    # ------------------------------------------------------------------
    # Broker entry recovery — used when entry submit returned an ambiguous
    # result. Queries broker to determine whether the order filled.
    # ------------------------------------------------------------------

    def _recover_equity_entry_from_broker(self, signal, result, entry_price: float) -> bool:
        if getattr(result, "ok", False):
            return False
        if not getattr(result, "order_id", None):
            return False
        if not order_result_needs_broker_recheck(getattr(result, "message", None)):
            return False
        row = self.broker_position_row(signal.symbol)
        side, qty, broker_avg_price = broker_position_side_qty(row)
        if side != signal.side or qty <= 0:
            return False
        recovered_entry = float(broker_avg_price if broker_avg_price is not None and broker_avg_price > 0 else entry_price)
        levels_ok, _ = self._entry_levels_valid(signal.side, recovered_entry, signal.stop_price, signal.target_price)
        if not levels_ok:
            recovered_entry = float(entry_price)
            levels_ok, _ = self._entry_levels_valid(signal.side, recovered_entry, signal.stop_price, signal.target_price)
            if not levels_ok:
                return False
        position_metadata = dict(signal.metadata or {})
        position_metadata.setdefault("initial_stop_price", float(signal.stop_price))
        position_metadata.setdefault("initial_target_price", safe_float(signal.target_price, None))
        position_metadata.setdefault("trail_armed", False)
        position_metadata["broker_reconciled_after_order_uncertainty"] = True
        position_metadata["broker_recovery_order_id"] = str(result.order_id)
        position_metadata["broker_recovery_message"] = str(result.message)
        position = Position(
            symbol=signal.symbol,
            strategy=signal.strategy,
            side=signal.side,
            qty=int(qty),
            entry_price=float(recovered_entry),
            entry_time=now_et(),
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            trail_pct=self.stock_position_trail_pct(position_metadata),
            highest_price=float(recovered_entry),
            lowest_price=float(recovered_entry),
            pair_id=signal.pair_id,
            reference_symbol=signal.reference_symbol,
            metadata=position_metadata,
        )
        self.position_manager.initialize_position_diagnostics(position, recovered_entry, self.position_manager.underlying_price_for_position(position, {}))
        self.positions[signal.symbol] = position
        self.account.record_entry(position, recovered_entry)
        self._save_reconcile_metadata()
        LOG.warning("Recovered equity entry state from broker for %s qty=%s after ambiguous order result=%s", signal.symbol, qty, result.message)
        return True

    @staticmethod
    def _option_single_entry_price_from_broker(row: dict[str, Any] | None) -> float | None:
        if not isinstance(row, dict):
            return None
        try:
            avg_price = row.get("averagePrice")
            if avg_price is None:
                return None
            avg = float(avg_price)
        except Exception:
            return None
        if avg <= 0:
            return None
        return float(avg * 100.0)

    @staticmethod
    def _option_vertical_entry_price_from_broker(metadata: dict[str, Any] | None, long_row: dict[str, Any] | None, short_row: dict[str, Any] | None) -> float | None:
        if not isinstance(metadata, dict) or not isinstance(long_row, dict) or not isinstance(short_row, dict):
            return None
        try:
            long_avg = float(long_row.get("averagePrice") or 0.0)
            short_avg = float(short_row.get("averagePrice") or 0.0)
        except Exception:
            return None
        if long_avg <= 0 or short_avg <= 0:
            return None
        spread_side = str(metadata.get("spread_side") or Side.LONG.value)
        if spread_side == Side.SHORT.value:
            net_points = short_avg - long_avg
        else:
            net_points = long_avg - short_avg
        if net_points <= 0:
            net_points = abs(short_avg - long_avg)
        if net_points <= 0:
            return None
        return float(net_points * 100.0)

    def _recover_option_entry_from_broker(self, signal, result) -> bool:
        if getattr(result, "ok", False):
            return False
        if not getattr(result, "order_id", None):
            return False
        if not order_result_needs_broker_recheck(getattr(result, "message", None)):
            return False
        meta = dict(signal.metadata or {})
        asset_type = str(meta.get("asset_type") or "").upper()
        position_key = str(meta.get("position_key") or signal.symbol)
        if asset_type == ASSET_TYPE_OPTION_SINGLE:
            option_symbol = str(meta.get("option_symbol") or "")
            row = self.broker_position_row(option_symbol)
            side, qty, _ = broker_position_side_qty(row)
            if side != signal.side or qty <= 0:
                return False
            recovered_entry = safe_float(getattr(result, "fill_price", None), None)
            if recovered_entry is None:
                recovered_entry = self._option_single_entry_price_from_broker(row)
            if recovered_entry is None or recovered_entry <= 0:
                recovered_entry = safe_float(meta.get("entry_price"), None)
            if recovered_entry is None or recovered_entry <= 0:
                return False
        elif asset_type == ASSET_TYPE_OPTION_VERTICAL:
            long_symbol = str(meta.get("long_leg_symbol") or "")
            short_symbol = str(meta.get("short_leg_symbol") or "")
            rows = self.broker_position_rows([long_symbol, short_symbol])
            long_row = rows.get(long_symbol.upper()) if long_symbol else None
            short_row = rows.get(short_symbol.upper()) if short_symbol else None
            long_side, long_qty, _ = broker_position_side_qty(long_row)
            short_side, short_qty, _ = broker_position_side_qty(short_row)
            if long_side != Side.LONG or short_side != Side.SHORT:
                return False
            if long_qty <= 0 or short_qty <= 0 or int(long_qty) != int(short_qty):
                return False
            qty = int(long_qty)
            recovered_entry = safe_float(getattr(result, "fill_price", None), None)
            if recovered_entry is None:
                recovered_entry = self._option_vertical_entry_price_from_broker(meta, long_row, short_row)
            if recovered_entry is None or recovered_entry <= 0:
                recovered_entry = safe_float(meta.get("entry_price"), None)
            if recovered_entry is None or recovered_entry <= 0:
                return False
        else:
            return False
        stop_price, target_price, position_metadata = self._materialize_option_position_levels(signal, float(recovered_entry))
        levels_ok, _ = self._entry_levels_valid(signal.side, float(recovered_entry), stop_price, target_price)
        if not levels_ok:
            fallback_entry = safe_float(meta.get("entry_price"), None)
            if fallback_entry is None or fallback_entry <= 0:
                return False
            recovered_entry = float(fallback_entry)
            stop_price, target_price, position_metadata = self._materialize_option_position_levels(signal, recovered_entry)
            levels_ok, _ = self._entry_levels_valid(signal.side, recovered_entry, stop_price, target_price)
            if not levels_ok:
                return False
        position_metadata["broker_reconciled_after_order_uncertainty"] = True
        position_metadata["broker_recovery_order_id"] = str(result.order_id)
        position_metadata["broker_recovery_message"] = str(result.message)
        position_metadata["qty"] = int(qty)
        position_metadata["entry_price"] = float(recovered_entry)
        position = Position(
            symbol=position_key,
            strategy=signal.strategy,
            side=signal.side,
            qty=int(qty),
            entry_price=float(recovered_entry),
            entry_time=now_et(),
            stop_price=float(stop_price),
            target_price=float(target_price) if target_price is not None else None,
            trail_pct=None,
            highest_price=float(recovered_entry),
            lowest_price=float(recovered_entry),
            pair_id=signal.pair_id,
            reference_symbol=signal.reference_symbol or meta.get("confirm_index"),
            metadata=position_metadata,
        )
        self.position_manager.initialize_position_diagnostics(position, float(recovered_entry), self.position_manager.underlying_price_for_position(position, {}))
        self.positions[position_key] = position
        self.account.record_entry(position, float(recovered_entry))
        self._save_reconcile_metadata()
        LOG.warning("Recovered option entry state from broker for %s qty=%s asset_type=%s after ambiguous order result=%s", position_key, qty, asset_type, result.message)
        return True

    # ------------------------------------------------------------------
    # Entry-decision logging + per-cycle summary.
    # ------------------------------------------------------------------

    @staticmethod
    def _decision_log_key(strategy_name: Any, symbol: str) -> str:
        return f"{strategy_name}:{symbol}"

    @staticmethod
    def _decision_reason_key(reason: Any) -> str:
        token = str(reason or '').strip()
        if not token:
            return 'none'
        if '.' in token:
            head, tail = token.split('.', 1)
            if head in {'long', 'short'}:
                token = tail
        if '(' in token:
            token = token.split('(', 1)[0]
        if ':' in token:
            token = token.split(':', 1)[0]
        return token or 'none'

    @staticmethod
    def _decision_context_marker(context: Mapping[str, Any] | None, details: Mapping[str, Any] | None = None) -> str | None:
        for source in (context, details):
            if not isinstance(source, Mapping):
                continue
            for key in ('bar_time', 'decision_bar_time', 'ltf_bar_time', 'signal_bar_time'):
                value = source.get(key)
                if value not in (None, ''):
                    return str(value)
        return None

    @staticmethod
    def _decision_side_preference(context: Mapping[str, Any] | None, details: Mapping[str, Any] | None = None) -> str | None:
        for source in (details, context):
            if not isinstance(source, Mapping):
                continue
            for key in ('candidate_directional_bias', 'side_preference', 'signal_side'):
                value = source.get(key)
                if value not in (None, ''):
                    return str(value)
        return None

    @staticmethod
    def _decision_entry_family(context: Mapping[str, Any] | None, details: Mapping[str, Any] | None = None) -> str | None:
        for source in (details, context):
            if not isinstance(source, Mapping):
                continue
            value = source.get('entry_family')
            if value not in (None, ''):
                return str(value)
        return None

    def _log_cycle_entry_summary(self, strategy_name: Any, decisions: Mapping[str, Any], *, candidate_count: int) -> None:
        if not decisions:
            return
        action_counts: dict[str, int] = {}
        blocker_counts: dict[str, int] = {}
        skipped_symbols: list[str] = []
        for symbol, payload in decisions.items():
            if not isinstance(payload, Mapping):
                continue
            action = str(payload.get('action') or 'unknown')
            action_counts[action] = action_counts.get(action, 0) + 1
            reasons = payload.get('reasons')
            if action == 'skipped':
                skipped_symbols.append(str(symbol))
                if isinstance(reasons, (list, tuple)):
                    seen: set[str] = set()
                    for reason in reasons:
                        key = self._decision_reason_key(reason)
                        if key in seen:
                            continue
                        seen.add(key)
                        blocker_counts[key] = blocker_counts.get(key, 0) + 1
        top_blockers = sorted(blocker_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
        summary = {
            'strategy': str(strategy_name),
            'candidate_count': int(candidate_count),
            'action_counts': action_counts,
            'signal_count': int(action_counts.get('signal', 0) + action_counts.get('entered', 0)),
            'skip_count': int(action_counts.get('skipped', 0)),
            'top_skip_reasons': {name: count for name, count in top_blockers},
            'skipped_symbols': skipped_symbols,
            'updated_at': now_et().isoformat(),
        }
        LOG.log(TRADEFLOW_LEVEL, 'Entry cycle summary strategy=%s candidates=%s actions=%s top_skips=%s', strategy_name, candidate_count, action_counts, summary['top_skip_reasons'] or 'none')
        self.audit.log_structured('ENTRY_CYCLE_SUMMARY', summary)

    def _log_entry_decision(
        self,
        strategy_name: Any,
        symbol: str,
        action: str,
        reasons: list[str] | tuple[str, ...] | None = None,
        *,
        force: bool = False,
        context: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        cleaned: list[str] = []
        for item in reasons or []:
            token = str(item or "").strip()
            if token and token not in cleaned:
                cleaned.append(token)
        # Tally session-wide skip reasons so session_report can surface a
        # filter-rejection summary at EOD. Each reason gets credit even
        # when multiple fire on the same decision.
        if str(action).lower() == "skipped":
            for reason in cleaned:
                self.session_skip_counts[reason] = self.session_skip_counts.get(reason, 0) + 1
        dashboard_symbol = str(symbol or '').upper().strip()
        context_payload = copy.deepcopy({str(k): v for k, v in context.items() if v is not None}) if isinstance(context, Mapping) else {}
        details_payload = copy.deepcopy({str(k): v for k, v in details.items() if v is not None}) if isinstance(details, Mapping) else {}
        if dashboard_symbol:
            payload: dict[str, Any] = {
                'symbol': dashboard_symbol,
                'strategy': str(strategy_name),
                'action': str(action),
                'reasons': list(cleaned),
                'primary_reason': cleaned[0] if cleaned else None,
                'secondary_reason': cleaned[1] if len(cleaned) > 1 else None,
                'updated_at': now_et().isoformat(),
            }
            if context_payload:
                payload['context'] = context_payload
            if details_payload:
                payload['details'] = details_payload
            self.last_entry_decisions[dashboard_symbol] = payload
        key = self._decision_log_key(strategy_name, symbol)
        now_ts = time.time()
        prior = self._last_entry_decision_log.get(key)
        marker = self._decision_context_marker(context_payload, details_payload)
        signature = (str(action), tuple(cleaned), marker)
        if not force and prior is not None:
            prev_action, prev_reasons, prev_ts, prev_marker = prior
            if prev_action == signature[0] and prev_reasons == signature[1] and prev_marker == signature[2] and (now_ts - prev_ts) < 120.0:
                return False
        primary_reason = cleaned[0] if cleaned else 'none'
        secondary_reason = cleaned[1] if len(cleaned) > 1 else None
        side_pref = self._decision_side_preference(context_payload, details_payload)
        entry_family = self._decision_entry_family(context_payload, details_payload)
        # Low-signal skip reasons log at DEBUG (and suppress SKIP_SUMMARY) to
        # keep the TRADEFLOW log focused on real decision events. The skips
        # are still counted in session_skip_counts and surface in the EOD
        # filter-rejection summary, so they're not hidden — just quieted.
        decision_log_level = TRADEFLOW_LEVEL
        if str(action).lower() == 'skipped' and primary_reason in _NOISY_SKIP_REASONS:
            decision_log_level = logging.DEBUG
        LOG.log(
            decision_log_level,
            'Decision symbol=%s strategy=%s action=%s primary=%s secondary=%s side_pref=%s family=%s reasons=%s',
            symbol,
            strategy_name,
            action,
            primary_reason,
            secondary_reason or 'none',
            side_pref or 'none',
            entry_family or 'none',
            ','.join(cleaned) if cleaned else 'none',
        )
        self._last_entry_decision_log[key] = (signature[0], signature[1], now_ts, signature[2])
        if str(action).lower() == 'skipped' and primary_reason not in _NOISY_SKIP_REASONS:
            payload = {'symbol': symbol, 'strategy': str(strategy_name), 'action': str(action), 'reasons': cleaned, **context_payload}
            if details_payload:
                payload['details'] = details_payload
            self.audit.log_structured('SKIP_SUMMARY', payload)
        return True

    # ------------------------------------------------------------------
    # Main entry point — runs every entry-actionable cycle.
    # ------------------------------------------------------------------

    def open_positions(self, candidates: list[Candidate], bars) -> None:
        candidate_symbols = [c.symbol for c in candidates]
        self.audit.log_cycle(
            f"entry_eval:{self.config.strategy}",
            ",".join(candidate_symbols),
            f"Entry cycle strategy={self.config.strategy} candidates={len(candidate_symbols)} symbols={','.join(candidate_symbols) if candidate_symbols else 'none'}",
            interval=30.0,
        )
        self.last_entry_decisions.clear()
        if not candidates:
            self.audit.log_cycle(
                f"entry_none:{self.config.strategy}",
                "no_candidates",
                f"Entry cycle strategy={self.config.strategy} action=skipped reasons=no_candidates",
                interval=30.0,
                level=TRADEFLOW_LEVEL,
            )
            return
        candidates_for_signals: list[Candidate] = []
        for candidate in candidates:
            candidate_context = self._candidate_snapshot(candidate, bars)
            if self._is_startup_reconcile_entry_blocked(candidate.symbol):
                self._log_entry_decision(self.config.strategy, candidate.symbol, "skipped", ["startup_reconcile_ignored_open_position"], context=candidate_context)
                continue
            if self.risk.is_symbol_on_cooldown(candidate.symbol):
                self._log_entry_decision(self.config.strategy, candidate.symbol, "skipped", ["cooldown"], context=candidate_context)
                continue
            live_entry_status = self.data.live_entry_bar_status(candidate.symbol)
            if bool(live_entry_status.get("requires_live_entry_bar")) and not bool(live_entry_status.get("ready", False)):
                blocker = str(live_entry_status.get("reason") or "live_1m_entry_not_ready")
                self._log_entry_decision(
                    self.config.strategy,
                    candidate.symbol,
                    "skipped",
                    [blocker],
                    context={**candidate_context, **{k: v for k, v in live_entry_status.items() if v is not None}},
                )
                continue
            candidates_for_signals.append(candidate)
        if not candidates_for_signals:
            return
        candidate_by_symbol = {c.symbol: c for c in candidates_for_signals}
        self.strategy.prefetch_entry_market_data(candidates_for_signals, bars, self.positions, data=self.data)
        signals = self.strategy.entry_signals(candidates_for_signals, bars, self.positions, client=self.client, data=self.data)
        signals = sorted(signals, key=lambda signal: self._signal_priority_key(signal, candidate_by_symbol.get(signal.symbol)), reverse=True)
        decision_map = self.strategy.pull_entry_decisions() if hasattr(self.strategy, "pull_entry_decisions") else {}
        finalized: set[str] = set()
        for signal in signals:
            finalized.add(signal.symbol)
            if self._is_startup_reconcile_entry_blocked(signal.symbol):
                self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, "startup_reconcile_ignored_open_position"], context={**self._candidate_snapshot(candidate_by_symbol.get(signal.symbol), bars), **self._signal_snapshot(signal, None, None)})
                continue
            allowed, reason = self.risk.can_open(signal, self.positions)
            if not allowed:
                LOG.log(TRADEFLOW_LEVEL, "Skipping %s %s: %s", signal.symbol, signal.reason, reason)
                self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, reason], context={**self._candidate_snapshot(candidate_by_symbol.get(signal.symbol), bars), **self._signal_snapshot(signal, None, None)})
                continue
            asset_type = signal.metadata.get("asset_type")
            if asset_type in OPTION_ASSET_TYPES and self._is_option_entry_retry_blocked(signal.symbol, signal.metadata):
                self._log_entry_decision(
                    signal.strategy,
                    signal.symbol,
                    "skipped",
                    [signal.reason, "entry_retry_backoff"],
                    context={**self._candidate_snapshot(candidate_by_symbol.get(signal.symbol), bars), **self._signal_snapshot(signal, None, None)},
                )
                continue
            if asset_type in OPTION_ASSET_TYPES:
                preview_entry_price = float(signal.metadata.get("entry_price") or 0.0)
                levels_ok, levels_reason = self._entry_levels_valid(signal.side, preview_entry_price, signal.stop_price, signal.target_price)
                if not levels_ok:
                    self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, levels_reason or "invalid_levels"], context={**self._candidate_snapshot(candidate_by_symbol.get(signal.symbol), bars), **self._signal_snapshot(signal, None, preview_entry_price)})
                    continue
                max_loss = float(signal.metadata.get("max_loss_per_contract") or 0.0)
                qty = self.risk.size_option_position(max_loss)
                if qty <= 0:
                    LOG.log(TRADEFLOW_LEVEL, "Skipping %s, option qty <= 0", signal.symbol)
                    self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, "option_qty_zero"], context={**self._candidate_snapshot(candidate_by_symbol.get(signal.symbol), bars), **self._signal_snapshot(signal, qty, preview_entry_price), **self._risk_snapshot(signal, preview_entry_price, qty)})
                    continue
                raw_spec = self._scaled_order_spec(signal.metadata["order_spec"], qty)
                if asset_type == ASSET_TYPE_OPTION_VERTICAL:
                    result = self.executor.submit_option_vertical(raw_spec, {**signal.metadata, "qty": qty}, data=self.data)
                else:
                    result = self.executor.submit_option_single(raw_spec, {**signal.metadata, "qty": qty}, data=self.data)
                filled_qty = int(result.filled_qty or 0) if result.ok else 0
                if result.ok and filled_qty <= 0:
                    LOG.warning("Option entry %s ok=True but filled_qty=%s — treating as unfilled", signal.metadata.get("position_key", signal.symbol), result.filled_qty)
                    self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, "filled_qty_zero"])
                    continue
                qty_for_position = filled_qty if filled_qty > 0 else qty
                if result.ok and filled_qty > 0 and filled_qty != qty:
                    LOG.info("Option entry %s requested_qty=%s filled_qty=%s result=%s", signal.metadata.get("position_key", signal.symbol), qty, filled_qty, result.message)
                else:
                    log_label = "Option entry" if result.ok else "Option entry attempt"
                    LOG.info("%s %s qty=%s result=%s", log_label, signal.metadata.get("position_key", signal.symbol), qty_for_position, result.message)
                entry_context_payload = self._entry_context_payload(signal, candidate_by_symbol.get(signal.symbol), bars, qty_for_position, preview_entry_price, result.message, result)
                self.audit.log_structured("ENTRY_CONTEXT", entry_context_payload)
                if not result.ok:
                    if self._recover_option_entry_from_broker(signal, result):
                        self._clear_option_entry_retry_backoff(signal.symbol, signal.metadata)
                        self._log_entry_decision(signal.strategy, signal.symbol, "entered", [signal.reason, "broker_recovered_after_order_uncertainty"])
                        continue
                    if self.config.schwab.dry_run and str(result.message).startswith("dry_run_not_filled_"):
                        self._register_option_entry_retry_backoff(signal.symbol, signal.metadata)
                    self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, f"order_failed:{result.message}"], context={**self._candidate_snapshot(candidate_by_symbol.get(signal.symbol), bars), **self._signal_snapshot(signal, qty_for_position, preview_entry_price, result.message), **self._risk_snapshot(signal, preview_entry_price, qty_for_position)})
                    continue
                self._clear_option_entry_retry_backoff(signal.symbol, signal.metadata)
                entry_price = float(result.fill_price if result.fill_price is not None else signal.metadata.get("entry_price") or 0.0)
                stop_price, target_price, position_metadata = self._materialize_option_position_levels(signal, entry_price)
                levels_ok, levels_reason = self._entry_levels_valid(signal.side, entry_price, stop_price, target_price)
                if not levels_ok:
                    # Order is already filled at broker — we MUST track the position.
                    # Use emergency fallback levels rather than orphaning it.
                    LOG.warning(
                        "Post-fill level validation failed for %s (reason=%s entry=%.4f stop=%.4f target=%s); "
                        "applying emergency fallback levels to avoid orphaned broker position.",
                        signal.metadata.get("position_key", signal.symbol),
                        levels_reason,
                        entry_price,
                        stop_price,
                        target_price,
                    )
                    if signal.side == Side.LONG:
                        stop_price = max(0.01, entry_price * 0.50)  # 50% emergency stop
                        target_price = entry_price * 2.0
                    else:
                        stop_price = entry_price * 2.0
                        target_price = max(0.01, entry_price * 0.50)
                    position_metadata["emergency_fallback_levels"] = True
                    position_metadata["original_levels_reason"] = levels_reason
                position_key = str(signal.metadata.get("position_key") or signal.symbol)
                position = Position(
                    symbol=position_key,
                    strategy=signal.strategy,
                    side=signal.side,
                    qty=qty_for_position,
                    entry_price=entry_price,
                    entry_time=now_et(),
                    stop_price=stop_price,
                    target_price=target_price,
                    trail_pct=None,
                    highest_price=entry_price,
                    lowest_price=entry_price,
                    pair_id=signal.pair_id,
                    reference_symbol=signal.reference_symbol or signal.metadata.get("confirm_index"),
                    metadata={**position_metadata, "qty": qty_for_position, "entry_price": entry_price},
                )
                self.position_manager.initialize_position_diagnostics(position, entry_price, self.position_manager.underlying_price_for_position(position, bars))
                self.positions[position_key] = position
                self.account.record_entry(position, entry_price)
                self._save_reconcile_metadata()
                self._log_entry_decision(signal.strategy, signal.symbol, "entered", [signal.reason])
                continue

            frame = bars.get(signal.symbol)
            if frame is None or frame.empty:
                self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, "entry_frame_unavailable"], context={**self._candidate_snapshot(candidate_by_symbol.get(signal.symbol), bars), **self._signal_snapshot(signal, None, None)})
                continue
            intent = self.executor.order_intent_for_entry(signal.side)
            preview = self.executor.preview_equity_entry(signal.symbol, intent, data=self.data)
            if preview is None:
                self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, "entry_quote_unavailable"], context={**self._candidate_snapshot(candidate_by_symbol.get(signal.symbol), bars), **self._signal_snapshot(signal, None, None)})
                continue
            entry_price = float(preview["limit_price"])
            levels_ok, levels_reason = self._entry_levels_valid(signal.side, entry_price, signal.stop_price, signal.target_price)
            if not levels_ok:
                self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, levels_reason or "invalid_levels"], context={**self._candidate_snapshot(candidate_by_symbol.get(signal.symbol), bars), **self._signal_snapshot(signal, None, entry_price, market_snapshot=preview.get('market_snapshot') if isinstance(preview, dict) else None, order_intent=intent)})
                continue
            qty = self.risk.size_position(entry_price, signal.stop_price)
            remaining_notional = self.risk.remaining_stock_notional_capacity(self.positions)
            if entry_price > 0:
                qty = min(qty, self.risk.floor_discrete_units(remaining_notional, entry_price))
            if qty <= 0:
                LOG.log(TRADEFLOW_LEVEL, "Skipping %s, qty <= 0 or max_total_notional reached", signal.symbol)
                self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, "qty_zero_or_notional_limit"], context={**self._candidate_snapshot(candidate_by_symbol.get(signal.symbol), bars), **self._signal_snapshot(signal, qty, entry_price, market_snapshot=preview.get('market_snapshot') if isinstance(preview, dict) else None, order_intent=intent), **self._risk_snapshot(signal, entry_price, qty)})
                continue
            proposed_notional = entry_price * qty
            allowed, reason = self.risk.can_add_stock_notional(self.positions, proposed_notional)
            if not allowed:
                LOG.log(TRADEFLOW_LEVEL, "Skipping %s %s: %s", signal.symbol, signal.reason, reason)
                self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, reason], context={**self._candidate_snapshot(candidate_by_symbol.get(signal.symbol), bars), **self._signal_snapshot(signal, None, entry_price, market_snapshot=preview.get('market_snapshot') if isinstance(preview, dict) else None, order_intent=intent)})
                continue
            result = self.executor.submit_equity_entry(signal.symbol, qty, intent, data=self.data, market_snapshot=preview.get("market_snapshot") if isinstance(preview, dict) else None)
            filled_qty = int(result.filled_qty or 0) if result.ok else 0
            if result.ok and filled_qty <= 0:
                LOG.warning("Entry %s ok=True but filled_qty=%s — treating as unfilled", signal.symbol, result.filled_qty)
                self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, "filled_qty_zero"])
                continue
            qty_for_position = filled_qty if filled_qty > 0 else qty
            if result.ok and filled_qty > 0 and filled_qty != qty:
                LOG.log(TRADEFLOW_LEVEL, "Entry %s %s requested_qty=%s filled_qty=%s result=%s", signal.symbol, signal.side.value, qty, filled_qty, result.message)
            else:
                LOG.log(TRADEFLOW_LEVEL, "Entry %s %s qty=%s result=%s", signal.symbol, signal.side.value, qty_for_position, result.message)
            entry_context_payload = self._entry_context_payload(signal, candidate_by_symbol.get(signal.symbol), bars, qty_for_position, entry_price, result.message, result, market_snapshot=preview.get('market_snapshot') if isinstance(preview, dict) else None, order_intent=intent)
            self.audit.log_structured("ENTRY_CONTEXT", entry_context_payload)
            if not result.ok:
                if self._recover_equity_entry_from_broker(signal, result, entry_price):
                    self._log_entry_decision(signal.strategy, signal.symbol, "entered", [signal.reason, "broker_recovered_after_order_uncertainty"])
                    continue
                self._log_entry_decision(signal.strategy, signal.symbol, "skipped", [signal.reason, f"order_failed:{result.message}"], context={**self._candidate_snapshot(candidate_by_symbol.get(signal.symbol), bars), **self._signal_snapshot(signal, qty_for_position, entry_price, result_message=result.message, market_snapshot=preview.get('market_snapshot') if isinstance(preview, dict) else None, order_intent=intent), **self._risk_snapshot(signal, entry_price, qty_for_position)})
                continue
            signal_entry_price = float(entry_price)  # pre-fill intended price
            entry_price = float(result.fill_price if result.fill_price is not None else entry_price)
            position_metadata = dict(signal.metadata or {})
            position_metadata.setdefault("initial_stop_price", float(signal.stop_price))
            position_metadata.setdefault("initial_target_price", safe_float(signal.target_price, None))
            position_metadata.setdefault("trail_armed", False)
            # Slippage tracking: signal price vs actual fill
            position_metadata["signal_entry_price"] = signal_entry_price
            position_metadata["entry_slippage"] = round(abs(entry_price - signal_entry_price), 6)
            position_metadata["entry_slippage_pct"] = round(abs(entry_price - signal_entry_price) / max(signal_entry_price, 0.01), 6)
            position = Position(
                symbol=signal.symbol,
                strategy=signal.strategy,
                side=signal.side,
                qty=qty_for_position,
                entry_price=entry_price,
                entry_time=now_et(),
                stop_price=signal.stop_price,
                target_price=signal.target_price,
                trail_pct=self.stock_position_trail_pct(position_metadata),
                highest_price=entry_price,
                lowest_price=entry_price,
                pair_id=signal.pair_id,
                reference_symbol=signal.reference_symbol,
                metadata=position_metadata,
            )
            self.position_manager.initialize_position_diagnostics(position, entry_price, self.position_manager.underlying_price_for_position(position, bars))
            self.positions[signal.symbol] = position
            self.account.record_entry(position, entry_price)
            self._save_reconcile_metadata()
            self._log_entry_decision(signal.strategy, signal.symbol, "entered", [signal.reason])

        for symbol, payload in decision_map.items():
            if symbol in finalized:
                continue
            extra_context = payload.get("context") if isinstance(payload, Mapping) else None
            extra_details = payload.get("details") if isinstance(payload, Mapping) else None
            merged_context = {
                **self._candidate_snapshot(candidate_by_symbol.get(symbol), bars),
                **({str(k): v for k, v in extra_context.items() if v is not None} if isinstance(extra_context, Mapping) else {}),
            }
            self._log_entry_decision(
                self.config.strategy,
                symbol,
                str(payload.get("action") or "skipped"),
                list(payload.get("reasons") or []),
                context=merged_context,
                details={str(k): v for k, v in extra_details.items() if v is not None} if isinstance(extra_details, Mapping) else None,
            )
        self._log_cycle_entry_summary(self.config.strategy, self.last_entry_decisions, candidate_count=len(candidates_for_signals))
