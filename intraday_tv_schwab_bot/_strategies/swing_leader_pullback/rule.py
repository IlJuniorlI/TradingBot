# SPDX-License-Identifier: MIT
"""The swing_leader_pullback rule on daily bars: pure functions, no engine.

The rule (the 2026-11-10 swing study's S2L with the 2 x ATR stop, README):
a momentum leader that closes above its trend SMA with a short RSI under the
bar is bought at the next session's open; it is sold at the first session
close above its exit SMA, or at the close of the ``max_hold_sessions``-th
session after the entry's, or at a stop ``stop_atr_mult`` daily ATRs under
the entry. Every function here reads COMPLETED sessions only; the caller
hands in today's price where a session's own close is asked for.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

OHLC = ("open", "high", "low", "close")


def wilder_rsi(closes: pd.Series, length: int) -> float | None:
    """Wilder's RSI of the last close. None while it cannot be read: fewer
    than ``length`` changes, a non-finite average, or no move at all in the
    window (no gain and no loss)."""
    delta = closes.astype(float).diff()
    alpha = 1.0 / float(length)
    gain = delta.clip(lower=0.0).ewm(alpha=alpha, adjust=False, min_periods=length).mean().iloc[-1]
    loss = (-delta.clip(upper=0.0)).ewm(alpha=alpha, adjust=False, min_periods=length).mean().iloc[-1]
    if not (math.isfinite(gain) and math.isfinite(loss)):
        return None
    if loss == 0.0:
        return None if gain == 0.0 else 100.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def simple_atr(daily: pd.DataFrame, length: int) -> float | None:
    """The mean true range of the last ``length`` sessions (a plain mean, as
    the study measured the stop). None with fewer than ``length`` + 1
    sessions or a value that is not finite and above 0."""
    if len(daily) < length + 1:
        return None
    high, low = daily["high"].astype(float), daily["low"].astype(float)
    prev = daily["close"].astype(float).shift(1)
    true_range = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    value = float(true_range.iloc[-length:].mean())
    return value if math.isfinite(value) and value > 0.0 else None


def setup_at_close(daily: pd.DataFrame, *, trend_sma: int, rsi_length: int, rsi_max: float,
                   atr_length: int) -> tuple[bool, str, dict[str, Any]]:
    """The pullback setup on the last session of ``daily`` (completed
    sessions, oldest first): the close above the SMA of the last
    ``trend_sma`` closes and the RSI under ``rsi_max``. Returns (passes,
    reason, the values read). A frame too short for the trend SMA, or an RSI
    or ATR that cannot be read, never passes."""
    closes = daily["close"].astype(float)
    if len(closes) < trend_sma:
        return False, f"daily_history_short({len(closes)}<{trend_sma})", {}
    close = float(closes.iloc[-1])
    sma = float(closes.iloc[-trend_sma:].mean())
    rsi = wilder_rsi(closes, rsi_length)
    atr = simple_atr(daily, atr_length)
    details: dict[str, Any] = {"close": close, "trend_sma": sma, "rsi": rsi, "atr": atr}
    if not (math.isfinite(close) and math.isfinite(sma)) or rsi is None or atr is None:
        return False, "daily_indicator_unreadable", details
    if not close > sma:
        return False, f"below_trend_sma({close:.4f}<={sma:.4f})", details
    if not rsi < rsi_max:
        return False, f"rsi_not_oversold({rsi:.2f}>={rsi_max:g})", details
    return True, "pullback_setup", details


def exit_reason(completed_closes: pd.Series, price: float, sessions_since_entry: int, *, exit_sma: int,
                max_hold_sessions: int) -> str | None:
    """Judged near a session's close, ``price`` standing for that session's
    close and ``completed_closes`` holding the sessions before it. The bounce
    exit: the price above the SMA of the last ``exit_sma`` closes, today's
    included. Else the time exit, once ``max_hold_sessions`` sessions have
    passed since the entry's own (0 on the entry session). None: hold. With
    fewer than ``exit_sma`` - 1 completed closes the bounce cannot be read
    and only the time exit can fire."""
    if len(completed_closes) >= exit_sma - 1:
        tail = completed_closes.astype(float).iloc[-(exit_sma - 1):] if exit_sma > 1 else completed_closes.iloc[0:0]
        sma = (float(tail.sum()) + float(price)) / float(exit_sma)
        if math.isfinite(sma) and price > sma:
            return f"bounce_above_sma{exit_sma}"
    if sessions_since_entry >= max_hold_sessions:
        return f"max_hold_{max_hold_sessions}_sessions"
    return None
