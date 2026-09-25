# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
    Position,
    Side,
    Signal,
    insufficient_bars_reason,
    _reason_with_values,
    _safe_float,
    pd,
)
from ..shared_entry import EntryProposal
from ..strategy_base import BaseStrategy

class PairsResidualStrategy(BaseStrategy):
    """Relative-value entries on the traded leg of a configured pair.

    The z-score of the leg's rolling return residual against its reference
    picks the side; the leg's own chart then has to carry the trade. One
    proposal per candidate on the leg's frame (style / family ``pairs``,
    ``EntryProposal.symbol`` = the leg): a z-score past ``max_zscore_entry``
    and the exhaustion checks are its pending reasons, the stop / target the
    ``risk.default_stop_pct`` / ``default_target_pct`` defaults, and every
    shared_entry knob -- the vetoes, the S/R and technical refinement, the
    score terms -- is applied by ``self.entry_policy.admit``. With no side
    ready (below ``zscore_entry``, side or shorts not allowed) the skip is
    recorded without a proposal. The manifest turns the divergence-only
    entries off: a pair trade needs its z-score.
    """

    strategy_name = 'pairs_residual'

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        return max(0, int(self.params.get("lookback_bars", 90) or 90))


    def __init__(self, config):
        super().__init__(config)
        self.pairs = config.pairs

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        lookback = int(self.params.get("lookback_bars", 90))
        entry_z = float(self.params.get("zscore_entry", 1.30))
        max_entry_z = max(entry_z, float(self.params.get("max_zscore_entry", 2.0)))
        global_allow_short = bool(self.config.risk.allow_short)
        for candidate in candidates:
            pair = candidate.metadata.get("pair")
            if not pair:
                continue
            symbol = pair.get("symbol") if isinstance(pair, dict) else getattr(pair, "symbol", None)
            reference = pair.get("reference") if isinstance(pair, dict) else getattr(pair, "reference", None)
            if not symbol or not reference:
                continue
            if symbol in positions:
                self._record_entry_decision(symbol, "skipped", ["already_in_position"])
                continue
            left = bars.get(symbol)
            right = bars.get(reference)
            if left is None or right is None or len(left) < lookback or len(right) < lookback:
                self._record_entry_decision(
                    symbol,
                    "skipped",
                    [
                        _reason_with_values(
                            "insufficient_pair_bars",
                            current=min(0 if left is None else len(left), 0 if right is None else len(right)),
                            required=lookback,
                            op=">=",
                            digits=0,
                            extras={
                                "left_bars": (0 if left is None else len(left), ">=", lookback),
                                "right_bars": (0 if right is None else len(right), ">=", lookback),
                            },
                        )
                    ],
                )
                continue
            merged = pd.DataFrame({"left": left["close"], "right": right["close"]}).dropna().tail(lookback)
            if len(merged) < lookback // 2:
                self._record_entry_decision(symbol, "skipped", [insufficient_bars_reason("insufficient_aligned_pair_bars", len(merged), lookback // 2)])
                continue
            left_ret = merged.left.pct_change().fillna(0)
            right_ret = merged.right.pct_change().fillna(0)
            window = max(10, lookback // 3)
            relative = (left_ret - right_ret).rolling(window, min_periods=window).sum().dropna()
            if len(relative) < 2:
                self._record_entry_decision(symbol, "skipped", [insufficient_bars_reason("insufficient_rolling_window", len(relative), 2)])
                continue
            rel_std = float(relative.std(ddof=0))
            if rel_std <= 0 or rel_std != rel_std:
                z = 0.0
            else:
                z = (float(relative.iloc[-1]) - float(relative.mean())) / rel_std
            last = left.iloc[-1]
            reasons: list[str] = []
            side_pref = str((pair.get("side_preference") if isinstance(pair, dict) else getattr(pair, "side_preference", "both")) or "both").strip().lower()
            allow_long = side_pref in {"both", "long"}
            allow_short = global_allow_short and side_pref in {"both", "short"}
            long_ready = allow_long and z >= entry_z
            short_ready = allow_short and z <= -entry_z
            if abs(z) > max_entry_z:
                reasons.append(_reason_with_values("relative_strength_too_extended", current=abs(z), required=max_entry_z, op="<=", digits=4))
            elif not (long_ready or short_ready):
                if abs(z) < entry_z:
                    reasons.append(_reason_with_values("relative_strength_abs_below_threshold", current=abs(z), required=entry_z, op=">=", digits=4))
                elif z <= -entry_z and not global_allow_short and side_pref in {"both", "short"}:
                    reasons.append("shorts_disabled")
                else:
                    reasons.append(f"side_preference_blocked({side_pref})")
            if long_ready:
                side = Side.LONG
            elif short_ready:
                side = Side.SHORT
            else:
                self._record_entry_decision(symbol, "skipped", reasons)
                continue
            long = side == Side.LONG
            last_close = _safe_float(last["close"])
            reasons.extend(self._entry_exhaustion_reasons(
                side, left, close=last_close,
                vwap=_safe_float(last.get("vwap"), last_close), ema9=_safe_float(last.get("ema9"), last_close),
            ))
            stop_pct = self.config.risk.default_stop_pct
            target_pct = self.config.risk.default_target_pct
            proposal = EntryProposal(
                candidate=candidate,
                direction=side,
                style="pairs",
                style_family="pairs",
                close=last_close,
                stop=last_close * (1.0 - stop_pct) if long else last_close * (1.0 + stop_pct),
                target=last_close * (1.0 + target_pct) if long else last_close * (1.0 - target_pct),
                gate_frame=left,
                sr_frame=left,
                level_frame=left,
                data=data,
                pending_reasons=tuple(reasons),
                symbol=symbol,
            )
            admitted = self.entry_policy.admit(proposal)
            if admitted is None:
                refusal = self._consume_build_failure_payload(symbol, proposal.style)
                self._record_entry_decision(symbol, "skipped", refusal["reasons"])
                continue
            ms_ctx = admitted.ms
            trend = "bullish" if long else "bearish"
            structure_bonus = 0.75 if getattr(ms_ctx, "bias", "neutral") == trend else 0.0
            bos, bos_age = ("bos_up", "bos_up_age_bars") if long else ("bos_down", "bos_down_age_bars")
            if bool(getattr(ms_ctx, bos, False)) and self._structure_event_recent(getattr(ms_ctx, bos_age, None)):
                structure_bonus += 0.5
            # Slightly widened runner gate: allow runners up to 1.9x
            # the entry threshold when continuation bias is moderate
            # (0.20+) and HTF structure agrees. This lets extended
            # residual setups run longer without changing the entry
            # gate — no new entries, just smarter exits on already-
            # selected pairs.
            fvg_cont_bias = float(admitted.fvg["fvg_continuation_bias"])
            runner_allowed = bool(abs(z) <= (entry_z * 1.9) and fvg_cont_bias >= 0.20 and getattr(ms_ctx, "bias", "neutral") == trend)
            management = self._adaptive_management_components(side, last_close, admitted.stop, admitted.target, style="pairs", runner_allowed=runner_allowed, continuation_bias=fvg_cont_bias)
            # final_priority_score = this + the shared context score (emit),
            # the same total as before 2026-09-24.
            strategy_score = (abs(z) * 100.0) + (float(candidate.activity_score) * 0.25) + structure_bonus
            reason = f"relative_strength_z={z:.2f}" if long else f"relative_weakness_z={z:.2f}"
            out.append(
                self.entry_policy.emit(
                    admitted,
                    reason=reason,
                    strategy_score=strategy_score,
                    management=management,
                    target=admitted.target,
                    metadata={"benchmark": reference, "zscore": z, "side_preference": side_pref},
                    reference_symbol=reference,
                    pair_id=f"{symbol}:{reference}",
                )
            )
            self._record_entry_decision(symbol, "signal", [reason])
        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
