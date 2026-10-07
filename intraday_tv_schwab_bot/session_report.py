# SPDX-License-Identifier: MIT
"""End-of-session reporting: log summary, structured JSON, and persistent CSV trade log.

In addition to the headline numbers (PnL, win rate, profit factor), the report
aggregates closed trades along five axes to support strategy/config tuning:

  * per-regime        — how each setup type performed (trend/pullback/range/...)
  * per-symbol        — catches concentration issues and high-variance tickers
  * per-exit-reason   — surfaces leaky exit mechanisms (phantom stops, tight targets)
  * per-partial-exit-reason — what each scale-out / partial-fill slice booked
  * per-hour         — identifies dead zones in the trading day
  * MAE / MFE         — max adverse / favorable excursion in R-multiples
  * post-stop run     — how far price ran the trade's way AFTER the stop,
                        i.e. how much of a correctly-called move a shakeout cost
  * filter rejections — tally of skip reasons the engine logged during the session

All aggregate sections are emitted both in the human log (fixed-width tables)
and inside the SESSION_REPORT structured JSON payload (under top-level keys
``per_regime``, ``per_symbol``, ``per_exit_reason``, ``per_partial_exit_reason``,
``per_hour``, ``mae_mfe``, ``post_stop_continuation``, ``filter_rejections``) so
downstream tooling can parse them without re-scraping.

The persistent trades.csv is ``append_trades_csv``'s: every closed trade the
process holds, once, under its exit's ET date. The per-day archive under
``{log_dir}/sessions/`` (bars, decisions, the manifest's regime-call outcomes
and gate attribution) is ``session_archive``'s; its trades.csv is the day's
rows of the persistent trades.csv, the exporting strategy's.
"""
from __future__ import annotations

import csv
import dataclasses
import json
import logging
import math
import os
import shutil
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from .paper_account import PaperAccount, TradeRecord, closed_trade_lifecycles
from .models import Position
from .reasons import SKIP_COUNT_UNIT, exit_reason_code, reason_gate

LOG = logging.getLogger(__name__)

TRADE_CSV_COLUMNS = ["date"] + [f.name for f in dataclasses.fields(TradeRecord)]


def trade_csv_row(trade: TradeRecord, session_date: str) -> dict[str, Any]:
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
        "entry_limit_buffer_pct": _round_opt(trade.entry_limit_buffer_pct, 6),
        "realized_entry_risk": _round_opt(trade.realized_entry_risk, 4),
        "entry_risk_budget": _round_opt(trade.entry_risk_budget, 4),
        "entry_risk_overage_frac": _round_opt(trade.entry_risk_overage_frac, 6),
        "armed_retest_status": trade.armed_retest_status,
        "armed_retest_waited_minutes": _round_opt(trade.armed_retest_waited_minutes, 2),
        "partial_exit_reasons": "|".join(trade.partial_exit_reasons),
    }


# The columns that make one day's trades.csv row one trade's exit: its
# lifecycle (position key and entry time; the symbol and entry time stand in
# for a record that has none) and when it closed. A process's day closes and
# its shutdown, or two processes, can each hold a trade; its row is written
# once.
TRADE_CSV_KEY = ("lifecycle_id", "symbol", "entry_time", "exit_time")


def trade_csv_key(row: dict[str, Any]) -> tuple[str, ...]:
    """``row``'s ``TRADE_CSV_KEY`` as the file spells it, for a row read back
    from trades.csv and a ``trade_csv_row`` alike (csv writes None as "")."""
    return tuple("" if row.get(name) is None else str(row[name]) for name in TRADE_CSV_KEY)


def read_trade_rows(csv_path: Path, session_date: str) -> list[dict[str, str]]:
    """The rows of the persistent trades.csv dated ``session_date``, in file
    order; [] when there is no file.

    Raises OSError, csv.Error or UnicodeDecodeError on a file it cannot read,
    and ValueError when a row of that day sits under a header other than
    ``TRADE_CSV_COLUMNS`` (a file an older version wrote, which the next
    append of a trade rotates, carrying the rows of the day it closes):
    those rows cannot be read as the current columns.
    """
    if not csv_path.exists():
        return []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = [row for row in reader if row.get("date") == session_date]
        header = reader.fieldnames
    if rows and header != TRADE_CSV_COLUMNS:
        raise ValueError(
            f"{csv_path} holds {len(rows)} rows of {session_date} under a header of "
            f"{len(header or [])} columns, not the current {len(TRADE_CSV_COLUMNS)}")
    return rows


def _exit_date(trade: TradeRecord) -> date | None:
    """``trade``'s exit ET date; None, with a WARNING, when its exit
    timestamp cannot be read.

    Such a trade is DROPPED from the report, the archive and the trades.csv
    append alike. Keeping it looks like the more careful choice --
    "unevaluable is not the same as absent" -- and here it is the opposite:
    `trade_csv_row` calls `exit_time.isoformat()`, so one unreadable record
    raises inside the report's broad try/except and costs the ENTIRE report,
    every aggregate with it. Losing one row beats losing the session.
    """
    exit_time = getattr(trade, "exit_time", None)
    try:
        return exit_time.date()
    except (AttributeError, TypeError):
        LOG.warning(
            "Dropping %s from the day's trades: unreadable exit_time %r",
            getattr(trade, "symbol", "?"), exit_time,
        )
        return None


def _exited_on(trade: TradeRecord, day: date) -> bool:
    """Whether ``trade`` exited on the ET date ``day`` (``_exit_date``: a
    trade whose exit cannot be read did not)."""
    return _exit_date(trade) == day


def trades_closed_on(trades: Iterable[TradeRecord], day: date) -> list[TradeRecord]:
    """The trades that closed on ``day``, one record each:
    ``closed_trade_lifecycles`` folds a trade's partial exits into it, so no
    slice's P&L is lost and a trade is a win or a loss on its whole result."""
    return [t for t in closed_trade_lifecycles(trades) if _exited_on(t, day)]


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
    """Closed trades by the reason of their FINAL exit."""
    return {reason: _summarize_group(group) for reason, group in _group_by(trades, lambda t: exit_reason_code(t.reason) or "unknown").items()}


def _per_partial_exit_reason(slices: list[TradeRecord]) -> dict[str, dict[str, Any]]:
    """Partial-exit SLICES by their own reason: what each scale-out (and each
    broker partial fill) booked. A folded trade reports only its final
    reason, so without this a divergence scale-out never appeared in the
    exit tables at all (2026-09-24)."""
    partials = [t for t in slices if bool(t.partial_exit)]
    return {reason: _summarize_group(group) for reason, group in _group_by(partials, lambda t: exit_reason_code(t.reason) or "unknown").items()}


def _per_hour(trades: list[TradeRecord]) -> dict[str, dict[str, Any]]:
    def _hour_bucket(t: TradeRecord) -> str:
        # Bucket by ENTRY hour (local time). Entry time tells us when the
        # bot decided to trade; exit time is a product of management and
        # can drift long after entry.
        return f"{t.entry_time.hour:02d}:00"

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
        if (exit_reason_code(t.reason) or "").lower() == "stop"
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
        window = frame[(frame.index > at) & (frame.index <= at + timedelta(minutes=int(window_minutes)))]
        if window.empty:
            return None
        worst = float(window["low"].min()) if is_long else float(window["high"].max())
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
        session_mask = frame.index.date == trade.entry_time.date()
        session = frame[session_mask]
        rows = (session.iloc[::max(1, int(baseline_stride))]
                if not session.empty else None)
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


def _filter_rejection_summary(skip_counts: dict[str, int] | None) -> dict[str, Any]:
    """Shape the engine's raw skip-count dict into a stable, sorted payload.

    Two views are emitted:
      * ``top_reasons`` / ``all_reasons`` — grouped by gate
        (``reasons.reason_gate``: no ``long.`` / ``short.`` side prefix, no
        ``(...)`` or ``:...`` detail), the key gate attribution and the
        entry cycle summary use too. This is the view the operator reads
        for day-over-day comparison. Until 2026-09-26 only the ``(`` detail
        was cut, so every value of a ``:`` detail
        (``long_level_score_below_min:2.50<2.90``) was a bucket of its own,
        and the peer family's ``long.x`` / ``short.x`` two.
      * ``variants`` — the raw reasons as logged, preserved so a tuner
        can inspect the full parameter distribution (and the sides) of a
        specific bucket.

    The counts are symbol-minutes (``unit``, ``reasons.SKIP_COUNT_UNIT``): a
    symbol skipped on a gate on a side (``reasons.blocked_side``) in one
    minute counts once, however many entry passes ran in it, so a bucket
    counts the peer family's ``long.x`` and ``short.x`` of one minute twice,
    as gate attribution does. Until 2026-10-06 every pass counted, so the totals
    scaled with the pass rate.
    """
    if not skip_counts:
        return {"unit": SKIP_COUNT_UNIT, "total_skips": 0, "top_reasons": [], "all_reasons": {}, "variants": {}}
    total = sum(int(v) for v in skip_counts.values())
    # Group by normalized reason.
    normalized: dict[str, int] = {}
    variants: dict[str, dict[str, int]] = {}
    for reason, count in skip_counts.items():
        bucket = reason_gate(reason)
        normalized[bucket] = normalized.get(bucket, 0) + int(count)
        if bucket != reason:
            # Preserve the raw variant so tuning can see distributions.
            variants.setdefault(bucket, {})[str(reason)] = int(count)
    sorted_items = sorted(normalized.items(), key=lambda kv: (-kv[1], kv[0]))
    top = [{"reason": reason, "count": int(count)} for reason, count in sorted_items[:10]]
    all_ = {reason: int(count) for reason, count in sorted_items}
    return {"unit": SKIP_COUNT_UNIT, "total_skips": total, "top_reasons": top, "all_reasons": all_,
            "variants": variants}


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
    LOG.info("  Filter rejections (%d skips in %s, a symbol's gate on a side counted once a minute; showing top %d):",
             total, summary["unit"], min(10, len(summary.get("top_reasons", []))))
    for item in summary.get("top_reasons", []):
        LOG.info("    %-40s %6d", str(item.get("reason", ""))[:40], int(item.get("count", 0)))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def write_session_report(
    account: PaperAccount,
    positions: dict[str, Position],
    *,
    session_date: date,
    strategy: str,
    dry_run: bool,
    structured_logger: Any | None = None,
    skip_counts: dict[str, int] | None = None,
    bars_for: Any | None = None,
    post_stop_window_minutes: int = 30,
    entry_timing_window_minutes: int = 15,
) -> None:
    """Write ``session_date``'s end-of-session summary to the log and emit a
    structured SESSION_REPORT JSON payload containing per-regime /
    per-symbol / per-exit-reason / per-hour breakdowns, MAE/MFE aggregates,
    and the filter-rejection tally.

    The session is the ET date ``session_date``: the trades in ``account``
    that closed on it. The engine writes it once a day, when a trading day's
    stream window closes (20:00 ET) or at a shutdown before then
    (``IntradayBot._close_session_day``), and appends the trades to the
    persistent trades.csv after it (``append_trades_csv``). A failure is
    logged with its type and never raised: the summary is a log record.

    Parameters
    ----------
    account : PaperAccount
        The paper/live account tracker with trade history.
    positions : dict[str, Position]
        Currently open positions (should be empty at session end).
    session_date : date
        The ET trading day reported.
    strategy : str
        Active strategy name.
    dry_run : bool
        Whether the bot ran in dry-run mode.
    structured_logger : callable, optional
        A ``(prefix, payload)`` callable for structured JSON logging
        (e.g., ``engine._log_structured``).
    skip_counts : dict[str, int], optional
        Session-wide tally of per-candidate skip reasons from
        ``engine.session_skip_counts``, in symbol-minutes (a signal an engine
        gate refused counts under that gate alone). Used to emit the
        filter-rejection summary.
    """
    session_date_str = session_date.isoformat()
    try:
        performance = account.capture_snapshot(positions)
        trades = list(account.trades)

        # Scoped to the SESSION, not the account. `account.realized_pnl` is a
        # lifetime accumulator -- set to 0.0 once in `PaperAccount.__init__`,
        # only ever incremented, never reset per day -- and every headline
        # number below used to come from it while `trades` counted the list
        # beside it. Two scopes in one line, with the wrong one as the
        # headline. `manifest.realized_pnl` was fixed for exactly this on
        # 2026-09-19; this is the call site that fix missed.
        #
        # The date filter matters for the same reason it does in
        # `session_archive.export_session_archive`: a bot that runs across
        # midnight without restarting keeps the prior day's records in
        # `account.trades`, so without it the headline mixes days.
        # `trades_closed_on` folds a trade's partial exits into it and drops
        # a trade whose exit timestamp cannot be read (`_exited_on`).
        closed = trades_closed_on(trades, session_date)
        # The slices that closed part of a trade that day, whether or not
        # the rest of it has closed yet: their P&L is realized that day.
        partial_slices = [t for t in trades if bool(t.partial_exit) and _exited_on(t, session_date)]

        # --- Log summary ---
        wins = sum(1 for t in closed if t.realized_pnl > 0)
        losses = sum(1 for t in closed if t.realized_pnl < 0)
        # Summing the ROUNDED per-row values, so this equals the sum of
        # trades.csv exactly rather than to within a cent -- `trade_csv_row`
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
            session_date_str, strategy, total_pnl, len(closed), wins, losses,
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
        per_partial_exit_reason = _per_partial_exit_reason(partial_slices)
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
            if per_partial_exit_reason:
                _log_group_table("Per partial exit reason", per_partial_exit_reason, key_label="partial_exit_reason")
            _log_group_table("Per hour (entry)", per_hour, key_label="hour_et")
            _log_mae_mfe(mae_mfe)
            _log_post_stop_continuation(post_stop)
            _log_entry_timing(entry_timing)
        _log_filter_rejections(filter_rejections)

        # --- Structured JSON log ---
        report_payload = {
            "date": session_date_str,
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
            "per_partial_exit_reason": per_partial_exit_reason,
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
        LOG.warning("Could not write session report: %s: %s", type(exc).__name__, exc)


def append_trades_csv(trades: Iterable[TradeRecord], *, log_dir: str, session_date: date) -> bool:
    """Append to ``{log_dir}/trades.csv`` every closed trade in ``trades``
    that the file lacks, each under its exit's ET date; True when the file
    then holds every one of them.

    The engine calls it at each day close and at shutdown with every trade
    the process holds, whatever its exit date: a trade booked after the
    day's 20:00 report (a late start's start-up reconcile books an exit that
    evening) or one an earlier append failed to write goes in the next
    time. A row already in the file (the same date and ``TRADE_CSV_KEY``)
    is not written again, so a repeat, a shutdown after the 20:00 close, or
    a second process holding the same trade adds nothing. A trade still
    open (a partial exit without its final slice) waits for its close:
    ``closed_trade_lifecycles`` folds its slices into one row then.

    False, with a WARNING naming the error's type, when the log directory,
    the file, its rotation or the write fails: a file that cannot be read
    appends nothing, since its rows could not be told apart from ours. The
    caller retries. A row ``trade_csv_row`` builds with a column the header
    lacks raises ValueError (``extrasaction="raise"``): that is field drift,
    a bug, not an I/O error.

    Schema guard: a file under a header other than ``TRADE_CSV_COLUMNS``
    (an upgrade added or renamed a column) is copied to
    ``trades.archive-<session_date>.csv`` as it is, and replaced by a fresh
    file under the current header that starts with its rows of
    ``session_date``, mapped by column name (a new column empty, a column the
    header no longer has left out, named in the WARNING), so the day's
    archive and manifest still hold the rows an earlier process wrote that
    day. The fresh file is written before it replaces the old one, so a
    failure leaves the old file in place.
    """
    rows: list[dict[str, Any]] = []
    for trade in closed_trade_lifecycles(trades):
        exit_day = _exit_date(trade)
        if exit_day is not None:
            rows.append(trade_csv_row(trade, exit_day.isoformat()))
    if not rows:
        return True
    log_path = Path(log_dir)
    csv_path = log_path / "trades.csv"
    try:
        log_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOG.warning("Could not create log directory %s, so %d trades were not appended: %s: %s",
                    log_path, len(rows), type(exc).__name__, exc)
        return False

    day = session_date.isoformat()
    header: list[str] | None = None
    file_rows: list[dict[str, Any]] = []
    if csv_path.exists():
        try:
            with open(csv_path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                file_rows = list(reader)
                header = list(reader.fieldnames) if reader.fieldnames is not None else None
        except (OSError, csv.Error, UnicodeDecodeError) as exc:
            LOG.warning("Could not read %s, so %d trades were not appended: %s: %s",
                        csv_path, len(rows), type(exc).__name__, exc)
            return False
    rotate = csv_path.exists() and header != TRADE_CSV_COLUMNS
    carried: list[dict[str, Any]] = []
    if rotate:
        carried = [{name: row.get(name) or "" for name in TRADE_CSV_COLUMNS}
                   for row in file_rows if row.get("date") == day]
        file_rows = carried
    held = {(str(row.get("date") or ""), trade_csv_key(row)) for row in file_rows}
    new_rows = [row for row in rows if (row["date"], trade_csv_key(row)) not in held]
    if len(new_rows) < len(rows):
        LOG.info("%d of this process's %d trades already in %s", len(rows) - len(new_rows), len(rows), csv_path)
    if not new_rows:
        return True
    if rotate:
        return _rotate_trades_csv(csv_path, header, carried, new_rows, day)
    try:
        with open(csv_path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=TRADE_CSV_COLUMNS, extrasaction="raise")
            if header is None:
                writer.writeheader()
            for row in new_rows:
                # ValueError from extrasaction="raise" propagates — field-drift is a bug.
                writer.writerow(row)
    except OSError as exc:
        LOG.warning("Could not append %d trades to %s: %s: %s", len(new_rows), csv_path, type(exc).__name__, exc)
        return False
    LOG.info("Session trades appended to %s (%d rows)", csv_path, len(new_rows))
    return True


def _rotate_trades_csv(csv_path: Path, header: list[str] | None, carried: list[dict[str, Any]],
                       new_rows: list[dict[str, Any]], day: str) -> bool:
    """``append_trades_csv``'s schema guard: keep the old file as
    ``trades.archive-<day>[-N].csv`` and replace it with one under the
    current header holding ``carried`` (its rows of ``day``) and
    ``new_rows``."""
    archive = csv_path.with_name(f"trades.archive-{day}.csv")
    # If the day already rotated once (rare), suffix with a counter.
    counter = 2
    while archive.exists():
        archive = csv_path.with_name(f"trades.archive-{day}-{counter}.csv")
        counter += 1
    dropped = [name for name in (header or []) if name not in TRADE_CSV_COLUMNS]
    LOG.warning(
        "trades.csv schema changed (old=%s cols, new=%d cols). Rotating existing file to %s and writing a fresh "
        "trades.csv that starts with its %d rows of %s (new columns empty%s).",
        len(header) if header else "?", len(TRADE_CSV_COLUMNS), archive.name, len(carried), day,
        f"; left out: {', '.join(dropped)}" if dropped else "",
    )
    fresh = csv_path.with_name("trades.csv.rotating")
    try:
        with open(fresh, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=TRADE_CSV_COLUMNS, extrasaction="raise")
            writer.writeheader()
            writer.writerows(carried)
            writer.writerows(new_rows)
        shutil.copy2(csv_path, archive)
        os.replace(fresh, csv_path)
    except OSError as exc:
        LOG.warning("Could not rotate trades.csv to %s, so %d trades were not appended: %s: %s",
                    archive, len(new_rows), type(exc).__name__, exc)
        # Nothing replaced the old file: drop the copies of it.
        fresh.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
        return False
    LOG.info("Session trades appended to %s (%d rows)", csv_path, len(new_rows))
    return True
