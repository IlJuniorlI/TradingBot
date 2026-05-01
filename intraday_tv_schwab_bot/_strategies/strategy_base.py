# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Mapping
from threading import RLock
from typing import TYPE_CHECKING, ClassVar, Iterable

from .helpers import (
    _bar_close_position,
    _bar_wick_fractions,
    _dashboard_zone_width_from_policy,
    _detail_fields,
    _normalize_symbol_list,
    _normalize_symbol_list_details,
    _optional_float,
    _optional_int,
    _position_strategy_matches,
    _reason_prefix,
    _reason_with_values,
    _safe_float,
)
from ..order_blocks import (
    OrderBlockContext,
    build_order_block_context,
    empty_order_block_context,
)
from .rvol import effective_relative_volume, relative_volume_gate_threshold
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
    # `("structure", "1m")`, `("technical",)`. Populated lazily on first
    # invocation of each builder. The engine reads this set every cycle
    # (after _prime_cycle_support_cache) to drive _prime_cycle_context_cache,
    # which pre-warms the observed contexts in parallel via
    # _parallel_symbol_map. Cycle 1 is lazy (set is empty); cycles 2+ benefit.
    # __init_subclass__ gives each subclass its own set so different strategy
    # classes don't cross-contaminate.
    _observed_contexts: ClassVar[set[tuple]] = set()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
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
        self._manifest = None
        try:
            from .registry import get_plugin
            self._manifest = get_plugin(config.strategy)
        except Exception:
            self._manifest = None
        self._entry_decisions: dict[str, dict[str, Any]] = {}
        self._build_failures: dict[tuple[str, str], dict[str, Any]] = {}
        self._candle_context_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._technical_context_cache: dict[tuple[Any, ...], Any] = {}
        self._structure_context_cache: dict[tuple[Any, ...], Any] = {}
        self._chart_context_cache: dict[tuple[Any, ...], Any] = {}
        # Locks protect the 3 context dicts when the engine pre-warms them
        # in parallel via _parallel_symbol_map. Different worker threads
        # write distinct cache_keys, but the dict mutations themselves still
        # need protection. Compute happens outside the locks, so threads
        # never wait on each other for the heavy work.
        self._chart_context_lock = RLock()
        self._structure_context_lock = RLock()
        self._technical_context_lock = RLock()

    def strategy_logic_default(self, section: str, key: str, default: Any) -> Any:
        return default

    @staticmethod
    def _effective_relative_volume(symbol: str, raw_relative_volume: object, params: dict[str, Any] | None = None, *, cap_default: float = 2.5, standard_floor: float = 0.5) -> float:
        return effective_relative_volume(symbol, raw_relative_volume, params or {}, cap_default=cap_default, standard_floor=standard_floor)

    @staticmethod
    def _relative_volume_gate_threshold(symbol: str, base_threshold: object, params: dict[str, Any] | None = None) -> float:
        return relative_volume_gate_threshold(symbol, base_threshold, params or {})

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

    def signal_priority_key(
        self,
        signal: Signal,
        candidate: Candidate | None,
        *,
        metadata: dict[str, Any],
        strength: float,
        candidate_activity_score: float,
        rank: float,
    ) -> tuple[float, ...] | None:
        _ = signal, candidate
        metadata_fields = self._capability("signal_priority.metadata_fields", None)
        if not isinstance(metadata_fields, list) or not metadata_fields:
            return None
        out: list[float] = []
        for raw_field in metadata_fields:
            field = str(raw_field or "").strip()
            if not field:
                continue
            fallback = None
            if field == "selection_quality_score":
                priority_tiebreak = _optional_float(metadata.get("selection_quality_score"))
                fallback = priority_tiebreak if priority_tiebreak is not None else strength
            out.append(float(_safe_float(metadata.get(field), fallback if fallback is not None else 0.0) or 0.0))
        out.extend((float(strength), float(candidate_activity_score), -float(rank)))
        return tuple(out)

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
        params = self.params if isinstance(self.params, dict) else {}
        spec = {
            "timeframe_minutes": max(1, int(params.get("htf_timeframe_minutes", 60) or 60)),
            "lookback_days": max(1, int(params.get("htf_lookback_days", 60) or 60)),
            "pivot_span": max(1, int(params.get("htf_pivot_span", 2) or 2)),
            "max_levels_per_side": max(1, int(params.get("htf_max_levels_per_side", 6) or 6)),
            "atr_tolerance_mult": float(params.get("htf_atr_tolerance_mult", 0.35) or 0.35),
            "pct_tolerance": float(params.get("htf_pct_tolerance", 0.0030) or 0.0030),
            "stop_buffer_atr_mult": float(params.get("htf_stop_buffer_atr_mult", 0.25) or 0.25),
            "ema_fast_span": max(1, int(params.get("htf_ema_fast_span", 50) or 50)),
            "ema_slow_span": max(1, int(params.get("htf_ema_slow_span", 200) or 200)),
            "refresh_seconds": max(1, int(params.get("htf_refresh_seconds", 180) or 180)),
            "trigger_timeframe_minutes": max(1, int(params.get("trigger_timeframe_minutes", 5) or 5)),
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

    def _strategy_logic_default(self, section: str, key: str, default: Any) -> Any:
        return self.strategy_logic_default(section, key, default)

    def _shared_entry_enabled(self, key: str, default: bool = True) -> bool:
        cfg = getattr(self.config, "shared_entry", None)
        base = getattr(cfg, key, default) if cfg is not None else default
        return bool(self._strategy_logic_default("shared_entry", key, base))

    def _shared_entry_value(self, key: str, default: Any) -> Any:
        cfg = getattr(self.config, "shared_entry", None)
        base = getattr(cfg, key, default) if cfg is not None else default
        return self._strategy_logic_default("shared_entry", key, base)

    def _target_meets_min_rr(self, side: Side, close: float, stop: float, target: float | None) -> bool:
        """Return True if (close, stop, target) clears shared_entry.min_target_rr.

        Used by the SR/technical refinement pipeline as a floor check so
        capping the target never silently destroys R:R below the
        configured threshold. Returns True when there's no target to test
        (None) so callers can use this as a one-line guard:
            if self._target_meets_min_rr(side, close, stop, proposed):
                target = proposed
        """
        if target is None:
            return True
        try:
            close_v = float(close)
            stop_v = float(stop)
            target_v = float(target)
        except (TypeError, ValueError):
            return True
        if side == Side.LONG:
            risk = close_v - stop_v
            reward = target_v - close_v
        else:
            risk = stop_v - close_v
            reward = close_v - target_v
        if risk <= 0 or reward <= 0:
            return False
        try:
            min_rr = float(self._shared_entry_value("min_target_rr", 1.0) or 1.0)
        except (TypeError, ValueError):
            min_rr = 1.0
        return (reward / risk) >= max(0.0, min_rr)

    def _shared_exit_enabled(self, key: str, default: bool = True) -> bool:
        cfg = getattr(self.config, "shared_exit", None)
        base = getattr(cfg, key, default) if cfg is not None else default
        return bool(self._strategy_logic_default("shared_exit", key, base))

    def _shared_exit_value(self, key: str, default: Any) -> Any:
        cfg = getattr(self.config, "shared_exit", None)
        base = getattr(cfg, key, default) if cfg is not None else default
        return self._strategy_logic_default("shared_exit", key, base)

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

    def _shared_exit_tape_confirm(
        self,
        direction: str,
        *,
        close: float,
        ema9: float,
        ema20: float,
        vwap: float,
        close_pos: float,
        close_pos_threshold: float,
    ) -> bool:
        conditions: list[bool] = []
        if direction == "bullish":
            if self._shared_exit_enabled("confirm_with_ema9", True):
                conditions.append(close < ema9)
            if self._shared_exit_enabled("confirm_with_ema20", True):
                conditions.append(close < ema20)
            if self._shared_exit_enabled("confirm_with_vwap", True):
                conditions.append(close < vwap)
            if self._shared_exit_enabled("confirm_with_close_position", True):
                threshold = float(self._shared_exit_value("bullish_close_position_max", close_pos_threshold))
                conditions.append(close_pos <= threshold)
        else:
            if self._shared_exit_enabled("confirm_with_ema9", True):
                conditions.append(close > ema9)
            if self._shared_exit_enabled("confirm_with_ema20", True):
                conditions.append(close > ema20)
            if self._shared_exit_enabled("confirm_with_vwap", True):
                conditions.append(close > vwap)
            if self._shared_exit_enabled("confirm_with_close_position", True):
                threshold = float(self._shared_exit_value("bearish_close_position_min", close_pos_threshold))
                conditions.append(close_pos >= threshold)
        return all(conditions) if conditions else True

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

    def reset_context_caches(self) -> None:
        """Cycle-boundary cache cleanup for the three pre-warmed context caches.

        Public API for the engine. Called inside `_prime_cycle_context_cache`
        before the parallel dispatch populates caches for the new cycle's
        frames. Without this reset the caches would grow unboundedly across
        the session (one entry per (symbol, timeframe) per cycle). Frame-id
        cache keys would never falsely collide, but memory would.
        """
        with self._chart_context_lock:
            self._chart_context_cache = {}
        with self._structure_context_lock:
            self._structure_context_cache = {}
        with self._technical_context_lock:
            self._technical_context_cache = {}

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
                timeframe = entry[1] if len(entry) > 1 else "1m"
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
        cleaned: list[str] = []
        for item in reasons or [reason]:
            token = str(item or '').strip()
            if token and token not in cleaned:
                cleaned.append(token)
        payload: dict[str, Any] = {
            "primary_reason": str(reason),
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
        # Per-cycle cache keyed like _technical_context — id(frame) separates
        # symbols; length+last-bar markers guard against id-reuse after GC.
        # Records the call signature in _observed_contexts so the engine can
        # pre-warm this context in parallel for next cycle's watchlist.
        type(self)._observed_contexts.add(("chart",))
        cache_key = self._technical_context_cache_key(frame)
        with self._chart_context_lock:
            cached = self._chart_context_cache.get(cache_key)
            if cached is not None:
                return cached
        if not bool(self._chart_pattern_setting("enabled", True)):
            ctx = analyze_chart_pattern_context(frame, bullish_allowed=[], bearish_allowed=[], lookback_bars=0)
            with self._chart_context_lock:
                self._chart_context_cache[cache_key] = ctx
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
            self._chart_context_cache[cache_key] = ctx
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

    def _build_bullish_reversal_signal(
        self,
        *,
        candidate: Candidate,
        frame: pd.DataFrame,
        data: Any,
        reason: str,
        matched_patterns: set[str],
        bullish_candle_score: float,
        bullish_candle_net_score: float,
        bullish_candle_anchor_pattern: str | None,
        bullish_candle_anchor_bars: int,
        chart_ctx: Any,
        sr_ctx: Any,
        ms_ctx: Any,
        tech_ctx: Any,
        stop: float,
        target: float,
        extra_priority: float = 0.0,
        management_style: str = "reversal",
    ) -> Signal:
        last_close = _safe_float(frame.iloc[-1]["close"])
        adjustments = self._entry_adjustment_components(Side.LONG, sr_ctx=sr_ctx, tech_ctx=tech_ctx)
        fvg_adjustments = self._fvg_entry_adjustment_components(Side.LONG, candidate.symbol, frame, data)
        management = self._adaptive_management_components(
            Side.LONG,
            last_close,
            stop,
            target,
            style=management_style,
            runner_allowed=False,
            continuation_bias=float(fvg_adjustments.get("fvg_reversal_bias", 0.0) or 0.0),
        )
        candle_priority = 0.60 * float(bullish_candle_net_score)
        final_priority_score = (
            float(candidate.activity_score)
            + float(extra_priority)
            + candle_priority
            + adjustments["entry_context_adjustment"]
            + float(fvg_adjustments.get("fvg_entry_adjustment", 0.0) or 0.0)
        )
        metadata = self._build_signal_metadata(
            entry_price=last_close,
            chart_ctx=chart_ctx, ms_ctx=ms_ctx, sr_ctx=sr_ctx, tech_ctx=tech_ctx,
            adjustments=adjustments, fvg_adjustments=fvg_adjustments,
            management=management,
            final_priority_score=final_priority_score,
            leading={
                "matched_bullish_patterns": sorted(matched_patterns),
                "bullish_candle_score": round(float(bullish_candle_score), 4),
                "bullish_candle_net_score": round(float(bullish_candle_net_score), 4),
                "bullish_candle_anchor_pattern": bullish_candle_anchor_pattern,
                "bullish_candle_anchor_bars": int(bullish_candle_anchor_bars),
            },
        )
        return Signal(
            symbol=candidate.symbol,
            strategy=self.strategy_name,
            side=Side.LONG,
            reason=reason,
            stop_price=stop,
            target_price=target,
            metadata=metadata,
        )

    def _bullish_sr_block_reason(self, sr_ctx) -> str:
        return _reason_with_values(
            "too_close_to_htf_resistance",
            current=sr_ctx.resistance_distance_pct,
            required=float(self._support_resistance_setting("entry_min_clearance_pct", 0.0038)),
            op=">",
            digits=4,
            extras={
                "clearance_atr": (sr_ctx.resistance_distance_atr, ">", float(self._support_resistance_setting("entry_min_clearance_atr", 0.85))),
            },
        )

    def _bearish_sr_block_reason(self, sr_ctx) -> str:
        return _reason_with_values(
            "too_close_to_htf_support",
            current=sr_ctx.support_distance_pct,
            required=float(self._support_resistance_setting("entry_min_clearance_pct", 0.0038)),
            op=">",
            digits=4,
            extras={
                "clearance_atr": (sr_ctx.support_distance_atr, ">", float(self._support_resistance_setting("entry_min_clearance_atr", 0.85))),
            },
        )

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

    @staticmethod
    def _apply_retest_stop_anchor(side: Side, close: float, stop: float, plan: dict[str, Any] | None) -> float:
        if not plan or str(plan.get("status", "none") or "none").strip().lower() != "allow":
            return float(stop)
        anchor = _optional_float(plan.get("stop_anchor"))
        if anchor is None:
            return float(stop)
        if side == Side.LONG:
            candidate = max(float(stop), float(anchor))
            return min(candidate, float(close) * 0.9995)
        candidate = min(float(stop), float(anchor))
        return max(candidate, float(close) * 1.0005)

    def _continuation_fvg_retest_plan(
        self,
        side: Side,
        symbol: str,
        frame: pd.DataFrame | None,
        data=None,
        *,
        trigger_level: float,
        breakout_active: bool,
        close: float,
        vwap: float,
        ema9: float,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": "none",
            "reason": None,
            "stop_anchor": None,
            "metadata": {
                "anti_chase_fvg_retest_enabled": bool(self.params.get("anti_chase_fvg_retest_enabled", True)),
                "anti_chase_fvg_retest_status": "none",
            },
        }
        if frame is None or frame.empty:
            return out
        if not bool(self.params.get("anti_chase_fvg_retest_enabled", True)):
            return out
        if not bool(self._shared_entry_enabled("use_fvg_context", True)):
            return out
        close = float(close or 0.0)
        if close <= 0:
            return out
        fvg_ctx = self._one_minute_fvg_context(symbol, frame, data)
        same_gap = getattr(fvg_ctx, "nearest_bullish_fvg", None) if side == Side.LONG else getattr(fvg_ctx, "nearest_bearish_fvg", None)
        opposing_gap = getattr(fvg_ctx, "nearest_bearish_fvg", None) if side == Side.LONG else getattr(fvg_ctx, "nearest_bullish_fvg", None)
        same_info = self._fvg_gap_state(same_gap, close)
        opposing_info = self._fvg_gap_state(opposing_gap, close)
        same_state = str(same_info.get("state", "none") or "none").strip().lower()
        opposing_state = str(opposing_info.get("state", "none") or "none").strip().lower()
        lower = _optional_float(same_info.get("lower"))
        upper = _optional_float(same_info.get("upper"))
        midpoint = _optional_float(same_info.get("midpoint"))
        size = max(1e-8, float(_optional_float(same_info.get("size"), 0.0) or 0.0))
        same_distance_pct = _optional_float(same_info.get("distance_pct"), 1.0)
        opposing_distance_pct = _optional_float(opposing_info.get("distance_pct"))
        max_gap_distance_pct = max(0.0002, float(self.params.get("anti_chase_fvg_retest_max_gap_distance_pct", 0.0030)))
        max_opposing_distance_pct = max(0.0002, float(self.params.get("anti_chase_fvg_retest_max_opposing_distance_pct", 0.0020)))
        lookback_bars = max(2, int(self.params.get("anti_chase_fvg_retest_lookback_bars", 5)))
        min_close_pos_raw = self.params.get("anti_chase_fvg_retest_min_close_position")
        if min_close_pos_raw is None:
            min_close_pos_raw = self.params.get("min_bar_close_position", 0.60)
        min_close_pos = min(0.95, max(0.05, float(min_close_pos_raw)))
        stop_buffer_gap_frac = max(0.0, float(self.params.get("anti_chase_fvg_retest_stop_buffer_gap_frac", 0.14)))
        trigger_tolerance_pct = max(0.0, float(self.params.get("anti_chase_fvg_retest_trigger_tolerance_pct", 0.0012)))
        touch_tolerance = max(size * 0.20, abs(close) * max_gap_distance_pct * 0.25, 1e-8)
        invalidation_tolerance = max(size * 0.18, abs(close) * 1e-6, 1e-8)
        # Edge-tolerance lets a bar that bounces *just above* a bullish FVG
        # upper bound (or just below a bearish FVG lower bound) without
        # penetrating the zone still qualify as a touch. Some reversals
        # respect the FVG boundary as support without filling the gap;
        # historically the strict touched_zone check missed those. Default
        # 0.0 = preserve existing strict behavior. A value of e.g. 0.003
        # = 0.3% of close treats near-edge reversals as valid retests.
        edge_tolerance = max(0.0, abs(close) * float(self.params.get("anti_chase_fvg_edge_tolerance_pct", 0.0) or 0.0))
        # Trend-MA reclaim gate: by default the confirming bar's close must
        # also be above min(VWAP, EMA9) for longs (or below max for shorts).
        # On microcap squeeze names that gap 50%+ and pull back hard into
        # earlier FVGs, VWAP/EMA9 lag well above the retest zone, so the
        # gate blocks exactly the deep-retest entries the strategy wants.
        # Setting this to True drops the trend-MA reclaim and keeps only the
        # FVG-midpoint reclaim + bar_confirm shape check. Default False
        # preserves prior behavior for every other strategy.
        skip_trend_reclaim = bool(self.params.get("anti_chase_fvg_retest_skip_vwap_ema9_reclaim", False))
        direction_label = "bullish" if side == Side.LONG else "bearish"
        out["metadata"].update(
            {
                "anti_chase_fvg_retest_side": direction_label,
                "anti_chase_fvg_retest_same_state": same_state,
                "anti_chase_fvg_retest_opposing_state": opposing_state,
                "anti_chase_fvg_retest_same_midpoint": midpoint,
                "anti_chase_fvg_retest_same_lower": lower,
                "anti_chase_fvg_retest_same_upper": upper,
                "anti_chase_fvg_retest_same_distance_pct": same_distance_pct,
                "anti_chase_fvg_retest_opposing_distance_pct": opposing_distance_pct,
                "anti_chase_fvg_retest_trigger_level": float(trigger_level or 0.0),
            }
        )
        if lower is None or upper is None or midpoint is None or same_state not in {"active", "validated"}:
            if same_state == "invalidated":
                out["status"] = "reject"
                out["reason"] = f"{direction_label}_fvg_retest_rejected({_detail_fields(detail='same_direction_gap_invalidated', midpoint=midpoint or 0.0)})"
                out["metadata"]["anti_chase_fvg_retest_status"] = out["status"]
            return out
        in_gap = lower - touch_tolerance <= close <= upper + touch_tolerance
        if not in_gap and (same_distance_pct is None or float(same_distance_pct) > max_gap_distance_pct):
            return out
        recent = frame.tail(lookback_bars + 1)
        prior = recent.iloc[:-1]
        if side == Side.LONG:
            impulse_seen = bool(breakout_active)
            if not impulse_seen and trigger_level > 0 and not prior.empty:
                impulse_seen = float(prior["close"].max()) >= (float(trigger_level) * (1.0 - trigger_tolerance_pct))
        else:
            impulse_seen = bool(breakout_active)
            if not impulse_seen and trigger_level > 0 and not prior.empty:
                impulse_seen = float(prior["close"].min()) <= (float(trigger_level) * (1.0 + trigger_tolerance_pct))
        if not impulse_seen:
            return out
        opposing_blocked = bool(opposing_state in {"active", "validated"} and opposing_distance_pct is not None and float(opposing_distance_pct) <= max_opposing_distance_pct)
        if opposing_blocked:
            out["status"] = "reject"
            out["reason"] = f"{direction_label}_fvg_retest_rejected({_detail_fields(detail='opposing_gap_too_close', opposing_distance_pct=opposing_distance_pct or 0.0)})"
            out["metadata"]["anti_chase_fvg_retest_status"] = out["status"]
            return out
        last = frame.iloc[-1]
        bar_low = _safe_float(last.get("low"), close)
        bar_high = _safe_float(last.get("high"), close)
        close_pos = _bar_close_position(frame)
        touched_zone = bar_low <= (upper + touch_tolerance + edge_tolerance) and bar_high >= (lower - touch_tolerance - edge_tolerance)
        if side == Side.LONG:
            respected_zone = bar_low >= (lower - invalidation_tolerance)
            reclaimed = close >= (midpoint - touch_tolerance)
            if not skip_trend_reclaim:
                reclaimed = reclaimed and close >= min(float(vwap or close), float(ema9 or close))
            bar_confirm = close_pos >= min_close_pos
            stop_anchor = max(0.01, lower - (size * stop_buffer_gap_frac))
        else:
            respected_zone = bar_high <= (upper + invalidation_tolerance)
            reclaimed = close <= (midpoint + touch_tolerance)
            if not skip_trend_reclaim:
                reclaimed = reclaimed and close <= max(float(vwap or close), float(ema9 or close))
            bar_confirm = close_pos <= (1.0 - min_close_pos)
            stop_anchor = upper + (size * stop_buffer_gap_frac)
        out["metadata"]["anti_chase_fvg_retest_recent_impulse"] = bool(impulse_seen)
        out["metadata"]["anti_chase_fvg_retest_touched_zone"] = bool(touched_zone)
        out["metadata"]["anti_chase_fvg_retest_respected_zone"] = bool(respected_zone)
        out["metadata"]["anti_chase_fvg_retest_bar_confirm"] = bool(bar_confirm)
        if touched_zone and respected_zone and reclaimed and bar_confirm:
            out["status"] = "allow"
            out["stop_anchor"] = float(stop_anchor)
            out["metadata"].update(
                {
                    "anti_chase_fvg_retest_status": "allow",
                    "anti_chase_fvg_retest_confirmed": True,
                    "anti_chase_fvg_retest_stop_anchor": float(stop_anchor),
                }
            )
            return out
        out["status"] = "wait"
        out["reason"] = f"wait_for_{direction_label}_fvg_retest({_detail_fields(state=same_state, midpoint=midpoint, trigger=trigger_level, distance_pct=same_distance_pct or 0.0)})"
        out["metadata"]["anti_chase_fvg_retest_status"] = out["status"]
        return out

    def _continuation_ob_retest_plan(
        self,
        side: Side,
        symbol: str,
        frame: pd.DataFrame | None,
        data=None,
        *,
        trigger_level: float,
        breakout_active: bool,
        close: float,
        vwap: float,
        ema9: float,
    ) -> dict[str, Any]:
        """Order-block retest plan, parallel to `_continuation_fvg_retest_plan`.

        Returns the same {status, reason, metadata, stop_anchor} dict shape so
        it composes with `_apply_continuation_zone_retest_plans`. Reuses the
        same `anti_chase_fvg_retest_*` knobs for confirmation thresholds — the
        user-stated convention is "same rules for confirm" between FVG and OB.
        Disabled by default; opt in via `support_resistance.one_minute_order_blocks_enabled`.
        """
        out: dict[str, Any] = {"status": "none", "reason": None, "metadata": {}, "stop_anchor": None}
        if frame is None or frame.empty:
            return out
        if not bool(self._support_resistance_setting("one_minute_order_blocks_enabled", False)):
            return out
        close = float(close or 0.0)
        if close <= 0:
            return out
        ob_ctx = self._one_minute_order_block_context(frame)
        same_ob = getattr(ob_ctx, "nearest_bullish_ob", None) if side == Side.LONG else getattr(ob_ctx, "nearest_bearish_ob", None)
        opposing_ob = getattr(ob_ctx, "nearest_bearish_ob", None) if side == Side.LONG else getattr(ob_ctx, "nearest_bullish_ob", None)
        same_info = self._fvg_gap_state(same_ob, close)
        opposing_info = self._fvg_gap_state(opposing_ob, close)
        same_state = str(same_info.get("state", "none") or "none").strip().lower()
        opposing_state = str(opposing_info.get("state", "none") or "none").strip().lower()
        lower = _optional_float(same_info.get("lower"))
        upper = _optional_float(same_info.get("upper"))
        midpoint = _optional_float(same_info.get("midpoint"))
        size = max(1e-8, float(_optional_float(same_info.get("size"), 0.0) or 0.0))
        same_distance_pct = _optional_float(same_info.get("distance_pct"), 1.0)
        opposing_distance_pct = _optional_float(opposing_info.get("distance_pct"))
        max_gap_distance_pct = max(0.0002, float(self.params.get("anti_chase_fvg_retest_max_gap_distance_pct", 0.0030)))
        max_opposing_distance_pct = max(0.0002, float(self.params.get("anti_chase_fvg_retest_max_opposing_distance_pct", 0.0020)))
        lookback_bars = max(2, int(self.params.get("anti_chase_fvg_retest_lookback_bars", 5)))
        min_close_pos_raw = self.params.get("anti_chase_fvg_retest_min_close_position")
        if min_close_pos_raw is None:
            min_close_pos_raw = self.params.get("min_bar_close_position", 0.60)
        min_close_pos = min(0.95, max(0.05, float(min_close_pos_raw)))
        stop_buffer_gap_frac = max(0.0, float(self.params.get("anti_chase_fvg_retest_stop_buffer_gap_frac", 0.14)))
        trigger_tolerance_pct = max(0.0, float(self.params.get("anti_chase_fvg_retest_trigger_tolerance_pct", 0.0012)))
        touch_tolerance = max(size * 0.20, abs(close) * max_gap_distance_pct * 0.25, 1e-8)
        invalidation_tolerance = max(size * 0.18, abs(close) * 1e-6, 1e-8)
        edge_tolerance = max(0.0, abs(close) * float(self.params.get("anti_chase_fvg_edge_tolerance_pct", 0.0) or 0.0))
        skip_trend_reclaim = bool(self.params.get("anti_chase_fvg_retest_skip_vwap_ema9_reclaim", False))
        direction_label = "bullish" if side == Side.LONG else "bearish"
        out["metadata"].update(
            {
                "anti_chase_ob_retest_side": direction_label,
                "anti_chase_ob_retest_same_state": same_state,
                "anti_chase_ob_retest_opposing_state": opposing_state,
                "anti_chase_ob_retest_same_midpoint": midpoint,
                "anti_chase_ob_retest_same_lower": lower,
                "anti_chase_ob_retest_same_upper": upper,
                "anti_chase_ob_retest_same_distance_pct": same_distance_pct,
                "anti_chase_ob_retest_opposing_distance_pct": opposing_distance_pct,
                "anti_chase_ob_retest_trigger_level": float(trigger_level or 0.0),
                "anti_chase_ob_retest_mode": str(getattr(ob_ctx, "mode", "loose") or "loose"),
            }
        )
        if lower is None or upper is None or midpoint is None or same_state not in {"active", "validated"}:
            if same_state == "invalidated":
                out["status"] = "reject"
                out["reason"] = f"{direction_label}_ob_retest_rejected({_detail_fields(detail='same_direction_block_invalidated', midpoint=midpoint or 0.0)})"
                out["metadata"]["anti_chase_ob_retest_status"] = out["status"]
            return out
        in_zone = lower - touch_tolerance <= close <= upper + touch_tolerance
        if not in_zone and (same_distance_pct is None or float(same_distance_pct) > max_gap_distance_pct):
            return out
        recent = frame.tail(lookback_bars + 1)
        prior = recent.iloc[:-1]
        if side == Side.LONG:
            impulse_seen = bool(breakout_active)
            if not impulse_seen and trigger_level > 0 and not prior.empty:
                impulse_seen = float(prior["close"].max()) >= (float(trigger_level) * (1.0 - trigger_tolerance_pct))
        else:
            impulse_seen = bool(breakout_active)
            if not impulse_seen and trigger_level > 0 and not prior.empty:
                impulse_seen = float(prior["close"].min()) <= (float(trigger_level) * (1.0 + trigger_tolerance_pct))
        if not impulse_seen:
            return out
        opposing_blocked = bool(opposing_state in {"active", "validated"} and opposing_distance_pct is not None and float(opposing_distance_pct) <= max_opposing_distance_pct)
        if opposing_blocked:
            out["status"] = "reject"
            out["reason"] = f"{direction_label}_ob_retest_rejected({_detail_fields(detail='opposing_block_too_close', opposing_distance_pct=opposing_distance_pct or 0.0)})"
            out["metadata"]["anti_chase_ob_retest_status"] = out["status"]
            return out
        last = frame.iloc[-1]
        bar_low = _safe_float(last.get("low"), close)
        bar_high = _safe_float(last.get("high"), close)
        close_pos = _bar_close_position(frame)
        touched_zone = bar_low <= (upper + touch_tolerance + edge_tolerance) and bar_high >= (lower - touch_tolerance - edge_tolerance)
        if side == Side.LONG:
            respected_zone = bar_low >= (lower - invalidation_tolerance)
            reclaimed = close >= (midpoint - touch_tolerance)
            if not skip_trend_reclaim:
                reclaimed = reclaimed and close >= min(float(vwap or close), float(ema9 or close))
            bar_confirm = close_pos >= min_close_pos
            stop_anchor = max(0.01, lower - (size * stop_buffer_gap_frac))
        else:
            respected_zone = bar_high <= (upper + invalidation_tolerance)
            reclaimed = close <= (midpoint + touch_tolerance)
            if not skip_trend_reclaim:
                reclaimed = reclaimed and close <= max(float(vwap or close), float(ema9 or close))
            bar_confirm = close_pos <= (1.0 - min_close_pos)
            stop_anchor = upper + (size * stop_buffer_gap_frac)
        out["metadata"]["anti_chase_ob_retest_recent_impulse"] = bool(impulse_seen)
        out["metadata"]["anti_chase_ob_retest_touched_zone"] = bool(touched_zone)
        out["metadata"]["anti_chase_ob_retest_respected_zone"] = bool(respected_zone)
        out["metadata"]["anti_chase_ob_retest_bar_confirm"] = bool(bar_confirm)
        if touched_zone and respected_zone and reclaimed and bar_confirm:
            out["status"] = "allow"
            out["stop_anchor"] = float(stop_anchor)
            out["metadata"].update(
                {
                    "anti_chase_ob_retest_status": "allow",
                    "anti_chase_ob_retest_confirmed": True,
                    "anti_chase_ob_retest_stop_anchor": float(stop_anchor),
                }
            )
            return out
        out["status"] = "wait"
        out["reason"] = f"wait_for_{direction_label}_ob_retest({_detail_fields(state=same_state, midpoint=midpoint, trigger=trigger_level, distance_pct=same_distance_pct or 0.0)})"
        out["metadata"]["anti_chase_ob_retest_status"] = out["status"]
        return out

    @staticmethod
    def _apply_continuation_zone_retest_plans(
        reasons: list[str],
        plans: list[dict[str, Any] | None],
        *,
        deferrable_prefixes: set[str],
    ) -> list[str]:
        """Combine multiple retest plans (e.g. FVG + OB) with OR logic.

        - If ANY plan returns ``status="allow"`` and all current reasons are
          deferrable, clear the reasons (entry can fire).
        - Otherwise prefer "wait" reasons over "reject" reasons (waiting
          could still resolve in a later bar).
        - Plans with ``status="none"`` (no zone available) are ignored.
        - When called with a single-plan list, behavior matches the prior
          ``_apply_continuation_fvg_retest_plan`` (now removed) exactly:
          allow on the only plan clears deferrable reasons; wait/reject
          replaces with the plan reason; none returns reasons unchanged.
        """
        if not reasons or not plans:
            return reasons
        engaged = [
            (p, str(p.get("status", "none") or "none").strip().lower())
            for p in plans
            if p
        ]
        engaged = [(p, s) for p, s in engaged if s != "none"]
        if not engaged:
            return reasons
        deferred = [reason for reason in reasons if _reason_prefix(reason) in deferrable_prefixes]
        other = [reason for reason in reasons if _reason_prefix(reason) not in deferrable_prefixes]
        if not deferred or other:
            return reasons
        if any(s == "allow" for _p, s in engaged):
            return []
        wait_plans = [p for p, s in engaged if s == "wait" and (str(p.get("reason") or "").strip())]
        if wait_plans:
            return [str(wait_plans[0]["reason"]).strip()]
        reject_plans = [p for p, s in engaged if s == "reject" and (str(p.get("reason") or "").strip())]
        if reject_plans:
            return [str(reject_plans[0]["reason"]).strip()]
        return reasons

    @staticmethod
    def _blocks_bullish_entry(ctx) -> bool:
        return bool(
            ctx.matched_bearish_reversal
            or ctx.bias_score <= -0.75
            or (len(ctx.matched_bearish_continuation) >= 2 and not ctx.matched_bullish_continuation)
        )

    @staticmethod
    def _blocks_bearish_entry(ctx) -> bool:
        return bool(
            ctx.matched_bullish_reversal
            or ctx.bias_score >= 0.75
            or (len(ctx.matched_bullish_continuation) >= 2 and not ctx.matched_bearish_continuation)
        )

    def _sr_timeframe_minutes(self) -> int:
        fallback = int(self._support_resistance_setting("timeframe_minutes", 15))
        return int(self.params.get("htf_timeframe_minutes", fallback))

    def _sr_lookback_days(self) -> int:
        fallback = int(self._support_resistance_setting("lookback_days", 10))
        return int(self.params.get("htf_lookback_days", fallback))

    def _sr_refresh_seconds(self) -> int:
        fallback = int(self._support_resistance_setting("refresh_seconds", 600))
        return int(self.params.get("htf_refresh_seconds", fallback))

    def _sr_context(self, symbol: str, frame: pd.DataFrame | None, data):
        current_price = float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        timeframe_minutes = self._sr_timeframe_minutes()
        if not bool(self._support_resistance_setting("enabled", True)) or data is None:
            return empty_support_resistance_context(current_price, timeframe_minutes=timeframe_minutes)
        ctx = data.get_support_resistance(
            symbol,
            current_price=current_price,
            flip_frame=frame,
            mode="trading",
            timeframe_minutes=timeframe_minutes,
            lookback_days=self._sr_lookback_days(),
            refresh_seconds=self._sr_refresh_seconds(),
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
            "sr_near_support": bool(ctx.near_support),
            "sr_near_resistance": bool(ctx.near_resistance),
            "sr_bias_score": float(ctx.bias_score),
            "sr_regime_hint": str(ctx.regime_hint),
            "sr_level_buffer": float(ctx.level_buffer or 0.0),
            **BaseStrategy._structure_lists(ms_ctx, prefix="mshtf"),
        }

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
        refresh_seconds: int | None = None,
        current_price: float | None = None,
        use_prior_day_high_low: bool = True,
        use_prior_week_high_low: bool = True,
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
            refresh_seconds=refresh_seconds,
            use_prior_day_high_low=bool(use_prior_day_high_low),
            use_prior_week_high_low=bool(use_prior_week_high_low),
            include_fair_value_gaps=bool(self._support_resistance_setting("htf_fair_value_gaps_enabled", True)),
            fair_value_gap_max_per_side=int(self._support_resistance_setting("fair_value_gap_max_per_side", 4) or 4),
            fair_value_gap_min_atr_mult=float(self._support_resistance_setting("fair_value_gap_min_atr_mult", 0.05) or 0.05),
            fair_value_gap_min_pct=float(self._support_resistance_setting("fair_value_gap_min_pct", 0.0005) or 0.0005),
        )
        if ctx is None:
            return empty_htf_context(current_price or 0.0, timeframe_minutes=timeframe_minutes)
        return ctx

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
            "htf_timeframe_minutes": int(getattr(ctx, "timeframe_minutes", 0) or 0),
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

    def _one_minute_fvg_context(self, symbol: str, frame: pd.DataFrame | None, data=None) -> FairValueGapContext:
        current_price = _safe_float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        if not bool(self._support_resistance_setting("one_minute_fair_value_gaps_enabled", False)):
            return empty_fvg_context(current_price, timeframe_minutes=1)
        max_per_side = int(self._support_resistance_setting("fair_value_gap_max_per_side", 4) or 4)
        min_gap_atr_mult = float(self._support_resistance_setting("fair_value_gap_min_atr_mult", 0.05) or 0.05)
        min_gap_pct = float(self._support_resistance_setting("fair_value_gap_min_pct", 0.0005) or 0.0005)
        if data is not None and hasattr(data, "get_fair_value_gap_context") and symbol:
            try:
                return data.get_fair_value_gap_context(
                    symbol,
                    timeframe_minutes=1,
                    current_price=current_price,
                    max_per_side=max_per_side,
                    min_gap_atr_mult=min_gap_atr_mult,
                    min_gap_pct=min_gap_pct,
                )
            except Exception:
                LOG.debug("Failed to load cached fair value gap context for %s; recomputing from frame.", symbol, exc_info=True)
        if frame is None or frame.empty:
            return empty_fvg_context(current_price, timeframe_minutes=1)
        return build_fair_value_gap_context(
            frame,
            timeframe_minutes=1,
            current_price=current_price,
            max_per_side=max_per_side,
            min_gap_atr_mult=min_gap_atr_mult,
            min_gap_pct=min_gap_pct,
        )

    def _order_block_tuning_knobs(self) -> dict[str, Any]:
        """Resolve the six SHARED OB tuning knobs from support_resistance config.
        Both 1m and HTF OB contexts read the same settings — only the enable
        flag and the input frame's timeframe differ between them."""
        return {
            "mode": str(self._support_resistance_setting("order_block_mode", "loose") or "loose").strip().lower() or "loose",
            "max_per_side": int(self._support_resistance_setting("order_block_max_per_side", 4) or 4),
            "min_atr_mult": float(self._support_resistance_setting("order_block_min_atr_mult", 0.05) or 0.05),
            "min_pct": float(self._support_resistance_setting("order_block_min_pct", 0.0005) or 0.0005),
            "pivot_span": int(self._support_resistance_setting("order_block_pivot_span", 2) or 2),
            "new_high_lookback": int(self._support_resistance_setting("order_block_new_high_lookback", 8) or 8),
        }

    def _one_minute_order_block_context(self, frame: pd.DataFrame | None) -> OrderBlockContext:
        """1m order block context. No symbol/data params — unlike FVG and HTF
        OB, the 1m OB context is computed inline from the per-symbol frame
        already in scope and doesn't consult any data-store cache. Add params
        back if/when MarketDataStore gains a `get_order_block_context` cache."""
        current_price = _safe_float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        knobs = self._order_block_tuning_knobs()
        mode = knobs["mode"]
        if not bool(self._support_resistance_setting("one_minute_order_blocks_enabled", False)):
            return empty_order_block_context(current_price, timeframe_minutes=1, mode=mode)
        if frame is None or frame.empty:
            return empty_order_block_context(current_price, timeframe_minutes=1, mode=mode)
        return build_order_block_context(
            frame,
            timeframe_minutes=1,
            current_price=current_price,
            mode=mode,
            max_per_side=knobs["max_per_side"],
            min_block_atr_mult=knobs["min_atr_mult"],
            min_block_pct=knobs["min_pct"],
            pivot_span=knobs["pivot_span"],
            new_high_lookback=knobs["new_high_lookback"],
        )

    def _htf_order_block_context(self, symbol: str, frame: pd.DataFrame | None, data=None) -> OrderBlockContext:
        """HTF order block context. Disabled by default — opt in via
        `support_resistance.htf_order_blocks_enabled: true`. Uses the same
        tuning knobs as 1m OBs; the only difference is the input frame is
        resampled to the HTF timeframe (default 15m via
        `support_resistance.timeframe_minutes`)."""
        current_price = _safe_float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        knobs = self._order_block_tuning_knobs()
        mode = knobs["mode"]
        htf_minutes = self._sr_timeframe_minutes()
        if not bool(self._support_resistance_setting("htf_order_blocks_enabled", False)):
            return empty_order_block_context(current_price, timeframe_minutes=htf_minutes, mode=mode)
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
    ) -> pd.DataFrame | None:
        if frame is None or frame.empty:
            return None
        tf = max(1, int(timeframe_minutes))
        if data is not None and symbol and hasattr(data, "get_merged"):
            try:
                cached = data.get_merged(str(symbol), timeframe=f"{tf}min", with_indicators=True)
                if cached is not None and not cached.empty:
                    return cached
            except Exception:
                LOG.debug("Failed to load cached %s-minute merged frame for %s; resampling from base frame.", tf, symbol, exc_info=True)
        if tf <= 1:
            return ensure_standard_indicator_frame(frame.copy())
        out = resample_bars(frame, f"{tf}min")
        return ensure_standard_indicator_frame(out)

    def _structure_context(self, frame: pd.DataFrame | None, timeframe: str = "1m"):
        # Per-cycle cache. Timeframe goes in the key because the pivot_span /
        # pct_tolerance branches below differ by timeframe token.
        # Records (name, timeframe) so the engine pre-warms the right
        # variant — peer_confirmed strategies use "ltf" while most others
        # use "1m"; both can co-exist in a single observed set.
        timeframe_token = str(timeframe).lower()
        type(self)._observed_contexts.add(("structure", timeframe_token))
        frame_key = self._technical_context_cache_key(frame)
        cache_key = (frame_key, timeframe_token)
        with self._structure_context_lock:
            cached = self._structure_context_cache.get(cache_key)
            if cached is not None:
                return cached
        current_price = _safe_float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        if frame is None or frame.empty or not bool(self._support_resistance_setting("structure_enabled", True)):
            empty_ctx = empty_market_structure_context(current_price)
            with self._structure_context_lock:
                self._structure_context_cache[cache_key] = empty_ctx
            return empty_ctx
        pivot_span = int(self._support_resistance_setting("pivot_span", 2) or 2)
        if timeframe_token in {"1m", "1min", "minute", "execution"}:
            pivot_span = int(self._support_resistance_setting("structure_1m_pivot_span", max(2, pivot_span)) or max(2, pivot_span))
        pct_tolerance = float(self._support_resistance_setting("pct_tolerance", 0.0030) or 0.0030)
        if timeframe_token in {"1m", "1min", "minute", "execution"}:
            pct_tolerance *= 0.60
        structure_event_max_age_bars = int(self._support_resistance_setting("structure_event_lookback_bars", 6) or 6)
        ctx = analyze_market_structure(
            frame,
            current_price=current_price,
            pivot_span=pivot_span,
            eq_atr_mult=float(self._support_resistance_setting("structure_eq_atr_mult", 0.25) or 0.25),
            pct_tolerance=pct_tolerance,
            breakout_atr_mult=float(self._support_resistance_setting("breakout_atr_mult", 0.35) or 0.35),
            breakout_buffer_pct=float(self._support_resistance_setting("breakout_buffer_pct", 0.0015) or 0.0015),
            structure_event_max_age_bars=structure_event_max_age_bars,
        )
        with self._structure_context_lock:
            self._structure_context_cache[cache_key] = ctx
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
            f"{prefix}_reason": str(getattr(ctx, "reason", "unknown") or "unknown"),
        }

    @staticmethod
    def _technical_context_cache_key(frame: pd.DataFrame | None) -> tuple[Any, ...]:
        """Per-cycle cache key. `id(frame)` is the primary discriminator —
        each symbol has its own DataFrame in `bars[...]`. `len` + last-bar
        timestamp guard against id-reuse if a frame is GC'd and a new one
        gets the same id (not possible mid-cycle, but cheap insurance)."""
        if frame is None or frame.empty:
            return ("empty",)
        last_idx = frame.index[-1]
        try:
            last_marker = last_idx.isoformat()  # type: ignore[attr-defined]
        except Exception:
            last_marker = repr(last_idx)
        return id(frame), len(frame), last_marker

    def _technical_context(self, frame: pd.DataFrame | None) -> TechnicalLevelsContext:
        type(self)._observed_contexts.add(("technical",))
        cache_key = self._technical_context_cache_key(frame)
        with self._technical_context_lock:
            cached = self._technical_context_cache.get(cache_key)
            if cached is not None:
                return cached
        current_price = _safe_float(frame.iloc[-1]["close"]) if frame is not None and not frame.empty else 0.0
        cfg = getattr(self.config, "technical_levels", None)
        sr_cfg = getattr(self.config, "support_resistance", None)
        if frame is None or frame.empty or not bool(self._technical_level_setting("enabled", True)):
            empty_ctx = empty_technical_levels_context(current_price)
            with self._technical_context_lock:
                self._technical_context_cache[cache_key] = empty_ctx
            return empty_ctx
        pivot_span = int(self._support_resistance_setting("structure_1m_pivot_span", self._support_resistance_setting("pivot_span", 2)) or 2) if sr_cfg is not None else 2
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
            trendline_breakout_buffer_atr_mult=float(self._technical_level_setting("trendline_breakout_buffer_atr_mult", 0.15) or 0.15),
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
            divergence_rsi_min_delta=float(self._technical_level_setting("divergence_rsi_min_delta", 2.0) or 2.0),
            divergence_obv_min_volume_frac=float(getattr(cfg, "divergence_obv_min_volume_frac", 0.50) or 0.50),
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
            self._technical_context_cache[cache_key] = ctx
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
            f"{prefix}_bullish_rsi_divergence": bool(getattr(ctx, "bullish_rsi_divergence", False)) if divergence_enabled else False,
            f"{prefix}_bearish_rsi_divergence": bool(getattr(ctx, "bearish_rsi_divergence", False)) if divergence_enabled else False,
            f"{prefix}_bullish_obv_divergence": bool(getattr(ctx, "bullish_obv_divergence", False)) if divergence_enabled else False,
            f"{prefix}_bearish_obv_divergence": bool(getattr(ctx, "bearish_obv_divergence", False)) if divergence_enabled else False,
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

    def _build_signal_metadata(
        self,
        *,
        # Intended-entry price keys. Any that are provided are stamped
        # onto signal.metadata with the canonical key names that
        # ``risk.py::_signal_entry_price`` reads. Equity strategies
        # typically pass ``entry_price=last_close`` (market-on-close);
        # limit-order strategies pass ``limit_price``; option strategies
        # stamp all three via their own builder because the option
        # contract's mid/limit/mark matter separately.
        #
        # Without at least one of these set, the same-level retry block
        # and the fib-pullback override in risk.py short-circuit to "ok"
        # because ``_signal_entry_price`` returns None — the gates exist
        # but never fire.
        entry_price: float | None = None,
        limit_price: float | None = None,
        mark_price_hint: float | None = None,
        # Component contexts. Pass None to skip that block entirely (e.g.
        # pairs_residual passes chart_ctx=None because it doesn't use
        # chart patterns).
        chart_ctx: Any = None,
        ms_ctx: Any = None,
        sr_ctx: Any = None,
        tech_ctx: Any = None,
        # Sub-dict blocks. None or empty is skipped. `retest_plan` is read
        # as ``retest_plan.get("metadata", {})`` to match the existing
        # call-site idiom.
        adjustments: dict[str, Any] | None = None,
        fvg_adjustments: dict[str, Any] | None = None,
        management: dict[str, Any] | None = None,
        retest_plan: dict[str, Any] | None = None,
        ladder_meta: dict[str, Any] | None = None,
        # Score convenience. ``final_priority_score=None`` skips the stamp
        # (use case: rth_trend_pullback stamps the score later in a
        # follow-up metadata.update). ``score_key`` and ``score_round``
        # are overridable for strategies that name their score differently
        # or want more/less precision.
        final_priority_score: float | None = None,
        score_key: str = "final_priority_score",
        score_round: int = 4,
        # Prefixes passed to the component-list helpers. ``ms_prefix``
        # defaults to "ms1m" (the de-facto call-site convention) rather
        # than the "ms" default on ``_structure_lists`` itself.
        ms_prefix: str = "ms1m",
        tech_prefix: str = "tech",
        # Caller-supplied leading keys (stamped FIRST — lowest
        # precedence, so shared-block keys overwrite collisions). Use for
        # strategy-specific identifying keys: e.g. ORB uses
        # ``{"or_high": ..., "or_low": ...}``, pairs_residual uses
        # ``{"benchmark": ..., "zscore": ...}``.
        leading: dict[str, Any] | None = None,
        # Caller-supplied trailing extras (stamped LAST — highest
        # precedence, used to override any shared-block key). This is the
        # escape hatch: if a strategy needs to monkey-patch a key that
        # one of the component lists would set, drop it in ``extras``.
        extras: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the signal.metadata dict shared across strategies.

        Merge order (later keys overwrite earlier keys on collision):

        1. ``entry_price``/``limit_price``/``mark_price_hint``   (if provided)
        2. ``leading``                               (caller-specific prepend)
        3. ``score_key: round(final_priority_score, score_round)``
        4. ``adjustments``
        5. ``fvg_adjustments``
        6. ``management``
        7. ``retest_plan.get("metadata", {})``
        8. ``ladder_meta``
        9. ``_chart_lists(chart_ctx)``               (if ``chart_ctx`` not None)
        10. ``_structure_lists(ms_ctx, prefix=ms_prefix)`` (if ``ms_ctx`` not None)
        11. ``_sr_lists(sr_ctx)``                    (if ``sr_ctx`` not None)
        12. ``_technical_lists(tech_ctx, prefix=tech_prefix)`` (if ``tech_ctx`` not None)
        13. ``extras``                               (caller-specific override slot)

        Every default argument is keyword-only and overrideable per call.
        Strategies that build metadata incrementally (e.g. rth_trend_pullback,
        volatility_squeeze_breakout) can still use this helper for the final
        assembly and ``dict.update`` the result as needed.
        """
        out: dict[str, Any] = {}
        if entry_price is not None:
            out["entry_price"] = float(entry_price)
        if limit_price is not None:
            out["limit_price"] = float(limit_price)
        if mark_price_hint is not None:
            out["mark_price_hint"] = float(mark_price_hint)
        if leading:
            out.update(leading)
        if final_priority_score is not None:
            out[score_key] = round(float(final_priority_score), score_round)
        if adjustments:
            out.update(adjustments)
        if fvg_adjustments:
            out.update(fvg_adjustments)
        if management:
            out.update(management)
        if retest_plan:
            retest_meta = retest_plan.get("metadata", {}) if isinstance(retest_plan, dict) else {}
            if retest_meta:
                out.update(retest_meta)
        if ladder_meta:
            out.update(ladder_meta)
        if chart_ctx is not None:
            out.update(self._chart_lists(chart_ctx))
        if ms_ctx is not None:
            out.update(self._structure_lists(ms_ctx, prefix=ms_prefix))
        if sr_ctx is not None:
            out.update(self._sr_lists(sr_ctx))
        if tech_ctx is not None:
            out.update(self._technical_lists(tech_ctx, prefix=tech_prefix))
        if extras:
            out.update(extras)
        return out

    def _dual_counter_divergence_reason(self, side: Side, tech_ctx) -> str | None:
        if not self._shared_entry_enabled("use_divergence_filter", True):
            return None
        if not bool(self._technical_level_setting("enabled", True)) or not bool(self._technical_level_setting("divergence_enabled", True)):
            return None
        if not bool(self._technical_level_setting("divergence_block_dual_counter", True)):
            return None
        if side == Side.LONG and bool(getattr(tech_ctx, "bearish_rsi_divergence", False)) and bool(getattr(tech_ctx, "bearish_obv_divergence", False)):
            return "dual_counter_divergence(rsi=bearish,obv=bearish)"
        if side == Side.SHORT and bool(getattr(tech_ctx, "bullish_rsi_divergence", False)) and bool(getattr(tech_ctx, "bullish_obv_divergence", False)):
            return "dual_counter_divergence(rsi=bullish,obv=bullish)"
        return None

    def _technical_entry_adjustment(self, side: Side, tech_ctx) -> float:
        if not self._shared_entry_enabled("use_technical_entry_adjustment", True):
            return 0.0
        if not bool(self._technical_level_setting("enabled", True)):
            return 0.0
        bonus = 0.0
        channel_bonus = float(self._technical_level_setting("entry_bonus_channel_alignment", 0.25) or 0.25)
        trendline_bonus = float(self._technical_level_setting("entry_bonus_trendline_respect", 0.25) or 0.25)
        bollinger_midband_bonus = float(self._technical_level_setting("bollinger_entry_bonus_midband", 0.18) or 0.18)
        bollinger_outer_penalty = float(self._technical_level_setting("bollinger_entry_penalty_outer_band", 0.22) or 0.22)
        extension_penalty = float(self._technical_level_setting("entry_penalty_near_extension", 0.35) or 0.35)
        near_extension = float(self._technical_level_setting("fib_near_extension_pct", 0.0060) or 0.0060)
        near_edge = float(self._technical_level_setting("channel_near_edge_pct", 0.18) or 0.18)
        channel = getattr(tech_ctx, "channel", None)
        position_pct = getattr(channel, "position_pct", None)
        bb_mid = getattr(tech_ctx, "bollinger_mid", None)
        bb_upper = getattr(tech_ctx, "bollinger_upper", None)
        bb_lower = getattr(tech_ctx, "bollinger_lower", None)
        bb_pct = getattr(tech_ctx, "bollinger_percent_b", None)
        bb_squeeze = bool(getattr(tech_ctx, "bollinger_squeeze", False))
        price = _safe_float(getattr(tech_ctx, "current_price", None), 0.0)
        adx = getattr(tech_ctx, "adx", None)
        dmi_bias = str(getattr(tech_ctx, "dmi_bias", "neutral") or "neutral")
        adx_rising = bool(getattr(tech_ctx, "adx_rising", False))
        adx_min = float(self._technical_level_setting("adx_min_strength", 18.0) or 18.0)
        adx_bonus = float(self._technical_level_setting("adx_entry_bonus", 0.22) or 0.22)
        adx_rising_bonus = float(self._technical_level_setting("adx_rising_bonus", 0.10) or 0.10)
        adx_weak_penalty = float(self._technical_level_setting("adx_weak_penalty", 0.12) or 0.12)
        open_avwap = getattr(tech_ctx, "anchored_vwap_open", None)
        bull_avwap = getattr(tech_ctx, "anchored_vwap_bullish_impulse", None)
        bear_avwap = getattr(tech_ctx, "anchored_vwap_bearish_impulse", None)
        avwap_bonus = float(self._technical_level_setting("anchored_vwap_entry_bonus", 0.20) or 0.20)
        avwap_penalty = float(self._technical_level_setting("anchored_vwap_entry_penalty", 0.18) or 0.18)
        atr_expansion = getattr(tech_ctx, "atr_expansion_mult", None)
        atr_expand_min = float(self._technical_level_setting("atr_expansion_min_mult", 0.80) or 0.80)
        atr_expand_bonus = float(self._technical_level_setting("atr_expansion_bonus", 0.14) or 0.14)
        atr_stretch_max = float(self._technical_level_setting("atr_stretch_penalty_mult", 2.80) or 2.80)
        atr_stretch_penalty = float(self._technical_level_setting("atr_stretch_penalty", 0.18) or 0.18)
        stretch_vwap = getattr(tech_ctx, "atr_stretch_vwap_mult", None)
        stretch_ema20 = getattr(tech_ctx, "atr_stretch_ema20_mult", None)
        obv_bias = str(getattr(tech_ctx, "obv_bias", "neutral") or "neutral")
        obv_bonus = float(self._technical_level_setting("obv_entry_bonus", 0.12) or 0.12)
        obv_penalty = float(self._technical_level_setting("obv_entry_penalty", 0.10) or 0.10)
        div_rsi_penalty = float(self._technical_level_setting("divergence_counter_rsi_penalty", 0.12) or 0.12)
        div_obv_penalty = float(self._technical_level_setting("divergence_counter_obv_penalty", 0.10) or 0.10)
        divergence_enabled = bool(self._technical_level_setting("divergence_enabled", True))
        if side == Side.LONG:
            if bool(self._technical_level_setting("trendline_enabled", True)):
                if bool(getattr(tech_ctx, "support_respected", False)):
                    bonus += trendline_bonus
                if bool(getattr(tech_ctx, "trendline_break_up", False)):
                    bonus += trendline_bonus * 0.8
            if bool(self._technical_level_setting("channel_enabled", True)) and bool(getattr(channel, "valid", False)) and position_pct is not None:
                if str(getattr(channel, "bias", "neutral")) == "bullish" and float(position_pct) <= 0.60:
                    bonus += channel_bonus
                if float(position_pct) >= 1.0 - near_edge:
                    bonus -= channel_bonus
            if bool(self._technical_level_setting("fib_enabled", True)):
                dist = getattr(tech_ctx, "bullish_extension_distance_pct", None)
                if dist is not None and float(dist) <= near_extension:
                    bonus -= extension_penalty
            if bool(self._technical_level_setting("bollinger_enabled", True)) and bb_mid is not None and bb_upper is not None and bb_lower is not None and price > 0:
                if price >= float(bb_mid) and (bb_pct is None or float(bb_pct) <= 0.82):
                    bonus += bollinger_midband_bonus
                if price >= float(bb_upper) or (bb_pct is not None and float(bb_pct) >= 0.96):
                    bonus -= bollinger_outer_penalty
                if bb_squeeze and bool(getattr(tech_ctx, "trendline_break_up", False)):
                    bonus += bollinger_midband_bonus * 0.5
            if bool(self._technical_level_setting("adx_enabled", True)) and adx is not None:
                if dmi_bias == "bullish" and float(adx) >= adx_min:
                    bonus += adx_bonus
                    if adx_rising:
                        bonus += adx_rising_bonus
                elif float(adx) < adx_min * 0.8 and not bb_squeeze:
                    bonus -= adx_weak_penalty
            if bool(self._technical_level_setting("anchored_vwap_enabled", True)) and price > 0:
                if open_avwap is not None and price >= float(open_avwap):
                    bonus += avwap_bonus * 0.6
                elif open_avwap is not None:
                    bonus -= avwap_penalty * 0.6
                if bull_avwap is not None and price >= float(bull_avwap):
                    bonus += avwap_bonus
                elif bull_avwap is not None:
                    bonus -= avwap_penalty
            if bool(self._technical_level_setting("atr_context_enabled", True)):
                if atr_expansion is not None and float(atr_expansion) >= atr_expand_min and (stretch_vwap is None or float(stretch_vwap) <= atr_stretch_max):
                    bonus += atr_expand_bonus
                if stretch_vwap is not None and float(stretch_vwap) >= atr_stretch_max:
                    bonus -= atr_stretch_penalty
                if stretch_ema20 is not None and float(stretch_ema20) >= atr_stretch_max:
                    bonus -= atr_stretch_penalty * 0.75
            if bool(self._technical_level_setting("obv_enabled", True)):
                if obv_bias == "bullish":
                    bonus += obv_bonus
                elif obv_bias == "bearish":
                    bonus -= obv_penalty
            if divergence_enabled:
                if bool(getattr(tech_ctx, "bearish_rsi_divergence", False)):
                    bonus -= div_rsi_penalty
                if bool(getattr(tech_ctx, "bearish_obv_divergence", False)):
                    bonus -= div_obv_penalty
        else:
            if bool(self._technical_level_setting("trendline_enabled", True)):
                if bool(getattr(tech_ctx, "resistance_respected", False)):
                    bonus += trendline_bonus
                if bool(getattr(tech_ctx, "trendline_break_down", False)):
                    bonus += trendline_bonus * 0.8
            if bool(self._technical_level_setting("channel_enabled", True)) and bool(getattr(channel, "valid", False)) and position_pct is not None:
                if str(getattr(channel, "bias", "neutral")) == "bearish" and float(position_pct) >= 0.40:
                    bonus += channel_bonus
                if float(position_pct) <= near_edge:
                    bonus -= channel_bonus
            if bool(self._technical_level_setting("fib_enabled", True)):
                dist = getattr(tech_ctx, "bearish_extension_distance_pct", None)
                if dist is not None and float(dist) <= near_extension:
                    bonus -= extension_penalty
            if bool(self._technical_level_setting("bollinger_enabled", True)) and bb_mid is not None and bb_upper is not None and bb_lower is not None and price > 0:
                if price <= float(bb_mid) and (bb_pct is None or float(bb_pct) >= 0.18):
                    bonus += bollinger_midband_bonus
                if price <= float(bb_lower) or (bb_pct is not None and float(bb_pct) <= 0.04):
                    bonus -= bollinger_outer_penalty
                if bb_squeeze and bool(getattr(tech_ctx, "trendline_break_down", False)):
                    bonus += bollinger_midband_bonus * 0.5
            if bool(self._technical_level_setting("adx_enabled", True)) and adx is not None:
                if dmi_bias == "bearish" and float(adx) >= adx_min:
                    bonus += adx_bonus
                    if adx_rising:
                        bonus += adx_rising_bonus
                elif float(adx) < adx_min * 0.8 and not bb_squeeze:
                    bonus -= adx_weak_penalty
            if bool(self._technical_level_setting("anchored_vwap_enabled", True)) and price > 0:
                if open_avwap is not None and price <= float(open_avwap):
                    bonus += avwap_bonus * 0.6
                elif open_avwap is not None:
                    bonus -= avwap_penalty * 0.6
                if bear_avwap is not None and price <= float(bear_avwap):
                    bonus += avwap_bonus
                elif bear_avwap is not None:
                    bonus -= avwap_penalty
            if bool(self._technical_level_setting("atr_context_enabled", True)):
                if atr_expansion is not None and float(atr_expansion) >= atr_expand_min and (stretch_vwap is None or float(stretch_vwap) <= atr_stretch_max):
                    bonus += atr_expand_bonus
                if stretch_vwap is not None and float(stretch_vwap) >= atr_stretch_max:
                    bonus -= atr_stretch_penalty
                if stretch_ema20 is not None and float(stretch_ema20) >= atr_stretch_max:
                    bonus -= atr_stretch_penalty * 0.75
            if bool(self._technical_level_setting("obv_enabled", True)):
                if obv_bias == "bearish":
                    bonus += obv_bonus
                elif obv_bias == "bullish":
                    bonus -= obv_penalty
            if divergence_enabled:
                if bool(getattr(tech_ctx, "bullish_rsi_divergence", False)):
                    bonus -= div_rsi_penalty
                if bool(getattr(tech_ctx, "bullish_obv_divergence", False)):
                    bonus -= div_obv_penalty
        return float(bonus)

    def _sr_entry_adjustment_components(self, side: Side, sr_ctx) -> dict[str, float]:
        out = {
            "sr_directional_bias": 0.0,
            "sr_bias_component": 0.0,
            "sr_favorable_proximity_score": 0.0,
            "sr_opposing_proximity_score": 0.0,
            "sr_entry_adjustment": 0.0,
        }
        if not bool(self._support_resistance_setting("entry_proximity_scoring_enabled", True)):
            return out
        if not bool(self._support_resistance_setting("enabled", True)):
            return out
        if sr_ctx is None:
            return out
        try:
            raw_bias = _safe_float(getattr(sr_ctx, "bias_score", 0.0), 0.0)
            directional_bias = raw_bias if side == Side.LONG else -raw_bias
            bias_weight = max(0.0, float(self._support_resistance_setting("entry_bias_score_weight", 0.60) or 0.60))
            favorable_bonus = max(0.0, float(self._support_resistance_setting("entry_favorable_proximity_bonus", 0.35) or 0.35))
            opposing_penalty = max(0.0, float(self._support_resistance_setting("entry_opposing_proximity_penalty", 0.35) or 0.35))
            proximity_window_atr = max(0.05, float(self._support_resistance_setting("proximity_atr_mult", 0.75) or 0.75))
            bias_component = directional_bias * bias_weight

            if side == Side.LONG:
                favorable_near = bool(getattr(sr_ctx, "near_support", False)) and not bool(getattr(sr_ctx, "breakdown_below_support", False))
                favorable_dist = _optional_float(getattr(sr_ctx, "support_distance_atr", None))
                opposing_near = bool(getattr(sr_ctx, "near_resistance", False)) and not bool(getattr(sr_ctx, "breakout_above_resistance", False))
                opposing_dist = _optional_float(getattr(sr_ctx, "resistance_distance_atr", None))
            else:
                favorable_near = bool(getattr(sr_ctx, "near_resistance", False)) and not bool(getattr(sr_ctx, "breakout_above_resistance", False))
                favorable_dist = _optional_float(getattr(sr_ctx, "resistance_distance_atr", None))
                opposing_near = bool(getattr(sr_ctx, "near_support", False)) and not bool(getattr(sr_ctx, "breakdown_below_support", False))
                opposing_dist = _optional_float(getattr(sr_ctx, "support_distance_atr", None))

            def _proximity_score(dist_atr: float | None) -> float:
                if dist_atr is None:
                    return 0.0
                return max(0.0, min(1.0, 1.0 - (float(dist_atr) / proximity_window_atr)))

            favorable_score = favorable_bonus * _proximity_score(favorable_dist) if favorable_near else 0.0
            opposing_score = opposing_penalty * _proximity_score(opposing_dist) if opposing_near else 0.0
            total = bias_component + favorable_score - opposing_score
            out.update({
                "sr_directional_bias": round(directional_bias, 4),
                "sr_bias_component": round(bias_component, 4),
                "sr_favorable_proximity_score": round(favorable_score, 4),
                "sr_opposing_proximity_score": round(opposing_score, 4),
                "sr_entry_adjustment": round(total, 4),
            })
        except Exception:
            return out
        return out

    def _entry_adjustment_components(self, side: Side, sr_ctx=None, tech_ctx=None) -> dict[str, float]:
        sr_fields = self._sr_entry_adjustment_components(side, sr_ctx)
        tech_adjustment = round(self._technical_entry_adjustment(side, tech_ctx), 4) if tech_ctx is not None else 0.0
        total = round(float(sr_fields.get("sr_entry_adjustment", 0.0)) + tech_adjustment, 4)
        return {
            **sr_fields,
            "technical_entry_adjustment": tech_adjustment,
            "entry_context_adjustment": total,
        }

    def _refine_bullish_technical_levels(self, close: float, stop: float, target: float | None, tech_ctx, frame: pd.DataFrame | None):
        if not self._shared_entry_enabled("use_technical_stop_target_refinement", True):
            return float(stop), (None if target is None else float(target))
        if not bool(self._technical_level_setting("enabled", True)):
            return float(stop), (None if target is None else float(target))
        atr = _safe_float(frame.iloc[-1]["atr14"], close * 0.0015) if frame is not None and not frame.empty and "atr14" in frame.columns else max(close * 0.0015, 0.01)
        buffer = max(atr * 0.12, close * 0.0010)
        if bool(self._technical_level_setting("stop_use_trendline", True)) and getattr(tech_ctx, "support_trendline", None) is not None:
            support_value = _safe_float(getattr(tech_ctx.support_trendline, "current_value", None), 0.0)
            trend_stop = support_value - buffer
            if 0 < trend_stop < close:
                stop = max(float(stop), float(trend_stop))
        if target is not None:
            risk = max(close - float(stop), buffer)
            caps: list[float] = []
            if bool(self._technical_level_setting("target_use_fib", True)) and getattr(tech_ctx, "nearest_bullish_extension", None) is not None:
                caps.append(float(tech_ctx.nearest_bullish_extension) - buffer)
            channel_ctx = getattr(tech_ctx, "channel", None)
            if bool(self._technical_level_setting("target_use_channel", True)) and bool(getattr(channel_ctx, "valid", False)) and getattr(channel_ctx, "upper", None) is not None:
                caps.append(float(getattr(channel_ctx, "upper")) - buffer)
            if bool(self._technical_level_setting("target_use_bollinger", True)) and getattr(tech_ctx, "bollinger_upper", None) is not None and not bool(getattr(tech_ctx, "bollinger_squeeze", False)):
                caps.append(float(tech_ctx.bollinger_upper) - buffer)
            if bool(self._technical_level_setting("target_use_trendline", True)) and getattr(tech_ctx, "resistance_trendline", None) is not None and not bool(getattr(tech_ctx, "trendline_break_up", False)):
                caps.append(float(tech_ctx.resistance_trendline.current_value) - buffer)
            valid = [cap for cap in caps if cap > close + max(buffer, risk * 0.35)]
            if valid:
                proposed_target = min(float(target), min(valid))
                # R:R floor: don't let a tech level crush reward below the
                # configured min_target_rr. Falls back to the un-capped
                # target when the cap would harm the trade.
                if self._target_meets_min_rr(Side.LONG, close, stop, proposed_target):
                    target = proposed_target
        return float(stop), (None if target is None else float(target))

    def _refine_bearish_technical_levels(self, close: float, stop: float, target: float | None, tech_ctx, frame: pd.DataFrame | None):
        if not self._shared_entry_enabled("use_technical_stop_target_refinement", True):
            return float(stop), (None if target is None else float(target))
        if not bool(self._technical_level_setting("enabled", True)):
            return float(stop), (None if target is None else float(target))
        atr = _safe_float(frame.iloc[-1]["atr14"], close * 0.0015) if frame is not None and not frame.empty and "atr14" in frame.columns else max(close * 0.0015, 0.01)
        buffer = max(atr * 0.12, close * 0.0010)
        if bool(self._technical_level_setting("stop_use_trendline", True)) and getattr(tech_ctx, "resistance_trendline", None) is not None:
            resistance_value = _safe_float(getattr(tech_ctx.resistance_trendline, "current_value", None), 0.0)
            trend_stop = resistance_value + buffer
            if trend_stop > close:
                stop = min(float(stop), float(trend_stop))
        if target is not None:
            risk = max(float(stop) - close, buffer)
            caps: list[float] = []
            if bool(self._technical_level_setting("target_use_fib", True)) and getattr(tech_ctx, "nearest_bearish_extension", None) is not None:
                caps.append(float(tech_ctx.nearest_bearish_extension) + buffer)
            channel_ctx = getattr(tech_ctx, "channel", None)
            if bool(self._technical_level_setting("target_use_channel", True)) and bool(getattr(channel_ctx, "valid", False)) and getattr(channel_ctx, "lower", None) is not None:
                caps.append(float(getattr(channel_ctx, "lower")) + buffer)
            if bool(self._technical_level_setting("target_use_bollinger", True)) and getattr(tech_ctx, "bollinger_lower", None) is not None and not bool(getattr(tech_ctx, "bollinger_squeeze", False)):
                caps.append(float(tech_ctx.bollinger_lower) + buffer)
            if bool(self._technical_level_setting("target_use_trendline", True)) and getattr(tech_ctx, "support_trendline", None) is not None and not bool(getattr(tech_ctx, "trendline_break_down", False)):
                caps.append(float(tech_ctx.support_trendline.current_value) + buffer)
            valid = [cap for cap in caps if cap < close - max(buffer, risk * 0.35)]
            if valid:
                proposed_target = max(float(target), max(valid))
                # R:R floor — see comment on the bullish twin above.
                if self._target_meets_min_rr(Side.SHORT, close, stop, proposed_target):
                    target = proposed_target
        return float(stop), (None if target is None else float(target))

    def _technical_exit_signal(self, direction: str, frame: pd.DataFrame, close: float, ema9: float, ema20: float, vwap: float, close_pos: float, position: Position) -> tuple[bool, str]:
        if not self._shared_exit_enabled("use_technical_exit", True):
            return False, "hold"
        if not bool(self._technical_level_setting("enabled", True)):
            return False, "hold"
        tech_ctx = self._technical_context(frame)
        atr = _safe_float(frame.iloc[-1]["atr14"], close * 0.0015) if frame is not None and not frame.empty and "atr14" in frame.columns else max(close * 0.0015, 0.01)
        buffer = max(atr * float(self._technical_level_setting("trendline_breakout_buffer_atr_mult", 0.15) or 0.15), close * 0.0010)
        if direction == "bullish":
            weak_tape = self._shared_exit_tape_confirm("bullish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.48)
            if bool(getattr(tech_ctx, "trendline_break_down", False)) and self._shared_exit_enabled("use_trendline_break", True) and weak_tape:
                support_value = _safe_float(getattr(getattr(tech_ctx, "support_trendline", None), "current_value", None), close)
                return True, f"trendline_break_exit:{support_value:.4f}"
            channel_ctx = getattr(tech_ctx, "channel", None)
            if bool(getattr(channel_ctx, "valid", False)) and getattr(channel_ctx, "lower", None) is not None and self._shared_exit_enabled("use_channel_break", True):
                lower = float(getattr(channel_ctx, "lower"))
                tape_ok = self._shared_exit_tape_confirm("bullish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.45)
                if close <= lower - buffer and tape_ok:
                    return True, f"channel_breakdown_exit:{lower:.4f}"
            if bool(getattr(tech_ctx, "bollinger_upper_reject", False)) and self._shared_exit_enabled("use_bollinger_reject", True):
                tape_ok = self._shared_exit_tape_confirm("bullish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.52)
                if tape_ok:
                    upper = _safe_float(getattr(tech_ctx, "bollinger_upper", None), close)
                    return True, f"bollinger_upper_reject_exit:{upper:.4f}"
            if self._shared_exit_enabled("use_anchored_vwap_loss", True):
                open_avwap = _safe_float(getattr(tech_ctx, "anchored_vwap_open", None), 0.0)
                bull_avwap = _safe_float(getattr(tech_ctx, "anchored_vwap_bullish_impulse", None), 0.0)
                avwap_floor = max(open_avwap, bull_avwap)
                # Armed-guard: the position must have traded at-or-above
                # avwap_floor + buffer at some point since entry. Without
                # this, a LONG entered below the floor (common when the
                # bullish-impulse AVWAP sits above current price) triggers
                # an instant "loss" exit on the next tick — observed on
                # AMZN 2026-04-24 10:59 (13-second exit, -$3.99). Mirrors
                # how trail_armed requires a favorable move before arming.
                # Buffer tightens the armed threshold so a one-tick poke
                # right at the floor doesn't arm the exit prematurely.
                highest_price = _safe_float(getattr(position, "highest_price", None), float(position.entry_price))
                avwap_armed = avwap_floor > 0 and highest_price >= avwap_floor + buffer
                tape_ok = self._shared_exit_tape_confirm("bullish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.48)
                if avwap_floor > 0 and avwap_armed and close < avwap_floor - buffer and tape_ok:
                    return True, f"anchored_vwap_loss_exit:{avwap_floor:.4f}"
        else:
            weak_tape = self._shared_exit_tape_confirm("bearish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.52)
            if bool(getattr(tech_ctx, "trendline_break_up", False)) and self._shared_exit_enabled("use_trendline_break", True) and weak_tape:
                resistance_value = _safe_float(getattr(getattr(tech_ctx, "resistance_trendline", None), "current_value", None), close)
                return True, f"trendline_break_exit:{resistance_value:.4f}"
            channel_ctx = getattr(tech_ctx, "channel", None)
            if bool(getattr(channel_ctx, "valid", False)) and getattr(channel_ctx, "upper", None) is not None and self._shared_exit_enabled("use_channel_break", True):
                upper = float(getattr(channel_ctx, "upper"))
                tape_ok = self._shared_exit_tape_confirm("bearish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.55)
                if close >= upper + buffer and tape_ok:
                    return True, f"channel_breakout_exit:{upper:.4f}"
            if bool(getattr(tech_ctx, "bollinger_lower_reject", False)) and self._shared_exit_enabled("use_bollinger_reject", True):
                tape_ok = self._shared_exit_tape_confirm("bearish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.48)
                if tape_ok:
                    lower = _safe_float(getattr(tech_ctx, "bollinger_lower", None), close)
                    return True, f"bollinger_lower_reject_exit:{lower:.4f}"
            if self._shared_exit_enabled("use_anchored_vwap_loss", True):
                open_avwap = _safe_float(getattr(tech_ctx, "anchored_vwap_open", None), 0.0)
                bear_avwap = _safe_float(getattr(tech_ctx, "anchored_vwap_bearish_impulse", None), 0.0)
                avwap_ceiling = min(px for px in [open_avwap, bear_avwap] if px > 0) if any(px > 0 for px in [open_avwap, bear_avwap]) else 0.0
                # Mirror of the bullish armed-guard. Require the position
                # to have traded at-or-below avwap_ceiling - buffer at some
                # point since entry. Without this, a SHORT entered with
                # price already near the AVWAP ceiling triggers an instant
                # reclaim exit on the next tick — observed on META
                # 2026-04-24 09:35 (55-second exit, -$68.86).
                lowest_price = _safe_float(getattr(position, "lowest_price", None), float(position.entry_price))
                avwap_armed = avwap_ceiling > 0 and lowest_price <= avwap_ceiling - buffer
                tape_ok = self._shared_exit_tape_confirm("bearish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.52)
                if avwap_ceiling > 0 and avwap_armed and close > avwap_ceiling + buffer and tape_ok:
                    return True, f"anchored_vwap_reclaim_exit:{avwap_ceiling:.4f}"
        return False, "hold"

    def _structure_event_recent(self, age_bars: int | None) -> bool:
        lookback = int(self._support_resistance_setting("structure_event_lookback_bars", 6) or 6)
        return age_bars is not None and age_bars <= lookback

    def _active_structure_break(self, flag: bool, age_bars: int | None) -> bool:
        return bool(flag) and self._structure_event_recent(age_bars)

    @staticmethod
    def _fvg_gap_state(gap: Any, current_price: float) -> dict[str, Any]:
        lower = _optional_float(getattr(gap, "lower", None))
        upper = _optional_float(getattr(gap, "upper", None))
        midpoint = _optional_float(getattr(gap, "midpoint", None))
        size = _optional_float(getattr(gap, "size", None))
        filled_pct = max(0.0, min(1.0, _optional_float(getattr(gap, "filled_pct", None), 0.0) or 0.0))
        direction = str(getattr(gap, "direction", "")).strip().lower()
        if lower is None or upper is None or midpoint is None or size is None or size <= 0:
            return {"state": "none", "direction": direction or "unknown", "distance": None, "distance_pct": None, "filled_pct": filled_pct}
        close = float(current_price or 0.0)
        eps = max(float(size) * 0.05, abs(close) * 1e-6, 1e-8)
        if close < lower:
            distance = float(lower - close)
        elif close > upper:
            distance = float(close - upper)
        else:
            distance = 0.0
        if direction == "bullish":
            state = "invalidated" if close < lower - eps else ("active" if close <= upper + eps else "validated")
        elif direction == "bearish":
            state = "invalidated" if close > upper + eps else ("active" if close >= lower - eps else "validated")
        else:
            state = "active" if lower - eps <= close <= upper + eps else "validated"
        return {
            "state": state,
            "direction": direction or "unknown",
            "lower": float(lower),
            "upper": float(upper),
            "midpoint": float(midpoint),
            "size": float(size),
            "filled_pct": filled_pct,
            "distance": float(distance),
            "distance_pct": float(distance / max(abs(close), 1e-9)) if close else None,
        }

    def _score_fvg_context(self, current_price: float, ctx: Any, *, timeframe_minutes: int) -> dict[str, Any]:
        close = float(current_price or 0.0)
        tf = max(1, int(timeframe_minutes or 1))
        is_htf = tf > 1
        valid_base = 0.37 if is_htf else 0.22
        active_base = 0.24 if is_htf else 0.15
        invalid_base = 0.42 if is_htf else 0.26
        proximity_floor = abs(close) * (0.0060 if is_htf else 0.0030)
        half_life_bars = 10.0 if is_htf else 14.0
        min_recency_factor = 0.30

        def _gap_recency_factor(gap: Any) -> float:
            stamp = getattr(gap, "last_seen", None) or getattr(gap, "first_seen", None)
            if not stamp:
                return 1.0
            try:
                seen = pd.Timestamp(stamp)
                if seen.tzinfo is not None:
                    seen = seen.tz_convert(None)
                current = pd.Timestamp(now_et())
                if current.tzinfo is not None:
                    current = current.tz_convert(None)
                age_seconds = max(0.0, float((current - seen).total_seconds()))
            except Exception:
                return 1.0
            age_bars = age_seconds / max(float(tf) * 60.0, 60.0)
            factor = 0.5 ** (age_bars / max(half_life_bars, 1.0))
            return float(max(min_recency_factor, min(1.0, factor)))

        def _score_gap(gap: Any) -> tuple[float, float, dict[str, Any]]:
            info = self._fvg_gap_state(gap, close)
            state = str(info.get("state", "none"))
            direction = str(info.get("direction", "unknown"))
            size = _optional_float(info.get("size"), 0.0) or 0.0
            distance = _optional_float(info.get("distance"), 0.0) or 0.0
            fill = max(0.0, min(1.0, _optional_float(info.get("filled_pct"), 0.0) or 0.0))
            if state == "none" or direction not in {"bullish", "bearish"}:
                return 0.0, 0.0, info
            distance_limit = max(float(size) * 2.5, float(proximity_floor), 1e-8)
            closeness = max(0.0, 1.0 - (float(distance) / distance_limit))
            if closeness <= 0.0:
                info["closeness"] = 0.0
                info["recency_factor"] = 0.0
                return 0.0, 0.0, info
            fill_damp = 1.0 - (0.35 * fill if state != "invalidated" else 0.0)
            recency_factor = _gap_recency_factor(gap)
            base = invalid_base if state == "invalidated" else (valid_base if state == "validated" else active_base)
            magnitude = float(base) * float(closeness) * float(fill_damp) * float(recency_factor)
            bull = 0.0
            bear = 0.0
            if direction == "bullish":
                if state == "invalidated":
                    bear += magnitude
                    bull -= magnitude * 0.80
                else:
                    bull += magnitude
                    bear -= magnitude * 0.80
            elif direction == "bearish":
                if state == "invalidated":
                    bull += magnitude
                    bear -= magnitude * 0.80
                else:
                    bear += magnitude
                    bull -= magnitude * 0.80
            info["closeness"] = float(closeness)
            info["recency_factor"] = float(recency_factor)
            info["score_magnitude"] = float(magnitude)
            return bull, bear, info

        bull_score = 0.0
        bear_score = 0.0
        nearest_bullish = getattr(ctx, "nearest_bullish_fvg", None)
        nearest_bearish = getattr(ctx, "nearest_bearish_fvg", None)
        bull_pos, bear_neg, bullish_info = _score_gap(nearest_bullish)
        bull_score += bull_pos
        bear_score += bear_neg
        bull_neg, bear_pos, bearish_info = _score_gap(nearest_bearish)
        bull_score += bull_neg
        bear_score += bear_pos
        directional_pressure = max(0.0, bull_score, bear_score)
        return {
            "bull_score": float(bull_score),
            "bear_score": float(bear_score),
            "directional_pressure": float(directional_pressure),
            "timeframe_minutes": tf,
            "nearest_bullish": bullish_info,
            "nearest_bearish": bearish_info,
        }

    def _fvg_entry_adjustment_components(self, side: Side, symbol: str, frame: pd.DataFrame | None, data=None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "fvg_context_enabled": False,
            "fvg_entry_adjustment": 0.0,
            "fvg_same_direction_score": 0.0,
            "fvg_opposing_score": 0.0,
            "fvg_continuation_bias": 0.0,
            "fvg_reversal_bias": 0.0,
            "fvg_same_direction_label": "bullish" if side == Side.LONG else "bearish",
            "fvg_opposing_label": "bearish" if side == Side.LONG else "bullish",
        }
        if frame is None or frame.empty or not bool(self._shared_entry_enabled("use_fvg_context", True)):
            return out
        close = _safe_float(frame.iloc[-1].get("close"), 0.0)
        if close <= 0:
            return out
        htf_ctx = self._htf_context(
            symbol,
            data,
            timeframe_minutes=self._sr_timeframe_minutes(),
            lookback_days=self._sr_lookback_days(),
            pivot_span=int(self._support_resistance_setting("pivot_span", 2) or 2),
            max_levels_per_side=int(self._support_resistance_setting("max_levels_per_side", 6) or 6),
            atr_tolerance_mult=float(self._support_resistance_setting("atr_tolerance_mult", 0.35) or 0.35),
            pct_tolerance=float(self._support_resistance_setting("pct_tolerance", 0.0030) or 0.0030),
            stop_buffer_atr_mult=float(self._support_resistance_setting("stop_buffer_atr_mult", 0.25) or 0.25),
            ema_fast_span=int(self._support_resistance_setting("ema_fast_span", 50) or 50),
            ema_slow_span=int(self._support_resistance_setting("ema_slow_span", 200) or 200),
            refresh_seconds=self._sr_refresh_seconds(),
            current_price=close,
            use_prior_day_high_low=bool(self._support_resistance_setting("use_prior_day_high_low", True)),
            use_prior_week_high_low=bool(self._support_resistance_setting("use_prior_week_high_low", True)),
        )
        fvg1_ctx = self._one_minute_fvg_context(symbol, frame, data)
        htf_score = self._score_fvg_context(close, htf_ctx, timeframe_minutes=getattr(htf_ctx, "timeframe_minutes", self._sr_timeframe_minutes()))
        fvg1_score = self._score_fvg_context(close, fvg1_ctx, timeframe_minutes=1)
        htf_weight = max(0.0, float(self.params.get("htf_fvg_entry_weight", 0.55)))
        one_minute_weight = max(0.0, float(self.params.get("one_minute_fvg_entry_weight", 0.35)))
        opposing_mult = max(0.50, float(self.params.get("opposing_fvg_entry_penalty_mult", 1.00)))
        same_validated_bonus = float(self.params.get("same_direction_fvg_validated_bonus", 0.15))
        same_active_bonus = float(self.params.get("same_direction_fvg_active_bonus", 0.12))
        opposing_validated_penalty = float(self.params.get("opposing_fvg_validated_penalty", 0.15))
        opposing_active_penalty = float(self.params.get("opposing_fvg_active_penalty", 0.12))
        invalidated_opposing_bonus = float(self.params.get("invalidated_opposing_fvg_bonus", 0.10))
        # Same-direction invalidated penalty. Defaults to opposing_active_penalty * 0.85
        # so callers that don't configure it explicitly get exactly the same behavior
        # as before — a ~15% discount relative to an active opposing-direction gap,
        # because a filled continuation gap is a weaker bearish signal than a live one.
        same_invalidated_penalty = float(self.params.get("same_direction_fvg_invalidated_penalty", opposing_active_penalty * 0.85))

        if side == Side.LONG:
            same_htf = float(htf_score.get("bull_score", 0.0) or 0.0)
            opposing_htf = float(htf_score.get("bear_score", 0.0) or 0.0)
            same_1m = float(fvg1_score.get("bull_score", 0.0) or 0.0)
            opposing_1m = float(fvg1_score.get("bear_score", 0.0) or 0.0)
            same_htf_info = dict(htf_score.get("nearest_bullish", {}) or {})
            opposing_htf_info = dict(htf_score.get("nearest_bearish", {}) or {})
            same_1m_info = dict(fvg1_score.get("nearest_bullish", {}) or {})
            opposing_1m_info = dict(fvg1_score.get("nearest_bearish", {}) or {})
        else:
            same_htf = float(htf_score.get("bear_score", 0.0) or 0.0)
            opposing_htf = float(htf_score.get("bull_score", 0.0) or 0.0)
            same_1m = float(fvg1_score.get("bear_score", 0.0) or 0.0)
            opposing_1m = float(fvg1_score.get("bull_score", 0.0) or 0.0)
            same_htf_info = dict(htf_score.get("nearest_bearish", {}) or {})
            opposing_htf_info = dict(htf_score.get("nearest_bullish", {}) or {})
            same_1m_info = dict(fvg1_score.get("nearest_bearish", {}) or {})
            opposing_1m_info = dict(fvg1_score.get("nearest_bullish", {}) or {})

        raw_same = (same_htf * htf_weight) + (same_1m * one_minute_weight)
        raw_opposing = ((opposing_htf * htf_weight) + (opposing_1m * one_minute_weight)) * opposing_mult
        state_bonus = 0.0
        continuation_bias = 0.0
        reversal_bias = 0.0

        def _apply_state(info: dict[str, Any], *, same_direction: bool, weight: float) -> None:
            nonlocal state_bonus, continuation_bias, reversal_bias
            state = str(info.get("state", "none") or "none").strip().lower()
            if state == "none":
                return
            if same_direction:
                if state == "validated":
                    state_bonus += same_validated_bonus * weight
                    continuation_bias += 0.35 * weight
                elif state == "active":
                    state_bonus += same_active_bonus * weight
                    continuation_bias += 0.24 * weight
                elif state == "invalidated":
                    # Same-direction gap has been filled — weaker continuation signal.
                    # Uses its own parameter now, but the default preserves the
                    # historical opposing_active_penalty * 0.85 behavior.
                    state_bonus -= same_invalidated_penalty * weight
            else:
                if state == "validated":
                    state_bonus -= opposing_validated_penalty * weight
                elif state == "active":
                    state_bonus -= opposing_active_penalty * weight
                elif state == "invalidated":
                    state_bonus += invalidated_opposing_bonus * weight
                    continuation_bias += 0.08 * weight
                    reversal_bias += 0.18 * weight

        _apply_state(same_htf_info, same_direction=True, weight=htf_weight)
        _apply_state(same_1m_info, same_direction=True, weight=one_minute_weight)
        _apply_state(opposing_htf_info, same_direction=False, weight=htf_weight)
        _apply_state(opposing_1m_info, same_direction=False, weight=one_minute_weight)

        entry_adjustment = round(raw_same - raw_opposing + state_bonus, 4)
        continuation_bias = round(max(0.0, raw_same + continuation_bias + max(0.0, state_bonus)), 4)
        reversal_bias = round(max(0.0, reversal_bias), 4)
        out.update(
            {
                "fvg_context_enabled": True,
                "fvg_entry_adjustment": entry_adjustment,
                "fvg_same_direction_score": round(raw_same, 4),
                "fvg_opposing_score": round(raw_opposing, 4),
                "fvg_state_bonus": round(state_bonus, 4),
                "fvg_continuation_bias": continuation_bias,
                "fvg_reversal_bias": reversal_bias,
                "htf_fvg_bull_score": round(float(htf_score.get("bull_score", 0.0) or 0.0), 4),
                "htf_fvg_bear_score": round(float(htf_score.get("bear_score", 0.0) or 0.0), 4),
                "fvg_1m_bull_score": round(float(fvg1_score.get("bull_score", 0.0) or 0.0), 4),
                "fvg_1m_bear_score": round(float(fvg1_score.get("bear_score", 0.0) or 0.0), 4),
                "htf_fvg_same_state": str(same_htf_info.get("state", "none") or "none"),
                "htf_fvg_opposing_state": str(opposing_htf_info.get("state", "none") or "none"),
                "fvg_1m_same_state": str(same_1m_info.get("state", "none") or "none"),
                "fvg_1m_opposing_state": str(opposing_1m_info.get("state", "none") or "none"),
                "htf_fvg_same_midpoint": _optional_float(same_htf_info.get("midpoint")),
                "htf_fvg_opposing_midpoint": _optional_float(opposing_htf_info.get("midpoint")),
                "fvg_1m_same_midpoint": _optional_float(same_1m_info.get("midpoint")),
                "fvg_1m_opposing_midpoint": _optional_float(opposing_1m_info.get("midpoint")),
                "htf_fvg_same_distance_pct": _optional_float(same_htf_info.get("distance_pct")),
                "htf_fvg_opposing_distance_pct": _optional_float(opposing_htf_info.get("distance_pct")),
                "fvg_1m_same_distance_pct": _optional_float(same_1m_info.get("distance_pct")),
                "fvg_1m_opposing_distance_pct": _optional_float(opposing_1m_info.get("distance_pct")),
            }
        )
        return out

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
        # structure exits (which this refactor gates, see position_exit_signal).
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

    def _blocks_bullish_structure_entry(self, ms_ctx) -> bool:
        if not self._shared_entry_enabled("use_structure_filter", True):
            return False
        if not bool(self._support_resistance_setting("structure_enabled", True)):
            return False
        if self._active_structure_break(bool(getattr(ms_ctx, "choch_down", False)), getattr(ms_ctx, "choch_down_age_bars", None)):
            return True
        active_bos_up = self._active_structure_break(bool(getattr(ms_ctx, "bos_up", False)), getattr(ms_ctx, "bos_up_age_bars", None))
        return bool(getattr(ms_ctx, "bias", "neutral") == "bearish" and not active_bos_up)

    def _blocks_bearish_structure_entry(self, ms_ctx) -> bool:
        if not self._shared_entry_enabled("use_structure_filter", True):
            return False
        if not bool(self._support_resistance_setting("structure_enabled", True)):
            return False
        if self._active_structure_break(bool(getattr(ms_ctx, "choch_up", False)), getattr(ms_ctx, "choch_up_age_bars", None)):
            return True
        active_bos_down = self._active_structure_break(bool(getattr(ms_ctx, "bos_down", False)), getattr(ms_ctx, "bos_down_age_bars", None))
        return bool(getattr(ms_ctx, "bias", "neutral") == "bullish" and not active_bos_down)

    def _bullish_structure_block_reason(self, ms_ctx) -> str:
        lookback = int(self._support_resistance_setting("structure_event_lookback_bars", 6) or 6)
        return (
            f"market_structure_bearish(bias={getattr(ms_ctx, 'bias', 'neutral')},"
            f"last_high={getattr(ms_ctx, 'last_high_label', 'na')},"
            f"last_low={getattr(ms_ctx, 'last_low_label', 'na')},"
            f"choch_down_age={getattr(ms_ctx, 'choch_down_age_bars', 'na')},"
            f"max_age={lookback})"
        )

    def _bearish_structure_block_reason(self, ms_ctx) -> str:
        lookback = int(self._support_resistance_setting("structure_event_lookback_bars", 6) or 6)
        return (
            f"market_structure_bullish(bias={getattr(ms_ctx, 'bias', 'neutral')},"
            f"last_high={getattr(ms_ctx, 'last_high_label', 'na')},"
            f"last_low={getattr(ms_ctx, 'last_low_label', 'na')},"
            f"choch_up_age={getattr(ms_ctx, 'choch_up_age_bars', 'na')},"
            f"max_age={lookback})"
        )

    def _blocks_bullish_sr_entry(self, sr_ctx) -> bool:
        if not self._shared_entry_enabled("use_sr_filter", True):
            return False
        if not bool(self._support_resistance_setting("enabled", True)):
            return False
        if bool(sr_ctx.breakdown_below_support):
            return True
        dist_pct = sr_ctx.resistance_distance_pct
        dist_atr = sr_ctx.resistance_distance_atr
        too_close = False
        if dist_pct is not None and dist_pct <= float(self._support_resistance_setting("entry_min_clearance_pct", 0.0038)):
            too_close = True
        if dist_atr is not None and dist_atr <= float(self._support_resistance_setting("entry_min_clearance_atr", 0.85)):
            too_close = True
        # Breakout escape hatch: only bypass the clearance filter when
        # price is ACTIVELY above the current nearest resistance — i.e.
        # we're riding through the broken level, not after the SR engine
        # has advanced ``nearest_resistance`` to the next wall and we've
        # pulled back below it. 2026-04-20 logs showed META/INTC/TSLA
        # LONG'd at 0.06-0.5 ATR below their new nearest resistance while
        # ``breakout_above_resistance`` was still True from an earlier
        # break — the entries were approaching the next wall, not riding.
        nearest_res = getattr(sr_ctx, "nearest_resistance", None)
        nearest_res_price = float(getattr(nearest_res, "price", 0.0) or 0.0) if nearest_res is not None else 0.0
        current_price = float(getattr(sr_ctx, "current_price", 0.0) or 0.0)
        actively_above = bool(
            sr_ctx.breakout_above_resistance
            and 0 < nearest_res_price < current_price
        )
        return bool(too_close and not actively_above)

    def _blocks_bearish_sr_entry(self, sr_ctx) -> bool:
        if not self._shared_entry_enabled("use_sr_filter", True):
            return False
        if not bool(self._support_resistance_setting("enabled", True)):
            return False
        if bool(sr_ctx.breakout_above_resistance):
            return True
        dist_pct = sr_ctx.support_distance_pct
        dist_atr = sr_ctx.support_distance_atr
        too_close = False
        if dist_pct is not None and dist_pct <= float(self._support_resistance_setting("entry_min_clearance_pct", 0.0038)):
            too_close = True
        if dist_atr is not None and dist_atr <= float(self._support_resistance_setting("entry_min_clearance_atr", 0.85)):
            too_close = True
        # Breakdown escape hatch: only bypass when price is actively BELOW
        # the current nearest support (riding through the broken level),
        # not when a stale breakdown flag lingers after a bounce back
        # above. Symmetric with the bullish path above.
        nearest_sup = getattr(sr_ctx, "nearest_support", None)
        nearest_sup_price = float(getattr(nearest_sup, "price", 0.0) or 0.0) if nearest_sup is not None else 0.0
        current_price = float(getattr(sr_ctx, "current_price", 0.0) or 0.0)
        actively_below = bool(
            sr_ctx.breakdown_below_support
            and nearest_sup_price > 0
            and current_price < nearest_sup_price
        )
        return bool(too_close and not actively_below)

    def _refine_bullish_sr_levels(self, close: float, stop: float, target: float | None, sr_ctx):
        if not self._shared_entry_enabled("use_sr_stop_target_refinement", True):
            return float(stop), (None if target is None else float(target))
        level_buffer = float(sr_ctx.level_buffer or 0.0)
        if sr_ctx.nearest_support and close > float(sr_ctx.nearest_support.price):
            support_stop = float(sr_ctx.nearest_support.price) - level_buffer
            if support_stop < close:
                stop = max(float(stop), support_stop)
        if target is not None and sr_ctx.nearest_resistance and close < float(sr_ctx.nearest_resistance.price):
            capped_target = max(close * 1.001, float(sr_ctx.nearest_resistance.price) - level_buffer)
            proposed_target = min(float(target), capped_target)
            # R:R floor: only accept the cap if the resulting reward is still
            # tradeable. Without this guard, a nearby resistance can crush
            # R:R toward zero ($0.10 targets, etc.).
            if self._target_meets_min_rr(Side.LONG, close, stop, proposed_target):
                target = proposed_target
        return float(stop), (None if target is None else float(target))

    def _refine_bearish_sr_levels(self, close: float, stop: float, target: float | None, sr_ctx):
        if not self._shared_entry_enabled("use_sr_stop_target_refinement", True):
            return float(stop), (None if target is None else float(target))
        level_buffer = float(sr_ctx.level_buffer or 0.0)
        if sr_ctx.nearest_resistance and close < float(sr_ctx.nearest_resistance.price):
            resistance_stop = float(sr_ctx.nearest_resistance.price) + level_buffer
            if resistance_stop > close:
                stop = min(float(stop), resistance_stop)
        if target is not None and sr_ctx.nearest_support and close > float(sr_ctx.nearest_support.price):
            capped_target = min(close * 0.999, float(sr_ctx.nearest_support.price) + level_buffer)
            proposed_target = max(float(target), capped_target)
            # R:R floor — see comment on the bullish twin above.
            if self._target_meets_min_rr(Side.SHORT, close, stop, proposed_target):
                target = proposed_target
        return float(stop), (None if target is None else float(target))

    def position_exit_signal(self, position: Position, bars: dict[str, pd.DataFrame], data=None) -> tuple[bool, str]:
        chart_cfg = getattr(self.config, "chart_patterns", None)
        symbol = str(position.metadata.get("underlying") or position.symbol)
        frame = bars.get(symbol)
        # Time-stop: if the position has been held longer than
        # ``config.risk.time_stop_minutes`` AND absolute return since entry
        # is below ``time_stop_min_return_pct``, scratch it. Frees the slot
        # for an active setup. 2026-04-17 META held 223 min for +$0.16 on
        # EQL exit — exactly what this gate now prevents.
        try:
            time_stop_minutes = int(getattr(self.config.risk, "time_stop_minutes", 0) or 0)
        except Exception:
            time_stop_minutes = 0
        if time_stop_minutes > 0:
            try:
                held_minutes = max(0.0, (now_et() - position.entry_time).total_seconds() / 60.0)
            except Exception:
                held_minutes = 0.0
            if held_minutes >= time_stop_minutes:
                entry = float(position.entry_price) if position.entry_price else 0.0
                last_close: float | None = None
                if frame is not None and not frame.empty and "close" in frame.columns:
                    last_close = _optional_float(frame.iloc[-1]["close"], None)
                if entry > 0 and last_close is not None:
                    return_pct = abs((float(last_close) - entry) / entry)
                    min_return_pct = float(getattr(self.config.risk, "time_stop_min_return_pct", 0.003) or 0.0)
                    if return_pct < min_return_pct:
                        return True, f"time_stop:{int(held_minutes)}m"
        # ORB-entry grace window — suppresses chart_pattern and non-CHoCH
        # structure exits for the first N minutes of trades entered during
        # the ORB window. Pullbacks early in an ORB-entry trade often
        # present as bearish structure/chart signals but continue higher
        # once the opening flush resolves. See SupportResistanceConfig.
        # orb_entry_exit_grace_minutes for motivation + 2026-04-24 data.
        orb_entry = bool(position.metadata.get("orb_window_entry")) if isinstance(position.metadata, dict) else False
        orb_grace_minutes = int(self._support_resistance_setting("orb_entry_exit_grace_minutes", 20) or 0)
        try:
            orb_hold_minutes = max(0.0, (now_et() - position.entry_time).total_seconds() / 60.0)
        except Exception:
            orb_hold_minutes = 0.0
        orb_grace_active = orb_entry and orb_grace_minutes > 0 and orb_hold_minutes < orb_grace_minutes

        chart_enabled = (
            chart_cfg is not None
            and bool(self._chart_pattern_setting("enabled", True))
            and self._shared_exit_enabled("use_chart_pattern_exit", False)
            and not orb_grace_active
        )
        if frame is None or frame.empty:
            return False, "hold"
        # Only chart-pattern needs min_bars; other exit paths self-handle short frames.
        min_bars = max(12, int(self._chart_pattern_setting("lookback_bars", 32)) // 2) if chart_enabled else 0
        last = frame.iloc[-1]
        close = _safe_float(last["close"])
        ema9 = _safe_float(last["ema9"], close) if "ema9" in frame.columns else close
        ema20 = _safe_float(last["ema20"], close) if "ema20" in frame.columns else close
        vwap = _safe_float(last["vwap"], close) if "vwap" in frame.columns else close
        direction = self._direction_token(position)
        close_pos = _bar_close_position(frame)

        if chart_enabled and len(frame) >= min_bars:
            ctx = self._chart_context(frame)
            if direction == "bullish":
                opposing_reversal = sorted(ctx.matched_bearish_reversal)
                opposing_cont = sorted(ctx.matched_bearish_continuation)
                strong_opposing = bool(opposing_reversal) or (bool(opposing_cont) and ctx.bias_score <= -0.65)
                reversal_tape_weak = bool(opposing_reversal) and self._shared_exit_tape_confirm("bullish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.52)
                continuation_tape_weak = (
                    self._shared_exit_tape_confirm("bullish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.48)
                    or (
                        self._shared_exit_enabled("confirm_with_close_position", True)
                        and close_pos <= float(self._shared_exit_value("bullish_close_position_loose_max", 0.40))
                        and ((not self._shared_exit_enabled("confirm_with_ema9", True)) or close < ema9)
                        and ((not self._shared_exit_enabled("confirm_with_vwap", True)) or close < vwap)
                    )
                )
                if strong_opposing and (reversal_tape_weak or continuation_tape_weak):
                    opposing = opposing_reversal + [p for p in opposing_cont if p not in opposing_reversal]
                    return True, f"chart_pattern_exit:{'+'.join(opposing)}"
            else:
                opposing_reversal = sorted(ctx.matched_bullish_reversal)
                opposing_cont = sorted(ctx.matched_bullish_continuation)
                strong_opposing = bool(opposing_reversal) or (bool(opposing_cont) and ctx.bias_score >= 0.65)
                reversal_tape_weak = bool(opposing_reversal) and self._shared_exit_tape_confirm("bearish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.48)
                continuation_tape_weak = (
                    self._shared_exit_tape_confirm("bearish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.52)
                    or (
                        self._shared_exit_enabled("confirm_with_close_position", True)
                        and close_pos >= float(self._shared_exit_value("bearish_close_position_loose_min", 0.60))
                        and ((not self._shared_exit_enabled("confirm_with_ema9", True)) or close > ema9)
                        and ((not self._shared_exit_enabled("confirm_with_vwap", True)) or close > vwap)
                    )
                )
                if strong_opposing and (reversal_tape_weak or continuation_tape_weak):
                    opposing = opposing_reversal + [p for p in opposing_cont if p not in opposing_reversal]
                    return True, f"chart_pattern_exit:{'+'.join(opposing)}"

        # Candle-pattern exit — mirrors the chart-pattern block above but
        # keyed on the candle context (detect_candle_context). That context
        # is @lru_cache'd AND cached per-strategy in self._candle_context_cache,
        # so this is free when the trigger frame has already been analyzed
        # for entry/scoring on the same cycle. Opt-in via
        # shared_exit.use_candle_pattern_exit; threshold is
        # candles.opposing_net_score_threshold (0.70 = "solid" tier default).
        if self._shared_exit_enabled("use_candle_pattern_exit", False):
            candle_ctx = self._candle_context(frame)
            threshold = float(self._candles_setting("opposing_net_score_threshold", 0.70))
            if direction == "bullish":
                opposing_net = float(candle_ctx.get("bearish_candle_net_score", 0.0) or 0.0)
                opposing_matches = list(candle_ctx.get("matched_bearish_candles", []) or [])
                tape_weak = self._shared_exit_tape_confirm(
                    "bullish", close=close, ema9=ema9, ema20=ema20, vwap=vwap,
                    close_pos=close_pos, close_pos_threshold=0.48,
                )
            else:
                opposing_net = float(candle_ctx.get("bullish_candle_net_score", 0.0) or 0.0)
                opposing_matches = list(candle_ctx.get("matched_bullish_candles", []) or [])
                tape_weak = self._shared_exit_tape_confirm(
                    "bearish", close=close, ema9=ema9, ema20=ema20, vwap=vwap,
                    close_pos=close_pos, close_pos_threshold=0.52,
                )
            if opposing_net >= threshold and tape_weak and opposing_matches:
                joined = "+".join(sorted(opposing_matches)[:3])
                return True, f"candle_pattern_exit:{joined}"

        ms_ctx = self._structure_context(frame, "1m")
        # Grace-window gate: a minor EQL/LL pivot forming in the first few
        # minutes after entry is noise, not reversal — session 2026-04-17
        # was 1W / 12T on structure exits (net -$354). Suppress the bias-
        # based structure exits (structure_bearish_exit:EQL/LL/HL,
        # structure_bullish_exit:HH/LH) during the grace window AND until
        # a minimum number of new pivots have formed post-entry. CHoCH
        # exits (true trend change) still fire — they are genuine reversal
        # signals, not minor pivots.
        try:
            hold_minutes = max(0.0, (now_et() - position.entry_time).total_seconds() / 60.0)
        except Exception:
            hold_minutes = 0.0
        grace_minutes = int(self._support_resistance_setting("structure_exit_grace_minutes", 10))
        min_post_entry_pivots = int(self._support_resistance_setting("structure_exit_min_post_entry_pivots", 2))
        pivot_count_now = int(getattr(ms_ctx, "pivot_count", 0) or 0)
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        # ms1m_pivot_count is stamped into signal.metadata at entry-signal
        # build time via _structure_lists(..., prefix="ms1m") and then frozen
        # onto position.metadata — it's never overwritten during management,
        # so reading it here yields the entry-time pivot count.
        pivot_count_at_entry = int(meta.get("ms1m_pivot_count", pivot_count_now) or 0)
        post_entry_pivots = max(0, pivot_count_now - pivot_count_at_entry)
        # ORB-entry grace extends the suppression of non-CHoCH structure
        # exits for positions entered during the ORB window; OR'd with the
        # existing time/pivot gates so the stricter of the two wins.
        structure_exit_gated = hold_minutes < grace_minutes or post_entry_pivots < min_post_entry_pivots or orb_grace_active
        if self._shared_exit_enabled("use_structure_exit", True) and bool(self._support_resistance_setting("structure_enabled", True)):
            if direction == "bullish":
                if bool(getattr(ms_ctx, "choch_down", False)) and self._structure_event_recent(getattr(ms_ctx, "choch_down_age_bars", None)) and self._shared_exit_tape_confirm("bullish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.48):
                    return True, f"structure_choch_down_exit:{getattr(ms_ctx, 'choch_down_age_bars', 'na')}"
                if not structure_exit_gated and getattr(ms_ctx, "bias", "neutral") == "bearish" and self._shared_exit_tape_confirm("bullish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.42):
                    return True, f"structure_bearish_exit:{getattr(ms_ctx, 'last_low_label', 'na')}"
            else:
                if bool(getattr(ms_ctx, "choch_up", False)) and self._structure_event_recent(getattr(ms_ctx, "choch_up_age_bars", None)) and self._shared_exit_tape_confirm("bearish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.52):
                    return True, f"structure_choch_up_exit:{getattr(ms_ctx, 'choch_up_age_bars', 'na')}"
                if not structure_exit_gated and getattr(ms_ctx, "bias", "neutral") == "bullish" and self._shared_exit_tape_confirm("bearish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.58):
                    return True, f"structure_bullish_exit:{getattr(ms_ctx, 'last_high_label', 'na')}"

        triggered, reason = self._technical_exit_signal(direction, frame, close, ema9, ema20, vwap, close_pos, position)
        if triggered:
            return True, reason

        if self._shared_exit_enabled("use_sr_loss_exit", True) and bool(self._support_resistance_setting("enabled", True)):
            sr_ctx = self._sr_context(symbol, frame, data)
            level_buffer = float(sr_ctx.level_buffer or 0.0)
            entry_price = float(position.entry_price)
            if direction == "bullish":
                # Only fire on a CONFIRMED break event from the SR engine
                # (broken_support), not a positional proximity check. Also
                # require the level to be BELOW entry — broken_support is
                # session-scoped and may persist from a pre-entry break,
                # which would otherwise trigger the exit on the first
                # management cycle after entry (same-bar phantom exit bug).
                support_level = sr_ctx.broken_support
                if support_level is not None:
                    support_price = float(support_level.price)
                    if support_price < entry_price and close <= support_price - level_buffer and self._shared_exit_tape_confirm("bullish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.45):
                        return True, f"support_break_exit:{support_price:.4f}"
            if direction == "bearish":
                # Mirror of the bullish branch above: require an actual
                # broken_resistance event AND that the level sat above
                # the short entry (a ceiling that we bet would hold).
                resistance_level = sr_ctx.broken_resistance
                if resistance_level is not None:
                    resistance_price = float(resistance_level.price)
                    if resistance_price > entry_price and close >= resistance_price + level_buffer and self._shared_exit_tape_confirm("bearish", close=close, ema9=ema9, ema20=ema20, vwap=vwap, close_pos=close_pos, close_pos_threshold=0.55):
                        return True, f"resistance_break_exit:{resistance_price:.4f}"
        return False, "hold"

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
