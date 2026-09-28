# SPDX-License-Identifier: MIT
"""Engine-side logging primitives, separated from business logic.

Owns:
  - Interval-deduplicated cycle logs (``log_cycle``): suppress noise when the
    same fingerprint recurs within N seconds.
  - Fingerprinted watchlist traces (``log_watchlist_trace``): only emit when
    the per-kind fingerprint changes.
  - Structured JSON event logs (``log_structured``): TRADEFLOW-level events
    that downstream tools (session_archive) parse out of the log file.
  - The position metadata those records carry
    (``structured_metadata_snapshot``): the entry gatekeeper's ENTRY_CONTEXT
    and the position manager's EXIT_CONTEXT both filter it through here.
  - The one JSON normalizer (``json_safe``): these records, the sqlite
    position store and the dashboard all write through it.

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
import math
import time
from collections.abc import Mapping
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd

from .log_setup import TRADEFLOW_LEVEL, warn_once

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


def json_safe(value: Any, *, non_finite: Literal["keep", "null"]) -> Any:
    """*value* in JSON's own types: a dict with str keys, a list, a str, an
    int, a float, a bool or None. The one normalizer of what the bot writes
    as JSON: the TRADEFLOW records (``log_structured``), the sqlite position
    metadata and risk state (``position_store``), and the dashboard's state
    and chart payloads (``dashboard``).

    - A numpy scalar is unwrapped with ``.item()``: an int64 stays an int
      and a bool_ a bool.
    - A date or datetime (a pandas Timestamp too, and its NaT, which reads
      ``NaT``) becomes its ``isoformat()``, a ``T`` between the date and the
      time. A numpy datetime64 is read as a pandas Timestamp first, whatever
      its unit (``.item()`` gives an int of nanoseconds at the ns unit pandas
      uses), and its NaT is None.
    - A duration is not a date: a timedelta and a pandas Timedelta are
      written as their string (``0:05:00``, ``0 days 00:05:00``), and a numpy
      timedelta64 as what ``.item()`` gives (a timedelta, but an int count
      at the ns, month and year units, and None for its NaT).
    - A tuple or set becomes a list, and a dict key its string.
    - Another number type (a Decimal, a Fraction) becomes a float. A value
      ``float()`` refuses with one of the three errors a number type raises
      is written as its string; any other error from ``float()`` raises.

    ``non_finite`` is what NaN and +/-inf become: ``"keep"`` leaves them for
    ``json.dumps`` to write as ``NaN`` / ``Infinity``, which Python's reader
    takes back (the audit records and the sqlite rows); ``"null"`` makes
    them None, for the dashboard, which serializes with ``allow_nan=False``
    because the browser's ``JSON.parse`` refuses them.
    """
    if non_finite not in ("keep", "null"):
        raise ValueError(f"non_finite must be 'keep' or 'null', got {non_finite!r}")
    return _json_safe_walk(value, non_finite == "keep")


def _json_safe_walk(value: Any, keep_non_finite: bool) -> Any:
    """``json_safe``'s walk; the checks run in the order of how common each
    type is in a dashboard payload."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # float() also unwraps a numpy float64, a float subclass.
        return float(value) if keep_non_finite or math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe_walk(v, keep_non_finite) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_walk(v, keep_non_finite) for v in value]
    if isinstance(value, np.datetime64):
        return None if np.isnat(value) else pd.Timestamp(value).isoformat()
    if isinstance(value, np.generic):
        return _json_safe_walk(value.item(), keep_non_finite)
    if isinstance(value, date):
        return value.isoformat()
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return _safe_str(value)
    return number if keep_non_finite or math.isfinite(number) else None


def _safe_str(value: Any) -> str:
    """``str(value)``, or ``<unserializable TYPE>`` when the value's own
    ``__str__`` / ``__repr__`` raises. The last fallback of ``json_safe``
    and of ``log_structured``, so it must not be able to throw: they feed
    audit logging, the sqlite position-metadata write and the dashboard, and
    none should ever fail because a value could not describe itself. The
    first failure per type is logged with its traceback (until 2026-09-26
    none was)."""
    try:
        return str(value)
    except Exception:
        name = type(value).__name__
        if warn_once(f"audit_safe_str:{name}"):
            LOG.warning(
                "A %s value raised in __str__ / __repr__; it is written as <unserializable %s>. "
                "Further occurrences are not logged.",
                name, name, exc_info=True,
            )
        return f"<unserializable {name}>"


def structured_metadata_snapshot(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """The position metadata an ENTRY_CONTEXT / EXIT_CONTEXT record carries:
    the keys named below and those starting with a prefix below, minus the
    excluded ones and None values (the gatekeeper's entry snapshot and the
    position manager's exit snapshot both add it)."""
    if not isinstance(meta, Mapping):
        return {}
    include_keys = {
        'benchmark', 'zscore', 'side_preference', 'runner_target_applied', 'qualifying_target_count',
        'ltf_score_required', 'min_peer_score_required', 'strong_setup_ltf_score_required', 'strong_setup_peer_score_required',
        'or_high', 'or_low', 'pullback_high', 'pullback_low', 'trigger_high', 'trigger_low',
        'support_low', 'resistance_high', 'extension_from_vwap_pct', 'peer_details', 'macro_details',
        'spread_side', 'spread_style', 'spread_type', 'entry_price_points', 'entry_credit',
        'bought_leg_symbol', 'sold_leg_symbol', 'bought_strike', 'sold_strike',
        'htf_minutes', 'nearest_htf_support', 'nearest_htf_resistance',
        'broken_htf_support', 'broken_htf_resistance', 'prior_day_high', 'prior_day_low',
        'prior_week_high', 'prior_week_low', 'htf_ema_fast', 'htf_ema_slow', 'htf_atr14',
        'htf_trend_bias', 'htf_level_buffer', 'nearest_htf_bullish_fvg', 'nearest_htf_bearish_fvg',
        'source_priority', 'selection_score', 'selection_ltf_score', 'htf_vote_edge',
        'macro_agreement_count', 'selection_quality_score', 'activity_score', 'setup_quality_score',
        'execution_quality_score', 'macro_score', 'entry_family', 'peer_universe', 'side_eval',
        'family_eval', 'evaluated_sides', 'primary_blocker', 'all_blockers', 'near_miss_blockers',
        'selection_components', 'candidate_reason', 'decision_summary',
        # Per-sector index confirmation snapshot. Stamped at entry by
        # top_tier_adaptive.strategy.entry_signals via
        # ``_indices_for_symbol(symbol)``. Logged here so post-session
        # analysis can verify which sector ETFs each entry was
        # confirmed against.
        'confirmation_indices',
        # Volatility widening factor stamped by Tier 2a / early-session
        # widening — useful for slicing trade outcomes by widening tier.
        'vol_widening_factor',
        # The shared entry stage's stamps (2026-09-24): the style family
        # the exit graces key on, the strategy's own priority vs the
        # shared score terms added to it, and where the entry came from
        # (a divergence-only entry) -- what a knob A/B needs afterwards.
        'entry_style_family', 'strategy_priority_score', 'shared_context_score',
        'entry_context_adjustment', 'technical_entry_adjustment', 'entry_source',
    }
    include_prefixes = (
        'fvg_', 'htf_fvg_', 'adaptive_', 'anti_chase_fvg_retest_',
        # Armed-retest provenance (2026-09-20): whether this entry came
        # from the retest the regime waited for or from the market
        # fallback after the wait expired, the level it armed on, and how
        # long it waited. Without this prefix the keys are stamped on the
        # Signal and then dropped here, and the A/B the feature exists to
        # settle cannot be measured after the fact.
        'armed_retest_',
        # HTF EMA trend at entry (htf_ema_trend / _votes / _bonus,
        # 2026-09-24) -- what a dry-run A/B of require_htf_ema_alignment
        # and htf_ema_alignment_score needs after the fact.
        'htf_ema_',
        # HTF RSI divergence score term where a strategy records it
        # (peer_confirmed_htf_pivots' htf_divergence_adjustment, live
        # since 2026-09-24): ranking-only, so it is invisible in the logs
        # without this.
        'htf_divergence_',
        # The gates the shared entry stage applied / exempted, whether a
        # retest admitted the entry, and the divergence confirmation or
        # conflict (2026-09-24).
        'shared_entry_', 'divergence_entry_', 'anti_chase_ob_retest_',
        'msltf_', 'mshtf_', 'sr_', 'tech_', 'matched_', 'chart_pattern_',
        'decision_', 'gate_', 'peak_giveback_', 'orb_',
    )
    exclude_keys = {
        'order_spec', 'long_leg', 'short_leg', 'option_leg', 'valuation_legs',
        'htf_bullish_fvgs', 'htf_bearish_fvgs',
    }
    out: dict[str, Any] = {}
    for key, value in meta.items():
        if value is None or key in exclude_keys:
            continue
        if key in include_keys or any(str(key).startswith(prefix) for prefix in include_prefixes):
            out[str(key)] = value
    return out


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
        """Emit ``{prefix} <compact-json>`` at TRADEFLOW_LEVEL. session_archive
        parses these lines back into events.jsonl at EOD."""
        try:
            text = json.dumps(json_safe(payload, non_finite="keep"), sort_keys=True, separators=(",", ":"))
        except Exception:
            # A logging call must not fail its caller, and the callers are the
            # entry and exit flows (ENTRY_CONTEXT / EXIT_CONTEXT): losing one
            # line's fidelity is acceptable, taking a trade operation down
            # with it is not. json_safe fails only on a payload it cannot
            # walk (a circular or runaway-deep one raises RecursionError) or a
            # value whose __float__ raises what no number type does. The line
            # is written as the payload's string (_safe_str cannot raise, even
            # on a payload whose __repr__ throws), and the first failure per
            # prefix is logged with its traceback (until 2026-09-26 none was).
            if warn_once(f"log_structured:{prefix}"):
                LOG.warning(
                    "%s payload could not be serialized; it is logged as serialization_error. "
                    "Further occurrences are not logged.",
                    prefix, exc_info=True,
                )
            text = json.dumps({"serialization_error": True, "payload": _safe_str(payload)})
        LOG.log(level, "%s %s", prefix, text)
