# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .models import OrderIntent, Side
from .position_metrics import safe_float


@dataclass(slots=True)
class OptionContract:
    symbol: str
    expiration: str
    put_call: str
    strike: float
    bid: float
    ask: float
    mark: float
    delta: float | None
    gamma: float | None
    theta: float | None
    open_interest: int
    total_volume: int
    days_to_expiration: int
    in_the_money: bool

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.mark

    @property
    def spread_pct(self) -> float:
        ref = self.mid or self.ask or self.bid or 0.0
        if ref <= 0 or self.ask <= 0 or self.bid < 0:
            return 1.0
        return max(0.0, (self.ask - self.bid) / ref)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def parse_option_chain(payload: dict[str, Any], only_dte: int | None = 0) -> list[OptionContract]:
    contracts: list[OptionContract] = []
    for root_key in ("callExpDateMap", "putExpDateMap"):
        exp_map = payload.get(root_key) or {}
        for exp_key, strikes in exp_map.items():
            try:
                exp_date, dte_txt = str(exp_key).split(":", 1)
                dte = int(float(dte_txt))
            except Exception:
                exp_date = str(exp_key)
                dte = _safe_int(payload.get("daysToExpiration"), -1)
            if only_dte is not None and dte != only_dte:
                continue
            for strike_key, entries in (strikes or {}).items():
                for entry in entries or []:
                    contracts.append(
                        OptionContract(
                            symbol=str(entry.get("symbol") or ""),
                            expiration=exp_date,
                            put_call=str(entry.get("putCall") or ("CALL" if root_key.startswith("call") else "PUT")),
                            strike=safe_float(entry.get("strikePrice") or strike_key, 0.0),
                            bid=safe_float(entry.get("bid"), 0.0),
                            ask=safe_float(entry.get("ask"), 0.0),
                            mark=safe_float(entry.get("mark") or entry.get("last"), 0.0),
                            delta=(None if entry.get("delta") in (None, "NaN") else safe_float(entry.get("delta"))),
                            gamma=(None if entry.get("gamma") in (None, "NaN") else safe_float(entry.get("gamma"))),
                            theta=(None if entry.get("theta") in (None, "NaN") else safe_float(entry.get("theta"))),
                            open_interest=_safe_int(entry.get("openInterest")),
                            total_volume=_safe_int(entry.get("totalVolume") or entry.get("volume")),
                            days_to_expiration=dte,
                            in_the_money=bool(entry.get("inTheMoney", False)),
                        )
                    )
    return [c for c in contracts if c.symbol]


def filter_contracts(
    contracts: Iterable[OptionContract],
    put_call: str,
    min_volume: int,
    min_open_interest: int,
    max_bid_ask_spread_pct: float,
) -> list[OptionContract]:
    out: list[OptionContract] = []
    want = put_call.upper()
    for c in contracts:
        if c.put_call.upper() != want:
            continue
        if c.ask <= 0 or c.bid < 0:
            continue
        if c.total_volume < min_volume or c.open_interest < min_open_interest:
            continue
        if c.spread_pct > max_bid_ask_spread_pct:
            continue
        out.append(c)
    return sorted(out, key=lambda c: (c.expiration, c.strike))


def choose_by_delta(contracts: Iterable[OptionContract], target_abs_delta: float, above_strike: float | None = None, below_strike: float | None = None) -> OptionContract | None:
    best: tuple[float, OptionContract] | None = None
    for c in contracts:
        if above_strike is not None and c.strike <= above_strike:
            continue
        if below_strike is not None and c.strike >= below_strike:
            continue
        abs_delta = abs(c.delta) if c.delta is not None else 99.0
        score = abs(abs_delta - abs(target_abs_delta))
        if best is None or score < best[0]:
            best = (score, c)
    return best[1] if best else None


def choose_nearest_strike(contracts: Iterable[OptionContract], target_strike: float, direction: str) -> OptionContract | None:
    direction = direction.lower()
    best: tuple[float, OptionContract] | None = None
    for c in contracts:
        if direction == "higher" and c.strike < target_strike:
            continue
        if direction == "lower" and c.strike > target_strike:
            continue
        score = abs(c.strike - target_strike)
        if best is None or score < best[0]:
            best = (score, c)
    return best[1] if best else None


def build_vertical_order(
    long_leg: OptionContract,
    short_leg: OptionContract,
    side: Side,
    qty: int,
    limit_price: float | None = None,
) -> dict[str, Any]:
    if side == Side.LONG:
        legs = [
            {
                "instruction": OrderIntent.BUY_TO_OPEN.value,
                "quantity": qty,
                "instrument": {"symbol": long_leg.symbol, "assetType": "OPTION"},
            },
            {
                "instruction": OrderIntent.SELL_TO_OPEN.value,
                "quantity": qty,
                "instrument": {"symbol": short_leg.symbol, "assetType": "OPTION"},
            },
        ]
    else:
        legs = [
            {
                "instruction": OrderIntent.SELL_TO_OPEN.value,
                "quantity": qty,
                "instrument": {"symbol": long_leg.symbol, "assetType": "OPTION"},
            },
            {
                "instruction": OrderIntent.BUY_TO_OPEN.value,
                "quantity": qty,
                "instrument": {"symbol": short_leg.symbol, "assetType": "OPTION"},
            },
        ]
    order = {
        "orderType": "NET_DEBIT" if side == Side.LONG else "NET_CREDIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "VERTICAL",
        "orderLegCollection": legs,
    }
    if limit_price is not None:
        order["price"] = f"{limit_price:.2f}"
    return order


def build_vertical_close_order(position_metadata: dict[str, Any], qty: int, limit_price: float | None = None) -> dict[str, Any]:
    side = Side(position_metadata.get("spread_side", Side.LONG.value))
    long_symbol = position_metadata["long_leg_symbol"]
    short_symbol = position_metadata["short_leg_symbol"]
    if side == Side.LONG:
        legs = [
            {
                "instruction": OrderIntent.SELL_TO_CLOSE.value,
                "quantity": qty,
                "instrument": {"symbol": long_symbol, "assetType": "OPTION"},
            },
            {
                "instruction": OrderIntent.BUY_TO_CLOSE.value,
                "quantity": qty,
                "instrument": {"symbol": short_symbol, "assetType": "OPTION"},
            },
        ]
    else:
        legs = [
            {
                "instruction": OrderIntent.BUY_TO_CLOSE.value,
                "quantity": qty,
                "instrument": {"symbol": short_symbol, "assetType": "OPTION"},
            },
            {
                "instruction": OrderIntent.SELL_TO_CLOSE.value,
                "quantity": qty,
                "instrument": {"symbol": long_symbol, "assetType": "OPTION"},
            },
        ]
    order = {
        "orderType": "NET_CREDIT" if side == Side.LONG else "NET_DEBIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "VERTICAL",
        "orderLegCollection": legs,
    }
    if limit_price is not None:
        order["price"] = f"{limit_price:.2f}"
    return order


def build_position_label(underlying: str, style: str, side: Side, long_leg: OptionContract, short_leg: OptionContract) -> str:
    if side == Side.LONG:
        # Debit spreads: bull call / bear put
        orient = "BULL" if long_leg.put_call == "CALL" else "BEAR"
    else:
        # Credit spreads: bear call (hedge is the CALL) / bull put (hedge is the PUT)
        orient = "BEAR" if long_leg.put_call == "CALL" else "BULL"
    return f"{underlying} {style} {orient} {long_leg.expiration} {long_leg.strike:g}/{short_leg.strike:g} {long_leg.put_call[0]}"


def net_debit_dollars(long_leg: OptionContract, short_leg: OptionContract) -> tuple[float, float]:
    conservative = max(0.0, (long_leg.ask - short_leg.bid) * 100.0)
    mid = max(0.0, (long_leg.mid - short_leg.mid) * 100.0)
    return conservative, mid


def net_credit_dollars(short_leg: OptionContract, long_leg: OptionContract) -> tuple[float, float, float]:
    conservative = max(0.0, (short_leg.bid - long_leg.ask) * 100.0)
    mid = max(0.0, (short_leg.mid - long_leg.mid) * 100.0)
    width = abs(short_leg.strike - long_leg.strike) * 100.0
    max_loss = max(0.0, width - conservative)
    return conservative, mid, max_loss


def _round_net_price(value: float) -> float:
    return round(max(0.01, float(value)), 2)


def vertical_price_bounds(first_leg: OptionContract, second_leg: OptionContract) -> tuple[float, float, float]:
    bid = max(0.0, float(first_leg.bid) - float(second_leg.ask))
    ask = max(0.0, float(first_leg.ask) - float(second_leg.bid))
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
    else:
        mid = max(0.0, float(first_leg.mid) - float(second_leg.mid))
    return bid, ask, mid


def vertical_limit_price(first_leg: OptionContract, second_leg: OptionContract, mode: str = "mid") -> float:
    bid, ask, mid = vertical_price_bounds(first_leg, second_leg)
    mode = str(mode or "mid").lower()
    if mode == "natural":
        price = ask if ask > 0 else (mid if mid > 0 else bid)
    elif mode == "bid":
        price = bid if bid > 0 else (mid if mid > 0 else ask)
    else:
        price = mid if mid > 0 else (ask if ask > 0 else bid)
    return _round_net_price(price)


def contract_from_quote(symbol: str, quote: dict[str, Any] | None, fallback: dict[str, Any] | None = None) -> OptionContract:
    quote = quote or {}
    fallback = fallback or {}

    def pick(key: str, default: Any = 0.0) -> Any:
        val = quote.get(key)
        if val in (None, ""):
            val = fallback.get(key, default)
        return val

    def pick_greek(key: str, alt_key: str | None = None) -> float | None:
        # Fresh quote first, then fallback metadata. Treat NaN/None/"" as missing.
        for source in (quote, fallback):
            val = source.get(key) if source else None
            if val in (None, "", "NaN"):
                if alt_key:
                    val = source.get(alt_key) if source else None
            if val not in (None, "", "NaN"):
                try:
                    return safe_float(val)
                except Exception:
                    continue
        return None

    return OptionContract(
        symbol=symbol,
        expiration=str(fallback.get("expiration") or ""),
        put_call=str(fallback.get("put_call") or fallback.get("putCall") or "CALL"),
        strike=safe_float(fallback.get("strike") or fallback.get("strikePrice"), 0.0),
        bid=safe_float(pick("bid"), 0.0),
        ask=safe_float(pick("ask"), 0.0),
        mark=safe_float(pick("mark", pick("last")), 0.0),
        delta=pick_greek("delta"),
        gamma=pick_greek("gamma"),
        theta=pick_greek("theta"),
        open_interest=_safe_int(pick("open_interest", pick("openInterest", 0))),
        total_volume=_safe_int(pick("total_volume", pick("totalVolume", pick("volume", 0)))),
        days_to_expiration=_safe_int(pick("days_to_expiration", pick("daysToExpiration", 0))),
        in_the_money=bool(quote.get("in_the_money") if "in_the_money" in quote else (quote.get("inTheMoney") if "inTheMoney" in quote else fallback.get("in_the_money") or fallback.get("inTheMoney", False))),
    )


def close_limit_price_from_metadata(position_metadata: dict[str, Any], first_quote: dict[str, Any] | None, second_quote: dict[str, Any] | None, mode: str = "mid") -> float | None:
    spread_side = Side(position_metadata.get("spread_side", Side.LONG.value))
    long_symbol = str(position_metadata.get("long_leg_symbol") or "")
    short_symbol = str(position_metadata.get("short_leg_symbol") or "")
    if not long_symbol or not short_symbol:
        return None
    if spread_side == Side.LONG:
        first_symbol, second_symbol = long_symbol, short_symbol
        first_meta, second_meta = position_metadata.get("long_leg"), position_metadata.get("short_leg")
    else:
        first_symbol, second_symbol = short_symbol, long_symbol
        first_meta, second_meta = position_metadata.get("short_leg"), position_metadata.get("long_leg")
    first_leg = contract_from_quote(first_symbol, first_quote, first_meta)
    second_leg = contract_from_quote(second_symbol, second_quote, second_meta)
    return vertical_limit_price(first_leg, second_leg, mode=mode)


def build_single_option_order(contract: OptionContract, qty: int, limit_price: float | None = None) -> dict[str, Any]:
    order = {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": OrderIntent.BUY_TO_OPEN.value,
                "quantity": qty,
                "instrument": {"symbol": contract.symbol, "assetType": "OPTION"},
            }
        ],
    }
    if limit_price is not None:
        order["price"] = f"{limit_price:.2f}"
    return order


def build_single_option_close_order(position_metadata: dict[str, Any], qty: int, limit_price: float | None = None) -> dict[str, Any]:
    symbol = str(position_metadata.get("option_symbol") or position_metadata.get("long_leg_symbol") or "")
    order = {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": OrderIntent.SELL_TO_CLOSE.value,
                "quantity": qty,
                "instrument": {"symbol": symbol, "assetType": "OPTION"},
            }
        ],
    }
    if limit_price is not None:
        order["price"] = f"{limit_price:.2f}"
    return order


def build_single_option_position_label(underlying: str, style: str, contract: OptionContract) -> str:
    orient = "CALL" if contract.put_call.upper() == "CALL" else "PUT"
    return f"{underlying} {style} {orient} {contract.expiration} {contract.strike:g}"


def single_option_dollars(contract: OptionContract) -> tuple[float, float]:
    conservative = max(0.0, float(contract.ask) * 100.0)
    mid = max(0.0, float(contract.mid) * 100.0)
    return conservative, mid


def single_option_price_bounds(contract: OptionContract) -> tuple[float, float, float]:
    bid = max(0.0, float(contract.bid))
    ask = max(0.0, float(contract.ask))
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
    else:
        mid = max(0.0, float(contract.mid) or float(contract.mark) or 0.0)
    return bid, ask, mid


def single_option_limit_price(contract: OptionContract, mode: str = "mid", opening: bool = True) -> float:
    bid, ask, mid = single_option_price_bounds(contract)
    mode = str(mode or "mid").lower()
    if mode == "natural":
        price = ask if opening else bid
        if price <= 0:
            price = mid if mid > 0 else (ask if ask > 0 else bid)
    elif mode == "bid":
        price = bid if bid > 0 else (mid if mid > 0 else ask)
    else:
        price = mid if mid > 0 else (ask if ask > 0 else bid)
    return _round_net_price(price)


def close_single_option_limit_from_metadata(position_metadata: dict[str, Any], quote: dict[str, Any] | None, mode: str = "mid") -> float | None:
    symbol = str(position_metadata.get("option_symbol") or "")
    if not symbol:
        return None
    contract = contract_from_quote(symbol, quote, position_metadata.get("option_leg"))
    return single_option_limit_price(contract, mode=mode, opening=False)
