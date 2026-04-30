# SPDX-License-Identifier: MIT
from __future__ import annotations

from typing import TYPE_CHECKING

from .helpers import insufficient_bars_reason
from .plugin_api import StrategyManifest
from .registry import (
    build_screener,
    build_strategy,
    default_strategy_name,
    get_plugin,
    get_plugins,
    is_option_strategy,
    normalize_strategy_name,
    normalize_strategy_params,
    option_strategy_names,
    plugin_names,
)


if TYPE_CHECKING:
    from .screener_base import BaseStrategyScreener
    from .strategy_base import BaseStrategy

__all__ = [
    "BaseStrategy",
    "BaseStrategyScreener",
    "StrategyManifest",
    "get_plugins",
    "get_plugin",
    "plugin_names",
    "normalize_strategy_name",
    "normalize_strategy_params",
    "default_strategy_name",
    "option_strategy_names",
    "is_option_strategy",
    "build_strategy",
    "build_screener",
    "insufficient_bars_reason",
]


def __getattr__(name: str):
    if name == "BaseStrategy":
        from .strategy_base import BaseStrategy

        return BaseStrategy
    if name == "BaseStrategyScreener":
        from .screener_base import BaseStrategyScreener

        return BaseStrategyScreener
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
