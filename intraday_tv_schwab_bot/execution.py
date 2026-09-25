# SPDX-License-Identifier: MIT
from __future__ import annotations

import copy
import datetime
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
        per_leg: dict[Any, int] = {}
        for activity in payload.get("orderActivityCollection") or []:
            if not isinstance(activity, dict):
                continue
            for leg in activity.get("executionLegs") or []:
                if not isinstance(leg, dict):
                    continue
                qty = cls._safe_int(leg.get("quantity"))
                if qty is None:
                    continue
                per_leg[leg.get("legId")] = per_leg.get(leg.get("legId"), 0) + max(0, qty)
        if not per_leg:
            return None
        legs = [leg for leg in (payload.get("orderLegCollection") or []) if isinstance(leg, dict)]
        if len(legs) <= 1:
            return sum(per_leg.values())
        # A vertical's executions arrive once per LEG: summing them counted
        # every spread twice. A spread unit is filled once every leg is, so
        # the order's fill is its least-filled leg, per unit of order quantity.
        order_qty = cls._safe_float(payload.get("quantity"))
        ratios: list[float] = []
        for leg in legs:
            leg_qty = cls._safe_float(leg.get("quantity"))
            ratios.append(leg_qty / order_qty if leg_qty and order_qty and order_qty > 0 else 1.0)
        if set(per_leg) == {None}:
            # No legId on any execution: the pool holds every leg's shares.
            return int(sum(per_leg.values()) / sum(ratios))
        return min(int(per_leg.get(leg.get("legId"), 0) / ratio) for leg, ratio in zip(legs, ratios))

    @classmethod
    def _order_executions(cls, payload: dict[str, Any]) -> dict[Any, tuple[float, float]]:
        """``(notional, quantity)`` of an order's executions, per ``legId``.

        Executions carrying no ``legId`` pool under ``None``; a single-leg
        order's executions need no attribution.
        """
        out: dict[Any, tuple[float, float]] = {}
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
                notional, filled = out.get(leg.get("legId"), (0.0, 0.0))
                out[leg.get("legId")] = (notional + px * qty, filled + qty)
        return out

    @classmethod
    def _multi_leg_net_fill_price(cls, payload: dict[str, Any], legs: list[dict[str, Any]],
                                  executions: dict[Any, tuple[float, float]]) -> float | None:
        """Net price per spread unit of a multi-leg order (a vertical).

        Each leg executes at its OWN price, so pooling the executions averaged
        the long and short strikes: a 2.00/1.00 vertical read back as 1.50
        instead of its 1.00 net, and the stop, target and P&L built on that
        entry price inherited the error. The net is the signed sum of the leg
        prices (buys add, sells subtract) scaled by each leg's ratio to the
        order quantity; its magnitude is the debit paid or credit received.
        None when any leg has no attributable execution.
        """
        order_qty = cls._safe_float(payload.get("quantity"))
        net = 0.0
        for leg in legs:
            notional, filled = executions.get(leg.get("legId"), (0.0, 0.0))
            leg_qty = cls._safe_float(leg.get("quantity"))
            if filled <= 0 or leg_qty is None or leg_qty <= 0:
                return None
            ratio = leg_qty / order_qty if order_qty and order_qty > 0 else 1.0
            sign = 1.0 if str(leg.get("instruction") or "").upper().startswith("BUY") else -1.0
            net += sign * (notional / filled) * ratio
        net = abs(net)
        return net if net > 0 else None

    @classmethod
    def _equity_order_fill_price(cls, payload: dict[str, Any] | None) -> float | None:
        """Average fill price per unit: per share, or per spread for a vertical."""
        if not isinstance(payload, dict):
            return None
        executions = cls._order_executions(payload)
        legs = [leg for leg in (payload.get("orderLegCollection") or []) if isinstance(leg, dict)]
        if len(legs) > 1:
            net = cls._multi_leg_net_fill_price(payload, legs, executions)
            if net is not None:
                return net
        elif executions:
            notional = sum(value[0] for value in executions.values())
            filled = sum(value[1] for value in executions.values())
            return notional / filled
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
            if self._equity_order_is_terminal_failure(payload):
                return True, f"{prefix}:{status}", payload
            if self._equity_order_is_filled(payload):
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

    def cancel_working_order(self, order_id: str) -> tuple[bool, str]:
        """Cancel an order a submit path left live; True once it is terminal."""
        if self.config.schwab.dry_run:
            return True, "dry_run_cancel"
        ok, msg, _payload = self._cancel_live_equity_order(str(order_id))
        return ok, msg

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
            if not result.ok:
                result = OrderResult(ok=False, order_id=order_id, raw=latest_payload or spec, message=f"partial_fill_finalize_failed:{cancel_msg}", simulated=False)
            result.may_still_be_working = not cancel_ok
            return result
        if not cancel_on_timeout:
            return OrderResult(ok=False, order_id=order_id, raw=payload or spec, message=f"live_unfilled_timeout:{status}", simulated=False,
                               may_still_be_working=True)
        cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
        latest_payload = cancel_payload or payload
        if latest_payload is not None and self._equity_order_is_filled(latest_payload):
            return self._finalize_live_polled_order_result(spec, latest_payload, order_id, f"live_fill_after_cancel:{cancel_msg}", price_scale=price_scale)
        latest_filled_qty = self._equity_order_filled_qty(latest_payload) or 0
        if latest_filled_qty > 0:
            result = self._finalize_live_polled_order_result(spec, latest_payload, order_id, f"live_partial_fill_after_cancel:{cancel_msg}", price_scale=price_scale)
            if not result.ok:
                result = OrderResult(ok=False, order_id=order_id, raw=latest_payload or spec, message=f"partial_fill_after_cancel_finalize_failed:{cancel_msg}", simulated=False)
            result.may_still_be_working = not cancel_ok
            return result
        if not cancel_ok:
            return OrderResult(ok=False, order_id=order_id, raw=latest_payload or spec, message=f"live_unfilled_cancel_failed:{cancel_msg}", simulated=False,
                               may_still_be_working=True)
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
                    result = OrderResult(ok=False, order_id=order_id, raw=latest_payload or current_spec, message=f"partial_fill_finalize_failed:{cancel_msg}", simulated=False)
                result.may_still_be_working = not cancel_ok
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
                        result.may_still_be_working = not cancel_ok
                        return result
                if not cancel_ok:
                    return OrderResult(ok=False, order_id=order_id, raw=latest_payload or current_spec, message=f"live_unfilled_cancel_failed:{cancel_msg}", simulated=False,
                                       may_still_be_working=True)
                return OrderResult(ok=False, order_id=order_id, raw=latest_payload or current_spec, message="live_unfilled_canceled", simulated=False)
            cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
            latest_payload = cancel_payload or payload
            if latest_payload is not None and self._equity_order_is_filled(latest_payload):
                return self._finalize_live_equity_entry_result(current_request, current_spec, latest_payload, order_id, f"live_fill_after_reprice_cancel_attempt_{attempt}:{cancel_msg}", data=data)
            latest_filled_qty = self._equity_order_filled_qty(latest_payload) or 0
            if latest_filled_qty > 0:
                result = self._finalize_live_equity_entry_result(current_request, current_spec, latest_payload, order_id, f"live_partial_fill_after_reprice_cancel_attempt_{attempt}:{cancel_msg}", data=data)
                if result.ok:
                    result.may_still_be_working = not cancel_ok
                    return result
            if not cancel_ok:
                return OrderResult(ok=False, order_id=order_id, raw=latest_payload or current_spec, message=f"live_reprice_cancel_failed:{cancel_msg}", simulated=False,
                                   may_still_be_working=True)
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

    def submit_equity_entry(self, symbol: str, qty: int, intent: OrderIntent, data=None, market_snapshot: Any | None = None,
                            side: Side | None = None, stop_price: float | None = None,
                            target_price: float | None = None) -> OrderResult:
        if not str(symbol or "").strip():
            return OrderResult(ok=False, order_id=None, raw=None, message="invalid_symbol", simulated=self.config.schwab.dry_run)
        if int(qty) <= 0:
            return OrderResult(ok=False, order_id=None, raw=None, message="invalid_qty", simulated=self.config.schwab.dry_run)
        session = self._equity_session()
        if session is None:
            return OrderResult(ok=False, order_id=None, raw=None, message=self._equity_session_blackout_reason(), simulated=self.config.schwab.dry_run)
        bracketed = self.bracket_orders_enabled() and side is not None and stop_price is not None
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
                # Dry-run keeps exits ENGINE-side (risk.update_position already
                # decides stop/target identically), so the bracket is recorded
                # for parity/inspection but nothing rests at a broker. Live
                # fills at the resting limit will beat these poll-priced exits.
                assert side is not None and stop_price is not None
                direction = self._bracket_round_direction(side)
                result.bracket = {
                    "parent_order_id": None,
                    "sync_mode": str(self.config.execution.bracket_sync_mode),
                    "legs": str(self.config.execution.bracket_legs),
                    "session": str(session),
                    "stop_price": self._round_equity_price(stop_price, direction),
                    "target_price": (
                        self._round_equity_price(target_price, direction)
                        if (self.bracket_carries_target() and target_price is not None) else None
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
            assert side is not None and stop_price is not None
            return self._submit_live_bracket_entry(request, side=side, stop_price=stop_price, target_price=target_price, data=data)
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


    # ------------------------------------------------------------------
    # Broker-side bracket (first-triggers-OCO) orders
    #
    # The entry goes out as ONE Schwab TRIGGER order whose child OCO carries
    # the protective stop (and, in ``stop_and_target`` leg mode, the target).
    # The exit then rests AT THE BROKER instead of waiting for the engine's
    # management poll to observe the level and fire a marketable limit.
    # ------------------------------------------------------------------

    _BRACKET_STOP_ORDER_TYPES = {"STOP", "STOP_LIMIT"}

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

    def bracket_stop_limit_price(self, side: Side, entry_price: float, stop_price: float) -> float | None:
        """Limit price for a STOP_LIMIT protective child (None for plain STOP).

        Offset below (LONG) / above (SHORT) the trigger by
        ``bracket_stop_limit_offset_r`` units of initial R, bounding slippage
        on a flush without making the order unfillable in normal conditions.
        """
        cfg = self.config.execution
        if cfg.bracket_stop_order_type != "STOP_LIMIT":
            return None
        initial_risk = abs(float(entry_price) - float(stop_price))
        offset = initial_risk * float(cfg.bracket_stop_limit_offset_r)
        if side == Side.LONG:
            return max(0.0001, self._round_equity_price(float(stop_price) - offset, "down"))
        return self._round_equity_price(float(stop_price) + offset, "up")

    def _bracket_stop_child(self, symbol: str, qty: int, exit_intent: OrderIntent, stop_price: float,
                            stop_limit_price: float | None, session: str) -> dict[str, Any]:
        cfg = self.config.execution
        child: dict[str, Any] = {
            "orderStrategyType": "SINGLE",
            "session": session,
            "duration": "DAY",
            "orderType": cfg.bracket_stop_order_type,
            "stopPrice": f"{stop_price:.4f}",
            "orderLegCollection": [self._equity_order_leg(symbol, qty, exit_intent)],
        }
        if cfg.bracket_stop_order_type == "STOP_LIMIT":
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

    def _bracket_exit_children(self, symbol: str, qty: int, side: Side, entry_price: float,
                               stop_price: float, target_price: float | None,
                               session: str) -> tuple[list[dict[str, Any]], float, float | None]:
        """Build the protective child order(s) plus the rounded levels used.

        Returns ``(children, rounded_stop, rounded_target_or_None)``. The
        target child is omitted in ``stop_only`` leg mode or when the strategy
        supplied no target (runner signals carry ``target_price=None``).
        """
        exit_intent = self.order_intent_for_exit(side)
        direction = self._bracket_round_direction(side)
        rounded_stop = self._round_equity_price(stop_price, direction)
        stop_limit = self.bracket_stop_limit_price(side, entry_price, rounded_stop)
        children = [self._bracket_stop_child(symbol, qty, exit_intent, rounded_stop, stop_limit, session)]
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
        """One-order first-triggers-OCO bracket: entry + protective exit(s)."""
        children, _rounded_stop, _rounded_target = self._bracket_exit_children(
            request.symbol, request.qty, side, float(request.price or 0.0),
            stop_price, target_price, request.session,
        )
        spec = self._build_order(request)
        spec["orderStrategyType"] = "TRIGGER"
        spec["childOrderStrategies"] = self._wrap_oco(children)
        return spec

    def build_protective_oco_order(self, symbol: str, qty: int, side: Side, entry_price: float,
                                   stop_price: float, target_price: float | None,
                                   session: str) -> dict[str, Any]:
        """Standalone protective OCO for an ALREADY-OPEN position.

        Used when a bracketed entry only partially filled and the broker
        cancelled the untriggered children along with the parent, and on
        startup reconcile to re-protect an adopted position.
        """
        children, _rounded_stop, _rounded_target = self._bracket_exit_children(
            symbol, qty, side, entry_price, stop_price, target_price, session,
        )
        wrapped = self._wrap_oco(children)
        return wrapped[0] if len(wrapped) == 1 else {"orderStrategyType": "OCO", "childOrderStrategies": children}

    @classmethod
    def extract_bracket_children(cls, payload: dict[str, Any] | None) -> dict[str, Any]:
        """Pull child order ids out of an ``order_details`` payload.

        Walks nested ``childOrderStrategies`` (TRIGGER -> OCO -> SINGLE) and
        classifies each leaf by ``orderType``. Returns empty ids when the
        broker has not yet materialised the children.
        """
        found: dict[str, Any] = {
            "oco_order_id": None,
            "stop_order_id": None,
            "target_order_id": None,
            "child_order_ids": [],
        }

        def _walk(nodes: Any) -> None:
            if not isinstance(nodes, list):
                return
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                order_id = node.get("orderId")
                strategy_type = str(node.get("orderStrategyType") or "").upper()
                order_type = str(node.get("orderType") or "").upper()
                if order_id is not None:
                    if strategy_type == "OCO":
                        found["oco_order_id"] = str(order_id)
                    else:
                        found["child_order_ids"].append(str(order_id))
                        if order_type in cls._BRACKET_STOP_ORDER_TYPES and found["stop_order_id"] is None:
                            found["stop_order_id"] = str(order_id)
                        elif order_type == "LIMIT" and found["target_order_id"] is None:
                            found["target_order_id"] = str(order_id)
                _walk(node.get("childOrderStrategies"))

        if isinstance(payload, dict):
            _walk(payload.get("childOrderStrategies"))
        return found

    @classmethod
    def _flatten_order_tree(cls, node: Any, out: dict[str, dict[str, Any]]) -> None:
        if isinstance(node, list):
            for item in node:
                cls._flatten_order_tree(item, out)
            return
        if not isinstance(node, dict):
            return
        order_id = node.get("orderId")
        if order_id is not None:
            legs = [leg for leg in (node.get("orderLegCollection") or []) if isinstance(leg, dict)]
            out[str(order_id)] = {
                "status": cls._equity_order_status(node),
                "order_type": str(node.get("orderType") or "").upper(),
                "filled_qty": cls._equity_order_filled_qty(node),
                "fill_price": cls._equity_order_fill_price(node),
                "is_filled": cls._equity_order_is_filled(node),
                "is_terminal_failure": cls._equity_order_is_terminal_failure(node),
                # Shares still resting: what adoption compares against the
                # position before it trusts a working child.
                "remaining_qty": cls._equity_order_remaining_qty(node),
                "leg_qty": cls._safe_int(legs[0].get("quantity")) if len(legs) == 1 else None,
                "stop_price": cls._safe_float(node.get("stopPrice")),
                "price": cls._safe_float(node.get("price")),
            }
        cls._flatten_order_tree(node.get("childOrderStrategies"), out)

    def fetch_order_states(self, lookback_minutes: int = 480) -> dict[str, dict[str, Any]] | None:
        """Snapshot every recent order (and child) in ONE ``account_orders`` call.

        Deliberately not per-position ``order_details``: at 4 open positions and
        ``loop_sleep_seconds: 2.0`` that would be 120 requests/minute, which is
        the entire Schwab budget. Returns None when the call fails so callers
        can distinguish "no data" from "nothing filled".
        """
        if self.config.schwab.dry_run:
            return {}
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            response = call_schwab_client(
                self.client, "account_orders", self.account_hash,
                now - datetime.timedelta(minutes=max(1, int(lookback_minutes))), now,
            )
        except Exception as exc:
            LOG.warning("account_orders failed during bracket reconcile: %s", exc)
            return None
        if not (200 <= getattr(response, "status_code", 0) < 300):
            LOG.warning("account_orders status=%s during bracket reconcile", getattr(response, "status_code", None))
            return None
        try:
            payload = response.json()
        except Exception as exc:
            LOG.warning("account_orders json decode failed during bracket reconcile: %s", exc)
            return None
        out: dict[str, dict[str, Any]] = {}
        self._flatten_order_tree(payload, out)
        return out

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
        self._flatten_order_tree(payload, out)
        return out.get(str(order_id))

    def _bracket_state_from_order(self, order_id: str) -> dict[str, Any]:
        """Read child order ids back off a submitted bracket/OCO parent.

        A ``stop_only`` standalone protective order has no children at all --
        the order itself IS the stop, so its own id is reported as the stop id.
        """
        payload, _status = self._equity_order_details(order_id)
        children = self.extract_bracket_children(payload)
        if not children["child_order_ids"] and isinstance(payload, dict):
            order_type = str(payload.get("orderType") or "").upper()
            if order_type in self._BRACKET_STOP_ORDER_TYPES:
                children["stop_order_id"] = str(order_id)
                children["child_order_ids"] = [str(order_id)]
            elif order_type == "LIMIT":
                children["target_order_id"] = str(order_id)
                children["child_order_ids"] = [str(order_id)]
        return children

    def submit_protective_oco(self, symbol: str, qty: int, side: Side, entry_price: float,
                              stop_price: float, target_price: float | None,
                              session: str) -> OrderResult:
        """Submit standalone protection for an already-open position.

        Used when a bracketed entry filled but its children never materialised
        (partial-fill cancel path), and on startup reconcile to re-protect an
        adopted position.
        """
        spec = self.build_protective_oco_order(symbol, qty, side, entry_price, stop_price, target_price, session)
        if self.config.schwab.dry_run:
            LOG.info("DRY RUN protective OCO: %s", spec)
            return OrderResult(ok=True, order_id=None, raw=spec, message="dry_run_protective_oco", simulated=True)
        response = self._submit_live_order_spec(spec)
        status_code = getattr(response, "status_code", 0)
        if not (200 <= status_code < 300):
            LOG.error("Protective OCO submission failed symbol=%s qty=%s status=%s", symbol, qty, status_code)
            return OrderResult(ok=False, order_id=None, raw=getattr(response, "text", spec),
                               message=f"protective_oco_status={status_code}", simulated=False)
        order_id = self._response_order_id(response)
        if not order_id:
            return OrderResult(ok=False, order_id=None, raw=getattr(response, "text", spec),
                               message="protective_oco_missing_order_id", simulated=False)
        return OrderResult(ok=True, order_id=str(order_id), raw=spec, message="protective_oco_submitted",
                           simulated=False, bracket=self._bracket_state_from_order(str(order_id)))

    def ensure_position_protected(self, symbol: str, qty: int, side: Side, entry_price: float,
                                  stop_price: float, target_price: float | None,
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

        Returns the bracket state dict, or None when bracket mode is off.
        """
        if not self.bracket_orders_enabled():
            return None
        if int(qty) <= 0:
            return None
        session = self._equity_session() or "NORMAL"
        direction = self._bracket_round_direction(side)
        base: dict[str, Any] = {
            "parent_order_id": str(parent_order_id) if parent_order_id else None,
            "sync_mode": str(self.config.execution.bracket_sync_mode),
            "legs": str(self.config.execution.bracket_legs),
            "session": session,
            "stop_price": self._round_equity_price(stop_price, direction),
            "target_price": (
                self._round_equity_price(target_price, direction)
                if (self.bracket_carries_target() and target_price is not None) else None
            ),
            "qty": int(qty),
        }
        existing = self._adoptable_protection(parent_order_id, known_bracket)
        if existing is not None:
            resting_qty = existing.pop("resting_qty", None)
            adopted = {**base, **existing, "active": True, "state": "adopted"}
            # A size that could not be read is re-issued at ``qty`` too (fail
            # closed, 2026-09-25): the stop may rest more shares than are
            # held. Until then an unread size was trusted, so restore_basic
            # left a 10-share stop resting against 7 held shares.
            if resting_qty is None or int(resting_qty) != int(qty):
                adopted["qty"] = None if resting_qty is None else int(resting_qty)
                resized, msg = self.resize_bracket_children(adopted, symbol, side, int(qty), entry_price, session)
                if not resized:
                    adopted["state"] = "qty_mismatch" if resting_qty is not None else "qty_unverified"
                    LOG.error(
                        "Adopted protection for %s rests %s shares against a %s-share position and could not "
                        "be resized (%s) -- a resting exit larger than the position would flip it on trigger",
                        symbol, "an unknown number of" if resting_qty is None else resting_qty, qty, msg,
                    )
            return adopted
        replacement = self.submit_protective_oco(
            symbol, int(qty), side, entry_price, float(base["stop_price"]), target_price, session,
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

    def _adoptable_protection(self, parent_order_id: str | None,
                              known_bracket: dict[str, Any] | None) -> dict[str, Any] | None:
        """Child ids (plus what they rest) of protection still working, or None.

        Each child's state comes from the account_orders listing or, when
        the listing does not return it (it failed, or the order is older
        than its 8-hour lookback: a stop entered before an overnight hold),
        from ``order_state``, as ``startup_reconciler._drop_retired_orders``
        reads a missing id (2026-09-25). Until then a missing stop was
        adopted with no size, so ``ensure_position_protected`` never resized
        it. A stop neither read returns is still adopted, rather than risk
        stacking a second protective order on a live one, with its size
        unknown (``resting_qty`` None), which ``ensure_position_protected``
        re-issues at the position's size.
        """
        if isinstance(known_bracket, dict) and known_bracket.get("stop_order_id"):
            ids: dict[str, Any] = {
                key: known_bracket.get(key)
                for key in ("oco_order_id", "protective_order_id", "stop_order_id", "target_order_id")
            }
            ids["child_order_ids"] = [str(oid) for oid in (known_bracket.get("child_order_ids") or []) if oid]
        elif parent_order_id:
            ids = self._bracket_state_from_order(str(parent_order_id))
        else:
            return None
        stop_id = ids.get("stop_order_id")
        if not stop_id:
            return None
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
        """
        child_order_id = str(bracket.get(child_key) or "")
        try:
            response = call_schwab_client(self.client, "replace_order", self.account_hash, child_order_id, spec)
        except Exception as exc:
            return False, f"replace_error:{exc}"
        status_code = getattr(response, "status_code", 0)
        if not 200 <= status_code < 300:
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
                                entry_price: float, session: str) -> tuple[bool, str]:
        """Re-issue the resting protective children at a new share count.

        The short-flip guard: children are submitted for the REQUESTED entry
        quantity, so a partial entry fill leaves an oversized resting exit that
        would take a long-only strategy net short when it triggers.
        """
        exit_intent = self.order_intent_for_exit(side)
        stop_price = float(bracket.get("stop_price") or 0.0)
        target_price = bracket.get("target_price")
        messages: list[str] = []
        ok = True
        if bracket.get("stop_order_id") and stop_price > 0:
            stop_limit = self.bracket_stop_limit_price(side, entry_price, stop_price)
            spec = self._bracket_stop_child(symbol, qty, exit_intent, stop_price, stop_limit, session)
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
                            entry_price: float, stop_price: float,
                            target_price: float | None) -> list[dict[str, Any]]:
        """Replace resting children whose level has moved beyond the debounce.

        Returns one management-adjustment record per child actually replaced,
        so the caller can log them alongside the engine's own adjustments. The
        debounce keeps a per-cycle trail ratchet from spending the Schwab rate
        budget on sub-penny moves.
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
                stop_limit = self.bracket_stop_limit_price(side, entry_price, rounded)
                spec = self._bracket_stop_child(symbol, int(qty), exit_intent, rounded, stop_limit, session)
                ok, msg = self.replace_bracket_child(bracket, "stop_order_id", spec)
                if ok:
                    bracket["stop_price"] = rounded
                    adjustments.append({"manager": "bracket_sync", "kind": "stop", "reason": "replace_child",
                                        "from": resting_stop, "to": rounded})
                else:
                    LOG.warning("Bracket stop replace failed for %s (%s); broker still rests at %s",
                                symbol, msg, resting_stop)

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
        """
        if not isinstance(bracket, dict):
            return BracketCancel(True, "no_bracket")
        if self.config.schwab.dry_run:
            return BracketCancel(True, "dry_run_cancel")
        wrapper_id = bracket.get("oco_order_id") or bracket.get("protective_order_id")
        child_ids: list[str] = []
        for oid in (bracket.get("stop_order_id"), bracket.get("target_order_id"), *(bracket.get("child_order_ids") or [])):
            if oid and str(oid) != str(wrapper_id or "") and str(oid) not in child_ids:
                child_ids.append(str(oid))
        if not wrapper_id and not child_ids:
            return BracketCancel(True, "no_resting_orders")
        ok = True
        messages: list[str] = []
        fills: dict[str, tuple[int, float | None, str]] = {}
        if wrapper_id:
            cancel_ok, msg, payload = self._cancel_live_equity_order(str(wrapper_id))
            ok = cancel_ok
            messages.append(f"{wrapper_id}:{msg}")
            self._collect_protective_fills(payload, fills)
        for child_id in child_ids:
            if wrapper_id:
                # Usually already down with the wrapper: confirm before
                # spending a cancel call on it.
                payload, _status = self._equity_order_details(child_id)
                if payload is not None and (self._equity_order_is_terminal_failure(payload) or self._equity_order_is_filled(payload)):
                    self._collect_protective_fills(payload, fills)
                    continue
            cancel_ok, msg, payload = self._cancel_live_equity_order(child_id)
            ok = ok and cancel_ok
            messages.append(f"{child_id}:{msg}")
            self._collect_protective_fills(payload, fills)
        if ok:
            bracket["active"] = False
            bracket["state"] = "canceled"
        filled_qty = sum(qty for qty, _px, _kind in fills.values())
        priced = [(qty, px) for qty, px, _kind in fills.values() if px is not None]
        priced_qty = sum(qty for qty, _px in priced)
        fill_price = sum(qty * px for qty, px in priced) / priced_qty if priced_qty > 0 else None
        fill_reason = max(fills.values(), key=lambda fill: fill[0])[2] if fills else None
        return BracketCancel(ok, ",".join(messages), int(filled_qty), fill_price, fill_reason)

    @classmethod
    def _collect_protective_fills(cls, payload: Any, into: dict[str, tuple[int, float | None, str]]) -> None:
        """Record ``order_id -> (filled_qty, fill_price, exit reason)`` for every
        protective leg in *payload* that has fills. Keyed by order id so a leg
        seen both under its wrapper and on its own is counted once."""
        if isinstance(payload, list):
            for node in payload:
                cls._collect_protective_fills(node, into)
            return
        if not isinstance(payload, dict):
            return
        order_id = payload.get("orderId")
        if order_id is not None and str(payload.get("orderStrategyType") or "").upper() != "OCO":
            filled_qty = cls._equity_order_filled_qty(payload) or 0
            if filled_qty > 0:
                order_type = str(payload.get("orderType") or "").upper()
                reason = "broker_stop" if order_type in cls._BRACKET_STOP_ORDER_TYPES else "broker_target"
                into[str(order_id)] = (int(filled_qty), cls._equity_order_fill_price(payload), reason)
        cls._collect_protective_fills(payload.get("childOrderStrategies"), into)

    def _finalize_bracket_protection(self, result: OrderResult, request: OrderRequest, side: Side,
                                     stop_price: float, target_price: float | None,
                                     parent_order_id: str, payload: dict[str, Any] | None) -> OrderResult:
        """Attach bracket state to an entry result and make the sizing correct."""
        children = self.extract_bracket_children(payload)
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
        entry_price = float(result.fill_price if result.fill_price is not None else (request.price or 0.0))

        if not children["child_order_ids"]:
            # Parent filled but no children are resting -- either the broker
            # never materialised them, or they were cancelled alongside the
            # parent on the partial-fill path. The position is OPEN AND
            # UNPROTECTED, so submit standalone protection immediately.
            replacement = self.submit_protective_oco(
                request.symbol, filled_qty, side, entry_price,
                float(bracket["stop_price"]), target_price, request.session,
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
                bracket, request.symbol, side, filled_qty, entry_price, request.session,
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
        result.bracket = bracket
        return result

    def _submit_live_bracket_entry(self, request: OrderRequest, *, side: Side, stop_price: float,
                                   target_price: float | None, data=None) -> OrderResult:
        """Submit the one-order bracket and reconcile the resting protection.

        No reprice loop: cancel/replace churn on a TRIGGER parent with live
        children is how brackets get orphaned, and an entry that needs several
        reprices has already left the level the signal was built on.
        """
        timeout_seconds = max(0.5, float(self.config.execution.entry_live_fill_timeout_seconds))
        poll_seconds = max(0.1, float(self.config.execution.entry_live_poll_seconds))
        spec = self.build_bracket_order(request, side=side, stop_price=stop_price, target_price=target_price)
        response = self._submit_live_order_spec(spec)
        status_code = getattr(response, "status_code", 0)
        if not (200 <= status_code < 300):
            return OrderResult(ok=False, order_id=None, raw=getattr(response, "text", spec),
                               message=f"bracket_status={status_code}", simulated=False)
        order_id = self._response_order_id(response)
        if not order_id:
            return OrderResult(ok=False, order_id=None, raw=getattr(response, "text", spec),
                               message="bracket_missing_order_id", simulated=False)
        payload, status = self._poll_equity_order(order_id, timeout_seconds, poll_seconds)
        if payload is not None and self._equity_order_is_filled(payload):
            result = self._finalize_live_equity_entry_result(request, spec, payload, order_id, f"live_bracket_fill:{status}", data=data)
            return self._finalize_bracket_protection(result, request, side, stop_price, target_price, order_id, payload)
        if (self._equity_order_filled_qty(payload) or 0) > 0:
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
            return self._finalize_bracket_protection(result, request, side, stop_price, target_price, order_id, latest)
        cancel_ok, cancel_msg, cancel_payload = self._cancel_live_equity_order(order_id)
        latest = cancel_payload or payload
        if latest is not None and self._equity_order_is_filled(latest):
            result = self._finalize_live_equity_entry_result(request, spec, latest, order_id, f"live_bracket_fill_after_cancel:{cancel_msg}", data=data)
            return self._finalize_bracket_protection(result, request, side, stop_price, target_price, order_id, latest)
        if (self._equity_order_filled_qty(latest) or 0) > 0:
            result = self._finalize_live_equity_entry_result(request, spec, latest, order_id, f"live_bracket_partial_fill_after_cancel:{cancel_msg}", data=data)
            if result.ok:
                result.may_still_be_working = not cancel_ok
                return self._finalize_bracket_protection(result, request, side, stop_price, target_price, order_id, latest)
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

    def close_position(self, position: Position, qty: int, data=None, market_snapshot: Any | None = None) -> OrderResult:
        """Close ``qty`` units of ``position`` -- all of it, or a scale-out slice.

        ``qty`` is required: until 2026-09-24 this always sent ``position.qty``,
        so a partial exit could not be expressed at all. It is the caller's
        sized request, already clamped to the position.
        """
        if not 1 <= int(qty) <= int(position.qty):
            raise ValueError(f"close_position qty {qty!r} outside 1..{position.qty} for {position.symbol}")
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
            spec = build_vertical_close_order(position.metadata, int(qty), limit_price=limit_price)
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
            spec = build_single_option_close_order(position.metadata, int(qty), limit_price=limit_price)
            if self.config.schwab.dry_run:
                return self._simulate_single_option_fill(spec, position.metadata, data, refresh_quotes=False, allow_natural_fill=True)
            return self._submit_live_single_order_with_poll(spec, cancel_on_timeout=True, price_scale=100.0)
        intent = self.order_intent_for_exit(position.side)
        return self.submit_equity_exit(position.symbol, int(qty), intent, data=data, market_snapshot=market_snapshot)

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
