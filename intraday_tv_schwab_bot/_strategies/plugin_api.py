# SPDX-License-Identifier: MIT
"""The plugin API's vocabulary: the manifest a plugin ships and the closed
sets its declarations name."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The shared entry stage's P3 vetoes (shared_entry.SharedEntryPolicy), in
# evaluation (and reporting) order. A manifest exempts a style from any of
# them: capabilities.shared_entry.exemptions {style: [gate]}, which the
# catalogue validates against this closed set.
VETO_GATES: tuple[str, ...] = ("structure", "sr", "broken_level", "chart", "dual_divergence", "candle")


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
