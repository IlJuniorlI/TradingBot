# SPDX-License-Identifier: MIT
from ..shared import (
    Candidate,
)
from ..screener_base import BaseStrategyScreener

class ZeroDteEtfLongOptionsScreener(BaseStrategyScreener):
    strategy_name = 'zero_dte_etf_long_options'

    def run(self) -> list[Candidate]:
        """Synthesize candidates for the fixed ETF universe without calling
        TradingView. Mirrors ``ZeroDteEtfOptionsScreener.run`` — see that
        docstring for the rationale. The long-options subclass inherits
        all regime-confirmation logic from the parent strategy class, so
        the candidate object only needs to be PRESENT for the engine to
        dispatch ``entry_signals`` per underlying.
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
            # change_from_open / relative_volume_10d_calc stubs were
            # dropped (2026-05-19) — see parent screener docstring.
            out.append(Candidate(
                symbol=sym,
                strategy=self.strategy_name,
                rank=rank,
                activity_score=1.0,
                directional_bias=None,
                metadata=metadata,
            ))
        return out
