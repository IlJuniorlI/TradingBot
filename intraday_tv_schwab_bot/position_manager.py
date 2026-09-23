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
- An exit order whose submit call could not settle it (left working through
  a halt, a cancel the broker never confirmed) is tracked on the position
  and settled from the order's own fill record (``_exit_order_in_flight``),
  never from a positions snapshot.
- Config accessors ``active_htf_*`` (HTF timeframe / lookback) live here
  so engine call sites in entry / screening paths can dispatch through
  ``self.position_manager.active_htf_*()`` — the accessors read
  ``self.config`` and ``self.strategy.params`` and have a single home.
  HTF refresh cadence is now bar-aligned in ``MarketDataStore.should_refresh_htf_context``
  so there's no longer a refresh-seconds knob on the strategy side.
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
from .execution import BracketCancel, SchwabExecutor
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
from .broker_positions import active_broker_bracket, order_result_needs_broker_recheck
from .support_resistance import zone_flip_confirmed
from .utils import TRADEFLOW_LEVEL, append_management_adjustment as _append_adjustment, now_et

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


def _next_unpassed_rung(rungs: list, active_index: int, close: float,
                        side: Side, gap: float) -> int | None:
    """Index of the first rung after ``active_index`` that price has not already
    passed, or ``None`` when every remaining rung is behind the trade.

    A rung behind price cannot serve as a target — setting one would exit
    immediately — and cannot serve as a defense level either, so the ladder is
    finished and the caller promotes the position to a runner.
    """
    for index in range(int(active_index) + 1, len(rungs)):
        entry = rungs[index]
        if not isinstance(entry, dict):
            continue
        try:
            price = float(entry.get("price", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        if side == Side.LONG:
            if price > close + gap:
                return index
        elif price < close - gap:
            return index
    return None


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
        self._structured_metadata_snapshot = structured_metadata_snapshot

    # ------------------------------------------------------------------
    # SR-config accessors (read config + strategy params).
    # ------------------------------------------------------------------

    def active_htf_minutes(self) -> int:
        """HTF (higher timeframe) for SR detection — strategies declare via
        `params.htf_minutes`; otherwise inherit
        `support_resistance.timeframe_minutes`."""
        cfg = getattr(self.config, "support_resistance", None)
        fallback = int(getattr(cfg, "timeframe_minutes", 15)) if cfg is not None else 15
        params = getattr(getattr(self, "strategy", None), "params", {}) or {}
        return int(params.get("htf_minutes", fallback))

    def active_htf_lookback_days(self) -> int:
        cfg = getattr(self.config, "support_resistance", None)
        fallback = int(getattr(cfg, "lookback_days", 10)) if cfg is not None else 10
        params = getattr(getattr(self, "strategy", None), "params", {}) or {}
        return int(params.get("htf_lookback_days", fallback))

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
        meta['last_mark_price'] = float(mark_price)
        if underlying_price is not None:
            meta['diag_best_underlying_price'] = float(underlying_price)
            meta['diag_worst_underlying_price'] = float(underlying_price)
            meta['underlying_high_since_entry'] = float(underlying_price)
            meta['underlying_low_since_entry'] = float(underlying_price)

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
        # The exit signal runs on the UNDERLYING's frame. For an option these
        # are what it compares against: the premium it is holding at (for R)
        # and how far the underlying itself has travelled since entry.
        meta['last_mark_price'] = float(mark_price)
        if underlying_price is not None:
            underlying = float(underlying_price)
            meta['underlying_high_since_entry'] = max(underlying, float(meta.get('underlying_high_since_entry', underlying)))
            meta['underlying_low_since_entry'] = min(underlying, float(meta.get('underlying_low_since_entry', underlying)))
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
            # Per-sector index ETFs the entry was confirmed against
            # (stamped by top_tier_adaptive at signal-build time via
            # ``_indices_for_symbol``). Useful for verifying the
            # sector_index_map is firing and slicing exit outcomes by
            # which sector was confirming the trade.
            'confirmation_indices': meta.get('confirmation_indices'),
            # Volatility widening factor applied to the structural stop
            # at entry (Tier 2a ATR-expansion + early-session widening).
            # 1.0 = no widening applied.
            'vol_widening_factor': safe_float(meta.get('vol_widening_factor'), None),
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
        sr_ctx = self.data.get_support_resistance(symbol, current_price=last_price, flip_frame=frame, mode="trading", timeframe_minutes=self.active_htf_minutes(), lookback_days=self.active_htf_lookback_days()) if self.data is not None else None
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

    def _ladder_indices_still_aligned(
        self,
        indices: list[str] | tuple[str, ...] | None,
        side: Side,
    ) -> bool:
        """Re-check at target-hit time whether at least one of the trade's
        confirmation indices is STILL aligned with the trade direction.

        Same close>vwap + ema9>=ema20 check (mirror for SHORT) that the
        entry-side ``_index_confirms`` does — applied again at suppress
        decision so a sector reversal can short-circuit the adaptive-
        ladder wait window. If the broader sector tape has flipped
        against the trade since entry, the trade's apparent strength is
        divergent and target-exit should fire instead of waiting for the
        multi-bar zone flip.

        Returns True if no ``indices`` are configured (no extra gate —
        treats the alignment check as inert for legacy positions /
        strategies that don't stamp ``confirmation_indices`` at entry).
        """
        if not indices:
            return True
        if self.data is None:
            return True
        for sym in indices:
            sym_key = str(sym or "").upper().strip()
            if not sym_key:
                continue
            try:
                frame = self.data.get_merged(sym_key)
            except Exception:
                continue
            if frame is None or len(frame) == 0:
                continue
            try:
                last = frame.iloc[-1]
                close = float(last.get("close") or 0.0)
                vwap_raw = last.get("vwap")
                vwap = float(vwap_raw) if vwap_raw is not None else close
                ema9_raw = last.get("ema9")
                ema9 = float(ema9_raw) if ema9_raw is not None else close
                ema20_raw = last.get("ema20")
                ema20 = float(ema20_raw) if ema20_raw is not None else close
            except (TypeError, ValueError, AttributeError):
                continue
            if side == Side.LONG and close > vwap and ema9 >= ema20:
                return True
            if side == Side.SHORT and close < vwap and ema9 <= ema20:
                return True
        return False

    @staticmethod
    def _ladder_target_strength_confirmed(
        frame: pd.DataFrame | None,
        side: Side,
        target_price: float | None,
        *,
        close_pos_min: float = 0.55,
    ) -> bool:
        """Return True when the last FULLY CLOSED bar shows a strong push
        through ``target_price``. Used as a pre-suppress gate so single-tick
        wicks at the target don't lock the position into a multi-bar
        zone-flip wait window.

        Strong push:
          LONG:  close >= target  AND  (close - low) / (high - low) >= ``close_pos_min``
          SHORT: close <= target  AND  (high - close) / (high - low) >= ``close_pos_min``

        ``close_pos_min`` default 0.55 means the close has to land in the
        upper 55% of the bar's intra-bar range (for LONG). Doji / wick-top
        prints don't qualify.

        Returns False on insufficient data — caller treats False as "not
        strong enough to suppress" and lets the normal target-exit fire.
        """
        if target_price is None or frame is None or len(frame) < 2:
            return False
        # iloc[-1] is the current FORMING bar; iloc[-2] is the last fully
        # closed bar. Strength must be evaluated on a closed bar so an
        # intra-bar tick doesn't get treated as a confirmed breakout.
        try:
            bar = frame.iloc[-2]
            bar_high = float(bar.get("high"))
            bar_low = float(bar.get("low"))
            bar_close = float(bar.get("close"))
            target = float(target_price)
        except (TypeError, ValueError, KeyError):
            return False
        bar_range = bar_high - bar_low
        if bar_range <= 0:
            return False
        if side == Side.LONG:
            if bar_close < target:
                return False
            close_pos = (bar_close - bar_low) / bar_range
        else:
            if bar_close > target:
                return False
            close_pos = (bar_high - bar_close) / bar_range
        return close_pos >= close_pos_min

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
        sr_ctx = self.data.get_support_resistance(symbol, current_price=last_price, flip_frame=frame, mode="trading", timeframe_minutes=self.active_htf_minutes(), lookback_days=self.active_htf_lookback_days(), allow_refresh=True) if self.data is not None else None
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
            # Confirmation layer #1: require the last CLOSED bar to show
            # a strong upper-body push through the target before
            # suppressing. Filters intra-bar wick-throughs that revert.
            breakout_strength = self._ladder_target_strength_confirmed(frame, Side.LONG, current_target)
            # Confirmation layer #2: re-check the trade's entry-time
            # confirmation indices (stamped on metadata as
            # ``confirmation_indices``). If the sector ETF has flipped
            # bearish since entry, the stock's target-tag is divergent
            # from its peer group — exit at target instead of waiting
            # for a zone flip that's now structurally less likely.
            indices_aligned = self._ladder_indices_still_aligned(
                meta.get("confirmation_indices"), Side.LONG,
            )
            meta["adaptive_ladder_suppress_target_exit"] = bool(
                target_reached and breakout_strength and indices_aligned and not rung_confirmed
            )
            if not rung_confirmed:
                return
            # Validate the promoted stop against BOTH the bar close and the
            # LIVE price. `close` comes from the management frame, while the
            # exit check in RiskManager.update_position runs against the quote
            # snapshot — two different sources that diverge on a fast move.
            # Promoting on `close` alone could set a stop the quote had already
            # fallen through, and update_position then stopped the trade out on
            # the same cycle at a price well past it: rung 1 at 101.00 with the
            # bar closing 101.50 and a 99.00 quote promoted the stop to 100.69
            # and exited immediately, where the original 98.00 stop would have
            # held. Taking the tighter of the two means a rung price has
            # already fallen back through simply does not promote, and the
            # position keeps the stop it had.
            reference_price = min(close, float(last_price))
            candidate_stop = float(lower) - stop_buffer
            if reference_price > candidate_stop > float(position.stop_price):
                prior_stop = float(position.stop_price)
                position.stop_price = float(candidate_stop)
                _append_adjustment(meta,{"manager": "adaptive_ladder", "kind": "stop", "reason": "promoted_support", "from": prior_stop, "to": float(candidate_stop), "source_level": float(rung_price)})
            meta["ladder_defense_price"] = float(rung_price)
            meta["ladder_defense_zone_width"] = float(zone_width)
            meta["ladder_defense_kind"] = str(current.get("kind") or "target")
            meta["ladder_last_promoted_price"] = float(rung_price)
            # Advance to the first rung price has NOT already passed.
            #
            # Stepping blindly to active_index + 1 left the target frozen on a
            # rung BEHIND price whenever one cycle cleared several rungs at
            # once. The guard below correctly refuses to set a target under
            # price, but the index advanced regardless, so the position kept a
            # stale target it had already blown through and update_position
            # fired a target exit on the next tick. Walked a LONG from 100.5 to
            # 104.9 against rungs at 101/102/103/104: the index stepped 1, 2, 3
            # while the target stayed 101.00 the whole way, exiting the
            # remainder at rung 1 on exactly the fast move the ladder exists to
            # ride. Consistent with the 2026-06-01 dry run, where runners came
            # in around 1R against 3-4R of MFE.
            #
            # When price has outrun EVERY remaining rung the ladder is spent,
            # so it falls through to the runner branch below instead of
            # defending a level that is now behind the trade.
            rung_gap = max(close * 0.0005, 1e-6)
            next_index = _next_unpassed_rung(rungs, active_index, close, Side.LONG, rung_gap)
            if next_index is not None:
                candidate_target = float(rungs[next_index].get("price", 0.0) or 0.0)
                if current_target is None or candidate_target > float(current_target) + rung_gap:
                    prior_target = float(current_target) if current_target is not None else None
                    position.target_price = float(candidate_target)
                    _append_adjustment(meta,{"manager": "adaptive_ladder", "kind": "target", "reason": "next_rung", "from": prior_target, "to": float(candidate_target), "source_level": float(candidate_target)})
                meta["ladder_active_index"] = int(next_index)
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
            # Mirror of the LONG suppress gate: strength check + index
            # re-alignment check before suppressing the SHORT's target.
            breakout_strength = self._ladder_target_strength_confirmed(frame, Side.SHORT, current_target)
            indices_aligned = self._ladder_indices_still_aligned(
                meta.get("confirmation_indices"), Side.SHORT,
            )
            meta["adaptive_ladder_suppress_target_exit"] = bool(
                target_reached and breakout_strength and indices_aligned and not rung_confirmed
            )
            if not rung_confirmed:
                return
            # Mirror of the LONG guard above: the tighter of bar close and
            # live quote, so a promotion is never validated against a price
            # the quote has already passed.
            reference_price = max(close, float(last_price))
            candidate_stop = float(upper) + stop_buffer
            if reference_price < candidate_stop < float(position.stop_price):
                prior_stop = float(position.stop_price)
                position.stop_price = float(candidate_stop)
                _append_adjustment(meta,{"manager": "adaptive_ladder", "kind": "stop", "reason": "promoted_resistance", "from": prior_stop, "to": float(candidate_stop), "source_level": float(rung_price)})
            meta["ladder_defense_price"] = float(rung_price)
            meta["ladder_defense_zone_width"] = float(zone_width)
            meta["ladder_defense_kind"] = str(current.get("kind") or "target")
            meta["ladder_last_promoted_price"] = float(rung_price)
            # Mirror of the LONG skip-ahead above.
            rung_gap = max(close * 0.0005, 1e-6)
            next_index = _next_unpassed_rung(rungs, active_index, close, Side.SHORT, rung_gap)
            if next_index is not None:
                candidate_target = float(rungs[next_index].get("price", 0.0) or 0.0)
                if current_target is None or candidate_target < float(current_target) - rung_gap:
                    prior_target = float(current_target) if current_target is not None else None
                    position.target_price = float(candidate_target)
                    _append_adjustment(meta,{"manager": "adaptive_ladder", "kind": "target", "reason": "next_rung", "from": prior_target, "to": float(candidate_target), "source_level": float(candidate_target)})
                meta["ladder_active_index"] = int(next_index)
                meta["ladder_final_rung_cleared"] = False
            else:
                if current_target is not None:
                    prior_target = float(current_target)
                    position.target_price = None
                    _append_adjustment(meta,{"manager": "adaptive_ladder", "kind": "target", "reason": "final_rung_runner", "from": prior_target, "to": None, "source_level": float(rung_price)})
                meta["ladder_final_rung_cleared"] = True
                meta["adaptive_ladder_suppress_target_exit"] = False

    # ------------------------------------------------------------------
    # Broker-side bracket lifecycle
    #
    # With execution.bracket_orders_enabled the protective stop (and, in
    # stop_and_target leg mode, the target) rest AT THE BROKER. That moves
    # three responsibilities here: notice when a resting child filled, keep
    # the resting levels in step with the engine's in-trade management, and
    # cancel them before the engine markets out for its own reasons.
    # ------------------------------------------------------------------

    def _book_broker_exit(self, key: str, position: Position, exit_qty: int, exit_price: float,
                          reason: str, bars, *, result_message: str, attempt_status: str,
                          fill_price_estimated: bool) -> float:
        """Record an exit the engine learned of from the BROKER -- a resting
        bracket child that filled, or an exit order that filled after its
        submit call returned -- mirroring the manage_positions tail. Returns
        the realized P&L of the slice."""
        management_symbol = str(position.metadata.get("underlying") or position.symbol)
        management_frame = bars.get(management_symbol)
        exit_context = self._position_exit_context(position, reason, exit_price, exit_price, None, bars)
        exited_position = copy.copy(position)
        exited_position.qty = int(exit_qty)
        remaining_qty_after_exit = max(0, int(position.qty) - int(exit_qty))
        final_exit = int(exit_qty) >= int(position.qty)
        realized = self.account.record_exit(
            exited_position, float(exit_price), reason,
            final_exit=final_exit,
            remaining_qty_after_exit=remaining_qty_after_exit,
            fill_price_estimated=fill_price_estimated,
            broker_recovered=True,
        )
        self.audit.log_structured("EXIT_CONTEXT", {
            **exit_context, "symbol": key, "qty": int(exit_qty), "filled_qty": int(exit_qty),
            "remaining_qty_after_exit": remaining_qty_after_exit,
            "result_message": result_message, "fill_price": float(exit_price),
            "realized_pnl": float(realized), "attempt_status": attempt_status,
        })
        self.audit.log_structured("TRADE_SUMMARY", self._trade_summary_payload(
            exited_position, float(exit_price), realized, reason,
            final_exit=final_exit, remaining_qty_after_exit=remaining_qty_after_exit,
            broker_recovered=True, fill_price_estimated=fill_price_estimated,
        ))
        if final_exit:
            exit_atr = (
                safe_float(management_frame.iloc[-1].get("atr14"), None)
                if (management_frame is not None and not management_frame.empty and "atr14" in management_frame.columns)
                else None
            )
            self.risk.register_exit(
                management_symbol, realized, additional_symbol=key, side=position.side,
                entry_price=float(position.entry_price), exit_price=float(exit_price),
                atr=exit_atr,
            )
            self.positions.pop(key, None)
        else:
            self.risk.register_realized_pnl(realized)
            position.qty -= int(exit_qty)
            if isinstance(position.metadata, dict):
                position.metadata["qty"] = position.qty
            self.positions[key] = position
        self._save_reconcile_metadata()
        return float(realized)

    def _reconcile_bracket_fills(self, bars) -> None:
        """Book exits the broker already executed, before anything else runs.

        MUST run ahead of the management loop: a filled resting child means the
        position no longer exists at the broker, and managing or exiting a
        phantom position sends a duplicate order that opens a NEW position in
        the opposite direction.
        """
        bracketed = {
            key: position for key, position in self.positions.items()
            if active_broker_bracket(position) is not None
        }
        if not bracketed:
            return
        states = self.executor.fetch_order_states()
        if states is None:
            # Could not read broker state. Do NOT assume "nothing filled" --
            # log loudly and leave positions untouched for the next cycle.
            LOG.warning(
                "Bracket reconcile could not read broker order state for %s position(s); "
                "engine state may be stale this cycle", len(bracketed),
            )
            return
        for key, position in bracketed.items():
            bracket = active_broker_bracket(position)
            if bracket is None:
                continue
            for child_key, reason in (("stop_order_id", "broker_stop"), ("target_order_id", "broker_target")):
                child_id = bracket.get(child_key)
                if not child_id:
                    continue
                state = states.get(str(child_id))
                if not isinstance(state, dict) or not state.get("is_filled"):
                    continue
                filled_qty = int(state.get("filled_qty") or 0)
                if filled_qty <= 0:
                    continue
                exit_qty = max(1, min(int(position.qty), filled_qty))
                fill_price = state.get("fill_price")
                if fill_price is None:
                    # Broker reported a fill without a price; fall back to the
                    # level the child was resting at, which is what it triggered on.
                    fill_price = bracket.get("stop_price") if child_key == "stop_order_id" else bracket.get("target_price")
                if fill_price is None:
                    LOG.error(
                        "Bracket child %s for %s filled but no fill price is available; "
                        "cannot book the exit this cycle", child_id, key,
                    )
                    continue
                LOG.log(TRADEFLOW_LEVEL, "Bracket %s filled for %s qty=%s price=%s", reason, key, exit_qty, fill_price)
                bracket["active"] = False
                bracket["state"] = f"filled:{reason}"
                self._book_broker_exit(
                    key, position, exit_qty, float(fill_price), reason, bars,
                    result_message="bracket_child_filled", attempt_status="broker_bracket",
                    fill_price_estimated=state.get("fill_price") is None,
                )
                if key not in self.positions:
                    # The OCO normally takes the sibling down, but a child moved
                    # by replace is a new order the OCO may not link. Anything
                    # still resting against a closed position opens a new one
                    # in the opposite direction when it triggers.
                    leftover = self.executor.cancel_bracket(bracket)
                    if not leftover.ok:
                        LOG.error(
                            "Could not confirm the rest of %s's bracket is down after its %s filled (%s) -- "
                            "check the broker for a resting order on a closed position",
                            key, reason, leftover.message,
                        )
                break

    def _book_bracket_cancel_fills(self, key: str, position: Position, bracket: dict[str, Any],
                                   cancel: BracketCancel, last_price: float | None, bars) -> None:
        """Book what the resting children filled before a cancel landed.

        A stop that triggered after this cycle's fill reconcile has already
        sold those shares; exiting the full local quantity on top of it takes
        the position net short.
        """
        if cancel.filled_qty <= 0:
            return
        reason = cancel.fill_reason or "broker_stop"
        fill_price = cancel.fill_price
        if fill_price is None:
            level = bracket.get("stop_price") if reason == "broker_stop" else bracket.get("target_price")
            fill_price = safe_float(level, None) if level is not None else safe_float(last_price, None)
        if fill_price is None:
            LOG.error(
                "Bracket children for %s filled %s share(s) before the cancel but no fill price is available; "
                "booking at entry so the quantity is right", key, cancel.filled_qty,
            )
            fill_price = float(position.entry_price)
        LOG.log(TRADEFLOW_LEVEL, "Bracket %s filled for %s qty=%s before its cancel landed", reason, key, cancel.filled_qty)
        self._book_broker_exit(
            key, position, max(1, min(int(position.qty), int(cancel.filled_qty))), float(fill_price), reason, bars,
            result_message="bracket_child_filled_before_cancel", attempt_status="broker_bracket",
            fill_price_estimated=cancel.fill_price is None,
        )

    def _sync_bracket_children(self, key: str, position: Position, last_price: float | None, bars) -> None:
        """Push engine-side level changes onto the resting broker children.

        Only runs in ``replace`` sync mode. ``static`` deliberately leaves the
        entry-time levels alone, which is why config validation refuses to pair
        it with a management mode that ratchets stops.
        """
        bracket = active_broker_bracket(position)
        if bracket is None:
            return
        if str(bracket.get("sync_mode") or "static") != "replace":
            return
        symbol = str(position.metadata.get("underlying") or position.symbol)
        entry_price = float(position.entry_price)

        engine_target = safe_float(position.target_price, None)
        resting_target = safe_float(bracket.get("target_price"), None)
        if bracket.get("target_order_id") and engine_target is None and resting_target is not None:
            # The ladder cleared the target to run a runner. A resting target
            # limit would cap exactly the move the runner exists to capture, so
            # tear the OCO down and re-establish stop-only protection -- but
            # only once the old one is confirmed down, or the new stop rests
            # beside the old and both fill.
            cancel = self.executor.cancel_bracket(bracket)
            if not cancel.ok:
                LOG.warning(
                    "Could not cancel %s's resting target to run the runner (%s); the old bracket "
                    "still protects the position, retrying next cycle", key, cancel.message,
                )
                return
            self._book_bracket_cancel_fills(key, position, bracket, cancel, last_price, bars)
            if key not in self.positions:
                return
            replacement = self.executor.ensure_position_protected(
                symbol, int(position.qty), position.side, entry_price, float(position.stop_price), None,
            )
            if replacement is not None and isinstance(position.metadata, dict):
                position.metadata["bracket"] = replacement
            return

        for adjustment in self.executor.sync_bracket_levels(
            bracket, symbol, position.side, int(position.qty), entry_price, float(position.stop_price), engine_target,
        ):
            _append_adjustment(position.metadata, adjustment)

    # ------------------------------------------------------------------
    # Exit orders whose outcome the submit call could not settle.
    #
    # A MARKET exit that did not fill inside the poll window (a halt, an LULD
    # pause) is deliberately left working, and a cancel the broker never
    # confirmed may leave a limit live. Either can still fill. The order is
    # tracked on the position and settled from its OWN fill record each cycle
    # -- the broker's per-order fills, not a positions snapshot -- and nothing
    # else is sent for the position while it is live.
    # ------------------------------------------------------------------

    @staticmethod
    def _track_working_exit(position: Position, result, reason: str, booked_qty: int) -> None:
        if isinstance(position.metadata, dict):
            position.metadata["working_exit_order"] = {
                "order_id": str(result.order_id),
                "reason": str(reason),
                "message": str(result.message),
                "booked_qty": int(booked_qty),
                "since": now_et().isoformat(),
            }

    def _exit_order_in_flight(self, key: str, position: Position, last_price: float | None, bars,
                              order_states: dict[str, Any]) -> bool:
        """Settle an exit order an earlier cycle left working. True while it
        still is -- the caller must send nothing else for the position.

        Fills booked here are the order's fills beyond what was already
        booked from it, so a partial booked at submit time is not counted
        twice. A live LIMIT exit is re-cancelled each cycle so a fresh,
        repriced exit can follow; a live MARKET exit is the most aggressive
        order there is and is left to fill.
        """
        record = position.metadata.get("working_exit_order") if isinstance(position.metadata, dict) else None
        if not isinstance(record, dict):
            return False
        if "states" not in order_states:
            order_states["states"] = self.executor.fetch_order_states()
        states = order_states["states"]
        order_id = str(record.get("order_id") or "")
        state = states.get(order_id) if isinstance(states, dict) else None
        if state is None:
            state = self.executor.order_state(order_id)
        if state is None:
            self.audit.log_cycle(
                f"exit_gate:{key}", "exit_order_unresolved",
                f"Exit held {key}: state of working exit order {order_id} unavailable this cycle",
                interval=60.0, level=TRADEFLOW_LEVEL,
            )
            return True
        filled = int(state.get("filled_qty") or 0)
        booked = int(record.get("booked_qty") or 0)
        if filled > booked:
            slice_qty = max(1, min(int(position.qty), filled - booked))
            asset_type = str(position.metadata.get("asset_type") or ASSET_TYPE_EQUITY)
            broker_price = safe_float(state.get("fill_price"), None)
            if broker_price is not None and asset_type in OPTION_ASSET_TYPES:
                broker_price *= 100.0
            exit_price = broker_price if broker_price is not None else safe_float(last_price, None)
            if exit_price is None:
                self.audit.log_cycle(
                    f"exit_gate:{key}", "exit_order_fill_unpriced",
                    f"Exit held {key}: working exit order {order_id} filled {filled - booked} with no price yet",
                    interval=60.0, level=TRADEFLOW_LEVEL,
                )
                return True
            record["booked_qty"] = booked + slice_qty
            self._book_broker_exit(
                key, position, slice_qty, float(exit_price), str(record.get("reason") or "exit"), bars,
                result_message=f"working_exit_filled:{state.get('status')}", attempt_status="broker_working_exit",
                fill_price_estimated=broker_price is None,
            )
            if key not in self.positions:
                return True
        if state.get("is_filled") or state.get("is_terminal_failure"):
            position.metadata.pop("working_exit_order", None)
            self._save_reconcile_metadata()
            return False
        if str(state.get("order_type") or "") != "MARKET":
            cancel_ok, cancel_msg = self.executor.cancel_working_order(order_id)
            if not cancel_ok:
                LOG.warning("Working exit %s for %s still live; cancel unconfirmed (%s)", order_id, key, cancel_msg)
        self.audit.log_cycle(
            f"exit_gate:{key}", "exit_order_working",
            f"Exit held {key}: exit order {order_id} is still working at the broker",
            interval=60.0, level=TRADEFLOW_LEVEL,
        )
        return True

    # ------------------------------------------------------------------
    # Main entry point — runs every management cycle.
    # ------------------------------------------------------------------

    def manage_positions(self, _now: datetime, bars) -> None:
        # Book any exit the broker already executed BEFORE evaluating anything.
        # A filled resting child means the position is gone at the broker, and
        # managing or exiting a phantom position sends a duplicate order that
        # opens a new one in the opposite direction.
        self._reconcile_bracket_fills(bars)
        order_states: dict[str, Any] = {}  # account_orders, fetched once and only if needed
        for key, position in list(self.positions.items()):
            if key not in self.positions:
                continue
            last_price, market_snapshot = self._position_management_snapshot(position, bars)
            if self._exit_order_in_flight(key, position, last_price, bars, order_states):
                continue
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
                # Push any level the managers just moved onto the resting
                # broker children, before the exit decision below can cancel
                # them. No-op outside `replace` sync mode.
                self._sync_bracket_children(key, position, last_price, bars)
                if key not in self.positions:
                    continue
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
                # Force flatten guarantees the exit but must not RELABEL one
                # that a real level already triggered. This ran
                # unconditionally, so every stop or target that fired inside
                # the force-flatten window was recorded as "force_flatten" —
                # inflating that bucket and hollowing out `stop` / `target` in
                # the per_exit_reason table the tuning is read from. Either
                # branch leaves should_exit True, so the guarantee holds.
                if not should_exit:
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
            # The engine has decided to exit for a reason the broker cannot see
            # (peak giveback, time stop, CHoCH, force flatten). Tear down the
            # resting protection FIRST: leaving it live means the market-out and
            # the resting stop both fill, taking the strategy net short.
            open_bracket = active_broker_bracket(position)
            if open_bracket is not None:
                cancel = self.executor.cancel_bracket(open_bracket)
                if not cancel.ok:
                    # Defer rather than double-exit. The protective stop is still
                    # resting, so the position is not unguarded while we retry.
                    LOG.error(
                        "Could not cancel resting bracket for %s before %s exit (%s); "
                        "deferring exit to avoid a double fill", key, reason, cancel.message,
                    )
                    self.audit.log_structured("EXIT_CONTEXT", {
                        **exit_context, "symbol": key, "qty": int(position.qty),
                        "result_message": f"bracket_cancel_failed:{cancel.message}",
                        "attempt_status": "deferred_bracket_cancel_failed",
                    })
                    continue
                self._book_bracket_cancel_fills(key, position, open_bracket, cancel, last_price, bars)
                if key not in self.positions:
                    continue
            result = self.executor.close_position(position, data=self.data, market_snapshot=market_snapshot)
            if not result.ok:
                if result.order_id and (result.may_still_be_working or order_result_needs_broker_recheck(result.message)):
                    # The order reached the broker and its outcome is not
                    # settled: it may be live, or have filled shares the result
                    # does not report. Track it and settle from its own fill
                    # record next cycle; sending another exit meanwhile is how
                    # a halted market-out fills twice.
                    self._track_working_exit(position, result, reason, booked_qty=0)
                    self._save_reconcile_metadata()
                    LOG.log(TRADEFLOW_LEVEL, "Exit order %s for %s unsettled (%s); tracking it", result.order_id, key, result.message)
                    self.audit.log_structured("EXIT_CONTEXT", {**exit_context, "symbol": key, "qty": int(position.qty), "result_message": result.message, "attempt_status": "tracking_unsettled_order"})
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
                # The shares are GONE -- skipping the booking to "retry next
                # cycle" sent a second exit for them. Book at entry, flagged
                # estimated, so the quantity is right and P&L reads flat.
                LOG.error("Exit fill price unavailable for %s after a filled close_position(); booking at entry price (estimated)", key)
                exit_price_value = float(position.entry_price)
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
                    entry_price=float(position.entry_price),
                    exit_price=exit_price,
                    atr=exit_atr,
                )
                del self.positions[key]
                self._save_reconcile_metadata()
            else:
                self.risk.register_realized_pnl(realized)
                position.qty -= exit_qty
                if result.may_still_be_working:
                    # Partial fill whose cancel never confirmed: the remainder
                    # may still fill. Track it so this cycle's slice is not
                    # followed by a second exit for the same shares.
                    self._track_working_exit(position, result, reason, booked_qty=exit_qty)
                if isinstance(position.metadata, dict):
                    position.metadata["qty"] = position.qty
                    if open_bracket is not None and not result.may_still_be_working:
                        # The exit only partially filled and we cancelled the
                        # bracket to get here, so the remaining shares are now
                        # unprotected. Re-establish protection at the current
                        # engine levels, sized to what is left -- unless the
                        # exit's remainder may still be live, in which case a
                        # resting stop beside it would sell the same shares
                        # twice; the engine-side stop covers them meanwhile.
                        reprotected = self.executor.ensure_position_protected(
                            str(position.metadata.get("underlying") or position.symbol),
                            int(position.qty), position.side, float(position.entry_price),
                            float(position.stop_price), position.target_price,
                        )
                        if reprotected is not None:
                            position.metadata["bracket"] = reprotected
                self.positions[key] = position
                self._save_reconcile_metadata()

        self._save_reconcile_metadata()
