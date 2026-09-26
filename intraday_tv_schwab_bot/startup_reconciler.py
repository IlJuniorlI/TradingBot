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
- ``save_reconcile_metadata`` injected as callable because the metadata
  persistence cache (`_last_reconcile_metadata_signature`) lives on engine
  to share with entry/exit paths. StartupReconciler just triggers a save
  after mutations.
- ``stock_position_trail_pct`` injected as callable (lives on
  EntryGatekeeper). Restore uses it to compute trail_pct consistent with
  normal entry path.
- ``settle_unsettled_entry_orders`` / ``unsettled_entry_order_ids`` injected
  as callables (live on EntryGatekeeper). The account holds an unsettled
  entry order's fills before the gatekeeper books them, so the reconcile
  settles those orders first and leaves the positions of any still unsettled
  to the gatekeeper (2026-09-25). Empty at startup.
- ``book_bracket_cancel_fills`` injected as callable (lives on
  PositionManager): what a cancelled bracket's children filled is booked at
  the broker's price with the risk manager's registration, as the manager
  books it. ``risk`` receives the estimated loss of a close outside the bot.
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

from .broker_positions import (
    active_broker_bracket,
    broker_position_side_qty,
    working_exit_outstanding_qty,
)
from .config import BotConfig
from .data_feed import MarketDataStore
from .models import ASSET_TYPE_EQUITY, ASSET_TYPE_OPTION_SINGLE, ASSET_TYPE_OPTION_VERTICAL, Position, Side
from .paper_account import PaperAccount
from .numeric import safe_float
from .position_store import ReconcileMetadataStore
from .risk import RiskManager
from ._strategies.catalogue import is_option_strategy
from . import sessions
from .sessions import UTC

if TYPE_CHECKING:
    from ._strategies.strategy_base import BaseStrategy

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


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
        stock_position_trail_pct: Callable[..., float | None],
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
        self._stock_position_trail_pct = stock_position_trail_pct
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
        raw = self.config.runtime.startup_reconcile_ignore_symbols or []
        return {str(symbol).upper().strip() for symbol in raw if str(symbol).strip()}

    def _ignored_open_position_symbols(self, positions: list[dict[str, Any]]) -> set[str]:
        ignored = self._ignore_symbols()
        if not ignored:
            return set()
        blocked: set[str] = set()
        for row in positions:
            symbol = str(row.get("symbol") or "").upper().strip()
            if not symbol or symbol not in ignored:
                continue
            # A held broker row reads the way the settle reads it: a
            # malformed quantity is not held, and neither is a row that is
            # both long and short.
            _side, qty, _avg = broker_position_side_qty(row)
            if qty > 0:
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
            try:
                allowed_symbols = strategy_obj.restore_eligible_symbols()
            except Exception:
                allowed_symbols = None
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
        entry = max(0.01, float(entry_price))
        current = max(0.01, float(current_price if current_price is not None else entry))
        if side == Side.LONG:
            stop = entry * (1.0 - float(self.config.risk.default_stop_pct))
            target = entry * (1.0 + float(self.config.risk.default_target_pct))
            highest = max(entry, current)
            lowest = min(entry, current)
        else:
            stop = entry * (1.0 + float(self.config.risk.default_stop_pct))
            target = entry * (1.0 - float(self.config.risk.default_target_pct))
            highest = max(entry, current)
            lowest = min(entry, current)
        trail_pct = self._stock_position_trail_pct(metadata)
        return float(stop), float(target), float(highest), float(lowest), trail_pct

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
        dict makes RiskManager suppress the engine's stop exit for a position
        that has nothing resting at the broker -- unprotected AND unmanaged.

        ``ensure_position_protected`` adopts the children when they are still
        working and submits fresh protection when they are not; it returns None
        when bracket mode is off, in which case any stale key is dropped so the
        engine unambiguously owns the exits. The persisted bracket goes along
        as ``known_bracket``: its child ids are the ones the bot last tracked,
        so a stop replaced before the restart is adopted instead of being read
        as dead (its original, off the parent, is REPLACED) and stacked on.

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
        listed = {str(order.get("orderId")) for order in working_orders}
        known = (
            saved if saved is not None and str(saved["stop_order_id"]) in listed
            else self._resting_stop_for(position, working_orders) or saved
        )
        try:
            refreshed = self.executor.ensure_position_protected(
                str(metadata.get("underlying") or position.symbol),
                uncovered, position.side, float(position.entry_price),
                float(position.stop_price), position.target_price,
                parent_order_id=str(parent_order_id) if parent_order_id else None,
                known_bracket=known,
            )
        except Exception as exc:
            LOG.warning(
                "Could not re-establish broker protection for restored position %s: %s; "
                "dropping stale bracket so the engine owns the exits", position.symbol, exc,
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
        its rows cannot be read as this position (vertical legs out of step)."""
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        asset_type = str(meta.get("asset_type") or ASSET_TYPE_EQUITY).upper()
        if asset_type == ASSET_TYPE_OPTION_VERTICAL:
            long_side, long_qty, _ = broker_position_side_qty(held.get(str(meta.get("long_leg_symbol") or "").upper().strip()))
            short_side, short_qty, _ = broker_position_side_qty(held.get(str(meta.get("short_leg_symbol") or "").upper().strip()))
            if long_qty <= 0 and short_qty <= 0:
                return 0
            if long_side != Side.LONG or short_side != Side.SHORT or long_qty != short_qty:
                return None
            return int(long_qty)
        if asset_type == ASSET_TYPE_OPTION_SINGLE:
            symbol = str(meta.get("option_symbol") or "")
        else:
            symbol = str(meta.get("underlying") or position.symbol)
        side, qty, _ = broker_position_side_qty(held.get(symbol.upper().strip()))
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
        live."""
        outstanding = working_exit_outstanding_qty(position)
        tracked = self._working_exit_order_state(position) if outstanding > 0 else None
        if tracked is None:
            return outstanding
        record, state = tracked
        if state is None or not (state.get("is_filled") or state.get("is_terminal_failure")):
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
        could not be read or confirmed -- its working exit order's state, the
        order list its re-protect needs, or its bracket's cancel -- so the
        reconcile reports failure and the engine retries.
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
            remaining = self._broker_held_qty(position, held)
            if remaining is None:
                LOG.warning("Broker rows for %s do not read as its position; leaving it tracked", key)
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
                    fresh_remaining = None if fresh is None else self._broker_held_qty(
                        position, {str(row.get("symbol") or "").upper().strip(): row for row in fresh})
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
                cancelled = {str(oid) for oid in (bracket.get("oco_order_id"), bracket.get("protective_order_id"),
                                                  bracket.get("stop_order_id"), bracket.get("target_order_id"),
                                                  *(bracket.get("child_order_ids") or [])) if oid}
                self._reprotect_restored_position(
                    position, [order for order in working_orders or [] if str(order.get("orderId")) not in cancelled],
                )
        if changed:
            self._save_reconcile_metadata()
        return settled

    @staticmethod
    def _resting_stop_for(position: Position, working_orders: list[dict[str, Any]]) -> dict[str, Any] | None:
        """A working protective stop at the broker for *position*, as a bracket stub.

        Used when the restored metadata carries no child ids (restore_basic,
        or a position entered before bracket mode was on). Without it the
        restore submitted FRESH protection beside the stop still resting from
        before the restart; both trigger together and take the position net
        short. The first match is adopted; any other stop on the symbol stays
        a foreign order and keeps entries blocked for a human to look at.
        """
        exit_instruction = "SELL" if position.side == Side.LONG else "BUY_TO_COVER"
        symbol = str(position.symbol).upper().strip()
        for order in working_orders:
            if order.get("orderId") is None:
                continue
            if [str(s).upper().strip() for s in order.get("symbols") or []] != [symbol]:
                continue
            if order.get("orderType") not in {"STOP", "STOP_LIMIT"}:
                continue
            if list(order.get("instructions") or []) != [exit_instruction]:
                continue
            order_id = str(order["orderId"])
            return {"stop_order_id": order_id, "child_order_ids": [order_id]}
        return None

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
            for key in ("oco_order_id", "protective_order_id", "stop_order_id", "target_order_id"):
                if bracket.get(key):
                    owned.add(str(bracket[key]))
            owned.update(str(oid) for oid in (bracket.get("child_order_ids") or []) if oid)
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
                                  working_orders: list[dict[str, Any]], unsettled: set[str]) -> tuple[int, int]:
        if is_option_strategy(self.config.strategy):
            LOG.warning("startup_reconcile_mode=%s does not restore option strategies; leaving options handling unchanged", self.config.runtime.startup_reconcile_mode)
            return 0, len(positions)
        metadata_positions = self._load_reconcile_metadata() if use_metadata else {}
        matched_metadata_keys: set[str] = set()
        restored = 0
        skipped = 0
        for row in positions:
            symbol = str(row.get("symbol") or "").upper().strip()
            asset_type = str(row.get("assetType") or "").upper().strip()
            long_qty = int(float(row.get("longQuantity") or 0) or 0)
            short_qty = int(float(row.get("shortQuantity") or 0) or 0)
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
            entry_price = max(0.01, float(row.get("averagePrice") or 0.0))
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
                    quote = self.data.get_quote(symbol)
                    if quote:
                        for _qk in ("mark", "markPrice", "last", "lastPrice", "close", "closePrice"):
                            _qv = quote.get(_qk)
                            try:
                                if _qv is not None and float(_qv) > 0:
                                    current_price = float(_qv)
                                    break
                            except Exception:
                                continue
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
                trail_pct = self._stock_position_trail_pct(metadata, trail_pct)
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
        if use_metadata:
            try:
                # A position already tracked keeps its row. The loop skips it,
                # so it never matches, and the engine's save skips an unchanged
                # position set: pruning its row lost its levels, bracket and
                # working-exit ids to the next restart (the session-boundary
                # re-run and every reconcile retry reach this).
                removed = self.reconcile_metadata_store.delete_unmatched_positions(matched_metadata_keys | set(self.positions))
                if removed:
                    LOG.info("Pruned %s stale startup reconcile metadata row(s) after hybrid restore", removed)
            except Exception as exc:
                LOG.warning("Could not prune stale startup reconcile metadata after hybrid restore: %s", exc)
        if restored or use_metadata:
            self._save_reconcile_metadata()
        return restored, skipped

    # ------------------------------------------------------------------
    # Main entry point — called once from engine.run() before step loop.
    # ------------------------------------------------------------------

    def reconcile(self) -> bool:
        """Read the broker and apply ``startup_reconcile_mode``.

        Returns False when the attempt could not read or settle the broker:
        the account, the working orders, a tracked or saved position's
        working exit order, a tracked position's bracket that cannot be
        confirmed down, or a working order it would otherwise count as
        foreign. It also returns False while an entry order is still settling:
        its position is left to the gatekeeper, and every order is judged by
        the retry. A failure is recorded in ``result`` and, in the blocking
        modes, blocks entries (``startup_reconcile_failed``, or
        ``working_orders_present`` for an unread foreign order); the engine
        retries until an attempt succeeds, which clears the block.
        """
        mode = str(self.config.runtime.startup_reconcile_mode or "ignore").lower()
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
                # stops stay (together they sell the position net short).
                # Without bracket mode the list protects nothing: restore what
                # the broker holds so the engine manages it, and fail the
                # attempt so the retry reads the list (it skips what is
                # already tracked).
                if mode in {"restore_basic", "restore_hybrid"} and not self.executor.bracket_orders_enabled():
                    self._restore_broker_positions(positions, use_metadata=(mode == "restore_hybrid"),
                                                   working_orders=[], unsettled=set(unsettled))
                raise RuntimeError("the broker working-order read failed")
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
                elif mode in {"restore_basic", "restore_hybrid"}:
                    restored, skipped = self._restore_broker_positions(
                        positions, use_metadata=(mode == "restore_hybrid"), working_orders=working_orders,
                        unsettled=set(unsettled),
                    )
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
                    if restored:
                        LOG.warning("Restored %s broker position(s) using startup_reconcile_mode=%s", restored, mode)
                    if is_option_strategy(self.config.strategy) and positions:
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
                    LOG.warning("Unknown startup_reconcile_mode=%s; treating as log_only", mode)
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
                    "(an unread working exit order, or a bracket not confirmed down); retrying"
                )
        self.result["order_states_read"] = order_states_read and exit_states_read
        return order_states_read and exit_states_read and not unsettled
