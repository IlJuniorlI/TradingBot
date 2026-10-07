# SPDX-License-Identifier: MIT
"""Per-day session archive: ``{log_dir}/sessions/{YYYY-MM-DD}/``.

``export_session_archive`` writes, for post-session analysis, the bars the bot
held (the merged 1m frame, its resamples and the stored HTF frame), the day's
closed trades, a copy of the day's log, the redacted config, the account
snapshot, the structured events and entry decisions read back from that log,
and a ``manifest.json`` that summarises them. It runs one stage per file; a
write stage that fails logs a WARNING and the stages after it still run.

The manifest also carries two analyses of the archive's own decisions and bars:

  * regime-call outcomes -- whether each directional regime call was right,
                            wrong or flat against the 30-minute forward move
  * gate attribution     -- what price did after each SKIP, by reason:
                            whether a gate blocks moves or blocks losses

The end-of-session report and the persistent trades.csv are
``session_report``'s. The archive's trades.csv is the day's rows of that
persistent file that the exporting strategy wrote, so the two files read the
same.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import yaml

from .models import Position
from . import sessions
from .position_manager import EQUITY_MARK_BASIS
from .reasons import SKIP_COUNT_UNIT, blocked_side, reason_gate, split_side_prefix
from .serialization import atomic_write_text
from .session_report import (
    TRADE_CSV_COLUMNS,
    read_trade_rows,
    trade_csv_key,
    trade_csv_row,
    trades_closed_on,
)

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config snapshot and log extraction
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
    if is_dataclass(config) and not isinstance(config, type):
        raw = asdict(config)
    elif isinstance(config, dict):
        raw = dict(config)
    else:
        # Fallback: walk __dict__ if available
        raw = getattr(config, "__dict__", {}) or {}
    return _redact_secrets(raw)


# Recognized structured-event prefixes emitted by AuditLogger.log_structured.
# Used by the events.jsonl extractor. CYCLE_TIMING (one per engine loop pass)
# and POSITION_MARK (one per held position per management pass) are DEBUG
# lines, in the log file only (2026-09-28).
_STRUCTURED_PREFIXES = (
    "ENTRY_CONTEXT", "EXIT_CONTEXT", "TRADE_SUMMARY",
    "SKIP_SUMMARY", "SESSION_REPORT", "POSITION_ADJUSTMENT",
    "ENTRY_CYCLE_SUMMARY", "CYCLE_TIMING", "POSITION_MARK",
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
#    primary=... secondary=... side_pref=... market_side=... family=...
#    reasons=..."
# Reasons can contain spaces inside parens but the OTHER fields are
# space-separated key=value (value has no spaces).
_DECISION_FIELD_RE = re.compile(r"\b(symbol|strategy|action|primary|secondary|side_pref|market_side|family)=(\S+)")
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
                # symbol/strategy/action/primary/secondary/side_pref/
                # market_side/family tokens but a future skip reason could.
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


# ---------------------------------------------------------------------------
# What price did after each decision: regime-call outcomes, gate attribution
# ---------------------------------------------------------------------------

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
    *, start: datetime | None = None, end: datetime | None = None,
) -> dict[str, Any]:
    """The unconditional forward move over the span the decisions cover.

    Without this every gate statistic is unreadable. A gate whose blocks were
    followed by a +0.3 ATR net move only matters if an arbitrary moment in the
    same session was not also followed by +0.3. Sampling from the session being
    reported also absorbs the day's character: a trending day lifts every
    LONG-side number, and only the gap above baseline is evidence.

    ``start`` / ``end`` bound the sampled bars to the decisions' own span. The
    archived 1m frames carry the PRIOR session and extended hours too, and
    until 2026-09-23 all of it was sampled: about 43% of a top_tier baseline
    came from yesterday or from pre/post-market bars, whose tiny ATR inflates
    any move measured in ATR. That is not the population any gate decided on.

    Measured from a LONG viewpoint (``median_net_atr``, ``up_pct``); the SHORT
    baseline is its mirror. Callers comparing a SHORT gate must use that
    mirror, not this number.
    """
    nets: list[float] = []
    for rows in bars_by_sym.values():
        for i in range(0, max(0, len(rows) - window_minutes - 1), max(1, stride)):
            ts = rows[i]["ts"]
            if (start is not None and ts < start) or (end is not None and ts > end):
                continue
            excursion = _forward_excursion_atr(rows, ts, window_minutes)
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


def _reason_tokens(reasons: str, maxsplit: int = -1) -> list[str]:
    """A decision's comma-joined ``reasons`` split into its reasons: at the
    commas outside parentheses, since a reason's detail
    (``(trend=1.0,pb=0.0)``, ``(group=ai_hardware,n=2)``) holds commas. At
    most ``maxsplit`` splits when it is not negative, as ``str.split``."""
    tokens: list[str] = []
    depth = 0
    start = 0
    for pos, char in enumerate(reasons):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0 and (maxsplit < 0 or len(tokens) < maxsplit):
            tokens.append(reasons[start:pos])
            start = pos + 1
    tokens.append(reasons[start:])
    return [token.strip() for token in tokens if token.strip()]


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

    Every gate on a row is scored, not only ``primary``: top_tier logs an
    unqualified side ahead of every build, so on a row where one side was
    unqualified the gate that stopped the other side's qualified build sat in
    ``reasons`` (2026-09-21: 267 minutes of long trend blocks by the
    confirmation-bar gate never scored, at the opposite edge to the counted
    ones). Each side-prefixed build reason after ``primary`` is scored too:
    top_tier's (``short_build_failed_...``) and the peer family's, which lists
    every blocker of each side it refused under that side's ``long.`` /
    ``short.`` prefix (``reasons.side_prefixed_reasons``). A row about a
    signal the strategy built, which the gatekeeper marks with the way the
    signal bets on the symbol (``market_side``; an option's underlying
    direction, not its order side), lists the signal's reason first and the
    engine gate that refused it after (``peer_confirmed_key_level_long,
    max_positions``): the gate, everything after the signal's reason, is
    scored, on that side. Until
    2026-09-26 only top_tier's signal names (``top_tier_range_long``) were
    recognised, so every other strategy's refused signal was scored under
    its own name, on the screener's side, and the gate never was.

    Each reason is bucketed under its gate, ``reasons.reason_gate``: the key
    the filter rejections and the entry cycle summary use, with no side
    prefix and no ``(...)`` or ``:...`` detail, so the numeric detail that
    fragments `session_skip_counts` into hundreds of near-duplicates rolls up
    (until 2026-09-26 only the ``(`` detail was cut, and the peer family's
    ``long.x`` / ``short.x`` were two gates). The side each block was scored
    on is kept per gate in ``sides``. Decisions are deduped by (symbol,
    minute, gate, side) because one decision is logged repeatedly across a
    cycle.

    Returns {} on any I/O or parse failure — never crashes the archive write.
    """
    try:
        decisions_path = archive_root / "decisions.csv"
        bars_by_sym = _load_archive_bars(archive_root / "bars" / "1m")
        if not decisions_path.exists() or not bars_by_sym:
            return {}

        net_moves: dict[str, list[float]] = defaultdict(list)
        edges: dict[str, list[float]] = defaultdict(list)
        favourable: dict[str, list[float]] = defaultdict(list)
        adverse: dict[str, list[float]] = defaultdict(list)
        families: dict[str, Counter] = defaultdict(Counter)
        sides: dict[str, Counter] = defaultdict(Counter)
        blocked = Counter()
        unevaluated = Counter()
        seen: set[tuple[str, datetime, str, str]] = set()
        # (reason, side, excursion) -- scored after the baseline, which needs
        # the span of every decision first.
        scored: list[tuple[str, str, tuple[float, float, float]]] = []
        first_ts: datetime | None = None
        last_ts: datetime | None = None

        with open(decisions_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ts_raw = str(row.get("timestamp", "")).strip('"').split(",")[0]
                try:
                    ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                # The baseline's span: every moment the strategy was deciding,
                # whatever it decided -- entries, sideless skips and all. It is
                # the population a blocked moment competes with.
                first_ts = ts if first_ts is None or ts < first_ts else first_ts
                last_ts = ts if last_ts is None or ts > last_ts else last_ts
                if str(row.get("action", "")).strip().lower() != "skipped":
                    continue
                symbol = str(row.get("symbol", "") or "")
                rows = bars_by_sym.get(symbol)
                if not rows:
                    continue
                primary = str(row.get("primary", "") or "").strip()
                if not primary or primary == "none":
                    continue
                # Which way was the bot about to trade? On a signal an engine
                # gate refused, the way the signal bet (``market_side``).
                # Otherwise the reason's own side when it names one
                # (``reasons.reason_side``) --
                # `short_build_failed_...` is the SHORT build this gate
                # stopped, and so is the peer family's `short.<gate>`, a
                # spelling read only since 2026-09-26 (a peer blocker took the
                # screener's side until then, and on key_levels, whose
                # candidates carry none, was dropped). `side_pref` is the
                # CANDIDATE's screener bias, and it disagrees with a
                # side-prefixed reason on about a quarter of rows (2026-09-22:
                # 2,065 `short_` rows carried side_pref=LONG); read first, it
                # scored those blocks in the wrong direction. It stands in
                # only for reasons that name no side. Without a side there is
                # no "favourable" direction and the row cannot be scored.
                side_pref = str(row.get("side_pref", "") or "").strip().upper()
                reasons = str(row.get("reasons", "") or "")
                market_side = str(row.get("market_side", "") or "").strip().upper()
                if market_side in {"LONG", "SHORT"}:
                    # The one gate the gatekeeper wrote is all that follows
                    # the signal's reason: a broker message in it
                    # (`order_failed:...cancel_error:{exc}`) can hold a comma
                    # outside parentheses, which until 2026-09-26 split off
                    # a second, spurious gate.
                    gates = _reason_tokens(reasons, maxsplit=1)[1:]
                else:
                    tokens = _reason_tokens(reasons) or [primary]
                    gates = [primary] + [
                        token for token in tokens[1:]
                        if _BUILD_FAILED_SIDE_RE.match(token.lower()) or split_side_prefix(token.lower())[0] is not None
                    ]
                family = str(row.get("family", "") or "none").strip() or "none"
                excursion: tuple[float, float, float] | None = None
                excursion_read = False
                for token in gates:
                    # ``reasons.blocked_side``: the rule above, which the
                    # session's skip tally keys its minutes on too.
                    side = blocked_side(token, market_side=market_side, side_pref=side_pref)
                    if side is None:
                        continue
                    # One gate can stop both sides on a row (the peer
                    # family's `long.x` and `short.x` are gate `x`): two blocks.
                    reason = reason_gate(token)
                    key = (symbol, ts.replace(second=0), reason, side)
                    if key in seen:
                        continue
                    seen.add(key)

                    blocked[reason] += 1
                    sides[reason][side] += 1
                    families[reason][family] += 1

                    if not excursion_read:
                        excursion = _forward_excursion_atr(rows, ts, window_minutes)
                        excursion_read = True
                    if excursion is None:
                        unevaluated[reason] += 1
                        continue
                    scored.append((reason, side, excursion))

        if not blocked:
            return {}

        baseline_stats = _forward_baseline(bars_by_sym, window_minutes, start=first_ts, end=last_ts)
        baseline_net = float(baseline_stats.get("median_net_atr") or 0.0)
        for reason, side, (up_atr, down_atr, net_atr) in scored:
            # Everything toward the side the bot wanted, and its edge over the
            # SAME side's baseline. Until 2026-09-23 every gate was compared
            # with the LONG baseline, so on an up day a SHORT gate's edge came
            # out understated by twice the drift, and on a down day overstated.
            if side == "LONG":
                favourable[reason].append(up_atr)
                adverse[reason].append(down_atr)
                net_moves[reason].append(net_atr)
                edges[reason].append(net_atr - baseline_net)
            else:
                favourable[reason].append(down_atr)
                adverse[reason].append(up_atr)
                net_moves[reason].append(-net_atr)
                edges[reason].append(-net_atr + baseline_net)

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
                "sides": dict(sorted(sides[reason].items())),
                "evaluated": len(nets),
                "unevaluated": int(unevaluated.get(reason, 0)),
                "regimes": dict(families[reason].most_common(4)),
                # The headline: net close-to-close move toward the side the bot
                # wanted. Baseline is ~0, so a positive number is evidence the
                # gate blocked a move that was going to happen anyway.
                "median_net_atr": None,
                # The same move less its side's baseline: what the ranking uses.
                "median_edge_atr": None,
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
                    "median_edge_atr": round(_median(edges[reason]), 3),
                    "favourable_pct": round(
                        100.0 * sum(1 for v in nets if v > 0) / len(nets), 1),
                    "median_favourable_excursion_atr": round(_median(fav), 3),
                    "median_adverse_excursion_atr": round(_median(adv), 3),
                })
            by_reason[reason] = entry

        # Rank by blocks x favourable edge: a gate that fires rarely cannot
        # cost much however wrong it is, and one that fires constantly with no
        # directional edge is not costing anything either.
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
            edge = entry.get("median_edge_atr")
            if edge is None or int(entry.get("evaluated", 0)) < min_samples:
                return 0.0
            return float(edge) * int(entry["blocked"]) if edge > 0 else 0.0

        ranked = sorted(by_reason.items(), key=_cost, reverse=True)
        return {
            "window_minutes": int(window_minutes),
            "min_samples_for_ranking": int(min_samples),
            "decisions_scored": int(sum(blocked.values())),
            "measures": ("net forward price movement against a same-session "
                         "baseline - NOT a backtest: no stop, target, sizing "
                         "or slippage"),
            "baseline": {
                **baseline_stats,
                "short_median_net_atr": (None if baseline_stats.get("median_net_atr") is None
                                         else round(-baseline_net, 3)),
                "span": [first_ts.isoformat() if first_ts else None,
                         last_ts.isoformat() if last_ts else None],
            },
            "costliest_gates": [
                {
                    "gate": name,
                    "blocked": entry["blocked"],
                    "net_atr": entry["median_net_atr"],
                    "edge_over_baseline_atr": entry["median_edge_atr"],
                }
                for name, entry in ranked[:8] if _cost((name, entry)) > 0
            ],
            "by_reason": dict(ranked),
        }
    except Exception as exc:
        LOG.warning("Could not compute gate attribution: %s", exc, exc_info=True)
        return {}


_QUALIFIED_REGIME_NAMES = ("vwap_reclaim", "vol_squeeze", "sr_scalp", "pullback", "momentum", "trend", "range", "orb")
_BUILD_FAILED_SIDE_RE = re.compile(
    r"^(?:(?P<side_a>long|short)_build_failed_|build_failed_(?P<side_b>long|short)_)(?P<rest>.*)$"
)
# The reason of a signal top_tier built (and small_cap_squeeze, which
# inherits the builder): `top_tier_{regime}_{side}`, whole.
_TOP_TIER_SIGNAL_RE = re.compile(
    r"^top_tier_(?P<regime>" + "|".join(_QUALIFIED_REGIME_NAMES) + r")_(?P<side>long|short)$"
)


def _qualified_regime_calls(row: dict[str, Any]) -> list[tuple[str, int]]:
    """Every ``(regime, direction)`` *row* records as QUALIFIED on a side.
    Direction is +1 LONG / -1 SHORT.

    The multi-regime equity strategies (top_tier_adaptive and its subclass)
    never emit ``ambiguous_regime``: every regime that clears its score floor
    joins a build queue, and the call is visible as that regime's build
    failing on a side (``long_build_failed_trend_...``, or the older
    ``build_failed_short_pullback_...`` shape) or as the signal it built
    (``top_tier_trend_long``) -- entered, or blocked afterwards by an engine
    gate, which the gatekeeper logs as a skip with the signal's reason first
    (``top_tier_range_long,max_positions``). ``unqualified_no_qualifying_regime``
    is not a call. A signal is read by top_tier's own name for it: until
    2026-09-26 any reason ending ``_<regime>_<side>`` was, so another
    strategy's signal (``rth_trend_pullback_long``) counted as a top_tier
    ``pullback`` call.

    A skipped row's calls are all its ``reasons``, not just ``primary``: an
    unqualified side is logged ahead of every build, so on a row where one
    side was unqualified and the other side's regime qualified and failed a
    later gate, ``primary`` is the non-call. Until 2026-09-23 only
    ``primary`` was read, which saw 2,613 of the 5,624 calls on 2026-09-22.

    A build reason with no regime in it (``no_fresh_breakout``) falls back to
    the row's ``family``, which names the first regime in the build queue --
    the one the FIRST build reason belongs to (the queue is tried in order,
    one reason per regime). A later regime-less reason names no regime this
    row records, so it is not counted.
    """
    primary = str(row.get("primary", "") or "").strip().lower()
    m = _TOP_TIER_SIGNAL_RE.match(primary)
    if m:
        return [(m.group("regime"), 1 if m.group("side") == "long" else -1)]
    if str(row.get("action", "") or "").strip().lower() == "entered":
        return []
    reasons = str(row.get("reasons", "") or row.get("primary", "") or "")
    family = str(row.get("family", "") or "").strip().lower()
    calls: list[tuple[str, int]] = []
    first_build = True
    for token in _reason_tokens(reasons.lower()):
        m = _BUILD_FAILED_SIDE_RE.match(token)
        if not m:
            continue
        side = m.group("side_a") or m.group("side_b")
        rest = m.group("rest")
        regime = next((name for name in _QUALIFIED_REGIME_NAMES if rest.startswith(name + "_") or rest == name), None)
        if regime is None and first_build and family in _QUALIFIED_REGIME_NAMES:
            regime = family
        first_build = False
        if regime is not None:
            calls.append((regime, 1 if side == "long" else -1))
    return calls


def _regime_call_outcomes(archive_root: Path) -> dict[str, Any]:
    """Classify each directional regime call against the 30-minute forward
    price move and bucket results by outcome, hour, regime and side.

    Purpose: regime scoring is the strategy's directional bet. When it
    says ``bullish_trend@4.50`` but the price drops 3 ATR in the next
    30 minutes, that's a regression signal worth knowing about. This
    helper writes the daily classification into ``manifest.json`` so a
    week-over-week comparison surfaces drift (e.g. "right%" trending
    below 30%) without anyone running ad-hoc post-mortems.

    Two sources of calls, whichever the strategy emits:
      * ``ambiguous_regime(top=...)`` skips (0DTE options) -- the top
        regime; ``bullish_trend`` / ``bearish_trend`` are directional,
        any other top is recorded as non-directional.
      * every regime that QUALIFIED on a side (top_tier_adaptive and its
        subclass) -- see ``_qualified_regime_calls``. Until 2026-09-23 only
        the first source was read, so every top_tier manifest reported 0
        calls: a measurement that could not see the strategy, not a
        strategy that made no calls.

    Classifier per call (excursions from ``_forward_excursion_atr``):
      * ``right`` -- price moved >= 1 ATR in the call's direction within
        the next 30 minutes, and further that way than the other.
      * ``wrong`` -- the opposite excursion was larger.
      * ``flat`` -- neither of the above, or a non-directional call.
      * ``unclear`` -- insufficient forward bars or missing ATR.

    Read ``by_side`` against the day: on a trend day most of one side is
    "right" whatever the regimes did.

    Reads from the already-written ``decisions.csv`` and the per-symbol
    1m bars under ``bars/1m/`` in the same archive directory. Dedupes
    calls by (symbol, minute, regime, direction) because one decision is
    logged on every cycle within the minute.

    Returns an empty dict on any I/O / parse failure -- never crashes
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

        calls: list[dict[str, Any]] = []
        seen: set[tuple[str, datetime, str, int]] = set()
        sources: Counter = Counter()
        try:
            with open(decisions_path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    sym = row.get("symbol", "")
                    if sym not in bars_by_sym:
                        continue
                    m = _AMBIGUOUS_REGIME_RE.search(row.get("primary", ""))
                    if m:
                        top = m.group("top")
                        direction = 1 if top == "bullish_trend" else -1 if top == "bearish_trend" else 0
                        found = [(top, direction, "ambiguous_regime")]
                    else:
                        found = [(top, direction, "qualified_regime") for top, direction in _qualified_regime_calls(row)]
                    if not found:
                        continue
                    ts_raw = row.get("timestamp", "").strip('"').split(",")[0]
                    try:
                        ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    for top, direction, source in found:
                        key = (sym, ts.replace(second=0), top, direction)
                        if key in seen:
                            continue
                        seen.add(key)
                        sources[source] += 1
                        calls.append({"ts": ts, "sym": sym, "top": top, "direction": direction})
        except OSError:
            return {}

        def _bucket() -> dict[str, int]:
            return {"total": 0, "right": 0, "wrong": 0, "flat": 0, "unclear": 0}

        by_outcome = {"right": 0, "wrong": 0, "flat": 0, "unclear": 0}
        by_hour: dict[str, dict[str, int]] = defaultdict(_bucket)
        by_regime: dict[str, dict[str, int]] = defaultdict(_bucket)
        by_side: dict[str, dict[str, int]] = defaultdict(_bucket)

        for call in calls:
            outcome = "unclear"
            excursion = _forward_excursion_atr(bars_by_sym[call["sym"]], call["ts"], 30)
            if excursion is not None:
                up_atr, down_atr, _net = excursion
                favourable, adverse = (up_atr, down_atr) if call["direction"] > 0 else (down_atr, up_atr)
                if call["direction"] == 0:
                    outcome = "flat"
                elif favourable >= 1.0 and favourable > adverse:
                    outcome = "right"
                elif adverse > favourable:
                    outcome = "wrong"
                else:
                    outcome = "flat"
            side = "LONG" if call["direction"] > 0 else "SHORT" if call["direction"] < 0 else "none"
            by_outcome[outcome] += 1
            for bucket in (by_hour[call["ts"].strftime("%H")], by_regime[call["top"]], by_side[side]):
                bucket["total"] += 1
                bucket[outcome] += 1

        def _right_pct(bucket: dict[str, int]) -> float | None:
            directional = bucket["right"] + bucket["wrong"] + bucket["flat"]
            return round(bucket["right"] / directional, 4) if directional > 0 else None

        call_times = [call["ts"] for call in calls]
        return {
            "total_unique_calls": len(calls),
            "sources": dict(sources),
            # The tape over the same span, LONG viewpoint: what "right" is
            # competing with. A 60% up_pct day makes most LONG calls right.
            "baseline": (_forward_baseline(bars_by_sym, 30, start=min(call_times), end=max(call_times))
                         if call_times else {}),
            "by_outcome": by_outcome,
            "right_pct_of_directional": _right_pct(by_outcome),
            "by_hour": {h: dict(d) for h, d in sorted(by_hour.items())},
            "by_regime": {r: {**d, "right_pct": _right_pct(d)} for r, d in sorted(by_regime.items())},
            "by_side": {s: {**d, "right_pct": _right_pct(d)} for s, d in sorted(by_side.items())},
        }
    except Exception as exc:
        LOG.warning("Could not compute regime-call outcomes: %s", exc, exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# Export stages, run in order by export_session_archive
# ---------------------------------------------------------------------------

def _archive_symbols(
    strategy: Any,
    last_candidates: Iterable[Any] | None,
    positions: dict[str, Position],
    account: Any,
) -> set[str]:
    """The symbols to export bars for: the active watchlist, the underlying of
    every open position, and every symbol in the account's trades, so a
    symbol that left the watchlist after it traded still gets its bars."""
    symbols: set[str] = set()
    watch = strategy.active_watchlist(list(last_candidates or []), positions or {})
    for sym in watch or set():
        key = str(sym or "").upper().strip()
        if key:
            symbols.add(key)
    for pos in (positions or {}).values():
        underlying = str((pos.metadata or {}).get("underlying") or pos.symbol or "").upper().strip()
        if underlying:
            symbols.add(underlying)
    if account is not None:
        for trade in list(getattr(account, "trades", []) or []):
            ticker = str(getattr(trade, "underlying", None) or getattr(trade, "symbol", "") or "").upper().strip()
            if ticker:
                symbols.add(ticker)
    return symbols


def _resample_timeframes(strategy_params: Any) -> list[int]:
    """The timeframes, in minutes, to export the merged frame at: always 1,
    plus the strategy's ``ltf_minutes`` and ``htf_minutes`` when above 1 (a
    value at or below 1 is the 1m frame itself). The HTF levels come from the
    stored HTF frame instead, which ``_export_htf_frames`` writes."""
    timeframes_min: set[int] = {1}
    for key in ("ltf_minutes", "htf_minutes"):
        raw = strategy_params.get(key) if isinstance(strategy_params, dict) else None
        try:
            tf = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            tf = 0
        if tf > 1:
            timeframes_min.add(tf)
    return sorted(timeframes_min)


def _export_merged_frames(data: Any, symbols: set[str], tf_min: int, tf_dir: Path) -> tuple[int, int] | None:
    """Write each symbol's merged frame at ``tf_min`` to ``tf_dir``.

    ``(written, skipped)``, or None when the folder cannot be created (the
    manifest then has no entry for it).
    """
    tf_label = tf_dir.name
    try:
        tf_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        LOG.warning("Could not create timeframe dir %s: %s", tf_dir, exc)
        return None
    tf_arg = "1min" if tf_min == 1 else f"{tf_min}min"
    written = 0
    skipped = 0
    for symbol in sorted(symbols):
        # A frame that cannot be built (get_merged resamples it and runs
        # the indicators) costs that symbol, not the archive, as the HTF
        # read below does. An error let through here would end the export
        # before the trades, log and manifest, and the daily export would
        # retry it every cycle.
        try:
            frame = data.get_merged(symbol, timeframe=tf_arg, with_indicators=True) if data is not None else None
        except Exception as exc:
            LOG.warning("Could not read the merged frame for %s/%s: %s", symbol, tf_label, exc, exc_info=True)
            skipped += 1
            continue
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
    return written, skipped


def _export_htf_frames(data: Any, symbols: set[str], htf_minutes: int, htf_dir: Path) -> tuple[int, int] | None:
    """Write each symbol's stored HTF frame to ``htf_dir``.

    The stored HTF frame is the series S/R levels, HTF levels, mshtf
    structure and HTF FVGs are built from. The resampled bars/{N}m is not
    it -- it spans only the days the 1m history covers (09-21 07:00 onward
    in the 09-22 AAPL archive, against a 10-day HTF lookback) -- so without
    this folder an HTF level could not be traced to the bar that made it
    (2026-09-23). The exporter reads what the bot held; a read never fetches
    (``MarketDataStore.get_htf_frame``).

    ``(written, skipped)``, or None when the folder cannot be created (the
    manifest then has no entry for it).
    """
    htf_label = htf_dir.name
    try:
        htf_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        LOG.warning("Could not create HTF frame dir %s: %s", htf_dir, exc)
        return None
    written = 0
    skipped = 0
    for symbol in sorted(symbols):
        try:
            frame = data.get_htf_frame(symbol, timeframe_minutes=htf_minutes) if data is not None else None
        except Exception as exc:
            LOG.warning("Could not read the HTF frame for %s/%s: %s", symbol, htf_label, exc)
            frame = None
        if frame is None or frame.empty:
            skipped += 1
            continue
        try:
            frame.to_csv(htf_dir / f"{symbol}.csv", index_label="timestamp")
            written += 1
        except Exception as exc:
            LOG.warning("Could not write HTF frame CSV for %s/%s: %s", symbol, htf_label, exc)
            skipped += 1
    return written, skipped


def _export_bars(
    data: Any, symbols: set[str], strategy: Any, bars_dir: Path,
) -> tuple[list[int], dict[str, int], dict[str, int]]:
    """Every bars folder: ``bars/{N}m`` per resample timeframe, then
    ``bars/htf_{N}m``.

    Returns the resample timeframes and, per folder written, the CSVs written
    and the symbols skipped.

    The FULL merged frame is exported for each timeframe, no filters. This
    captures everything the bot had access to: warmup history (needed to
    compute indicators like ema20/atr14 — the HTF in particular needs many
    bars from prior sessions), pre-market, RTH, and any post-market data the
    feed accumulated. A reconstructed view of what the bot saw at any moment
    during the session requires the warmup bars; filtering to today's RTH
    would silently drop them.
    """
    strategy_params = getattr(strategy, "params", {}) or {}
    timeframes_sorted = _resample_timeframes(strategy_params)
    written_by_folder: dict[str, int] = {}
    skipped_by_folder: dict[str, int] = {}
    for tf_min in timeframes_sorted:
        tf_label = f"{tf_min}m"
        counts = _export_merged_frames(data, symbols, tf_min, bars_dir / tf_label)
        if counts is not None:
            written_by_folder[tf_label], skipped_by_folder[tf_label] = counts
    htf_minutes = strategy.htf_minutes()
    htf_label = f"htf_{htf_minutes}m"
    counts = _export_htf_frames(data, symbols, htf_minutes, bars_dir / htf_label)
    if counts is not None:
        written_by_folder[htf_label], skipped_by_folder[htf_label] = counts
    return timeframes_sorted, written_by_folder, skipped_by_folder


def _copy_daily_log(log_src: Path, log_dst: Path) -> bool:
    """Copy the daily bot log into the archive; whether it was copied.

    The original FileHandler still has the file open (especially on Windows
    where moving a locked file fails), so we COPY rather than MOVE. The
    original at log_dir/bot_YYYY-MM-DD.log stays in place; the copy in the
    archive is a permanent record. Run a separate cleanup script later if you
    want to prune the originals from log_dir root.
    """
    if not log_src.exists():
        return False
    try:
        # Flush log handlers first so the copy includes the
        # latest in-memory buffered log lines.
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except (OSError, ValueError):
                # A handler whose stream failed or was closed (ValueError:
                # I/O operation on closed file) has nothing to add to the
                # copy.
                pass
        shutil.copy2(log_src, log_dst)
        return True
    except Exception as exc:
        LOG.warning("Could not copy daily log %s: %s", log_src, exc)
        return False


def _export_trades(account: Any, trades_src: Path, trades_dst: Path,
                   session_date: date, strategy_name: str) -> tuple[int, float | None, str | None]:
    """Write the day's rows of the persistent trades.csv (``trades_src``)
    that ``strategy_name`` wrote to ``trades_dst``.

    Returns ``(trades_today, realized_pnl, trades_export_error)``.

    The rows are those of every process of the strategy that appended to the
    file: the engine appends its closed trades (``append_trades_csv``) before
    it exports, at the end of the day and at shutdown, so a process that
    restarted during the day, and the one before it, each put their trades
    in. A process killed before either (SIGKILL, an OOM kill, a crash) never
    appended, and its trades are in no archive. Other strategies' rows, from
    a process sharing the log directory, are left out; a dry-run and a live
    process of one strategy share theirs, since trades.csv has no mode
    column.

    Until 2026-10-06 the rows came from the exporting process's in-memory
    account, because the append ran only at shutdown: the 20:00 export of an
    always-on bot found the persistent file a day behind (observed live
    2026-05-20: 2 SPY credit-spread closes in account + log, 0 rows in
    archive trades.csv). The account copy had its own gap: a restart later
    the same day, or a start after 20:00, re-exported the day with the new
    process's trades (none) over the old one's. A process that starts after
    a trading day's 20:00 writes that day's archive only when the day has
    none or an earlier shutdown left it owed (``archive_owed.json``), with
    ``exporter_ran_session`` false in the manifest: its bars and account
    snapshot are not the session's.

    ``account`` (the exporting process's) is checked against the file: a
    trade of the day and the strategy that it holds and the file does not
    (an append that failed) is an error, not a quietly shorter day.

    Today's realized PnL is summed from the SAME rows that produce trades.csv
    and trades_today. The manifest used to report `account.realized_pnl`, a
    LIFETIME accumulator (set to 0.0 once in PaperAccount.__init__ and never
    reset per day), so an always-on bot carried prior days into a field
    beside the date-filtered `trades_today` (observed 2026-07-31: manifest
    -80.77 against trades.csv -60.22, a gap of exactly the previous
    session's -20.55). The rows hold `trade_csv_row`'s rounded values, so
    `manifest.realized_pnl == sum(trades.csv.realized_pnl)` holds exactly.
    """
    session_date_str = session_date.isoformat()
    try:
        rows = [row for row in read_trade_rows(trades_src, session_date_str) if row.get("strategy") == strategy_name]
        with open(trades_dst, "w", newline="", encoding="utf-8") as dst_fh:
            writer = csv.DictWriter(dst_fh, fieldnames=TRADE_CSV_COLUMNS, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
        realized_pnl_today = round(sum(float(row["realized_pnl"]) for row in rows), 2)
    except Exception as exc:
        LOG.warning("Could not write the day's trades CSV from %s: %s: %s",
                    trades_src, type(exc).__name__, exc, exc_info=True)
        # Report UNKNOWN, not flat: a wrong-but-plausible zero is worse than
        # an absent value, since nobody investigates a zero.
        return 0, None, f"{type(exc).__name__}: {exc}"
    if account is None:
        return len(rows), realized_pnl_today, None
    try:
        held = {trade_csv_key(trade_csv_row(trade, session_date_str))
                for trade in trades_closed_on(getattr(account, "trades", []) or [], session_date)
                if trade.strategy == strategy_name}
    except Exception as exc:
        # A record the row cannot be built from (one missing a TradeRecord
        # field, rehydrated from an older store, say) failed the append too.
        error = (f"this process's trades of {session_date_str} could not be checked against {trades_src}: "
                 f"{type(exc).__name__}: {exc}")
        LOG.warning("Session archive: %s", error)
        return len(rows), None, error
    missing = len(held - {trade_csv_key(row) for row in rows})
    if missing:
        error = f"{missing} of this process's trades closed {session_date_str} are not in {trades_src}"
        LOG.warning("Session archive: %s", error)
        return len(rows), None, error
    return len(rows), realized_pnl_today, None


def _write_config_snapshot(config: Any | None, archive_root: Path) -> bool:
    """Dump the resolved config, secrets redacted, to config_snapshot.yaml, so
    future audits can reproduce decisions even if config.yaml has been edited
    since; whether it was written. No config, no snapshot."""
    if config is None:
        return False
    try:
        cfg_dict = _config_to_dict(config)
        with open(archive_root / "config_snapshot.yaml", "w") as fh:
            yaml.safe_dump(cfg_dict, fh, sort_keys=False, default_flow_style=False)
        return True
    except Exception as exc:
        LOG.warning("Could not write config snapshot: %s", exc)
        return False


def _write_account_snapshot(account: Any, positions: dict[str, Position], archive_root: Path) -> bool:
    """Write account_snapshot.json: equity, realized PnL, per-symbol PnL,
    equity curve — everything the PaperAccount knows at the moment of
    export; whether it was written."""
    if account is None:
        return False
    try:
        snapshot = account.capture_snapshot(positions or {})
        # capture_snapshot returns a dict; serialize via json (default=str
        # to handle datetimes inside equity curve points).
        atomic_write_text(
            archive_root / "account_snapshot.json",
            json.dumps(snapshot, indent=2, default=str),
        )
        return True
    except Exception as exc:
        LOG.warning("Could not write account snapshot: %s", exc)
        return False


def _export_events(log_path: Path, archive_root: Path) -> int:
    """Write the structured events (ENTRY_CONTEXT, EXIT_CONTEXT,
    TRADE_SUMMARY, SKIP_SUMMARY, etc.) in ``log_path`` to events.jsonl, one
    JSON object per line for easy querying with jq/pandas; how many."""
    try:
        events = _extract_structured_events(log_path)
        if not events:
            return 0
        atomic_write_text(
            archive_root / "events.jsonl",
            "".join(json.dumps(ev, default=str) + "\n" for ev in events),
        )
        return len(events)
    except Exception as exc:
        LOG.warning("Could not extract structured events: %s", exc)
        return 0


def _export_decisions(log_path: Path, archive_root: Path) -> int:
    """Write the entry decisions in ``log_path`` to decisions.csv, one row
    per engine entry-decision line; how many."""
    try:
        decisions = _extract_decisions(log_path)
        if not decisions:
            return 0
        cols = ["timestamp", "symbol", "strategy", "action", "primary",
                "secondary", "side_pref", "market_side", "family", "reasons"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in decisions:
            writer.writerow({c: row.get(c, "") for c in cols})
        atomic_write_text(archive_root / "decisions.csv", buf.getvalue())
        return len(decisions)
    except Exception as exc:
        LOG.warning("Could not extract decisions log: %s", exc)
        return 0


def _write_manifest(manifest: dict[str, Any], archive_root: Path) -> None:
    """Write ``manifest.json``, the archive's last file. An error propagates:
    an archive without its manifest is not written, and the engine retries
    it (``IntradayBot._close_session_day``)."""
    atomic_write_text(
        archive_root / "manifest.json",
        json.dumps(manifest, indent=2, default=str),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def session_archive_root(log_dir: str, session_date: date) -> Path:
    """``{log_dir}/sessions/{YYYY-MM-DD}/``, the archive of ``session_date``."""
    return Path(str(log_dir or ".logs")) / "sessions" / session_date.isoformat()


def session_archive_manifest_path(log_dir: str, session_date: date) -> Path:
    """The ``manifest.json`` an archive of ``session_date`` ends with."""
    return session_archive_root(log_dir, session_date) / "manifest.json"


def session_archive_owed_path(log_dir: str, session_date: date) -> Path:
    """The ``archive_owed.json`` a shutdown leaves in the folder of an
    archive it did not write (``leave_session_archive_owed``; it writes one
    before each of its exports too), so that the next start writes it;
    ``export_session_archive`` removes it."""
    return session_archive_root(log_dir, session_date) / "archive_owed.json"


def leave_session_archive_owed(log_dir: str, session_date: date, why: str,
                               session_skip_counts: dict[str, int]) -> None:
    """Leave ``session_date``'s archive owed to the next start that exports
    archives: write its ``archive_owed.json``, naming ``why``, with the
    day's skip tally (empty for a day the process did not run), which lives
    only in the memory of the process that ran the day. Raises OSError."""
    atomic_write_text(
        session_archive_owed_path(log_dir, session_date),
        json.dumps({"session_date": session_date.isoformat(), "left_at": sessions.now_et().isoformat(),
                    "why": why, "session_skip_counts": dict(session_skip_counts)}, indent=2, sort_keys=True),
    )


def owed_session_archives(log_dir: str) -> dict[date, dict[str, int]]:
    """The days whose archive an earlier shutdown left owed (an
    ``archive_owed.json`` in its folder), oldest first, each with the skip
    tally that shutdown left. A marker in a folder whose name is not a date
    is skipped, and one whose tally cannot be read still makes its day owed,
    with an empty tally; each with a WARNING."""
    owed: dict[date, dict[str, int]] = {}
    for marker in (Path(str(log_dir or ".logs")) / "sessions").glob("*/archive_owed.json"):
        try:
            day = date.fromisoformat(marker.parent.name)
        except ValueError:
            LOG.warning("Ignoring %s: its folder is not named for a session date", marker)
            continue
        try:
            counts = json.loads(marker.read_text(encoding="utf-8"))["session_skip_counts"]
            if not (isinstance(counts, dict)
                    and all(isinstance(reason, str) and type(count) is int for reason, count in counts.items())):
                raise ValueError(f"session_skip_counts is not a tally: {counts!r}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            LOG.warning("Could not read the skip tally in %s, so the %s archive's is empty: %s: %s",
                        marker, day, type(exc).__name__, exc)
            counts = {}
        owed[day] = counts
    return dict(sorted(owed.items()))


def export_session_archive(
    *,
    session_date: date,
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
    exporter_ran_session: bool,
) -> None:
    """Write a per-day archive of bars / trades / log / manifest to
    ``{log_dir}/sessions/{YYYY-MM-DD}/`` for post-session analysis.

    Contents:
    - ``bars/{N}m/{SYMBOL}.csv`` — the full merged 1m frame (history +
      live, warmup + pre-market + RTH + post-market) with all indicators
      for every active watchlist symbol, plus its resample to
      ``ltf_minutes`` and ``htf_minutes`` when they're set and > 1. For
      top_tier_adaptive that's ``bars/1m/`` and ``bars/15m/``. The full
      frame is written so debuggers can reconstruct the bot's view at any
      moment in the session — indicators like 15m ema20 need 5+ hours of
      warmup bars that filtering to today would drop. A resampled
      ``bars/{N}m`` is what LTF triggers and the resample-based LTF
      contexts read; it is NOT the frame the HTF levels are built from
      (it only spans the days the 1m history covers).
    - ``bars/htf_{N}m/{SYMBOL}.csv`` — the stored HTF frame
      (``data.get_htf_frame``: Schwab's native N-minute series over the
      HTF lookback, completed bars only) that S/R levels, HTF levels, HTF
      market structure (``mshtf_*``) and HTF fair-value gaps are built
      from. N is the strategy's HTF: ``params.htf_minutes``, else
      ``support_resistance.timeframe_minutes``. Until 2026-09-23 it was
      not archived and ``bars/15m`` was described as that frame, so an
      HTF level could not be traced to the bars that made it after the
      session.
    - ``trades.csv`` — the day's rows of the cumulative ``{log_dir}/trades.csv``
      (entry/exit/PnL/MFE/MAE per trade) that ``strategy_name`` wrote: those
      of every process of the strategy that appended its trades, since the
      engine appends before it exports (``_export_trades``).
    - ``bot_{YYYY-MM-DD}.log`` — copy of the daily log file (original
      stays in log_dir; copying avoids file-lock issues on Windows where
      the FileHandler still owns the original).
    - ``config_snapshot.yaml`` — the resolved BotConfig (with secrets
      redacted) so future audits can reproduce decisions even if
      config.yaml has been edited since.
    - ``account_snapshot.json`` — full PaperAccount snapshot at the
      moment of export (end-of-day daily fire or shutdown): equity
      curve, realized PnL by symbol, open positions, etc., with each held
      equity valued at the account's mark (the manifest's
      ``equity_mark_basis``).
    - ``events.jsonl`` — structured events (ENTRY_CONTEXT, EXIT_CONTEXT,
      TRADE_SUMMARY, SKIP_SUMMARY, the engine's CYCLE_TIMING, the position
      manager's POSITION_MARK, ...: ``_STRUCTURED_PREFIXES``) extracted
      from the log file as one-per-line JSON. Easier to parse with jq/pandas than grepping
      the raw text log.
    - ``decisions.csv`` — every entry-decision event from the engine as
      a queryable CSV (timestamp, symbol, action, regime, primary/
      secondary skip reasons).
    - ``manifest.json`` — strategy, dry_run, summary stats, skip counts,
      timeframes exported, write-flags for each archive component, and
      ``equity_mark_basis``: the price the paper account marked a held
      equity at (``position_manager.EQUITY_MARK_BASIS``). An archive
      without it was written by an earlier version, which marked every
      held equity at its 1m close.
    - ``archive_owed.json`` — only while the archive is owed: a shutdown
      that did not write it leaves one (``leave_session_archive_owed``), as
      does one whose export is cut off (a shutdown writes it before each
      export starts), and the export removes it once the manifest is
      written.

    Raises OSError when the archive directory or ``manifest.json`` cannot
    be written, or the ``archive_owed.json`` a shutdown left
    (``leave_session_archive_owed``) cannot be removed once it is: the
    archive is then not written, and the engine retries it. Every other
    stage logs its own failure and the stages after it run.

    Parameters
    ----------
    session_date
        The ET day archived: its folder, its ``bot_<date>.log`` and its
        trades.csv rows. The engine passes the day it closes, which after a
        retry past midnight is not today.
    log_dir
        Path to the bot's log directory (where bars/, trades.csv,
        bot_*.log already live). The archive subdirectory is created
        under ``{log_dir}/sessions/``.
    strategy_name, dry_run
        Recorded in manifest for later auditability.
    data
        DataFeed instance — used via ``data.get_merged(symbol, timeframe)``
        to pull the merged history+live frame for each symbol, and via
        ``data.get_htf_frame(...)`` for the stored HTF frame (a read, never
        a Schwab fetch).
    account
        PaperAccount (or live account tracker). Its ``account.trades`` add
        the closed-position symbols to the bars even if they left the
        watchlist, and are checked against the day's trades.csv rows.
    positions
        Currently-open positions at the moment of export. On the
        end-of-day daily fire (8pm ET) this is whatever the bot is
        holding overnight; on shutdown it's typically empty after
        force-flatten.
    strategy
        Strategy instance — used for ``strategy.active_watchlist(...)``,
        ``strategy.params`` (to read trigger/HTF timeframes) and
        ``strategy.htf_minutes()`` (the stored HTF frame's timeframe).
    last_candidates
        The most recent candidate list from the engine; passed to
        ``active_watchlist`` so dynamic-discovery strategies emit the
        right set of symbols.
    session_skip_counts
        Engine's session-wide skip-reason tally, in symbol-minutes (a
        symbol's gate on a side counted once a minute). Recorded in manifest, beside
        ``session_skip_counts_unit``.
    config
        Optional resolved BotConfig instance. If provided, a
        ``config_snapshot.yaml`` is written to the archive with secret
        fields (app_key, app_secret, account_hash, encryption_key,
        sessionid, etc.) redacted. Pass None to skip the snapshot.
    exporter_ran_session
        False when the exporting process did not run the session: it
        started after the day's 20:00 ET end (the engine then exports only a
        day without an archive), or writes a day an earlier shutdown left
        owed (``archive_owed.json``). Its bars and account snapshot are then
        its own, and so is the skip tally (empty) but for the one such a
        shutdown left. Recorded in the manifest.
    """
    log_dir_path = Path(str(log_dir or ".logs"))
    archive_root = session_archive_root(log_dir, session_date)
    bars_dir = archive_root / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)

    symbols = _archive_symbols(strategy, last_candidates, positions, account)
    timeframes_sorted, bars_written_by_tf, bars_skipped_by_tf = _export_bars(data, symbols, strategy, bars_dir)
    # Aggregate counts for the manifest summary.
    bars_written = sum(bars_written_by_tf.values())
    bars_skipped = sum(bars_skipped_by_tf.values())

    log_src = log_dir_path / f"bot_{session_date.isoformat()}.log"
    log_dst = archive_root / f"bot_{session_date.isoformat()}.log"
    log_copied = _copy_daily_log(log_src, log_dst)
    trades_today, realized_pnl_today, trades_export_error = _export_trades(
        account, log_dir_path / "trades.csv", archive_root / "trades.csv", session_date, str(strategy_name))
    config_snapshot_written = _write_config_snapshot(config, archive_root)
    account_snapshot_written = _write_account_snapshot(account, positions, archive_root)

    # events.jsonl and decisions.csv are read from the COPIED log (log_dst)
    # when it exists, so they and bot_*.log in the archive reference the
    # same snapshot — no asymmetry between human-readable log and machine-
    # parseable events. Falls back to the original log_src if the copy
    # failed (best-effort).
    extraction_src = log_dst if log_copied and log_dst.exists() else log_src
    events_written = _export_events(extraction_src, archive_root)
    decisions_written = _export_decisions(extraction_src, archive_root)

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
        "exported_at": sessions.now_et().isoformat(),
        "exporter_ran_session": bool(exporter_ran_session),
        "timeframes_exported": [f"{tf}m" for tf in timeframes_sorted],
        "symbols_exported": bars_written,
        "symbols_skipped": bars_skipped,
        "bars_written_by_timeframe": bars_written_by_tf,
        "bars_skipped_by_timeframe": bars_skipped_by_tf,
        "trades_today": trades_today,
        "log_file_copied": log_copied,
        "config_snapshot_written": config_snapshot_written,
        "account_snapshot_written": account_snapshot_written,
        "equity_mark_basis": EQUITY_MARK_BASIS,
        "events_extracted": events_written,
        "decisions_extracted": decisions_written,
        "open_positions_at_close": len(positions or {}),
        "realized_pnl": realized_pnl_today,
        "trades_export_error": trades_export_error,
        "session_skip_counts": dict(session_skip_counts or {}),
        "session_skip_counts_unit": SKIP_COUNT_UNIT,
        "regime_call_outcomes": regime_outcomes,
        "gate_attribution": gate_attribution,
    }
    _write_manifest(manifest, archive_root)
    # Written: no start owes it any more.
    session_archive_owed_path(log_dir, session_date).unlink(missing_ok=True)

    LOG.info(
        "Session archive written to %s (%d bars CSVs, %d trades, %d events, %d decisions, log_copied=%s)",
        archive_root, bars_written, trades_today, events_written, decisions_written, log_copied,
    )
