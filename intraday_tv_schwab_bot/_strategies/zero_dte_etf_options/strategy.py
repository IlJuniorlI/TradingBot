# SPDX-License-Identifier: MIT
from ..shared import (
    ASSET_TYPE_OPTION_VERTICAL,
    Any,
    Candidate,
    LOG,
    OptionContract,
    Path,
    Position,
    Side,
    Signal,
    asdict,
    build_position_label,
    build_vertical_order,
    call_schwab_client,
    choose_by_delta,
    choose_nearest_strike,
    contract_from_quote,
    datetime,
    empty_market_structure_context,
    equity_session_state,
    filter_contracts,
    net_credit_dollars,
    net_debit_dollars,
    now_et,
    parse_hhmm,
    parse_option_chain,
    pd,
    replace,
    single_option_price_bounds,
    summarize_htf_trend,
    time,
    time_mod,
    vertical_limit_price,
    vertical_price_bounds,
    yaml,
)
from ..strategy_base import BaseStrategy
from ..rvol import rvol_profile_for_symbol

class ZeroDteEtfOptionsStrategy(BaseStrategy):
    strategy_name = 'zero_dte_etf_options'

    def required_history_bars(self, symbol: str | None = None, positions: dict[str, Position] | None = None) -> int:
        capability_bars = self._manifest_required_history_bars()
        if capability_bars is not None:
            return capability_bars
        return max(0, int(self.params.get("min_bars", 40) or 40))
    def __init__(self, config):
        super().__init__(config)
        self.optcfg = config.options
        self.force_flat_time = parse_hhmm(self.optcfg.force_flatten_time)
        self.event_blackouts: list[dict[str, Any]] = []
        self._event_blackout_source_mtime: float | None = None
        self._event_blackout_source_path: str | None = None
        self.event_blackouts = self._load_event_blackouts(force_reload=True)
        self._option_chain_cache: dict[tuple[str, str], tuple[datetime, list[OptionContract]]] = {}
        self._underlying_atr_cache: dict[str, float] = {}
        self._underlying_ref_atr_cache: dict[str, float] = {}

    def _options_enabled(self) -> bool:
        return bool(self.optcfg.enabled)

    def _style_enabled(self, style: str) -> bool:
        allowed = {str(s).strip() for s in (self.optcfg.styles or []) if str(s).strip()}
        return style in allowed

    def _long_option_style_enabled(self, style: str) -> bool:
        allowed = {str(s).strip() for s in (self.optcfg.styles or []) if str(s).strip()}
        return style in allowed

    @staticmethod
    def _weekday_token(value: Any) -> str | None:
        if value is None:
            return None
        token = str(value).strip().upper()
        mapping = {"0": "MON", "1": "TUE", "2": "WED", "3": "THU", "4": "FRI", "5": "SAT", "6": "SUN", "MONDAY": "MON", "TUESDAY": "TUE", "WEDNESDAY": "WED", "THURSDAY": "THU", "FRIDAY": "FRI", "SATURDAY": "SAT", "SUNDAY": "SUN"}
        return mapping.get(token, token[:3] if token else None)

    def _load_event_blackouts(self, *, force_reload: bool = False) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for row in list(self.optcfg.event_blackouts or []):
            if isinstance(row, dict):
                events.append(dict(row))
        path = self.optcfg.event_blackout_file
        if path:
            raw_path = Path(path).expanduser()
            candidate_paths: list[Path] = [raw_path]

            if not raw_path.is_absolute():
                package_root = Path(__file__).resolve().parents[2]
                project_root = Path(__file__).resolve().parents[3]
                for base_dir in (package_root, project_root):
                    alt_path = (base_dir / raw_path).resolve()
                    if alt_path not in candidate_paths:
                        candidate_paths.append(alt_path)

            chosen_path = next((candidate for candidate in candidate_paths if candidate.exists()), candidate_paths[0])
            current_mtime: float | None = None
            if chosen_path.exists():
                try:
                    current_mtime = float(chosen_path.stat().st_mtime)
                except Exception:
                    current_mtime = None
            path_token = str(chosen_path)
            if (
                not force_reload
                and path_token == self._event_blackout_source_path
                and current_mtime == self._event_blackout_source_mtime
            ):
                return list(self.event_blackouts)

            try:
                payload = yaml.safe_load(chosen_path.read_text()) or []
                if isinstance(payload, dict):
                    payload = payload.get("events", [])
                for row in payload or []:
                    if isinstance(row, dict):
                        events.append(dict(row))
                self._event_blackout_source_path = path_token
                self._event_blackout_source_mtime = current_mtime
            except FileNotFoundError:
                self._event_blackout_source_path = path_token
                self._event_blackout_source_mtime = None
                LOG.warning("Event blackout file not found. Tried: %s", candidate_paths)
            except Exception as exc:
                self._event_blackout_source_path = path_token
                self._event_blackout_source_mtime = current_mtime
                LOG.warning("Failed to load event blackout file %s: %s", chosen_path, exc)
        self.event_blackouts = list(events)
        return list(events)

    def _matching_event_blackout(self, now_dt=None) -> dict[str, Any] | None:
        now_dt = now_dt or now_et()
        self.event_blackouts = self._load_event_blackouts()
        today = now_dt.date().isoformat()
        weekday = self._weekday_token(now_dt.weekday())
        now_t = now_dt.time()
        for event in self.event_blackouts:
            if not bool(event.get("enabled", True)):
                continue
            event_date = event.get("date")
            event_weekday = self._weekday_token(event.get("weekday")) if event.get("weekday") is not None else None
            if event_date and str(event_date) != today:
                continue
            if event_weekday and event_weekday != weekday:
                continue
            start = event.get("start")
            end = event.get("end")
            if not start or not end:
                continue
            if parse_hhmm(str(start)) <= now_t <= parse_hhmm(str(end)):
                return event
        return None

    def _option_entry_block_reason(self, now_dt=None) -> str | None:
        event = self._matching_event_blackout(now_dt)
        if event and bool(event.get("block_new_entries", True)):
            return str(event.get("label") or "event_blackout")
        return None

    @staticmethod
    def _option_chain_cache_key(symbol: str) -> tuple[str, str]:
        return str(symbol).upper().strip(), now_et().date().isoformat()

    def _get_cached_option_chain(self, symbol: str) -> list[OptionContract] | None:
        ttl = max(0, int(getattr(self.optcfg, "option_chain_cache_seconds", 8) or 0))
        if ttl <= 0:
            return None
        key = self._option_chain_cache_key(symbol)
        cached = self._option_chain_cache.get(key)
        if cached is None:
            return None
        fetched_at, contracts = cached
        if (now_et() - fetched_at).total_seconds() > ttl:
            self._option_chain_cache.pop(key, None)
            return None
        return list(contracts)

    def _set_cached_option_chain(self, symbol: str, contracts: list[OptionContract]) -> None:
        ttl = max(0, int(getattr(self.optcfg, "option_chain_cache_seconds", 8) or 0))
        if ttl <= 0:
            return
        key = self._option_chain_cache_key(symbol)
        if key in self._option_chain_cache:
            self._option_chain_cache.pop(key, None)
        self._option_chain_cache[key] = (now_et(), list(contracts))
        max_entries = max(1, int(getattr(self.optcfg, "option_chain_cache_max_entries", 24) or 24))
        while len(self._option_chain_cache) > max_entries:
            oldest = next(iter(self._option_chain_cache))
            self._option_chain_cache.pop(oldest, None)



    @classmethod
    def _underlying_already_open(cls, symbol: str, positions: dict[str, Position]) -> bool:
        for p in positions.values():
            if p.strategy not in {cls.strategy_name, 'zero_dte_etf_long_options'}:
                continue
            if str(p.metadata.get("underlying") or p.symbol) == symbol:
                return True
        return False

    @staticmethod
    def _time_in_range(now_t: time, start: str, end: str) -> bool:
        return parse_hhmm(start) <= now_t <= parse_hhmm(end)

    @staticmethod
    def _option_quote_force_cooldown_seconds() -> float:
        return 1.0

    def _compute_time_decay_scale(self) -> float:
        """Returns 1.0 at/before decay_start, min_scale at/after decay_end,
        linear interpolation between. Used to scale debit/single target and
        stop multipliers as theta decay accelerates through the 0DTE session."""
        if not getattr(self.optcfg, "debit_target_time_decay_enabled", False):
            return 1.0
        now_t = now_et().time()
        start = parse_hhmm(getattr(self.optcfg, "debit_target_time_decay_start", "10:30"))
        end = parse_hhmm(getattr(self.optcfg, "debit_target_time_decay_end", "14:00"))
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
        if not getattr(self.optcfg, "delta_time_shift_enabled", False):
            return base_delta
        now_t = now_et().time()
        shift_start = parse_hhmm(getattr(self.optcfg, "delta_time_shift_start", "10:00"))
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
        """Scale strike width with current ATR vs trailing median to adapt to
        volatility. Wider on high-vol days (more premium), tighter on quiet."""
        if not getattr(self.optcfg, "adaptive_width_enabled", False):
            return base_width
        current_atr = getattr(self, "_underlying_atr_cache", {}).get(underlying)
        ref_atr = getattr(self, "_underlying_ref_atr_cache", {}).get(underlying)
        if current_atr is None or ref_atr is None or ref_atr <= 0:
            return base_width
        max_scale = float(getattr(self.optcfg, "adaptive_width_max_scale", 1.5))
        scale = max(1.0, min(max_scale, current_atr / ref_atr))
        return round(base_width * scale * 2) / 2  # round to nearest $0.50

    @staticmethod
    def _option_quote_stability_force_cooldown_seconds() -> float:
        return 0.0

    def prefetch_entry_market_data(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], data=None) -> None:
        if data is None or not hasattr(data, "prefetch_htf_contexts"):
            return
        if not candidates:
            return
        symbols = [
            c.symbol
            for c in candidates
            if c.symbol in bars and bars.get(c.symbol) is not None and not bars.get(c.symbol).empty
        ]
        if not symbols:
            return
        data.prefetch_htf_contexts(
            symbols,
            timeframe_minutes=self._sr_timeframe_minutes(),
            lookback_days=self._sr_lookback_days(),
            refresh_seconds=self._sr_refresh_seconds(),
        )

    @staticmethod
    def _safe_pct(value: Any) -> float:
        pct = BaseStrategy._safe_float(value, 0.0)
        return pct / 100.0 if abs(pct) > 1.0 else pct

    def _bullish_sr_block_reason(self, sr_ctx) -> str:
        return self._reason_with_values(
            "too_close_to_htf_resistance",
            current=sr_ctx.resistance_distance_pct,
            required=float(self._support_resistance_setting("entry_min_clearance_pct", 0.0038)),
            op=">",
            digits=4,
            extras={
                "clearance_atr": (sr_ctx.resistance_distance_atr, ">", float(self._support_resistance_setting("entry_min_clearance_atr", 0.85))),
            },
        )

    def _bearish_sr_block_reason(self, sr_ctx) -> str:
        return self._reason_with_values(
            "too_close_to_htf_support",
            current=sr_ctx.support_distance_pct,
            required=float(self._support_resistance_setting("entry_min_clearance_pct", 0.0038)),
            op=">",
            digits=4,
            extras={
                "clearance_atr": (sr_ctx.support_distance_atr, ">", float(self._support_resistance_setting("entry_min_clearance_atr", 0.85))),
            },
        )

    @classmethod
    def _insufficient_bars_reason(cls, name: str, current: Any, required: Any) -> str:
        return cls._reason_with_values(name, current=current, required=required, op='>=', digits=0)

    @staticmethod
    def _fraction_relative(frame: pd.DataFrame, column: str, lookback: int, direction: str) -> float:
        if frame is None or frame.empty:
            return 0.0
        recent = frame.tail(max(2, lookback))
        if recent.empty or column not in recent.columns:
            return 0.0
        if direction == "above":
            return float((recent["close"] > recent[column]).mean())
        return float((recent["close"] < recent[column]).mean())

    @staticmethod
    def _flip_count(frame: pd.DataFrame, lookback: int) -> int:
        if frame is None or frame.empty:
            return 0
        recent = frame.tail(max(3, lookback))
        if recent.empty or "vwap" not in recent.columns:
            return 0
        sign = (recent["close"] - recent["vwap"]).apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0)).tolist()
        sign = [s for s in sign if s != 0]
        if len(sign) < 2:
            return 0
        return sum(1 for a, b in zip(sign, sign[1:]) if a != b)

    @staticmethod
    def _recent_range_pct(frame: pd.DataFrame, lookback: int) -> float:
        if frame is None or frame.empty:
            return 0.0
        recent = frame.tail(max(2, lookback))
        if recent.empty:
            return 0.0
        ref = BaseStrategy._safe_float(recent.iloc[-1]["close"], 0.0)
        if ref <= 0:
            return 0.0
        return max(0.0, float(recent["high"].max()) - float(recent["low"].min())) / ref

    def _htf_trend_context(self, symbol: str, data) -> dict[str, Any]:
        p = self.params
        sr_cfg = getattr(self.config, "support_resistance", None)
        if data is None or not hasattr(data, "get_htf_frame"):
            return {"available": False, "reason": "no_data_feed"}
        htf_tf = int(p.get("htf_timeframe_minutes", 15))
        lookback_days = int(p.get("htf_lookback_days", getattr(sr_cfg, "lookback_days", 10) if sr_cfg is not None else 10))
        refresh_seconds = int(p.get("htf_refresh_seconds", getattr(sr_cfg, "refresh_seconds", 600) if sr_cfg is not None else 600))
        frame = data.get_htf_frame(
            symbol,
            timeframe_minutes=htf_tf,
            lookback_days=lookback_days,
            refresh_seconds=refresh_seconds,
        )
        min_bars = int(p.get("htf_min_bars", 20))
        summary = summarize_htf_trend(
            frame,
            min_bars=min_bars,
            vwap_distance_pct=float(p.get("htf_vwap_distance_pct", 0.0009)),
            ema_gap_pct=float(p.get("htf_ema_gap_pct", 0.0007)),
            min_ret3=float(p.get("htf_min_ret3", 0.0009)),
            range_vwap_distance_pct=float(p.get("htf_range_vwap_distance_pct", 0.0020)),
            range_ema_gap_pct=float(p.get("htf_range_ema_gap_pct", 0.0010)),
        )
        if not bool(summary.get("available")):
            bars = 0 if frame is None else len(frame)
            summary["reason"] = self._insufficient_bars_reason("insufficient_htf_bars", bars, min_bars)
        summary["timeframe_minutes"] = htf_tf
        return summary

    def _regime_confirm(self, candidate: Candidate, bars: dict[str, pd.DataFrame], data) -> dict[str, Any]:
        p = self.params
        sr_cfg = getattr(self.config, "support_resistance", None)
        underlying = candidate.symbol
        confirm_symbol = self.optcfg.confirmation_symbols.get(underlying)
        vol_symbol = self.optcfg.volatility_symbol
        u = bars.get(underlying)
        idx = bars.get(confirm_symbol) if confirm_symbol else None
        min_bars = int(p.get("min_bars", 35))
        if u is None or len(u) < min_bars:
            return {
                "ok": False,
                "no_trade": True,
                "reason": self._insufficient_bars_reason("insufficient_underlying_bars", 0 if u is None else len(u), min_bars),
                "underlying": underlying,
                "confirm_index": confirm_symbol,
            }

        last_u = u.iloc[-1]
        u_close = self._safe_float(last_u["close"])
        u_vwap = self._safe_float(last_u["vwap"], u_close)
        u_ema9 = self._safe_float(last_u["ema9"], u_close)
        u_ema20 = self._safe_float(last_u["ema20"], u_close)
        u_vwap_dist = (u_close - u_vwap) / max(u_close, 1.0)
        u_ema_gap = (u_ema9 - u_ema20) / max(u_close, 1.0)
        u_ret5 = self._safe_float(last_u["ret5"], 0.0)
        u_ret15 = self._safe_float(last_u["ret15"], 0.0)
        session_day = now_et().date()
        u_open = self._session_open_price(u, session_day, regular_session_only=True)
        if u_open is None:
            u_open = self._session_open_price(u, session_day, regular_session_only=False)
        u_day_ret = float((u_close / u_open) - 1.0) if u_open else 0.0
        u_above_frac = self._fraction_relative(u, "vwap", int(p.get("trend_vwap_lookback", 8)), "above")
        u_below_frac = self._fraction_relative(u, "vwap", int(p.get("trend_vwap_lookback", 8)), "below")
        u_flip_count = self._flip_count(u, int(p.get("flip_lookback", 12)))
        u_range_pct = self._recent_range_pct(u, int(p.get("range_lookback", 20)))

        min_confirm_bars = int(p.get("min_confirm_bars", 20))
        idx_available = bool(idx is not None and len(idx) >= min_confirm_bars)
        idx_bullish = idx_bearish = idx_range = False
        idx_vwap_dist = 0.0
        idx_ema_gap = 0.0
        idx_flip_count = 0
        if idx_available:
            last_i = idx.iloc[-1]
            i_close = self._safe_float(last_i["close"])
            i_vwap = self._safe_float(last_i["vwap"], i_close)
            i_ema9 = self._safe_float(last_i["ema9"], i_close)
            i_ema20 = self._safe_float(last_i["ema20"], i_close)
            idx_vwap_dist = (i_close - i_vwap) / max(i_close, 1.0)
            idx_ema_gap = (i_ema9 - i_ema20) / max(i_close, 1.0)
            idx_flip_count = self._flip_count(idx, int(p.get("flip_lookback", 12)))
            idx_bullish = idx_vwap_dist >= float(p.get("trend_vwap_distance_pct", 0.0016)) and idx_ema_gap >= float(p.get("trend_ema_gap_pct", 0.00075))
            idx_bearish = idx_vwap_dist <= -float(p.get("trend_vwap_distance_pct", 0.0016)) and idx_ema_gap <= -float(p.get("trend_ema_gap_pct", 0.00075))
            idx_range = abs(idx_vwap_dist) <= float(p.get("range_vwap_distance_pct", 0.0019)) and abs(idx_ema_gap) <= float(p.get("range_ema_gap_pct", 0.00075))

        q = data.get_quote(vol_symbol) if data else None
        if data is not None and vol_symbol:
            try:
                max_age = max(1.0, float(self.config.runtime.quote_cache_seconds))
                if not data.quotes_are_fresh([vol_symbol], max_age):
                    q = None
            except Exception:
                LOG.debug("Failed to validate freshness of volatility quote for %s; using current quote snapshot as-is.", vol_symbol, exc_info=True)
        vix_last = self._positive_quote_value(q, "last", "mid", "mark")
        vix_pct = self._safe_pct(q.get("percent_change")) if q is not None and q.get("percent_change") is not None else 0.0
        candidate_rvol = self._safe_float(candidate.metadata.get("relative_volume_10d_calc"), 0.0)
        candidate_effective_rvol = self._effective_relative_volume(underlying, candidate_rvol, p, cap_default=2.5, standard_floor=1.0)
        candidate_rvol_profile = rvol_profile_for_symbol(underlying, p or {})
        candidate_day_move = self._safe_pct(candidate.metadata.get("change_from_open"))

        max_vix = float(self.optcfg.max_vix)
        vix_spike_pct = float(self.optcfg.vix_spike_pct)
        min_candidate_rvol = float(p.get("min_candidate_rvol", 1.15))
        min_candidate_rvol_required = self._relative_volume_gate_threshold(underlying, min_candidate_rvol, p)
        chaos_intraday_range_pct = float(p.get("chaos_intraday_range_pct", 0.016))
        chop_flip_min = int(p.get("chop_flip_min", 4))
        trend_vwap_distance_pct = float(p.get("trend_vwap_distance_pct", 0.0016))

        reasons: list[str] = []
        if vix_last is not None and vix_last > max_vix:
            reasons.append(self._reason_with_values("vix_above_limit", current=vix_last, required=max_vix, op="<=", digits=2))
        if abs(vix_pct) >= vix_spike_pct:
            reasons.append(self._reason_with_values("vix_spike", current=abs(vix_pct), required=vix_spike_pct, op="<", digits=4))
        if candidate_rvol < min_candidate_rvol_required:
            reasons.append(self._reason_with_values("weak_relative_volume", current=candidate_rvol, required=min_candidate_rvol_required, op=">=", digits=2))
        if u_range_pct >= chaos_intraday_range_pct and u_flip_count >= chop_flip_min:
            reasons.append(
                self._reason_with_values(
                    "chaotic_intraday_range",
                    current=u_range_pct,
                    required=chaos_intraday_range_pct,
                    op="<",
                    digits=4,
                    extras={"flips": (u_flip_count, "<", chop_flip_min)},
                )
            )
        require_index_confirmation = bool(p.get("require_index_confirmation", True))
        if require_index_confirmation and confirm_symbol and not idx_available:
            reasons.append(
                self._insufficient_bars_reason(
                    "insufficient_confirm_bars",
                    0 if idx is None else len(idx),
                    min_confirm_bars,
                )
            )
        if require_index_confirmation and idx_available:
            trend_disagree = (u_vwap_dist > 0 > idx_vwap_dist) or (u_vwap_dist < 0 < idx_vwap_dist)
            if trend_disagree and abs(u_vwap_dist) >= trend_vwap_distance_pct and abs(idx_vwap_dist) >= trend_vwap_distance_pct:
                reasons.append(
                    self._reason_with_values(
                        "underlying_index_disagreement",
                        current=abs(u_vwap_dist),
                        required=trend_vwap_distance_pct,
                        op="<",
                        digits=4,
                        extras={"index_vwap_dist": (abs(idx_vwap_dist), "<", trend_vwap_distance_pct)},
                    )
                )

        pattern_ctx = self._chart_context(u)
        candle_ctx = self._candle_context(u)
        bull_candle_signal = self._directional_candle_signal(u, Side.LONG)
        bear_candle_signal = self._directional_candle_signal(u, Side.SHORT)
        sr_ctx = self._sr_context(underlying, u, data)
        mshtf_ctx = getattr(sr_ctx, "market_structure", None) or empty_market_structure_context(u_close)
        ms1_ctx = self._structure_context(u, "1m")
        sr_weight = float(getattr(sr_cfg, "regime_weight", 0.75) or 0.75)
        mshtf_weight = float(getattr(sr_cfg, "structure_htf_weight", 0.90) or 0.90)
        ms1_weight = float(getattr(sr_cfg, "structure_1m_weight", 0.70) or 0.70)
        bullish_candle_score = float(bull_candle_signal.get("score", 0.0) or 0.0)
        bearish_candle_score = float(bear_candle_signal.get("score", 0.0) or 0.0)
        bullish_candle_net_score = float(bull_candle_signal.get("net_score", 0.0) or 0.0)
        bearish_candle_net_score = float(bear_candle_signal.get("net_score", 0.0) or 0.0)
        candle_weight = float(p.get("candle_weight", 0.50))
        candle_sr_weight = float(p.get("candle_sr_weight", 0.35))
        candle_trend_follow_weight = float(p.get("candle_trend_follow_weight", 0.25))
        candle_range_penalty = float(p.get("candle_range_penalty", 0.30))
        candle_mixed_penalty = float(p.get("candle_mixed_penalty", 0.18))
        candle_anchor = max(
            float(p.get("range_vwap_distance_pct", 0.0019)),
            float(p.get("trend_vwap_distance_pct", 0.0016)),
        )
        bullish_candle_confirm = bool(bull_candle_signal.get("confirmed") and bullish_candle_net_score > bearish_candle_net_score)
        bearish_candle_confirm = bool(bear_candle_signal.get("confirmed") and bearish_candle_net_score > bullish_candle_net_score)
        mixed_candles = bool(bull_candle_signal.get("mixed"))
        bullish_candle_scale = min(1.0, bullish_candle_net_score / 1.0) if bullish_candle_confirm else 0.0
        bearish_candle_scale = min(1.0, bearish_candle_net_score / 1.0) if bearish_candle_confirm else 0.0

        use_htf_confirmation = bool(p.get("use_htf_trend_confirmation", False))
        require_htf_alignment = bool(p.get("require_htf_alignment", use_htf_confirmation))
        htf_score_bonus = float(p.get("htf_score_bonus", 0.65))
        htf_score_penalty = float(p.get("htf_score_penalty", 0.65))
        htf_ctx = self._htf_trend_context(underlying, data) if use_htf_confirmation else {"available": False, "reason": "disabled"}
        htf_available = bool(htf_ctx.get("available"))
        htf_bullish = bool(htf_ctx.get("bullish")) if htf_available else False
        htf_bearish = bool(htf_ctx.get("bearish")) if htf_available else False
        htf_range = bool(htf_ctx.get("range")) if htf_available else False
        if use_htf_confirmation and require_htf_alignment and not htf_available:
            reasons.append(str(htf_ctx.get("reason") or "insufficient_htf_bars"))

        htf_fvg_ctx = self._htf_context(
            underlying,
            data,
            timeframe_minutes=self._sr_timeframe_minutes(),
            lookback_days=self._sr_lookback_days(),
            pivot_span=int(self._support_resistance_setting("pivot_span", 2) or 2),
            max_levels_per_side=int(self._support_resistance_setting("max_levels_per_side", 6) or 6),
            atr_tolerance_mult=float(self._support_resistance_setting("atr_tolerance_mult", 0.35) or 0.35),
            pct_tolerance=float(self._support_resistance_setting("pct_tolerance", 0.0030) or 0.0030),
            stop_buffer_atr_mult=float(self._support_resistance_setting("stop_buffer_atr_mult", 0.25) or 0.25),
            ema_fast_span=int(self._support_resistance_setting("ema_fast_span", 50) or 50),
            ema_slow_span=int(self._support_resistance_setting("ema_slow_span", 200) or 200),
            refresh_seconds=self._sr_refresh_seconds(),
            current_price=u_close,
            use_prior_day_high_low=bool(self._support_resistance_setting("use_prior_day_high_low", True)),
            use_prior_week_high_low=bool(self._support_resistance_setting("use_prior_week_high_low", True)),
        )
        fvg1_ctx = self._one_minute_fvg_context(underlying, u, data)
        use_fvg_context = self._shared_entry_enabled("use_fvg_context", True)
        fvg_context_weight_scale = max(0.0, float(p.get("fvg_context_weight_scale", 0.9) or 0.0))
        htf_fvg_score = self._score_fvg_context(u_close, htf_fvg_ctx, timeframe_minutes=getattr(htf_fvg_ctx, "timeframe_minutes", self._sr_timeframe_minutes())) if use_fvg_context else {"bull_score": 0.0, "bear_score": 0.0, "directional_pressure": 0.0}
        fvg1_score = self._score_fvg_context(u_close, fvg1_ctx, timeframe_minutes=1) if use_fvg_context else {"bull_score": 0.0, "bear_score": 0.0, "directional_pressure": 0.0}

        bull_score = 0.0
        bear_score = 0.0
        range_score = 0.0
        bull_score += 1.5 if u_vwap_dist >= float(p.get("trend_vwap_distance_pct", 0.0016)) else 0.0
        bull_score += 1.0 if u_ema_gap >= float(p.get("trend_ema_gap_pct", 0.00075)) else 0.0
        bull_score += 1.0 if u_ret5 >= float(p.get("trend_min_ret5", 0.0008)) else 0.0
        bull_score += 1.0 if u_ret15 >= float(p.get("trend_min_ret15", 0.0014)) else 0.0
        bull_score += 1.0 if u_above_frac >= float(p.get("trend_above_vwap_frac", 0.75)) else 0.0
        bull_score += 0.75 if candidate_effective_rvol >= float(p.get("trend_rvol", 1.25)) else 0.0
        bull_score += 1.0 if idx_bullish else (-0.5 if require_index_confirmation and idx_available else 0.0)
        bull_score += 1.25 if pattern_ctx.matched_bullish_continuation else 0.0
        bull_score += 0.75 if pattern_ctx.matched_bullish_reversal and u_vwap_dist >= 0 else 0.0
        bull_score -= 0.75 if pattern_ctx.matched_bearish_reversal else 0.0
        bull_score -= 1.00 if pattern_ctx.matched_bearish_continuation else 0.0
        bull_score -= 1.0 if u_flip_count > int(p.get("chop_flip_max_for_trend", 3)) else 0.0
        bull_score -= 1.0 if u_range_pct > float(p.get("chaos_intraday_range_pct", 0.016)) else 0.0
        bull_score += sr_weight if sr_ctx.breakout_above_resistance else 0.0
        bull_score += sr_weight * 0.40 if sr_ctx.near_support and not sr_ctx.breakdown_below_support else 0.0
        bull_score -= sr_weight * 0.45 if sr_ctx.near_resistance and not sr_ctx.breakout_above_resistance else 0.0
        bull_score += candle_weight * bullish_candle_scale if bullish_candle_confirm and u_vwap_dist >= -candle_anchor else 0.0
        bull_score += candle_sr_weight * bullish_candle_scale if bullish_candle_confirm and sr_ctx.near_support and not sr_ctx.breakdown_below_support else 0.0
        bull_score += candle_trend_follow_weight * bullish_candle_scale if bullish_candle_confirm and pattern_ctx.matched_bullish_continuation else 0.0
        bull_score -= candle_weight * bearish_candle_scale if bearish_candle_confirm else 0.0
        bull_score -= candle_mixed_penalty if mixed_candles else 0.0
        bull_score += htf_score_bonus if htf_bullish else 0.0
        bull_score -= htf_score_penalty if htf_bearish else 0.0

        bear_score += 1.5 if u_vwap_dist <= -float(p.get("trend_vwap_distance_pct", 0.0016)) else 0.0
        bear_score += 1.0 if u_ema_gap <= -float(p.get("trend_ema_gap_pct", 0.00075)) else 0.0
        bear_score += 1.0 if u_ret5 <= -float(p.get("trend_min_ret5", 0.0008)) else 0.0
        bear_score += 1.0 if u_ret15 <= -float(p.get("trend_min_ret15", 0.0014)) else 0.0
        bear_score += 1.0 if u_below_frac >= float(p.get("trend_above_vwap_frac", 0.75)) else 0.0
        bear_score += 0.75 if candidate_effective_rvol >= float(p.get("trend_rvol", 1.25)) else 0.0
        bear_score += 1.0 if idx_bearish else (-0.5 if require_index_confirmation and idx_available else 0.0)
        bear_score += 1.25 if pattern_ctx.matched_bearish_continuation else 0.0
        bear_score += 0.75 if pattern_ctx.matched_bearish_reversal and u_vwap_dist <= 0 else 0.0
        bear_score -= 0.75 if pattern_ctx.matched_bullish_reversal else 0.0
        bear_score -= 1.00 if pattern_ctx.matched_bullish_continuation else 0.0
        bear_score -= 1.0 if u_flip_count > int(p.get("chop_flip_max_for_trend", 3)) else 0.0
        bear_score -= 1.0 if u_range_pct > float(p.get("chaos_intraday_range_pct", 0.016)) else 0.0
        bear_score += sr_weight if sr_ctx.breakdown_below_support else 0.0
        bear_score += sr_weight * 0.40 if sr_ctx.near_resistance and not sr_ctx.breakout_above_resistance else 0.0
        bear_score -= sr_weight * 0.45 if sr_ctx.near_support and not sr_ctx.breakdown_below_support else 0.0
        bear_score += candle_weight * bearish_candle_scale if bearish_candle_confirm and u_vwap_dist <= candle_anchor else 0.0
        bear_score += candle_sr_weight * bearish_candle_scale if bearish_candle_confirm and sr_ctx.near_resistance and not sr_ctx.breakout_above_resistance else 0.0
        bear_score += candle_trend_follow_weight * bearish_candle_scale if bearish_candle_confirm and pattern_ctx.matched_bearish_continuation else 0.0
        bear_score -= candle_weight * bullish_candle_scale if bullish_candle_confirm else 0.0
        bear_score -= candle_mixed_penalty if mixed_candles else 0.0
        bear_score += htf_score_bonus if htf_bearish else 0.0
        bear_score -= htf_score_penalty if htf_bullish else 0.0

        range_score += 1.5 if abs(u_vwap_dist) <= float(p.get("range_vwap_distance_pct", 0.0019)) else 0.0
        range_score += 1.0 if abs(u_ema_gap) <= float(p.get("range_ema_gap_pct", 0.00075)) else 0.0
        range_score += 1.0 if u_range_pct <= float(p.get("range_max_intraday_move_pct", 0.012)) else 0.0
        range_score += 1.0 if abs(u_day_ret) <= float(p.get("credit_max_day_move_pct", 0.010)) else 0.0
        range_score += 1.0 if u_flip_count >= int(p.get("chop_flip_min", 4)) else 0.0
        range_score += 0.75 if idx_available and idx_range else 0.0
        range_score += 0.5 if candidate_effective_rvol >= float(p.get("credit_min_rvol", 0.90)) else 0.0
        range_score -= 0.50 if pattern_ctx.matched_bullish_continuation or pattern_ctx.matched_bearish_continuation else 0.0
        range_score -= 0.25 if pattern_ctx.matched_bullish_reversal or pattern_ctx.matched_bearish_reversal else 0.0
        range_score -= 1.0 if candidate_effective_rvol >= float(p.get("credit_max_rvol", 2.50)) else 0.0
        range_score -= 1.0 if abs(candidate_day_move) >= float(p.get("credit_max_day_move_pct", 0.010)) else 0.0
        range_score -= 1.0 if abs(vix_pct) >= float(p.get("credit_max_vix_change_pct", 0.015)) else 0.0
        range_score += sr_weight * 0.30 if sr_ctx.near_support and sr_ctx.near_resistance else 0.0
        range_score += sr_weight * 0.20 if sr_ctx.regime_hint == "range_between_levels" else 0.0
        range_score -= sr_weight * 0.35 if sr_ctx.breakout_above_resistance or sr_ctx.breakdown_below_support else 0.0
        range_score -= candle_range_penalty if bullish_candle_confirm or bearish_candle_confirm else 0.0
        range_score -= candle_mixed_penalty * 0.5 if mixed_candles else 0.0
        range_score += htf_score_bonus * 0.35 if htf_range else 0.0
        range_score -= htf_score_penalty * 0.35 if (htf_bullish or htf_bearish) else 0.0

        bull_score += mshtf_weight * 0.60 if mshtf_ctx.bias == "bullish" else 0.0
        bull_score -= mshtf_weight * 0.60 if mshtf_ctx.bias == "bearish" else 0.0
        bull_score += mshtf_weight * 0.95 if self._active_structure_break(mshtf_ctx.bos_up, mshtf_ctx.bos_up_age_bars) else 0.0
        bull_score -= mshtf_weight * 1.05 if self._active_structure_break(mshtf_ctx.choch_down, mshtf_ctx.choch_down_age_bars) else 0.0
        bull_score += ms1_weight * 0.70 if ms1_ctx.bias == "bullish" else 0.0
        bull_score -= ms1_weight * 0.75 if ms1_ctx.bias == "bearish" else 0.0
        bull_score += ms1_weight if (ms1_ctx.bos_up and self._structure_event_recent(ms1_ctx.bos_up_age_bars)) else 0.0
        bull_score -= ms1_weight if (ms1_ctx.choch_down and self._structure_event_recent(ms1_ctx.choch_down_age_bars)) else 0.0

        bear_score += mshtf_weight * 0.60 if mshtf_ctx.bias == "bearish" else 0.0
        bear_score -= mshtf_weight * 0.60 if mshtf_ctx.bias == "bullish" else 0.0
        bear_score += mshtf_weight * 0.95 if self._active_structure_break(mshtf_ctx.bos_down, mshtf_ctx.bos_down_age_bars) else 0.0
        bear_score -= mshtf_weight * 1.05 if self._active_structure_break(mshtf_ctx.choch_up, mshtf_ctx.choch_up_age_bars) else 0.0
        bear_score += ms1_weight * 0.70 if ms1_ctx.bias == "bearish" else 0.0
        bear_score -= ms1_weight * 0.75 if ms1_ctx.bias == "bullish" else 0.0
        bear_score += ms1_weight if (ms1_ctx.bos_down and self._structure_event_recent(ms1_ctx.bos_down_age_bars)) else 0.0
        bear_score -= ms1_weight if (ms1_ctx.choch_up and self._structure_event_recent(ms1_ctx.choch_up_age_bars)) else 0.0

        bull_score += (htf_fvg_score["bull_score"] + fvg1_score["bull_score"]) * fvg_context_weight_scale
        bear_score += (htf_fvg_score["bear_score"] + fvg1_score["bear_score"]) * fvg_context_weight_scale

        range_score += mshtf_weight * 0.35 if mshtf_ctx.bias == "neutral" else 0.0
        range_score -= mshtf_weight * 0.35 if mshtf_ctx.bias in {"bullish", "bearish"} else 0.0
        range_score += ms1_weight * 0.20 if ms1_ctx.bias == "neutral" else 0.0
        range_score -= min(0.45, ((htf_fvg_score["directional_pressure"] * 0.35) + (fvg1_score["directional_pressure"] * 0.25)) * fvg_context_weight_scale)

        scores = {"bullish_trend": bull_score, "bearish_trend": bear_score, "range": range_score}
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_name, top_score = ranked[0]
        second_name, second_score = ranked[1] if len(ranked) > 1 else ("none", 0.0)
        min_trend_score = float(p.get("min_trend_score", 4.9))
        min_range_score = float(p.get("min_range_score", 4.6))
        min_score_gap = float(p.get("min_score_gap", 1.6))
        regime = "no_trade"
        no_trade = bool(reasons)

        if not no_trade:
            if top_name in {"bullish_trend", "bearish_trend"} and top_score >= min_trend_score and (top_score - second_score) >= min_score_gap:
                regime = top_name
            elif top_name == "range" and top_score >= min_range_score and (top_score - second_score) >= min_score_gap:
                regime = "range"
            else:
                no_trade = True
                reasons.append(
                    self._ambiguous_regime_reason(
                        top_name=top_name,
                        top_score=top_score,
                        second_name=second_name,
                        second_score=second_score,
                        min_top_score=min_range_score if top_name == "range" else min_trend_score,
                        min_score_gap=min_score_gap,
                    )
                )

        if not no_trade and use_htf_confirmation and require_htf_alignment and htf_available:
            if regime == "bullish_trend" and not htf_bullish:
                no_trade = True
                reasons.append(
                    self._reason_with_values(
                        "htf_trend_misaligned",
                        current=htf_ctx.get("vwap_dist", 0.0),
                        required=float(p.get("htf_vwap_distance_pct", 0.0009)),
                        op=">=",
                        digits=4,
                        extras={
                            "htf_direction": ("bullish" if htf_bullish else ("bearish" if htf_bearish else "range"), "=", "bullish"),
                            "htf_ret3": (float(htf_ctx.get("ret3", 0.0)), ">=", float(p.get("htf_min_ret3", 0.0009))),
                        },
                    )
                )
            elif regime == "bearish_trend" and not htf_bearish:
                no_trade = True
                reasons.append(
                    self._reason_with_values(
                        "htf_trend_misaligned",
                        current=abs(float(htf_ctx.get("vwap_dist", 0.0))),
                        required=float(p.get("htf_vwap_distance_pct", 0.0009)),
                        op=">=",
                        digits=4,
                        extras={
                            "htf_direction": ("bearish" if htf_bearish else ("bullish" if htf_bullish else "range"), "=", "bearish"),
                            "htf_ret3": (abs(float(htf_ctx.get("ret3", 0.0))), ">=", float(p.get("htf_min_ret3", 0.0009))),
                        },
                    )
                )

        if not no_trade and sr_cfg is not None and bool(getattr(sr_cfg, "structure_enabled", True)):
            if regime == "bullish_trend":
                if mshtf_ctx.bias == "bearish":
                    no_trade = True
                    reasons.append(f"htf_structure_bearish(tf={self._sr_timeframe_minutes()}m,last_high={mshtf_ctx.last_high_label},last_low={mshtf_ctx.last_low_label})")
                elif self._blocks_bullish_structure_entry(ms1_ctx):
                    no_trade = True
                    reasons.append(self._bullish_structure_block_reason(ms1_ctx))
            elif regime == "bearish_trend":
                if mshtf_ctx.bias == "bullish":
                    no_trade = True
                    reasons.append(f"htf_structure_bullish(tf={self._sr_timeframe_minutes()}m,last_high={mshtf_ctx.last_high_label},last_low={mshtf_ctx.last_low_label})")
                elif self._blocks_bearish_structure_entry(ms1_ctx):
                    no_trade = True
                    reasons.append(self._bearish_structure_block_reason(ms1_ctx))

        return {
            "ok": True,
            "underlying": underlying,
            "confirm_index": confirm_symbol,
            "regime": regime,
            "no_trade": no_trade or regime == "no_trade",
            "reason": ",".join(reasons) if reasons else regime,
            "scores": scores,
            "metrics": {
                "underlying_vwap_dist": u_vwap_dist,
                "underlying_ema_gap": u_ema_gap,
                "underlying_ret5": u_ret5,
                "underlying_ret15": u_ret15,
                "underlying_day_ret": u_day_ret,
                "underlying_range_pct": u_range_pct,
                "underlying_flip_count": u_flip_count,
                "htf_available": htf_available,
                "htf_vwap_dist": float(htf_ctx.get("vwap_dist", 0.0)) if htf_available else 0.0,
                "htf_ema_gap": float(htf_ctx.get("ema_gap", 0.0)) if htf_available else 0.0,
                "htf_ret3": float(htf_ctx.get("ret3", 0.0)) if htf_available else 0.0,
                "htf_bullish": htf_bullish,
                "htf_bearish": htf_bearish,
                "htf_range": htf_range,
                "confirm_vwap_dist": idx_vwap_dist,
                "confirm_ema_gap": idx_ema_gap,
                "confirm_flip_count": idx_flip_count,
                "candidate_rvol": candidate_rvol,
                "candidate_effective_rvol": candidate_effective_rvol,
                "candidate_rvol_profile": candidate_rvol_profile,
                "candidate_rvol_required": min_candidate_rvol_required,
                "candidate_change_from_open": candidate_day_move,
                "vix": vix_last,
                "vix_pct": vix_pct,
                "chart_pattern_bias_score": float(pattern_ctx.bias_score),
                "chart_pattern_regime_hint": str(pattern_ctx.regime_hint),
                "candle_bias_score": float(candle_ctx["candle_bias_score"]),
                "candle_net_score": float(candle_ctx.get("candle_net_score", candle_ctx["candle_bias_score"]) or candle_ctx["candle_bias_score"]),
                "candle_regime_hint": str(candle_ctx["candle_regime_hint"]),
                "matched_bullish_candles": list(candle_ctx["matched_bullish_candles"]),
                "matched_bearish_candles": list(candle_ctx["matched_bearish_candles"]),
                "bullish_candle_score": round(bullish_candle_score, 4),
                "bearish_candle_score": round(bearish_candle_score, 4),
                "bullish_candle_net_score": round(bullish_candle_net_score, 4),
                "bearish_candle_net_score": round(bearish_candle_net_score, 4),
                **self._structure_lists(ms1_ctx, prefix="ms1m"),
                **self._structure_lists(mshtf_ctx, prefix="mshtf"),
                "sr_bias_score": float(sr_ctx.bias_score),
                "sr_regime_hint": str(sr_ctx.regime_hint),
                "sr_nearest_support": float(sr_ctx.nearest_support.price) if sr_ctx.nearest_support else None,
                "sr_nearest_resistance": float(sr_ctx.nearest_resistance.price) if sr_ctx.nearest_resistance else None,
                "sr_support_distance_pct": None if sr_ctx.support_distance_pct is None else float(sr_ctx.support_distance_pct),
                "sr_resistance_distance_pct": None if sr_ctx.resistance_distance_pct is None else float(sr_ctx.resistance_distance_pct),
                "sr_breakout_above_resistance": bool(sr_ctx.breakout_above_resistance),
                "sr_breakdown_below_support": bool(sr_ctx.breakdown_below_support),
                "sr_supports": [float(round(lv.price, 4)) for lv in sr_ctx.supports],
                "sr_resistances": [float(round(lv.price, 4)) for lv in sr_ctx.resistances],
                "matched_bullish_chart_patterns": sorted(pattern_ctx.matched_bullish),
                "matched_bearish_chart_patterns": sorted(pattern_ctx.matched_bearish),
                "matched_bullish_chart_reversal_patterns": sorted(pattern_ctx.matched_bullish_reversal),
                "matched_bullish_chart_continuation_patterns": sorted(pattern_ctx.matched_bullish_continuation),
                "matched_bearish_chart_reversal_patterns": sorted(pattern_ctx.matched_bearish_reversal),
                "matched_bearish_chart_continuation_patterns": sorted(pattern_ctx.matched_bearish_continuation),
                "htf_fvg_bull_score": float(htf_fvg_score["bull_score"]),
                "htf_fvg_bear_score": float(htf_fvg_score["bear_score"]),
                "htf_fvg_nearest_bullish_state": str(htf_fvg_score["nearest_bullish"].get("state", "none")),
                "htf_fvg_nearest_bearish_state": str(htf_fvg_score["nearest_bearish"].get("state", "none")),
                "htf_fvg_nearest_bullish_midpoint": BaseStrategy._optional_float(htf_fvg_score["nearest_bullish"].get("midpoint")),
                "htf_fvg_nearest_bearish_midpoint": BaseStrategy._optional_float(htf_fvg_score["nearest_bearish"].get("midpoint")),
                "fvg_1m_bull_score": float(fvg1_score["bull_score"]),
                "fvg_1m_bear_score": float(fvg1_score["bear_score"]),
                "fvg_1m_nearest_bullish_state": str(fvg1_score["nearest_bullish"].get("state", "none")),
                "fvg_1m_nearest_bearish_state": str(fvg1_score["nearest_bearish"].get("state", "none")),
                "fvg_1m_nearest_bullish_midpoint": BaseStrategy._optional_float(fvg1_score["nearest_bullish"].get("midpoint")),
                "fvg_1m_nearest_bearish_midpoint": BaseStrategy._optional_float(fvg1_score["nearest_bearish"].get("midpoint")),
            },
        }

    def _fetch_filtered_contracts(self, client, symbol: str, put_call: str) -> list[OptionContract]:
        today = now_et().date()
        contracts = self._get_cached_option_chain(symbol)
        if contracts is None:
            response = call_schwab_client(client, "option_chains",
                symbol=symbol,
                contractType="ALL",
                strikeCount=12,
                includeUnderlyingQuote=True,
                fromDate=today,
                toDate=today,
            )
            payload = response.json()
            contracts = parse_option_chain(payload, only_dte=0)
            self._set_cached_option_chain(symbol, contracts)
        filtered = filter_contracts(
            contracts,
            put_call=put_call,
            min_volume=self.optcfg.min_option_volume,
            min_open_interest=self.optcfg.min_open_interest,
            max_bid_ask_spread_pct=self.optcfg.max_bid_ask_spread_pct,
        )
        return [c for c in filtered if (c.ask - c.bid) <= float(self.optcfg.max_leg_spread_dollars)]

    def _spread_market_failure_detail(self, first_leg: OptionContract, second_leg: OptionContract) -> str:
        bid, ask, mid = vertical_price_bounds(first_leg, second_leg)
        max_net_spread_price = float(self.optcfg.max_net_spread_price)
        min_net_mid_price = float(self.optcfg.min_net_mid_price)
        max_net_spread_pct = float(self.optcfg.max_net_spread_pct)
        if ask <= 0 or mid <= 0:
            return self._detail_fields(reason="invalid_spread_market", net_bid=bid, net_ask=ask, net_mid=mid)
        if ask > max_net_spread_price:
            return self._detail_fields(reason="net_ask_too_high", required_max_net_ask=max_net_spread_price, current_net_ask=ask, net_bid=bid, net_mid=mid)
        if mid < min_net_mid_price:
            return self._detail_fields(reason="net_mid_too_low", required_min_net_mid=min_net_mid_price, current_net_mid=mid, net_bid=bid, net_ask=ask)
        spread_pct = (ask - bid) / max(mid, 0.01)
        if spread_pct > max_net_spread_pct:
            return self._detail_fields(reason="net_spread_pct_too_wide", required_max_net_spread_pct=max_net_spread_pct, current_net_spread_pct=spread_pct, net_bid=bid, net_ask=ask, net_mid=mid)
        return self._detail_fields(reason="invalid_spread_market", net_bid=bid, net_ask=ask, net_mid=mid)

    def _validate_spread_market(self, first_leg: OptionContract, second_leg: OptionContract) -> tuple[float, float, float] | None:
        bid, ask, mid = vertical_price_bounds(first_leg, second_leg)
        if ask <= 0 or mid <= 0:
            return None
        if ask > float(self.optcfg.max_net_spread_price):
            return None
        if mid < float(self.optcfg.min_net_mid_price):
            return None
        if (ask - bid) / max(mid, 0.01) > float(self.optcfg.max_net_spread_pct):
            return None
        return bid, ask, mid

    def _long_option_style_gate(self, symbol: str, bullish: bool, frame: pd.DataFrame, regime: dict[str, Any], data) -> list[str]:
        p = self.params
        reasons: list[str] = []
        if frame is None or frame.empty:
            return ["insufficient_underlying_bars"]
        last = frame.iloc[-1]
        last_close = self._safe_float(last["close"])
        last_vwap = self._safe_float(last["vwap"], last_close)
        last_ema9 = self._safe_float(last["ema9"], last_close)
        last_ema20 = self._safe_float(last["ema20"], last_close)
        last_ret5 = self._safe_float(last["ret5"], 0.0)
        last_ret15 = self._safe_float(last["ret15"], 0.0)
        vwap_dist = (last_close - last_vwap) / max(last_close, 1.0)
        ema_gap = (last_ema9 - last_ema20) / max(last_close, 1.0)
        scores = regime.get("scores") or {}
        top_score = float(regime.get("scores", {}).get(regime.get("regime"), 0.0) or 0.0)
        ranked = sorted(((str(k), float(v)) for k, v in scores.items()), key=lambda kv: kv[1], reverse=True)
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0

        min_style_score = float(p.get("long_option_min_trend_score", max(float(p.get("min_trend_score", 4.9)), 4.25)))
        min_style_gap = float(p.get("long_option_min_score_gap", max(float(p.get("min_score_gap", 1.6)), 2.10)))
        max_vwap_extension = float(p.get("long_option_max_vwap_extension_pct", max(float(p.get("trend_vwap_distance_pct", 0.0016)) * 2.25, 0.0035)))
        max_ema_extension = float(p.get("long_option_max_ema_gap_pct", max(float(p.get("trend_ema_gap_pct", 0.00075)) * 2.5, 0.0020)))
        max_ret5 = float(p.get("long_option_max_ret5", max(float(p.get("trend_min_ret5", 0.0008)) * 5.0, 0.0025)))
        max_ret15 = float(p.get("long_option_max_ret15", max(float(p.get("trend_min_ret15", 0.0014)) * 5.0, 0.0055)))

        if top_score < min_style_score:
            reasons.append(self._reason_with_values("trend_long_option_low_conviction", current=top_score, required=min_style_score, op=">=", digits=2))
        if (top_score - second_score) < min_style_gap:
            reasons.append(self._reason_with_values("trend_long_option_score_gap_too_small", current=top_score - second_score, required=min_style_gap, op=">=", digits=2))

        sr_ctx = self._sr_context(symbol, frame, data)
        ms_ctx = self._structure_context(frame, "1m")
        if bullish:
            if self._blocks_bullish_structure_entry(ms_ctx):
                reasons.append(self._bullish_structure_block_reason(ms_ctx))
            if self._blocks_bullish_sr_entry(sr_ctx):
                reasons.append(self._bullish_sr_block_reason(sr_ctx))
            if vwap_dist > max_vwap_extension:
                reasons.append(self._reason_with_values("trend_long_option_too_extended_from_vwap", current=vwap_dist, required=max_vwap_extension, op="<=", digits=4))
            if ema_gap > max_ema_extension:
                reasons.append(self._reason_with_values("trend_long_option_ema_gap_too_large", current=ema_gap, required=max_ema_extension, op="<=", digits=4))
            if last_ret5 > max_ret5:
                reasons.append(self._reason_with_values("trend_long_option_short_term_spike", current=last_ret5, required=max_ret5, op="<=", digits=4))
            if last_ret15 > max_ret15:
                reasons.append(self._reason_with_values("trend_long_option_already_extended", current=last_ret15, required=max_ret15, op="<=", digits=4))
        else:
            if self._blocks_bearish_structure_entry(ms_ctx):
                reasons.append(self._bearish_structure_block_reason(ms_ctx))
            if self._blocks_bearish_sr_entry(sr_ctx):
                reasons.append(self._bearish_sr_block_reason(sr_ctx))
            if vwap_dist < -max_vwap_extension:
                reasons.append(self._reason_with_values("trend_long_option_too_extended_from_vwap", current=abs(vwap_dist), required=max_vwap_extension, op="<=", digits=4))
            if ema_gap < -max_ema_extension:
                reasons.append(self._reason_with_values("trend_long_option_ema_gap_too_large", current=abs(ema_gap), required=max_ema_extension, op="<=", digits=4))
            if last_ret5 < -max_ret5:
                reasons.append(self._reason_with_values("trend_long_option_short_term_spike", current=abs(last_ret5), required=max_ret5, op="<=", digits=4))
            if last_ret15 < -max_ret15:
                reasons.append(self._reason_with_values("trend_long_option_already_extended", current=abs(last_ret15), required=max_ret15, op="<=", digits=4))
        return reasons

    def _stabilize_spread_quotes_detailed(self, data, metadata_first: OptionContract, metadata_second: OptionContract) -> tuple[tuple[OptionContract, OptionContract] | None, str | None]:
        if data is None:
            return (metadata_first, metadata_second), None
        symbols = [metadata_first.symbol, metadata_second.symbol]
        checks = max(1, int(self.optcfg.quote_stability_checks))
        mids: list[float] = []
        latest_pair: tuple[OptionContract, OptionContract] | None = None
        for idx in range(checks):
            data.fetch_quotes(symbols, force=True, min_force_interval_seconds=self._option_quote_stability_force_cooldown_seconds(), source="strategies:option_quote_stability_spread")
            if not data.quotes_are_fresh(symbols, self.optcfg.max_quote_age_seconds):
                return None, self._detail_fields(reason="quote_not_fresh", required_max_quote_age_seconds=float(self.optcfg.max_quote_age_seconds), completed_checks=idx, symbols="|".join(symbols))
            q1 = data.get_quote(metadata_first.symbol)
            q2 = data.get_quote(metadata_second.symbol)
            if not q1 or not q2:
                return None, self._detail_fields(reason="missing_leg_quotes", first_symbol=metadata_first.symbol, second_symbol=metadata_second.symbol, first_quote=bool(q1), second_quote=bool(q2), completed_checks=idx)
            first = contract_from_quote(metadata_first.symbol, q1, asdict(metadata_first))
            second = contract_from_quote(metadata_second.symbol, q2, asdict(metadata_second))
            latest_pair = (first, second)
            market = self._validate_spread_market(first, second)
            if market is None:
                return None, self._spread_market_failure_detail(first, second)
            _, _, mid = market
            mids.append(mid)
            if idx + 1 < checks:
                time_mod.sleep(max(0.0, float(self.optcfg.quote_stability_pause_ms) / 1000.0))
        if mids:
            drift = (max(mids) - min(mids)) / max(mids[-1], 0.01)
            if drift > float(self.optcfg.max_mid_drift_pct):
                return None, self._detail_fields(reason="mid_drift_too_high", required_max_mid_drift_pct=float(self.optcfg.max_mid_drift_pct), current_mid_drift_pct=drift, checks=checks)
        return latest_pair, None

    def _stabilize_spread_quotes(self, data, metadata_first: OptionContract, metadata_second: OptionContract) -> tuple[OptionContract, OptionContract] | None:
        pair, _ = self._stabilize_spread_quotes_detailed(data, metadata_first, metadata_second)
        return pair

    def _validate_single_option_market(self, contract: OptionContract) -> tuple[float, float, float] | None:
        bid, ask, mid = single_option_price_bounds(contract)
        if ask <= 0 or mid <= 0:
            return None
        if ask > float(self.optcfg.max_single_option_price):
            return None
        if contract.spread_pct > float(self.optcfg.max_bid_ask_spread_pct):
            return None
        return bid, ask, mid

    def _stabilize_single_option_quote(self, data, metadata_contract: OptionContract) -> OptionContract | None:
        if data is None:
            return metadata_contract
        symbol = metadata_contract.symbol
        checks = max(1, int(self.optcfg.quote_stability_checks))
        mids: list[float] = []
        latest_contract: OptionContract | None = None
        for idx in range(checks):
            data.fetch_quotes([symbol], force=True, min_force_interval_seconds=self._option_quote_stability_force_cooldown_seconds(), source="strategies:option_quote_stability_single")
            if not data.quotes_are_fresh([symbol], self.optcfg.max_quote_age_seconds):
                return None
            q = data.get_quote(symbol)
            if not q:
                return None
            contract = contract_from_quote(symbol, q, asdict(metadata_contract))
            latest_contract = contract
            market = self._validate_single_option_market(contract)
            if market is None:
                return None
            _, _, mid = market
            mids.append(mid)
            if idx + 1 < checks:
                time_mod.sleep(max(0.0, float(self.optcfg.quote_stability_pause_ms) / 1000.0))
        if mids:
            drift = (max(mids) - min(mids)) / max(mids[-1], 0.01)
            if drift > float(self.optcfg.max_mid_drift_pct):
                return None
        return latest_contract

    def _option_final_priority_score(self, candidate: Candidate, regime: dict[str, Any], *, bullish: bool | None = None, rangeish: bool = False) -> float:
        scores = regime.get("scores") if isinstance(regime, dict) else None
        if not isinstance(scores, dict):
            scores = {}
        primary_key = "range" if rangeish else ("bullish_trend" if bullish else "bearish_trend")
        primary = self._safe_float(scores.get(primary_key), 0.0)
        alternatives = [
            self._safe_float(scores.get("bullish_trend"), 0.0),
            self._safe_float(scores.get("bearish_trend"), 0.0),
            self._safe_float(scores.get("range"), 0.0),
        ]
        alternatives.sort(reverse=True)
        runner_up = alternatives[1] if len(alternatives) > 1 else 0.0
        margin = max(0.0, primary - runner_up)
        base = self._safe_float(candidate.activity_score, 0.0)
        return round(base + (primary * 100.0) + (margin * 40.0), 4)

    def _attach_option_final_priority_score(self, signal: Signal, candidate: Candidate, regime: dict[str, Any], *, bullish: bool | None = None, rangeish: bool = False) -> Signal:
        meta = dict(signal.metadata) if isinstance(signal.metadata, dict) else {}
        meta["final_priority_score"] = self._option_final_priority_score(candidate, regime, bullish=bullish, rangeish=rangeish)
        return replace(signal, metadata=meta)

    def _build_debit_spread_signal(self, underlying: str, bullish: bool, client, data, last_underlying: float, style: str, confirm_index: str | None, regime: dict[str, Any]) -> Signal | None:
        put_call = "CALL" if bullish else "PUT"
        contracts = self._fetch_filtered_contracts(client, underlying, put_call)
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
        entry_debit, mark_mid = net_debit_dollars(long_leg, short_leg)
        if entry_debit <= 0:
            self._set_build_failure(underlying, style, "non_positive_entry_debit")
            return None
        stable = self._stabilize_spread_quotes(data, long_leg, short_leg)
        if stable is None:
            self._set_build_failure(underlying, style, "quote_not_stable")
            return None
        long_leg, short_leg = stable
        market = self._validate_spread_market(long_leg, short_leg)
        if market is None:
            self._set_build_failure(underlying, style, "spread_market_invalid")
            return None
        nat_bid, nat_ask, quoted_mid = market
        entry_limit = vertical_limit_price(long_leg, short_leg, mode=self.optcfg.vertical_limit_mode)
        entry_value = entry_limit * 100.0
        # Guard: debit stop must be below entry, target must be above entry
        debit_stop_frac = max(0.01, min(0.99, float(self.optcfg.debit_stop_frac)))
        debit_target_mult = max(1.01, float(self.optcfg.debit_target_mult))
        time_decay_scale = self._compute_time_decay_scale()
        if time_decay_scale < 1.0:
            debit_target_mult = max(1.01, 1.0 + (debit_target_mult - 1.0) * time_decay_scale)
            widen = float(getattr(self.optcfg, "debit_stop_time_decay_widen_factor", 0.30))
            debit_stop_frac = max(0.01, min(0.99, debit_stop_frac * (1.0 + (1.0 - time_decay_scale) * widen)))
        stop = entry_value * debit_stop_frac
        target = entry_value * debit_target_mult
        stop, target = self._clamp_long_premium_levels(entry_value, stop, target)
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
            "mark_price_hint": (quoted_mid * 100.0) if quoted_mid else mark_mid,
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
        return Signal(symbol=underlying, strategy=self.strategy_name, side=Side.LONG, reason=f"{style}_{'bull' if bullish else 'bear'}", stop_price=stop, target_price=target, reference_symbol=confirm_index, metadata=metadata)

    def _build_credit_spread_signal(self, underlying: str, bullish: bool, client, data, last_underlying: float, style: str, confirm_index: str | None, regime: dict[str, Any]) -> Signal | None:
        put_call = "PUT" if bullish else "CALL"
        contracts = self._fetch_filtered_contracts(client, underlying, put_call)
        all_contracts = self._get_cached_option_chain(underlying) or []
        if not contracts:
            self._set_build_failure(
                underlying,
                style,
                self._style_unavailable_reason(
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
                self._style_unavailable_reason(
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
        if getattr(self.optcfg, "credit_distance_gate_enabled", False):
            underlying_atr = getattr(self, "_underlying_atr_cache", {}).get(underlying)
            if underlying_atr is not None and underlying_atr > 0:
                distance_atr = abs(float(short_leg.strike) - last_underlying) / underlying_atr
                min_dist = float(getattr(self.optcfg, "min_credit_distance_atr", 1.8))
                if distance_atr < min_dist:
                    self._set_build_failure(
                        underlying, style,
                        self._style_unavailable_reason(style, f"reason=short_strike_too_close(distance_atr={distance_atr:.2f}<{min_dist})"))
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
                self._style_unavailable_reason(
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
        entry_credit, mark_mid, max_loss = net_credit_dollars(short_leg, long_leg)
        if entry_credit <= 0 or max_loss <= 0:
            self._set_build_failure(
                underlying,
                style,
                self._style_unavailable_reason(
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
        stable, stability_reason = self._stabilize_spread_quotes_detailed(data, short_leg, long_leg)
        if stable is None:
            self._set_build_failure(
                underlying,
                style,
                self._style_unavailable_reason(style, stability_reason or "reason=quote_not_stable"),
            )
            return None
        short_leg, long_leg = stable
        market = self._validate_spread_market(short_leg, long_leg)
        if market is None:
            self._set_build_failure(
                underlying,
                style,
                self._style_unavailable_reason(style, self._spread_market_failure_detail(short_leg, long_leg)),
            )
            return None
        nat_bid, nat_ask, quoted_mid = market
        entry_limit = vertical_limit_price(short_leg, long_leg, mode=self.optcfg.vertical_limit_mode)
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
        stop, target = self._clamp_short_premium_levels(entry_credit_value, stop, target)
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
            "mark_price_hint": (quoted_mid * 100.0) if quoted_mid else mark_mid,
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
        return Signal(symbol=underlying, strategy=self.strategy_name, side=Side.SHORT, reason=f"{style}_{'bull' if bullish else 'bear'}", stop_price=stop, target_price=target, reference_symbol=confirm_index, metadata=metadata)

    def entry_signals(self, candidates: list[Candidate], bars: dict[str, pd.DataFrame], positions: dict[str, Position], client=None, data=None) -> list[Signal]:
        self._reset_entry_decisions()
        if not self._options_enabled() or client is None or data is None:
            return []
        out: list[Signal] = []
        # ATR caches for credit distance gate + adaptive width
        self._underlying_atr_cache.clear()
        self._underlying_ref_atr_cache.clear()
        now_dt = now_et()
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
        for c in candidates:
            reasons: list[str] = []
            if self._underlying_already_open(c.symbol, positions):
                self._record_entry_decision(c.symbol, "skipped", ["underlying_already_open"])
                continue
            frame = bars.get(c.symbol)
            min_bars = int(self.params.get("min_bars", 35))
            if frame is None or len(frame) < min_bars:
                self._record_entry_decision(c.symbol, "skipped", [self._insufficient_bars_reason("insufficient_underlying_bars", 0 if frame is None else len(frame), min_bars)])
                continue
            regime = self._regime_confirm(c, bars, data)
            if not regime.get("ok") or regime.get("no_trade"):
                reasons.append(str(regime.get("reason") or "regime_blocked"))
                reasons.extend([str(r) for r in regime.get("reasons", []) if str(r)])
                self._record_entry_decision(c.symbol, "skipped", reasons)
                continue
            confirm_index = regime.get("confirm_index")
            last = frame.iloc[-1]
            # Populate ATR caches for credit distance gate + adaptive width
            self._underlying_atr_cache[c.symbol] = self._safe_float(last.get("atr14"), 0.0)
            if "atr14" in frame.columns:
                atr_series = frame["atr14"].dropna().tail(20)
                self._underlying_ref_atr_cache[c.symbol] = float(atr_series.median()) if len(atr_series) >= 5 else 0.0
            opening = frame[self._same_day_mask(frame, now_et().date())].between_time("09:30", "09:34")
            regime_name = str(regime.get("regime") or "unknown")
            bullish = regime_name == "bullish_trend"
            bearish = regime_name == "bearish_trend"
            rangeish = regime_name == "range"
            attempted_style = False
            last_close = self._safe_float(last["close"])
            last_vwap = self._safe_float(last["vwap"], last_close)
            last_ret5 = self._safe_float(last["ret5"], 0.0)
            orb_enabled = self._style_enabled("orb_debit_spread")
            orb_window = self._time_in_range(now_t, "09:35", self.params.get("orb_end_time", "10:05"))
            trend_enabled = self._style_enabled("trend_debit_spread")
            trend_window = self._time_in_range(now_t, self.params.get("trend_start_time", "10:05"), self.params.get("trend_end_time", "13:40"))
            credit_enabled = self._style_enabled("midday_credit_spread")
            credit_window = self._time_in_range(now_t, self.params.get("credit_start_time", "11:05"), self.params.get("credit_end_time", "13:45"))
            or_high = self._safe_float(opening["high"].max()) if not opening.empty else None
            or_low = self._safe_float(opening["low"].min()) if not opening.empty else None
            buffer_pct = float(self.params.get("orb_breakout_buffer_pct", 0.0008))
            trend_min_ret5 = float(self.params.get("trend_min_ret5", 0.0007))

            if orb_enabled and orb_window and (bullish or bearish):
                if not opening.empty:
                    if bullish and last_close > self._safe_float(or_high) * (1.0 + buffer_pct) and last_close > last_vwap:
                        attempted_style = True
                        sig = self._build_debit_spread_signal(c.symbol, True, client, data, last_close, "orb_debit_spread", confirm_index, regime)
                        if sig:
                            out.append(self._attach_option_final_priority_score(sig, c, regime, bullish=True, rangeish=False))
                            self._record_entry_decision(c.symbol, "signal", [sig.reason])
                            continue
                        reasons.append(self._consume_build_failure(c.symbol, "orb_debit_spread") or "orb_debit_spread_unavailable")
                    if bearish and last_close < self._safe_float(or_low) * (1.0 - buffer_pct) and last_close < last_vwap:
                        attempted_style = True
                        sig = self._build_debit_spread_signal(c.symbol, False, client, data, last_close, "orb_debit_spread", confirm_index, regime)
                        if sig:
                            out.append(self._attach_option_final_priority_score(sig, c, regime, bullish=False, rangeish=False))
                            self._record_entry_decision(c.symbol, "signal", [sig.reason])
                            continue
                        reasons.append(self._consume_build_failure(c.symbol, "orb_debit_spread") or "orb_debit_spread_unavailable")

            if trend_enabled and trend_window and (bullish or bearish):
                # Trend momentum quality filter — reject if ATR isn't expanding
                # or volume isn't confirming the move.
                momentum_ok = True
                if getattr(self.optcfg, "trend_momentum_filter_enabled", False):
                    atr_current = self._safe_float(last.get("atr14"), 0.0)
                    atr_tail = frame.tail(20)["atr14"].dropna() if "atr14" in frame.columns else pd.Series(dtype=float)
                    atr_mean = float(atr_tail.mean()) if len(atr_tail) > 0 else 0.0
                    atr_expansion = atr_current / max(atr_mean, 1e-9) if atr_mean > 0 else 0.0
                    vol_current = self._safe_float(last.get("volume"), 0.0)
                    vol_tail = frame.tail(10)["volume"].dropna() if "volume" in frame.columns else pd.Series(dtype=float)
                    vol_mean = float(vol_tail.mean()) if len(vol_tail) > 0 else 1.0
                    volume_ratio = vol_current / max(vol_mean, 1.0)
                    min_atr_exp = float(getattr(self.optcfg, "trend_min_atr_expansion", 0.85))
                    min_vol_ratio = float(getattr(self.optcfg, "trend_min_volume_ratio", 0.90))
                    if atr_expansion < min_atr_exp or volume_ratio < min_vol_ratio:
                        momentum_ok = False
                        reasons.append(f"trend_momentum_filter(atr_exp={atr_expansion:.3f}<{min_atr_exp},vol_ratio={volume_ratio:.3f}<{min_vol_ratio})")
                if momentum_ok and bullish and last_ret5 >= trend_min_ret5:
                    attempted_style = True
                    sig = self._build_debit_spread_signal(c.symbol, True, client, data, last_close, "trend_debit_spread", confirm_index, regime)
                    if sig:
                        out.append(self._attach_option_final_priority_score(sig, c, regime, bullish=True, rangeish=False))
                        self._record_entry_decision(c.symbol, "signal", [sig.reason])
                        continue
                    reasons.append(self._consume_build_failure(c.symbol, "trend_debit_spread") or "trend_debit_spread_unavailable")
                if momentum_ok and bearish and last_ret5 <= -trend_min_ret5:
                    attempted_style = True
                    sig = self._build_debit_spread_signal(c.symbol, False, client, data, last_close, "trend_debit_spread", confirm_index, regime)
                    if sig:
                        out.append(self._attach_option_final_priority_score(sig, c, regime, bullish=False, rangeish=False))
                        self._record_entry_decision(c.symbol, "signal", [sig.reason])
                        continue
                    reasons.append(self._consume_build_failure(c.symbol, "trend_debit_spread") or "trend_debit_spread_unavailable")

            if credit_enabled and credit_window and rangeish:
                attempted_style = True
                bullish_credit = last_close >= last_vwap
                sig = self._build_credit_spread_signal(c.symbol, bullish_credit, client, data, last_close, "midday_credit_spread", confirm_index, regime)
                if sig:
                    out.append(self._attach_option_final_priority_score(sig, c, regime, bullish=bullish_credit, rangeish=True))
                    self._record_entry_decision(c.symbol, "signal", [sig.reason])
                    continue
                reasons.append(self._consume_build_failure(c.symbol, "midday_credit_spread") or "midday_credit_spread_unavailable")

            final_reasons = reasons or ([
                self._no_style_trigger_reason(
                    regime_name=regime_name,
                    bullish=bullish,
                    bearish=bearish,
                    rangeish=rangeish,
                    orb_enabled=orb_enabled,
                    orb_window=orb_window,
                    trend_enabled=trend_enabled,
                    trend_window=trend_window,
                    credit_enabled=credit_enabled,
                    credit_window=credit_window,
                    last_close=last_close,
                    last_vwap=last_vwap,
                    last_ret5=last_ret5,
                    trend_min_ret5=trend_min_ret5,
                    or_high=or_high,
                    or_low=or_low,
                    orb_buffer_pct=buffer_pct,
                )
            ] if not attempted_style else ["no_contract_selected"])
            self._record_entry_decision(c.symbol, "skipped", final_reasons)
        return out

    def should_force_flatten(self, position: Position) -> bool:
        event = self._matching_event_blackout(now_et())
        if event and bool(event.get("force_flatten", False)):
            return True
        now_dt = now_et()
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
        if position.strategy not in {self.strategy_name, 'zero_dte_etf_long_options'}:
            return None
        asset_type = position.metadata.get("asset_type")
        if asset_type != ASSET_TYPE_OPTION_VERTICAL:
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
        p1 = self._positive_quote_value(q1, "mid", "mark", "last")
        p2 = self._positive_quote_value(q2, "mid", "mark", "last")
        if p1 is None or p2 is None:
            return None
        return max(0.0, (p1 - p2) * 100.0)
