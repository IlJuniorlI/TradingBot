# SPDX-License-Identifier: MIT
"""The plugin API's vocabulary: the manifest a plugin ships and the closed
sets its declarations name."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The shared entry stage's P3 vetoes (shared_entry.SharedEntryPolicy), in
# evaluation (and reporting) order. A manifest exempts a style from any of
# them: capabilities.shared_entry.exemptions {style: [gate]}, which the
# catalogue validates against this closed set.
VETO_GATES: tuple[str, ...] = ("structure", "sr", "broken_level", "chart", "dual_divergence", "candle")

# The params the shared framework reads from any strategy's block, each with
# the reader's own default when the key is absent (the whole-bot review's
# R-106, 2026-11-08). The load accepts a ``strategies.<name>.params`` key the
# strategy's manifest declares or this set names, and refuses any other
# (``config._reject_unknown_params``): a manifest declares the knobs the
# strategy's own code reads, this set the knobs the base and the shared
# policies read for every strategy, whose defaults depend on the trade
# (``trend_like``) or on another config section and so cannot be written
# into a manifest. tests/guards/test_plugin_contract.py holds the set to the
# shared modules' reads.
FRAMEWORK_PARAMS: frozenset[str] = frozenset({
    # BaseStrategy.dashboard_tradable_symbols / dashboard_index_symbols and
    # config._validate_sector_index_map
    "tradable", "symbols", "index_symbols", "sector_index_map", "sector_groups",
    # BaseStrategy.dashboard_level_context_spec and indicators.htf_ema_spans
    "htf_pivot_span", "htf_max_levels_per_side", "htf_atr_tolerance_mult", "htf_pct_tolerance",
    "htf_stop_buffer_atr_mult", "min_level_score", "level_round_number_tolerance_pct",
    "zone_atr_mult", "pivot_zone_atr_mult", "zone_pct", "pivot_zone_pct",
    "htf_ema_fast_span", "htf_ema_slow_span",
    # the frames: StrategyContextMixin.htf_minutes / htf_lookback_days /
    # ltf_minutes and indicators.ltf_ema_spans
    "htf_minutes", "htf_lookback_days", "ltf_minutes",
    "ltf_ema_fast_span", "ltf_ema_slow_span", "ltf_indicator_span_scale",
    # BaseStrategy.required_history_bars
    "min_bars",
    # BaseStrategy._force_flatten_settings / _force_flatten_cutoff
    "force_flatten", "force_flatten_buffer_minutes",
    # BaseStrategy._entry_exhaustion_reasons
    "entry_exhaustion_filter_enabled", "max_entry_vwap_extension_atr", "max_entry_ema9_extension_atr",
    "max_entry_bar_range_atr", "max_entry_upper_wick_frac", "max_entry_lower_wick_frac",
    "entry_wick_close_position_guard",
    # BaseStrategy._adaptive_management_components
    "adaptive_breakeven_rr", "adaptive_breakeven_offset_r", "adaptive_profit_lock_rr",
    "adaptive_profit_lock_stop_rr", "adaptive_runner_trigger_rr", "adaptive_partial_breakeven_rr",
    "adaptive_partial_breakeven_offset_r", "fvg_runner_rr_bonus", "adaptive_runner_target_rr",
    "adaptive_runner_trail_pct",
    # BaseStrategy._build_ladder_rungs (``_ladder_param``)
    "ladder_max_rungs", "ladder_min_target_rr", "ladder_zone_atr_mult",
    # SharedEntryPolicy: the FVG score terms, the anti-chase retest and the
    # bar-close position
    "htf_fvg_entry_weight", "ltf_fvg_entry_weight", "invalidated_opposing_fvg_bonus",
    "opposing_fvg_active_penalty", "opposing_fvg_entry_penalty_mult", "opposing_fvg_validated_penalty",
    "same_direction_fvg_active_bonus", "same_direction_fvg_invalidated_penalty",
    "same_direction_fvg_validated_bonus",
    "anti_chase_fvg_retest_enabled", "anti_chase_fvg_retest_lookback_bars",
    "anti_chase_fvg_retest_max_gap_distance_pct", "anti_chase_fvg_retest_max_opposing_distance_pct",
    "anti_chase_fvg_retest_min_close_position", "anti_chase_fvg_retest_stop_buffer_gap_frac",
    "anti_chase_fvg_edge_tolerance_pct", "anti_chase_fvg_retest_skip_vwap_ema9_reclaim",
    "anti_chase_fvg_retest_trigger_tolerance_pct", "min_bar_close_position",
    # rvol: the symbol sets, the score floors and cap, the gate floors and
    # multipliers (read through its ``_param`` helper)
    "rvol_benchmark_symbols", "rvol_high_liquidity_symbols", "rvol_high_liquidity_dollar_volume",
    "rvol_score_floor_benchmark", "rvol_score_floor_high_liquidity", "rvol_score_floor_standard", "rvol_score_cap",
    "rvol_gate_multiplier_benchmark", "rvol_gate_floor_benchmark", "rvol_gate_multiplier_high_liquidity",
    "rvol_gate_floor_high_liquidity",
    # RiskManager's correlation concentration guard
    "correlation_groups", "max_same_correlation_group_same_direction",
})


@dataclass(frozen=True, slots=True)
class StrategyManifest:
    name: str
    strategy_module: str
    screener_module: str
    strategy_class: str
    screener_class: str
    entry_windows: list[tuple[str, str]]
    management_windows: list[tuple[str, str]]
    screener_windows: list[tuple[str, str]]
    params: dict[str, Any] = field(default_factory=dict)
    plugin_type: str = "stock"
    capabilities: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    manifest_path: str | None = None
