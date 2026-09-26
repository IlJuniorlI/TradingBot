# SPDX-License-Identifier: MIT
"""The shared exit families -- every ``shared_exit`` knob, for every strategy.

``SharedExitPolicy`` is built and owned by the ``PositionManager``, and it is
the ONLY reader of ``config.shared_exit``. Each cycle it decides, for one open
position, in this order:

1. the shared families, in the order of ``EXIT_FAMILY_GATES`` and each gated
   by its row there;
2. if none fired, the strategy's own hook ``strategy_exit_signal`` (the peer
   ladder defence, the microcap blowoff); it holds by default;
3. if that held too, the shared divergence scale-out, last so that any full
   exit wins.

No strategy can change this. Until 2026-09-24 the whole pipeline was
``BaseStrategy.position_exit_signal``, which any strategy could override and
which read every knob through a ``strategy_logic_default`` hook any strategy
could rewrite -- and the peer family did both. Its override ran the ladder and
the technical exits only, so ``risk.time_stop_minutes``, the chart / candle /
structure / S/R exits and the ORB grace were dead for all four peer strategies
whatever their YAML said, and its hook forced ``use_chart_pattern_exit``,
``use_structure_exit`` and ``use_sr_loss_exit`` off on top. Now
``BaseStrategy.__init_subclass__`` refuses a strategy that defines
``position_exit_signal`` or ``shared_exit_signal``, and every strategy's YAML
is honoured as written.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

import pandas as pd

from ..models import ExitDecision, Position
from ..bars import session_bucket_ends
from ..indicators import get_runtime_indicator_mode
from .. import sessions
from .helpers import CANDLE_PATTERN_WINDOW_BARS, _bars_have_range, _optional_float, _safe_float

if TYPE_CHECKING:
    from ..config import BotConfig
    from .strategy_base import BaseStrategy


# ---------------------------------------------------------------------------
# The gate table
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FamilyGates:
    """Which gates stand in front of one exit family.

    - ``r_gate``: open profit must reach ``shared_exit.discretionary_exit_min_r``.
    - ``hold_grace``: the position must be older than
      ``support_resistance.structure_exit_grace_minutes`` (the larger
      ``structure_exit_grace_minutes_pullback`` for a ``pullback`` entry).
    - ``orb_grace``: an ``orb`` entry must be older than
      ``support_resistance.orb_entry_exit_grace_minutes``.
    - ``pivot_guard``: ``structure_exit_min_post_entry_pivots`` pivots must
      have formed since entry.
    - ``post_entry_event``: the structure event the exit reads must have
      happened after entry.
    - ``bar_range``: every bar of the candle pattern window
      (``helpers.CANDLE_PATTERN_WINDOW_BARS``) must have traded a range
      (high > low) -- the window the entry candle veto checks too.
    - ``tape``: ``none``, ``strict`` (``ExitTape.weak``) or ``strict_or_loose``
      (``weak`` or ``weak_loose``).

    "Since entry" / "after entry" is judged by when a bar CLOSED
    (``bar_closed_after``), not by its label.
    """

    r_gate: bool
    hold_grace: bool
    orb_grace: bool
    pivot_guard: bool
    post_entry_event: bool
    bar_range: bool
    tape: str


# One row per shared family. Until 2026-09-24 these gates were scattered
# through a 240-line method, and three rows were wrong:
# - sr_loss sat behind the R gate, but it only fires with price through a
#   level on the ADVERSE side of entry -- R < 0 by construction -- so it could
#   never fire at any positive discretionary_exit_min_r. It is a loss-side
#   thesis-invalidation exit and is exempt; the adverse-side and
#   underlying-entry guards stay. (Reachable now, it ships off: see
#   use_sr_loss_exit in config.py.)
# - structure_choch read a CHoCH from the last structure_event_lookback_bars
#   bars without asking when it happened: a LONG opened within six bars
#   after a 1m choch_down exited on its first weak-tape cycle. The CHoCH must
#   now have happened after entry, and that is its only gate besides the
#   tape. It is an ungated structural stop-tightener -- it fires below about
#   -0.4R on roughly 0.5-1.3% of positions -- which the 2026-09-24 study
#   measured neutral on 5m structure and slightly negative on 1m structure.
#   No candidate guard (an R floor, post-entry pivots, the hold or ORB
#   grace) had a measurable effect, so it has none.
# - candle_pattern judged a zero-range last bar. TA-Lib reads a single-print
#   bar as a doji / white candle, so thin tape builds TRISTAR, DOJISTAR,
#   HARAMI, HIKKAKE or GAPSIDESIDEWHITE out of nothing; the tape used to read
#   such a bar's close position as 0.5, which failed the close-position vote
#   and hid those matches. Now the close position abstains there (see
#   ExitTape), so the family itself holds on a bar with no range.
# The rows are in evaluation order; the first family that fires decides
# (structure_choch and structure_bias are one stage, the CHoCH read first).
EXIT_FAMILY_GATES: Mapping[str, FamilyGates] = MappingProxyType({
    "time_stop": FamilyGates(r_gate=False, hold_grace=False, orb_grace=False, pivot_guard=False, post_entry_event=False, bar_range=False, tape="none"),
    "chart_pattern": FamilyGates(r_gate=False, hold_grace=False, orb_grace=True, pivot_guard=False, post_entry_event=False, bar_range=False, tape="strict_or_loose"),
    "candle_pattern": FamilyGates(r_gate=False, hold_grace=False, orb_grace=False, pivot_guard=False, post_entry_event=False, bar_range=True, tape="strict"),
    "structure_choch": FamilyGates(r_gate=False, hold_grace=False, orb_grace=False, pivot_guard=False, post_entry_event=True, bar_range=False, tape="strict"),
    "structure_bias": FamilyGates(r_gate=True, hold_grace=True, orb_grace=True, pivot_guard=True, post_entry_event=True, bar_range=False, tape="strict"),
    "technical": FamilyGates(r_gate=True, hold_grace=False, orb_grace=False, pivot_guard=False, post_entry_event=False, bar_range=False, tape="strict"),
    "sr_loss": FamilyGates(r_gate=False, hold_grace=False, orb_grace=False, pivot_guard=False, post_entry_event=False, bar_range=False, tape="strict"),
})

# Every exit reads the canonical 1m bars of the position's instrument.
_FRAME_BAR_MINUTES = 1


# ---------------------------------------------------------------------------
# The tape
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TapeRules:
    """The ``shared_exit.confirm_with_*`` switches and close-position bounds."""

    ema9: bool
    ema20: bool
    vwap: bool
    close_position: bool
    bullish_close_position_max: float
    bearish_close_position_min: float
    bullish_close_position_loose_max: float
    bearish_close_position_loose_min: float


def _first_session_bar(frame: pd.DataFrame) -> bool:
    """Is the last bar the first bar of the indicator session?

    With the session-reset indicators on, ``ema9`` / ``ema20`` restart on the
    first session bar (09:30, or 07:00 for an extended-window preset) and
    EQUAL its close. ``close < ema9`` is then false in both directions, so
    until 2026-09-24 that bar vetoed every tape-confirmed exit, long and
    short alike. The session ``vwap`` restarts there too and equals the bar's
    own typical price, (high + low + close) / 3: a close below it says only
    where the close sits in its own bar, which the close position already
    votes on.
    """
    if "ema9_rth" not in frame.columns:
        return False
    last = _optional_float(frame["ema9_rth"].iloc[-1])
    if last is None:
        return False
    if len(frame) < 2:
        return True
    if _optional_float(frame["ema9_rth"].iloc[-2]) is None:
        return True
    return pd.Timestamp(frame.index[-2]).date() != pd.Timestamp(frame.index[-1]).date()


@dataclass(frozen=True, slots=True)
class ExitTape:
    """The last bar's tape, read once per position per cycle.

    A reference with no value -- a missing column, a NaN -- is None and
    abstains from every vote; so does ``close_pos`` on a zero-range bar, and
    ``vwap`` on the first bar of the indicator session. Until 2026-09-24 a
    NaN reference fell back to the close (``close < close`` is a veto) and a
    zero-range bar read 0.5 (which fails both 0.46 and 0.54), so either one
    silently vetoed every tape-confirmed exit.
    """

    close: float
    ema9: float | None
    ema20: float | None
    vwap: float | None
    close_pos: float | None
    rules: TapeRules

    @classmethod
    def read(cls, frame: pd.DataFrame, rules: TapeRules) -> ExitTape | None:
        """None when the last close is unreadable: nothing can be judged."""
        last = frame.iloc[-1]
        close = _optional_float(last.get("close"))
        if close is None:
            return None
        # The session-reset references restart on the first session bar (see
        # _first_session_bar): the EMAs read their all-hours values there and
        # the VWAP, which that bar alone defines, abstains.
        first_bar = get_runtime_indicator_mode() and _first_session_bar(frame)
        ema9_col, ema20_col = ("ema9_all", "ema20_all") if first_bar else ("ema9", "ema20")
        high, low = _optional_float(last.get("high")), _optional_float(last.get("low"))
        close_pos = None
        if high is not None and low is not None and high > low:
            close_pos = (close - low) / (high - low)
        return cls(
            close=close,
            ema9=_optional_float(last.get(ema9_col)),
            ema20=_optional_float(last.get(ema20_col)),
            vwap=None if first_bar else _optional_float(last.get("vwap")),
            close_pos=close_pos,
            rules=rules,
        )

    def refs(self) -> tuple[float, ...]:
        """The EMA / VWAP references that have a value."""
        return tuple(ref for ref in (self.ema9, self.ema20, self.vwap) if ref is not None)

    def _below(self, ref: float, direction: str) -> bool:
        """Has the close gone through ``ref`` against a ``direction`` position?"""
        return self.close < ref if direction == "bullish" else self.close > ref

    def weak(self, direction: str) -> bool:
        """Does the tape confirm a ``direction`` position is weakening?

        Every enabled term with a value votes, and the votes are ANDed. No
        enabled term: True (confirmation is off). Every enabled term
        abstaining: False -- an unreadable tape confirms nothing.
        """
        rules = self.rules
        bullish = direction == "bullish"
        enabled = 0
        votes: list[bool] = []
        for on, ref in ((rules.ema9, self.ema9), (rules.ema20, self.ema20), (rules.vwap, self.vwap)):
            if not on:
                continue
            enabled += 1
            if ref is not None:
                votes.append(self._below(ref, direction))
        if rules.close_position:
            enabled += 1
            if self.close_pos is not None:
                votes.append(
                    self.close_pos <= rules.bullish_close_position_max if bullish
                    else self.close_pos >= rules.bearish_close_position_min
                )
        if enabled == 0:
            return True
        return bool(votes) and all(votes)

    def weak_loose(self, direction: str) -> bool:
        """The chart-pattern exit's looser tape: a close deep in the bar's
        adverse end, plus the ema9 and vwap terms (no ema20 term). Needs a
        close position -- the depth of the close IS this path's signal."""
        rules = self.rules
        if not rules.close_position or self.close_pos is None:
            return False
        if direction == "bullish":
            if self.close_pos > rules.bullish_close_position_loose_max:
                return False
        elif self.close_pos < rules.bearish_close_position_loose_min:
            return False
        for on, ref in ((rules.ema9, self.ema9), (rules.vwap, self.vwap)):
            if on and ref is not None and not self._below(ref, direction):
                return False
        return True


def bar_closed_after(label: Any, moment: Any, bar_minutes: int) -> bool:
    """Did the ``bar_minutes`` bar labelled ``label`` CLOSE after ``moment``?

    Bars are labelled at their START (``bars.resample_bars``) and the
    frames hold completed bars, so the entry never saw a bar that closed
    after its fill, even though that bar's label is earlier than the fill.
    Structure events, pivots and divergence pivots are judged against the
    entry this way (2026-09-24): compared by label, a CHoCH that crossed on
    the bar the entry filled in read as pre-entry for as long as price
    stayed through the level -- blind to the first post-entry breakdown, the
    most common reversal. A resampled frame's last bucket can be partial, so
    an event on the bucket the entry filled in counts as post-entry even if
    part of that bucket traded before the fill: the entry never saw its
    close. None (no event) is never after anything.
    """
    if label is None:
        return False
    end = session_bucket_ends(pd.DatetimeIndex([pd.Timestamp(label)]), max(1, int(bar_minutes)))[0]
    return end > pd.Timestamp(moment)


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

class SharedExitPolicy:
    def __init__(self, config: BotConfig, strategy: BaseStrategy) -> None:
        self.config = config
        self.strategy = strategy

    # -- config: the only reads of config.shared_exit in the code base ------
    #
    # Read live on every decision, and None-aware: a configured 0 means 0.
    # The old accessors coerced with `or default`, so a divergence partial
    # fraction of 0 closed half the position and a minimum age of 0 was 1.

    def _on(self, key: str) -> bool:
        return bool(getattr(self.config.shared_exit, key))

    def _number(self, key: str) -> float | None:
        return _optional_float(getattr(self.config.shared_exit, key))

    def tape_rules(self) -> TapeRules:
        cfg = self.config.shared_exit
        return TapeRules(
            ema9=bool(cfg.confirm_with_ema9),
            ema20=bool(cfg.confirm_with_ema20),
            vwap=bool(cfg.confirm_with_vwap),
            close_position=bool(cfg.confirm_with_close_position),
            bullish_close_position_max=float(cfg.bullish_close_position_max),
            bearish_close_position_min=float(cfg.bearish_close_position_min),
            bullish_close_position_loose_max=float(cfg.bullish_close_position_loose_max),
            bearish_close_position_loose_min=float(cfg.bearish_close_position_loose_min),
        )

    def _sr(self, key: str, default: Any) -> Any:
        return self.strategy._support_resistance_setting(key, default)

    # -- the decision --------------------------------------------------------

    def decide(self, position: Position, bars: dict[str, pd.DataFrame], data=None) -> ExitDecision | None:
        """The shared exit families, then the strategy's hook, then the
        divergence scale-out. Runs whether or not the position had a price
        this cycle; the frame is the canonical 1m bars of the instrument the
        position moves with (an option's underlying)."""
        symbol = str(position.metadata.get("underlying") or position.symbol)
        frame = bars.get(symbol)
        decision = self._time_stop(position, frame)
        if decision is not None:
            return decision
        if frame is None or frame.empty:
            return None
        tape = ExitTape.read(frame, self.tape_rules())
        if tape is None:
            return None
        direction = self.strategy._direction_token(position)
        for family in (self._chart_pattern_exit, self._candle_pattern_exit, self._structure_exit,
                       self._technical_exit, self._sr_loss_exit):
            decision = family(position, frame, tape, direction, symbol, data)
            if decision is not None:
                return decision
        decision = self.strategy.strategy_exit_signal(position, bars, tape, data)
        if decision is not None:
            return decision
        return self._divergence_partial_exit(position, frame, tape, direction)

    # -- gates ---------------------------------------------------------------

    @staticmethod
    def _hold_minutes(position: Position) -> float:
        try:
            return max(0.0, (sessions.now_et() - position.entry_time).total_seconds() / 60.0)
        except Exception:
            return 0.0

    @staticmethod
    def _entry_family(position: Position) -> str:
        # Stamped at emit time from the proposal's style family, so the ORB
        # grace covers every opening-range entry (opening_range_breakout,
        # microcap_gap_orb, top_tier's orb regime) and the pullback grace
        # every pullback entry. The graces used to key on orb_window_entry /
        # regime == 'pullback', which only top_tier stamped (2026-09-24).
        return str(position.metadata.get("entry_style_family") or "")

    def _structure_bar_minutes(self) -> int:
        """Bar length of the LTF structure the structure families read:
        ``_structure_context`` analyses the canonical 1m frame, resampled to
        ``support_resistance.structure_ltf_timeframe_minutes`` when that is
        above 1 (top_tier_adaptive and small_cap_squeeze ship 5)."""
        return max(_FRAME_BAR_MINUTES, int(self._sr("structure_ltf_timeframe_minutes", 0) or 0))

    def _structure_grace_minutes(self, position: Position) -> float:
        grace = float(self._sr("structure_exit_grace_minutes", 10) or 0)
        if self._entry_family(position) == "pullback":
            grace = max(grace, float(self._sr("structure_exit_grace_minutes_pullback", 15) or 0))
        return grace

    def _orb_grace_active(self, position: Position) -> bool:
        grace = float(self._sr("orb_entry_exit_grace_minutes", 20) or 0)
        return self._entry_family(position) == "orb" and grace > 0 and self._hold_minutes(position) < grace

    def discretionary_exit_allowed(self, position: Position, close: float) -> bool:
        """The R gate. Below ``shared_exit.discretionary_exit_min_r`` of open
        profit the protective stop governs the trade and the gated families
        stay silent.

        The bias-based structure exit and the technical exits are pattern
        reads on the tape -- a pivot label, a trendline touch, an
        anchored-VWAP cross -- that never ask whether the trade has earned
        anything, so on a trade still near entry they act as an arbitrary
        tightened stop. Measured over 2026-05-12..29 they closed 19 of 48
        top_tier_adaptive trades at a median MFE of 0.09-0.29R for -$831, and
        in the widest-stop bucket 0 of 22 trades ever reached their stop
        because one of these got there first.

        The S/R break is exempt (see ``EXIT_FAMILY_GATES``): a gate that
        demands profit can never pass an exit that only fires once price is
        through a level beyond entry. So is the CHoCH exit, which fires below
        about -0.4R and would never pass it either. An unknown R (no initial
        stop, an option with no mark yet) is no opinion: open.
        """
        min_r = self._number("discretionary_exit_min_r")
        if min_r is None or min_r <= 0:
            return True
        current_r = self.strategy._position_r_multiple(position, close)
        return current_r is None or current_r >= min_r

    def _gates_open(self, family: str, position: Position, tape: ExitTape, ms_ctx: Any = None,
                    frame: pd.DataFrame | None = None) -> bool:
        gates = EXIT_FAMILY_GATES[family]
        if gates.bar_range and not _bars_have_range(frame, CANDLE_PATTERN_WINDOW_BARS):
            return False
        if gates.orb_grace and self._orb_grace_active(position):
            return False
        if gates.hold_grace and self._hold_minutes(position) < self._structure_grace_minutes(position):
            return False
        if gates.pivot_guard:
            bar_minutes = self._structure_bar_minutes()
            post_entry_pivots = sum(
                1 for ts in ms_ctx.pivot_times if bar_closed_after(ts, position.entry_time, bar_minutes)
            )
            if post_entry_pivots < int(self._sr("structure_exit_min_post_entry_pivots", 2) or 0):
                return False
        if gates.r_gate and not self.discretionary_exit_allowed(position, tape.close):
            return False
        return True

    def _event_ok(self, family: str, event_ts: Any, position: Position) -> bool:
        """The structure event (on the LTF structure frame) closed after entry."""
        if not EXIT_FAMILY_GATES[family].post_entry_event:
            return True
        return bar_closed_after(event_ts, position.entry_time, self._structure_bar_minutes())

    @staticmethod
    def _tape_ok(family: str, tape: ExitTape, direction: str) -> bool:
        mode = EXIT_FAMILY_GATES[family].tape
        if mode == "none":
            return True
        if mode == "strict_or_loose":
            return tape.weak(direction) or tape.weak_loose(direction)
        return tape.weak(direction)

    # -- the families --------------------------------------------------------

    def _time_stop(self, position: Position, frame: pd.DataFrame | None) -> ExitDecision | None:
        """Scratch a trade held ``risk.time_stop_minutes`` that has gone
        nowhere (``|return| < risk.time_stop_min_return_pct``) to free the
        slot. 2026-04-17 META held 223 min for +$0.16.

        "Gone nowhere" is judged on the frame's own instrument: for an
        option, the underlying since entry -- its premium against the
        underlying's close was never under the threshold.
        """
        time_stop_minutes = int(self.config.risk.time_stop_minutes or 0)
        if time_stop_minutes <= 0:
            return None
        held_minutes = self._hold_minutes(position)
        if held_minutes < time_stop_minutes:
            return None
        entry = self.strategy._underlying_entry_price(position) or 0.0
        if entry <= 0 or frame is None or frame.empty or "close" not in frame.columns:
            return None
        last_close = _optional_float(frame["close"].iloc[-1])
        if last_close is None:
            return None
        min_return_pct = float(self.config.risk.time_stop_min_return_pct or 0.0)
        if abs((last_close - entry) / entry) < min_return_pct:
            return ExitDecision(f"time_stop:{int(held_minutes)}m", "time_stop")
        return None

    def _chart_pattern_exit(self, position, frame, tape, direction, symbol, data) -> ExitDecision | None:
        if not (self._on("use_chart_pattern_exit") and bool(self.strategy._chart_pattern_setting("enabled", True))):
            return None
        if len(frame) < max(12, int(self.strategy._chart_pattern_setting("lookback_bars", 32)) // 2):
            return None
        if not self._gates_open("chart_pattern", position, tape):
            return None
        ctx = self.strategy._chart_context(frame)
        if direction == "bullish":
            opposing_reversal = sorted(ctx.matched_bearish_reversal)
            opposing_cont = sorted(ctx.matched_bearish_continuation)
            strong_opposing = bool(opposing_reversal) or (bool(opposing_cont) and ctx.bias_score <= -0.65)
        else:
            opposing_reversal = sorted(ctx.matched_bullish_reversal)
            opposing_cont = sorted(ctx.matched_bullish_continuation)
            strong_opposing = bool(opposing_reversal) or (bool(opposing_cont) and ctx.bias_score >= 0.65)
        if strong_opposing and self._tape_ok("chart_pattern", tape, direction):
            opposing = opposing_reversal + [p for p in opposing_cont if p not in opposing_reversal]
            return ExitDecision(f"chart_pattern_exit:{'+'.join(opposing)}", "chart_pattern")
        return None

    def _candle_pattern_exit(self, position, frame, tape, direction, symbol, data) -> ExitDecision | None:
        """An opposing candle cluster at or above
        ``candles.opposing_net_score_threshold`` with the tape confirming.
        Holds when a bar of the pattern window traded no range (the
        ``bar_range`` gate): a single print is not a candle, whatever TA-Lib
        names it, and the multi-bar patterns read the whole window. The candle context
        is cached per frame, so this is free when the entry side already
        read it this cycle."""
        if not self._on("use_candle_pattern_exit"):
            return None
        if not self._gates_open("candle_pattern", position, tape, frame=frame):
            return None
        candle_ctx = self.strategy._candle_context(frame)
        threshold = float(self.strategy._candles_setting("opposing_net_score_threshold", 0.70))
        side = "bearish" if direction == "bullish" else "bullish"
        opposing_net = float(candle_ctx.get(f"{side}_candle_net_score", 0.0) or 0.0)
        opposing_matches = list(candle_ctx.get(f"matched_{side}_candles", []) or [])
        if opposing_net >= threshold and opposing_matches and self._tape_ok("candle_pattern", tape, direction):
            return ExitDecision(f"candle_pattern_exit:{'+'.join(sorted(opposing_matches)[:3])}", "candle_pattern")
        return None

    def _structure_exit(self, position, frame, tape, direction, symbol, data) -> ExitDecision | None:
        """The CHoCH exit, then the bias-flip exit.

        The CHoCH exit needs only a CHoCH that happened after entry and a
        confirming tape: no grace, no R gate, no pivot guard (see
        ``EXIT_FAMILY_GATES``).

        The bias flip waits out the grace window (longer for a pullback
        entry, whose design is to buy into LTF chop: AMD 2026-05-14 14:36 was
        cut at 10.2 min on an EQL and then ran past its target) and
        ``structure_exit_min_post_entry_pivots`` new pivots (2026-04-17: 1W /
        12T on structure exits, net -$354), clears the R gate, and with
        ``structure_exit_require_bos_confirmation`` needs a post-entry BoS in
        the exit's direction -- an EQL alone is a pivot label, a BoS is price
        actually breaking a prior swing.
        """
        if not (self._on("use_structure_exit") and bool(self._sr("structure_enabled", True))):
            return None
        ms_ctx = self.strategy._structure_context(frame, "ltf")
        if direction == "bullish":
            choch, choch_age, choch_ts, choch_label = ms_ctx.choch_down, ms_ctx.choch_down_age_bars, ms_ctx.choch_down_ts, "down"
            bos, bos_age, bos_ts = ms_ctx.bos_down, ms_ctx.bos_down_age_bars, ms_ctx.bos_down_ts
            against, pivot_label = "bearish", ms_ctx.last_low_label
        else:
            choch, choch_age, choch_ts, choch_label = ms_ctx.choch_up, ms_ctx.choch_up_age_bars, ms_ctx.choch_up_ts, "up"
            bos, bos_age, bos_ts = ms_ctx.bos_up, ms_ctx.bos_up_age_bars, ms_ctx.bos_up_ts
            against, pivot_label = "bullish", ms_ctx.last_high_label
        if (
            bool(choch)
            and self.strategy._structure_event_recent(choch_age)
            and self._event_ok("structure_choch", choch_ts, position)
            and self._gates_open("structure_choch", position, tape)
            and self._tape_ok("structure_choch", tape, direction)
        ):
            return ExitDecision(f"structure_choch_{choch_label}_exit:{choch_age}", "structure_choch")
        if ms_ctx.bias != against:
            return None
        if bool(self._sr("structure_exit_require_bos_confirmation", True)) and not (
            bool(bos)
            and self.strategy._structure_event_recent(bos_age)
            and self._event_ok("structure_bias", bos_ts, position)
        ):
            return None
        if self._gates_open("structure_bias", position, tape, ms_ctx) and self._tape_ok("structure_bias", tape, direction):
            return ExitDecision(f"structure_{against}_exit:{pivot_label or 'na'}", "structure_bias")
        return None

    def _technical_exit(self, position, frame, tape, direction, symbol, data) -> ExitDecision | None:
        """Trendline, channel, Bollinger and anchored-VWAP exits, in that
        order, behind one R gate and one tape read."""
        if not self._on("use_technical_exit"):
            return None
        if not self._gates_open("technical", position, tape):
            return None
        if not bool(self.strategy._technical_level_setting("enabled", True)):
            return None
        close = tape.close
        tech_ctx = self.strategy._technical_context(frame)
        atr = self.strategy._frame_atr14(frame, close)
        buffer = max(atr * float(self.strategy._technical_level_setting("trendline_breakout_buffer_atr_mult", 0.65)), close * 0.0010)
        weak_tape = self._tape_ok("technical", tape, direction)
        channel_ctx = tech_ctx.channel
        # A single 1m close through the anchored-VWAP level is ordinary
        # continuation noise in a trending stock: AVGO 2026-05-29 12:09 LONG
        # exited at 439.50 on one dip and closed the day at 446.67. With the
        # two-bar confirm the PRIOR bar must have closed through it too.
        two_bar = self._on("anchored_vwap_exit_require_two_bar_confirm")
        prior_close = _safe_float(frame["close"].iloc[-2], close) if len(frame) >= 2 else None
        if direction == "bullish":
            if bool(tech_ctx.trendline_break_down) and self._on("use_trendline_break") and weak_tape:
                support_value = _safe_float(getattr(tech_ctx.support_trendline, "current_value", None), close)
                return ExitDecision(f"trendline_break_exit:{support_value:.4f}", "technical")
            if bool(getattr(channel_ctx, "valid", False)) and getattr(channel_ctx, "lower", None) is not None and self._on("use_channel_break"):
                lower = float(channel_ctx.lower)
                if close <= lower - buffer and weak_tape:
                    return ExitDecision(f"channel_breakdown_exit:{lower:.4f}", "technical")
            if bool(tech_ctx.bollinger_upper_reject) and self._on("use_bollinger_reject") and weak_tape:
                upper = _safe_float(tech_ctx.bollinger_upper, close)
                return ExitDecision(f"bollinger_upper_reject_exit:{upper:.4f}", "technical")
            if self._on("use_anchored_vwap_loss"):
                avwap_floor = max(_safe_float(tech_ctx.anchored_vwap_open, 0.0), _safe_float(tech_ctx.anchored_vwap_bullish_impulse, 0.0))
                # Armed only once the underlying has traded at or above
                # floor + buffer since entry: a LONG filled below the floor
                # otherwise "lost" it on the next tick (AMZN 2026-04-24 10:59,
                # out in 13 s for -$3.99).
                highest_price, _lowest = self.strategy._underlying_extremes(position)
                avwap_armed = avwap_floor > 0 and highest_price is not None and highest_price >= avwap_floor + buffer
                prior_confirms = not two_bar or prior_close is None or prior_close < avwap_floor - buffer
                if avwap_armed and close < avwap_floor - buffer and weak_tape and prior_confirms:
                    return ExitDecision(f"anchored_vwap_loss_exit:{avwap_floor:.4f}", "technical")
            return None
        if bool(tech_ctx.trendline_break_up) and self._on("use_trendline_break") and weak_tape:
            resistance_value = _safe_float(getattr(tech_ctx.resistance_trendline, "current_value", None), close)
            return ExitDecision(f"trendline_break_exit:{resistance_value:.4f}", "technical")
        if bool(getattr(channel_ctx, "valid", False)) and getattr(channel_ctx, "upper", None) is not None and self._on("use_channel_break"):
            upper = float(channel_ctx.upper)
            if close >= upper + buffer and weak_tape:
                return ExitDecision(f"channel_breakout_exit:{upper:.4f}", "technical")
        if bool(tech_ctx.bollinger_lower_reject) and self._on("use_bollinger_reject") and weak_tape:
            lower = _safe_float(tech_ctx.bollinger_lower, close)
            return ExitDecision(f"bollinger_lower_reject_exit:{lower:.4f}", "technical")
        if self._on("use_anchored_vwap_loss"):
            ceilings = [px for px in (_safe_float(tech_ctx.anchored_vwap_open, 0.0), _safe_float(tech_ctx.anchored_vwap_bearish_impulse, 0.0)) if px > 0]
            avwap_ceiling = min(ceilings) if ceilings else 0.0
            # Mirror of the LONG armed guard: META 2026-04-24 09:35 SHORT was
            # filled under the ceiling and out in 55 s for -$68.86.
            _highest, lowest_price = self.strategy._underlying_extremes(position)
            avwap_armed = avwap_ceiling > 0 and lowest_price is not None and lowest_price <= avwap_ceiling - buffer
            prior_confirms = not two_bar or prior_close is None or prior_close > avwap_ceiling + buffer
            if avwap_armed and close > avwap_ceiling + buffer and weak_tape and prior_confirms:
                return ExitDecision(f"anchored_vwap_reclaim_exit:{avwap_ceiling:.4f}", "technical")
        return None

    def _sr_loss_exit(self, position, frame, tape, direction, symbol, data) -> ExitDecision | None:
        """A confirmed break of the level the trade was leaning on.

        Only a CONFIRMED break event from the S/R engine counts
        (``broken_support`` / ``broken_resistance``), not proximity, and the
        level must sit beyond ENTRY on the adverse side: a break is
        session-scoped and may predate the entry, which would otherwise exit
        on the first cycle. Both sides of that comparison are on the
        underlying's scale -- an option's premium entry would compare a $500
        level to a $1.20 debit -- and with no recorded underlying entry
        there is no opinion.
        """
        if not (self._on("use_sr_loss_exit") and bool(self._sr("enabled", True))):
            return None
        if not self._gates_open("sr_loss", position, tape):
            return None
        entry_price = self.strategy._underlying_entry_price(position)
        if entry_price is None:
            return None
        sr_ctx = self.strategy._sr_context(symbol, frame, data)
        level_buffer = float(sr_ctx.level_buffer or 0.0)
        close = tape.close
        if direction == "bullish":
            level = sr_ctx.broken_support
            if level is not None:
                price = float(level.price)
                if price < entry_price and close <= price - level_buffer and self._tape_ok("sr_loss", tape, direction):
                    return ExitDecision(f"support_break_exit:{price:.4f}", "sr_loss")
            return None
        level = sr_ctx.broken_resistance
        if level is not None:
            price = float(level.price)
            if price > entry_price and close >= price + level_buffer and self._tape_ok("sr_loss", tape, direction):
                return ExitDecision(f"resistance_break_exit:{price:.4f}", "sr_loss")
        return None

    def _divergence_partial_exit(self, position, frame, tape, direction) -> ExitDecision | None:
        """Scale out when a counter-direction REGULAR divergence forms.

        Hidden divergence is continuation context and never an exit, and a
        same-side divergence is confluence. The counter side is the
        position's MARKET direction (``_direction_token``), not its order
        side: a bull put spread is sold SHORT but is a bullish trade, so it
        watches for a bearish divergence.

        The divergence must be forming on the HELD position: its latest
        pivot (``pivot_b``) must have closed after the entry. One whose
        pivot predates the entry was on the chart when the trade was taken,
        and inside ``divergence_max_age_bars`` it used to scale the position
        out on its first in-profit cycle. The first indicator that passes
        every check inside ``[divergence_exit_min_age_bars,
        technical_levels.divergence_max_age_bars]`` is taken -- a too-fresh
        RSI divergence no longer hides a valid OBV one.

        One scale-out per divergence PIVOT (2026-09-24): RSI and OBV are
        measured on the same two price pivots, so both diverging at one
        ``pivot_b`` is one price event read twice, and keying the one-shot
        marker on (pivot, indicator) closed half the position and then half
        the rest -- 75% at the default fraction -- on it. A pivot already
        recorded in ``metadata['divergence_partial_exits']`` is skipped,
        whichever indicator acted on it, so it scales out once, not on every
        cycle it stays in the window.
        """
        if not self._on("use_divergence_exit_signal"):
            return None
        fraction = self._number("divergence_exit_partial_frac")
        if fraction is None or fraction <= 0:
            return None
        if self._on("divergence_exit_require_in_profit"):
            current_r = self.strategy._position_r_multiple(position, tape.close)
            if current_r is None or current_r <= 0:
                return None
        min_age = int(self._number("divergence_exit_min_age_bars") or 0)
        max_age = _optional_float(self.strategy._technical_level_setting("divergence_max_age_bars", None))
        counter = "bearish" if direction == "bullish" else "bullish"
        consumed = {
            str(marker.get("pivot_b_ts"))
            for marker in (position.metadata.get("divergence_partial_exits") or [])
            if isinstance(marker, dict)
        }
        tech_ctx = self.strategy._technical_context(frame)
        for indicator in ("rsi", "obv"):
            match = getattr(tech_ctx, f"{counter}_{indicator}_divergence")
            if match is None:
                continue
            if not bar_closed_after(match.pivot_b_ts, position.entry_time, _FRAME_BAR_MINUTES):
                continue
            age = int(match.age_bars)
            if age < min_age or (max_age is not None and age > max_age):
                continue
            pivot_b_ts = pd.Timestamp(match.pivot_b_ts).isoformat()
            if pivot_b_ts in consumed:
                continue
            return ExitDecision(
                f"divergence_partial_exit:{counter}_{indicator}_age{age}b",
                "divergence_partial",
                fraction=min(1.0, fraction),
                marker={"pivot_b_ts": pivot_b_ts, "indicator": indicator},
            )
        return None


def partial_exit_qty(qty: int, fraction: float) -> int:
    """Units a ``fraction`` exit closes out of ``qty``: all of it at 1.0,
    otherwise the floor -- a 1-lot option or a 1-share position cannot scale
    out, and rounding up would turn a half-exit of 3 into a two-thirds exit.
    The product is rounded to 9 places before the floor: a float product that
    is an integer in exact arithmetic can land just below it (100 x 0.29 =
    28.999999999999996, 90 x 0.7 = 62.99999999999999) and floored a unit
    short."""
    if fraction >= 1.0:
        return int(qty)
    return int(math.floor(round(int(qty) * float(fraction), 9)))
