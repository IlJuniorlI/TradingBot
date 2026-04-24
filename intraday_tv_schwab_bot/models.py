# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Any


DEFAULT_RUNTIME_TZ = "America/New_York"


# Strategy identifiers now live with each plugin manifest/class rather than in a
# central registry helper. Runtime models intentionally keep `strategy` typed as
# plain strings so new plugins can be added without touching this module.


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


# Asset-type string constants used in ``position.metadata["asset_type"]`` and
# signal metadata across entry/exit/risk/restore paths. Bare string literals
# ("OPTION_VERTICAL" etc.) proliferated in 40+ call sites across 9 modules
# before these constants were extracted — kept as plain strings rather than
# a StrEnum because the values are serialized into metadata dicts and
# round-tripped through reconcile metadata storage, where a string is more
# portable than an enum member.
ASSET_TYPE_EQUITY = "EQUITY"
ASSET_TYPE_OPTION_VERTICAL = "OPTION_VERTICAL"
ASSET_TYPE_OPTION_SINGLE = "OPTION_SINGLE"
OPTION_ASSET_TYPES: frozenset[str] = frozenset({ASSET_TYPE_OPTION_VERTICAL, ASSET_TYPE_OPTION_SINGLE})


class OrderIntent(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    SELL_SHORT = "SELL_SHORT"
    BUY_TO_COVER = "BUY_TO_COVER"
    BUY_TO_OPEN = "BUY_TO_OPEN"
    SELL_TO_OPEN = "SELL_TO_OPEN"
    BUY_TO_CLOSE = "BUY_TO_CLOSE"
    SELL_TO_CLOSE = "SELL_TO_CLOSE"


@dataclass(slots=True)
class Window:
    start: time
    end: time

    def contains(self, value: time) -> bool:
        if self.start <= self.end:
            return self.start <= value <= self.end
        # Overnight window (e.g., 22:00 to 02:00): matches before midnight OR after midnight
        return value >= self.start or value <= self.end


@dataclass(slots=True)
class StrategySchedule:
    entry_windows: list[Window]
    management_windows: list[Window]
    screener_windows: list[Window]

    def can_enter(self, t: time) -> bool:
        return any(w.contains(t) for w in self.entry_windows)

    def can_manage(self, t: time) -> bool:
        return any(w.contains(t) for w in self.management_windows)

    def should_screen(self, t: time) -> bool:
        return any(w.contains(t) for w in self.screener_windows)


@dataclass(slots=True)
class Candidate:
    symbol: str
    strategy: str
    rank: int
    activity_score: float
    directional_bias: Side | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    symbol: str
    strategy: str
    side: Side
    qty: int
    entry_price: float
    entry_time: datetime
    stop_price: float
    target_price: float | None
    trail_pct: float | None = None
    highest_price: float | None = None
    lowest_price: float | None = None
    pair_id: str | None = None
    reference_symbol: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def update_extremes(self, price: float) -> None:
        if self.highest_price is None or price > self.highest_price:
            self.highest_price = price
        if self.lowest_price is None or price < self.lowest_price:
            self.lowest_price = price


@dataclass(slots=True)
class Signal:
    symbol: str
    strategy: str
    side: Side
    reason: str
    stop_price: float
    target_price: float | None
    reference_symbol: str | None = None
    pair_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PairDefinition:
    symbol: str
    reference: str
    side_preference: str = "both"
    sector: str | None = None
    industry: str | None = None


@dataclass(slots=True)
class OrderResult:
    ok: bool
    order_id: str | None
    raw: Any
    message: str
    fill_price: float | None = None
    filled_qty: int | None = None
    simulated: bool = False
