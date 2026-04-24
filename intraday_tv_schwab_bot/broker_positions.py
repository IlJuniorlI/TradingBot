# SPDX-License-Identifier: MIT
"""Pure broker-side position/order parsing helpers.

Previously ``@staticmethod`` on ``IntradayBot``; extracted here so both
``PositionManager`` (exit recovery) and engine entry recovery can share
without dragging the full engine surface along. These are parsing-only —
no Schwab client or executor state — which is why they live as free
functions rather than methods on a broker client wrapper.
"""
from __future__ import annotations

from typing import Any

from .models import Side


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
    active = {"AWAITING_PARENT_ORDER", "AWAITING_STOP_CONDITION", "AWAITING_CONDITION", "AWAITING_MANUAL_REVIEW", "AWAITING_UR_OUT", "WORKING", "PENDING_ACTIVATION", "PENDING_ACKNOWLEDGEMENT", "PENDING_RECALL", "QUEUED", "ACCEPTED", "OPEN", "LIVE", "PARTIALLY_FILLED"}

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
        if status not in active:
            continue
        legs = row.get("orderLegCollection") or []
        symbols = [str(((leg.get("instrument") or {}).get("symbol") or "")) for leg in legs if isinstance(leg, dict)]
        out.append({
            "orderId": row.get("orderId"),
            "status": status,
            "symbols": [s for s in symbols if s],
            "enteredTime": row.get("enteredTime"),
        })
    return out


def order_result_needs_broker_recheck(message: Any) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    if text.startswith("status="):
        return False
    return text.startswith("live_") or "order_details_" in text or text.startswith("cancel_")


def broker_position_side_qty(row: dict[str, Any] | None) -> tuple[Side | None, int, float | None]:
    if not isinstance(row, dict):
        return None, 0, None
    try:
        long_qty = int(float(row.get("longQuantity") or 0) or 0)
    except Exception:
        long_qty = 0
    try:
        short_qty = int(float(row.get("shortQuantity") or 0) or 0)
    except Exception:
        short_qty = 0
    avg_price = None
    try:
        raw_avg = row.get("averagePrice")
        if raw_avg is not None:
            avg_price = float(raw_avg)
    except Exception:
        avg_price = None
    if long_qty > 0 >= short_qty:
        return Side.LONG, long_qty, avg_price
    if short_qty > 0 >= long_qty:
        return Side.SHORT, short_qty, avg_price
    return None, 0, avg_price
