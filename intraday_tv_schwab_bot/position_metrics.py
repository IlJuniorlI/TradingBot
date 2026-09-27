# SPDX-License-Identifier: MIT
"""Pure position-metric helpers extracted from ``IntradayBot``, and the
capped management-adjustment log a position's metadata carries.

These are ``@staticmethod`` helpers with no engine-side dependencies — just
:class:`~intraday_tv_schwab_bot.models.Position` inputs and scalar math. Moved
into their own module so future callers (PositionManager, reports, tests)
can import them without dragging the whole engine surface along.
"""
from __future__ import annotations

from typing import Any

from .models import Position, Side
from .numeric import safe_float
from .reasons import exit_reason_code


def position_unrealized_at_price(position: Position, price: float | None) -> float | None:
    """Return the unrealized P&L (in quote currency, not %) if ``position``
    were marked at ``price``. None if ``price`` is None."""
    if price is None:
        return None
    entry = float(position.entry_price)
    qty = int(position.qty)
    if position.side == Side.LONG:
        return (float(price) - entry) * qty
    return (entry - float(price)) * qty


def position_return_pct_at_price(position: Position, price: float | None) -> float | None:
    """Return % P&L relative to entry price.

    SHORT convention: a short from 100 → 90 is +10.0% (measured against
    entry basis), NOT +11.1% as the naive (entry / current - 1) formula
    would report. Mirrors paper_account._return_pct."""
    if price in (None, 0.0) or not float(position.entry_price):
        return None
    entry = float(position.entry_price)
    current = float(price)
    if position.side == Side.LONG:
        return ((current / entry) - 1.0) * 100.0
    return (1.0 - (current / entry)) * 100.0


# The adaptive ladder's touch hold (shared_exit.adaptive_ladder_touch_hold):
# the position metadata key of a hold in progress, which keeps
# RiskManager.update_position off the target, and the codes of the exits
# the hold takes instead (see PositionManager._adaptive_ladder_management).
LADDER_TOUCH_HOLD_KEY = "ladder_touch_hold"
TARGET_WEAK_CLOSE = "target_weak_close"
TARGET_HOLD_GUARD = "target_hold_guard"
TARGET_HOLD_TIMEOUT = "target_hold_timeout"
# The exit codes of the "risk" family: the stop and the target, and the touch
# hold's three exits at the target.
RISK_EXIT_CODES = frozenset({"stop", "target", TARGET_WEAK_CLOSE, TARGET_HOLD_GUARD, TARGET_HOLD_TIMEOUT})


def exit_reason_details(reason: str) -> dict[str, Any]:
    """Classify an exit reason string into family + code + optional trigger
    level. Used for structured event logging at exit time.

    Input format: ``"<code>"`` or ``"<code>:<level>"`` (e.g. ``"stop:99.5"``).
    Output dict includes ``exit_reason`` (raw), ``exit_reason_code``,
    ``exit_reason_family`` (one of: risk, schedule, technical, strategy),
    and ``exit_trigger_level`` (float or None)."""
    raw = str(reason or "").strip()
    code = exit_reason_code(raw)
    level_text = raw.partition(":")[2]
    family = "strategy"
    if code in RISK_EXIT_CODES:
        family = "risk"
    elif code in {"time_exit", "force_flatten", "session_exit"}:
        family = "schedule"
    elif isinstance(code, str) and any(token in code for token in ("trendline", "channel_", "bollinger_", "anchored_vwap")):
        family = "technical"
    return {
        "exit_reason": raw or None,
        "exit_reason_code": code,
        "exit_reason_family": family,
        # The level after the colon when it is a number; a word there
        # (``candle_pattern_exit:CDLENGULFING``) or a NaN is no level.
        "exit_trigger_level": safe_float(level_text),
    }


_MAX_MANAGEMENT_ADJUSTMENTS = 200


# The position metadata key naming the management step that last moved the
# stop, ``<manager>:<reason>`` (``adaptive:profit_lock``, ``adaptive:trail``,
# ``adaptive_ladder:touch_promoted``, ...), set by every stop move through
# append_management_adjustment. Absent while the stop is the one the position
# opened (or was restored) with; EXIT_CONTEXT reads that as ``initial``, so a
# stop exit says which ratchet's level it hit.
STOP_SOURCE_KEY = "stop_source"


def append_management_adjustment(meta: dict, entry: dict) -> None:
    """Append a management adjustment to position metadata with a size cap,
    and record a stop move's manager and reason under ``STOP_SOURCE_KEY``.

    Keeps the most recent ``_MAX_MANAGEMENT_ADJUSTMENTS`` entries so the list
    doesn't grow without bound on very active trades.
    """
    if entry.get("kind") == "stop":
        meta[STOP_SOURCE_KEY] = f"{entry.get('manager')}:{entry.get('reason')}"
    adjustments = meta.setdefault("management_adjustments", [])
    adjustments.append(entry)
    if len(adjustments) > _MAX_MANAGEMENT_ADJUSTMENTS:
        del adjustments[: len(adjustments) - _MAX_MANAGEMENT_ADJUSTMENTS]
