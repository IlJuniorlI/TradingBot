# SPDX-License-Identifier: MIT
"""The dashboard's state and payload builders.

``DashboardCache`` holds:

  - ``snapshot_cache``: per-symbol dashboard snapshot payloads, keyed by
    upper-cased symbol. Values are ``{"signature": tuple, "payload": dict}``
    entries that callers compare against a freshly-computed signature to
    decide whether to return the cached payload or recompute.
  - ``chart_cache``: per-(symbol, timeframe_mode, max_bars) chart payloads,
    same signature-keyed shape.
  - ``lock``: single ``RLock`` guarding both caches. Held briefly around
    get/set operations so concurrent dashboard polls don't corrupt state.
  - ``log_component_failure``: the payload builders' failure log
    (``log_setup.ComponentFailureLog``: WARNING at most once a minute per
    component, DEBUG otherwise).

and builds the payloads: the dashboard state's symbol part
(``build_payload``, which the engine publishes each cycle), each symbol's
snapshot, S/R row and key-level zones, and the chart the HTTP handler
serves. The stateless helpers are in ``dashboard_payloads``, the zone
classification in ``dashboard_zones`` and the S/R snapshot in
``sr_snapshot``.
"""
from __future__ import annotations

import copy
import logging
import math
from collections.abc import Mapping
from dataclasses import asdict
from threading import RLock
from typing import TYPE_CHECKING, Any

import pandas as pd

from .candles import detect_candle_context, detect_per_bar_candle_patterns
from .chart_patterns import analyze_chart_pattern_context
from .config import BotConfig, DashboardChartConfig
from .dashboard_payloads import (
    bars_from_frame,
    cache_json_signature,
    frame_signature,
    fvg_anchor_abs_index,
    fvg_payload,
    htf_chart_frame,
    normalize_exchange,
    quote_exchange,
    recent_trade_markers,
    symbol_trade_signature,
    technical_line_payload,
)
from .dashboard_zones import build_level_zones, level_anchors
from .htf_levels import HTFContext
from .log_setup import ComponentFailureLog
from .models import Candidate, Position, Side, asset_type_of, is_option_asset
from .numeric import safe_float
from .sr_snapshot import sr_snapshot, structure_event_label
from .support_resistance import analyze_market_structure
from .symbols import NON_STREAMABLE
from .technical_levels import TechnicalLevelsContext, build_technical_levels_context
from .bars import last_bucket_forming, session_bucket_ends
from .indicators import htf_ema_spans, last_bar_atr, ltf_ema_spans
from . import sessions
from .levels_shared import collapse_price_ladder, effective_side_tolerance

if TYPE_CHECKING:
    from ._strategies.strategy_base import BaseStrategy
    from .data_feed import MarketDataStore
    from .paper_account import PaperAccount

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


# TA-Lib's candle functions read at most 14 bars before the bar they score
# (CDLBREAKAWAY, CDLLADDERBOTTOM, CDLMATHOLD and CDLRISEFALL3METHODS; the
# custom tweezers read 1), so a per-bar pattern map fed this many bars ahead
# of the ones it shows scores every shown bar exactly as a full-history run
# does. Until 2026-09-23 the snapshot fed TA-Lib only its 48 shown bars and the
# chart only its 90/360: on 2026-09-18/21/22 (10 symbols x 6 times) 1,836 of
# 24,840 shown bars carried different tags than a 400-bar run, all in the
# oldest bars of the window, and the snapshot's starved tags overwrote the
# chart's on the newest 48 bars. 12 extra bars already matched on every bar.
_CANDLE_PATTERN_WARMUP_BARS = 14


class DashboardCache:
    """Dashboard-side state container, payload builders and config-bound
    helpers.

    Owns snapshot/chart caches, the rate-limited error logger, the builders
    (``build_payload``, ``symbol_snapshot``, ``sr_row``,
    ``strategy_level_zones``, ``chart_payload``), and the small
    config-reading helpers that resolve chart profile / max-bars /
    candidate-limit from ``config.dashboard`` and ``config.tradingview``.
    """

    def __init__(
        self,
        config: BotConfig,
        *,
        data: MarketDataStore,
        strategy: BaseStrategy,
        account: PaperAccount,
    ) -> None:
        self.config = config
        self.data = data
        self.strategy = strategy
        self.account = account
        self.snapshot_cache: dict[str, dict[str, Any]] = {}
        self.chart_cache: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.lock = RLock()
        self.log_component_failure = ComponentFailureLog(LOG)

    def prune_inactive_symbols(self, active_symbols: set[str]) -> int:
        """Drop cached snapshot + chart payloads for symbols no longer in the
        active set. Mirrors `MarketDataStore.prune_inactive_symbols` on the
        dashboard side. Each entry is a deep-copied serialized payload —
        kilobytes each — so a long-running bot with high symbol churn
        accumulates real memory here too. Returns count evicted."""
        active = {str(s).upper().strip() for s in (active_symbols or set()) if s}
        with self.lock:
            snap_stale = {sym for sym in self.snapshot_cache.keys() if str(sym).upper().strip() not in active}
            chart_stale = {key for key in self.chart_cache.keys() if str(key[0]).upper().strip() not in active}
            for sym in snap_stale:
                self.snapshot_cache.pop(sym, None)
            for key in chart_stale:
                self.chart_cache.pop(key, None)
        # Distinct symbol count, not entry count, so the engine has a
        # consistent figure to log alongside the data_feed prune count.
        return len({str(sym).upper().strip() for sym in (snap_stale | {k[0] for k in chart_stale})})

    # ---------------------------------------------------------------------
    # Chart-profile helpers (Phase 5 Step 3 extraction).
    # Previously instance methods on IntradayBot.
    # ---------------------------------------------------------------------

    def chart_profile(self, mode: str = "compact") -> DashboardChartConfig:
        return self.config.dashboard.charting.resolved_profile(mode)

    def chart_max_bars(self, mode: str = "compact") -> int:
        # An int in [1, 480], checked at load.
        return self.chart_profile(mode).max_bars

    def snapshot_max_bars(self) -> int:
        return max(12, min(self.chart_max_bars("compact"), 48))

    def charting_settings(self) -> dict[str, Any]:
        return {
            "compact_chart_timeframe": self.config.dashboard.charting.compact_chart_timeframe,
            "compact": asdict(self.chart_profile("compact")),
            "expanded": asdict(self.chart_profile("expanded")),
        }

    def candidate_limit(self) -> int:
        """Resolve the max candidate rows to emit on the dashboard.

        Base limit is ``config.tradingview.max_candidates``; a strategy may
        override via ``dashboard_candidate_limit(base)``."""
        limit = max(1, int(self.config.tradingview.max_candidates))
        return max(1, int(self.strategy.dashboard_candidate_limit(limit)))

    # ---------------------------------------------------------------------
    # Payload builders that need data/strategy/account (Phase 5 Step 5).
    # ---------------------------------------------------------------------

    def _chart_htf_level_request(self) -> dict[str, Any]:
        """Level arguments of the HTF context the chart's HTF FVGs and RSI
        divergence lines are drawn from: the strategy's own HTF build
        (``dashboard_level_context_spec``, which its level zones use and
        which matches the context its HTF divergence score reads), else
        support_resistance. Until 2026-09-24 it was support_resistance with
        EMA 50/200 whatever the strategy: the peer family scores divergence
        on ``htf_pivot_span``, so a preset changing it would chart
        divergences the score did not apply (and miss ones it did)."""
        sr_cfg = self.config.support_resistance
        spec = self.strategy.dashboard_level_context_spec()
        spec = spec if isinstance(spec, dict) else {}
        default_fast, default_slow = htf_ema_spans({})
        return {
            "pivot_span": int(spec.get("pivot_span", sr_cfg.pivot_span)),
            "max_levels_per_side": int(spec.get("max_levels_per_side", sr_cfg.max_levels_per_side)),
            "atr_tolerance_mult": float(spec.get("atr_tolerance_mult", sr_cfg.atr_tolerance_mult)),
            "pct_tolerance": float(spec.get("pct_tolerance", sr_cfg.pct_tolerance)),
            "stop_buffer_atr_mult": float(spec.get("stop_buffer_atr_mult", sr_cfg.stop_buffer_atr_mult)),
            "ema_fast_span": int(spec.get("ema_fast_span", default_fast)),
            "ema_slow_span": int(spec.get("ema_slow_span", default_slow)),
        }

    def _per_bar_candle_map(self, frame: pd.DataFrame, shown_bars: int) -> dict[Any, dict[str, list[str]]]:
        """Per-bar candle tags for the last ``shown_bars`` bars of ``frame``,
        each scored with full TA-Lib context (``_CANDLE_PATTERN_WARMUP_BARS``).
        The snapshot and the chart payload both use it, so the snapshot bars
        the client merges over the chart's carry the chart's tags."""
        return detect_per_bar_candle_patterns(
            frame,
            bullish_allowed=self.config.candles.bullish_patterns,
            bearish_allowed=self.config.candles.bearish_patterns,
            lookback=int(shown_bars) + _CANDLE_PATTERN_WARMUP_BARS,
        )

    def _apply_strategy_ltf_emas(
        self,
        symbol: str,
        frame: pd.DataFrame,
        bars: list[dict[str, Any]],
        *,
        timeframe: str,
    ) -> tuple[int, int]:
        """Put the strategy's own LTF fast/slow EMA on ``bars`` (the tail of
        ``frame``, a ``timeframe`` frame) and return the two spans.

        The strategy reads ema9/ema20 off ``get_merged(timeframe,
        span_scale=ltf_indicator_span_scale, ema_spans=ltf_ema_spans(params))``:
        on top_tier's 1m LTF that is a 45/100-bar EMA (its
        ltf_ema_fast_span / ltf_ema_slow_span) that restarts on each session's
        first RTH bar. Until 2026-09-23 the chart drew a continuous 45/100 EWM across the
        prior day and premarket instead (the opposite stack to the bot's on 93
        of 480 bars between 09:30 and 10:30 across 8 symbols on 2026-09-22),
        and the snapshot bars, merged over the chart's newest 48, carried the
        native 9/20 -- so the lines labelled EMA45/EMA100 turned into EMA9/20
        partway along the chart. The snapshot and the chart both go through
        here.
        """
        params = getattr(self.strategy, "params", {}) or {}
        scale = float(params.get("ltf_indicator_span_scale", 1.0))
        spans = ltf_ema_spans(params)
        # The frame the caller built is canonical (scale 1, EMA 9/20); fetch
        # the strategy's own whenever either differs.
        if bars and (scale != 1.0 or spans != (9, 20)):
            scaled = self.data.get_merged(symbol, timeframe=timeframe, with_indicators=True,
                                          span_scale=scale, ema_spans=spans)
            emas = scaled[["ema9", "ema20"]].reindex(frame.index[-len(bars):])
            for bar, fast, slow in zip(bars, emas["ema9"], emas["ema20"]):
                bar["ema9"] = safe_float(fast)
                bar["ema20"] = safe_float(slow)
        return spans

    def build_payload(
        self,
        *,
        positions: Mapping[str, Position],
        last_candidates: list[Candidate],
        watchlist: list[str],
        quote_watchlist: list[str],
        entry_decisions: Mapping[str, Any],
        warmup_summary: Mapping[str, Any],
        allow_refresh: bool,
    ) -> dict[str, Any]:
        """The symbol part of the dashboard state the engine publishes each
        cycle, which adds its own status fields: the account's performance
        (its positions carrying their S/R row's fields), the candidates card,
        each shown symbol's snapshot and exchange, the feed's and the
        strategy's symbol lists, and the chart settings. The symbols shown are
        the positions', the watchlist, the quote watchlist, the candidates and
        the S/R rows', in that order; the caches drop every other symbol.
        ``allow_refresh`` is the gate's context refresh: the S/R rows and
        snapshots refresh their HTF reads only while it is on. Until
        2026-09-27 this was the engine's ``_dashboard_state`` (refactor cut
        C40)."""
        performance = self.account.snapshot_copy(positions)
        candidates = []
        entry_decision_by_symbol = {str(symbol or '').upper().strip(): copy.deepcopy(payload) for symbol, payload in entry_decisions.items() if str(symbol or '').upper().strip()}
        candidate_limit = self.candidate_limit()
        symbol_exchanges: dict[str, str] = {}

        def remember_exchange(symbol_value: Any, exchange_value: Any = None) -> None:
            symbol_key = str(symbol_value or '').upper().strip()
            if not symbol_key:
                return
            normalized_exchange = normalize_exchange(exchange_value)
            if normalized_exchange is None:
                normalized_exchange = quote_exchange(self.data.get_quote(symbol_key) or {})
            if normalized_exchange:
                symbol_exchanges[symbol_key] = normalized_exchange

        # Live activity-score + directional-bias resolvers for the
        # dashboard candidates card. Strategies whose screeners can't
        # populate real values at screen time (e.g. 0DTE option
        # strategies that synthesize candidates locally without TV)
        # opt into resolution here by defining the public
        # methods ``live_activity_score(frame)``,
        # ``dashboard_directional_bias(frame)``, and/or
        # ``dashboard_change_from_open(frame)`` on the strategy class.
        # Strategies whose screeners DO populate real values (e.g.
        # equity strategies pulling rvol + change_from_open from TV)
        # just don't define them — the candidate's existing
        # activity_score / directional_bias / change_from_open values
        # flow through unchanged. Pure duck-typing — no plugin-type
        # dispatch needed. All compute paths share a single frame
        # fetch per candidate.
        live_score_fn = getattr(self.strategy, 'live_activity_score', None)
        live_bias_fn = getattr(self.strategy, 'dashboard_directional_bias', None)
        live_change_fn = getattr(self.strategy, 'dashboard_change_from_open', None)
        all_candidate_rows: list[dict[str, Any]] = []
        for c in last_candidates:
            remember_exchange(c.symbol, c.metadata.get("exchange"))
            exchange = normalize_exchange(c.metadata.get("exchange"))
            activity_score_for_row = c.activity_score
            directional_bias_for_row = c.directional_bias
            change_from_open_for_row = c.metadata.get("change_from_open")
            if live_score_fn is not None or live_bias_fn is not None or live_change_fn is not None:
                try:
                    frame = self.data.get_merged(c.symbol, with_indicators=True)
                    if live_score_fn is not None:
                        live_score = float(live_score_fn(frame))
                        # Reject NaN / +/-Inf — live_activity_score is
                        # designed to fail-open at 1.0 (neutral) but a
                        # subclass override could regress, and downstream
                        # json_safe would silently coerce to null and
                        # break the score ring rather than the candidate
                        # stub of 1.0 the rest of the system expects.
                        if math.isfinite(live_score):
                            activity_score_for_row = live_score
                    if live_bias_fn is not None:
                        live_bias = live_bias_fn(frame)
                        # Type guard — only accept Side enum members.
                        # Defends against a subclass returning a string
                        # ("LONG") or other shape that would crash the
                        # ``.value`` deref below and kill the entire
                        # publish loop.
                        if isinstance(live_bias, Side):
                            directional_bias_for_row = live_bias
                    if live_change_fn is not None:
                        live_change = live_change_fn(frame)
                        # Same finite guard as activity_score — a None
                        # return means "frame insufficient to compute,
                        # fall back to candidate metadata" (which for
                        # 0DTE is also None after the 2026-05-19
                        # stub-removal, so the dashboard renders "—"
                        # until session bars are sufficient).
                        if live_change is not None:
                            live_change_f = float(live_change)
                            if math.isfinite(live_change_f):
                                change_from_open_for_row = live_change_f
                except Exception:
                    LOG.debug("Dashboard live-publish compute failed for %s; using candidate stubs.", c.symbol, exc_info=True)
            row = {
                "symbol": c.symbol,
                "rank": c.rank,
                "activity_score": activity_score_for_row,
                "exchange": exchange or None,
                "change_from_open": change_from_open_for_row,
                # ``change`` (prior-close-relative) is shipped alongside
                # ``change_from_open`` (session-open-relative) for strategies
                # that emit both (currently: top_tier_adaptive). Dashboard
                # uses ``change`` for the "Day %" display fallback so the
                # screener-fallback value matches the prior-close semantic
                # of the live Schwab ``quote.percent_change`` primary.
                # Strategies that don't emit ``change`` get None here.
                "change": c.metadata.get("change"),
                "close": c.metadata.get("close"),
                "volume": c.metadata.get("volume"),
                "directional_bias": directional_bias_for_row.value if directional_bias_for_row else None,
            }
            all_candidate_rows.append(row)
            if len(candidates) < candidate_limit:
                candidates.append(copy.deepcopy(row))

        sr_symbols: list[str] = []
        seen: set[str] = set()
        for row in performance.get("positions", []):
            sym = str(row.get("underlying") or row.get("symbol") or "").upper().strip()
            if sym and sym not in seen:
                seen.add(sym)
                sr_symbols.append(sym)
        for sym in list(quote_watchlist) + list(watchlist) + [c.get("symbol") for c in candidates]:
            symbol = str(sym or "").upper().strip()
            if symbol and symbol not in seen:
                seen.add(symbol)
                sr_symbols.append(symbol)
        sr_symbols = sr_symbols[:12]

        sr_levels = []
        sr_by_symbol: dict[str, dict[str, Any]] = {}
        for symbol in sr_symbols:
            row = self.sr_row(symbol, allow_refresh=allow_refresh)
            if row is None:
                continue
            sr_levels.append(row)
            sr_by_symbol[symbol] = row

        if performance.get("positions"):
            enriched_positions = []
            for row in performance["positions"]:
                symbol = str(row.get("underlying") or row.get("symbol") or "").upper().strip()
                sr_row = sr_by_symbol.get(symbol) or self.sr_row(symbol, allow_refresh=allow_refresh)
                new_row = copy.deepcopy(row)
                if sr_row is not None:
                    new_row.update({
                        "sr_symbol": symbol,
                        "sr_timeframe": sr_row.get("timeframe"),
                        "sr_nearest_support": sr_row.get("nearest_support"),
                        "sr_nearest_resistance": sr_row.get("nearest_resistance"),
                        "sr_support_distance_pct": sr_row.get("support_distance_pct"),
                        "sr_resistance_distance_pct": sr_row.get("resistance_distance_pct"),
                        "sr_regime_hint": sr_row.get("regime_hint"),
                        "sr_state": sr_row.get("state"),
                    })
                enriched_positions.append(new_row)
            performance["positions"] = enriched_positions

        candidate_by_symbol = {str(row.get("symbol") or "").upper().strip(): row for row in all_candidate_rows}
        position_by_symbol: dict[str, dict[str, Any]] = {}
        for row in performance.get("positions", []):
            base_symbol = str(row.get("underlying") or row.get("symbol") or "").upper().strip()
            if base_symbol and base_symbol not in position_by_symbol:
                position_by_symbol[base_symbol] = row

        warmup_by_symbol = {
            str(item.get('symbol') or '').upper().strip(): item
            for item in (warmup_summary.get('symbols') or [])
            if str(item.get('symbol') or '').upper().strip()
        }

        dashboard_symbol_order: list[str] = []
        seen_dashboard_symbols: set[str] = set()
        for bucket in (
            [str(row.get("underlying") or row.get("symbol") or "").upper().strip() for row in performance.get("positions", [])],
            [str(sym or "").upper().strip() for sym in watchlist],
            [str(sym or "").upper().strip() for sym in quote_watchlist],
            [str(row.get("symbol") or "").upper().strip() for row in candidates],
            [str(row.get("symbol") or "").upper().strip() for row in sr_levels],
        ):
            for symbol in bucket:
                if symbol and symbol not in seen_dashboard_symbols:
                    seen_dashboard_symbols.add(symbol)
                    dashboard_symbol_order.append(symbol)

        for row in performance.get("positions", []):
            remember_exchange(row.get("underlying") or row.get("symbol"))
        for trade in performance.get("recent_trades", []):
            remember_exchange(trade.get("underlying") or trade.get("symbol"))
        for symbol in dashboard_symbol_order:
            remember_exchange(symbol)

        dashboard_symbols = [
            self.symbol_snapshot(
                symbol,
                exchange=symbol_exchanges.get(symbol),
                sr_row=sr_by_symbol.get(symbol),
                candidate_row=candidate_by_symbol.get(symbol),
                position_row=position_by_symbol.get(symbol),
                entry_decision=entry_decision_by_symbol.get(symbol),
                warmup=warmup_by_symbol.get(symbol),
                allow_refresh=allow_refresh,
            )
            for symbol in dashboard_symbol_order
        ]
        for snapshot in dashboard_symbols:
            remember_exchange(snapshot.get('symbol'), snapshot.get('exchange'))
        self.prune_inactive_symbols(set(dashboard_symbol_order))

        return {
            "data": {
                **self.data.dashboard_data_snapshot(),
                "non_streamable_symbols": sorted(NON_STREAMABLE),
                "tradable_symbols": self.strategy.dashboard_tradable_symbols(),
                "index_symbols": self.strategy.dashboard_index_symbols(),
            },
            "performance": performance,
            "candidates": candidates,
            "symbol_exchanges": symbol_exchanges,
            "dashboard_charting": self.charting_settings(),
            "dashboard_symbols": dashboard_symbols,
        }

    def symbol_snapshot(
        self,
        symbol: str,
        exchange: str | None = None,
        sr_row: dict[str, Any] | None = None,
        candidate_row: dict[str, Any] | None = None,
        position_row: dict[str, Any] | None = None,
        entry_decision: dict[str, Any] | None = None,
        warmup: dict[str, Any] | None = None,
        *,
        allow_refresh: bool = True,
    ) -> dict[str, Any]:
        """Assemble the full dashboard snapshot payload for a single symbol:
        quote, bars, S/R ladder, technicals, overlays and position markers in
        one cache-keyed dict. The ``_snapshot_*`` builders below make the
        parts, called in the order the data feed has always been read (its
        cycle caches are order-sensitive); each overlay builder logs its own
        failure and falls back to no overlay. The frame's close is read once,
        here, for the LTF gaps and order blocks; the HTF context is read
        once, by ``_snapshot_htf_overlays``, and handed on to the divergence
        lines."""
        symbol = str(symbol or "").upper().strip()
        quote = self.data.get_quote(symbol) or {}
        max_quote_age = max(1.0, float(self.config.runtime.quote_cache_seconds))
        quote_is_fresh = bool(symbol and quote and self.data.quotes_are_fresh([symbol], max_quote_age))
        if sr_row is None and symbol:
            sr_row = self.sr_row(symbol, allow_refresh=allow_refresh)
        frame = self.data.get_merged(symbol, with_indicators=True) if symbol else None
        snapshot_signature = self.symbol_snapshot_signature(
            symbol,
            frame,
            quote=quote,
            quote_is_fresh=quote_is_fresh,
            sr_row=sr_row,
            candidate_row=candidate_row,
            position_row=position_row,
            entry_decision=entry_decision,
            warmup=warmup,
            allow_refresh=allow_refresh,
        )
        with self.lock:
            cached_snapshot = self.snapshot_cache.get(symbol)
            if cached_snapshot is not None and cached_snapshot.get("signature") == snapshot_signature and not self.snapshot_should_bypass_cache(symbol, allow_refresh=allow_refresh):
                # Shallow copy on cache hit instead of deepcopy. The
                # snapshot is a flat-ish dict of pre-computed values;
                # downstream serialization (`json_safe`) creates new
                # containers rather than mutating, so sharing inner
                # references is safe. Saves ~5ms per cache hit on
                # busy multi-symbol watchlists where dashboard polls
                # this for every snapshot every refresh cycle.
                return dict(cached_snapshot["payload"])
        bars, snapshot_ema_spans = self._snapshot_bars(symbol, frame)
        quote_payload, current_price = self._snapshot_quote(
            quote, quote_is_fresh, frame, bars, candidate_row, sr_row,
        )
        ladder = self._snapshot_ladder(sr_row, current_price)
        tech_ctx, technical_payload = self._snapshot_technicals(symbol, frame, current_price)
        position_markers = self._position_markers(position_row)

        nearest_support = ladder["nearest_support"]
        nearest_resistance = ladder["nearest_resistance"]
        zone_support_prices = [nearest_support] if nearest_support not in (None, 0.0) else []
        zone_resistance_prices = [nearest_resistance] if nearest_resistance not in (None, 0.0) else []
        key_level_zones = self.strategy_level_zones(
            symbol,
            frame,
            current_price,
            support_prices=zone_support_prices,
            resistance_prices=zone_resistance_prices,
            broken_support_price=safe_float((sr_row or {}).get("broken_support")),
            broken_resistance_price=safe_float((sr_row or {}).get("broken_resistance")),
            pending_support_price=safe_float((sr_row or {}).get("pending_support")),
            pending_resistance_price=safe_float((sr_row or {}).get("pending_resistance")),
            allow_htf_refresh=allow_refresh,
        )
        compact_chart_profile = self.chart_profile("compact")
        expanded_chart_profile = self.chart_profile("expanded")
        # The HTF context is read once, if any overlay needs it (the HTF FVGs,
        # the RSI divergence lines), and the divergence lines reuse it.
        chart_wants_rsi_div = bool(compact_chart_profile.show_rsi_divergence) or bool(expanded_chart_profile.show_rsi_divergence)
        htf_ctx, htf_fair_value_gaps = self._snapshot_htf_overlays(
            symbol, compact_chart_profile, expanded_chart_profile, chart_wants_rsi_div, allow_refresh=allow_refresh,
        )

        # The LTF FVG and order block overlays are the contexts the strategy
        # reads: its request, at its price, the close of the frame's last
        # bar (the data feed's cycle cache holds them under that price). Until
        # 2026-09-27 the dashboard asked at the quote's last, so it drew
        # blocks and gaps sized, ranked and cut at a price the strategy never
        # judged, and built them a second time.
        frame_close = safe_float(frame.iloc[-1]["close"], 0.0) if frame is not None and not frame.empty else 0.0
        ltf_fair_value_gaps = self._snapshot_ltf_fair_value_gaps(
            symbol, frame, frame_close, compact_chart_profile, expanded_chart_profile,
        )
        htf_order_blocks, ltf_order_blocks = self._snapshot_order_blocks(
            symbol, frame, frame_close, compact_chart_profile, expanded_chart_profile,
        )
        ltf_divergence_lines, htf_divergence_lines = self._snapshot_divergence_lines(
            symbol, tech_ctx, htf_ctx, compact_chart_profile, expanded_chart_profile, chart_wants_rsi_div,
        )

        chart_payload = {
            "levels": {
                "nearest_support": nearest_support,
                "nearest_resistance": nearest_resistance,
                "support_distance_pct": safe_float((sr_row or {}).get("support_distance_pct")),
                "resistance_distance_pct": safe_float((sr_row or {}).get("resistance_distance_pct")),
                "supports": ladder["supports"],
                "resistances": ladder["resistances"],
                "next_support": ladder["next_support"],
                "next_resistance": ladder["next_resistance"],
                "broken_support": safe_float((sr_row or {}).get("broken_support")),
                "broken_resistance": safe_float((sr_row or {}).get("broken_resistance")),
                "pending_support": safe_float((sr_row or {}).get("pending_support")),
                "pending_resistance": safe_float((sr_row or {}).get("pending_resistance")),
                "key_level_zones": key_level_zones,
                "htf_fair_value_gaps": htf_fair_value_gaps,
                "ltf_fair_value_gaps": ltf_fair_value_gaps,
                "htf_order_blocks": htf_order_blocks,
                "ltf_order_blocks": ltf_order_blocks,
                "ltf_divergence_lines": ltf_divergence_lines,
                "htf_divergence_lines": htf_divergence_lines,
            },
            "technicals": technical_payload,
            "position_markers": position_markers,
            "recent_trades": recent_trade_markers(self.account, symbol),
            # Spans of the snapshot bars' ema9 / ema20, for labelling them
            # before (or without) a chart payload.
            "ema_fast_span": snapshot_ema_spans[0],
            "ema_slow_span": snapshot_ema_spans[1],
        }

        payload = {
            "symbol": symbol,
            "exchange": (
                normalize_exchange(exchange)
                or normalize_exchange((candidate_row or {}).get("exchange"))
                or quote_exchange(quote)
            ),
            "description": quote.get("description"),
            "quote": {
                **quote_payload,
                "age_seconds": self.data.quote_age_seconds(symbol) if quote else None,
            },
            "candidate": copy.deepcopy(candidate_row) if candidate_row else None,
            "entry_decision": copy.deepcopy(entry_decision) if entry_decision else None,
            "warmup": copy.deepcopy(warmup) if warmup else None,
            "position": copy.deepcopy(position_row) if position_row else None,
            "support_resistance": copy.deepcopy(sr_row) if sr_row else None,
            "bars": bars,
            "chart": chart_payload,
        }
        with self.lock:
            self.snapshot_cache[symbol] = {"signature": snapshot_signature, "payload": copy.deepcopy(payload)}
        return payload

    def _snapshot_bars(self, symbol: str, frame: pd.DataFrame | None) -> tuple[list[dict[str, Any]], tuple[int, int]]:
        """The snapshot's newest bars of ``frame``, each with its candle tags,
        and the spans of their ema9 / ema20: the strategy's own LTF EMAs when
        its LTF is 1m, else the frame's 9 / 20."""
        # Per-bar candle pattern map (completion-bar only, tier cascade).
        # Drives the tooltip's "Candle Patterns (this bar)" section. Computed
        # before bars are built so each bar dict can carry its own matched
        # patterns, for every bar the snapshot carries.
        snapshot_bars_count = self.snapshot_max_bars()
        snapshot_per_bar_candles: dict[Any, dict[str, list[str]]] = {}
        if frame is not None and not frame.empty:
            try:
                snapshot_per_bar_candles = self._per_bar_candle_map(frame, snapshot_bars_count)
            except Exception:
                self.log_component_failure(
                    "per_bar_candles",
                    "Per-bar candle pattern detection failed for %s",
                    symbol,
                )
                snapshot_per_bar_candles = {}
        bars = bars_from_frame(
            frame,
            max_bars=snapshot_bars_count,
            per_bar_candles=snapshot_per_bar_candles,
        )
        # Snapshot bars are 1m bars; they are the strategy's LTF bars (and are
        # merged into the LTF chart) only when its LTF is 1m.
        snapshot_ema_spans = (9, 20)
        if bars and self.strategy.ltf_minutes() == 1:
            snapshot_ema_spans = self._apply_strategy_ltf_emas(symbol, frame, bars, timeframe="1min")
        return bars, snapshot_ema_spans

    def _snapshot_quote(
        self,
        quote: dict[str, Any],
        quote_is_fresh: bool,
        frame: pd.DataFrame | None,
        bars: list[dict[str, Any]],
        candidate_row: dict[str, Any] | None,
        sr_row: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], float | None]:
        """The snapshot's ``quote`` block and the price its levels are read at.

        A stale quote gives no last, bid, ask, mark, mid or volume: the last
        price and the volume come from the bars instead, and the mark and mid
        from that last price. Its open, close and change fields are read
        whatever its age; outside the regular session, or when the quote has
        none, the close and the percent change are the candidate row's. The
        price is the last price, else the S/R row's,
        else the newest bar's close. The quote's age is not here:
        ``symbol_snapshot`` asks the feed for it last, where it always has."""
        latest_bar: dict[str, Any] = bars[-1] if bars else {}
        session_total_volume: float | None = None
        if frame is not None and not frame.empty:
            if isinstance(frame.index, pd.DatetimeIndex) and "volume" in frame.columns:
                session_index = pd.DatetimeIndex(frame.index)
                session_anchor = pd.Timestamp(session_index[-1]).normalize()
                same_session_mask = session_index.normalize() == session_anchor
                if bool(getattr(same_session_mask, "any", lambda: False)()):
                    session_volume_values = frame.loc[same_session_mask, "volume"]
                    session_volume_series = pd.Series(session_volume_values, copy=False)
                    session_volume_numeric_values = pd.to_numeric(session_volume_series, errors="coerce")
                    session_volume_numeric = pd.Series(session_volume_numeric_values, copy=False)
                    session_volume = session_volume_numeric.fillna(0.0).sum()
                    session_total_volume = safe_float(session_volume)

        quote_last = safe_float(quote.get("last")) if quote_is_fresh else None
        quote_bid = safe_float(quote.get("bid")) if quote_is_fresh else None
        quote_ask = safe_float(quote.get("ask")) if quote_is_fresh else None
        quote_mark = safe_float(quote.get("mark")) if quote_is_fresh else None
        quote_mid = safe_float(quote.get("mid")) if quote_is_fresh else None
        quote_open = safe_float(quote.get("open"))
        quote_close = safe_float(quote.get("close"))
        quote_total_volume = safe_float(quote.get("total_volume")) if quote_is_fresh else None
        # data_feed._normalize_quote reads percent_change / net_change with
        # numeric.first_float, which yields None (not 0.0) when both Schwab
        # fields are absent or NaN, so a 0.0 here is always a real
        # flat-session reading rather than a sentinel.
        cached_percent_change = safe_float(quote.get("percent_change"))
        cached_net_change = safe_float(quote.get("net_change"))
        candidate_percent_change = safe_float((candidate_row or {}).get("change_from_open"))
        candidate_close = safe_float((candidate_row or {}).get("close"))
        regular_session_active = self.data.is_regular_session(sessions.now_et())
        display_total_volume = quote_total_volume
        if display_total_volume is None:
            display_total_volume = session_total_volume
        last_price = quote_last
        if last_price is None:
            last_price = safe_float(latest_bar.get("close"))
        display_close = quote_close
        if not regular_session_active and candidate_close is not None:
            display_close = candidate_close
        if display_close is None and candidate_close is not None:
            display_close = candidate_close
        if display_close is None and len(bars) >= 2:
            display_close = safe_float(bars[-2].get("close"))
        session_reference_close = quote_close
        if not regular_session_active and candidate_percent_change is not None:
            percent_change = candidate_percent_change
        else:
            percent_change = cached_percent_change
        if percent_change is None:
            percent_change = candidate_percent_change
        if percent_change is None and last_price not in (None, 0.0) and session_reference_close not in (None, 0.0):
            percent_change = ((last_price - session_reference_close) / session_reference_close) * 100.0
        net_change = cached_net_change
        if net_change is None and last_price is not None and session_reference_close not in (None, 0.0):
            net_change = last_price - session_reference_close
        display_mark = quote_mark if quote_mark is not None else last_price
        display_mid = quote_mid if quote_mid is not None else display_mark

        current_price = last_price
        if current_price is None:
            current_price = safe_float((sr_row or {}).get("price"))
        if current_price is None:
            current_price = safe_float(latest_bar.get("close"))
        return {
            "last": last_price,
            "bid": quote_bid,
            "ask": quote_ask,
            "mid": display_mid,
            "mark": display_mark,
            "open": quote_open,
            "close": display_close,
            "net_change": net_change,
            "percent_change": percent_change,
            "total_volume": display_total_volume,
            "is_fresh": quote_is_fresh,
        }, current_price

    def _snapshot_ladder(self, sr_row: dict[str, Any] | None, current_price: float | None) -> dict[str, Any]:
        """The chart's S/R ladder from ``sr_row``: the nearest support and
        resistance, the rungs beyond them, collapsed at the row's spacing, and
        the next rung on each side. Without a row the prices are None and the
        rungs empty."""
        support_prices: list[float] = []
        resistance_prices: list[float] = []
        next_support = None
        next_resistance = None
        nearest_support = None
        nearest_resistance = None
        if sr_row:
            # The S/R build's spacing. A row without one (an empty context
            # carries 0) spaces its rungs at the config's price arms (no ATR
            # here), and without a price, which the arms scale by, only drops
            # repeated prices (collapse_price_ladder's floor).
            row_gap = safe_float(sr_row.get("side_tolerance"))
            if row_gap:
                ladder_min_gap = row_gap
            elif current_price is not None:
                ladder_min_gap = effective_side_tolerance(self.config.support_resistance, current_price)
            else:
                ladder_min_gap = 0.0
            nearest_support = safe_float(sr_row.get("nearest_support"))
            nearest_resistance = safe_float(sr_row.get("nearest_resistance"))

            support_prices = sorted(
                [float(v) for v in (sr_row.get("supports") or []) if safe_float(v) not in (None, 0.0)],
                reverse=True,
            )
            resistance_prices = sorted(
                [float(v) for v in (sr_row.get("resistances") or []) if safe_float(v) not in (None, 0.0)]
            )

            if nearest_support is not None:
                support_prices.append(float(nearest_support))
            if nearest_resistance is not None:
                resistance_prices.append(float(nearest_resistance))

            support_prices = collapse_price_ladder(support_prices, reverse=True, min_gap=ladder_min_gap)
            resistance_prices = collapse_price_ladder(resistance_prices, reverse=False, min_gap=ladder_min_gap)
            support_anchor_prices = list(support_prices)
            resistance_anchor_prices = list(resistance_prices)

            if nearest_support is None and support_prices:
                nearest_support = support_prices[0]
            if nearest_resistance is None and resistance_prices:
                nearest_resistance = resistance_prices[0]

            support_prices = [
                price for price in support_anchor_prices
                if nearest_support is None or abs(price - nearest_support) > max(1e-9, ladder_min_gap)
            ]
            resistance_prices = [
                price for price in resistance_anchor_prices
                if nearest_resistance is None or abs(price - nearest_resistance) > max(1e-9, ladder_min_gap)
            ]

            next_support = support_prices[0] if support_prices else None
            next_resistance = resistance_prices[0] if resistance_prices else None
        return {
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resistance,
            "supports": support_prices,
            "resistances": resistance_prices,
            "next_support": next_support,
            "next_resistance": next_resistance,
        }

    def _snapshot_technicals(
        self, symbol: str, frame: pd.DataFrame | None, current_price: float | None,
    ) -> tuple[TechnicalLevelsContext | None, dict[str, Any]]:
        """The technical levels built on the strategy's LTF frame, and the
        chart's ``technicals`` payload of them. A failed build is logged
        (``technical_overlay``) and gives no context and an empty payload."""
        technical_payload: dict[str, Any] = {}
        # Build technical levels (fib extensions/retracements, AVWAP,
        # Bollinger, ADX, channels, trendlines, etc.) on the strategy's LTF
        # frame so all overlays render at LTF-derived prices. Strategies
        # with default LTF=1 keep using the 1m streamed frame; strategies
        # with non-1m LTF (e.g. peer_confirmed_key_levels at LTF=5m) get
        # 5m-derived fibs/AVWAP/etc. matching the LTF chart bars.
        ltf_min_for_tech = self.strategy.ltf_minutes()
        if ltf_min_for_tech == 1:
            tech_frame = frame
        elif symbol:
            tech_frame = self.data.get_merged(symbol, timeframe=f"{ltf_min_for_tech}min", with_indicators=True)
        else:
            tech_frame = frame
        tech_ctx = None  # Stays None when tech_frame is empty (warmup path) or build_technical_levels_context raises; downstream readers (technical_payload, divergence_lines) all guard on `tech_ctx is not None`.
        if tech_frame is not None and not tech_frame.empty:
            tl_cfg = self.config.technical_levels
            sr_cfg = self.config.support_resistance
            try:
                # tech_frame itself, not a filtered copy: the lines come back
                # positioned in the frame passed, and tech_frame is the frame
                # the LTF chart's bars (and their abs_index) are cut from.
                tech_ctx = build_technical_levels_context(
                    tech_frame,
                    current_price=current_price,
                    pivot_span=int(getattr(sr_cfg, "structure_ltf_pivot_span", getattr(sr_cfg, "pivot_span", 2)) or 2),
                    fib_lookback_bars=int(getattr(tl_cfg, "fib_lookback_bars", 120) or 120),
                    fib_min_impulse_atr=float(getattr(tl_cfg, "fib_min_impulse_atr", 1.25) or 1.25),
                    anchored_vwap_impulse_lookback_bars=(int(getattr(tl_cfg, "anchored_vwap_impulse_lookback_bars")) if getattr(tl_cfg, "anchored_vwap_impulse_lookback_bars", None) is not None else None),
                    anchored_vwap_min_impulse_atr=(float(getattr(tl_cfg, "anchored_vwap_min_impulse_atr")) if getattr(tl_cfg, "anchored_vwap_min_impulse_atr", None) is not None else None),
                    anchored_vwap_pivot_span=(int(getattr(tl_cfg, "anchored_vwap_pivot_span")) if getattr(tl_cfg, "anchored_vwap_pivot_span", None) is not None else None),
                    trendline_lookback_bars=int(getattr(tl_cfg, "trendline_lookback_bars", 120) or 120),
                    trendline_min_touches=int(getattr(tl_cfg, "trendline_min_touches", 3) or 3),
                    trendline_atr_tolerance_mult=float(getattr(tl_cfg, "trendline_atr_tolerance_mult", 0.35) or 0.35),
                    trendline_breakout_buffer_atr_mult=float(getattr(tl_cfg, "trendline_breakout_buffer_atr_mult", 0.65)),
                    channel_lookback_bars=int(getattr(tl_cfg, "channel_lookback_bars", 120) or 120),
                    channel_min_touches=int(getattr(tl_cfg, "channel_min_touches", 3) or 3),
                    channel_atr_tolerance_mult=float(getattr(tl_cfg, "channel_atr_tolerance_mult", 0.35) or 0.35),
                    channel_parallel_slope_frac=float(getattr(tl_cfg, "channel_parallel_slope_frac", 0.12) or 0.12),
                    channel_min_gap_atr_mult=float(getattr(tl_cfg, "channel_min_gap_atr_mult", 0.80) or 0.80),
                    channel_min_gap_pct=float(getattr(tl_cfg, "channel_min_gap_pct", 0.0025) or 0.0025),
                    bollinger_length=int(getattr(tl_cfg, "bollinger_length", 20) or 20),
                    bollinger_std_mult=float(getattr(tl_cfg, "bollinger_std_mult", 2.0) or 2.0),
                    bollinger_squeeze_width_pct=float(getattr(tl_cfg, "bollinger_squeeze_width_pct", 0.060) or 0.060),
                    atr_expansion_lookback=int(getattr(tl_cfg, "atr_expansion_lookback", 5) or 5),
                    adx_length=int(getattr(tl_cfg, "adx_length", 14) or 14),
                    obv_ema_length=int(getattr(tl_cfg, "obv_ema_length", 20) or 20),
                    divergence_rsi_length=int(getattr(tl_cfg, "divergence_rsi_length", 14) or 14),
                    # As configured, as the strategy and the HTF build read
                    # them: `or <default>` drew a configured 0 at the default
                    # (2026-09-25), and a null fails the build.
                    divergence_rsi_min_delta=float(tl_cfg.divergence_rsi_min_delta),
                    divergence_obv_min_volume_frac=float(getattr(tl_cfg, "divergence_obv_min_volume_frac", 0.50) or 0.50),
                    divergence_pivot_lookback=int(tl_cfg.divergence_pivot_lookback),
                    # As configured: `or 8` drew a configured 0 at age 8 (2026-09-24).
                    divergence_max_age_bars=int(tl_cfg.divergence_max_age_bars),
                    divergence_min_price_move_pct=float(tl_cfg.divergence_min_price_move_pct),
                    fib_enabled=bool(getattr(tl_cfg, "fib_enabled", True)),
                    channel_enabled=bool(getattr(tl_cfg, "channel_enabled", True)),
                    trendline_enabled=bool(getattr(tl_cfg, "trendline_enabled", True)),
                    adx_enabled=bool(getattr(tl_cfg, "adx_enabled", True)),
                    anchored_vwap_enabled=bool(getattr(tl_cfg, "anchored_vwap_enabled", True)),
                    atr_context_enabled=bool(getattr(tl_cfg, "atr_context_enabled", True)),
                    obv_enabled=bool(getattr(tl_cfg, "obv_enabled", True)),
                    divergence_enabled=bool(getattr(tl_cfg, "divergence_enabled", True)),
                    bollinger_enabled=bool(getattr(tl_cfg, "bollinger_enabled", True)),
                )
            except Exception:
                self.log_component_failure(
                    "technical_overlay",
                    "Dashboard technical overlay build failed for %s",
                    symbol,
                )
                tech_ctx = None
            if tech_ctx is not None:
                technical_payload = {
                    "fib_direction": str(getattr(tech_ctx, "fib_direction", "neutral") or "neutral"),
                    "fib_bullish_1272": safe_float(getattr(tech_ctx, "fib_bullish_1272", None)),
                    "fib_bullish_1618": safe_float(getattr(tech_ctx, "fib_bullish_1618", None)),
                    "fib_bearish_1272": safe_float(getattr(tech_ctx, "fib_bearish_1272", None)),
                    "fib_bearish_1618": safe_float(getattr(tech_ctx, "fib_bearish_1618", None)),
                    "fib_bullish_382": safe_float(getattr(tech_ctx, "fib_bullish_382", None)),
                    "fib_bullish_500": safe_float(getattr(tech_ctx, "fib_bullish_500", None)),
                    "fib_bullish_618": safe_float(getattr(tech_ctx, "fib_bullish_618", None)),
                    "fib_bullish_786": safe_float(getattr(tech_ctx, "fib_bullish_786", None)),
                    "fib_bearish_382": safe_float(getattr(tech_ctx, "fib_bearish_382", None)),
                    "fib_bearish_500": safe_float(getattr(tech_ctx, "fib_bearish_500", None)),
                    "fib_bearish_618": safe_float(getattr(tech_ctx, "fib_bearish_618", None)),
                    "fib_bearish_786": safe_float(getattr(tech_ctx, "fib_bearish_786", None)),
                    "anchored_vwap_open": safe_float(getattr(tech_ctx, "anchored_vwap_open", None)),
                    "anchored_vwap_bullish_impulse": safe_float(getattr(tech_ctx, "anchored_vwap_bullish_impulse", None)),
                    "anchored_vwap_bearish_impulse": safe_float(getattr(tech_ctx, "anchored_vwap_bearish_impulse", None)),
                    "anchored_vwap_bias": str(getattr(tech_ctx, "anchored_vwap_bias", "neutral") or "neutral"),
                    "adx": safe_float(getattr(tech_ctx, "adx", None)),
                    "plus_di": safe_float(getattr(tech_ctx, "plus_di", None)),
                    "minus_di": safe_float(getattr(tech_ctx, "minus_di", None)),
                    "dmi_bias": str(getattr(tech_ctx, "dmi_bias", "neutral") or "neutral"),
                    "adx_rising": bool(getattr(tech_ctx, "adx_rising", False)),
                    "atr14": safe_float(getattr(tech_ctx, "atr14", None)),
                    "atr_pct": safe_float(getattr(tech_ctx, "atr_pct", None)),
                    "atr_expansion_mult": safe_float(getattr(tech_ctx, "atr_expansion_mult", None)),
                    "atr_stretch_vwap_mult": safe_float(getattr(tech_ctx, "atr_stretch_vwap_mult", None)),
                    "atr_stretch_ema20_mult": safe_float(getattr(tech_ctx, "atr_stretch_ema20_mult", None)),
                    "obv": safe_float(getattr(tech_ctx, "obv", None)),
                    "obv_ema": safe_float(getattr(tech_ctx, "obv_ema", None)),
                    "obv_bias": str(getattr(tech_ctx, "obv_bias", "neutral") or "neutral"),
                    "rsi14": safe_float(getattr(tech_ctx, "rsi14", None)),
                    "bullish_rsi_divergence": getattr(tech_ctx, "bullish_rsi_divergence", None) is not None,
                    "bearish_rsi_divergence": getattr(tech_ctx, "bearish_rsi_divergence", None) is not None,
                    "bullish_obv_divergence": getattr(tech_ctx, "bullish_obv_divergence", None) is not None,
                    "bearish_obv_divergence": getattr(tech_ctx, "bearish_obv_divergence", None) is not None,
                    "bullish_hidden_rsi_divergence": getattr(tech_ctx, "bullish_hidden_rsi_divergence", None) is not None,
                    "bearish_hidden_rsi_divergence": getattr(tech_ctx, "bearish_hidden_rsi_divergence", None) is not None,
                    "bullish_hidden_obv_divergence": getattr(tech_ctx, "bullish_hidden_obv_divergence", None) is not None,
                    "bearish_hidden_obv_divergence": getattr(tech_ctx, "bearish_hidden_obv_divergence", None) is not None,
                    "counter_divergence_bias": str(getattr(tech_ctx, "counter_divergence_bias", "neutral") or "neutral"),
                    "bollinger_mid": safe_float(getattr(tech_ctx, "bollinger_mid", None)),
                    "bollinger_upper": safe_float(getattr(tech_ctx, "bollinger_upper", None)),
                    "bollinger_lower": safe_float(getattr(tech_ctx, "bollinger_lower", None)),
                    "bollinger_width_pct": safe_float(getattr(tech_ctx, "bollinger_width_pct", None)),
                    "bollinger_percent_b": safe_float(getattr(tech_ctx, "bollinger_percent_b", None)),
                    "bollinger_zscore": safe_float(getattr(tech_ctx, "bollinger_zscore", None)),
                    "bollinger_squeeze": bool(getattr(tech_ctx, "bollinger_squeeze", False)),
                    "bollinger_upper_reject": bool(getattr(tech_ctx, "bollinger_upper_reject", False)),
                    "bollinger_lower_reject": bool(getattr(tech_ctx, "bollinger_lower_reject", False)),
                    "channel": {
                        "valid": bool(getattr(getattr(tech_ctx, "channel", None), "valid", False)),
                        "bias": str(getattr(getattr(tech_ctx, "channel", None), "bias", "neutral") or "neutral"),
                        "lower": safe_float(getattr(getattr(tech_ctx, "channel", None), "lower", None)),
                        "upper": safe_float(getattr(getattr(tech_ctx, "channel", None), "upper", None)),
                        "mid": safe_float(getattr(getattr(tech_ctx, "channel", None), "mid", None)),
                        "position_pct": safe_float(getattr(getattr(tech_ctx, "channel", None), "position_pct", None)),
                        "lower_line": technical_line_payload(getattr(getattr(tech_ctx, "channel", None), "lower_line", None)),
                        "upper_line": technical_line_payload(getattr(getattr(tech_ctx, "channel", None), "upper_line", None)),
                        "mid_line": technical_line_payload(getattr(getattr(tech_ctx, "channel", None), "mid_line", None)),
                    },
                    "support_trendline": technical_line_payload(getattr(tech_ctx, "support_trendline", None)),
                    "resistance_trendline": technical_line_payload(getattr(tech_ctx, "resistance_trendline", None)),
                    "trendline_break_up": bool(getattr(tech_ctx, "trendline_break_up", False)),
                    "trendline_break_down": bool(getattr(tech_ctx, "trendline_break_down", False)),
                    "support_respected": bool(getattr(tech_ctx, "support_respected", False)),
                    "resistance_respected": bool(getattr(tech_ctx, "resistance_respected", False)),
                }
        return tech_ctx, technical_payload

    @staticmethod
    def _position_markers(position_row: dict[str, Any] | None) -> dict[str, Any]:
        """The chart's markers for ``position_row``: an equity position's
        entry, stop and target, an option position's strikes (its stop and
        target are option prices, off the underlying's axis), and either
        one's breakeven."""
        is_option = is_option_asset(position_row)
        allows_underlying_markers = bool(position_row) and not is_option
        return {
            "asset_type": asset_type_of(position_row) if position_row else None,
            "show_underlying_lines": allows_underlying_markers,
            "side": (position_row or {}).get("side"),
            "entry": safe_float((position_row or {}).get("entry_price")) if allows_underlying_markers else None,
            "stop": safe_float((position_row or {}).get("stop_price")) if allows_underlying_markers else None,
            "target": safe_float((position_row or {}).get("target_price")) if allows_underlying_markers else None,
            # Breakeven is in underlying-price units for both stocks (from entry)
            # and options (via metadata['breakeven_underlying']), so it's safe to
            # draw on the underlying chart regardless of asset_type.
            "breakeven": safe_float((position_row or {}).get("breakeven")),
            "entry_time": (position_row or {}).get("entry_time"),
            # Option-specific: strikes in underlying-price units. Drawn on the
            # underlying chart for an option position (is_option_asset) because the
            # bot's stop_price/target_price are in OPTION-price units and can't
            # be plotted on the underlying's axis.
            "option_type": (position_row or {}).get("option_type") if is_option else None,
            "long_strike": safe_float((position_row or {}).get("long_strike")) if is_option else None,
            "short_strike": safe_float((position_row or {}).get("short_strike")) if is_option else None,
            "option_strike": safe_float((position_row or {}).get("option_strike")) if is_option else None,
        }

    def _snapshot_htf_overlays(
        self,
        symbol: str,
        compact_chart_profile: DashboardChartConfig,
        expanded_chart_profile: DashboardChartConfig,
        chart_wants_rsi_div: bool,
        *,
        allow_refresh: bool,
    ) -> tuple[HTFContext | None, list[dict[str, Any]]]:
        """The HTF context, read if a chart draws its FVGs (and the strategy's
        request builds them) or its RSI divergence lines, and its FVG overlay.
        A failure is logged (``htf_fair_value_gaps_collect``) and gives no
        context and no gaps."""
        htf_fair_value_gaps: list[dict[str, Any]] = []
        htf_ctx = None
        try:
            # The strategy's HTF FVG arguments, part of the context's cache key.
            htf_fvg_request = self.strategy.htf_fvg_request()
            include_fair_value_gaps = bool(htf_fvg_request["include_fair_value_gaps"])
            chart_wants_htf_fvgs = bool(compact_chart_profile.show_htf_fair_value_gaps) or bool(expanded_chart_profile.show_htf_fair_value_gaps)
            need_htf_ctx = (include_fair_value_gaps and chart_wants_htf_fvgs) or chart_wants_rsi_div
            if need_htf_ctx:
                htf_ctx = self.data.get_htf_context(
                    symbol,
                    timeframe_minutes=self.strategy.htf_minutes(),
                    lookback_days=self.strategy.htf_lookback_days(),
                    **self._chart_htf_level_request(),
                    allow_refresh=allow_refresh,
                    use_prior_day_high_low=bool(getattr(self.config.support_resistance, "use_prior_day_high_low", True)),
                    use_prior_week_high_low=bool(getattr(self.config.support_resistance, "use_prior_week_high_low", True)),
                    **htf_fvg_request,
                )
                if include_fair_value_gaps and chart_wants_htf_fvgs and htf_ctx is not None:
                    htf_min = self.strategy.htf_minutes()
                    htf_tf_minutes = int(getattr(htf_ctx, "timeframe_minutes", htf_min) or htf_min)
                    for gap in list(getattr(htf_ctx, "bullish_fvgs", []) or []) + list(getattr(htf_ctx, "bearish_fvgs", []) or []):
                        payload_fvg = fvg_payload(gap)
                        if payload_fvg is not None:
                            payload_fvg["timeframe"] = f"{htf_tf_minutes}m"
                            htf_fair_value_gaps.append(payload_fvg)
        except Exception:
            self.log_component_failure(
                "htf_fair_value_gaps_collect",
                "Dashboard HTF context / fair value gaps collect failed for %s",
                symbol,
            )
            htf_fair_value_gaps = []
            htf_ctx = None
        return htf_ctx, htf_fair_value_gaps

    def _snapshot_ltf_fair_value_gaps(
        self,
        symbol: str,
        frame: pd.DataFrame | None,
        frame_close: float,
        compact_chart_profile: DashboardChartConfig,
        expanded_chart_profile: DashboardChartConfig,
    ) -> list[dict[str, Any]]:
        """The LTF FVG overlay, when the config builds LTF gaps and a chart
        draws them: the strategy's context at ``frame_close``, each gap
        anchored on the LTF frame. A failure is logged
        (``ltf_fair_value_gaps_collect``) and gives no gaps."""
        ltf_fair_value_gaps: list[dict[str, Any]] = []
        try:
            sr_cfg = getattr(self.config, "support_resistance", None)
            include_ltf_fvgs = bool(getattr(sr_cfg, "ltf_fair_value_gaps_enabled", False)) if sr_cfg is not None else False
            chart_wants_ltf_fvgs = bool(compact_chart_profile.show_ltf_fair_value_gaps) or bool(expanded_chart_profile.show_ltf_fair_value_gaps)
            if include_ltf_fvgs and chart_wants_ltf_fvgs:
                ltf_min_for_fvg = self.strategy.ltf_minutes()
                fvg_ctx = self.data.get_fair_value_gap_context(
                    symbol,
                    timeframe_minutes=ltf_min_for_fvg,
                    current_price=frame_close,
                    **self.strategy.ltf_fvg_request(),
                )
                if fvg_ctx is not None:
                    if ltf_min_for_fvg == 1:
                        anchor_frame = frame if frame is not None and not frame.empty else self.data.get_merged(symbol, with_indicators=True)
                    else:
                        anchor_frame = self.data.get_merged(symbol, timeframe=f"{ltf_min_for_fvg}min", with_indicators=True)
                    for gap in list(getattr(fvg_ctx, "bullish_fvgs", []) or []) + list(getattr(fvg_ctx, "bearish_fvgs", []) or []):
                        payload_fvg = fvg_payload(gap)
                        if payload_fvg is not None:
                            payload_fvg["timeframe"] = f"{ltf_min_for_fvg}m"
                            payload_fvg["anchor_abs_index"] = fvg_anchor_abs_index(anchor_frame, payload_fvg.get("first_seen"))
                            ltf_fair_value_gaps.append(payload_fvg)
        except Exception:
            self.log_component_failure(
                "ltf_fair_value_gaps_collect",
                "Dashboard LTF fair value gaps collect failed for %s",
                symbol,
            )
            ltf_fair_value_gaps = []
        return ltf_fair_value_gaps

    def _snapshot_order_blocks(
        self,
        symbol: str,
        frame: pd.DataFrame | None,
        frame_close: float,
        compact_chart_profile: DashboardChartConfig,
        expanded_chart_profile: DashboardChartConfig,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The HTF and LTF order-block overlays, each when the config builds it
        and a chart draws it: the strategy's request at ``frame_close``. A
        failure is logged (``htf_order_blocks_collect`` /
        ``ltf_order_blocks_collect``) and gives no blocks on that side."""
        # Order blocks. Same payload shape as FVGs (lower/upper/midpoint/size/
        # direction/filled_pct/first_seen/last_seen) — `fvg_payload`
        # is reused since it's shape-driven, not type-driven. Frontend reads
        # `htf_order_blocks` and `ltf_order_blocks` separately and renders
        # them with dashed-stroke styling vs FVGs' solid-fill styling.
        # One request (the strategy's tuning knobs) for both blocks below.
        sr_cfg = getattr(self.config, "support_resistance", None)
        ob_request = self.strategy.order_block_request()

        htf_order_blocks: list[dict[str, Any]] = []
        try:
            include_htf_obs = bool(getattr(sr_cfg, "htf_order_blocks_enabled", False)) if sr_cfg is not None else False
            chart_wants_htf_obs = bool(compact_chart_profile.show_htf_order_blocks) or bool(expanded_chart_profile.show_htf_order_blocks)
            if include_htf_obs and chart_wants_htf_obs:
                htf_minutes = self.strategy.htf_minutes()
                # Cycle-cached: hits get_order_block_context's cache when the
                # strategy already computed it earlier in the same cycle.
                ob_ctx_htf = self.data.get_order_block_context(
                    symbol,
                    timeframe_minutes=htf_minutes,
                    current_price=frame_close,
                    **ob_request,
                )
                for ob in list(getattr(ob_ctx_htf, "bullish_obs", []) or []) + list(getattr(ob_ctx_htf, "bearish_obs", []) or []):
                    payload_ob = fvg_payload(ob)
                    if payload_ob is not None:
                        payload_ob["timeframe"] = f"{int(htf_minutes)}m"
                        payload_ob["kind"] = "ob"
                        payload_ob["mode"] = ob_ctx_htf.mode
                        htf_order_blocks.append(payload_ob)
        except Exception:
            self.log_component_failure(
                "htf_order_blocks_collect",
                "Dashboard HTF order blocks collect failed for %s",
                symbol,
            )
            htf_order_blocks = []

        ltf_order_blocks: list[dict[str, Any]] = []
        try:
            include_ltf_obs = bool(getattr(sr_cfg, "ltf_order_blocks_enabled", False)) if sr_cfg is not None else False
            chart_wants_ltf_obs = bool(compact_chart_profile.show_ltf_order_blocks) or bool(expanded_chart_profile.show_ltf_order_blocks)
            if include_ltf_obs and chart_wants_ltf_obs:
                # Cycle-cached: same cache as the strategy uses when it calls
                # `_ltf_order_block_context` during entry evaluation.
                ltf_min_for_ob = self.strategy.ltf_minutes()
                ob_ctx_ltf = self.data.get_order_block_context(
                    symbol,
                    timeframe_minutes=ltf_min_for_ob,
                    current_price=frame_close,
                    **ob_request,
                )
                # We still need an in-scope LTF frame for the anchor_abs_index
                # lookup that drives chart placement; the OB context alone
                # doesn't carry frame indices.
                if ltf_min_for_ob == 1:
                    ltf_frame = frame if frame is not None and not frame.empty else self.data.get_merged(symbol, with_indicators=True)
                else:
                    ltf_frame = self.data.get_merged(symbol, timeframe=f"{ltf_min_for_ob}min", with_indicators=True)
                for ob in list(getattr(ob_ctx_ltf, "bullish_obs", []) or []) + list(getattr(ob_ctx_ltf, "bearish_obs", []) or []):
                    payload_ob = fvg_payload(ob)
                    if payload_ob is not None:
                        payload_ob["timeframe"] = f"{ltf_min_for_ob}m"
                        payload_ob["kind"] = "ob"
                        payload_ob["mode"] = ob_ctx_ltf.mode
                        payload_ob["anchor_abs_index"] = fvg_anchor_abs_index(ltf_frame, payload_ob.get("first_seen"))
                        ltf_order_blocks.append(payload_ob)
        except Exception:
            self.log_component_failure(
                "ltf_order_blocks_collect",
                "Dashboard LTF order blocks collect failed for %s",
                symbol,
            )
            ltf_order_blocks = []
        return htf_order_blocks, ltf_order_blocks

    def _snapshot_divergence_lines(
        self,
        symbol: str,
        tech_ctx: TechnicalLevelsContext | None,
        htf_ctx: HTFContext | None,
        compact_chart_profile: DashboardChartConfig,
        expanded_chart_profile: DashboardChartConfig,
        chart_wants_rsi_div: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The chart's divergence lines: the LTF ones from ``tech_ctx`` (RSI and
        OBV, as the profiles draw them) and the HTF RSI ones from ``htf_ctx``.
        A failure is logged (``divergence_lines_collect``) and gives no lines
        on either side."""
        # Divergence trendlines (RSI / OBV, regular / hidden, bullish / bearish)
        # for the price chart. LTF divergences come from tech_ctx (built off
        # the strategy's primary frame), HTF divergences come from htf_ctx
        # (read once by _snapshot_htf_overlays, when divergence or FVG rendering
        # is enabled). Each entry is a DivergenceMatch.to_payload() dict —
        # frontend draws a line connecting the two pivot points and color-
        # codes by direction (green=bullish/red=bearish), kind (solid=regular,
        # dashed=hidden), indicator (RSI heavier stroke than OBV).
        ltf_divergence_lines: list[dict[str, Any]] = []
        htf_divergence_lines: list[dict[str, Any]] = []
        try:
            chart_wants_obv_div = bool(compact_chart_profile.show_obv_divergence) or bool(expanded_chart_profile.show_obv_divergence)
            if tech_ctx is not None and (chart_wants_rsi_div or chart_wants_obv_div):
                for attr_name in (
                    "bullish_rsi_divergence", "bearish_rsi_divergence",
                    "bullish_hidden_rsi_divergence", "bearish_hidden_rsi_divergence",
                    "bullish_obv_divergence", "bearish_obv_divergence",
                    "bullish_hidden_obv_divergence", "bearish_hidden_obv_divergence",
                ):
                    match = getattr(tech_ctx, attr_name, None)
                    if match is None:
                        continue
                    indicator = getattr(match, "indicator", "rsi")
                    if indicator == "rsi" and not chart_wants_rsi_div:
                        continue
                    if indicator == "obv" and not chart_wants_obv_div:
                        continue
                    line = match.to_payload()
                    line["timeframe"] = "ltf"
                    ltf_divergence_lines.append(line)
            # HTF divergence lines — only RSI is computed at HTF level
            # (build_htf_context populates the four HTF RSI fields; OBV
            # divergence is intentionally LTF-only since OBV is volume-driven
            # and HTF resampling smears the signal).
            if htf_ctx is not None and chart_wants_rsi_div:
                for attr_name in (
                    "bullish_rsi_divergence", "bearish_rsi_divergence",
                    "bullish_hidden_rsi_divergence", "bearish_hidden_rsi_divergence",
                ):
                    match = getattr(htf_ctx, attr_name, None)
                    if match is None:
                        continue
                    line = match.to_payload()
                    line["timeframe"] = "htf"
                    htf_divergence_lines.append(line)
        except Exception:
            self.log_component_failure(
                "divergence_lines_collect",
                "Dashboard divergence-line collect failed for %s",
                symbol,
            )
            ltf_divergence_lines = []
            htf_divergence_lines = []
        return ltf_divergence_lines, htf_divergence_lines

    def strategy_level_zones(
        self,
        symbol: str,
        frame: pd.DataFrame | None,
        current_price: float | None,
        support_prices: list[float] | None = None,
        resistance_prices: list[float] | None = None,
        broken_support_price: float | None = None,
        broken_resistance_price: float | None = None,
        pending_support_price: float | None = None,
        pending_resistance_price: float | None = None,
        allow_htf_refresh: bool = True,
    ) -> list[dict[str, Any]]:
        """The chart's key-level zones: the strategy's level candidates (the S/R
        row's levels for a strategy that allows the generic fallback) as zones
        sized by its hooks, read here with the HTF context and the LTF frame
        its spec names, then classified and picked by
        ``dashboard_zones.build_level_zones``."""
        try:
            level_ctx = self.strategy.dashboard_level_context_spec() or {}
        except Exception:
            # Reported, not replaced by the generic 60m / 60-day build below:
            # that build refreshes a key nothing else keeps, so every symbol
            # fetched from Schwab each hour on a spec error.
            self.log_component_failure("level_context_spec", "Level-context spec failed for %s", symbol)
            return []
        if not isinstance(level_ctx, dict):
            level_ctx = {}

        # Generic-fallback anchors from the S/R row, each tagged with its role
        # and the S/R builder's own verdict on its flip: the nearest levels
        # hold their role, broken_* flipped on the builder's trading-mode
        # confirmation, pending_* have been crossed with the flip still
        # unconfirmed (they keep their original role). Until 2026-09-23 every
        # support anchor was tagged nearest_htf_support, so a confirmed
        # breakout-retest level drew as an ordinary "HS · Original" support,
        # and pending levels were not drawn at all. A flipped or pending level
        # is listed ahead of a plain one at the same price, which it labels
        # more precisely.
        support_anchors = level_anchors([
            (broken_resistance_price, "broken_htf_resistance", True),
            (pending_support_price, "pending_htf_support", False),
            *((price, "nearest_htf_support", False) for price in (support_prices or [])),
        ])
        resistance_anchors = level_anchors([
            (broken_support_price, "broken_htf_support", True),
            (pending_resistance_price, "pending_htf_resistance", False),
            *((price, "nearest_htf_resistance", False) for price in (resistance_prices or [])),
        ])

        close = safe_float(current_price)
        if close is None and frame is not None and not frame.empty:
            close = safe_float(frame.iloc[-1].get("close"))
        if close is None or close <= 0:
            return []

        tf = max(1, int(level_ctx.get("timeframe_minutes", 60) or 60))
        lookback_days = max(1, int(level_ctx.get("lookback_days", 60) or 60))
        pivot_span = max(1, int(level_ctx.get("pivot_span", 2) or 2))
        max_lvls = max(1, int(level_ctx.get("max_levels_per_side", 6) or 6))
        # The spec's tolerances as it gives them (support_resistance's, checked
        # at load, unless the strategy declares htf_* ones), as
        # _chart_htf_level_request reads them; support_resistance's for a
        # spec without them. A 0 read as 0.35 / 0.003 until 2026-09-26.
        atr_tol = float(level_ctx.get("atr_tolerance_mult", self.config.support_resistance.atr_tolerance_mult))
        pct_tol = float(level_ctx.get("pct_tolerance", self.config.support_resistance.pct_tolerance))
        stop_atr = float(level_ctx.get("stop_buffer_atr_mult", 0.25) or 0.25)
        ema_fast_span = max(1, int(level_ctx.get("ema_fast_span", 50) or 50))
        ema_slow_span = max(1, int(level_ctx.get("ema_slow_span", 200) or 200))
        sr_cfg = getattr(self.config, "support_resistance", None)
        use_prior_day_high_low = bool(getattr(sr_cfg, "use_prior_day_high_low", True)) if sr_cfg is not None else True
        use_prior_week_high_low = bool(getattr(sr_cfg, "use_prior_week_high_low", True)) if sr_cfg is not None else True

        htf = self.data.get_htf_context(
            symbol,
            timeframe_minutes=tf,
            lookback_days=lookback_days,
            pivot_span=pivot_span,
            max_levels_per_side=max_lvls,
            atr_tolerance_mult=atr_tol,
            pct_tolerance=pct_tol,
            stop_buffer_atr_mult=stop_atr,
            ema_fast_span=ema_fast_span,
            ema_slow_span=ema_slow_span,
            allow_refresh=allow_htf_refresh,
            use_prior_day_high_low=use_prior_day_high_low,
            use_prior_week_high_low=use_prior_week_high_low,
            **self.strategy.htf_fvg_request(),
        )
        if htf is None:
            return []

        ltf_min = max(1, int(level_ctx.get("ltf_minutes", 5) or 5))
        timeframe = "1min" if ltf_min <= 1 else f"{ltf_min}min"
        ltf = self.data.get_merged(symbol, timeframe=timeframe, with_indicators=True)

        # The ATR key_levels' _select_level sizes its zones with, read by the
        # same call so the overlay cannot drift from it. Until 2026-09-26 a
        # hand-rolled copy read a zero, negative or -inf LTF ATR as 0.15% of
        # the close and kept +inf, where the strategy uses a finite reading
        # as read and the HTF ATR for an infinite one.
        atr = last_bar_atr(ltf, close, fallback_atr=getattr(htf, "atr14", None))
        min_level_score = float(level_ctx.get("min_level_score", 4.0) or 4.0)
        tolerance_pct = float(level_ctx.get("level_round_number_tolerance_pct", 0.0020) or 0.0020)
        base_zone_half_width = max(
            float(level_ctx.get("base_zone_atr_mult", 0.20) or 0.20) * float(atr),
            float(close) * float(level_ctx.get("base_zone_pct", 0.0015) or 0.0015),
            0.01,
        )

        long_candidates: list[dict[str, Any]] = []
        short_candidates: list[dict[str, Any]] = []
        selected_long_price = None
        selected_short_price = None
        selected_zone_match_tolerance = max(float(base_zone_half_width) * 0.75, float(close) * float(tolerance_pct) * 0.5, 0.01)

        try:
            if not ltf.empty:
                overlay_long = self.strategy.dashboard_overlay_candidates(Side.LONG, float(close), ltf, htf)
                overlay_short = self.strategy.dashboard_overlay_candidates(Side.SHORT, float(close), ltf, htf)
                if overlay_long is not None:
                    long_candidates = list(overlay_long or [])
                else:
                    long_candidates = list(self.strategy.dashboard_candidate_levels(float(close), htf, Side.LONG) or [])
                if overlay_short is not None:
                    short_candidates = list(overlay_short or [])
                else:
                    short_candidates = list(self.strategy.dashboard_candidate_levels(float(close), htf, Side.SHORT) or [])
                selected_long = self.strategy.dashboard_select_level(Side.LONG, float(close), ltf, htf)
                selected_short = self.strategy.dashboard_select_level(Side.SHORT, float(close), ltf, htf)
                selected_long_price = safe_float((selected_long or {}).get("price")) if isinstance(selected_long, dict) else None
                selected_short_price = safe_float((selected_short or {}).get("price")) if isinstance(selected_short, dict) else None
            else:
                long_candidates = list(self.strategy.dashboard_candidate_levels(float(close), htf, Side.LONG) or [])
                short_candidates = list(self.strategy.dashboard_candidate_levels(float(close), htf, Side.SHORT) or [])
        except Exception:
            # The strategy's own level hooks: a failure draws no zones and
            # marks no level for entry, so it must show in the log.
            self.log_component_failure("level_zone_candidates", "Level-zone candidate hooks failed for %s", symbol)
            long_candidates = []
            short_candidates = []
            selected_long_price = None
            selected_short_price = None

        allow_level_fallback = bool(self.strategy.dashboard_allow_generic_level_fallback())
        if allow_level_fallback and not long_candidates and support_anchors:
            long_candidates = [
                {"kind": kind_name, "price": price, "touches": 1, "level_score": 0.0, "source_priority": 0.0, "builder_flip_confirmed": flip_confirmed}
                for price, kind_name, flip_confirmed in support_anchors
            ]
        if allow_level_fallback and not short_candidates and resistance_anchors:
            short_candidates = [
                {"kind": kind_name, "price": price, "touches": 1, "level_score": 0.0, "source_priority": 0.0, "builder_flip_confirmed": flip_confirmed}
                for price, kind_name, flip_confirmed in resistance_anchors
            ]

        def _candidate_zone_payload(side: Side, candidate: dict[str, Any]) -> dict[str, Any] | None:
            price = safe_float(candidate.get("price"))
            if price is None or price <= 0:
                return None
            zone_kind = "support" if side == Side.LONG else "resistance"
            try:
                zone_width_override = self.strategy.dashboard_zone_width_for_level(side, float(close), float(atr), float(price), htf, candidate)
                zone_half_width = float(zone_width_override) if zone_width_override is not None else float(base_zone_half_width)
            except Exception:
                self.log_component_failure("level_zone_width", "Level-zone width hook failed for %s", symbol)
                zone_half_width = float(base_zone_half_width)
            zone_half_width = max(float(zone_half_width), 0.01)
            raw_lower = safe_float(candidate.get("zone_lower"))
            raw_upper = safe_float(candidate.get("zone_upper"))
            if raw_lower is not None and raw_upper is not None and raw_upper >= raw_lower:
                zone_lower = float(raw_lower)
                zone_upper = float(raw_upper)
                zone_half_width = max(float(zone_half_width), (zone_upper - zone_lower) / 2.0)
            else:
                zone_lower = max(0.0, float(price) - float(zone_half_width))
                zone_upper = float(price) + float(zone_half_width)
            kind_name = str(candidate.get("kind") or "").strip()
            selected_anchor_price = selected_long_price if side == Side.LONG else selected_short_price
            return {
                "kind": zone_kind,
                "price": float(price),
                "lower": float(zone_lower),
                "upper": float(zone_upper),
                "score": float(candidate.get("level_score", 0.0) or 0.0),
                "touches": int(candidate.get("touches", 1) or 1),
                "labels": [self.strategy.dashboard_candidate_label(kind_name, zone_kind)],
                "sources": self.strategy.dashboard_candidate_sources(kind_name, zone_kind),
                "timeframe": f"{tf}m",
                "zone_half_width": float(zone_half_width),
                "engine_level_kind": kind_name or None,
                "engine_source_priority": float(candidate.get("source_priority", 0.0) or 0.0),
                "engine_level_score": float(candidate.get("level_score", 0.0) or 0.0),
                "passes_min_level_score": bool(float(candidate.get("level_score", 0.0) or 0.0) >= float(min_level_score)),
                "selected_for_entry": bool(selected_anchor_price is not None and abs(float(price) - float(selected_anchor_price)) <= float(selected_zone_match_tolerance)),
                # The S/R builder's verdict on a generic-fallback level's flip;
                # absent on a strategy's own candidates, whose flips
                # dashboard_zones checks on the zone's edges.
                "builder_flip_confirmed": candidate.get("builder_flip_confirmed"),
            }

        support_zones = [zone for zone in (_candidate_zone_payload(Side.LONG, candidate) for candidate in long_candidates) if zone is not None]
        resistance_zones = [zone for zone in (_candidate_zone_payload(Side.SHORT, candidate) for candidate in short_candidates) if zone is not None]

        # Use trading-mode flip confirmation (flip_confirmation_bars) so the
        # chart's zone classification matches what position management and
        # strategy entries see. The previous code used loose dashboard mode
        # (1m_bars=1, 5m_bars=0) for snappier visual feedback, but that meant
        # a chart zone could flip color before the strategy itself treated it
        # as flipped — confusing when the dashboard sidebar (which already
        # uses trading mode via `sr_row()`) and the chart disagreed about
        # the same level.
        return build_level_zones(
            support_zones + resistance_zones,
            close=close,
            flip_frame=frame,
            flip_confirmation_bars=self.config.support_resistance.flip_confirmation_bars(),
            timeframe_minutes=tf,
        )

    def sr_row(self, symbol: str, price: float | None = None, *, allow_refresh: bool = True) -> dict[str, Any] | None:
        """The dashboard's support/resistance row: ``symbol``'s S/R snapshot
        (``sr_snapshot.sr_snapshot``, which the exit record reads too) and the
        strategy's LTF label, which the expanded chart's LTF toggle shows
        ("5m LTF" rather than a hardcoded "1M LTF"), third in the row."""
        snapshot = sr_snapshot(self.config, self.data, symbol, price=price, strategy=self.strategy,
                               account=self.account, allow_refresh=allow_refresh)
        if snapshot is None:
            return None
        return {
            "symbol": snapshot["symbol"],
            "timeframe": snapshot["timeframe"],
            "ltf_timeframe": f"{max(1, self.strategy.ltf_minutes())}m",
            **snapshot,
        }

    def snapshot_should_bypass_cache(self, symbol: str, *, allow_refresh: bool) -> bool:
        """True when support-resistance or HTF context needs a fresh refresh.

        Callers (dashboard snapshot builders) use this to decide whether a
        cached snapshot can be returned or must be recomputed."""
        if not allow_refresh:
            return False
        sr_tf = self.strategy.htf_minutes()
        if self.data.should_refresh_support_resistance(symbol, timeframe_minutes=sr_tf):
            return True
        if self.data.should_refresh_htf_context(symbol, sr_tf):
            return True
        return False

    def symbol_snapshot_signature(
        self,
        symbol: str,
        frame: pd.DataFrame | None,
        *,
        quote: Mapping[str, Any] | None,
        quote_is_fresh: bool,
        sr_row: Mapping[str, Any] | None,
        candidate_row: Mapping[str, Any] | None,
        position_row: Mapping[str, Any] | None,
        entry_decision: Mapping[str, Any] | None,
        warmup: Mapping[str, Any] | None,
        allow_refresh: bool,
    ) -> tuple[Any, ...]:
        """Tuple signature for the per-symbol dashboard snapshot cache. Any
        change in timestamps, quote, SR row, candidate, position, or trades
        invalidates the cached snapshot."""
        symbol_key = str(symbol or "").upper().strip()
        quote_refresh = self.data.last_quote_refresh.get(symbol_key) if symbol_key else None
        history_refresh = self.data.last_history_refresh.get(symbol_key) if symbol_key else None
        stream_refresh = self.data.last_stream_update.get(symbol_key) if symbol_key else None
        htf_refresh = self.data.last_htf_refresh.get((symbol_key, self.strategy.htf_minutes())) if symbol_key else None
        quote_body = quote or {}
        return (
            frame_signature(frame),
            bool(quote_is_fresh),
            quote_refresh.isoformat() if quote_refresh is not None else None,
            history_refresh.isoformat() if history_refresh is not None else None,
            stream_refresh.isoformat() if stream_refresh is not None else None,
            htf_refresh.isoformat() if htf_refresh is not None else None,
            safe_float(quote_body.get("last")) if isinstance(quote_body, Mapping) else None,
            safe_float(quote_body.get("bid")) if isinstance(quote_body, Mapping) else None,
            safe_float(quote_body.get("ask")) if isinstance(quote_body, Mapping) else None,
            safe_float(quote_body.get("mark")) if isinstance(quote_body, Mapping) else None,
            safe_float(quote_body.get("total_volume")) if isinstance(quote_body, Mapping) else None,
            cache_json_signature(sr_row or {}),
            cache_json_signature(candidate_row or {}),
            cache_json_signature(position_row or {}),
            cache_json_signature(entry_decision or {}),
            cache_json_signature(warmup or {}),
            symbol_trade_signature(self.account, symbol_key),
            bool(allow_refresh),
        )

    def current_pattern_payload(self, frame: pd.DataFrame | None) -> dict[str, Any]:
        """Build the candle + chart-pattern dashboard payload from ``frame``.
        Uses strategy's ``dashboard_candle_context`` if exposed, else falls
        back to default detect_candle_context with configured pattern lists."""
        payload: dict[str, Any] = {
            "candles_bullish": [],
            "candles_bearish": [],
            "candle_bias_score": None,
            "candle_net_score": None,
            "candle_regime_hint": "neutral",
            "bullish_candle_score": 0.0,
            "bearish_candle_score": 0.0,
            "bullish_candle_net_score": 0.0,
            "bearish_candle_net_score": 0.0,
            "bullish_candle_anchor_pattern": None,
            "bearish_candle_anchor_pattern": None,
            "bullish_candle_anchor_bars": 0,
            "bearish_candle_anchor_bars": 0,
            "chart_bullish": [],
            "chart_bearish": [],
            "chart_bullish_reversal": [],
            "chart_bullish_continuation": [],
            "chart_bearish_reversal": [],
            "chart_bearish_continuation": [],
            "chart_bias_score": None,
            "chart_regime_hint": "neutral",
        }
        if frame is None or frame.empty:
            return payload
        frame_for_analysis = frame.copy()
        for col in ("open", "high", "low", "close", "volume"):
            if col in frame_for_analysis.columns:
                frame_for_analysis[col] = pd.to_numeric(frame_for_analysis[col], errors="coerce")
        frame_for_analysis = frame_for_analysis.dropna(subset=[col for col in ("open", "high", "low", "close") if col in frame_for_analysis.columns]).copy()
        if frame_for_analysis.empty:
            return payload
        try:
            candle_builder = getattr(self.strategy, "dashboard_candle_context", None)
            if callable(candle_builder):
                candle_ctx = candle_builder(frame_for_analysis)
            else:
                candle_ctx = detect_candle_context(
                    frame_for_analysis,
                    bullish_allowed=self.config.candles.bullish_patterns,
                    bearish_allowed=self.config.candles.bearish_patterns,
                )
        except Exception:
            self.log_component_failure("candle_context", "Dashboard candle context failed")
            candle_ctx = detect_candle_context(pd.DataFrame())
        payload["candles_bullish"] = list(candle_ctx.get("matched_bullish_candles", []))
        payload["candles_bearish"] = list(candle_ctx.get("matched_bearish_candles", []))
        payload["candle_bias_score"] = float(candle_ctx.get("candle_bias_score", 0.0) or 0.0)
        payload["candle_regime_hint"] = str(candle_ctx.get("candle_regime_hint", "neutral") or "neutral")
        payload["candle_net_score"] = float(candle_ctx.get("candle_net_score", payload["candle_bias_score"]) or payload["candle_bias_score"])
        payload["bullish_candle_score"] = float(candle_ctx.get("bullish_candle_score", 0.0) or 0.0)
        payload["bearish_candle_score"] = float(candle_ctx.get("bearish_candle_score", 0.0) or 0.0)
        payload["bullish_candle_net_score"] = float(candle_ctx.get("bullish_candle_net_score", 0.0) or 0.0)
        payload["bearish_candle_net_score"] = float(candle_ctx.get("bearish_candle_net_score", 0.0) or 0.0)
        payload["bullish_candle_anchor_pattern"] = candle_ctx.get("bullish_candle_anchor_pattern")
        payload["bearish_candle_anchor_pattern"] = candle_ctx.get("bearish_candle_anchor_pattern")
        payload["bullish_candle_anchor_bars"] = int(candle_ctx.get("bullish_candle_anchor_bars", 0) or 0)
        payload["bearish_candle_anchor_bars"] = int(candle_ctx.get("bearish_candle_anchor_bars", 0) or 0)
        if bool(getattr(self.config.chart_patterns, "enabled", True)):
            try:
                chart_ctx = analyze_chart_pattern_context(
                    frame_for_analysis,
                    bullish_allowed=self.config.chart_patterns.bullish_patterns,
                    bearish_allowed=self.config.chart_patterns.bearish_patterns,
                    lookback_bars=int(getattr(self.config.chart_patterns, "lookback_bars", 32) or 32),
                )
                payload["chart_bullish"] = sorted(list(getattr(chart_ctx, "matched_bullish", set()) or []))
                payload["chart_bearish"] = sorted(list(getattr(chart_ctx, "matched_bearish", set()) or []))
                payload["chart_bullish_reversal"] = sorted(list(getattr(chart_ctx, "matched_bullish_reversal", set()) or []))
                payload["chart_bullish_continuation"] = sorted(list(getattr(chart_ctx, "matched_bullish_continuation", set()) or []))
                payload["chart_bearish_reversal"] = sorted(list(getattr(chart_ctx, "matched_bearish_reversal", set()) or []))
                payload["chart_bearish_continuation"] = sorted(list(getattr(chart_ctx, "matched_bearish_continuation", set()) or []))
                payload["chart_bias_score"] = safe_float(getattr(chart_ctx, "bias_score", None))
                payload["chart_regime_hint"] = str(getattr(chart_ctx, "regime_hint", "neutral") or "neutral")
            except Exception:
                LOG.debug("Failed to attach chart-pattern payload to dashboard response; returning partial payload.", exc_info=True)
        return payload

    def current_structure_overlay(self, frame: pd.DataFrame | None, *, timeframe_minutes: int,
                                  last_bar_forming: bool = False) -> dict[str, Any]:
        """Build the market-structure overlay payload (CHOCH/BOS event, age,
        level) from ``frame`` at the given timeframe. ``last_bar_forming``:
        ``frame``'s last bar is a bucket still trading, which confirms no
        pivot (as in the strategy's ``_structure_context``).

        Calls ``analyze_market_structure`` directly instead of building a
        full ``SupportResistanceContext`` — the overlay only consumes
        ``market_structure`` and skipping the surrounding S/R clustering,
        prior-day/week, FVG checks, broken-level reconciliation, and
        proximity metrics is roughly an order-of-magnitude speedup per
        chart render. Returns neutral payload on any failure (with
        rate-limited warning via log_component_failure)."""
        payload: dict[str, Any] = {
            "event": "—",
            "age_bars": None,
            # Start timestamp of the bar the event fired on, so the chart can
            # mark it on the matching bar whatever bars it has merged since.
            "event_ts": None,
            "level": None,
            "bias": "neutral",
            "pivot_bias": "neutral",
        }
        if frame is None or frame.empty:
            return payload
        frame_for_analysis = frame.copy()
        for col in ("open", "high", "low", "close", "volume"):
            if col in frame_for_analysis.columns:
                frame_for_analysis[col] = pd.to_numeric(frame_for_analysis[col], errors="coerce")
        frame_for_analysis = frame_for_analysis.dropna(subset=[col for col in ("open", "high", "low", "close") if col in frame_for_analysis.columns]).copy()
        if frame_for_analysis.empty:
            return payload
        close_val = safe_float(frame_for_analysis["close"].iloc[-1])
        if close_val is None:
            return payload
        try:
            sr_cfg = self.config.support_resistance
            # Match the strategy's LTF-vs-HTF structure params so the overlay
            # reflects what the bot actually computes — same "keep the chart
            # faithful to the strategy" principle as the HTF-EMA override in
            # the chart payload. The LTF structure context applies
            # structure_ltf_pivot_span, a 0.60x pct_tolerance, and the
            # min-pivot-gap filter (Fix B/D, 2026-05-27); the HTF/base context
            # does not. Detect the LTF chart by matching the display timeframe
            # to the strategy's effective LTF structure timeframe
            # (structure_ltf_timeframe_minutes, falling back to the
            # strategy's ltf_minutes()). HTF / other timeframes keep the
            # original base-param behavior unchanged.
            ltf_struct_tf = int(getattr(sr_cfg, "structure_ltf_timeframe_minutes", 0) or 0) or (self.strategy.ltf_minutes() or 1)
            is_ltf_chart = int(timeframe_minutes or 1) == ltf_struct_tf
            overlay_pivot_span = (
                int(getattr(sr_cfg, "structure_ltf_pivot_span", 2) or 2)
                if is_ltf_chart
                else int(getattr(sr_cfg, "pivot_span", 2) or 2)
            )
            overlay_pct_tolerance = float(sr_cfg.pct_tolerance)  # checked at load (above 0)
            if is_ltf_chart:
                overlay_pct_tolerance *= 0.60
            overlay_gap_bars = (
                int(getattr(sr_cfg, "structure_min_pivot_gap_bars", 0) or 0) if is_ltf_chart else 0
            )
            ms_ctx = analyze_market_structure(
                frame_for_analysis,
                current_price=close_val,
                pivot_span=overlay_pivot_span,
                eq_atr_mult=float(getattr(sr_cfg, "structure_eq_atr_mult", 0.25) or 0.25),
                pct_tolerance=overlay_pct_tolerance,
                breakout_atr_mult=float(getattr(sr_cfg, "breakout_atr_mult", 0.35) or 0.35),
                breakout_buffer_pct=float(getattr(sr_cfg, "breakout_buffer_pct", 0.0015) or 0.0015),
                # LTF overlay counts LTF bars, HTF overlay counts HTF bars --
                # the same split the pivot gap above already makes.
                structure_event_max_age_bars=(
                    int(getattr(sr_cfg, "structure_event_lookback_bars", 6) or 6)
                    if is_ltf_chart else sr_cfg.htf_structure_event_lookback()
                ),
                min_range_atr_mult=float(getattr(sr_cfg, "structure_min_range_atr_mult", 1.5) or 0.0),
                min_pivot_gap_bars=overlay_gap_bars,
                last_bar_forming=last_bar_forming,
            )
        except Exception:
            self.log_component_failure(
                "structure_overlay",
                "Dashboard structure overlay build failed for timeframe=%sm",
                int(timeframe_minutes or 1),
            )
            return payload
        event = structure_event_label(ms_ctx)
        age = None
        level = None
        if event == "CHOCH↑":
            age = int(getattr(ms_ctx, "choch_up_age_bars", 0) or 0)
            level = safe_float(getattr(ms_ctx, "reference_high", None))
        elif event == "CHOCH↓":
            age = int(getattr(ms_ctx, "choch_down_age_bars", 0) or 0)
            level = safe_float(getattr(ms_ctx, "reference_low", None))
        elif event == "BOS↑":
            age = int(getattr(ms_ctx, "bos_up_age_bars", 0) or 0)
            level = safe_float(getattr(ms_ctx, "reference_high", None))
        elif event == "BOS↓":
            age = int(getattr(ms_ctx, "bos_down_age_bars", 0) or 0)
            level = safe_float(getattr(ms_ctx, "reference_low", None))
        payload.update({
            "event": event,
            "age_bars": age,
            "event_ts": None if age is None else frame_for_analysis.index[len(frame_for_analysis) - 1 - age].isoformat(),
            "level": level,
            "bias": str(getattr(ms_ctx, "bias", "neutral") or "neutral"),
            "pivot_bias": str(getattr(ms_ctx, "pivot_bias", "neutral") or "neutral"),
        })
        return payload

    def chart_payload(self, symbol: str, *, max_bars: int = 90, timeframe_mode: str = "ltf") -> dict[str, Any]:
        """Build the dashboard chart payload for ``symbol`` — bars + patterns
        + structure overlay + chart config. This is the callable passed to
        ``DashboardServer`` as ``chart_payload_provider``.

        ``timeframe_mode``: ``"ltf"`` renders at the strategy's LTF
        (``params.ltf_minutes``, defaults to 1m streaming bars). ``"htf"``
        renders at the strategy's HTF (``params.htf_minutes`` or the shared
        ``support_resistance.timeframe_minutes`` default). Anything other
        than ``"htf"`` is normalized to ``"ltf"``."""
        from dataclasses import asdict
        resolved_mode = str(timeframe_mode or "ltf").strip().lower()
        if resolved_mode != "htf":
            resolved_mode = "ltf"
        symbol_key = str(symbol or "").upper().strip()
        try:
            capped_bars = max(1, min(int(max_bars or 90), 480))
        except (TypeError, ValueError):
            capped_bars = 90
        ltf_min = max(1, self.strategy.ltf_minutes())
        htf_min = self.strategy.htf_minutes()
        if resolved_mode == "htf":
            timeframe_minutes = htf_min
            timeframe_label = f"{htf_min}m"
        else:
            timeframe_minutes = ltf_min
            timeframe_label = f"{ltf_min}m" if ltf_min > 1 else "1m"
        # ``frame`` is what the chart plots, and ``forming_start`` the start
        # of its still-forming last bucket (None when every bar is complete).
        # ``completed_frame`` is ``frame`` without that bucket: the per-bar
        # candle tags are read from it, so every bar drawn as complete is
        # tagged and the forming one is not. ``context_frame`` is what the
        # chart patterns and structure overlay read. On the LTF chart that is
        # ``frame``, forming bucket included, because the strategy reads them
        # off that same frame (get_merged resamples the live 1m stream and
        # keeps the partial bucket). The chart patterns read the forming
        # bucket like any other bar; the structure overlay reads its close
        # and breaks but confirms no pivot with it, as the strategy's
        # _structure_context has since 2026-09-25. On the HTF chart it is
        # ``completed_frame``: the strategy's HTF contexts read completed
        # buckets only, and no strategy reads chart patterns off HTF bars --
        # there they describe the bars drawn. ``minute_frame`` is the 1m
        # frame the payload is current as of: its newest bar is
        # ``source_bar_ts``, which the client compares with the snapshot's
        # newest bar to know when this payload is stale.
        frame: pd.DataFrame | None = None
        stored_frame: pd.DataFrame | None = None
        minute_frame: pd.DataFrame | None = None
        forming_start: pd.Timestamp | None = None
        if resolved_mode == "htf" and symbol_key:
            # HTTP handler path: only read cached HTF data, never trigger a
            # Schwab fetch here. Forcing a refresh from the HTTP thread races
            # with the engine's per-cycle prefetch (the HTF frame refresh runs
            # under self._lock on the engine thread) and risks rate-limit
            # hits. If the cache is empty, return an empty chart — the next
            # engine cycle will populate it and the next poll will render.
            stored_frame = self.data.get_htf_frame(
                symbol_key,
                timeframe_minutes=htf_min,
                lookback_days=self.strategy.htf_lookback_days(),
                allow_refresh=False,
            )
            minute_frame = self.data.get_merged(symbol_key, with_indicators=False)
            frame, forming_start = htf_chart_frame(
                stored_frame,
                minute_frame,
                timeframe_minutes=htf_min,
                now=sessions.now_et(),
            )
        elif symbol_key:
            # LTF path: when ltf_min is 1 fetch the streaming 1m frame
            # directly (no resample); for ltf_min > 1 (e.g. 5-min trigger
            # candles) get_merged resamples 1m -> ltf via resample_bars.
            if ltf_min > 1:
                # The 1m frame first: a stream bar landing between the two
                # reads then makes the payload name the OLDER bar, and the
                # client refetches once the snapshot shows the new one. Read
                # second, source_bar_ts could name a minute the plotted
                # bucket does not hold, and nothing would ask again.
                minute_frame = self.data.get_merged(symbol_key, with_indicators=False)
                frame = self.data.get_merged(symbol_key, timeframe=f"{ltf_min}min", with_indicators=True)
                # The resampled frame keeps the partial last bucket. Until
                # 2026-09-23 it was drawn as complete and candle-tagged off
                # its first minutes (AAPL 09-22 10:03: a 3-minute 10:00 5m bar
                # tagged CDLHAMMER; complete, it tags as a bearish marubozu).
                if frame is not None and not frame.empty and last_bucket_forming(frame.index, ltf_min, sessions.now_et()):
                    forming_start = pd.Timestamp(frame.index[-1])
            else:
                frame = self.data.get_merged(symbol_key, with_indicators=True)
                minute_frame = frame
        completed_frame = frame.iloc[:-1] if frame is not None and forming_start is not None else frame
        context_frame = frame if resolved_mode == "ltf" else completed_frame
        source_bar_ts = minute_frame.index[-1].isoformat() if minute_frame is not None and not minute_frame.empty else None
        htf_refresh = self.data.last_htf_refresh.get((symbol_key, timeframe_minutes)) if resolved_mode == "htf" and symbol_key else None
        chart_signature = (
            frame_signature(frame),
            htf_refresh.isoformat() if htf_refresh is not None else None,
            source_bar_ts,
            forming_start.isoformat() if forming_start is not None else None,
        )
        cache_key = (symbol_key, resolved_mode, capped_bars)
        with self.lock:
            cache_entry = self.chart_cache.get(cache_key)
            if cache_entry is not None and cache_entry.get("signature") == chart_signature:
                # Shallow copy of the top-level dict — we only mutate
                # `last_update` on the returned object. A `copy.deepcopy`
                # here costs ~4ms per call on a 360-bar payload (measured)
                # and was the dominant cost of every chart refresh.
                # Safe because:
                #   1. `audit_logger.json_safe` recursively builds new
                #      dicts/lists for serialization rather than mutating
                #      the input — inner references can be shared.
                #   2. We only assign to a top-level key on the new shallow
                #      dict, so the cached entry's bars/levels/structure
                #      payloads stay isolated from the caller.
                # Re-stamping `last_update` keeps the frontend timestamp
                # advancing while the underlying chart_signature is
                # unchanged.
                cached_payload = dict(cache_entry["payload"])
                cached_payload["last_update"] = sessions.now_et().isoformat()
                return cached_payload
        # Per-bar candle pattern map for the tooltip's per-bar candle section,
        # for every chart bar (see the dashboard_payloads.bars_from_frame docstring +
        # detect_per_bar_candle_patterns). Read from completed_frame, so the
        # forming bucket gets none and every other bar is scored -- on the
        # HTF chart that includes the buckets completed since the stored
        # frame's last refresh, which until 2026-09-23 were drawn untagged
        # until it ran (at least 10 s into each bucket).
        chart_per_bar_candles: dict[Any, dict[str, list[str]]] = {}
        if completed_frame is not None and not completed_frame.empty:
            try:
                chart_per_bar_candles = self._per_bar_candle_map(completed_frame, capped_bars)
            except Exception:
                self.log_component_failure(
                    "per_bar_candles",
                    "Per-bar candle pattern detection failed for %s",
                    symbol_key,
                )
                chart_per_bar_candles = {}
        bars = bars_from_frame(
            frame,
            max_bars=capped_bars,
            per_bar_candles=chart_per_bar_candles,
        )
        forming_ends_at: str | None = None
        if forming_start is not None:
            # The forming bucket is the plotted frame's last row. Its end goes
            # on the payload: the client refetches once it has passed, since
            # a bucket whose last minutes print nothing brings no newer 1m
            # bar (the LTF chart's only other refetch trigger) and would stay
            # drawn as forming until the next trade.
            bars[-1]["in_progress"] = True
            forming_ends_at = session_bucket_ends(frame.index[-1:], timeframe_minutes)[0].isoformat()
        # Default EMA spans rendered on the chart (matches what
        # `ensure_standard_indicator_frame` populates as ema9/ema20 columns).
        ema_fast_span = 9
        ema_slow_span = 20
        # In HTF mode, draw the HTF EMAs the strategy reads, not the frame's
        # session-reset ema9 / ema20 it never looks at. Field names stay
        # `ema9` / `ema20` for renderer compatibility; the legend uses the
        # spans in this payload.
        #  * A strategy whose HTF trend reads frame columns directly names
        #    them (zero_dte: the continuous ema9_all / ema20_all).
        #  * Otherwise, when the strategy declares htf_ema_fast_span /
        #    htf_ema_slow_span, the continuous EWM of those spans that
        #    build_htf_context computes -- also at 9/20, which until
        #    2026-09-24 skipped the override -- blanked, like the bot's own
        #    value, while the stored frame is too short for it (the bot's
        #    ema_slow is None under `span` bars, its ema_fast under
        #    max(5, span // 3)).
        if resolved_mode == "htf" and frame is not None and not getattr(frame, "empty", True):
            params = getattr(self.strategy, "params", {}) or {}
            columns_hook = getattr(self.strategy, "dashboard_htf_ema_columns", None)
            columns = columns_hook() if callable(columns_hook) else None
            try:
                tail = frame.tail(len(bars))
                if columns is not None:
                    fast_col, slow_col = columns
                    for bar, (_idx, row) in zip(bars, tail.iterrows()):
                        bar["ema9"] = safe_float(row.get(fast_col))
                        bar["ema20"] = safe_float(row.get(slow_col))
                elif "htf_ema_fast_span" in params or "htf_ema_slow_span" in params:
                    htf_fast, htf_slow = htf_ema_spans(params)
                    built_from = len(stored_frame) if stored_frame is not None else len(frame)
                    fast_ok = built_from >= max(5, htf_fast // 3)
                    slow_ok = built_from >= htf_slow
                    ema_fast_series = frame["close"].ewm(span=htf_fast, adjust=False).mean()
                    ema_slow_series = frame["close"].ewm(span=htf_slow, adjust=False).mean()
                    for bar, (idx, _row) in zip(bars, tail.iterrows()):
                        bar["ema9"] = safe_float(ema_fast_series.loc[idx]) if fast_ok else None
                        bar["ema20"] = safe_float(ema_slow_series.loc[idx]) if slow_ok else None
                    ema_fast_span = htf_fast
                    ema_slow_span = htf_slow
            except Exception:
                LOG.debug("Failed to compute HTF strategy EMAs for %s; chart falls back to default ema9/ema20.", symbol_key, exc_info=True)
        # In LTF mode, draw the EMAs the strategy reads off its LTF frame (for
        # top_tier_adaptive's 1m LTF at ltf_indicator_span_scale 5, the
        # session-reset EMA45 / EMA100), exactly as the snapshot bars merged
        # over these carry them.
        elif resolved_mode == "ltf" and bars:
            ema_fast_span, ema_slow_span = self._apply_strategy_ltf_emas(symbol_key, frame, bars, timeframe=f"{ltf_min}min")
        pattern_payload = self.current_pattern_payload(context_frame)
        # Rendered on the chart as the event marker + reference level line
        # (until 2026-09-23 it was computed per payload and never drawn).
        structure_overlay = self.current_structure_overlay(
            context_frame,
            timeframe_minutes=timeframe_minutes,
            last_bar_forming=forming_start is not None and resolved_mode == "ltf",
        )
        chart_config_profile = asdict(self.chart_profile("compact"))
        chart_config_expanded = asdict(self.chart_profile("expanded"))
        payload = {
            "symbol": symbol_key,
            "bars": bars,
            "bar_count": len(bars),
            "max_bars": capped_bars,
            "timeframe_mode": resolved_mode,
            "timeframe_label": timeframe_label,
            "timeframe_minutes": timeframe_minutes,
            "ema_fast_span": ema_fast_span,
            "ema_slow_span": ema_slow_span,
            "htf_refresh_token": htf_refresh.isoformat() if htf_refresh is not None else None,
            "last_bar_ts": str(bars[-1].get("ts")) if bars else None,
            "source_bar_ts": source_bar_ts,
            "forming_ends_at": forming_ends_at,
            "last_update": sessions.now_et().isoformat(),
            "patterns": pattern_payload,
            "structure_overlay": structure_overlay,
            "chart_config": {
                "compact": chart_config_profile,
                "expanded": chart_config_expanded,
            },
        }
        # Isolate cache entry from the outgoing payload so concurrent
        # pollers that hit this cache_key can't observe/mutate each other.
        # The same deep copy on store as symbol_snapshot's cache.
        with self.lock:
            self.chart_cache[cache_key] = {"signature": chart_signature, "payload": copy.deepcopy(payload)}
        return payload
