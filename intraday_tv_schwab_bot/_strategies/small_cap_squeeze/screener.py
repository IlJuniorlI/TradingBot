# SPDX-License-Identifier: MIT
"""Screener for `small_cap_squeeze`.

Selects a DYNAMIC universe of small-cap squeeze candidates from TradingView
(no fixed tradable list), long only. Thesis: low-float stocks already running
premarket on heavy volume — names that are squeezing or set up to squeeze.

Filters (AND-ed onto the equity-only baseline — ETFs/warrants/rights/units/
preferreds excluded by ``_common_equity_conditions``):

  * close $2-$20                         (``close`` -> ``premarket_close`` pre-RTH)
  * float 400K-20M shares                (``float_shares_outstanding_current``; low float
                                          = the squeeze fuel)
  * relative volume >= 2.0               (``relative_volume_10d_calc``)
  * change from open >= 5%               (``change_from_open`` -> ``premarket_change``
                                          pre-RTH = the premarket gap from prior close)
  * volume >= 5M                         (``volume`` -> ``premarket_volume`` pre-RTH)

The three canonical fields (close / change_from_open / volume) auto-resolve to
their premarket variants in the pre-09:30 ET session, so the same thresholds
mean "premarket close / premarket change / premarket volume" during the
08:05-09:30 premarket entry window and "RTH close / intraday change / day
volume" after the open.

``watchlist_mode`` (default ``premarket_lock_rth_live``): pre-09:30 the gapper
universe is STICKY — once a name qualifies it stays on the watchlist for the
rest of the premarket session even if its premarket %change later slips below
threshold, so the universe doesn't churn before the open. At 09:30 it switches
to a live RTH re-screen each cycle, UNIONED with the premarket-locked names
(kept "warm" so a faded gapper is still tracked for a VWAP reclaim), then ranked
by activity_score and capped to ``tradingview.max_candidates``. Set ``none`` to
re-screen every cycle with no premarket lock. Ranked by gap% x clipped RVOL
(gap dominates; RVOL bounded so a single print can't dominate). Bias always LONG.
"""
from datetime import time

from ..shared import Candidate, Side, now_et
from ..screener_base import BaseStrategyScreener


class SmallCapSqueezeScreener(BaseStrategyScreener):
    strategy_name = "small_cap_squeeze"

    def watchlist_mode(self) -> str:
        params = self.config.strategies[self.strategy_name].params
        return str(params.get("watchlist_mode", "none") or "none").strip().lower() or "none"

    def run(self) -> list[Candidate]:
        params = self.config.strategies[self.strategy_name].params
        c = self._column

        min_price = float(params.get("min_price", 2.0))
        max_price = float(params.get("max_price", 20.0))
        min_float = float(params.get("min_float_shares", 400_000))
        max_float = float(params.get("max_float_shares", 20_000_000))
        min_rvol = float(params.get("min_rvol", 2.0))
        min_change_from_open = float(params.get("min_change_from_open", 5.0))
        min_volume = int(params.get("min_volume", 5_000_000))

        conditions = [
            *self._common_equity_conditions(),
            c("close").between(min_price, max_price),
            # TradingView exposes both `float_shares_outstanding_current` (the
            # live value the screener UI surfaces) and the legacy
            # `float_shares_outstanding`. If the dry-run universe comes back
            # empty/sparse, swap to the legacy name.
            c("float_shares_outstanding_current").between(min_float, max_float),
            c("relative_volume_10d_calc") >= min_rvol,
            c("change_from_open") >= min_change_from_open,
            c("volume") >= min_volume,
        ]

        q = (
            self._base_query()
            .select(*self._select_fields(
                "name",
                "description",
                "exchange",
                "close",
                "volume",
                "market_cap_basic",
                "float_shares_outstanding_current",
                "change",
                "change_from_open",
                "relative_volume_10d_calc",
            ))
            .where(*conditions)
            .order_by(self._order_field("change_from_open"), ascending=False)
        )
        df = self._execute(q)

        # Score by gap size weighted by RVOL: a 12% gapper with 2x RVOL ranks
        # above a 20% gapper with 0.6x. RVOL clipped to [0.5, 3.0] so a single
        # fluke print can't dominate; gap percent dominates otherwise.
        rows = self._candidate_rows(
            df,
            self.strategy_name,
            directional_bias_fn=lambda row: Side.LONG,
            activity_score_fn=lambda row: (
                float(row.get("change_from_open", 0.0) or 0.0)
                * max(0.5, min(float(row.get("relative_volume_10d_calc", 1.0) or 1.0), 3.0))
            ),
        )
        if self.watchlist_mode() == "premarket_lock_rth_live":
            return self._merge_premarket_lock(rows, now_et())
        return rows

    def _merge_premarket_lock(self, rows: list[Candidate], now) -> list[Candidate]:
        """Hybrid watchlist (``premarket_lock_rth_live``).

        Premarket (< 09:30 ET): sticky-accumulate — a name that qualifies stays
        locked for the rest of the premarket session even if it later drops out
        of the live screen, so the pre-open universe doesn't churn. RTH
        (>= 09:30): union the live re-screen with the premarket-locked set (fresh
        rows win on a symbol collision; a faded-but-locked name is carried with
        its last premarket metadata so it stays watched for a VWAP reclaim —
        entry logic reads live bars, not this metadata). The merged set is ranked
        by activity_score and capped to ``tradingview.max_candidates``.

        ``now`` is injected (not read off the wall clock) so the merge is
        unit-testable. The locked set resets on a new ET trading day.
        """
        locked = getattr(self, "_premarket_locked", None)
        if locked is None or getattr(self, "_lock_date", None) != now.date():
            locked = {}
            self._lock_date = now.date()
        fresh = {c.symbol: c for c in rows}
        if now.time() < time(9, 30):
            # premarket: accumulate (fresh rows refresh/extend the locked set)
            locked.update(fresh)
            merged = dict(locked)
        else:
            # RTH: locked names (kept warm) unioned with the live screen; fresh wins
            merged = dict(locked)
            merged.update(fresh)
        self._premarket_locked = locked
        max_n = int(self.config.tradingview.max_candidates)
        ranked = sorted(
            merged.values(),
            key=lambda c: float(getattr(c, "activity_score", 0.0) or 0.0),
            reverse=True,
        )
        return ranked[:max_n]
