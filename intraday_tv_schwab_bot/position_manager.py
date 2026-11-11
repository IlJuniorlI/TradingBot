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
- The HTF timeframe of the trade manager's S/R reads is the strategy's
  (``strategy.htf_minutes()``), the one resolution the engine, the entry
  gatekeeper and the dashboard read too. The reads never fetch: the engine
  refreshes the frames before management (``IntradayBot._refresh_htf_frames``,
  bar-aligned by ``MarketDataStore.htf_refresh_due``).
"""
from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

import pandas as pd

from .audit_logger import AuditLogger, structured_metadata_snapshot
from .config import BotConfig
from .data_feed import EXECUTION_LAST_KEYS, MANAGEMENT_PRICE_KEYS, MarketDataStore
from .execution import (
    ReplacementLookup,
    BracketCancel,
    SchwabExecutor,
    order_result_left_nothing_live,
    order_result_names_replacement,
)
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
from .position_store import ReconcileMetadataStore
from .position_metrics import (
    DISASTER_STOP_OWN_PRICE_KEY,
    DISASTER_STOP_PRICE_KEY,
    LADDER_TOUCH_HOLD_KEY,
    STOP_SOURCE_KEY,
    append_management_adjustment,
    exit_level,
    exit_reason_details,
    exit_slippage,
    favorable_move,
    initial_risk_per_unit,
    position_return_pct_at_price,
    position_unrealized_at_price,
)
from .risk import RiskManager
from .sr_snapshot import sr_snapshot
from .trade_management import ProtectiveRead, TradeManager, TradeSpreads, protective_exit, protective_read
from .broker_payloads import (
    BRACKET_CHILD_KEYS,
    BRACKET_ID_KEYS,
    BRACKET_REPLACED_IDS_KEY,
    DISASTER_STOP_REASON,
    DISASTER_SYNC_MODE,
    MOVED_STOP_ORDER_TYPES,
    ORDER_DEAD,
    ORDER_LIVE,
    ORDER_REPLACED,
    STOP_ORDER_TYPES,
    WORKING_STATUSES,
    active_broker_bracket,
    add_booked_child_fill,
    booked_child_part,
    booked_fills_ledger,
    bracket_order_ids,
    broker_position_side_qty,
    exit_orders,
    is_disaster_stop,
    listed_stop,
    note_replaced_order,
    order_entered_at,
    order_may_be_live,
    order_result_needs_broker_recheck,
    order_row_filled_qty,
    order_status_class,
    protective_stop_reason,
    replacement_order,
    sent_exit_stop,
    sent_exit_stops,
    unbooked_fill_price,
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
# evening before) is a NORMAL DAY order that can outlive it; so does the
# good-till-cancelled disaster stop of a strategy that holds across sessions
# (SchwabExecutor.disaster_stop_duration) from its second session on. One read
# a minute per such child keeps a few of them far inside Schwab's ~120
# requests/minute.
UNLISTED_BRACKET_CHILD_READ_SECONDS = 60.0

# How long after an attempt at a position's disaster stop that did not leave
# it resting the next one waits (ensure_disaster_stop): after the broker
# refused it (a 4xx: nothing placed) or rejected it once it was accepted,
# one a minute per position keeps a stop the broker keeps refusing (a level
# the price is already through, a halted symbol) far inside Schwab's ~120
# requests/minute, and every attempt logs; after a submit whose outcome is
# unknown, an order the broker did accept is listed long before the day's
# orders are read to look for it. The lookup reads the orders entered from
# this long before the attempt on, so a clock a little off the broker's
# still finds it.
DISASTER_STOP_RETRY_SECONDS = 60.0

# How often a post-close sweep still owed is tried again on the passes that
# manage no position (PositionManager.run_owed_sweeps). The leftover it is
# after cannot fill outside the regular session, and such a pass comes every
# loop sleep while a position is held: one try a minute keeps a broker that
# cannot be read from an order read and an ERROR every two seconds all evening.
OWED_SWEEP_RETRY_SECONDS = 60.0

# The price the paper account marks a held equity at, which the session
# archive's manifest and the SESSION REPORT name (``equity_mark_basis``): the
# price management read for it in the pass (``_manage_position``), or, in a
# pass that did not price it, the engine's quotes-phase mark, its step frame's
# last 1m close. An archive or a report that does not name it was written by
# an earlier version, which marked every held equity at that 1m close alone.
EQUITY_MARK_BASIS = "management_price"

# A position whose management raises is logged with its traceback on the first
# failure of a run of consecutive failed cycles and on every
# POSITION_ERROR_TRACEBACK_EVERY-th after, and as a one-line WARNING in
# between: the engine throttles its own cycle errors this way.
POSITION_ERROR_TRACEBACK_EVERY = 10


@dataclass(frozen=True, slots=True)
class _Look:
    """One management pass's read of a position's price: when (the wall
    clock at the read), and the mark its levels were checked against.
    ``entry_time`` ties it to the position it read, so a position opened
    later under the same key never inherits it."""

    entry_time: datetime
    at: datetime
    mark: float


def _look_fields(previous: _Look | None, current: _Look | None) -> dict[str, Any]:
    """The fields an EXIT_CONTEXT or POSITION_ADJUSTMENT record carries for
    the management passes around it: ``managed_at``, the pass that decided
    it (absent for an exit the broker filled); ``prev_managed_at`` and
    ``prev_mark_price``, the pass before it, or for an exit the broker filled
    the last pass before it was booked; ``managed_gap_s``, the seconds
    between the two. A position's first pass has no pass before it."""
    fields: dict[str, Any] = {}
    if current is not None:
        fields["managed_at"] = current.at.isoformat(timespec="milliseconds")
    if previous is not None:
        fields["prev_managed_at"] = previous.at.isoformat(timespec="milliseconds")
        fields["prev_mark_price"] = previous.mark
        if current is not None:
            fields["managed_gap_s"] = round((current.at - previous.at).total_seconds(), 3)
    return fields


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
        reconcile_metadata_store: ReconcileMetadataStore | None,
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
        # The state store, where the post-close sweeps still owed are saved
        # (``_save_owed_sweeps``); None only in tests that build a manager
        # with no store.
        self.reconcile_metadata_store = reconcile_metadata_store
        self._save_reconcile_metadata = save_reconcile_metadata
        # Child id -> (monotonic time read, order state) for bracket children
        # the listing does not return; see _unlisted_bracket_child_state.
        self._unlisted_child_states: dict[str, tuple[float, dict[str, Any] | None]] = {}
        # Position key -> consecutive management cycles in which managing
        # that position raised; see _position_failed.
        self._failure_streaks: dict[str, int] = {}
        # Position key -> the last management pass that read its price; see
        # _take_look.
        self._looks: dict[str, _Look] = {}
        # Position key -> the post-close sweep still owed for a closed
        # position whose orders could not be read, or whose leftover exit
        # orders' cancel is not confirmed; see _sweep_exit_orders. Saved to
        # the state store as it changes and tracked again by a restart
        # (``_save_owed_sweeps``, ``load_owed_sweeps``).
        self._sweeps_owed: dict[str, dict[str, Any]] = {}
        # The owed sweeps as the store holds them: what the last save wrote,
        # or what the start-up's load read. Empty before either, so nothing
        # is written until a sweep is owed.
        self._owed_sweeps_saved: dict[str, dict[str, Any]] = {}
        # When the store's owed sweeps could not be read (monotonic), None
        # once they were: until then nothing is written over them, and the
        # read is tried again (_try_owed_sweeps).
        self._owed_sweeps_unread_since: float | None = None
        # By position key, the bracket children (their record keys) the
        # broker reconcile followed to a replacement and left to the next
        # management pass to size (follow_replaced_children): the reconcile
        # has not compared the position with the account by then.
        self._unsized: dict[str, set[str]] = {}
        # A position was handed to the settle against the account (held
        # with ``settle_pending``) by the follow of a REPLACED stop that
        # found nothing left resting and shares sold by orders it cannot
        # tell apart (_replaced_child_gone): the engine settles it on its
        # next pass and clears this once it is settled
        # (IntradayBot._maybe_settle_handed_positions).
        self.settle_wanted = False
        # When run_owed_sweeps last tried them (monotonic), None before.
        self._owed_sweeps_tried_at: float | None = None
        # Position key -> its recent spreads, the abnormal-book guard of a
        # strategy that judges its protective exits on the executable side;
        # see _protective_read.
        self._trade_spreads: dict[str, TradeSpreads] = {}

    # ------------------------------------------------------------------
    # Mark-price resolution for open positions.
    # ------------------------------------------------------------------

    def _position_management_snapshot(self, position: Position, bars) -> tuple[float | None, dict[str, Any] | None]:
        """The price this cycle manages ``position`` at, and where it came
        from. An equity's snapshot names the price's time as ``price_at``:
        the quote's ``fetched_at``, or the open time of the bar whose close
        it is (the adaptive ladder's touch hold attributes a touch to that
        1m bar); the account's cached price has none.

        An equity is priced from its cached quote while that is younger than
        ``runtime.quote_cache_seconds``. One that aged within the pass (the
        HTF refresh before management, a live entry's settle, an earlier
        position's exit re-sends), or none cached, is read from the stream's
        book first when the stream serves the symbol
        (``_read_management_stream_quote``); the forced REST refresh runs
        only when an aged cached quote is still stale after that, and with no
        cached quote and no fresh book the price is the bar close, as before.
        The snapshot's bid, ask and last are what an exit it decides is priced
        from (``SchwabExecutor.submit_equity_exit``). A stream book's quote
        also names the book's last item time and its quote time
        (``stream_item_at``, ``stream_quote_time_ms``; None for a REST quote):
        diagnostics for POSITION_MARK, which no decision reads."""
        mark = self.strategy.position_mark_price(position, self.data)
        if mark is not None:
            price = float(mark)
            return price, None
        if is_option_asset(position.metadata):
            return self._option_position_management_snapshot(position)
        if self.data is not None:
            max_age = max(1.0, float(self.config.runtime.quote_cache_seconds))
            quote = self.data.get_quote(position.symbol) or {}
            if not (quote and self.data.quotes_are_fresh([position.symbol], max_age)) \
                    and self._read_management_stream_quote(position.symbol):
                quote = self.data.get_quote(position.symbol) or quote
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
                        "stream_item_at": quote.get("stream_item_at"),
                        "stream_quote_time_ms": quote.get("stream_quote_time_ms"),
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

    def _protective_read(self, key: str, position: Position, last_price: float | None,
                         market_snapshot: dict[str, Any] | None) -> ProtectiveRead | None:
        """The price this pass judges ``position``'s protective exits on (a
        stop at or past the entry, the peak giveback floors): with the
        strategy's ``protective_exits_on_executable_side`` on, the bid (a
        LONG) or the ask (a SHORT) of the management snapshot, the quote an
        exit it decides is priced from, or the mark when that book is not a
        quote, cannot be read, is abnormal against the position's recent
        spreads or lies past the mark (``trade_management.protective_read``).
        None, which judges every level on the mark, with the knob off, for an
        option, and on a pass with no price. Dry run and live read the same
        snapshot, so they take the same decision.

        The position's spreads are kept here, in memory, at most one a pass
        whose book has both sides and moved since the position's previous
        such pass (a zero spread, and once the window holds the minimum an
        abnormal one, left out; ``TradeSpreads``), and dropped with the
        position. A restart starts them again, and while the window refills
        the mark judges: a quote frozen across the restart adds one spread,
        so the mark judges for as long as it stays frozen."""
        if last_price is None or is_option_asset(position.metadata):
            return None
        if not self.strategy.protective_exits_on_executable_side():
            return None
        spreads = self._trade_spreads.get(key)
        if spreads is None or spreads.entry_time != position.entry_time:
            spreads = self._trade_spreads[key] = TradeSpreads(position.entry_time)
        return protective_read(position.side, float(last_price), market_snapshot, spreads)

    def _read_management_stream_quote(self, symbol: str) -> bool:
        """When the stream serves ``symbol`` (``MarketDataStore.stream_quote_fresh``),
        publish its book into the quote cache through a non-forced refresh
        (source ``engine:position_management_stream``) instead of forcing a
        REST quote. Returns whether that refresh was asked for, so the caller
        reads the cache again. A failure is logged with its type (WARNING at
        most once a minute, DEBUG between) and the caller goes on to its
        forced REST refresh, as it did before the stream was read."""
        asked = False
        try:
            if self.data.stream_quote_fresh(symbol):
                asked = True
                self.data.fetch_quotes([symbol], source="engine:position_management_stream")
        except Exception as exc:
            self._log_component_failure(
                "management_stream_quote",
                "Management's stream quote read for %s failed (%s: %s); the forced REST refresh decides",
                symbol, type(exc).__name__, exc,
            )
        return asked

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

        It also records the first pass whose mark was at or through the
        target as the pass's risk check starts (the pass calls this after
        the sr_flip and ladder managers and ahead of
        ``TradeManager.update_position``, whose runner extension can move
        the target before its own target check):
        ``diag_target_reached_price`` (that target) and
        ``diag_target_reached_ts``. A target moved later (the runner
        extension, a ladder promotion) does not reset it. The trade record
        carries it with the best mark (``diag_best_mark_price``), and the
        session report counts the trades that reached their target and
        closed at a loss (``target_reached_losses``).
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
        target = safe_float(position.target_price, None, finite=True)
        if target is not None and meta.get('diag_target_reached_price') is None \
                and favorable_move(position.side, target, float(mark_price)) >= 0.0:
            meta['diag_target_reached_price'] = target
            meta['diag_target_reached_ts'] = ts

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
        level, level_kind = exit_level(position, decision)
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
            # The level the exit is on (exit_level); a filled record adds how
            # far past it the fill landed (_exit_fill_fields).
            'exit_level': level,
            'exit_level_kind': level_kind,
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

    @staticmethod
    def _exit_fill_fields(position: Position, exit_context: dict[str, Any], fill_price: float) -> dict[str, Any]:
        """A filled exit's ``exit_slippage``: how far its fill landed past
        ``exit_level``, per unit, positive when worse for the position (see
        ``position_metrics.exit_slippage``), and ``exit_slippage_r``, that in
        the trade's initial R. Absent for an exit on no level, and for one
        whose fill price is estimated."""
        level = exit_context.get('exit_level')
        if level is None:
            return {}
        slippage = exit_slippage(position.side, float(level), float(fill_price))
        fields: dict[str, Any] = {'exit_slippage': round(slippage, 6)}
        risk = exit_context.get('initial_risk_per_unit')
        if risk is not None and risk > 0:
            fields['exit_slippage_r'] = round(slippage / risk, 4)
        return fields

    # ------------------------------------------------------------------
    # The management passes' reads of each position's price.
    # ------------------------------------------------------------------

    def _last_look(self, key: str, position: Position) -> _Look | None:
        """The last management pass that read ``position``'s price, or None
        (none has yet, or the last read was of an earlier position under
        ``key``)."""
        look = self._looks.get(key)
        return look if look is not None and look.entry_time == position.entry_time else None

    def _take_look(self, key: str, position: Position, last_price: float | None,
                   market_snapshot: dict[str, Any] | None) -> tuple[_Look | None, _Look | None]:
        """Record this pass's read of ``position``'s price; returns the pass
        before it and this one. A pass without a price records nothing, and
        the read before it stays the last. With ``runtime.log_position_marks``
        on, logs the read (``_log_position_mark``)."""
        previous = self._last_look(key, position)
        if last_price is None:
            return previous, None
        look = _Look(position.entry_time, sessions.now_et(), float(last_price))
        self._looks[key] = look
        if self.config.runtime.log_position_marks:
            self._log_position_mark(key, position, look, previous, market_snapshot)
        return previous, look

    def _log_position_mark(self, key: str, position: Position, look: _Look, previous: _Look | None,
                           market_snapshot: dict[str, Any] | None) -> None:
        """One POSITION_MARK record, at DEBUG (the log file, not the console):
        when the pass read the price (``at``), the mark, the seconds since the
        pass before (``gap_s``), the stop and target coming into the pass (a
        ratchet this pass moves them: POSITION_ADJUSTMENT), and the quote the
        mark came from, its ``source`` and its ``price_at`` (absent for a
        strategy's own mark, the 0DTE strategies'). A stream book's quote
        adds the book's own times (since 2026-10-13): ``stream_item_at``, the
        receipt of its last item, and ``stream_quote_time_ms``, its quote time
        (field 34, Schwab's epoch ms), so a mark says how old its own book was
        (``price_at`` is the stream's last packet, any symbol's)."""
        snapshot = market_snapshot if isinstance(market_snapshot, dict) else {}
        payload = {
            'symbol': key,
            'side': position.side.value,
            'at': look.at.isoformat(timespec="milliseconds"),
            'mark': look.mark,
            'gap_s': None if previous is None else round((look.at - previous.at).total_seconds(), 3),
            'stop': safe_float(position.stop_price, None),
            'target': safe_float(position.target_price, None),
            'bid': safe_float(snapshot.get('bid'), None),
            'ask': safe_float(snapshot.get('ask'), None),
            'last': safe_float(snapshot.get('last'), None),
            'source': snapshot.get('source'),
            'price_at': snapshot.get('price_at'),
            'stream_item_at': snapshot.get('stream_item_at'),
            'stream_quote_time_ms': snapshot.get('stream_quote_time_ms'),
        }
        self.audit.log_structured("POSITION_MARK", {k: v for k, v in payload.items() if v is not None},
                                  level=logging.DEBUG)

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

    def _held_part_of_fill(self, key: str, position: Position, filled_qty: int, source: str) -> int:
        """The part of a broker fill not yet booked (*filled_qty*, by
        *source*, the order named) that the position held: all of it, or the
        position's quantity when the order filled more. The extra was sold
        (or bought back) beyond the position, so the account now holds it
        the other way and nothing tracks it: logged at CRITICAL (``EXIT
        OVERFILLED``), naming the extra. Until 2026-09-29 each booking capped
        the fill at the position and said nothing above WARNING."""
        held = int(position.qty)
        if int(filled_qty) > held:
            extra = int(filled_qty) - held
            LOG.critical(
                "EXIT OVERFILLED — %s: %s filled %d against the %d held; the account holds the %d extra %s, "
                "untracked. Check the account.", key, source, int(filled_qty), held, extra,
                "short" if position.side == Side.LONG else "long",
            )
        return min(held, int(filled_qty))

    def _book_broker_exit(self, key: str, position: Position, exit_qty: int, exit_price: float,
                          decision: ExitDecision, bars, *, result_message: str, attempt_status: str,
                          fill_price_estimated: bool, exit_limits_missed: int | None) -> float:
        """Record an exit the engine learned of from the BROKER -- a resting
        bracket child that filled (a ``risk`` exit), or an exit order that
        filled after its submit call returned (the decision it was sent for)
        -- mirroring the manage_positions tail. Returns the realized P&L of
        the slice. ``exit_limits_missed`` is the missed-limit count of the
        live LIMIT exit attempt that sent the order (None for anything
        else), stamped on its EXIT_CONTEXT as the submit-time records
        are."""
        reason = decision.reason
        # The broker filled it: the pass before is the last one that read the
        # price before it was booked.
        exit_context = {
            **self._position_exit_context(position, decision, exit_price, exit_price, None, bars),
            **_look_fields(self._last_look(key, position), None),
        }
        if not fill_price_estimated:
            exit_context.update(self._exit_fill_fields(position, exit_context, exit_price))
        if exit_limits_missed is not None:
            exit_context["exit_limits_missed"] = int(exit_limits_missed)
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
        without filling -- a DAY order EXPIRED at its session's end (a
        good-till-cancelled disaster stop, a strategy's that holds across
        sessions, does not), one CANCELED in the app, a REJECTED one --
        retires its bracket
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
        # The children followed to a replacement this pass, and why each
        # one that reads REPLACED could not be (_follow_replaced_child); and
        # the ones the broker reconcile followed and left to this pass to
        # size (follow_replaced_children).
        followed: dict[str, dict[str, Any]] = {}
        unresolved: list[str] = []
        unsized = self._unsized.pop(key, set())
        for child_key, reason in (("stop_order_id", protective_stop_reason(bracket)), ("target_order_id", "broker_target")):
            child_id = bracket.get(child_key)
            if not child_id:
                continue
            state = self._bracket_child_state(str(child_id), states)
            if isinstance(state, dict) and order_status_class(state.get("status")) == ORDER_REPLACED:
                # It lives on in its replacement, which is tracked in its
                # place before anything is booked or retired.
                state, why = self._follow_replaced_child(key, position, bracket, child_key, state, states, bars)
                if key not in self.positions or state is None:
                    # Closed by the replaced order's own fills, or handed to
                    # its settle against the account.
                    return
                if why is None:
                    followed[child_key] = state
                else:
                    unresolved.append(why)
                child_id = bracket.get(child_key)
            child_states[child_key] = state
            if not isinstance(state, dict) or not state.get("is_filled"):
                continue
            # Less what an unconfirmed stop's lookup already booked from it,
            # and priced as the rest's own fills (broker_payloads
            # .unbooked_fill_price): the order's average covers both.
            booked_qty, booked_notional, booked_estimated = booked_child_part(bracket, child_id)
            filled = int(state.get("filled_qty") or 0)
            filled_qty = filled - booked_qty
            if filled_qty <= 0:
                continue
            exit_qty = self._held_part_of_fill(key, position, filled_qty, f"{reason} order {child_id}")
            fill_price, fill_price_estimated = unbooked_fill_price(
                filled, safe_float(state.get("fill_price"), None, finite=True),
                state.get("fill_price_estimated") is not False, booked_qty, booked_notional, booked_estimated,
                what=f"{reason} order {child_id} for {key}")
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
                # The resting level, or the order's own price: not an
                # execution price (broker_payloads.order_fill_price_estimated).
                fill_price_estimated=fill_price_estimated, exit_limits_missed=None,
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
            # One the reconcile left to size whose state does not read this
            # pass waits for the next.
            unread = {child_key for child_key in unsized
                      if bracket.get(child_key) and not isinstance(child_states.get(child_key), dict)}
            if unread:
                self._unsized[key] = unread
            followed = {**{child_key: child_states[child_key] for child_key in unsized - unread
                           if isinstance(child_states.get(child_key), dict)}, **followed}
            self._after_follow(key, position, bracket, child_states, followed, unresolved)
            stop_state = child_states.get("stop_order_id")
            if (bracket.get("failed_attempts") and bracket.get("state") in ("disaster_stop", "adopted")
                    and isinstance(stop_state, dict) and stop_state.get("status") in WORKING_STATUSES):
                # The broker lists the disaster stop working at the size it
                # was sent: its consecutive misses end here, not when a
                # submit is accepted, so a stop the broker rejects once it
                # has accepted it keeps counting (2026-09-29).
                del bracket["failed_attempts"]
            # The child that protects the position: its stop, or once an
            # unconfirmed retire dropped a dead stop, what is left of it.
            guard_key = "stop_order_id" if bracket.get("stop_order_id") else "target_order_id"
            self._retire_dead_bracket(key, position, bracket, guard_key, child_states.get(guard_key), bars)

    def _bracket_child_state(self, child_id: str, states: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """A bracket child's state row: the pass's listing's, else the
        child read on its own (``_unlisted_bracket_child_state``). None when
        neither gives it."""
        state = states.get(child_id)
        return state if state is not None else self._unlisted_bracket_child_state(child_id)

    def follow_replaced_children(self) -> None:
        """Follow every tracked position's REPLACED protective orders
        (``_follow_replaced_child``): for the broker reconcile, ahead of its
        settle and its foreign-order check (2026-11-11). The reconcile of a
        new day runs before the day's first management pass, so the
        replacement of a stop moved the evening before was a foreign order
        to it (entries blocked for the day, and "clear them" meant
        cancelling the position's only stop), and its settle sent its
        cancel to the REPLACED id. A position held for the settle's retry
        (``_settle_pending``) is followed too: its cancel waits on exactly
        this.

        The orders are read as a management pass reads them (the 8-hour
        listing, else each child on its own), and nothing is read when no
        position has live broker protection. A replacement's size is not
        put right here: the settle that follows has yet to compare the
        position with the account, and a resize to shares the account no
        longer holds would rest more than is held. The next management
        pass sizes it (``_unsized``). One whose follow raises is logged
        with the type, and the others are followed."""
        if self.config.schwab.dry_run:
            return
        states: dict[str, dict[str, Any]] | None = None
        for key, position in list(self.positions.items()):
            bracket = active_broker_bracket(position)
            if bracket is None:
                continue
            if states is None:
                states = self.executor.fetch_order_states() or {}
            try:
                child_states: dict[str, dict[str, Any] | None] = {}
                followed: dict[str, dict[str, Any]] = {}
                unresolved: list[str] = []
                for child_key in BRACKET_CHILD_KEYS:
                    if not bracket.get(child_key):
                        continue
                    state = self._bracket_child_state(str(bracket[child_key]), states)
                    if isinstance(state, dict) and order_status_class(state.get("status")) == ORDER_REPLACED:
                        state, why = self._follow_replaced_child(key, position, bracket, child_key, state, states, {})
                        if key not in self.positions or state is None:
                            break
                        if why is None:
                            followed[child_key] = state
                        else:
                            unresolved.append(why)
                    child_states[child_key] = state
                else:
                    self._after_follow(key, position, bracket, child_states, {}, unresolved)
                    if followed:
                        self._unsized.setdefault(key, set()).update(followed)
            except Exception as exc:
                LOG.error("%s: following its REPLACED protective orders failed (%s: %s); what it had booked or "
                          "re-pointed by then stands", key, type(exc).__name__, exc, exc_info=exc)

    def _after_follow(self, key: str, position: Position, bracket: dict[str, Any],
                      child_states: dict[str, dict[str, Any] | None], followed: dict[str, dict[str, Any]],
                      unresolved: list[str]) -> None:
        """Once *bracket*'s children are read and the REPLACED ones followed
        (*followed*: child key to the state of the replacement to size;
        *unresolved*: why each one that could not be followed was not):

        - Count the pass as a miss when any is unresolved (once a pass,
          however many are), on the record (``replacement_misses``), saved:
          logged at ERROR, and every
          ``execution.disaster_stop_escalation_attempts``-th ``ORDER REPLACED
          UNTRACKED`` at CRITICAL. The position is held while the count
          stands (``_replacement_unresolved``).
        - End the hold when none is unresolved and every child was read: one
          that cannot be read this pass may still be the REPLACED one.
        - Record the levels the working children rest at
          (``_track_resting_levels``: the price a stop was moved to is
          kept), then put right a replacement of another size
          (``_size_followed_children``)."""
        if unresolved:
            misses = int(bracket.get("replacement_misses") or 0) + 1
            bracket["replacement_misses"] = misses
            detail = f"{'; '.join(unresolved)} ({misses} consecutive time(s))"
            LOG.error("%s: %s; no exit is sent and nothing is placed for it until it is", key, detail)
            if misses % int(self.config.execution.disaster_stop_escalation_attempts) == 0:
                LOG.critical("ORDER REPLACED UNTRACKED — %s: %s; the replacement may still rest untracked, and the "
                             "position is held: no exit is sent for it. Check the account; the hold ends when the "
                             "orders show one order that replaced it (cancel any other exit order on %s at the "
                             "broker).", key, detail,
                             str(position.metadata.get("underlying") or position.symbol))
            self._save_reconcile_metadata()
        elif all(isinstance(state, dict) for state in child_states.values()) \
                and bracket.pop("replacement_misses", None) is not None:
            self._save_reconcile_metadata()
        self._track_resting_levels(key, bracket, child_states)
        self._note_moved_backup_level(key, position, bracket, child_states.get("stop_order_id"))
        if followed:
            self._size_followed_children(key, position, bracket, followed,
                                         str(position.metadata.get("underlying") or position.symbol))

    @staticmethod
    def _never_its_replacement(position: Position, bracket: dict[str, Any]) -> set[str]:
        """The orders that cannot be the replacement of one of *bracket*'s
        REPLACED children, for the look among the orders
        (``SchwabExecutor.replacement_of``): the ones the record tracked
        before (``BRACKET_REPLACED_IDS_KEY``), every order it tracks now (a
        bracket's target is no replacement of its stop), and the position's
        working exit order."""
        never = {str(oid) for oid in bracket.get(BRACKET_REPLACED_IDS_KEY) or ()} | bracket_order_ids(bracket)
        working = position.metadata.get("working_exit_order") if isinstance(position.metadata, dict) else None
        if isinstance(working, dict) and working.get("order_id"):
            never.add(str(working["order_id"]))
        return never

    def _follow_replaced_child(self, key: str, position: Position, bracket: dict[str, Any], child_key: str,
                               state: dict[str, Any], states: dict[str, dict[str, Any]],
                               bars) -> tuple[dict[str, Any] | None, str | None]:
        """Track, in place of a protective order of *bracket* the broker
        lists REPLACED (*child_key*: its stop or its target, in *state*),
        the order that replaced it (2026-11-11). Returns the state of the
        order the record tracks under *child_key* from here, and None when
        that is a replacement found, else why none was (it is still the
        REPLACED one). The state is None when the position was handed to
        its settle against the account instead (``_replaced_child_gone``).

        Schwab does not edit an order: a stop moved in the app, or resized
        by the bot with an answer that carried no new id
        (``SchwabExecutor.replace_bracket_child``, ``new_id_unknown``), ends
        as REPLACED and lives on in a new order. Until then the record kept
        the old id for the rest of the hold: the fill reconcile read only
        that id (a REPLACED order is neither filled nor dead), the cancel
        before an exit read it as down, so the exit went out beside the
        replacement, and the replacement's fill was never booked, so the
        bot went on managing shares the account no longer held and its next
        exit sold them short. For a good-till-cancelled disaster stop that
        lasted days (F-0004).

        In this order:

        - Its own fills the record has not booked are booked first, as the
          stop's (or the target's) exit, under the booked-fills ledger
          (``_book_replaced_child_fills``): a later cancel that still lists
          the old order reports nothing twice. The record stays active: the
          rest lives on in the replacement. A booking that closes the
          position leaves what still rests to its post-close sweep.
        - The replacement is the one ``SchwabExecutor.replacement_of``
          finds (the one the payload names, else the one among the orders
          entered since it was), and its state must read (the pass's
          listing, else a read of its own).
        - The record's child id and its entry in ``child_order_ids`` are
          re-pointed and saved, the id left behind added to the record's
          ``replaced_order_ids`` (never taken for a later replacement). A
          replacement that is itself REPLACED is followed in the same pass.

        The caller then treats the replacement as the child it is: a fill
        is booked, a dead one retired and re-placed, a working one's level
        and size put right (``_after_follow``).

        While the replacement is not found (none named and none or several
        that fit, orders or a state that cannot be read, a chain that comes
        back on itself) the caller counts the pass as a miss and the
        position is held (``_after_follow``, ``_replacement_unresolved``):
        ``_manage_position`` sends no exit and places nothing for it, as it
        holds one whose working exit order's replacement is not found
        (``_follow_replaced_exit``), until the orders show the replacement.
        When they show instead that nothing entered after it can still rest
        there is nothing to hold for, once they have shown it for a retry
        interval (``_gone_long_enough``, ``_replaced_child_gone``)."""
        symbol = str(position.metadata.get("underlying") or position.symbol)
        child = "stop" if child_key == "stop_order_id" else "target"
        seen: set[str] = set()
        while True:
            child_id = str(bracket[child_key])
            seen.add(child_id)
            priced = self._book_replaced_child_fills(key, position, bracket, child_key, child_id, state, bars)
            if key not in self.positions:
                return state, None
            lookup = ReplacementLookup(None, "its own fills cannot be priced") if not priced else \
                self.executor.replacement_of(child_id, state, symbol, position.side,
                                             earlier=self._never_its_replacement(position, bracket))
            if lookup.gone:
                if self._gone_long_enough(key, bracket, child, child_id):
                    return self._replaced_child_gone(key, position, bracket, child_key, child_id, state, lookup.sold)
                return state, None
            bracket.pop("replacement_gone_since", None)
            replacement_id, how = lookup.order_id, lookup.how
            replacement_state = None
            if replacement_id is not None and replacement_id in seen:
                replacement_id, how = None, f"its replacements come back to order {replacement_id}"
            if replacement_id is not None:
                replacement_state = self._bracket_child_state(replacement_id, states)
                if replacement_state is None:
                    replacement_id, how = None, f"its replacement {replacement_id} ({how}) could not be read"
            if replacement_id is None:
                return state, (f"its {child} order {child_id} was REPLACED at the broker and its replacement is "
                               f"not found ({how})")
            LOG.warning("Bracket %s %s for %s was REPLACED at the broker by order %s (%s), which is tracked in its "
                        "place", child, child_id, key, replacement_id, how)
            kind = replacement_state.get("order_type")
            if child == "stop" and kind and kind not in MOVED_STOP_ORDER_TYPES:
                # Tracked as the stop all the same: an exit cancels it first
                # and its fill is booked. But it stops nothing, and the log
                # said nothing of it until 2026-11-11.
                LOG.error("%s: its stop was changed at the broker into a %s order (%s), which is tracked in the "
                          "stop's place: NO STOP RESTS AT THE BROKER for it while that order stands. If it has to "
                          "be cut down it is re-issued as the bot's stop at %s", key, kind, replacement_id,
                          bracket.get("stop_price"))
            note_replaced_order(bracket, child_id)
            bracket[child_key] = replacement_id
            if str(bracket.get("protective_order_id") or "") == child_id:
                # A lone protective stop is its own wrapper.
                bracket["protective_order_id"] = replacement_id
            children = [str(oid) for oid in (bracket.get("child_order_ids") or [])]
            if child_id in children:
                children[children.index(child_id)] = replacement_id
            else:
                children.append(replacement_id)
            bracket["child_order_ids"] = children
            self._save_reconcile_metadata()
            state = replacement_state
            if order_status_class(state.get("status")) != ORDER_REPLACED:
                return state, None

    def _gone_long_enough(self, key: str, bracket: dict[str, Any], child: str, child_id: str) -> bool:
        """True once the orders have shown, for ``DISASTER_STOP_RETRY_SECONDS``
        from the first such read, that nothing entered after the REPLACED
        *child_id* can still rest (``ReplacementLookup.gone``): only then is
        the record retired or the position handed to the settle
        (``_replaced_child_gone``).

        One read is not enough to act on. An order listing can run behind
        the order's own state: the replacement of an order that already
        reads REPLACED may not be listed yet, and with another, older exit
        order listed after it (the third review's probe) that one read said
        "nothing rests", a fresh stop went in, and the replacement surfaced
        beside it: two resting stops, one untracked. The wait is the one a
        submit with an unknown outcome is given to show in the orders. The
        first read is stamped on the record (``replacement_gone_since``),
        which holds the position meanwhile (``_replacement_unresolved``) and
        is dropped by any read that finds the replacement or no longer says
        gone. The wait is logged once, at WARNING, and is no miss."""
        now = sessions.now_et()
        since = bracket.get("replacement_gone_since")
        if since is None:
            bracket["replacement_gone_since"] = now.isoformat()
            LOG.warning("%s: its %s order %s was REPLACED at the broker and the orders show nothing entered after it "
                        "that can still rest; held for %.0f s in case they do not list its replacement yet", key,
                        child, child_id, DISASTER_STOP_RETRY_SECONDS)
            self._save_reconcile_metadata()
            return False
        return (now - datetime.fromisoformat(str(since))).total_seconds() >= DISASTER_STOP_RETRY_SECONDS

    def _replaced_child_gone(self, key: str, position: Position, bracket: dict[str, Any], child_key: str,
                             child_id: str, state: dict[str, Any],
                             sold: int) -> tuple[dict[str, Any] | None, str | None]:
        """A REPLACED protective order whose replacement cannot be told,
        while the orders show that nothing entered after it can still rest
        (``broker_payloads.nothing_rests_after``): every later exit order
        on the symbol for the side filled or died. Nothing may rest
        untracked, so there is nothing to hold the position for
        (2026-11-11; the answer in ``_follow_replaced_child``'s shape).

        - Those orders sold nothing (*sold* 0: the stop was changed into
          another kind of order and then cancelled, say). A stop reads as
          dead from here, and the caller's retire takes it down, with what
          else the record tracks, and places a fresh one
          (``_retire_dead_bracket``). A target is dropped from the record,
          which keeps its stop: the engine owns the target again.
        - They sold shares: which of them replaced it is not known, and one
          of them may be a sale made by hand that the reconcile's settle
          has booked or will, so nothing is booked from them here. The
          record drops the order (``_drop_gone_child``) and stays active
          only for what else it tracks (a bracket's other leg, which the
          settle cancels); the position is held for its settle against the
          account (``settle_pending``), which compares it with the account,
          books what left it and lifts the hold, and the engine runs that
          settle for it alone on its next pass (``settle_wanted``,
          ``StartupReconciler.settle_handed_positions``), whatever
          ``runtime.startup_reconcile_mode`` is. The pass after it places a
          stop for what is held.

        Until then every such position was held with no exit and no stop
        until a restart. In the same day's first build the hand-off was to
        the whole broker reconcile, which a run with the reconcile off or
        in mode ``ignore`` never runs (the position stayed held, with no
        exit, until a restart), and which in mode ``block`` counted the
        bot's own open positions and blocked every entry for the rest of
        the day."""
        child = "stop" if child_key == "stop_order_id" else "target"
        dead = {**state, "status": "CANCELED", "is_filled": False, "is_terminal_failure": True}
        bracket.pop("replacement_gone_since", None)
        if sold <= 0 and child == "stop":
            LOG.warning("Bracket stop %s for %s was REPLACED at the broker and nothing entered after it can still "
                        "rest (every later exit order died unfilled); it is retired as a dead stop", child_id, key)
            return dead, None
        self._drop_gone_child(bracket, child_key, child_id)
        if sold <= 0:
            LOG.warning("Bracket target %s for %s was REPLACED at the broker and nothing entered after it can still "
                        "rest (every later exit order died unfilled); the record tracks no target from here, and "
                        "the engine owns it", child_id, key)
            self._save_reconcile_metadata()
            return dead, None
        bracket.pop("replacement_misses", None)
        position.metadata["settle_pending"] = True
        self.settle_wanted = True
        LOG.error("%s: its %s order %s was REPLACED at the broker and nothing entered after it can still rest, but "
                  "the order(s) entered after it sold %d share(s), and which of them replaced it is not known. "
                  "Nothing is booked from them; the position is held for its settle, which compares it with the "
                  "account", key, child, child_id, int(sold))
        self._save_reconcile_metadata()
        return None, None

    @staticmethod
    def _drop_gone_child(bracket: dict[str, Any], child_key: str, child_id: str) -> None:
        """Take *child_id*, a protective order nothing of which rests any
        more (``_replaced_child_gone``), off *bracket*: its child key, its
        entry in ``child_order_ids``, and the wrapper id when it was its
        own wrapper; it is noted among the orders the record left behind.
        A dropped stop is recorded as a dead one (``dead_stop_status``), as
        the retire records one it dropped. The record stays active only
        while it tracks another order (a bracket's other leg, its OCO): a
        record marked down with a leg still resting left that leg
        untracked (the third review's probe)."""
        bracket[child_key] = None
        bracket["child_order_ids"] = [str(oid) for oid in bracket.get("child_order_ids") or [] if str(oid) != child_id]
        if str(bracket.get("protective_order_id") or "") == child_id:
            bracket["protective_order_id"] = None
        note_replaced_order(bracket, child_id)
        child = "stop" if child_key == "stop_order_id" else "target"
        if child == "stop":
            bracket["dead_stop_status"] = "CANCELED"
        bracket["state"] = f"{child}_replaced"
        if not bracket_order_ids(bracket):
            bracket["active"] = False

    def _book_replaced_child_fills(self, key: str, position: Position, bracket: dict[str, Any], child_key: str,
                                   child_id: str, state: dict[str, Any], bars) -> bool:
        """Book what a REPLACED protective order (``_follow_replaced_child``)
        filled itself and the record has not booked: the fills beyond its
        ledger entry, priced as their own (``unbooked_fill_price``; the
        level it rested at when the broker gives no price, as the fill
        reconcile books one), as the exit of its kind. The ledger takes them
        before the booking, so a booking that raises is not made twice. A
        booking that closes the position leaves its replacement, and what
        else the record tracked, to the post-close sweep that booking runs
        (``_sweep_exit_orders``: every exit order still working on the
        symbol, retried until it is down). False, with nothing booked, when
        no price is available for them."""
        booked_qty, booked_notional, booked_estimated = booked_child_part(bracket, child_id)
        filled = int(state.get("filled_qty") or 0)
        if filled <= booked_qty:
            return True
        unbooked = filled - booked_qty
        reason = protective_stop_reason(bracket) if child_key == "stop_order_id" else "broker_target"
        fill_price, fill_price_estimated = unbooked_fill_price(
            filled, safe_float(state.get("fill_price"), None, finite=True),
            state.get("fill_price_estimated") is not False, booked_qty, booked_notional, booked_estimated,
            what=f"{reason} order {child_id} for {key}")
        level = bracket.get("stop_price") if child_key == "stop_order_id" else bracket.get("target_price")
        exit_price = fill_price if fill_price is not None else safe_float(level, None, finite=True)
        if exit_price is None:
            return False
        add_booked_child_fill(bracket, child_id, unbooked, fill_price, fill_price_estimated)
        LOG.warning("Bracket %s order %s for %s was REPLACED at the broker having filled %s share(s); booking the %s "
                    "not booked yet", reason, child_id, key, filled, unbooked)
        self._book_broker_exit(
            key, position, self._held_part_of_fill(key, position, unbooked, f"{reason} order {child_id}"),
            float(exit_price), ExitDecision(reason, "risk"), bars,
            result_message="bracket_child_filled_before_replace", attempt_status="broker_bracket",
            fill_price_estimated=fill_price_estimated, exit_limits_missed=None,
        )
        return True

    def _size_followed_children(self, key: str, position: Position, bracket: dict[str, Any],
                                followed: dict[str, dict[str, Any]], symbol: str) -> None:
        """Put right the size of the replacements ``_follow_replaced_child``
        took for *bracket*'s children (*followed*: child key to the
        replacement's state), as an adopted stop of another size is put
        right (``SchwabExecutor.ensure_position_protected``).

        A replacement that may still rest should rest the shares no working
        exit order covers. One that rests more (the quantity was changed in
        the app), or whose size does not read, would sell more than is
        held, so the children are re-issued at once at that size
        (``resize_bracket_children``, as the bot's own orders, at the
        levels the record holds: the price a stop was moved to, just
        recorded): beside a working exit order too, where a stop left
        larger would sell its shares a second time. One that rests fewer is
        left as it is, logged, the record's ``qty`` saying what rests: the
        bot enlarges no order it did not size (shares sold by hand leave
        its own count too high until the next broker reconcile). When a
        resize is
        not confirmed the record reads ``qty_mismatch`` (``qty_unverified``
        when the size does not read) at the size it rests: at ERROR for a
        bracket, and for a disaster stop as a miss, so
        ``ensure_disaster_stop`` resizes it again a retry interval later, or
        cancels it and places one. Outside the regular session, where the
        broker takes no STOP, a disaster stop's record is only marked, and
        the first regular-session pass resizes it.

        One that has filled shares the record has not booked is left as it
        is: it is executing, and its fill is booked when it completes. One
        that filled or died is the fill booking's or the retire's."""
        expected = int(position.qty) - working_exit_outstanding_qty(position)
        wrong: list[tuple[str, int | None]] = []
        right = 0
        for child_key, state in followed.items():
            if order_status_class(state.get("status")) != ORDER_LIVE:
                continue
            filled = int(state.get("filled_qty") or 0)
            if filled > booked_child_part(bracket, bracket[child_key])[0]:
                continue
            remaining, leg_qty = state.get("remaining_qty"), state.get("leg_qty")
            resting = remaining if remaining is not None else None if leg_qty is None else int(leg_qty) - filled
            if resting is None or int(resting) != expected:
                wrong.append((child_key, None if resting is None else int(resting)))
            else:
                right += 1
        if expected < 1:
            return
        if not wrong:
            if right == len(followed):
                # What the record says rests is what does: the replacement
                # holds what the original had left.
                recorded = bracket.get("qty")
                bracket["qty"] = expected
                if self._clear_missized(bracket) or recorded != expected:
                    self._save_reconcile_metadata()
            return
        resting = wrong[0][1]
        bracket["qty"] = resting
        missized = "qty_mismatch" if resting is not None else "qty_unverified"
        named = ", ".join(f"{'stop' if child_key == 'stop_order_id' else 'target'} {bracket[child_key]} rests "
                          f"{'an unknown number of' if size is None else size}" for child_key, size in wrong)
        if all(size is not None and size < expected for _child_key, size in wrong):
            # Fewer than it should cover: left as it was sized at the
            # broker. The bot's own count is too high once shares were sold
            # by hand (until the next broker reconcile), and enlarging the
            # stop to it would rest more than the account holds. The
            # engine's own stop still covers every share while the bot runs.
            LOG.warning("%s: the replacement followed rests fewer than the %s share(s) it should cover (%s); it "
                        "is left as it was sized at the broker, and the record says so", key, expected, named)
            self._save_reconcile_metadata()
            return
        disaster = is_disaster_stop(bracket)
        if disaster and not self.executor.regular_session_open():
            bracket["state"] = missized
            LOG.warning("Disaster stop for %s: the replacement followed is of another size than the %s share(s) it "
                        "should cover (%s); it is resized by the first regular-session pass", key, expected, named)
            self._save_reconcile_metadata()
            return
        resized, message = self.executor.resize_bracket_children(
            bracket, symbol, position.side, expected, "NORMAL" if disaster else str(bracket.get("session") or "NORMAL"),
            initial_risk=None if disaster else initial_risk_per_unit(position))
        if resized:
            LOG.warning("%s: the replacement followed was of another size than the %s share(s) it should cover "
                        "(%s); resized (%s)", key, expected, named, message)
            self._clear_missized(bracket)
            self._save_reconcile_metadata()
            return
        bracket["state"] = missized
        detail = (f"the replacement followed is of another size than the {expected} share(s) it should cover "
                  f"({named}) and could not be resized ({message})")
        if disaster:
            self._disaster_stop_missed(key, bracket, int(bracket.get("failed_attempts") or 0) + 1,
                                       sessions.now_et(), logging.ERROR, detail)
            return
        LOG.error("%s: %s -- a resting exit larger than the position would flip it on trigger", key, detail)
        self._save_reconcile_metadata()

    @classmethod
    def _clear_missized(cls, bracket: dict[str, Any]) -> bool:
        """A disaster stop's record that read as resting another size
        (``_DISASTER_STOP_MISSIZED_STATES``) rests the right one: it reads
        ``adopted``, with no retry time, as ``ensure_disaster_stop`` leaves
        one it resized. True when it did read so. Without this a record
        marked while its position was held
        (``late_fills_wait_for_replacement``) had ``ensure_disaster_stop``
        send a second, identical resize in the pass that sized the
        replacement."""
        if not is_disaster_stop(bracket) or bracket.get("state") not in cls._DISASTER_STOP_MISSIZED_STATES:
            return False
        bracket["state"] = "adopted"
        bracket.pop("attempted_at", None)
        return True

    def late_fills_wait_for_replacement(self, key: str, position: Position) -> bool:
        """True when late entry fills grew *key* while it is held for a
        protective order the broker lists REPLACED whose replacement is not
        found (``_replacement_unresolved``): its resize is not sent
        (``EntryGatekeeper._grow_position``, 2026-11-11). The record still
        names the REPLACED order, and a replace sent to it is refused: an
        ERROR, and for a disaster stop a miss toward ``DISASTER STOP
        DEGRADED`` that no stop's failure earned.

        A disaster stop's record reads ``qty_mismatch`` from here, with no
        miss counted and no retry time: once the hold ends,
        ``ensure_disaster_stop`` resizes the stop to the shares held on the
        first pass, as it does after a refused resize
        (``disaster_stop_resize_refused``). A bracket's children keep the
        size they rest, as a refused resize leaves them, and that is logged
        at ERROR."""
        if not self._replacement_unresolved(position):
            return False
        record = position.metadata["bracket"]
        if is_disaster_stop(record):
            record["state"] = "qty_mismatch"
            LOG.warning("Disaster stop for %s: late entry fills grew the position to %s while its stop reads "
                        "REPLACED at the broker and the replacement is not found; no resize is sent to the "
                        "replaced order, and the stop is resized once its replacement is tracked", key, position.qty)
        else:
            LOG.error("%s: late entry fills grew the position to %s while a protective order of its bracket reads "
                      "REPLACED at the broker and the replacement is not found; no resize is sent to the replaced "
                      "order, and its children rest the size they had", key, position.qty)
        return True

    def _note_moved_backup_level(self, key: str, position: Position, bracket: dict[str, Any],
                                 stop_state: dict[str, Any] | None) -> None:
        """Keep, as the position's backup level, the price its disaster
        stop was moved to at the broker (2026-11-11).

        The bot rests a disaster stop at the position's stamped price
        (``DISASTER_STOP_PRICE_KEY``) and never moves it, so one the broker
        lists working at another level was moved there by hand: in the app
        while the bot ran (the record's level follows it,
        ``_track_resting_levels``), or while it was down (a restore adopts
        the stop as it rests). Until then only the record kept that level,
        and every stop placed afresh (after a partial exit, a cancel, a
        refused resize: ``ensure_disaster_stop``, ``_reprotect_remainder``,
        the restore's re-protect) went back to the stamped price. The
        stamped price is moved to it, the bot's own kept beside it
        (``DISASTER_STOP_OWN_PRICE_KEY``) for a moved price the market is
        through when a stop has to be placed
        (``_drop_moved_level_if_through``); a stop moved back to the bot's
        own level drops that key.

        Only a plain stop that is working and whose level reads: a
        trailing stop's level moves by itself, an order of another kind
        has none, and a filled or dead one is the booking's or the
        retire's. A bracket's stop follows the engine's level and is not
        touched."""
        if not is_disaster_stop(bracket) or not isinstance(stop_state, dict) or not isinstance(position.metadata, dict):
            return
        if stop_state.get("is_filled") or stop_state.get("is_terminal_failure") \
                or stop_state.get("order_type") not in STOP_ORDER_TYPES:
            return
        resting = safe_float(stop_state.get("stop_price"), None, finite=True)
        stamped = safe_float(position.metadata.get(DISASTER_STOP_PRICE_KEY), None, finite=True)
        if resting is None or resting <= 0 or stamped is None:
            return
        if abs(resting - self.executor.protective_stop_level(position.side, stamped)) < 1e-9:
            return
        own = safe_float(position.metadata.get(DISASTER_STOP_OWN_PRICE_KEY), stamped, finite=True)
        position.metadata[DISASTER_STOP_PRICE_KEY] = resting
        if abs(resting - self.executor.protective_stop_level(position.side, own)) < 1e-9:
            position.metadata.pop(DISASTER_STOP_OWN_PRICE_KEY, None)
            LOG.warning("Disaster stop for %s rests at %s, the bot's own level again; that is its backup level",
                        key, resting)
        else:
            position.metadata[DISASTER_STOP_OWN_PRICE_KEY] = own
            LOG.warning("Disaster stop for %s rests at %s at the broker, not at the bot's own level %s (moved in "
                        "the app, or adopted where an earlier run left it); a stop placed afresh for it rests at %s "
                        "from here", key, resting, own, resting)
        self._save_reconcile_metadata()

    def _drop_moved_level_if_through(self, position: Position) -> None:
        """Before a disaster stop is placed afresh: give up a backup
        level moved at the broker (``_note_moved_backup_level``) that the
        last price is already through, for the bot's own, from here on
        (2026-11-11).

        A moved level can sit closer to the market than the bot's own. A
        stop sent at a level the price is through is refused by the
        broker, or sells at once; the bot's own level, further away, still
        rests. The price is the paper account's last mark for the symbol,
        which each management pass sets; with none, the moved level stands
        and the broker answers. The caller saves: the placement that
        follows writes the record before it sends anything."""
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        own = safe_float(meta.get(DISASTER_STOP_OWN_PRICE_KEY), None, finite=True)
        moved = safe_float(meta.get(DISASTER_STOP_PRICE_KEY), None, finite=True)
        mark = safe_float(self.account.last_prices.get(position.symbol), None, finite=True)
        if own is None or moved is None or mark is None or mark <= 0:
            return
        if (mark > moved) if position.side == Side.LONG else (mark < moved):
            return
        meta[DISASTER_STOP_PRICE_KEY] = own
        del meta[DISASTER_STOP_OWN_PRICE_KEY]
        LOG.warning("Disaster stop for %s: the last price %s is through the level it was moved to at the broker "
                    "(%s); a stop placed for it rests at the bot's own level %s from here", position.symbol, mark,
                    moved, own)

    @staticmethod
    def _replacement_unresolved(position: Position) -> bool:
        """True while the position's live broker protection tracks an order
        the broker lists REPLACED whose replacement is not found
        (``_follow_replaced_child``, ``_after_follow``), or is waited for
        (``_gone_long_enough``): that replacement may rest, so an
        exit would sell beside it and a placement would rest a second stop.
        Nothing is sent for the position until a pass finds it."""
        bracket = active_broker_bracket(position)
        return bracket is not None and bool(bracket.get("replacement_misses") or bracket.get("replacement_gone_since"))

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

    @staticmethod
    def _settle_pending(position: Position) -> bool:
        """The broker reconcile found the broker holding less than this
        position tracks but could not confirm its bracket down (or read the
        order list its re-protect needs), so the position is held as it was.
        Until its retry settles it, nothing is sent for it: an exit or a
        re-protect would be sized to shares the broker no longer holds
        (2026-09-25). So is a position the follow of a REPLACED stop handed
        to its settle (``_replaced_child_gone``): shares were sold by
        orders the record cannot book, and only the account says what is
        held."""
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
        cancel confirms or they fill. A bracket's stop is re-placed at most
        once per bracket, and never after a REJECTED one (2026-09-25). A
        disaster stop that is REJECTED, or dies again after that one
        re-placement, is owed again (``ensure_disaster_stop`` places it a
        retry interval later) and counts as a miss toward ``DISASTER STOP
        DEGRADED``: until 2026-09-29 the position was left with no disaster
        stop and only a WARNING."""
        status = str((child_state or {}).get("status") or "")
        if order_status_class(status) != ORDER_DEAD:
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
            # stays tracked reports its fills again, cumulatively. Less what
            # is already booked of the child (a stop adopted from an
            # unconfirmed one's lookup), priced as the rest's own fills.
            dead_filled = int((child_state or {}).get("filled_qty") or 0)
            booked_qty, booked_notional, booked_estimated = booked_child_part(bracket, dead_id)
            if dead_filled > booked_qty:
                dead_price, dead_estimated = unbooked_fill_price(
                    dead_filled, safe_float(child_state.get("fill_price"), None, finite=True),
                    child_state.get("fill_price_estimated") is not False, booked_qty, booked_notional,
                    booked_estimated, what=f"dead {child} {dead_id} for {key}")
                self.book_bracket_cancel_fills(
                    key, position, bracket,
                    BracketCancel(False, leftover.message, dead_filled - booked_qty, dead_price,
                                  protective_stop_reason(bracket) if child == "stop" else "broker_target",
                                  dead_estimated),
                    None, bars,
                )
                # The wrapper still lists the dead child; a later cancel of it
                # must not report these again.
                add_booked_child_fill(bracket, dead_id, dead_filled - booked_qty, dead_price, dead_estimated)
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
        # again at once is rejected again, every cycle, and each rejected
        # replacement read as protection that suppressed the engine stop. The
        # same goes for protection this retire already replaced once. A
        # bracket's is left to the engine. A disaster stop is the only stop a
        # position has while the bot is down, so it stays owed: counted as a
        # miss, and placed again a retry interval later (ensure_disaster_stop,
        # 2026-09-29).
        dead_stop = status if child == "stop" else bracket.get("dead_stop_status")
        if (key in self.positions and dead_stop and is_disaster_stop(bracket)
                and (dead_stop == "REJECTED" or bracket.get("replaces_dead_stop"))):
            self._count_disaster_stop_down(
                key, bracket, sessions.now_et(),
                f"its disaster stop {dead_id} was {status} at the broker"
                + (" after replacing one that died" if bracket.get("replaces_dead_stop") else ""),
                retry=True,
            )
        elif (key in self.positions and dead_stop and dead_stop != "REJECTED"
                and not bracket.get("replaces_dead_stop")):
            self._reprotect_beside_working_order(position, working_exit_outstanding_qty(position))
            # Marked whether the fresh stop rests or is still owed: a
            # disaster stop owed the regular session, or refused by the
            # broker, carries it to the stop ensure_disaster_stop places, so
            # that one, when it dies in turn, is owed as a miss rather than
            # re-placed at once.
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
            key, position, self._held_part_of_fill(key, position, int(cancel.filled_qty),
                                                   f"{reason} order(s) before their cancel"),
            float(fill_price), ExitDecision(reason, "risk"), bars,
            result_message="bracket_child_filled_before_cancel", attempt_status="broker_bracket",
            fill_price_estimated=cancel.fill_price is None or cancel.fill_price_estimated, exit_limits_missed=None,
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
    def _track_working_exit(position: Position, *, order_id: str, message: str, decision: ExitDecision,
                            booked_qty: int, booked_notional: float | None, booked_price_estimated: bool,
                            requested_qty: int, bracket_cancelled: bool, exit_limits_missed: int | None) -> None:
        if isinstance(position.metadata, dict):
            position.metadata["working_exit_order"] = {
                "order_id": str(order_id),
                "reason": str(decision.reason),
                "message": str(message),
                "booked_qty": int(booked_qty),
                # What the booked shares were booked at (quantity times the
                # order's price; None when they were booked at no price of
                # the order's), and whether that price was not an execution
                # price: a later fill of the order is priced net of them
                # (broker_payloads.unbooked_fill_price, 2026-10-06).
                "booked_notional": None if booked_notional is None else float(booked_notional),
                "booked_price_estimated": bool(booked_price_estimated),
                "requested_qty": int(requested_qty),
                # The live LIMIT exit attempt's missed-limit count, for the
                # record of the fill that settles the order (2026-09-29).
                "exit_limits_missed": exit_limits_missed,
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
                and not PositionManager._disaster_stop_unconfirmed(position))

    @staticmethod
    def _disaster_stop_unconfirmed(position: Position) -> bool:
        """True when the position's record is a disaster stop sent with an
        unknown outcome (``unconfirmed``): it may rest, or have sold shares,
        so nothing but ``ensure_disaster_stop`` (or the lookup before an
        exit) places a stop after it, once it has looked for it."""
        bracket = position.metadata.get("bracket") if isinstance(position.metadata, dict) else None
        return is_disaster_stop(bracket) and bracket.get("state") == "unconfirmed"

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
        (2026-09-24). With none to adopt, a backup level moved at the
        broker that the last price is through gives way to the bot's own
        first (``_drop_moved_level_if_through``)."""
        if active_broker_bracket(position) is None:
            self._drop_moved_level_if_through(position)
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

    # A disaster stop's record states that rest a stop of another size than
    # the position: one whose resize the broker refused (``qty_mismatch``: an
    # adopted stop larger than the position, or one smaller once late entry
    # fills grew it) or whose size could not be read (``qty_unverified``).
    _DISASTER_STOP_MISSIZED_STATES = frozenset({"qty_mismatch", "qty_unverified"})
    # The records a dead stop's retire leaves (``stop_<status>``).
    _DISASTER_STOP_DEAD_STATES = frozenset({"stop_rejected", "stop_expired", "stop_canceled", "stop_cancelled"})

    @classmethod
    def _disaster_stop_owed(cls, record: Any) -> bool:
        """True when a live position's disaster stop record rests no stop of
        the position's size: no record yet, one that rests nothing (owed the
        regular session, refused, unconfirmed, cancelled for an exit that did
        not follow, dead, or filled in part), or one of another size. A dry
        run's (simulated) is never owed."""
        if not isinstance(record, dict):
            return True
        if record.get("simulated"):
            return False
        return not record.get("active") or record.get("state") in cls._DISASTER_STOP_MISSIZED_STATES

    def ensure_disaster_stop(self, key: str, position: Position, bars) -> None:
        """Rest an equity position's disaster stop at the broker when it is
        owed one (``execution.disaster_stop_enabled``, ``_disaster_stop_owed``):
        right after its entry fills (the entry gatekeeper), and on each cycle
        that decides no exit for it (``_manage_position``). *bars* are the
        cycle's, for a fill the lookup books.

        - Outside the regular session (Schwab rejects a STOP there) nothing is
          sent or saved: a new position gets a ``pending_session`` record,
          and the first regular-session cycle places its stop. An unconfirmed
          one is still looked up.
        - A record that carries its last attempt's time (``attempted_at``: a
          refused or unconfirmed submit, an order list that could not be
          read, a stop of another size left as it was, a stop the broker
          rejected or that died again) waits ``DISASTER_STOP_RETRY_SECONDS``
          after it.
        - Unconfirmed (a submit with an unknown outcome, or a restore that
          could not read its stop): ``_look_up_unconfirmed_stop`` first. A
          stop that filled is booked (the position is closed, or the rest is
          protected); one in any status but a terminal one is adopted
          (resized to what is held); one REPLACED at the broker is followed
          to its replacement, and none is placed while that cannot be found;
          a dead one, or none after the retry interval, lets one be placed.
        - Of another size (``qty_mismatch`` / ``qty_unverified``: an adopted
          stop larger than the position, or one smaller once late entry
          fills grew it, ``disaster_stop_resize_refused``): the resize is
          tried again, then the stop is cancelled and one placed at the held
          size; one that neither resizes nor cancels is a miss.
        - Down with no exit working (``canceled`` by the cancel before an
          exit that did not follow, a stop that died and was not re-placed at
          once): placed again at once, and counted as a miss. A stop the
          broker rejected, or that died again, was counted when it went down
          (``_retire_dead_bracket``).
        - Every placement adopts an exit stop no record tracks that may
          still work on the symbol for the side, in any status but a
          terminal one (``SchwabExecutor.ensure_position_protected``), rather
          than place a second beside it.

        Before anything is sent the record reads ``unconfirmed`` at the price
        and quantity about to be sent, and is saved. Whatever raises after
        that (here, or in the entry pass that called it) leaves a stop the
        next attempt looks for before it places another: an order write is
        never assumed not to have landed.

        Each attempt that leaves the position without its stop in the
        regular session counts as a miss (``failed_attempts`` on the record),
        until the broker lists the stop working (the fill reconcile clears
        it); every ``execution.disaster_stop_escalation_attempts``-th
        consecutive one is logged at CRITICAL (``_escalate_disaster_stop``).
        A dry run places nothing: its record is the simulated one."""
        if not isinstance(position.metadata, dict) or is_option_asset(position.metadata) \
                or not self.executor.disaster_stop_enabled():
            return
        record = position.metadata.get("bracket")
        if not self._disaster_stop_owed(record):
            return
        symbol = str(position.metadata.get("underlying") or position.symbol)
        if self.config.schwab.dry_run:
            # Nothing is sent, so no outcome is unknown and nothing is looked up.
            position.metadata["bracket"] = self.executor.ensure_position_protected(
                symbol, int(position.qty), position.side, self.executor.resting_stop_price(position), None,
                initial_risk=None,
            )
            self._save_reconcile_metadata()
            return
        state = record.get("state") if isinstance(record, dict) else None
        if state != "unconfirmed" and not self.executor.regular_session_open():
            # Schwab rejects a STOP outside the regular session: nothing is
            # sent, and nothing is written each premarket cycle (2026-09-29).
            if record is None:
                position.metadata["bracket"] = self._pending_disaster_stop(position)
            return
        now = sessions.now_et()
        if isinstance(record, dict) and record.get("attempted_at") is not None \
                and (now - datetime.fromisoformat(str(record["attempted_at"]))).total_seconds() \
                < DISASTER_STOP_RETRY_SECONDS:
            return
        missed = int(record.get("failed_attempts") or 0) if isinstance(record, dict) else 0
        carried = {mark: record[mark] for mark in ("replaces_dead_stop",)
                   if isinstance(record, dict) and record.get(mark)}
        known: dict[str, Any] | None = None
        if state == "unconfirmed":
            outcome, known = self._look_up_unconfirmed_stop(key, position, record, now, bars)
            if outcome in ("unreadable", "unresolved"):
                self._disaster_stop_missed(
                    key, position.metadata["bracket"], missed + 1, now, logging.WARNING,
                    "the day's orders could not be read to look for its unconfirmed stop; none is placed until "
                    "they are" if outcome == "unreadable" else
                    "its unconfirmed stop was REPLACED at the broker and its replacement is not found among the "
                    "day's orders; none is placed until it is",
                )
                return
            if key not in self.positions:
                return
            if known is None and not self.executor.regular_session_open():
                # Not resting, and none can be placed before the open.
                position.metadata["bracket"] = self._pending_disaster_stop(position, missed=missed, **carried)
                self._save_reconcile_metadata()
                return
        elif state in self._DISASTER_STOP_MISSIZED_STATES:
            resized, message = self.executor.resize_bracket_children(record, symbol, position.side, int(position.qty),
                                                                      "NORMAL", initial_risk=None)
            if resized:
                record["state"] = "adopted"
                record.pop("attempted_at", None)
                LOG.warning("Disaster stop for %s: its stop now rests the %s shares held (%s)", key, position.qty,
                            message)
                self._save_reconcile_metadata()
                return
            cancel = self.executor.cancel_bracket(record)
            resting = record.get("qty")
            if not cancel.ok:
                self._disaster_stop_missed(
                    key, record, missed + 1, now, logging.ERROR,
                    f"its stop rests {'an unknown number of' if resting is None else resting} shares against "
                    f"{position.qty} held, and neither its resize ({message}) nor its cancel ({cancel.message}) "
                    "was confirmed; "
                    + (f"the other {int(position.qty) - int(resting)} have no broker stop"
                       if resting is not None and int(resting) < int(position.qty)
                       else "it would sell more than is held"),
                )
                return
            self.book_bracket_cancel_fills(key, position, record, cancel, None, bars)
            if key not in self.positions:
                return
            LOG.warning("Disaster stop for %s: cancelled the stop resting %s shares against the %s held, after its "
                        "resize was refused again (%s); placing one at the held size", key,
                        "an unknown number of" if resting is None else resting, position.qty, message)
        elif state == "canceled" or (state in self._DISASTER_STOP_DEAD_STATES and state != "stop_rejected"
                                     and not record.get("replaces_dead_stop")):
            # Down, and not counted when it went down: the retire counts the
            # stops it leaves owed (a REJECTED one, one that died again).
            missed = self._count_disaster_stop_down(
                key, record, now, f"its disaster stop is down ({state}) with no exit working", retry=False,
            )
        if known is None:
            # A stop goes in afresh: at the level it was moved to at the
            # broker, unless the last price is through that one.
            self._drop_moved_level_if_through(position)
        level = self.executor.protective_stop_level(position.side, self.executor.resting_stop_price(position))
        position.metadata["bracket"] = {
            "parent_order_id": None, "sync_mode": DISASTER_SYNC_MODE, "legs": "stop_only", "session": "NORMAL",
            "stop_price": level, "target_price": None, "qty": int(position.qty),
            **dict.fromkeys(BRACKET_ID_KEYS), "child_order_ids": [],
            "active": False, "state": "unconfirmed", "attempted_at": now.isoformat(), "sent_at": now.isoformat(),
            "failed_attempts": missed + 1, **carried,
        }
        self._save_reconcile_metadata()
        # The stop the lookup found is adopted as it listed it, with the
        # fills it booked: a later cancel of it reports them again,
        # cumulatively (SchwabExecutor._adoptable_protection).
        fresh = self.executor.ensure_position_protected(
            symbol, int(position.qty), position.side, self.executor.resting_stop_price(position), None,
            initial_risk=None, known_bracket=known,
        )
        self._record_disaster_stop_attempt(key, position, fresh, missed, carried, now)

    def _pending_disaster_stop(self, position: Position, *, missed: int = 0, **carried: Any) -> dict[str, Any]:
        """The record of a disaster stop owed the regular session: its level
        and the shares held, nothing resting."""
        record = {
            "parent_order_id": None, "sync_mode": DISASTER_SYNC_MODE, "legs": "stop_only", "session": "NORMAL",
            "stop_price": self.executor.protective_stop_level(position.side,
                                                              self.executor.resting_stop_price(position)),
            "target_price": None, "qty": int(position.qty), **dict.fromkeys(BRACKET_ID_KEYS),
            "child_order_ids": [], "active": False, "state": "pending_session", **carried,
        }
        if missed:
            record["failed_attempts"] = missed
        return record

    def _record_disaster_stop_attempt(self, key: str, position: Position, fresh: dict[str, Any], missed: int,
                                      carried: dict[str, Any], now: datetime) -> None:
        """Keep what a placement (or adoption) returned as the position's
        record, and count it: one that rests nothing, or rests more than is
        held, is a miss; one that rests keeps the count so far until the
        broker lists it working."""
        fresh.update(carried)
        state = fresh.get("state")
        if state in ("unprotected", "unconfirmed") or state in self._DISASTER_STOP_MISSIZED_STATES:
            fresh["failed_attempts"] = missed + 1
            fresh.setdefault("attempted_at", now.isoformat())
            self._escalate_disaster_stop(key, missed + 1, f"its last attempt ended {state}")
        elif missed:
            fresh["failed_attempts"] = missed
        position.metadata["bracket"] = fresh
        self._save_reconcile_metadata()

    def _look_up_unconfirmed_stop(self, key: str, position: Position, record: dict[str, Any], now: datetime,
                                  bars) -> tuple[str, dict[str, Any] | None]:
        """Find the stop an unconfirmed record sent among the day's orders, in
        whatever state it is now (``broker_payloads.sent_exit_stop``: the
        exit STOP at exactly the price and quantity sent, entered from
        ``DISASTER_STOP_RETRY_SECONDS`` before the submit on: ``sent_at``,
        which every unconfirmed record carries and a miss never moves). A
        stop REPLACED at the broker (moved in the app) is followed to its
        replacement (``broker_payloads.replacement_order``), which is then
        the stop looked at. Its status is read failing closed
        (``order_status_class``). Returns ``(outcome, stub)``:

        - ``unreadable``: the orders could not be read; nothing is decided.
        - ``unresolved``: it was REPLACED and its replacement is not listed,
          or cannot be told apart from another exit stop; nothing is
          decided, since the replacement may rest. The position's record
          stays unconfirmed, with what was booked (``booked_child_fills``).
        - ``closed``: it filled, and booking that closed the position.
        - ``working``: it may still rest (any status but a terminal one:
          PENDING_CANCEL, NEW, AWAITING_RELEASE_TIME and an unknown or
          missing one included); *stub* is for the caller to adopt as this
          read lists it (``broker_payloads.listed_stop``, with the fills
          booked here), never read again.
        - ``absent``: it never landed, or it filled or died: nothing of it
          rests.

        Whatever it filled (in part or in full, working or dead since, or
        before it was replaced) and the record has not booked yet
        (``booked_child_fills``) is booked first as a ``disaster_stop`` exit
        at the broker's price for those fills (the order's average net of
        what was booked: ``broker_payloads.unbooked_fill_price``; the level
        sent when the broker gives none, as the fill reconcile does): those
        shares are gone. A fill larger than
        the position is booked as the whole of it and logged at CRITICAL
        (``_held_part_of_fill``). Until 2026-09-29 only working orders were
        read, so a stop that landed and then filled was never booked, and a
        full-size stop was placed for shares already sold; and a stop in a
        status outside ``WORKING_STATUSES``, or one moved in the app, read as
        dead."""
        since = datetime.fromisoformat(str(record["sent_at"])) - timedelta(seconds=DISASTER_STOP_RETRY_SECONDS)
        orders = self.executor.todays_orders(since)
        if orders is None:
            return "unreadable", None
        symbol = str(position.metadata.get("underlying") or position.symbol)
        match = sent_exit_stop(orders, symbol, position.side, stop_price=record.get("stop_price"), qty=record.get("qty"))
        if match is None:
            return "absent", None
        # What is booked of each order, and at what (broker_payloads.booked_child_part).
        booked = booked_fills_ledger(record)
        followed: set[str] = set()
        while True:
            order_id = str(match["orderId"])
            followed.add(order_id)
            status = order_status_class(match["status"])
            filled = order_row_filled_qty(match)
            booked_qty, booked_notional, booked_estimated = booked_child_part(booked, order_id)
            if filled > booked_qty:
                unbooked = filled - booked_qty
                # The level sent, or the order's own price: not an execution
                # price (broker_payloads.order_fill_price_estimated). Priced
                # as the unbooked shares' own fills: the order's average
                # covers the booked ones too.
                fill_price, fill_price_estimated = unbooked_fill_price(
                    filled, safe_float(match.get("fillPrice"), None, finite=True),
                    match.get("fillPriceEstimated") is not False, booked_qty, booked_notional, booked_estimated,
                    what=f"disaster stop {order_id} for {key}")
                add_booked_child_fill(booked, order_id, unbooked, fill_price, fill_price_estimated)
                # Re-pointed at the order before the booking: a close's sweep
                # must not take this fill for one that landed beside the exit.
                position.metadata["bracket"] = {**record, "stop_order_id": order_id, "child_order_ids": [order_id],
                                                **booked_fills_ledger(booked), "active": False,
                                                "state": f"filled:{DISASTER_STOP_REASON}"}
                LOG.warning("Disaster stop %s for %s, sent with an unknown outcome, is %s having filled %s share(s); "
                            "booking the %s not booked yet", order_id, key, match["status"], filled, unbooked)
                self._book_broker_exit(
                    key, position, self._held_part_of_fill(key, position, unbooked, f"disaster stop {order_id}"),
                    float(fill_price if fill_price is not None else record["stop_price"]),
                    ExitDecision(DISASTER_STOP_REASON, "risk"), bars,
                    result_message=f"unconfirmed_disaster_stop_filled:{match['status']}",
                    attempt_status="broker_bracket", fill_price_estimated=fill_price_estimated,
                    exit_limits_missed=None,
                )
                if key not in self.positions:
                    return "closed", None
            if status == ORDER_LIVE:
                return "working", {**listed_stop(match), **(booked_fills_ledger(booked) if booked else {})}
            if status != ORDER_REPLACED:
                LOG.warning("Disaster stop %s for %s, sent with an unknown outcome, is %s at the broker", order_id, key,
                            match["status"])
                return "absent", None
            replacement = replacement_order(orders, match, symbol, position.side)
            if replacement is None or str(replacement["orderId"]) in followed:
                LOG.error("Disaster stop %s for %s, sent with an unknown outcome, was REPLACED at the broker, and its "
                          "replacement (%s) is not found among the day's orders; none is placed beside it until it "
                          "is", order_id, key, match.get("replacementId") or "not named")
                # Still unconfirmed, with what is booked, so the next lookup
                # books only what is new.
                position.metadata["bracket"] = {**record, **booked_fills_ledger(booked)} if booked else record
                return "unresolved", None
            LOG.warning("Disaster stop %s for %s, sent with an unknown outcome, was REPLACED at the broker by order "
                        "%s; that one is taken as the stop sent", order_id, key, replacement["orderId"])
            match = replacement

    def disaster_stop_resize_refused(self, key: str, position: Position, message: str) -> None:
        """Late entry fills grew *key* past the shares its disaster stop
        rests, and its resize to what is held was not confirmed (*message*;
        ``EntryGatekeeper._grow_position``). The record reads
        ``qty_mismatch`` at the size it rests, so it is owed like any stop
        of another size (``ensure_disaster_stop`` resizes it again a retry
        interval later, then cancels it and places one at the held size),
        and the shares beyond it, which have no broker stop, count as a miss
        toward ``DISASTER STOP DEGRADED`` (2026-09-29). Until then the record
        stayed ``disaster_stop``, and the stop rested fewer shares than were
        held for the rest of the trade, never retried or escalated."""
        record = position.metadata["bracket"]
        record["state"] = "qty_mismatch"
        self._disaster_stop_missed(
            key, record, int(record.get("failed_attempts") or 0) + 1, sessions.now_et(), logging.ERROR,
            f"its stop rests {record.get('qty')} shares against the {position.qty} held after late entry fills, "
            f"and its resize was not confirmed ({message}); the rest have no broker stop until a retry resizes "
            "it, or cancels it and places one at the held size",
        )

    def _disaster_stop_missed(self, key: str, record: dict[str, Any], missed: int, now: datetime, level: int,
                              detail: str) -> None:
        """An attempt at a disaster stop that placed nothing: log *detail*,
        count the miss on *record*, and wait the retry interval."""
        record["attempted_at"] = now.isoformat()
        record["failed_attempts"] = missed
        LOG.log(level, "Disaster stop for %s: %s", key, detail)
        self._escalate_disaster_stop(key, missed, detail)
        self._save_reconcile_metadata()

    def _count_disaster_stop_down(self, key: str, record: dict[str, Any], now: datetime, detail: str, *,
                                  retry: bool) -> int:
        """A disaster stop that went down with the position still held and
        no exit working (``detail`` says how): logged at ERROR and counted
        as a miss on *record*. With *retry* the next placement waits the
        retry interval (a stop the broker rejected is likely rejected again
        at once); without it the next one goes out now. Returns the count."""
        missed = int(record.get("failed_attempts") or 0) + 1
        record["failed_attempts"] = missed
        if retry:
            record["attempted_at"] = now.isoformat()
        LOG.error("Disaster stop for %s: %s, so the position has no broker stop; it is placed again %s", key, detail,
                  "after the retry interval" if retry else "now")
        self._escalate_disaster_stop(key, missed, detail)
        self._save_reconcile_metadata()
        return missed

    def _escalate_disaster_stop(self, key: str, count: int, detail: str) -> None:
        """Log at CRITICAL when *key* has gone *count* consecutive attempts
        without its disaster stop resting where it should (an owed stop not
        placed, one that went down, one resting more than is held, or a
        cancel before an exit not confirmed, which defers the exit): on
        every ``execution.disaster_stop_escalation_attempts``-th. Each miss
        is logged on its own line too; this is the one to page on."""
        if count % int(self.config.execution.disaster_stop_escalation_attempts) == 0:
            LOG.critical("DISASTER STOP DEGRADED — %s: %d consecutive attempts missed. Last: %s", key, count, detail)

    @property
    def owed_sweeps_unread(self) -> bool:
        """True while the post-close sweeps an earlier run saved could not
        be read (``load_owed_sweeps``): which symbols still owe one is not
        known yet. The read is tried again once a minute."""
        return self._owed_sweeps_unread_since is not None

    def exit_sweep_owed(self, symbol: str) -> bool:
        """True while a closed position in *symbol* still owes its post-close
        sweep (``_sweep_exit_orders``): the entry gatekeeper opens nothing in
        it until the sweep has read the orders, so a stop the closed one left
        never sells a new one's shares. A position that opens in it all the
        same (an unsettled entry order's late fill, which no signal decides)
        keeps its orders: the sweep never cancels one an open position
        tracks (``_run_exit_sweep``)."""
        wanted = str(symbol).upper().strip()
        return any(str(owed["symbol"]).upper().strip() == wanted for owed in self._sweeps_owed.values())

    def exit_sweep_symbols(self) -> list[str]:
        """The symbols whose closed positions still owe their post-close
        sweep (``_sweep_exit_orders``; its orders could not be read, or a leftover's cancel
        is not confirmed): exit orders may still work there, so
        the engine holds the day open for them (``IntradayBot._day_held_open``)
        and management keeps running the sweep."""
        return sorted({str(owed["symbol"]).upper().strip() for owed in self._sweeps_owed.values()})

    def run_owed_sweeps(self, now: datetime, *, paced: bool) -> None:
        """Try the post-close sweeps still owed (``_run_exit_sweep``) off a
        management pass, while the broker takes orders (the stream window,
        07:00-20:00 ET on a trading day), for a strategy that holds across
        sessions; nothing otherwise (2026-11-10).

        ``manage_positions`` tries an owed sweep at the start of each
        management pass, and those end with the regular session. The
        leftover such a strategy can owe is a good-till-cancelled stop: one
        whose read or cancel failed in the session's last minutes rested
        through the night and was live at 09:30:00, ahead of the first
        management pass, against a position no longer held. A DAY leftover
        died at the close, so every other strategy keeps the management
        pass's retry alone.

        The engine calls it on each pass that manages no position (*paced*:
        one try every ``OWED_SWEEP_RETRY_SECONDS`` at most), and once ahead
        of each broker reconcile (not paced): the reconcile of a new day
        would otherwise read the leftover as a foreign order and block the
        day's entries, minutes before the sweep cancelled it."""
        if not ((self._sweeps_owed or self.owed_sweeps_unread) and self.config.active_holds_across_sessions):
            return
        if not sessions.equity_session_state(
                now, extended_hours_enabled=bool(self.config.execution.extended_hours_enabled)).stream_available:
            return
        tried_at = self._owed_sweeps_tried_at
        if paced and tried_at is not None and time.monotonic() - tried_at < OWED_SWEEP_RETRY_SECONDS:
            return
        self._owed_sweeps_tried_at = time.monotonic()
        self._try_owed_sweeps()

    def _try_owed_sweeps(self) -> None:
        """Try every post-close sweep still owed (``_run_exit_sweep``), then
        save what is owed when it changed (``_save_owed_sweeps``). A store
        whose owed sweeps could not be read at start-up is read again
        first, once every ``OWED_SWEEP_RETRY_SECONDS``."""
        unread = self._owed_sweeps_unread_since
        if unread is not None and time.monotonic() - unread >= OWED_SWEEP_RETRY_SECONDS:
            self.load_owed_sweeps()
        for key in list(self._sweeps_owed):
            self._run_exit_sweep(key)
        self._save_owed_sweeps()

    @staticmethod
    def _serialize_owed_sweep(owed: dict[str, Any]) -> dict[str, Any]:
        """An owed sweep as the state store keeps it: what a restart needs
        to run it again. The consecutive-miss count and the wait of a
        loaded one are this run's and are not kept."""
        sent = owed["sent"]
        return {
            "symbol": str(owed["symbol"]), "side": owed["side"].value,
            "sent": None if sent is None else {**sent, "sent_at": sent["sent_at"].isoformat()},
            "reported": sorted(str(oid) for oid in owed["reported"]),
            "fills_logged": int(owed.get("fills_logged") or 0), "owed_at": owed.get("owed_at"),
        }

    @staticmethod
    def _deserialize_owed_sweep(raw: Any) -> dict[str, Any]:
        """The owed sweep a saved row (``_serialize_owed_sweep``) stands
        for, marked ``loaded``: it was owed by an earlier run
        (``_loaded_sweep_refusal``). Raises on a row that does not read:
        one whose JSON did not parse (the store hands its text back,
        ``ReconcileMetadataStore.load_owed_sweeps``), one that is no
        record, or one that lacks a field."""
        if not isinstance(raw, dict):
            raise ValueError(f"not a saved record: {str(raw)[:60]!r}")
        sent = raw["sent"]
        if sent is not None:
            sent = {"stop_price": sent["stop_price"], "qty": sent["qty"],
                    "sent_at": datetime.fromisoformat(str(sent["sent_at"])),
                    "booked": {str(oid): int(qty) for oid, qty in (sent.get("booked") or {}).items()}}
        return {"symbol": str(raw["symbol"]), "side": Side(str(raw["side"])), "sent": sent, "misses": 0,
                "reported": {str(oid) for oid in raw["reported"]}, "fills_logged": int(raw["fills_logged"]),
                "owed_at": raw["owed_at"], "loaded": True}

    def _save_owed_sweeps(self) -> None:
        """Save the post-close sweeps still owed as they stand (every row
        replaced), when they differ from what the store holds
        (2026-11-11). Until then they lived in memory only and a restart
        forgot them: the leftover order then blocked every entry as a
        foreign order until it was cancelled by hand (the startup reconcile
        in mode ``block`` or a restore mode, which a live disaster stop
        requires), or rested unseen with the symbol open to entries beside
        it (mode ``ignore`` or the reconcile off, which a live run can have
        with bracket orders).

        A close whose sweep is done at once writes nothing. A save that
        fails is logged with its type and the next call tries again: the
        close that owes the sweep must not fail on it. A dry run owes no
        sweep and writes nothing: the rows the store holds are a live
        run's. Nothing is written either while the rows an earlier run
        saved could not be read (``load_owed_sweeps``): a save replaces
        every row, and written after one failed read it erased sweeps that
        run still owed (the safety review's probe)."""
        if self.reconcile_metadata_store is None or self.config.schwab.dry_run \
                or self._owed_sweeps_unread_since is not None:
            return
        rows = {key: self._serialize_owed_sweep(owed) for key, owed in self._sweeps_owed.items()}
        if rows == self._owed_sweeps_saved:
            return
        try:
            self.reconcile_metadata_store.save_owed_sweeps(rows)
        except Exception as exc:
            LOG.error("Could not save the %d post-close sweep(s) still owed (%s: %s); a restart before the next "
                      "save would not know of them", len(rows), type(exc).__name__, exc)
            return
        self._owed_sweeps_saved = rows

    def load_owed_sweeps(self) -> None:
        """At start-up, before the startup reconcile: owe again the
        post-close sweeps the last run still owed (saved by
        ``_save_owed_sweeps``), so the exit orders a closed position left
        working are cancelled by this run and their symbols take no entry
        until they are (2026-11-11). Each is marked ``loaded``: it cancels
        nothing beside shares the bot does not track
        (``_loaded_sweep_refusal``).

        A row that does not read is logged with its type and not owed. A
        store that cannot be read owes none for now, at ERROR with the
        type: it is read again once a minute from the passes that try the
        owed sweeps (``_try_owed_sweeps``), nothing is written over it
        meanwhile, and in mode ``block`` and the restore modes the startup
        reconcile's own read of the working orders still blocks entries on
        a leftover. A sweep this run already owes under a saved row's key
        stands, marked ``loaded`` as that row was. A
        dry run sweeps nothing and cannot cancel an order: the rows are a
        live run's (the store is shared) and stay for it.

        A sweep of a strategy that is flat by the close, saved on an
        earlier day, is done at its first try: it reads the day's orders
        (``SchwabExecutor.resting_stop_orders``), and its leftover was a
        DAY order."""
        if self.reconcile_metadata_store is None:
            return
        try:
            rows = self.reconcile_metadata_store.load_owed_sweeps()
        except Exception as exc:
            LOG.error("Could not read the post-close sweeps the last run still owed (%s: %s); none is owed. An exit "
                      "order a closed position left working is still seen by the startup reconcile's read of the "
                      "working orders, where that reconcile is on", type(exc).__name__, exc)
            if not self.config.schwab.dry_run:
                # Read again a minute on, and nothing written until then.
                self._owed_sweeps_unread_since = time.monotonic()
            return
        self._owed_sweeps_unread_since = None
        if not rows:
            return
        if self.config.schwab.dry_run:
            LOG.warning("%d post-close sweep(s) a live run still owed (%s) are not run by this dry run, which "
                        "cancels no order; they stay in the store for the next live run", len(rows),
                        ", ".join(sorted(rows)))
            return
        self._owed_sweeps_saved = rows
        for key, raw in rows.items():
            try:
                owed = self._deserialize_owed_sweep(raw)
            except Exception as exc:
                LOG.error("The post-close sweep the last run still owed for %s cannot be read (%s: %s); it is not "
                          "owed", key, type(exc).__name__, exc)
                continue
            if str(key) in self._sweeps_owed:
                # Owed by this run too (the rows were read late): its own
                # stands, and reads the account as the saved one would.
                self._sweeps_owed[str(key)]["loaded"] = True
                continue
            self._sweeps_owed[str(key)] = owed
            LOG.warning("%s closed in the last run with its post-close sweep on %s still owed (since %s); it is "
                        "tried again, and entries in %s wait until it is done", key, owed["symbol"],
                        owed["owed_at"], owed["symbol"])
        self._save_owed_sweeps()

    def _unconfirmed_stops_spared(self, key: str, owed: dict[str, Any], orders: list[dict[str, Any]], symbol: str,
                                  side: Side) -> set[str]:
        """The ids, among *orders* (the sweep's read, whatever their
        status), of the exit stops that may still work and that a
        post-close sweep (*key*'s, *owed*) must not cancel because an open
        *side* position in *symbol* may own them: its disaster stop record
        is ``unconfirmed`` (sent with an unknown outcome, or left so by a
        restore that could not re-establish it), names no order, and so
        cannot shield its stop through ``_tracked_order_ids`` (2026-11-11).

        The orders that fit the record are the exit ``STOP`` orders at
        exactly the price and the size it sent
        (``broker_payloads.sent_exit_stops``). Spared among them, the ones
        the record's own path can still take:

        - When one of them was entered from the record's submit on
          (``sent_at`` less ``DISASTER_STOP_RETRY_SECONDS``, the window
          ``_look_up_unconfirmed_stop`` reads) and may still work or reads
          REPLACED: that is the stop the record sent, which its lookup
          adopts or follows, and only those entered in that window are
          spared. An older one at the same price and size is a leftover:
          the lookup never sees it, so spared it rested untracked for as
          long as the record stayed unresolved (the third check's probe).
        - Otherwise (none entered since the submit, or only ones that
          filled or died): the submit did not land or its stop is gone, or
          the record is a restore's, stamped at the restart, whose stop
          rests from before it. The lookup then answers that nothing of
          its stop rests, and the placement that follows adopts a stop
          still resting on the symbol
          (``SchwabExecutor.ensure_position_protected``), so every one that
          fits is spared, whenever it was entered. With a resend rejected
          at the restart counted as the record's own, the restored
          position's real stop was cancelled (the fourth check's probe).

        Cancelled, such a stop left the position resting none until one
        was placed again (the safety review's probe). Every other exit
        order is still a leftover and is cancelled: sparing the whole
        symbol, as the first fix did, left a larger leftover resting beside
        the position for as long as the record stayed unresolved (the
        second check's probes). The sweep stays owed while any is spared,
        and says so once. A submit time or an entry time that does not
        read rules nothing out."""
        wanted = str(symbol).upper().strip()
        spared: set[str] = set()
        owners: list[str] = []
        for position_key, position in self.positions.items():
            metadata = position.metadata if isinstance(position.metadata, dict) else {}
            if position.side != side or not self._disaster_stop_unconfirmed(position) \
                    or str(metadata.get("underlying") or position.symbol).upper().strip() != wanted:
                continue
            record = metadata["bracket"]
            fits = sent_exit_stops(orders, symbol, side, stop_price=record.get("stop_price"), qty=record.get("qty"))
            try:
                since = datetime.fromisoformat(str(record.get("sent_at"))) - timedelta(seconds=DISASTER_STOP_RETRY_SECONDS)
            except ValueError:
                since = None
            recent = [order for order in fits
                      if since is None or order_entered_at(order) is None or order_entered_at(order) >= since]
            own = any(order_status_class(order["status"]) in (ORDER_LIVE, ORDER_REPLACED) for order in recent)
            fits = [order for order in (recent if own else fits) if order_may_be_live(order["status"])]
            if fits:
                owners.append(position_key)
                spared |= {str(order["orderId"]) for order in fits}
        if not spared:
            owed.pop("spares", None)
        elif owed.get("spares") != sorted(spared):
            owed["spares"] = sorted(spared)
            LOG.warning("%s closed with exit order(s) %s still working on %s at the price and size the disaster stop "
                        "of %s was sent with an unknown outcome: they may be its stop, so they are left for its "
                        "lookup, and the sweep stays owed until its record is settled", key,
                        ", ".join(sorted(spared)), symbol, ", ".join(owners))
        return spared

    def _loaded_sweep_refusal(self, symbol: str, side: Side) -> str | None:
        """Why a sweep an earlier run owed (``load_owed_sweeps``) may not
        cancel the exit orders it found on *symbol* for *side*, or None
        when it may: one read of the account.

        A running bot's sweep never cancels an order an open position
        tracks (``_tracked_order_ids``), so a position opened in the symbol
        while the sweep was owed (an unsettled entry order's late fill)
        keeps its stop. After a restart that position may not be tracked:
        mode ``block`` restores nothing, mode ``ignore`` (and the reconcile
        off) drops every position, and a restore has not run before the
        first reconcile succeeds. Its stop then reads as a leftover, and
        cancelling it would strip the only stop of shares the account
        holds. So while the account holds
        shares of the symbol on that side beyond the ones the bot tracks,
        or cannot be read, nothing is cancelled."""
        rows = self.executor.fetch_account_positions()
        if rows is None:
            return "the account could not be read"
        wanted = str(symbol).upper().strip()
        row = next((row for row in rows if str(row.get("symbol") or "").upper().strip() == wanted), None)
        held_side, held = broker_position_side_qty(row)
        if held is None:
            return f"the account's row for {wanted} holds a quantity that cannot be read"
        if held_side != side:
            return None
        tracked = 0
        for position in self.positions.values():
            metadata = position.metadata if isinstance(position.metadata, dict) else {}
            if position.side == side and not is_option_asset(metadata) \
                    and str(metadata.get("underlying") or position.symbol).upper().strip() == wanted:
                tracked += int(position.qty)
        if held > tracked:
            return (f"the account holds {held} {side.value} share(s) of {wanted}, {held - tracked} more than the "
                    f"bot tracks")
        return None

    def _sweep_exit_orders(self, key: str, position: Position) -> None:
        """After a full close of a position that had live broker protection,
        cancel the exit orders that may still work on its symbol for its side
        (``order_may_be_live``: any status but a terminal one, PENDING_CANCEL,
        NEW and an unknown one included, since 2026-09-29).

        Once the position is gone, a SELL (for a LONG) or BUY_TO_COVER (for a
        SHORT) still working at the broker opens a position the bot does not
        track when it fills. The bot's own records cannot see three: a stop
        whose submit had an unknown outcome and landed, if the position
        closed in full before the lookup found it; a replacement a replace
        left untracked (``new_id_unknown``); and a stop moved in the app.
        One read of the day's orders per such close
        (``SchwabExecutor.resting_stop_orders``; of the whole order lookback
        for a strategy that holds across sessions, whose good-till-cancelled
        stop moved in the app on an earlier day still rests); a dry run and a
        position that had no broker protection (no record, or a simulated
        one) read nothing. The account's positions in a symbol the bot
        trades are taken as the bot's own, as the startup reconcile takes
        them.

        A close whose record was still unconfirmed also looks for the stop
        that record sent (and its replacement, when it was moved in the app):
        fills the record had not booked sold shares beside the exit (``EXIT
        ORDERS LEFT`` at CRITICAL). Its sweep stays owed until the retry
        interval after the submit (``sent_at`` +
        ``DISASTER_STOP_RETRY_SECONDS``) has passed, and runs again at the
        start of each management pass until then, so a stop the broker lists
        only after the close is still cancelled (2026-09-29). A read that
        fails is owed too, and tried again at the start of every management
        pass (``_run_exit_sweep``); while a sweep is owed the symbol takes no
        entry (``exit_sweep_owed``). Until 2026-09-29 it read once, and a
        failed read logged once and was dropped."""
        record = position.metadata.get("bracket") if isinstance(position.metadata, dict) else None
        if (self.config.schwab.dry_run or not isinstance(record, dict) or record.get("simulated")
                or is_option_asset(position.metadata)):
            return
        sent = None
        earlier = self._sweeps_owed.get(key)
        if is_disaster_stop(record) and record.get("state") == "unconfirmed":
            sent = {"stop_price": record.get("stop_price"), "qty": record.get("qty"),
                    "sent_at": datetime.fromisoformat(str(record["sent_at"])),
                    "booked": {str(oid): int(qty) for oid, qty in (record.get("booked_child_fills") or {}).items()}}
        self._sweeps_owed[key] = {"symbol": str(position.metadata.get("underlying") or position.symbol),
                                  "side": position.side, "sent": sent, "misses": 0, "reported": set(),
                                  "owed_at": sessions.now_et().isoformat()}
        if (earlier or {}).get("loaded"):
            # The sweep an earlier run owed on this symbol is still owed:
            # this one takes its place and its look at the account. Without
            # the mark, a position that opened and closed here after a
            # restart let its sweep cancel the stop of shares the bot does
            # not track, which the loaded one was refusing to (the safety
            # review's probe).
            self._sweeps_owed[key]["loaded"] = True
        self._run_exit_sweep(key)
        self._save_owed_sweeps()

    def _run_exit_sweep(self, key: str) -> None:
        """Run *key*'s owed post-close sweep (``_sweep_exit_orders``). One
        whose read fails, or whose leftover orders' cancel the broker does
        not confirm, stays owed and is tried again next pass (a management
        pass; for a strategy that holds across sessions also once a minute
        on the other passes while the broker takes orders,
        ``run_owed_sweeps``), logged at
        ERROR, and every ``execution.disaster_stop_escalation_attempts``-th
        consecutive failure logs ``EXIT ORDERS LEFT`` at CRITICAL; a
        leftover that sold shares before its cancel is CRITICAL at once. The
        sweep is done, and the key dropped, only once the symbol lists no
        live untracked exit order for the side, or the cancel confirmed
        (until 2026-11-08 the key was dropped before the cancel was sent, so
        an unconfirmed cancel was never retried and the symbol reopened to
        entries beside the resting order; the whole-bot review's R-047).
        One that reads stays owed while the stop an unconfirmed record sent
        may still be listed late (until ``sent_at`` +
        ``DISASTER_STOP_RETRY_SECONDS``). That stop
        is looked for among the orders entered from a minute before its
        submit, as the lookup does, so an earlier stop at the same price and
        size is never taken for it; each order's unbooked fills are reported
        once.

        An order an open position tracks (its broker protection's ids, its
        working exit order) is never cancelled: a position opened in the
        symbol while the sweep was owed (an unsettled entry order's late
        fill) had its disaster stop cancelled on every pass until the sweep
        ended, and rested none (2026-09-29). A sweep an earlier run owed
        (``load_owed_sweeps``) reads the account before it cancels, and
        cancels nothing beside shares the bot does not track
        (``_loaded_sweep_refusal``); it then waits
        ``OWED_SWEEP_RETRY_SECONDS`` before its next try."""
        owed = self._sweeps_owed[key]
        if owed.get("wait_until") is not None and time.monotonic() < owed["wait_until"]:
            return
        symbol, side, sent = owed["symbol"], owed["side"], owed["sent"]
        orders = self.executor.resting_stop_orders()
        sent_orders: list[dict[str, Any]] | None = []
        if orders is not None and sent is not None:
            sent_orders = self.executor.todays_orders(sent["sent_at"] - timedelta(seconds=DISASTER_STOP_RETRY_SECONDS))
        if orders is None or sent_orders is None:
            owed["misses"] += 1
            LOG.error("The day's orders could not be read after %s closed (%d consecutive time(s)); an exit order "
                      "still working on %s would open a position the bot does not track. Retried next pass; "
                      "entries in %s wait until then", key, owed["misses"], symbol, symbol)
            if owed["misses"] % int(self.config.execution.disaster_stop_escalation_attempts) == 0:
                LOG.critical("EXIT ORDERS LEFT — %s closed, and the day's orders on %s could not be read %d "
                             "consecutive times to cancel the exit orders it may have left working. Check the "
                             "account; entries in %s wait until they are read.", key, symbol, owed["misses"], symbol)
            return
        settled = (sent is None
                   or sessions.now_et() >= sent["sent_at"] + timedelta(seconds=DISASTER_STOP_RETRY_SECONDS))
        if sent is not None:
            self._report_sent_stop_fills(key, owed, sent_orders)
        tracked = self._tracked_order_ids()
        live = [order for order in orders if order_may_be_live(order["status"])]
        # The stops an open position's unconfirmed record may have resting
        # are that position's, not leftovers; the sweep stays owed for them.
        spared = self._unconfirmed_stops_spared(key, owed, orders, symbol, side)
        left = [order for order in exit_orders(live, symbol, side)
                if str(order["orderId"]) not in tracked and str(order["orderId"]) not in spared]
        if not left:
            owed["misses"] = 0
            if settled and not spared:
                del self._sweeps_owed[key]
            return
        ids = [str(order["orderId"]) for order in left]
        if owed.get("loaded"):
            refusal = self._loaded_sweep_refusal(symbol, side)
            if refusal is not None:
                owed["misses"] += 1
                owed["wait_until"] = time.monotonic() + OWED_SWEEP_RETRY_SECONDS
                LOG.error("%s closed in an earlier run with exit order(s) %s still working on %s, and %s: they are "
                          "left as they are, since one may be the stop of shares the bot does not track (%d "
                          "consecutive time(s)). Tried again in %.0f s; entries in %s wait until then", key,
                          ", ".join(ids), symbol, refusal, owed["misses"], OWED_SWEEP_RETRY_SECONDS, symbol)
                if owed["misses"] % int(self.config.execution.disaster_stop_escalation_attempts) == 0:
                    LOG.critical("EXIT ORDERS LEFT — %s closed in an earlier run, exit order(s) %s still work on "
                                 "%s, and %s (%d consecutive times). Nothing is cancelled. Check the account: "
                                 "close or cancel what is not the bot's; entries in %s wait until then.", key,
                                 ", ".join(ids), symbol, refusal, owed["misses"], symbol)
                return
            owed.pop("wait_until", None)
        LOG.warning("%s closed with exit order(s) %s still working on %s; cancelling them", key, ", ".join(
            f"{order['orderId']} ({order.get('orderType')} {order.get('quantity')} at "
            f"{order.get('stopPrice')}, {order.get('status') or 'no status'})" for order in left), symbol)
        cancel = self.executor.cancel_bracket({"child_order_ids": ids})
        if cancel.filled_qty > int(owed.get("fills_logged") or 0):
            # Once per share sold: a retried cancel reads the same
            # cumulative fill again.
            owed["fills_logged"] = int(cancel.filled_qty)
            LOG.critical(
                "EXIT ORDERS LEFT — %s closed, and its leftover exit order(s) %s on %s filled %d share(s) before "
                "the cancel: %s. Check the account for a position the bot does not track.",
                key, ", ".join(ids), symbol, cancel.filled_qty, cancel.message,
            )
        if not cancel.ok:
            owed["misses"] += 1
            LOG.error("%s closed, and its leftover exit order(s) %s on %s could not be confirmed cancelled (%s; the "
                      "sweep's %d consecutive failed pass(es)). Retried next pass; entries in %s wait until they are",
                      key, ", ".join(ids), symbol, cancel.message, owed["misses"], symbol)
            if owed["misses"] % int(self.config.execution.disaster_stop_escalation_attempts) == 0:
                LOG.critical("EXIT ORDERS LEFT — %s closed, and its leftover exit order(s) %s on %s could not be "
                             "confirmed cancelled (%s) after the sweep's %d consecutive failed pass(es). Check the "
                             "account for an order that would open a position the bot does not track; entries in "
                             "%s wait until it is gone.", key, ", ".join(ids), symbol, cancel.message, owed["misses"],
                             symbol)
            return
        owed["misses"] = 0
        if settled and not spared:
            del self._sweeps_owed[key]

    def _tracked_order_ids(self) -> set[str]:
        """Every order id an open position tracks: its broker protection's
        (``bracket_order_ids``) and its working exit order's."""
        tracked: set[str] = set()
        for position in self.positions.values():
            metadata = position.metadata if isinstance(position.metadata, dict) else {}
            if isinstance(metadata.get("bracket"), dict):
                tracked |= bracket_order_ids(metadata["bracket"])
            working = metadata.get("working_exit_order")
            if isinstance(working, dict) and working.get("order_id"):
                tracked.add(str(working["order_id"]))
        return tracked

    @staticmethod
    def _report_sent_stop_fills(key: str, owed: dict[str, Any], orders: list[dict[str, Any]]) -> None:
        """Log at CRITICAL (``EXIT ORDERS LEFT``), once per order, the fills
        the unconfirmed record of a closed position (*owed*'s ``sent``) did
        not book: of the stop it sent, and of that stop's replacement when it
        was moved in the app. Those shares were sold beside the exit."""
        symbol, side, sent = owed["symbol"], owed["side"], owed["sent"]
        match = sent_exit_stop(orders, symbol, side, stop_price=sent["stop_price"], qty=sent["qty"])
        followed: set[str] = set()
        while match is not None and str(match["orderId"]) not in followed:
            order_id = str(match["orderId"])
            followed.add(order_id)
            unbooked = order_row_filled_qty(match) - sent["booked"].get(order_id, 0)
            if unbooked > 0 and order_id not in owed["reported"]:
                owed["reported"].add(order_id)
                LOG.critical(
                    "EXIT ORDERS LEFT — %s closed, and the disaster stop %s it sent with an unknown outcome filled "
                    "%s share(s) on %s beside the exit. Check the account for a position the bot does not track.",
                    key, order_id, unbooked, symbol,
                )
            if order_status_class(match["status"]) != ORDER_REPLACED:
                return
            match = replacement_order(orders, match, symbol, side)

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
                              order_states: dict[str, Any], *, protective: ProtectiveRead | None) -> bool:
        """Settle an exit order an earlier cycle left working. True while it
        still is -- the caller must send nothing else for the position.
        ``protective`` is the pass's read of the protective exits
        (``_protective_read``), for the remainder's risk check.

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
            if self._working_slice_remainder_exit(key, position, record, last_price, protective):
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
            slice_qty = self._held_part_of_fill(key, position, filled - booked, f"exit order {order_id}")
            average = safe_float(state.get("fill_price"), None, finite=True)
            if average is not None and is_option_asset(position.metadata):
                average *= 100.0
            # The order's average covers the shares already booked from it:
            # the slice is priced as its own fills. A record from before
            # 2026-10-06 carries no booked_notional, so its slice is booked
            # at the average, estimated.
            booked_notional = 0.0 if booked <= 0 else safe_float(record.get("booked_notional"), None, finite=True)
            booked_estimated = booked > 0 and record.get("booked_price_estimated") is not False
            broker_price, broker_price_estimated = unbooked_fill_price(
                filled, average, state.get("fill_price_estimated") is not False, booked, booked_notional,
                booked_estimated, what=f"exit order {order_id} for {key}")
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
            record["booked_notional"] = (None if booked_notional is None or broker_price is None
                                         else booked_notional + broker_price * slice_qty)
            # Estimated whenever the booked part is (unbooked_fill_price).
            record["booked_price_estimated"] = broker_price_estimated
            self._book_broker_exit(
                key, position, slice_qty, float(exit_price), decision, bars,
                result_message=f"working_exit_filled:{state.get('status')}", attempt_status="broker_working_exit",
                # The mark, or the order's own price: not an execution price
                # (broker_payloads.order_fill_price_estimated).
                fill_price_estimated=broker_price_estimated,
                exit_limits_missed=record.get("exit_limits_missed"),
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
        if order_status_class(state.get("status")) == ORDER_REPLACED:
            # Ahead of the fill check: a REPLACED order may read filled
            # (nothing remaining) while its replacement still works.
            return self._follow_replaced_exit(key, position, record, state)
        if state.get("is_filled") or state.get("is_terminal_failure"):
            position.metadata.pop("working_exit_order", None)
            if record.get("reprotect") and not self._disaster_stop_unconfirmed(position) \
                    and not self._replacement_unresolved(position):
                # The order is settled and the position is still held. The
                # submit-time path re-protects a booked exit's remainder;
                # this is that step for an order that was left working, whose
                # remainder otherwise kept no broker bracket for the rest of
                # the trade (2026-09-24). A bracket placed beside a working
                # slice is adopted and resized to what is held. A disaster
                # stop placed beside it whose submit had an unknown outcome
                # is left to ensure_disaster_stop, which looks for it (books
                # what it sold, adopts it) before it places another: placed
                # here, a second stop rested beside the one that landed, and
                # the fills of that one were never booked (2026-09-29). Nor
                # beside a resting stop that was REPLACED and whose
                # replacement is not found yet (_follow_replaced_child).
                self._reprotect_remainder(position)
            self._save_reconcile_metadata()
            return False
        remainder_exit = self._working_slice_remainder_exit(key, position, record, last_price, protective)
        if remainder_exit or str(state.get("order_type") or "") != "MARKET":
            self._cancel_working_exit(key, order_id)
        self.audit.log_cycle(
            f"exit_gate:{key}", "exit_order_working",
            f"Exit held {key}: exit order {order_id} is still working at the broker",
            interval=60.0, level=TRADEFLOW_LEVEL,
        )
        return True

    def _follow_replaced_exit(self, key: str, position: Position, record: dict[str, Any],
                              state: dict[str, Any]) -> bool:
        """A tracked exit order the broker lists REPLACED (changed in the
        app), its own fills booked: its shares live on in the replacement,
        which is tracked in its place from here. That is the one its payload
        names, or else the one found among the day's orders
        (``broker_payloads.replacement_order``: of its kind, on the symbol
        for the side, entered after it). Returns True: the position sends
        nothing else this pass.

        While no replacement is found (none named or listed, several that
        fit, or orders that cannot be read) the position is held: no exit
        and no re-protect for it, the other positions managed as usual, and
        every ``execution.disaster_stop_escalation_attempts``-th pass logs
        ``ORDER REPLACED UNTRACKED`` at CRITICAL until the orders show it.
        Until 2026-09-29 a REPLACED order read as settled: the position was
        re-protected in full beside its replacement, and a later exit went
        out beside it too."""
        order_id = str(record.get("order_id") or "")
        replacement_id = state.get("replaced_by")
        if replacement_id is None:
            orders = self.executor.todays_orders()
            original = next((order for order in orders or [] if str(order.get("orderId")) == order_id), None)
            found = (replacement_order(orders, original, str(position.metadata.get("underlying") or position.symbol),
                                       position.side) if orders is not None and original is not None else None)
            replacement_id = str(found["orderId"]) if found is not None else None
        if replacement_id is None:
            misses = int(record.get("replacement_misses") or 0) + 1
            record["replacement_misses"] = misses
            detail = (f"exit order {order_id} was REPLACED at the broker and its replacement is not found among the "
                      f"day's orders ({misses} consecutive pass(es))")
            LOG.error("%s: %s; nothing else is sent or re-protected for it until it is", key, detail)
            if misses % int(self.config.execution.disaster_stop_escalation_attempts) == 0:
                LOG.critical("ORDER REPLACED UNTRACKED — %s: %s; the replacement may still work untracked, and the "
                             "position is held. Check the account.", key, detail)
            self._save_reconcile_metadata()
            self.audit.log_cycle(
                f"exit_gate:{key}", "exit_order_replacement_unknown",
                f"Exit held {key}: working exit order {order_id} was REPLACED and its replacement is not found",
                interval=60.0, level=TRADEFLOW_LEVEL,
            )
            return True
        LOG.error("Exit order %s for %s was REPLACED at the broker by order %s, which is tracked in its place",
                  order_id, key, replacement_id)
        record.pop("replacement_misses", None)
        # The replacement has filled nothing of its own yet, and holds what
        # the original had left.
        record.update({"order_id": str(replacement_id), "booked_qty": 0, "booked_notional": 0.0,
                       "booked_price_estimated": False,
                       "requested_qty": max(0, int(record.get("requested_qty") or 0)
                                            - int(state.get("filled_qty") or 0))})
        self._save_reconcile_metadata()
        return True

    def _cancel_working_exit(self, key: str, order_id: str) -> None:
        cancel_ok, cancel_msg = self.executor.cancel_working_order(order_id)
        if not cancel_ok:
            LOG.warning("Working exit %s for %s still live; cancel unconfirmed (%s)", order_id, key, cancel_msg)

    def _working_slice_remainder_exit(self, key: str, position: Position, record: dict[str, Any],
                                      last_price: float | None, protective: ProtectiveRead | None) -> bool:
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
        risk_exit, risk_reason = self.trade_manager.update_position(position, float(last_price), protective)
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
        - Anywhere else (its bracket-fill booking, the paper account's mark
          at the price this pass read, the working-exit settlement, the risk
          check itself, the bracket sync, the exit order and its booking) its
          cycle ends where it failed. Those steps but the mark can leave its
          orders in a state this cycle cannot know, and a second attempt
          risks a double exit; the mark, a dict write under the account's
          lock, cannot raise. It is retried next cycle, and a resting broker
          stop still protects it.

        A position whose management fails ``runtime.error_escalation_cycles``
        cycles in a row escalates to a CRITICAL naming it
        (``_escalate_failures``). A stop signal (KeyboardInterrupt) is not an
        Exception, so it still stops the cycle and reaches the engine.

        The pass takes one re-price deadline as it starts
        (``executor.exit_reprice_deadline``) and every exit it sends shares
        it: a live LIMIT exit that misses is re-sent only until then, so the
        wait the re-sends add to the pass is bounded whatever the number of
        positions that miss together (2026-09-28).

        A post-close sweep a closed position still owes (its orders could
        not be read, or a leftover's cancel is not confirmed,
        ``_sweep_exit_orders``) is tried again first.
        """
        reprice_deadline = self.executor.exit_reprice_deadline()
        self._try_owed_sweeps()
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
        self._looks = {key: look for key, look in self._looks.items() if key in self.positions}
        self._unsized = {key: children for key, children in self._unsized.items() if key in self.positions}
        self._trade_spreads = {key: spreads for key, spreads in self._trade_spreads.items() if key in self.positions}
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

    def _log_position_adjustments(self, key: str, position: Position, last_price: float | None,
                                  previous_look: _Look | None, look: _Look | None) -> None:
        """One POSITION_ADJUSTMENT record per level move this pass's managers
        made (``management_adjustments``), with the passes around it
        (``_look_fields``)."""
        adjustments = position.metadata.get("management_adjustments") if isinstance(position.metadata, dict) else None
        for adj in adjustments or ():
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
                **_look_fields(previous_look, look),
            })

    def _manage_position(self, now: datetime, key: str, position: Position, bars, order_states: dict[str, Any],
                         failures: dict[str, str], reprice_deadline: float) -> None:
        """One position's management cycle; see ``manage_positions``. It
        returns where the position's cycle ends. ``reprice_deadline`` is the
        pass's, for the exit it sends."""
        if self._settle_pending(position):
            self.audit.log_cycle(
                f"exit_gate:{key}", "settle_pending",
                f"Exit held {key}: it waits for its settle against the broker account; retrying",
                interval=60.0, level=TRADEFLOW_LEVEL,
            )
            return
        last_price, market_snapshot = self._position_management_snapshot(position, bars)
        previous_look, look = self._take_look(key, position, last_price, market_snapshot)
        protective = self._protective_read(key, position, last_price, market_snapshot)
        if look is not None and not is_option_asset(position.metadata):
            # The paper account marks a held equity at the price this pass
            # manages it at (its row, the equity, the peak and the max
            # drawdown), one with a working exit too. Until 2026-10-07 it
            # held the quotes phase's 1m close, a median 30 s older than
            # the quote management read; a pass that does not price the
            # position (settle pending above, management off, its bracket
            # fills unbooked) still leaves that close. An option keeps its
            # strategy's mark from fresh legs (the engine's). The archive's
            # manifest and the SESSION REPORT name this basis
            # (EQUITY_MARK_BASIS).
            self.account.mark_prices({position.symbol: look.mark})
        # Always reset management_adjustments at the start of each cycle to
        # prevent stale adjustments from persisting when price is unavailable.
        # Ahead of the working-exit check: its risk check on a working
        # slice's remainder can move the stop too.
        if isinstance(position.metadata, dict):
            position.metadata["management_adjustments"] = []
        if self._exit_order_in_flight(key, position, last_price, bars, order_states, protective=protective):
            # A ratchet the remainder's risk check made while the slice works
            # (_working_slice_remainder_exit) is on the record like any
            # other. Until 2026-09-28 the next pass reset it unlogged.
            self._log_position_adjustments(key, position, last_price, previous_look, look)
            return
        if self._replacement_unresolved(position):
            # Its resting stop (or target) was REPLACED at the broker and
            # the replacement is not found: it may rest, so no exit goes out
            # beside it and nothing is placed beside it. The fill reconcile
            # looks for it again at the head of every pass, and says so.
            self.audit.log_cycle(
                f"exit_gate:{key}", "bracket_replacement_unknown",
                f"Exit held {key}: a resting protective order was REPLACED at the broker and its replacement is not "
                "found",
                interval=60.0, level=TRADEFLOW_LEVEL,
            )
            return
        management_symbol = str(position.metadata.get("underlying") or position.symbol)
        management_frame = bars.get(management_symbol)
        if not is_option_asset(position.metadata):
            underlying_price = last_price
        else:
            underlying_price = self.underlying_price_for_position(position, bars, None)
            if underlying_price is None:
                underlying_price = first_float(self.data.get_quote(management_symbol), "mark", positive=True)
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
            risk_exit, risk_reason = self.trade_manager.update_position(position, last_price, protective)
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
            self._log_position_adjustments(key, position, last_price, previous_look, look)
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
            self.ensure_disaster_stop(key, position, bars)
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
        exit_context = {
            **self._position_exit_context(position, decision, last_price, underlying_price, market_snapshot, bars),
            **_look_fields(previous_look, look),
        }
        if protective is not None and decision.family == "risk" and protective_exit(position, reason):
            # Which side judged a protective exit (the bid, the ask, or the
            # mark and why), with the spread its guard read.
            exit_context.update(protective.exit_fields(float(last_price), exit_context.get("exit_level"),
                                                       position.side == Side.LONG))
        if not self.executor.can_close_position_now(position, now):
            self.audit.log_cycle(
                f"exit_gate:{key}",
                f"session_closed:{reason}",
                f"Exit deferred {key} qty={requested_qty} reason={reason} because market session is closed",
                interval=60.0,
                level=TRADEFLOW_LEVEL,
            )
            return
        record = position.metadata.get("bracket") if isinstance(position.metadata, dict) else None
        if (not self.config.schwab.dry_run and is_disaster_stop(record)
                and record.get("state") == "unconfirmed"):
            # A disaster stop sent with an unknown outcome may rest, or may
            # have sold shares already: it is looked up first, at once, so the
            # exit neither sells beside it nor sells shares it sold
            # (2026-09-29). Until then the exit went out with no cancel, and a
            # stop that had filled in the same flush sold the shares twice.
            outcome, stub = self._look_up_unconfirmed_stop(key, position, record, sessions.now_et(), bars)
            if outcome in ("unreadable", "unresolved"):
                # It may rest, so the exit would sell beside it: deferred,
                # as a miss.
                self._disaster_stop_missed(
                    key, position.metadata["bracket"], int(record.get("failed_attempts") or 0) + 1,
                    sessions.now_et(), logging.ERROR,
                    ("the day's orders could not be read to look for its unconfirmed stop"
                     if outcome == "unreadable" else
                     "its unconfirmed stop was REPLACED at the broker and its replacement is not found among the "
                     "day's orders") + f", so its {reason} exit is deferred",
                )
                self.audit.log_structured("EXIT_CONTEXT", {
                    **exit_context, "symbol": key, "qty": int(requested_qty),
                    "result_message": ("unconfirmed_disaster_stop_unread" if outcome == "unreadable"
                                       else "unconfirmed_disaster_stop_replacement_not_found"),
                    "attempt_status": "deferred_unconfirmed_disaster_stop",
                })
                return
            if key not in self.positions:
                return
            if outcome == "working":
                # Adopted, so the cancel below takes it down first.
                position.metadata["bracket"] = {**{mark: value for mark, value in record.items()
                                                   if mark not in ("attempted_at", "sent_at")},
                                                **{mark: value for mark, value in stub.items() if mark != "listed"},
                                                "active": True, "state": "adopted"}
            requested_qty = min(requested_qty, int(position.qty))
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
        # The remainder is owed a resting stop when this cycle cancelled
        # the bracket, or an earlier one left it down (2026-09-24): an
        # attempt that failed before reaching the broker used to leave
        # the bracket cancelled, and the retry that booked the slice then
        # saw no bracket and re-protected nothing.
        reprotect_owed = open_bracket is not None or self._broker_bracket_down(position)

        def _order_sent(order_id: str) -> None:
            # Each live order of the exit is on the record before it is
            # polled (2026-09-29): a stop signal during the poll (not an
            # Exception) or an error after it leaves the order tracked and
            # saved, so the next cycle, or a restart, settles it instead of
            # sending a second exit beside it.
            self._track_working_exit(position, order_id=order_id, message="submitted", decision=decision,
                                     booked_qty=0, booked_notional=0.0, booked_price_estimated=False,
                                     requested_qty=requested_qty, bracket_cancelled=reprotect_owed,
                                     exit_limits_missed=None)
            self._save_reconcile_metadata()

        result = self.executor.close_position(position, requested_qty, data=self.data, market_snapshot=market_snapshot,
                                              reprice_deadline=reprice_deadline, on_order_sent=_order_sent)
        # The call settled what it sent; the branches below track what is
        # still live.
        position.metadata.pop("working_exit_order", None)
        if result.exit_limits_missed is not None:
            # A live LIMIT exit's re-sends: how many of its limits missed, on
            # every record of this attempt (the message's suffix says it too).
            exit_context["exit_limits_missed"] = int(result.exit_limits_missed)
        # A result whose last order the broker confirmed dead with nothing
        # filled left nothing live: nothing to track, and the protection the
        # exit took down goes back now rather than on the next pass
        # (2026-09-29). Until then it was tracked as a working exit and the
        # position rested no broker stop until the pass after.
        nothing_live = order_result_left_nothing_live(result.message)
        # ok=True with filled_qty=0 filled nothing, so it is settled as the
        # unfilled attempt it is (2026-09-25). Its own branch tracked
        # nothing and re-protected nothing: the bracket this cycle
        # cancelled stayed down unless the deciding family fired again,
        # and an order that reached the broker was never settled from its
        # fill record.
        nothing_filled = result.filled_qty is not None and int(result.filled_qty) <= 0
        if not result.ok or nothing_filled:
            if result.order_id and not nothing_live and (
                    result.may_still_be_working or order_result_needs_broker_recheck(result.message)):
                # The order reached the broker and its outcome is not
                # settled: it may be live, or have filled shares the result
                # does not report. Track it and settle from its own fill
                # record next cycle; sending another exit meanwhile is how
                # a halted market-out fills twice.
                self._track_working_exit(position, order_id=str(result.order_id), message=result.message,
                                         decision=decision, booked_qty=0, booked_notional=0.0,
                                         booked_price_estimated=False, requested_qty=requested_qty,
                                         bracket_cancelled=reprotect_owed,
                                         exit_limits_missed=result.exit_limits_missed)
                if reprotect_owed:
                    self._reprotect_beside_working_order(position, requested_qty)
                self._save_reconcile_metadata()
                LOG.log(TRADEFLOW_LEVEL, "Exit order %s for %s unsettled (%s); tracking it", result.order_id, key, result.message)
                self.audit.log_structured("EXIT_CONTEXT", {**exit_context, "symbol": key, "qty": int(requested_qty), "result_message": result.message, "attempt_status": "tracking_unsettled_order"})
                return
            if reprotect_owed and (nothing_live or not order_result_needs_broker_recheck(result.message)):
                # The order never reached the broker (a rejected submit, a
                # stale quote), or the broker confirmed it dead with
                # nothing filled, so nothing of it can fill: put back the
                # protection this cycle tore down. A retry cancels it
                # again -- churn, never a double fill. One that may have
                # reached it with no id to track (live_missing_order_id,
                # or a POST answered 5xx: order_result_needs_broker_recheck,
                # 2026-09-29) may still fill, so a resting stop beside it
                # could sell the same shares twice; the engine stop keeps
                # owning it, and a disaster stop is placed again by the
                # next pass that decides no exit, as a miss
                # (ensure_disaster_stop).
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
        # A fill priced off its order's own price (a LIMIT's limit: the
        # broker reported no executions) is booked at it, flagged estimated,
        # and measures no slippage (OrderResult.fill_price_estimated).
        fill_price_estimated = fill_price is None or result.fill_price_estimated
        if not fill_price_estimated:
            exit_context.update(self._exit_fill_fields(position, exit_context, fill_price))
        exited_position = copy.copy(position)
        exited_position.qty = exit_qty
        remaining_qty_after_exit = max(0, int(position.qty) - int(exit_qty))
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
                # An order REPLACED at the broker is tracked by the
                # replacement its payload named, which has filled nothing of
                # its own yet and holds what the original had left; one that
                # named none is tracked as the original, whose settle looks
                # the replacement up (_follow_replaced_exit).
                replaced = order_result_names_replacement(result.message)
                self._track_working_exit(position, order_id=str(result.order_id), message=result.message,
                                         decision=decision, booked_qty=0 if replaced else exit_qty,
                                         booked_notional=(0.0 if replaced else None if fill_price is None
                                                          else fill_price * exit_qty),
                                         booked_price_estimated=not replaced and fill_price_estimated,
                                         requested_qty=requested_qty - exit_qty if replaced else requested_qty,
                                         bracket_cancelled=reprotect_owed,
                                         exit_limits_missed=result.exit_limits_missed)
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
