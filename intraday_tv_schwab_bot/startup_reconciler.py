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
- Trading-blocked state (``trading_blocked_reason`` / ``trading_blocked_message``)
  moved off ``IntradayBot`` onto this class. Engine reads via
  ``self.startup_reconciler.trading_blocked_reason`` at step() + publish
  time, so the message never drifts from the reconciliation that produced it.
"""
from __future__ import annotations

import copy
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Callable

from schwabdev import Client

from .broker_positions import active_broker_bracket, broker_position_side_qty, extract_broker_positions, extract_working_orders
from .config import BotConfig
from .data_feed import MarketDataStore
from .models import ASSET_TYPE_EQUITY, ASSET_TYPE_OPTION_SINGLE, ASSET_TYPE_OPTION_VERTICAL, Position, Side
from .paper_account import PaperAccount
from .position_metrics import safe_float
from .position_store import ReconcileMetadataStore
from ._strategies.registry import is_option_strategy
from ._strategies.strategy_base import BaseStrategy
from .utils import UTC, call_schwab_client, now_et

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


class StartupReconciler:
    def __init__(
        self,
        config: BotConfig,
        *,
        client: Client,
        executor,
        data: MarketDataStore,
        account: PaperAccount,
        strategy: BaseStrategy,
        positions: dict[str, Position],
        reconcile_metadata_store: ReconcileMetadataStore,
        save_reconcile_metadata: Callable[[], None],
        stock_position_trail_pct: Callable[..., float | None],
    ) -> None:
        self.config = config
        self.client = client
        self.executor = executor
        self.data = data
        self.account = account
        self.strategy = strategy
        self.positions = positions
        self.reconcile_metadata_store = reconcile_metadata_store
        self._save_reconcile_metadata = save_reconcile_metadata
        self._stock_position_trail_pct = stock_position_trail_pct
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
            long_qty = int(float(row.get("longQuantity") or 0) or 0)
            short_qty = int(float(row.get("shortQuantity") or 0) or 0)
            if long_qty > 0 or short_qty > 0:
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
        try:
            account = call_schwab_client(self.client, "account_details", self.executor.account_hash, fields="positions").json()
            raw_positions = extract_broker_positions(account)
            still_blocked = symbol_upper in self._ignored_open_position_symbols(raw_positions)
        except Exception as exc:
            LOG.warning("Could not refresh startup-reconcile entry block for %s: %s", symbol_upper, exc)
            return True
        if still_blocked:
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
            if int(position.qty) != int(qty):
                continue
            if position.strategy != self.config.strategy:
                continue
            if abs(float(position.entry_price) - float(entry_price)) > tolerance:
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
        """
        metadata = position.metadata if isinstance(position.metadata, dict) else None
        if metadata is None:
            return
        stale = metadata.get("bracket") if isinstance(metadata.get("bracket"), dict) else None
        parent_order_id = stale.get("parent_order_id") if stale else None
        known = stale if stale and stale.get("stop_order_id") else self._resting_stop_for(position, working_orders)
        try:
            refreshed = self.executor.ensure_position_protected(
                str(metadata.get("underlying") or position.symbol),
                int(position.qty), position.side, float(position.entry_price),
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

    def _settle_positions_closed_at_broker(self, raw_positions: list[dict[str, Any]]) -> None:
        """Stop managing what the broker no longer holds.

        The session-boundary re-run of ``reconcile`` exists for positions
        closed while the bot could not trade -- in the Schwab app, or by a
        broker stop -- but restore only ever ADDED positions, so those stayed
        tracked: holding a ``max_positions`` slot, feeding the correlation
        guard and open risk, and sending exits the broker rejects (a
        ``status=`` failure is never rechecked, so they repeat every cycle).

        Each is booked as an exit (``closed_outside_bot``) at the last mark,
        flagged estimated and broker-recovered so reports can tell it apart,
        and dropped; one only partly closed is cut to what remains. Anything
        of its bracket still resting is cancelled -- a stop left against a
        closed position opens a new one when it triggers. Not registered with
        the risk manager: the close happened outside this session.

        Skipped in dry-run: those positions are simulated and never reach the
        broker, so the account holding none of them says nothing -- reading
        it as a close wiped every paper position held into a new session.
        """
        if not self.positions or self.config.schwab.dry_run:
            return
        held = {str(row.get("symbol") or "").upper().strip(): row for row in raw_positions}
        changed = False
        for key, position in list(self.positions.items()):
            remaining = self._broker_held_qty(position, held)
            if remaining is None:
                LOG.warning("Broker rows for %s do not read as its position; leaving it tracked", key)
                continue
            if remaining >= int(position.qty):
                continue
            closed_qty = int(position.qty) - int(remaining)
            mark = self.account.last_prices.get(position.symbol)
            exit_price = float(mark) if mark is not None and float(mark) > 0 else float(position.entry_price)
            exited = copy.copy(position)
            exited.qty = closed_qty
            self.account.record_exit(
                exited, exit_price, "closed_outside_bot",
                final_exit=remaining <= 0,
                remaining_qty_after_exit=int(remaining),
                fill_price_estimated=True,
                broker_recovered=True,
            )
            bracket = active_broker_bracket(position)
            if bracket is not None:
                leftover = self.executor.cancel_bracket(bracket)
                if not leftover.ok:
                    LOG.error("Could not confirm %s's resting bracket is down (%s) -- check the broker",
                              key, leftover.message)
            if remaining <= 0:
                self.positions.pop(key, None)
                LOG.warning("%s qty=%s is no longer held at the broker; closed outside the bot, "
                            "booked at the last mark %.4f (estimated)", key, closed_qty, exit_price)
            else:
                position.qty = int(remaining)
                if isinstance(position.metadata, dict):
                    position.metadata["qty"] = int(remaining)
                    if bracket is not None:
                        position.metadata.pop("bracket", None)
                        self._reprotect_restored_position(position, [])
                LOG.warning("%s: broker holds %s of %s; %s closed outside the bot, booked at the last "
                            "mark %.4f (estimated)", key, remaining, remaining + closed_qty, closed_qty, exit_price)
            changed = True
        if changed:
            self._save_reconcile_metadata()

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
        """Working orders no restored position owns as its resting protection."""
        owned: set[str] = set()
        for position in self.positions.values():
            bracket = position.metadata.get("bracket") if isinstance(position.metadata, dict) else None
            if not isinstance(bracket, dict) or not bracket.get("active"):
                continue
            for key in ("oco_order_id", "protective_order_id", "stop_order_id", "target_order_id"):
                if bracket.get(key):
                    owned.add(str(bracket[key]))
            owned.update(str(oid) for oid in (bracket.get("child_order_ids") or []) if oid)
        return [order for order in working_orders if str(order.get("orderId")) not in owned]

    def _restore_broker_positions(self, positions: list[dict[str, Any]], *, use_metadata: bool,
                                  working_orders: list[dict[str, Any]]) -> tuple[int, int]:
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
                    qty=qty,
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
                    entry_time=now_et(),
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
                removed = self.reconcile_metadata_store.delete_unmatched_positions(matched_metadata_keys)
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

    def reconcile(self) -> None:
        mode = str(self.config.runtime.startup_reconcile_mode or "ignore").lower()
        self._entry_block_symbols = set()
        if not self.config.runtime.reconcile_on_startup or mode == "ignore":
            return
        try:
            # account_details and account_orders are independent reads; fire
            # both in parallel to halve the boot-time stall that blocks the
            # engine from entering its first scan cycle. Result objects are
            # processed sequentially below — same behaviour as before for
            # 4xx/5xx (status_code branch) and network errors (re-raised by
            # future.result(), caught by the outer try/except).
            now = now_et()
            from_ts = (now - timedelta(days=self.config.runtime.startup_order_lookback_days)).astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            to_ts = now.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="bot-startup-reconcile") as executor:
                # Wrap each call in a lambda so executor.submit sees a
                # zero-arg callable. Passing positional/keyword args directly
                # to submit() trips PyCharm's ParamSpec inference against
                # schwabdev.Client method overloads (e.g. place_order's
                # dict-typed second arg), producing false positives. The
                # lambda also keeps from_ts / to_ts bound at definition time
                # — they're set once above and never mutated, so no late-
                # binding issue.
                account_future = executor.submit(
                    lambda: call_schwab_client(
                        self.client, "account_details", self.executor.account_hash, fields="positions"
                    )
                )
                orders_future = executor.submit(
                    lambda: call_schwab_client(
                        self.client,
                        "account_orders",
                        self.executor.account_hash,
                        fromEnteredTime=from_ts,
                        toEnteredTime=to_ts,
                    )
                )
                account = account_future.result().json()
                orders_resp = orders_future.result()
            raw_positions = extract_broker_positions(account)
            self._settle_positions_closed_at_broker(raw_positions)
            ignored_open_position_symbols = sorted(self._ignored_open_position_symbols(raw_positions))
            if ignored_open_position_symbols:
                self._entry_block_symbols = set(ignored_open_position_symbols)
            positions = self._filter_reconcile_positions(raw_positions)
            orders_lookup_failed = False
            if getattr(orders_resp, "status_code", 200) >= 400:
                orders_lookup_failed = True
                LOG.warning("Startup order lookup failed status=%s body=%s", getattr(orders_resp, "status_code", None), getattr(orders_resp, "text", ""))
                working_orders = []
            else:
                working_orders = self._filter_reconcile_orders(extract_working_orders(orders_resp.json()))
            ignored = sorted(self._ignore_symbols())
            self.result = {
                "positions": positions,
                "working_orders": working_orders,
                "ignored_symbols": ignored,
                "ignored_open_position_symbols": ignored_open_position_symbols,
                "orders_lookup_failed": bool(orders_lookup_failed),
            }
            if positions or working_orders or orders_lookup_failed:
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
                    if orders_lookup_failed:
                        reasons.append("orders_lookup_failed")
                    self.trading_blocked_reason = ",".join(reasons) if reasons else "startup_reconcile_blocked"
                    self.trading_blocked_message = msg if not orders_lookup_failed else (msg + "; working-order lookup failed")
                elif mode == "log_only":
                    self.trading_blocked_reason = None
                    self.trading_blocked_message = None
                elif mode in {"restore_basic", "restore_hybrid"}:
                    restored, skipped = self._restore_broker_positions(
                        positions, use_metadata=(mode == "restore_hybrid"), working_orders=working_orders,
                    )
                    # A restored position's own resting protection is not a
                    # foreign order. Counting it blocked every entry for the
                    # rest of the session in bracket mode -- and "clear them"
                    # meant stripping the position's stop.
                    foreign_orders = self._foreign_working_orders(working_orders)
                    self.result["foreign_working_orders"] = foreign_orders
                    self.result["restored_positions"] = restored
                    self.result["skipped_restore_positions"] = skipped
                    if restored:
                        LOG.warning("Restored %s broker position(s) using startup_reconcile_mode=%s", restored, mode)
                    if orders_lookup_failed:
                        self.trading_blocked_reason = "startup_reconcile_orders_lookup_failed"
                        self.trading_blocked_message = "Startup reconciliation could not verify working orders; clear the issue before new entries"
                    elif is_option_strategy(self.config.strategy) and positions:
                        self.trading_blocked_reason = "startup_reconcile_option_restore_unsupported"
                        self.trading_blocked_message = (
                            f"Startup reconciliation found {len(positions)} broker position(s) for an option strategy, "
                            "but restore is unsupported; reconcile or close them before new entries"
                        )
                    elif foreign_orders:
                        self.trading_blocked_reason = "working_orders_present"
                        self.trading_blocked_message = f"Startup reconciliation restored positions but found {len(foreign_orders)} working orders they do not own; clear them before new entries"
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
                        removed = self.reconcile_metadata_store.delete_unmatched_positions(set())
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
            self.result = {"error": str(exc), "positions": [], "working_orders": [], "ignored_symbols": sorted(self._ignore_symbols()), "ignored_open_position_symbols": sorted(self._entry_block_symbols)}
            if mode in {"block", "restore_basic", "restore_hybrid"}:
                self.trading_blocked_reason = "startup_reconcile_failed"
                self.trading_blocked_message = f"Startup reconciliation failed: {exc}"
