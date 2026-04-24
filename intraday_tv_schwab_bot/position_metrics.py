# SPDX-License-Identifier: MIT
"""Pure position-metric helpers extracted from ``IntradayBot``.

These are ``@staticmethod`` helpers with no engine-side dependencies — just
:class:`~intraday_tv_schwab_bot.models.Position` inputs and scalar math. Moved
into their own module so future callers (PositionManager, reports, tests)
can import them without dragging the whole engine surface along.
"""
from __future__ import annotations

from typing import Any

from .models import Position, Side


def safe_float(value: Any, default: float | None = None) -> float | None:
    """NaN-safe float coercion with a default fallback.

    Previously triplicated as ``@staticmethod _safe_float`` on
    ``IntradayBot`` / ``PositionManager`` / ``EntryGatekeeper`` /
    ``StartupReconciler``. Collapsed to this module-level function so all
    callers share a single implementation.

    IMPORTANT: ``float(float('nan'))`` does NOT raise, so a naive
    try/except does not catch NaN. Downstream comparisons like
    ``x >= threshold`` silently return False for NaN, which means a
    single NaN from an indicator warmup bar can silently skip logic
    (e.g. ``PositionManager._sr_flip_management_confirmed``'s
    ``_momentum_ok`` gate). Uses the ``x == x`` idiom (False for NaN) to
    coerce NaN to ``default``.
    """
    try:
        if value is None:
            return default
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


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


def exit_reason_details(reason: str) -> dict[str, Any]:
    """Classify an exit reason string into family + code + optional trigger
    level. Used for structured event logging at exit time.

    Input format: ``"<code>"`` or ``"<code>:<level>"`` (e.g. ``"stop:99.5"``).
    Output dict includes ``exit_reason`` (raw), ``exit_reason_code``,
    ``exit_reason_family`` (one of: risk, schedule, technical, strategy),
    and ``exit_trigger_level`` (float or None)."""
    raw = str(reason or "").strip()
    code, _, level_text = raw.partition(":")
    code = code or raw or None
    family = "strategy"
    if code in {"stop", "target"}:
        family = "risk"
    elif code in {"time_exit", "force_flatten", "session_exit"}:
        family = "schedule"
    elif isinstance(code, str) and any(token in code for token in ("trendline", "channel_", "bollinger_", "anchored_vwap")):
        family = "technical"
    trigger_level = None
    if level_text:
        try:
            trigger_level = float(level_text)
        except Exception:
            trigger_level = None
    return {
        "exit_reason": raw or None,
        "exit_reason_code": code,
        "exit_reason_family": family,
        "exit_trigger_level": trigger_level,
    }
