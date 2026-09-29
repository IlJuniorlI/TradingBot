# SPDX-License-Identifier: MIT
"""Pure parsing of what the broker returns, and of the bracket record the bot
keeps for a position's resting protection.

Parsing only -- no Schwab client or executor state -- which is why these are
free functions rather than methods on a broker client wrapper:
``SchwabExecutor`` keeps the I/O and reads what comes back through them, and
the startup reconciler, ``PositionManager``, ``EntryGatekeeper`` and
``TradeManager`` share the position and bracket readers. The order readers
were ``SchwabExecutor`` classmethods until 2026-09-27, and the module was
``broker_positions``.
"""
from __future__ import annotations

from typing import Any

from .models import Side
from .numeric import safe_float, safe_int

# An order ``status`` still working at the broker.
WORKING_STATUSES = frozenset({
    "AWAITING_PARENT_ORDER",
    "AWAITING_STOP_CONDITION",
    "AWAITING_CONDITION",
    "AWAITING_MANUAL_REVIEW",
    "AWAITING_UR_OUT",
    "WORKING",
    "PENDING_ACTIVATION",
    "PENDING_ACKNOWLEDGEMENT",
    "PENDING_RECALL",
    "QUEUED",
    "ACCEPTED",
    "OPEN",
    "LIVE",
    "PARTIALLY_FILLED",
})
# An order ``status`` that ended the order without (the rest of) its fill.
TERMINAL_FAILURE_STATUSES = frozenset({"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "REPLACED"})
# The ``orderType`` of a protective stop.
STOP_ORDER_TYPES = frozenset({"STOP", "STOP_LIMIT"})

# The order-id keys of a bracket record (``Position.metadata["bracket"]``).
# A wrapper's cancel takes the original legs down with it: the OCO of a
# bracketed entry, or the standalone protective order; the first set one wins.
# The children are the resting stop and target; ``child_order_ids`` lists
# every child id as well. A new id key goes here, so every reader of the ids
# sees it: one a reader missed read an owned order as foreign.
BRACKET_WRAPPER_KEYS = ("oco_order_id", "protective_order_id")
BRACKET_CHILD_KEYS = ("stop_order_id", "target_order_id")
BRACKET_ID_KEYS = BRACKET_WRAPPER_KEYS + BRACKET_CHILD_KEYS

# The ``sync_mode`` of a disaster stop's record (``execution.
# disaster_stop_enabled``, 2026-09-28): a lone STOP beyond the engine's
# initial stop that is never moved, while the engine keeps its own stop (it
# checks it every cycle whatever rests at the broker). It is kept as the
# position's bracket record, so the fill reconcile, the cancel before an
# engine exit, the re-protect of a remainder, the restore's adoption and the
# foreign-order check treat it as they treat a bracket's stop.
DISASTER_SYNC_MODE = "disaster"
# The exit reason a filled disaster stop is booked with.
DISASTER_STOP_REASON = "disaster_stop"
# The exit reason a filled bracket stop is booked with.
BRACKET_STOP_REASON = "broker_stop"


def extract_broker_positions(payload: Any) -> list[dict[str, Any]]:
    acct = payload.get("securitiesAccount") if isinstance(payload, dict) and isinstance(payload.get("securitiesAccount"), dict) else payload
    positions = acct.get("positions") if isinstance(acct, dict) else None
    out: list[dict[str, Any]] = []
    for row in positions or []:
        instrument = row.get("instrument") or {}
        out.append({
            "symbol": instrument.get("symbol"),
            "assetType": instrument.get("assetType"),
            "longQuantity": row.get("longQuantity"),
            "shortQuantity": row.get("shortQuantity"),
            "averagePrice": row.get("averagePrice"),
        })
    return out


def extract_working_orders(payload: Any) -> list[dict[str, Any]]:
    def iter_orders(obj: Any):
        if obj is None:
            return
        if isinstance(obj, list):
            for item in obj:
                yield from iter_orders(item)
            return
        if isinstance(obj, dict):
            if any(k in obj for k in ("status", "orderId", "orderLegCollection", "childOrderStrategies")):
                yield obj
            for key in ("orders", "orderStrategies", "results", "childOrderStrategies"):
                nested = obj.get(key)
                if nested is not None:
                    yield from iter_orders(nested)

    out: list[dict[str, Any]] = []
    for row in iter_orders(payload):
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").upper()
        if status not in WORKING_STATUSES:
            continue
        legs = [leg for leg in (row.get("orderLegCollection") or []) if isinstance(leg, dict)]
        symbols = [str(((leg.get("instrument") or {}).get("symbol") or "")) for leg in legs]
        out.append({
            "orderId": row.get("orderId"),
            "status": status,
            "symbols": [s for s in symbols if s],
            "enteredTime": row.get("enteredTime"),
            "orderType": str(row.get("orderType") or "").upper(),
            "orderStrategyType": str(row.get("orderStrategyType") or "").upper(),
            "instructions": [str(leg.get("instruction") or "").upper() for leg in legs],
            # None when the row does not carry it as a finite number.
            "stopPrice": safe_float(row.get("stopPrice"), None, finite=True),
            "quantity": safe_int(row.get("quantity")),
            "filledQuantity": order_filled_qty(row),
        })
    return out


def order_status(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return "UNKNOWN"
    status = str(payload.get("status") or "").upper().strip()
    return status or "UNKNOWN"


def order_remaining_qty(payload: dict[str, Any] | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("remainingQuantity", "remainingQty", "leavesQuantity"):
        value = safe_int(payload.get(key))
        if value is not None:
            return max(0, value)
    return None


def order_filled_qty(payload: dict[str, Any] | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("filledQuantity", "filledQty", "cumulativeQuantity", "executedQuantity"):
        value = safe_int(payload.get(key))
        if value is not None:
            return max(0, value)
    per_leg: dict[Any, int] = {}
    for activity in payload.get("orderActivityCollection") or []:
        if not isinstance(activity, dict):
            continue
        for leg in activity.get("executionLegs") or []:
            if not isinstance(leg, dict):
                continue
            qty = safe_int(leg.get("quantity"))
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
    # A quantity that is not a finite number reads as missing: an
    # infinite order quantity made every ratio 0 and raised
    # ZeroDivisionError (2026-09-26).
    order_qty = safe_float(payload.get("quantity"), finite=True)
    ratios: list[float] = []
    for leg in legs:
        leg_qty = safe_float(leg.get("quantity"), finite=True)
        ratios.append(leg_qty / order_qty if leg_qty and order_qty and order_qty > 0 else 1.0)
    if set(per_leg) == {None}:
        # No legId on any execution: the pool holds every leg's shares.
        return int(sum(per_leg.values()) / sum(ratios))
    return min(int(per_leg.get(leg.get("legId"), 0) / ratio) for leg, ratio in zip(legs, ratios))


def _order_executions(payload: dict[str, Any]) -> dict[Any, tuple[float, float]]:
    """``(notional, quantity)`` of an order's executions, per ``legId``.

    Executions carrying no ``legId`` pool under ``None``; a single-leg
    order's executions need no attribution. One whose price or quantity
    is not a finite number is skipped, as a missing one is: an infinite
    quantity read the fill price as NaN (2026-09-26).
    """
    out: dict[Any, tuple[float, float]] = {}
    for activity in payload.get("orderActivityCollection") or []:
        if not isinstance(activity, dict):
            continue
        for leg in activity.get("executionLegs") or []:
            if not isinstance(leg, dict):
                continue
            px = safe_float(leg.get("price"), finite=True)
            qty = safe_float(leg.get("quantity"), finite=True)
            if px is None or qty is None or px <= 0 or qty <= 0:
                continue
            notional, filled = out.get(leg.get("legId"), (0.0, 0.0))
            out[leg.get("legId")] = (notional + px * qty, filled + qty)
    return out


def _multi_leg_net_fill_price(payload: dict[str, Any], legs: list[dict[str, Any]],
                              executions: dict[Any, tuple[float, float]]) -> float | None:
    """Net price per spread unit of a multi-leg order (a vertical).

    Each leg executes at its OWN price, so pooling the executions averaged
    the long and short strikes: a 2.00/1.00 vertical read back as 1.50
    instead of its 1.00 net, and the stop, target and P&L built on that
    entry price inherited the error. The net is the signed sum of the leg
    prices (buys add, sells subtract) scaled by each leg's ratio to the
    order quantity; its magnitude is the debit paid or credit received.
    None when any leg has no attributable execution. A quantity that is
    not a finite number reads as missing (2026-09-26).
    """
    order_qty = safe_float(payload.get("quantity"), finite=True)
    net = 0.0
    for leg in legs:
        notional, filled = executions.get(leg.get("legId"), (0.0, 0.0))
        leg_qty = safe_float(leg.get("quantity"), finite=True)
        if filled <= 0 or leg_qty is None or leg_qty <= 0:
            return None
        ratio = leg_qty / order_qty if order_qty and order_qty > 0 else 1.0
        sign = 1.0 if str(leg.get("instruction") or "").upper().startswith("BUY") else -1.0
        net += sign * (notional / filled) * ratio
    net = abs(net)
    return net if net > 0 else None


def order_fill_price(payload: dict[str, Any] | None) -> float | None:
    """Average fill price per unit: per share, or per spread for a vertical."""
    if not isinstance(payload, dict):
        return None
    executions = _order_executions(payload)
    legs = [leg for leg in (payload.get("orderLegCollection") or []) if isinstance(leg, dict)]
    if len(legs) > 1:
        net = _multi_leg_net_fill_price(payload, legs, executions)
        if net is not None:
            return net
    elif executions:
        notional = sum(value[0] for value in executions.values())
        filled = sum(value[1] for value in executions.values())
        return notional / filled
    for key in ("price", "filledPrice", "averagePrice"):
        px = safe_float(payload.get(key), finite=True)
        if px is not None and px > 0:
            return px
    return None


def order_is_filled(payload: dict[str, Any] | None) -> bool:
    status = order_status(payload)
    if status == "FILLED":
        return True
    remaining = order_remaining_qty(payload)
    filled_qty = order_filled_qty(payload)
    return remaining == 0 and (filled_qty or 0) > 0


def order_is_terminal_failure(payload: dict[str, Any] | None) -> bool:
    status = order_status(payload)
    return status in TERMINAL_FAILURE_STATUSES


def extract_bracket_children(payload: dict[str, Any] | None) -> dict[str, Any]:
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
                    if order_type in STOP_ORDER_TYPES and found["stop_order_id"] is None:
                        found["stop_order_id"] = str(order_id)
                    elif order_type == "LIMIT" and found["target_order_id"] is None:
                        found["target_order_id"] = str(order_id)
            _walk(node.get("childOrderStrategies"))

    if isinstance(payload, dict):
        _walk(payload.get("childOrderStrategies"))
    return found


def flatten_order_tree(node: Any, out: dict[str, dict[str, Any]]) -> None:
    """Add a state row to *out*, keyed by order id, for every order in *node*
    (one order or a list of them) and every child order under it."""
    if isinstance(node, list):
        for item in node:
            flatten_order_tree(item, out)
        return
    if not isinstance(node, dict):
        return
    order_id = node.get("orderId")
    if order_id is not None:
        legs = [leg for leg in (node.get("orderLegCollection") or []) if isinstance(leg, dict)]
        out[str(order_id)] = {
            "status": order_status(node),
            "order_type": str(node.get("orderType") or "").upper(),
            "filled_qty": order_filled_qty(node),
            "fill_price": order_fill_price(node),
            "is_filled": order_is_filled(node),
            "is_terminal_failure": order_is_terminal_failure(node),
            # Shares still resting: what adoption compares against the
            # position before it trusts a working child.
            "remaining_qty": order_remaining_qty(node),
            "leg_qty": safe_int(legs[0].get("quantity")) if len(legs) == 1 else None,
            # Adopted over the bracket's own levels, so an infinite one
            # reads as missing and the bracket keeps its own (2026-09-26).
            "stop_price": safe_float(node.get("stopPrice"), finite=True),
            "price": safe_float(node.get("price"), finite=True),
        }
    flatten_order_tree(node.get("childOrderStrategies"), out)


def collect_protective_fills(payload: Any, into: dict[str, tuple[int, float | None, str]], *,
                             stop_reason: str) -> None:
    """Record ``order_id -> (filled_qty, fill_price, exit reason)`` for every
    protective leg in *payload* that has fills: a stop leg's reason is
    *stop_reason* (``protective_stop_reason`` of its record), a limit's
    ``broker_target``. Keyed by order id so a leg seen both under its
    wrapper and on its own is counted once."""
    if isinstance(payload, list):
        for node in payload:
            collect_protective_fills(node, into, stop_reason=stop_reason)
        return
    if not isinstance(payload, dict):
        return
    order_id = payload.get("orderId")
    if order_id is not None and str(payload.get("orderStrategyType") or "").upper() != "OCO":
        filled_qty = order_filled_qty(payload) or 0
        if filled_qty > 0:
            order_type = str(payload.get("orderType") or "").upper()
            reason = stop_reason if order_type in STOP_ORDER_TYPES else "broker_target"
            into[str(order_id)] = (int(filled_qty), order_fill_price(payload), reason)
    collect_protective_fills(payload.get("childOrderStrategies"), into, stop_reason=stop_reason)


def is_disaster_stop(bracket: Any) -> bool:
    """True when *bracket* is a disaster stop's record (``DISASTER_SYNC_MODE``)."""
    return isinstance(bracket, dict) and bracket.get("sync_mode") == DISASTER_SYNC_MODE


def protective_stop_reason(bracket: Any) -> str:
    """The exit reason a fill of *bracket*'s stop is booked with:
    ``disaster_stop`` for a disaster stop, ``broker_stop`` for a bracket's."""
    return DISASTER_STOP_REASON if is_disaster_stop(bracket) else BRACKET_STOP_REASON


def active_broker_bracket(position: Any) -> dict[str, Any] | None:
    """Resting broker-side bracket for a position, or None.

    Returns None for dry-run (``simulated``) brackets and for any bracket whose
    protection could not be established (``active`` false). In both cases
    nothing rests at a broker, so the engine must keep owning the stop/target
    exits. Shared by ``TradeManager`` (which suppresses the exits the broker
    owns) and ``PositionManager`` (reconcile / sync / cancel-before-exit) so
    the two can never disagree about who owns an exit.
    """
    meta = getattr(position, "metadata", None)
    if not isinstance(meta, dict):
        return None
    bracket = meta.get("bracket")
    if not isinstance(bracket, dict):
        return None
    if bracket.get("simulated") or not bracket.get("active"):
        return None
    return bracket


def bracket_order_ids(bracket: dict[str, Any]) -> set[str]:
    """Every order id *bracket* tracks (``BRACKET_ID_KEYS`` and
    ``child_order_ids``), as strings; an unset one is skipped."""
    ids = [bracket.get(key) for key in BRACKET_ID_KEYS]
    return {str(oid) for oid in (*ids, *(bracket.get("child_order_ids") or [])) if oid}


def bracket_wrapper_and_children(bracket: dict[str, Any]) -> tuple[str | None, list[str]]:
    """The wrapper whose cancel takes *bracket* down (None when it has none)
    and the child ids apart from it, in order, each once.

    A protective id beside an OCO id is neither: the OCO wins, and the
    protective order is never cancelled as a wrapper."""
    wrapper = next((bracket.get(key) for key in BRACKET_WRAPPER_KEYS if bracket.get(key)), None)
    wrapper_id = str(wrapper) if wrapper else None
    children: list[str] = []
    for oid in (*(bracket.get(key) for key in BRACKET_CHILD_KEYS), *(bracket.get("child_order_ids") or [])):
        if oid and str(oid) != (wrapper_id or "") and str(oid) not in children:
            children.append(str(oid))
    return wrapper_id, children


def working_exit_orders(working_orders: list[dict[str, Any]], symbol: Any, side: Side, *,
                        order_types: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """The ``extract_working_orders`` rows on *symbol* alone whose one leg
    exits a *side* position (a SELL for a LONG, a BUY_TO_COVER for a SHORT),
    of *order_types* when given, in the order listed."""
    exit_instruction = "SELL" if side == Side.LONG else "BUY_TO_COVER"
    wanted = str(symbol).upper().strip()
    return [
        order for order in working_orders
        if order.get("orderId") is not None
        and [str(s).upper().strip() for s in order.get("symbols") or []] == [wanted]
        and (order_types is None or order.get("orderType") in order_types)
        and list(order.get("instructions") or []) == [exit_instruction]
    ]


def resting_exit_stop(working_orders: list[dict[str, Any]], symbol: Any, side: Side) -> dict[str, Any] | None:
    """A working protective stop at the broker for a *side* position in
    *symbol*, as a bracket stub, from ``extract_working_orders`` rows: a
    STOP / STOP_LIMIT on that symbol alone whose one leg exits the position.

    The startup reconciler adopts it when the restored metadata carries no
    child ids (restore_basic, or a position entered before bracket mode was
    on). Without it fresh protection went in beside the stop still resting;
    both trigger together and take the position net short. The first match
    is adopted; any other stop on the symbol stays a foreign order and keeps
    entries blocked for a human to look at.
    (``StartupReconciler._resting_stop_for`` until 2026-09-28.)
    """
    stops = working_exit_orders(working_orders, symbol, side, order_types=STOP_ORDER_TYPES)
    if not stops:
        return None
    order_id = str(stops[0]["orderId"])
    return {"stop_order_id": order_id, "child_order_ids": [order_id]}


def sent_exit_stop(working_orders: list[dict[str, Any]], symbol: Any, side: Side, *,
                   stop_price: Any, qty: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Look for the stop an order write with an unknown outcome sent:
    ``(stub, others)``.

    ``stub`` is the working exit STOP on *symbol* for a *side* position that
    rests exactly at *stop_price* for exactly *qty* shares, as a bracket stub,
    or None. ``others`` are the other working exit STOP / STOP_LIMIT orders
    on the symbol for that side: a stop the bot did not send (one placed by
    hand in the app) or one it cannot tell for its own. Never adopted, and
    a second stop is never placed beside one, since both would sell the
    same shares. A *stop_price* or *qty* that is not known matches nothing."""
    level = safe_float(stop_price, None, finite=True)
    shares = safe_int(qty)
    stops = working_exit_orders(working_orders, symbol, side, order_types=STOP_ORDER_TYPES)
    for order in stops:
        if (order.get("orderType") == "STOP" and level is not None and shares is not None
                and order.get("stopPrice") is not None and abs(float(order["stopPrice"]) - level) < 5e-5
                and order.get("quantity") == shares):
            order_id = str(order["orderId"])
            return ({"stop_order_id": order_id, "child_order_ids": [order_id]},
                    [other for other in stops if other is not order])
    return None, stops


def working_exit_outstanding_qty(position: Any) -> int:
    """Shares the position's tracked working exit order may still sell: what
    it asked for less what is already booked from it. 0 when none is tracked.

    Shared by ``PositionManager`` (the risk check on the shares outside a
    working slice) and ``StartupReconciler`` (the restore re-protect,
    2026-09-25) so a resting stop and the order never cover the same shares.
    """
    meta = getattr(position, "metadata", None)
    record = meta.get("working_exit_order") if isinstance(meta, dict) else None
    if not isinstance(record, dict):
        return 0
    return max(0, int(record.get("requested_qty") or 0) - int(record.get("booked_qty") or 0))


def order_result_needs_broker_recheck(message: Any) -> bool:
    """True when a failed order REACHED the broker, so some of it may have filled.

    A rejected submission (``status=`` / ``bracket_status=``) never did. Every
    other failure from the live submit paths -- an unfilled order, a partial
    fill whose cancel could not be confirmed, a bracket parent that would not
    cancel -- may have left shares filled that the result does not report.
    """
    text = str(message or "").strip().lower()
    if not text:
        return False
    if text.startswith(("status=", "bracket_status=")):
        return False
    return (
        text.startswith(("live_", "cancel_", "partial_fill_", "bracket_"))
        or "order_details_" in text
    )


def broker_quantity(value: Any) -> int | None:
    """A broker position row's quantity in whole units: 0 when it is absent
    (None or an empty string), None when it is not a finite number (NaN,
    ±inf, unparseable, a whitespace-only string included).

    An unreadable quantity says nothing about what is held. Until 2026-09-26
    it read as 0, so the settle booked a tracked position closed outside the
    bot and cancelled its broker stop.
    """
    return safe_int(value or 0)


def broker_position_side_qty(row: dict[str, Any] | None) -> tuple[Side | None, int | None]:
    """The side and whole-unit quantity a broker position row holds.

    ``(None, 0)`` for no row, a flat one or one both long and short;
    ``(None, None)`` when either quantity cannot be read (``broker_quantity``).
    """
    if not isinstance(row, dict):
        return None, 0
    long_qty = broker_quantity(row.get("longQuantity"))
    short_qty = broker_quantity(row.get("shortQuantity"))
    if long_qty is None or short_qty is None:
        return None, None
    if long_qty > 0 >= short_qty:
        return Side.LONG, long_qty
    if short_qty > 0 >= long_qty:
        return Side.SHORT, short_qty
    return None, 0
