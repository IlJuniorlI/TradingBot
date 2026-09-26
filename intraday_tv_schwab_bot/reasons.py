# SPDX-License-Identifier: MIT
"""Reason strings: the one format and the one reading of a skip or exit reason.

A skip reason is a name with optional detail in parentheses,
``name(required>=X,current=Y,...)``; an exit reason is a code with optional
detail after a colon, ``code:detail``. The strategies, the entry stage and the
warm-up tracker format them here, and the report and the entry stage read
them back through :func:`reason_head` and :func:`exit_reason_code`.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from .models import Side


def _is_scalar_missing(value: Any) -> bool:
    """True if ``value`` should be treated as missing for downstream math.

    Handles None, blank strings, and pd.isna-style NaN. DataFrames /
    Series / Index inputs are NOT considered missing (they're container
    shapes — caller decides what to do with them)."""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (pd.DataFrame, pd.Series, pd.Index)):
        return False
    try:
        missing = pd.isna(value)
    except Exception:
        return False
    return type(missing).__name__ in {"bool", "bool_"} and bool(missing)


def fmt_metric(value: Any, digits: int = 4) -> str:
    """Render ``value`` for embedding in a skip-reason string. NaN/None
    becomes ``'na'``; ints stay ints; floats get fixed-precision."""
    try:
        if _is_scalar_missing(value):
            return "na"
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return f"{float(value):.{digits}f}"
    except Exception:
        return "na"


def bool_token(value: Any) -> str:
    """Render any truthy/falsy value as the string ``'true'`` or
    ``'false'`` for embedding in skip-reason details."""
    return "true" if bool(value) else "false"


def side_prefixed_reason(side: Side, reason: str) -> str:
    """Ensure ``reason`` starts with ``{side.value.lower()}.`` prefix.
    Idempotent. Empty reason passes through unchanged."""
    token = str(reason or "").strip()
    if not token:
        return token
    prefix = f"{side.value.lower()}."
    return token if token.startswith(prefix) else f"{prefix}{token}"


def side_prefixed_reasons(side: Side, reasons: list[str] | tuple[str, ...] | None) -> list[str]:
    """Apply ``side_prefixed_reason`` across a sequence, dedup-preserving order.
    Empty / blank tokens are skipped."""
    out: list[str] = []
    for item in reasons or []:
        token = side_prefixed_reason(side, str(item or "").strip())
        if token and token not in out:
            out.append(token)
    return out


def reason_with_values(
    name: str,
    *,
    current: Any = None,
    required: Any = None,
    op: str = ">=",
    digits: int = 4,
    extras: dict[str, tuple[Any, str, Any]] | None = None,
) -> str:
    """Build a structured skip-reason string of the form
    ``name(required>=X,current=Y,...)`` for embedding in entry-decision
    records. ``extras`` is a mapping of label → (current, op, required)
    triples for additional comparison facets."""
    parts = [name]
    if required is not None or current is not None:
        parts.append(f"required{op}{fmt_metric(required, digits)}")
        parts.append(f"current={fmt_metric(current, digits)}")
    for label, payload in (extras or {}).items():
        extra_current, extra_op, extra_required = payload
        parts.append(f"{label}_required{extra_op}{fmt_metric(extra_required, digits)}")
        parts.append(f"{label}_current={fmt_metric(extra_current, digits)}")
    return f"{name}({','.join(parts[1:])})" if len(parts) > 1 else name


def detail_fields(**fields: Any) -> str:
    """Render ``key=value`` pairs for embedding inside a reason string.
    Bools become ``true``/``false``; ints stay ints; floats get
    fixed-precision; strings pass through."""
    parts: list[str] = []
    for key, value in fields.items():
        if isinstance(value, bool):
            rendered = bool_token(value)
        elif isinstance(value, int) and not isinstance(value, bool):
            rendered = str(value)
        elif isinstance(value, str):
            rendered = value
        else:
            rendered = fmt_metric(value, 4)
        parts.append(f"{key}={rendered}")
    return ",".join(parts)


def insufficient_bars_reason(name: str, current: Any, required: Any) -> str:
    """Standard 'not enough bars yet' skip reason."""
    return reason_with_values(name, current=current, required=required, op=">=", digits=0)


def reason_head(reason: Any) -> str:
    """A skip reason's name: the text before its first ``(``, stripped
    (``long_no_fresh_breakout(close=248.7250<=recent_high=248.8099)`` ->
    ``long_no_fresh_breakout``), so the numeric detail does not split one
    gate into hundreds of buckets. A reason with nothing before its ``(``
    (or a blank one) is returned whole."""
    text = str(reason or "")
    return text.split("(", 1)[0].strip() or text


def exit_reason_code(reason: Any) -> str | None:
    """An exit reason's code: the text before its first ``:``, stripped
    (``resistance_break_exit:311.5900`` -> ``resistance_break_exit``); None
    when there is none."""
    return str(reason or "").split(":", 1)[0].strip() or None
