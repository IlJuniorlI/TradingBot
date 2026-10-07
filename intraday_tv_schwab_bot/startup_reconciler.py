# SPDX-License-Identifier: MIT
"""StartupReconciler — owns bootstrap-time broker state recovery.

Extracted from ``IntradayBot`` as a follow-up to Phase 5 Step 10. Runs
once at bot startup (via ``reconcile()``) to query Schwab for pre-existing
broker positions + working orders, then depending on
``config.runtime.startup_reconcile_mode`` either:

  - ``ignore``: do nothing
  - ``log_only``: record what was found but don't block or restore
  - ``block``: set ``trading_blocked_*`` so the entry gate suppresses new trades
  - ``restore_basic``: materialize broker positions into ``self.positions``
    with fresh default stop/target levels
  - ``restore_hybrid``: same as ``restore_basic`` but prefer metadata-stored
    stop/target/highest/lowest if a match is found in ``reconcile_metadata_store``

Also owns the per-cycle ``is_entry_blocked(symbol)`` check that the entry
gate calls for symbols in the startup-reconcile block set (positions we
chose to ignore at startup but haven't yet confirmed still don't exist at
broker — a rechecks-on-entry pattern).

Design notes:

- ``self.positions`` is a shared-reference dict with ``IntradayBot``.
  Restored positions land here directly.
- ``save_reconcile_metadata`` injected as callable because the engine
  decides how a save writes (an upsert until a broker reconcile has
  succeeded, then a full replace) for the entry and exit paths too, and
  ``ReconcileMetadataStore.save_if_changed`` skips a save that would write
  what it last wrote. StartupReconciler just triggers a save after
  mutations.
- ``settle_unsettled_entry_orders`` / ``unsettled_entry_order_ids`` injected
  as callables (live on EntryGatekeeper). The account holds an unsettled
  entry order's fills before the gatekeeper books them, so the reconcile
  settles those orders first and leaves the positions of any still unsettled
  to the gatekeeper (2026-09-25). Empty at startup.
- ``book_bracket_cancel_fills`` injected as callable (lives on
  PositionManager): what a cancelled bracket's children filled is booked at
  the broker's price with the risk manager's registration, as the manager
  books it. ``risk`` receives the estimated loss of a close outside the bot,
  and gives a restored position its trail (``stock_position_trail_pct``) as
  the entry path gives a filled one; its default-distance levels are the
  entry path's too (``trade_management.default_levels``).
- Trading-blocked state (``trading_blocked_reason`` / ``trading_blocked_message``)
  moved off ``IntradayBot`` onto this class. Engine reads via
  ``self.startup_reconciler.trading_blocked_reason`` at step() + publish
  time, so the message never drifts from the reconciliation that produced it.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable

from .broker_payloads import (
    DISASTER_SYNC_MODE,
    ORDER_REPLACED,
    active_broker_bracket,
    booked_fills_ledger,
    bracket_order_ids,
    broker_position_side_qty,
    broker_quantity,
    is_disaster_stop,
    listed_stop,
    note_booked_child_fills,
    order_row_filled_qty,
    order_status_class,
    resting_exit_stop,
    sent_exit_stop,
    working_exit_outstanding_qty,
)
from .config import BotConfig
from .data_feed import MANAGEMENT_PRICE_KEYS, MarketDataStore
from .models import ASSET_TYPE_EQUITY, ASSET_TYPE_OPTION_SINGLE, ASSET_TYPE_OPTION_VERTICAL, Position, Side, asset_type_of
from .paper_account import PaperAccount
from .numeric import first_float, safe_float
from .position_metrics import initial_risk_per_unit
from .position_store import ReconcileMetadataStore
from .risk import RiskManager
from .trade_management import default_levels
from . import sessions
from .sessions import UTC

if TYPE_CHECKING:
    from ._strategies.strategy_base import BaseStrategy

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


def _unreadable_quantity(symbol: str, row: dict[str, Any]) -> str:
    """The message naming a broker position row whose ``longQuantity`` or
    ``shortQuantity`` is not a finite number, which fails a reconcile
    attempt."""
    return (f"the broker position row for {symbol} holds an unreadable quantity "
            f"(longQuantity={row.get('longQuantity')!r}, shortQuantity={row.get('shortQuantity')!r})")


def _held_side_qty(held: dict[str, dict[str, Any]], symbol: Any) -> tuple[Side | None, int]:
    """The side and whole-unit quantity of the broker's row for *symbol*
    (``broker_position_side_qty``; ``(None, 0)`` when it holds none). Raises
    ``ValueError`` (``_unreadable_quantity``) when the row's quantity cannot
    be read, which says nothing about what is held."""
    key = str(symbol or "").upper().strip()
    side, qty = broker_position_side_qty(held.get(key))
    if qty is None:
        raise ValueError(_unreadable_quantity(key, held[key]))
    return side, qty


class StartupReconciler:
    def __init__(
        self,
        config: BotConfig,
        *,
        executor,
        data: MarketDataStore,
        account: PaperAccount,
        risk: RiskManager,
        strategy: BaseStrategy,
        positions: dict[str, Position],
        reconcile_metadata_store: ReconcileMetadataStore,
        save_reconcile_metadata: Callable[[], None],
        book_bracket_cancel_fills: Callable[..., None],
        settle_unsettled_entry_orders: Callable[[], None],
        unsettled_entry_order_ids: Callable[[], dict[str, str]],
    ) -> None:
        self.config = config
        self.executor = executor
        self.data = data
        self.account = account
        self.risk = risk
        self.strategy = strategy
        self.positions = positions
        self.reconcile_metadata_store = reconcile_metadata_store
        self._save_reconcile_metadata = save_reconcile_metadata
        self._book_bracket_cancel_fills = book_bracket_cancel_fills
        self._settle_unsettled_entry_orders = settle_unsettled_entry_orders
        self._unsettled_entry_order_ids = unsettled_entry_order_ids
        # State set by reconcile() and read by engine + entry gate.
        self.trading_blocked_reason: str | None = None
        self.trading_blocked_message: str | None = None
        self.result: dict[str, Any] = {"positions": [], "working_orders": []}
        self._entry_block_symbols: set[str] = set()

    # ------------------------------------------------------------------
    # Symbol-set helpers (ignore list + ignored-open detection).
    # ------------------------------------------------------------------

    def _ignore_symbols(self) -> set[str]:
        # A list of non-blank strings, or null for none (checked at load).
        raw = self.config.runtime.startup_reconcile_ignore_symbols or []
        return {symbol.upper().strip() for symbol in raw}

    def _ignored_open_position_symbols(self, positions: list[dict[str, Any]]) -> set[str]:
        ignored = self._ignore_symbols()
        if not ignored:
            return set()
        blocked: set[str] = set()
        for row in positions:
            symbol = str(row.get("symbol") or "").upper().strip()
            if not symbol or symbol not in ignored:
                continue
            # A held broker row reads the way the settle reads it: a row
            # both long and short is not held, and one whose quantity cannot
            # be read may be, as every symbol may be when the account read
            # fails (2026-09-26; it read as not held).
            _side, qty = broker_position_side_qty(row)
            if qty is None or qty > 0:
                blocked.add(symbol)
        return blocked

    def _filter_reconcile_positions(self, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ignored = self._ignore_symbols()
        if not ignored:
            return positions
        out: list[dict[str, Any]] = []
        for row in positions:
            symbol = str(row.get("symbol") or "").upper().strip()
            if symbol and symbol in ignored:
                continue
            out.append(row)
        return out

    def _filter_reconcile_orders(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ignored = self._ignore_symbols()
        if not ignored:
            return orders
        out: list[dict[str, Any]] = []
        for row in orders:
            symbols = [str(symbol).upper().strip() for symbol in (row.get("symbols") or []) if str(symbol).strip()]
            kept = [symbol for symbol in symbols if symbol not in ignored]
            if not kept:
                continue
            cloned = dict(row)
            cloned["symbols"] = kept
            out.append(cloned)
        return out

    def _is_restore_eligible_symbol(self, symbol: str) -> bool:
        symbol_upper = str(symbol).upper().strip()
        if not symbol_upper:
            return False
        strategy_obj = self.strategy
        allowed_symbols = None
        if strategy_obj is not None:
            # An error raises and fails the restore attempt, which blocks
            # entries and is retried. Until 2026-09-26 it read as "no
            # universe" and made every broker position eligible.
            allowed_symbols = strategy_obj.restore_eligible_symbols()
        if allowed_symbols is not None:
            allowed = {str(sym).upper().strip() for sym in allowed_symbols if str(sym).strip()}
            return symbol_upper in allowed if allowed else False
        return True

    # ------------------------------------------------------------------
    # Per-cycle entry-block check (called by EntryGatekeeper each cycle).
    # ------------------------------------------------------------------

    def _refresh_entry_block(self, symbol: str) -> bool:
        symbol_upper = str(symbol).upper().strip()
        if not symbol_upper or symbol_upper not in self._entry_block_symbols:
            return False
        raw_positions = self.executor.fetch_account_positions()
        if raw_positions is None:
            LOG.warning("Could not refresh startup-reconcile entry block for %s: the broker account read failed", symbol_upper)
            return True
        if symbol_upper in self._ignored_open_position_symbols(raw_positions):
            return True
        self._entry_block_symbols.discard(symbol_upper)
        if isinstance(self.result, dict):
            remaining = sorted(sym for sym in self.result.get("ignored_open_position_symbols", []) if str(sym).upper().strip() != symbol_upper)
            self.result["ignored_open_position_symbols"] = remaining
        LOG.info("Cleared startup-reconcile entry block for %s after broker recheck found no ignored open position", symbol_upper)
        return False

    def is_entry_blocked(self, symbol: str) -> bool:
        symbol_upper = str(symbol).upper().strip()
        if symbol_upper in self.positions:
            self._entry_block_symbols.discard(symbol_upper)
            return False
        return self._refresh_entry_block(symbol_upper) if symbol_upper in self._entry_block_symbols else False

    # ------------------------------------------------------------------
    # Restore path — materialize broker positions into self.positions.
    # ------------------------------------------------------------------

    def _load_reconcile_metadata(self) -> dict[str, Position]:
        try:
            return self.reconcile_metadata_store.load_positions()
        except Exception as exc:
            LOG.warning("Could not load startup reconcile metadata: %s", exc)
            return {}

    def _restore_levels_for_stock_position(self, side: Side, entry_price: float, current_price: float | None = None, metadata: dict[str, Any] | None = None) -> tuple[float, float | None, float | None, float | None, float | None]:
        """A restored stock position's levels without saved ones: the
        default-distance stop and target (``trade_management.default_levels``,
        as a fill the signal's levels no longer fit gets), the watermarks from
        the entry and the current price, and the trail a new entry gets."""
        entry = max(0.01, float(entry_price))
        current = max(0.01, float(current_price if current_price is not None else entry))
        stop, target = default_levels(side, entry, self.config.risk)
        trail_pct = self.risk.stock_position_trail_pct(metadata)
        return float(stop), float(target), float(max(entry, current)), float(min(entry, current)), trail_pct

    def _find_reconcile_metadata_match_with_key(self, metadata_positions: dict[str, Position], symbol: str, side: Side, qty: int, entry_price: float) -> tuple[str, Position] | None:
        symbol_upper = str(symbol).upper().strip()
        tolerance = max(0.05, abs(float(entry_price)) * 0.003)
        for key, position in metadata_positions.items():
            if str(position.symbol).upper().strip() != symbol_upper:
                continue
            if position.side != side:
                continue
            if position.strategy != self.config.strategy:
                continue
            if abs(float(position.entry_price) - float(entry_price)) > tolerance:
                continue
            if self.config.schwab.dry_run and working_exit_outstanding_qty(position):
                # A dry run reads no order state, so it can never settle the
                # row's working exit order: restored with it, the paper
                # position was never managed again, and every cycle tried to
                # cancel the user's real order (2026-09-25). It restores
                # basic, and the engine owns the exits.
                continue
            held_when_saved = int(qty)
            if int(position.qty) != held_when_saved and working_exit_outstanding_qty(position):
                # Its working exit order sold shares while the bot was down.
                # They are this position's, booked from the order's own fill
                # record by the first cycle; strict equality lost the metadata
                # (levels, marker, the order) to a restore_basic (2026-09-25).
                # A gap the order does not explain still does not match. An
                # order whose state cannot be read fails the attempt: read as
                # no fills, it restored the position basic at the broker
                # quantity with its stop resized to all of it beside the exit
                # order's outstanding shares, and lost the order for good.
                unbooked = self._unbooked_working_exit_fills(position)
                if unbooked is None:
                    raise RuntimeError(f"the working exit order state of {symbol_upper} could not be read")
                held_when_saved += unbooked
            if int(position.qty) != held_when_saved:
                continue
            return str(key), position
        return None

    def _reprotect_restored_position(self, position: Position, working_orders: list[dict[str, Any]]) -> None:
        """Make a restored position's bracket state truthful before it is managed.

        The hybrid path rehydrates metadata written before the restart, so a
        restored position can carry a ``bracket`` dict whose child order ids
        were cancelled or filled while the bot was down. Left alone, that stale
        dict makes TradeManager leave the target exit to a child that no longer
        rests (until 2026-09-28 the stop exit too: unprotected AND unmanaged),
        and the manager replace and cancel orders that are gone.

        ``ensure_position_protected`` adopts the children when they are still
        working and submits fresh protection when they are not; it returns None
        when neither bracket mode nor the disaster stop is on, in which case
        any stale key is dropped so the engine unambiguously owns the exits.
        A disaster stop rests at the price the position opened with, saved in
        its metadata; a position restored without one (``restore_basic``, a
        row saved before the disaster stop was on) gets it from its entry
        and initial stop, as a new entry does (its current stop for a row
        saved without an initial stop). A restore whose protection step
        raises leaves the disaster stop ``unconfirmed`` at that price for the
        shares held, which the first management cycle looks up before it
        places one. The persisted bracket goes along
        as ``known_bracket``: its child ids are the ones the bot last tracked,
        so a stop replaced before the restart is adopted instead of being read
        as dead (its original, off the parent, is REPLACED) and stacked on.
        What the adopted stop sold before the snapshot is booked on it
        (``booked_child_fills``, at the snapshot's price:
        ``booked_child_notional``): the shares restored are already net of
        it.
        A dry run's disaster stop adopts nothing: every stop in the account
        is the user's (2026-09-29).

        A restored working exit order (a slice or a full exit left working
        before the restart) still sells its outstanding shares, so the
        resting stop covers only the rest, as the manager's
        ``_reprotect_beside_working_order`` sizes it; a full exit still
        working covers every share and gets nothing beside it (2026-09-25).
        Protecting the full quantity rested stop + order above what was held,
        and a stop that triggered while the order worked sold the position
        net short. See ``_working_exit_covered_qty`` for an order that
        settled while the bot was down.
        """
        metadata = position.metadata if isinstance(position.metadata, dict) else None
        if metadata is None:
            return
        uncovered = int(position.qty) - self._working_exit_covered_qty(position)
        if uncovered <= 0:
            # A live full exit: its settle re-protects whatever it leaves.
            metadata.pop("bracket", None)
            return
        stale = metadata.get("bracket") if isinstance(metadata.get("bracket"), dict) else None
        parent_order_id = stale.get("parent_order_id") if stale else None
        # The saved stop while the working-order snapshot still lists it,
        # otherwise the stop resting for the position now. A stop moved in the
        # app (REPLACED under a new id), or by a trail sync whose save was lost
        # in a crash, left the saved id dead: fresh protection went in beside
        # the live one, and both sold the position net short (2026-09-25).
        saved = stale if stale and stale.get("stop_order_id") else None
        listed = {str(order.get("orderId")): order for order in working_orders}
        # A saved record that carries no id but the price and quantity it
        # sent (an unconfirmed disaster stop: the submit's outcome was
        # unknown, or the process ended before it was looked up) knows its
        # stop exactly: that one is adopted first, never another stop on the
        # symbol, which may be one placed by hand (2026-09-29).
        sent = (
            sent_exit_stop(working_orders, position.symbol, position.side, stop_price=stale.get("stop_price"),
                           qty=stale.get("qty"))
            if stale and is_disaster_stop(stale) and not stale.get("stop_order_id") else None
        )
        disaster = self.executor.disaster_stop_enabled()
        known: dict[str, Any] | None
        if disaster and self.config.schwab.dry_run:
            # A dry run rests no stop, so a stop in the real account is the
            # user's: the paper position's simulated record takes none of
            # them, and the reconcile still counts it as a foreign order
            # (2026-09-29). Until then the paper position adopted it as its
            # own disaster stop, which lifted working_orders_present in the
            # restore modes.
            known = None
        elif saved is not None and str(saved["stop_order_id"]) in listed:
            known = saved
        elif sent is not None:
            known = listed_stop(sent)
        else:
            known = resting_exit_stop(working_orders, position.symbol, position.side) or saved
        if known is not None:
            # The shares restored are the ones the account holds now, net of
            # what the stops the snapshot lists sold before it: those fills
            # are booked, so the fill reconcile and a later cancel book only
            # the ones after it (2026-09-29). Until then a stop that had
            # filled in part while the bot was down was booked in full when
            # the rest of it filled, and EXIT OVERFILLED named a short the
            # account did not hold.
            # At the snapshot's average (fillPrice, flagged as the row flags
            # it), which a later fill of the stop is priced net of
            # (broker_payloads.unbooked_fill_price, 2026-10-06).
            booked = booked_fills_ledger(known)
            for order_id in bracket_order_ids(known):
                if order_id in listed and order_row_filled_qty(listed[order_id]) > 0:
                    row = listed[order_id]
                    filled = order_row_filled_qty(row)
                    price = safe_float(row.get("fillPrice"), None, finite=True)
                    note_booked_child_fills(booked, order_id, filled, None if price is None else price * filled,
                                            row.get("fillPriceEstimated") is not False)
            if booked:
                known = {**known, **booked}
        initial_stop = safe_float(metadata.get("initial_stop_price"), None, finite=True)
        level: float | None = None
        try:
            self.executor.stamp_disaster_stop_price(
                metadata, position.side, float(position.entry_price),
                float(position.stop_price) if initial_stop is None else initial_stop,
            )
            if disaster:
                level = self.executor.protective_stop_level(position.side, self.executor.resting_stop_price(position))
            refreshed = self.executor.ensure_position_protected(
                str(metadata.get("underlying") or position.symbol),
                uncovered, position.side, self.executor.resting_stop_price(position), position.target_price,
                initial_risk=initial_risk_per_unit(position),
                parent_order_id=str(parent_order_id) if parent_order_id else None,
                known_bracket=known,
            )
        except Exception as exc:
            if disaster:
                # Whether a disaster stop from before the restart still rests
                # is unknown: none is placed beside it until the manager finds
                # it or learns there is none (ensure_disaster_stop). The
                # engine owns its exits either way.
                LOG.error(
                    "Could not re-establish the disaster stop of restored position %s (%s: %s); one may still "
                    "rest from before the restart, so it is looked up before another is placed",
                    position.symbol, type(exc).__name__, exc,
                )
                # The stop a restore rests: the saved price, for the shares
                # held, sent at the restart. The lookup adopts only that one
                # (sent_exit_stop), entered from a minute before the restart
                # on: an earlier stop at that price and size (a closed
                # position's, filled) is never taken for it (2026-09-29).
                metadata["bracket"] = {"sync_mode": DISASTER_SYNC_MODE, "legs": "stop_only", "active": False,
                                       "state": "unconfirmed", "stop_price": level, "qty": uncovered,
                                       "sent_at": sessions.now_et().isoformat()}
                return
            LOG.warning(
                "Could not re-establish broker protection for restored position %s: %s: %s; "
                "dropping stale bracket so the engine owns the exits", position.symbol, type(exc).__name__, exc,
            )
            metadata.pop("bracket", None)
            return
        if refreshed is None:
            metadata.pop("bracket", None)
            return
        metadata["bracket"] = refreshed
        LOG.info(
            "Restored position %s protection state=%s stop=%s target=%s",
            position.symbol, refreshed.get("state"), refreshed.get("stop_price"), refreshed.get("target_price"),
        )

    @staticmethod
    def _broker_held_qty(position: Position, held: dict[str, dict[str, Any]]) -> int | None:
        """Units the broker holds of *position* (0 when none), or None when
        its vertical legs are out of step. A row whose quantity is not a
        finite number raises ``ValueError`` naming it (``_held_side_qty``)."""
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        asset_type = asset_type_of(meta)
        if asset_type == ASSET_TYPE_OPTION_VERTICAL:
            long_side, long_qty = _held_side_qty(held, meta.get("long_leg_symbol"))
            short_side, short_qty = _held_side_qty(held, meta.get("short_leg_symbol"))
            if long_qty <= 0 and short_qty <= 0:
                return 0
            if long_side != Side.LONG or short_side != Side.SHORT or long_qty != short_qty:
                return None
            return int(long_qty)
        if asset_type == ASSET_TYPE_OPTION_SINGLE:
            symbol = meta.get("option_symbol")
        else:
            symbol = meta.get("underlying") or position.symbol
        side, qty = _held_side_qty(held, symbol)
        return int(qty) if side == position.side else 0

    def _working_exit_order_state(self, position: Position) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
        """The position's tracked working exit record and the order's broker
        state row (None when it cannot be read), or None when it tracks no
        working exit order."""
        record = position.metadata.get("working_exit_order") if isinstance(position.metadata, dict) else None
        if not isinstance(record, dict):
            return None
        return record, self.executor.order_state(str(record.get("order_id") or ""))

    def _unbooked_working_exit_fills(self, position: Position) -> int | None:
        """Shares the position's own working exit order filled that are not
        booked yet (0 when it tracks none), or None when its state cannot be
        read. PositionManager._exit_order_in_flight books them next cycle."""
        tracked = self._working_exit_order_state(position)
        if tracked is None:
            return 0
        record, state = tracked
        if state is None:
            return None
        return max(0, int(state.get("filled_qty") or 0) - int(record.get("booked_qty") or 0))

    def _working_exit_covered_qty(self, position: Position) -> int:
        """Shares of *position* that a resting stop must leave to its working
        exit order.

        While the order is live, that is everything it may still sell
        (``working_exit_outstanding_qty``, the manager's sizing beside a
        slice). Once it has settled -- filled, or cancelled or expired while
        the bot was down -- it is only its fills not booked yet, which have
        already left the account. Reading the saved record alone left the
        shares a dead order no longer covered without a broker stop until the
        first cycle settled it (2026-09-25). An unreadable state reads as
        live, and so does a REPLACED order (changed in the app): its
        replacement may still sell them, and the first cycle tracks it
        (``PositionManager._follow_replaced_exit``, 2026-09-29)."""
        outstanding = working_exit_outstanding_qty(position)
        tracked = self._working_exit_order_state(position) if outstanding > 0 else None
        if tracked is None:
            return outstanding
        record, state = tracked
        if (state is None or order_status_class(state.get("status")) == ORDER_REPLACED
                or not (state.get("is_filled") or state.get("is_terminal_failure"))):
            return outstanding
        return max(0, int(state.get("filled_qty") or 0) - int(record.get("booked_qty") or 0))

    def _settle_positions_closed_at_broker(self, raw_positions: list[dict[str, Any]],
                                           working_orders: list[dict[str, Any]] | None,
                                           unsettled: set[str]) -> bool:
        """Stop managing what the broker no longer holds.

        The session-boundary re-run of ``reconcile`` exists for positions
        closed while the bot could not trade -- in the Schwab app, or by a
        broker stop -- but restore only ever ADDED positions, so those stayed
        tracked: holding a ``max_positions`` slot, feeding the correlation
        guard and open risk, and sending exits the broker rejects (a
        ``status=`` failure is never rechecked, so they repeat every cycle).

        Each is booked as an exit (``closed_outside_bot``) at the last mark,
        flagged estimated and broker-recovered so reports can tell it apart,
        and dropped; one only partly closed is cut to what remains. Its loss
        goes to the risk manager, an estimated gain does not (2026-09-25): the
        reconcile also runs mid-session on a retry, and dropping the position
        took its open risk out of the daily-loss projection, while a stale
        mark's gain must not loosen ``max_daily_loss``.

        A position with a resting broker bracket has it cancelled first, and
        what its children filled -- a stop that triggered after the last
        management cycle -- is the bracket's exit, not an outside close:
        ``book_bracket_cancel_fills`` books it at the broker's price with the
        risk manager's registration, and only the rest of the gap is booked
        outside. Booked at the mark, a stop filled at 3.91 against a 4.20 mark
        read as a gain, and the risk manager never saw the exit (2026-09-25).
        A cancel that cannot be confirmed leaves the position as it was:
        nothing is booked, nothing is placed beside a bracket that may still
        rest (fresh protection beside it sold the position net short when
        both stops triggered), and the retry sends the cancel again, as the
        manager's cancel-before-exit does. Until then the position is held
        (``settle_pending``): the manager neither exits nor re-protects it at
        a size the broker no longer holds. When the cancel reports fills, the
        account is read again once the bracket is down, since a child can fill
        between the first read and the cancel. What is left is re-protected,
        adopting a stop still resting for it (moved in the app) rather than
        stacking on it, so a bracketed position that keeps shares waits for
        the working-order list.

        Fills of the position's own working exit order are not an outside
        close: the manager books them from the order's record next cycle, so
        they are netted out here, and a position whose order cannot be read
        is left tracked (2026-09-25). So is one an entry order is still
        settling (``unsettled``): the account holds that order's late fills
        before the gatekeeper grows the position by them.

        Skipped in dry-run: those positions are simulated and never reach the
        broker, so the account holding none of them says nothing -- reading
        it as a close wiped every paper position held into a new session.

        Returns False when a position was left tracked because something
        could not be read or confirmed -- its broker row's quantity, its
        working exit order's state, the order list its re-protect needs, or
        its bracket's cancel -- so the reconcile reports failure and the
        engine retries. A vertical whose legs are out of step is left tracked
        without failing the attempt: its rows were read, and a retry would
        read them the same.
        """
        if not self.positions or self.config.schwab.dry_run:
            return True
        held = {str(row.get("symbol") or "").upper().strip(): row for row in raw_positions}
        changed = False
        settled = True
        for key, position in list(self.positions.items()):
            if str(key).upper().strip() in unsettled:
                LOG.warning("%s: its entry order is still settling; leaving it to the entry gatekeeper", key)
                continue
            try:
                remaining = self._broker_held_qty(position, held)
            except ValueError as exc:
                # An unread row is neither held nor closed: the position is
                # left as it is and the attempt fails, so a retry reads the
                # row again. Until 2026-09-26 the attempt still succeeded, and
                # block and log_only read it again only the next day.
                LOG.warning("%s: %s; leaving it tracked, and the retry reads it again", key, exc)
                settled = False
                continue
            if remaining is None:
                LOG.warning("Broker rows for %s do not read as its position (vertical legs out of step); "
                            "leaving it tracked", key)
                continue
            unbooked = self._unbooked_working_exit_fills(position)
            if unbooked is None:
                LOG.warning("%s: its working exit order's fills cannot be read; leaving it tracked", key)
                settled = False
                continue
            # A hold is lifted only here, once the position is decided again:
            # one whose reads failed above keeps it, and the manager still
            # sends nothing for it.
            if isinstance(position.metadata, dict):
                position.metadata.pop("settle_pending", None)
            # What the broker holds once the manager books the position's own
            # working exit fills (next cycle, from the order's fill record).
            # Those are not closed outside the bot: booking them here too
            # counted them twice and dropped a position the broker still held
            # (2026-09-25).
            expected = int(position.qty) - unbooked
            if remaining >= expected:
                continue
            gap = expected - int(remaining)
            bracket = active_broker_bracket(position)
            bracket_filled = 0
            if bracket is not None:
                if working_orders is None and int(remaining) + unbooked > 0:
                    LOG.warning("%s: the broker holds %s of %s, but the working-order list its re-protect needs "
                                "could not be read; holding it for the retry", key, remaining, expected)
                    position.metadata["settle_pending"] = True
                    settled = False
                    continue
                cancel = self.executor.cancel_bracket(bracket)
                if not cancel.ok:
                    LOG.error("%s: the broker holds %s of %s, but its resting bracket cannot be confirmed down (%s); "
                              "holding it until the retry cancels it", key, remaining, expected, cancel.message)
                    position.metadata["settle_pending"] = True
                    settled = False
                    continue
                changed = True
                position.metadata.pop("bracket", None)
                if cancel.filled_qty > 0:
                    # A child can fill between the account read and the cancel;
                    # once the bracket is down nothing of it can, so a second
                    # read sizes what is held. Without it the settle kept, and
                    # re-protected, shares the stop had sold after the read.
                    # The working exit's state is read again after it, in the
                    # reconcile's order (account first): a slice fill landing
                    # in between was otherwise booked here and again by the
                    # manager from the order's record.
                    fresh = self.executor.fetch_account_positions()
                    try:
                        fresh_remaining = None if fresh is None else self._broker_held_qty(
                            position, {str(row.get("symbol") or "").upper().strip(): row for row in fresh})
                    except ValueError:
                        fresh_remaining = None      # an unread row, as an unread account
                    fresh_unbooked = None if fresh_remaining is None else self._unbooked_working_exit_fills(position)
                    if fresh_remaining is None or fresh_unbooked is None:
                        LOG.warning("%s: the broker could not be read again after its bracket's fills; sizing from "
                                    "the first read, and the retry sizes it again", key)
                        settled = False
                        bracket_filled = min(int(position.qty), int(cancel.filled_qty))
                    else:
                        remaining, unbooked = fresh_remaining, fresh_unbooked
                        expected = int(position.qty) - unbooked
                        gap = max(0, expected - int(remaining))
                        bracket_filled = min(int(position.qty), int(cancel.filled_qty), gap)
                    if bracket_filled > 0:
                        self._book_bracket_cancel_fills(key, position, bracket,
                                                        replace(cancel, filled_qty=bracket_filled),
                                                        self.account.last_prices.get(position.symbol), {})
                        if key not in self.positions:
                            continue
            closed_qty = max(0, gap - bracket_filled)
            kept = int(position.qty) - closed_qty
            if closed_qty > 0:
                mark = self.account.last_prices.get(position.symbol)
                exit_price = float(mark) if mark is not None and float(mark) > 0 else float(position.entry_price)
                exited = copy.copy(position)
                exited.qty = closed_qty
                realized = self.account.record_exit(
                    exited, exit_price, "closed_outside_bot",
                    final_exit=kept <= 0,
                    remaining_qty_after_exit=kept,
                    fill_price_estimated=True,
                    broker_recovered=True,
                )
                if float(realized) < 0:
                    self.risk.register_realized_pnl(float(realized))
                changed = True
                LOG.warning("%s: broker holds %s of %s; %s closed outside the bot, booked at the last mark %.4f "
                            "(estimated)", key, remaining, expected, closed_qty, exit_price)
            if kept <= 0:
                self.positions.pop(key, None)
                continue
            position.qty = kept
            if isinstance(position.metadata, dict):
                position.metadata["qty"] = kept
            if bracket is not None:
                # The snapshot predates the cancel: the cancelled children
                # would read as a stop still resting for the position.
                cancelled = bracket_order_ids(bracket)
                self._reprotect_restored_position(
                    position, [order for order in working_orders or [] if str(order.get("orderId")) not in cancelled],
                )
        if changed:
            self._save_reconcile_metadata()
        return settled

    def _foreign_working_orders(self, working_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Working orders no restored position owns: its resting protection,
        or the exit order it left working before the restart (settled from
        its own fill record by the first management cycle). Counting that
        exit as foreign blocked every entry for the session, and "clear
        them" meant cancelling the position's own exit (2026-09-25)."""
        owned: set[str] = set()
        for position in self.positions.values():
            record = position.metadata.get("working_exit_order") if isinstance(position.metadata, dict) else None
            if isinstance(record, dict) and record.get("order_id"):
                owned.add(str(record["order_id"]))
            bracket = position.metadata.get("bracket") if isinstance(position.metadata, dict) else None
            # A dry run's bracket is simulated (the engine owns the exits) but
            # keeps the ids of the real protection it stands for: that order
            # is the position's own in a dry run too, and read as foreign it
            # blocked every entry of the dry run (2026-09-25).
            if not isinstance(bracket, dict) or not (bracket.get("active") or bracket.get("simulated")):
                continue
            owned |= bracket_order_ids(bracket)
        return [order for order in working_orders if str(order.get("orderId")) not in owned]

    def _drop_retired_orders(self, orders: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """The startup-snapshot orders still live now. A restore that resizes
        an adopted child replaces it, and the session-boundary settle cancels
        the bracket of a position closed outside the bot: the old ids read
        REPLACED or CANCELED at the broker but are still in the snapshot, and
        counting them as foreign blocked every entry for the session
        (2026-09-25).

        The listing covers only its lookback (8 hours), while the snapshot
        covers ``startup_order_lookback_days``, so an id it does not return
        (a stop entered before an overnight hold, or hours before the
        restart) is read on its own with ``order_details``. Reading the
        missing id as live kept it foreign. Unreadable state keeps an order
        (fail closed) and returns False with the list, so the reconcile is
        retried rather than blocking entries for the day on one bad read."""
        states = self.executor.fetch_order_states()
        if states is None:
            return orders, False
        live: list[dict[str, Any]] = []
        all_read = True
        for order in orders:
            order_id = str(order.get("orderId"))
            state = states.get(order_id)
            if state is None:
                state = self.executor.order_state(order_id)
            if state is None:
                all_read = False
                live.append(order)
            elif not (state.get("is_terminal_failure") or state.get("is_filled")):
                live.append(order)
        return live, all_read

    def _restore_broker_positions(self, positions: list[dict[str, Any]], *, use_metadata: bool,
                                  working_orders: list[dict[str, Any]],
                                  unsettled: set[str]) -> tuple[int, int, list[str]]:
        """Restore the broker's rows into ``self.positions``. Returns how many
        were restored and how many skipped, and a message naming each row
        that cannot be used (a quantity or an ``averagePrice`` that does not
        read).

        The caller fails the attempt on those messages, once every other row
        is restored: those positions are tracked and managed, stops included,
        and the retry restores an unusable row once it reads (a tracked
        symbol is skipped, so nothing is restored twice). Until 2026-09-26
        the first unusable row failed the attempt on the spot, and every row
        after it waited, unmanaged, for a retry that could read it. The
        hybrid prune waits for an attempt with no unusable row: such a row's
        saved levels are what its retry restores it from.
        """
        if self.config.active_is_option:
            LOG.warning("startup_reconcile_mode=%s does not restore option strategies; leaving options handling unchanged", self.config.runtime.startup_reconcile_mode)
            return 0, len(positions), []
        metadata_positions = self._load_reconcile_metadata() if use_metadata else {}
        matched_metadata_keys: set[str] = set()
        restored = 0
        skipped = 0
        unusable: list[str] = []
        for row in positions:
            symbol = str(row.get("symbol") or "").upper().strip()
            asset_type = str(row.get("assetType") or "").upper().strip()
            long_qty = broker_quantity(row.get("longQuantity"))
            short_qty = broker_quantity(row.get("shortQuantity"))
            if long_qty is None or short_qty is None:
                # Skipping the row left what the broker holds unmanaged
                # (2026-09-26).
                unusable.append(_unreadable_quantity(symbol or "?", row))
                continue
            qty = long_qty if long_qty > 0 else short_qty
            if not symbol or qty <= 0:
                skipped += 1
                continue
            if asset_type and asset_type != ASSET_TYPE_EQUITY:
                LOG.warning("Skipping startup restore for %s assetType=%s; only equity positions are restored", symbol, asset_type)
                skipped += 1
                continue
            if not self._is_restore_eligible_symbol(symbol):
                LOG.warning("Skipping startup restore for %s because it is outside the active strategy restore universe", symbol)
                skipped += 1
                continue
            if symbol in self.positions:
                continue
            if symbol in unsettled:
                # An entry order still settling bought these shares, and the
                # gatekeeper adopts them from the order's own fill record.
                # Restoring them as well tracked the fill twice: the
                # gatekeeper then grew the restored position by the same
                # shares and, in bracket mode, resized its stop to twice what
                # was held, net short on trigger (2026-09-25).
                LOG.warning("Skipping startup restore for %s: its entry order is still settling, and the entry "
                            "gatekeeper adopts its fills", symbol)
                skipped += 1
                continue
            side = Side.LONG if long_qty > 0 else Side.SHORT
            average_price = safe_float(row.get("averagePrice"), finite=True)
            if average_price is None or average_price <= 0:
                # An absent, zero, negative or NaN one read as an entry of
                # 0.01, whose restore_basic levels the live price is already
                # past: the first management cycle exited a LONG at its
                # target and a SHORT at its stop. An infinite one read as an
                # infinite entry (2026-09-26).
                unusable.append(f"the broker position row for {symbol} holds no usable averagePrice "
                                f"({row.get('averagePrice')!r})")
                continue
            entry_price = max(0.01, average_price)
            matched_info = self._find_reconcile_metadata_match_with_key(metadata_positions, symbol, side, qty, entry_price) if use_metadata else None
            matched = matched_info[1] if matched_info is not None else None
            if matched_info is not None:
                matched_metadata_keys.add(str(matched_info[0]))
            if bool(getattr(getattr(self, "strategy", None), "requires_hybrid_startup_restore_metadata", lambda: False)()) and matched is None:
                LOG.warning("Skipping startup restore for %s because %s requires hybrid metadata", symbol, self.config.strategy)
                skipped += 1
                continue
            current_price = entry_price
            # Try to get the actual current market price for accurate watermarks.
            # Using entry_price as current_price resets highest_price/lowest_price
            # to entry, which loosens trailing stops and resets adaptive management.
            if self.data is not None:
                try:
                    self.data.fetch_quotes([symbol], force=True, source="engine:restore_broker_position")
                    current_price = first_float(self.data.get_quote(symbol), *MANAGEMENT_PRICE_KEYS,
                                                default=entry_price, positive=True)
                except Exception:
                    LOG.debug("Could not fetch current price for restored position %s; using entry_price.", symbol, exc_info=True)
            if matched is not None:
                metadata = dict(matched.metadata or {})
                # A hold from the process that saved the row: this reconcile
                # settles the position afresh.
                metadata.pop("settle_pending", None)
                metadata.update({
                    "restored_on_startup": True,
                    "restored_mode": "restore_hybrid",
                    "restored_from_metadata": True,
                    "broker_avg_price": entry_price,
                })
                stop_price = float(matched.stop_price or 0.0)
                target_price = safe_float(matched.target_price, None)
                highest_price = safe_float(matched.highest_price, None)
                lowest_price = safe_float(matched.lowest_price, None)
                trail_pct = safe_float(matched.trail_pct, None)
                if stop_price <= 0:
                    stop_price, fallback_target, fallback_high, fallback_low, fallback_trail = self._restore_levels_for_stock_position(side, entry_price, current_price, metadata)
                    target_price = target_price if target_price is not None else fallback_target
                    highest_price = highest_price if highest_price is not None else fallback_high
                    lowest_price = lowest_price if lowest_price is not None else fallback_low
                    trail_pct = trail_pct if trail_pct is not None else fallback_trail
                trail_pct = self.risk.stock_position_trail_pct(metadata, trail_pct)
                position = Position(
                    symbol=symbol,
                    strategy=self.config.strategy,
                    side=side,
                    # The saved quantity: any gap to the broker's is the
                    # working exit's unbooked fills, which the first cycle
                    # books from the order's record (2026-09-25).
                    qty=int(matched.qty),
                    entry_price=entry_price,
                    entry_time=matched.entry_time,
                    stop_price=float(stop_price),
                    target_price=target_price,
                    trail_pct=trail_pct,
                    highest_price=highest_price,
                    lowest_price=lowest_price,
                    pair_id=matched.pair_id,
                    reference_symbol=matched.reference_symbol,
                    metadata=metadata,
                )
            else:
                metadata = {
                    "restored_on_startup": True,
                    "restored_mode": "restore_basic",
                    "restored_from_metadata": False,
                    "broker_avg_price": entry_price,
                }
                stop_price, target_price, highest_price, lowest_price, trail_pct = self._restore_levels_for_stock_position(side, entry_price, current_price, metadata)
                # The stop this position starts from is its initial stop, the
                # R anchor every entered position carries (2026-09-28):
                # without it a resting STOP_LIMIT's offset had no initial R.
                metadata["initial_stop_price"] = float(stop_price)
                position = Position(
                    symbol=symbol,
                    strategy=self.config.strategy,
                    side=side,
                    qty=qty,
                    entry_price=entry_price,
                    entry_time=sessions.now_et(),
                    stop_price=stop_price,
                    target_price=target_price,
                    trail_pct=trail_pct,
                    highest_price=highest_price,
                    lowest_price=lowest_price,
                    pair_id=None,
                    reference_symbol=None,
                    metadata=metadata,
                )
            self._reprotect_restored_position(position, working_orders)
            self.positions[symbol] = position
            try:
                self.account.record_entry(position, float(position.entry_price))
            except Exception as exc:
                LOG.warning("Could not materialize restored paper entry for %s: %s", symbol, exc)
            restored += 1
        if use_metadata and not unusable:
            try:
                # A position already tracked keeps its row. The loop skips it,
                # so it never matches, and the engine's save skips an unchanged
                # position set: pruning its row lost its levels, bracket and
                # working-exit ids to the next restart (the session-boundary
                # re-run and every reconcile retry reach this). An unusable
                # row matches nothing either, so no prune runs beside one.
                removed = self.reconcile_metadata_store.delete_unmatched_positions(matched_metadata_keys | set(self.positions))
                if removed:
                    LOG.info("Pruned %s stale startup reconcile metadata row(s) after hybrid restore", removed)
            except Exception as exc:
                LOG.warning("Could not prune stale startup reconcile metadata after hybrid restore: %s", exc)
        if restored or use_metadata:
            self._save_reconcile_metadata()
        return restored, skipped, unusable

    # ------------------------------------------------------------------
    # Main entry point — called once from engine.run() before step loop.
    # ------------------------------------------------------------------

    def reconcile(self) -> bool:
        """Read the broker and apply ``startup_reconcile_mode``.

        Returns False when the attempt could not read or settle the broker:
        the account, the working orders, a broker position row's quantity
        (or, in a restore, its average price: the restore still restores
        every other row first), a tracked or saved position's working exit
        order, a tracked position's bracket that cannot be confirmed down, or
        a working order it would otherwise count as foreign. It also returns
        False while an entry order is still settling: its position is left
        to the gatekeeper, and every order is judged by the retry. A failure
        is recorded in ``result`` and, in the blocking modes, blocks entries
        (``startup_reconcile_failed``, or ``working_orders_present`` for an
        unread foreign order); the engine retries until an attempt succeeds,
        which clears the block.
        """
        # One of the five modes, checked at load (_CHOICES): a typo read as
        # log_only, with a WARNING, until 2026-09-26.
        mode = self.config.runtime.startup_reconcile_mode
        if not self.config.runtime.reconcile_on_startup or mode == "ignore":
            self._entry_block_symbols = set()
            return True
        account_read = False
        unsettled: dict[str, str] = {}
        try:
            # Entry orders an earlier cycle left unsettled are booked first,
            # from their own fill records: the account read below already
            # holds their fills, and a position the gatekeeper had yet to
            # adopt was restored a second time, or read as closed short by its
            # late fills (2026-09-25). Before the reads, so a fill that lands
            # between them is in both or in neither.
            self._settle_unsettled_entry_orders()
            unsettled = self._unsettled_entry_order_ids()
            # account_details and account_orders are independent reads; fire
            # both in parallel to halve the boot-time stall that blocks the
            # engine from entering its first scan cycle. Each returns None
            # when the broker could not be read.
            now = sessions.now_et()
            from_ts = (now - timedelta(days=self.config.runtime.startup_order_lookback_days)).astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            to_ts = now.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="bot-startup-reconcile") as pool:
                positions_future = pool.submit(self.executor.fetch_account_positions)
                orders_future = pool.submit(self.executor.fetch_working_orders, from_ts, to_ts)
                raw_positions = positions_future.result()
                raw_orders = orders_future.result()
            # An unread account is not an empty one: settling against it
            # books every tracked position closed_outside_bot and cancels its
            # broker stop, and the empty hybrid branch below prunes the
            # metadata the next restart restores from. Nothing is done.
            if raw_positions is None:
                raise RuntimeError("the broker account read failed")
            account_read = True
            working_orders = None if raw_orders is None else self._filter_reconcile_orders(raw_orders)
            # The settle needs the order list only to re-protect a bracketed
            # position, and waits for it there.
            exit_states_read = self._settle_positions_closed_at_broker(raw_positions, working_orders, set(unsettled))
            ignored_open_position_symbols = sorted(self._ignored_open_position_symbols(raw_positions))
            self._entry_block_symbols = set(ignored_open_position_symbols)
            positions = self._filter_reconcile_positions(raw_positions)
            if raw_orders is None:
                # An unread order list is not an empty one either. A
                # bracket-mode restore against it cannot see the stop still
                # resting from before the restart and submits fresh protection
                # beside it, and a restored symbol is never revisited, so both
                # stops stay (together they sell the position net short); a
                # live disaster stop is placed the same way. Without either
                # (a dry run places no disaster stop, 2026-09-29), the list
                # protects nothing: restore what the broker holds so the
                # engine manages it, and fail the attempt so the retry reads
                # the list (it skips what is already tracked), naming each
                # row it could not use too.
                failures = ["the broker working-order read failed"]
                if mode in {"restore_basic", "restore_hybrid"} and not (
                        self.executor.bracket_orders_enabled()
                        or (self.executor.disaster_stop_enabled() and not self.config.schwab.dry_run)):
                    failures += self._restore_broker_positions(positions, use_metadata=(mode == "restore_hybrid"),
                                                               working_orders=[], unsettled=set(unsettled))[2]
                raise RuntimeError("; ".join(failures))
            # False when a working order's state could not be read (below);
            # the order still counts, and the attempt is retried.
            order_states_read = True
            ignored = sorted(self._ignore_symbols())
            self.result = {
                "positions": positions,
                "working_orders": working_orders,
                "ignored_symbols": ignored,
                "ignored_open_position_symbols": ignored_open_position_symbols,
                "unsettled_entry_orders": dict(unsettled),
            }
            if positions or working_orders:
                msg = f"Startup reconciliation found {len(positions)} broker positions and {len(working_orders)} working orders"
                if ignored:
                    msg += f" after ignoring {','.join(ignored)}"
                if ignored_open_position_symbols:
                    msg += f"; blocked new entries for ignored open-position symbols {','.join(ignored_open_position_symbols)}"
                LOG.warning(msg)
                if mode == "block":
                    reasons: list[str] = []
                    if positions:
                        reasons.append("broker_positions_present")
                    if working_orders:
                        reasons.append("working_orders_present")
                    self.trading_blocked_reason = ",".join(reasons) if reasons else "startup_reconcile_blocked"
                    self.trading_blocked_message = msg
                elif mode == "log_only":
                    self.trading_blocked_reason = None
                    self.trading_blocked_message = None
                else:  # restore_basic / restore_hybrid ("ignore" returned above)
                    restored, skipped, unusable = self._restore_broker_positions(
                        positions, use_metadata=(mode == "restore_hybrid"), working_orders=working_orders,
                        unsettled=set(unsettled),
                    )
                    if restored:
                        LOG.warning("Restored %s broker position(s) using startup_reconcile_mode=%s", restored, mode)
                    if unusable:
                        # Every other row is restored and managed. The working
                        # orders are judged by the retry that restores these: a
                        # stop resting for one of them is its own, not foreign.
                        raise ValueError("; ".join(unusable))
                    # A restored position's own resting protection is not a
                    # foreign order. Counting it blocked every entry for the
                    # rest of the session in bracket mode -- and "clear them"
                    # meant stripping the position's stop. Nor is a snapshot
                    # order this reconcile retired: a child the restore
                    # resized, or the bracket the settle cancelled. The settle
                    # runs with nothing restored (a tracked symbol is
                    # skipped), so the filter no longer waits on a restore
                    # (2026-09-25). A dry run retires nothing at the broker.
                    foreign_orders = self._foreign_working_orders(working_orders)
                    if foreign_orders and not unsettled and not self.config.schwab.dry_run:
                        foreign_orders, order_states_read = self._drop_retired_orders(foreign_orders)
                    self.result["foreign_working_orders"] = foreign_orders
                    self.result["restored_positions"] = restored
                    self.result["skipped_restore_positions"] = skipped
                    if self.config.active_is_option and positions:
                        self.trading_blocked_reason = "startup_reconcile_option_restore_unsupported"
                        self.trading_blocked_message = (
                            f"Startup reconciliation found {len(positions)} broker position(s) for an option strategy, "
                            "but restore is unsupported; reconcile or close them before new entries"
                        )
                    elif foreign_orders and not unsettled:
                        # While an entry order is still settling no order is
                        # judged: that entry's own (the order, its bracket's
                        # children) belong to no tracked position yet, and
                        # "clear them" meant cancelling the stop of the
                        # position it opened. The attempt fails and blocks
                        # below; the retry judges every order.
                        self.trading_blocked_reason = "working_orders_present"
                        self.trading_blocked_message = f"Startup reconciliation restored positions but found {len(foreign_orders)} working orders they do not own; clear them before new entries"
                        if not order_states_read:
                            self.trading_blocked_message += " (some order states could not be read; retrying)"
                    else:
                        self.trading_blocked_reason = None
                        self.trading_blocked_message = None
            else:
                self.trading_blocked_reason = None
                self.trading_blocked_message = None
                if mode == "restore_hybrid":
                    try:
                        # A position the settle left tracked keeps its row.
                        removed = self.reconcile_metadata_store.delete_unmatched_positions(set(self.positions))
                        if removed:
                            LOG.info("Pruned %s stale startup reconcile metadata row(s); no live broker positions were found", removed)
                    except Exception as exc:
                        LOG.warning("Could not prune startup reconcile metadata on empty hybrid restore: %s", exc)
                    self._save_reconcile_metadata()
                if ignored_open_position_symbols:
                    LOG.warning(
                        "Startup reconciliation found no non-ignored broker positions or working orders; blocked new entries for ignored open-position symbols %s",
                        ",".join(ignored_open_position_symbols),
                    )
                else:
                    LOG.info("Startup reconciliation found no broker positions or working orders")
        except Exception as exc:
            LOG.exception("Startup reconciliation failed: %s", exc)
            if not account_read:
                # Holdings unknown: every ignore-list symbol may be held, so
                # each stays blocked until its own recheck reads the account.
                self._entry_block_symbols |= self._ignore_symbols()
            self.result = {"error": str(exc), "positions": [], "working_orders": [], "ignored_symbols": sorted(self._ignore_symbols()), "ignored_open_position_symbols": sorted(self._entry_block_symbols)}
            if mode in {"block", "restore_basic", "restore_hybrid"}:
                self.trading_blocked_reason = "startup_reconcile_failed"
                self.trading_blocked_message = f"Startup reconciliation failed: {exc}"
            return False
        if (unsettled or not exit_states_read) and mode in {"block", "restore_basic", "restore_hybrid"} \
                and not self.trading_blocked_reason:
            # Blocks like any other failed attempt; a failed attempt never
            # clears an earlier attempt's block.
            self.trading_blocked_reason = "startup_reconcile_failed"
            if unsettled:
                waiting = ", ".join(f"{order_id} ({key})" for key, order_id in sorted(unsettled.items()))
                self.trading_blocked_message = (
                    f"Startup reconciliation is waiting on entry order(s) {waiting} to settle; retrying "
                    "(a restart clears an order that never does, and the restore then adopts its fills)"
                )
            else:
                self.trading_blocked_message = (
                    "Startup reconciliation could not settle a tracked position with the broker "
                    "(an unread broker row or working exit order, or a bracket not confirmed down); retrying"
                )
        self.result["order_states_read"] = order_states_read and exit_states_read
        return order_states_read and exit_states_read and not unsettled
