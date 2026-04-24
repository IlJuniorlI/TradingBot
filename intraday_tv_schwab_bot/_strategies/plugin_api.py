# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class StrategyManifest:
    name: str
    strategy_module: str
    screener_module: str
    strategy_class: str
    screener_class: str
    entry_windows: list[tuple[str, str]]
    management_windows: list[tuple[str, str]]
    screener_windows: list[tuple[str, str]]
    params: dict[str, Any] = field(default_factory=dict)
    plugin_type: str = "stock"
    capabilities: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    manifest_path: str | None = None
