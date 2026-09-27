# SPDX-License-Identifier: MIT
"""Ticker symbols: the one normalized symbol list, and what the feed may do
with a symbol (stream it, quote it under an alias, build S/R on it).

``normalize_symbol_list`` is the one reading of a configured or collected
symbol list: config's ``options.underlyings``, the strategies' watchlist and
dashboard sources, the dashboard's tradable / index lists and the peer
family's tradable / peer params all go through it. ``ticker_quote_hint`` is
the hint of every load check that refuses a ticker YAML read as a boolean or
null."""
from __future__ import annotations

import re
from collections.abc import Iterable

STREAMABLE_EQUITY_RE = re.compile(r"^[A-Z]{1,6}$")
NON_STREAMABLE = {"VIX", "$VIX", "$VIX.X", "DXY", "$DXY", "$DXY.X", "NYICDX", "$NYICDX", "$NYICDX.X", "SPX", "$SPX", "$SPX.X", "$COMPX", "COMPX", "NDX", "$NDX", "RUT", "$RUT", "$DJI", "DJI"}
SR_SYMBOL_ALIASES = {
    "VIX": "VIX",
    "$VIX": "VIX",
    "$VIX.X": "VIX",
    "DXY": "NYICDX",
    "$DXY": "NYICDX",
    "$DXY.X": "NYICDX",
    "NYICDX": "NYICDX",
    "$NYICDX": "NYICDX",
    "$NYICDX.X": "NYICDX",
}
MARKET_INTERNAL_SYMBOLS = {
    "TICK", "$TICK", "$TICK.X", "TICKQ", "$TICKQ", "$TICKQ.X",
    "ADD", "$ADD", "$ADD.X", "ADDQ", "$ADDQ", "$ADDQ.X",
    "VOLD", "$VOLD", "$VOLD.X", "VOLDQ", "$VOLDQ", "$VOLDQ.X",
    "TRIN", "$TRIN", "$TRIN.X", "TRINQ", "$TRINQ", "$TRINQ.X",
}
QUOTE_SYMBOL_ALIASES = {
    "VIX": ["$VIX", "$VIX.X", "VIX"],
    "$VIX": ["$VIX", "$VIX.X", "VIX"],
    "$VIX.X": ["$VIX.X", "$VIX", "VIX"],
    "DXY": ["$NYICDX", "NYICDX", "$DXY", "$DXY.X", "DXY"],
    "$DXY": ["$NYICDX", "NYICDX", "$DXY", "$DXY.X", "DXY"],
    "$DXY.X": ["$NYICDX", "NYICDX", "$DXY.X", "$DXY", "DXY"],
    "NYICDX": ["$NYICDX", "NYICDX", "$DXY", "$DXY.X", "DXY"],
    "$NYICDX": ["$NYICDX", "NYICDX", "$DXY", "$DXY.X", "DXY"],
    "$NYICDX.X": ["$NYICDX.X", "$NYICDX", "NYICDX", "$DXY", "$DXY.X", "DXY"],
}


def is_streamable_equity(symbol: str) -> bool:
    sym = str(symbol).upper().strip()
    if sym in NON_STREAMABLE:
        return False
    if sym.startswith("$") or " " in sym or "/" in sym:
        return False
    return bool(STREAMABLE_EQUITY_RE.match(sym))


def normalize_context_symbol(symbol: str) -> str:
    sym = str(symbol).upper().strip()
    if not sym:
        return ""
    return SR_SYMBOL_ALIASES.get(sym, sym)


def is_market_internal_symbol(symbol: str) -> bool:
    sym = normalize_context_symbol(symbol)
    raw = str(symbol).upper().strip()
    return sym in MARKET_INTERNAL_SYMBOLS or raw in MARKET_INTERNAL_SYMBOLS


def is_support_resistance_symbol(symbol: str) -> bool:
    raw = str(symbol).upper().strip()
    if not raw:
        return False
    if " " in raw or "/" in raw:
        return False
    if is_market_internal_symbol(raw):
        return False
    normalized = normalize_context_symbol(raw)
    if raw.startswith("$") and normalized == raw:
        return False
    return True


def normalize_symbol_list_details(values: object) -> tuple[list[str], list[str]]:
    """Normalize an iterable-of-symbols input into (kept, skipped).

    Accepts list/tuple/set or any iterable. Strings/bytes/dict/None, and a
    value that is not iterable (a YAML scalar such as ``5``), are treated as
    'no input'. Tokens are uppercased + stripped + dedup'd.
    Empty/None/NULL/NAN tokens are routed to the skipped list with
    placeholder labels (``<NONE>``, ``<EMPTY>``, etc.) for log clarity.

    An iterable that raises while it is read raises here. Until 2026-09-26
    a broad except read any error from ``list(values)`` as no input."""
    if isinstance(values, (str, bytes, dict)) or not isinstance(values, Iterable):
        raw_values: list[object] = []
    else:
        raw_values = list(values)
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


def normalize_symbol_list(values: object) -> list[str]:
    """The kept half of ``normalize_symbol_list_details`` (the skipped
    tokens are dropped silently)."""
    kept, _ = normalize_symbol_list_details(values)
    return kept


def ticker_quote_hint(value: object) -> str:
    """The hint for a ticker YAML read as a boolean or null (an unquoted
    ``ON``, ON Semiconductor, is ``true``), for the messages that refuse a
    ticker that is not a string: an event row's ``symbols``, an earnings
    key and ``runtime.startup_reconcile_ignore_symbols``. Empty for any
    other value."""
    if value is None or isinstance(value, bool):
        return " (YAML reads an unquoted ON, OFF, YES, NO, TRUE, FALSE or ~ as a boolean or null: quote the ticker)"
    return ""
