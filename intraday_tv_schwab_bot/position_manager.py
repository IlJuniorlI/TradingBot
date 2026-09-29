# SPDX-License-Identifier: MIT
"""PositionManager — owns the open-position management cycle.

Extracted from ``IntradayBot`` as Phase 5 Step 9 of the Phase 5 engine split.
Holds the logic that evaluates open positions each cycle: mark-price
resolution, diagnostic tracking, exit-signal evaluation, exit execution, and
broker exit recovery. The in-trade managers and the level check that move a
position's stop and target are ``TradeManager``'s (``trade_management.py``),
which ``manage_positions`` runs for each position.

Design notes:

- ``self.positions`` is a **shared-reference** dict with the owning
  ``IntradayBot``. Mutations here (pop on final exit, qty decrement on
  partial) are immediately visible to the engine's other methods.
- ``save_reconcile_metadata`` is injected as a callable because the engine
  also needs to call it from entry paths (``_open_positions``, broker
  entry recovery). Keeping a single save on the engine keeps one rule for
  how it writes (an upsert until a broker reconcile has succeeded, then a
  full replace); ``ReconcileMetadataStore.save_if_changed`` skips a save
  that would write what it last wrote.
- An exit order whose submit call could not settle it (left working through
  a halt, a cancel the broker never confirmed) is tracked on the position
  and settled from the order's own fill record (``_exit_order_in_flight``),
  never from a positions snapshot.
- Each position is managed on its own: an exception while managing one is
  logged against it, and escalated when it persists, and the others are
  managed as usual (``manage_positions``).
- The HTF timeframe / lookback of the trade manager's S/R reads are the
  strategy's (``strategy.htf_minutes()`` / ``htf_lookback_days()``), the one
  resolution the engine, the entry gatekeeper and the dashboard read too.
  HTF refresh cadence is now bar-aligned in ``MarketDataStore.should_refresh_htf_context``
  so there's no longer a refresh-seconds knob on the strategy side.
"""
from __future__ import annotations

import copy
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

import pandas as pd

from .audit_logger import AuditLogger, structured_metadata_snapshot
from .config import BotConfig
from .data_feed import EXECUTION_LAST_KEYS, MANAGEMENT_PRICE_KEYS, MarketDataStore
from .execution import BracketCancel, SchwabExecutor
from .models import (
    ASSET_TYPE_OPTION_SINGLE,
    ASSET_TYPE_OPTION_VERTICAL,
    ExitDecision,
    Position,
    Side,
    asset_type_of,
    is_option_asset,
)
from .numeric import first_float, safe_float
from .paper_account import PaperAccount
from .position_metrics import (
    LADDER_TOUCH_HOLD_KEY,
    STOP_SOURCE_KEY,
    append_management_adjustment,
    exit_reason_details,
    favorable_move,
    initial_risk_per_unit,
    position_return_pct_at_price,
    position_unrealized_at_price,
)
from .risk import RiskManager
from .sr_snapshot import sr_snapshot
from .trade_management import TradeManager
from .broker_payloads import (
    BRACKET_ID_KEYS,
    DISASTER_SYNC_MODE,
    active_broker_bracket,
    bracket_order_ids,
    is_disaster_stop,
    order_result_needs_broker_recheck,
    protective_stop_reason,
    sent_exit_stop,
    working_exit_orders,
    working_exit_outstanding_qty,
)
from .log_setup import TRADEFLOW_LEVEL, ComponentFailureLog
from . import sessions

if TYPE_CHECKING:
    from ._strategies.strategy_base import BaseStrategy

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")

# How often a bracket child the account_orders listing does not return is read
# on its own with order_details. The listing looks back 8 hours, and a child
# placed outside an order session (a restore before 07:00, a re-protect the
# evening before) is a NORMAL DAY order that can outlive it. One read a minute
# per such child keeps a few of them far inside Schwab's ~120 requests/minute.
UNLISTED_BRACKET_CHILD_READ_SECONDS = 60.0

# How long after an attempt at a position's disaster stop that did not leave
# it resting the next one waits (ensure_disaster_stop): after the broker
# refused it (a non-2xx status: nothing placed), one a minute per position
# keeps a stop the broker keeps refusing (a level the price is already
# through, a halted symbol) far inside Schwab's ~120 requests/minute, and
# every attempt logs; after a submit whose outcome is unknown, an order the
# broker did accept is listed long before the working orders are read to
# look for it.
DISASTER_STOP_RETRY_SECONDS = 60.0

# A position whose management raises is logged with its traceback on the first
# failure of a run of consecutive failed cycles and on every
# POSITION_ERROR_TRACEBACK_EVERY-th after, and as a one-line WARNING in
# between: the engine throttles its own cycle errors this way.
POSITION_ERROR_TRACEBACK_EVERY = 10


def _bar_time(frame: pd.DataFrame) -> pd.Timestamp | None:
    """The open time of ``frame``'s last bar in the exchange's time zone (a
    naive index is exchange time, as the feed's frames are), or None when the
    frame has no time index."""
    if not isinstance(frame.index, pd.DatetimeIndex) or pd.isna(frame.index[-1]):
        return None
    stamp = frame.index[-1]
    return stamp.tz_localize(sessions.EXCHANGE_TZ) if stamp.tzinfo is None else stamp.tz_convert(sessions.EXCHANGE_TZ)


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
        positions: dict[str, Position],
        save_reconcile_metadata: Callable[[], None],
    ) -> None:
        self.config = config
        self.data = data
        self.executor = executor
        self.risk = risk
        self.audit = audit
        self.account = account
        self.strategy = strategy
        # The in-trade managers and the level check, run for each position.
        self.trade_manager = TradeManager(config, data=data, strategy=strategy, audit=audit)
        # The exit record's S/R snapshot failures (_position_exit_context).
        self._log_component_failure = ComponentFailureLog(LOG)
        self.positions = positions
        self._save_reconcile_metadata = save_reconcile_metadata
        # Child id -> (monotonic time read, order state) for bracket children
        # the listing does not return; see _unlisted_bracket_child_state.
        self._unlisted_child_states: dict[str, tuple[float, dict[str, Any] | None]] = {}
        # Position key -> consecutive management cycles in which managing
        # that position raised; see _position_failed.
        self._failure_streaks: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Mark-price resolution for open positions.
    # ------------------------------------------------------------------

    def _position_management_snapshot(self, position: Position, bars) -> tuple[float | None, dict[str, Any] | None]:
        """The price this cycle manages ``position`` at, and where it came
        from. An equity's snapshot names the price's time as ``price_at``:
        the quote's ``fetched_at``, or the open time of the bar whose close
        it is (the adaptive ladder's touch hold attributes a touch to that
        1m bar); the account's cached price has none."""
        mark = self.strategy.position_mark_price(position, self.data)
        if mark is not None:
            price = float(mark)
            return price, None
        if is_option_asset(position.metadata):
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
                price = first_float(quote, *MANAGEMENT_PRICE_KEYS, positive=True)
                if price is not None:
                    market_snapshot = {
                        "bid": safe_float(quote.get("bid"), None),
                        "ask": safe_float(quote.get("ask"), None),
                        "last": first_float(quote, *EXECUTION_LAST_KEYS, positive=True),
                        "source": "quote",
                        "decision_price": price,
                        "price_at": quote.get("fetched_at"),
                    }
                    return price, market_snapshot
        frame = bars.get(position.symbol)
        if frame is not None and not frame.empty:
            price = float(frame.iloc[-1].close)
            return price, {"bid": price, "ask": price, "last": price, "source": "bar_close", "decision_price": price,
                           "price_at": _bar_time(frame)}
        underlying = position.metadata.get("underlying")
        if underlying:
            frame = bars.get(str(underlying))
            if frame is not None and not frame.empty:
                price = float(frame.iloc[-1].close)
                return price, {"bid": price, "ask": price, "last": price, "source": "underlying_bar_close",
                               "decision_price": price, "price_at": _bar_time(frame)}
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
        asset_type = asset_type_of(meta)
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
        ts = sessions.now_et().isoformat()
        meta['initial_qty'] = int(position.qty)
        meta['diag_best_unrealized_pnl_per_unit'] = 0.0
        meta['diag_worst_unrealized_pnl_per_unit'] = 0.0
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
        """Track the trade's best / worst excursion and the marks the exit
        side compares against.

        The excursion is tracked PER UNIT and reported in dollars at the
        trade's full size, ``initial_qty`` (the largest quantity it has held,
        so a late entry fill counts). Until 2026-09-24 it was dollars at the
        CURRENT quantity, so after a scale-out the peak was measured on a
        mix of sizes, while the session report divides it by the initial
        risk times the lifecycle's full quantity.
        """
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        unrealized = favorable_move(position.side, float(position.entry_price), float(mark_price))
        initial_qty = max(int(meta.get('initial_qty') or 0), int(position.qty))
        meta['initial_qty'] = initial_qty
        best = float(meta.get('diag_best_unrealized_pnl_per_unit', 0.0))
        worst = float(meta.get('diag_worst_unrealized_pnl_per_unit', 0.0))
        ts = sessions.now_et().isoformat()
        # The exit signal runs on the UNDERLYING's frame. For an option these
        # are what it compares against: the premium it is holding at (for R)
        # and how far the underlying itself has travelled since entry.
        meta['last_mark_price'] = float(mark_price)
        if underlying_price is not None:
            underlying = float(underlying_price)
            meta['underlying_high_since_entry'] = max(underlying, float(meta.get('underlying_high_since_entry', underlying)))
            meta['underlying_low_since_entry'] = min(underlying, float(meta.get('underlying_low_since_entry', underlying)))
        if unrealized > best:
            best = unrealized
            meta['diag_best_unrealized_pnl_per_unit'] = best
            meta['diag_best_unrealized_ts'] = ts
            meta['diag_best_mark_price'] = float(mark_price)
            if underlying_price is not None:
                meta['diag_best_underlying_price'] = float(underlying_price)
        if unrealized < worst:
            worst = unrealized
            meta['diag_worst_unrealized_pnl_per_unit'] = worst
            meta['diag_worst_unrealized_ts'] = ts
            meta['diag_worst_mark_price'] = float(mark_price)
            if underlying_price is not None:
                meta['diag_worst_underlying_price'] = float(underlying_price)
        meta['diag_best_unrealized_pnl'] = best * initial_qty
        meta['diag_worst_unrealized_pnl'] = worst * initial_qty

    @staticmethod
    def underlying_price_for_position(position: Position, bars, default: float | None = None) -> float | None:
        underlying = str(position.metadata.get('underlying') or position.symbol)
        frame = bars.get(underlying) if bars else None
        if frame is not None and not frame.empty:
            return float(frame.iloc[-1].close)
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
            'exit_time': sessions.now_et().isoformat(),
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
        last = frame.iloc[-1]
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

    def _position_exit_context(self, position: Position, decision: ExitDecision, mark_price: float | None, underlying_price: float | None, market_snapshot: dict[str, Any] | None, bars) -> dict[str, Any]:
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        entry_price = safe_float(position.entry_price, None)
        stop_price = safe_float(position.stop_price, None)
        target_price = safe_float(position.target_price, None)
        initial_stop_price = safe_float(meta.get('initial_stop_price'), stop_price, finite=True)
        initial_target_price = safe_float(meta.get('initial_target_price'), target_price)
        current_price = safe_float(mark_price, None)
        current_unrealized = position_unrealized_at_price(position, current_price)
        return_pct = position_return_pct_at_price(position, current_price)
        hold_minutes = max(0.0, (sessions.now_et() - position.entry_time).total_seconds() / 60.0)
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
        # The stop level and the peak in R from the entry, positive the
        # trade's way (2026-09-27): with stop_source they say what a stop exit
        # hit -- the profit lock's level, the trail's, the break-even -- and
        # how far the trade had run.
        stop_r = peak_r = None
        if entry_price is not None and initial_risk_per_unit is not None and initial_risk_per_unit > 0:
            peak_price = safe_float(position.highest_price if position.side == Side.LONG else position.lowest_price, None)
            if stop_price is not None:
                stop_r = favorable_move(position.side, entry_price, stop_price) / initial_risk_per_unit
            if peak_price is not None:
                peak_r = favorable_move(position.side, entry_price, peak_price) / initial_risk_per_unit
        management_symbol = str(meta.get('underlying') or position.symbol)
        management_frame = bars.get(management_symbol) if bars else None
        sr_fields = None
        if self.data is not None and management_symbol:
            try:
                sr_fields = sr_snapshot(
                    self.config,
                    self.data,
                    management_symbol,
                    price=underlying_price or current_price,
                    strategy=self.strategy,
                    account=self.account,
                    allow_refresh=False,
                )
            except Exception:
                # A diagnostic read for the exit record: it runs the S/R
                # build, and an exit is never held up by it.
                self._log_component_failure(
                    "exit_context_sr_row", "Exit-context S/R row failed for %s", management_symbol,
                )
                sr_fields = None
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
            'stop_source': meta.get(STOP_SOURCE_KEY) or 'initial',
            'stop_r': stop_r,
            'peak_r': peak_r,
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
            **exit_reason_details(decision),
            **self._exit_bar_snapshot(management_frame),
        }
        if sr_fields is not None:
            payload.update({
                'sr_timeframe': sr_fields.get('timeframe'),
                'sr_state': sr_fields.get('state'),
                'sr_trend_state': sr_fields.get('trend_state'),
                'sr_structure_bias': sr_fields.get('structure_bias'),
                'sr_structure_event': sr_fields.get('structure_event'),
                'sr_nearest_support': safe_float(sr_fields.get('nearest_support'), None),
                'sr_nearest_resistance': safe_float(sr_fields.get('nearest_resistance'), None),
                'sr_support_distance_pct': safe_float(sr_fields.get('support_distance_pct'), None),
                'sr_resistance_distance_pct': safe_float(sr_fields.get('resistance_distance_pct'), None),
                'sr_broken_support': safe_float(sr_fields.get('broken_support'), None),
                'sr_broken_resistance': safe_float(sr_fields.get('broken_resistance'), None),
            })
        extra = structured_metadata_snapshot(meta)
        payload.update({k: v for k, v in extra.items() if k not in payload and v is not None})
        return {k: v for k, v in payload.items() if v is not None}

    # ------------------------------------------------------------------
    # Broker-side bracket lifecycle
    #
    # With execution.bracket_orders_enabled the protective stop (and, in
    # stop_and_target leg mode, the target) rest AT THE BROKER. That moves
    # three responsibilities here: notice when a resting child filled, keep
    # the resting levels in step with the engine's in-trade management, and
    # cancel them before the engine markets out for its own reasons.
    # ------------------------------------------------------------------

    def _register_closed_position(self, key: str, position: Position, realized: float, exit_price: float,
                                  bars) -> None:
        """Hand a fully closed position to the risk manager: its realized
        P&L, the re-entry cooldown (keyed on the ORDER side) and the
        same-level retry block's record.

        The record is ``RiskManager.same_level_anchor`` of the position -- an
        option's UNDERLYING direction and price at entry, never its premium
        or order side (2026-09-25) -- scaled by the underlying's ATR at exit
        (the block zone is sized in ATR so cheap and high-priced names get
        proportional thresholds) and logged with the underlying's price at
        exit, so the record sits in one price space. No ATR yet (warm-up) or
        no readable level: the block skips this exit.
        """
        management_symbol = str(position.metadata.get("underlying") or position.symbol)
        frame = bars.get(management_symbol) if bars else None
        exit_atr = (
            safe_float(frame.iloc[-1].get("atr14"), None)
            if (frame is not None and not frame.empty and "atr14" in frame.columns)
            else None
        )
        level_exit = (
            self.underlying_price_for_position(position, bars) if is_option_asset(position.metadata)
            else float(exit_price)
        )
        self.risk.register_exit(
            management_symbol, realized, additional_symbol=key, side=position.side,
            level=RiskManager.same_level_anchor(position.side, position.metadata, position.entry_price),
            exit_price=level_exit, atr=exit_atr,
        )

    def _book_broker_exit(self, key: str, position: Position, exit_qty: int, exit_price: float,
                          decision: ExitDecision, bars, *, result_message: str, attempt_status: str,
                          fill_price_estimated: bool) -> float:
        """Record an exit the engine learned of from the BROKER -- a resting
        bracket child that filled (a ``risk`` exit), or an exit order that
        filled after its submit call returned (the decision it was sent for)
        -- mirroring the manage_positions tail. Returns the realized P&L of
        the slice."""
        reason = decision.reason
        exit_context = self._position_exit_context(position, decision, exit_price, exit_price, None, bars)
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
            self._register_closed_position(key, position, realized, float(exit_price), bars)
            self.positions.pop(key, None)
            self._sweep_exit_orders(key, position)
        else:
            self.risk.register_realized_pnl(realized)
            position.qty -= int(exit_qty)
            if isinstance(position.metadata, dict):
                position.metadata["qty"] = position.qty
            self.positions[key] = position
        self._save_reconcile_metadata()
        return float(realized)

    def _reconcile_bracket_fills(self, bars) -> dict[str, str]:
        """Book exits the broker already executed, before anything else runs.

        MUST run ahead of the management loop: a filled resting child means the
        position no longer exists at the broker, and managing or exiting a
        phantom position sends a duplicate order that opens a NEW position in
        the opposite direction.

        A child the 8-hour listing does not return is read on its own
        (``_unlisted_bracket_child_state``): a stop placed before 07:00 that
        filled after 15:00 was never booked. A stop that died at the broker
        without filling -- a DAY order EXPIRED at its session's end, one
        CANCELED in the app, a REJECTED one -- retires its bracket
        (``_retire_dead_bracket``): until then the bracket still read active,
        so RiskManager deferred the stop to an order that no longer rested
        and nothing protected the position (2026-09-25).

        Each position is booked on its own: one whose booking raises is
        logged against it (``_position_failed``) and the rest are still
        booked (2026-09-26). Returns those failures, by position key, for
        ``manage_positions``.
        """
        bracketed = {
            key: position for key, position in self.positions.items()
            if active_broker_bracket(position) is not None and not self._settle_pending(position)
        }
        if not bracketed:
            self._unlisted_child_states.clear()
            return {}
        tracked_ids: set[str] = set()
        for position in bracketed.values():
            tracked_ids |= bracket_order_ids(active_broker_bracket(position) or {})
        for child_id in [cid for cid in self._unlisted_child_states if cid not in tracked_ids]:
            del self._unlisted_child_states[child_id]
        states = self.executor.fetch_order_states()
        if states is None:
            # Could not read broker state. Do NOT assume "nothing filled" --
            # log loudly and leave positions untouched for the next cycle.
            LOG.warning(
                "Bracket reconcile could not read broker order state for %s position(s); "
                "engine state may be stale this cycle", len(bracketed),
            )
            return {}
        failures: dict[str, str] = {}
        for key, position in bracketed.items():
            try:
                self._reconcile_position_bracket(key, position, states, bars)
            except Exception as exc:
                # The position's fill may be half booked, so it is not managed
                # this cycle (see manage_positions); the others are.
                self._position_failed(key, "its bracket-fill booking", exc, failures, rest_runs=False)
        return failures

    def _reconcile_position_bracket(self, key: str, position: Position, states: dict[str, dict[str, Any]],
                                    bars) -> None:
        """Book what one position's resting bracket children filled, or retire
        a bracket whose protecting child died unfilled; see
        ``_reconcile_bracket_fills``."""
        bracket = active_broker_bracket(position)
        if bracket is None:
            return
        child_states: dict[str, dict[str, Any] | None] = {}
        for child_key, reason in (("stop_order_id", protective_stop_reason(bracket)), ("target_order_id", "broker_target")):
            child_id = bracket.get(child_key)
            if not child_id:
                continue
            state = states.get(str(child_id))
            if state is None:
                state = self._unlisted_bracket_child_state(str(child_id))
            child_states[child_key] = state
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
                key, position, exit_qty, float(fill_price), ExitDecision(reason, "risk"), bars,
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
        else:
            self._track_resting_levels(key, bracket, child_states)
            # The child that protects the position: its stop, or once an
            # unconfirmed retire dropped a dead stop, what is left of it.
            guard_key = "stop_order_id" if bracket.get("stop_order_id") else "target_order_id"
            self._retire_dead_bracket(key, position, bracket, guard_key, child_states.get(guard_key), bars)

    @staticmethod
    def _track_resting_levels(key: str, bracket: dict[str, Any],
                              child_states: dict[str, dict[str, Any] | None]) -> None:
        """Record the levels the broker's working children actually rest at.

        ``bracket['stop_price']`` and ``['target_price']`` are what the
        replace sync compares the engine's levels against (it replaces a
        child whose level differs) and what a fill reported without a price
        is booked at. Until 2026-09-28 only the bot's own writes set them (the
        entry, an adoption, a replace the broker acknowledged); each cycle's
        order state now does, for a child still working whose level reads as
        a finite number, and a level that differs from the recorded one is
        logged. A filled or dead child is left to the fill booking and the
        retire."""
        for child_key, level_key, state_key in (("stop_order_id", "stop_price", "stop_price"),
                                                ("target_order_id", "target_price", "price")):
            state = child_states.get(child_key)
            if not isinstance(state, dict) or state.get("is_filled") or state.get("is_terminal_failure"):
                continue
            resting = safe_float(state.get(state_key), None, finite=True)
            if resting is None:
                continue
            recorded = safe_float(bracket.get(level_key), None, finite=True)
            if recorded is not None and abs(resting - recorded) < 1e-9:
                continue
            LOG.warning("Bracket %s %s for %s rests at %s at the broker, not the recorded %s; recording the broker's",
                        level_key.removesuffix("_price"), bracket.get(child_key), key, resting, recorded)
            bracket[level_key] = resting

    _DEAD_STOP_STATUSES = frozenset({"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"})

    @staticmethod
    def _settle_pending(position: Position) -> bool:
        """The broker reconcile found the broker holding less than this
        position tracks but could not confirm its bracket down (or read the
        order list its re-protect needs), so the position is held as it was.
        Until its retry settles it, nothing is sent for it: an exit or a
        re-protect would be sized to shares the broker no longer holds
        (2026-09-25)."""
        return bool(position.metadata.get("settle_pending")) if isinstance(position.metadata, dict) else False

    def _unlisted_bracket_child_state(self, child_id: str) -> dict[str, Any] | None:
        """``order_details`` state of a bracket child the account_orders
        listing does not return, read at most once per
        UNLISTED_BRACKET_CHILD_READ_SECONDS; a final state (filled, or
        terminal) is kept for as long as the child is tracked. None when it
        cannot be read."""
        now = time.monotonic()
        cached = self._unlisted_child_states.get(child_id)
        if cached is not None:
            read_at, state = cached
            if state is not None and (state.get("is_filled") or state.get("is_terminal_failure")):
                return state
            if now - read_at < UNLISTED_BRACKET_CHILD_READ_SECONDS:
                return state
        state = self.executor.order_state(child_id)
        self._unlisted_child_states[child_id] = (now, state)
        return state

    def _retire_dead_bracket(self, key: str, position: Position, bracket: dict[str, Any],
                             child_key: str, child_state: dict[str, Any] | None, bars) -> None:
        """Take down a bracket whose protecting child (``child_key``: its
        stop, or its target once an unconfirmed retire dropped the dead stop)
        died at the broker without filling, and hand the position a fresh
        stop, or the engine's.

        A REPLACED stop is not dead: its replacement may rest under an id the
        bracket could not be re-pointed to. What else of the bracket still
        rests (a target the OCO no longer links) is cancelled first, and the
        fills its cancel reports are booked, so the fresh protection covers
        only what is held. A cancel that cannot be confirmed keeps the rest
        tracked and drops only the dead child; the dead stop's own fills are
        booked then (and recorded, so a later cancel of the wrapper that still
        lists it does not report them again), and the rest's once, when their
        cancel confirms or they fill. A stop is re-placed at most once per bracket, and never after a
        REJECTED one (2026-09-25)."""
        status = str((child_state or {}).get("status") or "")
        if status not in self._DEAD_STOP_STATUSES:
            return
        dead_id = str(bracket.get(child_key) or "")
        child = "stop" if child_key == "stop_order_id" else "target"
        LOG.warning("Bracket %s %s for %s is %s at the broker", child, dead_id, key, status)
        leftover = self.executor.cancel_bracket(bracket)
        if leftover.ok:
            if leftover.filled_qty > 0:
                self.book_bracket_cancel_fills(key, position, bracket, leftover, None, bars)
        else:
            # The rest of it (a target the OCO no longer links) may still
            # rest: it stays tracked, so its fill is still booked and the
            # cancel-before-exit still sends its cancel, and only the dead
            # child is dropped, so the engine owns the stop at once. Only that
            # child's fills are booked now: a later cancel or fill of what
            # stays tracked reports its fills again, cumulatively.
            dead_filled = int((child_state or {}).get("filled_qty") or 0)
            if dead_filled > 0:
                self.book_bracket_cancel_fills(
                    key, position, bracket,
                    BracketCancel(False, leftover.message, dead_filled, safe_float(child_state.get("fill_price"), None),
                                  protective_stop_reason(bracket) if child == "stop" else "broker_target"),
                    None, bars,
                )
                # The wrapper still lists the dead child; a later cancel of it
                # must not report these again.
                bracket.setdefault("booked_child_fills", {})[dead_id] = dead_filled
            bracket[child_key] = None
            bracket["child_order_ids"] = [oid for oid in bracket.get("child_order_ids") or [] if str(oid) != dead_id]
            if child == "stop":
                bracket["dead_stop_status"] = status
            if bracket.get("stop_order_id") or bracket.get("target_order_id"):
                bracket["state"] = f"{child}_{status.lower()}_cancel_unconfirmed"
                LOG.error(
                    "Could not confirm the rest of %s's bracket is down after its %s was %s (%s) -- "
                    "the engine owns the stop, and the leftovers stay tracked", key, child, status.lower(), leftover.message,
                )
                self._save_reconcile_metadata()
                return
        # Nothing of the bracket rests any more.
        bracket["active"] = False
        bracket["state"] = f"{child}_{status.lower()}"
        # A rejected stop is the broker's verdict on the order itself (a STOP
        # outside the regular session, a stop through the market): placing it
        # again is rejected again, every cycle, and each rejected replacement
        # read as protection that suppressed the engine stop. The same goes
        # for protection this retire already replaced once. The engine owns
        # the stop instead.
        dead_stop = status if child == "stop" else bracket.get("dead_stop_status")
        if (key in self.positions and dead_stop and dead_stop != "REJECTED"
                and not bracket.get("replaces_dead_stop")):
            self._reprotect_beside_working_order(position, working_exit_outstanding_qty(position))
            # Marked whether the fresh stop rests or is still owed: a
            # disaster stop owed the regular session, or refused by the
            # broker, carries it to the stop ensure_disaster_stop places, so
            # that one is never re-placed after it dies in turn.
            fresh = position.metadata.get("bracket") if isinstance(position.metadata, dict) else None
            if isinstance(fresh, dict) and fresh is not bracket:
                fresh["replaces_dead_stop"] = dead_stop
        self._save_reconcile_metadata()

    def book_bracket_cancel_fills(self, key: str, position: Position, bracket: dict[str, Any],
                                   cancel: BracketCancel, last_price: float | None, bars) -> None:
        """Book what the resting children filled before a cancel landed.

        A stop that triggered after this cycle's fill reconcile has already
        sold those shares; exiting the full local quantity on top of it takes
        the position net short. The startup reconciler's settle books a
        cancel's fills through here too, at the broker's price and with the
        risk manager's registration (2026-09-25).
        """
        if cancel.filled_qty <= 0:
            return
        reason = cancel.fill_reason or protective_stop_reason(bracket)
        fill_price = cancel.fill_price
        if fill_price is None:
            level = bracket.get("target_price") if reason == "broker_target" else bracket.get("stop_price")
            fill_price = safe_float(level, None) if level is not None else safe_float(last_price, None)
        if fill_price is None:
            LOG.error(
                "Bracket children for %s filled %s share(s) before the cancel but no fill price is available; "
                "booking at entry so the quantity is right", key, cancel.filled_qty,
            )
            fill_price = float(position.entry_price)
        LOG.log(TRADEFLOW_LEVEL, "Bracket %s filled for %s qty=%s before its cancel landed", reason, key, cancel.filled_qty)
        self._book_broker_exit(
            key, position, max(1, min(int(position.qty), int(cancel.filled_qty))), float(fill_price),
            ExitDecision(reason, "risk"), bars,
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
        initial_risk = initial_risk_per_unit(position)

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
            self.book_bracket_cancel_fills(key, position, bracket, cancel, last_price, bars)
            if key not in self.positions:
                return
            replacement = self.executor.ensure_position_protected(
                symbol, int(position.qty), position.side, float(position.stop_price), None, initial_risk=initial_risk,
            )
            if replacement is not None and isinstance(position.metadata, dict):
                position.metadata["bracket"] = replacement
            return

        for adjustment in self.executor.sync_bracket_levels(
            bracket, symbol, position.side, int(position.qty), float(position.stop_price), engine_target,
            initial_risk=initial_risk,
        ):
            append_management_adjustment(position.metadata, adjustment)

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
    def _track_working_exit(position: Position, result, decision: ExitDecision, booked_qty: int, requested_qty: int,
                            *, bracket_cancelled: bool) -> None:
        if isinstance(position.metadata, dict):
            position.metadata["working_exit_order"] = {
                "order_id": str(result.order_id),
                "reason": str(decision.reason),
                "message": str(result.message),
                "booked_qty": int(booked_qty),
                "requested_qty": int(requested_qty),
                # A scale-out's one-shot marker rides with the order, so the
                # cycle that books its fill records it (see _record_exit_marker).
                "family": str(decision.family),
                "marker": decision.marker,
                # The bracket came down to send this order: the cycle that
                # settles it re-protects whatever is still held (see
                # _exit_order_in_flight). A full exit too: when it dies part
                # filled, its remainder is only retried if the family that
                # decided it fires again, and a chart or CHoCH trigger often
                # does not (2026-09-24). A retry cancels that bracket again,
                # which is churn, never a double fill.
                "reprotect": bool(bracket_cancelled),
                "since": sessions.now_et().isoformat(),
            }

    @staticmethod
    def _broker_bracket_down(position: Position) -> bool:
        """True when the position tracks a live broker bracket that is not
        active: cancelled to send an exit, or never established. Its
        remaining shares have no resting stop until they are re-protected.
        False for a dry-run (simulated) bracket and for a position that has
        none (bracket mode off, an option)."""
        bracket = position.metadata.get("bracket") if isinstance(position.metadata, dict) else None
        # An unconfirmed disaster stop may rest: one placed beside it would
        # sell the shares twice, so only ensure_disaster_stop places after
        # it, once it has looked for it.
        return (isinstance(bracket, dict) and not bracket.get("simulated") and not bracket.get("active")
                and bracket.get("state") != "unconfirmed")

    def _reprotect_remainder(self, position: Position, qty: int | None = None) -> None:
        """Resting broker protection, at the current engine levels (a
        disaster stop at the position's disaster price, never the engine's
        moved stop: ``resting_stop_price``), for the ``qty`` shares of a
        position no exit order covers (default: all of it) after its bracket
        was cancelled to send an exit.

        The bracket the position still tracks goes along as
        ``known_bracket``: a live one -- the remainder bracket placed beside
        a working slice, here or by a startup restore (which sizes it the
        same way since 2026-09-25), or a full-size one a resize could not
        shrink -- is adopted and resized to ``qty``. Submitting fresh beside
        it orphaned the old OCO, and both sold when the stop traded
        (2026-09-24)."""
        reprotected = self.executor.ensure_position_protected(
            str(position.metadata.get("underlying") or position.symbol),
            int(position.qty) if qty is None else int(qty), position.side,
            self.executor.resting_stop_price(position), position.target_price,
            initial_risk=initial_risk_per_unit(position),
            known_bracket=active_broker_bracket(position),
        )
        if reprotected is not None:
            position.metadata["bracket"] = reprotected

    def _reprotect_beside_working_order(self, position: Position, outstanding_qty: int) -> None:
        """Re-protect the shares a working exit order does not cover.

        While a scale-out slice works the position is skipped (see
        _exit_order_in_flight), so the shares outside it had neither the
        bracket, cancelled to send the slice, nor an engine stop evaluation
        (2026-09-24). The resting stop covers only ``position.qty -
        outstanding_qty``, so it and the order never sell the same shares.
        A full exit covers every share and gets nothing here."""
        uncovered = int(position.qty) - int(outstanding_qty)
        if uncovered > 0:
            self._reprotect_remainder(position, uncovered)

    # The states of a disaster stop's record that ensure_disaster_stop places
    # afresh: owed the regular session (Schwab rejects a STOP outside it),
    # refused by the broker, and unconfirmed (looked for first); the last two
    # after DISASTER_STOP_RETRY_SECONDS.
    _DISASTER_STOP_OWED_STATES = frozenset({"pending_session", "unprotected", "unconfirmed"})
    _DISASTER_STOP_RETRY_STATES = frozenset({"unprotected", "unconfirmed"})

    def ensure_disaster_stop(self, key: str, position: Position) -> None:
        """Rest an equity position's disaster stop at the broker when it is
        owed one (``execution.disaster_stop_enabled``): right after its entry
        fills (the entry gatekeeper), and on each cycle that decides no exit
        for it (``_manage_position``).

        Owed: a position with no record yet; one whose stop waits for the
        regular session (a premarket entry gets it on the first
        regular-session cycle); one whose stop the broker refused; and one
        whose submit had an unknown outcome, or whose restore could not read
        it (``unconfirmed``). The last two wait ``DISASTER_STOP_RETRY_SECONDS``
        after the attempt (``attempted_at``). An unconfirmed one is looked for
        first among the day's working orders (``broker_payloads.
        sent_exit_stop``): the exit STOP resting at exactly the price and
        quantity sent is adopted; beside any other exit stop on the symbol
        (one placed by hand, or one it cannot tell for its own) none is
        adopted and none placed; and nothing is placed until the orders can
        be read. Not owed: a stop resting, a dry run's (simulated: nothing is
        placed), one the cancel before an engine exit took down (that exit's
        own path re-protects what is left), and one that died at the broker
        (the fill reconcile's retire re-places it once, and never after a
        REJECTED one; the mark that retire leaves on the stop it owes is
        carried to the one placed here).

        Before anything is sent the record reads ``unconfirmed`` at the price
        and quantity about to be sent, and is saved. Whatever raises after
        that (here, or in the entry pass that called it) leaves a stop the
        next attempt looks for before it places another: an order write is
        never assumed not to have landed, and a position never gets two
        resting stops.

        Each attempt that leaves the position without its stop in the
        regular session (refused, unknown outcome, the orders unreadable, a
        stop it cannot tell for its own) counts as a miss
        (``failed_attempts`` on the record); every
        ``execution.disaster_stop_escalation_attempts``-th consecutive one
        is logged at CRITICAL (``_escalate_disaster_stop``)."""
        if not isinstance(position.metadata, dict) or is_option_asset(position.metadata) \
                or not self.executor.disaster_stop_enabled():
            return
        record = position.metadata.get("bracket")
        state = record.get("state") if isinstance(record, dict) else None
        if isinstance(record, dict) and state not in self._DISASTER_STOP_OWED_STATES:
            return
        now = sessions.now_et()
        if state in self._DISASTER_STOP_RETRY_STATES and record.get("attempted_at") is not None \
                and (now - datetime.fromisoformat(str(record["attempted_at"]))).total_seconds() \
                < DISASTER_STOP_RETRY_SECONDS:
            return
        missed = int(record.get("failed_attempts") or 0) if isinstance(record, dict) else 0
        known: dict[str, Any] | None = None
        if state == "unconfirmed":
            orders = self._todays_working_orders(now)
            if orders is None:
                self._disaster_stop_missed(
                    key, record, missed + 1, now, logging.WARNING,
                    "the working orders could not be read to look for its unconfirmed stop; none is placed "
                    "until they are",
                )
                return
            known, others = sent_exit_stop(orders, position.symbol, position.side,
                                           stop_price=record.get("stop_price"), qty=record.get("qty"))
            if known is None and others:
                self._disaster_stop_missed(
                    key, record, missed + 1, now, logging.ERROR,
                    f"exit stop(s) {', '.join(str(order['orderId']) for order in others)} rest on "
                    f"{position.symbol}, none of them the {record.get('qty')} shares at {record.get('stop_price')} "
                    "its unconfirmed stop sent; none is adopted and none placed beside them",
                )
                return
        level = self.executor.protective_stop_level(position.side, self.executor.resting_stop_price(position))
        carried = {mark: record[mark] for mark in ("replaces_dead_stop",)
                   if isinstance(record, dict) and record.get(mark)}
        position.metadata["bracket"] = {
            "parent_order_id": None, "sync_mode": DISASTER_SYNC_MODE, "legs": "stop_only", "session": "NORMAL",
            "stop_price": level, "target_price": None, "qty": int(position.qty),
            **dict.fromkeys(BRACKET_ID_KEYS), "child_order_ids": [],
            "active": False, "state": "unconfirmed", "attempted_at": now.isoformat(),
            "failed_attempts": missed + 1, **carried,
        }
        self._save_reconcile_metadata()
        fresh = self.executor.ensure_position_protected(
            str(position.metadata.get("underlying") or position.symbol), int(position.qty), position.side,
            self.executor.resting_stop_price(position), None, initial_risk=None, known_bracket=known,
        )
        if isinstance(fresh, dict):
            fresh.update(carried)
            if fresh.get("state") in self._DISASTER_STOP_RETRY_STATES:
                fresh["failed_attempts"] = missed + 1
                self._escalate_disaster_stop(key, missed + 1, f"its last attempt ended {fresh['state']}")
            elif fresh.get("state") == "pending_session" and missed:
                fresh["failed_attempts"] = missed
        position.metadata["bracket"] = fresh
        self._save_reconcile_metadata()

    def _todays_working_orders(self, now: datetime) -> list[dict[str, Any]] | None:
        """The account's working orders entered since midnight ET
        (``SchwabExecutor.fetch_working_orders``), or None when they could not
        be read. A DAY order entered before today no longer works."""
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.executor.fetch_working_orders(
            *(stamp.astimezone(sessions.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
              for stamp in (day_start, now)),
        )

    def _disaster_stop_missed(self, key: str, record: dict[str, Any], missed: int, now: datetime, level: int,
                              detail: str) -> None:
        """An unconfirmed disaster stop's lookup that placed nothing: log
        *detail*, count the miss on *record*, and wait the retry interval."""
        record["attempted_at"] = now.isoformat()
        record["failed_attempts"] = missed
        LOG.log(level, "Disaster stop for %s: %s", key, detail)
        self._escalate_disaster_stop(key, missed, detail)
        self._save_reconcile_metadata()

    def _escalate_disaster_stop(self, key: str, count: int, detail: str) -> None:
        """Log at CRITICAL when *key* has gone *count* consecutive attempts
        without its disaster stop resting where it should (an owed stop not
        placed, or a cancel before an exit not confirmed, which defers the
        exit): on every ``execution.disaster_stop_escalation_attempts``-th.
        Each miss is logged on its own line too; this is the one to page
        on."""
        if count % int(self.config.execution.disaster_stop_escalation_attempts) == 0:
            LOG.critical("DISASTER STOP DEGRADED — %s: %d consecutive attempts missed. Last: %s", key, count, detail)

    def _sweep_exit_orders(self, key: str, position: Position) -> None:
        """After a full close of a position that had live broker protection,
        cancel the exit orders still working on its symbol for its side.

        Once the position is gone, a SELL (for a LONG) or BUY_TO_COVER (for a
        SHORT) still working at the broker opens a position the bot does not
        track when it fills. The bot's own records cannot see three: a stop
        whose submit had an unknown outcome and landed, if the position
        closed in full before the lookup found it; a replacement a replace
        left untracked (``new_id_unknown``); and a stop moved in the app.
        One read of the day's working orders per such close; a dry run and a
        position that had no broker protection (no record, or a simulated
        one) read nothing. The account's positions in a symbol the bot
        trades are taken as the bot's own, as the startup reconcile takes
        them."""
        record = position.metadata.get("bracket") if isinstance(position.metadata, dict) else None
        if (self.config.schwab.dry_run or not isinstance(record, dict) or record.get("simulated")
                or is_option_asset(position.metadata)):
            return
        symbol = str(position.metadata.get("underlying") or position.symbol)
        orders = self._todays_working_orders(sessions.now_et())
        if orders is None:
            LOG.error("The working orders could not be read after %s closed; an exit order still working on %s "
                      "is not cancelled and would open a position the bot does not track", key, symbol)
            return
        left = working_exit_orders(orders, symbol, position.side)
        if not left:
            return
        ids = [str(order["orderId"]) for order in left]
        LOG.warning("%s closed with exit order(s) %s still working on %s; cancelling them", key, ", ".join(
            f"{order['orderId']} ({order.get('orderType')} {order.get('quantity')} at "
            f"{order.get('stopPrice')})" for order in left), symbol)
        cancel = self.executor.cancel_bracket({"child_order_ids": ids})
        if not cancel.ok or cancel.filled_qty > 0:
            LOG.critical(
                "EXIT ORDERS LEFT — %s closed, and its leftover exit order(s) %s on %s %s: %s. Check the account "
                "for a position the bot does not track.", key, ", ".join(ids), symbol,
                f"filled {cancel.filled_qty} share(s) before the cancel" if cancel.filled_qty > 0
                else "could not be confirmed cancelled", cancel.message,
            )

    @staticmethod
    def _record_exit_marker(position: Position, family: str, marker: dict[str, Any] | None, status: str) -> None:
        """Append a one-shot exit marker to ``metadata['<family>_exits']``.

        The exit policy skips any trigger already recorded there, so a
        scale-out fires once per trigger rather than on every cycle the
        trigger stays live. ``status`` is ``booked`` once a slice filled, or
        ``below_one_unit`` when the slice rounded to nothing and the trigger
        is spent without an order. Persisted with the rest of the metadata.
        """
        if marker is None or not isinstance(position.metadata, dict):
            return
        markers = position.metadata.setdefault(f"{family}_exits", [])
        if any(isinstance(m, dict) and all(m.get(k) == v for k, v in marker.items()) for m in markers):
            return
        markers.append({**marker, "status": status, "at": sessions.now_et().isoformat()})

    def _exit_order_in_flight(self, key: str, position: Position, last_price: float | None, bars,
                              order_states: dict[str, Any]) -> bool:
        """Settle an exit order an earlier cycle left working. True while it
        still is -- the caller must send nothing else for the position.

        Fills booked here are the order's fills beyond what was already
        booked from it, so a partial booked at submit time is not counted
        twice. A live LIMIT exit is re-cancelled each cycle so a fresh,
        repriced exit can follow; a live MARKET exit is the most aggressive
        order there is and is left to fill -- unless the shares a working
        scale-out slice does not cover hit a risk exit
        (_working_slice_remainder_exit), which cancels it so the full exit
        can follow once it settles.
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
            if self._working_slice_remainder_exit(key, position, record, last_price):
                self._cancel_working_exit(key, order_id)
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
            broker_price = safe_float(state.get("fill_price"), None)
            if broker_price is not None and is_option_asset(position.metadata):
                broker_price *= 100.0
            exit_price = broker_price if broker_price is not None else safe_float(last_price, None)
            if exit_price is None:
                self.audit.log_cycle(
                    f"exit_gate:{key}", "exit_order_fill_unpriced",
                    f"Exit held {key}: working exit order {order_id} filled {filled - booked} with no price yet",
                    interval=60.0, level=TRADEFLOW_LEVEL,
                )
                return True
            # _track_working_exit, the record's only writer, stores the
            # reason and family the order was sent for. Read before the fill
            # is marked booked: a record without them raises every cycle with
            # the fill still unbooked, rather than once with it marked booked,
            # after which no cycle would ever book it.
            decision = ExitDecision(record["reason"], record["family"])
            record["booked_qty"] = booked + slice_qty
            self._book_broker_exit(
                key, position, slice_qty, float(exit_price), decision, bars,
                result_message=f"working_exit_filled:{state.get('status')}", attempt_status="broker_working_exit",
                fill_price_estimated=broker_price is None,
            )
            if key not in self.positions:
                # A bracket still resting beside the order that closed the
                # position (a remainder stop beside a slice) opens a new
                # position in the opposite direction when it triggers; the
                # fill reconcile's own close takes its leftovers down the same
                # way (2026-09-25).
                leftover_bracket = active_broker_bracket(position)
                if leftover_bracket is not None:
                    leftover = self.executor.cancel_bracket(leftover_bracket)
                    if not leftover.ok:
                        LOG.error(
                            "Could not confirm %s's bracket is down after its working exit order closed it (%s) -- "
                            "check the broker for a resting order on a closed position",
                            key, leftover.message,
                        )
                return True
            self._record_exit_marker(position, decision.family, record.get("marker"), "booked")
        if state.get("is_filled") or state.get("is_terminal_failure"):
            position.metadata.pop("working_exit_order", None)
            if record.get("reprotect"):
                # The order is settled and the position is still held. The
                # submit-time path re-protects a booked exit's remainder;
                # this is that step for an order that was left working, whose
                # remainder otherwise kept no broker bracket for the rest of
                # the trade (2026-09-24). A bracket placed beside a working
                # slice is adopted and resized to what is held.
                self._reprotect_remainder(position)
            self._save_reconcile_metadata()
            return False
        remainder_exit = self._working_slice_remainder_exit(key, position, record, last_price)
        if remainder_exit or str(state.get("order_type") or "") != "MARKET":
            self._cancel_working_exit(key, order_id)
        self.audit.log_cycle(
            f"exit_gate:{key}", "exit_order_working",
            f"Exit held {key}: exit order {order_id} is still working at the broker",
            interval=60.0, level=TRADEFLOW_LEVEL,
        )
        return True

    def _cancel_working_exit(self, key: str, order_id: str) -> None:
        cancel_ok, cancel_msg = self.executor.cancel_working_order(order_id)
        if not cancel_ok:
            LOG.warning("Working exit %s for %s still live; cancel unconfirmed (%s)", order_id, key, cancel_msg)

    def _working_slice_remainder_exit(self, key: str, position: Position, record: dict[str, Any],
                                      last_price: float | None) -> bool:
        """The engine's risk check on the shares a working scale-out slice
        does not cover. True when it wants the position out.

        The position is otherwise skipped while its slice works, so those
        shares' stop went unevaluated for as long as the order stayed live
        -- a halt, a LIMIT slice whose cancel keeps failing, an order-state
        lookup that keeps failing (2026-09-24). The caller cancels the slice;
        the cycle that settles it sends the full exit. A full exit's order
        covers every share and is not checked. In bracket mode the resting
        stop placed beside the slice (or a disaster stop, beyond the
        engine's stop) may fill first; the reconcile books it, and the stop
        exit that follows cancels what still rests (since 2026-09-28 the risk
        check no longer leaves the stop to the broker, see
        TradeManager.update_position)."""
        outstanding = working_exit_outstanding_qty(position)
        if last_price is None or outstanding >= int(position.qty):
            return False
        risk_exit, risk_reason = self.trade_manager.update_position(position, float(last_price))
        if not risk_exit:
            return False
        LOG.log(TRADEFLOW_LEVEL, "%s: %s on the %s share(s) outside working exit %s; cancelling it",
                key, risk_reason, int(position.qty) - outstanding, record.get("order_id"))
        self.audit.log_cycle(
            f"exit_gate:{key}", f"working_slice_cancelled:{risk_reason}",
            f"Exit {key}: {risk_reason} on the remainder; cancelling working exit {record.get('order_id')} "
            "so the full exit follows once it settles",
            interval=60.0, level=TRADEFLOW_LEVEL,
        )
        return True

    # ------------------------------------------------------------------
    # Main entry point — runs every management cycle.
    # ------------------------------------------------------------------

    def manage_positions(self, _now: datetime, bars) -> None:
        """Manage every open position once: book what the broker filled, then
        run each position's managers, risk check and exits.

        Each position is isolated (2026-09-26). An exception while managing
        one used to raise out of here, so every position after it went
        unmanaged that cycle, its stop and target included, and the engine's
        error backoff then slowed every position's next cycles. Now it is
        logged against that position (``_position_failed``) and the others
        are managed as usual. What the failing position still gets:

        - A step that only moves its levels or proposes an exit (the sr_flip
          manager, the adaptive ladder, the exit policy with the shared exits
          and the strategy's own) is skipped this cycle, and the rest of its
          management runs: the risk check (``TradeManager.update_position``)
          on the levels as the failed step left them, so its stop and target
          still fire (a touch hold left by a failed ladder pass is dropped,
          since it keeps the target off), then force flatten and the exit
          order.
        - Anywhere else (its bracket-fill booking, the working-exit
          settlement, the risk check itself, the bracket sync, the exit order
          and its booking) its cycle ends where it failed. Those steps can
          leave its orders in a state this cycle cannot know, and a second
          attempt risks a double exit. It is retried next cycle, and a
          resting broker stop still protects it.

        A position whose management fails ``runtime.error_escalation_cycles``
        cycles in a row escalates to a CRITICAL naming it
        (``_escalate_failures``). A stop signal (KeyboardInterrupt) is not an
        Exception, so it still stops the cycle and reaches the engine.

        The pass takes one re-price deadline as it starts
        (``executor.exit_reprice_deadline``) and every exit it sends shares
        it: a live LIMIT exit that misses is re-sent only until then, so the
        wait the re-sends add to the pass is bounded whatever the number of
        positions that miss together (2026-09-28).
        """
        reprice_deadline = self.executor.exit_reprice_deadline()
        # Book any exit the broker already executed BEFORE evaluating anything.
        # A filled resting child means the position is gone at the broker, and
        # managing or exiting a phantom position sends a duplicate order that
        # opens a new one in the opposite direction. A position whose booking
        # failed may be such a phantom, so it is left alone this cycle.
        failures = self._reconcile_bracket_fills(bars)
        order_states: dict[str, Any] = {}  # account_orders, fetched once and only if needed
        for key, position in list(self.positions.items()):
            if key not in self.positions or key in failures:
                continue
            try:
                self._manage_position(_now, key, position, bars, order_states, failures, reprice_deadline)
            except Exception as exc:
                self._position_failed(key, "its management", exc, failures, rest_runs=False)
        self._escalate_failures(failures)
        self._save_reconcile_metadata()

    def _position_failed(self, key: str, step: str, exc: Exception, failures: dict[str, str], *,
                         rest_runs: bool) -> None:
        """Log that ``step`` raised while managing ``key``, and record it in
        ``failures`` for this cycle's escalation. ``rest_runs``: the rest of
        the position's management still runs this cycle (a failed manager or
        exit policy), rather than its cycle ending here.

        The traceback is logged on the first failed cycle of a run and every
        POSITION_ERROR_TRACEBACK_EVERY-th after, and a one-line WARNING in
        between, so a position that fails every cycle cannot flood the log.
        """
        streak = self._failure_streaks.get(key, 0) + 1
        lines = str(exc).splitlines()
        error = f"{type(exc).__name__}: {lines[0]}" if lines else type(exc).__name__
        outcome = "the rest of its management still runs" if rest_runs else "it is not managed further this cycle"
        failures[key] = f"{step} raised {error}; {outcome}"
        if streak == 1 or streak % POSITION_ERROR_TRACEBACK_EVERY == 0:
            LOG.error("Managing %s failed (consecutive=%d): %s", key, streak, failures[key], exc_info=exc)
        else:
            LOG.warning("Managing %s failed (consecutive=%d): %s", key, streak, failures[key])

    def _escalate_failures(self, failures: dict[str, str]) -> None:
        """Count each position's consecutive failed cycles, and escalate one
        that reaches ``runtime.error_escalation_cycles`` to a CRITICAL naming
        it, on that cycle and every multiple after (as the engine's ENGINE
        DEGRADED does); 0 disables it. A position managed without an error
        this cycle, or no longer held, starts over."""
        self._failure_streaks = {
            key: self._failure_streaks.get(key, 0) + 1 for key in failures if key in self.positions
        }
        threshold = self.config.runtime.error_escalation_cycles
        if threshold <= 0:
            return
        for key, streak in sorted(self._failure_streaks.items()):
            if streak % threshold == 0:
                LOG.critical("POSITION DEGRADED — %s: its management failed %d consecutive cycles. Last: %s",
                             key, streak, failures[key])

    def _manage_position(self, now: datetime, key: str, position: Position, bars, order_states: dict[str, Any],
                         failures: dict[str, str], reprice_deadline: float) -> None:
        """One position's management cycle; see ``manage_positions``. It
        returns where the position's cycle ends. ``reprice_deadline`` is the
        pass's, for the exit it sends."""
        if self._settle_pending(position):
            self.audit.log_cycle(
                f"exit_gate:{key}", "settle_pending",
                f"Exit held {key}: the broker reconcile could not confirm its bracket down; retrying",
                interval=60.0, level=TRADEFLOW_LEVEL,
            )
            return
        last_price, market_snapshot = self._position_management_snapshot(position, bars)
        if self._exit_order_in_flight(key, position, last_price, bars, order_states):
            return
        management_symbol = str(position.metadata.get("underlying") or position.symbol)
        management_frame = bars.get(management_symbol)
        if not is_option_asset(position.metadata):
            underlying_price = last_price
        else:
            underlying_price = self.underlying_price_for_position(position, bars, None)
            if underlying_price is None:
                underlying_price = first_float(self.data.get_quote(management_symbol), "mark", positive=True)
        # Always reset management_adjustments at the start of each cycle to
        # prevent stale adjustments from persisting when price is unavailable.
        if isinstance(position.metadata, dict):
            position.metadata["management_adjustments"] = []
        decision: ExitDecision | None = None
        if last_price is not None:
            # The in-trade managers only move this position's levels, and the
            # adaptive ladder's touch hold proposes an exit. One that raises
            # is skipped, and the risk check below runs on the levels as it
            # left them, so the failure never costs the position its stop or
            # target (2026-09-26): a touch hold the failed ladder pass left in
            # the metadata is dropped, since it keeps the risk check off the
            # target.
            ladder_exit: ExitDecision | None = None
            try:
                self.trade_manager.manage_sr_flip(position, management_frame, float(last_price))
            except Exception as exc:
                self._position_failed(key, "the sr_flip manager", exc, failures, rest_runs=True)
            try:
                price_at = market_snapshot.get("price_at") if isinstance(market_snapshot, dict) else None
                ladder_exit = self.trade_manager.manage_adaptive_ladder(position, management_frame,
                                                                        float(last_price), price_at)
            except Exception as exc:
                if isinstance(position.metadata, dict):
                    position.metadata.pop(LADDER_TOUCH_HOLD_KEY, None)
                self._position_failed(key, "the adaptive ladder", exc, failures, rest_runs=True)
            self._update_position_diagnostics(position, last_price, underlying_price)
            risk_exit, risk_reason = self.trade_manager.update_position(position, last_price)
            if risk_exit:
                decision = ExitDecision(risk_reason, "risk")
            elif ladder_exit is not None:
                # The ladder's touch hold exits after the risk exits.
                decision = ladder_exit
            # Push any level the managers just moved onto the resting
            # broker children, before the exit decision below can cancel
            # them. No-op outside `replace` sync mode.
            self._sync_bracket_children(key, position, last_price, bars)
            if key not in self.positions:
                return
            adjustments = position.metadata.get("management_adjustments") if isinstance(position.metadata, dict) else None
            if adjustments:
                for adj in adjustments:
                    if not isinstance(adj, dict):
                        continue
                    self.audit.log_structured("POSITION_ADJUSTMENT", {
                        "symbol": key,
                        "underlying": str(position.metadata.get("underlying") or position.symbol),
                        "asset_type": asset_type_of(position.metadata),
                        "manager": str(adj.get("manager") or "unknown"),
                        "kind": str(adj.get("kind") or "unknown"),
                        "reason": str(adj.get("reason") or "unknown"),
                        "from": safe_float(adj.get("from"), None),
                        "to": safe_float(adj.get("to"), None),
                        "source_level": safe_float(adj.get("source_level"), None),
                        "last_price": safe_float(last_price, None),
                    })
        if decision is None:
            # The shared exits and the strategy's own only propose an exit. One
            # that raises proposes none this cycle; force flatten still applies.
            try:
                decision = self.strategy.exit_policy.decide(position, bars, data=self.data)
            except Exception as exc:
                self._position_failed(key, "the exit policy", exc, failures, rest_runs=True)
        if self.strategy.should_force_flatten(position):
            if not position.metadata.get("_force_flatten_logged"):
                LOG.warning("Force flatten triggered for %s qty=%s side=%s", key, position.qty, position.side.value)
                position.metadata["_force_flatten_logged"] = True
            # Force flatten guarantees a FULL exit but must not RELABEL one
            # that a real level already triggered. This ran
            # unconditionally, so every stop or target that fired inside
            # the force-flatten window was recorded as "force_flatten" —
            # inflating that bucket and hollowing out `stop` / `target` in
            # the per_exit_reason table the tuning is read from. A pending
            # scale-out is not a full exit, so it is upgraded (2026-09-24).
            if decision is None or decision.is_partial:
                decision = ExitDecision("force_flatten", "force_flatten")
        if decision is None:
            # No exit this cycle: a disaster stop the position is owed is
            # placed now (one opened before the regular session, or one the
            # broker refused). A cycle that exits sends none, only to cancel
            # it again.
            self.ensure_disaster_stop(key, position)
            return
        reason = decision.reason
        requested_qty = decision.close_qty(int(position.qty))
        if requested_qty < 1:
            # A 1-lot option or a 1-share position cannot scale out. The
            # trigger is spent, so it does not re-fire every cycle.
            self._record_exit_marker(position, decision.family, decision.marker, "below_one_unit")
            self._save_reconcile_metadata()
            self.audit.log_cycle(
                f"exit_gate:{key}", f"{decision.family}_below_one_unit",
                f"Exit held {key}: {reason} sizes to under one unit of qty={position.qty}",
                interval=60.0, level=TRADEFLOW_LEVEL,
            )
            return
        exit_context = self._position_exit_context(position, decision, last_price, underlying_price, market_snapshot, bars)
        if not self.executor.can_close_position_now(position, now):
            self.audit.log_cycle(
                f"exit_gate:{key}",
                f"session_closed:{reason}",
                f"Exit deferred {key} qty={requested_qty} reason={reason} because market session is closed",
                interval=60.0,
                level=TRADEFLOW_LEVEL,
            )
            return
        # The engine has decided to exit for a reason the broker cannot see
        # (peak giveback, time stop, CHoCH, force flatten). Tear down the
        # resting protection FIRST: leaving it live means the market-out and
        # the resting stop both fill, taking the strategy net short. A
        # scale-out goes the same way -- cancel, close the slice, then
        # re-protect what is left (below): at once when the slice books,
        # beside it when it is left working (only the shares it does not
        # cover), and in full when it never reached the broker. The
        # slice and a resting stop never cover the same shares, so
        # nothing double-fills.
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
                if is_disaster_stop(open_bracket):
                    # The stop resting is the far disaster stop, not the
                    # engine's: a DELETE that keeps failing holds the exit
                    # while the engine's own level is breached. Counted on
                    # the record, which a confirmed cancel retires.
                    deferred = int(open_bracket.get("cancel_failures") or 0) + 1
                    open_bracket["cancel_failures"] = deferred
                    self._escalate_disaster_stop(
                        key, deferred, f"its disaster stop's cancel was not confirmed ({cancel.message}), so its "
                        f"{reason} exit is deferred",
                    )
                self.audit.log_structured("EXIT_CONTEXT", {
                    **exit_context, "symbol": key, "qty": int(requested_qty),
                    "result_message": f"bracket_cancel_failed:{cancel.message}",
                    "attempt_status": "deferred_bracket_cancel_failed",
                })
                return
            self.book_bracket_cancel_fills(key, position, open_bracket, cancel, last_price, bars)
            if key not in self.positions:
                return
            # A child that filled before the cancel shrank the position.
            requested_qty = min(requested_qty, int(position.qty))
        result = self.executor.close_position(position, requested_qty, data=self.data, market_snapshot=market_snapshot,
                                              reprice_deadline=reprice_deadline)
        # The remainder is owed a resting stop when this cycle cancelled
        # the bracket, or an earlier one left it down (2026-09-24): an
        # attempt that failed before reaching the broker used to leave
        # the bracket cancelled, and the retry that booked the slice then
        # saw no bracket and re-protected nothing.
        reprotect_owed = open_bracket is not None or self._broker_bracket_down(position)
        # ok=True with filled_qty=0 filled nothing, so it is settled as the
        # unfilled attempt it is (2026-09-25). Its own branch tracked
        # nothing and re-protected nothing: the bracket this cycle
        # cancelled stayed down unless the deciding family fired again,
        # and an order that reached the broker was never settled from its
        # fill record.
        nothing_filled = result.filled_qty is not None and int(result.filled_qty) <= 0
        if not result.ok or nothing_filled:
            if result.order_id and (result.may_still_be_working or order_result_needs_broker_recheck(result.message)):
                # The order reached the broker and its outcome is not
                # settled: it may be live, or have filled shares the result
                # does not report. Track it and settle from its own fill
                # record next cycle; sending another exit meanwhile is how
                # a halted market-out fills twice.
                self._track_working_exit(position, result, decision, booked_qty=0, requested_qty=requested_qty,
                                         bracket_cancelled=reprotect_owed)
                if reprotect_owed:
                    self._reprotect_beside_working_order(position, requested_qty)
                self._save_reconcile_metadata()
                LOG.log(TRADEFLOW_LEVEL, "Exit order %s for %s unsettled (%s); tracking it", result.order_id, key, result.message)
                self.audit.log_structured("EXIT_CONTEXT", {**exit_context, "symbol": key, "qty": int(requested_qty), "result_message": result.message, "attempt_status": "tracking_unsettled_order"})
                return
            if reprotect_owed and not order_result_needs_broker_recheck(result.message):
                # The order never reached the broker (a rejected submit, a
                # stale quote), so nothing of it can fill: put back the
                # protection this cycle tore down. A retry cancels it
                # again -- churn, never a double fill. One that reached
                # it with no id to track (live_missing_order_id) may
                # still fill, so a resting stop beside it could sell the
                # same shares twice; the engine stop keeps owning it.
                self._reprotect_remainder(position)
                self._save_reconcile_metadata()
            attempt_status = "not_filled" if not result.ok else "filled_qty_zero"
            LOG.log(TRADEFLOW_LEVEL, "Exit attempt %s qty=%s reason=%s result=%s status=%s", key, requested_qty, reason, result.message, attempt_status)
            self.audit.log_structured("EXIT_CONTEXT", {**exit_context, "symbol": key, "qty": int(requested_qty), "result_message": result.message, "attempt_status": attempt_status})
            return
        if result.filled_qty is None:
            # No fill quantity reported (e.g., live broker that didn't
            # return filled_qty) — assume the order filled as sent. That is
            # the REQUESTED quantity: assuming position.qty booked a
            # scale-out as the whole position closed.
            exit_qty = requested_qty
        else:
            exit_qty = max(1, min(requested_qty, int(result.filled_qty)))
        if exit_qty < requested_qty:
            LOG.log(TRADEFLOW_LEVEL, "Partial exit %s requested_qty=%s filled_qty=%s reason=%s result=%s", key, requested_qty, exit_qty, reason, result.message)
        else:
            LOG.log(TRADEFLOW_LEVEL, "Exit %s qty=%s of %s reason=%s result=%s", key, exit_qty, position.qty, reason, result.message)
        # A fill price or mark that is not a finite number reads as
        # missing (2026-09-26). A NaN one booked NaN P&L into the account
        # and the risk manager's realized total, and the daily-loss check
        # never fired again that session.
        fill_price = safe_float(result.fill_price, finite=True)
        exit_price = fill_price if fill_price is not None else safe_float(last_price, finite=True)
        if exit_price is None:
            # The shares are GONE -- skipping the booking to "retry next
            # cycle" sent a second exit for them. Book at entry, flagged
            # estimated, so the quantity is right and P&L reads flat.
            LOG.error("Exit fill price unavailable for %s after a filled close_position(); booking at entry price (estimated)", key)
            exit_price = float(position.entry_price)
        # Exit slippage: how far the fill was from the intended level
        if isinstance(position.metadata, dict):
            if reason == "stop":
                position.metadata["exit_slippage"] = round(abs(exit_price - float(position.stop_price)), 6)
            elif reason == "target" and position.target_price is not None:
                position.metadata["exit_slippage"] = round(abs(exit_price - float(position.target_price)), 6)
        exited_position = copy.copy(position)
        exited_position.qty = exit_qty
        remaining_qty_after_exit = max(0, int(position.qty) - int(exit_qty))
        fill_price_estimated = fill_price is None
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
        self.audit.log_structured("EXIT_CONTEXT", {**exit_context, "symbol": key, "qty": int(exit_qty), "requested_qty": int(requested_qty), "filled_qty": int(exit_qty), "remaining_qty_after_exit": remaining_qty_after_exit, "result_message": result.message, "fill_price": float(exit_price), "realized_pnl": float(realized), "attempt_status": "filled"})
        self.audit.log_structured("TRADE_SUMMARY", self._trade_summary_payload(exited_position, exit_price, realized, reason, final_exit=final_exit, remaining_qty_after_exit=remaining_qty_after_exit, broker_recovered=False, fill_price_estimated=fill_price_estimated))
        if exit_qty >= position.qty:
            self._register_closed_position(key, position, realized, exit_price, bars)
            del self.positions[key]
            self._save_reconcile_metadata()
            self._sweep_exit_orders(key, position)
        else:
            self.risk.register_realized_pnl(realized)
            position.qty -= exit_qty
            self._record_exit_marker(position, decision.family, decision.marker, "booked")
            if isinstance(position.metadata, dict):
                position.metadata["qty"] = position.qty
            if result.may_still_be_working:
                # Partial fill whose cancel never confirmed: the remainder
                # may still fill. Track it so this cycle's slice is not
                # followed by a second exit for the same shares, and
                # re-protect only the shares the order does not cover (a
                # scale-out's); the rest is re-protected once it settles.
                self._track_working_exit(position, result, decision, booked_qty=exit_qty, requested_qty=requested_qty,
                                         bracket_cancelled=reprotect_owed)
                if reprotect_owed:
                    self._reprotect_beside_working_order(position, requested_qty - exit_qty)
            elif reprotect_owed:
                # A scale-out, or an exit that only partially filled, with
                # the bracket down -- cancelled this cycle or an earlier
                # one: re-establish protection at the current engine
                # levels, sized to what is left.
                self._reprotect_remainder(position)
            self.positions[key] = position
            self._save_reconcile_metadata()
