# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from copy import deepcopy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from .candles import (
    DEFAULT_BEARISH_PATTERNS,
    DEFAULT_BULLISH_PATTERNS,
    candle_allowed_tokens,
    candle_group_tokens,
    invalid_allowed_patterns,
)
from .chart_patterns import (
    DEFAULT_BEARISH_CHART_PATTERNS,
    DEFAULT_BULLISH_CHART_PATTERNS,
    chart_pattern_allowed_tokens,
    chart_pattern_group_tokens,
    invalid_allowed_chart_patterns,
)
from .models import PairDefinition
from ._strategies.registry import (
    default_strategy_name,
    get_plugins,
    is_option_strategy,
    normalize_strategy_name,
    normalize_strategy_params as apply_strategy_param_normalizer,
    plugin_names,
)
from .utils import build_schedule, set_runtime_indicator_mode, set_runtime_timezone

LOG = logging.getLogger(__name__)


# Placeholder values treated as "not set" so the env-var fallback kicks in.
# Matches the strings shipped in configs/config.example.yaml historically.
_SECRET_PLACEHOLDERS: frozenset[str] = frozenset({
    "",
    "YOUR_APP_KEY",
    "YOUR_APP_SECRET",
    "YOUR_SESSIONID",
    "CHANGEME",
})

_LOADED_DOTENV_PATHS: set[Path] = set()


def _load_dotenv(config_path: Path, explicit_env_path: Path | None = None) -> None:
    """Populate os.environ from a .env file if one exists.

    If ``explicit_env_path`` is provided (e.g. via the ``--env`` CLI flag),
    that file is loaded with priority. If it's set but missing, a hard
    error is raised — the user explicitly asked for it, so silently
    falling back would hide the typo.

    Otherwise, searches the config file's parent directory, (if under
    ``configs/``) the repo root, and finally the current working
    directory. Only sets keys that aren't already present in the
    environment — process env always wins over file values, so a .env
    never silently overrides something the user set explicitly. Each
    resolved path is parsed at most once per process.
    """
    if explicit_env_path is not None:
        path = Path(explicit_env_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(
                f".env file not found: {path}. Pass --env with a valid path or omit it to auto-discover."
            )
        resolved = path.resolve()
        if resolved in _LOADED_DOTENV_PATHS:
            return
        _LOADED_DOTENV_PATHS.add(resolved)
        try:
            _parse_dotenv_into_environ(path)
            LOG.info("Loaded .env from %s (explicit --env)", path)
        except OSError as exc:
            LOG.warning("Failed to read .env at %s: %s", path, exc)
        return

    candidates: list[Path] = []
    config_parent = config_path.resolve().parent
    candidates.append(config_parent / ".env")
    # If config lives in configs/, also look one level up (repo root).
    if config_parent.name == "configs":
        candidates.append(config_parent.parent / ".env")
    cwd_env = Path.cwd() / ".env"
    if cwd_env not in candidates:
        candidates.append(cwd_env)

    for path in candidates:
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in _LOADED_DOTENV_PATHS:
            return  # already processed this .env in a prior call
        # Mark as loaded before parsing so a malformed file isn't retried.
        _LOADED_DOTENV_PATHS.add(resolved)
        try:
            _parse_dotenv_into_environ(path)
            LOG.debug("Loaded .env from %s", path)
        except OSError as exc:
            LOG.warning("Failed to read .env at %s: %s", path, exc)
        return


def _parse_dotenv_into_environ(path: Path) -> None:
    """Minimal .env parser: KEY=VALUE per line, # for comments.

    - Reads with utf-8-sig so a BOM (Windows Notepad default) is stripped.
    - Handles CRLF line endings via str.splitlines.
    - Supports optional 'export' prefix.
    - Quoted values (single or double) are unwrapped; anything after the
      closing quote (e.g. a trailing comment) is discarded.
    - Unquoted values honor inline ' #' as a trailing comment delimiter.
    - Only sets keys that aren't already in os.environ, so the process
      environment always wins over file values.
    No variable interpolation, no multi-line values — intentionally small
    to avoid depending on python-dotenv.
    """
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if value and value[0] in {"'", '"'}:
            # Quoted value: content is between the first and matching
            # next quote of the same type. Trailing text (comments or
            # whitespace) is discarded.
            quote = value[0]
            end = value.find(quote, 1)
            if end > 0:
                value = value[1:end]
            # Unclosed quote: leave value as-is; the user will see the
            # literal and notice the typo when auth fails.
        else:
            # Unquoted: strip inline ' #' comments.
            comment_idx = value.find(" #")
            if comment_idx >= 0:
                value = value[:comment_idx].rstrip()
        if key not in os.environ:
            os.environ[key] = value


def _resolve_secret(yaml_value: Any, env_var: str) -> str | None:
    """Return a secret from yaml if real, else from the named env var.

    Placeholder values (empty string, YOUR_APP_KEY, etc.) are treated as
    unset so the env fallback applies. Returns None if neither source has a
    real value.
    """
    if isinstance(yaml_value, str):
        candidate = yaml_value.strip()
        if candidate and candidate not in _SECRET_PLACEHOLDERS:
            return candidate
    env_value = os.environ.get(env_var, "").strip()
    if env_value and env_value not in _SECRET_PLACEHOLDERS:
        return env_value
    return None


def _validate_token_list(
    *,
    config_path: Path,
    section: str,
    field_name: str,
    raw_value: Any,
    invalid_tokens: list[str],
    group_tokens: tuple[str, ...],
    allowed_tokens: tuple[str, ...],
) -> None:
    if raw_value is None:
        return
    if not isinstance(raw_value, list):
        raise TypeError(f"{config_path}:{section}.{field_name} must be a YAML list when present")
    if not invalid_tokens:
        return
    preview = ", ".join(allowed_tokens[:20])
    remainder = max(0, len(allowed_tokens) - 20)
    if remainder:
        preview += f", ... (+{remainder} more)"
    raise ValueError(
        f"{config_path}:{section}.{field_name} contains unsupported token(s): {', '.join(invalid_tokens)}. "
        f"Allowed group tokens: {', '.join(group_tokens)}. Allowed specific tokens include: {preview}"
    )


def _validate_pattern_config(config_path: Path, candles_raw: dict[str, Any], chart_patterns_raw: dict[str, Any]) -> None:
    _validate_token_list(
        config_path=config_path,
        section="candles",
        field_name="bullish_patterns",
        raw_value=candles_raw.get("bullish_patterns"),
        invalid_tokens=invalid_allowed_patterns(candles_raw.get("bullish_patterns"), bullish=True),
        group_tokens=candle_group_tokens(bullish=True),
        allowed_tokens=candle_allowed_tokens(bullish=True),
    )
    _validate_token_list(
        config_path=config_path,
        section="candles",
        field_name="bearish_patterns",
        raw_value=candles_raw.get("bearish_patterns"),
        invalid_tokens=invalid_allowed_patterns(candles_raw.get("bearish_patterns"), bullish=False),
        group_tokens=candle_group_tokens(bullish=False),
        allowed_tokens=candle_allowed_tokens(bullish=False),
    )
    _validate_token_list(
        config_path=config_path,
        section="chart_patterns",
        field_name="bullish_patterns",
        raw_value=chart_patterns_raw.get("bullish_patterns"),
        invalid_tokens=invalid_allowed_chart_patterns(chart_patterns_raw.get("bullish_patterns"), bullish=True),
        group_tokens=chart_pattern_group_tokens(bullish=True),
        allowed_tokens=chart_pattern_allowed_tokens(bullish=True),
    )
    _validate_token_list(
        config_path=config_path,
        section="chart_patterns",
        field_name="bearish_patterns",
        raw_value=chart_patterns_raw.get("bearish_patterns"),
        invalid_tokens=invalid_allowed_chart_patterns(chart_patterns_raw.get("bearish_patterns"), bullish=False),
        group_tokens=chart_pattern_group_tokens(bullish=False),
        allowed_tokens=chart_pattern_allowed_tokens(bullish=False),
    )

__all__ = [
    "available_strategy_names",
    "load_config",
    "BotConfig",
    "SchwabConfig",
    "TradingViewConfig",
    "RuntimeConfig",
    "DashboardConfig",
    "DashboardChartConfig",
    "DashboardChartingConfig",
    "RiskConfig",
]


def available_strategy_names() -> list[str]:
    return list(plugin_names())


@dataclass(slots=True)
class SchwabConfig:
    # Credentials default to empty string so they can be supplied via the
    # .env file (SCHWAB_APP_KEY / SCHWAB_APP_SECRET). load_config() enforces
    # that they are populated by either yaml or env before the bot starts.
    app_key: str = ""
    app_secret: str = ""
    callback_url: str = "https://127.0.0.1"
    tokens_db: str = ".schwabdev/tokens.db"
    encryption: str | None = None
    timeout: int = 10
    account_hash: str | None = None
    dry_run: bool = True


@dataclass(slots=True)
class TradingViewConfig:
    sessionid: str | None = None
    market: str = "america"
    max_candidates: int = 5
    screener_refresh_seconds: int = 90
    min_market_cap: float = 30_000_000
    max_market_cap: float = 2_000_000_000
    min_volume: int = 750_000
    min_value_traded_1m: float = 150_000.0
    min_volume_1m: int = 25_000


@dataclass(slots=True)
class RiskConfig:
    max_positions: int = 2
    # Dollar risk per trade is computed as max_notional_per_trade *
    # risk_per_trade_frac_of_notional. Example: with max_notional_per_trade
    # = $16,000 and risk_per_trade_frac_of_notional = 0.008, each trade
    # risks at most $128 (16000 * 0.008). This is a fraction of the
    # per-trade notional cap, NOT a fraction of account equity.
    risk_per_trade_frac_of_notional: float = 0.0040
    max_notional_per_trade: float = 4000.0
    max_total_notional: float = 8000.0
    max_daily_loss: float = 400.0
    default_stop_pct: float = 0.018
    default_target_pct: float = 0.038
    trailing_stop_pct: float = 0.014
    trade_management_mode: str = "adaptive_ladder"
    allow_short: bool = False
    cooldown_minutes: int = 20
    reentry_policy: str = "cooldown"
    # Direction-aware cooldown: a LONG exit only blocks LONG re-entries on the
    # same symbol; SHORTs are still allowed immediately (and vice versa).
    # Session 2026-04-17 had 3 LONG NVDA entries within 50min at ~$200.94,
    # each stopped out on structure, net -$94. A direction-aware cooldown
    # still lets the bot flip short if genuine bearish reversal develops.
    cooldown_direction_aware: bool = True
    # Same-level retry block: after a stop-out, block *same-direction* re-entry
    # on the same symbol within same_level_block_atr_mult * ATR of the prior
    # stop price for same_level_block_minutes minutes. Prevents the NVDA-style
    # breakout-chase pattern. Fib-pullback entries (see _fib_pullback_override)
    # can override the block.
    same_level_block_minutes: int = 30
    same_level_block_atr_mult: float = 0.3
    # Time-stop: scratch a trade held too long without meaningful price
    # movement. 2026-04-17 META held 223 min for +$0.16 on EQL exit — dead
    # capital blocking a slot. 0 = disabled.
    time_stop_minutes: int = 45
    time_stop_min_return_pct: float = 0.003
    # Peak-giveback floor: once the trade's max_favorable_r (peak R since
    # entry) crosses peak_giveback_min_r, force an exit when current_r
    # retraces past a tiered fraction of the peak. The fraction widens as
    # the peak grows so larger runs get more room: at 1R peak, retrace
    # below 50% of peak fires; at 2R peak, below 60%; at 3R+ peak, below 70%.
    # Complements Fix 4 (protective BE at +0.5R): BE catches 0.5-1R winners,
    # this catches 1R+ runners that give back too much. 2026-04-17 this
    # would have locked INTC +$95 → +$47 instead of -$3, AMZN +$62 → +$31
    # instead of -$13, AMD +$31 → +$15 instead of -$30. Net modeled
    # improvement ~+$135 on the session (alongside BE fix). Set
    # peak_giveback_min_r=0 to disable entirely.
    peak_giveback_enabled: bool = True
    peak_giveback_min_r: float = 1.0


@dataclass(slots=True)
class RuntimeConfig:
    timezone: str = "America/New_York"
    loop_sleep_seconds: float = 2.0
    history_poll_seconds: int = 150
    quote_poll_seconds: int = 6
    quote_cache_seconds: int = 6
    quote_batch_size: int = 20
    history_lookback_minutes: int = 390
    use_extended_hours_history: bool = True
    use_rth_session_indicators: bool = True
    warmup_minutes: int = 90
    prewarm_before_windows_minutes: int = 5
    log_dir: str = ".logs"
    stream_fields: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6, 7, 8])
    stream_connect_timeout_seconds: int = 20
    stream_fallback_poll_seconds: int = 25
    stream_stale_fallback_seconds: int = 60
    stream_health_log_seconds: int = 90
    reconcile_on_startup: bool = True
    startup_reconcile_mode: str = "block"
    startup_order_lookback_days: int = 2
    startup_reconcile_ignore_symbols: list[str] = field(default_factory=list)
    startup_reconcile_metadata_db_path: str = ".logs/startup_reconcile_metadata.sqlite"
    auto_exit_after_session: bool = False
    cycle_precompute_workers: int = 4
    # Per-symbol quote-fetch failure threshold. When a symbol fails this
    # many consecutive quote fetches (typically Schwab 401/403/404), it
    # is blacklisted from quote refresh for the remainder of the session.
    # Recovers on bot restart. Counter resets on any successful fetch.
    # Set to 0 to disable (always retry — pre-2026-04-29 behavior). The
    # default 5 catches symbol-specific permission errors (e.g. restricted
    # securities like KNRX 2026-04-29: 457 wasted 401 retries) without
    # blacklisting on transient hiccups.
    max_consecutive_quote_failures: int = 5
    # When True, on session shutdown the engine writes a per-day archive
    # to {log_dir}/sessions/{YYYY-MM-DD}/ containing: bars/{SYMBOL}.csv for
    # every active watchlist symbol (RTH only, with indicators), trades.csv
    # filtered to the day, and manifest.json with strategy + summary stats.
    # Useful for trade audits and post-session analysis. Disable to save
    # disk space if running without dashboard/analysis needs.
    export_session_archive: bool = True


@dataclass(slots=True)
class PaperConfig:
    starting_equity: float = 25_000.0
    max_equity_points: int = 2000
    max_trade_history: int = 200


@dataclass(slots=True)
class DashboardChartConfig:
    max_bars: int = 90
    show_volume: bool = False
    show_moving_averages: bool = True
    show_vwap: bool = True
    show_support_resistance: bool = True
    show_next_support_resistance: bool = True
    show_full_support_resistance_ladder: bool = False
    show_key_level_zones: bool = True
    show_key_level_zone_labels: bool = True
    show_bollinger_bands: bool = False
    show_anchored_vwap: bool = False
    show_fib_extensions: bool = False
    show_channel: bool = False
    show_trendlines: bool = False
    show_htf_fair_value_gaps: bool = False
    show_1m_fair_value_gaps: bool = False
    show_trade_markers: bool = True
    tooltip_show_returns: bool = True
    tooltip_show_support_resistance: bool = True
    tooltip_show_structure: bool = True
    tooltip_show_volatility: bool = True
    tooltip_show_orderflow: bool = True
    tooltip_show_patterns: bool = True


@dataclass(slots=True)
class DashboardChartingConfig:
    compact_chart_timeframe: str = "ltf"
    compact: DashboardChartConfig = field(default_factory=DashboardChartConfig)
    expanded: DashboardChartConfig = field(
        default_factory=lambda: DashboardChartConfig(
            max_bars=360,
            show_volume=True,
            show_full_support_resistance_ladder=True,
            show_bollinger_bands=True,
            show_anchored_vwap=True,
            show_fib_extensions=True,
        )
    )

    @staticmethod
    def _normalize_max_bars(value: Any, fallback: int) -> int:
        try:
            return max(1, min(int(value or fallback), 480))
        except Exception:
            return fallback

    @staticmethod
    def _normalize_chart_timeframe(value: Any) -> str:
        return "htf" if str(value or "ltf").strip().lower() == "htf" else "ltf"

    @classmethod
    def _normalize_profile(cls, cfg: DashboardChartConfig | None, *, fallback_max_bars: int) -> DashboardChartConfig:
        if cfg is None:
            cfg = DashboardChartConfig(max_bars=fallback_max_bars)
        values = asdict(cfg)
        normalized: dict[str, Any] = {}
        for key, value in list(values.items()):
            if key == "max_bars":
                normalized[key] = cls._normalize_max_bars(value, fallback_max_bars)
            else:
                normalized[key] = bool(value)
        return DashboardChartConfig(**normalized)

    def resolved_profile(self, mode: str) -> DashboardChartConfig:
        profile_name = "expanded" if str(mode or "").lower() == "expanded" else "compact"
        fallback_max_bars = 360 if profile_name == "expanded" else 90
        return self._normalize_profile(getattr(self, profile_name, None), fallback_max_bars=fallback_max_bars)

    def normalized_compact_chart_timeframe(self) -> str:
        return self._normalize_chart_timeframe(self.compact_chart_timeframe)


@dataclass(slots=True)
class DashboardConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    refresh_ms: int = 2000
    state_path: str = ".logs/dashboard_state.json"
    theme: str = "default"
    https: bool = False
    ssl_certfile: str = ""
    ssl_keyfile: str = ""
    charting: DashboardChartingConfig = field(default_factory=DashboardChartingConfig)


@dataclass(slots=True)
class EquityExecutionConfig:
    entry_limit_min_buffer: float = 0.03
    entry_limit_max_buffer: float = 0.05
    entry_limit_spread_frac: float = 0.10
    entry_live_fill_timeout_seconds: float = 3.0
    entry_live_poll_seconds: float = 0.5
    entry_live_reprice_attempts: int = 1
    entry_live_reprice_step_frac: float = 0.50
    extended_hours_enabled: bool = True
    market_exit_regular_hours: bool = True


@dataclass(slots=True)
class CandlesConfig:
    bullish_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_BULLISH_PATTERNS))
    bearish_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_BEARISH_PATTERNS))
    # Minimum opposing net_score required to block entry / fire exit. 0.70
    # matches the "solid" confirm tier (2+ corroborating candles) — below
    # this is one-candle noise. Range is 0.0 (any opposing match) to 1.0+
    # (only fully-confirmed strong clusters). Toggles for using this
    # threshold are shared_entry.use_opposing_candle_filter and
    # shared_exit.use_candle_pattern_exit.
    opposing_net_score_threshold: float = 0.70


@dataclass(slots=True)
class ChartPatternsConfig:
    enabled: bool = True
    lookback_bars: int = 32
    bullish_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_BULLISH_CHART_PATTERNS))
    bearish_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_BEARISH_CHART_PATTERNS))


@dataclass(slots=True)
class SupportResistanceConfig:
    enabled: bool = True
    timeframe_minutes: int = 15
    lookback_days: int = 10
    refresh_seconds: int = 480
    pivot_span: int = 2
    max_levels_per_side: int = 3
    atr_tolerance_mult: float = 0.60
    pct_tolerance: float = 0.0030
    same_side_min_gap_atr_mult: float = 0.10
    same_side_min_gap_pct: float = 0.0015
    fallback_reference_max_drift_atr_mult: float = 1.0
    fallback_reference_max_drift_pct: float = 0.01
    proximity_atr_mult: float = 0.70
    breakout_atr_mult: float = 0.30
    breakout_buffer_pct: float = 0.0012
    stop_buffer_atr_mult: float = 0.25
    entry_min_clearance_atr: float = 0.85
    entry_min_clearance_pct: float = 0.0038
    entry_proximity_scoring_enabled: bool = True
    entry_bias_score_weight: float = 0.50
    entry_favorable_proximity_bonus: float = 0.30
    entry_opposing_proximity_penalty: float = 0.30
    use_prior_day_high_low: bool = True
    use_prior_week_high_low: bool = True
    htf_fair_value_gaps_enabled: bool = True
    htf_fair_value_gap_max_per_side: int = 3
    htf_fair_value_gap_min_atr_mult: float = 0.06
    htf_fair_value_gap_min_pct: float = 0.0006
    one_minute_fair_value_gaps_enabled: bool = True
    one_minute_fair_value_gap_max_per_side: int = 3
    one_minute_fair_value_gap_min_atr_mult: float = 0.06
    one_minute_fair_value_gap_min_pct: float = 0.0006
    dashboard_flip_confirmation_1m_bars: int = 1
    trading_flip_confirmation_1m_bars: int = 2
    trading_flip_confirmation_5m_bars: int = 1
    flip_stop_buffer_atr_mult: float = 0.25
    flip_target_requires_momentum_confirm: bool = True
    regime_weight: float = 0.70
    structure_enabled: bool = True
    structure_1m_pivot_span: int = 2
    structure_eq_atr_mult: float = 0.25
    structure_1m_weight: float = 0.65
    structure_htf_weight: float = 0.85
    structure_event_lookback_bars: int = 6
    # Grace window post-entry during which 1m structure-based exits
    # (structure_bearish_exit:EQL/LL/HL, structure_bullish_exit:HH/LH) are
    # suppressed. An EQL pivot forming in the first few minutes after entry
    # is noise, not reversal — session 2026-04-17 had 1W/12T on structure
    # exits, net -$354. CHoCH exits and SR-break exits still fire in the
    # grace window (those are genuine reversal signals, not minor pivots).
    structure_exit_grace_minutes: int = 10
    # Minimum new 1m pivots formed AFTER entry before structure exits can
    # fire. If ms1m_pivot_count at exit-check time - at-entry time is
    # below this, exit is suppressed. Complements the time-grace by
    # requiring at least some actual structure to form.
    structure_exit_min_post_entry_pivots: int = 2
    # Extended grace window for positions opened during the ORB window
    # (09:35-orb_end). ORB pullbacks often look like bearish structure
    # breaks / bearish chart patterns but continue higher afterward.
    # 2026-04-24: 5 of 6 ORB entries lost via pullback-driven exits
    # (INTC at 2.0m, AMD at 11.2m via structure_bearish_exit:HL, etc).
    # When set > 0, suppresses both structure_bearish/bullish_exit AND
    # chart_pattern_exit for the first N minutes of ORB-window trades.
    # CHoCH exits still fire (genuine trend reversals). Set 0 to disable.
    orb_entry_exit_grace_minutes: int = 20


@dataclass(slots=True)
class TechnicalLevelsConfig:
    enabled: bool = True
    fib_enabled: bool = True
    fib_lookback_bars: int = 120
    fib_min_impulse_atr: float = 1.25
    fib_near_extension_pct: float = 0.0055
    anchored_vwap_impulse_lookback_bars: int | None = None
    anchored_vwap_min_impulse_atr: float | None = None
    anchored_vwap_pivot_span: int | None = None
    channel_enabled: bool = True
    channel_lookback_bars: int = 120
    channel_min_touches: int = 3
    channel_atr_tolerance_mult: float = 0.35
    channel_parallel_slope_frac: float = 0.12
    channel_min_gap_atr_mult: float = 0.80
    channel_min_gap_pct: float = 0.0025
    channel_near_edge_pct: float = 0.16
    trendline_enabled: bool = True
    trendline_lookback_bars: int = 120
    trendline_min_touches: int = 3
    trendline_atr_tolerance_mult: float = 0.35
    trendline_breakout_buffer_atr_mult: float = 0.15
    adx_enabled: bool = True
    adx_length: int = 14
    adx_min_strength: float = 18.0
    adx_entry_bonus: float = 0.20
    adx_rising_bonus: float = 0.08
    adx_weak_penalty: float = 0.12
    anchored_vwap_enabled: bool = True
    anchored_vwap_entry_bonus: float = 0.20
    anchored_vwap_entry_penalty: float = 0.18
    atr_context_enabled: bool = True
    atr_expansion_lookback: int = 5
    atr_expansion_min_mult: float = 0.80
    atr_expansion_bonus: float = 0.12
    atr_stretch_penalty_mult: float = 2.60
    atr_stretch_penalty: float = 0.20
    obv_enabled: bool = True
    obv_ema_length: int = 20
    obv_entry_bonus: float = 0.10
    obv_entry_penalty: float = 0.08
    divergence_enabled: bool = True
    divergence_rsi_length: int = 14
    divergence_rsi_min_delta: float = 2.5
    divergence_obv_min_volume_frac: float = 0.65
    divergence_counter_rsi_penalty: float = 0.12
    divergence_counter_obv_penalty: float = 0.10
    divergence_block_dual_counter: bool = True
    bollinger_enabled: bool = True
    bollinger_length: int = 20
    bollinger_std_mult: float = 2.0
    bollinger_squeeze_width_pct: float = 0.060
    bollinger_entry_bonus_midband: float = 0.16
    bollinger_entry_penalty_outer_band: float = 0.22
    target_use_bollinger: bool = False
    target_use_fib: bool = True
    target_use_channel: bool = True
    target_use_trendline: bool = True
    stop_use_trendline: bool = True
    entry_bonus_channel_alignment: float = 0.20
    entry_bonus_trendline_respect: float = 0.20
    entry_penalty_near_extension: float = 0.40


@dataclass(slots=True)
class SharedEntryLogicConfig:
    use_fvg_context: bool = True
    use_divergence_filter: bool = True
    use_technical_entry_adjustment: bool = True
    use_technical_stop_target_refinement: bool = True
    use_structure_filter: bool = True
    use_sr_filter: bool = True
    use_sr_stop_target_refinement: bool = True
    use_opposing_chart_filter: bool = True
    # Block entries when opposing-direction candle patterns cluster above
    # candles.opposing_net_score_threshold. Uses the cached candle context
    # (no extra ta-lib calls). See `_directional_candle_signal` entry block
    # in top_tier_adaptive/strategy.py.
    use_opposing_candle_filter: bool = False
    # Minimum risk-to-reward floor enforced by the SR/technical-level
    # target refinement pipeline. When a refine pass would cap the target
    # so close to entry that R:R drops below this value, the cap is
    # rejected and the strategy's original target is kept. Protects
    # against the "$0.10 target" bug where nearby S/R levels collapse R:R
    # toward zero. Default 1.0 = require at least 1:1 R:R after any
    # refinement.
    min_target_rr: float = 1.0


@dataclass(slots=True)
class SharedExitLogicConfig:
    use_technical_exit: bool = True
    use_trendline_break: bool = True
    use_channel_break: bool = True
    use_bollinger_reject: bool = False
    use_anchored_vwap_loss: bool = True
    use_chart_pattern_exit: bool = False
    # Fire candle_pattern_exit when an opposing-direction candle cluster
    # crosses candles.opposing_net_score_threshold and the tape confirms
    # (confirm_with_ema9/ema20/vwap/close_position below). Reuses the
    # cached candle context — no extra ta-lib calls.
    use_candle_pattern_exit: bool = False
    use_structure_exit: bool = True
    use_sr_loss_exit: bool = True
    confirm_with_ema9: bool = True
    confirm_with_ema20: bool = True
    confirm_with_vwap: bool = True
    confirm_with_close_position: bool = True
    bullish_close_position_max: float = 0.46
    bearish_close_position_min: float = 0.54
    bullish_close_position_loose_max: float = 0.38
    bearish_close_position_loose_min: float = 0.62


@dataclass(slots=True)
class ZeroDteOptionsConfig:
    enabled: bool = True
    underlyings: list[str] = field(default_factory=lambda: ["SPY", "QQQ"])
    confirmation_symbols: dict[str, str] = field(default_factory=lambda: {"SPY": "$SPX", "QQQ": "$COMPX", "IWM": "$RUT"})
    volatility_symbol: str = "VIX"
    styles: list[str] = field(default_factory=lambda: ["orb_debit_spread", "trend_debit_spread", "midday_credit_spread", "orb_long_option", "trend_long_option"])
    min_underlying_price: float = 100.0
    min_option_volume: int = 300
    min_open_interest: int = 600
    max_bid_ask_spread_pct: float = 0.10
    max_leg_spread_dollars: float = 0.08
    max_net_spread_pct: float = 0.20
    max_net_spread_price: float = 2.80
    min_net_mid_price: float = 0.25
    target_long_delta: float = 0.38
    target_short_delta: float = 0.23
    target_single_delta: float = 0.28
    max_single_option_price: float = 2.25
    option_limit_mode: str = "mid"
    strike_width_by_symbol: dict[str, float] = field(default_factory=lambda: {"SPY": 2.0, "QQQ": 2.0, "IWM": 1.0})
    max_contracts_per_trade: int = 1
    max_loss_per_trade: float = 200.0
    debit_stop_frac: float = 0.45
    debit_target_mult: float = 1.45
    credit_stop_mult: float = 1.65
    credit_target_frac: float = 0.32
    single_stop_frac: float = 0.38
    single_target_mult: float = 1.50
    force_flatten_time: str = "15:18"
    max_vix: float = 22.5
    vix_spike_pct: float = 0.0110
    vertical_limit_mode: str = "mid"
    quote_stability_checks: int = 3
    quote_stability_pause_ms: int = 500
    max_mid_drift_pct: float = 0.06
    max_quote_age_seconds: int = 6
    dry_run_replace_attempts: int = 2
    dry_run_step_frac: float = 0.25
    event_blackout_file: str | None = "./macro_events.auto.yaml"
    event_blackouts: list[dict[str, Any]] = field(default_factory=list)
    option_chain_cache_seconds: int = 6
    option_chain_cache_max_entries: int = 24
    # --- Options premium ratchet (post-entry stop management) ---
    options_breakeven_enabled: bool = False
    options_breakeven_mark_mult: float = 1.25
    options_breakeven_stop_mult: float = 1.05
    options_profit_lock_enabled: bool = False
    options_profit_lock_mark_mult: float = 1.40
    options_profit_lock_stop_mult: float = 1.15
    # --- Time-decay-aware stop/target scaling ---
    debit_target_time_decay_enabled: bool = False
    debit_target_time_decay_start: str = "10:30"
    debit_target_time_decay_end: str = "14:00"
    debit_target_time_decay_min_scale: float = 0.70
    debit_stop_time_decay_widen_factor: float = 0.30
    # --- Time-aware delta selection ---
    delta_time_shift_enabled: bool = False
    delta_time_shift_per_hour: float = 0.025
    delta_time_shift_max: float = 0.15
    delta_time_shift_start: str = "10:00"
    # --- Trend entry momentum filter ---
    trend_momentum_filter_enabled: bool = False
    trend_min_atr_expansion: float = 0.85
    trend_min_volume_ratio: float = 0.90
    # --- Credit strike distance gate ---
    credit_distance_gate_enabled: bool = False
    min_credit_distance_atr: float = 1.8
    # --- VIX-adaptive strike width ---
    adaptive_width_enabled: bool = False
    adaptive_width_max_scale: float = 1.5


@dataclass(slots=True)
class StrategyConfig:
    name: str
    entry_windows: list[tuple[str, str]]
    management_windows: list[tuple[str, str]]
    screener_windows: list[tuple[str, str]]
    params: dict[str, Any] = field(default_factory=dict)

    def schedule(self):
        return build_schedule(self.entry_windows, self.management_windows, self.screener_windows)


@dataclass(slots=True)
class BotConfig:
    strategy: str
    schwab: SchwabConfig
    tradingview: TradingViewConfig
    risk: RiskConfig
    runtime: RuntimeConfig
    paper: PaperConfig
    dashboard: DashboardConfig
    execution: EquityExecutionConfig
    candles: CandlesConfig
    chart_patterns: ChartPatternsConfig
    support_resistance: SupportResistanceConfig
    technical_levels: TechnicalLevelsConfig
    options: ZeroDteOptionsConfig
    strategies: dict[str, StrategyConfig]
    shared_entry: SharedEntryLogicConfig = field(default_factory=SharedEntryLogicConfig)
    shared_exit: SharedExitLogicConfig = field(default_factory=SharedExitLogicConfig)
    pairs: list[PairDefinition] = field(default_factory=list)

    @property
    def active_strategy(self) -> StrategyConfig:
        return self.strategies[self.strategy]


def _strategy_defaults() -> dict[str, StrategyConfig]:
    plugins = get_plugins()
    defaults: dict[str, StrategyConfig] = {}
    for plugin in plugins.values():
        defaults[plugin.name] = StrategyConfig(
            name=plugin.name,
            entry_windows=list(plugin.entry_windows),
            management_windows=list(plugin.management_windows),
            screener_windows=list(plugin.screener_windows),
            params=_normalize_strategy_params(deepcopy(plugin.params or {}), apply_plugin_normalizer=False),
        )
    return defaults


_PERCENT_PARAM_NAMES = {
    "min_change_from_open",
    "max_change_from_open",
    "min_day_strength",
    "max_day_strength",
    "watchlist_min_change",
}


_TRADE_MANAGEMENT_MODE_VALUES = {"adaptive", "adaptive_ladder", "sr_flip", "none"}




def _normalize_trade_management_mode(value: Any) -> str:
    mode = str(value or "adaptive_ladder").strip().lower()
    if mode not in _TRADE_MANAGEMENT_MODE_VALUES:
        LOG.warning("Unsupported risk.trade_management_mode=%r; using 'adaptive_ladder'. Valid values: %s", mode, sorted(_TRADE_MANAGEMENT_MODE_VALUES))
        return "adaptive_ladder"
    return mode


def _normalize_tv_percent_param(value: Any) -> float:
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("%"):
            return float(raw[:-1].strip())
        return float(raw)
    return float(value)


def _normalize_symbol_tokens(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    invalid_tokens = {"NONE", "NULL", "NAN"}
    for raw in values or []:
        if raw is None:
            continue
        token = str(raw).upper().strip()
        if not token or token in invalid_tokens or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _normalize_pairs_config(values: Any) -> list[PairDefinition]:
    out: list[PairDefinition] = []
    seen: set[tuple[str, str]] = set()
    for item in values or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper().strip()
        reference = str(item.get("reference") or "").upper().strip()
        if not symbol or not reference:
            continue
        key = (symbol, reference)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            PairDefinition(
                symbol=symbol,
                reference=reference,
                side_preference=str(item.get("side_preference") or "both").strip().lower() or "both",
                sector=item.get("sector"),
                industry=item.get("industry"),
            )
        )
    return out


def _normalize_force_flatten_time(value: Any) -> str:
    if isinstance(value, int) and 0 <= value < (24 * 60):
        hh, mm = divmod(int(value), 60)
        return f"{hh:02d}:{mm:02d}"
    if value is None:
        return "15:18"
    return str(value).strip() or "15:18"

def _normalize_options_config(raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw or {})
    underlyings = _normalize_symbol_tokens(out.get("underlyings"))
    if underlyings:
        out["underlyings"] = underlyings
    confirmation_symbols = out.get("confirmation_symbols") or {}
    if isinstance(confirmation_symbols, dict):
        normalized_confirmation: dict[str, str] = {}
        for key, value in confirmation_symbols.items():
            underlying = str(key or "").upper().strip()
            confirm_symbol = str(value or "").upper().strip()
            if not underlying or not confirm_symbol:
                continue
            normalized_confirmation[underlying] = confirm_symbol
        out["confirmation_symbols"] = normalized_confirmation
    out["force_flatten_time"] = _normalize_force_flatten_time(out.get("force_flatten_time", "15:18"))
    return out


def _normalize_strategy_params(
    params: dict[str, Any],
    strategy_name: str | None = None,
    *,
    apply_plugin_normalizer: bool = True,
) -> dict[str, Any]:
    out = dict(params or {})
    if apply_plugin_normalizer and strategy_name is not None:
        out = apply_strategy_param_normalizer(strategy_name, out)
    for key in _PERCENT_PARAM_NAMES:
        if key in out and out[key] is not None:
            out[key] = _normalize_tv_percent_param(out[key])
    return out


def _validate_risk_config(risk: RiskConfig, config_path: Path) -> None:
    """Reject or clamp nonsensical risk parameter values that would cause
    silent misbehaviour at runtime (e.g. negative max_daily_loss inverting
    the daily loss check, or zero max_positions blocking all entries)."""
    errors: list[str] = []
    if risk.max_positions < 1:
        errors.append(f"risk.max_positions must be >= 1, got {risk.max_positions}")
    if risk.risk_per_trade_frac_of_notional <= 0 or risk.risk_per_trade_frac_of_notional > 1.0:
        errors.append(
            "risk.risk_per_trade_frac_of_notional must be in (0, 1.0], got "
            f"{risk.risk_per_trade_frac_of_notional}"
        )
    if risk.max_notional_per_trade <= 0:
        errors.append(f"risk.max_notional_per_trade must be > 0, got {risk.max_notional_per_trade}")
    if risk.max_total_notional <= 0:
        errors.append(f"risk.max_total_notional must be > 0, got {risk.max_total_notional}")
    if risk.max_daily_loss <= 0:
        errors.append(f"risk.max_daily_loss must be > 0, got {risk.max_daily_loss}")
    if risk.default_stop_pct <= 0 or risk.default_stop_pct > 1.0:
        errors.append(f"risk.default_stop_pct must be in (0, 1.0], got {risk.default_stop_pct}")
    if risk.default_target_pct <= 0 or risk.default_target_pct > 1.0:
        errors.append(f"risk.default_target_pct must be in (0, 1.0], got {risk.default_target_pct}")
    if risk.cooldown_minutes < 0:
        errors.append(f"risk.cooldown_minutes must be >= 0, got {risk.cooldown_minutes}")
    if errors:
        raise ValueError(f"{config_path}: invalid risk configuration:\n  " + "\n  ".join(errors))


def _validate_runtime_config(runtime: RuntimeConfig, config_path: Path) -> None:
    """Plausibility checks for runtime cadence and cache settings.

    These fields previously had scattered getattr(..., default) fallbacks in
    call sites (engine.py, data_feed.py, execution.py) that silently papered
    over bad or missing values. With those removed, we validate at load time
    so misconfiguration fails loudly up front."""
    errors: list[str] = []
    if runtime.loop_sleep_seconds <= 0:
        errors.append(f"runtime.loop_sleep_seconds must be > 0, got {runtime.loop_sleep_seconds}")
    if runtime.quote_poll_seconds <= 0:
        errors.append(f"runtime.quote_poll_seconds must be > 0, got {runtime.quote_poll_seconds}")
    if runtime.quote_cache_seconds < 0:
        errors.append(f"runtime.quote_cache_seconds must be >= 0, got {runtime.quote_cache_seconds}")
    if runtime.quote_batch_size < 1:
        errors.append(f"runtime.quote_batch_size must be >= 1, got {runtime.quote_batch_size}")
    if runtime.history_lookback_minutes < 1:
        errors.append(f"runtime.history_lookback_minutes must be >= 1, got {runtime.history_lookback_minutes}")
    if runtime.warmup_minutes < 1:
        errors.append(f"runtime.warmup_minutes must be >= 1, got {runtime.warmup_minutes}")
    if runtime.prewarm_before_windows_minutes < 0:
        errors.append(f"runtime.prewarm_before_windows_minutes must be >= 0, got {runtime.prewarm_before_windows_minutes}")
    if errors:
        raise ValueError(f"{config_path}: invalid runtime configuration:\n  " + "\n  ".join(errors))


def _validate_options_config(options: "ZeroDteOptionsConfig", config_path: Path) -> None:
    """Plausibility checks for options sizing and quote-freshness.

    max_quote_age_seconds was previously read via ``getattr(..., 10)``
    fallback in the engine before Phase 1 validators landed; validation
    replaces that silent default."""
    errors: list[str] = []
    if options.max_quote_age_seconds < 0:
        errors.append(f"options.max_quote_age_seconds must be >= 0, got {options.max_quote_age_seconds}")
    if options.max_loss_per_trade <= 0:
        errors.append(f"options.max_loss_per_trade must be > 0, got {options.max_loss_per_trade}")
    if options.max_contracts_per_trade < 1:
        errors.append(f"options.max_contracts_per_trade must be >= 1, got {options.max_contracts_per_trade}")
    if errors:
        raise ValueError(f"{config_path}: invalid options configuration:\n  " + "\n  ".join(errors))


def load_config(path: str | Path, strategy_override: str | None = None, env_path: str | Path | None = None) -> BotConfig:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            "Copy configs/config.example.yaml to configs/config.yaml or pass --config with a valid YAML path."
        )
    raw = yaml.safe_load(config_path.read_text()) or {}
    if raw.get("strategies") is not None and not isinstance(raw.get("strategies"), dict):
        raise TypeError(f"{config_path}:strategies must be a YAML object when present")

    # Load .env (if present) before resolving secrets. Process env always
    # wins; .env only fills in keys that aren't already set. If env_path
    # is provided (via --env CLI flag) it's loaded with priority and
    # missing-file becomes a hard error.
    explicit_env = Path(env_path).expanduser() if env_path else None
    _load_dotenv(config_path, explicit_env_path=explicit_env)

    strategy = normalize_strategy_name(strategy_override or raw.get("strategy", default_strategy_name()))
    raw["strategy"] = strategy
    schwab_raw = dict(raw.get("schwab", {}) or {})
    tv_raw = dict(raw.get("tradingview", {}) or {})
    tv_raw.pop("cookies_from_browser", None)
    tv_raw.pop("browser", None)

    # Resolve secrets: yaml real value wins, env is the fallback. Missing
    # Schwab credentials are a hard error; sessionid is optional.
    schwab_app_key = _resolve_secret(schwab_raw.get("app_key"), "SCHWAB_APP_KEY")
    schwab_app_secret = _resolve_secret(schwab_raw.get("app_secret"), "SCHWAB_APP_SECRET")
    if not schwab_app_key or not schwab_app_secret:
        missing = []
        if not schwab_app_key:
            missing.append("SCHWAB_APP_KEY (or schwab.app_key)")
        if not schwab_app_secret:
            missing.append("SCHWAB_APP_SECRET (or schwab.app_secret)")
        raise ValueError(
            "Missing Schwab API credentials: "
            + ", ".join(missing)
            + ". Set them in a .env file at the repo root (see .env.example) "
              "or in the schwab section of your config yaml."
        )
    schwab_raw["app_key"] = schwab_app_key
    schwab_raw["app_secret"] = schwab_app_secret

    tv_sessionid = _resolve_secret(tv_raw.get("sessionid"), "TRADINGVIEW_SESSIONID")
    tv_raw["sessionid"] = tv_sessionid  # None is allowed (screener runs without it)

    # account_hash is optional — when None the client auto-resolves the
    # linked account. Allow the .env file (SCHWAB_ACCOUNT_HASH) to supply it
    # so it doesn't have to live in yaml.
    schwab_account_hash = _resolve_secret(schwab_raw.get("account_hash"), "SCHWAB_ACCOUNT_HASH")
    schwab_raw["account_hash"] = schwab_account_hash

    # encryption is a Fernet key used to encrypt the Schwab token DB at rest.
    # Optional — when None the DB is written unencrypted. Source from
    # SCHWAB_ENCRYPTION_KEY so the key isn't committed to yaml.
    schwab_encryption = _resolve_secret(schwab_raw.get("encryption"), "SCHWAB_ENCRYPTION_KEY")
    schwab_raw["encryption"] = schwab_encryption
    risk_raw = dict(raw.get("risk", {}) or {})
    risk_raw["trade_management_mode"] = _normalize_trade_management_mode(risk_raw.get("trade_management_mode", "adaptive_ladder"))
    runtime_raw = dict(raw.get("runtime", {}) or {})
    paper_raw = dict(raw.get("paper", {}) or {})
    dashboard_raw = dict(raw.get("dashboard", {}) or {})
    dashboard_charting_raw = dict(dashboard_raw.pop("charting", {}) or {})
    if "shared" in dashboard_charting_raw:
        raise ValueError("dashboard.charting.shared is no longer supported. Use dashboard.charting.compact and dashboard.charting.expanded only.")
    compact_charting_raw = dict(dashboard_charting_raw.get("compact", {}) or {})
    expanded_charting_raw = dict(dashboard_charting_raw.get("expanded", {}) or {})
    unsupported_charting_keys = [key for key in ("one_minute_max_bars", "1m_max_bars", "ltf_max_bars", "htf_max_bars") if key in dashboard_charting_raw]
    if unsupported_charting_keys:
        raise ValueError(
            "dashboard.charting no longer supports top-level timeframe "
            "max-bars keys. Use dashboard.charting.compact.max_bars and "
            "dashboard.charting.expanded.max_bars instead."
        )
    execution_raw = dict(raw.get("execution", {}) or {})
    candles_raw = dict(raw.get("candles", {}) or {})
    chart_patterns_raw = dict(raw.get("chart_patterns", {}) or {})
    _validate_pattern_config(config_path, candles_raw, chart_patterns_raw)
    support_resistance_raw = dict(raw.get("support_resistance", {}) or {})
    technical_levels_raw = dict(raw.get("technical_levels", {}) or {})
    shared_entry_raw = dict(raw.get("shared_entry", {}) or {})
    shared_exit_raw = dict(raw.get("shared_exit", {}) or {})
    options_raw = _normalize_options_config(raw.get("options", {}))

    strategies = _strategy_defaults()

    for key, value in (raw.get("strategies", {}) or {}).items():
        name = normalize_strategy_name(key)
        base = strategies[name]
        merged_params = deepcopy(base.params)
        merged_params.update(deepcopy(value.get("params", {}) or {}))
        strategies[name] = StrategyConfig(
            name=name,
            entry_windows=deepcopy(value.get("entry_windows", base.entry_windows)),
            management_windows=deepcopy(value.get("management_windows", base.management_windows)),
            screener_windows=deepcopy(value.get("screener_windows", base.screener_windows)),
            params=_normalize_strategy_params(merged_params, name),
        )

    active_base = strategies[strategy]
    strategies[strategy] = StrategyConfig(
        name=active_base.name,
        entry_windows=deepcopy(active_base.entry_windows),
        management_windows=deepcopy(active_base.management_windows),
        screener_windows=deepcopy(active_base.screener_windows),
        params=deepcopy(active_base.params),
    )

    pairs = _normalize_pairs_config(raw.get("pairs", []))

    runtime_cfg = RuntimeConfig(**runtime_raw)
    _validate_runtime_config(runtime_cfg, config_path)
    set_runtime_timezone(runtime_cfg.timezone)
    set_runtime_indicator_mode(runtime_cfg.use_rth_session_indicators)

    risk_cfg = RiskConfig(**risk_raw)
    _validate_risk_config(risk_cfg, config_path)

    options_cfg = ZeroDteOptionsConfig(**options_raw)
    _validate_options_config(options_cfg, config_path)
    if is_option_strategy(strategy) and not options_cfg.underlyings:
        raise ValueError(
            f"{config_path}: active strategy {strategy!r} is an options strategy "
            "but options.underlyings is empty. Add at least one underlying symbol "
            "(e.g. SPY, QQQ) under the options section."
        )

    return BotConfig(
        strategy=strategy,
        schwab=SchwabConfig(**schwab_raw),
        tradingview=TradingViewConfig(**tv_raw),
        risk=risk_cfg,
        runtime=runtime_cfg,
        paper=PaperConfig(**paper_raw),
        dashboard=DashboardConfig(
            **dashboard_raw,
            charting=DashboardChartingConfig(
                compact_chart_timeframe=dashboard_charting_raw.get("compact_chart_timeframe", "ltf"),
                compact=DashboardChartConfig(**compact_charting_raw),
                expanded=DashboardChartConfig(**expanded_charting_raw),
            ),
        ),
        execution=EquityExecutionConfig(**execution_raw),
        candles=CandlesConfig(**candles_raw),
        chart_patterns=ChartPatternsConfig(**chart_patterns_raw),
        support_resistance=SupportResistanceConfig(**support_resistance_raw),
        technical_levels=TechnicalLevelsConfig(**technical_levels_raw),
        shared_entry=SharedEntryLogicConfig(**shared_entry_raw),
        shared_exit=SharedExitLogicConfig(**shared_exit_raw),
        options=options_cfg,
        strategies=strategies,
        pairs=pairs,
    )
