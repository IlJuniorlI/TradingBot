# SPDX-License-Identifier: MIT
from __future__ import annotations

import copy
import dataclasses
import itertools
import json
import logging
import math
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .models import (
    ASSET_TYPE_EQUITY,
    ASSET_TYPE_OPTION_SINGLE,
    ASSET_TYPE_OPTION_VERTICAL,
    Position,
    Side,
    asset_type_of,
)
from .numeric import first_float, safe_float
from .position_metrics import favorable_move, position_unrealized_at_price, return_pct
from .log_setup import TRADEFLOW_LEVEL
from . import sessions

if TYPE_CHECKING:
    from .position_store import DayLedgerStore

LOG = logging.getLogger(__name__)

# The day ledger's curve (``PaperAccount.day``) keeps one point a minute, the
# latest sample of each: at most 1,441 points a day, the opening point
# included, in memory, in the dashboard state and in the day's row.
DAY_CURVE_POINT_SECONDS = 60
# The day's row (``DayLedgerStore``) is rewritten on each exit, at each roll
# and at shutdown, and otherwise at most this often while the day's P&L, its
# peak, its max drawdown or the open positions change. A failed write holds
# every write but the shutdown's this long.
DAY_LEDGER_SAVE_SECONDS = 60.0
# The rolled-out days the account keeps in memory, for a day's close retried
# after the roll (its report's and its archive's day figures).
DAY_LEDGERS_KEPT = 7
# A day row's figure beyond this many dollars, or a count beyond
# ``_ROW_COUNT_LIMIT``, is not read: no account holds it, and a sum or a
# ratio of it could leave the finite floats the row and the state are
# written in.
_ROW_FIGURE_LIMIT = 1e12
_ROW_COUNT_LIMIT = 10 ** 9
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _return_pct(side: Side, entry_price: float, last_or_exit_price: float) -> float:
    """``position_metrics.return_pct``, 0.0 for a zero entry: the paper
    account's rows and trades always carry a number."""
    if not entry_price:
        return 0.0
    return return_pct(side, entry_price, last_or_exit_price)


@dataclass(slots=True)
class TradeRecord:
    symbol: str
    strategy: str
    side: str
    qty: int
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    realized_pnl: float
    return_pct: float
    hold_minutes: float
    reason: str
    asset_type: str = ASSET_TYPE_EQUITY
    underlying: str | None = None
    exchange: str | None = None
    option_type: str | None = None  # "CALL" / "PUT" for option trades; None for stocks
    lifecycle_id: str | None = None
    partial_exit: bool = False
    final_exit: bool = True
    remaining_qty_after_exit: int = 0
    fill_price_estimated: bool = False
    broker_recovered: bool = False
    # Diagnostic fields populated from position.metadata at exit time.
    # These power the per-regime / MAE / MFE / slippage aggregates in the
    # end-of-day session report. All are optional — older trades won't
    # have them and the aggregator should handle None gracefully.
    regime: str | None = None
    initial_risk_per_unit: float | None = None   # abs(entry - initial_stop)
    # MFE / MAE in $ at the trade's FULL size (metadata initial_qty), on
    # every slice -- they are excursions of the trade, not of the slice, and
    # the session report divides them by initial risk x the folded quantity.
    max_favorable_pnl: float | None = None       # peak unrealized PnL (MFE in $)
    max_adverse_pnl: float | None = None         # trough unrealized PnL (MAE in $)
    # The entry fill's slippage past the touch its order crossed (the ask
    # for a LONG, the bid for a SHORT), as a fraction of the touch: positive
    # when worse for the position. entry_limit_buffer_pct is how far the
    # marketable limit lay past the same touch. Until 2026-10-06
    # entry_slippage_pct was |fill - limit| / limit, the limit's buffer; a
    # position entered before then (no entry_touch_price) records none.
    entry_slippage_pct: float | None = None
    entry_limit_buffer_pct: float | None = None
    # Post-fill risk reconciliation (2026-09-18). realized_entry_risk is
    # qty * |fill - stop| — what the trade actually risked once the fill was
    # known, versus entry_risk_budget (max_notional_per_trade *
    # risk_per_trade_frac_of_notional). entry_risk_overage_frac is the
    # fraction above budget, 0.0 when at or under it.
    realized_entry_risk: float | None = None
    entry_risk_budget: float | None = None
    entry_risk_overage_frac: float | None = None
    # Armed retest (2026-09-20). ``retest_confirmed`` means the entry came
    # from the retest the regime waited for; ``expired_market_entry`` means
    # the wait ran out and it entered at market, which is the pre-change
    # behaviour. None for every regime that does not arm. This is the column
    # the A/B is read from -- without it a retest entry and a fallback entry
    # are indistinguishable in trades.csv.
    armed_retest_status: str | None = None
    armed_retest_waited_minutes: float | None = None
    # Reasons of the slices that closed PART of the trade before its final
    # exit, oldest first -- a scale-out (divergence_partial_exit:...) or a
    # broker partial fill. Set on the folded lifecycle record; ``reason`` is
    # the final slice's. Without it a trade that scaled out read in
    # per_exit_reason and trades.csv as if it had only ever exited once
    # (2026-09-24).
    partial_exit_reasons: tuple[str, ...] = ()
    # The trade's best mark (management's mark at its best unrealized P&L),
    # and the target the first management pass whose mark was at or through
    # it came in with, None when no pass's mark reached a target
    # (PositionManager._update_position_diagnostics). The session report
    # counts the trades that reached their target and closed at a loss
    # (target_reached_losses), whatever exit closed them (2026-10-12).
    best_mark_price: float | None = None
    target_reached_price: float | None = None


def closed_trade_lifecycles(trades: Iterable[Any]) -> list[Any]:
    """One record per CLOSED trade, its exit slices folded together.

    ``record_exit`` books every exit slice on its own -- a partial exit fill,
    a broker-recovered remainder, a bracket child that filled part of the
    position -- and only the last slice carries ``final_exit``. Reports that
    kept just that slice dropped the earlier slices' P&L (100 shares out as
    40 + 60 at +$1: $100 realized, $60 reported, in the EOD report,
    trades.csv and the manifest alike) and judged the trade a win or loss on
    its last slice alone.

    Slices group by ``lifecycle_id`` (position key + entry time). A
    lifecycle with no final slice is still open and is left out. Quantities
    and P&L sum; entry and exit prices are quantity-weighted; the reason,
    exit time and diagnostics are the final slice's, and the earlier
    slices' reasons are kept, oldest first, in ``partial_exit_reasons``. A
    single-slice trade is returned as-is. Output follows the input order of
    each lifecycle's first appearance -- newest first for
    ``PaperAccount.trades``.
    """
    groups: dict[Any, list[Any]] = {}
    for trade in trades:
        key = getattr(trade, "lifecycle_id", None) or ("__unkeyed__", id(trade))
        groups.setdefault(key, []).append(trade)
    closed: list[Any] = []
    for group in groups.values():
        final = next((t for t in group if bool(getattr(t, "final_exit", True))), None)
        if final is None:
            continue
        if len(group) == 1:
            closed.append(final)
            continue
        qty = sum(int(t.qty) for t in group)
        entry_price = sum(float(t.entry_price) * int(t.qty) for t in group) / qty
        exit_price = sum(float(t.exit_price) * int(t.qty) for t in group) / qty
        partials = [t for t in group if t is not final]
        try:
            partials.sort(key=lambda t: t.exit_time)
        except TypeError:
            # A slice with an unreadable exit_time (see
            # PaperAccount._trade_to_dict) must not cost the report: keep
            # the input order.
            pass
        closed.append(dataclasses.replace(
            final,
            qty=qty,
            entry_price=entry_price,
            exit_price=exit_price,
            realized_pnl=sum(float(t.realized_pnl) for t in group),
            return_pct=_return_pct(Side(final.side), entry_price, exit_price),
            partial_exit=False,
            final_exit=True,
            remaining_qty_after_exit=0,
            fill_price_estimated=any(bool(t.fill_price_estimated) for t in group),
            broker_recovered=any(bool(t.broker_recovered) for t in group),
            partial_exit_reasons=tuple(str(t.reason) for t in partials),
        ))
    return closed


@dataclass(slots=True)
class EquityPoint:
    timestamp: datetime
    equity: float
    cash: float
    market_value: float
    realized_pnl: float
    unrealized_pnl: float
    open_positions: int


@dataclass(slots=True)
class TradeStats:
    """Closed trades' running sums, one trade a lifecycle: what the win rate,
    the profit factor and the average trade are read from."""
    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net: float = 0.0

    def add(self, pnl: float) -> None:
        self.trades += 1
        self.net += pnl
        if pnl > 0:
            self.wins += 1
            self.gross_profit += pnl
        elif pnl < 0:
            self.losses += 1
            self.gross_loss -= pnl

    def figures(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": (self.wins / self.trades) if self.trades else None,
            "profit_factor": (self.gross_profit / self.gross_loss) if self.gross_loss > 0 else None,
            "average_trade": (self.net / self.trades) if self.trades else None,
        }


@dataclass(slots=True, eq=False)
class DayLedger:
    """One ET trading day's figures (``PaperAccount.day``).

    The day's P&L is its equity change: ``pnl_at(equity)``, what earlier
    processes carried (``carried_pnl``, from the day's row) plus this
    process's change since ``base_equity``. ``opening_equity`` is the
    equity the day opened at, on the scale of the process that opened it;
    the curve, the peak (``peak_pnl``) and the max drawdown are on that
    scale too, so they run on across a restart. ``realized`` is the exit
    slices filled on the day, ``stats`` the trades whose final exit fell on
    it, ``partial_exits`` the reason and P&L of each slice that closed part
    of a trade on it. ``since`` is when the figures start: the opening
    point, or, ``late_start``, a start the day's earlier part was lost to.
    ``gaps`` are the spans whose trades a start found missing from the
    row: from the row's last write to that start. A day with either is
    ``partial``."""
    date: date
    opening_equity: float
    since: datetime
    late_start: bool
    base_equity: float
    carried_pnl: float = 0.0
    pnl: float = 0.0
    peak_pnl: float = 0.0
    max_drawdown: float = 0.0
    realized: float = 0.0
    stats: TradeStats = field(default_factory=TradeStats)
    partial_exits: list[tuple[str, float]] = field(default_factory=list)
    curve: list[dict[str, Any]] = field(default_factory=list)
    # The last curve point's time: a sample in its minute replaces it.
    curve_last: datetime | None = None
    gaps: list[tuple[datetime, datetime]] = field(default_factory=list)
    # Changed since its row was last written.
    dirty: bool = False

    @property
    def partial(self) -> bool:
        """The figures lack part of the day: its start, or a gap."""
        return self.late_start or bool(self.gaps)

    def pnl_at(self, equity: float) -> float:
        return self.carried_pnl + equity - self.base_equity

    def sample(self, ts: datetime, equity: float) -> None:
        """Move the day's P&L, peak, max drawdown and curve to the equity
        sampled at ``ts``, a time on its date."""
        pnl = self.pnl_at(equity)
        self.pnl = pnl
        self.peak_pnl = max(self.peak_pnl, pnl)
        self.max_drawdown = max(self.max_drawdown, self.peak_pnl - pnl)
        point = {"timestamp": ts.isoformat(timespec="seconds"), "equity": self.opening_equity + pnl}
        last = self.curve_last
        if (self.curve and last is not None and last.date() == self.date
                and int(ts.timestamp() // DAY_CURVE_POINT_SECONDS) == int(last.timestamp() // DAY_CURVE_POINT_SECONDS)):
            self.curve[-1] = point
        else:
            self.curve.append(point)
        self.curve_last = ts
        self.dirty = True

    def move(self, amount: float) -> None:
        """Move the day's P&L by ``amount``, an exit's move of the equity
        since the last sample (its fill against the price that sample valued
        the position at), until the next sample takes the equity whole. The
        peak and the max drawdown are the samples': the other positions'
        marks have moved since that sample too, so the P&L between two
        samples is no value the equity held."""
        self.pnl += amount
        self.dirty = True

    def figures(self, *, pnl: float, peak_pnl: float, max_drawdown: float, is_today: bool) -> dict[str, Any]:
        """The day as the dashboard state, the session report and the
        archive carry it."""
        opening = self.opening_equity
        return {
            "date": self.date.isoformat(),
            "is_today": is_today,
            "label": "today" if is_today else f"{_WEEKDAYS[self.date.weekday()]} {self.date:%m-%d}",
            "since": self.since.isoformat(timespec="seconds"),
            "partial": self.partial,
            "late_start": self.late_start,
            "gaps": [{"from": start.isoformat(timespec="seconds"), "to": end.isoformat(timespec="seconds")}
                     for start, end in self.gaps],
            "pnl": pnl,
            "pnl_pct": (pnl / opening * 100.0) if opening > 0 else None,
            "realized": self.realized,
            "opening_equity": opening,
            "equity": opening + pnl,
            "peak_equity": opening + peak_pnl,
            "max_drawdown": max_drawdown,
            **self.stats.figures(),
            "curve": [dict(point) for point in self.curve],
        }

    def row(self, positions: list[dict[str, Any]]) -> dict[str, Any]:
        """The day's row (``DayLedgerStore``), with the open positions as
        ``PaperAccount._day_row_positions`` gives them."""
        stats = self.stats
        return {
            "opening_equity": self.opening_equity,
            "since": self.since.isoformat(),
            "late_start": self.late_start,
            "gaps": [[start.isoformat(), end.isoformat()] for start, end in self.gaps],
            "pnl": self.pnl,
            "peak_pnl": self.peak_pnl,
            "max_drawdown": self.max_drawdown,
            "realized": self.realized,
            "stats": {"trades": stats.trades, "wins": stats.wins, "losses": stats.losses,
                      "gross_profit": stats.gross_profit, "gross_loss": stats.gross_loss, "net": stats.net},
            "partial_exits": [[reason, pnl] for reason, pnl in self.partial_exits],
            "curve": [[point["timestamp"], point["equity"]] for point in self.curve],
            "positions": positions,
        }


def _finite(value: Any, name: str) -> float:
    """``value`` as a finite float of at most ``_ROW_FIGURE_LIMIT``;
    ValueError naming ``name`` otherwise (a bool too, which float() would
    read as 0 or 1, and an integer beyond a double, which float() refuses
    with OverflowError)."""
    if isinstance(value, bool):
        raise ValueError(f"{name} is a bool")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} is beyond a double ({exc})") from exc
    if not math.isfinite(number) or abs(number) > _ROW_FIGURE_LIMIT:
        raise ValueError(f"{name} is {number}")
    return number


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _ROW_COUNT_LIMIT:
        raise ValueError(f"{name} is {value!r}, not a count")
    return value


def _aware(value: Any, name: str) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError(f"{name} {value!r} has no UTC offset")
    return stamp


def _optional_price(value: Any, name: str) -> float | None:
    return None if value is None else _finite(value, name)


# What ``_ledger_from_row`` raises on a row that does not read (a JSON
# nesting too deep for the parser raises RecursionError).
_ROW_ERRORS = (KeyError, TypeError, ValueError, RecursionError)


def _ledger_from_row(day: date, text: str, updated_at: str,
                     base_equity: float) -> tuple[DayLedger, dict[str, dict[str, Any]], datetime]:
    """The day row ``text`` (``DayLedger.row`` as JSON, last written at
    ``updated_at``) carried into a new process, whose equity is
    ``base_equity``; its open positions by key; and the time of its last
    write. Raises one of ``_ROW_ERRORS`` on a row that does not read."""
    row = json.loads(text)
    if not isinstance(row, dict):
        raise ValueError(f"the row holds a JSON {type(row).__name__}, not an object")
    if not isinstance(row["late_start"], bool):
        raise ValueError(f"late_start is {row['late_start']!r}")
    stats = row["stats"]
    curve = [{"timestamp": _aware(stamp, "a curve time").isoformat(timespec="seconds"),
              "equity": _finite(equity, "a curve equity")} for stamp, equity in row["curve"]]
    pnl = _finite(row["pnl"], "pnl")
    ledger = DayLedger(
        date=day,
        opening_equity=_finite(row["opening_equity"], "opening_equity"),
        since=_aware(row["since"], "since"),
        late_start=row["late_start"],
        base_equity=base_equity,
        carried_pnl=pnl,
        pnl=pnl,
        peak_pnl=_finite(row["peak_pnl"], "peak_pnl"),
        max_drawdown=_finite(row["max_drawdown"], "max_drawdown"),
        realized=_finite(row["realized"], "realized"),
        stats=TradeStats(trades=_count(stats["trades"], "stats.trades"), wins=_count(stats["wins"], "stats.wins"),
                         losses=_count(stats["losses"], "stats.losses"),
                         gross_profit=_finite(stats["gross_profit"], "stats.gross_profit"),
                         gross_loss=_finite(stats["gross_loss"], "stats.gross_loss"),
                         net=_finite(stats["net"], "stats.net")),
        partial_exits=[(str(reason), _finite(slice_pnl, "a partial exit's pnl"))
                       for reason, slice_pnl in row["partial_exits"]],
        curve=curve,
        curve_last=_aware(curve[-1]["timestamp"], "a curve time") if curve else None,
        gaps=[(_aware(start, "a gap's start"), _aware(end, "a gap's end")) for start, end in row["gaps"]],
    )
    positions: dict[str, dict[str, Any]] = {}
    for item in row["positions"]:
        key = str(item["key"])
        if key in positions:
            raise ValueError(f"the row holds two positions of {key}")
        mark = _optional_price(item["mark"], "a position's mark")
        day_ref = _optional_price(item["day_ref"], "a position's day_ref")
        if (mark is None) is not (day_ref is None):
            raise ValueError(f"{key}'s mark {mark!r} and day_ref {day_ref!r}: one is null")
        positions[key] = {"symbol": str(item["symbol"]), "side": Side(item["side"]),
                          "qty": _count(item["qty"], "a position's qty"), "mark": mark, "day_ref": day_ref}
    return ledger, positions, _aware(updated_at, "updated_at")


@dataclass(slots=True)
class _Holding:
    """An open position as the day ledger values it
    (``PaperAccount._holdings``, by position key)."""
    symbol: str
    side: Side
    qty: int
    # The price the account's last sample valued it at (its entry fill when
    # entered since): the stored day figures and ``_last_equity`` hold it at
    # this price, so an exit moves them by its fill against it.
    basis: float
    # The price the day counts its move from: its entry fill, its mark when
    # the day opened, or, restored, the day's row's day_ref; None while a
    # restored one the row lacks waits for its first mark.
    day_ref: float | None

    @property
    def sign(self) -> float:
        return 1.0 if self.side == Side.LONG else -1.0


def _clock_text(stamp: datetime, day: date) -> str:
    """``HH:MM``, with ``MM-DD`` on a date other than ``day``."""
    return f"{stamp:%H:%M}" if stamp.date() == day else f"{stamp:%m-%d %H:%M}"


class PaperAccount:
    """Internal paper ledger used for dry-run mode and estimated live tracking.

    Two scopes. The run's figures cover this process, from its start
    (``run_started_at``): the cash, the equity and its peak, drawdown and
    curve, the realized P&L, and the closed trades' statistics
    (``run_stats``: running sums, never folded from the capped history, so
    they cover every trade the process closed). The day's (``day``, a
    ``DayLedger``) cover one ET trading day, across a restart: the ledger
    rolls at the first sample or exit on a new trading day's ET date, holds
    still on a date that is not one (a weekend or a holiday shows the last
    trading day's figures), and with a ``DayLedgerStore`` its row is written
    at the start, on each exit, at each roll, at shutdown
    (``save_day_ledger``) and at most every ``DAY_LEDGER_SAVE_SECONDS`` while
    its equity-driven figures move, and a new process carries it on when the
    row is the current day's (``_open_day``).

    ``trades`` keeps the last ``max_trade_history`` exit slices (the chart
    markers, the recent exit events) and ``recent_lifecycles`` the last
    ``max_trade_history`` closed trades (the trades table): a trade is
    folded from its own slices when its final exit books, however many
    slices came between. ``trades_outbox`` holds every closed trade no
    trades.csv append has written yet; the engine appends from it and
    clears what an append wrote (``clear_appended``).
    """

    @staticmethod
    def _position_key(position: Position) -> str:
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        return str(meta.get("position_key") or position.symbol)

    @classmethod
    def _trade_lifecycle_id(cls, position: Position) -> str:
        return f"{cls._position_key(position)}|{position.entry_time.isoformat()}"

    def __init__(self, starting_equity: float, max_equity_points: int = 2000, max_trade_history: int = 200,
                 equity_point_seconds: float = 15.0, day_store: DayLedgerStore | None = None,
                 day_trades_exit: Callable[[date], tuple[datetime | None, str | None]] | None = None):
        """``day_store`` keeps the day ledger's row across a restart.
        ``day_trades_exit`` gives the latest exit time of a day's trades in
        trades.csv (every process's), with what it could not read (None when
        it read them all): read once, at the start, when the day's row is
        carried on, to find trades the row stops before."""
        self.starting_equity = float(starting_equity)
        self.cash = float(starting_equity)
        self.realized_pnl = 0.0
        self.last_prices: dict[str, float] = {}
        self.trades: deque[TradeRecord] = deque(maxlen=max_trade_history)
        self.recent_lifecycles: deque[TradeRecord] = deque(maxlen=max_trade_history)
        self.trades_outbox: list[TradeRecord] = []
        self.run_stats = TradeStats()
        # The exit slices of each trade not closed yet, newest first.
        self._open_lifecycles: dict[str, list[TradeRecord]] = {}
        self.equity_curve: deque[EquityPoint] = deque(maxlen=max_equity_points)
        self.equity_point_seconds = float(equity_point_seconds)
        self.peak_equity = float(starting_equity)
        self.max_drawdown = 0.0
        self._lock = RLock()
        now = sessions.now_et()
        self.run_started_at = now
        self._day_store = day_store
        # The last sample's time and equity, the exits booked since added
        # (each open position held at its ``_Holding.basis``): a roll opens
        # the new day there.
        self._last_equity: tuple[datetime, float] = (now, self.starting_equity)
        # The open positions as the day ledger values them, by position key.
        self._holdings: dict[str, _Holding] = {}
        # A restored position not marked yet, by its mark's key (the
        # symbol): its position key, its side's sign, quantity, broker
        # average price and the mark its day's move runs from (None: its
        # first mark).
        self._restored_pending: dict[str, tuple[str, float, int, float, float | None]] = {}
        # The open positions of the day's row this process carried on that
        # no restore has matched yet, by key, and those a restore of their
        # key did not match (another side or quantity): kept in the row, and
        # each logged, until the start-up reconcile ends
        # (``drop_unrestored_positions``) or the day rolls.
        self._carried_positions: dict[str, dict[str, Any]] = {}
        self._carried_unmatched: list[tuple[str, dict[str, Any]]] = []
        self._carried_day: date | None = None
        self._closed_days: dict[date, DayLedger] = {}
        # The current day's last write: its time and what it held.
        self._day_saved: tuple[datetime, tuple] | None = None
        self._day_save_failures = 0
        # The last failed write's time: no write is tried for
        # DAY_LEDGER_SAVE_SECONDS after it (but the shutdown's).
        self._day_failed_at: datetime | None = None
        self._day_unsaved: list[DayLedger] = []
        # The day whose row the store could not read at the start (a lock
        # held, an I/O error): its row is not written over until a re-read
        # finds none; and the day whose row that re-read found, which this
        # process never writes.
        self._day_unread: date | None = None
        self._day_unpersisted: date | None = None
        self.day = self._open_day(now, day_trades_exit)
        self.capture_snapshot({}, now)

    # ------------------------------------------------------------------
    # The day ledger
    # ------------------------------------------------------------------
    def _open_day(self, now: datetime,
                  day_trades_exit: Callable[[date], tuple[datetime | None, str | None]] | None) -> DayLedger:
        """The day this process starts on (``sessions.latest_session_date``:
        today on a trading day, else the last trading day): carried on from
        the store's row of that day, else started now, at the process's
        starting equity, marked ``late_start``. A row that is missing or
        unreadable, and a store that cannot be read, are logged once, with
        the error's type; rows dated after the day are named in a WARNING.
        A carried row the day's trades.csv holds exits after (its last write
        is older than them: an older version ran, or its writes failed), or
        that trades.csv cannot be checked against, is carried on with a gap
        from its last write to now, logged."""
        day = sessions.latest_session_date(now)
        fresh = DayLedger(date=day, opening_equity=self.starting_equity, since=now, late_start=True,
                          base_equity=self.starting_equity)
        store = self._day_store
        if store is None:
            return fresh
        try:
            stored, later = store.read_day(day.isoformat())
        except Exception as exc:
            self._day_unread = day
            LOG.warning("Day ledger: could not read %s (%s: %s); the %s figures start now, at %s, and its row "
                        "is not written over until a re-read finds none", store.path, type(exc).__name__, exc,
                        day, now.isoformat(timespec="seconds"))
            return fresh
        if later:
            LOG.warning("Day ledger: %s holds rows dated after %s (%s), written under a clock set ahead; they are "
                        "left as they are", store.path, day, ", ".join(later))
        if stored is None:
            LOG.info("Day ledger: %s holds no %s row; the day's figures start now, at %s", store.path, day,
                     now.isoformat(timespec="seconds"))
            return fresh
        try:
            ledger, positions, updated_at = _ledger_from_row(day, *stored, self.starting_equity)
        except _ROW_ERRORS as exc:
            LOG.warning("Day ledger: the %s row in %s could not be read (%s: %s); the day's figures start now, at %s",
                        day, store.path, type(exc).__name__, exc, now.isoformat(timespec="seconds"))
            return fresh
        self._carried_positions = positions
        self._carried_day = day
        if day_trades_exit is not None:
            last_exit, problem = day_trades_exit(day)
            stops = _clock_text(updated_at, day)
            if problem is not None:
                ledger.gaps.append((updated_at, now))
                LOG.warning("Day ledger: the %s row stops at %s and trades.csv could not be checked against it (%s): "
                            "the day is carried on, partial from %s", day, stops, problem, stops)
            elif last_exit is not None and last_exit > updated_at:
                ledger.gaps.append((updated_at, now))
                LOG.warning("Day ledger: the %s row stops at %s, and trades.csv holds the day's trades that exited "
                            "after it (the last at %s): the day is carried on, partial, without them", day, stops,
                            _clock_text(last_exit, day))
        LOG.info("Day ledger: %s carried on from its row (since %s%s): pnl=%.2f realized=%.2f trades=%d "
                 "max_drawdown=%.2f open_positions=%d", day, ledger.since.isoformat(timespec="seconds"),
                 ", partial" if ledger.partial else "", ledger.pnl, ledger.realized, ledger.stats.trades,
                 ledger.max_drawdown, len(positions))
        return ledger

    def drop_unrestored_positions(self) -> None:
        """The start-up reconcile ended (the engine's first successful one,
        or the day rolled first): the positions of the day's row it did not
        restore leave the row, each logged with its move up to the restart,
        which stays in the day's P&L, never realized and in no trade (a
        dry run restores no paper position). Nothing after the first call."""
        with self._lock:
            dropped = list(self._carried_positions.items()) + self._carried_unmatched
            self._carried_positions = {}
            self._carried_unmatched = []
            day = self._carried_day
            if dropped:
                self.day.dirty = True
        for key, item in dropped:
            mark, day_ref = item["mark"], item["day_ref"]
            sign = 1.0 if item["side"] == Side.LONG else -1.0
            amount = 0.0 if mark is None or day_ref is None else sign * item["qty"] * (mark - day_ref)
            LOG.info("Day ledger: %s %s %d%s, open in the %s row, was not restored: its move up to the restart, "
                     "%+.2f, stays in the day's P&L, never realized and in no trade", item["symbol"],
                     item["side"].value, item["qty"], "" if key == item["symbol"] else f" ({key})", day, amount)

    def _roll_day(self, day: date) -> None:
        """Open ``day``, a trading day after the current one, at the last
        sample's equity (the opening point); the day it ends is kept for a
        late close. Each open position's day move runs from the price that
        sample held it at. Under the lock."""
        ended = self.day
        opened_at, opening = self._last_equity
        LOG.info("Day ledger: %s ended (pnl=%.2f realized=%.2f trades=%d max_drawdown=%.2f); %s opens at %.2f",
                 ended.date, ended.pnl, ended.realized, ended.stats.trades, ended.max_drawdown, day, opening)
        self.drop_unrestored_positions()
        self._closed_days[ended.date] = ended
        for old in sorted(self._closed_days)[:-DAY_LEDGERS_KEPT]:
            del self._closed_days[old]
        pending = {item[0]: item[4] for item in self._restored_pending.values()}
        for key, holding in self._holdings.items():
            holding.day_ref = pending[key] if key in pending else holding.basis
        self.day = DayLedger(date=day, opening_equity=opening, since=opened_at, late_start=False, base_equity=opening,
                             curve=[{"timestamp": opened_at.isoformat(timespec="seconds"), "equity": opening}],
                             curve_last=opened_at, dirty=True)
        self._day_saved = None
        self._day_unsaved = [ledger for ledger in self._day_unsaved if ledger is not ended] + [ended, self.day]

    def _day_for(self, ts: datetime) -> DayLedger | None:
        """The ledger an event at ``ts`` books into, after rolling to its
        date when that is a later trading day; None on a date that is not
        the ledger's (a weekend or a holiday, or a clock set back). Under the
        lock."""
        day = ts.date()
        if day > self.day.date and sessions.is_weekday_session_day(day):
            self._roll_day(day)
        return self.day if day == self.day.date else None

    def _settle_restored_mark(self, symbol: str, price: float) -> float:
        """A restored position's first price: the day's P&L leaves out its
        move from the broker's average price to the mark it runs from (that
        move, returned; 0.0 for any other symbol), and the position is held
        at that mark from here, so the stored figures do not move. Under
        the lock."""
        pending = self._restored_pending.pop(symbol, None)
        if pending is None:
            return 0.0
        key, sign, qty, average, reference = pending
        start = price if reference is None else reference
        excluded = sign * qty * (start - average)
        self.day.base_equity += excluded
        sampled_at, equity = self._last_equity
        self._last_equity = (sampled_at, equity + excluded)
        holding = self._holdings.get(key)
        if holding is not None and holding.qty > 0:
            holding.basis += (start - average) * qty / holding.qty
            if holding.day_ref is None:
                holding.day_ref = start
        return excluded

    def _day_row_positions(self) -> list[dict[str, Any]]:
        """The day's row's open positions: each held one (its key, symbol,
        side, quantity, the mark the day's figures hold it at, and the price
        the day counts its move from; a restored one not marked yet at the
        mark its move runs from, null when that is its first), and the
        carried ones not restored yet, as the row held them. Under the
        lock."""
        pending = {item[0]: item[4] for item in self._restored_pending.values()}
        items = []
        for key, holding in sorted(self._holdings.items()):
            items.append({"key": key, "symbol": holding.symbol, "side": holding.side.value, "qty": holding.qty,
                          "mark": pending[key] if key in pending else holding.basis, "day_ref": holding.day_ref})
        held = set(self._holdings)
        for key, item in sorted(self._carried_positions.items()):
            if key not in held:
                items.append({"key": key, "symbol": item["symbol"], "side": item["side"].value, "qty": item["qty"],
                              "mark": item["mark"], "day_ref": item["day_ref"]})
        return items

    def _day_save_due(self, ts: datetime) -> bool:
        """Whether the current day's row is due a write at ``ts``: never
        written, or its P&L, peak, max drawdown or open positions changed
        and its last write is ``DAY_LEDGER_SAVE_SECONDS`` old (or stamped
        after ``ts``: a clock set back); never for a row this process does
        not write. Under the lock."""
        if self._day_store is None or not self.day.dirty or self.day.date == self._day_unpersisted:
            return False
        if self._day_saved is None:
            return True
        saved_at, saved = self._day_saved
        if 0 <= (ts - saved_at).total_seconds() < DAY_LEDGER_SAVE_SECONDS:
            return False
        return saved != self._day_signature()

    def _day_signature(self) -> tuple:
        day = self.day
        return (day.pnl, day.peak_pnl, day.max_drawdown, day.late_start, tuple(day.gaps),
                tuple(tuple(sorted(item.items())) for item in self._day_row_positions()))

    def _save_days(self, ts: datetime, *, final: bool = False) -> None:
        """Write the rows ``_day_unsaved`` names, oldest first, outside the
        lock. A failure is logged with its type (a WARNING on the first of a
        run of failures) and its row stays unsaved; no write is tried for
        ``DAY_LEDGER_SAVE_SECONDS`` after it, but at the shutdown
        (``final``), and the store's error that says the file cannot be
        written now (``DayLedgerStore.UNAVAILABLE``) ends the batch: each
        write waits ``DAY_LEDGER_BUSY_SECONDS`` at most. The row of the day
        the start could not read is re-read first (``_writable``)."""
        store = self._day_store
        with self._lock:
            if store is None:
                self._day_unsaved.clear()
                return
            failed_at = self._day_failed_at
            if not final and failed_at is not None and \
                    0 <= (ts - failed_at).total_seconds() < DAY_LEDGER_SAVE_SECONDS:
                return
            pending = list(self._day_unsaved)
            self._day_unsaved.clear()
            rows = [(ledger, ledger.row(self._day_row_positions() if ledger is self.day else []),
                     self._day_signature() if ledger is self.day else None) for ledger in pending]
        for index, (ledger, row, signature) in enumerate(rows):
            action = "write"
            try:
                if ledger.date == self._day_unread:
                    action = "re-read"
                    if not self._writable(store, ledger.date):
                        continue
                    action = "write"
                if ledger.date == self._day_unpersisted:
                    continue
                store.save(ledger.date.isoformat(), row, ts)
            except Exception as exc:
                self._day_save_failures += 1
                if self._day_save_failures == 1:
                    LOG.warning("Day ledger: could not %s the %s row in %s (%s: %s); retried in %.0f s", action,
                                ledger.date, store.path, type(exc).__name__, exc, DAY_LEDGER_SAVE_SECONDS)
                else:
                    LOG.debug("Day ledger: the %s row's %s failed again (%d in a row): %s: %s", ledger.date, action,
                              self._day_save_failures, type(exc).__name__, exc)
                unavailable = isinstance(exc, store.UNAVAILABLE)
                with self._lock:
                    self._day_failed_at = ts
                    for unsaved, _row, _signature in rows[index:] if unavailable else rows[index:index + 1]:
                        if unsaved not in self._day_unsaved:
                            self._day_unsaved.append(unsaved)
                if unavailable:
                    break
                continue
            if self._day_save_failures:
                LOG.info("Day ledger: the %s row written again after %d failed attempt(s)", ledger.date,
                         self._day_save_failures)
                self._day_save_failures = 0
            with self._lock:
                self._day_failed_at = None
                ledger.dirty = ledger in self._day_unsaved
                if ledger is self.day and signature is not None:
                    self._day_saved = (ts, signature)

    def _writable(self, store: DayLedgerStore, day: date) -> bool:
        """Whether this process may write ``day``'s row, which it could not
        read at its start: re-read now, a row found is left as it is (a
        WARNING, and the day is never written by this process), none found
        lets the write go on. Raises what the read raises."""
        stored, _later = store.read_day(day.isoformat())
        with self._lock:
            self._day_unread = None
            if stored is None:
                LOG.info("Day ledger: %s holds no %s row now; this process's figures of the day are written from "
                         "here", store.path, day)
                return True
            self._day_unpersisted = day
        LOG.warning("Day ledger: the %s row in %s, which this process could not read at its start, is there: it is "
                    "left as it is, and this process's %s figures are not written", day, store.path, day)
        return False

    def save_day_ledger(self) -> None:
        """Write the current day's row when it changed since its last write,
        and any row a failed write left (the shutdown's: tried whatever the
        last failure's time)."""
        with self._lock:
            if self.day.dirty and self.day not in self._day_unsaved:
                self._day_unsaved.append(self.day)
        self._save_days(sessions.now_et(), final=True)

    def day_figures(self, day: date) -> dict[str, Any] | None:
        """``day``'s figures as of the last sample on it (``DayLedger.figures``),
        when the account holds that day: the current one or one of the last
        ``DAY_LEDGERS_KEPT`` it rolled out. None for any other day."""
        with self._lock:
            ledger = self.day if self.day.date == day else self._closed_days.get(day)
            if ledger is None:
                return None
            return ledger.figures(pnl=ledger.pnl, peak_pnl=ledger.peak_pnl, max_drawdown=ledger.max_drawdown,
                                  is_today=ledger.date == sessions.now_et().date())

    def day_partial_exits(self, day: date) -> list[tuple[str, float]] | None:
        """The reason and P&L of each slice that closed part of a trade on
        ``day``, oldest first; None when the account does not hold the day
        (``day_figures``)."""
        with self._lock:
            ledger = self.day if self.day.date == day else self._closed_days.get(day)
            return None if ledger is None else list(ledger.partial_exits)

    # ------------------------------------------------------------------
    # Booking
    # ------------------------------------------------------------------
    def mark_prices(self, prices: dict[str, float]) -> None:
        """Mark each symbol at its price. A restored position's first mark
        settles where its day's move runs from (``_settle_restored_mark``):
        from then on the account holds it at that mark, so a day the next
        sample opens counts it from there."""
        with self._lock:
            for symbol, price in prices.items():
                number = safe_float(price)
                if number is not None:
                    if self._restored_pending:
                        self._settle_restored_mark(str(symbol), number)
                    self.last_prices[str(symbol)] = number

    def record_entry(self, position: Position, fill_price: float) -> None:
        fill_price = float(fill_price)
        with self._lock:
            if position.side == Side.LONG:
                self.cash -= position.qty * fill_price
            else:
                self.cash += position.qty * fill_price
            self.last_prices[position.symbol] = fill_price
            self._hold(position, fill_price)
            LOG.log(TRADEFLOW_LEVEL, "Paper account entry recorded %s qty=%s @ %.4f cash=%.2f", position.symbol, position.qty, fill_price, self.cash)

    def _hold(self, position: Position, fill_price: float) -> None:
        """The day ledger's holding of ``position``'s key gains its shares at
        ``fill_price`` (quantity-weighted into one already held). Under the
        lock."""
        key = self._position_key(position)
        qty = int(position.qty)
        holding = self._holdings.get(key)
        if holding is None or holding.side != position.side or holding.qty <= 0:
            self._holdings[key] = _Holding(symbol=position.symbol, side=position.side, qty=qty, basis=fill_price,
                                           day_ref=fill_price)
            return
        total = holding.qty + qty
        holding.basis = (holding.qty * holding.basis + qty * fill_price) / total
        if holding.day_ref is not None:
            holding.day_ref = (holding.qty * holding.day_ref + qty * fill_price) / total
        holding.qty = total

    def record_restored_entry(self, position: Position, fill_price: float) -> None:
        """``record_entry`` for a position the broker reconcile restored at
        the broker's average price (``fill_price``), whose move since its
        entry the run's figures then count. The day's P&L counts its move
        from the mark the day's row valued it at when the row's position
        has its key, side and quantity (the earlier process held it), else
        from its first mark after the restore. A row's position of the key
        that does not match is not restored (``drop_unrestored_positions``
        logs it)."""
        self.record_entry(position, fill_price)
        with self._lock:
            key = self._position_key(position)
            carried = self._carried_positions.pop(key, None)
            reference = day_ref = None
            if carried is not None and carried["side"] == position.side and carried["qty"] == int(position.qty):
                reference, day_ref = carried["mark"], carried["day_ref"]
            elif carried is not None:
                self._carried_unmatched.append((key, carried))
            self._holdings[key].day_ref = day_ref
            sign = 1.0 if position.side == Side.LONG else -1.0
            self._restored_pending[position.symbol] = (key, sign, int(position.qty), float(fill_price), reference)
            if reference is None:
                LOG.info("Day ledger: restored %s's move is counted from its first mark after the restore", key)
            else:
                LOG.info("Day ledger: restored %s's move is counted from %.4f, its mark in the day's row", key,
                         reference)

    def record_exit(
        self,
        position: Position,
        fill_price: float,
        reason: str,
        *,
        final_exit: bool = True,
        remaining_qty_after_exit: int = 0,
        fill_price_estimated: bool = False,
        broker_recovered: bool = False,
    ) -> float:
        fill_price = float(fill_price)
        with self._lock:
            exit_ts = sessions.now_et()
            # Before the exit moves the equity: a new trading day opens at
            # the last sample.
            ledger = self._day_for(exit_ts)
            if self._restored_pending:
                self._settle_restored_mark(position.symbol, fill_price)
            # The exit moves the equity by its fill against the price the
            # last sample held the position at: the day's P&L moves now, not
            # at the next sample, and so does the equity a later roll opens
            # at (an exit on a weekend or a holiday is in no day's figures).
            moved = self._release(position, fill_price, final_exit=final_exit)
            sampled_at, equity = self._last_equity
            self._last_equity = (sampled_at, equity + moved)
            if position.side == Side.LONG:
                self.cash += position.qty * fill_price
            else:
                self.cash -= position.qty * fill_price
            realized = favorable_move(position.side, position.entry_price, fill_price) * position.qty
            self.realized_pnl += realized
            self.last_prices[position.symbol] = fill_price
            hold_minutes = max(0.0, (exit_ts - position.entry_time).total_seconds() / 60.0)
            metadata = position.metadata or {}
            # Pull diagnostic fields from metadata; all are optional so
            # trades from strategies that don't populate them stay valid.
            # MAE/MFE prefer the clean key and fall back to the engine's
            # diag_ copy written during management.
            initial_stop = first_float(metadata, "initial_stop_price", finite=True)
            initial_risk = (
                abs(float(position.entry_price) - initial_stop)
                if initial_stop is not None and initial_stop > 0
                else None
            )
            trade = TradeRecord(
                symbol=position.symbol,
                strategy=str(position.strategy),
                side=str(position.side.value),
                qty=position.qty,
                entry_price=float(position.entry_price),
                exit_price=fill_price,
                entry_time=position.entry_time,
                exit_time=exit_ts,
                realized_pnl=realized,
                return_pct=_return_pct(position.side, float(position.entry_price), fill_price),
                hold_minutes=hold_minutes,
                reason=reason,
                asset_type=asset_type_of(metadata),
                underlying=str(metadata.get("underlying") or "").upper().strip() or None,
                exchange=str(metadata.get("exchange") or "").upper().strip() or None,
                option_type=(str(metadata.get("option_type") or "").upper().strip() or None),
                lifecycle_id=self._trade_lifecycle_id(position),
                partial_exit=not bool(final_exit),
                final_exit=bool(final_exit),
                remaining_qty_after_exit=max(0, int(remaining_qty_after_exit)),
                fill_price_estimated=bool(fill_price_estimated),
                broker_recovered=bool(broker_recovered),
                regime=(str(metadata.get("regime")) if metadata.get("regime") else None),
                armed_retest_status=(
                    str(metadata.get("armed_retest_status"))
                    if metadata.get("armed_retest_status") else None),
                armed_retest_waited_minutes=first_float(metadata, "armed_retest_waited_minutes"),
                initial_risk_per_unit=initial_risk,
                max_favorable_pnl=first_float(metadata, "best_unrealized_pnl", "diag_best_unrealized_pnl"),
                max_adverse_pnl=first_float(metadata, "worst_unrealized_pnl", "diag_worst_unrealized_pnl"),
                # Measured from the touch only when the touch is stamped
                # beside it: a position entered before 2026-10-06 carries the
                # old |fill - limit| / limit reading under the same key.
                entry_slippage_pct=(first_float(metadata, "entry_slippage_pct")
                                    if metadata.get("entry_touch_price") is not None else None),
                entry_limit_buffer_pct=first_float(metadata, "entry_limit_buffer_pct"),
                realized_entry_risk=first_float(metadata, "realized_entry_risk"),
                entry_risk_budget=first_float(metadata, "entry_risk_budget"),
                entry_risk_overage_frac=first_float(metadata, "entry_risk_overage_frac"),
                best_mark_price=first_float(metadata, "diag_best_mark_price"),
                target_reached_price=first_float(metadata, "diag_target_reached_price"),
            )
            # LIFO: newest trade at index 0 (consumers iterate from the left).
            self.trades.appendleft(trade)
            closed = self._close_slice(trade)
            if ledger is not None:
                ledger.move(moved)
                ledger.realized += realized
                if trade.partial_exit:
                    ledger.partial_exits.append((reason, realized))
                if closed is not None:
                    ledger.stats.add(closed.realized_pnl)
                if ledger not in self._day_unsaved:
                    self._day_unsaved.append(ledger)
            LOG.log(
                TRADEFLOW_LEVEL,
                "Paper account exit recorded %s realized=%.2f cash=%.2f final_exit=%s remaining_qty=%s broker_recovered=%s estimated_fill=%s",
                position.symbol,
                realized,
                self.cash,
                bool(final_exit),
                max(0, int(remaining_qty_after_exit)),
                bool(broker_recovered),
                bool(fill_price_estimated),
            )
            save = bool(self._day_unsaved)
        if save:
            self._save_days(exit_ts)
        return realized

    def _release(self, position: Position, fill_price: float, *, final_exit: bool) -> float:
        """The equity's move as an exit slice of ``position`` fills at
        ``fill_price``, against the price its holding is valued at; the
        holding loses the slice's shares (all of them at the final exit).
        A position the account holds no entry of is valued at its mark,
        with a WARNING. Under the lock."""
        key = self._position_key(position)
        qty = int(position.qty)
        holding = self._holdings.get(key)
        sign = 1.0 if position.side == Side.LONG else -1.0
        if holding is None:
            LOG.warning("Day ledger: %s exits without an entry the account booked; its exit is measured from its "
                        "mark", key)
            return sign * qty * (fill_price - float(self.last_prices.get(position.symbol, position.entry_price)))
        moved = sign * qty * (fill_price - holding.basis)
        holding.qty -= qty
        if final_exit or holding.qty <= 0:
            del self._holdings[key]
        return moved

    def _close_slice(self, trade: TradeRecord) -> TradeRecord | None:
        """Fold the exit slice ``trade`` into its trade: kept with the trade's
        earlier slices while it stays open (None), and on its final slice
        folded with them (``closed_trade_lifecycles``) into the closed trade,
        which joins the run's statistics, ``recent_lifecycles`` and the
        trades.csv outbox. Under the lock."""
        key = str(trade.lifecycle_id)
        if not trade.final_exit:
            self._open_lifecycles.setdefault(key, []).insert(0, trade)
            return None
        (closed,) = closed_trade_lifecycles([trade] + self._open_lifecycles.pop(key, []))
        self.run_stats.add(closed.realized_pnl)
        self.recent_lifecycles.appendleft(closed)
        self.trades_outbox.append(closed)
        return closed

    def clear_appended(self, appended: Iterable[TradeRecord]) -> None:
        """Drop the closed trades a trades.csv append wrote (each the very
        record ``trades_outbox`` holds) from the outbox."""
        written = {id(trade) for trade in appended}
        with self._lock:
            self.trades_outbox = [trade for trade in self.trades_outbox if id(trade) not in written]

    @staticmethod
    def _position_market_value(position: Position, last_price: float) -> float:
        if position.side == Side.LONG:
            return position.qty * last_price
        return -position.qty * last_price

    def _position_summary(self, position: Position) -> dict[str, Any]:
        last_price = float(self.last_prices.get(position.symbol, position.entry_price))
        unrealized = position_unrealized_at_price(position, last_price)
        market_value = self._position_market_value(position, last_price)
        metadata = position.metadata or {}
        asset_type = asset_type_of(metadata)
        breakeven = None
        _max_risk = None
        max_reward = None
        risk_label = None

        if asset_type == ASSET_TYPE_OPTION_VERTICAL:
            qty = int(position.qty)
            per_contract_risk = safe_float(metadata.get("max_loss_per_contract"), 0.0, finite=True)
            per_contract_reward = safe_float(metadata.get("max_profit_per_contract"), finite=True)
            _max_risk = per_contract_risk * qty if per_contract_risk > 0 else None
            if per_contract_reward is not None:
                max_reward = per_contract_reward * qty
            breakeven = safe_float(metadata.get("breakeven_underlying"), finite=True)
            spread_type = metadata.get("spread_type")
            if spread_type:
                risk_label = str(spread_type)
        elif asset_type == ASSET_TYPE_OPTION_SINGLE:
            qty = int(position.qty)
            per_contract_risk = safe_float(metadata.get("max_loss_per_contract"), 0.0, finite=True)
            _max_risk = per_contract_risk * qty if per_contract_risk > 0 else None
            breakeven = safe_float(metadata.get("breakeven_underlying"), finite=True)
            risk_label = str(metadata.get("option_type") or "long_option").lower()
        else:
            # Use the TRADE's INITIAL stop/target for max_risk/max_reward so
            # the dashboard's R/R reflects the planned trade ratio at entry,
            # not the current managed ratio. Adaptive management (breakeven
            # ratchet, profit-lock, ladder rung promotion) moves stop_price
            # and target_price as the trade progresses — using the live
            # values produced R/R = 0/0 = "—" once stop ratcheted to entry,
            # which is the most common state for winning trades. The
            # ``initial_*`` fields are stamped at entry by entry_gatekeeper
            # (lines 677-678 / 1215-1216) and never mutate after.
            initial_stop = safe_float(metadata.get("initial_stop_price"), finite=True)
            if initial_stop is None:
                initial_stop = float(position.stop_price or position.entry_price)
            if position.side == Side.LONG:
                risk_per_share = max(0.0, float(position.entry_price) - initial_stop)
                _max_risk = risk_per_share * int(position.qty)
                breakeven = None
                risk_label = "long_stock"
            else:
                risk_per_share = max(0.0, initial_stop - float(position.entry_price))
                _max_risk = risk_per_share * int(position.qty)
                breakeven = None
                risk_label = "short_stock"

        if max_reward is None:
            # Prefer initial_target_price (stamped at entry, immutable);
            # fall back to current position.target_price for legacy
            # positions or strategies that don't stamp the initial. One that
            # is not a finite number reads as unstamped (2026-09-26): a NaN
            # read as no reward at all, an infinity as an infinite one.
            target_for_reward = safe_float(metadata.get("initial_target_price"), finite=True)
            if target_for_reward is None:
                target_for_reward = safe_float(position.target_price, finite=True)
            if target_for_reward is not None:
                if position.side == Side.LONG:
                    reward_per_unit = max(0.0, target_for_reward - float(position.entry_price))
                else:
                    reward_per_unit = max(0.0, float(position.entry_price) - target_for_reward)
                if reward_per_unit > 0:
                    max_reward = reward_per_unit * int(position.qty)

        # Surface the trade's INITIAL stop/target alongside the live ones
        # so the dashboard's progress bar can use a stable trade range
        # (entry → initial_target) instead of recomputing the range each
        # time adaptive management ratchets the live stop/target. Without
        # this, ``adaptive_breakeven_rr`` collapsing stop to entry made
        # ``adverseSpan = 0`` and the bar fell back to the left-anchored
        # P&L view with no Stop/Target labels. A value that is not a finite
        # number reads as missing; a NaN or ±inf went into the row as is
        # (2026-09-26).
        initial_stop_payload = safe_float(metadata.get("initial_stop_price"), finite=True)
        initial_target_payload = safe_float(metadata.get("initial_target_price"), finite=True)
        return {
            "symbol": position.symbol,
            "strategy": position.strategy,
            "side": position.side.value,
            "qty": int(position.qty),
            "asset_type": asset_type,
            "risk_label": risk_label,
            "entry_price": float(position.entry_price),
            "last_price": last_price,
            "stop_price": float(position.stop_price),
            "target_price": float(position.target_price) if position.target_price is not None else None,
            "initial_stop_price": initial_stop_payload,
            "initial_target_price": initial_target_payload,
            "market_value": market_value,
            "unrealized_pnl": unrealized,
            "max_risk": _max_risk,
            "max_reward": max_reward,
            "breakeven": breakeven,
            "return_pct": _return_pct(position.side, float(position.entry_price), last_price),
            "entry_time": position.entry_time.isoformat(),
            "highest_price": float(position.highest_price) if position.highest_price is not None else None,
            "lowest_price": float(position.lowest_price) if position.lowest_price is not None else None,
            "reference_symbol": position.reference_symbol,
            "underlying": metadata.get("underlying"),
            "exchange": metadata.get("exchange"),
            "spread_type": metadata.get("spread_type"),
            "spread_style": metadata.get("spread_style"),
            "option_type": metadata.get("option_type"),
            "option_strike": metadata.get("option_strike"),
            "long_strike": metadata.get("long_strike"),
            "short_strike": metadata.get("short_strike"),
            "sold_strike": metadata.get("sold_strike"),
            "bought_strike": metadata.get("bought_strike"),
        }

    def _equity_rows(self, positions: dict[str, Position]) -> tuple[list[dict[str, Any]], float, float]:
        """The position rows, their market value and the total equity, at the
        marked prices. Under the lock."""
        position_rows = [self._position_summary(position) for position in positions.values()]
        market_value = sum(row["market_value"] for row in position_rows)
        return position_rows, market_value, self.cash + market_value

    def _curve_point_due(self, ts: datetime) -> bool:
        """Whether a sample at ``ts`` adds a curve point (or, at the last
        point's time, replaces it): the curve is empty, its last point is at
        least ``equity_point_seconds`` old, or that point is stamped after
        ``ts`` (a clock set back: the cadence restarts from the new time
        instead of waiting for the clock to pass the old point, and at 0
        every sample keeps its point, as before 2026-10-05). Under the lock."""
        if not self.equity_curve:
            return True
        age = (ts - self.equity_curve[-1].timestamp).total_seconds()
        return not 0 < age < self.equity_point_seconds

    def record_equity_point(self, positions: dict[str, Position], timestamp: datetime | None = None, *,
                            force_point: bool) -> None:
        """Sample the equity at ``timestamp`` (now by default). Every call
        moves the peak and the max drawdown; the curve gets the point when
        one is due (``_curve_point_due``: one every ``equity_point_seconds``,
        every call at 0) or with ``force_point`` (``capture_snapshot``, so
        the session report's and the archive's curve end on their own
        moment). A point stamped at the last point's time replaces it.

        The engine samples once a pass, before the dashboard build
        (``IntradayBot._publish_state``), whether or not the build runs;
        until 2026-10-05 the build itself sampled, so the drawdown was
        sampled only as often as the dashboard was built. Every sample also
        added a point until then: at this release's ~2.5 s passes the 2,000
        points of ``max_equity_points`` would hold only the session's last
        80-90 minutes; at one every 15 s they hold at least 8.3 hours.

        The sample moves the day ledger too (``DayLedger.sample``): on the
        ledger's date, after rolling it to a later trading day's, and never
        on another date; its row is written when that is due
        (``_day_save_due``)."""
        ts = timestamp or sessions.now_et()
        with self._lock:
            position_rows, market_value, total_equity = self._equity_rows(positions)
            self.peak_equity = max(self.peak_equity, total_equity)
            self.max_drawdown = max(self.max_drawdown, self.peak_equity - total_equity)
            ledger = self._day_for(ts)
            self._hold_at_sample(positions)
            if ledger is not None:
                ledger.sample(ts, total_equity)
                if self._day_save_due(ts) and ledger not in self._day_unsaved:
                    self._day_unsaved.append(ledger)
            self._last_equity = (ts, total_equity)
            if force_point or self._curve_point_due(ts):
                self._add_curve_point(ts, position_rows, market_value, total_equity)
            save = bool(self._day_unsaved)
        if save:
            self._save_days(ts)

    def _hold_at_sample(self, positions: dict[str, Position]) -> None:
        """The day ledger's holdings as a sample values ``positions`` (every
        open one): each at its mark (a restored one not marked yet stays at
        its average price), at its side and quantity; a position the account
        booked no entry of is held from this sample, its day's move from
        here; a holding no position is left for goes. Under the lock."""
        held: dict[str, _Holding] = {}
        for position in positions.values():
            key = self._position_key(position)
            mark = float(self.last_prices.get(position.symbol, position.entry_price))
            holding = self._holdings.get(key)
            if holding is None:
                holding = _Holding(symbol=position.symbol, side=position.side, qty=int(position.qty), basis=mark,
                                   day_ref=mark)
            elif position.symbol not in self._restored_pending:
                holding.basis = mark
            holding.side, holding.qty = position.side, int(position.qty)
            held[key] = holding
        self._holdings = held

    def _add_curve_point(self, ts: datetime, position_rows: list[dict[str, Any]], market_value: float,
                         total_equity: float) -> None:
        """The run curve's point at ``ts``; one stamped at the last point's
        time replaces it. Under the lock."""
        unrealized_pnl = sum(row["unrealized_pnl"] for row in position_rows)
        point = EquityPoint(
            timestamp=ts,
            equity=total_equity,
            cash=self.cash,
            market_value=market_value,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized_pnl,
            open_positions=len(position_rows),
        )
        if not self.equity_curve or self.equity_curve[-1].timestamp != point.timestamp:
            self.equity_curve.append(point)
        else:
            self.equity_curve[-1] = point

    def capture_snapshot(self, positions: dict[str, Position], timestamp: datetime | None = None) -> dict[str, Any]:
        """``record_equity_point`` with its point whatever the interval, then
        the ``snapshot`` with the run's curve (``equity_curve``, the last
        ``max_equity_points`` points, ending on the capture moment): the
        session report's and the archive's read."""
        with self._lock:
            self.record_equity_point(positions, timestamp, force_point=True)
            return {**self.snapshot(positions),
                    "equity_curve": [self._equity_point_to_dict(item) for item in self.equity_curve]}

    def snapshot(self, positions: dict[str, Position]) -> dict[str, Any]:
        """The account at the marked prices, without sampling it (the
        dashboard's read; ``capture_snapshot`` samples first and adds the
        run's curve, which no page draws: until 2026-10-12 every dashboard
        state carried it, each point with every traded symbol's P&L).

        The run's figures (since ``run_started_at``): the equity, its peak,
        drawdown and max drawdown, the realized P&L, and the closed trades' statistics
        (``closed_trades``, ``total_trades`` with the open positions,
        ``wins``, ``losses``, ``win_rate``, ``average_trade``,
        ``profit_factor``: the run's every trade, ``run_stats``). The
        recent trades are the last 20 closed trades and the last 20 exit
        slices. ``day`` is the day ledger's (``DayLedger.figures``): on its
        own date (``is_today``) its P&L, peak and max drawdown at the marked
        prices, as the run's are; on a later date, a weekend or a holiday,
        the last trading day's, as of its last sample."""
        with self._lock:
            position_rows, market_value, total_equity = self._equity_rows(positions)
            gross_market_value = sum(abs(float(row["market_value"])) for row in position_rows)
            gross_max_risk = sum(abs(float(row["max_risk"])) for row in position_rows if row.get("max_risk") is not None)
            unrealized_pnl = sum(row["unrealized_pnl"] for row in position_rows)
            peak_equity = max(self.peak_equity, total_equity)
            current_drawdown = peak_equity - total_equity
            max_drawdown = max(self.max_drawdown, current_drawdown)
            run = self.run_stats.figures()
            day = self.day
            if day.date == sessions.now_et().date():
                day_pnl = day.pnl_at(total_equity)
                day_peak = max(day.peak_pnl, day_pnl)
                day_figures = day.figures(pnl=day_pnl, peak_pnl=day_peak,
                                          max_drawdown=max(day.max_drawdown, day_peak - day_pnl), is_today=True)
            else:
                day_figures = day.figures(pnl=day.pnl, peak_pnl=day.peak_pnl, max_drawdown=day.max_drawdown,
                                          is_today=False)

            return {
                "starting_equity": self.starting_equity,
                "cash": self.cash,
                "market_value": market_value,
                "gross_market_value": gross_market_value,
                "gross_max_risk": gross_max_risk,
                "total_equity": total_equity,
                "peak_equity": peak_equity,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "drawdown": current_drawdown,
                "max_drawdown": max_drawdown,
                "open_positions": len(position_rows),
                "closed_trades": run["trades"],
                "total_trades": run["trades"] + len(position_rows),
                "wins": run["wins"],
                "losses": run["losses"],
                "win_rate": run["win_rate"],
                "average_trade": run["average_trade"],
                "profit_factor": run["profit_factor"],
                "positions": position_rows,
                "recent_trades": [self._trade_to_dict(trade) for trade in itertools.islice(self.recent_lifecycles, 20)],
                "recent_trade_events": [self._trade_to_dict(trade) for trade in itertools.islice(self.trades, 20)],
                "run_started_at": self.run_started_at.isoformat(),
                "day": day_figures,
            }

    @staticmethod
    def _trade_to_dict(trade: TradeRecord) -> dict[str, Any]:
        """Serialize one trade. A timestamp that is not a datetime serializes
        to None rather than raising.

        `entry_time` / `exit_time` are declared non-optional, so this looks
        like dead defensive code -- it is not. `capture_snapshot` calls this
        for every recent trade, and it is the FIRST thing
        `write_session_report` does; one record with an unreadable timestamp
        (rehydrated from an older store, recovered from the broker) raises
        here and takes down the entire EOD report from inside its broad
        try/except, plus the dashboard snapshot that shares this path. Found
        2026-09-21 while testing the session-scope fix, which could not
        protect against it because the raise happens upstream of the filter.
        """
        payload = asdict(trade)
        for field_name in ("entry_time", "exit_time"):
            value = getattr(trade, field_name, None)
            try:
                payload[field_name] = value.isoformat()
            except AttributeError:
                payload[field_name] = None
        return payload

    @staticmethod
    def _equity_point_to_dict(point: EquityPoint) -> dict[str, Any]:
        payload = asdict(point)
        payload["timestamp"] = point.timestamp.isoformat()
        return payload

    def snapshot_copy(self, positions: dict[str, Position]) -> dict[str, Any]:
        return copy.deepcopy(self.snapshot(positions))
