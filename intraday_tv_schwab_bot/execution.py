# SPDX-License-Identifier: MIT
from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass
import time as time_module
from typing import Any

from schwabdev import Client

from .config import BotConfig
from .models import (
    ASSET_TYPE_EQUITY,
    ASSET_TYPE_OPTION_SINGLE,
    ASSET_TYPE_OPTION_VERTICAL,
    OPTION_ASSET_TYPES,
    OrderIntent,
    OrderResult,
    Position,
    Side,
)
from .options_mode import build_single_option_close_order, build_vertical_close_order, close_limit_price_from_metadata, close_single_option_limit_from_metadata, contract_from_quote, single_option_price_bounds, vertical_price_bounds
from .utils import call_schwab_client, classify_equity_session, equity_session_state, is_regular_equity_session

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    qty: int
    intent: OrderIntent
    order_type: str = "MARKET"
    price: float | None = None
    session: str = "NORMAL"
    duration: str = "DAY"


class SchwabExecutor:
    _EQUITY_WORKING_STATUSES = {
        "AWAITING_PARENT_ORDER",
        "AWAITING_CONDITION",
        "AWAITING_MANUAL_REVIEW",
        "ACCEPTED",
        "AWAITING_UR_OUT",
        "PENDING_ACTIVATION",
        "PENDING_ACKNOWLEDGEMENT",
        "PENDING_RECALL",
        "QUEUED",
        "WORKING",
        "OPEN",
        "LIVE",
        "PARTIALLY_FILLED",
    }
    _EQUITY_TERMINAL_FAILURE_STATUSES = {
        "CANCELED",
        "CANCELLED",
        "EXPIRED",
        "REJECTED",
        "REPLACED",
    }

    @staticmethod
    def _is_regular_options_session(ts=None) -> bool:
        return is_regular_equity_session(ts)


    def __init__(self, client: Client, config: BotConfig):
        self.client = client
        self.config = config
        self.account_hash = config.schwab.account_hash or self._resolve_account_hash()

    def _resolve_account_hash(self) -> str:
        response = call_schwab_client(self.client, "linked_accounts")
        payload = response.json()
        if isinstance(payload, list) and payload:
            for row in payload:
                for key in ("hashValue", "accountHash", "encryptedAccountNumber"):
                    if row.get(key):
                        return str(row[key])
        raise RuntimeError("Could not resolve account hash from linked_accounts()")

    def submit(self, request: OrderRequest) -> OrderResult:
        spec = self._build_order(request)
        return self.submit_raw(spec)

    def submit_raw(self, spec: dict[str, Any]) -> OrderResult:
        if self.config.schwab.dry_run:
            LOG.info("DRY RUN order: %s", spec)
            return OrderResult(ok=True, order_id=None, raw=spec, message="dry_run", simulated=True)
        response = call_schwab_client(self.client, "place_order", self.account_hash, spec)
        ok = 200 <= response.status_code < 300
        order_id = self._response_order_id(response)
        if not ok:
            LOG.warning("Order submission failed status=%s spec=%s", response.status_code, spec)
        return OrderResult(ok=ok, order_id=order_id, raw=response.text, message=f"status={response.status_code}")

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
    def _quote_number(quote: dict[str, Any] | None, *keys: str) -> float | None:
        if not quote:
            return None
        for key in keys:
            value = quote.get(key)
            try:
                number = float(value)
            except Exception:
                continue
            if number > 0:
                return number
        return None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        # Coerce NaN to None via the `number == number` idiom — `float(nan)`
        # does not raise, and NaN silently fails downstream comparisons.
        try:
            if value is None:
                return None
            number = float(value)
        except Exception:
            return None
        return number if number == number else None

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            if value is None:
                return None
            if isinstance(value, bool):
                return int(value)
            return int(float(value))
        except Exception:
            return None

    @staticmethod
    def _response_order_id(response) -> str | None:
        location = getattr(response, "headers", {}).get("Location", "") or ""
        order_id = str(location).split("/")[-1].strip()
        return order_id or None

    @classmethod
    def _equity_order_status(cls, payload: dict[str, Any] | None) -> str:
        if not isinstance(payload, dict):
            return "UNKNOWN"
        status = str(payload.get("status") or "").upper().strip()
        return status or "UNKNOWN"

    @classmethod
    def _equity_order_remaining_qty(cls, payload: dict[str, Any] | None) -> int | None:
        if not isinstance(payload, dict):
            return None
        for key in ("remainingQuantity", "remainingQty", "leavesQuantity"):
            value = cls._safe_int(payload.get(key))
            if value is not None:
                return max(0, value)
        return None

    @classmethod
    def _equity_order_filled_qty(cls, payload: dict[str, Any] | None) -> int | None:
        if not isinstance(payload, dict):
            return None
        for key in ("filledQuantity", "filledQty", "cumulativeQuantity", "executedQuantity"):
            value = cls._safe_int(payload.get(key))
            if value is not None:
                return max(0, value)
        total = 0
        found = False
        for activity in payload.get("orderActivityCollection") or []:
            if not isinstance(activity, dict):
                continue
            for leg in activity.get("executionLegs") or []:
                if not isinstance(leg, dict):
                    continue
                qty = cls._safe_int(leg.get("quantity"))
                if qty is None:
                    continue
                total += max(0, qty)
                found = True
        return total if found else None

    @classmethod
    def _equity_order_fill_price(cls, payload: dict[str, Any] | None) -> float | None:
        if not isinstance(payload, dict):
            return None
        weighted_notional = 0.0
        weighted_qty = 0.0
        for activity in payload.get("orderActivityCollection") or []:
            if not isinstance(activity, dict):
                continue
            for leg in activity.get("executionLegs") or []:
                if not isinstance(leg, dict):
                    continue
                px = cls._safe_float(leg.get("price"))
                qty = cls._safe_float(leg.get("quantity"))
                if px is None or qty is None or px <= 0 or qty <= 0:
                    continue
                weighted_notional += px * qty
                weighted_qty += qty
        if weighted_qty > 0:
            return weighted_notional / weighted_qty
        for key in ("price", "filledPrice", "averagePrice"):
            px = cls._safe_float(payload.get(key))
            if px is not None and px > 0:
                return px
        return None

    @classmethod
    def _equity_order_is_filled(cls, payload: dict[str, Any] | None) -> bool:
        status = cls._equity_order_status(payload)
        if status == "FILLED":
            return True
        remaining = cls._equity_order_remaining_qty(payload)
        filled_qty = cls._equity_order_filled_qty(payload)
        return remaining == 0 and (filled_qty or 0) > 0


    @classmethod
    def _equity_order_is_terminal_failure(cls, payload: dict[str, Any] | None) -> bool:
        status = cls._equity_order_status(payload)
        return status in cls._EQUITY_TERMINAL_FAILURE_STATUSES


    def _equity_session(self, ts=None) -> str | None:
        return classify_equity_session(
            ts,
            extended_hours_enabled=bool(self.config.execution.extended_hours_enabled),
        )

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
        bid = self._quote_number(quote, "bid", "bidPrice")
        ask = self._quote_number(quote, "ask", "askPrice")
        last = self._quote_number(quote, "last", "lastPrice", "mark", "markPrice", "close", "closePrice")
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
            bid = self._quote_number({"v": market_snapshot[0]}, "v")
            ask = self._quote_number({"v": market_snapshot[1]}, "v")
            last = self._quote_number({"v": market_snapshot[2]}, "v")
            return bid, ask, last
        if isinstance(market_snapshot, dict):
            bid = self._quote_number(market_snapshot, "bid", "bidPrice")
            ask = self._quote_number(market_snapshot, "ask", "askPrice")
            last = self._quote_number(market_snapshot, "last", "lastPrice", "mark", "markPrice", "close", "closePrice", "mid")
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
        buffer = self._marketable_limit_buffer(bid, ask) * max(1.0, float(buffer_mult))
        buy_side = intent in {OrderIntent.BUY, OrderIntent.BUY_TO_COVER}
        if buy_side:
            reference = ask if ask is not None else last
            if reference is None or not math.isfinite(reference) or reference <= 0:
                return None
            price = round(reference + buffer, 4)
            return price if math.isfinite(price) else None
        reference = bid if bid is not None else last
        if reference is None or not math.isfinite(reference) or reference <= 0:
            return None
        price = round(max(0.01, reference - buffer), 4)
        return price if math.isfinite(price) else None

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
        if not (200 <= getattr(response, 'status_code', 0) < 300):
            return None, f"order_details_status={getattr(response, 'status_code', None)}"
        try:
            payload = response.json()
        except Exception as exc:
            return None, f"order_details_json_error:{exc}"
        return payload, self._equity_order_status(payload)

    def _poll_equity_order(self, order_id: str, timeout_seconds: float, poll_seconds: float) -> tuple[dict[str, Any] | None, str]:
        deadline = time_module.monotonic() + max(0.0, timeout_seconds)
        while True:
            payload, status = self._equity_order_details(order_id)
            if payload is None:
                return payload, status
            if self._equity_order_is_filled(payload) or self._equity_order_is_terminal_failure(payload):
                return payload, status
            if time_module.monotonic() >= deadline:
                return payload, status
            time_module.sleep(max(0.05, poll_seconds))

    def _cancel_live_equity_order(self, order_id: str) -> tuple[bool, str, dict[str, Any] | None]:
        timeout_seconds = max(0.5, min(5.0, float(self.config.execution.entry_live_fill_timeout_seconds)))
        poll_seconds = max(0.1, float(self.config.execution.entry_live_poll_seconds))

        def _post_cancel_check(prefix: str) -> tuple[bool, str, dict[str, Any] | None]:
            payload, status = self._poll_equity_order(order_id, timeout_seconds, poll_seconds)
            if payload is None:
                return False, f"{prefix}:{status}", payload
            if self._equity_order_is_terminal_failure(payload):
                return True, f"{prefix}:{status}", payload
            if self._equity_order_is_filled(payload):
                return True, f"{prefix}:{status}", payload
            filled_qty = self._equity_order_filled_qty(payload) or 0
            if filled_qty > 0:
                return True, f"{prefix}:{status}", payload
            return False, f"{prefix}_unconfirmed:{status}", payload

        try:
            response = call_schwab_client(self.client, "cancel_order", self.account_hash, order_id)
        except Exception as exc:
            ok, msg, payload = _post_cancel_check("cancel_postcheck")
            if ok:
                return ok, msg, payload
            return False, f"cancel_error:{exc}", payload
        status_code = getattr(response, 'status_code', 0)
        if 200 <= status_code < 300:
            return _post_cancel_check(f"cancel_status={status_code}")
        ok, msg, payload = _post_cancel_check("cancel_postcheck")
        if ok:
            return ok, msg, payload
        return False, f"cancel_status={status_code}", payload

    def _build_repriced_equity_request(self, request: OrderRequest, data, attempt_index: int) -> OrderRequest | None:
        market = self._equity_market(request.symbol, data, refresh_quotes=True)
        if market is None:
            return None
        bid, ask, last = market
        step_frac = max(0.05, float(self.config.execution.entry_live_reprice_step_frac))
        buffer_mult = 1.0 + (attempt_index * step_frac)
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
        filled_qty = self._equity_order_filled_qty(payload)
        broker_fill_price = self._equity_order_fill_price(payload)
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
        filled_qty = self._equity_order_filled_qty(payload)
        fill_price = self._equity_order_fill_price(payload)
        if fill_price is not None and price_scale != 1.0:
            fill_price *= float(price_scale)
        return OrderResult(ok=(filled_qty or 0) > 0, order_id=order_id, raw=payload or spec, message=message, fill_price=fill_price, filled_qty=filled_qty, simulated=False)

    def _submit_live_single_order_with_poll(
        self,
        spec: dict[str, Any],
        *,
        cancel_on_timeout: bool,
        price_scale: float = 1.0,
    ) -> OrderResult:
        timeout_seconds = max(0.5, float(self.config.execution.entry_live_fill_timeout_seconds))
        poll_seconds = max(0.1, float(self.config.execution.entry_live_poll_seconds))
        response = self._submit_live_order_spec(spec)
        status_code = getattr(response, 'status_code', 0)
        if not (200 <= status_code < 300):
            return OrderResult(ok=False, order_id=None, raw=getattr(response, 'text', spec), message=f"status={status_code}", simulated=False)
        order_id = self._response_order_id(response)
        if not order_id:
            return OrderResult(ok=False, order_id=None, raw=getattr(response, 'text', spec), message="live_missing_order_id", simulated=False)
        payload, status = self._poll_equity_order(order_id, timeout_seconds, poll_seconds)
        if payload is not None and self._equity_order_is_filled(payload):
            return self._finalize_live_polled_order_result(spec, payload, order_id, f"live_fill:{status}", price_scale=price_scale)
        filled_qty = self._equity_order_filled_qty(payload) or 0
        if filled_qty > 0:
            cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
            latest_payload = cancel_payload or payload
            result = self._finalize_live_polled_order_result(spec, latest_payload, order_id, f"live_partial_fill:{cancel_msg}", price_scale=price_scale)
            if result.ok:
                return result
            return OrderResult(ok=False, order_id=order_id, raw=latest_payload or spec, message=f"partial_fill_finalize_failed:{cancel_msg}", simulated=False)
        if not cancel_on_timeout:
            return OrderResult(ok=False, order_id=order_id, raw=payload or spec, message=f"live_unfilled_timeout:{status}", simulated=False)
        cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
        latest_payload = cancel_payload or payload
        if latest_payload is not None and self._equity_order_is_filled(latest_payload):
            return self._finalize_live_polled_order_result(spec, latest_payload, order_id, f"live_fill_after_cancel:{cancel_msg}", price_scale=price_scale)
        latest_filled_qty = self._equity_order_filled_qty(latest_payload) or 0
        if latest_filled_qty > 0:
            result = self._finalize_live_polled_order_result(spec, latest_payload, order_id, f"live_partial_fill_after_cancel:{cancel_msg}", price_scale=price_scale)
            if result.ok:
                return result
            return OrderResult(ok=False, order_id=order_id, raw=latest_payload or spec, message=f"partial_fill_after_cancel_finalize_failed:{cancel_msg}", simulated=False)
        if not cancel_ok:
            return OrderResult(ok=False, order_id=order_id, raw=latest_payload or spec, message=f"live_unfilled_cancel_failed:{cancel_msg}", simulated=False)
        return OrderResult(ok=False, order_id=order_id, raw=latest_payload or spec, message="live_unfilled_canceled", simulated=False)

    def _submit_live_equity_entry_with_reprice(self, initial_request: OrderRequest, data=None) -> OrderResult:
        timeout_seconds = max(0.5, float(self.config.execution.entry_live_fill_timeout_seconds))
        poll_seconds = max(0.1, float(self.config.execution.entry_live_poll_seconds))
        reprice_attempts = max(0, int(self.config.execution.entry_live_reprice_attempts))
        current_request = initial_request
        current_spec = self._build_order(current_request)
        for attempt in range(reprice_attempts + 1):
            response = self._submit_live_order_spec(current_spec)
            status_code = getattr(response, 'status_code', 0)
            if not (200 <= status_code < 300):
                return OrderResult(ok=False, order_id=None, raw=getattr(response, 'text', current_spec), message=f"status={status_code}", simulated=False)
            order_id = self._response_order_id(response)
            if not order_id:
                return OrderResult(ok=False, order_id=None, raw=getattr(response, 'text', current_spec), message="live_missing_order_id", simulated=False)
            payload, _ = self._poll_equity_order(order_id, timeout_seconds, poll_seconds)
            if self._equity_order_is_filled(payload):
                return self._finalize_live_equity_entry_result(current_request, current_spec, payload, order_id, f"live_fill_attempt_{attempt}", data=data)
            filled_qty = self._equity_order_filled_qty(payload) or 0
            if filled_qty > 0:
                cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
                latest_payload = cancel_payload or payload
                result = self._finalize_live_equity_entry_result(current_request, current_spec, latest_payload, order_id, f"live_partial_fill_attempt_{attempt}:{cancel_msg}", data=data)
                if not result.ok:
                    return OrderResult(ok=False, order_id=order_id, raw=latest_payload or current_spec, message=f"partial_fill_finalize_failed:{cancel_msg}", simulated=False)
                return result
            if attempt >= reprice_attempts:
                cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
                latest_payload = cancel_payload or payload
                if latest_payload is not None and self._equity_order_is_filled(latest_payload):
                    return self._finalize_live_equity_entry_result(current_request, current_spec, latest_payload, order_id, f"live_fill_after_cancel_attempt_{attempt}:{cancel_msg}", data=data)
                latest_filled_qty = self._equity_order_filled_qty(latest_payload) or 0
                if latest_filled_qty > 0:
                    result = self._finalize_live_equity_entry_result(current_request, current_spec, latest_payload, order_id, f"live_partial_fill_after_cancel_attempt_{attempt}:{cancel_msg}", data=data)
                    if result.ok:
                        return result
                if not cancel_ok:
                    return OrderResult(ok=False, order_id=order_id, raw=latest_payload or current_spec, message=f"live_unfilled_cancel_failed:{cancel_msg}", simulated=False)
                return OrderResult(ok=False, order_id=order_id, raw=latest_payload or current_spec, message="live_unfilled_canceled", simulated=False)
            cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
            latest_payload = cancel_payload or payload
            if latest_payload is not None and self._equity_order_is_filled(latest_payload):
                return self._finalize_live_equity_entry_result(current_request, current_spec, latest_payload, order_id, f"live_fill_after_reprice_cancel_attempt_{attempt}:{cancel_msg}", data=data)
            latest_filled_qty = self._equity_order_filled_qty(latest_payload) or 0
            if latest_filled_qty > 0:
                result = self._finalize_live_equity_entry_result(current_request, current_spec, latest_payload, order_id, f"live_partial_fill_after_reprice_cancel_attempt_{attempt}:{cancel_msg}", data=data)
                if result.ok:
                    return result
            if not cancel_ok:
                return OrderResult(ok=False, order_id=order_id, raw=latest_payload or current_spec, message=f"live_reprice_cancel_failed:{cancel_msg}", simulated=False)
            next_request = self._build_repriced_equity_request(current_request, data, attempt_index=attempt + 1)
            if next_request is None:
                return OrderResult(ok=False, order_id=order_id, raw=payload or current_spec, message="live_reprice_missing_or_stale_quotes", simulated=False)
            current_request = next_request
            current_spec = self._build_order(current_request)
        return OrderResult(ok=False, order_id=None, raw=current_spec, message="live_reprice_exhausted", simulated=False)

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

    def submit_equity_entry(self, symbol: str, qty: int, intent: OrderIntent, data=None, market_snapshot: Any | None = None) -> OrderResult:
        if not str(symbol or "").strip():
            return OrderResult(ok=False, order_id=None, raw=None, message="invalid_symbol", simulated=self.config.schwab.dry_run)
        if int(qty) <= 0:
            return OrderResult(ok=False, order_id=None, raw=None, message="invalid_qty", simulated=self.config.schwab.dry_run)
        session = self._equity_session()
        if session is None:
            return OrderResult(ok=False, order_id=None, raw=None, message=self._equity_session_blackout_reason(), simulated=self.config.schwab.dry_run)
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
            return self._simulate_equity_fill(request, data, refresh_quotes=False, market_snapshot=market)
        return self._submit_live_equity_entry_with_reprice(request, data=data)

    def submit_equity_exit(self, symbol: str, qty: int, intent: OrderIntent, data=None, market_snapshot: Any | None = None) -> OrderResult:
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
            return self._submit_live_single_order_with_poll(self._build_order(request), cancel_on_timeout=False, price_scale=1.0)
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
        return self._submit_live_single_order_with_poll(self._build_order(request), cancel_on_timeout=True, price_scale=1.0)


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
        allow_natural_fill: bool = False,
    ) -> OrderResult:
        market = self._vertical_market(metadata, data, refresh_quotes=refresh_quotes)
        if market is None:
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_missing_or_stale_quotes", simulated=True)
        bid, ask, mid = market
        limit = float(spec.get("price") or 0.0)
        if limit <= 0:
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_missing_limit_price", simulated=True)
        legs = spec.get("orderLegCollection") or []
        spec_qty = int((legs[0] or {}).get("quantity") or 0) if legs else 0
        if spec_qty <= 0:
            spec_qty = int(metadata.get("qty", 1) or 1)
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
            if allow_natural_fill and natural not in prices:
                prices.append(round(natural, 2))
            for idx, px in enumerate(prices):
                if px >= threshold or px >= natural:
                    sim = copy.deepcopy(spec)
                    sim["price"] = f"{px:.2f}"
                    suffix = "_natural" if allow_natural_fill and abs(px - natural) < 0.005 else ""
                    return OrderResult(ok=True, order_id=None, raw=sim, message=f"dry_run_fill_attempt_{idx}{suffix}", fill_price=px * 100.0, filled_qty=spec_qty, simulated=True)
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_not_filled_debit", simulated=True)
        if order_type == "NET_CREDIT":
            natural = min(cur, bid if bid > 0 else mid)
            threshold = mid - (max(0.0, mid - natural) * step_frac)
            for _ in range(attempts):
                cur = round(max(natural, cur - ((cur - natural) * step_frac)), 2)
                if cur not in prices:
                    prices.append(cur)
            if allow_natural_fill and natural not in prices:
                prices.append(round(natural, 2))
            for idx, px in enumerate(prices):
                if px <= threshold or px <= natural:
                    sim = copy.deepcopy(spec)
                    sim["price"] = f"{px:.2f}"
                    suffix = "_natural" if allow_natural_fill and abs(px - natural) < 0.005 else ""
                    return OrderResult(ok=True, order_id=None, raw=sim, message=f"dry_run_fill_attempt_{idx}{suffix}", fill_price=px * 100.0, filled_qty=spec_qty, simulated=True)
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_not_filled_credit", simulated=True)
        return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_unsupported_order_type", simulated=True)

    def _simulate_single_option_fill(
        self,
        spec: dict[str, Any],
        metadata: dict[str, Any],
        data,
        refresh_quotes: bool = True,
        allow_natural_fill: bool = False,
    ) -> OrderResult:
        market = self._single_option_market(metadata, data, refresh_quotes=refresh_quotes)
        if market is None:
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_missing_or_stale_quotes", simulated=True)
        bid, ask, mid = market
        limit = float(spec.get("price") or 0.0)
        if limit <= 0:
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_missing_limit_price", simulated=True)
        legs = spec.get("orderLegCollection") or []
        instruction = str((legs[0] or {}).get("instruction") or "") if legs else ""
        buy_side = instruction in {OrderIntent.BUY_TO_OPEN.value, OrderIntent.BUY_TO_CLOSE.value}
        spec_qty = int((legs[0] or {}).get("quantity") or 0) if legs else 0
        if spec_qty <= 0:
            spec_qty = int(metadata.get("qty", 1) or 1)
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
            if allow_natural_fill and natural not in prices:
                prices.append(round(natural, 2))
            for idx, px in enumerate(prices):
                if px >= threshold or px >= natural:
                    sim = copy.deepcopy(spec)
                    sim["price"] = f"{px:.2f}"
                    suffix = "_natural" if allow_natural_fill and abs(px - natural) < 0.005 else ""
                    return OrderResult(ok=True, order_id=None, raw=sim, message=f"dry_run_fill_attempt_{idx}{suffix}", fill_price=px * 100.0, filled_qty=spec_qty, simulated=True)
            return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_not_filled_long_option", simulated=True)
        natural = min(cur, bid if bid > 0 else mid)
        threshold = mid - (max(0.0, mid - natural) * step_frac)
        for _ in range(attempts):
            cur = round(max(natural, cur - ((cur - natural) * step_frac)), 2)
            if cur not in prices:
                prices.append(cur)
        if allow_natural_fill and natural not in prices:
            prices.append(round(natural, 2))
        for idx, px in enumerate(prices):
            if px <= threshold or px <= natural:
                sim = copy.deepcopy(spec)
                sim["price"] = f"{px:.2f}"
                suffix = "_natural" if allow_natural_fill and abs(px - natural) < 0.005 else ""
                return OrderResult(ok=True, order_id=None, raw=sim, message=f"dry_run_fill_attempt_{idx}{suffix}", fill_price=px * 100.0, filled_qty=spec_qty, simulated=True)
        return OrderResult(ok=False, order_id=None, raw=spec, message="dry_run_not_filled_long_option_exit", simulated=True)

    def submit_option_vertical(self, spec: dict[str, Any], metadata: dict[str, Any], data=None) -> OrderResult:
        if self.config.schwab.dry_run:
            # allow_natural_fill=True mirrors the close-position path so the
            # dry-run reprice loop can fall back to the natural (ask) price on
            # the final attempt. Without it, the 2-attempt step_frac=0.25 ramp
            # never reaches threshold when the limit starts below mid (e.g.,
            # after market movement between signal time and execution time),
            # causing "dry_run_not_filled_debit" and no entry in dry-run mode.
            return self._simulate_vertical_fill(spec, metadata, data, allow_natural_fill=True)
        if self._vertical_market(metadata, data, refresh_quotes=True) is None:
            return OrderResult(ok=False, order_id=None, raw=spec, message="live_missing_or_stale_quotes", simulated=False)
        return self._submit_live_single_order_with_poll(spec, cancel_on_timeout=True, price_scale=100.0)

    def submit_option_single(self, spec: dict[str, Any], metadata: dict[str, Any], data=None) -> OrderResult:
        if self.config.schwab.dry_run:
            # See submit_option_vertical for allow_natural_fill rationale.
            return self._simulate_single_option_fill(spec, metadata, data, allow_natural_fill=True)
        if self._single_option_market(metadata, data, refresh_quotes=True) is None:
            return OrderResult(ok=False, order_id=None, raw=spec, message="live_missing_or_stale_quotes", simulated=False)
        return self._submit_live_single_order_with_poll(spec, cancel_on_timeout=True, price_scale=100.0)

    def can_close_position_now(self, position: Position, ts=None) -> bool:
        asset_type = str((position.metadata or {}).get("asset_type") or ASSET_TYPE_EQUITY).upper()
        if asset_type in OPTION_ASSET_TYPES:
            return self._is_regular_options_session(ts)
        return self._equity_session(ts) is not None

    def close_position(self, position: Position, data=None, market_snapshot: Any | None = None) -> OrderResult:
        asset_type = position.metadata.get("asset_type")
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
            spec = build_vertical_close_order(position.metadata, position.qty, limit_price=limit_price)
            if self.config.schwab.dry_run:
                return self._simulate_vertical_fill(spec, position.metadata, data, refresh_quotes=False, allow_natural_fill=True)
            return self._submit_live_single_order_with_poll(spec, cancel_on_timeout=True, price_scale=100.0)
        if asset_type == ASSET_TYPE_OPTION_SINGLE:
            symbol = str(position.metadata.get("option_symbol") or "")
            if data and symbol:
                data.fetch_quotes([symbol], force=True, min_force_interval_seconds=self._option_quote_force_cooldown_seconds(), source="execution:close_single")
            q = data.get_quote(symbol) if data and symbol else None
            if data and (not q or not data.quotes_are_fresh([symbol], self.config.options.max_quote_age_seconds)):
                return OrderResult(ok=False, order_id=None, raw=None, message="close_missing_or_stale_quotes", simulated=self.config.schwab.dry_run)
            limit_price = close_single_option_limit_from_metadata(position.metadata, q, mode=self.config.options.option_limit_mode)
            spec = build_single_option_close_order(position.metadata, position.qty, limit_price=limit_price)
            if self.config.schwab.dry_run:
                return self._simulate_single_option_fill(spec, position.metadata, data, refresh_quotes=False, allow_natural_fill=True)
            return self._submit_live_single_order_with_poll(spec, cancel_on_timeout=True, price_scale=100.0)
        intent = self.order_intent_for_exit(position.side)
        return self.submit_equity_exit(position.symbol, position.qty, intent, data=data, market_snapshot=market_snapshot)

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
