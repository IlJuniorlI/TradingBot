# SPDX-License-Identifier: MIT
"""PositionManager — owns the open-position management cycle.

Extracted from ``IntradayBot`` as Phase 5 Step 9 of the Phase 5 engine split.
Holds the logic that evaluates open positions each cycle: mark-price
resolution, diagnostic tracking, sr-flip / adaptive-ladder management,
exit-signal evaluation, exit execution, and broker exit recovery.

Design notes:

- ``self.positions`` is a **shared-reference** dict with the owning
  ``IntradayBot``. Mutations here (pop on final exit, qty decrement on
  partial) are immediately visible to the engine's other methods.
- ``save_reconcile_metadata`` is injected as a callable because the engine
  also needs to call it from entry paths (``_open_positions``, broker
  entry recovery). Keeping a single source of truth on the engine avoids
  diverging metadata-signature caches between the two owners.
- ``_broker_position_row`` / ``_broker_position_rows`` live on engine for
  now — they depend on ``self.client`` / ``self.executor.account_hash``
  for live broker queries, and are also used by entry recovery. They're
  injected as callables so exit recovery can reach them without pulling
  the whole broker surface into PositionManager.
- Config accessors ``_active_sr_*`` moved here (they read ``self.config``
  and ``self.strategy``). Engine call sites in entry / screening paths
  dispatch through ``self.position_manager._active_sr_*()`` so the
  accessors have a single home.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime
from typing import Any, Callable, Mapping

import pandas as pd

from .audit_logger import AuditLogger
from .config import BotConfig
from .dashboard_cache import DashboardCache
from .data_feed import MarketDataStore
from .execution import SchwabExecutor
from .models import (
    ASSET_TYPE_EQUITY,
    ASSET_TYPE_OPTION_SINGLE,
    ASSET_TYPE_OPTION_VERTICAL,
    OPTION_ASSET_TYPES,
    Position,
    Side,
)
from .paper_account import PaperAccount
from .position_metrics import (
    exit_reason_details,
    position_return_pct_at_price,
    position_unrealized_at_price,
    safe_float,
)
from .risk import RiskManager
from ._sr_ladder import _select_next_distinct_level, _sr_effective_side_tolerance
from ._strategies.strategy_base import BaseStrategy
from .broker_positions import broker_position_side_qty, order_result_needs_broker_recheck
from .support_resistance import zone_flip_confirmed
from .utils import TRADEFLOW_LEVEL, append_management_adjustment as _append_adjustment, now_et

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


class PositionManager:
    def __init__(
        self,
        config: BotConfig,
        *,
        data: MarketDataStore,
        executor: SchwabExecutor,
        risk: RiskManager,
        audit: AuditLogger,
        account: PaperAccount,
        strategy: BaseStrategy,
        dashboard_cache: DashboardCache,
        positions: dict[str, Position],
        save_reconcile_metadata: Callable[[], None],
        broker_position_row: Callable[[str], dict[str, Any] | None],
        broker_position_rows: Callable[[list[str]], dict[str, dict[str, Any]]],
        structured_metadata_snapshot: Callable[[Mapping[str, Any] | None], dict[str, Any]],
    ) -> None:
        self.config = config
        self.data = data
        self.executor = executor
        self.risk = risk
        self.audit = audit
        self.account = account
        self.strategy = strategy
        self.dashboard_cache = dashboard_cache
        self.positions = positions
        self._save_reconcile_metadata = save_reconcile_metadata
        self._broker_position_row = broker_position_row
        self._broker_position_rows = broker_position_rows
        self._structured_metadata_snapshot = structured_metadata_snapshot

    # ------------------------------------------------------------------
    # SR-config accessors (read config + strategy params).
    # ------------------------------------------------------------------

    def active_sr_timeframe_minutes(self) -> int:
        cfg = getattr(self.config, "support_resistance", None)
        fallback = int(getattr(cfg, "timeframe_minutes", 15)) if cfg is not None else 15
        params = getattr(getattr(self, "strategy", None), "params", {}) or {}
        return int(params.get("htf_timeframe_minutes", fallback))

    def active_sr_lookback_days(self) -> int:
        cfg = getattr(self.config, "support_resistance", None)
        fallback = int(getattr(cfg, "lookback_days", 10)) if cfg is not None else 10
        params = getattr(getattr(self, "strategy", None), "params", {}) or {}
        return int(params.get("htf_lookback_days", fallback))

    def active_sr_refresh_seconds(self) -> int:
        cfg = getattr(self.config, "support_resistance", None)
        fallback = int(getattr(cfg, "refresh_seconds", 600)) if cfg is not None else 600
        params = getattr(getattr(self, "strategy", None), "params", {}) or {}
        return int(params.get("htf_refresh_seconds", fallback))

    # ------------------------------------------------------------------
    # Mark-price resolution for open positions.
    # ------------------------------------------------------------------

    def _position_management_snapshot(self, position: Position, bars) -> tuple[float | None, dict[str, Any] | None]:
        mark = self.strategy.position_mark_price(position, self.data)
        if mark is not None:
            price = float(mark)
            return price, None
        asset_type = str(position.metadata.get("asset_type") or ASSET_TYPE_EQUITY)
        if asset_type in OPTION_ASSET_TYPES:
            return self._option_position_management_snapshot(position)
        if self.data is not None:
            max_age = max(1.0, float(self.config.runtime.quote_cache_seconds))
            quote = self.data.get_quote(position.symbol) or {}
            if quote and not self.data.quotes_are_fresh([position.symbol], max_age):
                try:
                    self.data.fetch_quotes([position.symbol], force=True, source="engine:position_management_snapshot")
                except Exception:
                    LOG.debug("Forced quote refresh failed during position snapshot for %s; using cached quote.", position.symbol, exc_info=True)
                quote = self.data.get_quote(position.symbol) or quote
            if quote and self.data.quotes_are_fresh([position.symbol], max_age):
                # Use mark/last as primary price for stop/target evaluation.
                # Bid (for LONG) and ask (for SHORT) can cause false stop triggers
                # during wide spreads — the actual traded price may be far from the
                # bid/ask extremes.
                keys = ("mark", "markPrice", "last", "lastPrice", "close", "closePrice")
                price = None
                for key in keys:
                    value = quote.get(key)
                    try:
                        if value is not None and float(value) > 0:
                            price = float(value)
                            break
                    except Exception:
                        continue
                if price is not None:
                    market_snapshot = {
                        "bid": safe_float(quote.get("bid"), None),
                        "ask": safe_float(quote.get("ask"), None),
                        "last": safe_float(quote.get("last") or quote.get("mark") or quote.get("close"), None),
                        "source": "quote",
                        "decision_price": price,
                    }
                    return price, market_snapshot
        frame = bars.get(position.symbol)
        if frame is not None and not frame.empty:
            price = float(frame.iloc[-1].close)
            return price, {"bid": price, "ask": price, "last": price, "source": "bar_close", "decision_price": price}
        underlying = position.metadata.get("underlying")
        if underlying:
            frame = bars.get(str(underlying))
            if frame is not None and not frame.empty:
                price = float(frame.iloc[-1].close)
                return price, {"bid": price, "ask": price, "last": price, "source": "underlying_bar_close", "decision_price": price}
        cached = self.account.last_prices.get(position.symbol)
        if cached is not None:
            price = float(cached)
            return price, {"bid": price, "ask": price, "last": price, "source": "account_cache", "decision_price": price}
        return None, None

    def _option_position_management_snapshot(self, position: Position) -> tuple[float | None, dict[str, Any] | None]:
        """Fetch fresh quotes for option legs and compute a mark price for position management."""
        if self.data is None:
            return None, None
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        asset_type = str(meta.get("asset_type") or "")
        max_age = float(self.config.options.max_quote_age_seconds)
        if asset_type == ASSET_TYPE_OPTION_VERTICAL:
            long_symbol = str(meta.get("long_leg_symbol") or "")
            short_symbol = str(meta.get("short_leg_symbol") or "")
            if not long_symbol or not short_symbol:
                return None, None
            try:
                self.data.fetch_quotes([long_symbol, short_symbol], force=True, min_force_interval_seconds=1.0, source="engine:option_position_management")
            except Exception:
                LOG.debug("Option position management quote refresh failed for %s; using cached.", position.symbol, exc_info=True)
            q1 = self.data.get_quote(long_symbol)
            q2 = self.data.get_quote(short_symbol)
            if not q1 or not q2 or not self.data.quotes_are_fresh([long_symbol, short_symbol], max_age):
                cached = self.account.last_prices.get(position.symbol)
                if cached is not None:
                    price = float(cached)
                    return price, {"bid": price, "ask": price, "last": price, "source": "option_account_cache", "decision_price": price}
                return None, None
            from .options_mode import contract_from_quote, vertical_price_bounds
            spread_side = str(meta.get("spread_side") or Side.LONG.value)
            if spread_side == Side.SHORT.value:
                first_sym, second_sym = short_symbol, long_symbol
                first_meta, second_meta = meta.get("short_leg"), meta.get("long_leg")
            else:
                first_sym, second_sym = long_symbol, short_symbol
                first_meta, second_meta = meta.get("long_leg"), meta.get("short_leg")
            first_leg = contract_from_quote(first_sym, q1 if first_sym == long_symbol else q2, first_meta)
            second_leg = contract_from_quote(second_sym, q2 if second_sym == short_symbol else q1, second_meta)
            bid, ask, mid = vertical_price_bounds(first_leg, second_leg)
            mark = mid if mid > 0 else (bid if bid > 0 else ask)
            if mark <= 0:
                return None, None
            price = float(mark * 100.0)
            return price, {"bid": bid * 100.0, "ask": ask * 100.0, "last": price, "source": "option_vertical_quotes", "decision_price": price}
        elif asset_type == ASSET_TYPE_OPTION_SINGLE:
            option_symbol = str(meta.get("option_symbol") or "")
            if not option_symbol:
                return None, None
            try:
                self.data.fetch_quotes([option_symbol], force=True, min_force_interval_seconds=1.0, source="engine:option_position_management")
            except Exception:
                LOG.debug("Option position management quote refresh failed for %s; using cached.", position.symbol, exc_info=True)
            q = self.data.get_quote(option_symbol)
            if not q or not self.data.quotes_are_fresh([option_symbol], max_age):
                cached = self.account.last_prices.get(position.symbol)
                if cached is not None:
                    price = float(cached)
                    return price, {"bid": price, "ask": price, "last": price, "source": "option_account_cache", "decision_price": price}
                return None, None
            from .options_mode import contract_from_quote, single_option_price_bounds
            contract = contract_from_quote(option_symbol, q, meta.get("option_leg"))
            bid, ask, mid = single_option_price_bounds(contract)
            mark = mid if mid > 0 else (bid if bid > 0 else ask)
            if mark <= 0:
                return None, None
            price = float(mark * 100.0)
            return price, {"bid": bid * 100.0, "ask": ask * 100.0, "last": price, "source": "option_single_quotes", "decision_price": price}
        return None, None

    # ------------------------------------------------------------------
    # Position diagnostics (best/worst unrealized tracking).
    # ------------------------------------------------------------------

    @staticmethod
    def initialize_position_diagnostics(position: Position, mark_price: float, underlying_price: float | None = None) -> None:
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        ts = now_et().isoformat()
        meta['diag_best_unrealized_pnl'] = 0.0
        meta['diag_worst_unrealized_pnl'] = 0.0
        meta['diag_best_unrealized_ts'] = ts
        meta['diag_worst_unrealized_ts'] = ts
        meta['diag_best_mark_price'] = float(mark_price)
        meta['diag_worst_mark_price'] = float(mark_price)
        if underlying_price is not None:
            meta['diag_best_underlying_price'] = float(underlying_price)
            meta['diag_worst_underlying_price'] = float(underlying_price)

    @staticmethod
    def _update_position_diagnostics(position: Position, mark_price: float, underlying_price: float | None = None) -> None:
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        if position.side.value == 'LONG':
            unrealized = (float(mark_price) - float(position.entry_price)) * int(position.qty)
        else:
            unrealized = (float(position.entry_price) - float(mark_price)) * int(position.qty)
        best = float(meta.get('diag_best_unrealized_pnl', 0.0))
        worst = float(meta.get('diag_worst_unrealized_pnl', 0.0))
        ts = now_et().isoformat()
        if unrealized > best:
            meta['diag_best_unrealized_pnl'] = unrealized
            meta['diag_best_unrealized_ts'] = ts
            meta['diag_best_mark_price'] = float(mark_price)
            if underlying_price is not None:
                meta['diag_best_underlying_price'] = float(underlying_price)
        if unrealized < worst:
            meta['diag_worst_unrealized_pnl'] = unrealized
            meta['diag_worst_unrealized_ts'] = ts
            meta['diag_worst_mark_price'] = float(mark_price)
            if underlying_price is not None:
                meta['diag_worst_underlying_price'] = float(underlying_price)

    @staticmethod
    def underlying_price_for_position(position: Position, bars, default: float | None = None) -> float | None:
        underlying = str(position.metadata.get('underlying') or position.symbol)
        frame = bars.get(underlying) if bars else None
        if frame is not None and not frame.empty:
            try:
                return float(frame.iloc[-1].close)
            except Exception:
                return default
        return default

    # ------------------------------------------------------------------
    # Exit-time structured payloads.
    # ------------------------------------------------------------------

    @staticmethod
    def _trade_summary_payload(
        position: Position,
        exit_price: float,
        realized: float,
        reason: str,
        *,
        final_exit: bool = True,
        remaining_qty_after_exit: int = 0,
        broker_recovered: bool = False,
        fill_price_estimated: bool = False,
    ) -> dict[str, Any]:
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        return {
            'symbol': position.symbol,
            'strategy': position.strategy,
            'side': position.side.value,
            'qty': int(position.qty),
            'entry_time': position.entry_time.isoformat(),
            'exit_time': now_et().isoformat(),
            'entry_price': float(position.entry_price),
            'exit_price': float(exit_price),
            'realized_pnl': float(realized),
            'exit_reason': reason,
            'partial_exit': not bool(final_exit),
            'final_exit': bool(final_exit),
            'remaining_qty_after_exit': max(0, int(remaining_qty_after_exit)),
            'broker_recovered': bool(broker_recovered),
            'fill_price_estimated': bool(fill_price_estimated),
            'best_unrealized_pnl': safe_float(meta.get('diag_best_unrealized_pnl'), None),
            'worst_unrealized_pnl': safe_float(meta.get('diag_worst_unrealized_pnl'), None),
            'best_unrealized_ts': meta.get('diag_best_unrealized_ts'),
            'worst_unrealized_ts': meta.get('diag_worst_unrealized_ts'),
            'best_mark_price': safe_float(meta.get('diag_best_mark_price'), None),
            'worst_mark_price': safe_float(meta.get('diag_worst_mark_price'), None),
            'best_underlying_price': safe_float(meta.get('diag_best_underlying_price'), None),
            'worst_underlying_price': safe_float(meta.get('diag_worst_underlying_price'), None),
            'asset_type': meta.get('asset_type'),
            'style': meta.get('style'),
            'direction': meta.get('direction'),
            'regime': meta.get('regime'),
        }

    @staticmethod
    def _exit_bar_snapshot(frame) -> dict[str, Any]:
        if frame is None or frame.empty:
            return {}
        try:
            last = frame.iloc[-1]
        except Exception:
            return {}
        high = safe_float(last.get('high'), None) if hasattr(last, 'get') else safe_float(last['high'], None) if 'high' in frame.columns else None
        low = safe_float(last.get('low'), None) if hasattr(last, 'get') else safe_float(last['low'], None) if 'low' in frame.columns else None
        close = safe_float(last.get('close'), None) if hasattr(last, 'get') else safe_float(last['close'], None) if 'close' in frame.columns else None
        close_position_pct = None
        if high is not None and low is not None and close is not None and high > low:
            close_position_pct = (close - low) / (high - low)
        idx = frame.index[-1] if len(frame.index) else None
        return {
            'bar_ts': idx.isoformat() if hasattr(idx, 'isoformat') else (str(idx) if idx is not None else None),
            'bar_open': safe_float(last.get('open'), None) if hasattr(last, 'get') else safe_float(last['open'], None) if 'open' in frame.columns else None,
            'bar_high': high,
            'bar_low': low,
            'bar_close': close,
            'bar_volume': safe_float(last.get('volume'), None) if hasattr(last, 'get') else safe_float(last['volume'], None) if 'volume' in frame.columns else None,
            'bar_ret5': safe_float(last.get('ret5'), None) if hasattr(last, 'get') else safe_float(last['ret5'], None) if 'ret5' in frame.columns else None,
            'bar_ret15': safe_float(last.get('ret15'), None) if hasattr(last, 'get') else safe_float(last['ret15'], None) if 'ret15' in frame.columns else None,
            'ema9': safe_float(last.get('ema9'), None) if hasattr(last, 'get') else safe_float(last['ema9'], None) if 'ema9' in frame.columns else None,
            'ema20': safe_float(last.get('ema20'), None) if hasattr(last, 'get') else safe_float(last['ema20'], None) if 'ema20' in frame.columns else None,
            'vwap': safe_float(last.get('vwap'), None) if hasattr(last, 'get') else safe_float(last['vwap'], None) if 'vwap' in frame.columns else None,
            'atr14': safe_float(last.get('atr14'), None) if hasattr(last, 'get') else safe_float(last['atr14'], None) if 'atr14' in frame.columns else None,
            'close_position_pct': close_position_pct,
        }

    def _position_exit_context(self, position: Position, reason: str, mark_price: float | None, underlying_price: float | None, market_snapshot: dict[str, Any] | None, bars) -> dict[str, Any]:
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        entry_price = safe_float(position.entry_price, None)
        stop_price = safe_float(position.stop_price, None)
        target_price = safe_float(position.target_price, None)
        initial_stop_price = safe_float(meta.get('initial_stop_price'), stop_price)
        initial_target_price = safe_float(meta.get('initial_target_price'), target_price)
        current_price = safe_float(mark_price, None)
        current_unrealized = position_unrealized_at_price(position, current_price)
        return_pct = position_return_pct_at_price(position, current_price)
        hold_minutes = max(0.0, (now_et() - position.entry_time).total_seconds() / 60.0)
        stop_distance = None
        target_distance = None
        if current_price is not None and stop_price is not None:
            if position.side == Side.LONG:
                stop_distance = current_price - stop_price
            else:
                stop_distance = stop_price - current_price
        if current_price is not None and target_price is not None:
            if position.side == Side.LONG:
                target_distance = target_price - current_price
            else:
                target_distance = current_price - target_price
        initial_risk_per_unit = None
        if entry_price is not None and initial_stop_price is not None:
            initial_risk_per_unit = abs(entry_price - initial_stop_price)
        initial_reward_per_unit = None
        if entry_price is not None and initial_target_price is not None:
            initial_reward_per_unit = abs(initial_target_price - entry_price)
        initial_rr = None
        if initial_risk_per_unit not in (None, 0.0) and initial_reward_per_unit is not None:
            initial_rr = initial_reward_per_unit / initial_risk_per_unit
        management_symbol = str(meta.get('underlying') or position.symbol)
        management_frame = bars.get(management_symbol) if bars else None
        sr_row = None
        if self.data is not None and management_symbol:
            try:
                sr_row = self.dashboard_cache.sr_row(management_symbol, price=underlying_price or current_price, allow_refresh=False)
            except Exception:
                sr_row = None
        payload = {
            'position_symbol': position.symbol,
            'management_symbol': management_symbol,
            'strategy': position.strategy,
            'side': position.side.value,
            'position_qty_before_exit': int(position.qty),
            'asset_type': meta.get('asset_type'),
            'style': meta.get('style'),
            'direction': meta.get('direction'),
            'regime': meta.get('regime'),
            'final_priority_score': safe_float(meta.get('final_priority_score'), None),
            'selection_quality_score': safe_float(meta.get('selection_quality_score'), None),
            'activity_score': safe_float(meta.get('activity_score'), None),
            'setup_quality_score': safe_float(meta.get('setup_quality_score'), None),
            'execution_quality_score': safe_float(meta.get('execution_quality_score'), None),
            'reference_symbol': position.reference_symbol,
            'pair_id': position.pair_id,
            'entry_time': position.entry_time.isoformat(),
            'hold_minutes': hold_minutes,
            'entry_price': entry_price,
            'mark_price': current_price,
            'underlying_price': safe_float(underlying_price, None),
            'unrealized_pnl_at_mark': current_unrealized,
            'return_pct_at_mark': return_pct,
            'stop_price': stop_price,
            'target_price': target_price,
            'initial_stop_price': initial_stop_price,
            'initial_target_price': initial_target_price,
            'distance_to_stop': stop_distance,
            'distance_to_target': target_distance,
            'initial_risk_per_unit': initial_risk_per_unit,
            'initial_reward_per_unit': initial_reward_per_unit,
            'initial_rr': initial_rr,
            'trail_pct': safe_float(position.trail_pct, None),
            'trail_armed': bool(meta.get('trail_armed')) if meta.get('trail_armed') is not None else None,
            'trail_activation_price': safe_float(meta.get('trail_activation_price'), None),
            'highest_price': safe_float(position.highest_price, None),
            'lowest_price': safe_float(position.lowest_price, None),
            'best_unrealized_pnl': safe_float(meta.get('diag_best_unrealized_pnl'), None),
            'worst_unrealized_pnl': safe_float(meta.get('diag_worst_unrealized_pnl'), None),
            'best_unrealized_ts': meta.get('diag_best_unrealized_ts'),
            'worst_unrealized_ts': meta.get('diag_worst_unrealized_ts'),
            'best_mark_price': safe_float(meta.get('diag_best_mark_price'), None),
            'worst_mark_price': safe_float(meta.get('diag_worst_mark_price'), None),
            'best_underlying_price': safe_float(meta.get('diag_best_underlying_price'), None),
            'worst_underlying_price': safe_float(meta.get('diag_worst_underlying_price'), None),
            'decision_source': (market_snapshot or {}).get('source') if isinstance(market_snapshot, dict) else None,
            'decision_bid': safe_float((market_snapshot or {}).get('bid'), None) if isinstance(market_snapshot, dict) else None,
            'decision_ask': safe_float((market_snapshot or {}).get('ask'), None) if isinstance(market_snapshot, dict) else None,
            'decision_last': safe_float((market_snapshot or {}).get('last'), None) if isinstance(market_snapshot, dict) else None,
            'decision_price': safe_float((market_snapshot or {}).get('decision_price'), None) if isinstance(market_snapshot, dict) else None,
            **exit_reason_details(reason),
            **self._exit_bar_snapshot(management_frame),
        }
        if isinstance(sr_row, dict):
            payload.update({
                'sr_timeframe': sr_row.get('timeframe'),
                'sr_state': sr_row.get('state'),
                'sr_trend_state': sr_row.get('trend_state'),
                'sr_structure_bias': sr_row.get('structure_bias'),
                'sr_structure_event': sr_row.get('structure_event'),
                'sr_nearest_support': safe_float(sr_row.get('nearest_support'), None),
                'sr_nearest_resistance': safe_float(sr_row.get('nearest_resistance'), None),
                'sr_support_distance_pct': safe_float(sr_row.get('support_distance_pct'), None),
                'sr_resistance_distance_pct': safe_float(sr_row.get('resistance_distance_pct'), None),
                'sr_broken_support': safe_float(sr_row.get('broken_support'), None),
                'sr_broken_resistance': safe_float(sr_row.get('broken_resistance'), None),
            })
        extra = self._structured_metadata_snapshot(meta)
        payload.update({k: v for k, v in extra.items() if k not in payload and v is not None})
        return {k: v for k, v in payload.items() if v is not None}

    # ------------------------------------------------------------------
    # Trade-management managers (sr_flip + adaptive_ladder).
    # ------------------------------------------------------------------

    def _sr_flip_management_confirmed(self, position: Position, frame: pd.DataFrame | None, last_price: float) -> None:
        if isinstance(position.metadata, dict):
            position.metadata.setdefault("management_adjustments", [])
        if self.config.risk.trade_management_mode != "sr_flip":
            return
        cfg = getattr(self.config, "support_resistance", None)
        if cfg is None or not bool(cfg.enabled):
            return
        asset_type = str(position.metadata.get("asset_type") or ASSET_TYPE_EQUITY)
        if asset_type in OPTION_ASSET_TYPES:
            return
        if frame is None or frame.empty or last_price <= 0:
            return
        symbol = str(position.metadata.get("underlying") or position.symbol)
        sr_ctx = self.data.get_support_resistance(symbol, current_price=last_price, flip_frame=frame, mode="trading", timeframe_minutes=self.active_sr_timeframe_minutes(), lookback_days=self.active_sr_lookback_days(), refresh_seconds=self.active_sr_refresh_seconds()) if self.data is not None else None
        if sr_ctx is None:
            return
        last = frame.iloc[-1]
        close = safe_float(last.get("close"), last_price)
        ema9 = safe_float(last.get("ema9"), close) if "ema9" in frame.columns else close
        ema20 = safe_float(last.get("ema20"), close) if "ema20" in frame.columns else close
        vwap = safe_float(last.get("vwap"), close) if "vwap" in frame.columns else close
        ret5 = safe_float(last.get("ret5"), 0.0) if "ret5" in frame.columns else 0.0
        atr = safe_float(last.get("atr14"), 0.0) if "atr14" in frame.columns else 0.0
        stop_buffer = max(
            float(sr_ctx.level_buffer or 0.0),
            max(atr * float(getattr(cfg, "flip_stop_buffer_atr_mult", 0.25) or 0.25), close * 0.0005),
        )
        require_momentum = bool(getattr(cfg, "flip_target_requires_momentum_confirm", True))
        structural_gap = _sr_effective_side_tolerance(self.config, close, atr=atr, sr_ctx=sr_ctx)

        def _momentum_ok(long_side: bool) -> bool:
            if not require_momentum:
                return True
            if long_side:
                return bool(close >= ema9 and close >= max(vwap, ema20) and ret5 >= -0.0005)
            return bool(close <= ema9 and close <= min(vwap, ema20) and ret5 <= 0.0005)

        if position.side == Side.LONG:
            flipped_support = sr_ctx.broken_resistance
            if flipped_support is not None:
                candidate_stop = float(flipped_support.price) - stop_buffer
                if close > candidate_stop > float(position.stop_price):
                    prior_stop = float(position.stop_price)
                    position.stop_price = float(candidate_stop)
                    if isinstance(position.metadata, dict):
                        position.metadata["sr_flip_stop_source"] = float(flipped_support.price)
                        _append_adjustment(position.metadata,{"manager": "sr_flip", "kind": "stop", "reason": "flipped_support", "from": prior_stop, "to": float(candidate_stop), "source_level": float(flipped_support.price)})
            target_level = _select_next_distinct_level(getattr(sr_ctx, 'resistances', None), float(flipped_support.price) if flipped_support is not None else None, above=True, minimum_gap=structural_gap) if flipped_support is not None else None
            if target_level is None and flipped_support is None:
                target_level = sr_ctx.nearest_resistance
            if target_level is not None and _momentum_ok(True):
                candidate_target = float(target_level.price) - float(sr_ctx.level_buffer or 0.0)
                current_target = safe_float(position.target_price, None)
                if candidate_target > close and (current_target is None or candidate_target > current_target + max(close * 0.0005, 1e-6)):
                    prior_target = float(current_target) if current_target is not None else None
                    position.target_price = float(candidate_target)
                    if isinstance(position.metadata, dict):
                        position.metadata["sr_flip_target_source"] = float(target_level.price)
                        _append_adjustment(position.metadata,{"manager": "sr_flip", "kind": "target", "reason": "next_resistance", "from": prior_target, "to": float(candidate_target), "source_level": float(target_level.price), "structural_gap": float(structural_gap)})
        else:
            flipped_resistance = sr_ctx.broken_support
            if flipped_resistance is not None:
                candidate_stop = float(flipped_resistance.price) + stop_buffer
                if close < candidate_stop < float(position.stop_price):
                    prior_stop = float(position.stop_price)
                    position.stop_price = float(candidate_stop)
                    if isinstance(position.metadata, dict):
                        position.metadata["sr_flip_stop_source"] = float(flipped_resistance.price)
                        _append_adjustment(position.metadata,{"manager": "sr_flip", "kind": "stop", "reason": "flipped_resistance", "from": prior_stop, "to": float(candidate_stop), "source_level": float(flipped_resistance.price)})
            target_level = _select_next_distinct_level(getattr(sr_ctx, 'supports', None), float(flipped_resistance.price) if flipped_resistance is not None else None, above=False, minimum_gap=structural_gap) if flipped_resistance is not None else None
            if target_level is None and flipped_resistance is None:
                target_level = sr_ctx.nearest_support
            if target_level is not None and _momentum_ok(False):
                candidate_target = float(target_level.price) + float(sr_ctx.level_buffer or 0.0)
                current_target = safe_float(position.target_price, None)
                if candidate_target < close and (current_target is None or candidate_target < current_target - max(close * 0.0005, 1e-6)):
                    prior_target = float(current_target) if current_target is not None else None
                    position.target_price = float(candidate_target)
                    if isinstance(position.metadata, dict):
                        position.metadata["sr_flip_target_source"] = float(target_level.price)
                        _append_adjustment(position.metadata,{"manager": "sr_flip", "kind": "target", "reason": "next_support", "from": prior_target, "to": float(candidate_target), "source_level": float(target_level.price), "structural_gap": float(structural_gap)})

    def _adaptive_ladder_management(self, position: Position, frame: pd.DataFrame | None, last_price: float) -> None:
        if isinstance(position.metadata, dict):
            position.metadata.setdefault("management_adjustments", [])
        mode = self.config.risk.trade_management_mode
        if mode != "adaptive_ladder":
            return
        meta = position.metadata if isinstance(position.metadata, dict) else None
        if not isinstance(meta, dict) or not bool(meta.get("ladder_management_enabled")):
            return
        asset_type = str(meta.get("asset_type") or ASSET_TYPE_EQUITY)
        if asset_type in OPTION_ASSET_TYPES:
            return
        if frame is None or frame.empty or last_price <= 0:
            return
        rungs = meta.get("ladder_rungs")
        if not isinstance(rungs, list) or not rungs:
            return
        # Only clear the suppress flag once we know the ladder manager is
        # actually going to re-evaluate it. Clearing before the guard clauses
        # meant a stale-frame tick would reset a previously-computed True flag,
        # letting update_position fire a target exit on the next cycle.
        meta["adaptive_ladder_suppress_target_exit"] = False
        try:
            active_index = max(0, min(int(meta.get("ladder_active_index", 0) or 0), len(rungs) - 1))
        except Exception:
            active_index = 0
        current = rungs[active_index] if active_index < len(rungs) else None
        if not isinstance(current, dict):
            return
        try:
            rung_price = float(current.get("price", 0.0) or 0.0)
            zone_width = max(0.0, float(current.get("zone_width", 0.0) or 0.0))
            lower = float(current.get("lower", rung_price - zone_width) or (rung_price - zone_width))
            upper = float(current.get("upper", rung_price + zone_width) or (rung_price + zone_width))
        except Exception:
            return
        if rung_price <= 0:
            return
        symbol = str(meta.get("underlying") or position.symbol)
        sr_ctx = self.data.get_support_resistance(symbol, current_price=last_price, flip_frame=frame, mode="trading", timeframe_minutes=self.active_sr_timeframe_minutes(), lookback_days=self.active_sr_lookback_days(), refresh_seconds=self.active_sr_refresh_seconds(), allow_refresh=True) if self.data is not None else None
        close = safe_float(frame.iloc[-1].get("close"), last_price)
        level_buffer = float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0)
        stop_buffer = max(level_buffer, zone_width * 0.25, close * 0.0005)
        eps = max(level_buffer * 0.15, close * 0.0001, 1e-6)
        confirm_1m = max(0, int(getattr(getattr(self.config, "support_resistance", None), "trading_flip_confirmation_1m_bars", 2) or 2))
        confirm_5m = max(0, int(getattr(getattr(self.config, "support_resistance", None), "trading_flip_confirmation_5m_bars", 1) or 1))
        current_target = safe_float(position.target_price, None)
        if position.side == Side.LONG:
            rung_confirmed = zone_flip_confirmed("resistance", lower, upper, flip_frame=frame, confirm_1m_bars=confirm_1m, confirm_5m_bars=confirm_5m, fallback_bar=None, eps=eps)
            target_reached = bool(current_target is not None and last_price >= float(current_target) - max(close * 0.0003, 1e-6))
            meta["adaptive_ladder_suppress_target_exit"] = bool(target_reached and not rung_confirmed)
            if not rung_confirmed:
                return
            candidate_stop = float(lower) - stop_buffer
            if close > candidate_stop > float(position.stop_price):
                prior_stop = float(position.stop_price)
                position.stop_price = float(candidate_stop)
                _append_adjustment(meta,{"manager": "adaptive_ladder", "kind": "stop", "reason": "promoted_support", "from": prior_stop, "to": float(candidate_stop), "source_level": float(rung_price)})
            meta["ladder_defense_price"] = float(rung_price)
            meta["ladder_defense_zone_width"] = float(zone_width)
            meta["ladder_defense_kind"] = str(current.get("kind") or "target")
            meta["ladder_last_promoted_price"] = float(rung_price)
            if active_index + 1 < len(rungs):
                next_rung = rungs[active_index + 1]
                try:
                    candidate_target = float(next_rung.get("price", 0.0) or 0.0)
                except Exception:
                    candidate_target = 0.0
                if candidate_target > close and (current_target is None or candidate_target > float(current_target) + max(close * 0.0005, 1e-6)):
                    prior_target = float(current_target) if current_target is not None else None
                    position.target_price = float(candidate_target)
                    _append_adjustment(meta,{"manager": "adaptive_ladder", "kind": "target", "reason": "next_rung", "from": prior_target, "to": float(candidate_target), "source_level": float(candidate_target)})
                meta["ladder_active_index"] = int(active_index + 1)
                meta["ladder_final_rung_cleared"] = False
            else:
                if current_target is not None:
                    prior_target = float(current_target)
                    position.target_price = None
                    _append_adjustment(meta,{"manager": "adaptive_ladder", "kind": "target", "reason": "final_rung_runner", "from": prior_target, "to": None, "source_level": float(rung_price)})
                meta["ladder_final_rung_cleared"] = True
                meta["adaptive_ladder_suppress_target_exit"] = False
        else:
            rung_confirmed = zone_flip_confirmed("support", lower, upper, flip_frame=frame, confirm_1m_bars=confirm_1m, confirm_5m_bars=confirm_5m, fallback_bar=None, eps=eps)
            target_reached = bool(current_target is not None and last_price <= float(current_target) + max(close * 0.0003, 1e-6))
            meta["adaptive_ladder_suppress_target_exit"] = bool(target_reached and not rung_confirmed)
            if not rung_confirmed:
                return
            candidate_stop = float(upper) + stop_buffer
            if close < candidate_stop < float(position.stop_price):
                prior_stop = float(position.stop_price)
                position.stop_price = float(candidate_stop)
                _append_adjustment(meta,{"manager": "adaptive_ladder", "kind": "stop", "reason": "promoted_resistance", "from": prior_stop, "to": float(candidate_stop), "source_level": float(rung_price)})
            meta["ladder_defense_price"] = float(rung_price)
            meta["ladder_defense_zone_width"] = float(zone_width)
            meta["ladder_defense_kind"] = str(current.get("kind") or "target")
            meta["ladder_last_promoted_price"] = float(rung_price)
            if active_index + 1 < len(rungs):
                next_rung = rungs[active_index + 1]
                try:
                    candidate_target = float(next_rung.get("price", 0.0) or 0.0)
                except Exception:
                    candidate_target = 0.0
                if 0 < candidate_target < close and (current_target is None or candidate_target < float(current_target) - max(close * 0.0005, 1e-6)):
                    prior_target = float(current_target) if current_target is not None else None
                    position.target_price = float(candidate_target)
                    _append_adjustment(meta,{"manager": "adaptive_ladder", "kind": "target", "reason": "next_rung", "from": prior_target, "to": float(candidate_target), "source_level": float(candidate_target)})
                meta["ladder_active_index"] = int(active_index + 1)
                meta["ladder_final_rung_cleared"] = False
            else:
                if current_target is not None:
                    prior_target = float(current_target)
                    position.target_price = None
                    _append_adjustment(meta,{"manager": "adaptive_ladder", "kind": "target", "reason": "final_rung_runner", "from": prior_target, "to": None, "source_level": float(rung_price)})
                meta["ladder_final_rung_cleared"] = True
                meta["adaptive_ladder_suppress_target_exit"] = False

    # ------------------------------------------------------------------
    # Broker exit recovery — called when a close_position() returned an
    # ambiguous result and we need to re-check broker state.
    # ------------------------------------------------------------------

    def _recover_equity_exit_from_broker(self, position: Position, result, reason: str, last_price: float | None) -> tuple[bool, int, float | None, float | None]:
        if getattr(result, "ok", False):
            return False, 0, None, None
        if not getattr(result, "order_id", None):
            return False, 0, None, None
        if not order_result_needs_broker_recheck(getattr(result, "message", None)):
            return False, 0, None, None
        broker_symbol = str(position.metadata.get("underlying") or position.symbol)
        row = self._broker_position_row(broker_symbol)
        side, broker_qty, _ = broker_position_side_qty(row)
        local_qty = int(position.qty)
        remaining_qty = int(broker_qty) if side == position.side else 0
        if remaining_qty >= local_qty:
            return False, 0, None, None
        exit_qty = max(1, local_qty - max(0, remaining_qty))
        exit_price_value = safe_float(getattr(result, "fill_price", None), None)
        if exit_price_value is None:
            exit_price_value = safe_float(last_price, None)
        if exit_price_value is None:
            exit_price_value = float(position.entry_price)
        exit_price = float(exit_price_value)
        exited_position = copy.copy(position)
        exited_position.qty = int(exit_qty)
        fill_price_estimated = getattr(result, "fill_price", None) is None
        final_exit = remaining_qty <= 0
        realized = self.account.record_exit(
            exited_position,
            exit_price,
            reason,
            final_exit=final_exit,
            remaining_qty_after_exit=int(max(0, remaining_qty)),
            fill_price_estimated=fill_price_estimated,
            broker_recovered=True,
        )
        self.audit.log_structured(
            "TRADE_SUMMARY",
            self._trade_summary_payload(
                exited_position,
                exit_price,
                realized,
                reason,
                final_exit=final_exit,
                remaining_qty_after_exit=int(max(0, remaining_qty)),
                broker_recovered=True,
                fill_price_estimated=fill_price_estimated,
            ),
        )
        if remaining_qty <= 0:
            self.risk.register_exit(
                str(position.metadata.get("underlying") or position.symbol),
                realized,
                additional_symbol=position.symbol,
                side=position.side,
                exit_price=exit_price,
                atr=None,  # broker-recovery path: no bars context for ATR
            )
            self.positions.pop(position.symbol, None)
        else:
            self.risk.register_realized_pnl(realized)
            position.qty = int(remaining_qty)
            if isinstance(position.metadata, dict):
                position.metadata["qty"] = int(remaining_qty)
                position.metadata["broker_reconciled_after_order_uncertainty"] = True
                position.metadata["broker_recovery_order_id"] = str(result.order_id)
                position.metadata["broker_recovery_message"] = str(result.message)
            self.positions[position.symbol] = position
        self._save_reconcile_metadata()
        LOG.warning("Recovered equity exit state from broker for %s exit_qty=%s remaining_qty=%s after ambiguous order result=%s", position.symbol, exit_qty, remaining_qty, result.message)
        return True, int(exit_qty), float(exit_price), float(realized)

    def _recover_option_exit_from_broker(self, position: Position, result, reason: str, last_price: float | None) -> tuple[bool, int, float | None, float | None]:
        if getattr(result, "ok", False):
            return False, 0, None, None
        if not getattr(result, "order_id", None):
            return False, 0, None, None
        if not order_result_needs_broker_recheck(getattr(result, "message", None)):
            return False, 0, None, None
        asset_type = str(position.metadata.get("asset_type") or "").upper()
        local_qty = int(position.qty)
        if asset_type == ASSET_TYPE_OPTION_SINGLE:
            option_symbol = str(position.metadata.get("option_symbol") or "")
            row = self._broker_position_row(option_symbol)
            side, broker_qty, _ = broker_position_side_qty(row)
            remaining_qty = int(broker_qty) if side == position.side else 0
        elif asset_type == ASSET_TYPE_OPTION_VERTICAL:
            long_symbol = str(position.metadata.get("long_leg_symbol") or "")
            short_symbol = str(position.metadata.get("short_leg_symbol") or "")
            rows = self._broker_position_rows([long_symbol, short_symbol])
            long_row = rows.get(long_symbol.upper()) if long_symbol else None
            short_row = rows.get(short_symbol.upper()) if short_symbol else None
            long_side, long_qty, _ = broker_position_side_qty(long_row)
            short_side, short_qty, _ = broker_position_side_qty(short_row)
            if (long_qty > 0 or short_qty > 0) and (long_side != Side.LONG or short_side != Side.SHORT):
                return False, 0, None, None
            remaining_qty = 0
            if int(long_qty) > 0 or int(short_qty) > 0:
                if int(long_qty) != int(short_qty):
                    return False, 0, None, None
                remaining_qty = int(long_qty)
        else:
            return False, 0, None, None
        if remaining_qty >= local_qty:
            return False, 0, None, None
        exit_qty = max(1, local_qty - max(0, remaining_qty))
        exit_price_value = safe_float(getattr(result, "fill_price", None), None)
        if exit_price_value is None:
            exit_price_value = safe_float(last_price, None)
        if exit_price_value is None:
            exit_price_value = float(position.entry_price)
        exit_price = float(exit_price_value)
        exited_position = copy.copy(position)
        exited_position.qty = int(exit_qty)
        fill_price_estimated = getattr(result, "fill_price", None) is None
        final_exit = remaining_qty <= 0
        realized = self.account.record_exit(
            exited_position,
            exit_price,
            reason,
            final_exit=final_exit,
            remaining_qty_after_exit=int(max(0, remaining_qty)),
            fill_price_estimated=fill_price_estimated,
            broker_recovered=True,
        )
        self.audit.log_structured(
            "TRADE_SUMMARY",
            self._trade_summary_payload(
                exited_position,
                exit_price,
                realized,
                reason,
                final_exit=final_exit,
                remaining_qty_after_exit=int(max(0, remaining_qty)),
                broker_recovered=True,
                fill_price_estimated=fill_price_estimated,
            ),
        )
        if remaining_qty <= 0:
            self.risk.register_exit(
                str(position.metadata.get("underlying") or position.symbol),
                realized,
                additional_symbol=position.symbol,
                side=position.side,
                exit_price=exit_price,
                atr=None,  # broker-recovery path: no bars context for ATR
            )
            self.positions.pop(position.symbol, None)
        else:
            self.risk.register_realized_pnl(realized)
            position.qty = int(remaining_qty)
            if isinstance(position.metadata, dict):
                position.metadata["qty"] = int(remaining_qty)
                position.metadata["broker_reconciled_after_order_uncertainty"] = True
                position.metadata["broker_recovery_order_id"] = str(result.order_id)
                position.metadata["broker_recovery_message"] = str(result.message)
            self.positions[position.symbol] = position
        self._save_reconcile_metadata()
        LOG.warning("Recovered option exit state from broker for %s exit_qty=%s remaining_qty=%s asset_type=%s after ambiguous order result=%s", position.symbol, exit_qty, remaining_qty, asset_type, result.message)
        return True, int(exit_qty), float(exit_price), float(realized)

    # ------------------------------------------------------------------
    # Main entry point — runs every management cycle.
    # ------------------------------------------------------------------

    def manage_positions(self, _now: datetime, bars) -> None:
        for key, position in list(self.positions.items()):
            last_price, market_snapshot = self._position_management_snapshot(position, bars)
            asset_type = str(position.metadata.get("asset_type") or ASSET_TYPE_EQUITY)
            management_symbol = str(position.metadata.get("underlying") or position.symbol)
            management_frame = bars.get(management_symbol)
            if asset_type not in OPTION_ASSET_TYPES:
                underlying_price = last_price
            else:
                underlying_price = self.underlying_price_for_position(position, bars, None)
                if underlying_price is None:
                    quote = self.data.get_quote(management_symbol)
                    if quote is not None:
                        mark = quote.get("mark")
                        try:
                            if mark is not None and float(mark) > 0:
                                underlying_price = float(mark)
                        except (TypeError, ValueError):
                            underlying_price = None
            # Always reset management_adjustments at the start of each cycle to
            # prevent stale adjustments from persisting when price is unavailable.
            if isinstance(position.metadata, dict):
                position.metadata["management_adjustments"] = []
            should_exit, reason = False, "hold"
            if last_price is not None:
                self._sr_flip_management_confirmed(position, management_frame, float(last_price))
                self._adaptive_ladder_management(position, management_frame, float(last_price))
                self._update_position_diagnostics(position, last_price, underlying_price)
                should_exit, reason = self.risk.update_position(position, last_price)
                adjustments = position.metadata.get("management_adjustments") if isinstance(position.metadata, dict) else None
                if adjustments:
                    for adj in adjustments:
                        if not isinstance(adj, dict):
                            continue
                        self.audit.log_structured("POSITION_ADJUSTMENT", {
                            "symbol": key,
                            "underlying": str(position.metadata.get("underlying") or position.symbol),
                            "asset_type": str(position.metadata.get("asset_type") or ASSET_TYPE_EQUITY),
                            "manager": str(adj.get("manager") or "unknown"),
                            "kind": str(adj.get("kind") or "unknown"),
                            "reason": str(adj.get("reason") or "unknown"),
                            "from": safe_float(adj.get("from"), None),
                            "to": safe_float(adj.get("to"), None),
                            "source_level": safe_float(adj.get("source_level"), None),
                            "last_price": safe_float(last_price, None),
                        })
            if not should_exit:
                should_exit, reason = self.strategy.position_exit_signal(position, bars, data=self.data)
            if self.strategy.should_force_flatten(position):
                if not position.metadata.get("_force_flatten_logged"):
                    LOG.warning("Force flatten triggered for %s qty=%s side=%s", key, position.qty, position.side.value)
                    position.metadata["_force_flatten_logged"] = True
                should_exit, reason = True, "force_flatten"
            if not should_exit:
                continue
            exit_context = self._position_exit_context(position, reason, last_price, underlying_price, market_snapshot, bars)
            if not self.executor.can_close_position_now(position, _now):
                self.audit.log_cycle(
                    f"exit_gate:{key}",
                    f"session_closed:{reason}",
                    f"Exit deferred {key} qty={position.qty} reason={reason} because market session is closed",
                    interval=60.0,
                    level=TRADEFLOW_LEVEL,
                )
                continue
            result = self.executor.close_position(position, data=self.data, market_snapshot=market_snapshot)
            if not result.ok:
                if asset_type == ASSET_TYPE_EQUITY:
                    recovered, recovered_exit_qty, recovered_exit_price, recovered_realized = self._recover_equity_exit_from_broker(position, result, reason, last_price)
                elif asset_type in OPTION_ASSET_TYPES:
                    recovered, recovered_exit_qty, recovered_exit_price, recovered_realized = self._recover_option_exit_from_broker(position, result, reason, last_price)
                else:
                    recovered, recovered_exit_qty, recovered_exit_price, recovered_realized = (False, 0, None, None)
                if recovered:
                    remaining_qty_after_exit = 0
                    if key in self.positions:
                        remaining_qty_after_exit = max(0, int(self.positions[key].qty))
                    self.audit.log_structured("EXIT_CONTEXT", {**exit_context, "symbol": key, "qty": int(recovered_exit_qty), "filled_qty": int(recovered_exit_qty), "remaining_qty_after_exit": int(remaining_qty_after_exit), "result_message": result.message, "fill_price": safe_float(recovered_exit_price, None), "realized_pnl": safe_float(recovered_realized, None), "attempt_status": "broker_recovered"})
                    continue
                LOG.log(TRADEFLOW_LEVEL, "Exit attempt %s qty=%s reason=%s result=%s", key, position.qty, reason, result.message)
                self.audit.log_structured("EXIT_CONTEXT", {**exit_context, "symbol": key, "qty": int(position.qty), "result_message": result.message, "attempt_status": "not_filled"})
                continue
            if result.filled_qty is None:
                # No fill quantity reported (e.g., live broker that didn't return filled_qty) — assume full exit
                exit_qty = position.qty
            else:
                filled_qty = int(result.filled_qty or 0)
                if filled_qty <= 0:
                    # Broker reported an ok=True result but zero shares actually filled — treat as failed exit
                    LOG.log(TRADEFLOW_LEVEL, "Exit returned ok but filled_qty=0 for %s reason=%s result=%s", key, reason, result.message)
                    self.audit.log_structured("EXIT_CONTEXT", {**exit_context, "symbol": key, "qty": int(position.qty), "filled_qty": 0, "result_message": result.message, "attempt_status": "filled_qty_zero"})
                    continue
                exit_qty = max(1, min(position.qty, filled_qty))
            if exit_qty < position.qty:
                LOG.log(TRADEFLOW_LEVEL, "Partial exit %s requested_qty=%s filled_qty=%s reason=%s result=%s", key, position.qty, exit_qty, reason, result.message)
            else:
                LOG.log(TRADEFLOW_LEVEL, "Exit %s qty=%s reason=%s result=%s", key, position.qty, reason, result.message)
            exit_price_value = result.fill_price if result.fill_price is not None else last_price
            if exit_price_value is None:
                LOG.warning("Exit fill price unavailable for %s after close_position(); skipping trade record this cycle — will retry with fresh quotes", key)
                continue
            exit_price = float(exit_price_value)
            # Exit slippage: how far the fill was from the intended level
            if isinstance(position.metadata, dict):
                if reason == "stop":
                    position.metadata["exit_slippage"] = round(abs(exit_price - float(position.stop_price)), 6)
                elif reason == "target" and position.target_price is not None:
                    position.metadata["exit_slippage"] = round(abs(exit_price - float(position.target_price)), 6)
            exited_position = copy.copy(position)
            exited_position.qty = exit_qty
            remaining_qty_after_exit = max(0, int(position.qty) - int(exit_qty))
            fill_price_estimated = result.fill_price is None
            final_exit = exit_qty >= position.qty
            realized = self.account.record_exit(
                exited_position,
                exit_price,
                reason,
                final_exit=final_exit,
                remaining_qty_after_exit=remaining_qty_after_exit,
                fill_price_estimated=fill_price_estimated,
                broker_recovered=False,
            )
            self.audit.log_structured("EXIT_CONTEXT", {**exit_context, "symbol": key, "qty": int(exit_qty), "filled_qty": int(exit_qty), "remaining_qty_after_exit": remaining_qty_after_exit, "result_message": result.message, "fill_price": float(exit_price), "realized_pnl": float(realized), "attempt_status": "filled"})
            self.audit.log_structured("TRADE_SUMMARY", self._trade_summary_payload(exited_position, exit_price, realized, reason, final_exit=final_exit, remaining_qty_after_exit=remaining_qty_after_exit, broker_recovered=False, fill_price_estimated=fill_price_estimated))
            if exit_qty >= position.qty:
                # ATR from management frame feeds the same-level retry block:
                # the block zone is sized in ATR so small cheap stocks and
                # high-priced names use proportional thresholds. None when
                # the frame has no atr14 column yet (warmup); block skipped.
                exit_atr = safe_float(management_frame.iloc[-1].get("atr14"), None) if (management_frame is not None and not management_frame.empty and "atr14" in management_frame.columns) else None
                self.risk.register_exit(
                    str(position.metadata.get("underlying") or key),
                    realized,
                    additional_symbol=key,
                    side=position.side,
                    exit_price=exit_price,
                    atr=exit_atr,
                )
                del self.positions[key]
                self._save_reconcile_metadata()
            else:
                self.risk.register_realized_pnl(realized)
                position.qty -= exit_qty
                if isinstance(position.metadata, dict):
                    position.metadata["qty"] = position.qty
                self.positions[key] = position
                self._save_reconcile_metadata()

        self._save_reconcile_metadata()
