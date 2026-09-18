# SPDX-License-Identifier: MIT
"""Relative-volume profiles: how much volume a symbol must show to be taken
seriously, and how much its volume reading contributes to a focus score.

Three profiles, from most to least liquid:

  ``benchmark_etf``   index / sector ETFs — liquidity is never the question
  ``high_liquidity``  mega-cap equities — likewise, but slightly stricter
  ``standard``        everything else — the full gate applies

The profile drives two separate numbers, and keeping them separate matters:

  * :func:`relative_volume_gate_threshold` relaxes the PASS/FAIL gate. Callers
    must compare the symbol's RAW relative volume against it.
  * :func:`effective_relative_volume` floors and caps the value used for
    RANKING. The floor is why it must never be fed back into the gate — a
    benchmark ETF scores at least 0.90 on the volume term regardless of the
    day, which is right for scoring and wrong for gating.

Classification is by explicit symbol list first, then — for callers that can
supply it — by actual dollar volume. The dollar-volume path exists because a
hard-coded list of "liquid names" drifts out of date the moment the market
does: ORCL, PLTR, ARM, MU, ANET and friends were all absent while AAPL got an
80% gate relaxation, so two comparably liquid mega caps were gated 5x apart.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Iterable, cast

from ..position_metrics import safe_float

LOG = logging.getLogger(__name__)

# Misconfiguration warnings fire from a per-symbol hot path, so emit each
# distinct one once per process instead of once per symbol per cycle.
_WARNED: set[str] = set()


def _warn_once(key: str) -> bool:
    if key in _WARNED:
        return False
    _WARNED.add(key)
    return True


DEFAULT_BENCHMARK_RVOL_SYMBOLS: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "TLT",
    "GLD",
    "SLV",
    # Sector / industry ETFs used for index confirmation across the presets.
    "XLF",
    "XLK",
    "XLC",
    "XLY",
    "XLV",
    "XLP",
    "XLE",
    "XLI",
    "XLB",
    "XLRE",
    "XLU",
    "SMH",
    "SOXX",
    "IGV",
)

DEFAULT_HIGH_LIQUIDITY_RVOL_SYMBOLS: tuple[str, ...] = (
    # Mega-cap platforms
    "AAPL",
    "AMZN",
    "GOOG",
    "GOOGL",
    "META",
    "MSFT",
    "NFLX",
    "ORCL",
    "TSLA",
    # Semis / AI hardware
    "AMD",
    "ANET",
    "ARM",
    "AVGO",
    "DELL",
    "INTC",
    "MRVL",
    "MU",
    "NVDA",
    "QCOM",
    "SMCI",
    "TSM",
    "TXN",
    # Software
    "ADBE",
    "CRM",
    "CRWD",
    "NOW",
    "PANW",
    "PLTR",
    "SNOW",
    # Financials / consumer / other mega caps
    "BAC",
    "COST",
    "GS",
    "HD",
    "JPM",
    "LLY",
    "LOW",
    "MA",
    "MS",
    "RBLX",
    "TMUS",
    "UBER",
    "V",
    "WMT",
    "XOM",
)

# Dollar volume (price x shares) at or above which a symbol is treated as
# high-liquidity even when it is not on the list above. Deliberately generous:
# this is a "liquidity is not the question" test, not a screen.
DEFAULT_HIGH_LIQUIDITY_DOLLAR_VOLUME = 1_000_000_000.0


def _symbol_set(values: object) -> set[str]:
    """Flatten *values* into a set of upper-case symbol tokens.

    Accepts a sequence, a nested sequence, or a Mapping. For a Mapping the
    interpretation depends on the VALUE:

      ``{AAPL: true, MSFT: true}``   scalar values -> the KEYS are symbols
      ``{tech: [AAPL, MSFT]}``       container values -> the keys are group
                                     labels, only the values are symbols

    Both are natural YAML spellings and both were previously broken: reading
    ``.values()`` turned the first into the single token ``TRUE`` and silently
    discarded the symbols, and stringified the second into one bogus token.
    """
    out: set[str] = set()

    def _add(token: object) -> None:
        if isinstance(token, bytes):
            token = token.decode(errors="ignore")
        if not isinstance(token, str):
            return
        cleaned = token.upper().strip()
        # Booleans stringify to TRUE/FALSE and are never symbols.
        if cleaned and cleaned not in {"TRUE", "FALSE", "NONE"}:
            out.add(cleaned)

    def _walk(node: object, depth: int = 0) -> None:
        if node is None or depth > 4:
            return
        if isinstance(node, (str, bytes)):
            _add(node)
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                if isinstance(value, (Mapping, list, tuple, set, frozenset)):
                    _walk(value, depth + 1)   # key is a group label
                else:
                    _walk(key, depth + 1)     # key is the symbol
            return
        try:
            items = list(cast(Iterable[Any], node))
        except TypeError:
            return
        for item in items:
            _walk(item, depth + 1)

    _walk(values)
    return out


def _param(params: Mapping[str, Any] | None, key: str, default: float) -> float:
    """Read a float parameter with a default. ``params`` may be ``None``."""
    return safe_float((params or {}).get(key), default)


def rvol_profile_for_symbol(
    symbol: str,
    params: Mapping[str, Any] | None = None,
    *,
    dollar_volume: object = None,
) -> str:
    """Classify *symbol* as ``benchmark_etf`` / ``high_liquidity`` / ``standard``.

    ``dollar_volume`` (price x shares for the session) is optional. When
    supplied and at or above ``rvol_high_liquidity_dollar_volume``, a symbol
    that is not on either list is still treated as high-liquidity — so a
    screener-driven universe does not depend on a hand-maintained list of
    every liquid ticker in the market.
    """
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
    if dollar_volume is not None:
        threshold = _param(params, "rvol_high_liquidity_dollar_volume",
                           DEFAULT_HIGH_LIQUIDITY_DOLLAR_VOLUME)
        if 0 < threshold <= safe_float(dollar_volume, 0.0):
            return "high_liquidity"
    return "standard"


def _score_floor_for_profile(profile: str, params: Mapping[str, Any] | None,
                             standard_floor: float) -> float:
    if profile == "benchmark_etf":
        return _param(params, "rvol_score_floor_benchmark", 0.90)
    if profile == "high_liquidity":
        return _param(params, "rvol_score_floor_high_liquidity", 0.80)
    return _param(params, "rvol_score_floor_standard", standard_floor)


def effective_relative_volume(
    symbol: str,
    raw_relative_volume: object,
    params: Mapping[str, Any] | None = None,
    *,
    cap_default: float = 2.5,
    standard_floor: float = 0.5,
    dollar_volume: object = None,
) -> float:
    """Relative volume floored by profile and capped, for RANKING only.

    Never feed the result back into a gate: the floor means a benchmark ETF
    reports at least 0.90 no matter how dead the tape is. Gate on the raw
    value via :func:`relative_volume_gate_threshold`.
    """
    raw_rvol = max(0.0, safe_float(raw_relative_volume, 0.0))
    cap = max(0.5, _param(params, "rvol_score_cap", cap_default))
    profile = rvol_profile_for_symbol(symbol, params, dollar_volume=dollar_volume)
    floor = max(0.0, _score_floor_for_profile(profile, params, standard_floor))
    if floor > cap:
        # An inverted pair makes min(cap, max(floor, raw)) return `cap` for
        # EVERY input — the volume term stops discriminating between a dead
        # tape and a 5x surge, and every focus_score built on it degenerates
        # to a scaled day-change.
        #
        # DROP the floor rather than clamping it to the cap: clamping leaves
        # floor == cap, which is still a constant. With the floor at zero the
        # cap alone applies and the value discriminates again up to the
        # ceiling. The cap is the deliberate ceiling; the floor is a
        # don't-score-too-low nicety, so the floor is the one to yield.
        if _warn_once(f"floor_gt_cap:{profile}"):
            LOG.warning(
                "rvol_score_floor for profile '%s' (%.2f) exceeds rvol_score_cap "
                "(%.2f). Ignoring the floor — as configured the volume term would "
                "return %.2f for every symbol regardless of actual volume. "
                "Fix the config so the floor sits below the cap.",
                profile, floor, cap, cap,
            )
        floor = 0.0
    return min(cap, max(floor, raw_rvol))


def relative_volume_gate_threshold(
    symbol: str,
    base_threshold: object,
    params: Mapping[str, Any] | None = None,
    *,
    dollar_volume: object = None,
) -> float:
    """Relative-volume gate for *symbol*, relaxed by liquidity profile.

    Compare a symbol's RAW relative volume against this. Liquid names get the
    base threshold scaled down (they do not need elevated volume to be worth
    trading); ``standard`` names get it unchanged. Never returns more than
    ``base_threshold``.
    """
    base = max(0.0, safe_float(base_threshold, 0.0))
    if base <= 0:
        return 0.0
    profile = rvol_profile_for_symbol(symbol, params, dollar_volume=dollar_volume)
    if profile == "benchmark_etf":
        multiplier = _param(params, "rvol_gate_multiplier_benchmark", 0.20)
        floor = _param(params, "rvol_gate_floor_benchmark", 0.25)
    elif profile == "high_liquidity":
        multiplier = _param(params, "rvol_gate_multiplier_high_liquidity", 0.20)
        floor = _param(params, "rvol_gate_floor_high_liquidity", 0.28)
    else:
        return base
    return min(base, max(0.0, floor, base * max(0.0, multiplier)))
