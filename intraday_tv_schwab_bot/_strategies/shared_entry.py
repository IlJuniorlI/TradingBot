# SPDX-License-Identifier: MIT
"""The shared entry stage -- every ``shared_entry`` knob, for every strategy.

``SharedEntryPolicy`` is built by ``BaseStrategy.__init__`` as
``self.entry_policy`` and is the ONLY reader of ``config.shared_entry``. A
strategy keeps its own setup logic, alternatives and selection; for each
alternative it builds an ``EntryProposal`` and hands it to ``admit``, which
runs, in order:

- P1 the raw R:R gate the proposal asked for (``raw_rr_gate``);
- P0 the contexts on the frames the proposal declares (cached, so the
  engine's pre-warm keeps hitting);
- P2 the FVG / order-block retest admission of the proposal's deferrable
  pending reasons (``retest``);
- P3 the vetoes, in ``VETO_GATES`` order -- all of them evaluated, so every
  blocker is recorded -- minus the ones the strategy's manifest exempts for
  the proposal's style;
- P4 the stop / target: an optional ``stop_resolver``, the S/R then the
  technical refinement, the retest stop anchor (bounded by the stop floor),
  and a stop on the wrong side of entry refused;
- P5 the score: the entry-context terms, the FVG term, the divergence
  confirmation bump, and the optional ``min_shared_context_score`` floor.

A proposal ``admit`` refuses is recorded with ONE ``_set_build_failure`` call
under the proposal's failure key -- its style unless it names another --
(every blocker, the first one primary). An
admitted one comes back as an ``AdmittedEntry``, which only ``admit`` can
mint; the strategy runs its own post-admission steps on it (ladder, runner,
management, its score) and ``emit`` builds the ``Signal`` -- the only
``Signal(...)`` in the code base -- stamping the shared metadata (the style
family the exit graces key on, the shared score, the final priority score).

Why a stage the strategies call rather than a filter on their output: every
multi-alternative strategy selects inside itself (top_tier breaks on the
first regime that survives and falls through to the next, the peer and
squeeze strategies take the best side), so a gate has to act on each
alternative before the selection, and before the ladder, runner and
management are computed from the stop it may refine. Until 2026-09-24 each
strategy called the knob helpers it chose to: most knobs did nothing for most
strategies (key_levels reached three of them, the reversal strategies never
met the dual divergence veto, only top_tier met the candle filter), and a
``strategy_logic_default`` hook let a strategy rewrite any knob. The rules
are now enforced by ``tests/test_shared_knob_contract.py``.

The same module ranks the gatekeeper's signals (``rank_key``) and builds the
opt-in divergence-only entries (``divergence_entries``).
"""
from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pandas as pd

from ..models import Candidate, Position, Side, Signal
from ..utils import htf_ema_spans, now_et
from .helpers import (
    CANDLE_PATTERN_WINDOW_BARS,
    _bar_close_position,
    _bars_have_range,
    _detail_fields,
    _optional_float,
    _reason_prefix,
    _reason_with_values,
    _safe_float,
)

if TYPE_CHECKING:
    from .strategy_base import BaseStrategy


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# The closed set of style families a proposal declares. The family, not the
# strategy, is what cross-strategy consumers key on: the exit side's ORB
# grace covers every "orb" entry and its pullback grace every "pullback"
# entry, whichever strategy produced it (shared_exit.SharedExitPolicy).
STYLE_FAMILIES: frozenset[str] = frozenset({
    "trend", "pullback", "momentum", "breakout", "orb", "range", "sr_scalp",
    "vol_squeeze", "vwap_reclaim", "reversal", "pairs", "peer", "pivot",
    "continuation", "divergence", "option_debit", "option_credit", "option_long",
})

# The P3 vetoes, in evaluation (and reporting) order. A manifest exempts a
# style from any of them: capabilities.shared_entry.exemptions {style: [gate]}.
VETO_GATES: tuple[str, ...] = ("structure", "sr", "broken_level", "chart", "dual_divergence", "candle")

# Retest zones a RetestTrigger may name; the FVG plan always comes first.
RETEST_ZONES: frozenset[str] = frozenset({"fvg", "ob"})

# metadata['entry_source'] of a divergence-only entry. rank_key puts every
# such signal behind every strategy signal (tier 0).
DIVERGENCE_ENTRY_SOURCE = "shared_divergence_entry"
_DIVERGENCE_STYLES = frozenset({"divergence_regular", "divergence_hidden"})

# Skip-reason tokens (the part before "(") a strategy records for a symbol it
# never evaluated a setup on: not its symbol, already held, outside its own
# window or session, not enough or unusable data, a blackout, or every side it
# would trade switched off -- plus every ``insufficient_*`` token
# (helpers.insufficient_bars_reason). A divergence-only entry must not open
# such a symbol either; it only gets the symbols the strategy looked at and
# found no setup on (2026-09-24). The side skips count as the whole symbol,
# not the one side: rth_trend_pullback records ``shorts_disabled`` when the
# side it read from the day is SHORT, top_tier its relative-strength skip when
# the sector gate removed every side it prefers, and neither ever evaluated
# the other side there.
DIVERGENCE_INELIGIBLE_REASONS: frozenset[str] = frozenset({
    "symbol_not_tradable", "already_in_position", "underlying_already_open",
    "outside_entry_window", "after_entry_cutoff", "extended_hours_not_eligible",
    "session_empty", "last_close_invalid", "missing_ltf_context",
    "opening_range_incomplete", "opening_range_values_nan", "pm_reference_pmh_invalid",
    # top_tier's macro-window (CPI, FOMC) and per-symbol earnings blackouts.
    "event_blackout", "earnings_blackout",
    "shorts_disabled",
    "relative_strength_lagging_sector", "relative_strength_leading_sector",
})


def _disabled_fvg_score() -> dict[str, Any]:
    """The shape zero_dte's regime scoring reads when use_fvg_context is off
    (a fresh dict per call: nothing shares the nested ones)."""
    return {
        "bull_score": 0.0,
        "bear_score": 0.0,
        "directional_pressure": 0.0,
        "timeframe_minutes": 0,
        "nearest_bullish": {},
        "nearest_bearish": {},
    }


def _local_date(stamp: Any, tz: Any) -> Any:
    """``stamp``'s calendar date in ``tz`` (a naive stamp is taken as local)."""
    ts = pd.Timestamp(stamp)
    if ts.tzinfo is not None and tz is not None:
        ts = ts.tz_convert(tz)
    return ts.date()

# Set only while admit builds an AdmittedEntry (see AdmittedEntry). Per
# thread: the engine pre-warms contexts on worker threads.
_MINTING = threading.local()


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RetestTrigger:
    """What the FVG / order-block retest plans need to judge a retest of a
    breakout: the level that was broken, whether price is through it now,
    and the VWAP / EMA9 a confirming close must reclaim.

    ``zones`` names the plans: ``("fvg",)`` or ``("fvg", "ob")`` (the
    order-block plan only engages with
    ``support_resistance.ltf_order_blocks_enabled``). ``anchor_stop`` pulls
    the stop to the admitting zone's anchor when a plan allows -- bounded by
    ``shared_entry.min_stop_atr_mult`` like any stop refinement.
    """

    trigger_level: float
    breakout_active: bool
    vwap: float
    ema9: float
    zones: tuple[str, ...] = ("fvg",)
    anchor_stop: bool = True

    def __post_init__(self) -> None:
        if not self.zones or self.zones[0] != "fvg" or len(set(self.zones)) != len(self.zones) or not set(self.zones) <= RETEST_ZONES:
            raise ValueError(f"RetestTrigger.zones must be ('fvg',) or ('fvg', 'ob'), got {self.zones!r}")


@dataclass(frozen=True, slots=True)
class EntryContexts:
    """Contexts a strategy already built for its own logic, handed to
    ``admit`` so it does not build them twice. Each one must be the
    context of the frame the proposal declares for it (S/R on ``sr_frame``,
    the rest on ``gate_frame``); None fields are built by ``admit``."""

    sr: Any = None
    ms: Any = None
    tech: Any = None
    chart: Any = None


@dataclass(slots=True)
class EntryProposal:
    """One candidate entry, as the strategy's own logic built it.

    - ``direction``: the MARKET direction. For an option it is the
      underlying's: a bull put credit spread is sold SHORT but is a LONG
      proposal, a bearish debit spread is bought LONG but is a SHORT one.
      ``emit`` takes the order side separately.
    - ``style``: the regime / style token -- the manifest exemption key,
      the ``regime`` stamp and, unless ``failure_key`` is set, the
      build-failure key.
    - ``failure_key``: the key a refusal is recorded under when the
      strategy's loop consumes its failures under another token than the
      style (htf_pivots: its per-side key, while its style is the pivot
      family its exemption names); None = ``style``.
    - ``style_family``: one of ``STYLE_FAMILIES``; stamped as
      ``entry_style_family``.
    - ``stop`` / ``target``: price levels, or None for a premium proposal
      (``level_frame`` None: no refinement, no stop checks). ``target`` None
      on a price-level proposal is a runner. ``stop`` may be None when a
      ``stop_resolver`` computes it from the retest plans.
    - ``gate_frame``: the frame the structure, technical, chart and candle
      contexts and vetoes read; ``sr_frame``: the frame the S/R context is
      built from (and the broken-level guard's ATR, and the divergence
      candidate); ``level_frame``: the ATR the refinement clamp reads;
      ``zone_frame``: the frame the FVG / order-block reads use -- the
      retest plans and the FVG score term -- None = ``gate_frame``
      (key_levels gates on its 5m LTF but has always scored FVGs on the 1m
      frame).
    - ``pending_reasons``: the strategy's own blockers. They reject the
      proposal unless the retest admission clears them, which it does only
      when every one of them starts with a prefix in ``deferrable``.
    - ``raw_rr_gate``: when set, the raw stop / target must clear
      ``min_target_rr`` before anything else runs, else the proposal is
      refused as ``<side>_<raw_rr_gate>(...)``.
    - ``htf_ctx``: the HTF context the HTF-divergence score term reads;
      None = the strategy's default (``_default_htf_context_for_score``).
    - ``vol_scale``: scales the broken-level guard's percent clearance.
    - ``failure_details``: recorded with a refusal (gate snapshots,
      near-miss data).
    - ``symbol``: the traded symbol when it is not the candidate's
      (pairs_residual's leg); defaults to ``candidate.symbol``.
    """

    candidate: Candidate
    direction: Side
    style: str
    style_family: str
    close: float
    stop: float | None
    target: float | None
    gate_frame: pd.DataFrame
    sr_frame: pd.DataFrame
    level_frame: pd.DataFrame | None
    data: Any = None
    pending_reasons: tuple[str, ...] = ()
    deferrable: frozenset[str] = frozenset()
    retest: RetestTrigger | None = None
    stop_resolver: Callable[[tuple[dict[str, Any], ...]], float | None] | None = None
    raw_rr_gate: str | None = None
    htf_ctx: Any = None
    contexts: EntryContexts | None = None
    vol_scale: float = 1.0
    failure_details: Mapping[str, Any] | None = None
    symbol: str = ""
    zone_frame: pd.DataFrame | None = None
    failure_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.direction, Side):
            raise TypeError(f"EntryProposal.direction must be a Side, got {self.direction!r}")
        if not str(self.style or "").strip():
            raise ValueError("EntryProposal.style must be a non-empty token")
        if self.failure_key is not None and not str(self.failure_key).strip():
            raise ValueError("EntryProposal.failure_key must be None or a non-empty token")
        if self.style_family not in STYLE_FAMILIES:
            raise ValueError(f"EntryProposal.style_family {self.style_family!r} is not one of {sorted(STYLE_FAMILIES)}")
        self.pending_reasons = tuple(str(reason) for reason in self.pending_reasons)
        self.deferrable = frozenset(self.deferrable)
        if not self.symbol:
            self.symbol = str(self.candidate.symbol)
        if self.deferrable and self.retest is None:
            raise ValueError("EntryProposal.deferrable needs a RetestTrigger: only a retest defers a reason")
        if self.level_frame is None:
            if self.stop is not None or self.target is not None:
                raise ValueError("a premium proposal (level_frame None) carries no price stop / target")
            if self.retest is not None or self.stop_resolver is not None or self.raw_rr_gate is not None:
                raise ValueError("a premium proposal (level_frame None) has no retest, stop_resolver or raw_rr_gate")
        elif self.stop is None and self.stop_resolver is None:
            raise ValueError("a price-level proposal needs a stop or a stop_resolver")
        if self.raw_rr_gate is not None and self.stop is None:
            raise ValueError("raw_rr_gate judges the raw stop, so the proposal must carry one")


@dataclass(frozen=True, slots=True)
class AdmittedEntry:
    """A proposal that passed ``admit``, with the refined levels and every
    context and score term ``admit`` computed. Only ``admit`` can mint one.

    - ``candle_signal``: ``_directional_candle_signal(gate_frame,
      direction)`` -- the strategy's candle bonus reads it.
    - ``retest_plans`` / ``admitted_via_retest``: the plans ``admit`` built
      and whether one of them confirmed the retest (status ``allow``); a
      strategy's reason suffix and the stop anchor key on the latter, as
      they keyed on the FVG plan's status before 2026-09-24.
    - ``fvg``: the full FVG components, including ``fvg_continuation_bias``
      and ``fvg_reversal_bias`` (for the strategy's runner and management).
    - ``adjustments``: the entry-context components (S/R proximity,
      technical, HTF divergence and their sum).
    - ``divergence_confirmation``: the divergence entry candidate's verdict
      on this proposal when ``use_divergence_entry_signal`` is on.
    - ``gates_abstained``: applied vetoes that could not judge the tape and
      let the proposal through (the candle veto on single-print bars).
    """

    proposal: EntryProposal
    stop: float | None
    target: float | None
    sr: Any
    ms: Any
    tech: Any
    chart: Any
    htf: Any
    candle_signal: dict[str, Any]
    retest_plans: tuple[dict[str, Any], ...]
    admitted_via_retest: bool
    fvg: dict[str, Any]
    adjustments: dict[str, float]
    shared_context_score: float
    divergence_confirmation: dict[str, Any] | None
    gates_applied: tuple[str, ...]
    gates_exempted: tuple[str, ...]
    gates_abstained: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # dataclasses.replace() rebuilds through __init__ too, so an admitted
        # entry cannot be copied with a different stop or target either.
        if not getattr(_MINTING, "active", False):
            raise TypeError("AdmittedEntry is minted only by SharedEntryPolicy.admit")

    @property
    def direction(self) -> Side:
        return self.proposal.direction

    @property
    def close(self) -> float:
        return float(self.proposal.close)


# ---------------------------------------------------------------------------
# S/R clearance (module-level: the veto and the proximity score share them)
# ---------------------------------------------------------------------------

def _htf_clearance(sr_ctx, kind: str) -> tuple[float | None, float | None]:
    """``(pct, atr)`` room between price and the HTF ``kind`` level an entry
    must not crowd: the support under a SHORT, the resistance over a LONG.

    A pending level -- a support price has crossed below (a resistance it has
    crossed above) whose flip is not yet confirmed -- still plays its original
    role, and it is the nearest level of that role: price sits on its far
    side, so the room is NEGATIVE and always inside the minimum clearance.
    ``nearest_support`` / ``nearest_resistance`` never hold a crossed level,
    so until 2026-09-23 the clearance checks measured a short pressed right
    under an unconfirmed-lost support (a long just above an unconfirmed-broken
    resistance) against the NEXT level down (up) and let it through: exactly
    the failed-break entry the flip confirmation exists to stop. Over 4,144
    archived RTH samples (09-21/09-22, every 5 min, shipped 0.72 ATR / 0.25%
    minimums) that let 238 longs (5.7%) and 123 shorts (3.0%) through. A
    pending level sits close by construction (p99 0.51 ATR for supports, 0.88
    for resistances), so "always too close" differs from an absolute-distance
    rule in 5 of those 573 samples.
    """
    close = float(sr_ctx.current_price)
    if kind == "support":
        pending = sr_ctx.pending_support
        if pending is None:
            return sr_ctx.support_distance_pct, sr_ctx.support_distance_atr
        room = close - float(pending.price)
    else:
        pending = sr_ctx.pending_resistance
        if pending is None:
            return sr_ctx.resistance_distance_pct, sr_ctx.resistance_distance_atr
        room = float(pending.price) - close
    atr = float(sr_ctx.current_atr or 0.0)
    return (room / close if close > 0 else None), (room / atr if atr > 0 else None)


def _sr_flag_blocks(sr_ctx, side: Side) -> bool:
    """Whether ``breakdown_below_support`` blocks a LONG (``breakout_above_
    resistance`` a SHORT): only while no level stands on the entry's side of
    price -- a LONG with a support under it, a SHORT with a resistance over it,
    goes on to the clearance check instead.

    That is nearly always. Over 5,517 archived RTH checkpoints (09-18/21/22)
    breakdown_below_support was set on 1,840 and a support stood under price
    on 1,825 of them; breakout_above_resistance on 2,890, a resistance over
    price on 2,811. So in practice the flags block only a side with no level
    at all, and have since the 2026-05-13 exception. That change's stated
    reason -- a flag lingering after the level was reclaimed -- cannot
    happen: both flags are recomputed on every build from the broken level
    and the last bar. Blocking on the flags alone would stop a third of LONG
    and half of SHORT evaluations; that is a strategy decision, not this
    helper's.
    """
    close = float(getattr(sr_ctx, "current_price", 0.0) or 0.0)
    if side == Side.LONG:
        if not bool(sr_ctx.breakdown_below_support):
            return False
        level = getattr(sr_ctx, "nearest_support", None)
        return not (level is not None and 0 < float(level.price) < close)
    if not bool(sr_ctx.breakout_above_resistance):
        return False
    level = getattr(sr_ctx, "nearest_resistance", None)
    return not (level is not None and 0 < close < float(level.price))


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

class SharedEntryPolicy:
    """See the module docstring. One per strategy instance
    (``BaseStrategy.entry_policy``); it reads ``config.shared_entry`` live on
    every call, directly -- no getattr default, no ``or`` coercion, so a
    configured 0 is 0 -- and the strategy's manifest capabilities
    (``shared_entry`` exemptions / ``divergence_entry``, ``signal_priority``)
    once, when built."""

    def __init__(self, strategy: BaseStrategy) -> None:
        self.strategy = strategy
        self.config = strategy.config
        shared_entry_caps = strategy._capability("shared_entry", None) or {}
        self._exemptions: dict[str, frozenset[str]] = {
            str(style): frozenset(gates) for style, gates in (shared_entry_caps.get("exemptions") or {}).items()
        }
        self._divergence_entry_capable = bool(shared_entry_caps.get("divergence_entry", True))
        priority = strategy._capability("signal_priority", None) or {}
        self._rank_primary_field = str(priority.get("primary_field", "final_priority_score"))
        self._rank_shared_weight = float(priority.get("shared_score_weight", 0.0))
        self._rank_unit_field: str | None = priority.get("rank_unit_field")
        self._rank_tail_fields: tuple[str, ...] = tuple(priority.get("metadata_fields") or ())
        # A strategy_priority_score primary is declared by the strategies the
        # shared terms never ranked (microcap_pm, both zero_dte): their old
        # key broke a tie on activity then rank, so final_priority_score
        # (= strategy + shared) must not break it first.
        self._rank_final_tiebreak = self._rank_primary_field != "strategy_priority_score"
        # (symbol, frame key) -> the symbol's divergence entry candidate per
        # side (None where there is none). One read per symbol per cycle: the
        # confirmation bump of every proposal and the divergence-only entry
        # share it.
        self._divergence_cache: dict[tuple[Any, ...], dict[Side, dict[str, Any] | None]] = {}

    def reset_cycle(self) -> None:
        """Per-cycle state; called from ``BaseStrategy._reset_entry_decisions``."""
        self._divergence_cache = {}

    # -- the strategy's settings, as the moved helpers read them -------------

    @property
    def params(self) -> dict[str, Any]:
        return self.strategy.params

    def _support_resistance_setting(self, key: str, default: Any) -> Any:
        return self.strategy._support_resistance_setting(key, default)

    def _technical_level_setting(self, key: str, default: Any) -> Any:
        return self.strategy._technical_level_setting(key, default)

    def _candles_setting(self, key: str, default: Any) -> Any:
        return self.strategy._candles_setting(key, default)

    # The score terms' weights and thresholds, None-aware: a configured 0 is
    # 0. Until 2026-09-24 they were read as ``float(x or default)``, so a 0
    # meant the default -- small_cap_squeeze's three zeroed extension
    # penalties (technical_levels atr_stretch_penalty, bollinger_entry_
    # penalty_outer_band, entry_penalty_near_extension) still docked up to
    # 0.75 from a stretched entry. Only null (or NaN) falls back.

    def _technical_weight(self, key: str, default: float) -> float:
        return _safe_float(self._technical_level_setting(key, default), default)

    def _sr_weight(self, key: str, default: float) -> float:
        return _safe_float(self._support_resistance_setting(key, default), default)

    # -- admit ---------------------------------------------------------------

    def admit(self, p: EntryProposal) -> AdmittedEntry | None:
        """Run the shared stage on one proposal (see the module docstring).
        Returns None when it refuses, after recording the refusal under
        ``p.failure_key or p.style`` for the strategy to consume."""
        side_token = "long" if p.direction == Side.LONG else "short"
        # P1 -- structurally unusable raw levels are refused before anything
        # is built; a strategy asks for this where its target can be spent
        # before the entry (top_tier's orb measured move, sr_scalp's floored
        # stop).
        if p.raw_rr_gate is not None and not self._target_meets_min_rr(p.direction, p.close, float(p.stop), p.target):
            close, stop, target = float(p.close), float(p.stop), float(p.target)
            reward = (target - close) if p.direction == Side.LONG else (close - target)
            return self._refuse(p, [
                f"{side_token}_{p.raw_rr_gate}(close={close:.4f},target={target:.4f},"
                f"reward={reward:.4f},risk={abs(close - stop):.4f})"
            ])
        # P0 -- the contexts, on the frames the proposal pinned.
        strategy = self.strategy
        given = p.contexts or EntryContexts()
        sr = given.sr if given.sr is not None else strategy._sr_context(p.symbol, p.sr_frame, p.data)
        ms = given.ms if given.ms is not None else strategy._structure_context(p.gate_frame, "ltf")
        tech = given.tech if given.tech is not None else strategy._technical_context(p.gate_frame)
        chart = given.chart if given.chart is not None else strategy._chart_context(p.gate_frame)
        candle_signal = strategy._directional_candle_signal(p.gate_frame, p.direction)
        # Only the HTF divergence score term reads it; a proposal brings its
        # own when the strategy scores on a different HTF build.
        htf = p.htf_ctx
        if htf is None and self.config.shared_entry.use_htf_divergence_score:
            htf = strategy._default_htf_context_for_score(p.symbol, p.data)
        zone_frame = p.zone_frame if p.zone_frame is not None else p.gate_frame
        # P2 -- the retest admission, one pass over every pending reason.
        plans = self._retest_plans(p, zone_frame)
        pending = list(p.pending_reasons)
        if plans:
            pending = self._apply_continuation_zone_retest_plans(pending, list(plans), deferrable_prefixes=set(p.deferrable))
        admitted_via_retest = any(str(plan.get("status") or "").strip().lower() == "allow" for plan in plans)
        # P3 -- the vetoes. They run after the admission, so a retest cannot
        # carry an entry past one (the chart filter used to run only while
        # nothing else was pending, so a retest-admitted entry never met it),
        # and all of them run, so the refusal lists every blocker.
        vetoes, applied, exempted, abstained = self._vetoes(p, sr, ms, tech, chart, candle_signal)
        blockers = pending + [reason for reason in vetoes if reason not in pending]
        if blockers:
            return self._refuse(p, blockers)
        # P4 -- the levels.
        stop, target = p.stop, p.target
        if p.level_frame is not None:
            if p.stop_resolver is not None:
                stop = p.stop_resolver(plans)
                if stop is None:
                    return self._refuse(p, ["stop_above_entry"])
            close = float(p.close)
            if p.direction == Side.LONG:
                stop, target = self._refine_bullish_sr_levels(close, float(stop), target, sr, p.level_frame)
                stop, target = self._refine_bullish_technical_levels(close, stop, target, tech, p.level_frame)
            else:
                stop, target = self._refine_bearish_sr_levels(close, float(stop), target, sr, p.level_frame)
                stop, target = self._refine_bearish_technical_levels(close, stop, target, tech, p.level_frame)
            if admitted_via_retest and p.retest is not None and p.retest.anchor_stop:
                # The anchor used to be applied after the refinement clamp
                # and could put the stop 0.05% from entry; it is bounded by
                # the same floor now (2026-09-24).
                anchored = stop
                for plan in plans:
                    anchored = self._apply_retest_stop_anchor(p.direction, close, anchored, plan)
                stop = self._clamp_refined_stop(close, stop, anchored, strategy._frame_atr14(p.level_frame, close))
            if (stop >= close) if p.direction == Side.LONG else (stop <= close):
                return self._refuse(p, [_reason_with_values(
                    "stop_on_wrong_side", current=stop, required=close,
                    op="<" if p.direction == Side.LONG else ">", digits=4,
                )])
        # P5 -- the score.
        adjustments = self._entry_adjustment_components(p.direction, sr_ctx=sr, tech_ctx=tech, htf_ctx=htf)
        fvg = self._fvg_entry_adjustment_components(p.direction, p.symbol, zone_frame, p.data)
        divergence = self._divergence_confirmation(p)
        shared_context_score = (
            float(adjustments["entry_context_adjustment"])
            + float(fvg["fvg_entry_adjustment"])
            + (float(divergence["score_bump"]) if divergence is not None and divergence.get("confirmed") else 0.0)
        )
        floor = _optional_float(self.config.shared_entry.min_shared_context_score)
        if floor is not None and shared_context_score < floor:
            return self._refuse(p, [f"shared_context_below_min(score={shared_context_score:.4f},min={floor:.4f})"])
        _MINTING.active = True
        try:
            return AdmittedEntry(
                proposal=p,
                stop=None if stop is None else float(stop),
                target=None if target is None else float(target),
                sr=sr, ms=ms, tech=tech, chart=chart, htf=htf,
                candle_signal=candle_signal,
                retest_plans=plans,
                admitted_via_retest=admitted_via_retest,
                fvg=fvg,
                adjustments=adjustments,
                shared_context_score=round(shared_context_score, 4),
                divergence_confirmation=divergence,
                gates_applied=applied,
                gates_exempted=exempted,
                gates_abstained=abstained,
            )
        finally:
            _MINTING.active = False

    def _refuse(self, p: EntryProposal, reasons: list[str]) -> None:
        self.strategy._set_build_failure(p.symbol, p.failure_key or p.style, reasons[0], reasons=reasons,
                                         details=p.failure_details)
        return None

    def _retest_plans(self, p: EntryProposal, zone_frame: pd.DataFrame) -> tuple[dict[str, Any], ...]:
        trigger = p.retest
        if trigger is None:
            return ()
        kwargs = dict(
            trigger_level=float(trigger.trigger_level), breakout_active=bool(trigger.breakout_active),
            close=float(p.close), vwap=float(trigger.vwap), ema9=float(trigger.ema9),
        )
        # The FVG plan is 'none' with use_fvg_context off, and the OB plan with
        # support_resistance.ltf_order_blocks_enabled off, so a deferrable
        # reason then stays a blocker, as it always did.
        plans = [self._continuation_fvg_retest_plan(p.direction, p.symbol, zone_frame, p.data, **kwargs)]
        if "ob" in trigger.zones:
            plans.append(self._continuation_ob_retest_plan(p.direction, p.symbol, zone_frame, p.data, **kwargs))
        return tuple(plans)

    def _vetoes(self, p: EntryProposal, sr, ms, tech, chart, candle_signal) -> tuple[list[str], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """The P3 vetoes that are switched on, in ``VETO_GATES`` order, each
        judged on ``p.direction`` (the market direction, never an order
        side). Returns (blockers, gates applied, gates the manifest exempts
        for ``p.style``, applied gates that abstained)."""
        cfg = self.config.shared_entry
        long = p.direction == Side.LONG
        switched_on = {
            "structure": cfg.use_structure_filter and bool(self._support_resistance_setting("structure_enabled", True)),
            "sr": cfg.use_sr_filter and bool(self._support_resistance_setting("enabled", True)),
            "broken_level": cfg.use_broken_level_guard,
            "chart": cfg.use_opposing_chart_filter,
            "dual_divergence": (
                cfg.use_dual_divergence_veto
                and bool(self._technical_level_setting("enabled", True))
                and bool(self._technical_level_setting("divergence_enabled", True))
            ),
            "candle": cfg.use_opposing_candle_filter,
        }
        exempt = self._exemptions.get(p.style, frozenset())
        blockers: list[str] = []
        applied: list[str] = []
        exempted: list[str] = []
        abstained: list[str] = []
        for gate in VETO_GATES:
            if not switched_on[gate]:
                continue
            if gate in exempt:
                exempted.append(gate)
                continue
            applied.append(gate)
            reason: str | None = None
            if gate == "structure":
                if self._blocks_bullish_structure_entry(ms) if long else self._blocks_bearish_structure_entry(ms):
                    reason = self._bullish_structure_block_reason(ms) if long else self._bearish_structure_block_reason(ms)
            elif gate == "sr":
                if self._blocks_bullish_sr_entry(sr) if long else self._blocks_bearish_sr_entry(sr):
                    reason = self._bullish_sr_block_reason(sr) if long else self._bearish_sr_block_reason(sr)
            elif gate == "broken_level":
                reason = self._broken_level_reason(p, sr)
            elif gate == "chart":
                if self._blocks_bullish_entry(chart) if long else self._blocks_bearish_entry(chart):
                    reason = "chart_pattern_opposed"
            elif gate == "dual_divergence":
                reason = self._dual_counter_divergence_reason(p.direction, tech)
            elif not _bars_have_range(p.gate_frame, CANDLE_PATTERN_WINDOW_BARS):
                # A single print in the pattern window: TA-Lib reads it as a
                # doji / white candle, so on thin premarket tape 40% of
                # microcap_pm's LONG vetoes were TRISTAR / GAPSIDESIDEWHITE /
                # HARAMICROSS built from single prints alone. The exit side's
                # candle_pattern family holds on the same window (its
                # bar_range gate); the veto abstains (2026-09-24).
                abstained.append(gate)
            else:
                reason = self._opposing_candle_reason(p, candle_signal)
            if reason:
                blockers.append(reason)
        return blockers, tuple(applied), tuple(exempted), tuple(abstained)

    @staticmethod
    def _last_atr(frame: pd.DataFrame | None, close: float) -> float:
        return _safe_float(
            frame.iloc[-1].get("atr14") if (frame is not None and not frame.empty and "atr14" in frame.columns) else None,
            max(close * 0.0015, 0.01),
        )

    def _broken_level_reason(self, p: EntryProposal, sr_ctx) -> str | None:
        """A confirmed BROKEN level just beyond the entry (broken_support
        under a LONG, broken_resistance over a SHORT) inside either
        clearance. Moved from top_tier_adaptive (reject_entry_near_broken_
        level) on 2026-09-24; the percent clearance scales with the
        proposal's ``vol_scale`` as it did there, the ATR is ``sr_frame``'s."""
        cfg = self.config.shared_entry
        close = float(p.close)
        min_pct = float(cfg.broken_level_min_clearance_pct) * float(p.vol_scale)
        min_atr = float(cfg.broken_level_min_clearance_atr)
        atr = self._last_atr(p.sr_frame, close)
        if p.direction == Side.SHORT:
            broken = getattr(sr_ctx, "broken_resistance", None)
            level = float(getattr(broken, "price", 0.0) or 0.0) if broken is not None else 0.0
            if level > close:
                pct = (level - close) / max(close, 1e-9)
                atr_dist = (level - close) / max(atr, 1e-9)
                if pct <= min_pct or atr_dist <= min_atr:
                    return (f"short_near_broken_resistance(level={level:.4f},"
                            f"pct={pct:.4f}<={min_pct:.4f},atr={atr_dist:.2f}<={min_atr:.2f})")
            return None
        broken = getattr(sr_ctx, "broken_support", None)
        level = float(getattr(broken, "price", 0.0) or 0.0) if broken is not None else 0.0
        if 0.0 < level < close:
            pct = (close - level) / max(close, 1e-9)
            atr_dist = (close - level) / max(atr, 1e-9)
            if pct <= min_pct or atr_dist <= min_atr:
                return (f"long_near_broken_support(level={level:.4f},"
                        f"pct={pct:.4f}<={min_pct:.4f},atr={atr_dist:.2f}<={min_atr:.2f})")
        return None

    def _opposing_candle_reason(self, p: EntryProposal, candle_signal: Mapping[str, Any]) -> str | None:
        """The opposing candle cluster at or above
        ``candles.opposing_net_score_threshold``, from the same cached candle
        context the proposal's candle signal came from (no extra TA-Lib
        calls). Mirrors shared_exit.use_candle_pattern_exit on the entry
        side. Only top_tier read it until 2026-09-24."""
        opposing_net = float(candle_signal.get("opposite_net_score", 0.0) or 0.0)
        threshold = float(self._candles_setting("opposing_net_score_threshold", 0.70))
        if opposing_net < threshold:
            return None
        opposing = "bearish" if p.direction == Side.LONG else "bullish"
        matches = ",".join(
            str(m) for m in sorted(self.strategy._candle_context(p.gate_frame).get(f"matched_{opposing}_candles", []) or [])[:3]
        )
        side_token = "long" if p.direction == Side.LONG else "short"
        return f"{side_token}_opposing_candle(net_score={opposing_net:.2f}>={threshold:.2f},matches={matches or 'na'})"

    # -- emit ----------------------------------------------------------------

    def emit(
        self,
        admitted: AdmittedEntry,
        *,
        reason: str,
        strategy_score: float,
        management: Mapping[str, Any],
        target: float | None,
        ladder_meta: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        order_side: Side | None = None,
        reference_symbol: str | None = None,
        pair_id: str | None = None,
        premium_stop: float | None = None,
    ) -> Signal:
        """The ``Signal`` for an admitted entry -- the only place one is built.

        - ``target``: ``admitted.target``, the active rung of ``ladder_meta``
          (the ladder replaced it), or None (a runner). Anything else raises:
          the refinement's target cap is not optional.
        - ``strategy_score``: the strategy's own priority, WITHOUT the shared
          terms; ``final_priority_score`` = it + ``shared_context_score``.
        - ``metadata``: the strategy's own keys; the shared blocks and stamps
          are merged over them, except ``regime``, which defaults to the
          proposal's style.
        - ``order_side``: the order's side when it is not the market
          direction (a credit spread is sold, a bearish debit spread bought).
        - ``premium_stop``: a premium proposal's stop (its target comes in
          ``target``); the stop of a price-level proposal is
          ``admitted.stop`` and cannot be replaced.

        An option signal's ``metadata['direction']`` (bullish*, bearish*)
        must agree with the proposal's direction: every veto and score term
        was judged on the latter, so a conversion that proposed the ORDER
        side (a bull put credit spread as SHORT) raises here instead of
        trading on vetoes read the wrong way round.

        Stamps ``entry_style_family`` (the exit graces key on it: 'orb',
        'pullback'), ``orb_window_entry``, ``strategy_priority_score``,
        ``shared_context_score``, ``final_priority_score``,
        ``shared_entry_applied``, the gates applied / exempted (and
        ``shared_entry_candle_abstained`` when the candle veto abstained on
        single prints), the retest and divergence components.
        """
        p = admitted.proposal
        market = str((metadata or {}).get("direction") or "").strip().lower()
        if (market.startswith("bullish") and p.direction != Side.LONG) or (market.startswith("bearish") and p.direction != Side.SHORT):
            raise ValueError(
                f"{p.style}: metadata direction {market!r} disagrees with the proposal's market direction "
                f"{p.direction.value}; propose the underlying's direction and pass the order side as order_side"
            )
        if p.level_frame is None:
            if premium_stop is None:
                raise ValueError(f"{p.style}: a premium proposal's signal needs premium_stop")
            stop = float(premium_stop)
        else:
            if premium_stop is not None:
                raise ValueError(f"{p.style}: premium_stop is only for premium proposals; the stop is admitted.stop")
            if not self._target_is_admitted(admitted, target, ladder_meta):
                raise ValueError(
                    f"{p.style}: target {target!r} is neither the admitted target {admitted.target!r}, "
                    "the ladder's active rung, nor None"
                )
            stop = float(admitted.stop)
        shared = float(admitted.shared_context_score)
        final_priority_score = float(strategy_score) + shared
        meta = self._build_signal_metadata(
            entry_price=None if p.level_frame is None else p.close,
            chart_ctx=admitted.chart, ms_ctx=admitted.ms, sr_ctx=admitted.sr, tech_ctx=admitted.tech,
            adjustments=admitted.adjustments, fvg_adjustments=admitted.fvg, management=management,
            retest_plans=admitted.retest_plans, ladder_meta=ladder_meta,
            final_priority_score=final_priority_score, leading=metadata,
        )
        meta.setdefault("regime", p.style)
        meta.update({
            "entry_style_family": p.style_family,
            "orb_window_entry": p.style_family == "orb",
            "strategy_priority_score": round(float(strategy_score), 4),
            "shared_context_score": round(shared, 4),
            "shared_entry_applied": True,
            "shared_entry_gates_applied": list(admitted.gates_applied),
            "shared_entry_gates_exempted": list(admitted.gates_exempted),
            "shared_entry_admitted_via_retest": bool(admitted.admitted_via_retest),
        })
        if "candle" in admitted.gates_abstained:
            meta["shared_entry_candle_abstained"] = "zero_range"
        divergence = admitted.divergence_confirmation
        if divergence is not None:
            if divergence.get("confirmed"):
                meta.update({
                    "divergence_entry_confirmed": True,
                    "divergence_entry_reason": divergence["reason"],
                    "divergence_entry_score": round(float(divergence["score"]), 4),
                    "divergence_entry_score_bump": round(float(divergence["score_bump"]), 4),
                })
            else:
                meta.update({
                    "divergence_entry_conflict": True,
                    "divergence_entry_reason": divergence["reason"],
                    "divergence_entry_score": round(float(divergence["score"]), 4),
                })
        return Signal(
            symbol=p.symbol,
            strategy=str(self.config.strategy),
            side=order_side or p.direction,
            reason=str(reason),
            stop_price=stop,
            target_price=None if target is None else float(target),
            reference_symbol=reference_symbol,
            pair_id=pair_id,
            metadata=meta,
        )

    @staticmethod
    def _target_is_admitted(admitted: AdmittedEntry, target: float | None, ladder_meta: Mapping[str, Any] | None) -> bool:
        # Ladder rungs are stamped rounded to 6 decimals.
        def same(a: float, b: float) -> bool:
            return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=1e-6)

        if target is None:
            return True
        if admitted.target is not None and same(target, admitted.target):
            return True
        rungs = list((ladder_meta or {}).get("ladder_rungs") or [])
        index = int((ladder_meta or {}).get("ladder_active_index", 0) or 0)
        return 0 <= index < len(rungs) and same(rungs[index]["price"], target)

    # -- ranking -------------------------------------------------------------

    def rank_key(self, signal: Signal, candidate: Candidate | None) -> tuple[float, ...]:
        """The gatekeeper's sort key (descending), and the peer strategies'
        side pick.

        ``(tier, primary + w * shared_context_score * unit, *tail,
        final_priority_score, activity, -rank)`` from the manifest's
        ``capabilities.signal_priority``: ``primary_field`` (default
        ``final_priority_score``), ``shared_score_weight`` w (default 0),
        ``rank_unit_field`` (the metadata field that converts a shared-score
        point into the primary's units; default 1) and ``metadata_fields``
        (the tail). ``tier`` is 0 for a divergence-only entry and 1 for every
        strategy signal, so the strategy always wins a contested slot. A
        ``strategy_priority_score`` primary drops the final_priority_score
        tiebreak: the shared terms never ranked those strategies, not even
        on a tie.

        It replaced three rankers (2026-09-24): the gatekeeper's generic key
        (final_priority_score first), top_tier's override (normalised regime
        score first) and the manifest-tuple override the peers used. With w
        = 0 the order is theirs; w > 0 lets the shared terms, which only
        broke near-ties behind top_tier's regime score and the peers'
        seven-key tail, move a signal by a declared amount.
        """
        meta = signal.metadata
        tier = 0.0 if meta.get("entry_source") == DIVERGENCE_ENTRY_SOURCE else 1.0
        primary = _safe_float(meta.get(self._rank_primary_field), 0.0)
        if self._rank_shared_weight:
            unit = _safe_float(meta.get(self._rank_unit_field), 0.0) if self._rank_unit_field else 1.0
            primary += self._rank_shared_weight * _safe_float(meta.get("shared_context_score"), 0.0) * unit
        tail = tuple(_safe_float(meta.get(name), 0.0) for name in self._rank_tail_fields)
        tiebreak = (_safe_float(meta.get("final_priority_score"), 0.0),) if self._rank_final_tiebreak else ()
        activity = float(candidate.activity_score) if candidate is not None else 0.0
        rank = float(candidate.rank) if candidate is not None else 9_999.0
        return (tier, primary, *tail, *tiebreak, activity, -rank)

    # -- zero_dte's regime FVG scores ----------------------------------------

    def fvg_regime_scores(self, close: float, htf_ctx: Any, ltf_ctx: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """(HTF, LTF) FVG scores of an underlying for a regime scorer
        (zero_dte), or the zero-score shape when use_fvg_context is off."""
        if not self.config.shared_entry.use_fvg_context:
            return _disabled_fvg_score(), _disabled_fvg_score()
        return (
            self._score_fvg_context(close, htf_ctx, timeframe_minutes=getattr(htf_ctx, "timeframe_minutes", self.strategy._htf_minutes())),
            self._score_fvg_context(close, ltf_ctx, timeframe_minutes=self.strategy._ltf_minutes()),
        )

    # -- divergence ----------------------------------------------------------

    def _divergence_candidates(self, symbol: str, frame: pd.DataFrame, data) -> dict[Side, dict[str, Any] | None]:
        """The symbol's divergence entry candidate for each side this cycle
        (None where there is none). Cached per (symbol, frame).

        Both sides are kept: until 2026-09-24 only the better-scoring one
        was, so a SHORT candidate the strategy could not take (shorts off,
        a LONG screener bias) hid a valid LONG one, and a LONG proposal
        read as a conflict instead of a confirmation."""
        key = (str(symbol), self.strategy._technical_context_cache_key(frame))
        if key in self._divergence_cache:
            return self._divergence_cache[key]
        found: dict[Side, dict[str, Any] | None] = {Side.LONG: None, Side.SHORT: None}
        if frame is not None and not frame.empty:
            close = _safe_float(frame.iloc[-1].get("close"), 0.0)
            if close > 0:
                sr = self.strategy._sr_context(symbol, frame, data)
                tech = self.strategy._technical_context(frame)
                htf = self.strategy._default_htf_context_for_score(symbol, data)
                for side in found:
                    found[side] = self._divergence_entry_candidate(side, close, frame, sr, tech, htf)
        self._divergence_cache[key] = found
        return found

    def _divergence_confirmation(self, p: EntryProposal) -> dict[str, Any] | None:
        """How the symbol's divergence candidates relate to a strategy
        proposal: one on its own side confirms it (its shared score gains
        ``divergence_entry_score_bump``); one only on the opposite side
        conflicts (only stamped: the strategy's proposal wins). None when
        the knob is off, there is no candidate, or the proposal IS the
        divergence entry."""
        cfg = self.config.shared_entry
        if not cfg.use_divergence_entry_signal or p.style in _DIVERGENCE_STYLES:
            return None
        candidates = self._divergence_candidates(p.symbol, p.sr_frame, p.data)
        own = candidates[p.direction]
        if own is not None:
            return {"reason": own["reason"], "score": own["score"], "confirmed": True,
                    "score_bump": float(cfg.divergence_entry_score_bump)}
        opposite = candidates[Side.SHORT if p.direction == Side.LONG else Side.LONG]
        if opposite is not None:
            return {"reason": opposite["reason"], "score": opposite["score"], "confirmed": False}
        return None

    def divergence_entries(
        self,
        candidates: Iterable[Candidate],
        bars: Mapping[str, pd.DataFrame],
        positions: Mapping[str, Position],
        *,
        primaries: Iterable[Signal],
        data=None,
    ) -> list[Signal]:
        """Divergence-only entries for the candidates the strategy produced
        no signal for (``use_divergence_entry_signal``). The gatekeeper calls
        it after ``entry_signals``; a strategy whose manifest says
        ``capabilities.shared_entry.divergence_entry: false`` gets none.

        Each one is a proposal like any other -- style ``divergence_regular``
        (family 'reversal') or ``divergence_hidden`` ('continuation') on the
        symbol's canonical frame -- so every veto, the refinement and the
        score floor apply, then the ladder (regime 'divergence') and the
        adaptive management. A symbol is skipped when it is held, when the
        strategy signalled it, or when the strategy skipped it before
        evaluating a setup (``DIVERGENCE_INELIGIBLE_REASONS``: not its
        symbol, outside its window, a blackout, not enough data, every side
        it trades switched off...). A side is skipped
        when the candidate's directional bias or ``risk.allow_short`` blocks
        it, and the better-scoring remaining side is proposed. Refusals are
        added to the symbol's recorded decision as
        ``divergence_entry_blockers``.
        """
        if not self.config.shared_entry.use_divergence_entry_signal or not self._divergence_entry_capable:
            return []
        signalled = {signal.symbol for signal in primaries}
        allow_short = bool(self.config.risk.allow_short)
        out: list[Signal] = []
        for candidate in candidates:
            symbol = candidate.symbol
            if symbol in positions or symbol in signalled or self._ineligible(symbol):
                continue
            frame = bars.get(symbol)
            if frame is None or frame.empty:
                continue
            takeable = [
                found for side, found in self._divergence_candidates(symbol, frame, data).items()
                if found is not None
                and (side == Side.LONG or allow_short)
                and (candidate.directional_bias is None or candidate.directional_bias == side)
            ]
            if not takeable:
                continue
            signal = self._divergence_signal(candidate, max(takeable, key=lambda found: found["score"]), frame, data)
            if signal is not None:
                out.append(signal)
        return out

    def _ineligible(self, symbol: str) -> bool:
        """Whether the strategy's recorded decision for ``symbol`` says it
        never evaluated a setup there (``DIVERGENCE_INELIGIBLE_REASONS`` or
        an ``insufficient_*`` token)."""
        decision = self.strategy._entry_decisions.get(symbol) or {}
        for reason in decision.get("reasons") or ():
            token = _reason_prefix(str(reason))
            if token in DIVERGENCE_INELIGIBLE_REASONS or token.startswith("insufficient_"):
                return True
        return False

    def _divergence_signal(self, candidate: Candidate, found: Mapping[str, Any], frame: pd.DataFrame, data) -> Signal | None:
        strategy = self.strategy
        side = found["side"]
        family = "reversal" if found["kind"] == "regular" else "continuation"
        close = _safe_float(frame.iloc[-1].get("close"), 0.0)
        proposal = EntryProposal(
            candidate=candidate, direction=side, style=f"divergence_{found['kind']}", style_family=family,
            close=close, stop=float(found["stop"]), target=found["target"],
            gate_frame=frame, sr_frame=frame, level_frame=frame, data=data,
        )
        admitted = self.admit(proposal)
        if admitted is None:
            refusal = strategy._consume_build_failure_payload(candidate.symbol, proposal.style) or {}
            decision = strategy._entry_decisions.get(candidate.symbol)
            if decision is None:
                strategy._record_entry_decision(candidate.symbol, "skipped", list(refusal.get("reasons") or []))
            else:
                decision.setdefault("details", {})["divergence_entry_blockers"] = list(refusal.get("reasons") or [])
            return None
        bias_key = "fvg_reversal_bias" if family == "reversal" else "fvg_continuation_bias"
        target, ladder_meta = strategy._apply_ladder_if_enabled(
            side, close, admitted.stop, admitted.target, regime="divergence",
            sr_ctx=admitted.sr, atr=strategy._frame_atr14(frame, close),
        )
        management = strategy._adaptive_management_components(
            side, close, admitted.stop, target, style=family, runner_allowed=False,
            continuation_bias=float(admitted.fvg[bias_key]),
        )
        signal = self.emit(
            admitted,
            reason=found["reason"],
            strategy_score=float(found["score"]),
            management=management,
            target=target,
            ladder_meta=ladder_meta,
            metadata={
                "entry_source": DIVERGENCE_ENTRY_SOURCE,
                "divergence_kind": found["kind"],
                "divergence_indicator": found["indicator"],
                "divergence_age_bars": found["age_bars"],
                "divergence_confluence_level": found["confluence_level"],
            },
        )
        strategy._record_entry_decision(candidate.symbol, "signal", [signal.reason],
                                        details={"entry_source": DIVERGENCE_ENTRY_SOURCE})
        return signal

    def _hidden_divergence_aligned(self, side: Side, htf_ctx) -> bool:
        """A hidden divergence is a continuation read, so it needs the HTF
        trend it continues: the HTF fast EMA above the slow one for a LONG,
        below it for a SHORT. No HTF EMAs, no hidden entry."""
        fast = _optional_float(getattr(htf_ctx, "ema_fast", None))
        slow = _optional_float(getattr(htf_ctx, "ema_slow", None))
        if fast is None or slow is None:
            return False
        return fast > slow if side == Side.LONG else fast < slow

    def _divergence_entry_candidate(self, side: Side, close: float, frame: pd.DataFrame, sr_ctx, tech_ctx, htf_ctx) -> dict[str, Any] | None:
        """A divergence entry candidate for ``side``, or None.

        The divergence: the first of the regular then the hidden RSI then
        OBV divergence for ``side`` on ``tech_ctx`` whose age is inside
        [``divergence_entry_min_age_bars``, ``technical_levels.divergence_
        max_age_bars``], whose latest pivot is in the CURRENT session (the
        reader's date, ``now_et``; a frame whose last bar is not in it has
        no candidate), and -- a hidden one -- with the HTF EMAs aligned
        (``_hidden_divergence_aligned``). Yesterday's pivots are refused
        outright rather than compared across the overnight gap: since the
        session clock (2026-09-24) yesterday's closing divergences are young
        at 09:30.

        With ``divergence_entry_require_sr_confluence``, the divergence
        pivot must sit within the S/R level buffer (at least 0.30 ATR /
        0.15% of price) of a level on its side: the nearest support or the
        broken resistance for a LONG, mirrored for a SHORT.

        Score = indicator strength (the delta over twice the indicator's
        minimum delta, capped at 1: 5 RSI points at the shipped 2.5) +
        recency (1 - age / max age) + 0.5 regular / 0.3 hidden + 0.5 S/R
        confluence; below ``divergence_entry_score_floor`` there is no
        candidate.

        Stop: the divergence pivot -/+ ``support_resistance.stop_buffer_atr_
        mult`` ATR (a stop on the wrong side of the close refuses the
        candidate). Target: ``min_target_rr`` x risk, capped at the nearest
        opposing S/R level; a capped target must still clear
        ``min_target_rr``, so a divergence with a wall inside that room has
        no candidate. With ``min_target_rr`` off the target is the opposing
        level (None: a runner).

        Until 2026-09-24: the stop buffer was read from technical_levels,
        where it does not exist (always the 0.25 fallback); ``or`` defaults
        made a configured 0 impossible and read ``min_target_rr`` with a 1.5
        fallback; ``regular or hidden`` hid a valid hidden divergence behind a
        stale regular one; a hidden one needed no HTF trend; recency divided
        by 8 whatever the max age; OBV divergences were never read; and a
        capped target could leave any R:R.
        """
        cfg = self.config.shared_entry
        if tech_ctx is None or frame is None or frame.empty or close <= 0:
            return None
        min_age = max(0, int(cfg.divergence_entry_min_age_bars))
        max_age = int(self._technical_level_setting("divergence_max_age_bars", 8))
        if max_age < min_age:
            return None
        direction = "bullish" if side == Side.LONG else "bearish"
        # The reader's session, not the frame's last bar: at 09:30 a name
        # with no premarket prints still ends yesterday, and its closing
        # divergence would pass as "current" and open at yesterday's close.
        now = now_et()
        session_day = now.date()
        if _local_date(frame.index[-1], now.tzinfo) != session_day:
            return None
        match = None
        for kind in ("regular", "hidden"):
            if kind == "hidden" and not self._hidden_divergence_aligned(side, htf_ctx):
                continue
            infix = "" if kind == "regular" else "hidden_"
            for indicator in ("rsi", "obv"):
                found = getattr(tech_ctx, f"{direction}_{infix}{indicator}_divergence", None)
                if found is None:
                    continue
                if not (min_age <= int(found.age_bars) <= max_age):
                    continue
                if _local_date(found.pivot_b_ts, now.tzinfo) != session_day or float(found.pivot_b_price) <= 0:
                    continue
                match = found
                break
            if match is not None:
                break
        if match is None:
            return None
        pivot_price = float(match.pivot_b_price)
        age = int(match.age_bars)
        atr_val = _safe_float(frame.iloc[-1].get("atr14"), max(close * 0.0015, 0.01))

        confluence_level: float | None = None
        if sr_ctx is not None:
            buffer = max(float(getattr(sr_ctx, "level_buffer", 0.0) or 0.0), atr_val * 0.30, close * 0.0015)
            names = ("nearest_support", "broken_resistance") if side == Side.LONG else ("nearest_resistance", "broken_support")
            for name in names:
                level = getattr(sr_ctx, name, None)
                level_price = _optional_float(getattr(level, "price", None))
                if level_price and abs(pivot_price - level_price) <= buffer:
                    confluence_level = level_price
                    break
        if bool(cfg.divergence_entry_require_sr_confluence) and confluence_level is None:
            return None

        if match.indicator == "rsi":
            min_delta = float(self._technical_level_setting("divergence_rsi_min_delta", 2.5))
        else:
            # technical_levels' own OBV threshold: a share of the last 20 bars' volume.
            avg_volume = float(frame["volume"].tail(20).fillna(0.0).mean()) if "volume" in frame.columns else 0.0
            min_delta = max(1.0, avg_volume * max(0.0, float(self._technical_level_setting("divergence_obv_min_volume_frac", 0.65))))
        indicator_score = min(1.0, float(match.indicator_delta) / (2.0 * min_delta)) if min_delta > 0 else 1.0
        recency_score = max(0.0, 1.0 - age / max_age) if max_age > 0 else 1.0
        score = indicator_score + recency_score + (0.5 if match.kind == "regular" else 0.3) + (0.5 if confluence_level is not None else 0.0)
        if score < float(cfg.divergence_entry_score_floor):
            return None

        stop_buffer = atr_val * float(self._support_resistance_setting("stop_buffer_atr_mult", 0.25))
        stop = pivot_price - stop_buffer if side == Side.LONG else pivot_price + stop_buffer
        if (stop >= close) if side == Side.LONG else (stop <= close):
            return None
        risk = abs(close - stop)
        min_rr = _optional_float(cfg.min_target_rr)
        rr_target = None if min_rr is None or min_rr <= 0 else (close + risk * min_rr if side == Side.LONG else close - risk * min_rr)
        opposing = getattr(sr_ctx, "nearest_resistance" if side == Side.LONG else "nearest_support", None) if sr_ctx is not None else None
        opposing_price = _optional_float(getattr(opposing, "price", None))
        if opposing_price is not None and not ((opposing_price > close) if side == Side.LONG else (0 < opposing_price < close)):
            opposing_price = None
        if rr_target is None:
            target = opposing_price
        elif opposing_price is None:
            target = rr_target
        else:
            target = min(rr_target, opposing_price) if side == Side.LONG else max(rr_target, opposing_price)
        if target is not None and not self._target_meets_min_rr(side, close, stop, target):
            return None
        return {
            "source": DIVERGENCE_ENTRY_SOURCE,
            "side": side,
            "trigger_level": pivot_price,
            "stop": float(stop),
            "target": None if target is None else float(target),
            "score": float(score),
            "reason": f"divergence_entry({match.kind}_{side.value.lower()}_{match.indicator}_age{age}b)",
            "kind": str(match.kind),
            "indicator": str(match.indicator),
            "age_bars": age,
            "confluence_level": confluence_level,
        }

    # -- the moved helpers (strategy_base until 2026-09-24) ------------------
    #
    # Moved verbatim apart from the knob reads: each veto predicate below no
    # longer checks its own knob (``_vetoes`` does), every ``shared_entry``
    # read is a direct, None-aware attribute read, and so is every score
    # weight (``_technical_weight`` / ``_sr_weight``).

    def _target_meets_min_rr(self, side: Side, close: float, stop: float, target: float | None) -> bool:
        """Return True if (close, stop, target) clears shared_entry.min_target_rr.

        Used by the SR/technical refinement pipeline as a floor check so
        capping the target never silently destroys R:R below the
        configured threshold, by the raw R:R gate a proposal can ask for, and
        by the divergence entry's target. Returns True when there's no target
        to test (None) so callers can use this as a one-line guard:
            if self._target_meets_min_rr(side, close, stop, proposed):
                target = proposed

        A positive risk and reward are always required. ``min_target_rr`` 0
        or null switches only the ratio floor off; until 2026-09-24 it was
        read as ``float(x or 1.0)``, so a configured 0 meant 1.0.
        """
        if target is None:
            return True
        try:
            close_v = float(close)
            stop_v = float(stop)
            target_v = float(target)
        except (TypeError, ValueError):
            return True
        if side == Side.LONG:
            risk = close_v - stop_v
            reward = target_v - close_v
        else:
            risk = stop_v - close_v
            reward = close_v - target_v
        if risk <= 0 or reward <= 0:
            return False
        min_rr = _optional_float(self.config.shared_entry.min_target_rr)
        if min_rr is None or min_rr <= 0:
            return True
        return (reward / risk) >= min_rr

    def _clamp_refined_stop(self, close: float, incoming_stop: float,
                            proposed_stop: float, atr: float) -> float:
        """Bound how far the refinement pipeline may pull a stop toward entry.

        Both refinement passes (SR levels, technical levels) move the stop
        TOWARD entry whenever a support/resistance level or trendline sits
        inside the strategy's structural stop, and neither had a floor. A
        level a few cents from entry therefore produced a few-cent stop,
        silently overriding the ``default_stop_pct`` backstop the builder
        applied a few lines earlier.

        ``_target_meets_min_rr`` cannot police this — tightening the stop
        RAISES reward/risk, so the R:R guard that protects the target cap
        never binds on the stop side. The floor has to be absolute, so it is
        expressed in ATR: refinement may not pull the stop closer to entry
        than ``shared_entry.min_stop_atr_mult`` ATR.

        An over-tight proposal is clamped BACK TO that distance rather than
        discarded. Discarding would revert to the builder's stop, and the
        builder backstop is a flat ``default_stop_pct`` — a percentage floor
        applied regardless of the symbol's volatility. On a quiet name that
        is enormous in ATR terms (COP 2026-05-14: 1% of price = 6.8 ATR,
        one logged case reached 11 ATR), which would blow R out far enough
        that nothing downstream priced in R — breakeven, profit-lock, runner,
        peak-giveback, ``shared_exit.discretionary_exit_min_r`` — could ever
        arm. Clamping keeps the stop volatility-scaled and keeps R in the
        band the management ladder is tuned for.

        The clamp never WIDENS past the incoming stop. A builder that
        deliberately chose a stop tighter than the floor (range / sr_scalp
        anchor to level geometry, momentum caps at ``default_stop_pct``)
        keeps it exactly. Both callers pass a ``proposed_stop`` already on
        the tightening side of ``incoming_stop``, so its sign relative to
        ``close`` identifies the trade direction. The retest stop anchor is
        bounded the same way since 2026-09-24 (see ``admit``); it used to be
        applied after the clamp and could pull a stop to 0.05% from entry.

        ``min_stop_atr_mult`` 0 or null switches the floor off.
        """
        min_atr_mult = _optional_float(self.config.shared_entry.min_stop_atr_mult)
        close_v, proposed_v = float(close), float(proposed_stop)
        if min_atr_mult is None or min_atr_mult <= 0 or atr <= 0:
            return proposed_v
        incoming_distance = abs(close_v - float(incoming_stop))
        proposed_distance = abs(close_v - proposed_v)
        floor_distance = min(incoming_distance, min_atr_mult * float(atr))
        if proposed_distance >= floor_distance:
            return proposed_v
        return close_v - floor_distance if proposed_v < close_v else close_v + floor_distance

    @staticmethod
    def _apply_retest_stop_anchor(side: Side, close: float, stop: float, plan: dict[str, Any] | None) -> float:
        if not plan or str(plan.get("status", "none") or "none").strip().lower() != "allow":
            return float(stop)
        anchor = _optional_float(plan.get("stop_anchor"))
        if anchor is None:
            return float(stop)
        if side == Side.LONG:
            candidate = max(float(stop), float(anchor))
            return min(candidate, float(close) * 0.9995)
        candidate = min(float(stop), float(anchor))
        return max(candidate, float(close) * 1.0005)

    def _continuation_fvg_retest_plan(
        self,
        side: Side,
        symbol: str,
        frame: pd.DataFrame | None,
        data=None,
        *,
        trigger_level: float,
        breakout_active: bool,
        close: float,
        vwap: float,
        ema9: float,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": "none",
            "reason": None,
            "stop_anchor": None,
            "metadata": {
                "anti_chase_fvg_retest_enabled": bool(self.params.get("anti_chase_fvg_retest_enabled", True)),
                "anti_chase_fvg_retest_status": "none",
            },
        }
        if frame is None or frame.empty:
            return out
        if not bool(self.params.get("anti_chase_fvg_retest_enabled", True)):
            return out
        if not self.config.shared_entry.use_fvg_context:
            return out
        close = float(close or 0.0)
        if close <= 0:
            return out
        fvg_ctx = self.strategy._ltf_fvg_context(symbol, frame, data)
        same_gap = getattr(fvg_ctx, "nearest_bullish_fvg", None) if side == Side.LONG else getattr(fvg_ctx, "nearest_bearish_fvg", None)
        opposing_gap = getattr(fvg_ctx, "nearest_bearish_fvg", None) if side == Side.LONG else getattr(fvg_ctx, "nearest_bullish_fvg", None)
        same_info = self._fvg_gap_state(same_gap, close)
        opposing_info = self._fvg_gap_state(opposing_gap, close)
        same_state = str(same_info.get("state", "none") or "none").strip().lower()
        opposing_state = str(opposing_info.get("state", "none") or "none").strip().lower()
        lower = _optional_float(same_info.get("lower"))
        upper = _optional_float(same_info.get("upper"))
        midpoint = _optional_float(same_info.get("midpoint"))
        size = max(1e-8, float(_optional_float(same_info.get("size"), 0.0) or 0.0))
        same_distance_pct = _optional_float(same_info.get("distance_pct"), 1.0)
        opposing_distance_pct = _optional_float(opposing_info.get("distance_pct"))
        max_gap_distance_pct = max(0.0002, float(self.params.get("anti_chase_fvg_retest_max_gap_distance_pct", 0.0030)))
        max_opposing_distance_pct = max(0.0002, float(self.params.get("anti_chase_fvg_retest_max_opposing_distance_pct", 0.0020)))
        lookback_bars = max(2, int(self.params.get("anti_chase_fvg_retest_lookback_bars", 5)))
        min_close_pos_raw = self.params.get("anti_chase_fvg_retest_min_close_position")
        if min_close_pos_raw is None:
            min_close_pos_raw = self.params.get("min_bar_close_position", 0.60)
        min_close_pos = min(0.95, max(0.05, float(min_close_pos_raw)))
        stop_buffer_gap_frac = max(0.0, float(self.params.get("anti_chase_fvg_retest_stop_buffer_gap_frac", 0.14)))
        trigger_tolerance_pct = max(0.0, float(self.params.get("anti_chase_fvg_retest_trigger_tolerance_pct", 0.0012)))
        touch_tolerance = max(size * 0.20, abs(close) * max_gap_distance_pct * 0.25, 1e-8)
        invalidation_tolerance = max(size * 0.18, abs(close) * 1e-6, 1e-8)
        # Edge-tolerance lets a bar that bounces *just above* a bullish FVG
        # upper bound (or just below a bearish FVG lower bound) without
        # penetrating the zone still qualify as a touch. Some reversals
        # respect the FVG boundary as support without filling the gap;
        # historically the strict touched_zone check missed those. Default
        # 0.0 = preserve existing strict behavior. A value of e.g. 0.003
        # = 0.3% of close treats near-edge reversals as valid retests.
        edge_tolerance = max(0.0, abs(close) * float(self.params.get("anti_chase_fvg_edge_tolerance_pct", 0.0) or 0.0))
        # Trend-MA reclaim gate: by default the confirming bar's close must
        # also be above min(VWAP, EMA9) for longs (or below max for shorts).
        # On microcap squeeze names that gap 50%+ and pull back hard into
        # earlier FVGs, VWAP/EMA9 lag well above the retest zone, so the
        # gate blocks exactly the deep-retest entries the strategy wants.
        # Setting this to True drops the trend-MA reclaim and keeps only the
        # FVG-midpoint reclaim + bar_confirm shape check. Default False
        # preserves prior behavior for every other strategy.
        skip_trend_reclaim = bool(self.params.get("anti_chase_fvg_retest_skip_vwap_ema9_reclaim", False))
        direction_label = "bullish" if side == Side.LONG else "bearish"
        out["metadata"].update(
            {
                "anti_chase_fvg_retest_side": direction_label,
                "anti_chase_fvg_retest_same_state": same_state,
                "anti_chase_fvg_retest_opposing_state": opposing_state,
                "anti_chase_fvg_retest_same_midpoint": midpoint,
                "anti_chase_fvg_retest_same_lower": lower,
                "anti_chase_fvg_retest_same_upper": upper,
                "anti_chase_fvg_retest_same_distance_pct": same_distance_pct,
                "anti_chase_fvg_retest_opposing_distance_pct": opposing_distance_pct,
                "anti_chase_fvg_retest_trigger_level": float(trigger_level or 0.0),
            }
        )
        if lower is None or upper is None or midpoint is None or same_state not in {"active", "validated"}:
            if same_state == "invalidated":
                out["status"] = "reject"
                out["reason"] = f"{direction_label}_fvg_retest_rejected({_detail_fields(detail='same_direction_gap_invalidated', midpoint=midpoint or 0.0)})"
                out["metadata"]["anti_chase_fvg_retest_status"] = out["status"]
            return out
        in_gap = lower - touch_tolerance <= close <= upper + touch_tolerance
        if not in_gap and (same_distance_pct is None or float(same_distance_pct) > max_gap_distance_pct):
            return out
        recent = frame.tail(lookback_bars + 1)
        prior = recent.iloc[:-1]
        if side == Side.LONG:
            impulse_seen = bool(breakout_active)
            if not impulse_seen and trigger_level > 0 and not prior.empty:
                impulse_seen = float(prior["close"].max()) >= (float(trigger_level) * (1.0 - trigger_tolerance_pct))
        else:
            impulse_seen = bool(breakout_active)
            if not impulse_seen and trigger_level > 0 and not prior.empty:
                impulse_seen = float(prior["close"].min()) <= (float(trigger_level) * (1.0 + trigger_tolerance_pct))
        if not impulse_seen:
            return out
        opposing_blocked = bool(opposing_state in {"active", "validated"} and opposing_distance_pct is not None and float(opposing_distance_pct) <= max_opposing_distance_pct)
        if opposing_blocked:
            out["status"] = "reject"
            out["reason"] = f"{direction_label}_fvg_retest_rejected({_detail_fields(detail='opposing_gap_too_close', opposing_distance_pct=opposing_distance_pct or 0.0)})"
            out["metadata"]["anti_chase_fvg_retest_status"] = out["status"]
            return out
        last = frame.iloc[-1]
        bar_low = _safe_float(last.get("low"), close)
        bar_high = _safe_float(last.get("high"), close)
        close_pos = _bar_close_position(frame)
        touched_zone = bar_low <= (upper + touch_tolerance + edge_tolerance) and bar_high >= (lower - touch_tolerance - edge_tolerance)
        if side == Side.LONG:
            respected_zone = bar_low >= (lower - invalidation_tolerance)
            reclaimed = close >= (midpoint - touch_tolerance)
            if not skip_trend_reclaim:
                reclaimed = reclaimed and close >= min(float(vwap or close), float(ema9 or close))
            bar_confirm = close_pos >= min_close_pos
            stop_anchor = max(0.01, lower - (size * stop_buffer_gap_frac))
        else:
            respected_zone = bar_high <= (upper + invalidation_tolerance)
            reclaimed = close <= (midpoint + touch_tolerance)
            if not skip_trend_reclaim:
                reclaimed = reclaimed and close <= max(float(vwap or close), float(ema9 or close))
            bar_confirm = close_pos <= (1.0 - min_close_pos)
            stop_anchor = upper + (size * stop_buffer_gap_frac)
        out["metadata"]["anti_chase_fvg_retest_recent_impulse"] = bool(impulse_seen)
        out["metadata"]["anti_chase_fvg_retest_touched_zone"] = bool(touched_zone)
        out["metadata"]["anti_chase_fvg_retest_respected_zone"] = bool(respected_zone)
        out["metadata"]["anti_chase_fvg_retest_bar_confirm"] = bool(bar_confirm)
        if touched_zone and respected_zone and reclaimed and bar_confirm:
            out["status"] = "allow"
            out["stop_anchor"] = float(stop_anchor)
            out["metadata"].update(
                {
                    "anti_chase_fvg_retest_status": "allow",
                    "anti_chase_fvg_retest_confirmed": True,
                    "anti_chase_fvg_retest_stop_anchor": float(stop_anchor),
                }
            )
            return out
        out["status"] = "wait"
        out["reason"] = f"wait_for_{direction_label}_fvg_retest({_detail_fields(state=same_state, midpoint=midpoint, trigger=trigger_level, distance_pct=same_distance_pct or 0.0)})"
        out["metadata"]["anti_chase_fvg_retest_status"] = out["status"]
        return out

    def _continuation_ob_retest_plan(
        self,
        side: Side,
        symbol: str,
        frame: pd.DataFrame | None,
        data=None,
        *,
        trigger_level: float,
        breakout_active: bool,
        close: float,
        vwap: float,
        ema9: float,
    ) -> dict[str, Any]:
        """Order-block retest plan, parallel to `_continuation_fvg_retest_plan`.

        Returns the same {status, reason, metadata, stop_anchor} dict shape so
        it composes with `_apply_continuation_zone_retest_plans`. Reuses the
        same `anti_chase_fvg_retest_*` knobs for confirmation thresholds — the
        user-stated convention is "same rules for confirm" between FVG and OB.
        Disabled by default; opt in via `support_resistance.ltf_order_blocks_enabled`.
        """
        out: dict[str, Any] = {"status": "none", "reason": None, "metadata": {}, "stop_anchor": None}
        if frame is None or frame.empty:
            return out
        if not bool(self._support_resistance_setting("ltf_order_blocks_enabled", False)):
            return out
        close = float(close or 0.0)
        if close <= 0:
            return out
        ob_ctx = self.strategy._ltf_order_block_context(symbol, frame, data)
        same_ob = getattr(ob_ctx, "nearest_bullish_ob", None) if side == Side.LONG else getattr(ob_ctx, "nearest_bearish_ob", None)
        opposing_ob = getattr(ob_ctx, "nearest_bearish_ob", None) if side == Side.LONG else getattr(ob_ctx, "nearest_bullish_ob", None)
        same_info = self._fvg_gap_state(same_ob, close)
        opposing_info = self._fvg_gap_state(opposing_ob, close)
        same_state = str(same_info.get("state", "none") or "none").strip().lower()
        opposing_state = str(opposing_info.get("state", "none") or "none").strip().lower()
        lower = _optional_float(same_info.get("lower"))
        upper = _optional_float(same_info.get("upper"))
        midpoint = _optional_float(same_info.get("midpoint"))
        size = max(1e-8, float(_optional_float(same_info.get("size"), 0.0) or 0.0))
        same_distance_pct = _optional_float(same_info.get("distance_pct"), 1.0)
        opposing_distance_pct = _optional_float(opposing_info.get("distance_pct"))
        max_gap_distance_pct = max(0.0002, float(self.params.get("anti_chase_fvg_retest_max_gap_distance_pct", 0.0030)))
        max_opposing_distance_pct = max(0.0002, float(self.params.get("anti_chase_fvg_retest_max_opposing_distance_pct", 0.0020)))
        lookback_bars = max(2, int(self.params.get("anti_chase_fvg_retest_lookback_bars", 5)))
        min_close_pos_raw = self.params.get("anti_chase_fvg_retest_min_close_position")
        if min_close_pos_raw is None:
            min_close_pos_raw = self.params.get("min_bar_close_position", 0.60)
        min_close_pos = min(0.95, max(0.05, float(min_close_pos_raw)))
        stop_buffer_gap_frac = max(0.0, float(self.params.get("anti_chase_fvg_retest_stop_buffer_gap_frac", 0.14)))
        trigger_tolerance_pct = max(0.0, float(self.params.get("anti_chase_fvg_retest_trigger_tolerance_pct", 0.0012)))
        touch_tolerance = max(size * 0.20, abs(close) * max_gap_distance_pct * 0.25, 1e-8)
        invalidation_tolerance = max(size * 0.18, abs(close) * 1e-6, 1e-8)
        edge_tolerance = max(0.0, abs(close) * float(self.params.get("anti_chase_fvg_edge_tolerance_pct", 0.0) or 0.0))
        skip_trend_reclaim = bool(self.params.get("anti_chase_fvg_retest_skip_vwap_ema9_reclaim", False))
        direction_label = "bullish" if side == Side.LONG else "bearish"
        out["metadata"].update(
            {
                "anti_chase_ob_retest_side": direction_label,
                "anti_chase_ob_retest_same_state": same_state,
                "anti_chase_ob_retest_opposing_state": opposing_state,
                "anti_chase_ob_retest_same_midpoint": midpoint,
                "anti_chase_ob_retest_same_lower": lower,
                "anti_chase_ob_retest_same_upper": upper,
                "anti_chase_ob_retest_same_distance_pct": same_distance_pct,
                "anti_chase_ob_retest_opposing_distance_pct": opposing_distance_pct,
                "anti_chase_ob_retest_trigger_level": float(trigger_level or 0.0),
                "anti_chase_ob_retest_mode": str(getattr(ob_ctx, "mode", "loose") or "loose"),
            }
        )
        if lower is None or upper is None or midpoint is None or same_state not in {"active", "validated"}:
            if same_state == "invalidated":
                out["status"] = "reject"
                out["reason"] = f"{direction_label}_ob_retest_rejected({_detail_fields(detail='same_direction_block_invalidated', midpoint=midpoint or 0.0)})"
                out["metadata"]["anti_chase_ob_retest_status"] = out["status"]
            return out
        in_zone = lower - touch_tolerance <= close <= upper + touch_tolerance
        if not in_zone and (same_distance_pct is None or float(same_distance_pct) > max_gap_distance_pct):
            return out
        recent = frame.tail(lookback_bars + 1)
        prior = recent.iloc[:-1]
        if side == Side.LONG:
            impulse_seen = bool(breakout_active)
            if not impulse_seen and trigger_level > 0 and not prior.empty:
                impulse_seen = float(prior["close"].max()) >= (float(trigger_level) * (1.0 - trigger_tolerance_pct))
        else:
            impulse_seen = bool(breakout_active)
            if not impulse_seen and trigger_level > 0 and not prior.empty:
                impulse_seen = float(prior["close"].min()) <= (float(trigger_level) * (1.0 + trigger_tolerance_pct))
        if not impulse_seen:
            return out
        opposing_blocked = bool(opposing_state in {"active", "validated"} and opposing_distance_pct is not None and float(opposing_distance_pct) <= max_opposing_distance_pct)
        if opposing_blocked:
            out["status"] = "reject"
            out["reason"] = f"{direction_label}_ob_retest_rejected({_detail_fields(detail='opposing_block_too_close', opposing_distance_pct=opposing_distance_pct or 0.0)})"
            out["metadata"]["anti_chase_ob_retest_status"] = out["status"]
            return out
        last = frame.iloc[-1]
        bar_low = _safe_float(last.get("low"), close)
        bar_high = _safe_float(last.get("high"), close)
        close_pos = _bar_close_position(frame)
        touched_zone = bar_low <= (upper + touch_tolerance + edge_tolerance) and bar_high >= (lower - touch_tolerance - edge_tolerance)
        if side == Side.LONG:
            respected_zone = bar_low >= (lower - invalidation_tolerance)
            reclaimed = close >= (midpoint - touch_tolerance)
            if not skip_trend_reclaim:
                reclaimed = reclaimed and close >= min(float(vwap or close), float(ema9 or close))
            bar_confirm = close_pos >= min_close_pos
            stop_anchor = max(0.01, lower - (size * stop_buffer_gap_frac))
        else:
            respected_zone = bar_high <= (upper + invalidation_tolerance)
            reclaimed = close <= (midpoint + touch_tolerance)
            if not skip_trend_reclaim:
                reclaimed = reclaimed and close <= max(float(vwap or close), float(ema9 or close))
            bar_confirm = close_pos <= (1.0 - min_close_pos)
            stop_anchor = upper + (size * stop_buffer_gap_frac)
        out["metadata"]["anti_chase_ob_retest_recent_impulse"] = bool(impulse_seen)
        out["metadata"]["anti_chase_ob_retest_touched_zone"] = bool(touched_zone)
        out["metadata"]["anti_chase_ob_retest_respected_zone"] = bool(respected_zone)
        out["metadata"]["anti_chase_ob_retest_bar_confirm"] = bool(bar_confirm)
        if touched_zone and respected_zone and reclaimed and bar_confirm:
            out["status"] = "allow"
            out["stop_anchor"] = float(stop_anchor)
            out["metadata"].update(
                {
                    "anti_chase_ob_retest_status": "allow",
                    "anti_chase_ob_retest_confirmed": True,
                    "anti_chase_ob_retest_stop_anchor": float(stop_anchor),
                }
            )
            return out
        out["status"] = "wait"
        out["reason"] = f"wait_for_{direction_label}_ob_retest({_detail_fields(state=same_state, midpoint=midpoint, trigger=trigger_level, distance_pct=same_distance_pct or 0.0)})"
        out["metadata"]["anti_chase_ob_retest_status"] = out["status"]
        return out

    @staticmethod
    def _apply_continuation_zone_retest_plans(
        reasons: list[str],
        plans: list[dict[str, Any] | None],
        *,
        deferrable_prefixes: set[str],
    ) -> list[str]:
        """Combine multiple retest plans (e.g. FVG + OB) with OR logic.

        - If ANY plan returns ``status="allow"`` and all current reasons are
          deferrable, clear the reasons (entry can fire).
        - Otherwise prefer "wait" reasons over "reject" reasons (waiting
          could still resolve in a later bar).
        - Plans with ``status="none"`` (no zone available) are ignored.
        - When called with a single-plan list, behavior matches the prior
          ``_apply_continuation_fvg_retest_plan`` (now removed) exactly:
          allow on the only plan clears deferrable reasons; wait/reject
          replaces with the plan reason; none returns reasons unchanged.
        """
        if not reasons or not plans:
            return reasons
        engaged = [
            (p, str(p.get("status", "none") or "none").strip().lower())
            for p in plans
            if p
        ]
        engaged = [(p, s) for p, s in engaged if s != "none"]
        if not engaged:
            return reasons
        deferred = [reason for reason in reasons if _reason_prefix(reason) in deferrable_prefixes]
        other = [reason for reason in reasons if _reason_prefix(reason) not in deferrable_prefixes]
        if not deferred or other:
            return reasons
        if any(s == "allow" for _p, s in engaged):
            return []
        wait_plans = [p for p, s in engaged if s == "wait" and (str(p.get("reason") or "").strip())]
        if wait_plans:
            return [str(wait_plans[0]["reason"]).strip()]
        reject_plans = [p for p, s in engaged if s == "reject" and (str(p.get("reason") or "").strip())]
        if reject_plans:
            return [str(reject_plans[0]["reason"]).strip()]
        return reasons

    @staticmethod
    def _blocks_bullish_entry(ctx) -> bool:
        return bool(
            ctx.matched_bearish_reversal
            or ctx.bias_score <= -0.75
            or (len(ctx.matched_bearish_continuation) >= 2 and not ctx.matched_bullish_continuation)
        )

    @staticmethod
    def _blocks_bearish_entry(ctx) -> bool:
        return bool(
            ctx.matched_bullish_reversal
            or ctx.bias_score >= 0.75
            or (len(ctx.matched_bullish_continuation) >= 2 and not ctx.matched_bearish_continuation)
        )

    # An opposing CHoCH refuses; an opposing bias refuses unless a same-side
    # BoS is still fresh. The escape can only apply on an inverted reference
    # pair (the reference low above the reference high, after a gap) with
    # the close through both: _resolve_structure_bias then reads the later
    # break, and the earlier one can still be fresh. Anywhere else a fresh
    # BoS resolves the bias to its own side first (the LTF context the veto
    # reads is priced at its frame's last close). Until 2026-09-25 the
    # resolver read every such close bullish, so only the SHORT escape could
    # fire (it offset that tilt); the resolver now reads both sides alike,
    # so the LONG escape, unchanged, is reachable in the mirror case.
    def _blocks_bullish_structure_entry(self, ms_ctx) -> bool:
        if self.strategy._active_structure_break(bool(getattr(ms_ctx, "choch_down", False)), getattr(ms_ctx, "choch_down_age_bars", None)):
            return True
        active_bos_up = self.strategy._active_structure_break(bool(getattr(ms_ctx, "bos_up", False)), getattr(ms_ctx, "bos_up_age_bars", None))
        return bool(getattr(ms_ctx, "bias", "neutral") == "bearish" and not active_bos_up)

    def _blocks_bearish_structure_entry(self, ms_ctx) -> bool:
        if self.strategy._active_structure_break(bool(getattr(ms_ctx, "choch_up", False)), getattr(ms_ctx, "choch_up_age_bars", None)):
            return True
        active_bos_down = self.strategy._active_structure_break(bool(getattr(ms_ctx, "bos_down", False)), getattr(ms_ctx, "bos_down_age_bars", None))
        return bool(getattr(ms_ctx, "bias", "neutral") == "bullish" and not active_bos_down)

    def _bullish_structure_block_reason(self, ms_ctx) -> str:
        lookback = int(self._support_resistance_setting("structure_event_lookback_bars", 6) or 6)
        return (
            f"market_structure_bearish(bias={getattr(ms_ctx, 'bias', 'neutral')},"
            f"last_high={getattr(ms_ctx, 'last_high_label', 'na')},"
            f"last_low={getattr(ms_ctx, 'last_low_label', 'na')},"
            f"choch_down_age={getattr(ms_ctx, 'choch_down_age_bars', 'na')},"
            f"max_age={lookback})"
        )

    def _bearish_structure_block_reason(self, ms_ctx) -> str:
        lookback = int(self._support_resistance_setting("structure_event_lookback_bars", 6) or 6)
        return (
            f"market_structure_bullish(bias={getattr(ms_ctx, 'bias', 'neutral')},"
            f"last_high={getattr(ms_ctx, 'last_high_label', 'na')},"
            f"last_low={getattr(ms_ctx, 'last_low_label', 'na')},"
            f"choch_up_age={getattr(ms_ctx, 'choch_up_age_bars', 'na')},"
            f"max_age={lookback})"
        )

    def _blocks_bullish_sr_entry(self, sr_ctx) -> bool:
        if _sr_flag_blocks(sr_ctx, Side.LONG):
            return True

        dist_pct, dist_atr = _htf_clearance(sr_ctx, "resistance")
        too_close = False
        if dist_pct is not None and dist_pct <= float(self._support_resistance_setting("entry_min_clearance_pct", 0.0038)):
            too_close = True
        if dist_atr is not None and dist_atr <= float(self._support_resistance_setting("entry_min_clearance_atr", 0.85)):
            too_close = True
        # No breakout escape: once a resistance breaks, nearest_resistance is
        # the next wall above price and the clearance is measured to it
        # (2026-04-20: META/INTC/TSLA LONG'd 0.06-0.5 ATR under that wall on
        # a stale breakout flag). The escape that excused price "above
        # nearest_resistance" tested a state the builders never report
        # (nearest_resistance is at or above price) and was removed
        # 2026-09-23.
        return too_close

    def _blocks_bearish_sr_entry(self, sr_ctx) -> bool:
        if _sr_flag_blocks(sr_ctx, Side.SHORT):
            return True

        dist_pct, dist_atr = _htf_clearance(sr_ctx, "support")
        too_close = False
        if dist_pct is not None and dist_pct <= float(self._support_resistance_setting("entry_min_clearance_pct", 0.0038)):
            too_close = True
        if dist_atr is not None and dist_atr <= float(self._support_resistance_setting("entry_min_clearance_atr", 0.85)):
            too_close = True
        # No breakdown escape, symmetric with the bullish path above.
        return too_close

    def _refine_bullish_sr_levels(self, close: float, stop: float, target: float | None, sr_ctx, frame: pd.DataFrame | None):
        if not self.config.shared_entry.use_sr_stop_target_refinement:
            return float(stop), (None if target is None else float(target))
        level_buffer = float(sr_ctx.level_buffer or 0.0)
        if sr_ctx.nearest_support and close > float(sr_ctx.nearest_support.price):
            support_stop = float(sr_ctx.nearest_support.price) - level_buffer
            if support_stop < close:
                stop = self._clamp_refined_stop(
                    close, stop, max(float(stop), support_stop),
                    self.strategy._frame_atr14(frame, close),
                )
        if target is not None and sr_ctx.nearest_resistance and close < float(sr_ctx.nearest_resistance.price):
            capped_target = max(close * 1.001, float(sr_ctx.nearest_resistance.price) - level_buffer)
            proposed_target = min(float(target), capped_target)
            # R:R floor: only accept the cap if the resulting reward is still
            # tradeable. Without this guard, a nearby resistance can crush
            # R:R toward zero ($0.10 targets, etc.).
            if self._target_meets_min_rr(Side.LONG, close, stop, proposed_target):
                target = proposed_target
        return float(stop), (None if target is None else float(target))

    def _refine_bearish_sr_levels(self, close: float, stop: float, target: float | None, sr_ctx, frame: pd.DataFrame | None):
        if not self.config.shared_entry.use_sr_stop_target_refinement:
            return float(stop), (None if target is None else float(target))
        level_buffer = float(sr_ctx.level_buffer or 0.0)
        if sr_ctx.nearest_resistance and close < float(sr_ctx.nearest_resistance.price):
            resistance_stop = float(sr_ctx.nearest_resistance.price) + level_buffer
            if resistance_stop > close:
                stop = self._clamp_refined_stop(
                    close, stop, min(float(stop), resistance_stop),
                    self.strategy._frame_atr14(frame, close),
                )
        if target is not None and sr_ctx.nearest_support and close > float(sr_ctx.nearest_support.price):
            capped_target = min(close * 0.999, float(sr_ctx.nearest_support.price) + level_buffer)
            proposed_target = max(float(target), capped_target)
            # R:R floor — see comment on the bullish twin above.
            if self._target_meets_min_rr(Side.SHORT, close, stop, proposed_target):
                target = proposed_target
        return float(stop), (None if target is None else float(target))

    def _bullish_sr_block_reason(self, sr_ctx) -> str:
        # The branch that blocked. It used to report the clearance whichever
        # one did, so a breakdown-flag block read as too_close_to_htf_resistance.
        if _sr_flag_blocks(sr_ctx, Side.LONG):
            broken = getattr(sr_ctx, "broken_support", None)
            nearest = getattr(sr_ctx, "nearest_support", None)
            return (f"htf_breakdown_below_support(level={float(broken.price) if broken is not None else 'na'},"
                    f"close={float(sr_ctx.current_price):.4f},"
                    f"nearest_support={float(nearest.price) if nearest is not None else 'none'})")
        # Same clearance the check read: negative when price is above a
        # pending (unconfirmed-broken) resistance.
        dist_pct, dist_atr = _htf_clearance(sr_ctx, "resistance")
        return _reason_with_values(
            "too_close_to_htf_resistance",
            current=dist_pct,
            required=float(self._support_resistance_setting("entry_min_clearance_pct", 0.0038)),
            op=">",
            digits=4,
            extras={
                "clearance_atr": (dist_atr, ">", float(self._support_resistance_setting("entry_min_clearance_atr", 0.85))),
            },
        )

    def _bearish_sr_block_reason(self, sr_ctx) -> str:
        if _sr_flag_blocks(sr_ctx, Side.SHORT):
            broken = getattr(sr_ctx, "broken_resistance", None)
            nearest = getattr(sr_ctx, "nearest_resistance", None)
            return (f"htf_breakout_above_resistance(level={float(broken.price) if broken is not None else 'na'},"
                    f"close={float(sr_ctx.current_price):.4f},"
                    f"nearest_resistance={float(nearest.price) if nearest is not None else 'none'})")
        # Same clearance the check read: negative when price is below a
        # pending (unconfirmed-lost) support.
        dist_pct, dist_atr = _htf_clearance(sr_ctx, "support")
        return _reason_with_values(
            "too_close_to_htf_support",
            current=dist_pct,
            required=float(self._support_resistance_setting("entry_min_clearance_pct", 0.0038)),
            op=">",
            digits=4,
            extras={
                "clearance_atr": (dist_atr, ">", float(self._support_resistance_setting("entry_min_clearance_atr", 0.85))),
            },
        )

    @staticmethod
    def _dual_counter_divergence_reason(side: Side, tech_ctx) -> str | None:
        if side == Side.LONG and getattr(tech_ctx, "bearish_rsi_divergence", None) is not None and getattr(tech_ctx, "bearish_obv_divergence", None) is not None:
            return "dual_counter_divergence(rsi=bearish,obv=bearish)"
        if side == Side.SHORT and getattr(tech_ctx, "bullish_rsi_divergence", None) is not None and getattr(tech_ctx, "bullish_obv_divergence", None) is not None:
            return "dual_counter_divergence(rsi=bullish,obv=bullish)"
        return None

    def _refine_bullish_technical_levels(self, close: float, stop: float, target: float | None, tech_ctx, frame: pd.DataFrame | None):
        if not self.config.shared_entry.use_technical_stop_target_refinement:
            return float(stop), (None if target is None else float(target))
        if not bool(self._technical_level_setting("enabled", True)):
            return float(stop), (None if target is None else float(target))
        atr = self.strategy._frame_atr14(frame, close)
        buffer = max(atr * 0.12, close * 0.0010)
        if bool(self._technical_level_setting("stop_use_trendline", True)) and getattr(tech_ctx, "support_trendline", None) is not None:
            support_value = _safe_float(getattr(tech_ctx.support_trendline, "current_value", None), 0.0)
            trend_stop = support_value - buffer
            if 0 < trend_stop < close:
                stop = self._clamp_refined_stop(
                    close, stop, max(float(stop), float(trend_stop)), atr,
                )
        if target is not None:
            risk = max(close - float(stop), buffer)
            caps: list[float] = []
            if bool(self._technical_level_setting("target_use_fib", True)) and getattr(tech_ctx, "nearest_bullish_extension", None) is not None:
                caps.append(float(tech_ctx.nearest_bullish_extension) - buffer)
            channel_ctx = getattr(tech_ctx, "channel", None)
            if bool(self._technical_level_setting("target_use_channel", True)) and bool(getattr(channel_ctx, "valid", False)) and getattr(channel_ctx, "upper", None) is not None:
                caps.append(float(getattr(channel_ctx, "upper")) - buffer)
            if bool(self._technical_level_setting("target_use_bollinger", True)) and getattr(tech_ctx, "bollinger_upper", None) is not None and not bool(getattr(tech_ctx, "bollinger_squeeze", False)):
                caps.append(float(tech_ctx.bollinger_upper) - buffer)
            if bool(self._technical_level_setting("target_use_trendline", True)) and getattr(tech_ctx, "resistance_trendline", None) is not None and not bool(getattr(tech_ctx, "trendline_break_up", False)):
                caps.append(float(tech_ctx.resistance_trendline.current_value) - buffer)
            valid = [cap for cap in caps if cap > close + max(buffer, risk * 0.35)]
            if valid:
                proposed_target = min(float(target), min(valid))
                # R:R floor: don't let a tech level crush reward below the
                # configured min_target_rr. Falls back to the un-capped
                # target when the cap would harm the trade.
                if self._target_meets_min_rr(Side.LONG, close, stop, proposed_target):
                    target = proposed_target
        return float(stop), (None if target is None else float(target))

    def _refine_bearish_technical_levels(self, close: float, stop: float, target: float | None, tech_ctx, frame: pd.DataFrame | None):
        if not self.config.shared_entry.use_technical_stop_target_refinement:
            return float(stop), (None if target is None else float(target))
        if not bool(self._technical_level_setting("enabled", True)):
            return float(stop), (None if target is None else float(target))
        atr = self.strategy._frame_atr14(frame, close)
        buffer = max(atr * 0.12, close * 0.0010)
        if bool(self._technical_level_setting("stop_use_trendline", True)) and getattr(tech_ctx, "resistance_trendline", None) is not None:
            resistance_value = _safe_float(getattr(tech_ctx.resistance_trendline, "current_value", None), 0.0)
            trend_stop = resistance_value + buffer
            if trend_stop > close:
                stop = self._clamp_refined_stop(
                    close, stop, min(float(stop), float(trend_stop)), atr,
                )
        if target is not None:
            risk = max(float(stop) - close, buffer)
            caps: list[float] = []
            if bool(self._technical_level_setting("target_use_fib", True)) and getattr(tech_ctx, "nearest_bearish_extension", None) is not None:
                caps.append(float(tech_ctx.nearest_bearish_extension) + buffer)
            channel_ctx = getattr(tech_ctx, "channel", None)
            if bool(self._technical_level_setting("target_use_channel", True)) and bool(getattr(channel_ctx, "valid", False)) and getattr(channel_ctx, "lower", None) is not None:
                caps.append(float(getattr(channel_ctx, "lower")) + buffer)
            if bool(self._technical_level_setting("target_use_bollinger", True)) and getattr(tech_ctx, "bollinger_lower", None) is not None and not bool(getattr(tech_ctx, "bollinger_squeeze", False)):
                caps.append(float(tech_ctx.bollinger_lower) + buffer)
            if bool(self._technical_level_setting("target_use_trendline", True)) and getattr(tech_ctx, "support_trendline", None) is not None and not bool(getattr(tech_ctx, "trendline_break_down", False)):
                caps.append(float(tech_ctx.support_trendline.current_value) + buffer)
            valid = [cap for cap in caps if cap < close - max(buffer, risk * 0.35)]
            if valid:
                proposed_target = max(float(target), max(valid))
                # R:R floor — see comment on the bullish twin above.
                if self._target_meets_min_rr(Side.SHORT, close, stop, proposed_target):
                    target = proposed_target
        return float(stop), (None if target is None else float(target))

    def _technical_entry_adjustment(self, side: Side, tech_ctx) -> float:
        if not self.config.shared_entry.use_technical_entry_adjustment:
            return 0.0
        if not bool(self._technical_level_setting("enabled", True)):
            return 0.0
        bonus = 0.0
        channel_bonus = self._technical_weight("entry_bonus_channel_alignment", 0.25)
        trendline_bonus = self._technical_weight("entry_bonus_trendline_respect", 0.25)
        bollinger_midband_bonus = self._technical_weight("bollinger_entry_bonus_midband", 0.18)
        bollinger_outer_penalty = self._technical_weight("bollinger_entry_penalty_outer_band", 0.22)
        extension_penalty = self._technical_weight("entry_penalty_near_extension", 0.35)
        near_extension = self._technical_weight("fib_near_extension_pct", 0.0060)
        near_edge = self._technical_weight("channel_near_edge_pct", 0.18)
        channel = getattr(tech_ctx, "channel", None)
        position_pct = getattr(channel, "position_pct", None)
        bb_mid = getattr(tech_ctx, "bollinger_mid", None)
        bb_upper = getattr(tech_ctx, "bollinger_upper", None)
        bb_lower = getattr(tech_ctx, "bollinger_lower", None)
        bb_pct = getattr(tech_ctx, "bollinger_percent_b", None)
        bb_squeeze = bool(getattr(tech_ctx, "bollinger_squeeze", False))
        price = _safe_float(getattr(tech_ctx, "current_price", None), 0.0)
        adx = getattr(tech_ctx, "adx", None)
        dmi_bias = str(getattr(tech_ctx, "dmi_bias", "neutral") or "neutral")
        adx_rising = bool(getattr(tech_ctx, "adx_rising", False))
        adx_min = self._technical_weight("adx_min_strength", 18.0)
        adx_bonus = self._technical_weight("adx_entry_bonus", 0.22)
        adx_rising_bonus = self._technical_weight("adx_rising_bonus", 0.10)
        adx_weak_penalty = self._technical_weight("adx_weak_penalty", 0.12)
        open_avwap = getattr(tech_ctx, "anchored_vwap_open", None)
        bull_avwap = getattr(tech_ctx, "anchored_vwap_bullish_impulse", None)
        bear_avwap = getattr(tech_ctx, "anchored_vwap_bearish_impulse", None)
        avwap_bonus = self._technical_weight("anchored_vwap_entry_bonus", 0.20)
        avwap_penalty = self._technical_weight("anchored_vwap_entry_penalty", 0.18)
        atr_expansion = getattr(tech_ctx, "atr_expansion_mult", None)
        atr_expand_min = self._technical_weight("atr_expansion_min_mult", 0.80)
        atr_expand_bonus = self._technical_weight("atr_expansion_bonus", 0.14)
        atr_stretch_max = self._technical_weight("atr_stretch_penalty_mult", 2.80)
        atr_stretch_penalty = self._technical_weight("atr_stretch_penalty", 0.18)
        stretch_vwap = getattr(tech_ctx, "atr_stretch_vwap_mult", None)
        stretch_ema20 = getattr(tech_ctx, "atr_stretch_ema20_mult", None)
        obv_bias = str(getattr(tech_ctx, "obv_bias", "neutral") or "neutral")
        obv_bonus = self._technical_weight("obv_entry_bonus", 0.12)
        obv_penalty = self._technical_weight("obv_entry_penalty", 0.10)
        div_rsi_penalty = self._technical_weight("divergence_counter_rsi_penalty", 0.12)
        div_obv_penalty = self._technical_weight("divergence_counter_obv_penalty", 0.10)
        div_hidden_rsi_bonus = self._technical_weight("divergence_hidden_bonus_rsi", 0.10)
        div_hidden_obv_bonus = self._technical_weight("divergence_hidden_bonus_obv", 0.08)
        divergence_enabled = bool(self._technical_level_setting("divergence_enabled", True))
        if side == Side.LONG:
            if bool(self._technical_level_setting("trendline_enabled", True)):
                if bool(getattr(tech_ctx, "support_respected", False)):
                    bonus += trendline_bonus
                if bool(getattr(tech_ctx, "trendline_break_up", False)):
                    bonus += trendline_bonus * 0.8
            if bool(self._technical_level_setting("channel_enabled", True)) and bool(getattr(channel, "valid", False)) and position_pct is not None:
                # Alignment means price riding the lower 60% of a rising
                # channel, not below its floor. A channel stays valid while
                # the close is up to break_tolerance past a line, so
                # position_pct runs outside [0, 1]: until 2026-09-23 a LONG
                # under a bullish channel's floor (433 of 2,140 valid-channel
                # evaluations over the replay, position_pct down to -1.22)
                # collected this bonus. The SHORT branch mirrors it.
                if str(getattr(channel, "bias", "neutral")) == "bullish" and 0.0 <= float(position_pct) <= 0.60:
                    bonus += channel_bonus
                # Buying INTO the upper edge is penalised; beyond it is a
                # breakout. A channel now stays valid on the bar that breaks
                # it (2026-09-23), so an unbounded test would dock every
                # decisive upside break.
                if 1.0 - near_edge <= float(position_pct) <= 1.0:
                    bonus -= channel_bonus
            if bool(self._technical_level_setting("fib_enabled", True)):
                dist = getattr(tech_ctx, "bullish_extension_distance_pct", None)
                if dist is not None and float(dist) <= near_extension:
                    bonus -= extension_penalty
            if bool(self._technical_level_setting("bollinger_enabled", True)) and bb_mid is not None and bb_upper is not None and bb_lower is not None and price > 0:
                if price >= float(bb_mid) and (bb_pct is None or float(bb_pct) <= 0.82):
                    bonus += bollinger_midband_bonus
                if price >= float(bb_upper) or (bb_pct is not None and float(bb_pct) >= 0.96):
                    bonus -= bollinger_outer_penalty
                if bb_squeeze and bool(getattr(tech_ctx, "trendline_break_up", False)):
                    bonus += bollinger_midband_bonus * 0.5
            if bool(self._technical_level_setting("adx_enabled", True)) and adx is not None:
                if dmi_bias == "bullish" and float(adx) >= adx_min:
                    bonus += adx_bonus
                    if adx_rising:
                        bonus += adx_rising_bonus
                elif float(adx) < adx_min * 0.8 and not bb_squeeze:
                    bonus -= adx_weak_penalty
            if bool(self._technical_level_setting("anchored_vwap_enabled", True)) and price > 0:
                if open_avwap is not None and price >= float(open_avwap):
                    bonus += avwap_bonus * 0.6
                elif open_avwap is not None:
                    bonus -= avwap_penalty * 0.6
                if bull_avwap is not None and price >= float(bull_avwap):
                    bonus += avwap_bonus
                elif bull_avwap is not None:
                    bonus -= avwap_penalty
            if bool(self._technical_level_setting("atr_context_enabled", True)):
                if atr_expansion is not None and float(atr_expansion) >= atr_expand_min and (stretch_vwap is None or float(stretch_vwap) <= atr_stretch_max):
                    bonus += atr_expand_bonus
                if stretch_vwap is not None and float(stretch_vwap) >= atr_stretch_max:
                    bonus -= atr_stretch_penalty
                if stretch_ema20 is not None and float(stretch_ema20) >= atr_stretch_max:
                    bonus -= atr_stretch_penalty * 0.75
            if bool(self._technical_level_setting("obv_enabled", True)):
                if obv_bias == "bullish":
                    bonus += obv_bonus
                elif obv_bias == "bearish":
                    bonus -= obv_penalty
            if divergence_enabled:
                # Counter-direction REGULAR divergence -> reversal warning,
                # penalize a LONG entry. Hidden divergence in same direction
                # -> continuation, bonus.
                if getattr(tech_ctx, "bearish_rsi_divergence", None) is not None:
                    bonus -= div_rsi_penalty
                if getattr(tech_ctx, "bearish_obv_divergence", None) is not None:
                    bonus -= div_obv_penalty
                if getattr(tech_ctx, "bullish_hidden_rsi_divergence", None) is not None:
                    bonus += div_hidden_rsi_bonus
                if getattr(tech_ctx, "bullish_hidden_obv_divergence", None) is not None:
                    bonus += div_hidden_obv_bonus
        else:
            if bool(self._technical_level_setting("trendline_enabled", True)):
                if bool(getattr(tech_ctx, "resistance_respected", False)):
                    bonus += trendline_bonus
                if bool(getattr(tech_ctx, "trendline_break_down", False)):
                    bonus += trendline_bonus * 0.8
            if bool(self._technical_level_setting("channel_enabled", True)) and bool(getattr(channel, "valid", False)) and position_pct is not None:
                if str(getattr(channel, "bias", "neutral")) == "bearish" and 0.40 <= float(position_pct) <= 1.0:
                    bonus += channel_bonus
                if 0.0 <= float(position_pct) <= near_edge:
                    bonus -= channel_bonus
            if bool(self._technical_level_setting("fib_enabled", True)):
                dist = getattr(tech_ctx, "bearish_extension_distance_pct", None)
                if dist is not None and float(dist) <= near_extension:
                    bonus -= extension_penalty
            if bool(self._technical_level_setting("bollinger_enabled", True)) and bb_mid is not None and bb_upper is not None and bb_lower is not None and price > 0:
                if price <= float(bb_mid) and (bb_pct is None or float(bb_pct) >= 0.18):
                    bonus += bollinger_midband_bonus
                if price <= float(bb_lower) or (bb_pct is not None and float(bb_pct) <= 0.04):
                    bonus -= bollinger_outer_penalty
                if bb_squeeze and bool(getattr(tech_ctx, "trendline_break_down", False)):
                    bonus += bollinger_midband_bonus * 0.5
            if bool(self._technical_level_setting("adx_enabled", True)) and adx is not None:
                if dmi_bias == "bearish" and float(adx) >= adx_min:
                    bonus += adx_bonus
                    if adx_rising:
                        bonus += adx_rising_bonus
                elif float(adx) < adx_min * 0.8 and not bb_squeeze:
                    bonus -= adx_weak_penalty
            if bool(self._technical_level_setting("anchored_vwap_enabled", True)) and price > 0:
                if open_avwap is not None and price <= float(open_avwap):
                    bonus += avwap_bonus * 0.6
                elif open_avwap is not None:
                    bonus -= avwap_penalty * 0.6
                if bear_avwap is not None and price <= float(bear_avwap):
                    bonus += avwap_bonus
                elif bear_avwap is not None:
                    bonus -= avwap_penalty
            if bool(self._technical_level_setting("atr_context_enabled", True)):
                if atr_expansion is not None and float(atr_expansion) >= atr_expand_min and (stretch_vwap is None or float(stretch_vwap) <= atr_stretch_max):
                    bonus += atr_expand_bonus
                if stretch_vwap is not None and float(stretch_vwap) >= atr_stretch_max:
                    bonus -= atr_stretch_penalty
                if stretch_ema20 is not None and float(stretch_ema20) >= atr_stretch_max:
                    bonus -= atr_stretch_penalty * 0.75
            if bool(self._technical_level_setting("obv_enabled", True)):
                if obv_bias == "bearish":
                    bonus += obv_bonus
                elif obv_bias == "bullish":
                    bonus -= obv_penalty
            if divergence_enabled:
                # Counter-direction REGULAR divergence -> reversal warning,
                # penalize a SHORT entry. Hidden divergence in same direction
                # -> continuation, bonus.
                if getattr(tech_ctx, "bullish_rsi_divergence", None) is not None:
                    bonus -= div_rsi_penalty
                if getattr(tech_ctx, "bullish_obv_divergence", None) is not None:
                    bonus -= div_obv_penalty
                if getattr(tech_ctx, "bearish_hidden_rsi_divergence", None) is not None:
                    bonus += div_hidden_rsi_bonus
                if getattr(tech_ctx, "bearish_hidden_obv_divergence", None) is not None:
                    bonus += div_hidden_obv_bonus
        return float(bonus)

    def _sr_entry_adjustment_components(self, side: Side, sr_ctx) -> dict[str, float]:
        out = {
            "sr_directional_bias": 0.0,
            "sr_bias_component": 0.0,
            "sr_favorable_proximity_score": 0.0,
            "sr_opposing_proximity_score": 0.0,
            "sr_entry_adjustment": 0.0,
        }
        if not bool(self._support_resistance_setting("entry_proximity_scoring_enabled", True)):
            return out
        if not bool(self._support_resistance_setting("enabled", True)):
            return out
        if sr_ctx is None:
            return out
        try:
            raw_bias = _safe_float(getattr(sr_ctx, "bias_score", 0.0), 0.0)
            directional_bias = raw_bias if side == Side.LONG else -raw_bias
            bias_weight = max(0.0, self._sr_weight("entry_bias_score_weight", 0.60))
            favorable_bonus = max(0.0, self._sr_weight("entry_favorable_proximity_bonus", 0.35))
            opposing_penalty = max(0.0, self._sr_weight("entry_opposing_proximity_penalty", 0.35))
            proximity_window_atr = max(0.05, self._sr_weight("proximity_atr_mult", 0.75))
            bias_component = directional_bias * bias_weight

            # A pending level (crossed, flip unconfirmed) is the level price
            # is at, so it counts as near at the clearance the gates read
            # (``_htf_clearance``: negative, a full proximity score). Reading
            # nearest_* alone, a bounce testing a just-pierced support got no
            # favorable bonus (2026-09-23). The breakdown / breakout flags do
            # not switch these off: they describe a broken level on the far
            # side of price, never the near one, and they are set on about
            # half of all checkpoints (2026-09-23).
            support_near = (
                bool(getattr(sr_ctx, "near_support", False)) or getattr(sr_ctx, "pending_support", None) is not None
            )
            resistance_near = (
                bool(getattr(sr_ctx, "near_resistance", False)) or getattr(sr_ctx, "pending_resistance", None) is not None
            )
            support_dist = _optional_float(_htf_clearance(sr_ctx, "support")[1])
            resistance_dist = _optional_float(_htf_clearance(sr_ctx, "resistance")[1])
            if side == Side.LONG:
                favorable_near, favorable_dist = support_near, support_dist
                opposing_near, opposing_dist = resistance_near, resistance_dist
            else:
                favorable_near, favorable_dist = resistance_near, resistance_dist
                opposing_near, opposing_dist = support_near, support_dist

            def _proximity_score(dist_atr: float | None) -> float:
                if dist_atr is None:
                    return 0.0
                return max(0.0, min(1.0, 1.0 - (float(dist_atr) / proximity_window_atr)))

            favorable_score = favorable_bonus * _proximity_score(favorable_dist) if favorable_near else 0.0
            opposing_score = opposing_penalty * _proximity_score(opposing_dist) if opposing_near else 0.0
            total = bias_component + favorable_score - opposing_score
            out.update({
                "sr_directional_bias": round(directional_bias, 4),
                "sr_bias_component": round(bias_component, 4),
                "sr_favorable_proximity_score": round(favorable_score, 4),
                "sr_opposing_proximity_score": round(opposing_score, 4),
                "sr_entry_adjustment": round(total, 4),
            })
        except Exception:
            return out
        return out

    def _entry_adjustment_components(self, side: Side, sr_ctx=None, tech_ctx=None, htf_ctx=None) -> dict[str, float]:
        sr_fields = self._sr_entry_adjustment_components(side, sr_ctx)
        tech_adjustment = round(self._technical_entry_adjustment(side, tech_ctx), 4) if tech_ctx is not None else 0.0
        htf_div_adjustment = round(self._htf_divergence_adjustment(side, htf_ctx), 4) if htf_ctx is not None else 0.0
        total = round(float(sr_fields.get("sr_entry_adjustment", 0.0)) + tech_adjustment + htf_div_adjustment, 4)
        return {
            **sr_fields,
            "technical_entry_adjustment": tech_adjustment,
            "htf_divergence_adjustment": htf_div_adjustment,
            "entry_context_adjustment": total,
        }

    def _htf_divergence_adjustment(self, side: Side, htf_ctx) -> float:
        """Multi-timeframe divergence confluence adjustment.

        HTF same-direction divergence (regular bullish for LONG, regular
        bearish for SHORT) signals a higher-timeframe reversal aligned with
        the trade — bonus. Counter-direction HTF divergence (regular bullish
        for SHORT, regular bearish for LONG) signals a HTF reversal AGAINST
        the trade — penalty. HTF hidden divergence in trade direction signals
        HTF continuation aligned with the trade — small bonus.
        """
        if not self.config.shared_entry.use_htf_divergence_score:
            return 0.0
        if htf_ctx is None:
            return 0.0
        bonus_aligned = self._technical_weight("htf_divergence_aligned_bonus_rsi", 0.20)
        penalty_counter = self._technical_weight("htf_divergence_counter_penalty_rsi", 0.25)
        bonus_hidden = self._technical_weight("htf_divergence_hidden_bonus_rsi", 0.10)
        bonus = 0.0
        if side == Side.LONG:
            if getattr(htf_ctx, "bullish_rsi_divergence", None) is not None:
                bonus += bonus_aligned
            if getattr(htf_ctx, "bearish_rsi_divergence", None) is not None:
                bonus -= penalty_counter
            if getattr(htf_ctx, "bullish_hidden_rsi_divergence", None) is not None:
                bonus += bonus_hidden
        else:
            if getattr(htf_ctx, "bearish_rsi_divergence", None) is not None:
                bonus += bonus_aligned
            if getattr(htf_ctx, "bullish_rsi_divergence", None) is not None:
                bonus -= penalty_counter
            if getattr(htf_ctx, "bearish_hidden_rsi_divergence", None) is not None:
                bonus += bonus_hidden
        return float(bonus)

    @staticmethod
    def _fvg_gap_state(gap: Any, current_price: float) -> dict[str, Any]:
        lower = _optional_float(getattr(gap, "lower", None))
        upper = _optional_float(getattr(gap, "upper", None))
        midpoint = _optional_float(getattr(gap, "midpoint", None))
        size = _optional_float(getattr(gap, "size", None))
        filled_pct = max(0.0, min(1.0, _optional_float(getattr(gap, "filled_pct", None), 0.0) or 0.0))
        direction = str(getattr(gap, "direction", "")).strip().lower()
        if lower is None or upper is None or midpoint is None or size is None or size <= 0:
            return {"state": "none", "direction": direction or "unknown", "distance": None, "distance_pct": None, "filled_pct": filled_pct}
        close = float(current_price or 0.0)
        eps = max(float(size) * 0.05, abs(close) * 1e-6, 1e-8)
        if close < lower:
            distance = float(lower - close)
        elif close > upper:
            distance = float(close - upper)
        else:
            distance = 0.0
        if direction == "bullish":
            state = "invalidated" if close < lower - eps else ("active" if close <= upper + eps else "validated")
        elif direction == "bearish":
            state = "invalidated" if close > upper + eps else ("active" if close >= lower - eps else "validated")
        else:
            state = "active" if lower - eps <= close <= upper + eps else "validated"
        return {
            "state": state,
            "direction": direction or "unknown",
            "lower": float(lower),
            "upper": float(upper),
            "midpoint": float(midpoint),
            "size": float(size),
            "filled_pct": filled_pct,
            "distance": float(distance),
            "distance_pct": float(distance / max(abs(close), 1e-9)) if close else None,
        }

    def _score_fvg_context(self, current_price: float, ctx: Any, *, timeframe_minutes: int) -> dict[str, Any]:
        close = float(current_price or 0.0)
        tf = max(1, int(timeframe_minutes or 1))
        is_htf = tf > 1
        valid_base = 0.37 if is_htf else 0.22
        active_base = 0.24 if is_htf else 0.15
        invalid_base = 0.42 if is_htf else 0.26
        proximity_floor = abs(close) * (0.0060 if is_htf else 0.0030)
        half_life_bars = 10.0 if is_htf else 14.0
        min_recency_factor = 0.30

        def _gap_recency_factor(gap: Any) -> float:
            stamp = getattr(gap, "last_seen", None) or getattr(gap, "first_seen", None)
            if not stamp:
                return 1.0
            try:
                seen = pd.Timestamp(stamp)
                if seen.tzinfo is not None:
                    seen = seen.tz_convert(None)
                current = pd.Timestamp(now_et())
                if current.tzinfo is not None:
                    current = current.tz_convert(None)
                age_seconds = max(0.0, float((current - seen).total_seconds()))
            except Exception:
                return 1.0
            age_bars = age_seconds / max(float(tf) * 60.0, 60.0)
            factor = 0.5 ** (age_bars / max(half_life_bars, 1.0))
            return float(max(min_recency_factor, min(1.0, factor)))

        def _score_gap(gap: Any) -> tuple[float, float, dict[str, Any]]:
            info = self._fvg_gap_state(gap, close)
            state = str(info.get("state", "none"))
            direction = str(info.get("direction", "unknown"))
            size = _optional_float(info.get("size"), 0.0) or 0.0
            distance = _optional_float(info.get("distance"), 0.0) or 0.0
            fill = max(0.0, min(1.0, _optional_float(info.get("filled_pct"), 0.0) or 0.0))
            if state == "none" or direction not in {"bullish", "bearish"}:
                return 0.0, 0.0, info
            distance_limit = max(float(size) * 2.5, float(proximity_floor), 1e-8)
            closeness = max(0.0, 1.0 - (float(distance) / distance_limit))
            if closeness <= 0.0:
                info["closeness"] = 0.0
                info["recency_factor"] = 0.0
                return 0.0, 0.0, info
            fill_damp = 1.0 - (0.35 * fill if state != "invalidated" else 0.0)
            recency_factor = _gap_recency_factor(gap)
            base = invalid_base if state == "invalidated" else (valid_base if state == "validated" else active_base)
            magnitude = float(base) * float(closeness) * float(fill_damp) * float(recency_factor)
            bull = 0.0
            bear = 0.0
            if direction == "bullish":
                if state == "invalidated":
                    bear += magnitude
                    bull -= magnitude * 0.80
                else:
                    bull += magnitude
                    bear -= magnitude * 0.80
            elif direction == "bearish":
                if state == "invalidated":
                    bull += magnitude
                    bear -= magnitude * 0.80
                else:
                    bear += magnitude
                    bull -= magnitude * 0.80
            info["closeness"] = float(closeness)
            info["recency_factor"] = float(recency_factor)
            info["score_magnitude"] = float(magnitude)
            return bull, bear, info

        bull_score = 0.0
        bear_score = 0.0
        nearest_bullish = getattr(ctx, "nearest_bullish_fvg", None)
        nearest_bearish = getattr(ctx, "nearest_bearish_fvg", None)
        bull_pos, bear_neg, bullish_info = _score_gap(nearest_bullish)
        bull_score += bull_pos
        bear_score += bear_neg
        bull_neg, bear_pos, bearish_info = _score_gap(nearest_bearish)
        bull_score += bull_neg
        bear_score += bear_pos
        directional_pressure = max(0.0, bull_score, bear_score)
        return {
            "bull_score": float(bull_score),
            "bear_score": float(bear_score),
            "directional_pressure": float(directional_pressure),
            "timeframe_minutes": tf,
            "nearest_bullish": bullish_info,
            "nearest_bearish": bearish_info,
        }

    def _fvg_entry_adjustment_components(self, side: Side, symbol: str, frame: pd.DataFrame | None, data=None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "fvg_context_enabled": False,
            "fvg_entry_adjustment": 0.0,
            "fvg_same_direction_score": 0.0,
            "fvg_opposing_score": 0.0,
            "fvg_continuation_bias": 0.0,
            "fvg_reversal_bias": 0.0,
            "fvg_same_direction_label": "bullish" if side == Side.LONG else "bearish",
            "fvg_opposing_label": "bearish" if side == Side.LONG else "bullish",
        }
        if frame is None or frame.empty or not self.config.shared_entry.use_fvg_context:
            return out
        close = _safe_float(frame.iloc[-1].get("close"), 0.0)
        if close <= 0:
            return out
        # Build parameters, not weights: a 0 there is no switch, so they keep
        # the same fallback as every other builder of this HTF context.
        # The EMA spans are the strategy's HTF spans, as in
        # _default_htf_context_for_score (2026-09-25): support_resistance has
        # no ema_*_span field, so the read here was always 50/200 whatever
        # htf_ema_*_span said (the peers declare 34/200). The request now
        # differs from _default_htf_context_for_score's only by the FVG
        # arguments _htf_context adds. FVG detection never reads the EMAs,
        # so no FVG or FVG score changes.
        ema_fast_span, ema_slow_span = htf_ema_spans(self.params)
        htf_ctx = self.strategy._htf_context(
            symbol,
            data,
            timeframe_minutes=self.strategy._htf_minutes(),
            lookback_days=self.strategy._htf_lookback_days(),
            pivot_span=int(self._support_resistance_setting("pivot_span", 2) or 2),
            max_levels_per_side=int(self._support_resistance_setting("max_levels_per_side", 6) or 6),
            atr_tolerance_mult=float(self._support_resistance_setting("atr_tolerance_mult", 0.35) or 0.35),
            pct_tolerance=float(self._support_resistance_setting("pct_tolerance", 0.0030) or 0.0030),
            stop_buffer_atr_mult=float(self._support_resistance_setting("stop_buffer_atr_mult", 0.25) or 0.25),
            ema_fast_span=ema_fast_span,
            ema_slow_span=ema_slow_span,
            current_price=close,
            use_prior_day_high_low=bool(self._support_resistance_setting("use_prior_day_high_low", True)),
            use_prior_week_high_low=bool(self._support_resistance_setting("use_prior_week_high_low", True)),
        )
        fvg_ltf_ctx = self.strategy._ltf_fvg_context(symbol, frame, data)
        htf_score = self._score_fvg_context(close, htf_ctx, timeframe_minutes=getattr(htf_ctx, "timeframe_minutes", self.strategy._htf_minutes()))
        fvg_ltf_score = self._score_fvg_context(close, fvg_ltf_ctx, timeframe_minutes=self.strategy._ltf_minutes())
        htf_weight = max(0.0, float(self.params.get("htf_fvg_entry_weight", 0.55)))
        ltf_fvg_weight = max(0.0, float(self.params.get("ltf_fvg_entry_weight", 0.35)))
        opposing_mult = max(0.50, float(self.params.get("opposing_fvg_entry_penalty_mult", 1.00)))
        same_validated_bonus = float(self.params.get("same_direction_fvg_validated_bonus", 0.15))
        same_active_bonus = float(self.params.get("same_direction_fvg_active_bonus", 0.12))
        opposing_validated_penalty = float(self.params.get("opposing_fvg_validated_penalty", 0.15))
        opposing_active_penalty = float(self.params.get("opposing_fvg_active_penalty", 0.12))
        invalidated_opposing_bonus = float(self.params.get("invalidated_opposing_fvg_bonus", 0.10))
        # Same-direction invalidated penalty. Defaults to opposing_active_penalty * 0.85
        # so callers that don't configure it explicitly get exactly the same behavior
        # as before — a ~15% discount relative to an active opposing-direction gap,
        # because a filled continuation gap is a weaker bearish signal than a live one.
        same_invalidated_penalty = float(self.params.get("same_direction_fvg_invalidated_penalty", opposing_active_penalty * 0.85))

        if side == Side.LONG:
            same_htf = float(htf_score.get("bull_score", 0.0) or 0.0)
            opposing_htf = float(htf_score.get("bear_score", 0.0) or 0.0)
            same_ltf = float(fvg_ltf_score.get("bull_score", 0.0) or 0.0)
            opposing_ltf = float(fvg_ltf_score.get("bear_score", 0.0) or 0.0)
            same_htf_info = dict(htf_score.get("nearest_bullish", {}) or {})
            opposing_htf_info = dict(htf_score.get("nearest_bearish", {}) or {})
            same_ltf_info = dict(fvg_ltf_score.get("nearest_bullish", {}) or {})
            opposing_ltf_info = dict(fvg_ltf_score.get("nearest_bearish", {}) or {})
        else:
            same_htf = float(htf_score.get("bear_score", 0.0) or 0.0)
            opposing_htf = float(htf_score.get("bull_score", 0.0) or 0.0)
            same_ltf = float(fvg_ltf_score.get("bear_score", 0.0) or 0.0)
            opposing_ltf = float(fvg_ltf_score.get("bull_score", 0.0) or 0.0)
            same_htf_info = dict(htf_score.get("nearest_bearish", {}) or {})
            opposing_htf_info = dict(htf_score.get("nearest_bullish", {}) or {})
            same_ltf_info = dict(fvg_ltf_score.get("nearest_bearish", {}) or {})
            opposing_ltf_info = dict(fvg_ltf_score.get("nearest_bullish", {}) or {})

        raw_same = (same_htf * htf_weight) + (same_ltf * ltf_fvg_weight)
        raw_opposing = ((opposing_htf * htf_weight) + (opposing_ltf * ltf_fvg_weight)) * opposing_mult
        state_bonus = 0.0
        continuation_bias = 0.0
        reversal_bias = 0.0

        def _apply_state(info: dict[str, Any], *, same_direction: bool, weight: float) -> None:
            nonlocal state_bonus, continuation_bias, reversal_bias
            state = str(info.get("state", "none") or "none").strip().lower()
            if state == "none":
                return
            if same_direction:
                if state == "validated":
                    state_bonus += same_validated_bonus * weight
                    continuation_bias += 0.35 * weight
                elif state == "active":
                    state_bonus += same_active_bonus * weight
                    continuation_bias += 0.24 * weight
                elif state == "invalidated":
                    # Same-direction gap has been filled — weaker continuation signal.
                    # Uses its own parameter now, but the default preserves the
                    # historical opposing_active_penalty * 0.85 behavior.
                    state_bonus -= same_invalidated_penalty * weight
            else:
                if state == "validated":
                    state_bonus -= opposing_validated_penalty * weight
                elif state == "active":
                    state_bonus -= opposing_active_penalty * weight
                elif state == "invalidated":
                    state_bonus += invalidated_opposing_bonus * weight
                    continuation_bias += 0.08 * weight
                    reversal_bias += 0.18 * weight

        _apply_state(same_htf_info, same_direction=True, weight=htf_weight)
        _apply_state(same_ltf_info, same_direction=True, weight=ltf_fvg_weight)
        _apply_state(opposing_htf_info, same_direction=False, weight=htf_weight)
        _apply_state(opposing_ltf_info, same_direction=False, weight=ltf_fvg_weight)

        entry_adjustment = round(raw_same - raw_opposing + state_bonus, 4)
        continuation_bias = round(max(0.0, raw_same + continuation_bias + max(0.0, state_bonus)), 4)
        reversal_bias = round(max(0.0, reversal_bias), 4)
        out.update(
            {
                "fvg_context_enabled": True,
                "fvg_entry_adjustment": entry_adjustment,
                "fvg_same_direction_score": round(raw_same, 4),
                "fvg_opposing_score": round(raw_opposing, 4),
                "fvg_state_bonus": round(state_bonus, 4),
                "fvg_continuation_bias": continuation_bias,
                "fvg_reversal_bias": reversal_bias,
                "htf_fvg_bull_score": round(float(htf_score.get("bull_score", 0.0) or 0.0), 4),
                "htf_fvg_bear_score": round(float(htf_score.get("bear_score", 0.0) or 0.0), 4),
                "fvg_ltf_bull_score": round(float(fvg_ltf_score.get("bull_score", 0.0) or 0.0), 4),
                "fvg_ltf_bear_score": round(float(fvg_ltf_score.get("bear_score", 0.0) or 0.0), 4),
                "htf_fvg_same_state": str(same_htf_info.get("state", "none") or "none"),
                "htf_fvg_opposing_state": str(opposing_htf_info.get("state", "none") or "none"),
                "fvg_ltf_same_state": str(same_ltf_info.get("state", "none") or "none"),
                "fvg_ltf_opposing_state": str(opposing_ltf_info.get("state", "none") or "none"),
                "htf_fvg_same_midpoint": _optional_float(same_htf_info.get("midpoint")),
                "htf_fvg_opposing_midpoint": _optional_float(opposing_htf_info.get("midpoint")),
                "fvg_ltf_same_midpoint": _optional_float(same_ltf_info.get("midpoint")),
                "fvg_ltf_opposing_midpoint": _optional_float(opposing_ltf_info.get("midpoint")),
                "htf_fvg_same_distance_pct": _optional_float(same_htf_info.get("distance_pct")),
                "htf_fvg_opposing_distance_pct": _optional_float(opposing_htf_info.get("distance_pct")),
                "fvg_ltf_same_distance_pct": _optional_float(same_ltf_info.get("distance_pct")),
                "fvg_ltf_opposing_distance_pct": _optional_float(opposing_ltf_info.get("distance_pct")),
            }
        )
        return out

    def _build_signal_metadata(
        self,
        *,
        entry_price: float | None,
        chart_ctx: Any,
        ms_ctx: Any,
        sr_ctx: Any,
        tech_ctx: Any,
        adjustments: Mapping[str, Any],
        fvg_adjustments: Mapping[str, Any],
        management: Mapping[str, Any],
        retest_plans: tuple[dict[str, Any], ...],
        ladder_meta: Mapping[str, Any] | None,
        final_priority_score: float,
        leading: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """The signal.metadata every emitted entry carries.

        Merge order (later keys overwrite earlier keys on collision):

        1. ``entry_price``                        (price-level proposals)
        2. ``leading``                            (the strategy's own keys)
        3. ``final_priority_score``
        4. ``adjustments``, ``fvg_adjustments``, ``management``
        5. each retest plan's ``metadata``
        6. ``ladder_meta``
        7. the chart / structure (``msltf_*``) / S/R / technical lists of the
           contexts the proposal was gated on

        ``entry_price`` is the intended-entry price ``risk.py::_signal_entry_
        price`` reads; without it the same-level retry block and the
        fib-pullback override short-circuit to "ok". A premium proposal's
        strategy stamps its own entry / limit / mark keys in ``leading``.

        The structure prefix is always ``msltf``: until 2026-09-24
        htf_pivots and trend_continuation stamped ``ms_ltf_*`` and so
        carried different structure keys from every other strategy.
        """
        out: dict[str, Any] = {}
        if entry_price is not None:
            out["entry_price"] = float(entry_price)
        if leading:
            out.update(leading)
        out["final_priority_score"] = round(float(final_priority_score), 4)
        out.update(adjustments)
        out.update(fvg_adjustments)
        out.update(management)
        for plan in retest_plans:
            out.update(plan.get("metadata") or {})
        if ladder_meta:
            out.update(ladder_meta)
        out.update(self.strategy._chart_lists(chart_ctx))
        out.update(self.strategy._structure_lists(ms_ctx, prefix="msltf"))
        out.update(self.strategy._sr_lists(sr_ctx))
        out.update(self.strategy._technical_lists(tech_ctx, prefix="tech"))
        return out
