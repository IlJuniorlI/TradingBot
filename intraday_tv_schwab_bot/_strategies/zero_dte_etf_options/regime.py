# SPDX-License-Identifier: MIT
"""The 0DTE strategies' underlying regime (``RegimeMixin._regime_confirm``).

The regime classifies the underlying as bullish_trend, bearish_trend or
range, or refuses it. Its stages run in this order:

1. read the underlying's tape and the confirmation index;
2. read VIX and the live activity score, and apply the hard gates (VIX
   that cannot be read, VIX, IV rank, a VIX spike, a dead tape, a chaotic
   range, the index);
3. build the entry contexts;
4. read the HTF trend confirmation and the FVG scores;
5. score each regime, pick the top one if it clears its floor and the gap
   to the runner-up;
6. veto a trend against the HTF trend or the HTF structure bias.

The static tape stats and the HTF trend context sit here too. The mixin moved
out of ``strategy.py`` on 2026-09-27, and ``_regime_confirm`` stopped being a
single 465-line method at the same time; the stages are the method's own
blocks, unchanged.
"""
import logging
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ...bars import session_open_price
from ...htf_levels import summarize_htf_trend
from ...models import Candidate, Side
from ...numeric import first_float, safe_float
from ...reasons import detail_fields, fmt_metric, insufficient_bars_reason, reason_with_values
from ...support_resistance import empty_market_structure_context
from ... import sessions
from ..shared_entry import EntryContexts

LOG = logging.getLogger(__name__)

# The most a VIX quote's percent change may differ from its own net change /
# prior close, in percentage points. Schwab derives both from one quote, so
# they agree to its rounding unless a unit or a field changes.
VIX_CHANGE_TOLERANCE_PP = 0.05


def _ambiguous_regime_reason(
    *,
    top_name: str,
    top_score: Any,
    second_name: str,
    second_score: Any,
    min_top_score: Any,
    min_score_gap: Any,
) -> str:
    """Standard 'top regime score too close to second' skip reason."""
    top, second = safe_float(top_score), safe_float(second_score)
    gap = None if top is None or second is None else top - second
    return (
        "ambiguous_regime("
        f"top={top_name},"
        f"top_score={fmt_metric(top_score, 2)},"
        f"second={second_name},"
        f"second_score={fmt_metric(second_score, 2)},"
        f"required_top_score>={fmt_metric(min_top_score, 2)},"
        f"required_score_gap>={fmt_metric(min_score_gap, 2)},"
        f"current_score_gap={fmt_metric(gap, 2)}"
        ")"
    )


@dataclass(frozen=True)
class _UnderlyingTape:
    """The underlying's last bar and recent tape, as the regime reads them."""

    close: float
    vwap_dist: float
    ema_gap: float
    ret5: float
    ret15: float
    day_ret: float
    above_frac: float
    below_frac: float
    flip_count: int
    range_pct: float


@dataclass(frozen=True)
class _IndexTape:
    """The confirmation index: whether it has ``min_bars`` bars, and then
    its VWAP / EMA reads (all neutral when it has too few)."""

    min_bars: int
    available: bool
    bullish: bool
    bearish: bool
    range: bool
    vwap_dist: float
    ema_gap: float
    flip_count: int


@dataclass(frozen=True)
class _RegimeContexts:
    """The contexts the regime scores on: chart patterns, candles (the
    context and each side's directional signal), the S/R context, its HTF
    market structure (the empty one when it has none) and the LTF
    structure."""

    pattern: Any
    candle: dict[str, Any]
    bull_candle: dict[str, Any]
    bear_candle: dict[str, Any]
    sr: Any
    market_structure: Any
    ms_ltf: Any


@dataclass(frozen=True)
class _VixRead:
    """VIX for the hard gates: its last price and its day change since the
    prior close as a fraction (0.10 is +10%), or the reason the volatility
    quote cannot be read (``refusal``, which refuses the entry; ``last`` and
    ``change`` are then None)."""

    last: float | None
    change: float | None
    refusal: str | None


@dataclass(frozen=True)
class _HtfTrend:
    """The HTF trend confirmation: whether it is on and required, the
    context (``{"available": False, "reason": "disabled"}`` when off) and
    its direction."""

    use: bool
    require: bool
    ctx: dict[str, Any]
    available: bool
    bullish: bool
    bearish: bool
    range: bool


class RegimeMixin:
    """The regime engine of ``ZeroDteEtfOptionsStrategy``. It reads the
    strategy's ``params``, ``optcfg`` and ``config``, its ``entry_policy``
    (the FVG scores), the context builders with the HTF timeframe and
    request (``ContextBuildersMixin``), and one hook the strategy keeps:
    ``live_activity_score`` (also a dashboard hook). The strategy also keeps
    ``_vix_refusal_warned_at``: when each kind of refused VIX read last
    logged its WARNING (``_vix_refused``)."""

    @staticmethod
    def _fraction_relative(frame: pd.DataFrame, column: str, lookback: int, direction: str) -> float:
        if frame is None or frame.empty:
            return 0.0
        recent = frame.tail(max(2, lookback))
        if recent.empty or column not in recent.columns:
            return 0.0
        if direction == "above":
            return float((recent["close"] > recent[column]).mean())
        return float((recent["close"] < recent[column]).mean())

    @staticmethod
    def _flip_count(frame: pd.DataFrame, lookback: int) -> int:
        if frame is None or frame.empty:
            return 0
        recent = frame.tail(max(3, lookback))
        if recent.empty or "vwap" not in recent.columns:
            return 0
        sign = (recent["close"] - recent["vwap"]).apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0)).tolist()
        sign = [s for s in sign if s != 0]
        if len(sign) < 2:
            return 0
        return sum(1 for a, b in zip(sign, sign[1:]) if a != b)

    @staticmethod
    def _recent_range_pct(frame: pd.DataFrame, lookback: int) -> float:
        if frame is None or frame.empty:
            return 0.0
        recent = frame.tail(max(2, lookback))
        if recent.empty:
            return 0.0
        ref = safe_float(recent.iloc[-1]["close"], 0.0)
        if ref <= 0:
            return 0.0
        return max(0.0, float(recent["high"].max()) - float(recent["low"].min())) / ref

    def _htf_trend_context(self, symbol: str, data) -> dict[str, Any]:
        p = self.params
        if data is None or not hasattr(data, "get_htf_frame"):
            return {"available": False, "reason": "no_data_feed"}
        htf_tf = self.htf_minutes()
        frame = data.get_htf_frame(symbol, timeframe_minutes=htf_tf)
        min_bars = int(p.get("htf_min_bars", 20))
        summary = summarize_htf_trend(
            frame,
            min_bars=min_bars,
            vwap_distance_pct=float(p.get("htf_vwap_distance_pct", 0.0009)),
            ema_gap_pct=float(p.get("htf_ema_gap_pct", 0.0007)),
            min_ret3=float(p.get("htf_min_ret3", 0.0009)),
            range_vwap_distance_pct=float(p.get("htf_range_vwap_distance_pct", 0.0020)),
            range_ema_gap_pct=float(p.get("htf_range_ema_gap_pct", 0.0010)),
        )
        if not bool(summary.get("available")):
            bars = 0 if frame is None else len(frame)
            summary["reason"] = insufficient_bars_reason("insufficient_htf_bars", bars, min_bars)
        summary["timeframe_minutes"] = htf_tf
        return summary

    def _regime_confirm(self, candidate: Candidate, bars: dict[str, pd.DataFrame], data) -> dict[str, Any]:
        p = self.params
        underlying = candidate.symbol
        confirm_symbol = self.optcfg.confirmation_symbols.get(underlying)
        u = bars.get(underlying)
        idx = bars.get(confirm_symbol) if confirm_symbol else None
        min_bars = int(p.get("min_bars", 35))
        if u is None or len(u) < min_bars:
            return {
                "ok": False,
                "no_trade": True,
                "reason": insufficient_bars_reason("insufficient_underlying_bars", 0 if u is None else len(u), min_bars),
                "underlying": underlying,
                "confirm_index": confirm_symbol,
            }

        tape = self._underlying_tape(u)
        index = self._confirm_index_tape(idx)
        vix = self._vix_read(data)
        # Live activity score (2026-05-14) — replaces TV cumulative RVOL
        # for 0DTE gating and bonus scoring. Self-normalizing against
        # the symbol's own last 20 bars, so it works the same morning
        # vs afternoon and isn't biased low for benchmark ETFs. The TV
        # candidate_rvol pipeline (raw / effective / profile / required-
        # threshold) was removed in the same cleanup — those values
        # were stub-only after the local-synthesis switch and no longer
        # influenced any gate. Dashboard rings now use this live score.
        activity_score = self.live_activity_score(u)
        reasons = self._regime_gate_reasons(tape, index, idx, confirm_symbol, vix, activity_score)
        contexts = self._regime_contexts(underlying, u, data, tape.close)
        htf = self._htf_trend_confirmation(underlying, data)
        if htf.use and htf.require and not htf.available:
            reasons.append(str(htf.ctx.get("reason") or "insufficient_htf_bars"))
        htf_fvg_score, fvg_ltf_score = self._fvg_regime_scores(underlying, u, data, tape.close)
        scores = self._regime_scores(tape, index, activity_score, vix, contexts, htf, htf_fvg_score, fvg_ltf_score)
        regime, no_trade = self._select_regime(scores, reasons)
        no_trade = self._regime_vetoes(regime, no_trade, htf, contexts.market_structure, reasons)

        return {
            "ok": True,
            "underlying": underlying,
            "confirm_index": confirm_symbol,
            "regime": regime,
            "no_trade": no_trade or regime == "no_trade",
            "reason": ",".join(reasons) if reasons else regime,
            # The contexts the regime was scored on, for the premium
            # proposals (the S/R context of this frame, its LTF structure and
            # chart contexts): admit gates on the same reads.
            "entry_contexts": EntryContexts(sr=contexts.sr, ms=contexts.ms_ltf, chart=contexts.pattern),
            "scores": scores,
            "metrics": self._regime_metrics(tape, index, htf, activity_score, vix, contexts,
                                            htf_fvg_score, fvg_ltf_score),
        }

    def _underlying_tape(self, u: pd.DataFrame) -> _UnderlyingTape:
        p = self.params
        last_u = u.iloc[-1]
        u_close = safe_float(last_u["close"], 0.0)
        u_vwap = safe_float(last_u["vwap"], u_close)
        u_ema9 = safe_float(last_u["ema9"], u_close)
        u_ema20 = safe_float(last_u["ema20"], u_close)
        u_vwap_dist = (u_close - u_vwap) / max(u_close, 1.0)
        u_ema_gap = (u_ema9 - u_ema20) / max(u_close, 1.0)
        u_ret5 = safe_float(last_u["ret5"], 0.0)
        u_ret15 = safe_float(last_u["ret15"], 0.0)
        session_day = sessions.now_et().date()
        u_open = session_open_price(u, session_day, fallback_to_premarket_on_nan=True)
        u_day_ret = float((u_close / u_open) - 1.0) if u_open else 0.0
        u_above_frac = self._fraction_relative(u, "vwap", int(p.get("trend_vwap_lookback", 8)), "above")
        u_below_frac = self._fraction_relative(u, "vwap", int(p.get("trend_vwap_lookback", 8)), "below")
        u_flip_count = self._flip_count(u, int(p.get("flip_lookback", 12)))
        u_range_pct = self._recent_range_pct(u, int(p.get("range_lookback", 20)))
        return _UnderlyingTape(close=u_close, vwap_dist=u_vwap_dist, ema_gap=u_ema_gap, ret5=u_ret5, ret15=u_ret15,
                               day_ret=u_day_ret, above_frac=u_above_frac, below_frac=u_below_frac,
                               flip_count=u_flip_count, range_pct=u_range_pct)

    def _confirm_index_tape(self, idx: pd.DataFrame | None) -> _IndexTape:
        p = self.params
        min_confirm_bars = int(p.get("min_confirm_bars", 20))
        idx_available = bool(idx is not None and len(idx) >= min_confirm_bars)
        idx_bullish = idx_bearish = idx_range = False
        idx_vwap_dist = 0.0
        idx_ema_gap = 0.0
        idx_flip_count = 0
        if idx_available:
            last_i = idx.iloc[-1]
            i_close = safe_float(last_i["close"], 0.0)
            i_vwap = safe_float(last_i["vwap"], i_close)
            i_ema9 = safe_float(last_i["ema9"], i_close)
            i_ema20 = safe_float(last_i["ema20"], i_close)
            idx_vwap_dist = (i_close - i_vwap) / max(i_close, 1.0)
            idx_ema_gap = (i_ema9 - i_ema20) / max(i_close, 1.0)
            idx_flip_count = self._flip_count(idx, int(p.get("flip_lookback", 12)))
            idx_bullish = idx_vwap_dist >= float(p.get("trend_vwap_distance_pct", 0.0016)) and idx_ema_gap >= float(p.get("trend_ema_gap_pct", 0.00075))
            idx_bearish = idx_vwap_dist <= -float(p.get("trend_vwap_distance_pct", 0.0016)) and idx_ema_gap <= -float(p.get("trend_ema_gap_pct", 0.00075))
            idx_range = abs(idx_vwap_dist) <= float(p.get("range_vwap_distance_pct", 0.0019)) and abs(idx_ema_gap) <= float(p.get("range_ema_gap_pct", 0.00075))
        return _IndexTape(min_bars=min_confirm_bars, available=idx_available, bullish=idx_bullish, bearish=idx_bearish,
                          range=idx_range, vwap_dist=idx_vwap_dist, ema_gap=idx_ema_gap, flip_count=idx_flip_count)

    def _vix_read(self, data) -> _VixRead:
        """VIX's last price and its day change since the prior close as a
        fraction (0.10 is +10%), from the cached volatility quote. It fails
        closed (2026-10-06): the read is refused, and with it the entry, when
        - no quote is cached, it is older than
          ``options.max_vix_quote_age_seconds``, it carries no fetch time, or
          it has no price: ``vix_unavailable``;
        - it lacks its percent change, its net change or its prior close:
          ``vix_change_unavailable``;
        - its percent change (Schwab's ``netPercentChange``, in percent)
          differs from 100 x net change / prior close by more than
          ``VIX_CHANGE_TOLERANCE_PP`` percentage points, a unit or a field
          that changed: ``vix_change_mismatch``.
        Until 2026-10-06 a missing quote, or one older than
        ``runtime.quote_cache_seconds`` (the interval it is refreshed at, so
        the read often came just after it expired: 39% of 2026-05-22's
        regime checks), passed every VIX gate silently; the percent change
        was never filled, so the change gates read 0; and its unit was
        guessed by size (a +0.5% day would have read +50%)."""
        vol_symbol = self.optcfg.volatility_symbol
        limit = float(self.optcfg.max_vix_quote_age_seconds)
        q = data.get_quote(vol_symbol) if data is not None else None
        if q is None:
            return self._vix_refused("vix_unavailable", f"vix_unavailable({detail_fields(quote='none')})")
        fetched_at = q.get("fetched_at")
        if fetched_at is None:
            return self._vix_refused("vix_unavailable", f"vix_unavailable({detail_fields(fetched_at='none')})")
        age = (sessions.now_et() - fetched_at).total_seconds()
        if age > limit:
            return self._vix_refused(
                "vix_unavailable", reason_with_values("vix_unavailable", current=age, required=limit, op="<=", digits=1))
        vix_last = first_float(q, "last", "mid", "mark", positive=True)
        if vix_last is None:
            return self._vix_refused("vix_unavailable", f"vix_unavailable({detail_fields(price='none')})")
        percent_change = first_float(q, "percent_change", finite=True)
        net_change = first_float(q, "net_change", finite=True)
        prior_close = first_float(q, "close", positive=True)
        missing = [name for name, value in (("percent_change", percent_change), ("net_change", net_change),
                                            ("close", prior_close)) if value is None]
        if missing:
            return self._vix_refused("vix_change_unavailable",
                                     f"vix_change_unavailable({detail_fields(missing='+'.join(missing))})")
        net_over_close = 100.0 * net_change / prior_close
        if abs(percent_change - net_over_close) > VIX_CHANGE_TOLERANCE_PP:
            detail = detail_fields(percent_change=percent_change, net_over_close=net_over_close,
                                   limit_pp=VIX_CHANGE_TOLERANCE_PP)
            return self._vix_refused("vix_change_mismatch", f"vix_change_mismatch({detail})")
        return _VixRead(last=vix_last, change=percent_change / 100.0, refusal=None)

    def _vix_refused(self, kind: str, reason: str) -> _VixRead:
        """A refused VIX read: ``reason`` refuses the entry. Logged at
        WARNING at most once a minute per ``kind``, at DEBUG in between."""
        now_ts = time.monotonic()
        last = self._vix_refusal_warned_at.get(kind)
        level = logging.DEBUG
        if last is None or now_ts - last >= 60.0:
            self._vix_refusal_warned_at[kind] = now_ts
            level = logging.WARNING
        LOG.log(level, "0DTE VIX read refused: %s (volatility_symbol=%s); every 0DTE entry is refused while it lasts",
                reason, self.optcfg.volatility_symbol)
        return _VixRead(last=None, change=None, refusal=reason)

    def _vix_gate_reasons(self, vix_last: float, vix_change: float) -> list[str]:
        """The gates on a VIX read that was not refused: VIX above
        ``max_vix`` or below ``min_vix``, its IV rank outside the band, and
        a day change of ``vix_spike_pct`` or more either way."""
        max_vix = float(self.optcfg.max_vix)
        # Lower-bound VIX floor. 0.0 (default) disables the gate for
        # backward-compat. Long-premium strategies should set this to
        # ~12.0 — below that level the typical daily range is too small
        # to overcome 0DTE theta + commissions even on a correct
        # directional call. Credit-spread strategies leave it at 0.0
        # since low-VIX is their target environment.
        min_vix = float(getattr(self.optcfg, "min_vix", 0.0) or 0.0)
        vix_spike_pct = float(self.optcfg.vix_spike_pct)
        reasons: list[str] = []
        if vix_last > max_vix:
            reasons.append(reason_with_values("vix_above_limit", current=vix_last, required=max_vix, op="<=", digits=2))
        if min_vix > 0.0 and vix_last < min_vix:
            reasons.append(reason_with_values("vix_below_floor", current=vix_last, required=min_vix, op=">=", digits=2))
        # IV-rank gate (2026-05-14). Normalize current VIX against the
        # user-provided 52-week range. Long-premium strategies should
        # cap max_iv_rank to avoid buying expensive premium; credit-
        # spread strategies should floor min_iv_rank to ensure juicy
        # credits. Defaults (min=0.0, max=1.0) disable the gate.
        vix_52w_low = float(getattr(self.optcfg, "vix_52w_low", 12.0))
        vix_52w_high = float(getattr(self.optcfg, "vix_52w_high", 30.0))
        min_iv_rank = float(getattr(self.optcfg, "min_iv_rank", 0.0) or 0.0)
        max_iv_rank = float(getattr(self.optcfg, "max_iv_rank", 1.0) or 1.0)
        iv_range = max(0.01, vix_52w_high - vix_52w_low)
        iv_rank = max(0.0, min(1.0, (vix_last - vix_52w_low) / iv_range))
        if min_iv_rank > 0.0 and iv_rank < min_iv_rank:
            reasons.append(reason_with_values("iv_rank_too_low", current=iv_rank, required=min_iv_rank, op=">=", digits=2))
        if max_iv_rank < 1.0 and iv_rank > max_iv_rank:
            reasons.append(reason_with_values("iv_rank_too_high", current=iv_rank, required=max_iv_rank, op="<=", digits=2))
        if abs(vix_change) >= vix_spike_pct:
            reasons.append(reason_with_values("vix_spike", current=abs(vix_change), required=vix_spike_pct, op="<", digits=4))
        return reasons

    def _regime_gate_reasons(self, tape: _UnderlyingTape, index: _IndexTape, idx: pd.DataFrame | None,
                             confirm_symbol: str | None, vix: _VixRead, activity_score: float) -> list[str]:
        """The hard gates' refusals: a VIX read that was refused (the other
        VIX gates are then not asked), VIX, IV rank, a VIX spike, a dead
        tape, a chaotic range, the confirmation index."""
        p = self.params
        u_vwap_dist, u_flip_count, u_range_pct = tape.vwap_dist, tape.flip_count, tape.range_pct
        idx_available, idx_vwap_dist, min_confirm_bars = index.available, index.vwap_dist, index.min_bars
        chaos_intraday_range_pct = float(p.get("chaos_intraday_range_pct", 0.016))
        chop_flip_min = int(p.get("chop_flip_min", 4))
        trend_vwap_distance_pct = float(p.get("trend_vwap_distance_pct", 0.0016))

        reasons = [vix.refusal] if vix.refusal is not None else self._vix_gate_reasons(vix.last, vix.change)
        # Live activity gate (replaces legacy weak_relative_volume gate that
        # used TV cumulative RVOL — see live_activity_score docstring for
        # why that was unreachable for benchmark ETFs).
        min_activity = float(p.get("min_activity_for_entry", 0.0))
        if min_activity > 0.0 and activity_score < min_activity:
            reasons.append(reason_with_values("dead_tape", current=activity_score, required=min_activity, op=">=", digits=2))
        if u_range_pct >= chaos_intraday_range_pct and u_flip_count >= chop_flip_min:
            reasons.append(
                reason_with_values(
                    "chaotic_intraday_range",
                    current=u_range_pct,
                    required=chaos_intraday_range_pct,
                    op="<",
                    digits=4,
                    extras={"flips": (u_flip_count, "<", chop_flip_min)},
                )
            )
        require_index_confirmation = bool(p.get("require_index_confirmation", True))
        if require_index_confirmation and confirm_symbol and not idx_available:
            reasons.append(
                insufficient_bars_reason(
                    "insufficient_confirm_bars",
                    0 if idx is None else len(idx),
                    min_confirm_bars,
                )
            )
        if require_index_confirmation and idx_available:
            trend_disagree = (u_vwap_dist > 0 > idx_vwap_dist) or (u_vwap_dist < 0 < idx_vwap_dist)
            if trend_disagree and abs(u_vwap_dist) >= trend_vwap_distance_pct and abs(idx_vwap_dist) >= trend_vwap_distance_pct:
                reasons.append(
                    reason_with_values(
                        "underlying_index_disagreement",
                        current=abs(u_vwap_dist),
                        required=trend_vwap_distance_pct,
                        op="<",
                        digits=4,
                        extras={"index_vwap_dist": (abs(idx_vwap_dist), "<", trend_vwap_distance_pct)},
                    )
                )
        return reasons

    def _regime_contexts(self, underlying: str, u: pd.DataFrame, data, u_close: float) -> _RegimeContexts:
        pattern_ctx = self._chart_context(u)
        candle_ctx = self._candle_context(u)
        bull_candle_signal = self._directional_candle_signal(u, Side.LONG)
        bear_candle_signal = self._directional_candle_signal(u, Side.SHORT)
        sr_ctx = self._sr_context(underlying, u, data)
        mshtf_ctx = getattr(sr_ctx, "market_structure", None) or empty_market_structure_context(u_close)
        ms_ltf_ctx = self._structure_context(u, "ltf")
        return _RegimeContexts(pattern=pattern_ctx, candle=candle_ctx, bull_candle=bull_candle_signal,
                               bear_candle=bear_candle_signal, sr=sr_ctx, market_structure=mshtf_ctx, ms_ltf=ms_ltf_ctx)

    def _htf_trend_confirmation(self, underlying: str, data) -> _HtfTrend:
        p = self.params
        use_htf_confirmation = bool(p.get("use_htf_trend_confirmation", False))
        require_htf_alignment = bool(p.get("require_htf_alignment", use_htf_confirmation))
        htf_ctx = self._htf_trend_context(underlying, data) if use_htf_confirmation else {"available": False, "reason": "disabled"}
        htf_available = bool(htf_ctx.get("available"))
        htf_bullish = bool(htf_ctx.get("bullish")) if htf_available else False
        htf_bearish = bool(htf_ctx.get("bearish")) if htf_available else False
        htf_range = bool(htf_ctx.get("range")) if htf_available else False
        return _HtfTrend(use=use_htf_confirmation, require=require_htf_alignment, ctx=htf_ctx, available=htf_available,
                         bullish=htf_bullish, bearish=htf_bearish, range=htf_range)

    def _fvg_regime_scores(self, underlying: str, u: pd.DataFrame, data, u_close: float) -> tuple[dict[str, Any], dict[str, Any]]:
        htf_fvg_ctx = self._htf_context(underlying, data, current_price=u_close, **self._default_htf_request())
        fvg_ltf_ctx = self._ltf_fvg_context(underlying, u, data)
        # shared_entry.use_fvg_context is the entry policy's to read
        # (2026-09-24): with it off both scores come back as the zero shape,
        # nearest_bullish / nearest_bearish included, which the metrics
        # stamping below reads unconditionally.
        return self.entry_policy.fvg_regime_scores(u_close, htf_fvg_ctx, fvg_ltf_ctx)

    def _regime_scores(self, tape: _UnderlyingTape, index: _IndexTape, activity_score: float, vix: _VixRead,
                       contexts: _RegimeContexts, htf: _HtfTrend, htf_fvg_score: dict[str, Any],
                       fvg_ltf_score: dict[str, Any]) -> dict[str, float]:
        """The bullish_trend, bearish_trend and range scores."""
        p = self.params
        sr_cfg = getattr(self.config, "support_resistance", None)
        u_vwap_dist, u_ema_gap, u_ret5, u_ret15 = tape.vwap_dist, tape.ema_gap, tape.ret5, tape.ret15
        u_day_ret, u_above_frac, u_below_frac = tape.day_ret, tape.above_frac, tape.below_frac
        u_flip_count, u_range_pct = tape.flip_count, tape.range_pct
        # change_from_open is computed live from Schwab session bars
        # (u_day_ret above, via session_open_price with RTH-first +
        # extended-hours fallback). The 2026-05-19 local-synthesis
        # screener no longer stamps change_from_open on candidate.
        # metadata at all — bypass was cleaner than carrying a 0.0
        # stub through downstream consumers.
        candidate_day_move = u_day_ret
        idx_available, idx_bullish, idx_bearish, idx_range = index.available, index.bullish, index.bearish, index.range
        require_index_confirmation = bool(p.get("require_index_confirmation", True))
        pattern_ctx, sr_ctx, ms_ltf_ctx = contexts.pattern, contexts.sr, contexts.ms_ltf
        mshtf_ctx = contexts.market_structure
        bull_candle_signal, bear_candle_signal = contexts.bull_candle, contexts.bear_candle
        htf_bullish, htf_bearish, htf_range = htf.bullish, htf.bearish, htf.range
        fvg_context_weight_scale = max(0.0, float(p.get("fvg_context_weight_scale", 0.9) or 0.0))
        sr_weight = float(getattr(sr_cfg, "regime_weight", 0.75) or 0.75)
        mshtf_weight = float(getattr(sr_cfg, "structure_htf_weight", 0.90) or 0.90)
        ms_ltf_weight = float(getattr(sr_cfg, "structure_ltf_weight", 0.70) or 0.70)
        bullish_candle_net_score = float(bull_candle_signal.get("net_score", 0.0) or 0.0)
        bearish_candle_net_score = float(bear_candle_signal.get("net_score", 0.0) or 0.0)
        candle_weight = float(p.get("candle_weight", 0.50))
        candle_sr_weight = float(p.get("candle_sr_weight", 0.35))
        candle_trend_follow_weight = float(p.get("candle_trend_follow_weight", 0.25))
        candle_range_penalty = float(p.get("candle_range_penalty", 0.30))
        candle_mixed_penalty = float(p.get("candle_mixed_penalty", 0.18))
        candle_anchor = max(
            float(p.get("range_vwap_distance_pct", 0.0019)),
            float(p.get("trend_vwap_distance_pct", 0.0016)),
        )
        bullish_candle_confirm = bool(bull_candle_signal.get("confirmed") and bullish_candle_net_score > bearish_candle_net_score)
        bearish_candle_confirm = bool(bear_candle_signal.get("confirmed") and bearish_candle_net_score > bullish_candle_net_score)
        mixed_candles = bool(bull_candle_signal.get("mixed"))
        bullish_candle_scale = min(1.0, bullish_candle_net_score / 1.0) if bullish_candle_confirm else 0.0
        bearish_candle_scale = min(1.0, bearish_candle_net_score / 1.0) if bearish_candle_confirm else 0.0
        htf_score_bonus = float(p.get("htf_score_bonus", 0.65))
        htf_score_penalty = float(p.get("htf_score_penalty", 0.65))

        bull_score = 0.0
        bear_score = 0.0
        range_score = 0.0
        bull_score += 1.5 if u_vwap_dist >= float(p.get("trend_vwap_distance_pct", 0.0016)) else 0.0
        bull_score += 1.0 if u_ema_gap >= float(p.get("trend_ema_gap_pct", 0.00075)) else 0.0
        bull_score += 1.0 if u_ret5 >= float(p.get("trend_min_ret5", 0.0008)) else 0.0
        bull_score += 1.0 if u_ret15 >= float(p.get("trend_min_ret15", 0.0014)) else 0.0
        bull_score += 1.0 if u_above_frac >= float(p.get("trend_above_vwap_frac", 0.75)) else 0.0
        # Trend activity bonus — replaces legacy trend_rvol check that was
        # structurally unreachable for SPY/QQQ (cumulative TV-RVOL).
        bull_score += 0.75 if activity_score >= float(p.get("trend_activity_threshold", 1.15)) else 0.0
        bull_score += 1.0 if idx_bullish else (-0.5 if require_index_confirmation and idx_available else 0.0)
        bull_score += 1.25 if pattern_ctx.matched_bullish_continuation else 0.0
        bull_score += 0.75 if pattern_ctx.matched_bullish_reversal and u_vwap_dist >= 0 else 0.0
        bull_score -= 0.75 if pattern_ctx.matched_bearish_reversal else 0.0
        bull_score -= 1.00 if pattern_ctx.matched_bearish_continuation else 0.0
        bull_score -= 1.0 if u_flip_count > int(p.get("chop_flip_max_for_trend", 3)) else 0.0
        bull_score -= 1.0 if u_range_pct > float(p.get("chaos_intraday_range_pct", 0.016)) else 0.0
        bull_score += sr_weight if sr_ctx.breakout_above_resistance else 0.0
        # near_* is the level on price's own side; the breakdown / breakout
        # flags are about a broken level on the far side, so they no longer
        # switch the near terms off (2026-09-23).
        bull_score += sr_weight * 0.40 if sr_ctx.near_support else 0.0
        bull_score -= sr_weight * 0.45 if sr_ctx.near_resistance else 0.0
        bull_score += candle_weight * bullish_candle_scale if bullish_candle_confirm and u_vwap_dist >= -candle_anchor else 0.0
        bull_score += candle_sr_weight * bullish_candle_scale if bullish_candle_confirm and sr_ctx.near_support else 0.0
        bull_score += candle_trend_follow_weight * bullish_candle_scale if bullish_candle_confirm and pattern_ctx.matched_bullish_continuation else 0.0
        bull_score -= candle_weight * bearish_candle_scale if bearish_candle_confirm else 0.0
        bull_score -= candle_mixed_penalty if mixed_candles else 0.0
        bull_score += htf_score_bonus if htf_bullish else 0.0
        bull_score -= htf_score_penalty if htf_bearish else 0.0

        bear_score += 1.5 if u_vwap_dist <= -float(p.get("trend_vwap_distance_pct", 0.0016)) else 0.0
        bear_score += 1.0 if u_ema_gap <= -float(p.get("trend_ema_gap_pct", 0.00075)) else 0.0
        bear_score += 1.0 if u_ret5 <= -float(p.get("trend_min_ret5", 0.0008)) else 0.0
        bear_score += 1.0 if u_ret15 <= -float(p.get("trend_min_ret15", 0.0014)) else 0.0
        bear_score += 1.0 if u_below_frac >= float(p.get("trend_above_vwap_frac", 0.75)) else 0.0
        # Trend activity bonus (symmetric with bull side).
        bear_score += 0.75 if activity_score >= float(p.get("trend_activity_threshold", 1.15)) else 0.0
        bear_score += 1.0 if idx_bearish else (-0.5 if require_index_confirmation and idx_available else 0.0)
        bear_score += 1.25 if pattern_ctx.matched_bearish_continuation else 0.0
        bear_score += 0.75 if pattern_ctx.matched_bearish_reversal and u_vwap_dist <= 0 else 0.0
        bear_score -= 0.75 if pattern_ctx.matched_bullish_reversal else 0.0
        bear_score -= 1.00 if pattern_ctx.matched_bullish_continuation else 0.0
        bear_score -= 1.0 if u_flip_count > int(p.get("chop_flip_max_for_trend", 3)) else 0.0
        bear_score -= 1.0 if u_range_pct > float(p.get("chaos_intraday_range_pct", 0.016)) else 0.0
        bear_score += sr_weight if sr_ctx.breakdown_below_support else 0.0
        bear_score += sr_weight * 0.40 if sr_ctx.near_resistance else 0.0
        bear_score -= sr_weight * 0.45 if sr_ctx.near_support else 0.0
        bear_score += candle_weight * bearish_candle_scale if bearish_candle_confirm and u_vwap_dist <= candle_anchor else 0.0
        bear_score += candle_sr_weight * bearish_candle_scale if bearish_candle_confirm and sr_ctx.near_resistance else 0.0
        bear_score += candle_trend_follow_weight * bearish_candle_scale if bearish_candle_confirm and pattern_ctx.matched_bearish_continuation else 0.0
        bear_score -= candle_weight * bullish_candle_scale if bullish_candle_confirm else 0.0
        bear_score -= candle_mixed_penalty if mixed_candles else 0.0
        bear_score += htf_score_bonus if htf_bearish else 0.0
        bear_score -= htf_score_penalty if htf_bullish else 0.0

        range_score += 1.5 if abs(u_vwap_dist) <= float(p.get("range_vwap_distance_pct", 0.0019)) else 0.0
        range_score += 1.0 if abs(u_ema_gap) <= float(p.get("range_ema_gap_pct", 0.00075)) else 0.0
        range_score += 1.0 if u_range_pct <= float(p.get("range_max_intraday_move_pct", 0.012)) else 0.0
        range_score += 1.0 if abs(u_day_ret) <= float(p.get("credit_max_day_move_pct", 0.010)) else 0.0
        range_score += 1.0 if u_flip_count >= int(p.get("chop_flip_min", 4)) else 0.0
        range_score += 0.75 if idx_available and idx_range else 0.0
        # Range/credit activity bonus — moderate activity is the credit
        # sweet spot (theta-friendly tape). Floor + ceiling replace the
        # legacy credit_min_rvol / credit_max_rvol thresholds.
        range_score += 0.5 if activity_score >= float(p.get("credit_activity_min", 0.80)) else 0.0
        range_score -= 0.50 if pattern_ctx.matched_bullish_continuation or pattern_ctx.matched_bearish_continuation else 0.0
        range_score -= 0.25 if pattern_ctx.matched_bullish_reversal or pattern_ctx.matched_bearish_reversal else 0.0
        # Too-active tape kills credit setups (likely directional move
        # incoming, not range).
        range_score -= 1.0 if activity_score >= float(p.get("credit_activity_max", 1.30)) else 0.0
        range_score -= 1.0 if abs(candidate_day_move) >= float(p.get("credit_max_day_move_pct", 0.010)) else 0.0
        # Checked at load (ZeroDteEtfOptionsStrategy.normalize_params). A
        # refused VIX read (no change) refuses the entry at the gates and
        # docks nothing here.
        range_score -= 1.0 if vix.change is not None and abs(vix.change) >= p["credit_max_vix_change_pct"] else 0.0
        range_score += sr_weight * 0.30 if sr_ctx.near_support and sr_ctx.near_resistance else 0.0
        range_score += sr_weight * 0.20 if sr_ctx.regime_hint == "range_between_levels" else 0.0
        range_score -= sr_weight * 0.35 if sr_ctx.breakout_above_resistance or sr_ctx.breakdown_below_support else 0.0
        range_score -= candle_range_penalty if bullish_candle_confirm or bearish_candle_confirm else 0.0
        range_score -= candle_mixed_penalty * 0.5 if mixed_candles else 0.0
        range_score += htf_score_bonus * 0.35 if htf_range else 0.0
        range_score -= htf_score_penalty * 0.35 if (htf_bullish or htf_bearish) else 0.0

        bull_score += mshtf_weight * 0.60 if mshtf_ctx.bias == "bullish" else 0.0
        bull_score -= mshtf_weight * 0.60 if mshtf_ctx.bias == "bearish" else 0.0
        bull_score += mshtf_weight * 0.95 if self._active_structure_break(mshtf_ctx.bos_up, mshtf_ctx.bos_up_age_bars, htf=True) else 0.0
        bull_score -= mshtf_weight * 1.05 if self._active_structure_break(mshtf_ctx.choch_down, mshtf_ctx.choch_down_age_bars, htf=True) else 0.0
        bull_score += ms_ltf_weight * 0.70 if ms_ltf_ctx.bias == "bullish" else 0.0
        bull_score -= ms_ltf_weight * 0.75 if ms_ltf_ctx.bias == "bearish" else 0.0
        bull_score += ms_ltf_weight if (ms_ltf_ctx.bos_up and self._structure_event_recent(ms_ltf_ctx.bos_up_age_bars)) else 0.0
        bull_score -= ms_ltf_weight if (ms_ltf_ctx.choch_down and self._structure_event_recent(ms_ltf_ctx.choch_down_age_bars)) else 0.0

        bear_score += mshtf_weight * 0.60 if mshtf_ctx.bias == "bearish" else 0.0
        bear_score -= mshtf_weight * 0.60 if mshtf_ctx.bias == "bullish" else 0.0
        bear_score += mshtf_weight * 0.95 if self._active_structure_break(mshtf_ctx.bos_down, mshtf_ctx.bos_down_age_bars, htf=True) else 0.0
        bear_score -= mshtf_weight * 1.05 if self._active_structure_break(mshtf_ctx.choch_up, mshtf_ctx.choch_up_age_bars, htf=True) else 0.0
        bear_score += ms_ltf_weight * 0.70 if ms_ltf_ctx.bias == "bearish" else 0.0
        bear_score -= ms_ltf_weight * 0.75 if ms_ltf_ctx.bias == "bullish" else 0.0
        bear_score += ms_ltf_weight if (ms_ltf_ctx.bos_down and self._structure_event_recent(ms_ltf_ctx.bos_down_age_bars)) else 0.0
        bear_score -= ms_ltf_weight if (ms_ltf_ctx.choch_up and self._structure_event_recent(ms_ltf_ctx.choch_up_age_bars)) else 0.0

        bull_score += (htf_fvg_score["bull_score"] + fvg_ltf_score["bull_score"]) * fvg_context_weight_scale
        bear_score += (htf_fvg_score["bear_score"] + fvg_ltf_score["bear_score"]) * fvg_context_weight_scale

        range_score += mshtf_weight * 0.35 if mshtf_ctx.bias == "neutral" else 0.0
        range_score -= mshtf_weight * 0.35 if mshtf_ctx.bias in {"bullish", "bearish"} else 0.0
        range_score += ms_ltf_weight * 0.20 if ms_ltf_ctx.bias == "neutral" else 0.0
        range_score -= min(0.45, ((htf_fvg_score["directional_pressure"] * 0.35) + (fvg_ltf_score["directional_pressure"] * 0.25)) * fvg_context_weight_scale)

        return {"bullish_trend": bull_score, "bearish_trend": bear_score, "range": range_score}

    def _select_regime(self, scores: dict[str, float], reasons: list[str]) -> tuple[str, bool]:
        """The top regime when no gate refused and it clears its floor and
        the gap to the runner-up (else ``no_trade``, the ambiguous reason
        appended to ``reasons``); and whether the candidate is refused."""
        p = self.params
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_name, top_score = ranked[0]
        second_name, second_score = ranked[1] if len(ranked) > 1 else ("none", 0.0)
        min_trend_score = float(p.get("min_trend_score", 4.9))
        min_range_score = float(p.get("min_range_score", 4.6))
        min_score_gap = float(p.get("min_score_gap", 1.6))
        regime = "no_trade"
        no_trade = bool(reasons)

        if not no_trade:
            if top_name in {"bullish_trend", "bearish_trend"} and top_score >= min_trend_score and (top_score - second_score) >= min_score_gap:
                regime = top_name
            elif top_name == "range" and top_score >= min_range_score and (top_score - second_score) >= min_score_gap:
                regime = "range"
            else:
                no_trade = True
                reasons.append(
                    _ambiguous_regime_reason(
                        top_name=top_name,
                        top_score=top_score,
                        second_name=second_name,
                        second_score=second_score,
                        min_top_score=min_range_score if top_name == "range" else min_trend_score,
                        min_score_gap=min_score_gap,
                    )
                )
        return regime, no_trade

    def _regime_vetoes(self, regime: str, no_trade: bool, htf: _HtfTrend, mshtf_ctx: Any, reasons: list[str]) -> bool:
        """A trend regime against the HTF trend (when it is required) or the
        HTF structure bias: its refusal appended to ``reasons``. Returns
        whether the candidate is refused."""
        p = self.params
        sr_cfg = getattr(self.config, "support_resistance", None)
        htf_ctx, htf_available, htf_bullish, htf_bearish = htf.ctx, htf.available, htf.bullish, htf.bearish
        if not no_trade and htf.use and htf.require and htf_available:
            if regime == "bullish_trend" and not htf_bullish:
                no_trade = True
                reasons.append(
                    reason_with_values(
                        "htf_trend_misaligned",
                        current=htf_ctx.get("vwap_dist", 0.0),
                        required=float(p.get("htf_vwap_distance_pct", 0.0009)),
                        op=">=",
                        digits=4,
                        extras={
                            "htf_direction": ("bullish" if htf_bullish else ("bearish" if htf_bearish else "range"), "=", "bullish"),
                            "htf_ret3": (float(htf_ctx.get("ret3", 0.0)), ">=", float(p.get("htf_min_ret3", 0.0009))),
                        },
                    )
                )
            elif regime == "bearish_trend" and not htf_bearish:
                no_trade = True
                reasons.append(
                    reason_with_values(
                        "htf_trend_misaligned",
                        current=abs(float(htf_ctx.get("vwap_dist", 0.0))),
                        required=float(p.get("htf_vwap_distance_pct", 0.0009)),
                        op=">=",
                        digits=4,
                        extras={
                            "htf_direction": ("bearish" if htf_bearish else ("bullish" if htf_bullish else "range"), "=", "bearish"),
                            "htf_ret3": (abs(float(htf_ctx.get("ret3", 0.0))), ">=", float(p.get("htf_min_ret3", 0.0009))),
                        },
                    )
                )

        # The HTF structure bias is the regime's own veto. The LTF structure
        # veto that followed it here left on 2026-09-24: each style's premium
        # proposal meets it in the shared entry stage (shared_entry.
        # use_structure_filter), on this same frame and in the regime's
        # direction; the manifest exempts midday_credit_spread, as the range
        # regime was never structure-gated.
        if not no_trade and sr_cfg is not None and bool(getattr(sr_cfg, "structure_enabled", True)):
            if regime == "bullish_trend" and mshtf_ctx.bias == "bearish":
                no_trade = True
                reasons.append(f"htf_structure_bearish(tf={self.htf_minutes()}m,last_high={mshtf_ctx.last_high_label},last_low={mshtf_ctx.last_low_label})")
            elif regime == "bearish_trend" and mshtf_ctx.bias == "bullish":
                no_trade = True
                reasons.append(f"htf_structure_bullish(tf={self.htf_minutes()}m,last_high={mshtf_ctx.last_high_label},last_low={mshtf_ctx.last_low_label})")
        return no_trade

    def _regime_metrics(self, tape: _UnderlyingTape, index: _IndexTape, htf: _HtfTrend, activity_score: float,
                        vix: _VixRead, contexts: _RegimeContexts,
                        htf_fvg_score: dict[str, Any], fvg_ltf_score: dict[str, Any]) -> dict[str, Any]:
        """What the regime read, as the signal stamps it (``regime_metrics``)
        and the credit pivot-buffer gate reads it."""
        u_vwap_dist, u_ema_gap, u_ret5, u_ret15 = tape.vwap_dist, tape.ema_gap, tape.ret5, tape.ret15
        u_day_ret, u_range_pct, u_flip_count = tape.day_ret, tape.range_pct, tape.flip_count
        candidate_day_move = u_day_ret
        htf_ctx, htf_available = htf.ctx, htf.available
        htf_bullish, htf_bearish, htf_range = htf.bullish, htf.bearish, htf.range
        idx_vwap_dist, idx_ema_gap, idx_flip_count = index.vwap_dist, index.ema_gap, index.flip_count
        pattern_ctx, candle_ctx, sr_ctx = contexts.pattern, contexts.candle, contexts.sr
        mshtf_ctx, ms_ltf_ctx = contexts.market_structure, contexts.ms_ltf
        bullish_candle_score = float(contexts.bull_candle.get("score", 0.0) or 0.0)
        bearish_candle_score = float(contexts.bear_candle.get("score", 0.0) or 0.0)
        bullish_candle_net_score = float(contexts.bull_candle.get("net_score", 0.0) or 0.0)
        bearish_candle_net_score = float(contexts.bear_candle.get("net_score", 0.0) or 0.0)
        return {
            "underlying_vwap_dist": u_vwap_dist,
            "underlying_ema_gap": u_ema_gap,
            "underlying_ret5": u_ret5,
            "underlying_ret15": u_ret15,
            "underlying_day_ret": u_day_ret,
            "underlying_range_pct": u_range_pct,
            "underlying_flip_count": u_flip_count,
            "htf_available": htf_available,
            "htf_vwap_dist": float(htf_ctx.get("vwap_dist", 0.0)) if htf_available else 0.0,
            "htf_ema_gap": float(htf_ctx.get("ema_gap", 0.0)) if htf_available else 0.0,
            "htf_ret3": float(htf_ctx.get("ret3", 0.0)) if htf_available else 0.0,
            "htf_bullish": htf_bullish,
            "htf_bearish": htf_bearish,
            "htf_range": htf_range,
            "confirm_vwap_dist": idx_vwap_dist,
            "confirm_ema_gap": idx_ema_gap,
            "confirm_flip_count": idx_flip_count,
            "live_activity_score": activity_score,
            "candidate_change_from_open": candidate_day_move,
            # None when the read was refused (the entry then is too).
            "vix": vix.last,
            "vix_pct": vix.change,
            "chart_pattern_bias_score": float(pattern_ctx.bias_score),
            "chart_pattern_regime_hint": str(pattern_ctx.regime_hint),
            "candle_bias_score": float(candle_ctx["candle_bias_score"]),
            "candle_net_score": float(candle_ctx.get("candle_net_score", candle_ctx["candle_bias_score"]) or candle_ctx["candle_bias_score"]),
            "candle_regime_hint": str(candle_ctx["candle_regime_hint"]),
            "matched_bullish_candles": list(candle_ctx["matched_bullish_candles"]),
            "matched_bearish_candles": list(candle_ctx["matched_bearish_candles"]),
            "bullish_candle_score": round(bullish_candle_score, 4),
            "bearish_candle_score": round(bearish_candle_score, 4),
            "bullish_candle_net_score": round(bullish_candle_net_score, 4),
            "bearish_candle_net_score": round(bearish_candle_net_score, 4),
            **self._structure_lists(ms_ltf_ctx, prefix="msltf"),
            **self._structure_lists(mshtf_ctx, prefix="mshtf"),
            "sr_bias_score": float(sr_ctx.bias_score),
            "sr_regime_hint": str(sr_ctx.regime_hint),
            "sr_nearest_support": float(sr_ctx.nearest_support.price) if sr_ctx.nearest_support else None,
            "sr_nearest_resistance": float(sr_ctx.nearest_resistance.price) if sr_ctx.nearest_resistance else None,
            "sr_support_distance_pct": None if sr_ctx.support_distance_pct is None else float(sr_ctx.support_distance_pct),
            "sr_resistance_distance_pct": None if sr_ctx.resistance_distance_pct is None else float(sr_ctx.resistance_distance_pct),
            "sr_breakout_above_resistance": bool(sr_ctx.breakout_above_resistance),
            "sr_breakdown_below_support": bool(sr_ctx.breakdown_below_support),
            "sr_supports": [float(round(lv.price, 4)) for lv in sr_ctx.supports],
            "sr_resistances": [float(round(lv.price, 4)) for lv in sr_ctx.resistances],
            "matched_bullish_chart_patterns": sorted(pattern_ctx.matched_bullish),
            "matched_bearish_chart_patterns": sorted(pattern_ctx.matched_bearish),
            "matched_bullish_chart_reversal_patterns": sorted(pattern_ctx.matched_bullish_reversal),
            "matched_bullish_chart_continuation_patterns": sorted(pattern_ctx.matched_bullish_continuation),
            "matched_bearish_chart_reversal_patterns": sorted(pattern_ctx.matched_bearish_reversal),
            "matched_bearish_chart_continuation_patterns": sorted(pattern_ctx.matched_bearish_continuation),
            "htf_fvg_bull_score": float(htf_fvg_score["bull_score"]),
            "htf_fvg_bear_score": float(htf_fvg_score["bear_score"]),
            "htf_fvg_nearest_bullish_state": str(htf_fvg_score["nearest_bullish"].get("state", "none")),
            "htf_fvg_nearest_bearish_state": str(htf_fvg_score["nearest_bearish"].get("state", "none")),
            "htf_fvg_nearest_bullish_midpoint": safe_float(htf_fvg_score["nearest_bullish"].get("midpoint")),
            "htf_fvg_nearest_bearish_midpoint": safe_float(htf_fvg_score["nearest_bearish"].get("midpoint")),
            "fvg_ltf_bull_score": float(fvg_ltf_score["bull_score"]),
            "fvg_ltf_bear_score": float(fvg_ltf_score["bear_score"]),
            "fvg_ltf_nearest_bullish_state": str(fvg_ltf_score["nearest_bullish"].get("state", "none")),
            "fvg_ltf_nearest_bearish_state": str(fvg_ltf_score["nearest_bearish"].get("state", "none")),
            "fvg_ltf_nearest_bullish_midpoint": safe_float(fvg_ltf_score["nearest_bullish"].get("midpoint")),
            "fvg_ltf_nearest_bearish_midpoint": safe_float(fvg_ltf_score["nearest_bearish"].get("midpoint")),
        }
