# SPDX-License-Identifier: MIT
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, time
from typing import Any

import pandas as pd

from ...models import ASSET_TYPE_OPTION_VERTICAL, Candidate, Position, Side, Signal, asset_type_of
from ...options_mode import (
    OptionContract,
    build_position_label,
    build_vertical_order,
    choose_by_delta,
    choose_nearest_strike,
    clamp_long_premium_levels,
    clamp_short_premium_levels,
    net_credit_dollars,
    net_debit_dollars,
    vertical_limit_price,
)
from ...bars import opening_range, rth_open_plus, session_open_price
from ...sessions import EQUITY_RTH_OPEN, equity_session_state, is_time_in_window, parse_hhmm
from ...numeric import first_float, safe_float
from ...reasons import (
    bool_token,
    detail_fields,
    fmt_metric,
    insufficient_bars_reason,
    reason_with_values,
)
from ... import sessions
from ..shared_entry import AdmittedEntry, EntryProposal
from ..strategy_base import BaseStrategy
from .chain import OptionChainMixin
from .regime import RegimeMixin


def _style_unavailable_reason(style: str, detail: str, **fields: Any) -> str:
    """Standard format for a per-style 'unavailable' skip reason:
    ``{style}_unavailable({detail},k=v,...)``."""
    detail = str(detail or "").strip() or "unknown"
    extra = detail_fields(**fields)
    inner = f"{detail},{extra}" if extra else detail
    return f"{style}_unavailable({inner})"


def _no_style_trigger_reason(
    *,
    regime_name: str,
    bullish: bool,
    bearish: bool,
    rangeish: bool,
    orb_enabled: bool,
    orb_window: bool,
    trend_enabled: bool,
    trend_window: bool,
    credit_enabled: bool,
    credit_window: bool,
    last_close: Any,
    last_vwap: Any,
    last_ret5: Any,
    trend_min_ret5: Any,
    or_high: Any,
    or_low: Any,
    orb_buffer_pct: Any,
) -> str:
    """Standard 'no style trigger fired' skip reason for the
    multi-style regime pipeline. Renders the salient context fields
    so post-hoc analysis can reconstruct why none of the styles fired.

    Field names match the legacy BaseStrategy._no_style_trigger_reason
    output exactly so log-parsing tools and existing dashboard chips
    keep working."""
    high, low, buffer_pct = safe_float(or_high), safe_float(or_low), safe_float(orb_buffer_pct)
    bull_trigger = None if high is None or buffer_pct is None else high * (1.0 + buffer_pct)
    bear_trigger = None if low is None or buffer_pct is None else low * (1.0 - buffer_pct)
    return (
        "no_style_trigger("
        f"regime={regime_name},"
        f"bullish={bool_token(bullish)},"
        f"bearish={bool_token(bearish)},"
        f"rangeish={bool_token(rangeish)},"
        f"orb_enabled={bool_token(orb_enabled)},"
        f"orb_window={bool_token(orb_window)},"
        f"trend_enabled={bool_token(trend_enabled)},"
        f"trend_window={bool_token(trend_window)},"
        f"credit_enabled={bool_token(credit_enabled)},"
        f"credit_window={bool_token(credit_window)},"
        f"close={fmt_metric(last_close, 4)},"
        f"vwap={fmt_metric(last_vwap, 4)},"
        f"ret5={fmt_metric(last_ret5, 4)},"
        f"required_ret5>={fmt_metric(trend_min_ret5, 4)},"
        f"required_bear_ret5<={fmt_metric(-safe_float(trend_min_ret5, 0.0), 4)},"
        f"or_high={fmt_metric(or_high, 4)},"
        f"or_low={fmt_metric(or_low, 4)},"
        f"orb_bull_trigger>{fmt_metric(bull_trigger, 4)},"
        f"orb_bear_trigger<{fmt_metric(bear_trigger, 4)},"
        f"orb_buffer_pct={fmt_metric(orb_buffer_pct, 4)}"
        ")"
    )


_STYLE_KINDS = frozenset({"orb", "trend", "credit"})


@dataclass(frozen=True)
class _EntryStyle:
    """One row of a 0DTE strategy's style table (``_entry_styles``).

    ``name`` is the ``options.styles`` token and the signal's style.
    ``kind`` sets the style's window (``<kind>_start_time`` to
    ``<kind>_end_time``), the regime it trades and its trigger: ``orb`` a
    trend regime breaking the opening range, ``trend`` a trend regime's
    5-bar return, ``credit`` the range regime, on the side of VWAP the
    close is. ``build`` makes the signal; ``pending_reasons``, when set, is
    the style's own blockers, ``(bullish, frame, regime) -> list``, which
    ride on its premium proposal."""

    name: str
    kind: str
    build: Callable[..., Signal | None]
    pending_reasons: Callable[[bool, pd.DataFrame, dict[str, Any]], list[str]] | None = None

    def __post_init__(self) -> None:
        if self.kind not in _STYLE_KINDS:
            raise ValueError(f"0DTE style {self.name!r} has kind {self.kind!r}, not one of {sorted(_STYLE_KINDS)}")


class ZeroDteEtfOptionsStrategy(OptionChainMixin, RegimeMixin, BaseStrategy):
    """0DTE ETF verticals routed by the underlying's regime.

    ``_regime_confirm`` classifies the underlying (bullish / bearish trend,
    range) and vetoes on its own terms (VIX, chop, index disagreement, the
    HTF trend and HTF structure bias). Each style that triggers -- ORB debit,
    trend debit, midday credit -- then hands a PREMIUM proposal to
    ``self.entry_policy.admit`` before any chain work
    (``_admit_premium_entry``): the underlying's frame, its MARKET direction
    (a bull put credit spread is LONG), no price stop / target. Every
    shared_entry veto and score term is applied there (the manifest exempts
    ``midday_credit_spread`` from the structure veto: its range regime was
    never structure-gated); the builder picks the contracts and ``emit``
    builds the signal on the order side with the premium stop / target.

    ``entry_signals`` is the family's one entry loop: the long-options
    subclass swaps the style table (``_entry_styles``) and its builder, and
    turns the chain prefetch off. Until 2026-09-27 it carried its own copy
    of the loop, and fixes reached one copy only. The regime
    (``regime.RegimeMixin``) and the chain and quote plumbing
    (``chain.OptionChainMixin``) live beside this module.
    """

    strategy_name = 'zero_dte_etf_options'
    time_params = ("no_new_entries_after", "orb_start_time", "orb_end_time", "orb_opening_window_start",
                   "orb_opening_window_end", "trend_start_time", "trend_end_time", "credit_start_time",
                   "credit_end_time")
    # The 0DTE family: an underlying either strategy holds is open for both,
    # and either one marks the other's vertical. No current path puts the
    # other strategy's position beside this one's: the engine runs one
    # strategy, option positions are not restored at startup, and a
    # restored position takes the active strategy's name. Until 2026-09-27
    # the base named its subclass in two literals, and a classmethod call on
    # the subclass saw only the subclass, so the long options ignored the
    # spreads' positions.
    _OPTION_FAMILY = frozenset({"zero_dte_etf_options", "zero_dte_etf_long_options"})
    # entry_signals warms every candidate's chain in parallel before the
    # build loop (_prefetch_option_chains).
    _PREFETCH_OPTION_CHAINS = True

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        return max(0, int(self.params.get("min_bars", 40) or 40))

    def __init__(self, config):
        super().__init__(config)
        self.optcfg = config.options
        self.force_flat_time = parse_hhmm(self.optcfg.force_flatten_time)
        # The chain cache (chain.OptionChainMixin): (symbol, date) -> when it
        # was read and the chain.
        self._option_chain_cache: dict[tuple[str, str], tuple[datetime, list[OptionContract]]] = {}
        # Symbol -> when its last option_chains read failed. The read is not
        # retried until option_chain_cache_seconds has passed, the same pace
        # as a successful one, so a failing chain costs no extra Schwab calls.
        self._option_chain_read_failed_at: dict[str, datetime] = {}
        # A prefetch read that raised anything else is logged with its
        # traceback at WARNING at most once a minute per symbol.
        self._option_chain_prefetch_failures = self._option_chain_failure_log()
        self._underlying_atr_cache: dict[str, float] = {}
        self._underlying_ref_atr_cache: dict[str, float] = {}
        # pandas' between_time read a reversed opening window as the bars
        # OUTSIDE it, an opening range of premarket and afternoon prints.
        start, minutes = self._opening_window()
        if minutes < 1:
            raise ValueError(
                f"strategies.{self.strategy_name}.params.orb_opening_window_end must not be before "
                f"orb_opening_window_start ({start.strftime('%H:%M')}), got {self.params.get('orb_opening_window_end')!r}"
            )

    def _options_enabled(self) -> bool:
        return bool(self.optcfg.enabled)

    def _style_enabled(self, style: str) -> bool:
        allowed = {str(s).strip() for s in (self.optcfg.styles or []) if str(s).strip()}
        return style in allowed

    def _option_entry_block_reason(self, now_dt=None) -> str | None:
        return self._event_calendar.entry_block_reason(now_dt=now_dt)

    @classmethod
    def _underlying_already_open(cls, symbol: str, positions: dict[str, Position]) -> bool:
        for p in positions.values():
            if p.strategy not in cls._OPTION_FAMILY:
                continue
            if str(p.metadata.get("underlying") or p.symbol) == symbol:
                return True
        return False

    def _compute_time_decay_scale(self) -> float:
        """Returns 1.0 at/before decay_start, min_scale at/after decay_end,
        linear interpolation between. Used to scale debit/single target and
        stop multipliers as theta decay accelerates through the 0DTE session."""
        if not self.optcfg.debit_target_time_decay_enabled:
            return 1.0
        now_t = sessions.now_et().time()
        start = parse_hhmm(self.optcfg.debit_target_time_decay_start)
        end = parse_hhmm(self.optcfg.debit_target_time_decay_end)
        min_scale = max(0.10, float(getattr(self.optcfg, "debit_target_time_decay_min_scale", 0.70)))
        if now_t <= start:
            return 1.0
        if now_t >= end:
            return min_scale
        start_m = start.hour * 60 + start.minute
        end_m = end.hour * 60 + end.minute
        now_m = now_t.hour * 60 + now_t.minute
        progress = (now_m - start_m) / max(1, end_m - start_m)
        return 1.0 - progress * (1.0 - min_scale)

    def _time_adjusted_delta(self, base_delta: float) -> float:
        """Shift delta higher (more ITM) as session progresses to reduce theta
        exposure on 0DTE contracts. NOT applied to credit short deltas."""
        if not self.optcfg.delta_time_shift_enabled:
            return base_delta
        now_t = sessions.now_et().time()
        shift_start = parse_hhmm(self.optcfg.delta_time_shift_start)
        if now_t <= shift_start:
            return base_delta
        shift_per_hour = float(getattr(self.optcfg, "delta_time_shift_per_hour", 0.025))
        shift_max = float(getattr(self.optcfg, "delta_time_shift_max", 0.15))
        start_m = shift_start.hour * 60 + shift_start.minute
        now_m = now_t.hour * 60 + now_t.minute
        hours_elapsed = (now_m - start_m) / 60.0
        shift = min(shift_max, shift_per_hour * hours_elapsed)
        return base_delta + shift

    def _adaptive_strike_width(self, underlying: str, base_width: float) -> float:
        """Scale strike width with current ATR vs trailing median to adapt
        to volatility. Wider on high-vol days (more premium), tighter on
        quiet. Snapped to the chain's strike increment (whole dollars for
        SPY/QQQ/IWM 0DTE) so hedge_target always lands on a real strike
        — the 2026-05-20 session logged 183 ``no_hedge_leg`` skips with
        fractional target_hedge_strike values (732.5, 701.5, 702.5)
        because the previous $0.50 rounding produced strikes that don't
        exist on the chain. Rounding to whole dollars matches the
        ``strike_width_by_symbol`` defaults (all integer) and the actual
        Schwab chain grid for these ETFs."""
        if not self.optcfg.adaptive_width_enabled:
            return base_width
        current_atr = getattr(self, "_underlying_atr_cache", {}).get(underlying)
        ref_atr = getattr(self, "_underlying_ref_atr_cache", {}).get(underlying)
        if current_atr is None or ref_atr is None or ref_atr <= 0:
            return base_width
        max_scale = float(getattr(self.optcfg, "adaptive_width_max_scale", 1.5))
        scale = max(1.0, min(max_scale, current_atr / ref_atr))
        scaled = base_width * scale
        # Round to whole dollars but never collapse below base_width —
        # banker's rounding on 2.5 returns 2, which would be a no-op
        # scale and silently disable adaptive widening. round-up via
        # +0.5 ensures the gate-up step happens at the intended scale.
        snapped = float(int(scaled + 0.5))
        return max(snapped, float(base_width))

    def dashboard_htf_trend(self, symbol: str, data, price: float) -> dict[str, str] | None:
        """The HTF trend the entry gate reads: ``_htf_trend_context``
        (``summarize_htf_trend`` on the continuous ema9_all / ema20_all)."""
        summary = self._htf_trend_context(symbol, data)
        if not bool(summary.get("available")):
            return {"state": "neutral", "label": "—"}
        return {"state": str(summary.get("state", "neutral")), "label": str(summary.get("label", "—"))}

    def dashboard_htf_ema_columns(self) -> tuple[str, str] | None:
        """``summarize_htf_trend`` reads the continuous (all-hours) EMAs, not
        the session-reset ema9 / ema20 the chart drew until 2026-09-24."""
        return ("ema9_all", "ema20_all")

    @staticmethod
    def live_activity_score(frame: pd.DataFrame | None) -> float:
        """Self-normalizing "is the tape live right now?" score for 0DTE.

        Returns a unitless multiplier where 1.0 = neutral (normal pace
        for this symbol's last 20 bars), > 1.0 = elevated, < 1.0 = quiet.

        Replaces TradingView's ``relative_volume_10d_calc`` which is
        session-cumulative and structurally low all morning for SPY/QQQ
        — making the legacy ``trend_rvol`` / ``credit_rvol`` thresholds
        effectively unreachable for benchmark ETFs before 14:00. This
        score is computed from streamed bars and:

          * 60% volume momentum: sum(last 5 bars) / (sum(prior 15 bars) / 3)
            — recent vs prior averaged-per-5-bars, catches in-session
            volume surges without baseline calibration.
          * 40% ATR expansion: current atr14 / median(last 20 bars atr14)
            — catches volatility expansion even when volume alone might
            lie (illiquid spikes, etc).

        Returns 1.0 (neutral) when the frame is None/empty/insufficient
        or when columns are missing — fails open, doesn't crash entry. A
        frame whose columns do not read raises: until 2026-09-26 that read
        as the neutral 1.0 too, which ``min_activity_for_entry`` passes."""
        if frame is None or frame.empty or len(frame) < 20:
            return 1.0
        if "volume" in frame.columns:
            recent_vol = float(frame.tail(5)["volume"].sum())
            prior_vol = float(frame.iloc[-20:-5]["volume"].sum())
            vol_momentum = recent_vol / max(prior_vol / 3.0, 1.0)
        else:
            vol_momentum = 1.0
        if "atr14" in frame.columns:
            atr_tail = frame["atr14"].dropna().tail(20)
            atr_current = float(atr_tail.iloc[-1]) if len(atr_tail) > 0 else 0.0
            atr_baseline = float(atr_tail.median()) if len(atr_tail) >= 5 else 0.0
            atr_expansion = atr_current / max(atr_baseline, 0.01) if atr_baseline > 0 else 1.0
        else:
            atr_expansion = 1.0
        return 0.6 * vol_momentum + 0.4 * atr_expansion

    def dashboard_directional_bias(self, frame: pd.DataFrame | None) -> Side | None:
        """Quick directional read for the dashboard candidate tile.

        Returns ``Side.LONG`` / ``Side.SHORT`` when the underlying's
        VWAP distance, EMA9-vs-EMA20 gap, and session day-return all
        agree on a direction; returns None (neutral) when they
        disagree or the frame is insufficient.

        This is a *display* heuristic — the strategy's actual entry
        decision (``_regime_confirm``) runs additional gates (HTF
        alignment, index confirmation, regime score, SR context,
        candle bias, IV rank, etc.) before committing to a side. The
        candidate tile color reflects the dominant tape lean so the
        trader can see which side the strategy is biased toward
        without waiting for an entry-window cycle to complete. A
        tile that shows LONG here may still be skipped at entry if
        e.g. SPX disagrees with SPY — neutral here means "no strong
        lean," not "would never enter".

        Thresholds reuse the strategy's own ``trend_vwap_distance_pct``
        / ``trend_ema_gap_pct`` so the dashboard lean tracks the same
        signal strengths the strategy uses internally.
        """
        if frame is None or frame.empty or len(frame) < 5:
            return None
        last = frame.iloc[-1]
        close = safe_float(last.get("close"), 0.0)
        if close <= 0:
            return None
        vwap = safe_float(last.get("vwap"), close)
        ema9 = safe_float(last.get("ema9"), close)
        ema20 = safe_float(last.get("ema20"), close)
        vwap_dist = (close - vwap) / close
        ema_gap = (ema9 - ema20) / close
        p = self.params
        vwap_thresh = float(p.get("trend_vwap_distance_pct", 0.0016))
        ema_thresh = float(p.get("trend_ema_gap_pct", 0.00075))
        session_day = sessions.now_et().date()
        u_open = session_open_price(frame, session_day, fallback_to_premarket_on_nan=True)
        day_ret = ((close / u_open) - 1.0) if u_open and u_open > 0 else 0.0
        if vwap_dist >= vwap_thresh and ema_gap >= ema_thresh and day_ret > 0:
            return Side.LONG
        if vwap_dist <= -vwap_thresh and ema_gap <= -ema_thresh and day_ret < 0:
            return Side.SHORT
        return None

    @staticmethod
    def dashboard_change_from_open(frame: pd.DataFrame | None) -> float | None:
        """Live session-relative day return as a PERCENT for the
        dashboard candidate / watchlist Day% display.

        Returns e.g. ``1.23`` for +1.23% — matches the unit produced by
        TradingView's ``change_from_open`` field (which equity screeners
        still surface via candidate metadata) and the Schwab quote's
        ``netPercentChangeInDouble`` / ``percentChange`` field that the
        dashboard's fallback chain prefers. Returning a percent (not a
        0..1 ratio) lets the dashboard concatenate ``%`` without unit
        translation.

        ``DashboardCache.build_payload`` reads this via the same duck-typed
        ``getattr`` dispatch used for ``live_activity_score`` and
        ``dashboard_directional_bias``; strategies that don't define
        the hook fall through to the candidate's existing
        ``change_from_open`` metadata field. Used by the 0DTE
        strategies because their local-synthesis screener doesn't
        populate that field at screen time — the live Schwab quote's
        ``percent_change`` is the primary, but it can be temporarily
        missing during the gap between cycle quote refreshes and
        stream ticks; this resolver gives the dashboard a tape-truth
        fallback computed from the same ``session_open_price`` helper
        ``_regime_confirm`` uses internally.

        Returns None when the frame is missing/empty or the session
        open is unresolvable.
        """
        if frame is None or frame.empty:
            return None
        session_day = sessions.now_et().date()
        u_open = session_open_price(frame, session_day, fallback_to_premarket_on_nan=True)
        if not u_open or u_open <= 0:
            return None
        close = safe_float(frame.iloc[-1].get("close"), 0.0)
        if close <= 0:
            return None
        return ((close / u_open) - 1.0) * 100.0

    def _underlying_below_min_price(self, underlying: str, style: str, last_underlying: float) -> bool:
        """``options.min_underlying_price``, which the README documents as an
        option-universe filter and every preset set, was read by nothing
        until 2026-09-23. Checked before the chain is fetched."""
        floor = float(self.optcfg.min_underlying_price)
        if float(last_underlying) >= floor:
            return False
        self._set_build_failure(
            underlying, style,
            reason_with_values("underlying_below_min_price", current=last_underlying, required=floor, op=">=", digits=2),
        )
        return True

    @staticmethod
    def _option_strategy_score(candidate: Candidate, regime: dict[str, Any], primary_key: str) -> float:
        """The option signal's own priority -- emit's ``strategy_score``,
        which the manifest ranks on (``strategy_priority_score``): activity
        plus the score of the regime the style trades (``primary_key``:
        bullish_trend / bearish_trend / range) and its margin over the
        runner-up. It was stamped over the built signal's
        final_priority_score until 2026-09-24; emit now adds the shared
        terms to it for that stamp, and they never rank these strategies."""
        scores = regime.get("scores") if isinstance(regime, dict) else None
        if not isinstance(scores, dict):
            scores = {}
        primary = safe_float(scores.get(primary_key), 0.0)
        alternatives = [
            safe_float(scores.get("bullish_trend"), 0.0),
            safe_float(scores.get("bearish_trend"), 0.0),
            safe_float(scores.get("range"), 0.0),
        ]
        alternatives.sort(reverse=True)
        runner_up = alternatives[1] if len(alternatives) > 1 else 0.0
        margin = max(0.0, primary - runner_up)
        base = safe_float(candidate.activity_score, 0.0)
        return round(base + (primary * 100.0) + (margin * 40.0), 4)

    def _admit_premium_entry(
        self,
        candidate: Candidate,
        bullish: bool,
        frame: pd.DataFrame,
        data,
        last_underlying: float,
        style: str,
        family: str,
        regime: dict[str, Any],
        *,
        pending_reasons: tuple[str, ...] | list[str] = (),
    ) -> AdmittedEntry | None:
        """An option style's pass through the shared entry stage, BEFORE any
        chain work: a premium proposal (no price stop / target; the premium
        levels are set once the contracts are picked and go to ``emit``) on
        the underlying's frame, in the underlying's MARKET direction -- a
        bull put credit spread is sold but is a LONG proposal -- so every
        veto and score term reads the side the trade needs the underlying to
        go. ``pending_reasons`` are the style's own blockers. A refusal is
        recorded under ``style``, every blocker in it, for
        ``_consume_style_failure``."""
        return self.entry_policy.admit(EntryProposal(
            candidate=candidate,
            direction=Side.LONG if bullish else Side.SHORT,
            style=style,
            style_family=family,
            close=float(last_underlying),
            stop=None,
            target=None,
            gate_frame=frame,
            sr_frame=frame,
            level_frame=None,
            data=data,
            pending_reasons=tuple(pending_reasons),
            contexts=regime.get("entry_contexts") if isinstance(regime, dict) else None,
        ))

    def _consume_style_failure(self, symbol: str, style: str) -> list[str]:
        """Every blocker a style's build recorded -- the shared stage's
        refusal lists all of them, the first primary -- or
        ``<style>_unavailable`` when it recorded none."""
        payload = self._consume_build_failure_payload(symbol, style)
        return list(payload["reasons"]) if payload and payload.get("reasons") else [f"{style}_unavailable"]

    def _build_debit_spread_signal(self, candidate: Candidate, bullish: bool, client, data, frame: pd.DataFrame, last_underlying: float, style: str, confirm_index: str | None, regime: dict[str, Any], *, pending_reasons: tuple[str, ...] | list[str] = ()) -> Signal | None:
        underlying = candidate.symbol
        admitted = self._admit_premium_entry(candidate, bullish, frame, data, last_underlying, style, "option_debit", regime,
                                             pending_reasons=pending_reasons)
        if admitted is None:
            return None
        if self._underlying_below_min_price(underlying, style, last_underlying):
            return None
        put_call = "CALL" if bullish else "PUT"
        contracts = self._fetch_filtered_contracts(client, underlying, put_call)
        if contracts is None:
            self._set_build_failure(underlying, style, "option_chain_unavailable")
            return None
        if not contracts:
            self._set_build_failure(underlying, style, "option_chain_empty")
            return None
        base_delta = float(self.optcfg.target_long_delta)
        adjusted_delta = self._time_adjusted_delta(base_delta)
        long_leg = choose_by_delta(contracts, adjusted_delta)
        if long_leg is None:
            self._set_build_failure(underlying, style, "no_long_leg_near_target_delta")
            return None
        base_width = float(self.optcfg.strike_width_by_symbol.get(underlying, 2.0))
        width = self._adaptive_strike_width(underlying, base_width)
        target_strike = long_leg.strike + width if bullish else long_leg.strike - width
        short_leg = choose_nearest_strike(contracts, target_strike, "higher" if bullish else "lower")
        if short_leg is None:
            self._set_build_failure(underlying, style, "no_hedge_leg_at_width")
            return None
        entry_debit = net_debit_dollars(long_leg, short_leg)
        if entry_debit <= 0:
            self._set_build_failure(underlying, style, "non_positive_entry_debit")
            return None
        stable, instability = self._stabilize_quotes(data, (long_leg, short_leg), validate=self._validate_spread_market,
                                                     failure_detail=self._spread_market_failure_detail,
                                                     source="strategies:option_quote_stability_spread")
        if stable is None:
            self._set_build_failure(underlying, style, f"quote_not_stable({instability})")
            return None
        long_leg, short_leg = stable
        market = self._validate_spread_market(long_leg, short_leg)
        if market is None:
            self._set_build_failure(underlying, style, "spread_market_invalid")
            return None
        nat_bid, nat_ask, quoted_mid = market
        entry_limit = vertical_limit_price(long_leg, short_leg, mode=self.optcfg.vertical_limit_mode,
                                           spread_side=Side.LONG, opening=True)
        entry_value = entry_limit * 100.0
        # Guard: debit stop must be below entry, target must be above entry
        debit_stop_frac = max(0.01, min(0.99, float(self.optcfg.debit_stop_frac)))
        debit_target_mult = max(1.01, float(self.optcfg.debit_target_mult))
        time_decay_scale = self._compute_time_decay_scale()
        if time_decay_scale < 1.0:
            debit_target_mult = max(1.01, 1.0 + (debit_target_mult - 1.0) * time_decay_scale)
            widen = self.optcfg.debit_stop_time_decay_widen_factor
            debit_stop_frac = max(0.01, min(0.99, debit_stop_frac * (1.0 + (1.0 - time_decay_scale) * widen)))
        stop = entry_value * debit_stop_frac
        target = entry_value * debit_target_mult
        stop, target = clamp_long_premium_levels(entry_value, stop, target)
        position_key = build_position_label(underlying, style, Side.LONG, long_leg, short_leg)
        width_dollars = abs(float(short_leg.strike) - float(long_leg.strike)) * 100.0
        breakeven_underlying = float(long_leg.strike) + entry_limit if bullish else float(long_leg.strike) - entry_limit
        metadata = {
            "asset_type": ASSET_TYPE_OPTION_VERTICAL,
            "position_key": position_key,
            "underlying": underlying,
            "confirm_index": confirm_index,
            "style": style,
            "regime": regime.get("regime"),
            "regime_scores": regime.get("scores"),
            "regime_metrics": regime.get("metrics"),
            "spread_side": Side.LONG.value,
            "spread_style": "DEBIT",
            "spread_type": "bull_call_debit" if bullish else "bear_put_debit",
            "direction": "bullish" if bullish else "bearish",
            "option_type": put_call,
            "entry_price": entry_value,
            "entry_price_points": entry_limit,
            "time_decay_scale": round(time_decay_scale, 4),
            "mark_price_hint": quoted_mid * 100.0,
            "max_loss_per_contract": entry_value,
            "max_profit_per_contract": max(0.0, width_dollars - entry_value),
            "strike_width_dollars": width_dollars,
            "breakeven_underlying": breakeven_underlying,
            "limit_price": entry_limit,
            "natural_bid": nat_bid * 100.0,
            "natural_ask": nat_ask * 100.0,
            "underlying_entry": last_underlying,
            "valuation_legs": [long_leg.symbol, short_leg.symbol],
            "long_leg_symbol": long_leg.symbol,
            "short_leg_symbol": short_leg.symbol,
            "long_strike": float(long_leg.strike),
            "short_strike": float(short_leg.strike),
            "bought_leg_symbol": long_leg.symbol,
            "sold_leg_symbol": short_leg.symbol,
            "bought_strike": float(long_leg.strike),
            "sold_strike": float(short_leg.strike),
            "long_leg": asdict(long_leg),
            "short_leg": asdict(short_leg),
            "order_spec": build_vertical_order(long_leg, short_leg, Side.LONG, qty=1, limit_price=entry_limit),
        }
        # A debit spread is bought whichever way it points: the order side is
        # LONG, the proposal's (the underlying's) direction bullish / bearish.
        return self.entry_policy.emit(
            admitted,
            reason=f"{style}_{'bull' if bullish else 'bear'}",
            strategy_score=self._option_strategy_score(candidate, regime, "bullish_trend" if bullish else "bearish_trend"),
            management={},
            target=target,
            metadata=metadata,
            order_side=Side.LONG,
            reference_symbol=confirm_index,
            premium_stop=stop,
        )

    def _build_credit_spread_signal(self, candidate: Candidate, bullish: bool, client, data, frame: pd.DataFrame, last_underlying: float, style: str, confirm_index: str | None, regime: dict[str, Any], *, pending_reasons: tuple[str, ...] | list[str] = ()) -> Signal | None:
        underlying = candidate.symbol
        admitted = self._admit_premium_entry(candidate, bullish, frame, data, last_underlying, style, "option_credit", regime,
                                             pending_reasons=pending_reasons)
        if admitted is None:
            return None
        if self._underlying_below_min_price(underlying, style, last_underlying):
            return None
        put_call = "PUT" if bullish else "CALL"
        contracts = self._fetch_filtered_contracts(client, underlying, put_call)
        if contracts is None:
            self._set_build_failure(
                underlying, style,
                _style_unavailable_reason(style, "reason=option_chain_unavailable", put_call=put_call),
            )
            return None
        all_contracts = self._get_cached_option_chain(underlying) or []
        if not contracts:
            self._set_build_failure(
                underlying,
                style,
                _style_unavailable_reason(
                    style,
                    "reason=option_chain_empty",
                    put_call=put_call,
                    total_contracts=len(all_contracts),
                    filtered_contracts=0,
                    min_volume=int(self.optcfg.min_option_volume),
                    min_open_interest=int(self.optcfg.min_open_interest),
                    max_bid_ask_spread_pct=float(self.optcfg.max_bid_ask_spread_pct),
                    max_leg_spread_dollars=float(self.optcfg.max_leg_spread_dollars),
                ),
            )
            return None
        short_leg = choose_by_delta(contracts, self.optcfg.target_short_delta)
        if short_leg is None:
            self._set_build_failure(
                underlying,
                style,
                _style_unavailable_reason(
                    style,
                    "reason=no_short_leg",
                    put_call=put_call,
                    filtered_contracts=len(contracts),
                    target_short_delta=float(self.optcfg.target_short_delta),
                ),
            )
            return None
        # Credit strike distance gate — reject if short strike is too close
        # to the underlying price in ATR terms (risk of breach on vol days).
        if self.optcfg.credit_distance_gate_enabled:
            underlying_atr = getattr(self, "_underlying_atr_cache", {}).get(underlying)
            if underlying_atr is not None and underlying_atr > 0:
                distance_atr = abs(float(short_leg.strike) - last_underlying) / underlying_atr
                min_dist = float(getattr(self.optcfg, "min_credit_distance_atr", 1.8))
                if distance_atr < min_dist:
                    self._set_build_failure(
                        underlying, style,
                        _style_unavailable_reason(style, f"reason=short_strike_too_close(distance_atr={distance_atr:.2f}<{min_dist})"))
                    return None
        # Credit pivot-buffer gate (2026-05-21) — reject if the short
        # strike is within ``min_short_strike_pivot_buffer_atr * atr`` of
        # the recent market-structure pivots. The references are the
        # ``msltf_`` / ``mshtf_reference_*`` keys _regime_confirm puts in
        # ``regime['metrics']`` (the same dict the signal stamps as
        # regime_metrics, so a post-mortem sees what the gate saw). The
        # pivot used is the OUTERMOST one: for bear_call
        # max(LTF reference_high, HTF reference_high), and the short must
        # sit at least ``buffer * atr`` ABOVE it; for bull_put
        # min(LTF reference_low, HTF reference_low), and the short must
        # sit at least ``buffer * atr`` BELOW it. Without this gate a
        # short can land essentially AT the recent pivot (observed
        # 2026-05-21: SPY short 741 / pivot 740.62, QQQ short 712 / pivot
        # 711.89) and stop out within 30s on resistance_break_exit /
        # breakdown. The existing credit_distance_gate above measures
        # from current spot, which can pass even when the short is AT
        # the pivot. Skips silently when references or ATR are
        # unavailable (early session, no pivots) — never blocks the
        # build for missing data. Until 2026-09-25 it read the references
        # from the regime's top level, found none and never fired.
        if self.optcfg.credit_pivot_buffer_gate_enabled:
            underlying_atr = getattr(self, "_underlying_atr_cache", {}).get(underlying)
            if underlying_atr is not None and underlying_atr > 0 and math.isfinite(underlying_atr):
                buffer_mult = float(getattr(self.optcfg, "min_short_strike_pivot_buffer_atr", 1.0))
                regime_metrics = regime.get("metrics") or {}
                ref_levels: list[float] = []
                for key in (("mshtf_reference_high", "msltf_reference_high") if not bullish else ("mshtf_reference_low", "msltf_reference_low")):
                    raw = regime_metrics.get(key)
                    if raw is None:
                        continue
                    try:
                        value = float(raw)
                    except (TypeError, ValueError):
                        continue
                    # ``float('nan')`` succeeds silently in Python and NaN
                    # propagates through min/max — ``nan < buffer_mult``
                    # is False, which would fail the gate open. Treat
                    # non-finite refs as missing data (same as None).
                    if not math.isfinite(value):
                        continue
                    ref_levels.append(value)
                if ref_levels:
                    short_strike = float(short_leg.strike)
                    if bullish:
                        nearest_pivot = min(ref_levels)
                        cushion_atr = (nearest_pivot - short_strike) / underlying_atr
                    else:
                        nearest_pivot = max(ref_levels)
                        cushion_atr = (short_strike - nearest_pivot) / underlying_atr
                    if cushion_atr < buffer_mult:
                        self._set_build_failure(
                            underlying, style,
                            _style_unavailable_reason(
                                style,
                                f"reason=short_strike_too_close_to_pivot(cushion_atr={cushion_atr:.2f}<{buffer_mult},short_strike={short_strike:.2f},nearest_pivot={nearest_pivot:.2f},side={'put' if bullish else 'call'})",
                            ),
                        )
                        return None
        base_width = float(self.optcfg.strike_width_by_symbol.get(underlying, 2.0))
        width = self._adaptive_strike_width(underlying, base_width)
        hedge_target = short_leg.strike - width if bullish else short_leg.strike + width
        hedge_direction = "lower" if bullish else "higher"
        long_leg = choose_nearest_strike(contracts, hedge_target, hedge_direction)
        if long_leg is None:
            self._set_build_failure(
                underlying,
                style,
                _style_unavailable_reason(
                    style,
                    "reason=no_hedge_leg",
                    short_leg_symbol=short_leg.symbol,
                    short_strike=float(short_leg.strike),
                    target_hedge_strike=hedge_target,
                    required_width=width,
                    hedge_direction=hedge_direction,
                ),
            )
            return None
        entry_credit, max_loss = net_credit_dollars(short_leg, long_leg)
        if entry_credit <= 0 or max_loss <= 0:
            self._set_build_failure(
                underlying,
                style,
                _style_unavailable_reason(
                    style,
                    "reason=non_positive_credit_or_risk",
                    entry_credit=entry_credit,
                    max_loss=max_loss,
                    short_bid=float(short_leg.bid),
                    short_mid=float(short_leg.mid),
                    long_ask=float(long_leg.ask),
                    long_mid=float(long_leg.mid),
                ),
            )
            return None
        stable, instability = self._stabilize_quotes(data, (short_leg, long_leg), validate=self._validate_spread_market,
                                                     failure_detail=self._spread_market_failure_detail,
                                                     source="strategies:option_quote_stability_spread")
        if stable is None:
            self._set_build_failure(underlying, style, _style_unavailable_reason(style, instability))
            return None
        short_leg, long_leg = stable
        market = self._validate_spread_market(short_leg, long_leg)
        if market is None:
            self._set_build_failure(
                underlying,
                style,
                _style_unavailable_reason(style, self._spread_market_failure_detail(short_leg, long_leg)),
            )
            return None
        nat_bid, nat_ask, quoted_mid = market
        entry_limit = vertical_limit_price(short_leg, long_leg, mode=self.optcfg.vertical_limit_mode,
                                           spread_side=Side.SHORT, opening=True)
        entry_credit_value = entry_limit * 100.0
        # Guard: credit stop must be above entry (cost-to-close > credit received = loss),
        # target must be below entry (buy back for less than credit = profit)
        credit_stop_mult = max(1.01, float(self.optcfg.credit_stop_mult))
        credit_target_frac = max(0.01, min(0.99, float(self.optcfg.credit_target_frac)))
        target = entry_credit_value * credit_target_frac
        position_key = build_position_label(underlying, style, Side.SHORT, short_leg, long_leg)
        width_dollars = abs(float(short_leg.strike) - float(long_leg.strike)) * 100.0
        adjusted_max_loss = max(0.0, width_dollars - entry_credit_value)
        stop = min(width_dollars, entry_credit_value * credit_stop_mult)
        stop, target = clamp_short_premium_levels(entry_credit_value, stop, target)
        breakeven_underlying = float(short_leg.strike) - entry_limit if bullish else float(short_leg.strike) + entry_limit
        metadata = {
            "asset_type": ASSET_TYPE_OPTION_VERTICAL,
            "position_key": position_key,
            "underlying": underlying,
            "confirm_index": confirm_index,
            "style": style,
            "regime": regime.get("regime"),
            "regime_scores": regime.get("scores"),
            "regime_metrics": regime.get("metrics"),
            "spread_side": Side.SHORT.value,
            "spread_style": "CREDIT",
            "spread_type": "bull_put_credit" if bullish else "bear_call_credit",
            "direction": "bullish_credit" if bullish else "bearish_credit",
            "option_type": put_call,
            "entry_price": entry_credit_value,
            "entry_price_points": entry_limit,
            "entry_credit": entry_credit_value,
            "mark_price_hint": quoted_mid * 100.0,
            "max_loss_per_contract": adjusted_max_loss,
            "max_profit_per_contract": max(0.0, entry_credit_value),
            "strike_width_dollars": width_dollars,
            "breakeven_underlying": breakeven_underlying,
            "limit_price": entry_limit,
            "natural_bid": nat_bid * 100.0,
            "natural_ask": nat_ask * 100.0,
            "underlying_entry": last_underlying,
            "valuation_legs": [long_leg.symbol, short_leg.symbol],
            "long_leg_symbol": long_leg.symbol,
            "short_leg_symbol": short_leg.symbol,
            "short_strike": float(short_leg.strike),
            "long_strike": float(long_leg.strike),
            "sold_leg_symbol": short_leg.symbol,
            "bought_leg_symbol": long_leg.symbol,
            "sold_strike": float(short_leg.strike),
            "bought_strike": float(long_leg.strike),
            "long_leg": asdict(long_leg),
            "short_leg": asdict(short_leg),
            "order_spec": build_vertical_order(short_leg, long_leg, Side.SHORT, qty=1, limit_price=entry_limit),
        }
        # A credit spread is sold whichever way it leans: the order side is
        # SHORT, the proposal's direction LONG for a bull put, SHORT for a
        # bear call.
        return self.entry_policy.emit(
            admitted,
            reason=f"{style}_{'bull' if bullish else 'bear'}",
            strategy_score=self._option_strategy_score(candidate, regime, "range"),
            management={},
            target=target,
            metadata=metadata,
            order_side=Side.SHORT,
            reference_symbol=confirm_index,
            premium_stop=stop,
        )

    def _entry_styles(self) -> tuple[_EntryStyle, ...]:
        """The styles ``entry_signals`` tries, in order, one per kind: the
        ORB and trend debit spreads and the midday credit spread."""
        return (
            _EntryStyle("orb_debit_spread", "orb", self._build_debit_spread_signal),
            _EntryStyle("trend_debit_spread", "trend", self._build_debit_spread_signal),
            _EntryStyle("midday_credit_spread", "credit", self._build_credit_spread_signal),
        )

    def _style_window(self, kind: str, now_t: time) -> bool:
        """Is ``now_t`` inside the ``kind`` style's entry window (both ends
        inclusive)?"""
        p = self.params
        if kind == "orb":
            return is_time_in_window(now_t, p.get("orb_start_time", rth_open_plus(5)), p.get("orb_end_time", "10:05"))
        if kind == "trend":
            return is_time_in_window(now_t, p.get("trend_start_time", "10:05"), p.get("trend_end_time", "13:40"))
        return is_time_in_window(now_t, p.get("credit_start_time", "11:05"), p.get("credit_end_time", "13:45"))

    def _opening_window(self) -> tuple[time, int]:
        """The opening range's start and its length in minutes.
        ``orb_opening_window_start`` / ``orb_opening_window_end`` name its
        first and its last 1m bar, both inclusive, so the half-open window
        runs one minute past the end."""
        start = parse_hhmm(self.params.get("orb_opening_window_start", EQUITY_RTH_OPEN))
        end = parse_hhmm(self.params.get("orb_opening_window_end", rth_open_plus(4)))
        return start, (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute) + 1

    def _opening_range(self, frame: pd.DataFrame) -> tuple[float, float, int] | None:
        """Today's opening range the ORB style breaks, ``(high, low, bars)``
        over ``_opening_window()``, or None while fewer than
        ``orb_opening_min_bars`` of its bars are in or they hold no price.
        Until 2026-09-27 the debit ORB took a fixed 09:30-09:34 window and
        any bar in it, so a lone 09:34 bar was its opening range (the long
        options' own copy had required 3 since 2026-05-14), and both read a
        window with no price as a range of 0.0."""
        start, minutes = self._opening_window()
        return opening_range(frame, sessions.now_et().date(), start=start, minutes=minutes,
                             min_bars=int(self.params.get("orb_opening_min_bars", 3)))

    def _trend_momentum_blocker(self, frame: pd.DataFrame, last: pd.Series) -> str | None:
        """``options.trend_momentum_filter_enabled``: the trend style's
        refusal when ATR is not expanding or volume is not confirming the
        move, else None."""
        if not self.optcfg.trend_momentum_filter_enabled:
            return None
        atr_current = safe_float(last.get("atr14"), 0.0)
        atr_tail = frame.tail(20)["atr14"].dropna() if "atr14" in frame.columns else pd.Series(dtype=float)
        atr_mean = float(atr_tail.mean()) if len(atr_tail) > 0 else 0.0
        atr_expansion = atr_current / max(atr_mean, 1e-9) if atr_mean > 0 else 0.0
        vol_current = safe_float(last.get("volume"), 0.0)
        vol_tail = frame.tail(10)["volume"].dropna() if "volume" in frame.columns else pd.Series(dtype=float)
        vol_mean = float(vol_tail.mean()) if len(vol_tail) > 0 else 1.0
        volume_ratio = vol_current / max(vol_mean, 1.0)
        min_atr_exp = float(getattr(self.optcfg, "trend_min_atr_expansion", 0.85))
        min_vol_ratio = float(getattr(self.optcfg, "trend_min_volume_ratio", 0.90))
        if atr_expansion < min_atr_exp or volume_ratio < min_vol_ratio:
            return f"trend_momentum_filter(atr_exp={atr_expansion:.3f}<{min_atr_exp},vol_ratio={volume_ratio:.3f}<{min_vol_ratio})"
        return None

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        if not self._options_enabled() or client is None or data is None:
            return []
        out: list[Signal] = []
        # ATR caches for credit distance gate + adaptive width
        self._underlying_atr_cache.clear()
        self._underlying_ref_atr_cache.clear()
        now_dt = sessions.now_et()
        blackout_reason = self._option_entry_block_reason(now_dt)
        if blackout_reason:
            for c in candidates:
                self._record_entry_decision(c.symbol, "skipped", [blackout_reason])
            return out
        now_t = now_dt.time()
        if now_t > parse_hhmm(self.params.get("no_new_entries_after", "13:30")):
            for c in candidates:
                self._record_entry_decision(c.symbol, "skipped", ["after_entry_cutoff"])
            return out
        min_bars = int(self.params.get("min_bars", 35))
        if self._PREFETCH_OPTION_CHAINS:
            # Pre-warm option-chain cache for candidates that will reach a
            # spread builder. Cheap pre-filters (already-open, insufficient
            # bars) match the per-candidate guards below to avoid wasted
            # Schwab calls on candidates the loop will skip. Regime check is
            # NOT pre-evaluated here — it's local-only compute, and avoiding
            # double evaluation matters more than skipping a chain fetch for
            # a regime-rejected candidate. Worst case: ~1-2 wasted chain
            # fetches per cycle, well under Schwab's per-minute cap.
            prefetch_symbols = [
                c.symbol for c in candidates
                if not self._underlying_already_open(c.symbol, positions)
                and bars.get(c.symbol) is not None
                and len(bars.get(c.symbol)) >= min_bars
            ]
            if prefetch_symbols:
                self._prefetch_option_chains(prefetch_symbols, client)
        styles = self._entry_styles()
        enabled = {style.kind: self._style_enabled(style.name) for style in styles}
        in_window = {style.kind: self._style_window(style.kind, now_t) for style in styles}
        buffer_pct = float(self.params.get("orb_breakout_buffer_pct", 0.0008))
        trend_min_ret5 = float(self.params.get("trend_min_ret5", 0.0007))
        for c in candidates:
            if self._underlying_already_open(c.symbol, positions):
                self._record_entry_decision(c.symbol, "skipped", ["underlying_already_open"])
                continue
            frame = bars.get(c.symbol)
            if frame is None or len(frame) < min_bars:
                self._record_entry_decision(c.symbol, "skipped", [insufficient_bars_reason("insufficient_underlying_bars", 0 if frame is None else len(frame), min_bars)])
                continue
            regime = self._regime_confirm(c, bars, data)
            if not regime.get("ok") or regime.get("no_trade"):
                self._record_entry_decision(c.symbol, "skipped", [str(regime.get("reason") or "regime_blocked")])
                continue
            confirm_index = regime.get("confirm_index")
            last = frame.iloc[-1]
            # Populate ATR caches for credit distance gate + adaptive width
            self._underlying_atr_cache[c.symbol] = safe_float(last.get("atr14"), 0.0)
            if "atr14" in frame.columns:
                atr_series = frame["atr14"].dropna().tail(20)
                self._underlying_ref_atr_cache[c.symbol] = float(atr_series.median()) if len(atr_series) >= 5 else 0.0
            opening = self._opening_range(frame)
            or_high, or_low = (None, None) if opening is None else opening[:2]
            regime_name = str(regime.get("regime") or "unknown")
            bullish = regime_name == "bullish_trend"
            bearish = regime_name == "bearish_trend"
            rangeish = regime_name == "range"
            last_close = safe_float(last["close"], 0.0)
            last_vwap = safe_float(last["vwap"], last_close)
            last_ret5 = safe_float(last["ret5"], 0.0)
            reasons: list[str] = []
            signal: Signal | None = None
            for style in styles:
                if not (enabled[style.kind] and in_window[style.kind]):
                    continue
                if style.kind == "credit":
                    if not rangeish:
                        continue
                    direction = last_close >= last_vwap
                elif not (bullish or bearish):
                    continue
                elif style.kind == "orb":
                    if opening is None:
                        continue
                    if bullish and last_close > or_high * (1.0 + buffer_pct) and last_close > last_vwap:
                        direction = True
                    elif bearish and last_close < or_low * (1.0 - buffer_pct) and last_close < last_vwap:
                        direction = False
                    else:
                        continue
                else:
                    blocker = self._trend_momentum_blocker(frame, last)
                    if blocker is not None:
                        reasons.append(blocker)
                        continue
                    if bullish and last_ret5 >= trend_min_ret5:
                        direction = True
                    elif bearish and last_ret5 <= -trend_min_ret5:
                        direction = False
                    else:
                        continue
                # The style's own blockers are the proposal's pending
                # reasons: a refusal lists them first, then every shared veto.
                pending = style.pending_reasons(direction, frame, regime) if style.pending_reasons is not None else ()
                signal = style.build(c, direction, client, data, frame, last_close, style.name, confirm_index, regime,
                                     pending_reasons=pending)
                if signal is not None:
                    break
                reasons.extend(self._consume_style_failure(c.symbol, style.name))
            if signal is not None:
                out.append(signal)
                self._record_entry_decision(c.symbol, "signal", [signal.reason])
                continue
            # A style that tried to build recorded why it did not (at least
            # its ``<style>_unavailable``), so an empty list means no style
            # triggered.
            self._record_entry_decision(c.symbol, "skipped", reasons or [
                _no_style_trigger_reason(
                    regime_name=regime_name,
                    bullish=bullish,
                    bearish=bearish,
                    rangeish=rangeish,
                    orb_enabled=enabled.get("orb", False),
                    orb_window=in_window.get("orb", False),
                    trend_enabled=enabled.get("trend", False),
                    trend_window=in_window.get("trend", False),
                    credit_enabled=enabled.get("credit", False),
                    credit_window=in_window.get("credit", False),
                    last_close=last_close,
                    last_vwap=last_vwap,
                    last_ret5=last_ret5,
                    trend_min_ret5=trend_min_ret5,
                    or_high=or_high,
                    or_low=or_low,
                    orb_buffer_pct=buffer_pct,
                )
            ])
        return out

    def should_force_flatten(self, position: Position) -> bool:
        if self._event_calendar.force_flatten_event(now_dt=sessions.now_et()) is not None:
            return True
        now_dt = sessions.now_et()
        flat_time = self.force_flat_time
        # On early-close days (Jul 3, Black Friday, Christmas Eve) the market
        # closes at 1:00 PM ET.  If the configured force_flatten_time is at or
        # past the early close, clamp it to 12 minutes before the early close
        # so positions are unwound while the market is still open.
        state = equity_session_state(now_dt)
        if state.early_close and flat_time >= state.rth_close_time:
            early_m = state.rth_close_time.hour * 60 + state.rth_close_time.minute - 12
            flat_time = time(max(0, early_m) // 60, max(0, early_m) % 60)
        return now_dt.time() >= flat_time

    def position_mark_price(self, position: Position, data) -> float | None:
        if position.strategy not in self._OPTION_FAMILY:
            return None
        if asset_type_of(position.metadata) != ASSET_TYPE_OPTION_VERTICAL:
            return None
        spread_side = Side(position.metadata.get("spread_side", Side.LONG.value))
        long_symbol = str(position.metadata.get("long_leg_symbol") or "")
        short_symbol = str(position.metadata.get("short_leg_symbol") or "")
        if spread_side == Side.LONG:
            first_symbol, second_symbol = long_symbol, short_symbol
        else:
            first_symbol, second_symbol = short_symbol, long_symbol
        q1 = data.get_quote(first_symbol) if data and first_symbol else None
        q2 = data.get_quote(second_symbol) if data and second_symbol else None
        if not q1 or not q2:
            return None
        if data is not None and not data.quotes_are_fresh([first_symbol, second_symbol], self.optcfg.max_quote_age_seconds):
            return None
        p1 = first_float(q1, "mid", "mark", "last", positive=True)
        p2 = first_float(q2, "mid", "mark", "last", positive=True)
        if p1 is None or p2 is None:
            return None
        return max(0.0, (p1 - p2) * 100.0)
