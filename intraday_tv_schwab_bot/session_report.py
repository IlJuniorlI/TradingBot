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
      used by the strategy: always ``1m`` plus ``trigger_timeframe_minutes``
      and ``htf_timeframe_minutes`` if they're set and > 1. For
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
    for key in ("trigger_timeframe_minutes", "htf_timeframe_minutes"):
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

    # Filter the cumulative trades.csv down to today's trades for quick analysis.
    trades_src = log_dir_path / "trades.csv"
    trades_dst = archive_root / "trades.csv"
    trades_today = 0
    if trades_src.exists():
        try:
            with open(trades_src, newline="") as src_fh:
                rows = list(csv.reader(src_fh))
            if rows:
                header = rows[0]
                body = [r for r in rows[1:] if r and r[0] == session_date.isoformat()]
                with open(trades_dst, "w", newline="") as dst_fh:
                    writer = csv.writer(dst_fh)
                    writer.writerow(header)
                    writer.writerows(body)
                trades_today = len(body)
        except Exception as exc:
            LOG.warning("Could not write daily trades CSV: %s", exc)

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
