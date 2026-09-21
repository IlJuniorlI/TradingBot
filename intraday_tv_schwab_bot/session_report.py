# SPDX-License-Identifier: MIT
"""End-of-session reporting: log summary, structured JSON, and persistent CSV trade log.

In addition to the headline numbers (PnL, win rate, profit factor), the report
aggregates closed trades along five axes to support strategy/config tuning:

  * per-regime        — how each setup type performed (trend/pullback/range/...)
  * per-symbol        — catches concentration issues and high-variance tickers
  * per-exit-reason   — surfaces leaky exit mechanisms (phantom stops, tight targets)
  * per-hour          — identifies dead zones in the trading day
  * MAE / MFE         — max adverse / favorable excursion in R-multiples
  * post-stop run     — how far price ran the trade's way AFTER the stop,
                        i.e. how much of a correctly-called move a shakeout cost
  * gate attribution  — what price did after each SKIP, by reason (manifest
                        only): whether a gate blocks moves or blocks losses
  * filter rejections — tally of skip reasons the engine logged during the session

All aggregate sections are emitted both in the human log (fixed-width tables)
and inside the SESSION_REPORT structured JSON payload (under top-level keys
``per_regime``, ``per_symbol``, ``per_exit_reason``, ``per_hour``,
``mae_mfe``, ``post_stop_continuation``, ``filter_rejections``) so
downstream tooling can parse them without re-scraping.
"""
from __future__ import annotations

import csv
import dataclasses
import io
import json
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover — yaml is a hard dep elsewhere
    _yaml = None  # type: ignore[assignment]

from .paper_account import PaperAccount, TradeRecord
from .models import Position
from .utils import now_et

from .utils import atomic_write_text as _atomic_write_text

LOG = logging.getLogger(__name__)

TRADE_CSV_COLUMNS = ["date"] + [f.name for f in dataclasses.fields(TradeRecord)]


def _trade_csv_row(trade: TradeRecord, session_date: str) -> dict[str, Any]:
    def _round_opt(value: float | None, digits: int) -> float | None:
        return None if value is None else round(float(value), digits)

    return {
        "date": session_date,
        "symbol": trade.symbol,
        "strategy": trade.strategy,
        "side": trade.side,
        "qty": trade.qty,
        "entry_price": round(trade.entry_price, 4),
        "exit_price": round(trade.exit_price, 4),
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat(),
        "realized_pnl": round(trade.realized_pnl, 2),
        "return_pct": round(trade.return_pct, 4),
        "hold_minutes": round(trade.hold_minutes, 1),
        "reason": trade.reason,
        "asset_type": trade.asset_type,
        "underlying": trade.underlying,
        "exchange": trade.exchange,
        "option_type": trade.option_type,
        "lifecycle_id": trade.lifecycle_id,
        "partial_exit": trade.partial_exit,
        "final_exit": trade.final_exit,
        "remaining_qty_after_exit": trade.remaining_qty_after_exit,
        "fill_price_estimated": trade.fill_price_estimated,
        "broker_recovered": trade.broker_recovered,
        "regime": trade.regime,
        "initial_risk_per_unit": _round_opt(trade.initial_risk_per_unit, 4),
        "max_favorable_pnl": _round_opt(trade.max_favorable_pnl, 2),
        "max_adverse_pnl": _round_opt(trade.max_adverse_pnl, 2),
        "entry_slippage_pct": _round_opt(trade.entry_slippage_pct, 6),
        "realized_entry_risk": _round_opt(trade.realized_entry_risk, 4),
        "entry_risk_budget": _round_opt(trade.entry_risk_budget, 4),
        "entry_risk_overage_frac": _round_opt(trade.entry_risk_overage_frac, 6),
        "armed_retest_status": trade.armed_retest_status,
        "armed_retest_waited_minutes": _round_opt(trade.armed_retest_waited_minutes, 2),
    }


# ---------------------------------------------------------------------------
# Aggregators — pure functions over a list of closed trades
# ---------------------------------------------------------------------------

def _safe_pct(wins: int, total: int) -> float | None:
    return (wins / total) if total > 0 else None


def _summarize_group(trades: list[TradeRecord]) -> dict[str, Any]:
    """Compute the shared (count, wins, losses, net_pnl, avg_pnl, win_rate,
    best, worst) summary for any slice of trades."""
    if not trades:
        return {
            "count": 0, "wins": 0, "losses": 0,
            "net_pnl": 0.0, "avg_pnl": None, "win_rate": None,
            "best": None, "worst": None,
        }
    wins = sum(1 for t in trades if t.realized_pnl > 0)
    losses = sum(1 for t in trades if t.realized_pnl < 0)
    total_pnl = sum(t.realized_pnl for t in trades)
    best_trade = max(trades, key=lambda t: t.realized_pnl)
    worst_trade = min(trades, key=lambda t: t.realized_pnl)
    return {
        "count": len(trades),
        "wins": wins,
        "losses": losses,
        "net_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(trades), 2),
        "win_rate": _safe_pct(wins, len(trades)),
        "best": round(best_trade.realized_pnl, 2),
        "worst": round(worst_trade.realized_pnl, 2),
    }


def _group_by(trades: Iterable[TradeRecord], key_fn) -> dict[str, list[TradeRecord]]:
    buckets: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        key = key_fn(t)
        if key is None:
            key = "unknown"
        buckets[str(key)].append(t)
    return dict(buckets)


def _per_regime(trades: list[TradeRecord]) -> dict[str, dict[str, Any]]:
    return {regime: _summarize_group(group) for regime, group in _group_by(trades, lambda t: t.regime or "unknown").items()}


def _per_entry_path(trades: list[TradeRecord]) -> dict[str, dict[str, Any]]:
    """Outcomes split by HOW the entry was reached, for the arming regimes.

    ``retest`` — the entry fired on the retest the regime waited for.
    ``market_fallback`` — the wait expired and it entered at market, which is
    the pre-2026-09-20 behaviour and therefore the control.
    ``immediate`` — a regime that does not arm.

    This is the A/B. Without it the week produces a single blended number and
    the change cannot be judged: a good week could be the fallback entries
    carrying poor retest entries, or the reverse, and both read identically in
    the headline PnL.
    """
    def _bucket(trade: TradeRecord) -> str:
        status = (trade.armed_retest_status or "").strip()
        if status == "retest_confirmed":
            return "retest"
        if status == "expired_market_entry":
            return "market_fallback"
        return "immediate"

    return {key: _summarize_group(group)
            for key, group in sorted(_group_by(trades, _bucket).items())}


def _per_symbol(trades: list[TradeRecord]) -> dict[str, dict[str, Any]]:
    return {symbol: _summarize_group(group) for symbol, group in _group_by(trades, lambda t: t.symbol).items()}


def _per_exit_reason(trades: list[TradeRecord]) -> dict[str, dict[str, Any]]:
    # Strip any parameterization off the reason string so
    # "resistance_break_exit:311.5900" and "resistance_break_exit:313.00"
    # roll up into "resistance_break_exit".
    def _normalize(reason: str) -> str:
        base = str(reason or "unknown").split(":", 1)[0].strip()
        return base or "unknown"

    return {reason: _summarize_group(group) for reason, group in _group_by(trades, lambda t: _normalize(t.reason)).items()}


def _per_hour(trades: list[TradeRecord]) -> dict[str, dict[str, Any]]:
    def _hour_bucket(t: TradeRecord) -> str:
        # Bucket by ENTRY hour (local time). Entry time tells us when the
        # bot decided to trade; exit time is a product of management and
        # can drift long after entry.
        try:
            return f"{t.entry_time.hour:02d}:00"
        except Exception:
            return "unknown"

    return {hour: _summarize_group(group) for hour, group in _group_by(trades, _hour_bucket).items()}


def _mae_mfe_summary(trades: list[TradeRecord]) -> dict[str, Any]:
    """Aggregate max adverse/favorable excursion in R-multiples.

    R = dollars / (initial_risk_per_unit * qty). Requires both MAE/MFE
    values and initial risk — trades missing either are skipped.
    """
    r_favorable: list[float] = []
    r_adverse: list[float] = []
    heat_threshold_hits = 0  # trades where MAE > 1.0R (stop zone threatened)
    runup_threshold_hits = 0  # trades where MFE > 2.0R (let profit run)
    for t in trades:
        risk_per_unit = t.initial_risk_per_unit
        if risk_per_unit is None or risk_per_unit <= 0 or t.qty == 0:
            continue
        r_dollar = abs(risk_per_unit * t.qty)
        if r_dollar <= 0:
            continue
        if t.max_favorable_pnl is not None:
            mfe_r = t.max_favorable_pnl / r_dollar
            r_favorable.append(mfe_r)
            if mfe_r >= 2.0:
                runup_threshold_hits += 1
        if t.max_adverse_pnl is not None:
            mae_r = abs(t.max_adverse_pnl) / r_dollar
            r_adverse.append(mae_r)
            if mae_r >= 1.0:
                heat_threshold_hits += 1

    def _avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 3) if values else None

    return {
        "avg_mae_r": _avg(r_adverse),
        "avg_mfe_r": _avg(r_favorable),
        "max_mae_r": round(max(r_adverse), 3) if r_adverse else None,
        "max_mfe_r": round(max(r_favorable), 3) if r_favorable else None,
        "trades_mae_over_1r": heat_threshold_hits,
        "trades_mfe_over_2r": runup_threshold_hits,
        "sample_size": min(len(r_favorable), len(r_adverse)),
    }


def _post_stop_continuation(
    trades: list[TradeRecord],
    bars_for: Any | None,
    *,
    window_minutes: int = 30,
) -> dict[str, Any]:
    """How far price ran the trade's way AFTER it was stopped out.

    The question this answers: when the bot called the direction correctly but
    was shaken out on the retrace, how much of the move did it miss? Every
    other aggregate here scores the trade as it was closed; this one scores
    what happened next.

    It matters because of how the trend regime is shaped. Entry requires
    ``close > max(high of the previous N bars)``, so the fill is at a fresh
    N-bar extreme by construction — there is no retest path. The stop lands
    roughly 1-1.5% away once the ``default_stop_pct`` and ``min_stop_atr_mult``
    floors apply, which is inside the ordinary retest band for a mega cap. And
    ``same_level_block_minutes`` (30) then bars same-direction re-entry within
    ``same_level_block_atr_mult`` x ATR of the stop, which is usually where and
    when the next leg starts.

    Measured in R (``initial_risk_per_unit``), so it is comparable across
    symbols and sizes. ``window_minutes`` bounds how long after the stop
    counts — beyond that it is a different trade, not a missed continuation.

    ``opportunity_usd`` is an UPPER BOUND, not recoverable profit: it assumes
    re-entry at the stop price and an exit at the window's best tick. Read it
    as "the move left on the table", not "money the bot would have made".

    ``bars_for`` is a ``symbol -> DataFrame`` callable (the engine passes a
    thin wrapper over the data feed). Passing None disables the section, which
    is what the pure-aggregate tests do.
    """
    stop_exits = [
        t for t in trades
        if str(t.reason or "").split(":", 1)[0].strip().lower() == "stop"
    ]
    summary: dict[str, Any] = {
        "window_minutes": int(window_minutes),
        "stop_exits": len(stop_exits),
        "evaluated": 0,
        "not_evaluated": len(stop_exits),
        "reached_1r": 0,
        "reached_2r": 0,
        "avg_post_stop_r": None,
        "median_post_stop_r": None,
        "max_post_stop_r": None,
        "opportunity_usd": None,
    }
    if not stop_exits or bars_for is None:
        return summary

    post_r: list[float] = []
    opportunity = 0.0
    for trade in stop_exits:
        risk_per_unit = trade.initial_risk_per_unit
        # NaN survives `<= 0` (every comparison against NaN is False), so a
        # NaN risk used to reach the arithmetic below and turn
        # `opportunity_usd` into NaN — the same "unevaluable masquerading as
        # a value" this function is written to avoid. Require finite.
        if risk_per_unit is None or not math.isfinite(float(risk_per_unit)) or risk_per_unit <= 0:
            continue
        try:
            frame = bars_for(trade.symbol)
        except Exception:
            LOG.debug("post-stop lookup failed for %s", trade.symbol, exc_info=True)
            continue
        if frame is None or getattr(frame, "empty", True):
            continue
        try:
            window_end = trade.exit_time + timedelta(minutes=int(window_minutes))
            after = frame[(frame.index > trade.exit_time) & (frame.index <= window_end)]
            if after.empty:
                continue
            if str(trade.side).upper().endswith("LONG"):
                best = float(after["high"].max())
                excursion = best - float(trade.exit_price)
            else:
                best = float(after["low"].min())
                excursion = float(trade.exit_price) - best
        except Exception:
            LOG.debug("post-stop window failed for %s", trade.symbol, exc_info=True)
            continue
        # An infinite high would divide through to an infinite R and poison
        # every aggregate below it. `ensure_ohlcv_frame` drops NaN OHLC but
        # NOT inf, so this is the frame's own guard, not a duplicate.
        if not math.isfinite(excursion):
            continue
        # Negative means it kept going against the trade; the stop was right.
        r_multiple = max(0.0, excursion / float(risk_per_unit))
        post_r.append(r_multiple)
        opportunity += r_multiple * float(risk_per_unit) * abs(int(trade.qty))

    if not post_r:
        return summary

    ordered = sorted(post_r)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    summary.update({
        "evaluated": len(post_r),
        "not_evaluated": len(stop_exits) - len(post_r),
        "reached_1r": sum(1 for r in post_r if r >= 1.0),
        "reached_2r": sum(1 for r in post_r if r >= 2.0),
        "avg_post_stop_r": round(sum(post_r) / len(post_r), 3),
        "median_post_stop_r": round(median, 3),
        "max_post_stop_r": round(max(post_r), 3),
        "opportunity_usd": round(opportunity, 2),
    })
    return summary


def _entry_timing(
    trades: list[TradeRecord],
    bars_for: Any | None,
    *,
    window_minutes: int = 15,
    baseline_stride: int = 7,
    min_regime_samples: int = 5,
) -> dict[str, Any]:
    """Whether waiting for a pullback would have bought a better entry.

    For each trade, the best price available in the trade's favour within
    ``window_minutes`` AFTER the fill — the lowest low for a LONG, the highest
    high for a SHORT — expressed as a fraction of that trade's own
    entry-to-stop distance (``initial_risk_per_unit``). That denominator is
    what makes the number readable without a second unit: 0.4 means the
    retrace covered 40% of the way to the stop, so a patient entry down there
    would have been 0.4R better with the same stop. 1.0 or more means price
    reached the stop, which is what being stopped out IS.

    The point is the regime split. trend / momentum / vol_squeeze can only
    fill at an N-bar extreme by construction, so if the bot is chasing they
    should show a systematically deeper retrace than pullback, which requires
    a 25-50% leg retracement before it fires at all.

    THE BASELINE IS THE WHOLE MEASUREMENT. Price dips below any given price
    most of the time, so a raw "a better entry existed" count is noise — the
    first version of ``_gate_attribution`` made exactly this mistake and
    ranked a gate with no edge as the costliest one. So each trade is scored
    against arbitrary moments in the SAME symbol's session, using THAT TRADE'S
    risk distance as the denominator, which controls for the symbol's
    volatility and the trade's own stop width at once. Only the gap between
    the two is evidence.

    NOT a backtest. It says a better price existed, not that the bot could
    have got it: no fill model, and no claim the setup would still have been
    valid down there. Read it as "how far into the stop the ordinary retrace
    reached", not as recoverable profit.

    A regime with fewer than ``min_regime_samples`` trades is kept in
    ``by_regime`` but marked ``low_sample`` and sorted last. A single trade has
    a "median" of that one trade, and on the archives that put a regime with
    one fill at the top of the table with +7.4R -- the same small-sample trap
    that made the first ranking in ``_gate_attribution`` unreadable.

    ``bars_for`` is a ``symbol -> DataFrame`` callable. Passing None disables
    the section, which is what the pure-aggregate tests do.
    """
    summary: dict[str, Any] = {
        "window_minutes": int(window_minutes),
        "measures": ("retrace after entry as a fraction of the entry-to-stop "
                     "distance, against a same-session baseline - NOT a "
                     "backtest: no fill model, no re-validation of the setup"),
        "trades": len(trades),
        "evaluated": 0,
        "not_evaluated": len(trades),
        "median_retrace_r": None,
        "median_baseline_retrace_r": None,
        "edge_over_baseline_r": None,
        "reached_quarter_stop_pct": None,
        "reached_half_stop_pct": None,
        "reached_stop_pct": None,
        "by_regime": {},
        "by_entry_path": {},
    }
    if not trades or bars_for is None:
        return summary

    def _median(values: list[float]) -> float:
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    def _retrace_r(frame, at, is_long: bool, entry: float, risk: float) -> float | None:
        """Deepest move AGAINST *entry* in the window after *at*, in R."""
        try:
            window = frame[(frame.index > at) & (frame.index <= at + timedelta(minutes=int(window_minutes)))]
            if window.empty:
                return None
            worst = float(window["low"].min()) if is_long else float(window["high"].max())
        except Exception:
            return None
        adverse = (entry - worst) if is_long else (worst - entry)
        if not math.isfinite(adverse):
            return None
        return max(0.0, adverse / risk)

    observed: list[float] = []
    baseline: list[float] = []
    per_regime: dict[str, list[float]] = defaultdict(list)
    per_regime_baseline: dict[str, list[float]] = defaultdict(list)
    # Split by HOW the entry was reached. This is the mechanism test for the
    # armed retest: a retest entry should show a SHALLOWER post-fill retrace
    # than a market fallback on the same regime, because the retrace already
    # happened before the fill. If the two are equal the feature is not doing
    # what it was built to do, whatever the PnL says.
    per_path: dict[str, list[float]] = defaultdict(list)
    per_path_baseline: dict[str, list[float]] = defaultdict(list)

    for trade in trades:
        risk_per_unit = trade.initial_risk_per_unit
        # Same finiteness rule as _post_stop_continuation: NaN survives `<= 0`
        # because every comparison against NaN is False, and an unevaluable
        # trade must not be scored as "no retrace" — that would read as
        # evidence AGAINST chasing, which is the conclusion being tested.
        if risk_per_unit is None or not math.isfinite(float(risk_per_unit)) or risk_per_unit <= 0:
            continue
        entry_price = trade.entry_price
        if entry_price is None or not math.isfinite(float(entry_price)) or entry_price <= 0:
            continue
        try:
            frame = bars_for(trade.symbol)
        except Exception:
            LOG.debug("entry-timing lookup failed for %s", trade.symbol, exc_info=True)
            continue
        if frame is None or getattr(frame, "empty", True):
            continue
        is_long = str(trade.side).upper().endswith("LONG")
        risk = float(risk_per_unit)
        actual = _retrace_r(frame, trade.entry_time, is_long, float(entry_price), risk)
        if actual is None:
            continue
        regime = (trade.regime or "none").strip() or "none"
        status = (trade.armed_retest_status or "").strip()
        path = {"retest_confirmed": "retest",
                "expired_market_entry": "market_fallback"}.get(status, "immediate")
        observed.append(actual)
        per_regime[regime].append(actual)
        per_path[path].append(actual)

        # Baseline: the same measurement from arbitrary moments in this
        # symbol's session, anchored on each sampled bar's own close and
        # carrying THIS trade's risk distance.
        #
        # Scoped to the TRADE'S OWN SESSION DATE. `bars_for` hands back the
        # multi-day history frame, and sampling all of it would fold a quiet
        # overnight stretch into the baseline, depress it, and inflate every
        # edge above it -- while the docstring claimed a same-session
        # comparison. The whole metric is the gap between observed and
        # baseline, so a baseline drawn from a different tape measures nothing.
        try:
            session_mask = frame.index.date == trade.entry_time.date()
            session = frame[session_mask]
            rows = (session.iloc[::max(1, int(baseline_stride))]
                    if not session.empty else None)
        except Exception:
            rows = None
        if rows is not None:
            for at, row in rows.iterrows():
                anchor_price = float(row.get("close", float("nan")))
                if not math.isfinite(anchor_price) or anchor_price <= 0:
                    continue
                sampled = _retrace_r(frame, at, is_long, anchor_price, risk)
                if sampled is not None:
                    baseline.append(sampled)
                    per_regime_baseline[regime].append(sampled)
                    per_path_baseline[path].append(sampled)

    if not observed:
        return summary

    median_observed = _median(observed)
    median_baseline = _median(baseline) if baseline else None
    summary.update({
        "evaluated": len(observed),
        "not_evaluated": len(trades) - len(observed),
        "median_retrace_r": round(median_observed, 3),
        "median_baseline_retrace_r": (
            round(median_baseline, 3) if median_baseline is not None else None),
        "edge_over_baseline_r": (
            round(median_observed - median_baseline, 3)
            if median_baseline is not None else None),
        "baseline_samples": len(baseline),
        "reached_quarter_stop_pct": round(
            100.0 * sum(1 for v in observed if v >= 0.25) / len(observed), 1),
        "reached_half_stop_pct": round(
            100.0 * sum(1 for v in observed if v >= 0.50) / len(observed), 1),
        "reached_stop_pct": round(
            100.0 * sum(1 for v in observed if v >= 1.0) / len(observed), 1),
    })
    by_regime: dict[str, Any] = {}
    for regime, values in per_regime.items():
        regime_baseline = per_regime_baseline.get(regime, [])
        regime_median = _median(values)
        base_median = _median(regime_baseline) if regime_baseline else None
        by_regime[regime] = {
            "trades": len(values),
            "low_sample": len(values) < int(min_regime_samples),
            "median_retrace_r": round(regime_median, 3),
            "median_baseline_retrace_r": (
                round(base_median, 3) if base_median is not None else None),
            "edge_over_baseline_r": (
                round(regime_median - base_median, 3)
                if base_median is not None else None),
            "reached_stop_pct": round(
                100.0 * sum(1 for v in values if v >= 1.0) / len(values), 1),
        }
    by_path: dict[str, Any] = {}
    for path, values in per_path.items():
        path_baseline = per_path_baseline.get(path, [])
        path_median = _median(values)
        base_median = _median(path_baseline) if path_baseline else None
        by_path[path] = {
            "trades": len(values),
            "low_sample": len(values) < int(min_regime_samples),
            "median_retrace_r": round(path_median, 3),
            "median_baseline_retrace_r": (
                round(base_median, 3) if base_median is not None else None),
            "edge_over_baseline_r": (
                round(path_median - base_median, 3)
                if base_median is not None else None),
            "reached_stop_pct": round(
                100.0 * sum(1 for v in values if v >= 1.0) / len(values), 1),
        }
    summary["by_entry_path"] = dict(sorted(by_path.items()))
    summary["min_regime_samples"] = int(min_regime_samples)
    summary["by_regime"] = dict(
        sorted(by_regime.items(),
               key=lambda kv: (kv[1]["low_sample"],
                               kv[1]["edge_over_baseline_r"] is None,
                               -(kv[1]["edge_over_baseline_r"] or 0.0)))
    )
    return summary


def _normalize_skip_reason(reason: str) -> str:
    """Collapse parameterized skip reasons into a stable bucket name.

    Many skip reasons carry numeric context in parentheses — e.g.
    ``long_no_fresh_breakout(close=248.7250<=recent_high=248.8099)`` or
    ``short_no_qualifying_regime(trend=1.0,pb=0.0,range=1.0)``. Every
    unique price/score combination would otherwise bloat the filter-
    rejection counter into hundreds of near-duplicate buckets. Strip the
    parenthetical suffix so counts roll up cleanly. The detailed
    variants are still preserved in ``all_reasons`` under a separate
    ``variants`` bucket so they can be inspected when tuning."""
    idx = reason.find("(")
    if idx <= 0:
        return reason
    return reason[:idx].rstrip()


def _filter_rejection_summary(skip_counts: dict[str, int] | None) -> dict[str, Any]:
    """Shape the engine's raw skip-count dict into a stable, sorted payload.

    Two views are emitted:
      * ``top_reasons`` / ``all_reasons`` — grouped by normalized reason
        (no parenthetical suffix). This is the view the operator reads
        for day-over-day comparison.
      * ``variants`` — the raw reasons as logged, preserved so a tuner
        can inspect the full parameter distribution of a specific bucket.
    """
    if not skip_counts:
        return {"total_skips": 0, "top_reasons": [], "all_reasons": {}, "variants": {}}
    total = sum(int(v) for v in skip_counts.values())
    # Group by normalized reason.
    normalized: dict[str, int] = {}
    variants: dict[str, dict[str, int]] = {}
    for reason, count in skip_counts.items():
        bucket = _normalize_skip_reason(str(reason))
        normalized[bucket] = normalized.get(bucket, 0) + int(count)
        if bucket != reason:
            # Preserve the raw variant so tuning can see distributions.
            variants.setdefault(bucket, {})[str(reason)] = int(count)
    sorted_items = sorted(normalized.items(), key=lambda kv: (-kv[1], kv[0]))
    top = [{"reason": reason, "count": int(count)} for reason, count in sorted_items[:10]]
    all_ = {reason: int(count) for reason, count in sorted_items}
    return {"total_skips": total, "top_reasons": top, "all_reasons": all_, "variants": variants}


# ---------------------------------------------------------------------------
# Log formatters — human-readable fixed-width tables
# ---------------------------------------------------------------------------

def _fmt_pct_opt(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "n/a"


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:+.2f}"


def _log_group_table(title: str, rows: dict[str, dict[str, Any]], *, key_label: str) -> None:
    if not rows:
        return
    LOG.info("  %s:", title)
    LOG.info("    %-20s %6s %7s %10s %10s %8s %10s %10s", key_label, "count", "w/l", "net_pnl", "avg_pnl", "win%", "best", "worst")
    sorted_rows = sorted(rows.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    for key, summary in sorted_rows:
        LOG.info(
            "    %-20s %6d %7s %10s %10s %8s %10s %10s",
            key[:20],
            summary["count"],
            f"{summary['wins']}/{summary['losses']}",
            _fmt_money(summary["net_pnl"]),
            _fmt_money(summary["avg_pnl"]),
            _fmt_pct_opt(summary["win_rate"]),
            _fmt_money(summary["best"]),
            _fmt_money(summary["worst"]),
        )


def _log_mae_mfe(summary: dict[str, Any]) -> None:
    if summary.get("sample_size", 0) == 0:
        return
    LOG.info(
        "  MAE/MFE (n=%d): avg_MAE=%sR avg_MFE=%sR max_MAE=%sR max_MFE=%sR; trades>1R_heat=%d trades>2R_runup=%d",
        summary["sample_size"],
        summary["avg_mae_r"] if summary["avg_mae_r"] is not None else "n/a",
        summary["avg_mfe_r"] if summary["avg_mfe_r"] is not None else "n/a",
        summary["max_mae_r"] if summary["max_mae_r"] is not None else "n/a",
        summary["max_mfe_r"] if summary["max_mfe_r"] is not None else "n/a",
        int(summary.get("trades_mae_over_1r", 0)),
        int(summary.get("trades_mfe_over_2r", 0)),
    )


def _log_post_stop_continuation(summary: dict[str, Any]) -> None:
    """Only logged when there were stop exits — silence is the common case
    early in a session and an empty table is noise."""
    if not summary.get("stop_exits"):
        return
    LOG.info("--- Post-stop continuation (%d min window) ---", summary.get("window_minutes", 0))
    evaluated = int(summary.get("evaluated") or 0)
    LOG.info(
        "  stop exits=%d  evaluated=%d  (unevaluated=%d: no initial risk or no bars)",
        int(summary.get("stop_exits") or 0), evaluated,
        int(summary.get("not_evaluated") or 0),
    )
    if not evaluated:
        return
    LOG.info(
        "  ran >=1R after the stop: %d/%d      >=2R: %d/%d",
        int(summary.get("reached_1r") or 0), evaluated,
        int(summary.get("reached_2r") or 0), evaluated,
    )
    LOG.info(
        "  post-stop R  avg=%s  median=%s  max=%s",
        summary.get("avg_post_stop_r"), summary.get("median_post_stop_r"),
        summary.get("max_post_stop_r"),
    )
    LOG.info(
        "  move left on the table: %s  (UPPER BOUND - assumes re-entry at the "
        "stop and an exit at the window's best tick)",
        _fmt_money(summary.get("opportunity_usd")),
    )


def _log_entry_timing(summary: dict[str, Any]) -> None:
    """Render the entry-timing block. Reads as 'how far into the stop the
    ordinary retrace after our fills reached, versus an arbitrary moment'."""
    if not summary or not summary.get("evaluated"):
        return
    LOG.info(
        "Entry timing (%d min after fill, retrace as a fraction of the stop "
        "distance; NOT a backtest):",
        summary.get("window_minutes", 0),
    )
    baseline = summary.get("median_baseline_retrace_r")
    edge = summary.get("edge_over_baseline_r")

    # These fields are already percentages; `_fmt_pct_opt` formats a FRACTION
    # as a percent, so routing them through it multiplies by 100 twice.
    def _pct(value: float | None) -> str:
        return f"{value:.1f}%" if value is not None else "n/a"

    LOG.info(
        "  all trades: n=%d median=%.2fR baseline=%s edge=%s "
        "| reached 1/4 stop %s, 1/2 stop %s, full stop %s",
        summary.get("evaluated", 0),
        summary.get("median_retrace_r") or 0.0,
        f"{baseline:.2f}R" if baseline is not None else "n/a",
        f"{edge:+.2f}R" if edge is not None else "n/a",
        _pct(summary.get("reached_quarter_stop_pct")),
        _pct(summary.get("reached_half_stop_pct")),
        _pct(summary.get("reached_stop_pct")),
    )
    for path, row in (summary.get("by_entry_path") or {}).items():
        path_edge = row.get("edge_over_baseline_r")
        LOG.info(
            "    path=%-16s n=%-3d median=%.2fR edge=%s stopped_out=%s%s",
            path, row.get("trades", 0), row.get("median_retrace_r") or 0.0,
            f"{path_edge:+.2f}R" if path_edge is not None else "n/a",
            _pct(row.get("reached_stop_pct")),
            "  (low sample)" if row.get("low_sample") else "",
        )
    for regime, row in (summary.get("by_regime") or {}).items():
        regime_edge = row.get("edge_over_baseline_r")
        LOG.info(
            "    %-14s n=%-3d median=%.2fR edge=%s stopped_out=%s%s",
            regime, row.get("trades", 0), row.get("median_retrace_r") or 0.0,
            f"{regime_edge:+.2f}R" if regime_edge is not None else "n/a",
            _pct(row.get("reached_stop_pct")),
            "  (low sample)" if row.get("low_sample") else "",
        )


def _log_filter_rejections(summary: dict[str, Any]) -> None:
    total = int(summary.get("total_skips", 0))
    if total == 0:
        return
    LOG.info("  Filter rejections (%d total skips; showing top %d):", total, min(10, len(summary.get("top_reasons", []))))
    for item in summary.get("top_reasons", []):
        LOG.info("    %-40s %6d", str(item.get("reason", ""))[:40], int(item.get("count", 0)))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def write_session_report(
    account: PaperAccount,
    positions: dict[str, Position],
    *,
    strategy: str,
    dry_run: bool,
    log_dir: str,
    structured_logger: Any | None = None,
    skip_counts: dict[str, int] | None = None,
    bars_for: Any | None = None,
    post_stop_window_minutes: int = 30,
    entry_timing_window_minutes: int = 15,
) -> None:
    """Write an end-of-session summary to the log, append trades to a
    persistent CSV file in the log directory, and emit a structured
    SESSION_REPORT JSON payload containing per-regime / per-symbol /
    per-exit-reason / per-hour breakdowns, MAE/MFE aggregates, and the
    filter-rejection tally.

    Parameters
    ----------
    account : PaperAccount
        The paper/live account tracker with trade history.
    positions : dict[str, Position]
        Currently open positions (should be empty at session end).
    strategy : str
        Active strategy name.
    dry_run : bool
        Whether the bot ran in dry-run mode.
    log_dir : str
        Path to the log directory for the CSV file.
    structured_logger : callable, optional
        A ``(prefix, payload)`` callable for structured JSON logging
        (e.g., ``engine._log_structured``).
    skip_counts : dict[str, int], optional
        Session-wide tally of per-candidate skip reasons from
        ``engine.session_skip_counts``. Used to emit the filter-rejection
        summary.
    """
    # Initialized before the try so the CSV-append path below (outside the
    # try) can safely early-return if the report build raised before these
    # were populated. The linter flags a "might be referenced before
    # assignment" otherwise.
    closed: list = []
    session_date: str = now_et().date().isoformat()
    try:
        performance = account.capture_snapshot(positions)
        trades = list(account.trades)
        today = now_et().date()
        session_date = today.isoformat()

        # Scoped to the SESSION, not the account. `account.realized_pnl` is a
        # lifetime accumulator -- set to 0.0 once in `PaperAccount.__init__`,
        # only ever incremented, never reset per day -- and every headline
        # number below used to come from it while `trades` counted the list
        # beside it. Two scopes in one line, with the wrong one as the
        # headline. `manifest.realized_pnl` was fixed for exactly this on
        # 2026-09-19; this is the call site that fix missed.
        #
        # The date filter matters for the same reason it does in
        # `export_session_archive`: a bot that runs across midnight without
        # restarting keeps the prior day's records in `account.trades`, so
        # without it the headline mixes days AND the persistent trades.csv
        # re-appends yesterday's rows under today's date.
        #
        # A trade whose exit timestamp cannot be read is DROPPED, matching
        # `export_session_archive`. Keeping it looks like the more careful
        # choice -- "unevaluable is not the same as absent" -- and here it is
        # the opposite: `_trade_csv_row` calls `exit_time.isoformat()`, so one
        # unreadable record raises inside the broad try/except around this
        # whole block and costs the ENTIRE report, every aggregate and the CSV
        # append with it. Losing one row beats losing the session.
        def _closed_today(trade: TradeRecord) -> bool:
            if not bool(getattr(trade, "final_exit", True)):
                return False
            exit_time = getattr(trade, "exit_time", None)
            try:
                return exit_time.date() == today
            except (AttributeError, TypeError):
                LOG.warning(
                    "Dropping %s from the session report: unreadable exit_time %r",
                    getattr(trade, "symbol", "?"), exit_time,
                )
                return False

        closed = [t for t in trades if _closed_today(t)]

        # --- Log summary ---
        wins = sum(1 for t in closed if t.realized_pnl > 0)
        losses = sum(1 for t in closed if t.realized_pnl < 0)
        # Summing the ROUNDED per-row values, so this equals the sum of
        # trades.csv exactly rather than to within a cent -- `_trade_csv_row`
        # is what writes those rows.
        total_pnl = round(sum(round(float(t.realized_pnl), 2) for t in closed), 2)
        win_rate = (wins / len(closed)) if closed else None
        gross_profit = sum(t.realized_pnl for t in closed if t.realized_pnl > 0)
        gross_loss = abs(sum(t.realized_pnl for t in closed if t.realized_pnl < 0))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
        avg_trade = (total_pnl / len(closed)) if closed else None
        # Drawdown stays account-derived: it is an equity-curve metric, not a
        # per-trade aggregate, and has no meaningful session-only form here.
        max_dd = float(performance.get("max_drawdown", 0.0) or 0.0)
        LOG.info(
            "SESSION REPORT %s: strategy=%s pnl=%.2f trades=%d wins=%d losses=%d win_rate=%s pf=%s avg_trade=%s max_drawdown=%.2f",
            session_date, strategy, total_pnl, len(closed), wins, losses,
            f"{win_rate:.1%}" if win_rate is not None else "n/a",
            f"{profit_factor:.2f}" if profit_factor is not None else "n/a",
            f"${avg_trade:.2f}" if avg_trade is not None else "n/a",
            max_dd,
        )
        for trade in closed:
            LOG.info(
                "  %s %s %s qty=%d entry=%.2f exit=%.2f pnl=%.2f (%.2f%%) hold=%.0fm reason=%s",
                trade.symbol, trade.side, trade.strategy, trade.qty,
                trade.entry_price, trade.exit_price, trade.realized_pnl,
                trade.return_pct, trade.hold_minutes, trade.reason,
            )

        # --- Aggregates ---
        per_regime = _per_regime(closed)
        per_entry_path = _per_entry_path(closed)
        per_symbol = _per_symbol(closed)
        per_exit_reason = _per_exit_reason(closed)
        per_hour = _per_hour(closed)
        mae_mfe = _mae_mfe_summary(closed)
        filter_rejections = _filter_rejection_summary(skip_counts)
        post_stop = _post_stop_continuation(
            closed, bars_for, window_minutes=post_stop_window_minutes)
        entry_timing = _entry_timing(
            closed, bars_for, window_minutes=entry_timing_window_minutes)

        # --- Human-readable aggregate tables ---
        if closed:
            _log_group_table("Per regime", per_regime, key_label="regime")
            _log_group_table("Per entry path", per_entry_path, key_label="entry_path")
            _log_group_table("Per symbol", per_symbol, key_label="symbol")
            _log_group_table("Per exit reason", per_exit_reason, key_label="exit_reason")
            _log_group_table("Per hour (entry)", per_hour, key_label="hour_et")
            _log_mae_mfe(mae_mfe)
            _log_post_stop_continuation(post_stop)
            _log_entry_timing(entry_timing)
        _log_filter_rejections(filter_rejections)

        # --- Structured JSON log ---
        report_payload = {
            "date": session_date,
            "strategy": strategy,
            "dry_run": dry_run,
            "realized_pnl": round(total_pnl, 2),
            "trades": len(closed),
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4) if win_rate is not None else None,
            "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
            "average_trade": round(avg_trade, 2) if avg_trade is not None else None,
            "max_drawdown": round(max_dd, 2),
            "per_regime": per_regime,
            "per_entry_path": per_entry_path,
            "per_symbol": per_symbol,
            "per_exit_reason": per_exit_reason,
            "per_hour": per_hour,
            "mae_mfe": mae_mfe,
            "post_stop_continuation": post_stop,
            "entry_timing": entry_timing,
            "filter_rejections": filter_rejections,
        }
        if structured_logger is not None:
            structured_logger("SESSION_REPORT", report_payload)
        else:
            LOG.info("SESSION_REPORT %s", json.dumps(report_payload, sort_keys=True, separators=(",", ":")))
    except Exception as exc:
        LOG.warning("Could not write session report: %s", exc)

    # --- Append to persistent CSV ---
    # Kept outside the broad try/except above so that ValueError raised by
    # DictWriter(extrasaction="raise") — our TradeRecord field-drift guard —
    # propagates instead of being silently swallowed. Only I/O errors are
    # caught here.
    if not closed:
        return
    log_path = Path(log_dir)
    try:
        log_path.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as exc:
        LOG.warning("Could not create log directory %s: %s", log_path, exc)
        return
    csv_path = log_path / "trades.csv"

    # Schema guard: if an existing trades.csv has a different column
    # set than what we're about to write, appending would produce a
    # malformed file (header with N cols, rows with M cols). When a
    # mismatch is detected, rotate the old file to
    # trades.archive-<date>.csv and start fresh so historical data is
    # preserved but today's rows stay consistent with the header.
    write_header = True
    if csv_path.exists():
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                existing_header = next(csv.reader(f), None)
        except (OSError, PermissionError) as exc:
            LOG.warning("Could not read existing trades.csv header: %s", exc)
            existing_header = None
        if existing_header == TRADE_CSV_COLUMNS:
            write_header = False
        else:
            archive = csv_path.with_name(f"trades.archive-{session_date}.csv")
            # If today already archived once (rare), suffix with a counter.
            counter = 2
            while archive.exists():
                archive = csv_path.with_name(f"trades.archive-{session_date}-{counter}.csv")
                counter += 1
            LOG.warning(
                "trades.csv schema changed (old=%s cols, new=%d cols). "
                "Rotating existing file to %s and writing today's trades to a fresh trades.csv.",
                len(existing_header) if existing_header else "?",
                len(TRADE_CSV_COLUMNS),
                archive.name,
            )
            try:
                csv_path.rename(archive)
            except (OSError, PermissionError) as exc:
                LOG.warning("Could not rotate trades.csv to %s: %s", archive, exc)
                return

    try:
        f = open(csv_path, "a", newline="", encoding="utf-8")
    except (OSError, PermissionError) as exc:
        LOG.warning("Could not open trades.csv for append: %s", exc)
        return
    try:
        writer = csv.DictWriter(f, fieldnames=TRADE_CSV_COLUMNS, extrasaction="raise")
        if write_header:
            writer.writeheader()
        for trade in closed:
            # ValueError from extrasaction="raise" propagates — field-drift is a bug.
            writer.writerow(_trade_csv_row(trade, session_date))
    finally:
        f.close()
    LOG.info("Session trades appended to %s (%d rows)", csv_path, len(closed))


# ---------------------------------------------------------------------------
# Per-day archive
# ---------------------------------------------------------------------------

_SECRET_KEYS = frozenset({
    "app_key",
    "app_secret",
    "account_hash",
    "encryption",
    "encryption_key",
    "refresh_token",
    "access_token",
    "sessionid",
    "session_id",
    "auth_token",
    "twilio_sid",
    "twilio_auth_token",
    "webhook_url",
    "secret",
})


def _redact_secrets(obj: Any) -> Any:
    """Recursively replace values whose key looks like a secret with '[REDACTED]'."""
    if isinstance(obj, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in _SECRET_KEYS else _redact_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_secrets(x) for x in obj]
    if isinstance(obj, tuple):
        return [_redact_secrets(x) for x in obj]
    return obj


def _config_to_dict(config: Any) -> dict:
    """Convert a config dataclass tree to a redacted dict ready for YAML."""
    from dataclasses import asdict, is_dataclass
    if is_dataclass(config) and not isinstance(config, type):
        raw = asdict(config)
    elif isinstance(config, dict):
        raw = dict(config)
    else:
        # Fallback: walk __dict__ if available
        raw = getattr(config, "__dict__", {}) or {}
    return _redact_secrets(raw)


# Recognized structured-event prefixes emitted by engine._log_structured.
# Used by the events.jsonl extractor.
_STRUCTURED_PREFIXES = (
    "ENTRY_CONTEXT", "EXIT_CONTEXT", "TRADE_SUMMARY",
    "SKIP_SUMMARY", "SESSION_REPORT", "POSITION_ADJUSTMENT",
    "ENTRY_CYCLE_SUMMARY",
)


def _extract_structured_events(log_path: Path) -> list[dict]:
    """Scrape lines like '... PREFIX {json}' from the log file.

    Returns a list of {'event_type': ..., 'timestamp': ..., **payload}.
    Lines that don't match are silently ignored.
    """
    events: list[dict] = []
    if not log_path.exists():
        return events
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\b")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # Find the first known prefix in the line
                for prefix in _STRUCTURED_PREFIXES:
                    needle = f" {prefix} "
                    idx = line.find(needle)
                    if idx < 0:
                        continue
                    json_start = idx + len(needle)
                    json_text = line[json_start:].strip()
                    if not json_text or json_text[0] != "{":
                        continue
                    try:
                        payload = json.loads(json_text)
                    except json.JSONDecodeError:
                        continue
                    ts_match = ts_re.match(line)
                    record = {"event_type": prefix}
                    if ts_match:
                        record["log_timestamp"] = ts_match.group(1)
                    if isinstance(payload, dict):
                        record.update(payload)
                    else:
                        record["payload"] = payload
                    events.append(record)
                    break
    except OSError as exc:
        LOG.warning("Could not read log for events extraction: %s", exc)
    return events


# Engine decision lines look like:
#   "... Decision symbol=TSLA strategy=top_tier_adaptive action=skipped
#    primary=... secondary=... side_pref=... family=... reasons=..."
# Reasons can contain spaces inside parens but the OTHER fields are
# space-separated key=value (value has no spaces).
_DECISION_FIELD_RE = re.compile(r"\b(symbol|strategy|action|primary|secondary|side_pref|family)=(\S+)")
_DECISION_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\b.*\bDecision\s+(.*)$")


def _extract_decisions(log_path: Path) -> list[dict]:
    """Scrape 'Decision symbol=... action=... reasons=...' lines into dicts."""
    rows: list[dict] = []
    if not log_path.exists():
        return rows
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _DECISION_LINE_RE.match(line)
                if not m:
                    continue
                row = {"timestamp": m.group(1)}
                tail = m.group(2)
                # Split off the reasons portion FIRST so reason text (which
                # can contain arbitrary 'key=value' fragments like
                # 'last_high=na,last_low=HL') can't shadow the actual
                # field values. None of today's reason strings include
                # symbol/strategy/action/primary/secondary/side_pref/family
                # tokens but a future skip reason could.
                reasons_idx = tail.find(" reasons=")
                if reasons_idx >= 0:
                    head = tail[:reasons_idx]
                    row["reasons"] = tail[reasons_idx + len(" reasons="):].strip()
                else:
                    head = tail
                for field, value in _DECISION_FIELD_RE.findall(head):
                    row[field] = value
                rows.append(row)
    except OSError as exc:
        LOG.warning("Could not read log for decision extraction: %s", exc)
    return rows


_AMBIGUOUS_REGIME_RE = re.compile(
    r"ambiguous_regime\(top=(?P<top>\w+),top_score=(?P<top_score>[-\d.]+),"
    r"second=(?P<second>\w+),second_score=(?P<second_score>[-\d.]+),"
    r"required_top_score>=(?P<req_top>[-\d.]+),"
    r"required_score_gap>=(?P<req_gap>[-\d.]+),"
    r"current_score_gap=(?P<gap>[-\d.]+)\)"
)


def _load_archive_bars(bars_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Per-symbol 1m timelines from an archive's ``bars/1m/``.

    Only ts/high/low/close/atr14 — the fields every forward-looking
    classifier here needs. Bar timestamps are tz-aware ISO
    ("2026-05-21T11:12:00-04:00") while decisions.csv timestamps are naive
    ("2026-05-21 11:12:00,798"), so these are normalised to naive ET to make
    the two comparable.

    Shared by ``_regime_call_outcomes`` and ``_gate_attribution``; both ask
    the same question (what did price do after this decision) of the same
    files, and a second copy of the parsing would be a second place for the
    timestamp normalisation to drift.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    if not bars_dir.is_dir():
        return out
    for path in bars_dir.glob("*.csv"):
        try:
            rows: list[dict[str, Any]] = []
            with open(path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        ts = datetime.fromisoformat(row.get("timestamp", "")).replace(tzinfo=None)
                    except (ValueError, TypeError):
                        continue
                    try:
                        high = float(row.get("high"))
                        low = float(row.get("low"))
                        close = float(row.get("close"))
                    except (TypeError, ValueError):
                        continue
                    try:
                        atr14 = float(row.get("atr14") or 0.0)
                    except (TypeError, ValueError):
                        atr14 = 0.0
                    rows.append({"ts": ts, "high": high, "low": low,
                                 "close": close, "atr14": atr14})
            if rows:
                rows.sort(key=lambda r: r["ts"])
                out[path.stem] = rows
        except (OSError, csv.Error):
            continue
    return out


def _forward_excursion_atr(
    rows: list[dict[str, Any]], at: datetime, window_minutes: int,
) -> tuple[float, float, float] | None:
    """``(up_atr, down_atr, net_atr)`` over ``(at, at + window]``, or None.

    All three measured from the close of the first bar at or after ``at`` and
    scaled by the ATR as of ``at``, so they are comparable across symbols and
    price levels. None when there are too few forward bars or no usable ATR —
    callers must treat that as UNEVALUATED, not as zero movement.

    ``net_atr`` is the close-to-close move and is the one worth reading. The
    EXCURSIONS are nearly useless on their own at this window: sampled over
    random 30-minute windows on this bot's universe, the median up-excursion
    is 2.24 ATR and 77% of windows touch at least 1 ATR up — because a 1m
    ATR-14 measures fourteen minutes of range and the window is thirty. Any
    "% that moved 1 ATR our way" therefore sits near chance no matter what
    produced the window. The net move has a real baseline: +0.07 ATR over the
    same sample, i.e. zero.
    """
    window_end = at + timedelta(minutes=int(window_minutes))
    after = [r for r in rows if at <= r["ts"] <= window_end]
    prior = [r for r in rows if r["ts"] <= at]
    if len(after) < 2 or not prior:
        return None
    atr = prior[-1]["atr14"]
    if not atr or atr <= 0 or not math.isfinite(atr):
        return None
    p0 = after[0]["close"]
    up_atr = (max(r["high"] for r in after) - p0) / atr
    down_atr = (p0 - min(r["low"] for r in after)) / atr
    net_atr = (after[-1]["close"] - p0) / atr
    if not all(math.isfinite(v) for v in (up_atr, down_atr, net_atr)):
        return None
    return max(0.0, up_atr), max(0.0, down_atr), net_atr


def _forward_baseline(
    bars_by_sym: dict[str, list[dict[str, Any]]], window_minutes: int, stride: int = 7,
) -> dict[str, Any]:
    """The unconditional forward move, sampled from the SAME session's bars.

    Without this every gate statistic is unreadable. A gate whose blocks were
    followed by a +0.3 ATR net move only matters if an arbitrary moment in the
    same session was not also followed by +0.3. Sampling from the session being
    reported also absorbs the day's character: a trending day lifts every
    LONG-side number, and only the gap above baseline is evidence.

    Measured from a LONG viewpoint, so a SHORT gate's favourable direction is
    the mirror of ``up_pct``.
    """
    nets: list[float] = []
    for rows in bars_by_sym.values():
        for i in range(0, max(0, len(rows) - window_minutes - 1), max(1, stride)):
            excursion = _forward_excursion_atr(rows, rows[i]["ts"], window_minutes)
            if excursion is not None:
                nets.append(excursion[2])
    if not nets:
        return {"samples": 0, "median_net_atr": None, "up_pct": None}
    ordered = sorted(nets)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    return {
        "samples": len(nets),
        "median_net_atr": round(median, 3),
        "up_pct": round(100.0 * sum(1 for v in nets if v > 0) / len(nets), 1),
    }


def _gate_attribution(
    archive_root: Path, window_minutes: int = 30, min_samples: int = 20,
) -> dict[str, Any]:
    """What did price do AFTER each gate blocked a candidate?

    Every skip reason is currently a count: `no_fresh_breakout` fired 22,231
    times across 13 sessions, ~20% of all RTH decisions. A count says how much
    work a gate did, never whether the work was worth doing. Each gate was
    added in response to a specific loss, and none has been measured since.

    For every skipped decision this takes the forward excursion over
    ``window_minutes``, in ATR, split into the direction the bot was ABOUT to
    trade (favourable) and the opposite (adverse), and buckets it by skip
    reason. A gate whose blocks are consistently followed by a favourable move
    is costing money; one whose blocks are followed by adverse moves is
    earning its place.

    WHAT THIS IS NOT: a backtest. It measures PRICE MOVEMENT after the block,
    with no stop, no target, no sizing and no slippage. `favourable_1atr_pct`
    of 60 does not mean 60% of those trades would have won — the stop might
    have been hit first. Read it as "the gate blocked a move", not "the gate
    blocked a winner".

    Reasons are normalised through ``_normalize_skip_reason``, so the numeric
    detail that fragments `session_skip_counts` into hundreds of near-
    duplicates rolls up. Decisions are deduped by (symbol, minute, reason)
    because one decision is logged repeatedly across a cycle.

    Returns {} on any I/O or parse failure — never crashes the archive write.
    """
    try:
        decisions_path = archive_root / "decisions.csv"
        bars_by_sym = _load_archive_bars(archive_root / "bars" / "1m")
        if not decisions_path.exists() or not bars_by_sym:
            return {}

        net_moves: dict[str, list[float]] = defaultdict(list)
        favourable: dict[str, list[float]] = defaultdict(list)
        adverse: dict[str, list[float]] = defaultdict(list)
        families: dict[str, Counter] = defaultdict(Counter)
        blocked = Counter()
        unevaluated = Counter()
        seen: set[tuple[str, datetime, str]] = set()

        with open(decisions_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if str(row.get("action", "")).strip().lower() != "skipped":
                    continue
                symbol = str(row.get("symbol", "") or "")
                rows = bars_by_sym.get(symbol)
                if not rows:
                    continue
                primary = str(row.get("primary", "") or "").strip()
                if not primary or primary == "none":
                    continue
                reason = _normalize_skip_reason(primary)
                ts_raw = str(row.get("timestamp", "")).strip('"').split(",")[0]
                try:
                    ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                key = (symbol, ts.replace(second=0), reason)
                if key in seen:
                    continue
                seen.add(key)

                # Which way was the bot about to trade? `side_pref` when the
                # gatekeeper resolved one, else the reason's own side prefix
                # (`long_build_failed_...`). Without a side there is no
                # "favourable" direction and the row cannot be scored.
                side = str(row.get("side_pref", "") or "").strip().upper()
                if side not in {"LONG", "SHORT"}:
                    lowered = primary.lower()
                    if lowered.startswith("long_"):
                        side = "LONG"
                    elif lowered.startswith("short_"):
                        side = "SHORT"
                    else:
                        continue

                blocked[reason] += 1
                family = str(row.get("family", "") or "none").strip() or "none"
                families[reason][family] += 1

                excursion = _forward_excursion_atr(rows, ts, window_minutes)
                if excursion is None:
                    unevaluated[reason] += 1
                    continue
                up_atr, down_atr, net_atr = excursion
                if side == "LONG":
                    favourable[reason].append(up_atr)
                    adverse[reason].append(down_atr)
                    net_moves[reason].append(net_atr)
                else:
                    favourable[reason].append(down_atr)
                    adverse[reason].append(up_atr)
                    net_moves[reason].append(-net_atr)

        if not blocked:
            return {}

        def _median(values: list[float]) -> float:
            ordered = sorted(values)
            mid = len(ordered) // 2
            if len(ordered) % 2:
                return ordered[mid]
            return (ordered[mid - 1] + ordered[mid]) / 2.0

        by_reason: dict[str, dict[str, Any]] = {}
        for reason, count in blocked.items():
            nets = net_moves.get(reason, [])
            fav = favourable.get(reason, [])
            adv = adverse.get(reason, [])
            entry: dict[str, Any] = {
                "blocked": int(count),
                "evaluated": len(nets),
                "unevaluated": int(unevaluated.get(reason, 0)),
                "regimes": dict(families[reason].most_common(4)),
                # The headline: net close-to-close move toward the side the bot
                # wanted. Baseline is ~0, so a positive number is evidence the
                # gate blocked a move that was going to happen anyway.
                "median_net_atr": None,
                "favourable_pct": None,
                # Secondary, and near chance at this window (77% of random
                # 30-minute windows touch 1 ATR up). Kept because a wide
                # favourable excursion against a flat net says "it went our way
                # and came back", which is a stop-placement story rather than an
                # entry one.
                "median_favourable_excursion_atr": None,
                "median_adverse_excursion_atr": None,
            }
            if nets:
                entry.update({
                    "median_net_atr": round(_median(nets), 3),
                    "favourable_pct": round(
                        100.0 * sum(1 for v in nets if v > 0) / len(nets), 1),
                    "median_favourable_excursion_atr": round(_median(fav), 3),
                    "median_adverse_excursion_atr": round(_median(adv), 3),
                })
            by_reason[reason] = entry

        # Rank by blocks x favourable edge: a gate that fires rarely cannot
        # cost much however wrong it is, and one that fires constantly with no
        # directional edge is not costing anything either.
        baseline_stats = _forward_baseline(bars_by_sym, window_minutes)
        baseline_net = float(baseline_stats.get("median_net_atr") or 0.0)

        def _cost(item: tuple[str, dict[str, Any]]) -> float:
            """Blocks x edge ABOVE baseline, for gates with enough samples.

            A gate that fires rarely cannot cost much however wrong it is, and
            one that fires constantly with no edge over an arbitrary moment is
            not costing anything either.

            ``min_samples`` is what keeps this honest. A reason seen once has a
            "median" of that single observation, and on real data those produce
            the largest numbers in the table (+7.4 ATR off 11 blocks, +7.4 off
            1) purely because nothing averages out. They stay in ``by_reason``
            — nothing is hidden — but they must not head a list labelled
            "costliest".
            """
            entry = item[1]
            net = entry.get("median_net_atr")
            if net is None or int(entry.get("evaluated", 0)) < min_samples:
                return 0.0
            edge = float(net) - baseline_net
            return edge * int(entry["blocked"]) if edge > 0 else 0.0

        ranked = sorted(by_reason.items(), key=_cost, reverse=True)
        return {
            "window_minutes": int(window_minutes),
            "min_samples_for_ranking": int(min_samples),
            "decisions_scored": int(sum(blocked.values())),
            "measures": ("net forward price movement against a same-session "
                         "baseline - NOT a backtest: no stop, target, sizing "
                         "or slippage"),
            "baseline": baseline_stats,
            "costliest_gates": [
                {
                    "gate": name,
                    "blocked": entry["blocked"],
                    "net_atr": entry["median_net_atr"],
                    "edge_over_baseline_atr": round(
                        float(entry["median_net_atr"]) - baseline_net, 3),
                }
                for name, entry in ranked[:8] if _cost((name, entry)) > 0
            ],
            "by_reason": dict(ranked),
        }
    except Exception as exc:
        LOG.warning("Could not compute gate attribution: %s", exc, exc_info=True)
        return {}


def _regime_call_outcomes(archive_root: Path) -> dict[str, Any]:
    """Classify each ``ambiguous_regime`` decision against the 30-minute
    forward price move and bucket results by outcome + hour.

    Purpose: regime scoring is the strategy's directional bet. When it
    says ``bullish_trend@4.50`` but the price drops 3 ATR in the next
    30 minutes, that's a regression signal worth knowing about. This
    helper writes the daily classification into ``manifest.json`` so a
    week-over-week comparison surfaces drift (e.g. "right%" trending
    below 30%) without anyone running ad-hoc post-mortems.

    Classifier per call:
      * ``right`` — top was ``*_trend`` and price moved >= 1 ATR in
        that direction within the next 30 minutes.
      * ``wrong`` — opposite direction had a larger excursion.
      * ``flat`` — neither direction reached 1 ATR (or top was
        ``range``/non-directional).
      * ``unclear`` — insufficient forward bars or missing ATR.

    Reads from the already-written ``decisions.csv`` and the per-symbol
    1m bars under ``bars/1m/`` in the same archive directory. Dedupes
    calls by (symbol, minute, top_score) so a single decision repeated
    many times in one cycle is counted once.

    Returns an empty dict on any I/O / parse failure — never crashes
    the archive write.
    """
    try:
        decisions_path = archive_root / "decisions.csv"
        bars_dir = archive_root / "bars" / "1m"
        if not decisions_path.exists() or not bars_dir.is_dir():
            return {}

        bars_by_sym = _load_archive_bars(bars_dir)
        if not bars_by_sym:
            return {}

        # Parse ambiguous_regime calls, dedupe by (sym, minute, top_score).
        calls: list[dict[str, Any]] = []
        seen: set[tuple[str, datetime, float]] = set()
        try:
            with open(decisions_path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    sym = row.get("symbol", "")
                    if sym not in bars_by_sym:
                        continue
                    m = _AMBIGUOUS_REGIME_RE.search(row.get("primary", ""))
                    if not m:
                        continue
                    ts_raw = row.get("timestamp", "").strip('"').split(",")[0]
                    try:
                        ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    try:
                        top_score = float(m.group("top_score"))
                        gap = float(m.group("gap"))
                    except ValueError:
                        continue
                    key = (sym, ts.replace(second=0), round(top_score, 2))
                    if key in seen:
                        continue
                    seen.add(key)
                    calls.append({
                        "ts": ts, "sym": sym,
                        "top": m.group("top"),
                        "top_score": top_score,
                        "gap": gap,
                    })
        except OSError:
            return {}

        # Classify and aggregate.
        by_outcome = {"right": 0, "wrong": 0, "flat": 0, "unclear": 0}
        by_hour: dict[str, dict[str, int]] = defaultdict(
            lambda: {"total": 0, "right": 0, "wrong": 0, "flat": 0, "unclear": 0}
        )

        for call in calls:
            rows = bars_by_sym[call["sym"]]
            call_ts = call["ts"]
            window_end = call_ts + timedelta(minutes=30)
            after = [r for r in rows if call_ts <= r["ts"] <= window_end]
            prior = [r for r in rows if r["ts"] <= call_ts]
            outcome = "unclear"
            if len(after) >= 2 and prior:
                atr = prior[-1]["atr14"]
                if atr and atr > 0:
                    p0 = after[0]["close"]
                    high_max = max(r["high"] for r in after)
                    low_min = min(r["low"] for r in after)
                    up_atr = (high_max - p0) / atr
                    down_atr = (p0 - low_min) / atr
                    top = call["top"]
                    if top == "bullish_trend":
                        if up_atr >= 1.0 and up_atr > down_atr:
                            outcome = "right"
                        elif down_atr > up_atr:
                            outcome = "wrong"
                        else:
                            outcome = "flat"
                    elif top == "bearish_trend":
                        if down_atr >= 1.0 and down_atr > up_atr:
                            outcome = "right"
                        elif up_atr > down_atr:
                            outcome = "wrong"
                        else:
                            outcome = "flat"
                    else:
                        # range / non-directional top — not predictive
                        outcome = "flat"
            by_outcome[outcome] += 1
            hour = call_ts.strftime("%H")
            by_hour[hour]["total"] += 1
            by_hour[hour][outcome] += 1

        # Derived percentage (right vs evaluable calls). evaluable =
        # not unclear, and at least one directional outcome possible.
        directional = by_outcome["right"] + by_outcome["wrong"] + by_outcome["flat"]
        right_pct = (by_outcome["right"] / directional) if directional > 0 else None

        return {
            "total_unique_calls": len(calls),
            "by_outcome": by_outcome,
            "right_pct_of_directional": round(right_pct, 4) if right_pct is not None else None,
            "by_hour": {h: dict(d) for h, d in sorted(by_hour.items())},
        }
    except Exception as exc:
        LOG.warning("Could not compute regime-call outcomes: %s", exc, exc_info=True)
        return {}


def export_session_archive(
    *,
    log_dir: str,
    strategy_name: str,
    dry_run: bool,
    data: Any,
    account: Any,
    positions: dict[str, Position],
    strategy: Any,
    last_candidates: Iterable[Any] | None,
    session_skip_counts: dict[str, int] | None = None,
    config: Any | None = None,
) -> None:
    """Write a per-day archive of bars / trades / log / manifest to
    ``{log_dir}/sessions/{YYYY-MM-DD}/`` for post-session analysis.

    Contents:
    - ``bars/{N}m/{SYMBOL}.csv`` — full merged frame (history + live,
      warmup + pre-market + RTH + post-market) with all indicators for
      every active watchlist symbol. One subfolder per timeframe actually
      used by the strategy: always ``1m`` plus ``ltf_minutes``
      and ``htf_minutes`` if they're set and > 1. For
      top_tier_adaptive that's ``bars/1m/``, ``bars/5m/``, ``bars/15m/``.
      The full frame is written so debuggers can reconstruct the bot's
      view at any moment in the session — indicators like 15m ema20 need
      5+ hours of warmup bars that filtering to today would drop.
    - ``trades.csv`` — today's trades filtered from the cumulative
      trades.csv (entry/exit/PnL/MFE/MAE per trade).
    - ``bot_{YYYY-MM-DD}.log`` — copy of the daily log file (original
      stays in log_dir; copying avoids file-lock issues on Windows where
      the FileHandler still owns the original).
    - ``config_snapshot.yaml`` — the resolved BotConfig (with secrets
      redacted) so future audits can reproduce decisions even if
      config.yaml has been edited since.
    - ``account_snapshot.json`` — full PaperAccount snapshot at the
      moment of export (end-of-day daily fire or shutdown): equity
      curve, realized PnL by symbol, open positions, etc.
    - ``events.jsonl`` — structured events (ENTRY_CONTEXT, EXIT_CONTEXT,
      TRADE_SUMMARY, SKIP_SUMMARY) extracted from the log file as
      one-per-line JSON. Easier to parse with jq/pandas than grepping
      the raw text log.
    - ``decisions.csv`` — every entry-decision event from the engine as
      a queryable CSV (timestamp, symbol, action, regime, primary/
      secondary skip reasons).
    - ``manifest.json`` — strategy, dry_run, summary stats, skip counts,
      timeframes exported, write-flags for each archive component.

    Parameters
    ----------
    log_dir
        Path to the bot's log directory (where bars/, trades.csv,
        bot_*.log already live). The archive subdirectory is created
        under ``{log_dir}/sessions/``.
    strategy_name, dry_run
        Recorded in manifest for later auditability.
    data
        DataFeed instance — used via ``data.get_merged(symbol, timeframe)``
        to pull the merged history+live frame for each symbol.
    account
        PaperAccount (or live account tracker). Used to read
        ``account.realized_pnl`` and ``account.trades`` so closed-position
        symbols are included even if they left the watchlist.
    positions
        Currently-open positions at the moment of export. On the
        end-of-day daily fire (8pm ET) this is whatever the bot is
        holding overnight; on shutdown it's typically empty after
        force-flatten.
    strategy
        Strategy instance — used for ``strategy.active_watchlist(...)``
        and ``strategy.params`` (to read trigger/HTF timeframes).
    last_candidates
        The most recent candidate list from the engine; passed to
        ``active_watchlist`` so dynamic-discovery strategies emit the
        right set of symbols.
    session_skip_counts
        Engine's session-wide skip-reason tally. Recorded in manifest.
    config
        Optional resolved BotConfig instance. If provided (and the
        ``yaml`` package is importable), a ``config_snapshot.yaml`` is
        written to the archive with secret fields (app_key, app_secret,
        account_hash, encryption_key, sessionid, etc.) redacted. Pass
        None to skip the snapshot.
    """
    import shutil

    session_date = now_et().date()
    log_dir_path = Path(str(log_dir or ".logs"))
    archive_root = log_dir_path / "sessions" / session_date.isoformat()
    bars_dir = archive_root / "bars"
    try:
        bars_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        LOG.warning("Could not create session archive directory %s: %s", archive_root, exc)
        return

    # Collect symbols we care about: active watchlist + index symbols
    # + any symbol with a position today (in case it left the watchlist).
    symbols: set[str] = set()
    try:
        watch = strategy.active_watchlist(list(last_candidates or []), positions or {})
        for sym in watch or set():
            key = str(sym or "").upper().strip()
            if key:
                symbols.add(key)
    except Exception:
        pass
    for pos in (positions or {}).values():
        underlying = str((pos.metadata or {}).get("underlying") or pos.symbol or "").upper().strip()
        if underlying:
            symbols.add(underlying)
    # Pull symbols from today's trades too, so closed positions still get bars saved.
    if account is not None:
        for trade in list(getattr(account, "trades", []) or []):
            ticker = str(getattr(trade, "underlying", None) or getattr(trade, "symbol", "") or "").upper().strip()
            if ticker:
                symbols.add(ticker)

    # Determine which timeframes to export. Always include 1m. If the
    # active strategy uses a different trigger or HTF timeframe, include
    # those too — they're what the bot actually computed signals from.
    # Skip any timeframe that's effectively 1m (≤1) or duplicates 1m.
    timeframes_min: set[int] = {1}
    strategy_params = getattr(strategy, "params", {}) or {}
    for key in ("ltf_minutes", "htf_minutes"):
        raw = strategy_params.get(key) if isinstance(strategy_params, dict) else None
        try:
            tf = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            tf = 0
        if tf > 1:
            timeframes_min.add(tf)
    timeframes_sorted = sorted(timeframes_min)

    # Export the FULL merged frame for each timeframe, no filters. This
    # captures everything the bot had access to: warmup history (needed
    # to compute indicators like ema20/atr14 — the HTF in particular
    # needs many bars from prior sessions), pre-market, RTH, and any
    # post-market data the feed accumulated. A reconstructed view of
    # what the bot saw at any moment during the session requires the
    # warmup bars; filtering to today's RTH would silently drop them.
    bars_written_by_tf: dict[str, int] = {}
    bars_skipped_by_tf: dict[str, int] = {}

    for tf_min in timeframes_sorted:
        tf_label = f"{tf_min}m"
        tf_dir = bars_dir / tf_label
        try:
            tf_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            LOG.warning("Could not create timeframe dir %s: %s", tf_dir, exc)
            continue
        tf_arg = "1min" if tf_min == 1 else f"{tf_min}min"
        written = 0
        skipped = 0
        for symbol in sorted(symbols):
            try:
                frame = data.get_merged(symbol, timeframe=tf_arg, with_indicators=True) if data is not None else None
            except Exception:
                frame = None
            if frame is None or frame.empty:
                skipped += 1
                continue
            out_path = tf_dir / f"{symbol}.csv"
            try:
                frame.to_csv(out_path, index_label="timestamp")
                written += 1
            except Exception as exc:
                LOG.warning("Could not write bars CSV for %s/%s: %s", symbol, tf_label, exc)
                skipped += 1
        bars_written_by_tf[tf_label] = written
        bars_skipped_by_tf[tf_label] = skipped

    # Aggregate counts for the manifest summary.
    bars_written = sum(bars_written_by_tf.values())
    bars_skipped = sum(bars_skipped_by_tf.values())

    # Copy the daily bot log into the archive. The original FileHandler
    # still has the file open (especially on Windows where moving a
    # locked file fails), so we COPY rather than MOVE. The original
    # at log_dir/bot_YYYY-MM-DD.log stays in place; the copy in the
    # archive is a permanent record. Run a separate cleanup script
    # later if you want to prune the originals from log_dir root.
    log_src = log_dir_path / f"bot_{session_date.isoformat()}.log"
    log_dst = archive_root / f"bot_{session_date.isoformat()}.log"
    log_copied = False
    if log_src.exists():
        try:
            # Flush log handlers first so the copy includes the
            # latest in-memory buffered log lines.
            for handler in logging.getLogger().handlers:
                try:
                    handler.flush()
                except Exception:
                    pass
            shutil.copy2(log_src, log_dst)
            log_copied = True
        except Exception as exc:
            LOG.warning("Could not copy daily log %s: %s", log_src, exc)

    # Write today's closed trades directly from the account's in-memory
    # trade history. We previously filtered the cumulative trades.csv,
    # but that file is only appended-to by write_session_report() (which
    # runs on bot shutdown). When the daily end-of-day archive fires at
    # ~16:00 ET via _maybe_export_session_archive, the cumulative CSV
    # still has yesterday's last shutdown state — so today's trades
    # never made it into the archive (observed live 2026-05-20: 2
    # SPY credit-spread closes in account + log, 0 rows in archive
    # trades.csv).
    #
    # account.trades is the source of truth. final_exit guards against
    # partial-exit interim rows, and the ET-date filter matches the
    # per-day boundary the archive uses everywhere else.
    trades_dst = archive_root / "trades.csv"
    trades_today = 0
    # Today's realized PnL, summed from the SAME date-filtered list that
    # produces trades.csv and trades_today.
    #
    # The manifest used to report `account.realized_pnl`, which is a LIFETIME
    # accumulator — set to 0.0 once in PaperAccount.__init__ and only ever
    # incremented, with no per-day reset. So an always-on bot carried prior
    # days forward into a field sitting next to `trades_today`, which is
    # date-filtered. Two scopes in one manifest. Observed on 2026-07-31:
    # manifest -80.77 against trades.csv -60.22, a gap of exactly -20.55 =
    # the previous session's PnL. Five of the ten sessions that traded
    # disagreed with their own trades.csv, in both directions.
    #
    # Summing the rounded per-row values (not rounding the sum) is
    # deliberate: it is what _trade_csv_row writes, so
    # `manifest.realized_pnl == sum(trades.csv.realized_pnl)` holds exactly
    # rather than within a cent.
    #
    # Stays None when there is no account, preserving the previous contract.
    realized_pnl_today: float | None = None if account is None else 0.0
    trades_export_error: str | None = None
    session_date_str = session_date.isoformat()
    if account is not None:
        try:
            closed_today: list[TradeRecord] = []
            for trade in getattr(account, "trades", []) or []:
                if not bool(getattr(trade, "final_exit", True)):
                    continue
                exit_time = getattr(trade, "exit_time", None)
                if exit_time is None:
                    continue
                exit_date = None
                try:
                    exit_date = exit_time.astimezone(now_et().tzinfo).date()
                except Exception:
                    LOG.debug("Could not normalize exit_time for trade %s; falling back to naive date()", trade, exc_info=True)
                    # Naive datetime case — fall back to direct .date()
                    # without tz translation. Anything that doesn't have
                    # a .date() method (corrupt type) leaves exit_date
                    # as None, which won't match session_date and the
                    # trade is silently skipped (better than crashing).
                    try:
                        exit_date = exit_time.date()
                    except (AttributeError, TypeError):
                        pass
                if exit_date == session_date:
                    closed_today.append(trade)
            with open(trades_dst, "w", newline="", encoding="utf-8") as dst_fh:
                writer = csv.DictWriter(dst_fh, fieldnames=TRADE_CSV_COLUMNS, extrasaction="raise")
                writer.writeheader()
                for trade in closed_today:
                    writer.writerow(_trade_csv_row(trade, session_date_str))
            trades_today = len(closed_today)
            realized_pnl_today = round(
                sum(round(float(trade.realized_pnl), 2) for trade in closed_today), 2
            )
        except Exception as exc:
            LOG.warning("Could not write daily trades CSV from account: %s", exc, exc_info=True)
            # Report UNKNOWN, not flat. Leaving the initialized 0.0 in place
            # made a failed export indistinguishable in the manifest from a
            # genuinely flat day — and the failure is easy to hit, because
            # `_trade_csv_row` reads every TradeRecord field by name, so one
            # record missing a field added later (rehydrated from an older
            # store, say) raises here and is swallowed. A wrong-but-plausible
            # zero is worse than an absent value: nobody investigates a zero.
            trades_export_error = str(exc)
            realized_pnl_today = None

    # Config snapshot: dump the resolved config (with secrets redacted)
    # so future audits can reproduce decisions even if config.yaml has
    # been edited since. Skips silently if no config was passed in.
    config_snapshot_written = False
    if config is not None and _yaml is not None:
        try:
            cfg_dict = _config_to_dict(config)
            with open(archive_root / "config_snapshot.yaml", "w") as fh:
                _yaml.safe_dump(cfg_dict, fh, sort_keys=False, default_flow_style=False)
            config_snapshot_written = True
        except Exception as exc:
            LOG.warning("Could not write config snapshot: %s", exc)

    # Account snapshot: equity, realized PnL, per-symbol PnL, equity curve
    # — everything the PaperAccount knows at the moment of shutdown.
    account_snapshot_written = False
    if account is not None:
        try:
            snapshot = account.capture_snapshot(positions or {})
            # capture_snapshot returns a dict; serialize via json (default=str
            # to handle datetimes inside equity curve points).
            _atomic_write_text(
                archive_root / "account_snapshot.json",
                json.dumps(snapshot, indent=2, default=str),
            )
            account_snapshot_written = True
        except Exception as exc:
            LOG.warning("Could not write account snapshot: %s", exc)

    # Structured events extracted from the log (ENTRY_CONTEXT, EXIT_CONTEXT,
    # TRADE_SUMMARY, SKIP_SUMMARY, etc.) into JSON-lines for easy querying
    # with jq/pandas. We read from the COPIED log (log_dst) when it exists
    # so events.jsonl and bot_*.log in the archive reference the same
    # snapshot — no asymmetry between human-readable log and machine-
    # parseable events. Falls back to the original log_src if the copy
    # failed (best-effort).
    extraction_src = log_dst if log_copied and log_dst.exists() else log_src
    events_written = 0
    try:
        events = _extract_structured_events(extraction_src)
        if events:
            events_path = archive_root / "events.jsonl"
            _atomic_write_text(
                events_path,
                "".join(json.dumps(ev, default=str) + "\n" for ev in events),
            )
            events_written = len(events)
    except Exception as exc:
        LOG.warning("Could not extract structured events: %s", exc)

    # Decisions log as CSV: one row per entry-decision event from the
    # engine. Source is the same as events.jsonl (copied log when
    # available) for archive self-consistency.
    decisions_written = 0
    try:
        decisions = _extract_decisions(extraction_src)
        if decisions:
            decisions_path = archive_root / "decisions.csv"
            cols = ["timestamp", "symbol", "strategy", "action", "primary",
                    "secondary", "side_pref", "family", "reasons"]
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            for row in decisions:
                writer.writerow({c: row.get(c, "") for c in cols})
            _atomic_write_text(decisions_path, buf.getvalue())
            decisions_written = len(decisions)
    except Exception as exc:
        LOG.warning("Could not extract decisions log: %s", exc)

    # Regime-call outcome classification (2026-05-21) — for each
    # ambiguous_regime decision today, classify whether the strategy's
    # top-regime read was right/wrong/flat against the 30-min forward
    # price move. Embedded in the manifest so day-over-day comparison
    # surfaces regime-scoring drift without ad-hoc post-mortems.
    # Reads from the already-written decisions.csv + bars/1m/ in this
    # archive. Returns {} on any failure — never breaks the manifest
    # write.
    regime_outcomes = _regime_call_outcomes(archive_root)
    # Runs after decisions.csv and the bars are written — it reads both.
    gate_attribution = _gate_attribution(archive_root)

    # Manifest with strategy + summary stats so future audits know
    # exactly which config produced these bars/trades.
    manifest = {
        "session_date": session_date.isoformat(),
        "strategy": str(strategy_name),
        "dry_run": bool(dry_run),
        "exported_at": now_et().isoformat(),
        "timeframes_exported": [f"{tf}m" for tf in timeframes_sorted],
        "symbols_exported": bars_written,
        "symbols_skipped": bars_skipped,
        "bars_written_by_timeframe": bars_written_by_tf,
        "bars_skipped_by_timeframe": bars_skipped_by_tf,
        "trades_today": trades_today,
        "log_file_copied": log_copied,
        "config_snapshot_written": config_snapshot_written,
        "account_snapshot_written": account_snapshot_written,
        "events_extracted": events_written,
        "decisions_extracted": decisions_written,
        "open_positions_at_close": len(positions or {}),
        "realized_pnl": realized_pnl_today,
        "trades_export_error": trades_export_error,
        "session_skip_counts": dict(session_skip_counts or {}),
        "regime_call_outcomes": regime_outcomes,
        "gate_attribution": gate_attribution,
    }
    manifest_path = archive_root / "manifest.json"
    try:
        _atomic_write_text(
            manifest_path,
            json.dumps(manifest, indent=2, default=str),
        )
    except Exception as exc:
        LOG.warning("Could not write session manifest: %s", exc)

    LOG.info(
        "Session archive written to %s (%d bars CSVs, %d trades, %d events, %d decisions, log_copied=%s)",
        archive_root, bars_written, trades_today, events_written, decisions_written, log_copied,
    )
