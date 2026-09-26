# SPDX-License-Identifier: MIT
"""Import a plugin's strategy / screener class on demand and build it.

Manifest discovery, validation and lookup live in ``catalogue``, which
imports no plugin module.
"""
from __future__ import annotations

from functools import lru_cache
from importlib import import_module
from typing import TYPE_CHECKING

from .catalogue import plugin_key, get_plugin
from .screener_base import BaseStrategyScreener
from .strategy_base import BaseStrategy

if TYPE_CHECKING:
    from ..config import BotConfig


@lru_cache(maxsize=None)
def _load_strategy_class(name: str) -> type[BaseStrategy]:
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
    declared_name = plugin_key(getattr(strategy_cls, "strategy_name", ""))
    if declared_name != manifest.name:
        raise TypeError(
            f"{manifest.strategy_module}.{manifest.strategy_class}.strategy_name must be {manifest.name!r}, got {declared_name!r}"
        )
    return strategy_cls


@lru_cache(maxsize=None)
def _load_screener_class(name: str) -> type[BaseStrategyScreener]:
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
    declared_name = plugin_key(getattr(screener_cls, "strategy_name", ""))
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
