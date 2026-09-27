# SPDX-License-Identifier: MIT
"""Price-zone arithmetic shared by the fair-value-gap and order-block
builders: how far price is from a zone, how much of a zone price has traded
back into, and the minimum size and tolerances a builder derives from ATR.

Each builder keeps its own merge: a merged fair value gap keeps the smaller
``filled_pct`` of its parts and a merged order block the larger
(``fair_value_gaps._merge_fair_value_gaps``,
``order_blocks._merge_order_blocks``).
"""
from __future__ import annotations


def zone_distance(lower: float, upper: float, price: float) -> float:
    """How far ``price`` is from the zone ``[lower, upper]``: the distance to
    the nearer edge, 0.0 inside it. A bullish and a bearish zone measure
    alike."""
    if price < lower:
        return float(lower) - float(price)
    if price > upper:
        return float(price) - float(upper)
    return 0.0


def zone_filled_pct(lower: float, upper: float, extreme_after: float, *, bullish: bool) -> float:
    """The fraction of ``[lower, upper]`` price has traded back into, 0..1,
    given the extreme it reached after the zone formed: the lowest price
    after a bullish zone, which fills from the top down, the highest after a
    bearish one, which fills from the bottom up. The builder picks the
    series: a fair value gap is filled by wicks (lows / highs), an order
    block only by closes."""
    size = max(upper - lower, 1e-9)
    fill_top = min(upper, max(lower, extreme_after))
    filled = (upper - fill_top) if bullish else (fill_top - lower)
    return max(0.0, min(1.0, filled / size))


def zone_sizing(atr: float, price: float, *, min_atr_mult: float, min_pct: float) -> tuple[float, float, float]:
    """``(min_size, eps, merge_tol)`` for zones read at ``price`` on a frame
    whose ATR is ``atr``: the smallest zone a builder keeps (``min_atr_mult``
    ATRs or ``min_pct`` of price, whichever is larger), the margin a price
    must clear to count as beyond another, and how close two zones may sit
    and still merge."""
    min_size = max(float(atr) * float(min_atr_mult), float(price) * float(min_pct), 1e-8)
    eps = max(min_size * 0.05, float(price) * 1e-6, 1e-8)
    merge_tol = max(min_size * 0.25, float(price) * 0.00025, 1e-8)
    return min_size, eps, merge_tol
