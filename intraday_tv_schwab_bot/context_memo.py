# SPDX-License-Identifier: MIT
"""Level and context builds kept across cycles.

The bar of a step frame changes once a minute and the stored HTF frame once
per HTF bar, but the S/R, FVG, order-block, chart, structure and technical
contexts used to be rebuilt every cycle (the per-cycle caches died with the
cycle). A ``ContextMemo`` keeps the last build of each slot -- a builder and
the request it answered (symbol, timeframe, parameters) -- under a key of
every input the build read:

- the frame it read, by ``bars.frame_version`` (the store frames a
  ``get_merged`` hand-out was built from and the variant it is), or for S/R
  the stored HTF frame object itself, pinned by the entry so its id cannot
  be reused;
- the clock, only as the build reads it: which bars have completed
  (``bars.forming_positions``, the flip frame's completed-bar count), the
  session date, and ``indicator_clock_key`` (the session indicator settings
  and whether the clock is inside their session), never the bare minute.

A build files its result only when the clock part of its key reads the same
after the build as before it, so a minute or session boundary crossed while
it ran never files a context under a clock it did not read. A slot not read
for a whole cycle is dropped at the next (``next_generation``), so the memo
holds at most two cycles' worth of slots.

``shadow_every`` (``runtime.context_memo_shadow_every``) re-checks every
N-th hit: it rebuilds, compares the two contexts field by field (floats by
bits), logs CRITICAL on a difference and serves the rebuilt one. It is the
guard against an input a builder starts reading that the key does not hold.
"""
from __future__ import annotations

import dataclasses
import enum
import logging
import threading
from datetime import date, datetime
from typing import Any, Callable, Hashable

import numpy as np
import pandas as pd

from .indicators import get_runtime_indicator_mode, get_session_indicator_window, indicator_session_open

LOG = logging.getLogger(__name__)


def indicator_clock_key() -> tuple:
    """The process-wide inputs every ATR-sized build reads: the session
    indicator mode and window, and whether the clock is inside that session
    (``indicators.indicator_session_open``: ``latest_atr14``, the divergence
    clocks)."""
    return get_runtime_indicator_mode(), get_session_indicator_window(), indicator_session_open()


def _canon(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, (float, np.floating)):
        return ("f", float(value).hex())
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, enum.Enum):
        return ("enum", type(value).__name__, value.name)
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return ("t", value.isoformat())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (type(value).__name__, tuple((f.name, _canon(getattr(value, f.name))) for f in dataclasses.fields(value)))
    if isinstance(value, dict):
        return ("d", tuple(sorted(((repr(k), _canon(v)) for k, v in value.items()), key=lambda kv: kv[0])))
    if isinstance(value, (set, frozenset)):
        return ("s", tuple(sorted((_canon(v) for v in value), key=repr)))
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, tuple(_canon(v) for v in value))
    if isinstance(value, (pd.Series, pd.DataFrame)):
        return ("pd", repr(value.to_dict()))
    if isinstance(value, np.ndarray):
        return ("np", str(value.dtype), value.shape, value.tobytes())
    slots = [name for cls in type(value).__mro__ for name in getattr(cls, "__slots__", ())]
    if slots:
        return (type(value).__name__, tuple((name, _canon(getattr(value, name, None))) for name in slots))
    if hasattr(value, "__dict__"):
        return (type(value).__name__, _canon(vars(value)))
    return ("r", repr(value))


def same_context(a: Any, b: Any) -> bool:
    """Do two builds hold the same values (floats by bits, NaN equal)?"""
    return _canon(a) == _canon(b)


class ContextMemo:
    """One build per slot across cycles; see the module docstring."""

    def __init__(self, name: str, *, shadow_every: int) -> None:
        self.name = name
        self.shadow_every = int(shadow_every)
        self._lock = threading.Lock()
        # slot -> (key, context, pins)
        self._current: dict[Hashable, tuple[tuple, Any, tuple]] = {}
        self._previous: dict[Hashable, tuple[tuple, Any, tuple]] = {}
        self._hits = 0

    def next_generation(self) -> None:
        """A cycle starts: slots not read since the last call are dropped."""
        with self._lock:
            self._previous = self._current
            self._current = {}

    def retain(self, keep: Callable[[Hashable], bool]) -> None:
        """Drop every slot ``keep`` refuses (the store's symbol prune)."""
        with self._lock:
            self._current = {k: v for k, v in self._current.items() if keep(k)}
            self._previous = {k: v for k, v in self._previous.items() if keep(k)}

    def __len__(self) -> int:
        with self._lock:
            return len(self._current) + len(self._previous)

    def serve(
        self,
        slot: Hashable,
        static_key: tuple,
        clock_key: Callable[[], tuple],
        build: Callable[[tuple], Any],
        *,
        pins: tuple = (),
    ) -> Any:
        """The context for ``slot`` built on ``static_key``'s inputs (the
        frame read, the request) at the clock ``clock_key()`` reads: the
        memo's when its key matches, else ``build(clock)``, filed when the
        clock key still reads the same after it. A build that takes a
        clock-derived value as an argument takes it from ``clock``, the very
        value the key holds."""
        clock = clock_key()
        key = (static_key, clock)
        shadow = False
        with self._lock:
            entry = self._current.get(slot)
            if entry is None:
                entry = self._previous.pop(slot, None)
                if entry is not None:
                    self._current[slot] = entry
            if entry is not None and entry[0] == key:
                self._hits += 1
                shadow = self.shadow_every > 0 and self._hits % self.shadow_every == 0
                if not shadow:
                    return entry[1]
        fresh = build(clock)
        still = clock_key() == clock
        if shadow:
            if still and not same_context(entry[1], fresh):
                LOG.critical(
                    "Context memo %s served a context a rebuild does not give: slot=%r key=%r; "
                    "serving the rebuilt one.", self.name, slot, key,
                )
            elif still:
                return entry[1]
        if still:
            with self._lock:
                self._current[slot] = (key, fresh, pins)
        return fresh
