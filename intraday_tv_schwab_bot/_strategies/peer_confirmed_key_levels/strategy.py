# SPDX-License-Identifier: MIT
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, ClassVar

import pandas as pd

from ...htf_levels import HTFContext
from ...indicators import bar_posture, htf_ema_spans, last_bar_atr
from ...numeric import safe_float
from ...reasons import insufficient_bars_reason, side_prefixed_reasons
from ..shared_entry import EntryProposal, entry_spread_guard, entry_spread_limits
from ..shared_exit import ExitTape
from ..strategy_base import BaseStrategy
from ... import sessions
from ...bars import (
    bar_close_position,
    bar_closed_after,
    bar_wick_fractions,
    completed_bucket_mask,
    session_bucket_ends,
    session_open_price,
)
from ...models import Candidate, ExitDecision, Position, Side, Signal
from ...support_resistance import zone_flip_confirmed
from ...symbols import normalize_symbol_list

LOG = logging.getLogger(__name__)


def _score_threshold(value: Any, default: float, *, minimum: float = 0.0) -> float:
    """Threshold for a CONTINUOUS score, kept as configured.

    The LTF trigger score moves in 0.5 / 0.75 steps plus candle bonuses and
    the total score is continuous, so ``_discrete_score_threshold``'s ceil
    turned ``min_ltf_score: 2.5`` into 3 and ``min_total_score: 5.5`` into 6:
    setups the configured threshold passes were rejected. Falls back to
    ``default`` on parse failure; floors at ``minimum``.
    """
    try:
        raw = float(value)
    except (TypeError, ValueError):
        raw = float(default)
    if math.isnan(raw):
        raw = float(default)
    return max(float(minimum), raw)


def _discrete_score_threshold(
    value: Any,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    """Coerce ``value`` to an integer threshold, falling back to
    ``default`` on parse failure and clamping to ``[minimum, maximum]``
    (maximum optional).

    For INTEGER-valued counts only (peer agreement). A fractional setting is
    rounded UP, which is right for a count -- "at least 2.5 peers" means 3 --
    and wrong for a continuous score; use ``_score_threshold`` for those.
    """
    try:
        raw = float(value)
    except (TypeError, ValueError):
        raw = float(default)
    if math.isnan(raw):
        raw = float(default)
    threshold = int(math.ceil(raw))
    threshold = max(int(minimum), threshold)
    if maximum is not None:
        threshold = min(int(maximum), threshold)
    return threshold


def _gate_snapshot(
    name: str,
    *,
    passed: bool,
    current: Any = None,
    required: Any = None,
    op: str = ">=",
    note: str | None = None,
) -> dict[str, Any]:
    """Build a gate-decision record for structured logging."""
    payload: dict[str, Any] = {
        "name": str(name),
        "pass": bool(passed),
        "op": str(op),
    }
    if current is not None:
        payload["current"] = current
    if required is not None:
        payload["required"] = required
    if note:
        payload["note"] = str(note)
    return payload


# ---------------------------------------------------------------------------
# The load check of the family's params (``normalize_params``, 2026-10-13)
# ---------------------------------------------------------------------------

# The trigger frame lengths the family reads: whole minutes that divide 30,
# so every bucket starts on the session grid the broker's own bars use
# (``bars._on_the_open_grid``). A 7-minute frame loaded and traded on
# buckets that matched no broker bar and were cut short at every session
# boundary.
LTF_MINUTES_CHOICES = (1, 2, 3, 5, 6, 10, 15, 30)

# key_levels' base trigger at its most (``_ltf_score``): a CHoCH 1, a volume
# spike 1, the strongest candle tier 1.0 and a real sweep 1. A
# min_ltf_score above it could never pass.
LTF_BASE_SCORE_MAX = 4.0

# How late, on the step frame's data clock, the entry pass may first judge a
# completed trigger bucket (``entry_signals``): one 1m step past its end. On
# the normal loop the pass that first sees a bucket complete runs a moment
# after its last minute's bar arrives (0 late); a thin name with no print in
# the bucket's last minute completes it with its next minute's bar (1 late).
# A bucket first seen later -- a cooldown, an open position, a restart or a
# stream-inactive spell kept the strategy from running while the step frame
# moved on -- is skipped (``ltf_bucket_missed``) rather than judged on the
# current close against evidence that closed minutes ago.
LTF_BUCKET_MAX_LATE = pd.Timedelta(minutes=1)

# A peer's or a macro symbol's vote with no fresh read (``_peer_signal`` /
# ``_macro_signal``): its newest bar older than its age limit, or no frame at
# all. Each counts AGAINST the side, the worst case, in the peer score
# (``_directional_peer_score``) and in key_levels' macro net bias; it never
# agrees (2026-10-13, fail closed). While stale votes only abstained, a
# stale peer that would have opposed the trade raised the score: 3 fresh
# bullish peers and 3 stale bearish ones read +3, where 8c40c29 read 0.
NOT_FRESH_STATES = frozenset({"stale", "missing"})

# The age limits' floor, in minutes. A streamed 1m bar is a minute old when
# its minute closes and arrives a moment later, so a limit under 2 minutes
# reads every peer as stale for most of each minute. A polled macro symbol
# (VIX, $NYICDX: ``runtime.history_poll_seconds``) is older still between
# two polls: its limit must also be at least the poll interval plus this
# floor, checked when the strategy is built (``_check_build_knobs``).
VOTE_AGE_FLOOR_MINUTES = 2

# An entry pass that raises for one candidate logs its traceback on the first
# failure of a run of consecutive ones for that symbol and every
# ENTRY_ERROR_TRACEBACK_EVERY-th after, a one-line WARNING in between (the
# engine's per-symbol map failures, ``_symbol_map_failed``, do the same).
ENTRY_ERROR_TRACEBACK_EVERY = 10


@dataclass(frozen=True)
class Knob:
    """The load check of one params key (``PeerConfirmedKeyLevelsStrategy.
    check_knobs``). ``kind``: ``number`` (a finite int or float, never a
    bool), ``count`` (an int, never a bool), ``bool`` (true or false),
    ``symbol`` (a string; blank names none), ``symbols`` (a list of
    non-blank strings) or ``force_flatten`` (a mapping of ``long`` and / or
    ``short``, each true or false; a side it leaves out is flattened, as
    ``BaseStrategy._force_flatten_cutoff`` reads it). ``low`` / ``high`` bound a number or a
    count (``low_open``: strictly above ``low``); ``choices`` lists the
    values a count may take; ``nullable``: a number may also be null, which
    its reader takes as "off" or "derived" (the knob says which)."""

    kind: str
    low: float | None = None
    low_open: bool = False
    high: float | None = None
    choices: tuple[int, ...] | None = None
    nullable: bool = False

    def problem(self, value: Any) -> str | None:
        """What is wrong with ``value`` for this key, or None."""
        if value is None and self.nullable:
            return None
        if self.kind == "bool":
            return None if isinstance(value, bool) else "must be true or false"
        if self.kind == "symbol":
            return None if isinstance(value, str) else "must be a symbol (a string; blank for none)"
        if self.kind == "symbols":
            ok = isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)
            return None if ok else "must be a list of symbols"
        if self.kind == "force_flatten":
            ok = (isinstance(value, Mapping) and set(value) <= {"long", "short"}
                  and all(isinstance(flag, bool) for flag in value.values()))
            return None if ok else "must be a mapping of long: and / or short:, each true or false"
        integer = self.kind == "count"
        typed = (not isinstance(value, bool) and isinstance(value, int if integer else (int, float))
                 and (integer or math.isfinite(value)))
        if typed and self.choices is not None:
            if value in self.choices:
                return None
            return f"must be one of {', '.join(str(choice) for choice in self.choices)}"
        in_bounds = typed and (
            (self.low is None or (value > self.low if self.low_open else value >= self.low))
            and (self.high is None or value <= self.high)
        )
        if in_bounds:
            return None
        return (f"must be {'null or ' if self.nullable else ''}{'a whole number' if integer else 'a finite number'}"
                f"{self._bounds()}")

    def _bounds(self) -> str:
        if self.low is not None and self.high is not None:
            return f" in {'(' if self.low_open else '['}{self.low:g}, {self.high:g}]"
        if self.low is not None:
            return f" {'>' if self.low_open else '>='} {self.low:g}"
        if self.high is not None:
            return f" <= {self.high:g}"
        return ""


# The knobs every peer_confirmed strategy reads through this engine (the
# peer and macro votes, the HTF context, the trigger frame, the spread
# guard, the protective exits, the force flatten, the shared management and
# FVG terms, and BaseStrategy's own reads of the cutoff, the management and
# the ladder), checked for all four: htf_pivots and trend_continuation
# inherit ``normalize_params`` and check these (their own knobs are not
# checked yet). Each is declared in the four manifests, so a loaded config
# carries it. The entry spread guard's two knobs are checked by
# ``shared_entry.entry_spread_limits`` and the HTF EMA spans by
# ``indicators.htf_ema_spans``.
FAMILY_KNOBS: dict[str, Knob] = {
    "tradable": Knob("symbols"),
    "peers": Knob("symbols"),
    "htf_minutes": Knob("count", low=2),
    "htf_lookback_days": Knob("count", low=1),
    "htf_pivot_span": Knob("count", low=1),
    "htf_max_levels_per_side": Knob("count", low=1),
    "htf_atr_tolerance_mult": Knob("number", low=0),
    "htf_pct_tolerance": Knob("number", low=0, high=1),
    "htf_stop_buffer_atr_mult": Knob("number", low=0),
    "ltf_minutes": Knob("count", choices=LTF_MINUTES_CHOICES),
    "min_bars": Knob("count", low=1),
    "min_ltf_bars": Knob("count", low=1),
    "min_peer_agreement": Knob("count", low=0),
    "min_peer_score": Knob("count", low=0),
    # The vote age limits: at least VOTE_AGE_FLOOR_MINUTES (2); the macro
    # one also at least the macro poll interval plus that floor, checked at
    # the build (``_check_build_knobs``).
    "peer_max_age_minutes": Knob("number", low=VOTE_AGE_FLOOR_MINUTES),
    "enable_macro_confirmation": Knob("bool"),
    "require_macro_agreement_count": Knob("count", low=0),
    "dollar_symbol": Knob("symbol"),
    "bond_symbol": Knob("symbol"),
    "volatility_symbol": Knob("symbol"),
    "macro_max_age_minutes": Knob("number", low=VOTE_AGE_FLOOR_MINUTES),
    "protective_exits_on_executable_side": Knob("bool"),
    "force_flatten": Knob("force_flatten"),
    "min_rr": Knob("number", low=0, low_open=True),
    "stop_buffer_atr_mult": Knob("number", low=0),
    "strong_setup_runner_enabled": Knob("bool"),
    "activity_score_weight": Knob("number", low=0),
    "adaptive_breakeven_rr": Knob("number", low=0, low_open=True),
    "adaptive_profit_lock_rr": Knob("number", low=0, low_open=True),
    "adaptive_profit_lock_stop_rr": Knob("number", low=0),
    "adaptive_runner_trigger_rr": Knob("number", low=0, low_open=True),
    "htf_fvg_entry_weight": Knob("number", low=0),
    "ltf_fvg_entry_weight": Knob("number", low=0),
    # shared_entry reads anything under 0.5 as 0.5.
    "opposing_fvg_entry_penalty_mult": Knob("number", low=0.5),
    "fvg_runner_rr_bonus": Knob("number", low=0),
    "same_direction_fvg_validated_bonus": Knob("number", low=0),
    "same_direction_fvg_active_bonus": Knob("number", low=0),
    "opposing_fvg_validated_penalty": Knob("number", low=0),
    "opposing_fvg_active_penalty": Knob("number", low=0),
    "invalidated_opposing_fvg_bonus": Knob("number", low=0),
    "same_direction_fvg_invalidated_penalty": Knob("number", low=0),
    # BaseStrategy's own reads of these params (2026-10-13; until then no
    # manifest declared them and nothing checked them: a value that is no
    # number raised mid-session -- the buffer in the flatten check, the
    # first management pass of a position held on a flattened side, a
    # management knob at a signal's build -- or, the runner trail, read as
    # unset; a fraction was cut to a whole number, a negative clamped and a
    # 0 read as the default, silently). The force-flatten cutoff's buffer
    # (``_force_flatten_cutoff``: minutes before the management window's
    # end; shorter than the last management window, checked at the build).
    "force_flatten_buffer_minutes": Knob("count", low=0),
    # No new entry on a side this many minutes before its force-flatten
    # cutoff (``_entry_cutoff``, 2026-10-13): with the buffer, shorter than
    # the last management window, checked at the build.
    "entry_cutoff_lead_minutes": Knob("count", low=0),
    # The adaptive management (``_adaptive_management_components``): the
    # breakeven's stop offset, the partial breakeven (null: off) and its
    # offset, the runner extension's target (null: derived from the
    # signal's target, its FVG bias and the strong setup) and its trail
    # (null: derived from risk.trailing_stop_pct).
    "adaptive_breakeven_offset_r": Knob("number", low=0),
    "adaptive_partial_breakeven_rr": Knob("number", low=0, low_open=True, nullable=True),
    "adaptive_partial_breakeven_offset_r": Knob("number", low=0),
    "adaptive_runner_target_rr": Knob("number", low=0, low_open=True, nullable=True),
    "adaptive_runner_trail_pct": Knob("number", low=0, low_open=True, high=1, nullable=True),
    # The ladder a divergence-only entry is priced on (``_build_ladder_rungs``,
    # ``_ladder_metadata``; key_levels' own entries build theirs from the HTF
    # levels).
    "ladder_min_target_rr": Knob("number", low=0, low_open=True),
    "ladder_zone_atr_mult": Knob("number", low=0, low_open=True),
    "ladder_max_rungs": Knob("count", low=1),
}

# key_levels' own knobs (and its _1m variant's: the same class).
KEY_LEVELS_KNOBS: dict[str, Knob] = {
    "zone_atr_mult": Knob("number", low=0),
    "zone_pct": Knob("number", low=0, low_open=True, high=1),
    "min_level_score": Knob("number", low=0),
    "level_score_raw_htf_weight": Knob("number", low=0, high=1),
    "level_round_number_tolerance_pct": Knob("number", low=0, high=1),
    "min_ltf_score": Knob("number", low=0, high=LTF_BASE_SCORE_MAX),
    "ltf_quality_bonus_enabled": Knob("bool"),
    "ltf_quality_max_bonus": Knob("number", low=0),
    "ltf_reclaim_quality_bonus_cap": Knob("number", low=0),
    "ltf_zone_interaction_bonus_cap": Knob("number", low=0),
    "ltf_candle_quality_bonus_cap": Knob("number", low=0),
    "ltf_volume_quality_bonus_cap": Knob("number", low=0),
    "ltf_range_expansion_bonus_cap": Knob("number", low=0),
    "require_macro_net_bias": Knob("bool"),
    "require_htf_bias_alignment": Knob("bool"),
    "require_peer_target_clearance": Knob("bool"),
    "strong_setup_min_ltf_score": Knob("number", low=0),
    "strong_setup_min_level_score": Knob("number", low=0),
    "strong_setup_min_peer_score": Knob("count", low=0),
    # The HTF bias has three votes (``_htf_bias``).
    "strong_setup_min_htf_vote_edge": Knob("count", low=0, high=3),
    "strong_setup_target_level_offset": Knob("count", low=0),
}

# The macro symbols' keys, in the order ``_macro_signal`` reads them, with
# whether the symbol rising is bullish for longs.
MACRO_SYMBOL_KEYS: tuple[tuple[str, bool], ...] = (
    ("dollar_symbol", False), ("bond_symbol", True), ("volatility_symbol", False))


def _bar_age_minutes(frame: pd.DataFrame, now: datetime) -> float | None:
    """Minutes from the start of ``frame``'s newest bar to ``now``, the age
    the entry stage's live-bar check reads (``live_1m_bar_stale``, from the
    bar's chart time); a tz-naive index is ET wall time. None for a NaT
    label, whose vote then reads as stale (against the side): its age would
    read as 0, and pass as fresh."""
    newest = pd.Timestamp(frame.index[-1])
    if newest is pd.NaT:
        return None
    moment = pd.Timestamp(now)
    if newest.tzinfo is None and moment.tzinfo is not None:
        moment = moment.tz_convert(sessions.EXCHANGE_TZ).tz_localize(None)
    return max(0.0, (moment - newest).total_seconds() / 60.0)


def _et_date(moment: Any) -> date:
    """The ET calendar date of ``moment``; a tz-naive one is ET wall time."""
    stamp = pd.Timestamp(moment)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(sessions.EXCHANGE_TZ)
    return stamp.date()


def _not_fresh(details: Mapping[str, str]) -> int:
    """How many of a vote record's symbols (``_peer_signal`` /
    ``_macro_signal``'s ``details``) have no fresh read: stale or missing
    (``NOT_FRESH_STATES``)."""
    return sum(1 for state in details.values() if state in NOT_FRESH_STATES)


def _ages_detail(ages: Mapping[str, float | None]) -> str:
    """``SYM@4.2m|SYM@na`` for a reason's detail; ``none`` when empty."""
    if not ages:
        return "none"
    return "|".join(f"{symbol}@{'na' if age is None else f'{age:.1f}m'}" for symbol, age in ages.items())


@dataclass
class _BucketVerdict:
    """What ``entry_signals`` recorded on a symbol's last judged LTF bucket
    (``PeerConfirmedKeyLevelsStrategy._judged_buckets``): the bucket's
    label, whether it signalled, and the refusal's reasons and details,
    repeated on every later pass until the next bucket completes."""

    bucket: pd.Timestamp
    signalled: bool
    reasons: list[str]
    details: dict[str, Any]


@dataclass
class _EntryPass:
    """What key_levels' entry pass reads once and hands each candidate's
    judgement (``PeerConfirmedKeyLevelsStrategy._judge_candidate``): the
    knobs, the sides it may open and those past their entry cutoff, and the
    macro votes, read on the first setup it judges."""

    min_level_score: float
    htf_request: dict[str, Any]
    ltf_minutes: int
    min_bars: int
    min_ltf_bars: int
    tradable: frozenset[str]
    open_sides: list[Side]
    cutoffs_passed: dict[Side, str]
    macro_ctx: dict[str, Any] | None = None


@dataclass
class SideEvaluation:
    """What ``_evaluate_sides`` found for one candidate: the signals the sides
    built, every refused side's blockers (side-prefixed, in evaluation
    order), each side's ``side_eval`` and ``extra`` payloads keyed by side,
    and the near-miss blockers keyed ``<side>.<gate>``."""
    signals: list[Signal] = field(default_factory=list)
    fail_reasons: list[str] = field(default_factory=list)
    side_eval: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, dict[str, Any]] = field(default_factory=dict)
    near_miss_blockers: dict[str, Any] = field(default_factory=dict)


class PeerConfirmedKeyLevelsStrategy(BaseStrategy):
    strategy_name = 'peer_confirmed_key_levels'
    # The params checked at load and at the build (``check_knobs``): the
    # family's and key_levels' own. htf_pivots and trend_continuation check
    # the family's.
    KNOBS: ClassVar[dict[str, Knob]] = {**FAMILY_KNOBS, **KEY_LEVELS_KNOBS}

    def __init__(self, config):
        super().__init__(config)
        # Checked again here: params the load did not normalize (a config
        # with no params block for this strategy, or params set after the
        # load) reach the strategy only through its build. The entry spread
        # guard's limits and the protective exits' knob are read once, here.
        self.check_knobs(self.params)
        self._check_build_knobs()
        self._entry_spread_limits = entry_spread_limits(self.params, self.strategy_name)
        self._protective_exits_on_executable_side = bool(self.params["protective_exits_on_executable_side"])
        # Each symbol's last judged LTF bucket and its verdict (entry_signals
        # judges a completed bucket once).
        self._judged_buckets: dict[str, _BucketVerdict] = {}
        # Each symbol's run of consecutive entry passes that raised for it
        # (``_entry_pass_failed``), and the positions carried past a
        # session already named in a WARNING (``should_force_flatten``).
        self._entry_error_streaks: dict[str, int] = {}
        self._carried_warned: set[tuple[str, str, str]] = set()

    @classmethod
    def check_knobs(cls, params: Mapping[str, Any]) -> None:
        """Refuse params that would load and then misbehave mid-session: each
        knob in ``KNOBS`` present (the manifest declares it, so a loaded
        config carries it) and of its kind and range (``Knob``), strict
        bools; the entry spread guard's two knobs
        (``shared_entry.entry_spread_limits``); the HTF EMA spans
        (``indicators.htf_ema_spans``); and the counts reachable: the HTF
        coarser than the trigger frame, ``min_peer_agreement`` /
        ``min_peer_score`` (and key_levels' ``strong_setup_min_peer_score``)
        no more than the peers that can vote for a candidate (the
        confirmation universe less the candidate itself),
        ``require_macro_agreement_count`` no more than the macro symbols set
        while macro confirmation is on, and each adaptive stop move (the
        profit lock, the breakeven, the partial breakeven while it is on)
        under the peak R that arms it. A ValueError names the key. Until
        2026-10-13 min_rr -3, a min_peer_agreement of 'banana' (raised
        inside the entry pass), a require_macro_agreement_count of 7 (never
        traded) and an ltf_minutes of 7 all loaded. A params key no check
        knows (a typo) is not refused: the framework has no strategy-params
        schema yet."""
        name = cls.strategy_name
        errors: list[str] = []
        for key, knob in cls.KNOBS.items():
            if key not in params:
                errors.append(f"strategies.{name}.params.{key} is missing: the strategy's manifest declares it")
                continue
            problem = knob.problem(params[key])
            if problem is not None:
                errors.append(f"strategies.{name}.params.{key} {problem}, got {params[key]!r}")
        if errors:
            raise ValueError("; ".join(errors))
        entry_spread_limits(params, name)
        try:
            htf_ema_spans(params)
        except ValueError as exc:
            raise ValueError(f"strategies.{name}.params: {exc}") from exc
        if params["htf_minutes"] <= params["ltf_minutes"]:
            errors.append(f"strategies.{name}.params.htf_minutes ({params['htf_minutes']}) must be longer than "
                          f"ltf_minutes ({params['ltf_minutes']})")
        tradable = normalize_symbol_list(params["tradable"])
        universe = normalize_symbol_list(tradable + normalize_symbol_list(params["peers"]))
        voters = len(universe) - (1 if tradable else 0)
        for key in ("min_peer_agreement", "min_peer_score", "strong_setup_min_peer_score"):
            if key in cls.KNOBS and params[key] > voters:
                errors.append(f"strategies.{name}.params.{key} ({params[key]}) is more than the {voters} peers that "
                              f"can vote for a candidate (tradable + peers, less the candidate)")
        macro_symbols = [key for key, _ in MACRO_SYMBOL_KEYS if params[key].strip()]
        if params["enable_macro_confirmation"] and params["require_macro_agreement_count"] > len(macro_symbols):
            errors.append(f"strategies.{name}.params.require_macro_agreement_count "
                          f"({params['require_macro_agreement_count']}) is more than the {len(macro_symbols)} macro "
                          f"symbols set ({', '.join(macro_symbols) or 'none'})")
        # Each stop move must land under the peak R that arms it.
        for stop_key, arm_key, move in (
                ("adaptive_profit_lock_stop_rr", "adaptive_profit_lock_rr", "lock"),
                ("adaptive_breakeven_offset_r", "adaptive_breakeven_rr", "breakeven"),
                ("adaptive_partial_breakeven_offset_r", "adaptive_partial_breakeven_rr", "partial breakeven")):
            if params[arm_key] is not None and params[stop_key] >= params[arm_key]:
                errors.append(f"strategies.{name}.params.{stop_key} ({params[stop_key]}) must be under {arm_key} "
                              f"({params[arm_key]}): the {move} moves the stop below the price it arms at")
        if errors:
            raise ValueError("; ".join(errors))

    def _check_build_knobs(self) -> None:
        """The bounds a knob has against the rest of the config, which the
        load cannot check (``normalize_params`` sees the params alone), so
        checked when the strategy is built, at startup before the first
        cycle, as top_tier checks its ORB window:
        ``force_flatten_buffer_minutes`` shorter than the last management
        window, and with ``entry_cutoff_lead_minutes`` added too, so the
        flatten cutoff and the entry cutoff fall inside that window; and,
        while macro confirmation is on, ``macro_max_age_minutes`` at least
        the macro poll interval (``runtime.history_poll_seconds``) plus
        ``VOTE_AGE_FLOOR_MINUTES`` (7 at the presets' 300 s), so a polled
        symbol that prints every minute is not stale between two polls. A
        ValueError names each key. Until 2026-10-13 a buffer of 1500 loaded
        and clamped the cutoff to 00:00 (no entry all day, every position
        flattened on its first pass), and a macro age limit of 1 made every
        macro vote stale on every pass."""
        name = self.strategy_name
        errors: list[str] = []
        windows = list(self.config.active_strategy.schedule().management_windows)
        if windows:
            last = max(windows, key=lambda window: window.end)
            length = (last.end.hour * 60 + last.end.minute) - (last.start.hour * 60 + last.start.minute)
            span = f"{last.start:%H:%M}-{last.end:%H:%M}, {length} minutes"
            buffer = int(self.params["force_flatten_buffer_minutes"])
            lead = int(self.params["entry_cutoff_lead_minutes"])
            if buffer >= length:
                errors.append(f"strategies.{name}.params.force_flatten_buffer_minutes ({buffer}) must be shorter than "
                              f"the last management window ({span}): the flatten cutoff would fall before it opens")
            elif buffer + lead >= length:
                errors.append(f"strategies.{name}.params.entry_cutoff_lead_minutes ({lead}) plus "
                              f"force_flatten_buffer_minutes ({buffer}) must be shorter than the last management "
                              f"window ({span}): the entry cutoff would fall before it opens")
        if bool(self.params["enable_macro_confirmation"]):
            poll = float(self.config.runtime.history_poll_seconds)
            floor = poll / 60.0 + VOTE_AGE_FLOOR_MINUTES
            max_age = float(self.params["macro_max_age_minutes"])
            if max_age < floor:
                errors.append(f"strategies.{name}.params.macro_max_age_minutes ({max_age:g}) must be at least "
                              f"runtime.history_poll_seconds / 60 + {VOTE_AGE_FLOOR_MINUTES} ({floor:g}): a macro "
                              f"symbol polled every {poll:g} s would read as stale between two polls")
        if errors:
            raise ValueError("; ".join(errors))

    def _active_strategy_name(self) -> str:
        return str(self.strategy_name or self.config.strategy).strip().lower()

    def _failure_style_name(self, side: Side) -> str:
        suffix = 'long' if side == Side.LONG else 'short'
        return f"{self._active_strategy_name()}_{suffix}"

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        min_bars = int(self.params.get("min_bars", 80))
        ltf_min = max(1, int(self.params.get("ltf_minutes", 5)))
        min_ltf_bars = int(self.params.get("min_ltf_bars", 18))
        return max(min_bars, ltf_min * min_ltf_bars)

    @classmethod
    def normalize_params(cls, params: dict[str, Any]) -> dict[str, Any]:
        """Checks every knob at load (``check_knobs``; ``load_config`` runs
        this on every ``strategies.<name>.params`` a config sets), so a bad
        value refuses to start before the Schwab client is built, then
        normalizes the symbol lists (uppercased, de-duplicated, the peers
        that are also tradable dropped)."""
        out = super().normalize_params(params)
        cls.check_knobs(out)
        tradable = normalize_symbol_list(out["tradable"])
        tradable_set = set(tradable)
        out["tradable"] = tradable
        out["peers"] = [symbol for symbol in normalize_symbol_list(out["peers"]) if symbol not in tradable_set]
        return out

    def _tradable_symbols(self) -> list[str]:
        return normalize_symbol_list([str(sym) for sym in self.params.get("tradable", []) if str(sym).strip()])

    def _peer_symbols(self) -> list[str]:
        tradable = set(self._tradable_symbols())
        peers = normalize_symbol_list([str(sym) for sym in self.params.get("peers", []) if str(sym).strip()])
        return [symbol for symbol in peers if symbol not in tradable]

    def _confirmation_universe(self) -> list[str]:
        return normalize_symbol_list(self._tradable_symbols() + self._peer_symbols())

    def should_force_flatten(self, position: Position) -> bool:
        """True on a flattened side (``_force_flatten_cutoff``) from its
        cutoff, as for every stock strategy, and -- the family's own,
        2026-10-13 -- from the first management pass of a later session for
        a position on such a side entered in an earlier one: its flatten
        did not complete before the post-market closed (an order unfilled
        by 20:00, an outage, a restart across the cutoff), so it was
        carried overnight. Until then such a position was managed as a new
        one all through the next session, to that day's cutoff, and the
        flatten's WARNING had fired the evening before. The first carried
        pass logs one WARNING naming the position."""
        now_dt = sessions.now_et()
        cutoff = self._force_flatten_cutoff(position.side, now_dt)
        if cutoff is None:
            return False
        if now_dt.time() >= cutoff:
            return True
        entered = _et_date(position.entry_time)
        if entered >= now_dt.date():
            return False
        key = (position.symbol, position.side.value, pd.Timestamp(position.entry_time).isoformat())
        if key not in self._carried_warned:
            self._carried_warned.add(key)
            LOG.warning("Carried position %s %s qty=%s, entered %s: still open in the %s session, past the "
                        "force-flatten cutoff of the session it was entered in; flattening it at this management "
                        "pass", position.symbol, position.side.value, position.qty, entered.isoformat(),
                        now_dt.date().isoformat())
        return True

    def _trade_management_mode(self) -> str:
        return self.config.risk.trade_management_mode

    @staticmethod
    def _ladder_bounds(price: float, zone_width: float) -> tuple[float, float]:
        width = max(float(zone_width or 0.0), 0.0)
        return float(price) - width, float(price) + width

    def _ladder_exit_signal(self, position: Position, frame: pd.DataFrame, tape: ExitTape, data=None) -> ExitDecision | None:
        """The adaptive-ladder defence: the defended zone flipped, or HTF
        structure broke against the trade with price through its EMA / VWAP
        references.

        The references are the shared exit tape's (2026-09-24), so the first
        session bar reads the all-hours EMAs -- the session-reset ones equal
        the close there, which vetoed the structure-fail exit -- and a
        reference with no value is left out rather than standing in as the
        close. With no reference at all it holds.

        The structure break must have happened after entry: its HTF bar
        closed after the fill. Freshness alone
        (``htf_structure_event_lookback_bars``: 8 60m bars in the key-levels
        preset, a whole session) let a break the entry was taken against
        count, so a LONG opened after a morning HTF CHoCH-down exited on its
        first cycle with the close under its references -- the defect the
        shared CHoCH exit had (2026-09-24). The HTF frame holds completed
        bars, so an event bar that closed after the fill is one the entry
        never saw.
        """
        if self._trade_management_mode() != "adaptive_ladder":
            return None
        metadata = position.metadata if isinstance(position.metadata, dict) else {}
        if not bool(metadata.get("ladder_management_enabled")):
            return None
        defense_price = safe_float(metadata.get("ladder_defense_price"))
        if defense_price is None or defense_price <= 0:
            return None
        close = tape.close
        refs = tape.refs()
        defense_zone_width = max(0.0, safe_float(metadata.get("ladder_defense_zone_width"), 0.0) or 0.0)
        symbol = str(metadata.get("underlying") or position.symbol)
        htf_minutes = self.htf_minutes()
        sr_ctx = None
        if data is not None and hasattr(data, "get_support_resistance"):
            # A read of the stored frame (the engine fetches it). An error
            # here reaches the position manager, which skips this position's
            # exit policy for the cycle and still checks its stop and target
            # (manage_positions).
            sr_ctx = data.get_support_resistance(
                symbol,
                current_price=close,
                flip_frame=frame,
                mode="trading",
                timeframe_minutes=htf_minutes,
                use_prior_day_high_low=bool(self._support_resistance_setting("use_prior_day_high_low", True)),
                use_prior_week_high_low=bool(self._support_resistance_setting("use_prior_week_high_low", True)),
            )
        buffer = max(
            defense_zone_width * 0.25,
            float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0),
            close * 0.0005,
        )
        eps = max(float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0) * 0.15, close * 0.0001, 1e-6)
        # Through the shared reader: `int(value or 2)` turned a configured 0
        # (that frame's gate off) back into the default (2026-09-23).
        confirm_1m, confirm_5m = self.config.support_resistance.flip_confirmation_bars()
        lower, upper = self._ladder_bounds(defense_price, defense_zone_width)
        ms = getattr(sr_ctx, "market_structure", None)

        def _broke_after_entry(direction: str) -> bool:
            # A fresh HTF CHoCH or BoS in `direction` whose bar closed after the fill.
            return ms is not None and any(
                self._active_structure_break(bool(getattr(ms, f"{event}_{direction}", False)),
                                             getattr(ms, f"{event}_{direction}_age_bars", None), htf=True)
                and bar_closed_after(getattr(ms, f"{event}_{direction}_ts", None), position.entry_time, htf_minutes)
                for event in ("choch", "bos")
            )

        if position.side == Side.LONG:
            zone_lost = zone_flip_confirmed("support", lower, upper, flip_frame=frame, confirm_1m_bars=confirm_1m, confirm_5m_bars=confirm_5m, fallback_bar=None, eps=eps)
            if zone_lost and close <= lower - buffer:
                return ExitDecision(f"ladder_support_lost:{defense_price:.4f}", "strategy")
            if _broke_after_entry("down") and refs and close < min(refs):
                return ExitDecision(f"ladder_structure_fail_long:{defense_price:.4f}", "strategy")
        else:
            zone_lost = zone_flip_confirmed("resistance", lower, upper, flip_frame=frame, confirm_1m_bars=confirm_1m, confirm_5m_bars=confirm_5m, fallback_bar=None, eps=eps)
            if zone_lost and close >= upper + buffer:
                return ExitDecision(f"ladder_resistance_lost:{defense_price:.4f}", "strategy")
            if _broke_after_entry("up") and refs and close > max(refs):
                return ExitDecision(f"ladder_structure_fail_short:{defense_price:.4f}", "strategy")
        return None

    @staticmethod
    def _round_number_hit(price: float, tolerance_pct: float) -> bool:
        if price <= 0:
            return False
        if price >= 1000:
            step = 50.0
        elif price >= 300:
            step = 10.0
        elif price >= 100:
            step = 5.0
        else:
            step = 1.0
        anchor = round(price / step) * step
        return abs(price - anchor) <= max(step * 0.1, price * tolerance_pct)

    @staticmethod
    def _clamp_weight(value: float, default: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = float(default)
        return max(0.0, min(1.0, numeric))

    def _level_score_raw_htf_weight(self) -> float:
        return self._clamp_weight(self.params.get("level_score_raw_htf_weight", 0.65), 0.60)

    def _symbol_htf_request(self) -> dict[str, Any]:
        """The HTF context this strategy family trades on -- the symbol's own
        and each peer's. One definition, so they share the data feed's cache
        entry."""
        fast, slow = htf_ema_spans(self.params)
        return {
            "timeframe_minutes": self.htf_minutes(),
            "pivot_span": int(self.params.get("htf_pivot_span", 2)),
            "max_levels_per_side": int(self.params.get("htf_max_levels_per_side", 6)),
            "atr_tolerance_mult": float(self.params.get("htf_atr_tolerance_mult", 0.35)),
            "pct_tolerance": float(self.params.get("htf_pct_tolerance", 0.0030)),
            "stop_buffer_atr_mult": float(self.params.get("htf_stop_buffer_atr_mult", 0.25)),
            "ema_fast_span": fast,
            "ema_slow_span": slow,
            "use_prior_day_high_low": bool(self._support_resistance_setting("use_prior_day_high_low", True)),
            "use_prior_week_high_low": bool(self._support_resistance_setting("use_prior_week_high_low", True)),
        }

    def htf_context_requests(self) -> dict[str, dict[str, Any]]:
        """The score context's request and the family's own, ``symbol``
        (``_symbol_htf_request``), which its entry gates, peer votes and
        scores read, and the dashboard's HTF trend, overlays and level zones
        (2026-10-05: the engine built only the score context, so this one's
        price followed whichever reader came first, the dashboard build
        among them)."""
        return {**super().htf_context_requests(), "symbol": self._symbol_htf_request()}

    def dashboard_htf_trend(self, symbol: str, data, price: float) -> dict[str, str] | None:
        """The HTF EMA trend key_levels' gate (and trend_continuation's
        score) read, off the same context they read it from."""
        if data is None or not price:
            return None
        htf = self._htf_context(symbol, data, current_price=float(price), **self._symbol_htf_request())
        return self._htf_trend_row(*self._htf_bias(htf, float(price)))

    @staticmethod
    def _session_open(frame: pd.DataFrame) -> float | None:
        """Today's session open for the peer and macro votes: the first bar
        at or after 09:30, or today's first bar before then; None with no
        bar today."""
        return session_open_price(frame, sessions.now_et().date())

    def _peer_signal(self, symbol: str, bars: dict[str, pd.DataFrame], data) -> dict[str, Any]:
        """The peers' votes for ``symbol``: each other symbol of the
        confirmation universe votes bullish, bearish or neutral (``score``
        = bullish - bearish, over the fresh votes). A peer with no frame is
        ``missing`` and one whose newest bar started more than
        ``peer_max_age_minutes`` ago is ``stale`` (its age in ``stale``):
        neither agrees with a side, and each counts against every side in
        the peer score (``_directional_peer_score``). ``fresh`` counts the
        peers that voted. Until 2026-10-13 a stale frame voted as if current
        (a thin after-hours peer minutes old), and a missing one abstained."""
        universe = self._confirmation_universe()
        htf_request = self._symbol_htf_request()
        max_age = float(self.params["peer_max_age_minutes"])
        now = sessions.now_et()
        score = 0
        bullish = 0
        bearish = 0
        fresh = 0
        details: dict[str, str] = {}
        stale: dict[str, float | None] = {}
        for peer in universe:
            if peer == symbol:
                continue
            frame = bars.get(peer)
            if frame is None or frame.empty:
                details[peer] = "missing"
                continue
            age = _bar_age_minutes(frame, now)
            if age is None or age > max_age:
                details[peer] = "stale"
                stale[peer] = age
                continue
            fresh += 1
            close = safe_float(frame.iloc[-1]["close"], 0.0)
            ltf = self._resampled_frame(frame, int(self.params.get("ltf_minutes", 5)))
            htf = self._htf_context(peer, data, current_price=close, **htf_request)
            bull_votes = 0
            bear_votes = 0
            ema_fast = safe_float(getattr(htf, "ema_fast", None))
            if ema_fast is not None:
                if close > ema_fast:
                    bull_votes += 1
                elif close < ema_fast:
                    bear_votes += 1
            if ltf is not None and not ltf.empty:
                posture = bar_posture(ltf.iloc[-1])
                if posture == Side.LONG:
                    bull_votes += 1
                elif posture == Side.SHORT:
                    bear_votes += 1
            session_open = self._session_open(frame)
            if session_open is not None:
                if close > session_open:
                    bull_votes += 1
                elif close < session_open:
                    bear_votes += 1
            if bull_votes >= 2 and bull_votes > bear_votes:
                score += 1
                bullish += 1
                details[peer] = "bullish"
            elif bear_votes >= 2 and bear_votes > bull_votes:
                score -= 1
                bearish += 1
                details[peer] = "bearish"
            else:
                details[peer] = "neutral"
        return {"score": score, "bullish": bullish, "bearish": bearish, "details": details,
                "universe": [peer for peer in universe if peer != symbol], "fresh": fresh, "stale": stale,
                "max_age_minutes": max_age}

    def _macro_signal(self, bars: dict[str, pd.DataFrame], data=None) -> dict[str, Any]:
        """The macro symbols' votes (``MACRO_SYMBOL_KEYS``): each agrees
        with longs or shorts, or is neutral. A symbol with no frame is
        ``missing`` and one whose newest bar started more than
        ``macro_max_age_minutes`` ago is ``stale`` (its age in ``stale``,
        by symbol): neither agrees with a side, and key_levels' net bias
        counts each against the side. ``fresh`` counts the symbols that
        voted. VIX and $NYICDX are polled (``runtime.history_poll_seconds``,
        300 s in the presets) and print nothing after about 16:15 / 17:00;
        until 2026-10-13 their 15:5x bucket kept voting to 19:55."""
        if not bool(self.params["enable_macro_confirmation"]):
            return {"long_agree": 0, "short_agree": 0, "details": {}, "enabled": False, "fresh": 0, "stale": {}}
        details: dict[str, str] = {}
        stale: dict[str, float | None] = {}
        long_agree = 0
        short_agree = 0
        fresh = 0
        ltf_min = int(self.params["ltf_minutes"])
        max_age = float(self.params["macro_max_age_minutes"])
        now = sessions.now_et()
        for key, bullish_when_up in MACRO_SYMBOL_KEYS:
            symbol = str(self.params[key]).upper().strip()
            if not symbol:
                continue
            frame = bars.get(symbol)
            if frame is None or frame.empty:
                details[key] = "missing"
                continue
            age = _bar_age_minutes(frame, now)
            if age is None or age > max_age:
                details[key] = "stale"
                stale[symbol] = age
                continue
            macro = self._resampled_frame(frame, ltf_min)
            if macro is None or macro.empty:
                details[key] = "missing"
                continue
            fresh += 1
            last = macro.iloc[-1]
            close = safe_float(last["close"], 0.0)
            vwap = safe_float(last.get("vwap"))
            volume = safe_float(last.get("volume"))
            ema9 = safe_float(last.get("ema9"), close)
            ema20 = safe_float(last.get("ema20"), close)
            ret5 = safe_float(last.get("ret5"), 0.0)
            use_vwap = vwap is not None and volume is not None and volume > 0.0
            if use_vwap:
                posture = bar_posture(last)
                up = posture == Side.LONG and ret5 >= 0.0
                down = posture == Side.SHORT and ret5 <= 0.0
            else:
                # The RTH open _peer_signal votes on; with no bar today the
                # close stands in, so the term cannot vote.
                session_open = self._session_open(frame)
                if session_open is None:
                    session_open = close
                recent_window = macro.tail(6)
                prior_bars = recent_window.iloc[:-1] if len(recent_window) > 1 else recent_window.iloc[0:0]
                recent_5bar_high = safe_float(prior_bars["high"].max(), close) if not prior_bars.empty and "high" in prior_bars.columns else close
                recent_5bar_low = safe_float(prior_bars["low"].min(), close) if not prior_bars.empty and "low" in prior_bars.columns else close
                bull_votes = 0
                bear_votes = 0
                if ema9 > ema20:
                    bull_votes += 1
                elif ema9 < ema20:
                    bear_votes += 1
                if ret5 > 0.0:
                    bull_votes += 1
                elif ret5 < 0.0:
                    bear_votes += 1
                bull_price_action = close > session_open or close > ema9 or close > recent_5bar_high
                bear_price_action = close < session_open or close < ema9 or close < recent_5bar_low
                if bull_price_action:
                    bull_votes += 1
                if bear_price_action:
                    bear_votes += 1
                up = bull_votes >= 2 and bull_votes > bear_votes
                down = bear_votes >= 2 and bear_votes > bull_votes
            if bullish_when_up:
                if up:
                    long_agree += 1
                    details[key] = "bullish_for_longs"
                elif down:
                    short_agree += 1
                    details[key] = "bullish_for_shorts"
                else:
                    details[key] = "neutral"
            else:
                if down:
                    long_agree += 1
                    details[key] = "bullish_for_longs"
                elif up:
                    short_agree += 1
                    details[key] = "bullish_for_shorts"
                else:
                    details[key] = "neutral"
        return {"long_agree": long_agree, "short_agree": short_agree, "details": details, "enabled": True,
                "fresh": fresh, "stale": stale, "max_age_minutes": max_age}

    def _macro_allows(self, side: Side, macro_ctx: dict[str, Any]) -> bool:
        """The family's macro gate: at least ``require_macro_agreement_count``
        macro symbols agree with ``side``, or macro confirmation is off
        (``enable_macro_confirmation``). key_levels refuses on it (and adds
        its net-bias check); htf_pivots and trend_continuation score it. A
        stale or missing symbol never agrees (``_macro_signal``), so here it
        can only count against an entry."""
        if not bool(self.params["enable_macro_confirmation"]):
            return True
        required = int(self.params["require_macro_agreement_count"])
        agree = macro_ctx.get("long_agree" if side == Side.LONG else "short_agree", 0)
        return int(agree or 0) >= required

    @staticmethod
    def _directional_peer_score(side: Side, peer_ctx: Mapping[str, Any]) -> int:
        """The peer score for ``side``, the one every peer gate of the family
        reads: the fresh peers agreeing with it less those opposing it, less
        every peer with no fresh vote (``_peer_signal``'s ``details``:
        ``stale`` or ``missing``), each read as opposing -- the worst case
        (2026-10-13). A peer that cannot vote can only cost the side; until
        then a stale one abstained, so a stale peer that would have opposed
        the trade raised its score."""
        score = int(peer_ctx["score"])
        return (score if side == Side.LONG else -score) - _not_fresh(peer_ctx["details"])

    def _peers_stale_reason(self, peer_ctx: Mapping[str, Any]) -> str | None:
        """``peers_stale(...)`` when fewer peers voted (``_peer_signal``'s
        ``fresh``) than ``min_peer_agreement`` asks to agree, so the peer
        gate could not pass on fresh data: the stale peers with their ages
        and the missing ones in the detail. None otherwise."""
        required = int(self.params["min_peer_agreement"])
        fresh = int(peer_ctx["fresh"])
        if fresh >= required:
            return None
        missing = [peer for peer, state in dict(peer_ctx.get("details", {})).items() if state == "missing"]
        return (f"peers_stale(fresh={fresh},required={required},max_age={float(peer_ctx['max_age_minutes']):g}m,"
                f"stale={_ages_detail(peer_ctx['stale'])},missing={'|'.join(missing) or 'none'})")

    def _macro_stale_reason(self, macro_ctx: Mapping[str, Any]) -> str | None:
        """``macro_stale(...)`` when macro confirmation is on and fewer macro
        symbols voted (``_macro_signal``'s ``fresh``) than
        ``require_macro_agreement_count`` asks to agree: the stale symbols
        with their ages and the missing ones in the detail. None
        otherwise."""
        if not bool(self.params["enable_macro_confirmation"]):
            return None
        required = int(self.params["require_macro_agreement_count"])
        fresh = int(macro_ctx["fresh"])
        if fresh >= required:
            return None
        missing = [str(self.params[key]).upper().strip() for key, state in dict(macro_ctx.get("details", {})).items()
                   if state == "missing"]
        return (f"macro_stale(fresh={fresh},required={required},max_age={float(macro_ctx['max_age_minutes']):g}m,"
                f"stale={_ages_detail(macro_ctx['stale'])},missing={'|'.join(missing) or 'none'})")

    @staticmethod
    def _peer_level_source_priority(kind: str) -> float:
        name = str(kind or "").strip().lower()
        if "prior_week" in name:
            return 3.0
        if "prior_day" in name:
            return 2.0
        if "fvg" in name:
            return 1.75
        return 1.0

    def _collapse_peer_levels(self, candidates: list[dict[str, Any]], close: float, htf: HTFContext) -> list[dict[str, Any]]:
        if not candidates:
            return []
        atr = last_bar_atr(None, float(close), fallback_atr=getattr(htf, "atr14", None))
        tolerance = max(
            float(atr) * float(self.params.get("htf_atr_tolerance_mult", 0.35)),
            float(close) * float(self.params.get("htf_pct_tolerance", 0.0030)),
        )
        ordered = sorted(candidates, key=lambda item: float(item.get("price", 0.0) or 0.0))
        groups: list[list[dict[str, Any]]] = []
        for candidate in ordered:
            if not groups:
                groups.append([candidate])
                continue
            prior_prices = [float(item.get("price", 0.0) or 0.0) for item in groups[-1]]
            anchor = sum(prior_prices) / len(prior_prices)
            if abs(float(candidate.get("price", 0.0) or 0.0) - anchor) <= max(float(tolerance), 1e-9):
                groups[-1].append(candidate)
            else:
                groups.append([candidate])

        def _preference(item: dict[str, Any]) -> tuple[float, float, int, float, float]:
            return (
                float(item.get("level_score", 0.0) or 0.0),
                float(item.get("raw_htf_score", item.get("level_score", 0.0)) or 0.0),
                int(item.get("touches", 1) or 1),
                float(item.get("source_priority", 1.0) or 1.0),
                -abs(float(item.get("price", 0.0) or 0.0) - float(close)),
            )

        collapsed = [max(group, key=_preference) for group in groups]
        collapsed.sort(key=lambda item: float(item.get("price", 0.0) or 0.0))
        return collapsed

    def dashboard_candidate_levels(self, close: float, htf: HTFContext, side: Side) -> list[dict[str, Any]]:
        return self._candidate_levels(close, htf, side)

    def dashboard_zone_width_for_level(
        self,
        side: Side,
        close: float,
        atr: float,
        level_price: float,
        htf: HTFContext,
        candidate: dict[str, Any] | None = None,
    ) -> float | None:
        capability_width = super().dashboard_zone_width_for_level(side, close, atr, level_price, htf, candidate)
        if capability_width is not None:
            return capability_width
        return self._zone_width_for_level(side, close, atr, level_price, htf, candidate)

    def _candidate_levels(self, close: float, htf: HTFContext, side: Side) -> list[dict[str, Any]]:
        tolerance_pct = float(self.params.get("level_round_number_tolerance_pct", 0.0020))
        raw_weight = self._level_score_raw_htf_weight()
        levels: list[dict[str, Any]] = []

        def _append(
            kind: str,
            price: float | None,
            touches: int = 1,
            base_score: float = 1.0,
            zone_lower: float | None = None,
            zone_upper: float | None = None,
            zone_midpoint: float | None = None,
            raw_htf_score: float | None = None,
            source_priority: float | None = None,
        ):
            if price is None or price <= 0:
                return
            resolved_source_priority = float(source_priority if source_priority is not None else self._peer_level_source_priority(kind))
            local_confluence_score = 0.0
            if touches >= 2:
                local_confluence_score += 1.0
            if self._round_number_hit(float(price), tolerance_pct):
                local_confluence_score += 1.0
            ema_fast = safe_float(getattr(htf, "ema_fast", None))
            ema_slow = safe_float(getattr(htf, "ema_slow", None))
            if ema_fast is not None and abs(float(price) - ema_fast) <= max(float(close) * tolerance_pct, float(getattr(htf, "atr14", 0.0) or 0.0) * 0.25):
                local_confluence_score += 1.0
            if ema_slow is not None and abs(float(price) - ema_slow) <= max(float(close) * tolerance_pct, float(getattr(htf, "atr14", 0.0) or 0.0) * 0.25):
                local_confluence_score += 1.0
            raw_strength = max(float(raw_htf_score if raw_htf_score is not None else base_score), float(base_score), 0.0)
            local_quality_score = float(base_score) + local_confluence_score
            blended_level_score = (raw_strength * raw_weight) + (local_quality_score * (1.0 - raw_weight))
            payload = {
                "kind": kind,
                "price": float(price),
                "touches": int(touches),
                "level_score": float(blended_level_score),
                "blended_level_score": float(blended_level_score),
                "raw_htf_score": float(raw_strength),
                "local_confluence_score": float(local_confluence_score),
                "local_quality_score": float(local_quality_score),
                "level_score_raw_htf_weight": float(raw_weight),
                "source_priority": float(resolved_source_priority),
            }
            if zone_lower is not None and zone_upper is not None:
                payload["zone_lower"] = float(zone_lower)
                payload["zone_upper"] = float(zone_upper)
                payload["zone_midpoint"] = float(zone_midpoint if zone_midpoint is not None else (float(zone_lower) + float(zone_upper)) / 2.0)
            levels.append(payload)

        def _append_level_obj(level_obj: Any | None, default_kind: str) -> None:
            if level_obj is None:
                return
            kind = str(getattr(level_obj, "source", None) or default_kind)
            source_priority = float(getattr(level_obj, "source_priority", self._peer_level_source_priority(kind)) or self._peer_level_source_priority(kind))
            _append(
                kind,
                safe_float(getattr(level_obj, "price", None)),
                touches=int(getattr(level_obj, "touches", 1) or 1),
                base_score=max(1.5, source_priority),
                raw_htf_score=safe_float(getattr(level_obj, "score", None)),
                source_priority=source_priority,
            )

        if side == Side.LONG:
            for level_obj in (getattr(htf, "supports", []) or []):
                _append_level_obj(level_obj, "nearest_htf_support")
            _append_level_obj(getattr(htf, "broken_resistance", None), "broken_htf_resistance")
            for gap in (getattr(htf, "bullish_fvgs", []) or []):
                lower = safe_float(getattr(gap, "lower", None))
                upper = safe_float(getattr(gap, "upper", None))
                midpoint = safe_float(getattr(gap, "midpoint", None))
                if lower is None or upper is None or midpoint is None:
                    continue
                if lower > close and upper > close:
                    continue
                _append("bullish_htf_fvg", midpoint, base_score=1.75, zone_lower=lower, zone_upper=upper, zone_midpoint=midpoint)
        else:
            for level_obj in (getattr(htf, "resistances", []) or []):
                _append_level_obj(level_obj, "nearest_htf_resistance")
            _append_level_obj(getattr(htf, "broken_support", None), "broken_htf_support")
            for gap in (getattr(htf, "bearish_fvgs", []) or []):
                lower = safe_float(getattr(gap, "lower", None))
                upper = safe_float(getattr(gap, "upper", None))
                midpoint = safe_float(getattr(gap, "midpoint", None))
                if lower is None or upper is None or midpoint is None:
                    continue
                if lower < close and upper < close:
                    continue
                _append("bearish_htf_fvg", midpoint, base_score=1.75, zone_lower=lower, zone_upper=upper, zone_midpoint=midpoint)
        return self._collapse_peer_levels(levels, close, htf)

    def _zone_width_for_level(self, side: Side, close: float, atr: float, level_price: float, htf: HTFContext, candidate: dict[str, Any] | None = None) -> float:
        min_zone = max(close * float(self.params.get("zone_pct", 0.0015)), 0.01)
        zone_floor = float(min_zone)
        zone_width = max(float(self.params.get("zone_atr_mult", 0.21)) * atr, min_zone)
        if isinstance(candidate, dict):
            zone_lower = safe_float(candidate.get("zone_lower"))
            zone_upper = safe_float(candidate.get("zone_upper"))
            if zone_lower is not None and zone_upper is not None and zone_upper >= zone_lower:
                # Use the FVG span to raise the zone FLOOR (so very small FVGs
                # don't shrink below min_zone), but do NOT let a tall FVG blow
                # the zone out to the full gap height. The zone around the
                # midpoint should stay ATR-proportional, same as pivot zones.
                fvg_half_height = max((float(zone_upper) - float(zone_lower)) / 2.0, min_zone)
                atr_based_cap = max(zone_width, min_zone)
                explicit_zone_half_width = min(fvg_half_height, atr_based_cap)
                zone_floor = max(zone_floor, explicit_zone_half_width)
        opposite_side = Side.SHORT if side == Side.LONG else Side.LONG
        opposite_levels = self._candidate_levels(close, htf, opposite_side)
        opposing_gaps = []
        for candidate in opposite_levels:
            other_price = float(candidate["price"])
            gap = (other_price - float(level_price)) if side == Side.LONG else (float(level_price) - other_price)
            if gap > 0:
                opposing_gaps.append(gap)
        if opposing_gaps:
            zone_width = min(zone_width, max(zone_floor, min(opposing_gaps) * 0.33))
        same_side_prices = [float(candidate["price"]) for candidate in self._candidate_levels(close, htf, side)]
        same_side_gaps = [abs(other_price - float(level_price)) for other_price in same_side_prices if abs(other_price - float(level_price)) > 1e-9]
        if same_side_gaps:
            zone_width = min(zone_width, max(zone_floor, min(same_side_gaps) * 0.45))
        return max(zone_floor, zone_width)

    def _peer_level_selection_details(self, side: Side, candidate: dict[str, Any], ltf: pd.DataFrame, close: float, atr: float) -> dict[str, float]:
        level_price = float(candidate.get("price", 0.0) or 0.0)
        zone_width = float(candidate.get("zone_width", 0.0) or 0.0)
        source_priority = float(candidate.get("source_priority", self._peer_level_source_priority(str(candidate.get("kind") or ""))) or 0.0)
        ltf_preview = float(self._ltf_score(side, ltf, level_price, zone_width).get("score", 0.0) or 0.0)
        distance = abs(float(close) - level_price)
        distance_atr = distance / max(float(atr), 1e-9)
        selection_score = (
            float(candidate.get("level_score", 0.0) or 0.0)
            + (float(ltf_preview) * 0.85)
            + (source_priority * 0.45)
            - (min(distance_atr, 2.5) * 0.10)
        )
        return {
            "selection_ltf_score": float(ltf_preview),
            "selection_source_priority": source_priority,
            "selection_distance_atr": float(distance_atr),
            "selection_score": float(selection_score),
            "selection_level_score": float(candidate.get("level_score", 0.0) or 0.0),
            "selection_raw_htf_score": float(candidate.get("raw_htf_score", candidate.get("level_score", 0.0)) or 0.0),
            "selection_local_quality_score": float(candidate.get("local_quality_score", candidate.get("level_score", 0.0)) or 0.0),
        }

    @staticmethod
    def _level_selection_priority_key(payload: dict[str, Any]) -> tuple[float, float, float, float, float]:
        return (
            float(payload.get("selection_ltf_score", 0.0) or 0.0),
            float(payload.get("level_score", 0.0) or 0.0),
            float(payload.get("selection_source_priority", payload.get("source_priority", 0.0)) or 0.0),
            -float(payload.get("selection_distance_atr", float("inf")) or float("inf")),
            float(payload.get("selection_score", 0.0) or 0.0),
        )

    def dashboard_select_level(self, side: Side, close: float, ltf: pd.DataFrame, htf: HTFContext) -> dict[str, Any] | None:
        """The level the entry pass selects, on the dashboard's LTF: its
        completed buckets (``_completed_ltf``), on the ET clock, as the
        dashboard hands no step frame."""
        completed = self._completed_ltf(ltf, int(self.params["ltf_minutes"]), sessions.now_et())
        return self._select_level(side, close, completed, htf)

    def _select_level(self, side: Side, close: float, ltf: pd.DataFrame, htf: HTFContext) -> dict[str, Any] | None:
        if ltf is None or ltf.empty:
            return None
        atr = last_bar_atr(ltf, close, fallback_atr=getattr(htf, "atr14", None))
        recent = ltf.tail(4)
        best: dict[str, Any] | None = None
        for candidate in self._candidate_levels(close, htf, side):
            price = float(candidate["price"])
            zone = self._zone_width_for_level(side, close, atr, price, htf, candidate)
            # Per-bar overlap check: a bar "touches" the zone when its
            # OWN range covers part of [price-zone, price+zone]. The
            # window-wide min/max formulation (low.min() AND high.max())
            # was a false-positive trap — one bar entirely above the
            # zone AND another entirely below would pass without any
            # bar actually entering the zone (e.g. fast spike on news).
            touched = bool(((recent["low"] <= price + zone) & (recent["high"] >= price - zone)).any())
            if not touched:
                continue
            distance = abs(close - price)
            payload = {**candidate, "zone_width": zone, "distance": distance}
            payload.update(self._peer_level_selection_details(side, payload, ltf, close, atr))
            if best is None:
                best = payload
                continue

            if self._level_selection_priority_key(payload) > self._level_selection_priority_key(best):
                best = payload
        return best

    def _configured_ltf_candle_summary(self, side: Side, ltf: pd.DataFrame) -> dict[str, Any]:
        # _directional_candle_signal slices internally to CANDLE_CONTEXT_BARS
        # so TA-Lib has enough context — pass the full ltf frame.
        frame = ltf if ltf is not None and not ltf.empty else pd.DataFrame()
        summary = self._directional_candle_signal(frame, side)
        summary["matches"] = set(summary.get("matches", []))
        return summary

    def _configured_ltf_candle_match(self, side: Side, ltf: pd.DataFrame) -> bool:
        return bool(self._configured_ltf_candle_summary(side, ltf).get("confirmed"))

    @staticmethod
    def _clamp01(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    def _ltf_quality_caps(self) -> dict[str, float]:
        return {
            "reclaim_reject": max(0.0, float(self.params.get("ltf_reclaim_quality_bonus_cap", 0.80))),
            "zone_interaction": max(0.0, float(self.params.get("ltf_zone_interaction_bonus_cap", 0.50))),
            "candle_quality": max(0.0, float(self.params.get("ltf_candle_quality_bonus_cap", 0.50))),
            "volume_quality": max(0.0, float(self.params.get("ltf_volume_quality_bonus_cap", 0.40))),
            "range_expansion": max(0.0, float(self.params.get("ltf_range_expansion_bonus_cap", 0.40))),
            "max_total": max(0.0, float(self.params.get("ltf_quality_max_bonus", 2.00))),
        }

    def _ltf_quality_bonus(
        self,
        side: Side,
        ltf: pd.DataFrame,
        *,
        level_price: float,
        zone_width: float,
        choch: bool,
        volume_ratio: float,
        pattern_or_body: bool,
        reaction_confirmed: bool,
        sweep: bool,
    ) -> dict[str, Any]:
        if not bool(self.params.get("ltf_quality_bonus_enabled", True)):
            return {"quality_bonus": 0.0, "quality_reasons": [], "quality_breakdown": {}}
        if ltf is None or ltf.empty:
            return {"quality_bonus": 0.0, "quality_reasons": [], "quality_breakdown": {}}
        last = ltf.iloc[-1]
        prior = ltf.iloc[:-1]
        if prior.empty:
            return {"quality_bonus": 0.0, "quality_reasons": [], "quality_breakdown": {}}

        close = safe_float(last.get("close"), level_price)
        open_ = safe_float(last.get("open"), close)
        atr = last_bar_atr(ltf, close, floor_pct=0.0005)
        upper_wick_frac, lower_wick_frac, body_frac, bar_range = bar_wick_fractions(ltf)
        close_pos = bar_close_position(ltf)
        recent_ranges = (prior["high"] - prior["low"]).tail(10) if {"high", "low"}.issubset(prior.columns) else pd.Series(dtype=float)
        avg_range = safe_float(recent_ranges.mean(), bar_range if bar_range > 0 else atr)
        avg_range = max(avg_range, atr * 0.35, 1e-9)
        range_ratio = float(bar_range) / avg_range if avg_range > 0 else 0.0
        zone_scale = max(float(zone_width), atr * 0.15, abs(float(level_price)) * 0.0005, 1e-6)
        caps = self._ltf_quality_caps()
        components: dict[str, float] = {}
        sweep_window = ltf.tail(4)

        context_active = bool(reaction_confirmed or choch or sweep or pattern_or_body)
        directional_close_score = self._clamp01((close_pos - 0.45) / 0.55) if side == Side.LONG else self._clamp01((0.55 - close_pos) / 0.55)
        wick_score = self._clamp01(1.0 - ((upper_wick_frac if side == Side.LONG else lower_wick_frac) / 0.45))
        body_score = self._clamp01((body_frac - 0.40) / 0.40)
        directional_body = max(0.0, close - open_) if side == Side.LONG else max(0.0, open_ - close)
        directional_body_score = self._clamp01(directional_body / max(bar_range, 1e-9))

        if context_active and pattern_or_body:
            candle_bonus = caps["candle_quality"] * (
                (directional_body_score * 0.35)
                + (body_score * 0.30)
                + (directional_close_score * 0.20)
                + (wick_score * 0.15)
            )
            if candle_bonus > 0.0:
                components["candle_quality_bonus"] = candle_bonus

        if context_active and volume_ratio > 1.0:
            volume_bonus = caps["volume_quality"] * self._clamp01((float(volume_ratio) - 1.0) / 0.80)
            if volume_bonus > 0.0:
                components["volume_quality_bonus"] = volume_bonus

        if context_active and range_ratio > 1.0:
            range_bonus = caps["range_expansion"] * self._clamp01((float(range_ratio) - 1.0) / 0.80)
            if range_bonus > 0.0:
                components["range_expansion_bonus"] = range_bonus

        if side == Side.LONG:
            sweep_extreme = safe_float(sweep_window["low"].min(), close) if "low" in sweep_window.columns else close
            recovery_distance = max(0.0, close - float(level_price))
            penetration_ratio = max(0.0, float(level_price) - float(sweep_extreme)) / zone_scale
        else:
            sweep_extreme = safe_float(sweep_window["high"].max(), close) if "high" in sweep_window.columns else close
            recovery_distance = max(0.0, float(level_price) - close)
            penetration_ratio = max(0.0, float(sweep_extreme) - float(level_price)) / zone_scale

        if reaction_confirmed:
            reclaim_reject_bonus = caps["reclaim_reject"] * (
                (self._clamp01(penetration_ratio / 1.25) * 0.35)
                + (self._clamp01(recovery_distance / zone_scale) * 0.30)
                + (directional_close_score * 0.20)
                + (wick_score * 0.15)
            )
            if reclaim_reject_bonus > 0.0:
                components["reclaim_reject_quality_bonus"] = reclaim_reject_bonus

        if sweep:
            interaction_center = max(0.0, 1.0 - (abs(penetration_ratio - 0.85) / 1.25))
            overshoot_penalty = self._clamp01((penetration_ratio - 2.25) / 1.00)
            zone_bonus = caps["zone_interaction"] * interaction_center * (1.0 - (overshoot_penalty * 0.65))
            if zone_bonus > 0.0:
                components["zone_interaction_bonus"] = zone_bonus

        total_bonus = min(sum(components.values()), caps["max_total"])
        quality_reasons = [name for name, value in components.items() if value > 0.0]
        rounded_breakdown = {name: round(float(value), 4) for name, value in components.items() if value > 0.0}
        return {
            "quality_bonus": round(float(total_bonus), 4),
            "quality_reasons": quality_reasons,
            "quality_breakdown": rounded_breakdown,
        }

    def _ltf_score(self, side: Side, ltf: pd.DataFrame, level_price: float, zone_width: float) -> dict[str, Any]:
        """The trigger on ``ltf``'s last bar (the entry pass hands it the
        completed buckets, ``_completed_ltf``) at the level ``level_price``.

        ``base_score`` is the 4-part trigger: a CHoCH (the close beyond the
        prior 3 bars' extreme) 1, a volume spike (over 1.5x the prior 20
        bars' mean) 1, the candle model's confirmation (0.35 / 0.70 / 1.0),
        and a sweep-reclaim (a LONG) / sweep-reject (a SHORT) 1. The sweep
        counts only when it is one: within the last 4 bars a low traded
        below the level (a high above it for a SHORT), and the trigger bar
        closes back at or above it (at or below it). Until 2026-10-13 a low
        within the zone ABOVE the level counted -- the very touch that
        selects the level (``_select_level``), and the level sits on the
        trade's side of the close by construction -- so every selected
        setup scored it. ``quality_bonus`` (capped, ``_ltf_quality_bonus``)
        ranks setups; ``score`` = the two added. The entry gate,
        ``min_ltf_score``, reads ``base_score`` alone."""
        if ltf is None or len(ltf) < 5:
            return {"score": 0.0, "base_score": 0.0, "quality_bonus": 0.0, "reasons": [], "quality_reasons": [], "quality_breakdown": {}}
        last = ltf.iloc[-1]
        prior = ltf.iloc[:-1]
        prev3 = prior.tail(3)
        body = abs(float(last["close"]) - float(last["open"]))
        avg_body = abs(prior["close"] - prior["open"]).tail(20).mean() if len(prior) >= 2 else body
        avg_vol_raw = prior["volume"].tail(20).mean() if "volume" in prior.columns and len(prior) >= 2 else float(last.get("volume", 0.0))
        last_vol = float(last.get("volume", 0.0))
        if avg_vol_raw == avg_vol_raw and float(avg_vol_raw) > 0:
            avg_vol_valid = float(avg_vol_raw)
            volume_ratio = last_vol / avg_vol_valid
            vol_spike = last_vol > avg_vol_valid * 1.5
        else:
            volume_ratio = 1.0
            vol_spike = False
        strong_body = body > max(0.01, float(avg_body) * 1.2)
        reasons: list[str] = []
        base_score = 0.0
        ltf_sweep_window = ltf.tail(4)
        pattern_summary = self._configured_ltf_candle_summary(side, ltf)
        candle_score = float(pattern_summary.get("score", 0.0) or 0.0)
        candle_net_score = float(pattern_summary.get("net_score", 0.0) or 0.0)
        anchor_bars = int(pattern_summary.get("anchor_bars", 0) or 0)
        anchor_pattern = pattern_summary.get("anchor_pattern")
        anchor_label = "bullish_pattern" if side == Side.LONG else "bearish_pattern"
        if side == Side.LONG:
            choch = float(last["close"]) > float(prev3["high"].max()) if not prev3.empty else False
            reclaim = float(last["close"]) >= float(level_price)
            sweep = float(ltf_sweep_window["low"].min()) < float(level_price) if "low" in ltf_sweep_window.columns else False
            bullish_body = float(last["close"]) > float(last["open"]) and strong_body
            reaction_confirmed = bool(reclaim and sweep)
            candle_tier = str(pattern_summary.get("confirm_tier", "none") or "none")
            weak_candle = candle_tier == "weak_1c"
            candle_confirmed = (not weak_candle and bool(pattern_summary.get("confirmed"))) or (weak_candle and bool(choch or sweep or bullish_body or reaction_confirmed))
            pattern_or_body = candle_confirmed or bullish_body
            if choch:
                base_score += 1.0
                reasons.append("choch_up")
            if vol_spike:
                base_score += 1.0
                reasons.append("volume_spike")
            if candle_confirmed:
                candle_contribution = float(pattern_summary.get("confirm_contribution", 0.0) or 0.0)
                base_score += candle_contribution
                reasons.append(f"{anchor_label}_{anchor_bars}c" if anchor_bars > 0 else anchor_label)
            elif bullish_body:
                reasons.append("bullish_body_confirm")
            if reaction_confirmed:
                base_score += 1.0
                reasons.append("sweep_reclaim")
        else:
            choch = float(last["close"]) < float(prev3["low"].min()) if not prev3.empty else False
            reject = float(last["close"]) <= float(level_price)
            sweep = float(ltf_sweep_window["high"].max()) > float(level_price) if "high" in ltf_sweep_window.columns else False
            bearish_body = float(last["close"]) < float(last["open"]) and strong_body
            reaction_confirmed = bool(reject and sweep)
            candle_tier = str(pattern_summary.get("confirm_tier", "none") or "none")
            weak_candle = candle_tier == "weak_1c"
            candle_confirmed = (not weak_candle and bool(pattern_summary.get("confirmed"))) or (weak_candle and bool(choch or sweep or bearish_body or reaction_confirmed))
            pattern_or_body = candle_confirmed or bearish_body
            if choch:
                base_score += 1.0
                reasons.append("choch_down")
            if vol_spike:
                base_score += 1.0
                reasons.append("volume_spike")
            if candle_confirmed:
                candle_contribution = float(pattern_summary.get("confirm_contribution", 0.0) or 0.0)
                base_score += candle_contribution
                reasons.append(f"{anchor_label}_{anchor_bars}c" if anchor_bars > 0 else anchor_label)
            elif bearish_body:
                reasons.append("bearish_body_confirm")
            if reaction_confirmed:
                base_score += 1.0
                reasons.append("sweep_reject")
        quality = self._ltf_quality_bonus(
            side,
            ltf,
            level_price=float(level_price),
            zone_width=float(zone_width),
            choch=bool(choch),
            volume_ratio=float(volume_ratio),
            pattern_or_body=bool(pattern_or_body),
            reaction_confirmed=bool(reaction_confirmed),
            sweep=bool(sweep),
        )
        score = float(base_score) + float(quality.get("quality_bonus", 0.0) or 0.0)
        return {
            "score": round(float(score), 4),
            "base_score": round(float(base_score), 4),
            "quality_bonus": round(float(quality.get("quality_bonus", 0.0) or 0.0), 4),
            "reasons": reasons,
            "quality_reasons": list(quality.get("quality_reasons", [])),
            "quality_breakdown": dict(quality.get("quality_breakdown", {})),
            "ltf_candle_matches": sorted(pattern_summary.get("matches", set())),
            "ltf_candle_anchor_pattern": anchor_pattern,
            "ltf_candle_anchor_bars": int(anchor_bars),
            "ltf_candle_score": round(float(candle_score), 4),
            "ltf_candle_net_score": round(float(candle_net_score), 4),
            "ltf_candle_opposite_score": round(float(pattern_summary.get("opposite_score", 0.0) or 0.0), 4),
            "ltf_candle_regime_hint": str(pattern_summary.get("regime_hint", "neutral") or "neutral"),
        }

    def _sorted_target_levels(self, side: Side, close: float, htf: HTFContext) -> list[dict[str, Any]]:
        raw_candidates: list[dict[str, Any]] = []

        def _append(kind: str, price: float | None, *, touches: int = 1) -> None:
            if price is None:
                return
            level_price = float(price)
            if level_price <= 0:
                return
            raw_candidates.append({
                "kind": kind,
                "price": level_price,
                "touches": int(touches),
                "level_score": float(self._peer_level_source_priority(kind)),
                "source_priority": float(self._peer_level_source_priority(kind)),
            })

        def _append_level_obj(level_obj: Any | None, default_kind: str) -> None:
            if level_obj is None:
                return
            kind = str(getattr(level_obj, "source", None) or default_kind)
            _append(kind, getattr(level_obj, "price", None), touches=int(getattr(level_obj, "touches", 1) or 1))

        for level in getattr(htf, "resistances", []) or []:
            _append_level_obj(level, "resistance")
        for level in getattr(htf, "supports", []) or []:
            _append_level_obj(level, "support")
        _append_level_obj(getattr(htf, "broken_support", None), "broken_htf_support")
        _append_level_obj(getattr(htf, "broken_resistance", None), "broken_htf_resistance")
        for gap in (getattr(htf, "bearish_fvgs", []) or []):
            _append("bearish_htf_fvg", getattr(gap, "midpoint", None))
        for gap in (getattr(htf, "bullish_fvgs", []) or []):
            _append("bullish_htf_fvg", getattr(gap, "midpoint", None))

        if side == Side.LONG:
            candidates = [item for item in raw_candidates if float(item.get("price", 0.0) or 0.0) > float(close)]
        else:
            candidates = [item for item in raw_candidates if float(item.get("price", 0.0) or 0.0) < float(close)]

        collapsed = self._collapse_peer_levels(candidates, close, htf)
        collapsed = [item for item in collapsed if float(item.get("price", 0.0) or 0.0) > 0]
        collapsed.sort(key=lambda item: float(item.get("price", 0.0) or 0.0), reverse=(side != Side.LONG))
        return collapsed


    def _qualifying_target_levels(self, side: Side, close: float, stop: float, htf: HTFContext, atr: float, rr_required: float) -> list[dict[str, Any]]:
        qualifying: list[dict[str, Any]] = []
        for candidate in self._sorted_target_levels(side, close, htf):
            price = float(candidate.get("price", 0.0) or 0.0)
            if price <= 0:
                continue
            if side == Side.LONG:
                risk = max(0.01, close - stop)
                reward = price - close
            else:
                risk = max(0.01, stop - close)
                reward = close - price
            if reward <= 0:
                continue
            rr = reward / risk
            if rr < rr_required:
                continue
            zone_width = self._zone_width_for_level(side, close, atr, price, htf, candidate)
            lower, upper = self._ladder_bounds(price, zone_width)
            qualifying.append({**candidate, "price": float(price), "rr": float(rr), "zone_width": float(zone_width), "lower": float(lower), "upper": float(upper)})
        return qualifying

    @staticmethod
    def _ladder_management_metadata(side: Side, level: dict[str, Any], qualifying_target_levels: list[dict[str, Any]], selected_index: int) -> dict[str, Any]:
        rungs = [{"price": round(float(item.get("price", 0.0) or 0.0), 6), "kind": str(item.get("kind") or "target"), "zone_width": round(float(item.get("zone_width", 0.0) or 0.0), 6), "lower": round(float(item.get("lower", 0.0) or 0.0), 6), "upper": round(float(item.get("upper", 0.0) or 0.0), 6), "rr": round(float(item.get("rr", 0.0) or 0.0), 4)} for item in qualifying_target_levels]
        defense_price = float(level.get("price", 0.0) or 0.0)
        defense_zone_width = float(level.get("zone_width", 0.0) or 0.0)
        return {"ladder_management_enabled": True, "ladder_direction": "long" if side == Side.LONG else "short", "ladder_active_index": int(selected_index), "ladder_rungs": rungs, "ladder_defense_price": round(defense_price, 6), "ladder_defense_zone_width": round(defense_zone_width, 6), "ladder_defense_kind": str(level.get("kind") or "entry_level"), "ladder_entry_level_price": round(defense_price, 6), "ladder_entry_level_zone_width": round(defense_zone_width, 6), "ladder_entry_level_kind": str(level.get("kind") or "entry_level"), "ladder_final_rung_cleared": False}

    def _peer_target_clearance(self, side: Side, close: float, htf: HTFContext, atr: float) -> dict[str, Any] | None:
        targets = self._sorted_target_levels(side, close, htf)
        if not targets:
            return None
        nearest = targets[0]
        level_price = float(nearest.get("price", 0.0) or 0.0)
        if level_price <= 0 or close <= 0:
            return None
        distance = (level_price - close) if side == Side.LONG else (close - level_price)
        if distance <= 0:
            return None
        clearance_atr = distance / max(float(atr), 1e-9)
        return {
            "kind": str(nearest.get("kind") or "target"),
            "price": level_price,
            "distance": distance,
            "clearance_pct": distance / close,
            "clearance_atr": clearance_atr,
        }

    def _build_equity_signal(self, c: Candidate, frame: pd.DataFrame, ltf: pd.DataFrame, htf: HTFContext, side: Side, level: dict[str, Any], peer_ctx: dict[str, Any], macro_ctx: dict[str, Any], data=None) -> Signal | None:
        close = safe_float(frame.iloc[-1]["close"], 0.0)
        failure_style = self._failure_style_name(side)
        # Cheap directional gates run BEFORE expensive trigger scoring so that
        # symbols mis-sided against the hourly/peer/macro tape short-circuit out
        # instead of burning candle-pattern + quality-bonus compute and then
        # logging a misleading ``ltf_base_score_below_min:0.0000<...`` reason.
        bias, bull_votes, bear_votes = self._htf_bias(htf, close)
        require_htf_alignment = bool(self.params.get("require_htf_bias_alignment", True))
        if side == Side.LONG:
            if bias == "bearish":
                self._set_build_failure(c.symbol, failure_style, "htf_bias_bearish")
                return None
            if require_htf_alignment and bias != "bullish":
                self._set_build_failure(c.symbol, failure_style, f"htf_bias_not_bullish:{bias}({bull_votes}v{bear_votes})")
                return None
        if side == Side.SHORT:
            if bias == "bullish":
                self._set_build_failure(c.symbol, failure_style, "htf_bias_bullish")
                return None
            if require_htf_alignment and bias != "bearish":
                self._set_build_failure(c.symbol, failure_style, f"htf_bias_not_bearish:{bias}({bull_votes}v{bear_votes})")
                return None
        # Fewer fresh peers or macro symbols than their gate asks to agree:
        # refused on the data's age (2026-10-13). Past that, every peer and
        # macro symbol with no fresh read (stale or missing) counts against
        # the side, the worst case: in the peer score and the macro net bias.
        peers_stale = self._peers_stale_reason(peer_ctx)
        if peers_stale is not None:
            self._set_build_failure(c.symbol, failure_style, peers_stale)
            return None
        side_name, other_name = ("long", "short") if side == Side.LONG else ("short", "long")
        peer_score = int(peer_ctx["score"])
        directional_peer_score = self._directional_peer_score(side, peer_ctx)
        peers_not_fresh = _not_fresh(peer_ctx["details"])
        min_peer_score = _discrete_score_threshold(self.params.get("min_peer_score", 2), 2, minimum=0)
        min_peer_agreement = int(self.params.get("min_peer_agreement", 2))
        peer_agreement = int(peer_ctx["bullish"] if side == Side.LONG else peer_ctx["bearish"])
        if directional_peer_score < min_peer_score or peer_agreement < min_peer_agreement:
            self._set_build_failure(c.symbol, failure_style, f"peer_confirmation_insufficient_{side_name}"
                                    + (f":not_fresh={peers_not_fresh}" if peers_not_fresh else ""))
            return None
        macro_confirmation_enabled = bool(self.params.get("enable_macro_confirmation", True))
        require_macro_net_bias = bool(self.params.get("require_macro_net_bias", True))
        long_agree = int(macro_ctx.get("long_agree", 0))
        short_agree = int(macro_ctx.get("short_agree", 0))
        macro_stale = self._macro_stale_reason(macro_ctx)
        if macro_stale is not None:
            self._set_build_failure(c.symbol, failure_style, macro_stale)
            return None
        if not self._macro_allows(side, macro_ctx):
            self._set_build_failure(c.symbol, failure_style, f"macro_confirmation_insufficient_{side_name}")
            return None
        if macro_confirmation_enabled and require_macro_net_bias:
            agree, oppose = (long_agree, short_agree) if side == Side.LONG else (short_agree, long_agree)
            macro_not_fresh = _not_fresh(macro_ctx["details"])
            if agree <= oppose + macro_not_fresh:
                self._set_build_failure(c.symbol, failure_style,
                                        f"macro_net_bias_insufficient_{side_name}:{side_name}={agree}<={other_name}={oppose}"
                                        + (f"+not_fresh={macro_not_fresh}" if macro_not_fresh else ""))
                return None
        # Expensive trigger-score compute (candle patterns + quality bonus).
        # Only runs after the cheap directional gates above have accepted.
        trigger = self._ltf_score(side, ltf, float(level["price"]), float(level["zone_width"]))
        ltf_min_score = float(self.params["min_ltf_score"])
        # The gate reads the 4-part base trigger; the quality bonus only ranks
        # (``ltf_score``, the manifest's signal_priority field, and the
        # strong-setup bar). Until 2026-10-13 the bonus (up to 2.0) counted
        # toward this gate too.
        ltf_base_score = float(trigger["base_score"])
        ltf_score = float(trigger.get("score", 0.0) or 0.0)
        if ltf_base_score < ltf_min_score:
            self._set_build_failure(c.symbol, failure_style,
                                    f"ltf_base_score_below_min:{ltf_base_score:.4f}<{ltf_min_score:.4f}")
            return None
        atr = last_bar_atr(ltf, close, fallback_atr=getattr(htf, "atr14", None))
        target_clearance = self._peer_target_clearance(side, close, htf, atr)
        # key_levels' own clearance check: the side is refused when the
        # NEAREST HTF key level in the trade's direction is inside BOTH
        # support_resistance.entry_min_clearance_pct and _atr; clearing
        # either one passes. It rode on shared_entry.use_sr_filter until
        # 2026-09-24; that knob is now the shared S/R veto every strategy
        # meets (refusing inside EITHER clearance, against the S/R context),
        # so this check is the strategy's own param. The S/R section switch
        # still turns it off with the rest.
        if bool(self.params.get("require_peer_target_clearance", True)) and bool(self._support_resistance_setting("enabled", True)) and target_clearance is not None:
            min_clearance_pct = float(self._support_resistance_setting("entry_min_clearance_pct", 0.0038))
            min_clearance_atr = float(self._support_resistance_setting("entry_min_clearance_atr", 0.85))
            clearance_pct = float(target_clearance["clearance_pct"])
            clearance_atr = float(target_clearance["clearance_atr"])
            clearance_pct_blocked = clearance_pct <= min_clearance_pct
            clearance_atr_blocked = clearance_atr <= min_clearance_atr
            if clearance_pct_blocked and clearance_atr_blocked:
                self._set_build_failure(c.symbol, failure_style, "too_close_to_overhead_resistance" if side == Side.LONG else "too_close_to_nearby_support")
                return None
        swing_window = ltf.tail(5)
        buffer = max(atr * float(self.params.get("stop_buffer_atr_mult", 0.68)), float(level["zone_width"]) * 0.35)
        if side == Side.LONG:
            swing = float(swing_window["low"].min()) if not swing_window.empty else close
            stop = min(float(level["price"]) - buffer, swing - (buffer * 0.5))
        else:
            swing = float(swing_window["high"].max()) if not swing_window.empty else close
            stop = max(float(level["price"]) + buffer, swing + (buffer * 0.5))
        rr_required = float(self.params.get("min_rr", 1.75))
        qualifying_target_levels = self._qualifying_target_levels(side, close, stop, htf, atr, rr_required)
        if not qualifying_target_levels:
            self._set_build_failure(c.symbol, failure_style, f"no_qualifying_target_rr:{rr_required:.2f}")
            return None
        strong_vote_edge = self._side_vote_edge(side, bull_votes, bear_votes)
        strong_setup_trigger_min = max(0.0, float(self.params.get("strong_setup_min_ltf_score", 3.2)))
        strong_setup_peer_min = _discrete_score_threshold(self.params.get("strong_setup_min_peer_score", 2), 3, minimum=0)
        strong_setup = bool(self.params.get("strong_setup_runner_enabled", True)) and ltf_score >= strong_setup_trigger_min and float(level["level_score"]) >= float(self.params.get("strong_setup_min_level_score", 3.4)) and directional_peer_score >= strong_setup_peer_min and strong_vote_edge >= int(self.params.get("strong_setup_min_htf_vote_edge", 1))
        target_offset = max(0, int(self.params.get("strong_setup_target_level_offset", 1)))
        # The shared entry stage (2026-09-24). It gates on the 5m LTF the
        # setup was read on (amendment 4) and builds S/R on the 1m frame;
        # the FVG term and the runner's continuation bias stay on the 1m
        # frame, where this strategy has always read them. The proposal's
        # target is the FARTHEST qualifying rung: the refinement may only
        # cap it, and the ladder keeps every rung up to the cap -- proposing
        # the selected rung would cut the ladder's farther rungs off.
        admitted = self.entry_policy.admit(EntryProposal(
            candidate=c, direction=side, style="key_level", style_family="peer",
            close=close, stop=stop, target=float(qualifying_target_levels[-1]["price"]),
            gate_frame=ltf, sr_frame=frame, level_frame=ltf, zone_frame=frame, data=data,
            htf_ctx=htf, failure_key=failure_style,
        ))
        if admitted is None:
            return None
        # The ladder is priced from the refined stop: re-qualify the rungs at
        # min_rr from it, none past the (possibly capped) refined target.
        target_cap = float(admitted.target)
        qualifying_target_levels = [
            item for item in self._qualifying_target_levels(side, close, float(admitted.stop), htf, atr, rr_required)
            if (float(item["price"]) <= target_cap if side == Side.LONG else float(item["price"]) >= target_cap)
        ]
        if not qualifying_target_levels:
            self._set_build_failure(c.symbol, failure_style, f"no_qualifying_target_rr_after_refine:{rr_required:.2f}")
            return None
        target_selection_index = int(target_offset) if strong_setup and len(qualifying_target_levels) > target_offset else 0
        target = float(qualifying_target_levels[target_selection_index]["price"])
        continuation_bias = float(admitted.fvg["fvg_continuation_bias"])
        runner_allowed = bool(strong_setup or continuation_bias >= 0.40)
        management = self._adaptive_management_components(side, close, float(admitted.stop), target, style="peer", runner_allowed=runner_allowed, continuation_bias=continuation_bias, strong_setup=strong_setup)
        macro_agreement_count = long_agree if side == Side.LONG else short_agree
        source_priority = float(level.get("source_priority", self._peer_level_source_priority(str(level.get("kind") or ""))) or 0.0)
        level_selection_score = float(level.get("selection_score", 0.0) or 0.0)
        level_selection_ltf_score = float(level.get("selection_ltf_score", ltf_score) or ltf_score)
        extra_clearance_atr = 0.0 if target_clearance is None else max(0.0, float(target_clearance.get("clearance_atr", 0.0) or 0.0) - float(self._support_resistance_setting("entry_min_clearance_atr", 0.85)))
        htf_vote_bonus = max(0.0, float(strong_vote_edge)) * 0.30
        macro_bonus = (float(macro_agreement_count) * 0.15) if macro_confirmation_enabled else 0.0
        clearance_bonus = min(extra_clearance_atr, 2.5) * 0.20
        source_priority_bonus = source_priority * 0.35
        strong_setup_bonus = 0.60 if strong_setup else 0.0
        activity_weight = max(0.0, float(self.params.get("activity_score_weight", 0.11)))
        # The peers' term reads the side's directional score, the one its
        # peer gate passed (``_directional_peer_score``: every peer with no
        # fresh vote counted against it, 2026-10-13). Until then the strong
        # setup and this sum read abs(peer_score), the fresh votes alone,
        # which equalled the directional score once the gate had passed.
        # The strategy's own score. emit adds the shared context score on
        # top: the FVG term and the HTF RSI divergence term (read off this
        # strategy's own HTF context, handed to admit as htf_ctx) -- the two
        # this sum carried itself until 2026-09-24 -- plus any other shared
        # score term the YAML switches on.
        strategy_score = (
            float(level["level_score"])
            + float(trigger["score"])
            + (directional_peer_score * 0.25)
            + source_priority_bonus
            + htf_vote_bonus
            + macro_bonus
            + clearance_bonus
            + strong_setup_bonus
            + (float(c.activity_score) * activity_weight)
        )
        # final_priority_score + 0.20 x the level's selection score, as it
        # was: the side pick and the rank tail read it.
        selection_quality_score = strategy_score + float(admitted.shared_context_score) + (level_selection_score * 0.20)
        reason = f"peer_confirmed_key_level_{'long' if side == Side.LONG else 'short'}"
        return self.entry_policy.emit(
            admitted,
            reason=reason,
            strategy_score=strategy_score,
            management=management,
            target=target,
            ladder_meta=self._ladder_management_metadata(side, level, qualifying_target_levels, target_selection_index),
            metadata={
                "level_kind": str(level["kind"]),
                "level_price": float(level["price"]),
                "level_score": float(level["level_score"]),
                "blended_level_score": float(level.get("blended_level_score", level["level_score"]) or level["level_score"]),
                "raw_htf_score": float(level.get("raw_htf_score", level["level_score"]) or level["level_score"]),
                "local_confluence_score": float(level.get("local_confluence_score", 0.0) or 0.0),
                "local_quality_score": float(level.get("local_quality_score", level["level_score"]) or level["level_score"]),
                "level_score_raw_htf_weight": float(level.get("level_score_raw_htf_weight", self._level_score_raw_htf_weight()) or self._level_score_raw_htf_weight()),
                "source_priority": source_priority,
                "selection_score": level_selection_score,
                "selection_ltf_score": round(float(level_selection_ltf_score), 4),
                "selection_distance_atr": float(level.get("selection_distance_atr", 0.0) or 0.0),
                "ltf_score": round(float(ltf_score), 4),
                # The gate's input (min_ltf_score); read as recorded: until
                # 2026-10-13 a base of 0 (min_ltf_score 0) read as ltf_score.
                "ltf_base_score": round(ltf_base_score, 4),
                "ltf_quality_bonus": round(float(trigger.get("quality_bonus", 0.0) or 0.0), 4),
                "ltf_quality_reasons": list(trigger.get("quality_reasons", [])),
                "ltf_quality_breakdown": dict(trigger.get("quality_breakdown", {})),
                "ltf_candle_matches": list(trigger.get("ltf_candle_matches", [])),
                "ltf_candle_anchor_pattern": trigger.get("ltf_candle_anchor_pattern"),
                "ltf_candle_anchor_bars": int(trigger.get("ltf_candle_anchor_bars", 0) or 0),
                "ltf_candle_score": float(trigger.get("ltf_candle_score", 0.0) or 0.0),
                "ltf_candle_net_score": float(trigger.get("ltf_candle_net_score", 0.0) or 0.0),
                "ltf_candle_opposite_score": float(trigger.get("ltf_candle_opposite_score", 0.0) or 0.0),
                "ltf_candle_regime_hint": str(trigger.get("ltf_candle_regime_hint", "neutral") or "neutral"),
                # min_ltf_score: the requirement on ltf_base_score, not on
                # ltf_score (base + bonus), since 2026-10-13.
                "ltf_score_required": float(ltf_min_score),
                "ltf_reasons": list(trigger["reasons"]),
                "min_peer_score_required": min_peer_score,
                "strong_setup_ltf_score_required": float(strong_setup_trigger_min),
                "strong_setup_peer_score_required": strong_setup_peer_min,
                "htf_bias": bias,
                "htf_bull_votes": bull_votes,
                "htf_bear_votes": bear_votes,
                "htf_vote_edge": int(strong_vote_edge),
                "peer_score": peer_score,
                "directional_peer_score": directional_peer_score,
                "peer_bullish": int(peer_ctx.get("bullish", 0)),
                "peer_bearish": int(peer_ctx.get("bearish", 0)),
                "peer_details": dict(peer_ctx.get("details", {})),
                "peer_universe": list(peer_ctx.get("universe", [])),
                "macro_long_agree": int(macro_ctx.get("long_agree", 0)),
                "macro_short_agree": int(macro_ctx.get("short_agree", 0)),
                "macro_agreement_count": int(macro_agreement_count),
                "macro_details": dict(macro_ctx.get("details", {})),
                "nearest_target_level_kind": None if target_clearance is None else str(target_clearance.get("kind") or "target"),
                "nearest_target_level_price": None if target_clearance is None else float(target_clearance.get("price", 0.0) or 0.0),
                "nearest_target_clearance_pct": None if target_clearance is None else float(target_clearance.get("clearance_pct", 0.0) or 0.0),
                "nearest_target_clearance_atr": None if target_clearance is None else float(target_clearance.get("clearance_atr", 0.0) or 0.0),
                "activity_score": float(c.activity_score),
                "activity_score_weight": float(activity_weight),
                "setup_quality_score": round(float(level["level_score"]) + float(trigger["score"]) + (directional_peer_score * 0.25), 4),
                "execution_quality_score": round(float(admitted.fvg["fvg_entry_adjustment"]) + source_priority_bonus + htf_vote_bonus + macro_bonus + clearance_bonus + strong_setup_bonus, 4),
                "macro_score": round(float(macro_bonus), 4),
                "selection_quality_score": float(selection_quality_score),
                "regime_score": float(level["level_score"]),
                "selection_level_score": float(level.get("selection_level_score", level.get("level_score", 0.0)) or level.get("level_score", 0.0)),
                "selection_raw_htf_score": float(level.get("selection_raw_htf_score", level.get("raw_htf_score", level.get("level_score", 0.0))) or level.get("raw_htf_score", level.get("level_score", 0.0))),
                "selection_local_quality_score": float(level.get("selection_local_quality_score", level.get("local_quality_score", level.get("level_score", 0.0))) or level.get("local_quality_score", level.get("level_score", 0.0))),
                "directional_vote_edge": int(strong_vote_edge),
                "execution_headroom_score": 0.0 if target_clearance is None else float(target_clearance.get("clearance_atr", 0.0) or 0.0),
                "source_quality_score": float(source_priority),
                "runner_quality_score": 1 if strong_setup else 0,
                "runner_target_applied": bool(strong_setup and len(qualifying_target_levels) > target_offset),
                "qualifying_target_count": int(len(qualifying_target_levels)),
                **self._htf_lists(htf),
            },
        )

    def strategy_exit_signal(self, position: Position, bars: dict[str, pd.DataFrame], tape: ExitTape, data=None) -> ExitDecision | None:
        # Only the ladder defence is peer-specific. The technical exits this
        # override used to run itself -- and the time stop, chart, candle,
        # structure and S/R exits it skipped -- are the shared families, which
        # now run first for every peer as its own YAML configures them
        # (2026-09-24). Only the reason label can differ when a shared family
        # and the ladder would both fire on the same cycle.
        frame = bars[str(position.metadata.get("underlying") or position.symbol)]
        return self._ladder_exit_signal(position, frame, tape, data=data)

    # -- The side template htf_pivots and trend_continuation run -----------
    # key_levels keeps its own level-first loop: it gates each side on its
    # selected level, ignores the screener's directional bias and picks the
    # side on (final_priority_score, selection_quality_score).

    def _skip_untradable(self, symbol: str, tradable: set[str]) -> bool:
        """Record ``symbol_not_tradable`` for a candidate outside a non-empty
        ``tradable`` list; True when it was skipped."""
        if tradable and symbol not in tradable:
            self._record_entry_decision(symbol, "skipped", ["symbol_not_tradable"])
            return True
        return False

    def _preferred_sides(self, c: Candidate) -> list[Side] | None:
        """The sides to evaluate, the screener's bias first. None when the
        bias is SHORT and shorts are off: the candidate is skipped
        (``shorts_disabled``) rather than read LONG against its bias."""
        allow_short = bool(self.config.risk.allow_short)
        if c.directional_bias == Side.SHORT:
            return [Side.SHORT, Side.LONG] if allow_short else None
        return [Side.LONG, Side.SHORT] if allow_short else [Side.LONG]

    def _evaluate_sides(self, c: Candidate, side_order: list[Side], build: Callable[[Side], Signal | None],
                        *, extra: tuple[str, ...] = ()) -> SideEvaluation:
        """Build each side in ``side_order`` and collect what the decision
        log needs: each built signal's (or refusal's) ``side_eval`` and
        ``extra`` fields, the near-miss blockers, and every blocker of a
        refused side -- this side's own, then the shared vetoes. A refusal's
        details were written before the shared entry stage ran, so its
        reasons, not its details, are the full list."""
        out = SideEvaluation(extra={key: {} for key in extra})
        for side in side_order:
            side_key = str(side.value).lower()
            signal = build(side)
            if signal is not None:
                out.signals.append(signal)
                meta = signal.metadata if isinstance(signal.metadata, dict) else {}
                out.side_eval[side_key] = meta.get("side_eval")
                for key in extra:
                    out.extra[key][side_key] = meta.get(key)
                continue
            failure = self._consume_build_failure_payload(c.symbol, self._failure_style_name(side))
            if failure is None:
                reasons = [f"{side_key}_setup_not_ready"]
            else:
                details = failure.get("details") if isinstance(failure.get("details"), dict) else {}
                out.side_eval[side_key] = details.get("side_eval")
                for key in extra:
                    out.extra[key][side_key] = details.get(key)
                for gate, value in details.get("near_miss_blockers", {}).items():
                    out.near_miss_blockers[f"{side_key}.{gate}"] = value
                reasons = failure.get("reasons") or [failure.get("primary_reason") or f"{side_key}_setup_not_ready"]
            for token in side_prefixed_reasons(side, reasons):
                if token not in out.fail_reasons:
                    out.fail_reasons.append(token)
        return out

    def _pick_side_signal(self, signals: list[Signal], c: Candidate) -> Signal:
        """The side entered when both sides built: the gatekeeper's rank key
        (the manifest's signal_priority), then the screener's preferred
        side."""
        def _key(signal: Signal) -> tuple[float, ...]:
            preferred = 1.0 if c.directional_bias is not None and signal.side == c.directional_bias else 0.0
            return self.entry_policy.rank_key(signal, c) + (preferred,)
        return max(signals, key=_key)

    @staticmethod
    def _completed_ltf(ltf: pd.DataFrame | None, ltf_minutes: int, now: Any) -> pd.DataFrame | None:
        """``ltf`` without its last bucket when that bucket has not ended by
        ``now`` (``bars.completed_bucket_mask``): the resample of the step
        frame keeps the bucket still forming, 1-5 completed minutes of a 5m
        one. A 1m frame of completed bars is returned whole."""
        if ltf is None or ltf.empty or bool(completed_bucket_mask(ltf.index[-1:], ltf_minutes, now)[0]):
            return ltf
        return ltf.iloc[:-1]

    @staticmethod
    def _step_frame_end(frame: pd.DataFrame) -> pd.Timestamp:
        """When the step frame's newest bar ended: its start plus the minute
        a step frame's bar spans. A trigger bucket is complete once this
        reaches its end, so a bucket whose last minute's bar has not arrived
        yet (CHART_EQUITY delivers it a moment after the minute closes) is
        not read as complete on the clock alone."""
        return pd.Timestamp(frame.index[-1]) + pd.Timedelta(minutes=1)

    @staticmethod
    def _bucket_lateness(ltf: pd.DataFrame, ltf_minutes: int, data_clock: pd.Timestamp) -> pd.Timedelta:
        """How long after its end ``ltf``'s last (completed) bucket is read:
        the step frame's data clock (``_step_frame_end``) less the bucket's
        end (``bars.session_bucket_ends``). 0 when the bucket's last
        minute's bar is the step frame's newest."""
        return data_clock - session_bucket_ends(ltf.index[-1:], ltf_minutes)[0]

    def _miss_bucket(self, symbol: str, bucket: pd.Timestamp, late: pd.Timedelta) -> None:
        """Skip ``symbol``'s completed bucket ``bucket``, first seen ``late``
        past its end (more than ``LTF_BUCKET_MAX_LATE``), unjudged:
        ``ltf_bucket_missed(bucket=HH:MM,late=Xm)``, a divergence-ineligible
        token, kept as the bucket's verdict, so every later pass repeats it
        until the next bucket completes."""
        reason = f"ltf_bucket_missed(bucket={bucket.strftime('%H:%M')},late={late.total_seconds() / 60.0:g}m)"
        details = {"ltf_bucket": bucket.isoformat()}
        self._record_entry_decision(symbol, "skipped", [reason], details=details)
        self._judged_buckets[symbol] = _BucketVerdict(bucket, False, [reason], details)

    def _entry_cutoff(self, side: Side, now_dt: datetime) -> tuple[time, time] | None:
        """``side``'s entry cutoff on ``now_dt``'s day and the force-flatten
        cutoff it leads (``_force_flatten_cutoff``, the one
        ``should_force_flatten`` reads): ``entry_cutoff_lead_minutes`` before
        it (10 in the manifests and the presets: key_levels' 19:40 before
        its 19:50 flatten, 12:45 on an early-close day). None when that side
        is not flattened."""
        flatten = self._force_flatten_cutoff(side, now_dt)
        if flatten is None:
            return None
        minutes = max(0, flatten.hour * 60 + flatten.minute - int(self.params["entry_cutoff_lead_minutes"]))
        return time(minutes // 60, minutes % 60), flatten

    def _entry_cutoffs_passed(self, sides: list[Side], now_dt: datetime) -> dict[Side, str]:
        """The sides of ``sides`` past their entry cutoff at ``now_dt``
        (``_entry_cutoff``), each with ``cutoff=HH:MM,flatten=HH:MM``. A
        position opened on such a side would be flattened within
        ``entry_cutoff_lead_minutes`` of opening -- on its first management
        pass, past the flatten cutoff -- paying two crossings of the spread
        for nothing, key_levels' in the post-market. Until 2026-10-13 a
        SHORT could enter between the 19:50 cutoff and the 19:55 end of the
        entry window, and the 19:40 bucket, judged at 19:45, could open a
        position the 19:50 flatten closed."""
        passed: dict[Side, str] = {}
        for side in sides:
            cutoffs = self._entry_cutoff(side, now_dt)
            if cutoffs is not None and now_dt.time() >= cutoffs[0]:
                passed[side] = f"cutoff={cutoffs[0]:%H:%M},flatten={cutoffs[1]:%H:%M}"
        return passed

    def _entry_sides_now(self) -> tuple[list[Side], dict[Side, str]]:
        """The sides this entry pass may open -- LONG, and SHORT while
        ``risk.allow_short`` is on -- less those past their entry cutoff on
        the ET clock, and those, each with its cutoffs
        (``_entry_cutoffs_passed``). Every entry pass of the family reads it
        once a pass: htf_pivots' and trend_continuation's too since
        2026-10-13 (htf_pivots' afternoon window runs to 15:45, past its
        15:40 entry cutoff, and both run past an early-close day's)."""
        trade_sides = [Side.LONG] + ([Side.SHORT] if bool(self.config.risk.allow_short) else [])
        passed = self._entry_cutoffs_passed(trade_sides, sessions.now_et())
        return [side for side in trade_sides if side not in passed], passed

    def _skip_past_cutoff(self, symbol: str, open_sides: list[Side], passed: Mapping[Side, str]) -> bool:
        """With no side open (``_entry_sides_now``), record ``symbol`` as
        skipped ``after_entry_cutoff(cutoff=HH:MM,flatten=HH:MM)`` -- a
        divergence-ineligible token -- before anything is read; True when
        it was skipped."""
        if open_sides:
            return False
        detail = "|".join(sorted(set(passed.values())))
        self._record_entry_decision(symbol, "skipped", [f"after_entry_cutoff({detail})"])
        return True

    @staticmethod
    def _closed_side_reasons(passed: Mapping[Side, str]) -> list[str]:
        """``<side>.after_entry_cutoff(cutoff=HH:MM,flatten=HH:MM)`` for each
        side past its entry cutoff: what a skipped symbol's decision ends
        with while another side stays open."""
        return [f"{side.value.lower()}.after_entry_cutoff({detail})" for side, detail in passed.items()]

    def closed_entry_sides(self, now_dt: datetime) -> frozenset[Side]:
        """The sides past their entry cutoff at ``now_dt``
        (``_entry_cutoffs_passed``): the family's entry passes open neither,
        and the shared stage's divergence-only entries skip them too
        (``BaseStrategy.closed_entry_sides``, 2026-10-13; until then nothing
        kept a divergence-only entry off a side past its force-flatten
        cutoff, nor did the family's own entry passes)."""
        return frozenset(self._entry_cutoffs_passed([Side.LONG, Side.SHORT], now_dt))

    def _repeat_bucket_verdict(self, symbol: str, verdict: _BucketVerdict) -> None:
        """Record the verdict judged on ``symbol``'s last completed bucket
        again, on a pass before the next one completes: a refusal (or a
        missed bucket, ``_miss_bucket``) with its reasons and details
        (``ltf_bucket_repeat`` set), a signal as
        ``ltf_bucket_signalled(bucket=HH:MM)`` -- the entry stage took it or
        refused it on that pass, and it is not offered again."""
        bucket = verdict.bucket.strftime("%H:%M")
        if verdict.signalled:
            self._record_entry_decision(symbol, "skipped", [f"ltf_bucket_signalled(bucket={bucket})"],
                                        details={**verdict.details, "ltf_bucket_repeat": True})
            return
        self._record_entry_decision(symbol, "skipped", list(verdict.reasons),
                                    details={**verdict.details, "ltf_bucket_repeat": True})

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        """Each candidate's setup on its last COMPLETED trigger bucket
        (``_judge_candidate``), judged once per bucket. A candidate whose
        judgement raises is skipped alone, fail closed, and the pass goes
        on (``_entry_pass_failed``): no verdict is kept for its bucket, and
        the signals the other candidates built still reach the entry stage
        (2026-10-13; until then the raise ended the pass, so a bucket judged
        earlier in it kept a verdict for a signal the stage never saw, and
        read as signalled for the rest of the bucket)."""
        self._reset_entry_decisions()
        open_sides, cutoffs_passed = self._entry_sides_now()
        state = _EntryPass(
            min_level_score=float(self.params["min_level_score"]),
            htf_request=self._symbol_htf_request(),
            ltf_minutes=int(self.params["ltf_minutes"]),
            min_bars=int(self.params["min_bars"]),
            min_ltf_bars=int(self.params["min_ltf_bars"]),
            tradable=frozenset(self._tradable_symbols()),
            open_sides=open_sides,
            cutoffs_passed=cutoffs_passed,
        )
        out: list[Signal] = []
        for c in candidates:
            try:
                signal = self._judge_candidate(c, bars, positions, data, state)
            except Exception as exc:
                self._entry_pass_failed(c.symbol, exc)
                continue
            self._entry_error_streaks.pop(c.symbol, None)
            if signal is not None:
                out.append(signal)
        return out

    def _entry_pass_failed(self, symbol: str, exc: Exception) -> None:
        """``symbol``'s judgement raised ``exc``: record it skipped as
        ``entry_pass_error(error=<type>)`` -- a divergence-ineligible token,
        so no divergence-only entry opens it either -- and keep no verdict
        for its bucket, so the next pass judges it again while it is in
        time (``LTF_BUCKET_MAX_LATE``). Logged with the error's type: the
        traceback on the first failure of a run of consecutive ones for the
        symbol and every ``ENTRY_ERROR_TRACEBACK_EVERY``-th after, a one-line
        WARNING in between; a judgement that completes ends the run."""
        streak = self._entry_error_streaks.get(symbol, 0) + 1
        self._entry_error_streaks[symbol] = streak
        lines = str(exc).splitlines()
        error = f"{type(exc).__name__}: {lines[0]}" if lines else type(exc).__name__
        if streak == 1 or streak % ENTRY_ERROR_TRACEBACK_EVERY == 0:
            LOG.error("%s: the entry pass failed for %s (consecutive=%d): %s; no entry for it this pass",
                      self.strategy_name, symbol, streak, error, exc_info=exc)
        else:
            LOG.warning("%s: the entry pass failed for %s (consecutive=%d): %s; no entry for it this pass",
                        self.strategy_name, symbol, streak, error)
        self._record_entry_decision(symbol, "skipped", [f"entry_pass_error(error={type(exc).__name__})"])

    def _judge_candidate(self, c: Candidate, bars: dict[str, pd.DataFrame], positions: dict[str, Position],
                         data, state: _EntryPass) -> Signal | None:
        """``c``'s setup on its last COMPLETED trigger bucket
        (``_completed_ltf``: the level touch, the trigger, the ATR and the
        swing stop read completed buckets only), judged once per bucket: the
        first pass that sees a bucket complete judges it, and an entry can
        fire on that pass; every later pass repeats its verdict
        (``_repeat_bucket_verdict``) until the next bucket completes. The
        verdict is kept only once the judgement is done. Until 2026-10-13
        the trigger read the forming bucket, a partial 5m candle (82% of the
        time) against full ones, up to five looks a bucket.

        A bucket is judged only while the step frame ends within one 1m step
        of the bucket's end (``LTF_BUCKET_MAX_LATE``); one first seen later
        is skipped unjudged (``_miss_bucket``,
        ``ltf_bucket_missed(bucket=HH:MM,late=Xm)``) and the pass waits for
        the next bucket: a signal never reads evidence that closed minutes
        before the price it is entered at. Nor is it judged while the
        symbol's 1m frame owes a backfill (``data.pending_stream_backfill``:
        the stream (re)started, or reconnected after missing bars, and no
        price_history fetch has covered the gap yet): the pass records
        ``ltf_bucket_backfill_pending(bucket=HH:MM,first_bar=HH:MM)`` and
        keeps no verdict, so a later pass judges the bucket once the
        backfill is in, or misses it once it is late (2026-10-13; until
        then the first look judged the holed bucket and its verdict stood
        after the backfill repaired it).

        No side is opened past its entry cutoff (``_entry_sides_now``); with
        every side past it the symbol is skipped as
        ``after_entry_cutoff(cutoff=HH:MM,flatten=HH:MM)``. The macro votes
        are read once a pass, on the first setup it judges."""
        if self._skip_untradable(c.symbol, state.tradable):
            return None
        frame = bars.get(c.symbol)
        if c.symbol in positions:
            self._record_entry_decision(c.symbol, "skipped", ["already_in_position"])
            return None
        if self._skip_past_cutoff(c.symbol, state.open_sides, state.cutoffs_passed):
            return None
        if frame is None or len(frame) < state.min_bars:
            self._record_entry_decision(c.symbol, "skipped", [insufficient_bars_reason("insufficient_bars", 0 if frame is None else len(frame), state.min_bars)])
            return None
        ltf_min = state.ltf_minutes
        data_clock = self._step_frame_end(frame)
        ltf = self._completed_ltf(self._resampled_frame(frame, ltf_min), ltf_min, data_clock)
        if ltf is None or len(ltf) < state.min_ltf_bars:
            self._record_entry_decision(c.symbol, "skipped", [insufficient_bars_reason("insufficient_ltf_bars", 0 if ltf is None else len(ltf), state.min_ltf_bars)])
            return None
        bucket = pd.Timestamp(ltf.index[-1])
        judged = self._judged_buckets.get(c.symbol)
        if judged is not None and judged.bucket == bucket:
            self._repeat_bucket_verdict(c.symbol, judged)
            return None
        late = self._bucket_lateness(ltf, ltf_min, data_clock)
        if late > LTF_BUCKET_MAX_LATE:
            self._miss_bucket(c.symbol, bucket, late)
            return None
        bucket_details = {"ltf_bucket": bucket.isoformat()}
        if data is not None:
            owed = data.pending_stream_backfill(c.symbol)
            if owed is not None:
                self._record_entry_decision(
                    c.symbol, "skipped",
                    [f"ltf_bucket_backfill_pending(bucket={bucket:%H:%M},first_bar={pd.Timestamp(owed):%H:%M})"],
                    details=dict(bucket_details))
                return None
        if state.macro_ctx is None:
            state.macro_ctx = self._macro_signal(bars, data=data)
        macro_ctx = state.macro_ctx
        open_sides = state.open_sides
        min_level_score = state.min_level_score
        reasons: list[str] = []
        close = safe_float(frame.iloc[-1]["close"], 0.0)
        htf = self._htf_context(c.symbol, data, current_price=close, **state.htf_request)
        symbol_peer_ctx = self._peer_signal(c.symbol, bars, data)
        long_level = self._select_level(Side.LONG, close, ltf, htf) if Side.LONG in open_sides else None
        short_level = self._select_level(Side.SHORT, close, ltf, htf) if Side.SHORT in open_sides else None
        signals: list[Signal] = []
        if long_level is not None:
            long_level_score = float(long_level["level_score"])
            if long_level_score >= min_level_score:
                sig = self._build_equity_signal(c, frame, ltf, htf, Side.LONG, long_level, symbol_peer_ctx, macro_ctx, data=data)
                if sig is not None:
                    signals.append(sig)
            else:
                reasons.append(f"long_level_score_below_min:{long_level_score:.2f}<{min_level_score:.2f}")
        if short_level is not None:
            short_level_score = float(short_level["level_score"])
            if short_level_score >= min_level_score:
                sig = self._build_equity_signal(c, frame, ltf, htf, Side.SHORT, short_level, symbol_peer_ctx, macro_ctx, data=data)
                if sig is not None:
                    signals.append(sig)
            else:
                reasons.append(f"short_level_score_below_min:{short_level_score:.2f}<{min_level_score:.2f}")
        evaluated_sides = [side.value for side in open_sides]
        if not signals:
            if long_level is None and short_level is None:
                reasons.append("price_not_in_htf_zone")
            else:
                # Every blocker of each side: a shared-entry refusal
                # lists all of them (2026-09-24), its own gates one.
                for side in (Side.LONG, Side.SHORT):
                    failure = self._consume_build_failure_payload(c.symbol, self._failure_style_name(side))
                    for token in side_prefixed_reasons(side, failure["reasons"] if failure else []):
                        if token and token not in reasons:
                            reasons.append(token)
                if not reasons:
                    reasons.append("setup_not_confirmed")
            reasons.extend(self._closed_side_reasons(state.cutoffs_passed))
            decision_details = {"peer_universe": list(symbol_peer_ctx.get("universe", [])), "peer_details": dict(symbol_peer_ctx.get("details", {})), "evaluated_sides": evaluated_sides, **bucket_details}
            self._record_entry_decision(c.symbol, "skipped", reasons, details=decision_details)
            self._judged_buckets[c.symbol] = _BucketVerdict(bucket, False, list(reasons), decision_details)
            return None
        best = max(
            signals,
            key=lambda sig: (
                float(sig.metadata.get("final_priority_score", 0.0) or 0.0),
                float(sig.metadata.get("selection_quality_score", sig.metadata.get("final_priority_score", 0.0)) or 0.0),
            ),
        )
        meta = best.metadata if isinstance(best.metadata, dict) else {}
        signal_details = {"peer_universe": meta.get("peer_universe"), "entry_family": meta.get("entry_family"), "evaluated_sides": evaluated_sides, **bucket_details}
        self._record_entry_decision(c.symbol, "signal", [best.reason], details=signal_details)
        self._judged_buckets[c.symbol] = _BucketVerdict(bucket, True, [best.reason], signal_details)
        return best

    def entry_spread_refusal(self, signal: Signal, bid: Any, ask: Any, limit_price: float) -> str | None:
        """The entry spread guard (``shared_entry.entry_spread_guard``) on
        ``max_entry_spread_r`` / ``max_entry_spread_target_frac``, read at
        the build: refuse an entry whose quote's spread is at least that
        many x its risk |limit - stop| or its target distance |target -
        limit|, and a quote it cannot read. Both at 0 refuse nothing and do
        not read the quote. The family had no guard until 2026-10-13;
        key_levels' preset trades 07:00-19:55, into the thin pre- and
        post-market books, on stops sized on its trigger frame's ATR (the
        other presets trade the regular session, and trend_continuation
        from 07:10)."""
        max_spread_r, max_spread_target_frac = self._entry_spread_limits
        return entry_spread_guard(bid, ask, limit_price, signal.stop_price, signal.target_price,
                                  max_spread_r=max_spread_r, max_spread_target_frac=max_spread_target_frac)

    def protective_exits_on_executable_side(self) -> bool:
        """The ``protective_exits_on_executable_side`` knob, read at the
        build (``BaseStrategy.protective_exits_on_executable_side``): judge
        a stop at or past the entry and the peak giveback floors on the bid
        (a LONG) or the ask (a SHORT) of the pass's quote instead of the
        mark (``trade_management.protective_read``)."""
        return self._protective_exits_on_executable_side
