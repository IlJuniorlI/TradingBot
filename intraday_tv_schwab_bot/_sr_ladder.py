# SPDX-License-Identifier: MIT
"""Price-ladder + support-resistance tolerance helpers.

Originally defined as module-level functions in ``engine.py`` but needed
by ``DashboardCache`` (for the dashboard SR row) and various engine-side
management paths. Hoisted into their own module to avoid a circular import
between ``engine.py`` and ``dashboard_cache.py``.

Exports:
  - ``_same_side_ladder_min_gap_pct(config, reference_price)``: minimum gap
    between same-side ladder rungs, as an absolute price value derived
    from ``config.support_resistance.same_side_min_gap_pct``.
  - ``_sr_effective_side_tolerance(config, reference_price, *, atr, sr_ctx)``:
    side-tolerance for clustering / filtering levels. Prefers the value from
    an already-built ``SupportResistanceContext`` when available, else falls
    back to config-derived atr/pct tolerances combined with the same-side
    min gap.
  - ``_collapse_price_ladder(values, *, reverse, min_gap)``: dedupe + sort
    a list of prices into a monotonic ladder, collapsing any pair within
    ``min_gap``.
  - ``_select_next_distinct_level(levels, anchor_price, *, above, minimum_gap)``:
    pick the next level strictly above/below ``anchor_price`` by more than
    ``minimum_gap``, from an ordered list.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import BotConfig

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


def _same_side_ladder_min_gap_pct(config: BotConfig, reference_price: float | None) -> float:
    # support_resistance's tolerances are checked at load (finite numbers
    # above 0, config._NUMBER_CHECKS) and read as they are.
    pct = float(config.support_resistance.same_side_min_gap_pct)
    price = abs(float(reference_price or 0.0))
    return max(price * pct, 1e-4)


def _sr_effective_side_tolerance(config: BotConfig, reference_price: float | None, *, atr: float | None = None, sr_ctx: Any | None = None) -> float:
    try:
        if sr_ctx is not None:
            ctx_tol = float(getattr(sr_ctx, 'side_tolerance', 0.0) or 0.0)
            if ctx_tol > 0:
                return ctx_tol
    except Exception:
        LOG.debug("Failed to read side_tolerance from support/resistance context; using config fallback.", exc_info=True)
    cfg = config.support_resistance
    atr_tolerance_mult = float(cfg.atr_tolerance_mult)
    pct_tolerance = float(cfg.pct_tolerance)
    min_gap_atr_mult = float(cfg.same_side_min_gap_atr_mult)
    price = abs(float(reference_price or 0.0))
    atr_value = abs(float(atr or 0.0))
    merge_tol = max(atr_value * atr_tolerance_mult, price * pct_tolerance)
    same_side_gap = max(atr_value * min_gap_atr_mult, _same_side_ladder_min_gap_pct(config, price))
    return max(merge_tol, same_side_gap, 1e-4)


def _select_next_distinct_level(levels: list[Any] | None, anchor_price: float | None, *, above: bool, minimum_gap: float) -> Any | None:
    if not levels:
        return None
    if anchor_price is None:
        return levels[0]
    tol = max(float(minimum_gap or 0.0), 1e-6)
    anchor = float(anchor_price)
    for level in levels:
        price = float(level.price)
        if price <= 0:
            continue
        if above:
            if price > anchor + tol:
                return level
        else:
            if price < anchor - tol:
                return level
    return None


def _collapse_price_ladder(values: list[float], *, reverse: bool, min_gap: float) -> list[float]:
    ordered = sorted((float(v) for v in values if float(v) > 0), reverse=reverse)
    collapsed: list[float] = []
    for value in ordered:
        if not collapsed or abs(float(value) - float(collapsed[-1])) > max(float(min_gap), 1e-4):
            collapsed.append(float(value))
    return collapsed
