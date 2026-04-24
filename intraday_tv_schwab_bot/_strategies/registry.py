# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from functools import lru_cache
from importlib import import_module
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .plugin_api import StrategyManifest

if TYPE_CHECKING:
    from ..config import BotConfig
    from .strategy_base import BaseStrategy
    from .screener_base import BaseStrategyScreener

_MANIFEST_FILENAME = "manifest.json"
_STRATEGY_MODULE_NAME = "strategy"
_SCREENER_MODULE_NAME = "screener"
_PACKAGE_NAME = "intraday_tv_schwab_bot._strategies"
_VALID_PLUGIN_TYPES = {"stock", "option"}
_CURRENT_MANIFEST_SCHEMA_VERSION = 1
_VALID_MANIFEST_TOP_LEVEL_KEYS = {
    "schema_version",
    "name",
    "type",
    "strategy_module",
    "strategy_class",
    "screener_module",
    "screener_class",
    "entry_windows",
    "management_windows",
    "screener_windows",
    "params",
    "capabilities",
}


_VALID_TRADABLE_SYMBOL_SOURCES = {
    "none",
    "params.tradable",
    "params.symbols",
    "options.underlyings",
    "pairs.symbols",
}
_VALID_RESTORE_SYMBOL_SOURCES = _VALID_TRADABLE_SYMBOL_SOURCES | {"all", "dashboard_tradable_symbols"}
_VALID_WATCHLIST_SOURCE_TOKENS = {
    "active_watchlist",
    "candidates",
    "dashboard_tradable_symbols",
    "options.confirmation_symbols",
    "options.underlyings",
    "options.volatility_symbol",
    "pairs.references",
    "pairs.symbols",
    "params.peers",
    "params.symbols",
    "params.tradable",
    "positions.reference_symbols",
    "positions.symbols",
    "positions.underlyings_or_symbols",
}
_VALID_WATCHLIST_OBJECT_SOURCES = {
    "params.keys",
    "params.keys_if_true",
    "positions.metadata",
    "positions.metadata_list",
}


def _validate_non_empty_string_list(value: object, *, field_name: str, manifest_path: Path) -> list[str]:
    if not isinstance(value, list) or any(not str(item).strip() for item in value):
        raise TypeError(f"{manifest_path}: '{field_name}' must be a list of non-empty strings")
    return [str(item).strip() for item in value]


def _validate_watchlist_source(source: object, *, field_name: str, manifest_path: Path) -> None:
    if isinstance(source, str):
        token = source.strip()
        if token not in _VALID_WATCHLIST_SOURCE_TOKENS:
            raise ValueError(
                f"{manifest_path}: '{field_name}' must use one of {sorted(_VALID_WATCHLIST_SOURCE_TOKENS)} "
                f"or an object source from {sorted(_VALID_WATCHLIST_OBJECT_SOURCES)}"
            )
        if token == "active_watchlist" and ".active_sources[" in field_name:
            raise ValueError(f"{manifest_path}: '{field_name}' cannot use 'active_watchlist' inside capabilities.watchlist.active_sources")
        return
    if not isinstance(source, dict):
        raise TypeError(f"{manifest_path}: '{field_name}' entries must be strings or objects")
    kind = str(source.get('source') or '').strip()
    if kind not in _VALID_WATCHLIST_OBJECT_SOURCES:
        raise ValueError(
            f"{manifest_path}: '{field_name}.source' must be one of {sorted(_VALID_WATCHLIST_OBJECT_SOURCES)}"
        )
    if kind in {'positions.metadata', 'positions.metadata_list'}:
        key = str(source.get('key') or '').strip()
        if not key:
            raise ValueError(f"{manifest_path}: '{field_name}.key' must be non-empty for source={kind!r}")
        strategy_names = source.get('strategy_names')
        if strategy_names is not None:
            _validate_non_empty_string_list(strategy_names, field_name=f"{field_name}.strategy_names", manifest_path=manifest_path)
        return
    keys = _validate_non_empty_string_list(source.get('keys'), field_name=f"{field_name}.keys", manifest_path=manifest_path)
    if kind == 'params.keys_if_true':
        flag = str(source.get('flag') or '').strip()
        if not flag:
            raise ValueError(f"{manifest_path}: '{field_name}.flag' must be non-empty for source='params.keys_if_true'")
        return
    if kind == 'params.keys' and not keys:
        raise ValueError(f"{manifest_path}: '{field_name}.keys' must not be empty")


def _coerce_capabilities(raw: object, *, manifest_path: Path) -> dict[str, object]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TypeError(f"{manifest_path}: 'capabilities' must be a JSON object")
    out = deepcopy(raw)
    dashboard = out.get("dashboard")
    if dashboard is not None:
        if not isinstance(dashboard, dict):
            raise TypeError(f"{manifest_path}: 'capabilities.dashboard' must be an object")
        tradable_source = dashboard.get("tradable_symbols_source")
        if tradable_source is not None:
            token = str(tradable_source).strip()
            if token not in _VALID_TRADABLE_SYMBOL_SOURCES:
                raise ValueError(
                    f"{manifest_path}: 'capabilities.dashboard.tradable_symbols_source' must be one of {sorted(_VALID_TRADABLE_SYMBOL_SOURCES)}"
                )
        candidate_limit_mode = dashboard.get("candidate_limit_mode")
        if candidate_limit_mode is not None:
            token = str(candidate_limit_mode).strip()
            if token not in {"default", "tradable_count", "fixed"}:
                raise ValueError(
                    f"{manifest_path}: 'capabilities.dashboard.candidate_limit_mode' must be one of ['default', 'fixed', 'tradable_count']"
                )
            if token == "fixed" and not isinstance(dashboard.get("candidate_limit"), int):
                raise TypeError(f"{manifest_path}: 'capabilities.dashboard.candidate_limit' must be an integer when candidate_limit_mode='fixed'")
        fallback = dashboard.get("allow_generic_level_fallback")
        if fallback is not None and not isinstance(fallback, bool):
            raise TypeError(f"{manifest_path}: 'capabilities.dashboard.allow_generic_level_fallback' must be a boolean")
        level_context = dashboard.get("level_context")
        if level_context is not None:
            if not isinstance(level_context, dict):
                raise TypeError(f"{manifest_path}: 'capabilities.dashboard.level_context' must be an object")
            for key, value in level_context.items():
                if key in {"timeframe_minutes", "lookback_days", "pivot_span", "max_levels_per_side", "ema_fast_span", "ema_slow_span", "refresh_seconds", "trigger_timeframe_minutes"}:
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise TypeError(f"{manifest_path}: 'capabilities.dashboard.level_context.{key}' must be an integer")
                elif key in {"atr_tolerance_mult", "pct_tolerance", "stop_buffer_atr_mult", "min_level_score", "level_round_number_tolerance_pct", "base_zone_atr_mult", "base_zone_pct"}:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise TypeError(f"{manifest_path}: 'capabilities.dashboard.level_context.{key}' must be numeric")
                else:
                    raise ValueError(f"{manifest_path}: unsupported dashboard level_context key '{key}'")
        candidate_labels = dashboard.get("candidate_labels")
        if candidate_labels is not None:
            if not isinstance(candidate_labels, dict):
                raise TypeError(f"{manifest_path}: 'capabilities.dashboard.candidate_labels' must be an object")
            for key, value in candidate_labels.items():
                if not str(key).strip() or not isinstance(value, str) or not value.strip():
                    raise TypeError(f"{manifest_path}: 'capabilities.dashboard.candidate_labels' entries must map non-empty strings to non-empty labels")
        candidate_sources = dashboard.get("candidate_sources")
        if candidate_sources is not None:
            if not isinstance(candidate_sources, dict):
                raise TypeError(f"{manifest_path}: 'capabilities.dashboard.candidate_sources' must be an object")
            for key, value in candidate_sources.items():
                if not str(key).strip():
                    raise TypeError(f"{manifest_path}: 'capabilities.dashboard.candidate_sources' keys must be non-empty strings")
                if isinstance(value, str):
                    if not value.strip():
                        raise TypeError(f"{manifest_path}: 'capabilities.dashboard.candidate_sources.{key}' must not be empty")
                elif isinstance(value, list):
                    _validate_non_empty_string_list(value, field_name=f"capabilities.dashboard.candidate_sources.{key}", manifest_path=manifest_path)
                else:
                    raise TypeError(f"{manifest_path}: 'capabilities.dashboard.candidate_sources.{key}' must be a string or list of strings")
        zone_width = dashboard.get("zone_width")
        if zone_width is not None:
            def _validate_zone_width_policy(policy: object, *, field_name: str) -> None:
                if not isinstance(policy, dict):
                    raise TypeError(f"{manifest_path}: '{field_name}' must be an object")
                mode = str(policy.get("mode") or "").strip()
                if mode not in {"fixed", "atr_mult", "pct_of_price", "price_pct", "max_of"}:
                    raise ValueError(f"{manifest_path}: '{field_name}.mode' must be one of ['atr_mult', 'fixed', 'max_of', 'pct_of_price']")
                numeric_keys = {"value", "fixed_width", "atr_mult", "pct_of_price", "price_pct", "min_width"}
                for key2, value2 in policy.items():
                    if key2 == "mode":
                        continue
                    if key2 == "kind_overrides":
                        if not isinstance(value2, dict):
                            raise TypeError(f"{manifest_path}: '{field_name}.kind_overrides' must be an object")
                        for kind_key, override in value2.items():
                            if not str(kind_key).strip():
                                raise TypeError(f"{manifest_path}: '{field_name}.kind_overrides' keys must be non-empty strings")
                            _validate_zone_width_policy(override, field_name=f"{field_name}.kind_overrides.{kind_key}")
                        continue
                    if key2 not in numeric_keys:
                        raise ValueError(f"{manifest_path}: unsupported dashboard zone_width key '{key2}' in {field_name}")
                    if isinstance(value2, bool) or not isinstance(value2, (int, float)):
                        raise TypeError(f"{manifest_path}: '{field_name}.{key2}' must be numeric")
            _validate_zone_width_policy(zone_width, field_name="capabilities.dashboard.zone_width")
    restore = out.get("startup_restore")
    if restore is not None:
        if not isinstance(restore, dict):
            raise TypeError(f"{manifest_path}: 'capabilities.startup_restore' must be an object")
        eligible_source = restore.get("eligible_symbols_source")
        if eligible_source is not None:
            token = str(eligible_source).strip()
            if token not in _VALID_RESTORE_SYMBOL_SOURCES:
                raise ValueError(
                    f"{manifest_path}: 'capabilities.startup_restore.eligible_symbols_source' must be one of {sorted(_VALID_RESTORE_SYMBOL_SOURCES)}"
                )
        hybrid = restore.get("require_hybrid_metadata")
        if hybrid is not None and not isinstance(hybrid, bool):
            raise TypeError(f"{manifest_path}: 'capabilities.startup_restore.require_hybrid_metadata' must be a boolean")
    signal_priority = out.get("signal_priority")
    if signal_priority is not None:
        if not isinstance(signal_priority, dict):
            raise TypeError(f"{manifest_path}: 'capabilities.signal_priority' must be an object")
        metadata_fields = signal_priority.get("metadata_fields")
        if metadata_fields is not None:
            _validate_non_empty_string_list(
                metadata_fields,
                field_name="capabilities.signal_priority.metadata_fields",
                manifest_path=manifest_path,
            )
    history = out.get("history")
    if history is not None:
        if not isinstance(history, dict):
            raise TypeError(f"{manifest_path}: 'capabilities.history' must be an object")
        required_bars = history.get("required_bars")
        if required_bars is not None and (isinstance(required_bars, bool) or not isinstance(required_bars, int)):
            raise TypeError(f"{manifest_path}: 'capabilities.history.required_bars' must be an integer")
    watchlist = out.get("watchlist")
    if watchlist is not None:
        if not isinstance(watchlist, dict):
            raise TypeError(f"{manifest_path}: 'capabilities.watchlist' must be an object")
        for field in ("active_sources", "quote_sources"):
            sources = watchlist.get(field)
            if sources is None:
                continue
            if not isinstance(sources, list):
                raise TypeError(f"{manifest_path}: 'capabilities.watchlist.{field}' must be a list")
            for idx, source in enumerate(sources):
                _validate_watchlist_source(
                    source,
                    field_name=f"capabilities.watchlist.{field}[{idx}]",
                    manifest_path=manifest_path,
                )
    return out




def _coerce_schema_version(raw: object, *, manifest_path: Path) -> int:
    if raw is None:
        return _CURRENT_MANIFEST_SCHEMA_VERSION
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(f"{manifest_path}: 'schema_version' must be an integer")
    if raw != _CURRENT_MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"{manifest_path}: unsupported 'schema_version' {raw}; expected {_CURRENT_MANIFEST_SCHEMA_VERSION}"
        )
    return int(cast(int, raw))

def _normalize_name(value: str) -> str:
    return str(value or "").strip().lower()


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def _iter_manifest_paths() -> Iterable[Path]:
    package_dir = _package_dir()
    return sorted(
        p / _MANIFEST_FILENAME
        for p in package_dir.iterdir()
        if p.is_dir() and not p.name.startswith("_") and (p / _MANIFEST_FILENAME).is_file()
    )


def _coerce_windows(raw: object, *, field_name: str, manifest_path: Path) -> list[tuple[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TypeError(f"{manifest_path}:{field_name} must be a list of [start, end] windows")
    out: list[tuple[str, str]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, list | tuple) or len(item) != 2:
            raise TypeError(f"{manifest_path}:{field_name}[{idx}] must contain exactly two HH:MM values")
        start = str(item[0]).strip()
        end = str(item[1]).strip()
        if not start or not end:
            raise ValueError(f"{manifest_path}:{field_name}[{idx}] start/end must be non-empty")
        out.append((start, end))
    return out


def _required_manifest_string(raw: dict[str, object], field_name: str, *, manifest_path: Path) -> str:
    if field_name not in raw:
        raise ValueError(f"{manifest_path}: missing required manifest field '{field_name}'")
    value = str(raw.get(field_name) or "").strip()
    if not value:
        raise ValueError(f"{manifest_path}: '{field_name}' must be non-empty")
    return value


def _load_manifest(manifest_path: Path) -> StrategyManifest:
    try:
        raw = json.loads(manifest_path.read_text())
    except Exception as exc:
        raise RuntimeError(f"Failed to read strategy manifest '{manifest_path}': {exc}") from exc
    if not isinstance(raw, dict):
        raise TypeError(f"{manifest_path} must contain a JSON object")
    unknown_keys = sorted(set(raw) - _VALID_MANIFEST_TOP_LEVEL_KEYS)
    if unknown_keys:
        raise ValueError(f"{manifest_path}: unsupported top-level manifest keys: {', '.join(unknown_keys)}")

    plugin_dir = manifest_path.parent
    strategy_py = plugin_dir / f"{_STRATEGY_MODULE_NAME}.py"
    screener_py = plugin_dir / f"{_SCREENER_MODULE_NAME}.py"
    if not strategy_py.exists():
        raise FileNotFoundError(f"{manifest_path} requires a sibling Python module '{strategy_py.name}'")
    if not screener_py.exists():
        raise FileNotFoundError(f"{manifest_path} requires a sibling Python module '{screener_py.name}'")

    schema_version = _coerce_schema_version(raw.get("schema_version"), manifest_path=manifest_path)

    name = _normalize_name(raw.get("name", ""))
    if not name:
        raise ValueError(f"{manifest_path}: 'name' must be non-empty")
    if plugin_dir.name != name:
        raise ValueError(f"{manifest_path}: plugin directory name must match strategy name '{name}'")

    strategy_module = _required_manifest_string(raw, "strategy_module", manifest_path=manifest_path)
    strategy_class = _required_manifest_string(raw, "strategy_class", manifest_path=manifest_path)
    screener_module = _required_manifest_string(raw, "screener_module", manifest_path=manifest_path)
    screener_class = _required_manifest_string(raw, "screener_class", manifest_path=manifest_path)

    plugin_type = _normalize_name(_required_manifest_string(raw, "type", manifest_path=manifest_path))
    if plugin_type not in _VALID_PLUGIN_TYPES:
        raise ValueError(f"{manifest_path}: 'type' must be one of {sorted(_VALID_PLUGIN_TYPES)}")

    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise TypeError(f"{manifest_path}: 'params' must be a JSON object")
    capabilities = _coerce_capabilities(raw.get("capabilities"), manifest_path=manifest_path)

    return StrategyManifest(
        name=name,
        strategy_module=strategy_module,
        screener_module=screener_module,
        strategy_class=strategy_class,
        screener_class=screener_class,
        entry_windows=_coerce_windows(raw.get("entry_windows"), field_name="entry_windows", manifest_path=manifest_path),
        management_windows=_coerce_windows(raw.get("management_windows"), field_name="management_windows", manifest_path=manifest_path),
        screener_windows=_coerce_windows(raw.get("screener_windows"), field_name="screener_windows", manifest_path=manifest_path),
        params=deepcopy(params),
        plugin_type=plugin_type,
        capabilities=deepcopy(capabilities),
        schema_version=schema_version,
        manifest_path=str(manifest_path),
    )


@lru_cache(maxsize=1)
def get_plugins() -> dict[str, StrategyManifest]:
    manifests: dict[str, StrategyManifest] = {}
    for manifest_path in _iter_manifest_paths():
        manifest = _load_manifest(manifest_path)
        if manifest.name in manifests:
            raise ValueError(f"Duplicate strategy plugin name: {manifest.name}")
        manifests[manifest.name] = manifest
    if not manifests:
        raise RuntimeError(f"No strategy manifests were discovered under {_PACKAGE_NAME}")
    return manifests


def plugin_names() -> tuple[str, ...]:
    return tuple(sorted(get_plugins().keys()))


def get_plugin(name: str) -> StrategyManifest:
    normalized = _normalize_name(name)
    plugins = get_plugins()
    if normalized not in plugins:
        available = ", ".join(sorted(plugins))
        raise ValueError(f"Unknown strategy plugin: {name!r}. Available: {available}")
    return plugins[normalized]


def normalize_strategy_name(name: str | None) -> str:
    if name is None or str(name).strip() == "":
        return default_strategy_name()
    return get_plugin(str(name)).name


def default_strategy_name() -> str:
    plugins = get_plugins()
    return "momentum_close" if "momentum_close" in plugins else next(iter(sorted(plugins)))


def option_strategy_names() -> frozenset[str]:
    return frozenset(name for name, manifest in get_plugins().items() if manifest.plugin_type == "option")


def is_option_strategy(name: str | None) -> bool:
    if name is None:
        return False
    return _normalize_name(name) in option_strategy_names()


@lru_cache(maxsize=None)
def _load_strategy_class(name: str) -> type[BaseStrategy]:
    from .strategy_base import BaseStrategy

    manifest = get_plugin(name)
    try:
        module = import_module(manifest.strategy_module)
    except Exception as exc:
        raise RuntimeError(f"Failed to import strategy module '{manifest.strategy_module}': {exc}") from exc
    strategy_cls = getattr(module, manifest.strategy_class, None)
    if not isinstance(strategy_cls, type) or not issubclass(strategy_cls, BaseStrategy):
        raise TypeError(f"{manifest.strategy_module}.{manifest.strategy_class} must inherit BaseStrategy")
    if getattr(strategy_cls, "__module__", None) != manifest.strategy_module:
        raise TypeError(
            f"{manifest.strategy_module}.{manifest.strategy_class} must be defined in {manifest.strategy_module}, not re-exported from {getattr(strategy_cls, '__module__', None)!r}"
        )
    declared_name = _normalize_name(getattr(strategy_cls, "strategy_name", ""))
    if declared_name != manifest.name:
        raise TypeError(
            f"{manifest.strategy_module}.{manifest.strategy_class}.strategy_name must be {manifest.name!r}, got {declared_name!r}"
        )
    return strategy_cls


@lru_cache(maxsize=None)
def _load_screener_class(name: str) -> type[BaseStrategyScreener]:
    from .screener_base import BaseStrategyScreener

    manifest = get_plugin(name)
    try:
        module = import_module(manifest.screener_module)
    except Exception as exc:
        raise RuntimeError(f"Failed to import screener module '{manifest.screener_module}': {exc}") from exc
    screener_cls = getattr(module, manifest.screener_class, None)
    if not isinstance(screener_cls, type) or not issubclass(screener_cls, BaseStrategyScreener):
        raise TypeError(f"{manifest.screener_module}.{manifest.screener_class} must inherit BaseStrategyScreener")
    if getattr(screener_cls, "__module__", None) != manifest.screener_module:
        raise TypeError(
            f"{manifest.screener_module}.{manifest.screener_class} must be defined in {manifest.screener_module}, not re-exported from {getattr(screener_cls, '__module__', None)!r}"
        )
    declared_name = _normalize_name(getattr(screener_cls, "strategy_name", ""))
    if declared_name != manifest.name:
        raise TypeError(
            f"{manifest.screener_module}.{manifest.screener_class}.strategy_name must be {manifest.name!r}, got {declared_name!r}"
        )
    return screener_cls


def normalize_strategy_params(name: str | None, params: dict[str, object] | None) -> dict[str, object]:
    if name is None or str(name).strip() == "":
        return dict(params or {})
    strategy_cls = _load_strategy_class(name)
    normalizer = getattr(strategy_cls, "normalize_params", None)
    if normalizer is None:
        return dict(params or {})
    normalized = normalizer(dict(params or {}))
    if not isinstance(normalized, dict):
        raise TypeError(f"{strategy_cls.__module__}.{strategy_cls.__name__}.normalize_params() must return a dict")
    return normalized


def build_strategy(config: BotConfig) -> BaseStrategy:
    strategy_cls = _load_strategy_class(config.strategy)
    return strategy_cls(config)


def build_screener(client, strategy: str) -> BaseStrategyScreener:
    screener_cls = _load_screener_class(strategy)
    return screener_cls(client)
