# SPDX-License-Identifier: MIT
"""The dashboard's key-level zones, from the zones the strategy's level hooks
proposed: each zone's flip state, one zone per kind and price, a support and
a resistance that overlap trimmed to the midpoint between them, and the
zones drawn. ``DashboardCache.strategy_level_zones`` reads the HTF context,
the LTF frame and the strategy's hooks and hands the zones here, where
nothing else is read but the clock (the flip check counts the completed bars
of the frame it is given). Until 2026-09-27 this was the second half of that
method, as nested closures (refactor cut C40).
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from .numeric import safe_float
from .support_resistance import zone_flip_confirmed

# Key-level zone kinds for a level price has crossed: broken_* once the flip
# is confirmed, pending_* while it is not. Each is drawn as its own zone, in
# its flipped role once confirmed and marked pending until then.
_FLIP_CANDIDATE_LEVEL_KINDS = frozenset({
    "broken_htf_support",
    "broken_htf_resistance",
    "pending_htf_support",
    "pending_htf_resistance",
})


def level_anchors(entries: list[tuple[float | None, str, bool]]) -> list[tuple[float, str, bool]]:
    """The S/R row's levels as (price, kind, the S/R builder's flip verdict),
    in the order given, without a missing, non-positive or repeated price
    (to 4 decimals): the generic-fallback zones' anchors."""
    deduped: list[tuple[float, str, bool]] = []
    seen: set[float] = set()
    for price, kind_name, flip_confirmed in entries:
        value = safe_float(price)
        if value is None or round(value, 4) <= 0 or round(value, 4) in seen:
            continue
        seen.add(round(value, 4))
        deduped.append((value, kind_name, flip_confirmed))
    return deduped


def _zone_level_kind(zone: dict[str, Any]) -> str:
    return str(zone.get("engine_level_kind", "") or "").strip().lower()


def _is_fvg_zone(zone: dict[str, Any]) -> bool:
    kind_name = _zone_level_kind(zone)
    return kind_name in {"bullish_htf_fvg", "bearish_htf_fvg"} or "fvg" in kind_name


def _zone_original_kind(zone: dict[str, Any]) -> str | None:
    kind_name = _zone_level_kind(zone)
    if not kind_name or _is_fvg_zone(zone):
        return None
    if kind_name in {"broken_htf_support", "pending_htf_support"}:
        return "support"
    if kind_name in {"broken_htf_resistance", "pending_htf_resistance"}:
        return "resistance"
    if kind_name in {"prior_day_low", "prior_week_low"} or kind_name.endswith("_low"):
        return "support"
    if kind_name in {"prior_day_high", "prior_week_high"} or kind_name.endswith("_high"):
        return "resistance"
    if "support" in kind_name and "resistance" not in kind_name:
        return "support"
    if "resistance" in kind_name and "support" not in kind_name:
        return "resistance"
    return None


def _zone_flipped_kind(kind_name: str | None) -> str | None:
    if kind_name == "support":
        return "resistance"
    if kind_name == "resistance":
        return "support"
    return None


def _apply_zone_confirmation_state(
    zone: dict[str, Any],
    *,
    flip_frame: pd.DataFrame | None,
    confirm_1m_bars: int,
    confirm_5m_bars: int,
    fallback_bar: tuple[float, float] | None,
    eps: float,
) -> dict[str, Any]:
    original_kind = _zone_original_kind(zone)
    if original_kind is None:
        return zone
    flipped_kind = _zone_flipped_kind(original_kind)
    if flipped_kind is None:
        return zone
    level_kind = _zone_level_kind(zone)
    builder_verdict = zone.get("builder_flip_confirmed")
    if builder_verdict is None:
        confirmed = zone_flip_confirmed(
            original_kind,
            float(zone.get("lower", 0.0) or 0.0),
            float(zone.get("upper", 0.0) or 0.0),
            flip_frame=flip_frame,
            confirm_1m_bars=confirm_1m_bars,
            confirm_5m_bars=confirm_5m_bars,
            fallback_bar=fallback_bar,
            eps=eps,
        )
    else:
        # The builder confirmed (broken_*) or has yet to confirm
        # (pending_*, nearest) this flip on the level price; the
        # zone-edge check above answers a different question and
        # could relabel a confirmed breakout-retest level as pending.
        confirmed = bool(builder_verdict)
    sources = list(zone.get("sources", []) or [])
    zone["original_kind"] = str(original_kind)
    zone["confirmed_flip"] = False
    zone["flip_state"] = "original"
    zone["pending_flip"] = False
    zone["pending_state"] = ""
    zone["flip_target_kind"] = ""
    if level_kind in _FLIP_CANDIDATE_LEVEL_KINDS:
        if confirmed:
            zone["kind"] = flipped_kind
            zone["confirmed_flip"] = True
            zone["flip_state"] = "confirmed_flip"
            zone["sources"] = list(dict.fromkeys([*sources, f"confirmed_broken_{original_kind}_zone"]))
        else:
            zone["kind"] = original_kind
            zone["flip_state"] = "pending_flip"
            zone["pending_flip"] = True
            zone["pending_state"] = "pending_break" if original_kind == "support" else "pending_reclaim"
            zone["flip_target_kind"] = flipped_kind
            zone["sources"] = list(dict.fromkeys([*sources, f"pending_broken_{original_kind}"]))
        return zone
    if confirmed:
        zone["kind"] = flipped_kind
        zone["confirmed_flip"] = True
        zone["flip_state"] = "confirmed_flip"
        zone["sources"] = list(dict.fromkeys([*sources, f"confirmed_flipped_{original_kind}_zone"]))
    else:
        zone["kind"] = original_kind
        zone["sources"] = list(dict.fromkeys(sources))
    return zone


def _zone_rank_key(zone: dict[str, Any]) -> tuple[float, ...]:
    level_kind = _zone_level_kind(zone)
    return (
        1.0 if bool(zone.get("selected_for_entry", False)) else 0.0,
        1.0 if not bool(zone.get("pending_flip", False)) else 0.0,
        1.0 if level_kind in _FLIP_CANDIDATE_LEVEL_KINDS else 0.0,
        float(zone.get("engine_level_score", 0.0) or 0.0),
        float(zone.get("score", 0.0) or 0.0),
        float(int(zone.get("touches", 0) or 0)),
    )


def _collapse_duplicate_zones(zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    collapsed: dict[tuple[str, float], dict[str, Any]] = {}
    for zone in zones:
        key = (str(zone.get("kind", "") or ""), round(float(zone.get("price", 0.0) or 0.0), 6))
        existing = collapsed.get(key)
        if existing is None:
            collapsed[key] = zone
            continue
        existing_key = _zone_rank_key(existing)
        zone_key = _zone_rank_key(zone)
        if zone_key > existing_key:
            best, other = zone, existing
        else:
            best, other = existing, zone
        best["labels"] = list(dict.fromkeys([*list(best.get("labels", []) or []), *list(other.get("labels", []) or [])]))
        best["sources"] = list(dict.fromkeys([*list(best.get("sources", []) or []), *list(other.get("sources", []) or [])]))
        best["selected_for_entry"] = bool(best.get("selected_for_entry", False) or other.get("selected_for_entry", False))
        collapsed[key] = best
    return list(collapsed.values())


def _zone_sort_key(zone: dict[str, Any], close: float) -> tuple[float, float, float]:
    price = float(zone.get("price", 0.0) or 0.0)
    selected_delta = 0.0 if bool(zone.get("selected_for_entry", False)) else 1.0
    distance = abs(price - float(close))
    return selected_delta, distance, -float(zone.get("engine_level_score", 0.0) or 0.0)


def build_level_zones(
    candidate_zones: list[dict[str, Any]],
    *,
    close: float,
    flip_frame: pd.DataFrame | None,
    flip_confirmation_bars: tuple[int, int],
    timeframe_minutes: int,
) -> list[dict[str, Any]]:
    """The zones drawn, from ``candidate_zones`` (the supports, then the
    resistances, as ``DashboardCache.strategy_level_zones`` built them from
    the strategy's candidates). Each takes its flip state on ``flip_frame``
    with the trading-mode ``flip_confirmation_bars`` (1m, 5m), so the chart
    and position management agree on which levels have flipped; zones of one
    kind at one price merge; an overlapping support and resistance split the
    gap between them; and the drawn set is the zone selected for entry with
    the nearest opposite one, else the nearest plain zone of each kind and
    every broken / pending level."""
    zone_flip_1m, zone_flip_5m = flip_confirmation_bars
    fallback_bar = None
    if flip_frame is not None and not flip_frame.empty:
        last_bar = flip_frame.iloc[-1]
        fallback_bar = (float(last_bar.get("high")), float(last_bar.get("low")))
    zone_eps = max(abs(float(close)) * 1e-6, 1e-8)
    all_zones = [
        _apply_zone_confirmation_state(zone, flip_frame=flip_frame, confirm_1m_bars=zone_flip_1m,
                                       confirm_5m_bars=zone_flip_5m, fallback_bar=fallback_bar, eps=zone_eps)
        for zone in candidate_zones
    ]
    all_zones = _collapse_duplicate_zones(all_zones)
    support_zones = [item for item in all_zones if str(item.get("kind")) == "support"]
    resistance_zones = [item for item in all_zones if str(item.get("kind")) == "resistance"]

    # Overlapping support / resistance zones split the gap at its
    # midpoint. Only a support BELOW a resistance is such a pair: a
    # pending level is drawn in its original role on the far side of
    # price (a pending support above a nearer resistance), and trimming
    # that crossed pair collapsed both zones, the strategy's own nearest
    # level included, to zero width (2026-09-23).
    for support in support_zones:
        support_price = float(support.get("price", 0.0) or 0.0)
        for resistance in resistance_zones:
            resistance_price = float(resistance.get("price", 0.0) or 0.0)
            if support_price >= resistance_price:
                continue
            support_upper = float(support.get("upper", 0.0) or 0.0)
            resistance_lower = float(resistance.get("lower", 0.0) or 0.0)
            if support_upper < resistance_lower:
                continue
            midpoint = (support_price + resistance_price) / 2.0
            support_half_width = max(0.0, min(float(support.get("zone_half_width", 0.0) or 0.0), midpoint - support_price))
            resistance_half_width = max(0.0, min(float(resistance.get("zone_half_width", 0.0) or 0.0), resistance_price - midpoint))
            support["lower"] = max(0.0, support_price - support_half_width)
            support["upper"] = support_price + support_half_width
            resistance["lower"] = max(0.0, resistance_price - resistance_half_width)
            resistance["upper"] = resistance_price + resistance_half_width

    support_zones = [item for item in support_zones if float(item.get("upper", 0.0) or 0.0) >= float(item.get("price", 0.0) or 0.0)]
    resistance_zones = [item for item in resistance_zones if float(item.get("lower", 0.0) or 0.0) <= float(item.get("price", 0.0) or 0.0)]
    ordered = sorted((support_zones + resistance_zones), key=lambda item: (float(item["price"]), item["kind"]))

    selected_zones = sorted([item for item in ordered if bool(item.get("selected_for_entry", False))], key=lambda item: _zone_sort_key(item, close))
    display_zones: list[dict[str, Any]]
    if selected_zones:
        primary_selected = selected_zones[0]
        primary_kind = str(primary_selected.get("kind", "") or "")
        opposite_kind = "resistance" if primary_kind == "support" else "support"
        opposite_candidates = [item for item in ordered if str(item.get("kind", "") or "") == opposite_kind and not bool(item.get("selected_for_entry", False))]
        if opposite_kind == "resistance":
            above = [item for item in opposite_candidates if float(item.get("price", 0.0) or 0.0) >= float(close)]
            preferred_pool = above if above else opposite_candidates
            opposite_candidates = sorted(preferred_pool, key=lambda item: (float(item.get("price", 0.0) or 0.0), -float(item.get("engine_level_score", 0.0) or 0.0)))
        else:
            below = [item for item in opposite_candidates if float(item.get("price", 0.0) or 0.0) <= float(close)]
            preferred_pool = below if below else opposite_candidates
            opposite_candidates = sorted(preferred_pool, key=lambda item: (-float(item.get("price", 0.0) or 0.0), -float(item.get("engine_level_score", 0.0) or 0.0)))
        display_zones = [primary_selected]
        if opposite_candidates:
            display_zones.append(opposite_candidates[0])
        display_zones = sorted(display_zones, key=lambda item: (float(item["price"]), item["kind"]))
    else:
        # The nearest plain zone of each kind, plus every broken / pending
        # level as its own zone. A flipped level no longer competes with
        # the nearest one for the single support / resistance slot (until
        # 2026-09-23 the S/R row folded a broken resistance into the
        # support ladder, so the zone drawn was whichever of the two was
        # nearer, not the level the strategy reads).
        plain_zones = [item for item in ordered if _zone_level_kind(item) not in _FLIP_CANDIDATE_LEVEL_KINDS]
        nearest_support = sorted([item for item in plain_zones if str(item.get("kind", "") or "") == "support"], key=lambda item: abs(float(item.get("price", 0.0) or 0.0) - float(close)))
        nearest_resistance = sorted([item for item in plain_zones if str(item.get("kind", "") or "") == "resistance"], key=lambda item: abs(float(item.get("price", 0.0) or 0.0) - float(close)))
        display_zones = [item for item in ordered if _zone_level_kind(item) in _FLIP_CANDIDATE_LEVEL_KINDS]
        if nearest_support:
            display_zones.append(nearest_support[0])
        if nearest_resistance:
            display_zones.append(nearest_resistance[0])
        display_zones = sorted(display_zones, key=lambda item: (float(item["price"]), item["kind"]))

    return [
        {
            "kind": str(item["kind"]),
            "price": float(item["price"]),
            "lower": float(item["lower"]),
            "upper": float(item["upper"]),
            "score": float(item["score"]),
            "touches": int(item["touches"]),
            "labels": list(item["labels"]),
            "sources": list(item["sources"]),
            "timeframe": f"{timeframe_minutes}m",
            "zone_half_width": float(item.get("zone_half_width", 0.0) or 0.0),
            "pending_flip": bool(item.get("pending_flip", False)),
            "pending_state": str(item.get("pending_state", "") or ""),
            "flip_target_kind": str(item.get("flip_target_kind", "") or ""),
            "confirmed_flip": bool(item.get("confirmed_flip", False)),
            "flip_state": str(item.get("flip_state", "original") or "original"),
            "original_kind": str(item.get("original_kind", "") or ""),
            "engine_level_kind": item.get("engine_level_kind"),
            "engine_source_priority": float(item.get("engine_source_priority", 0.0) or 0.0),
            "engine_level_score": float(item.get("engine_level_score", 0.0) or 0.0),
            "passes_min_level_score": bool(item.get("passes_min_level_score", False)),
            "selected_for_entry": bool(item.get("selected_for_entry", False)),
        }
        for item in display_zones
    ]
