# SPDX-License-Identifier: MIT
"""Divergence between price pivots and a momentum indicator (RSI, OBV): the
detector, the match it reports, and the inputs the technical-levels (LTF)
and HTF builders give it.

A divergence is a misalignment between a price pivot pair and the
corresponding indicator pivot values:

  regular bullish: at pivot lows, price prints LL (price2 < price1 by
      price_move_frac) but indicator prints HL (ind2 > ind1 + delta).
      Reversal-likely from a downtrend.

  regular bearish: at pivot highs, price prints HH but indicator prints
      LH. Reversal-likely from an uptrend.

  hidden bullish: at pivot lows, price prints HL but indicator prints
      LL. Continuation in an uptrend.

  hidden bearish: at pivot highs, price prints LH but indicator prints
      HH. Continuation in a downtrend.

``b`` is always the most recent pivot (and must be within ``max_age_bars``,
counted on the caller's bar clock -- session bars while session indicators
are on and the clock is inside the session); ``a`` is the nearest earlier
pivot, within ``pivot_lookback``, that satisfies the price half of the
pattern, and a pivot in between that contradicts it means there is no
divergence. See ``find_divergence`` for why it is the nearest and not merely
any.
"""
from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd

from .indicators import (
    get_runtime_indicator_mode,
    indicator_session_mask,
    indicator_session_open,
    session_price_scale,
)


_DivergencePoint = tuple[int, "pd.Timestamp", float]


class DivergenceMatch:
    """A confirmed divergence between two pivot points and a momentum
    indicator series.

    Stored as a small immutable record so consumers (engine score
    adjustments, dashboard chart overlay, shared entry candidate builder)
    can read pivot positions, indicator values, and freshness without
    re-deriving them. ``__slots__`` keeps memory footprint tight when many
    contexts hold these — typically 8 fields per TechnicalLevelsContext.

    ``age_bars`` is how many bars have closed since ``b``, counted on the
    ``bar_clock`` ``find_divergence`` was given. The builders pass the
    session-bar clock while session indicators are on and the clock is
    inside the session (``indicators.indicator_session_open``), so it is SESSION
    bars there: the overnight between yesterday's last pivot and today's
    open is not part of the age. Without a clock (session indicators off, or
    a reader outside the session) it is every bar. The pivot positions are
    frame positions either way.
    """

    __slots__ = (
        "kind",
        "direction",
        "indicator",
        "pivot_a_pos",
        "pivot_a_ts",
        "pivot_a_price",
        "pivot_a_indicator",
        "pivot_b_pos",
        "pivot_b_ts",
        "pivot_b_price",
        "pivot_b_indicator",
        "indicator_delta",
        "age_bars",
    )

    kind: str
    direction: str
    indicator: str
    pivot_a_pos: int
    pivot_a_ts: pd.Timestamp
    pivot_a_price: float
    pivot_a_indicator: float
    pivot_b_pos: int
    pivot_b_ts: pd.Timestamp
    pivot_b_price: float
    pivot_b_indicator: float
    indicator_delta: float
    age_bars: int

    def __init__(
        self,
        *,
        kind: str,
        direction: str,
        indicator: str,
        pivot_a_pos: int,
        pivot_a_ts: pd.Timestamp,
        pivot_a_price: float,
        pivot_a_indicator: float,
        pivot_b_pos: int,
        pivot_b_ts: pd.Timestamp,
        pivot_b_price: float,
        pivot_b_indicator: float,
        indicator_delta: float,
        age_bars: int,
    ) -> None:
        self.kind = str(kind)
        self.direction = str(direction)
        self.indicator = str(indicator)
        self.pivot_a_pos = int(pivot_a_pos)
        self.pivot_a_ts = pivot_a_ts
        self.pivot_a_price = float(pivot_a_price)
        self.pivot_a_indicator = float(pivot_a_indicator)
        self.pivot_b_pos = int(pivot_b_pos)
        self.pivot_b_ts = pivot_b_ts
        self.pivot_b_price = float(pivot_b_price)
        self.pivot_b_indicator = float(pivot_b_indicator)
        self.indicator_delta = float(indicator_delta)
        self.age_bars = int(age_bars)

    def __bool__(self) -> bool:
        # All DivergenceMatch instances are truthy. Lets existing callers
        # that test ``bool(getattr(ctx, "bullish_rsi_divergence", False))``
        # keep working — None is falsy, a match is truthy.
        return True

    def __repr__(self) -> str:
        return (
            f"DivergenceMatch({self.kind} {self.direction} {self.indicator} "
            f"a@{self.pivot_a_ts}={self.pivot_a_price:.4f}/{self.pivot_a_indicator:.4f} "
            f"b@{self.pivot_b_ts}={self.pivot_b_price:.4f}/{self.pivot_b_indicator:.4f} "
            f"age={self.age_bars}b delta={self.indicator_delta:.4f})"
        )

    def to_payload(self) -> dict[str, object]:
        """Render to a JSON-friendly dict for chart / metadata export."""
        return {
            "kind": self.kind,
            "direction": self.direction,
            "indicator": self.indicator,
            "pivot_a": {
                "pos": int(self.pivot_a_pos),
                "ts": str(self.pivot_a_ts),
                "price": float(self.pivot_a_price),
                "indicator": float(self.pivot_a_indicator),
            },
            "pivot_b": {
                "pos": int(self.pivot_b_pos),
                "ts": str(self.pivot_b_ts),
                "price": float(self.pivot_b_price),
                "indicator": float(self.pivot_b_indicator),
            },
            "indicator_delta": float(self.indicator_delta),
            "age_bars": int(self.age_bars),
        }


def _pivot_indicator_value(indicator: pd.Series, pos: int) -> float | None:
    if pos < 0 or pos >= len(indicator):
        return None
    value = indicator.iloc[pos]
    if pd.isna(value):
        return None
    return float(value)


def _b_above_a(kind: str, direction: str) -> bool:
    """Does the pattern need the latest pivot ``b`` ABOVE the earlier ``a``?
    Regular bearish (HH) and hidden bullish (HL) do; regular bullish (LL) and
    hidden bearish (LH) need it below."""
    return (kind == "regular") == (direction == "bearish")


def _price_condition(*, kind: str, direction: str, price_a: float, price_b: float,
                     price_move_frac: float) -> bool:
    """The price half of ``_qualifies`` -- does ``b`` make the swing the
    pattern needs against ``a``, by at least ``price_move_frac``?"""
    if _b_above_a(kind, direction):
        return price_b > price_a * (1.0 + price_move_frac)
    return price_b < price_a * (1.0 - price_move_frac)


def _contradicts(*, kind: str, direction: str, price_a: float, price_b: float) -> bool:
    """Is ``a`` on the wrong side of ``b`` for this pattern -- so that, against
    the swing ``a`` marks, ``b`` made the OPPOSITE move to the one claimed?

    Regular bullish: a lower low than ``b`` (``b`` is not the extreme).
    Hidden bullish: a higher low than ``b`` (``b`` broke it -- not a higher
    low). The bearish two mirror them. A pivot on the right side of ``b`` but
    inside ``price_move_frac`` is neither: it is noise, and skipped."""
    if _b_above_a(kind, direction):
        return price_a > price_b
    return price_a < price_b


def _qualifies(
    *,
    kind: str,
    direction: str,
    price_a: float,
    price_b: float,
    ind_a: float,
    ind_b: float,
    price_move_frac: float,
    indicator_delta: float,
) -> tuple[bool, float]:
    """Return ``(matched, |ind_b - ind_a|)`` for the requested pattern.

    Pattern matrix (all four cases reduce to the same shape):

      regular bullish (lows):  price_b < price_a*(1-frac), ind_b > ind_a + delta
      regular bearish (highs): price_b > price_a*(1+frac), ind_b < ind_a - delta
      hidden bullish  (lows):  price_b > price_a*(1+frac), ind_b < ind_a - delta
      hidden bearish  (highs): price_b < price_a*(1-frac), ind_b > ind_a + delta
    """
    if kind == "regular" and direction == "bullish":
        price_ok = price_b < price_a * (1.0 - price_move_frac)
        ind_ok = ind_b > ind_a + indicator_delta
    elif kind == "regular" and direction == "bearish":
        price_ok = price_b > price_a * (1.0 + price_move_frac)
        ind_ok = ind_b < ind_a - indicator_delta
    elif kind == "hidden" and direction == "bullish":
        price_ok = price_b > price_a * (1.0 + price_move_frac)
        ind_ok = ind_b < ind_a - indicator_delta
    elif kind == "hidden" and direction == "bearish":
        price_ok = price_b < price_a * (1.0 - price_move_frac)
        ind_ok = ind_b > ind_a + indicator_delta
    else:
        return False, 0.0
    return bool(price_ok and ind_ok), abs(ind_b - ind_a)


def find_divergence(
    points: list[_DivergencePoint],
    indicator: pd.Series,
    *,
    kind: str,
    direction: str,
    indicator_name: str,
    price_move_frac: float,
    indicator_delta: float,
    pivot_lookback: int,
    max_age_bars: int,
    last_bar_pos: int,
    price_scale: np.ndarray | None = None,
    bar_clock: np.ndarray | None,
) -> DivergenceMatch | None:
    """Divergence between the latest swing and the swing it is measured
    against, or None.

    ``bar_clock`` (one value per bar of the frame the pivot positions index,
    non-decreasing) is what ``b``'s age is counted on: ``clock[last_bar_pos]
    - clock[pos_b]``. None counts every bar (``last_bar_pos - pos_b``). It
    has no default, so no caller falls back to the all-bar age by leaving it
    out. The builders pass the one ``divergence_inputs`` gives them:
    ``np.cumsum`` of the indicator session mask while session indicators are
    on and the clock is inside the session
    (``indicators.indicator_session_open``), so the age is session bars. Until
    2026-09-24 it was always every bar: with pivots paired only on session
    bars, the post- and pre-market bars aged yesterday's last session pivot
    past the limit overnight. On the divergence-age study's symbol-days with
    a dense overnight tape (447 over 27 sessions on 60m, 554 over 29 on 15m)
    the 60m HTF divergence read on none of the minutes from 09:30 to 11:00;
    on session-bar age it reads on 23.6% of RTH minutes instead of 10.4%
    (15m: 16.4% instead of 14.6%).

    ``price_scale`` (``indicators.session_price_scale`` of the frame the pivot
    positions index, from ``divergence_inputs``) puts the pivot prices on the scale the indicator was
    computed on before they are compared; the match still reports the raw
    prices. The session rsi14 / obv are stitched across the overnight gap,
    so compared raw a gap alone made a "higher high" the RSI never saw: 371
    of 571 cross-session HTF RSI divergences over 10 archived sessions were
    the gap (2026-09-23).

    ``points`` is a list of ``(pos, ts, price)`` tuples -- pivot lows for
    bullish patterns, pivot highs for bearish ones.

    ``b`` is the MOST RECENT pivot, and it must be within ``max_age_bars``.
    A newer pivot that does not diverge supersedes an older one that did:
    reporting the older one would describe a state the tape has moved past.

    ``a`` is the NEAREST earlier pivot (within ``pivot_lookback``) that
    satisfies the price half of the pattern, and only that pair has its
    indicator tested. Pivots nearer than it that miss the minimum price move
    are skipped as noise. A pivot on the WRONG side of ``b`` ends the scan
    with no divergence (``_contradicts``): for regular divergence ``b`` is then
    not the extreme of the swing, and for hidden divergence ``b`` broke the
    swing it would be a higher low (or lower high) against. The regular half
    of that rule landed first; hidden divergence kept skipping such pivots,
    so lows 95 -> 103 -> 101 were reported as a hidden bullish divergence
    95 -> 101 across the 103 swing that 101 broke.

    Until 2026-09-22 this paired ``b`` with the OLDEST qualifying pivot in
    the window. Lows at 100 (RSI 20), 96 (RSI 35), 95 (RSI 30) were reported
    as a bullish divergence 100 -> 95, stepping over the 96 swing at which
    RSI had in fact CONFIRMED the new low (30 < 35). Divergence is a claim
    about the swing price just made against the one before it; an older
    pivot is only a valid reference when nothing in between contradicts it.

    Returns None when no pair qualifies or the indicator is missing at
    either pivot.
    """
    # The clamps come before the early return, so an unreadable knob raises
    # on every call, as it did in the builders until 2026-09-27, not only
    # once a side has two pivots.
    pivot_lookback = max(2, int(pivot_lookback))
    max_age_bars = max(0, int(max_age_bars))
    if not points or len(points) < 2:
        return None
    candidates = points[-pivot_lookback:]
    if len(candidates) < 2:
        return None
    def _on_scale(pos: int, price: float) -> float:
        return float(price) * (float(price_scale[int(pos)]) if price_scale is not None else 1.0)

    pos_b, ts_b, price_b = candidates[-1]
    if bar_clock is None:
        age = max(0, int(last_bar_pos) - int(pos_b))
    else:
        age = max(0, int(bar_clock[int(last_bar_pos)]) - int(bar_clock[int(pos_b)]))
    if age > max_age_bars:
        return None
    ind_b = _pivot_indicator_value(indicator, pos_b)
    if ind_b is None:
        return None
    cmp_b = _on_scale(pos_b, price_b)
    for pos_a, ts_a, price_a in reversed(candidates[:-1]):
        cmp_a = _on_scale(pos_a, price_a)
        if _contradicts(kind=kind, direction=direction, price_a=cmp_a, price_b=cmp_b):
            return None
        if not _price_condition(kind=kind, direction=direction, price_a=cmp_a,
                                price_b=cmp_b, price_move_frac=float(price_move_frac)):
            continue
        ind_a = _pivot_indicator_value(indicator, pos_a)
        if ind_a is None:
            return None
        matched, delta = _qualifies(
            kind=kind,
            direction=direction,
            price_a=cmp_a,
            price_b=cmp_b,
            ind_a=ind_a,
            ind_b=ind_b,
            price_move_frac=float(price_move_frac),
            indicator_delta=float(indicator_delta),
        )
        if not matched:
            return None
        return DivergenceMatch(
            kind=kind,
            direction=direction,
            indicator=indicator_name,
            pivot_a_pos=int(pos_a),
            pivot_a_ts=ts_a,
            pivot_a_price=float(price_a),
            pivot_a_indicator=float(ind_a),
            pivot_b_pos=int(pos_b),
            pivot_b_ts=ts_b,
            pivot_b_price=float(price_b),
            pivot_b_indicator=float(ind_b),
            indicator_delta=float(delta),
            age_bars=int(age),
        )
    return None


def divergence_inputs(
    frame: pd.DataFrame,
    highs: list[_DivergencePoint],
    lows: list[_DivergencePoint],
    *,
    in_session: npt.NDArray[np.bool_] | None = None,
) -> tuple[list[_DivergencePoint], list[_DivergencePoint], npt.NDArray[np.int64] | None, npt.NDArray[np.float64]]:
    """``(highs, lows, bar_clock, price_scale)``: the pivots, the bar clock
    and the price scale ``find_divergence`` reads ``frame``'s divergences
    with. ``highs`` and ``lows`` are ``frame``'s pivots with their positions
    (``levels_shared.pivot_points(..., include_idx=True)``); ``in_session``
    is ``indicators.indicator_session_mask(frame.index)`` when the caller
    already holds it, and None computes it where it is needed.

    With session indicators on, rsi14 / obv hold the session-only series on
    session bars and the all-hours one on the others
    (``indicators.add_indicators``). A pivot pair straddling the two would
    compare different indicators, so only session-bar pivots are kept
    (2026-09-23: until then a 15m pair straddling the old 13:00 switch
    compared two different RSIs, KLZ-M2).

    Their age is counted in session bars too, while the clock is inside the
    session (``indicators.indicator_session_open``, 2026-09-24): the bar
    clock is the running count of session bars. Counted in every bar, the post- and
    pre-market bars between yesterday's last session pivot and today's open
    aged it out before the open. At 09:33 a 1m pivot at 15:57 is 5 session
    bars old, but 83 bars old on a name printing a 1m bar every 5 minutes
    outside RTH; on a tape printing every bucket the last 60m session bucket
    (15:30) is 7 bars old by 09:30 (16:00-19:00, 07:00-09:00), and on the
    divergence-age study's symbol-days with a dense overnight tape the 60m
    divergence read on none of the minutes from 09:30 to 11:00. The gate is
    the clock, not the frame's last bar, as for ``indicators.latest_atr14``:
    at 09:30 the last completed 1m bar is still a premarket one, and until
    the first session bucket closes (09:45 on 15m, 10:30 on 60m) an HTF frame
    still ends on a premarket bucket, so a last-bar gate kept the all-bar age
    through exactly the minutes this is for. A reader outside the session
    (premarket) gets no clock and keeps the all-bar age its own bars run on.

    The scale (``indicators.session_price_scale``) puts the pivot prices on
    the gap-free scale those series were computed on.

    With session indicators off the pivots come back as given, with no
    clock and a scale of 1.0.
    """
    bar_clock: npt.NDArray[np.int64] | None = None
    if get_runtime_indicator_mode():
        if in_session is None:
            in_session = indicator_session_mask(frame.index)
        highs = [p for p in highs if in_session[int(p[0])]]
        lows = [p for p in lows if in_session[int(p[0])]]
        if indicator_session_open():
            bar_clock = np.cumsum(in_session)
    return highs, lows, bar_clock, session_price_scale(frame, in_session=in_session)
