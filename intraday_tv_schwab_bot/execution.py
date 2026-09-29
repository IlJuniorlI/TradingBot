# SPDX-License-Identifier: MIT
from __future__ import annotations

import copy
import datetime
import logging
import math
from dataclasses import dataclass, replace
import time as time_module
from typing import Any, Callable

from schwabdev import Client

from .broker_payloads import (
    BRACKET_ID_KEYS,
    DISASTER_SYNC_MODE,
    STOP_ORDER_TYPES,
    bracket_wrapper_and_children,
    collect_protective_fills,
    extract_bracket_children,
    extract_broker_positions,
    extract_orders,
    flatten_order_tree,
    is_disaster_stop,
    order_fill_price,
    order_filled_qty,
    order_is_filled,
    order_is_terminal_failure,
    order_may_be_live,
    order_replacement_id,
    order_status,
    protective_stop_reason,
    resting_exit_stop,
)
from .config import BotConfig
from .data_feed import EXECUTION_LAST_KEYS
from .log_setup import TRADEFLOW_LEVEL
from .models import (
    ASSET_TYPE_EQUITY,
    ASSET_TYPE_OPTION_SINGLE,
    ASSET_TYPE_OPTION_VERTICAL,
    OrderIntent,
    OrderResult,
    Position,
    Side,
    asset_type_of,
    is_option_asset,
)
from .numeric import first_float, safe_float, safe_int
from .position_metrics import DISASTER_STOP_PRICE_KEY
from .options_mode import build_single_option_close_order, build_vertical_close_order, close_limit_price_from_metadata, close_single_option_limit_from_metadata, contract_from_quote, single_option_price_bounds, vertical_price_bounds
from . import sessions
from .sessions import UTC, classify_equity_session, equity_session_state, is_regular_equity_session
from .schwab_api import SCHWAB_WRITE_UNKNOWN_OUTCOME, call_schwab_client, call_schwab_json, response_ok

LOG = logging.getLogger(__name__)

# A live order that did not fill in its poll window and whose cancel the
# broker confirmed CANCELED, with nothing filled: nothing of it can still
# fill, so it is the one outcome another order for the same shares may
# follow in the same call (the exit re-send).
LIVE_UNFILLED_CANCELED = "live_unfilled_canceled"
# The same order found REJECTED or EXPIRED instead, after the broker had
# accepted it: nothing of it can fill either, but the broker refused or
# ended it, so no order follows it in the same call (2026-09-29). Until then
# every terminal status read as LIVE_UNFILLED_CANCELED, and the exit re-send
# followed a rejection too.
LIVE_UNFILLED_REJECTED = "live_unfilled_rejected"
LIVE_UNFILLED_EXPIRED = "live_unfilled_expired"
# The order was REPLACED at the broker (changed in the app): its shares live
# on in the replacement, which the result's ``order_id`` names when the
# broker's payload does (``may_still_be_working``: the caller tracks it).
# The message is ``live_order_replaced:<original>-><replacement>``, with
# ``REPLACEMENT_UNNAMED`` for a payload that names none: then ``order_id``
# is the original's, whose state reads REPLACED, and the caller looks the
# replacement up from there.
LIVE_ORDER_REPLACED = "live_order_replaced"
REPLACEMENT_UNNAMED = "unknown"
_LIVE_UNFILLED_BY_STATUS = {
    "CANCELED": LIVE_UNFILLED_CANCELED,
    "CANCELLED": LIVE_UNFILLED_CANCELED,
    "REJECTED": LIVE_UNFILLED_REJECTED,
    "EXPIRED": LIVE_UNFILLED_EXPIRED,
}


def order_result_left_nothing_live(message: Any) -> bool:
    """True when a result's last order is confirmed dead at the broker with
    nothing filled (CANCELED, REJECTED or EXPIRED): nothing of it rests or
    can fill, so the position's broker stop can go back at once. Read off
    the message's prefix, before any ``;`` suffix."""
    prefix = str(message or "").split(";", 1)[0]
    return prefix in (LIVE_UNFILLED_CANCELED, LIVE_UNFILLED_REJECTED, LIVE_UNFILLED_EXPIRED)


def order_result_names_replacement(message: Any) -> bool:
    """True when a result's last order was REPLACED at the broker and its
    ``order_id`` is the replacement the payload named: an order that has
    filled nothing of its own yet. False for any other result, one whose
    payload named no replacement included (its ``order_id`` is the
    original's, with the original's fills). Read off the message's prefix,
    before any ``;`` suffix."""
    prefix = str(message or "").split(";", 1)[0]
    return prefix.startswith(f"{LIVE_ORDER_REPLACED}:") and not prefix.endswith(f"->{REPLACEMENT_UNNAMED}")

# The caller's rule for the levels an entry fill leaves the trade with: the
# fill price in, ``(stop, target, fallback_reason)`` out, the reason None when
# the signal's levels still fit the fill (``EntryGatekeeper._post_fill_levels``).
PostFillLevels = Callable[[float], tuple[float, float | None, str | None]]


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    qty: int
    intent: OrderIntent
    order_type: str = "MARKET"
    price: float | None = None
    session: str = "NORMAL"
    duration: str = "DAY"


@dataclass(slots=True)
class BracketCancel:
    """Outcome of tearing down a position's resting broker protection."""
    ok: bool
    message: str
    # Shares the resting children filled before the cancel landed, their
    # average price, and the exit they were ("broker_stop" / "broker_target").
    # The caller books these before exiting what is left.
    filled_qty: int = 0
    fill_price: float | None = None
    fill_reason: str | None = None


class SchwabExecutor:
    @staticmethod
    def _is_regular_options_session(ts=None) -> bool:
        return is_regular_equity_session(ts)


    def __init__(self, client: Client, config: BotConfig):
        self.client = client
        self.config = config
        self.account_hash = config.schwab.account_hash or self._resolve_account_hash()

    def _resolve_account_hash(self) -> str:
        payload = call_schwab_json(self.client, "linked_accounts")
        if isinstance(payload, list) and payload:
            for row in payload:
                for key in ("hashValue", "accountHash", "encryptedAccountNumber"):
                    if row.get(key):
                        return str(row[key])
        raise RuntimeError("Could not resolve account hash from linked_accounts()")

    @staticmethod
    def order_intent_for_entry(side: Side) -> OrderIntent:
        return OrderIntent.BUY if side == Side.LONG else OrderIntent.SELL_SHORT

    @staticmethod
    def order_intent_for_exit(side: Side) -> OrderIntent:
        return OrderIntent.SELL if side == Side.LONG else OrderIntent.BUY_TO_COVER

    @staticmethod
    def _option_quote_force_cooldown_seconds() -> float:
        return 1.0

    @staticmethod
    def _response_order_id(response) -> str | None:
        location = getattr(response, "headers", {}).get("Location", "") or ""
        order_id = str(location).split("/")[-1].strip()
        return order_id or None

    def _equity_session(self, ts=None) -> str | None:
        return classify_equity_session(
            ts,
            extended_hours_enabled=bool(self.config.execution.extended_hours_enabled),
        )

    def regular_session_open(self, ts=None) -> bool:
        """True in the regular equity session, the only one a STOP rests in
        (Schwab rejects one outside it)."""
        return self._equity_session(ts) == "NORMAL"

    def _equity_session_blackout_reason(self, ts=None) -> str:
        state = equity_session_state(
            ts,
            extended_hours_enabled=bool(self.config.execution.extended_hours_enabled),
        )
        return state.order_blackout_reason or "equity_session_closed"

    def _equity_market(self, symbol: str, data, refresh_quotes: bool = True) -> tuple[float | None, float | None, float | None] | None:
        if data is None or not symbol:
            return None
        if refresh_quotes:
            data.fetch_quotes([symbol], force=True, source="execution:equity_market")
        quote = data.get_quote(symbol)
        if not quote:
            return None
        max_age = max(1.0, float(self.config.runtime.quote_cache_seconds))
        if not data.quotes_are_fresh([symbol], max_age):
            return None
        bid = first_float(quote, "bid", positive=True)
        ask = first_float(quote, "ask", positive=True)
        last = first_float(quote, *EXECUTION_LAST_KEYS, positive=True)
        return bid, ask, last

    @staticmethod
    def _market_snapshot_from_tuple(market: tuple[float | None, float | None, float | None] | None) -> dict[str, float | None] | None:
        if market is None:
            return None
        bid, ask, last = market
        return {"bid": bid, "ask": ask, "last": last}

    def _coerce_equity_market(self, market_snapshot: Any) -> tuple[float | None, float | None, float | None] | None:
        if market_snapshot is None:
            return None
        if isinstance(market_snapshot, tuple) and len(market_snapshot) == 3:
            bid = first_float({"v": market_snapshot[0]}, "v", positive=True)
            ask = first_float({"v": market_snapshot[1]}, "v", positive=True)
            last = first_float({"v": market_snapshot[2]}, "v", positive=True)
            return bid, ask, last
        if isinstance(market_snapshot, dict):
            bid = first_float(market_snapshot, "bid", positive=True)
            ask = first_float(market_snapshot, "ask", positive=True)
            last = first_float(market_snapshot, *EXECUTION_LAST_KEYS, "mid", positive=True)
            return bid, ask, last
        return None

    def _marketable_limit_buffer(self, bid: float | None, ask: float | None) -> float:
        cfg = self.config.execution
        spread = 0.0
        if bid is not None and ask is not None and ask >= bid:
            spread = max(0.0, ask - bid)
        raw = max(0.01, float(cfg.entry_limit_min_buffer), spread * float(cfg.entry_limit_spread_frac))
        capped = min(float(cfg.entry_limit_max_buffer), raw)
        return round(max(0.01, capped), 4)

    def _equity_limit_price(self, intent: OrderIntent, bid: float | None, ask: float | None, last: float | None, *, buffer_mult: float = 1.0) -> float | None:
        """Marketable limit: the touch plus a spread-scaled buffer, on a valid tick.

        The buffer is a fraction of the spread and the reprice loop scales it
        by ``1 + attempt * step``, so the raw sum is routinely sub-penny
        (150.1599). Schwab rejects a sub-penny limit on a stock at/above $1
        (SEC Rule 612) -- dry-run has no tick check, so this only ever bit
        live. Rounding runs AWAY from the touch (a buy up, a sell down) so the
        order stays at least as marketable as the unrounded price.
        """
        buffer = self._marketable_limit_buffer(bid, ask) * max(1.0, float(buffer_mult))
        buy_side = intent in {OrderIntent.BUY, OrderIntent.BUY_TO_COVER}
        if buy_side:
            reference = ask if ask is not None else last
            if reference is None or not math.isfinite(reference) or reference <= 0:
                return None
            raw = reference + buffer
            if not math.isfinite(raw):
                return None
            return self._round_equity_price(raw, "up")
        reference = bid if bid is not None else last
        if reference is None or not math.isfinite(reference) or reference <= 0:
            return None
        return self._round_equity_price(max(0.01, reference - buffer), "down")

    def _simulate_equity_fill(self, request: OrderRequest, data, refresh_quotes: bool = False, market_snapshot: Any | None = None) -> OrderResult:
        market = self._coerce_equity_market(market_snapshot)
        if market is None:
            market = self._equity_market(request.symbol, data, refresh_quotes=refresh_quotes)
        if market is None:
            return OrderResult(ok=False, order_id=None, raw=self._build_order(request), message="dry_run_missing_or_stale_quotes", simulated=True)
        bid, ask, last = market
        spec = self._build_order(request)
        if request.order_type == "MARKET":
            if request.intent in {OrderIntent.BUY, OrderIntent.BUY_TO_COVER}:
                fill_price = ask if ask is not None else last
            else:
                fill_price = bid if bid is not None else last
            if fill_price is None or fill_price <= 0:
                return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_missing_fill_price", simulated=True)
            return OrderResult(ok=True, order_id=None, raw=spec, message="dry_run_fill_market", fill_price=float(fill_price), filled_qty=request.qty, simulated=True)
        limit = float(request.price or 0.0)
        if limit <= 0:
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_missing_limit_price", simulated=True)
        if request.intent in {OrderIntent.BUY, OrderIntent.BUY_TO_COVER}:
            natural = ask if ask is not None else last
            if natural is None or natural <= 0:
                return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_missing_fill_price", simulated=True)
            if limit + 1e-9 < natural:
                return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_not_filled_limit", simulated=True)
            return OrderResult(ok=True, order_id=None, raw=spec, message="dry_run_fill_marketable_limit", fill_price=float(natural), filled_qty=request.qty, simulated=True)
        natural = bid if bid is not None else last
        if natural is None or natural <= 0:
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_missing_fill_price", simulated=True)
        if limit - 1e-9 > natural:
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_not_filled_limit", simulated=True)
        return OrderResult(ok=True, order_id=None, raw=spec, message="dry_run_fill_marketable_limit", fill_price=float(natural), filled_qty=request.qty, simulated=True)

    def _submit_live_order_spec(self, spec: dict[str, Any]):
        return call_schwab_client(self.client, "place_order", self.account_hash, spec)

    def _equity_order_details(self, order_id: str) -> tuple[dict[str, Any] | None, str]:
        try:
            response = call_schwab_client(self.client, "order_details", self.account_hash, order_id)
        except Exception as exc:
            return None, f"order_details_error:{exc}"
        if not response_ok(response):
            return None, f"order_details_status={getattr(response, 'status_code', None)}"
        try:
            payload = response.json()
        except Exception as exc:
            return None, f"order_details_json_error:{exc}"
        return payload, order_status(payload)

    def _poll_equity_order(self, order_id: str, timeout_seconds: float, poll_seconds: float) -> tuple[dict[str, Any] | None, str]:
        deadline = time_module.monotonic() + max(0.0, timeout_seconds)
        while True:
            payload, status = self._equity_order_details(order_id)
            if payload is None:
                return payload, status
            if order_is_filled(payload) or order_is_terminal_failure(payload):
                return payload, status
            if time_module.monotonic() >= deadline:
                return payload, status
            time_module.sleep(max(0.05, poll_seconds))

    def _cancel_live_equity_order(self, order_id: str) -> tuple[bool, str, dict[str, Any] | None]:
        """Cancel ``order_id``; True only once the broker shows it TERMINAL.

        A partial fill is not terminal. This used to return True for any order
        with fills, so a partially filled order whose cancel had not landed read
        as cancelled while its remainder was still live -- and an exit's
        remainder filling after the engine sent a fresh exit for the same
        shares takes the position net short.
        """
        timeout_seconds = max(0.5, min(5.0, float(self.config.execution.entry_live_fill_timeout_seconds)))
        poll_seconds = max(0.1, float(self.config.execution.entry_live_poll_seconds))

        def _post_cancel_check(prefix: str) -> tuple[bool, str, dict[str, Any] | None]:
            payload, status = self._poll_equity_order(order_id, timeout_seconds, poll_seconds)
            if payload is None:
                return False, f"{prefix}:{status}", payload
            if order_is_terminal_failure(payload):
                return True, f"{prefix}:{status}", payload
            if order_is_filled(payload):
                return True, f"{prefix}:{status}", payload
            return False, f"{prefix}_unconfirmed:{status}", payload

        try:
            response = call_schwab_client(self.client, "cancel_order", self.account_hash, order_id)
        except Exception as exc:
            ok, msg, payload = _post_cancel_check("cancel_postcheck")
            if ok:
                return ok, msg, payload
            return False, f"cancel_error:{exc}", payload
        status_code = getattr(response, 'status_code', None)
        if response_ok(response):
            return _post_cancel_check(f"cancel_status={status_code}")
        ok, msg, payload = _post_cancel_check("cancel_postcheck")
        if ok:
            return ok, msg, payload
        return False, f"cancel_status={status_code}", payload

    def cancel_working_order(self, order_id: str) -> tuple[bool, str]:
        """Cancel an order a submit path left live; True once it is terminal."""
        if self.config.schwab.dry_run:
            return True, "dry_run_cancel"
        ok, msg, _payload = self._cancel_live_equity_order(str(order_id))
        return ok, msg

    def _build_repriced_equity_request(self, request: OrderRequest, data, buffer_mult: float) -> OrderRequest | None:
        """``request`` at a limit priced off a fresh quote, its spread buffer
        ``buffer_mult`` times the first's; None without a fresh, usable quote."""
        market = self._equity_market(request.symbol, data, refresh_quotes=True)
        if market is None:
            return None
        bid, ask, last = market
        new_price = self._equity_limit_price(request.intent, bid, ask, last, buffer_mult=buffer_mult)
        if new_price is None:
            return None
        return OrderRequest(
            symbol=request.symbol,
            qty=request.qty,
            intent=request.intent,
            order_type=request.order_type,
            price=new_price,
            session=request.session,
            duration=request.duration,
        )

    def _finalize_live_equity_entry_result(self, request: OrderRequest, spec: dict[str, Any], payload: dict[str, Any] | None, order_id: str | None, message: str, data=None) -> OrderResult:
        filled_qty = order_filled_qty(payload)
        broker_fill_price = order_fill_price(payload)
        fill_price = broker_fill_price
        if fill_price is None and data is not None:
            # Use live quotes as estimated fill price for metadata/logging.
            # This is NOT evidence of a fill — only the broker-reported price is.
            market = self._equity_market(request.symbol, data, refresh_quotes=True)
            if market is not None:
                bid, ask, last = market
                if request.intent in {OrderIntent.BUY, OrderIntent.BUY_TO_COVER}:
                    fill_price = ask if ask is not None else last
                else:
                    fill_price = bid if bid is not None else last
        if filled_qty is None and broker_fill_price is not None:
            # Only assume full fill when the BROKER itself reported a fill price.
            # A quote-derived fallback price does not prove the order was filled.
            filled_qty = request.qty
        return OrderResult(ok=(filled_qty or 0) > 0, order_id=order_id, raw=payload or spec, message=message, fill_price=fill_price, filled_qty=filled_qty, simulated=False)

    def _finalize_live_polled_order_result(
        self,
        spec: dict[str, Any],
        payload: dict[str, Any] | None,
        order_id: str | None,
        message: str,
        *,
        price_scale: float = 1.0,
    ) -> OrderResult:
        filled_qty = order_filled_qty(payload)
        fill_price = order_fill_price(payload)
        if fill_price is not None and price_scale != 1.0:
            fill_price *= float(price_scale)
        return OrderResult(ok=(filled_qty or 0) > 0, order_id=order_id, raw=payload or spec, message=message, fill_price=fill_price, filled_qty=filled_qty, simulated=False)

    def _replaced_order_result(self, spec: dict[str, Any], payload: dict[str, Any] | None, order_id: str,
                               *, price_scale: float) -> OrderResult | None:
        """The result of an order found REPLACED at the broker, or None when
        *payload* is not REPLACED.

        A REPLACED order is one someone changed at the broker (in the app):
        its shares live on in the replacement, so the result names that order
        (``order_replacement_id``) and ``may_still_be_working``, and the
        caller tracks it rather than send another order for the same shares.
        A payload that does not name its replacement logs ``ORDER REPLACED
        UNTRACKED`` at CRITICAL and names the original
        (``REPLACEMENT_UNNAMED``): the replacement may still work, and the
        caller looks it up among the day's orders from the original
        (``PositionManager._exit_order_in_flight``). Fills the replaced order
        made before the replace are in the result, to book."""
        if order_status(payload) != "REPLACED":
            return None
        replacement = order_replacement_id(payload)
        leg = (spec.get("orderLegCollection") or [{}])[0]
        what = f"{leg.get('instruction')} {(leg.get('instrument') or {}).get('symbol')} qty={leg.get('quantity')}"
        if replacement:
            LOG.error("Order %s (%s) was REPLACED at the broker by order %s, which is tracked in its place; no "
                      "other order follows it", order_id, what, replacement)
        else:
            LOG.critical("ORDER REPLACED UNTRACKED — order %s (%s) was REPLACED at the broker and its payload does "
                         "not name the replacement, which may still work untracked; it is looked up among the "
                         "day's orders. Check the account.", order_id, what)
        result = self._finalize_live_polled_order_result(
            spec, payload, replacement or order_id,
            f"{LIVE_ORDER_REPLACED}:{order_id}->{replacement or REPLACEMENT_UNNAMED}", price_scale=price_scale,
        )
        result.may_still_be_working = True
        return result

    def _submit_live_single_order_with_poll(
        self,
        spec: dict[str, Any],
        *,
        cancel_on_timeout: bool,
        price_scale: float = 1.0,
        on_order_sent: Callable[[str], None] | None,
    ) -> OrderResult:
        """Send one order, poll it, and cancel what is left of it unfilled
        (``cancel_on_timeout``; otherwise it is left working).

        ``on_order_sent`` is called with the new order's id before it is
        polled, so the caller can record the order as working first: a stop
        signal during the poll (it is not an Exception) leaves it recorded,
        and a restart settles it (2026-09-29). An exit passes it; an entry
        passes None.

        An order the broker ended without filling is labelled by its status:
        ``LIVE_UNFILLED_CANCELED`` (the cancel landed), ``LIVE_UNFILLED_
        REJECTED`` or ``LIVE_UNFILLED_EXPIRED`` (the broker ended it after
        accepting it, logged at ERROR), and a REPLACED one is
        ``_replaced_order_result``'s."""
        timeout_seconds = max(0.5, float(self.config.execution.entry_live_fill_timeout_seconds))
        poll_seconds = max(0.1, float(self.config.execution.entry_live_poll_seconds))
        response = self._submit_live_order_spec(spec)
        if not response_ok(response):
            return OrderResult(ok=False, order_id=None, raw=getattr(response, 'text', spec), message=f"status={getattr(response, 'status_code', None)}", simulated=False)
        order_id = self._response_order_id(response)
        if not order_id:
            return OrderResult(ok=False, order_id=None, raw=getattr(response, 'text', spec), message="live_missing_order_id", simulated=False)
        if on_order_sent is not None:
            on_order_sent(order_id)
        payload, status = self._poll_equity_order(order_id, timeout_seconds, poll_seconds)
        replaced = self._replaced_order_result(spec, payload, order_id, price_scale=price_scale)
        if replaced is not None:
            return replaced
        if payload is not None and order_is_filled(payload):
            return self._finalize_live_polled_order_result(spec, payload, order_id, f"live_fill:{status}", price_scale=price_scale)
        filled_qty = order_filled_qty(payload) or 0
        if filled_qty > 0:
            cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
            latest_payload = cancel_payload or payload
            replaced = self._replaced_order_result(spec, latest_payload, order_id, price_scale=price_scale)
            if replaced is not None:
                return replaced
            result = self._finalize_live_polled_order_result(spec, latest_payload, order_id, f"live_partial_fill:{cancel_msg}", price_scale=price_scale)
            if not result.ok:
                result = OrderResult(ok=False, order_id=order_id, raw=latest_payload or spec, message=f"partial_fill_finalize_failed:{cancel_msg}", simulated=False)
            result.may_still_be_working = not cancel_ok
            return result
        if not cancel_on_timeout:
            return OrderResult(ok=False, order_id=order_id, raw=payload or spec, message=f"live_unfilled_timeout:{status}", simulated=False,
                               may_still_be_working=True)
        cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
        latest_payload = cancel_payload or payload
        replaced = self._replaced_order_result(spec, latest_payload, order_id, price_scale=price_scale)
        if replaced is not None:
            return replaced
        if latest_payload is not None and order_is_filled(latest_payload):
            return self._finalize_live_polled_order_result(spec, latest_payload, order_id, f"live_fill_after_cancel:{cancel_msg}", price_scale=price_scale)
        latest_filled_qty = order_filled_qty(latest_payload) or 0
        if latest_filled_qty > 0:
            result = self._finalize_live_polled_order_result(spec, latest_payload, order_id, f"live_partial_fill_after_cancel:{cancel_msg}", price_scale=price_scale)
            if not result.ok:
                result = OrderResult(ok=False, order_id=order_id, raw=latest_payload or spec, message=f"partial_fill_after_cancel_finalize_failed:{cancel_msg}", simulated=False)
            result.may_still_be_working = not cancel_ok
            return result
        if not cancel_ok:
            return OrderResult(ok=False, order_id=order_id, raw=latest_payload or spec, message=f"live_unfilled_cancel_failed:{cancel_msg}", simulated=False,
                               may_still_be_working=True)
        dead_status = order_status(latest_payload)
        message = _LIVE_UNFILLED_BY_STATUS[dead_status]
        if message != LIVE_UNFILLED_CANCELED:
            leg = (spec.get("orderLegCollection") or [{}])[0]
            LOG.error("Order %s (%s %s qty=%s) was %s at the broker after it was accepted; nothing of it filled",
                      order_id, leg.get("instruction"), (leg.get("instrument") or {}).get("symbol"),
                      leg.get("quantity"), dead_status)
        return OrderResult(ok=False, order_id=order_id, raw=latest_payload or spec, message=message, simulated=False)

    def _submit_live_equity_entry_with_reprice(self, initial_request: OrderRequest, data=None) -> OrderResult:
        timeout_seconds = max(0.5, float(self.config.execution.entry_live_fill_timeout_seconds))
        poll_seconds = max(0.1, float(self.config.execution.entry_live_poll_seconds))
        reprice_attempts = max(0, int(self.config.execution.entry_live_reprice_attempts))
        current_request = initial_request
        current_spec = self._build_order(current_request)
        for attempt in range(reprice_attempts + 1):
            response = self._submit_live_order_spec(current_spec)
            if not response_ok(response):
                return OrderResult(ok=False, order_id=None, raw=getattr(response, 'text', current_spec), message=f"status={getattr(response, 'status_code', None)}", simulated=False)
            order_id = self._response_order_id(response)
            if not order_id:
                return OrderResult(ok=False, order_id=None, raw=getattr(response, 'text', current_spec), message="live_missing_order_id", simulated=False)
            payload, _ = self._poll_equity_order(order_id, timeout_seconds, poll_seconds)
            if order_is_filled(payload):
                return self._finalize_live_equity_entry_result(current_request, current_spec, payload, order_id, f"live_fill_attempt_{attempt}", data=data)
            filled_qty = order_filled_qty(payload) or 0
            if filled_qty > 0:
                cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
                latest_payload = cancel_payload or payload
                result = self._finalize_live_equity_entry_result(current_request, current_spec, latest_payload, order_id, f"live_partial_fill_attempt_{attempt}:{cancel_msg}", data=data)
                if not result.ok:
                    result = OrderResult(ok=False, order_id=order_id, raw=latest_payload or current_spec, message=f"partial_fill_finalize_failed:{cancel_msg}", simulated=False)
                result.may_still_be_working = not cancel_ok
                return result
            if attempt >= reprice_attempts:
                cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
                latest_payload = cancel_payload or payload
                if latest_payload is not None and order_is_filled(latest_payload):
                    return self._finalize_live_equity_entry_result(current_request, current_spec, latest_payload, order_id, f"live_fill_after_cancel_attempt_{attempt}:{cancel_msg}", data=data)
                latest_filled_qty = order_filled_qty(latest_payload) or 0
                if latest_filled_qty > 0:
                    result = self._finalize_live_equity_entry_result(current_request, current_spec, latest_payload, order_id, f"live_partial_fill_after_cancel_attempt_{attempt}:{cancel_msg}", data=data)
                    if result.ok:
                        result.may_still_be_working = not cancel_ok
                        return result
                if not cancel_ok:
                    return OrderResult(ok=False, order_id=order_id, raw=latest_payload or current_spec, message=f"live_unfilled_cancel_failed:{cancel_msg}", simulated=False,
                                       may_still_be_working=True)
                return OrderResult(ok=False, order_id=order_id, raw=latest_payload or current_spec, message=LIVE_UNFILLED_CANCELED, simulated=False)
            cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
            latest_payload = cancel_payload or payload
            if latest_payload is not None and order_is_filled(latest_payload):
                return self._finalize_live_equity_entry_result(current_request, current_spec, latest_payload, order_id, f"live_fill_after_reprice_cancel_attempt_{attempt}:{cancel_msg}", data=data)
            latest_filled_qty = order_filled_qty(latest_payload) or 0
            if latest_filled_qty > 0:
                result = self._finalize_live_equity_entry_result(current_request, current_spec, latest_payload, order_id, f"live_partial_fill_after_reprice_cancel_attempt_{attempt}:{cancel_msg}", data=data)
                if result.ok:
                    result.may_still_be_working = not cancel_ok
                    return result
            if not cancel_ok:
                return OrderResult(ok=False, order_id=order_id, raw=latest_payload or current_spec, message=f"live_reprice_cancel_failed:{cancel_msg}", simulated=False,
                                   may_still_be_working=True)
            step_frac = max(0.05, float(self.config.execution.entry_live_reprice_step_frac))
            next_request = self._build_repriced_equity_request(current_request, data,
                                                               buffer_mult=1.0 + (attempt + 1) * step_frac)
            if next_request is None:
                return OrderResult(ok=False, order_id=order_id, raw=payload or current_spec, message="live_reprice_missing_or_stale_quotes", simulated=False)
            current_request = next_request
            current_spec = self._build_order(current_request)
        return OrderResult(ok=False, order_id=None, raw=current_spec, message="live_reprice_exhausted", simulated=False)

    def exit_reprice_deadline(self) -> float:
        """The deadline (monotonic seconds) a management pass starting now
        gives its missed live exits' re-sends: ``exit_live_reprice_max_seconds``
        from now. The position manager takes one per pass and hands it to
        every exit it sends in that pass, so the wait the re-sends add to the
        pass is bounded whatever the number of positions that miss together
        (2026-09-28)."""
        return time_module.monotonic() + float(self.config.execution.exit_live_reprice_max_seconds)

    def _exit_resend_refusal(self, request: OrderRequest, deadline: float) -> str | None:
        """Why no further order may follow ``request``'s missed exit, or None:
        the pass's time budget is spent (``deadline``, monotonic), or the
        equity session is no longer the one the exit was priced for (a NORMAL
        order past the close)."""
        if time_module.monotonic() >= deadline:
            return "time_budget"
        session = self._equity_session()
        if session != request.session:
            return f"session:{session}"
        return None

    def _submit_live_equity_exit_with_reprice(self, request: OrderRequest, data, deadline: float, *,
                                              on_order_sent: Callable[[str], None]) -> OrderResult:
        """A live marketable LIMIT exit, re-sent at a fresh quote while it misses.

        Each order is ``_submit_live_single_order_with_poll``'s: polled for
        ``entry_live_fill_timeout_seconds``, then cancelled, and handed to
        ``on_order_sent`` before it is polled. Another follows only an order
        the broker confirmed CANCELED with nothing filled
        (``LIVE_UNFILLED_CANCELED``). Any other outcome is returned as it is:
        a fill of any size, a refused submit, a missing order id, a cancel the
        broker did not confirm, and an order the broker REJECTED or EXPIRED
        after accepting it, or that was REPLACED at the broker (its
        replacement tracked in its place), the last three with
        ``;stopped=status:<STATUS>``. So no second exit order for the same
        shares goes out while one may still fill, except after an order whose
        submit's outcome is unknown (a POST that times out raises; one
        answered 5xx ends the call as ``status=5xx``, which
        ``order_result_needs_broker_recheck`` reads as possibly landed; the
        shared unknown-outcome path is queued). A re-send is priced off the
        quote read then, its spread buffer ``1 + n *
        exit_live_reprice_step_frac`` times the first's. There are at most
        ``exit_live_reprice_attempts`` of them, and none once the management
        pass's ``deadline`` (``exit_reprice_deadline``, shared by every exit
        of the pass) has passed or the session is no longer the one the exit
        was priced for. The first order always goes out. Then, with
        ``exit_live_market_fallback``, a regular-session exit goes out as a
        MARKET order under the same two limits, left working if it does not
        fill.

        The result's ``exit_limits_missed`` counts the limits that missed (0
        when the first order settled it): the ones confirmed CANCELED with
        nothing filled, never a rejection. Once a limit has missed, the
        message carries ``;exit_limits_missed=<n>`` too, then
        ``;exit_market_fallback`` for the MARKET order, or ``;stopped=<why>``
        when nothing filled (``attempts``, ``time_budget``, ``session:<now>``
        or ``missing_or_stale_quotes``). Its prefix is the last order's own,
        so the position manager settles the result as it did before. Until
        2026-09-28 the first miss ended the exit attempt, and the next order
        went out on the next cycle: about 24 s later on top_tier days
        (study B).
        """
        cfg = self.config.execution
        attempts = int(cfg.exit_live_reprice_attempts)
        started = time_module.monotonic()
        current = request
        missed = 0
        while True:
            result = self._submit_live_single_order_with_poll(self._build_order(current), cancel_on_timeout=True,
                                                              price_scale=1.0, on_order_sent=on_order_sent)
            if result.message != LIVE_UNFILLED_CANCELED:
                if result.message.startswith((LIVE_UNFILLED_REJECTED, LIVE_UNFILLED_EXPIRED, LIVE_ORDER_REPLACED)):
                    # The broker ended or someone changed the order: nothing
                    # follows it this pass (logged where it was read).
                    return replace(result, message=f"{result.message};exit_limits_missed={missed};"
                                                   f"stopped=status:{order_status(result.raw)}",
                                   exit_limits_missed=missed)
                if missed == 0:
                    return replace(result, exit_limits_missed=0)
                return replace(result, message=f"{result.message};exit_limits_missed={missed}",
                               exit_limits_missed=missed)
            missed += 1
            stopped = "attempts" if missed > attempts else self._exit_resend_refusal(request, deadline)
            if stopped is None:
                repriced = self._build_repriced_equity_request(
                    current, data, buffer_mult=1.0 + missed * float(cfg.exit_live_reprice_step_frac))
                if repriced is None:
                    stopped = "missing_or_stale_quotes"
                else:
                    LOG.log(TRADEFLOW_LEVEL, "Exit %s %s qty=%s: limit %.4f unfilled and cancelled (order %s) at "
                            "%.1fs; re-sending at %.4f (%d of %d)", request.intent.value, request.symbol,
                            request.qty, current.price, result.order_id, time_module.monotonic() - started,
                            repriced.price, missed, attempts)
                    current = repriced
                    continue
            break
        # The MARKET order follows limits that ran out or could not be priced
        # (it needs no quote), under the same budget and session checks.
        if (cfg.exit_live_market_fallback and request.session == "NORMAL"
                and stopped in ("attempts", "missing_or_stale_quotes")):
            stopped = self._exit_resend_refusal(request, deadline)
            if stopped is None:
                LOG.log(TRADEFLOW_LEVEL, "Exit %s %s qty=%s: %d limit order(s) unfilled and cancelled at %.1fs; "
                        "sending MARKET", request.intent.value, request.symbol, request.qty, missed,
                        time_module.monotonic() - started)
                market = OrderRequest(symbol=request.symbol, qty=request.qty, intent=request.intent,
                                      order_type="MARKET", session=request.session)
                result = self._submit_live_single_order_with_poll(self._build_order(market), cancel_on_timeout=False,
                                                                  price_scale=1.0, on_order_sent=on_order_sent)
                return replace(result, message=f"{result.message};exit_limits_missed={missed};exit_market_fallback",
                               exit_limits_missed=missed)
        return replace(result, message=f"{result.message};exit_limits_missed={missed};stopped={stopped}",
                       exit_limits_missed=missed)

    def preview_equity_entry(self, symbol: str, intent: OrderIntent, data=None) -> dict[str, Any] | None:
        session = self._equity_session()
        if session is None:
            return None
        market = self._equity_market(symbol, data, refresh_quotes=True)
        if market is None:
            return None
        bid, ask, last = market
        limit_price = self._equity_limit_price(intent, bid, ask, last)
        if limit_price is None:
            return None
        return {
            "session": session,
            "bid": bid,
            "ask": ask,
            "last": last,
            "limit_price": limit_price,
            "market_snapshot": self._market_snapshot_from_tuple(market),
        }

    def submit_equity_entry(self, symbol: str, qty: int, intent: OrderIntent, data=None, market_snapshot: Any | None = None,
                            side: Side | None = None, stop_price: float | None = None,
                            target_price: float | None = None,
                            post_fill_levels: PostFillLevels | None = None) -> OrderResult:
        """Send an equity entry: a marketable limit, or in bracket mode (with
        ``side`` and ``stop_price``) the entry with its resting protection.

        A bracketed entry needs ``post_fill_levels``, the caller's rule for
        the stop and target a fill leaves the trade with
        (``_finalize_bracket_protection``); without it the entry is refused
        with a ValueError rather than protected at levels the fill may have
        invalidated."""
        if not str(symbol or "").strip():
            return OrderResult(ok=False, order_id=None, raw=None, message="invalid_symbol", simulated=self.config.schwab.dry_run)
        if int(qty) <= 0:
            return OrderResult(ok=False, order_id=None, raw=None, message="invalid_qty", simulated=self.config.schwab.dry_run)
        session = self._equity_session()
        if session is None:
            return OrderResult(ok=False, order_id=None, raw=None, message=self._equity_session_blackout_reason(), simulated=self.config.schwab.dry_run)
        bracketed = self.bracket_orders_enabled() and side is not None and stop_price is not None
        if bracketed and post_fill_levels is None:
            raise ValueError(f"bracketed entry for {symbol} needs post_fill_levels, the caller's post-fill level rule")
        if bracketed and session != "NORMAL" and bool(self.config.execution.bracket_require_normal_session):
            # Schwab rejects STOP orders outside the NORMAL session. Refuse the
            # entry rather than silently opening it without resting protection.
            return OrderResult(ok=False, order_id=None, raw=None, message=f"bracket_requires_normal_session:{session}", simulated=self.config.schwab.dry_run)
        market = self._coerce_equity_market(market_snapshot)
        if market is None:
            market = self._equity_market(symbol, data, refresh_quotes=True)
        if market is None:
            return OrderResult(ok=False, order_id=None, raw=None, message="equity_missing_or_stale_quotes", simulated=self.config.schwab.dry_run)
        bid, ask, last = market
        limit_price = self._equity_limit_price(intent, bid, ask, last)
        if limit_price is None:
            return OrderResult(ok=False, order_id=None, raw=None, message="equity_invalid_limit_price", simulated=self.config.schwab.dry_run)
        request = OrderRequest(symbol=symbol, qty=qty, intent=intent, order_type="LIMIT", price=limit_price, session=session)
        if self.config.schwab.dry_run:
            result = self._simulate_equity_fill(request, data, refresh_quotes=False, market_snapshot=market)
            if bracketed and result.ok:
                # Dry-run keeps exits ENGINE-side (TradeManager.update_position already
                # decides stop/target identically), so the bracket is recorded
                # for parity/inspection but nothing rests at a broker. Live
                # fills at the resting limit will beat these poll-priced exits.
                # It records the levels a live entry's protection ends at: the
                # post-fill ones (_finalize_bracket_protection).
                assert side is not None and post_fill_levels is not None
                protect_stop, protect_target, _levels_reason = post_fill_levels(float(result.fill_price))
                direction = self._bracket_round_direction(side)
                result.bracket = {
                    "parent_order_id": None,
                    "sync_mode": str(self.config.execution.bracket_sync_mode),
                    "legs": str(self.config.execution.bracket_legs),
                    "session": str(session),
                    "stop_price": self._round_equity_price(protect_stop, direction),
                    "target_price": (
                        self._round_equity_price(protect_target, direction)
                        if (self.bracket_carries_target() and protect_target is not None) else None
                    ),
                    "qty": int(result.filled_qty or qty),
                    "oco_order_id": None,
                    "stop_order_id": None,
                    "target_order_id": None,
                    "child_order_ids": [],
                    "active": False,
                    "simulated": True,
                    "state": "dry_run",
                }
            return result
        if bracketed:
            assert side is not None and stop_price is not None and post_fill_levels is not None
            return self._submit_live_bracket_entry(request, side=side, stop_price=stop_price, target_price=target_price,
                                                   post_fill_levels=post_fill_levels, data=data)
        return self._submit_live_equity_entry_with_reprice(request, data=data)

    def submit_equity_exit(self, symbol: str, qty: int, intent: OrderIntent, data=None, market_snapshot: Any | None = None,
                           *, reprice_deadline: float, on_order_sent: Callable[[str], None]) -> OrderResult:
        """An engine exit for ``qty`` shares: a MARKET order in the regular
        session with ``market_exit_regular_hours``, a marketable LIMIT
        otherwise. A live LIMIT that misses is re-sent until
        ``reprice_deadline``, the management pass's (``exit_reprice_deadline``;
        ``_submit_live_equity_exit_with_reprice``). Each live order's id goes
        to ``on_order_sent`` before the order is polled."""
        if not str(symbol or "").strip():
            return OrderResult(ok=False, order_id=None, raw=None, message="invalid_symbol", simulated=self.config.schwab.dry_run)
        if int(qty) <= 0:
            return OrderResult(ok=False, order_id=None, raw=None, message="invalid_qty", simulated=self.config.schwab.dry_run)
        session = self._equity_session()
        if session is None:
            return OrderResult(ok=False, order_id=None, raw=None, message=self._equity_session_blackout_reason(), simulated=self.config.schwab.dry_run)
        market = self._coerce_equity_market(market_snapshot)
        if session == "NORMAL" and bool(self.config.execution.market_exit_regular_hours):
            request = OrderRequest(symbol=symbol, qty=qty, intent=intent, order_type="MARKET", session=session)
            if self.config.schwab.dry_run:
                return self._simulate_equity_fill(request, data, refresh_quotes=market is None, market_snapshot=market)
            return self._submit_live_single_order_with_poll(self._build_order(request), cancel_on_timeout=False,
                                                            price_scale=1.0, on_order_sent=on_order_sent)
        if market is None:
            market = self._equity_market(symbol, data, refresh_quotes=True)
        if market is None:
            return OrderResult(ok=False, order_id=None, raw=None, message="equity_missing_or_stale_quotes", simulated=self.config.schwab.dry_run)
        bid, ask, last = market
        limit_price = self._equity_limit_price(intent, bid, ask, last)
        if limit_price is None:
            return OrderResult(ok=False, order_id=None, raw=None, message="equity_invalid_limit_price", simulated=self.config.schwab.dry_run)
        request = OrderRequest(symbol=symbol, qty=qty, intent=intent, order_type="LIMIT", price=limit_price, session=session)
        if self.config.schwab.dry_run:
            return self._simulate_equity_fill(request, data, refresh_quotes=False, market_snapshot=market)
        return self._submit_live_equity_exit_with_reprice(request, data, reprice_deadline, on_order_sent=on_order_sent)

    # ------------------------------------------------------------------
    # Broker-side bracket (first-triggers-OCO) orders
    #
    # The entry goes out as ONE Schwab TRIGGER order whose child OCO carries
    # the protective stop (and, in ``stop_and_target`` leg mode, the target).
    # The exit then rests AT THE BROKER instead of waiting for the engine's
    # management poll to observe the level and fire a marketable limit.
    # ------------------------------------------------------------------

    def bracket_orders_enabled(self) -> bool:
        return bool(self.config.execution.bracket_orders_enabled)

    def bracket_carries_target(self) -> bool:
        """True when the target rests at the broker as an OCO sibling.

        ``stop_only`` keeps the target engine-side: the adaptive ladder
        deliberately declines a target-tag exit so it can roll to the next
        rung, and clears ``target_price`` entirely on the final rung to run a
        runner. A resting target limit fills through both.
        """
        return self.bracket_orders_enabled() and self.config.execution.bracket_legs == "stop_and_target"

    def disaster_stop_enabled(self) -> bool:
        """True when each equity position gets a static disaster stop
        (``execution.disaster_stop_enabled``; refused with brackets and for
        an option strategy at load)."""
        return bool(self.config.execution.disaster_stop_enabled)

    def disaster_stop_price(self, side: Side, entry_price: float, initial_stop: float) -> float:
        """Where a position's disaster stop rests: ``disaster_stop_r`` times
        its initial R beyond its initial stop, or ``disaster_stop_min_pct``
        of its entry price when that is further (below the stop for a LONG,
        floored at $0.01; above it for a SHORT). The floor keeps a trade
        whose initial risk is inside one minute's noise from resting its
        disaster stop there too. Unrounded: the order rounds it a tick away
        from the market (``_round_equity_price``)."""
        cfg = self.config.execution
        entry = float(entry_price)
        stop = float(initial_stop)
        distance = max(float(cfg.disaster_stop_r) * abs(entry - stop), float(cfg.disaster_stop_min_pct) * entry)
        if side == Side.LONG:
            return max(0.01, stop - distance)
        return stop + distance

    def stamp_disaster_stop_price(self, metadata: dict[str, Any], side: Side, entry_price: float,
                                  initial_stop: float) -> None:
        """Record in *metadata* the price the position's disaster stop rests
        at (``disaster_stop_price``), with the disaster stop on. A price
        already recorded is kept: a restored position keeps the one it
        opened with. Stamped in a dry run too, where nothing is placed."""
        if self.disaster_stop_enabled():
            metadata.setdefault(DISASTER_STOP_PRICE_KEY, self.disaster_stop_price(side, entry_price, initial_stop))

    def resting_stop_price(self, position: Position) -> float:
        """The level *position*'s resting broker stop rests at: its disaster
        price with the disaster stop on (never the engine's moving stop),
        else the engine's stop, which a bracket's stop follows in
        ``replace`` sync. Every path that opens or restores a position
        stamps the disaster price (``stamp_disaster_stop_price``), so one
        without it raises rather than rest a stop at a level nobody chose."""
        if not self.disaster_stop_enabled():
            return float(position.stop_price)
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        level = safe_float(meta.get(DISASTER_STOP_PRICE_KEY), None, finite=True)
        if level is None:
            raise ValueError(f"{position.symbol} has no {DISASTER_STOP_PRICE_KEY} to rest its disaster stop at")
        return level

    def protective_stop_level(self, side: Side, price: float) -> float:
        """The price a protective stop for a *side* position at *price*
        rests at: rounded to a valid tick, a tick away from the market (down
        for a LONG, up for a SHORT), as every protective order is sent."""
        return self._round_equity_price(float(price), self._bracket_round_direction(side))

    @staticmethod
    def _round_equity_price(price: float, direction: str = "nearest") -> float:
        """Round to a valid equity tick: a penny at/above $1, else 1/100 penny.

        Sub-penny prices are rejected outright on stop legs (SEC Rule 612), so
        bracket children cannot reuse the parent's raw ``.4f`` formatting.
        ``direction`` biases the rounding so a stop never lands TIGHTER than
        intended and a target never lands further away than intended.
        """
        value = float(price)
        quantum = 0.01 if abs(value) >= 1.0 else 0.0001
        scaled = value / quantum
        if direction == "down":
            ticks = math.floor(scaled + 1e-9)
        elif direction == "up":
            ticks = math.ceil(scaled - 1e-9)
        else:
            ticks = round(scaled)
        return round(ticks * quantum, 4)

    @staticmethod
    def _bracket_round_direction(side: Side) -> str:
        """Rounding bias for a side's protective levels.

        LONG rounds both levels DOWN: the stop gets marginally more room, the
        sell target gets marginally easier fill. SHORT mirrors with UP.
        """
        return "down" if side == Side.LONG else "up"

    @staticmethod
    def _equity_order_leg(symbol: str, qty: int, intent: OrderIntent) -> dict[str, Any]:
        return {
            "instruction": intent.value,
            "quantity": int(qty),
            "instrument": {"symbol": symbol, "assetType": ASSET_TYPE_EQUITY},
        }

    def bracket_stop_limit_price(self, side: Side, stop_price: float, *, initial_risk: float | None) -> float | None:
        """Limit price for a STOP_LIMIT protective child (None for plain STOP).

        Offset below (LONG) / above (SHORT) the trigger by
        ``bracket_stop_limit_offset_r`` units of the position's INITIAL R:
        ``initial_risk``, the per-share |entry - initial stop| the trade was
        opened with (``position_metrics.initial_risk_per_unit``), wherever
        the stop now rests. Until 2026-09-28 every caller measured R to the
        stop being placed, so once break-even moved the stop to about the
        entry the offset collapsed to about 0 and the limit sat on the
        trigger: a triggered stop left unfilled by the first tick through
        it. A STOP_LIMIT child without a positive initial R is refused
        (ValueError) rather than priced at a made-up offset.
        """
        cfg = self.config.execution
        if cfg.bracket_stop_order_type != "STOP_LIMIT":
            return None
        risk = safe_float(initial_risk, None, finite=True)
        if risk is None or risk <= 0:
            raise ValueError(f"a STOP_LIMIT protective child needs the position's initial risk, got {initial_risk!r}")
        offset = risk * float(cfg.bracket_stop_limit_offset_r)
        if side == Side.LONG:
            return max(0.0001, self._round_equity_price(float(stop_price) - offset, "down"))
        return self._round_equity_price(float(stop_price) + offset, "up")

    def _bracket_stop_child(self, symbol: str, qty: int, exit_intent: OrderIntent, stop_price: float,
                            stop_limit_price: float | None, session: str, *, order_type: str) -> dict[str, Any]:
        """A protective stop order: ``order_type`` is a bracket's
        ``bracket_stop_order_type``, or ``STOP`` for a disaster stop."""
        child: dict[str, Any] = {
            "orderStrategyType": "SINGLE",
            "session": session,
            "duration": "DAY",
            "orderType": order_type,
            "stopPrice": f"{stop_price:.4f}",
            "orderLegCollection": [self._equity_order_leg(symbol, qty, exit_intent)],
        }
        if order_type == "STOP_LIMIT":
            if stop_limit_price is None:
                raise ValueError("STOP_LIMIT bracket child requires a stop_limit_price")
            child["price"] = f"{stop_limit_price:.4f}"
        return child

    def _bracket_target_child(self, symbol: str, qty: int, exit_intent: OrderIntent, target_price: float,
                              session: str) -> dict[str, Any]:
        return {
            "orderStrategyType": "SINGLE",
            "session": session,
            "duration": "DAY",
            "orderType": "LIMIT",
            "price": f"{target_price:.4f}",
            "orderLegCollection": [self._equity_order_leg(symbol, qty, exit_intent)],
        }

    def _bracket_exit_children(self, symbol: str, qty: int, side: Side, stop_price: float,
                               target_price: float | None, session: str, *,
                               initial_risk: float | None) -> tuple[list[dict[str, Any]], float, float | None]:
        """Build the protective child order(s) plus the rounded levels used.

        Returns ``(children, rounded_stop, rounded_target_or_None)``. The
        target child is omitted in ``stop_only`` leg mode or when the strategy
        supplied no target (runner signals carry ``target_price=None``).
        ``initial_risk`` prices a STOP_LIMIT child (``bracket_stop_limit_price``).
        """
        exit_intent = self.order_intent_for_exit(side)
        direction = self._bracket_round_direction(side)
        rounded_stop = self._round_equity_price(stop_price, direction)
        stop_limit = self.bracket_stop_limit_price(side, rounded_stop, initial_risk=initial_risk)
        children = [self._bracket_stop_child(symbol, qty, exit_intent, rounded_stop, stop_limit, session,
                                             order_type=self.config.execution.bracket_stop_order_type)]
        rounded_target: float | None = None
        if self.bracket_carries_target() and target_price is not None:
            rounded_target = self._round_equity_price(target_price, direction)
            # Target first so the OCO reads target-then-stop in the payload.
            children.insert(0, self._bracket_target_child(symbol, qty, exit_intent, rounded_target, session))
        return children, rounded_stop, rounded_target

    @staticmethod
    def _wrap_oco(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Wrap protective children in an OCO only when there are two of them.

        A lone stop is attached to the TRIGGER parent directly: an OCO with a
        single child is not a meaningful one-cancels-other group.
        """
        if len(children) <= 1:
            return children
        return [{"orderStrategyType": "OCO", "childOrderStrategies": children}]

    def build_bracket_order(self, request: OrderRequest, *, side: Side, stop_price: float,
                            target_price: float | None) -> dict[str, Any]:
        """One-order first-triggers-OCO bracket: entry + protective exit(s).

        The fill is not known yet, so the initial R that prices a STOP_LIMIT
        child is measured from the entry's limit price."""
        children, _rounded_stop, _rounded_target = self._bracket_exit_children(
            request.symbol, request.qty, side, stop_price, target_price, request.session,
            initial_risk=abs(float(request.price or 0.0) - float(stop_price)),
        )
        spec = self._build_order(request)
        spec["orderStrategyType"] = "TRIGGER"
        spec["childOrderStrategies"] = self._wrap_oco(children)
        return spec

    def build_protective_oco_order(self, symbol: str, qty: int, side: Side, stop_price: float,
                                   target_price: float | None, session: str, *,
                                   initial_risk: float | None) -> dict[str, Any]:
        """Standalone protective OCO for an ALREADY-OPEN position.

        Used when a bracketed entry only partially filled and the broker
        cancelled the untriggered children along with the parent, and on
        startup reconcile to re-protect an adopted position.
        """
        children, _rounded_stop, _rounded_target = self._bracket_exit_children(
            symbol, qty, side, stop_price, target_price, session, initial_risk=initial_risk,
        )
        wrapped = self._wrap_oco(children)
        return wrapped[0] if len(wrapped) == 1 else {"orderStrategyType": "OCO", "childOrderStrategies": children}

    def fetch_order_states(self, lookback_minutes: int = 480) -> dict[str, dict[str, Any]] | None:
        """Snapshot every recent order (and child) in ONE ``account_orders`` call.

        Deliberately not per-position ``order_details``: at 4 open positions and
        ``loop_sleep_seconds: 2.0`` that would be 120 requests/minute, which is
        the entire Schwab budget. Returns None when the call fails so callers
        can distinguish "no data" from "nothing filled".
        """
        if self.config.schwab.dry_run:
            return {}
        now = sessions.now_et().astimezone(UTC)
        try:
            payload = call_schwab_json(
                self.client, "account_orders", self.account_hash,
                now - datetime.timedelta(minutes=max(1, int(lookback_minutes))), now,
            )
        except Exception as exc:
            LOG.warning("account_orders failed during bracket reconcile: %s", exc)
            return None
        out: dict[str, dict[str, Any]] = {}
        flatten_order_tree(payload, out)
        return out

    def fetch_account_positions(self) -> list[dict[str, Any]] | None:
        """The account's position rows (``extract_broker_positions``), or None
        when the account could not be read.

        Never an empty list for a failed read: the startup reconciler settles
        and prunes on this list, and an error body read as "no positions"
        booked every live position ``closed_outside_bot`` and cancelled its
        broker stop. Read in dry-run too, like every reconcile read.
        """
        try:
            payload = call_schwab_json(self.client, "account_details", self.account_hash, fields="positions")
            return extract_broker_positions(payload)
        except Exception as exc:
            LOG.warning("account_details read failed: %s", exc)
            return None

    def fetch_orders(self, from_ts: str, to_ts: str) -> list[dict[str, Any]] | None:
        """The account's orders entered between the two ISO timestamps,
        whatever their status (``extract_orders``), or None when they could
        not be read."""
        try:
            payload = call_schwab_json(
                self.client, "account_orders", self.account_hash, fromEnteredTime=from_ts, toEnteredTime=to_ts,
            )
            return extract_orders(payload)
        except Exception as exc:
            LOG.warning("account_orders read failed (%s: %s)", type(exc).__name__, exc)
            return None

    def fetch_working_orders(self, from_ts: str, to_ts: str) -> list[dict[str, Any]] | None:
        """``fetch_orders``' rows of the orders that may still work
        (``order_may_be_live``: any status but a terminal one), or None when
        they could not be read."""
        orders = self.fetch_orders(from_ts, to_ts)
        return None if orders is None else [order for order in orders if order_may_be_live(order["status"])]

    def todays_orders(self, since: datetime.datetime | None = None) -> list[dict[str, Any]] | None:
        """``fetch_orders`` from *since* (midnight ET when None; a DAY order
        entered before today no longer works) to now, or None when they could
        not be read."""
        now = sessions.now_et()
        start = since if since is not None else now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.fetch_orders(
            *(stamp.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
              for stamp in (start, now)),
        )

    def order_state(self, order_id: str) -> dict[str, Any] | None:
        """One order's state row (the ``fetch_order_states`` shape) via
        ``order_details``, for an order the account_orders listing did not
        return -- aged out of its lookback, or not listed yet. None when the
        broker cannot be read."""
        if self.config.schwab.dry_run:
            return None
        payload, _status = self._equity_order_details(str(order_id))
        if not isinstance(payload, dict):
            return None
        out: dict[str, dict[str, Any]] = {}
        flatten_order_tree(payload, out)
        return out.get(str(order_id))

    def _bracket_state_from_order(self, order_id: str) -> dict[str, Any]:
        """Read child order ids back off a submitted bracket/OCO parent.

        A ``stop_only`` standalone protective order has no children at all --
        the order itself IS the stop, so its own id is reported as the stop id.
        """
        payload, _status = self._equity_order_details(order_id)
        children = extract_bracket_children(payload)
        if not children["child_order_ids"] and isinstance(payload, dict):
            order_type = str(payload.get("orderType") or "").upper()
            if order_type in STOP_ORDER_TYPES:
                children["stop_order_id"] = str(order_id)
                children["child_order_ids"] = [str(order_id)]
            elif order_type == "LIMIT":
                children["target_order_id"] = str(order_id)
                children["child_order_ids"] = [str(order_id)]
        return children

    def submit_protective_oco(self, symbol: str, qty: int, side: Side, stop_price: float,
                              target_price: float | None, session: str, *,
                              initial_risk: float | None) -> OrderResult:
        """Submit standalone protection for an already-open position.

        Used when a bracketed entry filled but its children never materialised
        (partial-fill cancel path), and on startup reconcile to re-protect an
        adopted position.
        """
        spec = self.build_protective_oco_order(symbol, qty, side, stop_price, target_price, session,
                                               initial_risk=initial_risk)
        if self.config.schwab.dry_run:
            LOG.info("DRY RUN protective OCO: %s", spec)
            return OrderResult(ok=True, order_id=None, raw=spec, message="dry_run_protective_oco", simulated=True)
        response = self._submit_live_order_spec(spec)
        status_code = getattr(response, "status_code", None)
        if not response_ok(response):
            LOG.error("Protective OCO submission failed symbol=%s qty=%s status=%s", symbol, qty, status_code)
            return OrderResult(ok=False, order_id=None, raw=getattr(response, "text", spec),
                               message=f"protective_oco_status={status_code}", simulated=False)
        order_id = self._response_order_id(response)
        if not order_id:
            return OrderResult(ok=False, order_id=None, raw=getattr(response, "text", spec),
                               message="protective_oco_missing_order_id", simulated=False)
        return OrderResult(ok=True, order_id=str(order_id), raw=spec, message="protective_oco_submitted",
                           simulated=False, bracket=self._bracket_state_from_order(str(order_id)))

    def ensure_position_protected(self, symbol: str, qty: int, side: Side, stop_price: float,
                                  target_price: float | None, *, initial_risk: float | None,
                                  parent_order_id: str | None = None,
                                  known_bracket: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Guarantee an open position has resting broker protection.

        Adopts protection that is still working and only submits fresh
        protection when none is -- submitting unconditionally would double the
        resting exit size and take the position net short when it triggers.

        ``known_bracket`` is the bracket the bot last tracked for this position
        (restored metadata). Its child ids are the CURRENT ones: a stop moved
        by replace is a new order, so the parent's original children read
        REPLACED and would look dead while the replacement still rests.
        Without it, the children are read off ``parent_order_id``.

        An adopted child resting a different share count than ``qty`` (sized to
        the requested entry, not the fill), or one whose size cannot be read,
        is resized, and the levels recorded are the ones the broker actually
        holds.

        A dry run adopts, resizes and submits nothing: its bracket is
        simulated, as a dry-run entry's is, so the engine owns every exit. It
        keeps the ids ``known_bracket`` tracks, which the reconcile reads as
        the position's own rather than as foreign orders.

        ``stop_price`` is the level the protection rests at
        (``resting_stop_price``). ``initial_risk`` is the position's initial
        R per share (``position_metrics.initial_risk_per_unit``), which prices
        a bracket's STOP_LIMIT child (``bracket_stop_limit_price``). With
        ``execution.disaster_stop_enabled`` the protection is the position's
        disaster stop (``DISASTER_SYNC_MODE``): one plain STOP for the regular
        session, never a target, adopted, resized and mirrored in a dry run as
        a bracket's stop is, and placed by ``_place_disaster_stop``; neither
        ``initial_risk`` nor ``parent_order_id`` (a bracketed entry's) is read
        for it. Before one is placed in the regular session the day's orders
        are read: an exit stop no record tracks that may still work on the
        symbol for the side (``resting_exit_stop``: one moved in the app, or
        placed there, in any status but a terminal one) is adopted and
        resized instead, and logged; orders that cannot be read place nothing
        (``unprotected``, retried) (2026-09-29).

        Returns the bracket state dict, or None when neither brackets nor the
        disaster stop are on.
        """
        disaster = self.disaster_stop_enabled()
        if not (disaster or self.bracket_orders_enabled()):
            return None
        if int(qty) <= 0:
            return None
        direction = self._bracket_round_direction(side)
        base: dict[str, Any]
        if disaster:
            base = {
                "parent_order_id": None,
                "sync_mode": DISASTER_SYNC_MODE,
                "legs": "stop_only",
                "session": "NORMAL",
                "stop_price": self._round_equity_price(stop_price, direction),
                "target_price": None,
                "qty": int(qty),
            }
        else:
            base = {
                "parent_order_id": str(parent_order_id) if parent_order_id else None,
                "sync_mode": str(self.config.execution.bracket_sync_mode),
                "legs": str(self.config.execution.bracket_legs),
                "session": self._equity_session() or "NORMAL",
                "stop_price": self._round_equity_price(stop_price, direction),
                "target_price": (
                    self._round_equity_price(target_price, direction)
                    if (self.bracket_carries_target() and target_price is not None) else None
                ),
                "qty": int(qty),
            }
        session = str(base["session"])
        if self.config.schwab.dry_run:
            # What rests at the broker in a dry run is the real account's
            # protection, not the paper position's. A dry-run restore adopted
            # the real stop as an active bracket, so RiskManager stood the
            # engine stop down for the broker, while a dry run reads no order
            # state and never saw that stop fill: the paper position never
            # exited on its stop (2026-09-25).
            mirrored = self._tracked_protection_ids(known_bracket) or {
                **dict.fromkeys(BRACKET_ID_KEYS), "child_order_ids": [],
            }
            return {**base, **mirrored, "active": False, "simulated": True, "state": "dry_run"}
        existing = self._adoptable_protection(None if disaster else parent_order_id, known_bracket)
        if existing is None and disaster and self.regular_session_open():
            # A stop no record tracks already rests on the symbol for the
            # side: one moved in the app (Schwab REPLACES it under a new id)
            # or placed there. A disaster stop placed beside it sells the
            # same shares twice, so it is adopted instead (2026-09-29).
            # Outside the regular session nothing is placed, so nothing is
            # read either.
            orders = self.todays_orders()
            if orders is None:
                LOG.error(
                    "Disaster stop for %s qty=%s: the day's orders could not be read to look for a stop already "
                    "resting on it, so none is placed until they can be", symbol, qty,
                )
                return {**base, **dict.fromkeys(BRACKET_ID_KEYS), "child_order_ids": [], "active": False,
                        "state": "unprotected", "attempted_at": sessions.now_et().isoformat()}
            resting = resting_exit_stop(orders, symbol, side)
            if resting is not None:
                LOG.warning(
                    "Disaster stop for %s: exit stop %s already rests on it (moved or placed at the broker); "
                    "adopting it instead of placing another beside it", symbol, resting["stop_order_id"],
                )
                existing = self._adoptable_protection(None, resting)
        if existing is not None:
            resting_qty = existing.pop("resting_qty", None)
            adopted = {**base, **existing, "active": True, "state": "adopted"}
            # A size that could not be read is re-issued at ``qty`` too (fail
            # closed, 2026-09-25): the stop may rest more shares than are
            # held. Until then an unread size was trusted, so restore_basic
            # left a 10-share stop resting against 7 held shares.
            if resting_qty is None or int(resting_qty) != int(qty):
                adopted["qty"] = None if resting_qty is None else int(resting_qty)
                resized, msg = self.resize_bracket_children(adopted, symbol, side, int(qty), session,
                                                            initial_risk=initial_risk)
                if not resized:
                    adopted["state"] = "qty_mismatch" if resting_qty is not None else "qty_unverified"
                    LOG.error(
                        "Adopted protection for %s rests %s shares against a %s-share position and could not "
                        "be resized (%s) -- a resting exit larger than the position would flip it on trigger",
                        symbol, "an unknown number of" if resting_qty is None else resting_qty, qty, msg,
                    )
            return adopted
        if disaster:
            return self._place_disaster_stop(symbol, int(qty), side, base)
        replacement = self.submit_protective_oco(
            symbol, int(qty), side, float(base["stop_price"]), target_price, session, initial_risk=initial_risk,
        )
        if not replacement.ok:
            LOG.error(
                "Could not establish resting protection for %s qty=%s (%s) - position is open with NO broker stop",
                symbol, qty, replacement.message,
            )
            return {**base, "active": False, "state": "unprotected",
                    "oco_order_id": None, "stop_order_id": None, "target_order_id": None, "child_order_ids": []}
        return {**base, **(replacement.bracket or {}), "protective_order_id": replacement.order_id,
                "active": True, "state": "standalone_oco"}

    def _place_disaster_stop(self, symbol: str, qty: int, side: Side, base: dict[str, Any]) -> dict[str, Any]:
        """Submit a position's disaster stop: one plain STOP (never a
        STOP_LIMIT, which a flush can leave unfilled) for the regular
        session, DAY, at ``base["stop_price"]``. The record it returns is
        ``active`` only once the broker returned the new order's id.

        - Outside the regular session nothing is sent (Schwab rejects a STOP
          there): ``pending_session``, placed by the first regular-session
          cycle (``PositionManager.ensure_disaster_stop``).
        - A submit the broker refused (a 4xx status: the request was turned
          down, nothing was placed): ``unprotected``, which the position
          manager retries.
        - A submit whose outcome is unknown may have left the stop resting,
          and a second one beside it sells the shares twice: ``unconfirmed``.
          That is a transport failure or a timed-out response
          (``SCHWAB_WRITE_UNKNOWN_OUTCOME``: a POST whose response times out
          raises ``ReadTimeout``, since the client never retries a POST), a
          5xx (a gateway error on a POST says nothing of whether the order
          landed; the client retries only GET, PUT and DELETE, 2026-09-29), a
          status that cannot be read as a number or is neither 2xx nor 4xx,
          and a 2xx without the new order's id. The record keeps the price
          and quantity sent, and the position manager looks for exactly that
          stop among the day's orders before it places another.

        Both carry the attempt's time (``attempted_at``), which the retry
        waits on; an unconfirmed one carries it as its submit's time too
        (``sent_at``, required on every unconfirmed record since
        2026-09-29), which the lookup's window starts from and a miss never
        moves.
        """
        no_ids = {**dict.fromkeys(BRACKET_ID_KEYS), "child_order_ids": []}
        if not self.regular_session_open():
            return {**base, **no_ids, "active": False, "state": "pending_session"}
        attempted_at = sessions.now_et().isoformat()
        stop_price = float(base["stop_price"])
        spec = self._bracket_stop_child(symbol, qty, self.order_intent_for_exit(side), stop_price, None, "NORMAL",
                                        order_type="STOP")
        try:
            response = self._submit_live_order_spec(spec)
        except SCHWAB_WRITE_UNKNOWN_OUTCOME as exc:
            LOG.error(
                "Disaster stop for %s qty=%s at %s: the submit's outcome is unknown (%s: %s), so it may rest at "
                "the broker or not; the day's orders are read for it before another is sent",
                symbol, qty, stop_price, type(exc).__name__, exc,
            )
            return {**base, **no_ids, "active": False, "state": "unconfirmed", "attempted_at": attempted_at,
                    "sent_at": attempted_at}
        status_code = getattr(response, "status_code", None)
        status = safe_int(status_code)
        if status is not None and 400 <= status < 500:
            LOG.error(
                "Disaster stop for %s qty=%s at %s was refused (status=%s); the position has no broker stop "
                "until a retry places one", symbol, qty, stop_price, status_code,
            )
            return {**base, **no_ids, "active": False, "state": "unprotected", "attempted_at": attempted_at}
        if status is None or not 200 <= status < 300:
            LOG.error(
                "Disaster stop for %s qty=%s at %s: the submit's outcome is unknown (status=%s), so it may rest "
                "at the broker or not; the day's orders are read for it before another is sent",
                symbol, qty, stop_price, status_code,
            )
            return {**base, **no_ids, "active": False, "state": "unconfirmed", "attempted_at": attempted_at,
                    "sent_at": attempted_at}
        order_id = self._response_order_id(response)
        if not order_id:
            LOG.error(
                "Disaster stop for %s qty=%s at %s was accepted (status=%s) without its order id; the day's "
                "orders are read for it before another is sent", symbol, qty, stop_price, status_code,
            )
            return {**base, **no_ids, "active": False, "state": "unconfirmed", "attempted_at": attempted_at,
                    "sent_at": attempted_at}
        LOG.info("Disaster stop for %s qty=%s rests at %s (order %s)", symbol, qty, stop_price, order_id)
        return {**base, **no_ids, "stop_order_id": str(order_id), "child_order_ids": [str(order_id)],
                "active": True, "state": "disaster_stop"}

    @staticmethod
    def _tracked_protection_ids(known_bracket: dict[str, Any] | None) -> dict[str, Any] | None:
        """The protective order ids *known_bracket* tracks, with the fills
        of them already booked (``booked_child_fills``, when it has any), or
        None when it tracks no stop. The booked fills go along so the fill
        reconcile and a later cancel book only what is new (2026-09-29): an
        adopted stop that had filled in part (while the bot was down, or
        booked by an unconfirmed stop's lookup) was booked again, in full,
        when the rest of it filled."""
        if not (isinstance(known_bracket, dict) and known_bracket.get("stop_order_id")):
            return None
        ids: dict[str, Any] = {key: known_bracket.get(key) for key in BRACKET_ID_KEYS}
        ids["child_order_ids"] = [str(oid) for oid in (known_bracket.get("child_order_ids") or []) if oid]
        if known_bracket.get("booked_child_fills"):
            ids["booked_child_fills"] = {str(oid): int(qty) for oid, qty in known_bracket["booked_child_fills"].items()}
        return ids

    def _adoptable_protection(self, parent_order_id: str | None,
                              known_bracket: dict[str, Any] | None) -> dict[str, Any] | None:
        """Child ids (plus what they rest) of protection still working, or None.

        A stub a read of the orders just listed as one that may still work
        (``broker_payloads.listed_stop``: the unconfirmed stop's lookup, a
        stop resting on the symbol) is adopted as that read lists it, never
        read again (2026-09-29): a stop that filled between the two reads
        read as gone, a new stop was placed beside the fill, and the fill was
        never booked. Once adopted it is tracked, and the fill reconcile
        books what it sells.

        Otherwise each child's state comes from the account_orders listing
        or, when the listing does not return it (it failed, or the order is
        older than its 8-hour lookback: a stop entered before an overnight
        hold), from ``order_state``, as
        ``startup_reconciler._drop_retired_orders`` reads a missing id
        (2026-09-25). Until then a missing stop was adopted with no size, so
        ``ensure_position_protected`` never resized it. A stop neither read
        returns is still adopted, rather than risk stacking a second
        protective order on a live one, with its size unknown
        (``resting_qty`` None), which ``ensure_position_protected`` re-issues
        at the position's size.
        """
        ids = self._tracked_protection_ids(known_bracket)
        if ids is None and parent_order_id:
            ids = self._bracket_state_from_order(str(parent_order_id))
        if ids is None:
            return None
        stop_id = ids.get("stop_order_id")
        if not stop_id:
            return None
        listed = known_bracket.get("listed") if isinstance(known_bracket, dict) else None
        if listed is not None:
            adopted = {**ids, "resting_qty": listed["resting_qty"]}
            if listed["stop_price"] is not None:
                adopted["stop_price"] = float(listed["stop_price"])
            return adopted
        states = self.fetch_order_states() or {}

        def _state(order_id: Any) -> dict[str, Any] | None:
            listed = states.get(str(order_id))
            return listed if listed is not None else self.order_state(str(order_id))

        stop_state = _state(stop_id)
        if stop_state is not None and (stop_state.get("is_filled") or stop_state.get("is_terminal_failure")):
            return None
        adopted = dict(ids)
        if stop_state is not None:
            remaining = stop_state.get("remaining_qty")
            adopted["resting_qty"] = remaining if remaining is not None else stop_state.get("leg_qty")
            if stop_state.get("stop_price") is not None:
                adopted["stop_price"] = float(stop_state["stop_price"])
        target_id = ids.get("target_order_id")
        if target_id:
            target_state = _state(target_id)
            if target_state is not None and (target_state.get("is_filled") or target_state.get("is_terminal_failure")):
                adopted["target_order_id"] = None
                adopted["child_order_ids"] = [oid for oid in adopted.get("child_order_ids") or [] if oid != str(target_id)]
            elif target_state is not None and target_state.get("price") is not None:
                adopted["target_price"] = float(target_state["price"])
        return adopted

    def replace_bracket_child(self, bracket: dict[str, Any], child_key: str, spec: dict[str, Any]) -> tuple[bool, str]:
        """Replace the resting protective child ``bracket[child_key]`` in place.

        ``replace_order`` is atomic at the broker, so the position is never
        momentarily unprotected the way a cancel-then-place pair would be.

        It is also NOT an edit: Schwab cancels the original (status REPLACED)
        and creates a new order, returning the new id in the Location header.
        The bracket is re-pointed at that id. Keeping the old one sent every
        later replace at a dead order -- rejected, so the broker stop froze at
        its first moved level while the engine kept deferring its stop to it --
        and the fill reconcile watched the dead id, so the replacement's fill
        was never booked.

        A dry run sends nothing, as no executor write does in a dry run
        (``submit_protective_oco``, ``cancel_bracket``,
        ``cancel_working_order``). This one had no guard, and a dry-run
        restore that adopted the REAL resting stop resized it through here, so
        the paper bot replaced the user's stop at the broker (2026-09-25).
        Since then a dry run adopts nothing (``ensure_position_protected``).
        """
        child_order_id = str(bracket.get(child_key) or "")
        if self.config.schwab.dry_run:
            LOG.info("DRY RUN bracket replace %s %s: %s", child_key, child_order_id, spec)
            return True, "dry_run_replace"
        try:
            response = call_schwab_client(self.client, "replace_order", self.account_hash, child_order_id, spec)
        except Exception as exc:
            return False, f"replace_error:{exc}"
        status_code = getattr(response, "status_code", None)
        if not response_ok(response):
            return False, f"replace_status={status_code}"
        new_order_id = self._response_order_id(response)
        if not new_order_id:
            LOG.error(
                "Bracket %s %s replaced but the broker returned no new order id; "
                "the bracket still points at the replaced order", child_key, child_order_id,
            )
            return True, f"replaced:{status_code}:new_id_unknown"
        if new_order_id != child_order_id:
            bracket[child_key] = new_order_id
            children = [str(oid) for oid in (bracket.get("child_order_ids") or [])]
            if child_order_id in children:
                children[children.index(child_order_id)] = new_order_id
            else:
                children.append(new_order_id)
            bracket["child_order_ids"] = children
        return True, f"replaced:{status_code}"

    def resize_bracket_children(self, bracket: dict[str, Any], symbol: str, side: Side, qty: int,
                                session: str, *, initial_risk: float | None) -> tuple[bool, str]:
        """Re-issue the resting protective children at a new share count, at
        the levels they rest at (a disaster stop as the plain STOP it is).

        The short-flip guard: children are submitted for the REQUESTED entry
        quantity, so a partial entry fill leaves an oversized resting exit that
        would take a long-only strategy net short when it triggers.
        ``initial_risk`` prices a bracket's STOP_LIMIT child
        (``bracket_stop_limit_price``); a disaster stop needs none.
        """
        exit_intent = self.order_intent_for_exit(side)
        stop_price = float(bracket.get("stop_price") or 0.0)
        target_price = bracket.get("target_price")
        messages: list[str] = []
        ok = True
        if bracket.get("stop_order_id") and stop_price > 0:
            if is_disaster_stop(bracket):
                spec = self._bracket_stop_child(symbol, qty, exit_intent, stop_price, None, session, order_type="STOP")
            else:
                stop_limit = self.bracket_stop_limit_price(side, stop_price, initial_risk=initial_risk)
                spec = self._bracket_stop_child(symbol, qty, exit_intent, stop_price, stop_limit, session,
                                                order_type=self.config.execution.bracket_stop_order_type)
            child_ok, msg = self.replace_bracket_child(bracket, "stop_order_id", spec)
            ok = ok and child_ok
            messages.append(f"stop:{msg}")
        if bracket.get("target_order_id") and target_price is not None:
            spec = self._bracket_target_child(symbol, qty, exit_intent, float(target_price), session)
            child_ok, msg = self.replace_bracket_child(bracket, "target_order_id", spec)
            ok = ok and child_ok
            messages.append(f"target:{msg}")
        if ok:
            bracket["qty"] = int(qty)
        return ok, ",".join(messages) if messages else "no_children_to_resize"

    def sync_bracket_levels(self, bracket: dict[str, Any], symbol: str, side: Side, qty: int,
                            stop_price: float, target_price: float | None, *,
                            initial_risk: float | None) -> list[dict[str, Any]]:
        """Replace resting children whose level has moved beyond the debounce.

        Returns one management-adjustment record per child actually replaced,
        so the caller can log them alongside the engine's own adjustments. The
        debounce keeps a per-cycle trail ratchet from spending the Schwab rate
        budget on sub-penny moves. ``initial_risk`` prices a STOP_LIMIT child
        (``bracket_stop_limit_price``).

        A replace that fails leaves the bracket recording the level that still
        rests. Nothing defers to it: the engine checks its own stop every
        cycle (``TradeManager.update_position``), and the next cycle tries the
        replace again.
        """
        min_delta = float(self.config.execution.bracket_replace_min_price_delta)
        exit_intent = self.order_intent_for_exit(side)
        direction = self._bracket_round_direction(side)
        session = str(bracket.get("session") or "NORMAL")
        adjustments: list[dict[str, Any]] = []

        if bracket.get("stop_order_id") is not None:
            resting_stop = bracket.get("stop_price")
            rounded = self._round_equity_price(stop_price, direction)
            if resting_stop is None or abs(rounded - float(resting_stop)) >= min_delta:
                stop_limit = self.bracket_stop_limit_price(side, rounded, initial_risk=initial_risk)
                spec = self._bracket_stop_child(symbol, int(qty), exit_intent, rounded, stop_limit, session,
                                                order_type=self.config.execution.bracket_stop_order_type)
                ok, msg = self.replace_bracket_child(bracket, "stop_order_id", spec)
                if ok:
                    bracket["stop_price"] = rounded
                    adjustments.append({"manager": "bracket_sync", "kind": "stop", "reason": "replace_child",
                                        "from": resting_stop, "to": rounded})
                else:
                    LOG.warning("Bracket stop replace failed for %s (%s); the broker still rests at %s and "
                                "the engine enforces its own stop at %s", symbol, msg, resting_stop, rounded)

        if bracket.get("target_order_id") is not None and target_price is not None:
            resting_target = bracket.get("target_price")
            rounded = self._round_equity_price(target_price, direction)
            if resting_target is None or abs(rounded - float(resting_target)) >= min_delta:
                spec = self._bracket_target_child(symbol, int(qty), exit_intent, rounded, session)
                ok, msg = self.replace_bracket_child(bracket, "target_order_id", spec)
                if ok:
                    bracket["target_price"] = rounded
                    adjustments.append({"manager": "bracket_sync", "kind": "target", "reason": "replace_child",
                                        "from": resting_target, "to": rounded})
                else:
                    LOG.warning("Bracket target replace failed for %s (%s); broker still rests at %s",
                                symbol, msg, resting_target)
        return adjustments

    def cancel_bracket(self, bracket: dict[str, Any] | None) -> BracketCancel:
        """Cancel every resting protective order for a position.

        Called before ANY engine-side exit (peak giveback, time stop, CHoCH,
        force flatten). Without it the engine's market-out and the broker's
        resting stop both fill and the strategy ends up net short.

        The wrapper (the OCO, or the standalone protective order) takes the
        original legs down in one call. A child REPLACED since entry is a new
        order the wrapper may not own, so every tracked child id is checked
        too and cancelled if it is still live.

        The result carries what the children FILLED before the cancel landed.
        A stop that triggered after this cycle's fill reconcile has already
        sold those shares; the caller must book them and exit only the rest.
        A disaster stop's fills carry ``disaster_stop``, a bracket stop's
        ``broker_stop`` (``protective_stop_reason``).
        """
        if not isinstance(bracket, dict):
            return BracketCancel(True, "no_bracket")
        if self.config.schwab.dry_run:
            return BracketCancel(True, "dry_run_cancel")
        wrapper_id, child_ids = bracket_wrapper_and_children(bracket)
        if not wrapper_id and not child_ids:
            return BracketCancel(True, "no_resting_orders")
        ok = True
        messages: list[str] = []
        fills: dict[str, tuple[int, float | None, str]] = {}
        stop_reason = protective_stop_reason(bracket)
        if wrapper_id:
            cancel_ok, msg, payload = self._cancel_live_equity_order(wrapper_id)
            ok = cancel_ok
            messages.append(f"{wrapper_id}:{msg}")
            collect_protective_fills(payload, fills, stop_reason=stop_reason)
        for child_id in child_ids:
            if wrapper_id:
                # Usually already down with the wrapper: confirm before
                # spending a cancel call on it.
                payload, _status = self._equity_order_details(child_id)
                if payload is not None and (order_is_terminal_failure(payload) or order_is_filled(payload)):
                    collect_protective_fills(payload, fills, stop_reason=stop_reason)
                    continue
            cancel_ok, msg, payload = self._cancel_live_equity_order(child_id)
            ok = ok and cancel_ok
            messages.append(f"{child_id}:{msg}")
            collect_protective_fills(payload, fills, stop_reason=stop_reason)
        # Fills already booked for a child the bracket no longer tracks (a
        # dead stop an unconfirmed retire dropped): its wrapper's payload
        # still carries it and reports them again (2026-09-25).
        for oid, booked in (bracket.get("booked_child_fills") or {}).items():
            if str(oid) in fills:
                qty, px, kind = fills[str(oid)]
                if qty - int(booked) > 0:
                    fills[str(oid)] = (qty - int(booked), px, kind)
                else:
                    del fills[str(oid)]
        if ok:
            bracket["active"] = False
            bracket["state"] = "canceled"
        filled_qty = sum(qty for qty, _px, _kind in fills.values())
        priced = [(qty, px) for qty, px, _kind in fills.values() if px is not None]
        priced_qty = sum(qty for qty, _px in priced)
        fill_price = sum(qty * px for qty, px in priced) / priced_qty if priced_qty > 0 else None
        fill_reason = max(fills.values(), key=lambda fill: fill[0])[2] if fills else None
        return BracketCancel(ok, ",".join(messages), int(filled_qty), fill_price, fill_reason)

    def _finalize_bracket_protection(self, result: OrderResult, request: OrderRequest, side: Side,
                                     stop_price: float, target_price: float | None,
                                     parent_order_id: str, payload: dict[str, Any] | None,
                                     post_fill_levels: PostFillLevels) -> OrderResult:
        """Attach bracket state to an entry result, and make the protection
        match the fill.

        ``stop_price`` / ``target_price`` are the signal's levels: the
        children were submitted at them, for the requested quantity, before
        the fill was known. Once it is:

        - a partial fill resizes the children to the shares held;
        - ``post_fill_levels(fill)`` gives the levels the fill leaves the
          trade with: the signal's, or the default-distance fallback when the
          fill went through one of them (the entry gatekeeper's
          ``_post_fill_levels``, which books the position at the same
          levels). Resting children are replaced onto the fallback, and
          protection placed afresh (no children materialised) goes in at it.
          Until 2026-09-28 the children kept the signal's stop, on the wrong
          side of a fill through it, until a ``replace`` sync moved them a
          cycle later (never, in ``static`` mode), and fresh protection was
          placed at it too.

        The bracket records the levels that rest: a replace that fails leaves
        the signal's, and the engine enforces the fallback stop itself.
        """
        children = extract_bracket_children(payload)
        direction = self._bracket_round_direction(side)
        bracket: dict[str, Any] = {
            "parent_order_id": str(parent_order_id),
            "sync_mode": str(self.config.execution.bracket_sync_mode),
            "legs": str(self.config.execution.bracket_legs),
            "session": str(request.session),
            "stop_price": self._round_equity_price(stop_price, direction),
            "target_price": (
                self._round_equity_price(target_price, direction)
                if (self.bracket_carries_target() and target_price is not None) else None
            ),
            **children,
        }
        filled_qty = int(result.filled_qty or 0)
        if filled_qty <= 0:
            result.bracket = {**bracket, "active": False, "qty": 0, "state": "no_fill"}
            return result
        bracket["qty"] = filled_qty
        # Read as the entry gatekeeper reads it, so both derive the same levels.
        entry_price = safe_float(result.fill_price, float(request.price or 0.0), finite=True)
        protect_stop, protect_target, levels_reason = post_fill_levels(entry_price)
        initial_risk = abs(entry_price - float(protect_stop))
        if levels_reason is not None:
            LOG.warning(
                "Bracket entry %s filled at %.4f through its levels (%s): protecting at the post-fill "
                "fallback stop %.4f instead of the signal's %.4f",
                request.symbol, entry_price, levels_reason, protect_stop, stop_price,
            )

        if not children["child_order_ids"]:
            # Parent filled but no children are resting -- either the broker
            # never materialised them, or they were cancelled alongside the
            # parent on the partial-fill path. The position is OPEN AND
            # UNPROTECTED, so submit standalone protection immediately.
            bracket["stop_price"] = self._round_equity_price(protect_stop, direction)
            bracket["target_price"] = (
                self._round_equity_price(protect_target, direction)
                if (self.bracket_carries_target() and protect_target is not None) else None
            )
            replacement = self.submit_protective_oco(
                request.symbol, filled_qty, side, float(bracket["stop_price"]), protect_target, request.session,
                initial_risk=initial_risk,
            )
            if replacement.ok:
                bracket.update(replacement.bracket or {})
                bracket["protective_order_id"] = replacement.order_id
                bracket["active"] = True
                bracket["state"] = "standalone_oco"
            else:
                bracket["active"] = False
                bracket["state"] = "unprotected"
                LOG.error(
                    "Bracket entry %s filled qty=%s but protection could not be established (%s) -- "
                    "position is open with NO resting stop; engine-side management is the only guard",
                    request.symbol, filled_qty, replacement.message,
                )
            result.bracket = bracket
            return result

        if filled_qty != int(request.qty):
            resized, msg = self.resize_bracket_children(
                bracket, request.symbol, side, filled_qty, request.session, initial_risk=initial_risk,
            )
            bracket["active"] = True
            bracket["state"] = "resized" if resized else "qty_mismatch"
            if not resized:
                LOG.error(
                    "Bracket entry %s filled qty=%s of requested %s but children could not be resized (%s) -- "
                    "resting exit is OVERSIZED and would flip the position on trigger",
                    request.symbol, filled_qty, request.qty, msg,
                )
        else:
            bracket["active"] = True
            bracket["state"] = "attached"
        if levels_reason is not None:
            self.sync_bracket_levels(bracket, request.symbol, side, filled_qty, protect_stop, protect_target,
                                     initial_risk=initial_risk)
            if bracket["stop_price"] != self._round_equity_price(protect_stop, direction):
                LOG.error(
                    "Bracket entry %s: its stop still rests at the signal's %s, not the post-fill %.4f the fill at "
                    "%.4f left the trade with; the engine enforces the post-fill stop itself",
                    request.symbol, bracket["stop_price"], protect_stop, entry_price,
                )
        result.bracket = bracket
        return result

    def _submit_live_bracket_entry(self, request: OrderRequest, *, side: Side, stop_price: float,
                                   target_price: float | None, post_fill_levels: PostFillLevels,
                                   data=None) -> OrderResult:
        """Submit the one-order bracket and reconcile the resting protection.

        No reprice loop: cancel/replace churn on a TRIGGER parent with live
        children is how brackets get orphaned, and an entry that needs several
        reprices has already left the level the signal was built on.
        """
        timeout_seconds = max(0.5, float(self.config.execution.entry_live_fill_timeout_seconds))
        poll_seconds = max(0.1, float(self.config.execution.entry_live_poll_seconds))
        spec = self.build_bracket_order(request, side=side, stop_price=stop_price, target_price=target_price)
        response = self._submit_live_order_spec(spec)
        status_code = getattr(response, "status_code", None)
        if not response_ok(response):
            return OrderResult(ok=False, order_id=None, raw=getattr(response, "text", spec),
                               message=f"bracket_status={status_code}", simulated=False)
        order_id = self._response_order_id(response)
        if not order_id:
            return OrderResult(ok=False, order_id=None, raw=getattr(response, "text", spec),
                               message="bracket_missing_order_id", simulated=False)
        payload, status = self._poll_equity_order(order_id, timeout_seconds, poll_seconds)
        if payload is not None and order_is_filled(payload):
            result = self._finalize_live_equity_entry_result(request, spec, payload, order_id, f"live_bracket_fill:{status}", data=data)
            return self._finalize_bracket_protection(result, request, side, stop_price, target_price, order_id, payload,
                                                    post_fill_levels)
        if (order_filled_qty(payload) or 0) > 0:
            # Partial fill: stop further shares arriving BEFORE sizing the
            # protection, so the resize target quantity cannot move underneath.
            cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
            latest = cancel_payload or payload
            result = self._finalize_live_equity_entry_result(request, spec, latest, order_id, f"live_bracket_partial_fill:{cancel_msg}", data=data)
            if not result.ok:
                return OrderResult(ok=False, order_id=order_id, raw=latest or spec,
                                   message=f"bracket_partial_fill_finalize_failed:{cancel_msg}", simulated=False,
                                   may_still_be_working=not cancel_ok)
            result.may_still_be_working = not cancel_ok
            return self._finalize_bracket_protection(result, request, side, stop_price, target_price, order_id, latest,
                                                    post_fill_levels)
        cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
        latest = cancel_payload or payload
        if latest is not None and order_is_filled(latest):
            result = self._finalize_live_equity_entry_result(request, spec, latest, order_id, f"live_bracket_fill_after_cancel:{cancel_msg}", data=data)
            return self._finalize_bracket_protection(result, request, side, stop_price, target_price, order_id, latest,
                                                    post_fill_levels)
        if (order_filled_qty(latest) or 0) > 0:
            result = self._finalize_live_equity_entry_result(request, spec, latest, order_id, f"live_bracket_partial_fill_after_cancel:{cancel_msg}", data=data)
            if result.ok:
                result.may_still_be_working = not cancel_ok
                return self._finalize_bracket_protection(result, request, side, stop_price, target_price, order_id,
                                                         latest, post_fill_levels)
        if not cancel_ok:
            return OrderResult(ok=False, order_id=order_id, raw=latest or spec,
                               message=f"bracket_unfilled_cancel_failed:{cancel_msg}", simulated=False,
                               may_still_be_working=True)
        return OrderResult(ok=False, order_id=order_id, raw=latest or spec, message="bracket_unfilled_canceled", simulated=False)

    def _vertical_market(self, metadata: dict[str, Any], data, refresh_quotes: bool = True) -> tuple[float, float, float] | None:
        spread_side = Side(metadata.get("spread_side", Side.LONG.value))
        long_symbol = str(metadata.get("long_leg_symbol") or "")
        short_symbol = str(metadata.get("short_leg_symbol") or "")
        if not long_symbol or not short_symbol:
            return None
        if spread_side == Side.LONG:
            first_symbol, second_symbol = long_symbol, short_symbol
            first_meta, second_meta = metadata.get("long_leg"), metadata.get("short_leg")
        else:
            first_symbol, second_symbol = short_symbol, long_symbol
            first_meta, second_meta = metadata.get("short_leg"), metadata.get("long_leg")
        if data is not None and refresh_quotes:
            data.fetch_quotes([first_symbol, second_symbol], force=True, min_force_interval_seconds=self._option_quote_force_cooldown_seconds(), source="execution:vertical_market")
        q1 = data.get_quote(first_symbol) if data else None
        q2 = data.get_quote(second_symbol) if data else None
        if not q1 or not q2:
            return None
        if data is not None and not data.quotes_are_fresh([first_symbol, second_symbol], self.config.options.max_quote_age_seconds):
            return None
        first_leg = contract_from_quote(first_symbol, q1, first_meta)
        second_leg = contract_from_quote(second_symbol, q2, second_meta)
        return vertical_price_bounds(first_leg, second_leg)

    def _single_option_market(self, metadata: dict[str, Any], data, refresh_quotes: bool = True):
        symbol = str(metadata.get("option_symbol") or "")
        if not symbol:
            return None
        if data is not None and refresh_quotes:
            data.fetch_quotes([symbol], force=True, min_force_interval_seconds=self._option_quote_force_cooldown_seconds(), source="execution:single_option_market")
        q = data.get_quote(symbol) if data else None
        if not q:
            return None
        if data is not None and not data.quotes_are_fresh([symbol], self.config.options.max_quote_age_seconds):
            return None
        contract = contract_from_quote(symbol, q, metadata.get("option_leg"))
        return single_option_price_bounds(contract)

    def _simulate_vertical_fill(
        self,
        spec: dict[str, Any],
        metadata: dict[str, Any],
        data,
        refresh_quotes: bool = True,
    ) -> OrderResult:
        """A dry-run fill: the limit, ``dry_run_replace_attempts`` reprices
        ``dry_run_step_frac`` of the way to the natural price, then the
        natural itself. That last step models the chase a live order makes:
        without it the 2-attempt 0.25 ramp never reached the fill threshold
        from a limit below mid (the market moved between the signal and the
        order), so a dry run took no entry (``dry_run_not_filled_debit``).
        Until 2026-09-26 the step sat behind an ``allow_natural_fill`` flag
        that every caller set."""
        market = self._vertical_market(metadata, data, refresh_quotes=refresh_quotes)
        if market is None:
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_missing_or_stale_quotes", simulated=True)
        bid, ask, mid = market
        limit = safe_float(spec.get("price"), 0.0, finite=True)
        if limit <= 0:
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_missing_limit_price", simulated=True)
        legs = spec.get("orderLegCollection") or []
        spec_qty = safe_int((legs[0] or {}).get("quantity"), 0) if legs else 0
        if spec_qty <= 0:
            spec_qty = safe_int(safe_float(metadata.get("qty"), finite=True) or 1)
        attempts = max(0, int(self.config.options.dry_run_replace_attempts))
        step_frac = min(0.95, max(0.05, float(self.config.options.dry_run_step_frac)))
        order_type = str(spec.get("orderType") or "").upper()
        prices = [round(limit, 2)]
        cur = limit
        if order_type == "NET_DEBIT":
            natural = max(cur, ask if ask > 0 else mid)
            threshold = mid + (max(0.0, natural - mid) * step_frac)
            for _ in range(attempts):
                cur = round(min(natural, cur + ((natural - cur) * step_frac)), 2)
                if cur not in prices:
                    prices.append(cur)
            if natural not in prices:
                prices.append(round(natural, 2))
            for idx, px in enumerate(prices):
                if px >= threshold or px >= natural:
                    sim = copy.deepcopy(spec)
                    sim["price"] = f"{px:.2f}"
                    suffix = "_natural" if abs(px - natural) < 0.005 else ""
                    return OrderResult(ok=True, order_id=None, raw=sim, message=f"dry_run_fill_attempt_{idx}{suffix}", fill_price=px * 100.0, filled_qty=spec_qty, simulated=True)
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_not_filled_debit", simulated=True)
        if order_type == "NET_CREDIT":
            natural = min(cur, bid if bid > 0 else mid)
            threshold = mid - (max(0.0, mid - natural) * step_frac)
            for _ in range(attempts):
                cur = round(max(natural, cur - ((cur - natural) * step_frac)), 2)
                if cur not in prices:
                    prices.append(cur)
            if natural not in prices:
                prices.append(round(natural, 2))
            for idx, px in enumerate(prices):
                if px <= threshold or px <= natural:
                    sim = copy.deepcopy(spec)
                    sim["price"] = f"{px:.2f}"
                    suffix = "_natural" if abs(px - natural) < 0.005 else ""
                    return OrderResult(ok=True, order_id=None, raw=sim, message=f"dry_run_fill_attempt_{idx}{suffix}", fill_price=px * 100.0, filled_qty=spec_qty, simulated=True)
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_not_filled_credit", simulated=True)
        return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_unsupported_order_type", simulated=True)

    def _simulate_single_option_fill(
        self,
        spec: dict[str, Any],
        metadata: dict[str, Any],
        data,
        refresh_quotes: bool = True,
    ) -> OrderResult:
        """The single-option ladder, as ``_simulate_vertical_fill``'s."""
        market = self._single_option_market(metadata, data, refresh_quotes=refresh_quotes)
        if market is None:
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_missing_or_stale_quotes", simulated=True)
        bid, ask, mid = market
        limit = safe_float(spec.get("price"), 0.0, finite=True)
        if limit <= 0:
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_missing_limit_price", simulated=True)
        legs = spec.get("orderLegCollection") or []
        instruction = str((legs[0] or {}).get("instruction") or "") if legs else ""
        buy_side = instruction in {OrderIntent.BUY_TO_OPEN.value, OrderIntent.BUY_TO_CLOSE.value}
        spec_qty = safe_int((legs[0] or {}).get("quantity"), 0) if legs else 0
        if spec_qty <= 0:
            spec_qty = safe_int(safe_float(metadata.get("qty"), finite=True) or 1)
        attempts = max(0, int(self.config.options.dry_run_replace_attempts))
        step_frac = min(0.95, max(0.05, float(self.config.options.dry_run_step_frac)))
        prices = [round(limit, 2)]
        cur = limit
        if buy_side:
            natural = max(cur, ask if ask > 0 else mid)
            threshold = mid + (max(0.0, natural - mid) * step_frac)
            for _ in range(attempts):
                cur = round(min(natural, cur + ((natural - cur) * step_frac)), 2)
                if cur not in prices:
                    prices.append(cur)
            if natural not in prices:
                prices.append(round(natural, 2))
            for idx, px in enumerate(prices):
                if px >= threshold or px >= natural:
                    sim = copy.deepcopy(spec)
                    sim["price"] = f"{px:.2f}"
                    suffix = "_natural" if abs(px - natural) < 0.005 else ""
                    return OrderResult(ok=True, order_id=None, raw=sim, message=f"dry_run_fill_attempt_{idx}{suffix}", fill_price=px * 100.0, filled_qty=spec_qty, simulated=True)
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_not_filled_long_option", simulated=True)
        natural = min(cur, bid if bid > 0 else mid)
        threshold = mid - (max(0.0, mid - natural) * step_frac)
        for _ in range(attempts):
            cur = round(max(natural, cur - ((cur - natural) * step_frac)), 2)
            if cur not in prices:
                prices.append(cur)
        if natural not in prices:
            prices.append(round(natural, 2))
        for idx, px in enumerate(prices):
            if px <= threshold or px <= natural:
                sim = copy.deepcopy(spec)
                sim["price"] = f"{px:.2f}"
                suffix = "_natural" if abs(px - natural) < 0.005 else ""
                return OrderResult(ok=True, order_id=None, raw=sim, message=f"dry_run_fill_attempt_{idx}{suffix}", fill_price=px * 100.0, filled_qty=spec_qty, simulated=True)
        return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_not_filled_long_option_exit", simulated=True)

    def submit_option_vertical(self, spec: dict[str, Any], metadata: dict[str, Any], data=None) -> OrderResult:
        if self.config.schwab.dry_run:
            return self._simulate_vertical_fill(spec, metadata, data)
        if self._vertical_market(metadata, data, refresh_quotes=True) is None:
            return OrderResult(ok=False, order_id=None, raw=spec, message="live_missing_or_stale_quotes", simulated=False)
        return self._submit_live_single_order_with_poll(spec, cancel_on_timeout=True, price_scale=100.0,
                                                        on_order_sent=None)

    def submit_option_single(self, spec: dict[str, Any], metadata: dict[str, Any], data=None) -> OrderResult:
        if self.config.schwab.dry_run:
            return self._simulate_single_option_fill(spec, metadata, data)
        if self._single_option_market(metadata, data, refresh_quotes=True) is None:
            return OrderResult(ok=False, order_id=None, raw=spec, message="live_missing_or_stale_quotes", simulated=False)
        return self._submit_live_single_order_with_poll(spec, cancel_on_timeout=True, price_scale=100.0,
                                                        on_order_sent=None)

    def can_close_position_now(self, position: Position, ts=None) -> bool:
        if is_option_asset(position.metadata):
            return self._is_regular_options_session(ts)
        return self._equity_session(ts) is not None

    def close_position(self, position: Position, qty: int, data=None, market_snapshot: Any | None = None,
                       *, reprice_deadline: float, on_order_sent: Callable[[str], None]) -> OrderResult:
        """Close ``qty`` units of ``position`` -- all of it, or a scale-out slice.

        ``qty`` is required: until 2026-09-24 this always sent ``position.qty``,
        so a partial exit could not be expressed at all. It is the caller's
        sized request, already clamped to the position. ``reprice_deadline``
        is the management pass's (``exit_reprice_deadline``): an equity
        exit's live re-sends stop there; an option's close sends one order.
        ``on_order_sent`` gets each live order's id before the order is
        polled (the position manager records it as the position's working
        exit, 2026-09-29).
        """
        if not 1 <= int(qty) <= int(position.qty):
            raise ValueError(f"close_position qty {qty!r} outside 1..{position.qty} for {position.symbol}")
        asset_type = asset_type_of(position.metadata)
        if asset_type == ASSET_TYPE_OPTION_VERTICAL:
            first_symbol = str(position.metadata.get("long_leg_symbol") or "")
            second_symbol = str(position.metadata.get("short_leg_symbol") or "")
            if data and first_symbol and second_symbol:
                data.fetch_quotes([first_symbol, second_symbol], force=True, min_force_interval_seconds=self._option_quote_force_cooldown_seconds(), source="execution:close_vertical")
            q1 = data.get_quote(first_symbol) if data and first_symbol else None
            q2 = data.get_quote(second_symbol) if data and second_symbol else None
            if data and (not q1 or not q2 or not data.quotes_are_fresh([first_symbol, second_symbol], self.config.options.max_quote_age_seconds)):
                return OrderResult(ok=False, order_id=None, raw=None, message="close_missing_or_stale_quotes", simulated=self.config.schwab.dry_run)
            limit_price = close_limit_price_from_metadata(position.metadata, q1, q2, mode=self.config.options.vertical_limit_mode)
            spec = build_vertical_close_order(position.metadata, int(qty), limit_price=limit_price)
            if self.config.schwab.dry_run:
                return self._simulate_vertical_fill(spec, position.metadata, data, refresh_quotes=False)
            return self._submit_live_single_order_with_poll(spec, cancel_on_timeout=True, price_scale=100.0,
                                                            on_order_sent=on_order_sent)
        if asset_type == ASSET_TYPE_OPTION_SINGLE:
            symbol = str(position.metadata.get("option_symbol") or "")
            if data and symbol:
                data.fetch_quotes([symbol], force=True, min_force_interval_seconds=self._option_quote_force_cooldown_seconds(), source="execution:close_single")
            q = data.get_quote(symbol) if data and symbol else None
            if data and (not q or not data.quotes_are_fresh([symbol], self.config.options.max_quote_age_seconds)):
                return OrderResult(ok=False, order_id=None, raw=None, message="close_missing_or_stale_quotes", simulated=self.config.schwab.dry_run)
            limit_price = close_single_option_limit_from_metadata(position.metadata, q, mode=self.config.options.option_limit_mode)
            spec = build_single_option_close_order(position.metadata, int(qty), limit_price=limit_price)
            if self.config.schwab.dry_run:
                return self._simulate_single_option_fill(spec, position.metadata, data, refresh_quotes=False)
            return self._submit_live_single_order_with_poll(spec, cancel_on_timeout=True, price_scale=100.0,
                                                            on_order_sent=on_order_sent)
        intent = self.order_intent_for_exit(position.side)
        return self.submit_equity_exit(position.symbol, int(qty), intent, data=data, market_snapshot=market_snapshot,
                                       reprice_deadline=reprice_deadline, on_order_sent=on_order_sent)

    @staticmethod
    def _build_order(request: OrderRequest) -> dict[str, Any]:
        order: dict[str, Any] = {
            "orderType": request.order_type,
            "session": request.session,
            "duration": request.duration,
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": request.intent.value,
                    "quantity": request.qty,
                    "instrument": {"symbol": request.symbol, "assetType": ASSET_TYPE_EQUITY},
                }
            ],
        }
        if request.price is not None:
            order["price"] = f"{request.price:.4f}"
        return order
