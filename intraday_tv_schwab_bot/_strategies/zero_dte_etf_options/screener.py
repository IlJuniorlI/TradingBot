# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
)
from ..screener_base import BaseStrategyScreener

class ZeroDteEtfOptionsScreener(BaseStrategyScreener):
    strategy_name = 'zero_dte_etf_options'

    def run(self) -> list[Candidate]:
        """Synthesize candidates for the fixed ETF universe without calling
        TradingView. The 0DTE strategy targets a hardcoded set of liquid
        ETFs (SPY / QQQ / IWM) that are always tradable — TV adds no
        value here. Bypassing TV also eliminates a silent-failure mode
        where TV's america-market scan returns 0 rows for ETFs despite
        SPY / QQQ being present (observed live on 2026-05-19 — 0 trades
        all session because of this).

        Downstream decisioning (``_regime_confirm`` in strategy.py) uses
        ``bars[underlying]`` (Schwab data feed) for ALL metrics:
          * close / volume / change_from_open computed from the frame
            (change_from_open = u_day_ret from _session_open_price)
          * relative_volume_10d_calc replaced by ``live_activity_score``
          * confirm_index / volatility_symbol read from config
        So the candidate object only needs to be PRESENT for the engine
        to dispatch ``entry_signals`` per underlying. The dashboard
        candidate tile reads its activity_score + directional_bias from
        ``engine._publish_state``'s live resolver (which calls back into
        the strategy's public ``live_activity_score`` and
        ``dashboard_directional_bias`` hooks against the streamed
        frame — see _strategies/README.md "Extension hooks"), so
        the screener's stub values on the Candidate itself are
        intentionally minimal — only ``name`` and ``confirm_index`` are
        useful downstream.
        """
        if not bool(self.config.options.enabled):
            return []
        underlyings = list(self.config.options.underlyings)
        if not underlyings:
            return []
        confirmation_map = dict(self.config.options.confirmation_symbols or {})
        out: list[Candidate] = []
        for rank, symbol in enumerate(underlyings, start=1):
            sym = str(symbol or "").upper().strip()
            if not sym:
                continue
            metadata = {
                "name": sym,
                "confirm_index": confirmation_map.get(sym),
            }
            out.append(Candidate(
                symbol=sym,
                strategy=self.strategy_name,
                rank=rank,
                activity_score=1.0,
                directional_bias=None,
                metadata=metadata,
            ))
        return out
