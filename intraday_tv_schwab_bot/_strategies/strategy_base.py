# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Mapping
from threading import RLock
from typing import TYPE_CHECKING, ClassVar, Iterable

from .helpers import (
    _bar_close_position,
    _bar_wick_fractions,
    _dashboard_zone_width_from_policy,
    _normalize_symbol_list,
    _normalize_symbol_list_details,
    _optional_float,
    _optional_int,
    _position_strategy_matches,
    _reason_with_values,
    _safe_float,
)
from ..event_blackouts import EventBlackoutCalendar
from ..order_blocks import (
    OrderBlockContext,
    build_order_block_context,
    empty_order_block_context,
)
from .shared_entry import SharedEntryPolicy
from ..models import OPTION_ASSET_TYPES, ExitDecision
from ..utils import frame_bar_minutes, session_bucket_ends
from .shared import (
    Any,
    Candidate,
    FairValueGapContext,
    HTFContext,
    LOG,
    Position,
    Side,
    Signal,
    TechnicalLevelsContext,
    analyze_chart_pattern_context,
    analyze_market_structure,
    build_fair_value_gap_context,
    build_technical_levels_context,
    equity_session_state,
    detect_candle_context,
    directional_candle_signal,
    ensure_standard_indicator_frame,
    empty_fvg_context,
    empty_htf_context,
    htf_ema_spans,
    empty_market_structure_context,
    empty_support_resistance_context,
    empty_technical_levels_context,
    now_et,
    pd,
    resample_bars,
    time,
)

if TYPE_CHECKING:
    from ..config import BotConfig
    from .shared_exit import ExitTape


class BaseStrategy:
    strategy_name: str | None = None

    # Class-level flag for the adaptive_ladder trade-management mode.
    # Set to False by strategies that should NEVER run ladder mode (e.g.
    # options strategies). When config.risk.trade_management_mode is
    # "adaptive_ladder" and this flag is False, the engine logs a one-time
    # warning at startup so the user knows ladder mechanics are inactive.
    supports_adaptive_ladder: bool = True

    # Auto-detected set of context-builder calls the strategy has made over
    # its lifetime. Each entry is a tuple `(name, *args)` — e.g. `("chart",)`,
    # `("structure", "ltf")`, `("technical",)`. Populated lazily on first
    # invocation of each builder ON ONE OF THE CYCLE'S BARS FRAMES (see
    # _observe_context). The engine reads this set every cycle
    # (after _prime_cycle_support_cache) to drive _prime_cycle_context_cache,
    # which pre-warms the observed contexts in parallel via
    # _parallel_symbol_map. Cycle 1 is lazy (set is empty); cycles 2+ benefit.
    # __init_subclass__ gives each subclass its own set so different strategy
    # classes don't cross-contaminate.
    _observed_contexts: ClassVar[set[tuple]] = set()

    # The shared exit families belong to shared_exit.SharedExitPolicy, which
    # the position manager owns; a strategy adds its own exits through
    # strategy_exit_signal. Until 2026-09-24 the pipeline was a BaseStrategy
    # method any subclass could override, and the peer family's override
    # silently dropped the time stop and five exit families (see
    # shared_exit.py). The entry side is the same: the shared entry stage is
    # shared_entry.SharedEntryPolicy (self.entry_policy), the gatekeeper
    # ranks with its rank_key, and the knob-rewriting hook
    # strategy_logic_default is gone. Defining any of these names now fails
    # at import, so an out-of-tree plugin cannot quietly opt out of the
    # global knobs.
    _RESERVED_NAMES: ClassVar[dict[str, str]] = {
        "position_exit_signal": (
            "shared exits are decided by shared_exit.SharedExitPolicy for every strategy and cannot be "
            "overridden -- put strategy-only exits in strategy_exit_signal()"
        ),
        "shared_exit_signal": (
            "shared exits are decided by shared_exit.SharedExitPolicy for every strategy and cannot be "
            "overridden -- put strategy-only exits in strategy_exit_signal()"
        ),
        "strategy_logic_default": (
            "a strategy cannot rewrite a shared_entry / shared_exit knob; set it in the preset YAML, or "
            "exempt a style from a veto in the manifest (capabilities.shared_entry.exemptions)"
        ),
        "signal_priority_key": (
            "signals are ranked by shared_entry.SharedEntryPolicy.rank_key; declare the ranking in the "
            "manifest (capabilities.signal_priority)"
        ),
    }

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name, why in BaseStrategy._RESERVED_NAMES.items():
            if name in cls.__dict__:
                raise TypeError(f"{cls.__name__} defines {name}(); {why}")
        cls._observed_contexts = set()

    @classmethod
    def normalize_params(cls, params: dict[str, Any]) -> dict[str, Any]:
        return dict(params or {})

    def _manifest_capabilities(self) -> dict[str, Any]:
        raw = getattr(self._manifest, "capabilities", None)
        return raw if isinstance(raw, dict) else {}

    def _capability(self, path: str, default: Any = None) -> Any:
        node: Any = self._manifest_capabilities()
        for part in str(path or "").split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node.get(part)
        return node

    def _options_capability_enabled(self) -> bool:
        checker = getattr(self, "_options_enabled", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return True

    def _symbols_from_capability_source(self, source: object) -> list[str] | None:
        token = str(source or "").strip().lower()
        if not token or token == "none":
            return []
        if token == "all":
            return None
        if token == "dashboard_tradable_symbols":
            return self.dashboard_tradable_symbols()
        if token.startswith("params."):
            key = token.split(".", 1)[1]
            if isinstance(self.params, dict):
                return _normalize_symbol_list(self.params.get(key))
            return []
        if token.startswith("options."):
            if not self._options_capability_enabled():
                return []
            optcfg = getattr(self.config, "options", None)
            if token == "options.underlyings":
                return _normalize_symbol_list(getattr(optcfg, "underlyings", []))
            if token == "options.confirmation_symbols":
                values = getattr(optcfg, "confirmation_symbols", {})
                if isinstance(values, dict):
                    values = values.values()
                return _normalize_symbol_list(values)
            if token == "options.volatility_symbol":
                return _normalize_symbol_list([getattr(optcfg, "volatility_symbol", "")])
            return []
        if token == "pairs.symbols":
            return _normalize_symbol_list(getattr(pair, "symbol", "") for pair in (getattr(self, "pairs", None) or []))
        if token == "pairs.references":
            return _normalize_symbol_list(getattr(pair, "reference", "") for pair in (getattr(self, "pairs", None) or []))
        return []

    def __init__(self, config: BotConfig):
        self.config = config
        if self.strategy_name and str(self.strategy_name).strip() != str(config.strategy).strip():
            raise ValueError(
                f"{self.__class__.__name__}.strategy_name={self.strategy_name!r} does not match active config strategy {config.strategy!r}"
            )
        self.params = config.active_strategy.params
        # A bad htf_ema_fast_span / htf_ema_slow_span fails here, naming the
        # key: every HTF-EMA consumer resolves them through htf_ema_spans, and
        # the HTF context builders swallow errors (a bad pair used to turn
        # into "no HTF context" -- no EMA gate, no HTF divergence -- silently).
        htf_ema_spans(self.params)
        self._manifest = None
        try:
            from .registry import get_plugin
            self._manifest = get_plugin(config.strategy)
        except Exception:
            self._manifest = None
        # Scheduled-event calendar (macro windows + per-symbol earnings),
        # shared by every strategy. Lazily re-reads its YAML sources when
        # their mtime changes, so it is safe to build once here.
        self._event_calendar = EventBlackoutCalendar(config)
        self._entry_decisions: dict[str, dict[str, Any]] = {}
        self._build_failures: dict[tuple[str, str], dict[str, Any]] = {}
        self._candle_context_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        # Values are (frame, ctx): see _technical_context_cache_key.
        self._technical_context_cache: dict[tuple[Any, ...], tuple[pd.DataFrame | None, Any]] = {}
        self._structure_context_cache: dict[tuple[Any, ...], tuple[pd.DataFrame | None, Any]] = {}
        self._chart_context_cache: dict[tuple[Any, ...], tuple[pd.DataFrame | None, Any]] = {}
        # Locks protect the 3 context dicts when the engine pre-warms them
        # in parallel via _parallel_symbol_map. Different worker threads
        # write distinct cache_keys, but the dict mutations themselves still
        # need protection. Compute happens outside the locks, so threads
        # never wait on each other for the heavy work.
        self._chart_context_lock = RLock()
        self._structure_context_lock = RLock()
        self._technical_context_lock = RLock()
        # id -> frame of this cycle's bars frames, the only frames the
        # engine pre-warms (set_prewarm_frames). A builder records its call
        # in _observed_contexts only for one of them. Holding the frames keeps
        # their ids from being handed to another frame mid-cycle.
        self._prewarm_frames: dict[int, pd.DataFrame] = {}
        # The shared entry stage: the only reader of config.shared_entry
        # (see shared_entry.py). Built last -- it reads the manifest.
        self.entry_policy = SharedEntryPolicy(self)

    def _watchlist_capability_sources(self, kind: str) -> list[object] | None:
        raw = self._capability(f"watchlist.{kind}_sources", None)
        return raw if isinstance(raw, list) else None

    @staticmethod
    def _watchlist_source_label(source: object) -> str:
        if isinstance(source, str):
            return source.strip() or '<blank>'
        if isinstance(source, dict):
            kind = str(source.get("source") or "dict").strip() or "dict"
            if kind in {"params.keys", "params.keys_if_true"}:
                details = ",".join(str(item).strip() for item in source.get("keys", []) if str(item).strip())
                return f"{kind}({details})" if details else kind
            if kind in {"positions.metadata", "positions.metadata_list"}:
                key = str(source.get("key") or "").strip()
                return f"{kind}({key})" if key else kind
            return kind
        return str(type(source).__name__)

    def watchlist_trace(
        self,
        kind: str,
        candidates: list[Candidate],
        positions: dict[str, Position],
        *,
        bars: dict[str, pd.DataFrame] | None = None,
        active_symbols: set[str] | None = None,
    ) -> dict[str, dict[str, list[str]]] | None:
        sources = self._watchlist_capability_sources(kind)
        if sources is None:
            if kind == "active":
                fallback_sources: list[object] = ["candidates", "positions.underlyings_or_symbols", "positions.reference_symbols"]
                sources = fallback_sources
            elif kind == "quote":
                sources = ["active_watchlist"]
            else:
                return None
        trace: dict[str, dict[str, list[str]]] = {}
        for source in sources:
            label = self._watchlist_source_label(source)
            raw_values = self._watchlist_source_values(source, candidates, positions, bars=bars, active_symbols=active_symbols)
            normalized, skipped = _normalize_symbol_list_details(raw_values)
            trace[label] = {
                "symbols": normalized,
                "skipped": skipped,
            }
        return trace

    def _watchlist_source_values(
        self,
        source: object,
        candidates: list[Candidate],
        positions: dict[str, Position],
        *,
        bars: dict[str, pd.DataFrame] | None = None,
        active_symbols: set[str] | None = None,
    ) -> object:
        _ = bars
        if isinstance(source, str):
            token = source.strip().lower()
            if not token:
                return []
            if token == "candidates":
                return [getattr(c, "symbol", "") for c in candidates]
            if token == "positions.symbols":
                return [getattr(p, "symbol", "") for p in positions.values()]
            if token == "positions.reference_symbols":
                return [getattr(p, "reference_symbol", "") for p in positions.values()]
            if token == "positions.underlyings_or_symbols":
                return [
                    getattr(p, "metadata", {}).get("underlying") or getattr(p, "symbol", "")
                    for p in positions.values()
                ]
            if token == "active_watchlist":
                if active_symbols is not None:
                    return list(active_symbols)
                return list(self.active_watchlist(candidates, positions))
            return self._symbols_from_capability_source(token) or []
        if not isinstance(source, dict):
            return []
        kind = str(source.get("source") or "").strip().lower()
        if kind == "params.keys":
            if not isinstance(self.params, dict):
                return []
            keys = [str(item).strip() for item in source.get("keys", []) if str(item).strip()]
            values: list[object] = []
            for key in keys:
                raw_value = self.params.get(key)
                if isinstance(raw_value, (list, tuple, set, frozenset)):
                    values.extend(list(raw_value))
                elif raw_value not in (None, ""):
                    values.append(raw_value)
            return values
        if kind == "params.keys_if_true":
            if not isinstance(self.params, dict):
                return []
            flag = str(source.get("flag") or "").strip()
            if not flag or not bool(self.params.get(flag)):
                return []
            keys = [str(item).strip() for item in source.get("keys", []) if str(item).strip()]
            values: list[object] = []
            for key in keys:
                raw_value = self.params.get(key)
                if isinstance(raw_value, (list, tuple, set, frozenset)):
                    values.extend(list(raw_value))
                elif raw_value not in (None, ""):
                    values.append(raw_value)
            return values
        if kind in {"positions.metadata", "positions.metadata_list"}:
            key = str(source.get("key") or "").strip()
            strategy_names = [str(item).strip().lower() for item in source.get("strategy_names", []) if str(item).strip()]
            values: list[object] = []
            for position in positions.values():
                if not _position_strategy_matches(position, strategy_names):
                    continue
                metadata = getattr(position, "metadata", {}) if isinstance(getattr(position, "metadata", {}), dict) else {}
                raw_value = metadata.get(key)
                if kind == "positions.metadata_list":
                    if isinstance(raw_value, (list, tuple, set, frozenset)):
                        values.extend(list(raw_value))
                    elif raw_value not in (None, ""):
                        values.append(raw_value)
                elif raw_value not in (None, ""):
                    values.append(raw_value)
            return values
        return []

    def _watchlist_symbols_from_source(
        self,
        source: object,
        candidates: list[Candidate],
        positions: dict[str, Position],
        *,
        bars: dict[str, pd.DataFrame] | None = None,
        active_symbols: set[str] | None = None,
    ) -> list[str]:
        raw_values = self._watchlist_source_values(source, candidates, positions, bars=bars, active_symbols=active_symbols)
        return _normalize_symbol_list(raw_values)

    def _watchlist_symbols_from_capabilities(
        self,
        kind: str,
        candidates: list[Candidate],
        positions: dict[str, Position],
        *,
        bars: dict[str, pd.DataFrame] | None = None,
        active_symbols: set[str] | None = None,
    ) -> set[str] | None:
        sources = self._watchlist_capability_sources(kind)
        if sources is None:
            return None
        symbols: set[str] = set()
        for source in sources:
            symbols.update(self._watchlist_symbols_from_source(source, candidates, positions, bars=bars, active_symbols=active_symbols))
        return {token for token in _normalize_symbol_list(symbols)}

    def dashboard_tradable_symbols(self) -> list[str]:
        source = self._capability("dashboard.tradable_symbols_source", None)
        if source is not None:
            return self._symbols_from_capability_source(source) or []
        if not isinstance(self.params, dict):
            return []
        raw_symbols = self.params.get("tradable")
        if raw_symbols is None:
            raw_symbols = self.params.get("symbols")
        return _normalize_symbol_list(raw_symbols)

    def dashboard_index_symbols(self) -> list[str]:
        """Return the union of ETFs used for directional confirmation:
        ``params.index_symbols`` (the flat streamed list) plus every ETF
        referenced by ``params.sector_index_map`` (per-sector overrides).
        Surfaced to the dashboard payload so the watchlist UI can tag
        these cards with an "IX" chip and the user can distinguish them
        from tradable entry symbols at a glance. Strategies that don't
        use index confirmation (either param absent) return an empty
        list. Subclasses can override to provide a custom source."""
        if not isinstance(self.params, dict):
            return []
        symbols: set[str] = set()
        raw_index = self.params.get("index_symbols")
        if raw_index:
            symbols.update(_normalize_symbol_list(raw_index))
        sector_map = self.params.get("sector_index_map")
        if isinstance(sector_map, dict):
            for tickers in sector_map.values():
                symbols.update(_normalize_symbol_list(tickers))
        return sorted(symbols)

    def restore_eligible_symbols(self) -> list[str] | None:
        source = self._capability("startup_restore.eligible_symbols_source", "dashboard_tradable_symbols")
        token = str(source or "").strip().lower()
        if token == "all":
            return None
        if token == "dashboard_tradable_symbols":
            symbols = self.dashboard_tradable_symbols()
        else:
            symbols = self._symbols_from_capability_source(token)
        return symbols or None

    def requires_hybrid_startup_restore_metadata(self) -> bool:
        return bool(self._capability("startup_restore.require_hybrid_metadata", False))

    def dashboard_candidate_limit(self, default_limit: int) -> int:
        mode = str(self._capability("dashboard.candidate_limit_mode", "default") or "default").strip().lower()
        if mode == "tradable_count":
            symbols = self.dashboard_tradable_symbols()
            return len(symbols) if symbols else max(1, int(default_limit))
        if mode == "fixed":
            try:
                return max(1, int(self._capability("dashboard.candidate_limit", default_limit)))
            except Exception:
                return max(1, int(default_limit))
        return max(1, int(default_limit))

    def dashboard_allow_generic_level_fallback(self) -> bool:
        return bool(self._capability("dashboard.allow_generic_level_fallback", False))

    def dashboard_level_context_spec(self) -> dict[str, Any] | None:
        """The HTF level build the dashboard's key-level zones use. A level
        parameter the strategy does not declare as ``htf_*`` comes from
        ``support_resistance``, the values it trades on; until 2026-09-23 it
        fell back to 60m / 60 days / 6 levels / 0.35 ATR, so top_tier's zones
        (and every other preset without htf_* params) were a build the
        strategy never used."""
        params = self.params if isinstance(self.params, dict) else {}
        sr_cfg = self.config.support_resistance
        spec = {
            "timeframe_minutes": max(1, self._htf_minutes()),
            "lookback_days": max(1, self._htf_lookback_days()),
            "pivot_span": max(1, int(params.get("htf_pivot_span", sr_cfg.pivot_span))),
            "max_levels_per_side": max(1, int(params.get("htf_max_levels_per_side", sr_cfg.max_levels_per_side))),
            "atr_tolerance_mult": float(params.get("htf_atr_tolerance_mult", sr_cfg.atr_tolerance_mult)),
            "pct_tolerance": float(params.get("htf_pct_tolerance", sr_cfg.pct_tolerance)),
            "stop_buffer_atr_mult": float(params.get("htf_stop_buffer_atr_mult", sr_cfg.stop_buffer_atr_mult)),
            "ema_fast_span": htf_ema_spans(params)[0],
            "ema_slow_span": htf_ema_spans(params)[1],
            "ltf_minutes": max(1, int(params.get("ltf_minutes", 5) or 5)),
            "min_level_score": float(params.get("min_level_score", 4.0) or 4.0),
            "level_round_number_tolerance_pct": float(params.get("level_round_number_tolerance_pct", 0.0020) or 0.0020),
            "base_zone_atr_mult": float(params.get("zone_atr_mult", params.get("pivot_zone_atr_mult", 0.20)) or 0.20),
            "base_zone_pct": float(params.get("zone_pct", params.get("pivot_zone_pct", 0.0015)) or 0.0015),
        }
        overrides = self._capability("dashboard.level_context", None)
        if isinstance(overrides, dict):
            spec.update({k: v for k, v in overrides.items() if v is not None})
        return spec

    def dashboard_candidate_label(self, kind_name: str, zone_kind: str) -> str:
        name = str(kind_name or "").strip().lower()
        raw_map = self._capability("dashboard.candidate_labels", None)
        if isinstance(raw_map, dict):
            configured = raw_map.get(name)
            if configured is None:
                configured = raw_map.get(str(zone_kind or "").strip().lower())
            if isinstance(configured, str) and configured.strip():
                return configured.strip()
        label_map = {
            "prior_day_low": "PDL",
            "prior_day_high": "PDH",
            "prior_week_low": "PWL",
            "prior_week_high": "PWH",
            "nearest_htf_support": "HS",
            "nearest_htf_resistance": "HR",
            "support": "HS",
            "resistance": "HR",
            "broken_htf_resistance": "BR",
            "broken_htf_support": "BS",
            "bullish_htf_fvg": "BFVG",
            "bearish_htf_fvg": "RFVG",
            "bullish_continuation_trigger": "CT",
            "bearish_continuation_trigger": "CT",
            "bullish_pullback_anchor": "PA",
            "bearish_pullback_anchor": "PA",
            "bullish_htf_pivot_support": "HP",
            "bearish_htf_pivot_resistance": "HP",
        }
        return label_map.get(name, "HS" if zone_kind == "support" else "HR")

    def dashboard_candidate_sources(self, kind_name: str, zone_kind: str) -> list[str]:
        name = str(kind_name or "").strip().lower()
        raw_map = self._capability("dashboard.candidate_sources", None)
        if isinstance(raw_map, dict):
            configured = raw_map.get(name)
            if configured is None:
                configured = raw_map.get(str(zone_kind or "").strip().lower())
            if isinstance(configured, str) and configured.strip():
                return [configured.strip()]
            if isinstance(configured, list):
                out = [str(item).strip() for item in configured if str(item).strip()]
                if out:
                    return out
        source = str(kind_name or "").strip()
        return [source] if source else []

    def dashboard_candidate_levels(self, close: float, htf: HTFContext, side: Side) -> list[dict[str, Any]]:
        return []

    def dashboard_select_level(self, side: Side, close: float, ltf: pd.DataFrame, htf: HTFContext) -> dict[str, Any] | None:
        return None

    def _resolve_dashboard_zone_width_policy(self, candidate: dict[str, Any] | None = None) -> dict[str, Any] | None:
        raw = self._capability("dashboard.zone_width", None)
        if not isinstance(raw, dict):
            return None
        policy = raw
        kind_overrides = raw.get("kind_overrides")
        kind_name = str((candidate or {}).get("kind") or "").strip().lower()
        if kind_name and isinstance(kind_overrides, dict):
            override = kind_overrides.get(kind_name)
            if isinstance(override, dict):
                policy = override
        return policy if isinstance(policy, dict) else None

    def dashboard_zone_width_for_level(
        self,
        side: Side,
        close: float,
        atr: float,
        level_price: float,
        htf: HTFContext,
        candidate: dict[str, Any] | None = None,
    ) -> float | None:
        policy = self._resolve_dashboard_zone_width_policy(candidate)
        if isinstance(policy, dict):
            return _dashboard_zone_width_from_policy(policy, close=float(close), atr=float(atr))
        return None

    def dashboard_overlay_candidates(self, side: Side, close: float, ltf: pd.DataFrame, htf: HTFContext) -> list[dict[str, Any]] | None:
        return None

    def _manifest_required_history_bars(self) -> int | None:
        raw = self._capability("history.required_bars", None)
        if raw is None:
            return None
        try:
            return max(0, int(raw))
        except Exception:
            return None

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        try:
            return max(0, int(self.params.get("min_bars", 0) or 0))
        except Exception:
            return 0

    @staticmethod
    def _frame_atr14(frame: pd.DataFrame | None, close: float) -> float:
        """ATR14 from the last bar of ``frame``, with a price-scaled fallback."""
        if frame is not None and not frame.empty and "atr14" in frame.columns:
            return _safe_float(frame.iloc[-1]["atr14"], close * 0.0015)
        return max(close * 0.0015, 0.01)

    def _technical_level_setting(self, key: str, default: Any) -> Any:
        cfg = getattr(self.config, "technical_levels", None)
        return getattr(cfg, key, default) if cfg is not None else default

    def _support_resistance_setting(self, key: str, default: Any) -> Any:
        cfg = getattr(self.config, "support_resistance", None)
        return getattr(cfg, key, default) if cfg is not None else default

    def _chart_pattern_setting(self, key: str, default: Any) -> Any:
        cfg = getattr(self.config, "chart_patterns", None)
        return getattr(cfg, key, default) if cfg is not None else default

    def _candles_setting(self, key: str, default: Any) -> Any:
        cfg = getattr(self.config, "candles", None)
        return getattr(cfg, key, default) if cfg is not None else default

    def _force_flatten_settings(self) -> dict[str, bool]:
        raw = self.params.get("force_flatten", {}) if isinstance(self.params, dict) else {}
        settings: dict[str, bool] = {}
        if not isinstance(raw, dict):
            return settings
        if "long" in raw:
            settings["long"] = bool(raw.get("long", False))
        if "short" in raw:
            settings["short"] = bool(raw.get("short", False))
        return settings

    def _management_window_end_time(self) -> time | None:
        windows = getattr(self.config.active_strategy.schedule(), "management_windows", [])
        if not windows:
            return None
        try:
            return max((window.end for window in windows), default=None)
        except Exception:
            return None

    def _configurable_stock_force_flatten(self, position: Position, default_enabled: bool = True) -> bool:
        settings = self._force_flatten_settings()
        side_key = "long" if position.side == Side.LONG else "short"
        enabled = bool(settings.get(side_key, default_enabled))
        if not enabled:
            return False
        cutoff = self._management_window_end_time()
        if cutoff is None:
            return False
        # Apply a configurable buffer so the flatten fires *before* the
        # management window closes, giving the order time to fill before
        # the real market close.  Default 5 minutes.
        buffer = max(0, int(self.params.get("force_flatten_buffer_minutes", 5) or 0))
        cutoff_minutes = cutoff.hour * 60 + cutoff.minute
        adjusted_minutes = max(0, cutoff_minutes - buffer)
        # On early-close days, clamp the flatten time so it fires before the
        # early close rather than hours after the market has already closed.
        now_dt = now_et()
        state = equity_session_state(now_dt)
        if state.early_close:
            early_m = state.rth_close_time.hour * 60 + state.rth_close_time.minute - buffer
            adjusted_minutes = min(adjusted_minutes, max(0, early_m))
        adjusted_cutoff = time(adjusted_minutes // 60, adjusted_minutes % 60)
        return now_dt.time() >= adjusted_cutoff

    def _reset_entry_decisions(self) -> None:
        # Per-cycle decision tracking. Called at the start of every strategy's
        # entry_signals(). Does NOT touch the chart/structure/technical context
        # caches anymore — those are pre-warmed by the engine before
        # entry_signals runs and would be wiped here. The engine resets them
        # via reset_context_caches() inside _prime_cycle_context_cache, on
        # the cycle boundary instead of the entry_signals boundary.
        self._entry_decisions = {}
        self._build_failures = {}
        self._candle_context_cache = {}
        self.entry_policy.reset_cycle()

    def reset_context_caches(self) -> None:
        """Cycle-boundary cache cleanup for the three pre-warmed context caches.

        Public API for the engine. Called inside `_prime_cycle_context_cache`
        before the parallel dispatch populates caches for the new cycle's
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
        Each builder is self-caching under its own RLock, so this is safe
        to call from worker threads in parallel across watchlist symbols.
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

    def _entry_side_context(self, preferred_sides: list[Side]) -> tuple[list[Side], list[str]]:
        allow_short = bool(self.config.risk.allow_short)
        filtered: list[Side] = []
        evaluated_sides: list[str] = []
        seen: set[str] = set()
        for side in preferred_sides:
            if side == Side.SHORT and not allow_short:
                continue
            token = str(side.value)
            if token in seen:
                continue
            seen.add(token)
            filtered.append(side)
            evaluated_sides.append(token)
        return filtered, evaluated_sides

    def _record_entry_decision(
        self,
        symbol: str,
        action: str,
        reasons: list[str] | tuple[str, ...] | None = None,
        *,
        context: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        cleaned: list[str] = []
        for item in reasons or []:
            token = str(item or '').strip()
            if token and token not in cleaned:
                cleaned.append(token)
        payload: dict[str, Any] = {"action": str(action), "reasons": cleaned}
        if isinstance(context, Mapping):
            context_payload = {str(k): v for k, v in context.items() if v is not None}
            if context_payload:
                payload["context"] = context_payload
        if isinstance(details, Mapping):
            detail_payload = {str(k): v for k, v in details.items() if v is not None}
            if detail_payload:
                payload["details"] = detail_payload
        self._entry_decisions[str(symbol)] = payload

    def pull_entry_decisions(self) -> dict[str, dict[str, Any]]:
        out = dict(self._entry_decisions)
        self._entry_decisions = {}
        return out

    def _set_build_failure(
        self,
        symbol: str,
        style: str,
        reason: str,
        *,
        reasons: list[str] | tuple[str, ...] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a build failure for ``symbol`` under ``style`` (regime).

        ``reason`` is the primary failure tag (always used for the
        ``primary_reason`` field). ``reasons`` is an optional fuller list;
        when omitted, defaults to ``[reason]``. When both are passed, the
        primary tag is prepended to ``reasons`` if missing, so the primary
        is always discoverable in the list — prevents the latent mismatch
        where ``primary_reason`` could differ from every element of
        ``reasons``.
        """
        cleaned: list[str] = []
        # Always seed with the primary reason so it can never be absent
        # from the reasons list — even if the caller passes a kwarg list
        # that omits it.
        primary_token = str(reason or '').strip()
        if primary_token:
            cleaned.append(primary_token)
        for item in reasons or []:
            token = str(item or '').strip()
            if token and token not in cleaned:
                cleaned.append(token)
        payload: dict[str, Any] = {
            "primary_reason": primary_token,
            "reasons": cleaned,
        }
        if isinstance(details, Mapping):
            detail_payload = {str(k): v for k, v in details.items() if v is not None}
            if detail_payload:
                payload["details"] = detail_payload
        self._build_failures[(str(symbol), str(style))] = payload

    def _consume_build_failure(self, symbol: str, style: str) -> str | None:
        payload = self._build_failures.pop((str(symbol), str(style)), None)
        if isinstance(payload, Mapping):
            primary = payload.get("primary_reason")
            if primary is not None:
                return str(primary)
            reasons = payload.get("reasons")
            if isinstance(reasons, (list, tuple)) and reasons:
                return str(reasons[0])
        return str(payload) if payload is not None else None

    def _consume_build_failure_payload(self, symbol: str, style: str) -> dict[str, Any] | None:
        payload = self._build_failures.pop((str(symbol), str(style)), None)
        if payload is None:
            return None
        if isinstance(payload, Mapping):
            return {str(k): v for k, v in payload.items()}
        return {"primary_reason": str(payload), "reasons": [str(payload)]}

    def _chart_context(self, frame: pd.DataFrame):
        # Per-cycle cache keyed like _technical_context (see
        # _technical_context_cache_key). Records the call signature in
        # _observed_contexts (on a bars frame only, see _observe_context) so
        # the engine can pre-warm this context in parallel for next cycle's
        # watchlist.
        self._observe_context(frame, ("chart",))
        cache_key = self._technical_context_cache_key(frame)
        with self._chart_context_lock:
            cached = self._chart_context_cache.get(cache_key)
            if cached is not None:
                return cached[1]
        if not bool(self._chart_pattern_setting("enabled", True)):
            ctx = analyze_chart_pattern_context(frame, bullish_allowed=[], bearish_allowed=[], lookback_bars=0)
            with self._chart_context_lock:
                self._chart_context_cache[cache_key] = (frame, ctx)
            return ctx
        cfg = getattr(self.config, "chart_patterns", None)
        bullish_allowed = list(getattr(cfg, "bullish_patterns", []))
        bearish_allowed = list(getattr(cfg, "bearish_patterns", []))
        lookback_bars = int(self._chart_pattern_setting("lookback_bars", getattr(cfg, "lookback_bars", 32) if cfg is not None else 32))
        ctx = analyze_chart_pattern_context(
            frame,
            bullish_allowed=bullish_allowed,
            bearish_allowed=bearish_allowed,
            lookback_bars=lookback_bars,
        )
        with self._chart_context_lock:
            self._chart_context_cache[cache_key] = (frame, ctx)
        return ctx

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
            from ..candles import CANDLE_CONTEXT_BARS
            tail = frame[["open", "high", "low", "close"]].tail(CANDLE_CONTEXT_BARS).copy()
            for col in ("open", "high", "low", "close"):
                tail[col] = pd.to_numeric(tail[col], errors="coerce")
            tail = tail.dropna(subset=["open", "high", "low", "close"])
            rows: list[tuple[Any, ...]] = []
            for idx, row in tail.iterrows():
                try:
                    idx_marker = idx.isoformat()  # type: ignore[attr-defined]
                except Exception:
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

    @staticmethod
    def _direction_token(position: Position) -> str:
        direction = str(position.metadata.get("direction") or "").strip().lower()
        if direction.startswith("bullish"):
            return "bullish"
        if direction.startswith("bearish"):
            return "bearish"
        return "bullish" if position.side == Side.LONG else "bearish"

    def _entry_exhaustion_reasons(self, side: Side, frame: pd.DataFrame | None, *, close: float, vwap: float, ema9: float) -> list[str]:
        if frame is None or frame.empty:
            return []
        if not bool(self.params.get("entry_exhaustion_filter_enabled", True)):
            return []
        atr = _safe_float(frame.iloc[-1].get("atr14"), max(abs(float(close)) * 0.0015, 0.01))
        atr = max(atr, max(abs(float(close)) * 0.0005, 0.01))
        max_vwap_ext_atr = max(0.1, float(self.params.get("max_entry_vwap_extension_atr", 0.95)))
        max_ema9_ext_atr = max(0.1, float(self.params.get("max_entry_ema9_extension_atr", 0.75)))
        max_bar_range_atr = max(0.25, float(self.params.get("max_entry_bar_range_atr", 1.7)))
        max_upper_wick_frac = min(0.95, max(0.05, float(self.params.get("max_entry_upper_wick_frac", 0.30))))
        max_lower_wick_frac = min(0.95, max(0.05, float(self.params.get("max_entry_lower_wick_frac", 0.30))))
        wick_close_pos_guard = min(0.95, max(0.05, float(self.params.get("entry_wick_close_position_guard", 0.62))))
        upper_wick_frac, lower_wick_frac, _, bar_range = _bar_wick_fractions(frame)
        close_pos = _bar_close_position(frame)
        reasons: list[str] = []
        if side == Side.LONG:
            vwap_ext_atr = max(0.0, float(close) - float(vwap)) / atr if float(vwap) > 0 else 0.0
            ema9_ext_atr = max(0.0, float(close) - float(ema9)) / atr if float(ema9) > 0 else 0.0
            if vwap_ext_atr > max_vwap_ext_atr:
                reasons.append(_reason_with_values("too_extended_from_vwap_atr", current=vwap_ext_atr, required=max_vwap_ext_atr, op="<=", digits=4))
            if ema9_ext_atr > max_ema9_ext_atr:
                reasons.append(_reason_with_values("too_extended_from_ema9_atr", current=ema9_ext_atr, required=max_ema9_ext_atr, op="<=", digits=4))
            if upper_wick_frac > max_upper_wick_frac and close_pos < wick_close_pos_guard:
                reasons.append(_reason_with_values("upper_wick_rejection", current=upper_wick_frac, required=max_upper_wick_frac, op="<=", digits=4, extras={"close_position": (close_pos, ">=", wick_close_pos_guard)}))
            if (bar_range / atr) > max_bar_range_atr and (vwap_ext_atr > max_vwap_ext_atr * 0.75 or ema9_ext_atr > max_ema9_ext_atr * 0.75):
                reasons.append(_reason_with_values("expansion_bar_too_large", current=(bar_range / atr), required=max_bar_range_atr, op="<=", digits=4))
        else:
            vwap_ext_atr = max(0.0, float(vwap) - float(close)) / atr if float(vwap) > 0 else 0.0
            ema9_ext_atr = max(0.0, float(ema9) - float(close)) / atr if float(ema9) > 0 else 0.0
            if vwap_ext_atr > max_vwap_ext_atr:
                reasons.append(_reason_with_values("too_extended_from_vwap_atr", current=vwap_ext_atr, required=max_vwap_ext_atr, op="<=", digits=4))
            if ema9_ext_atr > max_ema9_ext_atr:
                reasons.append(_reason_with_values("too_extended_from_ema9_atr", current=ema9_ext_atr, required=max_ema9_ext_atr, op="<=", digits=4))
            if lower_wick_frac > max_lower_wick_frac and close_pos > (1.0 - wick_close_pos_guard):
                reasons.append(_reason_with_values("lower_wick_rejection", current=lower_wick_frac, required=max_lower_wick_frac, op="<=", digits=4, extras={"close_position": (close_pos, "<=", 1.0 - wick_close_pos_guard)}))
            if (bar_range / atr) > max_bar_range_atr and (vwap_ext_atr > max_vwap_ext_atr * 0.75 or ema9_ext_atr > max_ema9_ext_atr * 0.75):
                reasons.append(_reason_with_values("expansion_bar_too_large", current=(bar_range / atr), required=max_bar_range_atr, op="<=", digits=4))
        return reasons

    def _htf_minutes(self) -> int:
        """HTF (higher timeframe) for SR detection. Strategies declare via
        `params.htf_minutes`; otherwise inherit
        `support_resistance.timeframe_minutes`."""
        fallback = int(self._support_resistance_setting("timeframe_minutes", 15))
        return int(self.params.get("htf_minutes", fallback))

    def _htf_lookback_days(self) -> int:
        fallback = int(self._support_resistance_setting("lookback_days", 10))
        return int(self.params.get("htf_lookback_days", fallback))

    def _ltf_minutes(self) -> int:
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
        ltf_min = self._ltf_minutes()
        if ltf_min == 1:
            return normalized in {"1m", "1min", "minute", "execution"}
        return normalized in {f"{ltf_min}m", f"{ltf_min}min"}

    def _sr_context(self, symbol: str, frame: pd.DataFrame | None, data):
        current_price = float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        timeframe_minutes = self._htf_minutes()
        if not bool(self._support_resistance_setting("enabled", True)) or data is None:
            return empty_support_resistance_context(current_price, timeframe_minutes=timeframe_minutes)
        ctx = data.get_support_resistance(
            symbol,
            current_price=current_price,
            flip_frame=frame,
            mode="trading",
            timeframe_minutes=timeframe_minutes,
            lookback_days=self._htf_lookback_days(),
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
            **BaseStrategy._structure_lists(ms_ctx, prefix="mshtf"),
        }

    def _default_htf_context_for_score(self, symbol: str, data):
        """The HTF context a strategy scores on: its own HTF frame
        (``_htf_minutes`` / ``_htf_lookback_days``, the frame the engine
        refreshes), the support_resistance level settings, and its HTF EMA
        spans. It never refreshes.

        The shared entry policy scores a proposal's HTF RSI divergence on it
        when the proposal brings no HTF context of its own
        (``EntryProposal.htf_ctx``), and top_tier reads its HTF EMA trend
        from it. Until 2026-09-24 it was built on the
        support_resistance timeframe: with an htf_minutes of its own a
        strategy asked for a frame nothing stored, so the context was None
        on every cycle (peer_confirmed_htf_pivots' HTF divergence score was
        always 0 that way).

        Returns ``None`` if HTF data isn't available — the score path is
        defensive (None ctx -> zero adjustment).
        """
        if data is None or not hasattr(data, "get_htf_context"):
            return None
        sr_cfg = getattr(self.config, "support_resistance", None)
        if sr_cfg is None:
            return None
        ema_fast_span, ema_slow_span = htf_ema_spans(self.params)
        try:
            return data.get_htf_context(
                symbol,
                timeframe_minutes=int(self._htf_minutes()),
                lookback_days=int(self._htf_lookback_days()),
                pivot_span=int(getattr(sr_cfg, "pivot_span", 2) or 2),
                max_levels_per_side=int(getattr(sr_cfg, "max_levels_per_side", 6) or 6),
                atr_tolerance_mult=float(getattr(sr_cfg, "atr_tolerance_mult", 0.35) or 0.35),
                pct_tolerance=float(getattr(sr_cfg, "pct_tolerance", 0.0030) or 0.0030),
                stop_buffer_atr_mult=float(getattr(sr_cfg, "stop_buffer_atr_mult", 0.25) or 0.25),
                # The strategy's own HTF EMA spans: top_tier trades on this
                # context's EMA trend (require_htf_ema_alignment /
                # htf_ema_alignment_score). Until 2026-09-24 50/200 was
                # hard-coded here whatever htf_ema_*_span said.
                ema_fast_span=ema_fast_span,
                ema_slow_span=ema_slow_span,
                use_prior_day_high_low=bool(getattr(sr_cfg, "use_prior_day_high_low", True)),
                use_prior_week_high_low=bool(getattr(sr_cfg, "use_prior_week_high_low", True)),
                allow_refresh=False,
            )
        except Exception:
            return None

    def _htf_context(
            self,
            symbol: str,
        data,
        *,
        timeframe_minutes: int,
        lookback_days: int,
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
        allow_refresh: bool = True,
    ) -> HTFContext:
        if data is None or not hasattr(data, "get_htf_context"):
            return empty_htf_context(current_price or 0.0, timeframe_minutes=timeframe_minutes)
        ctx = data.get_htf_context(
            symbol,
            timeframe_minutes=timeframe_minutes,
            lookback_days=lookback_days,
            pivot_span=pivot_span,
            max_levels_per_side=max_levels_per_side,
            atr_tolerance_mult=atr_tolerance_mult,
            pct_tolerance=pct_tolerance,
            stop_buffer_atr_mult=stop_buffer_atr_mult,
            ema_fast_span=ema_fast_span,
            ema_slow_span=ema_slow_span,
            use_prior_day_high_low=bool(use_prior_day_high_low),
            use_prior_week_high_low=bool(use_prior_week_high_low),
            allow_refresh=bool(allow_refresh),
            **self._htf_fvg_request(),
        )
        if ctx is None:
            return empty_htf_context(current_price or 0.0, timeframe_minutes=timeframe_minutes)
        return ctx

    def _htf_fvg_request(self) -> dict[str, Any]:
        """The FVG arguments ``_htf_context`` builds every context with. They
        are part of the data feed's context cache key, so a prefetch meant to
        warm a context the strategy reads has to pass them too."""
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
        ema_fast = _optional_float(getattr(htf, "ema_fast", None))
        ema_slow = _optional_float(getattr(htf, "ema_slow", None))
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

    def dashboard_htf_trend(self, symbol: str, data, price: float, *, allow_refresh: bool = True) -> dict[str, str] | None:
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
            "prior_day_high": _optional_float(getattr(ctx, "prior_day_high", None)) if "prior_day_high" in active_sources else None,
            "prior_day_low": _optional_float(getattr(ctx, "prior_day_low", None)) if "prior_day_low" in active_sources else None,
            "prior_week_high": _optional_float(getattr(ctx, "prior_week_high", None)) if "prior_week_high" in active_sources else None,
            "prior_week_low": _optional_float(getattr(ctx, "prior_week_low", None)) if "prior_week_low" in active_sources else None,
            "htf_ema_fast": _optional_float(getattr(ctx, "ema_fast", None)),
            "htf_ema_slow": _optional_float(getattr(ctx, "ema_slow", None)),
            "htf_atr14": _optional_float(getattr(ctx, "atr14", None)),
            "htf_trend_bias": str(getattr(ctx, "trend_bias", "neutral")),
            "htf_level_buffer": float(getattr(ctx, "level_buffer", 0.0) or 0.0),
            "htf_bullish_fvgs": [
                {
                    "lower": _optional_float(getattr(gap, "lower", None)),
                    "upper": _optional_float(getattr(gap, "upper", None)),
                    "midpoint": _optional_float(getattr(gap, "midpoint", None)),
                    "size": _optional_float(getattr(gap, "size", None)),
                    "filled_pct": _optional_float(getattr(gap, "filled_pct", None)),
                }
                for gap in (getattr(ctx, "bullish_fvgs", []) or [])
            ],
            "htf_bearish_fvgs": [
                {
                    "lower": _optional_float(getattr(gap, "lower", None)),
                    "upper": _optional_float(getattr(gap, "upper", None)),
                    "midpoint": _optional_float(getattr(gap, "midpoint", None)),
                    "size": _optional_float(getattr(gap, "size", None)),
                    "filled_pct": _optional_float(getattr(gap, "filled_pct", None)),
                }
                for gap in (getattr(ctx, "bearish_fvgs", []) or [])
            ],
            "nearest_htf_bullish_fvg": _optional_float(getattr(getattr(ctx, "nearest_bullish_fvg", None), "midpoint", None)),
            "nearest_htf_bearish_fvg": _optional_float(getattr(getattr(ctx, "nearest_bearish_fvg", None), "midpoint", None)),
        }

    def _ltf_fvg_context(self, symbol: str, frame: pd.DataFrame | None, data=None) -> FairValueGapContext:
        ltf_min = self._ltf_minutes()
        current_price = _safe_float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        if not bool(self._support_resistance_setting("ltf_fair_value_gaps_enabled", False)):
            return empty_fvg_context(current_price, timeframe_minutes=ltf_min)
        max_per_side = int(self._support_resistance_setting("fair_value_gap_max_per_side", 4) or 4)
        min_gap_atr_mult = float(self._support_resistance_setting("fair_value_gap_min_atr_mult", 0.05) or 0.05)
        min_gap_pct = float(self._support_resistance_setting("fair_value_gap_min_pct", 0.0005) or 0.0005)
        if data is not None and hasattr(data, "get_fair_value_gap_context") and symbol:
            try:
                return data.get_fair_value_gap_context(
                    symbol,
                    timeframe_minutes=ltf_min,
                    current_price=current_price,
                    max_per_side=max_per_side,
                    min_gap_atr_mult=min_gap_atr_mult,
                    min_gap_pct=min_gap_pct,
                )
            except Exception:
                LOG.debug("Failed to load cached fair value gap context for %s; recomputing from frame.", symbol, exc_info=True)
        if frame is None or frame.empty:
            return empty_fvg_context(current_price, timeframe_minutes=ltf_min)
        return build_fair_value_gap_context(
            frame,
            timeframe_minutes=ltf_min,
            current_price=current_price,
            max_per_side=max_per_side,
            min_gap_atr_mult=min_gap_atr_mult,
            min_gap_pct=min_gap_pct,
        )

    def _order_block_tuning_knobs(self) -> dict[str, Any]:
        """Resolve the SHARED OB tuning knobs from support_resistance config.
        Both 1m and HTF OB contexts read the same settings — only the enable
        flag and the input frame's timeframe differ between them.

        The ``min_thrust_atr_mult`` knob (added 2026-05) gates BoS
        displacement to filter micro-breakouts; combined with the
        strength-based sort in build_order_block_context, this stops
        weak close-to-price OBs from displacing strong distant ones.
        """
        return {
            "mode": str(self._support_resistance_setting("order_block_mode", "loose") or "loose").strip().lower() or "loose",
            "max_per_side": int(self._support_resistance_setting("order_block_max_per_side", 4) or 4),
            "min_atr_mult": float(self._support_resistance_setting("order_block_min_atr_mult", 0.05) or 0.05),
            "min_pct": float(self._support_resistance_setting("order_block_min_pct", 0.0005) or 0.0005),
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
        ltf_min = self._ltf_minutes()
        current_price = _safe_float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        knobs = self._order_block_tuning_knobs()
        mode = knobs["mode"]
        if not bool(self._support_resistance_setting("ltf_order_blocks_enabled", False)):
            return empty_order_block_context(current_price, timeframe_minutes=ltf_min, mode=mode)
        if data is not None and hasattr(data, "get_order_block_context") and symbol:
            try:
                return data.get_order_block_context(
                    symbol,
                    timeframe_minutes=ltf_min,
                    current_price=current_price,
                    mode=mode,
                    max_per_side=knobs["max_per_side"],
                    min_block_atr_mult=knobs["min_atr_mult"],
                    min_block_pct=knobs["min_pct"],
                    min_thrust_atr_mult=knobs["min_thrust_atr_mult"],
                    pivot_span=knobs["pivot_span"],
                    new_high_lookback=knobs["new_high_lookback"],
                )
            except Exception:
                LOG.debug("Failed to load cached order block context for %s; recomputing from frame.", symbol, exc_info=True)
        if frame is None or frame.empty:
            return empty_order_block_context(current_price, timeframe_minutes=ltf_min, mode=mode)
        return build_order_block_context(
            frame,
            timeframe_minutes=ltf_min,
            current_price=current_price,
            mode=mode,
            max_per_side=knobs["max_per_side"],
            min_block_atr_mult=knobs["min_atr_mult"],
            min_block_pct=knobs["min_pct"],
            min_thrust_atr_mult=knobs["min_thrust_atr_mult"],
            pivot_span=knobs["pivot_span"],
            new_high_lookback=knobs["new_high_lookback"],
        )

    def _htf_order_block_context(self, symbol: str, frame: pd.DataFrame | None, data=None) -> OrderBlockContext:
        """HTF order block context. Disabled by default — opt in via
        `support_resistance.htf_order_blocks_enabled: true`. Uses the same
        tuning knobs as 1m OBs; the only difference is the input frame is
        resampled to the HTF timeframe (default 15m via
        `support_resistance.timeframe_minutes`).

        Routes through `data.get_order_block_context` when available so the
        HTF resample + OB detection is shared with the dashboard via the
        cycle-scoped cache."""
        current_price = _safe_float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        knobs = self._order_block_tuning_knobs()
        mode = knobs["mode"]
        htf_minutes = self._htf_minutes()
        if not bool(self._support_resistance_setting("htf_order_blocks_enabled", False)):
            return empty_order_block_context(current_price, timeframe_minutes=htf_minutes, mode=mode)
        if data is not None and hasattr(data, "get_order_block_context") and symbol:
            try:
                return data.get_order_block_context(
                    symbol,
                    timeframe_minutes=htf_minutes,
                    current_price=current_price,
                    mode=mode,
                    max_per_side=knobs["max_per_side"],
                    min_block_atr_mult=knobs["min_atr_mult"],
                    min_block_pct=knobs["min_pct"],
                    min_thrust_atr_mult=knobs["min_thrust_atr_mult"],
                    pivot_span=knobs["pivot_span"],
                    new_high_lookback=knobs["new_high_lookback"],
                )
            except Exception:
                LOG.debug("Failed to load cached HTF order block context for %s; recomputing from frame.", symbol, exc_info=True)
        if frame is None or frame.empty:
            return empty_order_block_context(current_price, timeframe_minutes=htf_minutes, mode=mode)
        htf_frame = self._resampled_frame(frame, htf_minutes, symbol=symbol, data=data)
        if htf_frame is None or htf_frame.empty:
            return empty_order_block_context(current_price, timeframe_minutes=htf_minutes, mode=mode)
        return build_order_block_context(
            htf_frame,
            timeframe_minutes=htf_minutes,
            current_price=current_price,
            mode=mode,
            max_per_side=knobs["max_per_side"],
            min_block_atr_mult=knobs["min_atr_mult"],
            min_block_pct=knobs["min_pct"],
            min_thrust_atr_mult=knobs["min_thrust_atr_mult"],
            pivot_span=knobs["pivot_span"],
            new_high_lookback=knobs["new_high_lookback"],
        )

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
        out = resample_bars(frame, f"{tf}min")
        return ensure_standard_indicator_frame(out, span_scale=span_scale, ema_spans=ema_spans)

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
        current_price = _safe_float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        if frame is None or frame.empty or not bool(self._support_resistance_setting("structure_enabled", True)):
            empty_ctx = empty_market_structure_context(current_price)
            with self._structure_context_lock:
                self._structure_context_cache[cache_key] = (frame, empty_ctx)
            return empty_ctx
        pivot_span = int(self._support_resistance_setting("pivot_span", 2) or 2)
        is_ltf_analysis = self._is_ltf_token(timeframe_token)
        analysis_frame = frame
        bar_minutes: int | None = None
        if is_ltf_analysis:
            pivot_span = int(self._support_resistance_setting("structure_ltf_pivot_span", max(2, pivot_span)) or max(2, pivot_span))
            # Fix D (2026-05-27): optionally resample the LTF structure frame
            # to a coarser timeframe so structure pivots track the bars the
            # strategy actually trades on (params.ltf_minutes) instead of the
            # raw 1m stream. 0/1 = use the frame as-is (original behavior).
            ltf_tf_min = int(self._support_resistance_setting("structure_ltf_timeframe_minutes", 0) or 0)
            if ltf_tf_min > 1:
                resampled = self._resampled_frame(frame, ltf_tf_min)
                if resampled is not None and not resampled.empty:
                    analysis_frame = resampled
                    bar_minutes = ltf_tf_min
                    current_price = _safe_float(analysis_frame.iloc[-1]["close"], current_price)
        if bar_minutes is None:
            bar_minutes = frame_bar_minutes(analysis_frame.index)
        # The resample keeps the still-forming last bucket, and so does a
        # peer's native 5m LTF frame (get_merged resamples the live 1m
        # stream). Its first minutes must not confirm a pivot (2026-09-25,
        # see analyze_market_structure). The same clock test as the
        # dashboard's forming bucket and data_feed._completed_bars; a frame of
        # completed 1m bars never reads as forming. A tz-naive index is ET
        # wall time (session_bucket_bounds).
        last_end = session_bucket_ends(analysis_frame.index[-1:], bar_minutes)[0]
        now = pd.Timestamp(now_et())
        if last_end.tzinfo is None:
            now = now.tz_localize(None)
        pct_tolerance = float(self._support_resistance_setting("pct_tolerance", 0.0030) or 0.0030)
        if is_ltf_analysis:
            pct_tolerance *= 0.60
        structure_event_max_age_bars = int(self._support_resistance_setting("structure_event_lookback_bars", 6) or 6)
        ctx = analyze_market_structure(
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
            last_bar_forming=bool(last_end > now),
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
        except Exception:
            last_marker = repr(last_idx)
        return id(frame), len(frame), last_marker

    def _technical_context(self, frame: pd.DataFrame | None) -> TechnicalLevelsContext:
        self._observe_context(frame, ("technical",))
        cache_key = self._technical_context_cache_key(frame)
        with self._technical_context_lock:
            cached = self._technical_context_cache.get(cache_key)
            if cached is not None:
                return cached[1]
        current_price = _safe_float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        cfg = getattr(self.config, "technical_levels", None)
        sr_cfg = getattr(self.config, "support_resistance", None)
        if frame is None or frame.empty or not bool(self._technical_level_setting("enabled", True)):
            empty_ctx = empty_technical_levels_context(current_price)
            with self._technical_context_lock:
                self._technical_context_cache[cache_key] = (frame, empty_ctx)
            return empty_ctx
        pivot_span = int(self._support_resistance_setting("structure_ltf_pivot_span", self._support_resistance_setting("pivot_span", 2)) or 2) if sr_cfg is not None else 2
        ctx = build_technical_levels_context(
            frame,
            current_price=current_price,
            pivot_span=max(1, pivot_span),
            fib_lookback_bars=int(self._technical_level_setting("fib_lookback_bars", 120) or 120),
            fib_min_impulse_atr=float(self._technical_level_setting("fib_min_impulse_atr", 1.25) or 1.25),
            anchored_vwap_impulse_lookback_bars=_optional_int(self._technical_level_setting("anchored_vwap_impulse_lookback_bars", None), None),
            anchored_vwap_min_impulse_atr=_optional_float(self._technical_level_setting("anchored_vwap_min_impulse_atr", None), None),
            anchored_vwap_pivot_span=_optional_int(self._technical_level_setting("anchored_vwap_pivot_span", None), None),
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
        with self._technical_context_lock:
            self._technical_context_cache[cache_key] = (frame, ctx)
        return ctx

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

    @staticmethod
    def _position_r_multiple(position: Position, close: float) -> float | None:
        """Open profit at ``close`` in initial-risk (R) units.

        Anchors to ``metadata['initial_stop_price']`` — stamped once at entry
        by the gatekeeper — rather than ``position.stop_price``, which moves
        with breakeven/trailing management and would make R drift over the
        life of the trade. Returns None when the initial risk is unknown or
        degenerate, which callers treat as "no opinion".

        An option position's entry and stop are PREMIUM while ``close`` is the
        underlying's, so its R is measured on the option's own mark (stamped
        each cycle by the position manager) -- dividing an underlying move by
        a premium risk read a debit position as ~+7R (discretionary exits
        always armed) and a credit spread as ~-5R (never armed).
        """
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        entry = _optional_float(position.entry_price)
        initial_stop = _optional_float(meta.get("initial_stop_price"), position.stop_price)
        if entry is None or initial_stop is None:
            return None
        risk = abs(entry - initial_stop)
        if risk <= 0:
            return None
        price: float | None = close
        if BaseStrategy._is_option_position(position):
            price = _optional_float(meta.get("last_mark_price"))
            if price is None:
                return None
        move = (price - entry) if position.side == Side.LONG else (entry - price)
        return move / risk

    @staticmethod
    def _is_option_position(position: Position) -> bool:
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        return str(meta.get("asset_type") or "").upper() in OPTION_ASSET_TYPES

    @staticmethod
    def _underlying_entry_price(position: Position) -> float | None:
        """Entry in the price space of the frame the exit logic reads.

        An equity's own entry. An option's ``entry_price`` is premium, so its
        underlying's price at entry (``underlying_entry``, stamped by every
        option signal builder) -- None when that was never recorded, which
        callers treat as "no opinion".
        """
        if not BaseStrategy._is_option_position(position):
            return _optional_float(position.entry_price)
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        return _optional_float(meta.get("underlying_entry"))

    @staticmethod
    def _underlying_extremes(position: Position) -> tuple[float | None, float | None]:
        """(high, low) since entry in the underlying's price space.

        An option position's ``highest_price`` / ``lowest_price`` track its
        PREMIUM; the underlying's own range is tracked separately by the
        position manager. Falls back to the entry when nothing has been seen.
        """
        entry = BaseStrategy._underlying_entry_price(position)
        if not BaseStrategy._is_option_position(position):
            return (_optional_float(position.highest_price, entry), _optional_float(position.lowest_price, entry))
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        return (
            _optional_float(meta.get("underlying_high_since_entry"), entry),
            _optional_float(meta.get("underlying_low_since_entry"), entry),
        )

    def _structure_event_recent(self, age_bars: int | None, *, htf: bool = False) -> bool:
        """Is a BOS/CHoCH ``age_bars`` old still fresh? ``age_bars`` is in the
        bars of the structure it came from, so pass ``htf=True`` for the S/R
        context's ``market_structure`` -- its ages are HTF bars and must be
        judged by the HTF window, not the LTF one."""
        # Local import: this module keeps `..config` behind TYPE_CHECKING.
        from ..config import htf_structure_event_lookback
        cfg = getattr(self.config, "support_resistance", None)
        lookback = (
            htf_structure_event_lookback(cfg) if htf
            else int(self._support_resistance_setting("structure_event_lookback_bars", 6) or 6)
        )
        return age_bars is not None and age_bars <= lookback

    def _active_structure_break(self, flag: bool, age_bars: int | None, *, htf: bool = False) -> bool:
        return bool(flag) and self._structure_event_recent(age_bars, htf=htf)

    def _adaptive_management_components(
        self,
        _side: Side,
        close: float,
        stop: float,
        target: float | None,
        *,
        style: str = "trend",
        runner_allowed: bool = False,
        continuation_bias: float = 0.0,
        strong_setup: bool = False,
    ) -> dict[str, Any]:
        management_mode = self.config.risk.trade_management_mode
        if management_mode not in {"adaptive", "adaptive_ladder"}:
            return {"adaptive_management_enabled": False}
        style_token = str(style or "trend").strip().lower()
        trend_like = style_token in {"trend", "breakout", "pairs", "peer", "continuation", "momentum"}
        risk_per_unit = max(0.01, abs(float(close) - float(stop)))
        target_rr = None
        if target is not None:
            try:
                reward = abs(float(target) - float(close))
            except Exception:
                reward = 0.0
            if reward > 0:
                target_rr = reward / risk_per_unit
        # Runner mode (target=None): the trade has no fixed take-profit and
        # relies on trail + structure for exit. Under those conditions a
        # trade that goes immediately against us never reaches the normal
        # 0.9R breakeven threshold, so it has ZERO protection except
        # structure exits (which this refactor gates, see shared_exit.EXIT_FAMILY_GATES).
        # Lower the BE arm to 0.5R in runner mode so a LONG that pokes +0.5R
        # and then reverses gets stopped out flat instead of full-R. 2026-04-17
        # NVDA 11:57 entry never reached +0.5R and got chewed up by EQL
        # exits — this fix doesn't save that specific trade (nothing to arm),
        # but it caps damage on any runner that at least trades favorable
        # briefly before reversing.
        runner_mode = target is None
        breakeven_rr_default = (
            0.50 if runner_mode
            else (0.90 if trend_like else 0.70)
        )
        breakeven_offset_default = 0.05 if trend_like else 0.02
        profit_lock_rr_default = 1.35 if trend_like else 0.95
        profit_lock_stop_default = 0.40 if trend_like else 0.20
        runner_trigger_default = 1.15 if trend_like else 1.00
        breakeven_rr = float(self.params.get("adaptive_breakeven_rr", breakeven_rr_default))
        breakeven_offset_r = float(self.params.get("adaptive_breakeven_offset_r", breakeven_offset_default))
        profit_lock_rr = float(self.params.get("adaptive_profit_lock_rr", profit_lock_rr_default))
        profit_lock_stop_r = float(self.params.get("adaptive_profit_lock_stop_rr", profit_lock_stop_default))
        runner_trigger_rr = float(self.params.get("adaptive_runner_trigger_rr", runner_trigger_default))
        # Partial-breakeven tier — opt-in. When set, arms at a lower RR than
        # the main breakeven so modest-peak trades (0.5–0.9R) get a stop
        # move even if they never reach the 1.0R gate. None disables.
        partial_breakeven_rr_raw = self.params.get("adaptive_partial_breakeven_rr", None)
        partial_breakeven_rr = (
            float(partial_breakeven_rr_raw) if partial_breakeven_rr_raw is not None else None
        )
        partial_breakeven_offset_r = float(self.params.get("adaptive_partial_breakeven_offset_r", 0.0))
        continuation_scale = min(2.0, max(0.0, float(continuation_bias)))
        runner_bonus_rr = max(0.0, float(self.params.get("fvg_runner_rr_bonus", 0.25 if trend_like else 0.12)))
        strong_setup_bonus = 0.20 if strong_setup else 0.0
        current_target_rr = float(target_rr or 0.0)
        runner_target_rr_default = max(current_target_rr, (2.35 if trend_like else current_target_rr))
        runner_target_rr = float(
            self.params.get(
                "adaptive_runner_target_rr",
                max(runner_target_rr_default, current_target_rr + runner_bonus_rr + (continuation_scale * 0.18) + strong_setup_bonus),
            )
            or max(runner_target_rr_default, current_target_rr + runner_bonus_rr + (continuation_scale * 0.18) + strong_setup_bonus)
        )
        base_trail_pct = _optional_float(getattr(self.config.risk, "trailing_stop_pct", None))
        runner_trail_pct = _optional_float(self.params.get("adaptive_runner_trail_pct"))
        if runner_trail_pct is None and base_trail_pct is not None and base_trail_pct > 0:
            runner_trail_pct = max(0.0005, float(base_trail_pct) * (0.85 if trend_like else 0.90))
        return {
            "adaptive_management_enabled": True,
            "adaptive_management_style": style_token,
            "adaptive_breakeven_rr": round(breakeven_rr, 4),
            "adaptive_breakeven_offset_r": round(breakeven_offset_r, 4),
            "adaptive_partial_breakeven_rr": (None if partial_breakeven_rr is None else round(partial_breakeven_rr, 4)),
            "adaptive_partial_breakeven_offset_r": round(partial_breakeven_offset_r, 4),
            "adaptive_profit_lock_rr": round(profit_lock_rr, 4),
            "adaptive_profit_lock_stop_rr": round(profit_lock_stop_r, 4),
            "adaptive_runner_extend_enabled": bool(runner_allowed and (target_rr is None or runner_target_rr > current_target_rr + 0.10)),
            "adaptive_runner_trigger_rr": round(runner_trigger_rr, 4),
            "adaptive_runner_target_rr": (None if target_rr is None and not runner_allowed else round(runner_target_rr, 4)),
            "adaptive_runner_trail_pct": (None if runner_trail_pct is None else round(float(runner_trail_pct), 6)),
        }

    # ------------------------------------------------------------------
    # Adaptive-ladder rung builder (shared by all strategies)
    #
    # Default implementation walks sr_ctx.resistances (long) or
    # sr_ctx.supports (short) and keeps only rungs that clear the configured
    # minimum R:R. Subclasses may override for custom behavior (e.g.
    # peer_confirmed_key_levels uses HTF peer-confirmed levels instead of
    # generic S/R, and top_tier_adaptive suppresses laddering for range
    # regimes where the thesis is mean-reversion inside a bounded zone).
    # ------------------------------------------------------------------
    def _ladder_param(self, name: str, default: float) -> float:
        try:
            return float(self.params.get(name, default) or default)
        except Exception:
            return float(default)

    def _build_ladder_rungs(
        self,
        side: Side,
        close: float,
        stop: float,
        atr: float,
        sr_ctx,
        *,
        regime: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a list of ladder rungs ordered in the direction of travel.

        Each rung dict matches the shape the adaptive_ladder manager
        (PositionManager._adaptive_ladder_management) expects: price, kind,
        zone_width, lower, upper, rr. An empty list disables laddering —
        the signal keeps its originally-computed target and behaves as a
        single-target trade.
        """
        if sr_ctx is None:
            return []
        min_rr = max(0.0, self._ladder_param("ladder_min_target_rr", 1.2))
        zone_mult = max(0.0, self._ladder_param("ladder_zone_atr_mult", 0.5))
        max_rungs = max(1, int(self.params.get("ladder_max_rungs", 4) or 4))
        close_val = float(close)
        stop_val = float(stop)
        atr_val = max(float(atr or 0.0), close_val * 0.0010, 1e-6)
        zone_floor = max(close_val * 0.0010, 1e-6)

        # Side-specific setup: source level list, direction polarity,
        # epsilon to keep a rung clearly past the entry price.
        if side == Side.LONG:
            levels = list(getattr(sr_ctx, "resistances", None) or [])
            risk = max(0.01, close_val - stop_val)
            min_gap = max(close_val * 0.0005, atr_val * 0.05, 1e-6)

            def _reward(level_price: float) -> float:
                return level_price - close_val

            def _keep_level(level_price: float) -> bool:
                return level_price > close_val + min_gap
        else:
            levels = list(getattr(sr_ctx, "supports", None) or [])
            risk = max(0.01, stop_val - close_val)
            min_gap = max(close_val * 0.0005, atr_val * 0.05, 1e-6)

            def _reward(level_price: float) -> float:
                return close_val - level_price

            def _keep_level(level_price: float) -> bool:
                return 0 < level_price < close_val - min_gap

        rungs: list[dict[str, Any]] = []
        seen_prices: list[float] = []
        dedupe_gap = max(atr_val * 0.20, close_val * 0.0010, 1e-6)
        for level in levels:
            if level is None:
                continue
            price = float(getattr(level, "price", 0.0) or 0.0)
            if not _keep_level(price):
                continue
            reward = _reward(price)
            if reward <= 0:
                continue
            rr = reward / risk
            if rr < min_rr:
                continue
            # Skip levels that cluster with one already picked (avoid
            # near-duplicate rungs from the S/R list).
            if any(abs(price - p) < dedupe_gap for p in seen_prices):
                continue
            zone_width = max(atr_val * zone_mult, zone_floor)
            rungs.append({
                "price": round(price, 6),
                "kind": str(getattr(level, "kind", "resistance" if side == Side.LONG else "support")),
                "zone_width": round(zone_width, 6),
                "lower": round(price - zone_width, 6),
                "upper": round(price + zone_width, 6),
                "rr": round(rr, 4),
                "source": str(getattr(level, "source", "sr_ctx")),
            })
            seen_prices.append(price)
            if len(rungs) >= max_rungs:
                break
        # Sort by direction of travel: longs ascending, shorts descending.
        rungs.sort(key=lambda r: float(r["price"]), reverse=(side == Side.SHORT))
        return rungs

    def _ladder_metadata(
        self,
        side: Side,
        rungs: list[dict[str, Any]],
        stop: float,
        close: float,
        atr: float,
    ) -> dict[str, Any]:
        """Produce the metadata dict the ladder manager reads.

        Keys match PositionManager._adaptive_ladder_management exactly —
        changing any of these without updating the manager will silently
        break ladder management. The manager uses ladder_defense_price /
        zone_width as the initial structural defense (usually the entry
        level). Subclasses that know a better defense level (e.g. HTF peer
        level) can add a post-process step after calling this helper.
        """
        zone_mult = max(0.0, self._ladder_param("ladder_zone_atr_mult", 0.5))
        defense_width = max(float(atr or 0.0) * zone_mult, float(close) * 0.0010, 1e-6)
        defense_price = float(stop)
        return {
            "ladder_management_enabled": True,
            "ladder_direction": "long" if side == Side.LONG else "short",
            "ladder_active_index": 0,
            "ladder_rungs": list(rungs),
            "ladder_defense_price": round(defense_price, 6),
            "ladder_defense_zone_width": round(defense_width, 6),
            "ladder_defense_kind": "entry_level",
            "ladder_entry_level_price": round(defense_price, 6),
            "ladder_entry_level_zone_width": round(defense_width, 6),
            "ladder_entry_level_kind": "entry_level",
            "ladder_final_rung_cleared": False,
            "adaptive_ladder_suppress_target_exit": False,
        }

    def _apply_ladder_if_enabled(
        self,
        side: Side,
        close: float,
        stop: float,
        target: float | None,
        *,
        regime: str | None = None,
        sr_ctx=None,
        atr: float | None = None,
    ) -> tuple[float | None, dict[str, Any]]:
        """Optionally replace the signal's target with the first ladder rung.

        Returns (adjusted_target, metadata_to_merge_into_signal). If ladder
        mode isn't active, the strategy opts out, or no qualifying rungs
        are found, the original target is returned with an empty dict so
        the caller can merge it unconditionally.
        """
        if target is None:
            return target, {}
        if not bool(self.__class__.supports_adaptive_ladder):
            return target, {}
        mode = self.config.risk.trade_management_mode
        if mode != "adaptive_ladder":
            return target, {}
        atr_val = float(atr or max(float(close) * 0.0015, 0.01))
        rungs = self._build_ladder_rungs(side, float(close), float(stop), atr_val, sr_ctx, regime=regime)
        if not rungs:
            # No qualifying rungs — either the strategy opted out (e.g.
            # range regime on top_tier) or no S/R levels qualified. Keep
            # the original target; the caller decides whether to drop it
            # into a trail-runner (typically trend/pullback regimes only).
            return target, {}
        first_target = float(rungs[0]["price"])
        ladder_meta = self._ladder_metadata(side, rungs, float(stop), float(close), atr_val)
        return first_target, ladder_meta

    def strategy_exit_signal(self, position: Position, bars: dict[str, pd.DataFrame], tape: ExitTape, data=None) -> ExitDecision | None:
        """This strategy's OWN exits -- the default holds.

        Called by ``shared_exit.SharedExitPolicy`` only after every shared
        exit family held, with the tape it already read for the position
        (the last bar of ``bars[underlying or symbol]``), so a hook reads
        the same references the shared families judged. A full exit returns
        ``ExitDecision(reason, "strategy")``. The shared families
        themselves cannot be overridden (see ``__init_subclass__``).
        """
        return None

    def active_watchlist(self, candidates: list[Candidate], positions: dict[str, Position]) -> set[str]:
        configured = self._watchlist_symbols_from_capabilities("active", candidates, positions)
        if configured is not None:
            return configured
        symbols = {c.symbol for c in candidates}
        for position in positions.values():
            sym = str(position.metadata.get("underlying") or position.symbol)
            symbols.add(sym)
            if position.reference_symbol:
                symbols.add(position.reference_symbol)
        return symbols

    def quote_watchlist(self, candidates: list[Candidate], positions: dict[str, Position], bars: dict[str, pd.DataFrame]) -> set[str]:
        configured = self._watchlist_symbols_from_capabilities(
            "quote",
            candidates,
            positions,
            bars=bars,
            active_symbols=self.active_watchlist(candidates, positions),
        )
        if configured is not None:
            return configured
        # Keep dashboard/watchlist quote pills live for stock strategies by default.
        # Options strategies can now declare leg-specific quote watchlists in manifest.json.
        return self.active_watchlist(candidates, positions)

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        raise NotImplementedError

    def prefetch_entry_market_data(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], data=None) -> None:
        return None

    def should_force_flatten(self, position: Position) -> bool:
        return False

    def position_mark_price(self, position: Position, data) -> float | None:
        return None
