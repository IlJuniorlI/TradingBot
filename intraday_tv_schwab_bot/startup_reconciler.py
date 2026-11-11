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
  to the gatekeeper (2026-09-25). The gatekeeper loads the orders the last
  run left unsettled before the reconcile runs (2026-11-08).
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
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from .broker_payloads import (
    BRACKET_REPLACED_IDS_KEY,
    DISASTER_SYNC_MODE,
    ORDER_REPLACED,
    active_broker_bracket,
    booked_child_part,
    booked_fills_ledger,
    protective_stop_reason,
    bracket_order_ids,
    broker_position_side_qty,
    broker_quantity,
    is_disaster_stop,
    listed_stop,
    note_booked_child_fills,
    order_entered_at,
    order_may_be_live,
    order_row_filled_qty,
    order_status_class,
    resting_exit_stop,
    sent_exit_stop,
    unbooked_fill_price,
    working_exit_outstanding_qty,
)
from .config import BotConfig
from .data_feed import MANAGEMENT_PRICE_KEYS, MarketDataStore
from .models import (ASSET_TYPE_EQUITY, ASSET_TYPE_OPTION_SINGLE, ASSET_TYPE_OPTION_VERTICAL, Position, Side, asset_type_of,
                     is_option_asset)
from .paper_account import PaperAccount
from .numeric import first_float, safe_float, safe_int
from .position_manager import DISASTER_STOP_RETRY_SECONDS
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
        follow_replaced_children: Callable[[], None],
        settle_unsettled_entry_orders: Callable[..., None],
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
        # ``PositionManager.follow_replaced_children``: each tracked
        # position's REPLACED protective orders followed to their
        # replacements, ahead of the settle and the foreign-order check.
        self._follow_replaced_children = follow_replaced_children
        self._settle_unsettled_entry_orders = settle_unsettled_entry_orders
        self._unsettled_entry_order_ids = unsettled_entry_order_ids
        # The error of the attempt's read of the reconcile metadata store,
        # when it failed (``metadata_read_error``): the broker's positions
        # are restored from their rows alone, nothing is pruned or saved
        # over the rows, and the attempt fails so the retry reads it again.
        self._metadata_read_error: str | None = None
        # A saved position's stop that could not be read when its close
        # while the bot was down was to be booked: the attempt fails so the
        # retry reads it (``_book_positions_closed_while_down``).
        self._close_booking_error: str | None = None
        # The saved rows this process booked as closed while the bot was
        # down: a row whose delete failed is not booked again by a retry.
        self._closes_booked: set[str] = set()
        # The symbols whose carried position no saved row matches any more,
        # each named once at CRITICAL (``_restore_broker_positions``).
        self._carried_unmatched_reported: set[str] = set()
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

    @property
    def metadata_read_error(self) -> str | None:
        """The last attempt's reconcile metadata read error (type and
        message), or None when it read. While set, the engine saves no
        metadata: a save would write the positions restored from the
        broker's rows alone over the saved rows (``IntradayBot._save_reconcile_metadata``)."""
        return self._metadata_read_error

    def _load_reconcile_metadata(self) -> dict[str, Position]:
        """The saved rows (``ReconcileMetadataStore.load_positions``). A
        store that cannot be read raises; the callers note the error
        (``_metadata_read_error``), go on with what the broker's rows give
        (a position restored basic is managed and protected rather than
        left untracked), prune and save nothing over the rows, and the
        attempt fails at its end so entries stay blocked and the retry reads
        the store again. Until 2026-11-08 the failure read as no saved rows:
        the hybrid restore rebuilt every held position with default levels
        and its save overwrote the saved rows for good (R-002)."""
        return self.reconcile_metadata_store.load_positions()

    def _note_metadata_read_error(self, exc: Exception, where: str) -> None:
        self._metadata_read_error = f"{type(exc).__name__}: {exc}"
        LOG.error("The startup reconcile metadata store could not be read for %s (%s): the broker's positions are "
                  "restored from their rows alone (default levels), nothing is pruned or saved over the rows, and "
                  "the attempt fails so entries stay blocked and the retry reads the store again",
                  where, self._metadata_read_error)

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

    def _carried_rows_left_unmatched(self, metadata_positions: dict[str, Position], symbol: str) -> list[str]:
        """The saved rows of this strategy on *symbol*, each as text, for a
        broker row the hybrid match took none of, in a live run of a
        strategy that holds across sessions
        (``BotConfig.active_holds_across_sessions``); empty otherwise.

        Such a row is a carried position the account still holds in another
        shape: its stop sold part of it while the bot was down (the
        quantity gap the match refuses), or the holding changed at the
        broker. The caller fails the attempt on it. Until 2026-11-10 the
        broker row was skipped and the saved row pruned as stale, which
        left the shares still held with nothing tracking them, and their
        stop read as a foreign order to clear.

        Left out of this rule, and so skipped and pruned as before: a row
        whose last writer was a dry run
        (``ReconcileMetadataStore.load_paper_rows``; named at WARNING), a
        paper position the account's holding only shares a symbol with; and
        every row of a strategy that is flat by the close, which this rule
        leaves as it was. A dry run is not judged: its saved rows are paper
        positions, and the broker's rows are not theirs.

        The hybrid match itself still reads such a row: the mark says who
        wrote the row last, not where the position came from, and a dry run
        that restored a real holding from a live run's row re-marks it.
        Left out of the match too, the next live start dropped that
        holding. So a paper row the account matches exactly (side, size,
        cost) is restored as this run's position; which of the two a live
        start should trust is not settled here."""
        if self.config.schwab.dry_run or not self.config.active_holds_across_sessions:
            return []
        symbol_upper = str(symbol).upper().strip()
        rows = {
            str(key): position for key, position in metadata_positions.items()
            if str(position.symbol).upper().strip() == symbol_upper and position.strategy == self.config.strategy
        }
        if not rows:
            return []
        paper = {str(row["key"]) for row in self.reconcile_metadata_store.load_paper_rows() if row["paper"]}
        if paper & set(rows):
            LOG.warning("Saved row(s) %s on %s were written by a dry run: paper positions, not this live run's. They "
                        "are left out of the restore and pruned", ", ".join(sorted(paper & set(rows))), symbol_upper)
        return [
            f"{key}: {position.side.value} {int(position.qty)} at {float(position.entry_price):.4f}, entered "
            f"{position.entry_time.isoformat()}"
            for key, position in rows.items() if key not in paper
        ]

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

    @staticmethod
    def _held_shares_by_symbol(raw_positions: list[dict[str, Any]]) -> dict[str, int]:
        """Net shares the account holds by symbol (long less short), from the
        broker's position rows, for the entry orders settling against it."""
        held: dict[str, int] = {}
        for row in raw_positions:
            symbol = str(row.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            try:
                long_qty = int(float(row.get("longQuantity") or 0))
                short_qty = int(float(row.get("shortQuantity") or 0))
            except (TypeError, ValueError):
                continue
            held[symbol] = held.get(symbol, 0) + abs(long_qty - short_qty)
        return held

    def _book_positions_closed_while_down(self, raw_positions: list[dict[str, Any]]) -> None:
        """A position the last run saved (its row in the reconcile metadata
        store) that the broker no longer holds closed while the bot was
        down: by its disaster stop, or in the app. Book it before any prune
        reads the row as stale (2026-11-08; until then the row was pruned
        unread and the close was in no trade, no report and not in the risk
        manager's realized P&L, so ``max_daily_loss`` had the loss's room
        left: the whole-bot review's R-053).

        The booking is the saved stop's fill (``bracket.stop_order_id``, the
        disaster stop or the bracket's stop) as the broker reports it
        (``executor.order_state``): its quantity and price, the reason the
        running bot books that stop's fill with (``disaster_stop`` or
        ``broker_stop``, ``broker_payloads.protective_stop_reason``),
        broker-recovered. A saved disaster stop record that is still
        ``unconfirmed`` (no id; the price and quantity it sent, and when) is
        looked up among the orders entered from its submit on, as the
        manager looks it up (``_unconfirmed_stop_fill``). What the running
        bot had booked of that order before it stopped (the record's
        ``booked_child_fills``) is left out, and the rest priced as its own
        fills (``broker_payloads.unbooked_fill_price``): the saved quantity
        is already net of those shares. The entry is
        booked for the filled shares only; a remainder the broker holds
        none of went some other way and is logged at ERROR, unbooked. The
        row is deleted once booked: a save before the first successful
        reconcile is an upsert that keeps it, and a retry or a restart
        booked the fill again (the verifier's probes); the row of a
        position this process closed itself goes at the engine's next
        metadata save (``IntradayBot._save_reconcile_metadata``), so a stop
        the manager booked is not booked here after a restart. A row whose
        delete fails is remembered for the life of the process
        (``_closes_booked``), so no retry books it a second time; the delete
        is tried again at each.

        The day the stop filled decides where the booking lands (the
        fill's time: ``broker_payloads.order_filled_at``; 2026-11-10):

        - Today: into the account (trades.csv, the day's figures), stamped
          now, and ``risk.register_realized_pnl``, gain or loss, since the
          price is an execution's.
        - An earlier day: into the account with the fill's time as the
          trade's exit time (``PaperAccount.record_exit``'s ``exit_time``),
          so it is in trades.csv under its own day and in the run's
          realized P&L, moves no figure of today's, and is not registered
          with the risk manager: ``max_daily_loss`` is today's.
        - A time that does not read (none in the payload, one after today
          or before the position's entry), for a position entered before
          today: booked stamped now, named at ERROR, and registered with
          the risk manager only when it is a loss. The day it belongs to
          is unknown; counting an unknown day's gain would widen today's
          room. A position entered today filled today whatever the payload
          says of the time: today's, as above.

        Until 2026-11-10 the position's entry day stood in for the fill's
        (a DAY stop fills the day it is entered): a position entered before
        today was reported at ERROR and never booked, so a stop that filled
        yesterday afternoon while the bot was down was in no trade, and a
        good-till-cancelled stop (a strategy that holds across sessions)
        could never have been booked at all.

        A saved stop the broker lists REPLACED (moved in the app, or by a
        replace of the bot's that returned no new id) is followed to the
        order that replaced it, and that one's in turn
        (``_saved_stop_links``, 2026-11-11): each order's unbooked fills are
        booked as their own slice, at their own price and on their own
        fill's day. Until then the saved id was read alone, a REPLACED stop
        read as one that filled nothing, and the replacement's fill, the
        close itself, was never booked. A replacement that cannot be found
        is named at ERROR: what was read is booked, the rest is not.

        A saved stop that may still rest is cancelled, with what else its
        record tracks, before anything is booked
        (``_cancel_orphan_stop``, 2026-11-11): the position closed some
        other way, and the bot's own stop must not outlive it. What it
        filled by the time it is down is booked like any other fill. Not
        the stop of a record a dry run saved (``simulated``): its ids are
        those of the real protection it stood for, which is the user's.

        A row whose stop is not saved or not found, cannot be read, or did
        not fill (closed in the app, or the save raced the exit) is logged
        at ERROR with what is known: its P&L is unknown and not in the
        tally, and the row is pruned as before (kept, and the attempt
        failed, while a stop cannot be read or its cancel is not
        confirmed).

        Not judged: a dry run (paper positions never reach the broker), a
        key still tracked (the settle's), a symbol the broker still holds
        (the restore's quantity gap), and an option row (no broker stop;
        its exits are the engine's, F-0063's family)."""
        if self.config.schwab.dry_run:
            return
        try:
            saved = self._load_reconcile_metadata()
        except Exception as exc:
            self._note_metadata_read_error(exc, "the positions closed while the bot was down")
            return
        if not saved:
            return
        held = {str(row.get("symbol") or "").upper().strip() for row in raw_positions}
        now = sessions.now_et()
        for key, position in saved.items():
            symbol = str(position.symbol or key).upper().strip()
            if key in self.positions or str(key).upper().strip() in held or symbol in held:
                continue
            if key in self._closes_booked:
                # Booked by an earlier attempt of this process whose delete
                # of the row failed: only the delete is tried again.
                self._delete_reconcile_metadata_row(key)
                continue
            metadata = position.metadata if isinstance(position.metadata, dict) else {}
            if is_option_asset(metadata):
                LOG.warning("%s: an option position saved by the last run is not held by the broker; its close "
                            "while the bot was down is not booked (option exits are the engine's)", key)
                continue
            bracket = metadata.get("bracket") if isinstance(metadata.get("bracket"), dict) else {}
            stop_id = bracket.get("stop_order_id")
            # Why no fill of the stop is known, when none is: it could not
            # be read (the row stays, the attempt fails), or it was not
            # found; "" while it was read.
            unread, absent = False, ""
            if stop_id:
                named = f"stop {stop_id}"
                state = self.executor.order_state(str(stop_id))
                unread = state is None
                if state is not None:
                    state = {**state, "order_id": str(stop_id)}
            elif is_disaster_stop(bracket) and bracket.get("state") == "unconfirmed":
                named = "the stop its unconfirmed record sent"
                state, unread, absent = self._unconfirmed_stop_fill(key, position, bracket)
            else:
                named, state = "", None
            # The saved stop and, when the broker lists it REPLACED (moved in
            # the app), the orders its shares lived on in, in turn; *lost*
            # says where that could not be followed.
            links: list[dict[str, Any]] = []
            lost = ""
            if state is not None:
                links, chain_unread, lost = self._saved_stop_links(position, bracket, state)
                unread = unread or chain_unread
            # The last of them may still rest (one the follow stopped at
            # reads REPLACED, never that): the bot's own stop, against a
            # position the account no longer holds. A dry run's record
            # (``simulated``) keeps the ids of the real protection it stood
            # for, the user's own: never cancelled here.
            if links and not bracket.get("simulated") and order_may_be_live(links[-1].get("status")) \
                    and not self._cancel_orphan_stop(key, position, bracket, links):
                continue
            # Each order's fills, less what the running bot booked of that
            # order before it stopped (the record's ledger,
            # ``booked_child_part``), priced as the rest's own fills, as the
            # manager's fill reconcile nets them: the saved quantity is
            # already net of those shares, and the order's average covers
            # both parts.
            slices: list[tuple[str, dict[str, Any], int, float, bool]] = []
            read: list[str] = []
            for link in links:
                link_name = named if link is links[0] else f"stop {link['order_id']}"
                filled = int(link.get("filled_qty") or 0)
                booked_qty, booked_notional, booked_estimated = booked_child_part(bracket, link["order_id"])
                read.append(f"{link_name} reads {link.get('status')} with {filled} filled"
                            + (f", {booked_qty} of them booked before the restart" if booked_qty else ""))
                if filled - booked_qty <= 0:
                    continue
                price, estimated = unbooked_fill_price(
                    filled, safe_float(link.get("fill_price"), None, finite=True),
                    link.get("fill_price_estimated") is not False, booked_qty, booked_notional, booked_estimated,
                    what=f"{link_name} for {key}")
                if price is not None and price > 0:
                    slices.append((link_name, link, filled - booked_qty, float(price), estimated))
            if unread:
                # A stop could not be read: the row stays for the retry, and
                # the attempt fails at its end so one runs. Nothing of it is
                # booked meanwhile: the retry books every order's fills.
                self._close_booking_error = f"{key}: {lost or f'{named} could not be read'}"
            if unread or not slices:
                LOG.error("%s: %s %d at %.4f saved by the last run is not held by the broker; its close while the bot "
                          "was down is not booked (%s). The day's loss tally does not count it: check the account",
                          key, position.side.value, int(position.qty), float(position.entry_price),
                          "no stop order saved" if not named
                          else (lost or f"{named} could not be read") if unread
                          else f"{named} {absent}" if state is None
                          else "; ".join(read + ([lost] if lost else [])))
                continue
            left = int(position.qty)
            for link_name, link, unbooked, price, estimated in slices:
                if left <= 0:
                    break
                qty = min(left, unbooked)
                self._book_close_while_down(key, position, bracket, link_name, link, qty, price, estimated, now)
                left -= qty
            self._carried_unmatched_reported.discard(symbol)
            if left > 0:
                LOG.error("%s: %d of its %d shares are not in the stop's fill and the broker holds none; how they "
                          "closed is unknown and not booked: check the account", key, left, int(position.qty))
            if lost:
                LOG.error("%s: %s; what the replacement sold is not booked, and it may still rest: check the "
                          "account", key, lost)
            self._delete_reconcile_metadata_row(key)

    def _book_close_while_down(self, key: str, position: Position, bracket: dict[str, Any], named: str,
                               state: dict[str, Any], qty: int, price: float, estimated: bool,
                               now: datetime) -> None:
        """Book *qty* shares of the saved *position* as closed by the stop
        of *state* (*named* in the log) at *price* while the bot was down,
        on the day its fill belongs to
        (``_book_positions_closed_while_down``)."""
        filled_at = self._fill_time_while_down(state, position, now)
        earlier_day = filled_at is not None and filled_at.date() < now.date()
        # No fill time that reads leaves the day unknown only for a
        # position entered before today: one entered today filled today.
        day_unknown = filled_at is None and position.entry_time.astimezone(now.tzinfo).date() < now.date()
        closed = copy.copy(position)
        closed.qty = qty
        self.account.record_restored_entry(closed, float(position.entry_price))
        realized = self.account.record_exit(
            closed, float(price), protective_stop_reason(bracket), final_exit=True, remaining_qty_after_exit=0,
            fill_price_estimated=estimated, broker_recovered=True,
            exit_time=filled_at if earlier_day else None,
        )
        self._closes_booked.add(key)
        what = f"its {named.removeprefix('the ')} filled {qty} of {int(position.qty)} at {float(price):.4f}"
        if day_unknown:
            if float(realized) < 0:
                self.risk.register_realized_pnl(float(realized))
            LOG.error("%s: closed while the bot was down: %s (realized %.2f), and the broker gives no fill time "
                      "that reads (%r): booked at the restart's time, on a day it may not belong to, and %s",
                      key, what, float(realized), state.get("filled_at"),
                      "counted in the day's loss tally" if float(realized) < 0
                      else "not counted in the day's loss tally (a gain of an unknown day)")
        elif earlier_day:
            LOG.warning("%s: closed while the bot was down: %s on %s (realized %.2f); booked at its fill time "
                        "%s, in no figure of today's and not in today's loss tally", key, what, filled_at.date(),
                        float(realized), filled_at.isoformat())
        else:
            self.risk.register_realized_pnl(float(realized))
            LOG.warning("%s: closed while the bot was down: %s (realized %.2f); booked and counted in the day's "
                        "loss tally", key, what, float(realized))

    def _saved_stop_links(self, position: Position, bracket: dict[str, Any],
                          first: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, str]:
        """The saved stop of a position the broker no longer holds (*first*:
        its state, with its ``order_id``) and, while one reads REPLACED
        (moved in the app, or by a replace of the bot's that returned no
        new id), the order that replaced it, in turn: ``(states, unread,
        lost)`` (2026-11-11). Until then a REPLACED saved stop read as one
        that filled nothing, and its replacement's fill, the position's
        actual close, was never booked.

        The replacement is the one the running bot would follow
        (``SchwabExecutor.replacement_of``: looked for among the orders
        entered since the stop itself was, so a restart on a later day
        finds it for a strategy that is flat by the close too; leaving out
        the orders the saved record tracked before,
        ``BRACKET_REPLACED_IDS_KEY``, and the ones followed here), read by
        its id. *lost* says
        where the chain could not be followed, "" when it was: *unread*
        with it when a read failed (the orders, or the replacement's own
        state; the caller keeps the row and fails the attempt), False when
        the orders were read and no one replacement fits (the caller books
        what was read and reports the rest)."""
        links = [first]
        symbol = str(position.metadata.get("underlying") or position.symbol)
        seen = {str(first["order_id"])}
        while order_status_class(links[-1].get("status")) == ORDER_REPLACED:
            last_id = str(links[-1]["order_id"])
            lookup = self.executor.replacement_of(
                last_id, links[-1], symbol, position.side,
                earlier=[*(bracket.get(BRACKET_REPLACED_IDS_KEY) or ()), *seen])
            replacement_id = lookup.order_id
            if replacement_id is None:
                return links, lookup.unread, (
                    f"stop {last_id} was REPLACED at the broker and its replacement is not found ({lookup.how})")
            if replacement_id in seen:
                return links, False, (f"stop {last_id} was REPLACED at the broker and its replacements come back "
                                      f"to stop {replacement_id}")
            state = self.executor.order_state(replacement_id)
            if state is None:
                return links, True, f"stop {replacement_id}, which replaced stop {last_id}, could not be read"
            seen.add(replacement_id)
            links.append({**state, "order_id": replacement_id})
        return links, False, ""

    def _cancel_orphan_stop(self, key: str, position: Position, bracket: dict[str, Any],
                            links: list[dict[str, Any]]) -> bool:
        """The saved stop of a position the broker no longer holds (the
        last of *links*) may still rest: the position closed some other way
        while the bot was down (sold in the app), and the bot's own stop
        outlived it. It is cancelled here, with what else the saved record
        tracks, and its final state read into *links* (2026-11-11). Until
        then the row was pruned with the stop left working: the reconcile
        read it as a foreign order and blocked entries until a human
        cancelled it, and a restart whose order lookback did not reach it
        left a sell stop resting against a flat account.

        False, with the row kept and the attempt failed
        (``_close_booking_error``), when the cancel is not confirmed or the
        stop cannot be read after it (or reads REPLACED: moved meanwhile):
        the retry cancels and reads again."""
        end_id = str(links[-1]["order_id"])
        chain = {str(link["order_id"]) for link in links}
        cancel = self.executor.cancel_bracket({
            **bracket, "stop_order_id": end_id,
            "child_order_ids": [str(oid) for oid in bracket.get("child_order_ids") or [] if str(oid) not in chain]
            + [end_id],
        })
        fresh = self.executor.order_state(end_id) if cancel.ok else None
        if fresh is None or order_status_class(fresh.get("status")) == ORDER_REPLACED:
            why = (f"its cancel was not confirmed ({cancel.message})" if not cancel.ok
                   else "it could not be read after its cancel" if fresh is None
                   else "it was REPLACED at the broker meanwhile")
            self._close_booking_error = f"{key}: stop {end_id} still rested for a position the broker no longer holds, and {why}"
            LOG.error("%s: %s %d at %.4f saved by the last run is not held by the broker, and its stop %s still "
                      "rested (%s): %s. The row is kept; the retry cancels and reads it again", key,
                      position.side.value, int(position.qty), float(position.entry_price), end_id,
                      links[-1].get("status"), why)
            return False
        LOG.warning("%s: its stop %s still rested (%s) for a position the broker no longer holds; cancelled (%s)",
                    key, end_id, links[-1].get("status"), cancel.message)
        links[-1] = {**fresh, "order_id": end_id}
        return True

    def _unconfirmed_stop_fill(self, key: str, position: Position,
                               record: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, str]:
        """What the disaster stop a saved ``unconfirmed`` record sent
        filled, for a position the broker no longer holds: ``(state,
        unread, absent)``. *state* is the stop's row among the orders
        entered from ``DISASTER_STOP_RETRY_SECONDS`` before its submit on
        (``broker_payloads.sent_exit_stop``: the exit STOP at exactly the
        price and quantity sent), in the shape ``executor.order_state``
        answers in, with its order id. It is None, *absent* saying why,
        when no such order is listed (it never landed) or the record does
        not say what to look for (no submit time, price or quantity that
        reads; logged at ERROR, a submit time with its error's type).
        *unread* is True when the orders could not be read: the caller
        keeps the row and fails the attempt. A stop that reads REPLACED
        (moved in the app) carries the replacement its row names
        (``replaced_by``) with its type and entry time, and the caller
        follows it as it follows a saved stop's (``_saved_stop_links``)."""
        try:
            since = datetime.fromisoformat(str(record.get("sent_at"))) - timedelta(seconds=DISASTER_STOP_RETRY_SECONDS)
        except (TypeError, ValueError) as exc:
            LOG.error("%s: its unconfirmed disaster stop record carries no submit time that reads (%r; %s: %s), so "
                      "the stop it sent cannot be looked up", key, record.get("sent_at"), type(exc).__name__, exc)
            return None, False, "was not looked up: its record carries no submit time that reads"
        if safe_float(record.get("stop_price"), None, finite=True) is None or safe_int(record.get("qty")) is None:
            LOG.error("%s: its unconfirmed disaster stop record carries no price or quantity that reads (%r, %r), "
                      "so the stop it sent cannot be looked up", key, record.get("stop_price"), record.get("qty"))
            return None, False, "was not looked up: its record carries no price or quantity that reads"
        orders = self.executor.todays_orders(since)
        if orders is None:
            return None, True, ""
        match = sent_exit_stop(orders, str(position.metadata.get("underlying") or position.symbol), position.side,
                               stop_price=record.get("stop_price"), qty=record.get("qty"))
        if match is None:
            return None, False, "is not among the account's orders"
        return {"order_id": str(match.get("orderId")), "status": match.get("status"),
                "filled_qty": order_row_filled_qty(match), "fill_price": match.get("fillPrice"),
                "fill_price_estimated": match.get("fillPriceEstimated"), "filled_at": match.get("filledAt"),
                "replaced_by": match.get("replacementId"), "order_type": match.get("orderType"),
                "entered_at": order_entered_at(match)}, False, ""

    @staticmethod
    def _fill_time_while_down(state: dict[str, Any], position: Position, now: datetime) -> datetime | None:
        """When the stop of *state* filled, in *now*'s zone, or None when
        the broker gives no time that reads or one that cannot be the
        fill's: after *now*'s day, or before the position was entered."""
        filled_at = state.get("filled_at")
        if not isinstance(filled_at, datetime) or filled_at.utcoffset() is None:
            return None
        filled_at = filled_at.astimezone(now.tzinfo)
        if filled_at.date() > now.date() or filled_at < position.entry_time.astimezone(now.tzinfo):
            return None
        return filled_at

    def _delete_reconcile_metadata_row(self, key: str) -> None:
        """Delete *key*'s saved row once its position is settled here, so no
        later attempt reads it again; a delete that fails is logged with its
        type (the prune on a successful attempt is the second chance)."""
        try:
            self.reconcile_metadata_store.delete_positions({str(key)})
        except Exception as exc:
            LOG.warning("%s: could not delete its startup reconcile metadata row (%s: %s)", key, type(exc).__name__, exc)

    def _held_for_settle(self) -> set[str]:
        """The keys of the positions held for a settle against the account
        (``settle_pending``)."""
        return {key for key, position in self.positions.items()
                if isinstance(position.metadata, dict) and position.metadata.get("settle_pending")}

    def settle_handed_positions(self) -> bool:
        """Settle against the broker account the positions the manager
        handed over, and no other (2026-11-11): the ones held with
        ``settle_pending``. True once none of them is held any more.

        The follow of a REPLACED stop hands a position over when nothing
        entered after the stop can still rest and those orders sold shares
        it cannot book (``PositionManager._replaced_child_gone``): only the
        account says what is held. The settle is the reconcile's own
        (``_settle_positions_closed_at_broker``): what the record still
        tracks is cancelled, what left the account is booked, the position
        is cut to what is held or dropped, and the hold is lifted; the next
        management pass places a stop for what is left.

        It runs whatever ``runtime.startup_reconcile_mode`` is, with the
        reconcile off too, and touches nothing else: no other position is
        compared with the account, nothing is restored, no order is judged
        foreign and no entry block is set or cleared. Handed to the whole
        reconcile instead, as the first build did, the position stayed
        held until a restart where the reconcile is off or in mode
        ``ignore``, and in mode ``block`` the reconcile, run mid-session,
        counted the bot's own open positions and blocked every entry for
        the rest of the day.

        The account's shares in the symbol are taken as the bot's own, as
        the reconcile takes them: shares held there for another reason hide
        what was sold, and nothing is booked.

        False, with the position still held, when the account or the
        working orders cannot be read, a cancel is not confirmed, the
        position's entry order is still settling, or anything raises
        (logged with its type): the engine tries again. A dry run hands
        nothing over and reads nothing.

        The positions settled are the ones held once the follow has run:
        that follow can hand another one over (its REPLACED stop reached
        the end of its minute), and the engine takes a True answer to mean
        that none is held. Read before the follow, as the first build of
        this did, that position was left out, the answer was still True,
        and it stayed held with no exit and no stop until a broker
        reconcile or a restart (the verifier's probe)."""
        handed = self._held_for_settle()
        if self.config.schwab.dry_run or not handed:
            return True
        try:
            raw_positions = self.executor.fetch_account_positions()
            if raw_positions is None:
                raise RuntimeError("the broker account read failed")
            now = sessions.now_et()
            from_ts, to_ts = (
                stamp.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                for stamp in (now - timedelta(days=self.config.runtime.startup_order_lookback_days), now))
            raw_orders = self.executor.fetch_working_orders(from_ts, to_ts)
            working_orders = None if raw_orders is None else self._filter_reconcile_orders(raw_orders)
            # The other leg of a bracket may read REPLACED too: followed
            # first, so the settle's cancel goes to the order that rests.
            self._follow_replaced_children()
            handed |= self._held_for_settle()
            settled = self._settle_positions_closed_at_broker(
                raw_positions, working_orders, set(self._unsettled_entry_order_ids()), only=handed)
        except Exception as exc:
            # The settle lifts a position's hold before it books and cuts
            # it: one that raised in between is held again, or the next try
            # would find nothing held and answer True for a position still
            # counted at shares the account no longer holds (the safety
            # review's probe).
            for key in handed | self._held_for_settle():
                if key in self.positions and isinstance(self.positions[key].metadata, dict):
                    self.positions[key].metadata["settle_pending"] = True
            LOG.error("The settle of %s against the broker account failed (%s: %s); held, and tried again",
                      ", ".join(sorted(self._held_for_settle())), type(exc).__name__, exc, exc_info=exc)
            return False
        still_held = sorted(self._held_for_settle())
        if still_held:
            LOG.warning("%s: not settled against the broker account yet; held, and tried again",
                        ", ".join(still_held))
        return settled and not still_held

    def _settle_positions_closed_at_broker(self, raw_positions: list[dict[str, Any]],
                                           working_orders: list[dict[str, Any]] | None,
                                           unsettled: set[str], *, only: set[str] | None = None) -> bool:
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

        With *only*, the positions of those keys are settled and no other
        is looked at (``settle_handed_positions``).
        """
        if not self.positions or self.config.schwab.dry_run:
            return True
        held = {str(row.get("symbol") or "").upper().strip(): row for row in raw_positions}
        changed = False
        settled = True
        for key, position in list(self.positions.items()):
            if only is not None and key not in only:
                continue
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
                changed = True
                # Cut the moment the close is booked (2026-11-11): whatever
                # raises from here on (the risk registration) fails the
                # attempt, and a position still at its old size would have
                # the same shares booked a second time by the retry.
                if kept <= 0:
                    self.positions.pop(key, None)
                else:
                    position.qty = kept
                    if isinstance(position.metadata, dict):
                        position.metadata["qty"] = kept
                if float(realized) < 0:
                    self.risk.register_realized_pnl(float(realized))
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
        them" meant cancelling the position's own exit (2026-09-25).

        For a live strategy that holds across sessions, the stop a tracked
        position's ``unconfirmed`` disaster record sent is its own too
        (``broker_payloads.sent_exit_stop``: the exit STOP at exactly the
        price and quantity sent; 2026-11-10). Its stop is
        good-till-cancelled: a submit whose outcome was unknown in the last
        minute of a session leaves the record unconfirmed overnight (no
        management pass looks it up before 09:30), the stop rests through
        it, and the next day's reconcile read it as foreign: entries were
        blocked for that day, and "clear them" meant cancelling the
        position's only broker stop. A DAY stop was dead by then."""
        owned: set[str] = set()
        held_live = self.config.active_holds_across_sessions and not self.config.schwab.dry_run
        for position in self.positions.values():
            record = position.metadata.get("working_exit_order") if isinstance(position.metadata, dict) else None
            if isinstance(record, dict) and record.get("order_id"):
                owned.add(str(record["order_id"]))
            bracket = position.metadata.get("bracket") if isinstance(position.metadata, dict) else None
            if not isinstance(bracket, dict):
                continue
            if held_live and is_disaster_stop(bracket) and bracket.get("state") == "unconfirmed":
                sent = sent_exit_stop(working_orders, str(position.metadata.get("underlying") or position.symbol),
                                      position.side, stop_price=bracket.get("stop_price"), qty=bracket.get("qty"))
                if sent is not None:
                    owned.add(str(sent.get("orderId")))
            # A dry run's bracket is simulated (the engine owns the exits) but
            # keeps the ids of the real protection it stands for: that order
            # is the position's own in a dry run too, and read as foreign it
            # blocked every entry of the dry run (2026-09-25).
            if not (bracket.get("active") or bracket.get("simulated")):
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

        A broker row that matches none of the saved rows a live strategy
        that holds across sessions has on its symbol is unusable too
        (``_carried_rows_left_unmatched``).

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
        metadata_positions: dict[str, Position] = {}
        if use_metadata:
            try:
                metadata_positions = self._load_reconcile_metadata()
            except Exception as exc:
                self._note_metadata_read_error(exc, "the hybrid restore")
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
                carried = self._carried_rows_left_unmatched(metadata_positions, symbol)
                if carried:
                    named = (f"the broker holds {side.value} {qty} {symbol} at {entry_price:.4f}, which matches no "
                             f"saved row of it ({'; '.join(carried)}): a stop or an exit filled in part while the bot "
                             "was down, or the holding changed at the broker. It is not restored or managed, its row "
                             "is kept, and entries wait until the account and the row agree")
                    unusable.append(named)
                    if symbol not in self._carried_unmatched_reported:
                        # Once: every retry fails on it until a human acts,
                        # and until then only its broker stop protects it.
                        self._carried_unmatched_reported.add(symbol)
                        LOG.critical("CARRIED POSITION UNMANAGED — %s. To end it: cancel its stop at the broker, "
                                     "then close the rest of it (the next attempt books what the stop sold), or "
                                     "bring the holding back to what the row says.", named)
                    continue
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
            self._carried_unmatched_reported.discard(symbol)
            try:
                # Booked at the broker's average price; the account's day
                # figures count its move from the mark the day's row valued
                # it at, or else from its first mark, never from its entry
                # again (PaperAccount.record_restored_entry).
                self.account.record_restored_entry(position, float(position.entry_price))
            except Exception as exc:
                LOG.warning("Could not materialize restored paper entry for %s: %s: %s", symbol, type(exc).__name__, exc)
            restored += 1
        if use_metadata and not unusable and self._metadata_read_error is None and self._close_booking_error is None:
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
        if (restored or use_metadata) and self._metadata_read_error is None:
            self._save_reconcile_metadata()
        return restored, skipped, unusable

    # ------------------------------------------------------------------
    # Paper positions at a dry-run restart
    # ------------------------------------------------------------------

    def _paper_restore_refusal(self, key: str, position: Position, paper: bool) -> str | None:
        """Why a saved row is not a paper position to take back, or None.
        Only a row this strategy's dry run wrote, of an equity position
        with nothing working at the broker and levels that read."""
        if position.strategy != self.config.strategy:
            return f"another strategy's row ({position.strategy})"
        if not paper:
            return "not written by a dry run"
        metadata = position.metadata if isinstance(position.metadata, dict) else {}
        if asset_type_of(metadata) != ASSET_TYPE_EQUITY:
            return f"not an equity position ({asset_type_of(metadata)})"
        if metadata.get("working_exit_order"):
            return "an exit order was working"
        bracket = metadata.get("bracket")
        if bracket is not None and not (isinstance(bracket, dict) and bracket.get("simulated") is True):
            return "its stop record is not a dry run's"
        if int(position.qty) <= 0 or not float(position.entry_price) > 0.0 or not float(position.stop_price) > 0.0:
            return (f"its quantity, entry or stop does not read (qty={position.qty}, entry={position.entry_price}, "
                    f"stop={position.stop_price})")
        if key in self.positions:
            return "its key is already tracked"
        return None

    def restore_paper_positions(self) -> int:
        """A dry run of a strategy that holds across sessions takes back the
        paper positions the earlier process saved (2026-11-10); the number
        restored. Called once at start-up, ahead of the first reconcile:
        the restore modes walk the broker account, which holds no paper
        share, and the first successful reconcile's save replaces the saved
        rows with what is tracked, so until then every position open at a
        restart left the record.

        Each accepted row is tracked as saved (its entry time, stop and
        metadata) and entered in the paper account at its entry price
        (``PaperAccount.record_restored_entry``, which also takes it back
        into the day's row when the row held it; on a later day the day's
        P&L counts its move from its first mark after the restore, so the
        gap while the bot was down is in no day's figure, though it is in
        the trade's and the run's). Nothing was simulated
        while the bot was down: a stop crossed meanwhile exits at the first
        pass's price, and the log names each row's last save. A row that is
        not such a position (``_paper_restore_refusal``) is refused at
        ERROR and left to that save to drop. An unreadable store raises:
        the start-up fails rather than start empty and wipe the rows. Any
        other run (live, or a strategy flat by the close) restores nothing
        here."""
        if not (self.config.schwab.dry_run and self.config.active_holds_across_sessions):
            return 0
        restored = 0
        for row in self.reconcile_metadata_store.load_paper_rows():
            key, position = str(row["key"]), row["position"]
            refusal = self._paper_restore_refusal(key, position, bool(row["paper"]))
            if refusal is not None:
                LOG.error("Paper restore: the saved row %s (%s %s %s) is not restored: %s. It leaves the store at "
                          "the next save.", key, position.symbol, position.side.value, position.qty, refusal)
                continue
            metadata = dict(position.metadata or {})
            metadata.update({"restored_on_startup": True, "restored_mode": "paper", "restored_from_metadata": True})
            # The price its disaster stop would rest at, as the hybrid
            # restore stamps it: a row saved before the entry stamped one
            # gets it here, one that carries it keeps its own.
            initial_stop = safe_float(metadata.get("initial_stop_price"), None, finite=True)
            self.executor.stamp_disaster_stop_price(
                metadata, position.side, float(position.entry_price),
                float(position.stop_price) if initial_stop is None else initial_stop)
            position.metadata = metadata
            self.positions[key] = position
            self.account.record_restored_entry(position, float(position.entry_price))
            restored += 1
            LOG.warning("Paper restore: %s %s %d @ %.4f, entered %s, stop %.4f, restored from its row (last saved "
                        "%s). Nothing was simulated since: a stop crossed meanwhile exits at the first pass's price.",
                        position.symbol, position.side.value, int(position.qty), float(position.entry_price),
                        position.entry_time.isoformat(timespec="seconds"), float(position.stop_price),
                        row["updated_at"] or "unknown")
        return restored

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
        self._metadata_read_error = None
        self._close_booking_error = None
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
            # The entry orders the last run left unsettled settle against
            # what the account holds (2026-11-08): their fills are adopted
            # capped at the account's shares, so a stop that sold part while
            # the bot was down is not tracked as held.
            self._settle_unsettled_entry_orders(held=self._held_shares_by_symbol(raw_positions))
            unsettled = self._unsettled_entry_order_ids()
            # A saved position the account no longer holds closed while the
            # bot was down: booked from its stop's fill before any prune
            # reads its row as stale.
            self._book_positions_closed_while_down(raw_positions)
            working_orders = None if raw_orders is None else self._filter_reconcile_orders(raw_orders)
            # A tracked position's protective order the broker lists
            # REPLACED (its stop moved in the app since the last management
            # pass) is followed to its replacement first: the settle's
            # cancel then goes to the order that rests, and the replacement
            # is the position's own below, not a foreign order (2026-11-11).
            self._follow_replaced_children()
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
                if mode == "restore_hybrid" and self._metadata_read_error is None and self._close_booking_error is None:
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
            if self._metadata_read_error is not None:
                # After the restore, so what the broker holds is tracked and
                # managed; the attempt still fails (entries blocked, retried)
                # until the store reads.
                raise RuntimeError(f"the startup reconcile metadata store could not be read "
                                   f"({self._metadata_read_error}); the broker's positions were restored from "
                                   f"their rows alone")
            if self._close_booking_error is not None:
                raise RuntimeError(f"a position closed while the bot was down could not be booked "
                                   f"({self._close_booking_error}); retried")
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
                    "(the record survives a restart; an order that never settles keeps its symbol closed to "
                    "entries until it is cancelled at the broker, and its fills are adopted as they show)"
                )
            else:
                self.trading_blocked_message = (
                    "Startup reconciliation could not settle a tracked position with the broker "
                    "(an unread broker row or working exit order, or a bracket not confirmed down); retrying"
                )
        self.result["order_states_read"] = order_states_read and exit_states_read
        return order_states_read and exit_states_read and not unsettled
