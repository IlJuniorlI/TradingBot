# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .broker_positions import active_broker_bracket
from .config import BotConfig
from .models import ASSET_TYPE_EQUITY, OPTION_ASSET_TYPES, Position, Side, Signal
from ._strategies.registry import is_option_strategy
from .utils import append_management_adjustment, now_et

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class RecentExitRecord:
    """One record per recent exit, used for the same-level retry block.

    ``entry_price`` is the LEVEL the block keys off — the price we already
    tried. It replaced ``exit_price`` on 2026-07-31: measuring from the exit
    made the block structurally unable to catch a repeat. A stopped-out SHORT
    exits roughly 1R ABOVE its entry, so re-entering the SAME level always
    sits ~1R away from that exit and cleared any sane threshold. Observed on
    AAPL 2026-07-30, five shorts inside $0.37 (331.46 / 331.57 / 331.66 /
    331.78 / 331.83): consecutive entries were 0.05-0.12 apart (0.1-0.25 ATR)
    while the exit-referenced distances were 0.50-1.02 — four to ten times
    larger. Only one of the five was ever blocked.

    ``exit_price`` is retained for logging: it is what the operator sees in
    the block message and is useful when reading the decision back.

    Every exit is recorded, not only losses. The old loser-only rule left a
    re-entry after a WIN completely unchecked, which is how that AAPL run
    alternated win/loss/win/loss/loss through one price for three hours.
    Kept in a list (not a dict keyed by symbol) so multiple exits on the same
    symbol within the window all participate in the check.

    ``side`` and ``entry_price`` are ``RiskManager.same_level_anchor``: for
    an option the UNDERLYING's market direction and its price at entry, and
    ``exit_price`` the underlying's price at exit, so every field sits in the
    price space of ``atr`` (the underlying's). Until 2026-09-25 an option was
    recorded by its premium and its order side.
    """
    symbol: str
    side: Side
    entry_price: float
    exit_price: float
    atr: float
    timestamp: datetime


@dataclass
class RiskState:
    """Per-RiskManager mutable state.

    `slots=True` is intentionally OFF here even though the rest of the
    codebase prefers it. PyCharm's static checker has a known bug where
    field access on `@dataclass(slots=True)` instances is mis-reported as
    "object has no attribute '<field>'" for every slot field. RiskState is
    a singleton (one per RiskManager, one per bot run), so the slots
    memory benefit is ~50 bytes on a single instance — not worth the IDE
    noise on every read/write of these fields.
    """
    realized_pnl: float = 0.0
    # Direction-aware cooldown keyed by (SYMBOL_UPPER, Side). A LONG exit
    # only blocks same-direction re-entry; opposite-direction entries remain
    # allowed so the bot can flip short on a genuine bearish reversal.
    # When ``config.risk.cooldown_direction_aware`` is False, writes mirror
    # both directions to preserve the legacy behavior.
    cooldown_until: dict[tuple[str, "Side"], datetime] = field(default_factory=dict)
    # Recent stopouts for the same-level retry block. Pruned lazily inside
    # can_open; cap at ~100 records in pathological scenarios.
    recent_exits: list[RecentExitRecord] = field(default_factory=list)
    # ET session date that owns the current realized_pnl tally. When the date
    # rolls over, realized_pnl is reset to 0.0 so that max_daily_loss behaves
    # as a per-day gate rather than a per-lifetime cap. Eager-initialized to
    # today's ET date so the field has a concrete `date` type and
    # `_reset_if_new_session` doesn't need a lazy-init branch.
    session_date: date = field(default_factory=lambda: now_et().date())


class RiskManager:
    def __init__(self, config: BotConfig, state_store: Any = None):
        self.config = config
        self.state = RiskState()
        self._reentry_policy = self._normalized_reentry_policy()
        # Optional SessionRiskStateStore. When attached, the per-day tallies
        # survive a restart (see attach_state_store).
        self._state_store = None
        if state_store is not None:
            self.attach_state_store(state_store)

    # ------------------------------------------------------------------
    # Cross-restart persistence
    # ------------------------------------------------------------------
    def attach_state_store(self, store: Any) -> None:
        """Attach a ``SessionRiskStateStore`` and restore today's tallies.

        Without this the risk counters are memory-only: a restart reset
        ``realized_pnl`` to 0.0 and dropped every cooldown and same-level
        block, so a bot restarted after most of ``max_daily_loss`` was gone
        came back able to lose the whole limit again. Open positions were
        already recovered from the broker; this recovers the counters that
        decide whether to open more.

        Only a row matching today's ET session date is restored, so the daily
        reset still happens by itself.
        """
        self._state_store = store
        today = now_et().date()
        payload = None
        try:
            payload = store.load(today.isoformat())
        except Exception:
            LOG.warning("Could not restore session risk state; starting the day flat.", exc_info=True)
        if not payload:
            return
        self.state.session_date = today
        self.state.realized_pnl = float(payload.get("realized_pnl", 0.0) or 0.0)
        restored_cooldowns = 0
        for row in payload.get("cooldowns") or []:
            try:
                key = (str(row["symbol"]).upper(), Side(row["side"]))
                self.state.cooldown_until[key] = datetime.fromisoformat(row["until"])
                restored_cooldowns += 1
            except Exception:
                LOG.debug("Skipping unparseable restored cooldown row: %r", row, exc_info=True)
        restored_exits = 0
        for row in payload.get("recent_exits") or []:
            try:
                self.state.recent_exits.append(
                    RecentExitRecord(
                        symbol=str(row["symbol"]).upper(),
                        side=Side(row["side"]),
                        entry_price=float(row["entry_price"]),
                        exit_price=float(row["exit_price"]),
                        atr=float(row["atr"]),
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                    )
                )
                restored_exits += 1
            except Exception:
                LOG.debug("Skipping unparseable restored exit row: %r", row, exc_info=True)
        LOG.info(
            "Restored session risk state for %s: realized_pnl=%.2f cooldowns=%d recent_exits=%d",
            today.isoformat(), self.state.realized_pnl, restored_cooldowns, restored_exits,
        )

    def _persist_state(self) -> None:
        """Write the current tallies. No-op when no store is attached."""
        if self._state_store is None:
            return
        cooldowns = [
            {"symbol": symbol, "side": side.value, "until": until.isoformat()}
            for (symbol, side), until in self.state.cooldown_until.items()
        ]
        recent_exits = [
            {
                "symbol": record.symbol,
                "side": record.side.value,
                "entry_price": record.entry_price,
                "exit_price": record.exit_price,
                "atr": record.atr,
                "timestamp": record.timestamp.isoformat(),
            }
            for record in self.state.recent_exits
        ]
        self._state_store.save(
            self.state.session_date.isoformat(), self.state.realized_pnl, cooldowns, recent_exits,
        )

    @staticmethod
    def floor_discrete_units(budget: float, unit_cost: float) -> int:
        try:
            budget_value = float(budget)
            unit_value = float(unit_cost)
        except Exception:
            return 0
        if budget_value <= 0 or unit_value <= 0:
            return 0
        ratio = budget_value / unit_value
        return max(0, int(math.floor(ratio + 1e-9)))

    @staticmethod
    def _symbol_key(symbol: str | None) -> str:
        return str(symbol or "").upper().strip()

    def _normalized_reentry_policy(self) -> str:
        policy = str(self.config.risk.reentry_policy).strip().lower()
        aliases = {
            "same_day": "rest_of_day",
            "same-day": "rest_of_day",
            "rest-of-day": "rest_of_day",
            "session": "rest_of_day",
            "day": "rest_of_day",
            "none": "immediate",
        }
        policy = aliases.get(policy, policy)
        if policy not in {"cooldown", "immediate", "rest_of_day"}:
            LOG.warning("Unknown risk.reentry_policy=%r; defaulting to 'cooldown'", policy)
            return "cooldown"
        return policy

    def is_symbol_on_cooldown(self, symbol: str, side: "Side | None" = None) -> bool:
        """Check whether ``symbol`` is on cooldown for ``side``.

        With ``cooldown_direction_aware: true`` (the shipped default), the
        write side only stamps the direction that just exited, so reads
        should pass the candidate's intended ``side`` when known to avoid
        blocking the opposite direction. When ``side`` is None — strategies
        whose screener doesn't pre-classify direction (e.g.
        peer_confirmed_key_levels) or test fixtures — this returns True if
        EITHER direction is on cooldown, the conservative fallback.
        """
        key = self._symbol_key(symbol)
        now = now_et()
        if side is not None:
            until = self.state.cooldown_until.get((key, side))
            return bool(until and now < until)
        for (stored_key, _stored_side), until in self.state.cooldown_until.items():
            if stored_key == key and until and now < until:
                return True
        return False

    def _reset_if_new_session(self) -> None:
        """Reset per-day realized P&L when the ET session date rolls over.

        Without this, ``max_daily_loss`` accumulates across days on continuous
        multi-day runs and one losing session blocks all future entries until
        the process is restarted. ``session_date`` is eager-initialized in
        ``RiskState`` to today's ET date, so there is no ``None`` initial
        state to handle here.
        """
        current_date = now_et().date()
        if current_date != self.state.session_date:
            if self.state.realized_pnl != 0.0:
                LOG.info(
                    "Risk session rollover %s -> %s: resetting realized_pnl=%.2f",
                    self.state.session_date,
                    current_date,
                    self.state.realized_pnl,
                )
            self.state.realized_pnl = 0.0
            self.state.session_date = current_date
            self._persist_state()

    def register_realized_pnl(self, pnl: float) -> None:
        """Add *pnl* to today's realized tally and persist it.

        Persisting HERE rather than only in ``register_exit`` matters: a
        PARTIAL exit books P&L through this method directly and never reaches
        ``register_exit`` -- a broker fill of part of an exit order or of a
        bracket child, and a deliberate scale-out (the shared divergence
        partial exit, ``ExitDecision.fraction < 1``). Persisting only on full
        exits would leave those slices' P&L unrecoverable after a restart,
        which is precisely the hole the state store exists to close. (The
        ``adaptive_ladder`` rungs are NOT partials: they move the stop and
        target and never scale out -- this docstring said otherwise until
        2026-09-24.) ``register_exit`` persists again once it has also
        updated the cooldown and same-level log; an extra sqlite write per
        exit is not worth avoiding.
        """
        self._reset_if_new_session()
        self.state.realized_pnl += float(pnl)
        self._persist_state()

    def register_exit(
        self,
        symbol: str,
        pnl: float,
        *,
        additional_symbol: str | None = None,
        side: "Side | None" = None,
        level: "tuple[Side, float] | None" = None,
        exit_price: float | None = None,
        atr: float | None = None,
    ) -> None:
        """Record an exit: update realized_pnl, cooldown, and recent-exit log.

        ``side`` — when provided, drives direction-aware cooldown (the ORDER
        side, which is what ``can_open`` checks). Callers that pass only
        symbol/pnl get both-direction cooldown.
        ``level`` + ``atr`` — when provided, a ``RecentExitRecord`` is
        appended so ``can_open`` can enforce the same-level retry block.
        ``level`` is ``same_level_anchor`` of the closed position: the
        direction and the price already tried (an option's are the
        underlying's). ``exit_price`` is carried for logging only, in the same
        price space. Every exit is recorded, win or loss.
        """
        self.register_realized_pnl(pnl)
        key = self._symbol_key(symbol)
        extra_key = self._symbol_key(additional_symbol) if additional_symbol else None
        policy = self._reentry_policy
        direction_aware = bool(getattr(self.config.risk, "cooldown_direction_aware", True))
        sides_to_write: list[Side]
        if side is None or not direction_aware:
            sides_to_write = [Side.LONG, Side.SHORT]
        else:
            sides_to_write = [side]

        def _apply(cooldown_ts: datetime | None) -> None:
            keys = [key] + ([extra_key] if extra_key and extra_key != key else [])
            for k in keys:
                for s in sides_to_write:
                    if cooldown_ts is None:
                        self.state.cooldown_until.pop((k, s), None)
                    else:
                        self.state.cooldown_until[(k, s)] = cooldown_ts

        if policy == "immediate":
            _apply(None)
        elif policy == "rest_of_day":
            _apply((now_et() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
        else:
            _apply(now_et() + timedelta(minutes=self.config.risk.cooldown_minutes))

        # Record the exit for the same-level retry block. Needs the LEVEL (the
        # direction and entry price being re-tried) and an ATR to scale the
        # tolerance; callers without them pass None and the block skips this
        # exit. Every exit is recorded regardless of P&L — see
        # RecentExitRecord for why the old loser-only rule could not catch a
        # repeat sequence.
        if level is not None and atr is not None:
            level_side, level_price = level
            try:
                record = RecentExitRecord(
                    symbol=key,
                    side=level_side,
                    entry_price=float(level_price),
                    exit_price=float(exit_price) if exit_price is not None else float(level_price),
                    atr=max(1e-6, float(atr)),
                    timestamp=now_et(),
                )
                self.state.recent_exits.append(record)
                # Trim: keep only records within the block window (plus a small
                # margin) to cap memory in pathological sessions.
                window_minutes = max(1, int(getattr(self.config.risk, "same_level_block_minutes", 30)))
                cutoff = now_et() - timedelta(minutes=window_minutes * 2)
                self.state.recent_exits = [r for r in self.state.recent_exits if r.timestamp >= cutoff]
            except Exception:
                LOG.debug("Could not record exit for same-level block", exc_info=True)

        # One write per exit — the only point where realized_pnl, the
        # cooldowns and the same-level log all change together.
        self._persist_state()

    @staticmethod
    def open_risk_to_stops(positions: dict[str, Position]) -> float:
        """Total remaining loss if every open position stopped out right now.

        Measured to each position's CURRENT stop, so a stop trailed to
        breakeven or beyond contributes 0.0 rather than a negative (that is
        locked-in profit, not risk, and netting it off would understate the
        downside).

        A position whose levels cannot be read contributes 0.0 and is LOGGED.
        Skipping it silently would under-count open risk, which pushes the
        daily-loss projection in the permissive direction — the one case where
        a quiet failure lets the bot keep opening trades it should not.
        """
        total = 0.0
        for key, position in (positions or {}).items():
            try:
                entry = float(position.entry_price)
                stop = float(position.stop_price)
                qty = max(0, int(position.qty))
            except (TypeError, ValueError):
                LOG.warning(
                    "Open-risk projection could not read levels for %s "
                    "(entry=%r stop=%r qty=%r); it contributes 0 to the daily-loss "
                    "projection, which UNDER-counts risk.",
                    key, getattr(position, "entry_price", None),
                    getattr(position, "stop_price", None), getattr(position, "qty", None),
                )
                continue
            if qty <= 0:
                continue
            if entry <= 0 or stop <= 0:
                LOG.warning(
                    "Open-risk projection skipping %s: non-positive entry=%.4f or stop=%.4f. "
                    "Its risk is NOT counted against max_daily_loss.",
                    key, entry, stop,
                )
                continue
            per_unit = (entry - stop) if position.side == Side.LONG else (stop - entry)
            total += max(0.0, per_unit) * qty
        return total

    def can_open(self, signal: Signal, positions: dict[str, Position]) -> tuple[bool, str]:
        self._reset_if_new_session()
        # Daily loss gate. Compares the WORST-CASE day against the limit when
        # ``daily_loss_includes_open_risk`` is on: realized P&L less what the
        # currently-open positions would lose if every one of them stopped
        # out. Comparing realized P&L alone let the bot keep opening trades
        # until the realized damage reached the limit, at which point
        # max_positions could still be open at full risk — finishing the day
        # at roughly twice the configured cap.
        limit = abs(self.config.risk.max_daily_loss)
        open_risk = (
            self.open_risk_to_stops(positions)
            if bool(getattr(self.config.risk, "daily_loss_includes_open_risk", True))
            else 0.0
        )
        projected_pnl = self.state.realized_pnl - open_risk
        if projected_pnl <= -limit:
            LOG.warning(
                "Daily loss limit reached: realized_pnl=%.2f open_risk=%.2f projected=%.2f "
                "limit=%.2f — blocking %s %s",
                self.state.realized_pnl, open_risk, projected_pnl, limit,
                signal.symbol, signal.side.value,
            )
            return False, "daily_loss_limit"
        max_positions = self.config.risk.max_positions
        if is_option_strategy(signal.strategy):
            max_positions = min(max_positions, len(self.config.options.underlyings))
        active_slots: set[str] = set()
        for pos in positions.values():
            pair_id = str(pos.pair_id).strip() if pos.pair_id is not None else ""
            if pair_id:
                active_slots.add(f"pair:{pair_id}")
            else:
                active_slots.add(f"symbol:{pos.symbol}")
        signal_pair_id = str(signal.pair_id).strip() if signal.pair_id is not None else ""
        raw_key = signal.metadata.get("position_key") if isinstance(signal.metadata, dict) else None
        key = self._symbol_key(raw_key or signal.symbol)
        signal_slot = f"pair:{signal_pair_id}" if signal_pair_id else f"symbol:{key}"
        if signal_slot not in active_slots and len(active_slots) >= max_positions:
            return False, "max_positions"
        # Correlation concentration guard — block when too many same-direction
        # positions already sit in the same correlated group.
        #
        # Configured per-strategy in params.correlation_groups /
        # params.max_same_correlation_group_same_direction. These are
        # DELIBERATELY separate from params.sector_groups, which exists to
        # route a symbol to its confirmation ETF and must stay at GICS
        # granularity. Risk grouping wants the opposite: on a mega-cap
        # universe the tech, communication and consumer-discretionary names
        # trade as one book (roughly 0.85 correlated on any macro day), so
        # treating them as three independent sectors let one directional bet
        # fill every position slot while appearing diversified.
        strategy_params = {}
        try:
            strategy_params = self.config.strategies.get(signal.strategy, self.config.active_strategy).params or {}
        except Exception:
            # Swallowing this silently disables the concentration guard
            # entirely (max_group falls to 0), so the bot would happily stack
            # correlated positions with nothing to show for it. Log loudly —
            # a risk control must not fail open in silence.
            LOG.warning(
                "Could not read strategy params for %s; the correlation concentration "
                "guard is INACTIVE for this signal.", signal.strategy, exc_info=True,
            )
        max_group = int(strategy_params.get("max_same_correlation_group_same_direction", 0) or 0)
        if max_group > 0:
            correlation_groups = strategy_params.get("correlation_groups") or {}
            signal_symbol = self._symbol_key(signal.symbol)
            signal_group = None
            for group, members in correlation_groups.items():
                if signal_symbol in {self._symbol_key(m) for m in (members or [])}:
                    signal_group = group
                    break
            if signal_group is not None:
                group_members = {self._symbol_key(m) for m in correlation_groups.get(signal_group, [])}
                same_group_same_dir = sum(
                    1 for pos in positions.values()
                    if pos.side == signal.side and self._symbol_key(pos.symbol) in group_members
                )
                if same_group_same_dir >= max_group:
                    LOG.warning(
                        "Correlation concentration limit: %s %s blocked — %d/%d %s positions in group '%s'",
                        signal.symbol, signal.side.value, same_group_same_dir, max_group,
                        signal.side.value, signal_group,
                    )
                    return False, "correlation_concentration"
        if key in positions:
            return False, "already_in_position"
        # Direction-aware cooldown: a LONG exit only blocks a LONG re-entry;
        # the opposite direction remains allowed so the bot can flip on a
        # genuine reversal. When config.risk.cooldown_direction_aware=False,
        # both directions were written at exit time so this still blocks
        # everything (legacy).
        if self.is_symbol_on_cooldown(signal.symbol, signal.side):
            return False, "cooldown"
        if raw_key and raw_key != signal.symbol and self.is_symbol_on_cooldown(raw_key, signal.side):
            return False, "cooldown"
        # Same-level retry block: if a same-direction exit on this symbol is
        # recent AND the current entry candidate is within N*ATR of that
        # exit's entry level, block unless the fib-pullback override applies.
        blocked, block_reason = self._same_level_block_check(signal)
        if blocked:
            return False, block_reason
        if signal.side == Side.SHORT and not self.config.risk.allow_short and not is_option_strategy(signal.strategy):
            return False, "shorts_disabled"
        return True, "ok"

    def _same_level_block_check(self, signal: Signal) -> tuple[bool, str]:
        """Enforce the same-level retry block with a fib-pullback exception.

        Returns ``(blocked, reason)``. Iterates recent exits; a record
        blocks the signal iff:
          - same symbol (or its underlying key)
          - same direction (``same_level_anchor``: an option's market
            direction, never its order side)
          - within ``same_level_block_minutes`` of the prior exit
          - |signal level - prior level| <= same_level_block_atr_mult * atr,
            the level being the entry price (an option's underlying's)

        If all four hold, the fib-pullback check runs: if the signal's entry
        price is inside the [0.5, 0.786] retracement band of the swing
        captured in signal.metadata (tech_fib_anchor_low/high), the block
        is overridden — the entry is a proper pullback, not a breakout
        chase.
        """
        window_minutes = max(0, int(getattr(self.config.risk, "same_level_block_minutes", 30)))
        atr_mult = max(0.0, float(getattr(self.config.risk, "same_level_block_atr_mult", 0.3)))
        if window_minutes <= 0 or atr_mult <= 0 or not self.state.recent_exits:
            return False, "ok"
        key = self._symbol_key(signal.symbol)
        meta = signal.metadata if isinstance(signal.metadata, dict) else {}
        raw_key = meta.get("position_key") if isinstance(meta, dict) else None
        raw_key_norm = self._symbol_key(raw_key) if raw_key else None
        anchor = self.same_level_anchor(signal.strategy, signal.side, signal.metadata,
                                        self._signal_entry_price(signal))
        if anchor is None:
            return False, "ok"
        signal_side, signal_entry = anchor
        cutoff = now_et() - timedelta(minutes=window_minutes)
        for record in reversed(self.state.recent_exits):
            if record.timestamp < cutoff:
                continue
            if record.side != signal_side:
                continue
            if record.symbol != key and record.symbol != (raw_key_norm or key):
                continue
            threshold = atr_mult * record.atr
            if threshold <= 0:
                continue
            # Distance to the LEVEL WE ALREADY TRIED, not to where that
            # attempt was closed out. See RecentExitRecord.
            if abs(signal_entry - record.entry_price) > threshold:
                continue
            # Fib-pullback override: allow the entry if it sits inside the
            # [0.5, 0.786] retracement band of the most-recent swing stored
            # in signal metadata. A LONG retrace back to the 0.5-0.786 zone
            # of a bullish impulse is a proper pullback entry, not a chase.
            if self._fib_pullback_override(signal, signal_entry):
                LOG.info(
                    "Same-level block overridden by fib-pullback for %s %s entry=%.4f "
                    "prior_entry=%.4f prior_exit=%.4f atr=%.4f",
                    signal.symbol, signal.side.value, signal_entry,
                    record.entry_price, record.exit_price, record.atr,
                )
                return False, "ok"
            return True, "same_level_retry_block"
        return False, "ok"

    @staticmethod
    def same_level_anchor(strategy: str, side: Side, metadata: Any, price: Any) -> tuple[Side, float] | None:
        """The (direction, price level) the same-level retry block keys on,
        read the same way from a signal and from the position it became.

        An equity: its side and ``price`` (the signal's intended entry, the
        position's fill). An option: the UNDERLYING's market direction
        (``metadata['direction']``, bullish* / bearish*) and the underlying's
        price at entry (``metadata['underlying_entry']``, stamped by every
        option signal builder and carried onto the position). Its premium is
        not a level -- the ATR the block scales by is the underlying's -- and
        its side is the ORDER side: a bear put debit is bought and a bull put
        credit sold, so matching on it blocked flips and missed the same bet
        through a different structure (fixed 2026-09-25). None when the level
        cannot be read; the block then skips.
        """
        if is_option_strategy(strategy):
            meta = metadata if isinstance(metadata, dict) else {}
            market = str(meta.get("direction") or "").strip().lower()
            if market.startswith("bullish"):
                side = Side.LONG
            elif market.startswith("bearish"):
                side = Side.SHORT
            else:
                return None
            price = meta.get("underlying_entry")
        try:
            value = float(price)
        except (TypeError, ValueError):
            return None
        return (side, value) if math.isfinite(value) and value > 0 else None

    @staticmethod
    def _signal_entry_price(signal: Signal) -> float | None:
        meta = signal.metadata if isinstance(signal.metadata, dict) else {}
        for key in ("entry_price", "limit_price", "mark_price_hint"):
            raw = meta.get(key) if isinstance(meta, dict) else None
            try:
                if raw is None:
                    continue
                value = float(raw)
            except (TypeError, ValueError):
                continue
            # math.isfinite rejects NaN and ±inf in one call. The previous
            # idiom `0 < value == value` (chained comparison) relied on
            # `NaN == NaN` being False to filter NaN, but it tripped
            # PyCharm's "Comparison with self" warning and accepted +inf.
            # isfinite reads cleanly and is also stricter — +inf is never
            # a valid entry price either.
            if math.isfinite(value) and value > 0:
                return value
        return None

    @staticmethod
    def _fib_pullback_override(signal: Signal, entry_price: float) -> bool:
        """True iff ``entry_price`` is inside the [0.5, 0.786] retracement
        band of the swing anchored by
        ``signal.metadata['tech_fib_anchor_low']`` and
        ``['tech_fib_anchor_high']``. Direction-aware: for a LONG entry we
        expect a bullish swing (low→high) with pullback DOWN; for SHORT we
        expect a bearish swing (high→low) with pullback UP.

        Keys are ``tech_``-prefixed because the shared entry stage stamps the
        ``_technical_lists(ctx, prefix="tech")`` of the frame a proposal was
        gated on onto every signal it emits.

        Never for an option signal. Its side is the ORDER side (a bull put
        credit spread is sold), so the direction check against the swing
        means nothing for it and could lift a legitimate block. The caller
        passes the underlying's entry (``same_level_anchor``, 2026-09-25), the
        same price space as the anchors. Option signals started carrying the
        anchors when their entries moved onto the shared stage (2026-09-24).
        """
        if is_option_strategy(signal.strategy):
            return False
        meta = signal.metadata if isinstance(signal.metadata, dict) else {}
        try:
            anchor_low = float(meta.get("tech_fib_anchor_low"))
            anchor_high = float(meta.get("tech_fib_anchor_high"))
        except (TypeError, ValueError):
            return False
        # Chained: anchor_low > 0 and anchor_high > anchor_low implies anchor_high > 0.
        if not 0 < anchor_low < anchor_high:
            return False
        swing = anchor_high - anchor_low
        direction = str(meta.get("tech_fib_direction") or "").strip().lower()
        if signal.side == Side.LONG:
            if direction and direction != "bullish":
                return False
            # Pullback zone for LONG entry on bullish swing: below 0.5 ret,
            # above 0.786 ret. i.e. entry between (high - 0.786*swing) and
            # (high - 0.5*swing).
            lower = anchor_high - 0.786 * swing
            upper = anchor_high - 0.5 * swing
        else:
            if direction and direction != "bearish":
                return False
            # Pullback zone for SHORT on bearish swing: above 0.5 ret, below
            # 0.786 ret. i.e. between (low + 0.5*swing) and (low + 0.786*swing).
            lower = anchor_low + 0.5 * swing
            upper = anchor_low + 0.786 * swing
        if lower > upper:
            lower, upper = upper, lower
        # Generous tolerance — within 5% of swing on either side still counts
        # as a pullback. Tighter bands produce too many false blocks on small
        # intraday swings where tick-level precision doesn't matter.
        tolerance = max(0.01, 0.05 * swing)
        return (lower - tolerance) <= entry_price <= (upper + tolerance)

    @staticmethod
    def position_notional(position: Position) -> float:
        return max(0.0, abs(float(position.entry_price)) * abs(int(position.qty)))

    def current_stock_notional(self, positions: dict[str, Position]) -> float:
        total = 0.0
        for position in positions.values():
            asset_type = str(position.metadata.get("asset_type") or "")
            if is_option_strategy(position.strategy) or asset_type.startswith("OPTION"):
                continue
            total += self.position_notional(position)
        return total

    def remaining_stock_notional_capacity(self, positions: dict[str, Position]) -> float:
        return max(0.0, float(self.config.risk.max_total_notional) - self.current_stock_notional(positions))

    def can_add_stock_notional(self, positions: dict[str, Position], proposed_notional: float) -> tuple[bool, str]:
        if proposed_notional <= 0:
            return False, "invalid_notional"
        if self.current_stock_notional(positions) + proposed_notional > float(self.config.risk.max_total_notional):
            return False, "max_total_notional"
        return True, "ok"

    def entry_risk_budget(self) -> float:
        """Dollar risk allowed on a single trade.

        ``max_notional_per_trade * risk_per_trade_frac_of_notional`` — a
        fraction of the per-trade notional cap, NOT of account equity. See the
        RiskConfig docstring in config.py for the full convention.
        """
        return float(self.config.risk.max_notional_per_trade) * float(
            self.config.risk.risk_per_trade_frac_of_notional
        )

    def entry_slippage_allowance(self, bid: float | None, ask: float | None, reference_price: float) -> float:
        """Expected adverse slippage on an entry, in price units.

        Sizing happens against the previewed limit price, but realized risk is
        ``qty * |fill - stop|``. Padding the stop distance by this amount makes
        the size conservative enough that a fill slipping by up to this much
        still lands inside ``entry_risk_budget``.

        Derived from the live spread, which is the actual cost of crossing,
        and capped at ``entry_slippage_allowance_max_pct`` of price so a single
        pathological quote cannot shrink a position to nothing. Returns 0.0
        when the spread is unusable or the feature is switched off — which
        restores the previous "size on the raw stop distance" behaviour.
        """
        spread_frac = float(self.config.risk.entry_slippage_allowance_spread_frac)
        if spread_frac <= 0.0 or reference_price <= 0:
            return 0.0
        try:
            spread = float(ask) - float(bid)
        except (TypeError, ValueError):
            return 0.0
        if not (spread > 0.0):
            return 0.0
        cap = float(self.config.risk.entry_slippage_allowance_max_pct) * float(reference_price)
        allowance = spread * spread_frac
        return max(0.0, min(allowance, cap) if cap > 0 else allowance)

    def size_position(self, entry_price: float, stop_price: float, *, slippage_allowance: float = 0.0) -> int:
        """Share count for an entry, sized so the trade risks at most
        ``entry_risk_budget`` even if the fill slips by ``slippage_allowance``.

        ``slippage_allowance`` widens the stop distance used for sizing only —
        it never moves the actual stop, which stays where the strategy put it.
        Default 0.0 keeps the original behaviour for callers that do not have
        a spread to work from.
        """
        if entry_price <= 0 or stop_price <= 0:
            return 0
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return 0
        sizing_distance = stop_distance + max(0.0, float(slippage_allowance))
        qty = self.floor_discrete_units(self.entry_risk_budget(), sizing_distance)
        max_qty_by_notional = self.floor_discrete_units(self.config.risk.max_notional_per_trade, entry_price)
        return max(0, min(qty, max_qty_by_notional))

    def realized_entry_risk(self, qty: int, fill_price: float, stop_price: float) -> dict[str, float]:
        """Reconcile the risk actually taken against the budget.

        Returns ``{"risk", "budget", "overage_frac"}`` where ``overage_frac``
        is the fraction ABOVE budget (0.0 when at or under it). Detection
        only: by the time this runs the shares are bought, so there is nothing
        to reject — ``size_position``'s slippage allowance is the preventive
        half of the pair.
        """
        try:
            risk = abs(float(fill_price) - float(stop_price)) * max(0, int(qty))
        except (TypeError, ValueError):
            return {"risk": 0.0, "budget": 0.0, "overage_frac": 0.0}
        budget = self.entry_risk_budget()
        overage = (risk / budget - 1.0) if budget > 0 else 0.0
        return {"risk": risk, "budget": budget, "overage_frac": max(0.0, overage)}

    def realized_option_risk(self, qty: int, max_loss_per_contract: float) -> dict[str, float]:
        """Reconcile an option entry against ``options.max_loss_per_trade``.

        The equity counterpart is ``realized_entry_risk``; options had none, so
        a fill worse than the previewed limit went unnoticed. It matters most
        in dry runs, where ``submit_option_vertical`` deliberately walks the
        price toward the natural to model a chase — sweeping the shipped gates
        produced a worst case of 20 contracts booked at $25 of max loss each
        that actually risked $38.79 each, $776 against a $500 budget.

        Detection only: by the time this runs the contracts are filled.
        """
        try:
            risk = max(0.0, float(max_loss_per_contract)) * max(0, int(qty))
        except (TypeError, ValueError):
            return {"risk": 0.0, "budget": 0.0, "overage_frac": 0.0}
        budget = float(self.config.options.max_loss_per_trade)
        overage = (risk / budget - 1.0) if budget > 0 else 0.0
        return {"risk": risk, "budget": budget, "overage_frac": max(0.0, overage)}

    def size_option_position(self, max_loss_per_contract: float) -> int:
        if max_loss_per_contract <= 0:
            return 0
        qty = self.floor_discrete_units(self.config.options.max_loss_per_trade, max_loss_per_contract)
        return max(0, min(qty, self.config.options.max_contracts_per_trade))

    @staticmethod
    def _peak_and_current_r(position: Position, last_price: float, initial_risk: float) -> tuple[float, float]:
        """Return (peak_r, current_r) where R = initial_risk per unit.

        For LONG, peak uses ``highest_price``; for SHORT, ``lowest_price``.
        Both are populated by ``position.update_extremes(last_price)`` on
        every cycle, so the peak is always at least last_price's direction.
        """
        if initial_risk <= 0:
            return 0.0, 0.0
        entry = float(position.entry_price)
        if position.side == Side.LONG:
            peak = float(position.highest_price) if position.highest_price is not None else float(last_price)
            peak_r = (peak - entry) / initial_risk
            current_r = (float(last_price) - entry) / initial_risk
        else:
            trough = float(position.lowest_price) if position.lowest_price is not None else float(last_price)
            peak_r = (entry - trough) / initial_risk  # favorable for SHORT
            current_r = (entry - float(last_price)) / initial_risk
        return round(peak_r, 9), round(current_r, 9)

    def _peak_giveback_floor_r(self, peak_r: float) -> float | None:
        """Tiered give-back floor (retain fraction grows with peak size).

        Returns the current_r level at which the trade should exit, given
        the peak reached since entry. None when peak is below the minimum
        threshold — no floor in that case (the low-tier / BE arm handles <1R).

        The retain fractions are configurable (``peak_giveback_retain_*``).
        Tightened 2026-05-27 from the original 0.50/0.60/0.70 after the
        5/12-5/27 sample showed winners captured only 44% of their MFE —
        and did NOT make new highs after their interim peak in-sample, so
        the loose floors were donating realized gains back to the market.
        Higher retain = capture more / exit sooner on a retrace; lower =
        more room to recover (risks clipping recoveries between the old
        and new floor). Tune down if runners get clipped on normal
        pullbacks.
        """
        if peak_r < 1.0:
            return None
        risk_cfg = self.config.risk
        if peak_r < 2.0:
            return peak_r * float(getattr(risk_cfg, "peak_giveback_retain_1to2r", 0.65))
        if peak_r < 3.0:
            return peak_r * float(getattr(risk_cfg, "peak_giveback_retain_2to3r", 0.72))
        return peak_r * float(getattr(risk_cfg, "peak_giveback_retain_3r_plus", 0.78))

    def _peak_giveback_triggered(self, position: Position, last_price: float, initial_risk: float) -> bool:
        # Per-position override (Tier 3b — high-conviction-day loosening).
        # Strategies that detect a strong directional day at entry (e.g.
        # top_tier_adaptive when |day_strength| ≥ threshold) stamp
        # ``peak_giveback_min_r_override`` on the signal metadata, which
        # carries through to position.metadata. The override raises the
        # threshold (default 2.0R vs the global default 1.0R) so 2R+
        # winners on trend days aren't cut by normal 50% retracements.
        # Falls back to ``config.risk.peak_giveback_min_r`` when not set.
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        default_min_r = float(getattr(self.config.risk, "peak_giveback_min_r", 1.0) or 1.0)
        override = meta.get("peak_giveback_min_r_override")
        override_active = False
        if override is not None:
            try:
                min_r = float(override)
                override_active = True
            except (TypeError, ValueError):
                LOG.warning(
                    "peak_giveback_min_r_override on %s (id=%s) is malformed (%r); falling back to default %.2fR",
                    position.symbol, getattr(position, "id", "?"), override, default_min_r,
                )
                min_r = default_min_r
        else:
            min_r = default_min_r
        peak_r, current_r = self._peak_and_current_r(position, last_price, initial_risk)

        # Low-tier check (2026-05-26). For trades that peaked between
        # ``peak_giveback_low_tier_min_r`` and ``min_r`` (the main gate),
        # arm a tighter give-back floor so 0.7-1R MFE trades don't
        # round-trip back to BE / fixed stop with no exit. Skipped when
        # the high-conviction override is active — those positions want
        # the wider main-tier behavior. The floor uses a fixed giveback
        # fraction (not the main tier's peak-size-dependent ladder)
        # because at sub-1R peaks the run-vs-noise signal is weaker and
        # a constant pct is simpler to reason about than tiers within
        # tiers.
        low_tier_enabled = bool(getattr(self.config.risk, "peak_giveback_low_tier_enabled", True))
        low_tier_min_r = float(getattr(self.config.risk, "peak_giveback_low_tier_min_r", 0.7) or 0.0)
        low_tier_frac = float(getattr(self.config.risk, "peak_giveback_low_tier_giveback_frac", 0.7))
        if (
            low_tier_enabled
            and not override_active
            and 0.0 < low_tier_min_r <= peak_r < min_r
        ):
            low_tier_floor = peak_r * (1.0 - low_tier_frac)
            if current_r <= low_tier_floor:
                if isinstance(meta, dict):
                    meta["_peak_giveback_min_r_used"] = round(float(low_tier_min_r), 4)
                    meta["_peak_giveback_override_active"] = False
                    meta["_peak_giveback_low_tier_active"] = True
                return True

        if peak_r < min_r:
            return False
        floor_r = self._peak_giveback_floor_r(peak_r)
        if floor_r is None:
            return False
        triggered = current_r <= floor_r
        if triggered and isinstance(meta, dict):
            # Stamp which threshold actually tripped so update_position can
            # emit a differentiated exit reason (default vs high-conviction
            # vs low-tier).
            meta["_peak_giveback_min_r_used"] = round(float(min_r), 4)
            meta["_peak_giveback_override_active"] = bool(override_active)
            meta["_peak_giveback_low_tier_active"] = False
        return triggered

    def update_position(self, position: Position, last_price: float) -> tuple[bool, str]:
        meta = position.metadata if isinstance(position.metadata, dict) else {}
        if isinstance(meta, dict):
            meta.setdefault("management_adjustments", [])
        position.update_extremes(last_price)

        initial_stop = meta.get("initial_stop_price", position.stop_price)
        try:
            initial_stop = float(initial_stop)
        except Exception:
            initial_stop = float(position.stop_price)
        initial_risk = max(0.0, abs(float(position.entry_price) - initial_stop))
        trail_activation_mult = 0.5
        asset_type = str(meta.get("asset_type") or ASSET_TYPE_EQUITY).upper()
        options_position = asset_type in OPTION_ASSET_TYPES

        # Peak-giveback floor — fires *before* normal stop/target/trail logic
        # so a winner that reaches +NR and retraces past the tiered floor
        # gets closed out even if the trail hasn't armed yet. Equity-only
        # (options have their own ratchet via options_breakeven + profit_lock).
        peak_giveback_exit = (
            (not options_position)
            and bool(getattr(self.config.risk, "peak_giveback_enabled", True))
            and initial_risk > 0
            and self._peak_giveback_triggered(position, last_price, initial_risk)
        )
        if peak_giveback_exit:
            peak_r, current_r = self._peak_and_current_r(position, last_price, initial_risk)
            # ``_peak_giveback_triggered`` stamped these markers right before
            # returning True; read them so we can emit a differentiated exit
            # reason across the three tiers (low / default / high-conviction).
            min_r_used = float(meta.get("_peak_giveback_min_r_used") or 0.0) if isinstance(meta, dict) else 0.0
            override_active = bool(meta.get("_peak_giveback_override_active")) if isinstance(meta, dict) else False
            low_tier_active = bool(meta.get("_peak_giveback_low_tier_active")) if isinstance(meta, dict) else False
            # Floor for the exit reason string: low-tier uses fixed
            # giveback_frac × peak; main tier uses the peak-size ladder.
            if low_tier_active:
                low_tier_frac = float(getattr(self.config.risk, "peak_giveback_low_tier_giveback_frac", 0.7))
                floor_r = peak_r * (1.0 - low_tier_frac)
            else:
                floor_r = self._peak_giveback_floor_r(peak_r)
            if isinstance(meta, dict):
                meta["peak_giveback_fired"] = True
                meta["peak_giveback_peak_r"] = round(float(peak_r), 4)
                meta["peak_giveback_floor_r"] = None if floor_r is None else round(float(floor_r), 4)
                meta["peak_giveback_current_r"] = round(float(current_r), 4)
                meta["peak_giveback_min_r_used"] = round(min_r_used, 4)
                meta["peak_giveback_override_active"] = override_active
                meta["peak_giveback_low_tier_active"] = low_tier_active
                # Drop the internal sentinels (we promoted them above).
                meta.pop("_peak_giveback_min_r_used", None)
                meta.pop("_peak_giveback_override_active", None)
                meta.pop("_peak_giveback_low_tier_active", None)
            if low_tier_active:
                reason_tag = "peak_giveback_low_tier"
            elif override_active:
                reason_tag = "peak_giveback_high_conviction"
            else:
                reason_tag = "peak_giveback"
            return True, (
                f"{reason_tag}:peak{peak_r:.2f}R_floor{(floor_r or 0.0):.2f}R"
                f"_minR{min_r_used:.2f}"
            )

        # --- Options premium ratchet (breakeven + profit lock) ---
        # Options bypass equity adaptive management, but this simpler premium-
        # based ratchet prevents giving back all gains on a winning trade.
        if options_position:
            opt_cfg = getattr(self.config, "options", None)
            opt_entry = max(0.01, float(position.entry_price))
            if position.side == Side.LONG:
                opt_peak = float(position.highest_price) if position.highest_price is not None else float(last_price)
                if opt_cfg is not None and getattr(opt_cfg, "options_breakeven_enabled", False):
                    be_thresh = opt_entry * float(getattr(opt_cfg, "options_breakeven_mark_mult", 1.25))
                    be_stop = opt_entry * float(getattr(opt_cfg, "options_breakeven_stop_mult", 1.05))
                    if opt_peak >= be_thresh and be_stop > float(position.stop_price):
                        prior = float(position.stop_price)
                        position.stop_price = float(be_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "options_ratchet", "kind": "stop", "reason": "breakeven", "from": prior, "to": float(be_stop)})
                            meta["options_breakeven_armed"] = True
                if opt_cfg is not None and getattr(opt_cfg, "options_profit_lock_enabled", False):
                    pl_thresh = opt_entry * float(getattr(opt_cfg, "options_profit_lock_mark_mult", 1.40))
                    pl_stop = opt_entry * float(getattr(opt_cfg, "options_profit_lock_stop_mult", 1.15))
                    if opt_peak >= pl_thresh and pl_stop > float(position.stop_price):
                        prior = float(position.stop_price)
                        position.stop_price = float(pl_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "options_ratchet", "kind": "stop", "reason": "profit_lock", "from": prior, "to": float(pl_stop)})
                            meta["options_profit_lock_armed"] = True
            else:
                # SHORT (credit spreads): mark goes DOWN for profit.
                opt_trough = float(position.lowest_price) if position.lowest_price is not None else float(last_price)
                if opt_cfg is not None and getattr(opt_cfg, "options_breakeven_enabled", False):
                    be_thresh = opt_entry * (2.0 - float(getattr(opt_cfg, "options_breakeven_mark_mult", 1.25)))
                    be_stop = opt_entry * (2.0 - float(getattr(opt_cfg, "options_breakeven_stop_mult", 1.05)))
                    if opt_trough <= be_thresh and be_stop < float(position.stop_price):
                        prior = float(position.stop_price)
                        position.stop_price = float(be_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "options_ratchet", "kind": "stop", "reason": "breakeven", "from": prior, "to": float(be_stop)})
                            meta["options_breakeven_armed"] = True
                if opt_cfg is not None and getattr(opt_cfg, "options_profit_lock_enabled", False):
                    pl_thresh = opt_entry * (2.0 - float(getattr(opt_cfg, "options_profit_lock_mark_mult", 1.40)))
                    pl_stop = opt_entry * (2.0 - float(getattr(opt_cfg, "options_profit_lock_stop_mult", 1.15)))
                    if opt_trough <= pl_thresh and pl_stop < float(position.stop_price):
                        prior = float(position.stop_price)
                        position.stop_price = float(pl_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "options_ratchet", "kind": "stop", "reason": "profit_lock", "from": prior, "to": float(pl_stop)})
                            meta["options_profit_lock_armed"] = True

        trade_management_mode = self.config.risk.trade_management_mode
        ladder_management_enabled = (not options_position) and trade_management_mode == "adaptive_ladder" and bool(meta.get("ladder_management_enabled", False))
        adaptive_enabled = (not options_position) and trade_management_mode in {"adaptive", "adaptive_ladder"} and bool(meta.get("adaptive_management_enabled", False)) and initial_risk > 0
        adaptive_runner_extension_enabled = adaptive_enabled and not ladder_management_enabled
        trailing_enabled = (not options_position) and (trade_management_mode == "adaptive" or (trade_management_mode == "adaptive_ladder" and not ladder_management_enabled))
        suppress_target_exit = (not options_position) and trade_management_mode == "adaptive_ladder" and bool(meta.get("adaptive_ladder_suppress_target_exit", False))

        # Broker-side bracket: whichever legs are actually RESTING at the broker
        # are owned by the broker, and the engine must not also fire them --
        # both fills would land and take the strategy net short. Keyed on the
        # resting child ids rather than the config, so a bracket that failed to
        # establish (state "unprotected") correctly falls back to engine exits.
        # Engine-only exits above/below this (peak giveback, trailing, time stop)
        # are unaffected; position_manager cancels the bracket before those.
        bracket = active_broker_bracket(position)
        broker_owns_stop = bracket is not None and bracket.get("stop_order_id") is not None
        broker_owns_target = bracket is not None and bracket.get("target_order_id") is not None

        def _meta_float(key: str, default: float | None = None) -> float | None:
            value = meta.get(key, default)
            try:
                if value is None:
                    return default
                return float(value)
            except Exception:
                return default

        if position.side == Side.LONG:
            if adaptive_enabled:
                # Round to 9dp to absorb IEEE 754 rounding errors that cause
                # exact-threshold hits (e.g. 0.9R, 1.15R) to fail >= checks.
                max_favorable_r = round(((float(position.highest_price) if position.highest_price is not None else float(last_price)) - float(position.entry_price)) / initial_risk, 9)
                # Partial-breakeven tier (fires first, at lowest RR). 2026-04-23
                # trades that peaked 0.5–0.8R (AVGO, RBLX 10:00, COST 09:51) had
                # nothing between the trail and the 1.0R breakeven — COST 09:51
                # gave back $32 despite a 0.56R peak. Arms a cheap early stop
                # move at a lower RR gate than the main breakeven.
                partial_breakeven_rr = _meta_float("adaptive_partial_breakeven_rr", None)
                partial_breakeven_offset_r = _meta_float("adaptive_partial_breakeven_offset_r", 0.0) or 0.0
                if partial_breakeven_rr is not None and max_favorable_r >= partial_breakeven_rr:
                    candidate_stop = float(position.entry_price) + (float(partial_breakeven_offset_r) * initial_risk)
                    if candidate_stop > float(position.stop_price):
                        prior_stop = float(position.stop_price)
                        position.stop_price = float(candidate_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "adaptive", "kind": "stop", "reason": "partial_breakeven", "from": prior_stop, "to": float(candidate_stop)})
                    meta["adaptive_partial_breakeven_armed"] = True
                breakeven_rr = _meta_float("adaptive_breakeven_rr", None)
                breakeven_offset_r = _meta_float("adaptive_breakeven_offset_r", 0.0) or 0.0
                if breakeven_rr is not None and max_favorable_r >= breakeven_rr:
                    candidate_stop = float(position.entry_price) + (float(breakeven_offset_r) * initial_risk)
                    if candidate_stop > float(position.stop_price):
                        prior_stop = float(position.stop_price)
                        position.stop_price = float(candidate_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "adaptive", "kind": "stop", "reason": "breakeven", "from": prior_stop, "to": float(candidate_stop)})
                    meta["adaptive_breakeven_armed"] = True
                profit_lock_rr = _meta_float("adaptive_profit_lock_rr", None)
                profit_lock_stop_rr = _meta_float("adaptive_profit_lock_stop_rr", None)
                if profit_lock_rr is not None and profit_lock_stop_rr is not None and max_favorable_r >= profit_lock_rr:
                    candidate_stop = float(position.entry_price) + (float(profit_lock_stop_rr) * initial_risk)
                    if candidate_stop > float(position.stop_price):
                        prior_stop = float(position.stop_price)
                        position.stop_price = float(candidate_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "adaptive", "kind": "stop", "reason": "profit_lock", "from": prior_stop, "to": float(candidate_stop)})
                    meta["adaptive_profit_lock_armed"] = True
                runner_enabled = adaptive_runner_extension_enabled and bool(meta.get("adaptive_runner_extend_enabled", False))
                runner_trigger_rr = _meta_float("adaptive_runner_trigger_rr", None)
                runner_target_rr = _meta_float("adaptive_runner_target_rr", None)
                if runner_enabled and runner_trigger_rr is not None and runner_target_rr is not None and max_favorable_r >= runner_trigger_rr and not bool(meta.get("adaptive_target_extended", False)):
                    candidate_target = float(position.entry_price) + (float(runner_target_rr) * initial_risk)
                    current_target = _meta_float("initial_target_price", None)
                    existing_target = float(position.target_price) if position.target_price is not None else None
                    if existing_target is None or candidate_target > float(existing_target) + 1e-6:
                        prior_target = float(existing_target) if existing_target is not None else None
                        position.target_price = float(candidate_target)
                        meta["adaptive_target_extended"] = True
                        meta["adaptive_target_price"] = float(candidate_target)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "adaptive", "kind": "target", "reason": "runner_extension", "from": prior_target, "to": float(candidate_target)})
                    if current_target is not None:
                        meta["adaptive_target_extension_rr"] = float((candidate_target - current_target) / initial_risk)
                    runner_trail_pct = _meta_float("adaptive_runner_trail_pct", None)
                    if runner_trail_pct is not None and runner_trail_pct > 0:
                        prior_trail = float(position.trail_pct) if position.trail_pct else None
                        position.trail_pct = float(runner_trail_pct)
                        if isinstance(meta, dict) and prior_trail != float(runner_trail_pct):
                            append_management_adjustment(meta,{"manager": "adaptive", "kind": "trail_pct", "reason": "runner_extension", "from": prior_trail, "to": float(runner_trail_pct)})
            if trailing_enabled and position.trail_pct and position.highest_price:
                activation_price = float(position.entry_price)
                if initial_risk > 0:
                    activation_price = float(position.entry_price) + (initial_risk * trail_activation_mult)
                trail_armed = float(position.highest_price) >= activation_price
                meta["trail_armed"] = bool(trail_armed)
                meta["trail_activation_price"] = float(activation_price)
                if trail_armed:
                    prior_stop = float(position.stop_price)
                    candidate_stop = max(position.stop_price, position.highest_price * (1.0 - position.trail_pct))
                    position.stop_price = candidate_stop
                    if isinstance(meta, dict) and candidate_stop > prior_stop + 1e-12:
                        append_management_adjustment(meta,{"manager": "adaptive", "kind": "stop", "reason": "trail", "from": prior_stop, "to": float(candidate_stop)})
            if last_price <= position.stop_price and not broker_owns_stop:
                return True, "stop"
            if position.target_price is not None and last_price >= position.target_price and not suppress_target_exit and not broker_owns_target:
                return True, "target"
        else:
            if adaptive_enabled:
                max_favorable_r = round((float(position.entry_price) - (float(position.lowest_price) if position.lowest_price is not None else float(last_price))) / initial_risk, 9)
                # Mirror of the LONG partial_breakeven tier above — see comment
                # at LONG branch for motivation.
                partial_breakeven_rr = _meta_float("adaptive_partial_breakeven_rr", None)
                partial_breakeven_offset_r = _meta_float("adaptive_partial_breakeven_offset_r", 0.0) or 0.0
                if partial_breakeven_rr is not None and max_favorable_r >= partial_breakeven_rr:
                    candidate_stop = float(position.entry_price) - (float(partial_breakeven_offset_r) * initial_risk)
                    if candidate_stop < float(position.stop_price):
                        prior_stop = float(position.stop_price)
                        position.stop_price = float(candidate_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "adaptive", "kind": "stop", "reason": "partial_breakeven", "from": prior_stop, "to": float(candidate_stop)})
                    meta["adaptive_partial_breakeven_armed"] = True
                breakeven_rr = _meta_float("adaptive_breakeven_rr", None)
                breakeven_offset_r = _meta_float("adaptive_breakeven_offset_r", 0.0) or 0.0
                if breakeven_rr is not None and max_favorable_r >= breakeven_rr:
                    candidate_stop = float(position.entry_price) - (float(breakeven_offset_r) * initial_risk)
                    if candidate_stop < float(position.stop_price):
                        prior_stop = float(position.stop_price)
                        position.stop_price = float(candidate_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "adaptive", "kind": "stop", "reason": "breakeven", "from": prior_stop, "to": float(candidate_stop)})
                    meta["adaptive_breakeven_armed"] = True
                profit_lock_rr = _meta_float("adaptive_profit_lock_rr", None)
                profit_lock_stop_rr = _meta_float("adaptive_profit_lock_stop_rr", None)
                if profit_lock_rr is not None and profit_lock_stop_rr is not None and max_favorable_r >= profit_lock_rr:
                    candidate_stop = float(position.entry_price) - (float(profit_lock_stop_rr) * initial_risk)
                    if candidate_stop < float(position.stop_price):
                        prior_stop = float(position.stop_price)
                        position.stop_price = float(candidate_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "adaptive", "kind": "stop", "reason": "profit_lock", "from": prior_stop, "to": float(candidate_stop)})
                    meta["adaptive_profit_lock_armed"] = True
                runner_enabled = adaptive_runner_extension_enabled and bool(meta.get("adaptive_runner_extend_enabled", False))
                runner_trigger_rr = _meta_float("adaptive_runner_trigger_rr", None)
                runner_target_rr = _meta_float("adaptive_runner_target_rr", None)
                if runner_enabled and runner_trigger_rr is not None and runner_target_rr is not None and max_favorable_r >= runner_trigger_rr and not bool(meta.get("adaptive_target_extended", False)):
                    candidate_target = float(position.entry_price) - (float(runner_target_rr) * initial_risk)
                    current_target = _meta_float("initial_target_price", None)
                    existing_target = float(position.target_price) if position.target_price is not None else None
                    if existing_target is None or candidate_target < float(existing_target) - 1e-6:
                        prior_target = float(existing_target) if existing_target is not None else None
                        position.target_price = float(candidate_target)
                        meta["adaptive_target_extended"] = True
                        meta["adaptive_target_price"] = float(candidate_target)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "adaptive", "kind": "target", "reason": "runner_extension", "from": prior_target, "to": float(candidate_target)})
                    if current_target is not None:
                        meta["adaptive_target_extension_rr"] = float((current_target - candidate_target) / initial_risk)
                    runner_trail_pct = _meta_float("adaptive_runner_trail_pct", None)
                    if runner_trail_pct is not None and runner_trail_pct > 0:
                        prior_trail = float(position.trail_pct) if position.trail_pct else None
                        position.trail_pct = float(runner_trail_pct)
                        if isinstance(meta, dict) and prior_trail != float(runner_trail_pct):
                            append_management_adjustment(meta,{"manager": "adaptive", "kind": "trail_pct", "reason": "runner_extension", "from": prior_trail, "to": float(runner_trail_pct)})
            if trailing_enabled and position.trail_pct and position.lowest_price:
                activation_price = float(position.entry_price)
                if initial_risk > 0:
                    activation_price = float(position.entry_price) - (initial_risk * trail_activation_mult)
                trail_armed = float(position.lowest_price) <= activation_price
                meta["trail_armed"] = bool(trail_armed)
                meta["trail_activation_price"] = float(activation_price)
                if trail_armed:
                    prior_stop = float(position.stop_price)
                    candidate_stop = min(position.stop_price, position.lowest_price * (1.0 + position.trail_pct))
                    position.stop_price = candidate_stop
                    if isinstance(meta, dict) and candidate_stop < prior_stop - 1e-12:
                        append_management_adjustment(meta,{"manager": "adaptive", "kind": "stop", "reason": "trail", "from": prior_stop, "to": float(candidate_stop)})
            if last_price >= position.stop_price and not broker_owns_stop:
                return True, "stop"
            if position.target_price is not None and last_price <= position.target_price and not suppress_target_exit and not broker_owns_target:
                return True, "target"
        return False, "hold"
