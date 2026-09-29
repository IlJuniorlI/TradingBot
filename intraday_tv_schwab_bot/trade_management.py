# SPDX-License-Identifier: MIT
"""TradeManager — in-position stop and target management.

Every step that moves an open position's stop or target, or takes a level
exit, lives here:

- ``update_position``: the peak give-back floor, the option premium ratchet,
  the adaptive breakeven / profit lock / runner extension / trail, and the
  stop and target exits themselves (the target deferring to a broker-held
  bracket leg and to the adaptive ladder's touch hold);
- ``manage_sr_flip``: ``risk.trade_management_mode: sr_flip``, the stop to a
  flipped level and the target to the next one;
- ``manage_adaptive_ladder``: the adaptive ladder's touch hold
  (``shared_exit.adaptive_ladder_touch_hold``) and the exit it takes.

``PositionManager`` runs them once per position per cycle -- the two
managers, then the level check -- and owns everything after: the exit
decision's order, the bracket sync and the booking. ``RiskManager`` keeps the
entry side: the entry gates, sizing, the daily loss and the cooldowns. The
two rules a stock position's levels start from are here too:
``default_levels`` (the default-distance stop and target) and
``trail_allowed`` (whether its stop trails). Extracted from
``RiskManager.update_position``, ``PositionManager``'s managers and
``risk.py``'s level rules (refactor cut C38).
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from .audit_logger import AuditLogger
from .broker_payloads import active_broker_bracket
from .config import BotConfig, RiskConfig
from .data_feed import MarketDataStore
from .levels_shared import effective_side_tolerance, select_next_distinct_level
from .models import ExitDecision, Position, Side, is_option_asset
from .numeric import safe_float
from .position_metrics import (
    LADDER_TOUCH_HOLD_KEY,
    PEAK_GIVEBACK,
    PEAK_GIVEBACK_HIGH_CONVICTION,
    PEAK_GIVEBACK_LOW_TIER,
    TARGET_HOLD_GUARD,
    TARGET_HOLD_TIMEOUT,
    TARGET_WEAK_CLOSE,
    append_management_adjustment,
    favorable_move,
)
from .reasons import exit_reason_code
from . import sessions

if TYPE_CHECKING:
    from ._strategies.strategy_base import BaseStrategy

# The position manager's logger: the ladder's LADDER_TOUCH / LADDER_VERDICT
# lines are part of the management cycle's log.
LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


def default_levels(side: Side, entry_price: float, risk_cfg: RiskConfig) -> tuple[float, float]:
    """The default-distance stop and target of a stock position:
    ``risk.default_stop_pct`` / ``default_target_pct`` from ``entry_price``,
    a LONG's stop and a SHORT's target floored at $0.01. What a fill the
    signal's levels no longer fit is booked with (``EntryGatekeeper``), and a
    broker position restored without its saved levels (``StartupReconciler``).
    """
    stop_pct = float(risk_cfg.default_stop_pct)
    target_pct = float(risk_cfg.default_target_pct)
    if side == Side.LONG:
        return max(0.01, entry_price * (1.0 - stop_pct)), entry_price * (1.0 + target_pct)
    return entry_price * (1.0 + stop_pct), max(0.01, entry_price * (1.0 - target_pct))


def trail_allowed(mode: str, meta: Mapping[str, Any], *, options: bool) -> bool:
    """Whether a position's stop trails by its ``trail_pct``: never an
    option's (its premium ratchet stands in), always under the ``adaptive``
    trade management mode, and under ``adaptive_ladder`` unless the ladder
    manages the position (``metadata['ladder_management_enabled']``)."""
    if options:
        return False
    return mode == "adaptive" or (mode == "adaptive_ladder" and not bool(meta.get("ladder_management_enabled")))


def _next_unpassed_rung(rungs: list, active_index: int, close: float,
                        side: Side, gap: float) -> int | None:
    """Index of the first rung after ``active_index`` that price has not already
    passed, or ``None`` when every remaining rung is behind the trade.

    A rung behind price cannot serve as a target — setting one would exit
    immediately — and cannot serve as a defense level either, so the ladder is
    finished and the caller promotes the position to a runner. A rung whose
    price is not a finite number above 0 is no target and is skipped.
    """
    for index in range(int(active_index) + 1, len(rungs)):
        entry = rungs[index]
        if not isinstance(entry, dict):
            continue
        price = safe_float(entry.get("price"), None, finite=True)
        if price is None or price <= 0:
            continue
        if side == Side.LONG:
            if price > close + gap:
                return index
        elif price < close - gap:
            return index
    return None


# The adaptive ladder's touch hold (``shared_exit.adaptive_ladder_touch_hold``).
# A delivered touch bar that closed through the target at least this far into
# its range, from the low for a LONG and from the high for a SHORT, promotes
# the rung; any other close exits.
LADDER_TOUCH_CLOSE_POSITION_MIN = 0.55
# The hold's stop buffer, the widest of the S/R context's level buffer, this
# share of the rung's zone width and this share of the touch price. It sets
# the in-bar guard and the promoted stop (the zone-flip promotion's formula).
LADDER_STOP_BUFFER_ZONE_FRAC = 0.25
LADDER_STOP_BUFFER_PRICE_FRAC = 0.0005
# A promotion's next target is the first later rung more than this share of
# the touch bar's close beyond that close; a rung within it counts as passed.
LADDER_NEXT_RUNG_GAP_FRAC = 0.0005
_TOUCH_HOLD_EXIT_CODES = frozenset({TARGET_WEAK_CLOSE, TARGET_HOLD_GUARD, TARGET_HOLD_TIMEOUT})


def _finite_number(value: Any) -> float | None:
    """``value`` when it is a finite int or float (not a bool, not text)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _whole_number(value: Any) -> int | None:
    """``value`` as an int when it is a finite whole number (``2`` or ``2.0``)."""
    number = _finite_number(value)
    return int(number) if number is not None and number.is_integer() else None


def _aware_time(value: Any) -> pd.Timestamp | None:
    """``value`` (an ISO string or a datetime) as a tz-aware timestamp in the
    exchange's time zone; None when it does not parse or names no zone."""
    if not isinstance(value, str | datetime):
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp) or stamp.tzinfo is None:
        return None
    return stamp.tz_convert(sessions.EXCHANGE_TZ)


@dataclass(frozen=True, slots=True)
class _TouchHold:
    """A touch hold in progress: ``metadata[LADDER_TOUCH_HOLD_KEY]``, written
    by ``TradeManager._start_touch_hold`` and read back each cycle,
    possibly from the position store after a restart.

    ``read`` checks every field the hold reads, so a hold is either whole or
    dropped: one that came back without its ``kind``, with a naive time or a
    rung index past its count can never apply half a promotion. The times
    are stored as ISO strings, ``exit_reason`` once the hold has decided to
    exit (the order is then sent again with it until it books)."""

    rung_index: int
    rung_count: int
    level: float
    zone_width: float
    kind: str
    touch_price: float
    touch_at: pd.Timestamp
    touch_bar: pd.Timestamp
    stop_buffer: float
    guard: float
    deadline: pd.Timestamp
    exit_reason: str | None = None

    def to_meta(self) -> dict[str, Any]:
        return {
            "rung_index": self.rung_index, "rung_count": self.rung_count, "level": self.level,
            "zone_width": self.zone_width, "kind": self.kind, "touch_price": self.touch_price,
            "touch_at": self.touch_at.isoformat(), "touch_bar": self.touch_bar.isoformat(),
            "stop_buffer": self.stop_buffer, "guard": self.guard, "deadline": self.deadline.isoformat(),
            "exit_reason": self.exit_reason,
        }

    @classmethod
    def read(cls, raw: Any) -> _TouchHold | None:
        """The hold ``raw`` holds, or None when any field it reads is missing
        or unreadable: a number that is not finite (or a negative buffer or
        zone), a rung index that is not a whole number below the rung count,
        a blank ``kind``, a time that does not parse or names no time zone,
        or an ``exit_reason`` that is not one of the hold's exits."""
        if not isinstance(raw, dict):
            return None
        numbers = {key: _finite_number(raw.get(key))
                   for key in ("level", "zone_width", "touch_price", "stop_buffer", "guard")}
        if any(value is None for value in numbers.values()) or numbers["zone_width"] < 0 or numbers["stop_buffer"] < 0:
            return None
        rung_index, rung_count = _whole_number(raw.get("rung_index")), _whole_number(raw.get("rung_count"))
        if rung_index is None or rung_count is None or not 0 <= rung_index < rung_count:
            return None
        kind = raw.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            return None
        times = {key: _aware_time(raw.get(key)) for key in ("touch_at", "touch_bar", "deadline")}
        if any(value is None for value in times.values()):
            return None
        exit_reason = raw.get("exit_reason")
        if exit_reason is not None and not (isinstance(exit_reason, str)
                                            and exit_reason_code(exit_reason) in _TOUCH_HOLD_EXIT_CODES):
            return None
        return cls(rung_index=rung_index, rung_count=rung_count, kind=kind, exit_reason=exit_reason,
                   **numbers, **times)


def _ladder_rungs(meta: Mapping[str, Any]) -> list | None:
    """A laddered position's rungs, or None when they are not a non-empty
    list (every ladder builder emits at least one rung)."""
    rungs = meta.get("ladder_rungs")
    return rungs if isinstance(rungs, list) and rungs else None


def _delivered_touch_bar(frame: pd.DataFrame | None, touch_bar: pd.Timestamp,
                         now: pd.Timestamp) -> pd.Series | None:
    """The touch bar's row once it has closed and is in the frame, else None.

    The management frame holds completed bars, each delivered a few seconds
    after it closes. The clock check keeps a frame that does carry a forming
    row from being judged before its minute is over."""
    if frame is None or frame.empty or now < touch_bar + pd.Timedelta(minutes=1):
        return None
    index = pd.DatetimeIndex(frame.index)
    label = touch_bar if index.tz is not None else touch_bar.tz_convert(sessions.EXCHANGE_TZ).tz_localize(None)
    hits = (index == label).nonzero()[0]
    return frame.iloc[int(hits[-1])] if len(hits) else None


def _touch_bar_verdict(bar: pd.Series, side: Side, level: float) -> tuple[str, float | None, float | None]:
    """``strong``, ``weak`` or ``unreadable`` for a delivered touch bar, with
    its close position (from the low for a LONG, from the high for a SHORT;
    None on a bar with no range or an unreadable price) and its close.

    Strong: the close is at or through ``level`` and its close position is at
    least ``LADDER_TOUCH_CLOSE_POSITION_MIN``. A bar with no range has no
    upper part and is weak."""
    high = safe_float(bar.get("high"), None, finite=True)
    low = safe_float(bar.get("low"), None, finite=True)
    close = safe_float(bar.get("close"), None, finite=True)
    if high is None or low is None or close is None:
        return "unreadable", None, None
    span = high - low
    if span <= 0:
        return "weak", None, close
    long_side = side == Side.LONG
    close_pos = (close - low) / span if long_side else (high - close) / span
    through = close >= level if long_side else close <= level
    return ("strong" if through and close_pos >= LADDER_TOUCH_CLOSE_POSITION_MIN else "weak"), close_pos, close


class TradeManager:
    def __init__(
        self,
        config: BotConfig,
        *,
        data: MarketDataStore,
        strategy: BaseStrategy,
        audit: AuditLogger,
    ) -> None:
        self.config = config
        self.data = data
        # The strategy's timeframes and its exit policy: the ladder reads
        # its touch-hold switch and timeout there (strategy.exit_policy).
        self.strategy = strategy
        self.audit = audit

    # ------------------------------------------------------------------
    # The level check: peak give-back, option ratchet, adaptive
    # management, and the stop and target exits.
    # ------------------------------------------------------------------

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
        # The peak is the highest price for a LONG, the lowest for a SHORT.
        extreme = position.highest_price if position.side == Side.LONG else position.lowest_price
        peak = float(extreme) if extreme is not None else float(last_price)
        peak_r = favorable_move(position.side, entry, peak) / initial_risk
        current_r = favorable_move(position.side, entry, float(last_price)) / initial_risk
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
            return peak_r * risk_cfg.peak_giveback_retain_1to2r
        if peak_r < 3.0:
            return peak_r * risk_cfg.peak_giveback_retain_2to3r
        return peak_r * risk_cfg.peak_giveback_retain_3r_plus

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
        # Above 0, checked at load (a 0 read as 1.0 until 2026-09-26).
        default_min_r = self.config.risk.peak_giveback_min_r
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
        low_tier_enabled = self.config.risk.peak_giveback_low_tier_enabled
        low_tier_min_r = self.config.risk.peak_giveback_low_tier_min_r
        low_tier_frac = self.config.risk.peak_giveback_low_tier_giveback_frac
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

        initial_stop = safe_float(meta.get("initial_stop_price"), finite=True)
        if initial_stop is None:
            initial_stop = float(position.stop_price)
        initial_risk = max(0.0, abs(float(position.entry_price) - initial_stop))
        trail_activation_mult = 0.5
        options_position = is_option_asset(meta)

        # Peak-giveback floor — fires *before* normal stop/target/trail logic
        # so a winner that reaches +NR and retraces past the tiered floor
        # gets closed out even if the trail hasn't armed yet. Equity-only
        # (options have their own ratchet via options_breakeven + profit_lock).
        peak_giveback_exit = (
            (not options_position)
            and self.config.risk.peak_giveback_enabled
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
                low_tier_frac = self.config.risk.peak_giveback_low_tier_giveback_frac
                floor_r = peak_r * (1.0 - low_tier_frac)
            else:
                floor_r = self._peak_giveback_floor_r(peak_r)
            if isinstance(meta, dict):
                # The floor and the peak as prices too (2026-09-27), beside
                # their R: what EXIT_CONTEXT records for the exit
                # (``peak_giveback_*`` is in the structured snapshot).
                direction = 1.0 if position.side == Side.LONG else -1.0
                entry = float(position.entry_price)
                meta["peak_giveback_fired"] = True
                meta["peak_giveback_peak_r"] = round(float(peak_r), 4)
                meta["peak_giveback_floor_r"] = None if floor_r is None else round(float(floor_r), 4)
                meta["peak_giveback_current_r"] = round(float(current_r), 4)
                peak_price = position.highest_price if position.side == Side.LONG else position.lowest_price
                meta["peak_giveback_peak_price"] = float(last_price if peak_price is None else peak_price)
                meta["peak_giveback_floor_price"] = (
                    None if floor_r is None else round(entry + direction * float(floor_r) * initial_risk, 6))
                meta["peak_giveback_min_r_used"] = round(min_r_used, 4)
                meta["peak_giveback_override_active"] = override_active
                meta["peak_giveback_low_tier_active"] = low_tier_active
                # Drop the internal sentinels (we promoted them above).
                meta.pop("_peak_giveback_min_r_used", None)
                meta.pop("_peak_giveback_override_active", None)
                meta.pop("_peak_giveback_low_tier_active", None)
            if low_tier_active:
                reason_tag = PEAK_GIVEBACK_LOW_TIER
            elif override_active:
                reason_tag = PEAK_GIVEBACK_HIGH_CONVICTION
            else:
                reason_tag = PEAK_GIVEBACK
            return True, (
                f"{reason_tag}:peak{peak_r:.2f}R_floor{(floor_r or 0.0):.2f}R"
                f"_minR{min_r_used:.2f}"
            )

        # --- Options premium ratchet (breakeven + profit lock) ---
        # Options bypass equity adaptive management, but this simpler premium-
        # based ratchet prevents giving back all gains on a winning trade.
        if options_position:
            opt_cfg = self.config.options
            opt_entry = max(0.01, float(position.entry_price))
            if position.side == Side.LONG:
                opt_peak = float(position.highest_price) if position.highest_price is not None else float(last_price)
                if opt_cfg.options_breakeven_enabled:
                    be_thresh = opt_entry * opt_cfg.options_breakeven_mark_mult
                    be_stop = opt_entry * opt_cfg.options_breakeven_stop_mult
                    if opt_peak >= be_thresh and be_stop > float(position.stop_price):
                        prior = float(position.stop_price)
                        position.stop_price = float(be_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "options_ratchet", "kind": "stop", "reason": "breakeven", "from": prior, "to": float(be_stop)})
                            meta["options_breakeven_armed"] = True
                if opt_cfg.options_profit_lock_enabled:
                    pl_thresh = opt_entry * opt_cfg.options_profit_lock_mark_mult
                    pl_stop = opt_entry * opt_cfg.options_profit_lock_stop_mult
                    if opt_peak >= pl_thresh and pl_stop > float(position.stop_price):
                        prior = float(position.stop_price)
                        position.stop_price = float(pl_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "options_ratchet", "kind": "stop", "reason": "profit_lock", "from": prior, "to": float(pl_stop)})
                            meta["options_profit_lock_armed"] = True
            else:
                # SHORT (credit spreads): mark goes DOWN for profit.
                opt_trough = float(position.lowest_price) if position.lowest_price is not None else float(last_price)
                if opt_cfg.options_breakeven_enabled:
                    be_thresh = opt_entry * (2.0 - opt_cfg.options_breakeven_mark_mult)
                    be_stop = opt_entry * (2.0 - opt_cfg.options_breakeven_stop_mult)
                    if opt_trough <= be_thresh and be_stop < float(position.stop_price):
                        prior = float(position.stop_price)
                        position.stop_price = float(be_stop)
                        if isinstance(meta, dict):
                            append_management_adjustment(meta,{"manager": "options_ratchet", "kind": "stop", "reason": "breakeven", "from": prior, "to": float(be_stop)})
                            meta["options_breakeven_armed"] = True
                if opt_cfg.options_profit_lock_enabled:
                    pl_thresh = opt_entry * (2.0 - opt_cfg.options_profit_lock_mark_mult)
                    pl_stop = opt_entry * (2.0 - opt_cfg.options_profit_lock_stop_mult)
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
        trailing_enabled = trail_allowed(trade_management_mode, meta, options=options_position)
        # The adaptive ladder's touch hold (shared_exit.adaptive_ladder_touch_hold,
        # off by default): while manage_adaptive_ladder holds a touched rung,
        # the target is not taken here. It only ever sets the key when on.
        touch_hold = ladder_management_enabled and isinstance(meta.get(LADDER_TOUCH_HOLD_KEY), dict)

        # Broker-side bracket. The stop is checked here every cycle, whatever
        # rests at the broker: an engine stop exit, like every engine exit,
        # cancels the bracket first and books what its children filled
        # (PositionManager._manage_position), so the two never both fill.
        # Until 2026-09-28 a resting stop child stood the engine's stop down,
        # which left the position unprotected whenever the child no longer
        # stood for the engine's level: a replace that failed left it at an
        # older level, and a STOP_LIMIT that triggered with the price through
        # its limit stayed a working order, unfilled. No order state the bot
        # reads tells a triggered stop from a resting one, and the mark at or
        # through the stop, the one sign the engine sees each cycle, is
        # exactly when that deferral applied, so it is gone rather than
        # qualified. A
        # resting TARGET child still owns the target: a limit in the trade's
        # favour with the stop still resting beside it.
        bracket = active_broker_bracket(position)
        broker_owns_target = bracket is not None and bracket.get("target_order_id") is not None

        def _meta_float(key: str, default: float | None = None) -> float | None:
            return safe_float(meta.get(key), default)

        if position.side == Side.LONG:
            if adaptive_enabled:
                # The peak R, rounded to 9dp (_peak_and_current_r) to absorb
                # IEEE 754 rounding errors that cause exact-threshold hits
                # (e.g. 0.9R, 1.15R) to fail >= checks.
                max_favorable_r = self._peak_and_current_r(position, last_price, initial_risk)[0]
                # Partial-breakeven tier (fires first, at lowest RR). 2026-04-23
                # trades that peaked 0.5–0.8R (AVGO, RBLX 10:00, COST 09:51) had
                # nothing between the trail and the 1.0R breakeven — COST 09:51
                # gave back $32 despite a 0.56R peak. Arms a cheap early stop
                # move at a lower RR gate than the main breakeven.
                partial_breakeven_rr = _meta_float("adaptive_partial_breakeven_rr", None)
                partial_breakeven_offset_r = _meta_float("adaptive_partial_breakeven_offset_r", 0.0)
                if partial_breakeven_rr is not None and max_favorable_r >= partial_breakeven_rr:
                    candidate_stop = float(position.entry_price) + (float(partial_breakeven_offset_r) * initial_risk)
                    if math.isfinite(candidate_stop):
                        if candidate_stop > float(position.stop_price):
                            prior_stop = float(position.stop_price)
                            position.stop_price = float(candidate_stop)
                            if isinstance(meta, dict):
                                append_management_adjustment(meta,{"manager": "adaptive", "kind": "stop", "reason": "partial_breakeven", "from": prior_stop, "to": float(candidate_stop)})
                        meta["adaptive_partial_breakeven_armed"] = True
                breakeven_rr = _meta_float("adaptive_breakeven_rr", None)
                breakeven_offset_r = _meta_float("adaptive_breakeven_offset_r", 0.0)
                if breakeven_rr is not None and max_favorable_r >= breakeven_rr:
                    candidate_stop = float(position.entry_price) + (float(breakeven_offset_r) * initial_risk)
                    if math.isfinite(candidate_stop):
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
                    if math.isfinite(candidate_stop):
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
                    if math.isfinite(candidate_target):
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
            if position.target_price is not None and last_price >= position.target_price and not touch_hold and not broker_owns_target:
                return True, "target"
        else:
            if adaptive_enabled:
                max_favorable_r = self._peak_and_current_r(position, last_price, initial_risk)[0]
                # Mirror of the LONG partial_breakeven tier above — see comment
                # at LONG branch for motivation.
                partial_breakeven_rr = _meta_float("adaptive_partial_breakeven_rr", None)
                partial_breakeven_offset_r = _meta_float("adaptive_partial_breakeven_offset_r", 0.0)
                if partial_breakeven_rr is not None and max_favorable_r >= partial_breakeven_rr:
                    candidate_stop = float(position.entry_price) - (float(partial_breakeven_offset_r) * initial_risk)
                    if math.isfinite(candidate_stop):
                        if candidate_stop < float(position.stop_price):
                            prior_stop = float(position.stop_price)
                            position.stop_price = float(candidate_stop)
                            if isinstance(meta, dict):
                                append_management_adjustment(meta,{"manager": "adaptive", "kind": "stop", "reason": "partial_breakeven", "from": prior_stop, "to": float(candidate_stop)})
                        meta["adaptive_partial_breakeven_armed"] = True
                breakeven_rr = _meta_float("adaptive_breakeven_rr", None)
                breakeven_offset_r = _meta_float("adaptive_breakeven_offset_r", 0.0)
                if breakeven_rr is not None and max_favorable_r >= breakeven_rr:
                    candidate_stop = float(position.entry_price) - (float(breakeven_offset_r) * initial_risk)
                    if math.isfinite(candidate_stop):
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
                    if math.isfinite(candidate_stop):
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
                    if math.isfinite(candidate_target):
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
            if position.target_price is not None and last_price <= position.target_price and not touch_hold and not broker_owns_target:
                return True, "target"
        return False, "hold"

    # ------------------------------------------------------------------
    # The sr_flip manager (risk.trade_management_mode: sr_flip).
    # ------------------------------------------------------------------

    def manage_sr_flip(self, position: Position, frame: pd.DataFrame | None, last_price: float) -> None:
        if isinstance(position.metadata, dict):
            position.metadata.setdefault("management_adjustments", [])
        if self.config.risk.trade_management_mode != "sr_flip":
            return
        cfg = getattr(self.config, "support_resistance", None)
        if cfg is None or not bool(cfg.enabled):
            return
        if is_option_asset(position.metadata):
            return
        if frame is None or frame.empty or last_price <= 0:
            return
        symbol = str(position.metadata.get("underlying") or position.symbol)
        sr_ctx = self.data.get_support_resistance(symbol, current_price=last_price, flip_frame=frame, mode="trading", timeframe_minutes=self.strategy.htf_minutes()) if self.data is not None else None
        if sr_ctx is None:
            return
        last = frame.iloc[-1]
        close = safe_float(last.get("close"), last_price)
        ema9 = safe_float(last.get("ema9"), close) if "ema9" in frame.columns else close
        ema20 = safe_float(last.get("ema20"), close) if "ema20" in frame.columns else close
        vwap = safe_float(last.get("vwap"), close) if "vwap" in frame.columns else close
        ret5 = safe_float(last.get("ret5"), 0.0) if "ret5" in frame.columns else 0.0
        atr = safe_float(last.get("atr14"), 0.0) if "atr14" in frame.columns else 0.0
        stop_buffer = max(
            float(sr_ctx.level_buffer or 0.0),
            max(atr * float(getattr(cfg, "flip_stop_buffer_atr_mult", 0.25) or 0.25), close * 0.0005),
        )
        require_momentum = bool(getattr(cfg, "flip_target_requires_momentum_confirm", True))
        structural_gap = effective_side_tolerance(cfg, close, atr=atr, sr_ctx=sr_ctx)

        def _momentum_ok(long_side: bool) -> bool:
            if not require_momentum:
                return True
            if long_side:
                return bool(close >= ema9 and close >= max(vwap, ema20) and ret5 >= -0.0005)
            return bool(close <= ema9 and close <= min(vwap, ema20) and ret5 <= 0.0005)

        if position.side == Side.LONG:
            flipped_support = sr_ctx.broken_resistance
            if flipped_support is not None:
                candidate_stop = float(flipped_support.price) - stop_buffer
                if close > candidate_stop > float(position.stop_price):
                    prior_stop = float(position.stop_price)
                    position.stop_price = float(candidate_stop)
                    if isinstance(position.metadata, dict):
                        position.metadata["sr_flip_stop_source"] = float(flipped_support.price)
                        append_management_adjustment(position.metadata,{"manager": "sr_flip", "kind": "stop", "reason": "flipped_support", "from": prior_stop, "to": float(candidate_stop), "source_level": float(flipped_support.price)})
            target_level = select_next_distinct_level(getattr(sr_ctx, 'resistances', None), float(flipped_support.price) if flipped_support is not None else None, above=True, minimum_gap=structural_gap) if flipped_support is not None else None
            if target_level is None and flipped_support is None:
                target_level = sr_ctx.nearest_resistance
            if target_level is not None and _momentum_ok(True):
                candidate_target = float(target_level.price) - float(sr_ctx.level_buffer or 0.0)
                current_target = safe_float(position.target_price, None)
                if candidate_target > close and (current_target is None or candidate_target > current_target + max(close * 0.0005, 1e-6)):
                    prior_target = float(current_target) if current_target is not None else None
                    position.target_price = float(candidate_target)
                    if isinstance(position.metadata, dict):
                        position.metadata["sr_flip_target_source"] = float(target_level.price)
                        append_management_adjustment(position.metadata,{"manager": "sr_flip", "kind": "target", "reason": "next_resistance", "from": prior_target, "to": float(candidate_target), "source_level": float(target_level.price), "structural_gap": float(structural_gap)})
        else:
            flipped_resistance = sr_ctx.broken_support
            if flipped_resistance is not None:
                candidate_stop = float(flipped_resistance.price) + stop_buffer
                if close < candidate_stop < float(position.stop_price):
                    prior_stop = float(position.stop_price)
                    position.stop_price = float(candidate_stop)
                    if isinstance(position.metadata, dict):
                        position.metadata["sr_flip_stop_source"] = float(flipped_resistance.price)
                        append_management_adjustment(position.metadata,{"manager": "sr_flip", "kind": "stop", "reason": "flipped_resistance", "from": prior_stop, "to": float(candidate_stop), "source_level": float(flipped_resistance.price)})
            target_level = select_next_distinct_level(getattr(sr_ctx, 'supports', None), float(flipped_resistance.price) if flipped_resistance is not None else None, above=False, minimum_gap=structural_gap) if flipped_resistance is not None else None
            if target_level is None and flipped_resistance is None:
                target_level = sr_ctx.nearest_support
            if target_level is not None and _momentum_ok(False):
                candidate_target = float(target_level.price) + float(sr_ctx.level_buffer or 0.0)
                current_target = safe_float(position.target_price, None)
                if candidate_target < close and (current_target is None or candidate_target < current_target - max(close * 0.0005, 1e-6)):
                    prior_target = float(current_target) if current_target is not None else None
                    position.target_price = float(candidate_target)
                    if isinstance(position.metadata, dict):
                        position.metadata["sr_flip_target_source"] = float(target_level.price)
                        append_management_adjustment(position.metadata,{"manager": "sr_flip", "kind": "target", "reason": "next_support", "from": prior_target, "to": float(candidate_target), "source_level": float(target_level.price), "structural_gap": float(structural_gap)})

    # ------------------------------------------------------------------
    # The adaptive ladder
    #
    # The strategy builds the rungs at entry (``_build_ladder_rungs``, or the
    # key-levels peer rungs) and emits the active rung as the target. With
    # ``shared_exit.adaptive_ladder_touch_hold`` off -- every preset and the
    # code default -- that target is a plain take-profit: update_position
    # exits on the first quote at it and nothing here acts.
    #
    # Until 2026-09-27 this pass also ran a target-exit suppression and a
    # two-bar zone-flip rung promotion, both removed. Since 2026-05-14 the
    # suppression needed the last closed bar in the frame to have closed
    # strongly through the target (and a confirmation index still leaning
    # the trade's way); before that one quote at the target was enough, and
    # it did hold targets live on 2026-05-13 and 05-14. The frame holds only
    # delivered bars, so that bar closed one to two minutes before the quote
    # being judged, and a quote at the target during it had already been
    # taken as the target. The suppression could act only when the quote
    # sampling (one quote per management pass, about every 4-25 s) missed a
    # strong 1m close through the target and a later quote reached it again
    # before the next bar was delivered (or the quote's mark lagged the
    # prints that way). The 2026-09-26/27 replays found it only with a
    # quote once a minute, in three trades (AMD 2026-09-24 and ADBE better
    # for taking the target, NVDA worse); where it happens the target is now
    # taken. The zone flip
    # needed two delivered bars wholly past the rung's zone while the target
    # still sat on the rung, which only a restart past the rung or an exit
    # order failing for two bars could leave; no ladder adjustment was
    # logged from 2026-05-01 to 2026-09-25.
    # ------------------------------------------------------------------

    def manage_adaptive_ladder(self, position: Position, frame: pd.DataFrame | None,
                               last_price: float, price_at: datetime | None) -> ExitDecision | None:
        """The adaptive ladder's touch hold, when it is on; the exit it takes.

        ``price_at`` is when ``last_price`` was observed (a quote's
        ``fetched_at``, or the open time of the bar whose close it is; see
        ``PositionManager._position_management_snapshot``): the touch is
        attributed to the 1m bar that time falls in, not to the cycle's
        clock, which can run up to the quote cache age later.

        A hold starts on the first price at or through the target, the price
        ``update_position`` would take the target on; while it lasts,
        ``LADDER_TOUCH_HOLD_KEY`` in the metadata keeps ``update_position``
        off the target. It ends:

        - at once, on a price ``stop_buffer`` back through the rung (under
          it for a LONG, over it for a SHORT): ``target_hold_guard``;
        - when the bar that touched the target is in the frame, delivered a
          few seconds after it closes, with one verdict on that bar. A close
          at or through the target at least ``LADDER_TOUCH_CLOSE_POSITION_MIN``
          of the way up its range (down, for a SHORT) promotes the rung. Any
          other close exits at market: ``target_weak_close``. A bar with no
          range has no upper part and is weak; one with an unreadable price
          is weak too;
        - when the touch bar is still missing the configured timeout after
          it closed: ``target_hold_timeout``, at market.

        A promotion moves the stop to the rung less ``stop_buffer`` (never
        loosening it) and the target to the first later rung more than
        ``LADDER_NEXT_RUNG_GAP_FRAC`` of the touch bar's close beyond that
        close (``_next_unpassed_rung``), or clears it past the last rung,
        which leaves a runner. A price already at the new target starts that
        rung's hold in the same pass. ``stop_buffer`` is the widest of the
        S/R context's level buffer (when it is a finite number),
        ``LADDER_STOP_BUFFER_ZONE_FRAC`` of the rung's zone width and
        ``LADDER_STOP_BUFFER_PRICE_FRAC`` of the touch price.

        The exits belong to the ``risk`` family, and the caller lets
        ``update_position``'s own exits (stop, peak giveback) win on the same
        cycle. A decided exit stays on the hold, so a failed order is sent
        again with the same reason, whatever the price does next, and the
        target is never taken instead. There is no index veto. Ladder metadata
        the hold cannot read (the rungs, the active rung, the price's time) is
        reported and leaves the target exit in place; a hold it cannot read
        back is dropped whole. Every touch and every verdict is logged on one
        INFO line (``LADDER_TOUCH`` / ``LADDER_VERDICT``), so a dry-run A/B
        can be read from the log.
        """
        if self.config.risk.trade_management_mode != "adaptive_ladder":
            return None
        meta = position.metadata if isinstance(position.metadata, dict) else None
        if meta is None or not bool(meta.get("ladder_management_enabled")):
            return None
        if is_option_asset(meta):
            return None
        timeout = self.strategy.exit_policy.ladder_touch_hold_timeout_seconds()
        if timeout is None:
            # The hold is off. One a restart carried over from a run with it
            # on would keep update_position off the target for good.
            if meta.pop(LADDER_TOUCH_HOLD_KEY, None) is not None:
                LOG.info("LADDER_VERDICT symbol=%s side=%s outcome=dropped reason=touch_hold_off",
                         position.symbol, position.side.value)
            return None
        if not math.isfinite(last_price) or last_price <= 0:
            return None
        now = pd.Timestamp(sessions.now_et())
        if LADDER_TOUCH_HOLD_KEY in meta:
            decision = self._resolve_touch_hold(position, meta, frame, float(last_price), now)
            if decision is not None or LADDER_TOUCH_HOLD_KEY in meta:
                return decision
        self._start_touch_hold(position, meta, frame, float(last_price), price_at, now, float(timeout))
        return None

    def _ladder_unreadable(self, position: Position, what: str, message: str) -> None:
        self.audit.log_cycle(f"ladder_unreadable:{position.symbol}", what, message, level=logging.WARNING)

    def _start_touch_hold(self, position: Position, meta: dict[str, Any], frame: pd.DataFrame | None,
                          last_price: float, price_at: datetime | None, now: pd.Timestamp, timeout: float) -> None:
        """Start a hold when ``last_price`` is at or through the target. The
        ladder metadata is read back from the position store: a value the
        hold cannot read is reported, and the position keeps its target
        exit."""
        target = safe_float(position.target_price, None, finite=True)
        if target is None:
            return
        long_side = position.side == Side.LONG
        if (last_price < target) if long_side else (last_price > target):
            return
        rungs = _ladder_rungs(meta)
        if rungs is None:
            self._ladder_unreadable(position, "rungs", f"Ladder rungs of {position.symbol} are unreadable "
                                    f"({meta.get('ladder_rungs')!r}); taking the target without a hold")
            return
        active_index = _whole_number(meta.get("ladder_active_index"))
        if active_index is None or not 0 <= active_index < len(rungs):
            self._ladder_unreadable(position, "active_index",
                                    f"Ladder active index {meta.get('ladder_active_index')!r} of {position.symbol} "
                                    f"is unreadable for {len(rungs)} rung(s); taking the target without a hold")
            return
        rung = rungs[active_index]
        zone_width = _finite_number(rung.get("zone_width")) if isinstance(rung, dict) else None
        kind = rung.get("kind") if isinstance(rung, dict) else None
        if zone_width is None or zone_width < 0 or not isinstance(kind, str) or not kind.strip():
            self._ladder_unreadable(position, "rung", f"Ladder rung {active_index} of {position.symbol} is unreadable "
                                    f"({rung!r}); taking the target without a hold")
            return
        touch_at = _aware_time(price_at)
        if touch_at is None:
            self._ladder_unreadable(position, "price_at", f"The time of {position.symbol}'s price {last_price:.4f} is "
                                    f"unknown ({price_at!r}); taking the target without a hold")
            return
        symbol = str(meta.get("underlying") or position.symbol)
        sr_ctx = self.data.get_support_resistance(symbol, current_price=last_price, flip_frame=frame, mode="trading", timeframe_minutes=self.strategy.htf_minutes()) if self.data is not None else None
        # A level buffer that is not a finite number is no buffer, and one at
        # or below 0 never beats the price term: the zone and price terms set
        # the stop buffer then.
        level_buffer = safe_float(getattr(sr_ctx, "level_buffer", None), None, finite=True)
        stop_buffer = max(0.0 if level_buffer is None else level_buffer,
                          zone_width * LADDER_STOP_BUFFER_ZONE_FRAC, last_price * LADDER_STOP_BUFFER_PRICE_FRAC)
        touch_bar = touch_at.floor("1min")
        hold = _TouchHold(
            rung_index=active_index, rung_count=len(rungs), level=target, zone_width=zone_width, kind=kind,
            touch_price=last_price, touch_at=touch_at, touch_bar=touch_bar, stop_buffer=stop_buffer,
            guard=target - stop_buffer if long_side else target + stop_buffer,
            deadline=touch_bar + pd.Timedelta(minutes=1) + pd.Timedelta(seconds=timeout),
        )
        meta[LADDER_TOUCH_HOLD_KEY] = hold.to_meta()
        LOG.info(
            "LADDER_TOUCH symbol=%s side=%s rung=%d/%d level=%.4f touch=%.4f guard=%.4f bar=%s price_at=%s "
            "deadline=%s",
            position.symbol, position.side.value, active_index + 1, len(rungs), target, last_price, hold.guard,
            touch_bar.strftime("%H:%M"), touch_at.strftime("%H:%M:%S"), hold.deadline.strftime("%H:%M:%S"),
        )

    def _resolve_touch_hold(self, position: Position, meta: dict[str, Any], frame: pd.DataFrame | None,
                            last_price: float, now: pd.Timestamp) -> ExitDecision | None:
        """A decided exit again, else the guard, the verdict or the timeout
        for the hold in progress; None while it still holds (the hold stays
        in the metadata) or once it promoted the rung (the hold is gone)."""
        raw = meta.get(LADDER_TOUCH_HOLD_KEY)
        hold = _TouchHold.read(raw)
        rungs = _ladder_rungs(meta)
        if hold is None or rungs is None:
            # Read back from the position store: a hold it cannot read (or
            # whose rungs it cannot) is dropped whole, reported, and a price
            # at the target starts a new one or takes the target.
            meta.pop(LADDER_TOUCH_HOLD_KEY, None)
            if hold is None:
                message = f"Ladder touch hold of {position.symbol} is unreadable ({raw!r}); dropped"
            else:
                message = (f"Ladder rungs of {position.symbol} are unreadable ({meta.get('ladder_rungs')!r}); "
                           "touch hold dropped")
            self._ladder_unreadable(position, "touch_hold", message)
            return None
        if hold.exit_reason:
            # Decided on an earlier cycle and not booked yet (a failed or
            # deferred order): the same exit, whatever the price does now.
            return ExitDecision(hold.exit_reason, "risk")
        long_side = position.side == Side.LONG
        if (last_price <= hold.guard) if long_side else (last_price >= hold.guard):
            return self._touch_hold_exit(position, meta, hold, f"{TARGET_HOLD_GUARD}:{hold.guard:.4f}", "pending",
                                         None, last_price, now, guard_hit=True)
        bar = _delivered_touch_bar(frame, hold.touch_bar, now)
        if bar is not None:
            verdict, close_pos, close = _touch_bar_verdict(bar, position.side, hold.level)
            if verdict != "strong" or close is None:
                return self._touch_hold_exit(position, meta, hold, f"{TARGET_WEAK_CLOSE}:{hold.level:.4f}", verdict,
                                             close_pos, last_price, now, guard_hit=False)
            del meta[LADDER_TOUCH_HOLD_KEY]
            outcome = self._promote_touched_rung(position, meta, hold, rungs, close)
            self._log_touch_verdict(position, hold, verdict, close_pos, outcome, last_price, now, guard_hit=False)
            return None
        if now >= hold.deadline:
            return self._touch_hold_exit(position, meta, hold, f"{TARGET_HOLD_TIMEOUT}:{hold.level:.4f}", "missing",
                                         None, last_price, now, guard_hit=False)
        return None

    def _touch_hold_exit(self, position: Position, meta: dict[str, Any], hold: _TouchHold, reason: str, verdict: str,
                         close_pos: float | None, last_price: float, now: pd.Timestamp, *,
                         guard_hit: bool) -> ExitDecision:
        """Record the hold's exit on the hold itself and log its verdict."""
        meta[LADDER_TOUCH_HOLD_KEY] = replace(hold, exit_reason=reason).to_meta()
        self._log_touch_verdict(position, hold, verdict, close_pos, f"exit:{exit_reason_code(reason)}",
                                last_price, now, guard_hit=guard_hit)
        return ExitDecision(reason, "risk")

    @staticmethod
    def _promote_touched_rung(position: Position, meta: dict[str, Any], hold: _TouchHold, rungs: list,
                              bar_close: float) -> str:
        """A strong touch bar: the stop to the rung less the stop buffer, the
        target to the next rung the bar's close has not passed, or none past
        the last rung. Returns ``promoted`` or ``runner``. Every value is
        read before the first write, so the promotion applies whole."""
        long_side = position.side == Side.LONG
        candidate_stop = hold.level - hold.stop_buffer if long_side else hold.level + hold.stop_buffer
        prior_stop = float(position.stop_price)
        tighter = (candidate_stop > prior_stop) if long_side else (candidate_stop < prior_stop)
        next_index = _next_unpassed_rung(rungs, hold.rung_index, bar_close, position.side,
                                         max(bar_close * LADDER_NEXT_RUNG_GAP_FRAC, 1e-6))
        new_target = None if next_index is None else safe_float(rungs[next_index].get("price"), None, finite=True)
        prior_target = safe_float(position.target_price, None)
        if tighter:
            position.stop_price = float(candidate_stop)
            append_management_adjustment(meta, {"manager": "adaptive_ladder", "kind": "stop", "reason": "touch_promoted", "from": prior_stop, "to": float(candidate_stop), "source_level": hold.level})
        meta["ladder_defense_price"] = hold.level
        meta["ladder_defense_zone_width"] = hold.zone_width
        meta["ladder_defense_kind"] = hold.kind
        if next_index is not None and new_target is not None:
            position.target_price = new_target
            meta["ladder_active_index"] = int(next_index)
            meta["ladder_final_rung_cleared"] = False
            append_management_adjustment(meta, {"manager": "adaptive_ladder", "kind": "target", "reason": "next_rung", "from": prior_target, "to": new_target, "source_level": new_target})
            return "promoted"
        position.target_price = None
        meta["ladder_final_rung_cleared"] = True
        append_management_adjustment(meta, {"manager": "adaptive_ladder", "kind": "target", "reason": "final_rung_runner", "from": prior_target, "to": None, "source_level": hold.level})
        return "runner"

    @staticmethod
    def _log_touch_verdict(position: Position, hold: _TouchHold, verdict: str, close_pos: float | None,
                           outcome: str, last_price: float, now: pd.Timestamp, *, guard_hit: bool) -> None:
        target = safe_float(position.target_price, None)
        LOG.info(
            "LADDER_VERDICT symbol=%s side=%s rung=%d/%d level=%.4f touch=%.4f bar=%s verdict=%s close_pos=%s "
            "guard_hit=%s outcome=%s price=%.4f stop=%.4f target=%s held_s=%.0f",
            position.symbol, position.side.value, hold.rung_index + 1, hold.rung_count, hold.level,
            hold.touch_price, hold.touch_bar.strftime("%H:%M"), verdict,
            "-" if close_pos is None else f"{close_pos:.2f}", "yes" if guard_hit else "no", outcome,
            last_price, float(position.stop_price), "none" if target is None else f"{target:.4f}",
            (now - hold.touch_at).total_seconds(),
        )
