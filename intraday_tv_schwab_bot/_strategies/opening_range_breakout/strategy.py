# SPDX-License-Identifier: MIT
import logging
import math
from zoneinfo import ZoneInfo

from ..shared import (
    Candidate,
    Position,
    Side,
    Signal,
    insufficient_bars_reason,
    _reason_with_values,
    _safe_float,
    _same_day_mask,
    _time_gte_mask,
    datetime,
    now_et,
    pd,
    time,
    timedelta,
)
from ..shared_entry import EntryContexts, EntryProposal, RetestTrigger
from ..strategy_base import BaseStrategy

_ET_ZONE = ZoneInfo("America/New_York")

LOG = logging.getLogger(__name__)
_VALID_ORB_WATCHLIST_MODES = {"none", "premarket", "early_session"}

# The pending reasons an FVG retest of the opening-range trigger may clear:
# price not through the trigger yet, or through it but stretched (the
# anti-chase exhaustion checks). Until 2026-09-24 two passes cleared them --
# the own reasons first, the exhaustion ones only once nothing else was
# pending -- and one pass over the union decides the same.
_RETEST_DEFERRABLE = frozenset({
    "no_orb_breakout",
    "too_extended_from_vwap_atr",
    "too_extended_from_ema9_atr",
    "upper_wick_rejection",
    "expansion_bar_too_large",
})


class ORBStrategy(BaseStrategy):
    """Long-only opening-range breakout (microcap_gap_orb inherits it).

    One LONG proposal per candidate (style / family ``orb``): the setup's own
    blockers and the anti-chase exhaustion checks are its pending reasons,
    and an FVG retest of the opening-range trigger may clear the breakout /
    exhaustion ones. The stop is the opening-range low, the target a 2.0R
    (2.5R on a strong, structure-confirmed break) measured move. Every
    shared_entry knob -- the vetoes, the refinement, the retest stop anchor,
    the score terms -- is applied by ``self.entry_policy.admit``. The
    ``orb`` family puts the entry under the exit side's ORB grace
    (``support_resistance.orb_entry_exit_grace_minutes``), which until
    2026-09-24 covered only top_tier's ORB regime.
    """

    strategy_name = 'opening_range_breakout'

    @classmethod
    def normalize_params(cls, params: dict[str, object]) -> dict[str, object]:
        out = super().normalize_params(params)
        mode = str(out.get("orb_watchlist_mode", "premarket")).strip().lower()
        if mode not in _VALID_ORB_WATCHLIST_MODES:
            LOG.warning("Unsupported ORB orb_watchlist_mode=%r; using 'premarket'. Valid values: %s", mode, sorted(_VALID_ORB_WATCHLIST_MODES))
            mode = "premarket"
        out["orb_watchlist_mode"] = mode
        return out
    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        min_bars = int(self.params.get("min_bars", 30) or 30)
        opening_range_minutes = int(self.params.get("opening_range_minutes", 5))
        buffer_pct = float(self.params.get("min_breakout_buffer_pct", 0.0012))
        for c in candidates:
            reasons: list[str] = []
            frame = bars.get(c.symbol)
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue
            if frame is None or len(frame) < min_bars:
                self._record_entry_decision(c.symbol, "skipped", [insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), min_bars)])
                continue
            day = now_et().date()
            session = frame[_same_day_mask(frame, day)]
            if len(session) < opening_range_minutes + 2:
                self._record_entry_decision(c.symbol, "skipped", [insufficient_bars_reason("opening_range_incomplete", len(session), opening_range_minutes + 2)])
                continue
            opening_start_time = time(9, 30)
            after_start_time = (datetime.combine(day, opening_start_time, tzinfo=_ET_ZONE) + timedelta(minutes=max(0, opening_range_minutes))).time()
            times_series = session.index.to_series().map(lambda ts: ts.time())
            opening_mask = (times_series >= opening_start_time) & (times_series < after_start_time)
            opening = session[opening_mask.to_numpy()]
            after = session[_time_gte_mask(session, after_start_time)]
            if opening.empty or after.empty:
                self._record_entry_decision(
                    c.symbol,
                    "skipped",
                    [
                        _reason_with_values(
                            "opening_range_incomplete",
                            current=len(opening),
                            required=1,
                            op=">=",
                            digits=0,
                            extras={"after_bars": (len(after), ">=", 1)},
                        )
                    ],
                )
                continue
            or_high = float(opening["high"].max())
            or_low = float(opening["low"].min())
            if math.isnan(or_high) or math.isnan(or_low):
                self._record_entry_decision(c.symbol, "skipped", ["opening_range_values_nan"])
                continue
            last = after.iloc[-1]
            trigger = or_high * (1.0 + buffer_pct)
            ctx = self._chart_context(frame)
            ms_ctx = self._structure_context(frame, "ltf")
            last_close = _safe_float(last["close"])
            pattern_ok = bool(ctx.matched_bullish_continuation or ctx.matched_bullish_reversal) or ctx.bias_score >= 0.0
            last_vwap = _safe_float(last["vwap"], last_close)
            last_ema9 = _safe_float(last["ema9"], last_close)
            last_ema20 = _safe_float(last["ema20"], last_close)
            if last_close <= trigger:
                reasons.append(_reason_with_values("no_orb_breakout", current=last_close, required=trigger, op=">", digits=4))
            if last_close <= last_vwap:
                reasons.append(_reason_with_values("below_vwap", current=last_close, required=last_vwap, op=">", digits=4))
            if last_ema9 < last_ema20:
                reasons.append(_reason_with_values("ema9_below_ema20", current=last_ema9, required=last_ema20, op=">=", digits=4))
            if not pattern_ok:
                reasons.append("chart_pattern_not_supportive")
            reasons.extend(self._entry_exhaustion_reasons(Side.LONG, frame, close=last_close, vwap=last_vwap, ema9=last_ema9))
            breakout_pct = max(0.0, (last_close - trigger) / trigger) if trigger > 0 else 0.0
            ms_bias = getattr(ms_ctx, "bias", "neutral")
            # Adaptive target RR: base 2.0, extended to 2.5 when the
            # breakout is visibly strong (>1.5% above trigger) AND HTF
            # structure bias confirms bullish. Non-restrictive — never
            # TIGHTENS the target, only lets strong breakouts run farther.
            base_rr = 2.5 if breakout_pct >= 0.015 and ms_bias == "bullish" else 2.0
            proposal = EntryProposal(
                candidate=c,
                direction=Side.LONG,
                style="orb",
                style_family="orb",
                close=last_close,
                stop=or_low,
                target=last_close + (last_close - or_low) * base_rr,
                gate_frame=frame,
                sr_frame=frame,
                level_frame=frame,
                data=data,
                pending_reasons=tuple(reasons),
                deferrable=_RETEST_DEFERRABLE,
                retest=RetestTrigger(trigger, bool(last_close > trigger), last_vwap, last_ema9),
                contexts=EntryContexts(ms=ms_ctx, chart=ctx),
            )
            admitted = self.entry_policy.admit(proposal)
            if admitted is None:
                refusal = self._consume_build_failure_payload(c.symbol, proposal.style)
                self._record_entry_decision(c.symbol, "skipped", refusal["reasons"])
                continue
            structure_bonus = 0.75 if ms_bias == "bullish" else 0.0
            pattern_bonus = 0.35 if ctx.matched_bullish_continuation else 0.15 if ctx.matched_bullish_reversal else 0.0
            fvg_continuation_bias = float(admitted.fvg["fvg_continuation_bias"])
            runner_allowed = bool(fvg_continuation_bias >= 0.35 and (ctx.matched_bullish_continuation or ms_bias == "bullish"))
            management = self._adaptive_management_components(
                Side.LONG, last_close, admitted.stop, admitted.target,
                style="breakout", runner_allowed=runner_allowed, continuation_bias=fvg_continuation_bias,
            )
            strategy_score = float(c.activity_score) + (breakout_pct * 200.0) + structure_bonus + pattern_bonus
            reason = "smallcap_orb_fvg_retest" if admitted.admitted_via_retest else "smallcap_orb_breakout"
            if ctx.matched_bullish_continuation:
                reason += f":{'+'.join(sorted(ctx.matched_bullish_continuation))}"
            out.append(self.entry_policy.emit(
                admitted, reason=reason, strategy_score=strategy_score, management=management, target=admitted.target,
                metadata={"or_high": or_high, "or_low": or_low},
            ))
            self._record_entry_decision(c.symbol, "signal", [reason])
        return out

    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
