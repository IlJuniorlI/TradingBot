# SPDX-License-Identifier: MIT
"""Engine-side logging primitives, separated from business logic.

Owns:
  - Interval-deduplicated cycle logs (``log_cycle``): suppress noise when the
    same fingerprint recurs within N seconds.
  - Fingerprinted watchlist traces (``log_watchlist_trace``): only emit when
    the per-kind fingerprint changes.
  - Structured JSON event logs (``log_structured``): TRADEFLOW-level events
    that downstream tools (session_report) parse out of the log file.

Historically these were ``IntradayBot`` methods before Phase 2 of the
engine refactor. Extracting to a dedicated class decouples logging state
from the business loop so Phase 5 subsystems (``EntryGatekeeper``,
``PositionManager``, ``StartupReconciler``, ``CycleGate``) all share one
``AuditLogger`` instance and keep dedup/fingerprint state coherent.

Logger name is kept as ``intraday_tv_schwab_bot.engine`` so existing log
routing configs continue to apply.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from .utils import TRADEFLOW_LEVEL

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


def _json_ready(value: Any) -> Any:
    """JSON-safe value normalization. Canonical implementation.

    Also imported by ``engine._reconcile_metadata_signature`` (to build the
    signature payload for reconcile-metadata dedup) and
    ``position_store.ReconcileMetadataStore.save_positions`` (to serialize
    position.metadata before sqlite insert). Previously triplicated across
    those three call sites; consolidated here after Phase 5.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            LOG.debug("Failed to serialize isoformat-capable value", exc_info=True)
    try:
        return float(value)
    except Exception:
        return str(value)


class AuditLogger:
    """Dedup-aware logging facade for the engine cycle.

    Instantiate once per ``IntradayBot`` (with the active strategy name) and
    call methods instead of ``self._log_*``. State (dedup dicts) lives on
    the instance so multiple engine components can share the same facade.
    """

    def __init__(self, strategy_name: str) -> None:
        self._strategy_name = strategy_name
        self._last_cycle_log: dict[str, tuple[str, float]] = {}
        self._last_watchlist_trace_fingerprint: dict[str, str] = {}

    def log_cycle(
        self,
        key: str,
        signature: str,
        message: str,
        *,
        interval: float = 60.0,
        force: bool = False,
        level: int = logging.INFO,
    ) -> None:
        """Log ``message`` only if the signature has changed or ``interval``
        seconds have elapsed since the prior log with this ``key``."""
        now_ts = time.time()
        prior = self._last_cycle_log.get(key)
        if not force and prior is not None and prior[0] == signature and (now_ts - prior[1]) < interval:
            return
        LOG.log(level, message)
        self._last_cycle_log[key] = (signature, now_ts)

    def log_watchlist_trace(
        self,
        kind: str,
        trace: dict[str, dict[str, list[str]]] | None,
    ) -> None:
        """Emit a watchlist trace summary for ``kind`` (``active`` / ``quote``)
        only when the fingerprint changes. Prevents spam when the screener
        returns the same symbol set cycle after cycle."""
        if not trace:
            return
        parts: list[str] = []
        for source, details in trace.items():
            symbols = ",".join(details.get("symbols", [])) or "none"
            skipped_values = details.get("skipped", []) or []
            skipped = ",".join(skipped_values) if skipped_values else "none"
            parts.append(f"{source}:symbols=[{symbols}] skipped=[{skipped}]")
        summary = "; ".join(parts)
        fingerprint = f"{self._strategy_name}|{kind}|{summary}"
        if self._last_watchlist_trace_fingerprint.get(kind) == fingerprint:
            return
        self._last_watchlist_trace_fingerprint[kind] = fingerprint
        LOG.info("Watchlist trace strategy=%s kind=%s %s", self._strategy_name, kind, summary)

    @staticmethod
    def log_structured(
        prefix: str,
        payload: dict[str, Any],
        *,
        level: int = TRADEFLOW_LEVEL,
    ) -> None:
        """Emit ``{prefix} <compact-json>`` at TRADEFLOW_LEVEL. session_report
        parses these lines back into events.jsonl at EOD."""
        try:
            text = json.dumps(_json_ready(payload), sort_keys=True, separators=(",", ":"))
        except Exception:
            text = json.dumps({"serialization_error": True, "payload": str(payload)})
        LOG.log(level, "%s %s", prefix, text)
