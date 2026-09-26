# SPDX-License-Identifier: MIT
"""Numeric coercion: the one reading of a loosely typed number.

Broker payloads, position metadata, frame cells and quotes arrive as floats,
ints, numeric strings, None, NaN or pandas missing markers. ``float(nan)``
does not raise and ``nan >= x`` is False, so a NaN that slips through
silently skips whatever logic compares it. Every reader goes through these
three functions instead of a private copy.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def safe_float(value: Any, default: float | None = None, *, finite: bool = False) -> float | None:
    """``value`` as a float, or ``default`` when it is None, blank, NaN (the
    string ``'nan'`` included), a pandas missing marker (``pd.NA``, ``NaT``)
    or unparseable (an int too large for a float included). ±inf passes
    unless ``finite``."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if math.isnan(number) or (finite and math.isinf(number)):
        return default
    return number


def safe_int(value: Any, default: int | None = None) -> int | None:
    """``value`` truncated toward zero (``'12.0'`` and ``12.7`` read as 12),
    or ``default`` when :func:`safe_float` finds no finite number in it."""
    number = safe_float(value, finite=True)
    return default if number is None else int(number)


def first_float(mapping: Mapping[str, Any] | None, *keys: str, default: float | None = None,
                positive: bool = False, finite: bool = False) -> float | None:
    """The first of ``keys`` whose value in ``mapping`` reads as a number
    (per :func:`safe_float`), skipping values <= 0 when ``positive``; a key
    that is missing, NaN or unparseable falls through to the next.
    ``default`` when none reads or ``mapping`` is not a mapping."""
    if not isinstance(mapping, Mapping):
        return default
    for key in keys:
        number = safe_float(mapping.get(key), finite=finite)
        if number is not None and (not positive or number > 0):
            return number
    return default
