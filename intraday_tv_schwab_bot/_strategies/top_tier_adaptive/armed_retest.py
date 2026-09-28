# SPDX-License-Identifier: MIT
"""Armed retest: the wait between a breakout qualifying and the entry.

``trend`` and ``momentum`` (``ARMED_RETEST_REGIMES``) do not enter the cycle
they qualify on. ``_armed_retest_verdict`` records the level they cleared and
enters when price comes back to it and closes through it again;
``_expired_armed_retests`` takes the market fallback when the wait runs out,
and ``_prune_armed_retests`` / ``_drop_armed_retests`` reap arms nothing will
use. The arms live on the strategy (``self._armed_retests``, set up in
``TopTierAdaptiveStrategy.__init__``); the level comes from the strategy's
``_breakout_reference``, the same one the trend and momentum builders check.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from ...bars import bar_close_position
from ...models import Side
from ... import sessions

# Regimes that ARM instead of entering the moment they qualify. Both can only
# fill at an N-bar extreme -- `close > max(high of the previous N bars)` -- so
# the fill sits at the highest price in 25 (trend) or 6 (momentum) minutes by
# construction, and the stop then lands inside the ordinary retrace band. The
# other four are excluded on their own evidence:
#   * `pullback` already requires a 25-50% leg retracement before it fires.
#   * `vwap_reclaim` already requires a flush THROUGH session VWAP and a
#     reclaim back across it within `vwap_reclaim_lookback_bars` -- it cannot
#     fire on a continuous move, so the wait is built into its own trigger.
#     (An earlier version of this comment grouped it with `range` as entering
#     "against the move". That is right for `range` and wrong here: a reclaim
#     enters WITH the session thesis after a counter-move against it. The
#     exclusion stands, the reason was imprecise.)
#   * `range` is true mean-reversion -- it buys the low and sells the high,
#     so there is no breakout to retest.
#   * `vol_squeeze` measured no post-entry retrace above baseline at all
#     (-0.024R across 11 archived trades, against trend's +0.641R across 9),
#     so arming it would add latency for nothing.
ARMED_RETEST_REGIMES = frozenset({"trend", "momentum"})


class ArmedRetestMixin:
    """Arms a qualifying breakout regime and decides when it enters. Mixed
    into ``TopTierAdaptiveStrategy`` (``strategy.py``), whose
    ``BaseStrategy`` supplies ``params``, ``config`` and the contexts."""

    def _prune_armed_retests(self) -> None:
        """Drop arms from a previous session, and any that outlived their
        window without being consumed. Called once per ``entry_signals`` so a
        symbol that stops appearing in the watchlist cannot leak an entry."""
        if not self._armed_retests:
            return
        now = sessions.now_et()
        today = now.date()
        max_minutes = float(self.params.get("armed_retest_max_minutes", 12.0))
        # Twice the window. Expiry itself is handled in the verdict, where it
        # produces the market entry; this reaps arms nothing came back for.
        #
        # The bound matters. An arm past its window is a licence to enter at
        # market on the next qualifying cycle, and the regime can go a long
        # time without qualifying -- index confirmation lapses, the score
        # dips. Keeping arms for 40+ minutes meant a fallback entry could be
        # justified by a breakout that happened most of an hour earlier, at a
        # level the tape had moved away from. Past 2x, the arm is dropped and
        # the next qualifying cycle arms again, which waits rather than
        # entering on stale evidence.
        stale_after = max_minutes * 2.0
        for key, arm in list(self._armed_retests.items()):
            if arm.get("session_date") != today:
                del self._armed_retests[key]
                continue
            armed_at = arm.get("armed_at")
            if armed_at is None:
                del self._armed_retests[key]
                continue
            if (now - armed_at).total_seconds() / 60.0 >= stale_after:
                del self._armed_retests[key]

    def _drop_armed_retests(self, symbol: str) -> None:
        """Forget every arm on *symbol*, both sides and both regimes."""
        if not self._armed_retests:
            return
        prefix = f"{symbol}|"
        for key in [k for k in self._armed_retests if k.startswith(prefix)]:
            del self._armed_retests[key]

    def _expired_armed_retests(
        self, symbol: str, allowed_regimes: set[str],
    ) -> list[tuple[Side, str, dict[str, Any]]]:
        """Arms whose wait ran out, popped and returned for immediate entry.

        This is the market fallback, and it runs BEFORE the build queue and
        outside the per-cycle gates on purpose.

        Those gates -- the ``_decide_side`` vote, index confirmation, the
        confirmation bar -- were ALL satisfied at arm time; that is the only
        way an arm gets created. Re-imposing them at expiry means an arm can
        only take its fallback on a cycle where the setup happens to fully
        re-qualify, and if it does not, the trade is silently dropped.

        That is not hypothetical. INTC, 2026-09-21: armed 09:58 at 118.39 on a
        setup that passed every gate, index confirmation lapsed at 10:03, and
        the stock ran to 124.64 without a single entry. The retest never came
        (``touched=0`` throughout), so the fallback was the whole point, and it
        never fired once -- the expiry check sat behind the gate that had
        failed.

        Index confirmation is an ENTRY gate: once in a position it no longer
        applies. Arming holds the trade OUT across exactly the window where a
        lapse can lock it out, so the setup is re-validated against conditions
        it had already cleared. The fallback has to be judged on the arm-time
        decision or it is not a fallback.

        What still applies: the regime must still be offered in this window
        (a trend arm does not fire at midday); the breakout must still HOLD --
        close on the trade's side of the armed level, or the arm has faded and
        is dropped as ``armed_retest_faded`` (checked at the entry path, which
        has the close); and ``_finalize_signal`` still applies the stretched /
        SR / structure rejections. A runaway that has gone too far to chase is
        still declined, by the gate that exists for that. The builder's
        fresh-N-bar-extreme check is NOT re-run: a runaway that never retested
        is rarely at a new extreme on the exact expiry minute, and requiring
        one made the fallback fire only by coincidence (2026-09-22).
        """
        if not self._armed_retests:
            return []
        prefix = f"{symbol}|"
        # `armed_retest_enabled` is the A/B control and the revert path, so it
        # has to be a clean off switch. The verdict already refuses to create
        # arms when it is false, but without this an arm created moments
        # earlier would still fire its fallback here -- the flag would stop
        # new arms and keep producing entries from old ones, which is the one
        # behaviour a kill switch must not have. Flipping it off drops
        # whatever is in flight.
        if not bool(self.params.get("armed_retest_enabled", True)):
            for key in [k for k in self._armed_retests if k.startswith(prefix)]:
                del self._armed_retests[key]
            return []
        now = sessions.now_et()
        max_minutes = float(self.params.get("armed_retest_max_minutes", 12.0))
        out: list[tuple[Side, str, dict[str, Any]]] = []
        for key in [k for k in self._armed_retests if k.startswith(prefix)]:
            arm = self._armed_retests[key]
            armed_at = arm.get("armed_at")
            if armed_at is None or arm.get("session_date") != now.date():
                del self._armed_retests[key]
                continue
            if (now - armed_at).total_seconds() / 60.0 < max_minutes:
                continue
            try:
                _sym, side_token, regime = key.split("|", 2)
            except ValueError:
                del self._armed_retests[key]
                continue
            del self._armed_retests[key]
            # The time window is a real constraint, not a gate the arm can
            # carry past: a trend arm must not fire during midday, when the
            # regime is not offered at all.
            if regime not in allowed_regimes:
                continue
            side = Side.LONG if str(side_token).upper() == "LONG" else Side.SHORT
            arm["waited_minutes"] = (now - armed_at).total_seconds() / 60.0
            out.append((side, regime, arm))
        return out

    def _armed_retest_verdict(
        self, symbol: str, side: Side, regime: str, close: float, atr: float,
        ltf: pd.DataFrame, frame: pd.DataFrame,
        *, regime_score: float = 0.0, regime_norm: float = 0.0,
    ) -> dict[str, Any]:
        """Arm on qualification; enter on the retest, or at market on expiry.

        The problem: ``trend`` and ``momentum`` fill at an N-bar extreme by
        construction, so the entry is the top of the move so far and the stop
        sits inside the retrace that normally follows. Measured on the archived
        sessions, a trend fill was followed by a retrace covering 85% of the way
        to its stop, against 21% from an arbitrary moment in the same session.

        So qualification no longer means "enter". It means "remember the level
        that was cleared and wait for price to come back to it". Four outcomes:

        ``none``   feature off, or no usable trigger level -- behave as before.
        ``wait``   armed, retest not yet confirmed. The cycle skips; other
                   regimes in the build queue are unaffected, so arming trend
                   does not stop a pullback firing on the same symbol.
        ``enter``  price returned to within ``armed_retest_zone_atr`` of the
                   level and closed back through it on a bar with the right
                   shape. This is the entry the regime was waiting for.

        EXPIRY IS NOT HANDLED HERE. It lives in ``_expired_armed_retests``,
        which runs before the build queue, because this method is only reached
        once a setup has re-cleared the side / index / confirmation-bar gates
        -- and an arm whose index confirmation lapsed while it waited would
        then never reach its own expiry. See that method for the INTC case
        that proved it.

        The stop rules are NOT changed on a retest entry. The gain is that the
        fill sits at a level price has already tested and held instead of at a
        fresh extreme; re-deriving the stop from a retest low would mean
        bypassing ``default_stop_pct`` / ``min_stop_atr_mult``, which are risk
        floors and a separate decision. The retest low is stamped in metadata
        so the question can be answered from data later.
        """
        out: dict[str, Any] = {"status": "none", "reason": None, "metadata": {}}
        if regime not in ARMED_RETEST_REGIMES:
            return out
        if not bool(self.params.get("armed_retest_enabled", True)):
            return out
        if not math.isfinite(close) or close <= 0 or not math.isfinite(atr) or atr <= 0:
            return out
        _recent, trigger_level = self._breakout_reference(regime, side, ltf, frame)
        if trigger_level is None or not math.isfinite(trigger_level) or trigger_level <= 0:
            return out

        now = sessions.now_et()
        key = f"{symbol}|{side.value}|{regime}"
        arm = self._armed_retests.get(key)
        zone_atr = max(0.0, float(self.params.get("armed_retest_zone_atr", 0.35)))
        invalidation_atr = max(0.0, float(self.params.get("armed_retest_invalidation_atr", 0.75)))
        max_minutes = float(self.params.get("armed_retest_max_minutes", 12.0))

        # A close well back through the level means the breakout failed; the
        # arm is dead. No rejection is raised here -- the builder's own
        # fresh-breakout check owns that message, and duplicating it would put
        # two different reasons on the same condition.
        #
        # Measured against the ARMED level, not the current one. The N-bar
        # reference walks up as new highs print, so testing against it would
        # move the invalidation line away from price on exactly the setups
        # that are still working, and drag it along behind a rolling-over one.
        # The level we are waiting for is the level we armed on.
        reference = float(arm["trigger_level"]) if arm is not None else float(trigger_level)
        if side == Side.LONG:
            invalidated = close < reference - invalidation_atr * atr
        else:
            invalidated = close > reference + invalidation_atr * atr
        if invalidated:
            self._armed_retests.pop(key, None)
            return out

        if arm is None or arm.get("session_date") != now.date():
            self._armed_retests[key] = {
                "armed_at": now,
                "session_date": now.date(),
                "trigger_level": float(trigger_level),
                # Carried so the expiry sweep can build the signal from the
                # score the setup had WHEN IT WAS VALIDATED. Re-scoring at
                # expiry would ask a different question -- the trade was
                # justified at arm time, the wait was only about price.
                "regime_score": float(regime_score),
                "regime_score_norm": float(regime_norm),
            }
            out.update({
                "status": "wait",
                "reason": (f"armed_awaiting_retest(level={trigger_level:.4f},"
                           f"close={close:.4f},wait={max_minutes:.0f}m)"),
            })
            return out

        armed_at = arm["armed_at"]
        level = float(arm["trigger_level"])
        waited_minutes = (now - armed_at).total_seconds() / 60.0

        # Did price come back to the level while we waited? Measured on the
        # base 1m frame regardless of the regime's own timeframe -- the finest
        # resolution gives the truest extreme for the window.
        touched = False
        extreme: float | None = None
        if frame is not None and not frame.empty:
            since = frame[frame.index > armed_at]
            if not since.empty:
                extreme = (float(since["low"].min()) if side == Side.LONG
                           else float(since["high"].max()))
        if extreme is not None and math.isfinite(extreme):
            if side == Side.LONG:
                touched = extreme <= level + zone_atr * atr
            else:
                touched = extreme >= level - zone_atr * atr

        reclaimed = close > level if side == Side.LONG else close < level
        min_close_pos = min(0.95, max(0.05, float(
            self.params.get("armed_retest_min_close_position", 0.60))))
        close_pos = bar_close_position(frame)
        bar_ok = (close_pos >= min_close_pos if side == Side.LONG
                  else close_pos <= (1.0 - min_close_pos))

        base_meta = {
            "armed_retest_regime": regime,
            "armed_retest_level": round(level, 4),
            "armed_retest_waited_minutes": round(waited_minutes, 2),
            "armed_retest_extreme": (round(extreme, 4) if extreme is not None
                                     and math.isfinite(extreme) else None),
        }

        if touched and reclaimed and bar_ok:
            self._armed_retests.pop(key, None)
            out.update({
                "status": "enter",
                "metadata": {**base_meta, "armed_retest_status": "retest_confirmed"},
            })
            return out

        out.update({
            "status": "wait",
            "reason": (f"armed_awaiting_retest(level={level:.4f},"
                       f"waited={waited_minutes:.1f}m/{max_minutes:.0f}m,"
                       f"touched={int(bool(touched))},reclaimed={int(bool(reclaimed))})"),
        })
        return out
