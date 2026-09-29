# SPDX-License-Identifier: MIT
"""Pure position math, and the capped management-adjustment log a
position's metadata carries.

Plain functions of a :class:`~intraday_tv_schwab_bot.models.Position` (or a
side, an entry and a price) with no engine-side dependencies: the paper
account, the position manager, the trade manager and the exit side read a
position's move, return, R and underlying price space here. Each caller
keeps its own answer for degenerate input (a zero entry, no price).
"""
from __future__ import annotations

from typing import Any

from .models import ExitDecision, Position, Side, is_option_asset
from .numeric import safe_float
from .reasons import exit_reason_code


def favorable_move(side: Side, entry: float, price: float) -> float:
    """The per-unit move from ``entry`` to ``price``, positive the trade's
    way: ``price - entry`` for a LONG, ``entry - price`` for a SHORT."""
    return (price - entry) if side == Side.LONG else (entry - price)


def return_pct(side: Side, entry: float, price: float) -> float:
    """The % return from ``entry`` to ``price``, on the entry basis for
    both sides: a SHORT from 100 covered at 90 is +10.0%, not the +11.1% of
    ``entry / price - 1``. ``entry`` must not be 0; each caller answers for a
    zero one (the paper account 0.0, ``position_return_pct_at_price`` None)."""
    ratio = price / entry
    if side == Side.LONG:
        return (ratio - 1.0) * 100.0
    return (1.0 - ratio) * 100.0


def position_unrealized_at_price(position: Position, price: float | None) -> float | None:
    """Return the unrealized P&L (in quote currency, not %) if ``position``
    were marked at ``price``. None if ``price`` is None."""
    if price is None:
        return None
    return favorable_move(position.side, float(position.entry_price), float(price)) * int(position.qty)


def position_return_pct_at_price(position: Position, price: float | None) -> float | None:
    """``return_pct`` of ``position`` marked at ``price``; None without a
    price, at a price of 0 or with a zero entry."""
    if price in (None, 0.0) or not float(position.entry_price):
        return None
    return return_pct(position.side, float(position.entry_price), float(price))


def initial_risk_per_unit(position: Position) -> float | None:
    """The per-unit risk ``position`` was opened with: |entry - initial
    stop|, from ``metadata['initial_stop_price']`` (stamped at entry, and
    since 2026-09-28 at a ``restore_basic`` restore). Unlike
    ``position_r_multiple`` it never falls back to the current stop, which
    break-even and the trail move toward the entry. None when the position
    carries no initial stop or the distance is not a positive finite number.
    """
    meta = position.metadata if isinstance(position.metadata, dict) else {}
    entry = safe_float(position.entry_price, finite=True)
    initial_stop = safe_float(meta.get("initial_stop_price"), finite=True)
    if entry is None or initial_stop is None:
        return None
    risk = abs(entry - initial_stop)
    return risk if risk > 0 else None


def position_r_multiple(position: Position, close: float) -> float | None:
    """Open profit at ``close`` in initial-risk (R) units.

    Anchors to ``metadata['initial_stop_price']`` — stamped once at entry
    by the gatekeeper — rather than ``position.stop_price``, which moves
    with breakeven/trailing management and would make R drift over the
    life of the trade. Returns None when the initial risk is unknown or
    degenerate, which callers treat as "no opinion".

    An option position's entry and stop are PREMIUM while ``close`` is the
    underlying's, so its R is measured on the option's own mark (stamped
    each cycle by the position manager) -- dividing an underlying move by
    a premium risk read a debit position as ~+7R (discretionary exits
    always armed) and a credit spread as ~-5R (never armed).
    """
    meta = position.metadata if isinstance(position.metadata, dict) else {}
    entry = safe_float(position.entry_price)
    initial_stop = safe_float(meta.get("initial_stop_price"), safe_float(position.stop_price, finite=True), finite=True)
    if entry is None or initial_stop is None:
        return None
    risk = abs(entry - initial_stop)
    if risk <= 0:
        return None
    price: float | None = close
    if is_option_asset(meta):
        price = safe_float(meta.get("last_mark_price"))
        if price is None:
            return None
    return favorable_move(position.side, entry, price) / risk


def underlying_entry_price(position: Position) -> float | None:
    """Entry in the price space of the frame the exit logic reads.

    An equity's own entry. An option's ``entry_price`` is premium, so its
    underlying's price at entry (``underlying_entry``, stamped by every
    option signal builder) -- None when that was never recorded, which
    callers treat as "no opinion".
    """
    meta = position.metadata if isinstance(position.metadata, dict) else {}
    if not is_option_asset(meta):
        return safe_float(position.entry_price)
    return safe_float(meta.get("underlying_entry"))


def underlying_extremes(position: Position) -> tuple[float | None, float | None]:
    """(high, low) since entry in the underlying's price space.

    An option position's ``highest_price`` / ``lowest_price`` track its
    PREMIUM; the underlying's own range is tracked separately by the
    position manager. Falls back to the entry when nothing has been seen.
    """
    entry = underlying_entry_price(position)
    meta = position.metadata if isinstance(position.metadata, dict) else {}
    if not is_option_asset(meta):
        return (safe_float(position.highest_price, entry), safe_float(position.lowest_price, entry))
    return (
        safe_float(meta.get("underlying_high_since_entry"), entry),
        safe_float(meta.get("underlying_low_since_entry"), entry),
    )


# The adaptive ladder's touch hold (shared_exit.adaptive_ladder_touch_hold):
# the position metadata key of a hold in progress, which keeps
# TradeManager.update_position off the target, and the codes of the exits
# the hold takes instead (see TradeManager.manage_adaptive_ladder).
LADDER_TOUCH_HOLD_KEY = "ladder_touch_hold"
TARGET_WEAK_CLOSE = "target_weak_close"
TARGET_HOLD_GUARD = "target_hold_guard"
TARGET_HOLD_TIMEOUT = "target_hold_timeout"


def exit_reason_details(decision: ExitDecision) -> dict[str, Any]:
    """The exit fields of the structured exit record (EXIT_CONTEXT) for
    ``decision``: ``exit_reason`` (the reason, stripped, or None),
    ``exit_reason_code`` (:func:`~intraday_tv_schwab_bot.reasons.exit_reason_code`),
    ``exit_reason_family`` (the decision's ``family``: who decided it, see
    :class:`~intraday_tv_schwab_bot.models.ExitDecision`) and
    ``exit_trigger_level``.

    Until 2026-09-27 the family was guessed from the reason string (risk,
    schedule, technical or strategy), so a peak-giveback exit read
    ``strategy`` and force flatten ``schedule`` while the decision said
    ``risk`` and ``force_flatten``."""
    raw = decision.reason.strip()
    code = exit_reason_code(raw)
    level_text = raw.partition(":")[2]
    return {
        "exit_reason": raw or None,
        "exit_reason_code": code,
        "exit_reason_family": decision.family,
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
