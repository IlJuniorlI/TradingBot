# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Any


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


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """What the exit pipeline decided for one position this cycle.

    ``family`` names who decided -- ``risk`` (stop / target / trail /
    peak-giveback), a shared exit family (``time_stop``, ``chart_pattern``,
    ``structure_choch``, ...), ``strategy`` (a strategy's own
    ``strategy_exit_signal``), ``divergence_partial`` or ``force_flatten``.
    ``fraction`` is the share of the CURRENT quantity to close; below 1.0 it
    is a scale-out, which the position manager sizes with a floor and holds
    when that rounds to zero units. ``marker`` is the one-shot record the
    manager appends to ``metadata['<family>_exits']`` once the slice books,
    so the same trigger cannot scale the position out on every cycle.

    Until 2026-09-24 exits were a ``(should_exit, reason)`` tuple and every
    exit closed the whole position, so a partial-close decision had no way
    to be expressed.
    """

    reason: str
    family: str
    fraction: float = 1.0
    marker: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not 0.0 < float(self.fraction) <= 1.0:
            raise ValueError(f"ExitDecision.fraction must be in (0, 1], got {self.fraction!r} ({self.reason})")

    @property
    def is_partial(self) -> bool:
        return self.fraction < 1.0


@dataclass(slots=True)
class PairDefinition:
    symbol: str
    reference: str
    side_preference: str = "both"
    sector: str | None = None
    industry: str | None = None


# noinspection PyDataclass
#   PyCharm's checker mis-reports field access on a `@dataclass(slots=True)`
#   instance as "object has no attribute '<field>'". It flags every
#   `result.bracket = ...` in execution.py even though `bracket` is a declared
#   field and is present in __slots__. Suppressed rather than dropping
#   slots=True: OrderResult is constructed per order, so the slots benefit is
#   real, unlike RiskState (a singleton) where slots was turned off for the
#   same false positive — see the RiskState docstring in risk.py.
@dataclass(slots=True)
class OrderResult:
    ok: bool
    order_id: str | None
    raw: Any
    message: str
    fill_price: float | None = None
    filled_qty: int | None = None
    simulated: bool = False
    # Broker-side bracket state for an equity entry submitted as a
    # first-triggers-OCO order: the parent/child order ids and the levels the
    # broker is actually resting at. None for every non-bracketed order.
    # Consumed by entry_gatekeeper to stamp position metadata, and by
    # position_manager to reconcile broker fills and replace-sync levels.
    bracket: dict[str, Any] | None = None
    # True when the order reached the broker and was NOT confirmed terminal:
    # a MARKET exit deliberately left working past its poll window (a halt),
    # or a cancel the broker never confirmed. The order can still fill, so the
    # caller must track ``order_id`` until it is terminal rather than send a
    # second order for the same shares.
    may_still_be_working: bool = False
