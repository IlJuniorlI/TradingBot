# SPDX-License-Identifier: MIT
"""Multi-regime adaptive intraday strategy for top-tier liquid stocks.

Detects whether each symbol is trending, pulling back, or ranging, then
applies the appropriate entry style.  Uses SPY/QQQ as index confirmation
for trend and pullback entries.  Trades both long and short across the
full RTH session with time-of-day regime gating.
"""
from __future__ import annotations

from collections import deque

from ..shared import Candidate, Position, Signal, Side, pd
from ..strategy_base import BaseStrategy
from ...utils import now_et, parse_hhmm


class TopTierAdaptiveStrategy(BaseStrategy):
    strategy_name = "top_tier_adaptive"

    def __init__(self, config):
        super().__init__(config)
        # Per-symbol rolling window of recent candidate_directional_bias
        # observations. Trailing-bias memory for Fix A (2026-04-23): when
        # the current bar's bias is None, infer the effective side from
        # recent observations if one side dominates.
        self._recent_directional_bias: dict[str, deque[Side | None]] = {}

    # ------------------------------------------------------------------
    # Watchlist — include index confirmation symbols (SPY/QQQ) so they
    # get history fetching, streaming, and appear in the bars dict.
    # ------------------------------------------------------------------
    def active_watchlist(self, candidates: list[Candidate], positions: dict[str, Position]) -> set[str]:
        symbols = super().active_watchlist(candidates, positions)
        index_symbols = [str(s).upper().strip() for s in (self.params.get("index_symbols") or []) if str(s).strip()]
        symbols.update(index_symbols)
        return symbols

    # ------------------------------------------------------------------
    # Adaptive-ladder rung override
    #
    # Trend and pullback regimes lend themselves to laddering — the trade
    # thesis is "ride momentum through successive resistance levels" so the
    # generic S/R-based rung builder works as-is. Range trades are the
    # opposite: the thesis is mean-reversion bounded by range_low and
    # range_high. Laddering past the range high would chase a breakout
    # that contradicts the entry, so we return [] and let the signal keep
    # its single, range-bounded target.
    # ------------------------------------------------------------------
    def _build_ladder_rungs(self, side, close, stop, atr, sr_ctx, *, regime=None):
        if str(regime or "").strip().lower() == "range":
            return []
        return super()._build_ladder_rungs(side, close, stop, atr, sr_ctx, regime=regime)

    # ------------------------------------------------------------------
    # required_history_bars
    # ------------------------------------------------------------------
    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        return max(0, int(self.params.get("min_bars", 60) or 60))

    # ------------------------------------------------------------------
    # Index confirmation
    # ------------------------------------------------------------------
    def _index_confirms(self, side: Side, bars: dict[str, pd.DataFrame], _data=None) -> bool:
        """Return True if at least one index symbol agrees with *side*."""
        if not bool(self.params.get("require_index_confirmation", True)):
            return True
        index_symbols = [str(s).upper().strip() for s in (self.params.get("index_symbols") or ["SPY", "QQQ"]) if str(s).strip()]
        for sym in index_symbols:
            frame = bars.get(sym)
            if frame is None or frame.empty:
                continue
            last = frame.iloc[-1]
            close = self._safe_float(last["close"])
            vwap = self._safe_float(last.get("vwap"), close)
            ema9 = self._safe_float(last.get("ema9"), close)
            ema20 = self._safe_float(last.get("ema20"), close)
            if side == Side.LONG and close > vwap and ema9 >= ema20:
                return True
            if side == Side.SHORT and close < vwap and ema9 <= ema20:
                return True
        return False

    def _index_neutral(self, bars: dict[str, pd.DataFrame]) -> bool:
        """Return True if indices are not strongly directional (range-friendly)."""
        index_symbols = [str(s).upper().strip() for s in (self.params.get("index_symbols") or ["SPY", "QQQ"]) if str(s).strip()]
        for sym in index_symbols:
            frame = bars.get(sym)
            if frame is None or frame.empty:
                continue
            last = frame.iloc[-1]
            close = self._safe_float(last["close"])
            vwap = self._safe_float(last.get("vwap"), close)
            if vwap > 0 and abs((close - vwap) / vwap) >= 0.0025:
                return False
        return True

    # ------------------------------------------------------------------
    # Regime scoring
    # ------------------------------------------------------------------
    def _score_trend(self, side: Side, close: float, vwap: float, ema9: float, ema20: float,
                     adx: float, ret5: float, ret15: float, index_ok: bool) -> float:
        score = 0.0
        if side == Side.LONG:
            if close > vwap:
                score += 1.0
            if ema9 > ema20:
                score += 1.0
            if close > ema9:
                score += 1.0
            if ret5 > 0:
                score += 0.5
            if ret15 > 0:
                score += 0.5
        else:
            if close < vwap:
                score += 1.0
            if ema9 < ema20:
                score += 1.0
            if close < ema9:
                score += 1.0
            if ret5 < 0:
                score += 0.5
            if ret15 < 0:
                score += 0.5
        if adx >= float(self.params.get("min_adx14", 15.0)):
            score += 1.0
        if index_ok:
            score += 1.0
        return score

    def _score_pullback(self, side: Side, close: float, vwap: float, ema9: float, ema20: float,
                        adx: float, atr: float, trend_score: float, ltf: pd.DataFrame) -> float:
        min_trend = float(self.params.get("min_pullback_trend_score", 3.0))
        if trend_score < min_trend:
            return 0.0
        session_ltf = ltf[self._same_day_mask(ltf, now_et().date())]
        score = 0.0
        touch_mult = float(self.params.get("pullback_ema_touch_atr_mult", 0.35))
        touch_dist = atr * touch_mult
        lookback = max(2, int(self.params.get("pullback_lookback_bars", 5)))
        recent = session_ltf.tail(lookback + 1).iloc[:-1] if len(session_ltf) > lookback else session_ltf.iloc[:-1]
        if recent.empty:
            return 0.0

        if side == Side.LONG:
            recent_low = self._safe_float(recent["low"].min(), close)
            touched_ema20 = recent_low <= ema20 + touch_dist
            touched_vwap = recent_low <= vwap + touch_dist
            if touched_ema20 or touched_vwap:
                score += 1.5
            hold_mult = float(self.params.get("pullback_hold_atr_mult", 0.40))
            if recent_low >= ema20 - (atr * hold_mult):
                score += 1.0
            if close > ema9:
                score += 1.0
            close_pos = self._bar_close_position(session_ltf if not session_ltf.empty else ltf)
            if close_pos >= 0.60:
                score += 0.5
        else:
            recent_high = self._safe_float(recent["high"].max(), close)
            touched_ema20 = recent_high >= ema20 - touch_dist
            touched_vwap = recent_high >= vwap - touch_dist
            if touched_ema20 or touched_vwap:
                score += 1.5
            hold_mult = float(self.params.get("pullback_hold_atr_mult", 0.40))
            if recent_high <= ema20 + (atr * hold_mult):
                score += 1.0
            if close < ema9:
                score += 1.0
            close_pos = self._bar_close_position(session_ltf if not session_ltf.empty else ltf)
            if close_pos <= 0.40:
                score += 0.5

        # Volume expansion on current bar
        vol_src = session_ltf if not session_ltf.empty else ltf
        vol = self._safe_float(vol_src.iloc[-1].get("volume"), 0.0)
        vol_mean = self._safe_float(recent["volume"].mean(), 1.0)
        if vol_mean > 0 and vol / vol_mean >= 1.10:
            score += 0.5
        if adx >= float(self.params.get("min_adx14", 15.0)):
            score += 0.5
        return score

    def _score_range(self, close: float, vwap: float, ema9: float, ema20: float,
                     frame: pd.DataFrame, index_neutral: bool) -> float:
        session_frame = frame[self._same_day_mask(frame, now_et().date())]
        score = 0.0
        max_vwap_dist = float(self.params.get("range_max_vwap_dist_pct", 0.0020))
        max_ema_gap = float(self.params.get("range_max_ema_gap_pct", 0.0008))
        min_flips = int(self.params.get("range_min_flip_count", 3))
        lookback = max(8, int(self.params.get("range_lookback_bars", 20)))

        if vwap > 0 and abs((close - vwap) / vwap) <= max_vwap_dist:
            score += 1.5
        if close > 0 and abs((ema9 - ema20) / close) <= max_ema_gap:
            score += 1.0

        # Count VWAP crosses in the lookback
        recent = session_frame.tail(lookback)
        if "vwap" in recent.columns and len(recent) >= 4:
            closes = recent["close"].astype(float)
            vwaps = recent["vwap"].astype(float)
            above = closes > vwaps
            flips = int((above != above.shift()).sum()) - 1
            if flips >= min_flips:
                score += 1.0

        # Tight intraday range
        if len(recent) >= 8:
            range_pct = (float(recent["high"].max()) - float(recent["low"].min())) / max(close, 1.0)
            if range_pct <= 0.012:
                score += 1.0

        if index_neutral:
            score += 0.5
        return score

    # ------------------------------------------------------------------
    # Time-of-day gating
    # ------------------------------------------------------------------
    @staticmethod
    def _time_in_range(now_t, start: str, end: str) -> bool:
        return parse_hhmm(start) <= now_t <= parse_hhmm(end)

    def _allowed_regimes(self, now_t) -> set[str]:
        """Return which regimes are allowed at the current time."""
        orb_end = self.params.get("orb_end_time", "10:05")
        midday_start = self.params.get("midday_start_time", "11:30")
        midday_end = self.params.get("midday_end_time", "13:00")
        afternoon_start = self.params.get("afternoon_start_time", "13:00")
        no_new = self.params.get("no_new_entries_after", "15:00")

        if now_t > parse_hhmm(no_new):
            return set()
        if self._time_in_range(now_t, "09:35", orb_end):
            return {"trend"}  # ORB window: trend only
        if self._time_in_range(now_t, orb_end, midday_start):
            return {"trend", "pullback", "range"}  # Primary: all
        if self._time_in_range(now_t, midday_start, midday_end):
            return {"pullback"}  # Midday: pullbacks only
        if self._time_in_range(now_t, afternoon_start, no_new):
            # Range regime is now included in afternoon by default because
            # afternoon tapes are often range-bound and forcing trend/pullback
            # entries there produces late-in-move longs. Range regime handles
            # mean-reversion at the extremes. Disable via
            # ``afternoon_include_range: false`` in params.
            if bool(self.params.get("afternoon_include_range", True)):
                return {"trend", "pullback", "range"}
            return {"trend", "pullback"}
        return set()

    # ------------------------------------------------------------------
    # Signal building per regime
    # ------------------------------------------------------------------
    def _build_trend_signal(self, c: Candidate, side: Side, close: float, atr: float,
                            ltf: pd.DataFrame, frame: pd.DataFrame, regime_score: float,
                            data=None) -> Signal | None:
        lookback = max(3, int(self.params.get("pullback_lookback_bars", 5)))
        session_ltf = ltf[self._same_day_mask(ltf, now_et().date())]
        recent = session_ltf.tail(lookback + 1).iloc[:-1] if len(session_ltf) > lookback else session_ltf.iloc[:-1]
        if recent.empty:
            self._set_build_failure(c.symbol, "trend", "insufficient_ltf_history")
            return None
        buffer = atr * float(self.params.get("stop_buffer_atr_mult", 0.25))
        target_rr = float(self.params.get("trend_target_rr", 2.0))

        if side == Side.LONG:
            trigger_high = self._safe_float(recent["high"].max(), close)
            if close <= trigger_high:
                self._set_build_failure(
                    c.symbol, "trend",
                    f"no_fresh_breakout(close={close:.4f}<=recent_high={trigger_high:.4f})",
                )
                return None
            stop = self._safe_float(recent["low"].min(), close) - buffer
            stop = min(stop, close * (1.0 - self.config.risk.default_stop_pct))
            risk = max(0.01, close - stop)
            target = close + risk * target_rr
        else:
            trigger_low = self._safe_float(recent["low"].min(), close)
            if close >= trigger_low:
                self._set_build_failure(
                    c.symbol, "trend",
                    f"no_fresh_breakdown(close={close:.4f}>=recent_low={trigger_low:.4f})",
                )
                return None
            stop = self._safe_float(recent["high"].max(), close) + buffer
            stop = max(stop, close * (1.0 + self.config.risk.default_stop_pct))
            risk = max(0.01, stop - close)
            target = max(0.01, close - risk * target_rr)

        return self._finalize_signal(c, side, close, stop, target, "trend", regime_score, frame, data)

    def _build_pullback_signal(self, c: Candidate, side: Side, close: float, atr: float,
                               ltf: pd.DataFrame, frame: pd.DataFrame, regime_score: float,
                               data=None) -> Signal | None:
        lookback = max(3, int(self.params.get("pullback_lookback_bars", 5)))
        # ltf is resampled from the full multi-day history frame, so tail(N)
        # crosses session boundary during early RTH. Scope swing/stop lookups
        # to today's session bars only.
        session_ltf = ltf[self._same_day_mask(ltf, now_et().date())]
        recent = session_ltf.tail(lookback + 1).iloc[:-1] if len(session_ltf) > lookback else session_ltf.iloc[:-1]
        if recent.empty:
            self._set_build_failure(c.symbol, "pullback", "insufficient_ltf_history")
            return None
        buffer = atr * float(self.params.get("stop_buffer_atr_mult", 0.25))
        target_rr = float(self.params.get("pullback_target_rr", 2.0))

        if side == Side.LONG:
            stop = self._safe_float(recent["low"].min(), close) - buffer
            stop = min(stop, close * (1.0 - self.config.risk.default_stop_pct))
            risk = max(0.01, close - stop)
            swing_high = self._safe_float(session_ltf.tail(20)["high"].max(), close + risk * target_rr)
            target = max(close + risk * target_rr, swing_high)
        else:
            stop = self._safe_float(recent["high"].max(), close) + buffer
            stop = max(stop, close * (1.0 + self.config.risk.default_stop_pct))
            risk = max(0.01, stop - close)
            swing_low = self._safe_float(session_ltf.tail(20)["low"].min(), close - risk * target_rr)
            target = max(0.01, min(close - risk * target_rr, swing_low))

        return self._finalize_signal(c, side, close, stop, target, "pullback", regime_score, frame, data)

    def _build_range_signal(self, c: Candidate, side: Side, close: float, atr: float,
                            frame: pd.DataFrame, regime_score: float, data=None) -> Signal | None:
        lookback = max(8, int(self.params.get("range_lookback_bars", 20)))
        # Scope to today's session so range_high/range_low are not polluted
        # by prior-session bars during early RTH.
        session_frame = frame[self._same_day_mask(frame, now_et().date())]
        recent = session_frame.tail(lookback)
        if len(recent) < 8:
            self._set_build_failure(c.symbol, "range", f"insufficient_range_bars({len(recent)}<8)")
            return None
        # Reject range entries during a Bollinger squeeze. Squeeze = compressed
        # volatility, typically resolves via breakout — the opposite of what
        # range mean-reversion needs. NFLX 2026-04-24 13:22 SHORT fired on a
        # 12-cent range inside a squeeze (bollinger_width_pct 0.155%,
        # ATR14 0.044 on $92) and stopped at -$11.34 in 2.2 min.
        if bool(self.params.get("reject_range_during_squeeze", True)):
            tech_ctx = self._technical_context(frame)
            if bool(getattr(tech_ctx, "bollinger_squeeze", False)):
                width_pct = float(getattr(tech_ctx, "bollinger_width_pct", 0.0) or 0.0)
                self._set_build_failure(
                    c.symbol, "range",
                    f"range_bollinger_squeeze(width_pct={width_pct:.4f})",
                )
                return None
        range_high = self._safe_float(recent["high"].max(), close)
        range_low = self._safe_float(recent["low"].min(), close)
        buffer = atr * float(self.params.get("stop_buffer_atr_mult", 0.25))
        # Previous-bar confirmation — 2026-04-23 red-from-tick-one bucket
        # (AMZN 10:07, COST 11:09/13:02/15:15, LOW 13:08, HD 14:12 SHORT,
        # V 14:14 SHORT) all fired on an in-progress bar whose live tick
        # happened to cross the range-edge threshold, but the bar itself
        # closed at a mid-range value and the next bar moved adversely.
        # When enabled, require the last COMPLETED bar's close (iloc[-2])
        # to also sit in the entry zone — filters single-tick whipsaws.
        require_prev_bar = bool(self.params.get("range_require_prev_bar_confirmation", True))
        prev_close = None
        if require_prev_bar and len(recent) >= 2:
            prev_close = self._optional_float(recent.iloc[-2].get("close"), None)

        if side == Side.LONG:
            # Enter near range low
            threshold = range_low + (range_high - range_low) * 0.35
            if close > threshold:
                self._set_build_failure(
                    c.symbol, "range",
                    f"not_near_range_low(close={close:.4f}>{threshold:.4f},range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            if require_prev_bar and prev_close is not None and prev_close > threshold:
                self._set_build_failure(
                    c.symbol, "range",
                    f"not_near_range_low_prev_bar(prev_close={prev_close:.4f}>{threshold:.4f},"
                    f"range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            stop = range_low - buffer
            target = range_high - buffer
        else:
            # Enter near range high
            threshold = range_high - (range_high - range_low) * 0.35
            if close < threshold:
                self._set_build_failure(
                    c.symbol, "range",
                    f"not_near_range_high(close={close:.4f}<{threshold:.4f},range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            if require_prev_bar and prev_close is not None and prev_close < threshold:
                self._set_build_failure(
                    c.symbol, "range",
                    f"not_near_range_high_prev_bar(prev_close={prev_close:.4f}<{threshold:.4f},"
                    f"range={range_low:.2f}-{range_high:.2f})",
                )
                return None
            stop = range_high + buffer
            target = max(0.01, range_low + buffer)

        return self._finalize_signal(c, side, close, stop, target, "range", regime_score, frame, data)

    def _finalize_signal(self, c: Candidate, side: Side, close: float, stop: float,
                         target: float, regime: str, regime_score: float,
                         frame: pd.DataFrame, data=None) -> Signal | None:
        """Apply shared gates (structure, S/R, exhaustion, chart patterns) and
        build the final Signal with adaptive management metadata."""
        sr_ctx = self._sr_context(c.symbol, frame, data)
        ms_ctx = self._structure_context(frame, "1m")
        tech_ctx = self._technical_context(frame)
        ctx = self._chart_context(frame)

        # Single ORB-window flag reused by all _finalize_signal ORB-bypasses
        # (Fix D, HTF bias, ORB 5m follow-through, structure entry, SR entry,
        # exhaustion, entered_in_orb_window metadata). Computed once here to
        # avoid duplicate now_et() calls with potential clock-skew at the
        # 10:05 boundary.
        orb_end = self.params.get("orb_end_time", "10:05")
        in_orb_window = now_et().time() <= parse_hhmm(orb_end)

        # Fix D — reject stretched / contradicted entries before expensive
        # signal refinement. Applies to trend + pullback only; range regime
        # is explicitly mean-reversion so "stretched at top" IS the setup.
        # ORB-window bypass: during 09:35-orb_end, Bollinger %B and ATR stretch
        # read extreme values on gap opens (e.g. AMD 2026-04-16 ran +5.4% with
        # pct_b=1.0+ at 09:40), and DMI/OBV reflect stale overnight state.
        # Both are default-bypassed during ORB; post-ORB the gates apply
        # normally to prevent late-in-move chases.
        orb_stretched_bypass = bool(self.params.get("orb_bypass_stretched_filter", False)) and in_orb_window
        orb_tech_bias_bypass = bool(self.params.get("orb_bypass_tech_bias_contradiction", True)) and in_orb_window
        if regime in {"trend", "pullback"}:
            if bool(self.params.get("reject_stretched_entries", True)) and not orb_stretched_bypass:
                pct_b = self._optional_float(getattr(tech_ctx, "bollinger_percent_b", None))
                atr_stretch = self._optional_float(getattr(tech_ctx, "atr_stretch_ema20_mult", None))
                pct_b_max = float(self.params.get("stretched_percent_b_max", 0.80))
                stretch_max = float(self.params.get("stretched_atr_mult_max", 1.1))
                if (
                    side == Side.LONG
                    and pct_b is not None and atr_stretch is not None
                    and pct_b >= pct_b_max and atr_stretch >= stretch_max
                ):
                    self._set_build_failure(
                        c.symbol, regime,
                        f"long_stretched_at_top(pct_b={pct_b:.3f}>={pct_b_max:.2f},stretch={atr_stretch:.2f}>={stretch_max:.2f})",
                    )
                    return None
                if (
                    side == Side.SHORT
                    and pct_b is not None and atr_stretch is not None
                    and pct_b <= (1.0 - pct_b_max) and atr_stretch >= stretch_max
                ):
                    # atr_stretch_ema20_mult is abs(close-ema20)/atr14 — always
                    # non-negative (see technical_levels.py:688). The direction
                    # (above vs below EMA20) is captured by bollinger_percent_b:
                    # pct_b <= 0.15 = near lower band = stretched below. So the
                    # magnitude threshold stretch_max applies symmetrically to
                    # both sides; pct_b alone disambiguates direction.
                    self._set_build_failure(
                        c.symbol, regime,
                        f"short_stretched_at_bottom(pct_b={pct_b:.3f}<={1.0 - pct_b_max:.2f},stretch={atr_stretch:.2f}>={stretch_max:.2f})",
                    )
                    return None
            if bool(self.params.get("reject_tech_bias_contradiction", True)) and not orb_tech_bias_bypass:
                dmi_bias = str(getattr(tech_ctx, "dmi_bias", "neutral") or "neutral").lower()
                obv_bias = str(getattr(tech_ctx, "obv_bias", "neutral") or "neutral").lower()
                if side == Side.LONG and (dmi_bias == "bearish" or obv_bias == "bearish"):
                    self._set_build_failure(
                        c.symbol, regime,
                        f"long_tech_bias_contradicts(dmi={dmi_bias},obv={obv_bias})",
                    )
                    return None
                if side == Side.SHORT and (dmi_bias == "bullish" or obv_bias == "bullish"):
                    self._set_build_failure(
                        c.symbol, regime,
                        f"short_tech_bias_contradicts(dmi={dmi_bias},obv={obv_bias})",
                    )
                    return None

        # Entry-side mirror of resistance_break_exit / support_break_exit
        # in strategy_base.position_exit_signal. Those exits fire on
        # bar-close through sr_ctx.broken_resistance (SHORT) or
        # broken_support (LONG). If entry happens right below/above such
        # a level, the exit triggers on the first reclaim and the trade
        # never had head-room. Thresholds mirror the HTF S/R entry gate
        # (_bearish_sr_block_reason): require both pct and ATR clearance
        # so the stop and exit are separated by a non-trivial band.
        if bool(self.params.get("reject_entry_near_broken_level", True)):
            min_pct = float(self.params.get("broken_level_min_clearance_pct", 0.0025))
            min_atr = float(self.params.get("broken_level_min_clearance_atr", 0.72))
            atr_local = self._safe_float(
                frame.iloc[-1].get("atr14") if (frame is not None and not frame.empty and "atr14" in frame.columns) else None,
                max(close * 0.0015, 0.01),
            )
            if side == Side.SHORT:
                broken_res = getattr(sr_ctx, "broken_resistance", None)
                res_price = float(getattr(broken_res, "price", 0.0) or 0.0) if broken_res is not None else 0.0
                if res_price > close:
                    pct = (res_price - close) / max(close, 1e-9)
                    atr_dist = (res_price - close) / max(atr_local, 1e-9)
                    if pct <= min_pct or atr_dist <= min_atr:
                        self._set_build_failure(
                            c.symbol, regime,
                            f"short_near_broken_resistance(level={res_price:.4f},"
                            f"pct={pct:.4f}<={min_pct:.4f},atr={atr_dist:.2f}<={min_atr:.2f})",
                        )
                        return None
            else:
                broken_sup = getattr(sr_ctx, "broken_support", None)
                sup_price = float(getattr(broken_sup, "price", 0.0) or 0.0) if broken_sup is not None else 0.0
                if 0.0 < sup_price < close:
                    pct = (close - sup_price) / max(close, 1e-9)
                    atr_dist = (close - sup_price) / max(atr_local, 1e-9)
                    if pct <= min_pct or atr_dist <= min_atr:
                        self._set_build_failure(
                            c.symbol, regime,
                            f"long_near_broken_support(level={sup_price:.4f},"
                            f"pct={pct:.4f}<={min_pct:.4f},atr={atr_dist:.2f}<={min_atr:.2f})",
                        )
                        return None

        # HTF bias alignment filter. The higher-timeframe market-structure
        # context (usually 15m) is attached to sr_ctx.market_structure.
        # Two layers:
        #   1. Explicit bias: block if mshtf_bias is opposed to the trade
        #      direction (e.g. LONG vs bearish). This catches confirmed
        #      trend opposition.
        #   2. Pivot pattern: `mshtf_bias` is labeled "bearish" only after
        #      an active BOS/CHoCH, so a stock forming LL/LH pivots may
        #      still read "neutral" while being structurally bearish.
        #      Extend the filter to pullback entries so the AMZN 2026-04-15
        #      pattern is caught (LL pivots, screener bias SHORT, bot
        #      took 3 range/pullback LONGs and lost on all three).
        #   3. Neutral/aligned bias still passes.
        #
        # ORB-window bypass: during the first 30 minutes (09:35-orb_end),
        # the 15-min chart has zero or one completed bars from today — the
        # HTF structure is stale (yesterday's pivots). The trend regime
        # already requires a fresh breakout above recent highs, which is
        # its own directional proof. Blocking on stale HTF bias here
        # killed the TSLA 2026-04-15 open-dip-then-run ($362→$394).
        # After the ORB window, 2-3 closed 15-min bars exist and the
        # filter becomes meaningful again.
        # (orb_end / in_orb_window now computed once at the top of this
        # function so Fix D gates share the same reading; see comment there.)
        # ORB follow-through gate: during the ORB window, require the most
        # recent *completed* 5m bar of today's session to have closed in the
        # signal's direction (bullish bar for LONG, bearish for SHORT). This
        # filters the "poke above range then reverse" false breakouts that
        # dominated 2026-04-17's ORB book (AAPL LONG @269.61 rejected at
        # 268.54 resistance; NFLX SHORT @95.26 squeezed back to 96.82, both
        # within minutes). The gate only engages when ≥2 today's 5m bars
        # exist, so the first 5 minutes of the session (before any 5m bar
        # has closed) remain unconstrained — ORB is allowed to fire at 09:36
        # if momentum is obvious, but must survive the 09:40 5m close.
        if in_orb_window and bool(self.params.get("orb_require_5m_followthrough", True)):
            try:
                frame_5m = self._resampled_frame(frame, 5, symbol=c.symbol, data=data)
            except Exception:
                frame_5m = None
            if frame_5m is not None and not frame_5m.empty:
                now_dt = now_et()
                session_start = now_dt.replace(hour=9, minute=30, second=0, microsecond=0)
                today_bars = frame_5m[frame_5m.index >= session_start] if hasattr(frame_5m, "index") else frame_5m
                # Use iloc[-2] (previous completed bar) when ≥2 exist.
                # iloc[-1] is the currently-forming bar.
                if hasattr(today_bars, "iloc") and len(today_bars) >= 2:
                    last_closed = today_bars.iloc[-2]
                    bar_open = self._optional_float(last_closed.get("open"), None)
                    bar_close = self._optional_float(last_closed.get("close"), None)
                    if bar_open is not None and bar_close is not None:
                        if side == Side.LONG and bar_close <= bar_open:
                            self._set_build_failure(c.symbol, regime, f"long_orb_5m_not_bullish(open={bar_open:.4f},close={bar_close:.4f})")
                            return None
                        if side == Side.SHORT and bar_close >= bar_open:
                            self._set_build_failure(c.symbol, regime, f"short_orb_5m_not_bearish(open={bar_open:.4f},close={bar_close:.4f})")
                            return None
        orb_htf_bypass = bool(self.params.get("orb_bypass_htf_bias", True)) and in_orb_window
        if bool(self.params.get("require_htf_bias_alignment", True)) and not orb_htf_bypass:
            mshtf_ctx = getattr(sr_ctx, "market_structure", None)
            if mshtf_ctx is not None:
                htf_bias = str(getattr(mshtf_ctx, "bias", "neutral") or "neutral").lower()
                pivot_bias = str(getattr(mshtf_ctx, "pivot_bias", "neutral") or "neutral").lower()
                last_high = str(getattr(mshtf_ctx, "last_high_label", "") or "")
                last_low = str(getattr(mshtf_ctx, "last_low_label", "") or "")
                # Layer 1 — explicit opposing bias (applies to all regimes)
                if side == Side.LONG and htf_bias == "bearish":
                    self._set_build_failure(
                        c.symbol, regime,
                        f"htf_bias_bearish(last_high={last_high or 'na'},"
                        f"last_low={last_low or 'na'})",
                    )
                    return None
                if side == Side.SHORT and htf_bias == "bullish":
                    self._set_build_failure(
                        c.symbol, regime,
                        f"htf_bias_bullish(last_high={last_high or 'na'},"
                        f"last_low={last_low or 'na'})",
                    )
                    return None
                # Layer 2 — pullback + trend regimes: block when the HTF
                # pivot pattern itself leans against the trade even though
                # bias is labeled neutral. Extended to trend as Fix E after
                # top_tier INTC 2026-04-20 (-$29) showed mshtf_bias=bullish
                # but mshtf_pivot_bias=bearish let a doomed trend long through.
                # Range regime still skips this — range thesis doesn't
                # presume trend direction.
                # Disable via ``require_htf_pivot_alignment_trend: false``.
                pivot_regimes = {"pullback"}
                if bool(self.params.get("require_htf_pivot_alignment_trend", True)):
                    pivot_regimes.add("trend")
                if regime in pivot_regimes:
                    if side == Side.LONG and last_high == "LH" and last_low in {"LL", "EQL"} and pivot_bias != "bullish":
                        self._set_build_failure(
                            c.symbol, regime,
                            f"htf_pivot_bearish(last_high={last_high},last_low={last_low},"
                            f"pivot_bias={pivot_bias})",
                        )
                        return None
                    if side == Side.SHORT and last_low == "HL" and last_high in {"HH", "EQH"} and pivot_bias != "bearish":
                        self._set_build_failure(
                            c.symbol, regime,
                            f"htf_pivot_bullish(last_high={last_high},last_low={last_low},"
                            f"pivot_bias={pivot_bias})",
                        )
                        return None

        # ORB-window bypasses for 1m structure and S/R blocks. Both signals
        # are backward-looking from the opening action: a 9:30 dump candle
        # registers as CHoCH_down on the 1m chart and flips
        # `breakdown_below_support` to true, blocking LONG entries for
        # several bars even after the recovery. The trend regime's own
        # fresh-breakout gate (`close > recent 5m highs`) already proves
        # direction during the ORB window. After the window, these checks
        # resume normally.
        orb_structure_bypass = bool(self.params.get("orb_bypass_structure_entry", True)) and in_orb_window
        orb_sr_bypass = bool(self.params.get("orb_bypass_sr_entry", True)) and in_orb_window
        # Narrow the SR bypass: it still bypasses the noisy "close to level"
        # checks that the ORB bypass exists to suppress, BUT re-engages when
        # the *opposing* level (resistance for LONG, support for SHORT) is
        # dangerously close — within orb_opposing_sr_atr_mult * ATR. 2026-04-17
        # NFLX was shorted at 95.26 with support that had just broken at 95.90,
        # 0.64 away; AAPL was long'd at 269.61 with resistance 268.54 just
        # above (false break). These are exactly the "entry into the teeth
        # of opposing level" trades that the bypass shouldn't let through.
        orb_opposing_atr_mult = float(self.params.get("orb_opposing_sr_atr_mult", 0.5) or 0.0)
        atr_for_orb = self._safe_float(
            frame.iloc[-1].get("atr14") if (frame is not None and not frame.empty and "atr14" in frame.columns) else None,
            max(close * 0.0015, 0.01),
        )
        opposing_sr_block = False
        if orb_sr_bypass and orb_opposing_atr_mult > 0 and atr_for_orb > 0:
            threshold = orb_opposing_atr_mult * atr_for_orb
            if side == Side.LONG:
                nearest_res = getattr(sr_ctx, "nearest_resistance", None)
                if nearest_res is not None:
                    res_price = float(getattr(nearest_res, "price", 0.0) or 0.0)
                    if res_price > 0 and 0 <= (res_price - close) <= threshold:
                        opposing_sr_block = True
            else:
                nearest_sup = getattr(sr_ctx, "nearest_support", None)
                if nearest_sup is not None:
                    sup_price = float(getattr(nearest_sup, "price", 0.0) or 0.0)
                    # Only count supports BELOW entry (proper floor); a support
                    # that sits above entry is a recently-broken level acting
                    # differently and handled by the SR engine's "breakdown" state.
                    if sup_price > 0 and 0 <= (close - sup_price) <= threshold:
                        opposing_sr_block = True
        effective_sr_bypass = orb_sr_bypass and not opposing_sr_block
        if side == Side.LONG:
            if not orb_structure_bypass and self._blocks_bullish_structure_entry(ms_ctx):
                self._set_build_failure(c.symbol, regime, self._bullish_structure_block_reason(ms_ctx))
                return None
            if not effective_sr_bypass and self._blocks_bullish_sr_entry(sr_ctx):
                self._set_build_failure(c.symbol, regime, self._bullish_sr_block_reason(sr_ctx))
                return None
            if opposing_sr_block:
                self._set_build_failure(c.symbol, regime, f"long_orb_opposing_resistance_within_{orb_opposing_atr_mult:.2f}atr")
                return None
            stop, target = self._refine_bullish_sr_levels(close, stop, target, sr_ctx)
            stop, target = self._refine_bullish_technical_levels(close, stop, target, tech_ctx, frame)
        else:
            if not orb_structure_bypass and self._blocks_bearish_structure_entry(ms_ctx):
                self._set_build_failure(c.symbol, regime, self._bearish_structure_block_reason(ms_ctx))
                return None
            if not effective_sr_bypass and self._blocks_bearish_sr_entry(sr_ctx):
                self._set_build_failure(c.symbol, regime, self._bearish_sr_block_reason(sr_ctx))
                return None
            if opposing_sr_block:
                self._set_build_failure(c.symbol, regime, f"short_orb_opposing_support_within_{orb_opposing_atr_mult:.2f}atr")
                return None
            stop, target = self._refine_bearish_sr_levels(close, stop, target, sr_ctx)
            stop, target = self._refine_bearish_technical_levels(close, stop, target, tech_ctx, frame)

        # Entry exhaustion check — skipped during the ORB window because
        # VWAP and EMA9 haven't equilibrated after the open. A sharp
        # V-reversal (e.g. TSLA 2026-04-15 open dump $367→$362 then run
        # to $394) artificially depresses VWAP, making the recovery look
        # "extended" when it's really the trend establishing itself.
        # After the ORB window, VWAP reflects today's action and the
        # filter becomes meaningful.
        orb_exhaustion_bypass = bool(self.params.get("orb_bypass_exhaustion", True)) and in_orb_window
        if not orb_exhaustion_bypass:
            vwap = self._safe_float(frame.iloc[-1].get("vwap"), close)
            ema9 = self._safe_float(frame.iloc[-1].get("ema9"), close)
            exhaustion = self._entry_exhaustion_reasons(side, frame, close=close, vwap=vwap, ema9=ema9)
            if exhaustion:
                self._set_build_failure(c.symbol, regime, exhaustion[0])
                return None

        # Scoring
        structure_bonus = 0.75 if getattr(ms_ctx, "bias", "neutral") == ("bullish" if side == Side.LONG else "bearish") else 0.0
        if side == Side.LONG and getattr(ms_ctx, "bos_up", False) and self._structure_event_recent(getattr(ms_ctx, "bos_up_age_bars", None)):
            structure_bonus += 0.5
        elif side == Side.SHORT and getattr(ms_ctx, "bos_down", False) and self._structure_event_recent(getattr(ms_ctx, "bos_down_age_bars", None)):
            structure_bonus += 0.5
        if side == Side.LONG:
            pattern_bonus = 0.35 if ctx.matched_bullish_continuation else (0.15 if ctx.matched_bullish_reversal else 0.0)
        else:
            pattern_bonus = 0.35 if ctx.matched_bearish_continuation else (0.15 if ctx.matched_bearish_reversal else 0.0)

        # Candle pattern confirmation on the trigger frame.
        # directional_candle_signal returns opposite_score/opposite_net_score
        # from the SAME @lru_cache'd context — no extra ta-lib calls.
        # _candle_context slices internally to CANDLE_CONTEXT_BARS so TA-Lib
        # has enough context to initialize its internal state.
        candle_signal = self._directional_candle_signal(frame, side)
        # Entry filter: reject when the opposing-direction candle cluster is
        # at or above candles.opposing_net_score_threshold (default 0.70 =
        # "solid" tier). Mirrors shared_exit.use_candle_pattern_exit on the
        # entry side.
        if self._shared_entry_enabled("use_opposing_candle_filter", False):
            opposing_net = float(candle_signal.get("opposite_net_score", 0.0) or 0.0)
            threshold = float(self._candles_setting("opposing_net_score_threshold", 0.70))
            if opposing_net >= threshold:
                # _candle_context is cached per strategy instance — second
                # call on same frame returns the stored dict, no detection.
                cc = self._candle_context(frame)
                opp_prefix = "bearish" if side == Side.LONG else "bullish"
                opp_matches = ",".join(
                    str(m) for m in sorted(cc.get(f"matched_{opp_prefix}_candles", []) or [])[:3]
                )
                self._set_build_failure(
                    c.symbol, regime,
                    f"{'long' if side == Side.LONG else 'short'}_opposing_candle"
                    f"(net_score={opposing_net:.2f}>={threshold:.2f},"
                    f"matches={opp_matches or 'na'})",
                )
                return None
        candle_bonus = 0.0
        candle_confirmed = bool(candle_signal.get("confirmed"))
        if candle_confirmed:
            tier = str(candle_signal.get("confirm_tier", ""))
            if tier == "strong_3c":
                candle_bonus = 0.40
            elif tier == "solid_2c":
                candle_bonus = 0.25
            else:
                candle_bonus = 0.10

        adjustments = self._entry_adjustment_components(side, sr_ctx=sr_ctx, tech_ctx=tech_ctx)
        fvg_adjustments = self._fvg_entry_adjustment_components(side, c.symbol, frame, data)
        fvg_cont_bias = float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0)
        runner_allowed = bool(fvg_cont_bias >= 0.35 and structure_bonus >= 0.75)

        # Apply adaptive_ladder rungs when configured. The helper falls back
        # to the original target when ladder mode isn't active, the regime
        # opts out (range), or no qualifying rungs exist — so this call is
        # safe to make unconditionally.
        atr_for_ladder = self._safe_float(
            frame.iloc[-1].get("atr14") if (frame is not None and not frame.empty and "atr14" in frame.columns) else None,
            max(close * 0.0015, 0.01),
        )
        target, ladder_meta = self._apply_ladder_if_enabled(
            side, close, stop, target,
            regime=regime, sr_ctx=sr_ctx, atr=atr_for_ladder,
        )
        # Trail-runner: in adaptive_ladder mode, when no qualifying rungs
        # exist for a trend/pullback entry, drop the fixed target so the
        # trade runs until the trailing stop + structural exits (CHoCH, SR
        # loss) catch it. A fixed 2R target here would prematurely close a
        # trend-day move (e.g. TSLA 2026-04-15: $365→$394 run, 2R target
        # exits at $372). Range regime keeps its single target at
        # range_high — laddering past the range contradicts its thesis.
        ladder_mode_active = self.config.risk.trade_management_mode == "adaptive_ladder"
        if ladder_mode_active and not ladder_meta and regime in {"trend", "pullback"}:
            target = None
            runner_allowed = False

        # Fix G — CEILING on target extension past nearest opposing SR;
        # complements entry_min_clearance_atr (FLOOR on SR clearance).
        # Placement invariant: runs AFTER ladder + runner override so
        # `target` is the trade's FINAL take-profit (None in runner mode →
        # gate inert). Trend-only: range targets ARE opposing SR by design.
        if (
            regime == "trend"
            and target is not None
            and bool(self.params.get("reject_target_beyond_sr", True))
        ):
            target_max_sr_ratio = float(self.params.get("target_max_sr_ratio", 0.8))
            tgt = float(target)
            if side == Side.LONG:
                near = getattr(sr_ctx, "nearest_resistance", None)
                level_price = float(getattr(near, "price", 0.0) or 0.0)
                valid = level_price > close
                dist_to_sr = level_price - close if valid else 0.0
                dist_to_target = tgt - close
                level_name = "resistance"
                reason_prefix = "long_target_beyond_resistance"
            else:
                near = getattr(sr_ctx, "nearest_support", None)
                level_price = float(getattr(near, "price", 0.0) or 0.0)
                valid = 0.0 < level_price < close
                dist_to_sr = close - level_price if valid else 0.0
                dist_to_target = close - tgt
                level_name = "support"
                reason_prefix = "short_target_beyond_support"
            if valid and dist_to_target > dist_to_sr * target_max_sr_ratio:
                ratio = dist_to_target / dist_to_sr
                self._set_build_failure(
                    c.symbol, regime,
                    f"{reason_prefix}(target={tgt:.4f},"
                    f"{level_name}={level_price:.4f},ratio={ratio:.2f}>{target_max_sr_ratio:.2f})",
                )
                return None

        management = self._adaptive_management_components(
            side, close, stop, target, style=regime,
            runner_allowed=runner_allowed, continuation_bias=fvg_cont_bias,
        )
        activity_weight = float(self.params.get("activity_score_weight", 0.12))
        final_priority_score = (
            regime_score
            + (float(c.activity_score) * activity_weight)
            + structure_bonus
            + pattern_bonus
            + candle_bonus
            + adjustments["entry_context_adjustment"]
            + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
        )

        reason = f"top_tier_{regime}_{'long' if side == Side.LONG else 'short'}"
        # Tag entries that used an ORB-window bypass so post-session analysis
        # can slice performance by ORB vs post-ORB entries. 2026-04-17 ORB
        # entries were 1W/4T (-$120); post-ORB 10:05-11:00 window was 2W/5T
        # (+$126) thanks to TSLA. Without an explicit tag we reconstruct from
        # entry timestamps, which conflates 10:00-10:05 edge cases.
        entered_in_orb_window = bool(in_orb_window)
        metadata = self._build_signal_metadata(
            entry_price=close,
            chart_ctx=ctx, ms_ctx=ms_ctx, sr_ctx=sr_ctx, tech_ctx=tech_ctx,
            adjustments=adjustments, fvg_adjustments=fvg_adjustments,
            management=management, ladder_meta=ladder_meta,
            final_priority_score=final_priority_score,
            leading={
                "regime": regime,
                "regime_score": round(regime_score, 4),
                "structure_bonus": round(structure_bonus, 4),
                "pattern_bonus": round(pattern_bonus, 4),
                "candle_bonus": round(candle_bonus, 4),
                "candle_confirmed": candle_confirmed,
                "candle_tier": candle_signal.get("confirm_tier"),
                "candle_anchor": candle_signal.get("anchor_pattern"),
                "candle_matches": candle_signal.get("matches", []),
                "orb_window_entry": entered_in_orb_window,
                "orb_end_time": str(orb_end),
            },
        )
        return Signal(
            symbol=c.symbol, strategy=self.strategy_name, side=side,
            reason=reason, stop_price=float(stop),
            target_price=None if target is None else float(target),
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # entry_signals — main loop
    # ------------------------------------------------------------------
    def entry_signals(
        self,
        candidates: list[Candidate],
        bars: dict[str, pd.DataFrame],
        positions: dict[str, Position],
        client=None,
        data=None,
    ) -> list[Signal]:
        self._reset_entry_decisions()
        out: list[Signal] = []
        min_bars = int(self.params.get("min_bars", 60) or 60)
        trigger_tf = max(1, int(self.params.get("trigger_timeframe_minutes", 5)))
        min_trigger_bars = int(self.params.get("min_trigger_bars", 15))
        allow_short = bool(self.config.risk.allow_short)
        now_t = now_et().time()
        allowed_regimes = self._allowed_regimes(now_t)
        if not allowed_regimes:
            for c in candidates:
                self._record_entry_decision(c.symbol, "skipped", ["outside_entry_window"])
            return out
        # ORB window flag used to gate the index-confirmation hard block.
        # Scoring still reflects index state accurately (index_ok is passed
        # to _score_trend); this bypass only prevents the "index not
        # confirmed" hard skip during 09:35-orb_end when the 5m VWAP/EMA
        # on SPY/QQQ is still equilibrating after the open.
        orb_end = self.params.get("orb_end_time", "10:05")
        in_orb_window = now_t <= parse_hhmm(orb_end)
        orb_index_bypass = bool(self.params.get("orb_bypass_index_confirmation", True)) and in_orb_window

        # SPY/QQQ bars are cycle-invariant; hoist out of per-candidate loop.
        sides_to_evaluate = [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]
        index_ok_by_side = {side: self._index_confirms(side, bars, data) for side in sides_to_evaluate}
        idx_neutral = self._index_neutral(bars)

        for c in candidates:
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue
            frame = bars.get(c.symbol)
            if frame is None or len(frame) < min_bars:
                self._record_entry_decision(c.symbol, "skipped", [
                    self._insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), min_bars)])
                continue

            ltf = self._resampled_frame(frame, trigger_tf, symbol=c.symbol, data=data)
            if ltf is None or ltf.empty or len(ltf) < min_trigger_bars:
                self._record_entry_decision(c.symbol, "skipped", ["missing_ltf_context"])
                continue

            last = ltf.iloc[-1]
            close = self._safe_float(last["close"])
            vwap = self._safe_float(last.get("vwap"), close)
            ema9 = self._safe_float(last.get("ema9"), close)
            ema20 = self._safe_float(last.get("ema20"), close)
            adx = self._safe_float(last.get("adx14"), 0.0)
            ret5 = self._safe_float(last.get("ret5"), 0.0)
            ret15 = self._safe_float(last.get("ret15"), 0.0)
            atr = max(self._safe_float(last.get("atr14"), close * 0.0015), close * 0.0005, 0.01)

            # Determine preferred side from candidate bias.
            # Fix A: when respect_screener_bias is enabled (default), a
            # directional-biased candidate is evaluated on that side ONLY — no
            # fallthrough to the opposite side. Previously the strategy would
            # evaluate [SHORT, LONG] for a SHORT-tagged candidate and if SHORT
            # scoring failed on a short-term bounce, fall through and take LONG.
            # That pattern produced the 2026-04-20 META/INTC/TSLA losses: all
            # three had change_from_open deeply negative (screener tagged SHORT)
            # but the strategy took LONG via fallthrough.
            #
            # ORB-window bypass: during the first 30 min (09:35-orb_end),
            # change_from_open is dominated by the opening gap — a gap-down
            # day that reverses (TSLA 2026-04-15 $367→$362→$394) correctly
            # belongs to the LONG side, but change_from_open still reads
            # negative and the screener tags SHORT. Fallthrough was the old
            # escape hatch for this pattern; the bypass preserves it during
            # ORB while Fix A still applies post-ORB.
            orb_screener_bypass = bool(self.params.get("orb_bypass_screener_bias", True)) and in_orb_window
            respect_screener_bias = bool(self.params.get("respect_screener_bias", True)) and not orb_screener_bypass

            # Trailing-bias memory for Fix A. A momentary "neutral" bias on
            # a symbol that's been consistently SHORT (or LONG) for many
            # bars should NOT open the door to a counter-direction trade.
            # 2026-04-23 GOOG 12:51 pullback_long fired with current bias
            # None after the 10 preceding decisions all had side_pref=SHORT;
            # lost $22. Infer an effective bias from the trailing window
            # when current is None and recent was strongly one-sided.
            trailing_enabled = bool(self.params.get("trailing_bias_enabled", True))
            trailing_lookback = max(3, int(self.params.get("trailing_bias_lookback", 10)))
            trailing_threshold = float(self.params.get("trailing_bias_majority_threshold", 0.7))
            effective_bias = c.directional_bias
            if effective_bias is None and trailing_enabled and respect_screener_bias:
                recent = list(self._recent_directional_bias.get(c.symbol, ()))
                long_count = sum(1 for b in recent if b == Side.LONG)
                short_count = sum(1 for b in recent if b == Side.SHORT)
                total_directional = long_count + short_count
                min_directional = max(3, trailing_lookback // 2)
                if total_directional >= min_directional:
                    if short_count / total_directional >= trailing_threshold:
                        effective_bias = Side.SHORT
                    elif long_count / total_directional >= trailing_threshold:
                        effective_bias = Side.LONG
            # Record the raw (not inferred) bias for future cycles.
            hist = self._recent_directional_bias.get(c.symbol)
            if hist is None or hist.maxlen != trailing_lookback:
                existing = list(hist) if hist is not None else []
                hist = deque(existing[-trailing_lookback:], maxlen=trailing_lookback)
                self._recent_directional_bias[c.symbol] = hist
            hist.append(c.directional_bias)

            if effective_bias == Side.LONG:
                if respect_screener_bias:
                    preferred_sides = [Side.LONG]
                else:
                    preferred_sides = [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]
            elif effective_bias == Side.SHORT:
                if not allow_short:
                    self._record_entry_decision(c.symbol, "skipped", ["shorts_disabled"])
                    continue
                preferred_sides = [Side.SHORT] if respect_screener_bias else [Side.SHORT, Side.LONG]
            else:
                # Neutral bias (change_from_open in [-0.20%, +0.20%]) with
                # no strong trailing lean: evaluate both sides — screener
                # hasn't picked a side and recent history is too mixed.
                preferred_sides = [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]

            best_signal: Signal | None = None
            fail_reasons: list[str] = []

            for side in preferred_sides:
                index_ok = index_ok_by_side.get(side, False)

                # Trend must always be scored — pullback's min_pullback_trend_score
                # gate reads trend_score as input. Pullback/range are skipped
                # entirely when not in the current time window's allowed_regimes
                # (e.g. ORB window is trend-only → skip pullback + range).
                trend_score = self._score_trend(side, close, vwap, ema9, ema20, adx, ret5, ret15, index_ok)
                pullback_score = (
                    self._score_pullback(side, close, vwap, ema9, ema20, adx, atr, trend_score, ltf)
                    if "pullback" in allowed_regimes else 0.0
                )
                range_score = (
                    self._score_range(close, vwap, ema9, ema20, frame, idx_neutral)
                    if "range" in allowed_regimes else 0.0
                )

                scores = {
                    "trend": trend_score,
                    "pullback": pullback_score,
                    "range": range_score,
                }
                min_trend = float(self.params.get("min_trend_score", 4.0))
                min_pullback = float(self.params.get("min_pullback_score", 4.0))
                min_range = float(self.params.get("min_range_score", 3.5))
                min_gap = float(self.params.get("min_score_gap", 1.5))

                ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
                top_regime, top_score = ranked[0]
                second_score = ranked[1][1] if len(ranked) > 1 else 0.0

                # Select regime
                selected = None
                if top_regime == "trend" and top_score >= min_trend and (top_score - second_score) >= min_gap and "trend" in allowed_regimes:
                    selected = "trend"
                elif top_regime == "pullback" and top_score >= min_pullback and "pullback" in allowed_regimes:
                    selected = "pullback"
                elif top_regime == "range" and top_score >= min_range and (top_score - second_score) >= min_gap * 0.67 and "range" in allowed_regimes:
                    selected = "range"
                # Fallback: the primary selection above enforces the min_gap
                # ambiguity guard on the TOP regime. If that fails, we fall
                # through here and try each regime in rank order using ONLY
                # its min-score threshold (no gap check). This lets us
                # recover the TSLA-style case where trend=5.0, pullback=3.0,
                # range=4.0 failed the primary gap check (gap=1.0 < 1.2)
                # even though trend clearly dominates; we still attempt the
                # trend signal build, which has its own fresh-breakout
                # filter to reject low-quality breakouts. Same for other
                # regime fallbacks.
                if selected is None:
                    for regime_name, regime_score in ranked:
                        if regime_name not in allowed_regimes:
                            continue
                        if regime_name == "trend" and regime_score >= min_trend:
                            selected = "trend"
                            break
                        if regime_name == "pullback" and regime_score >= min_pullback:
                            selected = "pullback"
                            break
                        if regime_name == "range" and regime_score >= min_range:
                            selected = "range"
                            break

                if selected is None:
                    fail_reasons.append(f"{side.value.lower()}_no_qualifying_regime(trend={trend_score:.1f},pb={pullback_score:.1f},range={range_score:.1f})")
                    continue

                # Index confirmation for trend/pullback (skipped during ORB
                # window when orb_bypass_index_confirmation is true).
                if selected in {"trend", "pullback"} and not index_ok and not orb_index_bypass:
                    fail_reasons.append(f"{side.value.lower()}_{selected}_index_not_confirmed")
                    continue

                regime_score_val = scores[selected]
                sig = None
                if selected == "trend":
                    sig = self._build_trend_signal(c, side, close, atr, ltf, frame, regime_score_val, data)
                elif selected == "pullback":
                    sig = self._build_pullback_signal(c, side, close, atr, ltf, frame, regime_score_val, data)
                elif selected == "range":
                    sig = self._build_range_signal(c, side, close, atr, frame, regime_score_val, data)

                if sig is not None:
                    best_signal = sig
                    break
                failure = self._consume_build_failure(c.symbol, selected) or f"{selected}_signal_build_failed"
                fail_reasons.append(f"{side.value.lower()}_{failure}")

            if best_signal is not None:
                out.append(best_signal)
                self._record_entry_decision(c.symbol, "signal", [best_signal.reason])
            else:
                self._record_entry_decision(c.symbol, "skipped", fail_reasons or ["no_setup"])
        return out

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------
    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
