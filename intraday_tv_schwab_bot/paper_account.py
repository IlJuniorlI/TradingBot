# SPDX-License-Identifier: MIT
from __future__ import annotations

import copy
import logging
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from threading import RLock
from typing import Any

from .models import ASSET_TYPE_EQUITY, ASSET_TYPE_OPTION_SINGLE, ASSET_TYPE_OPTION_VERTICAL, Position, Side
from .utils import TRADEFLOW_LEVEL, now_et, register_tradeflow_logging_level

LOG = logging.getLogger(__name__)
register_tradeflow_logging_level()


def _return_pct(side: Side, entry_price: float, last_or_exit_price: float) -> float:
    if not entry_price:
        return 0.0
    if side == Side.LONG:
        return ((last_or_exit_price / entry_price) - 1.0) * 100.0
    # Short: profit % is measured relative to the entry (= margin committed),
    # not the exit. A short from 100 → cover at 90 is a 10% gain on entry,
    # NOT 11.1% (which is what (entry/exit - 1) reports).
    return (1.0 - (last_or_exit_price / entry_price)) * 100.0


@dataclass(slots=True)
class TradeRecord:
    symbol: str
    strategy: str
    side: str
    qty: int
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    realized_pnl: float
    return_pct: float
    hold_minutes: float
    reason: str
    asset_type: str = ASSET_TYPE_EQUITY
    underlying: str | None = None
    exchange: str | None = None
    option_type: str | None = None  # "CALL" / "PUT" for option trades; None for stocks
    lifecycle_id: str | None = None
    partial_exit: bool = False
    final_exit: bool = True
    remaining_qty_after_exit: int = 0
    fill_price_estimated: bool = False
    broker_recovered: bool = False
    # Diagnostic fields populated from position.metadata at exit time.
    # These power the per-regime / MAE / MFE / slippage aggregates in the
    # end-of-day session report. All are optional — older trades won't
    # have them and the aggregator should handle None gracefully.
    regime: str | None = None
    initial_risk_per_unit: float | None = None   # abs(entry - initial_stop)
    max_favorable_pnl: float | None = None       # peak unrealized PnL (MFE in $)
    max_adverse_pnl: float | None = None         # trough unrealized PnL (MAE in $)
    entry_slippage_pct: float | None = None      # |fill - signal| / signal


@dataclass(slots=True)
class EquityPoint:
    timestamp: datetime
    equity: float
    cash: float
    market_value: float
    realized_pnl: float
    unrealized_pnl: float
    open_positions: int
    per_symbol_equity: dict[str, float]


class PaperAccount:
    """Internal paper ledger used for dry-run mode and estimated live tracking."""

    @staticmethod
    def _trade_lifecycle_id(position: Position) -> str:
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        base = str(meta.get("position_key") or position.symbol)
        try:
            entry_ts = position.entry_time.isoformat()
        except Exception:
            entry_ts = str(position.entry_time)
        return f"{base}|{entry_ts}"

    def __init__(self, starting_equity: float, max_equity_points: int = 2000, max_trade_history: int = 200):
        self.starting_equity = float(starting_equity)
        self.cash = float(starting_equity)
        self.realized_pnl = 0.0
        self.last_prices: dict[str, float] = {}
        self.realized_pnl_by_symbol: dict[str, float] = {}
        self.trades: deque[TradeRecord] = deque(maxlen=max_trade_history)
        self.equity_curve: deque[EquityPoint] = deque(maxlen=max_equity_points)
        self.peak_equity = float(starting_equity)
        self.max_drawdown = 0.0
        self._lock = RLock()
        self.capture_snapshot({}, now_et())

    def mark_prices(self, prices: dict[str, float]) -> None:
        with self._lock:
            for symbol, price in prices.items():
                if price is None:
                    continue
                try:
                    self.last_prices[str(symbol)] = float(price)
                except Exception:
                    continue

    def record_entry(self, position: Position, fill_price: float) -> None:
        fill_price = float(fill_price)
        with self._lock:
            if position.side == Side.LONG:
                self.cash -= position.qty * fill_price
            else:
                self.cash += position.qty * fill_price
            self.last_prices[position.symbol] = fill_price
            LOG.log(TRADEFLOW_LEVEL, "Paper account entry recorded %s qty=%s @ %.4f cash=%.2f", position.symbol, position.qty, fill_price, self.cash)

    def record_exit(
        self,
        position: Position,
        fill_price: float,
        reason: str,
        *,
        final_exit: bool = True,
        remaining_qty_after_exit: int = 0,
        fill_price_estimated: bool = False,
        broker_recovered: bool = False,
    ) -> float:
        fill_price = float(fill_price)
        with self._lock:
            if position.side == Side.LONG:
                self.cash += position.qty * fill_price
                realized = (fill_price - position.entry_price) * position.qty
            else:
                self.cash -= position.qty * fill_price
                realized = (position.entry_price - fill_price) * position.qty
            self.realized_pnl += realized
            self.realized_pnl_by_symbol[position.symbol] = self.realized_pnl_by_symbol.get(position.symbol, 0.0) + realized
            self.last_prices[position.symbol] = fill_price
            exit_ts = now_et()
            hold_minutes = max(0.0, (exit_ts - position.entry_time).total_seconds() / 60.0)
            metadata = position.metadata or {}
            # Pull diagnostic fields from metadata; all are optional so
            # trades from strategies that don't populate them stay valid.
            def _opt_float(*keys: str) -> float | None:
                """First non-None float from the given metadata keys.

                Tries each key in order so we can read either the clean
                name (initial_stop_price, entry_slippage_pct) or the
                engine's prefixed diagnostic key (diag_best_unrealized_pnl,
                diag_worst_unrealized_pnl). The engine writes MAE/MFE
                under the diag_ prefix during management; we prefer the
                clean name when set but fall back to the diag_ copy."""
                for key in keys:
                    try:
                        value = metadata.get(key)
                        if value is None:
                            continue
                        f = float(value)
                        if f == f:  # NaN guard
                            return f
                    except (TypeError, ValueError):
                        continue
                return None

            initial_stop = _opt_float("initial_stop_price")
            initial_risk = (
                abs(float(position.entry_price) - initial_stop)
                if initial_stop is not None and initial_stop > 0
                else None
            )
            trade = TradeRecord(
                symbol=position.symbol,
                strategy=str(position.strategy),
                side=str(position.side.value),
                qty=position.qty,
                entry_price=float(position.entry_price),
                exit_price=fill_price,
                entry_time=position.entry_time,
                exit_time=exit_ts,
                realized_pnl=realized,
                return_pct=_return_pct(position.side, float(position.entry_price), fill_price),
                hold_minutes=hold_minutes,
                reason=reason,
                asset_type=str(metadata.get("asset_type") or ASSET_TYPE_EQUITY),
                underlying=str(metadata.get("underlying") or "").upper().strip() or None,
                exchange=str(metadata.get("exchange") or "").upper().strip() or None,
                option_type=(str(metadata.get("option_type") or "").upper().strip() or None),
                lifecycle_id=self._trade_lifecycle_id(position),
                partial_exit=not bool(final_exit),
                final_exit=bool(final_exit),
                remaining_qty_after_exit=max(0, int(remaining_qty_after_exit)),
                fill_price_estimated=bool(fill_price_estimated),
                broker_recovered=bool(broker_recovered),
                regime=(str(metadata.get("regime")) if metadata.get("regime") else None),
                initial_risk_per_unit=initial_risk,
                max_favorable_pnl=_opt_float("best_unrealized_pnl", "diag_best_unrealized_pnl"),
                max_adverse_pnl=_opt_float("worst_unrealized_pnl", "diag_worst_unrealized_pnl"),
                entry_slippage_pct=_opt_float("entry_slippage_pct"),
            )
            # LIFO: newest trade at index 0 (consumers iterate from the left).
            self.trades.appendleft(trade)
            LOG.log(
                TRADEFLOW_LEVEL,
                "Paper account exit recorded %s realized=%.2f cash=%.2f final_exit=%s remaining_qty=%s broker_recovered=%s estimated_fill=%s",
                position.symbol,
                realized,
                self.cash,
                bool(final_exit),
                max(0, int(remaining_qty_after_exit)),
                bool(broker_recovered),
                bool(fill_price_estimated),
            )
            return realized

    @staticmethod
    def _position_market_value(position: Position, last_price: float) -> float:
        if position.side == Side.LONG:
            return position.qty * last_price
        return -position.qty * last_price

    @staticmethod
    def _position_unrealized(position: Position, last_price: float) -> float:
        if position.side == Side.LONG:
            return (last_price - position.entry_price) * position.qty
        return (position.entry_price - last_price) * position.qty

    def _position_summary(self, position: Position) -> dict[str, Any]:
        last_price = float(self.last_prices.get(position.symbol, position.entry_price))
        unrealized = self._position_unrealized(position, last_price)
        market_value = self._position_market_value(position, last_price)
        metadata = position.metadata or {}
        asset_type = str(metadata.get("asset_type") or ASSET_TYPE_EQUITY)
        breakeven = None
        _max_risk = None
        max_reward = None
        risk_label = None

        if asset_type == ASSET_TYPE_OPTION_VERTICAL:
            qty = int(position.qty)
            per_contract_risk = float(metadata.get("max_loss_per_contract") or 0.0)
            per_contract_reward = metadata.get("max_profit_per_contract")
            _max_risk = per_contract_risk * qty if per_contract_risk > 0 else None
            if per_contract_reward is not None:
                try:
                    max_reward = float(per_contract_reward) * qty
                except Exception:
                    max_reward = None
            be = metadata.get("breakeven_underlying")
            if be is not None:
                try:
                    breakeven = float(be)
                except Exception:
                    breakeven = None
            spread_type = metadata.get("spread_type")
            if spread_type:
                risk_label = str(spread_type)
        elif asset_type == ASSET_TYPE_OPTION_SINGLE:
            qty = int(position.qty)
            per_contract_risk = float(metadata.get("max_loss_per_contract") or 0.0)
            _max_risk = per_contract_risk * qty if per_contract_risk > 0 else None
            be = metadata.get("breakeven_underlying")
            if be is not None:
                try:
                    breakeven = float(be)
                except Exception:
                    breakeven = None
            risk_label = str(metadata.get("option_type") or "long_option").lower()
        else:
            if position.side == Side.LONG:
                risk_per_share = max(0.0, float(position.entry_price) - float(position.stop_price or position.entry_price))
                _max_risk = risk_per_share * int(position.qty)
                breakeven = None
                risk_label = "long_stock"
            else:
                risk_per_share = max(0.0, float(position.stop_price or position.entry_price) - float(position.entry_price))
                _max_risk = risk_per_share * int(position.qty)
                breakeven = None
                risk_label = "short_stock"

        if max_reward is None and position.target_price is not None:
            try:
                target_price = float(position.target_price)
                entry_price = float(position.entry_price)
                qty = int(position.qty)
                if position.side == Side.LONG:
                    reward_per_unit = max(0.0, target_price - entry_price)
                else:
                    reward_per_unit = max(0.0, entry_price - target_price)
                if reward_per_unit > 0:
                    max_reward = reward_per_unit * qty
            except Exception:
                max_reward = None

        return {
            "symbol": position.symbol,
            "strategy": position.strategy,
            "side": position.side.value,
            "qty": int(position.qty),
            "asset_type": asset_type,
            "risk_label": risk_label,
            "entry_price": float(position.entry_price),
            "last_price": last_price,
            "stop_price": float(position.stop_price),
            "target_price": float(position.target_price) if position.target_price is not None else None,
            "market_value": market_value,
            "unrealized_pnl": unrealized,
            "max_risk": _max_risk,
            "max_reward": max_reward,
            "breakeven": breakeven,
            "return_pct": _return_pct(position.side, float(position.entry_price), last_price),
            "entry_time": position.entry_time.isoformat(),
            "highest_price": float(position.highest_price) if position.highest_price is not None else None,
            "lowest_price": float(position.lowest_price) if position.lowest_price is not None else None,
            "reference_symbol": position.reference_symbol,
            "underlying": metadata.get("underlying"),
            "exchange": metadata.get("exchange"),
            "spread_type": metadata.get("spread_type"),
            "spread_style": metadata.get("spread_style"),
            "option_type": metadata.get("option_type"),
            "option_strike": metadata.get("option_strike"),
            "long_strike": metadata.get("long_strike"),
            "short_strike": metadata.get("short_strike"),
            "sold_strike": metadata.get("sold_strike"),
            "bought_strike": metadata.get("bought_strike"),
        }

    def capture_snapshot(self, positions: dict[str, Position], timestamp: datetime | None = None) -> dict[str, Any]:
        ts = timestamp or now_et()
        with self._lock:
            position_rows = [self._position_summary(position) for position in positions.values()]
            market_value = sum(row["market_value"] for row in position_rows)
            gross_market_value = sum(abs(float(row["market_value"])) for row in position_rows)
            gross_max_risk = sum(abs(float(row["max_risk"])) for row in position_rows if row.get("max_risk") is not None)
            unrealized_pnl = sum(row["unrealized_pnl"] for row in position_rows)
            total_equity = self.cash + market_value
            self.peak_equity = max(self.peak_equity, total_equity)
            current_drawdown = self.peak_equity - total_equity
            self.max_drawdown = max(self.max_drawdown, current_drawdown)
            unrealized_by_symbol: dict[str, float] = {}
            for row in position_rows:
                symbol = str(row["symbol"])
                unrealized_by_symbol[symbol] = unrealized_by_symbol.get(symbol, 0.0) + float(row["unrealized_pnl"])
            all_symbols = sorted(set(self.realized_pnl_by_symbol) | set(unrealized_by_symbol))
            per_symbol_equity = {
                symbol: float(self.realized_pnl_by_symbol.get(symbol, 0.0) + unrealized_by_symbol.get(symbol, 0.0))
                for symbol in all_symbols
            }
            point = EquityPoint(
                timestamp=ts,
                equity=total_equity,
                cash=self.cash,
                market_value=market_value,
                realized_pnl=self.realized_pnl,
                unrealized_pnl=unrealized_pnl,
                open_positions=len(position_rows),
                per_symbol_equity=per_symbol_equity,
            )
            if not self.equity_curve or self.equity_curve[-1].timestamp != point.timestamp:
                self.equity_curve.append(point)
            else:
                self.equity_curve[-1] = point

            trade_events = list(self.trades)
            closed = [trade for trade in trade_events if bool(getattr(trade, "final_exit", True))]
            wins = sum(1 for trade in closed if trade.realized_pnl > 0)
            losses = sum(1 for trade in closed if trade.realized_pnl < 0)
            gross_profit = sum(trade.realized_pnl for trade in closed if trade.realized_pnl > 0)
            gross_loss = abs(sum(trade.realized_pnl for trade in closed if trade.realized_pnl < 0))
            profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

            return {
                "starting_equity": self.starting_equity,
                "cash": self.cash,
                "market_value": market_value,
                "gross_market_value": gross_market_value,
                "gross_max_risk": gross_max_risk,
                "total_equity": total_equity,
                "peak_equity": self.peak_equity,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "drawdown": current_drawdown,
                "max_drawdown": self.max_drawdown,
                "open_positions": len(position_rows),
                "closed_trades": len(closed),
                "total_trades": len(closed) + len(position_rows),
                "wins": wins,
                "losses": losses,
                "win_rate": (wins / len(closed)) if closed else None,
                "average_trade": (sum(trade.realized_pnl for trade in closed) / len(closed)) if closed else None,
                "profit_factor": profit_factor,
                "positions": position_rows,
                "recent_trades": [self._trade_to_dict(trade) for trade in closed[:20]],
                "recent_trade_events": [self._trade_to_dict(trade) for trade in trade_events[:20]],
                "equity_curve": [self._equity_point_to_dict(item) for item in self.equity_curve],
            }

    @staticmethod
    def _trade_to_dict(trade: TradeRecord) -> dict[str, Any]:
        payload = asdict(trade)
        payload["entry_time"] = trade.entry_time.isoformat()
        payload["exit_time"] = trade.exit_time.isoformat()
        return payload

    @staticmethod
    def _equity_point_to_dict(point: EquityPoint) -> dict[str, Any]:
        payload = asdict(point)
        payload["timestamp"] = point.timestamp.isoformat()
        return payload

    def snapshot_copy(self, positions: dict[str, Position]) -> dict[str, Any]:
        return copy.deepcopy(self.capture_snapshot(positions))
