# SPDX-License-Identifier: MIT
"""Pure helpers extracted from `BaseStrategy` and `shared.py`.

Single home for stateless utility functions used across strategies. None
of these depend on strategy state (no `self.params`, no `self.config`,
no caches) — they are pure data transforms and reason-string formatters.

Why a separate module: `strategy_base.py` had grown past 3400 lines with
~30 pure helpers mixed into the strategy framework. Extracting them
keeps:
  - strategy_base.py focused on strategy-state-coupled decision logic
  - shared.py focused on its re-export-hub role (no helper definitions)
  - tests easier — each helper exercisable as a free function

Naming convention: leading-underscore names are preserved on free
functions to match the prior `BaseStrategy._method` naming. The
`BaseStrategy` methods themselves remain as thin sugar wrappers so
existing `self._method(...)` callsites keep working without churn.

shared.py re-exports everything here in its `__all__`, so existing
`from ..shared import _discrete_score_threshold` style imports continue
to work unchanged.
"""
from __future__ import annotations

import math
from datetime import date, time
from typing import Any, Iterable

import pandas as pd

from ..models import Position, Side
from ..utils import now_et


# ---------------------------------------------------------------------------
# Numeric / NaN-safe coercion
# ---------------------------------------------------------------------------

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


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to float, returning ``default`` on failure or NaN.

    Always returns ``float`` (not ``Optional[float]``). For an
    optional-float helper use ``_optional_float``. The canonical
    NaN-safe helper for the wider bot is ``position_metrics.safe_float``;
    this implementation mirrors it for use inside strategy code that
    already has a 0.0 default contract.
    """
    try:
        if _is_scalar_missing(value):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _optional_float(value: Any, default: float | None = None) -> float | None:
    """Coerce ``value`` to ``float | None``. Returns ``default`` on
    failure or NaN. Use when downstream code distinguishes 'unset' from
    a real numeric zero."""
    try:
        if _is_scalar_missing(value):
            return default
        return float(value)
    except Exception:
        return default


def _optional_int(value: Any, default: int | None = None) -> int | None:
    """Coerce ``value`` to ``int | None``. Returns ``default`` on
    failure or NaN."""
    try:
        if _is_scalar_missing(value):
            return default
        return int(value)
    except Exception:
        return default


def _fmt_metric(value: Any, digits: int = 4) -> str:
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


def _bool_token(value: Any) -> str:
    """Render any truthy/falsy value as the string ``'true'`` or
    ``'false'`` for embedding in skip-reason details."""
    return "true" if bool(value) else "false"


def _discrete_score_threshold(
    value: Any,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    """Coerce ``value`` to an integer threshold, falling back to
    ``default`` on parse failure and clamping to ``[minimum, maximum]``
    (maximum optional)."""
    try:
        raw = float(value)
    except Exception:
        raw = float(default)
    if math.isnan(raw):
        raw = float(default)
    threshold = int(math.ceil(raw))
    threshold = max(int(minimum), threshold)
    if maximum is not None:
        threshold = min(int(maximum), threshold)
    return threshold


# ---------------------------------------------------------------------------
# DataFrame / bar shape helpers
# ---------------------------------------------------------------------------

def _bar_close_position(frame: pd.DataFrame) -> float:
    """Close position within the last bar's range, in [0, 1].

    1.0 = close at high, 0.0 = close at low. Returns 0.5 on degenerate
    or empty frames so caller logic doesn't have to special-case."""
    if frame is None or frame.empty:
        return 0.5
    last = frame.iloc[-1]
    low = _safe_float(last["low"])
    high = _safe_float(last["high"])
    if high <= low:
        return 0.5
    return (_safe_float(last["close"]) - low) / (high - low)


def _bar_wick_fractions(frame: pd.DataFrame) -> tuple[float, float, float, float]:
    """Decompose the latest bar into (upper_wick_frac, lower_wick_frac,
    body_frac, bar_range). The first three are fractions of bar range
    in [0, 1]; the fourth is the absolute bar range (high-low)."""
    if frame is None or frame.empty:
        return 0.0, 0.0, 0.0, 0.0
    last = frame.iloc[-1]
    high = _safe_float(last.get("high"), 0.0)
    low = _safe_float(last.get("low"), 0.0)
    open_ = _safe_float(last.get("open"), low)
    close = _safe_float(last.get("close"), open_)
    bar_range = max(0.0, high - low)
    if bar_range <= 0.0:
        return 0.0, 0.0, 0.0, 0.0
    upper_wick = max(0.0, high - max(open_, close))
    lower_wick = max(0.0, min(open_, close) - low)
    body = abs(close - open_)
    return upper_wick / bar_range, lower_wick / bar_range, body / bar_range, bar_range


def _same_day_mask(frame: pd.DataFrame, day: date) -> pd.Series:
    """Boolean mask selecting bars whose timestamp falls on ``day`` (ET)."""
    return frame.index.to_series().map(lambda ts: ts.date() == day)


def _time_gte_mask(frame: pd.DataFrame, t: time) -> pd.Series:
    """Boolean mask selecting bars at or after time-of-day ``t``."""
    return frame.index.to_series().map(lambda ts: ts.time() >= t)


def _session_open_price(
    frame: pd.DataFrame | None,
    day: date | None = None,
    *,
    regular_session_only: bool = True,
) -> float | None:
    """First open price of the trading day. Returns None if frame has
    no bars for the requested day. With ``regular_session_only=True``
    (default), prefers the first 09:30+ ET bar; falls back to extended
    hours if the regular session hasn't started yet."""
    if frame is None or frame.empty:
        return None
    target_day = day or now_et().date()
    same_day = frame[_same_day_mask(frame, target_day)]
    if same_day.empty:
        return None
    if regular_session_only:
        regular = same_day[same_day.index.to_series().map(lambda ts: ts.time() >= time(9, 30))]
        if not regular.empty:
            same_day = regular
    try:
        open_value = same_day.iloc[0]["open"]
    except Exception:
        return None
    if _is_scalar_missing(open_value):
        return None
    try:
        return float(open_value)
    except Exception:
        return None


def _positive_quote_value(quote: dict[str, Any] | None, *keys: str) -> float | None:
    """Return the first positive numeric value found at any of ``keys``
    in ``quote``. Returns None if quote is None or no key resolves to
    a positive float."""
    if not isinstance(quote, dict):
        return None
    for key in keys:
        try:
            value = quote.get(key)
            if value is None:
                continue
            number = float(value)
        except Exception:
            continue
        if number > 0:
            return number
    return None


# ---------------------------------------------------------------------------
# Long / short premium-level clamping (used by option strategies)
# ---------------------------------------------------------------------------

def _clamp_long_premium_levels(
    entry_value: float, stop_value: float, target_value: float | None,
) -> tuple[float, float | None]:
    """Ensure stop < entry and (if set) target > entry on a LONG. Floors
    everything at 0.01 (penny) and enforces a 0.01 minimum gap between
    stop/target and entry."""
    entry = max(0.01, float(entry_value))
    stop = max(0.01, min(float(stop_value), entry - 0.01))
    if target_value is None:
        return stop, None
    target = max(entry + 0.01, float(target_value))
    return stop, target


def _clamp_short_premium_levels(
    entry_value: float, stop_value: float, target_value: float | None,
) -> tuple[float, float | None]:
    """Ensure stop > entry and (if set) target < entry on a SHORT. Floors
    everything at 0.01 and enforces 0.01 minimum gap between
    stop/target and entry."""
    entry = max(0.01, float(entry_value))
    stop = max(entry + 0.01, float(stop_value))
    if target_value is None:
        return stop, None
    target = max(0.01, min(float(target_value), entry - 0.01))
    return stop, target


# ---------------------------------------------------------------------------
# Symbol list / strategy matching
# ---------------------------------------------------------------------------

def _normalize_symbol_list_details(values: object) -> tuple[list[str], list[str]]:
    """Normalize an iterable-of-symbols input into (kept, skipped).

    Accepts list/tuple/set or any iterable. Strings/bytes/dict/None are
    treated as 'no input'. Tokens are uppercased + stripped + dedup'd.
    Empty/None/NULL/NAN tokens are routed to the skipped list with
    placeholder labels (``<NONE>``, ``<EMPTY>``, etc.) for log clarity."""
    if isinstance(values, (str, bytes, dict)) or values is None:
        raw_values: list[object] = []
    elif isinstance(values, (list, tuple, set, frozenset)):
        raw_values = list(values)
    else:
        try:
            iterable_values: Iterable[object] = values  # type: ignore[assignment]
            raw_values = list(iterable_values)
        except Exception:
            raw_values = []
    out: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    invalid_tokens = {"NONE", "NULL", "NAN"}
    for raw in raw_values:
        if raw is None:
            skipped.append("<NONE>")
            continue
        token = str(raw).upper().strip()
        if not token:
            skipped.append("<EMPTY>")
            continue
        if token in invalid_tokens:
            skipped.append(token)
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out, skipped


def _normalize_symbol_list(values: object) -> list[str]:
    """Convenience wrapper around ``_normalize_symbol_list_details`` that
    returns just the kept-symbols list (drops skipped tokens silently)."""
    kept, _ = _normalize_symbol_list_details(values)
    return kept


def _position_strategy_matches(position: Position, strategy_names: list[str] | None) -> bool:
    """True if ``position.strategy`` matches any of the configured
    strategy names. None or empty strategy_names means 'match anything'."""
    if not strategy_names:
        return True
    current = str(getattr(position, "strategy", "") or "").strip().lower()
    return current in {str(name).strip().lower() for name in strategy_names if str(name).strip()}


def _side_prefixed_reason(side: Side, reason: str) -> str:
    """Ensure ``reason`` starts with ``{side.value.lower()}.`` prefix.
    Idempotent. Empty reason passes through unchanged."""
    token = str(reason or "").strip()
    if not token:
        return token
    prefix = f"{side.value.lower()}."
    return token if token.startswith(prefix) else f"{prefix}{token}"


def _side_prefixed_reasons(side: Side, reasons: list[str] | tuple[str, ...] | None) -> list[str]:
    """Apply ``_side_prefixed_reason`` across a sequence, dedup-preserving order.
    Empty / blank tokens are skipped."""
    out: list[str] = []
    for item in reasons or []:
        token = _side_prefixed_reason(side, str(item or "").strip())
        if token and token not in out:
            out.append(token)
    return out


# ---------------------------------------------------------------------------
# Skip-reason / decision-string formatters
# ---------------------------------------------------------------------------

def _reason_with_values(
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
        parts.append(f"required{op}{_fmt_metric(required, digits)}")
        parts.append(f"current={_fmt_metric(current, digits)}")
    for label, payload in (extras or {}).items():
        extra_current, extra_op, extra_required = payload
        parts.append(f"{label}_required{extra_op}{_fmt_metric(extra_required, digits)}")
        parts.append(f"{label}_current={_fmt_metric(extra_current, digits)}")
    return f"{name}({','.join(parts[1:])})" if len(parts) > 1 else name


def _detail_fields(**fields: Any) -> str:
    """Render ``key=value`` pairs for embedding inside a reason string.
    Bools become ``true``/``false``; ints stay ints; floats get
    fixed-precision; strings pass through."""
    parts: list[str] = []
    for key, value in fields.items():
        if isinstance(value, bool):
            rendered = _bool_token(value)
        elif isinstance(value, int) and not isinstance(value, bool):
            rendered = str(value)
        elif isinstance(value, str):
            rendered = value
        else:
            rendered = _fmt_metric(value, 4)
        parts.append(f"{key}={rendered}")
    return ",".join(parts)


def _style_unavailable_reason(style: str, detail: str, **fields: Any) -> str:
    """Standard format for a per-style 'unavailable' skip reason:
    ``{style}_unavailable({detail},k=v,...)``."""
    detail = str(detail or "").strip() or "unknown"
    extra = _detail_fields(**fields)
    inner = f"{detail},{extra}" if extra else detail
    return f"{style}_unavailable({inner})"


def insufficient_bars_reason(name: str, current: Any, required: Any) -> str:
    """Standard 'not enough bars yet' skip reason."""
    return _reason_with_values(name, current=current, required=required, op=">=", digits=0)


def _ambiguous_regime_reason(
    *,
    top_name: str,
    top_score: Any,
    second_name: str,
    second_score: Any,
    min_top_score: Any,
    min_score_gap: Any,
) -> str:
    """Standard 'top regime score too close to second' skip reason."""
    gap = None
    try:
        if not _is_scalar_missing(top_score) and not _is_scalar_missing(second_score):
            gap = float(top_score) - float(second_score)
    except Exception:
        gap = None
    return (
        "ambiguous_regime("
        f"top={top_name},"
        f"top_score={_fmt_metric(top_score, 2)},"
        f"second={second_name},"
        f"second_score={_fmt_metric(second_score, 2)},"
        f"required_top_score>={_fmt_metric(min_top_score, 2)},"
        f"required_score_gap>={_fmt_metric(min_score_gap, 2)},"
        f"current_score_gap={_fmt_metric(gap, 2)}"
        ")"
    )


def _no_style_trigger_reason(
    *,
    regime_name: str,
    bullish: bool,
    bearish: bool,
    rangeish: bool,
    orb_enabled: bool,
    orb_window: bool,
    trend_enabled: bool,
    trend_window: bool,
    credit_enabled: bool,
    credit_window: bool,
    last_close: Any,
    last_vwap: Any,
    last_ret5: Any,
    trend_min_ret5: Any,
    or_high: Any,
    or_low: Any,
    orb_buffer_pct: Any,
) -> str:
    """Standard 'no style trigger fired' skip reason for the
    multi-style regime pipeline. Renders the salient context fields
    so post-hoc analysis can reconstruct why none of the styles fired.

    Field names match the legacy BaseStrategy._no_style_trigger_reason
    output exactly so log-parsing tools and existing dashboard chips
    keep working."""
    bull_trigger = None
    bear_trigger = None
    try:
        if not _is_scalar_missing(or_high) and not _is_scalar_missing(orb_buffer_pct):
            bull_trigger = float(or_high) * (1.0 + float(orb_buffer_pct))
    except Exception:
        bull_trigger = None
    try:
        if not _is_scalar_missing(or_low) and not _is_scalar_missing(orb_buffer_pct):
            bear_trigger = float(or_low) * (1.0 - float(orb_buffer_pct))
    except Exception:
        bear_trigger = None
    return (
        "no_style_trigger("
        f"regime={regime_name},"
        f"bullish={_bool_token(bullish)},"
        f"bearish={_bool_token(bearish)},"
        f"rangeish={_bool_token(rangeish)},"
        f"orb_enabled={_bool_token(orb_enabled)},"
        f"orb_window={_bool_token(orb_window)},"
        f"trend_enabled={_bool_token(trend_enabled)},"
        f"trend_window={_bool_token(trend_window)},"
        f"credit_enabled={_bool_token(credit_enabled)},"
        f"credit_window={_bool_token(credit_window)},"
        f"close={_fmt_metric(last_close, 4)},"
        f"vwap={_fmt_metric(last_vwap, 4)},"
        f"ret5={_fmt_metric(last_ret5, 4)},"
        f"required_ret5>={_fmt_metric(trend_min_ret5, 4)},"
        f"required_bear_ret5<={_fmt_metric(-_safe_float(trend_min_ret5, 0.0), 4)},"
        f"or_high={_fmt_metric(or_high, 4)},"
        f"or_low={_fmt_metric(or_low, 4)},"
        f"orb_bull_trigger>{_fmt_metric(bull_trigger, 4)},"
        f"orb_bear_trigger<{_fmt_metric(bear_trigger, 4)},"
        f"orb_buffer_pct={_fmt_metric(orb_buffer_pct, 4)}"
        ")"
    )


def _reason_prefix(reason: str) -> str:
    """Extract the leading head from a skip-reason string (the part
    before the first ``(``). Used to test reason-prefix membership in
    deferrable-reason sets."""
    return str(reason or "").split("(", 1)[0].strip()


# ---------------------------------------------------------------------------
# Structured-logging payload builder
# ---------------------------------------------------------------------------

def _gate_snapshot(
    name: str,
    *,
    passed: bool,
    current: Any = None,
    required: Any = None,
    op: str = ">=",
    note: str | None = None,
) -> dict[str, Any]:
    """Build a gate-decision record for structured logging."""
    payload: dict[str, Any] = {
        "name": str(name),
        "pass": bool(passed),
        "op": str(op),
    }
    if current is not None:
        payload["current"] = current
    if required is not None:
        payload["required"] = required
    if note:
        payload["note"] = str(note)
    return payload


# ---------------------------------------------------------------------------
# Dashboard zone-width policy
# ---------------------------------------------------------------------------

def _dashboard_zone_width_from_policy(policy: dict[str, Any], close: float, atr: float) -> float | None:
    """Resolve a numeric zone-width given a dashboard zone-width policy
    dict and the current price + ATR. Supports four ``mode`` values:
    ``fixed`` (use ``value``/``fixed_width`` directly), ``atr_mult``
    (multiply ATR by ``value``/``atr_mult``), ``pct_of_price`` /
    ``price_pct`` (multiply close by the configured percentage), and
    ``max_of`` (take max of any of the above components present).
    Returns None for unrecognized modes or invalid values; otherwise
    floors at ``min_width`` (default 0.01)."""
    mode = str(policy.get("mode") or "").strip().lower()
    min_width = float(policy.get("min_width", 0.01) or 0.01)
    computed_width: float | None
    if mode == "fixed":
        value = policy.get("value", policy.get("fixed_width"))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        computed_width = float(value)
    elif mode == "atr_mult":
        value = policy.get("value", policy.get("atr_mult"))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        computed_width = float(atr) * float(value)
    elif mode in {"pct_of_price", "price_pct"}:
        value = policy.get("value", policy.get("pct_of_price", policy.get("price_pct")))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        computed_width = float(close) * float(value)
    elif mode == "max_of":
        parts: list[float] = []
        fixed_width = policy.get("fixed_width")
        atr_mult = policy.get("atr_mult")
        pct_of_price = policy.get("pct_of_price", policy.get("price_pct"))
        if isinstance(fixed_width, (int, float)) and not isinstance(fixed_width, bool):
            parts.append(float(fixed_width))
        if isinstance(atr_mult, (int, float)) and not isinstance(atr_mult, bool):
            parts.append(float(atr) * float(atr_mult))
        if isinstance(pct_of_price, (int, float)) and not isinstance(pct_of_price, bool):
            parts.append(float(close) * float(pct_of_price))
        if not parts:
            return None
        computed_width = max(parts)
    else:
        return None
    return max(float(computed_width), float(min_width), 0.01)


__all__ = [
    "_ambiguous_regime_reason",
    "_bar_close_position",
    "_bar_wick_fractions",
    "_bool_token",
    "_clamp_long_premium_levels",
    "_clamp_short_premium_levels",
    "_dashboard_zone_width_from_policy",
    "_detail_fields",
    "_discrete_score_threshold",
    "_fmt_metric",
    "_gate_snapshot",
    "insufficient_bars_reason",
    "_is_scalar_missing",
    "_no_style_trigger_reason",
    "_normalize_symbol_list",
    "_normalize_symbol_list_details",
    "_optional_float",
    "_optional_int",
    "_position_strategy_matches",
    "_positive_quote_value",
    "_reason_prefix",
    "_reason_with_values",
    "_safe_float",
    "_same_day_mask",
    "_session_open_price",
    "_side_prefixed_reason",
    "_side_prefixed_reasons",
    "_style_unavailable_reason",
    "_time_gte_mask",
]
