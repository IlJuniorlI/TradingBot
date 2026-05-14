# SPDX-License-Identifier: MIT
"""Multi-regime adaptive intraday strategy for top-tier liquid stocks.

Detects whether each symbol is trending, pulling back, ranging, breaking
out of a volatility squeeze, sustaining momentum from the session open,
or scalping between HTF S/R zones — then applies the appropriate entry
style. Index confirmation uses a per-sector ETF map (see
``sector_index_map`` + ``_indices_for_symbol``) so each symbol is gated
by its actual sector tape, not an arbitrary broad-market ETF. Trades
both long and short across the full RTH session with time-of-day regime
gating.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..shared import (
    Candidate,
    Position,
    Side,
    Signal,
    _bar_close_position,
    _bar_wick_fractions,
    insufficient_bars_reason,
    _optional_float,
    _safe_float,
    _same_day_mask,
    _session_open_price,
    now_et,
    parse_hhmm,
    pd,
)
from ..strategy_base import BaseStrategy


class TopTierAdaptiveStrategy(BaseStrategy):
    strategy_name = "top_tier_adaptive"

    def __init__(self, config):
        super().__init__(config)
        # Per-symbol rolling window of recent LIVE directional bias
        # observations (output of ``_compute_live_directional_bias``).
        # Trailing-bias memory for Fix A (2026-04-23): when the current
        # bar's live bias is None (day_strength within the neutral band),
        # infer the effective side from recent observations if one side
        # dominates.
        self._recent_directional_bias: dict[str, deque[Side | None]] = {}

    # ------------------------------------------------------------------
    # Watchlist — include all configured index confirmation ETFs so they
    # get history fetching, streaming, and appear in the bars dict. The
    # specific ETFs depend on which sectors the active universe touches
    # (see ``sector_index_map`` in config) — could be sector ETFs
    # (XLK/XLE/XLB/...) and/or broad-market (SPY/QQQ).
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
    def _indices_for_symbol(self, symbol: str) -> list[str]:
        """Return the index ETFs to consult when confirming trades on
        *symbol*. Walks ``sector_groups`` to find which sector owns the
        symbol, then reads ``sector_index_map[sector]`` for the per-sector
        ETF list.

        Falls back to the universe-wide ``index_symbols`` when the symbol
        isn't in any sector OR the sector has no per-sector ETF mapping.
        Backward-compat: dropping ``sector_index_map`` from config = the
        old "OR across every index_symbols entry" behavior.

        The fallback is intentional, not lazy — it lets a user opt-in
        sector-by-sector instead of forcing them to map all 11 GICS sectors
        upfront. Sectors without a map entry retain the broad-market
        confirmation path.
        """
        fallback = [
            str(s).upper().strip()
            for s in (self.params.get("index_symbols") or ["SPY", "QQQ"])
            if str(s).strip()
        ]
        sector_groups = self.params.get("sector_groups") or {}
        sector_index_map = self.params.get("sector_index_map") or {}
        if not sector_groups or not sector_index_map:
            return fallback
        symbol_upper = str(symbol).upper()
        for sector_name, members in sector_groups.items():
            if not isinstance(members, (list, tuple)):
                continue
            if symbol_upper in {str(m).upper() for m in members if m}:
                mapped = sector_index_map.get(sector_name)
                if mapped:
                    cleaned = [str(s).upper().strip() for s in mapped if str(s).strip()]
                    if cleaned:
                        return cleaned
                break
        return fallback

    def _index_confirms(self, side: Side, symbol: str, bars: dict[str, pd.DataFrame], _data=None) -> bool:
        """Return True if at least one index ETF for *symbol*'s sector
        agrees with *side*. The candidate-aware lookup prevents e.g. an
        AAPL LONG from being confirmed by XLE (energy ETF) just because
        XLE happens to be bullish-aligned. See ``_indices_for_symbol`` for
        the lookup behavior + fallback semantics."""
        if not bool(self.params.get("require_index_confirmation", True)):
            return True
        index_symbols = self._indices_for_symbol(symbol)
        for sym in index_symbols:
            frame = bars.get(sym)
            if frame is None or frame.empty:
                continue
            last = frame.iloc[-1]
            close = _safe_float(last["close"])
            vwap = _safe_float(last.get("vwap"), close)
            ema9 = _safe_float(last.get("ema9"), close)
            ema20 = _safe_float(last.get("ema20"), close)
            if side == Side.LONG and close > vwap and ema9 >= ema20:
                return True
            if side == Side.SHORT and close < vwap and ema9 <= ema20:
                return True
        return False

    def _index_neutral(self, symbol: str, bars: dict[str, pd.DataFrame]) -> bool:
        """Return True if the per-symbol indices (see ``_indices_for_symbol``)
        are not strongly directional — i.e. all are within 0.25% of their
        own VWAP. Used by the range regime's scoring bonus."""
        index_symbols = self._indices_for_symbol(symbol)
        for sym in index_symbols:
            frame = bars.get(sym)
            if frame is None or frame.empty:
                continue
            last = frame.iloc[-1]
            close = _safe_float(last["close"])
            vwap = _safe_float(last.get("vwap"), close)
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
        session_ltf = ltf[_same_day_mask(ltf, now_et().date())]
        score = 0.0
        touch_mult = float(self.params.get("pullback_ema_touch_atr_mult", 0.35))
        touch_dist = atr * touch_mult
        lookback = max(2, int(self.params.get("pullback_lookback_bars", 5)))
        recent = session_ltf.tail(lookback + 1).iloc[:-1] if len(session_ltf) > lookback else session_ltf.iloc[:-1]
        if recent.empty:
            return 0.0

        if side == Side.LONG:
            recent_low = _safe_float(recent["low"].min(), close)
            touched_ema20 = recent_low <= ema20 + touch_dist
            touched_vwap = recent_low <= vwap + touch_dist
            if touched_ema20 or touched_vwap:
                score += 1.5
            hold_mult = float(self.params.get("pullback_hold_atr_mult", 0.40))
            if recent_low >= ema20 - (atr * hold_mult):
                score += 1.0
            if close > ema9:
                score += 1.0
            close_pos = _bar_close_position(session_ltf if not session_ltf.empty else ltf)
            if close_pos >= 0.60:
                score += 0.5
        else:
            recent_high = _safe_float(recent["high"].max(), close)
            touched_ema20 = recent_high >= ema20 - touch_dist
            touched_vwap = recent_high >= vwap - touch_dist
            if touched_ema20 or touched_vwap:
                score += 1.5
            hold_mult = float(self.params.get("pullback_hold_atr_mult", 0.40))
            if recent_high <= ema20 + (atr * hold_mult):
                score += 1.0
            if close < ema9:
                score += 1.0
            close_pos = _bar_close_position(session_ltf if not session_ltf.empty else ltf)
            if close_pos <= 0.40:
                score += 0.5

        # Volume expansion on current bar
        vol_src = session_ltf if not session_ltf.empty else ltf
        vol = _safe_float(vol_src.iloc[-1].get("volume"), 0.0)
        vol_mean = _safe_float(recent["volume"].mean(), 1.0)
        if vol_mean > 0 and vol / vol_mean >= 1.10:
            score += 0.5
        if adx >= float(self.params.get("min_adx14", 15.0)):
            score += 0.5
        return score

    def _score_range(self, close: float, vwap: float, ema9: float, ema20: float,
                     frame: pd.DataFrame, index_neutral: bool) -> float:
        session_frame = frame[_same_day_mask(frame, now_et().date())]
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

    def _score_vol_squeeze(self, side: Side, close: float, vwap: float, ema9: float,
                           ema20: float, atr: float, frame: pd.DataFrame, tech_ctx) -> float:
        """Score the Bollinger-squeeze breakout setup. Looks for compressed-range
        consolidation followed by a directional break out of the box. Compression
        comes from a tight box_range + low BB width OR an active BB squeeze flag
        on tech_ctx; the breakout comes from current close clearing the
        box-high (LONG) / box-low (SHORT) with a buffer. Volume and bar-close
        position add confirmation points. Ported (lighter) from
        volatility_squeeze_breakout/strategy.py."""
        lookback = max(6, int(self.params.get("vol_squeeze_lookback_bars", 12)))
        session_frame = frame[_same_day_mask(frame, now_et().date())]
        if len(session_frame) < lookback + 2:
            return 0.0
        # Look at the box (last lookback bars BEFORE the current one)
        last = session_frame.iloc[-1]
        prior = session_frame.iloc[:-1]
        box = prior.tail(lookback)
        if len(box) < lookback:
            return 0.0
        box_high = _safe_float(box["high"].max(), close)
        box_low = _safe_float(box["low"].min(), close)
        box_range = max(0.0, box_high - box_low)
        box_range_pct = (box_range / close) if close > 0 else 0.0
        box_range_atr = (box_range / atr) if atr > 0 else float("inf")
        max_range_pct = float(self.params.get("vol_squeeze_max_range_pct", 0.012))
        max_range_atr = float(self.params.get("vol_squeeze_max_range_atr", 1.8))
        compression_box_ok = box_range_pct <= max_range_pct and box_range_atr <= max_range_atr
        bb_squeeze_flag = bool(getattr(tech_ctx, "bollinger_squeeze", False))
        bb_width_pct = _safe_float(getattr(tech_ctx, "bollinger_width_pct", None), 0.0)
        max_width_pct = float(self.params.get("vol_squeeze_max_width_pct", 0.05))
        compression_bb_ok = bb_squeeze_flag or (0.0 < bb_width_pct <= max_width_pct)

        score = 0.0
        if compression_box_ok:
            score += 2.0
        if compression_bb_ok:
            score += 1.0
        if compression_box_ok and bb_squeeze_flag:
            # Both compression signals agreeing — strong setup
            score += 0.5

        # Breakout detection (side-aware)
        buffer = float(self.params.get("vol_squeeze_breakout_buffer_pct", 0.0008))
        if side == Side.LONG:
            broke_out = _safe_float(last["close"]) >= box_high * (1.0 + buffer)
        else:
            broke_out = _safe_float(last["close"]) <= box_low * (1.0 - buffer)
        if broke_out:
            score += 1.5

        # Volume confirmation: current bar volume vs box-median
        try:
            vol_baseline = max(1.0, float(box["volume"].median()))
        except Exception:
            vol_baseline = 1.0
        cur_vol = _safe_float(last.get("volume"), 0.0)
        min_vol_ratio = float(self.params.get("vol_squeeze_min_breakout_volume_ratio", 1.12))
        if cur_vol >= vol_baseline * min_vol_ratio:
            score += 0.5

        # Bar close position
        close_pos = _bar_close_position(session_frame)
        min_close_pos = float(self.params.get("vol_squeeze_min_bar_close_position", 0.63))
        if side == Side.LONG and close_pos >= min_close_pos:
            score += 0.5
        if side == Side.SHORT and close_pos <= (1.0 - min_close_pos):
            score += 0.5

        # Alignment with VWAP/EMA (cheap continuation confirmation)
        if side == Side.LONG and close > vwap and ema9 >= ema20:
            score += 0.5
        if side == Side.SHORT and close < vwap and ema9 <= ema20:
            score += 0.5
        return score

    def _score_momentum(self, side: Side, close: float, vwap: float, ema9: float,
                        ema20: float, ret15: float, frame: pd.DataFrame) -> float:
        """Score the momentum-from-open setup. The thesis is a stock with
        strong day-direction (live ``day_strength`` from session open) that's
        breaking out of a recent N-bar high (or low for SHORT), still in a
        trend-aligned posture, with ret15 confirming acceleration. Uses live
        frame data to compute ``day_strength`` rather than the screener's
        stale change_from_open. Renamed from ``_score_momentum_close``
        2026-05-12 when the regime was generalized from afternoon-only to
        post-ORB through close (skip-ORB) — the day_strength hard gate is
        what filters out chop, not the time window. Scoring logic ported
        (lighter) from the standalone ``momentum_close`` strategy."""
        # Compute live day_strength from session open + current close
        today_open = _session_open_price(frame)
        if today_open is None or today_open <= 0:
            return 0.0
        day_strength = (close - today_open) / today_open * 100.0
        min_day = float(self.params.get("momentum_min_day_strength", 1.5))

        # Hard gate: side-correct day strength magnitude
        if side == Side.LONG and day_strength < min_day:
            return 0.0
        if side == Side.SHORT and day_strength > -min_day:
            return 0.0

        # N-bar breakout (use today's bars only)
        lookback = max(3, int(self.params.get("momentum_breakout_lookback_bars", 6)))
        session_frame = frame[_same_day_mask(frame, now_et().date())]
        if len(session_frame) < lookback + 1:
            return 0.0
        recent = session_frame.tail(lookback + 1).iloc[:-1]
        if recent.empty:
            return 0.0

        score = 0.0
        # day_strength magnitude scoring (tier-based)
        ds_abs = abs(day_strength)
        if ds_abs >= min_day:
            score += 1.0
        if ds_abs >= min_day * 2.0:
            score += 1.0
        if ds_abs >= min_day * 3.0:
            score += 0.5

        # Breakout above N-bar high (LONG) / below low (SHORT)
        if side == Side.LONG:
            breakout_level = _safe_float(recent["high"].max(), close)
            if close > breakout_level:
                score += 1.5
            if close > vwap:
                score += 1.0
            if ema9 >= ema20:
                score += 0.5
            if ret15 > 0:
                score += 0.5
        else:
            breakout_level = _safe_float(recent["low"].min(), close)
            if close < breakout_level:
                score += 1.5
            if close < vwap:
                score += 1.0
            if ema9 <= ema20:
                score += 0.5
            if ret15 < 0:
                score += 0.5
        return score

    @staticmethod
    def _score_sr_scalp(side: Side, close: float, vwap: float, ema9: float,
                        ema20: float, atr: float, adx: float,
                        frame: pd.DataFrame) -> float:
        """Score the HTF S/R scalp setup. Mean-reversion thesis: price is
        rotating between two HTF support / resistance levels with enough
        room between them to scalp. The score is PERMISSIVE — it only
        checks bar character + neutrality conditions ("does this LOOK
        like a scalp setup"). The HARD constraint (HTF zone gap +
        proximity to the entry level) is enforced in
        ``_build_sr_scalp_signal``; entries with too-close S/R zones get
        rejected at build time so other regimes can fall through.

        Score components (max 5.0):
          * +1.5  bar shows a rejection wick on the trade side
                  (lower wick >= 0.40 of bar range for LONG, upper for SHORT)
          * +1.0  VWAP-neutral: |close - vwap| <= 0.5 * ATR
          * +1.0  EMA-neutral:  |ema9 - ema20| / close <= 0.001
          * +1.0  ADX low (no trend): adx14 <= 18
          * +0.5  base score (regime is in play)
        """
        score = 0.5  # base: regime is in play
        upper_wick, lower_wick, _body, bar_range = _bar_wick_fractions(frame)
        if bar_range > 0:
            wick = lower_wick if side == Side.LONG else upper_wick
            if wick >= 0.40:
                score += 1.5
        if vwap > 0 and atr > 0 and abs(close - vwap) <= 0.5 * atr:
            score += 1.0
        if close > 0 and abs(ema9 - ema20) / close <= 0.001:
            score += 1.0
        if adx <= 18.0:
            score += 1.0
        return score

    # ------------------------------------------------------------------
    # Live directional bias
    # ------------------------------------------------------------------
    def _compute_live_bias_and_day_strength(
        self, frame: pd.DataFrame, close: float,
    ) -> tuple[Side | None, float | None]:
        """Single-source computation: returns ``(bias, day_strength_pct)``.

        ``day_strength_pct = (close − session_open) / session_open * 100``.
        Bias is ``Side.LONG`` when day_strength exceeds
        ``+directional_bias_min_day_strength`` (default 0.20%), ``Side.SHORT``
        below the negative threshold, ``None`` within the neutral band.

        Both values share one ``_session_open_price`` call so the soft-bias
        penalty path doesn't repeat the session-open lookup. Used by
        ``entry_signals`` (needs both bias for side selection AND magnitude
        for penalty scaling) and by ``_compute_live_directional_bias`` (the
        single-return wrapper kept for test compatibility).

        Returns ``(None, None)`` when the session_open is unavailable
        (warmup frame, missing data, etc.).
        """
        session_open = _session_open_price(frame)
        if session_open is None or session_open <= 0:
            return None, None
        try:
            day_strength = (float(close) - session_open) / session_open * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            return None, None
        threshold = float(self.params.get("directional_bias_min_day_strength", 0.20))
        if day_strength > threshold:
            return Side.LONG, day_strength
        if day_strength < -threshold:
            return Side.SHORT, day_strength
        return None, day_strength

    def _compute_live_directional_bias(self, frame: pd.DataFrame, close: float) -> Side | None:
        """Bias-only wrapper around ``_compute_live_bias_and_day_strength``.
        Kept as the public API for tests + any caller that doesn't need
        the day_strength magnitude. Same semantics as before: returns
        ``Side.LONG`` / ``Side.SHORT`` / ``None`` based on day_strength
        vs. ``directional_bias_min_day_strength``."""
        bias, _ = self._compute_live_bias_and_day_strength(frame, close)
        return bias

    def _volatility_widening_factor(self, tech_ctx: Any, current_time: Any = None) -> float:
        """Stop-buffer widening multiplier — combines TWO orthogonal triggers:

        1. **ATR expansion (Tier 2a, existing)** — when the current bar's
           ATR is large vs its 5-bar average (``tech_ctx.atr_expansion_mult
           > atr_widening_threshold``), stops scale up linearly to
           ``atr_widening_max_factor``. Catches trend-day RELATIVE
           volatility surges.

        2. **Early-session time-of-day widening (new)** — applies an
           ABSOLUTE multiplier when ``current_time`` is before
           ``early_session_stop_widening_until``. Catches the post-open
           high-vol window where ATR may not be "expanding" relative to
           recent bars (which are all noisy) but absolute vol is high.
           Without this, a trade taken at 10:10 on AMD got stopped by a
           single 1m wick that recovered 5 min later — the stop was set
           by Tier 2a's RELATIVE-expansion logic, which read normal at
           the time even though absolute volatility was elevated.

        The factor returned is ``max(expansion_factor, time_factor)``,
        capped at ``atr_widening_max_factor``. Tier 2a + time-of-day
        don't compound (multiplying both would over-widen on volatile
        morning opens); the trade gets whichever cushion is larger.

        Returns 1.0 when:
          * ``atr_aware_stop_enabled`` is False (master toggle)
          * No expansion AND not in early session
          * ``tech_ctx`` is missing or has no ``atr_expansion_mult`` AND
            ``current_time`` is unknown
        """
        if not bool(self.params.get("atr_aware_stop_enabled", True)):
            return 1.0
        max_factor = float(self.params.get("atr_widening_max_factor", 1.5))

        # --- Trigger 1: ATR expansion (Tier 2a) ---
        expansion_factor = 1.0
        expansion_mult = float(getattr(tech_ctx, "atr_expansion_mult", 1.0) or 1.0) if tech_ctx is not None else 1.0
        threshold = float(self.params.get("atr_widening_threshold", 1.3))
        if expansion_mult > threshold:
            # Linear scale from 1.0 (at threshold) to max_factor (at 2x threshold)
            scale_range = max(0.1, threshold)
            progress = min(1.0, (expansion_mult - threshold) / scale_range)
            expansion_factor = 1.0 + (max_factor - 1.0) * progress

        # --- Trigger 2: Early-session time-of-day widening ---
        time_factor = 1.0
        if current_time is not None and bool(self.params.get("early_session_stop_widening_enabled", True)):
            try:
                cutoff = parse_hhmm(str(self.params.get("early_session_stop_widening_until", "10:30")))
                if current_time <= cutoff:
                    time_factor = float(self.params.get("early_session_stop_widening_mult", 1.3))
            except Exception:
                pass

        # Take the LARGER widening (not compound) so morning + ATR
        # expansion don't double-multiply into an unrealistic stop.
        return min(max(expansion_factor, time_factor), max_factor)

    def _bias_penalty(self, side: Side, day_strength: float | None,
                      effective_bias: Side | None, respect_bias: bool) -> float:
        """Soft-bias score adjustment applied to a side's regime scores
        when the side disagrees with ``effective_bias``.

        Returns 0.0 when:
          * ``respect_bias`` is False (master toggle off / ORB bypass active)
          * ``effective_bias`` is None (no bias set)
          * ``side`` agrees with ``effective_bias``

        Otherwise returns ``bias_penalty_base * magnitude_factor`` where
        ``magnitude_factor = min(1.0, |day_strength| / bias_penalty_saturate_at)``.
        Default base 1.0, saturate at 2.0% day_strength — so a −0.5% day with
        SHORT bias applies only a 0.25 penalty to LONG-side regimes (a strong
        structural LONG setup can still qualify), while a −3% deep-down day
        applies the full 1.0 penalty (filters most LONG-side setups).

        Replaces Fix A's previous HARD lockout (``preferred_sides = [Side]``).
        Both sides are now always evaluated; the penalty filters weak
        counter-bias setups while letting strong structural ones through.
        Preserves the 2026-04-20 protection (deep day_strength → full
        penalty) and the trailing-bias memory (inferred bias still
        contributes to the penalty).
        """
        if not respect_bias:
            return 0.0
        if effective_bias is None or effective_bias == side:
            return 0.0
        penalty_base = float(self.params.get("bias_penalty_base", 1.0))
        saturate_at = max(0.1, float(self.params.get("bias_penalty_saturate_at", 2.0)))
        ds = float(day_strength) if day_strength is not None else 0.0
        magnitude_factor = min(1.0, abs(ds) / saturate_at)
        return penalty_base * magnitude_factor

    # ------------------------------------------------------------------
    # Time-of-day gating
    # ------------------------------------------------------------------
    @staticmethod
    def _time_in_range(now_t, start: str, end: str) -> bool:
        return parse_hhmm(start) <= now_t <= parse_hhmm(end)

    def _allowed_regimes(self, now_t) -> set[str]:
        """Return which regimes are allowed at the current time.

        Window cutoffs are param-driven (orb_end_time, midday_start_time,
        midday_end_time, afternoon_start_time, no_new_entries_after) — no
        hard-coded times.

        Six regimes:
          - trend / pullback / range: primary scoring regimes
          - vol_squeeze: Bollinger-squeeze breakout. Allowed in the primary
            window (orb_end → midday_start) and the afternoon
            (afternoon_start → no_new).
          - momentum: momentum-from-open continuation. Allowed post-ORB
            through close (orb_end → no_new). Includes midday because the
            ``momentum_min_day_strength`` hard gate filters out chop —
            stocks without enough intraday move score zero. Renamed from
            ``momentum_close`` 2026-05-12 when the window was widened from
            afternoon-only.
          - sr_scalp: HTF S/R mean-reversion scalp. Allowed post-ORB
            through close (orb_end → no_new). Excluded from the ORB
            window because morning chop near recent levels often breaks
            through; the build-time distance gate
            (``sr_scalp_min_distance_pct`` / ``sr_scalp_min_distance_atr``)
            rejects when the HTF zones are too close to be worth the
            round-trip.

        Each regime has its own opt-out knob via params:
          disable_trend_regime / disable_pullback_regime /
          disable_range_regime / disable_vol_squeeze_regime /
          disable_momentum_regime / disable_sr_scalp_regime.

        The 09:35 → orb_end_time window has a separate whole-window
        opt-out (``disable_orb_window``) that skips the ORB window entirely
        — different from ``orb_bypass_*`` flags (which loosen filters
        within the ORB window). Use this when the opening 30 minutes
        are too whippy and you'd rather start trading at ``orb_end_time``.

        Score thresholds (min_*_score) gate each regime independently and
        the score-gap auction picks the winner.
        """
        orb_end = self.params.get("orb_end_time", "10:05")
        midday_start = self.params.get("midday_start_time", "11:30")
        midday_end = self.params.get("midday_end_time", "13:00")
        afternoon_start = self.params.get("afternoon_start_time", "13:00")
        no_new = self.params.get("no_new_entries_after", "15:00")
        orb_window_enabled = not bool(self.params.get("disable_orb_window", False))
        trend_enabled = not bool(self.params.get("disable_trend_regime", False))
        pullback_enabled = not bool(self.params.get("disable_pullback_regime", False))
        range_enabled = not bool(self.params.get("disable_range_regime", False))
        vol_squeeze_enabled = not bool(self.params.get("disable_vol_squeeze_regime", False))
        momentum_enabled = not bool(self.params.get("disable_momentum_regime", False))
        sr_scalp_enabled = not bool(self.params.get("disable_sr_scalp_regime", False))

        def _filter(regimes: set[str]) -> set[str]:
            """Strip regimes whose disable knob is set."""
            if not trend_enabled:
                regimes.discard("trend")
            if not pullback_enabled:
                regimes.discard("pullback")
            if not range_enabled:
                regimes.discard("range")
            if not vol_squeeze_enabled:
                regimes.discard("vol_squeeze")
            if not momentum_enabled:
                regimes.discard("momentum")
            if not sr_scalp_enabled:
                regimes.discard("sr_scalp")
            return regimes

        if now_t > parse_hhmm(no_new):
            return set()
        if self._time_in_range(now_t, "09:35", orb_end):
            # Whole-window opt-out: when disable_orb_window is true, the
            # 09:35 → orb_end window is treated as a no-entry zone. Useful
            # for tapes where the opening 30 minutes are too whippy to
            # trade reliably; the bot then starts taking entries at
            # orb_end_time instead. The orb_bypass_* flags loosen filters
            # within the ORB window; this flag skips the window entirely.
            if not orb_window_enabled:
                return set()
            return _filter({"trend"})  # ORB window: trend only
        if self._time_in_range(now_t, orb_end, midday_start):
            # Primary window: full regime mix including momentum + sr_scalp.
            # The day_strength gate filters momentum; the distance gate
            # filters sr_scalp (zones must be far enough apart). Neither
            # blocks the trend / pullback / range / vol_squeeze regimes.
            return _filter({"trend", "pullback", "range", "vol_squeeze", "momentum", "sr_scalp"})
        if self._time_in_range(now_t, midday_start, midday_end):
            # Midday: pullbacks remain the default fit for top-tier chop,
            # but momentum is allowed because day_strength >= threshold
            # implies a stock is genuinely trending despite the lunchtime
            # tape. sr_scalp is also allowed — midday's low-volatility
            # chop is often the cleanest scalp environment between HTF
            # zones (when the gap qualifies).
            return _filter({"pullback", "momentum", "sr_scalp"})
        if self._time_in_range(now_t, afternoon_start, no_new):
            # Range regime is included in afternoon by default because
            # afternoon tapes are often range-bound and forcing trend/pullback
            # entries there produces late-in-move longs. Range regime handles
            # mean-reversion at the extremes. Disable via
            # ``afternoon_include_range: false`` in params (or globally via
            # ``disable_range_regime: true``).
            if bool(self.params.get("afternoon_include_range", True)):
                regimes = {"trend", "pullback", "range", "vol_squeeze", "momentum", "sr_scalp"}
            else:
                regimes = {"trend", "pullback", "vol_squeeze", "momentum", "sr_scalp"}
            return _filter(regimes)
        return set()

    # ------------------------------------------------------------------
    # Signal building per regime
    # ------------------------------------------------------------------
    def _build_trend_signal(self, c: Candidate, side: Side, close: float, atr: float,
                            ltf: pd.DataFrame, frame: pd.DataFrame, regime_score: float,
                            data=None, vol_widening: float = 1.0) -> Signal | None:
        lookback = max(3, int(self.params.get("pullback_lookback_bars", 5)))
        session_ltf = ltf[_same_day_mask(ltf, now_et().date())]
        recent = session_ltf.tail(lookback + 1).iloc[:-1] if len(session_ltf) > lookback else session_ltf.iloc[:-1]
        if recent.empty:
            self._set_build_failure(c.symbol, "trend", "insufficient_ltf_history")
            return None
        # ATR buffer + default_stop_pct floor both scale with vol_widening
        # (Tier 2a) — trend-day capture: wider noise tolerance, same dollar
        # risk per trade (risk manager downsizes share count).
        buffer = atr * float(self.params.get("stop_buffer_atr_mult", 0.25)) * vol_widening
        effective_default_stop_pct = self.config.risk.default_stop_pct * vol_widening
        target_rr = float(self.params.get("trend_target_rr", 2.0))

        if side == Side.LONG:
            trigger_high = _safe_float(recent["high"].max(), close)
            if close <= trigger_high:
                self._set_build_failure(
                    c.symbol, "trend",
                    f"no_fresh_breakout(close={close:.4f}<=recent_high={trigger_high:.4f})",
                )
                return None
            stop = _safe_float(recent["low"].min(), close) - buffer
            stop = min(stop, close * (1.0 - effective_default_stop_pct))
            risk = max(0.01, close - stop)
            target = close + risk * target_rr
        else:
            trigger_low = _safe_float(recent["low"].min(), close)
            if close >= trigger_low:
                self._set_build_failure(
                    c.symbol, "trend",
                    f"no_fresh_breakdown(close={close:.4f}>=recent_low={trigger_low:.4f})",
                )
                return None
            stop = _safe_float(recent["high"].max(), close) + buffer
            stop = max(stop, close * (1.0 + effective_default_stop_pct))
            risk = max(0.01, stop - close)
            target = max(0.01, close - risk * target_rr)

        return self._finalize_signal(c, side, close, stop, target, "trend", regime_score, frame, data)

    def _build_pullback_signal(self, c: Candidate, side: Side, close: float, atr: float,
                               ltf: pd.DataFrame, frame: pd.DataFrame, regime_score: float,
                               data=None, vol_widening: float = 1.0) -> Signal | None:
        lookback = max(3, int(self.params.get("pullback_lookback_bars", 5)))
        # ltf is resampled from the full multi-day history frame, so tail(N)
        # crosses session boundary during early RTH. Scope swing/stop lookups
        # to today's session bars only.
        session_ltf = ltf[_same_day_mask(ltf, now_et().date())]
        recent = session_ltf.tail(lookback + 1).iloc[:-1] if len(session_ltf) > lookback else session_ltf.iloc[:-1]
        if recent.empty:
            self._set_build_failure(c.symbol, "pullback", "insufficient_ltf_history")
            return None
        # vol_widening applied to both ATR buffer and default_stop_pct floor (Tier 2a).
        buffer = atr * float(self.params.get("stop_buffer_atr_mult", 0.25)) * vol_widening
        effective_default_stop_pct = self.config.risk.default_stop_pct * vol_widening
        target_rr = float(self.params.get("pullback_target_rr", 2.0))

        if side == Side.LONG:
            stop = _safe_float(recent["low"].min(), close) - buffer
            stop = min(stop, close * (1.0 - effective_default_stop_pct))
            risk = max(0.01, close - stop)
            swing_high = _safe_float(session_ltf.tail(20)["high"].max(), close + risk * target_rr)
            target = max(close + risk * target_rr, swing_high)
        else:
            stop = _safe_float(recent["high"].max(), close) + buffer
            stop = max(stop, close * (1.0 + effective_default_stop_pct))
            risk = max(0.01, stop - close)
            swing_low = _safe_float(session_ltf.tail(20)["low"].min(), close - risk * target_rr)
            target = max(0.01, min(close - risk * target_rr, swing_low))

        return self._finalize_signal(c, side, close, stop, target, "pullback", regime_score, frame, data)

    def _build_range_signal(self, c: Candidate, side: Side, close: float, atr: float,
                            frame: pd.DataFrame, regime_score: float, data=None,
                            vol_widening: float = 1.0) -> Signal | None:
        lookback = max(8, int(self.params.get("range_lookback_bars", 20)))
        # Scope to today's session so range_high/range_low are not polluted
        # by prior-session bars during early RTH.
        session_frame = frame[_same_day_mask(frame, now_et().date())]
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
        range_high = _safe_float(recent["high"].max(), close)
        range_low = _safe_float(recent["low"].min(), close)
        # vol_widening applied (Tier 2a). Note: in range, the buffer also
        # pulls in the target (target = range_high - buffer for LONG) so
        # both stop room AND target conservatism scale with volatility,
        # which is the correct direction (wider noise needs both).
        buffer = atr * float(self.params.get("stop_buffer_atr_mult", 0.25)) * vol_widening
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
            prev_close = _optional_float(recent.iloc[-2].get("close"), None)

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

    def _build_vol_squeeze_signal(self, c: Candidate, side: Side, close: float, atr: float,
                                  frame: pd.DataFrame, regime_score: float,
                                  data=None, vol_widening: float = 1.0) -> Signal | None:
        """Build a Bollinger-squeeze breakout signal. Stops sit just outside
        the squeeze box (below box_low for LONG / above box_high for SHORT),
        with an ATR-floored buffer to absorb noise around the breakout. Target
        is the standard RR multiple. Shared filters (HTF bias, stretched-entry,
        SR/structure, FVG retest, etc.) run inside ``_finalize_signal``."""
        lookback = max(6, int(self.params.get("vol_squeeze_lookback_bars", 12)))
        session_frame = frame[_same_day_mask(frame, now_et().date())]
        if len(session_frame) < lookback + 2:
            self._set_build_failure(c.symbol, "vol_squeeze", "insufficient_session_bars")
            return None
        prior = session_frame.iloc[:-1]
        box = prior.tail(lookback)
        if len(box) < lookback:
            self._set_build_failure(c.symbol, "vol_squeeze", "insufficient_box_bars")
            return None
        box_high = _safe_float(box["high"].max(), close)
        box_low = _safe_float(box["low"].min(), close)
        box_range = max(0.0, box_high - box_low)
        target_rr = float(self.params.get("vol_squeeze_target_rr", 2.05))
        # Stop buffer scales with box range so tighter squeezes don't get
        # over-wide ATR-based stops. Mirrors source strategy logic.
        # vol_widening (Tier 2a) applies on top of the max() so all three
        # buffer floors expand together in trend-day regimes.
        stop_buffer = max(atr * 0.12, close * 0.0010, box_range * 0.22) * vol_widening
        effective_default_stop_pct = self.config.risk.default_stop_pct * vol_widening

        if side == Side.LONG:
            stop = box_low - stop_buffer
            stop = min(stop, close * (1.0 - effective_default_stop_pct))
            risk = max(0.01, close - stop)
            target = close + risk * target_rr
        else:
            stop = box_high + stop_buffer
            stop = max(stop, close * (1.0 + effective_default_stop_pct))
            risk = max(0.01, stop - close)
            target = max(0.01, close - risk * target_rr)

        return self._finalize_signal(c, side, close, stop, target, "vol_squeeze", regime_score, frame, data)

    def _build_momentum_signal(self, c: Candidate, side: Side, close: float, atr: float,
                               frame: pd.DataFrame,
                               regime_score: float, data=None,
                               vol_widening: float = 1.0) -> Signal | None:
        """Build a momentum-from-open continuation signal. Stops anchor below
        recent swing low (LONG) / above recent swing high (SHORT) with an
        ATR-cushioned buffer so single-bar wicks (during midday's lower volume
        or afternoon thin liquidity) don't trigger the stop. Target uses
        ``momentum_target_rr``.

        Uses the 1m ``frame`` for the N-bar breakout check so it stays
        consistent with ``_score_momentum`` and with the source standalone
        momentum_close strategy. Renamed from ``_build_momentum_close_signal``
        2026-05-12 when the regime was generalized from afternoon-only to
        post-ORB through close.
        """
        lookback = max(3, int(self.params.get("momentum_breakout_lookback_bars", 6)))
        session_frame = frame[_same_day_mask(frame, now_et().date())]
        recent = session_frame.tail(lookback + 1).iloc[:-1] if len(session_frame) > lookback else session_frame.iloc[:-1]
        if recent.empty:
            self._set_build_failure(c.symbol, "momentum", "insufficient_session_history")
            return None

        # Fresh-breakout gate (matches the source strategy's check)
        if side == Side.LONG:
            breakout_level = _safe_float(recent["high"].max(), close)
            if close <= breakout_level:
                self._set_build_failure(
                    c.symbol, "momentum",
                    f"no_fresh_breakout(close={close:.4f}<=recent_high={breakout_level:.4f})",
                )
                return None
        else:
            breakout_level = _safe_float(recent["low"].min(), close)
            if close >= breakout_level:
                self._set_build_failure(
                    c.symbol, "momentum",
                    f"no_fresh_breakdown(close={close:.4f}>=recent_low={breakout_level:.4f})",
                )
                return None

        target_rr = float(self.params.get("momentum_target_rr", 2.0))
        # ATR-cushioned swing anchor (mirrors standalone momentum_close/strategy.py:76-79).
        # vol_widening (Tier 2a) widens the ATR cushion AND the
        # default_stop_pct floor so momentum trades on trend days don't
        # get knocked out by expanded per-bar noise.
        effective_default_stop_pct = self.config.risk.default_stop_pct * vol_widening
        if side == Side.LONG:
            swing = _safe_float(recent["low"].min(), close) - (atr * 0.08 * vol_widening)
            stop = max(close * (1.0 - effective_default_stop_pct), swing)
            risk = max(0.01, close - stop)
            target = close + risk * target_rr
        else:
            swing = _safe_float(recent["high"].max(), close) + (atr * 0.08 * vol_widening)
            stop = min(close * (1.0 + effective_default_stop_pct), swing)
            risk = max(0.01, stop - close)
            target = max(0.01, close - risk * target_rr)

        return self._finalize_signal(c, side, close, stop, target, "momentum", regime_score, frame, data)

    def _build_sr_scalp_signal(self, c: Candidate, side: Side, close: float, atr: float,
                               frame: pd.DataFrame, regime_score: float,
                               data=None, vol_widening: float = 1.0) -> Signal | None:
        """Build an HTF S/R scalp signal — mean-reversion between the bot's
        existing HTF support / resistance zones.

        Uses the bot's existing S/R machinery — NO strategy-local level
        creation. All level prices, zone bands, and stop nudges come from
        the same sources the rest of the bot uses:

          * Level prices: ``sr_ctx.nearest_support.price`` (HS) and
            ``sr_ctx.nearest_resistance.price`` (HR). Same fields the
            dashboard labels "HS" / "HR" and ``_refine_*_sr_levels`` use.
          * Zone bands: ``zone_atr_mult * atr`` or ``zone_pct * close``
            (max), defaulting to the bot-wide 0.20*atr / 0.15%*close.
            Same construction the dashboard's ``key_level_zones`` use.
          * Stop nudge: ``sr_ctx.level_buffer`` × ``vol_widening``. Same
            buffer ``_refine_bullish_sr_levels`` / ``_refine_bearish_sr_levels``
            use to nudge stops past structural levels.

        Strict build-time gates:
          1. Both ``nearest_support`` (HS) and ``nearest_resistance`` (HR)
             exist on ``sr_ctx``. No scalp without two-sided zones.
          2. Inner gap between zones ``(HR_zone_lower − HS_zone_upper)``
             clears BOTH the % floor (``sr_scalp_min_distance_pct *
             close``, default 0.8%) AND the ATR floor
             (``sr_scalp_min_distance_atr * atr``, default 2.5x). Inner
             gap (not center-to-center) is the conservative measure —
             wide zones mean the tradeable space between them shrinks.
          3. Close is inside the entry-side zone OR within
             ``sr_scalp_max_distance_from_zone_atr * atr`` of its inner
             edge (default 0.5x). Mid-range candles get rejected.
          4. Close hasn't broken through the entry-side zone (LONG:
             ``close > HS_zone_lower``; SHORT: ``close < HR_zone_upper``).
             Bounces only, not breakdowns.

        Stop = HS_zone_lower − level_buffer (LONG) or HR_zone_upper +
        level_buffer (SHORT). Target = HR_zone_lower − level_buffer
        (LONG) or HS_zone_upper + level_buffer (SHORT) — exits at the
        opposite zone's inner edge, matching the bot's structural-exit
        conventions everywhere else.
        """
        sr_ctx = self._sr_context(c.symbol, frame, data)
        support_level = getattr(sr_ctx, "nearest_support", None)
        resistance_level = getattr(sr_ctx, "nearest_resistance", None)
        support_price = float(getattr(support_level, "price", 0.0) or 0.0) if support_level is not None else 0.0
        resistance_price = float(getattr(resistance_level, "price", 0.0) or 0.0) if resistance_level is not None else 0.0
        if support_price <= 0.0 and resistance_price <= 0.0:
            self._set_build_failure(c.symbol, "sr_scalp", "missing_htf_levels_both")
            return None
        if support_price <= 0.0:
            self._set_build_failure(c.symbol, "sr_scalp", "missing_htf_support")
            return None
        if resistance_price <= 0.0:
            self._set_build_failure(c.symbol, "sr_scalp", "missing_htf_resistance")
            return None
        if resistance_price <= support_price:
            self._set_build_failure(c.symbol, "sr_scalp", "inverted_htf_levels")
            return None

        # Zone band half-width — bot's existing zone construction (same
        # formula as dashboard's key_level_zones via
        # dashboard_level_context_spec). Reads ``zone_atr_mult`` /
        # ``zone_pct`` from params; defaults match the bot-wide defaults.
        zone_atr_mult = float(self.params.get("zone_atr_mult", 0.20))
        zone_pct = float(self.params.get("zone_pct", 0.0015))
        zone_half_width = max(zone_atr_mult * atr, close * zone_pct, 0.01)
        support_zone_lower = support_price - zone_half_width
        support_zone_upper = support_price + zone_half_width
        resistance_zone_lower = resistance_price - zone_half_width
        resistance_zone_upper = resistance_price + zone_half_width

        # Inner zone gap = tradeable distance between zone edges. Must
        # clear both floors (max wins).
        zone_gap_inner = resistance_zone_lower - support_zone_upper
        min_gap_pct = float(self.params.get("sr_scalp_min_distance_pct", 0.008))
        min_gap_atr = float(self.params.get("sr_scalp_min_distance_atr", 2.5))
        required_gap = max(min_gap_pct * close, min_gap_atr * atr)
        if zone_gap_inner < required_gap:
            self._set_build_failure(
                c.symbol, "sr_scalp",
                f"htf_zones_too_close(inner_gap={zone_gap_inner:.4f}<{required_gap:.4f},"
                f"pct_floor={min_gap_pct*close:.4f},atr_floor={min_gap_atr*atr:.4f})",
            )
            return None

        # Proximity gate — close must be inside the entry-side zone OR
        # within ``proximity_buffer`` of its inner edge (toward midrange).
        proximity_atr_mult = float(self.params.get("sr_scalp_max_distance_from_zone_atr", 0.5))
        proximity_buffer = proximity_atr_mult * atr

        # Stop nudge — bot's existing ``sr_ctx.level_buffer`` (same buffer
        # ``_refine_*_sr_levels`` uses everywhere). Scales with
        # ``vol_widening`` (Tier 2a) on trend-day expansion. Defensive
        # fallback if sr_ctx didn't supply one.
        level_buffer = float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0) * vol_widening
        if level_buffer <= 0.0:
            level_buffer = max(atr * 0.05, 0.01) * vol_widening

        if side == Side.LONG:
            # Broken-support guard: a bar trading BELOW the support zone's
            # lower edge is a breakdown, not a bounce — wrong setup type.
            if close <= support_zone_lower:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"long_below_support_zone(close={close:.4f}<=zone_low={support_zone_lower:.4f})",
                )
                return None
            # Proximity: must be inside support zone OR within proximity_buffer
            # above its upper edge.
            if close > support_zone_upper + proximity_buffer:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"long_far_from_support_zone(close={close:.4f}>{support_zone_upper+proximity_buffer:.4f})",
                )
                return None
            stop = support_zone_lower - level_buffer
            target = resistance_zone_lower - level_buffer
        else:
            if close >= resistance_zone_upper:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"short_above_resistance_zone(close={close:.4f}>=zone_high={resistance_zone_upper:.4f})",
                )
                return None
            if close < resistance_zone_lower - proximity_buffer:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"short_far_from_resistance_zone(close={close:.4f}<{resistance_zone_lower-proximity_buffer:.4f})",
                )
                return None
            stop = resistance_zone_upper + level_buffer
            target = support_zone_upper + level_buffer

        return self._finalize_signal(c, side, close, stop, target, "sr_scalp", regime_score, frame, data)

    def _finalize_signal(self, c: Candidate, side: Side, close: float, stop: float,
                         target: float, regime: str, regime_score: float,
                         frame: pd.DataFrame, data=None) -> Signal | None:
        """Apply shared gates (structure, S/R, exhaustion, chart patterns) and
        build the final Signal with adaptive management metadata."""
        sr_ctx = self._sr_context(c.symbol, frame, data)
        ms_ctx = self._structure_context(frame, "ltf")
        tech_ctx = self._technical_context(frame)
        ctx = self._chart_context(frame)
        htf_ctx = self._default_htf_context_for_score(c.symbol, data)

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
                pct_b = _optional_float(getattr(tech_ctx, "bollinger_percent_b", None))
                atr_stretch = _optional_float(getattr(tech_ctx, "atr_stretch_ema20_mult", None))
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
                    # non-negative (computed in build_technical_levels_context).
                    # The direction
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
            atr_local = _safe_float(
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
                    bar_open = _optional_float(last_closed.get("open"), None)
                    bar_close = _optional_float(last_closed.get("close"), None)
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
        atr_for_orb = _safe_float(
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
            vwap = _safe_float(frame.iloc[-1].get("vwap"), close)
            ema9 = _safe_float(frame.iloc[-1].get("ema9"), close)
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

        adjustments = self._entry_adjustment_components(side, sr_ctx=sr_ctx, tech_ctx=tech_ctx, htf_ctx=htf_ctx)
        fvg_adjustments = self._fvg_entry_adjustment_components(side, c.symbol, frame, data)
        fvg_cont_bias = float(fvg_adjustments.get("fvg_continuation_bias", 0.0) or 0.0)
        runner_allowed = bool(fvg_cont_bias >= 0.35 and structure_bonus >= 0.75)

        # Apply adaptive_ladder rungs when configured. The helper falls back
        # to the original target when ladder mode isn't active, the regime
        # opts out (range), or no qualifying rungs exist — so this call is
        # safe to make unconditionally.
        atr_for_ladder = _safe_float(
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
        ltf_min = max(1, int(self.params.get("ltf_minutes", 5)))
        min_ltf_bars = int(self.params.get("min_ltf_bars", 15))
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
        # on the sector ETFs (XLK/XLE/etc.) are still equilibrating after
        # the open.
        orb_end = self.params.get("orb_end_time", "10:05")
        in_orb_window = now_t <= parse_hhmm(orb_end)
        orb_index_bypass = bool(self.params.get("orb_bypass_index_confirmation", True)) and in_orb_window

        # Index ok / neutral are now PER-CANDIDATE because of the
        # ``sector_index_map``-driven per-sector ETF lookup (see
        # ``_indices_for_symbol``). An AAPL LONG checks XLK; an XOM LONG
        # checks XLE; FCX/NEM check XLB; etc. Moved into the per-
        # candidate loop below since the result varies by ``c.symbol``
        # even within a single cycle.
        sides_to_evaluate = [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]

        for c in candidates:
            if c.symbol in positions:
                self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
                continue
            frame = bars.get(c.symbol)
            if frame is None or len(frame) < min_bars:
                self._record_entry_decision(c.symbol, "skipped", [
                    insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), min_bars)])
                continue

            ltf = self._resampled_frame(frame, ltf_min, symbol=c.symbol, data=data)
            if ltf is None or ltf.empty or len(ltf) < min_ltf_bars:
                self._record_entry_decision(c.symbol, "skipped", ["missing_ltf_context"])
                continue

            # Per-candidate index lookup — uses ``sector_index_map`` to route
            # AAPL → XLK, XOM → XLE, FCX → XLB, etc. Falls back to
            # ``index_symbols`` when no sector mapping exists for the symbol.
            index_ok_by_side = {side: self._index_confirms(side, c.symbol, bars, data) for side in sides_to_evaluate}
            idx_neutral = self._index_neutral(c.symbol, bars)

            last = ltf.iloc[-1]
            close = _safe_float(last["close"])
            vwap = _safe_float(last.get("vwap"), close)
            ema9 = _safe_float(last.get("ema9"), close)
            ema20 = _safe_float(last.get("ema20"), close)
            adx = _safe_float(last.get("adx14"), 0.0)
            ret5 = _safe_float(last.get("ret5"), 0.0)
            ret15 = _safe_float(last.get("ret15"), 0.0)
            atr = max(_safe_float(last.get("atr14"), close * 0.0015), close * 0.0005, 0.01)

            # Soft-bias gating (Fix A, refactored 2026-05-12). The original
            # Fix A hard-locked ``preferred_sides`` to one direction when
            # ``effective_bias`` was set, fully suppressing the opposite
            # side. That correctly blocked the 2026-04-20 META/INTC/TSLA
            # fallthrough losses (deeply-negative day_strength + intraday
            # bounce), but was too rigid for the opposite case: a stock
            # with mild bias and a strong structural setup on the opposite
            # side (fresh BOS↑, breakout above HTF resistance, bullish
            # structure_bias) had its LONG opportunity silently ignored.
            #
            # Replaced with a soft score-penalty in ``_bias_penalty``:
            # both sides are always evaluated, but the side that disagrees
            # with ``effective_bias`` has each of its regime scores reduced
            # by ``bias_penalty_base * min(1.0, |day_strength| / saturate_at)``.
            # A mild −0.5% day applies only ~0.25 penalty (strong setups
            # still pass min_*_score). A deep −3% day applies the full
            # 1.0 penalty (filters all but the strongest setups, preserving
            # the 2026-04-20 protection).
            #
            # The bias is computed LIVE from session_open + current close
            # (``_compute_live_directional_bias``), authoritative for
            # decisions; the screener's pre-computed
            # ``c.directional_bias`` is only used by the gatekeeper's
            # cooldown lookup before entry_signals runs.
            #
            # ORB-window bypass (``orb_bypass_screener_bias``, default
            # ``true``): during 09:35→orb_end, day_strength is dominated
            # by the opening gap; the bypass disables the penalty entirely
            # so gap-fade entries (TSLA 2026-04-15 $367→$362→$394) can
            # qualify on either side without bias drag.
            orb_screener_bypass = bool(self.params.get("orb_bypass_screener_bias", True)) and in_orb_window
            respect_bias = bool(self.params.get("respect_screener_bias", True)) and not orb_screener_bypass

            # Single computation — bias for side selection + day_strength
            # magnitude for penalty scaling, sharing one _session_open_price
            # lookup (instead of the two separate calls the prior code did).
            live_bias, day_strength = self._compute_live_bias_and_day_strength(frame, close)

            # Trailing-bias memory: when current live_bias is None but the
            # recent window of decisions had a strong one-sided read, infer
            # that bias for the penalty calculation. Addresses the
            # 2026-04-23 GOOG 12:51 pullback_long case (current bias None
            # after 10 SHORT-biased bars, lost $22 to counter-trend).
            trailing_enabled = bool(self.params.get("trailing_bias_enabled", True))
            trailing_lookback = max(3, int(self.params.get("trailing_bias_lookback", 10)))
            trailing_threshold = float(self.params.get("trailing_bias_majority_threshold", 0.7))
            effective_bias = live_bias
            if effective_bias is None and trailing_enabled and respect_bias:
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
            # Record the raw live bias (not the trailing-inferred fallback)
            # so the trailing memory reflects what the LIVE day_strength
            # has actually been doing across recent cycles.
            hist = self._recent_directional_bias.get(c.symbol)
            if hist is None or hist.maxlen != trailing_lookback:
                existing = list(hist) if hist is not None else []
                hist = deque(existing[-trailing_lookback:], maxlen=trailing_lookback)
                self._recent_directional_bias[c.symbol] = hist
            hist.append(live_bias)

            # Both sides are always evaluated under soft bias. The penalty
            # applied per-side inside the loop below filters out weak
            # counter-bias setups. Shorts can still be globally disabled
            # via ``allow_short = False``.
            preferred_sides = [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]

            best_signal: Signal | None = None
            fail_reasons: list[str] = []

            # Score thresholds — read once, used for both sides' qualifier
            # filtering. ``min_score_gap`` is intentionally NOT read: the
            # primary-vs-fallback selection paths that used it are collapsed
            # into the flat score-ordered build_queue, so the gap no longer
            # gates anything. The param remains in user configs for
            # backwards compat but is silently ignored.
            min_trend = float(self.params.get("min_trend_score", 4.0))
            min_pullback = float(self.params.get("min_pullback_score", 4.0))
            min_range = float(self.params.get("min_range_score", 3.5))
            min_vol_squeeze = float(self.params.get("min_vol_squeeze_score", 4.0))
            min_momentum = float(self.params.get("min_momentum_score", 4.0))
            min_sr_scalp = float(self.params.get("min_sr_scalp_score", 3.5))

            # tech_ctx is built ONCE per candidate (per-frame @lru_cache makes
            # repeated calls O(1)). Used by vol_squeeze scoring AND by
            # _volatility_widening_factor (Tier 2a) for stop widening. Pulled
            # out of the per-side loop since both sides see the same frame
            # state and the same volatility regime.
            tech_ctx_for_candidate = self._technical_context(frame)
            # Pass ``now_t`` so the time-of-day arm of the widening factor
            # can fire during the early-session high-vol window (typically
            # 09:35-10:30 ET). Catches absolute volatility that Tier 2a's
            # relative-expansion check would miss.
            vol_widening = self._volatility_widening_factor(tech_ctx_for_candidate, current_time=now_t)

            # Pass 1 — score each side independently, apply bias penalty,
            # build the per-side ``build_order`` list of qualifying regimes.
            # No single "winner" is picked here; pass 2 flattens all sides'
            # build_orders into a cross-side queue sorted by post-penalty
            # score. The deferred-build design lets the highest-scoring
            # qualifying (side, regime) pair go first regardless of which
            # side it's on — no regime blocks another within OR across sides.
            side_decisions: list[tuple[Side, dict[str, Any]]] = []
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
                # vol_squeeze and momentum: scoring methods read live frame
                # data — vol_squeeze derives compression from session bars +
                # BB-width; momentum derives day_strength from the session
                # open. ``momentum`` was renamed from ``momentum_close`` and
                # widened from afternoon-only to post-ORB through close.
                vol_squeeze_score = (
                    self._score_vol_squeeze(side, close, vwap, ema9, ema20, atr, frame, tech_ctx_for_candidate)
                    if "vol_squeeze" in allowed_regimes else 0.0
                )
                momentum_score = (
                    self._score_momentum(side, close, vwap, ema9, ema20, ret15, frame)
                    if "momentum" in allowed_regimes else 0.0
                )
                # sr_scalp: permissive bar-character scoring. The hard
                # constraint (HTF zone distance + proximity to entry level)
                # runs at build time in _build_sr_scalp_signal.
                sr_scalp_score = (
                    self._score_sr_scalp(side, close, vwap, ema9, ema20, atr, adx, frame)
                    if "sr_scalp" in allowed_regimes else 0.0
                )

                # Soft-bias penalty (Fix A refactored 2026-05-12). See
                # ``_bias_penalty`` docstring for rationale + worked examples.
                bias_penalty = self._bias_penalty(side, day_strength, effective_bias, respect_bias)
                if bias_penalty > 0.0:
                    trend_score = max(0.0, trend_score - bias_penalty)
                    pullback_score = max(0.0, pullback_score - bias_penalty)
                    range_score = max(0.0, range_score - bias_penalty)
                    vol_squeeze_score = max(0.0, vol_squeeze_score - bias_penalty)
                    momentum_score = max(0.0, momentum_score - bias_penalty)
                    sr_scalp_score = max(0.0, sr_scalp_score - bias_penalty)

                scores = {
                    "trend": trend_score,
                    "pullback": pullback_score,
                    "range": range_score,
                    "vol_squeeze": vol_squeeze_score,
                    "momentum": momentum_score,
                    "sr_scalp": sr_scalp_score,
                }

                # Per-side BUILD ORDER: list of qualifying regimes in score
                # order. A regime qualifies if it's in allowed_regimes AND
                # its post-penalty score meets its own min_*_score threshold.
                # The build phase iterates ACROSS sides AND regimes in
                # cross-side score order so no regime blocks another (within
                # OR across sides). Both sides' qualifiers compete in the
                # same flat queue.
                #
                # ``min_score_gap`` config param is silently ignored — the
                # primary-vs-fallback selection paths it used to gate are
                # collapsed into the unified score-ordered queue.
                thresholds = {
                    "trend": min_trend,
                    "pullback": min_pullback,
                    "range": min_range,
                    "vol_squeeze": min_vol_squeeze,
                    "momentum": min_momentum,
                    "sr_scalp": min_sr_scalp,
                }
                build_order: list[tuple[str, float]] = []
                for regime_name, regime_score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
                    if regime_name not in allowed_regimes:
                        continue
                    if regime_score >= thresholds.get(regime_name, float("inf")):
                        build_order.append((regime_name, regime_score))

                side_decisions.append((side, {
                    "build_order": build_order,
                    "scores": scores,
                    "index_ok": index_ok,
                    "bias_penalty": bias_penalty,
                    "trend_score": trend_score,
                    "pullback_score": pullback_score,
                    "range_score": range_score,
                    "vol_squeeze_score": vol_squeeze_score,
                    "momentum_score": momentum_score,
                    "sr_scalp_score": sr_scalp_score,
                }))

            # Pass 2 — record fail reasons for sides with no qualifying
            # regimes, then flatten the remaining (side, regime) pairs into
            # a single cross-side build queue sorted by post-penalty score
            # descending. First successful build wins.
            #
            # The flat queue means a high-scoring SHORT range CAN beat a
            # low-scoring LONG pullback even if LONG's top regime had a
            # higher score (because trend's build failed). Truly "regimes
            # don't block each other" — within OR across sides.
            #
            # Skip-summary buckets:
            #   * ``<side>_unqualified_no_qualifying_regime(...)`` — no
            #     regime cleared its min-score floor for this side. Signal
            #     was never attempted.
            #   * ``<side>_build_failed_<regime>_<reason>`` — regime cleared
            #     its floor and the build method was invoked, but a hard
            #     gate inside the builder rejected. Signal was attempted.
            # Differentiating these matters for tuning: the first wants
            # looser score thresholds; the second wants looser hard gates.
            build_queue: list[tuple[Side, str, float, dict[str, Any]]] = []
            for side, decision in side_decisions:
                if not decision["build_order"]:
                    penalty_suffix = (
                        f",bias_pen={decision['bias_penalty']:.2f}"
                        if decision["bias_penalty"] > 0.0 else ""
                    )
                    fail_reasons.append(
                        f"{side.value.lower()}_unqualified_no_qualifying_regime("
                        f"trend={decision['trend_score']:.1f},"
                        f"pb={decision['pullback_score']:.1f},"
                        f"range={decision['range_score']:.1f},"
                        f"squeeze={decision['vol_squeeze_score']:.1f},"
                        f"mom={decision['momentum_score']:.1f},"
                        f"sr_scalp={decision['sr_scalp_score']:.1f}{penalty_suffix})"
                    )
                    continue
                for regime_name, regime_score in decision["build_order"]:
                    build_queue.append((side, regime_name, regime_score, decision))

            # Stable sort by score desc — ties default to preferred_sides
            # insertion order (LONG before SHORT) since side_decisions was
            # built in that order.
            build_queue.sort(key=lambda item: item[2], reverse=True)

            winning_decision: dict[str, Any] | None = None
            winning_regime: str | None = None
            for side, regime_name, regime_score, decision in build_queue:
                index_ok = decision["index_ok"]

                # Index confirmation for trend/pullback/vol_squeeze/momentum —
                # these need a market-aligned tape. Range AND sr_scalp are
                # exempt — both are mean-reversion theses where a divergent
                # index reads as "the index doesn't dictate intra-symbol
                # rotation between levels." Skipped during ORB window when
                # orb_bypass_index_confirmation is true. Index failure on
                # one regime falls through to the next regime in the queue.
                if regime_name in {"trend", "pullback", "vol_squeeze", "momentum"} and not index_ok and not orb_index_bypass:
                    fail_reasons.append(
                        f"{side.value.lower()}_build_failed_{regime_name}_index_not_confirmed"
                    )
                    continue

                sig = None
                if regime_name == "trend":
                    sig = self._build_trend_signal(c, side, close, atr, ltf, frame, regime_score, data, vol_widening=vol_widening)
                elif regime_name == "pullback":
                    sig = self._build_pullback_signal(c, side, close, atr, ltf, frame, regime_score, data, vol_widening=vol_widening)
                elif regime_name == "range":
                    sig = self._build_range_signal(c, side, close, atr, frame, regime_score, data, vol_widening=vol_widening)
                elif regime_name == "vol_squeeze":
                    sig = self._build_vol_squeeze_signal(c, side, close, atr, frame, regime_score, data, vol_widening=vol_widening)
                elif regime_name == "momentum":
                    sig = self._build_momentum_signal(c, side, close, atr, frame, regime_score, data, vol_widening=vol_widening)
                elif regime_name == "sr_scalp":
                    sig = self._build_sr_scalp_signal(c, side, close, atr, frame, regime_score, data, vol_widening=vol_widening)

                if sig is not None:
                    # Tier 3b: on high-conviction days, loosen the
                    # peak-giveback threshold so a 2R+ runner doesn't get
                    # cut by a normal 50% retracement. Override is stamped
                    # per-trade based on day_strength at ENTRY; risk.py
                    # reads it from position.metadata at management time.
                    # Falls back to the global config default when not set.
                    if day_strength is not None:
                        conv_threshold = float(self.params.get("peak_giveback_high_conviction_day_strength_pct", 2.0))
                        if abs(day_strength) >= conv_threshold:
                            override_r = float(self.params.get("peak_giveback_high_conviction_min_r", 2.0))
                            if isinstance(sig.metadata, dict):
                                sig.metadata["peak_giveback_min_r_override"] = override_r
                                sig.metadata["peak_giveback_high_conviction_day_strength"] = round(float(day_strength), 4)
                    # Stamp the volatility widening factor for post-mortem.
                    # Always stamped when Tier 2a is enabled (even when the
                    # factor is 1.0 — disambiguates "feature disabled" from
                    # "feature enabled but inactive this cycle").
                    if bool(self.params.get("atr_aware_stop_enabled", True)) and isinstance(sig.metadata, dict):
                        sig.metadata["vol_widening_factor"] = round(float(vol_widening), 4)
                    # Stamp the per-sector confirmation indices used at
                    # entry. ``position_manager._adaptive_ladder_management``
                    # re-checks these at target-hit time so a sector ETF
                    # that has flipped against the trade can short-circuit
                    # the multi-bar zone-flip wait (target exits at the
                    # rung price instead of riding through a sector
                    # reversal). Read in
                    # ``_ladder_indices_still_aligned``.
                    if isinstance(sig.metadata, dict):
                        sig.metadata["confirmation_indices"] = list(self._indices_for_symbol(c.symbol))
                    best_signal = sig
                    winning_decision = decision
                    winning_regime = regime_name
                    break

                # Build attempted but rejected by a hard gate inside the
                # builder. ``_set_build_failure`` already side-prefixes most
                # rejection tags (long_/short_); only prefix here when the
                # tag isn't already side-tagged to avoid stuttered buckets
                # like ``long_long_below_support_zone`` in the EOD summary.
                failure = self._consume_build_failure(c.symbol, regime_name) or f"{regime_name}_signal_build_failed"
                side_tag = side.value.lower()
                if failure.startswith(("long_", "short_")):
                    fail_reasons.append(f"build_failed_{failure}")
                else:
                    fail_reasons.append(f"{side_tag}_build_failed_{failure}")

            if best_signal is not None:
                out.append(best_signal)
                # Stamp the soft-bias penalty value on the success path too
                # so post-mortem can see whether a winner was nearly killed
                # by bias drag. Sourced from the winning side's decision.
                detail_payload: dict[str, Any] = {}
                if winning_decision is not None:
                    bp = float(winning_decision.get("bias_penalty", 0.0) or 0.0)
                    if bp > 0.0:
                        detail_payload["bias_pen"] = round(bp, 4)
                    if winning_regime is not None:
                        detail_payload["regime"] = winning_regime
                        scores_dict = winning_decision.get("scores") or {}
                        detail_payload["score"] = round(float(scores_dict.get(winning_regime, 0.0)), 4)
                self._record_entry_decision(
                    c.symbol, "signal", [best_signal.reason],
                    details=detail_payload or None,
                )
            else:
                self._record_entry_decision(c.symbol, "skipped", fail_reasons or ["no_setup"])
        return out

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------
    def should_force_flatten(self, position: Position) -> bool:
        return self._configurable_stock_force_flatten(position)
