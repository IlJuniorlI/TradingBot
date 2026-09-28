# SPDX-License-Identifier: MIT
"""The regimes of the top_tier_adaptive engine, one module each.

Each module holds a regime's scorer and its builder (and the helpers only
that pair reads) as a mixin of ``TopTierAdaptiveStrategy``; every builder
ends in the strategy's ``_finalize_signal``.
"""
