# SPDX-License-Identifier: MIT
"""The dashboard's key-level zones, from the zones the strategy's level hooks
proposed: each zone's flip state, one zone per kind and price, a support and
a resistance that overlap trimmed to the midpoint between them, and the
zones drawn, a confirmed flip of the S/R row within the row's side
tolerance of the row's nearest level of its new role (the S/R build's merge
distance, so usually a member of that level's cluster) in that level's
zone. ``DashboardCache.strategy_level_zones`` reads the HTF context, the LTF
frame and the strategy's hooks and hands the zones here, with the S/R row's
side tolerance, where nothing else is read but the clock (the flip check
counts the completed bars of the frame it is given). Until 2026-09-27 this
was the second half of that method, as nested closures (refactor cut C40).
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from .numeric import safe_float
from .support_resistance import zone_flip_confirmed

# Key-level zone kinds for a level price has crossed: broken_* once the flip
# is confirmed, pending_* while it is not. Each is drawn as its own zone, in
# its flipped role once confirmed and marked pending until then, except a
# broken level of the S/R row within the row's side tolerance of the row's
# nearest level of its new role (_fold_broken_zones).
_BROKEN_LEVEL_KINDS = frozenset({"broken_htf_support", "broken_htf_resistance"})
_FLIP_CANDIDATE_LEVEL_KINDS = _BROKEN_LEVEL_KINDS | {"pending_htf_support", "pending_htf_resistance"}


def level_anchors(entries: list[tuple[float | None, str, bool]]) -> list[tuple[float, str, bool]]:
    """The S/R row's levels as (price, kind, the S/R builder's flip verdict),
    in the order given, without a missing or non-positive price or a price
    (to 4 decimals) an earlier level holds: the generic-fallback zones'
    anchors. A flipped or pending level listed first labels its price alone,
    except that a plain level at a confirmed flip's price is kept, at the
    flip's price, so the two merge into one zone labelled with both
    (``_collapse_duplicate_zones``), as a flip near the nearest level of its
    new role is drawn (``_fold_broken_zones``). Until 2026-10-07 the plain
    level was dropped, and the zone read "BR" where a near one read "BR ·
    HS"."""
    deduped: list[tuple[float, str, bool]] = []
    held: dict[float, list[tuple[float, str]]] = {}
    for price, kind_name, flip_confirmed in entries:
        value = safe_float(price)
        if value is None or round(value, 4) <= 0:
            continue
        earlier = held.setdefault(round(value, 4), [])
        if earlier:
            if kind_name in _FLIP_CANDIDATE_LEVEL_KINDS or any(kind not in _BROKEN_LEVEL_KINDS for _, kind in earlier):
                continue
            value = earlier[0][0]
        earlier.append((value, kind_name))
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


def _is_sr_row_zone(zone: dict[str, Any]) -> bool:
    # The generic-fallback zones, the S/R row's levels, carry the S/R
    # builder's flip verdict; a strategy's own candidates carry None.
    return zone.get("builder_flip_confirmed") is not None


def _between_them(zones: list[dict[str, Any]], first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Whether a zone of ``zones`` of the other kind than ``first`` is priced
    strictly between ``first`` and ``second``."""
    low, high = sorted((float(first["price"]), float(second["price"])))
    return any(str(zone["kind"]) != str(first["kind"]) and low < float(zone["price"]) < high for zone in zones)


def _fold_broken_zones(zones: list[dict[str, Any]], *, side_tolerance: float | None) -> list[dict[str, Any]]:
    """The drawn ``zones`` with each confirmed flip of the S/R row that lies
    within the row's ``side_tolerance`` of the row's nearest level of its
    new role, when that level is drawn and no zone of the other kind is
    priced between the two, drawn in that level's zone.

    The S/R build publishes each cluster at its strongest member's price and
    derives broken_* from the raw levels at their own prices, never spaced
    against the ladder, so a lost support within the merge distance of the
    nearest resistance drew as a second zone just under it (TSM 2026-10-06:
    BS 485.42 under HR 485.62, side_tolerance 1.452). The zone keeps the
    nearest level's price (the one the strategy reads) and spans from the
    lower member's lower edge to the upper member's upper edge; the broken
    level's labels and sources come first and its flip state is the
    zone's, as at a merge at one price, where a flip candidate outranks a
    plain zone. That span would cover a zone of the other kind priced
    between the two (a pending level the overlap trim split them around),
    so such a pair keeps two zones, as do a flip farther away, a pending
    level, a strategy's own candidate and a zone of the other kind; without
    the row's finite, positive tolerance nothing folds. One plain zone of
    each kind is drawn, so a kind holds at most one such nearest level."""
    if side_tolerance is None or not math.isfinite(side_tolerance) or side_tolerance <= 0.0:
        return zones
    nearest = {
        str(zone["kind"]): zone
        for zone in zones
        if _is_sr_row_zone(zone) and _zone_level_kind(zone) not in _FLIP_CANDIDATE_LEVEL_KINDS
    }
    drawn: list[dict[str, Any]] = []
    for zone in zones:
        host = nearest.get(str(zone["kind"]))
        if (
            host is None
            or not _is_sr_row_zone(zone)
            or _zone_level_kind(zone) not in _BROKEN_LEVEL_KINDS
            or abs(float(zone["price"]) - float(host["price"])) > float(side_tolerance)
            or _between_them(zones, zone, host)
        ):
            drawn.append(zone)
            continue
        host["lower"] = min(float(host["lower"]), float(zone["lower"]))
        host["upper"] = max(float(host["upper"]), float(zone["upper"]))
        host["labels"] = list(dict.fromkeys([*list(zone.get("labels", []) or []), *list(host.get("labels", []) or [])]))
        host["sources"] = list(dict.fromkeys([*list(zone.get("sources", []) or []), *list(host.get("sources", []) or [])]))
        for key in ("original_kind", "confirmed_flip", "flip_state", "pending_flip", "pending_state", "flip_target_kind"):
            host[key] = zone[key]
    return drawn


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
    side_tolerance: float | None,
) -> list[dict[str, Any]]:
    """The zones drawn, from ``candidate_zones`` (the supports, then the
    resistances, as ``DashboardCache.strategy_level_zones`` built them from
    the strategy's candidates). Each takes its flip state on ``flip_frame``
    with the trading-mode ``flip_confirmation_bars`` (1m, 5m), so the chart
    and position management agree on which levels have flipped; zones of one
    kind at one price merge; an overlapping support and resistance above it
    split the gap between them, the nearest pair first; and the drawn set is
    the zone selected for entry with the nearest opposite one, else the
    nearest plain zone of each kind and every broken / pending level, a
    confirmed flip of the S/R row within the row's ``side_tolerance`` of the
    drawn nearest level of its new role, with no zone of the other kind
    priced between them, drawn in that level's zone."""
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
    # level included, to zero width (2026-09-23). The nearest pair splits
    # first, from the zones' current edges, never out: each facing edge
    # moves to the midpoint, and each far edge in to no farther from the
    # zone's price than its facing edge. A farther pair trims only what
    # still overlaps, and the bands do not depend on the order the zones
    # came in. Until 2026-10-07 each pair was cut from the original
    # zone_half_width, so a farther resistance listed later re-widened a
    # support a nearer one had trimmed (HS [99.35, 100.65] over BS
    # [100.50, 101.50]), and a band narrower than its half-width (a
    # strategy's own bounds) was widened.
    pairs = sorted(
        (
            (support, resistance)
            for support in support_zones
            for resistance in resistance_zones
            if float(support.get("price", 0.0) or 0.0) < float(resistance.get("price", 0.0) or 0.0)
        ),
        key=lambda pair: (
            float(pair[1].get("price", 0.0) or 0.0) - float(pair[0].get("price", 0.0) or 0.0),
            float(pair[0].get("price", 0.0) or 0.0),
        ),
    )
    for support, resistance in pairs:
        support_upper = float(support.get("upper", 0.0) or 0.0)
        resistance_lower = float(resistance.get("lower", 0.0) or 0.0)
        if support_upper < resistance_lower:
            continue
        midpoint = (float(support.get("price", 0.0) or 0.0) + float(resistance.get("price", 0.0) or 0.0)) / 2.0
        support["upper"] = min(support_upper, midpoint)
        resistance["lower"] = max(resistance_lower, midpoint)
        support["lower"] = max(float(support.get("lower", 0.0) or 0.0), 2.0 * float(support.get("price", 0.0) or 0.0) - support["upper"])
        resistance["upper"] = min(float(resistance.get("upper", 0.0) or 0.0), 2.0 * float(resistance.get("price", 0.0) or 0.0) - resistance["lower"])

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
        # level as its own zone unless it lies within the row's side
        # tolerance of the row's nearest level of its new role (below). A
        # flipped level no longer competes with the nearest one for the
        # single support / resistance slot (until 2026-09-23 the S/R row
        # folded a broken resistance into the support ladder, so the zone
        # drawn was whichever of the two was nearer, not the level the
        # strategy reads).
        plain_zones = [item for item in ordered if _zone_level_kind(item) not in _FLIP_CANDIDATE_LEVEL_KINDS]
        nearest_support = sorted([item for item in plain_zones if str(item.get("kind", "") or "") == "support"], key=lambda item: abs(float(item.get("price", 0.0) or 0.0) - float(close)))
        nearest_resistance = sorted([item for item in plain_zones if str(item.get("kind", "") or "") == "resistance"], key=lambda item: abs(float(item.get("price", 0.0) or 0.0) - float(close)))
        display_zones = [item for item in ordered if _zone_level_kind(item) in _FLIP_CANDIDATE_LEVEL_KINDS]
        if nearest_support:
            display_zones.append(nearest_support[0])
        if nearest_resistance:
            display_zones.append(nearest_resistance[0])
        display_zones = sorted(display_zones, key=lambda item: (float(item["price"]), item["kind"]))
        # Among the zones drawn, so a flip whose nearest level is not drawn
        # keeps its zone, and after the trim, so each member is split
        # against the other kind on its own band first. The zone spans
        # from the lower member's lower edge to the upper member's upper
        # edge, so a pair with a zone of the other kind priced between
        # them keeps two zones.
        display_zones = _fold_broken_zones(display_zones, side_tolerance=side_tolerance)

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
