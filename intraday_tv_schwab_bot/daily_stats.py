# SPDX-License-Identifier: MIT
"""Per-symbol daily statistics — volatility scale and benchmark beta.

Intraday frames in this bot span at most ``runtime.history_lookback_minutes``
(780 = ~2 trading days on the top_tier preset), which is far too short to
measure how volatile a symbol normally is or how it moves relative to its
sector. Both questions need daily bars, so this module owns the small amount
of maths that turns a daily OHLC frame into the two numbers the strategies
actually consume:

  * ``adr_pct`` — average true range as a fraction of close, over
    ``adr_lookback_days`` sessions. The universal *scale* for a symbol: a
    threshold expressed as "0.6 x ADR" means the same thing on COST (~0.9%
    ADR) and NVDA (~3.2% ADR), where a flat "1.0%" does not.
  * ``beta`` — OLS slope of the symbol's daily returns regressed on its
    benchmark's daily returns over ``beta_lookback_days`` sessions. Lets a
    relative-strength read subtract the move a symbol was *expected* to make
    given its benchmark, instead of assuming every name moves 1:1 with its
    sector.

Everything here is pure: frames in, floats out. Fetching and caching the
daily frames is ``MarketDataStore.get_daily_history``'s job.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

LOG = logging.getLogger(__name__)

# Minimum usable samples. Below these the estimate is noise and we return
# None so the caller can gate explicitly rather than act on a bad number.
MIN_ADR_SAMPLES = 10
MIN_BETA_SAMPLES = 30


@dataclass(frozen=True)
class SymbolDailyStats:
    """Daily-derived scale + benchmark sensitivity for one symbol.

    ``adr_pct`` and ``beta`` are ``None`` when there were not enough clean
    daily samples to estimate them. Callers must branch on that rather than
    substituting a default — a silently-assumed 1.0 beta is the exact bug
    this module exists to remove.
    """

    symbol: str
    adr_pct: float | None = None
    adr_samples: int = 0
    beta: float | None = None
    beta_benchmark: str | None = None
    beta_samples: int = 0
    beta_r2: float | None = None

    @property
    def has_scale(self) -> bool:
        return self.adr_pct is not None and self.adr_pct > 0.0

    @property
    def has_beta(self) -> bool:
        return self.beta is not None


EMPTY_STATS = SymbolDailyStats(symbol="")


def _clean_daily(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    """Return *frame* with the OHLC columns coerced to float and any row
    carrying a non-positive or missing price dropped. ``None`` when nothing
    usable survives."""
    if frame is None or frame.empty:
        return None
    needed = {"high", "low", "close"}
    if not needed.issubset(frame.columns):
        return None
    out = frame.loc[:, sorted(needed | ({"open"} & set(frame.columns)))].apply(
        pd.to_numeric, errors="coerce"
    )
    out = out.dropna(subset=["high", "low", "close"])
    out = out[(out["close"] > 0.0) & (out["high"] >= out["low"])]
    return out if not out.empty else None


def compute_adr_pct(frame: pd.DataFrame | None, lookback_days: int) -> tuple[float | None, int]:
    """Average true range over the last *lookback_days* sessions, as a
    fraction of the mean close across those sessions.

    True range (not the raw high-low) so overnight gaps count — for mega caps
    the gap is often the majority of a session's actual movement, and a
    high-low ADR would under-scale exactly the names that gap.

    Returns ``(adr_pct, samples_used)``; ``adr_pct`` is ``None`` when fewer
    than ``MIN_ADR_SAMPLES`` clean sessions were available.
    """
    clean = _clean_daily(frame)
    if clean is None:
        return None, 0
    window = clean.tail(max(1, int(lookback_days)) + 1)
    if len(window) < 2:
        return None, 0
    prev_close = window["close"].shift(1)
    true_range = pd.concat(
        [
            window["high"] - window["low"],
            (window["high"] - prev_close).abs(),
            (window["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Drop the first row explicitly. Its prev_close is NaN, and the row-wise
    # max skips NaN rather than propagating it, so that bar would silently
    # contribute a high-low value instead of a true range — and would make a
    # 20-day lookback return 21 samples.
    true_range = true_range.iloc[1:].dropna()
    if len(true_range) < MIN_ADR_SAMPLES:
        return None, len(true_range)
    mean_close = float(window["close"].tail(len(true_range)).mean())
    if mean_close <= 0.0:
        return None, len(true_range)
    adr_pct = float(true_range.mean()) / mean_close
    if not (adr_pct > 0.0) or adr_pct != adr_pct:  # NaN-safe
        return None, len(true_range)
    return adr_pct, int(len(true_range))


def compute_beta(
    symbol_frame: pd.DataFrame | None,
    benchmark_frame: pd.DataFrame | None,
    lookback_days: int,
) -> tuple[float | None, int, float | None]:
    """OLS slope of symbol daily returns on benchmark daily returns.

    Returns ``(beta, samples_used, r_squared)``. ``beta`` is ``None`` when
    fewer than ``MIN_BETA_SAMPLES`` overlapping sessions exist or the
    benchmark had no variance over the window.

    The two frames are inner-joined on their index, so a symbol that IPO'd
    mid-window or a benchmark with a missing session simply contributes
    fewer samples instead of silently misaligning returns.
    """
    sym = _clean_daily(symbol_frame)
    bench = _clean_daily(benchmark_frame)
    if sym is None or bench is None:
        return None, 0, None
    lookback = max(1, int(lookback_days))
    sym_ret = sym["close"].pct_change().dropna()
    bench_ret = bench["close"].pct_change().dropna()
    joined = pd.concat(
        {"sym": sym_ret, "bench": bench_ret}, axis=1, join="inner"
    ).dropna().tail(lookback)
    if len(joined) < MIN_BETA_SAMPLES:
        return None, int(len(joined)), None
    bench_var = float(joined["bench"].var())
    if not (bench_var > 0.0):
        return None, int(len(joined)), None
    covariance = float(joined["sym"].cov(joined["bench"]))
    beta = covariance / bench_var
    if beta != beta:  # NaN
        return None, int(len(joined)), None
    correlation = float(joined["sym"].corr(joined["bench"]))
    r2 = correlation * correlation if correlation == correlation else None
    return float(beta), int(len(joined)), r2


def build_symbol_stats(
    symbol: str,
    symbol_frame: pd.DataFrame | None,
    *,
    benchmark_symbol: str | None = None,
    benchmark_frame: pd.DataFrame | None = None,
    adr_lookback_days: int = 20,
    beta_lookback_days: int = 60,
) -> SymbolDailyStats:
    """Assemble :class:`SymbolDailyStats` for *symbol*.

    ``benchmark_symbol``/``benchmark_frame`` are optional — omit them for a
    symbol that is itself a benchmark (an ETF), or when the caller only needs
    the volatility scale.
    """
    adr_pct, adr_samples = compute_adr_pct(symbol_frame, adr_lookback_days)
    beta = None
    beta_samples = 0
    beta_r2 = None
    if benchmark_frame is not None and benchmark_symbol:
        beta, beta_samples, beta_r2 = compute_beta(
            symbol_frame, benchmark_frame, beta_lookback_days
        )
    return SymbolDailyStats(
        symbol=str(symbol).upper().strip(),
        adr_pct=adr_pct,
        adr_samples=adr_samples,
        beta=beta,
        beta_benchmark=str(benchmark_symbol).upper().strip() if (beta is not None and benchmark_symbol) else None,
        beta_samples=beta_samples,
        beta_r2=beta_r2,
    )


def volatility_scale(
    stats: SymbolDailyStats | None,
    reference_adr_pct: float,
    *,
    min_scale: float = 0.5,
    max_scale: float = 2.5,
) -> float:
    """Multiplier that converts a threshold written for a *reference* symbol
    into the equivalent threshold for this symbol.

    ``reference_adr_pct`` is the ADR the existing absolute parameters were
    tuned against. A symbol with twice that ADR gets 2.0, so a "1% stop"
    becomes a 2% stop and represents the same amount of normal daily noise.

    Clamped to ``[min_scale, max_scale]`` so one anomalous name (a post-split
    or post-news symbol whose 20-day ADR is temporarily 5x) cannot produce an
    absurd stop. Returns 1.0 — i.e. the unscaled parameter — when the symbol
    has no usable ADR, which keeps behaviour identical to the pre-scaling
    code for anything the daily fetch could not cover.
    """
    if stats is None or not stats.has_scale or reference_adr_pct <= 0.0:
        return 1.0
    scale = float(stats.adr_pct) / float(reference_adr_pct)
    return max(float(min_scale), min(float(max_scale), scale))
