# SPDX-License-Identifier: MIT
"""The strategy's analysis contexts: ``ContextBuildersMixin``, which
``BaseStrategy`` inherits.

Each builder reads a bars frame -- or the data feed's cycle-cached context
for it -- into the context a strategy scores and gates on: chart patterns and
candles; the S/R levels, the HTF levels and the HTF EMA trend; the LTF fair
value gaps and the LTF / HTF order blocks; market structure and the
technical levels. The ``*_lists`` helpers flatten each context into signal
metadata, and the ``dashboard_*`` hooks are the dashboard's reads of the same
contexts. The chart, structure and technical contexts are cached per cycle,
each under its own lock, and the engine pre-warms the ones a strategy asked
for on this cycle's bars frames (``_observed_contexts``,
``set_prewarm_frames``, ``prime_cycle_contexts``, ``reset_context_caches``).
The candle contexts are cached per entry cycle, and the host empties that
cache as each ``entry_signals`` starts (``_reset_entry_decisions`` calls
``_reset_candle_context_cache``).

The host class provides ``config``, ``params`` and the settings accessors
``_support_resistance_setting``, ``_technical_level_setting`` and
``_chart_pattern_setting``.
"""
from __future__ import annotations

import logging
from threading import RLock
from typing import Any, ClassVar, Iterable

import pandas as pd

from .. import sessions
from ..bars import (
    derived_frame,
    frame_bar_minutes,
    frame_source_token,
    frame_version,
    last_bucket_forming,
    resample_bars,
    verified_frame,
)
from ..candles import CANDLE_CONTEXT_BARS, detect_candle_context, directional_candle_signal
from ..chart_patterns import analyze_chart_pattern_context, clean_input_marked, mark_clean_input
from ..context_memo import ContextMemo, indicator_clock_key
from ..fair_value_gaps import FairValueGapContext, build_fair_value_gap_context, empty_fvg_context
from ..htf_levels import HTFContext, empty_htf_context
from ..indicators import (
    ensure_standard_indicator_frame,
    get_runtime_indicator_mode,
    get_session_indicator_window,
    htf_ema_spans,
)
from ..models import Side
from ..numeric import safe_float, safe_int
from ..order_blocks import OrderBlockContext, build_order_block_context, empty_order_block_context
from ..support_resistance import (
    analyze_market_structure,
    empty_market_structure_context,
    empty_support_resistance_context,
)
from ..technical_levels import (
    TechnicalLevelsContext,
    build_technical_levels_context,
    empty_technical_levels_context,
)

LOG = logging.getLogger(__name__)


class ContextBuildersMixin:
    """The context builders and their per-cycle caches (see the module
    docstring). ``BaseStrategy`` is the host."""

    # Auto-detected set of context-builder calls the strategy has made over
    # its lifetime. Each entry is a tuple `(name, *args)` — e.g. `("chart",)`,
    # `("structure", "ltf")`, `("technical",)`. Populated lazily on first
    # invocation of each builder ON ONE OF THE CYCLE'S BARS FRAMES (see
    # _observe_context). The engine reads this set every cycle
    # (after _prime_cycle_support_cache) to drive _prime_cycle_context_cache,
    # which pre-warms the observed contexts one symbol at a time via
    # _compute_symbol_map. Cycle 1 is lazy (set is empty); cycles 2+ benefit.
    # __init_subclass__ gives each subclass its own set so different strategy
    # classes don't cross-contaminate.
    _observed_contexts: ClassVar[set[tuple]] = set()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._observed_contexts = set()

    def __init__(self) -> None:
        self._candle_context_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        # Values are (frame, ctx): see _technical_context_cache_key.
        self._technical_context_cache: dict[tuple[Any, ...], tuple[pd.DataFrame | None, Any]] = {}
        self._structure_context_cache: dict[tuple[Any, ...], tuple[pd.DataFrame | None, Any]] = {}
        self._chart_context_cache: dict[tuple[Any, ...], tuple[pd.DataFrame | None, Any]] = {}
        # Locks around the 3 context dicts' mutations. They were added for
        # the engine's four-worker pre-warm pool, which wrote distinct
        # cache_keys concurrently; since 2026-09-28 the pre-warm runs on the
        # engine thread (_compute_symbol_map), and the locks still keep any
        # other thread's build safe. Compute happens outside the locks, so a
        # thread never waits on another for the heavy work.
        self._chart_context_lock = RLock()
        self._structure_context_lock = RLock()
        self._technical_context_lock = RLock()
        # id -> frame of this cycle's bars frames, the only frames the
        # engine pre-warms (set_prewarm_frames). A builder records its call
        # in _observed_contexts only for one of them. Holding the frames keeps
        # their ids from being handed to another frame mid-cycle.
        self._prewarm_frames: dict[int, pd.DataFrame] = {}
        # The three contexts across cycles (context_memo), for frames handed
        # out by get_merged: slots (builder, timeframe token, symbol, source
        # timeframe, variant), keyed on the frame's version and what each
        # build reads of the clock. Per instance: two instances of a class
        # can carry different settings.
        self._context_memo = ContextMemo(
            f"{type(self).__name__} contexts",
            shadow_every=self.config.runtime.context_memo_shadow_every,
        )

    def reset_context_caches(self) -> None:
        """Cycle-boundary cache cleanup for the three pre-warmed context caches.

        Public API for the engine. Called inside `_prime_cycle_context_cache`
        before the pre-warm populates caches for the new cycle's
        frames. Without this reset the caches would grow unboundedly across
        the session (one entry per (symbol, timeframe) per cycle), and every
        entry pins its frame (see _technical_context_cache_key), so this is
        also what lets the cycle's frames go.
        """
        with self._chart_context_lock:
            self._chart_context_cache = {}
        with self._structure_context_lock:
            self._structure_context_cache = {}
        with self._technical_context_lock:
            self._technical_context_cache = {}
        self._context_memo.next_generation()

    def _reset_candle_context_cache(self) -> None:
        """Empty the candle-context cache as each ``entry_signals`` starts
        (the host's ``_reset_entry_decisions`` calls it). The engine does not
        pre-warm this cache, so ``reset_context_caches`` leaves it."""
        self._candle_context_cache = {}

    def set_prewarm_frames(self, frames: Iterable[pd.DataFrame | None]) -> None:
        """Public API for the engine: this cycle's bars frames, the ones
        `_prime_cycle_context_cache` pre-warms. Called before the pre-warm
        every cycle. A context built on any other frame -- the peers' 5m LTF,
        key_levels_1m's get_merged copy, a test tape -- is not recorded in
        `_observed_contexts` (see `_observe_context`)."""
        self._prewarm_frames = {id(frame): frame for frame in frames if frame is not None}

    def _observe_context(self, frame: pd.DataFrame | None, entry: tuple) -> None:
        """Record a builder call for the engine's pre-warm, only when it was
        made on a frame the pre-warm is handed. The caches key on id(frame),
        so a context pre-warmed on the 1m bars frame is never read by a
        build on another frame. Until 2026-09-24 every call was recorded:
        admit's builds on key_levels' 5m LTF registered ('chart',) and
        ('technical',), and from then on the engine built both on every
        watchlist symbol's 1m frame each cycle, and nothing read them."""
        if frame is not None and self._prewarm_frames.get(id(frame)) is frame:
            type(self)._observed_contexts.add(entry)

    def prime_cycle_contexts(self, frame: pd.DataFrame, observed: Iterable[tuple]) -> None:
        """Pre-warm the strategy's context caches for one symbol's frame.

        Public API for the engine. Replays each entry in `observed` against
        the per-symbol frame, hitting the appropriate internal builder
        (`_chart_context`, `_structure_context`, `_technical_context`).
        Each builder is self-caching under its own RLock; the engine calls
        this for one symbol at a time (``_compute_symbol_map``).
        """
        if frame is None or frame.empty:
            return
        for entry in observed:
            if not entry:
                continue
            name = entry[0]
            if name == "chart":
                self._chart_context(frame)
            elif name == "structure":
                timeframe = entry[1] if len(entry) > 1 else "ltf"
                self._structure_context(frame, timeframe)
            elif name == "technical":
                self._technical_context(frame)

    def _chart_context(self, frame: pd.DataFrame):
        # Per-cycle cache keyed like _technical_context (see
        # _technical_context_cache_key). Records the call signature in
        # _observed_contexts (on a bars frame only, see _observe_context) so
        # the engine can pre-warm this context for next cycle's watchlist.
        self._observe_context(frame, ("chart",))
        cache_key = self._technical_context_cache_key(frame)
        with self._chart_context_lock:
            cached = self._chart_context_cache.get(cache_key)
            if cached is not None:
                return cached[1]
        version = frame_version(frame)
        if version is None:
            ctx = self._build_chart_context(frame)
        else:
            # A pure function of the frame (and the fixed config): no clock,
            # no process-wide setting (tools/clock_audit). The clean-frame mark
            # is an input, and the one effect a build has on the frame is
            # replayed on a hit.
            marked = clean_input_marked(frame)
            ctx = self._context_memo.serve(
                ("chart", None, version[0][0], version[0][2], version[1]),
                (version, marked),
                tuple,
                lambda _clock: self._build_chart_context(frame),
            )
            mark_clean_input(frame)
        with self._chart_context_lock:
            self._chart_context_cache[cache_key] = (frame, ctx)
        return ctx

    def _build_chart_context(self, frame: pd.DataFrame):
        if not bool(self._chart_pattern_setting("enabled", True)):
            return analyze_chart_pattern_context(frame, bullish_allowed=[], bearish_allowed=[], lookback_bars=0)
        cfg = getattr(self.config, "chart_patterns", None)
        bullish_allowed = list(getattr(cfg, "bullish_patterns", []))
        bearish_allowed = list(getattr(cfg, "bearish_patterns", []))
        lookback_bars = int(self._chart_pattern_setting("lookback_bars", getattr(cfg, "lookback_bars", 32) if cfg is not None else 32))
        return analyze_chart_pattern_context(
            frame,
            bullish_allowed=bullish_allowed,
            bearish_allowed=bearish_allowed,
            lookback_bars=lookback_bars,
        )

    @staticmethod
    def _chart_lists(ctx) -> dict[str, list[str]]:
        return {
            "matched_bullish_chart_patterns": sorted(ctx.matched_bullish),
            "matched_bullish_chart_reversal_patterns": sorted(ctx.matched_bullish_reversal),
            "matched_bullish_chart_continuation_patterns": sorted(ctx.matched_bullish_continuation),
            "matched_bearish_chart_patterns": sorted(ctx.matched_bearish),
            "matched_bearish_chart_reversal_patterns": sorted(ctx.matched_bearish_reversal),
            "matched_bearish_chart_continuation_patterns": sorted(ctx.matched_bearish_continuation),
            "chart_pattern_bias_score": float(ctx.bias_score),
            "chart_pattern_regime_hint": str(ctx.regime_hint),
        }

    @staticmethod
    def _candle_context_cache_key(frame: pd.DataFrame | None, bullish_allowed: list[str], bearish_allowed: list[str]) -> tuple[Any, ...]:
        if frame is None or frame.empty:
            frame_marker: tuple[Any, ...] = (("empty",),)
        else:
            # Slice size MUST match what detect_candle_context uses so this
            # cache doesn't collide across different inputs.
            tail = frame[["open", "high", "low", "close"]].tail(CANDLE_CONTEXT_BARS).copy()
            for col in ("open", "high", "low", "close"):
                tail[col] = pd.to_numeric(tail[col], errors="coerce")
            tail = tail.dropna(subset=["open", "high", "low", "close"])
            rows: list[tuple[Any, ...]] = []
            for idx, row in tail.iterrows():
                try:
                    idx_marker = idx.isoformat()  # type: ignore[attr-defined]
                except AttributeError:
                    # An index label that is not a timestamp.
                    idx_marker = repr(idx)
                rows.append((idx_marker, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])))
            frame_marker = tuple(rows) if rows else (("empty",),)
        return (
            frame_marker,
            tuple(str(item or "").strip().upper() for item in bullish_allowed if str(item).strip()),
            tuple(str(item or "").strip().upper() for item in bearish_allowed if str(item).strip()),
        )

    def _candle_context(self, frame: pd.DataFrame) -> dict[str, Any]:
        cfg = getattr(self.config, "candles", None)
        bullish_allowed = list(getattr(cfg, "bullish_patterns", []) or [])
        bearish_allowed = list(getattr(cfg, "bearish_patterns", []) or [])
        cache_key = self._candle_context_cache_key(frame, bullish_allowed, bearish_allowed)
        cached = self._candle_context_cache.get(cache_key)
        if cached is not None:
            return {key: list(value) if isinstance(value, list) else value for key, value in cached.items()}
        ctx = detect_candle_context(frame, bullish_allowed, bearish_allowed)
        self._candle_context_cache[cache_key] = {key: list(value) if isinstance(value, list) else value for key, value in ctx.items()}
        return ctx

    def dashboard_candle_context(self, frame: pd.DataFrame | None) -> dict[str, Any]:
        if frame is None or frame.empty:
            return self._candle_context(pd.DataFrame())
        # _candle_context slices internally to CANDLE_CONTEXT_BARS; pass the
        # full frame so TA-Lib has enough context to initialize.
        return self._candle_context(frame)

    def _directional_candle_signal(self, frame: pd.DataFrame, side: Side) -> dict[str, Any]:
        return directional_candle_signal(self._candle_context(frame), bullish=side == Side.LONG)

    def htf_minutes(self) -> int:
        """HTF (higher timeframe) for SR detection. Strategies declare via
        `params.htf_minutes`; otherwise inherit
        `support_resistance.timeframe_minutes`. The one resolution: the
        engine, the trade manager, the entry gatekeeper, the dashboard, the
        S/R snapshot and the session archive's HTF folder read it here
        (until 2026-09-27 the dashboard and the position manager each kept
        a copy)."""
        fallback = int(self._support_resistance_setting("timeframe_minutes", 15))
        return int(self.params.get("htf_minutes", fallback))

    def htf_lookback_days(self) -> int:
        """Days of HTF history the engine's HTF refresh fetches and keeps
        (``IntradayBot._refresh_htf_frames``): `params.htf_lookback_days`,
        else `support_resistance.lookback_days`."""
        fallback = int(self._support_resistance_setting("lookback_days", 10))
        return int(self.params.get("htf_lookback_days", fallback))

    def ltf_minutes(self) -> int:
        """LTF (lower timeframe / trigger frame). Strategies with a distinct
        intraday trigger candle declare `params.ltf_minutes` (e.g. peer_confirmed
        uses 5-min trigger candles). Otherwise defaults to 1-minute streamed bars."""
        return int(self.params.get("ltf_minutes", 1))

    def _is_ltf_token(self, token: str) -> bool:
        """Whether ``token`` refers to the strategy's LTF timeframe.

        Accepts the literal ``"ltf"`` token plus the numeric forms
        (``"1m"`` / ``"1min"`` / ``"minute"`` / ``"execution"`` when
        ``ltf_minutes == 1``; ``"5m"`` / ``"5min"`` when ``ltf_minutes == 5``,
        and so on). Used by ``_structure_context`` to decide when to apply
        the LTF-specific pivot-span and pct-tolerance overrides.
        """
        normalized = str(token or "").strip().lower()
        if normalized == "ltf":
            return True
        ltf_min = self.ltf_minutes()
        if ltf_min == 1:
            return normalized in {"1m", "1min", "minute", "execution"}
        return normalized in {f"{ltf_min}m", f"{ltf_min}min"}

    def _sr_context(self, symbol: str, frame: pd.DataFrame | None, data):
        current_price = float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        timeframe_minutes = self.htf_minutes()
        if not bool(self._support_resistance_setting("enabled", True)) or data is None:
            return empty_support_resistance_context(current_price, timeframe_minutes=timeframe_minutes)
        ctx = data.get_support_resistance(
            symbol,
            current_price=current_price,
            flip_frame=frame,
            mode="trading",
            timeframe_minutes=timeframe_minutes,
            use_prior_day_high_low=bool(self._support_resistance_setting("use_prior_day_high_low", True)),
            use_prior_week_high_low=bool(self._support_resistance_setting("use_prior_week_high_low", True)),
        )
        return ctx if ctx is not None else empty_support_resistance_context(current_price, timeframe_minutes=timeframe_minutes)

    @staticmethod
    def _sr_lists(ctx) -> dict[str, Any]:
        ms_ctx = getattr(ctx, "market_structure", None) or empty_market_structure_context(getattr(ctx, "current_price", 0.0))
        return {
            "sr_timeframe": f'{int(getattr(ctx, "timeframe_minutes", 0) or 0)}m',
            "sr_supports": [float(round(lv.price, 4)) for lv in ctx.supports],
            "sr_resistances": [float(round(lv.price, 4)) for lv in ctx.resistances],
            "sr_nearest_support": float(ctx.nearest_support.price) if ctx.nearest_support else None,
            "sr_nearest_resistance": float(ctx.nearest_resistance.price) if ctx.nearest_resistance else None,
            "sr_support_distance_pct": None if ctx.support_distance_pct is None else float(ctx.support_distance_pct),
            "sr_resistance_distance_pct": None if ctx.resistance_distance_pct is None else float(ctx.resistance_distance_pct),
            "sr_support_distance_atr": None if ctx.support_distance_atr is None else float(ctx.support_distance_atr),
            "sr_resistance_distance_atr": None if ctx.resistance_distance_atr is None else float(ctx.resistance_distance_atr),
            "sr_breakout_above_resistance": bool(ctx.breakout_above_resistance),
            "sr_breakdown_below_support": bool(ctx.breakdown_below_support),
            # Shadow log for the fresh-break gate hypothesis: how long ago
            # price last traded at the broken level (see
            # SupportResistanceContext.breakout_age_minutes). Nothing blocks
            # on these.
            "sr_breakout_age_minutes": getattr(ctx, "breakout_age_minutes", None),
            "sr_breakdown_age_minutes": getattr(ctx, "breakdown_age_minutes", None),
            "sr_near_support": bool(ctx.near_support),
            "sr_near_resistance": bool(ctx.near_resistance),
            "sr_bias_score": float(ctx.bias_score),
            "sr_regime_hint": str(ctx.regime_hint),
            "sr_level_buffer": float(ctx.level_buffer or 0.0),
            **ContextBuildersMixin._structure_lists(ms_ctx, prefix="mshtf"),
        }

    def _default_htf_request(self) -> dict[str, Any]:
        """The level arguments of the HTF context a strategy scores on: its
        own HTF frame (``htf_minutes()``, the frame the engine refreshes),
        the support_resistance level settings and its HTF EMA spans.
        ``_htf_context`` adds the FVG arguments.

        The score context (``_default_htf_context_for_score``), the shared
        FVG score term and zero_dte's entry read all ask for it, so they
        share one data-feed cache entry. Until 2026-09-27 each built
        its own copy, and the score context left out the FVG arguments (part
        of the cache key): with any FVG setting off its default, every
        preset's, it was a second build of the same frame."""
        ema_fast_span, ema_slow_span = htf_ema_spans(self.params)
        return {
            "timeframe_minutes": self.htf_minutes(),
            "pivot_span": int(self._support_resistance_setting("pivot_span", 2) or 2),
            "max_levels_per_side": int(self._support_resistance_setting("max_levels_per_side", 6) or 6),
            # Checked at load (above 0); a 0 read as 0.35 / 0.003 until
            # 2026-09-26.
            "atr_tolerance_mult": float(self.config.support_resistance.atr_tolerance_mult),
            "pct_tolerance": float(self.config.support_resistance.pct_tolerance),
            "stop_buffer_atr_mult": float(self._support_resistance_setting("stop_buffer_atr_mult", 0.25) or 0.25),
            # The strategy's own HTF EMA spans: top_tier trades on this
            # context's EMA trend (require_htf_ema_alignment /
            # htf_ema_alignment_score). Until 2026-09-24 50/200 was
            # hard-coded here whatever htf_ema_*_span said.
            "ema_fast_span": ema_fast_span,
            "ema_slow_span": ema_slow_span,
            "use_prior_day_high_low": bool(self._support_resistance_setting("use_prior_day_high_low", True)),
            "use_prior_week_high_low": bool(self._support_resistance_setting("use_prior_week_high_low", True)),
        }

    def htf_context_requests(self) -> dict[str, dict[str, Any]]:
        """The level arguments of every HTF context the strategy and its
        dashboard rows read (``_htf_context``), by name: ``score``, the score
        context's (``_default_htf_request``), which the shared score terms,
        top_tier's EMA trend and zero_dte's regime read, and the dashboard's
        HTF overlays and level zones ask for too; and ``generic_trend``
        (``generic_htf_trend_request``) for a class that shows no HTF trend
        of its own (it keeps this ``dashboard_htf_trend``, which returns
        None), whose S/R row reads that trend. A strategy that reads another
        HTF build adds it (the peer family's ``symbol``,
        ``_symbol_htf_request``), and one whose own ``dashboard_htf_trend``
        returns None adds ``generic_trend`` (peer_confirmed_htf_pivots).

        A context carries the price of its first build until the next HTF
        refresh (the price is not part of ``MarketDataStore.htf_cache``'s
        key), so the engine builds every listed one for each step-frame
        symbol at fixed points of the cycle
        (``IntradayBot._prime_strategy_htf_contexts``), never leaving that
        first build to whichever reader comes first: since 2026-10-05 the
        dashboard build, the first reader of most symbols, runs only on
        demand. A read whose arguments are not listed carries the price of
        its own first reader."""
        requests = {"score": self._default_htf_request()}
        if type(self).dashboard_htf_trend is ContextBuildersMixin.dashboard_htf_trend:
            requests["generic_trend"] = self.generic_htf_trend_request()
        return requests

    def generic_htf_trend_request(self) -> dict[str, Any]:
        """The level arguments of the generic HTF trend, which the
        dashboard's S/R row (``sr_snapshot``) shows when the strategy shows
        none of its own (``dashboard_htf_trend`` returns None): EMA 50/200 on
        the strategy's HTF frame, with the support_resistance levels."""
        cfg = self.config.support_resistance
        return {
            "timeframe_minutes": self.htf_minutes(),
            "pivot_span": int(getattr(cfg, "pivot_span", 2) or 2),
            "max_levels_per_side": int(getattr(cfg, "max_levels_per_side", 3) or 3),
            "atr_tolerance_mult": float(cfg.atr_tolerance_mult),  # checked at load (above 0)
            "pct_tolerance": float(cfg.pct_tolerance),
            "stop_buffer_atr_mult": float(getattr(cfg, "stop_buffer_atr_mult", 0.25) or 0.25),
            "ema_fast_span": 50,
            "ema_slow_span": 200,
            "use_prior_day_high_low": bool(getattr(cfg, "use_prior_day_high_low", True)),
            "use_prior_week_high_low": bool(getattr(cfg, "use_prior_week_high_low", True)),
        }

    def _default_htf_context_for_score(self, symbol: str, data) -> HTFContext:
        """The HTF context a strategy scores on (``_default_htf_request``,
        through ``_htf_context``).

        The shared entry policy scores a proposal's HTF RSI divergence on it
        when the proposal brings no HTF context of its own
        (``EntryProposal.htf_ctx``), and top_tier reads its HTF EMA trend
        from it. Until 2026-09-24 it was built on the
        support_resistance timeframe: with an htf_minutes of its own a
        strategy asked for a frame nothing stored, so the context was None
        on every cycle (peer_confirmed_htf_pivots' HTF divergence score was
        always 0 that way).

        No HTF data (no feed, no stored frame yet) is the empty context,
        which every reader takes as None was taken until 2026-09-27: no EMAs,
        a neutral trend, no divergence, so a zero adjustment. A build that
        raises is not "no data" and propagates: until 2026-09-26 it returned
        None, which ``require_htf_ema_alignment`` reads as a neutral trend,
        so the error let the entry through.
        """
        return self._htf_context(symbol, data, **self._default_htf_request())

    def _htf_context(
            self,
            symbol: str,
        data,
        *,
        timeframe_minutes: int,
        pivot_span: int,
        max_levels_per_side: int,
        atr_tolerance_mult: float,
        pct_tolerance: float,
        stop_buffer_atr_mult: float,
        ema_fast_span: int,
        ema_slow_span: int,
        current_price: float | None = None,
        use_prior_day_high_low: bool = True,
        use_prior_week_high_low: bool = True,
    ) -> HTFContext:
        """The HTF context of ``symbol``'s stored frame for these level
        arguments and the strategy's FVG arguments (``htf_fvg_request``), or
        the empty context while no frame is stored. A read: the engine
        refreshes the frames (``IntradayBot._refresh_htf_frames``); until
        2026-09-28 this read fetched a frame whose HTF bar had closed."""
        if data is None or not hasattr(data, "get_htf_context"):
            return empty_htf_context(current_price or 0.0, timeframe_minutes=timeframe_minutes)
        ctx = data.get_htf_context(
            symbol,
            timeframe_minutes=timeframe_minutes,
            pivot_span=pivot_span,
            max_levels_per_side=max_levels_per_side,
            atr_tolerance_mult=atr_tolerance_mult,
            pct_tolerance=pct_tolerance,
            stop_buffer_atr_mult=stop_buffer_atr_mult,
            ema_fast_span=ema_fast_span,
            ema_slow_span=ema_slow_span,
            use_prior_day_high_low=bool(use_prior_day_high_low),
            use_prior_week_high_low=bool(use_prior_week_high_low),
            **self.htf_fvg_request(),
        )
        if ctx is None:
            return empty_htf_context(current_price or 0.0, timeframe_minutes=timeframe_minutes)
        return ctx

    def htf_fvg_request(self) -> dict[str, Any]:
        """The FVG arguments ``_htf_context`` builds every context with. They
        are part of the data feed's context cache key, so the dashboard's HTF
        context read passes them too (until 2026-09-27 it resolved them from
        the config itself)."""
        return {
            "include_fair_value_gaps": bool(self._support_resistance_setting("htf_fair_value_gaps_enabled", True)),
            "fair_value_gap_max_per_side": int(self._support_resistance_setting("fair_value_gap_max_per_side", 4) or 4),
            "fair_value_gap_min_atr_mult": float(self._support_resistance_setting("fair_value_gap_min_atr_mult", 0.05) or 0.05),
            "fair_value_gap_min_pct": float(self._support_resistance_setting("fair_value_gap_min_pct", 0.0005) or 0.0005),
        }

    @staticmethod
    def _htf_bias(htf: HTFContext | None, close: float) -> tuple[str, int, int]:
        """The HTF EMA trend: close vs the fast EMA, the fast vs the slow EMA
        and the context's trend bias each vote; 2 of 3 decide. Returns
        ``(bias, bull_votes, bear_votes)``."""
        bull = 0
        bear = 0
        ema_fast = safe_float(getattr(htf, "ema_fast", None))
        ema_slow = safe_float(getattr(htf, "ema_slow", None))
        if ema_fast is not None:
            if close > ema_fast:
                bull += 1
            elif close < ema_fast:
                bear += 1
        if ema_fast is not None and ema_slow is not None:
            if ema_fast > ema_slow:
                bull += 1
            elif ema_fast < ema_slow:
                bear += 1
        trend_bias = str(getattr(htf, "trend_bias", "neutral"))
        if trend_bias == "bullish":
            bull += 1
        elif trend_bias == "bearish":
            bear += 1
        return "bullish" if bull >= 2 else ("bearish" if bear >= 2 else "neutral"), bull, bear

    @staticmethod
    def _htf_ema_alignment_sides(value: Any) -> frozenset[Side]:
        """The sides ``require_htf_ema_alignment`` gates: ``enabled`` / true
        both, ``long_only`` LONG, ``short_only`` SHORT, ``disabled`` / false
        neither. Anything else raises, naming the key. The mode exists
        because the trend's evidence is one-sided: over 21 archived top_tier
        sessions it separated LONG outcomes and not SHORT ones."""
        if isinstance(value, bool):
            return frozenset({Side.LONG, Side.SHORT}) if value else frozenset()
        mode = str(value).strip().lower()
        modes = {
            "enabled": frozenset({Side.LONG, Side.SHORT}),
            "true": frozenset({Side.LONG, Side.SHORT}),
            "long_only": frozenset({Side.LONG}),
            "short_only": frozenset({Side.SHORT}),
            "disabled": frozenset(),
            "false": frozenset(),
        }
        if mode not in modes:
            raise ValueError(
                "require_htf_ema_alignment must be enabled / true, disabled / false, long_only or short_only, "
                f"got {value!r}"
            )
        return modes[mode]

    @staticmethod
    def _side_vote_edge(side: Side, bullish: int, bearish: int) -> int:
        """Votes FOR ``side`` net of those against it: the
        ``directional_vote_edge`` the entry gatekeeper ranks signals on."""
        net = int(bullish) - int(bearish)
        return net if side == Side.LONG else -net

    @staticmethod
    def _htf_trend_row(bias: str, bull: int, bear: int) -> dict[str, str]:
        label = "Bullish" if bias == "bullish" else ("Bearish" if bias == "bearish" else "—")
        return {"state": bias, "label": label, "votes": f"{bull}v{bear}"}

    def dashboard_htf_trend(self, symbol: str, data, price: float) -> dict[str, str] | None:
        """The HTF trend the dashboard sidebar shows: ``{"state", "label"}``
        from the same read the strategy's decisions use, or None when the
        strategy has none (the sidebar then shows its generic read). Until
        2026-09-24 the sidebar always used a 50/200 context of its own, so on
        a peer_confirmed preset (34/200) it could say "Bullish" while the
        strategy's HTF gate read neutral and blocked the long."""
        return None

    def dashboard_htf_ema_columns(self) -> tuple[str, str] | None:
        """Frame columns the dashboard's HTF chart draws as the fast / slow
        EMA when the strategy's HTF trend reads them directly (zero_dte), or
        None for the default (the strategy's htf_ema_*_span when declared)."""
        return None

    @staticmethod
    def _htf_lists(ctx: HTFContext) -> dict[str, Any]:
        active_sources = {
            str(getattr(level, "source", "") or "").strip().lower()
            for level in [
                *(getattr(ctx, "supports", []) or []),
                *(getattr(ctx, "resistances", []) or []),
                getattr(ctx, "broken_resistance", None),
                getattr(ctx, "broken_support", None),
            ]
            if level is not None
        }
        return {
            "htf_minutes": int(getattr(ctx, "timeframe_minutes", 0) or 0),
            "htf_supports": [float(round(lv.price, 4)) for lv in getattr(ctx, "supports", [])],
            "htf_resistances": [float(round(lv.price, 4)) for lv in getattr(ctx, "resistances", [])],
            "nearest_htf_support": float(ctx.nearest_support.price) if getattr(ctx, "nearest_support", None) else None,
            "broken_htf_resistance": float(ctx.broken_resistance.price) if getattr(ctx, "broken_resistance", None) else None,
            "nearest_htf_resistance": float(ctx.nearest_resistance.price) if getattr(ctx, "nearest_resistance", None) else None,
            "broken_htf_support": float(ctx.broken_support.price) if getattr(ctx, "broken_support", None) else None,
            "prior_day_high": safe_float(getattr(ctx, "prior_day_high", None)) if "prior_day_high" in active_sources else None,
            "prior_day_low": safe_float(getattr(ctx, "prior_day_low", None)) if "prior_day_low" in active_sources else None,
            "prior_week_high": safe_float(getattr(ctx, "prior_week_high", None)) if "prior_week_high" in active_sources else None,
            "prior_week_low": safe_float(getattr(ctx, "prior_week_low", None)) if "prior_week_low" in active_sources else None,
            "htf_ema_fast": safe_float(getattr(ctx, "ema_fast", None)),
            "htf_ema_slow": safe_float(getattr(ctx, "ema_slow", None)),
            "htf_atr14": safe_float(getattr(ctx, "atr14", None)),
            "htf_trend_bias": str(getattr(ctx, "trend_bias", "neutral")),
            "htf_level_buffer": float(getattr(ctx, "level_buffer", 0.0) or 0.0),
            "htf_bullish_fvgs": [
                {
                    "lower": safe_float(getattr(gap, "lower", None)),
                    "upper": safe_float(getattr(gap, "upper", None)),
                    "midpoint": safe_float(getattr(gap, "midpoint", None)),
                    "size": safe_float(getattr(gap, "size", None)),
                    "filled_pct": safe_float(getattr(gap, "filled_pct", None)),
                }
                for gap in (getattr(ctx, "bullish_fvgs", []) or [])
            ],
            "htf_bearish_fvgs": [
                {
                    "lower": safe_float(getattr(gap, "lower", None)),
                    "upper": safe_float(getattr(gap, "upper", None)),
                    "midpoint": safe_float(getattr(gap, "midpoint", None)),
                    "size": safe_float(getattr(gap, "size", None)),
                    "filled_pct": safe_float(getattr(gap, "filled_pct", None)),
                }
                for gap in (getattr(ctx, "bearish_fvgs", []) or [])
            ],
            "nearest_htf_bullish_fvg": safe_float(getattr(getattr(ctx, "nearest_bullish_fvg", None), "midpoint", None)),
            "nearest_htf_bearish_fvg": safe_float(getattr(getattr(ctx, "nearest_bearish_fvg", None), "midpoint", None)),
        }

    def ltf_fvg_request(self) -> dict[str, Any]:
        """The detection arguments of the LTF FVG context
        (``get_fair_value_gap_context`` / ``build_fair_value_gap_context``,
        both take these names); the caller adds the timeframe and the
        price. The dashboard's LTF FVG overlay asks for it with the frame's
        close, as ``_ltf_fvg_context`` does, so the overlay is the context
        the strategy read (until 2026-09-27 it resolved the arguments from
        the config itself and built at the quote's last)."""
        return {
            "max_per_side": int(self._support_resistance_setting("fair_value_gap_max_per_side", 4) or 4),
            "min_gap_atr_mult": float(self._support_resistance_setting("fair_value_gap_min_atr_mult", 0.05) or 0.05),
            "min_gap_pct": float(self._support_resistance_setting("fair_value_gap_min_pct", 0.0005) or 0.0005),
        }

    def _ltf_fvg_context(self, symbol: str, frame: pd.DataFrame | None, data=None) -> FairValueGapContext:
        ltf_min = self.ltf_minutes()
        current_price = safe_float(frame.iloc[-1]["close"], 0.0) if frame is not None and not frame.empty else 0.0
        if not bool(self._support_resistance_setting("ltf_fair_value_gaps_enabled", False)):
            return empty_fvg_context(current_price, timeframe_minutes=ltf_min)
        request = self.ltf_fvg_request()
        if data is not None and hasattr(data, "get_fair_value_gap_context") and symbol:
            try:
                return data.get_fair_value_gap_context(symbol, timeframe_minutes=ltf_min, current_price=current_price, **request)
            except Exception:
                LOG.debug("Failed to load cached fair value gap context for %s; recomputing from frame.", symbol, exc_info=True)
        if frame is None or frame.empty:
            return empty_fvg_context(current_price, timeframe_minutes=ltf_min)
        return build_fair_value_gap_context(frame, timeframe_minutes=ltf_min, current_price=current_price, **request)

    def order_block_request(self) -> dict[str, Any]:
        """The SHARED OB tuning knobs from support_resistance config, under
        the names ``get_order_block_context`` / ``build_order_block_context``
        take; the caller adds the timeframe and the price. Both 1m and HTF
        OB contexts read the same settings — only the enable flag and the
        input frame's timeframe differ between them. The dashboard's OB
        overlays ask for it with the frame's close, as the strategy does
        (until 2026-09-27 they resolved the knobs from the config
        themselves and built at the quote's last).

        The ``min_thrust_atr_mult`` knob (added 2026-05) gates BoS
        displacement to filter micro-breakouts; combined with the
        strength-based sort in build_order_block_context, this stops
        weak close-to-price OBs from displacing strong distant ones.

        ``mode`` is the checked ``order_block_mode`` (``loose`` / ``strict``,
        config._CHOICES), read as it is.
        """
        return {
            "mode": self.config.support_resistance.order_block_mode,
            "max_per_side": int(self._support_resistance_setting("order_block_max_per_side", 4) or 4),
            "min_block_atr_mult": float(self._support_resistance_setting("order_block_min_atr_mult", 0.05) or 0.05),
            "min_block_pct": float(self._support_resistance_setting("order_block_min_pct", 0.0005) or 0.0005),
            "min_thrust_atr_mult": float(
                self._support_resistance_setting("order_block_min_thrust_atr_mult", 0.75) or 0.75
            ),
            "pivot_span": int(self._support_resistance_setting("order_block_pivot_span", 2) or 2),
            "new_high_lookback": int(self._support_resistance_setting("order_block_new_high_lookback", 8) or 8),
        }

    def _ltf_order_block_context(self, symbol: str, frame: pd.DataFrame | None, data=None) -> OrderBlockContext:
        """LTF order block context. Runs on the strategy's
        ``params.ltf_minutes`` frame (defaults to 1-minute streaming bars
        when not declared). Routes through `data.get_order_block_context`
        when available (cycle-cached, avoids redundant builds across multiple
        candidates per cycle and the dashboard). Falls back to inline
        `build_order_block_context` when there's no data store available."""
        ltf_min = self.ltf_minutes()
        current_price = safe_float(frame.iloc[-1]["close"], 0.0) if frame is not None and not frame.empty else 0.0
        request = self.order_block_request()
        mode = request["mode"]
        if not bool(self._support_resistance_setting("ltf_order_blocks_enabled", False)):
            return empty_order_block_context(current_price, timeframe_minutes=ltf_min, mode=mode)
        if data is not None and hasattr(data, "get_order_block_context") and symbol:
            try:
                return data.get_order_block_context(symbol, timeframe_minutes=ltf_min, current_price=current_price, **request)
            except Exception:
                LOG.debug("Failed to load cached order block context for %s; recomputing from frame.", symbol, exc_info=True)
        if frame is None or frame.empty:
            return empty_order_block_context(current_price, timeframe_minutes=ltf_min, mode=mode)
        return build_order_block_context(frame, timeframe_minutes=ltf_min, current_price=current_price, **request)

    def _htf_order_block_context(self, symbol: str, frame: pd.DataFrame | None, data=None) -> OrderBlockContext:
        """HTF order block context. Disabled by default — opt in via
        `support_resistance.htf_order_blocks_enabled: true`. Uses the same
        tuning knobs as 1m OBs; the only difference is the input frame is
        resampled to the HTF timeframe (default 15m via
        `support_resistance.timeframe_minutes`).

        Routes through `data.get_order_block_context` when available so the
        HTF resample + OB detection is shared with the dashboard via the
        cycle-scoped cache."""
        current_price = safe_float(frame.iloc[-1]["close"], 0.0) if frame is not None and not frame.empty else 0.0
        request = self.order_block_request()
        mode = request["mode"]
        htf_minutes = self.htf_minutes()
        if not bool(self._support_resistance_setting("htf_order_blocks_enabled", False)):
            return empty_order_block_context(current_price, timeframe_minutes=htf_minutes, mode=mode)
        if data is not None and hasattr(data, "get_order_block_context") and symbol:
            try:
                return data.get_order_block_context(symbol, timeframe_minutes=htf_minutes, current_price=current_price, **request)
            except Exception:
                LOG.debug("Failed to load cached HTF order block context for %s; recomputing from frame.", symbol, exc_info=True)
        if frame is None or frame.empty:
            return empty_order_block_context(current_price, timeframe_minutes=htf_minutes, mode=mode)
        htf_frame = self._resampled_frame(frame, htf_minutes, symbol=symbol, data=data)
        if htf_frame is None or htf_frame.empty:
            return empty_order_block_context(current_price, timeframe_minutes=htf_minutes, mode=mode)
        return build_order_block_context(htf_frame, timeframe_minutes=htf_minutes, current_price=current_price, **request)

    @staticmethod
    def _resampled_frame(
        frame: pd.DataFrame | None,
        timeframe_minutes: int,
        *,
        symbol: str | None = None,
        data=None,
        span_scale: float = 1.0,
        ema_spans: tuple[int, int] | None = None,
    ) -> pd.DataFrame | None:
        # span_scale stretches every indicator lookback so a fine timeframe can
        # carry a coarser timeframe's wall-clock horizon (top_tier's 1m LTF uses
        # span_scale=5); ema_spans sets the ema9 / ema20 spans on their own
        # (ltf_ema_fast_span / ltf_ema_slow_span). Defaults = canonical spans,
        # unchanged for every other caller. The merged-frame cache keys the
        # enriched frame by both, so such a request never collides with the
        # shared canonical frame.
        if frame is None or frame.empty:
            return None
        tf = max(1, int(timeframe_minutes))
        if data is not None and symbol and hasattr(data, "get_merged"):
            try:
                cached = data.get_merged(str(symbol), timeframe=f"{tf}min", with_indicators=True,
                                         span_scale=span_scale, ema_spans=ema_spans)
                if cached is not None and not cached.empty:
                    return cached
            except Exception:
                LOG.debug("Failed to load cached %s-minute merged frame for %s; resampling from base frame.", tf, symbol, exc_info=True)
        if tf <= 1:
            return ensure_standard_indicator_frame(frame.copy(), span_scale=span_scale, ema_spans=ema_spans)

        def build() -> pd.DataFrame:
            return ensure_standard_indicator_frame(resample_bars(frame, f"{tf}min"), span_scale=span_scale,
                                                   ema_spans=ema_spans)

        # A step frame from get_merged carries its source token: the resample
        # reads only its OHLCV bars, so the token names the result, which is
        # then built once per new bar instead of every cycle (the structure
        # context's call above passes no `data`). The source frame's own
        # indicator columns and span never reach the result: the resample
        # keeps OHLCV only and add_indicators restamps the span attr.
        token = frame_source_token(frame)
        if token is None:
            return build()
        spans = None if ema_spans is None else tuple(ema_spans)
        variant = (tf, span_scale, spans, get_runtime_indicator_mode(), get_session_indicator_window())
        return derived_frame(token, variant, build)

    def _structure_context(self, frame: pd.DataFrame | None, timeframe: str = "ltf"):
        # Per-cycle cache. Timeframe goes in the key because the pivot_span /
        # pct_tolerance branches below differ by timeframe token (LTF gets
        # the structure_ltf_* overrides via `_is_ltf_token`). All strategy
        # call sites pass "ltf" so the analysis tracks the strategy's
        # `params.ltf_minutes` (default 1m). Records (name, timeframe) so
        # the engine pre-warms the right variant (on a bars frame only, see
        # _observe_context).
        timeframe_token = str(timeframe).lower()
        self._observe_context(frame, ("structure", timeframe_token))
        frame_key = self._technical_context_cache_key(frame)
        cache_key = (frame_key, timeframe_token)
        with self._structure_context_lock:
            cached = self._structure_context_cache.get(cache_key)
            if cached is not None:
                return cached[1]
        current_price = safe_float(frame.iloc[-1]["close"], 0.0) if frame is not None and not frame.empty else 0.0
        if frame is None or frame.empty or not bool(self._support_resistance_setting("structure_enabled", True)):
            empty_ctx = empty_market_structure_context(current_price)
            with self._structure_context_lock:
                self._structure_context_cache[cache_key] = (frame, empty_ctx)
            return empty_ctx
        pivot_span = int(self._support_resistance_setting("pivot_span", 2) or 2)
        is_ltf_analysis = self._is_ltf_token(timeframe_token)
        # A step frame written after its hand-out (its index or OHLCV columns
        # no longer the ones get_merged handed out) is analysed as an
        # unregistered copy: nothing kept on its token or version (the 5m
        # structure frame, the memoized context below) is served for bars it
        # no longer holds, and its build is not kept.
        bars_frame = verified_frame(frame)
        analysis_frame = bars_frame
        bar_minutes: int | None = None
        if is_ltf_analysis:
            pivot_span = int(self._support_resistance_setting("structure_ltf_pivot_span", max(2, pivot_span)) or max(2, pivot_span))
            # Fix D (2026-05-27): optionally resample the LTF structure frame
            # to a coarser timeframe so structure pivots track the bars the
            # strategy actually trades on (params.ltf_minutes) instead of the
            # raw 1m stream. 0/1 = use the frame as-is (original behavior).
            ltf_tf_min = int(self._support_resistance_setting("structure_ltf_timeframe_minutes", 0) or 0)
            if ltf_tf_min > 1:
                resampled = self._resampled_frame(bars_frame, ltf_tf_min)
                if resampled is not None and not resampled.empty:
                    analysis_frame = resampled
                    bar_minutes = ltf_tf_min
                    current_price = safe_float(analysis_frame.iloc[-1]["close"], current_price)
        if bar_minutes is None:
            bar_minutes = frame_bar_minutes(analysis_frame.index)
        # The resample keeps the still-forming last bucket, and so does a
        # peer's native 5m LTF frame (get_merged resamples the live 1m
        # stream). Its first minutes must not confirm a pivot (2026-09-25,
        # see analyze_market_structure). The same clock test as the
        # dashboard's forming bucket and data_feed._completed_bars
        # (bars.last_bucket_forming / completed_bucket_mask); a frame of
        # completed 1m bars never reads as forming.
        def clock_key() -> tuple:
            return last_bucket_forming(analysis_frame.index, bar_minutes, sessions.now_et()), indicator_clock_key()

        pct_tolerance = float(self.config.support_resistance.pct_tolerance)  # checked at load (above 0)
        if is_ltf_analysis:
            pct_tolerance *= 0.60
        structure_event_max_age_bars = int(self._support_resistance_setting("structure_event_lookback_bars", 6) or 6)

        def build(clock: tuple):
            return analyze_market_structure(
                analysis_frame,
                current_price=current_price,
                pivot_span=pivot_span,
                eq_atr_mult=float(self._support_resistance_setting("structure_eq_atr_mult", 0.25) or 0.25),
                pct_tolerance=pct_tolerance,
                breakout_atr_mult=float(self._support_resistance_setting("breakout_atr_mult", 0.35) or 0.35),
                breakout_buffer_pct=float(self._support_resistance_setting("breakout_buffer_pct", 0.0015) or 0.0015),
                structure_event_max_age_bars=structure_event_max_age_bars,
                min_range_atr_mult=float(self._support_resistance_setting("structure_min_range_atr_mult", 1.5) or 0.0),
                min_pivot_gap_bars=int(self._support_resistance_setting("structure_min_pivot_gap_bars", 0) or 0),
                last_bar_forming=clock[0],
            )

        # The analysis frame (the frame, or its memoized resample) is a
        # function of the frame's version, so the version and the clock as
        # the build reads it (the forming last bucket, the ATR's session
        # switch) are every input. The forming flag is read once, into the
        # key and the build alike.
        version = frame_version(bars_frame)
        if version is None:
            ctx = build(clock_key())
        else:
            ctx = self._context_memo.serve(
                ("structure", timeframe_token, version[0][0], version[0][2], version[1]),
                (version,),
                clock_key,
                build,
            )
        with self._structure_context_lock:
            self._structure_context_cache[cache_key] = (frame, ctx)
        return ctx

    @staticmethod
    def _structure_lists(ctx, prefix: str = "ms") -> dict[str, Any]:
        return {
            f"{prefix}_bias": str(getattr(ctx, "bias", "neutral") or "neutral"),
            f"{prefix}_pivot_bias": str(getattr(ctx, "pivot_bias", "neutral") or "neutral"),
            f"{prefix}_last_high_label": getattr(ctx, "last_high_label", None),
            f"{prefix}_last_low_label": getattr(ctx, "last_low_label", None),
            f"{prefix}_last_pivot_kind": getattr(ctx, "last_pivot_kind", None),
            f"{prefix}_last_pivot_label": getattr(ctx, "last_pivot_label", None),
            f"{prefix}_bos_up": bool(getattr(ctx, "bos_up", False)),
            f"{prefix}_bos_down": bool(getattr(ctx, "bos_down", False)),
            f"{prefix}_choch_up": bool(getattr(ctx, "choch_up", False)),
            f"{prefix}_choch_down": bool(getattr(ctx, "choch_down", False)),
            f"{prefix}_bos_up_age_bars": getattr(ctx, "bos_up_age_bars", None),
            f"{prefix}_bos_down_age_bars": getattr(ctx, "bos_down_age_bars", None),
            f"{prefix}_choch_up_age_bars": getattr(ctx, "choch_up_age_bars", None),
            f"{prefix}_choch_down_age_bars": getattr(ctx, "choch_down_age_bars", None),
            f"{prefix}_eqh": bool(getattr(ctx, "eqh", False)),
            f"{prefix}_eql": bool(getattr(ctx, "eql", False)),
            f"{prefix}_structure_age_bars": getattr(ctx, "structure_age_bars", None),
            f"{prefix}_event_age_bars": getattr(ctx, "event_age_bars", None),
            f"{prefix}_reference_high": getattr(ctx, "reference_high", None),
            f"{prefix}_reference_low": getattr(ctx, "reference_low", None),
            f"{prefix}_pivot_count": int(getattr(ctx, "pivot_count", 0) or 0),
            # Tight-EQH+EQL detector (2026-05-14): when both EQ flags coexist
            # within < structure_min_range_atr_mult ATR, bias resolves to
            # "neutral" to suppress noise-driven structure exits. Surface
            # both the spread (for tuning) and the boolean trigger.
            f"{prefix}_structure_range_atr": float(getattr(ctx, "structure_range_atr", 0.0) or 0.0),
            f"{prefix}_tight_structure_range": bool(getattr(ctx, "tight_structure_range", False)),
            f"{prefix}_reason": str(getattr(ctx, "reason", "unknown") or "unknown"),
        }

    @staticmethod
    def _technical_context_cache_key(frame: pd.DataFrame | None) -> tuple[Any, ...]:
        """Per-cycle cache key for the chart / structure / technical caches.
        `id(frame)` is the discriminator: each symbol has its own DataFrame.

        An id only names a LIVE object -- CPython hands a freed frame's
        address to the next allocation -- so every cache entry stores its
        frame as ``(frame, ctx)``: while the entry exists the frame cannot be
        freed, and no other frame can arrive with its id. Until 2026-09-23
        the entries held only the ctx, and the peer_confirmed strategies
        build a fresh ``get_merged(...).copy()`` per candidate that dies at
        the next rebind: over 728 such 5m copies on 09-22, 21 took a freed
        frame's id and one (NFLX after AAPL at 13:40) matched its whole
        key, which serves AAPL's context for NFLX -- `len` and the last-bar
        stamp cannot tell symbols apart, because every 5m frame in a cycle
        ends on the same bucket and liquid names share a length. They stay
        in the key to catch a frame grown in place (`frame.loc[ts] = ...`).
        """
        if frame is None or frame.empty:
            return ("empty",)
        last_idx = frame.index[-1]
        try:
            last_marker = last_idx.isoformat()  # type: ignore[attr-defined]
        except AttributeError:
            # An index label that is not a timestamp.
            last_marker = repr(last_idx)
        return id(frame), len(frame), last_marker

    def _technical_context(self, frame: pd.DataFrame | None) -> TechnicalLevelsContext:
        self._observe_context(frame, ("technical",))
        cache_key = self._technical_context_cache_key(frame)
        with self._technical_context_lock:
            cached = self._technical_context_cache.get(cache_key)
            if cached is not None:
                return cached[1]
        current_price = safe_float(frame.iloc[-1]["close"], 0.0) if frame is not None and not frame.empty else 0.0
        if frame is None or frame.empty or not bool(self._technical_level_setting("enabled", True)):
            empty_ctx = empty_technical_levels_context(current_price)
            with self._technical_context_lock:
                self._technical_context_cache[cache_key] = (frame, empty_ctx)
            return empty_ctx
        version = frame_version(frame)
        if version is None:
            ctx = self._build_technical_context(frame, current_price)
        else:
            # Reads the clock only through the ATR's and the divergence
            # clocks' session switch (tools/clock_audit), plus the session
            # indicator settings: indicator_clock_key.
            ctx = self._context_memo.serve(
                ("technical", None, version[0][0], version[0][2], version[1]),
                (version,),
                indicator_clock_key,
                lambda _clock: self._build_technical_context(frame, current_price),
            )
        with self._technical_context_lock:
            self._technical_context_cache[cache_key] = (frame, ctx)
        return ctx

    def _build_technical_context(self, frame: pd.DataFrame, current_price: float) -> TechnicalLevelsContext:
        cfg = getattr(self.config, "technical_levels", None)
        sr_cfg = getattr(self.config, "support_resistance", None)
        pivot_span = int(self._support_resistance_setting("structure_ltf_pivot_span", self._support_resistance_setting("pivot_span", 2)) or 2) if sr_cfg is not None else 2
        return build_technical_levels_context(
            frame,
            current_price=current_price,
            pivot_span=max(1, pivot_span),
            fib_lookback_bars=int(self._technical_level_setting("fib_lookback_bars", 120) or 120),
            fib_min_impulse_atr=float(self._technical_level_setting("fib_min_impulse_atr", 1.25) or 1.25),
            anchored_vwap_impulse_lookback_bars=safe_int(self._technical_level_setting("anchored_vwap_impulse_lookback_bars", None)),
            anchored_vwap_min_impulse_atr=safe_float(self._technical_level_setting("anchored_vwap_min_impulse_atr", None)),
            anchored_vwap_pivot_span=safe_int(self._technical_level_setting("anchored_vwap_pivot_span", None)),
            trendline_lookback_bars=int(self._technical_level_setting("trendline_lookback_bars", 120) or 120),
            trendline_min_touches=int(self._technical_level_setting("trendline_min_touches", 3) or 3),
            trendline_atr_tolerance_mult=float(self._technical_level_setting("trendline_atr_tolerance_mult", 0.35) or 0.35),
            trendline_breakout_buffer_atr_mult=float(self._technical_level_setting("trendline_breakout_buffer_atr_mult", 0.65)),
            channel_lookback_bars=int(self._technical_level_setting("channel_lookback_bars", 120) or 120),
            channel_min_touches=int(self._technical_level_setting("channel_min_touches", 3) or 3),
            channel_atr_tolerance_mult=float(self._technical_level_setting("channel_atr_tolerance_mult", 0.35) or 0.35),
            channel_parallel_slope_frac=float(self._technical_level_setting("channel_parallel_slope_frac", 0.12) or 0.12),
            channel_min_gap_atr_mult=float(self._technical_level_setting("channel_min_gap_atr_mult", 0.80) or 0.80),
            channel_min_gap_pct=float(self._technical_level_setting("channel_min_gap_pct", 0.0025) or 0.0025),
            bollinger_length=int(self._technical_level_setting("bollinger_length", 20) or 20),
            bollinger_std_mult=float(self._technical_level_setting("bollinger_std_mult", 2.0) or 2.0),
            bollinger_squeeze_width_pct=float(getattr(cfg, "bollinger_squeeze_width_pct", 0.060) or 0.060),
            atr_expansion_lookback=int(self._technical_level_setting("atr_expansion_lookback", 5) or 5),
            adx_length=int(self._technical_level_setting("adx_length", 14) or 14),
            obv_ema_length=int(self._technical_level_setting("obv_ema_length", 20) or 20),
            divergence_rsi_length=int(self._technical_level_setting("divergence_rsi_length", 14) or 14),
            # The three thresholds the HTF divergence shares are read as
            # configured, as the HTF build (data_feed.get_htf_context) reads
            # them: a configured 0 is honoured (the builder clamps it) and a
            # null raises on both timeframes alike. `or <default>` read a 0
            # as the default here and in the dashboard's LTF build until
            # 2026-09-25.
            divergence_rsi_min_delta=float(self._technical_level_setting("divergence_rsi_min_delta", 2.5)),
            divergence_obv_min_volume_frac=float(getattr(cfg, "divergence_obv_min_volume_frac", 0.50) or 0.50),
            divergence_pivot_lookback=int(self._technical_level_setting("divergence_pivot_lookback", 4)),
            # No `or 8`: a configured 0 (only a divergence ending on the
            # last bar) is honoured (2026-09-24).
            divergence_max_age_bars=int(self._technical_level_setting("divergence_max_age_bars", 8)),
            divergence_min_price_move_pct=float(self._technical_level_setting("divergence_min_price_move_pct", 0.0015)),
            fib_enabled=bool(self._technical_level_setting("fib_enabled", True)),
            channel_enabled=bool(self._technical_level_setting("channel_enabled", True)),
            trendline_enabled=bool(self._technical_level_setting("trendline_enabled", True)),
            adx_enabled=bool(self._technical_level_setting("adx_enabled", True)),
            anchored_vwap_enabled=bool(self._technical_level_setting("anchored_vwap_enabled", True)),
            atr_context_enabled=bool(self._technical_level_setting("atr_context_enabled", True)),
            obv_enabled=bool(self._technical_level_setting("obv_enabled", True)),
            divergence_enabled=bool(self._technical_level_setting("divergence_enabled", True)),
            bollinger_enabled=bool(self._technical_level_setting("bollinger_enabled", True)),
        )

    def _technical_lists(self, ctx, prefix: str = "tech") -> dict[str, Any]:
        cfg_enabled = bool(self._technical_level_setting("enabled", True))
        ch = getattr(ctx, "channel", None) if cfg_enabled and bool(self._technical_level_setting("channel_enabled", True)) else None
        support_line = getattr(ctx, "support_trendline", None) if cfg_enabled and bool(self._technical_level_setting("trendline_enabled", True)) else None
        resistance_line = getattr(ctx, "resistance_trendline", None) if cfg_enabled and bool(self._technical_level_setting("trendline_enabled", True)) else None

        fib_enabled = bool(cfg_enabled and self._technical_level_setting("fib_enabled", True))
        avwap_enabled = bool(cfg_enabled and self._technical_level_setting("anchored_vwap_enabled", True))
        adx_enabled = bool(cfg_enabled and self._technical_level_setting("adx_enabled", True))
        atr_enabled = bool(cfg_enabled and self._technical_level_setting("atr_context_enabled", True))
        obv_enabled = bool(cfg_enabled and self._technical_level_setting("obv_enabled", True))
        divergence_enabled = bool(cfg_enabled and self._technical_level_setting("divergence_enabled", True))
        trendline_enabled = bool(cfg_enabled and self._technical_level_setting("trendline_enabled", True))
        channel_enabled = bool(cfg_enabled and self._technical_level_setting("channel_enabled", True))
        bollinger_enabled = bool(cfg_enabled and self._technical_level_setting("bollinger_enabled", True))

        out = {
            f"{prefix}_fib_direction": str(getattr(ctx, "fib_direction", "neutral") or "neutral") if fib_enabled else "neutral",
            f"{prefix}_fib_anchor_low": getattr(ctx, "fib_anchor_low", None) if fib_enabled else None,
            f"{prefix}_fib_anchor_high": getattr(ctx, "fib_anchor_high", None) if fib_enabled else None,
            f"{prefix}_fib_bullish_1272": getattr(ctx, "fib_bullish_1272", None) if fib_enabled else None,
            f"{prefix}_fib_bullish_1618": getattr(ctx, "fib_bullish_1618", None) if fib_enabled else None,
            f"{prefix}_fib_bearish_1272": getattr(ctx, "fib_bearish_1272", None) if fib_enabled else None,
            f"{prefix}_fib_bearish_1618": getattr(ctx, "fib_bearish_1618", None) if fib_enabled else None,
            f"{prefix}_nearest_bullish_extension": getattr(ctx, "nearest_bullish_extension", None) if fib_enabled else None,
            f"{prefix}_nearest_bullish_extension_ratio": getattr(ctx, "nearest_bullish_extension_ratio", None) if fib_enabled else None,
            f"{prefix}_bullish_extension_distance_pct": getattr(ctx, "bullish_extension_distance_pct", None) if fib_enabled else None,
            f"{prefix}_nearest_bearish_extension": getattr(ctx, "nearest_bearish_extension", None) if fib_enabled else None,
            f"{prefix}_nearest_bearish_extension_ratio": getattr(ctx, "nearest_bearish_extension_ratio", None) if fib_enabled else None,
            f"{prefix}_bearish_extension_distance_pct": getattr(ctx, "bearish_extension_distance_pct", None) if fib_enabled else None,
            f"{prefix}_anchored_vwap_open": getattr(ctx, "anchored_vwap_open", None) if avwap_enabled else None,
            f"{prefix}_anchored_vwap_bullish_impulse": getattr(ctx, "anchored_vwap_bullish_impulse", None) if avwap_enabled else None,
            f"{prefix}_anchored_vwap_bearish_impulse": getattr(ctx, "anchored_vwap_bearish_impulse", None) if avwap_enabled else None,
            f"{prefix}_anchored_vwap_bias": getattr(ctx, "anchored_vwap_bias", None) if avwap_enabled else "neutral",
            f"{prefix}_adx": getattr(ctx, "adx", None) if adx_enabled else None,
            f"{prefix}_plus_di": getattr(ctx, "plus_di", None) if adx_enabled else None,
            f"{prefix}_minus_di": getattr(ctx, "minus_di", None) if adx_enabled else None,
            f"{prefix}_dmi_bias": getattr(ctx, "dmi_bias", None) if adx_enabled else "neutral",
            f"{prefix}_adx_rising": bool(getattr(ctx, "adx_rising", False)) if adx_enabled else False,
            f"{prefix}_atr14": getattr(ctx, "atr14", None) if atr_enabled else None,
            f"{prefix}_atr_pct": getattr(ctx, "atr_pct", None) if atr_enabled else None,
            f"{prefix}_atr_expansion_mult": getattr(ctx, "atr_expansion_mult", None) if atr_enabled else None,
            f"{prefix}_atr_stretch_vwap_mult": getattr(ctx, "atr_stretch_vwap_mult", None) if atr_enabled else None,
            f"{prefix}_atr_stretch_ema20_mult": getattr(ctx, "atr_stretch_ema20_mult", None) if atr_enabled else None,
            f"{prefix}_obv": getattr(ctx, "obv", None) if obv_enabled else None,
            f"{prefix}_obv_ema": getattr(ctx, "obv_ema", None) if obv_enabled else None,
            f"{prefix}_obv_bias": getattr(ctx, "obv_bias", None) if obv_enabled else "neutral",
            f"{prefix}_rsi14": getattr(ctx, "rsi14", None) if divergence_enabled else None,
            f"{prefix}_bullish_rsi_divergence": (getattr(ctx, "bullish_rsi_divergence", None) is not None) if divergence_enabled else False,
            f"{prefix}_bearish_rsi_divergence": (getattr(ctx, "bearish_rsi_divergence", None) is not None) if divergence_enabled else False,
            f"{prefix}_bullish_obv_divergence": (getattr(ctx, "bullish_obv_divergence", None) is not None) if divergence_enabled else False,
            f"{prefix}_bearish_obv_divergence": (getattr(ctx, "bearish_obv_divergence", None) is not None) if divergence_enabled else False,
            f"{prefix}_bullish_hidden_rsi_divergence": (getattr(ctx, "bullish_hidden_rsi_divergence", None) is not None) if divergence_enabled else False,
            f"{prefix}_bearish_hidden_rsi_divergence": (getattr(ctx, "bearish_hidden_rsi_divergence", None) is not None) if divergence_enabled else False,
            f"{prefix}_bullish_hidden_obv_divergence": (getattr(ctx, "bullish_hidden_obv_divergence", None) is not None) if divergence_enabled else False,
            f"{prefix}_bearish_hidden_obv_divergence": (getattr(ctx, "bearish_hidden_obv_divergence", None) is not None) if divergence_enabled else False,
            f"{prefix}_counter_divergence_bias": getattr(ctx, "counter_divergence_bias", None) if divergence_enabled else "neutral",
            f"{prefix}_support_touches": getattr(support_line, "touches", None) if trendline_enabled else None,
            f"{prefix}_support_current": getattr(support_line, "current_value", None) if trendline_enabled else None,
            f"{prefix}_support_direction": getattr(support_line, "direction", None) if trendline_enabled else None,
            f"{prefix}_resistance_touches": getattr(resistance_line, "touches", None) if trendline_enabled else None,
            f"{prefix}_resistance_current": getattr(resistance_line, "current_value", None) if trendline_enabled else None,
            f"{prefix}_resistance_direction": getattr(resistance_line, "direction", None) if trendline_enabled else None,
            f"{prefix}_trendline_break_up": bool(getattr(ctx, "trendline_break_up", False)) if trendline_enabled else False,
            f"{prefix}_trendline_break_down": bool(getattr(ctx, "trendline_break_down", False)) if trendline_enabled else False,
            f"{prefix}_support_respected": bool(getattr(ctx, "support_respected", False)) if trendline_enabled else False,
            f"{prefix}_resistance_respected": bool(getattr(ctx, "resistance_respected", False)) if trendline_enabled else False,
            f"{prefix}_support_distance_pct": getattr(ctx, "support_distance_pct", None) if trendline_enabled else None,
            f"{prefix}_resistance_distance_pct": getattr(ctx, "resistance_distance_pct", None) if trendline_enabled else None,
            f"{prefix}_channel_valid": bool(getattr(ch, "valid", False)) if channel_enabled else False,
            f"{prefix}_channel_bias": getattr(ch, "bias", None) if channel_enabled else None,
            f"{prefix}_channel_lower": getattr(ch, "lower", None) if channel_enabled else None,
            f"{prefix}_channel_upper": getattr(ch, "upper", None) if channel_enabled else None,
            f"{prefix}_channel_mid": getattr(ch, "mid", None) if channel_enabled else None,
            f"{prefix}_channel_position_pct": getattr(ch, "position_pct", None) if channel_enabled else None,
            f"{prefix}_channel_width": getattr(ch, "width", None) if channel_enabled else None,
            f"{prefix}_channel_lower_touches": getattr(ch, "lower_touches", None) if channel_enabled else None,
            f"{prefix}_channel_upper_touches": getattr(ch, "upper_touches", None) if channel_enabled else None,
            f"{prefix}_bollinger_mid": getattr(ctx, "bollinger_mid", None) if bollinger_enabled else None,
            f"{prefix}_bollinger_upper": getattr(ctx, "bollinger_upper", None) if bollinger_enabled else None,
            f"{prefix}_bollinger_lower": getattr(ctx, "bollinger_lower", None) if bollinger_enabled else None,
            f"{prefix}_bollinger_width": getattr(ctx, "bollinger_width", None) if bollinger_enabled else None,
            f"{prefix}_bollinger_width_pct": getattr(ctx, "bollinger_width_pct", None) if bollinger_enabled else None,
            f"{prefix}_bollinger_percent_b": getattr(ctx, "bollinger_percent_b", None) if bollinger_enabled else None,
            f"{prefix}_bollinger_zscore": getattr(ctx, "bollinger_zscore", None) if bollinger_enabled else None,
            f"{prefix}_bollinger_squeeze": bool(getattr(ctx, "bollinger_squeeze", False)) if bollinger_enabled else False,
            f"{prefix}_bollinger_upper_reject": bool(getattr(ctx, "bollinger_upper_reject", False)) if bollinger_enabled else False,
            f"{prefix}_bollinger_lower_reject": bool(getattr(ctx, "bollinger_lower_reject", False)) if bollinger_enabled else False,
            f"{prefix}_reason": str(getattr(ctx, "reason", "unknown") or "unknown"),
        }
        return out
