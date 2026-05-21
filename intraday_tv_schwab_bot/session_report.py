# SPDX-License-Identifier: MIT
"""End-of-session reporting: log summary, structured JSON, and persistent CSV trade log.

In addition to the headline numbers (PnL, win rate, profit factor), the report
aggregates closed trades along five axes to support strategy/config tuning:

  * per-regime        — how each setup type performed (trend/pullback/range/...)
  * per-symbol        — catches concentration issues and high-variance tickers
  * per-exit-reason   — surfaces leaky exit mechanisms (phantom stops, tight targets)
  * per-hour          — identifies dead zones in the trading day
  * MAE / MFE         — max adverse / favorable excursion in R-multiples
  * filter rejections — tally of skip reasons the engine logged during the session

All aggregate sections are emitted both in the human log (fixed-width tables)
and inside the SESSION_REPORT structured JSON payload (under top-level keys
``per_regime``, ``per_symbol``, ``per_exit_reason``, ``per_hour``,
``mae_mfe``, ``filter_rejections``) so downstream tooling can parse them
without re-scraping.
"""
from __future__ import annotations

import csv
import dataclasses
import io
import json
import logging
import re
from collections import defaultdict
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
        closed = [t for t in trades if bool(getattr(t, "final_exit", True))]
        session_date = now_et().date().isoformat()

        # --- Log summary ---
        total_pnl = float(performance.get("realized_pnl", 0.0) or 0.0)
        wins = int(performance.get("wins", 0) or 0)
        losses = int(performance.get("losses", 0) or 0)
        win_rate = performance.get("win_rate")
        profit_factor = performance.get("profit_factor")
        avg_trade = performance.get("average_trade")
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
        per_symbol = _per_symbol(closed)
        per_exit_reason = _per_exit_reason(closed)
        per_hour = _per_hour(closed)
        mae_mfe = _mae_mfe_summary(closed)
        filter_rejections = _filter_rejection_summary(skip_counts)

        # --- Human-readable aggregate tables ---
        if closed:
            _log_group_table("Per regime", per_regime, key_label="regime")
            _log_group_table("Per symbol", per_symbol, key_label="symbol")
            _log_group_table("Per exit reason", per_exit_reason, key_label="exit_reason")
            _log_group_table("Per hour (entry)", per_hour, key_label="hour_et")
            _log_mae_mfe(mae_mfe)
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
            "per_symbol": per_symbol,
            "per_exit_reason": per_exit_reason,
            "per_hour": per_hour,
            "mae_mfe": mae_mfe,
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

        # Load bar timelines per symbol — only need ts/high/low/close/atr14.
        # Bars timestamps are tz-aware ISO ("2026-05-21T11:12:00-04:00");
        # decisions timestamps are naive ("2026-05-21 11:12:00,798").
        # Normalize both to naive ET for the lookup comparison.
        bars_by_sym: dict[str, list[dict[str, Any]]] = {}
        for path in bars_dir.glob("*.csv"):
            try:
                rows: list[dict[str, Any]] = []
                with open(path, newline="", encoding="utf-8") as fh:
                    for row in csv.DictReader(fh):
                        ts_raw = row.get("timestamp", "")
                        try:
                            ts = datetime.fromisoformat(ts_raw).replace(tzinfo=None)
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
                        rows.append({"ts": ts, "high": high, "low": low, "close": close, "atr14": atr14})
                if rows:
                    rows.sort(key=lambda r: r["ts"])
                    bars_by_sym[path.stem] = rows
            except (OSError, csv.Error):
                continue

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
        except Exception as exc:
            LOG.warning("Could not write daily trades CSV from account: %s", exc, exc_info=True)

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
        "realized_pnl": float(getattr(account, "realized_pnl", 0.0) or 0.0) if account is not None else None,
        "session_skip_counts": dict(session_skip_counts or {}),
        "regime_call_outcomes": regime_outcomes,
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
