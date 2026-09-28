# SPDX-License-Identifier: MIT
from typing import Any

from ...models import Side
from ..peer_confirmed_key_levels.screener import PeerConfirmedKeyLevelsScreener


class PeerConfirmedTrendContinuationScreener(PeerConfirmedKeyLevelsScreener):
    """The key_levels screen with a trend bias: a move from open past +0.30%
    hints LONG, past -0.30% SHORT."""

    strategy_name = 'peer_confirmed_trend_continuation'

    def _score_row(self, day_change: float, effective_relative_volume: float, params: dict[str, Any]) -> tuple[float, Side | None, dict[str, Any]]:
        focus_score = abs(day_change) * effective_relative_volume
        directional_bias = None
        if day_change > 0.30:
            directional_bias = Side.LONG
        elif day_change < -0.30:
            directional_bias = Side.SHORT
        return focus_score, directional_bias, {"trend_focus_score": float(focus_score)}
