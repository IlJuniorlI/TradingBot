# SPDX-License-Identifier: MIT
"""Time-of-day schedule: which regimes may trade now.

The ORB window (``_orb_range_minutes``, ``_orb_range_end``,
``_in_orb_window``, checked once at construction by
``_validate_orb_window``), the per-window regime sets of
``_allowed_regimes``, and the extended-hours tradable list.
"""
from __future__ import annotations

from ...bars import rth_open_plus
from ...indicators import get_session_indicator_window
from ...sessions import EQUITY_RTH_OPEN, is_time_in_window, parse_hhmm


class ScheduleMixin:
    """The ORB window and the per-window regime sets. Mixed into
    ``TopTierAdaptiveStrategy`` (``strategy.py``), whose ``BaseStrategy``
    supplies ``params``, ``config`` and the contexts."""

    def _validate_orb_window(self) -> None:
        """Fail loudly when the opening range cannot finish before the ORB
        window is meant to close.

        ``orb_range_minutes`` and ``orb_end_time`` are independent knobs with
        an implicit ordering between them, and nothing checked it. Violating
        it does not error -- it silently shrinks the window to nothing:

            orb_range_minutes=15 -> 20 tradeable minutes
                            =30 ->  5
                            =34 ->  1
                            =35 ->  NEVER, with no error and no log line

        A regime that quietly stops existing is the worst way for a config
        mistake to present, so this raises at construction instead. Only
        checked when the regime is ON; with ``disable_orb_regime`` the knobs
        are inert and an odd pair is harmless.
        """
        if bool(self.params.get("disable_orb_regime", False)):
            return
        try:
            range_end = parse_hhmm(self._orb_range_end())
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"top_tier_adaptive: cannot parse the ORB window "
                f"(orb_range_minutes={self.params.get('orb_range_minutes')!r}): {exc}"
            ) from exc
        # BaseStrategy.__init__ has already checked orb_end_time (time_params).
        orb_end = parse_hhmm(self.params.get("orb_end_time", "10:05"))
        if range_end >= orb_end:
            raise ValueError(
                f"top_tier_adaptive: the opening range finishes at "
                f"{range_end.strftime('%H:%M')} but orb_end_time is "
                f"{orb_end.strftime('%H:%M')}, so the ORB window would never "
                f"open. Lower orb_range_minutes "
                f"({self.params.get('orb_range_minutes', 15)}) or raise "
                f"orb_end_time, or set disable_orb_regime: true if the regime "
                f"is not wanted."
            )

    def _orb_range_minutes(self) -> int:
        """The opening range's length: ``orb_range_minutes``, at least 1."""
        return max(1, int(self.params.get("orb_range_minutes", 15)))

    def _orb_range_end(self) -> str:
        """HH:MM at which today's opening range finishes forming."""
        return rth_open_plus(self._orb_range_minutes()).strftime("%H:%M")

    def _in_orb_window(self, now_t) -> bool:
        """Is *now_t* inside the ORB window — the span where the ORB regime
        is the only regime allowed?

        The window is BOUNDED AT BOTH ENDS: ``[opening-range end, orb_end]``,
        and it does not exist at all when ``disable_orb_regime`` is set.

        This drives every ``orb_bypass_*`` flag (HTF bias, exhaustion, side
        decision, relative strength, screener bias) — gates that are relaxed
        on the argument that the opening range-break is its own directional
        proof. (The shared structure and S/R vetoes are skipped for the orb
        regime by the manifest exemption ``{orb: [structure, sr]}`` since
        2026-09-24; the regime exists only inside this window.) An
        open-ended "before orb_end" reading
        hands those bypasses to entries that have no ORB thesis behind them:
        with ``equity_session_indicator_window: extended`` the whole
        pre-market session qualifies, and with ``disable_orb_regime: true``
        (where there IS no ORB regime) so does every entry from the open
        through orb_end.
        """
        if bool(self.params.get("disable_orb_regime", False)):
            return False
        return is_time_in_window(now_t, self._orb_range_end(), self.params.get("orb_end_time", "10:05"))

    def _allowed_regimes(self, now_t) -> set[str]:
        """Return which regimes are allowed at the current time.

        Window cutoffs are param-driven (orb_end_time, midday_start_time,
        midday_end_time, afternoon_start_time, no_new_entries_after) — no
        hard-coded times.

        Eight regimes:
          - orb: true Opening Range Breakout. The opening range forms over
            the first ``orb_range_minutes`` of RTH (09:30 →); the ORB regime
            is the ONLY regime allowed in the window from range-end →
            orb_end_time, and it trades a break of that range (stop = opposite
            range edge, target = measured move). 09:30 → range-end is a
            no-entry zone (the range is still forming).
          - trend / pullback / range: primary scoring regimes
          - vol_squeeze: Bollinger-squeeze breakout. Allowed in the primary
            window (orb_end → midday_start), midday (2026-09-21 — the
            lunchtime tape IS the compression its thesis is about) and the
            afternoon (afternoon_start → no_new).
          - momentum: momentum-from-open continuation. Allowed post-ORB
            through close (orb_end → no_new). Includes midday because the
            ``momentum_min_day_strength`` hard gate filters out chop —
            stocks without enough intraday move score zero. Renamed from
            ``momentum_close`` 2026-05-12 when the window was widened from
            afternoon-only.
          - sr_scalp: HTF S/R mean-reversion scalp. Allowed from orb_end
            through close (orb_end → no_new) WHETHER OR NOT the ORB regime
            is on. Excluded from the opening window because morning chop
            near recent levels often breaks through; the build-time
            distance gate (``sr_scalp_min_distance_pct`` /
            ``sr_scalp_min_distance_atr``) rejects when the HTF zones are
            too close to be worth the round-trip.
          - vwap_reclaim: momentum-family reclaim of session VWAP after a
            flush. Allowed wherever momentum is.

        "Post-ORB" above for vol_squeeze and momentum assumes the ORB regime
        is on. With ``disable_orb_regime`` there is no ORB window: the primary
        window starts at 09:30, so they are available from the open and the
        strategy's ``entry_windows`` decide when entries actually begin.
        sr_scalp is the exception and still waits for orb_end.

        Each regime has its own opt-out knob via params:
          disable_trend_regime / disable_pullback_regime /
          disable_range_regime / disable_vol_squeeze_regime /
          disable_momentum_regime / disable_sr_scalp_regime /
          disable_vwap_reclaim_regime / disable_orb_regime.

        The ORB window (opening-range end → orb_end_time) has a separate
        whole-window opt-out (``disable_orb_window``) that skips it entirely
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
        # A momentum-family regime — available wherever momentum is, stripped
        # otherwise.
        #
        # This was `enable_vwap_reclaim_regime` (opt-IN, default off) when it
        # shipped, so adding it to the shared engine could not silently switch
        # it on for `SmallCapSqueezeStrategy`, which subclasses this class.
        # That protection is spent: small_cap_squeeze now opts in explicitly in
        # its own manifest, so nothing depended on the inverted default while
        # the odd polarity made it the one regime flag that reads backwards.
        vwap_reclaim_enabled = not bool(self.params.get("disable_vwap_reclaim_regime", False))

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
            if not vwap_reclaim_enabled:
                regimes.discard("vwap_reclaim")
            return regimes

        if now_t > parse_hhmm(no_new):
            return set()
        # ORB regime + its opening-range carve-out. ``disable_orb_regime``
        # (off by default) removes BOTH: no 09:30→range-end no-entry zone and
        # no ORB-only window — the open just trades the normal regime mix
        # continuously (the primary window starts at 09:30 instead of orb_end).
        # Used by momentum/squeeze strategies that want to trade the open
        # directly. Distinct from ``disable_orb_window`` (which keeps the
        # carve-out but skips entries until orb_end_time).
        orb_disabled = bool(self.params.get("disable_orb_regime", False))
        if not orb_disabled:
            orb_range_end = self._orb_range_end()
            # Opening range forms over the first ``orb_range_minutes`` of RTH
            # (09:30 →). NO entries while it forms — the true-ORB thesis waits
            # for the range, it does not trade the opening chaos. (In extended
            # mode pre-market 07:00-09:30 still trades via the fallthrough
            # below; 09:30→range-end is reserved for range formation.)
            # HALF-OPEN at the end. `is_time_in_window` is inclusive on both
            # sides, so a closed check here overlapped the ORB window by one
            # minute at `orb_range_end` -- and since this branch returns
            # first, that minute was silently unreachable: with the default
            # 15-minute range the window ran 09:46-10:05, not 09:45-10:05,
            # while `entry_windows` opened at 09:45. The range is built from
            # bars in [09:30, range_end), so at range_end it is complete and
            # the window should already be open.
            if EQUITY_RTH_OPEN <= now_t < parse_hhmm(orb_range_end):
                return set()
            if is_time_in_window(now_t, orb_range_end, orb_end):
                # ORB window: opening-range breakout only. Whole-window opt-out
                # via disable_orb_window (skip the open, start at orb_end_time).
                if not orb_window_enabled:
                    return set()
                return _filter({"orb"})
        # Primary window. With ORB enabled it starts at orb_end; with the ORB
        # regime disabled it starts at 09:30 so the open trades the normal mix.
        primary_start = EQUITY_RTH_OPEN if orb_disabled else orb_end
        if is_time_in_window(now_t, primary_start, midday_start):
            # Full regime mix including momentum + sr_scalp. The day_strength
            # gate filters momentum; the distance gate filters sr_scalp. Neither
            # blocks the trend / pullback / range / vol_squeeze regimes.
            regimes = {"trend", "pullback", "range", "vol_squeeze", "momentum", "sr_scalp", "vwap_reclaim"}
            if now_t <= parse_hhmm(orb_end):
                # sr_scalp waits for orb_end_time whether or not the ORB regime
                # is on. Its reason for skipping the opening window -- morning
                # chop near recent levels breaks through them -- is about the
                # tape, not about ORB, so dropping the ORB regime must not open
                # the window to it. Only reachable with ORB off (with it on the
                # branch above owns everything up to orb_end); `<=` matches that
                # branch's inclusive end, so the two modes agree to the second.
                regimes.discard("sr_scalp")
            return _filter(regimes)
        if is_time_in_window(now_t, midday_start, midday_end):
            # Midday: pullbacks remain the default fit for top-tier chop,
            # but momentum is allowed because day_strength >= threshold
            # implies a stock is genuinely trending despite the lunchtime
            # tape. sr_scalp is also allowed — midday's low-volatility
            # chop is often the cleanest scalp environment between HTF
            # zones (when the gap qualifies).
            #
            # vol_squeeze added 2026-09-21. Its thesis is compression
            # resolving into expansion, and the lunchtime tape IS the
            # compression — it was excluded from the one window where its
            # setup is most common. The measurement that prompted it: on
            # 2026-09-21, 51% of midday skips across the session's five
            # biggest movers (INTC/META/AMD/QCOM/NFLX, all +3% to +6%) were
            # "no regime qualified", and of the three regimes offered, NONE
            # came within half a point of its floor — pullback peaked at
            # 3.00 against 3.5, momentum at 3.00 against 4.0, vwap_reclaim
            # at 0.00. Ninety minutes a day in which nothing could fire.
            return _filter({"pullback", "momentum", "sr_scalp", "vwap_reclaim", "vol_squeeze"})
        if is_time_in_window(now_t, afternoon_start, no_new):
            # Range regime is included in afternoon by default because
            # afternoon tapes are often range-bound and forcing trend/pullback
            # entries there produces late-in-move longs. Range regime handles
            # mean-reversion at the extremes. Disable via
            # ``afternoon_include_range: false`` in params (or globally via
            # ``disable_range_regime: true``).
            if bool(self.params.get("afternoon_include_range", True)):
                regimes = {"trend", "pullback", "range", "vol_squeeze", "momentum", "sr_scalp", "vwap_reclaim"}
            else:
                regimes = {"trend", "pullback", "vol_squeeze", "momentum", "sr_scalp", "vwap_reclaim"}
            return _filter(regimes)
        if get_session_indicator_window() == "extended" and now_t <= parse_hhmm(no_new):
            # Extended-hours opt-in (07:00-20:00 trading): times not matched by
            # the RTH windows above — pre-market (<09:30) and any RTH gap — get
            # the full non-ORB regime mix. ORB stays RTH-anchored (range-end →
            # orb_end above). After-RTH (>16:00) is already covered by the afternoon
            # branch when no_new_entries_after is pushed past the close. The
            # per-regime score gates + the extended-hours universe gate in
            # _read_candidate self-filter; this just opens the time window.
            return _filter({"trend", "pullback", "range", "vol_squeeze", "momentum", "sr_scalp", "vwap_reclaim"})
        return set()

    def _extended_hours_tradable_set(self) -> set[str]:
        """Symbols eligible for pre/post-market entries (extended-indicator
        mode only). Empty => no extended-hours entries (RTH only)."""
        raw = self.params.get("extended_hours_tradable", []) or []
        return {str(s).upper().strip() for s in raw if str(s).strip()}
