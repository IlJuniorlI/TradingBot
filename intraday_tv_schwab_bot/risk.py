# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import BotConfig
from .models import ASSET_TYPE_EQUITY, OPTION_ASSET_TYPES, Position, Side, Signal
from ._strategies.registry import is_option_strategy
from .utils import append_management_adjustment, now_et

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class StopoutRecord:
    """One record per recent losing exit, used for the same-level retry block.

    ``exit_price`` is where the loss actually fired (the fill price), not the
    configured stop level — a structure_bearish_exit at 200.30 and a hard
    stop at 198.98 both populate exit_price=fill. Kept in a list (not a dict
    keyed by symbol) so multiple stopouts on the same symbol within the
    window all participate in the block check.
    """
    symbol: str
    side: Side
    exit_price: float
    atr: float
    timestamp: datetime


@dataclass(slots=True)
class RiskState:
    realized_pnl: float = 0.0
    # Direction-aware cooldown keyed by (SYMBOL_UPPER, Side). A LONG exit
    # only blocks same-direction re-entry; opposite-direction entries remain
    # allowed so the bot can flip short on a genuine bearish reversal.
    # When ``config.risk.cooldown_direction_aware`` is False, writes mirror
    # both directions to preserve the legacy behavior.
    cooldown_until: dict[tuple[str, "Side"], datetime] = field(default_factory=dict)
    # Recent stopouts for the same-level retry block. Pruned lazily inside
    # can_open; cap at ~100 records in pathological scenarios.
    recent_stopouts: list[StopoutRecord] = field(default_factory=list)
    # ET session date that owns the current realized_pnl tally. When the date
    # rolls over, realized_pnl is reset to 0.0 so that max_daily_loss behaves
    # as a per-day gate (matching README.md:195) rather than a per-lifetime cap.
    session_date: date | None = None


class RiskManager:
    def __init__(self, config: BotConfig):
        self.config = config
        self.state = RiskState()
        self._reentry_policy = self._normalized_reentry_policy()

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

        If ``side`` is None (legacy callers, e.g. test fixtures that don't
        track direction), returns True if EITHER direction is on cooldown —
        preserves the strictest legacy behavior. When called with a specific
        side from ``can_open``, only the same-side cooldown blocks.
        """
        key = self._symbol_key(symbol)
        now = now_et()
        if side is not None:
            until = self.state.cooldown_until.get((key, side))
            return bool(until and now < until)
        # Legacy: any direction on cooldown
        for (stored_key, _stored_side), until in self.state.cooldown_until.items():
            if stored_key == key and until and now < until:
                return True
        return False

    def _reset_if_new_session(self) -> None:
        """Reset per-day realized P&L when the ET session date rolls over.

        Without this, ``max_daily_loss`` accumulates across days on continuous
        multi-day runs and one losing session blocks all future entries until
        the process is restarted.
        """
        current_date = now_et().date()
        if self.state.session_date is None:
            self.state.session_date = current_date
            return
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

    def register_realized_pnl(self, pnl: float) -> None:
        self._reset_if_new_session()
        self.state.realized_pnl += float(pnl)

    def register_exit(
        self,
        symbol: str,
        pnl: float,
        *,
        additional_symbol: str | None = None,
        side: "Side | None" = None,
        exit_price: float | None = None,
        atr: float | None = None,
    ) -> None:
        """Record an exit: update realized_pnl, cooldown, and recent-stopout log.

        ``side`` — when provided, drives direction-aware cooldown. Callers that
        still pass only symbol/pnl get both-direction cooldown for backward
        compatibility.
        ``exit_price`` + ``atr`` — when provided AND the exit was a loss, a
        ``StopoutRecord`` is appended so ``can_open`` can enforce the
        same-level retry block. ``exit_price`` is the actual fill price.
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

        # Record stopout for same-level retry block. Only meaningful when we
        # have a fill price and ATR — callers without these pass None and the
        # block is skipped for this exit. Also skip if the exit was a winner
        # (pnl > 0): the "chase a losing level" pattern only applies to losses.
        if side is not None and exit_price is not None and atr is not None and float(pnl) <= 0:
            try:
                record = StopoutRecord(
                    symbol=key,
                    side=side,
                    exit_price=float(exit_price),
                    atr=max(1e-6, float(atr)),
                    timestamp=now_et(),
                )
                self.state.recent_stopouts.append(record)
                # Trim: keep only records within the block window (plus a small
                # margin) to cap memory in pathological sessions.
                window_minutes = max(1, int(getattr(self.config.risk, "same_level_block_minutes", 30)))
                cutoff = now_et() - timedelta(minutes=window_minutes * 2)
                self.state.recent_stopouts = [r for r in self.state.recent_stopouts if r.timestamp >= cutoff]
            except Exception:
                LOG.debug("Could not record stopout for same-level block", exc_info=True)

    def can_open(self, signal: Signal, positions: dict[str, Position]) -> tuple[bool, str]:
        self._reset_if_new_session()
        if self.state.realized_pnl <= -abs(self.config.risk.max_daily_loss):
            LOG.warning(
                "Daily loss limit reached: realized_pnl=%.2f limit=%.2f — blocking %s %s",
                self.state.realized_pnl, self.config.risk.max_daily_loss,
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
        # Sector concentration guard — block if too many same-direction positions
        # in the same correlated group (e.g., 3 LONG tech stocks simultaneously).
        # Configured per-strategy in params.sector_groups / params.max_same_sector_same_direction.
        strategy_params = {}
        try:
            strategy_params = self.config.strategies.get(signal.strategy, self.config.active_strategy).params or {}
        except Exception:
            pass
        max_sector = int(strategy_params.get("max_same_sector_same_direction", 0) or 0)
        if max_sector > 0:
            sector_groups = strategy_params.get("sector_groups") or {}
            signal_symbol = self._symbol_key(signal.symbol)
            signal_sector = None
            for sector, members in sector_groups.items():
                if signal_symbol in {self._symbol_key(m) for m in (members or [])}:
                    signal_sector = sector
                    break
            if signal_sector is not None:
                sector_members = {self._symbol_key(m) for m in sector_groups.get(signal_sector, [])}
                same_sector_same_dir = sum(
                    1 for pos in positions.values()
                    if pos.side == signal.side and self._symbol_key(pos.symbol) in sector_members
                )
                if same_sector_same_dir >= max_sector:
                    LOG.warning(
                        "Sector concentration limit: %s %s blocked — %d/%d %s positions in sector '%s'",
                        signal.symbol, signal.side.value, same_sector_same_dir, max_sector,
                        signal.side.value, signal_sector,
                    )
                    return False, "sector_concentration"
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
        # Same-level retry block: if a same-side stopout on this symbol is
        # recent AND the current entry candidate is within N*ATR of that
        # prior stop, block unless the fib-pullback override applies.
        blocked, block_reason = self._same_level_block_check(signal)
        if blocked:
            return False, block_reason
        if signal.side == Side.SHORT and not self.config.risk.allow_short and not is_option_strategy(signal.strategy):
            return False, "shorts_disabled"
        return True, "ok"

    def _same_level_block_check(self, signal: Signal) -> tuple[bool, str]:
        """Enforce the same-level retry block with a fib-pullback exception.

        Returns ``(blocked, reason)``. Iterates recent stopouts; a record
        blocks the signal iff:
          - same symbol (or its underlying key)
          - same side
          - within ``same_level_block_minutes`` of the stopout
          - |signal_entry - stopout_price| <= same_level_block_atr_mult * atr

        If all four hold, the fib-pullback check runs: if the signal's entry
        price is inside the [0.5, 0.786] retracement band of the swing
        captured in signal.metadata (tech_fib_anchor_low/high), the block
        is overridden — the entry is a proper pullback, not a breakout
        chase.
        """
        window_minutes = max(0, int(getattr(self.config.risk, "same_level_block_minutes", 30)))
        atr_mult = max(0.0, float(getattr(self.config.risk, "same_level_block_atr_mult", 0.3)))
        if window_minutes <= 0 or atr_mult <= 0 or not self.state.recent_stopouts:
            return False, "ok"
        key = self._symbol_key(signal.symbol)
        meta = signal.metadata if isinstance(signal.metadata, dict) else {}
        raw_key = meta.get("position_key") if isinstance(meta, dict) else None
        raw_key_norm = self._symbol_key(raw_key) if raw_key else None
        signal_entry = self._signal_entry_price(signal)
        if signal_entry is None or signal_entry <= 0:
            return False, "ok"
        cutoff = now_et() - timedelta(minutes=window_minutes)
        for record in reversed(self.state.recent_stopouts):
            if record.timestamp < cutoff:
                continue
            if record.side != signal.side:
                continue
            if record.symbol != key and record.symbol != (raw_key_norm or key):
                continue
            threshold = atr_mult * record.atr
            if threshold <= 0:
                continue
            if abs(signal_entry - record.exit_price) > threshold:
                continue
            # Fib-pullback override: allow the entry if it sits inside the
            # [0.5, 0.786] retracement band of the most-recent swing stored
            # in signal metadata. A LONG retrace back to the 0.5-0.786 zone
            # of a bullish impulse is a proper pullback entry, not a chase.
            if self._fib_pullback_override(signal, signal_entry):
                LOG.info(
                    "Same-level block overridden by fib-pullback for %s %s entry=%.4f "
                    "prior_exit=%.4f atr=%.4f",
                    signal.symbol, signal.side.value, signal_entry,
                    record.exit_price, record.atr,
                )
                return False, "ok"
            return True, "same_level_retry_block"
        return False, "ok"

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
            if 0 < value == value:  # NaN-safe: NaN fails both sides of the chain.
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

        Keys are ``tech_``-prefixed because the shared
        ``strategy_base._technical_lists(ctx, prefix="tech")`` helper
        stamps the fib anchors onto every strategy's signal metadata under
        that namespace.
        """
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

    def size_position(self, entry_price: float, stop_price: float) -> int:
        if entry_price <= 0 or stop_price <= 0:
            return 0
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return 0
        # Risk budget is max_notional_per_trade * risk_per_trade_frac_of_notional.
        # Both inputs come from config.risk. This is a fraction of the
        # per-trade notional cap, not a fraction of account equity — see
        # the RiskConfig docstring in config.py for the full convention.
        risk_budget = self.config.risk.max_notional_per_trade * self.config.risk.risk_per_trade_frac_of_notional
        qty = self.floor_discrete_units(risk_budget, stop_distance)
        max_qty_by_notional = self.floor_discrete_units(self.config.risk.max_notional_per_trade, entry_price)
        return max(0, min(qty, max_qty_by_notional))

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

    @staticmethod
    def _peak_giveback_floor_r(peak_r: float) -> float | None:
        """Tiered give-back floor (lock-in fraction grows with peak size).

        Returns the current_r level at which the trade should exit, given
        the peak reached since entry. None when peak is below the minimum
        threshold — no floor in that case (Fix 4's BE arm handles <1R).
        """
        if peak_r < 1.0:
            return None
        if peak_r < 2.0:
            return peak_r * 0.5   # 50% giveback allowed 1R-2R
        if peak_r < 3.0:
            return peak_r * 0.6   # 40% giveback 2R-3R
        return peak_r * 0.7       # 30% giveback 3R+

    def _peak_giveback_triggered(self, position: Position, last_price: float, initial_risk: float) -> bool:
        min_r = float(getattr(self.config.risk, "peak_giveback_min_r", 1.0) or 1.0)
        peak_r, current_r = self._peak_and_current_r(position, last_price, initial_risk)
        if peak_r < min_r:
            return False
        floor_r = self._peak_giveback_floor_r(peak_r)
        if floor_r is None:
            return False
        return current_r <= floor_r

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
            floor_r = self._peak_giveback_floor_r(peak_r)
            if isinstance(meta, dict):
                meta["peak_giveback_fired"] = True
                meta["peak_giveback_peak_r"] = round(float(peak_r), 4)
                meta["peak_giveback_floor_r"] = None if floor_r is None else round(float(floor_r), 4)
                meta["peak_giveback_current_r"] = round(float(current_r), 4)
            return True, f"peak_giveback:peak{peak_r:.2f}R_floor{(floor_r or 0.0):.2f}R"

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
            if last_price <= position.stop_price:
                return True, "stop"
            if position.target_price is not None and last_price >= position.target_price and not suppress_target_exit:
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
            if last_price >= position.stop_price:
                return True, "stop"
            if position.target_price is not None and last_price <= position.target_price and not suppress_target_exit:
                return True, "target"
        return False, "hold"
