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
- Each position is managed on its own: an exception while managing one is
  logged against it, and escalated when it persists, and the others are
  managed as usual (``manage_positions``).
- The HTF timeframe / lookback are the strategy's
  (``self.strategy.htf_minutes()`` / ``htf_lookback_days()``), the one
  resolution the engine, the entry gatekeeper and the dashboard read too.
  HTF refresh cadence is now bar-aligned in ``MarketDataStore.should_refresh_htf_context``
  so there's no longer a refresh-seconds knob on the strategy side.
"""
from __future__ import annotations

import copy
import logging
import math
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Mapping

import pandas as pd

from .audit_logger import AuditLogger
from .config import BotConfig
from .dashboard_cache import DashboardCache
from .data_feed import EXECUTION_LAST_KEYS, MANAGEMENT_PRICE_KEYS, MarketDataStore
from .execution import BracketCancel, SchwabExecutor
from .models import (
    ASSET_TYPE_EQUITY,
    ASSET_TYPE_OPTION_SINGLE,
    ASSET_TYPE_OPTION_VERTICAL,
    OPTION_ASSET_TYPES,
    ExitDecision,
    Position,
    Side,
)
from .numeric import first_float, safe_float
from .paper_account import PaperAccount
from .reasons import exit_reason_code
from .position_metrics import (
    LADDER_TOUCH_HOLD_KEY,
    STOP_SOURCE_KEY,
    TARGET_HOLD_GUARD,
    TARGET_HOLD_TIMEOUT,
    TARGET_WEAK_CLOSE,
    append_management_adjustment,
    exit_reason_details,
    position_return_pct_at_price,
    position_unrealized_at_price,
)
from .risk import RiskManager
from .levels_shared import effective_side_tolerance, select_next_distinct_level
from ._strategies.catalogue import is_option_strategy
from .broker_payloads import (
    active_broker_bracket,
    bracket_order_ids,
    order_result_needs_broker_recheck,
    working_exit_outstanding_qty,
)
from .log_setup import TRADEFLOW_LEVEL
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

# A position whose management raises is logged with its traceback on the first
# failure of a run of consecutive failed cycles and on every
# POSITION_ERROR_TRACEBACK_EVERY-th after, and as a one-line WARNING in
# between: the engine throttles its own cycle errors this way.
POSITION_ERROR_TRACEBACK_EVERY = 10


def _next_unpassed_rung(rungs: list, active_index: int, close: float,
                        side: Side, gap: float) -> int | None:
    """Index of the first rung after ``active_index`` that price has not already
    passed, or ``None`` when every remaining rung is behind the trade.

    A rung behind price cannot serve as a target — setting one would exit
    immediately — and cannot serve as a defense level either, so the ladder is
    finished and the caller promotes the position to a runner. A rung whose
    price is not a finite number above 0 is no target and is skipped.
    """
    for index in range(int(active_index) + 1, len(rungs)):
        entry = rungs[index]
        if not isinstance(entry, dict):
            continue
        price = safe_float(entry.get("price"), None, finite=True)
        if price is None or price <= 0:
            continue
        if side == Side.LONG:
            if price > close + gap:
                return index
        elif price < close - gap:
            return index
    return None


# The adaptive ladder's touch hold (``shared_exit.adaptive_ladder_touch_hold``).
# A delivered touch bar that closed through the target at least this far into
# its range, from the low for a LONG and from the high for a SHORT, promotes
# the rung; any other close exits.
LADDER_TOUCH_CLOSE_POSITION_MIN = 0.55
# The hold's stop buffer, the widest of the S/R context's level buffer, this
# share of the rung's zone width and this share of the touch price. It sets
# the in-bar guard and the promoted stop (the zone-flip promotion's formula).
LADDER_STOP_BUFFER_ZONE_FRAC = 0.25
LADDER_STOP_BUFFER_PRICE_FRAC = 0.0005
# A promotion's next target is the first later rung more than this share of
# the touch bar's close beyond that close; a rung within it counts as passed.
LADDER_NEXT_RUNG_GAP_FRAC = 0.0005
_TOUCH_HOLD_EXIT_CODES = frozenset({TARGET_WEAK_CLOSE, TARGET_HOLD_GUARD, TARGET_HOLD_TIMEOUT})


def _finite_number(value: Any) -> float | None:
    """``value`` when it is a finite int or float (not a bool, not text)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _whole_number(value: Any) -> int | None:
    """``value`` as an int when it is a finite whole number (``2`` or ``2.0``)."""
    number = _finite_number(value)
    return int(number) if number is not None and number.is_integer() else None


def _aware_time(value: Any) -> pd.Timestamp | None:
    """``value`` (an ISO string or a datetime) as a tz-aware timestamp in the
    exchange's time zone; None when it does not parse or names no zone."""
    if not isinstance(value, str | datetime):
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp) or stamp.tzinfo is None:
        return None
    return stamp.tz_convert(sessions.EXCHANGE_TZ)


def _bar_time(frame: pd.DataFrame) -> pd.Timestamp | None:
    """The open time of ``frame``'s last bar in the exchange's time zone (a
    naive index is exchange time, as the feed's frames are), or None when the
    frame has no time index."""
    if not isinstance(frame.index, pd.DatetimeIndex) or pd.isna(frame.index[-1]):
        return None
    stamp = frame.index[-1]
    return stamp.tz_localize(sessions.EXCHANGE_TZ) if stamp.tzinfo is None else stamp.tz_convert(sessions.EXCHANGE_TZ)


@dataclass(frozen=True, slots=True)
class _TouchHold:
    """A touch hold in progress: ``metadata[LADDER_TOUCH_HOLD_KEY]``, written
    by ``PositionManager._start_touch_hold`` and read back each cycle,
    possibly from the position store after a restart.

    ``read`` checks every field the hold reads, so a hold is either whole or
    dropped: one that came back without its ``kind``, with a naive time or a
    rung index past its count can never apply half a promotion. The times
    are stored as ISO strings, ``exit_reason`` once the hold has decided to
    exit (the order is then sent again with it until it books)."""

    rung_index: int
    rung_count: int
    level: float
    zone_width: float
    kind: str
    touch_price: float
    touch_at: pd.Timestamp
    touch_bar: pd.Timestamp
    stop_buffer: float
    guard: float
    deadline: pd.Timestamp
    exit_reason: str | None = None

    def to_meta(self) -> dict[str, Any]:
        return {
            "rung_index": self.rung_index, "rung_count": self.rung_count, "level": self.level,
            "zone_width": self.zone_width, "kind": self.kind, "touch_price": self.touch_price,
            "touch_at": self.touch_at.isoformat(), "touch_bar": self.touch_bar.isoformat(),
            "stop_buffer": self.stop_buffer, "guard": self.guard, "deadline": self.deadline.isoformat(),
            "exit_reason": self.exit_reason,
        }

    @classmethod
    def read(cls, raw: Any) -> _TouchHold | None:
        """The hold ``raw`` holds, or None when any field it reads is missing
        or unreadable: a number that is not finite (or a negative buffer or
        zone), a rung index that is not a whole number below the rung count,
        a blank ``kind``, a time that does not parse or names no time zone,
        or an ``exit_reason`` that is not one of the hold's exits."""
        if not isinstance(raw, dict):
            return None
        numbers = {key: _finite_number(raw.get(key))
                   for key in ("level", "zone_width", "touch_price", "stop_buffer", "guard")}
        if any(value is None for value in numbers.values()) or numbers["zone_width"] < 0 or numbers["stop_buffer"] < 0:
            return None
        rung_index, rung_count = _whole_number(raw.get("rung_index")), _whole_number(raw.get("rung_count"))
        if rung_index is None or rung_count is None or not 0 <= rung_index < rung_count:
            return None
        kind = raw.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            return None
        times = {key: _aware_time(raw.get(key)) for key in ("touch_at", "touch_bar", "deadline")}
        if any(value is None for value in times.values()):
            return None
        exit_reason = raw.get("exit_reason")
        if exit_reason is not None and not (isinstance(exit_reason, str)
                                            and exit_reason_code(exit_reason) in _TOUCH_HOLD_EXIT_CODES):
            return None
        return cls(rung_index=rung_index, rung_count=rung_count, kind=kind, exit_reason=exit_reason,
                   **numbers, **times)


def _ladder_rungs(meta: Mapping[str, Any]) -> list | None:
    """A laddered position's rungs, or None when they are not a non-empty
    list (every ladder builder emits at least one rung)."""
    rungs = meta.get("ladder_rungs")
    return rungs if isinstance(rungs, list) and rungs else None


def _delivered_touch_bar(frame: pd.DataFrame | None, touch_bar: pd.Timestamp,
                         now: pd.Timestamp) -> pd.Series | None:
    """The touch bar's row once it has closed and is in the frame, else None.

    The management frame holds completed bars, each delivered a few seconds
    after it closes. The clock check keeps a frame that does carry a forming
    row from being judged before its minute is over."""
    if frame is None or frame.empty or now < touch_bar + pd.Timedelta(minutes=1):
        return None
    index = pd.DatetimeIndex(frame.index)
    label = touch_bar if index.tz is not None else touch_bar.tz_convert(sessions.EXCHANGE_TZ).tz_localize(None)
    hits = (index == label).nonzero()[0]
    return frame.iloc[int(hits[-1])] if len(hits) else None


def _touch_bar_verdict(bar: pd.Series, side: Side, level: float) -> tuple[str, float | None, float | None]:
    """``strong``, ``weak`` or ``unreadable`` for a delivered touch bar, with
    its close position (from the low for a LONG, from the high for a SHORT;
    None on a bar with no range or an unreadable price) and its close.

    Strong: the close is at or through ``level`` and its close position is at
    least ``LADDER_TOUCH_CLOSE_POSITION_MIN``. A bar with no range has no
    upper part and is weak."""
    high = safe_float(bar.get("high"), None, finite=True)
    low = safe_float(bar.get("low"), None, finite=True)
    close = safe_float(bar.get("close"), None, finite=True)
    if high is None or low is None or close is None:
        return "unreadable", None, None
    span = high - low
    if span <= 0:
        return "weak", None, close
    long_side = side == Side.LONG
    close_pos = (close - low) / span if long_side else (high - close) / span
    through = close >= level if long_side else close <= level
    return ("strong" if through and close_pos >= LADDER_TOUCH_CLOSE_POSITION_MIN else "weak"), close_pos, close


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
        if position.side.value == 'LONG':
            unrealized = float(mark_price) - float(position.entry_price)
        else:
            unrealized = float(position.entry_price) - float(mark_price)
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

    def _position_exit_context(self, position: Position, reason: str, mark_price: float | None, underlying_price: float | None, market_snapshot: dict[str, Any] | None, bars) -> dict[str, Any]:
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
            direction = 1.0 if position.side == Side.LONG else -1.0
            peak_price = safe_float(position.highest_price if position.side == Side.LONG else position.lowest_price, None)
            if stop_price is not None:
                stop_r = direction * (stop_price - entry_price) / initial_risk_per_unit
            if peak_price is not None:
                peak_r = direction * (peak_price - entry_price) / initial_risk_per_unit
        management_symbol = str(meta.get('underlying') or position.symbol)
        management_frame = bars.get(management_symbol) if bars else None
        sr_row = None
        if self.data is not None and management_symbol:
            try:
                sr_row = self.dashboard_cache.sr_row(management_symbol, price=underlying_price or current_price, allow_refresh=False)
            except Exception:
                # A diagnostic read for the exit record: it runs the S/R
                # build, and an exit is never held up by it.
                self.dashboard_cache.log_component_failure(
                    "exit_context_sr_row", "Exit-context S/R row failed for %s", management_symbol,
                )
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
        sr_ctx = self.data.get_support_resistance(symbol, current_price=last_price, flip_frame=frame, mode="trading", timeframe_minutes=self.strategy.htf_minutes(), lookback_days=self.strategy.htf_lookback_days()) if self.data is not None else None
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
        structural_gap = effective_side_tolerance(cfg, close, atr=atr, sr_ctx=sr_ctx)

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
                        append_management_adjustment(position.metadata,{"manager": "sr_flip", "kind": "stop", "reason": "flipped_support", "from": prior_stop, "to": float(candidate_stop), "source_level": float(flipped_support.price)})
            target_level = select_next_distinct_level(getattr(sr_ctx, 'resistances', None), float(flipped_support.price) if flipped_support is not None else None, above=True, minimum_gap=structural_gap) if flipped_support is not None else None
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
                        append_management_adjustment(position.metadata,{"manager": "sr_flip", "kind": "target", "reason": "next_resistance", "from": prior_target, "to": float(candidate_target), "source_level": float(target_level.price), "structural_gap": float(structural_gap)})
        else:
            flipped_resistance = sr_ctx.broken_support
            if flipped_resistance is not None:
                candidate_stop = float(flipped_resistance.price) + stop_buffer
                if close < candidate_stop < float(position.stop_price):
                    prior_stop = float(position.stop_price)
                    position.stop_price = float(candidate_stop)
                    if isinstance(position.metadata, dict):
                        position.metadata["sr_flip_stop_source"] = float(flipped_resistance.price)
                        append_management_adjustment(position.metadata,{"manager": "sr_flip", "kind": "stop", "reason": "flipped_resistance", "from": prior_stop, "to": float(candidate_stop), "source_level": float(flipped_resistance.price)})
            target_level = select_next_distinct_level(getattr(sr_ctx, 'supports', None), float(flipped_resistance.price) if flipped_resistance is not None else None, above=False, minimum_gap=structural_gap) if flipped_resistance is not None else None
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
                        append_management_adjustment(position.metadata,{"manager": "sr_flip", "kind": "target", "reason": "next_support", "from": prior_target, "to": float(candidate_target), "source_level": float(target_level.price), "structural_gap": float(structural_gap)})

    # ------------------------------------------------------------------
    # The adaptive ladder
    #
    # The strategy builds the rungs at entry (``_build_ladder_rungs``, or the
    # key-levels peer rungs) and emits the active rung as the target. With
    # ``shared_exit.adaptive_ladder_touch_hold`` off -- every preset and the
    # code default -- that target is a plain take-profit: RiskManager exits
    # on the first quote at it and nothing here acts.
    #
    # Until 2026-09-27 this pass also ran a target-exit suppression and a
    # two-bar zone-flip rung promotion, both removed. Since 2026-05-14 the
    # suppression needed the last closed bar in the frame to have closed
    # strongly through the target (and a confirmation index still leaning
    # the trade's way); before that one quote at the target was enough, and
    # it did hold targets live on 2026-05-13 and 05-14. The frame holds only
    # delivered bars, so that bar closed one to two minutes before the quote
    # being judged, and a quote at the target during it had already been
    # taken as the target. The suppression could act only when the quote
    # sampling (one quote per management pass, about every 4-25 s) missed a
    # strong 1m close through the target and a later quote reached it again
    # before the next bar was delivered (or the quote's mark lagged the
    # prints that way). The 2026-09-26/27 replays found it only with a
    # quote once a minute, in three trades (AMD 2026-09-24 and ADBE better
    # for taking the target, NVDA worse); where it happens the target is now
    # taken. The zone flip
    # needed two delivered bars wholly past the rung's zone while the target
    # still sat on the rung, which only a restart past the rung or an exit
    # order failing for two bars could leave; no ladder adjustment was
    # logged from 2026-05-01 to 2026-09-25.
    # ------------------------------------------------------------------

    def _adaptive_ladder_management(self, position: Position, frame: pd.DataFrame | None,
                                    last_price: float, price_at: datetime | None) -> ExitDecision | None:
        """The adaptive ladder's touch hold, when it is on; the exit it takes.

        ``price_at`` is when ``last_price`` was observed (a quote's
        ``fetched_at``, or the open time of the bar whose close it is; see
        ``_position_management_snapshot``): the touch is attributed to the
        1m bar that time falls in, not to the cycle's clock, which can run up
        to the quote cache age later.

        A hold starts on the first price at or through the target, the price
        RiskManager would take the target on; while it lasts,
        ``LADDER_TOUCH_HOLD_KEY`` in the metadata keeps RiskManager off the
        target. It ends:

        - at once, on a price ``stop_buffer`` back through the rung (under
          it for a LONG, over it for a SHORT): ``target_hold_guard``;
        - when the bar that touched the target is in the frame, delivered a
          few seconds after it closes, with one verdict on that bar. A close
          at or through the target at least ``LADDER_TOUCH_CLOSE_POSITION_MIN``
          of the way up its range (down, for a SHORT) promotes the rung. Any
          other close exits at market: ``target_weak_close``. A bar with no
          range has no upper part and is weak; one with an unreadable price
          is weak too;
        - when the touch bar is still missing the configured timeout after
          it closed: ``target_hold_timeout``, at market.

        A promotion moves the stop to the rung less ``stop_buffer`` (never
        loosening it) and the target to the first later rung more than
        ``LADDER_NEXT_RUNG_GAP_FRAC`` of the touch bar's close beyond that
        close (``_next_unpassed_rung``), or clears it past the last rung,
        which leaves a runner. A price already at the new target starts that
        rung's hold in the same pass. ``stop_buffer`` is the widest of the
        S/R context's level buffer (when it is a finite number),
        ``LADDER_STOP_BUFFER_ZONE_FRAC`` of the rung's zone width and
        ``LADDER_STOP_BUFFER_PRICE_FRAC`` of the touch price.

        The exits belong to the ``risk`` family, and the caller lets
        RiskManager's own exits (stop, peak giveback) win on the same cycle.
        A decided exit stays on the hold, so a failed order is sent again
        with the same reason, whatever the price does next, and the target
        is never taken instead. There is no index veto. Ladder metadata the
        hold cannot read (the rungs, the active rung, the price's time) is
        reported and leaves the target exit in place; a hold it cannot read
        back is dropped whole. Every touch and every verdict is logged on one
        INFO line (``LADDER_TOUCH`` / ``LADDER_VERDICT``), so a dry-run A/B
        can be read from the log.
        """
        if self.config.risk.trade_management_mode != "adaptive_ladder":
            return None
        meta = position.metadata if isinstance(position.metadata, dict) else None
        if meta is None or not bool(meta.get("ladder_management_enabled")):
            return None
        if str(meta.get("asset_type") or ASSET_TYPE_EQUITY) in OPTION_ASSET_TYPES:
            return None
        timeout = self.strategy.exit_policy.ladder_touch_hold_timeout_seconds()
        if timeout is None:
            # The hold is off. One a restart carried over from a run with it
            # on would keep RiskManager off the target for good.
            if meta.pop(LADDER_TOUCH_HOLD_KEY, None) is not None:
                LOG.info("LADDER_VERDICT symbol=%s side=%s outcome=dropped reason=touch_hold_off",
                         position.symbol, position.side.value)
            return None
        if not math.isfinite(last_price) or last_price <= 0:
            return None
        now = pd.Timestamp(sessions.now_et())
        if LADDER_TOUCH_HOLD_KEY in meta:
            decision = self._resolve_touch_hold(position, meta, frame, float(last_price), now)
            if decision is not None or LADDER_TOUCH_HOLD_KEY in meta:
                return decision
        self._start_touch_hold(position, meta, frame, float(last_price), price_at, now, float(timeout))
        return None

    def _ladder_unreadable(self, position: Position, what: str, message: str) -> None:
        self.audit.log_cycle(f"ladder_unreadable:{position.symbol}", what, message, level=logging.WARNING)

    def _start_touch_hold(self, position: Position, meta: dict[str, Any], frame: pd.DataFrame | None,
                          last_price: float, price_at: datetime | None, now: pd.Timestamp, timeout: float) -> None:
        """Start a hold when ``last_price`` is at or through the target. The
        ladder metadata is read back from the position store: a value the
        hold cannot read is reported, and the position keeps its target
        exit."""
        target = safe_float(position.target_price, None, finite=True)
        if target is None:
            return
        long_side = position.side == Side.LONG
        if (last_price < target) if long_side else (last_price > target):
            return
        rungs = _ladder_rungs(meta)
        if rungs is None:
            self._ladder_unreadable(position, "rungs", f"Ladder rungs of {position.symbol} are unreadable "
                                    f"({meta.get('ladder_rungs')!r}); taking the target without a hold")
            return
        active_index = _whole_number(meta.get("ladder_active_index"))
        if active_index is None or not 0 <= active_index < len(rungs):
            self._ladder_unreadable(position, "active_index",
                                    f"Ladder active index {meta.get('ladder_active_index')!r} of {position.symbol} "
                                    f"is unreadable for {len(rungs)} rung(s); taking the target without a hold")
            return
        rung = rungs[active_index]
        zone_width = _finite_number(rung.get("zone_width")) if isinstance(rung, dict) else None
        kind = rung.get("kind") if isinstance(rung, dict) else None
        if zone_width is None or zone_width < 0 or not isinstance(kind, str) or not kind.strip():
            self._ladder_unreadable(position, "rung", f"Ladder rung {active_index} of {position.symbol} is unreadable "
                                    f"({rung!r}); taking the target without a hold")
            return
        touch_at = _aware_time(price_at)
        if touch_at is None:
            self._ladder_unreadable(position, "price_at", f"The time of {position.symbol}'s price {last_price:.4f} is "
                                    f"unknown ({price_at!r}); taking the target without a hold")
            return
        symbol = str(meta.get("underlying") or position.symbol)
        sr_ctx = self.data.get_support_resistance(symbol, current_price=last_price, flip_frame=frame, mode="trading", timeframe_minutes=self.strategy.htf_minutes(), lookback_days=self.strategy.htf_lookback_days(), allow_refresh=True) if self.data is not None else None
        # A level buffer that is not a finite number is no buffer, and one at
        # or below 0 never beats the price term: the zone and price terms set
        # the stop buffer then.
        level_buffer = safe_float(getattr(sr_ctx, "level_buffer", None), None, finite=True)
        stop_buffer = max(0.0 if level_buffer is None else level_buffer,
                          zone_width * LADDER_STOP_BUFFER_ZONE_FRAC, last_price * LADDER_STOP_BUFFER_PRICE_FRAC)
        touch_bar = touch_at.floor("1min")
        hold = _TouchHold(
            rung_index=active_index, rung_count=len(rungs), level=target, zone_width=zone_width, kind=kind,
            touch_price=last_price, touch_at=touch_at, touch_bar=touch_bar, stop_buffer=stop_buffer,
            guard=target - stop_buffer if long_side else target + stop_buffer,
            deadline=touch_bar + pd.Timedelta(minutes=1) + pd.Timedelta(seconds=timeout),
        )
        meta[LADDER_TOUCH_HOLD_KEY] = hold.to_meta()
        LOG.info(
            "LADDER_TOUCH symbol=%s side=%s rung=%d/%d level=%.4f touch=%.4f guard=%.4f bar=%s price_at=%s "
            "deadline=%s",
            position.symbol, position.side.value, active_index + 1, len(rungs), target, last_price, hold.guard,
            touch_bar.strftime("%H:%M"), touch_at.strftime("%H:%M:%S"), hold.deadline.strftime("%H:%M:%S"),
        )

    def _resolve_touch_hold(self, position: Position, meta: dict[str, Any], frame: pd.DataFrame | None,
                            last_price: float, now: pd.Timestamp) -> ExitDecision | None:
        """A decided exit again, else the guard, the verdict or the timeout
        for the hold in progress; None while it still holds (the hold stays
        in the metadata) or once it promoted the rung (the hold is gone)."""
        raw = meta.get(LADDER_TOUCH_HOLD_KEY)
        hold = _TouchHold.read(raw)
        rungs = _ladder_rungs(meta)
        if hold is None or rungs is None:
            # Read back from the position store: a hold it cannot read (or
            # whose rungs it cannot) is dropped whole, reported, and a price
            # at the target starts a new one or takes the target.
            meta.pop(LADDER_TOUCH_HOLD_KEY, None)
            if hold is None:
                message = f"Ladder touch hold of {position.symbol} is unreadable ({raw!r}); dropped"
            else:
                message = (f"Ladder rungs of {position.symbol} are unreadable ({meta.get('ladder_rungs')!r}); "
                           "touch hold dropped")
            self._ladder_unreadable(position, "touch_hold", message)
            return None
        if hold.exit_reason:
            # Decided on an earlier cycle and not booked yet (a failed or
            # deferred order): the same exit, whatever the price does now.
            return ExitDecision(hold.exit_reason, "risk")
        long_side = position.side == Side.LONG
        if (last_price <= hold.guard) if long_side else (last_price >= hold.guard):
            return self._touch_hold_exit(position, meta, hold, f"{TARGET_HOLD_GUARD}:{hold.guard:.4f}", "pending",
                                         None, last_price, now, guard_hit=True)
        bar = _delivered_touch_bar(frame, hold.touch_bar, now)
        if bar is not None:
            verdict, close_pos, close = _touch_bar_verdict(bar, position.side, hold.level)
            if verdict != "strong" or close is None:
                return self._touch_hold_exit(position, meta, hold, f"{TARGET_WEAK_CLOSE}:{hold.level:.4f}", verdict,
                                             close_pos, last_price, now, guard_hit=False)
            del meta[LADDER_TOUCH_HOLD_KEY]
            outcome = self._promote_touched_rung(position, meta, hold, rungs, close)
            self._log_touch_verdict(position, hold, verdict, close_pos, outcome, last_price, now, guard_hit=False)
            return None
        if now >= hold.deadline:
            return self._touch_hold_exit(position, meta, hold, f"{TARGET_HOLD_TIMEOUT}:{hold.level:.4f}", "missing",
                                         None, last_price, now, guard_hit=False)
        return None

    def _touch_hold_exit(self, position: Position, meta: dict[str, Any], hold: _TouchHold, reason: str, verdict: str,
                         close_pos: float | None, last_price: float, now: pd.Timestamp, *,
                         guard_hit: bool) -> ExitDecision:
        """Record the hold's exit on the hold itself and log its verdict."""
        meta[LADDER_TOUCH_HOLD_KEY] = replace(hold, exit_reason=reason).to_meta()
        self._log_touch_verdict(position, hold, verdict, close_pos, f"exit:{exit_reason_code(reason)}",
                                last_price, now, guard_hit=guard_hit)
        return ExitDecision(reason, "risk")

    @staticmethod
    def _promote_touched_rung(position: Position, meta: dict[str, Any], hold: _TouchHold, rungs: list,
                              bar_close: float) -> str:
        """A strong touch bar: the stop to the rung less the stop buffer, the
        target to the next rung the bar's close has not passed, or none past
        the last rung. Returns ``promoted`` or ``runner``. Every value is
        read before the first write, so the promotion applies whole."""
        long_side = position.side == Side.LONG
        candidate_stop = hold.level - hold.stop_buffer if long_side else hold.level + hold.stop_buffer
        prior_stop = float(position.stop_price)
        tighter = (candidate_stop > prior_stop) if long_side else (candidate_stop < prior_stop)
        next_index = _next_unpassed_rung(rungs, hold.rung_index, bar_close, position.side,
                                         max(bar_close * LADDER_NEXT_RUNG_GAP_FRAC, 1e-6))
        new_target = None if next_index is None else safe_float(rungs[next_index].get("price"), None, finite=True)
        prior_target = safe_float(position.target_price, None)
        if tighter:
            position.stop_price = float(candidate_stop)
            append_management_adjustment(meta, {"manager": "adaptive_ladder", "kind": "stop", "reason": "touch_promoted", "from": prior_stop, "to": float(candidate_stop), "source_level": hold.level})
        meta["ladder_defense_price"] = hold.level
        meta["ladder_defense_zone_width"] = hold.zone_width
        meta["ladder_defense_kind"] = hold.kind
        if next_index is not None and new_target is not None:
            position.target_price = new_target
            meta["ladder_active_index"] = int(next_index)
            meta["ladder_final_rung_cleared"] = False
            append_management_adjustment(meta, {"manager": "adaptive_ladder", "kind": "target", "reason": "next_rung", "from": prior_target, "to": new_target, "source_level": new_target})
            return "promoted"
        position.target_price = None
        meta["ladder_final_rung_cleared"] = True
        append_management_adjustment(meta, {"manager": "adaptive_ladder", "kind": "target", "reason": "final_rung_runner", "from": prior_target, "to": None, "source_level": hold.level})
        return "runner"

    @staticmethod
    def _log_touch_verdict(position: Position, hold: _TouchHold, verdict: str, close_pos: float | None,
                           outcome: str, last_price: float, now: pd.Timestamp, *, guard_hit: bool) -> None:
        target = safe_float(position.target_price, None)
        LOG.info(
            "LADDER_VERDICT symbol=%s side=%s rung=%d/%d level=%.4f touch=%.4f bar=%s verdict=%s close_pos=%s "
            "guard_hit=%s outcome=%s price=%.4f stop=%.4f target=%s held_s=%.0f",
            position.symbol, position.side.value, hold.rung_index + 1, hold.rung_count, hold.level,
            hold.touch_price, hold.touch_bar.strftime("%H:%M"), verdict,
            "-" if close_pos is None else f"{close_pos:.2f}", "yes" if guard_hit else "no", outcome,
            last_price, float(position.stop_price), "none" if target is None else f"{target:.4f}",
            (now - hold.touch_at).total_seconds(),
        )

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
            self.underlying_price_for_position(position, bars) if is_option_strategy(position.strategy)
            else float(exit_price)
        )
        self.risk.register_exit(
            management_symbol, realized, additional_symbol=key, side=position.side,
            level=RiskManager.same_level_anchor(position.strategy, position.side, position.metadata,
                                                position.entry_price),
            exit_price=level_exit, atr=exit_atr,
        )

    def _book_broker_exit(self, key: str, position: Position, exit_qty: int, exit_price: float,
                          reason: str, bars, *, result_message: str, attempt_status: str,
                          fill_price_estimated: bool) -> float:
        """Record an exit the engine learned of from the BROKER -- a resting
        bracket child that filled, or an exit order that filled after its
        submit call returned -- mirroring the manage_positions tail. Returns
        the realized P&L of the slice."""
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
            self._register_closed_position(key, position, realized, float(exit_price), bars)
            self.positions.pop(key, None)
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
        for child_key, reason in (("stop_order_id", "broker_stop"), ("target_order_id", "broker_target")):
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
        else:
            # The child that protects the position: its stop, or once an
            # unconfirmed retire dropped a dead stop, what is left of it.
            guard_key = "stop_order_id" if bracket.get("stop_order_id") else "target_order_id"
            self._retire_dead_bracket(key, position, bracket, guard_key, child_states.get(guard_key), bars)

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
                                  f"broker_{child}"),
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
            fresh = active_broker_bracket(position)
            if fresh is not None and fresh is not bracket:
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
            self.book_bracket_cancel_fills(key, position, bracket, cancel, last_price, bars)
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
        return isinstance(bracket, dict) and not bracket.get("simulated") and not bracket.get("active")

    def _reprotect_remainder(self, position: Position, qty: int | None = None) -> None:
        """Resting broker protection, at the current engine levels, for the
        ``qty`` shares of a position no exit order covers (default: all of
        it) after its bracket was cancelled to send an exit.

        The bracket the position still tracks goes along as
        ``known_bracket``: a live one -- the remainder bracket placed beside
        a working slice, here or by a startup restore (which sizes it the
        same way since 2026-09-25), or a full-size one a resize could not
        shrink -- is adopted and resized to ``qty``. Submitting fresh beside
        it orphaned the old OCO, and both sold when the stop traded
        (2026-09-24)."""
        reprotected = self.executor.ensure_position_protected(
            str(position.metadata.get("underlying") or position.symbol),
            int(position.qty) if qty is None else int(qty), position.side, float(position.entry_price),
            float(position.stop_price), position.target_price,
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
            self._record_exit_marker(position, str(record.get("family") or ""), record.get("marker"), "booked")
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
        stop placed beside the slice owns the stop exit, so the risk check
        leaves it to the broker (see risk.update_position)."""
        outstanding = working_exit_outstanding_qty(position)
        if last_price is None or outstanding >= int(position.qty):
            return False
        risk_exit, risk_reason = self.risk.update_position(position, float(last_price))
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
          management runs: the RiskManager check on the levels as the failed
          step left them, so its stop and target still fire (a touch hold
          left by a failed ladder pass is dropped, since it keeps the
          target off), then force flatten and the exit order.
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
        """
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
                self._manage_position(_now, key, position, bars, order_states, failures)
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
                         failures: dict[str, str]) -> None:
        """One position's management cycle; see ``manage_positions``. It
        returns where the position's cycle ends."""
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
        asset_type = str(position.metadata.get("asset_type") or ASSET_TYPE_EQUITY)
        management_symbol = str(position.metadata.get("underlying") or position.symbol)
        management_frame = bars.get(management_symbol)
        if asset_type not in OPTION_ASSET_TYPES:
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
            # the metadata is dropped, since it keeps RiskManager off the
            # target.
            ladder_exit: ExitDecision | None = None
            try:
                self._sr_flip_management_confirmed(position, management_frame, float(last_price))
            except Exception as exc:
                self._position_failed(key, "the sr_flip manager", exc, failures, rest_runs=True)
            try:
                price_at = market_snapshot.get("price_at") if isinstance(market_snapshot, dict) else None
                ladder_exit = self._adaptive_ladder_management(position, management_frame, float(last_price), price_at)
            except Exception as exc:
                if isinstance(position.metadata, dict):
                    position.metadata.pop(LADDER_TOUCH_HOLD_KEY, None)
                self._position_failed(key, "the adaptive ladder", exc, failures, rest_runs=True)
            self._update_position_diagnostics(position, last_price, underlying_price)
            risk_exit, risk_reason = self.risk.update_position(position, last_price)
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
                        "asset_type": str(position.metadata.get("asset_type") or ASSET_TYPE_EQUITY),
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
        exit_context = self._position_exit_context(position, reason, last_price, underlying_price, market_snapshot, bars)
        exit_context["exit_family"] = decision.family
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
        result = self.executor.close_position(position, requested_qty, data=self.data, market_snapshot=market_snapshot)
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
