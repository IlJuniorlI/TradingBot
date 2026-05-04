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
from ..utils import (
    call_schwab_client,
    ensure_standard_indicator_frame,
    equity_session_state,
    now_et,
    parse_hhmm,
    resample_bars,
    talib_bbands,
    talib_obv,
)

# Pure helpers live in `helpers.py`. shared.py is the import boundary
# for the strategies package and re-exports them for convenience.
# Strategies can `from ..shared import _discrete_score_threshold` etc.
# without needing to know the helpers.py module exists.
from .helpers import (
    _ambiguous_regime_reason,
    _bar_close_position,
    _bar_wick_fractions,
    _bool_token,
    _clamp_long_premium_levels,
    _clamp_short_premium_levels,
    _dashboard_zone_width_from_policy,
    _detail_fields,
    _discrete_score_threshold,
    _fmt_metric,
    _gate_snapshot,
    insufficient_bars_reason,
    _is_scalar_missing,
    _no_style_trigger_reason,
    _normalize_symbol_list,
    _normalize_symbol_list_details,
    _optional_float,
    _optional_int,
    _position_strategy_matches,
    _positive_quote_value,
    _reason_prefix,
    _reason_with_values,
    _safe_float,
    _same_day_mask,
    _session_open_price,
    _side_prefixed_reason,
    _side_prefixed_reasons,
    _style_unavailable_reason,
    _time_gte_mask,
)


LOG = logging.getLogger(__name__)


__all__ = [
    '_ambiguous_regime_reason',
    '_bar_close_position',
    '_bar_wick_fractions',
    '_bool_token',
    '_clamp_long_premium_levels',
    '_clamp_short_premium_levels',
    '_dashboard_zone_width_from_policy',
    '_detail_fields',
    '_discrete_score_threshold',
    '_fmt_metric',
    '_gate_snapshot',
    'insufficient_bars_reason',
    '_is_scalar_missing',
    '_no_style_trigger_reason',
    '_normalize_symbol_list',
    '_normalize_symbol_list_details',
    '_optional_float',
    '_optional_int',
    '_position_strategy_matches',
    '_positive_quote_value',
    '_reason_prefix',
    '_reason_with_values',
    '_safe_float',
    '_same_day_mask',
    '_session_open_price',
    '_side_prefixed_reason',
    '_side_prefixed_reasons',
    '_style_unavailable_reason',
    '_time_gte_mask',
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
    'talib_bbands',
    'talib_obv',
    'time',
    'time_mod',
    'timedelta',
    'vertical_limit_price',
    'vertical_price_bounds',
    'summarize_pattern_matches',
    'yaml',
]
