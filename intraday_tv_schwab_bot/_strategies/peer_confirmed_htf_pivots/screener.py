# SPDX-License-Identifier: MIT
from typing import Any

from ...models import Candidate, Side
from ..peer_confirmed_key_levels.screener import PeerConfirmedKeyLevelsScreener


class PeerConfirmedHTFPivotsScreener(PeerConfirmedKeyLevelsScreener):
    """The key_levels screen scored for S/R scalps: a moderate move scores
    best (``move_fit``), and the bias is contrarian (a move up past the
    threshold hints SHORT, down LONG)."""

    strategy_name = 'peer_confirmed_htf_pivots'

    def _relative_volume_cap(self, params: dict[str, Any]) -> float:
        return max(0.75, float(params.get("screener_relative_volume_cap", 2.5) or 2.5))

    def _score_row(self, day_change: float, effective_relative_volume: float, params: dict[str, Any]) -> tuple[float, Side | None, dict[str, Any]]:
        abs_day_change = abs(day_change)
        move_sweet_spot = max(0.20, float(params.get("screener_activity_move_sweet_spot_pct", 1.25) or 1.25))
        move_cap = max(move_sweet_spot, float(params.get("screener_activity_move_cap_pct", 3.0) or 3.0))
        if abs_day_change <= move_sweet_spot:
            move_fit = 0.40 + (abs_day_change / move_sweet_spot)
        else:
            overshoot = min(1.0, (abs_day_change - move_sweet_spot) / max(move_cap - move_sweet_spot, 1e-9))
            move_fit = max(0.55, 1.40 - (overshoot * 0.80))
        focus_score = move_fit * effective_relative_volume
        bias_threshold = max(0.10, float(params.get("screener_contrarian_bias_threshold_pct", 1.0) or 1.0))
        directional_bias = None
        if day_change >= bias_threshold:
            directional_bias = Side.SHORT
        elif day_change <= (-bias_threshold):
            directional_bias = Side.LONG
        return focus_score, directional_bias, {
            "pivot_focus_score": float(focus_score),
            "activity_move_fit": float(move_fit),
            "abs_change_from_open": float(abs_day_change),
            "screener_bias_mode": "contrarian_sr_scalp",
        }

    @staticmethod
    def _sort_key(candidate: Candidate) -> tuple[float, float, float, int]:
        """The score, then the move fit, the effective RVOL and the
        configured order."""
        return (
            float(candidate.activity_score),
            float(candidate.metadata.get("activity_move_fit", 0.0) or 0.0),
            float(candidate.metadata.get("activity_relative_volume", 0.0) or 0.0),
            -int(candidate.metadata.get("configured_order", 9_999) or 9_999),
        )
