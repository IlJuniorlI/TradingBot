# SPDX-License-Identifier: MIT
"""Strategy for `small_cap_squeeze`.

A long-only, multi-regime small-cap "squeeze" strategy. It REUSES the
top_tier_adaptive multi-regime engine (trend / pullback / range / vol_squeeze /
momentum) unchanged — only the universe, the disabled regimes, and the tuning
differ, and all of that lives in config:

  * Universe: a DYNAMIC TradingView screener (low-float names gapping premarket
    on heavy volume) instead of a fixed tradable list — see ``screener.py``.
  * Regimes: ORB and sr_scalp are OFF (``disable_orb_regime`` /
    ``disable_sr_scalp_regime``); with ORB off the open trades the normal
    trend/pullback/range/vol_squeeze/momentum mix continuously from 08:05.
  * Confirmations: no index/ETF confirmation (``require_index_confirmation:
    false``), no sector groups / concentration guard (empty ``sector_groups``),
    no relative-strength gate (``relative_strength_block_threshold_pct: 0``).
  * Long only (``risk.allow_short: false``); entries 08:05-11:50 in extended-
    indicator mode with EVERY screened symbol extended-hours eligible
    (``extended_hours_tradable_all: true``).

The squeeze tuning — wider stops, loosened "don't chase extension" gates (a
squeezer is always extended), hybrid scalp+runner management — lives in
``configs/config.small_cap_squeeze.yaml``. No engine logic is overridden here;
this subclass exists only to bind a distinct ``strategy_name`` (and therefore
its own manifest + params block) to the shared top_tier engine. The plugin
registry requires the class to be defined in this module, so it cannot simply
point at ``TopTierAdaptiveStrategy`` directly.
"""
from __future__ import annotations

from ..top_tier_adaptive.strategy import TopTierAdaptiveStrategy


class SmallCapSqueezeStrategy(TopTierAdaptiveStrategy):
    strategy_name = "small_cap_squeeze"
