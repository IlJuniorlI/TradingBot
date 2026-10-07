# SPDX-License-Identifier: MIT
"""The ``sr_scalp`` regime: scorer, level pierce and builder."""
from __future__ import annotations

import pandas as pd

from ....bars import bar_wick_fractions, same_day_mask
from ....models import Candidate, Side, Signal
from ....numeric import safe_float
from ....support_resistance import role_level
from .... import sessions


class SrScalpRegimeMixin:
    """S/R scalp: a holding level or a confirmed flip, riding to the next
    rung. Mixed into ``TopTierAdaptiveStrategy`` (``strategy.py``), whose
    ``BaseStrategy`` supplies ``params``, ``config`` and the contexts."""

    def _score_sr_scalp(self, side: Side, close: float, atr: float,
                        frame: pd.DataFrame, sr_ctx, vol_scale: float = 1.0) -> float:
        """Score the HTF S/R scalp on ACTUAL level interaction (2026-05-29
        redesign).

        Thesis: enter LONG at/just off a support zone that is HOLDING (price
        above it, not broken through), OR on the CONTINUATION after a
        resistance zone has confirm-flipped to support and is holding (close
        back above the broken resistance) — then ride toward the next
        resistance up the ladder. SHORT mirrors: at/off a holding resistance,
        or continuation after a confirmed support break, riding down to the
        next support.

        The previous scorer measured chop character (wick / VWAP-neutral /
        EMA-neutral / low-ADX), which is UNCORRELATED with the level geometry
        the builder actually gates on — so the regime effectively never
        qualified (max score 2.5 vs the 3.0 threshold across the entire
        2026-05-29 RTH session). This scorer measures the SAME geometry the
        builder enforces, so a genuine support-hold / flip-continuation
        scores high enough to win the regime auction; the hard zone-gap +
        proximity + not-broken-through gates still run at build time.

        Components (max 5.0):
          * +0.5  base (regime in play)
          * +2.0  price is at/just off the HOLDING entry-side zone — the
                  nearest support (LONG) / resistance (SHORT) or the pending
                  one whose zone price has wicked into, OR a confirmed
                  flip level (LONG: close just above broken_resistance ;
                  SHORT: close just below broken_support). No level
                  interaction => base only (won't qualify).
          * +0.5  continuation bonus when the proximity is a confirmed flip
                  rather than a fresh nearest-level zone
          * +1.0  bounce/rejection bar character (LONG: lower wick >= 0.30 ;
                  SHORT: upper wick >= 0.30)
          * +1.0  room to ride: inner gap from the nearest (or pending)
                  entry-side zone to the builder's target zone (the
                  nearest level playing the opposite role at the close,
                  ``role_level``) clears the build's required distance (a
                  ladder exists to ride to). Only the target end is the
                  builder's: its gap starts at its floor / ceiling, which
                  can be a confirmed flip nearer the target.
        """
        if sr_ctx is None or atr <= 0 or close <= 0:
            return 0.0
        sup = getattr(sr_ctx, "nearest_support", None)
        res = getattr(sr_ctx, "nearest_resistance", None)
        bres = getattr(sr_ctx, "broken_resistance", None)
        bsup = getattr(sr_ctx, "broken_support", None)
        psup = getattr(sr_ctx, "pending_support", None)
        pres = getattr(sr_ctx, "pending_resistance", None)
        sup_px = float(getattr(sup, "price", 0.0) or 0.0) if sup is not None else 0.0
        res_px = float(getattr(res, "price", 0.0) or 0.0) if res is not None else 0.0
        bres_px = float(getattr(bres, "price", 0.0) or 0.0) if bres is not None else 0.0
        bsup_px = float(getattr(bsup, "price", 0.0) or 0.0) if bsup is not None else 0.0
        psup_px = float(getattr(psup, "price", 0.0) or 0.0) if psup is not None else 0.0
        pres_px = float(getattr(pres, "price", 0.0) or 0.0) if pres is not None else 0.0

        zone_hw = max(
            float(self.params.get("zone_atr_mult", 0.20)) * atr,
            close * float(self.params.get("zone_pct", 0.0015)),
            0.01,
        )
        prox = float(self.params.get("sr_scalp_max_distance_from_zone_atr", 0.5)) * atr

        def _near_support(level_px: float) -> bool:
            return level_px > 0.0 and (level_px - zone_hw) < close <= (level_px + zone_hw + prox)

        def _near_resistance(level_px: float) -> bool:
            return level_px > 0.0 and (level_px - zone_hw - prox) <= close < (level_px + zone_hw)

        score = 0.5
        proximity_hit = False
        flip_hit = False
        if side == Side.LONG:
            # The pending support (dipped under, loss unconfirmed) is the zone
            # a bounce wicks into; see _build_sr_scalp_signal.
            if _near_support(sup_px) or _near_support(psup_px):
                proximity_hit = True
            if 0.0 < bres_px < close and _near_support(bres_px):
                flip_hit = True
        else:
            if _near_resistance(res_px) or _near_resistance(pres_px):
                proximity_hit = True
            if bsup_px > 0.0 and close < bsup_px and _near_resistance(bsup_px):
                flip_hit = True

        if not (proximity_hit or flip_hit):
            return score
        score += 2.0
        if flip_hit and not proximity_hit:
            score += 0.5

        upper_wick, lower_wick, _body, bar_range = bar_wick_fractions(frame)
        if bar_range > 0:
            wick = lower_wick if side == Side.LONG else upper_wick
            if wick >= 0.30:
                score += 1.0

        # Room is measured from the zone the setup leans on: a pending level
        # that price is inside of sits nearer the target than the next level
        # beyond it, so it bounds the ride, as in the builder. The ride ends
        # at the builder's target, the nearest level playing the opposite
        # role at this close (``role_level``): a confirmed flip between the
        # close and nearest_* ends it there (2026-10-07). Measured to
        # nearest_* past it, the term paid for room the builder then refused
        # as htf_zones_too_close. Only that end is shared: the room starts
        # at the support (LONG; the resistance for a SHORT) or the pending
        # one, while the builder's gap starts at its floor (ceiling), which
        # is a reclaimed resistance (lost support) when that one is in
        # proximity and nearer the target, so the term can still pay for a
        # gap the builder refuses.
        target = role_level(sr_ctx, "resistance" if side == Side.LONG else "support", price=close)
        target_px = float(getattr(target, "price", 0.0) or 0.0) if target is not None else 0.0
        if side == Side.LONG:
            lower_px, upper_px = (psup_px if _near_support(psup_px) else sup_px), target_px
        else:
            lower_px, upper_px = target_px, (pres_px if _near_resistance(pres_px) else res_px)
        if 0.0 < lower_px < upper_px:
            inner_gap = (upper_px - zone_hw) - (lower_px + zone_hw)
            required_gap = max(
                self._pct_param("sr_scalp_min_distance_pct", 0.008, vol_scale) * close,
                float(self.params.get("sr_scalp_min_distance_atr", 2.5)) * atr,
            )
            if inner_gap >= required_gap:
                score += 1.0
        return score

    @staticmethod
    def _level_pierce(side: Side, bars: pd.DataFrame, level: float) -> float:
        """How far price has pushed THROUGH *level* while it held the role this
        side leans on it for -- support under a LONG, resistance over a SHORT.

        That role starts at the first bar in *bars* that CLOSES on the entry
        side of the level (above it for LONG, below for SHORT). If that is not
        the window's first bar, the bar that crossed is the flip itself and is
        skipped too: its extreme is the approach from the far side. Everything
        before it is price on the far side of a level that was playing the
        OPPOSITE role -- a resistance that later flipped to support -- and says
        nothing about whether it holds now.

        Measured from the start of the window instead (2026-09-22, first cut),
        a fresh flip's whole approach from below counted as "pierces": the stop
        went under the pre-breakout lows and FLIP-CONTINUATION -- setup B of
        ``_build_sr_scalp_signal``, a confirmed-flipped level by definition --
        died on ``stop_floor_kills_rr`` whenever the break was inside the
        lookback.

        After the role starts, every excursion counts, closes included: a level
        that has since been closed through and reclaimed is exactly the chop
        the floor exists to price in (META 2026-07-28). If no bar closes on the
        entry side, price never held the level and the whole window counts.
        """
        if bars is None or len(bars) < 2 or level <= 0.0:
            return 0.0
        closes = bars["close"].astype(float)
        held = (closes > level) if side == Side.LONG else (closes < level)
        if bool(held.any()):
            first = int(held.to_numpy().argmax())
            active = bars.iloc[first if first == 0 else first + 1:]
        else:
            active = bars
        if active.empty:
            return 0.0
        if side == Side.LONG:
            return max(0.0, level - safe_float(active["low"].min(), level))
        return max(0.0, safe_float(active["high"].max(), level) - level)

    def _build_sr_scalp_signal(self, c: Candidate, side: Side, close: float, atr: float,
                               frame: pd.DataFrame, regime_score: float,
                               data=None, vol_widening: float = 1.0, vol_scale: float = 1.0) -> Signal | None:
        """Build an HTF S/R scalp signal (2026-05-29 redesign).

        Two LONG setups (SHORT mirrors), both riding to the next level:
          A. BOUNCE — price at/just off a HOLDING nearest support zone,
             target the nearest level playing the resistance role above
             the close (the next rung up, or a lost support short of it).
          B. FLIP-CONTINUATION — price holding just above a confirmed-
             flipped resistance (``sr_ctx.broken_resistance``, now acting
             as support), the same target. SHORT uses ``broken_support``
             (a confirmed support break, now resistance).
        The higher (more immediate) of the two floors is used when both are
        in proximity. SHORT is the exact mirror with ceilings. The target
        level is the nearest one playing the opposite role at the close
        (``support_resistance.role_level``): ``nearest_resistance``, or a
        lost support (``broken_support``) between the close and it, which a
        LONG's target cannot ride past (since 2026-10-07; mirror for SHORT).
        The scorer's room to ride ends at the same level (from its own
        start: see ``_score_sr_scalp``).

        Uses the bot's existing S/R machinery — NO strategy-local level
        creation. Level prices come from ``sr_ctx.nearest_support`` /
        ``nearest_resistance`` / ``broken_resistance`` / ``broken_support``
        and, for a zone price has wicked into past its level while the flip
        is unconfirmed, ``pending_support`` / ``pending_resistance``;
        zone bands from ``zone_atr_mult*atr`` / ``zone_pct*close`` (max);
        stop nudge from ``sr_ctx.level_buffer × vol_widening``.

        Build-time gates (per side):
          1. An entry-side floor (LONG) / ceiling (SHORT) exists in
             proximity — either the nearest level or a confirmed flip level
             (within ``sr_scalp_max_distance_from_zone_atr*atr`` of its edge).
          2. A target level exists in the trade direction (LONG: a
             resistance-role level ABOVE close; SHORT: a support-role level
             BELOW close).
          3. Inner gap from the floor/ceiling zone to the target level clears
             BOTH the % floor (``sr_scalp_min_distance_pct*close``) and the
             ATR floor (``sr_scalp_min_distance_atr*atr``).
          4. Price hasn't broken through the floor/ceiling zone (holding,
             not breaking).

        Stop = floor_zone_lower − buffer (LONG) / ceiling_zone_upper + buffer
        (SHORT). Target = the target level zone's inner edge ∓ buffer —
        matching the bot's structural-exit conventions everywhere else.
        """
        sr_ctx = self._sr_context(c.symbol, frame, data)
        sup = getattr(sr_ctx, "nearest_support", None)
        res = getattr(sr_ctx, "nearest_resistance", None)
        bres = getattr(sr_ctx, "broken_resistance", None)
        bsup = getattr(sr_ctx, "broken_support", None)
        psup = getattr(sr_ctx, "pending_support", None)
        pres = getattr(sr_ctx, "pending_resistance", None)
        sup_px = float(getattr(sup, "price", 0.0) or 0.0) if sup is not None else 0.0
        res_px = float(getattr(res, "price", 0.0) or 0.0) if res is not None else 0.0
        bres_px = float(getattr(bres, "price", 0.0) or 0.0) if bres is not None else 0.0
        bsup_px = float(getattr(bsup, "price", 0.0) or 0.0) if bsup is not None else 0.0
        psup_px = float(getattr(psup, "price", 0.0) or 0.0) if psup is not None else 0.0
        pres_px = float(getattr(pres, "price", 0.0) or 0.0) if pres is not None else 0.0

        # Zone band half-width — bot's existing zone construction (same
        # formula as the dashboard's key_level_zones via
        # dashboard_level_context_spec). Reads ``zone_atr_mult`` / ``zone_pct``.
        zone_half_width = max(
            float(self.params.get("zone_atr_mult", 0.20)) * atr,
            close * float(self.params.get("zone_pct", 0.0015)),
            0.01,
        )
        proximity_buffer = float(self.params.get("sr_scalp_max_distance_from_zone_atr", 0.5)) * atr
        # Stop nudge — bot's existing ``sr_ctx.level_buffer`` (same buffer the
        # shared entry stage's S/R stop refinement uses). Scales with
        # vol_widening (Tier 2a).
        level_buffer = float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0) * vol_widening * self._side_stop_buffer_mult(side)
        if level_buffer <= 0.0:
            level_buffer = max(atr * 0.05, 0.01) * vol_widening
        # Inner-gap floor (tradeable distance to the next rung). Max of the
        # % and ATR floors, same as before.
        required_gap = max(
            self._pct_param("sr_scalp_min_distance_pct", 0.008, vol_scale) * close,
            float(self.params.get("sr_scalp_min_distance_atr", 2.5)) * atr,
        )
        # The target: the nearest level playing the opposite role at this
        # close (``role_level``), a confirmed flip between the close and
        # nearest_* included (2026-10-07); a refusal that measured to a flip
        # names it.
        target_level = role_level(sr_ctx, "resistance" if side == Side.LONG else "support", price=close)
        target_px = float(getattr(target_level, "price", 0.0) or 0.0) if target_level is not None else 0.0
        target_name = "res" if side == Side.LONG else "sup"
        target_flip_detail = ""
        if target_level is not None and target_level is (bsup if side == Side.LONG else bres):
            target_name = "broken_support" if side == Side.LONG else "broken_resistance"
            target_flip_detail = f",{target_name}={target_px:.4f}"

        if side == Side.LONG:
            # Entry-side floor: the nearest support (mean-reversion bounce) OR
            # a confirmed-flipped resistance now acting as support
            # (continuation). Prefer the higher (more immediate) floor. The
            # proximity bounds below enforce "holding" (close stays above the
            # floor zone low), so no separate broken-through guard is needed.
            #
            # A bounce that wicks into the LOWER half of the support zone has
            # crossed the level, and until the loss confirms the builder
            # reports it as ``pending_support`` (above close), never as
            # nearest_support (2026-09-23). Reading nearest_support alone made
            # the zone's lower half unreachable: the test below compared close
            # with the next support DOWN and rejected the setup exactly when
            # price was testing the level.
            floor_px = 0.0
            for level_px in (sup_px, psup_px):
                if level_px > floor_px and (level_px - zone_half_width) < close <= (level_px + zone_half_width + proximity_buffer):
                    floor_px = level_px
            if 0.0 < bres_px < close <= (bres_px + zone_half_width + proximity_buffer) and bres_px > floor_px:
                floor_px = bres_px
            if floor_px <= 0.0:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"long_no_holding_support_or_flip(close={close:.4f},sup={sup_px:.4f},flipped_res={bres_px:.4f})",
                )
                return None
            # Target = the nearest resistance ABOVE close (the next rung up
            # the ladder), or a lost support between price and it, which
            # acts as resistance (``role_level``, 2026-10-07).
            if target_px <= close:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"long_no_resistance_above({target_name}={target_px:.4f}<=close={close:.4f})",
                )
                return None
            inner_gap = (target_px - zone_half_width) - (floor_px + zone_half_width)
            if inner_gap < required_gap:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"htf_zones_too_close(inner_gap={inner_gap:.4f}<{required_gap:.4f}{target_flip_detail})",
                )
                return None
            entry_level = floor_px
            stop = (floor_px - zone_half_width) - level_buffer
            target = (target_px - zone_half_width) - level_buffer
        else:
            # Entry-side ceiling: the nearest resistance (rejection) OR a
            # confirmed-flipped support now acting as resistance
            # (continuation). Prefer the lower (more immediate) ceiling. The
            # proximity bounds below enforce "holding" (close stays below the
            # ceiling zone high), so no separate broken-through guard is needed.
            # A rejection that pokes into the UPPER half of the resistance
            # zone reads the ``pending_resistance`` (below close), mirroring
            # the LONG floor.
            ceil_px = 0.0
            for level_px in (res_px, pres_px):
                if level_px > 0.0 and (ceil_px <= 0.0 or level_px < ceil_px) and (level_px - zone_half_width - proximity_buffer) <= close < (level_px + zone_half_width):
                    ceil_px = level_px
            if bsup_px > 0.0 and (bsup_px - zone_half_width - proximity_buffer) <= close < bsup_px and (ceil_px <= 0.0 or bsup_px < ceil_px):
                ceil_px = bsup_px
            if ceil_px <= 0.0:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"short_no_holding_resistance_or_flip(close={close:.4f},res={res_px:.4f},flipped_sup={bsup_px:.4f})",
                )
                return None
            # Target = the nearest support BELOW close (the next rung down
            # the ladder), or a reclaimed resistance between it and price.
            if target_px <= 0.0 or target_px >= close:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"short_no_support_below({target_name}={target_px:.4f}>=close={close:.4f})",
                )
                return None
            inner_gap = (ceil_px - zone_half_width) - (target_px + zone_half_width)
            if inner_gap < required_gap:
                self._set_build_failure(
                    c.symbol, "sr_scalp",
                    f"htf_zones_too_close(inner_gap={inner_gap:.4f}<{required_gap:.4f}{target_flip_detail})",
                )
                return None
            entry_level = ceil_px
            stop = (ceil_px + zone_half_width) + level_buffer
            target = (target_px + zone_half_width) + level_buffer

        # Noise floor on the scalp stop: it must sit beyond the deepest recent
        # violation of the level it leans on, not beyond a flat ATR multiple.
        #
        # 2026-07-29 installed a flat ``sr_scalp_min_stop_atr_mult * atr``
        # floor (2.5) after 07-28 META — three shorts into a band the tape
        # chopped 11.9 ATR through, stops 1.09-2.70 ATR away, 63% of the
        # window's bars trading through them. The diagnosis was right and the
        # instrument was wrong. The geometric stop above is at most
        # ``proximity + zone_half_width + level_buffer`` from entry, which on
        # the shipped 0.4 / 0.2 / ~0.3 is about 0.9 ATR — so a 2.5 ATR flat
        # floor bound on EVERY setup, not the noisy ones. The level picked the
        # direction and was then thrown away on the risk side, which is not
        # what an S/R scalp is: the premise is a stop just past a level that is
        # HOLDING. It also made the advertised reward floor a fiction —
        # ``sr_scalp_min_distance_atr`` says 2.0 while a flat 2.5 ATR risk
        # needs 2.4-3.2 ATR of gap to clear ``min_target_rr``, so a setup at
        # the documented floor could never build. Two floors, one of them
        # silently dominant, is the shape that also hid inside ORB.
        #
        # What actually distinguishes META from a clean bounce is whether the
        # level has been HOLDING. ``pierce`` measures exactly that: how far
        # price has pushed through this level over the lookback, counted from
        # when it took the role it plays now (``_level_pierce`` -- a fresh
        # flip's approach from the far side is not a breach). A level never
        # breached leaves the geometric stop alone; a level being cut through
        # every few bars pushes the stop out past the breaches, and since the
        # target IS the opposing zone and cannot stretch, the R:R check below
        # then rejects the setup — the right answer for a level that is not
        # really there. ``sr_scalp_min_stop_atr_mult`` stays as a small
        # absolute backstop for a degenerate, never-touched level.
        noise_lookback = max(2, int(self.params.get("sr_scalp_noise_lookback_bars", 20)))
        noise_frame = frame[same_day_mask(frame, sessions.now_et().date())].tail(noise_lookback)
        pierce = self._level_pierce(side, noise_frame, entry_level)
        min_stop_atr = float(self.params.get("sr_scalp_min_stop_atr_mult", 0.5))
        atr_floor = min_stop_atr * atr if (min_stop_atr > 0 and atr > 0) else 0.0
        if side == Side.LONG:
            pierce_stop = entry_level - pierce - level_buffer
            floored = min(stop, pierce_stop, close - atr_floor if atr_floor > 0 else stop)
        else:
            pierce_stop = entry_level + pierce + level_buffer
            floored = max(stop, pierce_stop, close + atr_floor if atr_floor > 0 else stop)
        # Widening the stop costs R:R, and sr_scalp cannot extend its reward to
        # compensate — the target IS the opposing zone. When the zone gap can
        # no longer pay for the tape's noise the setup simply isn't tradeable,
        # so reject instead of taking a sub-floor R:R: the shared entry
        # stage's raw R:R gate, asked for only when the floor moved the stop
        # (2026-09-24: it was a builder-local min_target_rr read; the refusal
        # `<side>_stop_floor_kills_rr(close,target,reward,risk)` no longer
        # names what bound the stop -- a pierce or the ATR backstop).
        raw_rr_gate = "stop_floor_kills_rr" if floored != stop else None
        stop = floored

        return self._finalize_signal(c, side, close, stop, target, "sr_scalp", regime_score, frame, data,
                                     vol_scale=vol_scale, raw_rr_gate=raw_rr_gate)
