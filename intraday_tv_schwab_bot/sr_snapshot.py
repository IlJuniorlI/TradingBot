# SPDX-License-Identifier: MIT
"""A symbol's S/R snapshot: the trading-mode support/resistance context and
what the bot reads off it -- the state (breakout, near_support, ...), the HTF
trend, the market-structure bias and last event, and the level prices.

The exit record (``PositionManager._position_exit_context``) and the
dashboard's S/R row (``DashboardCache.sr_row``, which adds the strategy's LTF
label) both read it. Until 2026-09-27 it was ``DashboardCache.sr_row``
itself, so the position manager imported the dashboard for its exit record
(refactor cut C41).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .data_feed import DISPLAY_PRICE_KEYS, MarketDataStore
from .htf_levels import summarize_htf_trend
from .numeric import first_float, safe_float

if TYPE_CHECKING:
    import pandas as pd

    from .config import BotConfig
    from .paper_account import PaperAccount
    from ._strategies.strategy_base import BaseStrategy

LOG = logging.getLogger("intraday_tv_schwab_bot.engine")


def symbol_price(data: MarketDataStore, account: PaperAccount, symbol: str) -> float | None:
    """The symbol's price: the quote's first positive display price
    (``DISPLAY_PRICE_KEYS``), else the 1m frame's last close, else the
    account's last price for it."""
    price = first_float(data.get_quote(symbol), *DISPLAY_PRICE_KEYS, positive=True)
    if price is not None:
        return price
    try:
        frame = data.get_merged(symbol, with_indicators=False)
        if frame is not None and not frame.empty:
            return float(frame.iloc[-1].close)
    except Exception:
        LOG.debug(
            "Failed to read merged frame last price for %s; falling back to cached/account.",
            symbol, exc_info=True,
        )
    cached = account.last_prices.get(symbol)
    return None if cached is None else float(cached)


def structure_event_label(ms_ctx: Any) -> str:
    """The market structure's most recent CHOCH / BOS event ("CHOCH↑",
    "BOS↓", ...; a CHOCH wins a tie), or "—" when there is none."""
    if ms_ctx is None:
        return "—"
    candidates: list[tuple[int, int, str]] = []
    choch_up_age = getattr(ms_ctx, "choch_up_age_bars", None)
    choch_down_age = getattr(ms_ctx, "choch_down_age_bars", None)
    bos_up_age = getattr(ms_ctx, "bos_up_age_bars", None)
    bos_down_age = getattr(ms_ctx, "bos_down_age_bars", None)
    if bool(getattr(ms_ctx, "choch_up", False)) and choch_up_age is not None:
        candidates.append((int(choch_up_age), 0, "CHOCH↑"))
    if bool(getattr(ms_ctx, "choch_down", False)) and choch_down_age is not None:
        candidates.append((int(choch_down_age), 0, "CHOCH↓"))
    if bool(getattr(ms_ctx, "bos_up", False)) and bos_up_age is not None:
        candidates.append((int(bos_up_age), 1, "BOS↑"))
    if bool(getattr(ms_ctx, "bos_down", False)) and bos_down_age is not None:
        candidates.append((int(bos_down_age), 1, "BOS↓"))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        return candidates[0][2]
    return "—"


def _htf_trend(data: MarketDataStore, strategy: BaseStrategy, symbol: str) -> dict[str, Any]:
    """The generic trend row of the strategy's stored HTF frame, built once
    per stored frame (``derive_from_htf_frame``): the frame changes once a
    bar, at its refresh, and the row reads nothing else."""
    tf = strategy.htf_minutes()
    row = data.derive_from_htf_frame(symbol, timeframe_minutes=tf, slot="sr_snapshot.htf_trend",
                                     build=lambda frame: _htf_trend_row(frame, tf))
    return dict(row)


def _htf_trend_row(frame: pd.DataFrame | None, tf: int) -> dict[str, Any]:
    summary = summarize_htf_trend(
        frame,
        min_bars=20,
        vwap_distance_pct=0.0010,
        ema_gap_pct=0.0008,
        min_ret3=0.0010,
        range_vwap_distance_pct=0.0020,
        range_ema_gap_pct=0.0010,
    )
    return {
        "label": str(summary.get("label", "—")),
        "state": str(summary.get("state", "neutral")),
        "vwap_dist": float(summary.get("vwap_dist", 0.0) or 0.0),
        "ema_gap": float(summary.get("ema_gap", 0.0) or 0.0),
        "ret3": float(summary.get("ret3", 0.0) or 0.0),
        "timeframe": f"{tf}m",
    }


def sr_snapshot(
    config: BotConfig,
    data: MarketDataStore,
    symbol: str,
    *,
    price: float | None,
    strategy: BaseStrategy,
    account: PaperAccount,
) -> dict[str, Any] | None:
    """``symbol``'s S/R snapshot at ``price`` (``symbol_price`` when None) on
    the strategy's HTF frame (``strategy.htf_minutes()``), or None when
    support_resistance is off or the feed has no context for it. It reads
    only what the feed holds: the engine refreshes the HTF frames
    (``IntradayBot._refresh_htf_frames``). Until 2026-09-28 the dashboard's
    reads passed ``allow_refresh`` and fetched a symbol whose HTF bar had
    closed, one at a time."""
    cfg = config.support_resistance
    if not bool(cfg.enabled):
        return None
    current_price = price if price is not None else symbol_price(data, account, symbol)
    # mode="trading" gives the sidebar the same flip-confirmation
    # strictness (support_resistance.flip_confirmation_bars()) that position management,
    # the chart's zone-flip detection, and the entry gatekeeper all
    # use. A single "trading" mode means the sidebar / chart /
    # gatekeeper / strategy agree on which side of a level price is
    # currently sitting on — no path where one consumer sees a level
    # as broken before another does.
    ctx = data.get_support_resistance(
        symbol,
        current_price=current_price,
        flip_frame=data.get_merged(symbol, with_indicators=False),
        mode="trading",
        timeframe_minutes=strategy.htf_minutes(),
    )
    if ctx is None:
        return None
    display_price: float | None = None
    candidate_price = current_price if current_price is not None else getattr(ctx, "current_price", None)
    if candidate_price is not None and float(candidate_price) > 0:
        display_price = float(candidate_price)
    state = "neutral"

    def _level_price(level: Any) -> float | None:
        return None if level is None else float(level.price)

    trend_row = _htf_trend(data, strategy, symbol)
    htf_trend_bias = "neutral"
    # The strategy's own HTF trend -- the read its gates and scores use --
    # when it has one; the generic 50/200 read below only for the rest.
    own_trend = None
    own_trend_hook = getattr(strategy, "dashboard_htf_trend", None)
    trend_price = display_price if display_price is not None else float(getattr(ctx, "current_price", 0.0) or 0.0)
    if callable(own_trend_hook) and trend_price:
        try:
            own_trend = own_trend_hook(symbol, data, trend_price)
        except Exception:
            LOG.debug("Failed to read the strategy's HTF trend for %s; using the generic read.", symbol, exc_info=True)
    try:
        if own_trend is None:
            # The strategy lists this request (htf_context_requests), so the
            # engine builds its context at a fixed point of the cycle.
            htf_ctx = data.get_htf_context(
                symbol,
                **strategy.generic_htf_trend_request(),
                **strategy.htf_fvg_request(),
            )
            htf_trend_bias = str(getattr(htf_ctx, "trend_bias", "neutral") or "neutral").strip().lower()
    except Exception:
        LOG.debug("Failed to read HTF trend bias context for %s; falling back to summarize_htf_trend().", symbol, exc_info=True)
    trend_state = str(trend_row.get("state", "neutral") or "neutral").strip().lower()
    trend_label = str(trend_row.get("label", "—") or "—")
    if own_trend is not None:
        trend_state = str(own_trend.get("state", "neutral") or "neutral").strip().lower()
        trend_label = str(own_trend.get("label", "—") or "—")
    elif htf_trend_bias in {"bullish", "bearish"}:
        trend_state = htf_trend_bias
        trend_label = "Bullish" if htf_trend_bias == "bullish" else "Bearish"
    ms_ctx = getattr(ctx, "market_structure", None)
    structure_bias = str(getattr(ms_ctx, "bias", "neutral") or "neutral") if ms_ctx is not None else "neutral"
    structure_event = structure_event_label(ms_ctx)

    bullish_structure = structure_bias == "bullish" or structure_event in {"BOS↑", "CHOCH↑"}
    bearish_structure = structure_bias == "bearish" or structure_event in {"BOS↓", "CHOCH↓"}
    bullish_conflict = bearish_structure or trend_state == "bearish"
    bearish_conflict = bullish_structure or trend_state == "bullish"

    if ctx.breakout_above_resistance and not bullish_conflict and (bullish_structure or trend_state == "bullish"):
        state = "breakout"
    elif ctx.breakdown_below_support and not bearish_conflict and (bearish_structure or trend_state == "bearish"):
        state = "breakdown"
    elif ctx.near_support and not ctx.near_resistance:
        state = "near_support"
    elif ctx.near_resistance and not ctx.near_support:
        state = "near_resistance"
    elif ctx.near_support and ctx.near_resistance:
        state = "compressed"
    elif ctx.breakout_above_resistance and not bullish_conflict:
        state = "breakout_watch"
    elif ctx.breakdown_below_support and not bearish_conflict:
        state = "breakdown_watch"

    htf_min_active = strategy.htf_minutes()
    timeframe_minutes = int(getattr(ctx, "timeframe_minutes", htf_min_active) or htf_min_active)
    symbol_key = str(symbol or "").upper().strip()
    htf_refresh = data.last_htf_refresh.get((symbol_key, timeframe_minutes)) if symbol_key else None

    return {
        "symbol": symbol,
        "timeframe": f"{timeframe_minutes}m",
        "price": display_price,
        "htf_refresh_token": htf_refresh.isoformat() if htf_refresh is not None else None,
        "side_tolerance": safe_float(getattr(ctx, "side_tolerance", None)),
        # The strategy's own levels, as it reads them (2026-09-23): the
        # nearest support / resistance and their distances are ctx's, the
        # ladders are ctx's (nearest first), and broken / pending levels
        # travel in their own fields for the chart to draw as their own
        # zones. Until then the row folded broken_resistance into the
        # support ladder (broken_support into the resistance one) and
        # published the NEAREST price of the result, while ctx keeps the
        # STRONGEST member of each side_tolerance group: in 11% of
        # archived samples the sidebar and top_tier's support zone showed
        # another level than sr_ctx.nearest_support (AAPL 2026-09-22 10:05:
        # 338.58 drawn, 338.42 used by the strategy), next to a distance
        # measured to the strategy's level.
        "nearest_support": _level_price(ctx.nearest_support),
        "nearest_resistance": _level_price(ctx.nearest_resistance),
        "support_distance_pct": ctx.support_distance_pct,
        "resistance_distance_pct": ctx.resistance_distance_pct,
        "support_distance_atr": ctx.support_distance_atr,
        "resistance_distance_atr": ctx.resistance_distance_atr,
        "breakout_above_resistance": bool(ctx.breakout_above_resistance),
        "breakdown_below_support": bool(ctx.breakdown_below_support),
        "near_support": bool(ctx.near_support),
        "near_resistance": bool(ctx.near_resistance),
        "regime_hint": str(ctx.regime_hint),
        "trend": trend_label,
        "trend_state": trend_state,
        "structure_bias": structure_bias,
        "structure_event": structure_event,
        "structure_last_high_label": getattr(ms_ctx, "last_high_label", None) if ms_ctx is not None else None,
        "structure_last_low_label": getattr(ms_ctx, "last_low_label", None) if ms_ctx is not None else None,
        "bias_score": float(ctx.bias_score),
        "state": state,
        "supports": [float(level.price) for level in ctx.supports],
        "resistances": [float(level.price) for level in ctx.resistances],
        "broken_support": _level_price(ctx.broken_support),
        "broken_resistance": _level_price(ctx.broken_resistance),
        "pending_support": _level_price(ctx.pending_support),
        "pending_resistance": _level_price(ctx.pending_resistance),
    }
