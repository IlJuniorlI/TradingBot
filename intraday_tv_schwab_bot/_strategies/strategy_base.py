# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from typing import TYPE_CHECKING, Any, ClassVar

import pandas as pd

from ..numeric import safe_float
from ..reasons import reason_with_values
from ..event_blackouts import EventBlackoutCalendar
from .shared_entry import SharedEntryPolicy
from .shared_exit import ExitTape, SharedExitPolicy
from .catalogue import get_plugin
from .contexts import ContextBuildersMixin
from ..models import Candidate, ExitDecision, Position, Side, Signal
from ..bars import bar_close_position, bar_wick_fractions
from ..symbols import normalize_symbol_list, normalize_symbol_list_details
from ..htf_levels import HTFContext
from ..indicators import htf_ema_spans, last_bar_atr
from ..sessions import equity_session_state, is_hhmm
from .. import sessions

if TYPE_CHECKING:
    from ..config import BotConfig

# The reason heads BaseStrategy._entry_exhaustion_reasons (the anti-chase
# checks) emits, per side; the wick check is side-specific. A strategy whose
# retest may clear them builds its deferrable set from this, so a check
# renamed or added here reaches every such set.
EXHAUSTION_REASONS: dict[Side, frozenset[str]] = {
    Side.LONG: frozenset({
        "too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "upper_wick_rejection", "expansion_bar_too_large",
    }),
    Side.SHORT: frozenset({
        "too_extended_from_vwap_atr", "too_extended_from_ema9_atr", "lower_wick_rejection", "expansion_bar_too_large",
    }),
}


class BaseStrategy(ContextBuildersMixin):
    strategy_name: str | None = None

    # Class-level flag for the adaptive_ladder trade-management mode.
    # Set to False by strategies that should NEVER run ladder mode (e.g.
    # options strategies). When config.risk.trade_management_mode is
    # "adaptive_ladder" and this flag is False, the engine logs a one-time
    # warning at startup so the user knows ladder mechanics are inactive.
    supports_adaptive_ladder: bool = True

    # The params this strategy reads as HH:MM times. ``__init__`` checks each
    # one the params carry, so a blank or malformed value fails at startup,
    # naming the key, instead of at its first read -- for a window edge like
    # ``afternoon_start_time``, mid-session. An absent key is not checked:
    # its reader falls back to a literal default.
    time_params: ClassVar[tuple[str, ...]] = ()

    # The shared exit families belong to shared_exit.SharedExitPolicy
    # (self.exit_policy, which the position manager calls); a strategy adds
    # its own exits through strategy_exit_signal. Until 2026-09-24 the
    # pipeline was a BaseStrategy method any subclass could override, and
    # the peer family's override silently dropped the time stop and five
    # exit families (see shared_exit.py). The entry side is the same: the
    # shared entry stage is shared_entry.SharedEntryPolicy
    # (self.entry_policy), the gatekeeper ranks with its rank_key, and the
    # knob-rewriting hook strategy_logic_default is gone. Defining any of
    # these names now fails at import, so an out-of-tree plugin cannot
    # quietly opt out of the global knobs.
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
        # Looked up on the class, not in its own namespace: a strategy built
        # from mixins (top_tier_adaptive's engine) must not carry one in a
        # mixin either. BaseStrategy and ContextBuildersMixin define none of
        # them.
        for name, why in BaseStrategy._RESERVED_NAMES.items():
            if hasattr(cls, name):
                raise TypeError(f"{cls.__name__} defines {name}(); {why}")

    @classmethod
    def normalize_params(cls, params: dict[str, Any]) -> dict[str, Any]:
        return dict(params or {})

    def _manifest_capabilities(self) -> dict[str, Any]:
        return self._manifest.capabilities

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
            return bool(checker())
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
                return normalize_symbol_list(self.params.get(key))
            return []
        if token.startswith("options."):
            if not self._options_capability_enabled():
                return []
            optcfg = getattr(self.config, "options", None)
            if token == "options.underlyings":
                return normalize_symbol_list(getattr(optcfg, "underlyings", []))
            if token == "options.confirmation_symbols":
                values = getattr(optcfg, "confirmation_symbols", {})
                if isinstance(values, dict):
                    values = values.values()
                return normalize_symbol_list(values)
            if token == "options.volatility_symbol":
                return normalize_symbol_list([getattr(optcfg, "volatility_symbol", "")])
            return []
        if token == "pairs.symbols":
            return normalize_symbol_list(getattr(pair, "symbol", "") for pair in (getattr(self, "pairs", None) or []))
        if token == "pairs.references":
            return normalize_symbol_list(getattr(pair, "reference", "") for pair in (getattr(self, "pairs", None) or []))
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
        for key in self.time_params:
            if key in self.params and not is_hhmm(self.params[key]):
                raise ValueError(
                    f"strategies.{self.strategy_name}.params.{key} must be an HH:MM time, got {self.params[key]!r}"
                )
        self._manifest = get_plugin(config.strategy)
        # Scheduled-event calendar (macro windows + per-symbol earnings),
        # shared by every strategy. Lazily re-reads its YAML sources when
        # their mtime changes, so it is safe to build once here.
        self._event_calendar = EventBlackoutCalendar(config)
        self._entry_decisions: dict[str, dict[str, Any]] = {}
        self._build_failures: dict[tuple[str, str], dict[str, Any]] = {}
        # The context builders' per-cycle caches and their locks
        # (contexts.ContextBuildersMixin).
        super().__init__()
        # The shared entry stage: the only reader of config.shared_entry
        # (see shared_entry.py). Built last -- it reads the manifest.
        self.entry_policy = SharedEntryPolicy(self)
        # Every shared_exit knob, for every strategy: the only reader of
        # config.shared_exit (see shared_exit.py). The position manager
        # decides each open position's exit through it.
        self.exit_policy = SharedExitPolicy(config, self)

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
            normalized, skipped = normalize_symbol_list_details(raw_values)
            trace[label] = {
                "symbols": normalized,
                "skipped": skipped,
            }
        return trace

    @staticmethod
    def _position_strategy_matches(position: Position, strategy_names: list[str] | None) -> bool:
        """True if ``position.strategy`` matches any of the configured
        strategy names. None or empty strategy_names means 'match anything'."""
        if not strategy_names:
            return True
        current = str(getattr(position, "strategy", "") or "").strip().lower()
        return current in {str(name).strip().lower() for name in strategy_names if str(name).strip()}

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
                if not self._position_strategy_matches(position, strategy_names):
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
        return normalize_symbol_list(raw_values)

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
        return {token for token in normalize_symbol_list(symbols)}

    def dashboard_tradable_symbols(self) -> list[str]:
        source = self._capability("dashboard.tradable_symbols_source", None)
        if source is not None:
            return self._symbols_from_capability_source(source) or []
        if not isinstance(self.params, dict):
            return []
        raw_symbols = self.params.get("tradable")
        if raw_symbols is None:
            raw_symbols = self.params.get("symbols")
        return normalize_symbol_list(raw_symbols)

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
            symbols.update(normalize_symbol_list(raw_index))
        sector_map = self.params.get("sector_index_map")
        if isinstance(sector_map, dict):
            for tickers in sector_map.values():
                symbols.update(normalize_symbol_list(tickers))
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
            return max(1, int(self._capability("dashboard.candidate_limit", default_limit)))
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
            "timeframe_minutes": max(1, self.htf_minutes()),
            "lookback_days": max(1, self.htf_lookback_days()),
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

    @staticmethod
    def _dashboard_zone_width_from_policy(policy: dict[str, Any], close: float, atr: float) -> float | None:
        """Resolve a numeric zone-width given a dashboard zone-width policy
        dict and the current price + ATR. Supports four ``mode`` values:
        ``fixed`` (use ``value``/``fixed_width`` directly), ``atr_mult``
        (multiply ATR by ``value``/``atr_mult``), ``pct_of_price`` /
        ``price_pct`` (multiply close by the configured percentage), and
        ``max_of`` (take max of any of the above components present).
        Returns None for unrecognized modes or invalid values; otherwise
        floors at ``min_width`` (default 0.01)."""
        mode = str(policy.get("mode") or "").strip().lower()
        min_width = float(policy.get("min_width", 0.01) or 0.01)
        computed_width: float | None
        if mode == "fixed":
            value = policy.get("value", policy.get("fixed_width"))
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            computed_width = float(value)
        elif mode == "atr_mult":
            value = policy.get("value", policy.get("atr_mult"))
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            computed_width = float(atr) * float(value)
        elif mode in {"pct_of_price", "price_pct"}:
            value = policy.get("value", policy.get("pct_of_price", policy.get("price_pct")))
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            computed_width = float(close) * float(value)
        elif mode == "max_of":
            parts: list[float] = []
            fixed_width = policy.get("fixed_width")
            atr_mult = policy.get("atr_mult")
            pct_of_price = policy.get("pct_of_price", policy.get("price_pct"))
            if isinstance(fixed_width, (int, float)) and not isinstance(fixed_width, bool):
                parts.append(float(fixed_width))
            if isinstance(atr_mult, (int, float)) and not isinstance(atr_mult, bool):
                parts.append(float(atr) * float(atr_mult))
            if isinstance(pct_of_price, (int, float)) and not isinstance(pct_of_price, bool):
                parts.append(float(close) * float(pct_of_price))
            if not parts:
                return None
            computed_width = max(parts)
        else:
            return None
        return max(float(computed_width), float(min_width), 0.01)

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
            return self._dashboard_zone_width_from_policy(policy, close=float(close), atr=float(atr))
        return None

    def dashboard_overlay_candidates(self, side: Side, close: float, ltf: pd.DataFrame, htf: HTFContext) -> list[dict[str, Any]] | None:
        return None

    def _manifest_required_history_bars(self) -> int | None:
        raw = self._capability("history.required_bars", None)
        if raw is None:
            return None
        return max(0, int(raw))

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        return max(0, int(self.params.get("min_bars", 0) or 0))

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
        return max(window.end for window in windows)

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
        now_dt = sessions.now_et()
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
        atr = last_bar_atr(frame, float(close), floor_pct=0.0005)
        max_vwap_ext_atr = max(0.1, float(self.params.get("max_entry_vwap_extension_atr", 0.95)))
        max_ema9_ext_atr = max(0.1, float(self.params.get("max_entry_ema9_extension_atr", 0.75)))
        max_bar_range_atr = max(0.25, float(self.params.get("max_entry_bar_range_atr", 1.7)))
        max_upper_wick_frac = min(0.95, max(0.05, float(self.params.get("max_entry_upper_wick_frac", 0.30))))
        max_lower_wick_frac = min(0.95, max(0.05, float(self.params.get("max_entry_lower_wick_frac", 0.30))))
        wick_close_pos_guard = min(0.95, max(0.05, float(self.params.get("entry_wick_close_position_guard", 0.62))))
        upper_wick_frac, lower_wick_frac, _, bar_range = bar_wick_fractions(frame)
        close_pos = bar_close_position(frame)
        reasons: list[str] = []
        if side == Side.LONG:
            vwap_ext_atr = max(0.0, float(close) - float(vwap)) / atr if float(vwap) > 0 else 0.0
            ema9_ext_atr = max(0.0, float(close) - float(ema9)) / atr if float(ema9) > 0 else 0.0
            if vwap_ext_atr > max_vwap_ext_atr:
                reasons.append(reason_with_values("too_extended_from_vwap_atr", current=vwap_ext_atr, required=max_vwap_ext_atr, op="<=", digits=4))
            if ema9_ext_atr > max_ema9_ext_atr:
                reasons.append(reason_with_values("too_extended_from_ema9_atr", current=ema9_ext_atr, required=max_ema9_ext_atr, op="<=", digits=4))
            if upper_wick_frac > max_upper_wick_frac and close_pos < wick_close_pos_guard:
                reasons.append(reason_with_values("upper_wick_rejection", current=upper_wick_frac, required=max_upper_wick_frac, op="<=", digits=4, extras={"close_position": (close_pos, ">=", wick_close_pos_guard)}))
            if (bar_range / atr) > max_bar_range_atr and (vwap_ext_atr > max_vwap_ext_atr * 0.75 or ema9_ext_atr > max_ema9_ext_atr * 0.75):
                reasons.append(reason_with_values("expansion_bar_too_large", current=(bar_range / atr), required=max_bar_range_atr, op="<=", digits=4))
        else:
            vwap_ext_atr = max(0.0, float(vwap) - float(close)) / atr if float(vwap) > 0 else 0.0
            ema9_ext_atr = max(0.0, float(ema9) - float(close)) / atr if float(ema9) > 0 else 0.0
            if vwap_ext_atr > max_vwap_ext_atr:
                reasons.append(reason_with_values("too_extended_from_vwap_atr", current=vwap_ext_atr, required=max_vwap_ext_atr, op="<=", digits=4))
            if ema9_ext_atr > max_ema9_ext_atr:
                reasons.append(reason_with_values("too_extended_from_ema9_atr", current=ema9_ext_atr, required=max_ema9_ext_atr, op="<=", digits=4))
            if lower_wick_frac > max_lower_wick_frac and close_pos > (1.0 - wick_close_pos_guard):
                reasons.append(reason_with_values("lower_wick_rejection", current=lower_wick_frac, required=max_lower_wick_frac, op="<=", digits=4, extras={"close_position": (close_pos, "<=", 1.0 - wick_close_pos_guard)}))
            if (bar_range / atr) > max_bar_range_atr and (vwap_ext_atr > max_vwap_ext_atr * 0.75 or ema9_ext_atr > max_ema9_ext_atr * 0.75):
                reasons.append(reason_with_values("expansion_bar_too_large", current=(bar_range / atr), required=max_bar_range_atr, op="<=", digits=4))
        return reasons

    def _structure_event_recent(self, age_bars: int | None, *, htf: bool = False) -> bool:
        """Is a BOS/CHoCH ``age_bars`` old still fresh? ``age_bars`` is in the
        bars of the structure it came from, so pass ``htf=True`` for the S/R
        context's ``market_structure`` -- its ages are HTF bars and must be
        judged by the HTF window, not the LTF one."""
        lookback = (
            self.config.support_resistance.htf_structure_event_lookback() if htf
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
            reward = abs(float(target) - float(close))
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
        base_trail_pct = self.config.risk.trailing_stop_pct  # a finite number >= 0 or null, checked at load
        runner_trail_pct = safe_float(self.params.get("adaptive_runner_trail_pct"))
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
        return float(self.params.get(name, default) or default)

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

        Each rung dict carries price, kind, zone_width, lower, upper (the
        zone around the price) and rr. The first rung is the signal's target.
        The adaptive ladder's touch hold (TradeManager.manage_adaptive_ladder,
        ``shared_exit.adaptive_ladder_touch_hold``) reads the price, zone
        width and kind to promote past a rung; with the hold off the first
        rung is a plain take-profit. An empty list disables laddering — the
        signal keeps its originally-computed target and behaves as a
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
        """Produce the ladder metadata a laddered position carries.

        TradeManager.update_position and manage_adaptive_ladder read
        ladder_management_enabled, ladder_rungs and ladder_active_index; a
        touch-hold promotion rewrites ladder_active_index, the
        ladder_defense_* keys and ladder_final_rung_cleared. The key-levels
        ladder defence reads ladder_defense_price / _zone_width, the defended
        level (the entry stop here). Subclasses that know a better defense
        level (e.g. HTF peer level) can add a post-process step after
        calling this helper.
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
