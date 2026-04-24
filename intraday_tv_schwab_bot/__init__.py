# SPDX-License-Identifier: MIT
from pathlib import Path as _Path


def _read_version() -> str:
    # version.txt lives at the repo root alongside this package.
    candidate = _Path(__file__).resolve().parent.parent / "version.txt"
    if candidate.exists():
        try:
            return candidate.read_text(encoding="utf-8").strip() or "0.0.0"
        except Exception:
            return "0.0.0"
    return "0.0.0"


__version__ = _read_version()

__all__ = [
    "__version__",
    "audit_logger",
    "broker_positions",
    "candles",
    "chart_patterns",
    "config",
    "cycle_gate",
    "dashboard",
    "dashboard_cache",
    "data_feed",
    "engine",
    "entry_gatekeeper",
    "execution",
    "htf_levels",
    "levels_shared",
    "models",
    "options_mode",
    "paper_account",
    "position_manager",
    "position_metrics",
    "position_store",
    "risk",
    "screener_client",
    "session_report",
    "startup_reconciler",
    "support_resistance",
    "technical_levels",
    "utils",
    "warmup_tracker",
]
