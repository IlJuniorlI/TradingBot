# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Iterable, cast

from ..position_metrics import safe_float

DEFAULT_BENCHMARK_RVOL_SYMBOLS: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "TLT",
    "GLD",
    "SLV",
    "XLF",
    "XLK",
    "SMH",
)

DEFAULT_HIGH_LIQUIDITY_RVOL_SYMBOLS: tuple[str, ...] = (
    "AAPL",
    "AMD",
    "AMZN",
    "AVGO",
    "BAC",
    "COST",
    "CRM",
    "GOOG",
    "GOOGL",
    "GS",
    "HD",
    "INTC",
    "JPM",
    "LLY",
    "LOW",
    "MA",
    "META",
    "MS",
    "MSFT",
    "NFLX",
    "NVDA",
    "RBLX",
    "TMUS",
    "TSLA",
    "TSM",
    "UBER",
    "V",
)


def _symbol_set(values: object) -> set[str]:
    if isinstance(values, (str, bytes)) or values is None:
        raw_values: list[object] = []
    elif isinstance(values, Mapping):
        raw_values = list(values.values())
    else:
        try:
            raw_values = list(cast(Iterable[Any], values))
        except Exception:
            raw_values = []
    out: set[str] = set()
    for raw in raw_values:
        token = str(raw or "").upper().strip()
        if token:
            out.add(token)
    return out


def rvol_profile_for_symbol(symbol: str, params: Mapping[str, Any] | None = None) -> str:
    token = str(symbol or "").upper().strip()
    if not token:
        return "standard"
    benchmark_symbols = set(DEFAULT_BENCHMARK_RVOL_SYMBOLS)
    high_liquidity_symbols = set(DEFAULT_HIGH_LIQUIDITY_RVOL_SYMBOLS)
    if params is not None:
        benchmark_symbols.update(_symbol_set(params.get("rvol_benchmark_symbols")))
        high_liquidity_symbols.update(_symbol_set(params.get("rvol_high_liquidity_symbols")))
    if token in benchmark_symbols:
        return "benchmark_etf"
    if token in high_liquidity_symbols:
        return "high_liquidity"
    return "standard"


def effective_relative_volume(
    symbol: str,
    raw_relative_volume: object,
    params: Mapping[str, Any] | None = None,
    *,
    cap_default: float = 2.5,
    standard_floor: float = 0.5,
) -> float:
    raw_rvol = max(0.0, safe_float(raw_relative_volume, 0.0))
    cap = max(0.5, safe_float((params or {}).get("rvol_score_cap") if params is not None else None, cap_default))
    profile = rvol_profile_for_symbol(symbol, params)
    if profile == "benchmark_etf":
        floor = safe_float((params or {}).get("rvol_score_floor_benchmark") if params is not None else None, 0.90)
    elif profile == "high_liquidity":
        floor = safe_float((params or {}).get("rvol_score_floor_high_liquidity") if params is not None else None, 0.80)
    else:
        floor = safe_float((params or {}).get("rvol_score_floor_standard") if params is not None else None, standard_floor)
    return min(cap, max(0.0, floor, raw_rvol))


def relative_volume_gate_threshold(
    symbol: str,
    base_threshold: object,
    params: Mapping[str, Any] | None = None,
) -> float:
    base = max(0.0, safe_float(base_threshold, 0.0))
    if base <= 0:
        return 0.0
    profile = rvol_profile_for_symbol(symbol, params)
    if profile == "benchmark_etf":
        multiplier = safe_float((params or {}).get("rvol_gate_multiplier_benchmark") if params is not None else None, 0.20)
        floor = safe_float((params or {}).get("rvol_gate_floor_benchmark") if params is not None else None, 0.25)
        return min(base, max(floor, base * multiplier))
    if profile == "high_liquidity":
        multiplier = safe_float((params or {}).get("rvol_gate_multiplier_high_liquidity") if params is not None else None, 0.20)
        floor = safe_float((params or {}).get("rvol_gate_floor_high_liquidity") if params is not None else None, 0.28)
        return min(base, max(floor, base * multiplier))
    return base
