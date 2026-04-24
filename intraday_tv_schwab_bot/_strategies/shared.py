# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import asdict, replace
import logging
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
import time as time_mod

import pandas as pd
import yaml

from ..models import ASSET_TYPE_OPTION_SINGLE, ASSET_TYPE_OPTION_VERTICAL, Candidate, Position, Side, Signal
from ..options_mode import (
    OptionContract,
    build_position_label,
    build_single_option_order,
    build_single_option_position_label,
    build_vertical_order,
    choose_by_delta,
    choose_nearest_strike,
    contract_from_quote,
    filter_contracts,
    net_credit_dollars,
    net_debit_dollars,
    parse_option_chain,
    single_option_dollars,
    single_option_limit_price,
    single_option_price_bounds,
    vertical_limit_price,
    vertical_price_bounds,
)
from ..candles import (
    detect_bearish_patterns,
    detect_bullish_patterns,
    detect_candle_context,
    directional_candle_signal,
    summarize_pattern_matches,
)
from ..chart_patterns import analyze_chart_pattern_context
from ..support_resistance import analyze_market_structure, empty_market_structure_context, empty_support_resistance_context
from ..htf_levels import (
    HTFContext,
    FairValueGapContext,
    build_fair_value_gap_context,
    empty_fvg_context,
    empty_htf_context,
    summarize_htf_trend,
)
from ..technical_levels import TechnicalLevelsContext, build_technical_levels_context, empty_technical_levels_context
from ..utils import call_schwab_client, ensure_standard_indicator_frame, equity_session_state, now_et, parse_hhmm, resample_bars


LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 4 helper extractions — pure static functions moved out of
# BaseStrategy to reduce strategy_base.py and make them usable without
# inheriting. Each was originally `@staticmethod` on BaseStrategy.
# ---------------------------------------------------------------------------

def _discrete_score_threshold(
    value: Any,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    """Coerce ``value`` to an integer threshold, falling back to ``default``
    on parse failure and clamping to ``[minimum, maximum]`` (maximum optional).
    Originally BaseStrategy._discrete_score_threshold."""
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


def _side_prefixed_reason(side: Side, reason: str) -> str:
    """Ensure ``reason`` starts with ``{side.value.lower()}.`` prefix.
    Idempotent. Empty reason passes through unchanged.
    Originally BaseStrategy._side_prefixed_reason."""
    token = str(reason or "").strip()
    if not token:
        return token
    prefix = f"{side.value.lower()}."
    return token if token.startswith(prefix) else f"{prefix}{token}"


def _gate_snapshot(
    name: str,
    *,
    passed: bool,
    current: Any = None,
    required: Any = None,
    op: str = ">=",
    note: str | None = None,
) -> dict[str, Any]:
    """Build a gate-decision record for structured logging.
    Originally BaseStrategy._gate_snapshot."""
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


__all__ = [
    '_discrete_score_threshold',
    '_gate_snapshot',
    '_side_prefixed_reason',
    'ASSET_TYPE_OPTION_SINGLE',
    'ASSET_TYPE_OPTION_VERTICAL',
    'Any',
    'Candidate',
    'FairValueGapContext',
    'HTFContext',
    'LOG',
    'OptionContract',
    'Path',
    'Position',
    'Side',
    'Signal',
    'TechnicalLevelsContext',
    'analyze_chart_pattern_context',
    'analyze_market_structure',
    'asdict',
    'build_fair_value_gap_context',
    'build_position_label',
    'build_single_option_order',
    'build_single_option_position_label',
    'build_technical_levels_context',
    'build_vertical_order',
    'call_schwab_client',
    'ensure_standard_indicator_frame',
    'equity_session_state',
    'choose_by_delta',
    'choose_nearest_strike',
    'contract_from_quote',
    'date',
    'datetime',
    'detect_bearish_patterns',
    'detect_bullish_patterns',
    'detect_candle_context',
    'directional_candle_signal',
    'empty_fvg_context',
    'empty_htf_context',
    'empty_market_structure_context',
    'empty_support_resistance_context',
    'empty_technical_levels_context',
    'filter_contracts',
    'math',
    'net_credit_dollars',
    'net_debit_dollars',
    'now_et',
    'parse_hhmm',
    'parse_option_chain',
    'pd',
    'replace',
    'resample_bars',
    'single_option_dollars',
    'single_option_limit_price',
    'single_option_price_bounds',
    'summarize_htf_trend',
    'time',
    'time_mod',
    'timedelta',
    'vertical_limit_price',
    'vertical_price_bounds',
    'summarize_pattern_matches',
    'yaml',
]
