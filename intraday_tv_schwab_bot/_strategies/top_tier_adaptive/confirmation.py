# SPDX-License-Identifier: MIT
"""Confirmation and context reads for the regime engine.

What the entry loop and the regimes read besides the bars themselves: the
LTF setup reads (``today_bars``, ``session_bars``, ``setup_window``,
``session_window``, ``closed_ltf``, ``current_bucket_extreme``,
``vwap_at_bucket_close``), the sector confirmation (index ETFs and peer
breadth, the leg-anchored VWAP), the per-symbol daily statistics and
volatility scale, the regime score normalisation
(``REGIME_SCORE_CEILINGS``), the side asymmetry multipliers, the side vote
and confirmation bar, and the live directional bias, stop widening and
soft-bias penalty.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any

import pandas as pd

from ...bars import last_bucket_forming, session_open_price
from ...indicators import bar_posture, indicator_session_start
from ...models import Side
from ...sessions import parse_hhmm
from ...numeric import safe_float
from ... import sessions
from ...daily_stats import SymbolDailyStats, build_symbol_stats, volatility_scale

LOG = logging.getLogger(__name__)

# The index confirmation's freshness bound (2026-10-13): a peer or
# sector ETF frame whose newest 1m bar closed more than this many minutes ago
# does not vote. The frames are the step frames: a stream bar lands seconds
# after its minute closes, and over 8 archived days the longest RTH gap was 2
# minutes, so 3 tolerates a gap and the minute turn while an outage or a
# halted peer drops out within minutes instead of voting its old posture.
INDEX_FRAME_MAX_AGE_MINUTES = 3.0

# Per-regime score ceilings — the maximum each _score_* method can return.
# Used to normalise scores onto a common 0..1 scale before the build-order
# auction and the cross-signal slot auction compare them. Without this a
# trend at 4.5/6.0 (25% of its headroom) outranks an sr_scalp at 4.4/4.5
# (93% of its headroom) purely because trend's scorer has more components.
# Keep in sync with the _score_* methods (``regimes/``);
# ``test_regime_score_ceilings`` asserts each scorer cannot exceed its entry
# here.
REGIME_SCORE_CEILINGS = {
    "trend": 6.0,
    "pullback": 5.0,
    "range": 5.0,
    "vol_squeeze": 6.5,
    "momentum": 6.0,
    "sr_scalp": 5.0,
    "orb": 5.0,
    "vwap_reclaim": 5.0,
}


def last_closed_bars(symbol: str, bars: pd.DataFrame, bar_minutes: int, frame: pd.DataFrame) -> pd.DataFrame:
    """``bars``, a ``bar_minutes`` frame resampled from *symbol*'s step frame
    ``frame``, without a last bucket that has not closed yet.

    The step frame holds closed 1m bars only: the minute history without the
    minute still trading (``MarketDataStore._completed_bars``) and the
    stream's bars, each sent once its minute closed. Its data therefore ends
    when its last bar closed, and a coarser bucket that ends after that is
    still forming: its close is not the bar's close yet. The data end is the
    earlier of the step frame's end and the clock. The step frame's end,
    because for a moment after a bucket ends the clock has passed it while
    its last 1m bar may not be in. The clock, so that a forming bar never
    counts: a last 1m bar whose minute is still trading (a partial minute
    from the stream, or a history path that kept it) is left out with its
    bucket, at 1m too, and the drop is logged. A 1m frame of closed bars is
    returned whole."""
    if bars is None or bars.empty or frame is None or frame.empty:
        return bars
    data_end = pd.Timestamp(frame.index[-1]) + pd.Timedelta(minutes=1)
    if last_bucket_forming(bars.index, bar_minutes, data_end):
        return bars.iloc[:-1]
    now = sessions.now_et()
    if last_bucket_forming(bars.index, bar_minutes, now):
        LOG.warning("%s: the step frame's last 1m bar %s has not closed at %s; its %dm bar %s is left out as forming",
                    symbol, frame.index[-1], now.isoformat(timespec="seconds"), bar_minutes, bars.index[-1])
        return bars.iloc[:-1]
    return bars


# ---------------------------------------------------------------------------
# The LTF setup reads (2026-10-13)
#
# Every regime sets up on the strategy's LTF (``ltf_minutes``, 5 in the
# shipped preset), resampled from the step frame's closed 1m bars, and times
# its entry on the step frame itself. The step frame an entry pass reads has
# closed bars only: ``_read_candidate`` drops a last 1m bar whose minute is
# still trading by the clock (``last_closed_bars``), so the trigger bar, the
# bar before it and the close every regime reads are closed bars'. The LTF's
# last row is the CURRENT bucket, the one the step frame's last 1m bar (the
# trigger bar) is in. It is still forming until the step frame holds its
# last minute (``last_closed_bars``); ``_CandidateRead.ltf_forming`` is the
# one flag that says so, read once a pass. A setup reads closed buckets only:
#   * values (ATR14, ADX14, EMA9 / EMA20, ret5 / ret15, the pullback's close
#     position and volume, the vote's last three bars, the pullback leg):
#     the closed buckets (``closed_ltf``), so the last value is the last
#     closed bucket's;
#   * windows (the trend and momentum reference extreme, the pullback touch,
#     the range zone, the squeeze box): today's buckets before the current
#     one (``setup_window``), all closed; the reclaim's dip: the session's
#     buckets before the current one (``session_window``, below). The
#     current bucket is no part of its own setup: a breakout cannot clear a
#     high its own bucket set, at the minute a bucket closes as much as
#     within it.
# Windows, targets and the pullback leg count today's bars from midnight
# (``today_bars``), premarket included, as they did on 1m bars (user
# decision, 2026-10-13); the 09:45 warm-up counts the session's own buckets
# (``session_bars``), and so do the volume baselines (pullback's,
# vol_squeeze's), the session's volume like the session VWAP. The LTF FVG
# and order-block contexts read the LTF as resampled, its forming bucket
# included (an accepted side effect).
# Price stays the step frame's: the close is its last 1m close and VWAP its
# session VWAP, on the LTF too (``vwap_at_bucket_close``). That VWAP resets
# at the session's start (09:30 on the "rth" window), and a premarket bucket
# carries the all-hours VWAP instead, so a read of buckets AGAINST VWAP --
# the reclaim's dip, range's flips -- counts the session's buckets alone
# (``session_window``, ``session_bars``): the dip and the reclaim read one
# session VWAP, and a premarket bucket is no dip and no flip (user decision,
# 2026-10-13). The pullback's touch reads a window's lows / highs against
# the session VWAP's current value, one VWAP, and keeps today's window.
# A stop anchored on a window also clears the current bucket's 1m bars
# (``current_bucket_extreme``; kept by the user, 2026-10-13).
# ---------------------------------------------------------------------------

def _day_slice(frame: pd.DataFrame, start: time) -> pd.DataFrame:
    """``frame``'s bars from ``start`` today to the day's end, today being
    ``sessions.now_et()``'s date, read on the index's own clock (tz-aware or
    ET wall time alike). Taken by position on the time-ordered index, as
    ``_leg_anchor_vwap`` scans."""
    index = frame.index
    today = sessions.now_et().date()
    first = pd.Timestamp(datetime.combine(today, start), tz=index.tz)
    end = pd.Timestamp(datetime.combine(today + timedelta(days=1), time()), tz=index.tz)
    return frame.iloc[index.searchsorted(first):index.searchsorted(end)]


def today_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """``frame``'s bars of today's calendar day, premarket included: the
    bars ``same_day_mask`` selects on a time-ordered frame. Every setup
    window, the pullback's leg and swing target, the reclaim's HOD / LOD
    target and range's previous 1m bar count these, as they did on 1m bars
    until 2026-10-13 (user decision, 2026-10-13: the setups keep the
    premarket bars they always read). The step frame also holds the prior
    session; nothing is measured on it."""
    if frame is None or frame.empty:
        return frame
    return _day_slice(frame, time())


def session_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """``frame``'s bars of today's indicator session: from where the session
    VWAP / EMA reset starts (``indicator_session_start``: the 09:30 open under
    the "rth" window, the 07:00 stream open under "extended") to the day's
    end. The opening warm-up counts its closed buckets
    (``LTF_WARMUP_CLOSED_BUCKETS``: no setup before 09:45 on the "rth"
    window), and so do a read of buckets against the session VWAP
    (``session_window``, range's flips) and the volume baselines (the
    pullback's buckets, vol_squeeze's box minutes: the session's volume,
    like the session VWAP, 2026-10-13); other windows count
    ``today_bars``."""
    if frame is None or frame.empty:
        return frame
    return _day_slice(frame, indicator_session_start())


def setup_window(ltf: pd.DataFrame, bars: int) -> pd.DataFrame:
    """The last ``bars`` of today's buckets before the current one (``ltf``'s
    last row; ``today_bars``, premarket included): the window a regime
    measures its setup on. All of them have closed. Fewer when the day holds
    fewer; empty when it holds none before the current bucket."""
    day = today_bars(ltf)
    if day is None or day.empty:
        return ltf.iloc[0:0]
    return day.iloc[:-1].tail(max(0, int(bars)))


def session_window(ltf: pd.DataFrame, bars: int) -> pd.DataFrame:
    """The last ``bars`` of today's SESSION buckets before the current one
    (``ltf``'s last row; ``session_bars``: from the indicator session's
    start, 09:30 on the "rth" window): the window a setup read against VWAP
    measures on. Each of its buckets' ``vwap`` (``vwap_at_bucket_close``)
    is the session VWAP the latest close is read against; a premarket
    bucket's is the all-hours VWAP, which the session VWAP replaces at the
    open, so it has no place here (user decision, 2026-10-13: the reclaim's
    dip and its reclaim read the same session VWAP). All of them have
    closed. Fewer when the session holds fewer before the current one (it
    holds six 5m buckets from 10:00 on the "rth" window); empty when it
    holds none."""
    return session_bars(ltf.iloc[:-1]).tail(max(0, int(bars)))


def closed_ltf(ltf: pd.DataFrame, forming: bool) -> pd.DataFrame:
    """``ltf`` without its last bucket when ``forming`` (the pass's
    ``ltf_forming`` flag) says it is still trading: the buckets a setup's
    values are read from, the last closed one last."""
    return ltf.iloc[:-1] if forming else ltf


def current_bucket_extreme(frame: pd.DataFrame, ltf: pd.DataFrame, side: Side) -> float | None:
    """The lowest low (``side`` LONG) or highest high (SHORT) of the step
    frame's 1m bars in the current LTF bucket, the trigger bar (the frame's
    last) included; None when none reads. They traded after the setup
    window closed and before the entry, so a stop anchored on the window
    clears them too: a stop inside them sits where price has just been. On a
    1m LTF the current bucket is the trigger bar alone."""
    if frame is None or frame.empty or ltf is None or ltf.empty:
        return None
    bucket = frame.iloc[frame.index.searchsorted(ltf.index[-1]):]
    if bucket.empty:
        return None
    return safe_float(bucket["low"].min() if side == Side.LONG else bucket["high"].max())


def stop_anchor(side: Side, window: pd.DataFrame, frame: pd.DataFrame, ltf: pd.DataFrame, fallback: float) -> float:
    """The level a builder puts its stop beyond: the setup window's lowest
    low (``side`` LONG) or highest high (SHORT), ``fallback`` when that does
    not read, and past that every 1m bar of the current bucket
    (``current_bucket_extreme``)."""
    if side == Side.LONG:
        level = safe_float(window["low"].min(), fallback)
        extreme = current_bucket_extreme(frame, ltf, side)
        return level if extreme is None else min(level, extreme)
    level = safe_float(window["high"].max(), fallback)
    extreme = current_bucket_extreme(frame, ltf, side)
    return level if extreme is None else max(level, extreme)


def vwap_at_bucket_close(ltf: pd.DataFrame, frame: pd.DataFrame) -> pd.Series:
    """The step frame's session VWAP at each LTF bucket's last 1m bar, on
    ``ltf``'s index (``ltf`` is resampled from ``frame``): the one VWAP
    reference. A resample rebuilds VWAP from its own typical prices, which
    drifts from the 1m session VWAP that the peers, the reclaim and the vote
    read; the LTF's ``vwap`` is replaced by this. The current bucket's is
    the frame's last VWAP. A bucket's last 1m bar is the frame's last bar
    before the next bucket's label. A premarket bucket's is the step
    frame's all-hours VWAP (the session VWAP starts at the open), so a read
    of buckets against VWAP keeps to the session's (``session_window``)."""
    if ltf.empty:
        return pd.Series([], index=ltf.index, dtype=float)
    positions = [*(frame.index.searchsorted(ltf.index[1:], side="left") - 1), len(frame) - 1]
    return pd.Series(frame["vwap"].to_numpy(dtype=float)[positions], index=ltf.index, dtype=float)


def frame_age_minutes(frame: pd.DataFrame, now: datetime) -> float:
    """Minutes from the close of ``frame``'s newest 1m bar (its label plus a
    minute) to ``now``; negative when that bar has not closed yet. A
    tz-naive index is ET wall time."""
    last = pd.Timestamp(frame.index[-1])
    moment = pd.Timestamp(now)
    if last.tzinfo is None and moment.tzinfo is not None:
        moment = moment.tz_convert(sessions.EXCHANGE_TZ).tz_localize(None)
    return (moment - (last + pd.Timedelta(minutes=1))).total_seconds() / 60.0


class ConfirmationMixin:
    """Confirmation, bias and scaling reads the entry loop and the regimes
    share. Mixed into ``TopTierAdaptiveStrategy`` (``strategy.py``), whose
    ``BaseStrategy`` supplies ``params``, ``config`` and the contexts."""

    # The entry pass's memo of the frame reads that do not depend on the
    # candidate (``_pass_memo_read``): a dict while ``entry_signals`` runs,
    # ``None`` outside it, so a read made outside a pass is never served
    # from one.
    _entry_pass_memo: dict[tuple[Any, ...], tuple[pd.DataFrame | None, Any]] | None = None

    def _pass_memo_read(self, kind: str, frame: pd.DataFrame | None, inputs: tuple[Any, ...],
                        read: Callable[[], Any]) -> Any:
        """``read()``, kept for the rest of the entry pass under *kind*, the
        frame's context-cache key (``_technical_context_cache_key``: id,
        length, last bar) and the other *inputs* ``read`` uses. Within one
        pass the bars are fixed, so every candidate whose sector holds a
        frame reads it once instead of once per candidate and side.

        Each entry holds its frame, so no other frame can take its id while
        the entry exists, and the memo is dropped as the pass ends. Outside
        a pass ``read()`` runs every time."""
        memo = self._entry_pass_memo
        if memo is None:
            return read()
        key = (kind, *self._technical_context_cache_key(frame), *inputs)
        hit = memo.get(key)
        if hit is not None and hit[0] is frame:
            return hit[1]
        value = read()
        memo[key] = (frame, value)
        return value

    # ------------------------------------------------------------------
    # Index confirmation
    # ------------------------------------------------------------------
    def _index_symbols(self) -> list[str]:
        """The universe-wide ``index_symbols``, upper-cased: the ETFs
        ``active_watchlist`` streams and the fallback of
        ``_indices_for_symbol``, read here once so the two cannot disagree.

        An empty list means no index ETFs, and so does a missing key: a
        loaded config always carries it (each manifest supplies its own list,
        SMH / IGV / XLK for top_tier and none for small_cap_squeeze). Until
        2026-09-26 ``_indices_for_symbol`` read an empty list as SPY / QQQ,
        which were never streamed.
        """
        return [str(s).upper().strip() for s in (self.params.get("index_symbols") or []) if str(s).strip()]

    def _indices_for_symbol(self, symbol: str) -> list[str]:
        """Return the index ETFs to consult when confirming trades on
        *symbol*. Walks ``sector_groups`` to find which sector owns the
        symbol, then reads ``sector_index_map[sector]`` for the per-sector
        ETF list.

        Falls back to the universe-wide ``index_symbols`` when the symbol
        isn't in any sector OR the sector has no per-sector ETF mapping.
        Backward-compat: dropping ``sector_index_map`` from config = the
        old "OR across every index_symbols entry" behavior.

        The fallback is intentional, not lazy — it lets a user opt-in
        sector-by-sector instead of forcing them to map all 11 GICS sectors
        upfront. Sectors without a map entry retain the broad-market
        confirmation path.

        Empty when there are no index symbols (``index_symbols: []`` and no
        mapping for the symbol): the ETF path of ``_index_confirms`` then
        has nothing to agree, ``_index_neutral`` reads not neutral (since
        2026-10-13; it read neutral until then), the relative-strength gate
        and the sector beta have no benchmark, and the signal's
        ``confirmation_indices`` stamp is empty.
        """
        fallback = self._index_symbols()
        sector_groups = self.params.get("sector_groups") or {}
        sector_index_map = self.params.get("sector_index_map") or {}
        if not sector_groups or not sector_index_map:
            return fallback
        symbol_upper = str(symbol).upper()
        for sector_name, members in sector_groups.items():
            if not isinstance(members, (list, tuple)):
                continue
            if symbol_upper in {str(m).upper() for m in members if m}:
                mapped = sector_index_map.get(sector_name)
                if mapped:
                    cleaned = [str(s).upper().strip() for s in mapped if str(s).strip()]
                    if cleaned:
                        return cleaned
                break
        return fallback

    def _leg_anchor_vwap(self, frame: pd.DataFrame) -> float | None:
        """VWAP anchored at the CURRENT LEG's origin instead of at 09:30.

        Session VWAP is a whole-day average, so after a morning flush it sits
        far above price and "price has reclaimed VWAP" only becomes true long
        after a reversal is under way. Measured on a 3% flush that retraced
        87%: session VWAP was reclaimed 44 minutes after the low with half the
        move already gone, while a VWAP anchored at the low was reclaimed
        after 1 minute. Anchoring to the leg is what lets the confirmation
        describe the move that is actually happening.

        The anchor is today's more recent extreme — the low when we are in an
        up-leg, the high when we are in a down-leg. Two guards stop it
        chasing every wiggle, which would read an ordinary pullback as a
        reversal and confirm the wrong side:

          * ``leg_anchor_min_age_bars`` — the extreme must be far enough back
            to be a confirmed pivot. A pullback high in a grind is only a few
            bars old; a reversal pivot is not. This is the guard that does
            the work: at 15 bars an ordinary grind-with-pullbacks produced 12
            spurious counter-trend confirmations, at 20 it produced none.
          * ``leg_anchor_min_impulse_pct`` — price must have travelled far
            enough from the anchor for the leg to mean anything.

        Returns ``None`` when there is no established leg, in which case the
        caller uses session VWAP — the correct reference when the session has
        not yet turned.

        Both guards and the 5-bar session floor count *frame*'s bars, and
        every caller hands it a 1m step frame (each peer's in
        ``_frame_posture``, the candidate's in ``_decide_side``, both through
        ``_leg_anchor_read``), so they are minutes: one reference with one
        horizon for the vote and the gate.
        """
        if frame is None or frame.empty or "volume" not in frame.columns:
            return None
        # Scan from where the REST of the strategy's session logic starts
        # (mirrors `_day_strength_session_open`): the RTH open, or the 07:00
        # equity-stream open in extended-indicator mode. Scanning from
        # midnight instead lets a thin pre-market print become the anchor —
        # on a frame carrying an 08:00 dump that moved the anchor off the
        # session low entirely and flipped the verdict, while the rest of the
        # strategy was still measuring from 09:30.
        #
        # Taken by POSITION: today's bars are a contiguous tail of a
        # time-ordered frame, and `same_day_mask` maps a Python lambda over
        # EVERY bar of the merged frame. This ran once per peer per side
        # (once per frame per entry pass since ``_frame_posture``), so on a
        # 12-peer group that was ~15ms of per-candidate overhead for a
        # slice `searchsorted` does in microseconds. `tz=index.tz` covers
        # tz-aware and naive indexes identically.
        index = frame.index
        opened_at = pd.Timestamp(datetime.combine(sessions.now_et().date(), indicator_session_start()), tz=index.tz)
        session = frame.iloc[index.searchsorted(opened_at):]
        if len(session) < 5:
            return None
        closes = session["close"].to_numpy(dtype=float)
        pos = max(int(closes.argmin()), int(closes.argmax()))
        if (len(closes) - 1 - pos) < int(self.params.get("leg_anchor_min_age_bars", 20)):
            return None
        anchor_px = closes[pos]
        if anchor_px <= 0:
            return None
        min_impulse = float(self.params.get("leg_anchor_min_impulse_pct", 0.005))
        if abs(closes[-1] - anchor_px) / anchor_px < min_impulse:
            return None
        leg = session.iloc[pos:]
        volume = leg["volume"].to_numpy(dtype=float)
        total = float(volume.sum())
        if not total > 0:
            return None
        typical = (leg["high"].to_numpy(dtype=float)
                   + leg["low"].to_numpy(dtype=float)
                   + leg["close"].to_numpy(dtype=float)) / 3.0
        return float((typical * volume).sum() / total)

    def _leg_anchor_read(self, frame: pd.DataFrame) -> float | None:
        """``_leg_anchor_vwap(frame)``, scanned once per frame in an entry
        pass (``_pass_memo_read``): a candidate's side vote reads its step
        frame's leg, and the breadth gate reads the same frame again as a
        peer of each other candidate of its sector. Keyed also on what the
        scan reads: the clock's date, the session start and the two leg
        params."""
        inputs = (sessions.now_et().date(), indicator_session_start(),
                  int(self.params.get("leg_anchor_min_age_bars", 20)),
                  float(self.params.get("leg_anchor_min_impulse_pct", 0.005)))
        return self._pass_memo_read("leg_anchor", frame, inputs, lambda: self._leg_anchor_vwap(frame))

    def _frame_agrees(self, side: Side, frame: pd.DataFrame | None) -> bool | None:
        """Does *frame*'s latest bar lean *side*? ``None`` when unreadable.

        The shared posture test (``indicators.bar_posture``: close vs a VWAP
        reference plus EMA9/EMA20 alignment), used for both the sector ETF
        and each sector peer so the two confirmation paths answer the same
        question.

        The reference is session VWAP, or the current leg's anchored VWAP
        when ``leg_anchored_confirmation`` is on and a leg is established
        (see ``_leg_anchor_vwap``). On a trending or choppy session the two
        agree; they diverge only after the session has turned, which is
        exactly where session VWAP describes the wrong move.
        """
        if frame is None or frame.empty:
            return None
        return self._frame_posture(frame) == side

    def _frame_posture(self, frame: pd.DataFrame) -> Side | None:
        """The ``bar_posture`` of *frame*'s latest bar against
        ``_frame_agrees``' reference. It depends on neither the side nor the
        candidate, and ``_index_confirms`` asks it of every sector peer for
        every candidate and side (~390 reads of ~28 frames a top_tier pass),
        so an entry pass reads it once per frame (``_pass_memo_read``), keyed
        also on what the leg anchor reads: the clock's date, the session
        start and the three ``leg_*`` params."""
        leg_anchored = bool(self.params.get("leg_anchored_confirmation", False))

        def read() -> Side | None:
            reference = self._leg_anchor_read(frame) if leg_anchored else None
            return bar_posture(frame.iloc[-1], reference)

        inputs = (sessions.now_et().date(), indicator_session_start(), leg_anchored,
                  int(self.params.get("leg_anchor_min_age_bars", 20)),
                  float(self.params.get("leg_anchor_min_impulse_pct", 0.005)))
        return self._pass_memo_read("posture", frame, inputs, read)

    def _index_confirms(self, side: Side, symbol: str, bars: dict[str, pd.DataFrame]) -> tuple[bool, str]:
        """``(confirmed, why_not)``: does *symbol*'s sector tape agree with
        *side*? ``why_not`` is the refusal's detail (``""`` when confirmed),
        which the build queue puts in ``<side>_build_failed_<regime>_
        index_not_confirmed(<why_not>)``.

        Two paths, chosen by how much of the sector the symbol itself is:

        **Breadth** (preferred). Counts how many of the symbol's OTHER
        ``sector_groups`` members lean *side*, and requires at least
        ``index_breadth_min_agree_frac`` of the readable ones. Used whenever
        the symbol has at least ``index_breadth_min_peers`` peers. A peer
        reads only with a fresh frame: one whose newest 1m bar closed more
        than ``INDEX_FRAME_MAX_AGE_MINUTES`` ago (a halted peer, an outage)
        is left out, and so is a missing one. Fewer than
        ``index_breadth_min_peers`` readable peers is no confirmation
        (``breadth_unreadable(...)``): the tape cannot be read, so it does
        not agree (2026-10-13). Until then a stale peer voted its
        old posture, and too few readable peers fell back to the ETF.

        **Sector ETF**. The original check — at least one mapped ETF leaning
        *side*, on a fresh frame. Used when the symbol has fewer mapped peers
        than ``index_breadth_min_peers``. A symbol with no index ETFs
        (``_indices_for_symbol`` empty), or none fresh, has nothing to agree
        on this path, so it is not confirmed.

        Breadth exists because the ETF test is close to circular on a mega-cap
        universe: AAPL+MSFT+NVDA+AVGO are roughly 45% of XLK, GOOG+META about
        45% of XLC, AMZN+TSLA about 40% of XLY. Asking XLK whether AAPL's
        move is confirmed substantially asks AAPL about AAPL, and it fails in
        the one case that matters — the mega cap moving against the rest of
        its sector. Peer breadth excludes the symbol itself, so it cannot
        confirm a move with that move.
        """
        if not bool(self.params.get("require_index_confirmation", True)):
            return True, ""
        now = sessions.now_et()
        peers = self._sector_peers(symbol)
        min_peers = int(self.params.get("index_breadth_min_peers", 3))
        if len(peers) >= min_peers:
            agree = readable = stale = missing = 0
            for peer in peers:
                frame = bars.get(peer)
                if frame is None or frame.empty:
                    missing += 1
                    continue
                if frame_age_minutes(frame, now) > INDEX_FRAME_MAX_AGE_MINUTES:
                    stale += 1
                    continue
                readable += 1
                if self._frame_agrees(side, frame):
                    agree += 1
            if readable < min_peers:
                return False, (f"breadth_unreadable(readable={readable},stale={stale},missing={missing},"
                               f"min_peers={min_peers})")
            min_frac = float(self.params.get("index_breadth_min_agree_frac", 0.5))
            if agree / readable >= min_frac:
                return True, ""
            return False, f"breadth(agree={agree}/{readable}<{min_frac:.2f})"
        etfs = self._indices_for_symbol(symbol)
        fresh = [sym for sym in etfs if bars.get(sym) is not None and not bars[sym].empty
                 and frame_age_minutes(bars[sym], now) <= INDEX_FRAME_MAX_AGE_MINUTES]
        for sym in fresh:
            if self._frame_agrees(side, bars[sym]):
                return True, ""
        return False, f"etf(fresh={len(fresh)}/{len(etfs)},agree=0)"

    def _index_neutral(self, symbol: str, bars: dict[str, pd.DataFrame]) -> bool:
        """Whether every index ETF of *symbol* (``_indices_for_symbol``) sits
        within 0.25% of its own VWAP: not directional. The range regime's
        +0.5 reads it. Each ETF must read, on a fresh frame
        (``INDEX_FRAME_MAX_AGE_MINUTES``) with a close and a positive VWAP,
        and a symbol must have one: an ETF that does not read is no
        evidence the tape is neutral (2026-10-13; until then a
        missing frame was skipped and a missing VWAP read as the close, both
        "neutral", and so was a symbol with no ETF)."""
        index_symbols = self._indices_for_symbol(symbol)
        if not index_symbols:
            return False
        now = sessions.now_et()
        for sym in index_symbols:
            frame = bars.get(sym)
            if frame is None or frame.empty or frame_age_minutes(frame, now) > INDEX_FRAME_MAX_AGE_MINUTES:
                return False
            last = frame.iloc[-1]
            close = safe_float(last.get("close"))
            vwap = safe_float(last.get("vwap"))
            if close is None or vwap is None or vwap <= 0:
                return False
            if abs((close - vwap) / vwap) >= 0.0025:
                return False
        return True

    def _sector_day_strength(self, symbol: str, bars: dict[str, pd.DataFrame]) -> tuple[float | None, str]:
        """``(day_strength, why_not)``: the first of *symbol*'s sector ETFs
        (``_indices_for_symbol``) that reads, its day strength (close vs
        session open, in percent), for the relative-strength gate in
        ``_read_candidate``. An ETF reads on a fresh frame only
        (``INDEX_FRAME_MAX_AGE_MINUTES``, the index confirmation's bound),
        with a positive close and a session open. ``(None, why_not)`` when
        none reads, ``why_not`` naming each ETF's state, ``SMH=stale`` /
        ``missing`` / ``unreadable`` (``etfs=none`` with no ETF): the gate
        then refuses the candidate (2026-10-13; until then it read a frame
        of any age, a NaN close as 0, and skipped on no frame). A frame that
        raises propagates: until 2026-09-26 an error here was skipped as a
        missing frame, which let the entry through."""
        states: list[str] = []
        now = sessions.now_et()
        for sym in self._indices_for_symbol(symbol):
            frame = bars.get(sym)
            if frame is None or frame.empty:
                states.append(f"{sym}=missing")
                continue
            if frame_age_minutes(frame, now) > INDEX_FRAME_MAX_AGE_MINUTES:
                states.append(f"{sym}=stale")
                continue
            close = safe_float(frame.iloc[-1].get("close"))
            ds = (None if close is None or close <= 0
                  else self._compute_live_bias_and_day_strength(frame, close)[1])
            if ds is None:
                states.append(f"{sym}=unreadable")
                continue
            return ds, ""
        return None, ",".join(states) or "etfs=none"

    # ------------------------------------------------------------------
    # Daily statistics — per-symbol volatility scale + sector beta
    # ------------------------------------------------------------------
    def _symbol_daily_stats(self, symbol: str, data=None) -> SymbolDailyStats | None:
        """Cached :class:`SymbolDailyStats` for *symbol*, or ``None``.

        Mega-cap universes span a wide volatility range (COST/V/TMUS around
        0.9% ADR, NVDA/TSLA/AMD around 3%+), so a parameter written as a flat
        percentage is a different gate on every name. This resolves the two
        daily-horizon numbers that let the strategy state thresholds in
        symbol-relative terms: ``adr_pct`` (the scale) and ``beta`` versus the
        symbol's own sector ETF (the expected share of a sector move).

        Costs one Schwab daily ``price_history`` call per symbol per ET day
        via ``MarketDataStore.get_daily_history``, which the engine makes
        from the prewarm on (``TopTierAdaptiveStrategy.daily_history_symbols``);
        everything after that is a dict lookup. Returns ``None`` when the feed is unavailable or the
        fetch failed — callers must gate on that explicitly instead of
        assuming a default ADR or a beta of 1.0. The feed reports a failed
        fetch as ``None`` (logged; a read never fetches it again that day,
        and the engine's prefetch retries it until the first entry window,
        ``MarketDataStore.daily_history_due``); anything it
        raises is not a failed fetch and propagates. Until 2026-09-26 it was
        read as one, which skipped the relative-strength gate in silence.
        """
        if data is None or not hasattr(data, "get_daily_history"):
            return None
        today = sessions.now_et().date()
        if self._daily_stats_date != today:
            self._daily_stats.clear()
            self._daily_stats_date = today
        key = str(symbol).upper().strip()
        cached = self._daily_stats.get(key)
        if cached is not None:
            return cached
        symbol_daily = data.get_daily_history(key)
        if symbol_daily is None or symbol_daily.empty:
            return None
        benchmark = None
        benchmark_daily = None
        indices = self._indices_for_symbol(key)
        if indices:
            benchmark = indices[0]
            benchmark_daily = data.get_daily_history(benchmark)
            if benchmark_daily is None or benchmark_daily.empty:
                benchmark_daily = None
        stats = build_symbol_stats(
            key,
            symbol_daily,
            benchmark_symbol=benchmark if benchmark_daily is not None else None,
            benchmark_frame=benchmark_daily,
            adr_lookback_days=int(self.params.get("adr_lookback_days", 20)),
            beta_lookback_days=int(self.params.get("beta_lookback_days", 60)),
        )
        self._daily_stats[key] = stats
        return stats

    def _vol_scale(self, symbol: str, data=None) -> float:
        """Multiplier that converts a percent-of-price parameter tuned on a
        reference-volatility name into this symbol's equivalent.

        ``reference_adr_pct`` is the ADR the existing absolute thresholds were
        written against; a symbol running twice that gets 2.0. Returns 1.0
        (parameters unchanged) when scaling is disabled or the symbol has no
        usable daily stats, so a feed outage degrades to the previous
        behaviour rather than to an arbitrary number.
        """
        if not bool(self.params.get("volatility_scaled_thresholds", True)):
            return 1.0
        stats = self._symbol_daily_stats(symbol, data)
        return volatility_scale(
            stats,
            float(self.params.get("reference_adr_pct", 0.018)),
            min_scale=float(self.params.get("volatility_scale_min", 0.6)),
            max_scale=float(self.params.get("volatility_scale_max", 2.2)),
        )

    @staticmethod
    def _normalized_regime_score(regime: str, score: float, threshold: float) -> float:
        """Fraction of a regime's own headroom that *score* used, in 0..1.

        ``(score - threshold) / (ceiling - threshold)`` — 0.0 sits exactly on
        the regime's qualifying floor, 1.0 at the most its scorer can return.
        This is the only form in which two regimes' scores may be compared:
        raw scores are on per-regime scales (see REGIME_SCORE_CEILINGS).

        A regime missing from REGIME_SCORE_CEILINGS, or one whose configured
        threshold is at or above its ceiling, degenerates to 0.0 rather than
        raising — a mis-set threshold should surface as "never wins the
        auction", not as a crash mid-cycle.
        """
        ceiling = REGIME_SCORE_CEILINGS.get(regime)
        if ceiling is None:
            return 0.0
        headroom = float(ceiling) - float(threshold)
        if headroom <= 0.0:
            return 0.0
        return max(0.0, min(1.0, (float(score) - float(threshold)) / headroom))

    @staticmethod
    def _regime_rank_unit(regime: str, threshold: float) -> float:
        """What one raw score point is worth on *regime*'s normalised scale:
        ``1 / (ceiling - threshold)``, the slope of ``_normalized_regime_score``.

        Stamped as ``regime_rank_unit`` next to ``regime_score_normalized``
        (2026-09-24): the manifest's ``signal_priority`` adds the shared
        entry-context score -- raw final-priority points -- to the normalised
        regime score through it (``shared_score_weight`` x score x unit), so a
        shared point moves a signal exactly as far as the same point added to
        its raw regime score would (unclamped). 0.0 where the normalised
        score degenerates (unknown regime, threshold at or above the
        ceiling): no headroom, so no shared term either.
        """
        ceiling = REGIME_SCORE_CEILINGS.get(regime)
        if ceiling is None:
            return 0.0
        headroom = float(ceiling) - float(threshold)
        return 1.0 / headroom if headroom > 0.0 else 0.0

    # ------------------------------------------------------------------
    # Side asymmetry
    #
    # Every threshold in this strategy is shared between LONG and SHORT, which
    # assumes the two sides are mirror images. On an equity universe they are
    # not:
    #   * Upside moves in crowded names are faster and sharper than downside
    #     ones — a short covering into a squeeze needs more stop room to
    #     survive the same amount of "normal" adverse movement.
    #   * Equities drift up over time, so a short is fighting the base rate
    #     and deserves a higher bar to fire at all.
    #   * That same drift means short profits are less durable, so taking them
    #     sooner is worth more than holding for the last fraction of R.
    # These three multipliers encode exactly those three asymmetries rather
    # than duplicating the whole parameter block per side. All default to
    # symmetric (premium 0.0, mults 1.0), so a preset that does not set them
    # behaves exactly as before.
    # ------------------------------------------------------------------
    def _short_score_premium(self, side: Side) -> float:
        """Extra regime score a SHORT must clear beyond its normal floor."""
        if side != Side.SHORT:
            return 0.0
        return max(0.0, float(self.params.get("short_min_score_premium", 0.0)))

    def _side_stop_buffer_mult(self, side: Side) -> float:
        """Stop-buffer multiplier for *side* — widens shorts against squeezes."""
        if side != Side.SHORT:
            return 1.0
        return max(0.1, float(self.params.get("short_stop_buffer_mult", 1.0)))

    def _side_target_rr_mult(self, side: Side) -> float:
        """Target-R:R multiplier for *side* — banks short profits sooner."""
        if side != Side.SHORT:
            return 1.0
        return max(0.1, float(self.params.get("short_target_rr_mult", 1.0)))

    def _pct_param(self, name: str, default: float, vol_scale: float) -> float:
        """A percent-of-price parameter, rescaled to the symbol's own ADR.

        Only percent-of-price thresholds go through here. Parameters already
        expressed in ATR multiples (``*_atr_mult``) are volatility-relative by
        construction and must NOT be scaled again — doing so would square the
        adjustment.
        """
        return float(self.params.get(name, default)) * float(vol_scale)

    def _sector_peers(self, symbol: str) -> list[str]:
        """Other members of *symbol*'s ``sector_groups`` entry.

        Feeds the breadth confirmation in ``_index_confirms``. Returns an
        empty list when the symbol is unmapped or alone in its sector.
        """
        symbol_upper = str(symbol).upper().strip()
        for members in (self.params.get("sector_groups") or {}).values():
            if not isinstance(members, (list, tuple)):
                continue
            cleaned = [str(m).upper().strip() for m in members if str(m or "").strip()]
            if symbol_upper in cleaned:
                return [m for m in cleaned if m != symbol_upper]
        return []

    @staticmethod
    def _recent_momentum_pct(ltf: pd.DataFrame, lookback_bars: int, close: float) -> float | None:
        """The percent change from the close ``lookback_bars`` LTF bars
        before the current one (``ltf``'s last row, the bucket the step
        frame's last 1m bar is in) to ``close``, the latest 1m close:
        ``(close - close_ago) / close_ago * 100``. The bar it reads has
        closed; on the 5m LTF and the shipped 6 bars the span is 25-30
        minutes, as it was on the 5m LTF before 2026-05-29 (the current
        bucket's close was the latest close then too).

        Its reader is ``_decide_side``: the side vote's first signal, the
        LOCAL trend (last N bars). On a stock that has reversed intraday,
        ``day_strength`` (from the session open) still reflects the original
        direction while recent price action has flipped.

        Returns ``None`` when there aren't enough bars or the prior close
        does not read or is non-positive."""
        if ltf is None or len(ltf) < lookback_bars + 1:
            return None
        prior_close = safe_float(ltf.iloc[-(lookback_bars + 1)].get("close"))
        if prior_close is None or prior_close <= 0:
            return None
        return (float(close) - prior_close) / prior_close * 100.0

    def _decide_side(
        self,
        ltf: pd.DataFrame,
        closed: pd.DataFrame,
        frame: pd.DataFrame,
        close: float,
        vwap: float,
        ema9: float,
        ema20: float,
    ) -> tuple[Side | None, dict[str, Any]]:
        """Pick the trade side from current price action (Fix A, 2026-05-27).

        ``ltf`` is the LTF, its current bucket last; ``closed`` its closed
        buckets (``closed_ltf``); ``frame`` the step frame; ``close`` and
        ``vwap`` the step frame's last close and session VWAP; ``ema9`` /
        ``ema20`` the last closed bucket's.

        Counts LONG and SHORT votes across four current-action signals:
          1. Recent return: ``close`` against the close
             ``side_decision_recent_lookback_bars`` LTF bars before the
             current one (``_recent_momentum_pct``; the shipped 6 on the 5m
             LTF = 25-30 minutes, the window Fix A was designed on)
          2. Close vs VWAP — the current leg's anchored VWAP under
             ``leg_anchored_confirmation``, measured on the step frame (the
             1m frame the peers' confirmation measures it on, so the vote and
             the gate read one reference with one ``leg_anchor_min_age_bars``
             horizon), session VWAP otherwise
          3. EMA9 vs EMA20, of the last closed LTF bucket
          4. The last 3 CLOSED LTF bars' net direction (green-count vs
             red-count): three 5m buckets, 15 minutes, as Fix A designed it

        Each signal contributes one LONG or one SHORT vote, or abstains
        when its read is genuinely neutral (recent return inside the noise
        threshold, price inside the VWAP dead-band, a mixed 3-bar colour
        count). Decision threshold is a MAJORITY OF THE SIGNALS THAT VOTED:
        a side wins when its votes ≥
        ``max(side_decision_min_agreeing, ceil(participating × side_decision_majority_frac))``
        AND opposing ≤ ``side_decision_max_opposing``. Scaling to the
        participating count matters because two of the four signals abstain
        often — an absolute threshold rejected unanimous 2-0 reads. Mixed
        reads return ``None``: the direction-following regimes are refused
        (``<side>_build_failed_<regime>_side_undecided``), while range and
        sr_scalp still build.

        This replaces the previous "evaluate both sides per regime, pick
        highest-scoring" implicit side selection. The old approach could
        pick SHORT just because the SHORT regime score was 0.5 higher
        even when every meaningful current-action signal said LONG.
        """
        long_votes = 0
        short_votes = 0
        breakdown: list[str] = []

        recent_lookback = max(1, int(self.params.get("side_decision_recent_lookback_bars", 6)))
        recent_threshold = float(self.params.get("side_decision_recent_threshold_pct", 0.1))
        recent_pct = self._recent_momentum_pct(ltf, recent_lookback, close)
        if recent_pct is not None:
            if recent_pct >= recent_threshold:
                long_votes += 1
                breakdown.append(f"recent+{recent_pct:.2f}%>L")
            elif recent_pct <= -recent_threshold:
                short_votes += 1
                breakdown.append(f"recent{recent_pct:.2f}%>S")
            else:
                breakdown.append(f"recent{recent_pct:+.2f}%>neutral")

        # VWAP arm. Under ``leg_anchored_confirmation`` this reads the SAME
        # reference ``_frame_agrees`` does — the current leg's anchored VWAP —
        # rather than session VWAP.
        #
        # Session VWAP is a whole-day average, so after a flush it sits far
        # above price and this arm keeps voting for the OLD direction well
        # into the reversal. Auditing each arm against the tape's actual
        # direction on a V-reversal: this one was wrong on 54 of 120 bars and
        # the EMA arm on 59, while the recent-return arm — the only genuinely
        # current-action signal — was wrong on 14. The two lagging arms are
        # also the two that vote most reliably (``recent`` and ``bars3`` carry
        # dead-bands and abstain often), so they outvote the accurate one.
        #
        # Leaving the layers on different references was also incoherent: the
        # vote and the confirmation gate disagreed about what "reclaimed VWAP"
        # meant for the same symbol on the same bar.
        #
        # The anchor is measured on the step frame, the 1m bars the peers'
        # anchor is measured on (2026-10-13): ``leg_anchor_min_age_bars`` and
        # the 5-bar session floor count that frame's bars, so on the 5m LTF
        # the vote would have needed 100 minutes of leg age where the gate
        # needs 20, and read session VWAP on most of the minutes the gate
        # reads the leg (the incoherence 2026-09-19 removed).
        vwap_buffer = float(self.params.get("side_decision_vwap_buffer_pct", 0.0005))
        vwap_reference = None
        if bool(self.params.get("leg_anchored_confirmation", False)):
            vwap_reference = self._leg_anchor_read(frame)
        # Label the breakdown so an operator reading a `side_undecided(...)`
        # line can tell which reference produced the vote.
        vwap_label = "legvwap" if vwap_reference is not None else "vwap"
        if vwap_reference is None:
            vwap_reference = vwap
        if vwap_reference > 0:
            vwap_dist = (close - vwap_reference) / vwap_reference
            if vwap_dist > vwap_buffer:
                long_votes += 1
                breakdown.append(f"close>{vwap_label}>L")
            elif vwap_dist < -vwap_buffer:
                short_votes += 1
                breakdown.append(f"close<{vwap_label}>S")
            else:
                breakdown.append(f"close~{vwap_label}>neutral")

        if ema9 > ema20:
            long_votes += 1
            breakdown.append("ema9>ema20>L")
        elif ema9 < ema20:
            short_votes += 1
            breakdown.append("ema9<ema20>S")

        # Last-3-bars direction. Requires UNANIMITY to vote (2026-07-29).
        # The old rule (greens >= 2 -> LONG, else SHORT) had no neutral band,
        # so with three bars it ALWAYS voted — and in a downtrend the constant
        # two-green bounce bars voted LONG against the trend while carrying
        # the same weight as the EMA structure. Across 2,074 undecided tech
        # rows on 2026-07-28/29 it split 1,021 LONG / 1,053 SHORT: a coin
        # flip. Three consecutive same-colour bars is a real read; 2-of-3 is
        # not, so mixed counts now abstain like the other two signals.
        #
        # The three are the last CLOSED LTF bars (2026-10-13): three 5m
        # buckets, the 15 minutes Fix A designed it on, read on the 1m LTF
        # as three minutes from 2026-05-29. A forming bucket's partial bar
        # does not vote. A bar whose open or close does not read leaves the
        # arm out.
        if closed is not None and len(closed) >= 3:
            last_3 = closed.iloc[-3:]
            opens = [safe_float(value) for value in last_3["open"]]
            closes = [safe_float(value) for value in last_3["close"]]
            if any(value is None for value in (*opens, *closes)):
                greens = -1
            else:
                greens = sum(1 for o, c in zip(opens, closes) if c > o)
            if greens == 3:
                long_votes += 1
                breakdown.append(f"3b:{greens}G>L")
            elif greens == 0:
                short_votes += 1
                breakdown.append(f"3b:{greens}G>S")
            elif greens > 0:
                breakdown.append(f"3b:{greens}G>neutral")

        # Decision threshold is a MAJORITY OF THE SIGNALS THAT ACTUALLY VOTED,
        # not an absolute count (2026-07-29). ``recent`` and ``vwap`` both
        # carry neutral dead-bands and abstain ~35% of the time, so the old
        # absolute ``side_decision_min_votes: 3`` was unreachable whenever two
        # signals sat out — a UNANIMOUS 2-0 read was rejected for having only
        # two votes. Observed directly on 2026-07-28:
        #   NVDA long=0 short=2 [recent-0.01%>neutral, close~vwap>neutral,
        #                        ema9<ema20>S, 3b:1G>S]   -> rejected
        # In a downtrend the reliably-achievable SHORT tally is 2 (vwap +
        # ema), so requiring 3 only admitted shorts when price was already
        # making a new low — i.e. at local exhaustion, right before the
        # bounce that then swept the stop. side_undecided was the single
        # largest blocker across the tech universe (3,506 of 12,935 rows)
        # during a week those names fell 4-12%.
        participating = long_votes + short_votes
        min_agreeing = int(self.params.get("side_decision_min_agreeing", 2))
        majority_frac = float(self.params.get("side_decision_majority_frac", 0.6))
        max_opposing = int(self.params.get("side_decision_max_opposing", 1))
        required = max(min_agreeing, math.ceil(participating * majority_frac))
        vote_info = {
            "long": long_votes,
            "short": short_votes,
            "participating": participating,
            "required": required,
            "breakdown": breakdown,
        }
        if long_votes >= required and short_votes <= max_opposing:
            return Side.LONG, vote_info
        if short_votes >= required and long_votes <= max_opposing:
            return Side.SHORT, vote_info
        return None, vote_info

    @staticmethod
    def _entry_bar_confirms(symbol: str, side: Side, frame: pd.DataFrame) -> bool:
        """Confirmation-bar trigger (Fix B, 2026-05-27): the most recent
        FULLY CLOSED 1m bar of *symbol*'s step frame ``frame`` must confirm
        direction before we enter.

        For LONG: ``last_closed.close > last_closed.open`` (green bar) AND
        ``last_closed.close > prev_closed.close`` (higher-high tape).
        Mirror for SHORT. The bar is entry timing, so it is a 1m bar whatever
        the LTF the setup is measured on (2026-10-13): the step frame's own
        bars, the bar the entry is built at last. The step frame holds closed
        bars only, so the last closed bar is its last row, unless that bar's
        minute is still trading by the clock (``last_closed_bars``: it is
        left out and logged), so a forming bar never confirms. Until
        2026-10-12 this read ``iloc[-2]`` on the premise that ``iloc[-1]`` was
        the bar in progress, which a step frame has seldom held (none from
        2026-09-24 on, in H:'s archive): the gate judged the bar before the
        last closed one, so a setup confirmed one bar late, and an armed
        retest needed its reclaim bar and then one more. From 2026-10-12 to
        2026-10-13 it read the LTF's last closed bar, the same bars while
        the LTF was 1m. Returns ``True`` when not enough bars (don't block on
        thin early-session data — other gates handle that case), and
        ``False`` when a bar does not read: it confirms nothing. Until
        2026-09-26 a read that raised returned ``True`` and a missing column
        read as 0, which a LONG's close always beat."""
        if frame is None:
            return True
        closed = last_closed_bars(symbol, frame, 1, frame)
        if len(closed) < 2:
            return True
        last_closed = closed.iloc[-1]
        prev_closed = closed.iloc[-2]
        last_open = safe_float(last_closed.get("open"))
        last_close = safe_float(last_closed.get("close"))
        prev_close = safe_float(prev_closed.get("close"))
        if last_open is None or last_close is None or prev_close is None:
            return False
        if side == Side.LONG:
            return last_close > last_open and last_close > prev_close
        return last_close < last_open and last_close < prev_close

    # ------------------------------------------------------------------
    # Live directional bias
    # ------------------------------------------------------------------
    def _compute_live_bias_and_day_strength(
        self, frame: pd.DataFrame, close: float,
    ) -> tuple[Side | None, float | None]:
        """Single-source computation: returns ``(bias, day_strength_pct)``.

        ``day_strength_pct = (close − session_open) / session_open * 100``.
        Bias is ``Side.LONG`` when day_strength exceeds
        ``+directional_bias_min_day_strength`` (default 0.20%), ``Side.SHORT``
        below the negative threshold, ``None`` within the neutral band.

        Both values share one ``session_open_price`` call so the soft-bias
        penalty path doesn't repeat the session-open lookup. Used by
        ``_read_candidate`` (needs both bias for side selection AND magnitude
        for penalty scaling) and by ``_compute_live_directional_bias`` (the
        single-return wrapper kept for test compatibility).

        Returns ``(None, None)`` when the session_open is unavailable
        (warmup frame, missing data, etc.).
        """
        session_open = self._day_strength_session_open(frame)
        if session_open is None or session_open <= 0:
            return None, None
        try:
            day_strength = (float(close) - session_open) / session_open * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            return None, None
        threshold = float(self.params.get("directional_bias_min_day_strength", 0.20))
        if day_strength > threshold:
            return Side.LONG, day_strength
        if day_strength < -threshold:
            return Side.SHORT, day_strength
        return None, day_strength

    def _compute_live_directional_bias(self, frame: pd.DataFrame, close: float) -> Side | None:
        """Bias-only wrapper around ``_compute_live_bias_and_day_strength``.
        Kept as the public API for tests + any caller that doesn't need
        the day_strength magnitude. Same semantics as before: returns
        ``Side.LONG`` / ``Side.SHORT`` / ``None`` based on day_strength
        vs. ``directional_bias_min_day_strength``."""
        bias, _ = self._compute_live_bias_and_day_strength(frame, close)
        return bias

    def _volatility_widening_factor(self, tech_ctx: Any, current_time: Any = None) -> float:
        """Stop-buffer widening multiplier — combines TWO orthogonal triggers:

        1. **ATR expansion (Tier 2a, existing)** — when the last 5 bars'
           mean true range is large vs the ``atr14`` in force before them
           (``tech_ctx.atr_expansion_mult > atr_widening_threshold``), stops
           scale up linearly to ``atr_widening_max_factor``. Catches
           RELATIVE volatility surges. Until 2026-09-22 the mult was a
           5-bar net displacement in ATRs, which fired on any clean trend
           (37% of RTH bars over 1.2) and not on a zero-net chop (16% now).

        2. **Early-session time-of-day widening (new)** — applies an
           ABSOLUTE multiplier when ``current_time`` is before
           ``early_session_stop_widening_until``. Catches the post-open
           high-vol window where ATR may not be "expanding" relative to
           recent bars (which are all noisy) but absolute vol is high.
           Without this, a trade taken at 10:10 on AMD got stopped by a
           single 1m wick that recovered 5 min later — the stop was set
           by Tier 2a's RELATIVE-expansion logic, which read normal at
           the time even though absolute volatility was elevated.

        The factor returned is ``max(expansion_factor, time_factor)``,
        capped at ``atr_widening_max_factor``. Tier 2a + time-of-day
        don't compound (multiplying both would over-widen on volatile
        morning opens); the trade gets whichever cushion is larger.

        Returns 1.0 when:
          * ``atr_aware_stop_enabled`` is False (master toggle)
          * No expansion AND not in early session
          * ``tech_ctx`` is missing or has no ``atr_expansion_mult`` AND
            ``current_time`` is unknown
        """
        if not bool(self.params.get("atr_aware_stop_enabled", True)):
            return 1.0
        max_factor = float(self.params.get("atr_widening_max_factor", 1.5))

        # --- Trigger 1: ATR expansion (Tier 2a) ---
        expansion_factor = 1.0
        expansion_mult = float(getattr(tech_ctx, "atr_expansion_mult", 1.0) or 1.0) if tech_ctx is not None else 1.0
        threshold = float(self.params.get("atr_widening_threshold", 1.3))
        if expansion_mult > threshold:
            # Linear scale from 1.0 (at threshold) to max_factor (at 2x threshold)
            scale_range = max(0.1, threshold)
            progress = min(1.0, (expansion_mult - threshold) / scale_range)
            expansion_factor = 1.0 + (max_factor - 1.0) * progress

        # --- Trigger 2: Early-session time-of-day widening ---
        time_factor = 1.0
        if current_time is not None and bool(self.params.get("early_session_stop_widening_enabled", True)):
            # No guard: the cutoff is checked at construction (time_params).
            # Until 2026-09-26 an ``except Exception: pass`` here swallowed
            # the ValueError of str()-ing an unquoted YAML time (10:30 is the
            # int 630, and "630" does not parse), so with an unquoted cutoff
            # the widening never applied.
            cutoff = parse_hhmm(self.params.get("early_session_stop_widening_until", "10:30"))
            if current_time <= cutoff:
                time_factor = float(self.params.get("early_session_stop_widening_mult", 1.3))

        # Take the LARGER widening (not compound) so morning + ATR
        # expansion don't double-multiply into an unrealistic stop.
        return min(max(expansion_factor, time_factor), max_factor)

    def _bias_penalty(self, side: Side, day_strength: float | None,
                      effective_bias: Side | None, respect_bias: bool) -> float:
        """Soft-bias score adjustment applied to a side's regime scores
        when the side disagrees with ``effective_bias``.

        Returns 0.0 when:
          * ``respect_bias`` is False (master toggle off / ORB bypass active)
          * ``effective_bias`` is None (no bias set)
          * ``side`` agrees with ``effective_bias``

        Otherwise returns ``bias_penalty_base * magnitude_factor`` where
        ``magnitude_factor = min(1.0, |day_strength| / bias_penalty_saturate_at)``.
        The manifest ships base 1.0 and saturates at 0.75% day_strength (the
        code's fallbacks are 1.0 and 2.0) — so a −0.5% day with SHORT bias
        applies a 0.67 penalty to LONG-side regimes (a strong structural LONG
        setup can still qualify), while a −1% day applies the full 1.0
        penalty (filters most LONG-side setups).

        Replaces Fix A's previous HARD lockout (``preferred_sides = [Side]``).
        With no explicit side decision it applies to every regime of the
        disagreeing side; when the side vote picked a side, only to range and
        sr_scalp, the regimes that build against it.
        Preserves the 2026-04-20 protection (deep day_strength → full
        penalty) and the trailing-bias memory (inferred bias still
        contributes to the penalty).
        """
        if not respect_bias:
            return 0.0
        if effective_bias is None or effective_bias == side:
            return 0.0
        penalty_base = float(self.params.get("bias_penalty_base", 1.0))
        saturate_at = max(0.1, float(self.params.get("bias_penalty_saturate_at", 2.0)))
        ds = float(day_strength) if day_strength is not None else 0.0
        magnitude_factor = min(1.0, abs(ds) / saturate_at)
        return penalty_base * magnitude_factor

    def _day_strength_session_open(self, frame: pd.DataFrame) -> float | None:
        """Session-open anchor for the live ``day_strength`` bias: today's
        first open from where the VWAP/EMA session reset starts
        (``indicator_session_start``) -- the 07:00 equity-stream open in
        extended-hours indicator mode, otherwise the RTH 09:30 open.

        Read for the candidate (its bias, and the momentum regime once per
        side) and for its sector ETF by every candidate the ETF covers
        (``_sector_day_strength``), so an entry pass reads it once per
        frame (``_pass_memo_read``)."""
        day = sessions.now_et().date()
        session_start = indicator_session_start()
        return self._pass_memo_read("session_open", frame, (day, session_start),
                                    lambda: session_open_price(frame, day, session_start=session_start))
